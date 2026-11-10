"""Named users, roles, and the two-person rule.

Users
  viewer    read everything, change nothing -- the link for a manager
  operator  run the campaign
  admin     also settings, notifications and users
Each user gets a personal console link; only a SHA-256 of its token is stored.
The token `serve` prints at start-up still works and acts as "owner" (admin).
A user may also have a password (PBKDF2-SHA256, salted; never the text itself).
Signing in with it yields a short-lived session token, kept in memory only, that
rides in the same X-VCFA-Token header as a link token does.

Approvals
  When a stage is listed in `require_approval`, asking for it creates a
  request instead of acting. A *different* operator or admin approves it, and
  only then does it run (or, for a scheduled window, become runnable).
  The CLI identifies people by their OS account, so it follows the same rule.
  This is an audit and change-control measure; anyone holding the workspace
  files can still edit them.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ROLES = ("viewer", "operator", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
PENDING, APPROVED, REJECTED, EXECUTED, CANCELLED = "pending", "approved", "rejected", "executed", "cancelled"


class AccessError(Exception):
    """Not allowed, or not possible (a 403 in the console)."""


class LoginDenied(Exception):
    """Wrong name, wrong password or a disabled user -- deliberately not told apart (a 401)."""


class LoginThrottled(Exception):
    """Too many recent failures (a 429)."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def os_user() -> str:
    try:
        return getpass.getuser() or "cli"
    except Exception:  # noqa: BLE001 -- no login name in some service contexts
        return "cli"


def allows(role: Optional[str], needed: str) -> bool:
    return RANK.get(role or "", -1) >= RANK[needed]


# ------------------------------------------------------------------- users
def create_user(store: Any, name: str, role: str, actor: str, password: Optional[str] = None,
                pw_hash: Optional[str] = None) -> str:
    """Create (or re-issue) a user; returns the new personal token.

    With a password the user signs in by name instead, so no link is handed out ("" is
    returned) and the stored token is one nobody holds. Without one, an existing user's
    password is left alone."""
    name = (name or "").strip()
    if not re.fullmatch(r"[\w.@-]{1,64}", name) or name.lower() == "owner":
        raise AccessError("user names are 1-64 letters, digits, . @ _ or - (and not 'owner')")
    if role not in ROLES:
        raise AccessError("role must be one of {}".format(", ".join(ROLES)))
    if pw_hash is None and password is not None:
        pw_hash = hash_password(password)
    token = secrets.token_urlsafe(24)
    store.conn.execute(
        "INSERT INTO users(name, role, token_hash, created_at, created_by, disabled, password_hash)"
        " VALUES(?,?,?,?,?,0,?)"
        " ON CONFLICT(name) DO UPDATE SET role=excluded.role, token_hash=excluded.token_hash, disabled=0,"
        " password_hash=COALESCE(excluded.password_hash, users.password_hash)",
        (name, role, _hash(token), _now(), actor, pw_hash))
    store.conn.commit()
    store.log_event("user {} set to {} ({})".format(
        name, role, "password set" if pw_hash else "new link issued"), actor=actor)
    return "" if pw_hash else token


# --------------------------------------------------------------- passwords
PBKDF2_ITERATIONS = 600_000        # OWASP's figure for PBKDF2-HMAC-SHA256
PASSWORD_MIN, PASSWORD_MAX = 8, 256


def hash_password(password: Any) -> str:
    if not isinstance(password, str) or not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        raise AccessError("a password is {}-{} characters".format(PASSWORD_MIN, PASSWORD_MAX))
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(PBKDF2_ITERATIONS, salt.hex(), digest.hex())


def verify_password(stored: Optional[str], password: str) -> bool:
    """Constant-time check; a missing or malformed hash costs the same work and fails."""
    try:
        scheme, iters, salt, digest = (stored or "").split("$")
        if scheme != "pbkdf2_sha256":
            raise ValueError(scheme)
        n, salt_b, want = int(iters), bytes.fromhex(salt), bytes.fromhex(digest)
        if not 1 <= n <= 5_000_000:
            raise ValueError(iters)
    except ValueError:
        n, salt_b, want = PBKDF2_ITERATIONS, bytes(16), b""
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_b, n)
    return bool(want) and hmac.compare_digest(got, want)


def set_password(store: Any, name: str, password: str, actor: str, pw_hash: Optional[str] = None) -> None:
    """`pw_hash` lets a caller that holds a lock do the slow hashing before taking it."""
    pw_hash = pw_hash or hash_password(password)
    cur =store.conn.execute("UPDATE users SET password_hash=? WHERE name=?", (pw_hash, name))
    store.conn.commit()
    if not cur.rowcount:
        raise AccessError("no user {}".format(name))
    store.log_event("password set for {}".format(name), actor=actor)


def user_row(store: Any, name: str) -> Optional[Dict[str, Any]]:
    """The current record of an enabled user, or None. Role and disabled state are read
    fresh on every request, so disabling someone or changing a role bites at once."""
    row = store.conn.execute("SELECT name, role, password_hash FROM users WHERE name=? AND disabled=0",
                             (name,)).fetchone()
    return dict(row) if row else None


class Sessions:
    """Sign-ins by name and password. Sessions and failure counters live in memory only:
    restarting the console signs everyone out, as a fresh start-up token does.

    The wrong-password limit is per name (so someone can lock a name out for a few
    minutes -- accepted) and per client address."""

    IDLE_SECONDS = 8 * 3600
    MAX_AGE_SECONDS = 24 * 3600
    WINDOW_SECONDS = 300
    MAX_FAILS_NAME = 5
    MAX_FAILS_CLIENT = 20
    MAX_SESSIONS = 500

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._by_hash: Dict[str, Dict[str, Any]] = {}
        self._fails: Dict[str, List[float]] = {}

    def _recent(self, key: str, now: float) -> List[float]:
        fresh = [t for t in self._fails.get(key, []) if now - t < self.WINDOW_SECONDS]
        if fresh:
            self._fails[key] = fresh
        else:
            self._fails.pop(key, None)
        return fresh

    @staticmethod
    def _keys(name: str, client: str) -> List[str]:
        # Behind an SSH tunnel every caller is loopback, so a per-address count would be
        # one global count: anyone's typos would lock everybody out. Only the name counts there.
        keys = ["n:" + name.lower()]
        if not client.startswith("127.") and client not in ("::1", "?"):
            keys.append("c:" + client)
        return keys

    def throttled(self, name: str, client: str) -> bool:
        now = self._clock()
        with self._lock:
            return any(len(self._recent(k, now)) >= (self.MAX_FAILS_NAME if k[0] == "n" else self.MAX_FAILS_CLIENT)
                       for k in self._keys(name, client))

    def fail(self, name: str, client: str) -> None:
        now = self._clock()
        with self._lock:
            for key in self._keys(name, client):
                self._recent(key, now)
                self._fails.setdefault(key, []).append(now)

    def clear_fails(self, name: str) -> None:
        with self._lock:
            self._fails.pop("n:" + name.lower(), None)

    def _dead(self, s: Dict[str, Any], now: float) -> bool:
        return now - s["last"] > self.IDLE_SECONDS or now - s["born"] > self.MAX_AGE_SECONDS

    def start(self, name: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            if len(self._by_hash) >= self.MAX_SESSIONS:
                for h in [h for h, s in self._by_hash.items() if self._dead(s, now)]:
                    del self._by_hash[h]
                if len(self._by_hash) >= self.MAX_SESSIONS:       # still full: drop the stalest
                    del self._by_hash[min(self._by_hash, key=lambda h: self._by_hash[h]["last"])]
            self._by_hash[_hash(token)] = {"name": name, "born": now, "last": now}
        return token

    def lookup(self, token: str) -> Optional[str]:
        """The user a live session belongs to, refreshing its idle clock."""
        now = self._clock()
        h = _hash(token)
        with self._lock:
            s = self._by_hash.get(h)
            if s is None:
                return None
            if self._dead(s, now):
                del self._by_hash[h]
                return None
            s["last"] = now
            return s["name"]

    def end(self, token: str) -> None:
        with self._lock:
            self._by_hash.pop(_hash(token), None)

    def end_user(self, name: str, keep: Optional[str] = None) -> None:
        """Sign a user out everywhere (except the session whose token is `keep`)."""
        keep_hash = _hash(keep) if keep else None
        with self._lock:
            for h in [h for h, s in self._by_hash.items() if s["name"] == name and h != keep_hash]:
                del self._by_hash[h]


def login(store: Any, lock: Any, sessions: Sessions, name: str, password: str, client: str) -> Dict[str, Any]:
    """Check a name and password; returns {"name", "role", "session"}.

    Unknown name, wrong password and disabled user all raise the same LoginDenied. The
    store is touched under `lock` only briefly; the slow PBKDF2 runs outside it, and
    runs even for an unknown name so the timing does not say whether the name exists."""
    name = name.strip()
    if sessions.throttled(name, client):
        raise LoginThrottled("too many failed sign-ins; wait a few minutes")
    with lock:
        row = user_row(store, name)
    ok = verify_password(row["password_hash"] if row else None, password)
    if not (row and ok):
        sessions.fail(name, client)
        with lock:
            # the typed name goes in the message only: as `actor` anyone could blame "admin"
            store.log_event("failed sign-in for {!r} from {}".format(name[:64], client), "warn")
        raise LoginDenied("wrong name or password")
    sessions.clear_fails(name)
    with lock:
        store.log_event("{} signed in from {}".format(row["name"], client), actor=row["name"])
        touch(store, row["name"])
    return {"name": row["name"], "role": row["role"], "session": sessions.start(row["name"])}




def authenticate(store: Any, token: str) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    digest = _hash(token)
    for row in store.conn.execute("SELECT * FROM users WHERE disabled=0"):
        if hmac.compare_digest(row["token_hash"], digest):
            return {"name": row["name"], "role": row["role"]}
    return None


def touch(store: Any, name: str) -> None:
    store.conn.execute("UPDATE users SET last_seen=? WHERE name=?", (_now(), name))
    store.conn.commit()


def list_users(store: Any) -> List[Dict[str, Any]]:
    return [dict({k: r[k] for k in ("name", "role", "created_at", "created_by", "disabled", "last_seen")},
                 has_password=bool(r["password_hash"]))
            for r in store.conn.execute("SELECT * FROM users ORDER BY name")]


def set_disabled(store: Any, name: str, disabled: bool, actor: str) -> None:
    cur = store.conn.execute("UPDATE users SET disabled=? WHERE name=?", (1 if disabled else 0, name))
    store.conn.commit()
    if not cur.rowcount:
        raise AccessError("no user {}".format(name))
    store.log_event("user {} {}".format(name, "disabled" if disabled else "enabled"), actor=actor)


# --------------------------------------------------------------- approvals
def needs_approval(cfg: Any, stage: str, body: Dict[str, Any]) -> bool:
    if body.get("dry_run"):
        return False
    return stage in (getattr(cfg, "require_approval", None) or [])


def summarize(stage: str, body: Dict[str, Any]) -> str:
    bits = [stage]
    if body.get("waves"):
        bits.append("wave " + ", ".join(str(w) for w in body["waves"]))
    if body.get("folders"):
        bits.append("in " + ", ".join(body["folders"]))
    if body.get("morefs"):
        bits.append("{} VM(s)".format(len(body["morefs"])))
    if body.get("batches"):
        bits.append("{} batch(es)".format(len(body["batches"])))
    if body.get("failed"):
        bits.append("every failed batch")
    return " ".join(bits)


def request(store: Any, stage: str, body: Dict[str, Any], actor: str,
            schedule_id: Optional[int] = None) -> Dict[str, Any]:
    clean = {k: v for k, v in body.items() if k not in ("confirm", "approval")}
    cur = store.conn.execute(
        "INSERT INTO approvals(stage, body_json, summary, requested_by, requested_at, state, schedule_id)"
        " VALUES(?,?,?,?,?,?,?)",
        (stage, json.dumps(clean), summarize(stage, clean), actor, _now(), PENDING, schedule_id))
    store.conn.commit()
    store.log_event("approval #{} requested: {}".format(cur.lastrowid, summarize(stage, clean)),
                    "warn", actor=actor)
    return get(store, cur.lastrowid)


def get(store: Any, approval_id: int) -> Dict[str, Any]:
    row = store.conn.execute("SELECT * FROM approvals WHERE id=?", (int(approval_id),)).fetchone()
    if row is None:
        raise AccessError("no approval #{}".format(approval_id))
    out = dict(row)
    out["body"] = json.loads(out.pop("body_json") or "{}")
    return out


def list_approvals(store: Any, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    sql = "SELECT id FROM approvals"
    params: List[Any] = []
    if state:
        sql += " WHERE state=?"
        params.append(state)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [get(store, r["id"]) for r in store.conn.execute(sql, params)]


def decide(store: Any, approval_id: int, approve: bool, actor: str, note: str = "") -> Dict[str, Any]:
    a = get(store, approval_id)
    if a["state"] != PENDING:
        raise AccessError("approval #{} is already {}".format(approval_id, a["state"]))
    if actor == a["requested_by"]:
        raise AccessError("a request cannot be approved or rejected by the person who made it")
    state = APPROVED if approve else REJECTED
    store.conn.execute("UPDATE approvals SET state=?, decided_by=?, decided_at=?, note=? WHERE id=?",
                       (state, actor, _now(), note or None, int(approval_id)))
    store.conn.commit()
    store.log_event("approval #{} {} by {}{}".format(approval_id, state, actor,
                                                    ": " + note if note else ""),
                    "info" if approve else "warn", actor=actor)
    return get(store, approval_id)


def consume(store: Any, approval_id: int, stage: str, job_id: Optional[str] = None) -> Dict[str, Any]:
    """Use an approved request exactly once, for the stage it was granted for."""
    a = get(store, approval_id)
    if a["stage"] != stage:
        raise AccessError("approval #{} is for {}, not {}".format(approval_id, a["stage"], stage))
    if a["state"] != APPROVED:
        raise AccessError("approval #{} is {}, not approved".format(approval_id, a["state"]))
    store.conn.execute("UPDATE approvals SET state=?, job_id=? WHERE id=?",
                       (EXECUTED, job_id, int(approval_id)))
    store.conn.commit()
    return get(store, approval_id)


def cancel(store: Any, approval_id: int, actor: str) -> None:
    a = get(store, approval_id)
    if a["state"] not in (PENDING, APPROVED):
        raise AccessError("approval #{} is already {}".format(approval_id, a["state"]))
    store.conn.execute("UPDATE approvals SET state=?, decided_by=?, decided_at=? WHERE id=?",
                       (CANCELLED, actor, _now(), int(approval_id)))
    store.conn.commit()
