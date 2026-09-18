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
import random
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

TOKEN = "fake-session-token"
USER = "administrator@vsphere.local"
PASSWORD = "hunter2"

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
                "backing": {
                    "type": "DISTRIBUTED_PORTGROUP",
                    "network": "dvportgroup-{}".format(NETWORKS.index(
                        NETWORKS[(i + n) % len(NETWORKS)]) + 100),
                },
                "mac_address": "00:50:56:{:02x}:{:02x}:{:02x}".format(i % 255, n, rng.randint(0, 255)),
                "state": "CONNECTED",
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
            },
            "_tools": "RUNNING" if i % 11 else "NOT_RUNNING",
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

    def do_POST(self) -> None:  # noqa: N802
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

        if path.startswith("/api/vcenter/vm/") and path.endswith("/tools"):
            moref = path.split("/")[4]
            vm = inv["vms"].get(moref)
            self._send(200, {"run_state": vm["_tools"]} if vm else {})
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
                             for i, name in enumerate(NETWORKS)])
            return

        if path == "/api/vcenter/host":
            self._send(200, [{"host": "host-{}".format(10 + i), "name": "esx{:02d}.lab".format(i)}
                             for i in range(4)])
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
