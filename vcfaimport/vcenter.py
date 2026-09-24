"""Minimal vCenter REST client (stdlib only).

Covers just what discovery needs: list every VM, resolve its folder, cluster and
network adapters, and read VM Tools state. Works against the modern `/api`
endpoints (vSphere 7.0U2+) and falls back to the older `/rest` ones, unwrapping
the `{"value": ...}` envelope those return.

Credentials are never written to disk. The session token lives in memory and is
deleted on close.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

ENV_SERVER = "VCFA_VC_SERVER"
ENV_USER = "VCFA_VC_USER"
ENV_PASSWORD = "VCFA_VC_PASSWORD"


class VCenterError(Exception):
    pass


class VCenterAuthError(VCenterError):
    pass


@dataclass
class DiscoveredNic:
    device_key: int
    network_id: str = ""
    network_name: str = ""
    mac: str = ""
    backing: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_key": self.device_key,
            "network_id": self.network_id,
            "network_name": self.network_name,
            "mac": self.mac,
            "backing": self.backing,
        }


@dataclass
class DiscoveredVm:
    moref: str
    name: str
    power_state: str = ""
    cpu_count: int = 0
    memory_mb: int = 0
    folder: str = ""          # full path below the datacenter, e.g. Production/Web/Tier1
    datacenter: str = ""
    cluster: str = ""
    host: str = ""
    tools_status: str = ""
    guest_os: str = ""
    nics: List[DiscoveredNic] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)       # "Category:Tag"
    facts: Dict[str, Any] = field(default_factory=dict)  # readiness facts
    ip: str = ""                                         # guest IP, if Tools reports one

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "moref": self.moref,
            "name": self.name,
            "power_state": self.power_state,
            "cpu_count": self.cpu_count,
            "memory_mb": self.memory_mb,
            "folder": self.folder,
            "datacenter": self.datacenter,
            "cluster": self.cluster,
            "host": self.host,
            "tools_status": self.tools_status,
            "guest_os": self.guest_os,
        }
        d["nics"] = [n.to_dict() for n in self.nics]
        d["tags"] = list(self.tags)
        d["facts"] = dict(self.facts)
        d["ip"] = self.ip
        return d


@dataclass
class FolderNode:
    folder_id: str
    path: str            # "" for the datacenter root
    datacenter: str
    root: bool = False


def resolve_credentials(
    server: Optional[str],
    user: Optional[str],
    password: Optional[str],
    prompt: bool = True,
) -> tuple:
    """Fill in server/user/password from flags, then environment, then a prompt."""
    server = server or os.environ.get(ENV_SERVER)
    user = user or os.environ.get(ENV_USER)
    password = password or os.environ.get(ENV_PASSWORD)

    if not server:
        raise VCenterError(
            "no vCenter given; pass --vcenter or set {}".format(ENV_SERVER))
    if not user:
        raise VCenterError(
            "no vCenter user given; pass --user or set {}".format(ENV_USER))
    if not password:
        if not prompt or not sys.stdin or not sys.stdin.isatty():
            raise VCenterError(
                "no vCenter password available; set {} (or run interactively)".format(ENV_PASSWORD))
        import getpass
        password = getpass.getpass("Password for {}@{}: ".format(user, server))
    return server, user, password


class VCenterClient:
    def __init__(
        self,
        server: str,
        user: str,
        password: str,
        insecure: bool = False,
        timeout: int = 60,
        log: Optional[Callable[[str], None]] = None,
    ):
        raw = (server or "").strip().rstrip("/")
        # An explicit scheme is honoured; anything else is assumed to be HTTPS.
        self.scheme = "http" if raw.lower().startswith("http://") else "https"
        self.server = raw.split("://", 1)[-1]
        self._user = user
        self._password = password
        self.insecure = insecure
        self.timeout = timeout
        self._log = log or (lambda msg: None)
        self._token: Optional[str] = None
        self._prefix = "/api"
        self._lock = threading.Lock()
        self._ctx = ssl.create_default_context()
        if insecure:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------- session
    def __enter__(self) -> "VCenterClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        if self.scheme == "http":
            self._log("warning: talking to vCenter over plain HTTP -- "
                      "credentials are not encrypted in transit")
        basic = base64.b64encode(
            "{}:{}".format(self._user, self._password).encode("utf-8")).decode("ascii")
        headers = {"Authorization": "Basic {}".format(basic)}
        for prefix, path in (("/api", "/api/session"),
                            ("/rest", "/rest/com/vmware/cis/session")):
            try:
                body = self._request("POST", path, headers=headers, raw=True)
            except VCenterAuthError:
                raise
            except VCenterError:
                continue
            try:
                token = json.loads(body)
            except json.JSONDecodeError:
                token = body.strip().strip('"')
            if isinstance(token, dict):
                token = token.get("value")
            if token:
                self._token = str(token)
                self._prefix = prefix
                self._log("connected to {} ({} endpoints)".format(self.server, prefix))
                return
        raise VCenterError("could not establish a session with {}".format(self.server))

    def close(self) -> None:
        if not self._token:
            return
        try:
            path = "/api/session" if self._prefix == "/api" else "/rest/com/vmware/cis/session"
            self._request("DELETE", path, raw=True)
        except VCenterError:
            pass
        finally:
            self._token = None
            self._password = ""

    # ------------------------------------------------------------ transport
    def _request(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        raw: bool = False,
        body: Any = None,
    ) -> Any:
        url = "{}://{}{}".format(self.scheme, self.server, path)
        hdrs = {"Accept": "application/json", "Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if self._token:
            hdrs["vmware-api-session-id"] = self._token
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, method=method, headers=hdrs, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            if exc.code in (401, 403):
                raise VCenterAuthError(
                    "vCenter rejected the credentials ({}): {}".format(exc.code, detail)) from exc
            raise VCenterError("{} {} -> HTTP {}: {}".format(method, path, exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            hint = ""
            if isinstance(reason, ssl.SSLCertVerificationError):
                hint = "  (self-signed certificate? re-run with --insecure)"
            raise VCenterError("cannot reach {}: {}{}".format(self.server, reason, hint)) from exc

        if raw:
            return body
        if not body.strip():
            return None
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise VCenterError("{} returned non-JSON: {}".format(path, body[:200])) from exc
        # The legacy /rest endpoints wrap everything in {"value": ...}
        if isinstance(data, dict) and set(data) == {"value"}:
            return data["value"]
        return data

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        full = self._prefix + path
        if params:
            pairs: List[tuple] = []
            for key, val in params.items():
                if val is None:
                    continue
                if isinstance(val, (list, tuple, set)):
                    for item in val:
                        pairs.append((key, item))
                else:
                    pairs.append((key, val))
            if pairs:
                full += "?" + urllib.parse.urlencode(pairs)
        return self._request("GET", full)

    # -------------------------------------------------------------- queries
    def list_vms(self, power_state: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if power_state:
            params["power_states"] = power_state
        try:
            vms = self.get("/vcenter/vm", params or None)
        except VCenterError as exc:
            if "too many" in str(exc).lower() or "unable_to_allocate_resource" in str(exc).lower():
                self._log("  vCenter refused a single full listing; enumerating per folder")
                return self._list_vms_by_folder()
            raise
        return list(vms or [])

    def _list_vms_by_folder(self) -> List[Dict[str, Any]]:
        seen: Dict[str, Dict[str, Any]] = {}
        for folder in self.folders():
            fid = folder.get("folder")
            if not fid:
                continue
            try:
                for vm in self.get("/vcenter/vm", {"folders": fid}) or []:
                    seen[vm["vm"]] = vm
            except VCenterError:
                continue
        return list(seen.values())

    def folders(self) -> List[Dict[str, Any]]:
        try:
            return list(self.get("/vcenter/folder", {"type": "VIRTUAL_MACHINE"}) or [])
        except VCenterError:
            return []

    def datacenters(self) -> List[Dict[str, Any]]:
        try:
            return list(self.get("/vcenter/datacenter") or [])
        except VCenterError:
            return []

    def datacenter_detail(self, dc_id: str) -> Dict[str, Any]:
        try:
            return self.get("/vcenter/datacenter/{}".format(urllib.parse.quote(dc_id))) or {}
        except VCenterError:
            return {}

    def child_folders(self, parent_id: str) -> List[Dict[str, Any]]:
        try:
            return list(self.get("/vcenter/folder", {
                "type": "VIRTUAL_MACHINE", "parent_folders": parent_id}) or [])
        except VCenterError:
            return []

    def folder_tree(self) -> List["FolderNode"]:
        """Every VM folder with its full path, walked from each datacenter's root.

        vCenter's folder listing is flat and carries no parent, so the tree is
        rebuilt by asking each folder for its children. One call per folder;
        a few hundred folders is a few hundred quick calls.
        """
        nodes: List[FolderNode] = []
        for dc in self.datacenters():
            dc_id = dc.get("datacenter", "")
            dc_name = dc.get("name", dc_id)
            root_id = (self.datacenter_detail(dc_id) or {}).get("vm_folder")
            if not root_id:
                continue
            # The datacenter's own "vm" folder holds VMs too; it is the "" path.
            nodes.append(FolderNode(folder_id=root_id, path="", datacenter=dc_name, root=True))
            queue: List[Tuple[str, str]] = [(root_id, "")]
            seen = {root_id}
            while queue:
                parent_id, parent_path = queue.pop(0)
                for child in self.child_folders(parent_id):
                    cid = child.get("folder", "")
                    if not cid or cid in seen:
                        continue
                    seen.add(cid)
                    path = "{}/{}".format(parent_path, child.get("name", cid)) if parent_path \
                        else child.get("name", cid)
                    nodes.append(FolderNode(folder_id=cid, path=path, datacenter=dc_name))
                    queue.append((cid, path))
        if nodes:
            return nodes
        # Fallback for an API that will not give us the datacenter root: flat names.
        return [FolderNode(folder_id=f.get("folder", ""), path=f.get("name", ""), datacenter="")
                for f in self.folders() if f.get("folder")]

    def clusters(self) -> List[Dict[str, Any]]:
        try:
            return list(self.get("/vcenter/cluster") or [])
        except VCenterError:
            return []

    def networks(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        try:
            for net in self.get("/vcenter/network") or []:
                if net.get("network"):
                    out[net["network"]] = net.get("name", "")
        except VCenterError:
            pass
        return out

    def hosts(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        try:
            for host in self.get("/vcenter/host") or []:
                if host.get("host"):
                    out[host["host"]] = host.get("name", "")
        except VCenterError:
            pass
        return out

    def vms_in(self, kind: str, ident: str) -> List[str]:
        """morefs of VMs in a folder or cluster ('folders' / 'clusters')."""
        try:
            return [vm["vm"] for vm in self.get("/vcenter/vm", {kind: ident}) or []]
        except VCenterError:
            return []

    def vm_detail(self, moref: str) -> Optional[Dict[str, Any]]:
        try:
            return self.get("/vcenter/vm/{}".format(urllib.parse.quote(moref)))
        except VCenterError:
            return None

    def guest_ip(self, moref: str) -> str:
        """The guest's primary IP, as VMware Tools reports it ("" if none)."""
        try:
            data = self.get("/vcenter/vm/{}/guest/identity".format(urllib.parse.quote(moref)))
        except VCenterError:
            return ""
        return str((data or {}).get("ip_address") or "") if isinstance(data, dict) else ""

    def attached_tags(self, morefs: List[str], log: Optional[Callable[[str], None]] = None) -> Dict[str, List[str]]:
        """moref -> ["Category:Tag", ...], in a few batched calls, not one per VM.

        Uses the vSphere Automation tagging API. Any failure (an older vCenter,
        no tagging privilege) returns what was gathered and says so; tags are
        an optional extra, never a reason for discovery to fail.
        """
        say = log or (lambda msg: None)
        out: Dict[str, List[str]] = {}
        if self._prefix != "/api" or not morefs:
            return out
        tag_names: Dict[str, str] = {}
        categories: Dict[str, str] = {}
        try:
            for i in range(0, len(morefs), 500):
                chunk = morefs[i:i + 500]
                res = self._request(
                    "POST", "/api/cis/tagging/tag-association?action=list-attached-tags-on-objects",
                    body={"object_ids": [{"type": "VirtualMachine", "id": m} for m in chunk]}) or []
                for item in res:
                    oid = ((item or {}).get("object_id") or {}).get("id")
                    if oid:
                        out[oid] = list(item.get("tag_ids") or [])
            for tid in sorted({t for ids in out.values() for t in ids}):
                tag = self.get("/cis/tagging/tag/{}".format(urllib.parse.quote(tid))) or {}
                cid = tag.get("category_id", "")
                if cid and cid not in categories:
                    categories[cid] = (self.get("/cis/tagging/category/{}".format(
                        urllib.parse.quote(cid))) or {}).get("name", cid)
                tag_names[tid] = "{}:{}".format(categories.get(cid, "?"), tag.get("name", tid))
        except VCenterError as exc:
            say("  tags not read ({}); continuing without them".format(str(exc)[:160]))
            return {}
        return {m: sorted(tag_names.get(t, t) for t in ids) for m, ids in out.items()}

    def vm_tools(self, moref: str) -> str:
        try:
            data = self.get("/vcenter/vm/{}/tools".format(urllib.parse.quote(moref)))
        except VCenterError:
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("run_state") or data.get("version_status") or "")


def _parse_nics(detail: Dict[str, Any], networks: Dict[str, str]) -> List[DiscoveredNic]:
    """VM detail reports NICs keyed by their device key -- exactly what we need."""
    nics: List[DiscoveredNic] = []
    raw = (detail or {}).get("nics")
    entries: List[tuple] = []
    if isinstance(raw, dict):
        entries = list(raw.items())
    elif isinstance(raw, list):
        # /rest style: [{"key": "4000", "value": {...}}]
        for item in raw:
            if isinstance(item, dict) and "key" in item:
                entries.append((item["key"], item.get("value", {})))
    for key, value in entries:
        if not isinstance(value, dict):
            continue
        backing = value.get("backing") or {}
        net_id = backing.get("network") or ""
        try:
            device_key = int(str(key))
        except ValueError:
            continue
        nics.append(
            DiscoveredNic(
                device_key=device_key,
                network_id=net_id,
                network_name=backing.get("network_name") or networks.get(net_id, "") or net_id,
                mac=value.get("mac_address") or "",
                backing=backing.get("type") or "",
            )
        )
    return sorted(nics, key=lambda n: n.device_key)


def _facts(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Readiness facts from the VM detail already fetched (no extra calls)."""
    def entries(key: str) -> List[Dict[str, Any]]:
        raw = (detail or {}).get(key)
        if isinstance(raw, dict):
            return [v for v in raw.values() if isinstance(v, dict)]
        if isinstance(raw, list):
            return [i.get("value", i) for i in raw if isinstance(i, dict)]
        return []
    cdroms = entries("cdroms")
    return {
        "hw_version": ((detail or {}).get("hardware") or {}).get("version", ""),
        "disk_backings": [((d.get("backing") or {}).get("type") or "") for d in entries("disks")],
        "iso_connected": any((c.get("backing") or {}).get("type") == "ISO_FILE"
                             and c.get("state", "CONNECTED") == "CONNECTED" for c in cdroms),
        "nic_states": [n.get("state", "") for n in entries("nics")],
    }


def discover(
    client: VCenterClient,
    *,
    power_state: Optional[str] = None,
    with_tools: bool = False,
    with_placement: bool = True,
    with_tags: bool = True,
    concurrency: int = 12,
    progress: Optional[Callable[[int, int], None]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> List[DiscoveredVm]:
    """Pull the full VM inventory. One list call, then details in parallel."""
    say = log or (lambda msg: None)

    say("listing virtual machines...")
    listing = client.list_vms(power_state=power_state)
    say("  {} VM(s) returned".format(len(listing)))
    if not listing:
        return []

    networks = client.networks()
    say("  {} network(s) resolved".format(len(networks)))

    folder_of: Dict[str, str] = {}
    dc_of: Dict[str, str] = {}
    cluster_of: Dict[str, str] = {}
    if with_placement:
        tree = client.folder_tree()
        for node in tree:
            # The `folders` filter returns a folder's direct members only, so
            # walking every node gives each VM exactly one path.
            for moref in client.vms_in("folders", node.folder_id):
                folder_of[moref] = node.path
                dc_of[moref] = node.datacenter
        clusters = client.clusters()
        for cluster in clusters:
            name = cluster.get("name", "")
            for moref in client.vms_in("clusters", cluster.get("cluster", "")):
                cluster_of[moref] = name
        say("  placement resolved: {} folder(s) across {} datacenter(s), {} cluster(s)".format(
            sum(1 for n in tree if not n.root), len({n.datacenter for n in tree}), len(clusters)))

    hosts = client.hosts()
    total = len(listing)
    done = 0
    lock = threading.Lock()
    results: List[DiscoveredVm] = []

    def fetch(entry: Dict[str, Any]) -> Optional[DiscoveredVm]:
        nonlocal done
        moref = entry.get("vm", "")
        detail = client.vm_detail(moref) or {}
        vm = DiscoveredVm(
            moref=moref,
            name=entry.get("name") or detail.get("name") or moref,
            power_state=entry.get("power_state") or detail.get("power_state") or "",
            cpu_count=int((detail.get("cpu") or {}).get("count") or entry.get("cpu_count") or 0),
            memory_mb=int((detail.get("memory") or {}).get("size_MiB")
                          or entry.get("memory_size_MiB") or 0),
            folder=folder_of.get(moref, ""),
            datacenter=dc_of.get(moref, ""),
            cluster=cluster_of.get(moref, ""),
            host=hosts.get(detail.get("host", ""), ""),
            guest_os=detail.get("guest_OS") or "",
            nics=_parse_nics(detail, networks),
            facts=_facts(detail),
        )
        if with_tools:
            vm.tools_status = client.vm_tools(moref)
            if "running" in vm.tools_status.lower() and "not" not in vm.tools_status.lower():
                vm.ip = client.guest_ip(moref)
        with lock:
            done += 1
            if progress:
                progress(done, total)
        return vm

    workers = max(1, min(int(concurrency), 32))
    say("  reading VM details with {} worker(s)...".format(workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for vm in pool.map(fetch, listing):
            if vm is not None:
                results.append(vm)

    if with_tags and results:
        say("  reading vCenter tags...")
        tags = client.attached_tags([v.moref for v in results], log=say)
        for vm in results:
            vm.tags = tags.get(vm.moref, [])
        if tags:
            say("  {} VM(s) carry tags".format(sum(1 for v in results if v.tags)))
    results.sort(key=lambda v: (v.name or "").lower())
    return results
