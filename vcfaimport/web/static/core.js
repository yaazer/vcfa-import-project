'use strict';
/* VCFA Import Console — core: templating, API client, shell, dock, drawers,
 * modals, toasts and the actions shared by several views. views.js holds the
 * pages. Plain scripts, no build step, no network dependencies. */

// ------------------------------------------------------------ templating
class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
const raw = (s) => new Raw(String(s));
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (v) => String(v).replace(/[&<>"']/g, (c) => ESC[c]);
function fmt(v) {
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(fmt).join('');
  if (v === null || v === undefined || v === false) return '';
  return esc(v);
}
/** Tagged template: interpolated values are escaped unless wrapped in raw()/html``. */
function html(strings, ...vals) {
  let out = strings[0];
  for (let i = 0; i < vals.length; i++) out += fmt(vals[i]) + strings[i + 1];
  return new Raw(out);
}
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const attr = (cond, name) => (cond ? raw(' ' + name) : '');

/** Replace an element's content, keeping focus, caret and scroll positions.
 * Unchanged markup leaves the DOM alone: re-parsing it anyway re-lays-out and
 * repaints the whole subtree, which the status poll would otherwise do every
 * few seconds to the nav, the topbar and live pages (costly without a GPU).
 * The firstChild check notices content that was replaced by other means. */
function mount(el, content) {
  if (!el) return;
  const markup = fmt(content);
  if (el.__markup === markup && el.firstChild && el.__first === el.firstChild) return;
  const active = document.activeElement;
  const keepId = active && el.contains(active) && active.id ? active.id : null;
  let caret = null;
  if (keepId && typeof active.selectionStart === 'number') {
    try { caret = [active.selectionStart, active.selectionEnd]; } catch (e) { caret = null; }
  }
  const scrolls = $$('[data-scroll]', el).map((n) => [n.dataset.scroll, n.scrollTop]);
  el.innerHTML = markup;
  el.__markup = markup;
  el.__first = el.firstChild;
  for (const [key, top] of scrolls) {
    const n = el.querySelector('[data-scroll="' + key + '"]');
    if (n) n.scrollTop = top;
  }
  if (keepId) {
    const n = document.getElementById(keepId);
    if (n) {
      n.focus({ preventScroll: true });
      if (caret) { try { n.setSelectionRange(caret[0], caret[1]); } catch (e) { /* not a text input */ } }
    }
  }
}

// ----------------------------------------------------------------- icons
const ICONS = {
  overview: '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
  discover: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 12l6-6"/>',
  select: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 12l3 3 5-6"/>',
  stage: '<path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2-6-2z"/><path d="M9 4v14M15 6v14"/>',
  waves: '<rect x="3" y="4" width="5" height="16" rx="1.5"/><rect x="10" y="4" width="5" height="11" rx="1.5"/><rect x="17" y="4" width="4" height="7" rx="1.5"/>',
  execute: '<circle cx="12" cy="12" r="9"/><path d="M10 8.5l5.5 3.5-5.5 3.5z"/>',
  queue: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.5" cy="6" r="1"/><circle cx="3.5" cy="12" r="1"/><circle cx="3.5" cy="18" r="1"/>',
  triage: '<path d="M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  batches: '<path d="M12 2l9 5-9 5-9-5 9-5z"/><path d="M3 12l9 5 9-5"/><path d="M3 17l9 5 9-5"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  left: '<path d="M15 6l-6 6 6 6"/>',
  right: '<path d="M9 6l6 6-6 6"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  refresh: '<path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  download: '<path d="M12 3v12M7 10l5 5 5-5"/><path d="M5 21h14"/>',
  rollback: '<path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 010 10h-3"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  skip: '<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/>',
  folder: '<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>',
  commit: '<circle cx="12" cy="12" r="3.5"/><path d="M3 12h5.5M15.5 12H21"/>',
  terminal: '<path d="M4 17l6-5-6-5M12 19h8"/>',
  up: '<path d="M6 15l6-6 6 6"/>',
  down: '<path d="M6 9l6 6 6-6"/>',
  key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="M10.7 12.3L21 2M16 7l3 3M18 5l2 2"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  book: '<path d="M4 5a2 2 0 012-2h13v16H6a2 2 0 00-2 2z"/><path d="M4 19V5M8 7h7"/>',
  help: '<circle cx="12" cy="12" r="9"/><path d="M9.6 9.2a2.5 2.5 0 014.9.6c0 1.7-2.5 2.1-2.5 3.7M12 16.8h.01"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  gear: '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M4.6 4.6l2.1 2.1M17.3 17.3l2.1 2.1M2.5 12h3M18.5 12h3M4.6 19.4l2.1-2.1M17.3 6.7l2.1-2.1"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0116 0"/>',
  bell: '<path d="M6 8a6 6 0 1112 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10 21a2 2 0 004 0"/>',
  tag: '<path d="M3 12V4a1 1 0 011-1h8l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  palette: '<path d="M12 3a9 9 0 100 18c1.1 0 1.7-.9 1.4-1.9-.3-.8.3-1.6 1.1-1.6H17a4 4 0 004-4c0-5-4-8.5-9-8.5z"/><circle cx="7.5" cy="11.5" r="1.2"/><circle cx="10.5" cy="7.5" r="1.2"/><circle cx="15.5" cy="8" r="1.2"/>',
};
function icon(name, cls) {
  // A design-language skin (skins.js) redraws some icons in its own style. A skin whose
  // icons are full-colour art (not `everywhere`) only draws them where the call site marks
  // an icon slot with the `art` class (navigation, palette, empty states, folder rows):
  // inside a tinted callout box or a button, colour art fights the tone.
  const skin = document.documentElement.dataset.skin;
  const set = skin && window.SKINS && window.SKINS[skin];
  if (set && set.icons[name] && (set.everywhere || /(^|\s)art(\s|$)/.test(cls || ''))) {
    return raw('<svg viewBox="0 0 24 24" ' + set.attrs + ' aria-hidden="true" class="skin-ic' +
      (cls ? ' ' + cls : '') + '">' + set.icons[name] + '</svg>');
  }
  return raw('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"' +
    (cls ? ' class="' + cls + '"' : '') + '>' + (ICONS[name] || '') + '</svg>');
}

// ---------------------------------------------------------------- storage
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
};

// ------------------------------------------------------------------ state
const S = {
  token: null, info: null, pulse: null, view: null, route: 'overview', params: {},
  lastActive: null, pulseSig: '',
};

// ---------------------------------------------------------------- formats
const n = (x) => (x === null || x === undefined ? '0' : Number(x).toLocaleString());
const pct = (a, b) => (b ? Math.round((100 * a) / b) + '%' : '—');
const plural = (k, one, many) => n(k) + ' ' + (k === 1 ? one : (many || one + 's'));
function parseTs(s) {
  if (!s) return null;
  const iso = /[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s.replace(' ', 'T');
  const d = new Date(iso);
  return isNaN(d) ? null : d;
}
function fmtTime(s, withDate) {
  const d = parseTs(s);
  if (!d) return '—';
  const today = new Date().toDateString() === d.toDateString();
  const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  if (today && !withDate) return t;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + t;
}
function fmtDur(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return '—';
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + 's';
  if (sec < 3600) return Math.floor(sec / 60) + 'm ' + String(sec % 60).padStart(2, '0') + 's';
  return Math.floor(sec / 3600) + 'h ' + String(Math.floor((sec % 3600) / 60)).padStart(2, '0') + 'm';
}
function fmtAgo(s) {
  const d = parseTs(s);
  if (!d) return 'never';
  const sec = (Date.now() - d.getTime()) / 1000;
  if (sec < 45) return 'just now';
  return fmtDur(sec).split(' ')[0] + ' ago';
}
function elapsed(job) {
  const a = parseTs(job.started_at);
  const b = job.finished_at ? parseTs(job.finished_at) : new Date();
  return a && b ? (b - a) / 1000 : null;
}

// ------------------------------------------------------------ vm states
const STATE_ORDER = ['pending', 'precheck_running', 'precheck_passed', 'precheck_failed', 'importing',
  'awaiting_commit', 'committed', 'failed', 'rolling_back', 'rolled_back', 'skipped'];
const BUSY = new Set(['precheck_running', 'importing', 'rolling_back']);
const FAILED = new Set(['failed', 'precheck_failed']);
const RETRYABLE = new Set(['failed', 'precheck_failed', 'rolled_back']);
const ROLLBACKABLE = new Set(['failed', 'awaiting_commit', 'importing']);
const label = (s) => (S.info && S.info.state_labels && S.info.state_labels[s]) || String(s || '').replace(/_/g, ' ');
const pill = (s) => html`<span class="pill ${BUSY.has(s) ? 'busy' : ''}" style="--c:var(--s-${s})" data-tip="state:${s}">${label(s)}</span>`;
const dot = (s) => html`<span class="dot" style="--c:var(--s-${s})" title="${label(s)}"></span>`;
const BATCH_TONE = { succeeded: 'ok', rolled_back: 'info', failed: 'bad', partial: 'warn', timedout: 'warn',
  applied: 'info', running: 'info', rolling_back: 'info', planned: '', deleted: '' };
const batchTag = (s) => html`<span class="tag ${BATCH_TONE[s] || ''}">${String(s || '').replace(/_/g, ' ')}</span>`;

function segbar(counts, total, large) {
  const sum = total || STATE_ORDER.reduce((a, s) => a + (counts[s] || 0), 0);
  if (!sum) return html`<div class="segbar ${large ? 'lg' : ''}"></div>`;
  return html`<div class="segbar ${large ? 'lg' : ''}">${STATE_ORDER.filter((s) => counts[s]).map((s) =>
    html`<span style="width:${(100 * counts[s] / sum).toFixed(3)}%;--c:var(--s-${s})" title="${label(s)}: ${n(counts[s])}"></span>`)}</div>`;
}
function legend(counts) {
  return html`<div class="legend">${STATE_ORDER.filter((s) => counts[s]).map((s) =>
    html`<span>${dot(s)} ${label(s)} <b>${n(counts[s])}</b></span>`)}</div>`;
}
function emptyState(ic, title, text, action) {
  return html`<div class="empty"><div class="ic">${icon(ic, 'art')}</div><h3>${title}</h3><p>${text}</p>${action || ''}</div>`;
}
function errorBox(e) {
  return html`<div class="callout bad"><div class="ic">${icon('triage')}</div><div><h3>Something went wrong</h3><p>${e.message || e}</p></div></div>`;
}
function pager(total, page, size) {
  const pages = Math.max(1, Math.ceil(total / size));
  if (total <= size) return html`<div class="pager"><span>${plural(total, 'row')}</span></div>`;
  const from = page * size + 1, to = Math.min(total, (page + 1) * size);
  return html`<div class="pager"><span>${n(from)}–${n(to)} of ${n(total)}</span><span class="grow"></span>
    <button class="btn xs" data-act="page" data-page="${page - 1}" ${attr(page <= 0, 'disabled')}>${icon('left')}</button>
    <span>page ${page + 1} / ${pages}</span>
    <button class="btn xs" data-act="page" data-page="${page + 1}" ${attr(page >= pages - 1, 'disabled')}>${icon('right')}</button></div>`;
}
function sortRows(rows, key, dir) {
  return rows.slice().sort((a, b) => {
    const x = a[key], y = b[key];
    if (typeof x === 'number' && typeof y === 'number') return (x - y) * dir;
    return String(x === null || x === undefined ? '' : x).localeCompare(String(y === null || y === undefined ? '' : y), undefined, { numeric: true, sensitivity: 'base' }) * dir;
  });
}
function th(v, key, text, cls) {
  const on = v.sort && v.sort.key === key;
  return html`<th class="sort ${cls || ''}" data-act="sort" data-key="${key}">${text}${on ? html`<span class="arr">${v.sort.dir > 0 ? '▲' : '▼'}</span>` : ''}</th>`;
}
function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// -------------------------------------------------------------------- api
class ApiErr extends Error { constructor(msg, status) { super(msg); this.status = status; } }
async function api(method, path, body) {
  const opts = { method, headers: { 'X-VCFA-Token': S.token || '' } };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  let res;
  try { res = await fetch(path, opts); } catch (e) {
    throw new ApiErr('The console is not reachable — is `vcfa-import serve` still running?', 0);
  }
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (res.status === 401) { tokenGate(); throw new ApiErr('The access token is missing or out of date.', 401); }
  if (!res.ok) throw new ApiErr((data && data.error) || res.status + ' ' + res.statusText, res.status);
  return data;
}
const GET = (p) => api('GET', p);
const POST = (p, b) => api('POST', p, b || {});
const PUT = (p, b) => api('PUT', p, b || {});
const exportUrl = (name) => '/api/export/' + name + '?t=' + encodeURIComponent(S.token || '');

function initToken() {
  const m = location.hash.match(/(?:^#|&)t=([^&]+)/);
  if (m) {
    S.token = decodeURIComponent(m[1]);
    store.set('vcfa-token', S.token);
    history.replaceState(null, '', location.pathname + '#/overview');
  } else {
    S.token = store.get('vcfa-token');
  }
}
function tokenGate() {
  S.gated = true;
  mount($('#view'), html`<div class="card" style="max-width:520px;margin:8vh auto">
    <div class="card-h"><h3>Access token needed</h3></div>
    <div class="card-b stack">
      <p class="muted" style="margin:0">Open the link printed by <code>vcfa-import serve</code>, or paste the token from it
      (the part after <code>#t=</code>). A new token is issued each time the console starts unless <code>--token</code> is given.</p>
      <input class="input mono" id="token-in" placeholder="token" autocomplete="off">
      <div><button class="btn primary" data-act="setToken">${icon('key')} Continue</button></div>
    </div></div>`);
  $('#topbar').innerHTML = '';
}

// ------------------------------------------------------------------ toast
function toast(title, msg, kind, ms) {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.innerHTML = fmt(html`<div class="grow"><b>${title}</b>${msg ? html`<span class="muted">${msg}</span>` : ''}</div><button class="x" aria-label="Dismiss">×</button>`);
  $('#toasts').appendChild(el);
  const close = () => el.remove();
  el.querySelector('.x').onclick = close;
  setTimeout(close, ms || (kind === 'bad' ? 9000 : 5000));
}
const fail = (e) => toast('Could not complete that', e.message || String(e), 'bad');

// ------------------------------------------------------------------ modal
function modal({ title, body, confirm, cancel, danger, typeWord, ic, wide }) {
  return new Promise((resolve) => {
    const root = $('#modal-root');
    root.innerHTML = fmt(html`<div class="overlay modal-ov"></div>
      <div class="modal" role="dialog" aria-modal="true" style="${wide ? 'width:min(820px,94vw)' : ''}">
        <div class="modal-h"><div class="ic ${danger ? 'danger' : ''}">${icon(ic || (danger ? 'triage' : 'info'))}</div>
          <div><h2>${title}</h2></div></div>
        <div class="modal-b">${body || ''}
          ${typeWord ? html`<label class="typeconfirm"><span class="small muted">Type <code>${typeWord}</code> to confirm</span>
            <input class="input mono" id="modal-type" autocomplete="off" spellcheck="false"></label>` : ''}</div>
        <div class="modal-f"><button class="btn" data-m="no">${cancel || 'Cancel'}</button>
          ${confirm === null ? '' : html`<button class="btn ${danger ? 'danger' : 'primary'}" data-m="yes" ${attr(!!typeWord, 'disabled')}>${confirm || 'Confirm'}</button>`}</div>
      </div>`);
    const done = (v) => { root.innerHTML = ''; document.removeEventListener('keydown', onKey); resolve(v); };
    const onKey = (e) => {
      if (e.key === 'Escape') done(false);
      if (e.key === 'Enter' && !typeWord && e.target.tagName !== 'TEXTAREA') done(collect());
    };
    const collect = () => {
      const vals = {};
      $$('[data-field]', root).forEach((f) => { vals[f.dataset.field] = f.type === 'checkbox' ? f.checked : f.value; });
      return Object.keys(vals).length ? vals : true;
    };
    document.addEventListener('keydown', onKey);
    root.querySelector('.overlay').onclick = () => done(false);
    root.querySelector('[data-m="no"]').onclick = () => done(false);
    const yes = root.querySelector('[data-m="yes"]');
    if (yes) yes.onclick = () => done(collect());
    const typed = $('#modal-type');
    if (typed) {
      typed.oninput = () => { yes.disabled = typed.value.trim() !== typeWord; };
      typed.onkeydown = (e) => { if (e.key === 'Enter' && !yes.disabled) done(collect()); };
      typed.focus();
    } else {
      const first = root.querySelector('[data-field]') || yes;
      if (first) first.focus();
    }
  });
}
async function askWave(count, current) {
  const waves = S.waves || [];
  const res = await modal({
    title: 'Move ' + plural(count, 'VM') + ' to another wave', ic: 'waves', confirm: 'Move',
    body: html`<p class="muted">Waves run in order. VMs already in a batch (in flight or committed) stay where they are.</p>
      <label class="field"><span>Target wave</span><input class="input" type="number" min="1" data-field="wave" value="${current || ''}" list="wave-list"></label>
      <datalist id="wave-list">${waves.map((w) => html`<option value="${w}">`)}</datalist>`,
  });
  if (!res) return null;
  const w = parseInt(res.wave, 10);
  if (!(w >= 1)) { toast('Pick a wave number of 1 or higher', '', 'warn'); return null; }
  return w;
}

// ----------------------------------------------------------------- drawer
const DR = { loader: null, act: {} };
async function openDrawer(loader) {
  DR.loader = loader;
  const root = $('#drawer-root');
  root.innerHTML = fmt(html`<div class="overlay" data-act="closeDrawer"></div>
    <aside class="drawer" role="dialog" aria-modal="true"><div class="drawer-b"><div class="boot">Loading…</div></div></aside>`);
  await reloadDrawer();
}
async function reloadDrawer() {
  if (!DR.loader) return;
  const root = $('#drawer-root');
  let d;
  try { d = await DR.loader(); } catch (e) { d = { head: html`<h2>Unavailable</h2>`, body: errorBox(e) }; }
  if (!DR.loader) return;
  DR.act = d.act || {};
  const drawer = root.querySelector('.drawer');
  if (!drawer) return;
  mount(drawer, html`<div class="drawer-h"><div class="grow">${d.head}</div>
      <button class="btn ghost icon" data-act="closeDrawer" aria-label="Close">${icon('x')}</button></div>
    <div class="drawer-b" data-scroll="drawer">${d.body}</div>
    ${d.foot ? html`<div class="drawer-f">${d.foot}</div>` : ''}`);
}
function closeDrawer() { DR.loader = null; DR.act = {}; $('#drawer-root').innerHTML = ''; }

// ------------------------------------------------------------------- dock
const D = { id: null, next: 0, lines: [], job: null, open: false, timer: null, filter: '' };
function lineClass(t) {
  if (/^\s*(!|PROBLEM|error|RUN HALTED|!!)|\[!!\]|refus/i.test(t) || /FAILED/.test(t)) return 'bad';
  if (/warning|^\s*~|\|\|/i.test(t)) return 'warn';
  if (/^\s*<-|\[ok\]|passed|confirmed|succeeded/i.test(t)) return 'ok';
  if (/^\s*->|resuming|\[wave /i.test(t)) return 'busy';
  if (/^===|^\s*\S.* finished/.test(t)) return 'head';
  if (/^\s*\$ /.test(t)) return 'dim';
  return '';
}
function logLines(lines, filter) {
  const f = (filter || '').toLowerCase();
  const out = [];
  for (const [, ts, text] of lines) {
    if (f && !text.toLowerCase().includes(f)) continue;
    let body = esc(text);
    if (f) body = body.replace(new RegExp(esc(f).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), (m) => '<mark>' + m + '</mark>');
    out.push('<div class="ln"><span class="ts">' + esc(ts) + '</span><span class="' + lineClass(text) + '">' + (body || ' ') + '</span></div>');
  }
  return raw(out.join(''));
}
function statusIcon(status) {
  if (status === 'running') return html`<span class="status-ic running"><span class="spinner"></span></span>`;
  const ic = { succeeded: 'check', warning: 'triage', failed: 'x', stopped: 'stop' }[status] || 'info';
  return html`<span class="status-ic ${status}">${icon(ic)}</span>`;
}
const STATUS_WORD = { running: 'running', succeeded: 'finished', warning: 'finished with warnings', failed: 'failed', stopped: 'stopped' };
function followJob(id, open) {
  if (D.id !== id) { D.id = id; D.next = 0; D.lines = []; D.job = null; D.filter = ''; }
  if (open !== undefined) D.open = open;
  renderDock(true);
  pollDock();
}
async function pollDock() {
  clearTimeout(D.timer);
  if (!D.id) return;
  const id = D.id;
  try {
    const snap = await GET('/api/jobs/' + encodeURIComponent(id) + '?since=' + D.next);
    if (id !== D.id) return;
    const fresh = snap.log || [];
    D.job = snap;
    D.next = snap.next;
    D.lines = D.lines.concat(fresh);
    if (window.FX && fresh.length && snap.status === 'running') FX.logActivity(fresh.length);
    if (D.lines.length > 8000) D.lines = D.lines.slice(-8000);
    renderDock(false, fresh);
    if (snap.status === 'running') D.timer = setTimeout(pollDock, 1000);
  } catch (e) { D.timer = setTimeout(pollDock, 3000); }
}
function renderDock(full, fresh) {
  const el = $('#dock');
  if (!D.id) { el.hidden = true; return; }
  el.hidden = false;
  const j = D.job;
  el.classList.toggle('live', !!(j && j.status === 'running'));
  const prog = j && j.progress;
  const bar = html`<div class="dock-bar" data-act="toggleDock">
      ${statusIcon(j ? j.status : 'running')}
      <span class="t">${j ? j.title : 'Starting…'}</span>
      <span class="meta">${j ? html`${STATUS_WORD[j.status] || j.status} · ${fmtDur(elapsed(j))}` : ''}</span>
      ${j && j.status === 'running' && window.FX ? FX.spark() : ''}
      ${prog && j.status === 'running' ? html`<div class="progress"><span style="width:${pct(prog.done, prog.total)}"></span></div><span class="meta">${n(prog.done)}/${n(prog.total)} ${prog.label}</span>`
        : (j && j.status === 'running' ? html`<div class="progress indet"><span></span></div>` : html`<span class="grow"></span>`)}
      ${j && j.stoppable ? html`<button class="btn sm danger-ghost" data-act="stopJob" data-id="${j.id}" ${attr(j.stop_requested, 'disabled')}>${icon('stop')} ${j.stop_requested ? 'Stopping…' : 'Stop'}</button>` : ''}
      <button class="btn ghost sm" data-act="toggleDock">${icon(D.open ? 'down' : 'up')} ${D.open ? 'Hide' : 'Log'}</button>
      ${j && j.status !== 'running' ? html`<button class="btn ghost icon sm" data-act="closeDock" aria-label="Close">${icon('x')}</button>` : ''}
    </div>`;
  const existingLog = el.querySelector('.log');
  if (full || !existingLog || !D.open) {
    el.innerHTML = fmt(html`${bar}${D.open ? html`<div class="dock-body">
      <div class="dock-tools"><div class="search" style="width:260px">${icon('search')}<input class="input sm" id="dock-filter" placeholder="Filter log" value="${D.filter}" data-input="dockFilter"></div>
      <span class="small muted grow">${j ? html`${n(j.lines)} lines · started ${fmtTime(j.started_at)}` : ''}</span>
      <a class="btn sm ghost" href="#/activity?tab=jobs&job=${D.id}">Open in Activity</a></div>
      <div class="log" id="dock-log">${logLines(D.lines, D.filter)}</div></div>` : ''}`);
    const log = $('#dock-log');
    if (log) log.scrollTop = log.scrollHeight;
    return;
  }
  // incremental: redraw the bar, append new lines, stick to the bottom if already there
  const barEl = el.querySelector('.dock-bar');
  const barHtml = fmt(bar);
  if (barEl.__markup !== barHtml) {
    const tmp = document.createElement('div');
    tmp.innerHTML = barHtml;
    tmp.firstElementChild.__markup = barHtml;
    barEl.replaceWith(tmp.firstElementChild);
  }
  if (fresh && fresh.length) {
    const atBottom = existingLog.scrollHeight - existingLog.scrollTop - existingLog.clientHeight < 40;
    existingLog.insertAdjacentHTML('beforeend', fmt(logLines(fresh, D.filter)));
    if (atBottom) existingLog.scrollTop = existingLog.scrollHeight;
  }
}

// ----------------------------------------------------------- shared actions
async function startJob(kind, body, quiet) {
  try {
    const r = await POST('/api/run/' + kind, body || {});
    if (r.approval) {
      // The two-person rule: nothing started; someone else decides.
      toast('Approval #' + r.approval.id + ' requested', r.approval.summary + ' — someone other than you approves it under Change control, and it starts then.', 'warn', 9000);
      if (S.pulse) S.pulse.approvals_pending = (S.pulse.approvals_pending || 0) + 1;
      renderNav();
      return null;
    }
    followJob(r.job.id, true);
    S.lastActive = r.job.id;
    if (!quiet) toast('Started: ' + r.job.title, 'Follow it in the log panel below.', 'info', 3500);
    return r.job;
  } catch (e) {
    fail(e);
    return null;
  }
}
async function doRetry(vms) {
  const morefs = vms.map((v) => v.moref);
  const retryable = vms.filter((v) => RETRYABLE.has(v.state)).length;
  if (!retryable) { toast('Nothing to retry', 'Only failed, precheck-failed and rolled-back VMs can be requeued.', 'warn'); return 0; }
  const ok = await modal({ title: 'Retry ' + plural(retryable, 'VM') + '?', ic: 'refresh', confirm: 'Requeue',
    body: html`<p>They go back to <b>pending</b> and are picked up by the next precheck. Nothing is sent to the cluster now.</p>` });
  if (!ok) return 0;
  try {
    let r = await POST('/api/vms/retry', { morefs });
    const over = retryable - r.requeued;
    if (over > 0) {
      const force = await modal({ title: plural(over, 'VM') + ' reached max_retries', ic: 'triage', confirm: 'Retry anyway',
        body: html`<p>They have already been attempted more than <b>max_retries = ${S.info.settings.max_retries}</b> times. Retrying again is usually only worth it once the cause is fixed.</p>` });
      if (force) {
        const r2 = await POST('/api/vms/retry', { morefs, force: true });
        r = { requeued: r.requeued + r2.requeued };
      }
    }
    toast('Requeued ' + plural(r.requeued, 'VM'), 'Precheck them again from Execute.', 'ok');
    return r.requeued;
  } catch (e) { fail(e); return 0; }
}
async function doSkip(morefs, unskip) {
  if (!unskip) {
    const ok = await modal({ title: 'Skip ' + plural(morefs.length, 'VM') + '?', ic: 'skip', confirm: 'Skip',
      body: html`<p>Skipped VMs are left out of every precheck and import until you unskip them. VMs already in a batch cannot be skipped.</p>` });
    if (!ok) return;
  }
  try {
    const r = await POST('/api/vms/skip', { morefs, unskip: !!unskip });
    toast((unskip ? 'Unskipped ' : 'Skipped ') + plural(r.changed, 'VM'), r.notes.slice(0, 3).join('; '), r.notes.length ? 'warn' : 'ok');
  } catch (e) { fail(e); }
}
async function doMoveWave(morefs, current) {
  const wave = await askWave(morefs.length, current);
  if (!wave) return false;
  try {
    const r = await POST('/api/vms/wave', { morefs, wave });
    toast('Moved ' + plural(r.moved, 'VM') + ' to wave ' + wave,
      (r.pulled_with_app ? plural(r.pulled_with_app, 'app member') + ' came along (apps move together). ' : '') +
      (r.refused.length ? r.refused.length + ' refused: ' + r.refused.slice(0, 2).join('; ') : ''), r.refused.length ? 'warn' : 'ok');
    return true;
  } catch (e) { fail(e); return false; }
}
function notesList(notes) {
  return notes && notes.length ? html`<div class="note warn mt-s"><ul class="plain">${notes.map((x) => html`<li>${x}</li>`)}</ul></div>` : '';
}
async function doRollback(sel) {
  let p;
  try { p = await POST('/api/rollback/preview', sel); } catch (e) { fail(e); return; }
  if (!p.batches.length) {
    await modal({ title: 'Nothing to roll back', confirm: null, cancel: 'Close', body: html`<p class="muted">No import batch in the selection has a VM that can be handed back.</p>${notesList(p.notes)}` });
    return;
  }
  const res = await modal({
    title: 'Hand ' + plural(p.revert, 'VM') + ' back to vCenter?', danger: true, ic: 'rollback', confirm: 'Roll back', typeWord: 'ROLLBACK', wide: true,
    body: html`<p>Sets <code>controlAction.rollbackAction: Immediate</code> on ${plural(p.batches.length, 'batch', 'batches')} and waits for the operator to revert ownership.
      Rollback is per batch: every un-committed VM in these batches is reverted. <b>Committed VMs are never touched</b> — a committed import cannot be reversed.</p>
      <div class="card mt-s"><div class="table-wrap" style="max-height:260px"><table class="t compact">
        <thead><tr><th>Batch</th><th>Namespace</th><th>Wave</th><th>State</th><th class="right">To revert</th><th class="right">Committed (kept)</th></tr></thead>
        <tbody>${p.batches.map((b) => html`<tr><td class="mono">${b.name}</td><td>${b.namespace}</td><td>${b.wave}</td><td>${batchTag(b.state)}</td><td class="right num">${b.revert}</td><td class="right num">${b.committed}</td></tr>`)}</tbody></table></div></div>
      ${notesList(p.notes)}
      <label class="check mt"><input type="checkbox" data-field="delete"> Delete each batch once the operator confirms its rollback</label>`,
  });
  if (!res) return;
  await startJob('rollback', Object.assign({}, sel, { batches: p.batches.map((b) => b.name), morefs: null, failed: false, folders: null, delete: !!res.delete, confirm: true }));
}
async function doAbandon(sel) {
  let p;
  try { p = await POST('/api/abandon/preview', sel); } catch (e) { fail(e); return; }
  if (!p.batches.length) {
    await modal({ title: 'Nothing to abandon', confirm: null, cancel: 'Close', body: html`<p class="muted">No batch in the selection can be discarded safely.</p>${notesList(p.notes)}` });
    return;
  }
  const vms = p.batches.reduce((a, b) => a + b.vms, 0);
  const ok = await modal({
    title: 'Abandon ' + plural(p.batches.length, 'batch', 'batches') + '?', danger: true, ic: 'trash', confirm: 'Abandon', wide: true,
    body: html`<p>The batch objects are deleted from the cluster and their ${plural(vms, 'VM')} return to <b>pending</b>. No rollback is involved:
      precheck batches migrate nothing, and import batches are only accepted here once none of their VMs is still in flight.</p>
      <div class="card mt-s"><div class="table-wrap" style="max-height:240px"><table class="t compact">
      <thead><tr><th>Batch</th><th>Namespace</th><th>Stage</th><th>State</th><th class="right">VMs</th></tr></thead>
      <tbody>${p.batches.map((b) => html`<tr><td class="mono">${b.name}</td><td>${b.namespace}</td><td>${b.stage}</td><td>${batchTag(b.state)}</td><td class="right num">${b.vms}</td></tr>`)}</tbody></table></div></div>
      ${notesList(p.notes)}`,
  });
  if (!ok) return;
  await startJob('abandon', { batches: p.batches.map((b) => b.name), confirm: true });
}
async function doCommit(sel, count) {
  const ok = await modal({
    title: 'Commit ' + plural(count, 'held import') + '?', danger: true, ic: 'commit', confirm: 'Commit', typeWord: 'COMMIT',
    body: html`<p>Releases the <code>commitAction: Wait</code> gate on their batches. <b>Committing is irreversible</b>: committed VMs cannot be handed back to vCenter.</p>`,
  });
  if (!ok) return;
  await startJob('commit', Object.assign({}, sel, { confirm: true }));
}
async function doCleanup(batches) {
  const ok = await modal({
    title: 'Delete ' + plural(batches.length, 'rolled-back batch', 'rolled-back batches') + '?', ic: 'trash', confirm: 'Delete', danger: true,
    body: html`<p>Only batches whose rollback the operator has confirmed are deleted. Their VMs stay rolled back and can be retried.</p>
      <ul class="plain small mono">${batches.slice(0, 12).map((b) => html`<li>${b.namespace}/${b.name}</li>`)}</ul>`,
  });
  if (!ok) return;
  await startJob('cleanup', { batches: batches.map((b) => b.name), confirm: true });
}

// ------------------------------------------------------------ VM & batch drawers
function vmDrawer(moref) {
  openDrawer(async () => {
    const d = await GET('/api/vms/' + encodeURIComponent(moref));
    const vm = d.vm, s = d.summary;
    const retry = RETRYABLE.has(vm.state), canRb = ROLLBACKABLE.has(vm.state) && vm.batch_name;
    const hasBatch = vm.batch_name || vm.precheck_batch;
    const facts = (rows) => html`<dl class="facts">${rows.filter((r) => r[1] !== '' && r[1] !== null && r[1] !== undefined).map((r) => html`<dt>${r[0]}</dt><dd>${r[1]}</dd>`)}</dl>`;
    const blocked = (vm.readiness || []).filter((f) => f.level !== 'info');
    return {
      head: html`<div class="row wrap"><h2>${vm.vm_name}</h2>${pill(vm.state)}${appTag(vm.app)}${verifyTag(vm.verify_state, vm.verified_at)}</div>
        <div class="small muted mono">${vm.moref} · wave ${vm.wave} · ${vm.namespace}</div>`,
      body: html`
        ${d.issue ? html`<div class="callout ${vm.state === 'failed' ? 'bad' : 'warn'}"><div class="ic">${icon('triage')}</div><div><h3>${d.issue.title}</h3><p>${d.issue.advice}</p></div></div>` : ''}
        ${vm.message && !/^(true|false)$/i.test(vm.message) ? html`<div class="section-title">Last message from the operator</div><div class="msgbox">${vm.message}</div>` : ''}
        <div class="grid two mt">
          <div><div class="section-title">Source (vCenter)</div>${facts([
            ['vCenter', vm.src_vcenter], ['Datacenter', vm.src_datacenter], ['Cluster', vm.src_cluster],
            ['Folder', vm.src_folder || '/'], ['Host', vm.src_host], ['Power', (vm.src_power || '').replace('POWERED_', '').toLowerCase()],
            ['Sizing', vm.src_cpu ? vm.src_cpu + ' vCPU · ' + n(vm.src_memory_mb) + ' MiB' : ''], ['VM Tools', vm.src_tools], ['Networks', vm.src_networks],
            ['IP', vm.src_ip], ['Application', vm.app], ['Tags', (vm.tags || []).length ? tagChips(vm.tags, 6) : '']])}</div>
          <div><div class="section-title">Target (VCF Automation)</div>${facts([
            ['Namespace', vm.namespace], ['Wave', vm.wave], ['Batch group', vm.grp], ['Mode', vm.mode],
            ['Interfaces', vm.nics.map((x) => x.device_key + ' → ' + (x.subnet || '(no subnet)')).join(', ') || '(none)'],
            ['Resource', vm.target_resource || '(not reported yet)'], ['Attempts', vm.attempts], ['Last phase', vm.last_phase],
            ['Precheck batch', vm.precheck_batch], ['Import batch', vm.batch_name], ['Operation', vm.operation_name]])}</div>
        </div>
        ${vm.state === 'committed' ? html`<div class="section-title">Post-import verification ${vm.verified_at ? html`<span class="tiny faint">${fmtTime(vm.verified_at, true)}</span>` : ''}</div>
          ${(vm.verify || []).length ? verifyChecks(vm.verify) : html`<div class="small muted">Not verified yet. Verification checks power, VMware Tools, that the IP was kept, ping and any TCP ports set in Settings.</div>`}` : ''}
        ${['pending', 'precheck_failed', 'precheck_running'].includes(vm.state) || blocked.length ? html`<div class="section-title">Readiness (from vCenter)</div>${findingsList(vm.readiness)}` : ''}
        ${d.batches.length ? html`<div class="section-title">Batches</div><div class="stack" style="gap:6px">${d.batches.map((b) => html`
          <div class="covrow" style="cursor:pointer" data-act="batch" data-ns="${b.namespace}" data-name="${b.name}">
            <span class="p mono">${b.name}</span><span class="tag">${b.stage}</span>${batchTag(b.state)}<span class="arrow">${icon('chevron')}</span></div>`)}</div>` : ''}
        <div class="section-title">History</div>
        <div class="timeline">${d.transitions.map((t) => html`<div class="tl" style="--c:var(--s-${t.to_state})">
          <div class="h">${t.from_state ? label(t.from_state) + ' → ' : ''}${label(t.to_state)}</div>
          <div class="m">${fmtTime(t.ts, true)}${t.stage ? ' · ' + t.stage : ''}${t.batch ? ' · ' + t.batch : ''}${t.held_secs !== null && t.held_secs !== undefined ? ' · held ' + fmtDur(t.held_secs) : ''}${t.attempt ? ' · attempt ' + t.attempt : ''}</div>
          ${t.message || t.phase ? html`<div class="x">${t.message || t.phase}</div>` : ''}</div>`)}</div>
        ${d.events.length ? html`<div class="section-title">Events</div><table class="t compact"><tbody>${d.events.map((e) => html`<tr>
          <td class="nowrap small muted">${fmtTime(e.ts, true)}</td><td><span class="tag ${e.level === 'error' ? 'bad' : e.level === 'warn' ? 'warn' : ''}">${e.level}</span></td><td class="small">${e.message}</td></tr>`)}</tbody></table>` : ''}`,
      foot: html`
        <button class="btn" data-act="d_retry" ${attr(!retry, 'disabled')}>${icon('refresh')} Retry</button>
        ${vm.state === 'skipped' ? html`<button class="btn" data-act="d_unskip">Unskip</button>` : html`<button class="btn" data-act="d_skip" ${attr(s.locked, 'disabled')}>${icon('skip')} Skip</button>`}
        <button class="btn" data-act="d_wave" ${attr(s.locked, 'disabled')}>${icon('waves')} Move wave</button>
        ${vm.state === 'committed' ? html`<button class="btn" data-act="d_verify">${icon('check')} Verify</button>` : ''}
        <span class="grow"></span>
        <button class="btn danger-ghost" data-act="d_abandon" ${attr(!hasBatch || vm.state === 'committed', 'disabled')}>${icon('trash')} Abandon batch</button>
        <button class="btn danger-ghost" data-act="d_rollback" ${attr(!canRb, 'disabled')}>${icon('rollback')} Roll back</button>`,
      act: {
        d_retry: async () => { if (await doRetry([s])) { reloadDrawer(); refreshView(); } },
        d_skip: async () => { await doSkip([vm.moref]); reloadDrawer(); refreshView(); },
        d_unskip: async () => { await doSkip([vm.moref], true); reloadDrawer(); refreshView(); },
        d_wave: async () => { if (await doMoveWave([vm.moref], vm.wave)) { reloadDrawer(); refreshView(); } },
        d_rollback: () => doRollback({ morefs: [vm.moref] }),
        d_verify: () => startJob('verify', { morefs: [vm.moref] }),
        d_abandon: () => doAbandon({ morefs: [vm.moref] }),
      },
    };
  });
}
function batchDrawer(ns, name) {
  openDrawer(async () => {
    const d = await GET('/api/batches/' + encodeURIComponent(ns) + '/' + encodeURIComponent(name));
    const b = d.batch;
    const counts = {};
    d.members.forEach((m) => { counts[m.state] = (counts[m.state] || 0) + 1; });
    const canRb = b.stage === 'import' && d.members.some((m) => ROLLBACKABLE.has(m.state)) && b.state !== 'deleted';
    return {
      head: html`<div class="row wrap"><h2 class="mono" style="font-size:16px">${b.name}</h2>${batchTag(b.state)}<span class="tag">${b.stage}</span></div>
        <div class="small muted">${b.namespace} · wave ${b.wave} · ${plural(b.vm_count, 'VM')} · run ${b.run_id}</div>`,
      body: html`
        ${b.message ? html`<div class="msgbox">${b.message}</div>` : ''}
        <dl class="facts mt"><dt>Created</dt><dd>${fmtTime(b.created_at, true)}</dd><dt>Applied</dt><dd>${fmtTime(b.applied_at, true)}</dd>
          <dt>Finished</dt><dd>${fmtTime(b.finished_at, true)}</dd><dt>Manifest</dt><dd class="mono small">${b.manifest_path || '—'}</dd></dl>
        <div class="section-title">Members</div>${segbar(counts, 0)}${legend(counts)}
        <div class="card mt-s"><table class="t compact"><thead><tr><th>VM</th><th>State</th><th>Message</th></tr></thead><tbody>
          ${d.members.map((m) => html`<tr class="click" data-act="vm" data-moref="${m.moref}"><td><span class="vm-name">${m.vm_name}<small>${m.moref}</small></span></td><td>${pill(m.state)}</td><td class="msg"><div>${m.message || ''}</div></td></tr>`)}</tbody></table></div>
        ${d.manifest ? html`<div class="section-title">Manifest</div><pre class="msgbox" style="max-height:340px">${d.manifest}</pre>` : ''}
        ${d.events.length ? html`<div class="section-title">Events</div><table class="t compact"><tbody>${d.events.map((e) => html`<tr>
          <td class="nowrap small muted">${fmtTime(e.ts, true)}</td><td><span class="tag ${e.level === 'error' ? 'bad' : e.level === 'warn' ? 'warn' : ''}">${e.level}</span></td><td class="small">${e.message}</td></tr>`)}</tbody></table>` : ''}`,
      foot: html`
        ${b.state === 'rolled_back' ? html`<button class="btn" data-act="b_delete">${icon('trash')} Delete from cluster</button>` : ''}
        <span class="grow"></span>
        <button class="btn danger-ghost" data-act="b_abandon" ${attr(b.state === 'deleted', 'disabled')}>${icon('trash')} Abandon</button>
        <button class="btn danger-ghost" data-act="b_rollback" ${attr(!canRb, 'disabled')}>${icon('rollback')} Roll back</button>`,
      act: {
        b_rollback: () => doRollback({ batches: [b.name] }),
        b_abandon: () => doAbandon({ batches: [b.name] }),
        b_delete: () => doCleanup([b]),
      },
    };
  });
}

// ---------------------------------------------------------------- shell
const NAV = [
  { id: 'overview', label: 'Overview', icon: 'overview' },
  { group: 'Plan' },
  { id: 'discover', label: 'Discover', icon: 'discover', step: 1 },
  { id: 'select', label: 'Select VMs', icon: 'select', step: 2 },
  { id: 'stage', label: 'Map & Stage', icon: 'stage', step: 3 },
  { id: 'waves', label: 'Waves', icon: 'waves', step: 4 },
  { group: 'Run' },
  { id: 'execute', label: 'Execute', icon: 'execute', step: 5 },
  { id: 'queue', label: 'Import queue', icon: 'queue' },
  { id: 'batches', label: 'Batches', icon: 'batches' },
  { group: 'Investigate' },
  { id: 'triage', label: 'Triage', icon: 'triage' },
  { id: 'activity', label: 'Activity & logs', icon: 'activity' },
  { group: 'Govern' },
  { id: 'schedule', label: 'Change control', icon: 'calendar' },
  { id: 'settings', label: 'Settings', icon: 'gear' },
  { group: 'Learn' },
  { id: 'help', label: 'Help & guides', icon: 'book' },
];
function navBadge(id, p) {
  if (!p) return '';
  const b = (text, cls) => html`<span class="nav-badge ${cls || ''}">${text}</span>`;
  switch (id) {
    case 'select': return p.discovered.selected ? b(n(p.discovered.selected)) : '';
    case 'queue': return p.total ? b(n(p.total)) : '';
    case 'execute': return p.in_flight ? b(n(p.in_flight), 'busy') : (p.awaiting_commit ? b(n(p.awaiting_commit), 'warn') : '');
    case 'batches': return p.live_batches ? b(n(p.live_batches), 'busy') : '';
    case 'triage': return p.failed ? b(n(p.failed), 'bad') : '';
    case 'activity': return p.job ? b('1', 'busy') : '';
    case 'schedule': return p.approvals_pending ? b(n(p.approvals_pending), 'warn') : (p.next_window && p.next_window.state === 'running' ? b('live', 'busy') : '');
    default: return '';
  }
}
function stepDone(id, p) {
  if (!p) return false;
  return { discover: p.discovered.total > 0, select: p.discovered.selected > 0, stage: p.total > 0,
    waves: p.total > 0 && (p.counts.pending || 0) < p.total }[id] || false;
}
function renderNav() {
  const p = S.pulse;
  mount($('#nav'), NAV.map((it) => it.group ? html`<div class="nav-group">${it.group}</div>` :
    html`<a class="nav-item ${S.route === it.id ? 'active' : ''}" href="#/${it.id}">
      ${it.step ? html`<span class="nav-step ${stepDone(it.id, p) && S.route !== it.id ? 'done' : ''}">${stepDone(it.id, p) && S.route !== it.id ? '✓' : it.step}</span>` : icon(it.icon, 'art')}
      <span>${it.label}</span>${navBadge(it.id, p)}</a>`));
  if (S.info) {
    mount($('#side-foot'), html`<div class="row"><span>version</span><b>${S.info.version}</b></div>
      <div class="row"><span>context</span><b title="${S.info.context || ''}">${S.info.context || 'current'}</b></div>
      <div class="row"><span>workdir</span><span title="${S.info.workdir}">${S.info.workdir}</span></div>`);
  }
  if (window.FX) FX.navGlider();
}
function renderTopbar() {
  if (S.gated) return;
  const v = S.view, p = S.pulse, i = S.info;
  mount($('#topbar'), html`<div><h1>${v ? v.title : ''}</h1>${v && v.sub ? html`<div class="sub">${v.sub}</div>` : ''}</div>
    <span class="spacer"></span>
    ${p && p.job ? html`<button class="jobchip" data-act="showJob" data-id="${p.job.id}"><span class="spinner"></span><span>${p.job.title}</span></button>` : ''}
    ${i ? html`<span class="ctxchip" data-tip="context" tabindex="0">ctx <b>${i.context || 'current'}</b></span>
      <span class="ctxchip" data-tip="commit_action" tabindex="0">commit <b class="${i.settings.commit_action === 'Auto' ? 'warn-text' : ''}">${i.settings.commit_action}</b></span>` : ''}
    <button class="btn ghost sm" data-act="palette" title="Command palette (Ctrl+K)">${icon('search')}<span class="kbd">ctrl k</span></button>
    <button class="btn ghost icon" data-act="help" title="Help & guides: the tour, the lab guide, the glossary" aria-label="Help">${icon('help')}</button>
    <button class="btn ghost icon" data-act="theme" title="Theme Studio">${icon('palette')}</button>
    ${userChip()}`);
  const who = typeof me === 'function' ? me() : null;
  if (who) document.body.dataset.role = who.role;
}
function userChip() {
  if (typeof me !== 'function') return '';
  const u = me();
  return html`<span class="userchip role-${u.role}" title="${u.role === 'viewer' ? 'Read-only link: you can look at everything and change nothing' : 'Signed in as ' + u.name + ' (' + u.role + '); your actions are recorded under this name'}">
    ${icon(u.role === 'viewer' ? 'eye' : 'user')}<b>${u.name}</b><small>${u.role}</small></span>`;
}
function applyTheme() {
  if (window.FX) FX.applyTheme();
}

// ---------------------------------------------------------------- router
const VIEWS = {};
function parseHash() {
  const h = location.hash.replace(/^#\/?/, '');
  const [id, qs] = h.split('?');
  return { id: VIEWS[id] ? id : 'overview', params: Object.fromEntries(new URLSearchParams(qs || '')) };
}
function go(id, qs) { location.hash = '#/' + id + (qs ? '?' + qs : ''); }
async function route() {
  if (S.gated) return;
  const { id, params } = parseHash();
  const v = VIEWS[id];
  if (S.view && S.view.leave) S.view.leave(S.view);
  S.view = v; S.route = id; S.params = params;
  closeDrawer();
  renderNav();
  renderTopbar();
  const el = $('#view');
  el.innerHTML = '<div class="boot">Loading…</div>';
  window.scrollTo(0, 0);
  if (window.FX) FX.beforeRoute();
  try { await v.enter(el, params); } catch (e) { if (S.view === v) mount(el, errorBox(e)); }
  if (window.FX && S.view === v) FX.afterRoute(el);
}
async function refreshView() {
  const v = S.view;
  if (!v || !v.refresh) return;
  try { await v.refresh(); } catch (e) { /* a failed background refresh keeps the last good render */ }
}

/** Build a view from {title, sub, live, init(v, params), load(v), paint(v), after(v), act, dnd, onPulse}. */
function view(def) {
  const v = Object.assign({ act: {} }, def);
  v.enter = async (el, params) => {
    v.el = el; v.params = params || {};
    if (def.init) def.init(v, v.params);
    v.loaded = false;
    await v.refresh();
  };
  v.refresh = async () => {
    if (v.loading) return;
    v.loading = true;
    try { await def.load(v); v.loaded = true; v.lastLoad = Date.now(); } finally { v.loading = false; }
    v.render();
  };
  v.render = () => {
    if (!v.el || S.view !== v || !v.loaded) return;
    mount(v.el, def.paint(v));
    if (def.after) def.after(v);
    if (window.FX) FX.afterRender(v.el, v);
  };
  return v;
}

// --------------------------------------------------------------- events
const GLOBAL_ACT = {
  go: (t) => go(t.dataset.to, t.dataset.q || ''),
  vm: (t) => vmDrawer(t.dataset.moref),
  batch: (t) => batchDrawer(t.dataset.ns, t.dataset.name),
  closeDrawer: () => closeDrawer(),
  toggleDock: (t, e) => { if (e.target.closest('button') && e.target.closest('button') !== t) return; D.open = !D.open; renderDock(true); },
  closeDock: () => { D.id = null; D.open = false; clearTimeout(D.timer); renderDock(true); },
  showJob: (t) => followJob(t.dataset.id, true),
  stopJob: async (t) => {
    const ok = await modal({ title: 'Stop this job?', ic: 'stop', confirm: 'Stop',
      body: html`<p>No new batches are applied. Batches already on the cluster are polled to completion, so the job may take a little while to wind down. Nothing is rolled back.</p>` });
    if (!ok) return;
    try { await POST('/api/jobs/' + encodeURIComponent(t.dataset.id) + '/stop'); toast('Stop requested', 'In-flight batches will finish first.', 'warn'); pollDock(); } catch (e) { fail(e); }
  },
  theme: () => FX.openStudio(),
  palette: () => FX.openPalette(),
  setToken: () => {
    const v = ($('#token-in').value || '').trim().replace(/^.*#t=/, '');
    if (!v) return;
    S.token = v; store.set('vcfa-token', v); S.gated = false; boot();
  },
};
function findAct(name) {
  if (DR.loader && DR.act[name]) return DR.act[name];
  if (S.view && S.view.act && S.view.act[name]) return S.view.act[name];
  return GLOBAL_ACT[name];
}
function wireEvents() {
  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-act]');
    if (!t || t.disabled) return;
    const fn = findAct(t.dataset.act);
    if (!fn) return;
    if (t.tagName === 'A' && !t.getAttribute('href')) e.preventDefault();
    if (t.tagName === 'BUTTON') e.preventDefault();
    Promise.resolve(fn(t, e, S.view)).catch(fail);
  });
  for (const type of ['input', 'change']) {
    document.addEventListener(type, (e) => {
      const t = e.target.closest('[data-' + type + ']');
      if (!t) return;
      const name = t.dataset[type];
      if (name === 'dockFilter') { D.filter = t.value; const log = $('#dock-log'); if (log) mount(log, logLines(D.lines, D.filter)); return; }
      const fn = findAct(name);
      if (fn) Promise.resolve(fn(t, e, S.view)).catch(fail);
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && DR.loader && !$('#modal-root').innerHTML) closeDrawer();
    const t = e.target.closest && e.target.closest('[data-enter]');
    if (t && e.key === 'Enter') { const fn = findAct(t.dataset.enter); if (fn) { e.preventDefault(); Promise.resolve(fn(t, e, S.view)).catch(fail); } }
  });
  for (const type of ['dragstart', 'dragover', 'dragleave', 'drop', 'dragend']) {
    document.addEventListener(type, (e) => {
      const v = S.view;
      if (v && v.dnd && v.dnd[type]) v.dnd[type](e, v);
    });
  }
  document.addEventListener('toggle', (e) => {
    const t = e.target;
    if (t.tagName === 'DETAILS' && t.dataset.toggle) { const fn = findAct(t.dataset.toggle); if (fn) fn(t, e, S.view); }
  }, true);
  window.addEventListener('hashchange', route);
}

// ---------------------------------------------------------------- pulse
async function pulse() {
  let wait = 3000;
  try {
    const p = await GET('/api/pulse');
    const prevActive = S.lastActive;
    S.pulse = p;
    if (p.job) {
      wait = 1500;
      if (S.lastActive !== p.job.id) { S.lastActive = p.job.id; if (D.id !== p.job.id) followJob(p.job.id); }
    } else if (prevActive) {
      S.lastActive = null;
      jobFinished(prevActive);
    }
    renderNav();
    renderTopbar();
    if (window.FX) FX.onPulse(p);
    const sig = JSON.stringify([p.counts, p.discovered, p.live_batches]);
    const v = S.view;
    if (v && v.onPulse) v.onPulse(v, p);
    if (v && v.live && v.loaded && (sig !== S.pulseSig || p.job) && Date.now() - (v.lastLoad || 0) > 2500) refreshView();
    S.pulseSig = sig;
    if (S.offline) {
      S.offline = false;
      $('#offline').hidden = true;
      toast('Reconnected', 'The console is reachable again.', 'ok', 3500);
      refreshView();
    }
  } catch (e) {
    if (e.status === 401) return;   // gated; boot() restarts the pulse
    wait = 5000;
    if (e.status === 0 && !S.offline) {
      // Say so: silently frozen counters in the middle of an import mislead.
      S.offline = true;
      $('#offline').hidden = false;
    }
  }
  setTimeout(pulse, wait);
}
async function jobFinished(id) {
  let j;
  try { j = await GET('/api/jobs/' + encodeURIComponent(id) + '?since=999999999'); } catch (e) { return; }
  const kind = { succeeded: 'ok', warning: 'warn', failed: 'bad', stopped: 'warn' }[j.status] || 'info';
  toast(j.title + ' ' + (STATUS_WORD[j.status] || j.status), j.error || jobSummary(j), kind, 8000);
  if (D.id === id) pollDock();
  const v = S.view;
  if (v && v.onJobDone) v.onJobDone(v, j); else refreshView();
}
function jobSummary(j) {
  const r = j.result || {};
  switch (j.kind) {
    case 'discover': return plural(r.discovered || 0, 'VM') + ' discovered (' + n(r.added) + ' new)';
    case 'preflight': return r.ok ? 'All checks passed' : plural((r.problems || []).length, 'problem') + ' found';
    case 'execute': return 'applied ' + n(r.applied) + ' · succeeded ' + n(r.succeeded) + ' · failed ' + n(r.failed) + (r.awaiting_commit ? ' · awaiting commit ' + n(r.awaiting_commit) : '');
    case 'rollback': return n(r.reverted) + ' VM(s) reverted to vCenter';
    case 'abandon': return (r.deleted || []).length + ' batch(es) deleted, ' + n(r.requeued) + ' VM(s) requeued';
    default: return 'took ' + fmtDur(r.seconds);
  }
}

// ------------------------------------------------------------------ boot
async function boot() {
  if (window.FX && !S.fxReady) { S.fxReady = true; FX.init(); }
  if (!S.token) { tokenGate(); return; }
  try {
    S.info = await GET('/api/info');
    S.pulse = await GET('/api/pulse');
  } catch (e) {
    if (e.status !== 401) mount($('#view'), errorBox(e));
    return;
  }
  S.gated = false;
  const recent = S.pulse.job;
  if (recent) { S.lastActive = recent.id; followJob(recent.id, false); }
  renderNav();
  await route();
  if (window.HELP) HELP.maybeWelcome();
  if (!S.pulsing) { S.pulsing = true; setTimeout(pulse, 1500); }
}
document.addEventListener('DOMContentLoaded', () => { wireEvents(); initToken(); boot(); });
