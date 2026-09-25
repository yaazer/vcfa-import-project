"""Command line interface."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .config import SAMPLE_CONFIG, Config, ConfigError
from .discovery import (
    Filters,
    SelectionError,
    apply_filters,
    load_folder_map,
    load_network_map,
    load_tag_map,
    read_selection_file,
    resolve_identifiers,
    stage,
    write_picker,
    write_selection_csv,
)
from .engine import STAGE_IMPORT, STAGE_PRECHECK
from .folders import describe as describe_scope
from .inventory import InventoryError, load_inventory, write_template
from .kube import KubectlError
from .planner import summarize
from . import access
from . import report
from . import service
from . import state as st
from .vcenter import VCenterClient, VCenterError, discover, resolve_credentials


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        log("refusing to proceed without --yes in a non-interactive session")
        return False
    try:
        answer = input("{} [y/N] ".format(prompt)).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _load_config(args) -> Config:
    cfg = Config.load(args.config)
    if args.workdir:
        cfg.workdir = args.workdir
    if args.context:
        cfg.context = args.context
    if args.kubeconfig:
        cfg.kubeconfig = args.kubeconfig
    return cfg


def _open(args) -> tuple:
    ws = _open_ws(args)
    return ws.cfg, ws.store, ws.kube, ws.engine


def _open_ws(args) -> "service.Workspace":
    cfg = _load_config(args)
    ws = service.Workspace(cfg, dry_run=getattr(args, "dry_run", False),
                           verbose=getattr(args, "verbose", False), log=log,
                           actor=access.os_user())
    # per-run flags win over the workspace settings the Workspace just applied
    if getattr(args, "batch_size", None):
        cfg.batch_size = args.batch_size
    if getattr(args, "parallel", None):
        cfg.max_parallel_batches = args.parallel
    if getattr(args, "no_precheck", False):
        cfg.require_precheck = False
    cfg.validate()
    return ws


def _gate(store, cfg, stage: str, args, body: dict) -> Optional[int]:
    """Two-person rule: a stage in require_approval needs an approved --approval ID.

    Returns an exit code to stop with, or None to proceed (the approval is consumed).
    """
    if not access.needs_approval(cfg, stage, body):
        return None
    aid = getattr(args, "approval", None)
    if not aid:
        log("{} needs a second person's approval (require_approval).".format(stage))
        log("  ask for it:   vcfa-import approvals request --stage {} ...".format(stage))
        log("  then re-run with --approval <id> once someone else has approved it")
        return 2
    try:
        access.consume(store, aid, stage)
    except access.AccessError as exc:
        log("refused: {}".format(exc))
        return 2
    log("using approval #{} ({})".format(aid, stage))
    return None


def _waves(args) -> Optional[List[int]]:
    if getattr(args, "wave", None) is None:
        return None
    return [int(w) for w in args.wave]


# ------------------------------------------------------------------ commands
def cmd_init(args) -> int:
    target = Path(args.dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    cfg_path = target / "vcfa-import.toml"
    inv_path = target / "inventory.csv"
    if cfg_path.exists() and not args.force:
        log("{} already exists (use --force to overwrite)".format(cfg_path))
    else:
        cfg_path.write_text(SAMPLE_CONFIG, encoding="utf-8")
        log("wrote {}".format(cfg_path))
    if inv_path.exists() and not args.force:
        log("{} already exists (use --force to overwrite)".format(inv_path))
    else:
        write_template(str(inv_path))
        log("wrote {}".format(inv_path))

    # The two maps used by `stage`, with every supported column present so the
    # optional ones (wave, device_key, group) are discoverable without the docs.
    for name, text in (("portgroup-map.csv", PORTGROUP_MAP_TEMPLATE),
                       ("folder-map.csv", FOLDER_MAP_TEMPLATE)):
        path = target / name
        if path.exists() and not args.force:
            log("{} already exists (use --force to overwrite)".format(path))
        else:
            path.write_text(text, encoding="utf-8")
            log("wrote {}".format(path))

    log("")
    log("Next, either discover from vCenter:")
    log("  vcfa-import discover  -c {}".format(cfg_path.name))
    log("  vcfa-import select    -c {} --folder <path>".format(cfg_path.name))
    log("  vcfa-import stage     -c {} --map portgroup-map.csv --folder-map folder-map.csv".format(
        cfg_path.name))
    log("or load a list you already have:")
    log("  vcfa-import load      -c {} --inventory {}".format(cfg_path.name, inv_path.name))
    log("then:")
    log("  vcfa-import preflight -c {}".format(cfg_path.name))
    log("  vcfa-import precheck  -c {} --wave 1".format(cfg_path.name))
    log("  vcfa-import run       -c {} --wave 1".format(cfg_path.name))
    return 0


# Every column each map accepts. Blank cells are fine; only portgroup/namespace
# (or folder/namespace) are required. `wave` here is used when neither the
# picker nor the folder map set one.
PORTGROUP_MAP_TEMPLATE = """\
portgroup,namespace,subnet,wave,device_key,subnet_kind,subnet_api_group
VLAN197-Prod,redbull-ns1-r95mc,subnet-vlan197,1,,,
VLAN200-DB,redbull-ns2-k22ab,subnet-vlan200,2,,,
VLAN2*,redbull-ns2-k22ab,subnet-vlan2xx,2,,Subnet,crd.nsx.vmware.com
"""

FOLDER_MAP_TEMPLATE = """\
folder,namespace,wave,group
Production,redbull-ns1-r95mc,2,
Production/Web,redbull-ns1-r95mc,1,web
Databases,redbull-ns2-k22ab,1,
Legacy/*,redbull-ns3-legacy,3,
"""


def cmd_validate(args) -> int:
    cfg = Config.load(args.config)
    records, warnings = load_inventory(args.inventory, cfg)
    log("inventory: {} VM(s) across {} namespace(s), {} wave(s)".format(
        len(records),
        len({r.namespace for r in records}),
        len({r.wave for r in records}),
    ))
    rows = []
    for wave in sorted({r.wave for r in records}):
        members = [r for r in records if r.wave == wave]
        rows.append([wave, len(members), len({m.namespace for m in members})])
    log("")
    log(report.table(["wave", "vms", "namespaces"], rows, indent="  "))
    no_net = [r for r in records if not r.nics]
    if no_net:
        log("")
        log("  {} VM(s) have no subnet mapping".format(len(no_net)))
    if warnings:
        log("")
        log("warnings ({}):".format(len(warnings)))
        for w in warnings[:40]:
            log("  - " + w)
        if len(warnings) > 40:
            log("  ... and {} more".format(len(warnings) - 40))
    log("")
    log("inventory is structurally valid")
    return 0


def cmd_load(args) -> int:
    cfg, store, _kube, _engine = _open(args)
    records, warnings = load_inventory(args.inventory, cfg)
    summary = store.sync_inventory(records)
    store.set_meta("inventory_path", str(Path(args.inventory).expanduser().resolve()))
    log("loaded {} row(s) from {}".format(len(records), args.inventory))
    log("  added {} | updated {} | unchanged {} | locked (in flight) {}".format(
        summary["added"], summary["updated"], summary["unchanged"], summary["locked"]))
    for conflict in summary["conflicts"][:20]:
        log("  ! " + conflict)
    if warnings:
        log("  {} inventory warning(s); run `validate` to see them".format(len(warnings)))
    log("")
    log(report.render_status(store, cfg, failures=0))
    return 0


# ------------------------------------------------------------- discovery
def _filters_from(args) -> Filters:
    return Filters(
        name=getattr(args, "name", None) or [],
        exclude_name=getattr(args, "exclude_name", None) or [],
        cluster=getattr(args, "cluster", None) or [],
        folder=getattr(args, "folder", None) or [],
        network=getattr(args, "network", None) or [],
        guest_os=getattr(args, "guest_os", None) or [],
        datacenter=getattr(args, "datacenter", None) or [],
        folder_exact=getattr(args, "no_subfolders", False),
        powered_on=getattr(args, "powered_on", False),
        powered_off=getattr(args, "powered_off", False),
        tools_running=getattr(args, "tools_running", False),
        with_nics=getattr(args, "with_nics", False),
        morefs=getattr(args, "vm", None) or [],
        regex=getattr(args, "regex", None),
        tag=getattr(args, "tag", None) or [],
    )


def cmd_discover(args) -> int:
    cfg, store, _kube, _engine = _open(args)
    server, user, password = resolve_credentials(args.vcenter, args.user, args.password)
    if args.insecure:
        log("warning: TLS verification disabled (--insecure)")

    def progress(done: int, total: int) -> None:
        if done == total or done % 100 == 0:
            log("    {}/{} VM details read".format(done, total))

    client = VCenterClient(server, user, password, insecure=args.insecure,
                           timeout=args.timeout, log=log)
    extras: Dict[str, Any] = {}
    try:
        client.connect()
        vms = discover(
            client,
            power_state="POWERED_ON" if args.powered_on else None,
            with_tools=not args.no_tools,
            with_placement=not args.no_placement,
            with_tags=not args.no_tags,
            concurrency=args.concurrency,
            progress=progress,
            log=log,
            extras=extras,
        )
    finally:
        client.close()

    summary = store.upsert_discovered(vms)
    service.record_discovery(store, server, extras)
    store.set_meta("vcenter", server)
    store.set_meta("discovered_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    counts = store.discovered_counts()
    log("")
    log("discovered {} VM(s) from {} (added {}, refreshed {})".format(
        len(vms), server, summary["added"], summary["updated"]))
    log("  inventory cache now holds {} VM(s), {} selected".format(
        counts["total"], counts["selected"]))
    no_nics = sum(1 for v in vms if not v.nics)
    if no_nics:
        log("  {} VM(s) have no network adapters".format(no_nics))
    if not args.no_tools:
        stale = sum(1 for v in vms if "running" not in (v.tools_status or "").lower())
        if stale:
            log("  {} VM(s) are not running VM Tools (likely precheck failures)".format(stale))
    log("")
    log("Next: `browse` to look around, `pick` for a clickable list, or")
    log("      `select --cluster ... --name ...` to choose non-interactively")
    return 0


def cmd_browse(args) -> int:
    _cfg, store, _kube, _engine = _open(args)
    cached = store.query_discovered()
    if not cached:
        # --json is a scripting interface: it must always emit JSON.
        log("[]" if args.json else "nothing discovered yet -- run `discover` first")
        return 0
    rows = [r for r in cached if r["selected"]] if args.selected else cached
    filtered = apply_filters(rows, _filters_from(args))
    if args.json:
        log(json.dumps([dict(r) for r in filtered], indent=2))
        return 0
    if not filtered:
        log("no discovered VM matches those filters ({} in the cache)".format(len(cached)))
        return 0
    if args.csv:
        count = write_selection_csv(filtered, args.csv)
        log("wrote {} row(s) to {}".format(count, args.csv))
        return 0
    if args.folders:
        _print_folder_tree(store)
        return 0
    if args.facets:
        facets = store.discovered_facets()
        for key, values in facets.items():
            log("  {:<12} {}".format(key, ", ".join(values[:40]) or "(none)"))
            if len(values) > 40:
                log("  {:<12} ... and {} more".format("", len(values) - 40))
        return 0

    shown = filtered[:args.limit] if args.limit else filtered
    log(report.table(
        ["sel", "vm", "moref", "power", "cluster", "folder", "networks", "cpu", "MiB", "tools"],
        [["x" if r["selected"] else "", r["name"], r["moref"],
          (r["power_state"] or "").replace("POWERED_", ""), r["cluster"][:20],
          (r["folder"] or "/")[-30:],
          (r["networks"] or "")[:28], r["cpu_count"], r["memory_mb"],
          "yes" if "running" in (r["tools_status"] or "").lower() else
          ((r["tools_status"] or "")[:12] or "?")]
         for r in shown],
        indent="  "))
    log("")
    log("  {} of {} discovered VM(s) match".format(len(filtered), len(rows)))
    if args.limit and len(filtered) > args.limit:
        log("  (showing the first {}; raise --limit to see more)".format(args.limit))
    return 0


def _print_folder_tree(store) -> None:
    tree = store.folder_tree()
    if not tree:
        log("no folders discovered")
        return
    multi_dc = len({n["datacenter"] for n in tree}) > 1
    log("  {:<44} {:>6} {:>8} {:>8}".format("folder", "direct", "subtree", "selected"))
    log("  {:<44} {:>6} {:>8} {:>8}".format("-" * 44, "-" * 6, "-" * 8, "-" * 8))
    last_dc = None
    for node in tree:
        if multi_dc and node["datacenter"] != last_dc:
            log("  [{}]".format(node["datacenter"] or "(unknown datacenter)"))
            last_dc = node["datacenter"]
        name = node["path"].split("/")[-1] + "/" if node["path"] else "(datacenter root)"
        indent = "  " * node["depth"]
        label = (indent + name)[:44]
        log("  {:<44} {:>6} {:>8} {:>8}".format(
            label, node["direct"] or "", node["subtree"],
            node["selected"] or ""))
    log("")
    log("  select a folder and everything beneath it with:  select --folder <path>")
    log("  (paths are relative to the datacenter, e.g. Production/Web)")


def cmd_pick(args) -> int:
    _cfg, store, _kube, _engine = _open(args)
    rows = store.query_discovered()
    if not rows:
        log("nothing discovered yet -- run `discover` first")
        return 0
    filters = _filters_from(args)
    if not filters.is_empty():
        rows = apply_filters(rows, filters)
    out = args.out or str(Path(_cfg_workdir(args)) / "picker.html")
    path = write_picker(rows, out, source=str(store.get_meta("vcenter") or ""))
    log("wrote {} ({} VM(s))".format(path, len(rows)))
    log("")
    log("Open it in a browser, tick the VMs you want, optionally set namespaces,")
    log("then click 'Download selection.csv' and run:")
    log("  vcfa-import select --from-file selection.csv")
    return 0


def _cfg_workdir(args) -> str:
    cfg = Config.load(args.config)
    return args.workdir or cfg.workdir


def cmd_select(args) -> int:
    _cfg, store, _kube, _engine = _open(args)
    rows = store.query_discovered()
    if not rows:
        log("nothing discovered yet -- run `discover` first")
        return 0

    if args.none:
        cleared = store.clear_selection()
        log("cleared the selection ({} VM(s))".format(cleared))
        return 0

    namespaces: Dict[str, str] = {}
    waves: Dict[str, int] = {}
    if args.from_file:
        idents, namespaces, waves = read_selection_file(args.from_file)
        morefs, unmatched, ambiguous = resolve_identifiers(rows, idents)
        for name in unmatched[:20]:
            log("  ! no discovered VM matches '{}'".format(name))
        for name in ambiguous[:20]:
            log("  ! '{}' matches more than one VM; use its moref".format(name))
        if unmatched or ambiguous:
            log("  ({} unmatched, {} ambiguous)".format(len(unmatched), len(ambiguous)))
        targets = [r for r in rows if r["moref"] in set(morefs)]
    else:
        filters = _filters_from(args)
        if filters.is_empty() and not args.all:
            log("no filters given; pass --all to select every discovered VM, or narrow with")
            log("--name/--cluster/--folder/--network/--powered-on/--from-file")
            return 2
        targets = apply_filters(rows, filters)

    if not targets:
        log("nothing matched; the selection is unchanged")
        return 0

    deselect = args.deselect
    morefs = [r["moref"] for r in targets]
    changed = store.set_selected(morefs, not deselect,
                                 namespace=args.namespace, wave=args.wave_single)
    if args.app is not None:
        store.set_app(morefs, args.app)
        log("application set to '{}' on {} VM(s)".format(args.app, len(morefs)))

    # Per-VM namespaces from a picker export take precedence over --namespace.
    if namespaces or waves:
        by_ident = {r["moref"]: r["moref"] for r in rows}
        by_name = {(r["name"] or "").lower(): r["moref"] for r in rows}
        for ident, ns in namespaces.items():
            moref = by_ident.get(ident) or by_name.get(ident.lower())
            if moref:
                store.set_selected([moref], True, namespace=ns)
        for ident, wv in waves.items():
            moref = by_ident.get(ident) or by_name.get(ident.lower())
            if moref:
                store.set_selected([moref], True, wave=wv)

    counts = store.discovered_counts()
    log("{} {} VM(s); {} of {} now selected".format(
        "deselected" if deselect else "selected", changed, counts["selected"], counts["total"]))

    missing_ns = [r for r in store.query_discovered(selected=True) if not r["namespace"]]
    if missing_ns and not deselect:
        log("  {} selected VM(s) have no namespace yet -- set one with".format(len(missing_ns)))
        log("  `select --namespace <ns>`, in the picker, or via `stage --map`")
    return 0


def cmd_stage(args) -> int:
    cfg, store, _kube, _engine = _open(args)
    rows = store.query_discovered(selected=True)
    if not rows:
        log("no VMs are selected -- use `pick` or `select` first")
        return 0

    mapping = load_network_map(args.map) if args.map else {}
    folder_mapping = load_folder_map(args.folder_map) if args.folder_map else {}
    tag_mapping = load_tag_map(args.tag_map) if args.tag_map else {}
    result = stage(rows, cfg, mapping=mapping, folder_mapping=folder_mapping,
                   default_namespace=args.default_namespace, default_wave=args.wave_single or 1,
                   tag_mapping=tag_mapping)
    for move in result.app_moves[:20]:
        log("  ~ " + move)
    if result.ns_conflicts:
        log("namespace conflicts -- the first source wins; fix whichever map is wrong:")
        for c in result.ns_conflicts[:30]:
            log("  ! {} ({}): {} from the {}; ignored {}".format(
                c["vm_name"], c["moref"], c["namespace"], c["source"],
                ", ".join("{} ({})".format(i["namespace"], i["source"]) for i in c["ignored"])))
        if len(result.ns_conflicts) > 30:
            log("  ... and {} more".format(len(result.ns_conflicts) - 30))

    if result.unmapped_folders:
        log("folders with no entry in the folder map:")
        for folder, count in sorted(result.unmapped_folders.items(), key=lambda kv: -kv[1]):
            log("  {:<44} {} VM(s)".format(folder, count))
        log("")
    if result.unmapped_networks:
        log("networks with no mapping entry:")
        for net, count in sorted(result.unmapped_networks.items(), key=lambda kv: -kv[1]):
            log("  {:<40} {} VM(s)".format(net or "(none)", count))
        log("")
    if result.problems:
        log("{} VM(s) cannot be staged:".format(len(result.problems)))
        for problem in result.problems[:25]:
            log("  ! " + problem)
        if len(result.problems) > 25:
            log("  ... and {} more".format(len(result.problems) - 25))
        log("")
    if not result.records:
        log("nothing could be staged")
        return 2

    if args.out:
        _write_inventory_csv(result.records, args.out)
        log("wrote {} row(s) to {}".format(len(result.records), args.out))
        log("review it, then: vcfa-import load -i {}".format(args.out))
        return 0

    summary = store.sync_inventory(result.records)
    log("staged {} VM(s) into the import queue".format(len(result.records)))
    log("  added {} | updated {} | unchanged {} | locked (in flight) {}".format(
        summary["added"], summary["updated"], summary["unchanged"], summary["locked"]))
    for conflict in summary["conflicts"][:10]:
        log("  ! " + conflict)
    log("")
    log(report.render_status(store, cfg, failures=0))
    return 0


def _write_inventory_csv(records, path: str) -> None:
    import csv as _csv
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    max_nics = max((len(r.nics) for r in records), default=1) or 1
    header = ["vm_name", "moref", "namespace", "wave", "mode", "notes"]
    for i in range(1, max_nics + 1):
        header += ["nic{}_subnet".format(i), "nic{}_device_key".format(i)]
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = _csv.writer(fh)
        writer.writerow(header)
        for rec in records:
            row = [rec.vm_name, rec.moref, rec.namespace, rec.wave, rec.mode, rec.notes]
            for i in range(max_nics):
                if i < len(rec.nics):
                    row += [rec.nics[i].subnet or "", rec.nics[i].device_key]
                else:
                    row += ["", ""]
            writer.writerow(row)


def cmd_preflight(args) -> int:
    cfg, store, _kube, engine = _open(args)
    result = engine.preflight(check_targets=not args.skip_targets)
    log("context        : {}".format(result.context))
    log("server version : {}".format(result.server_version or "unknown"))
    log("")
    for name, ok, detail in result.checks:
        log("  [{}] {:<32} {}".format("ok" if ok else "!!", name, detail))
    if result.crds:
        log("")
        log("  mobility-operator resources: " + ", ".join(sorted(result.crds)))
    for warning in result.warnings:
        log("\n  warning: " + warning)
    if result.problems:
        log("")
        for problem in result.problems:
            log("  PROBLEM: " + problem)
        return 2
    log("\npreflight passed")
    return 0


def cmd_plan(args) -> int:
    cfg, store, _kube, engine = _open(args)
    stage = STAGE_PRECHECK if args.stage == "precheck" else STAGE_IMPORT
    waves = _waves(args) or [None]
    all_batches = []
    for wave in waves:
        all_batches += engine.plan(stage, wave=wave, limit=args.limit,
                                   include_failed=args.include_failed,
                                   write_manifests=not args.no_write)
    if not all_batches:
        log("nothing to plan for stage '{}' (no eligible VMs)".format(stage))
        return 0
    summary = summarize(all_batches)
    log("stage {}: {} batch(es), {} VM(s)".format(stage, summary["batches"], summary["vms"]))
    log("")
    log(report.table(
        ["wave", "batches", "vms"],
        [[w, v["batches"], v["vms"]] for w, v in summary["by_wave"].items()], indent="  "))
    log("")
    log(report.table(
        ["namespace", "batches", "vms"],
        [[n, v["batches"], v["vms"]] for n, v in summary["by_namespace"].items()], indent="  "))
    if not args.no_write:
        log("")
        log("manifests written to {}".format(engine.manifest_dir()))
    if args.show:
        log("")
        log(all_batches[0].yaml())
    if args.server_dry_run:
        log("")
        log("server-side validation:")
        ok = True
        for b in all_batches:
            try:
                engine.kube.server_dry_run(b.yaml(), b.namespace)
                log("  [ok] {}".format(b.name))
            except KubectlError as exc:
                ok = False
                log("  [!!] {}: {}".format(b.name, str(exc)[:400]))
        return 0 if ok else 2
    return 0


def _execute(args, stage: str) -> int:
    cfg, store, _kube, engine = _open(args)
    waves = _waves(args)
    only = args.vm or None
    scope = args.folder or None
    exact = bool(getattr(args, "no_subfolders", False))
    rows = [list(r) for r in service.eligible_by_wave(
        engine, stage, waves, morefs=only, include_failed=args.include_failed,
        folders=scope, folder_exact=exact)]
    held = service.holdbacks(engine, stage, waves, morefs=only, include_failed=args.include_failed,
                             folders=scope, folder_exact=exact)
    if not rows:
        log("nothing to do for stage '{}'".format(stage))
        for note in held:
            log("  held back: " + note)
        if stage == STAGE_IMPORT and cfg.require_precheck:
            log("(VMs must reach precheck_passed first; run `precheck`, or pass --no-precheck)")
        return 0

    total = sum(r[1] for r in rows)
    per_ns: Dict[str, int] = {}
    for wave, _n in rows:
        for vm in engine.eligible(stage, wave, morefs=only, include_failed=args.include_failed,
                                  folders=scope, folder_exact=exact):
            per_ns[vm["namespace"]] = per_ns.get(vm["namespace"], 0) + 1
    from .estimate import estimate, human
    eta = estimate(store, cfg, stage, per_ns)
    log("about to {} {} VM(s){}:".format(
        "precheck" if stage == STAGE_PRECHECK else "IMPORT", total,
        " in " + describe_scope(scope, exact) if scope else ""))
    log(report.table(["wave", "vms"], rows, indent="  "))
    log("")
    log("  context      : {}".format(cfg.context or "(kubectl current-context)"))
    log("  batch size   : {}   parallel batches: {} (per namespace {})".format(
        cfg.batch_size, cfg.max_parallel_batches, cfg.max_parallel_batches_per_namespace))
    if stage == STAGE_IMPORT:
        log("  commitAction : {}{}".format(
            cfg.commit_action,
            "  (imports commit automatically and cannot be exported back to vCenter)"
            if cfg.commit_action == "Auto" else "  (each batch will wait for `commit`)"))
    log("  estimate     : about {} ({} batch(es); {} batch time {})".format(
        human(eta["seconds"]), eta["batches"], eta["basis"], human(eta["batch_seconds"])))
    for note in held:
        log("  held back    : " + note)
    if args.dry_run:
        log("  DRY RUN      : manifests are rendered and validated, nothing is applied")
    log("")

    verb = "Precheck" if stage == STAGE_PRECHECK else "Import"
    if not _confirm("{} {} VM(s) now?".format(verb, total), args.yes):
        log("aborted")
        return 1

    body = {"waves": waves, "folders": scope, "morefs": only, "dry_run": args.dry_run}
    code = _gate(store, cfg, stage, args, body)
    if code is not None:
        return code
    started = time.time()
    lock = None if args.dry_run else service.WorkspaceLock(cfg, "`{}` from the CLI".format(stage))
    try:
        if lock:
            lock.acquire()
        totals = service.run_stage(
            engine, stage, waves=waves, limit=args.limit, morefs=only,
            include_failed=args.include_failed, folders=scope, folder_exact=exact,
            rollback_failed=getattr(args, "rollback_failed", False), log=log)
    except KeyboardInterrupt:
        log("\ninterrupted; state is saved -- re-run the same command to resume")
        return 130
    finally:
        if lock:
            lock.release()

    halted = totals["halted"]
    if not args.dry_run:
        status = "warning" if (halted or totals["failed"]) else "succeeded"
        service.notify_run(cfg, stage, "{} from the CLI".format(stage), status, totals,
                           actor=store.actor, wait=True)
    if halted is not None:
        log("\nRUN HALTED: {}".format(halted))
        log("Investigate with `vcfa-import status` and `vcfa-import events --level error`,")
        log("then resume with the same command once the cause is fixed.")
        return 3

    log("")
    log("{} finished in {:.0f}s: applied {} | succeeded {} | failed {} | awaiting commit {}".format(
        stage, time.time() - started, totals["applied"], totals["succeeded"],
        totals["failed"], totals["awaiting_commit"]))
    log("")
    log(report.render_status(store, cfg))
    return 0 if totals["failed"] == 0 else 4


def cmd_precheck(args) -> int:
    return _execute(args, STAGE_PRECHECK)


def cmd_run(args) -> int:
    return _execute(args, STAGE_IMPORT)


def cmd_status(args) -> int:
    cfg, store, _kube, engine = _open(args)
    if args.refresh:
        counts = engine.refresh()
        log("re-polled {} batch(es)".format(counts["batches"]))
        log("")
    if args.folder:
        rows = store.query_vms(folders=args.folder, folder_exact=args.no_subfolders)
        log(report.render_scope_status(
            store, rows, describe_scope(args.folder, args.no_subfolders), failures=args.failures))
        return 0
    log(report.render_status(store, cfg, failures=args.failures))
    return 0


def cmd_watch(args) -> int:
    cfg, store, _kube, engine = _open(args)
    try:
        while True:
            counts = engine.refresh()
            print("\033[2J\033[H", end="")
            log(report.render_status(store, cfg, failures=args.failures))
            log("")
            log("  refreshed {} batch(es) at {} -- Ctrl-C to stop".format(
                counts["batches"], time.strftime("%H:%M:%S")))
            remaining = store.counts()
            active = sum(remaining.get(s, 0) for s in st.ACTIVE_STATES)
            if not active and not args.forever:
                log("\nno batches in flight; exiting")
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("\nstopped")
        return 0


def cmd_commit(args) -> int:
    cfg, store, _kube, engine = _open(args)
    waiting = store.query_vms(states=[st.S_AWAITING_COMMIT], wave=args.wave_single,
                              morefs=args.vm or None)
    if not waiting:
        log("no VMs are awaiting commit")
        return 0
    log("{} VM(s) awaiting commit across {} batch(es)".format(
        len(waiting), len({w["batch_name"] for w in waiting if w["batch_name"]})))
    log(report.table(
        ["vm", "moref", "namespace", "batch"],
        [[v["vm_name"], v["moref"], v["namespace"], v["batch_name"] or "?"] for v in waiting[:25]],
        indent="  "))
    if len(waiting) > 25:
        log("  ... and {} more".format(len(waiting) - 25))
    log("")
    log("  Committing is irreversible: committed VMs cannot be exported back to vCenter.")
    if not _confirm("Commit these {} VM(s)?".format(len(waiting)), args.yes):
        log("aborted")
        return 1
    code = _gate(store, cfg, "commit", args, {"wave": args.wave_single, "morefs": args.vm})
    if code is not None:
        return code
    with service.WorkspaceLock(cfg, "`commit` from the CLI"):
        result = engine.commit(morefs=args.vm or None, wave=args.wave_single)
    log("patched {} batch(es) covering {} VM(s)".format(result["batches"], result["vms"]))
    for err in result["errors"]:
        log("  ! " + err)
    log("")
    log("run `vcfa-import status --refresh` once the operator has processed the commit")
    return 0 if not result["errors"] else 4


def cmd_rollback(args) -> int:
    cfg, store, _kube, engine = _open(args)
    if not (args.batch or args.vm or args.failed or args.folder):
        log("say what to roll back: --batch NAME, --vm MOREF, --folder PATH, "
            "or --failed (all failed batches)")
        return 2

    batches, notes = engine.rollback_targets(
        batch_names=args.batch, morefs=args.vm, failed_only=args.failed, wave=args.wave_single,
        folders=args.folder, folder_exact=args.no_subfolders)
    for note in notes:
        log("  ! " + note)
    if not batches:
        log("nothing to roll back")
        return 0

    rows = []
    total_revert = total_keep = 0
    for b in batches:
        members = [v for v in store.query_vms(batch_name=b["name"]) if v["namespace"] == b["namespace"]]
        revert = sum(1 for v in members if v["state"] in engine.ROLLBACKABLE_STATES)
        keep = sum(1 for v in members if v["state"] == st.S_COMMITTED)
        total_revert += revert
        total_keep += keep
        rows.append([b["name"], b["namespace"], b["state"], revert, keep])
    log("rollbackAction={} will be set on {} batch(es):".format(args.action, len(batches)))
    log(report.table(["batch", "namespace", "state", "to revert", "committed (kept)"],
                     rows, indent="  "))
    log("")
    log("  This hands {} VM(s) back to vCenter. Committed VMs are not affected --".format(total_revert))
    log("  a committed import cannot be reversed.")
    if args.delete:
        log("  Each batch will be DELETED once the operator confirms its rollback.")
    if not _confirm("Roll back {} VM(s) across {} batch(es)?".format(total_revert, len(batches)),
                    args.yes):
        log("aborted")
        return 1
    code = _gate(store, cfg, "rollback", args, {"batches": [b["name"] for b in batches]})
    if code is not None:
        return code

    with service.WorkspaceLock(cfg, "`rollback` from the CLI"):
        result = engine.rollback(batches, action=args.action, wait=not args.no_wait,
                                 delete=args.delete, timeout_minutes=args.timeout)
    log("")
    log("patched {} batch(es); {} VM(s) confirmed reverted to vCenter; {} deleted".format(
        len(result["patched"]), result["reverted"], len(result["deleted"])))
    for name in result["pending"]:
        log("  ~ {} still reverting -- `status --refresh` to follow up".format(name))
    for err in result["errors"]:
        log("  ! " + err)
    if result["reverted"]:
        log("")
        log("reverted VMs are retryable: `retry` returns them to the queue")
    if not args.delete and result["reverted"] and not result["pending"]:
        log("the rolled-back batch objects are still on the cluster; `cleanup` removes them")
    return 0 if not result["errors"] and not result["pending"] else 4


def cmd_abandon(args) -> int:
    _cfg, store, _kube, engine = _open(args)
    if not (args.batch or args.vm or args.folder or args.stage):
        log("say what to abandon: --batch NAME, --vm MOREF, --folder PATH, or --stage precheck")
        return 2
    batches, notes = engine.abandon_targets(
        batch_names=args.batch, morefs=args.vm, folders=args.folder,
        folder_exact=args.no_subfolders, stage=args.stage)
    for note in notes:
        log("  ! " + note)
    if not batches:
        log("nothing to abandon")
        return 0 if not notes else 4
    rows = []
    for b in batches:
        members = [v for v in store.query_vms(batch_name=b["name"]) if v["namespace"] == b["namespace"]]
        rows.append([b["name"], b["namespace"], b["stage"], b["state"], len(members)])
    log("these batch objects will be deleted and their VMs returned to pending:")
    log(report.table(["batch", "namespace", "stage", "state", "vms"], rows, indent="  "))
    log("")
    log("  No rollback is involved: precheck batches migrate nothing, and import")
    log("  batches are only accepted here once none of their VMs is still in flight.")
    if not _confirm("Abandon {} batch(es)?".format(len(batches)), args.yes):
        log("aborted")
        return 1
    with service.WorkspaceLock(_cfg, "`abandon` from the CLI"):
        result = engine.abandon(batches)
    log("deleted {} batch(es); {} VM(s) back to pending".format(
        len(result["deleted"]), result["requeued"]))
    for err in result["errors"]:
        log("  ! " + err)
    if result["requeued"]:
        log("")
        log("edit the map or inventory if needed, then `stage` / `load` will update the")
        log("pending VMs; `precheck` starts them over")
    return 0 if not result["errors"] else 4


def cmd_cleanup(args) -> int:
    cfg, _store, _kube, engine = _open(args)
    candidates = engine.cleanup_candidates()
    if args.batch:
        wanted = set(args.batch)
        candidates = [b for b in candidates if b["name"] in wanted]
        missing = wanted - {b["name"] for b in candidates}
        for name in sorted(missing):
            log("  ! {}: not in a confirmed rolled-back state; refusing to delete".format(name))
    if not candidates:
        log("no batches with a confirmed rollback to delete")
        return 0
    log("batches whose rollback the operator has confirmed:")
    log(report.table(["batch", "namespace", "vms", "note"],
                     [[b["name"], b["namespace"], b["vm_count"], (b["message"] or "")[:50]]
                      for b in candidates], indent="  "))
    if not _confirm("Delete {} batch object(s) from the cluster?".format(len(candidates)),
                    args.yes):
        log("aborted")
        return 1
    with service.WorkspaceLock(cfg, "`cleanup` from the CLI"):
        result = engine.delete_batches(candidates)
    log("deleted {} batch(es)".format(len(result["deleted"])))
    for err in result["errors"]:
        log("  ! " + err)
    return 0 if not result["errors"] else 4


def cmd_retry(args) -> int:
    _cfg, store, _kube, engine = _open(args)
    moved = engine.requeue(morefs=args.vm or None, wave=args.wave_single, force=args.force,
                           folders=args.folder or None, folder_exact=args.no_subfolders)
    log("requeued {} VM(s) to pending".format(moved))
    if moved:
        log("run `precheck` then `run` to retry them")
    return 0


def cmd_vms(args) -> int:
    _cfg, store, _kube, _engine = _open(args)
    rows = store.query_vms(states=args.state or None, wave=args.wave_single,
                           namespace=args.namespace, morefs=args.vm or None, limit=args.limit,
                           folders=args.folder or None, folder_exact=args.no_subfolders)
    if args.json:
        log(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    log(report.table(
        ["vm", "moref", "folder", "namespace", "wave", "state", "att", "batch", "message"],
        [[r["vm_name"], r["moref"], (r["src_folder"] or "/")[-24:], r["namespace"], r["wave"],
          report.STATE_LABEL.get(r["state"], r["state"]), r["attempts"],
          (r["batch_name"] or r["precheck_batch"] or "")[:24],
          (r["message"] or "")[:60]] for r in rows],
        indent="  "))
    log("")
    log("  {} row(s)".format(len(rows)))
    return 0


def cmd_skip(args) -> int:
    _cfg, store, _kube, _engine = _open(args)
    changed, notes = service.skip_vms(store, args.vm, unskip=args.unskip)
    for note in notes:
        log("  ! " + note)
    log("{} {} VM(s)".format("unskipped" if args.unskip else "skipped", changed))
    return 0


def cmd_events(args) -> int:
    _cfg, store, _kube, _engine = _open(args)
    rows = store.recent_events(limit=args.limit, level=args.level)
    log(report.table(
        ["time", "level", "vm", "batch", "message"],
        [[r["ts"], r["level"], r["moref"] or "", (r["batch"] or "")[:24], r["message"][:90]]
         for r in rows],
        indent="  "))
    return 0


def cmd_history(args) -> int:
    _cfg, store, _kube, _engine = _open(args)
    if args.vm:
        for moref in args.vm:
            log(report.render_history(store, moref))
            log("")
        return 0

    rows = store.transitions(limit=args.limit, to_state=args.state,
                             since=args.since, newest_first=True)
    if args.json:
        log(json.dumps([dict(r) for r in rows], indent=2))
        return 0
    log(report.table(
        ["when", "vm", "moref", "from", "to", "stage", "batch", "held"],
        [[r["ts"], r["vm_name"][:22], r["moref"], r["from_state"] or "-", r["to_state"],
          r["stage"] or "", (r["batch"] or "")[:24],
          "" if r["held_secs"] is None else "{:.0f}s".format(r["held_secs"])]
         for r in rows],
        indent="  "))
    log("")
    stats = store.transition_stats()
    log("  {} transition(s) recorded".format(stats["transitions"]))
    if stats.get("import_seconds"):
        t = stats["import_seconds"]
        log("  import duration over {} committed VM(s): avg {:.0f}s, min {:.0f}s, max {:.0f}s"
            .format(t["count"], t["avg"], t["min"], t["max"]))
    log("  full append-only log: {}".format(store.ledger_path))
    return 0


def cmd_ledger(args) -> int:
    cfg, store, _kube, _engine = _open(args)
    if args.verify:
        return _verify_ledger(store)
    outputs = []
    if args.csv:
        count = report.write_transitions_csv(store, args.csv)
        outputs.append("{} ({} transitions)".format(args.csv, count))
    if args.tracker:
        count = report.write_csv(store, args.tracker)
        outputs.append("{} ({} VMs)".format(args.tracker, count))
    if not outputs:
        default = str(Path(cfg.workdir).expanduser() / "tracker.csv")
        count = report.write_csv(store, default)
        outputs.append("{} ({} VMs)".format(default, count))
        trans = str(Path(cfg.workdir).expanduser() / "transitions.csv")
        outputs.append("{} ({} transitions)".format(
            trans, report.write_transitions_csv(store, trans)))
    for out in outputs:
        log("wrote {}".format(out))
    log("")
    log("append-only ledger: {}".format(store.ledger_path))
    return 0


def _verify_ledger(store) -> int:
    """Cross-check the JSONL ledger against the transitions table."""
    path = Path(store.ledger_path)
    if not path.is_file():
        log("no ledger file at {}".format(path))
        return 2
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    bad = 0
    for i, line in enumerate(lines, start=1):
        try:
            json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            log("  ! line {} is not valid JSON".format(i))
    rows = store.transitions()
    log("ledger  : {} entries ({} unreadable)".format(len(lines), bad))
    log("database: {} transitions".format(len(rows)))
    if bad:
        return 4
    if len(lines) < len(rows):
        log("the ledger has fewer entries than the database -- it may have been "
            "truncated or started late; the database is authoritative")
        return 4
    if len(lines) > len(rows):
        log("the ledger has more entries than the database, which is expected if "
            "the database was rebuilt; the ledger is the longer record")
    log("ledger looks consistent")
    return 0


def cmd_report(args) -> int:
    cfg, store, _kube, engine = _open(args)
    if args.refresh:
        engine.refresh()
    outputs = []
    if args.csv:
        count = report.write_csv(store, args.csv)
        outputs.append("{} ({} rows)".format(args.csv, count))
    if args.html:
        path = report.write_html(store, cfg, args.html)
        outputs.append(path)
    if not outputs:
        default = str(Path(cfg.workdir).expanduser() / "report.html")
        outputs.append(report.write_html(store, cfg, default))
    for out in outputs:
        log("wrote {}".format(out))
    return 0


def cmd_namespaces(args) -> int:
    cfg, _store, kube, _engine = _open(args)
    data = service.cluster_namespaces(kube)
    for note in data["notes"]:
        log(note)
    if not data["namespaces"]:
        log("no namespaces found")
        return 0
    label = {"cluster": "on the Supervisor", "kubeconfig": "kubeconfig context", "both": "on the Supervisor, kubeconfig context"}
    log(report.table(["namespace", "seen"], [[n["name"], label[n["source"]]] for n in data["namespaces"]], indent="  "))
    return 0


def cmd_portgroups(args) -> int:
    _cfg, store, _kube, _engine = _open(args)
    data = service.vcenter_portgroups(store)
    for note in data["notes"]:
        log(note)
    if data["portgroups"]:
        log(report.table(["portgroup", "type", "VMs", "selected"],
                         [[p["name"], p["type"].replace("_", " ").lower(), p["vms"] or "", p["selected"] or ""]
                          for p in data["portgroups"]], indent="  "))
        if data["discovered_at"]:
            log("  as of the discovery at {}".format(data["discovered_at"]))
    return 0


def cmd_subnets(args) -> int:
    _cfg, store, kube, _engine = _open(args)
    names = [n["name"] for n in service.cluster_namespaces(kube)["namespaces"]]
    names += [r["namespace"] for r in store.query_vms() if r["namespace"]]
    data = service.cluster_subnets(kube, sorted(set(names)))
    for note in data["notes"]:
        log(note)
    if not data["subnets"]:
        log("no subnets found")
        return 0
    log(report.table(["subnet", "kind", "namespace", "display name"],
                     [[s["name"], s["kind"], s["namespace"], s["display"]] for s in data["subnets"]], indent="  "))
    log("  map a portgroup to the name in the first column (subnetInfo carries no namespace)")
    return 0


def cmd_readiness(args) -> int:
    from . import readiness
    _cfg, store, _kube, _engine = _open(args)
    rows = store.query_discovered(selected=True if args.selected else None)
    rows = apply_filters(rows, _filters_from(args))
    if not rows:
        log("nothing discovered (or nothing matches)")
        return 0
    summ = readiness.summary(rows)
    g = summ["grades"]
    log("readiness of {} VM(s): {} ready · {} worth a look · {} likely to fail precheck".format(
        len(rows), g["ready"], g["warn"], g["block"]))
    log("(advisory: the operator's precheck is the authority)")
    log("")
    names = {r["moref"]: r["name"] for r in rows}
    for f in summ["findings"]:
        log("  [{}] {:<34} {:>5} VM(s)  {}".format(f["level"], f["title"], f["count"],
                                                  ", ".join(names[m] for m in f["morefs"][:4])
                                                  + (" ..." if f["count"] > 4 else "")))
        log("         {}".format(f["advice"]))
    return 0 if not g["block"] else 4


def cmd_verify(args) -> int:
    ws = _open_ws(args)
    store = ws.store
    ws_rows = service.verify_rows(store, morefs=args.vm or None, wave=args.wave_single,
                                  only_unverified=args.unverified)
    if not ws_rows:
        log("no committed VMs to verify")
        return 0
    client = None
    if not args.no_vcenter:
        try:
            server, user, password = resolve_credentials(args.vcenter, args.user, args.password)
            client = VCenterClient(server, user, password, insecure=args.insecure, log=log)
            client.connect()
        except VCenterError as exc:
            log("  (vCenter checks skipped: {})".format(exc))
            client = None
    log("verifying {} committed VM(s)...".format(len(ws_rows)))
    try:
        result = service.run_verification(ws, ws_rows, client)
    finally:
        if client:
            client.close()
    c = result["counts"]
    log("verified {}: {} ok · {} unverifiable · {} FAILED".format(result["checked"], c.get("ok", 0),
                                                                   c.get("warn", 0), c.get("fail", 0)))
    for line in result["failures"][:30]:
        log("  ! " + line)
    return 0 if not c.get("fail") else 4


def cmd_schedule(args) -> int:
    from . import schedule as sch
    ws = _open_ws(args)
    cfg, store, engine = ws.cfg, ws.store, ws.engine
    actor = store.actor
    if args.action == "list":
        rows = sch.list_schedules(store)
        if not rows:
            log("no change windows")
            return 0
        log(report.table(["id", "stage", "start (UTC)", "end (UTC)", "state", "by", "note"],
                         [[r["id"], r["stage"], r["start_at"], r["end_at"], r["state"], r["created_by"] or "",
                           (r["message"] or "")[:50]] for r in rows], indent="  "))
        return 0
    if args.action == "cancel":
        sch.cancel(store, args.id, actor)
        log("schedule #{} cancelled".format(args.id))
        return 0
    if args.action == "add":
        body = {"waves": args.wave or None, "folders": args.folder or None, "include_failed": args.include_failed,
                "rollback_failed": getattr(args, "rollback_failed", False)}
        start, end = sch.parse_when(args.start), sch.parse_when(args.end)
        needs = access.needs_approval(cfg, args.stage, body)
        s = sch.create(store, args.stage, body, start, end, actor, needs_approval=needs)
        if needs:
            a = access.request(store, args.stage, dict(body, stage=args.stage), actor, schedule_id=s["id"])
            sch.mark(store, s["id"], sch.AWAITING, approval_id=a["id"])
            service.notifier(cfg)("approval_requested", "Approval #{} requested".format(a["id"]),
                                  "Change window #{}: {}".format(s["id"], a["summary"]), {"by": actor}, wait=True)
            log("schedule #{} created; it needs approval #{} from someone else".format(s["id"], a["id"]))
        else:
            log("schedule #{}: {} from {} to {} (UTC)".format(s["id"], args.stage, s["start_at"], s["end_at"]))
        from .estimate import estimate, human
        per_ns: Dict[str, int] = {}
        for w, _n in service.eligible_by_wave(engine, args.stage, args.wave or None, folders=args.folder or None):
            for vm in engine.eligible(args.stage, w, folders=args.folder or None):
                per_ns[vm["namespace"]] = per_ns.get(vm["namespace"], 0) + 1
        eta = estimate(store, cfg, args.stage, per_ns)
        span = (end - start).total_seconds()
        log("  estimate {} for what is eligible now; the window is {}{}".format(
            human(eta["seconds"]), human(span), "" if eta["seconds"] <= span else "  -- it will NOT all fit"))
        return 0
    # tick: run whatever is due now, then return (Task Scheduler / cron entry point)
    for m in sch.sweep_missed(store):
        log("  missed: schedule #{} ({})".format(m["id"], m["message"]))
        service.notifier(cfg)("schedule_missed", "Change window #{} missed".format(m["id"]), m["message"] or "",
                              wait=True)
    due = sch.due(store)
    if not due:
        log("nothing due")
        return 0
    s = due[0]
    try:
        with service.WorkspaceLock(cfg, "schedule #{} (tick)".format(s["id"])):
            result = service.run_schedule(ws, s, log)
    except service.WorkspaceBusy as exc:
        log("not started this tick: {}".format(exc))
        return 7
    log("schedule #{}: {}".format(s["id"], result["message"]))
    return 0


def cmd_settings(args) -> int:
    from . import settings as settings_mod
    cfg_file = _load_config(args)
    _cfg, store, _kube, _engine = _open(args)
    if args.action == "show":
        for row in settings_mod.describe(cfg_file, store):
            log("  {:<36} {:<22} {}".format(row["key"], str(row["value"]),
                                            "(workspace; file: {})".format(row["file_value"]) if row["overridden"] else ""))
        log("")
        log("  notification channels: {}".format(len(_cfg.notify)))
        return 0
    if args.action == "set":
        changes = {}
        for pair in args.pairs:
            key, _, value = pair.partition("=")
            if not _:
                log("use key=value, e.g. batch_size=20")
                return 2
            changes[key.strip()] = value.strip()
        settings_mod.update(store, changes, store.actor)
        log("saved: " + ", ".join(changes))
        return 0
    settings_mod.update(store, {k: None for k in args.pairs}, store.actor)
    log("reset to the config file: " + ", ".join(args.pairs))
    return 0


def cmd_users(args) -> int:
    _cfg, store, _kube, _engine = _open(args)
    if args.action == "list":
        rows = access.list_users(store)
        log(report.table(["user", "role", "created", "by", "disabled", "last seen"],
                         [[u["name"], u["role"], u["created_at"], u["created_by"] or "", "yes" if u["disabled"] else "",
                           u["last_seen"] or ""] for u in rows], indent="  ") if rows else "no users yet")
        return 0
    if args.action == "add":
        token = access.create_user(store, args.name, args.role, store.actor)
        log("{} ({}) -- personal console link (shown once; keep it private):".format(args.name, args.role))
        log("  http://127.0.0.1:<port>/#t={}".format(token))
        return 0
    access.set_disabled(store, args.name, args.action == "disable", store.actor)
    log("{} {}d".format(args.name, args.action))
    return 0


def cmd_approvals(args) -> int:
    from . import schedule as sch
    cfg, store, _kube, _engine = _open(args)
    actor = store.actor
    if args.action == "list":
        rows = access.list_approvals(store, state=None if args.all else "pending")
        if not rows:
            log("no approvals" + ("" if args.all else " pending"))
            return 0
        log(report.table(["id", "stage", "what", "requested by", "at", "state", "decided by"],
                         [[a["id"], a["stage"], a["summary"], a["requested_by"], a["requested_at"], a["state"],
                           a["decided_by"] or ""] for a in rows], indent="  "))
        return 0
    if args.action == "request":
        body = {"waves": args.wave or None, "folders": args.folder or None}
        a = access.request(store, args.stage, body, actor)
        service.notifier(cfg)("approval_requested", "Approval #{} requested".format(a["id"]), a["summary"],
                              {"by": actor}, wait=True)
        log("approval #{} requested: {} -- someone other than {} must approve it".format(a["id"], a["summary"], actor))
        return 0
    a = access.decide(store, args.id, args.action == "approve", actor, args.note or "")
    sch.on_approval(store, a)
    service.notifier(cfg)("approval_decided", "Approval #{} {}".format(a["id"], a["state"]),
                          a["summary"] or "", {"by": actor}, wait=True)
    log("approval #{} {}".format(a["id"], a["state"]))
    return 0


def cmd_serve(args) -> int:
    from .web.api import WebApp
    from .web.server import serve

    cfg = _load_config(args)
    app = WebApp(cfg, config_path=args.config, folder_map=args.folder_map,
                 network_map=args.map, verbose=args.verbose, tag_map=args.tag_map)
    try:
        return serve(app, args.host, args.port, token=args.token,
                     open_browser=args.open, log=log)
    except OSError as exc:
        app.close()
        log("error: cannot listen on {}:{}: {}".format(args.host, args.port, exc))
        return 2


# -------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vcfa-import",
        description="Bulk-import vCenter VMs into VCF Automation namespaces "
                    "via the Mobility Operator (VCF 9.1+).",
    )
    p.add_argument("--version", action="version", version="vcfa-import {}".format(__version__))
    p.add_argument("-c", "--config", help="path to vcfa-import.toml")
    p.add_argument("--workdir", help="override run directory (state, manifests, reports)")
    p.add_argument("--context", help="kubectl context for the Supervisor")
    p.add_argument("--kubeconfig", help="kubeconfig path")
    p.add_argument("-v", "--verbose", action="store_true", help="echo every kubectl invocation")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, func, help_text, **kwargs):
        sp = sub.add_parser(name, help=help_text, description=help_text, **kwargs)
        sp.set_defaults(func=func)
        return sp

    sp = add("init", cmd_init, "write a starter config and inventory template")
    sp.add_argument("--dir", default=".", help="directory to write into (default: .)")
    sp.add_argument("--force", action="store_true")

    sp = add("validate", cmd_validate, "check an inventory CSV without touching the cluster")
    sp.add_argument("-i", "--inventory", required=True)

    sp = add("load", cmd_load, "load/merge the inventory into the run state")
    sp.add_argument("-i", "--inventory", required=True)

    def add_scope(sp):
        """--folder for commands that act on the import queue (not the discovery cache)."""
        sp.add_argument("--folder", action="append", metavar="PATH",
                        help="only VMs whose source folder is this path or beneath it; repeatable")
        sp.add_argument("--no-subfolders", action="store_true",
                        help="--folder matches that folder only, not its subtree")

    def add_filters(sp, with_moref=True):
        sp.add_argument("--name", action="append", metavar="GLOB",
                        help="VM name glob, e.g. 'web-*'; repeatable")
        sp.add_argument("--exclude-name", action="append", metavar="GLOB")
        sp.add_argument("--regex", help="regular expression on the VM name")
        sp.add_argument("--cluster", action="append", metavar="GLOB")
        sp.add_argument("--folder", action="append", metavar="PATH",
                        help="VM folder path, e.g. Production/Web; includes subfolders. "
                             "Globs allowed (* crosses slashes). Repeatable")
        sp.add_argument("--no-subfolders", action="store_true",
                        help="--folder matches that folder only, not its subtree")
        sp.add_argument("--datacenter", action="append", metavar="GLOB")
        sp.add_argument("--network", action="append", metavar="GLOB",
                        help="portgroup the VM is attached to")
        sp.add_argument("--guest-os", action="append", metavar="GLOB")
        sp.add_argument("--powered-on", action="store_true")
        sp.add_argument("--powered-off", action="store_true")
        sp.add_argument("--tools-running", action="store_true")
        sp.add_argument("--with-nics", action="store_true",
                        help="only VMs that have at least one network adapter")
        sp.add_argument("--tag", action="append", metavar="CATEGORY:TAG",
                        help="vCenter tag, e.g. Application:Payroll (globs allowed); repeatable")
        if with_moref:
            sp.add_argument("--vm", action="append", metavar="MOREF", help="repeatable")

    sp = add("discover", cmd_discover,
             "pull the VM inventory from vCenter into the local cache")
    sp.add_argument("--vcenter", help="vCenter host (or set VCFA_VC_SERVER)")
    sp.add_argument("--user", help="vCenter user (or set VCFA_VC_USER)")
    sp.add_argument("--password", help="prefer VCFA_VC_PASSWORD or the interactive prompt")
    sp.add_argument("--insecure", action="store_true", help="skip TLS verification")
    sp.add_argument("--powered-on", action="store_true", help="only powered-on VMs")
    sp.add_argument("--concurrency", type=int, default=12)
    sp.add_argument("--timeout", type=int, default=60)
    sp.add_argument("--no-tools", action="store_true",
                    help="skip the VM Tools check (one fewer call per VM)")
    sp.add_argument("--no-placement", action="store_true",
                    help="skip folder and cluster attribution")
    sp.add_argument("--no-tags", action="store_true", help="skip reading vCenter tags")

    sp = add("browse", cmd_browse, "list the discovered vCenter inventory")
    add_filters(sp)
    sp.add_argument("--selected", action="store_true", help="only VMs already selected")
    sp.add_argument("--facets", action="store_true",
                    help="list the distinct datacenters, clusters, folders and networks")
    sp.add_argument("--folders", action="store_true",
                    help="show the VM folder tree with counts per folder and subtree")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--csv", help="write the matching rows to a CSV instead")
    sp.add_argument("--json", action="store_true")

    sp = add("pick", cmd_pick,
             "write a clickable HTML picker of the discovered VMs")
    add_filters(sp)
    sp.add_argument("--out", help="output path (default: <workdir>/picker.html)")

    sp = add("select", cmd_select, "choose which discovered VMs to import")
    add_filters(sp)
    sp.add_argument("--from-file", help="selection.csv from the picker, or a list of morefs/names")
    sp.add_argument("--all", action="store_true", help="select every discovered VM")
    sp.add_argument("--none", action="store_true", help="clear the whole selection")
    sp.add_argument("--deselect", action="store_true", help="unselect the matches instead")
    sp.add_argument("--namespace", help="assign this target namespace to the matches")
    sp.add_argument("--wave", dest="wave_single", type=int, help="assign this wave to the matches")
    sp.add_argument("--app", help="name the application of the matches ('' clears it)")

    sp = add("stage", cmd_stage,
             "turn the selected VMs into the import queue")
    sp.add_argument("--map", help="portgroup -> namespace/subnet mapping CSV")
    sp.add_argument("--folder-map",
                    help="folder -> namespace[,wave][,group] mapping CSV; most specific folder wins")
    sp.add_argument("--tag-map", help="vCenter tag -> namespace[,wave][,group] mapping CSV")
    sp.add_argument("--default-namespace", help="namespace for VMs no map covers")
    sp.add_argument("--wave", dest="wave_single", type=int, default=1)
    sp.add_argument("--out", help="write an inventory CSV for review instead of staging directly")

    sp = add("preflight", cmd_preflight, "verify CRDs, namespaces, subnets and RBAC")
    sp.add_argument("--skip-targets", action="store_true",
                    help="skip per-namespace and per-subnet existence checks")

    sp = add("plan", cmd_plan, "compute batches and render manifests without applying")
    sp.add_argument("--stage", choices=["precheck", "import"], default="import")
    sp.add_argument("--wave", action="append", type=int)
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--batch-size", type=int)
    sp.add_argument("--include-failed", action="store_true")
    sp.add_argument("--no-write", action="store_true", help="do not write manifest files")
    sp.add_argument("--show", action="store_true", help="print the first manifest")
    sp.add_argument("--server-dry-run", action="store_true",
                    help="validate every manifest against the API server (applies nothing)")

    for name, func, text in (
        ("precheck", cmd_precheck, "run precheckOnly batches to validate VMs against the operator"),
        ("run", cmd_run, "execute the import batches"),
    ):
        sp = add(name, func, text)
        sp.add_argument("--wave", action="append", type=int, help="repeatable; default is all waves")
        sp.add_argument("--limit", type=int, default=0, help="cap VMs per wave (0 = no cap)")
        sp.add_argument("--batch-size", type=int)
        sp.add_argument("--parallel", type=int, help="max in-flight batches")
        sp.add_argument("--vm", action="append",
                        help="restrict to these morefs; repeatable")
        add_scope(sp)
        sp.add_argument("--include-failed", action="store_true",
                        help="also retry VMs currently in a failed state")
        sp.add_argument("--dry-run", action="store_true",
                        help="render and log, but never apply to the cluster")
        sp.add_argument("-y", "--yes", action="store_true")
        if name == "run":
            sp.add_argument("--no-precheck", action="store_true",
                            help="import VMs that have not passed a precheck")
            sp.add_argument("--rollback-failed", action="store_true",
                            help="after the run, hand every failed import back to vCenter "
                                 "(patches rollbackAction onto the failed batches and waits)")
            sp.add_argument("--approval", type=int, metavar="ID",
                            help="an approved request, when require_approval covers import")

    sp = add("status", cmd_status, "show campaign progress")
    add_scope(sp)
    sp.add_argument("--refresh", action="store_true", help="re-poll live batches first")
    sp.add_argument("--failures", type=int, default=10)

    sp = add("watch", cmd_watch, "poll and redraw progress until nothing is in flight")
    sp.add_argument("--interval", type=int, default=30)
    sp.add_argument("--failures", type=int, default=8)
    sp.add_argument("--forever", action="store_true")

    sp = add("commit", cmd_commit, "release VMs held by commitAction: Wait")
    sp.add_argument("--approval", type=int, metavar="ID", help="an approved request (require_approval)")
    sp.add_argument("--wave", dest="wave_single", type=int)
    sp.add_argument("--vm", action="append", help="moref; repeatable")
    sp.add_argument("-y", "--yes", action="store_true")

    sp = add("rollback", cmd_rollback,
             "hand failed imports back to vCenter (sets rollbackAction, waits, optionally deletes)")
    sp.add_argument("--batch", action="append", metavar="NAME", help="batch to roll back; repeatable")
    sp.add_argument("--vm", action="append", metavar="MOREF",
                    help="roll back the batch this VM is in; repeatable")
    sp.add_argument("--failed", action="store_true",
                    help="every batch with failed or uncommitted VMs")
    add_scope(sp)
    sp.add_argument("--wave", dest="wave_single", type=int)
    sp.add_argument("--action", default="Immediate", help="rollbackAction value (default Immediate)")
    sp.add_argument("--no-wait", action="store_true",
                    help="set the action and return without waiting for the operator")
    sp.add_argument("--timeout", type=int, metavar="MIN",
                    help="minutes to wait for the revert (default rollback_timeout_minutes)")
    sp.add_argument("--delete", action="store_true",
                    help="delete each batch once its rollback is confirmed")
    sp.add_argument("--approval", type=int, metavar="ID", help="an approved request (require_approval)")
    sp.add_argument("-y", "--yes", action="store_true")

    sp = add("abandon", cmd_abandon,
             "discard a batch without rollback (precheck-only, or nothing in flight) "
             "and return its VMs to pending")
    sp.add_argument("--batch", action="append", metavar="NAME")
    sp.add_argument("--vm", action="append", metavar="MOREF", help="the batch this VM is in")
    add_scope(sp)
    sp.add_argument("--stage", choices=["precheck", "import"],
                    help="every live batch of this stage")
    sp.add_argument("-y", "--yes", action="store_true")

    sp = add("cleanup", cmd_cleanup,
             "delete batch objects whose rollback the operator has confirmed")
    sp.add_argument("--batch", action="append", metavar="NAME", help="restrict to these; repeatable")
    sp.add_argument("-y", "--yes", action="store_true")

    sp = add("retry", cmd_retry, "return failed VMs to the queue")
    add_scope(sp)
    sp.add_argument("--wave", dest="wave_single", type=int)
    sp.add_argument("--vm", action="append")
    sp.add_argument("--force", action="store_true", help="ignore the max_retries ceiling")

    sp = add("vms", cmd_vms, "list VMs in the run state")
    add_scope(sp)
    sp.add_argument("--state", action="append", choices=report.STATE_ORDER)
    sp.add_argument("--wave", dest="wave_single", type=int)
    sp.add_argument("--namespace")
    sp.add_argument("--vm", action="append")
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--json", action="store_true")

    sp = add("skip", cmd_skip, "exclude (or re-include) VMs from the campaign")
    sp.add_argument("--vm", action="append", required=True)
    sp.add_argument("--unskip", action="store_true")

    sp = add("events", cmd_events, "show the run's event log")
    sp.add_argument("--limit", type=int, default=40)
    sp.add_argument("--level", choices=["info", "warn", "error"])

    sp = add("history", cmd_history,
             "the movement log: every state change, or one VM's full story")
    sp.add_argument("--vm", action="append", metavar="MOREF",
                    help="show this VM's complete history; repeatable")
    sp.add_argument("--state", choices=report.STATE_ORDER, help="only transitions into this state")
    sp.add_argument("--since", metavar="ISO8601", help="e.g. 2026-09-10T00:00:00Z")
    sp.add_argument("--limit", type=int, default=40)
    sp.add_argument("--json", action="store_true")

    sp = add("ledger", cmd_ledger, "export the tracker and the movement log")
    sp.add_argument("--tracker", help="per-VM tracker CSV (source -> target, timings)")
    sp.add_argument("--csv", help="per-transition CSV")
    sp.add_argument("--verify", action="store_true",
                    help="cross-check the JSONL ledger against the database")

    sp = add("report", cmd_report, "write an HTML and/or CSV report")
    sp.add_argument("--html", help="output path for the HTML report")
    sp.add_argument("--csv", help="output path for the per-VM CSV")
    sp.add_argument("--refresh", action="store_true")

    add("namespaces", cmd_namespaces, "list the Supervisor namespaces you can map VMs to")
    add("portgroups", cmd_portgroups, "list vCenter portgroups (as of the last discovery) with VM counts")
    add("subnets", cmd_subnets, "list the Subnets and SubnetSets on the Supervisor you can map portgroups to")

    sp = add("readiness", cmd_readiness, "spot likely precheck failures from vCenter facts (advisory)")
    add_filters(sp)
    sp.add_argument("--selected", action="store_true", help="only VMs already selected")

    sp = add("verify", cmd_verify, "check committed VMs: power, Tools, IP preserved, ping, TCP ports")
    sp.add_argument("--vm", action="append", metavar="MOREF")
    sp.add_argument("--wave", dest="wave_single", type=int)
    sp.add_argument("--unverified", action="store_true", help="only VMs not verified yet")
    sp.add_argument("--no-vcenter", action="store_true", help="network checks only")
    sp.add_argument("--vcenter"); sp.add_argument("--user"); sp.add_argument("--password")
    sp.add_argument("--insecure", action="store_true")

    sp = add("schedule", cmd_schedule, "change windows: run a precheck or import between two times")
    sp.add_argument("action", choices=["add", "list", "cancel", "tick"])
    sp.add_argument("id", nargs="?", type=int, help="schedule id (cancel)")
    sp.add_argument("--stage", choices=["precheck", "import"], default="precheck")
    sp.add_argument("--wave", action="append", type=int)
    sp.add_argument("--folder", action="append", metavar="PATH")
    sp.add_argument("--start", help="e.g. '2026-09-26 22:00' (local time) or ISO 8601")
    sp.add_argument("--end", help="when the window closes; no batch starts that would not finish by then")
    sp.add_argument("--include-failed", action="store_true")
    sp.add_argument("--rollback-failed", action="store_true")

    sp = add("settings", cmd_settings, "workspace settings shared with the web console")
    sp.add_argument("action", choices=["show", "set", "reset"])
    sp.add_argument("pairs", nargs="*", help="set: key=value ...; reset: key ...")

    sp = add("users", cmd_users, "console users: viewer, operator, admin")
    sp.add_argument("action", choices=["add", "list", "disable", "enable"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("--role", choices=list(access.ROLES), default="operator")

    sp = add("approvals", cmd_approvals, "the two-person rule for require_approval stages")
    sp.add_argument("action", choices=["list", "request", "approve", "reject"])
    sp.add_argument("id", nargs="?", type=int)
    sp.add_argument("--stage", choices=["import", "commit", "rollback"], default="import")
    sp.add_argument("--wave", action="append", type=int)
    sp.add_argument("--folder", action="append", metavar="PATH")
    sp.add_argument("--note")
    sp.add_argument("--all", action="store_true", help="list decided ones too")

    sp = add("serve", cmd_serve,
             "run the web console: discover, plan waves, execute and triage from a browser")
    sp.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1; prefer an SSH tunnel "
                         "over exposing it)")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--token", help="access token (default: a fresh random one per start)")
    sp.add_argument("--open", action="store_true", help="open the console in a browser")
    sp.add_argument("--folder-map",
                    help="folder map CSV the console edits (default: folder-map.csv next to "
                         "the config)")
    sp.add_argument("--map", help="portgroup map CSV the console edits (default: "
                                  "portgroup-map.csv next to the config)")
    sp.add_argument("--tag-map", help="vCenter tag map CSV the console edits (default: "
                                      "tag-map.csv next to the config)")

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    # vCenter allows any Unicode VM name. On Windows, output piped to a file or
    # `tee` uses the ANSI code page, and one such name used to crash `vms`,
    # `history` -- or the summary at the end of a `run`. Never die on output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, InventoryError, SelectionError) as exc:
        log("error: {}".format(exc))
        return 2
    except VCenterError as exc:
        log("vCenter error: {}".format(exc))
        return 6
    except KubectlError as exc:
        log("kubectl error: {}".format(exc))
        return 5
    except service.WorkspaceBusy as exc:
        log("refused: {}".format(exc))
        return 7
    except access.AccessError as exc:
        log("refused: {}".format(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 -- schedule/settings user errors
        if type(exc).__name__ in ("ScheduleError",):
            log("error: {}".format(exc))
            return 2
        raise
    except KeyboardInterrupt:
        log("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
