'use strict';
/* fx.js — the look and feel.
 *
 *   Theme engine   palettes generated live in OKLCH from a few hues, so any
 *                  preset or hue shift stays legible; status colours keep
 *                  their meaning in every theme.
 *   Ambience       a slow aurora behind the glass that reacts to the
 *                  campaign: failures bleed in a hazard tint, a running job
 *                  quickens it, a finished campaign blooms.
 *   Migration      every VM is a particle flowing vCenter -> VCF Automation.
 *   Stream
 *   Motion         page entrances, count-ups, a sliding nav, spotlight/tilt.
 *   Palette        Ctrl+K: pages, actions, VMs, batches, themes.
 *   Studio         pick a theme, shift its hue, set glow and motion.
 *
 * Loaded after core.js, before views.js. core.js calls the FX hooks; every
 * one of them fails soft — the console works without any of this. */

const FX = (() => {
  // ------------------------------------------------------------- themes
  const THEMES = [
    { id: 'aurora', label: 'Aurora', tag: 'teal and violet on deep ink', mode: 'dark', h1: 190, h2: 285, h3: 155, hb: 255, c: 1 },
    { id: 'nebula', label: 'Nebula', tag: 'magenta and indigo', mode: 'dark', h1: 335, h2: 280, h3: 215, hb: 292, c: 1.05 },
    { id: 'solar', label: 'Solar Flare', tag: 'amber and coral', mode: 'dark', h1: 68, h2: 28, h3: 345, hb: 38, c: 1 },
    { id: 'phosphor', label: 'Phosphor', tag: 'terminal green', mode: 'dark', h1: 148, h2: 170, h3: 125, hb: 165, c: 0.95 },
    { id: 'graphite', label: 'Graphite', tag: 'quiet monochrome', mode: 'dark', h1: 250, h2: 250, h3: 250, hb: 250, c: 0.16 },
    { id: 'glacier', label: 'Glacier', tag: 'ice blue, light', mode: 'light', h1: 238, h2: 200, h3: 285, hb: 235, c: 1 },
    { id: 'daylight', label: 'Daylight', tag: 'warm paper, light', mode: 'light', h1: 268, h2: 22, h3: 172, hb: 75, c: 0.9 },
  ];
  const DEFAULTS = { theme: 'aurora', shift: 0, glow: 100, motion: null, reactive: true, density: 'comfortable', streamBy: 'stage' };
  const P = Object.assign({}, DEFAULTS, readPrefs());
  if (!P.motion) P.motion = matchMedia('(prefers-reduced-motion: reduce)').matches ? 'off' : 'full';

  function readPrefs() {
    try { return JSON.parse(store.get('vcfa-fx') || '{}') || {}; } catch (e) { return {}; }
  }
  function savePrefs() { store.set('vcfa-fx', JSON.stringify(P)); }
  const theme = () => THEMES.find((t) => t.id === P.theme) || THEMES[0];
  const wrap = (h) => ((h % 360) + 360) % 360;
  const ok = (l, c, h, a) => 'oklch(' + l + ' ' + (+c).toFixed(3) + ' ' + wrap(h).toFixed(1) + (a === undefined ? '' : ' / ' + a) + ')';
  const H = { h1: 190, h2: 285, h3: 155, hb: 255, mode: 'dark' };   // resolved hues of the live theme

  function palette(t) {
    const s = +P.shift || 0, c = t.c;
    const h1 = t.h1 + s, h2 = t.h2 + s, h3 = t.h3 + s, hb = t.hb + s * 0.6;
    Object.assign(H, { h1, h2, h3, hb, mode: t.mode });
    const v = { '--h1': wrap(h1), '--h2': wrap(h2), '--h3': wrap(h3), '--hb': wrap(hb), '--c': c, '--glow': (P.glow / 100).toFixed(2) };
    if (t.mode === 'dark') {
      const ac = Math.max(0.03, 0.15 * c);
      Object.assign(v, {
        '--bg0': ok(0.14, 0.025 * c, hb), '--bg1': ok(0.175, 0.03 * c, hb),
        '--glass': ok(0.21, 0.03 * c, hb, 0.62), '--glass-2': ok(0.25, 0.035 * c, hb, 0.66), '--glass-strong': ok(0.19, 0.03 * c, hb, 0.92),
        '--panel': ok(0.21, 0.03 * c, hb, 0.7), '--panel-2': ok(0.26, 0.03 * c, hb, 0.55), '--panel-3': ok(0.31, 0.03 * c, hb, 0.6),
        '--line': ok(0.9, 0.04 * c, hb, 0.09), '--line-2': ok(0.9, 0.04 * c, hb, 0.17), '--hi': 'oklch(1 0 0 / 0.07)',
        '--fg': ok(0.97, 0.01, hb), '--muted': ok(0.77, 0.025 * c, hb), '--faint': ok(0.6, 0.025 * c, hb),
        '--accent': ok(t.id === 'graphite' ? 0.88 : 0.78, ac, h1), '--accent-2': ok(0.72, Math.max(0.03, 0.19 * c), h2),
        '--accent-3': ok(0.8, Math.max(0.03, 0.16 * c), h3), '--accent-soft': ok(0.78, ac, h1, 0.14),
        '--accent-fg': ok(0.17, 0.04 * c, h1), '--info-soft': ok(0.78, ac, h1, 0.12),
        '--glow-1': ok(0.7, 0.2 * c, h1), '--glow-2': ok(0.62, 0.22 * c, h2), '--glow-3': ok(0.7, 0.19 * c, h3),
        '--grid-dot': ok(0.9, 0.05 * c, hb, 0.07),
        '--ok': ok(0.8, 0.17, 152), '--ok-soft': ok(0.8, 0.17, 152, 0.14), '--bad': ok(0.7, 0.2, 25), '--bad-soft': ok(0.7, 0.2, 25, 0.15),
        '--warn': ok(0.84, 0.15, 80), '--warn-soft': ok(0.84, 0.15, 80, 0.14),
        '--shadow': '0 1px 0 var(--hi) inset, 0 18px 40px -22px oklch(0 0 0 / 0.75)',
        '--shadow-lg': '0 1px 0 var(--hi) inset, 0 30px 80px -20px oklch(0 0 0 / 0.8)',
        '--s-pending': ok(0.7, 0.03, hb), '--s-precheck_running': ok(0.78, 0.13, 225), '--s-precheck_passed': ok(0.8, 0.13, 185),
        '--s-precheck_failed': ok(0.76, 0.16, 55), '--s-importing': ok(0.72, 0.17, 265), '--s-awaiting_commit': ok(0.84, 0.15, 85),
        '--s-committed': ok(0.8, 0.18, 152), '--s-failed': ok(0.69, 0.21, 25), '--s-rolling_back': ok(0.75, 0.15, 305),
        '--s-rolled_back': ok(0.67, 0.16, 295), '--s-skipped': ok(0.52, 0.02, hb),
      });
    } else {
      const ac = Math.max(0.04, 0.19 * c);
      Object.assign(v, {
        '--bg0': ok(0.972, 0.012 * c, hb), '--bg1': ok(0.95, 0.016 * c, hb),
        '--glass': 'oklch(1 0 0 / 0.62)', '--glass-2': 'oklch(1 0 0 / 0.72)', '--glass-strong': ok(0.99, 0.006, hb, 0.93),
        '--panel': 'oklch(1 0 0 / 0.78)', '--panel-2': ok(0.94, 0.014 * c, hb, 0.7), '--panel-3': ok(0.9, 0.018 * c, hb, 0.78),
        '--line': ok(0.3, 0.04 * c, hb, 0.1), '--line-2': ok(0.3, 0.04 * c, hb, 0.19), '--hi': 'oklch(1 0 0 / 0.85)',
        '--fg': ok(0.23, 0.03, hb), '--muted': ok(0.45, 0.03 * c, hb), '--faint': ok(0.6, 0.02 * c, hb),
        '--accent': ok(0.56, ac, h1), '--accent-2': ok(0.55, Math.max(0.04, 0.21 * c), h2), '--accent-3': ok(0.6, Math.max(0.04, 0.17 * c), h3),
        '--accent-soft': ok(0.56, ac, h1, 0.12), '--accent-fg': ok(0.99, 0.01, h1), '--info-soft': ok(0.56, ac, h1, 0.1),
        '--glow-1': ok(0.82, 0.13 * c, h1), '--glow-2': ok(0.82, 0.13 * c, h2), '--glow-3': ok(0.85, 0.12 * c, h3),
        '--grid-dot': ok(0.3, 0.05 * c, hb, 0.09),
        '--ok': ok(0.57, 0.16, 152), '--ok-soft': ok(0.57, 0.16, 152, 0.12), '--bad': ok(0.57, 0.2, 25), '--bad-soft': ok(0.57, 0.2, 25, 0.1),
        '--warn': ok(0.64, 0.15, 70), '--warn-soft': ok(0.64, 0.15, 70, 0.12),
        '--shadow': '0 1px 0 var(--hi) inset, 0 18px 40px -26px ' + ok(0.3, 0.05, hb, 0.4),
        '--shadow-lg': '0 1px 0 var(--hi) inset, 0 30px 70px -24px ' + ok(0.3, 0.05, hb, 0.45),
        '--s-pending': ok(0.62, 0.03, hb), '--s-precheck_running': ok(0.62, 0.14, 230), '--s-precheck_passed': ok(0.62, 0.12, 185),
        '--s-precheck_failed': ok(0.66, 0.17, 50), '--s-importing': ok(0.55, 0.19, 265), '--s-awaiting_commit': ok(0.72, 0.16, 80),
        '--s-committed': ok(0.6, 0.17, 152), '--s-failed': ok(0.58, 0.21, 25), '--s-rolling_back': ok(0.6, 0.16, 305),
        '--s-rolled_back': ok(0.52, 0.17, 295), '--s-skipped': ok(0.75, 0.02, hb),
      });
    }
    return v;
  }

  function applyTheme() {
    const t = theme();
    const root = document.documentElement;
    const vars = palette(t);
    for (const k in vars) root.style.setProperty(k, vars[k]);
    root.style.colorScheme = t.mode;
    root.dataset.theme = t.mode;
    document.body.dataset.motion = P.motion;
    document.body.dataset.density = P.density === 'compact' ? 'compact' : 'comfortable';
    readColors();
    applyMood();
  }

  // --------------------------------------------------------------- mood
  const M = { hue: null, strength: 0, busy: false, done: false, fail: 0, burstAt: 0 };
  function onPulse(p) {
    const total = p.total || 0;
    const committed = (p.counts || {}).committed || 0;
    const busy = !!p.job;
    const done = total > 0 && committed === total;
    if (busy) document.body.dataset.busy = '1'; else delete document.body.dataset.busy;
    if (done && M.done === false && M.seen) celebrate();
    M.seen = true;
    Object.assign(M, { busy, done, fail: total ? (p.failed || 0) / total : 0, hold: (p.awaiting_commit || 0) > 0 });
    applyMood();
  }
  function applyMood() {
    let hue = H.h1, strength = 0;
    if (P.reactive) {
      if (M.fail > 0) { hue = 25; strength = Math.min(1, 0.35 + M.fail * 3); }
      else if (M.hold) { hue = 85; strength = 0.6; }
      else if (M.done) { hue = 152; strength = 0.7; }
      else if (M.busy) { hue = H.h1; strength = 0.5; }
    }
    M.hue = hue; M.strength = strength;
    const root = document.documentElement.style;
    root.setProperty('--mood', ok(H.mode === 'dark' ? 0.72 : 0.6, 0.2, hue));
    root.setProperty('--aura', strength.toFixed(2));
  }
  function celebrate() {
    M.burstAt = performance.now();
    if (P.motion === 'off') return;
    const s = ST;
    const lane = s.lanes.find((l) => l.id === 'vcfa');
    if (!lane) return;
    for (let i = 0; i < 140; i++) {
      const a = Math.random() * Math.PI * 2, v = 1 + Math.random() * 3.5;
      s.sparks.push({ x: (lane.x0 + lane.x1) / 2, y: (lane.y0 + lane.y1) / 2, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 1,
        life: 1, decay: 0.008 + Math.random() * 0.012, col: [colors.committed, colors.accent, colors.accent2][i % 3], burst: true });
    }
  }

  // ------------------------------------------------------------ colours
  const colors = {};
  function readColors() {
    const cs = getComputedStyle(document.documentElement);
    const get = (k) => cs.getPropertyValue(k).trim();
    for (const s of ['pending', 'precheck_running', 'precheck_passed', 'precheck_failed', 'importing', 'awaiting_commit',
      'committed', 'failed', 'rolling_back', 'rolled_back', 'skipped']) colors[s] = get('--s-' + s);
    colors.discovered = get('--faint');
    colors.accent = get('--accent');
    colors.accent2 = get('--accent-2');
    colors.line = get('--line-2');
    colors.fg = get('--fg');
  }

  // ------------------------------------------------------------ aurora
  const AU = { canvas: null, ctx: null, w: 0, h: 0, last: 0 };
  const BLOBS = [
    { hk: 'h1', ax: 0.22, ay: 0.18, r: 0.55, sx: 0.045, sy: 0.06, ph: 0 },
    { hk: 'h2', ax: 0.78, ay: 0.26, r: 0.5, sx: 0.035, sy: 0.05, ph: 2.1 },
    { hk: 'h3', ax: 0.58, ay: 0.82, r: 0.58, sx: 0.05, sy: 0.04, ph: 4.2 },
    { hk: 'mood', ax: 0.12, ay: 0.78, r: 0.46, sx: 0.06, sy: 0.045, ph: 1.3 },
  ];
  function sizeAurora() {
    if (!AU.canvas) return;
    AU.w = AU.canvas.width = Math.max(64, Math.round(innerWidth * 1.2 / 6));
    AU.h = AU.canvas.height = Math.max(48, Math.round(innerHeight * 1.2 / 6));
  }
  function drawAurora(t) {
    const { ctx, w, h } = AU;
    if (!ctx) return;
    const dark = H.mode === 'dark';
    const speed = (M.busy ? 2.4 : 1) * (P.motion === 'calm' ? 0.35 : 1);
    const time = t / 1000 * speed;
    const flash = Math.max(0, 1 - (t - M.burstAt) / 2500);
    ctx.clearRect(0, 0, w, h);
    ctx.globalCompositeOperation = dark ? 'lighter' : 'source-over';
    for (const b of BLOBS) {
      let hue = b.hk === 'mood' ? (M.strength > 0 ? M.hue : H.h2) : H[b.hk];
      const x = (b.ax + Math.sin(time * b.sx * 6 + b.ph) * 0.12) * w;
      const y = (b.ay + Math.cos(time * b.sy * 6 + b.ph * 1.3) * 0.1) * h;
      const r = b.r * Math.max(w, h) * (b.hk === 'mood' ? 0.6 + M.strength * 0.6 : 1) * (1 + flash * 0.4);
      const a = b.hk === 'mood' ? (dark ? 0.25 + M.strength * 0.55 : 0.12 + M.strength * 0.22) : (dark ? 0.55 : 0.42);
      const g = ctx.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, ok(dark ? 0.62 : 0.84, dark ? 0.2 : 0.12, hue, a));
      g.addColorStop(1, ok(dark ? 0.62 : 0.84, dark ? 0.2 : 0.12, hue, 0));
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
    }
    ctx.globalCompositeOperation = 'source-over';
  }

  // --------------------------------------------------- migration stream
  const LANES = [
    { id: 'vcenter', label: 'vCenter' }, { id: 'queued', label: 'Queued' }, { id: 'precheck', label: 'Precheck' },
    { id: 'import', label: 'Import' }, { id: 'vcfa', label: 'VCF Automation' },
  ];
  const LANE_OF = {
    discovered: 'vcenter', skipped: 'vcenter', pending: 'queued', precheck_running: 'precheck', precheck_passed: 'precheck',
    importing: 'import', awaiting_commit: 'import', rolling_back: 'import', committed: 'vcfa',
    failed: 'attn', precheck_failed: 'attn', rolled_back: 'attn',
  };
  const ORDER = Object.keys(LANE_OF);
  const MOVING = new Set(['precheck_running', 'importing', 'rolling_back']);
  const ST = { canvas: null, ctx: null, w: 0, h: 0, dpr: 1, dots: [], id: 1, unit: 1, lanes: [], sparks: [], size: 6,
    counts: {}, animating: false, ro: null, host: null };
  const stats = { frames: 0, drawMs: 0, dots: 0 };

  // Grouped streams (by wave, namespace or application): each lane is a group,
  // and its tank stacks by state -- done at the bottom, trouble on top.
  const STACK = ['committed', 'awaiting_commit', 'importing', 'rolling_back', 'precheck_passed', 'precheck_running',
    'pending', 'rolled_back', 'precheck_failed', 'failed', 'skipped'];
  const GROUP_BY = [['stage', 'Stage'], ['wave', 'Wave'], ['namespace', 'Namespace'], ['app', 'App']];
  const MAX_LANES = 8;
  const sumOf = (o) => Object.values(o || {}).reduce((a, b) => a + (b || 0), 0);
  const keyState = (k) => k.slice(k.lastIndexOf('|') + 1);
  const keyLane = (k) => (ST.model && ST.model.by !== 'stage' ? k.slice(0, k.lastIndexOf('|')) : LANE_OF[k]);

  function streamCounts(d) {
    const c = Object.assign({}, d.counts || {});
    c.discovered = Math.max(0, ((d.discovered || {}).total || 0) - (d.total || 0));
    return c;
  }
  /** {by, lanes: [{id, label, total, done}], counts: {key: n}}; key = state, or lane|state when grouped. */
  function streamModel(d) {
    let by = P.streamBy || 'stage';
    let groups = [];
    if (by === 'wave') groups = (d.waves || []).map((w) => ({ id: 'w' + w.wave, label: 'Wave ' + w.wave, counts: w.counts }));
    else if (by === 'namespace') groups = (d.namespaces || []).map((x) => ({ id: 'n:' + x.namespace, label: x.namespace, counts: x.counts }));
    else if (by === 'app' && d.apps && d.apps.length) {
      groups = d.apps.map((a) => ({ id: 'a:' + a.app, label: a.app, counts: a.counts }));
      const rest = {};
      for (const s in d.counts || {}) {
        const left = d.counts[s] - groups.reduce((a, g) => a + (g.counts[s] || 0), 0);
        if (left > 0) rest[s] = left;
      }
      if (sumOf(rest)) groups.push({ id: 'a:', label: 'No app', counts: rest });
    }
    if (by !== 'stage' && !groups.length) by = 'stage';
    if (by === 'stage') {
      const c = streamCounts(d), counts = {};
      for (const s of ORDER) if (c[s]) counts[s] = c[s];
      return { by, counts, lanes: LANES.map((l) => ({ id: l.id, label: l.label,
        total: ORDER.filter((s) => LANE_OF[s] === l.id).reduce((a, s) => a + (c[s] || 0), 0) })) };
    }
    groups.forEach((g) => { g.total = sumOf(g.counts); });
    if (groups.length > MAX_LANES) {
      const keep = by === 'wave' ? groups.slice(0, MAX_LANES - 1)
        : groups.slice().sort((a, b) => b.total - a.total).slice(0, MAX_LANES - 1);
      const ids = new Set(keep.map((g) => g.id));
      const rest = { id: 'other', label: '+' + (groups.length - keep.length) + ' more', counts: {}, total: 0 };
      for (const g of groups) {
        if (ids.has(g.id)) continue;
        for (const s in g.counts) rest.counts[s] = (rest.counts[s] || 0) + g.counts[s];
        rest.total += g.total;
      }
      groups = keep.concat([rest]);
    }
    const counts = {};
    for (const g of groups) for (const s in g.counts) if (g.counts[s]) counts[g.id + '|' + s] = g.counts[s];
    return { by, counts, lanes: groups.map((g) => ({ id: g.id, label: g.label, total: g.total, done: g.counts.committed || 0 })) };
  }
  function streamHtml(d, opts) {
    const compact = !!(opts && opts.compact);
    const m = streamModel(d);
    const staged = m.by === 'stage';
    const attn = staged ? ['failed', 'precheck_failed', 'rolled_back'].reduce((a, s) => a + (m.counts[s] || 0), 0) : 0;
    const total = sumOf(m.counts);
    const unit = Math.max(1, Math.ceil(total / 1500));
    const hasApps = !!(d.apps && d.apps.length);
    const choices = GROUP_BY.filter(([k]) => k !== 'app' || hasApps);
    return html`<div class="card hero ${compact ? 'compact' : ''}">
      ${compact ? '' : html`<div class="hero-ring">${ring(d)}</div>`}
      <div class="stream ${staged ? '' : 'grouped'}" id="fx-stream" data-model="${JSON.stringify(m)}">
        <canvas aria-hidden="true"></canvas>
        <div class="lanes" style="grid-template-columns:repeat(${m.lanes.length},minmax(0,1fr))">${m.lanes.map((l) => html`<div class="lane-h">
          <div class="ln-n" data-count="${l.total}" data-key="lane:${l.id}">${n(l.total)}</div><div class="ln-l" title="${l.label}">${l.label}</div>
          ${staged ? '' : html`<div class="ln-d">${pct(l.done, l.total)} done</div>`}</div>`)}</div>
        <div class="stream-foot"><span>${!total ? 'Discover your vCenter to see the estate flow' : (unit > 1 ? 'each particle = ' + unit + ' VMs' : 'each particle is a VM')}</span>
          <span class="stream-by" role="group" aria-label="Group the stream by"><span class="lbl">lanes</span>${choices.map(([k, l]) =>
            html`<button class="${m.by === k ? 'on' : ''}" data-fx="streamBy" data-k="${k}" aria-pressed="${m.by === k}">${l}</button>`)}</span>
          ${attn ? html`<span class="att">${plural(attn, 'VM')} need attention ↓</span>` : html`<span>${staged ? 'live' : 'by ' + m.by + ', colour = state'}</span>`}</div>
      </div></div>`;
  }
  function ring(d) {
    const c = d.counts || {};
    const total = d.total || 0;
    const committed = c.committed || 0;
    const moving = ['precheck_passed', 'precheck_running', 'importing', 'awaiting_commit', 'rolling_back'].reduce((a, s) => a + (c[s] || 0), 0);
    const attn = ['failed', 'precheck_failed', 'rolled_back'].reduce((a, s) => a + (c[s] || 0), 0);
    const r = 86, C = 2 * Math.PI * r;
    let start = 0;
    const arcs = [['committed', committed, 'var(--s-committed)'], ['moving', moving, 'var(--accent)'], ['attn', attn, 'var(--s-failed)']]
      .map(([k, v, col]) => {
        const len = total ? Math.max(0, C * v / total - (v ? 3 : 0)) : 0;
        const out = html`<circle class="arc" cx="100" cy="100" r="${r}" stroke-width="12" data-key="ring:${k}" data-da="${len.toFixed(1)} ${C.toFixed(1)}"
          style="--rc:${col};stroke-dasharray:${len.toFixed(1)} ${C.toFixed(1)};stroke-dashoffset:${(-start).toFixed(1)}"></circle>`;
        start += total ? C * v / total : 0;
        return out;
      });
    const pc = total ? Math.round(100 * committed / total) : 0;
    return html`<div class="ring"><svg viewBox="0 0 200 200" aria-hidden="true"><circle class="track" cx="100" cy="100" r="${r}" stroke-width="12"></circle>${arcs}</svg>
      <div class="ring-center"><div class="pc" data-count="${pc}" data-key="ring:pc" data-suffix="%">${pc}%</div>
        <div class="lb">committed</div><div class="sm">${n(committed)} of ${n(total)} queued VMs</div></div></div>`;
  }

  function mountStream(root) {
    const host = root.querySelector('#fx-stream');
    if (!host) return;
    let model = null;
    try { model = JSON.parse(host.dataset.model || 'null'); } catch (e) { model = null; }
    if (!model) model = { by: 'stage', lanes: LANES, counts: {} };
    const fresh = host.querySelector('canvas');
    if (!ST.canvas) {
      ST.canvas = fresh;
      ST.ctx = ST.canvas.getContext('2d');
    } else if (fresh !== ST.canvas) {
      fresh.replaceWith(ST.canvas);   // keep the particles alive across refreshes
    }
    if (ST.host !== host) {
      ST.host = host;
      if (ST.ro) ST.ro.disconnect();
      if (window.ResizeObserver) { ST.ro = new ResizeObserver(() => { sizeStream(); layout(); }); ST.ro.observe(host); }
      sizeStream();
    }
    setStreamData(model);
  }
  function sizeStream() {
    if (!ST.host) return;
    const r = ST.host.getBoundingClientRect();
    ST.dpr = Math.min(2, window.devicePixelRatio || 1);
    ST.w = Math.max(10, r.width); ST.h = Math.max(10, r.height);
    ST.canvas.width = Math.round(ST.w * ST.dpr); ST.canvas.height = Math.round(ST.h * ST.dpr);
  }
  function setStreamData(model) {
    const counts = model.counts || {};
    ST.model = model;
    ST.unit = Math.max(1, Math.ceil(sumOf(counts) / 1500));
    const keys = Object.keys(counts);
    const want = {};
    for (const k of keys) want[k] = Math.max(1, Math.round(counts[k] / ST.unit));
    // Reconcile by key (a state, or lane|state when grouped): dots whose key lost
    // members are freed, and keys that gained take freed dots first -- so VMs
    // visibly flow to their new lane, and a regroup re-sorts the same particles.
    // A freed dot of the same state is preferred, so a regroup keeps colours.
    const byKey = {};
    for (const d of ST.dots) if (!d.dying) (byKey[d.key] = byKey[d.key] || []).push(d);
    const freeBy = {};
    let freeN = 0;
    for (const k in byKey) {
      const have = byKey[k], w = want[k] || 0;
      if (have.length > w) for (const d of have.splice(w)) { (freeBy[d.state] = freeBy[d.state] || []).push(d); freeN++; }
    }
    const take = (state) => {
      if (!freeN) return null;
      let list = freeBy[state];
      if (!list || !list.length) list = Object.values(freeBy).find((l) => l.length);
      freeN--;
      return list.shift();
    };
    const first = !ST.dots.length;
    for (const k of keys) {
      const have = byKey[k] || (byKey[k] = []);
      const state = keyState(k);
      while (have.length < want[k]) {
        let d = take(state);
        if (d) { d.key = k; d.state = state; d.delay = performance.now() + Math.random() * 700; }
        else {
          const still = P.motion === 'off';     // no frames will follow: arrive fully formed
          d = { id: ST.id++, key: k, state, x: 0, y: 0, tx: 0, ty: 0, a: still ? 1 : 0,
            born: still ? 0 : performance.now() + Math.random() * (first ? 900 : 300),
            ph: Math.random() * 6.28, delay: 0, fresh: true };
          ST.dots.push(d);
        }
        have.push(d);
      }
    }
    for (const list of Object.values(freeBy)) for (const d of list) d.dying = performance.now();
    ST.counts = counts;
    layout();
    stats.dots = ST.dots.length;
  }
  function layout() {
    const w = ST.w, h = ST.h;
    if (!w || !h) return;
    const staged = !ST.model || ST.model.by === 'stage';
    const defs = ST.model ? ST.model.lanes : LANES;
    const top = staged ? 74 : 88, attnH = staged ? Math.max(44, h * 0.18) : 0;
    const bottom = staged ? h - attnH - 30 : h - 32, colW = w / Math.max(1, defs.length);
    ST.lanes = defs.map((l, i) => ({ id: l.id, x0: i * colW + 10, x1: (i + 1) * colW - 10, y0: top, y1: bottom }));
    if (staged) ST.lanes.push({ id: 'attn', x0: 16, x1: w - 16, y0: bottom + 14, y1: h - 30 });
    const members = {};
    for (const d of ST.dots) if (!d.dying) { const l = keyLane(d.key); (members[l] = members[l] || []).push(d); }
    // one particle size for every lane, so lanes compare at a glance
    let size = 12;
    for (const lane of ST.lanes) {
      const m = (members[lane.id] || []).length;
      if (!m) continue;
      const area = (lane.x1 - lane.x0) * (lane.y1 - lane.y0);
      size = Math.min(size, Math.sqrt(area / m) * 0.92);
    }
    size = Math.max(2.4, size);
    ST.size = size;
    for (const lane of ST.lanes) {
      const rank = staged ? ORDER : STACK;
      const list = (members[lane.id] || []).sort((a, b) => rank.indexOf(a.state) - rank.indexOf(b.state) || a.id - b.id);
      const lw = lane.x1 - lane.x0;
      if (lane.id === 'attn') {
        const rows = Math.max(1, Math.floor((lane.y1 - lane.y0) / size));
        list.forEach((d, k) => {
          d.tx = lane.x0 + Math.floor(k / rows) * size + size / 2;
          d.ty = lane.y0 + (k % rows) * size + size / 2;
        });
        continue;
      }
      const cols = Math.max(1, Math.floor(lw / size));
      const used = Math.min(cols, list.length) * size;
      const x0 = lane.x0 + (lw - used) / 2;
      list.forEach((d, k) => {     // tanks fill from the bottom
        d.tx = x0 + (k % cols) * size + size / 2;
        d.ty = lane.y1 - Math.floor(k / cols) * size - size / 2;
      });
    }
    for (const d of ST.dots) {
      if (d.fresh) {
        const lane = ST.lanes.find((l) => l.id === keyLane(d.key)) || ST.lanes[0];
        d.x = d.tx; d.y = P.motion === 'off' ? d.ty : lane.y0 - 10 - Math.random() * 40; d.fresh = false;
      }
      if (P.motion === 'off') { d.x = d.tx; d.y = d.ty; }
    }
    if (P.motion === 'off') drawStream(performance.now());
  }
  function drawStream(t) {
    const { ctx, dpr } = ST;
    if (!ctx || !ST.canvas.isConnected) return false;
    const t0 = performance.now();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, ST.w, ST.h);
    // lane guides
    ctx.strokeStyle = colors.line || 'rgba(255,255,255,.1)';
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 6]);
    const staged = !ST.model || ST.model.by === 'stage';
    const nLanes = staged ? LANES.length : Math.max(1, ST.lanes.length);
    for (let i = 1; i < nLanes; i++) {
      const x = (ST.w / nLanes) * i;
      ctx.beginPath(); ctx.moveTo(x, 70); ctx.lineTo(x, ST.lanes.length ? ST.lanes[0].y1 : ST.h - 60); ctx.stroke();
    }
    const attn = staged ? ST.lanes[ST.lanes.length - 1] : null;
    if (attn) { ctx.beginPath(); ctx.moveTo(16, attn.y0 - 7); ctx.lineTo(ST.w - 16, attn.y0 - 7); ctx.stroke(); }
    ctx.setLineDash([]);
    const moving = P.motion !== 'off';
    let active = false;
    const r = Math.max(1.1, ST.size * 0.34);
    // Time-based, not frame-based: the same speed at 144fps, 30fps, or in a
    // throttled background tab. `ease` is the share of the gap closed this frame.
    const dt = Math.min(250, Math.max(1, t - (ST.lastT || t - 16.7)));
    ST.lastT = t;
    const ease = 1 - Math.pow(1 - 0.075, dt / 16.7);
    const survivors = [];
    for (const d of ST.dots) {
      if (!moving) {                       // still mode: settled, opaque, no fades
        if (d.dying) continue;
        d.a = 1; d.x = d.tx; d.y = d.ty;
      } else if (d.dying) {
        const k = (t - d.dying) / 600;
        if (k >= 1) continue;
        d.a = 1 - k; active = true;
      } else if (t < d.born) { survivors.push(d); active = true; continue; }
      else d.a = Math.min(1, (t - d.born) / 380);
      survivors.push(d);
      if (moving && t >= d.delay) {
        const dx = d.tx - d.x, dy = d.ty - d.y;
        if (Math.abs(dx) + Math.abs(dy) > 0.3) {
          active = true;
          const px = d.x, py = d.y;
          d.x += dx * ease; d.y += dy * ease;
          if (Math.abs(dx) > 8) {          // a comet tail while crossing lanes
            ctx.strokeStyle = colors[d.state];
            ctx.globalAlpha = 0.35 * d.a;
            ctx.lineWidth = r * 1.4;
            ctx.beginPath(); ctx.moveTo(px - dx * 0.12, py - dy * 0.12); ctx.lineTo(d.x, d.y); ctx.stroke();
          }
        } else { d.x = d.tx; d.y = d.ty; }
      }
      const tw = moving ? 0.72 + 0.28 * Math.sin(t / 700 + d.ph) : 1;
      const pulse = moving && MOVING.has(d.state) ? 1 + 0.35 * Math.sin(t / 220 + d.ph) : 1;
      ctx.globalAlpha = Math.max(0, d.a * tw);
      ctx.fillStyle = colors[d.state] || colors.pending;
      ctx.beginPath(); ctx.arc(d.x, d.y, r * pulse, 0, 6.2832); ctx.fill();
    }
    ST.dots = survivors;
    // sparks: flow between lanes while a job runs, and the finale burst
    if (moving && M.busy && staged && ST.lanes.length > 4 && Math.random() < 0.5) {
      const i = 1 + Math.floor(Math.random() * 3);
      const a = ST.lanes[i], b = ST.lanes[i + 1];
      ST.sparks.push({ x: (a.x0 + a.x1) / 2, y: a.y1 - 8, tx: (b.x0 + b.x1) / 2, ty: b.y1 - 8, p: 0, v: 0.008 + Math.random() * 0.01, col: colors.accent });
    }
    if (ST.sparks.length) {
      active = true;
      ctx.globalCompositeOperation = H.mode === 'dark' ? 'lighter' : 'source-over';
      ST.sparks = ST.sparks.filter((s) => {
        if (s.burst) {
          s.x += s.vx; s.y += s.vy; s.vy += 0.04; s.life -= s.decay;
          if (s.life <= 0) return false;
          ctx.globalAlpha = s.life; ctx.fillStyle = s.col;
          ctx.beginPath(); ctx.arc(s.x, s.y, 2.2, 0, 6.2832); ctx.fill();
          return true;
        }
        s.p += s.v;
        if (s.p >= 1) return false;
        const mx = (s.x + s.tx) / 2, my = Math.min(s.y, s.ty) - 70;
        const q = s.p, x = (1 - q) * (1 - q) * s.x + 2 * (1 - q) * q * mx + q * q * s.tx;
        const y = (1 - q) * (1 - q) * s.y + 2 * (1 - q) * q * my + q * q * s.ty;
        ctx.globalAlpha = Math.sin(q * Math.PI) * 0.9; ctx.fillStyle = s.col;
        ctx.beginPath(); ctx.arc(x, y, 1.8, 0, 6.2832); ctx.fill();
        return true;
      });
      ctx.globalCompositeOperation = 'source-over';
    }
    ctx.globalAlpha = 1;
    const ms = performance.now() - t0;
    stats.frames++;
    stats.drawMs = stats.drawMs ? stats.drawMs * 0.95 + ms * 0.05 : ms;
    return active;
  }

  // --------------------------------------------------------------- loop
  let raf = 0, lastStream = 0;
  function loop(t) {
    raf = 0;
    if (document.hidden || P.motion === 'off') return;
    const auroraFps = P.motion === 'calm' ? 12 : 30;
    if (t - AU.last > 1000 / auroraFps) { AU.last = t; drawAurora(t); }
    if (ST.canvas && ST.canvas.isConnected) {
      // full rate while particles move; a gentle twinkle rate when idle
      const busy = drawStreamThrottled(t);
      if (busy) lastStream = t;
    }
    raf = requestAnimationFrame(loop);
  }
  let lastIdleDraw = 0;
  function drawStreamThrottled(t) {
    if (t - lastStream < 1500 || t - lastIdleDraw > 1000 / (P.motion === 'calm' ? 10 : 24)) {
      lastIdleDraw = t;
      return drawStream(t);
    }
    return false;
  }
  function kick() {
    if (!raf && P.motion !== 'off' && !document.hidden) raf = requestAnimationFrame(loop);
    if (P.motion === 'off') { drawAurora(0); if (ST.canvas) drawStream(performance.now()); }
  }

  // ------------------------------------------------------------- motion
  const counters = new Map();
  let entering = false;
  function countUp(el) {
    for (const node of el.querySelectorAll('[data-count]')) {
      const to = +node.dataset.count;
      const key = node.dataset.key;
      const suffix = node.dataset.suffix || '';
      const had = key && counters.has(key);
      const from = had ? counters.get(key) : (entering ? 0 : to);
      if (key) counters.set(key, to);
      if (from === to || P.motion === 'off' || isNaN(to)) continue;
      const start = performance.now(), dur = Math.min(1200, 500 + Math.abs(to - from) * 0.8);
      node.textContent = Math.round(from).toLocaleString() + suffix;
      const step = (now) => {
        if (!node.isConnected) return;
        const k = Math.min(1, (now - start) / dur);
        const e = 1 - Math.pow(1 - k, 3);
        node.textContent = Math.round(from + (to - from) * e).toLocaleString() + suffix;
        if (k < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }
  }
  const arcs = new Map();
  function animateArcs(el) {
    for (const arc of el.querySelectorAll('.ring .arc[data-key]')) {
      const key = arc.dataset.key, next = arc.dataset.da;
      const C = next.split(' ')[1];
      const prev = arcs.has(key) ? arcs.get(key) : (entering ? '0 ' + C : next);
      arcs.set(key, next);
      if (prev === next || P.motion === 'off') continue;
      arc.style.transition = 'none';
      arc.style.strokeDasharray = prev;
      arc.getBoundingClientRect();
      arc.style.transition = '';
      requestAnimationFrame(() => { arc.style.strokeDasharray = next; });
    }
  }
  function navGlider() {
    const g = document.getElementById('nav-glider');
    const nav = document.getElementById('nav');
    const a = nav && nav.querySelector('.nav-item.active');
    if (!g) return;
    if (!a) { g.classList.remove('on'); return; }
    g.style.setProperty('--gy', (a.offsetTop - nav.scrollTop) + 'px');
    g.classList.add('on');
  }
  let pointer = null;
  function onPointer(e) {
    pointer = e;
    if (pointerQueued) return;
    pointerQueued = true;
    requestAnimationFrame(applyPointer);
  }
  let pointerQueued = false, tilted = null;
  function applyPointer() {
    pointerQueued = false;
    const e = pointer;
    if (!e) return;
    if (P.motion === 'full') {
      document.documentElement.style.setProperty('--px', ((e.clientX / innerWidth - 0.5) * -14).toFixed(1) + 'px');
      document.documentElement.style.setProperty('--py', ((e.clientY / innerHeight - 0.5) * -10).toFixed(1) + 'px');
    }
    const t = e.target && e.target.closest ? e.target : null;
    const card = t && t.closest('.card, .kpi');
    if (card) {
      const r = card.getBoundingClientRect();
      card.style.setProperty('--mx', (e.clientX - r.left) + 'px');
      card.style.setProperty('--my', (e.clientY - r.top) + 'px');
    }
    const kpi = P.motion === 'full' && t ? t.closest('.kpi') : null;
    if (tilted && tilted !== kpi) { tilted.style.removeProperty('--tx'); tilted.style.removeProperty('--ty'); }
    if (kpi) {
      const r = kpi.getBoundingClientRect();
      kpi.style.setProperty('--tx', (((e.clientY - r.top) / r.height - 0.5) * -7).toFixed(2) + 'deg');
      kpi.style.setProperty('--ty', (((e.clientX - r.left) / r.width - 0.5) * 9).toFixed(2) + 'deg');
    }
    tilted = kpi;
  }

  // ------------------------------------------------------- dock sparkline
  const activity = new Array(60).fill(0);
  let activitySec = Math.floor(Date.now() / 1000);
  function logActivity(lines) {
    rollActivity();
    activity[activity.length - 1] += lines;
  }
  function rollActivity() {
    const now = Math.floor(Date.now() / 1000);
    while (activitySec < now) { activity.shift(); activity.push(0); activitySec++; if (now - activitySec > 60) activitySec = now; }
  }
  function spark() {
    rollActivity();
    const max = Math.max(4, ...activity);
    const pts = activity.map((v, i) => [(i / (activity.length - 1)) * 120, 24 - (v / max) * 21]);
    const line = 'M' + pts.map((p) => p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' L');
    return html`<svg class="spark" viewBox="0 0 120 26" preserveAspectRatio="none" aria-hidden="true">
      <path class="area" d="${line + ' L120 26 L0 26 Z'}"></path><path d="${line}"></path></svg>`;
  }

  // ---------------------------------------------------- command palette
  const PAL = { open: false, items: [], sel: 0, q: '', vms: null, batches: null, fetched: 0 };
  function openPalette() {
    const root = document.getElementById('fx-root');
    PAL.open = true; PAL.q = ''; PAL.sel = 0;
    root.innerHTML = fmt(html`<div class="overlay cmdk-ov" data-fx="closePalette"></div>
      <div class="cmdk" role="dialog" aria-modal="true" aria-label="Command palette">
        <div class="cmdk-in">${icon('search')}<input id="cmdk-q" placeholder="Jump to a page, VM or batch, run an action, switch theme…" autocomplete="off" spellcheck="false"><span class="kbd">esc</span></div>
        <div class="cmdk-list" id="cmdk-list"></div>
        <div class="cmdk-foot"><span><span class="kbd">↑</span> <span class="kbd">↓</span> move</span><span><span class="kbd">enter</span> run</span><span><span class="kbd">ctrl</span> <span class="kbd">k</span> anywhere</span></div>
      </div>`);
    const q = document.getElementById('cmdk-q');
    q.addEventListener('input', () => { PAL.q = q.value; PAL.sel = 0; renderPalette(); });
    q.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { PAL.sel = Math.min(PAL.items.length - 1, PAL.sel + 1); renderPalette(); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { PAL.sel = Math.max(0, PAL.sel - 1); renderPalette(); e.preventDefault(); }
      else if (e.key === 'Enter') { e.preventDefault(); runItem(PAL.items[PAL.sel]); }
      else if (e.key === 'Escape') { e.preventDefault(); closePalette(); }
    });
    q.focus();
    renderPalette();
    if (Date.now() - PAL.fetched > 15000) {
      PAL.fetched = Date.now();
      Promise.all([GET('/api/vms').catch(() => null), GET('/api/batches').catch(() => null)]).then(([v, b]) => {
        PAL.vms = v ? v.vms : []; PAL.batches = b ? b.batches : [];
        if (PAL.open) renderPalette();
      });
    }
  }
  function closePalette() { PAL.open = false; document.getElementById('fx-root').innerHTML = ''; }
  function score(text, q) {
    if (!q) return 1;
    text = text.toLowerCase();
    const at = text.indexOf(q);
    if (at >= 0) return 100 - at;
    let i = 0;
    for (const ch of text) if (ch === q[i]) i++;
    return i === q.length ? 10 : 0;
  }
  function paletteItems() {
    const out = [];
    const add = (sec, label, ic, hint, run) => out.push({ sec, label, ic, hint, run });
    for (const it of NAV) if (it.id) add('Go to', it.label, it.icon, it.step ? 'step ' + it.step : '', () => go(it.id));
    add('Actions', 'Run preflight', 'shield', 'read-only checks', () => startJob('preflight', {}));
    add('Actions', 'Refresh from cluster', 'refresh', 'poll live batches once', () => startJob('refresh', {}));
    add('Actions', 'Watch in-flight batches', 'refresh', 'until done', () => startJob('watch', {}));
    add('Actions', 'Precheck…', 'shield', 'Execute', () => go('execute', 'stage=precheck'));
    add('Actions', 'Import…', 'execute', 'Execute', () => go('execute', 'stage=import'));
    add('Actions', 'Download tracker.csv', 'download', 'export', () => { location.href = exportUrl('tracker.csv'); });
    add('Look & feel', 'Open Theme Studio', 'sun', 'colours, glow, motion', () => openStudio());
    for (const t of THEMES) add('Look & feel', 'Theme: ' + t.label, 'sun', t.tag, () => { set({ theme: t.id }); toast('Theme: ' + t.label, t.tag, 'ok', 2500); });
    add('Look & feel', 'Motion: ' + (P.motion === 'off' ? 'turn on' : 'turn off'), 'activity', 'animations', () => set({ motion: P.motion === 'off' ? 'full' : 'off' }));
    add('Look & feel', 'Density: ' + (P.density === 'compact' ? 'comfortable' : 'compact'), 'queue', 'row height', () => set({ density: P.density === 'compact' ? 'comfortable' : 'compact' }));
    for (const [k, l] of GROUP_BY) add('Look & feel', 'Stream lanes by ' + l.toLowerCase(), 'waves', 'Migration Stream', () => streamBy(k));
    if (window.GOV) for (const it of GOV.paletteItems()) add(it.sec, it.label, it.ic, it.hint, it.run);
    const q = PAL.q.trim().toLowerCase();
    if (PAL.vms && q.length >= 2) {
      PAL.vms.filter((v) => score(v.vm_name + ' ' + v.moref, q) >= 50).slice(0, 8)
        .forEach((v) => add('VMs', v.vm_name, 'queue', label(v.state) + ' · wave ' + v.wave, () => vmDrawer(v.moref)));
    }
    if (PAL.batches && q.length >= 2) {
      PAL.batches.filter((b) => score(b.name + ' ' + b.namespace, q) >= 50).slice(0, 5)
        .forEach((b) => add('Batches', b.name, 'batches', b.state + ' · ' + b.namespace, () => batchDrawer(b.namespace, b.name)));
    }
    const hits = out.map((it, i) => Object.assign(it, { i, s: score(it.label + ' ' + (it.hint || ''), q) })).filter((it) => it.s > 0);
    const best = {};
    for (const it of hits) best[it.sec] = Math.max(best[it.sec] || 0, it.s);
    // sections stay together, ordered by their best match; items by score within
    return hits.sort((a, b) => (q ? (best[b.sec] - best[a.sec]) || (a.sec < b.sec ? -1 : a.sec > b.sec ? 1 : 0) || (b.s - a.s) : a.i - b.i))
      .slice(0, 40);
  }
  function renderPalette() {
    PAL.items = paletteItems();
    const list = document.getElementById('cmdk-list');
    if (!list) return;
    if (!PAL.items.length) { list.innerHTML = fmt(html`<div class="cmdk-empty">Nothing matches “${PAL.q}”.</div>`); return; }
    let sec = '';
    list.innerHTML = fmt(PAL.items.map((it, i) => {
      const head = it.sec !== sec ? html`<div class="cmdk-sec">${it.sec}</div>` : '';
      sec = it.sec;
      return html`${head}<div class="cmdk-item ${i === PAL.sel ? 'on' : ''}" data-fx="runItem" data-i="${i}">${icon(it.ic)}<span>${it.label}</span><span class="hint">${it.hint}</span></div>`;
    }));
    const on = list.querySelector('.cmdk-item.on');
    if (on) on.scrollIntoView({ block: 'nearest' });
  }
  function runItem(it) {
    if (!it) return;
    closePalette();
    Promise.resolve(it.run()).catch(fail);
  }

  // --------------------------------------------------------- theme studio
  function openStudio() {
    const root = document.getElementById('fx-root');
    root.innerHTML = fmt(html`<div class="overlay studio-ov" data-fx="closeStudio"></div>
      <aside class="studio" role="dialog" aria-label="Theme Studio" id="fx-studio"></aside>`);
    renderStudio();
  }
  function renderStudio() {
    const el = document.getElementById('fx-studio');
    if (!el) return;
    const sw = (t) => {
      const bg = t.mode === 'dark' ? ok(0.16, 0.03 * t.c, t.hb) : ok(0.97, 0.012 * t.c, t.hb);
      const fg = t.mode === 'dark' ? ok(0.96, 0.01, t.hb) : ok(0.25, 0.03, t.hb);
      const L = t.mode === 'dark' ? 0.66 : 0.8, C = (t.mode === 'dark' ? 0.2 : 0.13) * t.c;
      const bgi = 'radial-gradient(circle at 20% 20%, ' + ok(L, C, t.h1, 0.9) + ', transparent 55%), radial-gradient(circle at 85% 30%, ' +
        ok(L, C, t.h2, 0.85) + ', transparent 50%), radial-gradient(circle at 60% 110%, ' + ok(L, C, t.h3, 0.8) + ', transparent 55%), ' + bg;
      return html`<button class="swatch ${P.theme === t.id ? 'on' : ''}" data-fx="pickTheme" data-id="${t.id}" style="background:${bgi};color:${fg}">
        <span class="dots"><i style="background:${ok(t.mode === 'dark' ? 0.78 : 0.56, 0.16 * Math.max(t.c, 0.2), t.h1)}"></i><i style="background:${ok(0.72, 0.19 * Math.max(t.c, 0.2), t.h2)}"></i><i style="background:${ok(0.8, 0.16 * Math.max(t.c, 0.2), t.h3)}"></i></span>
        <b>${t.label}</b><small>${t.tag}</small></button>`;
    };
    el.innerHTML = fmt(html`<div class="studio-h"><div class="grow"><h2>Theme Studio</h2><div class="small muted">Live — changes apply as you move</div></div>
        <button class="btn ghost icon" data-fx="closeStudio" aria-label="Close">${icon('x')}</button></div>
      <div class="studio-b">
        <div><div class="section-title" style="margin-top:0">Theme</div><div class="swatches">${THEMES.map(sw)}</div></div>
        <label class="field"><span>Hue shift <b class="num">${P.shift > 0 ? '+' : ''}${P.shift}°</b></span>
          <div class="hue-track"></div><input type="range" min="-180" max="180" step="5" value="${P.shift}" data-fx-in="shift"></label>
        <label class="field"><span>Glow <b class="num">${P.glow}%</b></span>
          <input type="range" min="0" max="160" step="5" value="${P.glow}" data-fx-in="glow"></label>
        <div class="field"><span>Motion</span><div class="seg">${[['full', 'Full'], ['calm', 'Calm'], ['off', 'Off']].map(([k, l]) =>
          html`<button class="${P.motion === k ? 'on' : ''}" data-fx="motion" data-k="${k}">${l}</button>`)}</div>
          <small>Off also stops the background and the particle stream. Your OS "reduce motion" setting picks Off by default.</small></div>
        <div class="field"><span>Density</span><div class="seg">${[['comfortable', 'Comfortable'], ['compact', 'Compact']].map(([k, l]) =>
          html`<button class="${(P.density || 'comfortable') === k ? 'on' : ''}" data-fx="density" data-k="${k}">${l}</button>`)}</div>
          <small>Compact fits more rows on a screen: tables, cards and the queue tighten up.</small></div>
        <div class="field"><span>Migration Stream lanes</span><div class="seg">${GROUP_BY.map(([k, l]) =>
          html`<button class="${(P.streamBy || 'stage') === k ? 'on' : ''}" data-fx="streamBy" data-k="${k}">${l}</button>`)}</div>
          <small>By stage shows the flow; by wave, namespace or app shows each group's progress, coloured by state.</small></div>
        <div class="opt-row"><div><b>Reactive ambience</b><div class="d">The glow tints with campaign health: hazard for failures, amber for held commits, green when done.</div></div>
          <button class="switch ${P.reactive ? 'on' : ''}" data-fx="reactive" role="switch" aria-checked="${P.reactive}" aria-label="Reactive ambience"></button></div>
        <div class="row"><button class="btn sm" data-fx="resetFx">Reset to defaults</button><span class="grow"></span>
          <span class="small faint"><span class="kbd">ctrl</span> <span class="kbd">k</span> switches themes too</span></div>
      </div>`);
  }
  function closeStudio() { document.getElementById('fx-root').innerHTML = ''; }
  function streamBy(k) {
    P.streamBy = k;
    savePrefs();
    renderStudio();
    if (S.view && S.view.render) S.view.render();
  }
  function set(changes) {
    Object.assign(P, changes);
    savePrefs();
    applyTheme();
    kick();
    renderStudio();
    if (S.pulse) renderTopbar();
  }

  const ACTIONS = {
    closePalette, closeStudio, openStudio, openPalette,
    runItem: (t) => runItem(PAL.items[+t.dataset.i]),
    pickTheme: (t) => set({ theme: t.dataset.id }),
    motion: (t) => set({ motion: t.dataset.k }),
    reactive: () => set({ reactive: !P.reactive }),
    density: (t) => { set({ density: t.dataset.k }); if (S.view && S.view.render) S.view.render(); },
    streamBy: (t) => streamBy(t.dataset.k),
    resetFx: () => set(Object.assign({}, DEFAULTS, { motion: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'off' : 'full' })),
  };

  // --------------------------------------------------------------- hooks
  function init() {
    AU.canvas = document.getElementById('fx-aurora');
    AU.ctx = AU.canvas ? AU.canvas.getContext('2d') : null;
    sizeAurora();
    applyTheme();
    addEventListener('resize', () => { sizeAurora(); navGlider(); kick(); }, { passive: true });
    document.addEventListener('pointermove', onPointer, { passive: true });
    document.addEventListener('visibilitychange', kick);
    document.addEventListener('click', (e) => {
      const t = e.target.closest('[data-fx]');
      if (!t) return;
      const fn = ACTIONS[t.dataset.fx];
      if (fn) { e.preventDefault(); fn(t, e); }
    });
    document.addEventListener('input', (e) => {
      const t = e.target.closest('[data-fx-in]');
      if (!t) return;
      const k = t.dataset.fxIn;
      P[k] = +t.value;
      savePrefs(); applyTheme();
      const b = t.closest('.field').querySelector('b');
      if (b) b.textContent = k === 'shift' ? (P.shift > 0 ? '+' : '') + P.shift + '°' : P.glow + '%';
    });
    document.addEventListener('keydown', (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '');
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); PAL.open ? closePalette() : openPalette(); }
      else if (e.key === '/' && !typing && !PAL.open) { e.preventDefault(); openPalette(); }
      else if (e.key === 'Escape' && document.getElementById('fx-studio')) closeStudio();
    });
    kick();
  }
  function beforeRoute() { entering = true; }
  function afterRoute(el) {
    entering = false;
    if (P.motion === 'off') return;
    el.setAttribute('data-enter', '');
    clearTimeout(afterRoute.t);
    afterRoute.t = setTimeout(() => el.removeAttribute('data-enter'), 1000);
  }
  function afterRender(el, v) {
    try {
      countUp(el);
      animateArcs(el);
      if (el.querySelector('#fx-stream')) { mountStream(el); kick(); }
    } catch (e) { /* the look must never break the console */ }
  }

  return {
    THEMES, prefs: P, stats, init, applyTheme, onPulse, beforeRoute, afterRoute, afterRender, navGlider,
    streamHtml, streamModel, ring, logActivity, spark, openPalette, openStudio, set, kick,
    // one stream frame on demand (tests drive time; browsers use requestAnimationFrame)
    drawNow: () => drawStream(performance.now()),
  };
})();

window.FX = FX;   // core.js probes window.FX: a top-level const is not a window property
