'use strict';
/* VCFA Import Console — the pages. Each is built with view() from core.js:
 * load() fetches, paint() returns markup, act{} handles data-act clicks. */

const PAGE = 100;
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== '' && v !== null && v !== undefined)).toString();
const countBy = (rows, key) => rows.reduce((m, r) => { m[r[key]] = (m[r[key]] || 0) + 1; return m; }, {});
const uniq = (xs) => Array.from(new Set(xs.filter((x) => x !== '' && x !== null && x !== undefined)));
const inFolder = (path, folder) => folder === '' || path === folder || (path || '').startsWith(folder + '/');

function kpi(lbl, value, sub, to, color) {
  return html`<div class="kpi ${to ? 'link' : ''}" style="--c:${color || 'transparent'}" ${to ? raw(`data-act="go" data-to="${esc(to.split('?')[0])}" data-q="${esc(to.split('?')[1] || '')}"`) : ''}>
    <div class="v" ${typeof value === 'number' ? raw('data-count="' + value + '" data-key="kpi:' + esc(lbl) + '"') : ''}>${typeof value === 'number' ? n(value) : value}</div><div class="l">${lbl}</div>${sub ? html`<div class="s">${sub}</div>` : ''}</div>`;
}

// ================================================================= OVERVIEW
function nextStep(d, p) {
  const c = d.counts;
  const lowest = (state) => { const w = d.waves.find((x) => x.counts[state]); return w ? w.wave : null; };
  const box = (tone, ic, title, text, btn) => html`<div class="callout ${tone}"><div class="ic">${icon(ic)}</div>
    <div class="grow"><h3>${title}</h3><p>${text}</p></div>${btn || ''}</div>`;
  const link = (lbl, to, q, primary) => html`<button class="btn ${primary === false ? '' : 'primary'}" data-act="go" data-to="${to}" data-q="${q || ''}">${lbl} ${icon('arrow')}</button>`;
  if (p && p.job) return box('', 'terminal', 'Running: ' + p.job.title, 'Progress and the live log are in the panel at the bottom of the screen.', html`<button class="btn" data-act="showJob" data-id="${p.job.id}">Show log</button>`);
  if (d.awaiting_commit) return box('warn', 'commit', plural(d.awaiting_commit, 'import') + ' waiting for commit', 'commitAction is Wait: these VMs are imported but held. Commit them, or roll them back to vCenter.', link('Review', 'execute'));
  if (d.failed) return box('bad', 'triage', plural(d.failed, 'VM') + ' need attention', 'Failures are grouped by cause on the Triage page, with the likely fix for each.', link('Triage', 'triage'));
  if (d.in_flight) return box('', 'refresh', plural(d.in_flight, 'VM') + ' in flight', 'Batches from an earlier session are still on the cluster. Watch them to record the outcome.', link('Execute', 'execute'));
  if (c.precheck_passed) { const w = lowest('precheck_passed'); return box('ok', 'execute', 'Wave ' + w + ' is ready to import', plural(c.precheck_passed, 'VM') + ' passed precheck across all waves.', link('Import wave ' + w, 'execute', 'stage=import&waves=' + w)); }
  if (c.pending) { const w = lowest('pending'); return box('', 'shield', 'Precheck wave ' + w, 'Run preflight to verify the cluster, then precheck: the operator validates each VM without moving anything.', link('Go to Execute', 'execute', 'stage=precheck&waves=' + w)); }
  if (d.total && c.committed === d.total) return box('ok', 'check', 'Campaign complete', 'Every queued VM is committed in VCF Automation. Export the tracker from Activity for the change record.', link('Exports', 'activity', 'tab=exports', false));
  if (c.rolled_back) return box('', 'rollback', plural(c.rolled_back, 'VM') + ' handed back to vCenter', 'Their failed imports were rolled back. Once the cause is fixed, retry them from Triage and precheck again.', link('Triage', 'triage'));
  if (d.total) return box('', 'queue', 'Nothing left to run', 'Remaining VMs are skipped or rolled back. Retry or unskip them from the queue.', link('Queue', 'queue'));
  if (d.discovered.selected) return box('', 'stage', plural(d.discovered.selected, 'VM') + ' selected', 'Map their folders and networks to namespaces and subnets, then stage them into the import queue.', link('Map & Stage', 'stage'));
  if (d.discovered.total) return box('', 'select', plural(d.discovered.total, 'VM') + ' discovered', 'Pick the VMs to import. Folders are the unit of collection.', link('Select VMs', 'select'));
  return box('', 'discover', 'Start with discovery', 'Pull the VM inventory, folder tree and networks from vCenter. Read-only; nothing in vCenter changes.', link('Discover', 'discover'));
}

VIEWS.overview = view({
  title: 'Overview', sub: 'Where the campaign stands', live: true,
  async load(v) { v.d = await GET('/api/overview'); S.waves = v.d.waves.map((w) => w.wave); },
  paint(v) {
    const d = v.d, c = d.counts, st = d.stats || {};
    const fail = (w) => (w.counts.failed || 0) + (w.counts.precheck_failed || 0);
    return html`
      ${nextStep(d, S.pulse)}
      ${govCallouts(d, S.pulse)}
      <div class="mt">${FX.streamHtml(d)}</div>
      <div class="kpis mt">
        ${kpi('Discovered', d.discovered.total, n(d.discovered.selected) + ' selected', 'select', 'var(--accent)')}
        ${kpi('In the queue', d.total, plural(d.waves.length, 'wave'), 'queue')}
        ${kpi('Committed', d.committed, pct(d.committed, d.total) + ' of the queue', 'queue?state=committed', 'var(--s-committed)')}
        ${kpi('In flight', d.in_flight, plural(d.live_batches.length, 'live batch', 'live batches'), 'batches', 'var(--s-importing)')}
        ${kpi('Awaiting commit', d.awaiting_commit, 'commitAction ' + S.info.settings.commit_action, 'execute', 'var(--s-awaiting_commit)')}
        ${kpi('Need attention', d.failed, 'failed or precheck failed', 'triage', 'var(--s-failed)')}
        ${d.verify && d.committed ? kpi('Verified', d.verify.ok || 0, (d.verify.fail ? n(d.verify.fail) + ' failed · ' : '') + n(d.verify.none || 0) + ' not checked', 'queue?state=committed', 'var(--ok)') : ''}
      </div>
      ${d.total ? html`<div class="card mt"><div class="card-b">${segbar(c, d.total, true)}${legend(c)}
        ${st.import_seconds ? html`<div class="small faint mt-s">average import ${fmtDur(st.import_seconds.avg)}</div>` : ''}</div></div>` : ''}
      <div class="grid two mt">
        <div class="card"><div class="card-h"><h3>Waves</h3><span class="sub">run in order, lowest first</span>
          <div class="tools"><button class="btn sm" data-act="go" data-to="waves">Arrange ${icon('arrow')}</button></div></div>
          ${d.waves.length ? html`<div class="table-wrap"><table class="t"><thead><tr><th>Wave</th><th style="width:40%">Progress</th><th class="right">VMs</th><th class="right">Done</th><th class="right">Failed</th><th></th></tr></thead><tbody>
            ${d.waves.map((w) => html`<tr><td class="nowrap"><b>Wave ${w.wave}</b>${waveEta(d, w) ? html`<div class="tiny faint" title="estimated time for what is left in this wave">${icon('clock')} ${waveEta(d, w)} left</div>` : ''}</td><td>${segbar(w.counts, w.total)}</td><td class="right num">${n(w.total)}</td>
              <td class="right num">${n(w.counts.committed || 0)}</td><td class="right num ${fail(w) ? 'bad-text' : ''}">${n(fail(w))}</td>
              <td class="right nowrap"><button class="btn xs" data-act="go" data-to="execute" data-q="stage=precheck&waves=${w.wave}" ${attr(!w.counts.pending, 'disabled')}>Precheck</button>
                <button class="btn xs" data-act="go" data-to="execute" data-q="stage=import&waves=${w.wave}" ${attr(!w.counts.precheck_passed, 'disabled')}>Import</button></td></tr>`)}
          </tbody></table></div>` : html`<div class="card-b muted">No waves yet — stage VMs first.</div>`}</div>
        <div class="card"><div class="card-h"><h3>Namespaces</h3><span class="sub">target Supervisor namespaces</span></div>
          ${d.namespaces.length ? html`<div class="table-wrap"><table class="t"><thead><tr><th>Namespace</th><th style="width:40%">Progress</th><th class="right">VMs</th><th class="right">Done</th></tr></thead><tbody>
            ${d.namespaces.map((x) => html`<tr class="click" data-act="go" data-to="queue" data-q="ns=${encodeURIComponent(x.namespace)}"><td class="mono small">${x.namespace}</td><td>${segbar(x.counts, x.total)}</td><td class="right num">${n(x.total)}</td><td class="right num">${n(x.counts.committed || 0)}</td></tr>`)}
          </tbody></table></div>` : html`<div class="card-b muted">No namespaces yet.</div>`}</div>
      </div>
      ${d.apps && d.apps.length ? html`<div class="card mt"><div class="card-h"><h3>Applications</h3><span class="sub">${S.info.features.app_together ? 'kept together: an app imports only when all of it is ready' : 'from the ' + (S.info.features.app_category || 'app') + ' tag or set by hand'}</span></div>
        <div class="table-wrap"><table class="t"><thead><tr><th>Application</th><th style="width:40%">Progress</th><th class="right">VMs</th><th class="right">Done</th><th>Waves</th></tr></thead><tbody>
        ${d.apps.map((a) => { const split = (d.app_splits || []).find((x) => x.app === a.app); return html`<tr class="click" data-act="go" data-to="queue" data-q="app=${encodeURIComponent(a.app)}">
          <td>${appTag(a.app)}</td><td>${segbar(a.counts, a.total)}</td><td class="right num">${n(a.total)}</td><td class="right num">${n(a.counts.committed || 0)}</td>
          <td class="small">${split ? html`<span class="warn-text">split: ${Object.keys(split.waves).join(', ')}</span>` : ''}</td></tr>`; })}</tbody></table></div></div>` : ''}
      <div class="grid two mt">
        <div class="card"><div class="card-h"><h3>Live batches</h3><span class="sub">${plural(d.live_batches.length, 'batch', 'batches')} on the cluster</span></div>
          ${d.live_batches.length ? html`<div class="table-wrap"><table class="t compact"><tbody>${d.live_batches.slice(0, 10).map((b) => html`<tr class="click" data-act="batch" data-ns="${b.namespace}" data-name="${b.name}">
            <td class="mono small">${b.name}</td><td><span class="tag">${b.stage}</span></td><td>${batchTag(b.state)}</td><td class="msg"><div>${b.message || ''}</div></td></tr>`)}</tbody></table></div>`
            : html`<div class="card-b muted small">Nothing in flight.</div>`}</div>
        <div class="card"><div class="card-h"><h3>Recent failures</h3><div class="tools"><button class="btn sm" data-act="go" data-to="triage" ${attr(!d.failed, 'disabled')}>Triage ${icon('arrow')}</button></div></div>
          ${d.recent_failures.length ? html`<div class="table-wrap"><table class="t compact"><tbody>${d.recent_failures.map((x) => html`<tr class="click" data-act="vm" data-moref="${x.moref}">
            <td><span class="vm-name">${x.vm_name}<small>${x.moref}</small></span></td><td>${pill(x.state)}</td><td class="msg"><div>${x.message || ''}</div></td></tr>`)}</tbody></table></div>`
            : html`<div class="card-b muted small">No failures.</div>`}</div>
      </div>
      <p class="small faint mt">run ${d.meta.run_id || '—'} · vCenter ${d.meta.vcenter || '—'} · discovered ${d.meta.discovered_at || 'never'} · ${n(st.transitions)} recorded transitions</p>`;
  },
});

function waveEta(d, w) {
  const e = (d.estimates || {})[w.wave];
  if (!e) return '';
  if (w.counts.pending || w.counts.precheck_failed) return eta(e.precheck.seconds + e.import.seconds);
  return e.import.seconds ? eta(e.import.seconds) : '';
}

// ================================================================= DISCOVER
VIEWS.discover = view({
  title: 'Discover', sub: 'Step 1 · read the VM inventory from vCenter',
  init(v) {
    const vc = S.info.vcenter;
    v.form = v.form || { server: vc.server || '', user: vc.user || '', password: '', insecure: false, powered_on: false, no_tools: false, concurrency: 12, remember: false, no_tags: false };
  },
  async load(v) {
    const [o, d, r] = await Promise.all([GET('/api/overview'), GET('/api/discovered'), GET('/api/readiness')]);
    v.o = o;
    v.vms = d.vms;
    v.ready = r;
  },
  paint(v) {
    const f = v.form, o = v.o, vc = S.info.vcenter;
    const running = S.pulse && S.pulse.job && S.pulse.job.kind === 'discover';
    const dcs = uniq(v.vms.map((x) => x.datacenter)), clusters = uniq(v.vms.map((x) => x.cluster));
    const nets = uniq(v.vms.flatMap((x) => (x.networks || '').split(',')));
    const top = {};
    v.vms.forEach((x) => { const t = (x.folder || '').split('/')[0] || '(datacenter root)'; top[t] = (top[t] || 0) + 1; });
    const noTools = v.vms.filter((x) => x.tools_status && !/running/i.test(x.tools_status)).length;
    return html`<div class="grid two">
      <div class="card"><div class="card-h"><h3>Connect to vCenter</h3><span class="sub">read-only</span></div>
        <div class="card-b stack">
          <div class="form-grid">
            <label class="field"><span>vCenter server</span><input class="input" id="f-server" data-input="field" data-k="server" value="${f.server}" placeholder="vcenter.example.local" autocomplete="off"></label>
            <label class="field"><span>User</span><input class="input" id="f-user" data-input="field" data-k="user" value="${f.user}" placeholder="administrator@vsphere.local" autocomplete="username"></label>
            <label class="field"><span>Password ${tip('vc_password')}</span><input class="input" id="f-pass" type="password" data-input="field" data-k="password" value="${f.password}" data-enter="discover"
              placeholder="${vc.password_from_env ? 'from VCFA_VC_PASSWORD' : ''}" autocomplete="current-password">
              <small>Used for this discovery only; never stored.</small></label>
            <label class="field"><span>Parallel detail reads ${tip('concurrency')}</span><input class="input" type="number" min="1" max="32" id="f-conc" data-input="field" data-k="concurrency" value="${f.concurrency}"></label>
          </div>
          <div class="row wrap" style="gap:18px">
            <label class="check"><input type="checkbox" data-change="field" data-k="powered_on" ${attr(f.powered_on, 'checked')}> Powered-on VMs only ${tip('powered_on')}</label>
            <label class="check"><input type="checkbox" data-change="field" data-k="no_tools" ${attr(f.no_tools, 'checked')}> Skip the VM Tools check ${tip('no_tools')}</label>
            <label class="check"><input type="checkbox" data-change="field" data-k="insecure" ${attr(f.insecure, 'checked')}> Skip TLS verification ${tip('insecure')}</label>
            <label class="check"><input type="checkbox" data-change="field" data-k="no_tags" ${attr(f.no_tags, 'checked')}> Skip vCenter tags ${tip('no_tags')}</label>
          </div>
          <label class="check small"><input type="checkbox" data-change="field" data-k="remember" ${attr(f.remember, 'checked')}> Keep these credentials in memory for post-import verification ${tip('remember')}
            <span class="muted">(never written to disk; gone when the console stops)</span></label>
          ${f.insecure ? html`<div class="note warn">TLS verification is off: fine for a lab with a self-signed certificate, not for production.</div>` : ''}
          <div id="disc-progress">${running ? discProgress(S.pulse.job) : ''}</div>
        </div>
        <div class="card-f"><button class="btn primary" data-act="discover" ${attr(!!(S.pulse && S.pulse.job), 'disabled')}>${icon('discover')} ${o.discovered.total ? 'Re-discover' : 'Discover inventory'}</button>
          <span class="small muted">Re-discovering refreshes VM facts and keeps your selection.</span></div>
      </div>
      <div class="card"><div class="card-h"><h3>Inventory cache</h3>${o.meta.discovered_at ? html`<span class="sub">${o.meta.vcenter} · ${o.meta.discovered_at}</span>` : ''}</div>
        ${o.discovered.total ? html`<div class="card-b stack">
          <div class="kpis">${kpi('VMs', o.discovered.total, '')}${kpi('Selected', o.discovered.selected, '')}${kpi('Clusters', clusters.length, plural(dcs.length, 'datacenter'))}${kpi('Networks', nets.length, '')}</div>
          ${readinessPanel(v.ready)}
          <div><div class="section-title" style="margin-top:4px">Top-level folders</div>
            <div class="chips">${Object.entries(top).sort((a, b) => b[1] - a[1]).map(([k, c]) => html`<span class="chip" data-act="go" data-to="select" data-q="folder=${encodeURIComponent(k === '(datacenter root)' ? '' : k)}">${icon('folder')} ${k} <span class="n">${n(c)}</span></span>`)}</div></div>
        </div>
        <div class="card-f"><button class="btn primary" data-act="go" data-to="select">Select VMs ${icon('arrow')}</button></div>`
        : emptyState('discover', 'Nothing discovered yet', 'Connect to vCenter to pull the VM list, the folder tree and each VM’s networks.')}
      </div></div>`;
  },
  onPulse(v, p) {
    const el = $('#disc-progress');
    if (el) mount(el, p.job && p.job.kind === 'discover' ? discProgress(p.job) : '');
  },
  onJobDone(v, j) { if (j.kind === 'discover') v.refresh(); else v.render(); },
  act: {
    field(t, e, v) { v.form[t.dataset.k] = t.type === 'checkbox' ? t.checked : t.value; if (t.type === 'checkbox') v.render(); },
    async discover(t, e, v) {
      const f = v.form;
      if (!f.server || !f.user) { toast('Server and user are required', '', 'warn'); return; }
      if (!f.password && !S.info.vcenter.password_from_env) { toast('Enter the vCenter password', '', 'warn'); $('#f-pass').focus(); return; }
      const job = await startJob('discover', Object.assign({}, f, { concurrency: parseInt(f.concurrency, 10) || 12 }));
      if (job) { f.password = ''; v.render(); }
    },
  },
});
function readinessPanel(r) {
  if (!r || !(r.grades.ready + r.grades.warn + r.grades.block)) return '';
  const g = r.grades;
  return html`<div><div class="section-title" style="margin-top:4px">Readiness <span class="tiny faint">advisory — the operator's precheck is the authority</span></div>
    <div class="chips">
      <span class="chip" data-act="go" data-to="select" data-q="readiness=ready">${gradeTag('ready')} <span class="n">${n(g.ready)}</span></span>
      <span class="chip" data-act="go" data-to="select" data-q="readiness=warn">${gradeTag('warn')} <span class="n">${n(g.warn)}</span></span>
      <span class="chip" data-act="go" data-to="select" data-q="readiness=block">${gradeTag('block')} <span class="n">${n(g.block)}</span></span></div>
    ${r.findings.length ? html`<ul class="plain small mt-s">${r.findings.filter((f) => f.level !== 'info').slice(0, 5).map((f) => html`<li><b>${n(f.count)}</b> · ${f.title} <span class="muted">— ${f.advice}</span></li>`)}</ul>` : ''}</div>`;
}
function discProgress(job) {
  const p = job.progress;
  return html`<div class="stack" style="gap:6px"><div class="row small"><span class="spinner"></span><b>Discovering…</b>
    <span class="muted">${p ? n(p.done) + ' of ' + n(p.total) + ' VM details read' : 'listing VMs, folders and networks'}</span></div>
    <div class="progress ${p ? '' : 'indet'}"><span style="width:${p ? pct(p.done, p.total) : '30%'}"></span></div></div>`;
}

// =================================================================== SELECT
function buildTree(vms) {
  const dcs = new Map();
  for (const vm of vms) {
    const dc = vm.datacenter || '';
    const parts = (vm.folder || '').split('/').filter(Boolean);
    if (!dcs.has(dc)) dcs.set(dc, new Map());
    const nodes = dcs.get(dc);
    for (let i = 0; i <= parts.length; i++) {
      const path = parts.slice(0, i).join('/');
      let nd = nodes.get(path);
      if (!nd) {
        nd = { path, name: i ? parts[i - 1] : '', depth: i, children: [], subtree: 0, sel: 0 };
        nodes.set(path, nd);
        if (i > 0) nodes.get(parts.slice(0, i - 1).join('/')).children.push(path);
      }
      nd.subtree++;
      if (vm.selected) nd.sel++;
    }
  }
  return dcs;
}

VIEWS.select = view({
  title: 'Select VMs', sub: 'Step 2 · choose what to import; folders are the unit of collection',
  init(v, p) {
    v.f = v.f || { q: '', dc: null, folder: null, cluster: '', network: '', power: '', tools: '', show: 'all', ready: '', tag: '', app: '' };
    if (p.folder !== undefined) { v.f.folder = p.folder; v.f.dc = null; }
    if (p.readiness !== undefined) v.f.ready = p.readiness;
    v.sort = v.sort || { key: 'name', dir: 1 };
    v.page = 0;
    v.open = v.open || new Set();
  },
  async load(v) { const d = await GET('/api/discovered'); v.vms = d.vms; v.meta = d.meta; v.byMoref = new Map(v.vms.map((x) => [x.moref, x])); },
  filtered(v) {
    const f = v.f, q = f.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
    return v.vms.filter((x) => {
      if (f.folder !== null && !((f.dc === null || x.datacenter === f.dc) && inFolder(x.folder || '', f.folder))) return false;
      if (f.cluster && x.cluster !== f.cluster) return false;
      if (f.network && !(x.networks || '').split(',').includes(f.network)) return false;
      if (f.power === 'on' && x.power_state !== 'POWERED_ON') return false;
      if (f.power === 'off' && x.power_state === 'POWERED_ON') return false;
      if (f.tools === 'ok' && !/running/i.test(x.tools_status || '')) return false;
      if (f.tools === 'bad' && /running/i.test(x.tools_status || '')) return false;
      if (f.show === 'sel' && !x.selected) return false;
      if (f.show === 'unsel' && x.selected) return false;
      if (f.ready && x.readiness !== f.ready) return false;
      if (f.tag && !(x.tags || []).includes(f.tag)) return false;
      if (f.app && (f.app === '(none)' ? x.app : x.app !== f.app)) return false;
      if (q.length) {
        const hay = [x.name, x.moref, x.cluster, x.folder, x.networks, x.guest_os, x.namespace, x.app, x.ip, (x.tags || []).join(' ')].join(' ').toLowerCase();
        if (!q.every((t) => hay.includes(t))) return false;
      }
      return true;
    });
  },
  paint(v) {
    if (!v.vms.length) return emptyState('discover', 'Nothing discovered yet', 'Discover the vCenter inventory first.', html`<button class="btn primary" data-act="go" data-to="discover">Discover</button>`);
    const rows = sortRows(VIEWS.select.filtered(v), v.sort.key, v.sort.dir);
    v.rows = rows;
    const maxPage = Math.max(0, Math.ceil(rows.length / PAGE) - 1);
    if (v.page > maxPage) v.page = maxPage;
    const pageRows = rows.slice(v.page * PAGE, (v.page + 1) * PAGE);
    const selCount = v.vms.filter((x) => x.selected).length;
    const inViewSel = rows.filter((x) => x.selected).length;
    const noNs = v.vms.filter((x) => x.selected && !x.namespace).length;
    const clusters = uniq(v.vms.map((x) => x.cluster)).sort();
    const nets = uniq(v.vms.flatMap((x) => (x.networks || '').split(','))).sort();
    const nss = uniq(v.vms.map((x) => x.namespace)).sort();
    const apps = uniq(v.vms.map((x) => x.app)).sort();
    const tags = uniq(v.vms.flatMap((x) => x.tags || [])).sort();
    const f = v.f;
    const allOnPage = pageRows.length && pageRows.every((x) => x.selected);
    return html`<div class="grid side">
      <div class="card"><div class="card-h"><h3>Folders ${tip('selection')}</h3><span class="sub">${plural(v.vms.length, 'VM')}</span></div>
        <div class="tree" data-scroll="tree">
          <div class="tnode ${f.folder === null ? 'on' : ''}" data-act="folder" data-path="" data-all="1"><span class="caret"></span><span class="name">All folders</span><span class="cnt"><b>${n(selCount)}</b> / ${n(v.vms.length)}</span></div>
          ${treeHtml(v)}</div></div>
      <div class="stack" style="gap:12px">
        <div class="card">
          <div class="toolbar">
            <div class="search">${icon('search')}<input class="input" id="sel-q" placeholder="Search name, moref, network, OS…" value="${f.q}" data-input="q"></div>
            <select class="select" data-change="filter" data-k="cluster"><option value="">All clusters</option>${clusters.map((c) => html`<option ${attr(f.cluster === c, 'selected')}>${c}</option>`)}</select>
            <select class="select" data-change="filter" data-k="network"><option value="">All networks</option>${nets.map((c) => html`<option ${attr(f.network === c, 'selected')}>${c}</option>`)}</select>
            <select class="select" data-change="filter" data-k="power"><option value="">Any power</option><option value="on" ${attr(f.power === 'on', 'selected')}>Powered on</option><option value="off" ${attr(f.power === 'off', 'selected')}>Powered off</option></select>
            <select class="select" data-change="filter" data-k="tools"><option value="">Any Tools</option><option value="ok" ${attr(f.tools === 'ok', 'selected')}>Tools running</option><option value="bad" ${attr(f.tools === 'bad', 'selected')}>Tools not running</option></select>
            <select class="select" data-change="filter" data-k="ready"><option value="">Any readiness</option>${[['ready', 'Ready'], ['warn', 'Check first'], ['block', 'Likely to fail']].map(([k, l]) => html`<option value="${k}" ${attr(f.ready === k, 'selected')}>${l}</option>`)}</select>
            ${apps.length ? html`<select class="select" data-change="filter" data-k="app"><option value="">Any app</option><option value="(none)" ${attr(f.app === '(none)', 'selected')}>No app</option>${apps.map((a) => html`<option ${attr(f.app === a, 'selected')}>${a}</option>`)}</select>` : ''}
            ${tags.length ? html`<select class="select" data-change="filter" data-k="tag"><option value="">Any tag</option>${tags.map((t) => html`<option ${attr(f.tag === t, 'selected')}>${t}</option>`)}</select>` : ''}
            <div class="seg">${[['all', 'All'], ['sel', 'Selected'], ['unsel', 'Not selected']].map(([k, l]) => html`<button class="${f.show === k ? 'on' : ''}" data-act="show" data-k="${k}">${l}</button>`)}</div>
          </div>
          <div class="toolbar thin">${savedViews('select')}</div>
          <div class="bulkbar">
            <span><b>${n(rows.length)}</b> match · <b>${n(inViewSel)}</b> of them selected</span>
            <button class="btn sm" data-act="selMatching" data-on="1" ${attr(!rows.length, 'disabled')}>Select all ${n(rows.length)}</button>
            <button class="btn sm" data-act="selMatching" data-on="0" ${attr(!inViewSel, 'disabled')}>Deselect</button>
            <span class="grow"></span>
            <input class="input sm" id="bulk-ns" list="ns-list" placeholder="namespace" style="width:180px">
            <button class="btn sm" data-act="bulkNs" ${attr(!inViewSel, 'disabled')}>Set namespace</button>${tip('namespace')}
            <input class="input sm" id="bulk-wave" type="number" min="0" placeholder="wave" style="width:74px">
            <button class="btn sm" data-act="bulkWave" ${attr(!inViewSel, 'disabled')}>Set wave</button>${tip('wave')}
            <input class="input sm" id="bulk-app" list="app-list" placeholder="application" style="width:140px">
            <button class="btn sm" data-act="bulkApp" ${attr(!inViewSel, 'disabled')}>Set app</button>
            <datalist id="ns-list">${nss.map((x) => html`<option value="${x}">`)}</datalist>
            <datalist id="app-list">${apps.map((x) => html`<option value="${x}">`)}</datalist>
          </div>
          <div class="table-wrap"><table class="t">
            <thead><tr><th class="chk"><input type="checkbox" data-act="selPage" title="Select this page" ${attr(allOnPage, 'checked')}></th>
              ${th(v, 'name', 'VM')}${th(v, 'folder', 'Folder · cluster')}${th(v, 'networks', 'Networks')}
              ${th(v, 'cpu_count', 'Size')}${th(v, 'power_state', 'Power · Tools')}${th(v, 'readiness', html`Ready ${tip('readiness')}`)}${th(v, 'app', html`App · tags ${tip('app')}`)}${th(v, 'namespace', 'Namespace')}${th(v, 'wave', 'Wave')}<th>Queue</th></tr></thead>
            <tbody>${pageRows.map((x, i) => html`<tr class="${x.selected ? 'on' : ''}">
              <td class="chk"><input type="checkbox" data-act="sel" data-moref="${x.moref}" data-i="${v.page * PAGE + i}" ${attr(x.selected, 'checked')}></td>
              <td><span class="vm-name">${x.name}<small>${x.moref}</small></span></td>
              <td class="small clip" title="${x.folder} · ${x.cluster}">${x.folder || html`<span class="faint">/</span>`}<div class="tiny faint">${x.cluster}</div></td>
              <td class="small clip" title="${x.networks}">${(x.networks || '').split(',').filter(Boolean).join(', ') || html`<span class="warn-text">no NICs</span>`}</td>
              <td class="small nowrap num">${x.cpu_count} vCPU · ${n(Math.round(x.memory_mb / 1024))} GiB</td>
              <td class="nowrap"><span class="tag ${x.power_state === 'POWERED_ON' ? 'ok' : ''}">${(x.power_state || '?').replace('POWERED_', '').toLowerCase()}</span>
                <span class="small ${x.tools_status && !/running/i.test(x.tools_status) ? 'warn-text' : 'muted'}">${(x.tools_status || '—').replace(/^TOOLS_/, '').replace(/_/g, ' ').toLowerCase()}</span></td>
              <td>${gradeTag(x.readiness, (x.findings || []).join('\n'))}</td>
              <td class="nowrap">${appTag(x.app)} ${tagChips((x.tags || []).filter((t) => !x.app || !t.endsWith(':' + x.app)), 2)}</td>
              <td class="small mono">${x.namespace || html`<span class="faint">from map</span>`}</td>
              <td class="num">${x.wave || html`<span class="faint">—</span>`}</td>
              <td>${x.queue_state ? pill(x.queue_state) : ''}</td></tr>`)}</tbody></table></div>
          ${pager(rows.length, v.page, PAGE)}
        </div>
        <div class="callout stickybar" style="background:var(--panel)"><div class="ic">${icon('select')}</div>
          <div class="grow"><h3>${plural(selCount, 'VM')} selected</h3><p>${noNs ? plural(noNs, 'selected VM') + ' without a namespace — the folder or portgroup map decides in the next step.' : 'Namespace and wave can come from the maps in the next step.'}
            ${v.meta.discovered_at ? ' · discovered ' + v.meta.discovered_at : ''}</p></div>
          <button class="btn danger-ghost sm" data-act="clearAll" ${attr(!selCount, 'disabled')}>Clear selection</button>
          <button class="btn primary" data-act="go" data-to="stage" ${attr(!selCount, 'disabled')}>Map & Stage ${icon('arrow')}</button></div>
      </div></div>`;
  },
  after(v) {
    $$('[data-tri]', v.el).forEach((cb) => { cb.indeterminate = cb.dataset.tri === 'some'; });
  },
  onJobDone(v, j) { if (j.kind === 'discover') v.refresh(); },
  act: {
    q: debounce((t, e, v) => { v.f.q = t.value; v.page = 0; v.render(); }, 180),
    filter(t, e, v) { v.f[t.dataset.k] = t.value; v.page = 0; v.render(); },
    show(t, e, v) { v.f.show = t.dataset.k; v.page = 0; v.render(); },
    sort(t, e, v) { const k = t.dataset.key; v.sort = { key: k, dir: v.sort.key === k ? -v.sort.dir : 1 }; v.render(); },
    page(t, e, v) { v.page = +t.dataset.page; v.render(); window.scrollTo(0, 0); },
    folder(t, e, v) {
      if (e.target.closest('.caret') || e.target.closest('input')) return;
      if (t.dataset.all) { v.f.folder = null; v.f.dc = null; } else { v.f.folder = t.dataset.path; v.f.dc = t.dataset.dc; }
      v.page = 0; v.render();
    },
    toggle(t, e, v) { const k = t.dataset.key; if (v.open.has(k)) v.open.delete(k); else v.open.add(k); v.render(); },
    async treeSel(t, e, v) {
      const dc = t.dataset.dc, path = t.dataset.path;
      const members = v.vms.filter((x) => (x.datacenter || '') === dc && inFolder(x.folder || '', path));
      const on = !members.every((x) => x.selected);
      await setSel(v, members, on);
    },
    async sel(t, e, v) {
      const i = +t.dataset.i, on = t.checked;
      let targets = [v.byMoref.get(t.dataset.moref)];
      if (e.shiftKey && v.lastI !== undefined) {
        const [a, b] = [Math.min(v.lastI, i), Math.max(v.lastI, i)];
        targets = v.rows.slice(a, b + 1);
      }
      v.lastI = i;
      await setSel(v, targets, on);
    },
    async selPage(t, e, v) { await setSel(v, v.rows.slice(v.page * PAGE, (v.page + 1) * PAGE), t.checked); },
    async selMatching(t, e, v) {
      const on = t.dataset.on === '1';
      if (on && v.rows.length > 300) {
        const ok = await modal({ title: 'Select ' + plural(v.rows.length, 'VM') + '?', confirm: 'Select', body: html`<p>Everything matching the current filters is selected for import.</p>` });
        if (!ok) return;
      }
      await setSel(v, v.rows, on);
    },
    async clearAll(t, e, v) {
      const ok = await modal({ title: 'Clear the whole selection?', confirm: 'Clear', danger: true, body: html`<p>Deselects every discovered VM. VMs already staged stay in the import queue.</p>` });
      if (!ok) return;
      await POST('/api/select/clear');
      await v.refresh();
    },
    async bulkNs(t, e, v) {
      const ns = $('#bulk-ns').value.trim();
      const targets = v.rows.filter((x) => x.selected);
      const ok = await modal({ title: ns ? 'Set namespace ' + ns : 'Clear the namespace', confirm: 'Apply',
        body: html`<p>For the ${plural(targets.length, 'selected VM')} matching the filters. ${ns ? 'An explicit namespace beats the folder and portgroup maps.' : 'The maps decide again.'}</p>` });
      if (!ok) return;
      await POST('/api/select', { morefs: targets.map((x) => x.moref), namespace: ns });
      targets.forEach((x) => { x.namespace = ns; });
      toast('Namespace ' + (ns ? 'set' : 'cleared') + ' on ' + plural(targets.length, 'VM'), '', 'ok');
      v.render();
    },
    async bulkApp(t, e, v) {
      const app = $('#bulk-app').value.trim();
      const targets = v.rows.filter((x) => x.selected);
      await POST('/api/select', { morefs: targets.map((x) => x.moref), app });
      toast(app ? 'Application ' + app + ' set on ' + plural(targets.length, 'VM') : 'Application cleared on ' + plural(targets.length, 'VM'),
        app ? 'A hand-set application beats the tag. Staging again carries it into the queue.' : 'The tag decides again.', 'ok');
      await v.refresh();
    },
    async bulkWave(t, e, v) {
      const w = parseInt($('#bulk-wave').value, 10);
      if (!(w >= 0)) { toast('Enter a wave number (0 clears it)', '', 'warn'); return; }
      const targets = v.rows.filter((x) => x.selected);
      await POST('/api/select', { morefs: targets.map((x) => x.moref), wave: w });
      targets.forEach((x) => { x.wave = w || null; });
      toast(w ? 'Wave ' + w + ' set on ' + plural(targets.length, 'VM') : 'Wave cleared on ' + plural(targets.length, 'VM'), 'An explicit wave beats the maps.', 'ok');
      v.render();
    },
  },
});
async function setSel(v, targets, on) {
  const change = targets.filter((x) => x && x.selected !== on);
  if (!change.length) { v.render(); return; }
  change.forEach((x) => { x.selected = on; });
  v.render();
  try { await POST('/api/select', { morefs: change.map((x) => x.moref), selected: on }); } catch (e) {
    change.forEach((x) => { x.selected = !on; }); v.render(); throw e;
  }
}
function treeHtml(v) {
  const dcs = buildTree(v.vms), out = [];
  const multi = dcs.size > 1;
  for (const [dc, nodes] of dcs) {
    if (multi) out.push(html`<div class="tdc">${dc || '(unknown datacenter)'}</div>`);
    const walk = (path) => {
      const nd = nodes.get(path);
      const kids = nd.children.slice().sort((a, b) => a.localeCompare(b));
      for (const k of kids) {
        const c = nodes.get(k), key = dc + '|' + k, open = v.open.has(key);
        const on = v.f.folder === k && (v.f.dc === null || v.f.dc === dc);
        const tri = c.sel === 0 ? 'none' : c.sel === c.subtree ? 'all' : 'some';
        out.push(html`<div class="tnode ${on ? 'on' : ''}" style="padding-left:${6 + (c.depth - 1) * 14}px" data-act="folder" data-path="${k}" data-dc="${dc}">
          <span class="caret ${open ? 'open' : ''}" ${c.children.length ? raw(`data-act="toggle" data-key="${esc(key)}"`) : ''}>${c.children.length ? icon('chevron') : ''}</span>
          <input type="checkbox" data-act="treeSel" data-path="${k}" data-dc="${dc}" data-tri="${tri}" ${attr(tri === 'all', 'checked')} title="Select everything in this folder">
          <span class="name" title="${k}">${c.name}</span><span class="cnt">${c.sel ? html`<b>${n(c.sel)}</b> / ` : ''}${n(c.subtree)}</span></div>`);
        if (open) walk(k);
      }
    };
    const root = nodes.get('');
    const direct = v.vms.filter((x) => (x.datacenter || '') === dc && !x.folder);
    if (direct.length) {
      const sel = direct.filter((x) => x.selected).length;
      out.push(html`<div class="tnode ${v.f.folder === '' && v.f.dc === dc ? 'on' : ''}" data-act="folder" data-path="" data-dc="${dc}" title="VMs directly in the datacenter's root VM folder">
        <span class="caret"></span><span class="name faint">(root) — ${plural(direct.length, 'VM')}</span><span class="cnt">${sel ? html`<b>${n(sel)}</b>` : ''}</span></div>`);
    }
    if (root) walk('');
  }
  return out;
}

// ============================================================ MAP & STAGE
VIEWS.stage = view({
  title: 'Map & Stage', sub: 'Step 3 · decide namespace, wave and subnet, then build the import queue',
  init(v) { v.defaults = v.defaults || { ns: '', wave: 1 }; v.recPage = 0; v.adv = v.adv || false; v.result = null; },
  async load(v) {
    const m = await GET('/api/maps');
    v.maps = m;
    v.fr = m.folder.rows.map((r) => Object.assign({}, r));
    v.nr = m.network.rows.map((r) => Object.assign({}, r));
    v.tr = m.tag.rows.map((r) => Object.assign({}, r));
    v.saved = mapsSig(v);
    v.preview = await POST('/api/stage/preview', stageBody(v));
  },
  paint(v) {
    const p = v.preview;
    if (!p.selected) return emptyState('select', 'No VMs selected', 'Select the VMs to import first; this step decides where each one goes.', html`<button class="btn primary" data-act="go" data-to="select">Select VMs</button>`);
    const dirty = mapsSig(v) !== v.saved;
    const nsOptions = uniq([].concat(v.fr.map((r) => r.namespace), v.nr.map((r) => r.namespace), v.tr.map((r) => r.namespace), p.records.map((r) => r.namespace))).sort();
    return html`
      <div class="note info">A VM's <b>namespace</b> comes from, in order: the namespace set on it in Select, the tag map, the folder map (most specific folder wins), the portgroup map, then the default below.
        <b>Wave</b> follows the same order. <b>Subnets</b> come from the portgroup map. Nothing is guessed: a VM with no namespace is reported and left out.</div>
      <datalist id="ns-options">${nsOptions.map((x) => html`<option value="${x}">`)}</datalist>
      <div class="grid two mt">
        <div class="card"><div class="card-h"><h3>Folders → namespace &amp; wave ${tip('folder_map')}</h3><span class="sub mono tiny" title="${v.maps.folder.path}">${v.maps.folder.path.split(/[\\/]/).pop()}</span></div>
          <div class="card-b"><div class="small muted mb">Folders of the selected VMs</div><div id="cov-folders" class="cov">${covFolders(p.coverage)}</div></div>
          <div class="card-b flush" style="border-top:1px solid var(--line)">${mapTable(v, 'f')}</div>
          <div class="card-f"><button class="btn sm" data-act="addRow" data-m="f">${icon('plus')} Add entry</button><span class="small muted">Globs allowed, e.g. <code>Legacy/*</code>. A folder covers its whole subtree.</span>${tip('batch_group')}</div></div>
        <div class="card"><div class="card-h"><h3>Networks → subnet ${tip('portgroup_map')}</h3><span class="sub mono tiny" title="${v.maps.network.path}">${v.maps.network.path.split(/[\\/]/).pop()}</span>
          <div class="tools"><label class="check small"><input type="checkbox" data-change="adv" ${attr(v.adv, 'checked')}> Advanced columns</label>${tip('device_key')}</div></div>
          <div class="card-b"><div class="small muted mb">Portgroups of the selected VMs</div><div id="cov-networks" class="cov">${covNetworks(p.coverage)}</div></div>
          <div class="card-b flush" style="border-top:1px solid var(--line)">${mapTable(v, 'n')}</div>
          <div class="card-f"><button class="btn sm" data-act="addRow" data-m="n">${icon('plus')} Add entry</button><span class="small muted">In a VPC namespace, name the Subnet as it appears in the VPC's own namespace.</span>${tip('subnet')}</div></div>
      </div>
      <details class="card mt tagmap" ${attr(v.tr.length || (p.coverage.tags || []).length && v.tagOpen !== false, 'open')} data-toggle="tagOpen">
        <summary class="card-h"><h3>${icon('tag')} vCenter tags → namespace &amp; wave ${tip('tag_map')}</h3><span class="sub">optional · beats the folder map · <span class="mono tiny">${v.maps.tag.path.split(/[\\/]/).pop()}</span></span></summary>
        <div class="card-b"><div class="small muted mb">Tags on the selected VMs</div><div id="cov-tags" class="cov">${covTags(p.coverage)}</div></div>
        <div class="card-b flush" style="border-top:1px solid var(--line)">${mapTable(v, 't')}</div>
        <div class="card-f"><button class="btn sm" data-act="addRow" data-m="t">${icon('plus')} Add entry</button><span class="small muted">Tags are <code>Category:Tag</code>; globs allowed, e.g. <code>Application:*</code>. An exact tag beats a glob.</span></div></details>
      <div class="card mt"><div class="card-h"><h3>Result</h3><span class="sub">live preview — nothing is staged until you click Stage</span>
        <div class="tools"><label class="field" style="grid-auto-flow:column;align-items:center;gap:8px"><span>Default namespace ${tip('default_namespace')}</span>
          <input class="input sm" id="def-ns" list="ns-options" data-input="defNs" value="${v.defaults.ns}" placeholder="none — report instead" style="width:200px"></label>
          <label class="field" style="grid-auto-flow:column;align-items:center;gap:8px"><span>Default wave ${tip('default_wave')}</span>
          <input class="input sm" id="def-wave" type="number" min="1" data-input="defWave" value="${v.defaults.wave}" style="width:64px"></label></div></div>
        <div id="stage-preview">${stagePreview(v)}</div></div>
      <div class="callout stickybar" style="background:var(--panel)"><div class="ic">${icon('stage')}</div>
        <div class="grow"><h3 id="stage-count">${plural(p.records.length, 'VM')} ready to stage</h3>
          <p id="stage-dirty">${dirty ? html`<span class="warn-text">The maps have unsaved edits — staging saves them.</span>` : 'Maps saved.'} Staging again later is safe: VMs already in flight are never changed.</p></div>
        <button class="btn" data-act="saveMaps">Save maps</button>
        <button class="btn primary" data-act="stage" ${attr(!p.records.length, 'disabled')}>Stage ${icon('arrow')}</button></div>`;
  },
  act: {
    cell(t, e, v) {
      rowsOf(v, t.dataset.m)[+t.dataset.i][t.dataset.col] = t.value;
      schedulePreview(v);
    },
    addRow(t, e, v) {
      const m = t.dataset.m;
      const rows = rowsOf(v, m);
      const row = m === 'f' ? { folder: t.dataset.key || '', namespace: '', wave: '', group: '' }
        : m === 't' ? { tag: t.dataset.key || '', namespace: '', wave: '', group: '' }
          : { portgroup: t.dataset.key || '', namespace: '', subnet: '', wave: '', device_key: '', subnet_kind: '', subnet_api_group: '' };
      rows.push(row);
      if (m === 't') v.tagOpen = true;
      v.render();
      const keyCol = { f: 'folder', n: 'portgroup', t: 'tag' }[m], valCol = m === 'n' ? 'subnet' : 'namespace';
      const focus = $('#' + m + '-' + (rows.length - 1) + '-' + (t.dataset.key ? valCol : keyCol));
      if (focus) focus.focus();
      schedulePreview(v);
    },
    delRow(t, e, v) { rowsOf(v, t.dataset.m).splice(+t.dataset.i, 1); v.render(); schedulePreview(v); },
    tagOpen(t, e, v) { v.tagOpen = t.open; },
    adv(t, e, v) { v.adv = t.checked; v.render(); },
    defNs(t, e, v) { v.defaults.ns = t.value; schedulePreview(v); },
    defWave(t, e, v) { v.defaults.wave = t.value; schedulePreview(v); },
    page(t, e, v) { v.recPage = +t.dataset.page; mount($('#stage-preview'), stagePreview(v)); },
    async saveMaps(t, e, v) {
      const m = await PUT('/api/maps', { folder_rows: v.fr, network_rows: v.nr, tag_rows: v.tr });
      v.saved = mapsSig(v);
      v.maps = m;
      toast('Maps saved', m.folder.path + ' · ' + m.network.path + (v.tr.length ? ' · ' + m.tag.path : ''), 'ok');
      v.render();
    },
    async stage(t, e, v) {
      await runPreview(v);
      const p = v.preview;
      const already = p.records.filter((r) => r.queue_state).length, locked = p.records.filter((r) => r.locked).length;
      const ok = await modal({
        title: 'Stage ' + plural(p.records.length, 'VM') + ' into the import queue?', ic: 'stage', confirm: 'Stage',
        body: html`<p>${n(p.records.length - already)} new · ${n(already - locked)} already queued and updated · ${n(locked)} in flight or done and left untouched.</p>
          ${p.problems.length ? html`<div class="note warn">${plural(p.problems.length, 'VM')} cannot be placed and will be left out (see the list under Result).</div>` : ''}
          <p class="small muted">The maps are saved at the same time. Nothing is sent to the cluster.</p>`,
      });
      if (!ok) return;
      const r = await POST('/api/stage', Object.assign(stageBody(v), { save_maps: true }));
      v.saved = mapsSig(v);
      await modal({
        title: 'Staged ' + plural(r.staged, 'VM'), ic: 'check', confirm: 'Arrange waves', cancel: 'Stay here',
        body: html`<div class="kpis">${kpi('Added', r.added, '')}${kpi('Updated', r.updated, '')}${kpi('Unchanged', r.unchanged, '')}${kpi('Locked', r.locked, 'in flight or done')}</div>
          ${r.conflicts.length ? html`<div class="note warn mt"><ul class="plain">${r.conflicts.slice(0, 10).map((c) => html`<li>${c}</li>`)}</ul></div>` : ''}
          ${(r.app_moves || []).length ? html`<div class="note info mt"><b>Applications kept together:</b><ul class="plain">${r.app_moves.slice(0, 10).map((c) => html`<li>${c}</li>`)}</ul></div>` : ''}`,
      }).then((go2) => { if (go2) go('waves'); else v.refresh(); });
    },
  },
});
function stageBody(v) {
  return { folder_rows: v.fr, network_rows: v.nr, tag_rows: v.tr, default_namespace: v.defaults.ns, default_wave: parseInt(v.defaults.wave, 10) || 1 };
}
const mapsSig = (v) => JSON.stringify([v.fr, v.nr, v.tr]);
const rowsOf = (v, m) => (m === 'f' ? v.fr : m === 't' ? v.tr : v.nr);
function covTags(cov) {
  const tags = cov.tags || [];
  if (!tags.length) return html`<span class="small muted">The selected VMs carry no vCenter tags${S.info.features ? '' : ''} (or discovery skipped them).</span>`;
  return tags.map((x) => html`<div class="covrow ${x.pattern ? '' : 'dim'}">${icon('tag')}<span class="p mono" title="${x.tag}">${x.tag}</span>
    <span class="small muted">${plural(x.vms, 'VM')}</span><span class="arrow">→</span>
    ${x.pattern ? html`<span class="small"><span class="mono">${x.namespace || html`<span class="faint">no namespace</span>`}</span>${x.wave ? ' · wave ' + x.wave : ''}
      ${x.pattern !== x.tag ? html`<span class="tag" title="matched by ${x.pattern}">via ${x.pattern}</span>` : ''}</span>`
      : html`<span class="small faint">not mapped</span><button class="btn xs" data-act="addRow" data-m="t" data-key="${x.tag}">${icon('plus')} Map</button>`}</div>`);
}
async function runPreview(v) {
  try {
    v.preview = await POST('/api/stage/preview', stageBody(v));
    v.previewErr = null;
  } catch (e) { v.previewErr = e.message; }
  if (S.view !== v) return;
  mount($('#stage-preview'), stagePreview(v));
  mount($('#cov-folders'), covFolders(v.preview.coverage));
  mount($('#cov-networks'), covNetworks(v.preview.coverage));
  mount($('#cov-tags'), covTags(v.preview.coverage));
  const c = $('#stage-count');
  if (c) c.textContent = plural(v.preview.records.length, 'VM') + ' ready to stage';
  const d = $('#stage-dirty');
  if (d) mount(d, mapsSig(v) !== v.saved ? html`<span class="warn-text">The maps have unsaved edits — staging saves them.</span> Staging again later is safe.` : 'Maps saved. Staging again later is safe: VMs already in flight are never changed.');
}
const schedulePreview = debounce((v) => runPreview(v), 400);
function mapTable(v, m) {
  const rows = rowsOf(v, m);
  const cols = m === 'f' ? [['folder', 'Folder', ''], ['namespace', 'Namespace', 'ns-options'], ['wave', 'Wave', ''], ['group', 'Batch group', '']]
    : m === 't' ? [['tag', 'Tag (Category:Tag)', ''], ['namespace', 'Namespace', 'ns-options'], ['wave', 'Wave', ''], ['group', 'Batch group', '']]
    : [['portgroup', 'Portgroup', ''], ['subnet', 'Subnet', ''], ['namespace', 'Namespace', 'ns-options'], ['wave', 'Wave', '']]
      .concat(v.adv ? [['device_key', 'Device key', ''], ['subnet_kind', 'Kind', ''], ['subnet_api_group', 'API group', '']] : []);
  if (!rows.length) return html`<div class="card-b small muted">No entries yet. Use <b>+ Map</b> next to a ${m === 'f' ? 'folder' : m === 't' ? 'tag' : 'network'} above, or add one.</div>`;
  return html`<div class="table-wrap" style="max-height:340px" data-scroll="map-${m}"><table class="t maptable"><thead><tr>${cols.map((c) => html`<th>${c[1]}</th>`)}<th></th></tr></thead><tbody>
    ${rows.map((r, i) => html`<tr>${cols.map(([k, , list]) => html`<td style="${k === 'wave' || k === 'device_key' ? 'width:70px' : ''}"><input class="input ${k === 'folder' || k === 'portgroup' || k === 'tag' ? 'mono' : ''}"
      id="${m}-${i}-${k}" data-input="cell" data-m="${m}" data-i="${i}" data-col="${k}" value="${r[k] || ''}" ${list ? raw(`list="${list}"`) : ''} ${k === 'wave' || k === 'device_key' ? raw('inputmode="numeric"') : ''}></td>`)}
      <td style="width:34px"><button class="btn ghost icon sm" data-act="delRow" data-m="${m}" data-i="${i}" title="Remove">${icon('x')}</button></td></tr>`)}</tbody></table></div>`;
}
function covFolders(cov) {
  if (!cov.folders.length) return html`<span class="small muted">—</span>`;
  return cov.folders.map((f) => {
    const mapped = f.pattern !== null;
    const miss = !mapped && f.explicit_namespace < f.vms;
    return html`<div class="covrow ${miss ? 'miss' : ''}">${icon('folder')}<span class="p" title="${f.folder}">${f.folder || '(datacenter root)'}</span>
      <span class="small muted">${plural(f.vms, 'VM')}</span><span class="arrow">→</span>
      ${mapped ? html`<span class="small"><span class="mono">${f.namespace || html`<span class="faint">no namespace</span>`}</span>${f.wave ? ' · wave ' + f.wave : ''}
        ${f.pattern !== f.folder ? html`<span class="tag" title="matched by ${f.pattern}">via ${f.pattern}</span>` : ''}</span>`
        : (f.explicit_namespace ? html`<span class="small muted">${f.explicit_namespace} set in Select</span>` : html`<span class="small bad-text">unmapped</span>`)}
      ${(mapped && f.pattern === f.folder) || !f.folder ? '' : html`<button class="btn xs" data-act="addRow" data-m="f" data-key="${f.folder}">${icon('plus')} Map</button>`}</div>`;
  });
}
function covNetworks(cov) {
  if (!cov.networks.length) return html`<span class="small muted">The selected VMs have no network adapters.</span>`;
  return cov.networks.map((x) => html`<div class="covrow ${x.mapped ? '' : 'miss'}"><span class="p mono">${x.network}</span>
    <span class="small muted">${plural(x.vms, 'VM')}</span><span class="arrow">→</span>
    ${x.mapped ? html`<span class="small mono">${x.subnet || html`<span class="faint">no subnet</span>`}</span>` : html`<span class="small bad-text">unmapped</span>`}
    ${x.mapped ? '' : html`<button class="btn xs" data-act="addRow" data-m="n" data-key="${x.network}">${icon('plus')} Map</button>`}</div>`);
}
function stagePreview(v) {
  const p = v.preview;
  if (v.previewErr) return html`<div class="card-b"><div class="note bad">${v.previewErr}</div></div>`;
  const recs = p.records.slice(v.recPage * 50, (v.recPage + 1) * 50);
  return html`<div class="card-b stack">
    <div class="kpis">${kpi('Selected', p.selected, '')}${kpi('Stageable', p.records.length, '', null, 'var(--ok)')}
      ${kpi('Cannot place', p.problems.length, 'no namespace', null, p.problems.length ? 'var(--bad)' : '')}
      ${kpi('Unmapped networks', Object.keys(p.unmapped_networks).length, 'VMs keep no subnet for them', null, Object.keys(p.unmapped_networks).length ? 'var(--warn)' : '')}</div>
    ${p.by_wave.length ? html`<div class="row wrap"><span class="small muted">By wave</span>${p.by_wave.map(([w, c]) => html`<span class="chip static">Wave ${w} <span class="n">${n(c)}</span></span>`)}
      <span class="small muted" style="margin-left:14px">By namespace</span>${p.by_namespace.map(([ns, c]) => html`<span class="chip static mono">${ns} <span class="n">${n(c)}</span></span>`)}</div>` : ''}
    ${p.problems.length ? html`<details class="note bad"><summary><b>${plural(p.problems.length, 'VM')} cannot be staged</b> — expand for details</summary><ul class="plain small">${p.problems.slice(0, 60).map((x) => html`<li>${x}</li>`)}</ul></details>` : ''}
    ${(p.app_moves || []).length ? html`<details class="note info"><summary><b>${plural(p.app_moves.length, 'application')} aligned to one wave</b> — apps are kept together</summary><ul class="plain small">${p.app_moves.slice(0, 40).map((x) => html`<li>${x}</li>`)}</ul></details>` : ''}
  </div>
  ${p.records.length ? html`<div class="table-wrap" style="border-top:1px solid var(--line)"><table class="t compact"><thead><tr><th>VM</th><th>Folder</th><th>App</th><th>Namespace</th><th>Wave</th><th>Subnets</th><th>Notes</th><th>Queue</th></tr></thead><tbody>
    ${recs.map((r) => html`<tr><td><span class="vm-name">${r.vm_name}<small>${r.moref}</small></span></td><td class="small">${r.folder || '/'}</td><td>${appTag(r.app)}</td><td class="mono small">${r.namespace}</td>
      <td class="num">${r.wave}</td><td class="mono small">${r.subnets.join(', ') || html`<span class="warn-text">none</span>`}</td><td class="small muted">${r.notes}</td>
      <td>${r.queue_state ? html`${pill(r.queue_state)}${r.locked ? html` <span class="tag" title="In flight or done: staging will not change it">locked</span>` : ''}` : html`<span class="tag info">new</span>`}</td></tr>`)}</tbody></table></div>
    ${pager(p.records.length, v.recPage, 50)}` : ''}`;
}

// ==================================================================== WAVES
VIEWS.waves = view({
  title: 'Waves', sub: 'Step 4 · order the queue: wave 1 runs first; drag VMs or whole folders between waves',
  init(v) { v.sel = new Set(); v.open = v.open || new Set(); v.q = ''; if (v.withApp === undefined) v.withApp = !!(S.info.features || {}).app_together; },
  async load(v) {
    const [d, o] = await Promise.all([GET('/api/vms'), GET('/api/overview')]);
    v.vms = d.vms; v.o = o; v.byMoref = new Map(v.vms.map((x) => [x.moref, x]));
    S.waves = uniq(v.vms.map((x) => x.wave)).sort((a, b) => a - b);
  },
  paint(v) {
    if (!v.vms.length) return emptyState('waves', 'The import queue is empty', 'Stage selected VMs first; they land in the waves the maps decide, and you arrange them here.', html`<button class="btn primary" data-act="go" data-to="stage">Map & Stage</button>`);
    const waves = S.waves;
    const q = v.q.trim().toLowerCase();
    const hits = q ? v.vms.filter((x) => (x.vm_name + ' ' + x.moref + ' ' + (x.folder || '')).toLowerCase().includes(q)) : [];
    const hitSet = new Set(hits.map((x) => x.moref));
    const selVms = Array.from(v.sel).map((m) => v.byMoref.get(m)).filter(Boolean);
    const splits = v.o.app_splits || [];
    const hasApps = v.vms.some((x) => x.app);
    return html`
      ${splits.length ? html`<div class="callout warn mb"><div class="ic">${icon('waves')}</div><div class="grow"><h3>${plural(splits.length, 'application')} split across waves</h3>
        <div class="row wrap mt-s" style="gap:8px">${splits.slice(0, 8).map((a) => html`<span class="chip static">${appTag(a.app)} waves ${Object.keys(a.waves).join(', ')}
          <button class="btn xs" data-act="pullApp" data-app="${a.app}">Pull into wave ${Math.min(...Object.keys(a.waves).map(Number))}</button></span>`)}</div></div></div>` : ''}
      <div class="card mb"><div class="toolbar" style="border:0">
        <div class="search">${icon('search')}<input class="input" id="wave-q" placeholder="Find a VM or folder…" value="${v.q}" data-input="q"></div>
        ${q ? html`<span class="small muted">${plural(hits.length, 'match', 'matches')}</span>` : ''}
        <button class="btn sm ghost" data-act="expand" data-on="1">Expand all</button><button class="btn sm ghost" data-act="expand" data-on="0">Collapse all</button>
        ${hasApps ? html`<label class="check small" title="Moving one VM of an application moves all of it"><input type="checkbox" data-change="withApp" ${attr(v.withApp, 'checked')}> Move whole apps</label>${tip('app_together')}` : ''}
        <span class="grow"></span>
        ${v.sel.size ? html`<span class="small"><b>${n(v.sel.size)}</b> selected</span>
          <button class="btn sm primary" data-act="moveSel">${icon('waves')} Move to wave…</button>
          <button class="btn sm" data-act="clearSel">Clear</button>` : html`<span class="small muted">${icon('lock')} locked VMs are already in a batch or committed</span>${tip('locked')}`}
      </div></div>
      <div class="board" data-scroll="board">
        ${waves.map((w, idx) => waveColumn(v, w, idx, waves, hitSet, q))}
        <div class="wcol new" data-dropwave="${(waves[waves.length - 1] || 0) + 1}">
          <div>${icon('plus')}<div><b>New wave ${(waves[waves.length - 1] || 0) + 1}</b></div><div class="small">Drop VMs or folders here</div>
          ${selVms.length ? html`<button class="btn sm mt" data-act="moveNew">Move ${n(selVms.length)} selected here</button>` : ''}</div></div>
      </div>`;
  },
  onJobDone(v) { v.refresh(); },
  act: {
    q: debounce((t, e, v) => { v.q = t.value; v.render(); }, 200),
    expand(t, e, v) {
      if (t.dataset.on === '1') v.vms.forEach((x) => v.open.add(x.wave + '|' + (x.folder || ''))); else v.open.clear();
      v.render();
    },
    toggleGroup(t, e, v) { if (e.target.closest('input')) return; const k = t.dataset.key; if (v.open.has(k)) v.open.delete(k); else v.open.add(k); v.render(); },
    pick(t, e, v) { const m = t.dataset.moref; if (t.checked) v.sel.add(m); else v.sel.delete(m); v.render(); },
    pickGroup(t, e, v) {
      const members = v.vms.filter((x) => x.wave === +t.dataset.wave && (x.folder || '') === t.dataset.folder && !x.locked);
      members.forEach((x) => (t.checked ? v.sel.add(x.moref) : v.sel.delete(x.moref)));
      v.render();
    },
    clearSel(t, e, v) { v.sel.clear(); v.render(); },
    withApp(t, e, v) { v.withApp = t.checked; },
    async pullApp(t, e, v) {
      const app = t.dataset.app;
      const members = v.vms.filter((x) => x.app === app);
      const wave = Math.min(...members.map((x) => x.wave));
      await moveTo(v, members.filter((x) => !x.locked).map((x) => x.moref), wave, true);
    },
    async moveSel(t, e, v) {
      const morefs = Array.from(v.sel);
      if (await doMoveWave(morefs)) { v.sel.clear(); await v.refresh(); }
    },
    async moveNew(t, e, v) { await moveTo(v, Array.from(v.sel), (S.waves[S.waves.length - 1] || 0) + 1); },
    async swap(t, e, v) {
      const a = +t.dataset.a, b = +t.dataset.b;
      try { await POST('/api/waves/swap', { a, b }); toast('Wave ' + a + ' and wave ' + b + ' swapped', 'Wave ' + Math.min(a, b) + ' now runs first.', 'ok'); } catch (err) { fail(err); }
      await v.refresh();
    },
  },
  dnd: {
    dragstart(e, v) {
      const el = e.target.closest && e.target.closest('[data-drag]');
      if (!el) return;
      let morefs;
      if (el.dataset.drag === 'vm') {
        const m = el.dataset.moref;
        morefs = v.sel.has(m) ? Array.from(v.sel) : [m];
      } else {
        morefs = v.vms.filter((x) => x.wave === +el.dataset.wave && (x.folder || '') === el.dataset.folder && !x.locked).map((x) => x.moref);
      }
      v.drag = morefs;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', morefs.join(','));
      el.classList.add('dragging');
    },
    dragover(e, v) {
      const col = e.target.closest && e.target.closest('[data-dropwave]');
      if (!col || !v.drag) return;
      e.preventDefault();
      $$('.wcol.drop').forEach((c) => c !== col && c.classList.remove('drop'));
      col.classList.add('drop');
    },
    dragleave(e) {
      const col = e.target.closest && e.target.closest('[data-dropwave]');
      if (col && !col.contains(e.relatedTarget)) col.classList.remove('drop');
    },
    async drop(e, v) {
      const col = e.target.closest && e.target.closest('[data-dropwave]');
      if (!col || !v.drag) return;
      e.preventDefault();
      const morefs = v.drag;
      v.drag = null;
      $$('.wcol.drop').forEach((c) => c.classList.remove('drop'));
      await moveTo(v, morefs, +col.dataset.dropwave);
    },
    dragend(e, v) { v.drag = null; $$('.dragging').forEach((x) => x.classList.remove('dragging')); $$('.wcol.drop').forEach((c) => c.classList.remove('drop')); },
  },
});
async function moveTo(v, morefs, wave, withApp) {
  const moving = morefs.filter((m) => { const x = v.byMoref.get(m); return x && x.wave !== wave; });
  if (!moving.length) return;
  try {
    const r = await POST('/api/vms/wave', { morefs: moving, wave, with_app: withApp === undefined ? !!v.withApp : withApp });
    toast('Moved ' + plural(r.moved, 'VM') + ' to wave ' + wave,
      (r.pulled_with_app ? plural(r.pulled_with_app, 'app member') + ' came along. ' : '') + (r.refused.length ? r.refused.length + ' locked: ' + r.refused.slice(0, 2).join('; ') : ''), r.refused.length ? 'warn' : 'ok');
    v.sel.clear();
  } catch (err) { fail(err); }
  await v.refresh();
}
function waveColumn(v, w, idx, waves, hitSet, q) {
  const members = v.vms.filter((x) => x.wave === w);
  const counts = countBy(members, 'state');
  const locked = members.filter((x) => x.locked).length;
  const groups = {};
  members.forEach((x) => { (groups[x.folder || ''] = groups[x.folder || ''] || []).push(x); });
  const nss = uniq(members.map((x) => x.namespace));
  const prev = waves[idx - 1], next = waves[idx + 1];
  const e = ((v.o || {}).estimates || {})[w];
  const est = e ? (counts.pending || counts.precheck_failed ? eta(e.precheck.seconds + e.import.seconds) : e.import.seconds ? eta(e.import.seconds) : '') : '';
  return html`<div class="wcol" data-dropwave="${w}">
    <div class="wcol-h">
      <div class="title"><span class="wave-no">${w}</span><h3>Wave ${w}</h3><span class="small muted">${plural(members.length, 'VM')}</span>
        <span class="grow"></span>
        <button class="btn ghost icon xs" data-act="swap" data-a="${w}" data-b="${prev}" ${attr(prev === undefined, 'disabled')} title="Run earlier (swap with wave ${prev})">${icon('left')}</button>
        <button class="btn ghost icon xs" data-act="swap" data-a="${w}" data-b="${next}" ${attr(next === undefined, 'disabled')} title="Run later (swap with wave ${next})">${icon('right')}</button></div>
      ${segbar(counts, members.length)}
      <div class="row wrap tiny muted">${STATE_ORDER.filter((s) => counts[s]).map((s) => html`<span>${dot(s)} ${n(counts[s])} ${label(s)}</span>`)}</div>
      <div class="row wrap tiny">${nss.slice(0, 3).map((x) => html`<span class="tag mono">${x}</span>`)}${nss.length > 3 ? html`<span class="tag">+${nss.length - 3}</span>` : ''}
        ${est ? html`<span class="faint eta" title="estimated time for what is left in this wave">${icon('clock')} ${est}</span>` : ''}</div>
      <div class="row"><button class="btn xs" data-act="go" data-to="execute" data-q="stage=precheck&waves=${w}" ${attr(!counts.pending, 'disabled')}>Precheck</button>
        <button class="btn xs" data-act="go" data-to="execute" data-q="stage=import&waves=${w}" ${attr(!counts.precheck_passed, 'disabled')}>Import</button>
        ${locked ? html`<span class="tiny faint">${icon('lock')} ${n(locked)} locked</span>` : ''}</div>
    </div>
    <div class="wcol-b" data-scroll="wave-${w}">${Object.keys(groups).sort().map((folder) => {
      const g = groups[folder], key = w + '|' + folder;
      const gHits = q ? g.filter((x) => hitSet.has(x.moref)).length : 0;
      const open = v.open.has(key) || (q && gHits > 0);
      if (q && !gHits && !folder.toLowerCase().includes(q)) return '';
      const movable = g.filter((x) => !x.locked);
      const selN = movable.filter((x) => v.sel.has(x.moref)).length;
      return html`<div class="fgroup">
        <div class="fgroup-h" data-act="toggleGroup" data-key="${key}" draggable="${movable.length ? 'true' : 'false'}" data-drag="group" data-wave="${w}" data-folder="${folder}" title="${folder || '(datacenter root)'} — drag to move the whole folder">
          <span class="caret ${open ? 'open' : ''}" style="display:grid">${icon('chevron')}</span>
          <input type="checkbox" data-act="pickGroup" data-wave="${w}" data-folder="${folder}" ${attr(selN && selN === movable.length, 'checked')} ${attr(!movable.length, 'disabled')}>
          ${icon('folder')}<span class="name">${folder || '(root)'}</span><span class="tiny muted">${n(g.length)}</span></div>
        ${open ? html`<div class="fgroup-b">${g.map((x) => html`<div class="vmrow ${x.locked ? 'locked' : ''} ${hitSet.has(x.moref) ? 'hit' : ''}" ${x.locked ? '' : raw(`draggable="true" data-drag="vm" data-moref="${esc(x.moref)}"`)}>
          <input type="checkbox" data-act="pick" data-moref="${x.moref}" ${attr(v.sel.has(x.moref), 'checked')} ${attr(x.locked, 'disabled')}>
          ${dot(x.state)}<span class="name" title="${x.vm_name} · ${x.moref} · ${label(x.state)}${x.app ? ' · app ' + x.app : ''}">${x.vm_name}</span>${x.app ? html`<span class="tag app xs">${x.app}</span>` : ''}
          ${x.locked ? icon('lock', 'lock') : ''}<a class="tiny" data-act="vm" data-moref="${x.moref}">details</a></div>`)}</div>` : ''}
      </div>`;
    })}</div></div>`;
}

// ================================================================== EXECUTE
VIEWS.execute = view({
  title: 'Execute', sub: 'Step 5 · preflight, precheck, import, commit',
  live: true,
  init(v, p) {
    v.o = null;
    v.opt = {
      stage: p.stage || null, waves: new Set(p.waves ? p.waves.split(',').map(Number) : []), folder: p.folder || '', folder_exact: false,
      include_failed: false, limit: '', batch_size: '', parallel: '', dry_run: false, no_precheck: false, rollback_failed: false,
    };
    v.skipTargets = false;
    v.adv = false;
  },
  async load(v) {
    const [o, jobs, vms] = await Promise.all([GET('/api/overview'), GET('/api/jobs'), v.folders ? null : GET('/api/vms')]);
    v.o = o;
    S.waves = o.waves.map((w) => w.wave);
    if (vms) v.folders = uniq(vms.vms.map((x) => x.folder)).sort();
    v.preflight = jobs.jobs.find((j) => j.kind === 'preflight' && j.status !== 'running' && j.result) || null;
    if (!v.opt.stage) v.opt.stage = o.counts.precheck_passed ? 'import' : 'precheck';
    v.plan = await POST('/api/execute/preview', execBody(v));
    if (!v.opt.waves.size && !v.touched) {
      const first = Object.entries(v.plan.eligible_by_wave).find(([, c]) => c > 0);
      if (first) { v.opt.waves.add(+first[0]); v.plan = await POST('/api/execute/preview', execBody(v)); }
    }
  },
  paint(v) {
    const o = v.o, c = o.counts, opt = v.opt, s = S.info.settings, pl = v.plan;
    const job = S.pulse && S.pulse.job;
    const imp = opt.stage === 'import';
    const pf = v.preflight;
    const pfState = !pf ? '' : pf.result.ok ? 'done' : 'warn';
    const pre = (c.precheck_passed || 0) + (c.importing || 0) + (c.awaiting_commit || 0) + (c.committed || 0);
    const steps = [
      [pfState, 'Preflight', !pf ? 'Not run in this workspace yet' : (pf.result.ok ? 'Passed ' + fmtAgo(pf.finished_at) : plural(pf.result.problems.length, 'problem') + ' · ' + fmtAgo(pf.finished_at))],
      [c.precheck_failed ? 'warn' : (c.pending ? (pre ? 'now' : '') : (o.total ? 'done' : '')), 'Precheck', n(c.precheck_passed || 0) + ' passed · ' + n(c.precheck_failed || 0) + ' failed · ' + n(c.pending || 0) + ' pending'],
      [c.failed ? 'bad' : (o.total && c.committed === o.total ? 'done' : (c.precheck_passed ? 'now' : '')), 'Import', n(c.committed || 0) + ' committed · ' + n(c.precheck_passed || 0) + ' ready · ' + n(c.failed || 0) + ' failed'],
      [c.awaiting_commit ? 'warn' : '', 'Commit', s.commit_action === 'Auto' ? 'Automatic (commitAction Auto)' : n(c.awaiting_commit || 0) + ' held for approval'],
    ];
    return html`
      <div class="steps">${steps.map(([st, t, d], i) => html`<div class="step ${st}"><span class="n">${st === 'done' ? icon('check') : i + 1}</span><div><h4>${t} ${tip(['preflight', 'precheck', 'import', 'commit'][i])}</h4><p>${d}</p></div></div>`)}</div>
      <div class="mt">${FX.streamHtml(o, { compact: true })}</div>
      ${job && ['execute', 'rollback', 'watch', 'commit'].includes(job.kind) ? html`<div class="callout mt"><div class="ic"><span class="spinner"></span></div>
        <div class="grow"><h3>${job.title}</h3><p>${job.stop_requested ? 'Stopping: in-flight batches are being polled to completion.' : 'Running for ' + fmtDur(elapsed(job)) + '. Waves update live below; the full log is in the panel at the bottom.'}</p></div>
        <button class="btn" data-act="showJob" data-id="${job.id}">Log</button>
        ${job.stoppable ? html`<button class="btn danger-ghost" data-act="stopJob" data-id="${job.id}" ${attr(job.stop_requested, 'disabled')}>${icon('stop')} Stop</button>` : ''}</div>` : ''}
      <div class="grid mt" style="grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);align-items:start">
        <div class="card"><div class="card-h"><h3>Run</h3>
          <div class="tools"><div class="seg"><button class="${!imp ? 'on' : ''}" data-act="stage" data-k="precheck">Precheck</button><button class="${imp ? 'on' : ''}" data-act="stage" data-k="import">Import</button></div></div></div>
          <div class="card-b stack">
            <div class="note ${imp ? 'warn' : 'info'}">${imp
              ? html`<b>Import</b> migrates VMs that passed precheck${opt.no_precheck ? html` <b>-- and, with the gate off, pending VMs too</b>` : ''}. ${s.commit_action === 'Auto' ? html`With <b>commitAction Auto</b> a successful import commits at once and <b>cannot be handed back to vCenter</b>.` : html`With <b>commitAction Wait</b> each batch holds at the commit gate for your approval.`}`
              : html`<b>Precheck</b> applies <code>precheckOnly</code> batches: the operator validates each VM against the target without migrating anything. Safe to repeat.`}</div>
            <div><div class="section-title" style="margin-top:0">Waves ${tip('wave')}</div><div class="chips">
              <span class="chip ${opt.waves.size ? '' : 'on'}" data-act="wave" data-w="all">All waves <span class="n">${n(Object.values(pl.eligible_by_wave).reduce((a, b) => a + b, 0))}</span></span>
              ${Object.entries(pl.eligible_by_wave).map(([w, cnt]) => html`<span class="chip ${opt.waves.has(+w) ? 'on' : ''}" data-act="wave" data-w="${w}">Wave ${w} <span class="n">${n(cnt)}</span></span>`)}
            </div><div class="tiny faint mt-s">Counts are VMs eligible for ${opt.stage} in each wave. Waves run one after another, lowest first.</div></div>
            <div class="form-grid">
              <label class="field"><span>Folder scope (optional) ${tip('folder_scope')}</span><input class="input" id="ex-folder" list="ex-folders" data-input="opt" data-k="folder" value="${opt.folder}" placeholder="every folder">
                <datalist id="ex-folders">${(v.folders || []).map((f) => html`<option value="${f}">`)}</datalist></label>
              <label class="check" style="align-self:end;padding-bottom:8px"><input type="checkbox" data-change="opt" data-k="folder_exact" ${attr(opt.folder_exact, 'checked')}> That folder only, not its subfolders</label>
            </div>
            <details ${attr(v.adv, 'open')} data-toggle="adv"><summary class="small" style="cursor:pointer;font-weight:600">Advanced options</summary>
              <div class="form-grid mt-s">
                <label class="field"><span>Batch size ${tip('batch_size')}</span><input class="input" id="ex-bs" type="number" min="1" data-input="opt" data-k="batch_size" value="${opt.batch_size}" placeholder="${s.batch_size}"></label>
                <label class="field"><span>Parallel batches ${tip('parallel')}</span><input class="input" id="ex-par" type="number" min="1" data-input="opt" data-k="parallel" value="${opt.parallel}" placeholder="${s.max_parallel_batches}"></label>
                <label class="field"><span>Max VMs per wave ${tip('limit')}</span><input class="input" id="ex-lim" type="number" min="0" data-input="opt" data-k="limit" value="${opt.limit}" placeholder="no limit"></label>
              </div>
              <div class="stack mt-s" style="gap:8px">
                <label class="check"><input type="checkbox" data-change="opt" data-k="include_failed" ${attr(opt.include_failed, 'checked')}> Also retry VMs that ${imp ? 'failed import' : 'failed precheck'} ${tip('include_failed')}</label>
                <label class="check"><input type="checkbox" data-change="opt" data-k="dry_run" ${attr(opt.dry_run, 'checked')}> Dry run — render manifests, apply nothing ${tip('dry_run')}</label>
                ${imp ? html`<label class="check"><input type="checkbox" data-change="opt" data-k="rollback_failed" ${attr(opt.rollback_failed, 'checked')}> After the run, hand failed imports back to vCenter ${tip('rollback_failed')}</label>
                  <label class="check"><input type="checkbox" data-change="opt" data-k="no_precheck" ${attr(opt.no_precheck, 'checked')}> <span class="warn-text">Import VMs that have not passed precheck</span> ${tip('no_precheck')}</label>` : ''}
              </div></details>
            <div id="ex-plan">${planTable(v)}</div>
          </div>
          <div class="card-f" id="ex-go">${goButton(v)}</div></div>
        <div class="stack">
          <div class="card"><div class="card-h"><h3>Preflight ${tip('preflight')}</h3><span class="sub">context, CRDs, operator, namespaces, RBAC, subnets</span></div>
            <div class="card-b">${pf ? preflightResult(pf) : html`<p class="muted small" style="margin:0">Checks the cluster before anything is applied. Read-only.</p>`}</div>
            <div class="card-f"><button class="btn ${pf && pf.result.ok ? '' : 'primary'}" data-act="preflight" ${attr(!!job, 'disabled')}>${icon('shield')} Run preflight</button>
              <label class="check small"><input type="checkbox" data-change="skipTargets" ${attr(v.skipTargets, 'checked')}> skip namespace/subnet checks</label>${tip('skip_targets')}</div></div>
          ${c.awaiting_commit || s.commit_action === 'Wait' ? html`<div class="card"><div class="card-h"><h3>Commit gate ${tip('commit')}</h3><span class="sub">commitAction ${s.commit_action}</span></div>
            <div class="card-b">${c.awaiting_commit ? html`<p style="margin:0"><b>${plural(c.awaiting_commit, 'VM')}</b> imported and held. Commit to finish, or roll back to hand them to vCenter.</p>`
              : html`<p class="muted small" style="margin:0">Nothing is waiting for approval.</p>`}</div>
            <div class="card-f"><button class="btn primary" data-act="commit" ${attr(!c.awaiting_commit || !!job, 'disabled')}>${icon('commit')} Commit ${n(c.awaiting_commit || 0)}</button>
              <button class="btn danger-ghost" data-act="rollbackHeld" ${attr(!c.awaiting_commit || !!job, 'disabled')}>${icon('rollback')} Roll back instead</button></div></div>` : ''}
          ${c.committed ? html`<div class="card"><div class="card-h"><h3>Post-import verification ${tip('verification')}</h3><span class="sub">power, Tools, IP kept, ping${(o.verify_ports || []).length ? ', ports' : ''}</span></div>
            <div class="card-b">${o.verify ? html`<div class="row wrap" style="gap:14px"><span>${verifyTag('ok')} <b>${n(o.verify.ok || 0)}</b></span><span>${verifyTag('fail')} <b>${n(o.verify.fail || 0)}</b></span>
              <span>${verifyTag('warn')} <b>${n(o.verify.warn || 0)}</b></span><span class="muted">not checked <b>${n(o.verify.none || 0)}</b></span></div>` : ''}
              <p class="small muted mb-0">${S.info.features.verify_after_import ? 'Runs automatically after each import.' : 'Turn on "Verify after import" in Settings to run it after every import.'}
              ${S.info.features.vcenter_remembered || S.info.vcenter.password_from_env ? '' : ' Without a vCenter session only ping and port checks run: tick "keep credentials" on Discover.'}</p></div>
            <div class="card-f"><button class="btn" data-act="verify" data-unverified="1" ${attr(!!job || !(o.verify && o.verify.none), 'disabled')}>${icon('check')} Verify ${n((o.verify && o.verify.none) || 0)} unchecked</button>
              <button class="btn ghost" data-act="verify" ${attr(!!job, 'disabled')}>Re-verify all</button></div></div>` : ''}
          <div class="card"><div class="card-h"><h3>On the cluster</h3><span class="sub">${plural(o.live_batches.length, 'live batch', 'live batches')}</span></div>
            ${o.live_batches.length ? html`<div class="table-wrap"><table class="t compact"><tbody>${o.live_batches.slice(0, 8).map((b) => html`<tr class="click" data-act="batch" data-ns="${b.namespace}" data-name="${b.name}">
              <td class="mono small">${b.name}</td><td>${batchTag(b.state)}</td><td class="msg"><div>${b.message || ''}</div></td></tr>`)}</tbody></table></div>`
              : html`<div class="card-b small muted">Nothing in flight.</div>`}
            <div class="card-f"><button class="btn sm" data-act="watch" ${attr(!!job || !o.live_batches.length, 'disabled')}>${icon('refresh')} Watch until done</button>${tip('watch')}
              <button class="btn sm ghost" data-act="refreshCluster" ${attr(!!job, 'disabled')}>Refresh once</button>${tip('refresh')}</div></div>
        </div></div>
      <div class="card mt"><div class="card-h"><h3>Waves</h3><span class="sub">live</span></div>
        ${o.waves.length ? html`<div class="table-wrap"><table class="t"><tbody>${o.waves.map((w) => html`<tr><td style="width:90px"><b>Wave ${w.wave}</b></td><td>${segbar(w.counts, w.total)}</td>
          <td class="small muted nowrap" style="width:40%">${STATE_ORDER.filter((x) => w.counts[x]).map((x) => html`<span style="margin-right:10px">${dot(x)} ${n(w.counts[x])} ${label(x)}</span>`)}</td></tr>`)}</tbody></table></div>`
          : html`<div class="card-b muted">The queue is empty.</div>`}</div>`;
  },
  onJobDone(v) { v.refresh(); },
  act: {
    stage(t, e, v) { v.opt.stage = t.dataset.k; v.opt.waves.clear(); v.touched = false; v.refresh(); },
    wave(t, e, v) {
      v.touched = true;
      const w = t.dataset.w;
      if (w === 'all') v.opt.waves.clear(); else if (v.opt.waves.has(+w)) v.opt.waves.delete(+w); else v.opt.waves.add(+w);
      replan(v, true);
    },
    opt(t, e, v) {
      v.opt[t.dataset.k] = t.type === 'checkbox' ? t.checked : t.value;
      if (t.type === 'checkbox') replan(v, true); else replanSoon(v);
    },
    adv(t, e, v) { v.adv = t.open; },
    skipTargets(t, e, v) { v.skipTargets = t.checked; },
    preflight: (t, e, v) => startJob('preflight', { skip_targets: v.skipTargets }),
    watch: () => startJob('watch', {}),
    refreshCluster: () => startJob('refresh', {}),
    commit: (t, e, v) => doCommit({}, v.o.counts.awaiting_commit),
    rollbackHeld: () => doRollback({ failed: true }),
    schedule(t, e, v) { go('schedule', qs({ stage: v.opt.stage, waves: Array.from(v.opt.waves).join(',') })); },
    async start(t, e, v) {
      await replan(v, false);
      const pl = v.plan, opt = v.opt, imp = opt.stage === 'import', s = pl.settings;
      if (!pl.total) { toast('Nothing to run', 'No VM in the chosen waves is eligible for ' + opt.stage + '.', 'warn'); return; }
      const risky = imp && !opt.dry_run;
      const ask = pl.needs_approval && !opt.dry_run;
      const ok = await modal({
        title: (ask ? 'Request approval: ' : '') + (opt.dry_run ? 'Dry run: ' : '') + (imp ? 'Import ' : 'Precheck ') + plural(pl.total, 'VM') + '?',
        danger: risky && !ask, ic: ask ? 'shield' : imp ? 'execute' : 'shield', confirm: ask ? 'Request approval' : opt.dry_run ? 'Start dry run' : (imp ? 'Import' : 'Start precheck'),
        typeWord: risky && !ask ? 'IMPORT' : null, wide: true,
        body: html`${ask ? html`<div class="note warn mb"><b>Two-person rule.</b> This sends a request: someone other than you approves it under Change control, and it starts at that moment.</div>` : ''}${planTable(v)}
          <dl class="facts mt"><dt>Context</dt><dd>${s.context || '(kubectl current-context)'}</dd><dt>Batching</dt><dd>${s.batch_size} VMs per batch · ${s.max_parallel_batches} in parallel (${s.max_parallel_batches_per_namespace} per namespace)</dd>
            ${imp ? html`<dt>commitAction</dt><dd>${s.commit_action}</dd>` : ''}${opt.folder ? html`<dt>Scope</dt><dd>${opt.folder}${opt.folder_exact ? ' (no subfolders)' : ' and subfolders'}</dd>` : ''}</dl>
          ${risky && s.commit_action === 'Auto' ? html`<div class="note bad mt"><b>Irreversible.</b> Imported VMs commit automatically and cannot be handed back to vCenter. Failed imports can still be rolled back.</div>` : ''}
          <p class="small muted">You can stop at any time: no new batches are applied, and what is already on the cluster is polled to completion. A circuit breaker halts the run if ${Math.round(S.info.settings.failure_rate_abort * 100)}% of a wave fails.</p>`,
      });
      if (!ok) return;
      await startJob('execute', Object.assign(execBody(v), { confirm: true }));
    },
  },
});
function execBody(v) {
  const o = v.opt;
  return {
    stage: o.stage, waves: Array.from(o.waves).sort((a, b) => a - b), folders: o.folder ? [o.folder] : null, folder_exact: o.folder_exact,
    include_failed: o.include_failed, limit: o.limit || 0, batch_size: o.batch_size || null, parallel: o.parallel || null,
    dry_run: o.dry_run, no_precheck: o.stage === 'import' && o.no_precheck, rollback_failed: o.stage === 'import' && o.rollback_failed,
  };
}
async function replan(v, full) {
  try { v.plan = await POST('/api/execute/preview', execBody(v)); v.planErr = null; } catch (e) { v.planErr = e.message; }
  if (S.view !== v) return;
  if (full) { v.render(); return; }
  mount($('#ex-plan'), planTable(v));
  mount($('#ex-go'), goButton(v));
}
const replanSoon = debounce((v) => replan(v, false), 350);
function planTable(v) {
  if (v.planErr) return html`<div class="note bad">${v.planErr}</div>`;
  const pl = v.plan;
  if (!pl.plan.length) {
    const why = v.opt.stage === 'import' && S.info.settings.require_precheck && !v.opt.no_precheck
      ? 'Import only takes VMs that passed precheck. Precheck first, or choose other waves.' : 'No VM in the chosen waves and scope is eligible.';
    return html`<div class="note">${why}</div>`;
  }
  const basis = pl.plan[0] && pl.plan[0].basis === 'measured' ? 'measured batch times from this campaign' : 'a default batch time, until batches have run here';
  return html`<table class="t compact" style="border:1px solid var(--line);border-radius:8px"><thead><tr><th>Wave</th><th class="right">VMs</th><th class="right">Batches</th><th>Namespaces</th><th class="right">Time ${tip('eta')}</th></tr></thead><tbody>
    ${pl.plan.map((p) => html`<tr><td><b>${p.wave}</b></td><td class="right num">${n(p.vms)}${p.vms !== p.eligible ? html` <span class="faint">of ${n(p.eligible)}</span>` : ''}</td><td class="right num">${n(p.batches)}</td>
      <td class="small mono">${p.namespaces.join(', ')}</td><td class="right small nowrap">${eta(p.seconds)}</td></tr>`)}
    <tr><td><b>Total</b></td><td class="right num"><b>${n(pl.total)}</b></td><td class="right num"><b>${n(pl.plan.reduce((a, p) => a + p.batches, 0))}</b></td><td></td><td class="right nowrap"><b>${eta(pl.seconds)}</b></td></tr></tbody></table>
    <div class="tiny faint mt-s">Times use ${basis}; waves run one after another.</div>
    ${(pl.holdbacks || []).length ? html`<div class="note warn mt-s"><b>Held back:</b><ul class="plain">${pl.holdbacks.map((h) => html`<li>${h}</li>`)}</ul></div>` : ''}`;
}
function goButton(v) {
  const pl = v.plan, imp = v.opt.stage === 'import', busy = !!(S.pulse && S.pulse.job);
  const ask = pl && pl.needs_approval && !v.opt.dry_run;
  const label2 = (ask ? 'Request approval: ' : '') + (v.opt.dry_run ? 'Dry run ' : '') + (imp ? 'Import ' : 'Precheck ') + plural(pl ? pl.total : 0, 'VM');
  return html`<button class="btn ${imp && !v.opt.dry_run && !ask ? 'danger' : 'primary'}" data-act="start" ${attr((busy && !ask) || !pl || !pl.total, 'disabled')}>${icon(ask ? 'shield' : 'execute')} ${label2}</button>
    <button class="btn ghost" data-act="schedule" ${attr(v.opt.dry_run, 'disabled')} title="Run it unattended in a change window">${icon('calendar')} Schedule…</button>
    <span class="small muted">${ask ? 'A second person approves it.' : busy ? 'Another job is running.' : 'You will review and confirm first.'}</span>`;
}
function preflightResult(pf) {
  const r = pf.result;
  return html`<div class="checks">${r.checks.map((c) => html`<div class="checkrow ${c.ok ? 'ok' : 'bad'}"><span class="ic">${icon(c.ok ? 'check' : 'x')}</span>
      <div><div>${c.name}</div><div class="d">${c.detail}</div></div></div>`)}</div>
    ${r.problems.length ? html`<div class="note bad mt-s"><ul class="plain">${r.problems.map((p) => html`<li style="white-space:pre-wrap">${p}</li>`)}</ul></div>` : ''}
    ${r.warnings.length ? html`<div class="note warn mt-s"><ul class="plain">${r.warnings.map((p) => html`<li>${p}</li>`)}</ul></div>` : ''}
    <div class="tiny faint mt-s">context ${r.context} · server ${r.server_version || '?'} · ${fmtTime(pf.finished_at, true)}</div>`;
}

// ==================================================================== QUEUE
VIEWS.queue = view({
  title: 'Import queue', sub: 'Every staged VM and where it stands', live: true,
  init(v, p) {
    v.f = { states: new Set(p.state ? p.state.split(',') : []), q: p.q || '', wave: p.wave || '', ns: p.ns || '', folder: '', app: p.app || '', verify: '' };
    v.sort = { key: 'wave', dir: 1 };
    v.page = 0;
    v.checked = new Set();
  },
  async load(v) { const d = await GET('/api/vms'); v.vms = d.vms; v.byMoref = new Map(v.vms.map((x) => [x.moref, x])); S.waves = uniq(v.vms.map((x) => x.wave)).sort((a, b) => a - b); },
  paint(v) {
    if (!v.vms.length) return emptyState('queue', 'The import queue is empty', 'Stage VMs from the Map & Stage step.', html`<button class="btn primary" data-act="go" data-to="stage">Map & Stage</button>`);
    const f = v.f, q = f.q.trim().toLowerCase();
    const base = v.vms.filter((x) => (!f.wave || String(x.wave) === f.wave) && (!f.ns || x.namespace === f.ns) && (!f.folder || inFolder(x.folder || '', f.folder)) &&
      (!f.app || x.app === f.app) && (!f.verify || (f.verify === 'none' ? x.state === 'committed' && !x.verify_state : x.verify_state === f.verify)) &&
      (!q || [x.vm_name, x.moref, x.message, x.batch_name, x.precheck_batch, x.folder, x.app].join(' ').toLowerCase().includes(q)));
    const apps = uniq(v.vms.map((x) => x.app)).sort();
    const counts = countBy(base, 'state');
    const rows = sortRows(base.filter((x) => !f.states.size || f.states.has(x.state)), v.sort.key, v.sort.dir);
    v.rows = rows;
    const pageRows = rows.slice(v.page * PAGE, (v.page + 1) * PAGE);
    const checked = Array.from(v.checked).map((m) => v.byMoref.get(m)).filter(Boolean);
    const allOn = pageRows.length && pageRows.every((x) => v.checked.has(x.moref));
    return html`<div class="card">
      <div class="toolbar">
        <div class="search">${icon('search')}<input class="input" id="q-q" placeholder="Search VM, moref, batch, message…" value="${f.q}" data-input="q"></div>
        <select class="select" data-change="filter" data-k="wave"><option value="">All waves</option>${S.waves.map((w) => html`<option value="${w}" ${attr(String(w) === f.wave, 'selected')}>Wave ${w}</option>`)}</select>
        <select class="select" data-change="filter" data-k="ns"><option value="">All namespaces</option>${uniq(v.vms.map((x) => x.namespace)).sort().map((x) => html`<option ${attr(x === f.ns, 'selected')}>${x}</option>`)}</select>
        <input class="input" id="q-folder" placeholder="Folder…" value="${f.folder}" data-input="folderF" style="width:160px">
        ${apps.length ? html`<select class="select" data-change="filter" data-k="app"><option value="">All apps</option>${apps.map((a) => html`<option ${attr(a === f.app, 'selected')}>${a}</option>`)}</select>` : ''}
        ${v.vms.some((x) => x.state === 'committed') ? html`<select class="select" data-change="filter" data-k="verify"><option value="">Any verification</option>${[['ok', 'Verified'], ['fail', 'Verify failed'], ['warn', 'Unverifiable'], ['none', 'Not checked']].map(([k, l]) => html`<option value="${k}" ${attr(f.verify === k, 'selected')}>${l}</option>`)}</select>` : ''}
      </div>
      <div class="toolbar thin">${savedViews('queue')}</div>
      <div class="toolbar"><div class="chips">
        <span class="chip ${f.states.size ? '' : 'on'}" data-act="st" data-s="">All <span class="n">${n(base.length)}</span></span>
        ${STATE_ORDER.filter((s) => counts[s]).map((s) => html`<span class="chip ${f.states.has(s) ? 'on' : ''}" data-act="st" data-s="${s}">${dot(s)} ${label(s)} <span class="n">${n(counts[s])}</span></span>`)}</div></div>
      ${checked.length ? html`<div class="bulkbar"><span><b>${n(checked.length)}</b> checked</span>
        <button class="btn sm" data-act="bRetry">${icon('refresh')} Retry</button>
        <button class="btn sm" data-act="bSkip">${icon('skip')} Skip</button>
        <button class="btn sm" data-act="bUnskip">Unskip</button>
        <button class="btn sm" data-act="bWave">${icon('waves')} Move wave</button>
        <button class="btn sm" data-act="bVerify" ${attr(!checked.some((x) => x.state === 'committed'), 'disabled')}>${icon('check')} Verify</button>
        <button class="btn sm danger-ghost" data-act="bRollback">${icon('rollback')} Roll back</button>
        <button class="btn sm danger-ghost" data-act="bAbandon">${icon('trash')} Abandon batches</button>
        <span class="grow"></span><button class="btn sm ghost" data-act="bClear">Clear</button></div>` : ''}
      <div class="table-wrap"><table class="t"><thead><tr>
        <th class="chk"><input type="checkbox" data-act="chkPage" ${attr(allOn, 'checked')}></th>
        ${th(v, 'vm_name', 'VM')}${th(v, 'state', 'State')}${th(v, 'verify_state', html`Verified ${tip('verification')}`)}${th(v, 'app', 'App')}${th(v, 'wave', 'Wave')}${th(v, 'namespace', 'Namespace')}${th(v, 'folder', 'Folder')}${th(v, 'batch_name', 'Batch')}${th(v, 'attempts', 'Att.')}${th(v, 'message', 'Message')}${th(v, 'updated_at', 'Updated')}</tr></thead>
        <tbody>${pageRows.map((x) => html`<tr class="click ${v.checked.has(x.moref) ? 'on' : ''}" data-act="vm" data-moref="${x.moref}">
          <td class="chk"><input type="checkbox" data-act="chk" data-moref="${x.moref}" ${attr(v.checked.has(x.moref), 'checked')}></td>
          <td><span class="vm-name">${x.vm_name}<small>${x.moref}</small></span></td><td>${pill(x.state)}</td><td>${verifyTag(x.verify_state, x.verified_at)}</td><td>${appTag(x.app)}</td><td class="num">${x.wave}</td>
          <td class="mono small nowrap">${x.namespace}</td><td class="small clip" title="${x.folder}">${x.folder || '/'}</td>
          <td class="mono tiny nowrap">${x.batch_name || x.precheck_batch || ''}</td><td class="num">${x.attempts}</td>
          <td class="msg"><div title="${x.message || ''}">${x.message || ''}</div></td><td class="small muted nowrap">${fmtTime(x.updated_at)}</td></tr>`)}</tbody></table></div>
      ${pager(rows.length, v.page, PAGE)}</div>`;
  },
  act: {
    q: debounce((t, e, v) => { v.f.q = t.value; v.page = 0; v.render(); }, 180),
    folderF: debounce((t, e, v) => { v.f.folder = t.value.trim(); v.page = 0; v.render(); }, 250),
    filter(t, e, v) { v.f[t.dataset.k] = t.value; v.page = 0; v.render(); },
    st(t, e, v) { const s = t.dataset.s; if (!s) v.f.states.clear(); else if (v.f.states.has(s)) v.f.states.delete(s); else v.f.states.add(s); v.page = 0; v.render(); },
    sort(t, e, v) { const k = t.dataset.key; v.sort = { key: k, dir: v.sort.key === k ? -v.sort.dir : 1 }; v.render(); },
    page(t, e, v) { v.page = +t.dataset.page; v.render(); window.scrollTo(0, 0); },
    chk(t, e, v) { e.stopPropagation(); if (t.checked) v.checked.add(t.dataset.moref); else v.checked.delete(t.dataset.moref); v.render(); },
    chkPage(t, e, v) { v.rows.slice(v.page * PAGE, (v.page + 1) * PAGE).forEach((x) => (t.checked ? v.checked.add(x.moref) : v.checked.delete(x.moref))); v.render(); },
    bClear(t, e, v) { v.checked.clear(); v.render(); },
    async bRetry(t, e, v) { if (await doRetry(checkedVms(v))) { v.checked.clear(); v.refresh(); } },
    async bSkip(t, e, v) { await doSkip(Array.from(v.checked)); v.refresh(); },
    async bUnskip(t, e, v) { await doSkip(Array.from(v.checked), true); v.refresh(); },
    async bWave(t, e, v) { if (await doMoveWave(Array.from(v.checked))) { v.checked.clear(); v.refresh(); } },
    bRollback: (t, e, v) => doRollback({ morefs: Array.from(v.checked) }),
    bVerify: (t, e, v) => startJob('verify', { morefs: checkedVms(v).filter((x) => x.state === 'committed').map((x) => x.moref) }),
    bAbandon: (t, e, v) => doAbandon({ morefs: Array.from(v.checked) }),
  },
});
const checkedVms = (v) => Array.from(v.checked).map((m) => v.byMoref.get(m)).filter(Boolean);

// =================================================================== TRIAGE
VIEWS.triage = view({
  title: 'Triage', sub: 'Failures grouped by cause, with the likely fix', live: true,
  init(v) { v.open = v.open || new Set(); },
  async load(v) { v.d = await GET('/api/triage'); },
  paint(v) {
    const d = v.d;
    const total = d.groups.reduce((a, g) => a + g.count, 0);
    const nothing = !total && !d.stalled.length && !d.rolled_back.length && !d.cleanup.length && !d.awaiting_commit.length;
    if (nothing) return emptyState('check', 'Nothing needs attention', 'No failed, stalled or rolled-back VMs. Failures show up here grouped by cause as soon as they happen.');
    return html`
      <div class="kpis">${kpi('Failed VMs', total, plural(d.groups.length, 'distinct cause'), null, 'var(--s-failed)')}
        ${kpi('Stalled', d.stalled.length, 'batch timed out', null, 'var(--warn)')}
        ${kpi('Rolled back', d.rolled_back.length, 'back in vCenter, retryable', null, 'var(--s-rolled_back)')}
        ${kpi('Awaiting commit', d.awaiting_commit.length, '', d.awaiting_commit.length ? 'execute' : null, 'var(--s-awaiting_commit)')}</div>
      <div class="stack mt">
        ${d.groups.map((g, i) => issueCard(v, g, i))}
        ${d.stalled.length ? html`<div class="card issue warn"><div class="card-h"><h3>Stalled in a timed-out batch ${tip('stalled')}</h3><span class="tag warn">${plural(d.stalled.length, 'VM')}</span></div>
          <div class="card-b"><p class="muted small" style="margin-top:0">The operator never reported a result within batch_timeout_minutes, so nobody is polling these. Refresh to pick up a late result; if they stay stuck, roll the batch back.</p>${vmChips(d.stalled, 30)}</div>
          <div class="card-f"><button class="btn" data-act="refreshCluster">${icon('refresh')} Refresh from cluster</button>
            <button class="btn danger-ghost" data-act="rbList" data-list="stalled">${icon('rollback')} Roll back</button></div></div>` : ''}
        ${d.rolled_back.length ? html`<div class="card issue info"><div class="card-h"><h3>Handed back to vCenter ${tip('rollback')}</h3><span class="tag info">${plural(d.rolled_back.length, 'VM')}</span></div>
          <div class="card-b"><p class="muted small" style="margin-top:0">Ownership is back with vCenter. Once the cause is fixed, requeue them and precheck again.</p>${vmChips(d.rolled_back, 30)}</div>
          <div class="card-f"><button class="btn primary" data-act="retryList" data-list="rolled_back">${icon('refresh')} Retry ${n(d.rolled_back.length)}</button></div></div>` : ''}
        ${d.cleanup.length ? html`<div class="card issue info"><div class="card-h"><h3>Batch objects left on the cluster ${tip('cleanup')}</h3><span class="tag">${plural(d.cleanup.length, 'batch', 'batches')}</span></div>
          <div class="card-b"><p class="muted small" style="margin-top:0">Their rollback is confirmed, so they are safe to delete. Deleting also frees the child ImportOperation names for a retry.</p>
            <div class="vmlist">${d.cleanup.map((b) => html`<a data-act="batch" data-ns="${b.namespace}" data-name="${b.name}">${b.name}</a>`)}</div></div>
          <div class="card-f"><button class="btn" data-act="cleanup">${icon('trash')} Delete ${n(d.cleanup.length)}</button></div></div>` : ''}
      </div>`;
  },
  act: {
    expand(t, e, v) { const k = t.dataset.key; if (v.open.has(k)) v.open.delete(k); else v.open.add(k); v.render(); },
    async gRetry(t, e, v) { if (await doRetry(v.d.groups[+t.dataset.i].vms)) v.refresh(); },
    async gSkip(t, e, v) { await doSkip(v.d.groups[+t.dataset.i].vms.map((x) => x.moref)); v.refresh(); },
    gRollback: (t, e, v) => doRollback({ morefs: v.d.groups[+t.dataset.i].vms.map((x) => x.moref) }),
    gAbandon: (t, e, v) => doAbandon({ morefs: v.d.groups[+t.dataset.i].vms.map((x) => x.moref) }),
    gQueue: (t, e, v) => { const g = v.d.groups[+t.dataset.i]; go('queue', 'state=' + Object.keys(g.stages).map((s) => (s === 'precheck' ? 'precheck_failed' : 'failed')).join(',')); },
    async retryList(t, e, v) { if (await doRetry(v.d[t.dataset.list])) v.refresh(); },
    rbList: (t, e, v) => doRollback({ morefs: v.d[t.dataset.list].map((x) => x.moref) }),
    refreshCluster: () => startJob('refresh', {}),
    cleanup: (t, e, v) => doCleanup(v.d.cleanup),
  },
});
function vmChips(vms, max) {
  return html`<div class="vmlist">${vms.slice(0, max).map((x) => html`<a data-act="vm" data-moref="${x.moref}" title="${x.moref} · wave ${x.wave}">${x.vm_name}</a>`)}
    ${vms.length > max ? html`<span class="small muted">and ${n(vms.length - max)} more</span>` : ''}</div>`;
}
function issueCard(v, g, i) {
  const is = g.issue;
  const open = v.open.has(g.key);
  const acts = new Set(is ? is.actions : ['retry']);
  const hasImport = !!g.stages.import;
  const precheckOnly = !hasImport;
  return html`<div class="card issue ${is && is.id === 'rollback' ? '' : precheckOnly ? 'warn' : ''}">
    <div class="card-h"><div class="count ${precheckOnly ? 'warn-text' : 'bad-text'}">${n(g.count)}</div>
      <div class="grow"><h3>${is ? is.title : 'Unrecognised failure'}</h3>
        <div class="row wrap tiny">${Object.entries(g.stages).map(([s, c]) => html`<span class="tag ${s === 'import' ? 'bad' : 'warn'}">${c} at ${s}</span>`)}</div></div>
      <button class="btn sm ghost" data-act="expand" data-key="${g.key}">${open ? 'Hide' : 'Show'} VMs</button></div>
    <div class="card-b stack" style="gap:10px">
      <div class="msgbox">${g.message || '(the operator gave no message)'}</div>
      ${is ? html`<div class="small"><b>What to do:</b> ${is.advice}</div>` : html`<div class="small muted">No known pattern matches this message. Open a VM for its full history, and check the batch on the cluster.</div>`}
      ${open ? html`<table class="t compact" style="border:1px solid var(--line);border-radius:8px"><thead><tr><th>VM</th><th>State</th><th>Wave</th><th>Namespace</th><th>Batch</th><th>Att.</th></tr></thead><tbody>
        ${g.vms.map((x) => html`<tr class="click" data-act="vm" data-moref="${x.moref}"><td><span class="vm-name">${x.vm_name}<small>${x.moref}</small></span></td><td>${pill(x.state)}</td>
          <td>${x.wave}</td><td class="mono small">${x.namespace}</td><td class="mono tiny">${x.batch_name || x.precheck_batch || ''}</td><td>${x.attempts}</td></tr>`)}</tbody></table>` : vmChips(g.vms, 14)}
    </div>
    <div class="card-f">
      ${acts.has('retry') || !is ? html`<button class="btn primary sm" data-act="gRetry" data-i="${i}">${icon('refresh')} Retry ${n(g.count)}</button>` : ''}
      ${acts.has('skip') ? html`<button class="btn sm" data-act="gSkip" data-i="${i}">${icon('skip')} Skip</button>` : ''}
      ${acts.has('abandon') ? html`<button class="btn sm" data-act="gAbandon" data-i="${i}">${icon('trash')} Abandon batches</button>` : ''}
      <button class="btn sm ghost" data-act="gQueue" data-i="${i}">Open in queue</button>
      <span class="grow"></span>
      ${hasImport ? html`<button class="btn sm danger-ghost" data-act="gRollback" data-i="${i}">${icon('rollback')} Roll back to vCenter</button>` : ''}
    </div></div>`;
}

// ================================================================== BATCHES
VIEWS.batches = view({
  title: 'Batches', sub: 'ImportOperationBatch objects this campaign created', live: true,
  init(v) { v.f = { stage: '', states: new Set(), q: '' }; v.page = 0; v.sort = { key: 'created_at', dir: -1 }; },
  async load(v) { v.d = await GET('/api/batches'); },
  paint(v) {
    const all = v.d.batches;
    if (!all.length) return emptyState('batches', 'No batches yet', 'Batches are created when you precheck or import. Every batch holds VMs from one namespace, wave and folder.');
    const f = v.f, q = f.q.trim().toLowerCase();
    const base = all.filter((b) => (!f.stage || b.stage === f.stage) && (!q || (b.name + ' ' + b.namespace + ' ' + (b.message || '')).toLowerCase().includes(q)));
    const counts = countBy(base, 'state');
    const rows = sortRows(base.filter((b) => !f.states.size || f.states.has(b.state)), v.sort.key, v.sort.dir);
    const pageRows = rows.slice(v.page * PAGE, (v.page + 1) * PAGE);
    return html`<div class="card">
      <div class="toolbar"><div class="search">${icon('search')}<input class="input" id="b-q" placeholder="Search batch, namespace, message…" value="${f.q}" data-input="q"></div>
        <div class="seg">${[['', 'All'], ['precheck', 'Precheck'], ['import', 'Import']].map(([k, l]) => html`<button class="${f.stage === k ? 'on' : ''}" data-act="stage" data-k="${k}">${l}</button>`)}</div>
        <div class="chips">${Object.entries(counts).map(([s, c]) => html`<span class="chip ${f.states.has(s) ? 'on' : ''}" data-act="st" data-s="${s}">${s.replace(/_/g, ' ')} <span class="n">${n(c)}</span></span>`)}</div></div>
      <div class="table-wrap"><table class="t"><thead><tr>${th(v, 'name', 'Batch')}${th(v, 'namespace', 'Namespace')}${th(v, 'stage', 'Stage')}${th(v, 'wave', 'Wave')}${th(v, 'state', 'State')}
        <th style="width:150px">Members</th>${th(v, 'applied_at', 'Applied')}${th(v, 'finished_at', 'Finished')}${th(v, 'message', 'Message')}</tr></thead>
        <tbody>${pageRows.map((b) => html`<tr class="click" data-act="batch" data-ns="${b.namespace}" data-name="${b.name}">
          <td class="mono small">${b.name}</td><td class="mono small">${b.namespace}</td><td><span class="tag">${b.stage}</span></td><td class="num">${b.wave}</td><td>${batchTag(b.state)}</td>
          <td>${segbar(b.members, 0)}<div class="tiny muted">${plural(b.vm_count, 'VM')}</div></td>
          <td class="small muted nowrap">${fmtTime(b.applied_at)}</td><td class="small muted nowrap">${fmtTime(b.finished_at)}</td><td class="msg"><div title="${b.message || ''}">${b.message || ''}</div></td></tr>`)}</tbody></table></div>
      ${pager(rows.length, v.page, PAGE)}</div>`;
  },
  act: {
    q: debounce((t, e, v) => { v.f.q = t.value; v.page = 0; v.render(); }, 180),
    stage(t, e, v) { v.f.stage = t.dataset.k; v.page = 0; v.render(); },
    st(t, e, v) { const s = t.dataset.s; if (v.f.states.has(s)) v.f.states.delete(s); else v.f.states.add(s); v.page = 0; v.render(); },
    sort(t, e, v) { const k = t.dataset.key; v.sort = { key: k, dir: v.sort.key === k ? -v.sort.dir : 1 }; v.render(); },
    page(t, e, v) { v.page = +t.dataset.page; v.render(); },
  },
});

// ================================================================= ACTIVITY
VIEWS.activity = view({
  title: 'Activity & logs', sub: 'Job logs, the event log, every state change, and exports', live: true,
  init(v, p) { v.tab = p.tab || v.tab || 'jobs'; v.jobId = p.job || v.jobId || null; v.filter = ''; v.errOnly = false; v.level = ''; v.state = ''; v.q = ''; },
  async load(v) {
    if (v.tab === 'jobs') {
      const d = await GET('/api/jobs');
      v.jobs = d.jobs;
      if (!v.jobId && v.jobs.length) v.jobId = v.jobs[0].id;
      if (v.jobId) {
        const same = v.job && v.job.id === v.jobId && v.job.status !== 'running';
        if (!same) v.job = await GET('/api/jobs/' + encodeURIComponent(v.jobId));
      }
    } else if (v.tab === 'events') {
      v.events = (await GET('/api/events?' + qs({ limit: 1000, level: v.level }))).events;
    } else if (v.tab === 'movement') {
      const d = await GET('/api/transitions?' + qs({ limit: 1000, state: v.state }));
      v.trans = d.transitions; v.stats = d.stats; v.ledger = d.ledger_path;
    }
  },
  paint(v) {
    const tabs = [['jobs', 'Jobs'], ['events', 'Event log'], ['movement', 'Movement log'], ['exports', 'Exports']];
    return html`<div class="tabs">${tabs.map(([k, l]) => html`<button class="${v.tab === k ? 'on' : ''}" data-act="tab" data-k="${k}">${l}</button>`)}</div>
      ${v.tab === 'jobs' ? jobsTab(v) : v.tab === 'events' ? eventsTab(v) : v.tab === 'movement' ? movementTab(v) : exportsTab()}`;
  },
  after(v) { const log = $('#act-log'); if (log && v.follow) log.scrollTop = log.scrollHeight; },
  act: {
    tab(t, e, v) { v.tab = t.dataset.k; v.loaded = false; v.refresh(); },
    pickJob(t, e, v) { v.jobId = t.dataset.id; v.job = null; v.follow = true; v.refresh(); },
    logFilter: debounce((t, e, v) => { v.filter = t.value; v.follow = false; v.render(); }, 150),
    errOnly(t, e, v) { v.errOnly = t.checked; v.render(); },
    level(t, e, v) { v.level = t.value; v.refresh(); },
    tstate(t, e, v) { v.state = t.value; v.refresh(); },
    evq: debounce((t, e, v) => { v.q = t.value; v.render(); }, 150),
    follow(t, e, v) { followJob(t.dataset.id, true); },
    async verify() {
      const d = await GET('/api/transitions?limit=1');
      toast('Ledger', d.stats.transitions + ' transitions in the database; ledger at ' + d.ledger_path, 'info', 8000);
    },
  },
});
function jobsTab(v) {
  if (!v.jobs.length) return emptyState('terminal', 'No jobs yet', 'Every discovery, preflight, precheck, import and rollback started from the console is logged here, and kept under <workdir>/jobs.');
  const j = v.job;
  let lines = j ? j.log : [];
  if (v.errOnly) lines = lines.filter(([, , t]) => ['bad', 'warn'].includes(lineClass(t)));
  return html`<div class="grid side">
    <div class="card"><div class="joblist" data-scroll="jobs">${v.jobs.map((x) => html`<div class="jobitem ${x.id === v.jobId ? 'on' : ''}" data-act="pickJob" data-id="${x.id}">
      ${statusIcon(x.status)}<div class="grow" style="min-width:0"><div class="t ellipsis">${x.title}</div><div class="m">${fmtTime(x.started_at, true)} · ${fmtDur(elapsed(x))}${x.user && x.user !== 'owner' ? ' · ' + x.user : ''}</div></div></div>`)}</div></div>
    <div class="card">${j ? html`<div class="card-h">${statusIcon(j.status)}<div class="grow"><h3>${j.title}</h3>
        <div class="sub">${STATUS_WORD[j.status] || j.status} · started ${fmtTime(j.started_at, true)} by ${j.user || 'owner'} · ${fmtDur(elapsed(j))}${j.error ? html` · <span class="bad-text">${j.error}</span>` : ''}</div></div>
        ${j.status === 'running' ? html`<button class="btn sm" data-act="follow" data-id="${j.id}">Follow live</button>` : ''}</div>
      <div class="toolbar"><div class="search">${icon('search')}<input class="input sm" id="act-filter" placeholder="Filter lines" value="${v.filter}" data-input="logFilter"></div>
        <label class="check small"><input type="checkbox" data-change="errOnly" ${attr(v.errOnly, 'checked')}> Problems only</label>
        <span class="grow"></span><span class="small muted">${plural(j.log.length, 'line')}</span></div>
      <div class="log" id="act-log" style="max-height:calc(100vh - 330px);border-radius:0 0 var(--radius) var(--radius)" data-scroll="actlog">${logLines(lines, v.filter)}</div>` : html`<div class="card-b muted">Pick a job.</div>`}</div></div>`;
}
function eventsTab(v) {
  const q = v.q.toLowerCase();
  const rows = v.events.filter((e) => !q || [e.message, e.moref, e.batch, e.actor].join(' ').toLowerCase().includes(q));
  return html`<div class="card"><div class="toolbar">
      <div class="search">${icon('search')}<input class="input" id="ev-q" placeholder="Search events" value="${v.q}" data-input="evq"></div>
      <select class="select" data-change="level"><option value="">All levels</option>${['info', 'warn', 'error'].map((l) => html`<option ${attr(v.level === l, 'selected')}>${l}</option>`)}</select>
      <span class="small muted">${plural(rows.length, 'event')} (newest first)</span></div>
    <div class="table-wrap"><table class="t compact"><thead><tr><th>When</th><th>Level</th><th>Who</th><th>VM</th><th>Batch</th><th>Message</th></tr></thead><tbody>
    ${rows.slice(0, 500).map((e) => html`<tr><td class="nowrap small muted">${fmtTime(e.ts, true)}</td><td><span class="tag ${e.level === 'error' ? 'bad' : e.level === 'warn' ? 'warn' : ''}">${e.level}</span></td><td class="small">${e.actor || ''}</td>
      <td>${e.moref ? html`<a data-act="vm" data-moref="${e.moref}">${e.moref}</a>` : ''}</td><td class="mono tiny">${e.batch || ''}</td><td class="small">${e.message}</td></tr>`)}</tbody></table></div></div>`;
}
function movementTab(v) {
  const s = v.stats || {};
  return html`<div class="kpis mb">${kpi('Transitions', s.transitions || 0, 'every state change, ever')}
      ${s.import_seconds ? kpi('Avg import', fmtDur(s.import_seconds.avg), 'min ' + fmtDur(s.import_seconds.min) + ' · max ' + fmtDur(s.import_seconds.max)) : ''}
      ${kpi('First', s.first ? fmtTime(s.first, true) : '—', '')}${kpi('Latest', s.last ? fmtTime(s.last, true) : '—', '')}</div>
    <div class="card"><div class="toolbar"><select class="select" data-change="tstate"><option value="">Into any state</option>${STATE_ORDER.map((x) => html`<option value="${x}" ${attr(v.state === x, 'selected')}>into ${label(x)}</option>`)}</select>
      <span class="small muted">append-only ledger: <span class="mono">${v.ledger}</span></span></div>
    <div class="table-wrap"><table class="t compact"><thead><tr><th>When</th><th>VM</th><th>Change</th><th>Stage</th><th>Batch</th><th>Held</th><th>Detail</th></tr></thead><tbody>
    ${v.trans.map((t) => html`<tr class="click" data-act="vm" data-moref="${t.moref}"><td class="nowrap small muted">${fmtTime(t.ts, true)}</td><td><span class="vm-name">${t.vm_name}<small>${t.moref}</small></span></td>
      <td class="nowrap">${t.from_state ? html`${dot(t.from_state)} <span class="faint">→</span> ` : ''}${pill(t.to_state)}</td><td class="small">${t.stage || ''}</td><td class="mono tiny">${t.batch || ''}</td>
      <td class="small num">${t.held_secs !== null && t.held_secs !== undefined ? fmtDur(t.held_secs) : ''}</td><td class="msg"><div>${t.message || t.phase || ''}</div></td></tr>`)}</tbody></table></div></div>`;
}
function exportsTab() {
  const card = (name, title, text) => html`<div class="card"><div class="card-b"><h3>${title}</h3><p class="small muted">${text}</p>
    <a class="btn" href="${exportUrl(name)}" download="${name}">${icon('download')} ${name}</a></div></div>`;
  return html`<div class="grid two">
    ${card('tracker.csv', 'Tracker', 'One row per VM: source facts, target namespace and resource, batches, and timings. For the change ticket.')}
    ${card('transitions.csv', 'Movement log', 'Every state change of every VM, with how long it was held. The audit trail in CSV.')}
    ${card('ledger.jsonl', 'Ledger', 'The append-only JSONL ledger. Survives a rebuilt database; ship it to a SIEM or a change record.')}
    ${card('report.html', 'HTML report', 'A standalone progress report, including the moved-into-VCFA table. Opens in any browser.')}</div>`;
}
