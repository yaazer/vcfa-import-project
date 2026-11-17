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
    def __init__(self, message: str = "", status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status    # the HTTP status vCenter answered with; None if it never answered


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
                    "vCenter rejected the credentials ({}): {}".format(exc.code, detail), status=exc.code) from exc
            raise VCenterError("{} {} -> HTTP {}: {}".format(method, path, exc.code, detail), status=exc.code) from exc
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

    def network_list(self) -> List[Dict[str, str]]:
        """Every network vCenter knows: {id, name, type} (DISTRIBUTED_PORTGROUP, STANDARD_PORTGROUP, ...)."""
        out: List[Dict[str, str]] = []
        try:
            for net in self.get("/vcenter/network") or []:
                if net.get("network"):
                    out.append({"id": net["network"], "name": net.get("name", ""), "type": net.get("type", "")})
        except VCenterError:
            pass
        return out

    def networks(self) -> Dict[str, str]:
        return {n["id"]: n["name"] for n in self.network_list()}

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

    def vm_exists(self, moref: str) -> Optional[bool]:
        """True if vCenter knows this VM, False only when it answers 404 for it,
        None when it could not say (unreachable, timed out, refused): no answer
        is never read as "gone"."""
        try:
            self.get("/vcenter/vm/{}".format(urllib.parse.quote(moref)))
            return True
        except VCenterError as exc:
            return False if exc.status == 404 else None

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

    def namespace_list(self) -> Optional[List[Dict[str, str]]]:
        """Supervisor namespaces vCenter's own Workload Management already knows
        about -- independent of any Supervisor login or kubectl context. None
        when this vCenter could not be asked (pre-7.0 /rest only, no System.Read
        privilege, or no answer): never read as "there are none". An empty list
        is a real answer -- no namespace configured yet.

        Response shape (a bare array of Vcenter.Namespaces.Instances.Summary,
        fields cluster/namespace/description/config_status/stats/self_service_namespace)
        confirmed against VMware's own published OpenAPI spec (vmware/vcf-api-specs,
        specifications/vsphere/openapi/automation/vcenter.yaml, version 9.1.1.0,
        operationId Vcenter.Namespaces.Instances_list) -- not against a live vCenter."""
        if self._prefix != "/api":
            return None
        try:
            items = self.get("/vcenter/namespaces/instances")
        except VCenterError:
            return None
        if not isinstance(items, list):
            return None
        return [
            {"namespace": item.get("namespace", ""), "cluster": item.get("cluster", ""),
             "description": item.get("description", ""), "config_status": item.get("config_status", "")}
            for item in items if isinstance(item, dict) and item.get("namespace")
        ]

    def namespace_subnets(self, namespace: str) -> Optional[List[Dict[str, str]]]:
        """NSX Subnets and SubnetSets vCenter lists for one Supervisor namespace --
        independent of any Supervisor login or kubectl context. None when it could
        not be asked (older vCenter, no privilege, the namespace is unknown to
        vCenter, or no answer); an empty list is a real answer.

        Response shape confirmed against VMware's own published OpenAPI spec
        (vmware/vcf-api-specs, specifications/vsphere/openapi/automation/vcenter.yaml,
        version 9.1.1.0, operationId Vcenter.Namespaces.Networks.Nsx.Subnets_list) --
        not against a live vCenter. `type` is the entity's kind as an enum string,
        `SUBNET` or `SUBNETSET`; anything else is left blank rather than guessed,
        and service.cluster_subnets then treats a blank kind as "Subnet" (the common
        case) when merging the entry in. The item carries no `namespace` of its own
        -- it is namespace-scoped by the request, so it is supplied here instead."""
        if self._prefix != "/api":
            return None
        try:
            data = self.get("/vcenter/namespaces/{}/networks/nsx/subnets".format(
                urllib.parse.quote(namespace, safe="")))
        except VCenterError:
            return None
        items = (data or {}).get("subnets") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return None
        kind_of = {"SUBNET": "Subnet", "SUBNETSET": "SubnetSet"}
        out = []
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            out.append({
                "name": item["name"], "namespace": namespace,
                "kind": kind_of.get(str(item.get("type") or "").strip().upper(), ""),
            })
        return out

    def vm_storage_compliance(self, moref: str, live: bool = False) -> Optional[Dict[str, Any]]:
        """Storage-policy compliance of a VM's home directory and virtual disks.

        None only when this vCenter cannot be asked at all (pre-7.0 /rest-only
        session: no VM-level storage policy API exists there). Any other
        outcome is a dict and must be read with `storage_compliance_ok`, not
        compared to "COMPLIANT" by hand: {"overall": "NO_POLICY", "disks": {}}
        when the VM has no storage-policy association (nothing to be
        non-compliant with); {"overall": "ERROR", "status": <http status or
        None>, "error": <message>} when vCenter answered but refused or
        failed the call -- this must never be read as compliant, only as "not
        asked successfully"; otherwise {"overall": <COMPLIANT|NON_COMPLIANT|
        UNKNOWN_COMPLIANCE|NOT_APPLICABLE|OUT_OF_DATE>, "disks": {disk_id:
        {"status":..., "failure_cause":...}}, "vm_home_status":...,
        "vm_home_cause":...}.

        `live=True` asks vCenter to recompute compliance now (POST
        .../compliance?action=check) instead of returning its last cached
        value -- the only way to see a storage-policy edit made since the
        last discover (the suspected cause of the 2026-11-13 incident: see
        docs/incidents/2026-11-13-stuck-rollback-cns-unregister.md).

        Response shape (Vcenter.Vm.Storage.Policy.Compliance.Info /
        VmComplianceInfo) confirmed against VMware's own published OpenAPI
        spec (vmware/vcf-api-specs, specifications/vsphere/openapi/automation/
        vcenter.yaml, version 9.1.1.0, operationId Vcenter.Vm.Storage.Policy.
        Compliance_get / _check) -- not against a live vCenter."""
        if self._prefix != "/api":
            return None
        path = "/vcenter/vm/{}/storage/policy/compliance".format(urllib.parse.quote(moref))
        try:
            if live:
                data = self._request("POST", self._prefix + path + "?action=check", body=None)
            else:
                data = self.get(path)
        except VCenterError as exc:
            return {"overall": "ERROR", "status": exc.status, "error": str(exc)[:200]}
        if not isinstance(data, dict):
            return {"overall": "NO_POLICY", "disks": {}, "vm_home_status": ""}
        disks: Dict[str, Dict[str, str]] = {}
        for disk_id, info in (data.get("disks") or {}).items():
            if isinstance(info, dict):
                disks[disk_id] = {"status": info.get("status", ""),
                                  "failure_cause": _compliance_cause(info)}
        vm_home = data.get("vm_home") if isinstance(data.get("vm_home"), dict) else {}
        return {
            "overall": data.get("overall_compliance") or "",
            "disks": disks,
            "vm_home_status": vm_home.get("status", ""),
            "vm_home_cause": _compliance_cause(vm_home),
        }

    def vm_storage_policies(self, moref: str) -> Optional[Dict[str, Any]]:
        """Which storage policy the VM's home directory and each virtual disk is
        associated with -- what the compliance check above cannot tell: a disk can
        be perfectly COMPLIANT with a policy its target namespace does not offer.

        None only when this vCenter cannot be asked at all (pre-7.0 /rest-only
        session). {"error": <message>, "status": <http status or None>} when the
        call failed -- never read as "no policy", only as "not asked successfully".
        Otherwise {"vm_home": <policy id or "">, "disks": {disk_key: policy id}}.
        A disk with no policy is *absent* from `disks` (it is not listed with an
        empty value), so callers compare against the VM's own disk list
        (facts["disk_ids"]) to spot one.

        Response shape (Vcenter.Vm.Storage.Policy.Info: `vm_home` a policy id
        string, `disks` a map of disk key -> policy id string, unpolicied disks
        omitted) per the vmware.vmware_rest vcenter_vm_storage_policy_info
        documentation -- not against a live vCenter. The Spec-shaped
        {"type": ..., "policy": ...} form is tolerated too."""
        if self._prefix != "/api":
            return None
        try:
            data = self.get("/vcenter/vm/{}/storage/policy".format(urllib.parse.quote(moref)))
        except VCenterError as exc:
            return {"error": str(exc)[:200], "status": exc.status}

        def policy_of(value: Any) -> str:
            if isinstance(value, dict):
                value = value.get("policy")
            return str(value) if value else ""
        data = data if isinstance(data, dict) else {}
        raw = data.get("disks") if isinstance(data.get("disks"), dict) else {}
        disks = {str(k): policy_of(v) for k, v in raw.items() if policy_of(v)}
        return {"vm_home": policy_of(data.get("vm_home")), "disks": disks}

    def namespace_storage_policies(self, namespace: str) -> Optional[List[str]]:
        """Storage policy ids a Supervisor namespace offers (its storage_specs).

        None when it could not be asked (older vCenter, no privilege, the
        namespace is unknown to vCenter, or no answer): never read as "it offers
        none". An empty list is a real answer -- no storage assigned -- and blocks
        every VM targeting that namespace.

        Response shape (Vcenter.Namespaces.Instances.Info.storage_specs, a list of
        {policy, limit}) per govmomi's vapi/namespace NamespacesInstanceInfo /
        StorageSpec -- not against a live vCenter."""
        if self._prefix != "/api":
            return None
        try:
            data = self.get("/vcenter/namespaces/instances/{}".format(
                urllib.parse.quote(namespace, safe="")))
        except VCenterError:
            return None
        if not isinstance(data, dict):
            return None
        specs = data.get("storage_specs")
        if specs is None:
            return []
        if not isinstance(specs, list):
            return None
        return sorted({str(s["policy"]) for s in specs if isinstance(s, dict) and s.get("policy")})

    def storage_policy_names(self) -> Dict[str, str]:
        """{policy id: display name}, for messages only. {} when it could not be
        asked: ids are then shown as they are, nothing is decided on names."""
        if self._prefix != "/api":
            return {}
        try:
            items = self.get("/vcenter/storage/policies")
        except VCenterError:
            return {}
        if not isinstance(items, list):
            return {}
        return {str(i["policy"]): str(i.get("name") or i["policy"])
                for i in items if isinstance(i, dict) and i.get("policy")}


# Pass set for Vcenter.Vm.Storage.Policy.Compliance.Info.overall_compliance,
# plus the two sentinels vm_storage_compliance adds of its own ("NO_POLICY"
# for a null/empty response, "ERROR" for a failed request). UNKNOWN_COMPLIANCE
# and OUT_OF_DATE are deliberately NOT a pass: "unless compliant" (the user's
# own words, 2026-11-16) means a status that is not a known-good answer blocks
# the same as a known-bad one. OUT_OF_DATE is the state the 2026-11-13 incident
# suspects caused its stuck rollback (a policy edited mid-import, not applied).
# NO_POLICY is not a pass either (2026-11-17): a VM with no storage policy cannot
# be on one its target namespace offers, so it blocks like a wrong one.
STORAGE_COMPLIANCE_PASS = {"COMPLIANT", "NOT_APPLICABLE"}


def storage_compliance_ok(result: Optional[Dict[str, Any]]) -> bool:
    """False unless vCenter's own answer says this VM's storage is fine to
    import. None (never asked) and ERROR (asked, vCenter refused) both fail
    closed, the same as NON_COMPLIANT / OUT_OF_DATE / UNKNOWN_COMPLIANCE."""
    return bool(result) and result.get("overall") in STORAGE_COMPLIANCE_PASS


def storage_compliance_reason(result: Optional[Dict[str, Any]]) -> str:
    """One line explaining why `result` did or didn't pass, for holdback/log text."""
    if result is None:
        return "never checked (older vCenter, or discover ran with --no-storage-compliance)"
    overall = result.get("overall") or "?"
    if overall == "ERROR":
        return "vCenter's storage/policy/compliance call failed (HTTP {}): {}".format(
            result.get("status"), result.get("error") or "no detail")
    if overall == "NO_POLICY":
        return "no storage policy associated (assign one the target namespace offers)"
    causes = [result.get("vm_home_cause")] + [d.get("failure_cause") for d in (result.get("disks") or {}).values()]
    detail = "; ".join(c for c in causes if c)
    return "overall_compliance={}{}".format(overall, " ({})".format(detail) if detail else "")


def storage_compliance_checker(
    client: "VCenterClient", concurrency: int = 12
) -> Callable[[Sequence[str]], Dict[str, Optional[Dict[str, Any]]]]:
    """A moref -> live compliance-dict function, bound to one connected client.

    For `engine.py`'s pre-apply gate: it must not hold a vcenter.py import, so
    the CLI/web layer that does hold vCenter credentials builds this closure
    and hands it to the engine instead."""
    def check(morefs: Sequence[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        unique = list(dict.fromkeys(morefs))
        if not unique:
            return {}
        workers = max(1, min(int(concurrency), 32))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda m: client.vm_storage_compliance(m, live=True), unique))
        return dict(zip(unique, results))
    return check


def storage_policy_problems(attached: Optional[Dict[str, Any]], disk_ids: Sequence[str],
                            namespace: str, allowed: Optional[Sequence[str]],
                            names: Optional[Dict[str, str]] = None) -> List[str]:
    """Why a VM's storage policies do not fit its target namespace; [] when they do.

    `attached` is a vm_storage_policies() result, `disk_ids` the VM's own disk
    keys (a disk with no policy is missing from `attached`), `allowed` the policy
    ids `namespace` offers (namespace_storage_policies). Fails closed: never
    read, a failed read, an unknown namespace, a home or disk with no policy, and
    a policy the namespace does not offer all come back as a problem."""
    names = names or {}

    def label(pid: str) -> str:
        return "'{}'".format(names[pid]) if names.get(pid) else pid
    if attached is None:
        return ["its storage policies were never read (an older discover, or one run "
                "with --no-storage-compliance)"]
    if attached.get("error"):
        return ["vCenter's VM storage-policy call failed (HTTP {}): {}".format(
            attached.get("status"), attached.get("error"))]
    if not namespace:
        return ["it has no target namespace"]
    if allowed is None:
        return ["the storage policies namespace {} offers are unknown (vCenter did not list "
                "them at the last discover)".format(namespace)]
    allowed_set = set(allowed)
    problems: List[str] = []
    home = attached.get("vm_home") or ""
    if not home:
        problems.append("VM home has no storage policy")
    elif home not in allowed_set:
        problems.append("VM home uses {}".format(label(home)))
    disks = attached.get("disks") or {}
    for disk in sorted(set(str(d) for d in (disk_ids or [])) | set(disks)):
        pid = disks.get(disk) or ""
        if not pid:
            problems.append("disk {} has no storage policy".format(disk))
        elif pid not in allowed_set:
            problems.append("disk {} uses {}".format(disk, label(pid)))
    if problems:
        offers = ", ".join(sorted(label(p) for p in allowed_set)) or "none"
        problems.append("namespace {} offers: {}".format(namespace, offers))
    return problems


def storage_policy_checker(
    client: "VCenterClient", concurrency: int = 12
) -> Callable[[Sequence[str], Sequence[str]], Dict[str, Any]]:
    """(morefs, namespaces) -> live {"vms": {moref: vm_storage_policies()},
    "namespaces": {ns: namespace_storage_policies()}, "names": {id: name}},
    bound to one connected client -- the policy half of engine.py's pre-apply
    recheck, built by the CLI/web layer like storage_compliance_checker."""
    def check(morefs: Sequence[str], namespaces: Sequence[str]) -> Dict[str, Any]:
        unique = list(dict.fromkeys(morefs))
        spaces = sorted(set(n for n in namespaces if n))
        workers = max(1, min(int(concurrency), 32))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            vms = list(pool.map(client.vm_storage_policies, unique))
            allowed = list(pool.map(client.namespace_storage_policies, spaces))
        return {"vms": dict(zip(unique, vms)), "namespaces": dict(zip(spaces, allowed)),
                "names": client.storage_policy_names()}
    return check


def _compliance_cause(info: Dict[str, Any]) -> str:
    msgs = (info or {}).get("failure_cause") or []
    return "; ".join(
        m.get("default_message", "") for m in msgs if isinstance(m, dict) and m.get("default_message"))


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
    raw_disks = (detail or {}).get("disks")
    if isinstance(raw_disks, dict):
        disk_ids = [str(k) for k in raw_disks]
    elif isinstance(raw_disks, list):
        disk_ids = [str(i["key"]) for i in raw_disks if isinstance(i, dict) and "key" in i]
    else:
        disk_ids = []
    return {
        # every disk key, so a disk missing from vm_storage_policies (no policy) shows
        "disk_ids": disk_ids,
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
    with_namespaces: bool = True,
    with_subnets: bool = True,
    with_storage_compliance: bool = True,
    policy_namespaces: Sequence[str] = (),
    concurrency: int = 12,
    progress: Optional[Callable[[int, int], None]] = None,
    log: Optional[Callable[[str], None]] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> List[DiscoveredVm]:
    """Pull the full VM inventory. One list call, then details in parallel."""
    say = log or (lambda msg: None)

    say("listing virtual machines...")
    listing = client.list_vms(power_state=power_state)
    say("  {} VM(s) returned".format(len(listing)))
    if not listing:
        return []

    network_list = client.network_list()
    networks = {n["id"]: n["name"] for n in network_list}
    say("  {} network(s) resolved".format(len(networks)))
    if extras is not None:
        # Kept by the caller, so portgroup fields can suggest every portgroup --
        # including ones no discovered VM uses yet.
        extras["networks"] = [{"name": n["name"], "type": n["type"]} for n in network_list if n["name"]]
        if with_namespaces:
            ns_list = client.namespace_list()
            extras["namespaces"] = ns_list    # None = could not ask; [] = none configured
            if ns_list is not None:
                say("  {} Supervisor namespace(s) known to vCenter".format(len(ns_list)))
                if with_subnets and ns_list:
                    # Per namespace, not a single call: a namespace this failed for
                    # contributes nothing (unknown), never a guessed "it has none".
                    # A namespace that did answer, even with an empty list, is a real
                    # observation and is kept as such.
                    names = sorted({n["namespace"] for n in ns_list if n.get("namespace")})[:50]
                    workers = max(1, min(int(concurrency), 32))
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        per_ns = list(pool.map(client.namespace_subnets, names))
                    subnets: List[Dict[str, str]] = []
                    asked, answered = len(names), 0
                    for found in per_ns:
                        if found is not None:
                            answered += 1
                            subnets.extend(found)
                    # None when not one namespace could be asked (e.g. every call got a 403):
                    # that is "could not ask", not "there are none", and must not overwrite a
                    # previously good vc_subnets snapshot with an empty one (record_discovery
                    # only replaces it when this is not None).
                    extras["subnets"] = subnets if answered else None
                    if answered:
                        say("  {} NSX Subnet(s)/SubnetSet(s) known to vCenter across {} of {} namespace(s)".format(
                            len(subnets), answered, asked))
                    else:
                        say("  vCenter could not be asked for any namespace's subnet list")

    if extras is not None and with_storage_compliance:
        # Which storage policies each namespace offers: the namespaces vCenter
        # listed plus any the queue already targets (`policy_namespaces`), so a
        # login that cannot list namespaces still learns about the ones in use.
        # Uncapped, unlike subnets: a namespace left out here would block its VMs.
        listed = [n["namespace"] for n in (extras.get("namespaces") or []) if n.get("namespace")]
        names = sorted(set(listed) | set(n for n in policy_namespaces if n))
        if names:
            workers = max(1, min(int(concurrency), 32))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                per_ns = list(pool.map(client.namespace_storage_policies, names))
            answered = {ns: found for ns, found in zip(names, per_ns) if found is not None}
            # None = not one namespace could be asked: keep the last snapshot.
            extras["storage_policies"] = ({"namespaces": answered,
                                           "names": client.storage_policy_names()}
                                          if answered else None)
            if answered:
                say("  storage policies read for {} of {} namespace(s)".format(len(answered), len(names)))
            else:
                say("  vCenter could not be asked which storage policies any namespace offers")

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
        if with_storage_compliance:
            # Cached (GET), not live: a per-VM live recheck for every discovered
            # VM would be one more vCenter round trip each, for a snapshot that
            # is only ever a precheck/import-time gate's starting point -- the
            # gate itself re-checks live right before it matters (see engine.py).
            vm.facts["storage_compliance"] = client.vm_storage_compliance(moref)
            vm.facts["storage_policies"] = client.vm_storage_policies(moref)
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
