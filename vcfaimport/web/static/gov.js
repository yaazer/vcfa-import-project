'use strict';
/* VCFA Import Console — governance: change windows, the two-person rule,
 * settings, people and notifications; plus the small shared pieces the other
 * pages use (readiness, applications, verification, saved views, ETAs). */

// ================================================================ identity
const ROLE_RANK = { viewer: 0, operator: 1, admin: 2 };
const me = () => (S.pulse && S.pulse.me) || (S.info && S.info.me) || { name: 'owner', role: 'admin' };
const can = (role) => (ROLE_RANK[me().role] || 0) >= ROLE_RANK[role];

// ================================================================ badges
const GRADE = { ready: ['ok', 'ready'], warn: ['warn', 'check first'], block: ['bad', 'likely to fail'] };
function gradeTag(g, why) {
  if (!g) return '';
  const [c, l] = GRADE[g] || ['', g];
  return html`<span class="tag ${c}" title="${why || ''}">${l}</span>`;
}
const VERIFY = { ok: ['ok', 'verified'], warn: ['', 'unverifiable'], fail: ['bad', 'verify failed'] };
function verifyTag(vs, when) {
  if (!vs) return '';
  const [c, l] = VERIFY[vs] || ['', vs];
  return html`<span class="tag ${c}" title="${when ? 'checked ' + fmtTime(when, true) : ''}">${l}</span>`;
}
const appTag = (a) => (a ? html`<span class="tag app" title="application">${a}</span>` : '');
function tagChips(tags, max) {
  tags = tags || [];
  if (!tags.length) return html`<span class="faint">—</span>`;
  const shown = tags.slice(0, max || 2);
  return html`${shown.map((t) => html`<span class="tag vtag" title="${t}">${t.split(':').pop()}</span>`)}${tags.length > shown.length ? html`<span class="tag" title="${tags.join('\n')}">+${tags.length - shown.length}</span>` : ''}`;
}
const eta = (sec) => (sec ? '≈ ' + fmtDur(sec) : '—');
const CHECK_IC = { ok: 'check', fail: 'x', skip: 'skip' };
function verifyChecks(checks) {
  return html`<div class="checks">${(checks || []).map((c) => html`<div class="checkrow ${c.status === 'ok' ? 'ok' : c.status === 'fail' ? 'bad' : 'skip'}">
    <span class="ic">${icon(CHECK_IC[c.status] || 'info')}</span><div><div>${c.check}</div><div class="d">${c.detail}</div></div></div>`)}</div>`;
}
function findingsList(found) {
  if (!found || !found.length) return html`<div class="small muted">No readiness concerns found in the vCenter facts.</div>`;
  return html`<div class="stack" style="gap:8px">${found.map((f) => html`<div class="finding ${f.level}">
    <span class="tag ${f.level === 'block' ? 'bad' : f.level === 'warn' ? 'warn' : ''}">${f.level === 'block' ? 'likely to fail' : f.level === 'warn' ? 'check' : 'note'}</span>
    <div><b>${f.title}</b><div class="small muted">${f.advice}</div></div></div>`)}</div>`;
}

// ============================================================ saved views
/* Per page, per browser: a named snapshot of a page's filter object (v.f). */
const SV = {
  key: (page) => 'vcfa-views:' + page,
  list(page) { try { return JSON.parse(store.get(SV.key(page)) || '[]') || []; } catch (e) { return []; } },
  put(page, list) { store.set(SV.key(page), JSON.stringify(list.slice(0, 20))); },
  pack(f) { const o = {}; for (const k in f) o[k] = f[k] instanceof Set ? { __set: Array.from(f[k]) } : f[k]; return o; },
  unpack(o, into) {
    for (const k in o) into[k] = o[k] && o[k].__set ? new Set(o[k].__set) : o[k];
    return into;
  },
};
function savedViews(page) {
  const list = SV.list(page);
  return html`<div class="savedviews"><span class="small muted">Views</span>
    ${list.map((x) => html`<span class="chip sv" data-act="svApply" data-page="${page}" data-name="${x.name}">${x.name}
      <button class="x" data-act="svDel" data-page="${page}" data-name="${x.name}" aria-label="Delete view ${x.name}">×</button></span>`)}
    <button class="btn xs ghost" data-act="svSave" data-page="${page}">${icon('plus')} Save view</button></div>`;
}
Object.assign(GLOBAL_ACT, {
  async svSave(t) {
    const v = S.view, page = t.dataset.page;
    if (!v || !v.f) return;
    const res = await modal({ title: 'Save this view', ic: 'search', confirm: 'Save',
      body: html`<p class="muted">Keeps the current filters under a name, in this browser.</p>
        <label class="field"><span>Name</span><input class="input" data-field="name" maxlength="40" placeholder="e.g. Payroll, not ready"></label>` });
    const name = res && (res.name || '').trim();
    if (!name) return;
    const list = SV.list(page).filter((x) => x.name !== name);
    list.push({ name, f: SV.pack(v.f) });
    SV.put(page, list);
    toast('View saved: ' + name, '', 'ok', 2500);
    v.render();
  },
  svApply(t, e) {
    if (e.target.closest('.x')) return;
    const v = S.view, hit = SV.list(t.dataset.page).find((x) => x.name === t.dataset.name);
    if (!v || !hit) return;
    SV.unpack(hit.f, v.f);
    v.page = 0;
    v.render();
  },
  svDel(t, e) {
    e.stopPropagation();
    SV.put(t.dataset.page, SV.list(t.dataset.page).filter((x) => x.name !== t.dataset.name));
    if (S.view) S.view.render();
  },
  async verify(t) {
    const body = t.dataset.morefs ? { morefs: t.dataset.morefs.split(',') } : { unverified: t.dataset.unverified === '1' };
    await startJob('verify', body);
  },
});

// ============================================================== callouts
/* Governance news for the Overview: approvals waiting, the next window, split apps. */
function govCallouts(d, p) {
  const out = [];
  const pend = (p && p.approvals_pending) || d.pending_approvals || 0;
  if (pend) {
    out.push(html`<div class="callout warn"><div class="ic">${icon('shield')}</div><div class="grow"><h3>${plural(pend, 'request')} waiting for a second person</h3>
      <p>The two-person rule is on for ${(S.info.features.require_approval || []).join(', ') || 'some stages'}. Someone other than the requester approves it, and it starts at once.</p></div>
      <button class="btn" data-act="go" data-to="schedule">Review ${icon('arrow')}</button></div>`);
  }
  const w = d.next_window || (p && p.next_window);
  if (w) {
    const start = parseTs(w.start_at), end = parseTs(w.end_at), now = Date.now();
    const when = w.state === 'running' ? 'running now, until ' + fmtTime(w.end_at, true)
      : start && start > now ? 'opens in ' + fmtDur((start - now) / 1000) + ' (' + fmtTime(w.start_at, true) + ')' : 'open until ' + fmtTime(w.end_at, true);
    out.push(html`<div class="callout"><div class="ic">${icon('calendar')}</div><div class="grow"><h3>Change window #${w.id}: ${w.stage} ${scheduleScope(w.body)}</h3>
      <p>${when}${w.state === 'awaiting_approval' ? ' · waiting for approval' : ''}${end ? ' · no batch starts that could not finish by the end' : ''}</p></div>
      <button class="btn ghost" data-act="go" data-to="schedule">Windows ${icon('arrow')}</button></div>`);
  }
  if (d.app_splits && d.app_splits.length) {
    out.push(html`<div class="callout warn"><div class="ic">${icon('waves')}</div><div class="grow"><h3>${plural(d.app_splits.length, 'application')} split across waves</h3>
      <p>${d.app_splits.slice(0, 4).map((a) => a.app + ' (waves ' + Object.keys(a.waves).join(', ') + ')').join(' · ')}${d.app_splits.length > 4 ? ' …' : ''}. VMs that must move together should share a wave.</p></div>
      <button class="btn ghost" data-act="go" data-to="waves">Arrange ${icon('arrow')}</button></div>`);
  }
  return out.length ? html`<div class="stack mb" style="gap:10px">${out}</div>` : '';
}
function scheduleScope(b) {
  b = b || {};
  const bits = [];
  if (b.waves && b.waves.length) bits.push('wave ' + b.waves.join(', '));
  if (b.folders && b.folders.length) bits.push('in ' + b.folders.join(', '));
  return bits.length ? bits.join(' ') : 'every wave';
}

// ========================================================== CHANGE CONTROL
const SCHED_TONE = { scheduled: 'info', awaiting_approval: 'warn', running: 'busy', done: 'ok', stopped: 'warn', failed: 'bad', missed: 'bad', cancelled: '' };
const APPROVAL_TONE = { pending: 'warn', approved: 'info', executed: 'ok', rejected: 'bad', cancelled: '' };
const toLocalInput = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);

VIEWS.schedule = view({
  title: 'Change control', sub: 'Change windows that run unattended, and the two-person rule', live: true,
  init(v, p) {
    const start = new Date(Date.now() + 3600e3);
    start.setMinutes(0, 0, 0);
    v.form = v.form || { stage: p.stage || 'import', waves: new Set(p.waves ? p.waves.split(',').map(Number) : []), folder: '',
      start: toLocalInput(start), end: toLocalInput(new Date(start.getTime() + 4 * 3600e3)) };
    if (p.stage) v.form.stage = p.stage;
    v.fit = null;
  },
  async load(v) {
    const [s, a, o] = await Promise.all([GET('/api/schedules'), GET('/api/approvals'), GET('/api/overview')]);
    v.s = s; v.a = a; v.o = o;
    await refit(v);
  },
  paint(v) {
    const pending = v.a.approvals.filter((x) => x.state === 'pending');
    const decided = v.a.approvals.filter((x) => x.state !== 'pending');
    const open = v.s.schedules.filter((x) => ['scheduled', 'awaiting_approval', 'running'].includes(x.state));
    const past = v.s.schedules.filter((x) => !open.includes(x));
    const f = v.form, i = me();
    return html`
      ${!v.s.scheduler ? html`<div class="note warn mb">The scheduler is not running in this console, so windows will not start from here. Keep <code>vcfa-import serve</code> running, or run <code>vcfa-import schedule tick</code> every few minutes from Task Scheduler or cron.</div>` : ''}
      <div class="card"><div class="card-h"><h3>Approvals ${tip('approval')}</h3><span class="sub">${v.a.required.length ? 'two-person rule on for ' + v.a.required.join(', ') : 'the two-person rule is off — turn it on in Settings'}</span></div>
        ${pending.length ? html`<div class="stack card-b" style="gap:10px">${pending.map((x) => approvalRow(x, i))}</div>`
          : html`<div class="card-b small muted">Nothing waiting. Requests appear here when someone starts a ${v.a.required.join(' or ') || 'gated'} step.</div>`}
        ${decided.length ? html`<details class="card-b" style="border-top:1px solid var(--line)"><summary class="small" style="cursor:pointer;font-weight:600">History (${n(decided.length)})</summary>
          <table class="t compact mt-s"><thead><tr><th>#</th><th>Request</th><th>By</th><th>State</th><th>Decided by</th><th>Note</th></tr></thead><tbody>
          ${decided.slice(0, 50).map((x) => html`<tr><td class="num">${x.id}</td><td>${x.summary}</td><td class="small">${x.requested_by}<div class="tiny faint">${fmtTime(x.requested_at, true)}</div></td>
            <td><span class="tag ${APPROVAL_TONE[x.state] || ''}">${x.state}</span></td><td class="small">${x.decided_by || ''}<div class="tiny faint">${fmtTime(x.decided_at, true)}</div></td><td class="small muted">${x.note || ''}</td></tr>`)}</tbody></table></details>` : ''}
      </div>
      <div class="card mt"><div class="card-h"><h3>Change windows ${tip('change_window')}</h3><span class="sub">next 7 days</span></div>
        <div class="card-b">${timeline(open.concat(past))}</div>
        ${open.length ? html`<div class="table-wrap" style="border-top:1px solid var(--line)"><table class="t compact"><thead><tr><th>#</th><th>Runs</th><th>Window</th><th>State</th><th>By</th><th></th></tr></thead><tbody>
          ${open.map((x) => html`<tr><td class="num">${x.id}</td><td><b>${x.stage}</b> ${scheduleScope(x.body)}</td>
            <td class="small nowrap">${fmtTime(x.start_at, true)} → ${fmtTime(x.end_at, true)}<div class="tiny faint">${fmtDur((parseTs(x.end_at) - parseTs(x.start_at)) / 1000)}</div></td>
            <td><span class="tag ${SCHED_TONE[x.state]}">${x.state.replace('_', ' ')}</span>${x.message ? html`<div class="tiny muted">${x.message}</div>` : ''}</td>
            <td class="small">${x.created_by}</td>
            <td class="right">${x.job_id ? html`<button class="btn xs" data-act="showJob" data-id="${x.job_id}">Log</button>` : ''}
              ${x.state !== 'running' ? html`<button class="btn xs danger-ghost" data-act="cancelWindow" data-id="${x.id}">Cancel</button>` : ''}</td></tr>`)}</tbody></table></div>` : ''}
        ${past.length ? html`<details class="card-b" style="border-top:1px solid var(--line)"><summary class="small" style="cursor:pointer;font-weight:600">Past windows (${n(past.length)})</summary>
          <table class="t compact mt-s"><tbody>${past.slice(0, 40).map((x) => html`<tr><td class="num">${x.id}</td><td>${x.stage} ${scheduleScope(x.body)}</td>
            <td class="small nowrap">${fmtTime(x.start_at, true)} → ${fmtTime(x.end_at, true)}</td><td><span class="tag ${SCHED_TONE[x.state] || ''}">${x.state}</span></td>
            <td class="small muted">${x.message || ''}</td><td>${x.job_id ? html`<button class="btn xs" data-act="showJob" data-id="${x.job_id}">Log</button>` : ''}</td></tr>`)}</tbody></table></details>` : ''}
      </div>
      <div class="card mt"><div class="card-h"><h3>Plan a window</h3><span class="sub">it starts on its own, and never starts a batch that could not finish before the end</span></div>
        <div class="card-b stack">
          <div class="row wrap" style="gap:14px">
            <div class="seg">${['precheck', 'import'].map((k) => html`<button class="${f.stage === k ? 'on' : ''}" data-act="fStage" data-k="${k}">${k === 'import' ? 'Import' : 'Precheck'}</button>`)}</div>
            <div class="chips"><span class="chip ${f.waves.size ? '' : 'on'}" data-act="fWave" data-w="all">All waves</span>
              ${v.o.waves.map((w) => html`<span class="chip ${f.waves.has(w.wave) ? 'on' : ''}" data-act="fWave" data-w="${w.wave}">Wave ${w.wave}</span>`)}</div>
          </div>
          <div class="form-grid">
            <label class="field"><span>Opens</span><input class="input" type="datetime-local" id="w-start" data-input="fTime" data-k="start" value="${f.start}"></label>
            <label class="field"><span>Closes</span><input class="input" type="datetime-local" id="w-end" data-input="fTime" data-k="end" value="${f.end}"></label>
            <label class="field"><span>Folder scope (optional)</span><input class="input" data-input="fTime" data-k="folder" value="${f.folder}" placeholder="every folder"></label>
          </div>
          <div id="w-fit">${fitNote(v)}</div><div class="tiny faint">How the estimate is made ${tip('eta')}</div>
          ${S.info.features.require_approval.includes(f.stage) ? html`<div class="note info">${f.stage} needs a second person: the window waits for approval, and is cancelled if it is rejected or not approved before it closes.</div>` : ''}
        </div>
        <div class="card-f"><button class="btn primary" data-act="createWindow" ${attr(!can('operator'), 'disabled')}>${icon('calendar')} Schedule ${f.stage}</button>
          <span class="small muted">Times are your browser's local time (${Intl.DateTimeFormat().resolvedOptions().timeZone || 'local'}).</span></div></div>`;
  },
  onJobDone(v) { v.refresh(); },
  act: {
    fStage(t, e, v) { v.form.stage = t.dataset.k; refit(v).then(() => v.render()); },
    fWave(t, e, v) {
      const w = t.dataset.w;
      if (w === 'all') v.form.waves.clear(); else if (v.form.waves.has(+w)) v.form.waves.delete(+w); else v.form.waves.add(+w);
      refit(v).then(() => v.render());
    },
    // Store at once; only the fit check waits (one shared timer would drop a field typed right after another).
    fTime(t, e, v) { v.form[t.dataset.k] = t.value; refitSoon(v); },
    async createWindow(t, e, v) {
      const b = windowBody(v);
      if (!b) { toast('Pick when the window opens and closes', '', 'warn'); return; }
      const r = await POST('/api/schedules', b);
      const s = r.schedule;
      toast('Change window #' + s.id + ' planned', s.state === 'awaiting_approval' ? 'It waits for a second person to approve it.' : 'It starts on its own at ' + fmtTime(s.start_at, true) + '.', 'ok', 6000);
      v.refresh();
    },
    async cancelWindow(t, e, v) {
      const ok = await modal({ title: 'Cancel change window #' + t.dataset.id + '?', ic: 'calendar', confirm: 'Cancel window', danger: true, cancel: 'Keep it',
        body: html`<p>It will not start. Nothing on the cluster changes.</p>` });
      if (!ok) return;
      await POST('/api/schedules/' + t.dataset.id + '/cancel');
      v.refresh();
    },
    async approve(t, e, v) { await decide(v, t.dataset.id, true); },
    async reject(t, e, v) { await decide(v, t.dataset.id, false); },
    async withdraw(t, e, v) {
      const ok = await modal({ title: 'Withdraw request #' + t.dataset.id + '?', confirm: 'Withdraw', body: html`<p>It can no longer be approved. Nothing runs.</p>` });
      if (!ok) return;
      await POST('/api/approvals/' + t.dataset.id + '/cancel');
      v.refresh();
    },
  },
});
function approvalRow(x, i) {
  const mine = x.requested_by === i.name;
  return html`<div class="approval">
    <div class="ic">${icon(x.stage === 'rollback' ? 'rollback' : x.stage === 'commit' ? 'commit' : 'execute')}</div>
    <div class="grow"><div><b>#${x.id} · ${x.summary}</b>${x.schedule_id ? html` <span class="tag info">window #${x.schedule_id}</span>` : ''}</div>
      <div class="small muted">requested by <b>${x.requested_by}</b> ${fmtAgo(x.requested_at)}${mine ? ' · you cannot approve your own request' : ''}</div>
      ${x.body && Object.keys(x.body).length ? html`<div class="tiny faint mono">${JSON.stringify(x.body)}</div>` : ''}</div>
    ${mine || !can('operator') ? '' : html`<button class="btn sm primary" data-act="approve" data-id="${x.id}">${icon('check')} Approve</button>
      <button class="btn sm danger-ghost" data-act="reject" data-id="${x.id}">Reject</button>`}
    ${mine || can('admin') ? html`<button class="btn sm ghost" data-act="withdraw" data-id="${x.id}">Withdraw</button>` : ''}</div>`;
}
async function decide(v, id, approve) {
  const x = v.a.approvals.find((a) => String(a.id) === String(id));
  const res = await modal({
    title: (approve ? 'Approve' : 'Reject') + ' request #' + id + '?', ic: approve ? 'check' : 'x',
    confirm: approve ? (x && x.schedule_id ? 'Approve the window' : 'Approve and start') : 'Reject',
    body: html`<p><b>${x ? x.summary : ''}</b>, requested by ${x ? x.requested_by : ''}.</p>
      ${approve && x && !x.schedule_id ? html`<p class="small muted">It starts right away, on behalf of you both.</p>` : ''}
      <label class="field"><span>Note (optional)</span><input class="input" data-field="note" maxlength="200" placeholder="${approve ? 'e.g. CAB-1234 approved' : 'why not'}"></label>` });
  if (!res) return;
  const r = await POST('/api/approvals/' + id + '/decide', { approve, note: res.note || '' });
  if (r.job) { followJob(r.job.id, true); toast('Approved: ' + r.job.title, 'Started on behalf of ' + r.approval.requested_by + ' and you.', 'ok'); }
  else toast('Request #' + id + ' ' + r.approval.state, '', approve ? 'ok' : 'warn');
  v.refresh();
}
function windowBody(v) {
  const f = v.form;
  const a = new Date(f.start), b = new Date(f.end);
  if (isNaN(a) || isNaN(b)) return null;
  return { stage: f.stage, waves: Array.from(f.waves).sort((x, y) => x - y), folders: f.folder ? [f.folder] : null,
    start_at: a.toISOString(), end_at: b.toISOString() };
}
const refitSoon = debounce((v) => refit(v).then(() => { const el = $('#w-fit'); if (el && S.view === v) mount(el, fitNote(v)); }), 300);
async function refit(v) {
  const b = windowBody(v);
  try { v.fit = b ? await POST('/api/schedules/fit', b) : null; v.fitErr = null; } catch (e) { v.fit = null; v.fitErr = e.message; }
}
function fitNote(v) {
  if (v.fitErr) return html`<div class="note bad">${v.fitErr}</div>`;
  const f = v.fit;
  if (!f) return '';
  const e = f.estimate;
  if (!e.batches) return html`<div class="note">Nothing is eligible for ${v.form.stage} in that scope right now — it may be by the time the window opens.</div>`;
  const basis = e.basis === 'measured' ? 'measured here: ' + fmtDur(e.batch_seconds) + ' per batch over ' + plural(e.samples, 'batch', 'batches') : 'default ' + fmtDur(e.batch_seconds) + ' per batch until batches have run here';
  if (f.window_seconds === undefined) return html`<div class="note">Needs about <b>${fmtDur(e.seconds)}</b> (${plural(e.batches, 'batch', 'batches')} in ${plural(e.rounds, 'round')}; ${basis}).</div>`;
  const fits = f.fits;
  const bar = Math.min(100, Math.round(100 * e.seconds / Math.max(1, f.window_seconds)));
  return html`<div class="fit ${fits ? 'ok' : 'warn'}"><div class="fitbar"><span style="width:${bar}%"></span></div>
    <div class="small">${fits ? html`<b>Fits.</b> About ${fmtDur(e.seconds)} of a ${fmtDur(f.window_seconds)} window.`
      : html`<b>Probably will not finish.</b> About ${fmtDur(e.seconds)} needed for a ${fmtDur(f.window_seconds)} window: what is left when it closes waits for the next one.`}
    <span class="muted"> ${plural(e.batches, 'batch', 'batches')} · ${basis}.</span></div></div>`;
}
function timeline(rows) {
  const now = Date.now(), span = 7 * 86400e3, start = now - 6 * 3600e3, end = start + span;
  const vis = rows.filter((x) => parseTs(x.end_at) > start && parseTs(x.start_at) < end);
  const x = (t) => Math.max(0, Math.min(100, (100 * (t - start)) / span));
  const days = [];
  for (let d = new Date(start); d < end; d = new Date(d.getTime() + 86400e3)) {
    const mid = new Date(d); mid.setHours(0, 0, 0, 0);
    if (mid.getTime() > start) days.push(mid);
  }
  return html`<div class="tline">
    ${days.map((d) => html`<div class="tick" style="left:${x(d.getTime())}%"><span>${d.toLocaleDateString([], { weekday: 'short', day: 'numeric' })}</span></div>`)}
    <div class="now" style="left:${x(now)}%" title="now"></div>
    ${vis.map((w, i) => html`<div class="win ${SCHED_TONE[w.state] || ''}" style="left:${x(parseTs(w.start_at))}%;width:${Math.max(0.6, x(parseTs(w.end_at)) - x(parseTs(w.start_at)))}%;top:${24 + (i % 3) * 24}px"
      title="#${w.id} ${w.stage} ${scheduleScope(w.body)} · ${fmtTime(w.start_at, true)} → ${fmtTime(w.end_at, true)} · ${w.state}"><span class="lbl">#${w.id} ${w.stage} ${scheduleScope(w.body)}</span></div>`)}
    ${vis.length ? '' : html`<div class="tiny faint" style="position:absolute;left:0;right:0;top:40px;text-align:center">No windows in the next week.</div>`}</div>`;
}

// ================================================================ SETTINGS
VIEWS.settings = view({
  title: 'Settings', sub: 'Guardrails, notifications and people — shared by the console and the CLI',
  init(v) { v.changes = {}; v.newCh = v.newCh || { type: 'teams', name: '', url: '', events: new Set() }; },
  async load(v) {
    const [s, u] = await Promise.all([GET('/api/settings'), can('admin') ? GET('/api/users') : null]);
    v.s = s; v.users = u ? u.users : null; v.roles = u ? u.roles : [];
    v.notify = JSON.parse(JSON.stringify(s.notify || []));
    v.notifySaved = JSON.stringify(v.notify);
    v.changes = {};
  },
  paint(v) {
    const admin = can('admin');
    const groups = {};
    for (const r of v.s.settings) (groups[r.group] = groups[r.group] || []).push(r);
    const dirty = Object.keys(v.changes).length + (JSON.stringify(v.notify) !== v.notifySaved ? 1 : 0);
    return html`
      ${admin ? '' : html`<div class="note warn mb">You are signed in as <b>${me().name}</b> (${me().role}). Only an admin can change settings; they are shown read-only.</div>`}
      <div class="note info mb">Values here override the TOML file for this workspace, for the console and the CLI alike. <b>↺</b> returns a setting to the file's value. Every change is recorded in the event log with who made it.</div>
      <div class="grid two settings-grid">${Object.entries(groups).map(([g, rows]) => html`<div class="card"><div class="card-h"><h3>${g} ${tip(SETTING_TIPS[g] || 'workspace_setting')}</h3></div>
        <div class="card-b stack" style="gap:14px">${rows.map((r) => settingField(v, r, admin))}</div></div>`)}</div>
      ${notifyCard(v, admin)}
      ${admin ? usersCard(v) : ''}
      ${admin && dirty ? html`<div class="callout stickybar" style="background:var(--panel)"><div class="ic">${icon('gear')}</div>
        <div class="grow"><h3>${plural(dirty, 'unsaved change')}</h3><p>Saved changes apply to the next run; a run in progress keeps its settings.</p></div>
        <button class="btn" data-act="discard">Discard</button><button class="btn primary" data-act="save">Save</button></div>` : ''}`;
  },
  act: {
    set(t, e, v) {
      const k = t.dataset.k, r = v.s.settings.find((x) => x.key === k);
      let val = t.type === 'checkbox' ? t.checked : t.value;
      if (r.kind === 'stages') {
        const cur = new Set(k in v.changes ? v.changes[k] : r.value);
        if (t.checked) cur.add(t.dataset.stage); else cur.delete(t.dataset.stage);
        val = ['import', 'commit', 'rollback'].filter((s) => cur.has(s));
      }
      if (JSON.stringify(val) === JSON.stringify(r.value) || String(val) === String(r.value)) delete v.changes[k]; else v.changes[k] = val;
      v.render();
    },
    toggle(t, e, v) {
      const k = t.dataset.k, r = v.s.settings.find((x) => x.key === k);
      const cur = k in v.changes ? v.changes[k] : r.value;
      if (!cur === r.value) delete v.changes[k]; else v.changes[k] = !cur;
      v.render();
    },
    choice(t, e, v) {
      const k = t.dataset.k, r = v.s.settings.find((x) => x.key === k);
      if (t.dataset.val === r.value) delete v.changes[k]; else v.changes[k] = t.dataset.val;
      v.render();
    },
    async reset(t, e, v) {
      await PUT('/api/settings', { changes: { [t.dataset.k]: null } });
      toast('Reset to the config file', t.dataset.k, 'ok', 2500);
      await refreshInfo();
      v.refresh();
    },
    discard(t, e, v) { v.changes = {}; v.notify = JSON.parse(v.notifySaved); v.render(); },
    async save(t, e, v) {
      const changes = Object.assign({}, v.changes);
      if (JSON.stringify(v.notify) !== v.notifySaved) changes.notify = v.notify;
      const risky = changes.commit_action === 'Auto' || changes.require_precheck === false || changes.failure_rate_abort > 0.5;
      if (risky) {
        const ok = await modal({ title: 'Loosen a guardrail?', danger: true, ic: 'triage', confirm: 'Save anyway',
          body: html`<p>${changes.commit_action === 'Auto' ? html`<b>commitAction Auto</b> commits successful imports at once: they cannot be handed back. ` : ''}
            ${changes.require_precheck === false ? html`<b>Imports without a passed precheck</b> skip the operator's validation. ` : ''}
            ${changes.failure_rate_abort > 0.5 ? html`A <b>circuit breaker above 50%</b> lets most of a wave fail before stopping. ` : ''}</p>` });
        if (!ok) return;
      }
      await PUT('/api/settings', { changes });
      toast('Settings saved', Object.keys(changes).join(', '), 'ok');
      await refreshInfo();
      v.refresh();
    },
    chAdd(t, e, v) {
      const c = v.newCh;
      const ch = { type: c.type, name: c.name || c.type, events: Array.from(c.events), enabled: true };
      if (c.type === 'email') Object.assign(ch, { smtp_host: c.smtp_host || '', smtp_port: +c.smtp_port || 587, from: c.from || '', to: (c.to || '').split(',').map((x) => x.trim()).filter(Boolean), username: c.username || '', password_env: c.password_env || '' });
      else ch.url = c.url || '';
      v.notify.push(ch);
      v.newCh = { type: c.type, name: '', url: '', events: new Set() };
      v.render();
    },
    chDel(t, e, v) { v.notify.splice(+t.dataset.i, 1); v.render(); },
    chToggle(t, e, v) { const ch = v.notify[+t.dataset.i]; ch.enabled = ch.enabled === false; v.render(); },
    chField(t, e, v) { v.newCh[t.dataset.k] = t.value; if (t.tagName === 'SELECT') v.render(); },
    chFieldIn(t, e, v) { v.newCh[t.dataset.k] = t.value; },
    chEvent(t, e, v) { const s = v.newCh.events; if (s.has(t.dataset.ev)) s.delete(t.dataset.ev); else s.add(t.dataset.ev); v.render(); },
    async chTest(t, e, v) {
      const channels = t.dataset.i !== undefined ? [v.notify[+t.dataset.i]] : v.notify;
      t.disabled = true;
      try {
        const r = await POST('/api/settings/notify-test', { channels });
        const bad = r.results.filter((x) => x.level !== 'info');
        toast(bad.length ? 'Some channels failed' : 'Test sent', r.results.map((x) => x.message).join(' · ') || 'no enabled channel', bad.length ? 'bad' : 'ok', 9000);
      } finally { t.disabled = false; }
    },
    async userAdd(t, e, v) {
      const name = ($('#u-name').value || '').trim(), role = $('#u-role').value;
      if (!name) { toast('Enter a user name', '', 'warn'); return; }
      const r = await POST('/api/users', { name, role });
      const link = location.origin + '/#t=' + encodeURIComponent(r.token);
      await modal({ title: 'Personal link for ' + name, ic: 'key', confirm: null, cancel: 'Done', wide: true,
        body: html`<p>Send this to <b>${name}</b> (${role}). It is shown <b>once</b>; issuing a new link for the same name retires this one.</p>
          <div class="linkbox"><input class="input mono" readonly value="${link}" id="u-link" onfocus="this.select()"><button class="btn" data-act="copyLink">Copy</button></div>
          <p class="small muted">${role === 'viewer' ? 'A viewer can look at everything and change nothing.' : role === 'operator' ? 'An operator runs discovery, prechecks and imports, and approves other people’s requests.' : 'An admin can also change settings and manage people.'}</p>` });
      v.refresh();
    },
    async userToggle(t, e, v) {
      await POST('/api/users/' + encodeURIComponent(t.dataset.name) + '/' + t.dataset.to, {});
      v.refresh();
    },
  },
});
Object.assign(GLOBAL_ACT, {
  copyLink() {
    const el = $('#u-link');
    if (!el) return;
    el.select();
    (navigator.clipboard ? navigator.clipboard.writeText(el.value) : Promise.reject()).then(
      () => toast('Link copied', '', 'ok', 2000), () => { document.execCommand && document.execCommand('copy'); toast('Link selected — copy it with Ctrl+C', '', 'info', 3000); });
  },
});
async function refreshInfo() {
  try { S.info = await GET('/api/info'); renderTopbar(); } catch (e) { /* keep the old info */ }
}
const SETTING_TIPS = { Pacing: 'parallel', Safety: 'circuit_breaker', Governance: 'approval', Applications: 'app', Verification: 'verification' };
function settingField(v, r, admin) {
  const val = r.key in v.changes ? v.changes[r.key] : r.value;
  const changed = r.key in v.changes;
  const dis = attr(!admin, 'disabled');
  let input;
  if (r.kind === 'bool') {
    input = html`<button class="switch ${val ? 'on' : ''}" data-act="toggle" data-k="${r.key}" role="switch" aria-checked="${!!val}" aria-label="${r.label}" ${dis}></button>`;
  } else if (r.kind.startsWith('choice:')) {
    input = html`<div class="seg">${r.kind.slice(7).split(',').map((c) => html`<button class="${val === c ? 'on' : ''}" data-act="choice" data-k="${r.key}" data-val="${c}" ${dis}>${c}</button>`)}</div>`;
  } else if (r.kind === 'stages') {
    input = html`<div class="row wrap" style="gap:12px">${['import', 'commit', 'rollback'].map((s) => html`<label class="check"><input type="checkbox" data-change="set" data-k="${r.key}" data-stage="${s}" ${attr((val || []).includes(s), 'checked')} ${dis}> ${s}</label>`)}</div>`;
  } else if (r.kind === 'ports') {
    input = html`<input class="input" id="set-${r.key}" data-input="set" data-k="${r.key}" value="${Array.isArray(val) ? val.join(', ') : val}" placeholder="none" ${dis}>`;
  } else {
    const num = r.kind === 'int' || r.kind === 'float';
    input = html`<input class="input ${num ? 'num' : ''}" id="set-${r.key}" ${num ? raw('type="number"') : ''} ${r.min !== null && num ? raw('min="' + r.min + '"') : ''} ${r.max !== null && num ? raw('max="' + r.max + '"') : ''}
      ${r.kind === 'float' ? raw('step="0.05"') : ''} data-input="set" data-k="${r.key}" value="${val === null || val === undefined ? '' : val}" style="max-width:${num ? '140px' : '320px'}" ${dis}>`;
  }
  const fileVal = Array.isArray(r.file_value) ? (r.file_value.join(', ') || 'none') : String(r.file_value === '' ? '(empty)' : r.file_value);
  return html`<div class="setting ${changed ? 'changed' : ''} ${r.overridden ? 'over' : ''}">
    <div class="sl"><b>${r.label}</b><div class="small muted">${r.help}</div>
      <div class="tiny faint">${r.key} · file: ${fileVal}${r.min !== null && r.max !== null && (r.kind === 'int' || r.kind === 'float') ? ' · allowed ' + r.min + '–' + r.max : ''}</div></div>
    <div class="sv">${input}${r.overridden && admin ? html`<button class="btn xs ghost" data-act="reset" data-k="${r.key}" title="Back to the file value (${fileVal})">↺</button>` : ''}
      ${r.overridden ? html`<span class="tag info" title="overrides the config file">workspace</span>` : ''}</div></div>`;
}
function notifyCard(v, admin) {
  const c = v.newCh, ev = v.s.events;
  const email = c.type === 'email';
  return html`<div class="card mt"><div class="card-h"><h3>Notifications ${tip('notify')}</h3><span class="sub">Teams, Slack, a webhook or email — when runs finish, fail, need a decision</span>
      <div class="tools">${v.notify.length ? html`<button class="btn sm" data-act="chTest" ${attr(!admin, 'disabled')}>${icon('bell')} Send a test to all</button>` : ''}</div></div>
    ${v.notify.length ? html`<div class="table-wrap"><table class="t compact"><thead><tr><th>Channel</th><th>Type</th><th>Where</th><th>Events</th><th>On</th><th></th></tr></thead><tbody>
      ${v.notify.map((ch, i) => html`<tr><td><b>${ch.name}</b></td><td><span class="tag">${ch.type}</span></td>
        <td class="small mono clip" title="${ch.url || (ch.to || []).join(', ')}">${ch.type === 'email' ? (ch.to || []).join(', ') + ' via ' + ch.smtp_host : String(ch.url || '').replace(/(https?:\/\/[^/]+\/).{12,}/, '$1…')}</td>
        <td class="small">${(ch.events || []).length ? ch.events.map((e) => html`<span class="tag">${e.replace(/_/g, ' ')}</span>`) : html`<span class="muted">everything</span>`}</td>
        <td><button class="switch sm ${ch.enabled === false ? '' : 'on'}" data-act="chToggle" data-i="${i}" ${attr(!admin, 'disabled')} aria-label="Enabled"></button></td>
        <td class="right nowrap"><button class="btn xs" data-act="chTest" data-i="${i}" ${attr(!admin, 'disabled')}>Test</button>
          <button class="btn xs ghost" data-act="chDel" data-i="${i}" ${attr(!admin, 'disabled')}>${icon('x')}</button></td></tr>`)}</tbody></table></div>`
      : html`<div class="card-b small muted">No channels. Nothing is sent anywhere until you add one.</div>`}
    ${admin ? html`<div class="card-b" style="border-top:1px solid var(--line)"><div class="section-title" style="margin-top:0">Add a channel</div>
      <div class="form-grid">
        <label class="field"><span>Type</span><select class="select" data-change="chField" data-k="type">${v.s.types.map((t) => html`<option ${attr(c.type === t, 'selected')}>${t}</option>`)}</select></label>
        <label class="field"><span>Name</span><input class="input" id="ch-name" data-input="chFieldIn" data-k="name" value="${c.name}" placeholder="e.g. migration-ops"></label>
        ${email ? html`<label class="field"><span>SMTP host</span><input class="input" id="ch-smtp_host" data-input="chFieldIn" data-k="smtp_host" value="${c.smtp_host || ''}"></label>
          <label class="field"><span>Port</span><input class="input" type="number" id="ch-smtp_port" data-input="chFieldIn" data-k="smtp_port" value="${c.smtp_port || 587}"></label>
          <label class="field"><span>From</span><input class="input" id="ch-from" data-input="chFieldIn" data-k="from" value="${c.from || ''}"></label>
          <label class="field"><span>To (comma separated)</span><input class="input" id="ch-to" data-input="chFieldIn" data-k="to" value="${c.to || ''}"></label>
          <label class="field"><span>SMTP user (optional)</span><input class="input" id="ch-username" data-input="chFieldIn" data-k="username" value="${c.username || ''}"></label>
          <label class="field"><span>Password env var</span><input class="input mono" id="ch-password_env" data-input="chFieldIn" data-k="password_env" value="${c.password_env || ''}" placeholder="VCFA_SMTP_PASSWORD"><small>The password itself is never stored.</small></label>`
          : html`<label class="field" style="grid-column:span 2"><span>Webhook URL</span><input class="input mono" id="ch-url" data-input="chFieldIn" data-k="url" value="${c.url}" placeholder="https://…"></label>`}
      </div>
      <div class="mt-s"><div class="small muted mb">Events (none picked = all of them)</div><div class="chips">${Object.entries(ev).filter(([k]) => k !== 'test').map(([k, text]) =>
        html`<span class="chip ${c.events.has(k) ? 'on' : ''}" data-act="chEvent" data-ev="${k}" title="${text}">${k.replace(/_/g, ' ')}</span>`)}</div></div></div>
      <div class="card-f"><button class="btn" data-act="chAdd">${icon('plus')} Add channel</button><span class="small muted">Added channels are saved with the Save button below.</span></div>` : ''}</div>`;
}
function usersCard(v) {
  return html`<div class="card mt"><div class="card-h"><h3>People ${tip('roles')}</h3><span class="sub">personal links; everything they do is recorded under their name</span></div>
    <div class="table-wrap"><table class="t compact"><thead><tr><th>User</th><th>Role</th><th>Created</th><th>Last seen</th><th></th></tr></thead><tbody>
      <tr><td><b>owner</b> <span class="tiny faint">the link printed at start-up</span></td><td><span class="tag">admin</span></td><td></td><td></td><td></td></tr>
      ${(v.users || []).map((u) => html`<tr class="${u.disabled ? 'off' : ''}"><td><b>${u.name}</b></td><td><span class="tag ${u.role === 'admin' ? 'warn' : u.role === 'viewer' ? '' : 'info'}">${u.role}</span></td>
        <td class="small muted">${fmtTime(u.created_at, true)} by ${u.created_by || '?'}</td><td class="small muted">${u.last_seen ? fmtAgo(u.last_seen) : 'never'}</td>
        <td class="right">${u.disabled ? html`<button class="btn xs" data-act="userToggle" data-name="${u.name}" data-to="enable">Enable</button>`
          : html`<button class="btn xs danger-ghost" data-act="userToggle" data-name="${u.name}" data-to="disable">Disable</button>`}</td></tr>`)}</tbody></table></div>
    <div class="card-f"><input class="input sm" id="u-name" placeholder="name, e.g. jsmith" style="width:200px" maxlength="64">
      <select class="select" id="u-role">${v.roles.map((r) => html`<option ${attr(r === 'operator', 'selected')}>${r}</option>`)}</select>
      <button class="btn" data-act="userAdd">${icon('user')} Create link</button>
      <span class="small muted">Re-creating a name issues a fresh link and retires the old one.</span></div></div>`;
}

// ================================================================ palette
const GOV = {
  paletteItems() {
    const out = [];
    const add = (label, ic, hint, run) => out.push({ sec: 'Actions', label, ic, hint, run });
    add('Verify committed VMs', 'check', 'power, Tools, IP, ping, ports', () => startJob('verify', {}));
    add('Plan a change window…', 'calendar', 'Change control', () => go('schedule'));
    add('Review approvals', 'shield', 'two-person rule', () => go('schedule'));
    if (can('admin')) add('Notification channels…', 'bell', 'Settings', () => go('settings'));
    return out;
  },
};
window.GOV = GOV;
