"""Configuration loading and defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore


class ConfigError(Exception):
    pass


# Status buckets that every operator-reported phase is normalised into.
BUCKET_SUCCEEDED = "succeeded"
BUCKET_FAILED = "failed"
BUCKET_AWAITING_COMMIT = "awaiting_commit"
BUCKET_ROLLED_BACK = "rolled_back"   # ownership reverted to vCenter; batch is safe to delete
BUCKET_RUNNING = "running"
BUCKET_UNKNOWN = "unknown"

# The Mobility Operator's status vocabulary is not fully documented and has
# already moved between alpha revisions. Rather than hard-coding field names we
# classify whatever string the operator reports using these ordered regexes.
# First match wins, so the terminal states are listed before the busy ones.
DEFAULT_PHASE_PATTERNS: List[List[str]] = [
    [r"^(succeeded|success|completed|complete|committed|done|imported|ready)$", BUCKET_SUCCEEDED],
    # A finished rollback is its own outcome: the VM is back under vCenter.
    [r"(rolledback|rolled_back|rollbackcomplete|rollbackcompleted|rollbacksucceeded|reverted)", BUCKET_ROLLED_BACK],
    # A rollback still in progress is just "busy"; it must not read as failed.
    [r"(rollingback|rollbackinprogress|rollbackpending|rollbackrequested|rollbackrunning|reverting)", BUCKET_RUNNING],
    [r"(failed|failure|error|aborted|cancell?ed|timedout|timeout|invalid|rejected)", BUCKET_FAILED],
    [r"(awaitingcommit|awaiting_commit|waitingforcommit|pendingcommit|readytocommit|committable|waitingcommit)", BUCKET_AWAITING_COMMIT],
    [r"(pending|running|inprogress|in_progress|progress|migrating|importing|precheck|initializ|creating|working|active|reconcil|objectnotready|notready)", BUCKET_RUNNING],
]

APPROVABLE_STAGES = ("import", "commit", "rollback")

DEFAULT_CONDITION_TYPES = ["Succeeded", "Ready", "Complete", "Completed", "Committed", "Imported"]


@dataclass
class Config:
    # ---- cluster / API -------------------------------------------------
    kubectl: str = "kubectl"
    context: Optional[str] = None
    kubeconfig: Optional[str] = None
    api_version: str = "mobility-operator.vmware.com/v1alpha3"
    batch_kind: str = "ImportOperationBatch"
    batch_resource: str = "importoperationbatches"
    operation_resource: str = "importoperations"
    vmim_resource: str = "virtualmachineinframigrations"

    # ---- import semantics ----------------------------------------------
    mode: str = "preserve"
    commit_action: str = "Auto"            # Auto | Wait
    # There is deliberately no rollback option here. The operator treats
    # controlAction.rollbackAction as an instruction ("revert now"), not a
    # policy ("revert on failure"), so it must only ever be added to a batch
    # that is already running -- which is what `vcfa-import rollback` does.
    subnet_api_group: str = "crd.nsx.vmware.com"
    subnet_kind: str = "Subnet"
    default_device_key: int = 4000

    # ---- batching / pacing ----------------------------------------------
    batch_size: int = 10
    max_parallel_batches: int = 4
    max_parallel_batches_per_namespace: int = 2
    poll_interval_seconds: int = 20
    batch_timeout_minutes: int = 90
    rollback_timeout_minutes: int = 30  # how long `rollback` waits for the operator to revert
    settle_seconds: int = 5   # pause between successive applies
    max_retries: int = 1      # per-VM automatic retries

    # ---- safety ----------------------------------------------------------
    failure_rate_abort: float = 0.25   # halt a run if this share of a wave fails
    failure_rate_min_sample: int = 8   # ...but only after this many results
    max_vms_per_run: int = 0           # 0 = unlimited
    require_precheck: bool = True      # refuse to import VMs with no passing precheck
    readiness_exclude_blocked: bool = False  # keep VMs readiness marks "blocked" out of precheck

    # ---- governance --------------------------------------------------------
    # Stages that need a second person: any of "import", "commit", "rollback".
    require_approval: List[str] = field(default_factory=list)

    # ---- applications -------------------------------------------------------
    app_category: str = ""        # vCenter tag category that names the application
    app_together: bool = False    # import an application only when all its VMs are ready

    # ---- verification after import ------------------------------------------
    verify_after_import: bool = False
    verify_ping: bool = True
    verify_ports: List[int] = field(default_factory=list)
    verify_timeout_seconds: int = 2

    # ---- notifications ([[notify]] tables; see README) -----------------------
    notify: List[Dict[str, Any]] = field(default_factory=list)

    # ---- naming -----------------------------------------------------------
    batch_name_prefix: str = "imp"

    # ---- status interpretation --------------------------------------------
    phase_patterns: List[List[str]] = field(
        default_factory=lambda: [list(p) for p in DEFAULT_PHASE_PATTERNS]
    )
    condition_types: List[str] = field(default_factory=lambda: list(DEFAULT_CONDITION_TYPES))

    # ---- paths -------------------------------------------------------------
    workdir: str = "./run"
    # Append-only movement log. Defaults to <workdir>/ledger.jsonl; point it at a
    # share or an audit volume to keep the record off the operator's laptop.
    ledger_path: Optional[str] = None

    # Populated at load time, not read from the file.
    source_path: Optional[str] = None

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[str]) -> "Config":
        cfg = cls()
        if path:
            p = Path(path).expanduser()
            if not p.is_file():
                raise ConfigError("config file not found: {}".format(p))
            cfg = cls.from_dict(_read_toml(p))
            cfg.source_path = str(p.resolve())
        cfg.apply_env()
        cfg.validate()
        return cfg

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        # Accept both flat keys and [section] tables; sections are flattened.
        flat: Dict[str, Any] = {}
        for key, val in data.items():
            if isinstance(val, dict):
                flat.update(val)
            else:
                flat[key] = val
        known = set(cls().__dict__)
        if "rollback_action" in flat:
            raise ConfigError(
                "rollback_action is no longer a config option and must be removed.\n"
                "The Mobility Operator treats controlAction.rollbackAction as an instruction "
                "to revert NOW, not as an on-failure policy: a batch created with it would "
                "revert every VM instead of importing them. Roll back after a failure with "
                "`vcfa-import rollback --failed`, or add --rollback-failed to `run`."
            )
        unknown = sorted(set(flat) - known)
        if unknown:
            raise ConfigError(
                "unknown config key(s): "
                + ", ".join(unknown)
                + "\nvalid keys: "
                + ", ".join(sorted(known - {"source_path"}))
            )
        cfg = cls()
        for key, val in flat.items():
            setattr(cfg, key, val)
        return cfg

    def apply_env(self) -> None:
        """Environment overrides, handy for CI and scheduled runs."""
        env_map = {
            "VCFA_IMPORT_CONTEXT": "context",
            "VCFA_IMPORT_KUBECONFIG": "kubeconfig",
            "VCFA_IMPORT_KUBECTL": "kubectl",
            "VCFA_IMPORT_WORKDIR": "workdir",
        }
        for env, attr in env_map.items():
            val = os.environ.get(env)
            if val:
                setattr(self, attr, val)

    def validate(self) -> None:
        if self.commit_action not in ("Auto", "Wait"):
            raise ConfigError("commit_action must be 'Auto' or 'Wait'")
        if self.batch_size < 1:
            raise ConfigError("batch_size must be >= 1")
        if self.max_parallel_batches < 1:
            raise ConfigError("max_parallel_batches must be >= 1")
        if self.max_parallel_batches_per_namespace < 1:
            raise ConfigError("max_parallel_batches_per_namespace must be >= 1")
        if self.poll_interval_seconds < 1:
            raise ConfigError("poll_interval_seconds must be >= 1")
        if not 0 < float(self.failure_rate_abort) <= 1:
            raise ConfigError("failure_rate_abort must be in (0, 1]")
        bad = sorted(set(self.require_approval) - set(APPROVABLE_STAGES))
        if bad:
            raise ConfigError("require_approval accepts {}; not {}".format(
                ", ".join(APPROVABLE_STAGES), ", ".join(bad)))
        for port in self.verify_ports:
            if not isinstance(port, int) or not 0 < port < 65536:
                raise ConfigError("verify_ports must be TCP port numbers, not {!r}".format(port))
        if not isinstance(self.notify, list) or not all(isinstance(c, dict) for c in self.notify):
            raise ConfigError("notify must be a list of [[notify]] tables")
        if "/" not in self.api_version:
            raise ConfigError("api_version must look like 'group/version'")
        for pat in self.phase_patterns:
            if len(pat) != 2:
                raise ConfigError("each phase_patterns entry must be [regex, bucket]")
            try:
                re.compile(pat[0])
            except re.error as exc:
                raise ConfigError("bad regex in phase_patterns: {}: {}".format(pat[0], exc)) from exc

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("source_path", None)
        return d

    @property
    def api_group(self) -> str:
        return self.api_version.split("/", 1)[0]

    def qualified(self, resource: str) -> str:
        """Fully-qualified resource name, e.g. importoperationbatches.<group>."""
        return "{}.{}".format(resource, self.api_group)


def _read_toml(path: Path) -> Dict[str, Any]:
    if tomllib is None:
        raise ConfigError("TOML config requires Python 3.11 or newer")
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("{} is not valid TOML: {}".format(path, exc)) from exc


SAMPLE_CONFIG = '''\
# vcfa-import configuration
# Every key is optional; the values shown are the built-in defaults.

[cluster]
# kubectl      = "kubectl"
# context      = "supervisor.example.local"   # kubectl context for the Supervisor
# kubeconfig   = "~/.kube/config"
api_version    = "mobility-operator.vmware.com/v1alpha3"

[import]
mode               = "preserve"   # preserve keeps the existing IP addressing
commit_action      = "Auto"       # Auto | Wait  (Wait = manual commit gate)
# Rollback is never set at creation time -- see `vcfa-import rollback --help`.
subnet_api_group   = "crd.nsx.vmware.com"
subnet_kind        = "Subnet"
default_device_key = 4000

[batching]
batch_size                         = 10   # VMs per ImportOperationBatch
max_parallel_batches               = 4    # in-flight batches, cluster wide
max_parallel_batches_per_namespace = 2
poll_interval_seconds              = 20
batch_timeout_minutes              = 90
rollback_timeout_minutes           = 30
settle_seconds                     = 5
max_retries                        = 1

[safety]
failure_rate_abort      = 0.25   # halt the wave once 25% of results have failed
failure_rate_min_sample = 8
max_vms_per_run         = 0      # 0 = unlimited
require_precheck        = true
# readiness_exclude_blocked = false   # keep VMs readiness marks "blocked" out of precheck

[governance]
# require_approval = ["import", "commit"]   # a second person approves these (console and CLI)

[apps]
# app_category = "Application"   # vCenter tag category that names each VM's application
# app_together = true            # import an application only when all its VMs passed precheck

[verify]
# verify_after_import    = true
# verify_ping            = true
# verify_ports           = [22, 443, 3389]
# verify_timeout_seconds = 2

# [[notify]]
# name   = "ops channel"
# type   = "teams"            # teams | slack | webhook | email
# url    = "https://example.webhook.office.com/..."
# events = ["job_failed", "circuit_breaker", "awaiting_commit", "approval_requested"]

[naming]
batch_name_prefix = "imp"

[paths]
workdir = "./run"
# ledger_path = "//fileserver/migration/vcfa-ledger.jsonl"
'''
