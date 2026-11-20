"""Readiness: spot likely precheck failures from vCenter facts, before precheck.

These are heuristics over what discovery already read (power, VMware Tools,
network adapters, CD-ROMs, disks, hardware version). They are advisory: the
Mobility Operator's precheck remains the authority. A VM graded "blocked" is
only kept out of precheck when `readiness_exclude_blocked` is set.

Each rule: (code, level, title, advice, test). Levels: block (precheck is
expected to fail), warn (worth a look), info (context).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from .vcenter import storage_compliance_ok, storage_compliance_reason

BLOCK, WARN, INFO = "block", "warn", "info"
GRADES = ("ready", "warn", "block")


def _facts(row: Any) -> Dict[str, Any]:
    try:
        return json.loads(row["facts_json"] or "{}") or {}
    except (KeyError, IndexError, TypeError, ValueError):
        return {}


def _nics(row: Any) -> List[Dict[str, Any]]:
    try:
        return json.loads(row["nics_json"] or "[]") or []
    except (KeyError, IndexError, TypeError, ValueError):
        return []


def _on(row: Any) -> bool:
    return (row["power_state"] or "").upper() == "POWERED_ON"


def _tools_running(row: Any) -> bool:
    return "running" in (row["tools_status"] or "").lower() and "not" not in (row["tools_status"] or "").lower()


def _hw_version(facts: Dict[str, Any]) -> int:
    raw = str(facts.get("hw_version") or "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else 0


def _disks(facts: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    d = facts.get("disks")
    return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}


def _total_gb(facts: Dict[str, Any]) -> float:
    return sum(v.get("capacity_gb") or 0 for v in _disks(facts).values())


# The 63-char label / 253-char subdomain a Kubernetes object name must fit (RFC 1123).
_K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
LARGE_VM_GB = 2048        # a VM this big moves slowly; the batch timeout is one fixed number
MANY_DISKS = 8


def duplicate_names(rows: Sequence[Any]) -> set:
    """Lower-cased VM names that more than one discovered VM carries (different folders, datacenters, vCenters)."""
    seen: Dict[str, int] = {}
    for r in rows:
        name = (r["name"] or "").strip().lower()
        if name:
            seen[name] = seen.get(name, 0) + 1
    return {n for n, c in seen.items() if c > 1}


def context(rows: Sequence[Any]) -> Dict[str, Any]:
    """Facts about the whole inventory that single-VM rules need (today: names shared by several VMs)."""
    return {"dupes": duplicate_names(rows)}


Rule = Dict[str, Any]
RULES: List[Rule] = [
    {"code": "tools_not_running", "level": BLOCK, "title": "VMware Tools is not running",
     "advice": "The operator needs Tools in the guest; precheck will fail. Start Tools, then re-discover.",
     "test": lambda r, f: _on(r) and bool(r["tools_status"]) and not _tools_running(r)},
    {"code": "non_vmdk_disk", "level": BLOCK, "title": "A disk is not a plain VMDK",
     "advice": "RDM and other disk backings are not expected to import. Migrate the disk to a VMDK first.",
     "test": lambda r, f: any(t and t != "VMDK_FILE" for t in f.get("disk_backings") or [])},
    {"code": "powered_off", "level": WARN, "title": "Powered off",
     "advice": "Tools cannot report while the VM is off, and the import may need it running. Power it on or confirm it should move cold.",
     "test": lambda r, f: bool(r["power_state"]) and not _on(r)},
    {"code": "iso_connected", "level": BLOCK, "title": "A CD-ROM has an ISO attached",
     # The operator's precheck passes a connected ISO (seen 2026-09-25, a guest
     # mid-install): nothing downstream catches it, so it blocks here.
     "advice": "An OS install may still be running, and the ISO's datastore path may not exist on "
               "the target. Finish the install, disconnect the ISO, then re-discover.",
     "test": lambda r, f: bool(f.get("iso_connected"))},
    {"code": "nic_disconnected", "level": WARN, "title": "A network adapter is disconnected",
     "advice": "Check it is meant to be down; the import maps it to a subnet regardless.",
     "test": lambda r, f: any(s and s != "CONNECTED" for s in f.get("nic_states") or [])},
    {"code": "no_nics", "level": WARN, "title": "No network adapters",
     "advice": "Nothing to map to a subnet. Fine for isolated VMs; otherwise add an adapter first.",
     "test": lambda r, f: not _nics(r)},
    {"code": "old_hardware", "level": INFO, "title": "Old virtual hardware",
     "advice": "Hardware version below 10. Consider upgrading it before the move.",
     "test": lambda r, f: 0 < _hw_version(f) < 10},
    {"code": "no_ip", "level": INFO, "title": "No guest IP reported",
     "advice": "Post-import verification cannot ping it or compare its IP.",
     "test": lambda r, f: _on(r) and _tools_running(r) and not (r["ip"] if "ip" in r.keys() else "")},
    # ---- added 2026-11-20. Warn or info only: none of these was seen to fail a precheck, so none may block
    # (a new block would silently change what readiness_exclude_blocked keeps out). They read the disk and
    # device facts of the last discover; an older cache lacks them and these rules stay quiet rather than guess.
    {"code": "snapshot_disk", "level": WARN, "title": "The VM has a snapshot",
     "advice": "A disk is a snapshot delta (named like vm-000001.vmdk). Delta chains are a common reason a disk "
               "move fails: delete or consolidate the snapshots in vCenter and re-discover. The tool reads this from the "
               "disk file name; vCenter's REST API has no snapshot list, so a snapshot that left no delta disk is not seen.",
     "test": lambda r, f: any(d.get("delta") for d in _disks(f).values())},
    {"code": "legacy_devices", "level": WARN, "title": "Serial port, parallel port or floppy attached",
     "advice": "Legacy devices may not carry over to a Supervisor VM. Remove the ones the guest does not use, "
               "then re-discover.",
     "test": lambda r, f: sum(v for v in (f.get("devices") or {}).values() if isinstance(v, int)) > 0},
    {"code": "large_vm", "level": INFO, "title": "A large VM",
     "advice": "Over {} GB of disk, or {} or more disks. It will take long and the batch timeout is one fixed "
               "number: give it a batch of its own, or its own change window.".format(LARGE_VM_GB, MANY_DISKS),
     "test": lambda r, f: bool(_disks(f)) and (_total_gb(f) >= LARGE_VM_GB or len(_disks(f)) >= MANY_DISKS)},
    {"code": "k8s_name", "level": INFO, "title": "Name is not a valid Kubernetes name",
     "advice": "Uppercase letters, spaces, underscores or more than 253 characters. The manifest refers to the VM by its "
               "ID, so this may change nothing; whether the operator renames or rejects such a VM has not been observed.",
     "test": lambda r, f: bool((r["name"] or "").strip()) and not (len(r["name"]) <= 253 and _K8S_NAME.match(r["name"]))},
    {"code": "duplicate_name", "level": INFO, "title": "Another VM has the same name",
     "advice": "Same name in another folder, datacenter or vCenter. Names are not what the tool keys on (the moref is), "
               "but the imported VMs may collide in a namespace; check before importing both into one.",
     "ctx": True,
     "test": lambda r, f, ctx: (r["name"] or "").strip().lower() in (ctx or {}).get("dupes", ())},
    # Unlike every other rule here, this one is not advisory-only: `engine.py`'s
    # own eligible() refuses to import a VM this flags, unconditionally (the
    # user asked, 2026-11-16, that imports never happen "unless the VM's
    # storage policy is compliant" -- a deliberate departure from this
    # module's usual "advisory, the operator's precheck is the authority"
    # convention). It is still listed here too so `readiness`/the console
    # show it before precheck, not only at the import gate.
    {"code": "storage_noncompliant", "level": BLOCK, "title": "Storage policy not confirmed compliant",
     "advice": "vCenter's own storage-policy compliance check for this VM is not COMPLIANT or "
               "NOT_APPLICABLE -- or it has never been checked at all (an older discover, or one "
               "run with --no-storage-compliance). Call storage_compliance_detail(row) for which. "
               "The import gate refuses to move this VM until a fresh discover shows it compliant, "
               "or storage_compliance_enforce is set to false.",
     "test": lambda r, f: not storage_compliance_ok(f.get("storage_compliance"))},
    # Namespace-independent half of the policy gate (engine.eligible() also
    # checks each policy is one the VM's target namespace offers, which needs the
    # namespace a discovered VM does not have yet): a home or disk with no
    # policy, or policies never read, can never pass.
    {"code": "storage_policy_missing", "level": BLOCK, "title": "Storage policy missing or not read",
     "advice": "The VM's home or a disk has no storage policy, or discover never read which "
               "policies it uses. Assign one of the target namespace's storage policies in vCenter "
               "and re-discover; precheck and import hold it back until then "
               "(storage_compliance_enforce).",
     "test": lambda r, f: _policy_missing(f)},
]


def _policy_missing(facts: Dict[str, Any]) -> bool:
    attached = facts.get("storage_policies")
    if not isinstance(attached, dict) or attached.get("error"):
        return True
    disks = attached.get("disks") or {}
    return not attached.get("vm_home") or any(not disks.get(str(d)) for d in facts.get("disk_ids") or [])


def storage_compliance_detail(row: Any) -> str:
    """Why `storage_noncompliant` fired for one discovered row, for the console/CLI."""
    return storage_compliance_reason(_facts(row).get("storage_compliance"))


def assess(row: Any, ctx: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """Findings for one discovered VM row. `ctx` (see context()) lets the rules that compare VMs run;
    without it they stay quiet."""
    facts = _facts(row)
    out = []
    for rule in RULES:
        if rule.get("ctx") and ctx is None:
            continue
        try:
            hit = rule["test"](row, facts, ctx) if rule.get("ctx") else rule["test"](row, facts)
        except (KeyError, IndexError, TypeError, ValueError):
            hit = False
        if hit:
            out.append({k: rule[k] for k in ("code", "level", "title", "advice")})
    return out


def grade(findings: Sequence[Dict[str, str]]) -> str:
    levels = {f["level"] for f in findings}
    return "block" if BLOCK in levels else "warn" if WARN in levels else "ready"


def summary(rows: Sequence[Any]) -> Dict[str, Any]:
    """Grades and per-rule counts over many VMs."""
    grades = {g: 0 for g in GRADES}
    by_code: Dict[str, Dict[str, Any]] = {}
    ctx = context(rows)
    for r in rows:
        found = assess(r, ctx)
        grades[grade(found)] += 1
        for f in found:
            entry = by_code.setdefault(f["code"], dict(f, count=0, morefs=[]))
            entry["count"] += 1
            if len(entry["morefs"]) < 500:
                entry["morefs"].append(r["moref"])
    order = {BLOCK: 0, WARN: 1, INFO: 2}
    return {"grades": grades,
            "findings": sorted(by_code.values(), key=lambda e: (order[e["level"]], -e["count"]))}


def blocked_morefs(store: Any, morefs: Sequence[str] = ()) -> List[str]:
    """Queued VMs whose discovery facts grade them "block"."""
    rows = store.query_discovered(morefs=list(morefs) or None)
    return [r["moref"] for r in rows if grade(assess(r)) == "block"]


def test_for(code: str) -> Callable[[Any, Dict[str, Any]], bool]:
    return next(r["test"] for r in RULES if r["code"] == code)
