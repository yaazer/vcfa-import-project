#!/usr/bin/env python3
"""Stress the web console's UI in a real browser (headless Chrome or Edge).

    python tools/ui_stress.py                 # every scenario, 200 VMs
    python tools/ui_stress.py --only execute  # one scenario
    python tools/ui_stress.py --scale         # add the 1800-VM rendering run

The console runs in-process against tools/fake_vcenter.py and
tools/fake_kubectl.py, seeded with a realistic campaign: committed, failed and
pending VMs across waves, plus VMs whose names and messages are hostile HTML.
A harness script is injected (test-only, by patching the static loader) that
drives the real UI -- clicks, typing, drag and drop, modals -- and asserts on
the result through the API. Each scenario reports its checks, any uncaught
browser error, and render timings.

Needs PyYAML (the fake kubectl) and Chrome or Edge.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import fake_vcenter  # noqa: E402
from vcfaimport.config import Config  # noqa: E402
from vcfaimport.web import server as web_server  # noqa: E402
from vcfaimport.web.api import WebApp  # noqa: E402

TOKEN = "ui-stress-token"

HOSTILE = [
    ('vm-9001', '<img src=x onerror="window.__pwned=1">'),
    ('vm-9002', '<b data-evil=1>bold</b>'),
    ('vm-9003', "O'Brien \"quoted\" `tick` & amp"),
    ('vm-9004', '</script><script>window.__pwned=2</script>'),
    ('vm-9005', 'ünïcödé-名前-✓-\u202eevil'),
    ('vm-9006', 'x' * 180),
]
HOSTILE_MESSAGE = '<img src=x onerror="window.__pwned=3"> operator said <b data-evil=1>no</b>'

CHROME_CANDIDATES = [
    os.environ.get("CHROME", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_chrome():
    for c in CHROME_CANDIDATES:
        if c and Path(c).is_file():
            return c
    for name in ("google-chrome", "chromium", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------- harness JS
HARNESS_JS = r"""
'use strict';
const H = { scenario: null, errors: [], checks: [], timings: {}, notes: [], done: false };
window.addEventListener('error', (e) => H.errors.push('error: ' + e.message + ' @' + (e.filename || '') + ':' + (e.lineno || '')));
window.addEventListener('unhandledrejection', (e) => H.errors.push('rejection: ' + ((e.reason && e.reason.message) || e.reason)));
const HP = new URLSearchParams(location.search);
H.scenario = HP.get('s');
store.set('vcfa-token', HP.get('bad') ? 'wrong-token' : HP.get('t'));
if (HP.get('theme')) store.set('vcfa-theme', HP.get('theme'));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function flush() {
  let el = document.getElementById('h-result');
  if (!el) { el = document.createElement('pre'); el.id = 'h-result'; el.hidden = true; document.body.appendChild(el); }
  el.textContent = JSON.stringify(H);
}
function check(name, cond, detail) {
  H.checks.push({ name, ok: !!cond, detail: detail === undefined ? '' : String(detail).slice(0, 400) });
  flush();
}
async function waitFor(fn, ms, step) {
  ms = ms || 15000; step = step || 100;
  const t0 = performance.now();
  while (performance.now() - t0 < ms) {
    try { const v = await fn(); if (v) return v; } catch (e) { /* not yet */ }
    await sleep(step);
  }
  return null;
}
// Real elapsed time comes from the server: the page's own clock is virtual
// in headless mode and does not count the time spent rendering.
async function realNow() { return (await GET('/api/h_now')).t; }
async function open(id, query) {
  const t0 = await realNow();
  location.hash = '#/' + id + (query ? '?' + query : '');
  // Same route, new parameters (activity -> activity?tab=jobs) must wait for the router too.
  const want = JSON.stringify(Object.fromEntries(new URLSearchParams(query || '')));
  const ok = await waitFor(() => S.route === id && JSON.stringify(S.params) === want && S.view && S.view.loaded && !document.querySelector('#view .boot'), 30000, 5);
  H.timings['open ' + id + (query ? '?' + query : '')] = Math.round((await realNow()) - t0);
  return ok;
}
const brokeView = () => { const h = document.querySelector('#view .callout.bad h3'); return h && h.textContent === 'Something went wrong' ? document.querySelector('#view .callout.bad p').textContent : ''; };
function overflowers() {
  // the outermost elements poking past the viewport, outside any scroll container
  const out = [];
  for (const el of document.querySelectorAll('#view *, #topbar *')) {
    const r = el.getBoundingClientRect();
    if (r.right <= innerWidth + 2 || !r.width) continue;
    let p = el.parentElement, clipped = false;
    while (p && p !== document.body) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') { clipped = true; break; }
      p = p.parentElement;
    }
    if (clipped) continue;
    const parentOver = el.parentElement && el.parentElement.getBoundingClientRect().right > innerWidth + 2;
    if (!parentOver || el.parentElement.id === 'view') out.push(el.tagName.toLowerCase() + '.' + [...el.classList].join('.') + ' ' + Math.round(r.right));
  }
  return out.slice(0, 6).join(' | ');
}
const noInjection = () => !window.__pwned && !document.querySelector('[data-evil]') && !document.querySelector('img[src="x"]');
function click(sel, root) {
  const el = typeof sel === 'string' ? (root || document).querySelector(sel) : sel;
  if (!el) { H.notes.push('missing ' + sel); flush(); return false; }
  el.click();
  return true;
}
function type(el, value) {
  if (typeof el === 'string') el = document.querySelector(el);
  el.focus(); el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}
// Jobs are told apart by id, not time: headless Chrome's virtual clock runs
// ahead of the server's real one.
async function jobIds() { return new Set((await GET('/api/jobs')).jobs.map((j) => j.id)); }
async function waitJob(kind, known) {
  return waitFor(async () => {
    const jobs = (await GET('/api/jobs')).jobs.filter((j) => j.kind === kind && !known.has(j.id));
    return jobs.length && jobs[0].status !== 'running' ? jobs[0] : null;
  }, 180000, 400);
}
const counts = async () => (await GET('/api/overview')).counts;

const SCENARIOS = {
  async pages() {
    for (const id of ['overview', 'discover', 'select', 'stage', 'waves', 'execute', 'queue', 'batches', 'triage', 'activity']) {
      const ok = await open(id);
      check('renders ' + id, ok);
      check('no error box on ' + id, !brokeView(), brokeView());
      check('no markup injected on ' + id, noInjection());
      if (HP.get('narrow')) check('no page-level horizontal scroll on ' + id, document.documentElement.scrollWidth <= innerWidth + 2, document.documentElement.scrollWidth + ' > ' + innerWidth + ': ' + overflowers());
    }
    for (const tab of ['jobs', 'events', 'movement', 'exports']) {
      await open('activity', 'tab=' + tab);
      check('activity tab ' + tab + ' renders', !brokeView() && document.querySelector('.tabs button.on').textContent.length > 0);
    }
  },

  async hostile() {
    await open('select');
    type('#sel-q', 'bold');
    await sleep(400);
    const cell = await waitFor(() => [...document.querySelectorAll('#view .vm-name')].find((n) => n.textContent.includes('<b data-evil=1>bold</b>')));
    check('a name made of HTML is shown as text in Select', cell);
    check('and not rendered as markup', noInjection());
    await open('queue');
    type('#q-q', 'onerror');
    await sleep(400);
    const row = document.querySelector('#view tbody tr[data-act="vm"]');
    check('hostile VM is in the queue', row);
    if (row) {
      row.click();
      const h2 = await waitFor(() => document.querySelector('.drawer h2'));
      check('drawer title is the literal name', h2 && h2.textContent.includes('onerror='), h2 && h2.textContent);
      check('hostile operator message is shown as text', [...document.querySelectorAll('.drawer .msgbox')].some((m) => m.textContent.includes('<b data-evil=1>no</b>')));
      check('nothing injected by the drawer', noInjection());
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      check('Escape closes the drawer', await waitFor(() => !document.querySelector('.drawer')));
    }
    await open('triage');
    check('triage shows the hostile message as text', document.querySelector('#view .msgbox') && document.querySelector('#view').textContent.includes('<b data-evil=1>no</b>'));
    await open('waves');
    click('[data-act="expand"][data-on="1"]');
    await sleep(300);
    check('waves board escapes names', noInjection());
    await open('stage');
    check('stage preview escapes names', noInjection());
  },

  async select() {
    await open('select');
    const sel = async () => (await GET('/api/discovered')).vms.filter((v) => v.selected).length;
    const before = await sel();
    const dmz = document.querySelector('input[data-act="treeSel"][data-path="DMZ"]');
    check('folder tree has DMZ', dmz);
    const dmzCount = +document.querySelector('.tnode[data-path="DMZ"] .cnt').textContent.split('/').pop().replace(/\D/g, '');
    dmz.click();
    check('ticking a folder selects its whole subtree', await waitFor(async () => (await sel()) === before + dmzCount), (await sel()) + ' vs ' + (before + dmzCount));
    document.querySelector('input[data-act="treeSel"][data-path="DMZ"]').click();
    check('unticking deselects it again', await waitFor(async () => (await sel()) === before));

    document.querySelector('.tnode[data-path="DMZ"]').click();
    await sleep(150);
    const boxes = () => [...document.querySelectorAll('#view tbody input[data-act="sel"]')];
    check('clicking a folder filters the table', boxes().length === dmzCount, boxes().length);
    boxes()[0].click();
    await sleep(150);
    boxes()[4].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, shiftKey: true }));
    check('shift-click selects the whole range', await waitFor(async () => (await sel()) === before + 5), (await sel()) - before);
    const t0 = performance.now();
    type('#sel-q', 'zzz-no-such-vm');
    await waitFor(() => boxes().length === 0, 3000, 20);
    H.timings['search to empty'] = Math.round(performance.now() - t0);
    check('search narrows to nothing', boxes().length === 0);
    type('#sel-q', '');
    click('.tnode[data-all]');
    await sleep(200);
    check('pager present when there are many VMs', document.querySelector('.pager'));
    const next = document.querySelector('.pager [data-act="page"][data-page="1"]');
    if (next) { next.click(); await sleep(150); check('page 2 renders rows', boxes().length > 0); }
    // tidy up: deselect the DMZ VMs we picked
    await POST('/api/select', { morefs: (await GET('/api/discovered')).vms.filter((v) => v.folder === 'DMZ').map((v) => v.moref), selected: false });
    check('select page never broke', !brokeView());
  },

  async stage() {
    await open('stage');
    const recs = () => S.view.preview.records;
    const n0 = recs().length;
    check('preview lists the selected VMs', n0 > 0, n0);
    const rowsBefore = S.view.fr.length;
    click('[data-act="addRow"][data-m="f"]:not([data-key])');
    await sleep(100);
    const i = rowsBefore;
    type('#f-' + i + '-folder', 'Production');
    type('#f-' + i + '-namespace', 'zz-stress-ns');
    check('a new map row re-previews live', await waitFor(() => recs().some((r) => r.namespace === 'zz-stress-ns'), 8000), recs().map((r) => r.namespace).slice(0, 3));
    check('coverage reflects the unsaved row', await waitFor(() => document.querySelector('#cov-folders').textContent.includes('zz-stress-ns')));
    check('unsaved-edits warning shows', document.querySelector('#stage-dirty').textContent.includes('unsaved'));
    type('#f-' + i + '-wave', 'soon');
    check('a bad wave is reported, not crashed', await waitFor(() => document.querySelector('#stage-preview .note.bad'), 8000));
    click('[data-act="delRow"][data-m="f"][data-i="' + i + '"]');
    check('removing the row restores the preview', await waitFor(() => !recs().some((r) => r.namespace === 'zz-stress-ns') && !document.querySelector('#stage-preview .note.bad'), 8000));
    const mapsBefore = (await GET('/api/maps')).folder.rows.length;
    click('[data-act="saveMaps"]');
    await sleep(600);
    check('saving unchanged maps keeps them intact', (await GET('/api/maps')).folder.rows.length === mapsBefore);
    check('stage view never broke', !brokeView());
  },

  async waves() {
    await open('waves');
    click('[data-act="expand"][data-on="1"]');
    await sleep(200);
    const src = document.querySelector('.vmrow[draggable="true"]');
    check('there is a movable VM', src);
    if (!src) return;
    const moref = src.dataset.moref;
    const target = +document.querySelector('.wcol.new').dataset.dropwave;
    const dt = new DataTransfer();
    src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt }));
    const col = document.querySelector('.wcol.new');
    col.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt }));
    check('drop target highlights', col.classList.contains('drop'));
    col.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }));
    const vmWave = async () => (await GET('/api/vms/' + encodeURIComponent(moref))).vm.wave;
    check('dragging a VM to "New wave" moves it', await waitFor(async () => (await vmWave()) === target, 8000), await vmWave());
    check('a new column appears for it', await waitFor(() => document.querySelector('.wcol[data-dropwave="' + target + '"] .wave-no')));
    // drag a whole folder group back into wave 1
    click('[data-act="expand"][data-on="1"]');
    await sleep(200);
    const grp = document.querySelector('.wcol[data-dropwave="' + target + '"] .fgroup-h[data-drag="group"]');
    const dt2 = new DataTransfer();
    grp.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: dt2 }));
    const w1 = document.querySelector('.wcol[data-dropwave="1"]');
    w1.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: dt2 }));
    w1.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt2 }));
    check('dragging a folder group moves it back', await waitFor(async () => (await vmWave()) === 1, 8000));
    // swapping a wave that has committed VMs must be refused with an explanation
    const lockedCol = [...document.querySelectorAll('.wcol[data-dropwave]')].find((c) => /locked/.test(c.textContent));
    if (lockedCol) {
      const btn = lockedCol.querySelector('[data-act="swap"]:not([disabled])');
      if (btn) {
        btn.click();
        const t = await waitFor(() => [...document.querySelectorAll('.toast.bad')].find((x) => /cannot be swapped/.test(x.textContent)), 8000);
        check('swapping a wave with locked VMs is refused, explained', t);
      }
    }
    // selection + "Move to wave" via the modal
    const pick = document.querySelector('.vmrow input[data-act="pick"]:not([disabled])');
    pick.click();
    await sleep(100);
    click('[data-act="moveSel"]');
    const input = await waitFor(() => document.querySelector('.modal input[data-field="wave"]'));
    check('move-to-wave asks for a wave', input);
    type(input, '0');
    click('.modal [data-m="yes"]');
    check('wave 0 is rejected with a warning', await waitFor(() => [...document.querySelectorAll('.toast.warn')].some((x) => /1 or higher/.test(x.textContent)), 5000));
    check('waves view never broke', !brokeView() && noInjection());
  },

  async execute() {
    await open('execute');
    let since = await jobIds();
    click('[data-act="preflight"]');
    const pf = await waitJob('preflight', since);
    check('preflight ran from the button', pf && pf.status === 'succeeded', pf && pf.status);
    check('its checklist renders', await waitFor(() => document.querySelectorAll('.checks .checkrow.ok').length >= 3, 15000));

    await open('execute', 'stage=precheck&waves=2');
    const before = await counts();
    check('the plan shows batches for wave 2', await waitFor(() => document.querySelector('#ex-plan table'), 8000));
    click('[data-act="start"]');
    const yes = await waitFor(() => document.querySelector('.modal [data-m="yes"]'));
    check('precheck asks for confirmation', yes);
    since = await jobIds();
    yes.click();
    check('the log dock opens', await waitFor(() => !document.querySelector('#dock').hidden, 5000));
    const job = await waitJob('execute', since);
    check('precheck job finished', job && ['succeeded', 'warning'].includes(job.status), job && job.status);
    const after = await counts();
    check('VMs moved out of pending', (after.pending || 0) < (before.pending || 0), JSON.stringify(after));
    check('the dock shows the finished job', await waitFor(() => /finished|warnings/.test(document.querySelector('#dock').textContent), 8000));

    await open('execute', 'stage=import&waves=2');
    await waitFor(() => document.querySelector('[data-act="start"]:not([disabled])'), 8000);
    click('[data-act="start"]');
    const typed = await waitFor(() => document.querySelector('#modal-type'));
    check('import demands a typed confirmation', typed);
    const go = document.querySelector('.modal [data-m="yes"]');
    check('confirm starts disabled', go.disabled);
    type(typed, 'import');
    check('lower-case does not count', go.disabled);
    type(typed, 'IMPORT');
    check('exact word enables it', !go.disabled);
    since = await jobIds();
    go.click();
    const imp = await waitJob('execute', since);
    check('import job finished', imp && ['succeeded', 'warning'].includes(imp.status), imp && imp.status);
    check('execute view never broke', !brokeView());
  },

  async drawers() {
    await open('queue');
    click('#view tbody tr[data-act="vm"]');
    check('VM drawer shows history', await waitFor(() => document.querySelectorAll('.drawer .timeline .tl').length > 0));
    const b = document.querySelector('.drawer [data-act="batch"]');
    if (b) { b.click(); check('batch drawer opens from the VM drawer', await waitFor(() => document.querySelector('.drawer h2.mono'))); }
    click('.drawer [data-act="closeDrawer"]');
    check('close button closes', await waitFor(() => !document.querySelector('.drawer')));
    await open('batches');
    click('#view tbody tr[data-act="batch"]');
    check('batch drawer shows the manifest', await waitFor(() => [...document.querySelectorAll('.drawer pre')].some((p) => p.textContent.includes('ImportOperationBatch'))));
    click('.overlay');
    check('clicking outside closes', await waitFor(() => !document.querySelector('.drawer')));
    await open('triage');
    const retry = document.querySelector('[data-act="gRetry"]');
    if (retry) {
      retry.click();
      check('retry asks first', await waitFor(() => document.querySelector('.modal')));
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      check('Escape cancels the modal', await waitFor(() => !document.querySelector('.modal')));
    }
  },

  async resilience() {
    const real = window.fetch.bind(window);
    let mode = 'down';
    window.fetch = (u, o) => {
      const url = String(u);
      if (mode === 'down' && url.includes('/api/')) return Promise.reject(new TypeError('Failed to fetch'));
      if (mode === '500' && url.includes('/api/triage')) return Promise.resolve(new Response('{"error":"boom from the server"}', { status: 500, headers: { 'Content-Type': 'application/json' } }));
      if (mode === '502' && url.includes('/api/batches')) return Promise.resolve(new Response('<html>Bad Gateway</html>', { status: 502, statusText: 'Bad Gateway' }));
      return real(u, o);
    };
    location.hash = '#/queue';
    check('an unreachable console shows a clear message', await waitFor(() => /not reachable/.test(document.querySelector('#view').textContent), 10000));
    check('a connection-lost banner appears', await waitFor(() => !document.querySelector('#offline').hidden, 15000));
    await sleep(5000);
    mode = 'up';
    const seen = S.pulse;
    check('the pulse recovers once the server is back', await waitFor(() => S.pulse !== seen, 15000));
    check('the banner clears on reconnect', await waitFor(() => document.querySelector('#offline').hidden, 15000));
    check('pages load again', await open('queue'));
    mode = '500';
    location.hash = '#/triage';
    check('a server error is shown, not thrown', await waitFor(() => /boom from the server/.test(document.querySelector('#view').textContent), 10000));
    mode = '502';
    location.hash = '#/batches';
    check('a proxy error page (non-JSON) is handled', await waitFor(() => /502/.test(document.querySelector('#view').textContent), 10000));
    mode = 'up';
    check('and everything works afterwards', await open('overview'));
  },

  async token() {
    check('a wrong token shows the gate', await waitFor(() => document.querySelector('#token-in'), 10000));
    type('#token-in', 'http://127.0.0.1:1/#t=' + HP.get('t'));
    click('[data-act="setToken"]');
    check('pasting the whole link works', await waitFor(() => S.info && S.view && S.view.loaded, 15000));
    check('the gate is gone', !document.querySelector('#token-in'));
  },

  async theme() {
    await open('overview');
    const bg = () => getComputedStyle(document.body).backgroundColor;
    const accent = () => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    const before = bg();
    click('[data-act="theme"]');
    check('the palette button opens the Theme Studio', await waitFor(() => document.querySelector('#fx-studio .swatch')));
    check('every theme is offered', document.querySelectorAll('#fx-studio .swatch').length === FX.THEMES.length);
    click('#fx-studio .swatch[data-id="glacier"]');
    check('a light theme switches the whole palette', await waitFor(() => bg() !== before) && document.documentElement.style.colorScheme === 'light');
    const a0 = accent();
    const hue = document.querySelector('#fx-studio input[data-fx-in="shift"]');
    hue.value = '90'; hue.dispatchEvent(new Event('input', { bubbles: true }));
    check('the hue slider re-tints live', accent() !== a0, a0 + ' -> ' + accent());
    check('status colours keep their meaning under a hue shift', /152/.test(getComputedStyle(document.documentElement).getPropertyValue('--s-committed')));
    click('#fx-studio [data-fx="motion"][data-k="off"]');
    check('motion Off is applied to the page', document.body.dataset.motion === 'off');
    check('and remembered', JSON.parse(store.get('vcfa-fx')).motion === 'off');
    click('#fx-studio [data-fx="resetFx"]');
    check('reset restores Aurora', await waitFor(() => bg() === before) && FX.prefs.theme === 'aurora' && FX.prefs.shift === 0);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    check('Escape closes the studio', await waitFor(() => !document.querySelector('#fx-studio')));
  },

  async palette() {
    await open('overview');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }));
    const q = await waitFor(() => document.getElementById('cmdk-q'));
    check('Ctrl+K opens the command palette', q);
    type(q, 'triag');
    await sleep(50);
    check('it finds pages as you type', /Triage/.test(document.querySelector('.cmdk-item.on').textContent));
    q.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    check('Enter runs the highlighted item', await waitFor(() => S.route === 'triage' && !document.getElementById('cmdk-q')));
    document.dispatchEvent(new KeyboardEvent('keydown', { key: '/', bubbles: true }));
    const q2 = await waitFor(() => document.getElementById('cmdk-q'));
    check('/ opens it too', q2);
    type(q2, 'web-0');
    const vm = await waitFor(() => [...document.querySelectorAll('.cmdk-item')].find((i) => /web-0/.test(i.textContent)), 8000);
    check('it finds VMs by name', vm);
    const secs = [...document.querySelectorAll('.cmdk-sec')].map((x) => x.textContent);
    check('sections are never repeated', new Set(secs).size === secs.length, secs.join(','));
    if (vm) { vm.click(); check('picking a VM opens its drawer', await waitFor(() => document.querySelector('.drawer h2'))); }
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    type(q2.isConnected ? q2 : document.createElement('input'), '');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }));
    const q3 = await waitFor(() => document.getElementById('cmdk-q'));
    type(q3, 'zzqqxx');
    check('no match says so', await waitFor(() => document.querySelector('.cmdk-empty')));
    q3.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    check('Escape closes it', await waitFor(() => !document.getElementById('cmdk-q')));
  },

  async stream() {
    const pixels = () => {
      const cv = document.querySelector('#fx-stream canvas');
      if (!cv || !cv.width) return 0;
      const d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data;
      let lit = 0;
      for (let i = 3; i < d.length; i += 16) if (d[i] > 200) lit++;
      return lit;
    };
    for (const m of ['off', 'full']) {
      FX.set({ motion: m });
      await open(m === 'off' ? 'queue' : 'batches');
      await open('overview');
      check('[' + m + '] the stream canvas mounts', await waitFor(() => document.querySelector('#fx-stream canvas')));
      check('[' + m + '] a particle per VM (or per unit at scale)', FX.stats.dots > 0, FX.stats.dots);
      // Headless Chrome's virtual clock barely runs requestAnimationFrame, so
      // frames are driven by hand; the animation itself is time-based.
      check('[' + m + '] particles are drawn solid, not faded out', await waitFor(() => { FX.drawNow(); return pixels() > 50; }, 8000), pixels());
    }
    const lanes = [...document.querySelectorAll('.lane-h .ln-n')].map((x) => +x.dataset.count);
    const c = (await GET('/api/overview')).counts;
    check('lane totals add up to the estate', lanes[4] === (c.committed || 0), lanes.join(',') + ' vs committed ' + c.committed);
    await open('execute');
    check('the same stream follows you to Execute', await waitFor(() => { FX.drawNow(); return document.querySelector('#fx-stream canvas') && pixels() > 50; }, 8000));
    check('Execute uses the compact form (no ring)', !document.querySelector('#view .hero-ring'));
    check('a frame costs little', FX.stats.drawMs < 8, FX.stats.drawMs.toFixed(2) + 'ms');
    FX.set({ motion: 'full' });
  },

  async scale() {
    for (const id of ['overview', 'select', 'queue', 'waves', 'batches', 'triage', 'stage', 'execute']) {
      check('renders ' + id + ' at scale', await open(id));
      check('no error box on ' + id, !brokeView(), brokeView());
    }
    const timeIt = async (name, fn) => { const t0 = await realNow(); await fn(); H.timings[name] = Math.round((await realNow()) - t0); };
    check('the queue really holds 1800 VMs', (await GET('/api/vms')).vms.length >= 1800);
    await open('select');
    await timeIt('select: select-all 1800 + render', async () => { click('[data-act="selMatching"][data-on="0"]'); await sleep(0); });
    await timeIt('select: filter + render', async () => { S.view.f.q = 'db-'; S.view.page = 0; S.view.render(); });
    await timeIt('select: clear filter + render', async () => { S.view.f.q = ''; S.view.render(); });
    await timeIt('select: shift-click 100 rows', async () => {
      const boxes = [...document.querySelectorAll('#view tbody input[data-act="sel"]')];
      boxes[0].click(); await sleep(0);
      boxes[99].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, shiftKey: true }));
      await waitFor(() => document.querySelectorAll('#view tbody tr.on').length >= 100, 10000, 5);
    });
    await open('waves');
    await timeIt('waves: expand all + render', async () => { click('[data-act="expand"][data-on="1"]'); });
    check('the expanded board renders every VM', document.querySelectorAll('.vmrow').length >= 1800, document.querySelectorAll('.vmrow').length);
    await timeIt('waves: search + render', async () => { S.view.q = 'svc-01'; S.view.render(); });
    await timeIt('waves: re-render expanded', async () => { S.view.q = ''; S.view.render(); });
    await open('queue');
    await timeIt('queue: sort by state + render', async () => { click('#view th[data-key="state"]'); });
    await timeIt('queue: filter chips + render', async () => { click('#view .chip[data-s="pending"]'); });
    await open('overview');
    check('1800 VMs stay within the particle budget', FX.stats.dots > 0 && FX.stats.dots <= 1600, FX.stats.dots);
    await sleep(1500);
    check('a stream frame at 1800 VMs costs under 8ms', FX.stats.drawMs < 8, FX.stats.drawMs.toFixed(2) + 'ms');
    for (const [k, v] of Object.entries(H.timings)) check('under 4s: ' + k, v < 4000, v + 'ms');
  },
};

document.addEventListener('DOMContentLoaded', () => {
  setTimeout(async () => {
    try {
      if (!HP.get('bad')) await waitFor(() => S.info && S.view && S.view.loaded, 20000);
      const fn = SCENARIOS[H.scenario];
      if (!fn) throw new Error('no scenario ' + H.scenario);
      await fn();
    } catch (e) {
      H.errors.push('harness: ' + (e && e.stack || e));
    }
    H.done = true;
    flush();
  }, 300);
});
"""


# ------------------------------------------------------------------ plumbing
def install_harness(harness_dir):
    index = web_server._static("index.html").decode("utf-8")
    index = index.replace('<script src="/static/views.js" defer></script>',
                          '<script src="/static/views.js" defer></script>\n'
                          '<script src="/static/h_harness.js" defer></script>')
    (harness_dir / "h_index.html").write_text(index, encoding="utf-8")
    (harness_dir / "h_harness.js").write_text(HARNESS_JS, encoding="utf-8")
    original = web_server._static

    def patched(name):
        if name.startswith("h_"):
            p = harness_dir / name
            return p.read_bytes() if p.is_file() else None
        return original(name)
    web_server._static = patched
    # A test-only clock endpoint (see realNow() in the harness).
    WebApp.h_now = _h_now
    import vcfaimport.web.api as web_api
    web_api.ROUTES.insert(0, ("GET", re.compile(r"^/api/h_now$"), "h_now"))

    # Headless Chrome's virtual clock races ahead while the page is idle; a
    # little real time per poll of a running job keeps it honest.
    original_job = WebApp.job

    def slow_job(self, q, body, job_id):
        out = original_job(self, q, body, job_id)
        if out.get("status") == "running":
            time.sleep(0.25)
        return out
    WebApp.job = slow_job
    original_jobs = WebApp.jobs_list

    def slow_jobs(self, q, body):
        out = original_jobs(self, q, body)
        if out.get("active"):
            time.sleep(0.25)
        return out
    WebApp.jobs_list = slow_jobs


def wait_job(app, job, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline and job.status == "running":
        time.sleep(0.2)
    return job


def build(tmp, vms, seed=True, hostile=True, scenario="import-flaky"):
    import demo
    vc_server, vc_port = fake_vcenter.serve(count=vms)
    env = demo.setup(tmp, vc_port, scenario, batch_size=8 if vms < 1000 else 25)
    os.environ.update(env)
    cfg_path = tmp / "cfg.toml"
    cwd = os.getcwd()
    os.chdir(str(tmp))
    try:
        cfg = Config.load("cfg.toml")
        cfg.workdir = str(tmp / "run")
        cfg.max_parallel_batches = 8 if vms >= 1000 else cfg.max_parallel_batches
        app = WebApp(cfg, config_path=str(cfg_path))
    finally:
        os.chdir(cwd)
    if seed:
        wait_job(app, app._job_discover({}))
        rows = app.store.query_discovered()
        chosen = [r["moref"] for r in rows if vms >= 1000 or
                  (r["folder"] or "").startswith(("Production", "Databases"))]
        app.store.set_selected(chosen, True)
        app.stage_commit({}, {"default_namespace": "prod-web-ns1"} if vms >= 1000 else {})
        if vms < 1000:
            wait_job(app, app._job_execute({"stage": "precheck", "waves": [1], "confirm": True}))
            wait_job(app, app._job_execute({"stage": "import", "waves": [1], "confirm": True}))
        if hostile:
            app.store.upsert_discovered([{"moref": m, "name": n, "folder": "Evil/" + n[:12],
                                          "datacenter": "LabDC", "cluster": "PROD-CL01",
                                          "networks": "VLAN197-Prod", "power_state": "POWERED_ON",
                                          "nics": [{"device_key": 4000, "network_name": "VLAN197-Prod"}]}
                                         for m, n in HOSTILE])
            app.store.set_selected([m for m, _ in HOSTILE], True, namespace="prod-web-ns1", wave=2)
            app.stage_commit({}, {})
            from vcfaimport import state as st
            app.store.set_vm_state(HOSTILE[0][0], st.S_FAILED, stage="import",
                                   message=HOSTILE_MESSAGE)
    return app, vc_server


def _h_now(self, q, body):
    return {"t": time.perf_counter() * 1000.0}


def serve(app):
    srv = web_server.make_server(app, "127.0.0.1", 0, token=TOKEN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:{}".format(srv.server_address[1])


def run_chrome(chrome, url, profile, size, budget):
    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
           "--user-data-dir=" + str(profile), "--window-size={},{}".format(*size),
           "--virtual-time-budget={}".format(budget), "--dump-dom", url]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=900)
    m = re.search(r'<pre id="h-result"[^>]*>(.*?)</pre>', proc.stdout, re.S)
    if not m:
        return {"done": False, "errors": ["no harness result (page did not load?)"], "checks": [],
                "timings": {}, "notes": [proc.stderr[-500:]]}
    return json.loads(html.unescape(m.group(1)))


SCENARIOS = ["pages", "hostile", "select", "stage", "waves", "drawers", "execute",
             "resilience", "token", "theme", "palette", "stream"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vms", type=int, default=200)
    ap.add_argument("--only", action="append", help="scenario name; repeatable")
    ap.add_argument("--scale", action="store_true", help="also run the 1800-VM rendering scenario")
    ap.add_argument("--chrome", help="path to chrome/msedge (default: auto-detect)")
    ap.add_argument("--keep", help="keep the workspace in this directory")
    args = ap.parse_args()

    chrome = args.chrome or find_chrome()
    if not chrome:
        print("no Chrome or Edge found; set CHROME=/path/to/chrome")
        return 2
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("the fake kubectl needs PyYAML: pip install pyyaml")
        return 2

    base_tmp = Path(args.keep).resolve() if args.keep else Path(tempfile.mkdtemp(prefix="vcfa-ui-"))
    base_tmp.mkdir(parents=True, exist_ok=True)
    harness_dir = base_tmp / "harness"
    harness_dir.mkdir(exist_ok=True)
    install_harness(harness_dir)
    saved_env = dict(os.environ)

    plan = []   # (label, workspace kind, scenario, extra query, window size)
    wanted = args.only or SCENARIOS
    for s in wanted:
        if s in SCENARIOS:
            plan.append((s, "main", s, "", (1440, 900)))
    if not args.only or "narrow" in (args.only or []):
        plan.append(("pages @ 390px", "main", "pages", "&narrow=1", (390, 844)))
    if not args.only or "empty" in (args.only or []):
        plan.append(("pages on an empty workspace", "empty", "pages", "", (1440, 900)))
    if not args.only or "light" in (args.only or []):
        plan.append(("pages in light theme", "main", "pages", "&theme=light", (1440, 900)))
    if args.scale or "scale" in (args.only or []):
        plan.append(("scale: 1800 VMs", "scale", "scale", "", (1440, 900)))

    workspaces = {}
    failures = 0
    started = time.time()
    try:
        for label, kind, scenario, extra, size in plan:
            if kind not in workspaces:
                ws = base_tmp / kind
                if ws.exists():
                    shutil.rmtree(ws)
                ws.mkdir()
                print("building the '{}' workspace...".format(kind), flush=True)
                os.environ.clear()
                os.environ.update(saved_env)
                if kind == "empty":
                    app, vc = build(ws, 10, seed=False, hostile=False)
                elif kind == "scale":
                    app, vc = build(ws, 1800, hostile=False, scenario="happy")
                else:
                    app, vc = build(ws, args.vms)
                srv, base = serve(app)
                workspaces[kind] = (app, vc, srv, base)
            app, vc, srv, base = workspaces[kind]
            profile = base_tmp / "profiles" / re.sub(r"\W+", "-", label)
            if profile.exists():
                shutil.rmtree(profile, ignore_errors=True)
            query = "?s={}&t={}{}{}".format(scenario, TOKEN, extra, "&bad=1" if scenario == "token" else "")
            url = base + "/static/h_index.html" + query + "#/overview"
            t0 = time.time()
            res = run_chrome(chrome, url, profile, size, 240000)
            took = time.time() - t0
            bad = [c for c in res["checks"] if not c["ok"]]
            ok = res.get("done") and not bad and not res["errors"] and res["checks"]
            failures += 0 if ok else 1
            print("\n[{}] {}  ({} checks, {:.0f}s)".format("PASS" if ok else "FAIL", label,
                                                          len(res["checks"]), took))
            for c in bad:
                print("    x {}  {}".format(c["name"], c["detail"]))
            for e in res["errors"]:
                print("    ! {}".format(e[:600]))
            if not res.get("done"):
                print("    ! the scenario did not finish in the time budget")
            for n in res.get("notes", [])[:5]:
                if n:
                    print("    . {}".format(n[:200]))
            for k, v in sorted(res.get("timings", {}).items()):
                print("      {:<34} {:>6} ms".format(k, v))
    finally:
        for app, vc, srv, _ in workspaces.values():
            srv.shutdown()
            srv.server_close()
            app.close()
            vc.shutdown()
        os.environ.clear()
        os.environ.update(saved_env)
    print("\n{} scenario(s), {} failed, {:.0f}s".format(len(plan), failures, time.time() - started))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
