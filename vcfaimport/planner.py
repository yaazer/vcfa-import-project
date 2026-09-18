"""Turning a flat inventory into an ordered set of ImportOperationBatches."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .inventory import Nic, VmRecord, slugify
from .render import build_batch_manifest, manifest_to_yaml


@dataclass
class PlannedBatch:
    name: str
    namespace: str
    wave: int
    stage: str                       # "precheck" | "import"
    records: List[VmRecord] = field(default_factory=list)
    manifest: Dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.records)

    @property
    def morefs(self) -> List[str]:
        return [r.moref for r in self.records]

    def yaml(self) -> str:
        return manifest_to_yaml(self.manifest)


def record_from_row(row: sqlite3.Row) -> VmRecord:
    """Rebuild an inventory record from a stored row."""
    return VmRecord(
        moref=row["moref"],
        vm_name=row["vm_name"],
        namespace=row["namespace"],
        nics=[Nic.from_dict(n) for n in json.loads(row["nics_json"])],
        mode=row["mode"],
        wave=row["wave"],
        group=row["grp"],
        notes=row["notes"],
        row=row["src_row"],
    )


def _batch_name(cfg: Config, stage: str, wave: int, namespace: str, group: str, seq: int, salt: str) -> str:
    stage_tag = "pre" if stage == "precheck" else cfg.batch_name_prefix
    group_slug = slugify(group)[:22].strip("-") or "grp"
    digest = hashlib.sha1(
        "{}|{}|{}|{}|{}".format(salt, namespace, group, stage, seq).encode("utf-8")
    ).hexdigest()[:5]
    name = "{}-w{}-{}-{:03d}-{}".format(stage_tag, wave, group_slug, seq, digest)
    return name[:63].strip("-").lower()


def plan_batches(
    records: Sequence[VmRecord],
    cfg: Config,
    run_id: str,
    stage: str,
    *,
    precheck_only: Optional[bool] = None,
    commit_action: Optional[str] = None,
    batch_size: Optional[int] = None,
    name_salt: Optional[str] = None,
) -> List[PlannedBatch]:
    """Group records into batches. Batches never span wave, namespace, mode or group."""
    if precheck_only is None:
        precheck_only = stage == "precheck"

    size = batch_size or cfg.batch_size
    salt = name_salt or run_id
    groups: Dict[Tuple[int, str, str, str], List[VmRecord]] = {}
    for rec in records:
        groups.setdefault(rec.grouping_key, []).append(rec)

    batches: List[PlannedBatch] = []
    for key in sorted(groups, key=lambda k: (k[0], k[1], k[3], k[2])):
        wave, namespace, _mode, group = key
        members = sorted(groups[key], key=lambda r: (r.vm_name, r.moref))
        for seq, start in enumerate(range(0, len(members), size), start=1):
            chunk = members[start:start + size]
            name = _batch_name(cfg, stage, wave, namespace, group, seq, salt)
            manifest = build_batch_manifest(
                name,
                namespace,
                chunk,
                cfg,
                run_id=run_id,
                wave=wave,
                precheck_only=precheck_only,
                commit_action=commit_action,
                mode=chunk[0].mode,
            )
            batches.append(
                PlannedBatch(
                    name=name,
                    namespace=namespace,
                    wave=wave,
                    stage=stage,
                    records=chunk,
                    manifest=manifest,
                )
            )
    return batches


def summarize(batches: Sequence[PlannedBatch]) -> Dict[str, Any]:
    by_wave: Dict[int, Dict[str, int]] = {}
    by_ns: Dict[str, Dict[str, int]] = {}
    for b in batches:
        w = by_wave.setdefault(b.wave, {"batches": 0, "vms": 0})
        w["batches"] += 1
        w["vms"] += b.size
        n = by_ns.setdefault(b.namespace, {"batches": 0, "vms": 0})
        n["batches"] += 1
        n["vms"] += b.size
    return {
        "batches": len(batches),
        "vms": sum(b.size for b in batches),
        "by_wave": dict(sorted(by_wave.items())),
        "by_namespace": dict(sorted(by_ns.items())),
    }
