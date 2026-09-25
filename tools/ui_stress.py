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
// The first-visit welcome would cover every page; only the help scenario wants it.
store.set('vcfa-welcome', 'done');
// Headless Chrome runs without a GPU, so Auto rendering resolves to Lite; &quality=full forces the glass.
if (HP.get('quality') && window.FX) FX.prefs.quality = HP.get('quality');
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
// An element's own text, without the "?" help marks inside it.
const ownText = (el) => [...el.childNodes].filter((n) => !(n.classList && n.classList.contains('tipq'))).map((n) => n.textContent).join('').trim();
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
    for (const id of ['overview', 'discover', 'select', 'stage', 'waves', 'execute', 'queue', 'batches', 'triage', 'activity', 'schedule', 'settings', 'help']) {
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
    await open('queue');
    const first = document.querySelector('#view').firstChild;
    S.view.render();
    renderNav();
    check('re-rendering unchanged data leaves the page alone (no repaint)', document.querySelector('#view').firstChild === first);
    for (const q of ['doc=lab', 'doc=readme', 'tab=glossary']) {
      await open('help', q);
      check('help ' + q + ' renders', !brokeView() && noInjection());
      if (HP.get('narrow')) check('no page-level horizontal scroll on help ' + q, document.documentElement.scrollWidth <= innerWidth + 2, document.documentElement.scrollWidth + ' > ' + innerWidth + ': ' + overflowers());
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

    // namespaces from the Supervisor, suggested while mapping
    const chip = await waitFor(() => document.querySelector('.nsbar .nschip'), 10000);
    check('the Supervisor\'s namespaces are listed while mapping', chip && /prod-web-ns1/.test(document.querySelector('.nsbar').textContent),
      document.querySelector('.nsbar') && document.querySelector('.nsbar').textContent.slice(0, 160));
    check('system namespaces are left out', !/kube-system|vmware-system|svc-/.test(document.querySelector('.nsbar').textContent));
    const nsOpts = [...document.querySelectorAll('#ns-options option, #ns-list option, #pg-options option, #subnet-options option')];
    check('every namespace field suggests them', nsOpts.some((o) => o.value === 'prod-db-ns2'));
    // Some browsers show an option's label in place of its value: the name itself must be what shows.
    check('the suggestions show the namespace names themselves', nsOpts.length && nsOpts.every((o) => !o.hasAttribute('label') && (!o.textContent || o.textContent === o.value)),
      nsOpts.slice(0, 3).map((o) => o.value + '/' + o.label).join(' '));
    click('[data-act="addRow"][data-m="f"]:not([data-key])');
    const k = await waitFor(() => { const i = S.view.fr.length - 1; return document.getElementById('f-' + i + '-namespace') ? i + 1 : 0; }) - 1;
    document.getElementById('f-' + k + '-namespace').focus();
    const pick = [...document.querySelectorAll('.nsbar .nschip')].find((x) => x.dataset.ns === 'dmz-ns3');
    pick.click();
    check('clicking a namespace fills the field you were in', await waitFor(() => S.view.fr[k].namespace === 'dmz-ns3', 3000), S.view.fr[k].namespace);
    type('#f-' + k + '-namespace', 'prod-web-nsl');
    check('a namespace not on the Supervisor is flagged as you type', document.getElementById('f-' + k + '-namespace').classList.contains('unknown'));
    check('with a reason', /Supervisor|kubeconfig/.test(document.getElementById('f-' + k + '-namespace').title));
    check('and listed above the maps', await waitFor(() => { S.view.render(); return /prod-web-nsl/.test((document.querySelector('.nsbar .note.warn') || {}).textContent || ''); }, 4000));
    click('[data-act="delRow"][data-m="f"][data-i="' + k + '"]');
    await waitFor(() => S.view.fr.length === k, 3000);

    // portgroups from the last discovery, subnets from the Supervisor
    const pgChip = await waitFor(() => [...document.querySelectorAll('.nsbar .pgchip')].find((x) => x.dataset.name === 'VLAN300-Spare'), 10000);
    check('every vCenter portgroup is listed, even one no VM uses', pgChip);
    const sbChip = await waitFor(() => [...document.querySelectorAll('.nsbar .sbchip')].find((x) => x.dataset.name === 'subnet-vlan197'), 10000);
    check('the Supervisor\'s subnets are listed with their namespace', sbChip && /^(prod-web-ns1|prod-db-ns2|dmz-ns3)$/.test((sbChip.querySelector('small') || {}).textContent || ''), sbChip && sbChip.textContent);
    check('portgroup and subnet fields suggest them', document.querySelector('#pg-options option[value="VLAN300-Spare"]') && document.querySelector('#subnet-options option[value="subnet-vlan200"]'));
    check('the portgroup and subnet columns are wired to them', document.getElementById('n-0-portgroup').getAttribute('list') === 'pg-options' && document.getElementById('n-0-subnet').getAttribute('list') === 'subnet-options');
    document.getElementById('def-wave').focus();          // no portgroup field in focus: the click adds a row
    const nBefore = S.view.nr.length;
    pgChip.click();
    check('clicking an unmapped portgroup adds it to the portgroup map', await waitFor(() => S.view.nr.length === nBefore + 1 && S.view.nr[nBefore].portgroup === 'VLAN300-Spare', 3000));
    check('and puts you in its subnet field', await waitFor(() => document.activeElement && document.activeElement.id === 'n-' + nBefore + '-subnet', 3000), document.activeElement && document.activeElement.id);
    [...document.querySelectorAll('.nsbar .sbchip')].find((x) => x.dataset.name === 'subnet-dmz').click();
    check('clicking a subnet fills the subnet field you are in', await waitFor(() => S.view.nr[nBefore].subnet === 'subnet-dmz', 3000), S.view.nr[nBefore].subnet);
    type('#n-' + nBefore + '-subnet', 'subnet-dmx');
    check('a subnet not on the Supervisor is flagged', document.getElementById('n-' + nBefore + '-subnet').classList.contains('unknown'));
    type('#n-' + nBefore + '-portgroup', 'VLAN9*');
    check('a portgroup pattern that matches nothing is flagged', document.getElementById('n-' + nBefore + '-portgroup').classList.contains('unknown'));
    type('#n-' + nBefore + '-portgroup', 'vlan3*');
    check('a pattern matches portgroups case-insensitively, as staging does', !document.getElementById('n-' + nBefore + '-portgroup').classList.contains('unknown'));
    document.getElementById('def-wave').focus();
    [...document.querySelectorAll('.nsbar .sbchip')][0].click();
    check('a subnet click with no subnet field in focus explains what to do', await waitFor(() => [...document.querySelectorAll('.toast')].some((x) => /Subnet field first/.test(x.textContent)), 3000));
    click('[data-act="delRow"][data-m="n"][data-i="' + nBefore + '"]');
    await waitFor(() => S.view.nr.length === nBefore, 3000);

    // two maps disagree about a VM's namespace
    const ni = S.view.nr.findIndex((r) => r.portgroup === 'DMZ-Uplink');   // the Databases VMs' network in the fake vCenter
    if (ni >= 0) {
      type('#n-' + ni + '-namespace', 'prod-web-ns1');
      const conflict = await waitFor(() => [...document.querySelectorAll('#stage-preview details.note.warn')].find((d) => /conflicting namespace/.test(d.textContent)), 8000);
      check('a portgroup entry that disagrees with the folder map is reported', conflict, S.view.preview.ns_conflicts && S.view.preview.ns_conflicts.length);
      check('the folder map still wins', S.view.preview.records.filter((r) => r.folder === 'Databases').every((r) => r.namespace === 'prod-db-ns2'));
      check('each conflict names both sources', conflict && /folder map/.test(conflict.textContent) && /portgroup map \(DMZ-Uplink\)/.test(conflict.textContent));
      type('#n-' + ni + '-namespace', '');
      check('clearing it clears the warning', await waitFor(() => !(S.view.preview.ns_conflicts || []).length, 8000));
    }
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
    const aurora = document.getElementById('fx-aurora');
    const cardBlur = () => getComputedStyle(document.querySelector('#view .card, #view .kpi')).backdropFilter;
    check('the aurora carries no full-screen CSS blur', getComputedStyle(aurora).filter === 'none', getComputedStyle(aurora).filter);
    check('without GPU acceleration, Auto picks Lite rendering', FX.rendering().soft && document.body.dataset.fx === 'lite', JSON.stringify(FX.rendering()));
    check('Lite drops the glass and the aurora', cardBlur() === 'none' && getComputedStyle(aurora).display === 'none', cardBlur());
    check('the Studio says why Auto chose Lite', /Lite/.test(document.querySelector('#fx-studio').textContent));
    click('#fx-studio [data-fx="quality"][data-k="full"]');
    check('Full brings the glass back', document.body.dataset.fx === 'full' && cardBlur() !== 'none' && getComputedStyle(aurora).display !== 'none', cardBlur());
    click('#fx-studio [data-fx="quality"][data-k="auto"]');
    check('and Auto goes back to Lite here', document.body.dataset.fx === 'lite');
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

  async govern() {
    const setting = async (k) => (await GET('/api/settings')).settings.find((x) => x.key === k).value;
    const toastWith = (cls, re) => waitFor(() => [...document.querySelectorAll('.toast' + cls)].find((x) => re.test(x.textContent)), 30000);

    // ---- settings: guardrails, a save bar, refusals explained
    await open('settings');
    const groups = [...document.querySelectorAll('#view .card-h h3')].map(ownText);
    check('settings show every group', ['Pacing', 'Safety', 'Governance', 'Applications', 'Verification'].every((g) => groups.includes(g)), groups.join(','));
    click('[data-act="toggle"][data-k="app_together"]');
    check('a change raises the save bar', await waitFor(() => document.querySelector('.stickybar [data-act="save"]')));
    type('#set-app_category', 'Application');
    check('typing keeps focus through the re-render', document.activeElement && document.activeElement.id === 'set-app_category');
    document.querySelector('input[data-k="require_approval"][data-stage="import"]').click();
    await sleep(100);
    click('.stickybar [data-act="save"]');
    check('saved settings take effect', await waitFor(async () => (await setting('app_together')) === true && (await setting('app_category')) === 'Application' && (await setting('require_approval')).includes('import'), 10000));
    check('the file value stays visible, the override is marked', await waitFor(() => document.querySelector('.setting.over [data-act="reset"][data-k="app_category"]')));
    type('#set-batch_size', '0');
    await sleep(50);
    click('.stickybar [data-act="save"]');
    check('an out-of-range value is refused with the reason', await toastWith('.bad', /between/));
    click('.stickybar [data-act="discard"]');
    check('discard drops unsaved edits', await waitFor(() => !document.querySelector('.stickybar')));

    // ---- notifications: an unreachable channel reports the failure
    const typeSel = document.querySelector('select[data-k="type"][data-change="chField"]');
    typeSel.value = 'webhook'; typeSel.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(100);
    type('#ch-name', 'dead-end');
    type('#ch-url', 'http://127.0.0.1:9/hook');
    await sleep(100);
    click('[data-act="chEvent"][data-ev="job_failed"]');
    click('[data-act="chAdd"]');
    check('the new channel is listed', await waitFor(() => /dead-end/.test(document.querySelector('#view').textContent)));
    click('.stickybar [data-act="save"]');
    check('channels are saved', await waitFor(async () => (await GET('/api/settings')).notify.length === 1, 8000));
    click('[data-act="chTest"][data-i="0"]');
    check('a failed test says which channel failed', await toastWith('.bad', /dead-end/));

    // ---- people: a personal link, shown once
    type('#u-name', 'vic');
    document.querySelector('#u-role').value = 'viewer';
    click('[data-act="userAdd"]');
    const link = await waitFor(() => document.querySelector('#u-link'));
    check('creating a user shows their personal link', link && /#t=/.test(link.value), link && link.value);
    click('.modal [data-m="no"]');
    check('the user is listed', await waitFor(() => [...document.querySelectorAll('#view td b')].some((b) => b.textContent === 'vic')));
    const vicToken = link ? decodeURIComponent(link.value.split('#t=')[1]) : '';
    const asVic = await fetch('/api/select', { method: 'POST', headers: { 'X-VCFA-Token': vicToken, 'Content-Type': 'application/json' }, body: '{"morefs":[],"selected":true}' });
    check('the viewer link cannot change anything', asVic.status === 403, asVic.status);

    // ---- apps come from tags once staged; the stream regroups
    await POST('/api/stage', {});
    await open('overview');
    click('.stream-by [data-k="wave"]');
    check('the stream regroups by wave', await waitFor(() => [...document.querySelectorAll('.lane-h .ln-l')].every((x) => /^Wave /.test(x.textContent))));
    check('a grouped lane shows its progress', document.querySelector('.lane-h .ln-d'));
    const appBtn = await waitFor(() => document.querySelector('.stream-by [data-k="app"]'));
    check('an App grouping is offered once VMs have apps', appBtn);
    if (appBtn) {
      appBtn.click();
      check('lanes by application', await waitFor(() => [...document.querySelectorAll('.lane-h .ln-l')].some((x) => /Payroll|CRM|Portal|Billing/.test(x.textContent))));
      check('particles survive a regroup', FX.stats.dots > 0, FX.stats.dots);
    }
    check('the Applications card lists apps', /Applications/.test(document.querySelector('#view').textContent));
    click('.stream-by [data-k="stage"]');
    check('and back to the flow by stage', await waitFor(() => document.querySelectorAll('.lane-h').length === 5));

    // ---- density
    FX.set({ density: 'compact' });
    await open('queue');
    const td = document.querySelector('#view table.t td');
    check('compact density tightens tables', document.body.dataset.density === 'compact' && td && getComputedStyle(td).paddingTop === '5px', td && getComputedStyle(td).paddingTop);
    FX.set({ density: 'comfortable' });

    // ---- select: readiness filter, saved views
    await open('select');
    const ready = document.querySelector('select[data-k="ready"]');
    ready.value = 'block'; ready.dispatchEvent(new Event('change', { bubbles: true }));
    check('the readiness filter narrows to likely failures', await waitFor(() => S.view.rows.length > 0 && S.view.rows.every((x) => x.readiness === 'block')), S.view.rows.length);
    click('[data-act="svSave"]');
    const nm = await waitFor(() => document.querySelector('.modal input[data-field="name"]'));
    type(nm, 'likely failures');
    click('.modal [data-m="yes"]');
    check('a saved view appears', await waitFor(() => document.querySelector('.chip.sv[data-name="likely failures"]')));
    const r2 = document.querySelector('select[data-k="ready"]');
    r2.value = ''; r2.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(100);
    click('.chip.sv[data-name="likely failures"]');
    check('applying it restores the filters', await waitFor(() => S.view.f.ready === 'block' && S.view.rows.every((x) => x.readiness === 'block')));
    click('.chip.sv[data-name="likely failures"] .x');
    check('and it can be deleted', await waitFor(() => !document.querySelector('.chip.sv')));
    check('tags are shown per VM', document.querySelector('#view .tag.vtag, #view .tag.app'));

    // ---- stage: the tag map
    await open('stage');
    check('tags of the selection are listed', await waitFor(() => document.querySelectorAll('#cov-tags .covrow').length > 0));
    const mapBtn = document.querySelector('#cov-tags [data-act="addRow"]');
    if (mapBtn) {
      const tag = mapBtn.dataset.key;
      mapBtn.click();
      const nsIn = await waitFor(() => document.activeElement && /^t-\d+-namespace$/.test(document.activeElement.id) && document.activeElement);
      check('mapping a tag focuses its namespace', nsIn);
      if (nsIn) type(nsIn, 'zz-tag-ns');
      check('the tag map re-previews live', await waitFor(() => S.view.preview.records.some((r) => r.namespace === 'zz-tag-ns'), 8000), tag);
      click('[data-act="delRow"][data-m="t"]');
      check('removing it restores the preview', await waitFor(() => !S.view.preview.records.some((r) => r.namespace === 'zz-tag-ns'), 8000));
    }

    // ---- the two-person rule: request, cannot self-approve, a colleague approves
    let since = await jobIds();
    const pre = await POST('/api/run/execute', { stage: 'precheck', waves: [2], confirm: true });
    await waitJob('execute', since);
    await open('execute', 'stage=import&waves=2');
    const go = await waitFor(() => { const b = document.querySelector('[data-act="start"]:not([disabled])'); return b && /Request approval/.test(b.textContent) && b; }, 10000);
    check('Execute offers "Request approval" when the rule is on', go);
    check('the plan shows an ETA', /≈/.test(document.querySelector('#ex-plan').textContent));
    if (go) {
      go.click();
      const yes = await waitFor(() => document.querySelector('.modal [data-m="yes"]'));
      check('no typed word is needed to ask', yes && !yes.disabled && !document.querySelector('#modal-type'));
      yes.click();
      check('the request is acknowledged', await toastWith('.warn', /Approval #\d+ requested/));
    }
    const pending = (await GET('/api/approvals')).approvals.filter((a) => a.state === 'pending');
    check('one request is pending', pending.length === 1, pending.length);
    await open('schedule');
    check('the requester sees it without an Approve button', await waitFor(() => document.querySelector('.approval')) && !document.querySelector('.approval [data-act="approve"]'));
    check('the nav badges the waiting request', await waitFor(() => document.querySelector('.nav-item[href="#/schedule"] .nav-badge')));
    const otto = (await POST('/api/users', { name: 'otto', role: 'operator' })).token;
    since = await jobIds();
    const dec = await fetch('/api/approvals/' + pending[0].id + '/decide', { method: 'POST', headers: { 'X-VCFA-Token': otto, 'Content-Type': 'application/json' }, body: '{"approve":true,"note":"CAB ok"}' });
    check('a colleague approves it', dec.status === 200, dec.status);
    const imp = await waitJob('execute', since);
    check('the approved import runs on behalf of both', imp && /otto/.test(imp.user || ''), imp && imp.user);
    check('the approval is used up', (await GET('/api/approvals')).approvals[0].state === 'executed');

    // ---- change windows
    await open('schedule');
    click('[data-act="fStage"][data-k="precheck"]');
    await sleep(200);
    const t0 = Date.now() + 2 * 3600e3;
    const loc = (ms) => { const d = new Date(ms); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };
    type('#w-start', loc(t0));
    type('#w-end', loc(t0 + 2 * 3600e3));
    check('the window shows whether the work fits', await waitFor(() => document.querySelector('#w-fit .fit, #w-fit .note'), 8000));
    click('[data-act="createWindow"]');
    check('the window is planned', await waitFor(async () => (await GET('/api/schedules')).schedules.some((x) => x.state === 'scheduled'), 8000));
    const planned = (await GET('/api/schedules')).schedules[0];
    check('it opens and closes when typed (no field lost)', Math.abs(parseTs(planned.start_at) - t0) < 90e3 && Math.abs(parseTs(planned.end_at) - t0 - 2 * 3600e3) < 90e3, planned.start_at + ' -> ' + planned.end_at);
    check('it appears on the timeline', await waitFor(() => document.querySelector('.tline .win')));
    click('[data-act="cancelWindow"]');
    const c1 = await waitFor(() => document.querySelector('.modal [data-m="yes"]'));
    if (c1) c1.click();
    check('and can be cancelled', await waitFor(async () => (await GET('/api/schedules')).schedules.every((x) => x.state === 'cancelled'), 8000));

    // ---- queue: verification
    await open('queue', 'state=committed');
    check('the queue has a Verified column', [...document.querySelectorAll('#view th')].some((x) => ownText(x) === 'Verified'));
    click('#view tbody input[data-act="chk"]');
    await sleep(100);
    since = await jobIds();
    click('[data-act="bVerify"]');
    const vj = await waitJob('verify', since);
    check('verification runs from the queue', vj && ['succeeded', 'warning'].includes(vj.status), vj && vj.status);
    check('its verdict shows in the queue', await waitFor(async () => { await S.view.refresh(); return document.querySelector('#view td .tag.ok, #view td .tag.bad'); }, 15000));
    click('#view tbody tr[data-act="vm"]');
    check('the drawer lists the checks', await waitFor(() => document.querySelector('.drawer .checks .checkrow')));
    check('governance pages never broke', !brokeView() && noInjection());
  },

  async help() {
    const count = () => { const c = document.querySelector('.tour-card .tour-count'); return c ? parseInt(c.textContent, 10) : 0; };
    const key = (k) => document.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));

    // ---- first visit: the welcome, then the whole tour
    store.set('vcfa-welcome', null);
    HELP.maybeWelcome();
    const welcome = await waitFor(() => document.querySelector('.tour-card.center'), 5000);
    check('a first visit opens the welcome', welcome && /Welcome/.test(welcome.textContent));
    check('the welcome says the tour changes nothing', welcome && /changes nothing/.test(welcome.textContent));
    check('it offers the lab guide straight away', document.querySelector('.tour-card [data-tour="lab"]'));
    click('[data-tour="next"]');
    const n = HELP.TOUR_STEPS.length;
    let seen = 0, spotted = 0;
    const off = [], wrong = [];
    for (let k = 1; k < n; k++) {
      if (!(await waitFor(() => count() === k, 8000))) { H.notes.push('tour stuck before step ' + k); break; }
      seen++;
      if (document.querySelector('.tour-spot')) spotted++;
      const want = HELP.TOUR_STEPS[k].route;
      if (want && S.route !== want) wrong.push(k + ':' + S.route);
      const r = document.querySelector('.tour-card').getBoundingClientRect();
      if (r.left < 0 || r.top < 0 || r.right > innerWidth + 1 || r.bottom > innerHeight + 1) off.push(k);
      if (k === 2) {
        key('ArrowLeft');
        check('the Left arrow goes back a step', await waitFor(() => count() === 1, 5000));
        key('ArrowRight');
        check('and Right goes forward', await waitFor(() => count() === 2, 5000));
      }
      if (k < n - 1) click('[data-tour="next"]');
    }
    check('every tour step renders', seen === n - 1, seen + ' of ' + (n - 1));
    check('the steps spotlight what they talk about', spotted >= seen - 1, spotted + ' of ' + seen);
    check('every tour card stays on screen', !off.length, off.join(','));
    check('the tour opens each page it talks about', !wrong.length, wrong.join(','));
    click('[data-tour="next"]');
    check('Finish closes the tour', await waitFor(() => !document.querySelector('#tour-root')));
    check('and the welcome does not come back', store.get('vcfa-welcome') === 'done');
    HELP.startTour(3);
    await waitFor(() => document.querySelector('.tour-card'));
    key('Escape');
    check('Escape ends the tour', await waitFor(() => !document.querySelector('#tour-root')));

    // ---- tooltips
    await open('execute');
    const tipBox = () => { const t = document.getElementById('tip'); return t && !t.hidden ? t : null; };
    const pf = document.querySelector('.steps .tipq[data-tip="preflight"]');
    pf.dispatchEvent(new PointerEvent('pointerover', { bubbles: true }));
    const t1 = await waitFor(tipBox, 3000);
    check('hovering a ? explains it', t1 && /Preflight/.test(t1.textContent) && /read-only/.test(t1.textContent), t1 && t1.textContent);
    const tr = t1 && t1.getBoundingClientRect();
    check('the tooltip stays on screen', tr && tr.left >= 0 && tr.right <= innerWidth && tr.top >= 0 && tr.bottom <= innerHeight);
    pf.dispatchEvent(new PointerEvent('pointerout', { bubbles: true, relatedTarget: document.body }));
    check('and leaving hides it', await waitFor(() => !tipBox(), 3000));
    const chip = document.querySelector('.ctxchip[data-tip="commit_action"]');
    chip.dispatchEvent(new PointerEvent('pointerover', { bubbles: true }));
    const t2 = await waitFor(tipBox, 3000);
    check('the commit chip explains commitAction for this workspace', t2 && /commitAction: (Auto|Wait)/.test(t2.textContent));
    check('Auto is flagged as irreversible', !/Auto/.test(t2.textContent) || (t2.classList.contains('bad') && /never be handed back/.test(t2.textContent)));
    key('Escape');
    await waitFor(() => !tipBox(), 2000);
    click(pf);
    check('clicking a ? pins it', await waitFor(() => tipBox() && tipBox().classList.contains('pinned'), 3000));
    pf.dispatchEvent(new PointerEvent('pointerout', { bubbles: true, relatedTarget: document.body }));
    await sleep(400);
    check('a pinned tooltip stays when the pointer leaves', tipBox());
    const link = tipBox() && tipBox().querySelector('a.tip-l');
    check('it links into the lab guide', link && /doc=lab/.test(link.getAttribute('href')));
    if (link) {
      link.click();
      const hd = await waitFor(() => S.route === 'help' && S.view.loaded && document.getElementById('h-5-preflight-and-validate--nothing-applied-yet'), 8000);
      check('the link lands on the right section of the guide', hd && hd.getBoundingClientRect().top < innerHeight / 2 && hd.getBoundingClientRect().top > -5, hd && hd.getBoundingClientRect().top);
      check('and the tooltip closes', !tipBox());
    }
    await open('discover');
    const tls = document.querySelector('input[data-k="insecure"]');
    const before = tls.checked;
    click('.tipq[data-tip="insecure"]');
    await sleep(150);
    check('a ? inside a checkbox label does not tick the box', tls.checked === before && tipBox() && /TLS/.test(tipBox().textContent));
    key('Escape');
    await open('select');
    const sortKey = S.view.sort.key;
    const thq = document.querySelector('th .tipq[data-tip="readiness"]');
    if (thq) {
      click(thq);
      await sleep(150);
      check('a ? in a column header does not sort the table', S.view.sort.key === sortKey && tipBox());
      key('Escape');
    }
    await open('queue');
    const pillEl = document.querySelector('#view .pill[data-tip]');
    pillEl.dispatchEvent(new PointerEvent('pointerover', { bubbles: true }));
    check('status pills explain themselves', await waitFor(() => tipBox() && tipBox().textContent.length > 20, 3000), pillEl.textContent);
    pillEl.dispatchEvent(new PointerEvent('pointerout', { bubbles: true, relatedTarget: document.body }));
    await waitFor(() => !tipBox(), 2000);

    // ---- every ? on every page has an explanation
    const missing = new Set();
    let marks = 0;
    for (const id of ['overview', 'discover', 'select', 'stage', 'waves', 'execute', 'queue', 'triage', 'schedule', 'settings', 'help']) {
      await open(id);
      document.querySelectorAll('[data-tip]').forEach((el) => { marks++; if (!HELP.glossary(el.dataset.tip)) missing.add(id + ':' + el.dataset.tip); });
    }
    check('every ? has an explanation', !missing.size && marks > 30, [...missing].join(', ') + ' / ' + marks + ' marks');

    // ---- every link into the guides lands on a heading
    const ids = {}, htmls = {};
    for (const d of ['lab', 'readme']) {
      htmls[d] = HELP.mdBlocks((await GET('/api/docs/' + d)).markdown, d).html;
      ids[d] = new Set([...htmls[d].matchAll(/ id="h-([^"]+)"/g)].map((m) => m[1]));
    }
    const refs = Object.values(HELP.GLOSSARY).map((g) => g.doc).concat(HELP.TOUR_STEPS.map((x) => x.doc)).filter(Boolean);
    const broken = refs.filter(([d, h]) => !ids[d].has(h)).map((r) => r.join('#'));
    check('every tooltip and tour link lands on a heading', !broken.length && refs.length > 20, broken.join(', '));
    const inDoc = [];
    for (const d of ['lab', 'readme']) {
      for (const m of htmls[d].matchAll(/href="#\/help\?doc=(\w+)&amp;h=([^"]+)"/g)) if (!ids[m[1]].has(decodeURIComponent(m[2]))) inDoc.push(d + '->' + m[1] + '#' + m[2]);
    }
    check('links inside the guides resolve', !inDoc.length, inDoc.join(', '));

    // ---- the guides render, safely
    await open('help', 'doc=lab');
    check('the lab guide renders with a table of contents', document.querySelectorAll('.toc a').length > 10, document.querySelectorAll('.toc a').length);
    check('angle brackets in the guide are shown as text', document.querySelector('.doc').textContent.includes('<target-namespace>') && noInjection());
    check('code blocks render', document.querySelectorAll('.doc pre.code').length > 5);
    const ext = document.querySelector('.doc a[target="_blank"]');
    check('outside links open in a new tab without opener', ext && /noopener/.test(ext.rel));
    await open('help', 'doc=readme&h=the-two-person-rule');
    const h2 = await waitFor(() => document.getElementById('h-the-two-person-rule'));
    check('a deep link scrolls to its heading', h2 && h2.getBoundingClientRect().top > -5 && h2.getBoundingClientRect().top < innerHeight / 2, h2 && h2.getBoundingClientRect().top);
    check('README tables render', document.querySelectorAll('.doc table').length > 3);
    await open('help', 'tab=glossary');
    const all = document.querySelectorAll('.gloss').length;
    type('#gloss-q', 'irreversible');
    check('the glossary searches', await waitFor(() => { const k = document.querySelectorAll('.gloss').length; return k > 0 && k < all; }, 3000), all);

    // ---- hints can be hidden
    HELP.setHints(false);
    await open('execute');
    check('hiding hints hides every ?', [...document.querySelectorAll('.tipq:not(.static)')].every((x) => getComputedStyle(x).display === 'none'));
    HELP.setHints(true);
    await open('execute');
    check('and showing brings them back', [...document.querySelectorAll('.tipq')].some((x) => getComputedStyle(x).display !== 'none'));

    // ---- the palette and the topbar lead here too
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }));
    const pq = await waitFor(() => document.getElementById('cmdk-q'));
    type(pq, 'tour');
    check('the palette offers the tour', await waitFor(() => [...document.querySelectorAll('.cmdk-item')].some((i) => /Take the tour/.test(i.textContent))));
    document.getElementById('cmdk-q').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    click('[data-act="help"]');
    check('the ? button in the topbar opens Help', await waitFor(() => S.route === 'help'));
    check('help never broke', !brokeView() && noInjection());
  },

  async shot() {   // tools only: open one page and let it settle, for a screenshot
    if (HP.get('by')) FX.set({ streamBy: HP.get('by') });
    await open(HP.get('page') || 'overview', HP.get('q') || '');
    if (S.view) S.view.render();
    await sleep(800);
    if (window.FX) FX.drawNow();
    if (HP.get('tour')) { HELP.startTour(+HP.get('tour')); await waitFor(() => document.querySelector('.tour-card'), 8000); await sleep(700); }
    if (HP.get('tip')) { const q = document.querySelector('.tipq[data-tip="' + HP.get('tip') + '"]'); if (q) q.click(); await sleep(300); }
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
    # Last in <head>: the harness runs after every console script.
    index = index.replace('</head>', '<script src="/static/h_harness.js" defer></script>\n</head>', 1)
    assert "h_harness.js" in index

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
             "resilience", "token", "theme", "palette", "stream", "govern", "help"]


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
            # govern turns on the two-person rule: its own workspace, so no other scenario sees it
            plan.append((s, "govern" if s == "govern" else "main", s, "", (1440, 900)))
    if not args.only or "narrow" in (args.only or []):
        plan.append(("pages @ 390px", "main", "pages", "&narrow=1", (390, 844)))
    if not args.only or "empty" in (args.only or []):
        plan.append(("pages on an empty workspace", "empty", "pages", "", (1440, 900)))
    if not args.only or "full" in (args.only or []):
        plan.append(("pages with Full rendering (glass)", "main", "pages", "&quality=full", (1440, 900)))
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
