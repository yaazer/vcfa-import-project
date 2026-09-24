"""How long will it take? Estimates from this campaign's own measurements.

A batch's duration is taken from batches that already finished here (the
median, so one stuck batch does not skew it). Until there is history, a
conservative default is used and the estimate says so. The schedule is then
simulated with the real limits: `max_parallel_batches` overall and
`max_parallel_batches_per_namespace` per namespace.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from . import state as st

DEFAULT_BATCH_SECONDS = {"precheck": 180, "import": 900}
FINISHED = (st.B_SUCCEEDED, st.B_PARTIAL, st.B_FAILED, st.B_ROLLED_BACK)


def _ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def batch_seconds(store: st.Store, stage: str) -> Dict[str, Any]:
    """Median batch duration for a stage, measured here, or the default."""
    durations: List[float] = []
    for b in store.query_batches(states=list(FINISHED), stage=stage):
        a, f = _ts(b["applied_at"]), _ts(b["finished_at"])
        if a and f and f >= a:
            durations.append((f - a).total_seconds())
    if not durations:
        return {"seconds": DEFAULT_BATCH_SECONDS.get(stage, 600), "samples": 0, "basis": "default"}
    durations.sort()
    mid = len(durations) // 2
    med = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2
    return {"seconds": max(1.0, med), "samples": len(durations), "basis": "measured"}


def simulate(batches_per_ns: Mapping[str, int], parallel: int, per_ns: int) -> int:
    """Rounds needed when every batch takes one round (greedy, like the engine)."""
    left = {ns: n for ns, n in batches_per_ns.items() if n > 0}
    rounds = 0
    while left:
        rounds += 1
        slots = parallel
        for ns in sorted(left, key=lambda k: -left[k]):
            if slots <= 0:
                break
            take = min(per_ns, left[ns], slots)
            left[ns] -= take
            slots -= take
        left = {ns: n for ns, n in left.items() if n > 0}
    return rounds


def estimate(store: st.Store, cfg: Any, stage: str, vms_per_ns: Mapping[str, int],
             batch_size: Optional[int] = None, parallel: Optional[int] = None) -> Dict[str, Any]:
    """Seconds to run `vms_per_ns` through `stage` with the current settings."""
    size = max(1, int(batch_size or cfg.batch_size))
    par = max(1, int(parallel or cfg.max_parallel_batches))
    per_ns = max(1, int(cfg.max_parallel_batches_per_namespace))
    per = {ns: math.ceil(n / size) for ns, n in vms_per_ns.items() if n > 0}
    total_batches = sum(per.values())
    basis = batch_seconds(store, stage)
    if not total_batches:
        return {"seconds": 0, "batches": 0, "rounds": 0, "batch_seconds": basis["seconds"],
                "samples": basis["samples"], "basis": basis["basis"]}
    rounds = simulate(per, par, per_ns)
    seconds = rounds * basis["seconds"] + total_batches * max(0, int(cfg.settle_seconds))
    return {"seconds": round(seconds), "batches": total_batches, "rounds": rounds,
            "batch_seconds": round(basis["seconds"]), "samples": basis["samples"],
            "basis": basis["basis"]}


def human(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 90:
        return "{}s".format(seconds)
    if seconds < 3600:
        return "{}m".format(round(seconds / 60))
    return "{}h {:02d}m".format(seconds // 3600, (seconds % 3600) // 60)
