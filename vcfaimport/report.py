"""Progress reporting: terminal tables, CSV export, and a standalone HTML report."""

from __future__ import annotations

import csv
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Sequence

from . import __version__
from .config import Config
from . import state as st

STATE_ORDER = [
    st.S_PENDING,
    st.S_PRECHECK_RUNNING,
    st.S_PRECHECK_PASSED,
    st.S_PRECHECK_FAILED,
    st.S_IMPORTING,
    st.S_AWAITING_COMMIT,
    st.S_COMMITTED,
    st.S_FAILED,
    st.S_ROLLING_BACK,
    st.S_ROLLED_BACK,
    st.S_SKIPPED,
]

STATE_LABEL = {
    st.S_PENDING: "pending",
    st.S_PRECHECK_RUNNING: "precheck running",
    st.S_PRECHECK_PASSED: "precheck passed",
    st.S_PRECHECK_FAILED: "precheck FAILED",
    st.S_IMPORTING: "importing",
    st.S_AWAITING_COMMIT: "awaiting commit",
    st.S_COMMITTED: "committed",
    st.S_FAILED: "FAILED",
    st.S_ROLLING_BACK: "rolling back",
    st.S_ROLLED_BACK: "rolled back",
    st.S_SKIPPED: "skipped",
}

STATE_CLASS = {
    st.S_COMMITTED: "ok",
    st.S_PRECHECK_PASSED: "ok",
    st.S_FAILED: "bad",
    st.S_PRECHECK_FAILED: "bad",
    st.S_AWAITING_COMMIT: "hold",
    st.S_IMPORTING: "busy",
    st.S_PRECHECK_RUNNING: "busy",
    st.S_PENDING: "idle",
    st.S_ROLLING_BACK: "busy",
    st.S_ROLLED_BACK: "hold",
    st.S_SKIPPED: "idle",
}


# --------------------------------------------------------------------- tables
def table(headers: Sequence[str], rows: Sequence[Sequence[Any]], indent: str = "") -> str:
    if not rows:
        return indent + "(none)"
    cols = len(headers)
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(row[i]) if i < len(row) else ""))
    sep = indent + "  ".join("-" * w for w in widths)
    out = [indent + "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)), sep]
    for row in rows:
        out.append(indent + "  ".join(
            str(row[i] if i < len(row) else "").ljust(widths[i]) for i in range(cols)))
    return "\n".join(out)


def bar(done: int, total: int, width: int = 32) -> str:
    if total <= 0:
        return "[" + " " * width + "]   0%"
    filled = int(round(width * done / total))
    pct = 100.0 * done / total
    return "[{}{}] {:3.0f}%".format("#" * filled, "." * (width - filled), pct)


def _wave_rows(store: st.Store) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for wave in store.waves():
        counts = store.counts(wave=wave)
        total = sum(counts.values())
        done = counts.get(st.S_COMMITTED, 0)
        failed = counts.get(st.S_FAILED, 0) + counts.get(st.S_PRECHECK_FAILED, 0)
        active = counts.get(st.S_IMPORTING, 0) + counts.get(st.S_PRECHECK_RUNNING, 0)
        rows.append([
            wave, total, done, failed, active,
            counts.get(st.S_AWAITING_COMMIT, 0),
            counts.get(st.S_PRECHECK_PASSED, 0),
            counts.get(st.S_PENDING, 0),
            bar(done, total, 20),
        ])
    return rows


def _namespace_rows(store: st.Store) -> List[List[Any]]:
    rows: List[List[Any]] = []
    for ns in store.namespaces():
        vms = store.query_vms(namespace=ns)
        total = len(vms)
        done = sum(1 for v in vms if v["state"] == st.S_COMMITTED)
        failed = sum(1 for v in vms if v["state"] in (st.S_FAILED, st.S_PRECHECK_FAILED))
        rows.append([ns, total, done, failed, bar(done, total, 16)])
    return rows


def render_status(store: st.Store, cfg: Config, failures: int = 10) -> str:
    counts = store.counts()
    total = sum(counts.values())
    done = counts.get(st.S_COMMITTED, 0)
    out: List[str] = []
    out.append("VCF Automation import campaign")
    out.append("  context : {}".format(cfg.context or "(kubectl current-context)"))
    out.append("  run id  : {}".format(store.get_meta("run_id") or "-"))
    out.append("  state   : {}".format(store.path))
    out.append("")
    out.append("  overall {}  {} / {} committed".format(bar(done, total), done, total))
    out.append("")
    out.append(table(
        ["state", "vms"],
        [[STATE_LABEL.get(s, s), counts[s]] for s in STATE_ORDER if counts.get(s)],
        indent="  ",
    ))
    out.append("")
    out.append("  By wave")
    out.append(table(
        ["wave", "total", "done", "failed", "active", "hold", "ready", "pending", "progress"],
        _wave_rows(store), indent="  ",
    ))
    out.append("")
    out.append("  By namespace")
    out.append(table(["namespace", "total", "done", "failed", "progress"],
                     _namespace_rows(store), indent="  "))

    live = store.query_batches(states=[st.B_APPLIED, st.B_RUNNING])
    if live:
        out.append("")
        out.append("  Live batches")
        out.append(table(
            ["batch", "namespace", "stage", "vms", "state", "note"],
            [[b["name"], b["namespace"], b["stage"], b["vm_count"], b["state"],
              (b["message"] or "")[:48]] for b in live[:20]],
            indent="  ",
        ))

    bad = store.query_vms(states=[st.S_FAILED, st.S_PRECHECK_FAILED], limit=failures)
    if bad:
        out.append("")
        out.append("  Recent failures (showing {} of {})".format(
            len(bad), len(store.query_vms(states=[st.S_FAILED, st.S_PRECHECK_FAILED]))))
        out.append(table(
            ["vm", "moref", "namespace", "state", "message"],
            [[v["vm_name"], v["moref"], v["namespace"], STATE_LABEL.get(v["state"], v["state"]),
              (v["message"] or "")[:70]] for v in bad],
            indent="  ",
        ))
    return "\n".join(out)


def render_scope_status(store: st.Store, rows: Sequence[Any], label: str,
                        failures: int = 10) -> str:
    """Status for an arbitrary set of queued VMs -- e.g. one folder subtree."""
    counts: dict = {}
    for v in rows:
        counts[v["state"]] = counts.get(v["state"], 0) + 1
    total = len(rows)
    done = counts.get(st.S_COMMITTED, 0)
    out: List[str] = ["Scope: {}".format(label), ""]
    out.append("  {}  {} / {} committed".format(bar(done, total), done, total))
    out.append("")
    out.append(table(["state", "vms"],
                     [[STATE_LABEL.get(s, s), counts[s]] for s in STATE_ORDER if counts.get(s)],
                     indent="  "))
    by_folder: dict = {}
    for v in rows:
        f = by_folder.setdefault(v["src_folder"] or "(root)", {"total": 0, "done": 0, "failed": 0})
        f["total"] += 1
        f["done"] += v["state"] == st.S_COMMITTED
        f["failed"] += v["state"] in (st.S_FAILED, st.S_PRECHECK_FAILED)
    out.append("")
    out.append("  By folder")
    out.append(table(["folder", "total", "done", "failed", "progress"],
                     [[f, c["total"], c["done"], c["failed"], bar(c["done"], c["total"], 16)]
                      for f, c in sorted(by_folder.items())], indent="  "))
    bad = [v for v in rows if v["state"] in (st.S_FAILED, st.S_PRECHECK_FAILED)][:failures]
    if bad:
        out.append("")
        out.append("  Failures")
        out.append(table(["vm", "moref", "folder", "state", "message"],
                         [[v["vm_name"], v["moref"], v["src_folder"] or "(root)",
                           STATE_LABEL.get(v["state"], v["state"]), (v["message"] or "")[:60]]
                          for v in bad], indent="  "))
    return "\n".join(out)


def _duration(start: str, end: str) -> str:
    a, b = st._parse_ts(start), st._parse_ts(end)
    if not a or not b:
        return ""
    secs = (b - a).total_seconds()
    if secs < 60:
        return "{:.0f}s".format(secs)
    if secs < 3600:
        return "{:.0f}m{:02.0f}s".format(secs // 60, secs % 60)
    return "{:.0f}h{:02.0f}m".format(secs // 3600, (secs % 3600) // 60)


# ----------------------------------------------------------------------- csv
TRACKER_COLUMNS = [
    "vm_name", "moref", "namespace", "target_resource", "wave", "group", "mode",
    "state", "attempts", "subnets",
    "src_vcenter", "src_cluster", "src_folder", "src_host", "src_networks",
    "src_power", "src_cpu", "src_memory_mb", "src_tools",
    "precheck_batch", "batch_name", "operation_name", "last_phase", "message",
    "queued_at", "precheck_started_at", "precheck_finished_at",
    "import_started_at", "committed_at", "import_duration", "total_duration",
    "updated_at",
]


def write_csv(store: st.Store, path: str) -> int:
    """The tracker: one fully-attributed row per VM, source through to target."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = store.query_vms()
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(TRACKER_COLUMNS)
        for v in rows:
            subnets = ",".join(
                "{}:{}".format(n.get("device_key"), n.get("subnet"))
                for n in json.loads(v["nics_json"])
            )
            writer.writerow([
                v["vm_name"], v["moref"], v["namespace"], v["target_resource"],
                v["wave"], v["grp"], v["mode"], v["state"], v["attempts"], subnets,
                v["src_vcenter"], v["src_cluster"], v["src_folder"], v["src_host"],
                v["src_networks"], v["src_power"], v["src_cpu"], v["src_memory_mb"],
                v["src_tools"],
                v["precheck_batch"] or "", v["batch_name"] or "", v["operation_name"] or "",
                v["last_phase"] or "", (v["message"] or "").replace("\n", " "),
                v["created_at"], v["precheck_started_at"] or "", v["precheck_finished_at"] or "",
                v["import_started_at"] or "", v["committed_at"] or "",
                _duration(v["import_started_at"], v["committed_at"]),
                _duration(v["created_at"], v["committed_at"]),
                v["updated_at"],
            ])
    return len(rows)


def write_transitions_csv(store: st.Store, path: str) -> int:
    """The full append-only movement log, one row per state change."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = store.transitions()
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "moref", "vm_name", "namespace", "from_state", "to_state",
                         "stage", "batch", "operation", "phase", "wave", "attempt",
                         "held_secs", "message"])
        for r in rows:
            writer.writerow([
                r["ts"], r["moref"], r["vm_name"], r["namespace"], r["from_state"] or "",
                r["to_state"], r["stage"] or "", r["batch"] or "", r["operation"] or "",
                r["phase"] or "", r["wave"], r["attempt"],
                "" if r["held_secs"] is None else r["held_secs"],
                (r["message"] or "").replace("\n", " "),
            ])
    return len(rows)


def render_history(store: st.Store, moref: str) -> str:
    """A single VM's full story, source facts through to target resource."""
    vm = store.get_vm(moref)
    if vm is None:
        return "no VM with moref {} in the run state".format(moref)
    out = ["{}  ({})".format(vm["vm_name"], vm["moref"])]
    out.append("  state      : {}".format(STATE_LABEL.get(vm["state"], vm["state"])))
    out.append("  source     : {}{}{}".format(
        vm["src_vcenter"] or "(vcenter unknown)",
        "  cluster " + vm["src_cluster"] if vm["src_cluster"] else "",
        "  folder " + vm["src_folder"] if vm["src_folder"] else ""))
    if vm["src_networks"]:
        out.append("  networks   : {}".format(vm["src_networks"]))
    if vm["src_cpu"] or vm["src_memory_mb"]:
        out.append("  sizing     : {} vCPU, {} MiB".format(vm["src_cpu"], vm["src_memory_mb"]))
    out.append("  target     : {} / {}".format(
        vm["namespace"], vm["target_resource"] or "(resource name not reported)"))
    subnets = ", ".join("{}->{}".format(n.get("device_key"), n.get("subnet"))
                        for n in json.loads(vm["nics_json"]))
    out.append("  interfaces : {}".format(subnets or "(none)"))
    if vm["committed_at"]:
        out.append("  durations  : import {}, end to end {}".format(
            _duration(vm["import_started_at"], vm["committed_at"]) or "?",
            _duration(vm["created_at"], vm["committed_at"]) or "?"))
    out.append("")
    out.append(table(
        ["when", "from", "to", "stage", "batch", "held", "detail"],
        [[t["ts"], t["from_state"] or "-", t["to_state"], t["stage"] or "",
          (t["batch"] or "")[:26],
          "" if t["held_secs"] is None else "{:.0f}s".format(t["held_secs"]),
          ((t["message"] or t["phase"] or "")[:52])]
         for t in store.transitions(moref=moref)],
        indent="  "))
    return "\n".join(out)


# ---------------------------------------------------------------------- html
HTML_CSS = """
:root{--bg:#f7f7f5;--fg:#1b1b19;--muted:#6b6b66;--card:#ffffff;--line:#e3e3df;
 --ok:#2f7d4f;--bad:#b3261e;--hold:#8a5a00;--busy:#1f5fa8;--idle:#8a8a84;--accent:#3d3d3a}
:root:not([data-theme="light"]) {}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
 --bg:#16161a;--fg:#ececea;--muted:#9a9a95;--card:#1f1f24;--line:#33333a;
 --ok:#67c98d;--bad:#ef8279;--hold:#e0ab4a;--busy:#6fa8e8;--idle:#75757a;--accent:#cfcfc9}}
:root[data-theme="dark"]{--bg:#16161a;--fg:#ececea;--muted:#9a9a95;--card:#1f1f24;--line:#33333a;
 --ok:#67c98d;--bad:#ef8279;--hold:#e0ab4a;--busy:#6fa8e8;--idle:#75757a;--accent:#cfcfc9}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:32px}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 24px;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .n{font-size:26px;font-weight:600;line-height:1.1}
.card .l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-top:4px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.hold{color:var(--hold)}.busy{color:var(--busy)}.idle{color:var(--idle)}
h2{font-size:15px;margin:26px 0 10px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-weight:600;color:var(--muted);font-size:12px}
tr:last-child td{border-bottom:none}
td.msg{white-space:normal;max-width:420px;color:var(--muted);font-size:12px}
.pbar{position:relative;height:8px;border-radius:5px;background:var(--line);min-width:120px;overflow:hidden}
.pbar>span{position:absolute;inset:0 auto 0 0;background:var(--ok);border-radius:5px}
.pbar>span.f{background:var(--bad);left:auto;right:0}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
footer{color:var(--muted);font-size:12px;margin-top:28px}
"""


def _pbar(done: int, failed: int, total: int) -> str:
    if total <= 0:
        return '<div class="pbar"></div>'
    d = 100.0 * done / total
    f = 100.0 * failed / total
    return ('<div class="pbar"><span style="width:{:.1f}%"></span>'
            '<span class="f" style="width:{:.1f}%"></span></div>').format(d, f)


def _rows_html(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "".join("<th>{}</th>".format(html.escape(str(h))) for h in headers)
    body = "".join(
        "<tr>" + "".join(c if str(c).startswith("<") else "<td>{}</td>".format(html.escape(str(c)))
                         for c in row) + "</tr>"
        for row in rows
    )
    return ('<div class="scroll"><table><thead><tr>{}</tr></thead><tbody>{}</tbody>'
            "</table></div>").format(head, body)


def write_html(store: st.Store, cfg: Config, path: str) -> str:
    counts = store.counts()
    total = sum(counts.values())
    done = counts.get(st.S_COMMITTED, 0)
    failed = counts.get(st.S_FAILED, 0) + counts.get(st.S_PRECHECK_FAILED, 0)
    active = counts.get(st.S_IMPORTING, 0) + counts.get(st.S_PRECHECK_RUNNING, 0)
    hold = counts.get(st.S_AWAITING_COMMIT, 0)
    pending = counts.get(st.S_PENDING, 0) + counts.get(st.S_PRECHECK_PASSED, 0)

    cards = [
        ("total VMs", total, ""),
        ("committed", done, "ok"),
        ("failed", failed, "bad" if failed else "idle"),
        ("in flight", active, "busy" if active else "idle"),
        ("awaiting commit", hold, "hold" if hold else "idle"),
        ("remaining", pending, "idle"),
    ]
    cards_html = "".join(
        '<div class="card"><div class="n {}">{}</div><div class="l">{}</div></div>'.format(
            cls, val, html.escape(label))
        for label, val, cls in cards
    )

    wave_rows = []
    for row in _wave_rows(store):
        wave, wtotal, wdone, wfailed = row[0], row[1], row[2], row[3]
        wave_rows.append([
            "wave {}".format(wave), wtotal, wdone, wfailed, row[4], row[5], row[7],
            '<td>{}</td>'.format(_pbar(wdone, wfailed, wtotal)),
        ])

    ns_rows = []
    for ns in store.namespaces():
        vms = store.query_vms(namespace=ns)
        ntotal = len(vms)
        ndone = sum(1 for v in vms if v["state"] == st.S_COMMITTED)
        nfail = sum(1 for v in vms if v["state"] in (st.S_FAILED, st.S_PRECHECK_FAILED))
        ns_rows.append([ns, ntotal, ndone, nfail,
                        '<td>{}</td>'.format(_pbar(ndone, nfail, ntotal))])

    bad_rows = [
        [v["vm_name"], v["moref"], v["namespace"], STATE_LABEL.get(v["state"], v["state"]),
         v["batch_name"] or v["precheck_batch"] or "",
         '<td class="msg">{}</td>'.format(html.escape((v["message"] or "")[:400]))]
        for v in store.query_vms(states=[st.S_FAILED, st.S_PRECHECK_FAILED], limit=200)
    ]

    batch_rows = [
        [b["name"], b["namespace"], b["stage"], b["state"], b["vm_count"],
         b["applied_at"] or "", '<td class="msg">{}</td>'.format(html.escape((b["message"] or "")[:200]))]
        for b in store.query_batches(states=[st.B_APPLIED, st.B_RUNNING, st.B_TIMEDOUT, st.B_PARTIAL])
    ]

    # The tracker proper: what actually moved, from where, to where, how long.
    moved = [v for v in store.query_vms(states=[st.S_COMMITTED],
                                        order="committed_at DESC")]
    moved_rows = [
        [v["vm_name"], v["moref"],
         "{}{}".format(v["src_cluster"] or "?",
                       " / " + v["src_folder"] if v["src_folder"] else ""),
         v["src_networks"] or "",
         v["namespace"], v["target_resource"] or "-",
         v["committed_at"] or "",
         _duration(v["import_started_at"], v["committed_at"])]
        for v in moved[:500]
    ]
    stats = store.transition_stats()
    timing = ""
    if stats.get("import_seconds"):
        t = stats["import_seconds"]
        timing = ("<p class=\"sub\">import duration across {} committed VMs &mdash; "
                  "average {:.0f}s, fastest {:.0f}s, slowest {:.0f}s</p>").format(
            t["count"], t["avg"], t["min"], t["max"])

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    doc = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VCFA import progress</title><style>{css}</style></head><body><div class="wrap">
<h1>VCF Automation import progress</h1>
<p class="sub">context <code>{ctx}</code> &middot; run <code>{run}</code> &middot; generated {gen} &middot; vcfa-import {ver}</p>
<div class="cards">{cards}</div>
<h2>Waves</h2>{waves}
<h2>Namespaces</h2>{ns}
<h2>Moved into VCFA</h2>{timing}{moved}
<h2>Batches needing attention</h2>{batches}
<h2>Failures</h2>{fails}
<footer>Progress bars show committed (green) and failed (red) against the wave total.
The moved table is the tracker: every VM that has actually landed in a namespace,
with where it came from and how long it took. Full per-VM history is in
<code>ledger.jsonl</code> and <code>vcfa-import history --vm &lt;moref&gt;</code>.</footer>
</div></body></html>""".format(
        css=HTML_CSS,
        ctx=html.escape(cfg.context or "(kubectl current-context)"),
        run=html.escape(str(store.get_meta("run_id") or "-")),
        gen=generated,
        ver=__version__,
        cards=cards_html,
        waves=_rows_html(["wave", "total", "committed", "failed", "in flight", "awaiting commit",
                          "pending", "progress"], wave_rows),
        ns=_rows_html(["namespace", "total", "committed", "failed", "progress"], ns_rows),
        batches=_rows_html(["batch", "namespace", "stage", "state", "vms", "applied", "note"],
                           batch_rows),
        timing=timing,
        moved=_rows_html(["vm", "moref", "source cluster / folder", "source networks",
                          "namespace", "target resource", "committed", "took"], moved_rows),
        fails=_rows_html(["vm", "moref", "namespace", "state", "batch", "message"], bad_rows),
    )
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc, encoding="utf-8")
    return str(p)
