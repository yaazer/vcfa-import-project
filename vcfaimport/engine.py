"""Orchestration: preflight, precheck, import, commit, retry, rollback."""

from __future__ import annotations

import json
import signal
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .config import (
    BUCKET_AWAITING_COMMIT,
    BUCKET_FAILED,
    BUCKET_ROLLED_BACK,
    BUCKET_RUNNING,
    BUCKET_SUCCEEDED,
    BUCKET_UNKNOWN,
    Config,
)
from .kube import Kubectl, KubectlError
from .folders import folder_matches
from .planner import PlannedBatch, plan_batches, record_from_row
from .render import LABEL_RUN
from . import state as st
from .status import BatchStatus, batch_status

STAGE_PRECHECK = "precheck"
STAGE_IMPORT = "import"


class AbortRun(Exception):
    """Raised when a safety guard halts the run."""


@dataclass
class PreflightResult:
    ok: bool
    context: str
    server_version: Optional[str]
    crds: Dict[str, str]
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks: List[Tuple[str, bool, str]] = field(default_factory=list)


class Engine:
    def __init__(self, cfg: Config, store: st.Store, kube: Kubectl, log: Callable[[str], None]):
        self.cfg = cfg
        self.store = store
        self.kube = kube
        self.log = log
        self._stopping = False
        self._orig_sigint = None
        # Change windows: never start a batch that would not finish by `deadline`
        # (epoch seconds), given one batch takes `deadline_batch_s`.
        self.deadline: Optional[float] = None
        self.deadline_batch_s: float = 0.0
        self.hit_deadline = False
        # Why VMs were held back from the last eligibility check (apps, readiness).
        self.holdbacks: List[str] = []

    # ------------------------------------------------------------- signals
    def request_stop(self) -> None:
        """Graceful stop, as the first Ctrl-C: apply nothing new, finish what is in flight.

        Safe to call from another thread (the web console's Stop button).
        """
        if not self._stopping:
            self._stopping = True
            self.log("! stop requested -- no new batches will be applied; "
                     "in-flight batches will be polled to completion")

    def request_rollback_stop(self) -> None:
        """Stop for a rollback: patch no further batches and stop waiting. A revert
        already requested carries on -- the operator does not take it back."""
        if not self._stopping:
            self._stopping = True
            self.log("! stop requested -- no further batches will be patched; batches already "
                     "given rollbackAction keep reverting on the cluster (refresh to follow up)")

    @property
    def stopping(self) -> bool:
        return self._stopping

    def _install_sigint(self) -> None:
        def handler(signum, frame):  # noqa: ARG001
            if self._stopping:
                raise KeyboardInterrupt
            self._stopping = True
            self.log("\n! stop requested -- no new batches will be applied; "
                     "in-flight batches will be polled to completion (Ctrl-C again to force)")
        try:
            self._orig_sigint = signal.signal(signal.SIGINT, handler)
        except ValueError:  # not on the main thread
            self._orig_sigint = None

    def _restore_sigint(self) -> None:
        if self._orig_sigint is not None:
            try:
                signal.signal(signal.SIGINT, self._orig_sigint)
            except ValueError:
                pass

    # ----------------------------------------------------------- preflight
    def preflight(self, check_targets: bool = True) -> PreflightResult:
        problems: List[str] = []
        warnings: List[str] = []
        checks: List[Tuple[str, bool, str]] = []

        info = self.kube.preflight()
        crds = info.get("resources") or {}
        checks.append(("kubectl reachable", True, info.get("context") or "?"))

        have_batch = self.cfg.batch_resource in crds
        checks.append((
            "{} CRD present".format(self.cfg.batch_kind),
            have_batch,
            self.cfg.api_group if have_batch else "not found in api-resources",
        ))
        if not have_batch:
            problems.append(
                "{} not found in API group {}. Is this the Supervisor context, and is the "
                "Mobility Operator (VCF 9.1+) installed?".format(
                    self.cfg.batch_resource, self.cfg.api_group)
            )

        have_ops = self.cfg.operation_resource in crds
        checks.append(("ImportOperation CRD present", have_ops,
                       "per-VM status available" if have_ops else "will fall back to batch status"))
        if not have_ops:
            warnings.append(
                "{} not present; per-VM status will be inferred from the batch object"
                .format(self.cfg.operation_resource)
            )

        pod_state = self._operator_pod_state()
        if pod_state is not None:
            healthy, detail = pod_state
            checks.append(("Mobility Operator pod running", healthy, detail))
            if not healthy:
                problems.append("the Mobility Operator pod is not healthy: " + detail)

        namespaces = self.store.namespaces()
        if check_targets and namespaces:
            missing_ns: List[str] = []
            unverified: List[str] = []    # the API server did not answer: unknown, not missing
            for ns in namespaces:
                found = self.kube.exists("namespace", ns)
                if found is None:
                    unverified.append("namespace " + ns)
                elif not found:
                    missing_ns.append(ns)
            checks.append(("target namespaces exist", not missing_ns,
                           "{}/{} present".format(len(namespaces) - len(missing_ns), len(namespaces))))
            if missing_ns:
                problems.append("namespace(s) not found: " + ", ".join(sorted(missing_ns)[:20]))

            # RBAC: creating batches is the one verb we cannot do without.
            denied = []
            for ns in namespaces[:25]:
                allowed = self.kube.can_i("create", self.cfg.batch_resource, ns)
                if allowed is None:
                    unverified.append("create permission in " + ns)
                elif not allowed:
                    denied.append(ns)
            checks.append(("can create batches", not denied,
                           "denied in " + ", ".join(denied[:5]) if denied else "ok"))
            if denied:
                problems.append(
                    "not authorised to create {} in: {}".format(
                        self.cfg.batch_resource, ", ".join(sorted(denied)[:10]))
                )

            missing_subnets: List[str] = []
            vpc_scoped: List[str] = []
            for entry in self.store.subnets():
                resource = entry["kind"].lower()
                if entry["api_group"]:
                    resource = "{}.{}".format(resource, entry["api_group"])
                found = self.kube.exists(resource, entry["subnet"], entry["namespace"])
                if found:
                    continue
                if found is None:
                    unverified.append("subnet {}/{}".format(entry["namespace"], entry["subnet"]))
                    continue
                # VPC-mode namespaces reference subnets that live in the VPC's own
                # namespace; the batch names them without a namespace and the
                # operator resolves them. Seen in the lab: subnet in
                # testing-vpc-k826r, referenced from migration-testing-ns-kcvm5.
                elsewhere = self._find_subnet_elsewhere(resource, entry["subnet"])
                if elsewhere:
                    vpc_scoped.append("{}/{} -> found in namespace {}".format(
                        entry["namespace"], entry["subnet"], elsewhere))
                    continue
                missing_subnets.append("{}/{}".format(entry["namespace"], entry["subnet"]))
            detail = "ok" if not missing_subnets else "{} missing".format(len(missing_subnets))
            if vpc_scoped and not missing_subnets:
                detail = "ok ({} VPC-scoped)".format(len(vpc_scoped))
            checks.append(("target subnets exist", not missing_subnets, detail))
            for line in vpc_scoped:
                warnings.append("subnet is VPC-scoped, not in the tenant namespace: " + line)
            if missing_subnets:
                problems.append(
                    "subnet(s) not found: " + ", ".join(sorted(missing_subnets)[:20])
                    + ("  (+{} more)".format(len(missing_subnets) - 20) if len(missing_subnets) > 20 else "")
                )
                # The usual cause is a display name that differs from the object
                # name, or a different kind (SubnetSet). Show what is actually
                # there so the fix can be read straight off the output.
                for ns in sorted({m.split("/", 1)[0] for m in missing_subnets}):
                    found = self._subnet_like_objects(ns)
                    if found:
                        problems.append(
                            "  in namespace {} the subnet-like objects are:\n    {}\n"
                            "  use one of these names in the map, and if the kind differs set "
                            "subnet_kind / subnet_api_group in the config".format(
                                ns, "\n    ".join(found)))
                    else:
                        problems.append(
                            "  namespace {} has no subnet-like objects visible to you: either the "
                            "subnet has not been created/shared into it yet, or your account "
                            "cannot list subnets there (kubectl auth can-i list subnets.crd.nsx.vmware.com -n {})"
                            .format(ns, ns))
            if unverified:
                warnings.append(
                    "could not verify {} item(s) because the API server kept timing out: {}. "
                    "They are not known to be missing -- check connectivity to the Supervisor "
                    "(a wedged DNS resolver on a control-plane node has caused this before) and "
                    "run preflight again".format(len(unverified), ", ".join(unverified[:8])))

        return PreflightResult(
            ok=not problems,
            context=info.get("context") or "?",
            server_version=info.get("server_version"),
            crds=crds,
            problems=problems,
            warnings=warnings,
            checks=checks,
        )

    def _operator_pod_state(self) -> Optional[Tuple[bool, str]]:
        """(healthy, detail) for the mobility operator's pod(s); None if not found."""
        try:
            listing = self.kube.get_json("pods", all_namespaces=True, check=False) or {}
        except KubectlError:
            return None
        hits = []
        for pod in listing.get("items", []):
            meta = pod.get("metadata") or {}
            name = meta.get("name", "")
            if "mobility" not in name.lower() and "mobility" not in meta.get("namespace", "").lower():
                continue
            phase = (pod.get("status") or {}).get("phase", "")
            ready = all(str(c.get("ready", "")).lower() == "true"
                        for c in (pod.get("status") or {}).get("containerStatuses", []) or [{}])
            restarts = sum(int(c.get("restartCount", 0))
                           for c in (pod.get("status") or {}).get("containerStatuses", []) or [])
            hits.append((name, meta.get("namespace", ""), phase, ready, restarts))
        if not hits:
            return None
        bad = [h for h in hits if h[2] != "Running" or not h[3]]
        detail = "; ".join("{} {} ready={} restarts={}".format(h[0][:40], h[2], h[3], h[4])
                           for h in hits[:3])
        return (not bad, detail)

    def _find_subnet_elsewhere(self, resource: str, name: str) -> Optional[str]:
        try:
            listing = self.kube.get_json(resource, all_namespaces=True, check=False) or {}
        except KubectlError:
            return None
        for item in listing.get("items", []):
            meta = item.get("metadata") or {}
            if meta.get("name") == name:
                return meta.get("namespace")
        return None

    # Every resource kind that can stand in for a subnet in a Supervisor
    # namespace, across the NSX and VPC API groups seen in VCF 9.x.
    SUBNET_LIKE_RESOURCES = (
        "subnets.crd.nsx.vmware.com",
        "subnetsets.crd.nsx.vmware.com",
        "subnets.nsx.vmware.com",
        "subnetsets.nsx.vmware.com",
        "subnets.vpc.vmware.com",
        "networks.netoperator.vmware.com",
    )

    def _subnet_like_objects(self, namespace: str) -> List[str]:
        """'kind/name' for every subnet-ish object in the namespace, any known group."""
        found: List[str] = []
        for resource in self.SUBNET_LIKE_RESOURCES:
            try:
                listing = self.kube.get_json(resource, namespace=namespace, check=False)
            except KubectlError:
                continue   # that kind does not exist on this cluster, or is not readable
            for item in (listing or {}).get("items", []) or []:
                meta = item.get("metadata") or {}
                name = meta.get("name", "")
                kind = item.get("kind") or resource.split(".")[0]
                group = (item.get("apiVersion") or "").split("/")[0]
                label = "{}.{}/{}".format(kind, group, name) if group else "{}/{}".format(kind, name)
                display = ((meta.get("annotations") or {}).get("vmware-system-display-name")
                           or (meta.get("labels") or {}).get("vmware-system-display-name"))
                if display and display != name:
                    label += "   (display name: {})".format(display)
                if label not in found:   # one kind can answer to several resource aliases
                    found.append(label)
        return found

    # ------------------------------------------------------------ planning
    def eligible(self, stage: str, wave: Optional[int], morefs: Optional[Sequence[str]] = None,
                 include_failed: bool = False, folders: Optional[Sequence[str]] = None,
                 folder_exact: bool = False) -> List[sqlite3.Row]:
        if stage == STAGE_PRECHECK:
            states = [st.S_PENDING]
            if include_failed:
                states.append(st.S_PRECHECK_FAILED)
        else:
            states = [st.S_PRECHECK_PASSED]
            if not self.cfg.require_precheck:
                states.append(st.S_PENDING)
            if include_failed:
                states.append(st.S_FAILED)
        rows = self.store.query_vms(states=states, wave=wave, morefs=morefs,
                                    folders=folders, folder_exact=folder_exact)
        self.holdbacks = []
        if stage == STAGE_PRECHECK and getattr(self.cfg, "readiness_exclude_blocked", False) and rows:
            from .readiness import blocked_morefs
            blocked = set(blocked_morefs(self.store, [r["moref"] for r in rows]))
            if blocked:
                self.holdbacks.append("{} VM(s) held back: readiness marks them blocked".format(len(blocked)))
                rows = [r for r in rows if r["moref"] not in blocked]
        if stage == STAGE_IMPORT and getattr(self.cfg, "app_together", False) and rows:
            rows = self._whole_apps(rows)
        return rows

    def _whole_apps(self, rows: List[sqlite3.Row]) -> List[sqlite3.Row]:
        """Import an application only when every one of its VMs can go now.

        A member that is committed (or skipped) is already settled; any other
        member that is not eligible in this very run -- not prechecked, failed,
        or in another wave or scope -- holds the whole application back.
        """
        eligible = {r["moref"] for r in rows}
        keep = []
        held = {}
        by_app: Dict[str, List[sqlite3.Row]] = {}
        for r in rows:
            (by_app.setdefault(r["app"], []) if r["app"] else keep).append(r)
        for app, members in by_app.items():
            blockers = [m for m in self.store.query_vms(morefs=self.store.app_members([app]))
                        if m["moref"] not in eligible and m["state"] not in (st.S_COMMITTED, st.S_SKIPPED)]
            if blockers:
                held[app] = blockers
            else:
                keep.extend(members)
        for app, blockers in sorted(held.items()):
            self.holdbacks.append("app {} held back: {} VM(s) not ready ({})".format(
                app, len(blockers), ", ".join("{} {}".format(b["vm_name"], b["state"]) for b in blockers[:3])
                + (" ..." if len(blockers) > 3 else "")))
        order = {r["moref"]: i for i, r in enumerate(rows)}
        return sorted(keep, key=lambda r: order[r["moref"]])

    def plan(
        self,
        stage: str,
        wave: Optional[int] = None,
        morefs: Optional[Sequence[str]] = None,
        limit: int = 0,
        include_failed: bool = False,
        run_id: Optional[str] = None,
        name_salt: Optional[str] = None,
        write_manifests: bool = True,
        folders: Optional[Sequence[str]] = None,
        folder_exact: bool = False,
    ) -> List[PlannedBatch]:
        rows = self.eligible(stage, wave, morefs, include_failed, folders, folder_exact)
        if limit:
            rows = rows[:limit]
        records = [record_from_row(r) for r in rows]
        if not records:
            return []
        rid = run_id or self.current_run_id()
        batches = plan_batches(
            records,
            self.cfg,
            rid,
            stage,
            commit_action=self.cfg.commit_action if stage == STAGE_IMPORT else None,
            name_salt=name_salt,
        )
        if write_manifests:
            self.write_manifests(batches)
        return batches

    def manifest_dir(self) -> Path:
        d = Path(self.cfg.workdir).expanduser() / "manifests"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_manifests(self, batches: Sequence[PlannedBatch]) -> None:
        out = self.manifest_dir()
        for b in batches:
            path = out / "{}-{}.yaml".format(b.namespace, b.name)
            path.write_text(b.yaml(), encoding="utf-8")

    def manifest_path(self, b: PlannedBatch) -> str:
        return str(self.manifest_dir() / "{}-{}.yaml".format(b.namespace, b.name))

    def current_run_id(self) -> str:
        rid = self.store.get_meta("run_id")
        if not rid:
            rid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            self.store.set_meta("run_id", rid)
        return rid

    def new_run_id(self) -> str:
        rid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.store.set_meta("run_id", rid)
        return rid

    # ----------------------------------------------------------- execution
    def execute(
        self,
        stage: str,
        waves: Optional[Sequence[int]] = None,
        limit: int = 0,
        morefs: Optional[Sequence[str]] = None,
        include_failed: bool = False,
        resume: bool = True,
        watch_only: bool = False,
        folders: Optional[Sequence[str]] = None,
        folder_exact: bool = False,
    ) -> Dict[str, Any]:
        """Apply and monitor batches, one wave at a time."""
        run_id = self.current_run_id()
        self._scope = (list(folders) if folders else None, folder_exact)
        target_waves = list(waves) if waves else self.store.waves()
        totals = {"applied": 0, "succeeded": 0, "failed": 0, "awaiting_commit": 0, "batches": 0}

        self._install_sigint()
        try:
            for wave in target_waves:
                if self._stopping:
                    break
                result = self._run_wave(
                    stage, wave, run_id, limit, morefs, include_failed, resume, watch_only
                )
                for key in totals:
                    totals[key] += result.get(key, 0)
                if result.get("aborted"):
                    raise AbortRun(result.get("abort_reason", "safety guard tripped"))
        finally:
            self._restore_sigint()
        return totals

    def _run_wave(
        self,
        stage: str,
        wave: int,
        run_id: str,
        limit: int,
        morefs: Optional[Sequence[str]],
        include_failed: bool,
        resume: bool,
        watch_only: bool,
    ) -> Dict[str, Any]:
        result = {"applied": 0, "succeeded": 0, "failed": 0, "awaiting_commit": 0,
                  "batches": 0, "aborted": False, "abort_reason": ""}

        pending: List[PlannedBatch] = []
        in_flight: Dict[Tuple[str, str], Dict[str, Any]] = {}

        # Pick up anything this stage left in flight from an earlier invocation.
        if resume:
            for row in self.store.query_batches(states=[st.B_APPLIED, st.B_RUNNING],
                                                stage=stage, wave=wave):
                in_flight[(row["namespace"], row["name"])] = {
                    "applied_at": time.time(), "resumed": True}
                self.log("  resuming in-flight batch {}/{}".format(row["namespace"], row["name"]))

        if not watch_only and not self._stopping:
            scope_folders, scope_exact = getattr(self, "_scope", (None, False))
            pending = self.plan(stage, wave=wave, morefs=morefs, limit=limit,
                                include_failed=include_failed, run_id=run_id,
                                folders=scope_folders, folder_exact=scope_exact)
            for note in self.holdbacks:
                self.log("  ~ " + note)
                self.store.log_event(note, "warn")
            if self.cfg.max_vms_per_run:
                capped: List[PlannedBatch] = []
                total = 0
                for b in pending:
                    if total + b.size > self.cfg.max_vms_per_run:
                        break
                    capped.append(b)
                    total += b.size
                if len(capped) != len(pending):
                    self.log("  max_vms_per_run={} caps this wave at {} VMs in {} batches".format(
                        self.cfg.max_vms_per_run, total, len(capped)))
                pending = capped

        if not pending and not in_flight:
            return result

        self.log("\n=== wave {} / {} : {} batch(es), {} VM(s) ===".format(
            wave, stage, len(pending), sum(b.size for b in pending)))

        if self.kube.dry_run:
            # Render and report only. Nothing is applied, so there is nothing to
            # poll and no VM state may change -- a dry run must leave the
            # campaign exactly as it found it.
            for b in pending:
                self.log("  [dry-run] {} -> {} ({} VMs): {}".format(
                    b.name, b.namespace, b.size, ", ".join(r.moref for r in b.records)))
            self.log("  [dry-run] {} batch(es) rendered to {}; nothing applied".format(
                len(pending), self.manifest_dir()))
            result["batches"] = len(pending)
            return result

        for b in pending:
            self.store.create_batch(b.name, b.namespace, run_id, b.wave, stage, b.size,
                                    self.manifest_path(b))

        queue = list(pending)
        wave_results = {"ok": 0, "bad": 0}

        while queue or in_flight:
            # --- top up in-flight batches ---------------------------------
            while queue and not self._stopping and len(in_flight) < self.cfg.max_parallel_batches:
                nxt = self._next_applyable(queue, in_flight)
                if nxt is None:
                    break
                if self.deadline is not None:
                    left = self.deadline - time.time()
                    if left < max(1.0, self.deadline_batch_s):
                        msg = ("change window closes in {:.0f} min; a batch takes about {:.0f} min, "
                               "so no more batches are started ({} left for the next window)").format(
                            max(0, left) / 60, self.deadline_batch_s / 60, len(queue))
                        self.log("\n~ " + msg)
                        self.store.log_event(msg, "warn")
                        self.hit_deadline = True
                        self._stopping = True
                        break
                queue.remove(nxt)
                try:
                    self._apply_batch(nxt, stage)
                except KubectlError as exc:
                    self.store.set_batch_state(nxt.name, nxt.namespace, st.B_FAILED, str(exc)[:900])
                    self.store.log_event("apply failed: {}".format(exc)[:900], "error", batch=nxt.name)
                    self.log("  ! apply failed for {}: {}".format(nxt.name, str(exc)[:300]))
                    for rec in nxt.records:
                        self._fail_vm(rec.moref, stage, "batch apply failed: {}".format(exc)[:400])
                        wave_results["bad"] += 1
                        result["failed"] += 1
                    continue
                in_flight[(nxt.namespace, nxt.name)] = {"applied_at": time.time(), "batch": nxt}
                result["applied"] += nxt.size
                result["batches"] += 1
                if self.cfg.settle_seconds and queue:
                    time.sleep(self.cfg.settle_seconds)

            if not in_flight:
                if self._stopping:
                    break
                continue

            time.sleep(self.cfg.poll_interval_seconds)

            # --- poll -----------------------------------------------------
            try:
                finished = self._poll(in_flight, stage, run_id, wave_results, result)
            except KubectlError as exc:
                self.log("  ! poll error (will retry): {}".format(str(exc)[:300]))
                continue
            for key in finished:
                in_flight.pop(key, None)

            # --- circuit breaker -------------------------------------------
            done = wave_results["ok"] + wave_results["bad"]
            if done >= self.cfg.failure_rate_min_sample:
                rate = wave_results["bad"] / done
                if rate >= self.cfg.failure_rate_abort:
                    result["aborted"] = True
                    result["abort_reason"] = (
                        "wave {}: {}/{} operations failed ({:.0%} >= failure_rate_abort {:.0%}); "
                        "no further batches will be applied".format(
                            wave, wave_results["bad"], done, rate, self.cfg.failure_rate_abort)
                    )
                    self.store.log_event(result["abort_reason"], "error")
                    self.log("\n!! " + result["abort_reason"])
                    queue.clear()
                    self._stopping = True

            self._print_progress(wave, stage, in_flight, queue, wave_results)

        return result

    def _next_applyable(
        self,
        queue: List[PlannedBatch],
        in_flight: Dict[Tuple[str, str], Dict[str, Any]],
    ) -> Optional[PlannedBatch]:
        per_ns: Dict[str, int] = {}
        for ns, _name in in_flight:
            per_ns[ns] = per_ns.get(ns, 0) + 1
        for b in queue:
            if per_ns.get(b.namespace, 0) < self.cfg.max_parallel_batches_per_namespace:
                return b
        return None

    def _apply_batch(self, b: PlannedBatch, stage: str) -> None:
        self.log("  -> applying {} ({} VMs) in {}".format(b.name, b.size, b.namespace))
        self.kube.apply(b.yaml(), b.namespace)
        self.store.set_batch_state(b.name, b.namespace, st.B_APPLIED)
        self.store.log_event("applied {} VMs".format(b.size), batch=b.name)
        ops = b.manifest["spec"]["operations"]
        new_state = st.S_PRECHECK_RUNNING if stage == STAGE_PRECHECK else st.S_IMPORTING
        for rec, op in zip(b.records, ops):
            kwargs = {"operation_name": op["name"], "phase": "Applied", "stage": stage}
            if stage == STAGE_PRECHECK:
                kwargs["precheck_batch"] = b.name
            else:
                kwargs["batch_name"] = b.name
                kwargs["bump_attempts"] = True
            self.store.set_vm_state(rec.moref, new_state, **kwargs)

    # --------------------------------------------------------------- polling
    def _fetch_namespace(self, namespace: str, run_id: Optional[str]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        selector = "{}={}".format(LABEL_RUN, run_id) if run_id else None
        listing = self.kube.get_json(
            self.cfg.batch_resource, namespace=namespace, selector=selector, check=False
        ) or {}
        objs = {(o.get("metadata") or {}).get("name"): o for o in listing.get("items", [])}
        children: List[Dict[str, Any]] = []
        if self.cfg.operation_resource in (self.kube.api_resources() or {}):
            child_listing = self.kube.get_json(
                self.cfg.operation_resource, namespace=namespace, check=False
            ) or {}
            children = child_listing.get("items", [])
        return objs, children

    def _poll(
        self,
        in_flight: Dict[Tuple[str, str], Dict[str, Any]],
        stage: str,
        run_id: str,
        wave_results: Dict[str, int],
        result: Dict[str, Any],
    ) -> List[Tuple[str, str]]:
        finished: List[Tuple[str, str]] = []
        namespaces = sorted({ns for ns, _ in in_flight})
        cache: Dict[str, Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}
        for ns in namespaces:
            cache[ns] = self._fetch_namespace(ns, run_id)

        for (ns, name), meta in list(in_flight.items()):
            objs, children = cache.get(ns, ({}, []))
            obj = objs.get(name)
            if obj is None:
                # Applied but not yet visible, or label selector missed it.
                obj = self.kube.get_json(self.cfg.batch_resource, name, namespace=ns, check=False)
                if obj is None:
                    age = time.time() - meta.get("applied_at", time.time())
                    if age > 120:
                        self._batch_vanished(ns, name, stage, "{:.0f}s after apply".format(age))
                        finished.append((ns, name))
                    continue

            bstatus = batch_status(obj, self.cfg, children, stage=stage)
            self.store.set_batch_state(
                name, ns,
                st.B_RUNNING if bstatus.bucket == BUCKET_RUNNING else st.B_APPLIED,
                bstatus.message,
            )
            done = self._apply_status_to_vms(ns, name, stage, bstatus, wave_results, result)

            if done:
                finished.append((ns, name))
                continue

            timeout_s = self.cfg.batch_timeout_minutes * 60
            if time.time() - meta.get("applied_at", time.time()) > timeout_s:
                msg = "batch exceeded batch_timeout_minutes={}; VMs left in flight for manual review".format(
                    self.cfg.batch_timeout_minutes)
                self.store.set_batch_state(name, ns, st.B_TIMEDOUT, msg)
                self.store.log_event(msg, "warn", batch=name)
                self.log("  ! {} timed out: {}".format(name, msg))
                finished.append((ns, name))
        return finished

    def _apply_status_to_vms(
        self,
        namespace: str,
        batch_name: str,
        stage: str,
        bstatus: BatchStatus,
        wave_results: Dict[str, int],
        result: Dict[str, Any],
    ) -> bool:
        """Update per-VM state from a batch status. Returns True when the batch is done."""
        rows = self.store.query_vms(batch_name=batch_name)
        rows = [r for r in rows if r["namespace"] == namespace]
        if not rows:
            return bstatus.bucket in (BUCKET_SUCCEEDED, BUCKET_FAILED)

        by_moref = bstatus.by_moref()
        by_name = bstatus.by_op_name()
        still_running = False

        for row in rows:
            if row["state"] in st.TERMINAL_STATES:
                continue
            was_waiting = row["state"] == st.S_AWAITING_COMMIT
            op = by_moref.get(row["moref"])
            if op is None and row["operation_name"]:
                op = by_name.get(row["operation_name"])
            if op is not None and op.bucket == BUCKET_UNKNOWN:
                # The child said something we cannot read; the batch's own
                # verdict is a better guide than "keep waiting forever".
                op = None
            bucket = op.bucket if op else bstatus.bucket
            phase = (op.phase if op else bstatus.phase) or ""
            message = (op.message if op else bstatus.message) or ""

            if bucket == BUCKET_SUCCEEDED:
                if stage == STAGE_PRECHECK:
                    self.store.set_vm_state(row["moref"], st.S_PRECHECK_PASSED,
                                            phase=phase, message=message, stage=stage)
                else:
                    self.store.set_vm_state(row["moref"], st.S_COMMITTED,
                                            phase=phase, message=message, stage=stage,
                                            target_resource=(op.target if op else None))
                wave_results["ok"] += 1
                result["succeeded"] += 1
            elif bucket == BUCKET_FAILED:
                self._fail_vm(row["moref"], stage, message or phase or "operation failed", phase)
                wave_results["bad"] += 1
                result["failed"] += 1
            elif bucket == BUCKET_ROLLED_BACK:
                # The operator reverted this VM to vCenter ownership -- either
                # because rollbackAction was set at creation or requested later.
                self.store.set_vm_state(row["moref"], st.S_ROLLED_BACK, stage="rollback",
                                        phase=phase, message=message or "reverted to vCenter")
            elif bucket == BUCKET_AWAITING_COMMIT:
                if stage == STAGE_PRECHECK:
                    # A precheck has nothing to commit; treat this as still working.
                    still_running = True
                    continue
                if not was_waiting:
                    self.store.set_vm_state(row["moref"], st.S_AWAITING_COMMIT,
                                            phase=phase, message=message, stage=stage,
                                            target_resource=(op.target if op else None))
                    result["awaiting_commit"] += 1
            else:
                still_running = True
                # Nothing final yet, but say *why* it is waiting: a stalled
                # precheck with a DNS error in its message should not look
                # identical to one that is simply slow.
                if (phase or message) and (row["last_phase"] != phase or row["message"] != message):
                    self.store.set_vm_state(row["moref"], row["state"], stage=stage,
                                            phase=phase or None, message=message or None)

        if still_running:
            return False

        remaining = [r for r in self.store.query_vms(batch_name=batch_name)
                     if r["namespace"] == namespace
                     and r["state"] in (st.S_PRECHECK_RUNNING, st.S_IMPORTING, st.S_ROLLING_BACK)]
        if remaining:
            return False

        final_rows = [r for r in self.store.query_vms(batch_name=batch_name)
                      if r["namespace"] == namespace]
        waiting = [r for r in final_rows if r["state"] == st.S_AWAITING_COMMIT]
        if waiting:
            # Held by commitAction: Wait. Stop tracking it in this run -- a human
            # has to release it -- but leave the batch in a re-pollable state so
            # `commit` and `refresh` can finish the job later.
            msg = "{} VM(s) awaiting commit approval".format(len(waiting))
            self.store.set_batch_state(batch_name, namespace, st.B_APPLIED, msg)
            self.log("  || {} {}".format(batch_name, msg))
            return True

        bad = sum(1 for r in final_rows if r["state"] in (st.S_FAILED, st.S_PRECHECK_FAILED))
        reverted = sum(1 for r in final_rows if r["state"] == st.S_ROLLED_BACK)
        done_ok = sum(1 for r in final_rows if r["state"] == st.S_COMMITTED)
        if reverted and reverted + done_ok == len(final_rows):
            batch_state = st.B_ROLLED_BACK
        elif bad == 0:
            batch_state = st.B_SUCCEEDED
        elif bad == len(final_rows):
            batch_state = st.B_FAILED
        else:
            batch_state = st.B_PARTIAL
        self.store.set_batch_state(batch_name, namespace, batch_state, bstatus.message)
        self.log("  <- {} {} ({} of {} ok)".format(
            batch_name, batch_state, len(final_rows) - bad, len(final_rows)))
        return True

    def _batch_vanished(self, ns: str, name: str, stage: str, when: str) -> None:
        """A batch we applied is no longer on the cluster. Do not spin until timeout."""
        msg = ("batch {} disappeared from the cluster {} -- deleted outside the tool? "
               "Verify the VM in vCenter, then `retry`").format(name, when)
        self.store.set_batch_state(name, ns, st.B_DELETED, msg)
        self.store.log_event(msg, "error", batch=name)
        self.log("  ! {}/{}: {}".format(ns, name, msg))
        for vm in self.store.query_vms(batch_name=name):
            if vm["namespace"] == ns and vm["state"] in (
                    st.S_PRECHECK_RUNNING, st.S_IMPORTING, st.S_ROLLING_BACK, st.S_AWAITING_COMMIT):
                self._fail_vm(vm["moref"], stage, msg, "BatchGone")

    def abandon_targets(
        self,
        batch_names: Optional[Sequence[str]] = None,
        morefs: Optional[Sequence[str]] = None,
        folders: Optional[Sequence[str]] = None,
        folder_exact: bool = False,
        stage: Optional[str] = None,
    ) -> Tuple[List[sqlite3.Row], List[str]]:
        """Batches that can be discarded without a rollback.

        Safe: precheck-only batches (they migrate nothing), and import batches
        whose every VM is already terminal or reverted. An import batch with a
        VM still importing or held at the commit gate is refused -- that needs
        `rollback` first, because deleting it would strand the VM's ownership.
        """
        notes: List[str] = []
        by_key: Dict[Tuple[str, str], sqlite3.Row] = {}
        all_batches = [b for b in self.store.query_batches() if b["state"] != st.B_DELETED]

        wanted: List[sqlite3.Row] = []
        for name in batch_names or []:
            hits = [b for b in all_batches if b["name"] == name]
            if not hits:
                notes.append("{}: not found in run state (or already deleted)".format(name))
            wanted += hits
        vm_rows: List[sqlite3.Row] = []
        if morefs:
            vm_rows += self.store.query_vms(morefs=morefs)
        if folders:
            vm_rows += self.store.query_vms(folders=folders, folder_exact=folder_exact)
        for vm in vm_rows:
            # The batch the VM is *currently* in, not every batch it ever touched.
            if vm["state"] in (st.S_PRECHECK_RUNNING, st.S_PRECHECK_PASSED, st.S_PRECHECK_FAILED):
                col = "precheck_batch"
            elif vm["state"] == st.S_PENDING:
                col = "precheck_batch" if vm["precheck_batch"] else "batch_name"
            else:
                col = "batch_name"
            if not vm[col]:
                notes.append("{} ({}): is {} and in no batch".format(
                    vm["moref"], vm["vm_name"], vm["state"]))
                continue
            wanted += [b for b in all_batches
                       if b["name"] == vm[col] and b["namespace"] == vm["namespace"]]
        if stage:
            wanted += [b for b in all_batches if b["stage"] == stage
                       and b["state"] in (st.B_APPLIED, st.B_RUNNING, st.B_TIMEDOUT, st.B_PLANNED)]

        for row in wanted:
            key = (row["namespace"], row["name"])
            if key in by_key:
                continue
            members = [v for v in self.store.query_vms(batch_name=row["name"])
                       if v["namespace"] == row["namespace"]]
            live = [v for v in members if v["state"] in (
                st.S_IMPORTING, st.S_AWAITING_COMMIT, st.S_ROLLING_BACK)]
            if row["stage"] != STAGE_PRECHECK and live:
                notes.append(
                    "{}: refused -- {} VM(s) are {} (ownership is with the Supervisor); "
                    "run `rollback --batch {}` first".format(
                        row["name"], len(live),
                        "/".join(sorted({v["state"] for v in live})), row["name"]))
                continue
            by_key[key] = row
        return list(by_key.values()), notes

    def abandon(self, batches: Sequence[sqlite3.Row]) -> Dict[str, Any]:
        """Delete discarded batches and return their VMs to the queue."""
        result: Dict[str, Any] = {"deleted": [], "requeued": 0, "errors": []}
        for row in batches:
            ns, name = row["namespace"], row["name"]
            try:
                self.kube.delete(self.cfg.batch_resource, name, ns)
            except KubectlError as exc:
                result["errors"].append("{}/{}: {}".format(ns, name, str(exc)[:300]))
                continue
            if not self.kube.dry_run and self.kube.exists(self.cfg.batch_resource, name, ns):
                result["errors"].append(
                    "{}/{}: still present after delete (finalizer pending) -- wait, or see "
                    "`kubectl get importoperationbatch {} -n {} -o yaml`".format(ns, name, name, ns))
            self.store.set_batch_state(name, ns, st.B_DELETED, "abandoned")
            self.store.log_event("batch abandoned and deleted", "warn", batch=name)
            result["deleted"].append(name)
            for vm in self.store.query_vms(batch_name=name):
                if vm["namespace"] != ns:
                    continue
                if vm["state"] in (st.S_PRECHECK_RUNNING, st.S_PRECHECK_PASSED,
                                   st.S_PRECHECK_FAILED, st.S_FAILED, st.S_ROLLED_BACK):
                    self.store.set_vm_state(vm["moref"], st.S_PENDING, stage="abandon",
                                            message="batch {} abandoned".format(name),
                                            batch_name="", precheck_batch="")
                    result["requeued"] += 1
            self.log("  x  abandoned {}/{}".format(ns, name))
        return result

    def _fail_vm(self, moref: str, stage: str, message: str, phase: Optional[str] = None) -> None:
        state = st.S_PRECHECK_FAILED if stage == STAGE_PRECHECK else st.S_FAILED
        self.store.set_vm_state(moref, state, message=message[:900], phase=phase, stage=stage)
        self.store.log_event(message[:900], "error", moref=moref)

    def _print_progress(self, wave, stage, in_flight, queue, wave_results) -> None:
        self.log("  [wave {} {}] in-flight {} | queued {} | ok {} | failed {}".format(
            wave, stage, len(in_flight), len(queue), wave_results["ok"], wave_results["bad"]))

    # -------------------------------------------------------------- refresh
    def refresh(self, run_id: Optional[str] = None) -> Dict[str, int]:
        """Re-poll every non-terminal batch once and update state."""
        rid = run_id or self.current_run_id()
        batches = self.store.query_batches(
            states=[st.B_APPLIED, st.B_RUNNING, st.B_TIMEDOUT, st.B_ROLLING_BACK])
        counts = {"batches": 0, "updated": 0}
        cache: Dict[str, Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}
        dummy_wave = {"ok": 0, "bad": 0}
        dummy_res = {"succeeded": 0, "failed": 0, "awaiting_commit": 0}
        for row in batches:
            ns = row["namespace"]
            if ns not in cache:
                cache[ns] = self._fetch_namespace(ns, None)
            objs, children = cache[ns]
            obj = objs.get(row["name"])
            if obj is None:
                applied = st._parse_ts(row["applied_at"]) if row["applied_at"] else None
                age = (datetime.now(timezone.utc) - applied).total_seconds() if applied else 1e9
                if age > 120:
                    self._batch_vanished(ns, row["name"], row["stage"], "during refresh")
                    counts["batches"] += 1
                continue
            bstatus = batch_status(obj, self.cfg, children, stage=row["stage"])
            self._apply_status_to_vms(ns, row["name"], row["stage"], bstatus, dummy_wave, dummy_res)
            counts["batches"] += 1
        counts["updated"] = dummy_wave["ok"] + dummy_wave["bad"] + dummy_res["awaiting_commit"]
        return counts

    # --------------------------------------------------------------- commit
    def commit(self, morefs: Optional[Sequence[str]] = None, wave: Optional[int] = None) -> Dict[str, Any]:
        """Release VMs held by commitAction: Wait.

        The gate lives on the batch, so we patch the batch's controlAction to
        Auto; the operator then commits every operation still waiting in it.
        """
        rows = self.store.query_vms(states=[st.S_AWAITING_COMMIT], wave=wave, morefs=morefs)
        targets: Dict[Tuple[str, str], List[str]] = {}
        for row in rows:
            if not row["batch_name"]:
                continue
            targets.setdefault((row["namespace"], row["batch_name"]), []).append(row["moref"])

        patched, errors = 0, []
        patch = {"spec": {"defaultSpec": {"controlAction": {"commitAction": "Auto"}}}}
        for (ns, batch), members in sorted(targets.items()):
            try:
                self.kube.patch(self.cfg.batch_resource, batch, ns, patch)
                patched += 1
                self.store.log_event(
                    "commit gate released for {} VM(s)".format(len(members)), batch=batch)
                self.log("  committed {}/{} ({} VMs)".format(ns, batch, len(members)))
            except KubectlError as exc:
                errors.append("{}/{}: {}".format(ns, batch, str(exc)[:300]))
        return {"batches": patched, "vms": len(rows), "errors": errors}

    # ------------------------------------------------------------- rollback
    # A failed import can leave a VM half-owned by the Supervisor. The recovery
    # documented for the Mobility Operator is: edit the ImportOperationBatch and
    # set controlAction.rollbackAction: Immediate, wait for the operator to hand
    # ownership back to vCenter, and only then delete the batch. This does the
    # same thing with a merge patch, and refuses to delete until the operator
    # has confirmed the revert.
    ROLLBACKABLE_STATES = (st.S_FAILED, st.S_AWAITING_COMMIT, st.S_IMPORTING)

    def rollback_targets(
        self,
        batch_names: Optional[Sequence[str]] = None,
        morefs: Optional[Sequence[str]] = None,
        failed_only: bool = False,
        wave: Optional[int] = None,
        folders: Optional[Sequence[str]] = None,
        folder_exact: bool = False,
    ) -> Tuple[List[sqlite3.Row], List[str]]:
        """Resolve which import batches a rollback request refers to.

        With `folders`, the batches holding rollback-able VMs from those folders
        are chosen. rollbackAction is batch-scoped, so if such a batch also holds
        rollback-able VMs from *outside* the folders, that is reported: they
        would be reverted too.
        """
        notes: List[str] = []
        by_key: Dict[Tuple[str, str], sqlite3.Row] = {}
        all_batches = self.store.query_batches()

        if folders:
            in_scope = self.store.query_vms(
                states=list(self.ROLLBACKABLE_STATES), wave=wave,
                folders=folders, folder_exact=folder_exact)
            if not in_scope:
                notes.append("no VM under {} is in a rollback-able state".format(
                    ", ".join(folders)))
            wanted_batches = {(v["namespace"], v["batch_name"]) for v in in_scope if v["batch_name"]}
            for row in all_batches:
                if (row["namespace"], row["name"]) not in wanted_batches:
                    continue
                outside = [v for v in self.store.query_vms(batch_name=row["name"],
                                                           states=list(self.ROLLBACKABLE_STATES))
                           if v["namespace"] == row["namespace"]
                           and not folder_matches(v["src_folder"] or "", folders, exact=folder_exact)]
                if outside:
                    notes.append(
                        "{}: also holds {} rollback-able VM(s) outside the folder scope "
                        "({}); rollback is per batch, so they will be reverted as well"
                        .format(row["name"], len(outside),
                                ", ".join(v["vm_name"] for v in outside[:4])
                                + (" ..." if len(outside) > 4 else "")))
                batch_names = list(batch_names or []) + [row["name"]]

        def add(row: sqlite3.Row) -> None:
            if row["stage"] == STAGE_PRECHECK:
                notes.append("{}: precheck batches migrate nothing; nothing to roll back"
                             .format(row["name"]))
                return
            if row["state"] == st.B_DELETED:
                notes.append("{}: already deleted".format(row["name"]))
                return
            by_key[(row["namespace"], row["name"])] = row

        for name in batch_names or []:
            matches = [b for b in all_batches if b["name"] == name]
            if not matches:
                notes.append("{}: not found in run state".format(name))
            for row in matches:
                add(row)

        for moref in morefs or []:
            vm = self.store.get_vm(moref)
            if vm is None:
                notes.append("{}: not in the inventory".format(moref))
                continue
            if vm["state"] == st.S_COMMITTED:
                notes.append("{} ({}): committed imports cannot be rolled back".format(
                    moref, vm["vm_name"]))
                continue
            if not vm["batch_name"]:
                notes.append("{} ({}): has no import batch".format(moref, vm["vm_name"]))
                continue
            for row in all_batches:
                if row["name"] == vm["batch_name"] and row["namespace"] == vm["namespace"]:
                    add(row)

        if failed_only:
            candidates = [b for b in all_batches
                          if b["state"] in (st.B_FAILED, st.B_PARTIAL, st.B_TIMEDOUT)
                          and (wave is None or b["wave"] == wave)]
            # Also batches whose VMs are failed even if the batch row says otherwise.
            for vm in self.store.query_vms(states=[st.S_FAILED, st.S_AWAITING_COMMIT], wave=wave):
                if vm["batch_name"]:
                    candidates += [b for b in all_batches
                                   if b["name"] == vm["batch_name"]
                                   and b["namespace"] == vm["namespace"]]
            for row in candidates:
                add(row)

        rows = list(by_key.values())
        if wave is not None:
            rows = [r for r in rows if r["wave"] == wave]
        return rows, notes

    def rollback(
        self,
        batches: Sequence[sqlite3.Row],
        action: str = "Immediate",
        wait: bool = True,
        delete: bool = False,
        timeout_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "patched": [], "reverted": 0, "still_committed": 0, "deleted": [],
            "pending": [], "errors": [],
        }
        patch = {"spec": {"defaultSpec": {"controlAction": {"rollbackAction": action}}}}
        tracked: List[sqlite3.Row] = []

        for row in batches:
            ns, name = row["namespace"], row["name"]
            if self._stopping:
                result["errors"].append("{}/{}: not rolled back -- stopped first".format(ns, name))
                continue
            members = [v for v in self.store.query_vms(batch_name=name) if v["namespace"] == ns]
            to_revert = [v for v in members if v["state"] in self.ROLLBACKABLE_STATES]
            committed = [v for v in members if v["state"] == st.S_COMMITTED]
            result["still_committed"] += len(committed)
            if not to_revert:
                result["errors"].append(
                    "{}/{}: no VM in a rollback-able state ({} committed, which is irreversible)"
                    .format(ns, name, len(committed)))
                continue
            try:
                self.kube.patch(self.cfg.batch_resource, name, ns, patch)
            except KubectlError as exc:
                if exc.is_not_found:
                    result["errors"].append(
                        "{}/{}: batch no longer exists on the cluster; VM ownership is unknown "
                        "-- check vCenter".format(ns, name))
                    self.store.set_batch_state(name, ns, st.B_DELETED,
                                               "gone from cluster before rollback")
                else:
                    result["errors"].append("{}/{}: {}".format(ns, name, str(exc)[:300]))
                continue

            result["patched"].append(name)
            self.store.set_batch_state(name, ns, st.B_ROLLING_BACK,
                                       "rollbackAction={} requested".format(action))
            self.store.log_event("rollbackAction={} requested for {} VM(s)".format(
                action, len(to_revert)), "warn", batch=name)
            for vm in to_revert:
                self.store.set_vm_state(vm["moref"], st.S_ROLLING_BACK, stage="rollback",
                                        phase="RollbackRequested",
                                        message="rollbackAction={} set on {}".format(action, name))
            self.log("  -> rollbackAction={} on {}/{} ({} VM(s) to revert, {} committed untouched)"
                     .format(action, ns, name, len(to_revert), len(committed)))
            tracked.append(row)

        if self.kube.dry_run or not tracked:
            return result

        if wait:
            self._wait_for_rollback(tracked, result, timeout_minutes)
        else:
            result["pending"] = [r["name"] for r in tracked]
            self.log("  not waiting; run `status --refresh` or `rollback --wait` to follow up")
            return result

        if delete:
            self._delete_rolled_back(tracked, result)
        return result

    def _wait_for_rollback(
        self,
        batches: Sequence[sqlite3.Row],
        result: Dict[str, Any],
        timeout_minutes: Optional[int],
    ) -> None:
        timeout_s = (timeout_minutes or self.cfg.rollback_timeout_minutes) * 60
        started = time.time()
        waiting = {(r["namespace"], r["name"]): r for r in batches}
        self.log("  waiting up to {}m for the operator to revert ownership...".format(
            int(timeout_s // 60)))

        while waiting and time.time() - started < timeout_s and not self._stopping:
            # Sleep in short steps so Stop answers within a second, not a poll interval.
            nap_until = time.time() + self.cfg.poll_interval_seconds
            while time.time() < nap_until and not self._stopping:
                time.sleep(min(1.0, max(0.0, nap_until - time.time())))
            if self._stopping:
                break
            cache: Dict[str, Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}
            for (ns, name), row in list(waiting.items()):
                if ns not in cache:
                    try:
                        cache[ns] = self._fetch_namespace(ns, None)
                    except KubectlError as exc:
                        self.log("  ! poll error (will retry): {}".format(str(exc)[:200]))
                        continue
                objs, children = cache[ns]
                obj = objs.get(name)
                if obj is None:
                    self.log("  ! {}/{} disappeared from the cluster during rollback".format(ns, name))
                    self.store.set_batch_state(name, ns, st.B_DELETED, "gone during rollback")
                    result["errors"].append(
                        "{}/{}: batch vanished mid-rollback; verify VM ownership in vCenter"
                        .format(ns, name))
                    waiting.pop((ns, name))
                    continue
                if self._apply_rollback_status(
                        ns, name, batch_status(obj, self.cfg, children, stage=STAGE_IMPORT)):
                    waiting.pop((ns, name))

        for (ns, name), row in waiting.items():
            left = [v for v in self.store.query_vms(batch_name=name)
                    if v["namespace"] == ns and v["state"] == st.S_ROLLING_BACK]
            if self._stopping:
                # Stop ends the wait, not the revert: rollbackAction is already on the
                # batch and the operator carries on. A refresh records the outcome.
                msg = ("stopped waiting; {} VM(s) still reverting on the cluster -- "
                       "refresh to record the outcome".format(len(left)))
            else:
                msg = "rollback not confirmed within {}m; {} VM(s) still reverting".format(
                    int(timeout_s // 60), len(left))
            self.store.set_batch_state(name, ns, st.B_ROLLING_BACK, msg)
            self.store.log_event(msg, "warn", batch=name)
            self.log("  ! {}/{}: {}".format(ns, name, msg))
            result["pending"].append(name)

        names = {(r["namespace"], r["name"]) for r in batches}
        result["reverted"] = sum(
            1 for v in self.store.query_vms(states=[st.S_ROLLED_BACK])
            if (v["namespace"], v["batch_name"]) in names)

    def _apply_rollback_status(self, ns: str, name: str, bstatus: BatchStatus) -> bool:
        """Update VMs from a rollback poll. True once nothing is still reverting."""
        rows = [v for v in self.store.query_vms(batch_name=name) if v["namespace"] == ns]
        by_moref = bstatus.by_moref()
        by_name = bstatus.by_op_name()
        still = 0
        for vm in rows:
            if vm["state"] != st.S_ROLLING_BACK:
                continue
            op = by_moref.get(vm["moref"]) or (by_name.get(vm["operation_name"])
                                                if vm["operation_name"] else None)
            bucket = op.bucket if op else bstatus.bucket
            phase = (op.phase if op else bstatus.phase) or ""
            message = (op.message if op else bstatus.message) or ""
            if bucket == BUCKET_ROLLED_BACK:
                self.store.set_vm_state(vm["moref"], st.S_ROLLED_BACK, stage="rollback",
                                        phase=phase, message=message or "reverted to vCenter")
                self.store.log_event("ownership reverted to vCenter", moref=vm["moref"], batch=name)
            elif bucket == BUCKET_FAILED and "rollback" in (phase + message).lower():
                # The operator tried to revert and could not: this VM needs a human.
                self.store.set_vm_state(vm["moref"], st.S_FAILED, stage="rollback",
                                        phase=phase, message="ROLLBACK FAILED: " + (message or phase))
                self.store.log_event("rollback failed: " + (message or phase), "error",
                                     moref=vm["moref"], batch=name)
            else:
                still += 1
        if still:
            return False
        outcome = [v for v in self.store.query_vms(batch_name=name) if v["namespace"] == ns]
        reverted = sum(1 for v in outcome if v["state"] == st.S_ROLLED_BACK)
        bad = sum(1 for v in outcome if v["state"] == st.S_FAILED)
        state = st.B_ROLLED_BACK if not bad else st.B_PARTIAL
        self.store.set_batch_state(name, ns, state,
                                   "{} reverted, {} rollback failures".format(reverted, bad))
        self.log("  <- {}/{}: {} VM(s) reverted to vCenter{}".format(
            ns, name, reverted, ", {} rollback FAILED".format(bad) if bad else ""))
        return True

    def _delete_rolled_back(self, batches: Sequence[sqlite3.Row], result: Dict[str, Any]) -> None:
        for row in batches:
            ns, name = row["namespace"], row["name"]
            current = self.store.get_batch(name, ns)
            if current is None or current["state"] != st.B_ROLLED_BACK:
                result["errors"].append(
                    "{}/{}: not deleted -- rollback is not confirmed complete ({})".format(
                        ns, name, current["state"] if current else "unknown"))
                continue
            try:
                self.kube.delete(self.cfg.batch_resource, name, ns)
            except KubectlError as exc:
                result["errors"].append("{}/{}: delete failed: {}".format(ns, name, str(exc)[:300]))
                continue
            self.store.set_batch_state(name, ns, st.B_DELETED, "deleted after confirmed rollback")
            self.store.log_event("batch deleted after rollback", batch=name)
            self.log("  x  deleted {}/{}".format(ns, name))
            result["deleted"].append(name)

    def cleanup_candidates(self) -> List[sqlite3.Row]:
        """Batches whose rollback the operator has confirmed, and so are safe to delete."""
        return self.store.query_batches(states=[st.B_ROLLED_BACK])

    def delete_batches(self, batches: Sequence[sqlite3.Row]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"deleted": [], "errors": []}
        self._delete_rolled_back(batches, result)
        return result

    # ---------------------------------------------------------------- retry
    def requeue(self, morefs: Optional[Sequence[str]] = None, wave: Optional[int] = None,
                include_precheck_failures: bool = True, force: bool = False,
                folders: Optional[Sequence[str]] = None, folder_exact: bool = False) -> int:
        """Return failed VMs to the queue so a later run picks them up again."""
        states = [st.S_FAILED, st.S_ROLLED_BACK]
        if include_precheck_failures:
            states.append(st.S_PRECHECK_FAILED)
        rows = self.store.query_vms(states=states, wave=wave, morefs=morefs,
                                    folders=folders, folder_exact=folder_exact)
        moved = 0
        for row in rows:
            if not force and row["attempts"] > self.cfg.max_retries:
                continue
            self.store.set_vm_state(row["moref"], st.S_PENDING, stage="retry",
                                    message="requeued after {}".format(row["state"]))
            moved += 1
        return moved
