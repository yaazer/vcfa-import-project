"""The web console's JSON API.

Handlers are thin: they parse a request, call the same engine / store /
service functions the CLI calls, and return plain data. Anything that touches
vCenter or the cluster runs as a background job (see jobs.py); anything that
only reads or edits the local state store runs inline.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import __version__
from .. import report
from .. import service
from .. import state as st
from ..config import Config
from ..discovery import (
    FOLDER_MAP_COLUMNS,
    NETWORK_MAP_COLUMNS,
    SelectionError,
    folder_map_from_rows,
    network_map_from_rows,
    read_folder_map_rows,
    read_network_map_rows,
    stage,
    write_map_rows,
)
from ..engine import STAGE_IMPORT, STAGE_PRECHECK, Engine
from ..kube import Kubectl
from ..vcenter import ENV_PASSWORD, ENV_SERVER, ENV_USER, VCenterClient, VCenterError, discover
from .jobs import FAILED, STOPPED, WARNING, Job, JobManager


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _row(r) -> Dict[str, Any]:
    return dict(r) if r is not None else None


def _list(value, cast=str) -> Optional[List[Any]]:
    """Accept a list, a comma-separated string, or nothing."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        value = [v for v in value.split(",") if v.strip()]
    try:
        return [cast(v) for v in value]
    except (TypeError, ValueError):
        raise ApiError(400, "expected a list of {} values, got {!r}".format(cast.__name__, value))


def _int(value, default: Optional[int] = None, minimum: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "expected a number, got {!r}".format(value))
    if minimum is not None and out < minimum:
        raise ApiError(400, "{} is below the minimum of {}".format(out, minimum))
    return out


def _stage(value) -> str:
    if value not in (STAGE_PRECHECK, STAGE_IMPORT):
        raise ApiError(400, "stage must be 'precheck' or 'import'")
    return value


def _require_confirm(body: Dict[str, Any]) -> None:
    # The browser asks the human; this stops a stray script from skipping that.
    if body.get("confirm") is not True:
        raise ApiError(400, "this action changes the cluster; resend with \"confirm\": true")


class WebApp:
    def __init__(self, cfg: Config, *, config_path: Optional[str] = None,
                 folder_map: Optional[str] = None, network_map: Optional[str] = None,
                 verbose: bool = False):
        self.cfg = cfg
        self.config_path = config_path
        base = Path(config_path).expanduser().resolve().parent if config_path else Path.cwd()
        self.folder_map_path = str(Path(folder_map).expanduser() if folder_map
                                   else base / "folder-map.csv")
        self.network_map_path = str(Path(network_map).expanduser() if network_map
                                    else base / "portgroup-map.csv")
        self.verbose = verbose
        # One shared connection for request threads, serialised by a lock.
        # Jobs open their own (a connection is not shared across a long run).
        self._ws = service.Workspace(copy.deepcopy(cfg), check_same_thread=False)
        self.store = self._ws.store
        self.lock = threading.RLock()
        self.jobs = JobManager(Path(cfg.workdir).expanduser() / "jobs")

    def close(self) -> None:
        self._ws.close()

    def _engine(self, cfg: Optional[Config] = None) -> Engine:
        """An engine over the shared store, for DB-only operations (planning, previews)."""
        c = cfg or self.cfg
        return Engine(c, self.store, Kubectl(c), lambda msg: None)

    def _lock_for(self, what: str) -> service.WorkspaceLock:
        return service.WorkspaceLock(self.cfg, "{} from the web console".format(what))

    def _job_workspace(self, job: Job, cfg: Optional[Config] = None,
                       dry_run: bool = False) -> service.Workspace:
        return service.Workspace(copy.deepcopy(cfg or self.cfg), dry_run=dry_run,
                                 verbose=self.verbose, log=job.log)

    # ================================================================ info
    def info(self, q, body) -> Dict[str, Any]:
        c = self.cfg
        with self.lock:
            vcenter_meta = self.store.get_meta("vcenter")
        return {
            "version": __version__,
            "config_path": self.config_path,
            "workdir": str(Path(c.workdir).expanduser().resolve()),
            "context": c.context,
            "api_version": c.api_version,
            "settings": {
                "commit_action": c.commit_action, "mode": c.mode,
                "batch_size": c.batch_size, "max_parallel_batches": c.max_parallel_batches,
                "max_parallel_batches_per_namespace": c.max_parallel_batches_per_namespace,
                "poll_interval_seconds": c.poll_interval_seconds,
                "batch_timeout_minutes": c.batch_timeout_minutes,
                "rollback_timeout_minutes": c.rollback_timeout_minutes,
                "require_precheck": c.require_precheck, "max_retries": c.max_retries,
                "failure_rate_abort": c.failure_rate_abort,
                "failure_rate_min_sample": c.failure_rate_min_sample,
                "max_vms_per_run": c.max_vms_per_run, "subnet_kind": c.subnet_kind,
                "subnet_api_group": c.subnet_api_group,
            },
            "vcenter": {
                "server": os.environ.get(ENV_SERVER) or vcenter_meta or "",
                "user": os.environ.get(ENV_USER) or "",
                "password_from_env": bool(os.environ.get(ENV_PASSWORD)),
            },
            "maps": {"folder": self.folder_map_path, "network": self.network_map_path},
            "states": report.STATE_ORDER,
            "state_labels": report.STATE_LABEL,
        }

    def pulse(self, q, body) -> Dict[str, Any]:
        """Cheap, polled every couple of seconds: badges, counters, the running job."""
        with self.lock:
            counts = self.store.counts()
            disc = self.store.discovered_counts()
            live = len(self.store.query_batches(
                states=[st.B_APPLIED, st.B_RUNNING, st.B_ROLLING_BACK]))
        active = self.jobs.active
        latest = self.jobs.list()[:1]
        return {
            "counts": counts,
            "total": sum(counts.values()),
            "failed": sum(counts.get(s, 0) for s in service.FAILED_STATES),
            "in_flight": sum(counts.get(s, 0) for s in service.IN_FLIGHT_STATES),
            "awaiting_commit": counts.get(st.S_AWAITING_COMMIT, 0),
            "discovered": disc,
            "live_batches": live,
            "job": active.summary() if active else None,
            "latest_job": latest[0] if latest else None,
        }

    def overview(self, q, body) -> Dict[str, Any]:
        with self.lock:
            return service.overview(self.store, self.cfg)

    # =========================================================== discovery
    def discovered(self, q, body) -> Dict[str, Any]:
        with self.lock:
            rows = self.store.query_discovered()
            meta = {"vcenter": self.store.get_meta("vcenter"),
                    "discovered_at": self.store.get_meta("discovered_at")}
            queued = {r["moref"]: r["state"] for r in self.store.conn.execute(
                "SELECT moref, state FROM vms")}
        out = []
        for r in rows:
            nics = json.loads(r["nics_json"] or "[]")
            out.append({
                "moref": r["moref"], "name": r["name"], "power_state": r["power_state"],
                "cpu_count": r["cpu_count"], "memory_mb": r["memory_mb"],
                "folder": r["folder"], "datacenter": r["datacenter"], "cluster": r["cluster"],
                "host": r["host"], "guest_os": r["guest_os"], "tools_status": r["tools_status"],
                "networks": r["networks"], "nic_count": len(nics),
                "selected": bool(r["selected"]), "namespace": r["namespace"],
                "wave": r["wave"] or None, "queue_state": queued.get(r["moref"]),
            })
        return {"vms": out, "meta": meta}

    def folders(self, q, body) -> Dict[str, Any]:
        with self.lock:
            return {"tree": self.store.folder_tree()}

    def select(self, q, body) -> Dict[str, Any]:
        morefs = _list(body.get("morefs")) or []
        namespace = body.get("namespace")
        wave = body.get("wave")
        if wave is not None:
            wave = _int(wave, minimum=0)
        if namespace is not None:
            namespace = str(namespace).strip()
        with self.lock:
            if "selected" in body:
                changed = self.store.set_selected(morefs, bool(body["selected"]),
                                                  namespace=namespace, wave=wave)
            else:
                changed = 0
                for r in self.store.query_discovered(morefs=morefs):
                    changed += self.store.set_selected([r["moref"]], bool(r["selected"]),
                                                       namespace=namespace, wave=wave)
            counts = self.store.discovered_counts()
        return {"changed": changed, "counts": counts}

    def select_clear(self, q, body) -> Dict[str, Any]:
        with self.lock:
            cleared = self.store.clear_selection()
            return {"changed": cleared, "counts": self.store.discovered_counts()}

    # ============================================================= mapping
    def _map_rows(self) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        folder_rows = (read_folder_map_rows(self.folder_map_path)
                       if Path(self.folder_map_path).is_file() else [])
        network_rows = (read_network_map_rows(self.network_map_path)
                        if Path(self.network_map_path).is_file() else [])
        trim = lambda rows, cols: [{c: r.get(c, "") for c in cols} for r in rows]  # noqa: E731
        return trim(folder_rows, FOLDER_MAP_COLUMNS), trim(network_rows, NETWORK_MAP_COLUMNS)

    def maps_get(self, q, body) -> Dict[str, Any]:
        folder_rows, network_rows = self._map_rows()
        with self.lock:
            selected = self.store.query_discovered(selected=True)
        coverage = service.mapping_coverage(
            selected, folder_map_from_rows(folder_rows), network_map_from_rows(network_rows))
        return {
            "folder": {"path": self.folder_map_path, "exists": Path(self.folder_map_path).is_file(),
                       "columns": FOLDER_MAP_COLUMNS, "rows": folder_rows},
            "network": {"path": self.network_map_path,
                        "exists": Path(self.network_map_path).is_file(),
                        "columns": NETWORK_MAP_COLUMNS, "rows": network_rows},
            "coverage": coverage,
        }

    def _save_maps(self, body: Dict[str, Any]) -> None:
        if body.get("folder_rows") is not None:
            rows = body["folder_rows"]
            folder_map_from_rows(rows)       # validates; raises SelectionError
            write_map_rows(self.folder_map_path, FOLDER_MAP_COLUMNS, rows)
        if body.get("network_rows") is not None:
            rows = body["network_rows"]
            network_map_from_rows(rows)
            write_map_rows(self.network_map_path, NETWORK_MAP_COLUMNS, rows)

    def maps_put(self, q, body) -> Dict[str, Any]:
        self._save_maps(body)
        return self.maps_get(q, {})

    def _stage_result(self, body: Dict[str, Any]):
        if body.get("folder_rows") is not None or body.get("network_rows") is not None:
            saved_f, saved_n = self._map_rows()
            folder_rows = body.get("folder_rows") if body.get("folder_rows") is not None else saved_f
            network_rows = (body.get("network_rows") if body.get("network_rows") is not None
                            else saved_n)
        else:
            folder_rows, network_rows = self._map_rows()
        folder_mapping = folder_map_from_rows(folder_rows)
        network_mapping = network_map_from_rows(network_rows)
        default_ns = (body.get("default_namespace") or "").strip() or None
        default_wave = _int(body.get("default_wave"), default=1, minimum=1)
        with self.lock:
            selected = self.store.query_discovered(selected=True)
            result = stage(selected, self.cfg, mapping=network_mapping,
                           folder_mapping=folder_mapping, default_namespace=default_ns,
                           default_wave=default_wave)
        coverage = service.mapping_coverage(selected, folder_mapping, network_mapping)
        return selected, result, coverage

    def stage_preview(self, q, body) -> Dict[str, Any]:
        selected, result, coverage = self._stage_result(body)
        folder_of = {r["moref"]: r["folder"] for r in selected}
        with self.lock:
            queued = {r["moref"]: r["state"] for r in self.store.conn.execute(
                "SELECT moref, state FROM vms")}
        by_wave: Dict[int, int] = {}
        by_ns: Dict[str, int] = {}
        records = []
        for rec in result.records:
            by_wave[rec.wave] = by_wave.get(rec.wave, 0) + 1
            by_ns[rec.namespace] = by_ns.get(rec.namespace, 0) + 1
            state = queued.get(rec.moref)
            records.append({
                "moref": rec.moref, "vm_name": rec.vm_name, "namespace": rec.namespace,
                "wave": rec.wave, "group": rec.group, "folder": folder_of.get(rec.moref, ""),
                "subnets": [n.subnet for n in rec.nics if n.subnet], "notes": rec.notes,
                "queue_state": state,
                "locked": state is not None and state not in (
                    st.S_PENDING, st.S_PRECHECK_FAILED, st.S_FAILED, st.S_SKIPPED),
            })
        return {
            "selected": len(selected),
            "records": records,
            "problems": result.problems,
            "unmapped_folders": result.unmapped_folders,
            "unmapped_networks": result.unmapped_networks,
            "by_wave": sorted(by_wave.items()),
            "by_namespace": sorted(by_ns.items()),
            "coverage": coverage,
        }

    def stage_commit(self, q, body) -> Dict[str, Any]:
        if body.get("save_maps"):
            self._save_maps(body)
        selected, result, _coverage = self._stage_result(body)
        if not selected:
            raise ApiError(400, "no VMs are selected")
        if not result.records:
            raise ApiError(400, "nothing could be staged: {}".format(
                "; ".join(result.problems[:3]) or "no records"))
        with self.lock:
            summary = self.store.sync_inventory(result.records)
            self.store.log_event("staged {} VM(s) from the web console ({} added, {} updated)"
                                 .format(len(result.records), summary["added"], summary["updated"]))
        summary["staged"] = len(result.records)
        summary["problems"] = result.problems
        return summary

    # =============================================================== queue
    def vms(self, q, body) -> Dict[str, Any]:
        with self.lock:
            rows = self.store.query_vms()
        return {"vms": [service.vm_summary(r) for r in rows]}

    def vm_detail(self, q, body, moref: str) -> Dict[str, Any]:
        with self.lock:
            row = self.store.get_vm(moref)
            if row is None:
                raise ApiError(404, "{} is not in the import queue".format(moref))
            transitions = [dict(t) for t in self.store.transitions(moref=moref)]
            events = [dict(e) for e in self.store.recent_events(limit=100, moref=moref)]
            batches = []
            for name in {row["precheck_batch"], row["batch_name"]} - {None, ""}:
                b = self.store.get_batch(name, row["namespace"])
                if b is not None:
                    batches.append(dict(b))
        vm = dict(row)
        vm["nics"] = json.loads(vm.pop("nics_json") or "[]")
        issue = service.classify_failure(row["message"] or "") if row["state"] in \
            service.FAILED_STATES else None
        return {"vm": vm, "summary": service.vm_summary(row), "transitions": transitions,
                "events": events, "batches": batches, "issue": issue}

    def vms_wave(self, q, body) -> Dict[str, Any]:
        morefs = _list(body.get("morefs")) or []
        wave = _int(body.get("wave"), minimum=1)
        if wave is None:
            raise ApiError(400, "wave is required")
        with self.lock:
            moved, refused = self.store.set_vm_wave(morefs, wave)
        return {"moved": moved, "refused": refused}

    def waves_swap(self, q, body) -> Dict[str, Any]:
        a, b = _int(body.get("a"), minimum=1), _int(body.get("b"), minimum=1)
        if a is None or b is None:
            raise ApiError(400, "two waves are needed: a and b")
        with self.lock:
            ok, refused = self.store.swap_waves(a, b)
        if not ok:
            raise ApiError(409, "waves {} and {} cannot be swapped -- some of their VMs are "
                                "already in a batch: {}".format(a, b, "; ".join(refused)))
        return {"swapped": True}

    def vms_skip(self, q, body) -> Dict[str, Any]:
        with self.lock:
            changed, notes = service.skip_vms(self.store, _list(body.get("morefs")) or [],
                                              unskip=bool(body.get("unskip")))
        return {"changed": changed, "notes": notes}

    def vms_retry(self, q, body) -> Dict[str, Any]:
        with self.lock:
            moved = self._engine().requeue(
                morefs=_list(body.get("morefs")), wave=_int(body.get("wave")),
                force=bool(body.get("force")), folders=_list(body.get("folders")),
                folder_exact=bool(body.get("folder_exact")))
        return {"requeued": moved}

    # ============================================================= batches
    def batches(self, q, body) -> Dict[str, Any]:
        with self.lock:
            rows = self.store.query_batches()
            members = self.store.batch_member_counts()
        out = []
        for b in rows:
            d = dict(b)
            d["members"] = members.get((b["namespace"], b["name"]), {})
            out.append(d)
        out.sort(key=lambda d: d["created_at"] or "", reverse=True)
        return {"batches": out}

    def batch_detail(self, q, body, ns: str, name: str) -> Dict[str, Any]:
        with self.lock:
            b = self.store.get_batch(name, ns)
            if b is None:
                raise ApiError(404, "no batch {}/{} in the run state".format(ns, name))
            members = [service.vm_summary(v) for v in service.batch_members(self.store, b)]
            events = [dict(e) for e in self.store.recent_events(limit=100, batch=name)]
        manifest = None
        if b["manifest_path"]:
            p = Path(b["manifest_path"])
            if p.is_file() and p.stat().st_size < 2_000_000:
                manifest = p.read_text(encoding="utf-8", errors="replace")
        return {"batch": dict(b), "members": members, "events": events, "manifest": manifest}

    # ============================================================ previews
    def _exec_cfg(self, body: Dict[str, Any]) -> Config:
        cfg = copy.deepcopy(self.cfg)
        if body.get("batch_size"):
            cfg.batch_size = _int(body["batch_size"], minimum=1)
        if body.get("parallel"):
            cfg.max_parallel_batches = _int(body["parallel"], minimum=1)
        if body.get("no_precheck"):
            cfg.require_precheck = False
        cfg.validate()
        return cfg

    def execute_preview(self, q, body) -> Dict[str, Any]:
        stage_name = _stage(body.get("stage"))
        cfg = self._exec_cfg(body)
        waves = _list(body.get("waves"), int)
        folders = _list(body.get("folders"))
        morefs = _list(body.get("morefs"))
        include_failed = bool(body.get("include_failed"))
        limit = _int(body.get("limit"), default=0, minimum=0)
        with self.lock:
            engine = self._engine(cfg)
            rows = service.eligible_by_wave(engine, stage_name, waves, morefs=morefs,
                                            include_failed=include_failed, folders=folders,
                                            folder_exact=bool(body.get("folder_exact")))
            plan = []
            for wave, count in rows:
                batches = engine.plan(stage_name, wave=wave, morefs=morefs, limit=limit,
                                      include_failed=include_failed, write_manifests=False,
                                      folders=folders, folder_exact=bool(body.get("folder_exact")))
                plan.append({"wave": wave, "vms": sum(b.size for b in batches),
                             "eligible": count, "batches": len(batches),
                             "namespaces": sorted({b.namespace for b in batches})})
            all_waves = self.store.waves()
            per_wave_all = {w: len(engine.eligible(stage_name, w, include_failed=include_failed))
                            for w in all_waves}
        return {
            "stage": stage_name, "plan": plan,
            "total": sum(p["vms"] for p in plan),
            "eligible_by_wave": per_wave_all,
            "settings": {"context": cfg.context, "batch_size": cfg.batch_size,
                         "max_parallel_batches": cfg.max_parallel_batches,
                         "max_parallel_batches_per_namespace": cfg.max_parallel_batches_per_namespace,
                         "commit_action": cfg.commit_action,
                         "require_precheck": cfg.require_precheck},
        }

    def rollback_preview(self, q, body) -> Dict[str, Any]:
        with self.lock:
            engine = self._engine()
            batches, notes = engine.rollback_targets(
                batch_names=_list(body.get("batches")), morefs=_list(body.get("morefs")),
                failed_only=bool(body.get("failed")), wave=_int(body.get("wave")),
                folders=_list(body.get("folders")), folder_exact=bool(body.get("folder_exact")))
            rows = service.rollback_preview(engine, batches)
        return {"batches": rows, "notes": notes,
                "revert": sum(r["revert"] for r in rows),
                "committed": sum(r["committed"] for r in rows)}

    def abandon_preview(self, q, body) -> Dict[str, Any]:
        with self.lock:
            engine = self._engine()
            batches, notes = engine.abandon_targets(
                batch_names=_list(body.get("batches")), morefs=_list(body.get("morefs")),
                folders=_list(body.get("folders")), folder_exact=bool(body.get("folder_exact")),
                stage=body.get("stage") or None)
            rows = service.abandon_preview(self.store, batches)
        return {"batches": rows, "notes": notes}

    def triage(self, q, body) -> Dict[str, Any]:
        with self.lock:
            return service.failure_groups(self.store)

    # ============================================================ activity
    def events(self, q, body) -> Dict[str, Any]:
        with self.lock:
            rows = self.store.recent_events(
                limit=_int(q.get("limit"), default=300, minimum=1),
                level=q.get("level") or None, moref=q.get("moref") or None,
                batch=q.get("batch") or None)
        return {"events": [dict(r) for r in rows]}

    def transitions(self, q, body) -> Dict[str, Any]:
        with self.lock:
            rows = self.store.transitions(
                moref=q.get("moref") or None, limit=_int(q.get("limit"), default=500, minimum=1),
                to_state=q.get("state") or None, since=q.get("since") or None,
                newest_first=True)
            stats = self.store.transition_stats()
        return {"transitions": [dict(r) for r in rows], "stats": stats,
                "ledger_path": self.store.ledger_path}

    def jobs_list(self, q, body) -> Dict[str, Any]:
        return {"jobs": self.jobs.list(), "active": self.jobs.active.id if self.jobs.active else None}

    def job(self, q, body, job_id: str) -> Dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError(404, "no job {}".format(job_id))
        return job.snapshot(since=_int(q.get("since"), default=0, minimum=0))

    def job_stop(self, q, body, job_id: str) -> Dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError(404, "no job {}".format(job_id))
        if not job.request_stop():
            raise ApiError(409, "this job cannot be stopped (finished, or not interruptible)")
        return {"stopping": True}

    def export(self, q, body, name: str) -> Tuple[bytes, str, str]:
        workdir = Path(self.cfg.workdir).expanduser()
        with self.lock:
            if name == "ledger.jsonl":
                path = Path(self.store.ledger_path)
                data = path.read_bytes() if path.is_file() else b""
                return data, "application/x-ndjson", name
            fd, tmp = tempfile.mkstemp(dir=str(workdir), suffix="-" + name)
            os.close(fd)
            try:
                if name == "tracker.csv":
                    report.write_csv(self.store, tmp)
                    kind = "text/csv"
                elif name == "transitions.csv":
                    report.write_transitions_csv(self.store, tmp)
                    kind = "text/csv"
                else:
                    report.write_html(self.store, self.cfg, tmp)
                    kind = "text/html"
                return Path(tmp).read_bytes(), kind, name
            finally:
                Path(tmp).unlink(missing_ok=True)

    # ================================================================ jobs
    def run_job(self, q, body, kind: str) -> Dict[str, Any]:
        starter = getattr(self, "_job_" + kind)
        job = starter(body)
        return {"job": job.summary()}

    def _job_discover(self, body: Dict[str, Any]) -> Job:
        server = (body.get("server") or os.environ.get(ENV_SERVER) or "").strip()
        user = (body.get("user") or os.environ.get(ENV_USER) or "").strip()
        password = body.get("password") or os.environ.get(ENV_PASSWORD) or ""
        if not server or not user:
            raise ApiError(400, "vCenter server and user are required")
        if not password:
            raise ApiError(400, "a password is required (or set {} before starting the "
                                "console)".format(ENV_PASSWORD))
        insecure = bool(body.get("insecure"))
        opts = {
            "power_state": "POWERED_ON" if body.get("powered_on") else None,
            "with_tools": not body.get("no_tools"),
            "with_placement": not body.get("no_placement"),
            "concurrency": _int(body.get("concurrency"), default=12, minimum=1),
        }
        timeout = _int(body.get("timeout"), default=60, minimum=5)
        params = {"server": server, "user": user, "insecure": insecure,
                  "powered_on": bool(body.get("powered_on")), "no_tools": bool(body.get("no_tools")),
                  "concurrency": opts["concurrency"]}

        def run(job: Job) -> Dict[str, Any]:
            if insecure:
                job.log("warning: TLS verification disabled")
            client = VCenterClient(server, user, password, insecure=insecure,
                                   timeout=timeout, log=job.log)
            try:
                client.connect()
                vms = discover(client, progress=lambda d, t: job.set_progress(
                    d, t, "VM details read"), log=job.log, **opts)
            finally:
                client.close()
            with self._job_workspace(job) as ws:
                summary = ws.store.upsert_discovered(vms)
                ws.store.set_meta("vcenter", server)
                ws.store.set_meta("discovered_at", _local_now())
                counts = ws.store.discovered_counts()
            job.log("")
            job.log("discovered {} VM(s) from {} (added {}, refreshed {})".format(
                len(vms), server, summary["added"], summary["updated"]))
            no_nics = sum(1 for v in vms if not v.nics)
            stale = sum(1 for v in vms if opts["with_tools"]
                        and "running" not in (v.tools_status or "").lower())
            if no_nics:
                job.log("  {} VM(s) have no network adapters".format(no_nics))
            if stale:
                job.log("  {} VM(s) are not running VM Tools (likely precheck failures)".format(stale))
            return {"discovered": len(vms), "added": summary["added"],
                    "updated": summary["updated"], "total": counts["total"],
                    "selected": counts["selected"], "no_nics": no_nics, "tools_not_running": stale}

        return self.jobs.start("discover", "Discover {}".format(server), params, run)

    def _job_preflight(self, body: Dict[str, Any]) -> Job:
        skip_targets = bool(body.get("skip_targets"))

        def run(job: Job) -> Dict[str, Any]:
            with self._job_workspace(job) as ws:
                job.log("checking context, CRDs, operator, namespaces, RBAC and subnets...")
                result = ws.engine.preflight(check_targets=not skip_targets)
            job.log("context        : {}".format(result.context))
            job.log("server version : {}".format(result.server_version or "unknown"))
            for name, ok, detail in result.checks:
                job.log("  [{}] {:<32} {}".format("ok" if ok else "!!", name, detail))
            for w in result.warnings:
                job.log("  warning: " + w)
            for p in result.problems:
                job.log("  PROBLEM: " + p)
            job.log("preflight passed" if result.ok else "preflight found problems")
            return {"status": None if result.ok else WARNING, "ok": result.ok,
                    "context": result.context, "server_version": result.server_version,
                    "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in result.checks],
                    "problems": result.problems, "warnings": result.warnings,
                    "crds": sorted(result.crds)}

        return self.jobs.start("preflight", "Preflight", {"skip_targets": skip_targets}, run)

    def _job_execute(self, body: Dict[str, Any]) -> Job:
        _require_confirm(body)
        stage_name = _stage(body.get("stage"))
        cfg = self._exec_cfg(body)
        waves = _list(body.get("waves"), int)
        folders = _list(body.get("folders"))
        folder_exact = bool(body.get("folder_exact"))
        morefs = _list(body.get("morefs"))
        include_failed = bool(body.get("include_failed"))
        limit = _int(body.get("limit"), default=0, minimum=0)
        dry_run = bool(body.get("dry_run"))
        rollback_failed = bool(body.get("rollback_failed")) and stage_name == STAGE_IMPORT
        params = {"stage": stage_name, "waves": waves, "folders": folders, "morefs": morefs,
                  "include_failed": include_failed, "limit": limit, "dry_run": dry_run,
                  "batch_size": cfg.batch_size, "parallel": cfg.max_parallel_batches,
                  "require_precheck": cfg.require_precheck, "rollback_failed": rollback_failed}
        title = "{}{} {}".format(
            "Dry run: " if dry_run else "", "Precheck" if stage_name == STAGE_PRECHECK else "Import",
            "wave " + ", ".join(str(w) for w in waves) if waves else "all waves")

        def run(job: Job) -> Dict[str, Any]:
            with self._job_workspace(job, cfg, dry_run=dry_run) as ws:
                job.on_stop(ws.engine.request_stop)
                job.log("{} with context {} | batch size {} | parallel {} (per namespace {})".format(
                    stage_name, cfg.context or "(kubectl current-context)", cfg.batch_size,
                    cfg.max_parallel_batches, cfg.max_parallel_batches_per_namespace))
                if stage_name == STAGE_IMPORT:
                    job.log("commitAction {}".format(cfg.commit_action))
                if dry_run:
                    job.log("DRY RUN: manifests are rendered and validated, nothing is applied")
                totals = service.run_stage(
                    ws.engine, stage_name, waves=waves, limit=limit, morefs=morefs,
                    include_failed=include_failed, folders=folders, folder_exact=folder_exact,
                    rollback_failed=rollback_failed, log=job.log)
                job.log("")
                if totals["halted"]:
                    job.log("RUN HALTED: {}".format(totals["halted"]))
                job.log("{} finished: applied {} | succeeded {} | failed {} | awaiting commit {}"
                        .format(stage_name, totals["applied"], totals["succeeded"],
                                totals["failed"], totals["awaiting_commit"]))
                job.log("")
                job.log(report.render_status(ws.store, cfg, failures=5))
            status = None
            if totals["halted"] or totals["failed"]:
                status = WARNING
            if job.stop_requested:
                status = STOPPED
            return dict(totals, status=status)

        lock = None if dry_run else service.WorkspaceLock(cfg, "'{}' from the web console".format(title))
        return self.jobs.start("execute", title, params, run, stoppable=True, lock=lock)

    def _job_refresh(self, body: Dict[str, Any]) -> Job:
        def run(job: Job) -> Dict[str, Any]:
            with self._job_workspace(job) as ws:
                counts = ws.engine.refresh()
            job.log("re-polled {} batch(es); {} VM result(s) recorded".format(
                counts["batches"], counts["updated"]))
            return counts

        return self.jobs.start("refresh", "Refresh from cluster", {}, run, lock=self._lock_for("refresh"))

    def _job_watch(self, body: Dict[str, Any]) -> Job:
        interval = _int(body.get("interval"), default=max(self.cfg.poll_interval_seconds, 5),
                        minimum=1)

        def run(job: Job) -> Dict[str, Any]:
            stop = threading.Event()
            job.on_stop(stop.set)
            polls = 0
            with self._job_workspace(job) as ws:
                while not stop.is_set():
                    counts = ws.engine.refresh()
                    polls += 1
                    remaining = ws.store.counts()
                    active = sum(remaining.get(s, 0) for s in service.IN_FLIGHT_STATES)
                    job.log("refreshed {} batch(es); {} VM(s) still in flight".format(
                        counts["batches"], active))
                    if not active:
                        job.log("nothing in flight; watch finished")
                        break
                    stop.wait(interval)
            return {"polls": polls}

        return self.jobs.start("watch", "Watch in-flight batches", {"interval": interval}, run,
                               stoppable=True, lock=self._lock_for("watch"))

    def _job_commit(self, body: Dict[str, Any]) -> Job:
        _require_confirm(body)
        morefs = _list(body.get("morefs"))
        wave = _int(body.get("wave"))

        def run(job: Job) -> Dict[str, Any]:
            with self._job_workspace(job) as ws:
                result = ws.engine.commit(morefs=morefs, wave=wave)
            job.log("patched {} batch(es) covering {} VM(s)".format(result["batches"], result["vms"]))
            for err in result["errors"]:
                job.log("  ! " + err)
            job.log("refresh once the operator has processed the commit")
            return dict(result, status=WARNING if result["errors"] else None)

        return self.jobs.start("commit", "Commit held imports", {"morefs": morefs, "wave": wave}, run,
                               lock=self._lock_for("commit"))

    def _job_rollback(self, body: Dict[str, Any]) -> Job:
        _require_confirm(body)
        sel = {"batch_names": _list(body.get("batches")), "morefs": _list(body.get("morefs")),
               "failed_only": bool(body.get("failed")), "wave": _int(body.get("wave")),
               "folders": _list(body.get("folders")), "folder_exact": bool(body.get("folder_exact"))}
        if not (sel["batch_names"] or sel["morefs"] or sel["failed_only"] or sel["folders"]):
            raise ApiError(400, "say what to roll back: batches, morefs, folders or failed")
        action = body.get("action") or "Immediate"
        delete = bool(body.get("delete"))
        wait = body.get("wait", True) is not False
        timeout = _int(body.get("timeout"), minimum=1)

        def run(job: Job) -> Dict[str, Any]:
            with self._job_workspace(job) as ws:
                batches, notes = ws.engine.rollback_targets(**sel)
                for note in notes:
                    job.log("  ! " + note)
                if not batches:
                    job.log("nothing to roll back")
                    return {"patched": [], "reverted": 0, "status": WARNING if notes else None}
                result = ws.engine.rollback(batches, action=action, wait=wait, delete=delete,
                                            timeout_minutes=timeout)
            job.log("patched {} batch(es); {} VM(s) confirmed reverted to vCenter; {} deleted".format(
                len(result["patched"]), result["reverted"], len(result["deleted"])))
            for name in result["pending"]:
                job.log("  ~ {} still reverting -- refresh to follow up".format(name))
            for err in result["errors"]:
                job.log("  ! " + err)
            bad = result["errors"] or result["pending"]
            return dict(result, status=WARNING if bad else None)

        params = dict(sel, action=action, delete=delete, wait=wait)
        return self.jobs.start("rollback", "Roll back to vCenter", params, run,
                               lock=self._lock_for("rollback"))

    def _job_abandon(self, body: Dict[str, Any]) -> Job:
        _require_confirm(body)
        sel = {"batch_names": _list(body.get("batches")), "morefs": _list(body.get("morefs")),
               "folders": _list(body.get("folders")), "folder_exact": bool(body.get("folder_exact")),
               "stage": body.get("stage") or None}
        if not (sel["batch_names"] or sel["morefs"] or sel["folders"] or sel["stage"]):
            raise ApiError(400, "say what to abandon: batches, morefs, folders or stage")

        def run(job: Job) -> Dict[str, Any]:
            with self._job_workspace(job) as ws:
                batches, notes = ws.engine.abandon_targets(**sel)
                for note in notes:
                    job.log("  ! " + note)
                if not batches:
                    job.log("nothing to abandon")
                    return {"deleted": [], "requeued": 0, "status": WARNING if notes else None}
                result = ws.engine.abandon(batches)
            job.log("deleted {} batch(es); {} VM(s) back to pending".format(
                len(result["deleted"]), result["requeued"]))
            for err in result["errors"]:
                job.log("  ! " + err)
            return dict(result, status=WARNING if (result["errors"] or notes) else None)

        return self.jobs.start("abandon", "Abandon batches", sel, run, lock=self._lock_for("abandon"))

    def _job_cleanup(self, body: Dict[str, Any]) -> Job:
        _require_confirm(body)
        wanted = set(_list(body.get("batches")) or [])

        def run(job: Job) -> Dict[str, Any]:
            with self._job_workspace(job) as ws:
                candidates = ws.engine.cleanup_candidates()
                if wanted:
                    candidates = [b for b in candidates if b["name"] in wanted]
                    for name in sorted(wanted - {b["name"] for b in candidates}):
                        job.log("  ! {}: not in a confirmed rolled-back state; refusing to delete"
                                .format(name))
                if not candidates:
                    job.log("no batches with a confirmed rollback to delete")
                    return {"deleted": []}
                result = ws.engine.delete_batches(candidates)
            job.log("deleted {} batch(es)".format(len(result["deleted"])))
            for err in result["errors"]:
                job.log("  ! " + err)
            return dict(result, status=WARNING if result["errors"] else None)

        return self.jobs.start("cleanup", "Delete rolled-back batches",
                               {"batches": sorted(wanted)}, run, lock=self._lock_for("cleanup"))


def _local_now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


# (method, path regex, handler name). Named groups become keyword arguments.
ROUTES: List[Tuple[str, "re.Pattern[str]", str]] = [
    (m, re.compile("^" + p + "$"), h) for m, p, h in [
        ("GET", r"/api/info", "info"),
        ("GET", r"/api/pulse", "pulse"),
        ("GET", r"/api/overview", "overview"),
        ("GET", r"/api/discovered", "discovered"),
        ("GET", r"/api/folders", "folders"),
        ("POST", r"/api/select", "select"),
        ("POST", r"/api/select/clear", "select_clear"),
        ("GET", r"/api/maps", "maps_get"),
        ("PUT", r"/api/maps", "maps_put"),
        ("POST", r"/api/stage/preview", "stage_preview"),
        ("POST", r"/api/stage", "stage_commit"),
        ("GET", r"/api/vms", "vms"),
        ("POST", r"/api/vms/wave", "vms_wave"),
        ("POST", r"/api/vms/skip", "vms_skip"),
        ("POST", r"/api/vms/retry", "vms_retry"),
        ("GET", r"/api/vms/(?P<moref>[^/]+)", "vm_detail"),
        ("POST", r"/api/waves/swap", "waves_swap"),
        ("GET", r"/api/batches", "batches"),
        ("GET", r"/api/batches/(?P<ns>[^/]+)/(?P<name>[^/]+)", "batch_detail"),
        ("POST", r"/api/execute/preview", "execute_preview"),
        ("POST", r"/api/rollback/preview", "rollback_preview"),
        ("POST", r"/api/abandon/preview", "abandon_preview"),
        ("GET", r"/api/triage", "triage"),
        ("GET", r"/api/events", "events"),
        ("GET", r"/api/transitions", "transitions"),
        ("GET", r"/api/jobs", "jobs_list"),
        ("GET", r"/api/jobs/(?P<job_id>[\w.-]+)", "job"),
        ("POST", r"/api/jobs/(?P<job_id>[\w.-]+)/stop", "job_stop"),
        ("POST", r"/api/run/(?P<kind>discover|preflight|execute|refresh|watch|commit|rollback|"
                 r"abandon|cleanup)", "run_job"),
        ("GET", r"/api/export/(?P<name>tracker\.csv|transitions\.csv|ledger\.jsonl|report\.html)",
         "export"),
    ]
]

# Errors that are the user's to fix, reported as 400 rather than 500.
USER_ERRORS = (SelectionError, ValueError, VCenterError)
# Errors meaning "not now": reported as 409 Conflict.
BUSY_ERRORS = (service.WorkspaceBusy,)
