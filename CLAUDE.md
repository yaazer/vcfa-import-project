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
lists at the bottom of `tests/run_tests.py`. Add a test for any operator behaviour
learned from a real cluster — the fakes should model what was observed.

## Operator facts observed on a real Supervisor (2026-09-18, VCF 9.1)

- Conditions: `ReadyForImport` / `ReadyForCommit` / `Complete`; `False` with reason
  `ObjectNotReady` while working; `True` when reached. `readyCount`, `readyOps`.
- A precheck-only batch ends at `ReadyForImport: True`; `Complete` never turns True.
- The operator creates a child `ImportOperation` named after the operation
  (`<vmname>-<moref digits>`), owned by the batch, carrying the same conditions.
  Two batches for the same VM collide on that name.
- `subnetInfo` is `{apiGroup, kind (Subnet|SubnetSet), name}` — no namespace. In a
  VPC-backed namespace the `Subnet` lives in the VPC's own namespace and is referenced
  by plain name. Precheck **does not validate** the subnet reference.
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
- Unique operation names per batch (avoid the child-name collision).
- `abandon` should delete the batch's `ImportOperation` children and wait for them.
- Refuse to apply when the cluster already has an `ImportOperation` for the same vmID.
- Preflight: warn when two lab workspaces (two `run/state.db`) target the same namespace.
