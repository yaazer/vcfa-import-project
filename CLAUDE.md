# vcfa-import — project notes for Claude Code

Bulk-import vCenter VMs into VCF Automation (VCFA) namespaces via the VCF 9.1
Mobility Operator (`ImportOperationBatch`, `mobility-operator.vmware.com/v1alpha3`).
CLI wrapper around `kubectl`; discovery talks to the vCenter REST API.
Read `README.md` for usage and `LAB-GUIDE.md` for the lab runbook.

## Ground rules

- **Standard library only** in `vcfaimport/`. No pip dependencies: the tool has to
  run as a single `.pyz` on a jump box. PyYAML is allowed in `tools/` and `tests/`.
- **Never emit `controlAction.rollbackAction` at creation time.** The operator treats
  it as "revert now", not "revert on failure". Rollback is only ever patched onto a
  batch that already ran (`rollback` command). The config refuses `rollback_action`.
- **Never guess a namespace or subnet.** `stage` reports VMs it cannot place and
  leaves them out. Same spirit everywhere: an unknown operator phase keeps a VM
  "running" rather than declaring a false verdict.
- **Committed imports are irreversible.** `rollback`, `abandon` and `cleanup` all
  refuse to touch a committed VM.
- Every state change is a transition + a line in the append-only `ledger.jsonl`.
  Do not bypass `Store.set_vm_state`.
- Batches are built **per vCenter folder**; folders are the unit of collection
  (`select --folder`) and execution (`run --folder`).
- **CLI and web console must not diverge.** Put behaviour in the engine, store or
  `service.py`; `cli.py` and `web/api.py` only parse input and format output.
- Web console: no external assets (no CDN, no web fonts; the jump box may be air-gapped),
  every `/api` call needs the `X-VCFA-Token` header, and cluster-changing endpoints
  require `"confirm": true` on the server side too. Only one job runs at a time.
- **A finished wave** (every VM committed or skipped, `engine.wave_finished`) can have all its batch
  objects deleted (`engine.cleanup_plan` / `delete_batches`, Execute step "Clean up finished waves",
  `cleanup --wave`). `delete_batches` re-checks each batch at delete time; a wave with anything open
  is never touched. Cleaned-up batches stay in the store as `deleted`.
- **A campaign reset** (`service.reset_campaign`; Settings → *Reset campaign*, `reset`) is refused
  while a VM is in flight or awaiting commit, deletes the campaign's batch objects first (one failure
  = nothing reset), archives `state.db` + local ledger + job logs to `<workdir>/archive/`, then
  empties `Store.CAMPAIGN_TABLES`. Users, settings and maps survive; a shared `ledger_path` is never moved.
  `POST /api/reset` needs `"confirm_phrase": "RESET"` as well as `confirm` (the HTTP fuzz test sends `confirm: true`).
- **Staging decides waves only for VMs entering the queue** (`service.place_new_vms`): "auto" =
  the maps on a campaign's first staging (empty queue), the next wave (highest + 1) after that;
  "next" / "maps" / a number on request. Queued VMs keep their wave unless "maps" (the old
  behaviour, which re-applies the maps to pending VMs); a wave set in Select always wins.
- A wave's **name and notes** (`wave_info`, often the tenant it moves) belong to its VMs, not its
  number: `swap_waves` moves them along. Setting them is an event (`Store.set_wave_info`), and
  every "Wave N" label in the UI goes through `waveLabel()` (names arrive with `/api/pulse`).
- A wave is not a state: `Store.set_vm_wave` / `swap_waves` log an event, not a
  transition, and refuse VMs already in a batch (`WAVE_LOCKED_STATES`).
- **Anything that changes the cluster takes `service.WorkspaceLock`** (CLI: exit 7 when
  busy; console: 409). Two runs on one state.db were seen applying duplicate batches.
- kubectl probes (`exists`, `can_i`, `api_resources`) return `None` / raise when the API
  server never answered. Never turn a timeout into "missing" or "denied".
- **Re-discovery removes a VM only when vCenter answers 404 for its moref** (`VCenterClient.vm_exists`);
  no answer keeps it. A queued VM still with vCenter becomes skipped (a transition); in-flight and
  committed VMs are never touched. Nothing is checked across vCenters or outside a pass's power filter,
  and a pass that would remove over half the cache is held (`service.reconcile_discovery`).
- Map rows reach the parsers from files *and* from the browser: validate shape, never
  assume a list of dicts. Report bad input as `SelectionError` (400), never a 500.
- UI colours are tokens (`app.css` defaults = Aurora; `fx.js` regenerates them per
  theme in OKLCH). Never hard-code a colour in a view. Status colours (`--s-*`)
  keep their hue in every theme -- green is always committed, red always failed.
- Design-language skins (`skin:` in `fx.js` THEMES -> `html[data-skin]`) carry exact
  colours in `vars`, their structure in the `app.css` skin block and icons in `skins.js`.
  Colour-art icon sets (not `everywhere`) only render where `icon(name, 'art')` marks
  the slot (nav, palette, empty states, folder rows). Skins are flat: no aurora or glass.
- **The five stages flow top to bottom.** Their pages are one column: numbered `flowSec`
  sections ending in a `flowNext` bar to the next stage (views.js); no side-by-side grids
  there. The sidebar is the `FLOW` stepper plus collapsible groups (`NAV` group markers,
  open state in `NAVG` / localStorage `vcfa-nav`); a closed group still shows alert badges.
- The app icon's colours are brand constants (`.appicon` in app.css), not theme tokens; a theme
  only picks the colourway. Its geometry lives in `tools/make_icons.py` and is repeated inline in
  `appIcon()` (core.js) and index.html -- `t_app_icon` fails if they drift.
- **Nobody runs the tool without accepting DISCLAIMER.md** (`vcfaimport/legal.py`). The CLI gate
  (`cli.terms_gate`) lets only `disclaimer` and `accept` through (exit 8 otherwise); the console
  answers 451 to everything outside `LEGAL_OPEN` until that console user has accepted. Acceptances are
  bound to the SHA-256 of the text and fail closed. Never add a bypass: tests accept through
  `VCFA_ACCEPTANCE_FILE` and `_accept_terms()`, the UI harness through its `accept` flag.
- Animation is time-based, never per-frame, and every effect must degrade to a
  complete static render under Motion: Off (the OS reduce-motion default). `fx.js`
  hooks fail soft: the console must work if the look breaks.
- **Campaign controls are opt-in and default-off** (readiness exclusion, tag map, apps
  kept together, change windows, two-person rule, users, notifications, verification).
  An empty setting must mean "behave exactly as before".
- Settings precedence: defaults < TOML < workspace settings (`settings.py`, meta
  `settings`) < per-run CLI flags. `settings.apply()` runs once per cfg (marker attr).
- Approvals are an **audit control, not a security boundary**: the requester can never
  decide their own request, an approval is consumed once for its stage, dry runs are exempt.
- A change window never *starts* a batch that could not finish by its end
  (`Engine.deadline`); in-flight batches are still polled to completion.
- **A precheck vouches for the VM as it was.** Discovery (`Store.upsert_discovered`) sends a
  `precheck_passed` VM back to `pending` when it was renamed, moved, powered off/on or its Tools
  changed since its facts were recorded; `precheck_max_age_hours` (0 = off) holds stale passes
  back at import. Deselecting never unqueues: plans name every VM and flag left-over entries
  (`service.run_members`). A golden image was imported on a three-day-old pass (2026-10-01).
- Verification never changes a VM's state (a committed import stays committed); it
  writes `verifications` rows and `vms.verify_*` only.
- Secrets never hit disk: SMTP passwords via `password_env`, user tokens stored as
  sha256, remembered vCenter credentials live in memory for the console's lifetime.
- Notifications must never break a run: delivery runs in threads and failures are events.
- **Rendering has two modes** (`body[data-fx]`): Full (glass, aurora, pointer effects) and
  Lite, which Auto picks without GPU acceleration. Anything that software compositing
  recomputes every frame -- `backdrop-filter`, CSS `filter` on large or many elements,
  blend modes, full-screen fixed layers, infinite decorative animations -- must be off in
  Lite and absent from the always-on baseline. Measure with `tools/ui_perf.py` before and
  after a visual change. `mount()` skips unchanged markup; keep renders deterministic.
- Help links point at heading slugs of README.md / LAB-GUIDE.md (GitHub rule). Renaming a
  heading breaks them; `t_help_doc_anchors` and ui_stress `help` catch it. The guides ship
  inside the .pyz/.exe as `vcfaimport/web/docs/` (tools/build.py); new UI terms get a
  `GLOSSARY` entry and a `tip('key')`.

## Layout

| path | role |
|---|---|
| `vcfaimport/cli.py` | argparse commands; `_execute` drives precheck/run |
| `vcfaimport/engine.py` | orchestration: preflight, apply/poll loop, rollback, abandon |
| `vcfaimport/status.py` | reads operator status; `operator_status()` is the real vocabulary |
| `vcfaimport/state.py` | SQLite store, transitions, ledger, discovery cache |
| `vcfaimport/discovery.py` | filters, folder/portgroup maps, `stage()`, HTML picker |
| `vcfaimport/vcenter.py` | REST client: VM list, folder tree, NICs |
| `vcfaimport/render.py` | manifest builder + dependency-free YAML emitter |
| `vcfaimport/service.py` | logic shared by CLI and web: `Workspace`, `run_stage`, skip, previews, overview, triage (`KNOWN_ISSUES`), map coverage |
| `vcfaimport/web/server.py` | `serve`: stdlib HTTP server, token + Host checks, static assets via `pkgutil` (works in .pyz/.exe) |
| `vcfaimport/web/api.py` | JSON API; `ROUTES` table; cluster-touching actions start jobs, DB-only ones run inline |
| `vcfaimport/web/jobs.py` | background jobs: one at a time, own `Store`, log streamed + kept in `<workdir>/jobs/` |
| `vcfaimport/readiness.py` | advisory pre-precheck rules over discovery facts: ready / warn / block |
| `vcfaimport/estimate.py` | batch-time median (measured or default) + parallel-limit simulation -> ETA |
| `vcfaimport/schedule.py` | change windows: states, due / missed, deadline |
| `vcfaimport/access.py` | users + roles (token hashes), approvals (request / decide / consume) |
| `vcfaimport/settings.py` | workspace settings overlay: `FIELDS` with ranges, `apply`, `update`, `describe` |
| `vcfaimport/notify.py` | Teams / Slack / webhook / email channels, `EVENTS`, retrying delivery |
| `vcfaimport/verify.py` | post-import checks: power, Tools, IP kept, ping, TCP ports |
| `vcfaimport/web/static/` | the UI: `core.js` (templating, shell, dock, shared actions), `fx.js` (theme engine, aurora, Migration Stream, palette, studio), `skins.js` (icon sets of the design-language skins), `views.js` (pages), `gov.js` (Change control, Settings, badges, saved views), `help.js` (glossary + `?` tooltips, welcome tour, offline Markdown viewer for the guides), `app.css` (token design system) |
| `tools/web_demo.py` | the console against the fakes: `--keep ./webdemo` |
| `tools/ui_perf.py` | rendering cost in software compositing (a GPU-less jump box): traces Full vs Lite |
| `tools/ui_stress.py` | browser stress: injects a harness into the real UI, headless Chrome/Edge (`govern` runs in its own workspace) |
| `tools/fake_kubectl.py` | simulated kubectl + operator (emits the real condition vocabulary) |
| `tools/fake_vcenter.py` | simulated vCenter REST API |
| `tools/demo.py` | full offline campaign; `--keep ./rehearsal` |
| `tools/build.py` | builds `dist/vcfa-import.pyz` (+ `.exe` on Windows with PyInstaller, icon from `make_icons`) |
| `tools/make_icons.py` | the app icon: one 64-unit geometry -> `static/appicon.svg`, `favicon.svg`, the exe's `.ico` |

## Testing

```
python tests/run_tests.py --unit-only   # stdlib only, seconds
python tests/run_tests.py               # + e2e against the fakes (needs PyYAML), ~2 min
python tests/run_tests.py --slow        # + 1800-VM scale case
```

No pytest; tests are `@test`-decorated functions registered in the `UNIT` / `E2E`
lists at the bottom of `tests/run_tests.py`. `_Console` runs the web console
in-process on a free port for API tests; `--only=<text>` runs matching tests.
UI changes: run `python tools/ui_stress.py` (headless Chrome/Edge; drives the real
UI against the fakes) and add a scenario for new interactions. Add a test for any operator behaviour
learned from a real cluster — the fakes should model what was observed.

## Operator facts observed on a real Supervisor (2026-09-18, VCF 9.1)

- Conditions: `ReadyForImport` / `ReadyForCommit` / `Complete`; `False` with reason
  `ObjectNotReady` while working; `True` when reached. `readyCount`, `readyOps`.
- A precheck-only batch ends at `ReadyForImport: True`; `Complete` never turns True.
- The operator creates a child `ImportOperation` named after the operation,
  owned by the batch (controller ownerRef), carrying the same conditions.
  Precheck-only batches create children too (2026-09-21). Two batches naming the
  same operation deadlock: the second can neither create the child nor adopt it,
  and reports `number of operations from status: 0 does not match number of
  operations from spec: N`. Operation names therefore carry a per-batch digest
  (`render.batch_discriminator`) — do not remove it.
- `subnetInfo` is `{apiGroup, kind (Subnet|SubnetSet), name}` — no namespace. In a
  VPC-backed namespace the `Subnet` lives in the VPC's own namespace and is referenced
  by plain name. Precheck **does not validate** the subnet reference.
- **Children speak a different condition vocabulary than batches** (2026-09-23). A
  batch's completion condition is `Complete`; a child's is `Completed`. A child states
  its precheck verdict as `PrecheckSucceeded` (batches have no equivalent and signal a
  pass with `ReadyForImport: True`); a precheck-only child carries that condition alone.
  A finished import child also carries `VirtualMachineCreated`, `VirtualMachineReady`,
  `VirtualMachineReadyForImport`, `NetworkBackingReady`, `GuestCustomization`,
  `VirtualMachineSetManagedBySucceeded`, plus `completionTime`, `stateTransitions` and
  `taskMonitor` in `status`. Child status wins over the batch, so reading only the batch
  spelling left committed VMs stuck at `awaiting_commit` and failed prechecks reading as
  "still running" until the 90-minute batch timeout. See `COMPLETE_CONDITIONS` /
  `COND_PRECHECK_OK` in `status.py` — do not collapse them back to the batch names.
- **Precheck passes a VM with an ISO connected** (2026-09-25): a guest mid-install
  got `PrecheckSucceeded=True`. Tools read `RUNNING` too -- the Ubuntu installer
  runs open-vm-tools -- so Tools status cannot detect an unfinished install.
  Readiness grades `iso_connected` as block for this reason.
- **Under `commitAction: Wait`, the operator set `Completed: True` with no commit**
  (2026-09-25): `ReadyForCommit` at 17:15:37, `Completed` at 17:16:05, the gate never
  patched. Either Wait is not honoured in this build or `Completed` does not mean
  committed under Wait -- unresolved. The run stops polling at the gate, so the tool
  read "awaiting commit" for days; `commit` now re-reads the batch before patching
  and follows it to the end.
- A batch `status` may also carry `operationPlacements` (seen 2026-09-23; unparsed).
- **Every ImportOperation holds a `StoragePolicyReservation`** (cns.vmware.com) the size of
  its VM's disks, owned by the child -- precheck-only children included -- until its batch is
  deleted (2026-10-01). Committed and rolled-back imports release theirs. Old precheck batches
  held 426 GiB of a 250 GiB namespace limit; the quota webhook then denied the next import's
  reservation and its children sat at `VirtualMachineReadyForImport: False
  (VirtualMachineRelocateFailure) waiting for placement results from importoperationbatch` with the
  batch at `ObjectNotReady` -- the quota error is only in the operator's log. Deleting the precheck
  batches let the import proceed within seconds. So finished precheck batches are deleted
  (`Engine._release_precheck`), and a running child's blocking condition is surfaced
  (`status._blocking_condition`).
- **A VirtualMachine deleted mid-import** (2026-10-01): the child keeps `ReadyForCommit: True`
  and adds `VirtualMachineCreated: False (VMCRNotFoundPostCreation) VM CR was created but not
  found`; the batch never reaches `Complete`, and a commit changes nothing for that VM. The vCenter
  VM stays in the namespace's folder. Read as failed after 2 minutes (`status._fatal_condition`).
- **`rollbackAction` is refused once a batch is committed**: the webhook answers
  `rollbackAction: Invalid value: "Immediate": field is immutable`. Whether a batch *created*
  with `commitAction: Auto` refuses it too is not known, so `rollback` explains the error rather
  than refusing up front. Deleting a committed batch (finalizer included) left its imported
  VMs alone: they carry no ownerReferences to the batch or its children.
- Not yet observed: the per-op lists for failed/completed/rolled-back operations, the
  target VM resource name field. `status.py` tries several spellings; confirm when seen.
- A wedged `systemd-resolved` on a Supervisor CP node showed up as
  `lookup <vcenter> on 127.0.0.53:53: i/o timeout` in the condition message.

## Lab environment (fill in)

- Supervisor kubectl context: `Supervisor`
- Tenant namespace: `migration-testing-ns-kcvm5` (VPC `default-homelab`, VPC namespace `testing-vpc-k826r`)
- Subnet (VLAN extension, 10.10.11.0/24): `migration-testing`
- Lab VM: `ubuntu-2` (`vm-3064`), folder `Migration/Wave1`
- Jump box runs the `.pyz`; the lab workspace is `vcfa-lab/` (gitignored)

## Backlog

- `adopt --batch NAME`: take over a batch the tool did not create.
- `abandon` should delete the batch's `ImportOperation` children and wait for them.
- Refuse to apply when the cluster already has an `ImportOperation` for the same vmID
  (still worth having for batches the tool did not create).
- Preflight: warn when two lab workspaces (two `run/state.db`) target the same namespace.
