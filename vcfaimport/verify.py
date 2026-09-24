"""Verify imported VMs: committed is not the same as working.

For each VM, the checks that apply:
  powered on          vCenter says the VM is powered on
  VMware Tools        vCenter says Tools is running
  IP preserved        the guest IP now equals the IP recorded at discovery
  ping                the guest answers ICMP from this machine
  tcp <port>          a TCP connection to each configured port succeeds

A check is ok, fail, or skip (not applicable, or no data to check with).
Verdict: fail if any check failed; warn if nothing could be checked; else ok.
A verification never changes a VM's state -- committed stays committed -- it
is recorded next to it, with its history, and as an event.
"""

from __future__ import annotations

import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

OK, FAIL, SKIP = "ok", "fail", "skip"
Check = Dict[str, str]


def ping(ip: str, timeout: int) -> Tuple[str, str]:
    """One ICMP echo. Windows `ping` exits 0 even for "Destination host
    unreachable" (the reply came from a router), so success there means a TTL."""
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(max(1, timeout) * 1000), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout)), ip]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5,
                              errors="replace")
    except FileNotFoundError:
        return SKIP, "no ping command on this machine"
    except subprocess.TimeoutExpired:
        return FAIL, "no reply within {}s".format(timeout)
    out = proc.stdout or ""
    replied = ("TTL=" in out.upper()) if os.name == "nt" else proc.returncode == 0
    return (OK, "replied") if replied else (FAIL, "no reply within {}s".format(timeout))


def tcp(ip: str, port: int, timeout: int) -> Tuple[str, str]:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return OK, "open"
    except socket.timeout:
        return FAIL, "timed out"
    except OSError as exc:
        return FAIL, (exc.strerror or str(exc))[:80]


def verify_one(row: Any, cfg: Any, client: Any = None) -> Tuple[str, List[Check]]:
    checks: List[Check] = []
    moref, src_ip = row["moref"], (row["src_ip"] or "").strip()
    add = lambda name, status, detail: checks.append({"check": name, "status": status, "detail": detail})  # noqa: E731
    current_ip = ""
    if client is not None:
        detail = client.vm_detail(moref)
        if detail is None:
            add("powered on", FAIL, "vCenter no longer knows this VM")
        else:
            power = str(detail.get("power_state") or "")
            add("powered on", OK if power == "POWERED_ON" else FAIL, power or "unknown")
            tools = client.vm_tools(moref)
            running = "running" in tools.lower() and "not" not in tools.lower()
            add("VMware Tools", OK if running else FAIL, tools or "unknown")
            current_ip = client.guest_ip(moref) if running else ""
            if src_ip and current_ip:
                add("IP preserved", OK if current_ip == src_ip else FAIL,
                    current_ip if current_ip == src_ip else "{} (was {})".format(current_ip, src_ip))
            else:
                add("IP preserved", SKIP, "no IP to compare" if not src_ip else "guest reports no IP")
    else:
        add("vCenter checks", SKIP, "no vCenter session: sign in on Discover, or set VCFA_VC_*")
    ip = current_ip or src_ip
    timeout = int(getattr(cfg, "verify_timeout_seconds", 2) or 2)
    if getattr(cfg, "verify_ping", True):
        if ip:
            status, text = ping(ip, timeout)
            add("ping " + ip, status, text)
        else:
            add("ping", SKIP, "no IP known")
    for port in getattr(cfg, "verify_ports", []) or []:
        if ip:
            status, text = tcp(ip, int(port), timeout)
            add("tcp {}".format(port), status, text)
        else:
            add("tcp {}".format(port), SKIP, "no IP known")
    statuses = [c["status"] for c in checks]
    if FAIL in statuses:
        verdict = "fail"
    elif OK not in statuses:
        verdict = "warn"
    else:
        verdict = "ok"
    return verdict, checks


def verify_many(rows: Sequence[Any], cfg: Any, client: Any = None,
                progress: Optional[Callable[[int, int], None]] = None,
                concurrency: int = 16) -> List[Tuple[str, str, List[Check]]]:
    """(moref, verdict, checks) for each row, checked in parallel."""
    rows = list(rows)
    done = [0]

    def one(row):
        try:
            verdict, checks = verify_one(row, cfg, client)
        except Exception as exc:  # noqa: BLE001 -- one VM's surprise must not stop the rest
            verdict, checks = "fail", [{"check": "verification", "status": FAIL, "detail": str(exc)[:200]}]
        done[0] += 1
        if progress:
            progress(done[0], len(rows))
        return row["moref"], verdict, checks

    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, 64))) as pool:
        return list(pool.map(one, rows))
