#!/usr/bin/env python3
"""Measure the console's rendering cost the way a GPU-less jump box pays it.

    python tools/ui_perf.py                          # every rendering mode, motion off
    python tools/ui_perf.py --motion full --quality full

Runs the console against the fakes (as tools/ui_stress.py does) in headless
Chrome/Edge with GPU compositing disabled -- software rendering, like a VM or
an RDP session without a GPU -- and records a trace while a scripted user
hovers over cards, scrolls, switches pages and sits idle through the live
polling. It reports the CPU time Chrome spent on style, layout, paint, raster
and compositing, per phase and in total. Compare modes side by side.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import ui_stress as U  # noqa: E402

PERF_JS = r"""
  async perf() {
    if (HP.get('quality')) FX.set({ quality: HP.get('quality') });
    FX.set({ motion: HP.get('motion') || 'off' });
    const t = {};
    const phase = async (name, fn) => { const t0 = await realNow(); console.timeStamp('phase:' + name); await fn(); t[name] = Math.round((await realNow()) - t0); };
    await open('overview');
    await sleep(800);
    await phase('hover', async () => {
      for (let i = 0; i < 90; i++) {
        const x = 300 + (i * 53) % 1050, y = 120 + (i * 37) % 700;
        const el = document.elementFromPoint(x, y);
        if (el) el.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX: x, clientY: y }));
        await sleep(33);
      }
    });
    await phase('scroll', async () => {
      for (let i = 0; i < 30; i++) { scrollBy(0, 60); await sleep(33); }
      for (let i = 0; i < 30; i++) { scrollBy(0, -60); await sleep(33); }
    });
    await phase('pages', async () => { for (const id of ['queue', 'execute', 'select', 'waves', 'overview']) await open(id); });
    await phase('idle', async () => { await sleep(8000); });
    H.timings = t;
    H.notes.push('quality=' + (FX.prefs.quality || 'n/a') + ' motion=' + FX.prefs.motion + ' dots=' + FX.stats.dots);
    check('perf run finished', true);
  },
"""

# Renderer + compositor work, by the event names Chrome's trace uses for it.
BUCKETS = {
    "style": ("UpdateLayoutTree", "RecalculateStyles"),
    "layout": ("Layout",),
    "paint": ("Paint", "PaintImage", "PrePaint"),
    "raster": ("RasterTask", "TileManager::FlushAndIssueSignals"),
    # Display::DrawAndSwap is one composited frame; the renderer's DrawFrame runs inside it.
    "composite": ("Display::DrawAndSwap",),
    "script": ("FunctionCall", "EvaluateScript", "TimerFire", "FireAnimationFrame", "EventDispatch"),
}


def analyse(trace_path: Path):
    data = json.loads(trace_path.read_text(encoding="utf-8", errors="replace"))
    events = data["traceEvents"] if isinstance(data, dict) else data
    totals = defaultdict(float)
    names = defaultdict(float)
    frames = []
    for e in events:
        if e.get("ph") != "X" or "dur" not in e:
            continue
        if e.get("name") == "Display::DrawAndSwap":
            frames.append(e["dur"] / 1000.0)
        for bucket, keys in BUCKETS.items():
            if e.get("name") in keys:
                totals[bucket] += e["dur"] / 1000.0
                names[e["name"]] += e["dur"] / 1000.0
    frames.sort()
    totals["_frames"] = len(frames)
    totals["_frame_avg"] = sum(frames) / len(frames) if frames else 0
    totals["_frame_p90"] = frames[int(len(frames) * 0.9)] if frames else 0
    return totals, names


def work(totals):
    return sum(v for k, v in totals.items() if not k.startswith("_"))


def run(chrome, base, profile, trace, quality, motion, size):
    q = "?s=perf&t={}&motion={}{}".format(U.TOKEN, motion, "&quality=" + quality if quality else "")
    url = base + "/static/h_index.html" + q + "#/overview"
    cmd = [chrome, "--headless=new", "--disable-gpu", "--disable-gpu-compositing", "--no-first-run",
           "--no-default-browser-check", "--user-data-dir=" + str(profile),
           "--window-size={},{}".format(*size),
           "--trace-startup=devtools.timeline,disabled-by-default-devtools.timeline,viz,cc,blink",
           "--trace-startup-file=" + str(trace), "--trace-startup-format=json", "--trace-startup-duration=120",
           "--virtual-time-budget=60000", "--dump-dom", url]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    m = re.search(r'<pre id="h-result"[^>]*>(.*?)</pre>', proc.stdout, re.S)
    res = json.loads(U.html.unescape(m.group(1))) if m else {"errors": ["no result"], "timings": {}, "notes": []}
    for _ in range(40):             # the trace file is flushed as the browser exits
        if trace.exists() and trace.stat().st_size > 0:
            break
        time.sleep(0.25)
    return res, time.time() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quality", action="append", help="rendering mode(s) to measure (default: full, lite)")
    ap.add_argument("--motion", default="off", choices=["off", "calm", "full"])
    ap.add_argument("--vms", type=int, default=200)
    ap.add_argument("--chrome")
    ap.add_argument("--keep", help="keep traces in this directory")
    args = ap.parse_args()
    chrome = args.chrome or U.find_chrome()
    if not chrome:
        print("no Chrome or Edge found")
        return 2
    U.HARNESS_JS = U.HARNESS_JS.replace("const SCENARIOS = {", "const SCENARIOS = {\n" + PERF_JS, 1)
    base_tmp = Path(args.keep).resolve() if args.keep else Path(tempfile.mkdtemp(prefix="vcfa-perf-"))
    base_tmp.mkdir(parents=True, exist_ok=True)
    (base_tmp / "harness").mkdir(exist_ok=True)
    U.install_harness(base_tmp / "harness")
    ws = base_tmp / "ws"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir()
    saved = dict(os.environ)
    print("building the workspace ({} VMs)...".format(args.vms), flush=True)
    app, vc = U.build(ws, args.vms)
    srv, base = U.serve(app)
    rows = []
    try:
        for quality in (args.quality or ["full", "lite"]):
            trace = base_tmp / "trace-{}-{}.json".format(quality, args.motion)
            if trace.exists():
                trace.unlink()
            res, took = run(chrome, base, base_tmp / ("p-" + quality), trace, quality, args.motion, (1440, 900))
            if not trace.exists():
                print("  {}: no trace written".format(quality))
                continue
            totals, names = analyse(trace)
            rows.append((quality, totals, res))
            print("\n[{}] motion={}  ({:.0f}s)  {}".format(quality, args.motion, took, " ".join(res.get("notes", [])[:1])))
            for e in res.get("errors", []):
                print("   ! " + e[:300])
            for k in BUCKETS:
                print("   {:<10} {:>9.0f} ms".format(k, totals.get(k, 0)))
            print("   {:<10} {:>9.0f} ms".format("TOTAL", work(totals)))
            print("   frames {:.0f}: {:.1f} ms each on average, {:.1f} ms at p90".format(
                totals["_frames"], totals["_frame_avg"], totals["_frame_p90"]))
            top = sorted(names.items(), key=lambda kv: -kv[1])[:6]
            print("   top: " + ", ".join("{} {:.0f}ms".format(n, v) for n, v in top))
    finally:
        srv.shutdown()
        app.close()
        vc.shutdown()
        os.environ.clear()
        os.environ.update(saved)
    if len(rows) > 1:
        base_total = work(rows[0][1]) or 1
        print()
        for quality, totals, _ in rows:
            print("  {:<6} {:>8.0f} ms  ({:.0%} of {})   frame avg {:.1f} ms".format(
                quality, work(totals), work(totals) / base_total, rows[0][0], totals["_frame_avg"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
