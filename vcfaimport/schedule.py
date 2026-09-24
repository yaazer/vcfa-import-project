"""Change windows: run a precheck or import between a start and an end time.

A schedule is a stage plus the same options a manual run takes (waves, folder
scope, batch size, ...) and a window. It starts at `start_at`; it never
*starts* a batch that its measured duration says would not finish before
`end_at`, and at `end_at` it stops like the Stop button: nothing new applied,
what is on the cluster polled to completion.

Something has to be awake to start it: the web console runs a scheduler, and
`vcfa-import schedule tick` does the same from Windows Task Scheduler or cron.

States: scheduled -> running -> done | stopped (window closed with work left)
        | failed; awaiting_approval (two-person rule) -> scheduled | cancelled;
        missed (the window passed while nothing was running); cancelled.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

SCHEDULED, AWAITING, RUNNING, DONE, STOPPED, FAILED, MISSED, CANCELLED = (
    "scheduled", "awaiting_approval", "running", "done", "stopped", "failed", "missed", "cancelled")
OPEN_STATES = (SCHEDULED, AWAITING)
STAGES = ("precheck", "import")
FMT = "%Y-%m-%dT%H:%M:%SZ"


class ScheduleError(Exception):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def parse_when(text: str) -> datetime:
    """'2026-09-26 22:00' (local time), or ISO 8601 with Z or an offset."""
    raw = (text or "").strip()
    if not raw:
        raise ScheduleError("a start and an end time are required")
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw.replace(" ", "T", 1) if " " in raw and "T" not in raw else raw)
    except ValueError:
        raise ScheduleError("'{}' is not a date and time like 2026-09-26 22:00".format(text))
    if dt.tzinfo is None:
        dt = dt.astimezone()           # a bare time means this machine's local time
    return dt.astimezone(timezone.utc)


def _parse(value: str) -> datetime:
    return datetime.strptime(value, FMT).replace(tzinfo=timezone.utc)


def get(store: Any, sid: int) -> Dict[str, Any]:
    row = store.conn.execute("SELECT * FROM schedules WHERE id=?", (int(sid),)).fetchone()
    if row is None:
        raise ScheduleError("no schedule #{}".format(sid))
    out = dict(row)
    out["body"] = json.loads(out.pop("body_json") or "{}")
    return out


def list_schedules(store: Any, include_closed: bool = True, limit: int = 200) -> List[Dict[str, Any]]:
    sql = "SELECT id FROM schedules"
    if not include_closed:
        sql += " WHERE state IN ('scheduled','awaiting_approval','running')"
    sql += " ORDER BY start_at DESC LIMIT ?"
    return [get(store, r["id"]) for r in store.conn.execute(sql, (limit,))]


def create(store: Any, stage: str, body: Dict[str, Any], start: datetime, end: datetime,
           actor: str, needs_approval: bool = False) -> Dict[str, Any]:
    if stage not in STAGES:
        raise ScheduleError("a window runs a precheck or an import")
    if end <= start:
        raise ScheduleError("the window must end after it starts")
    if end <= now_utc():
        raise ScheduleError("that window is already over")
    if end - start < timedelta(minutes=5):
        raise ScheduleError("a window must be at least 5 minutes long")
    clean = {k: v for k, v in body.items() if k not in ("confirm", "approval", "start_at", "end_at")}
    clean["stage"] = stage
    cur = store.conn.execute(
        "INSERT INTO schedules(stage, body_json, start_at, end_at, state, created_by, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (stage, json.dumps(clean), iso(start), iso(end), AWAITING if needs_approval else SCHEDULED,
         actor, iso(now_utc())))
    store.conn.commit()
    store.log_event("schedule #{}: {} window {} -> {}".format(cur.lastrowid, stage, iso(start), iso(end)),
                    actor=actor)
    return get(store, cur.lastrowid)


def mark(store: Any, sid: int, state: str, message: Optional[str] = None, job_id: Optional[str] = None,
         approval_id: Optional[int] = None) -> Dict[str, Any]:
    sets, params = ["state=?"], [state]
    if message is not None:
        sets.append("message=?"); params.append(message)
    if job_id is not None:
        sets.append("job_id=?"); params.append(job_id)
    if approval_id is not None:
        sets.append("approval_id=?"); params.append(approval_id)
    if state == RUNNING:
        sets.append("started_at=?"); params.append(iso(now_utc()))
    if state in (DONE, STOPPED, FAILED, MISSED, CANCELLED):
        sets.append("finished_at=?"); params.append(iso(now_utc()))
    params.append(int(sid))
    store.conn.execute("UPDATE schedules SET {} WHERE id=?".format(", ".join(sets)), params)
    store.conn.commit()
    return get(store, sid)


def cancel(store: Any, sid: int, actor: str) -> Dict[str, Any]:
    s = get(store, sid)
    if s["state"] not in OPEN_STATES:
        raise ScheduleError("schedule #{} is {}; only a waiting window can be cancelled".format(sid, s["state"]))
    store.log_event("schedule #{} cancelled".format(sid), "warn", actor=actor)
    return mark(store, sid, CANCELLED, "cancelled by {}".format(actor))


def on_approval(store: Any, approval: Dict[str, Any]) -> None:
    """An approval decision for a window releases or cancels it."""
    sid = approval.get("schedule_id")
    if not sid:
        return
    s = get(store, sid)
    if s["state"] != AWAITING:
        return
    if approval["state"] == "approved":
        mark(store, sid, SCHEDULED, "approved by {}".format(approval["decided_by"]))
    elif approval["state"] in ("rejected", "cancelled"):
        mark(store, sid, CANCELLED, "approval {} by {}".format(approval["state"], approval["decided_by"]))


def due(store: Any, at: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Windows that are open now and have not started."""
    t = iso(at or now_utc())
    rows = store.conn.execute(
        "SELECT id FROM schedules WHERE state=? AND start_at<=? AND end_at>? ORDER BY start_at",
        (SCHEDULED, t, t))
    return [get(store, r["id"]) for r in rows]


def sweep_missed(store: Any, at: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Windows that closed without starting (nothing was running to start them)."""
    t = iso(at or now_utc())
    missed = []
    for r in store.conn.execute("SELECT id, state FROM schedules WHERE state IN (?,?) AND end_at<=?",
                                (SCHEDULED, AWAITING, t)).fetchall():
        why = ("never approved" if r["state"] == AWAITING
               else "nothing was running to start it (keep the console or `schedule tick` running)")
        missed.append(mark(store, r["id"], MISSED, "the window closed: " + why))
    return missed


def deadline(s: Dict[str, Any]) -> float:
    return _parse(s["end_at"]).timestamp()


def next_open(store: Any) -> Optional[Dict[str, Any]]:
    row = store.conn.execute(
        "SELECT id FROM schedules WHERE state IN ('scheduled','awaiting_approval','running')"
        " ORDER BY start_at LIMIT 1").fetchone()
    return get(store, row["id"]) if row else None
