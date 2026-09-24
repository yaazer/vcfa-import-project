#!/usr/bin/env python3
"""Try the web console against simulated infrastructure.

Starts tools/fake_vcenter.py (a vCenter REST API with N VMs) and points the
console at tools/fake_kubectl.py (kubectl plus a simulated Mobility Operator),
then serves the console. Nothing real is contacted.

    python tools/web_demo.py                         # 120 VMs, some import failures
    python tools/web_demo.py --scenario happy --vms 300
    python tools/web_demo.py --keep ./webdemo        # keep the workspace between runs

In the browser: Discover (the server, user and password are pre-filled from the
environment), select a few folders, Map & Stage (the demo maps are already in
place), arrange Waves, then Execute: preflight, precheck, import.

Needs PyYAML (used by the fake kubectl only).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import demo  # noqa: E402
import fake_vcenter  # noqa: E402
from vcfaimport.config import Config  # noqa: E402
from vcfaimport.web.api import WebApp  # noqa: E402
from vcfaimport.web.server import serve  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vms", type=int, default=120)
    ap.add_argument("--scenario", default="import-flaky",
                    choices=["happy", "flaky", "import-flaky", "wait"],
                    help="flaky: some VMs fail precheck; import-flaky: some fail at import "
                         "(shows rollback); wait: imports hold at the commit gate")
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--keep", help="workspace directory to create or reuse")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", help="fixed access token (default: random)")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = ap.parse_args()

    try:
        import yaml  # noqa: F401
    except ImportError:
        print("the fake kubectl needs PyYAML: pip install pyyaml")
        return 2

    workdir = (Path(args.keep).expanduser().resolve() if args.keep
               else Path(tempfile.mkdtemp(prefix="vcfa-webdemo-")))
    workdir.mkdir(parents=True, exist_ok=True)
    vc_server, vc_port = fake_vcenter.serve(count=args.vms)

    fresh = not (workdir / "cfg.toml").exists()
    env = demo.setup(workdir, vc_port, args.scenario, args.batch_size) if fresh else dict(
        os.environ,
        FAKE_KUBECTL_STATE=str(workdir / "cluster.json"),
        FAKE_KUBECTL_SCENARIO=args.scenario,
        FAKE_KUBECTL_SETTLE="1",
        VCFA_VC_SERVER="http://127.0.0.1:{}".format(vc_port),
        VCFA_VC_USER=fake_vcenter.USER,
        VCFA_VC_PASSWORD=fake_vcenter.PASSWORD,
    )
    if args.scenario == "wait" and fresh:
        cfg_path = workdir / "cfg.toml"
        cfg_path.write_text(cfg_path.read_text(encoding="utf-8") + 'commit_action = "Wait"\n',
                            encoding="utf-8")
    os.environ.update(env)   # the console and every fake kubectl it spawns see these
    os.chdir(str(workdir))

    print("simulated infrastructure")
    print("  workspace     : {}{}".format(workdir, "" if fresh else " (reused)"))
    print("  fake vCenter  : http://127.0.0.1:{} with {} VMs".format(vc_port, args.vms))
    print("  fake operator : scenario '{}'".format(args.scenario))
    print("")
    cfg = Config.load("cfg.toml")
    app = WebApp(cfg, config_path="cfg.toml")
    try:
        return serve(app, "127.0.0.1", args.port, token=args.token,
                     open_browser=not args.no_open)
    finally:
        vc_server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
