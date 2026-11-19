#!/usr/bin/env python3
"""A stand-in vCenter REST API, for testing discovery offline.

Serves the handful of /api/vcenter endpoints vcfa-import uses, over plain HTTP
with a self-signed-free setup (the client is pointed at http:// via
VCFA_FAKE_VC_HTTP=1). Run standalone to poke at it:

    python tools/fake_vcenter.py --vms 200 --port 8099
"""

from __future__ import annotations

import argparse
import json
import os
import random
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

TOKEN = "fake-session-token"
USER = "administrator@vsphere.local"
PASSWORD = "hunter2"

# Supervisor namespaces vCenter's own Workload Management knows about -- two
# names so a test can tell "the tenant's own namespace" from "one it cannot
# list itself" (see t_e2e_vcenter_namespaces). FAKE_VC_NS_LIST overrides how
# the endpoint behaves: "forbidden" (403, no System.Read), "old" (404, as on
# a pre-7.0 /rest-only vCenter), "empty" (200, []).
NAMESPACE_INSTANCES = [
    {"cluster": "PROD-CL01", "namespace": "migration-testing-ns-kcvm5",
     "description": "", "config_status": "RUNNING"},
    {"cluster": "PROD-CL01", "namespace": "testing-vpc-k826r",
     "description": "VPC namespace", "config_status": "RUNNING"},
]
# NSX Subnets/SubnetSets vCenter lists per namespace (GET /vcenter/namespaces/{ns}/networks/nsx/subnets).
# FAKE_VC_SUBNET_LIST overrides how the endpoint behaves for every namespace:
# "forbidden" (403), "old" (404, as if the endpoint itself did not exist), "empty" (200, []).
# "type" is the real enum value from VMware's published OpenAPI spec (SUBNET /
# SUBNETSET), not the "Subnet" / "SubnetSet" kind name vcfaimport maps it to.
NAMESPACE_SUBNETS = {
    "migration-testing-ns-kcvm5": [{"name": "subnet-local", "type": "SUBNET"}],
    "testing-vpc-k826r": [{"name": "migration-testing", "type": "SUBNET"}],
}
# GET/POST(?action=check) /vcenter/vm/{vm}/storage/policy/compliance. Defaults
# every VM to COMPLIANT so existing tests that do not care about this feature
# are unaffected. FAKE_VC_STORAGE_COMPLIANCE overrides the answer for every
# VM: "forbidden" (403), "old" (404, as if the VM-level storage policy API did
# not exist), "nopolicy" (200, null body), "noncompliant" / "outofdate" /
# "unknown" (200, that overall_compliance value with a failure_cause).
STORAGE_COMPLIANCE_CAUSE = {
    "NON_COMPLIANT": "Datastore does not satisfy the policy's Space Efficiency rule",
    "OUT_OF_DATE": "The policy was edited but not re-applied to this entity",
    "UNKNOWN_COMPLIANCE": "SPBM could not reach the storage provider",
}

# Storage policies (GET /vcenter/storage/policies), which ones each namespace
# offers (storage_specs of GET /vcenter/namespaces/instances/{ns}), and which one
# each VM's home and disks use (GET /vcenter/vm/{vm}/storage/policy). By default
# every VM uses Gold everywhere, which every namespace offers, so tests that do
# not care about this feature are unaffected. FAKE_VC_VM_POLICY overrides every
# VM: "wrong" (disk 2000 on Bronze, which no namespace offers), "nodisk" (disk
# 2000 has no policy: left out of the map, as vCenter does), "none" (no policy
# at all), "forbidden" (403). FAKE_VC_NS_POLICY: "forbidden" (403 on the
# namespace instance), "nostorage" (no storage_specs).
STORAGE_POLICIES = {"pol-gold-0001": "Gold", "pol-ds2-0003": "DS2-Thick", "pol-bronze-0002": "Bronze"}
NAMESPACE_STORAGE = {
    "migration-testing-ns-kcvm5": ["pol-gold-0001", "pol-ds2-0003"],
    "testing-vpc-k826r": ["pol-gold-0001"],
    # The namespaces tools/ui_stress.py stages into. They are not in NAMESPACE_INSTANCES (the
    # autodiscovery suggestions stay as they were); the policy endpoint answers for them, because
    # the compliance gate fails closed on a namespace whose policies it could not read.
    "prod-web-ns1": ["pol-gold-0001"], "prod-db-ns2": ["pol-gold-0001"], "dmz-ns3": ["pol-gold-0001"],
    "zz-stress-ns": ["pol-gold-0001"], "zz-tag-ns": ["pol-gold-0001"],
}

CLUSTERS = ["PROD-CL01", "PROD-CL02", "DMZ-CL01"]
# A realistic nested VM-folder tree. Keys are folder ids, values are
# (parent id, display name). "group-v1" is the datacenter's hidden root.
FOLDER_TREE = {
    "group-v1":   (None,        ""),            # datacenter root "vm" folder
    "group-v10":  ("group-v1",  "Production"),
    "group-v11":  ("group-v10", "Web"),
    "group-v12":  ("group-v10", "App"),
    "group-v13":  ("group-v11", "Tier1"),
    "group-v20":  ("group-v1",  "Databases"),
    "group-v30":  ("group-v1",  "DMZ"),
    "group-v40":  ("group-v1",  "Legacy"),
    "group-v41":  ("group-v40", "Decommission"),
}
# VMs are placed round-robin into these leaf-ish folders (the root included,
# because real estates always have a few VMs loose at the top).
PLACEMENT = ["group-v11", "group-v13", "group-v12", "group-v20", "group-v30",
             "group-v41", "group-v1", "group-v11"]


def folder_path(fid: str) -> str:
    parts = []
    while fid and FOLDER_TREE.get(fid, (None, ""))[0] is not None:
        parent, name = FOLDER_TREE[fid]
        parts.append(name)
        fid = parent
    return "/".join(reversed(parts))
NETWORKS = ["VLAN197-Prod", "VLAN200-DB", "VLAN210-App", "DMZ-Uplink"]
# Portgroups no VM uses (yet): mapping suggestions must list these too.
SPARE_NETWORKS = [("VLAN300-Spare", "DISTRIBUTED_PORTGROUP"), ("Lab-Isolated", "STANDARD_PORTGROUP")]

# vSphere tags: an "Application" category that spans folders (so apps are not
# just folders by another name) and a "Tier" category derived from the name.
CATEGORIES = {"cat-app": "Application", "cat-tier": "Tier"}
APPS = ["Payroll", "CRM", "Portal", "Billing"]
TIERS = {"app": "App", "web": "Web", "db": "DB", "svc": "App"}
TAGS = {"tag-app-{}".format(a.lower()): ("cat-app", a) for a in APPS}
TAGS.update({"tag-tier-{}".format(t.lower()): ("cat-tier", t) for t in set(TIERS.values())})
GUEST = ["RHEL_8_64", "WINDOWS_SERVER_2019", "UBUNTU_64"]


def build_inventory(count: int, seed: int = 7) -> Dict[str, Any]:
    rng = random.Random(seed)
    vms: Dict[str, Any] = {}
    folder_members: Dict[str, List[str]] = {f: [] for f in FOLDER_TREE}
    cluster_members: Dict[str, List[str]] = {c: [] for c in CLUSTERS}
    for i in range(count):
        moref = "vm-{}".format(1483400 + i * 2)
        folder = PLACEMENT[i % len(PLACEMENT)]
        cluster = CLUSTERS[i % len(CLUSTERS)]
        nic_count = 2 if i % 9 == 0 else 1
        nics = {}
        for n in range(nic_count):
            nics[str(4000 + n)] = {
                # readiness: a disconnected adapter now and then
                "state": "NOT_CONNECTED" if (i % 19 == 0 and n == 0) else "CONNECTED",
                "backing": {
                    "type": "DISTRIBUTED_PORTGROUP",
                    "network": "dvportgroup-{}".format(NETWORKS.index(
                        NETWORKS[(i + n) % len(NETWORKS)]) + 100),
                },
                "mac_address": "00:50:56:{:02x}:{:02x}:{:02x}".format(i % 255, n, rng.randint(0, 255)),
            }
        vms[moref] = {
            "vm": moref,
            "name": "{}-{:03d}".format(["app", "web", "db", "svc"][i % 4], i),
            "power_state": "POWERED_ON" if i % 7 else "POWERED_OFF",
            "cpu_count": [2, 4, 8][i % 3],
            "memory_size_MiB": [4096, 8192, 16384][i % 3],
            "_detail": {
                "cpu": {"count": [2, 4, 8][i % 3]},
                "memory": {"size_MiB": [4096, 8192, 16384][i % 3]},
                "guest_OS": GUEST[i % len(GUEST)],
                "host": "host-{}".format(10 + (i % 4)),
                "nics": nics,
                "hardware": {"version": "VMX_08" if i % 23 == 0 else "VMX_19"},
                "disks": dict({"2000": {"backing": {"type": "VMDK_FILE"}}},
                              **({"2001": {"backing": {"type": "RDM"}}} if i % 17 == 0 else {})),
                "cdroms": {"3000": {"state": "CONNECTED" if i % 13 == 0 else "NOT_CONNECTED",
                                    "backing": {"type": "ISO_FILE" if i % 13 == 0 else "CLIENT_DEVICE"}}},
            },
            "_tools": "RUNNING" if i % 11 else "NOT_RUNNING",
            # every third VM answers on loopback; the rest sit on TEST-NET (never reachable)
            "_ip": "127.0.0.1" if i % 3 == 0 else "203.0.113.{}".format(i % 250 + 1),
            "_tags": ["tag-app-{}".format(APPS[(i // 4) % len(APPS)].lower())]
                     + (["tag-tier-{}".format(TIERS[["app", "web", "db", "svc"][i % 4]].lower())]),
        }
        folder_members[folder].append(moref)
        cluster_members[cluster].append(moref)
    return {"vms": vms, "folders": folder_members, "clusters": cluster_members}


class Handler(BaseHTTPRequestHandler):
    inventory: Dict[str, Any] = {}

    def log_message(self, *args) -> None:  # silence
        pass

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        return self.headers.get("vmware-api-session-id") == TOKEN

    def _storage_compliance_response(self, moref: str) -> None:
        vm = Handler.inventory["vms"].get(moref)
        if not vm:
            self._send(404, {"error": "no such vm"})
            return
        mode = os.environ.get("FAKE_VC_STORAGE_COMPLIANCE", "")
        if mode == "forbidden":
            self._send(403, {"error_type": "UNAUTHORIZED",
                             "messages": [{"default_message": "System.Read is required"}]})
            return
        if mode == "old":
            self._send(404, {"error": "not found"})
            return
        if mode == "nopolicy":
            self._send(200, None)
            return
        overall = {"noncompliant": "NON_COMPLIANT", "outofdate": "OUT_OF_DATE",
                  "unknown": "UNKNOWN_COMPLIANCE"}.get(mode, "COMPLIANT")
        disk_info = {"status": overall}
        cause = STORAGE_COMPLIANCE_CAUSE.get(overall)
        if cause:
            disk_info["failure_cause"] = [{"id": "com.vmware.spbm.fake", "default_message": cause, "args": []}]
        self._send(200, {
            "overall_compliance": overall,
            "vm_home": dict(disk_info),
            "disks": {"2000": dict(disk_info)},
        })

    def _vm_policy_response(self, moref: str) -> None:
        vm = Handler.inventory["vms"].get(moref)
        if not vm:
            self._send(404, {"error": "no such vm"})
            return
        mode = os.environ.get("FAKE_VC_VM_POLICY", "")
        if mode == "forbidden":
            self._send(403, {"error_type": "UNAUTHORIZED",
                             "messages": [{"default_message": "StorageProfile.View is required"}]})
            return
        if mode == "none":
            self._send(200, {"disks": {}})
            return
        disks = {key: "pol-gold-0001" for key in vm["_detail"].get("disks", {})}
        if mode == "wrong":
            disks["2000"] = "pol-bronze-0002"
        if mode == "nodisk":
            disks.pop("2000", None)
        self._send(200, {"vm_home": "pol-gold-0001", "disks": disks})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/api/cis/tagging/tag-association"):
            if not self._authed():
                self._send(401, {"error": "unauthenticated"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            vms = Handler.inventory["vms"]
            out = []
            for obj in body.get("object_ids", []):
                vm = vms.get(obj.get("id"))
                if vm:
                    out.append({"object_id": obj, "tag_ids": vm["_tags"]})
            self._send(200, out)
            return
        parsed = urllib.parse.urlparse(self.path)
        if (parsed.path.startswith("/api/vcenter/vm/") and parsed.path.endswith("/storage/policy/compliance")
                and urllib.parse.parse_qs(parsed.query).get("action") == ["check"]):
            if not self._authed():
                self._send(401, {"error": "unauthenticated"})
                return
            self._storage_compliance_response(parsed.path.split("/")[4])
            return
        if self.path != "/api/session":
            self._send(404, {"error": "not found"})
            return
        import base64
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            self._send(401, {"error": "no credentials"})
            return
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        user, _, password = decoded.partition(":")
        if user != USER or password != PASSWORD:
            self._send(401, {"error": "invalid credentials"})
            return
        self._send(201, TOKEN)

    def do_DELETE(self) -> None:  # noqa: N802
        self._send(204, None)

    def do_GET(self) -> None:  # noqa: N802
        if not self._authed():
            self._send(401, {"error": "unauthenticated"})
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        inv = Handler.inventory

        if path == "/api/vcenter/vm":
            items = list(inv["vms"].values())
            if "folders" in query:
                wanted = set()
                for f in query["folders"]:
                    wanted |= set(inv["folders"].get(f, []))
                items = [v for v in items if v["vm"] in wanted]
            if "clusters" in query:
                wanted = set()
                for c in query["clusters"]:
                    wanted |= set(inv["clusters"].get(c, []))
                items = [v for v in items if v["vm"] in wanted]
            if "power_states" in query:
                items = [v for v in items if v["power_state"] in query["power_states"]]
            self._send(200, [{k: v for k, v in vm.items() if not k.startswith("_")}
                             for vm in items])
            return

        if path.startswith("/api/cis/tagging/tag/"):
            tid = path.rsplit("/", 1)[-1]
            if tid not in TAGS:
                self._send(404, {"error": "no such tag"})
                return
            cat, name = TAGS[tid]
            self._send(200, {"id": tid, "name": name, "category_id": cat})
            return

        if path.startswith("/api/cis/tagging/category/"):
            cid = path.rsplit("/", 1)[-1]
            self._send(200, {"id": cid, "name": CATEGORIES.get(cid, cid)})
            return

        if path.startswith("/api/vcenter/vm/") and path.endswith("/guest/identity"):
            vm = inv["vms"].get(path.split("/")[4])
            if not vm or vm["_tools"] != "RUNNING" or vm["power_state"] != "POWERED_ON":
                self._send(503, {"error": "tools not running"})
                return
            self._send(200, {"ip_address": vm["_ip"], "host_name": vm["name"]})
            return

        if path.startswith("/api/vcenter/vm/") and path.endswith("/tools"):
            moref = path.split("/")[4]
            vm = inv["vms"].get(moref)
            self._send(200, {"run_state": vm["_tools"]} if vm else {})
            return

        if path.startswith("/api/vcenter/vm/") and path.endswith("/storage/policy/compliance"):
            self._storage_compliance_response(path.split("/")[4])
            return

        if path.startswith("/api/vcenter/vm/") and path.endswith("/storage/policy"):
            self._vm_policy_response(path.split("/")[4])
            return

        if path == "/api/vcenter/storage/policies":
            self._send(200, [{"policy": pid, "name": name, "description": ""}
                             for pid, name in STORAGE_POLICIES.items()])
            return

        if path.startswith("/api/vcenter/namespaces/instances/"):
            namespace = urllib.parse.unquote(path.split("/")[5])
            mode = os.environ.get("FAKE_VC_NS_POLICY", "")
            if mode == "forbidden":
                self._send(403, {"error_type": "UNAUTHORIZED",
                                 "messages": [{"default_message": "System.Read is required"}]})
                return
            known = {n["namespace"]: n for n in NAMESPACE_INSTANCES}
            if namespace not in known and namespace in NAMESPACE_STORAGE:
                known[namespace] = {"namespace": namespace, "cluster": "PROD-CL01", "description": ""}
            if namespace not in known:
                self._send(404, {"error": "no such namespace"})
                return
            info = {"cluster": known[namespace]["cluster"], "config_status": "RUNNING",
                    "description": known[namespace]["description"]}
            if mode != "nostorage":
                info["storage_specs"] = [{"policy": pid, "limit": 262144}
                                         for pid in NAMESPACE_STORAGE.get(namespace, [])]
            self._send(200, info)
            return

        if path.startswith("/api/vcenter/vm/"):
            moref = path.split("/")[4]
            vm = inv["vms"].get(moref)
            if not vm:
                self._send(404, {"error": "no such vm"})
                return
            detail = dict(vm["_detail"])
            detail["name"] = vm["name"]
            detail["power_state"] = vm["power_state"]
            self._send(200, detail)
            return

        if path == "/api/vcenter/folder":
            items = [{"folder": fid, "name": name, "type": "VIRTUAL_MACHINE"}
                     for fid, (parent, name) in FOLDER_TREE.items() if parent is not None]
            if "parent_folders" in query:
                wanted = set(query["parent_folders"])
                items = [it for it in items if FOLDER_TREE[it["folder"]][0] in wanted]
            self._send(200, items)
            return

        if path == "/api/vcenter/datacenter":
            self._send(200, [{"datacenter": "datacenter-1", "name": "LabDC"}])
            return

        if path == "/api/vcenter/datacenter/datacenter-1":
            self._send(200, {"name": "LabDC", "vm_folder": "group-v1",
                             "host_folder": "group-h1", "datastore_folder": "group-s1",
                             "network_folder": "group-n1"})
            return

        if path == "/api/vcenter/cluster":
            self._send(200, [{"cluster": name, "name": name} for name in inv["clusters"]])
            return

        if path == "/api/vcenter/network":
            self._send(200, [{"network": "dvportgroup-{}".format(100 + i), "name": name,
                              "type": "DISTRIBUTED_PORTGROUP"}
                             for i, name in enumerate(NETWORKS)]
                       + [{"network": "network-{}".format(200 + i), "name": name, "type": kind}
                          for i, (name, kind) in enumerate(SPARE_NETWORKS)])
            return

        if path == "/api/vcenter/host":
            self._send(200, [{"host": "host-{}".format(10 + i), "name": "esx{:02d}.lab".format(i)}
                             for i in range(4)])
            return

        if path == "/api/vcenter/namespaces/instances":
            mode = os.environ.get("FAKE_VC_NS_LIST", "")
            if mode == "forbidden":
                self._send(403, {"error_type": "UNAUTHORIZED",
                                 "messages": [{"default_message": "System.Read is required"}]})
                return
            if mode == "old":
                self._send(404, {"error": "not found"})
                return
            if mode == "empty":
                self._send(200, [])
                return
            self._send(200, NAMESPACE_INSTANCES)
            return

        if path.startswith("/api/vcenter/namespaces/") and path.endswith("/networks/nsx/subnets"):
            namespace = urllib.parse.unquote(path.split("/")[4])
            mode = os.environ.get("FAKE_VC_SUBNET_LIST", "")
            if mode == "forbidden":
                self._send(403, {"error_type": "UNAUTHORIZED",
                                 "messages": [{"default_message": "System.Read is required"}]})
                return
            if mode == "old":
                self._send(404, {"error": "not found"})
                return
            if mode == "empty":
                self._send(200, {"subnets": []})
                return
            if namespace not in {n["namespace"] for n in NAMESPACE_INSTANCES}:
                self._send(404, {"error": "no such namespace"})
                return
            self._send(200, {"subnets": NAMESPACE_SUBNETS.get(namespace, [])})
            return

        self._send(404, {"error": "unsupported path {}".format(path)})


def serve(count: int = 50, port: int = 0) -> tuple:
    Handler.inventory = build_inventory(count)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vms", type=int, default=50)
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()
    srv, port = serve(args.vms, args.port)
    print("fake vCenter on http://127.0.0.1:{} ({} VMs)".format(port, args.vms))
    print("user: {}  password: {}".format(USER, PASSWORD))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
