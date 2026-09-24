"""Operations shared by the command line and the web console.

cli.py prints these results as text; web/api.py returns them as JSON. Keeping
the logic in one place means the two front ends cannot disagree about what an
action does, which VMs it touches, or what it refuses.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .discovery import match_folder_map, _match_mapping  # noqa: F401  (re-exported for web)
from .engine import STAGE_IMPORT, AbortRun, Engine
from .kube import Kubectl, KubectlError
from .folders import folder_matches, norm_folder
from . import state as st

Log = Callable[[str], None]


def store_path(cfg: Config) -> str:
    return str(Path(cfg.workdir).expanduser() / "state.db")


class WorkspaceBusy(Exception):
    """Another process (or console job) is already changing this campaign on the cluster."""


class WorkspaceLock:
    """One cluster-changing operation per workspace, across processes.

    Two `run`s against one state.db plan the same pending VMs and apply
    duplicate batches for them (observed: 6 applies where 4 were due), which
    on a real Supervisor collide on the child ImportOperation names. This is
    an OS file lock: it is released by the OS if the holder dies, so a crash
    can never leave the workspace wedged. The holder's description is kept in
    the file for the error message; the lock itself sits on a byte far past it
    (Windows locks are mandatory, and the text must stay readable).
    """

    LOCK_OFFSET = 1 << 20

    def __init__(self, cfg: Config, what: str):
        self.path = Path(cfg.workdir).expanduser() / "run.lock"
        self.what = what
        self._fh = None

    def acquire(self) -> "WorkspaceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            fh.seek(self.LOCK_OFFSET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            holder = self.holder() or "another process"
            raise WorkspaceBusy(
                "this workspace is busy: {} is already changing it on the cluster. Wait for it "
                "to finish (or stop it) — two runs at once would apply duplicate batches for "
                "the same VMs.".format(holder))
        fh.seek(0)
        fh.truncate()
        fh.write("{} (pid {}, since {})".format(
            self.what, os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
        fh.flush()
        self._fh = fh
        return self

    def holder(self) -> str:
        try:
            return self.path.read_bytes()[:300].decode("utf-8", "replace").strip("\x00 \n")
        except OSError:
            return ""

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            fh.truncate()
            fh.seek(self.LOCK_OFFSET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    def __enter__(self) -> "WorkspaceLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


class Workspace:
    """A campaign's config, state store, kubectl wrapper and engine, opened together.

    `actor` names who is acting (OS user for the CLI, console user for web
    jobs); it is written to every transition, event and ledger line. The
    workspace settings (console Settings page) are layered onto cfg here.
    """

    def __init__(self, cfg: Config, *, dry_run: bool = False, verbose: bool = False,
                 log: Optional[Log] = None, check_same_thread: bool = True,
                 actor: Optional[str] = None):
        cfg.validate()
        Path(cfg.workdir).expanduser().mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.log: Log = log or (lambda msg: None)
        self.store = st.Store(store_path(cfg), ledger_path=cfg.ledger_path,
                              check_same_thread=check_same_thread)
        self.store.actor = actor
        from . import settings as _settings
        _settings.apply(cfg, self.store)
        self.kube = Kubectl(cfg, dry_run=dry_run, verbose=verbose, log=self.log)
        self.engine = Engine(cfg, self.store, self.kube, self.log)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ------------------------------------------------------------------ queue edits
def skip_vms(store: st.Store, morefs: Sequence[str], unskip: bool = False) -> Tuple[int, List[str]]:
    """Exclude VMs from the campaign (or bring them back). Returns (changed, refusals)."""
    changed, notes = 0, []
    for moref in morefs:
        row = store.get_vm(moref)
        if row is None:
            notes.append("{} is not in the inventory".format(moref))
            continue
        if row["state"] in st.WAVE_LOCKED_STATES:
            notes.append("{} is {}; refusing to skip".format(moref, row["state"]))
            continue
        if unskip and row["state"] != st.S_SKIPPED:
            continue
        target = st.S_PENDING if unskip else st.S_SKIPPED
        store.set_vm_state(moref, target, message="manually {}".format(
            "unskipped" if unskip else "skipped"))
        changed += 1
    return changed, notes


# ------------------------------------------------------------------- execution
def eligible_by_wave(engine: Engine, stage: str, waves: Optional[Sequence[int]] = None,
                     morefs: Optional[Sequence[str]] = None, include_failed: bool = False,
                     folders: Optional[Sequence[str]] = None,
                     folder_exact: bool = False) -> List[Tuple[int, int]]:
    """[(wave, eligible VM count)] for the waves that have something to do."""
    rows = []
    for wave in (waves or engine.store.waves()):
        eligible = engine.eligible(stage, wave, morefs=morefs, include_failed=include_failed,
                                   folders=folders, folder_exact=folder_exact)
        if eligible:
            rows.append((wave, len(eligible)))
    return rows


def holdbacks(engine: Engine, stage: str, waves: Optional[Sequence[int]] = None,
              morefs: Optional[Sequence[str]] = None, include_failed: bool = False,
              folders: Optional[Sequence[str]] = None, folder_exact: bool = False) -> List[str]:
    """Why VMs in scope are being kept out of this run (readiness, apps kept together).

    Every wave in scope is asked, including one held back entirely -- that wave
    has nothing eligible, so it would otherwise be silently missing from the plan."""
    notes: List[str] = []
    for wave in (waves or engine.store.waves()):
        engine.eligible(stage, wave, morefs=morefs, include_failed=include_failed,
                        folders=folders, folder_exact=folder_exact)
        notes.extend("wave {}: {}".format(wave, h) for h in engine.holdbacks)
    return notes


def run_stage(engine: Engine, stage: str, *, waves: Optional[Sequence[int]] = None,
              limit: int = 0, morefs: Optional[Sequence[str]] = None,
              include_failed: bool = False, folders: Optional[Sequence[str]] = None,
              folder_exact: bool = False, rollback_failed: bool = False,
              log: Optional[Log] = None, deadline: Optional[float] = None) -> Dict[str, Any]:
    """engine.execute plus the optional --rollback-failed follow-up.

    Returns the totals with a "halted" key: the circuit breaker's reason, or None.
    `deadline` (epoch seconds) is a change window's end: no batch is started
    that its measured duration says would not finish by then.
    KeyboardInterrupt is left to the caller.
    """
    say = log or engine.log
    halted = None
    if deadline is not None:
        from .estimate import batch_seconds
        engine.deadline = deadline
        engine.deadline_batch_s = float(batch_seconds(engine.store, stage)["seconds"])
    try:
        totals = engine.execute(stage, waves=waves, limit=limit, morefs=morefs,
                                include_failed=include_failed, watch_only=False,
                                folders=folders, folder_exact=folder_exact)
    except AbortRun as exc:
        halted = str(exc)
        totals = {"applied": 0, "succeeded": 0, "failed": 1, "awaiting_commit": 0, "batches": 0}
    if stage == STAGE_IMPORT and rollback_failed and totals["failed"]:
        rollback_failed_after_run(engine, waves, folders, folder_exact, say)
    totals["halted"] = halted
    totals["hit_deadline"] = engine.hit_deadline
    totals["holdbacks"] = list(engine.holdbacks)
    return totals


def rollback_failed_after_run(engine: Engine, waves: Optional[Sequence[int]],
                              folders: Optional[Sequence[str]] = None,
                              folder_exact: bool = False, log: Optional[Log] = None) -> None:
    """--rollback-failed: hand every failed import back to vCenter, then stop.

    Same mechanism as `rollback --failed` — rollbackAction is patched onto
    batches that have already run and failed — just without a second command.
    Batches are left on the cluster for `cleanup`; nothing is deleted here.
    """
    say = log or engine.log
    targets: List[sqlite3.Row] = []
    for wave in (waves or [None]):
        rows, notes = engine.rollback_targets(failed_only=not folders, wave=wave,
                                              folders=folders, folder_exact=folder_exact)
        for note in notes:
            say("  ! " + note)
        targets += [r for r in rows if r not in targets]
    if not targets:
        return
    say("")
    say("--rollback-failed: reverting {} batch(es) with failed imports".format(len(targets)))
    result = engine.rollback(targets, action="Immediate", wait=True, delete=False)
    say("  {} VM(s) confirmed back under vCenter; {} batch(es) still reverting".format(
        result["reverted"], len(result["pending"])))
    for err in result["errors"]:
        say("  ! " + err)
    if result["reverted"]:
        say("  rolled-back VMs are retryable with `retry`; delete their batches with `cleanup`")


def batch_members(store: st.Store, batch: sqlite3.Row) -> List[sqlite3.Row]:
    return [v for v in store.query_vms(batch_name=batch["name"])
            if v["namespace"] == batch["namespace"]]


def rollback_preview(engine: Engine, batches: Sequence[sqlite3.Row]) -> List[Dict[str, Any]]:
    out = []
    for b in batches:
        members = batch_members(engine.store, b)
        out.append({
            "name": b["name"], "namespace": b["namespace"], "state": b["state"],
            "wave": b["wave"],
            "revert": sum(1 for v in members if v["state"] in engine.ROLLBACKABLE_STATES),
            "committed": sum(1 for v in members if v["state"] == st.S_COMMITTED),
        })
    return out


def abandon_preview(store: st.Store, batches: Sequence[sqlite3.Row]) -> List[Dict[str, Any]]:
    return [{"name": b["name"], "namespace": b["namespace"], "stage": b["stage"],
             "state": b["state"], "wave": b["wave"], "vms": len(batch_members(store, b))}
            for b in batches]


# ---------------------------------------------------------------- snapshots
FAILED_STATES = (st.S_FAILED, st.S_PRECHECK_FAILED)
IN_FLIGHT_STATES = (st.S_PRECHECK_RUNNING, st.S_IMPORTING, st.S_ROLLING_BACK)


def vm_summary(row: sqlite3.Row) -> Dict[str, Any]:
    """The compact per-VM record the web console lists."""
    nics = json.loads(row["nics_json"] or "[]")
    return {
        "moref": row["moref"], "vm_name": row["vm_name"], "namespace": row["namespace"],
        "wave": row["wave"], "group": row["grp"], "state": row["state"],
        "attempts": row["attempts"], "batch_name": row["batch_name"],
        "precheck_batch": row["precheck_batch"], "phase": row["last_phase"],
        "message": row["message"], "folder": row["src_folder"],
        "cluster": row["src_cluster"], "power": row["src_power"],
        "subnets": [n.get("subnet") for n in nics if n.get("subnet")],
        "updated_at": row["updated_at"],
        "locked": row["state"] in st.WAVE_LOCKED_STATES,
    }


def overview(store: st.Store, cfg: Config) -> Dict[str, Any]:
    """Everything the dashboard shows, in a handful of queries."""
    counts = store.counts()
    total = sum(counts.values())
    by_wave = store.state_matrix("wave")
    by_ns = store.state_matrix("namespace")
    live = store.query_batches(states=[st.B_APPLIED, st.B_RUNNING, st.B_ROLLING_BACK])
    failures = store.query_vms(states=list(FAILED_STATES), order="updated_at DESC", limit=8)
    return {
        "counts": counts,
        "total": total,
        "committed": counts.get(st.S_COMMITTED, 0),
        "failed": sum(counts.get(s, 0) for s in FAILED_STATES),
        "in_flight": sum(counts.get(s, 0) for s in IN_FLIGHT_STATES),
        "awaiting_commit": counts.get(st.S_AWAITING_COMMIT, 0),
        "waves": [{"wave": w, "total": sum(c.values()), "counts": c}
                  for w, c in sorted(by_wave.items())],
        "namespaces": [{"namespace": n, "total": sum(c.values()), "counts": c}
                       for n, c in sorted(by_ns.items())],
        "live_batches": [dict(b) for b in live],
        "recent_failures": [vm_summary(v) for v in failures],
        "discovered": store.discovered_counts(),
        "meta": {
            "run_id": store.get_meta("run_id"),
            "vcenter": store.get_meta("vcenter"),
            "discovered_at": store.get_meta("discovered_at"),
        },
        "stats": store.transition_stats(),
    }


# ------------------------------------------------------------------- triage
# What a failure message means, and what to do about it. Patterns come from
# what has actually been seen on a Supervisor (see CLAUDE.md) and from the
# tool's own messages. First match wins.
KNOWN_ISSUES: List[Dict[str, Any]] = [
    {"id": "dns", "pattern": r"lookup \S+ on [\d.:]+.*i/o timeout|127\.0\.0\.53:53",
     "title": "DNS lookup from the Supervisor timed out",
     "advice": "The Supervisor could not resolve vCenter. In the lab this was a wedged "
               "systemd-resolved on a control-plane node: restart it there (or fix the "
               "Supervisor's DNS servers), then retry these VMs.",
     "actions": ["retry"]},
    {"id": "tools", "pattern": r"tools.{0,20}not running|toolsnotrunning",
     "title": "VMware Tools is not running",
     "advice": "The operator needs VMware Tools in the guest. Power the VM on and start "
               "Tools, then retry — or skip VMs that should not be imported.",
     "actions": ["retry", "skip"]},
    {"id": "collision",
     "pattern": r"does not match number of operations|already exists|name collision",
     "title": "Another batch already owns this VM's operation",
     "advice": "Two batches for the same VM collide on the child ImportOperation name, and "
               "the second one never starts. Abandon the older batch (precheck batches are "
               "safe to abandon), then retry.",
     "actions": ["abandon", "retry"]},
    {"id": "rollback", "pattern": r"ROLLBACK FAILED",
     "title": "The operator could not hand the VM back to vCenter",
     "advice": "Needs a human: check the VM in vCenter and its ImportOperation in the "
               "namespace before doing anything else with it.",
     "actions": []},
    {"id": "apply", "pattern": r"batch apply failed|forbidden|unauthori[sz]ed",
     "title": "The batch could not be applied",
     "advice": "Nothing reached the operator. Run Preflight to check the context, RBAC, "
               "CRDs and namespaces, then retry.",
     "actions": ["retry"]},
    {"id": "vanished", "pattern": r"disappeared from the cluster|BatchGone",
     "title": "The batch was deleted outside this tool",
     "advice": "VM ownership is unknown. Verify each VM in vCenter, then retry.",
     "actions": ["retry"]},
    {"id": "timeout", "pattern": r"batch_timeout_minutes",
     "title": "The batch timed out",
     "advice": "The operator never reported a result. Check the batch on the cluster; "
               "refresh to pick up a late result, or roll back.",
     "actions": ["rollback"]},
    {"id": "subnet", "pattern": r"subnet",
     "title": "Subnet reference problem",
     "advice": "Precheck does not validate the subnet reference. Check the portgroup map "
               "and that the Subnet exists (in a VPC namespace it lives in the VPC's own "
               "namespace and is referenced by plain name).",
     "actions": ["retry"]},
]


def normalise_message(message: str) -> str:
    """Collapse VM-specific details so identical failures group together."""
    text = (message or "").strip() or "(no message)"
    # Batch names (<prefix>-w<wave>-<group>-<seq>-<hash>) and namespace flags
    # differ per batch, not per cause.
    text = re.sub(r"\b[a-z][a-z0-9]*-w\d+-[a-z0-9-]*?-\d{3}-[0-9a-f]{5}\b", "<batch>", text)
    text = re.sub(r"(\s-n|--namespace)[ =]\S+", r"\1 <ns>", text)
    text = re.sub(r"vm-\d+", "vm-<id>", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", text)
    text = re.sub(r"(['\"]).{1,80}?\1", r"\1…\1", text)
    text = re.sub(r"\d+", "<n>", text)
    return re.sub(r"\s+", " ", text)[:240]


def classify_failure(message: str) -> Optional[Dict[str, Any]]:
    for issue in KNOWN_ISSUES:
        if re.search(issue["pattern"], message or "", re.IGNORECASE):
            return {k: v for k, v in issue.items() if k != "pattern"}
    return None


def failure_groups(store: st.Store) -> Dict[str, Any]:
    """Failed VMs grouped by what went wrong, each with advice and suggested actions."""
    groups: Dict[str, Dict[str, Any]] = {}
    for row in store.query_vms(states=list(FAILED_STATES), order="updated_at DESC"):
        issue = classify_failure(row["message"] or row["last_phase"] or "")
        key = (issue["id"] if issue else "") + "|" + normalise_message(row["message"] or "")
        g = groups.setdefault(key, {
            "key": key, "issue": issue, "message": row["message"] or row["last_phase"] or "",
            "pattern": normalise_message(row["message"] or ""), "vms": [], "stages": {},
        })
        stage = "precheck" if row["state"] == st.S_PRECHECK_FAILED else "import"
        g["stages"][stage] = g["stages"].get(stage, 0) + 1
        g["vms"].append(vm_summary(row))
    ordered = sorted(groups.values(), key=lambda g: -len(g["vms"]))
    for g in ordered:
        g["count"] = len(g["vms"])

    # In flight, but inside a batch that timed out or vanished: nobody is polling them.
    stuck_batches = {(b["namespace"], b["name"]): b
                     for b in store.query_batches(states=[st.B_TIMEDOUT])}
    stalled = [vm_summary(v) for v in store.query_vms(states=list(IN_FLIGHT_STATES))
               if (v["namespace"], v["batch_name"]) in stuck_batches
               or (v["namespace"], v["precheck_batch"]) in stuck_batches]
    rolled_back = [vm_summary(v) for v in store.query_vms(states=[st.S_ROLLED_BACK])]
    return {
        "groups": ordered,
        "stalled": stalled,
        "rolled_back": rolled_back,
        "cleanup": [dict(b) for b in store.query_batches(states=[st.B_ROLLED_BACK])],
        "awaiting_commit": [vm_summary(v) for v in store.query_vms(states=[st.S_AWAITING_COMMIT])],
    }


# ----------------------------------------------------------------- mapping
def mapping_coverage(rows: Sequence[sqlite3.Row], folder_mapping: Dict[str, Any],
                     network_mapping: Dict[str, Any]) -> Dict[str, Any]:
    """For the selected VMs: which folders and networks the maps cover, and how."""
    folders: Dict[str, Dict[str, Any]] = {}
    networks: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        path = r["folder"] or ""
        f = folders.setdefault(path, {"folder": path, "vms": 0, "explicit_namespace": 0})
        f["vms"] += 1
        if (r["namespace"] or "").strip():
            f["explicit_namespace"] += 1
        for net in [n for n in (r["networks"] or "").split(",") if n]:
            n = networks.setdefault(net, {"network": net, "vms": 0})
            n["vms"] += 1
    for path, f in folders.items():
        best = None
        for pattern in folder_mapping:
            if folder_matches(path, [pattern]):
                if best is None or len(norm_folder(pattern)) > len(norm_folder(best)):
                    best = pattern
        entry = folder_mapping.get(best) if best else None
        f["pattern"] = best
        f["namespace"] = entry.namespace if entry else None
        f["wave"] = entry.wave if entry else None
    for net, n in networks.items():
        entry = _match_mapping(net, network_mapping)
        n["subnet"] = entry.subnet if entry else None
        n["namespace"] = entry.namespace if entry else None
        n["mapped"] = entry is not None
    return {
        "folders": sorted(folders.values(), key=lambda f: f["folder"].lower()),
        "networks": sorted(networks.values(), key=lambda n: n["network"].lower()),
    }


# ------------------------------------------------------------------ waves & apps
def move_wave(store: st.Store, cfg: Config, morefs: Sequence[str], wave: int,
              with_app: Optional[bool] = None) -> Dict[str, Any]:
    """Move VMs to a wave; with apps kept together, their whole applications move."""
    together = cfg.app_together if with_app is None else with_app
    targets = list(morefs)
    pulled: List[str] = []
    if together:
        apps = sorted({r["app"] for r in store.query_vms(morefs=list(morefs)) if r["app"]})
        extra = [m for m in store.app_members(apps) if m not in set(targets)]
        pulled = extra
        targets += extra
    moved, refused = store.set_vm_wave(targets, wave)
    return {"moved": moved, "refused": refused, "pulled_with_app": len(pulled)}


def app_splits(store: st.Store) -> List[Dict[str, Any]]:
    """Applications whose VMs are spread over more than one wave."""
    waves: Dict[str, Dict[int, int]] = {}
    for r in store.conn.execute("SELECT app, wave, COUNT(*) AS n FROM vms WHERE app != '' GROUP BY app, wave"):
        waves.setdefault(r["app"], {})[r["wave"]] = r["n"]
    return [{"app": a, "waves": w} for a, w in sorted(waves.items()) if len(w) > 1]


# ------------------------------------------------------------------ notifications
def notifier(cfg: Config) -> Callable[..., Any]:
    """send(event, title, text, fields) for this workspace's channels.

    Delivery results are logged as events through a short-lived connection
    (the notification threads cannot share the caller's).
    """
    from . import notify as _notify

    path, ledger = store_path(cfg), cfg.ledger_path

    def record(level: str, message: str) -> None:
        try:
            with st.Store(path, ledger_path=ledger) as s2:
                s2.log_event(message, level, actor="notify")
        except Exception:  # noqa: BLE001 -- logging a notification must never fail anything
            pass

    def send(event: str, title: str, text: str = "", fields: Optional[Dict[str, Any]] = None,
             wait: bool = False) -> None:
        channels = getattr(cfg, "notify", None) or []
        if channels:
            _notify.send(channels, event, title, text, fields or {},
                         {"context": cfg.context, "workdir": str(cfg.workdir)}, record=record, wait=wait)
    return send


def notify_run(cfg: Config, kind: str, title: str, status: str, result: Dict[str, Any],
               actor: Optional[str] = None, wait: bool = False) -> None:
    """The notifications a finished run (or job) warrants."""
    send = notifier(cfg)
    fields = {"by": actor, "applied": result.get("applied"), "succeeded": result.get("succeeded"),
              "failed": result.get("failed"), "awaiting commit": result.get("awaiting_commit")}
    if result.get("halted"):
        send("circuit_breaker", "Run halted: " + title, str(result["halted"]), fields, wait)
    event = {"succeeded": "job_succeeded", "warning": "job_warning", "failed": "job_failed",
             "stopped": "job_warning"}.get(status, "job_warning")
    text = "{} {}".format(title, {"succeeded": "finished cleanly", "warning": "finished with failures",
                                  "failed": "failed", "stopped": "was stopped"}.get(status, status))
    if result.get("error"):
        text += ": " + str(result["error"])[:300]
    send(event, title + " — " + status, text, fields, wait)
    if kind in ("execute", "import") and result.get("awaiting_commit"):
        send("awaiting_commit", "{} import(s) waiting for commit".format(result["awaiting_commit"]),
             "commitAction is Wait: commit them or roll them back.", fields, wait)


# -------------------------------------------------------------------- schedules
def run_schedule(ws: Workspace, sched: Dict[str, Any], log: Optional[Log] = None,
                 on_stop: Optional[Callable[[Callable[[], None]], None]] = None) -> Dict[str, Any]:
    """Run one due change window to completion (or to its end). Caller holds the lock."""
    from . import schedule as _sched
    say = log or ws.log
    body = sched["body"]
    _sched.mark(ws.store, sched["id"], _sched.RUNNING)
    send = notifier(ws.cfg)
    send("schedule_started", "Change window #{} started: {}".format(sched["id"], sched["stage"]),
         "Runs until {} UTC.".format(sched["end_at"]))
    say("change window #{}: {} until {} UTC".format(sched["id"], sched["stage"], sched["end_at"]))
    if on_stop:
        on_stop(ws.engine.request_stop)
    try:
        totals = run_stage(ws.engine, sched["stage"], waves=body.get("waves") or None,
                           limit=int(body.get("limit") or 0), morefs=body.get("morefs") or None,
                           include_failed=bool(body.get("include_failed")),
                           folders=body.get("folders") or None, folder_exact=bool(body.get("folder_exact")),
                           rollback_failed=bool(body.get("rollback_failed")), log=say,
                           deadline=_sched.deadline(sched))
    except Exception as exc:
        _sched.mark(ws.store, sched["id"], _sched.FAILED, str(exc)[:500])
        send("schedule_finished", "Change window #{} failed".format(sched["id"]), str(exc)[:300])
        raise
    left = sum(n for _w, n in eligible_by_wave(ws.engine, sched["stage"], body.get("waves") or None,
                                              folders=body.get("folders") or None))
    if totals.get("hit_deadline") or (ws.engine.stopping and left):
        state, msg = _sched.STOPPED, "window closed with {} VM(s) still to go".format(left)
    else:
        state, msg = _sched.DONE, "applied {} · succeeded {} · failed {}".format(
            totals["applied"], totals["succeeded"], totals["failed"])
    _sched.mark(ws.store, sched["id"], state, msg)
    send("schedule_finished", "Change window #{} {}".format(sched["id"], state), msg,
         {"applied": totals["applied"], "failed": totals["failed"]})
    return dict(totals, schedule_state=state, message=msg)


# ---------------------------------------------------------------- verification
def verify_rows(store: st.Store, morefs: Optional[Sequence[str]] = None, wave: Optional[int] = None,
                only_unverified: bool = False) -> List[Any]:
    rows = store.query_vms(states=[st.S_COMMITTED], morefs=morefs, wave=wave)
    if only_unverified:
        rows = [r for r in rows if not r["verify_state"]]
    return rows


def run_verification(ws: Workspace, rows: Sequence[Any], client: Any = None,
                     progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    """Check committed VMs, record every result, and notify on failures."""
    from . import verify as _verify
    results = _verify.verify_many(rows, ws.cfg, client, progress=progress)
    counts = {"ok": 0, "warn": 0, "fail": 0}
    failed = []
    names = {r["moref"]: r["vm_name"] for r in rows}
    for moref, verdict, checks in results:
        ws.store.record_verification(moref, verdict, checks)
        counts[verdict] = counts.get(verdict, 0) + 1
        if verdict == "fail":
            bad = [c for c in checks if c["status"] == "fail"]
            failed.append("{}: {}".format(names.get(moref, moref),
                                          "; ".join("{} {}".format(c["check"], c["detail"]) for c in bad)))
            ws.store.log_event("verification failed: " + failed[-1], "error", moref=moref)
    if failed:
        notifier(ws.cfg)("verify_failed", "{} VM(s) failed post-import verification".format(len(failed)),
                         "\n".join(failed[:10]), {"checked": len(results)})
    return {"checked": len(results), "counts": counts, "failures": failed[:50]}


# --------------------------------------------------------------------- estimates
def wave_estimates(store: st.Store, cfg: Config) -> Dict[int, Dict[str, Any]]:
    """Per wave: how long its remaining precheck and import would take."""
    from .estimate import estimate
    out: Dict[int, Dict[str, Any]] = {}
    pre: Dict[int, Dict[str, int]] = {}
    imp: Dict[int, Dict[str, int]] = {}
    for r in store.conn.execute("SELECT wave, namespace, state, COUNT(*) AS n FROM vms GROUP BY wave, namespace, state"):
        if r["state"] in (st.S_PENDING, st.S_PRECHECK_FAILED):
            pre.setdefault(r["wave"], {}).setdefault(r["namespace"], 0)
            pre[r["wave"]][r["namespace"]] += r["n"]
        if r["state"] in (st.S_PENDING, st.S_PRECHECK_FAILED, st.S_PRECHECK_PASSED, st.S_FAILED, st.S_ROLLED_BACK):
            imp.setdefault(r["wave"], {}).setdefault(r["namespace"], 0)
            imp[r["wave"]][r["namespace"]] += r["n"]
    for w in store.waves():
        out[w] = {"precheck": estimate(store, cfg, "precheck", pre.get(w, {})),
                  "import": estimate(store, cfg, "import", imp.get(w, {}))}
    return out



# ------------------------------------------------------------ cluster namespaces
# Namespaces the Supervisor runs for itself: never an import target.
SYSTEM_NAMESPACE_RE = re.compile(r"^(kube-|vmware-system-|svc-)|^(default|tkg-system)$")


def cluster_namespaces(kube: Kubectl) -> Dict[str, Any]:
    """Namespaces a mapping could target, to suggest while mapping.

    Asks the Supervisor for its namespaces (system ones left out). Where that
    is not allowed or not answered, falls back to the kubeconfig's contexts for
    the same Supervisor -- one per namespace this login may use. `complete`
    says whether the list came from the cluster itself.
    """
    out: Dict[str, Any] = {"namespaces": [], "complete": False, "notes": []}
    try:
        listed, why = kube.list_namespaces()
    except KubectlError as exc:
        listed, why = None, str(exc).splitlines()[-1][:200] if str(exc) else "kubectl failed"
    try:
        known, kwhy = kube.kubeconfig_namespaces()
    except KubectlError:
        known, kwhy = [], "kubeconfig not readable"
    names: Dict[str, str] = {n: "kubeconfig" for n in known}
    if listed is not None:
        out["complete"] = True
        for n in listed:
            if not SYSTEM_NAMESPACE_RE.search(n):
                names[n] = "both" if n in names else "cluster"
    else:
        source = ("the {} namespace(s) your kubeconfig has contexts for".format(len(known)) if known
                  else "none ({}): log in with `kubectl vsphere login` to add a context per namespace, "
                       "or type the name".format(kwhy or "no contexts for this Supervisor"))
        out["notes"].append({
            "forbidden": "This login may not list every namespace on the Supervisor; showing " + source + ".",
            "unanswered": "The Supervisor did not answer; showing " + source + ".",
        }.get(why, "Could not list namespaces ({}); showing {}.".format(why, source)))
    out["namespaces"] = [{"name": n, "source": src} for n, src in sorted(names.items())]
    return out
