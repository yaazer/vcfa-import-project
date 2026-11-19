"""Why a queued VM is not eligible: one vocabulary for every hold-back.

`Engine.eligible()` keeps VMs out of a precheck or import for several reasons
(storage policy, readiness, a stale precheck, an application waiting on its
members). Each reason used to be one line of prose in a list that the Execute
page showed only when something else *was* eligible, so a run with every VM held
back said "No VM is eligible" and nothing more.

Now each hold-back is a record per VM -- a stable `code`, the VM's own `detail`
-- and this module says what each code means, how to fix it and where to go to
do that. The same records feed the Execute page, the Import queue badges, the
CLI plan and the discover log, so they can never disagree.

Pure data and grouping; nothing here touches the store or the network.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

# code -> (title, why, fix, where). `where` is a page of the console ("discover", "stage",
# "select", "queue", "execute") the fix starts from; "" when it is done elsewhere (vCenter).
CAUSES: Dict[str, Dict[str, str]] = {
    "sp_unread": {
        "title": "Storage policy not read",
        "why": "Discover has not recorded which storage policy this VM's home and disks use "
               "(an older discover, or storage-policy checking was switched off), so the tool "
               "cannot tell whether the target namespace will accept it.",
        "fix": "Run Discover again; it reads each VM's policies.",
        "where": "discover",
    },
    "sp_read_failed": {
        "title": "vCenter refused the storage-policy read",
        "why": "The call that reads this VM's storage policy failed, so the policy is unknown. "
               "The tool never guesses one.",
        "fix": "Give the Discover account the StorageProfile.View privilege, then Discover again.",
        "where": "discover",
    },
    "no_namespace": {
        "title": "No target namespace",
        "why": "The VM's folder has no namespace in the folder map and there is no default, so "
               "there is nothing to check its storage policy against.",
        "fix": "Map its folder to a namespace in Map & Stage, then stage again.",
        "where": "stage",
    },
    "ns_unread": {
        "title": "Namespace's storage policies unknown",
        "why": "The tool has not read which storage policies this namespace offers. Discover only "
               "reads the namespaces vCenter lists plus the ones the queue targets, and this one "
               "was not read -- it is not a namespace vCenter lists (a typo in the folder map, a "
               "namespace on another vCenter) or this login may not read it.",
        "fix": "Check the namespace name in the folder map. If it is right, re-read the namespaces and "
               "policies from vCenter (no need to read every VM again).",
        "where": "catalog",
    },
    "sp_not_offered": {
        "title": "VM uses a storage policy its namespace does not offer",
        "why": "A disk (or the VM home) is on a storage policy that is not one of the policies the "
               "target namespace offers. It may be compliant with that policy; the import would "
               "still have nowhere to put it.",
        "fix": "In vCenter, give the VM a policy the namespace offers and Discover again; or offer this "
               "policy to the namespace and re-read the namespaces; or map the VM's folder to a namespace "
               "that offers its policy.",
        "where": "catalog",
    },
    "sp_missing": {
        "title": "VM home or disk has no storage policy",
        "why": "vCenter reports no storage policy for part of this VM.",
        "fix": "Assign a VM storage policy in vCenter that the target namespace offers, then Discover again.",
        "where": "",
    },
    "sp_noncompliant": {
        "title": "Not compliant with its storage policy",
        "why": "vCenter's own check says this VM does not comply with the policy it is on.",
        "fix": "In vCenter, check compliance and reapply the policy (VM Storage Policies), then Discover again.",
        "where": "",
    },
    "sp_not_checked": {
        "title": "Storage-policy compliance never checked",
        "why": "No compliance verdict is recorded for this VM (an older discover, an older vCenter, or "
               "compliance checking was off).",
        "fix": "Run Discover again.",
        "where": "discover",
    },
    "readiness_blocked": {
        "title": "Readiness expects the precheck to fail",
        "why": "Readiness rules over the discovered facts (for example an ISO connected) grade this VM "
               "blocked, and readiness_exclude_blocked keeps blocked VMs out of precheck.",
        "fix": "Open Select VMs, filter by Readiness, fix what each rule says, then Discover again.",
        "where": "select",
    },
    "precheck_stale": {
        "title": "Precheck pass is too old",
        "why": "The VM passed precheck longer ago than precheck_max_age_hours; the VM may have changed since.",
        "fix": "Precheck it again.",
        "where": "execute",
    },
    "app_waiting": {
        "title": "Waiting for the rest of its application",
        "why": "app_together imports an application only when every one of its VMs can go in the same run.",
        "fix": "Get the other members ready (precheck them, or include their waves), or skip them.",
        "where": "queue",
    },
}

# Order of the groups on screen: what the user can act on first.
ORDER = ["no_namespace", "ns_unread", "sp_not_offered", "sp_missing", "sp_read_failed", "sp_unread",
         "sp_noncompliant", "sp_not_checked", "readiness_blocked", "precheck_stale", "app_waiting"]

# vcenter.storage_policy_findings() codes -> a cause above.
FINDING_CAUSE = {
    "never_read": "sp_unread", "read_failed": "sp_read_failed", "no_namespace": "no_namespace",
    "ns_unread": "ns_unread", "home_wrong": "sp_not_offered", "disk_wrong": "sp_not_offered",
    "home_none": "sp_missing", "disk_none": "sp_missing",
}


def cause(code: str) -> Dict[str, str]:
    return CAUSES.get(code) or {"title": code, "why": "", "fix": "", "where": ""}


def hold(row: Any, code: str, detail: str = "") -> Dict[str, Any]:
    """One VM kept out of a run, as a record. `row` is a vms row (or any mapping)."""
    def get(key: str, default: Any = "") -> Any:
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return default
    return {"moref": get("moref"), "vm_name": get("vm_name") or get("moref"), "wave": get("wave", None),
            "namespace": get("namespace"), "code": code, "detail": detail}


def from_findings(row: Any, findings: Sequence[Dict[str, str]]) -> List[Dict[str, Any]]:
    """vcenter.storage_policy_findings() for one VM -> its hold records, one per cause.

    A VM with two bad disks is one "not offered" record carrying both, plus what the
    namespace does offer."""
    by_cause: Dict[str, List[str]] = {}
    for f in findings:
        c = FINDING_CAUSE.get(f["code"])
        if c:
            by_cause.setdefault(c, []).append(f["text"])
    offers = next((f["text"] for f in findings if f["code"] == "offers"), "")
    return [hold(row, c, "; ".join(texts) + ("; " + offers if offers and c in ("sp_not_offered", "sp_missing") else ""))
            for c, texts in by_cause.items()]


def group(held: Iterable[Dict[str, Any]], per_group: int = 200) -> List[Dict[str, Any]]:
    """Held records -> one group per cause, most actionable first, each with its VMs.

    A VM held for two causes appears in both groups; `vms` counts distinct VMs per group.
    Duplicate (moref, code) records -- the same VM asked about by two waves or stages -- collapse."""
    groups: Dict[str, Dict[str, Any]] = {}
    seen = set()
    for h in held:
        key = (h["moref"], h["code"])
        if key in seen:
            continue
        seen.add(key)
        g = groups.get(h["code"])
        if g is None:
            c = cause(h["code"])
            g = groups[h["code"]] = dict(c, code=h["code"], count=0, vms=[])
        g["count"] += 1
        if len(g["vms"]) < per_group:
            g["vms"].append({k: h[k] for k in ("moref", "vm_name", "wave", "namespace", "detail")})
    rank = {c: i for i, c in enumerate(ORDER)}
    return sorted(groups.values(), key=lambda g: (rank.get(g["code"], len(ORDER)), g["code"]))


def by_moref(held: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, str]]]:
    """moref -> its reasons ({code, title, detail}), for a badge next to the VM."""
    out: Dict[str, List[Dict[str, str]]] = {}
    for h in held:
        reasons = out.setdefault(h["moref"], [])
        if not any(r["code"] == h["code"] for r in reasons):
            reasons.append({"code": h["code"], "title": cause(h["code"])["title"], "detail": h["detail"]})
    return out


def headline(groups: Sequence[Dict[str, Any]]) -> Optional[str]:
    """One sentence for a log or a CLI line: how many VMs are held and the biggest cause."""
    if not groups:
        return None
    vms = {v["moref"] for g in groups for v in g["vms"]}
    top = max(groups, key=lambda g: g["count"])
    return "{} VM(s) held back; most for: {} ({})".format(len(vms), top["title"].lower(), top["count"])
