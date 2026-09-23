#!/usr/bin/env python3
"""Test suite for vcfa-import.

Runs without pytest:   python tests/run_tests.py

Unit tests need nothing but the standard library. The end-to-end tests drive
the real CLI against tools/fake_kubectl.py, which stands in for kubectl and the
Mobility Operator; those need PyYAML (a test-only dependency).
"""

from __future__ import annotations

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


# -------------------------------------------------------------------- main
UNIT = [
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
]

E2E = [
    t_fake_operator_name_collision,
    t_e2e_happy, t_e2e_precheck_gate, t_e2e_preflight_missing, t_e2e_flaky,
    t_e2e_init_maps, t_e2e_preflight_subnet_hint, t_e2e_preflight_vpc_subnet, t_e2e_preflight_operator_pod,
    t_e2e_running_message, t_e2e_vanished_batch, t_e2e_abandon, t_e2e_abandon_refuses_live_import,
    t_e2e_circuit_breaker, t_e2e_commit_gate, t_e2e_dry_run, t_e2e_resume,
    t_e2e_report, t_e2e_server_dry_run,
    t_e2e_discover, t_e2e_folders, t_e2e_folder_execution, t_e2e_discover_auth, t_e2e_rediscover,
    t_e2e_tracker, t_e2e_tracker_retry,
    t_e2e_rollback, t_e2e_run_rollback_failed, t_e2e_rollback_nowait, t_e2e_rollback_held,
]

SLOW = [t_e2e_scale]


def main() -> int:
    args = set(sys.argv[1:])
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
