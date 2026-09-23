"""Manifest construction and a dependency-free YAML emitter.

We emit YAML by hand rather than depending on PyYAML: the document shape is
entirely ours, and a zero-dependency tool is far easier to get approved onto a
jump host than one that needs pip access.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import __version__
from .config import Config
from .inventory import VmRecord

LABEL_RUN = "vcfa-import/run"
LABEL_WAVE = "vcfa-import/wave"
LABEL_KIND = "vcfa-import/stage"
ANNOTATION_TOOL = "vcfa-import/tool-version"
ANNOTATION_TIME = "vcfa-import/generated-at"
ANNOTATION_VMS = "vcfa-import/vm-names"

# Length of the per-batch discriminator appended to every operation name. Five
# hex characters is what `_batch_name` already uses for batches; it keeps the
# names readable and is far below any realistic collision risk.
OP_DISCRIMINATOR_LEN = 5


def batch_discriminator(batch_name: str) -> str:
    """The tag that makes one batch's operation names its own.

    Derived from the batch name rather than sliced out of it, so it does not
    depend on how `_batch_name` happens to be formatted, and two batches can
    never share a tag unless they share a name -- in which case they are the
    same batch being re-planned, which is exactly when the names must match.
    """
    return hashlib.sha1(batch_name.encode("utf-8")).hexdigest()[:OP_DISCRIMINATOR_LEN]


# Plain (unquoted) scalars must not collide with YAML's implicit types.
_YAML_RESERVED = {
    "true", "false", "yes", "no", "on", "off", "null", "none", "~", "y", "n",
}
_PLAIN_SAFE = re.compile(r"^[A-Za-z0-9_./][A-Za-z0-9_./+@-]*$")
_NUMERIC = re.compile(r"^[-+]?(\d+\.?\d*([eE][-+]?\d+)?|\.\d+([eE][-+]?\d+)?|0x[0-9a-fA-F]+)$")


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if (
        text == ""
        or text.lower() in _YAML_RESERVED
        or _NUMERIC.match(text)
        or not _PLAIN_SAFE.match(text)
    ):
        escaped = (
            text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return '"{}"'.format(escaped)
    return text


def _emit(data: Any, indent: int, lines: List[str]) -> None:
    pad = " " * indent
    if isinstance(data, dict):
        for key, val in data.items():
            if val is None:
                continue  # omit unset optional fields rather than sending null
            if isinstance(val, dict):
                if val:
                    lines.append("{}{}:".format(pad, yaml_scalar(key)))
                    _emit(val, indent + 2, lines)
                else:
                    lines.append("{}{}: {{}}".format(pad, yaml_scalar(key)))
            elif isinstance(val, list):
                if val:
                    lines.append("{}{}:".format(pad, yaml_scalar(key)))
                    _emit(val, indent + 2, lines)
                else:
                    lines.append("{}{}: []".format(pad, yaml_scalar(key)))
            else:
                lines.append("{}{}: {}".format(pad, yaml_scalar(key), yaml_scalar(val)))
        return

    if isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)) and item:
                sub: List[str] = []
                _emit(item, indent + 2, sub)
                # Splice the "- " marker over the first child line's indent.
                sub[0] = pad + "- " + sub[0][indent + 2:]
                lines.extend(sub)
            elif isinstance(item, dict):
                lines.append(pad + "- {}")
            elif isinstance(item, list):
                lines.append(pad + "- []")
            else:
                lines.append(pad + "- " + yaml_scalar(item))
        return

    lines.append(pad + yaml_scalar(data))


def to_yaml(data: Any, indent: int = 0) -> str:
    """Render dicts/lists/scalars as block-style YAML."""
    lines: List[str] = []
    _emit(data, indent, lines)
    return "\n".join(lines) + "\n" if lines else ""


def build_batch_manifest(
    name: str,
    namespace: str,
    records: List[VmRecord],
    cfg: Config,
    *,
    run_id: str,
    wave: int,
    precheck_only: bool = False,
    commit_action: Optional[str] = None,
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one ImportOperationBatch as described in the Mobility Operator docs.

    Never emits controlAction.rollbackAction: the operator reads that field as
    "revert now". It is only ever patched onto a running batch by `rollback`.
    """
    control: Dict[str, Any] = {}
    if precheck_only:
        control["precheckOnly"] = True
    # A precheck never commits anything, so don't send a commit action with it
    # unless the caller asked for one explicitly.
    effective_commit = commit_action or (None if precheck_only else cfg.commit_action)
    if effective_commit:
        control["commitAction"] = effective_commit

    default_spec: Dict[str, Any] = {"mode": mode or (records[0].mode if records else cfg.mode)}
    if control:
        default_spec["controlAction"] = control

    operations: List[Dict[str, Any]] = []
    used: Dict[str, int] = {}
    discriminator = batch_discriminator(name)
    for rec in records:
        base = rec.operation_name()
        if base in used:
            # Two VMs with the same name in one batch: extend the tag rather
            # than the base, so the batch's own discriminator always survives.
            used[base] += 1
            tag = "{}{}".format(discriminator, used[base])
        else:
            used[base] = 0
            tag = discriminator
        op_name = rec.operation_name(tag)

        spec: Dict[str, Any] = {"virtualMachineID": rec.moref}
        nics = [
            {
                "deviceKey": nic.device_key,
                "subnetInfo": {
                    "apiGroup": nic.subnet_api_group,
                    "kind": nic.subnet_kind,
                    "name": nic.subnet,
                },
            }
            for nic in rec.nics
            if nic.subnet
        ]
        if nics:
            spec["networkInterfaces"] = nics
        operations.append({"name": op_name, "spec": spec})

    return {
        "apiVersion": cfg.api_version,
        "kind": cfg.batch_kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                LABEL_RUN: run_id,
                LABEL_WAVE: str(wave),
                LABEL_KIND: "precheck" if precheck_only else "import",
            },
            "annotations": {
                ANNOTATION_TOOL: __version__,
                ANNOTATION_TIME: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ANNOTATION_VMS: ",".join(r.vm_name for r in records)[:4000],
            },
        },
        "spec": {"defaultSpec": default_spec, "operations": operations},
    }


def manifest_to_yaml(manifest: Dict[str, Any]) -> str:
    return "---\n" + to_yaml(manifest)
