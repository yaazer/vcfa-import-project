"""Settings edited from the console (or `vcfa-import settings`), within guardrails.

The TOML config stays the baseline. Changes made here are stored in the
workspace (state.db meta "settings") and layered on top of it, so the CLI and
the console always agree. Order of precedence, lowest first:

    built-in defaults  <  vcfa-import.toml  <  workspace settings  <  per-run flags

Every editable key has a type and a range; anything outside is refused.
"""

from __future__ import annotations

from typing import Any, Dict, List

from . import notify
from .config import APPROVABLE_STAGES, Config, ConfigError

META_KEY = "settings"
MARK = "_settings_applied"

# key: (group, label, kind, lo, hi, help)
FIELDS: Dict[str, tuple] = {
    "batch_size": ("Pacing", "VMs per batch", "int", 1, 200, "How many VMs share one ImportOperationBatch."),
    "max_parallel_batches": ("Pacing", "Parallel batches", "int", 1, 64, "Batches in flight at once, cluster-wide."),
    "max_parallel_batches_per_namespace": ("Pacing", "Parallel per namespace", "int", 1, 32, "Batches in flight in one namespace."),
    "poll_interval_seconds": ("Pacing", "Poll interval (s)", "int", 2, 600, "How often in-flight batches are read."),
    "settle_seconds": ("Pacing", "Pause between applies (s)", "int", 0, 120, "Breathing room between applying batches."),
    "batch_timeout_minutes": ("Pacing", "Batch timeout (min)", "int", 5, 1440, "A batch still running after this is flagged for review."),
    "rollback_timeout_minutes": ("Pacing", "Rollback wait (min)", "int", 1, 720, "How long a rollback waits for the operator."),
    "failure_rate_abort": ("Safety", "Circuit breaker", "float", 0.05, 1.0, "Halt a wave once this share of its results failed."),
    "failure_rate_min_sample": ("Safety", "Breaker minimum sample", "int", 1, 1000, "Results needed before the breaker may trip."),
    "max_vms_per_run": ("Safety", "Max VMs per run", "int", 0, 100000, "0 means no cap."),
    "max_retries": ("Safety", "Automatic retries", "int", 0, 10, "Retry ceiling per VM (retry --force overrides)."),
    "require_precheck": ("Safety", "Require a passed precheck", "bool", None, None, "Import refuses VMs that did not pass precheck."),
    "readiness_exclude_blocked": ("Safety", "Skip VMs readiness blocks", "bool", None, None, "Keep VMs graded 'blocked' out of precheck."),
    "commit_action": ("Safety", "Commit action", "choice:Auto,Wait", None, None, "Auto commits on success; Wait holds imports for approval."),
    "require_approval": ("Governance", "Needs a second person", "stages", None, None, "Stages that a different operator must approve."),
    "app_category": ("Applications", "App tag category", "str", 0, 80, "vCenter tag category that names each VM's application."),
    "app_together": ("Applications", "Keep apps together", "bool", None, None, "Import an app only when every VM in it is ready."),
    "verify_after_import": ("Verification", "Verify after import", "bool", None, None, "Check committed VMs automatically when an import finishes."),
    "verify_ping": ("Verification", "Ping the guest", "bool", None, None, "ICMP echo to the VM's IP."),
    "verify_ports": ("Verification", "TCP ports to check", "ports", None, None, "e.g. 22, 443, 3389."),
    "verify_timeout_seconds": ("Verification", "Check timeout (s)", "int", 1, 30, "Per ping or port check."),
}


def _coerce(key: str, value: Any) -> Any:
    group, label, kind, lo, hi, _ = FIELDS[key]
    try:
        if kind == "int":
            v = int(value)
            if isinstance(value, float) and value != int(value):
                raise ValueError
        elif kind == "float":
            v = float(value)
        elif kind == "bool":
            if isinstance(value, str):
                if value.lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                    raise ValueError
                v = value.lower() in ("true", "1", "yes", "on")
            else:
                v = bool(value)
            return v
        elif kind.startswith("choice:"):
            choices = kind.split(":", 1)[1].split(",")
            if value not in choices:
                raise ConfigError("{} must be one of {}".format(label, ", ".join(choices)))
            return value
        elif kind == "stages":
            items = [s.strip() for s in (value.split(",") if isinstance(value, str) else value or []) if str(s).strip()]
            bad = [s for s in items if s not in APPROVABLE_STAGES]
            if bad:
                raise ConfigError("{}: only {}".format(label, ", ".join(APPROVABLE_STAGES)))
            return sorted(set(items), key=list(APPROVABLE_STAGES).index)
        elif kind == "ports":
            items = value.split(",") if isinstance(value, str) else (value or [])
            ports = [int(str(p).strip()) for p in items if str(p).strip()]
            if any(not 0 < p < 65536 for p in ports) or len(ports) > 20:
                raise ConfigError("{}: up to 20 port numbers from 1 to 65535".format(label))
            return ports
        elif kind == "str":
            v = str(value or "").strip()
            if len(v) > hi:
                raise ConfigError("{} is at most {} characters".format(label, hi))
            return v
        else:
            raise ConfigError("unknown setting kind " + kind)
    except (TypeError, ValueError):
        raise ConfigError("{}: {!r} is not a valid {}".format(label, value, kind))
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ConfigError("{} must be between {} and {}".format(label, lo, hi))
    return v


def overlay(store: Any) -> Dict[str, Any]:
    data = store.get_meta(META_KEY) or {}
    return data if isinstance(data, dict) else {}


def apply(cfg: Config, store: Any) -> Config:
    """Layer the workspace settings onto cfg (once), then validate."""
    if getattr(cfg, MARK, False):
        return cfg
    for key, value in overlay(store).items():
        if key == "notify":
            cfg.notify = value
        elif key in FIELDS:
            setattr(cfg, key, value)
    setattr(cfg, MARK, True)
    cfg.validate()
    return cfg


def update(store: Any, changes: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """Validate and store changes. A value of None removes the override."""
    current = dict(overlay(store))
    done = []
    for key, value in changes.items():
        if key not in FIELDS and key != "notify":
            raise ConfigError("{} cannot be changed here".format(key))
        if value is None:
            current.pop(key, None)
            done.append("{} reset to the config file".format(key))
            continue
        clean = notify.validate(value) if key == "notify" else _coerce(key, value)
        current[key] = clean
        done.append("{} = {}".format(key, "{} channel(s)".format(len(clean)) if key == "notify" else clean))
    # the whole result must still be a valid configuration
    probe = Config()
    for key, value in current.items():
        setattr(probe, key if key != "notify" else "notify", value)
    probe.validate()
    store.set_meta(META_KEY, current)
    for line in done:
        store.log_event("setting changed: " + line, actor=actor)
    return current


def describe(file_cfg: Config, store: Any) -> List[Dict[str, Any]]:
    """Every editable setting: effective value, file value, and whether it is overridden."""
    ov = overlay(store)
    out = []
    for key, (group, label, kind, lo, hi, help_text) in FIELDS.items():
        file_value = getattr(file_cfg, key)
        out.append({"key": key, "group": group, "label": label, "kind": kind, "min": lo, "max": hi,
                    "help": help_text, "file_value": file_value,
                    "value": ov.get(key, file_value), "overridden": key in ov})
    return out
