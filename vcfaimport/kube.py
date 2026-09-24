"""Thin kubectl wrapper.

Shelling out to kubectl (rather than talking to the API directly) means the
tool inherits whatever authentication the operator already has working for the
Supervisor -- the vSphere kubectl plugin, an exec credential plugin, an OIDC
token, whatever. There is no second auth path to keep alive.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence

from .config import Config

# Errors worth another attempt: the API server behind a Supervisor is not
# always reachable during control-plane rollouts or VC certificate refreshes.
TRANSIENT_RE = re.compile(
    r"(connection refused|connection reset|i/o timeout|TLS handshake timeout|"
    r"etcdserver: request timed out|too many requests|429|"
    r"unexpected EOF|EOF$|the server is currently unable to handle the request|"
    r"context deadline exceeded|no route to host|temporarily unavailable)",
    re.IGNORECASE,
)


class KubectlError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.args_list = list(args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = (stderr or stdout or "").strip()
        super().__init__("kubectl {} failed (exit {}): {}".format(
            " ".join(args[1:4]), returncode, detail[:800]))

    @property
    def is_not_found(self) -> bool:
        return "NotFound" in self.stderr or "not found" in self.stderr.lower()

    @property
    def is_already_exists(self) -> bool:
        return "AlreadyExists" in self.stderr or "already exists" in self.stderr.lower()

    @property
    def is_transient(self) -> bool:
        return bool(TRANSIENT_RE.search(self.stderr or ""))


class Kubectl:
    def __init__(self, cfg: Config, dry_run: bool = False, verbose: bool = False, log=None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.verbose = verbose
        self._log = log or (lambda msg: None)
        self._resource_cache: Optional[Dict[str, str]] = None

    # ---------------------------------------------------------------- core
    def _kubectl_argv(self) -> List[str]:
        """Allow `kubectl` to be a wrapper command, not just a binary path.

        e.g. kubectl = "kubectl --request-timeout=30s" or a site-specific
        login wrapper. Splitting is Windows-safe (backslashes preserved).
        """
        raw = self.cfg.kubectl
        if " " not in raw.strip():
            return [raw]
        parts = shlex.split(raw, posix=False)
        return [p.strip('"').strip("'") for p in parts if p]

    def _base(self) -> List[str]:
        args = list(self._kubectl_argv())
        if self.cfg.kubeconfig:
            args += ["--kubeconfig", self.cfg.kubeconfig]
        if self.cfg.context:
            args += ["--context", self.cfg.context]
        return args

    def run(
        self,
        args: Sequence[str],
        stdin: Optional[str] = None,
        check: bool = True,
        timeout: int = 180,
        retries: int = 2,
    ) -> subprocess.CompletedProcess:
        cmd = self._base() + list(args)
        if self.verbose:
            self._log("$ " + " ".join(cmd))
        attempt = 0
        while True:
            attempt += 1
            try:
                proc = subprocess.run(
                    cmd,
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    encoding="utf-8",
                    errors="replace",
                )
            except FileNotFoundError as exc:
                raise KubectlError(cmd, 127, "", "kubectl not found on PATH: {}".format(exc)) from exc
            except subprocess.TimeoutExpired as exc:
                if attempt <= retries:
                    time.sleep(min(2 ** attempt, 15))
                    continue
                raise KubectlError(cmd, 124, "", "timed out after {}s".format(timeout)) from exc

            if proc.returncode == 0 or not check:
                return proc
            err = KubectlError(cmd, proc.returncode, proc.stdout, proc.stderr)
            if err.is_transient and attempt <= retries:
                self._log("  transient kubectl error, retry {}/{}: {}".format(
                    attempt, retries, (proc.stderr or "").strip()[:160]))
                time.sleep(min(2 ** attempt, 15))
                continue
            raise err

    # ------------------------------------------------------------- queries
    def preflight(self) -> Dict[str, Any]:
        """Confirm kubectl works, we are pointed somewhere, and the CRDs exist."""
        info: Dict[str, Any] = {}
        exe = self._kubectl_argv()[0]
        if shutil.which(exe) is None and "/" not in exe and "\\" not in exe:
            raise KubectlError([exe], 127, "", "kubectl not found on PATH")
        proc = self.run(["config", "current-context"], check=False, timeout=30)
        info["context"] = self.cfg.context or proc.stdout.strip() or "(none)"
        ver = self.run(["version", "-o", "json"], check=False, timeout=60)
        try:
            info["server_version"] = json.loads(ver.stdout).get("serverVersion", {}).get("gitVersion")
        except (json.JSONDecodeError, AttributeError):
            info["server_version"] = None
        info["resources"] = self.api_resources()
        return info

    def _probe(self, args: Sequence[str], timeout: int = 60, attempts: int = 3):
        """A read-only query whose failure is an answer ("no such thing"), retried
        while the failure is only the API server not answering."""
        proc = None
        for attempt in range(1, attempts + 1):
            proc = self.run(args, check=False, timeout=timeout)
            if proc.returncode == 0 or not TRANSIENT_RE.search(proc.stderr or ""):
                return proc
            if attempt < attempts:
                self._log("  transient kubectl error, retry {}/{}: {}".format(
                    attempt, attempts - 1, (proc.stderr or "").strip()[:160]))
                time.sleep(min(2 ** attempt, 8))
        return proc

    @staticmethod
    def _unanswered(proc) -> bool:
        return proc.returncode != 0 and bool(TRANSIENT_RE.search(proc.stderr or ""))

    def api_resources(self) -> Dict[str, str]:
        """Map resource name -> kind for the mobility-operator API group.

        A failed listing is never cached as "no resources": one API-server
        timeout would otherwise make a whole run believe the ImportOperation
        CRD is missing. kubectl often exits non-zero because some *other* API
        group is down while still listing ours, so output wins over exit code.
        """
        if self._resource_cache is not None:
            return self._resource_cache
        args = ["api-resources", "--api-group", self.cfg.api_group, "--no-headers"]
        proc = self._probe(args)
        found: Dict[str, str] = {}
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                found[parts[0]] = parts[-1]
        if not found and proc.returncode != 0:
            raise KubectlError(self._base() + args, proc.returncode, proc.stdout, proc.stderr)
        self._resource_cache = found
        return found

    def get_json(
        self,
        resource: str,
        name: Optional[str] = None,
        namespace: Optional[str] = None,
        selector: Optional[str] = None,
        check: bool = True,
        all_namespaces: bool = False,
    ) -> Optional[Dict[str, Any]]:
        args = ["get", resource]
        if name:
            args.append(name)
        if all_namespaces:
            args.append("-A")
        elif namespace:
            args += ["-n", namespace]
        if selector:
            args += ["-l", selector]
        args += ["-o", "json"]
        try:
            proc = self.run(args, check=True, timeout=120)
        except KubectlError as err:
            if err.is_not_found and not check:
                return None
            raise
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None

    def exists(self, resource: str, name: str, namespace: Optional[str] = None) -> Optional[bool]:
        """True or False -- or None when the API server never answered, which is
        not the same as "not found" and must not be reported as missing."""
        args = ["get", resource, name, "-o", "name"]
        if namespace:
            args += ["-n", namespace]
        proc = self._probe(args)
        if proc.returncode == 0:
            return True
        return None if self._unanswered(proc) else False

    def can_i(self, verb: str, resource: str, namespace: Optional[str] = None) -> Optional[bool]:
        """True / False, or None when the API server never answered."""
        args = ["auth", "can-i", verb, resource]
        if namespace:
            args += ["-n", namespace]
        proc = self._probe(args)
        answer = proc.stdout.strip().lower()
        if answer.startswith("yes"):
            return True
        if not answer and self._unanswered(proc):
            return None
        return False

    # ------------------------------------------------------------ mutations
    def apply(self, manifest_yaml: str, namespace: str) -> str:
        if self.dry_run:
            self._log("  [dry-run] would apply manifest to namespace {}".format(namespace))
            return "dry-run"
        proc = self.run(["apply", "-n", namespace, "-f", "-"], stdin=manifest_yaml, timeout=180)
        return proc.stdout.strip()

    def server_dry_run(self, manifest_yaml: str, namespace: str) -> str:
        """Server-side validation without persisting anything."""
        proc = self.run(
            ["apply", "-n", namespace, "-f", "-", "--dry-run=server"],
            stdin=manifest_yaml,
            timeout=180,
        )
        return proc.stdout.strip()

    def patch(
        self,
        resource: str,
        name: str,
        namespace: str,
        patch: Dict[str, Any],
        patch_type: str = "merge",
    ) -> str:
        body = json.dumps(patch)
        if self.dry_run:
            self._log("  [dry-run] would patch {}/{} in {} with {}".format(resource, name, namespace, body))
            return "dry-run"
        proc = self.run(
            ["patch", resource, name, "-n", namespace, "--type", patch_type, "-p", body],
            timeout=120,
        )
        return proc.stdout.strip()

    def delete(self, resource: str, name: str, namespace: str, ignore_missing: bool = True) -> str:
        if self.dry_run:
            self._log("  [dry-run] would delete {}/{} in {}".format(resource, name, namespace))
            return "dry-run"
        args = ["delete", resource, name, "-n", namespace]
        if ignore_missing:
            args.append("--ignore-not-found")
        proc = self.run(args, check=False, timeout=180)
        return proc.stdout.strip()
