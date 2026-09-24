#!/usr/bin/env python3
"""Test suite for vcfa-import.

Runs without pytest:   python tests/run_tests.py

Unit tests need nothing but the standard library. The end-to-end tests drive
the real CLI against tools/fake_kubectl.py, which stands in for kubectl and the
Mobility Operator; those need PyYAML (a test-only dependency).
"""

from __future__ import annotations

import copy
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vcfaimport.config import Config  # noqa: E402
from vcfaimport.inventory import InventoryError, load_inventory  # noqa: E402
from vcfaimport.planner import plan_batches  # noqa: E402
from vcfaimport.render import build_batch_manifest, to_yaml  # noqa: E402
from vcfaimport.status import batch_status, classify, operator_status  # noqa: E402

RESULTS = {"pass": 0, "fail": 0}
FAILURES = []


def test(name):
    def deco(fn):
        def wrapper():
            try:
                fn()
                RESULTS["pass"] += 1
                print("  [pass] {}".format(name))
            except Exception:
                RESULTS["fail"] += 1
                FAILURES.append((name, traceback.format_exc()))
                print("  [FAIL] {}".format(name))
        wrapper.__test_name__ = name
        return wrapper
    return deco


def eq(actual, expected, note=""):
    assert actual == expected, "{}expected {!r}, got {!r}".format(
        note + ": " if note else "", expected, actual)


def write_csv(path, rows, header):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


# ------------------------------------------------------------------ YAML
@test("emitted YAML parses back to the same structure")
def t_yaml_roundtrip():
    try:
        import yaml
    except ImportError:
        print("       (skipped: PyYAML not installed)")
        return
    cfg = Config()
    records, _ = _sample_records()
    manifest = build_batch_manifest(
        "b1", "ns-a", records, cfg, run_id="r1", wave=1, precheck_only=True)
    text = to_yaml(manifest)
    parsed = yaml.safe_load(text)
    eq(parsed, manifest, "round-trip")
    eq(parsed["spec"]["defaultSpec"]["controlAction"]["precheckOnly"], True)
    eq(parsed["spec"]["operations"][0]["spec"]["virtualMachineID"], "vm-1001")
    eq(parsed["spec"]["operations"][0]["spec"]["networkInterfaces"][0]["deviceKey"], 4000)


@test("awkward scalars are quoted correctly")
def t_yaml_quoting():
    try:
        import yaml
    except ImportError:
        return
    data = {
        "a": "yes", "b": "123", "c": "", "d": "on", "e": "a: b", "f": "vm-1", "g": True,
        "h": 4000, "i": "null", "j": "line\nbreak", "k": "* star", "l": "#hash",
    }
    parsed = yaml.safe_load(to_yaml(data))
    eq(parsed, data, "quoting")


@test("manifests never carry rollbackAction at creation, and the config refuses it")
def t_no_creation_rollback():
    from vcfaimport.config import ConfigError
    cfg = Config()
    records, _ = _sample_records()
    for precheck in (True, False):
        m = build_batch_manifest("b", "ns", records, cfg, run_id="r", wave=1,
                                 precheck_only=precheck)
        control = m["spec"]["defaultSpec"].get("controlAction", {})
        assert "rollbackAction" not in control, control
        assert "rollbackAction" not in to_yaml(m)
    try:
        Config.from_dict({"rollback_action": "Immediate"})
    except ConfigError as exc:
        assert "revert NOW" in str(exc) and "rollback --failed" in str(exc), exc
    else:
        raise AssertionError("a config with rollback_action must be rejected")


@test("the fake operator reverts everything if rollbackAction is present at creation")
def t_fake_creation_rollback():
    """Guards the simulator's fidelity to the operator's real semantics."""
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import fake_kubectl
    except ImportError:
        return  # PyYAML missing; e2e is skipped too
    obj = {"metadata": {"name": "b", "namespace": "ns"},
           "spec": {"defaultSpec": {"controlAction": {"rollbackAction": "Immediate",
                                                      "commitAction": "Auto"}},
                    "operations": [{"name": "op1", "spec": {"virtualMachineID": "vm-1001"}}]},
           "_rollback_poll": 0, "_rollback_at_creation": True}
    os.environ["FAKE_KUBECTL_SCENARIO"] = "happy"
    os.environ["FAKE_KUBECTL_SETTLE"] = "1"
    status = fake_kubectl.build_status(obj, polls=5)
    eq(status.get("rolledBackOps"), ["op1"],
       "a VM that would have imported cleanly is reverted instead")
    eq(status.get("completedOps"), [])
    complete = next(c for c in status["conditions"] if c["type"] == "Complete")
    eq(complete["status"], "False")


@test("precheck batches carry no commit action")
def t_precheck_no_commit():
    cfg = Config()
    records, _ = _sample_records()
    m = build_batch_manifest("b", "ns", records, cfg, run_id="r", wave=1, precheck_only=True)
    control = m["spec"]["defaultSpec"]["controlAction"]
    assert "commitAction" not in control, control
    m2 = build_batch_manifest("b", "ns", records, cfg, run_id="r", wave=1, precheck_only=False)
    eq(m2["spec"]["defaultSpec"]["controlAction"]["commitAction"], "Auto")


# ------------------------------------------------------------- inventory
def _sample_records():
    cfg = Config()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "inv.csv")
        write_csv(path, [
            ["web-01", "vm-1001", "ns-a", "sub-a", "4000", "1"],
            ["web-02", "vm-1002", "ns-a", "sub-a", "4000", "1"],
        ], ["vm_name", "moref", "namespace", "subnet", "device_key", "wave"])
        return load_inventory(path, cfg)


@test("inventory accepts column aliases")
def t_inventory_aliases():
    cfg = Config()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "inv.csv")
        write_csv(path, [["web-01", "vm-1001", "ns-a", "sub-a"]],
                  ["Name", "virtualMachineID", "Target Namespace", "Subnet Name"])
        records, _ = load_inventory(path, cfg)
        eq(len(records), 1)
        eq(records[0].moref, "vm-1001")
        eq(records[0].namespace, "ns-a")
        eq(records[0].nics[0].subnet, "sub-a")
        eq(records[0].nics[0].device_key, 4000)


@test("inventory rejects duplicate morefs")
def t_inventory_dupes():
    cfg = Config()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "inv.csv")
        write_csv(path, [
            ["a", "vm-1", "ns-a", "s"],
            ["b", "vm-1", "ns-a", "s"],
        ], ["vm_name", "moref", "namespace", "subnet"])
        try:
            load_inventory(path, cfg)
        except InventoryError as exc:
            assert "duplicate" in str(exc), exc
            return
        raise AssertionError("expected InventoryError")


@test("multi-NIC rows work via numbered columns and JSON")
def t_inventory_multinic():
    cfg = Config()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "inv.csv")
        write_csv(path, [
            ["a", "vm-1", "ns-a", "front", "4000", "back", "4001"],
        ], ["vm_name", "moref", "namespace", "nic1_subnet", "nic1_device_key",
            "nic2_subnet", "nic2_device_key"])
        records, _ = load_inventory(path, cfg)
        eq(len(records[0].nics), 2)
        eq(records[0].nics[1].subnet, "back")
        eq(records[0].nics[1].device_key, 4001)

        path2 = os.path.join(d, "inv2.csv")
        write_csv(path2, [
            ["a", "vm-2", "ns-a", json.dumps([
                {"deviceKey": 4000, "subnet": "x"},
                {"deviceKey": 4001, "subnet": "y", "kind": "SubnetSet"},
            ])],
        ], ["vm_name", "moref", "namespace", "nics"])
        records2, _ = load_inventory(path2, cfg)
        eq(len(records2[0].nics), 2)
        eq(records2[0].nics[1].subnet_kind, "SubnetSet")


@test("skip column excludes rows")
def t_inventory_skip():
    cfg = Config()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "inv.csv")
        write_csv(path, [
            ["a", "vm-1", "ns-a", "s", ""],
            ["b", "vm-2", "ns-a", "s", "yes"],
        ], ["vm_name", "moref", "namespace", "subnet", "skip"])
        records, _ = load_inventory(path, cfg)
        eq([r.moref for r in records], ["vm-1"])


@test("bad moref format warns but does not fail")
def t_inventory_moref_warning():
    cfg = Config()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "inv.csv")
        write_csv(path, [["a", "500 (vm-9)", "ns-a", "s"]],
                  ["vm_name", "moref", "namespace", "subnet"])
        records, warnings = load_inventory(path, cfg)
        eq(len(records), 1)
        assert any("moref" in w for w in warnings), warnings


# --------------------------------------------------------------- planner
@test("batches respect size and never span namespace, wave or group")
def t_planner_grouping():
    cfg = Config()
    cfg.batch_size = 3
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "inv.csv")
        rows = []
        for i in range(7):
            rows.append(["vm{}".format(i), "vm-{}".format(1000 + i), "ns-a", "sub-a", "4000", "1"])
        for i in range(4):
            rows.append(["vmb{}".format(i), "vm-{}".format(2000 + i), "ns-b", "sub-b", "4000", "2"])
        write_csv(path, rows, ["vm_name", "moref", "namespace", "subnet", "device_key", "wave"])
        records, _ = load_inventory(path, cfg)

    batches = plan_batches(records, cfg, "run1", "import")
    eq(len(batches), 5, "3+3+1 for ns-a, 3+1 for ns-b")
    for b in batches:
        eq(len({r.namespace for r in b.records}), 1)
        eq(len({r.wave for r in b.records}), 1)
        assert b.size <= 3
    eq(len({b.name for b in batches}), 5, "names unique")
    for b in batches:
        assert len(b.name) <= 63, b.name
        assert b.name.islower() or "-" in b.name


@test("retry salt produces fresh batch names")
def t_planner_salt():
    cfg = Config()
    records, _ = _sample_records()
    a = plan_batches(records, cfg, "run1", "import")
    b = plan_batches(records, cfg, "run1", "import", name_salt="run1#r2")
    assert a[0].name != b[0].name, (a[0].name, b[0].name)


@test("a precheck and an import never name the same child ImportOperation")
def t_planner_operation_names_are_per_batch():
    # Observed 2026-09-21: the operator names each child ImportOperation after
    # its operation and owns it from the batch. When a precheck batch and the
    # import that follows both asked for "ubuntu-3-3079", the import could
    # neither create nor adopt that object and reported
    # "number of operations from status: 0 does not match ... from spec: 2"
    # until the precheck batch was deleted.
    cfg = Config()
    records, _ = _sample_records()
    pre = plan_batches(records, cfg, "run1", "precheck")
    imp = plan_batches(records, cfg, "run1", "import")

    def op_names(batches):
        return [o["name"] for b in batches for o in b.manifest["spec"]["operations"]]

    pre_names, imp_names = op_names(pre), op_names(imp)
    eq(len(pre_names), len(records))
    assert not (set(pre_names) & set(imp_names)), \
        "precheck and import share a child name: {}".format(set(pre_names) & set(imp_names))
    for name in pre_names + imp_names:
        assert len(name) <= 63, (name, len(name))
        assert name == name.lower().strip("-"), name

    # Re-planning the same batch must reproduce the names, or a resumed run
    # would stop recognising the children it already applied.
    eq(op_names(plan_batches(records, cfg, "run1", "import")), imp_names, "stable")
    # A retry salt makes a new batch, so its children are new objects too.
    salted = op_names(plan_batches(records, cfg, "run1", "import", name_salt="run1#r2"))
    assert not (set(salted) & set(imp_names)), salted


@test("two VMs that slug to the same name stay distinct within one batch")
def t_render_operation_name_within_batch():
    from vcfaimport.inventory import Nic, VmRecord
    from vcfaimport.render import batch_discriminator
    cfg = Config()
    nic = Nic(device_key=4000, subnet="sub-a", subnet_kind="Subnet",
              subnet_api_group="crd.nsx.vmware.com")
    # Same display name, and morefs whose trailing digits match as well.
    records = [VmRecord(moref="vm-100", vm_name="dup", namespace="ns-a", nics=[nic], wave=1),
               VmRecord(moref="vmx-100", vm_name="dup", namespace="ns-a", nics=[nic], wave=1)]
    manifest = build_batch_manifest("imp-w1-g-001-abcde", "ns-a", records, cfg,
                                    run_id="run1", wave=1)
    names = [o["name"] for o in manifest["spec"]["operations"]]
    eq(len(set(names)), 2, "names must stay distinct")
    tag = batch_discriminator("imp-w1-g-001-abcde")
    for name in names:
        assert tag in name, (name, tag)
        assert len(name) <= 63, name


@test("the fake operator reproduces the child-name deadlock")
def t_fake_operator_name_collision():
    # Guards the simulator itself: if it stopped modelling the collision, the
    # e2e runs would pass even with per-batch naming reverted.
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_kubectl

    def batch(name, ops):
        return {"metadata": {"name": name, "namespace": "ns-a"},
                "spec": {"defaultSpec": {"mode": "preserve"},
                         "operations": [{"name": o, "spec": {"virtualMachineID": "vm-1"}}
                                        for o in ops]}}

    data = {"batches": {"ns-a/pre-1": batch("pre-1", ["ubuntu-3-3079"]),
                        "ns-a/imp-1": batch("imp-1", ["ubuntu-3-3079"])},
            "polls": {}}
    blocked = fake_kubectl.build_status(data["batches"]["ns-a/imp-1"], 99, "ns-a/imp-1", data)
    eq(blocked["readyCount"], 0)
    ready_for_import = [c for c in blocked["conditions"] if c["type"] == "ReadyForImport"][0]
    eq(ready_for_import["status"], "False")
    assert "does not match number of operations from spec: 1" in ready_for_import["message"], \
        ready_for_import["message"]
    eq(fake_kubectl.child_operations(data["batches"]["ns-a/imp-1"], blocked), [],
       "a blocked batch creates no children")

    # The first claimant is unaffected, and distinct names free both batches.
    ok = fake_kubectl.build_status(data["batches"]["ns-a/pre-1"], 99, "ns-a/pre-1", data)
    assert "_blocked_on" not in ok, ok
    data["batches"]["ns-a/imp-1"] = batch("imp-1", ["ubuntu-3-3079-008cf"])
    freed = fake_kubectl.build_status(data["batches"]["ns-a/imp-1"], 99, "ns-a/imp-1", data)
    assert "_blocked_on" not in freed, freed


# ---------------------------------------------------------------- status
@test("phase strings classify into the right buckets")
def t_status_classify():
    cfg = Config()
    cases = {
        "Succeeded": "succeeded", "Completed": "succeeded", "Committed": "succeeded",
        "Failed": "failed", "PrecheckFailed": "failed", "Error": "failed",
        "RolledBack": "rolled_back", "Timeout": "failed",
        "AwaitingCommit": "awaiting_commit", "WaitingForCommit": "awaiting_commit",
        "InProgress": "running", "Migrating": "running", "Pending": "running",
        "RollingBack": "running", "RollbackInProgress": "running",
        "RollbackComplete": "rolled_back", "RollbackFailed": "failed",
        "Bananas": "unknown",
    }
    for phase, want in cases.items():
        eq(classify(phase, cfg), want, phase)


@test("per-VM status is read from child ImportOperations")
def t_status_children():
    cfg = Config()
    batch = {"metadata": {"name": "b1", "namespace": "ns"},
             "status": {"phase": "InProgress"}}
    children = [
        {"metadata": {"name": "b1-op1", "namespace": "ns",
                      "ownerReferences": [{"name": "b1"}]},
         "spec": {"virtualMachineID": "vm-1"},
         "status": {"phase": "Succeeded"}},
        {"metadata": {"name": "b1-op2", "namespace": "ns",
                      "ownerReferences": [{"name": "b1"}]},
         "spec": {"virtualMachineID": "vm-2"},
         "status": {"phase": "Failed", "message": "no VM Tools"}},
        {"metadata": {"name": "other-op", "namespace": "ns",
                      "ownerReferences": [{"name": "someone-else"}]},
         "spec": {"virtualMachineID": "vm-3"},
         "status": {"phase": "Succeeded"}},
    ]
    st = batch_status(batch, cfg, children)
    eq(st.source, "importoperations")
    by = st.by_moref()
    eq(set(by), {"vm-1", "vm-2"}, "other batches' operations are ignored")
    eq(by["vm-1"].bucket, "succeeded")
    eq(by["vm-2"].bucket, "failed")
    eq(by["vm-2"].message, "no VM Tools")


@test("inline status.operations is used when no child CRs exist")
def t_status_inline():
    cfg = Config()
    batch = {"metadata": {"name": "b1"}, "status": {"phase": "InProgress", "operations": [
        {"name": "op1", "virtualMachineID": "vm-1", "phase": "Succeeded"},
        {"name": "op2", "virtualMachineID": "vm-2", "phase": "AwaitingCommit"},
    ]}}
    st = batch_status(batch, cfg, [])
    eq(st.source, "status.operations")
    eq(st.by_moref()["vm-2"].bucket, "awaiting_commit")
    eq(st.bucket, "awaiting_commit", "batch verdict derives from its operations")


@test("a batch with no status yet counts as running, not unknown")
def t_status_empty():
    cfg = Config()
    st = batch_status({"metadata": {"name": "b"}}, cfg, [])
    eq(st.bucket, "running")


@test("the operator's real precheck status reads as precheck passed")
def t_status_operator_precheck():
    """Byte-for-byte the shape seen in the lab on 2026-09-18 after the DNS fix."""
    cfg = Config()
    batch = {"metadata": {"name": "pre-w1-testing-vpc-k826r-001-418fa"},
             "spec": {"defaultSpec": {"controlAction": {"precheckOnly": True}},
                      "operations": [{"name": "ubuntu-2-3064",
                                      "spec": {"virtualMachineID": "vm-3064"}}]},
             "status": {"conditions": [
                 {"type": "Complete", "status": "False", "reason": "ObjectNotReady",
                  "message": "Operations are not ready. Operation Names: ubuntu-2-3064"},
                 {"type": "ReadyForCommit", "status": "False", "reason": "ObjectNotReady",
                  "message": "Operations are not ready. Operation Names: ubuntu-2-3064"},
                 {"type": "ReadyForImport", "status": "True", "reason": "True", "message": ""}],
                 "readyCount": 1, "readyOps": ["ubuntu-2-3064"]}}
    st = batch_status(batch, cfg, [], stage="precheck")
    eq(st.bucket, "succeeded", "Complete=False must not mask a passed precheck")
    eq(st.source, "operator-conditions")
    eq(st.by_op_name()["ubuntu-2-3064"].bucket, "succeeded")
    # Without the stage hint, precheckOnly in the spec decides.
    eq(batch_status(batch, cfg, []).bucket, "succeeded")
    # The same object read as an *import* batch is merely "imported, not committed".
    eq(batch_status(batch, cfg, [], stage="import").bucket, "running")


@test("a precheck child with Complete=False does not mask the batch's pass")
def t_status_child_does_not_mask_pass():
    """The lab case that looped: batch ReadyForImport=True, child present."""
    cfg = Config()
    batch = {"metadata": {"name": "pre-w1-x-001-c9672"},
             "spec": {"defaultSpec": {"controlAction": {"precheckOnly": True}}},
             "status": {"conditions": [
                 {"type": "Complete", "status": "False", "reason": "ObjectNotReady"},
                 {"type": "ReadyForCommit", "status": "False", "reason": "ObjectNotReady"},
                 {"type": "ReadyForImport", "status": "True", "reason": "True"}],
                 "readyCount": 1, "readyOps": ["ubuntu-2-3064"]}}
    child_same = {"metadata": {"name": "ubuntu-2-3064",
                               "ownerReferences": [{"name": "pre-w1-x-001-c9672"}]},
                  "spec": {"virtualMachineID": "vm-3064"},
                  "status": {"conditions": [
                      {"type": "Complete", "status": "False", "reason": "ObjectNotReady"},
                      {"type": "ReadyForImport", "status": "True"}]}}
    st = batch_status(batch, cfg, [child_same], stage="precheck")
    eq(st.by_moref()["vm-3064"].bucket, "succeeded")
    child_opaque = {"metadata": {"name": "ubuntu-2-3064",
                                 "ownerReferences": [{"name": "pre-w1-x-001-c9672"}]},
                    "spec": {"virtualMachineID": "vm-3064"},
                    "status": {"someField": "Whatever"}}
    st = batch_status(batch, cfg, [child_opaque], stage="precheck")
    eq(st.by_moref()["vm-3064"].bucket, "unknown", "opaque child is unknown...")
    eq(st.bucket, "succeeded", "...but the batch verdict stands; the engine defers to it")


@test("a child's Completed condition is read as committed, not awaiting commit")
def t_status_child_completed_spelling():
    """Observed 2026-09-23: batches say `Complete`, children say `Completed`.

    Reading only the batch spelling left `complete` as None on every child, so
    the ReadyForCommit branch won and two committed VMs sat at awaiting_commit
    through every refresh -- child status wins over the batch verdict.
    """
    cfg = Config()
    batch = {"metadata": {"name": "imp-w1-x-001-5271a"},
             "spec": {"defaultSpec": {"controlAction": {"commitAction": "Auto"}}},
             "status": {"conditions": [
                 {"type": "Complete", "status": "True", "reason": "True"},
                 {"type": "ReadyForCommit", "status": "True", "reason": "True"},
                 {"type": "ReadyForImport", "status": "True", "reason": "True"}],
                 "readyCount": 2}}
    child = {"metadata": {"name": "ubuntu-5-3082-92323",
                          "ownerReferences": [{"name": "imp-w1-x-001-5271a"}]},
             "spec": {"virtualMachineID": "vm-3082"},
             "status": {"conditions": [
                 {"type": "Completed", "status": "True", "reason": "True"},
                 {"type": "ReadyForCommit", "status": "True", "reason": "True"},
                 {"type": "PrecheckSucceeded", "status": "True", "reason": "True"},
                 {"type": "VirtualMachineCreated", "status": "True", "reason": "True"}],
                 "completionTime": "2026-09-23T19:52:11Z"}}
    st = batch_status(batch, cfg, [child], stage="import")
    eq(st.by_moref()["vm-3082"].bucket, "succeeded", "Completed means committed")

    # Still at the gate: ReadyForCommit true, Completed explicitly false.
    child["status"]["conditions"][0] = {"type": "Completed", "status": "False",
                                        "reason": "ObjectNotReady"}
    child["status"].pop("completionTime")
    st = batch_status(batch, cfg, [child], stage="import")
    eq(st.by_moref()["vm-3082"].bucket, "awaiting_commit", "the gate still holds")

    # The batch spelling must keep working -- batches really do say `Complete`.
    eq(operator_status(batch, cfg, "import").bucket, "succeeded")


@test("a child's PrecheckSucceeded verdict is read directly")
def t_status_child_precheck_condition():
    """Observed 2026-09-21/23: a precheck child carries PrecheckSucceeded alone.

    It is not in the batch vocabulary, so a *failed* precheck child used to fall
    through to the phase heuristics, read as "still running", and hang the batch
    until batch_timeout_minutes (90) expired.
    """
    cfg = Config()
    batch = {"metadata": {"name": "pre-w1-x-001-b5f31"},
             "spec": {"defaultSpec": {"controlAction": {"precheckOnly": True}}},
             "status": {"conditions": [
                 {"type": "ReadyForImport", "status": "False", "reason": "ObjectNotReady",
                  "message": "Operations are not ready. Operation Names: ubuntu-3-3079-6df9d"}]}}

    def child(status, reason, message=""):
        return {"metadata": {"name": "ubuntu-3-3079-6df9d",
                             "ownerReferences": [{"name": "pre-w1-x-001-b5f31"}]},
                "spec": {"virtualMachineID": "vm-3079"},
                "status": {"conditions": [{"type": "PrecheckSucceeded", "status": status,
                                           "reason": reason, "message": message}]}}

    st = batch_status(batch, cfg, [child("True", "True")], stage="precheck")
    eq(st.by_moref()["vm-3079"].bucket, "succeeded")

    failed = child("False", "VirtualMachineAlreadyExists",
                   "virtual machine with name ubuntu-3 already exists at target folder")
    st = batch_status(batch, cfg, [failed], stage="precheck")
    op = st.by_moref()["vm-3079"]
    eq(op.bucket, "failed", "a named verdict is a failure, not 'still working'")
    eq(op.phase, "VirtualMachineAlreadyExists")
    assert "already exists" in (op.message or ""), op.message

    # A transient reason is still just work in progress.
    st = batch_status(batch, cfg, [child("False", "ObjectNotReady")], stage="precheck")
    eq(st.by_moref()["vm-3079"].bucket, "running")


@test("the operator's DNS stall reads as running, with the reason exposed")
def t_status_operator_stall():
    cfg = Config()
    batch = {"metadata": {"name": "b"},
             "spec": {"defaultSpec": {"controlAction": {"precheckOnly": True}}},
             "status": {"conditions": [{
                 "type": "ReadyForImport", "status": "False", "reason": "ObjectNotReady",
                 "message": "failed to get cluster anti-affinity rules: Post "
                            "\"https://vcf-m01-vc01.thepromisedlan.ca:443/sdk\": dial tcp: "
                            "lookup vcf-m01-vc01.thepromisedlan.ca on 127.0.0.53:53: i/o timeout"}]}}
    st = batch_status(batch, cfg, [], stage="precheck")
    eq(st.bucket, "running", "ObjectNotReady is not a verdict")
    eq(st.phase, "ObjectNotReady")
    assert "127.0.0.53" in (st.message or ""), "the reason must survive to the VM row"


@test("the operator's commit gate and completion read correctly")
def t_status_operator_import():
    cfg = Config()
    base = {"metadata": {"name": "imp"},
            "spec": {"defaultSpec": {"controlAction": {"commitAction": "Wait"}}}}
    held = dict(base, status={"conditions": [
        {"type": "ReadyForImport", "status": "True"},
        {"type": "ReadyForCommit", "status": "True"},
        {"type": "Complete", "status": "False", "reason": "ObjectNotReady"}],
        "readyOps": ["a-1", "b-2"]})
    st = batch_status(held, cfg, [], stage="import")
    eq(st.bucket, "awaiting_commit")
    eq({o.key: o.bucket for o in st.operations}, {"a-1": "awaiting_commit", "b-2": "awaiting_commit"})

    done = dict(base, status={"conditions": [
        {"type": "ReadyForImport", "status": "True"},
        {"type": "ReadyForCommit", "status": "True"},
        {"type": "Complete", "status": "True"}],
        "readyOps": ["a-1", "b-2"], "completedOps": ["a-1", "b-2"]})
    eq(batch_status(done, cfg, [], stage="import").bucket, "succeeded")

    partial = dict(base, status={"conditions": [
        {"type": "ReadyForImport", "status": "True"},
        {"type": "ReadyForCommit", "status": "False", "reason": "OperationsFailed",
         "message": "VM Tools not running: b-2"},
        {"type": "Complete", "status": "False", "reason": "OperationsFailed"}],
        "readyOps": ["a-1"], "failedOps": ["b-2"]})
    st = batch_status(partial, cfg, [], stage="import")
    ops = {o.key: o.bucket for o in st.operations}
    eq(ops["b-2"], "failed")
    eq(ops["a-1"], "running", "a-1 is imported but its batch is not complete")


@test("conditions are honoured when no phase field exists")
def t_status_conditions():
    cfg = Config()
    batch = {"metadata": {"name": "b"}, "status": {"conditions": [
        {"type": "Succeeded", "status": "False", "reason": "PrecheckFailed",
         "message": "subnet not reachable"}]}}
    st = batch_status(batch, cfg, [])
    eq(st.bucket, "failed")
    eq(st.message, "subnet not reachable")


# ----------------------------------------------------------- discovery
class _Row(dict):
    """Stand-in for a sqlite3.Row in unit tests."""
    def __getitem__(self, key):
        return dict.get(self, key, "")


def _disc_row(**kw):
    base = {"moref": "vm-1", "name": "web-01", "power_state": "POWERED_ON", "cluster": "PROD-CL01",
            "folder": "Production", "networks": "VLAN197-Prod", "guest_os": "RHEL_8_64",
            "tools_status": "RUNNING", "nics_json": '[{"device_key":4000,"network_name":"VLAN197-Prod"}]',
            "selected": 0, "namespace": "", "wave": 0, "notes": "", "cpu_count": 2,
            "memory_mb": 4096, "host": ""}
    base.update(kw)
    return _Row(base)


@test("selection filters combine as AND")
def t_filter_and():
    from vcfaimport.discovery import Filters, apply_filters
    rows = [
        _disc_row(moref="vm-1", name="web-01", cluster="PROD-CL01"),
        _disc_row(moref="vm-2", name="web-02", cluster="DMZ-CL01"),
        _disc_row(moref="vm-3", name="db-01", cluster="PROD-CL01"),
        _disc_row(moref="vm-4", name="web-03", cluster="PROD-CL01", power_state="POWERED_OFF"),
    ]
    eq([r["moref"] for r in apply_filters(rows, Filters(name=["web-*"]))],
       ["vm-1", "vm-2", "vm-4"])
    eq([r["moref"] for r in apply_filters(rows, Filters(name=["web-*"], cluster=["PROD-*"]))],
       ["vm-1", "vm-4"])
    eq([r["moref"] for r in apply_filters(
        rows, Filters(name=["web-*"], cluster=["PROD-*"], powered_on=True))], ["vm-1"])
    eq([r["moref"] for r in apply_filters(rows, Filters(exclude_name=["db-*"]))],
       ["vm-1", "vm-2", "vm-4"])
    eq([r["moref"] for r in apply_filters(rows, Filters(regex=r"^db"))], ["vm-3"])
    eq([r["moref"] for r in apply_filters(rows, Filters(tools_running=True))],
       ["vm-1", "vm-2", "vm-3", "vm-4"])


@test("folder filter covers the subtree unless told otherwise")
def t_filter_folder():
    from vcfaimport.discovery import Filters, apply_filters, folder_matches
    rows = [
        _disc_row(moref="vm-1", folder="Production"),
        _disc_row(moref="vm-2", folder="Production/Web"),
        _disc_row(moref="vm-3", folder="Production/Web/Tier1"),
        _disc_row(moref="vm-4", folder="ProductionOld"),
        _disc_row(moref="vm-5", folder="DMZ"),
        _disc_row(moref="vm-6", folder=""),                       # datacenter root
    ]
    pick = lambda f: [r["moref"] for r in apply_filters(rows, f)]  # noqa: E731
    eq(pick(Filters(folder=["Production"])), ["vm-1", "vm-2", "vm-3"], "subtree by default")
    eq(pick(Filters(folder=["Production"], folder_exact=True)), ["vm-1"], "--no-subfolders")
    eq(pick(Filters(folder=["production/web"])), ["vm-2", "vm-3"], "case-insensitive")
    eq(pick(Filters(folder=["/Production/Web/"])), ["vm-2", "vm-3"], "edge slashes ignored")
    eq(pick(Filters(folder=["Production\\Web"])), ["vm-2", "vm-3"], "backslashes accepted")
    eq(pick(Filters(folder=["*/Tier1"])), ["vm-3"], "glob")
    eq(pick(Filters(folder=["Production", "DMZ"])), ["vm-1", "vm-2", "vm-3", "vm-5"], "repeatable")
    assert not folder_matches("ProductionOld", ["Production"]), "prefix must be a whole segment"
    eq(pick(Filters(folder=["/"])), [r["moref"] for r in rows], "root recursive = everything")
    eq(pick(Filters(folder=["/"], folder_exact=True)), ["vm-6"], "root exact = loose VMs only")


@test("folder map: the most specific folder wins")
def t_folder_map():
    from vcfaimport.discovery import FolderMapping, match_folder_map, stage
    mapping = {
        "Production": FolderMapping(namespace="ns-prod", wave=2),
        "Production/Web": FolderMapping(namespace="ns-web", wave=1, group="web"),
        "Legacy/*": FolderMapping(namespace="ns-legacy"),
    }
    eq(match_folder_map("Production/App", mapping).namespace, "ns-prod")
    eq(match_folder_map("Production/Web/Tier1", mapping).namespace, "ns-web", "deeper wins")
    eq(match_folder_map("Legacy/Decommission", mapping).namespace, "ns-legacy", "glob")
    eq(match_folder_map("DMZ", mapping), None)

    cfg = Config()
    rows = [
        _disc_row(moref="vm-1", folder="Production/Web/Tier1"),
        _disc_row(moref="vm-2", folder="Production/App"),
        _disc_row(moref="vm-3", folder="DMZ"),
        _disc_row(moref="vm-4", folder="Production/App", namespace="ns-explicit"),
    ]
    result = stage(rows, cfg, folder_mapping=mapping)
    by = {r.moref: r for r in result.records}
    eq(by["vm-1"].namespace, "ns-web")
    eq(by["vm-1"].wave, 1)
    eq(by["vm-1"].group, "web", "group from the folder map separates batches")
    eq(by["vm-2"].namespace, "ns-prod")
    eq(by["vm-2"].wave, 2)
    eq(by["vm-4"].namespace, "ns-explicit", "explicit namespace beats the folder map")
    assert "vm-3" not in by, "unmapped folder is reported, not guessed"
    eq(result.unmapped_folders, {"DMZ": 1})


@test("folder map beats network map for namespace; both can supply subnets")
def t_folder_map_precedence():
    from vcfaimport.discovery import FolderMapping, NetworkMapping, stage
    cfg = Config()
    folders = {"Production": FolderMapping(namespace="ns-from-folder", wave=3)}
    nets = {"VLAN197-Prod": NetworkMapping(namespace="ns-from-net", subnet="s197", wave=1)}
    rows = [_disc_row(moref="vm-1", folder="Production/Web", networks="VLAN197-Prod",
                      nics_json='[{"device_key":4000,"network_name":"VLAN197-Prod"}]')]
    result = stage(rows, cfg, mapping=nets, folder_mapping=folders)
    rec = result.records[0]
    eq(rec.namespace, "ns-from-folder")
    eq(rec.wave, 3, "folder wave wins over network wave")
    eq(rec.nics[0].subnet, "s197", "subnet still comes from the network map")


@test("the folder tree counts direct and subtree VMs")
def t_folder_tree():
    from vcfaimport.vcenter import DiscoveredVm
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.upsert_discovered([
            DiscoveredVm(moref="vm-1", name="a", folder="Production/Web", datacenter="DC1"),
            DiscoveredVm(moref="vm-2", name="b", folder="Production/Web/Tier1", datacenter="DC1"),
            DiscoveredVm(moref="vm-3", name="c", folder="DMZ", datacenter="DC1"),
            DiscoveredVm(moref="vm-4", name="d", folder="", datacenter="DC1"),
        ])
        store.set_selected(["vm-2"], True)
        tree = {n["path"]: n for n in store.folder_tree()}
        eq(tree[""]["direct"], 1)
        eq(tree[""]["subtree"], 4)
        eq(tree["Production"]["direct"], 0, "an ancestor with no VMs still appears")
        eq(tree["Production"]["subtree"], 2)
        eq(tree["Production/Web"]["direct"], 1)
        eq(tree["Production/Web"]["subtree"], 2)
        eq(tree["Production/Web"]["selected"], 1)
        eq(tree["Production/Web/Tier1"]["direct"], 1)
        store.close()


@test("network filter matches any of a VM's portgroups")
def t_filter_network():
    from vcfaimport.discovery import Filters, apply_filters
    rows = [
        _disc_row(moref="vm-1", networks="VLAN197-Prod"),
        _disc_row(moref="vm-2", networks="VLAN200-DB,VLAN210-App"),
        _disc_row(moref="vm-3", networks="DMZ-Uplink"),
    ]
    eq([r["moref"] for r in apply_filters(rows, Filters(network=["VLAN210-App"]))], ["vm-2"])
    eq([r["moref"] for r in apply_filters(rows, Filters(network=["VLAN*"]))], ["vm-1", "vm-2"])


@test("identifiers resolve by moref or by name, flagging ambiguity")
def t_resolve_identifiers():
    from vcfaimport.discovery import resolve_identifiers
    rows = [_disc_row(moref="vm-1", name="web-01"),
            _disc_row(moref="vm-2", name="web-02"),
            _disc_row(moref="vm-3", name="web-02")]
    morefs, unmatched, ambiguous = resolve_identifiers(
        rows, ["vm-1", "web-01", "web-02", "nope"])
    eq(morefs, ["vm-1", "vm-1"])
    eq(unmatched, ["nope"])
    eq(ambiguous, ["web-02"], "duplicate names must not be guessed")


@test("network map assigns namespace and subnet, globs included")
def t_network_map():
    from vcfaimport.discovery import load_network_map, stage
    cfg = Config()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "map.csv")
        write_csv(path, [
            ["VLAN197-Prod", "ns-prod", "subnet-197", "1"],
            ["VLAN2*", "ns-db", "subnet-2xx", "2"],
        ], ["portgroup", "namespace", "subnet", "wave"])
        mapping = load_network_map(path)

    rows = [
        _disc_row(moref="vm-1", name="web-01", networks="VLAN197-Prod",
                  nics_json='[{"device_key":4000,"network_name":"VLAN197-Prod"}]'),
        _disc_row(moref="vm-2", name="db-01", networks="VLAN200-DB",
                  nics_json='[{"device_key":4000,"network_name":"VLAN200-DB"}]'),
        _disc_row(moref="vm-3", name="odd-01", networks="Unmapped-PG",
                  nics_json='[{"device_key":4000,"network_name":"Unmapped-PG"}]'),
    ]
    result = stage(rows, cfg, mapping=mapping)
    eq(len(result.records), 2)
    eq(result.records[0].namespace, "ns-prod")
    eq(result.records[0].nics[0].subnet, "subnet-197")
    eq(result.records[1].namespace, "ns-db", "glob match")
    eq(result.records[1].wave, 2, "wave comes from the map")
    eq(len(result.problems), 1, "the unmapped VM is reported, not guessed")
    assert "Unmapped-PG" in result.unmapped_networks


@test("a namespace set on the VM wins over the network map")
def t_stage_namespace_precedence():
    from vcfaimport.discovery import NetworkMapping, stage
    cfg = Config()
    mapping = {"VLAN197-Prod": NetworkMapping(namespace="ns-from-map", subnet="s1")}
    rows = [_disc_row(moref="vm-1", namespace="ns-explicit", networks="VLAN197-Prod")]
    result = stage(rows, cfg, mapping=mapping)
    eq(result.records[0].namespace, "ns-explicit")
    eq(result.records[0].nics[0].subnet, "s1", "subnet still comes from the map")


@test("multi-NIC VMs stage every mapped adapter with its own device key")
def t_stage_multinic():
    from vcfaimport.discovery import NetworkMapping, stage
    cfg = Config()
    mapping = {"front": NetworkMapping(namespace="ns", subnet="sub-front"),
               "back": NetworkMapping(namespace="ns", subnet="sub-back")}
    rows = [_disc_row(moref="vm-1", networks="front,back",
                      nics_json='[{"device_key":4000,"network_name":"front"},'
                                '{"device_key":4001,"network_name":"back"}]')]
    result = stage(rows, cfg, mapping=mapping)
    nics = result.records[0].nics
    eq(len(nics), 2)
    eq([(n.device_key, n.subnet) for n in nics], [(4000, "sub-front"), (4001, "sub-back")])


@test("picker export CSV round-trips through the selection reader")
def t_selection_file():
    from vcfaimport.discovery import read_selection_file
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "selection.csv")
        write_csv(path, [
            ["web-01", "vm-1", "ns-a", "1"],
            ["web-02", "vm-2", "ns-b", "2"],
        ], ["vm_name", "moref", "namespace", "wave"])
        idents, namespaces, waves = read_selection_file(path)
        eq(idents, ["vm-1", "vm-2"])
        eq(namespaces, {"vm-1": "ns-a", "vm-2": "ns-b"})
        eq(waves, {"vm-1": 1, "vm-2": 2})

        plain = os.path.join(d, "list.txt")
        Path(plain).write_text("# a comment\nvm-9\nweb-07\n\n", encoding="utf-8")
        idents2, _, _ = read_selection_file(plain)
        eq(idents2, ["vm-9", "web-07"])


@test("the HTML picker embeds the inventory safely")
def t_picker_html():
    from vcfaimport.discovery import write_picker
    rows = [_disc_row(moref="vm-1", name='evil</script><img src=x>'),
            _disc_row(moref="vm-2", name="ok-02")]
    with tempfile.TemporaryDirectory() as d:
        path = write_picker(rows, os.path.join(d, "picker.html"))
        doc = Path(path).read_text(encoding="utf-8")
        assert "</script><img" not in doc, "payload must not break out of the script tag"
        assert "vm-1" in doc and "ok-02" in doc
        assert "Download selection.csv" in doc
        assert "prefers-color-scheme" in doc


@test("vCenter NIC parsing keeps device keys from both API shapes")
def t_vcenter_nics():
    from vcfaimport.vcenter import _parse_nics
    networks = {"dvportgroup-100": "VLAN197-Prod"}
    modern = {"nics": {"4000": {"backing": {"type": "DISTRIBUTED_PORTGROUP",
                                            "network": "dvportgroup-100"},
                                "mac_address": "00:50:56:aa:bb:cc"}}}
    nics = _parse_nics(modern, networks)
    eq(len(nics), 1)
    eq(nics[0].device_key, 4000)
    eq(nics[0].network_name, "VLAN197-Prod")

    legacy = {"nics": [{"key": "4001", "value": {"backing": {"network": "dvportgroup-100"}}}]}
    nics2 = _parse_nics(legacy, networks)
    eq(nics2[0].device_key, 4001)
    eq(nics2[0].network_name, "VLAN197-Prod")


# ------------------------------------------------------------- tracking
def _store(tmp):
    from vcfaimport.state import Store
    return Store(os.path.join(tmp, "state.db"))


def _record(store, moref="vm-1", name="web-01", ns="ns-a", wave=1):
    from vcfaimport.inventory import Nic, VmRecord
    rec = VmRecord(moref=moref, vm_name=name, namespace=ns,
                   nics=[Nic(4000, "sub-a", "Subnet", "crd.nsx.vmware.com")], wave=wave)
    store.sync_inventory([rec])
    return rec


@test("every state change is recorded as a transition")
def t_track_transitions():
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _record(store)
        store.set_vm_state("vm-1", vst.S_PRECHECK_RUNNING, stage="precheck", batch_name="pre-1")
        store.set_vm_state("vm-1", vst.S_PRECHECK_PASSED, stage="precheck")
        store.set_vm_state("vm-1", vst.S_IMPORTING, stage="import", batch_name="imp-1")
        store.set_vm_state("vm-1", vst.S_COMMITTED, stage="import",
                           target_resource="vm-a1b2c3d4")

        moves = store.transitions(moref="vm-1")
        eq([m["to_state"] for m in moves],
           ["pending", "precheck_running", "precheck_passed", "importing", "committed"])
        eq(moves[0]["from_state"], None, "the first entry is the VM joining the campaign")
        eq(moves[-1]["from_state"], "importing")
        eq(moves[3]["stage"], "import")
        eq(moves[3]["batch"], "imp-1")
        store.close()


@test("re-setting the same state does not create a duplicate entry")
def t_track_no_duplicates():
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _record(store)
        store.set_vm_state("vm-1", vst.S_IMPORTING, stage="import")
        for _ in range(5):
            store.set_vm_state("vm-1", vst.S_IMPORTING, phase="InProgress", stage="import")
        eq(len(store.transitions(moref="vm-1")), 2, "queued + importing only")
        store.close()


@test("milestone timestamps and the target resource are stamped")
def t_track_milestones():
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _record(store)
        store.set_vm_state("vm-1", vst.S_PRECHECK_RUNNING, stage="precheck")
        store.set_vm_state("vm-1", vst.S_PRECHECK_PASSED, stage="precheck")
        store.set_vm_state("vm-1", vst.S_IMPORTING, stage="import")
        store.set_vm_state("vm-1", vst.S_COMMITTED, stage="import",
                           target_resource="vm-a1b2c3d4")
        vm = store.get_vm("vm-1")
        for column in ("precheck_started_at", "precheck_finished_at",
                       "import_started_at", "committed_at"):
            assert vm[column], "{} was not stamped".format(column)
        eq(vm["target_resource"], "vm-a1b2c3d4")
        store.close()


@test("the JSONL ledger mirrors the transition table")
def t_track_ledger_file():
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _record(store)
        store.set_vm_state("vm-1", vst.S_PRECHECK_RUNNING, stage="precheck")
        store.set_vm_state("vm-1", vst.S_PRECHECK_FAILED, stage="precheck",
                           message="VM Tools not running")
        lines = [json.loads(x) for x in
                 Path(store.ledger_path).read_text(encoding="utf-8").splitlines() if x.strip()]
        eq(len(lines), 3)
        eq(lines[-1]["to_state"], "precheck_failed")
        eq(lines[-1]["message"], "VM Tools not running")
        eq(lines[-1]["moref"], "vm-1")
        assert lines[-1]["ts"], "every entry is timestamped"
        store.close()


@test("the ledger survives the database being deleted")
def t_track_ledger_durable():
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _record(store)
        store.set_vm_state("vm-1", vst.S_IMPORTING, stage="import")
        ledger = store.ledger_path
        store.close()

        os.remove(os.path.join(tmp, "state.db"))
        for suffix in ("-wal", "-shm"):
            extra = os.path.join(tmp, "state.db" + suffix)
            if os.path.exists(extra):
                os.remove(extra)

        store2 = _store(tmp)                     # fresh, empty database
        eq(len(store2.transitions()), 0)
        lines = [x for x in Path(ledger).read_text(encoding="utf-8").splitlines() if x.strip()]
        eq(len(lines), 2, "the movement record outlives the database")
        store2.close()


@test("provenance is copied from discovery when a VM is queued")
def t_track_provenance():
    from vcfaimport.vcenter import DiscoveredNic, DiscoveredVm
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.set_meta("vcenter", "vcenter.example.local")
        store.upsert_discovered([DiscoveredVm(
            moref="vm-1", name="web-01", power_state="POWERED_ON", cpu_count=4,
            memory_mb=8192, folder="Production", cluster="PROD-CL01", host="esx01.lab",
            tools_status="RUNNING",
            nics=[DiscoveredNic(4000, "dvportgroup-100", "VLAN197-Prod")])])
        _record(store)
        vm = store.get_vm("vm-1")
        eq(vm["src_cluster"], "PROD-CL01")
        eq(vm["src_folder"], "Production")
        eq(vm["src_networks"], "VLAN197-Prod")
        eq(vm["src_cpu"], 4)
        eq(vm["src_vcenter"], "vcenter.example.local")
        store.close()


@test("an older database gains the tracking columns on open")
def t_track_migration():
    import sqlite3 as sq
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.db")
        # A v1.0-shaped vms table, without any of the tracking columns.
        conn = sq.connect(path)
        conn.execute(
            "CREATE TABLE vms (moref TEXT PRIMARY KEY, vm_name TEXT NOT NULL DEFAULT '',"
            " namespace TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'preserve',"
            " wave INTEGER NOT NULL DEFAULT 1, grp TEXT NOT NULL DEFAULT '',"
            " nics_json TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',"
            " src_row INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',"
            " attempts INTEGER NOT NULL DEFAULT 0, batch_name TEXT, precheck_batch TEXT,"
            " operation_name TEXT, last_phase TEXT, message TEXT,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO vms(moref, namespace, created_at, updated_at)"
                     " VALUES('vm-9', 'ns-old', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
        conn.commit()
        conn.close()

        store = _store(tmp)                       # must migrate, not crash
        vm = store.get_vm("vm-9")
        eq(vm["namespace"], "ns-old", "existing rows survive")
        eq(vm["target_resource"], "")
        eq(vm["src_cluster"], "")
        from vcfaimport import state as vst
        store.set_vm_state("vm-9", vst.S_IMPORTING, stage="import")
        eq(len(store.transitions(moref="vm-9")), 1, "tracking works after migration")
        store.close()


@test("the tracker CSV carries source, target and timings")
def t_track_csv():
    from vcfaimport import report as rp
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _record(store)
        store.attach_provenance("vm-1", {"vcenter": "vc.lab", "cluster": "PROD-CL01",
                                         "folder": "Production", "networks": "VLAN197-Prod",
                                         "cpu_count": 4, "memory_mb": 8192})
        store.set_vm_state("vm-1", vst.S_IMPORTING, stage="import")
        store.set_vm_state("vm-1", vst.S_COMMITTED, stage="import",
                           target_resource="vm-a1b2c3d4")
        out = os.path.join(tmp, "tracker.csv")
        rp.write_csv(store, out)
        row = list(csv.DictReader(Path(out).open(encoding="utf-8")))[0]
        eq(row["moref"], "vm-1")
        eq(row["src_cluster"], "PROD-CL01")
        eq(row["target_resource"], "vm-a1b2c3d4")
        eq(row["state"], "committed")
        assert row["committed_at"], "committed_at must be populated"
        assert row["import_duration"], "duration must be computed"

        moves = os.path.join(tmp, "moves.csv")
        eq(rp.write_transitions_csv(store, moves), 3)
        store.close()


@test("a target resource name is read from operator status when present")
def t_track_target_extraction():
    from vcfaimport.status import batch_status
    cfg = Config()
    children = [{
        "metadata": {"name": "b1-op1", "ownerReferences": [{"name": "b1"}]},
        "spec": {"virtualMachineID": "vm-1"},
        "status": {"phase": "Succeeded", "targetVirtualMachine": "vm-a1b2c3d4"},
    }, {
        "metadata": {"name": "b1-op2", "ownerReferences": [{"name": "b1"}]},
        "spec": {"virtualMachineID": "vm-2"},
        "status": {"phase": "Succeeded"},
    }]
    stat = batch_status({"metadata": {"name": "b1"}, "status": {"phase": "Succeeded"}},
                        cfg, children)
    by = stat.by_moref()
    eq(by["vm-1"].target, "vm-a1b2c3d4")
    eq(by["vm-2"].target, None, "the operation's own name is never used as the target")


# -------------------------------------------------------------------- e2e
def _e2e_env(tmp, scenario, settle="2"):
    env = dict(os.environ)
    env["FAKE_KUBECTL_STATE"] = str(Path(tmp) / "cluster.json")
    env["FAKE_KUBECTL_SCENARIO"] = scenario
    env["FAKE_KUBECTL_SETTLE"] = settle
    return env


def _seed_cluster(tmp, namespaces, subnets):
    Path(tmp, "cluster.json").write_text(json.dumps({
        "namespaces": namespaces, "subnets": subnets,
        "batches": {}, "polls": {}, "applies": 0,
    }), encoding="utf-8")


def _write_config(tmp, **overrides):
    fake = (ROOT / "tools" / "fake_kubectl.py").as_posix()
    values = {
        "kubectl": "{} {}".format(Path(sys.executable).as_posix(), fake),
        "api_version": "mobility-operator.vmware.com/v1alpha3",
        "poll_interval_seconds": 1,
        "settle_seconds": 0,
        "batch_size": 4,
        "max_parallel_batches": 3,
        "workdir": Path(tmp, "run").as_posix(),
    }
    values.update(overrides)  # merge, so a key is never written twice
    lines = []
    for key, val in values.items():
        if isinstance(val, bool):
            lines.append("{} = {}".format(key, "true" if val else "false"))
        elif isinstance(val, str):
            lines.append('{} = "{}"'.format(key, val))
        else:
            lines.append("{} = {}".format(key, val))
    path = Path(tmp, "cfg.toml")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _run_cli(args, env, tmp, expect=0):
    cmd = [sys.executable, str(ROOT / "vcfa-import.py")] + args
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=tmp, timeout=300)
    if expect is not None and proc.returncode != expect:
        raise AssertionError(
            "exit {} (wanted {}) for {}\nSTDOUT:\n{}\nSTDERR:\n{}".format(
                proc.returncode, expect, " ".join(args), proc.stdout[-4000:], proc.stderr[-2000:]))
    return proc


def _inventory(tmp, count=10, namespaces=("ns-a", "ns-b"), waves=(1, 2)):
    path = Path(tmp, "inv.csv")
    rows = []
    for i in range(count):
        ns = namespaces[i % len(namespaces)]
        wave = waves[i % len(waves)]
        rows.append(["vm{:02d}".format(i), "vm-{}".format(1001 + i), ns,
                     "sub-{}".format(ns), "4000", wave])
    write_csv(path, rows, ["vm_name", "moref", "namespace", "subnet", "device_key", "wave"])
    return str(path)


@test("e2e: preflight, precheck and import drive every VM to committed")
def t_e2e_happy():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        _seed_cluster(tmp, ["ns-a", "ns-b"],
                      ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp)
        inv = _inventory(tmp, 10)

        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        out = _run_cli(["-c", cfg, "preflight"], env, tmp).stdout
        assert "preflight passed" in out, out

        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        states = {v["state"] for v in vms}
        eq(states, {"precheck_passed"}, "after precheck")

        _run_cli(["-c", cfg, "run", "-y"], env, tmp)
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"committed"}, "after import")
        eq(len(vms), 10)

        status = _run_cli(["-c", cfg, "status"], env, tmp).stdout
        assert "10 / 10 committed" in status, status


@test("e2e: import is refused before a precheck passes")
def t_e2e_precheck_gate():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp)
        inv = _inventory(tmp, 4)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        out = _run_cli(["-c", cfg, "run", "-y"], env, tmp).stdout
        assert "nothing to do" in out, out
        cluster = json.loads(Path(tmp, "cluster.json").read_text())
        eq(cluster["applies"], 0, "nothing was applied")


@test("e2e: preflight fails loudly on a missing namespace or subnet")
def t_e2e_preflight_missing():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-ns-a"])   # ns-b and its subnet absent
        cfg = _write_config(tmp)
        inv = _inventory(tmp, 6)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        proc = _run_cli(["-c", cfg, "preflight"], env, tmp, expect=2)
        assert "ns-b" in proc.stdout, proc.stdout
        assert "PROBLEM" in proc.stdout, proc.stdout


@test("e2e: a VPC-scoped subnet passes preflight with a note, not a failure")
def t_e2e_preflight_vpc_subnet():
    """The lab case: subnet lives in the VPC namespace, batch references it by name."""
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        _seed_cluster(tmp, ["migration-testing-ns-kcvm5", "testing-vpc-k826r"],
                      ["testing-vpc-k826r/migration-testing"])
        cfg = _write_config(tmp)
        write_csv(str(Path(tmp, "inv.csv")),
                  [["ubuntu-2", "vm-3064", "migration-testing-ns-kcvm5", "migration-testing"]],
                  ["vm_name", "moref", "namespace", "subnet"])
        _run_cli(["-c", cfg, "load", "-i", str(Path(tmp, "inv.csv"))], env, tmp)
        out = _run_cli(["-c", cfg, "preflight"], env, tmp).stdout      # exit 0
        assert "[ok] target subnets exist" in out, out
        assert "VPC-scoped" in out and "testing-vpc-k826r" in out, out
        assert "preflight passed" in out, out


@test("e2e: preflight reports the Mobility Operator pod")
def t_e2e_preflight_operator_pod():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-ns-a"])
        cfg = _write_config(tmp)
        _run_cli(["-c", cfg, "load", "-i", _inventory(tmp, 2, namespaces=("ns-a",), waves=(1,))],
                 env, tmp)
        out = _run_cli(["-c", cfg, "preflight"], env, tmp).stdout
        assert "[ok] Mobility Operator pod running" in out, out
        env["FAKE_KUBECTL_OPERATOR_POD"] = "CrashLoopBackOff"
        out = _run_cli(["-c", cfg, "preflight"], env, tmp, expect=2).stdout
        assert "[!!] Mobility Operator pod running" in out, out
        assert "PROBLEM: the Mobility Operator pod is not healthy" in out, out


@test("e2e: the operator's message is visible while a precheck is still running")
def t_e2e_running_message():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy", settle="1000")    # never settles within the run
        _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-ns-a"])
        cfg = _write_config(tmp, batch_timeout_minutes=1, poll_interval_seconds=1)
        _run_cli(["-c", cfg, "load", "-i", _inventory(tmp, 1, namespaces=("ns-a",), waves=(1,))],
                 env, tmp)
        # The run will time out after a minute; that is fine, we only want the row.
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp, expect=None)
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq(vms[0]["state"], "precheck_running")
        eq(vms[0]["last_phase"], "ObjectNotReady")
        assert "Operations are not ready" in (vms[0]["message"] or ""), vms[0]


@test("e2e: a batch deleted behind the tool's back fails its VMs instead of spinning")
def t_e2e_vanished_batch():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy", settle="1000")
        _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-ns-a"])
        cfg = _write_config(tmp, poll_interval_seconds=1)
        _run_cli(["-c", cfg, "load", "-i", _inventory(tmp, 2, namespaces=("ns-a",), waves=(1,))],
                 env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y", "--dry-run"], env, tmp)   # manifests only
        # Apply for real but do not wait: use --no-wait-like behaviour via a tiny timeout.
        cfg2 = _write_config(tmp, poll_interval_seconds=1, batch_timeout_minutes=1)
        proc = subprocess.Popen([sys.executable, str(ROOT / "vcfa-import.py"), "-c", cfg2,
                                 "precheck", "-y"], cwd=tmp, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        import time as _t
        _t.sleep(6)
        proc.kill()
        proc.wait()
        cluster = json.loads(Path(tmp, "cluster.json").read_text())
        assert cluster["batches"], "batch should have been applied"
        cluster["batches"] = {}                      # someone ran kubectl delete
        Path(tmp, "cluster.json").write_text(json.dumps(cluster))
        # Backdate applied_at so the 120 s grace period has elapsed.
        import sqlite3 as sq
        conn = sq.connect(str(Path(tmp, "run", "state.db")))
        conn.execute("UPDATE batches SET applied_at='2026-01-01T00:00:00Z'")
        conn.commit(); conn.close()

        out = _run_cli(["-c", cfg2, "status", "--refresh"], env, tmp).stdout
        vms = json.loads(_run_cli(["-c", cfg2, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"precheck_failed"}, out)
        assert all("disappeared" in (v["message"] or "") for v in vms), vms
        assert "retry" in vms[0]["message"]
        out = _run_cli(["-c", cfg2, "retry"], env, tmp).stdout
        assert "requeued 2" in out, out


@test("e2e: abandon deletes a precheck batch and returns its VMs to pending")
def t_e2e_abandon():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy", settle="1000")
        _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-ns-a", "ns-a/sub-right"])
        cfg = _write_config(tmp, poll_interval_seconds=1, batch_timeout_minutes=1)
        inv = _inventory(tmp, 2, namespaces=("ns-a",), waves=(1,))
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp, expect=None)   # times out, stays running
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"precheck_running"})
        batch = vms[0]["precheck_batch"]

        # The VM is locked while in flight: an inventory change must not land.
        write_csv(inv, [[v["vm_name"], v["moref"], "ns-a", "sub-right"] for v in vms],
                  ["vm_name", "moref", "namespace", "subnet"])
        out = _run_cli(["-c", cfg, "load", "-i", inv], env, tmp).stdout
        assert "locked (in flight) 2" in out, out

        # abandon requires a target
        _run_cli(["-c", cfg, "abandon", "-y"], env, tmp, expect=2)
        out = _run_cli(["-c", cfg, "abandon", "--stage", "precheck", "-y"], env, tmp).stdout
        assert "deleted 1 batch(es); 2 VM(s) back to pending" in out, out
        cluster = json.loads(Path(tmp, "cluster.json").read_text())
        eq(cluster["batches"], {}, "the object is gone from the cluster")
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"pending"})
        assert all(not v["precheck_batch"] for v in vms), vms

        # ...and now the corrected inventory lands, and a fresh precheck works.
        out = _run_cli(["-c", cfg, "load", "-i", inv], env, tmp).stdout
        assert "updated 2" in out, out
        env["FAKE_KUBECTL_SETTLE"] = "1"
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"precheck_passed"})
        assert all("sub-right" in v["nics_json"] for v in vms), "new subnet applied"
        story = _run_cli(["-c", cfg, "history", "--vm", vms[0]["moref"]], env, tmp).stdout
        assert "abandon" in story, story


@test("e2e: abandon refuses an import batch with VMs still in flight")
def t_e2e_abandon_refuses_live_import():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "wait")
        _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-ns-a"])
        cfg = _write_config(tmp, commit_action="Wait")
        _run_cli(["-c", cfg, "load", "-i", _inventory(tmp, 2, namespaces=("ns-a",), waves=(1,))],
                 env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
        _run_cli(["-c", cfg, "run", "-y"], env, tmp)
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"awaiting_commit"})
        out = _run_cli(["-c", cfg, "abandon", "--vm", vms[0]["moref"], "-y"], env, tmp, expect=4).stdout
        assert "refused" in out and "rollback --batch" in out, out
        cluster = json.loads(Path(tmp, "cluster.json").read_text())
        assert cluster["batches"], "nothing may be deleted"


@test("e2e: init writes both maps, and the portgroup map's wave column is honoured")
def t_e2e_init_maps():
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter
    server, port = fake_vcenter.serve(count=8)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = _e2e_env(tmp, "happy")
            env["VCFA_VC_SERVER"] = "http://127.0.0.1:{}".format(port)
            env["VCFA_VC_USER"] = fake_vcenter.USER
            env["VCFA_VC_PASSWORD"] = fake_vcenter.PASSWORD
            out = _run_cli(["init", "--dir", tmp], env, tmp).stdout
            for name in ("vcfa-import.toml", "inventory.csv", "portgroup-map.csv", "folder-map.csv"):
                assert Path(tmp, name).is_file(), name
                assert name in out, out
            header = Path(tmp, "portgroup-map.csv").read_text(encoding="utf-8").splitlines()[0]
            eq(header, "portgroup,namespace,subnet,wave,device_key,subnet_kind,subnet_api_group")
            header = Path(tmp, "folder-map.csv").read_text(encoding="utf-8").splitlines()[0]
            eq(header, "folder,namespace,wave,group")

            # A portgroup map alone (no folder map, nothing set in the picker) sets the wave.
            _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-a", "ns-a/sub-b"])
            cfg = _write_config(tmp)
            _run_cli(["-c", cfg, "discover"], env, tmp)
            _run_cli(["-c", cfg, "select", "--all"], env, tmp)
            write_csv(str(Path(tmp, "portgroup-map.csv")), [
                ["VLAN197-Prod", "ns-a", "sub-a", "3", "", "", ""],
                ["*", "ns-a", "sub-b", "7", "", "", ""],
            ], ["portgroup", "namespace", "subnet", "wave", "device_key", "subnet_kind", "subnet_api_group"])
            _run_cli(["-c", cfg, "stage", "--map", str(Path(tmp, "portgroup-map.csv"))], env, tmp)
            vms = json.loads(_run_cli(["-c", cfg, "vms", "--json", "--limit", "100"], env, tmp).stdout)
            waves = {v["wave"] for v in vms}
            assert waves <= {3, 7} and 3 in waves, waves
            for v in vms:
                nets = v["src_networks"].split(",")
                expect = 3 if nets and nets[0] == "VLAN197-Prod" else 7
                eq(v["wave"], expect, "{} on {}".format(v["vm_name"], v["src_networks"]))
    finally:
        server.shutdown()


@test("e2e: a missing subnet lists what the namespace actually holds")
def t_e2e_preflight_subnet_hint():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        # The namespace exists and has a subnet -- just not the one the map names.
        _seed_cluster(tmp, ["ns-a"], ["ns-a/subnet-vlan197-a1b2c"])
        cfg = _write_config(tmp)
        inv = _inventory(tmp, 2, namespaces=("ns-a",), waves=(1,))   # asks for sub-ns-a
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        out = _run_cli(["-c", cfg, "preflight"], env, tmp, expect=2).stdout
        assert "subnet(s) not found: ns-a/sub-ns-a" in out, out
        assert "Subnet.crd.nsx.vmware.com/subnet-vlan197-a1b2c" in out, out
        assert "display name: vlan197-a1b2c" in out, out
        assert "subnet_kind / subnet_api_group" in out, out


@test("e2e: failures are isolated, recorded, and retryable")
def t_e2e_flaky():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "flaky")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        # failure_rate_abort raised so the circuit breaker does not trip here
        cfg = _write_config(tmp, failure_rate_abort=0.9)
        inv = _inventory(tmp, 10)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp, expect=4)

        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        failed = [v for v in vms if v["state"] == "precheck_failed"]
        passed = [v for v in vms if v["state"] == "precheck_passed"]
        assert failed, "expected some precheck failures"
        assert passed, "expected the rest to pass"
        assert all("VM Tools" in (v["message"] or "") for v in failed), failed

        # The healthy majority still imports.
        _run_cli(["-c", cfg, "run", "-y"], env, tmp)
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq(sum(1 for v in vms if v["state"] == "committed"), len(passed))

        # retry puts the failures back in the queue
        out = _run_cli(["-c", cfg, "retry"], env, tmp).stdout
        assert "requeued {}".format(len(failed)) in out, out
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq(sum(1 for v in vms if v["state"] == "pending"), len(failed))


@test("e2e: the circuit breaker halts a run that is going badly")
def t_e2e_circuit_breaker():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "disaster")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp, failure_rate_abort=0.25, failure_rate_min_sample=4,
                            batch_size=4, max_parallel_batches=1)
        inv = _inventory(tmp, 24, namespaces=("ns-a",), waves=(1,))
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        proc = _run_cli(["-c", cfg, "precheck", "-y"], env, tmp, expect=3)
        assert "RUN HALTED" in proc.stdout, proc.stdout

        cluster = json.loads(Path(tmp, "cluster.json").read_text())
        assert cluster["applies"] < 6, \
            "expected the breaker to stop early, applied {}".format(cluster["applies"])


@test("e2e: commitAction Wait holds VMs until commit is run")
def t_e2e_commit_gate():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "wait")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp, commit_action="Wait")
        inv = _inventory(tmp, 8)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
        _run_cli(["-c", cfg, "run", "-y"], env, tmp)

        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"awaiting_commit"}, "held at the commit gate")

        out = _run_cli(["-c", cfg, "commit", "-y"], env, tmp).stdout
        assert "patched" in out, out
        _run_cli(["-c", cfg, "status", "--refresh"], env, tmp)
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"committed"}, "after commit")


@test("e2e: dry run applies nothing")
def t_e2e_dry_run():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp)
        inv = _inventory(tmp, 6)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        out = _run_cli(["-c", cfg, "precheck", "-y", "--dry-run"], env, tmp).stdout
        assert "[dry-run]" in out, out
        cluster = json.loads(Path(tmp, "cluster.json").read_text())
        eq(cluster["applies"], 0, "dry run applied something")
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"pending"}, "dry run must not move VM state")
        # ...and the manifests are still on disk for review
        manifests = list(Path(tmp, "run", "manifests").glob("*.yaml"))
        assert manifests, "dry run should still render manifests"


@test("e2e: state survives a restart and resumes where it stopped")
def t_e2e_resume():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp)
        inv = _inventory(tmp, 8)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y", "--wave", "1"], env, tmp)

        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        w1 = [v for v in vms if v["wave"] == 1]
        w2 = [v for v in vms if v["wave"] == 2]
        eq({v["state"] for v in w1}, {"precheck_passed"})
        eq({v["state"] for v in w2}, {"pending"}, "wave 2 untouched")

        # Re-loading the same inventory must not reset progress.
        out = _run_cli(["-c", cfg, "load", "-i", inv], env, tmp).stdout
        assert "added 0" in out, out
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms if v["wave"] == 1}, {"precheck_passed"})


@test("e2e: report writes HTML and CSV")
def t_e2e_report():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp)
        inv = _inventory(tmp, 6)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
        _run_cli(["-c", cfg, "report", "--html", "r.html", "--csv", "r.csv"], env, tmp)
        html = Path(tmp, "r.html").read_text(encoding="utf-8")
        assert "VCF Automation import progress" in html
        assert "prefers-color-scheme" in html, "report should be theme aware"
        rows = list(csv.DictReader(Path(tmp, "r.csv").open(encoding="utf-8")))
        eq(len(rows), 6)
        assert rows[0]["state"] == "precheck_passed"


@test("e2e: plan --server-dry-run validates without applying")
def t_e2e_server_dry_run():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-ns-a"])
        cfg = _write_config(tmp)
        inv = _inventory(tmp, 4, namespaces=("ns-a", "ns-b"), waves=(1,))
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        proc = _run_cli(["-c", cfg, "plan", "--stage", "precheck", "--server-dry-run"],
                        env, tmp, expect=2)
        assert "[!!]" in proc.stdout, proc.stdout
        cluster = json.loads(Path(tmp, "cluster.json").read_text())
        eq(cluster["applies"], 0)


@test("e2e: discover from vCenter, pick, select and stage into the import queue")
def t_e2e_discover():
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter

    server, port = fake_vcenter.serve(count=60)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = _e2e_env(tmp, "happy")
            env["VCFA_VC_SERVER"] = "http://127.0.0.1:{}".format(port)
            env["VCFA_VC_USER"] = fake_vcenter.USER
            env["VCFA_VC_PASSWORD"] = fake_vcenter.PASSWORD
            _seed_cluster(tmp, ["ns-prod", "ns-db"],
                          ["ns-prod/subnet-197", "ns-db/subnet-2xx"])
            cfg = _write_config(tmp)

            out = _run_cli(["-c", cfg, "discover"], env, tmp).stdout
            assert "discovered 60 VM(s)" in out, out

            # Facets and filtering work off the cache, with no further vCenter calls.
            browsed = _run_cli(["-c", cfg, "browse", "--facets"], env, tmp).stdout
            assert "PROD-CL01" in browsed and "VLAN197-Prod" in browsed, browsed

            listed = json.loads(_run_cli(
                ["-c", cfg, "browse", "--json", "--name", "web-*", "--powered-on"],
                env, tmp).stdout)
            assert listed, "expected some web-* VMs"
            assert all(v["name"].startswith("web-") for v in listed)
            assert all(v["power_state"] == "POWERED_ON" for v in listed)

            # Nothing is selected until asked.
            eq(json.loads(_run_cli(["-c", cfg, "browse", "--json", "--selected"],
                                   env, tmp).stdout), [])

            # A bare select refuses to act without filters.
            _run_cli(["-c", cfg, "select"], env, tmp, expect=2)

            _run_cli(["-c", cfg, "select", "--name", "web-*", "--powered-on"], env, tmp)
            selected = json.loads(_run_cli(["-c", cfg, "browse", "--json", "--selected"],
                                           env, tmp).stdout)
            eq(len(selected), len(listed), "selection matches the filter")

            # The picker renders the whole cache and marks what is selected.
            picker = _run_cli(["-c", cfg, "pick", "--out", "picker.html"], env, tmp).stdout
            assert "picker.html" in picker, picker
            doc = Path(tmp, "picker.html").read_text(encoding="utf-8")
            assert '"selected": true' in doc or '"selected":true' in doc

            # Selecting from a picker-style CSV carries per-VM namespaces.
            sel_csv = Path(tmp, "selection.csv")
            write_csv(str(sel_csv),
                      [[v["name"], v["moref"], "ns-db", 2] for v in listed[:3]],
                      ["vm_name", "moref", "namespace", "wave"])
            _run_cli(["-c", cfg, "select", "--none"], env, tmp)
            _run_cli(["-c", cfg, "select", "--from-file", str(sel_csv)], env, tmp)
            selected = json.loads(_run_cli(["-c", cfg, "browse", "--json", "--selected"],
                                           env, tmp).stdout)
            eq(len(selected), 3)
            eq({v["namespace"] for v in selected}, {"ns-db"})
            eq({v["wave"] for v in selected}, {2})

            # Stage into the import queue via a portgroup map.
            map_path = Path(tmp, "map.csv")
            write_csv(str(map_path), [
                ["VLAN197-Prod", "ns-prod", "subnet-197", "1"],
                ["VLAN2*", "ns-db", "subnet-2xx", "2"],
                ["DMZ-Uplink", "ns-db", "subnet-2xx", "2"],
            ], ["portgroup", "namespace", "subnet", "wave"])
            staged = _run_cli(["-c", cfg, "stage", "--map", str(map_path)], env, tmp).stdout
            assert "staged 3 VM(s)" in staged, staged

            vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
            eq(len(vms), 3)
            eq({v["namespace"] for v in vms}, {"ns-db"}, "picker namespace wins over the map")
            for v in vms:
                assert json.loads(v["nics_json"]), "each staged VM keeps its NIC mapping"

            # ...and the staged VMs import like any other inventory.
            _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
            _run_cli(["-c", cfg, "run", "-y"], env, tmp)
            vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
            eq({v["state"] for v in vms}, {"committed"})
    finally:
        server.shutdown()


@test("e2e: VMs are collected by folder, subfolders included, and staged via a folder map")
def t_e2e_folders():
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter

    server, port = fake_vcenter.serve(count=40)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = _e2e_env(tmp, "happy")
            env["VCFA_VC_SERVER"] = "http://127.0.0.1:{}".format(port)
            env["VCFA_VC_USER"] = fake_vcenter.USER
            env["VCFA_VC_PASSWORD"] = fake_vcenter.PASSWORD
            _seed_cluster(tmp, ["ns-web", "ns-prod"], ["ns-web/sub-a", "ns-prod/sub-a"])
            cfg = _write_config(tmp)
            _run_cli(["-c", cfg, "load", "-i", _inventory(tmp, 0)], env, tmp, expect=None)

            out = _run_cli(["-c", cfg, "discover"], env, tmp).stdout
            assert "folder(s) across 1 datacenter(s)" in out, out

            # Full paths and the datacenter come back from discovery.
            vms = json.loads(_run_cli(["-c", cfg, "browse", "--json"], env, tmp).stdout)
            paths = {v["folder"] for v in vms}
            assert "Production/Web/Tier1" in paths, paths
            assert "Legacy/Decommission" in paths, paths
            assert "" in paths, "VMs loose in the datacenter root have an empty path"
            eq({v["datacenter"] for v in vms}, {"LabDC"})

            # The tree view.
            tree = _run_cli(["-c", cfg, "browse", "--folders"], env, tmp).stdout
            assert "Production/" in tree and "Tier1/" in tree, tree
            assert "(datacenter root)" in tree, tree

            # Selecting a folder takes its subtree.
            _run_cli(["-c", cfg, "select", "--folder", "Production/Web"], env, tmp)
            sel = json.loads(_run_cli(["-c", cfg, "browse", "--json", "--selected"],
                                      env, tmp).stdout)
            eq({v["folder"] for v in sel}, {"Production/Web", "Production/Web/Tier1"})
            expected = sum(1 for v in vms if v["folder"] in ("Production/Web", "Production/Web/Tier1"))
            eq(len(sel), expected, "every VM under Production/Web, and only those")

            # ...unless subfolders are excluded.
            _run_cli(["-c", cfg, "select", "--none"], env, tmp)
            _run_cli(["-c", cfg, "select", "--folder", "Production/Web", "--no-subfolders"],
                     env, tmp)
            sel = json.loads(_run_cli(["-c", cfg, "browse", "--json", "--selected"],
                                      env, tmp).stdout)
            eq({v["folder"] for v in sel}, {"Production/Web"})

            # Stage with a folder map: most specific folder wins.
            _run_cli(["-c", cfg, "select", "--folder", "Production"], env, tmp)
            fmap = Path(tmp, "folder-map.csv")
            write_csv(str(fmap), [
                ["Production", "ns-prod", "2"],
                ["Production/Web", "ns-web", "1"],
            ], ["folder", "namespace", "wave"])
            nmap = Path(tmp, "net-map.csv")
            write_csv(str(nmap), [["*", "", "sub-a"]], ["portgroup", "namespace", "subnet"])
            out = _run_cli(["-c", cfg, "stage", "--folder-map", str(fmap), "--map", str(nmap)],
                           env, tmp).stdout
            assert "staged" in out, out
            staged = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
            by_folder = {}
            for v in staged:
                by_folder.setdefault(v["src_folder"], set()).add((v["namespace"], v["wave"]))
            eq(by_folder["Production/Web"], {("ns-web", 1)})
            eq(by_folder["Production/Web/Tier1"], {("ns-web", 1)}, "inherits the deeper rule")
            eq(by_folder["Production/App"], {("ns-prod", 2)}, "falls back to the parent rule")
            assert all(v["src_datacenter"] == "LabDC" for v in staged)

            # The exported selection carries the path and datacenter.
            _run_cli(["-c", cfg, "browse", "--selected", "--csv", "sel.csv"], env, tmp)
            rows = list(csv.DictReader(Path(tmp, "sel.csv").open(encoding="utf-8")))
            assert rows and rows[0]["datacenter"] == "LabDC"
            assert any("/" in r["folder"] for r in rows)
    finally:
        server.shutdown()


@test("e2e: precheck, run, rollback, retry and status all scope by --folder")
def t_e2e_folder_execution():
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter

    server, port = fake_vcenter.serve(count=40)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = _e2e_env(tmp, "import-flaky")
            env["VCFA_VC_SERVER"] = "http://127.0.0.1:{}".format(port)
            env["VCFA_VC_USER"] = fake_vcenter.USER
            env["VCFA_VC_PASSWORD"] = fake_vcenter.PASSWORD
            _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-a"])
            cfg = _write_config(tmp, failure_rate_abort=0.9)
            _run_cli(["-c", cfg, "discover"], env, tmp)
            _run_cli(["-c", cfg, "select", "--all"], env, tmp)
            nmap = Path(tmp, "net-map.csv")
            write_csv(str(nmap), [["*", "ns-a", "sub-a"]], ["portgroup", "namespace", "subnet"])
            _run_cli(["-c", cfg, "stage", "--map", str(nmap)], env, tmp)

            def states(folder=None, exact=False):
                args = ["-c", cfg, "vms", "--json", "--limit", "500"]
                if folder:
                    args += ["--folder", folder] + (["--no-subfolders"] if exact else [])
                return {v["moref"]: (v["src_folder"], v["state"])
                        for v in json.loads(_run_cli(args, env, tmp).stdout)}

            everything = states()
            web = states("Production/Web")
            assert web and all(f.startswith("Production/Web") for f, _ in web.values())
            assert len(web) < len(everything)

            # Precheck only the Web subtree; nothing else moves.
            out = _run_cli(["-c", cfg, "precheck", "-y", "--folder", "Production/Web"],
                           env, tmp).stdout
            assert "folder Production/Web (with subfolders)" in out, out
            after = states()
            for moref, (folder, state) in after.items():
                if moref in web:
                    eq(state, "precheck_passed", moref)
                else:
                    eq(state, "pending", "{} in {} must be untouched".format(moref, folder))

            # Batches are per folder: Web and Web/Tier1 land in different batches.
            batches = {v["precheck_batch"] for v in json.loads(_run_cli(
                ["-c", cfg, "vms", "--json", "--folder", "Production/Web", "--limit", "500"],
                env, tmp).stdout)}
            assert any("tier1" in b for b in batches), batches
            assert any("tier1" not in b for b in batches), batches

            # Import the same scope; some fail (import-flaky).
            _run_cli(["-c", cfg, "run", "-y", "--folder", "Production/Web"], env, tmp, expect=4)
            after = states()
            assert all(after[m][1] == "pending" for m in after if m not in web), \
                "run --folder must not touch VMs outside the folder"
            failed = [m for m in web if after[m][1] == "failed"]
            assert failed, "expected import failures inside the scope"

            # status --folder shows just that scope.
            scoped = _run_cli(["-c", cfg, "status", "--folder", "Production/Web"], env, tmp).stdout
            assert "Scope: folder Production/Web" in scoped and "By folder" in scoped, scoped
            assert "Production/Web/Tier1" in scoped, scoped
            assert "Databases" not in scoped, scoped

            # Roll back only Tier1's failures; Web's own failures stay put.
            tier1_failed = [m for m in failed if after[m][0] == "Production/Web/Tier1"]
            web_failed = [m for m in failed if after[m][0] == "Production/Web"]
            assert tier1_failed and web_failed, "need failures in both folders"
            out = _run_cli(["-c", cfg, "rollback", "-y", "--folder", "Production/Web/Tier1"],
                           env, tmp).stdout
            assert "reverted to vCenter" in out, out
            after = states()
            for m in tier1_failed:
                eq(after[m][1], "rolled_back", m)
            for m in web_failed:
                eq(after[m][1], "failed", "{} is outside the rollback scope".format(m))

            # retry --folder requeues only that subtree's rolled-back VMs.
            out = _run_cli(["-c", cfg, "retry", "--folder", "Production/Web/Tier1"], env, tmp).stdout
            assert "requeued {}".format(len(tier1_failed)) in out, out
            after = states()
            for m in web_failed:
                eq(after[m][1], "failed")

            # --no-subfolders on an execution command.
            _run_cli(["-c", cfg, "precheck", "-y", "--folder", "Production", "--no-subfolders"],
                     env, tmp)
            assert all(after[m][1] == "pending" for m in after
                       if after[m][0] in ("Production/App",)), "subfolders must stay pending"
    finally:
        server.shutdown()


@test("rollback --folder warns when a batch also holds VMs outside the scope")
def t_rollback_scope_warning():
    from vcfaimport import state as vst
    from vcfaimport.engine import Engine
    from vcfaimport.kube import Kubectl
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        for moref, folder in (("vm-1", "A/x"), ("vm-2", "B/y")):
            _record(store, moref=moref, name=moref)
            store.attach_provenance(moref, {"folder": folder})
        store.create_batch("imp-shared", "ns-a", "r", 1, "import", 2)
        for moref in ("vm-1", "vm-2"):
            store.set_vm_state(moref, vst.S_IMPORTING, batch_name="imp-shared", stage="import")
            store.set_vm_state(moref, vst.S_FAILED, stage="import")
        cfg = Config()
        engine = Engine(cfg, store, Kubectl(cfg, dry_run=True), lambda m: None)
        batches, notes = engine.rollback_targets(folders=["A"])
        eq([b["name"] for b in batches], ["imp-shared"])
        assert any("outside the folder scope" in n and "vm-2" in n for n in notes), notes
        store.close()


@test("e2e: discovery refuses bad credentials and reports it clearly")
def t_e2e_discover_auth():
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter

    server, port = fake_vcenter.serve(count=5)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = _e2e_env(tmp, "happy")
            env["VCFA_VC_SERVER"] = "http://127.0.0.1:{}".format(port)
            env["VCFA_VC_USER"] = fake_vcenter.USER
            env["VCFA_VC_PASSWORD"] = "wrong-password"
            _seed_cluster(tmp, [], [])
            cfg = _write_config(tmp)
            proc = _run_cli(["-c", cfg, "discover"], env, tmp, expect=6)
            assert "rejected the credentials" in proc.stdout, proc.stdout
    finally:
        server.shutdown()


@test("e2e: re-discovery preserves an existing selection")
def t_e2e_rediscover():
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter

    server, port = fake_vcenter.serve(count=20)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = _e2e_env(tmp, "happy")
            env["VCFA_VC_SERVER"] = "http://127.0.0.1:{}".format(port)
            env["VCFA_VC_USER"] = fake_vcenter.USER
            env["VCFA_VC_PASSWORD"] = fake_vcenter.PASSWORD
            _seed_cluster(tmp, [], [])
            cfg = _write_config(tmp)
            _run_cli(["-c", cfg, "discover"], env, tmp)
            _run_cli(["-c", cfg, "select", "--name", "db-*", "--namespace", "ns-keep"], env, tmp)
            before = json.loads(_run_cli(["-c", cfg, "browse", "--json", "--selected"],
                                         env, tmp).stdout)
            assert before, "expected a selection"

            out = _run_cli(["-c", cfg, "discover"], env, tmp).stdout
            assert "added 0" in out or "refreshed 20" in out, out
            after = json.loads(_run_cli(["-c", cfg, "browse", "--json", "--selected"],
                                        env, tmp).stdout)
            eq({v["moref"] for v in after}, {v["moref"] for v in before})
            eq({v["namespace"] for v in after}, {"ns-keep"})
    finally:
        server.shutdown()


@test("e2e: a full run leaves a complete audit trail")
def t_e2e_tracker():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp)
        inv = _inventory(tmp, 6)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
        _run_cli(["-c", cfg, "run", "-y"], env, tmp)

        # Ledger: every VM walked the full path, in order.
        ledger = Path(tmp, "run", "ledger.jsonl")
        assert ledger.is_file(), "ledger.jsonl was not written"
        entries = [json.loads(x) for x in
                   ledger.read_text(encoding="utf-8").splitlines() if x.strip()]
        by_vm = {}
        for e in entries:
            by_vm.setdefault(e["moref"], []).append(e["to_state"])
        eq(len(by_vm), 6)
        for moref, states in by_vm.items():
            eq(states,
               ["pending", "precheck_running", "precheck_passed", "importing", "committed"],
               moref)

        # history: one VM's whole story, and the aggregate view.
        a_moref = sorted(by_vm)[0]
        story = _run_cli(["-c", cfg, "history", "--vm", a_moref], env, tmp).stdout
        assert "committed" in story and a_moref in story, story
        assert "durations" in story, story

        moves = json.loads(_run_cli(
            ["-c", cfg, "history", "--json", "--state", "committed", "--limit", "50"],
            env, tmp).stdout)
        eq(len(moves), 6)
        assert all(m["to_state"] == "committed" for m in moves)
        assert all(m["ts"] and m["batch"] for m in moves), moves[:1]

        # ledger export: tracker CSV + per-transition CSV, and a consistency check.
        _run_cli(["-c", cfg, "ledger", "--tracker", "tracker.csv", "--csv", "moves.csv"],
                 env, tmp)
        tracker = list(csv.DictReader(Path(tmp, "tracker.csv").open(encoding="utf-8")))
        eq(len(tracker), 6)
        for row in tracker:
            eq(row["state"], "committed")
            assert row["committed_at"], row
            assert row["import_duration"], row
            assert row["namespace"], row
        transitions = list(csv.DictReader(Path(tmp, "moves.csv").open(encoding="utf-8")))
        eq(len(transitions), 30, "6 VMs x 5 states")

        verify = _run_cli(["-c", cfg, "ledger", "--verify"], env, tmp).stdout
        assert "consistent" in verify, verify

        # the HTML report gains the tracker table
        _run_cli(["-c", cfg, "report", "--html", "r.html"], env, tmp)
        html_doc = Path(tmp, "r.html").read_text(encoding="utf-8")
        assert "Moved into VCFA" in html_doc
        assert "target resource" in html_doc


@test("e2e: a failure and its retry are both visible in the trail")
def t_e2e_tracker_retry():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "flaky")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp, failure_rate_abort=0.9)
        inv = _inventory(tmp, 10)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp, expect=4)
        _run_cli(["-c", cfg, "retry"], env, tmp)

        entries = [json.loads(x) for x in
                   Path(tmp, "run", "ledger.jsonl").read_text(encoding="utf-8").splitlines()
                   if x.strip()]
        failed = [e for e in entries if e["to_state"] == "precheck_failed"]
        assert failed, "expected recorded failures"
        assert all(e["message"] for e in failed), "a failure must record why"

        requeued = [e for e in entries if e["stage"] == "retry"]
        eq(len(requeued), len(failed), "every requeue is recorded")
        for e in requeued:
            eq(e["from_state"], "precheck_failed")
            eq(e["to_state"], "pending")

        story = _run_cli(["-c", cfg, "history", "--vm", failed[0]["moref"]], env, tmp).stdout
        assert "precheck_failed" in story and "VM Tools" in story, story


@test("e2e: rollback hands failed imports back to vCenter, deletes, and allows retry")
def t_e2e_rollback():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "import-flaky")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp, failure_rate_abort=0.9)
        inv = _inventory(tmp, 10)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)      # everything passes precheck
        _run_cli(["-c", cfg, "run", "-y"], env, tmp, expect=4)  # ...then some fail at import

        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        failed = [v for v in vms if v["state"] == "failed"]
        committed = [v for v in vms if v["state"] == "committed"]
        assert failed and committed, "expected a mix of failed and committed"

        # Refuses to act without a target.
        _run_cli(["-c", cfg, "rollback", "-y"], env, tmp, expect=2)

        # Roll back every batch with failures, wait for the revert, delete on confirmation.
        out = _run_cli(["-c", cfg, "rollback", "--failed", "--delete", "-y"], env, tmp).stdout
        assert "rollbackAction=Immediate" in out, out
        assert "reverted to vCenter" in out, out
        assert "deleted" in out, out

        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        by = {v["moref"]: v for v in vms}
        for v in failed:
            eq(by[v["moref"]]["state"], "rolled_back", v["moref"])
            assert by[v["moref"]]["rolled_back_at"], "rolled_back_at must be stamped"
        for v in committed:
            eq(by[v["moref"]]["state"], "committed", "committed VMs are never touched")

        # The batch objects are gone from the (fake) cluster, and recorded as such.
        cluster = json.loads(Path(tmp, "cluster.json").read_text())
        touched = {v["batch_name"] for v in failed}
        for key in cluster["batches"]:
            assert key.split("/", 1)[1] not in touched, "rolled-back batch still on cluster"
        events = _run_cli(["-c", cfg, "events", "--limit", "100"], env, tmp).stdout
        assert "deleted after rollback" in events, events

        # The ledger shows the whole path for a rolled-back VM.
        story = _run_cli(["-c", cfg, "history", "--vm", failed[0]["moref"]], env, tmp).stdout
        for step in ("importing", "failed", "rolling_back", "rolled_back"):
            assert step in story, "{} missing from:\n{}".format(step, story)

        # Nothing left to clean up; and rollback on a committed-only batch is refused.
        assert "no batches" in _run_cli(["-c", cfg, "cleanup", "-y"], env, tmp).stdout
        out = _run_cli(["-c", cfg, "rollback", "--vm", committed[0]["moref"], "-y"],
                       env, tmp).stdout
        assert "cannot be rolled back" in out, out

        # Recovery: the environment is fixed, the reverted VMs are retried and land.
        env["FAKE_KUBECTL_SCENARIO"] = "happy"
        out = _run_cli(["-c", cfg, "retry"], env, tmp).stdout
        assert "requeued {}".format(len(failed)) in out, out
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
        _run_cli(["-c", cfg, "run", "-y"], env, tmp)
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"committed"}, "everything lands on the second attempt")
        story = _run_cli(["-c", cfg, "history", "--vm", failed[0]["moref"]], env, tmp).stdout
        assert story.count("committed") >= 1 and "rolled_back" in story


@test("e2e: run --rollback-failed reverts failed imports in the same invocation")
def t_e2e_run_rollback_failed():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "import-flaky")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp, failure_rate_abort=0.9)
        inv = _inventory(tmp, 10)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
        out = _run_cli(["-c", cfg, "run", "-y", "--rollback-failed"], env, tmp, expect=4).stdout
        assert "--rollback-failed: reverting" in out, out
        assert "back under vCenter" in out, out

        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        states = {v["state"] for v in vms}
        assert "failed" not in states, "nothing should be left merely failed: {}".format(states)
        assert "rolled_back" in states and "committed" in states, states

        # Manifests that were applied never contained the field.
        for path in Path(tmp, "run", "manifests").glob("*.yaml"):
            assert "rollbackAction" not in path.read_text(encoding="utf-8"), path
        # The revert happened by patching, after the failure.
        events = _run_cli(["-c", cfg, "events", "--limit", "200"], env, tmp).stdout
        assert "rollbackAction=Immediate requested" in events, events
        # Batches are left for cleanup, not deleted.
        assert _run_cli(["-c", cfg, "cleanup", "-y"], env, tmp).stdout.count("deleted")


@test("e2e: rollback --no-wait returns at once and refresh picks up the revert")
def t_e2e_rollback_nowait():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "import-flaky")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp, failure_rate_abort=0.9)
        inv = _inventory(tmp, 10)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
        _run_cli(["-c", cfg, "run", "-y"], env, tmp, expect=4)

        out = _run_cli(["-c", cfg, "rollback", "--failed", "--no-wait", "-y"],
                       env, tmp, expect=4).stdout
        assert "not waiting" in out, out
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        assert any(v["state"] == "rolling_back" for v in vms), "should be mid-rollback"

        # A few refreshes later the operator has reverted them.
        for _ in range(4):
            _run_cli(["-c", cfg, "status", "--refresh"], env, tmp)
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        assert not any(v["state"] == "rolling_back" for v in vms), "still rolling back"
        assert any(v["state"] == "rolled_back" for v in vms), "nothing reverted"

        # cleanup deletes only what the operator confirmed, and needs a yes.
        out = _run_cli(["-c", cfg, "cleanup", "-y"], env, tmp).stdout
        assert "deleted" in out, out


@test("e2e: a held (commitAction: Wait) batch can be rolled back instead of committed")
def t_e2e_rollback_held():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "wait")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg = _write_config(tmp, commit_action="Wait")
        inv = _inventory(tmp, 4)
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
        _run_cli(["-c", cfg, "run", "-y"], env, tmp)
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"awaiting_commit"})

        # Changed our minds: send them all back.
        out = _run_cli(["-c", cfg, "rollback", "--failed", "-y"], env, tmp).stdout
        assert "reverted to vCenter" in out, out
        vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], env, tmp).stdout)
        eq({v["state"] for v in vms}, {"rolled_back"})
        cluster = json.loads(Path(tmp, "cluster.json").read_text())
        assert cluster["batches"], "without --delete the batch objects remain for inspection"


@test("e2e: scales to 1800 VMs without stalling")
def t_e2e_scale():
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy", settle="1")
        namespaces = ["ns-{:02d}".format(i) for i in range(12)]
        _seed_cluster(tmp, namespaces, ["{}/sub-{}".format(n, n) for n in namespaces])
        cfg = _write_config(tmp, batch_size=25, max_parallel_batches=8,
                            max_parallel_batches_per_namespace=2, poll_interval_seconds=1)
        inv = _inventory(tmp, 1800, namespaces=tuple(namespaces), waves=(1,))
        _run_cli(["-c", cfg, "load", "-i", inv], env, tmp)
        _run_cli(["-c", cfg, "precheck", "-y"], env, tmp)
        _run_cli(["-c", cfg, "run", "-y"], env, tmp)
        status = _run_cli(["-c", cfg, "status"], env, tmp).stdout
        assert "1800 / 1800 committed" in status, status[-2000:]
        cluster = json.loads(Path(tmp, "cluster.json").read_text())
        eq(cluster["applies"], 144, "1800 VMs / 25 per batch, twice (precheck + import)")


# ------------------------------------------------------------- web console
@test("moving VMs between waves: queued VMs move, in-batch VMs are refused, no transition")
def t_store_wave_moves():
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        for i, state in enumerate([None, vst.S_IMPORTING, vst.S_COMMITTED, vst.S_FAILED]):
            _record(store, moref="vm-{}".format(i), name="vm{}".format(i))
            if state:
                store.set_vm_state("vm-{}".format(i), state, stage="import")
        store.upsert_discovered([{"moref": "vm-0", "name": "vm0"}])
        before = store.get_vm("vm-0")["updated_at"]
        n_trans = len(store.transitions())

        moved, refused = store.set_vm_wave(["vm-0", "vm-1", "vm-2", "vm-3", "vm-404"], 3)
        eq(moved, 2, "pending and failed move")
        eq(len(refused), 3, "importing, committed and unknown are refused")
        eq(store.get_vm("vm-0")["wave"], 3)
        eq(store.get_vm("vm-1")["wave"], 1, "an importing VM keeps the wave its batch has")
        eq(store.query_discovered(morefs=["vm-0"])[0]["wave"], 3, "re-staging keeps the choice")
        eq(store.get_vm("vm-0")["updated_at"], before, "held-time accounting is untouched")
        eq(len(store.transitions()), n_trans, "a wave is not a state")
        assert any("to wave 3" in e["message"] for e in store.recent_events(moref="vm-0"))
        try:
            store.set_vm_wave(["vm-0"], 0)
        except ValueError:
            pass
        else:
            raise AssertionError("wave 0 must be rejected")
        store.close()


@test("swapping waves is all or nothing")
def t_store_swap_waves():
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _record(store, moref="vm-1", name="a", wave=1)
        _record(store, moref="vm-2", name="b", wave=2)
        ok, _ = store.swap_waves(1, 2)
        assert ok
        eq((store.get_vm("vm-1")["wave"], store.get_vm("vm-2")["wave"]), (2, 1))
        store.set_vm_state("vm-2", vst.S_PRECHECK_RUNNING, stage="precheck")
        ok, refused = store.swap_waves(1, 2)
        assert not ok and refused, "a VM in a batch pins its wave"
        eq((store.get_vm("vm-1")["wave"], store.get_vm("vm-2")["wave"]), (2, 1), "nothing moved")
        store.close()


@test("triage groups failures by cause and recognises the lab's DNS wedge")
def t_triage_groups():
    from vcfaimport import service
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        msgs = {
            "vm-1": "lookup vc01.lab on 127.0.0.53:53: read udp 10.0.0.4:4411->127.0.0.53:53: i/o timeout",
            "vm-2": "lookup vc01.lab on 127.0.0.53:53: read udp 10.0.0.4:5822->127.0.0.53:53: i/o timeout",
            "vm-3": "VM Tools not running",
            "vm-4": "something nobody has seen before",
        }
        for moref, msg in msgs.items():
            _record(store, moref=moref, name=moref)
            store.set_vm_state(moref, vst.S_PRECHECK_FAILED if moref != "vm-3" else vst.S_FAILED,
                               message=msg, stage="precheck")
        out = service.failure_groups(store)
        by_issue = {(g["issue"] or {}).get("id"): g for g in out["groups"]}
        eq(by_issue["dns"]["count"], 2, "two DNS failures, different ports, one group")
        eq(by_issue["tools"]["stages"], {"import": 1})
        assert None in by_issue, "an unknown message still gets its own group"
        eq(out["groups"][0]["count"], 2, "biggest group first")
        store.close()


@test("map files round-trip through the editor rows, and bad numbers are rejected")
def t_map_rows_roundtrip():
    from vcfaimport.discovery import (
        FOLDER_MAP_COLUMNS, SelectionError, folder_map_from_rows, load_folder_map,
        read_folder_map_rows, write_map_rows)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "folder-map.csv")
        rows = [{"folder": "Production", "namespace": "ns-a", "wave": "2", "group": ""},
                {"folder": "Production/Web", "namespace": "ns-b", "wave": "", "group": "web"},
                {"folder": "  ", "namespace": "dropped"}]
        eq(write_map_rows(path, FOLDER_MAP_COLUMNS, rows), 2, "blank keys are dropped")
        loaded = load_folder_map(path)
        eq(loaded["Production"].wave, 2)
        eq(loaded["Production/Web"].group, "web")
        eq(folder_map_from_rows(read_folder_map_rows(path)), loaded)
        try:
            folder_map_from_rows([{"folder": "X", "namespace": "n", "wave": "soon"}])
        except SelectionError as exc:
            assert "wave" in str(exc)
        else:
            raise AssertionError("a non-numeric wave must be rejected")


@test("skip refuses VMs in a batch; unskip never requeues a failed VM")
def t_skip_semantics():
    from vcfaimport import service
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _record(store, moref="vm-1")
        _record(store, moref="vm-2", name="b")
        store.set_vm_state("vm-1", vst.S_AWAITING_COMMIT, stage="import")
        store.set_vm_state("vm-2", vst.S_FAILED, stage="import")
        changed, notes = service.skip_vms(store, ["vm-1"])
        eq(changed, 0)
        assert notes and "awaiting_commit" in notes[0], notes
        changed, _ = service.skip_vms(store, ["vm-2"], unskip=True)
        eq(changed, 0)
        eq(store.get_vm("vm-2")["state"], vst.S_FAILED, "retry, not unskip, requeues a failure")
        store.close()


class _Console:
    """An in-process web console on a free port, for tests."""

    def __init__(self, tmp, cfg=None, token="t0ken"):
        from vcfaimport.web.api import WebApp
        from vcfaimport.web.server import make_server
        import threading
        if cfg is None:
            cfg = Config()
            # Never let a unit test reach a real cluster through kubectl on PATH.
            cfg.kubectl = "vcfa-test-no-such-kubectl"
        if cfg.workdir == "./run":
            cfg.workdir = os.path.join(tmp, "run")
        self.app = WebApp(cfg, config_path=None, folder_map=os.path.join(tmp, "folder-map.csv"),
                          network_map=os.path.join(tmp, "portgroup-map.csv"),
                          tag_map=os.path.join(tmp, "tag-map.csv"))
        self.server = make_server(self.app, "127.0.0.1", 0, token=token)
        self.base = "http://127.0.0.1:{}".format(self.server.server_address[1])
        self.token = token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, token=None, headers=None, raw=False):
        import urllib.error
        import urllib.request
        h = {"Content-Type": "application/json"}
        if token is not False:
            h["X-VCFA-Token"] = token or self.token
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, method=method, headers=h,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
                return resp.status, (data if raw else json.loads(data)), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            data = exc.read()
            try:
                return exc.code, json.loads(data), dict(exc.headers)
            except ValueError:
                return exc.code, data, dict(exc.headers)

    def ok(self, method, path, body=None):
        status, data, _ = self.call(method, path, body)
        assert status == 200, "{} {} -> {}: {}".format(method, path, status, data)
        return data

    def wait(self, job, timeout=240):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.ok("GET", "/api/jobs/" + job["id"])
            if snap["status"] != "running":
                return snap
            time.sleep(0.3)
        raise AssertionError("job {} did not finish".format(job["id"]))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()


@test("web console: token, Host check, static assets and the confirm guard")
def t_web_security():
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            status, body, headers = c.call("GET", "/", token=False, raw=True)
            eq(status, 200)
            assert b"core.js" in body and "script-src 'self'" in headers["Content-Security-Policy"]
            eq(c.call("GET", "/static/app.css", token=False, raw=True)[0], 200)
            eq(c.call("GET", "/static/..%2Fapi.py", token=False, raw=True)[0], 404)
            eq(c.call("GET", "/api/info", token=False)[0], 401, "no token")
            eq(c.call("GET", "/api/info", token="wrong")[0], 401, "wrong token")
            eq(c.call("GET", "/api/info", headers={"Host": "evil.example:80"})[0], 403,
               "DNS rebinding: a foreign Host is refused on loopback")
            info = c.ok("GET", "/api/info")
            eq(info["settings"]["commit_action"], "Auto")
            # Downloads cannot send a header, so exports alone accept ?t=
            eq(c.call("GET", "/api/export/tracker.csv?t=" + c.token, token=False, raw=True)[0], 200)
            eq(c.call("GET", "/api/overview?t=" + c.token, token=False)[0], 401)
            status, body, _ = c.call("POST", "/api/run/execute", {"stage": "import"})
            eq(status, 400)
            assert "confirm" in body["error"], body
            eq(c.call("POST", "/api/run/rollback", {"failed": True})[0], 400)
            eq(c.call("POST", "/api/vms/wave", {"morefs": ["vm-1"], "wave": 0})[0], 400)
            eq(c.call("GET", "/api/nope")[0], 404)
        finally:
            c.close()


@test("web jobs: one at a time, logged to disk, reloaded after a restart")
def t_web_jobs():
    import threading
    from vcfaimport.web.jobs import FAILED, JobBusy, JobManager, STOPPED, SUCCEEDED
    with tempfile.TemporaryDirectory() as tmp:
        jm = JobManager(Path(tmp))
        gate = threading.Event()

        def slow(job):
            job.log("first\nsecond")
            stop = threading.Event()
            job.on_stop(stop.set)
            gate.wait(10)
            stop.wait(10)
            return {"n": 1}

        job = jm.start("x", "Slow job", {}, slow)
        try:
            jm.start("y", "Another", {}, lambda j: {})
        except JobBusy:
            pass
        else:
            raise AssertionError("a second job must wait for the first")
        gate.set()
        import time
        for _ in range(100):
            if job.stoppable:
                break
            time.sleep(0.05)
        assert job.request_stop()
        for _ in range(100):
            if job.status != "running":
                break
            time.sleep(0.05)
        eq(job.status, STOPPED)
        eq([line[2] for line in job.snapshot()["log"]], ["first", "second"])
        eq(job.snapshot(since=1)["log"][0][2], "second", "incremental reads")

        boom = jm.start("z", "Broken", {}, lambda j: 1 / 0)
        for _ in range(100):
            if boom.status != "running":
                break
            time.sleep(0.05)
        eq(boom.status, FAILED)
        assert "division" in boom.error

        again = JobManager(Path(tmp))
        eq({j["id"] for j in again.list()}, {job.id, boom.id})
        eq(again.get(job.id).snapshot()["log"][1][2], "second")
        eq(again.get(job.id).status, STOPPED)
        eq(SUCCEEDED, "succeeded")


@test("e2e web: discover, select, stage, arrange, precheck, import, triage, roll back, retry")
def t_e2e_web_campaign():
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter
    server, port = fake_vcenter.serve(count=40)
    saved = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.update(_e2e_env(tmp, "import-flaky", settle="1"))
            os.environ.update(VCFA_VC_SERVER="http://127.0.0.1:{}".format(port),
                              VCFA_VC_USER=fake_vcenter.USER, VCFA_VC_PASSWORD=fake_vcenter.PASSWORD)
            _seed_cluster(tmp, ["ns-web", "ns-db"], ["ns-web/sub-prod", "ns-db/sub-prod"])
            cfg = Config.load(_write_config(tmp, failure_rate_abort=0.9))
            c = _Console(tmp, cfg)
            try:
                job = c.ok("POST", "/api/run/discover", {})["job"]
                snap = c.wait(job)
                eq(snap["status"], "succeeded", snap.get("error"))
                eq(snap["result"]["discovered"], 40)
                assert all(line[2] != fake_vcenter.PASSWORD for line in snap["log"])

                vms = c.ok("GET", "/api/discovered")["vms"]
                picked = [v["moref"] for v in vms if v["folder"].startswith(("Production", "Databases"))]
                c.ok("POST", "/api/select", {"morefs": picked, "selected": True})

                # Maps edited in the browser: preview with unsaved rows, then stage (saves them).
                folder_rows = [{"folder": "Production", "namespace": "ns-web", "wave": "1"},
                               {"folder": "Databases", "namespace": "ns-db", "wave": "2"}]
                network_rows = [{"portgroup": "VLAN*", "subnet": "sub-prod"},
                                {"portgroup": "DMZ-Uplink", "subnet": "sub-prod"}]
                body = {"folder_rows": folder_rows, "network_rows": network_rows}
                preview = c.ok("POST", "/api/stage/preview", body)
                eq(len(preview["records"]), len(picked))
                eq(preview["problems"], [])
                staged = c.ok("POST", "/api/stage", dict(body, save_maps=True))
                eq(staged["added"], len(picked))
                assert Path(tmp, "folder-map.csv").is_file(), "staging saved the maps"
                eq(len(c.ok("GET", "/api/maps")["folder"]["rows"]), 2)

                queue = c.ok("GET", "/api/vms")["vms"]
                db = [v["moref"] for v in queue if v["wave"] == 2]
                eq(c.ok("POST", "/api/waves/swap", {"a": 1, "b": 2}), {"swapped": True})
                eq({v["wave"] for v in c.ok("GET", "/api/vms")["vms"] if v["moref"] in db}, {1},
                   "Databases now runs first")

                eq(c.wait(c.ok("POST", "/api/run/preflight", {})["job"])["result"]["ok"], True)
                plan = c.ok("POST", "/api/execute/preview", {"stage": "precheck", "waves": [1]})
                eq(plan["total"], len(db))
                pre = c.wait(c.ok("POST", "/api/run/execute",
                                  {"stage": "precheck", "confirm": True})["job"])
                eq(pre["status"], "succeeded")
                eq(pre["result"]["succeeded"], len(picked))

                imp = c.wait(c.ok("POST", "/api/run/execute",
                                  {"stage": "import", "confirm": True})["job"])
                eq(imp["status"], "warning", "import-flaky fails some VMs")
                failed = imp["result"]["failed"]
                assert failed > 0

                tri = c.ok("GET", "/api/triage")
                eq(sum(g["count"] for g in tri["groups"]), failed)
                eq(tri["groups"][0]["issue"]["id"], "tools")

                rb = c.ok("POST", "/api/rollback/preview", {"failed": True})
                eq(rb["revert"], failed)
                done = c.wait(c.ok("POST", "/api/run/rollback",
                                   {"failed": True, "delete": True, "confirm": True})["job"])
                eq(done["result"]["reverted"], failed)
                eq(done["result"]["errors"], [])

                again = c.ok("POST", "/api/vms/retry", {})
                eq(again["requeued"], failed)
                counts = c.ok("GET", "/api/overview")["counts"]
                eq(counts.get("pending"), failed)
                eq(counts.get("committed"), len(picked) - failed)

                detail = c.ok("GET", "/api/vms/" + queue[0]["moref"])
                assert len(detail["transitions"]) >= 4
                batches = c.ok("GET", "/api/batches")["batches"]
                assert any(b["state"] == "deleted" for b in batches)
                kinds = [j["kind"] for j in c.ok("GET", "/api/jobs")["jobs"]]
                eq(kinds, ["rollback", "execute", "execute", "preflight", "discover"])
                assert list(Path(cfg.workdir, "jobs").glob("*-execute.log")), "job logs on disk"
            finally:
                c.close()
    finally:
        os.environ.clear()
        os.environ.update(saved)
        server.shutdown()


@test("e2e web: Stop applies nothing new and leaves nothing half-tracked")
def t_e2e_web_stop():
    saved = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.update(_e2e_env(tmp, "happy", settle="2"))
            _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
            cfg = Config.load(_write_config(tmp, batch_size=2, max_parallel_batches=1))
            _run_cli(["-c", str(Path(tmp, "cfg.toml")), "load", "-i", _inventory(tmp, 12, waves=(1,))],
                     dict(os.environ), tmp)
            c = _Console(tmp, cfg)
            try:
                job = c.ok("POST", "/api/run/execute", {"stage": "precheck", "confirm": True})["job"]
                import time
                for _ in range(100):
                    snap = c.ok("GET", "/api/jobs/" + job["id"])
                    if snap["stoppable"] and any("applying" in l[2] for l in snap["log"]):
                        break
                    time.sleep(0.1)
                eq(c.call("POST", "/api/run/refresh", {})[0], 409, "one job at a time")
                c.ok("POST", "/api/jobs/{}/stop".format(job["id"]))
                snap = c.wait(job)
                eq(snap["status"], "stopped")
                counts = c.ok("GET", "/api/overview")["counts"]
                eq(counts.get("precheck_running", 0), 0, "in-flight batches were finished")
                assert counts.get("pending", 0) > 0, "and nothing new was applied: {}".format(counts)
            finally:
                c.close()
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ============================================================= stress: HTTP
def _raw_http(port, payload, timeout=3.0):
    """Send raw bytes, return every HTTP status code that comes back."""
    import re as _re
    import socket
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(payload)
    data = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    except (socket.timeout, ConnectionResetError, ConnectionAbortedError):
        pass
    finally:
        s.close()
    return [int(c) for c in _re.findall(r"HTTP/1\.[01] (\d{3})", data.decode("latin-1"))]


@test("stress/http: a rejected request's body is never parsed as the next request")
def t_http_keepalive_desync():
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            port = c.server.server_address[1]
            smuggled = b"GET /api/info HTTP/1.1\r\nHost: 127.0.0.1\r\nX-VCFA-Token: t0ken\r\n\r\n"
            for prefix in (b"POST /api/select", b"POST /api/nowhere", b"PUT /api/maps",
                           b"GET /api/vms"):
                req = (prefix + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                       b"Content-Length: %d\r\n\r\n" % len(smuggled)) + smuggled
                req += b"GET /api/pulse HTTP/1.1\r\nHost: 127.0.0.1\r\nX-VCFA-Token: t0ken\r\n\r\n"
                codes = _raw_http(port, req)
                eq(len(codes), 2, "{}: one response per real request, got {}".format(prefix, codes))
                eq(codes[1], 200, prefix.decode())
        finally:
            c.close()


@test("stress/http: oversized, malformed and chunked bodies are refused cleanly")
def t_http_body_limits():
    from vcfaimport.web.server import MAX_BODY
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            port = c.server.server_address[1]
            head = b"POST /api/select HTTP/1.1\r\nHost: 127.0.0.1\r\nX-VCFA-Token: t0ken\r\n"
            eq(_raw_http(port, head + b"Content-Length: %d\r\n\r\n{}" % (MAX_BODY + 1)), [413])
            eq(_raw_http(port, head + b"Content-Length: banana\r\n\r\n"), [400])
            eq(_raw_http(port, head + b"Content-Length: -5\r\n\r\n"), [400])
            eq(_raw_http(port, head + b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"), [400])
            for body in (b"[1,2]", b'"text"', b"{not json", b"\xff\xfe\x00", b"null", b"42"):
                codes = _raw_http(port, head + b"Content-Length: %d\r\n\r\n" % len(body) + body)
                eq(codes, [400], repr(body))
            eq(c.call("GET", "/api/pulse")[0], 200, "still healthy afterwards")
        finally:
            c.close()


@test("stress/http: every route survives a matrix of hostile inputs without a 500")
def t_http_fuzz():
    import time as _t
    from vcfaimport.web.api import ROUTES
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            _seed_store_with_vms(c.app.store, 30)
            bodies = [
                {}, {"morefs": 5}, {"morefs": "vm-1,vm-2"}, {"morefs": [None, 3, {"x": 1}]},
                {"morefs": ["vm-%d" % i for i in range(5000)]}, {"wave": "x"}, {"wave": -3},
                {"wave": 10 ** 12}, {"wave": 1.5}, {"waves": "1,x"}, {"waves": [None]},
                {"stage": "bogus"}, {"stage": None}, {"a": "1", "b": 1}, {"a": 0, "b": 0},
                {"selected": "yes", "morefs": ["vm-1"]}, {"namespace": ["list"]},
                {"folder_rows": "nope"}, {"folder_rows": [{"folder": "X", "wave": "soon"}]},
                {"network_rows": [None]}, {"folder_rows": [[1, 2]]},
                {"default_wave": "zero"}, {"batches": 7}, {"folders": [["nested"]]},
                {"limit": "-1", "stage": "precheck", "confirm": True},
                {"batch_size": 0, "stage": "import", "confirm": True},
                {"interval": "fast"}, {"server": "x" * 5000, "user": "u", "password": "p",
                                      "timeout": 5},
                {"‮": "\U0001F4A5", "morefs": ["\u0000", "'; DROP TABLE vms; --"]},
                {"nested": {"deep": [[[[[[[[[[{}]]]]]]]]]]}},
            ]
            paths = {
                "POST": [r.pattern.strip("^$") for m, r, _ in ROUTES if m == "POST"],
                "PUT": ["/api/maps"],
            }
            fives = []
            for method, plist in paths.items():
                for raw_path in plist:
                    path = (raw_path.replace("(?P<job_id>[\\w.-]+)", "nope")
                            .replace("(?P<kind>discover|preflight|execute|refresh|watch|commit|"
                                     "rollback|abandon|cleanup)", "{kind}"))
                    kinds = ["discover", "preflight", "execute", "refresh", "watch", "commit",
                             "rollback", "abandon", "cleanup"] if "{kind}" in path else [None]
                    for kind in kinds:
                        p = path.replace("{kind}", kind or "")
                        for body in bodies:
                            status, data, _ = c.call(method, p, body)
                            if status >= 500:
                                fives.append((method, p, body, data))
                            # a job that did start must not block the rest of the matrix
                            active = c.app.jobs.active
                            if active is not None:
                                active.request_stop()
                                for _ in range(200):
                                    if c.app.jobs.active is None:
                                        break
                                    _t.sleep(0.05)
            for bad in [["GET", "/api/vms/%00"], ["GET", "/api/vms/" + "x" * 3000],
                        ["GET", "/api/batches/a/b"], ["GET", "/api/jobs/..%2F..%2Fstate.db"],
                        ["GET", "/api/events?limit=abc"], ["GET", "/api/events?limit=-4"],
                        ["GET", "/api/transitions?limit=99999999999"], ["DELETE", "/api/vms"],
                        ["GET", "/api/export/state.db?t=t0ken"]]:
                status, data, _ = c.call(bad[0], bad[1])
                if status >= 500 and status != 501:
                    fives.append((bad, data))
            assert not fives, "server errors:\n" + "\n".join(repr(f)[:300] for f in fives[:10])
            eq(c.call("GET", "/api/pulse")[0], 200)
            # Valid-but-odd requests (a 5000-moref skip) may legitimately change states;
            # what must hold is that nothing was lost, duplicated or corrupted.
            eq(sum(c.app.store.counts().values()), 30, "no VM lost or duplicated")
            eq(c.app.store.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            for line in Path(c.app.store.ledger_path).read_text(encoding="utf-8").splitlines():
                json.loads(line)
        finally:
            c.close()


def _seed_store_with_vms(store, count, waves=(1, 2), folders=("Prod/Web", "Prod/DB", "DMZ")):
    """Discovered + queued VMs straight into a store, for API tests that need data."""
    from vcfaimport.inventory import Nic, VmRecord
    disc, recs = [], []
    for i in range(count):
        moref = "vm-{}".format(1000 + i)
        folder = folders[i % len(folders)]
        disc.append({"moref": moref, "name": "vm{:04d}".format(i), "power_state": "POWERED_ON",
                     "folder": folder, "datacenter": "DC1", "cluster": "CL1",
                     "tools_status": "RUNNING",
                     "nics": [{"device_key": 4000, "network_name": "VLAN1"}]})
        recs.append(VmRecord(moref=moref, vm_name="vm{:04d}".format(i), namespace="ns-a",
                             nics=[Nic(4000, "sub-a", "Subnet", "crd.nsx.vmware.com")],
                             wave=waves[i % len(waves)], group=folder.replace("/", "-").lower()))
    store.upsert_discovered(disc)
    store.set_selected([d["moref"] for d in disc], True)
    store.sync_inventory(recs)
    for d in disc:
        store._provenance_from_discovery(d["moref"])
    store.conn.commit()


@test("stress/http: Host header variants (DNS rebinding) and static path traversal")
def t_http_host_and_paths():
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            port = c.server.server_address[1]
            good = ["127.0.0.1", "127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port,
                    "LOCALHOST"]
            bad = ["evil.example", "127.0.0.1.evil.example", "localhost.evil:80", "10.0.0.5",
                   "[fe80::1]:80", "127.0.0.1@evil.example"]
            for host in good:
                eq(c.call("GET", "/api/pulse", headers={"Host": host})[0], 200, host)
            for host in bad:
                eq(c.call("GET", "/api/pulse", headers={"Host": host})[0], 403, host)
            for path in ["/static/../api.py", "/static/%2e%2e/api.py", "/static/..%5Capi.py",
                         "/static/.hidden", "/static/sub/app.css", "/static/", "/static/app.css/",
                         "/static/core.js%00.css", "/static//etc/passwd", "/../vcfaimport/cli.py",
                         "/static/__init__.py", "/static/api.py"]:
                status, body, _ = c.call("GET", path, token=False, raw=True)
                eq(status, 404, path)
                assert b"def " not in body, path
        finally:
            c.close()


@test("stress/http: bound to 0.0.0.0 the Host check relaxes but the token does not")
def t_http_non_loopback():
    from vcfaimport.web.api import WebApp
    from vcfaimport.web.server import make_server
    import threading
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.workdir = os.path.join(tmp, "run")
        app = WebApp(cfg)
        srv = make_server(app, "0.0.0.0", 0, token="abc")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            ok = _raw_http(port, b"GET /api/pulse HTTP/1.1\r\nHost: jumpbox.corp:%d\r\n"
                                 b"X-VCFA-Token: abc\r\nConnection: close\r\n\r\n" % port)
            eq(ok, [200])
            eq(_raw_http(port, b"GET /api/pulse HTTP/1.1\r\nHost: jumpbox.corp\r\n"
                               b"Connection: close\r\n\r\n"), [401])
        finally:
            srv.shutdown()
            srv.server_close()
            app.close()


@test("stress/concurrency: 24 threads editing and reading at once -- no errors, no lost writes")
def t_http_concurrent_edits():
    import threading
    import time as _t
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            _seed_store_with_vms(c.app.store, 480)
            c.ok("POST", "/api/select/clear")
            morefs = ["vm-{}".format(1000 + i) for i in range(480)]
            errors, latencies = [], []
            lock = threading.Lock()

            def writer(k):
                mine = morefs[k * 40:(k + 1) * 40]
                for i in range(0, 40, 4):
                    chunk = mine[i:i + 4]
                    for path, body in (("/api/select", {"morefs": chunk, "selected": True}),
                                       ("/api/vms/wave", {"morefs": chunk, "wave": 3 + k % 3})):
                        t0 = _t.time()
                        status, data, _ = c.call("POST", path, body)
                        with lock:
                            latencies.append(_t.time() - t0)
                            if status != 200:
                                errors.append((path, status, data))

            def reader(k):
                for i in range(15):
                    for path in ("/api/pulse", "/api/overview", "/api/vms", "/api/discovered",
                                 "/api/triage", "/api/batches"):
                        t0 = _t.time()
                        status, data, _ = c.call("GET", path)
                        with lock:
                            latencies.append(_t.time() - t0)
                            if status != 200:
                                errors.append((path, status, data))

            threads = [threading.Thread(target=writer, args=(k,)) for k in range(12)]
            threads += [threading.Thread(target=reader, args=(k,)) for k in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
            assert not errors, errors[:5]
            eq(c.app.store.discovered_counts()["selected"], 480, "every select landed")
            waves = c.app.store.state_matrix("wave")
            eq(sum(sum(v.values()) for w, v in waves.items() if w >= 3), 480, "every move landed")
            latencies.sort()
            p95 = latencies[int(len(latencies) * 0.95)]
            assert p95 < 5.0, "p95 latency {:.2f}s under contention".format(p95)
        finally:
            c.close()


# ============================================================ stress: locks & jobs
@test("stress/lock: one cluster-changing operation per workspace, across processes")
def t_workspace_lock():
    from vcfaimport import service
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.workdir = tmp
        first = service.WorkspaceLock(cfg, "import wave 1").acquire()
        try:
            service.WorkspaceLock(cfg, "rollback").acquire()
        except service.WorkspaceBusy as exc:
            assert "import wave 1" in str(exc) and "pid" in str(exc), exc
        else:
            raise AssertionError("a second holder must be refused")
        first.release()
        service.WorkspaceLock(cfg, "again").acquire().release()

        # A holder that dies without releasing must not wedge the workspace.
        code = ("import sys, os; sys.path.insert(0, {root!r}); "
                "from vcfaimport import service; from vcfaimport.config import Config; "
                "c = Config(); c.workdir = {tmp!r}; service.WorkspaceLock(c, 'doomed').acquire(); "
                "print('held', flush=True); os._exit(9)").format(root=str(ROOT), tmp=tmp)
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        eq(proc.stdout.strip(), "held")
        eq(proc.returncode, 9)
        service.WorkspaceLock(cfg, "after a crash").acquire().release()


@test("stress/lock: a held workspace turns console jobs away with 409, not a failed job")
def t_web_lock_conflict():
    from vcfaimport import service
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            held = service.WorkspaceLock(c.app.cfg, "`import` from the CLI").acquire()
            try:
                for kind, body in (("rollback", {"failed": True, "confirm": True}),
                                   ("commit", {"confirm": True}), ("refresh", {}), ("watch", {}),
                                   ("cleanup", {"confirm": True}),
                                   ("execute", {"stage": "precheck", "confirm": True})):
                    status, data, _ = c.call("POST", "/api/run/" + kind, body)
                    eq(status, 409, kind)
                    assert "from the CLI" in data["error"], data
                eq(c.ok("GET", "/api/jobs")["jobs"], [], "nothing was started")
                # A dry run applies nothing, so it does not need (or wait for) the lock.
                job = c.ok("POST", "/api/run/execute",
                           {"stage": "precheck", "dry_run": True, "confirm": True})["job"]
                eq(c.wait(job)["status"], "succeeded")
            finally:
                held.release()
            job = c.ok("POST", "/api/run/refresh", {})["job"]
            eq(c.wait(job)["status"], "succeeded")
            job = c.ok("POST", "/api/run/refresh", {})["job"]
            eq(c.wait(job)["status"], "succeeded", "the job released the lock when it ended")
        finally:
            c.close()


@test("stress/jobs: Stop works from the first instant, and crashes release everything")
def t_jobs_edge_cases():
    import threading
    import time as _t
    from vcfaimport import service
    from vcfaimport.web.jobs import FAILED, MAX_LINES_IN_MEMORY, JobManager, STOPPED
    with tempfile.TemporaryDirectory() as tmp:
        jm = JobManager(Path(tmp, "jobs"))
        registered = threading.Event()
        stopped = threading.Event()

        def late(job):
            _t.sleep(0.4)                       # Stop arrives before the handler exists
            job.on_stop(stopped.set)
            registered.set()
            stopped.wait(10)
            return {}

        job = jm.start("x", "late", {}, late, stoppable=True)
        assert job.request_stop(), "stoppable jobs accept Stop immediately"
        assert stopped.wait(5), "the late handler fired on registration"
        for _ in range(100):
            if job.status != "running":
                break
            _t.sleep(0.05)
        eq(job.status, STOPPED)

        # Not stoppable: refused, not silently accepted.
        gate = threading.Event()
        j2 = jm.start("y", "busy", {}, lambda j: gate.wait(10) and {})
        assert not j2.request_stop()
        gate.set()
        for _ in range(100):
            if j2.status != "running":
                break
            _t.sleep(0.05)

        # A crash inside a locked job releases the workspace lock.
        cfg = Config()
        cfg.workdir = tmp
        j3 = jm.start("z", "crash", {}, lambda j: [][1], lock=service.WorkspaceLock(cfg, "crash"))
        for _ in range(100):
            if j3.status != "running":
                break
            _t.sleep(0.05)
        eq(j3.status, FAILED)
        assert "IndexError" in "\n".join(l[2] for l in j3.snapshot()["log"])
        service.WorkspaceLock(cfg, "after").acquire().release()

        # Very chatty jobs: memory is capped, reads past the window still work.
        def chatty(job):
            for i in range(MAX_LINES_IN_MEMORY + 5000):
                job.log("line {}".format(i))
            return {}
        j4 = jm.start("w", "chatty", {}, chatty)
        for _ in range(400):
            if j4.status != "running":
                break
            _t.sleep(0.05)
        snap = j4.snapshot(since=0, limit=10)
        eq(snap["log"][0][2], "line 5000", "oldest lines dropped from memory")
        eq(j4.line_count, MAX_LINES_IN_MEMORY + 5000)
        eq(j4.snapshot(since=10 ** 9)["log"], [])
        eq(len(Path(tmp, "jobs", j4.id + ".log").read_text().splitlines()),
           MAX_LINES_IN_MEMORY + 5000, "but the file keeps everything")

        # Corrupt history files are skipped, not fatal.
        Path(tmp, "jobs", "garbage.json").write_text("{not json", encoding="utf-8")
        Path(tmp, "jobs", "noid.json").write_text("{}", encoding="utf-8")
        again = JobManager(Path(tmp, "jobs"))
        assert j4.id in {j["id"] for j in again.list()}


# ============================================================ stress: data edges
@test("stress/data: map editing -- JSON types, globs, unicode, duplicates, failed saves")
def t_web_map_edges():
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            rows = [{"folder": "Prod", "namespace": "ns-a", "wave": 2, "group": None, "extra": "x"},
                    {"folder": "Légacy/Ünïcode ✓", "namespace": "ns-ü", "wave": "3.0"},
                    {"folder": "Legacy/*", "namespace": "ns-g", "wave": ""},
                    {"folder": "Prod", "namespace": "ns-dup", "wave": 1}]
            got = c.ok("PUT", "/api/maps", {"folder_rows": rows, "network_rows": [
                {"portgroup": "VLAN*", "subnet": "s", "device_key": 4001}]})
            eq([r["folder"] for r in got["folder"]["rows"]],
               ["Prod", "Légacy/Ünïcode ✓", "Legacy/*", "Prod"])
            before = Path(tmp, "folder-map.csv").read_bytes()
            status, data, _ = c.call("PUT", "/api/maps", {"folder_rows": [
                {"folder": "X", "namespace": "n", "wave": "tomorrow"}]})
            eq(status, 400)
            assert "wave" in data["error"]
            eq(Path(tmp, "folder-map.csv").read_bytes(), before, "a rejected save changes nothing")
            from vcfaimport.discovery import load_folder_map
            eq(load_folder_map(os.path.join(tmp, "folder-map.csv"))["Prod"].namespace, "ns-dup",
               "a duplicate key: the last row wins, as in the CLI")
            assert not list(Path(tmp).glob("*.tmp")), "no temp files left behind"
        finally:
            c.close()


@test("stress/data: staging edge cases -- no selection, no namespace, locked VMs, defaults")
def t_web_stage_edges():
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            eq(c.ok("POST", "/api/stage/preview", {})["selected"], 0)
            eq(c.call("POST", "/api/stage", {})[0], 400, "nothing selected")

            _seed_store_with_vms(c.app.store, 9)
            c.app.store.set_vm_state("vm-1000", vst.S_IMPORTING, stage="import")
            p = c.ok("POST", "/api/stage/preview", {})
            eq(len(p["problems"]), 9, "no maps, no default: nothing is guessed")
            eq(c.call("POST", "/api/stage", {})[0], 400)

            body = {"folder_rows": [{"folder": "Prod", "namespace": "ns-new", "wave": "5"}],
                    "default_namespace": "ns-default", "default_wave": 2}
            p = c.ok("POST", "/api/stage/preview", body)
            eq(p["problems"], [])
            nss = {r["moref"]: (r["namespace"], r["wave"]) for r in p["records"]}
            eq(nss["vm-1002"], ("ns-default", 2), "DMZ falls back to the default")
            eq(nss["vm-1001"], ("ns-new", 5))
            assert [r for r in p["records"] if r["moref"] == "vm-1000"][0]["locked"]

            r = c.ok("POST", "/api/stage", body)
            assert any("vm-1000" in x for x in r["conflicts"]), r
            vm = c.app.store.get_vm("vm-1000")
            eq((vm["namespace"], vm["state"]), ("ns-a", "importing"), "in flight: untouched")
            eq(c.app.store.get_vm("vm-1003")["namespace"], "ns-new", "pending: updated")
            eq(c.call("POST", "/api/stage/preview", {"default_wave": 0})[0], 400)
        finally:
            c.close()


@test("stress/data: hostile VM names and messages round-trip intact and are escaped in reports")
def t_web_hostile_names():
    from vcfaimport import state as vst
    evil = ['<img src=x onerror="alert(1)">', "Robert'); DROP TABLE vms;--", "名前 ✓ ‮evil",
            "a\"b'c`d", "x" * 400, "tab\there", "</script><script>alert(2)</script>"]
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            store = c.app.store
            store.upsert_discovered([{"moref": "vm-{}".format(i), "name": n,
                                      "folder": "F/" + n[:20], "networks": n[:10]}
                                     for i, n in enumerate(evil)])
            got = {v["moref"]: v for v in c.ok("GET", "/api/discovered")["vms"]}
            for i, n in enumerate(evil):
                eq(got["vm-{}".format(i)]["name"], n)
            store.set_selected(list(got), True)
            p = c.ok("POST", "/api/stage/preview", {"default_namespace": "ns"})
            eq(len(p["records"]), len(evil))
            c.ok("POST", "/api/stage", {"default_namespace": "ns"})
            store.set_vm_state("vm-0", vst.S_FAILED, message=evil[0] + evil[6], stage="import")
            tri = c.ok("GET", "/api/triage")
            eq(tri["groups"][0]["message"], evil[0] + evil[6])
            status, report_html, _ = c.call("GET", "/api/export/report.html?t=t0ken", raw=True)
            eq(status, 200)
            assert b"<script>alert(2)" not in report_html and b'<img src=x' not in report_html
            status, csv_bytes, _ = c.call("GET", "/api/export/tracker.csv?t=t0ken", raw=True)
            assert evil[1].encode() in csv_bytes
            eq(len(c.ok("GET", "/api/vms")["vms"]), len(evil))
            eq(c.ok("GET", "/api/vms/vm-0")["vm"]["vm_name"], evil[0])
        finally:
            c.close()


@test("stress/data: triage normalisation groups by cause, not by VM detail")
def t_triage_normalisation():
    from vcfaimport.service import classify_failure, normalise_message
    same = [
        'failed to create op "web-001-1483400": vm-1483400 is locked by task-99812',
        'failed to create op "db-777-1483999": vm-1483999 is locked by task-1',
    ]
    eq(normalise_message(same[0]), normalise_message(same[1]))
    assert normalise_message("VM Tools not running") != normalise_message("disk full")
    eq(normalise_message(None), "(no message)")
    eq(normalise_message("   "), "(no message)")
    assert len(normalise_message("x" * 10000)) <= 240
    eq(normalise_message("id 6f1c2a9e-1b2c-4d5e-8f90-123456789abc gone"),
       normalise_message("id 0a0b0c0d-1111-2222-3333-444455556666 gone"))
    # Found by the stress run: one kubectl outage split into a group per namespace/batch.
    eq(normalise_message("batch apply failed: kubectl apply -n ns-a failed (exit 127)"),
       normalise_message("batch apply failed: kubectl apply -n prod-db-ns2 failed (exit 127)"))
    eq(normalise_message("batch pre-w1-sub-ns-a-001-3cbaa disappeared from the cluster"),
       normalise_message("batch imp-w12-databases-web-004-9d2c4 disappeared from the cluster"))
    for msg, issue in [
        ("lookup vc.lab on 127.0.0.53:53: read udp: i/o timeout", "dns"),
        ("VM Tools not running", "tools"), ("guest toolsNotRunning", "tools"),
        ("number of operations from status: 0 does not match number of operations from spec: 2",
         "collision"),
        ("ROLLBACK FAILED: vm locked", "rollback"),
        ("batch apply failed: forbidden", "apply"),
        ("batch x disappeared from the cluster during refresh", "vanished"),
        ("batch exceeded batch_timeout_minutes=90", "timeout"),
        ("subnet sub-a not found", "subnet"),
    ]:
        eq((classify_failure(msg) or {}).get("id"), issue, msg)
    eq(classify_failure(""), None)
    eq(classify_failure(None), None)


@test("stress/data: lookups of things that do not exist")
def t_web_missing_things():
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            eq(c.call("GET", "/api/vms/vm-404")[0], 404)
            eq(c.call("GET", "/api/vms/vm%2F..%2F1")[0], 404)
            eq(c.call("GET", "/api/batches/ns/none")[0], 404)
            eq(c.call("GET", "/api/jobs/20260101-000000-001-x")[0], 404)
            eq(c.call("POST", "/api/jobs/nope/stop")[0], 404)
            for name in ("tracker.csv", "transitions.csv", "ledger.jsonl", "report.html"):
                eq(c.call("GET", "/api/export/%s?t=t0ken" % name, raw=True)[0], 200, name)
            _seed_store_with_vms(c.app.store, 2)
            c.app.store.create_batch("b1", "ns-a", "r", 1, "precheck", 2,
                                     manifest_path=os.path.join(tmp, "gone.yaml"))
            eq(c.ok("GET", "/api/batches/ns-a/b1")["manifest"], None, "missing manifest file")
            eq(c.ok("POST", "/api/vms/wave", {"morefs": ["vm-404"], "wave": 2})["moved"], 0)
            eq(c.ok("POST", "/api/vms/retry", {"morefs": ["vm-404"]})["requeued"], 0)
            eq(c.ok("POST", "/api/waves/swap", {"a": 7, "b": 8}), {"swapped": True})
            eq(c.call("POST", "/api/waves/swap", {"a": 1, "b": 1})[0], 400)
        finally:
            c.close()


# ======================================================= stress: failure e2e
class _EnvPatch:
    """Temporarily set os.environ (the fake kubectl reads its scenario from there)."""

    def __init__(self, **values):
        self.values = values
        self.saved = None

    def __enter__(self):
        self.saved = dict(os.environ)
        os.environ.update({k: str(v) for k, v in self.values.items()})
        return self

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.saved)


def _cluster(tmp):
    return json.loads(Path(tmp, "cluster.json").read_text(encoding="utf-8"))


def _queue_console(tmp, scenario, count=8, settle="1", namespaces=("ns-a", "ns-b"),
                   waves=(1,), seed=True, **cfg_overrides):
    """A console over a workspace whose queue was loaded from an inventory CSV."""
    env = _e2e_env(tmp, scenario, settle=settle)
    patch = _EnvPatch(**{k: env[k] for k in ("FAKE_KUBECTL_STATE", "FAKE_KUBECTL_SCENARIO",
                                              "FAKE_KUBECTL_SETTLE")})
    patch.__enter__()
    if seed:
        _seed_cluster(tmp, list(namespaces), ["{0}/sub-{0}".format(n) for n in namespaces])
    cfg_path = _write_config(tmp, **cfg_overrides)
    _run_cli(["-c", cfg_path, "load", "-i", _inventory(tmp, count, namespaces=namespaces,
                                                       waves=waves)], dict(os.environ), tmp)
    return _Console(tmp, Config.load(cfg_path)), patch, cfg_path


def _run_job(c, kind, body=None, expect_status=None):
    snap = c.wait(c.ok("POST", "/api/run/" + kind, dict(body or {}))["job"])
    if expect_status:
        eq(snap["status"], expect_status, "{}: {}".format(kind, snap.get("error")))
    return snap


@test("stress/e2e: the circuit breaker halts a disastrous wave and says so")
def t_e2e_web_circuit_breaker():
    with tempfile.TemporaryDirectory() as tmp:
        c, env, _ = _queue_console(tmp, "disaster", count=12, batch_size=2, max_parallel_batches=1,
                                   failure_rate_abort=0.5, failure_rate_min_sample=2)
        try:
            snap = _run_job(c, "execute", {"stage": "precheck", "confirm": True}, "warning")
            assert snap["result"]["halted"] and "failure_rate_abort" in snap["result"]["halted"]
            assert any("RUN HALTED" in l[2] for l in snap["log"])
            assert _cluster(tmp)["applies"] < 6, "the breaker stopped further batches"
            counts = c.ok("GET", "/api/overview")["counts"]
            assert counts.get("pending", 0) > 0, "unapplied VMs stay pending: {}".format(counts)
            eq(counts.get("precheck_running", 0), 0, "nothing left half-tracked")
            # After the operator is healthy again, the rest go through.
            os.environ["FAKE_KUBECTL_SCENARIO"] = "happy"
            _run_job(c, "execute", {"stage": "precheck", "include_failed": True, "confirm": True},
                     "succeeded")
            eq(c.ok("GET", "/api/overview")["counts"], {"precheck_passed": 12})
        finally:
            c.close()
            env.__exit__()


@test("stress/e2e: commitAction Wait -- held, refused without confirm, committed on request")
def t_e2e_web_commit_gate():
    with tempfile.TemporaryDirectory() as tmp:
        c, env, _ = _queue_console(tmp, "wait", count=4, commit_action="Wait")
        try:
            _run_job(c, "execute", {"stage": "precheck", "confirm": True}, "succeeded")
            _run_job(c, "execute", {"stage": "import", "confirm": True}, "succeeded")
            eq(c.ok("GET", "/api/overview")["awaiting_commit"], 4)
            eq(len(c.ok("GET", "/api/triage")["awaiting_commit"]), 4)
            eq(c.call("POST", "/api/run/commit", {})[0], 400, "commit needs confirm")
            eq(c.call("POST", "/api/run/commit", {"confirm": "yes"})[0], 400, "exactly true")
            snap = _run_job(c, "commit", {"confirm": True}, "succeeded")
            eq(snap["result"]["vms"], 4)
            for _ in range(6):
                _run_job(c, "refresh")
                if c.ok("GET", "/api/overview")["committed"] == 4:
                    break
            eq(c.ok("GET", "/api/overview")["counts"], {"committed": 4})
            # Committed is irreversible: rollback finds nothing to revert.
            snap = _run_job(c, "rollback", {"failed": True, "confirm": True})
            eq(snap["result"].get("reverted", 0), 0)
            eq(c.ok("GET", "/api/overview")["counts"], {"committed": 4})
        finally:
            c.close()
            env.__exit__()


@test("stress/e2e: kubectl missing -- preflight fails clearly, a run fails every VM cleanly")
def t_e2e_web_kubectl_missing():
    with tempfile.TemporaryDirectory() as tmp:
        c, env, _ = _queue_console(tmp, "happy", count=4, kubectl="vcfa-definitely-not-kubectl")
        try:
            snap = _run_job(c, "preflight", {}, "failed")
            assert "not found" in snap["error"], snap["error"]
            snap = _run_job(c, "execute", {"stage": "precheck", "confirm": True}, "warning")
            eq(snap["result"]["failed"], 4)
            eq(c.ok("GET", "/api/overview")["counts"], {"precheck_failed": 4})
            tri = c.ok("GET", "/api/triage")["groups"]
            eq([g["issue"]["id"] for g in tri], ["apply"])
            eq(c.ok("POST", "/api/vms/retry", {})["requeued"], 4, "and they can be retried")
        finally:
            c.close()
            env.__exit__()


@test("stress/e2e: preflight names a missing namespace and a crash-looping operator")
def t_e2e_web_preflight_problems():
    with tempfile.TemporaryDirectory() as tmp:
        c, env, _ = _queue_console(tmp, "happy", count=4, seed=False)
        _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-ns-a"])
        os.environ["FAKE_KUBECTL_OPERATOR_POD"] = "CrashLoopBackOff"
        try:
            snap = _run_job(c, "preflight", {}, "warning")
            r = snap["result"]
            eq(r["ok"], False)
            text = "\n".join(r["problems"])
            assert "ns-b" in text, text
            assert "Mobility Operator" in text, text
            failed_checks = {x["name"] for x in r["checks"] if not x["ok"]}
            assert "target namespaces exist" in failed_checks, failed_checks
        finally:
            c.close()
            env.__exit__()


@test("stress/e2e: vCenter wrong password, unreachable, and missing -- never leaks the password")
def t_e2e_web_vcenter_failures():
    import socket
    import time as _t
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter
    server, port = fake_vcenter.serve(count=5)
    try:
        with tempfile.TemporaryDirectory() as tmp, _EnvPatch():
            for k in ("VCFA_VC_SERVER", "VCFA_VC_USER", "VCFA_VC_PASSWORD"):
                os.environ.pop(k, None)
            c = _Console(tmp)
            try:
                url = "http://127.0.0.1:{}".format(port)
                secret = "Sup3r-S3cret-!"
                snap = _run_job(c, "discover", {"server": url, "user": fake_vcenter.USER,
                                                "password": secret}, "failed")
                assert "401" in snap["error"] or "auth" in snap["error"].lower(), snap["error"]
                everything = json.dumps(snap) + "".join(
                    p.read_text(encoding="utf-8") for p in Path(c.app.cfg.workdir, "jobs").glob("*"))
                assert secret not in everything, "the password leaked into a job record"

                s = socket.socket()
                s.bind(("127.0.0.1", 0))
                dead = s.getsockname()[1]
                s.close()
                t0 = _t.time()
                snap = _run_job(c, "discover", {"server": "http://127.0.0.1:{}".format(dead),
                                                "user": "u", "password": "p", "timeout": 5}, "failed")
                assert _t.time() - t0 < 30, "an unreachable vCenter fails fast"

                eq(c.call("POST", "/api/run/discover", {"server": url, "user": "u"})[0], 400)
                eq(c.call("POST", "/api/run/discover", {"password": "p"})[0], 400)
                eq(c.ok("GET", "/api/discovered")["vms"], [], "failures leave the cache alone")
            finally:
                c.close()
    finally:
        server.shutdown()


@test("stress/e2e: a batch deleted behind the tool's back is caught and triaged")
def t_e2e_web_vanished_batch():
    import sqlite3 as sq
    import time as _t
    with tempfile.TemporaryDirectory() as tmp:
        c, env, cfg_path = _queue_console(tmp, "happy", count=2, settle="1000",
                                          namespaces=("ns-a",))
        try:
            job = c.ok("POST", "/api/run/execute", {"stage": "precheck", "confirm": True})["job"]
            for _ in range(200):
                if _cluster(tmp)["batches"]:
                    break
                _t.sleep(0.1)
            c.ok("POST", "/api/jobs/{}/stop".format(job["id"]))
            cluster = _cluster(tmp)
            cluster["batches"] = {}                       # kubectl delete, outside the tool
            Path(tmp, "cluster.json").write_text(json.dumps(cluster))
            conn = sq.connect(str(Path(c.app.cfg.workdir, "state.db")))
            conn.execute("UPDATE batches SET applied_at='2026-01-01T00:00:00Z'")
            conn.commit()
            conn.close()
            c.wait(job)
            _run_job(c, "refresh", {}, "succeeded")
            tri = c.ok("GET", "/api/triage")["groups"]
            eq([g["issue"]["id"] for g in tri], ["vanished"])
            eq(tri[0]["count"], 2)
        finally:
            c.close()
            env.__exit__()


def _spawn_console(tmp, cfg_path, env, token="tok"):
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "vcfa-import.py"), "-c", cfg_path, "serve", "--port", "0",
         "--token", token], cwd=tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    for _ in range(200):
        line = proc.stdout.readline()
        if "open" in line and "http://" in line:
            base = line.split("open", 1)[1].split(":", 1)[1].strip().split("/#")[0]
            return proc, base
    proc.kill()
    raise AssertionError("console did not start")


def _http(base, method, path, body=None, token="tok"):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(base + path, method=method,
                                 headers={"X-VCFA-Token": token, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@test("stress/e2e: the console is killed mid-run; a restart resumes with no duplicate batches")
def t_e2e_web_crash_resume():
    import time as _t
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy", settle="3")
        _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-ns-a", "ns-b/sub-ns-b"])
        cfg_path = _write_config(tmp, batch_size=2, max_parallel_batches=2)
        _run_cli(["-c", cfg_path, "load", "-i", _inventory(tmp, 8, waves=(1,))], env, tmp)

        proc, base = _spawn_console(tmp, cfg_path, env)
        try:
            status, r = _http(base, "POST", "/api/run/execute", {"stage": "precheck", "confirm": True})
            eq(status, 200)
            job_id = r["job"]["id"]
            for _ in range(300):
                if _cluster(tmp)["applies"] >= 1:
                    break
                _t.sleep(0.1)
            _t.sleep(0.5)
        finally:
            proc.kill()                        # a crash: no cleanup, no graceful stop
            proc.wait()
        applied_before = _cluster(tmp)["applies"]
        assert applied_before >= 1

        proc, base = _spawn_console(tmp, cfg_path, env)
        try:
            jobs = _http(base, "GET", "/api/jobs")[1]["jobs"]
            eq([j["status"] for j in jobs if j["id"] == job_id], ["stopped"],
               "the interrupted job is recorded as stopped")
            ov = _http(base, "GET", "/api/overview")[1]
            assert ov["in_flight"] > 0 and ov["live_batches"], "batches outlived the console"
            status, r = _http(base, "POST", "/api/run/watch", {"interval": 1})
            eq(status, 200, "the crashed holder's lock was released by the OS")
            for _ in range(300):
                j = _http(base, "GET", "/api/jobs/" + r["job"]["id"])[1]
                if j["status"] != "running":
                    break
                _t.sleep(0.2)
            eq(j["status"], "succeeded")
            eq(_http(base, "GET", "/api/overview")[1]["in_flight"], 0)
            status, r = _http(base, "POST", "/api/run/execute", {"stage": "precheck", "confirm": True})
            eq(status, 200)
            for _ in range(600):
                j = _http(base, "GET", "/api/jobs/" + r["job"]["id"])[1]
                if j["status"] != "running":
                    break
                _t.sleep(0.2)
            eq(_http(base, "GET", "/api/overview")[1]["counts"], {"precheck_passed": 8})
            eq(_cluster(tmp)["applies"], 4, "8 VMs / 2 per batch: nothing applied twice")
        finally:
            proc.kill()
            proc.wait()
        _run_cli(["-c", cfg_path, "ledger", "--verify"], env, tmp)


@test("stress/e2e: CLI and console on one workspace -- a second run is refused, reads work")
def t_e2e_web_cli_interplay():
    import time as _t
    with tempfile.TemporaryDirectory() as tmp:
        c, env, cfg_path = _queue_console(tmp, "happy", count=8, settle="3", batch_size=2,
                                          max_parallel_batches=1)
        try:
            job = c.ok("POST", "/api/run/execute", {"stage": "precheck", "confirm": True})["job"]
            for _ in range(100):
                if _cluster(tmp)["applies"]:
                    break
                _t.sleep(0.1)
            out = _run_cli(["-c", cfg_path, "precheck", "-y"], dict(os.environ), tmp, expect=7)
            assert "web console" in out.stdout, out.stdout
            _run_cli(["-c", cfg_path, "status"], dict(os.environ), tmp)
            _run_cli(["-c", cfg_path, "rollback", "--failed", "-y"], dict(os.environ), tmp,
                     expect=None)
            eq(c.wait(job)["status"], "succeeded")
            _run_cli(["-c", cfg_path, "ledger", "--verify"], dict(os.environ), tmp)
            cli_vms = json.loads(_run_cli(["-c", cfg_path, "vms", "--json"], dict(os.environ),
                                          tmp).stdout)
            web_vms = c.ok("GET", "/api/vms")["vms"]
            eq(sorted((v["moref"], v["state"]) for v in cli_vms),
               sorted((v["moref"], v["state"]) for v in web_vms), "one truth, two views")
            eq(_cluster(tmp)["applies"], 4)
            # And the other way round: a CLI run holds the lock, the console is refused.
            proc = subprocess.Popen([sys.executable, str(ROOT / "vcfa-import.py"), "-c", cfg_path,
                                     "run", "-y"], cwd=tmp, env=dict(os.environ),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                for _ in range(200):
                    if _cluster(tmp)["applies"] > 4:
                        break
                    _t.sleep(0.1)
                status, data, _ = c.call("POST", "/api/run/execute", {"stage": "import", "confirm": True})
                eq(status, 409)
                assert "from the CLI" in data["error"], data
            finally:
                proc.communicate(timeout=300)
            eq(proc.returncode, 0)
        finally:
            c.close()
            env.__exit__()


@test("stress/e2e: 12 clients hammer the console during a live import -- no errors")
def t_e2e_web_load_during_run():
    import threading
    import time as _t
    with tempfile.TemporaryDirectory() as tmp:
        c, env, _ = _queue_console(tmp, "import-flaky", count=40, settle="2", batch_size=4,
                                   max_parallel_batches=3, failure_rate_abort=0.95)
        try:
            _run_job(c, "execute", {"stage": "precheck", "confirm": True}, "succeeded")
            job = c.ok("POST", "/api/run/execute", {"stage": "import", "confirm": True})["job"]
            stop = threading.Event()
            errors, latencies, lock = [], [], threading.Lock()
            paths = ["/api/pulse", "/api/overview", "/api/vms", "/api/batches", "/api/triage",
                     "/api/jobs", "/api/jobs/" + job["id"] + "?since=0", "/api/events",
                     "/api/transitions?limit=200", "/api/discovered"]

            def client(k):
                i = 0
                while not stop.is_set():
                    path = paths[(i + k) % len(paths)]
                    t0 = _t.time()
                    try:
                        status, data, _ = c.call("GET", path)
                    except Exception as exc:  # noqa: BLE001
                        status, data = -1, repr(exc)
                    with lock:
                        latencies.append(_t.time() - t0)
                        if status != 200:
                            errors.append((path, status, str(data)[:200]))
                    i += 1

            threads = [threading.Thread(target=client, args=(k,)) for k in range(12)]
            for t in threads:
                t.start()
            snap = c.wait(job)
            stop.set()
            for t in threads:
                t.join(30)
            assert not errors, errors[:5]
            assert len(latencies) > 200, "the clients were actually busy: {}".format(len(latencies))
            latencies.sort()
            p95 = latencies[int(len(latencies) * 0.95)]
            assert p95 < 3.0, "p95 {:.2f}s".format(p95)
            eq(snap["status"], "warning")
            counts = c.ok("GET", "/api/overview")["counts"]
            eq(counts.get("committed", 0) + counts.get("failed", 0), 40, counts)
            eq(counts.get("failed"), snap["result"]["failed"])
        finally:
            c.close()
            env.__exit__()


@test("stress/e2e: retry ceiling, forced retry, abandon, refused abandon of a live import")
def t_e2e_web_retry_abandon():
    with tempfile.TemporaryDirectory() as tmp:
        c, env, _ = _queue_console(tmp, "import-flaky", count=10, max_retries=0,
                                   failure_rate_abort=0.95)
        try:
            _run_job(c, "execute", {"stage": "precheck", "confirm": True}, "succeeded")
            snap = _run_job(c, "execute", {"stage": "import", "confirm": True}, "warning")
            failed = snap["result"]["failed"]
            _run_job(c, "rollback", {"failed": True, "delete": True, "confirm": True}, "succeeded")
            eq(c.ok("POST", "/api/vms/retry", {})["requeued"], 0, "max_retries=0 holds them back")
            eq(c.ok("POST", "/api/vms/retry", {"force": True})["requeued"], failed)

            # Abandon: requeued VMs get a fresh precheck, then that batch is discarded.
            os.environ["FAKE_KUBECTL_SCENARIO"] = "flaky"
            snap = _run_job(c, "execute", {"stage": "precheck", "confirm": True})
            pre_failed = [v["moref"] for v in c.ok("GET", "/api/vms")["vms"]
                          if v["state"] == "precheck_failed"]
            assert pre_failed, "flaky fails some prechecks"
            prev = c.ok("POST", "/api/abandon/preview", {"morefs": pre_failed})
            assert prev["batches"] and all(b["stage"] == "precheck" for b in prev["batches"])
            _run_job(c, "abandon", {"morefs": pre_failed, "confirm": True}, "succeeded")
            states = {v["moref"]: v["state"] for v in c.ok("GET", "/api/vms")["vms"]}
            assert all(states[m] == "pending" for m in pre_failed), states

            # A held import cannot be abandoned: that would strand VM ownership.
            os.environ["FAKE_KUBECTL_SCENARIO"] = "wait"
            c.app.cfg.commit_action = "Wait"
            _run_job(c, "execute", {"stage": "precheck", "confirm": True})
            _run_job(c, "execute", {"stage": "import", "confirm": True})
            held = [v["moref"] for v in c.ok("GET", "/api/vms")["vms"] if v["state"] == "awaiting_commit"]
            assert held
            prev = c.ok("POST", "/api/abandon/preview", {"morefs": held})
            eq(prev["batches"], [])
            assert any("refused" in n for n in prev["notes"]), prev["notes"]
            snap = _run_job(c, "abandon", {"morefs": held, "confirm": True}, "warning")
            eq(snap["result"]["deleted"], [])
        finally:
            c.close()
            env.__exit__()


@test("stress/e2e: dry run changes nothing; folder scope and per-wave limits are honoured")
def t_e2e_web_scope_limits():
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter
    server, port = fake_vcenter.serve(count=48)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = _e2e_env(tmp, "happy", settle="1")
            with _EnvPatch(FAKE_KUBECTL_STATE=env["FAKE_KUBECTL_STATE"], FAKE_KUBECTL_SCENARIO="happy",
                           FAKE_KUBECTL_SETTLE="1", VCFA_VC_SERVER="http://127.0.0.1:{}".format(port),
                           VCFA_VC_USER=fake_vcenter.USER, VCFA_VC_PASSWORD=fake_vcenter.PASSWORD):
                _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-a"])
                c = _Console(tmp, Config.load(_write_config(tmp)))
                try:
                    _run_job(c, "discover", {}, "succeeded")
                    all_vms = c.ok("GET", "/api/discovered")["vms"]
                    c.ok("POST", "/api/select", {"morefs": [v["moref"] for v in all_vms], "selected": True})
                    c.ok("POST", "/api/stage", {"default_namespace": "ns-a", "network_rows": [
                        {"portgroup": "*", "subnet": "sub-a"}]})
                    before = c.ok("GET", "/api/overview")["counts"]

                    snap = _run_job(c, "execute", {"stage": "precheck", "dry_run": True,
                                                   "confirm": True}, "succeeded")
                    eq(_cluster(tmp)["applies"], 0, "dry run applies nothing")
                    eq(c.ok("GET", "/api/overview")["counts"], before, "and changes no state")
                    eq(c.ok("GET", "/api/batches")["batches"], [])

                    db = [v["moref"] for v in c.ok("GET", "/api/vms")["vms"]
                          if (v["folder"] or "").startswith("Databases")]
                    _run_job(c, "execute", {"stage": "precheck", "folders": ["Databases"],
                                            "confirm": True}, "succeeded")
                    passed = {v["moref"] for v in c.ok("GET", "/api/vms")["vms"]
                              if v["state"] == "precheck_passed"}
                    eq(passed, set(db), "only the scoped folder was touched")

                    _run_job(c, "execute", {"stage": "precheck", "limit": 3, "confirm": True},
                             "succeeded")
                    eq(len([v for v in c.ok("GET", "/api/vms")["vms"]
                            if v["state"] == "precheck_passed"]), len(db) + 3)
                finally:
                    c.close()
    finally:
        server.shutdown()


@test("stress/misc: double-clicked Start, busy port, corrupt map file, bad config, odd paths")
def t_e2e_web_misc():
    import socket
    import threading
    with tempfile.TemporaryDirectory() as root:
        tmp = Path(root, "work dir with spaces ünïcode ✓")
        tmp.mkdir()
        c, env, cfg_path = _queue_console(str(tmp), "happy", count=4, settle="3")
        try:
            # A double click fires two starts at once: exactly one wins.
            results = []

            def start():
                results.append(c.call("POST", "/api/run/execute",
                                      {"stage": "precheck", "confirm": True})[0])
            ts = [threading.Thread(target=start) for _ in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(60)
            eq(sorted(results), [200, 409, 409, 409, 409, 409])
            job = [j for j in c.ok("GET", "/api/jobs")["jobs"] if j["kind"] == "execute"][0]
            eq(c.wait(job)["status"], "succeeded")
            eq(_cluster(str(tmp))["applies"], 2, "one run, not six")

            # Exports and the lock work under an awkward path.
            eq(c.call("GET", "/api/export/tracker.csv?t=t0ken", raw=True)[0], 200)
            eq(c.call("GET", "/api/export/report.html?t=t0ken", raw=True)[0], 200)

            # A map file mangled on disk is a clear 400, not a crash.
            Path(c.app.folder_map_path).write_bytes(b"\xff\xfe\x00\x01garbage\n\x00,,,")
            status, data, _ = c.call("GET", "/api/maps")
            eq(status, 400)
            assert "folder-map.csv" in data["error"], data
            eq(c.call("POST", "/api/stage/preview", {})[0], 400)
            Path(c.app.folder_map_path).write_text("nothing,useful\n1,2\n", encoding="utf-8")
            status, data, _ = c.call("GET", "/api/maps")
            eq(status, 400)
            assert "folder" in data["error"] and "namespace" in data["error"], data
            Path(c.app.folder_map_path).unlink()
            eq(c.call("GET", "/api/maps")[0], 200, "and recovers once the file is fixed")

            # `serve` on a port that is taken: a clear refusal, exit 2.
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            busy = s.getsockname()[1]
            try:
                out = _run_cli(["-c", cfg_path, "serve", "--port", str(busy)], dict(os.environ),
                               str(tmp), expect=2).stdout
                assert "cannot listen" in out, out
            finally:
                s.close()
            bad_cfg = Path(tmp, "bad.toml")
            bad_cfg.write_text('rollback_action = "Immediate"\n', encoding="utf-8")
            out = _run_cli(["-c", str(bad_cfg), "serve"], dict(os.environ), str(tmp), expect=2).stdout
            assert "revert NOW" in out, out
            bad_cfg.write_text("this is = not [valid toml", encoding="utf-8")
            _run_cli(["-c", str(bad_cfg), "serve"], dict(os.environ), str(tmp), expect=2)
        finally:
            c.close()
            env.__exit__()


@test("web demo: a busy default port falls back; an explicit busy port is refused")
def t_web_demo_ports():
    import socket
    sys.path.insert(0, str(ROOT / "tools"))
    import web_demo
    holders = []
    try:
        for port in range(web_demo.DEFAULT_PORT, web_demo.DEFAULT_PORT + 20):
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                s.close()
                continue           # already taken by something else: just as good
            holders.append(s)
            if len(holders) == 2:
                break
        chosen = web_demo.pick_port(None)
        assert chosen is not None and web_demo._port_free(chosen), chosen
        assert chosen not in [h.getsockname()[1] for h in holders]
        eq(web_demo.pick_port(holders[0].getsockname()[1]), None, "an explicit busy port is refused")
    finally:
        for h in holders:
            h.close()


@test("stress/cli: Unicode VM names never crash CLI output piped on Windows")
def t_cli_unicode_output():
    from vcfaimport import state as vst
    with tempfile.TemporaryDirectory() as tmp:
        names = ["名前-✓-db", "ünïcode-web", "emoji-🚀-svc"]
        write_csv(os.path.join(tmp, "inv.csv"),
                  [[n, "vm-{}".format(1001 + i), "ns-a", "sub", "4000", 1] for i, n in enumerate(names)],
                  ["vm_name", "moref", "namespace", "subnet", "device_key", "wave"])
        cfg = _write_config(tmp)
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        env.pop("PYTHONUTF8", None)
        _run_cli(["-c", cfg, "load", "-i", os.path.join(tmp, "inv.csv")], env, tmp)
        store = vst.Store(os.path.join(tmp, "run", "state.db"))
        store.set_vm_state("vm-1001", vst.S_FAILED, stage="import", message="✗ failed: 名前")
        store.close()
        for args in (["vms"], ["status"], ["history", "--vm", "vm-1001"], ["history"],
                     ["events"], ["report"], ["ledger"], ["retry", "--vm", "vm-1001"]):
            proc = subprocess.run([sys.executable, str(ROOT / "vcfa-import.py"), "-c", cfg] + args,
                                  capture_output=True, cwd=tmp, env=env, timeout=120)
            eq(proc.returncode, 0, "{}: {}".format(args, proc.stderr[-300:]))


CHAOS_KUBECTL = r'''
import os, random, subprocess, sys
FAKE = {fake!r}
rates = {{"get": float(os.environ.get("CHAOS_GET", "0")),
         "api-resources": float(os.environ.get("CHAOS_GET", "0")),
         "apply": float(os.environ.get("CHAOS_APPLY", "0")),
         "patch": float(os.environ.get("CHAOS_APPLY", "0"))}}
args = sys.argv[1:]
verb = next((a for a in args if not a.startswith("-") and a not in ("fake-supervisor",)), "")
i = 0
while i < len(args) and args[i].startswith("--"):
    i += 2 if "=" not in args[i] else 1
verb = args[i] if i < len(args) else ""
if random.random() < rates.get(verb, 0):
    with open(os.environ["CHAOS_LOG"], "a") as fh:
        fh.write(verb + "\n")
    sys.stderr.write("Unable to connect to the server: dial tcp 10.0.0.1:6443: i/o timeout\n")
    sys.exit(1)
sys.exit(subprocess.call([sys.executable, FAKE] + args))
'''


@test("stress/chaos: the API server flaps (25% of reads, 8% of writes time out) -- no false verdicts")
def t_e2e_web_chaos():
    with tempfile.TemporaryDirectory() as tmp:
        chaos = Path(tmp, "chaos_kubectl.py")
        chaos.write_text(CHAOS_KUBECTL.format(fake=str(ROOT / "tools" / "fake_kubectl.py")),
                         encoding="utf-8")
        c, env, _ = _queue_console(
            tmp, "happy", count=16, settle="4", batch_size=4, max_parallel_batches=2,
            kubectl="{} {}".format(Path(sys.executable).as_posix(), chaos.as_posix()))
        os.environ.update(CHAOS_GET="0.25", CHAOS_APPLY="0.08", CHAOS_LOG=str(Path(tmp, "chaos.log")))
        try:
            import random as _r
            _r.seed(7)
            pre = _run_job(c, "execute", {"stage": "precheck", "confirm": True})
            imp = _run_job(c, "execute", {"stage": "import", "confirm": True})
            injected = Path(tmp, "chaos.log").read_text().splitlines()
            assert len(injected) >= 8, "chaos actually happened: {}".format(len(injected))
            counts = c.ok("GET", "/api/overview")["counts"]
            eq(counts, {"committed": 16},
               "every VM committed despite {} injected faults (precheck {}, import {})".format(
                   len(injected), pre["status"], imp["status"]))
            eq(_cluster(tmp)["applies"], 8, "retries never double-applied a batch")
            log = "\n".join(l[2] for l in pre["log"] + imp["log"])
            assert "transient kubectl error" in log or "poll error" in log, "faults were retried"
            # Preflight on a flapping API must not invent missing namespaces.
            os.environ.update(CHAOS_GET="0.3", CHAOS_APPLY="0")
            for _ in range(3):
                pf = c.wait(c.ok("POST", "/api/run/preflight", {})["job"])
                problems = "\n".join((pf.get("result") or {}).get("problems") or [pf.get("error") or ""])
                assert "namespace(s) not found" not in problems, problems
        finally:
            c.close()
            env.__exit__()


@test("stress/scale: 1800 VMs discovered, staged and imported through the console")
def t_e2e_web_scale():
    import time as _t
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter
    server, port = fake_vcenter.serve(count=1800)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = _e2e_env(tmp, "happy", settle="1")
            with _EnvPatch(FAKE_KUBECTL_STATE=env["FAKE_KUBECTL_STATE"], FAKE_KUBECTL_SCENARIO="happy",
                           FAKE_KUBECTL_SETTLE="1", VCFA_VC_SERVER="http://127.0.0.1:{}".format(port),
                           VCFA_VC_USER=fake_vcenter.USER, VCFA_VC_PASSWORD=fake_vcenter.PASSWORD):
                namespaces = ["ns-{:02d}".format(i) for i in range(6)]
                _seed_cluster(tmp, namespaces, ["{}/sub".format(n) for n in namespaces])
                c = _Console(tmp, Config.load(_write_config(
                    tmp, batch_size=25, max_parallel_batches=8, max_parallel_batches_per_namespace=2)))
                timings = {}

                def timed(name, fn):
                    t0 = _t.time()
                    out = fn()
                    timings[name] = _t.time() - t0
                    return out
                try:
                    _run_job(c, "discover", {"concurrency": 24}, "succeeded")
                    vms = timed("GET discovered", lambda: c.ok("GET", "/api/discovered")["vms"])
                    eq(len(vms), 1800)
                    timed("select 1800", lambda: c.ok("POST", "/api/select", {
                        "morefs": [v["moref"] for v in vms], "selected": True}))
                    folders = sorted({(v["folder"] or "").split("/")[0] for v in vms})
                    body = {"folder_rows": [{"folder": f or "/", "namespace": namespaces[i % 6],
                                             "wave": str(i % 3 + 1)} for i, f in enumerate(folders)],
                            "network_rows": [{"portgroup": "*", "subnet": "sub"}]}
                    p = timed("stage preview", lambda: c.ok("POST", "/api/stage/preview", body))
                    eq(len(p["records"]), 1800, p["problems"][:3])
                    timed("stage", lambda: c.ok("POST", "/api/stage", body))
                    timed("GET vms", lambda: c.ok("GET", "/api/vms"))
                    timed("GET overview", lambda: c.ok("GET", "/api/overview"))
                    timed("execute preview", lambda: c.ok("POST", "/api/execute/preview",
                                                          {"stage": "precheck"}))
                    moving = [v["moref"] for v in vms[:600]]
                    timed("move 600 VMs", lambda: c.ok("POST", "/api/vms/wave",
                                                       {"morefs": moving, "wave": 4}))
                    t0 = _t.time()
                    _run_job(c, "execute", {"stage": "precheck", "confirm": True}, "succeeded")
                    _run_job(c, "execute", {"stage": "import", "confirm": True}, "succeeded")
                    timings["precheck+import 1800"] = _t.time() - t0
                    eq(c.ok("GET", "/api/overview")["counts"], {"committed": 1800})
                    timed("GET triage", lambda: c.ok("GET", "/api/triage"))
                    timed("GET batches", lambda: c.ok("GET", "/api/batches"))
                    timed("export tracker", lambda: c.call("GET", "/api/export/tracker.csv?t=t0ken", raw=True))
                    for name, secs in timings.items():
                        print("       {:<24} {:6.2f}s".format(name, secs))
                    slow = {k: v for k, v in timings.items() if "1800" not in k and v > 5}
                    assert not slow, "interactive calls must stay under 5s at 1800 VMs: {}".format(slow)
                finally:
                    c.close()
    finally:
        server.shutdown()


# -------------------------------------------------------------------- main
# ------------------------------------------------------------------ campaign features
class _Capture:
    """A local HTTP endpoint that records every JSON POST (webhook / Teams / Slack)."""

    def __init__(self, status=200):
        import http.server
        import threading
        got = self.got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                got.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:{}".format(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait_for(self, pred, timeout=15):
        import time
        end = time.time() + timeout
        while time.time() < end:
            if any(pred(p, b) for p, b in list(self.got)):
                return True
            time.sleep(0.1)
        return False

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _FakeSmtp:
    """Just enough SMTP to accept one message (no TLS, no auth)."""

    def __init__(self):
        import socketserver
        import threading
        got = self.got = []

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                def w(t):
                    self.wfile.write((t + "\r\n").encode())
                w("220 fake")
                data = None
                while True:
                    line = self.rfile.readline()
                    if not line:
                        return
                    cmd = line.decode("utf-8", "replace").rstrip("\r\n")
                    if data is not None:
                        if cmd == ".":
                            got.append("\n".join(data))
                            data = None
                            w("250 queued")
                        else:
                            data.append(cmd)
                        continue
                    u = cmd.upper()
                    if u.startswith("EHLO"):
                        w("250-fake")
                        w("250 SIZE 1000000")
                    elif u.startswith(("HELO", "MAIL", "RCPT", "RSET", "NOOP")):
                        w("250 ok")
                    elif u == "DATA":
                        data = []
                        w("354 go ahead")
                    elif u == "QUIT":
                        w("221 bye")
                        return
                    else:
                        w("502 not here")

        socketserver.ThreadingTCPServer.daemon_threads = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@test("readiness: vCenter facts grade VMs ready / worth a look / likely to fail")
def t_readiness_rules():
    from vcfaimport import readiness
    good = dict(ip="10.0.0.5", facts_json=json.dumps(
        {"hw_version": "VMX_19", "disk_backings": ["VMDK_FILE"], "nic_states": ["CONNECTED"]}))
    eq(readiness.grade(readiness.assess(_disc_row(**good))), "ready")
    codes = lambda **kw: {f["code"] for f in readiness.assess(_disc_row(**dict(good, **kw)))}  # noqa: E731
    eq(codes(tools_status="toolsNotRunning"), {"tools_not_running"})
    eq(readiness.grade(readiness.assess(_disc_row(**dict(good, tools_status="NOT_RUNNING")))), "block")
    assert "non_vmdk_disk" in codes(facts_json=json.dumps({"disk_backings": ["VMDK_FILE", "RDM"]}))
    assert "iso_connected" in codes(facts_json=json.dumps({"iso_connected": True}))
    assert "nic_disconnected" in codes(facts_json=json.dumps({"nic_states": ["CONNECTED", "NOT_CONNECTED"]}))
    assert "old_hardware" in codes(facts_json=json.dumps({"hw_version": "VMX_08"}))
    assert "no_nics" in codes(nics_json="[]")
    assert "no_ip" in codes(ip="")
    # Powered off: a warning, not "Tools not running" (Tools cannot run while it is off).
    off = codes(power_state="POWERED_OFF", tools_status="NOT_RUNNING")
    assert "powered_off" in off and "tools_not_running" not in off, off
    eq(readiness.grade([{"level": "info"}]), "ready", "info alone does not lower the grade")
    # Rows from before this release (no facts, no ip column) never crash.
    assert isinstance(readiness.assess(_Row(moref="vm-9", power_state="POWERED_ON")), list)
    summ = readiness.summary([_disc_row(**good), _disc_row(moref="vm-2", **dict(good, tools_status="NOT_RUNNING"))])
    eq(summ["grades"], {"ready": 1, "warn": 0, "block": 1})
    eq(summ["findings"][0]["morefs"], ["vm-2"])


@test("estimates: rounds honour parallel limits; measured batch time replaces the default")
def t_estimate():
    from vcfaimport import estimate as est
    eq(est.simulate({"a": 4, "b": 1}, parallel=3, per_ns=2), 2)
    eq(est.simulate({"a": 4}, parallel=8, per_ns=1), 4, "one namespace, one at a time")
    eq(est.simulate({}, parallel=3, per_ns=2), 0)
    eq(est.human(45), "45s")
    eq(est.human(360), "6m")
    eq(est.human(3700), "1h 01m")
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        cfg = Config()
        cfg.batch_size, cfg.max_parallel_batches, cfg.max_parallel_batches_per_namespace = 4, 3, 2
        cfg.settle_seconds = 0
        e = est.estimate(store, cfg, "precheck", {"a": 16, "b": 4})
        eq((e["batches"], e["rounds"], e["basis"]), (5, 2, "default"))
        eq(e["seconds"], 2 * est.DEFAULT_BATCH_SECONDS["precheck"])
        eq(est.estimate(store, cfg, "import", {})["seconds"], 0)
        # Two finished import batches of 100s and 300s here -> the median, 200s.
        for i, secs in enumerate((100, 300)):
            store.conn.execute(
                "INSERT INTO batches(name, namespace, stage, wave, state, created_at, applied_at, finished_at,"
                " run_id) VALUES(?,?,?,?,?,?,?,?,?)",
                ("b{}".format(i), "a", "import", 1, "succeeded", "2026-09-20T09:59:00Z", "2026-09-20T10:00:00Z",
                 "2026-09-20T10:{:02d}:{:02d}Z".format(secs // 60, secs % 60), "r1"))
        store.conn.commit()
        e = est.estimate(store, cfg, "import", {"a": 4})
        eq((e["basis"], e["samples"], e["seconds"]), ("measured", 2, 200))
        store.close()


@test("verification: TCP and vCenter checks, IP preserved or changed, verdicts")
def t_verify_checks():
    import socket
    from vcfaimport import verify
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    open_port = listener.getsockname()[1]
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    try:
        eq(verify.tcp("127.0.0.1", open_port, 2)[0], "ok")
        eq(verify.tcp("127.0.0.1", closed_port, 2)[0], "fail")

        class Client:
            def __init__(self, ip, power="POWERED_ON", tools="RUNNING"):
                self.ip, self.power, self.tools = ip, power, tools

            def vm_detail(self, moref):
                return {"power_state": self.power}

            def vm_tools(self, moref):
                return self.tools

            def guest_ip(self, moref):
                return self.ip

        cfg = Config()
        cfg.verify_ping, cfg.verify_ports, cfg.verify_timeout_seconds = False, [open_port], 2
        row = _Row(moref="vm-1", src_ip="127.0.0.1")
        verdict, checks = verify.verify_one(row, cfg, Client("127.0.0.1"))
        eq(verdict, "ok", checks)
        eq([c["check"] for c in checks], ["powered on", "VMware Tools", "IP preserved", "tcp {}".format(open_port)])
        verdict, checks = verify.verify_one(_Row(moref="vm-1", src_ip="10.9.9.9"), cfg, Client("127.0.0.1"))
        eq(verdict, "fail", "the guest came up on a different IP")
        assert "was 10.9.9.9" in checks[2]["detail"], checks
        eq(verify.verify_one(row, cfg, Client("127.0.0.1", power="POWERED_OFF"))[0], "fail")
        cfg.verify_ports = []
        eq(verify.verify_one(row, cfg, None)[0], "warn", "nothing could be checked")
        cfg.verify_ports = [closed_port]
        eq(verify.verify_one(row, cfg, None)[0], "fail")
        # ping: success or "no ping command here" -- never an exception
        assert verify.ping("127.0.0.1", 2)[0] in ("ok", "skip")
        # one surprise does not stop the rest
        class Boom(Client):
            def vm_detail(self, moref):
                if moref == "vm-2":
                    raise RuntimeError("kaboom")
                return super().vm_detail(moref)
        out = verify.verify_many([row, _Row(moref="vm-2", src_ip="")], cfg, Boom("127.0.0.1"))
        eq([o[1] for o in out], ["fail", "fail"])
        assert "kaboom" in out[1][2][0]["detail"]
    finally:
        listener.close()


@test("notifications: channel checks, Teams/Slack/webhook payloads, email, event filters")
def t_notify_channels():
    from vcfaimport import notify
    for bad, why in [({"type": "pager", "url": "http://x"}, "type"),
                     ({"type": "teams", "url": "ftp://x"}, "url"),
                     ({"type": "webhook", "url": "http://x", "events": ["nope"]}, "unknown event"),
                     ({"type": "email", "smtp_host": "h", "from": "a@x", "to": "b@x", "password": "s"}, "password_env"),
                     ({"type": "email", "smtp_host": "h", "to": "b@x"}, "from")]:
        try:
            notify.validate([bad])
            raise AssertionError("accepted " + why)
        except ValueError as exc:
            assert why in str(exc), (why, exc)
    cap, smtp = _Capture(), _FakeSmtp()
    try:
        channels = notify.validate([
            {"type": "teams", "name": "ops-teams", "url": cap.url + "/teams"},
            {"type": "slack", "url": cap.url + "/slack"},
            {"type": "webhook", "url": cap.url + "/hook", "events": "job_failed, verify_failed"},
            {"type": "webhook", "url": cap.url + "/quiet", "events": ["job_succeeded"]},
            {"type": "webhook", "url": cap.url + "/off", "enabled": False},
            {"type": "email", "smtp_host": "127.0.0.1", "smtp_port": smtp.port, "starttls": False,
             "from": "vcfa@lab", "to": "ops@lab, oncall@lab"},
        ])
        eq(channels[5]["to"], ["ops@lab", "oncall@lab"])
        log = []
        notify.send(channels, "job_failed", "Import wave 2 failed", "3 VMs failed", {"wave": 2, "empty": ""},
                    {"context": "Supervisor"}, record=lambda lvl, msg: log.append((lvl, msg)), wait=True)
        paths = sorted(p for p, _ in cap.got)
        eq(paths, ["/hook", "/slack", "/teams"])
        body = dict(cap.got)
        eq(body["/teams"]["@type"], "MessageCard")
        eq(body["/teams"]["themeColor"], notify.COLOR["bad"])
        eq(body["/teams"]["sections"][0]["facts"], [{"name": "wave", "value": "2"}])
        assert body["/slack"]["text"].startswith("*Import wave 2 failed*"), body["/slack"]
        eq((body["/hook"]["event"], body["/hook"]["context"], body["/hook"]["fields"]["wave"]),
           ("job_failed", "Supervisor", 2))
        eq(len(smtp.got), 1)
        assert "Subject: [vcfa-import] Import wave 2 failed" in smtp.got[0], smtp.got[0]
        eq(sum(1 for lvl, _ in log if lvl == "info"), 4, log)
        # a test message reaches every enabled channel, whatever its events filter
        cap.got.clear()
        notify.send(channels[:5], "test", "hello", wait=True)
        eq(sorted(p for p, _ in cap.got), ["/hook", "/quiet", "/slack", "/teams"])
        # delivery retries, then reports -- it never raises out of send()
        dead = notify.validate([{"type": "webhook", "url": "http://127.0.0.1:9/x"}])[0]
        try:
            notify.deliver(dead, "test", "t", "", {}, {}, attempts=2, pause=0)
            raise AssertionError("delivered to a closed port")
        except OSError:
            pass
    finally:
        cap.close()
        smtp.close()


@test("users and approvals: tokens, roles, the two-person rule, one use per approval")
def t_access_rules():
    from vcfaimport import access
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        tok = access.create_user(store, "alice", "operator", "owner")
        eq(access.authenticate(store, tok), {"name": "alice", "role": "operator"})
        eq(access.authenticate(store, tok + "x"), None)
        eq(access.authenticate(store, ""), None)
        assert tok not in json.dumps([dict(r) for r in store.conn.execute("SELECT * FROM users")]), \
            "only a hash of the token is stored"
        for name, role in (("owner", "admin"), ("bad name", "viewer"), ("carol", "root")):
            try:
                access.create_user(store, name, role, "owner")
                raise AssertionError("accepted {}/{}".format(name, role))
            except access.AccessError:
                pass
        tok2 = access.create_user(store, "alice", "viewer", "owner")
        eq(access.authenticate(store, tok), None, "a re-issued link retires the old one")
        eq(access.authenticate(store, tok2)["role"], "viewer")
        access.set_disabled(store, "alice", True, "owner")
        eq(access.authenticate(store, tok2), None)
        access.set_disabled(store, "alice", False, "owner")
        assert access.authenticate(store, tok2)
        assert access.allows("admin", "operator") and access.allows("viewer", "viewer")
        assert not access.allows("viewer", "operator") and not access.allows(None, "viewer")

        cfg = Config()
        cfg.require_approval = ["import", "rollback"]
        assert access.needs_approval(cfg, "import", {"waves": [1]})
        assert not access.needs_approval(cfg, "import", {"dry_run": True}), "a dry run changes nothing"
        assert not access.needs_approval(cfg, "precheck", {})
        a = access.request(store, "import", {"waves": [2], "confirm": True}, "alice")
        eq((a["state"], a["summary"], "confirm" in a["body"]), ("pending", "import wave 2", False))
        try:
            access.decide(store, a["id"], True, "alice")
            raise AssertionError("approved own request")
        except access.AccessError:
            pass
        a = access.decide(store, a["id"], True, "bob", "window agreed")
        eq((a["state"], a["decided_by"], a["note"]), ("approved", "bob", "window agreed"))
        for stage in ("rollback", "import"):
            if stage == "rollback":
                try:
                    access.consume(store, a["id"], stage)
                    raise AssertionError("an import approval used for a rollback")
                except access.AccessError:
                    pass
        eq(access.consume(store, a["id"], "import", "job-1")["state"], "executed")
        try:
            access.consume(store, a["id"], "import")
            raise AssertionError("an approval used twice")
        except access.AccessError:
            pass
        rows = [dict(r) for r in store.conn.execute("SELECT actor FROM events WHERE message LIKE 'approval%'")]
        eq({r["actor"] for r in rows}, {"alice", "bob"})
        store.close()


@test("change windows: times, validation, approval gating, due and missed windows")
def t_schedule_rules():
    from datetime import timedelta
    from vcfaimport import access
    from vcfaimport import schedule as sch
    eq(sch.iso(sch.parse_when("2026-09-26T22:00:00Z")), "2026-09-26T22:00:00Z")
    eq(sch.iso(sch.parse_when("2026-09-26T22:00:00+02:00")), "2026-09-26T20:00:00Z")
    assert sch.parse_when("2026-09-26 22:00").tzinfo is not None, "local time is made explicit"
    for bad in ("", "tomorrow", "26/09/2026"):
        try:
            sch.parse_when(bad)
            raise AssertionError("parsed " + bad)
        except sch.ScheduleError:
            pass
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        now = sch.now_utc()
        for stage, start, end in (("commit", now, now + timedelta(hours=1)),
                                  ("import", now + timedelta(hours=1), now),
                                  ("import", now - timedelta(hours=2), now - timedelta(hours=1)),
                                  ("import", now, now + timedelta(minutes=3))):
            try:
                sch.create(store, stage, {}, start, end, "alice")
                raise AssertionError("accepted {} {} {}".format(stage, start, end))
            except sch.ScheduleError:
                pass
        s1 = sch.create(store, "precheck", {"waves": [1], "confirm": True}, now - timedelta(minutes=1),
                        now + timedelta(hours=1), "alice")
        eq((s1["state"], s1["body"], s1["created_by"]), ("scheduled", {"waves": [1], "stage": "precheck"}, "alice"))
        s2 = sch.create(store, "import", {"waves": [1]}, now + timedelta(hours=2), now + timedelta(hours=3),
                        "alice", needs_approval=True)
        eq(s2["state"], "awaiting_approval")
        eq([s["id"] for s in sch.due(store)], [s1["id"]], "only an open, approved window is due")
        eq(sch.next_open(store)["id"], s1["id"])
        a = access.request(store, "import", s2["body"], "alice", schedule_id=s2["id"])
        sch.on_approval(store, access.decide(store, a["id"], True, "bob"))
        eq(sch.get(store, s2["id"])["state"], "scheduled")
        eq(len(sch.due(store, at=now + timedelta(hours=2, minutes=5))), 1)
        # Nobody ran the scheduler: both windows are missed once they close.
        missed = sch.sweep_missed(store, at=now + timedelta(hours=4))
        eq(sorted(m["id"] for m in missed), sorted([s1["id"], s2["id"]]))
        assert all("closed" in m["message"] for m in missed)
        try:
            sch.cancel(store, s1["id"], "alice")
            raise AssertionError("cancelled a missed window")
        except sch.ScheduleError:
            pass
        s3 = sch.create(store, "import", {}, now + timedelta(hours=5), now + timedelta(hours=6), "alice", True)
        a = access.request(store, "import", {}, "alice", schedule_id=s3["id"])
        sch.on_approval(store, access.decide(store, a["id"], False, "bob"))
        eq(sch.get(store, s3["id"])["state"], "cancelled", "a rejected window never runs")
        store.close()


@test("settings: workspace overrides layer over the file, with guardrails and reset")
def t_settings_overlay():
    from vcfaimport import settings as settings_mod
    from vcfaimport.config import ConfigError
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.actor = "alice"
        file_cfg = Config()
        file_cfg.batch_size = 10
        settings_mod.update(store, {"batch_size": "25", "verify_ports": "22, 443", "require_approval": "commit,import",
                                    "app_together": "yes"}, "alice")
        cfg = settings_mod.apply(Config(), store)
        eq((cfg.batch_size, cfg.verify_ports, cfg.require_approval, cfg.app_together),
           (25, [22, 443], ["import", "commit"], True))
        settings_mod.apply(cfg, store)   # applying twice is a no-op
        for bad in ({"batch_size": 0}, {"batch_size": 5000}, {"failure_rate_abort": "lots"},
                    {"verify_ports": "70000"}, {"require_approval": "precheck"}, {"kubectl": "rm"},
                    {"commit_action": "Sometimes"}, {"app_together": "maybe"},
                    {"notify": [{"type": "email", "smtp_host": "h", "from": "a@x", "to": "b@x", "password": "p"}]}):
            try:
                settings_mod.update(store, bad, "alice")
                raise AssertionError("accepted {}".format(bad))
            except (ConfigError, ValueError):
                pass
        eq(settings_mod.overlay(store)["batch_size"], 25, "a refused change changes nothing")
        rows = {r["key"]: r for r in settings_mod.describe(file_cfg, store)}
        eq((rows["batch_size"]["value"], rows["batch_size"]["file_value"], rows["batch_size"]["overridden"]),
           (25, 10, True))
        eq(rows["poll_interval_seconds"]["overridden"], False)
        settings_mod.update(store, {"batch_size": None}, "alice")
        eq(settings_mod.apply(copy.deepcopy(file_cfg), store).batch_size, 10, "reset falls back to the file")
        logged = [r["message"] for r in store.conn.execute("SELECT message FROM events WHERE actor='alice'")]
        assert any("batch_size = 25" in m for m in logged) and any("reset" in m for m in logged), logged
        store.close()


@test("tag map: exact tags beat globs; tag placement sits between the row and the folder map")
def t_tag_map_stage():
    from vcfaimport.discovery import match_tag_map, stage, tag_map_from_rows
    m = tag_map_from_rows([{"tag": "Application:*", "namespace": "ns-apps", "wave": "3"},
                           {"tag": "Application:Payroll", "namespace": "ns-pay", "wave": "1"}])
    eq(match_tag_map(["Tier:Web", "application:payroll"], m)[0], "Application:Payroll")
    eq(match_tag_map(["Application:CRM"], m)[1].namespace, "ns-apps")
    eq(match_tag_map(["Tier:Web"], m), None)
    try:
        tag_map_from_rows([{"tag": "A:B", "namespace": "x", "wave": "soon"}])
        raise AssertionError("bad wave accepted")
    except Exception as exc:  # noqa: BLE001
        assert "wave" in str(exc)
    from vcfaimport.discovery import folder_map_from_rows, network_map_from_rows
    nets = network_map_from_rows([{"portgroup": "*", "subnet": "sub-a"}])
    folders = folder_map_from_rows([{"folder": "Production", "namespace": "ns-folder", "wave": "5"}])
    rows = [_disc_row(moref="vm-1", tags_json='["Application:Payroll"]'),
            _disc_row(moref="vm-2", tags_json='["Application:CRM"]', namespace="ns-picked", wave=7),
            _disc_row(moref="vm-3", tags_json='[]')]
    res = stage(rows, Config(), mapping=nets, folder_mapping=folders, tag_mapping=m)
    got = {r.moref: (r.namespace, r.wave) for r in res.records}
    eq(got, {"vm-1": ("ns-pay", 1), "vm-2": ("ns-picked", 7), "vm-3": ("ns-folder", 5)})


@test("apps: an application is aligned to one wave, and only imported whole")
def t_apps_together():
    from vcfaimport import service
    from vcfaimport.discovery import network_map_from_rows, stage
    from vcfaimport.engine import Engine
    from vcfaimport import state as st
    cfg = Config()
    cfg.app_category, cfg.app_together = "Application", True
    nets = network_map_from_rows([{"portgroup": "*", "subnet": "sub-a"}])
    rows = [_disc_row(moref="vm-1", tags_json='["Application:Payroll"]', namespace="ns-a", wave=2),
            _disc_row(moref="vm-2", tags_json='["application:Payroll", "Tier:DB"]', namespace="ns-a", wave=1),
            _disc_row(moref="vm-3", tags_json='["Application:CRM"]', namespace="ns-a", wave=3)]
    res = stage(rows, cfg, mapping=nets)
    eq({r.moref: (r.wave, r.app) for r in res.records},
       {"vm-1": (1, "Payroll"), "vm-2": (1, "Payroll"), "vm-3": (3, "CRM")})
    assert res.app_moves and "Payroll" in res.app_moves[0], res.app_moves
    with tempfile.TemporaryDirectory() as tmp:
        cfg.kubectl = "vcfa-test-no-such-kubectl"
        cfg.workdir = tmp
        store = _store(tmp)
        store.sync_inventory(res.records)
        store.set_vm_state("vm-1", st.S_PRECHECK_PASSED, message="test")
        store.set_vm_state("vm-3", st.S_PRECHECK_PASSED, message="test")
        engine = Engine(cfg, store, None, lambda m: None)
        eq([r["moref"] for r in engine.eligible("import", 1)], [], "Payroll waits for vm-2's precheck")
        assert "Payroll" in engine.holdbacks[0] and "pending" in engine.holdbacks[0], engine.holdbacks
        notes = service.holdbacks(engine, "import")
        eq(len(notes), 1)
        assert notes[0].startswith("wave 1:"), notes
        eq([r["moref"] for r in engine.eligible("import", 3)], ["vm-3"])
        store.set_vm_state("vm-2", st.S_PRECHECK_PASSED, message="test")
        eq(sorted(r["moref"] for r in engine.eligible("import", 1)), ["vm-1", "vm-2"])
        cfg.app_together = False
        store.set_vm_state("vm-2", st.S_PRECHECK_FAILED, message="test")
        eq([r["moref"] for r in engine.eligible("import", 1)], ["vm-1"], "off: VMs move on their own")
        # Moving one member moves the app when apps are kept together.
        cfg.app_together = True
        moved = service.move_wave(store, cfg, ["vm-1"], 4)
        eq((moved["moved"], moved["pulled_with_app"]), (2, 1))
        eq(service.app_splits(store), [])
        moved = service.move_wave(store, cfg, ["vm-1"], 5, with_app=False)
        eq((moved["moved"], moved["pulled_with_app"]), (1, 0))
        eq([s["app"] for s in service.app_splits(store)], ["Payroll"])
        store.close()


@test("readiness exclusion keeps blocked VMs out of precheck, and says so")
def t_readiness_exclusion():
    from vcfaimport.engine import Engine
    from vcfaimport import service
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        _seed_store_with_vms(store, 4, waves=(1,))
        store.conn.execute("UPDATE discovered SET tools_status='NOT_RUNNING' WHERE moref='vm-1001'")
        store.conn.commit()
        cfg = Config()
        cfg.kubectl, cfg.workdir = "vcfa-test-no-such-kubectl", tmp
        engine = Engine(cfg, store, None, lambda m: None)
        eq(len(engine.eligible("precheck", 1)), 4, "off by default")
        cfg.readiness_exclude_blocked = True
        eligible = [r["moref"] for r in engine.eligible("precheck", 1)]
        assert "vm-1001" not in eligible and len(eligible) == 3, eligible
        eq(service.holdbacks(engine, "precheck"), ["wave 1: 1 VM(s) held back: readiness marks them blocked"])
        store.close()


@test("web console: named users and roles; settings, apps, readiness and notify-test endpoints")
def t_web_roles_settings():
    cap = _Capture()
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            eq(c.ok("GET", "/api/me")["user"], {"name": "owner", "role": "admin"})
            toks = {}
            for name, role in (("vic", "viewer"), ("olga", "operator"), ("otto", "operator"), ("ada", "admin")):
                toks[name] = c.ok("POST", "/api/users", {"name": name, "role": role})["token"]
            eq(c.call("POST", "/api/users", {"name": "owner", "role": "admin"})[0], 400)
            eq(c.call("GET", "/api/me", token=toks["olga"])[1]["user"], {"name": "olga", "role": "operator"})
            eq(c.call("GET", "/api/overview", token=toks["vic"])[0], 200, "a viewer reads")
            status, body, _ = c.call("POST", "/api/select", {"morefs": ["vm-1"], "selected": True}, token=toks["vic"])
            eq(status, 403, "a viewer changes nothing")
            assert "viewer" in body["error"], body
            eq(c.call("POST", "/api/run/execute", {"stage": "precheck", "confirm": True}, token=toks["vic"])[0], 403)
            eq(c.call("PUT", "/api/settings", {"changes": {"batch_size": 5}}, token=toks["olga"])[0], 403,
               "an operator cannot change guardrails")
            eq(c.call("GET", "/api/users", token=toks["otto"])[0], 403)
            eq(c.call("GET", "/api/settings", token=toks["olga"])[0], 200)
            eq(c.call("PUT", "/api/settings", {"changes": {"batch_size": 7}}, token=toks["ada"])[0], 200)
            # Settings: guardrails refused with 400, accepted ones take effect at once
            eq(c.call("PUT", "/api/settings", {"changes": {"batch_size": 0}})[0], 400)
            eq(c.call("PUT", "/api/settings", {"changes": "batch_size=3"})[0], 400)
            rows = {r["key"]: r for r in c.ok("GET", "/api/settings")["settings"]}
            eq((rows["batch_size"]["value"], rows["batch_size"]["overridden"]), (7, True))
            eq(c.ok("GET", "/api/info")["settings"]["batch_size"], 7, "the console uses it right away")
            # Notification channels: a secret is refused; a test message reaches the webhook
            bad = [{"type": "email", "smtp_host": "h", "from": "a@x", "to": "b@x", "password": "hunter2"}]
            eq(c.call("PUT", "/api/settings", {"changes": {"notify": bad}})[0], 400)
            c.ok("PUT", "/api/settings", {"changes": {"notify": [{"type": "webhook", "url": cap.url + "/h"}]}})
            res = c.ok("POST", "/api/settings/notify-test", {})["results"]
            eq([r["level"] for r in res], ["info"], res)
            eq(cap.got[-1][1]["event"], "test")
            eq(cap.got[-1][1]["fields"]["sent by"], "owner")
            # Disabling a user takes effect on their next request
            c.ok("POST", "/api/users/olga/disable", {})
            eq(c.call("GET", "/api/me", token=toks["olga"])[0], 401)
            eq(c.call("POST", "/api/users/nobody/disable", {})[0], 404)
            users = {u["name"]: u for u in c.ok("GET", "/api/users")["users"]}
            eq(users["olga"]["disabled"], 1)
            assert users["vic"]["last_seen"], "use is recorded"
            eq(c.ok("GET", "/api/readiness")["grades"], {"ready": 0, "warn": 0, "block": 0})
            eq(c.ok("GET", "/api/apps")["apps"], [])
            # The audit trail names who did what
            actors = {e.get("actor") for e in c.ok("GET", "/api/events")["events"]}
            assert {"owner", "ada"} <= actors, actors
        finally:
            c.close()
            cap.close()


@test("e2e web: tags and apps, change window, two-person import, verification, notifications")
def t_e2e_web_governance():
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter
    from datetime import timedelta
    from vcfaimport import schedule as sch
    server, port = fake_vcenter.serve(count=24)
    cap = _Capture()
    saved = dict(os.environ)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.update(_e2e_env(tmp, "happy", settle="1"))
            os.environ.update(VCFA_VC_SERVER="http://127.0.0.1:{}".format(port),
                              VCFA_VC_USER=fake_vcenter.USER, VCFA_VC_PASSWORD=fake_vcenter.PASSWORD)
            _seed_cluster(tmp, ["ns-a", "ns-b"], ["ns-a/sub-a", "ns-b/sub-a"])
            cfg = Config.load(_write_config(tmp, failure_rate_abort=0.9))
            c = _Console(tmp, cfg)
            try:
                eq(c.wait(c.ok("POST", "/api/run/discover", {})["job"])["status"], "succeeded")
                vms = c.ok("GET", "/api/discovered")["vms"]
                assert all(v["tags"] for v in vms), "every fake VM carries tags"
                assert {v["readiness"] for v in vms} >= {"ready", "block"}, {v["readiness"] for v in vms}
                eq({v["app"] for v in vms}, {""}, "no app category yet")
                c.ok("PUT", "/api/settings", {"changes": {
                    "app_category": "Application", "app_together": True, "verify_ping": False,
                    "notify": [{"type": "webhook", "url": cap.url + "/h"}]}})
                vms = c.ok("GET", "/api/discovered")["vms"]
                assert {v["app"] for v in vms} == set(fake_vcenter.APPS), {v["app"] for v in vms}
                ready = [v["moref"] for v in vms if v["readiness"] != "block"]
                c.ok("POST", "/api/select", {"morefs": ready, "selected": True})

                # Tag map: Payroll to ns-b, wave 2; everything else by the default.
                tag_rows = [{"tag": "Application:Payroll", "namespace": "ns-b", "wave": "2"}]
                body = {"network_rows": [{"portgroup": "*", "subnet": "sub-a"}], "tag_rows": tag_rows,
                        "default_namespace": "ns-a", "default_wave": 1}
                preview = c.ok("POST", "/api/stage/preview", body)
                eq(preview["problems"], [])
                for r in preview["records"]:
                    eq(r["namespace"], "ns-b" if r["app"] == "Payroll" else "ns-a", r["vm_name"])
                cov = {t["tag"]: t for t in preview["coverage"]["tags"]}
                eq(cov["Application:Payroll"]["namespace"], "ns-b")
                c.ok("POST", "/api/stage", dict(body, save_maps=True))
                eq(c.ok("GET", "/api/maps")["tag"]["rows"][0]["tag"], "Application:Payroll")
                apps = {a["app"]: a for a in c.ok("GET", "/api/apps")["apps"]}
                eq(set(map(str, apps["Payroll"]["waves"])), {"2"})
                eq(apps["Payroll"]["split"], False)
                ov = c.ok("GET", "/api/overview")
                assert ov["apps"] and ov["estimates"], ov.keys()

                # A change window: precheck everything, run by the scheduler.
                now = sch.now_utc()
                window = {"stage": "precheck", "start_at": sch.iso(now - timedelta(minutes=1)),
                          "end_at": sch.iso(now + timedelta(minutes=30))}
                fit = c.ok("POST", "/api/schedules/fit", window)
                assert fit["fits"] and fit["estimate"]["batches"] > 0, fit
                sched = c.ok("POST", "/api/schedules", window)["schedule"]
                eq(sched["state"], "scheduled")
                job = c.app.scheduler_tick()
                assert job is not None, "the open window started"
                eq(c.app.scheduler_tick(), None, "never twice")
                snap = c.wait(job.summary())
                eq(snap["status"], "succeeded", snap.get("error"))
                assert snap["user"].startswith("scheduler"), snap["user"]
                sched = c.ok("GET", "/api/schedules")["schedules"][0]
                eq(sched["state"], "done", sched)
                assert sched["job_id"] == job.id

                # Two-person rule for the import.
                c.ok("PUT", "/api/settings", {"changes": {"require_approval": ["import"],
                                                          "verify_after_import": True}})
                olga = c.ok("POST", "/api/users", {"name": "olga", "role": "operator"})["token"]
                otto = c.ok("POST", "/api/users", {"name": "otto", "role": "operator"})["token"]
                pre = c.ok("POST", "/api/execute/preview", {"stage": "import"})
                assert pre["needs_approval"] and pre["seconds"] > 0, pre
                st_, dry, _ = c.call("POST", "/api/run/execute",
                                     {"stage": "import", "dry_run": True, "confirm": True}, token=olga)
                assert "job" in dry, "a dry run needs no approval"
                c.wait(dry["job"])
                st_, req, _ = c.call("POST", "/api/run/execute", {"stage": "import", "confirm": True}, token=olga)
                eq(st_, 200)
                assert "approval" in req and "job" not in req, req
                aid = req["approval"]["id"]
                eq(c.call("POST", "/api/approvals/{}/decide".format(aid), {"approve": True}, token=olga)[0], 403)
                st_, dec, _ = c.call("POST", "/api/approvals/{}/decide".format(aid), {"approve": True}, token=otto)
                eq(st_, 200, dec)
                snap = c.wait(dec["job"])
                eq(snap["status"], "succeeded", snap.get("error"))
                assert "olga" in snap["user"] and "otto" in snap["user"], snap["user"]
                eq(c.ok("GET", "/api/approvals")["approvals"][0]["state"], "executed")
                assert any("verification:" in line[2] for line in snap["log"]), "verified after the import"

                committed = [v for v in c.ok("GET", "/api/vms")["vms"] if v["state"] == "committed"]
                assert committed and all(v["verify_state"] in ("ok", "warn", "fail") for v in committed)
                detail = c.ok("GET", "/api/vms/" + committed[0]["moref"])["vm"]
                eq(detail["verify"][0]["check"], "powered on")
                assert detail["verify_history"], detail.keys()

                # Re-verify on demand, as a job.
                snap = c.wait(c.ok("POST", "/api/run/verify", {"wave": 1})["job"])
                eq(snap["status"] in ("succeeded", "warning"), True, snap)
                assert snap["result"]["checked"] > 0

                assert cap.wait_for(lambda p, b: b.get("event") == "approval_requested")
                assert cap.wait_for(lambda p, b: b.get("event") in ("job_succeeded", "job_warning")
                                    and "Import" in b.get("title", "")), [b.get("event") for _, b in cap.got]
                assert cap.wait_for(lambda p, b: b.get("event") == "schedule_finished")
            finally:
                c.close()
    finally:
        os.environ.clear()
        os.environ.update(saved)
        server.shutdown()
        cap.close()


@test("e2e cli: settings, readiness, tag filter, approvals between two OS users, verify")
def t_e2e_cli_governance():
    sys.path.insert(0, str(ROOT / "tools"))
    import fake_vcenter
    server, port = fake_vcenter.serve(count=16)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            env = _e2e_env(tmp, "happy", settle="1")
            env.update(VCFA_VC_SERVER="http://127.0.0.1:{}".format(port),
                       VCFA_VC_USER=fake_vcenter.USER, VCFA_VC_PASSWORD=fake_vcenter.PASSWORD)
            as_user = lambda name: dict(env, LOGNAME=name, USER=name, LNAME=name, USERNAME=name)  # noqa: E731
            alice, bob = as_user("alice"), as_user("bob")
            _seed_cluster(tmp, ["ns-a"], ["ns-a/sub-a"])
            cfg = _write_config(tmp)
            _run_cli(["-c", cfg, "discover"], alice, tmp)
            out = _run_cli(["-c", cfg, "readiness"], alice, tmp, expect=None)
            assert out.returncode in (0, 4) and "readiness of 16 VM(s)" in out.stdout, out.stdout
            _run_cli(["-c", cfg, "settings", "set", "app_category=Application", "require_approval=import",
                      "verify_ping=false"], alice, tmp)
            out = _run_cli(["-c", cfg, "settings", "show"], alice, tmp).stdout
            assert "app_category" in out and "Application" in out, out
            _run_cli(["-c", cfg, "settings", "set", "batch_size=0"], alice, tmp, expect=2)
            _run_cli(["-c", cfg, "select", "--tag", "Application:Payroll"], alice, tmp)
            write_csv(str(Path(tmp, "pg.csv")), [["*", "ns-a", "sub-a", "1", "", "", ""]],
                      ["portgroup", "namespace", "subnet", "wave", "device_key", "subnet_kind", "subnet_api_group"])
            _run_cli(["-c", cfg, "stage", "--map", str(Path(tmp, "pg.csv"))], alice, tmp)
            vms = json.loads(_run_cli(["-c", cfg, "vms", "--json"], alice, tmp).stdout)
            assert 0 < len(vms) < 16, len(vms)
            _run_cli(["-c", cfg, "precheck", "--yes"], alice, tmp)
            out = _run_cli(["-c", cfg, "run", "--yes"], alice, tmp, expect=None)
            assert out.returncode != 0 and "approval" in (out.stdout + out.stderr), out.stdout
            _run_cli(["-c", cfg, "approvals", "request", "--stage", "import"], alice, tmp)
            out = _run_cli(["-c", cfg, "approvals", "approve", "1"], alice, tmp, expect=2)
            assert "person who made it" in (out.stdout + out.stderr)
            _run_cli(["-c", cfg, "approvals", "approve", "1"], bob, tmp)
            _run_cli(["-c", cfg, "run", "--yes", "--approval", "1"], alice, tmp)
            _run_cli(["-c", cfg, "run", "--yes", "--approval", "1"], alice, tmp, expect=None)
            out = _run_cli(["-c", cfg, "verify"], alice, tmp, expect=None)
            assert "verified" in out.stdout, out.stdout
            out = _run_cli(["-c", cfg, "approvals", "list", "--all"], alice, tmp).stdout
            assert "executed" in out and "bob" in out, out
            ledger = Path(tmp, "run", "ledger.jsonl").read_text(encoding="utf-8")
            assert '"actor": "alice"' in ledger or '"actor":"alice"' in ledger, "the ledger names the operator"
    finally:
        server.shutdown()


@test("web console: the bundled guides are served for Help, to every role")
def t_web_docs():
    with tempfile.TemporaryDirectory() as tmp:
        c = _Console(tmp)
        try:
            lab = c.ok("GET", "/api/docs/lab")
            eq((lab["file"], lab["title"]), ("LAB-GUIDE.md", "Lab guide"))
            assert lab["markdown"].startswith("# vcfa-import"), lab["markdown"][:60]
            readme = c.ok("GET", "/api/docs/readme")
            assert "## Campaign controls" in readme["markdown"]
            eq(c.call("GET", "/api/docs/secrets")[0], 404)
            eq(c.call("GET", "/api/docs/..%2Fconfig")[0], 404)
            eq(c.call("GET", "/api/docs/lab", token=False)[0], 401)
            viewer = c.ok("POST", "/api/users", {"name": "vic", "role": "viewer"})["token"]
            eq(c.call("GET", "/api/docs/lab", token=viewer)[0], 200, "a viewer can read the guides")
        finally:
            c.close()


@test("help: every tooltip and tour link names a heading that exists in the guides")
def t_help_doc_anchors():
    import re
    def slug(t):
        return re.sub(r"[^\w\- ]", "", t.strip().lower()).replace(" ", "-")
    ids = {}
    for key, name in (("lab", "LAB-GUIDE.md"), ("readme", "README.md")):
        found, fence = set(), False
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("```"):
                fence = not fence
                continue
            m = None if fence else re.match(r"(#{1,6})\s+(.*?)\s*#*\s*$", line)
            if m:
                found.add(slug(m.group(2)))
        ids[key] = found
    js = (ROOT / "vcfaimport" / "web" / "static" / "help.js").read_text(encoding="utf-8")
    refs = re.findall(r"\b(LAB|RM)\('([^']+)'\)", js)
    assert len(refs) > 20, len(refs)
    broken = ["{}#{}".format(k, h) for k, h in refs if h not in ids["lab" if k == "LAB" else "readme"]]
    eq(broken, [], "renaming a heading in the guides breaks these links")


@test("linux jump box: serve --open never launches a text browser without a desktop session")
def t_serve_open_headless():
    from vcfaimport.web import server
    saved_platform, saved_env = sys.platform, dict(os.environ)
    try:
        for key in ("DISPLAY", "WAYLAND_DISPLAY"):
            os.environ.pop(key, None)
        sys.platform = "linux"
        eq(server.can_open_browser(), False, "headless Ubuntu")
        os.environ["DISPLAY"] = ":0"
        eq(server.can_open_browser(), True, "Ubuntu desktop")
        os.environ.pop("DISPLAY")
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        eq(server.can_open_browser(), True, "Wayland desktop")
        os.environ.pop("WAYLAND_DISPLAY")
        for plat in ("win32", "darwin"):
            sys.platform = plat
            eq(server.can_open_browser(), True, plat)
    finally:
        sys.platform = saved_platform
        os.environ.clear()
        os.environ.update(saved_env)


@test("an old Python (Ubuntu 22.04's 3.10) gets a clear message, not a traceback")
def t_python_version_message():
    code = ("import sys, runpy; sys.version_info = (3, 10, 12); sys.argv = ['vcfa-import.py', '--version']; "
            "runpy.run_path({!r}, run_name='__main__')").format(str(ROOT / "vcfa-import.py"))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    eq(proc.returncode, 1)
    assert "needs Python 3.11 or newer" in proc.stderr and "sudo apt install python3.11" in proc.stderr, proc.stderr
    sys.path.insert(0, str(ROOT / "tools"))
    import build
    assert "sys.version_info < (3, 11)" in build.PYZ_VERSION_CHECK, "the .pyz entry point checks too"
    compile("import sys\n" + build.PYZ_VERSION_CHECK, "<pyz __main__>", "exec")


@test("stage: when two sources name different namespaces, the first wins and the rest are reported")
def t_stage_ns_conflicts():
    from vcfaimport.discovery import folder_map_from_rows, network_map_from_rows, stage, tag_map_from_rows
    folders = folder_map_from_rows([{"folder": "Production", "namespace": "ns-folder", "wave": "1"}])
    nets = network_map_from_rows([{"portgroup": "VLAN197-Prod", "namespace": "ns-network", "subnet": "sub-a"},
                                  {"portgroup": "VLAN200-DB", "namespace": "ns-folder", "subnet": "sub-b"}])
    two_nics = '[{"device_key":4000,"network_name":"VLAN197-Prod"},{"device_key":4001,"network_name":"VLAN200-DB"}]'
    rows = [_disc_row(moref="vm-1"),                                            # folder vs network: conflict
            _disc_row(moref="vm-2", nics_json='[{"device_key":4000,"network_name":"VLAN200-DB"}]'),  # agree
            _disc_row(moref="vm-3", namespace="ns-picked"),                     # Select overrides both maps
            _disc_row(moref="vm-4", folder="Elsewhere", nics_json=two_nics)]     # two adapters disagree
    res = stage(rows, Config(), mapping=nets, folder_mapping=folders)
    got = {r.moref: r.namespace for r in res.records}
    eq(got, {"vm-1": "ns-folder", "vm-2": "ns-folder", "vm-3": "ns-picked", "vm-4": "ns-network"})
    by = {c["moref"]: c for c in res.ns_conflicts}
    eq(sorted(by), ["vm-1", "vm-3", "vm-4"], "no conflict when the sources agree")
    eq((by["vm-1"]["source"], by["vm-1"]["ignored"]), ("folder map", [{"source": "portgroup map (VLAN197-Prod)", "namespace": "ns-network"}]))
    eq(by["vm-3"]["source"], "set in Select")
    eq(sorted(i["namespace"] for i in by["vm-3"]["ignored"]), ["ns-folder", "ns-network"])
    eq(by["vm-4"]["ignored"], [{"source": "portgroup map (VLAN200-DB)", "namespace": "ns-folder"}])
    note = next(r.notes for r in res.records if r.moref == "vm-1")
    assert "ns-network (portgroup map (VLAN197-Prod))" in note, note
    tags = tag_map_from_rows([{"tag": "Application:Payroll", "namespace": "ns-tag"}])
    res = stage([_disc_row(moref="vm-5", tags_json='["Application:Payroll"]')], Config(), mapping=nets,
                folder_mapping=folders, tag_mapping=tags)
    eq(res.records[0].namespace, "ns-tag")
    eq(res.ns_conflicts[0]["source"], "tag map (Application:Payroll)")


@test("e2e: namespace suggestions from the Supervisor, or from kubeconfig when listing is not allowed")
def t_e2e_cluster_namespaces():
    from vcfaimport import service
    from vcfaimport.kube import Kubectl
    with tempfile.TemporaryDirectory() as tmp:
        env = _e2e_env(tmp, "happy")
        _seed_cluster(tmp, ["migration-testing-ns-kcvm5", "ns-b"], ["migration-testing-ns-kcvm5/migration-testing"])
        cfg_path = _write_config(tmp)
        saved = dict(os.environ)
        try:
            os.environ.update(env)
            cfg = Config.load(cfg_path)
            names = lambda d: [n["name"] for n in d["namespaces"]]  # noqa: E731
            d = service.cluster_namespaces(Kubectl(cfg))
            eq(names(d), ["migration-testing-ns-kcvm5", "ns-b"], "system namespaces left out")
            eq((d["complete"], d["notes"]), (True, []))
            eq({n["source"] for n in d["namespaces"]}, {"both"}, "on the cluster and in kubeconfig")
            for mode, word in (("forbidden", "may not list"), ("timeout", "did not answer")):
                os.environ["FAKE_KUBECTL_NS_LIST"] = mode
                d = service.cluster_namespaces(Kubectl(cfg))
                eq(names(d), ["migration-testing-ns-kcvm5", "ns-b"], mode + ": kubeconfig contexts, same cluster only")
                eq(d["complete"], False)
                assert word in d["notes"][0] and "kubeconfig" in d["notes"][0], d["notes"]
            os.environ["FAKE_KUBECTL_NS_LIST"] = "forbidden"
            out = _run_cli(["-c", cfg_path, "namespaces"], dict(env, FAKE_KUBECTL_NS_LIST="forbidden"), tmp).stdout
            assert "migration-testing-ns-kcvm5" in out and "kubeconfig context" in out and "may not list" in out, out
            os.environ.pop("FAKE_KUBECTL_NS_LIST")
            cfg.kubectl = "vcfa-test-no-such-kubectl"
            d = service.cluster_namespaces(Kubectl(cfg))
            eq((d["namespaces"], d["complete"]), ([], False), "no kubectl: nothing, and no crash")
            assert d["notes"], d
            c = _Console(tmp, Config.load(cfg_path))
            try:
                first = c.ok("GET", "/api/cluster/namespaces")
                eq(names(first), ["migration-testing-ns-kcvm5", "ns-b"])
                Path(tmp, "cluster.json").write_text(Path(tmp, "cluster.json").read_text().replace('"ns-b"', '"ns-c"'))
                eq(names(c.ok("GET", "/api/cluster/namespaces")), names(first), "cached between looks")
                eq(names(c.ok("GET", "/api/cluster/namespaces?refresh=1")), ["migration-testing-ns-kcvm5", "ns-c"])
            finally:
                c.close()
        finally:
            os.environ.clear()
            os.environ.update(saved)


UNIT = [
    t_stage_ns_conflicts,
    t_serve_open_headless, t_python_version_message,
    t_web_docs, t_help_doc_anchors,
    t_readiness_rules, t_estimate, t_verify_checks, t_notify_channels, t_access_rules,
    t_schedule_rules, t_settings_overlay, t_tag_map_stage, t_apps_together, t_readiness_exclusion,
    t_web_roles_settings,
    t_yaml_roundtrip, t_yaml_quoting, t_no_creation_rollback, t_fake_creation_rollback,
    t_precheck_no_commit,
    t_inventory_aliases, t_inventory_dupes, t_inventory_multinic, t_inventory_skip,
    t_inventory_moref_warning,
    t_planner_grouping, t_planner_salt, t_planner_operation_names_are_per_batch,
    t_render_operation_name_within_batch,
    t_status_classify, t_status_children, t_status_inline, t_status_empty,
    t_status_operator_precheck, t_status_child_does_not_mask_pass, t_status_operator_stall,
    t_status_child_completed_spelling, t_status_child_precheck_condition,
    t_status_operator_import,
    t_status_conditions,
    t_filter_and, t_filter_folder, t_folder_map, t_folder_map_precedence, t_folder_tree,
    t_rollback_scope_warning,
    t_filter_network, t_resolve_identifiers, t_network_map,
    t_stage_namespace_precedence, t_stage_multinic, t_selection_file, t_picker_html,
    t_vcenter_nics,
    t_track_transitions, t_track_no_duplicates, t_track_milestones, t_track_ledger_file,
    t_track_ledger_durable, t_track_provenance, t_track_migration, t_track_csv,
    t_track_target_extraction,
    t_store_wave_moves, t_store_swap_waves, t_triage_groups, t_map_rows_roundtrip,
    t_skip_semantics, t_web_security, t_web_jobs,
    t_http_keepalive_desync, t_http_body_limits, t_http_fuzz, t_http_host_and_paths,
    t_http_non_loopback, t_http_concurrent_edits, t_workspace_lock, t_web_lock_conflict,
    t_jobs_edge_cases, t_web_map_edges, t_web_stage_edges, t_web_hostile_names,
    t_triage_normalisation, t_web_missing_things,
]

E2E = [
    t_e2e_cluster_namespaces,
    t_e2e_web_governance, t_e2e_cli_governance,
    t_fake_operator_name_collision,
    t_e2e_happy, t_e2e_precheck_gate, t_e2e_preflight_missing, t_e2e_flaky,
    t_e2e_init_maps, t_e2e_preflight_subnet_hint, t_e2e_preflight_vpc_subnet, t_e2e_preflight_operator_pod,
    t_e2e_running_message, t_e2e_vanished_batch, t_e2e_abandon, t_e2e_abandon_refuses_live_import,
    t_e2e_circuit_breaker, t_e2e_commit_gate, t_e2e_dry_run, t_e2e_resume,
    t_e2e_report, t_e2e_server_dry_run,
    t_e2e_discover, t_e2e_folders, t_e2e_folder_execution, t_e2e_discover_auth, t_e2e_rediscover,
    t_e2e_tracker, t_e2e_tracker_retry,
    t_e2e_rollback, t_e2e_run_rollback_failed, t_e2e_rollback_nowait, t_e2e_rollback_held,
    t_e2e_web_campaign, t_e2e_web_stop,
    t_e2e_web_circuit_breaker, t_e2e_web_commit_gate, t_e2e_web_kubectl_missing,
    t_e2e_web_preflight_problems, t_e2e_web_vcenter_failures, t_e2e_web_vanished_batch,
    t_e2e_web_crash_resume, t_e2e_web_cli_interplay, t_e2e_web_load_during_run,
    t_e2e_web_retry_abandon, t_e2e_web_scope_limits, t_e2e_web_chaos,
    t_e2e_web_misc, t_cli_unicode_output, t_web_demo_ports,
]

SLOW = [t_e2e_scale, t_e2e_web_scale]


def main() -> int:
    for stream in (sys.stdout, sys.stderr):   # tracebacks may carry non-ASCII test data
        stream.reconfigure(errors="backslashreplace")
    args = set(sys.argv[1:])
    only = [a.split("=", 1)[1] for a in args if a.startswith("--only=")]
    if only:
        chosen = [fn for fn in UNIT + E2E + SLOW if any(o in fn.__test_name__ for o in only)]
        for fn in chosen:
            fn()
        print("\n{} passed, {} failed".format(RESULTS["pass"], RESULTS["fail"]))
        for name, tb in FAILURES:
            print("\n--- {} ---\n{}".format(name, tb))
        return 1 if RESULTS["fail"] else 0
    print("unit tests")
    for fn in UNIT:
        fn()
    if "--unit-only" not in args:
        try:
            import yaml  # noqa: F401
        except ImportError:
            print("\ne2e tests skipped: PyYAML is needed by tools/fake_kubectl.py")
        else:
            print("\nend-to-end tests (fake kubectl)")
            for fn in E2E:
                fn()
            if "--slow" in args:
                print("\nscale test")
                for fn in SLOW:
                    fn()
            else:
                print("\n(scale test skipped; pass --slow to run the 1800-VM case)")

    print("\n{} passed, {} failed".format(RESULTS["pass"], RESULTS["fail"]))
    for name, tb in FAILURES:
        print("\n--- {} ---\n{}".format(name, tb))
    return 1 if RESULTS["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
