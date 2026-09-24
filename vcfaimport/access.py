"""Named users, roles, and the two-person rule.

Users
  viewer    read everything, change nothing -- the link for a manager
  operator  run the campaign
  admin     also settings, notifications and users
Each user gets a personal console link; only a SHA-256 of its token is stored.
The token `serve` prints at start-up still works and acts as "owner" (admin).

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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ROLES = ("viewer", "operator", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
PENDING, APPROVED, REJECTED, EXECUTED, CANCELLED = "pending", "approved", "rejected", "executed", "cancelled"


class AccessError(Exception):
    """Not allowed, or not possible (a 403 in the console)."""


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
def create_user(store: Any, name: str, role: str, actor: str) -> str:
    """Create (or re-issue) a user; returns the new personal token."""
    name = (name or "").strip()
    if not re.fullmatch(r"[\w.@-]{1,64}", name) or name.lower() == "owner":
        raise AccessError("user names are 1-64 letters, digits, . @ _ or - (and not 'owner')")
    if role not in ROLES:
        raise AccessError("role must be one of {}".format(", ".join(ROLES)))
    token = secrets.token_urlsafe(24)
    store.conn.execute(
        "INSERT INTO users(name, role, token_hash, created_at, created_by, disabled) VALUES(?,?,?,?,?,0)"
        " ON CONFLICT(name) DO UPDATE SET role=excluded.role, token_hash=excluded.token_hash, disabled=0",
        (name, role, _hash(token), _now(), actor))
    store.conn.commit()
    store.log_event("user {} set to {} (new link issued)".format(name, role), actor=actor)
    return token


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
    return [{k: r[k] for k in ("name", "role", "created_at", "created_by", "disabled", "last_seen")}
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
