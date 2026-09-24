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
from typing import Any, Callable, Dict, List, Sequence

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
    {"code": "iso_connected", "level": WARN, "title": "A CD-ROM has an ISO attached",
     "advice": "The ISO's datastore path may not exist on the target. Disconnect it before importing.",
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
]


def assess(row: Any) -> List[Dict[str, str]]:
    """Findings for one discovered VM row."""
    facts = _facts(row)
    out = []
    for rule in RULES:
        try:
            hit = rule["test"](row, facts)
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
    for r in rows:
        found = assess(r)
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
