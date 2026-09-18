#!/usr/bin/env python3
"""Run a complete vcfa-import campaign against simulated infrastructure.

Nothing real is touched: tools/fake_vcenter.py stands in for vCenter and
tools/fake_kubectl.py for kubectl plus the Mobility Operator. Use it to
rehearse the workflow, to see what the output looks like before running it for
real, or to demonstrate the tool to a change board.

    python tools/demo.py                    # 60 VMs, some failures injected
    python tools/demo.py --vms 200 --scenario happy
    python tools/demo.py --keep ./rehearsal # keep the workspace afterwards
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import fake_vcenter  # noqa: E402

STEP = 0


def banner(title: str, why: str = "") -> None:
    global STEP
    STEP += 1
    print("\n" + "=" * 78)
    print("  STEP {}  {}".format(STEP, title))
    if why:
        print("  {}".format(why))
    print("=" * 78)


class Demo:
    def __init__(self, workdir: Path, env: dict):
        self.workdir = workdir
        self.env = env

    def run(self, *args: str, expect=None, quiet: bool = False,
            silent: bool = False) -> str:
        """quiet: show the command but not its output. silent: show neither."""
        cmd = [sys.executable, str(ROOT / "vcfa-import.py"), "-c", "cfg.toml"] + list(args)
        if not silent:
            print("\n$ vcfa-import {}".format(" ".join(args)))
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=self.env, cwd=str(self.workdir), timeout=900)
        out = (proc.stdout or "") + (proc.stderr or "")
        if not (quiet or silent):
            print(out.rstrip())
        if expect is not None and proc.returncode != expect:
            print("!! expected exit {}, got {}".format(expect, proc.returncode))
        elif proc.returncode not in (0, expect if expect is not None else 0):
            print("   (exit {})".format(proc.returncode))
        return out


def setup(workdir: Path, port: int, scenario: str, batch_size: int) -> dict:
    namespaces = ["prod-web-ns1", "prod-db-ns2", "dmz-ns3"]
    subnets = []
    for ns in namespaces:
        for sub in ("subnet-vlan197", "subnet-vlan200", "subnet-vlan210", "subnet-dmz"):
            subnets.append("{}/{}".format(ns, sub))
    (workdir / "cluster.json").write_text(json.dumps({
        "namespaces": namespaces, "subnets": subnets,
        "batches": {}, "polls": {}, "applies": 0,
    }), encoding="utf-8")

    (workdir / "folder-map.csv").write_text(
        "folder,namespace,wave\n"
        "Production,prod-web-ns1,1\n"
        "Production/App,prod-web-ns1,2\n"
        "Databases,prod-db-ns2,1\n"
        "DMZ,dmz-ns3,2\n", encoding="utf-8")

    # The portgroup map only supplies subnets now; namespace/wave come from folders.
    (workdir / "portgroup-map.csv").write_text(
        "portgroup,namespace,subnet\n"
        "VLAN197-Prod,,subnet-vlan197\n"
        "VLAN200-DB,,subnet-vlan200\n"
        "VLAN210-App,,subnet-vlan210\n"
        "DMZ-Uplink,,subnet-dmz\n", encoding="utf-8")

    (workdir / "cfg.toml").write_text("\n".join([
        '# Demo configuration -- kubectl and vCenter are both simulated.',
        'kubectl = "{} {}"'.format(Path(sys.executable).as_posix(),
                                   (ROOT / "tools" / "fake_kubectl.py").as_posix()),
        'context = "supervisor-demo-01"',
        'poll_interval_seconds = 1',
        'settle_seconds = 0',
        'batch_size = {}'.format(batch_size),
        'max_parallel_batches = 4',
        'max_parallel_batches_per_namespace = 2',
        'failure_rate_abort = 0.9   # relaxed so the demo can show failures and retries',
        'workdir = "run"',
    ]) + "\n", encoding="utf-8")

    return dict(
        os.environ,
        FAKE_KUBECTL_STATE=str(workdir / "cluster.json"),
        FAKE_KUBECTL_SCENARIO=scenario,
        FAKE_KUBECTL_SETTLE="1",
        VCFA_VC_SERVER="http://127.0.0.1:{}".format(port),
        VCFA_VC_USER=fake_vcenter.USER,
        VCFA_VC_PASSWORD=fake_vcenter.PASSWORD,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vms", type=int, default=60)
    ap.add_argument("--scenario", default="import-flaky",
                    choices=["happy", "flaky", "import-flaky"],
                    help="flaky: some VMs fail precheck; import-flaky: some fail during "
                         "import, so rollback is demonstrated (default)")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--keep", help="keep the demo workspace in this directory")
    args = ap.parse_args()

    workdir = Path(args.keep).expanduser().resolve() if args.keep else \
        Path(tempfile.mkdtemp(prefix="vcfa-demo-"))
    workdir.mkdir(parents=True, exist_ok=True)

    server, port = fake_vcenter.serve(count=args.vms)
    env = setup(workdir, port, args.scenario, args.batch_size)
    d = Demo(workdir, env)

    print("vcfa-import demonstration")
    print("  workspace     : {}".format(workdir))
    print("  fake vCenter  : http://127.0.0.1:{} with {} VMs".format(port, args.vms))
    print("  fake operator : scenario '{}'".format(args.scenario))
    print("  nothing real is contacted; no cluster or vCenter is required")

    try:
        banner("Discover the vCenter inventory",
               "One REST call lists the VMs; details are read in parallel.")
        d.run("discover")

        banner("Look around before choosing anything",
               "The folder tree is the map of the estate; facets list the other filterable values.")
        d.run("browse", "--folders")
        d.run("browse", "--facets")

        banner("Collect VMs by folder",
               "A folder path takes everything beneath it. `pick` writes a clickable list instead.")
        d.run("select", "--folder", "Production", "--powered-on", "--with-nics")
        d.run("select", "--folder", "Databases", "--folder", "DMZ", "--powered-on", "--with-nics")
        d.run("browse", "--folders")
        d.run("pick", "--out", "picker.html")

        banner("Stage the selection into the import queue",
               "The folder map decides namespace and wave (most specific folder wins); "
               "the portgroup map decides the subnet.")
        d.run("stage", "--folder-map", "folder-map.csv", "--map", "portgroup-map.csv")

        banner("Preflight the cluster",
               "Namespaces, subnets, CRDs and RBAC, before anything is applied.")
        d.run("preflight")

        banner("Inspect a generated manifest",
               "Exactly what would be applied, written to disk for review.")
        out = d.run("plan", "--stage", "precheck", "--show", quiet=True)
        head = out.split("---", 1)
        print(head[0].rstrip())
        if len(head) > 1:
            print("---" + head[1].rstrip()[:1400])

        banner("The precheck gate",
               "Importing is irreversible, so `run` refuses VMs that have not been prechecked.")
        d.run("run", "-y")

        banner("Dry run",
               "Renders and validates every batch, applies nothing, changes no state.")
        d.run("precheck", "-y", "--dry-run")

        banner("Precheck for real",
               "precheckOnly batches: the operator validates without migrating.")
        d.run("precheck", "-y")

        banner("Review what failed",
               "Failures are per VM, with the operator's own message.")
        d.run("vms", "--state", "precheck_failed", "--limit", "10")

        banner("Import wave 1",
               "Only VMs that passed a precheck; batches applied with a concurrency cap.")
        d.run("run", "-y", "--wave", "1")

        banner("Import wave 2")
        d.run("run", "-y", "--wave", "2")

        banner("Campaign status")
        d.run("status")

        if args.scenario == "import-flaky":
            banner("Roll back the imports that failed",
                   "Sets controlAction.rollbackAction: Immediate (as `kubectl edit` would), waits "
                   "for the operator to hand ownership back to vCenter, then deletes the batch.")
            d.run("vms", "--state", "failed", "--limit", "6")
            d.run("rollback", "--failed", "--delete", "-y")
            d.run("vms", "--state", "rolled_back", "--limit", "6")
            d.run("history", "--limit", "8")

        banner("The tracker: one VM's complete history",
               "Where it came from, where it went, and every step in between.")
        committed = json.loads(d.run("history", "--json", "--state", "committed",
                                     "--limit", "1", silent=True) or "[]")
        if committed:
            d.run("history", "--vm", committed[0]["moref"])

        banner("Retry the failures",
               "Failed and rolled-back VMs go back in the queue; the requeue is itself recorded.")
        d.run("retry")
        if args.scenario == "import-flaky":
            # The cause has been fixed in the meantime -- the retry lands.
            d.env["FAKE_KUBECTL_SCENARIO"] = "happy"
            d.run("precheck", "-y")
            d.run("run", "-y")
            d.run("status")
        else:
            d.run("history", "--limit", "6")

        banner("Export the record",
               "tracker.csv for a change ticket, transitions.csv for the full movement log.")
        d.run("ledger")
        d.run("ledger", "--verify")
        d.run("report", "--html", "report.html")

        print("\n" + "=" * 78)
        print("  Demo complete.")
        print("=" * 78)
        for name, desc in (
            ("report.html", "progress report, including the Moved into VCFA table"),
            ("picker.html", "the VM picker, openable in a browser"),
            ("run/tracker.csv", "per-VM tracker: source -> target, with timings"),
            ("run/transitions.csv", "every state change"),
            ("run/ledger.jsonl", "the append-only ledger"),
            ("run/manifests", "every ImportOperationBatch that was applied"),
        ):
            path = workdir / name
            if path.exists():
                print("  {:<22} {}".format(name, desc))
        print("\n  workspace: {}".format(workdir))
        if not args.keep:
            print("  (temporary; pass --keep <dir> to preserve it)")
        return 0
    finally:
        server.shutdown()
        if not args.keep:
            pass  # leave it for inspection; the OS will reclaim the temp dir


if __name__ == "__main__":
    sys.exit(main())
