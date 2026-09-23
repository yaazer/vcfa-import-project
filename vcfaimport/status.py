"""Interpreting Mobility Operator status.

The operator's status sub-resource is not fully documented and the API is still
at v1alpha3, so nothing here assumes a fixed schema. We look for per-VM signal
in three places, best first:

  1. child ImportOperation objects owned by the batch -- these carry
     spec.virtualMachineID, which maps to a VM unambiguously;
  2. an inline per-operation list on the batch's own status;
  3. the batch-level phase, applied to every VM in the batch.

Whatever phase string turns up is then classified into one of five buckets by
the configurable regex table in Config.phase_patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    BUCKET_AWAITING_COMMIT,
    BUCKET_FAILED,
    BUCKET_ROLLED_BACK,
    BUCKET_RUNNING,
    BUCKET_SUCCEEDED,
    BUCKET_UNKNOWN,
    Config,
)

# Keys that may hold a phase-like string on a status object.
PHASE_KEYS = ("phase", "state", "result", "importPhase", "migrationPhase", "operationState")
# Keys that may hold an inline per-operation list on the batch status.
OP_LIST_KEYS = (
    "operations", "operationStatuses", "operationStatus", "importOperations",
    "results", "operationResults", "items",
)
# Keys that may hold a human-readable explanation.
MESSAGE_KEYS = ("message", "reason", "error", "details", "lastError", "failureMessage")
NAME_KEYS = ("name", "operationName", "importOperation", "importOperationName", "operation")
VMID_KEYS = ("virtualMachineID", "virtualMachineId", "vmID", "vmId", "sourceVirtualMachineID")
# The namespace-scoped resource the operator creates for an imported VM. Its
# name is generated (vm-xxxyyyzzzz), so capturing it is the only way to tie the
# VCFA object back to the vCenter VM it came from.
TARGET_KEYS = (
    "targetVirtualMachine", "targetVirtualMachineName", "virtualMachineName",
    "importedVirtualMachine", "targetName", "vmName", "createdResource",
    "targetResource", "resourceName",
)


@dataclass
class OpStatus:
    key: str                      # moref if known, else operation name
    bucket: str
    phase: Optional[str] = None
    message: Optional[str] = None
    by_moref: bool = False
    target: Optional[str] = None  # the VCFA resource the import produced


@dataclass
class BatchStatus:
    bucket: str
    phase: Optional[str] = None
    message: Optional[str] = None
    operations: List[OpStatus] = field(default_factory=list)
    source: str = "batch"         # which of the three signals we used
    counts: Dict[str, int] = field(default_factory=dict)

    def by_moref(self) -> Dict[str, OpStatus]:
        return {o.key: o for o in self.operations if o.by_moref}

    def by_op_name(self) -> Dict[str, OpStatus]:
        return {o.key: o for o in self.operations if not o.by_moref}


def classify(phase: Optional[str], cfg: Config) -> str:
    if not phase:
        return BUCKET_UNKNOWN
    norm = re.sub(r"[\s_-]+", "", str(phase)).lower()
    for pattern, bucket in cfg.phase_patterns:
        if re.search(pattern, norm, re.IGNORECASE):
            return bucket
    # Also try the raw string, so patterns anchored on the original spelling work.
    for pattern, bucket in cfg.phase_patterns:
        if re.search(pattern, str(phase), re.IGNORECASE):
            return bucket
    return BUCKET_UNKNOWN


def _first_str(obj: Dict[str, Any], keys: Tuple[str, ...]) -> Optional[str]:
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def extract_phase(obj: Dict[str, Any], cfg: Config) -> Tuple[Optional[str], Optional[str]]:
    """Return (phase, message) for a resource or an inline operation entry."""
    if not isinstance(obj, dict):
        return None, None
    status = obj.get("status") if isinstance(obj.get("status"), dict) else obj

    phase = _first_str(status, PHASE_KEYS)
    message = _first_str(status, MESSAGE_KEYS)

    conditions = status.get("conditions")
    if isinstance(conditions, list) and conditions:
        # A True condition of a "success" type is the strongest signal there is.
        for want in cfg.condition_types:
            for cond in conditions:
                if not isinstance(cond, dict) or cond.get("type") != want:
                    continue
                cstatus = str(cond.get("status", "")).lower()
                cmsg = _first_str(cond, MESSAGE_KEYS)
                if cstatus == "true":
                    return (phase or want), (message or cmsg)
                if cstatus == "false":
                    reason = cond.get("reason") or "Failed"
                    return (phase or str(reason)), (message or cmsg or str(reason))
        if not phase:
            # No known type matched; fall back to the newest condition we can see.
            last = conditions[-1]
            if isinstance(last, dict):
                reason = last.get("reason") or last.get("type")
                phase = str(reason) if reason else None
                message = message or _first_str(last, MESSAGE_KEYS)

    return phase, message


def extract_target(obj: Dict[str, Any]) -> Optional[str]:
    """Best-effort: the namespace resource created for this import."""
    if not isinstance(obj, dict):
        return None
    for scope in (obj.get("status") if isinstance(obj.get("status"), dict) else None, obj):
        if not isinstance(scope, dict):
            continue
        found = _first_str(scope, TARGET_KEYS)
        if found:
            return found
        for key in ("target", "result", "imported", "destination"):
            nested = scope.get(key)
            if isinstance(nested, dict):
                found = _first_str(nested, TARGET_KEYS + ("name",))
                if found:
                    return found
    return None


def _extract_counts(status: Dict[str, Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for key in ("total", "succeeded", "failed", "running", "pending", "completed", "inProgress"):
        val = status.get(key)
        if isinstance(val, int):
            out[key] = val
    summary = status.get("summary")
    if isinstance(summary, dict):
        for key, val in summary.items():
            if isinstance(val, int):
                out[key] = val
    return out


def _inline_operations(batch_obj: Dict[str, Any], cfg: Config) -> List[OpStatus]:
    status = batch_obj.get("status")
    if not isinstance(status, dict):
        return []
    for key in OP_LIST_KEYS:
        raw = status.get(key)
        entries: List[Dict[str, Any]] = []
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            entries = raw
        elif isinstance(raw, dict) and raw:
            entries = [dict(v, **{"name": k}) for k, v in raw.items() if isinstance(v, dict)]
        if not entries:
            continue
        out: List[OpStatus] = []
        for entry in entries:
            phase, message = extract_phase(entry, cfg)
            vmid = _first_str(entry, VMID_KEYS)
            if not vmid and isinstance(entry.get("spec"), dict):
                vmid = _first_str(entry["spec"], VMID_KEYS)
            name = _first_str(entry, NAME_KEYS)
            if not (vmid or name):
                continue
            out.append(
                OpStatus(
                    key=vmid or name or "",
                    bucket=classify(phase, cfg),
                    phase=phase,
                    message=message,
                    by_moref=bool(vmid),
                    target=extract_target(entry),
                )
            )
        if out:
            return out
    return []


def _child_operations(children: List[Dict[str, Any]], batch_name: str, cfg: Config,
                      stage: Optional[str] = None) -> List[OpStatus]:
    """Per-VM status from the batch's ImportOperation children.

    A child carries the same ReadyForImport / ReadyForCommit / Complete
    conditions as its batch, so it is read with the same stage-aware rules;
    the generic phase heuristics are only the fallback. Seen in the lab: a
    precheck child with Complete=False and ReadyForImport=True is a *pass*.
    """
    out: List[OpStatus] = []
    for obj in children:
        meta = obj.get("metadata") or {}
        owners = meta.get("ownerReferences") or []
        owned = any(o.get("name") == batch_name for o in owners if isinstance(o, dict))
        labels = meta.get("labels") or {}
        if not owned and labels.get("mobility-operator.vmware.com/batch") != batch_name:
            # Last resort: operator implementations often prefix the child name.
            if not str(meta.get("name", "")).startswith(batch_name):
                continue
        spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
        vmid = _first_str(spec, VMID_KEYS) or _first_str(obj, VMID_KEYS)
        key = vmid or str(meta.get("name", ""))
        if not key:
            continue
        known = operator_status(obj, cfg, stage)
        if known is not None:
            bucket, phase, message = known.bucket, known.phase, known.message
        else:
            phase, message = extract_phase(obj, cfg)
            bucket = classify(phase, cfg)
        out.append(
            OpStatus(
                key=key,
                bucket=bucket,
                phase=phase,
                message=message,
                by_moref=bool(vmid),
                # Only a real target name; the operation's own name is not one.
                target=extract_target(obj),
            )
        )
    return out


# ---------------------------------------------------------------------------
# The Mobility Operator's actual vocabulary, as observed on VCF 9.1
# (mobility-operator.vmware.com/v1alpha3) in September 2026:
#
#   status.conditions[]  type: ReadyForImport | ReadyForCommit | Complete
#                        status: "True" when reached; "False" with
#                        reason: ObjectNotReady while the operator is still working
#                        message: e.g. "Operations are not ready. Operation Names: x"
#                                 or the underlying error (a DNS failure was seen here)
#   status.readyCount    number of operations that passed precheck
#   status.readyOps      their operation names
#
# A precheckOnly batch stops at ReadyForImport=True; ReadyForCommit and Complete
# stay False/ObjectNotReady for it forever, which is *success* for a precheck.
# A precheck-only batch does create ImportOperation children (2026-09-21); each
# child is named after its operation, with no batch-name prefix, so two batches
# naming the same operation fight over one object -- see render.batch_discriminator.
#
# The per-operation lists for failure/commit/rollback outcomes have not been
# observed yet; the names below are educated guesses and are tried in order.
COND_READY_IMPORT = "ReadyForImport"
COND_READY_COMMIT = "ReadyForCommit"
COND_COMPLETE = "Complete"
# A batch says "Complete"; its children say "Completed" for the same thing
# (observed 2026-09-23). Reading only the batch spelling left committed VMs
# stuck at awaiting_commit forever, because child status wins over the batch.
COND_COMPLETED_ALT = "Completed"
COMPLETE_CONDITIONS = (COND_COMPLETE, COND_COMPLETED_ALT)
# Children report their precheck verdict here; batches have no equivalent and
# express a passed precheck as ReadyForImport: True.
COND_PRECHECK_OK = "PrecheckSucceeded"
OPERATOR_CONDITIONS = (COND_READY_IMPORT, COND_READY_COMMIT, COND_COMPLETE,
                       COND_COMPLETED_ALT, COND_PRECHECK_OK)
REASON_WORKING = ("objectnotready", "notready", "inprogress", "pending", "reconciling")

READY_OPS_KEYS = ("readyOps", "readyOperations")
DONE_OPS_KEYS = ("completedOps", "completeOps", "committedOps", "importedOps",
                 "succeededOps", "completedOperations")
FAILED_OPS_KEYS = ("failedOps", "failedOperations", "errorOps", "notReadyOps",
                   "unreadyOps", "failures")
ROLLED_BACK_OPS_KEYS = ("rolledBackOps", "rollbackOps", "revertedOps", "rolledBackOperations")


def _condition_map(status: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for cond in status.get("conditions") or []:
        if isinstance(cond, dict) and cond.get("type"):
            out[str(cond["type"])] = cond
    return out


def _first_cond(conds: Dict[str, Dict[str, Any]],
                types: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    """The first of these condition types the object actually carries."""
    for name in types:
        if name in conds:
            return conds[name]
    return None


def _names(status: Dict[str, Any], keys: Tuple[str, ...]) -> List[str]:
    for key in keys:
        raw = status.get(key)
        if isinstance(raw, list):
            return [str(x.get("name") if isinstance(x, dict) else x) for x in raw]
    return []


def _is_true(cond: Optional[Dict[str, Any]]) -> bool:
    return bool(cond) and str(cond.get("status", "")).lower() == "true"


def _precheck_only(batch_obj: Dict[str, Any]) -> bool:
    spec = batch_obj.get("spec") if isinstance(batch_obj.get("spec"), dict) else {}
    control = ((spec.get("defaultSpec") or {}).get("controlAction") or {})
    return bool(control.get("precheckOnly"))


def operator_status(
    batch_obj: Dict[str, Any],
    cfg: Config,
    stage: Optional[str] = None,
) -> Optional[BatchStatus]:
    """Interpret the operator's own condition vocabulary. None if not present."""
    status = batch_obj.get("status") if isinstance(batch_obj.get("status"), dict) else {}
    conds = _condition_map(status)
    if not any(t in conds for t in OPERATOR_CONDITIONS):
        return None

    precheck = stage == "precheck" if stage else _precheck_only(batch_obj)
    ready_import = conds.get(COND_READY_IMPORT)
    ready_commit = conds.get(COND_READY_COMMIT)
    complete = _first_cond(conds, COMPLETE_CONDITIONS)
    precheck_ok = conds.get(COND_PRECHECK_OK)

    def reason_of(cond: Optional[Dict[str, Any]]) -> str:
        return str((cond or {}).get("reason") or "")

    def message_of(cond: Optional[Dict[str, Any]]) -> Optional[str]:
        return _first_str(cond or {}, MESSAGE_KEYS)

    def working(cond: Optional[Dict[str, Any]]) -> bool:
        """False-with-a-transient-reason, or absent: the operator is still busy."""
        if not cond or _is_true(cond):
            return not cond
        r = reason_of(cond).replace(" ", "").replace("_", "").lower()
        return r in REASON_WORKING or r == ""

    def verdict(cond: Optional[Dict[str, Any]]) -> bool:
        """A False condition carrying a real reason: the operator has decided."""
        return bool(cond) and not _is_true(cond) and not working(cond)

    # --- batch-level verdict -------------------------------------------------
    if precheck and precheck_ok is not None:
        # A child ImportOperation states its precheck verdict outright. Without
        # this, a failed precheck child (PrecheckSucceeded: False, reason e.g.
        # VirtualMachineAlreadyExists) fell through to the phase heuristics,
        # read as "still running", and the batch sat until batch_timeout_minutes.
        if _is_true(precheck_ok):
            bucket, phase, message = BUCKET_SUCCEEDED, COND_PRECHECK_OK, message_of(precheck_ok)
        else:
            phase = reason_of(precheck_ok) or COND_PRECHECK_OK
            bucket = BUCKET_RUNNING if working(precheck_ok) else classify(phase, cfg)
            if bucket == BUCKET_UNKNOWN:
                bucket = BUCKET_FAILED
            message = message_of(precheck_ok)
    elif precheck:
        if _is_true(ready_import):
            bucket, phase, message = BUCKET_SUCCEEDED, COND_READY_IMPORT, message_of(ready_import)
        elif not verdict(ready_import):
            bucket = BUCKET_RUNNING
            phase = reason_of(ready_import) or "ObjectNotReady"
            message = message_of(ready_import)
        else:
            phase = reason_of(ready_import)
            bucket = classify(phase, cfg)
            if bucket == BUCKET_UNKNOWN:
                bucket = BUCKET_FAILED   # a named non-working reason on a precheck is a verdict
            message = message_of(ready_import)
    else:
        if _is_true(complete):
            bucket, phase, message = BUCKET_SUCCEEDED, COND_COMPLETE, message_of(complete)
        elif _is_true(ready_commit):
            bucket, phase, message = BUCKET_AWAITING_COMMIT, COND_READY_COMMIT, message_of(ready_commit)
        else:
            # Whichever gate is furthest along tells us where the batch is.
            gate = (complete if verdict(complete)
                    else ready_commit if verdict(ready_commit)
                    else ready_import if verdict(ready_import)
                    else None)
            if gate is not None:
                phase = reason_of(gate)
                bucket = classify(phase, cfg)
                if bucket == BUCKET_UNKNOWN:
                    bucket = BUCKET_FAILED
                message = message_of(gate)
            else:
                bucket = BUCKET_RUNNING
                busiest = ready_commit if _is_true(ready_import) else ready_import
                phase = ("ImportedAwaitingCommit" if _is_true(ready_import) and not _is_true(ready_commit)
                         else reason_of(busiest) or "ObjectNotReady")
                message = message_of(busiest) or message_of(ready_import)

    # --- per-operation detail from the name lists -----------------------------
    ready = set(_names(status, READY_OPS_KEYS))
    done = set(_names(status, DONE_OPS_KEYS))
    failed = set(_names(status, FAILED_OPS_KEYS))
    reverted = set(_names(status, ROLLED_BACK_OPS_KEYS))
    ops: List[OpStatus] = []
    for name in sorted(ready | done | failed | reverted):
        if name in reverted:
            b, p = BUCKET_ROLLED_BACK, "RolledBack"
        elif name in failed:
            b, p = BUCKET_FAILED, "Failed"
        elif precheck and name in ready:
            b, p = BUCKET_SUCCEEDED, COND_READY_IMPORT
        elif name in done:
            b, p = BUCKET_SUCCEEDED, COND_COMPLETE
        elif name in ready and _is_true(ready_commit):
            b, p = BUCKET_AWAITING_COMMIT, COND_READY_COMMIT
        else:
            b, p = BUCKET_RUNNING, "Ready" if name in ready else "Working"
        ops.append(OpStatus(key=name, bucket=b, phase=p,
                            message=message if b in (BUCKET_FAILED, BUCKET_RUNNING) else None,
                            by_moref=False))

    counts = _extract_counts(status)
    if isinstance(status.get("readyCount"), int):
        counts["ready"] = status["readyCount"]
    return BatchStatus(bucket=bucket, phase=phase, message=message, operations=ops,
                       source="operator-conditions", counts=counts)


def batch_status(
    batch_obj: Dict[str, Any],
    cfg: Config,
    children: Optional[List[Dict[str, Any]]] = None,
    stage: Optional[str] = None,
) -> BatchStatus:
    """Build a normalised view of one ImportOperationBatch.

    The operator's own condition vocabulary is used when present (see
    operator_status); the generic phase/condition heuristics below are the
    fallback for anything else. Child ImportOperations, when they exist, add
    per-VM detail keyed by moref on top of either.
    """
    name = (batch_obj.get("metadata") or {}).get("name", "")
    eff_stage = stage or ("precheck" if _precheck_only(batch_obj) else "import")
    known = operator_status(batch_obj, cfg, stage)
    if known is not None:
        if children:
            child_ops = _child_operations(children, name, cfg, eff_stage)
            if child_ops:
                # Children are keyed by moref, which beats operation names.
                # by_moref() / by_op_name() keep the two keyed views apart.
                known.operations = child_ops + known.operations
                known.source = "importoperations+conditions"
        return known

    phase, message = extract_phase(batch_obj, cfg)
    bucket = classify(phase, cfg)
    status_obj = batch_obj.get("status") if isinstance(batch_obj.get("status"), dict) else {}
    counts = _extract_counts(status_obj or {})

    ops: List[OpStatus] = []
    source = "batch"
    if children:
        ops = _child_operations(children, name, cfg, eff_stage)
        if ops:
            source = "importoperations"
    if not ops:
        ops = _inline_operations(batch_obj, cfg)
        if ops:
            source = "status.operations"

    # A batch with no status at all has almost certainly only just been accepted.
    if bucket == BUCKET_UNKNOWN and not status_obj:
        bucket = BUCKET_RUNNING
        phase = phase or "Accepted"

    # Derive the batch verdict from its operations when the batch itself is quiet.
    if ops and bucket in (BUCKET_UNKNOWN, BUCKET_RUNNING):
        buckets = [o.bucket for o in ops]
        if all(b == BUCKET_SUCCEEDED for b in buckets):
            bucket = BUCKET_SUCCEEDED
        elif all(b in (BUCKET_SUCCEEDED, BUCKET_FAILED) for b in buckets):
            bucket = BUCKET_FAILED if BUCKET_FAILED in buckets else BUCKET_SUCCEEDED
        elif all(b in (BUCKET_SUCCEEDED, BUCKET_FAILED, BUCKET_AWAITING_COMMIT) for b in buckets):
            bucket = BUCKET_AWAITING_COMMIT
        else:
            bucket = BUCKET_RUNNING

    return BatchStatus(
        bucket=bucket,
        phase=phase,
        message=message,
        operations=ops,
        source=source,
        counts=counts,
    )
