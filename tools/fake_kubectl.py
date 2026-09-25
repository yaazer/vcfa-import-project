#!/usr/bin/env python3
"""A stand-in kubectl + Mobility Operator, for testing vcfa-import offline.

Point the tool at it with:

    kubectl = "python /path/to/tools/fake_kubectl.py"

and set these environment variables:

    FAKE_KUBECTL_STATE     path to a JSON file holding simulated cluster state
    FAKE_KUBECTL_SCENARIO  happy | flaky | import-flaky | wait | disaster   (default: happy)
                           flaky fails some VMs at precheck; import-flaky passes precheck
                           and fails some at import time (the case that needs rollback)
    FAKE_KUBECTL_SETTLE    polls before a batch reaches a terminal phase (default 2)

It is deliberately not a general kubectl: it implements only the verbs
vcfa-import actually uses. Unlike the tool itself, it may use PyYAML.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import yaml  # test-only dependency

GROUP = "mobility-operator.vmware.com"
VERSION = "v1alpha3"


def state_path() -> Path:
    return Path(os.environ.get("FAKE_KUBECTL_STATE", "fake-cluster.json"))


def load_state() -> dict:
    p = state_path()
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return {
        "namespaces": [],
        "subnets": [],
        "batches": {},      # "ns/name" -> object
        "polls": {},        # "ns/name" -> int
        "applies": 0,
    }


def save_state(data: dict) -> None:
    state_path().write_text(json.dumps(data, indent=1), encoding="utf-8")


def scenario() -> str:
    return os.environ.get("FAKE_KUBECTL_SCENARIO", "happy")


def settle() -> int:
    return int(os.environ.get("FAKE_KUBECTL_SETTLE", "2"))


def op_outcome(vm_id: str, precheck: bool) -> str:
    """Decide the terminal phase for one operation."""
    scen = scenario()
    tail = int("".join(ch for ch in vm_id if ch.isdigit()) or "0")
    if scen == "disaster":
        return "Failed"
    if scen == "flaky" and tail % 5 == 0:
        return "Failed"
    if scen == "import-flaky" and not precheck and tail % 5 == 0:
        return "Failed"
    if scen == "wait" and not precheck:
        return "AwaitingCommit"
    return "Succeeded"


def operation_name_owners(data: dict) -> dict:
    """Which batch owns each child ImportOperation name.

    Children are cluster objects in a shared namespace, so the first batch to
    claim a name keeps it; dict order here is apply order. Mirrors the real
    controller, which cannot adopt a child another controller already owns.
    """
    owners: dict = {}
    for key, obj in data.get("batches", {}).items():
        ns = key.split("/", 1)[0]
        for entry in ((obj.get("spec") or {}).get("operations") or []):
            owners.setdefault((ns, entry.get("name")), key)
    return owners


def blocked_operations(obj: dict, key: str, data: dict) -> list:
    """Operation names this batch wants but another batch already owns."""
    owners = operation_name_owners(data)
    ns = key.split("/", 1)[0]
    return [e.get("name") for e in ((obj.get("spec") or {}).get("operations") or [])
            if owners.get((ns, e.get("name")), key) != key]


def build_status(obj: dict, polls: int, key: str = "", data: Optional[dict] = None) -> dict:
    """Synthesise the status the operator would report after `polls` reads."""
    spec = obj.get("spec", {})
    default = spec.get("defaultSpec", {})
    control = default.get("controlAction", {}) or {}
    precheck = bool(control.get("precheckOnly"))
    committed = control.get("commitAction") == "Auto" and obj.get("_committed")

    blocked = blocked_operations(obj, key, data) if data is not None and key else []
    if blocked:
        # Observed 2026-09-21: the batch applies fine, but its controller can
        # neither create nor adopt the children, so it never reports any
        # operations at all. It stays here until the other batch is deleted.
        total = len((spec.get("operations") or []))
        msg = ("number of operations from status: 0 does not match number of "
               "operations from spec: {}".format(total))
        busy = "Operations are not ready. Operation Names: " + "; ".join(blocked)
        return {"conditions": [
            {"type": "Complete", "status": "False", "reason": "ObjectNotReady",
             "message": busy},
            {"type": "ReadyForCommit", "status": "False", "reason": "ObjectNotReady",
             "message": busy},
            {"type": "ReadyForImport", "status": "False", "reason": "ObjectNotReady",
             "message": msg}],
            "readyCount": 0, "readyOps": [], "_blocked_on": blocked}

    if polls < settle():
        # Still working: every gate False / ObjectNotReady, exactly as observed.
        names = ", ".join(e.get("name", "") for e in spec.get("operations", []))
        msg = "Operations are not ready. Operation Names: " + names
        return {"conditions": [
            {"type": t, "status": "False", "reason": "ObjectNotReady", "message": msg}
            for t in ("Complete", "ReadyForCommit", "ReadyForImport")],
            "readyCount": 0, "readyOps": []}

    rollback_at = obj.get("_rollback_poll")   # poll count when rollbackAction arrived
    rolling = rollback_at is not None
    reverted = rolling and polls >= rollback_at + settle()

    ops = []
    terminal = True
    for entry in spec.get("operations", []):
        vm_id = entry.get("spec", {}).get("virtualMachineID", "")
        phase = op_outcome(vm_id, precheck)
        if phase == "AwaitingCommit" and committed:
            phase = "Succeeded"
        if rolling and (phase in ("Failed", "AwaitingCommit") or obj.get("_rollback_at_creation")):
            # Only what did not commit can be handed back to vCenter -- and
            # with the action present from the start, nothing ever commits.
            phase = "RolledBack" if reverted else "RollingBack"
            if not reverted:
                terminal = False
        if phase == "AwaitingCommit":
            terminal = False
        ops.append({
            "name": entry.get("name"),
            "virtualMachineID": vm_id,
            "phase": phase,
            "message": {"Succeeded": "", "Failed": "VM Tools not running",
                        "AwaitingCommit": "waiting for commit",
                        "RollingBack": "reverting ownership to vCenter",
                        "RolledBack": "ownership reverted to vCenter"}.get(phase, ""),
        })

    return real_vocabulary(obj, ops, precheck, rolling, reverted)


def real_vocabulary(obj: dict, ops: list, precheck: bool, rolling: bool, reverted: bool) -> dict:
    """Shape the status the way the real operator does (VCF 9.1, v1alpha3).

    Observed in a lab on 2026-09-18:
      conditions ReadyForImport / ReadyForCommit / Complete, each "True" when
      reached or "False" with reason ObjectNotReady and a message such as
      "Operations are not ready. Operation Names: <op>"; plus readyCount and
      readyOps. No per-operation list. Precheck-only batches *do* get
      ImportOperation children (2026-09-21). The failed/completed/rolled-back
      name lists below (failedOps, completedOps, rolledBackOps) are guesses.
    """
    names = [o["name"] for o in ops]
    failed = [o["name"] for o in ops if o["phase"] == "Failed"]
    ok = [o["name"] for o in ops if o["phase"] == "Succeeded"]
    waiting = [o["name"] for o in ops if o["phase"] == "AwaitingCommit"]
    rolled = [o["name"] for o in ops if o["phase"] == "RolledBack"]
    rolling_ops = [o["name"] for o in ops if o["phase"] == "RollingBack"]
    not_ready_msg = "Operations are not ready. Operation Names: " + ", ".join(names)

    def cond(kind, ok_flag, reason=None, message=""):
        return {"type": kind, "status": "True" if ok_flag else "False",
                "reason": "True" if ok_flag else (reason or "ObjectNotReady"),
                "message": "" if ok_flag else message,
                "lastTransitionTime": "2026-09-18T17:16:39Z"}

    if precheck:
        # The precheck verdict: ReadyForImport. Commit/Complete never turn True.
        if failed:
            ready_import = cond("ReadyForImport", False, "PrecheckFailed",
                                "VM Tools not running: " + ", ".join(failed))
        else:
            ready_import = cond("ReadyForImport", True)
        ready = ok
        status = {
            "conditions": [cond("Complete", False, message=not_ready_msg),
                           cond("ReadyForCommit", False, message=not_ready_msg),
                           ready_import],
            "readyCount": len(ready), "readyOps": ready,
        }
        if failed:
            status["failedOps"] = failed
        return status

    # Import batch.
    ready = ok + waiting + rolled + rolling_ops
    done = ok
    conds = [
        cond("ReadyForImport", bool(ready), message=not_ready_msg),
        cond("ReadyForCommit", bool(ok or waiting) and not rolling,
             "RolledBack" if reverted else ("RollingBack" if rolling else None),
             "rollback requested" if rolling else not_ready_msg),
        cond("Complete", bool(ok) and not waiting and not failed and not rolling,
             "RolledBack" if reverted else ("RollingBack" if rolling else None),
             "rollback requested" if rolling else not_ready_msg),
    ]
    status = {"conditions": conds, "readyCount": len(ready), "readyOps": ready,
              "completedOps": done}
    if failed:
        status["failedOps"] = failed
        status["conditions"][2] = cond("Complete", False, "OperationsFailed",
                                       "VM Tools not running: " + ", ".join(failed))
    if rolled:
        status["rolledBackOps"] = rolled
    # Per-VM detail for import batches also comes through ImportOperation children.
    status["_ops"] = ops
    status["_precheck"] = precheck
    return status


def child_operations(obj: dict, status: dict) -> list:
    """The ImportOperation objects the operator would create for a batch.

    Observed on a real Supervisor (2026-09-21): precheck-only batches create
    children too, and a child is named after its operation exactly -- the batch
    name is *not* prefixed. That naming is what lets two batches collide.

    A batch that lost the race for its names creates nothing (see build_status).
    """
    name = obj["metadata"]["name"]
    ns = obj["metadata"]["namespace"]
    if status.get("_blocked_on"):
        return []
    out = []
    for entry in status.get("_ops", []):
        out.append({
            "apiVersion": "{}/{}".format(GROUP, VERSION),
            "kind": "ImportOperation",
            "metadata": {
                "name": entry["name"][:63],
                "namespace": ns,
                "ownerReferences": [{"kind": "ImportOperationBatch", "name": name,
                                     "controller": True, "blockOwnerDeletion": True}],
            },
            "spec": {"virtualMachineID": entry["virtualMachineID"]},
            "status": child_conditions(entry["phase"], entry.get("message", ""),
                                       precheck=bool(status.get("_precheck"))),
        })
    return out


# The VM-lifecycle conditions a finished import child carries alongside its
# verdict (observed 2026-09-23). The tool ignores them; they are here so the
# fake's objects look like the real ones.
CHILD_LIFECYCLE = ("VirtualMachineCreated", "VirtualMachineReady",
                   "VirtualMachineReadyForImport", "NetworkBackingReady",
                   "GuestCustomization", "VirtualMachineSetManagedBySucceeded")


def child_conditions(phase: str, message: str, precheck: bool = False) -> dict:
    """An ImportOperation's status in the child condition vocabulary.

    Children do NOT speak the batch's vocabulary -- observed 2026-09-23 on a
    real Supervisor:
      * a child's completion condition is `Completed`, not the batch's `Complete`
      * a child states its precheck verdict as `PrecheckSucceeded`; batches have
        no equivalent and signal a pass with ReadyForImport: True
      * a precheck-only child carries `PrecheckSucceeded` and nothing else
    Mirroring the batch's three conditions onto children (as this fake used to)
    hid a bug that left committed VMs reading as awaiting_commit forever.
    """
    def cond(kind, ok, reason=None):
        return {"type": kind, "status": "True" if ok else "False",
                "reason": "True" if ok else (reason or "ObjectNotReady"),
                "message": "" if ok else message}

    if precheck:
        # Observed: the only condition on a precheck child.
        if phase == "Succeeded":
            return {"conditions": [cond("PrecheckSucceeded", True)]}
        if phase == "Failed":
            return {"conditions": [cond("PrecheckSucceeded", False, "PrecheckFailed")]}
        return {"conditions": [cond("PrecheckSucceeded", False)]}

    done = [cond(k, True) for k in CHILD_LIFECYCLE]
    if phase == "Succeeded":
        return {"conditions": [cond("PrecheckSucceeded", True), cond("ReadyForCommit", True),
                               cond("Completed", True)] + done,
                "completionTime": "2026-09-23T19:52:11Z"}
    if phase == "AwaitingCommit":
        # Completed-while-False at the gate is inferred, not yet observed: the
        # lab only ever caught these children after the commit had landed.
        return {"conditions": [cond("PrecheckSucceeded", True), cond("ReadyForCommit", True),
                               cond("Completed", False)] + done}
    if phase == "Failed":
        return {"conditions": [cond("PrecheckSucceeded", True),
                               cond("ReadyForCommit", False, "OperationFailed"),
                               cond("Completed", False, "OperationFailed")]}
    if phase in ("RollingBack", "RolledBack"):
        return {"conditions": [cond("PrecheckSucceeded", True),
                               cond("ReadyForCommit", False, phase),
                               cond("Completed", False, phase)]}
    return {"conditions": [cond("ReadyForCommit", False), cond("Completed", False)]}


def strip_globals(argv: list) -> list:
    out = []
    i = 0
    while i < len(argv):
        if argv[i] in ("--kubeconfig", "--context"):
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


def arg_value(argv: list, flag: str, default=None):
    if flag in argv:
        idx = argv.index(flag)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return default


def main(argv: list) -> int:
    argv = strip_globals(argv)
    if not argv:
        return 1
    verb = argv[0]
    data = load_state()
    ns = arg_value(argv, "-n") or arg_value(argv, "--namespace")

    if verb == "config":
        if len(argv) > 1 and argv[1] == "view":
            # What `kubectl vsphere login` leaves behind: a Supervisor context, one
            # context per namespace on the same cluster, and an unrelated cluster.
            contexts = [{"name": "fake-supervisor", "context": {"cluster": "10.0.0.10", "user": "wcp:10.0.0.10:admin"}}]
            contexts += [{"name": n, "context": {"cluster": "10.0.0.10", "namespace": n, "user": "wcp:10.0.0.10:admin"}}
                         for n in data["namespaces"]]
            contexts.append({"name": "elsewhere", "context": {"cluster": "10.9.9.9", "namespace": "other-cluster-ns",
                                                             "user": "someone"}})
            print(json.dumps({"apiVersion": "v1", "kind": "Config", "current-context": "fake-supervisor",
                              "contexts": contexts}))
            return 0
        print("fake-supervisor")
        return 0

    if verb == "version":
        print(json.dumps({"serverVersion": {"gitVersion": "v1.30.0+vmware.wcp.1"}}))
        return 0

    if verb == "api-resources":
        group = arg_value(argv, "--api-group", "")
        if group == GROUP:
            for res, kind in (
                ("importoperationbatches", "ImportOperationBatch"),
                ("importoperations", "ImportOperation"),
                ("virtualmachineinframigrations", "VirtualMachineInfraMigration"),
                ("namespaceinframigrations", "NamespaceInfraMigration"),
            ):
                print("{}   {}/{}   true   {}".format(res, GROUP, VERSION, kind))
        return 0

    if verb == "auth":
        print("yes")
        return 0

    if verb == "apply":
        text = sys.stdin.read()
        obj = yaml.safe_load(text)
        if obj is None:
            sys.stderr.write("error: empty manifest\n")
            return 1
        meta = obj.get("metadata", {})
        key = "{}/{}".format(meta.get("namespace"), meta.get("name"))
        if "--dry-run=server" in argv:
            if meta.get("namespace") not in data["namespaces"]:
                sys.stderr.write('Error from server (NotFound): namespaces "{}" not found\n'
                                 .format(meta.get("namespace")))
                return 1
            print("importoperationbatch.{}/{} created (server dry run)".format(GROUP, meta.get("name")))
            return 0
        data["batches"][key] = obj
        data["polls"].setdefault(key, 0)
        data["applies"] += 1
        control = ((obj.get("spec") or {}).get("defaultSpec") or {}).get("controlAction") or {}
        if control.get("rollbackAction"):
            # The real operator reads rollbackAction as an instruction: a batch
            # created with it reverts every operation and imports nothing.
            obj["_rollback_poll"] = 0
            obj["_rollback_at_creation"] = True
        save_state(data)
        print("importoperationbatch.{}/{} created".format(GROUP, meta.get("name")))
        return 0

    if verb == "get":
        resource = argv[1] if len(argv) > 1 else ""
        name = argv[2] if len(argv) > 2 and not argv[2].startswith("-") else None
        base = resource.split(".")[0]

        if base in ("pod", "pods"):
            phase = os.environ.get("FAKE_KUBECTL_OPERATOR_POD", "Running")
            healthy = phase == "Running"
            print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [{
                "metadata": {"name": "mobility-operator-controller-manager-7d9f8b6c4-x2k9p",
                             "namespace": "vmware-system-mobility"},
                "status": {"phase": phase if healthy else "Running",
                           "containerStatuses": [{"name": "manager", "ready": healthy,
                                                  "restartCount": 0 if healthy else 17,
                                                  "state": {} if healthy else
                                                  {"waiting": {"reason": phase}}}]},
            }, {
                "metadata": {"name": "coredns-abc", "namespace": "kube-system"},
                "status": {"phase": "Running", "containerStatuses": [{"ready": True}]},
            }]}))
            return 0

        if base in ("namespace", "namespaces", "ns") and name is None:
            mode = os.environ.get("FAKE_KUBECTL_NS_LIST", "")
            if mode == "forbidden":
                sys.stderr.write('Error from server (Forbidden): namespaces is forbidden: User "sso:admin@vsphere.local" '
                                 'cannot list resource "namespaces" in API group "" at the cluster scope\n')
                return 1
            if mode == "timeout":
                sys.stderr.write("Unable to connect to the server: dial tcp 10.0.0.10:443: i/o timeout\n")
                return 1
            system = ["default", "kube-system", "kube-public", "vmware-system-mobility", "svc-tmc-c8", "tkg-system"]
            print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [
                {"metadata": {"name": n}} for n in system + list(data["namespaces"])]}))
            return 0
        if base in ("namespace", "namespaces", "ns"):
            if name in data["namespaces"]:
                print("namespace/{}".format(name))
                return 0
            sys.stderr.write('Error from server (NotFound): namespaces "{}" not found\n'.format(name))
            return 1

        if base == "pods" and "-A" in argv:
            # The operator's pod, as preflight's health check sees it.
            print(json.dumps({"apiVersion": "v1", "kind": "List", "items": [{
                "metadata": {"name": "mobility-operator-controller-manager-7d9f8b6c4-x2k9p",
                             "namespace": "vmware-system-mobility"},
                "status": {"phase": "Running",
                           "containerStatuses": [{"ready": True, "restartCount": 0}]},
            }]}))
            return 0

        if base in ("subnet", "subnets"):
            if name is None and not ns and os.environ.get("FAKE_KUBECTL_SUBNET_LIST") == "forbidden":
                sys.stderr.write('Error from server (Forbidden): subnets.crd.nsx.vmware.com is forbidden: User '
                                 '"sso:admin@vsphere.local" cannot list resource "subnets" in API group '
                                 '"crd.nsx.vmware.com" at the cluster scope\n')
                return 1
            if name is None:
                # A listing: everything seeded for this namespace, shaped like the API.
                items = []
                for key in data["subnets"]:
                    if ns and not key.startswith(ns + "/"):   # no -n means -A
                        continue
                    obj_name = key.split("/", 1)[1]
                    items.append({
                        "apiVersion": "crd.nsx.vmware.com/v1alpha1", "kind": "Subnet",
                        "metadata": {"name": obj_name, "namespace": key.split("/", 1)[0],
                                     "annotations": {"vmware-system-display-name":
                                                     obj_name.replace("subnet-", "")}},
                    })
                print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
                return 0
            if "{}/{}".format(ns, name) in data["subnets"]:
                print("subnet/{}".format(name))
                return 0
            sys.stderr.write('Error from server (NotFound): subnets "{}" not found\n'.format(name))
            return 1

        if base in ("subnetsets", "networks"):
            sys.stderr.write("error: the server doesn't have a resource type \"{}\"\n".format(base))
            return 1

        if base == "importoperationbatches":
            matches = []
            for key, obj in data["batches"].items():
                obj_ns, obj_name = key.split("/", 1)
                if ns and obj_ns != ns:
                    continue
                if name and obj_name != name:
                    continue
                data["polls"][key] = data["polls"].get(key, 0) + 1
                enriched = json.loads(json.dumps(obj))
                enriched["status"] = {k: v for k, v in build_status(obj, data["polls"][key], key, data).items()
                                      if not k.startswith("_")}
                matches.append(enriched)
            save_state(data)
            if name:
                if not matches:
                    sys.stderr.write('Error from server (NotFound): "{}" not found\n'.format(name))
                    return 1
                print(json.dumps(matches[0]))
            else:
                print(json.dumps({"apiVersion": "v1", "kind": "List", "items": matches}))
            return 0

        if base == "importoperations":
            items = []
            for key, obj in data["batches"].items():
                obj_ns = key.split("/", 1)[0]
                if ns and obj_ns != ns:
                    continue
                status = build_status(obj, data["polls"].get(key, 0), key, data)
                items.extend(child_operations(obj, status))
            print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
            return 0

        sys.stderr.write('error: the server doesn\'t have a resource type "{}"\n'.format(resource))
        return 1

    if verb == "patch":
        name = argv[2]
        key = "{}/{}".format(ns, name)
        if key not in data["batches"]:
            sys.stderr.write('Error from server (NotFound): "{}" not found\n'.format(name))
            return 1
        patch = json.loads(arg_value(argv, "-p", "{}"))
        commit = (patch.get("spec", {}).get("defaultSpec", {})
                  .get("controlAction", {}).get("commitAction"))
        if commit == "Auto":
            data["batches"][key]["_committed"] = True
            data["batches"][key]["spec"]["defaultSpec"].setdefault("controlAction", {})
            data["batches"][key]["spec"]["defaultSpec"]["controlAction"]["commitAction"] = "Auto"
        rollback = (patch.get("spec", {}).get("defaultSpec", {})
                    .get("controlAction", {}).get("rollbackAction"))
        if rollback:
            # Same effect as `kubectl edit` adding rollbackAction: Immediate.
            data["batches"][key].setdefault("_rollback_poll", data["polls"].get(key, 0))
            data["batches"][key]["spec"]["defaultSpec"].setdefault("controlAction", {})
            data["batches"][key]["spec"]["defaultSpec"]["controlAction"]["rollbackAction"] = rollback
        save_state(data)
        print("importoperationbatch.{}/{} patched".format(GROUP, name))
        return 0

    if verb == "delete":
        name = argv[2]
        data["batches"].pop("{}/{}".format(ns, name), None)
        save_state(data)
        print('importoperationbatch.{}/{} deleted'.format(GROUP, name))
        return 0

    sys.stderr.write("fake-kubectl: unsupported verb {}\n".format(verb))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
