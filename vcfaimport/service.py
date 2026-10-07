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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .discovery import SelectionError, match_folder_map, _match_mapping  # noqa: F401  (re-exported for web)
from .engine import STAGE_IMPORT, SETTLED_STATES, AbortRun, Engine, cleanup_candidates, cleanup_plan, wave_finished
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


def run_members(store: st.Store, morefs: Sequence[str]) -> Dict[str, Any]:
    """Who a precheck or import is about to touch, and which of them are left over.

    "Left over" means discovered but no longer selected: staged in an earlier
    session, then deselected. Deselecting never takes a VM out of the queue, so
    without this a three-day-old entry for a since-repurposed VM rode along
    unnoticed (2026-10-01). VMs loaded from a CSV have no selection to compare.
    """
    vms = {r["moref"]: r for r in store.query_vms(morefs=list(morefs))} if morefs else {}
    disc = {r["moref"]: r for r in store.query_discovered(morefs=list(morefs))} if morefs else {}
    members, left_over = [], []
    for moref in morefs:
        vm = vms.get(moref)
        if vm is None:
            continue
        d = disc.get(moref)
        item = {"moref": moref, "vm_name": vm["vm_name"], "wave": vm["wave"],
                "queued_at": vm["created_at"], "selected": bool(d["selected"]) if d else None}
        members.append(item)
        if d is not None and not d["selected"]:
            left_over.append(item)
    return {"members": members, "left_over": left_over}


def left_over_lines(left_over: Sequence[Dict[str, Any]], limit: int = 10) -> List[str]:
    if not left_over:
        return []
    lines = ["  {} VM(s) here are not in your current selection -- queued earlier:".format(len(left_over))]
    lines += ["    {}  {}  wave {}  queued {}".format(m["vm_name"], m["moref"], m["wave"], m["queued_at"][:10])
              for m in left_over[:limit]]
    if len(left_over) > limit:
        lines.append("    ... and {} more".format(len(left_over) - limit))
    lines.append("  deselecting does not unqueue a VM: `skip --vm MOREF` keeps one out")
    return lines


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


def commit_summary(result: Dict[str, Any]) -> str:
    """One line for Engine.commit's result, the same in the CLI and the console."""
    parts = ["released {} batch(es) covering {} VM(s)".format(result["batches"], result["vms"]),
             "{} committed".format(result["committed"])]
    if result["already_committed"]:
        parts.append("{} of them already committed by the operator".format(result["already_committed"]))
    if result["failed"]:
        parts.append("{} FAILED".format(result["failed"]))
    if result["pending"]:
        parts.append("{} batch(es) not confirmed yet -- refresh to follow up".format(len(result["pending"])))
    return "; ".join(parts)


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


# ------------------------------------------------------------ staging waves
# Where VMs entering the queue go. "maps": the wave the maps (or the default wave)
# give them -- as staging always did. "next": one past the highest wave in the
# queue, so each staging pass of a running campaign opens the next wave. "wave":
# a wave named by number. "auto" is "maps" for the first staging of a campaign
# (an empty queue: the maps lay out the waves) and "next" after that.
WAVE_MODES = ("auto", "next", "maps", "wave")


def next_wave(store: st.Store) -> int:
    waves = store.waves()
    return (max(waves) + 1) if waves else 1


def place_new_vms(store: st.Store, cfg: Config, rows: Sequence[sqlite3.Row], records: Sequence[Any],
                  mode: str = "auto", wave: Optional[int] = None) -> Dict[str, Any]:
    """Settle the wave of every staged record, in place. Returns what was decided.

    Under "next" and "wave", only VMs entering the queue are placed: a VM already
    queued keeps its wave (re-staging still refreshes its namespace and subnets;
    moving it is the Waves page's job), a wave set on the VM in Select still wins,
    and with app_together a new VM joins its application's wave if the app is
    already queued. "maps" leaves stage()'s waves exactly as they were.
    """
    mode = (mode or "auto").lower()
    if mode not in WAVE_MODES:
        raise SelectionError("wave choice must be one of: {}".format(", ".join(WAVE_MODES)))
    queued = {r["moref"]: (r["wave"], r["app"]) for r in store.conn.execute("SELECT moref, wave, app FROM vms")}
    nxt = (max(w for w, _ in queued.values()) + 1) if queued else 1
    resolved = mode if mode != "auto" else ("next" if queued else "maps")
    out: Dict[str, Any] = {"mode": resolved, "asked": mode, "next_wave": nxt, "wave": None,
                           "placed": 0, "kept": 0, "explicit": 0, "with_app": 0, "maps_overridden": 0}
    if resolved == "maps":
        return out
    if resolved == "wave":
        if not wave or int(wave) < 1:
            raise SelectionError("a wave number of 1 or higher is needed")
        target = int(wave)
    else:
        target = nxt
    out["wave"] = target
    explicit = {r["moref"]: int(r["wave"] or 0) for r in rows}
    app_wave: Dict[str, int] = {}
    if getattr(cfg, "app_together", False):
        for w, app in queued.values():
            if app:
                app_wave[app] = min(app_wave.get(app, w), w)
    for rec in records:
        if rec.moref in queued:
            rec.wave = queued[rec.moref][0]
            out["kept"] += 1
        elif explicit.get(rec.moref):
            rec.wave = explicit[rec.moref]
            out["explicit"] += 1
        elif rec.app and rec.app in app_wave:
            rec.wave = app_wave[rec.app]
            out["with_app"] += 1
        else:
            if rec.wave != target and rec.wave != 1:
                out["maps_overridden"] += 1      # the maps named a wave; this choice beats it
            rec.wave = target
            out["placed"] += 1
    return out


def wave_choice_lines(choice: Dict[str, Any]) -> List[str]:
    """What place_new_vms decided, for the CLI and the console log."""
    if choice["mode"] == "maps":
        return ["  waves: as the maps say{}".format(" (first staging of the campaign)" if choice["asked"] == "auto" else "")]
    if choice["placed"]:
        lines = ["  waves: {} new VM(s) into wave {}{}".format(
            choice["placed"], choice["wave"], " (the next wave)" if choice["mode"] == "next" else "")]
    else:
        lines = ["  waves: nothing new to place; wave {} is where new VMs would go".format(choice["wave"])]
    if choice["maps_overridden"]:
        lines.append("    the maps named another wave for {} of them; choose \"As the maps say\" "
                     "(--wave maps) to follow the maps".format(choice["maps_overridden"]))
    if choice["kept"]:
        lines.append("    {} already queued keep their wave (move them on the Waves page)".format(choice["kept"]))
    if choice["explicit"]:
        lines.append("    {} keep the wave set on them in Select".format(choice["explicit"]))
    if choice["with_app"]:
        lines.append("    {} join their application's wave".format(choice["with_app"]))
    return lines


def wave_summaries(store: st.Store) -> List[Dict[str, Any]]:
    """Every wave with VMs in it: its name and notes, and the top-level folders its VMs come from.

    Clients usually keep one top-level folder per tenant, so when every VM of a wave
    shares one, it is offered as the wave's name ("suggested") until one is given.
    """
    info = store.wave_info()
    folders: Dict[int, Dict[str, int]] = {}
    sizes: Dict[int, int] = {}
    for r in store.conn.execute("SELECT wave, src_folder, COUNT(*) AS n FROM vms GROUP BY wave, src_folder"):
        sizes[r["wave"]] = sizes.get(r["wave"], 0) + r["n"]
        top = (r["src_folder"] or "").strip("/").split("/")[0]
        f = folders.setdefault(r["wave"], {})
        f[top] = f.get(top, 0) + r["n"]
    out = []
    for wave in store.waves():
        i = info.get(wave) or {}
        tops = folders.get(wave, {})
        named = sorted((k for k in tops if k), key=lambda k: (-tops[k], k))
        out.append({
            "wave": wave, "name": i.get("name", ""), "notes": i.get("notes", ""),
            "updated_at": i.get("updated_at"), "updated_by": i.get("updated_by"),
            "vms": sizes.get(wave, 0), "top_folders": [{"folder": k, "vms": tops[k]} for k in named],
            "suggested_name": named[0] if len(named) == 1 and not tops.get("") else "",
        })
    return out


def wave_cleanup(store: st.Store) -> List[Dict[str, Any]]:
    """Per wave: is it finished, what is still open in it, and how many of its batch
    objects are on the cluster and could be cleaned up now."""
    info = store.wave_info()
    plan: Dict[int, int] = {}
    for b in cleanup_plan(store):
        plan[b["wave"]] = plan.get(b["wave"], 0) + 1
    on_cluster: Dict[int, int] = {}
    for b in store.query_batches():
        if b["state"] != st.B_DELETED:
            on_cluster[b["wave"]] = on_cluster.get(b["wave"], 0) + 1
    out = []
    for wave in store.waves():
        counts = store.counts(wave=wave)
        out.append({
            "wave": wave, "name": (info.get(wave) or {}).get("name", ""),
            "vms": sum(counts.values()), "committed": counts.get(st.S_COMMITTED, 0),
            "finished": wave_finished(store, wave),
            "open": {s: n for s, n in counts.items() if s not in SETTLED_STATES},
            "on_cluster": on_cluster.get(wave, 0), "cleanable": plan.get(wave, 0),
        })
    return out


# ------------------------------------------------------------------ reset
class ResetRefused(ValueError):
    """The campaign cannot be reset now; nothing was changed."""


# The operator owns these VMs until they are committed or rolled back: forgetting
# them would strand the import on the cluster with nothing left to finish it.
RESET_BLOCKING_STATES = IN_FLIGHT_STATES + (st.S_AWAITING_COMMIT,)
LIVE_BATCH_STATES = (st.B_APPLIED, st.B_RUNNING, st.B_ROLLING_BACK)


def reset_preview(store: st.Store, cfg: Config) -> Dict[str, Any]:
    """What a reset would clear, keep and delete from the cluster, and what stops it."""
    from . import access, report, schedule as sch
    counts = store.counts()
    blocking = {s: counts[s] for s in RESET_BLOCKING_STATES if counts.get(s)}
    live = store.query_batches(states=list(LIVE_BATCH_STATES))
    blockers = []
    if blocking:
        blockers.append("{}: commit or roll them back first".format(", ".join(
            "{} {}".format(n, report.STATE_LABEL.get(s, s)) for s, n in blocking.items())))
    elif live:
        # Batches holding in-flight or gated VMs are already explained by the line above.
        blockers.append("{} still running on the cluster: wait for {} to finish, or roll {} back".format(
            _plural(len(live), "batch", "batches"), *(("it", "it") if len(live) == 1 else ("them", "them"))))
    q = store.conn.execute
    return {
        "vms": sum(counts.values()), "counts": counts, "committed": counts.get(st.S_COMMITTED, 0),
        "waves": len(store.waves()),
        "batches_on_cluster": [{"name": b["name"], "namespace": b["namespace"], "stage": b["stage"],
                                "state": b["state"]}
                               for b in store.query_batches() if b["state"] != st.B_DELETED],
        "discovered": store.discovered_counts()["total"],
        "events": q("SELECT COUNT(*) FROM events").fetchone()[0],
        "schedules_open": q("SELECT COUNT(*) FROM schedules WHERE state IN (?, ?)", sch.OPEN_STATES).fetchone()[0],
        "approvals_pending": q("SELECT COUNT(*) FROM approvals WHERE state=?", (access.PENDING,)).fetchone()[0],
        "blockers": blockers,
        "ledger_path": store.ledger_path,
        # A ledger configured elsewhere (a file share) is shared with others: never moved.
        "ledger_shared": bool(cfg.ledger_path),
        "archive_root": str(Path(cfg.workdir).expanduser().resolve() / "archive"),
    }


def _plural(n: int, one: str, many: str) -> str:
    return "{} {}".format(n, one if n == 1 else many)


def reset_campaign(ws: "Workspace", keep_discovered: bool = False,
                   keep_jobs: Sequence[str] = ()) -> Dict[str, Any]:
    """Archive this campaign and start the workspace afresh.

    Refused while any VM is in flight or held at the commit gate. Otherwise every
    batch object the campaign still has on the cluster is deleted first -- if one
    cannot be, nothing is reset -- then the database, the local ledger and the
    console's job history go to <workdir>/archive/campaign-<time>/, and the
    campaign tables are emptied. People, settings, maps and the config stay.
    Committed VMs stay in their namespaces: there is no way back to vCenter.
    The caller holds the WorkspaceLock.
    """
    import shutil
    store, cfg, log = ws.store, ws.cfg, ws.log
    plan = reset_preview(store, cfg)
    if plan["blockers"]:
        raise ResetRefused("cannot reset the campaign: " + "; ".join(plan["blockers"]))

    deleted, errors = [], []
    for b in plan["batches_on_cluster"]:
        try:
            ws.kube.delete(cfg.batch_resource, b["name"], b["namespace"])
        except KubectlError as exc:
            errors.append("{}/{}: {}".format(b["namespace"], b["name"], str(exc)[:300]))
            continue
        deleted.append(b["name"])
        log("  x  deleted {}/{}".format(b["namespace"], b["name"]))
    if errors:
        for err in errors:
            log("  ! " + err)
        raise ResetRefused("cannot reset the campaign: {} could not be deleted from the cluster "
                           "(see above); nothing was reset. Fix that and reset again."
                           .format(_plural(len(errors), "batch object", "batch objects")))

    work = Path(cfg.workdir).expanduser().resolve()
    stamp = time.strftime("campaign-%Y%m%d-%H%M%S")
    archive, n = Path(plan["archive_root"]) / stamp, 1
    while archive.exists():
        n += 1
        archive = Path(plan["archive_root"]) / "{}-{}".format(stamp, n)
    store.backup_to(str(archive / "state.db"))
    ledger = Path(store.ledger_path)
    if not plan["ledger_shared"] and ledger.is_file():
        shutil.move(str(ledger), str(archive / "ledger.jsonl"))
    jobs = work / "jobs"
    if jobs.is_dir():
        keep = set(keep_jobs)
        for f in list(jobs.iterdir()):
            if f.is_file() and f.stem not in keep:
                (archive / "jobs").mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(archive / "jobs" / f.name))
    summary = {
        "reset_at": time.strftime("%Y-%m-%d %H:%M:%S"), "by": store.actor,
        "vms": plan["vms"], "counts": plan["counts"], "waves": plan["waves"],
        "batches_deleted": deleted, "kept_discovered": keep_discovered,
        "ledger": str(archive / "ledger.jsonl") if (archive / "ledger.jsonl").exists() else plan["ledger_path"],
    }
    (archive / "reset.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    store.clear_campaign(keep_discovered=keep_discovered)
    msg = "campaign reset: {} VMs in {} archived to {}; {} deleted from the cluster".format(
        plan["vms"], _plural(plan["waves"], "wave", "waves"), archive,
        _plural(len(deleted), "batch object", "batch objects"))
    store.log_event(msg, "warn")
    if plan["ledger_shared"]:
        store.note_in_ledger("campaign_reset", msg, archive=str(archive))
    log("archived the campaign to {}".format(archive))
    return dict(summary, archive=str(archive), committed=plan["committed"])


def wave_label(wave: int, name: str = "") -> str:
    """'Wave 2', or 'Wave 2 · Acme' once it is named."""
    return "Wave {}{}".format(wave, " · " + name if name else "")


def overview(store: st.Store, cfg: Config) -> Dict[str, Any]:
    """Everything the dashboard shows, in a handful of queries."""
    counts = store.counts()
    total = sum(counts.values())
    by_wave = store.state_matrix("wave")
    names = store.wave_info()
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
        "waves": [{"wave": w, "total": sum(c.values()), "counts": c,
                   "name": names.get(w, {}).get("name", ""), "notes": names.get(w, {}).get("notes", "")}
                  for w, c in sorted(by_wave.items())],
        "namespaces": [{"namespace": n, "total": sum(c.values()), "counts": c}
                       for n, c in sorted(by_ns.items())],
        "live_batches": [dict(b) for b in live],
        "wave_cleanup": wave_cleanup(store),
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
    {"id": "vm_deleted", "pattern": r"deleted before the import completed|VMCRNotFoundPostCreation",
     "title": "The VM was deleted before its import completed",
     "advice": "Its VirtualMachine in the namespace was deleted mid-import, so the operator can never "
               "finish it. Before a commit, roll the batch back. After a commit the operator refuses "
               "that: abandon the batch (committed VMs stay), delete the leftover VM in vCenter, then "
               "skip it.",
     "actions": ["rollback", "abandon", "skip"]},
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
        "cleanup": [dict(b) for b in cleanup_candidates(store)],
        "awaiting_commit": [vm_summary(v) for v in store.query_vms(states=[st.S_AWAITING_COMMIT])],
    }


# ----------------------------------------------------------------- activity
# What is happening right now, and if it looks stuck, why: the Activity page's
# "Right now" panel. Signals come from the store and the running job only; nothing
# here calls the cluster, so it is cheap enough to refresh with every pulse.

# A running job with no log line, no VM state change and no event for this long is
# worth a look. Polling is silent until something changes, so this is a nudge, not a verdict.
QUIET_AFTER_S = 600
_LEVEL_ORDER = {"bad": 0, "warn": 1, "info": 2, "ok": 3}


def _ts_epoch(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _pid_alive(pid: int) -> Optional[bool]:
    """Whether a process exists; None if that cannot be told."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5             # access denied: it exists
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            k32.CloseHandle(handle)
            return bool(ok) and code.value == 259               # STILL_ACTIVE
        except (OSError, AttributeError):
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def other_lock_holder(cfg: Config) -> str:
    """Who holds this workspace from another live process (a CLI run), or "".

    The holder's text stays in the lock file after a crash; the OS has released the
    lock itself, so a dead holder blocks nothing and is not reported."""
    text = WorkspaceLock(cfg, "").holder()
    m = re.search(r"\(pid (\d+)", text or "")
    if not m or int(m.group(1)) == os.getpid():
        return ""
    return text if _pid_alive(int(m.group(1))) else ""


def activity_now(store: st.Store, cfg: Config, *, job: Optional[Dict[str, Any]] = None,
                 latest_job: Optional[Dict[str, Any]] = None, other_holder: str = "",
                 approvals_pending: int = 0, next_window: Optional[Dict[str, Any]] = None,
                 now: Optional[float] = None) -> Dict[str, Any]:
    """A verdict (idle / running / possibly stuck / needs attention), the signals behind
    it -- each with the evidence and what to do -- and the batches on the cluster."""
    now = now or time.time()
    signals: List[Dict[str, Any]] = []

    def add(level: str, key: str, title: str, detail: str = "", action: Optional[Dict[str, str]] = None) -> None:
        signals.append({"level": level, "key": key, "title": title, "detail": detail, "action": action})

    counts = store.counts()
    timeout_s = cfg.batch_timeout_minutes * 60
    in_flight = store.query_vms(states=list(IN_FLIGHT_STATES))
    # How long each batch has really been going: from its VMs' start stamps (a batch's
    # applied_at is rewritten as it is polled).
    started: Dict[Tuple[str, str], float] = {}
    members: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
    for v in in_flight:
        name = v["precheck_batch"] if v["state"] == st.S_PRECHECK_RUNNING else v["batch_name"]
        t = _ts_epoch(v["precheck_started_at"] if v["state"] == st.S_PRECHECK_RUNNING else v["import_started_at"])
        key = (v["namespace"], name or "")
        members.setdefault(key, []).append(v)
        if t is not None:
            started[key] = min(started.get(key, t), t)

    batches: List[Dict[str, Any]] = []
    issues: Dict[str, Dict[str, Any]] = {}
    for b in store.query_batches(states=[st.B_APPLIED, st.B_RUNNING, st.B_ROLLING_BACK]):
        key = (b["namespace"], b["name"])
        age = now - started[key] if key in started else None
        vm_msgs = [m["message"] or m["last_phase"] or "" for m in members.get(key, [])]
        issue = classify_failure(b["message"] or "") or next(
            (i for i in (classify_failure(x) for x in vm_msgs if x) if i), None)
        entry = {"name": b["name"], "namespace": b["namespace"], "stage": b["stage"], "state": b["state"],
                 "wave": b["wave"], "vms": b["vm_count"], "in_flight": len(members.get(key, [])),
                 "age_s": round(age) if age is not None else None, "timeout_s": timeout_s,
                 "message": b["message"] or next((x for x in vm_msgs if x), ""),
                 "issue": {"id": issue["id"], "title": issue["title"], "advice": issue["advice"]} if issue else None}
        batches.append(entry)
        if issue:
            issues.setdefault(issue["id"], {"issue": issue, "batches": []})["batches"].append(b["name"])
        elif age is not None and age > timeout_s / 2 and members.get(key):
            add("warn", "long:" + b["name"], "{} has been running {:.0f} min (it times out at {} min)".format(
                b["name"], age / 60, cfg.batch_timeout_minutes),
                "The operator says: {}".format(entry["message"]) if entry["message"] else
                "The operator has not reported a reason. Open the batch to see its conditions.",
                {"label": "Open batch", "batch": b["name"], "ns": b["namespace"]})

    for found in issues.values():
        i, names = found["issue"], found["batches"]
        add("bad", "issue:" + i["id"], "{} ({})".format(i["title"], plural_s(len(names), "batch", "batches")),
            "{} Seen in: {}.".format(i["advice"], ", ".join(names[:4]) + (" …" if len(names) > 4 else "")),
            {"label": "Open batch", "batch": names[0], "ns": next(b["namespace"] for b in batches if b["name"] == names[0])})

    # In flight inside a batch that timed out: nobody polls them any more.
    timed_out = {(b["namespace"], b["name"]) for b in store.query_batches(states=[st.B_TIMEDOUT])}
    stalled = [v for v in in_flight if (v["namespace"], v["batch_name"]) in timed_out
               or (v["namespace"], v["precheck_batch"]) in timed_out]
    if stalled:
        add("bad", "stalled", "{} in a batch that timed out, and nothing polls them".format(plural_s(len(stalled), "VM")),
            "The batch passed batch_timeout_minutes ({} min). Refresh to pick up a late result, or roll back.".format(
                cfg.batch_timeout_minutes), {"label": "Open Triage", "go": "triage"})

    running = job is not None and job.get("status") == "running"
    if batches and not running and not other_holder:
        add("warn", "unwatched", "{} on the cluster and nothing is polling {}".format(
            plural_s(len(batches), "batch", "batches"), "it" if len(batches) == 1 else "them"),
            "A run that ended early (stopped, or the console restarted) leaves batches behind; their results "
            "are only recorded when something polls them.", {"label": "Watch until done", "act": "watch"})

    quiet_s = None
    if running:
        last = max(filter(None, [_ts_epoch(job.get("last_log_at")), _ts_epoch(job.get("started_at")),
                                 _ts_epoch(_last_ts(store, "transitions")),
                                 _ts_epoch(_last_ts(store, "events"))]), default=None)
        quiet_s = now - last if last else None
        if quiet_s is not None and quiet_s > max(QUIET_AFTER_S, 30 * cfg.poll_interval_seconds):
            add("warn", "quiet", "No progress for {:.0f} min".format(quiet_s / 60),
                "No log line, no VM state change and no event since then. The last line was: “{}”. "
                "Batches are still polled every {}s; check them above for the operator's reason.".format(
                    job.get("last_line") or "(none)", cfg.poll_interval_seconds),
                {"label": "Open log", "job": job["id"]})
    if other_holder:
        add("info", "holder", "A run outside this console is changing the workspace",
            other_holder + ". Its log is in the terminal that started it; its VM state changes still appear in the timeline.")
    if counts.get(st.S_AWAITING_COMMIT):
        add("info", "commit", "{} held at the commit gate".format(plural_s(counts[st.S_AWAITING_COMMIT], "VM")),
            "commitAction Wait: nothing moves until someone commits them, or rolls them back.",
            {"label": "Go to Execute", "go": "execute"})
    if approvals_pending:
        add("info", "approvals", "{} waiting for approval".format(plural_s(approvals_pending, "request")),
            "Nothing runs until someone other than the requester approves it.", {"label": "Change control", "go": "schedule"})
    if next_window and next_window.get("state") in ("scheduled", "awaiting_approval"):
        add("info", "window", "Next change window: {} {}".format(next_window.get("stage", ""), next_window.get("start_at", "")),
            "It starts on its own at that time.", {"label": "Change control", "go": "schedule"})
    failed = sum(counts.get(s, 0) for s in FAILED_STATES)
    if failed:
        groups = failure_groups(store)["groups"]
        top = groups[0] if groups else None
        cause = (top["issue"]["title"] if top and top["issue"] else (top["message"] if top else "")) or "see Triage"
        add("warn", "failed", "{} failed".format(plural_s(failed, "VM")),
            "Most common cause: {}.".format(cause[:200]), {"label": "Open Triage", "go": "triage"})
    if not running and latest_job and latest_job.get("status") == "failed":
        add("bad", "lastjob", "The last job failed: {}".format(latest_job.get("title", "")),
            latest_job.get("error") or "", {"label": "Open log", "job": latest_job["id"]})

    signals.sort(key=lambda s: _LEVEL_ORDER[s["level"]])
    worst = signals[0]["level"] if signals else "ok"
    if worst == "bad":
        verdict = {"level": "bad", "title": "Needs attention", "detail": signals[0]["title"]}
    elif running and any(s["key"] == "quiet" for s in signals):
        verdict = {"level": "warn", "title": "Possibly stuck", "detail": "{} has been quiet for {:.0f} min".format(
            job.get("title", "The job"), (quiet_s or 0) / 60)}
    elif running:
        verdict = {"level": "busy", "title": "Running: " + job.get("title", ""),
                   "detail": "{} on the cluster".format(plural_s(len(batches), "batch", "batches")) if batches else "working"}
    elif worst == "warn":
        verdict = {"level": "warn", "title": "Needs a look", "detail": signals[0]["title"]}
    elif counts.get(st.S_AWAITING_COMMIT) or approvals_pending:
        verdict = {"level": "info", "title": "Waiting on you", "detail": signals[0]["title"] if signals else ""}
    else:
        verdict = {"level": "ok", "title": "Idle", "detail": "Nothing running." + (
            " Last: {} ({})".format(latest_job.get("title", ""), latest_job.get("status", "")) if latest_job else "")}
    return {"verdict": verdict, "signals": signals, "batches": batches, "quiet_s": round(quiet_s) if quiet_s else None}


def plural_s(count: int, word: str, many: str = "") -> str:
    return "{} {}".format(count, word if count == 1 else (many or word + "s"))


def _last_ts(store: st.Store, table: str) -> Optional[str]:
    """The newest timestamp in events or transitions (both keep ts in UTC ISO form)."""
    assert table in ("events", "transitions")
    row = store.conn.execute("SELECT MAX(ts) AS t FROM {}".format(table)).fetchone()
    return row["t"] if row else None


_CHANGE_LEVEL = {st.S_FAILED: "bad", st.S_PRECHECK_FAILED: "bad", st.S_ROLLED_BACK: "warn",
                 st.S_COMMITTED: "ok", st.S_PRECHECK_PASSED: "ok", st.S_AWAITING_COMMIT: "info"}
_JOB_LEVEL = {"failed": "bad", "warning": "warn", "stopped": "warn", "succeeded": "ok"}


def activity_timeline(store: st.Store, *, jobs: Sequence[Dict[str, Any]] = (), since: Optional[str] = None,
                      until: Optional[str] = None, limit: int = 300) -> List[Dict[str, Any]]:
    """Jobs, events and VM state changes as one stream, newest first.

    State changes made together (same from -> to, stage, batch and actor, within a
    minute) collapse into one entry listing the VMs, so a batch of twelve reads as one
    line, not twelve."""
    def inside(ts: Optional[str]) -> bool:
        return bool(ts) and (not since or ts >= since) and (not until or ts <= until)

    out: List[Dict[str, Any]] = []
    ns_of = {b["name"]: b["namespace"] for b in store.query_batches()}     # to open a batch from an entry
    for j in jobs:
        if inside(j.get("started_at")):
            out.append({"ts": j["started_at"], "kind": "job", "level": "info", "job": j["id"],
                        "title": "Started: " + j.get("title", ""), "actor": j.get("user") or ""})
        if j.get("finished_at") and inside(j["finished_at"]):
            out.append({"ts": j["finished_at"], "kind": "job", "level": _JOB_LEVEL.get(j.get("status"), "info"),
                        "job": j["id"], "title": "{} {}".format(j.get("title", ""), {
                            "succeeded": "finished", "warning": "finished with warnings"}.get(j.get("status"), j.get("status"))),
                        "detail": j.get("error") or "", "actor": j.get("user") or ""})
    changes = store.transitions(limit=limit * 20, since=since, until=until, newest_first=True)
    # An event that only repeats a VM's state change (same VM, same message) adds nothing.
    said = {(t["moref"], (t["message"] or "").strip()) for t in changes if t["message"]}
    for e in store.recent_events(limit=limit * 2, since=since, until=until):
        if e["moref"] and (e["moref"], (e["message"] or "").strip()) in said:
            continue
        out.append({"ts": e["ts"], "kind": "event", "level": {"error": "bad", "warn": "warn"}.get(e["level"], "info"),
                    "title": e["message"], "moref": e["moref"], "batch": e["batch"],
                    "ns": ns_of.get(e["batch"] or "", ""), "actor": e["actor"] or ""})
    group: Optional[Dict[str, Any]] = None
    for t in changes:
        key = (t["from_state"], t["to_state"], t["stage"], t["batch"], t["actor"])
        t_epoch = _ts_epoch(t["ts"]) or 0
        if group is not None and group["_key"] == key and group["_oldest"] - t_epoch <= 60:
            group["count"] += 1
            group["_oldest"] = t_epoch
            if len(group["vms"]) < 50:
                group["vms"].append({"moref": t["moref"], "name": t["vm_name"]})
            if (t["message"] or "") != group["message"]:
                group["mixed"] = True
            continue
        group = {"ts": t["ts"], "kind": "change", "level": _CHANGE_LEVEL.get(t["to_state"], "info"),
                 "from": t["from_state"], "to": t["to_state"], "stage": t["stage"] or "", "batch": t["batch"],
                 "ns": ns_of.get(t["batch"] or "", t["namespace"] or ""),
                 "actor": t["actor"] or "", "count": 1, "vms": [{"moref": t["moref"], "name": t["vm_name"]}],
                 "message": t["message"] or "", "mixed": False, "_key": key, "_oldest": t_epoch}
        out.append(group)
    for e in out:
        e.pop("_key", None)
        e.pop("_oldest", None)
    out.sort(key=lambda e: e["ts"] or "", reverse=True)
    return out[:limit]


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


# ------------------------------------------------------------ vCenter portgroups
def record_discovery(store: st.Store, server: str, extras: Optional[Dict[str, Any]]) -> None:
    """What a discovery pass learned besides the VMs: vCenter's network list."""
    if extras and extras.get("networks") is not None:
        store.set_meta("vc_networks", {"server": server, "networks": extras["networks"]})


# ------------------------------------------------------------ rediscovery
# A pass that would remove more than half of what was discovered (and at least this many
# VMs) is held: an account that lost sight of part of the inventory is far more likely
# than an estate halved overnight. allow_mass_removal applies it anyway.
MASS_REMOVAL_MIN = 20


def _server_key(server: str) -> str:
    s = (server or "").strip().lower()
    for prefix in ("https://", "http://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    return s.rstrip("/")


def reconcile_discovery(store: st.Store, client: Any, vms: Sequence[Any], server: str, *,
                        power_state: Optional[str] = None, concurrency: int = 12,
                        allow_mass_removal: bool = False, log: Optional[Log] = None) -> Dict[str, Any]:
    """After a discovery pass: the VMs discovered before that this pass did not return.

    Each is looked up in vCenter by moref; only a 404 counts as gone, and no answer
    keeps the VM. A gone VM leaves the inventory cache. If it was queued and is still
    with vCenter (not yet handed to the importer, or back), it becomes skipped, with a
    transition saying why. VMs in flight or committed are never touched, only reported.

    Not checked: a pass against a different vCenter than the last one (a moref is only
    unique within one vCenter), and VMs a powered-on-only pass filtered out. Call it
    before meta "vcenter" is set to this server.
    """
    say = log or (lambda msg: None)
    out: Dict[str, Any] = {"checked": 0, "removed": [], "skipped": [], "untouched": [],
                           "unconfirmed": 0, "outside_filter": 0, "held": 0, "note": ""}
    previous = str(store.get_meta("vcenter") or "")
    if previous and _server_key(previous) != _server_key(server):
        out["note"] = ("this pass read {} but the inventory came from {}: previously discovered "
                       "VMs were not checked for removal".format(server, previous))
        return out
    seen = {(v.moref if hasattr(v, "moref") else v["moref"]) for v in vms}
    known = store.query_discovered()
    missing = [r for r in known if r["moref"] not in seen]
    if power_state:
        kept_out = {r["moref"] for r in missing if (r["power_state"] or "").upper() != power_state.upper()}
        out["outside_filter"] = len(kept_out)
        missing = [r for r in missing if r["moref"] not in kept_out]
    if not missing:
        return out
    say("checking {} previously discovered VM(s) this pass did not return...".format(len(missing)))
    workers = max(1, min(int(concurrency or 1), 32))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        answers = list(pool.map(lambda r: client.vm_exists(r["moref"]), missing))
    out["checked"] = len(missing)
    out["unconfirmed"] = sum(1 for a in answers if a is None)
    gone = [r for r, a in zip(missing, answers) if a is False]
    if gone and not allow_mass_removal and len(gone) >= MASS_REMOVAL_MIN and len(gone) * 2 > len(known):
        out["held"] = len(gone)
        out["note"] = ("vCenter reports {} of the {} discovered VMs gone, so nothing was removed: that usually "
                       "means the account no longer sees part of the inventory. Check its permissions, or "
                       "discover again allowing mass removal if they really are gone.".format(len(gone), len(known)))
        store.log_event(out["note"], "warn")
        return out
    why = "no longer in vCenter {} (deleted, unregistered or moved to another vCenter)".format(server)
    for r in gone:
        entry = {"moref": r["moref"], "name": r["name"], "folder": r["folder"]}
        vm = store.get_vm(r["moref"])
        if vm is not None and vm["state"] not in st.PROVENANCE_REFRESH_STATES:
            # In flight or committed: what the importer did is not undone by an inventory pass.
            entry["state"] = vm["state"]
            out["untouched"].append(entry)
            store.log_event("{} ({}) is {} but is {}: left as is".format(r["name"], r["moref"], why, vm["state"]),
                            "warn", moref=r["moref"])
            continue
        if vm is not None and vm["state"] != st.S_SKIPPED:
            entry["was"] = vm["state"]
            store.set_vm_state(r["moref"], st.S_SKIPPED, message=why)
            out["skipped"].append(entry)
        store.remove_discovered([r["moref"]])
        store.log_event("{} ({}) is {}: removed from discovery{}".format(
            r["name"], r["moref"], why, "; its queued record is now skipped" if "was" in entry else ""),
            "warn", moref=r["moref"])
        out["removed"].append(entry)
    return out


def discovery_report(summary: Dict[str, Any], rec: Dict[str, Any], limit: int = 20) -> List[str]:
    """The lines a discovery pass ends with, the same for the CLI and the console log."""
    lines: List[str] = []

    def listing(title: str, entries: List[Dict[str, Any]], fmt: Callable[[Dict[str, Any]], str]) -> None:
        if not entries:
            return
        lines.append("  " + title)
        lines.extend("    - " + fmt(e) for e in entries[:limit])
        if len(entries) > limit:
            lines.append("    ... and {} more".format(len(entries) - limit))

    listing("{} VM(s) renamed in vCenter:".format(len(summary.get("renamed") or [])), summary.get("renamed") or [],
            lambda e: "{} -> {} ({})".format(e["from"], e["to"], e["moref"]))
    listing("{} VM(s) moved to another folder:".format(len(summary.get("moved") or [])), summary.get("moved") or [],
            lambda e: "{}: {} -> {}".format(e["name"], e["from"] or "(root)", e["to"] or "(root)"))
    listing("{} VM(s) no longer in vCenter, removed from discovery:".format(len(rec.get("removed") or [])),
            rec.get("removed") or [],
            lambda e: "{} ({}){}".format(e["name"], e["moref"], " - was queued ({}), now skipped".format(e["was"]) if e.get("was") else ""))
    listing("{} VM(s) no longer in vCenter but in flight or committed, left as is:".format(len(rec.get("untouched") or [])),
            rec.get("untouched") or [], lambda e: "{} ({}) - {}".format(e["name"], e["moref"], e["state"]))
    if rec.get("unconfirmed"):
        lines.append("  {} VM(s) not returned by this pass could not be confirmed (vCenter did not answer for them): "
                     "kept".format(rec["unconfirmed"]))
    if rec.get("outside_filter"):
        lines.append("  {} VM(s) outside this pass's power filter were not checked; a full discovery checks "
                     "them".format(rec["outside_filter"]))
    if rec.get("note"):
        lines.append("  " + rec["note"])
    if summary.get("drifted"):
        lines.append("  {} queued VM(s) changed in vCenter since their facts were recorded "
                     "(renamed, moved, power or Tools); see `events`".format(summary["drifted"]))
    if summary.get("precheck_cleared"):
        lines.append("  {} of them had passed precheck: back to pending, precheck them again"
                     .format(summary["precheck_cleared"]))
    return lines


def vcenter_portgroups(store: st.Store) -> Dict[str, Any]:
    """Portgroups to suggest while mapping: every network vCenter listed at the
    last discovery, plus any a discovered VM uses; with how many (selected) VMs
    sit on each. No vCenter call -- that would need the password again."""
    kept = store.get_meta("vc_networks") or {}
    types: Dict[str, str] = {n["name"]: n.get("type", "") for n in (kept.get("networks") or []) if n.get("name")}
    vms: Dict[str, int] = {}
    selected: Dict[str, int] = {}
    for r in store.query_discovered():
        for net in {n.strip() for n in (r["networks"] or "").split(",") if n.strip()}:
            vms[net] = vms.get(net, 0) + 1
            if r["selected"]:
                selected[net] = selected.get(net, 0) + 1
    names = sorted(set(types) | set(vms), key=str.lower)
    return {
        "portgroups": [{"name": n, "type": types.get(n, ""), "vms": vms.get(n, 0), "selected": selected.get(n, 0)}
                       for n in names],
        "complete": bool(kept),
        "discovered_at": store.get_meta("discovered_at"),
        "notes": [] if kept else (["Only the portgroups your discovered VMs use: discover again to list every "
                                   "portgroup in vCenter."] if names else ["Nothing discovered yet: run Discover first."]),
    }


# ------------------------------------------------------------ Supervisor subnets
# Kinds a VM's subnetInfo can name (see config subnet_kind / subnet_api_group).
SUBNET_RESOURCES = (("subnets.crd.nsx.vmware.com", "Subnet"), ("subnetsets.crd.nsx.vmware.com", "SubnetSet"))


def cluster_subnets(kube: Kubectl, namespaces: Sequence[str] = ()) -> Dict[str, Any]:
    """Subnets and SubnetSets to suggest while mapping, with the namespace each
    lives in (a VPC namespace's subnets live in the VPC's own namespace) and
    its display name. Lists cluster-wide when allowed; otherwise namespace by
    namespace for the ones given. `complete` is False when some could not be read."""
    out: Dict[str, Any] = {"subnets": [], "complete": True, "notes": []}
    found: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    def take(listing: Dict[str, Any], kind: str, group: str) -> None:
        for item in (listing or {}).get("items") or []:
            meta = item.get("metadata") or {}
            name, ns = meta.get("name", ""), meta.get("namespace", "")
            if not name:
                continue
            display = ((meta.get("annotations") or {}).get("vmware-system-display-name")
                       or (meta.get("labels") or {}).get("vmware-system-display-name") or "")
            found[(ns, kind, name)] = {"name": name, "namespace": ns, "kind": item.get("kind") or kind,
                                       "api_group": group, "display": display if display != name else ""}

    for resource, kind in SUBNET_RESOURCES:
        group = resource.split(".", 1)[1]
        try:
            proc = kube.run(["get", resource, "-A", "-o", "json"], check=False, timeout=25, retries=0)
        except KubectlError as exc:
            out["complete"] = False
            out["notes"].append("Could not list {}s: {}".format(kind, str(exc).splitlines()[-1][:160] if str(exc) else "kubectl failed"))
            break
        if proc.returncode == 0:
            try:
                take(json.loads(proc.stdout or "{}"), kind, group)
            except ValueError:
                out["complete"] = False
            continue
        err = (proc.stderr or "").strip()
        if "doesn't have a resource type" in err or "the server doesn't have" in err:
            continue                       # this kind does not exist on this cluster
        # Not allowed (or not answered) cluster-wide: read the namespaces we know instead.
        unreadable = []
        for ns in list(dict.fromkeys(n for n in namespaces if n))[:25]:
            p2 = kube.run(["get", resource, "-n", ns, "-o", "json"], check=False, timeout=20, retries=0)
            if p2.returncode == 0:
                try:
                    take(json.loads(p2.stdout or "{}"), kind, group)
                except ValueError:
                    unreadable.append(ns)
            elif "doesn't have a resource type" not in (p2.stderr or ""):
                unreadable.append(ns)
        out["complete"] = False
        why = "not allowed" if "forbidden" in err.lower() else "no answer" if TRANSIENT_HINT.search(err) else "failed"
        note = "Listing {}s across the Supervisor {}; read namespace by namespace ({})".format(
            kind, why, ", ".join(sorted(set(namespaces))) or "none known")
        if unreadable:
            note += "; could not read: " + ", ".join(unreadable)
        out["notes"].append(note + ". A VPC's subnets live in the VPC's own namespace, which may be missing here.")
    out["subnets"] = sorted(found.values(), key=lambda s_: (s_["name"].lower(), s_["namespace"]))
    return out


TRANSIENT_HINT = re.compile(r"i/o timeout|connection refused|unable to connect|deadline exceeded", re.I)
