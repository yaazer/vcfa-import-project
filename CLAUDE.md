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
- A wave is not a state: `Store.set_vm_wave` / `swap_waves` log an event, not a
  transition, and refuse VMs already in a batch (`WAVE_LOCKED_STATES`).
- **Anything that changes the cluster takes `service.WorkspaceLock`** (CLI: exit 7 when
  busy; console: 409). Two runs on one state.db were seen applying duplicate batches.
- kubectl probes (`exists`, `can_i`, `api_resources`) return `None` / raise when the API
  server never answered. Never turn a timeout into "missing" or "denied".
- Map rows reach the parsers from files *and* from the browser: validate shape, never
  assume a list of dicts. Report bad input as `SelectionError` (400), never a 500.

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
| `vcfaimport/web/static/` | the UI: `core.js` (templating, shell, dock, shared actions), `views.js` (pages), `app.css` |
| `tools/web_demo.py` | the console against the fakes: `--keep ./webdemo` |
| `tools/ui_stress.py` | browser stress: injects a harness into the real UI, headless Chrome/Edge |
| `tools/fake_kubectl.py` | simulated kubectl + operator (emits the real condition vocabulary) |
| `tools/fake_vcenter.py` | simulated vCenter REST API |
| `tools/demo.py` | full offline campaign; `--keep ./rehearsal` |
| `tools/build.py` | builds `dist/vcfa-import.pyz` (+ `.exe` on Windows with PyInstaller) |

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
- A batch `status` may also carry `operationPlacements` (seen 2026-09-23; unparsed).
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
