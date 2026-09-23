"""Inventory loading: the CSV that describes which VMs go where."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import Config

MOREF_RE = re.compile(r"^vm-\d+$")
DNS1123_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
NIC_COL_RE = re.compile(r"^nic(\d+)_(subnet|device_key|devicekey|subnet_kind|subnet_api_group)$")

# Accepted spellings for each logical column, lower-cased with separators stripped.
ALIASES: Dict[str, str] = {
    "vmname": "vm_name", "name": "vm_name", "vm": "vm_name", "displayname": "vm_name",
    "moref": "moref", "moid": "moref", "vmid": "moref", "virtualmachineid": "moref",
    "vmmoref": "moref", "managedobjectid": "moref", "id": "moref",
    "namespace": "namespace", "ns": "namespace", "targetnamespace": "namespace",
    "subnet": "subnet", "targetsubnet": "subnet", "subnetname": "subnet",
    "devicekey": "device_key", "nicdevicekey": "device_key",
    "subnetkind": "subnet_kind",
    "subnetapigroup": "subnet_api_group", "apigroup": "subnet_api_group",
    "mode": "mode",
    "wave": "wave", "phase": "wave",
    "group": "group", "batchgroup": "group", "batch": "group",
    "nics": "nics",
    "skip": "skip", "exclude": "skip",
    "enabled": "enabled", "include": "enabled",
    "notes": "notes", "comment": "notes",
}

TEMPLATE_HEADER = [
    "vm_name", "moref", "namespace", "subnet", "device_key", "wave", "group", "mode", "notes",
]

TEMPLATE_ROWS = [
    ["app-web-01", "vm-1483405", "redbull-ns1-r95mc", "subnet-vlan197", "4000", "1", "", "", "pilot"],
    ["app-web-02", "vm-1483407", "redbull-ns1-r95mc", "subnet-vlan197", "4000", "1", "", "", ""],
    ["db-01", "vm-1483411", "redbull-ns2-k22ab", "subnet-vlan200", "4000", "2", "", "", "dual-nic example below"],
]


class InventoryError(Exception):
    pass


@dataclass
class Nic:
    device_key: int
    subnet: Optional[str]
    subnet_kind: str
    subnet_api_group: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_key": self.device_key,
            "subnet": self.subnet,
            "subnet_kind": self.subnet_kind,
            "subnet_api_group": self.subnet_api_group,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Nic":
        return cls(
            device_key=int(d["device_key"]),
            subnet=d.get("subnet"),
            subnet_kind=d.get("subnet_kind") or "Subnet",
            subnet_api_group=d.get("subnet_api_group") or "crd.nsx.vmware.com",
        )


@dataclass
class VmRecord:
    moref: str
    vm_name: str
    namespace: str
    nics: List[Nic] = field(default_factory=list)
    mode: str = "preserve"
    wave: int = 1
    group: str = ""
    notes: str = ""
    row: int = 0

    @property
    def grouping_key(self) -> Tuple[int, str, str, str]:
        """Batches never span a wave, a namespace, a mode, or an explicit group.

        Mode is part of the key so that defaultSpec.mode always applies to every
        operation in the batch and we never need a per-operation override.
        """
        return (self.wave, self.namespace, self.mode, self.group or self._auto_group())

    def _auto_group(self) -> str:
        # Without an explicit group, VMs that share a subnet set travel together.
        subnets = sorted(n.subnet or "-" for n in self.nics)
        return "+".join(subnets) if subnets else "no-network"

    def operation_name(self, discriminator: str = "") -> str:
        """Name for this VM's operation, and so for the child object it creates.

        The operator names each child `ImportOperation` after the operation and
        makes the batch its controller. Two batches for the same VM therefore
        ask for the same child name, and the second one deadlocks: it cannot
        create the object (it exists) and cannot adopt it (another controller
        owns it), so its status.operations stays empty forever. Observed in the
        lab on 2026-09-21 between a precheck batch and the import that followed.

        The discriminator -- the batch's own digest, see render.py -- keeps each
        batch's children distinct. It is deterministic in the batch name, so
        re-planning the same batch reproduces the same names and a resumed run
        still matches its children.
        """
        base = slugify(self.vm_name) or slugify(self.moref)
        suffix = self.moref.split("-")[-1] if "-" in self.moref else self.moref
        tail = "-" + discriminator if discriminator else ""
        base = base[:48].rstrip("-")
        name = "{}-{}".format(base, slugify(suffix)) if base else slugify(self.moref)
        return name[:63 - len(tail)].rstrip("-") + tail

    def to_dict(self) -> Dict[str, Any]:
        return {
            "moref": self.moref,
            "vm_name": self.vm_name,
            "namespace": self.namespace,
            "nics": [n.to_dict() for n in self.nics],
            "mode": self.mode,
            "wave": self.wave,
            "group": self.group,
            "notes": self.notes,
            "row": self.row,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VmRecord":
        return cls(
            moref=d["moref"],
            vm_name=d.get("vm_name", ""),
            namespace=d["namespace"],
            nics=[Nic.from_dict(n) for n in d.get("nics", [])],
            mode=d.get("mode") or "preserve",
            wave=int(d.get("wave", 1)),
            group=d.get("group", ""),
            notes=d.get("notes", ""),
            row=int(d.get("row", 0)),
        )


def slugify(value: str) -> str:
    """Lower-case DNS-1123-safe label fragment."""
    out = re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower())
    return re.sub(r"-+", "-", out).strip("-")


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (key or "").lower())


def load_inventory(path: str, cfg: Config) -> Tuple[List[VmRecord], List[str]]:
    """Read the inventory CSV. Returns (records, warnings); raises on hard errors."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise InventoryError("inventory file not found: {}".format(p))

    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        if not reader.fieldnames:
            raise InventoryError("inventory CSV has no header row")
        colmap, nic_cols = _map_columns(reader.fieldnames)
        if "moref" not in colmap.values():
            raise InventoryError(
                "inventory CSV needs a moref column (accepted: moref, moid, vm_id, virtualMachineID)"
            )
        if "namespace" not in colmap.values():
            raise InventoryError("inventory CSV needs a namespace column")
        rows = list(reader)

    records: List[VmRecord] = []
    warnings: List[str] = []
    errors: List[str] = []
    seen: Dict[str, int] = {}

    for idx, raw in enumerate(rows, start=2):  # row 1 is the header
        row = {colmap.get(_norm_key(k), _norm_key(k)): (v or "").strip()
               for k, v in raw.items() if k is not None}

        if _is_falsey(row.get("enabled", "")) and "enabled" in row and row.get("enabled", "") != "":
            continue
        if _is_truthy(row.get("skip", "")):
            continue

        moref = row.get("moref", "")
        if not moref:
            continue  # blank trailing line
        if not MOREF_RE.match(moref):
            warnings.append("row {}: '{}' does not look like a vCenter moref (expected vm-<number>)"
                            .format(idx, moref))
        if moref in seen:
            errors.append("row {}: duplicate moref {} (first seen on row {})".format(idx, moref, seen[moref]))
            continue
        seen[moref] = idx

        namespace = row.get("namespace", "")
        if not namespace:
            errors.append("row {}: namespace is required".format(idx))
            continue
        if not DNS1123_RE.match(namespace):
            errors.append("row {}: namespace '{}' is not a valid Kubernetes name".format(idx, namespace))
            continue

        nics = _build_nics(row, raw, nic_cols, cfg, idx, warnings)

        wave_raw = row.get("wave", "") or "1"
        try:
            wave = int(float(wave_raw))
        except ValueError:
            errors.append("row {}: wave '{}' is not a number".format(idx, wave_raw))
            continue

        records.append(
            VmRecord(
                moref=moref,
                vm_name=row.get("vm_name", "") or moref,
                namespace=namespace,
                nics=nics,
                mode=row.get("mode", "") or cfg.mode,
                wave=wave,
                group=row.get("group", ""),
                notes=row.get("notes", ""),
                row=idx,
            )
        )

    if errors:
        raise InventoryError("inventory has {} error(s):\n  - {}".format(len(errors), "\n  - ".join(errors)))

    _warn_on_name_collisions(records, warnings)
    return records, warnings


def _map_columns(fieldnames: Iterable[str]) -> Tuple[Dict[str, str], Dict[int, Dict[str, str]]]:
    """Map raw header names onto logical names, plus per-index nicN_* columns."""
    colmap: Dict[str, str] = {}
    nic_cols: Dict[int, Dict[str, str]] = {}
    for name in fieldnames:
        if name is None:
            continue
        norm = _norm_key(name)
        low = re.sub(r"[^a-z0-9_]", "", name.strip().lower())
        m = NIC_COL_RE.match(low)
        if m:
            idx = int(m.group(1))
            attr = m.group(2).replace("devicekey", "device_key")
            nic_cols.setdefault(idx, {})[attr] = name
            continue
        if norm in ALIASES:
            colmap[norm] = ALIASES[norm]
    return colmap, nic_cols


def _build_nics(row, raw, nic_cols, cfg: Config, idx: int, warnings: List[str]) -> List[Nic]:
    nics: List[Nic] = []

    # 1. JSON column wins if present: [{"deviceKey": 4000, "subnet": "s1"}, ...]
    nics_json = row.get("nics", "")
    if nics_json:
        try:
            parsed = json.loads(nics_json)
        except json.JSONDecodeError as exc:
            raise InventoryError("row {}: nics column is not valid JSON: {}".format(idx, exc)) from exc
        for entry in parsed:
            nics.append(
                Nic(
                    device_key=int(entry.get("deviceKey", entry.get("device_key", cfg.default_device_key))),
                    subnet=entry.get("subnet") or entry.get("name"),
                    subnet_kind=entry.get("kind") or entry.get("subnet_kind") or cfg.subnet_kind,
                    subnet_api_group=entry.get("apiGroup") or entry.get("subnet_api_group") or cfg.subnet_api_group,
                )
            )
        return nics

    # 2. Numbered columns: nic1_subnet, nic1_device_key, nic2_subnet, ...
    for n in sorted(nic_cols):
        cols = nic_cols[n]
        subnet = (raw.get(cols.get("subnet", ""), "") or "").strip()
        if not subnet:
            continue
        dk_raw = (raw.get(cols.get("device_key", ""), "") or "").strip()
        nics.append(
            Nic(
                device_key=int(dk_raw) if dk_raw else cfg.default_device_key + (n - 1),
                subnet=subnet,
                subnet_kind=(raw.get(cols.get("subnet_kind", ""), "") or "").strip() or cfg.subnet_kind,
                subnet_api_group=(raw.get(cols.get("subnet_api_group", ""), "") or "").strip()
                or cfg.subnet_api_group,
            )
        )
    if nics:
        return nics

    # 3. Single flat subnet column.
    subnet = row.get("subnet", "")
    if not subnet:
        warnings.append(
            "row {}: no subnet given; the batch will omit networkInterfaces for this VM".format(idx)
        )
        return nics
    dk_raw = row.get("device_key", "")
    nics.append(
        Nic(
            device_key=int(dk_raw) if dk_raw else cfg.default_device_key,
            subnet=subnet,
            subnet_kind=row.get("subnet_kind", "") or cfg.subnet_kind,
            subnet_api_group=row.get("subnet_api_group", "") or cfg.subnet_api_group,
        )
    )
    return nics


def _warn_on_name_collisions(records: List[VmRecord], warnings: List[str]) -> None:
    by_ns: Dict[str, Dict[str, str]] = {}
    for rec in records:
        names = by_ns.setdefault(rec.namespace, {})
        op = rec.operation_name()
        if op in names:
            warnings.append(
                "operation name '{}' is shared in namespace {} ({} and {}); the batch "
                "discriminator keeps the child objects apart"
                .format(op, rec.namespace, names[op], rec.moref)
            )
        else:
            names[op] = rec.moref


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "y", "x")


def _is_falsey(value: str) -> bool:
    return value.strip().lower() in ("0", "false", "no", "n")


def write_template(path: str) -> None:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(TEMPLATE_HEADER)
        for row in TEMPLATE_ROWS:
            writer.writerow(row)
