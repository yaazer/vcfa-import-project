'use strict';
/* VCFA Import Console — help: the glossary behind every "?" tooltip, the
 * welcome tour, and the Help & guides page (the lab guide and README, rendered
 * offline from the copies bundled with the tool). */

// ================================================================ glossary
/* key -> {t: title, d: text (string or () => string), doc: [guide, heading slug], tone}
 * Heading slugs follow GitHub's rule (see slugify); tests check every one resolves. */
const LAB = (h) => ['lab', h];
const RM = (h) => ['readme', h];
const isAuto = () => !!(S.info && S.info.settings && S.info.settings.commit_action === 'Auto');
const GLOSSARY = {
  commit_action: {
    t: () => 'commitAction: ' + (isAuto() ? 'Auto' : 'Wait'),
    d: () => (isAuto()
      ? 'A successful import is committed at once and can never be handed back to vCenter. Failed imports can still be rolled back. For a first run, Wait is the safer choice.'
      : 'Each import holds at the commit gate: commit to finish, or roll back to hand the VMs to vCenter. The safe setting for a lab.'),
    tone: () => (isAuto() ? 'bad' : ''), doc: LAB('7-import-one-vm-held-at-the-gate'),
  },
  context: { t: 'kubectl context', d: 'The Supervisor this console talks to. Every batch is applied here — check it before an import.', doc: LAB('1-install') },
  preflight: { t: 'Preflight', d: 'The tool checking the environment, read-only: kubectl context, the Mobility Operator CRDs and pod, target namespaces, permission to create batches, and subnets. Nothing is applied.', doc: LAB('5-preflight-and-validate--nothing-applied-yet') },
  precheck: { t: 'Precheck', d: 'The operator validates each VM against its target without moving anything (precheckOnly batches). Safe to repeat. Import only takes VMs that passed.', doc: LAB('6-precheck--first-contact-with-the-operator') },
  import: { t: 'Import', d: 'Moves VMs that passed precheck into their namespace. What happens after a success depends on commitAction.', tone: 'warn', doc: RM('the-commit-gate') },
  commit: { t: 'Commit', d: 'Releases imports held by commitAction Wait. Irreversible: a committed VM can never be handed back to vCenter.', tone: 'bad', doc: LAB('9-import-and-commit-for-real') },
  rollback: { t: 'Roll back', d: 'Hands the un-committed VMs of a batch that already ran back to vCenter. Committed VMs are never touched, and rollback is never written into a new batch.', doc: LAB('8-roll-it-back--the-recovery-test') },
  abandon: { t: 'Abandon', d: 'Deletes batches with nothing in flight (precheck batches, or imports that ended). Their VMs return to pending. No rollback is involved.', doc: LAB('10b-starting-a-batch-over') },
  cleanup: { t: 'Clean up', d: 'Deletes batches whose rollback the operator confirmed. Their VMs stay rolled back and can be retried.', doc: RM('when-an-import-fails-rollback') },
  wave: { t: 'Wave', d: 'The order VMs run in: wave 1 first. A wave is a label, not a state — moving a VM between waves changes nothing on the cluster.', doc: RM('stage-the-selection-for-import') },
  namespace: { t: 'Namespace', d: 'The Supervisor namespace a VM imports into. It comes from, in order: Select, the tag map, the folder map, the portgroup map, the default. Never a guess — a VM with none is left out.', doc: RM('stage-the-selection-for-import') },
  subnet: { t: 'Subnet', d: 'The NSX Subnet (or SubnetSet) a network adapter lands on. In a VPC namespace, name the Subnet as it appears in the VPC’s own namespace. Precheck does not validate it; preflight does.', doc: LAB('4-discover-and-select-the-lab-vms') },
  folder_map: { t: 'Folder map', d: 'folder → namespace, wave and batch group. The most specific folder wins and covers its whole subtree; globs like Legacy/* work.', doc: RM('collect-by-folder') },
  portgroup_map: { t: 'Portgroup map', d: 'portgroup → subnet (and optionally namespace and wave). A network with no entry leaves that adapter without a subnet.', doc: LAB('4-discover-and-select-the-lab-vms') },
  tag_map: { t: 'Tag map', d: 'vCenter tag (Category:Tag) → namespace, wave and group. Beats the folder map; an exact tag beats a glob. Optional.', doc: RM('vcenter-tags-and-applications') },
  batch_group: { t: 'Batch group', d: 'VMs only share a batch within one namespace, wave and group. It defaults to the folder, so a batch never mixes folders.', doc: RM('what-gets-generated') },
  device_key: { t: 'Device key', d: 'The adapter’s key as vCenter reports it (4000, 4001…). It goes into the manifest; an adapter that was ever re-added may not be 4000.', doc: LAB('4-discover-and-select-the-lab-vms') },
  subnet_kind: { t: 'Kind and API group', d: 'Subnet or SubnetSet, and its API group (crd.nsx.vmware.com). Leave blank to use the config default.', doc: RM('the-inventory-csv') },
  default_namespace: { t: 'Default namespace', d: 'Only for VMs nothing else placed. Leave it empty to have them reported and left out instead.' },
  default_wave: { t: 'Default wave', d: 'The wave for VMs that no map or setting gave one.' },
  batch_size: { t: 'Batch size', d: 'VMs per ImportOperationBatch. Small batches are easier to inspect and roll back; large ones finish a wave in fewer rounds.', doc: RM('running-1800-vms') },
  parallel: { t: 'Parallel batches', d: 'Batches in flight at once, cluster-wide (with a per-namespace cap). Faster, at the cost of more load on vCenter and the operator.', doc: RM('running-1800-vms') },
  limit: { t: 'Max VMs per wave', d: 'Takes at most this many VMs of each wave in this run — a pilot of ten, say. Empty means all.', doc: RM('running-1800-vms') },
  include_failed: { t: 'Retry failures', d: 'Also takes VMs that failed this stage before. Each try counts toward max_retries.' },
  dry_run: { t: 'Dry run', d: 'Renders and validates the manifests but applies nothing to the cluster. Needs no approval.' },
  rollback_failed: { t: 'Roll back failures after the run', d: 'When the run ends, every failed import is handed back to vCenter in the same job.', doc: RM('rolling-back-as-part-of-the-run') },
  no_precheck: { t: 'Import without precheck', d: 'Lets pending VMs import without passing precheck, skipping the operator’s validation. Only for VMs you already know are fine.', tone: 'bad' },
  skip_targets: { t: 'Skip namespace and subnet checks', d: 'Preflight still checks the cluster and the operator, but not each target namespace and subnet — for when they are not created yet.' },
  folder_scope: { t: 'Folder scope', d: 'Runs only the VMs in this vCenter folder (subfolders included unless you untick it). Folders are the unit of execution.', doc: RM('run-by-folder') },
  circuit_breaker: { t: 'Circuit breaker', d: 'A wave halts once this share of its results has failed (after a minimum sample). Batches in flight finish; nothing new is applied.', doc: RM('safety') },
  watch: { t: 'Watch', d: 'Polls batches already on the cluster — from an earlier session or another run — until they finish, recording each result.' },
  refresh: { t: 'Refresh once', d: 'One poll of every live batch, to pick up results that arrived while nothing was watching.' },
  stop: { t: 'Stop', d: 'No new batches are applied; those on the cluster are polled to completion. Nothing is rolled back.' },
  eta: { t: 'Time estimate', d: 'Batches laid out against the parallel limits, times the median batch time measured in this workspace — a conservative default until batches have run here.', doc: RM('time-estimates') },
  readiness: { t: 'Readiness', d: 'Graded from vCenter facts before precheck: ready, check first, or likely to fail, with the reason and the fix. Advice only — the operator’s precheck decides.', doc: RM('readiness-before-precheck') },
  app: { t: 'Application', d: 'From the vCenter tag category set in Settings, or set by hand. With “keep apps together” an application moves and imports as one.', doc: RM('vcenter-tags-and-applications') },
  app_together: { t: 'Move whole apps', d: 'Moving one VM of an application moves all of it, so an app never ends up split across waves.', doc: RM('vcenter-tags-and-applications') },
  locked: { t: 'Locked', d: 'The VM is in a batch or committed: its wave and namespace can no longer change here.' },
  verification: { t: 'Post-import verification', d: 'Checks each committed VM: powered on, VMware Tools running, the same IP as before, ping, and any TCP ports from Settings. It never changes a VM’s state.', doc: RM('post-import-verification') },
  change_window: { t: 'Change window', d: 'Runs a precheck or import unattended between two times, and never starts a batch that could not finish before the end.', doc: RM('change-windows') },
  approval: { t: 'Two-person rule', d: 'A gated step becomes a request that someone other than the requester approves. It then runs once, on behalf of both.', doc: RM('the-two-person-rule') },
  roles: { t: 'Roles', d: 'viewer looks and changes nothing; operator runs imports and approves other people’s requests; admin also changes settings and manages people.', doc: RM('people-roles-and-read-only-links') },
  notify: { t: 'Notifications', d: 'Teams, Slack, a webhook or email when runs finish, fail, or need a decision. Delivery failures are logged and never stop a run.', doc: RM('notifications') },
  workspace_setting: { t: 'Workspace settings', d: 'Override the TOML file for this workspace, for the console and the CLI alike. ↺ returns a setting to the file’s value.', doc: RM('workspace-settings') },
  insecure: { t: 'Skip TLS verification', d: 'Accepts vCenter’s certificate without checking it. Fine for a lab with a self-signed certificate, not for production.', tone: 'warn' },
  remember: { t: 'Keep credentials in memory', d: 'Lets post-import verification query vCenter. Held in memory only, never written to disk, and gone when the console stops.' },
  vc_password: { t: 'vCenter password', d: 'Used for this discovery and never stored. Read-only credentials are enough.' },
  concurrency: { t: 'Parallel detail reads', d: 'How many VM detail calls run at once against vCenter. Lower it if vCenter is slow or rate-limits the API.' },
  powered_on: { t: 'Powered-on VMs only', d: 'Leaves powered-off VMs out of the inventory. Tools cannot report while a VM is off.' },
  no_tools: { t: 'Skip the VM Tools check', d: 'Faster discovery, but the Tools column and readiness cannot warn about VMs whose Tools are not running.' },
  no_tags: { t: 'Skip vCenter tags', d: 'Tags feed the tag filter, the tag map and applications. Skipping them saves a few calls.' },
  attempts: { t: 'Attempts', d: 'How many times the VM went into a batch. Automatic retries stop at max_retries; a forced retry goes past it.' },
  stalled: { t: 'Stalled', d: 'The batch passed batch_timeout_minutes without a verdict, so nothing polls it. Refresh to pick up a late result; roll back if it stays stuck.' },
  ledger: { t: 'Ledger', d: 'Append-only JSONL of every state change with who made it. It survives a rebuilt database; ship it to a SIEM or a change record.', doc: RM('the-tracker') },
  cluster_namespaces: { t: 'Namespaces you can map to', d: 'Read from the Supervisor. If this login may not list them (common for tenant users), they come from your kubeconfig instead: kubectl vsphere login adds a context for each namespace you can use. Click one to fill the namespace field you last clicked; names not in the list are flagged.', doc: RM('stage-the-selection-for-import') },
  cluster_subnets: { t: 'Subnets you can map to', d: 'The Subnets and SubnetSets on the Supervisor, each with the namespace it lives in — a VPC namespace’s subnets live in the VPC’s own namespace, and the map names them as they appear there. When this login may not list them everywhere, only the namespaces it can read are shown, and names are not flagged. Picking a SubnetSet also sets that row’s kind.', doc: LAB('4-discover-and-select-the-lab-vms') },
  vcenter_portgroups: { t: 'Portgroups in vCenter', d: 'Every portgroup vCenter listed at the last discovery, including ones no VM uses yet; the number is how many selected (or discovered) VMs sit on it. Click one to fill the portgroup field you last clicked, or to add it to the portgroup map. Discover again to refresh the list.', doc: RM('stage-the-selection-for-import') },
  ns_conflict: { t: 'Conflicting namespace entries', d: 'Two sources name different namespaces for the same VM — say the folder map and the portgroup map. The first in the order Select, tag map, folder map, portgroup map wins; the others are ignored. Name a VM’s namespace in one place, usually the folder map.', tone: 'warn', doc: RM('stage-the-selection-for-import') },
  selection: { t: 'Folders are the unit of collection', d: 'Ticking a folder selects its whole subtree. Selecting is cumulative — deselect explicitly.', doc: RM('collect-by-folder') },
};
const STATE_TIPS = {
  pending: 'Queued. Next step: precheck.',
  precheck_running: 'In a precheck batch; the operator is validating it. Nothing moves.',
  precheck_passed: 'The operator validated it. Ready to import.',
  precheck_failed: 'The operator found a problem. See Triage for the cause and fix, then retry.',
  importing: 'In an import batch on the cluster.',
  awaiting_commit: 'Imported and held by commitAction Wait. Commit to finish (irreversible), or roll back.',
  committed: 'Done: owned by VCF Automation. Irreversible — it can no longer be handed back to vCenter.',
  failed: 'The import failed. Roll it back to vCenter, then retry once the cause is fixed.',
  rolling_back: 'The operator is handing it back to vCenter.',
  rolled_back: 'Back in vCenter. Retry once the cause is fixed.',
  skipped: 'Left out of every precheck and import until unskipped.',
};
function glossary(key) {
  if (key.startsWith('state:')) {
    const s = key.slice(6);
    if (!STATE_TIPS[s]) return null;
    return { t: label(s), d: STATE_TIPS[s], tone: s === 'failed' || s === 'precheck_failed' ? 'bad' : s === 'awaiting_commit' ? 'warn' : '',
      doc: RM('vm-lifecycle') };
  }
  const g = GLOSSARY[key];
  if (!g) return null;
  const val = (x) => (typeof x === 'function' ? x() : x);
  return { t: val(g.t), d: val(g.d), tone: val(g.tone) || '', doc: g.doc };
}
const DOC_NAME = { lab: 'Lab guide', readme: 'README' };
const docHrefFor = (doc) => '#/help?doc=' + doc[0] + '&h=' + encodeURIComponent(doc[1]);
/** A small "?" that explains the value next to it. */
function tip(key) {
  return html`<button type="button" class="tipq" data-tip="${key}" aria-label="What is this?">?</button>`;
}

// ================================================================ tooltips
const TIP = { el: null, for: null, show: 0, hide: 0, pinned: false };
function tipEl() {
  if (!TIP.el) {
    TIP.el = document.createElement('div');
    TIP.el.id = 'tip';
    TIP.el.className = 'tip';
    TIP.el.setAttribute('role', 'tooltip');
    TIP.el.hidden = true;
    TIP.el.addEventListener('pointerenter', () => clearTimeout(TIP.hide));
    TIP.el.addEventListener('pointerleave', () => { if (!TIP.pinned) hideTipSoon(); });
    TIP.el.addEventListener('click', (e) => { if (e.target.closest('a')) hideTip(true); });
    document.body.appendChild(TIP.el);
  }
  return TIP.el;
}
function showTip(target, pin) {
  const g = glossary(target.dataset.tip || '');
  if (!g) return;
  const el = tipEl();
  clearTimeout(TIP.hide);
  TIP.for = target;
  TIP.pinned = !!pin;
  el.className = 'tip ' + (g.tone || '') + (pin ? ' pinned' : '');
  el.innerHTML = fmt(html`<div class="tip-h">${g.tone === 'bad' ? icon('triage') : ''}<b>${g.t}</b></div>
    <div class="tip-b">${g.d}</div>
    ${g.doc ? html`<a class="tip-l" href="${docHrefFor(g.doc)}">${icon('book')} ${DOC_NAME[g.doc[0]]}: read more</a>` : ''}`);
  el.hidden = false;
  target.setAttribute('aria-describedby', 'tip');
  placeTip();
}
function placeTip() {
  const el = TIP.el, t = TIP.for;
  if (!el || el.hidden || !t) return;
  if (!t.isConnected) { hideTip(true); return; }
  const r = t.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight, m = 8;
  let top = r.bottom + 8;
  if (top + h > innerHeight - m && r.top - 8 - h > m) top = r.top - 8 - h;
  let left = Math.min(Math.max(m, r.left + r.width / 2 - w / 2), innerWidth - w - m);
  el.style.top = Math.round(Math.max(m, top)) + 'px';
  el.style.left = Math.round(left) + 'px';
}
function hideTip(force) {
  if (TIP.pinned && !force) return;
  clearTimeout(TIP.show); clearTimeout(TIP.hide);
  if (TIP.for) TIP.for.removeAttribute('aria-describedby');
  TIP.for = null; TIP.pinned = false;
  if (TIP.el) TIP.el.hidden = true;
}
function hideTipSoon() { clearTimeout(TIP.hide); TIP.hide = setTimeout(() => hideTip(), 160); }
function wireTips() {
  document.addEventListener('pointerover', (e) => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (!t || TIP.pinned || t === TIP.for) { if (t && t === TIP.for) clearTimeout(TIP.hide); return; }
    clearTimeout(TIP.show);
    // Instant when moving between tips. Otherwise a pause: short for a "?" (asked
    // for), longer for a status pill or chip, so scanning a table does not flicker.
    const wait = TIP.el && !TIP.el.hidden ? 0 : t.classList.contains('tipq') ? 200 : 650;
    TIP.show = setTimeout(() => showTip(t), wait);
  });
  document.addEventListener('pointerout', (e) => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (!t) return;
    const to = e.relatedTarget;
    if (to && (t.contains(to) || (TIP.el && TIP.el.contains(to)))) return;
    clearTimeout(TIP.show);
    if (t === TIP.for) hideTipSoon();
  });
  document.addEventListener('focusin', (e) => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (t && !TIP.pinned) showTip(t);
  });
  document.addEventListener('focusout', (e) => {
    const t = e.target.closest && e.target.closest('[data-tip]');
    if (t && t === TIP.for && !TIP.pinned && !(TIP.el && TIP.el.contains(e.relatedTarget))) hideTipSoon();
  });
  // Capture phase: a "?" inside a label, chip or row must not also toggle, open or sort it.
  document.addEventListener('click', (e) => {
    const q = e.target.closest && e.target.closest('.tipq');
    if (q) {
      e.preventDefault();
      e.stopPropagation();
      if (TIP.pinned && TIP.for === q) hideTip(true); else showTip(q, true);
      return;
    }
    if (TIP.pinned && TIP.el && !TIP.el.contains(e.target)) hideTip(true);
  }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && TIP.el && !TIP.el.hidden) hideTip(true); });
  addEventListener('scroll', () => { if (TIP.pinned) placeTip(); else if (TIP.el && !TIP.el.hidden) hideTip(); }, { passive: true, capture: true });
  addEventListener('resize', placeTip, { passive: true });
  addEventListener('hashchange', () => hideTip(true));
}

// ================================================================== hints
const hintsOn = () => store.get('vcfa-hints') !== 'off';
function applyHints() { document.body.dataset.hints = hintsOn() ? 'on' : 'off'; }
function setHints(on) {
  store.set('vcfa-hints', on ? null : 'off');
  applyHints();
  toast(on ? 'Hints on' : 'Hints hidden', on ? 'The ? marks explain the value next to them.' : 'Bring them back from Help & guides or Ctrl+K.', 'info', 3000);
}

// =================================================================== tour
const TOUR_STEPS = [
  { title: 'Welcome to the VCFA Import console', center: true,
    body: () => html`<p>This console moves vCenter VMs into VCF Automation namespaces with the Mobility Operator:
      discover, pick, map, arrange waves, then precheck, import and follow up — all from here.</p>
      <p>The tour takes about two minutes and <b>changes nothing</b>.${me().role === 'viewer' ? ' Your link is read-only: you can look at everything and change nothing.' : ''}</p>` },
  { route: 'overview', sel: '#nav', title: 'The path through a migration',
    body: () => html`<p><b>Plan</b>: Discover → Select VMs → Map &amp; Stage → Waves. <b>Run</b>: Execute, then follow the Import queue and Batches.</p><p>A tick marks each step that is done.</p>` },
  { route: 'overview', sel: '#view > .callout', title: 'What to do next',
    body: () => html`<p>The Overview always opens with the next sensible step for this campaign, and the button that takes you there.</p>` },
  { route: 'overview', sel: '#fx-stream', title: 'The Migration Stream',
    body: () => html`<p>Every particle is a VM flowing from vCenter through precheck and import into VCF Automation; anything that needs attention pools underneath.</p><p>Switch the <b>lanes</b> to see progress by wave, namespace or application.</p>` },
  { route: 'discover', sel: '#view .card', title: 'Discover is read-only',
    body: () => html`<p>It reads VMs, folders, networks and tags from vCenter. The password is used once and never stored. Nothing in vCenter changes.</p>`, doc: LAB('4-discover-and-select-the-lab-vms') },
  { route: 'select', sel: '#view .tree', title: 'Folders are the unit of collection',
    body: () => html`<p>Ticking a folder selects its whole subtree. The <b>Ready</b> column warns about VMs likely to fail precheck before you spend a batch on them.</p>` },
  { route: 'stage', sel: '#cov-folders', title: 'Nothing is guessed',
    body: () => html`<p>Each folder and network shows where it lands. A VM with no namespace is reported and left out — never placed by guesswork.</p><p>In a VPC namespace, name the subnet as it appears in the VPC’s own namespace.</p>`, doc: LAB('4-discover-and-select-the-lab-vms') },
  { route: 'waves', sel: '#view .board', title: 'Waves run in order',
    body: () => html`<p>Drag VMs or whole folders between waves; wave 1 runs first. A padlock means the VM is already in a batch or committed and cannot move.</p>` },
  { route: 'execute', sel: '#view .steps', title: 'Preflight, precheck, import, commit',
    body: () => html`<p><b>Preflight</b> checks the cluster (read-only). <b>Precheck</b> has the operator validate each VM without moving it — safe to repeat. <b>Import</b> moves them. <b>Commit</b> makes it final.</p>`, doc: LAB('5-preflight-and-validate--nothing-applied-yet') },
  { route: 'execute', sel: '#ex-go', title: 'Nothing runs without a review',
    body: () => html`<p>You see the batch plan and a time estimate first; an import that commits automatically makes you type <code>IMPORT</code>.</p><p><b>Stop</b> is always safe: nothing new is applied, and what is already on the cluster finishes.</p>` },
  { route: 'execute', sel: '.ctxchip[data-tip="commit_action"]', title: 'Know your commitAction', tone: () => (isAuto() ? 'bad' : ''),
    body: () => html`<p>This workspace uses <b>${isAuto() ? 'Auto' : 'Wait'}</b>.</p><p><b>Auto</b>: a successful import is committed at once and cannot be handed back to vCenter. <b>Wait</b>: imports hold at a gate until you commit or roll back — the setting for a first run.</p>`, doc: LAB('7-import-one-vm-held-at-the-gate') },
  { route: 'execute', sel: '.nav-item[href="#/triage"]', title: 'When something fails',
    body: () => html`<p>Triage groups failures by cause, with the likely fix and the right buttons. Failed imports can be rolled back to vCenter; committed ones cannot.</p>`, doc: LAB('8-roll-it-back--the-recovery-test') },
  { route: 'execute', sel: '.nav-item[href="#/schedule"]', title: 'Change control and settings',
    body: () => html`<p><b>Change control</b> runs imports in change windows and gates risky steps behind a second person. <b>Settings</b> holds the guardrails, notifications and people. All of it is off until you turn it on.</p>` },
  { route: 'execute', sel: '[data-act="help"]', title: 'Help is always one click away',
    body: () => html`<p>Hover or tap any <span class="tipq static">?</span> for what the value next to it means. <b>Help &amp; guides</b> has the lab guide, the README and a glossary — offline.</p><p><span class="kbd">ctrl</span> <span class="kbd">k</span> jumps anywhere.</p>`, last: true },
];
const TOUR = { i: -1, target: null, raf: 0, seq: 0 };
async function startTour(at) {
  store.set('vcfa-welcome', 'done');
  hideTip(true);
  closeDrawer();
  TOUR.i = at || 0;
  document.addEventListener('keydown', tourKey, true);
  addEventListener('resize', tourPlaceSoon, { passive: true });
  addEventListener('scroll', tourPlaceSoon, { passive: true, capture: true });
  await tourStep();
}
function endTour() {
  TOUR.i = -1; TOUR.target = null; TOUR.seq++;
  document.removeEventListener('keydown', tourKey, true);
  removeEventListener('resize', tourPlaceSoon);
  removeEventListener('scroll', tourPlaceSoon, true);
  const root = $('#tour-root');
  if (root) root.remove();
  store.set('vcfa-welcome', 'done');
}
function tourKey(e) {
  if (TOUR.i < 0) return;
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); endTour(); }
  else if (e.key === 'ArrowRight' || (e.key === 'Enter' && !e.target.closest('button'))) { e.preventDefault(); e.stopPropagation(); tourGo(1); }
  else if (e.key === 'ArrowLeft') { e.preventDefault(); e.stopPropagation(); tourGo(-1); }
}
function tourGo(d) {
  const next = TOUR.i + d;
  if (next < 0) return;
  if (next >= TOUR_STEPS.length) { endTour(); toast('Tour finished', 'Help & guides has the lab guide whenever you need it.', 'ok', 4000); return; }
  TOUR.i = next;
  tourStep();
}
async function tourStep() {
  const seq = ++TOUR.seq;
  const s = TOUR_STEPS[TOUR.i];
  if (s.route && S.route !== s.route) {
    go(s.route);
    await waitUntil(() => S.route === s.route && S.view && S.view.loaded, 5000);
  }
  let target = null;
  if (s.sel) target = await waitUntil(() => { const el = $(s.sel); return el && el.getBoundingClientRect().width ? el : null; }, 2500);
  if (seq !== TOUR.seq) return;          // the reader moved on while this step was loading
  TOUR.target = target;
  if (target) target.scrollIntoView({ block: 'center', behavior: 'auto' });
  tourDraw();
}
async function waitUntil(fn, ms) {
  const t0 = Date.now();
  for (;;) {
    let v = null;
    try { v = fn(); } catch (e) { v = null; }
    if (v || Date.now() - t0 > ms) return v || null;
    await new Promise((r) => setTimeout(r, 60));
  }
}
function tourDraw() {
  let root = $('#tour-root');
  if (!root) {
    root = document.createElement('div');
    root.id = 'tour-root';
    root.addEventListener('click', tourClick);
    document.body.appendChild(root);
  }
  const s = TOUR_STEPS[TOUR.i], n = TOUR_STEPS.length, first = TOUR.i === 0;
  const tone = typeof s.tone === 'function' ? s.tone() : s.tone || '';
  const centered = s.center || !TOUR.target;
  root.innerHTML = fmt(html`<div class="tour-block ${centered ? 'dim' : ''}"></div>
    ${centered ? '' : html`<div class="tour-spot ${tone}"></div>`}
    <div class="tour-card ${tone} ${centered ? 'center' : ''}" role="dialog" aria-modal="true" aria-labelledby="tour-title">
      ${first ? html`<div class="tour-hero">${icon('book')}</div>` : html`<div class="tour-top"><span class="tour-count">${TOUR.i} / ${n - 1}</span>
        <span class="tour-dots">${TOUR_STEPS.slice(1).map((x, k) => html`<i class="${k + 1 === TOUR.i ? 'on' : k + 1 < TOUR.i ? 'done' : ''}"></i>`)}</span></div>`}
      <h3 id="tour-title">${s.title}</h3>
      <div class="tour-body">${s.body()}</div>
      ${s.doc ? html`<a class="tour-doc" href="${docHrefFor(s.doc)}" data-tour="doc">${icon('book')} ${DOC_NAME[s.doc[0]]}: read more</a>` : ''}
      <div class="tour-foot">
        ${first ? html`<button class="btn ghost" data-tour="end">Not now</button><span class="grow"></span>
          <button class="btn" data-tour="lab">${icon('book')} Open the lab guide</button>
          <button class="btn primary" data-tour="next">Take the tour ${icon('arrow')}</button>`
        : html`<button class="btn ghost sm" data-tour="end">End tour</button><span class="grow"></span>
          <button class="btn sm" data-tour="back">${icon('left')} Back</button>
          ${s.last ? html`<button class="btn sm" data-tour="lab">Lab guide</button><button class="btn sm primary" data-tour="next">Finish</button>`
            : html`<button class="btn sm primary" data-tour="next">Next ${icon('right')}</button>`}`}
      </div></div>`);
  tourPlace();
  const btn = root.querySelector('[data-tour="next"]');
  if (btn) btn.focus({ preventScroll: true });
}
function tourPlaceSoon() {
  if (TOUR.raf) return;
  TOUR.raf = requestAnimationFrame(() => { TOUR.raf = 0; tourPlace(); });
}
function tourPlace() {
  const root = $('#tour-root');
  if (!root) return;
  const card = root.querySelector('.tour-card'), spot = root.querySelector('.tour-spot');
  if (!spot || !TOUR.target) return;          // centred: CSS places the card
  if (!TOUR.target.isConnected) { const el = $(TOUR_STEPS[TOUR.i].sel); if (el) TOUR.target = el; else return; }
  const r = TOUR.target.getBoundingClientRect(), pad = 6, m = 12;
  // A very tall target (the nav, a board) is framed by its visible part.
  const top = Math.max(m, r.top - pad), bottom = Math.min(innerHeight - m, r.bottom + pad);
  Object.assign(spot.style, { left: (r.left - pad) + 'px', top: top + 'px', width: (r.width + pad * 2) + 'px', height: Math.max(24, bottom - top) + 'px' });
  const w = card.offsetWidth, h = card.offsetHeight;
  let x, y;
  if (r.right + 16 + w < innerWidth - m) { x = r.right + 16; y = r.top; }                 // right of it
  else if (r.left - 16 - w > m) { x = r.left - 16 - w; y = r.top; }                        // left of it
  else if (bottom + 16 + h < innerHeight - m) { x = r.left; y = bottom + 16; }              // below
  else { x = r.left; y = top - 16 - h; }                                                     // above
  x = Math.min(Math.max(m, x), innerWidth - w - m);
  y = Math.min(Math.max(m, y), innerHeight - h - m);
  Object.assign(card.style, { left: Math.round(x) + 'px', top: Math.round(y) + 'px' });
}
function tourClick(e) {
  const b = e.target.closest('[data-tour]');
  if (!b) return;
  e.preventDefault();
  const what = b.dataset.tour;
  if (what === 'next') tourGo(1);
  else if (what === 'back') tourGo(-1);
  else if (what === 'end') endTour();
  else if (what === 'lab') { endTour(); go('help', 'doc=lab'); }
  else if (what === 'doc') { const href = b.getAttribute('href'); endTour(); location.hash = href; }
}
function maybeWelcome() {
  if (S.gated || store.get('vcfa-welcome')) return;
  setTimeout(() => { if (!S.gated && TOUR.i < 0 && !$('#modal-root').innerHTML) startTour(0); }, 400);
}

// =============================================================== markdown
/* A small, strict Markdown renderer for the bundled guides: headings (with
 * GitHub-style ids), paragraphs, lists (nested), fenced code, tables,
 * blockquotes, rules; inline code, bold, italic and links. Everything is
 * escaped; links only go to http(s) or to another place in the guides. */
function slugify(text) {
  return String(text).trim().toLowerCase().replace(/[^\p{L}\p{N}_\- ]/gu, '').replace(/ /g, '-');
}
function mdHref(u, doc) {
  if (/^https?:\/\//i.test(u)) return { url: u, ext: true };
  let m = u.match(/^#(.+)$/);
  if (m) return { url: '#/help?doc=' + doc + '&h=' + encodeURIComponent(m[1]) };
  m = u.match(/^(?:\.\/)?(README|LAB-GUIDE)\.md(?:#(.+))?$/i);
  if (m) return { url: '#/help?doc=' + (m[1].toUpperCase() === 'README' ? 'readme' : 'lab') + (m[2] ? '&h=' + encodeURIComponent(m[2]) : '') };
  return null;
}
function mdInline(text, doc) {
  const codes = [], links = [];
  const basic = (s) => esc(s)
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^\w*])\*([^*\s][^*]*?)\*(?!\w)/g, '$1<i>$2</i>')
    .replace(/(^|[^\w])_([^_\s][^_]*?)_(?!\w)/g, '$1<i>$2</i>')
    .replace(/\u0000(\d+)\u0000/g, (m, k) => codes[+k]);
  let s = String(text).replace(/`([^`]+)`/g, (m, c) => { codes.push('<code>' + esc(c) + '</code>'); return '\u0000' + (codes.length - 1) + '\u0000'; });
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, t, u) => {
    const h = mdHref(u, doc);
    links.push(h ? '<a href="' + esc(h.url) + '"' + (h.ext ? ' target="_blank" rel="noopener noreferrer"' : '') + '>' + basic(t) + '</a>' : basic(t));
    return '\u0001' + (links.length - 1) + '\u0001';
  });
  return basic(s).replace(/\u0001(\d+)\u0001/g, (m, k) => links[+k]);
}
function mdCells(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const out = [];
  let cur = '';
  for (let k = 0; k < s.length; k++) {
    if (s[k] === '\\' && s[k + 1] === '|') { cur += '|'; k++; } else if (s[k] === '|') { out.push(cur.trim()); cur = ''; } else cur += s[k];
  }
  out.push(cur.trim());
  return out;
}
const MD_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
function mdList(lines, i, doc) {
  const first = lines[i].match(MD_ITEM);
  const indent = first[1].length, ordered = /\d/.test(first[2]);
  const items = [];
  while (i < lines.length) {
    const m = lines[i].match(MD_ITEM);
    if (!m || m[1].length !== indent || /\d/.test(m[2]) !== ordered) break;
    const body = [m[3]];
    i++;
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) {
        let j = i + 1;
        while (j < lines.length && !lines[j].trim()) j++;
        if (j < lines.length && lines[j].match(/^\s*/)[0].length > indent) { body.push(''); i = j; continue; }
        break;
      }
      const ind = l.match(/^\s*/)[0].length;
      if (ind > indent) { body.push(l.slice(Math.min(ind, indent + 2))); i++; continue; }
      if (MD_ITEM.test(l) || /^\s*(#|```|\|)/.test(l)) break;
      body.push(l.trim());                      // lazy continuation of the item's paragraph
      i++;
    }
    items.push(mdBlocks(body.join('\n'), doc).html.replace(/^<p>([\s\S]*?)<\/p>/, '$1'));
  }
  const tag = ordered ? 'ol' : 'ul';
  return { html: '<' + tag + '>' + items.map((x) => '<li>' + x + '</li>').join('') + '</' + tag + '>', next: i };
}
function mdBlocks(src, doc) {
  const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
  const out = [], toc = [], para = [];
  const flush = () => { if (para.length) { out.push('<p>' + mdInline(para.join(' '), doc) + '</p>'); para.length = 0; } };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let m = line.match(/^\s*```\s*([\w+-]*)\s*$/);
    if (m) {
      flush();
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      out.push('<pre class="code"><code>' + esc(buf.join('\n')) + '</code></pre>');
      continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    m = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (m) {
      flush();
      const lvl = m[1].length, id = slugify(m[2]);
      out.push('<h' + lvl + ' id="h-' + esc(id) + '">' + mdInline(m[2], doc) + '</h' + lvl + '>');
      if (lvl === 2 || lvl === 3) toc.push({ lvl, text: m[2].replace(/`/g, ''), id });
      i++;
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flush(); out.push('<hr>'); i++; continue; }
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      flush();
      const head = mdCells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) { rows.push(mdCells(lines[i])); i++; }
      out.push('<div class="table-wrap doc-tw"><table class="t compact"><thead><tr>' + head.map((c) => '<th>' + mdInline(c, doc) + '</th>').join('') +
        '</tr></thead><tbody>' + rows.map((r) => '<tr>' + r.map((c) => '<td>' + mdInline(c, doc) + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>');
      continue;
    }
    if (/^\s*>/.test(line)) {
      flush();
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
      out.push('<blockquote>' + mdBlocks(buf.join('\n'), doc).html + '</blockquote>');
      continue;
    }
    if (MD_ITEM.test(line)) { flush(); const r = mdList(lines, i, doc); out.push(r.html); i = r.next; continue; }
    para.push(line.trim());
    i++;
  }
  flush();
  return { html: out.join('\n'), toc };
}

// =============================================================== HELP page
const HELP = { docs: {}, rendered: {} };
async function loadDoc(name) {
  if (!HELP.rendered[name]) {
    const d = await GET('/api/docs/' + name);
    HELP.docs[name] = d;
    HELP.rendered[name] = mdBlocks(d.markdown, name);
  }
  return HELP.rendered[name];
}
VIEWS.help = view({
  title: 'Help & guides', sub: 'The tour, the lab guide, the README and a glossary — all offline',
  init(v, p) { v.tab = ['lab', 'readme'].includes(p.doc) ? p.doc : (p.tab === 'glossary' ? 'glossary' : 'start'); v.h = p.h || null; v.q = v.q || ''; v.docErr = null; },
  async load(v) {
    v.docErr = null;
    if (v.tab === 'lab' || v.tab === 'readme') {
      try { v.doc = await loadDoc(v.tab); } catch (e) { v.doc = null; v.docErr = e.message; }
    }
  },
  paint(v) {
    const tabs = [['start', 'Getting started'], ['lab', 'Lab guide'], ['readme', 'README'], ['glossary', 'Glossary']];
    return html`<div class="tabs">${tabs.map(([k, l]) => html`<button class="${v.tab === k ? 'on' : ''}" data-act="helpTab" data-k="${k}">${l}</button>`)}</div>
      ${v.tab === 'start' ? helpStart() : v.tab === 'glossary' ? helpGlossary(v) : helpDoc(v)}`;
  },
  after(v) {
    if (!v.h || !v.doc) return;
    const el = document.getElementById('h-' + v.h);
    v.h = null;
    if (!el) return;
    el.scrollIntoView({ block: 'start' });
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 2200);
  },
  act: {
    helpTab(t, e, v) { go('help', t.dataset.k === 'start' ? '' : t.dataset.k === 'glossary' ? 'tab=glossary' : 'doc=' + t.dataset.k); },
    gq: debounce((t, e, v) => { v.q = t.value; const el = $('#gloss-list'); if (el) mount(el, glossaryList(v)); }, 120),
    tour: () => startTour(0),
    hints: (t) => { setHints(!hintsOn()); if (S.view) S.view.render(); },
  },
});
function helpStart() {
  const step = (n2, to, title, text, key) => html`<div class="gs-step" data-act="go" data-to="${to}"><span class="n">${n2}</span>
    <div class="grow"><b>${title}</b> ${key ? tip(key) : ''}<div class="small muted">${text}</div></div>${icon('chevron')}</div>`;
  return html`<div class="grid two">
    <div class="card"><div class="card-h"><h3>New here?</h3></div>
      <div class="card-b stack"><p class="muted" style="margin:0">A two-minute tour of the console: where things are, what is safe, and what is not. It changes nothing.</p>
        <div class="row wrap"><button class="btn primary" data-act="tour">${icon('book')} Take the tour</button>
          <a class="btn" href="#/help?doc=lab">Open the lab guide</a></div></div></div>
    <div class="card"><div class="card-h"><h3>Hints</h3></div>
      <div class="card-b stack"><div class="opt-row"><div><b>Show the <span class="tipq static">?</span> marks</b><div class="d">Hover or tap one for what the value next to it means, with a link into the guides.
        Status pills explain themselves too.</div></div>
        <button class="switch ${hintsOn() ? 'on' : ''}" data-act="hints" role="switch" aria-checked="${hintsOn()}" aria-label="Show hints"></button></div></div></div>
  </div>
  <div class="card mt"><div class="card-h"><h3>Your first import, step by step</h3><span class="sub">each step opens its page</span></div>
    <div class="card-b stack" style="gap:8px">
      ${step(1, 'discover', 'Discover', 'Read the inventory from vCenter. Read-only; the password is never stored.', 'vc_password')}
      ${step(2, 'select', 'Select VMs', 'Tick folders (a folder takes its subtree). Check the Ready column.', 'readiness')}
      ${step(3, 'stage', 'Map & Stage', 'Map folders to namespaces and portgroups to subnets. Unmapped VMs are left out, never guessed.', 'namespace')}
      ${step(4, 'waves', 'Waves', 'Put the pilot VMs in wave 1. Waves run in order.', 'wave')}
      ${step(5, 'execute', 'Preflight', 'Check the cluster, read-only.', 'preflight')}
      ${step(6, 'execute', 'Precheck', 'The operator validates each VM without moving it.', 'precheck')}
      ${step(7, 'execute', 'Import', 'With commitAction Wait, imports hold at the gate for you to commit or roll back.', 'commit_action')}
      ${step(8, 'triage', 'Triage', 'Anything that failed, grouped by cause with the fix.', 'rollback')}
    </div></div>
  <div class="grid two mt">
    <div class="card"><div class="card-h"><h3>Keyboard</h3></div><div class="card-b"><dl class="facts">
      <dt><span class="kbd">ctrl</span> <span class="kbd">k</span> or <span class="kbd">/</span></dt><dd>Command palette: any page, VM, batch or action</dd>
      <dt><span class="kbd">esc</span></dt><dd>Close a drawer, dialog, tooltip or the tour</dd>
      <dt><span class="kbd">←</span> <span class="kbd">→</span></dt><dd>Step through the tour</dd>
      <dt>shift-click</dt><dd>Select a range of rows in Select VMs</dd></dl></div></div>
    <div class="card"><div class="card-h"><h3>Where things are kept</h3></div><div class="card-b"><dl class="facts">
      <dt>workdir</dt><dd class="mono small">${S.info.workdir}</dd>
      <dt>State</dt><dd>state.db — the queue, batches and every transition</dd>
      <dt>Ledger ${tip('ledger')}</dt><dd>ledger.jsonl — append-only, every state change</dd>
      <dt>Job logs</dt><dd>jobs/ — every console job’s full log</dd></dl></div></div>
  </div>`;
}
function helpDoc(v) {
  if (v.docErr) return html`<div class="note bad">${v.docErr}</div>`;
  const d = v.doc;
  return html`<div class="grid side docgrid">
    <div class="card toc-card"><div class="card-h"><h3>${HELP.docs[v.tab].title}</h3><span class="sub mono tiny">${HELP.docs[v.tab].file}</span></div>
      <nav class="toc" data-scroll="toc">${d.toc.map((x) => html`<a class="l${x.lvl}" href="#/help?doc=${v.tab}&h=${encodeURIComponent(x.id)}">${x.text}</a>`)}</nav></div>
    <div class="card"><article class="doc card-b">${raw(d.html)}</article></div></div>`;
}
function glossaryList(v) {
  const q = v.q.trim().toLowerCase();
  const rows = Object.keys(GLOSSARY).map((k) => Object.assign({ key: k }, glossary(k)))
    .concat(Object.keys(STATE_TIPS).map((s) => Object.assign({ key: 'state:' + s, state: s }, glossary('state:' + s))))
    .filter((g) => !q || (g.t + ' ' + g.d).toLowerCase().includes(q))
    .sort((a, b) => String(a.t).localeCompare(String(b.t)));
  if (!rows.length) return html`<div class="card-b muted">Nothing matches “${v.q}”.</div>`;
  return rows.map((g) => html`<div class="gloss ${g.tone || ''}"><div class="gt">${g.state ? pill(g.state) : html`<b>${g.t}</b>`}</div>
    <div class="gd">${g.d}${g.doc ? html` <a href="${docHrefFor(g.doc)}">${DOC_NAME[g.doc[0]]} →</a>` : ''}</div></div>`);
}
function helpGlossary(v) {
  return html`<div class="card"><div class="toolbar"><div class="search">${icon('search')}<input class="input" id="gloss-q" placeholder="Search the glossary" value="${v.q}" data-input="gq"></div>
    <span class="small muted">every term a <span class="tipq static">?</span> explains</span></div>
    <div id="gloss-list" class="gloss-list">${glossaryList(v)}</div></div>`;
}

// ================================================================== hooks
Object.assign(GLOBAL_ACT, {
  help: () => go('help'),
  startTour: () => startTour(0),
});
Object.assign(HELP, {
  tip, glossary, startTour, endTour, maybeWelcome, slugify, mdBlocks, setHints, hintsOn, TOUR_STEPS, GLOSSARY,
  paletteItems() {
    const add = (label2, ic, hint, run) => ({ sec: 'Help', label: label2, ic, hint, run });
    return [
      add('Take the tour', 'book', 'two minutes, changes nothing', () => startTour(0)),
      add('Open the lab guide', 'book', 'Help & guides', () => go('help', 'doc=lab')),
      add('Open the README', 'book', 'Help & guides', () => go('help', 'doc=readme')),
      add('Glossary', 'search', 'every term explained', () => go('help', 'tab=glossary')),
      add((hintsOn() ? 'Hide' : 'Show') + ' the ? hints', 'info', 'tooltips', () => { setHints(!hintsOn()); if (S.view) S.view.render(); }),
    ];
  },
});
window.HELP = HELP;
applyHints();
wireTips();
