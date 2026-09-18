# vcfa-import

Bulk-import vCenter VMs into VCF Automation namespaces.

VCF 9.1's Mobility Operator imports VMs by applying an `ImportOperationBatch`
to a Supervisor namespace ([Tomas Fojta's
walkthrough](https://fojta.wordpress.com/2026/06/19/how-to-import-vms-into-vcf-automation/)).
Hand-writing those manifests is fine for a handful of VMs. This tool exists for
the case where you have 1800 of them. It pulls your whole VM inventory out of
vCenter, lets you choose which VMs are in scope, turns that choice into batched
manifests, applies them against your live VCF context with a controlled amount
of concurrency, tracks every VM's state in a resumable local database, and tells
you exactly what failed and why.

```
vCenter ──discover──> folder tree ──select --folder──> selection ──stage──>
    import queue ──precheck──> run ──> committed in VCFA
```

It is a **wrapper around `kubectl`**, not a reimplementation of it — whatever
authentication already works for your Supervisor context works here.

---

## See it work first

```bash
python tools/demo.py --keep ./rehearsal
```

Runs a complete campaign -- discover, select, stage, preflight, precheck,
import in waves, retry, export -- against a simulated vCenter and a simulated
Mobility Operator. No cluster, no vCenter, nothing real is contacted. It injects
import failures so you can watch the rollback -- ownership handed back to
vCenter, batch deleted, VMs retried and landing -- and leaves a workspace
containing the picker, the report, the tracker CSVs, the ledger and every
generated manifest.

```
--vms 200          how many VMs the fake vCenter serves
--scenario flaky   fail at precheck instead of import
--scenario happy   no injected failures
--batch-size 25    batch sizing to rehearse
```

---

## Requirements

| | |
|---|---|
| Python | 3.11+ (standard library only — no pip install) |
| `kubectl` | on `PATH`, logged in to the Supervisor |
| VCF | 9.1 or later (Mobility Operator CRDs) |
| Permissions | ability to create `importoperationbatches` in the target namespaces |

PyYAML is needed only to run the offline test suite, never by the tool itself.

## Install

Three options, in order of least to most packaging:

```bash
python vcfa-import.py --help          # from the source tree
python vcfa-import.pyz --help         # single file, any OS with Python 3.11+
vcfa-import.exe --help                # Windows, Python not required
```

`tools/build.py` produces the `.pyz` (no extra tooling) and, on Windows with
PyInstaller installed, the `.exe`, plus a zip bundling both with this README,
the lab guide, examples and source. **[LAB-GUIDE.md](LAB-GUIDE.md)** walks
through a first run against a real Supervisor.

---

## Quick start

Two ways in. **Discover from vCenter and pick** (start here if you don't already
have a VM list):

```bash
python vcfa-import.py init --dir ./campaign
cd campaign

export VCFA_VC_SERVER=vcenter.example.local
export VCFA_VC_USER=administrator@vsphere.local
python ../vcfa-import.py -c vcfa-import.toml discover        # prompts for the password
python ../vcfa-import.py -c vcfa-import.toml browse --folders   # the folder tree
python ../vcfa-import.py -c vcfa-import.toml select --folder Production/Web
python ../vcfa-import.py -c vcfa-import.toml stage --folder-map folder-map.csv --map portgroup-map.csv
```

**Or bring your own CSV**:

```bash
python ../vcfa-import.py -c vcfa-import.toml load -i inventory.csv
```

Either way, the import itself is the same:

```bash
python ../vcfa-import.py -c vcfa-import.toml preflight
python ../vcfa-import.py -c vcfa-import.toml precheck  --wave 1
python ../vcfa-import.py -c vcfa-import.toml run       --wave 1
python ../vcfa-import.py -c vcfa-import.toml status
```

Every command is safe to re-run: state lives in `run/state.db`, and re-running
`run` picks up wherever the last invocation stopped.

---

## Discovering and selecting VMs

`discover` pulls the whole VM inventory straight from the vCenter REST API into
a local cache — moref, name, power state, cluster, folder, guest OS, VM Tools
state, and every network adapter with its **device key** (which is exactly what
`ImportOperationBatch` needs). No PowerCLI required.

```bash
vcfa-import discover --vcenter vcenter.example.local --user administrator@vsphere.local
vcfa-import discover --insecure          # self-signed vCenter certificate
vcfa-import discover --powered-on --no-tools --concurrency 24
```

Credentials come from `--vcenter/--user/--password`, then `VCFA_VC_SERVER` /
`VCFA_VC_USER` / `VCFA_VC_PASSWORD`, then an interactive prompt. **The password
is never written to disk**, and the session is deleted when the command ends.
Re-running `discover` refreshes the cache and keeps whatever you had selected.

### Collect by folder

VM folders are the unit of collection. Discovery walks the folder tree, so
every VM carries its **full path** below the datacenter (`Production/Web/Tier1`),
not just its leaf folder name — two folders both called `Web` under different
parents are never confused.

```bash
vcfa-import browse --folders
```

```
  folder                                       direct  subtree selected
  -------------------------------------------- ------ -------- --------
  (datacenter root)                                 5      240
    Databases/                                     30       30
    Production/                                            120
      App/                                         40       40
      Web/                                         50       80       80
        Tier1/                                     30       30       30
```

A folder takes **everything beneath it**:

```bash
vcfa-import select --folder Production/Web            # Web + Tier1: 80 VMs
vcfa-import select --folder Production --folder DMZ   # repeatable
vcfa-import select --folder Production --no-subfolders   # that folder only
vcfa-import select --folder 'Legacy/*'                # globs; * crosses slashes
vcfa-import select --folder /                         # the whole datacenter
```

Paths are case-insensitive, accept either slash, and ignore leading/trailing
slashes. With more than one datacenter, `--datacenter` disambiguates.

### Look around

```bash
vcfa-import browse --facets                       # datacenters, clusters, folders, networks
vcfa-import browse --folder Production/Web --powered-on
vcfa-import browse --cluster 'PROD-*' --network VLAN197-Prod --limit 200
vcfa-import browse --folder DMZ --json            # for scripting
vcfa-import browse --selected --csv chosen.csv
```

### Pick them by hand

```bash
vcfa-import pick --out picker.html
```

Opens as a normal local web page: search, pick a folder from the tree (its
subtree is included), filter by cluster / network / power state, sort any
column, tick the VMs you want, bulk-assign a namespace and wave, then
**Download selection.csv**. Feed that straight back in:

```bash
vcfa-import select --from-file selection.csv
```

Namespaces and waves typed in the picker are carried through. The file can also
just be a plain list of morefs or VM names, one per line.

### Narrow further, non-interactively

```bash
vcfa-import select --folder Production/Web --powered-on --with-nics \
                   --namespace redbull-ns1-r95mc --wave 1
vcfa-import select --folder Legacy --exclude-name 'test-*'
vcfa-import select --folder Production/App --deselect
vcfa-import select --none                         # start over
```

Filters are ANDed. `--folder` takes paths (subtree by default); `--name`,
`--cluster`, `--datacenter`, `--network`, `--guest-os` and `--exclude-name`
take globs; all are repeatable; `--regex` matches VM names. `select` with no
filters refuses to act — pass `--all` if you really mean every VM.

### Stage the selection for import

```bash
vcfa-import stage --folder-map folder-map.csv --map portgroup-map.csv
vcfa-import stage --folder-map folder-map.csv --map portgroup-map.csv --out inventory.csv
```

`stage` turns selected VMs into import inventory. Two maps do two jobs:

**`folder-map.csv`** — which namespace and wave a folder's VMs go to. A folder
covers its subtree; the **most specific folder wins**, so a rule for
`Production/Web` overrides one for `Production` for the VMs under Web. An
optional `group` column keeps those VMs in their own batches.

```csv
folder,namespace,wave,group
Production,redbull-ns1-r95mc,2,
Production/Web,redbull-ns1-r95mc,1,web
Databases,redbull-ns2-k22ab,1,
Legacy/*,redbull-ns3-legacy,3,
```

**`portgroup-map.csv`** — which subnet each network adapter lands on. It can
also carry a namespace and wave, used only for VMs no folder rule covers, plus
per-portgroup overrides for the NIC device key and the subnet kind/API group.
All columns after `namespace` are optional:

```csv
portgroup,namespace,subnet,wave,device_key,subnet_kind,subnet_api_group
VLAN197-Prod,,subnet-vlan197,1,,,
VLAN2*,,subnet-vlan2xx,2,,,
```

`init` writes both map files with every column present.

Namespace precedence: picker / `select --namespace` → folder map → portgroup
map → `--default-namespace`. Wave follows the same order. **A VM with no
namespace is reported, never guessed** — `stage` lists it with its folder and
leaves it out; folders with no rule are listed too.

### Run by folder

Folders are the unit of execution as well as collection. Every command that
acts on the import queue takes `--folder PATH` (subtree included, `--no-subfolders`
to exclude), so a migration can be driven one folder at a time regardless of
how waves were assigned:

```bash
vcfa-import precheck --folder Production/Web
vcfa-import run      --folder Production/Web --rollback-failed
vcfa-import status   --folder Production/Web        # progress for that subtree, by folder
vcfa-import rollback --folder Production/Web/Tier1
vcfa-import retry    --folder Production/Web/Tier1
vcfa-import vms      --folder Legacy --state failed
```

Batches are built **per folder** — a batch never mixes VMs from two folders —
so batch names carry the folder (`imp-w1-production-web-tier1-001-…`) and
`rollback --folder` maps cleanly onto whole batches. `rollbackAction` is
batch-scoped, so if a batch *does* span the scope (possible only with an
explicit `group` in the folder map, or a CSV-loaded inventory), `rollback`
names the VMs outside the scope that would be reverted too, before asking for
confirmation.

---

## The inventory CSV

One row per VM. Column names are matched loosely (case, spaces and underscores
are ignored, and common aliases are accepted).

| column | required | meaning |
|---|---|---|
| `moref` | **yes** | vCenter managed object reference, e.g. `vm-1483405`. Aliases: `moid`, `vm_id`, `virtualMachineID` |
| `namespace` | **yes** | target Supervisor namespace |
| `vm_name` | no | display name, used in reports and to build operation names |
| `subnet` | no | target subnet name for the VM's NIC |
| `device_key` | no | NIC device key (default `4000`) |
| `subnet_kind` | no | default `Subnet` |
| `subnet_api_group` | no | default `crd.nsx.vmware.com` |
| `mode` | no | default `preserve` |
| `wave` | no | integer; waves run in order (default `1`) |
| `group` | no | forces VMs into separate batches |
| `skip` | no | `yes`/`true` excludes the row |
| `notes` | no | free text, carried into reports |

Multi-NIC VMs, either way:

```csv
vm_name,moref,namespace,nic1_subnet,nic1_device_key,nic2_subnet,nic2_device_key
app-01,vm-1483405,ns-prod,front-subnet,4000,back-subnet,4001
```

```csv
vm_name,moref,namespace,nics
app-01,vm-1483405,ns-prod,"[{""deviceKey"":4000,""subnet"":""front""},{""deviceKey"":4001,""subnet"":""back""}]"
```

### Getting the inventory out of vCenter with PowerCLI

`discover` is usually the easier path (see above), but if REST access to vCenter
is blocked from where you run this, or you would rather work from a spreadsheet,
`tools/Export-VcenterInventory.ps1` (PowerCLI, read-only) exports every VM with
its moref and per-NIC portgroups, and translates portgroups into namespaces and
subnets via a mapping file:

```powershell
Connect-VIServer vcenter.example.local
.\tools\Export-VcenterInventory.ps1 -MappingCsv .\portgroup-map.csv -OutFile .\inventory.csv
```

`portgroup-map.csv`:

```csv
portgroup,namespace,subnet,wave
VLAN197-Prod,redbull-ns1-r95mc,subnet-vlan197,1
VLAN200-DB,redbull-ns2-k22ab,subnet-vlan200,2
```

It flags VMs with unmapped portgroups, no target namespace, or VM Tools not
running (a common precheck failure) rather than dropping them silently.

---

## What gets generated

A batch of VMs sharing a wave, namespace, mode and subnet set becomes one
`ImportOperationBatch`:

```yaml
apiVersion: mobility-operator.vmware.com/v1alpha3
kind: ImportOperationBatch
metadata:
  name: imp-w1-subnet-vlan197-001-8821b
  namespace: redbull-ns1-r95mc
  labels:
    vcfa-import/run: 20260910-191649
    vcfa-import/wave: "1"
    vcfa-import/stage: import
spec:
  defaultSpec:
    mode: preserve
    controlAction:
      commitAction: Auto
  operations:
    - name: app-web-01-1483405
      spec:
        virtualMachineID: vm-1483405
        networkInterfaces:
          - deviceKey: 4000
            subnetInfo:
              apiGroup: crd.nsx.vmware.com
              kind: Subnet
              name: subnet-vlan197
```

Manifests are written to `run/manifests/` before they are applied, so every
change is auditable and you can `kubectl apply -f` any of them by hand.

The `vcfa-import/run` label is how the tool finds its own batches when polling —
it never touches batches it did not create.

---

## Commands

| command | what it does |
|---|---|
| `init` | write a starter config and inventory template |
| `discover` | pull the full VM inventory from vCenter into the local cache |
| `browse` | the folder tree (`--folders`), facets, or a filtered list (`--json`, `--csv`) |
| `pick` | write a clickable HTML picker of the discovered VMs |
| `select` | choose VMs to import, by folder (`--folder`, subtree included) or other filters |
| `stage` | turn the selection into the import queue, via folder and portgroup maps |
| `validate` | check an inventory CSV; touches nothing |
| `load` | merge an inventory CSV into the run state |
| `preflight` | verify CRDs, namespaces, subnets and RBAC |
| `plan` | compute batches and render manifests; `--server-dry-run` validates them against the API server |
| `precheck` | apply `precheckOnly: true` batches |
| `run` | apply the real import batches; `--folder` scopes, `--rollback-failed` reverts failures afterwards |
| `status` | progress by wave and namespace, or `--folder` for one subtree; `--refresh` re-polls first |
| `watch` | redraw `status` on an interval until nothing is in flight |
| `commit` | release VMs held by `commitAction: Wait` |
| `retry` | return failed VMs to the queue |
| `rollback` | hand failed imports back to vCenter: set `rollbackAction`, wait for the revert, optionally delete |
| `cleanup` | delete batch objects whose rollback the operator has confirmed |
| `abandon` | discard a batch without rollback (precheck-only, or nothing in flight); VMs return to `pending` |
| `vms` | list VMs and their states (`--json` for scripting) |
| `skip` | exclude or re-include VMs |
| `events` | the run's event log |
| `report` | standalone HTML and/or CSV report |

### VM lifecycle

```
pending ──precheck──> precheck_passed ──run──> importing ──> committed
   │                        │                      │
   └──> precheck_failed     └──> (retry)           ├──> awaiting_commit ──commit──> committed
                                                   │         │
                                                   └──> failed ──rollback──> rolled_back ──retry──> pending
```

---

## When an import fails: rollback

A failed import can leave a VM half-owned by the Supervisor (WCP). The
Mobility Operator's recovery is to edit the `ImportOperationBatch`, add
`rollbackAction: Immediate` under `controlAction`, save, and wait — the
operator hands ownership back to vCenter, after which the batch object can be
safely deleted. `rollback` does exactly that sequence, and records every step:

```bash
vcfa-import rollback --failed                # every batch with failed or uncommitted VMs
vcfa-import rollback --batch imp-w1-vlan197-003-8a1c2
vcfa-import rollback --vm vm-1483405         # whichever batch this VM is in
vcfa-import rollback --failed --delete       # ...and delete each batch once confirmed
vcfa-import rollback --failed --no-wait      # set the action and come back later
```

What happens, in order:

1. **Patch.** `controlAction.rollbackAction: Immediate` is merged onto each
   batch (equivalent to `kubectl edit`). The affected VMs move to
   `rolling_back` and the request is written to the ledger.
2. **Wait.** The batch is polled until the operator reports the VMs reverted
   (`RolledBack` / `RollbackComplete` phases), up to `rollback_timeout_minutes`
   (default 30). A VM the operator could not revert is marked `failed` with
   `ROLLBACK FAILED:` in its message — that one needs a human.
3. **Confirm.** Reverted VMs move to `rolled_back`, with `rolled_back_at`
   stamped. They are retryable: `retry` returns them to the queue.
4. **Delete** — only with `--delete`, and **only after step 3 confirms**.
   Deleting a batch whose rollback has not completed is refused. Batches you
   rolled back without `--delete` stay on the cluster for inspection; remove
   them later with `cleanup`, which likewise deletes only confirmed rollbacks.

```
importing ──> failed ──rollback──> rolling_back ──> rolled_back ──retry──> pending
                                        │
                                        └──> failed (ROLLBACK FAILED: needs a human)
```

Two things it will not do:

- **Touch a committed VM.** A committed import is irreversible; `rollback`
  reports those and leaves them alone. A batch where 8 committed and 2 failed
  rolls back the 2.
- **Guess.** If a batch vanished from the cluster mid-rollback, the VMs are
  left as they were and you are told to verify ownership in vCenter.

A batch held by `commitAction: Wait` can be rolled back instead of committed —
that is the point of the gate.

### Why rollback is never in the manifest

The operator reads `controlAction.rollbackAction` as an **instruction** —
"revert now" — not as a policy of "revert on failure". A batch created with the
field already present reverts every VM in it instead of importing them. So the
tool never writes it at creation time, there is no config option for it, and a
config that still contains `rollback_action` is refused with an explanation.
Rollback is only ever patched onto a batch that has already run — which is
exactly the manual `kubectl edit` procedure, automated.

### Rolling back as part of the run

For a long campaign, three commands per wave is tedious. `--rollback-failed`
does the patch-and-wait for whatever failed, in the same invocation, after the
wave settles:

```bash
vcfa-import run --wave 3 --rollback-failed
vcfa-import retry && vcfa-import precheck --wave 3 && vcfa-import run --wave 3
vcfa-import cleanup                      # delete the rolled-back batch objects
```

It never deletes anything — batches stay for inspection until `cleanup` — and
it fires even when the circuit breaker halts the run, so a wave that went badly
does not leave VMs half-owned overnight.

---

## The tracker

Every VM movement is recorded permanently. Nothing is ever overwritten — each
state change appends a new entry, so months later you can still answer "when did
`app-web-042` move, out of which cluster, into which namespace, and how long did
it take?"

Three views of the same record:

| | |
|---|---|
| `run/ledger.jsonl` | append-only, one JSON object per state change, written as it happens |
| `transitions` table | the same entries in SQLite, for querying |
| `vms` table | current state per VM, plus source facts, milestones and the target resource |

```bash
vcfa-import history                        # the last 40 movements, newest first
vcfa-import history --vm vm-1483405        # one VM's complete story
vcfa-import history --state failed --json  # everything that failed, for scripting
vcfa-import history --since 2026-09-10T00:00:00Z

vcfa-import ledger                         # writes tracker.csv + transitions.csv
vcfa-import ledger --tracker cmdb.csv      # per-VM: source -> target, with timings
vcfa-import ledger --verify                # cross-check the JSONL against the database
```

`vcfa-import history --vm <moref>` prints the full picture for one VM:

```
app-web-042  (vm-1483405)
  state      : committed
  source     : vcenter.example.local  cluster PROD-CL01  folder Production
  networks   : VLAN197-Prod
  sizing     : 4 vCPU, 8192 MiB
  target     : redbull-ns1-r95mc / vm-a1b2c3d4
  interfaces : 4000->subnet-vlan197
  durations  : import 3m12s, end to end 1h04m

  when                  from             to                stage     batch                 held   detail
  2026-09-10T09:00:11Z  -                pending           queue                                  queued for import
  2026-09-10T09:58:02Z  pending          precheck_running  precheck  pre-w1-vlan197-001-8  3471s  Applied
  2026-09-10T09:59:40Z  precheck_running precheck_passed   precheck  pre-w1-vlan197-001-8  98s
  2026-09-10T10:00:51Z  precheck_passed  importing         import    imp-w1-vlan197-001-a  71s    Applied
  2026-09-10T10:04:03Z  importing        committed         import    imp-w1-vlan197-001-a  192s
```

### What each VM record keeps

**Where it came from** — captured when the VM is queued, from the discovery
cache, so the record stands on its own even after the cache is refreshed or the
VM no longer exists in vCenter: source vCenter, cluster, folder, ESXi host,
portgroups, power state, vCPU/RAM, VM Tools state.

**Where it went** — namespace, the per-NIC subnet mapping actually applied, and
`target_resource`: the generated `vm-xxxyyyzzzz` name the operator gave the VM
inside the namespace. That name is the only durable link between the VCFA object
and the vCenter VM it came from, which makes it the column your CMDB wants.
It is read from the operator's status where reported, and left blank otherwise —
never guessed.

**When** — queued, precheck started/finished, import started, committed, plus
computed durations. `history` also reports average, fastest and slowest import
times across everything committed so far, which is what you need to forecast how
long the remaining waves will take.

**Why, when things went wrong** — the failure message from the operator is kept
on the transition itself, so a VM that failed, was requeued and later succeeded
shows all three steps rather than just its final state.

The ledger file is deliberately separate from the database: delete or rebuild
`state.db` and `ledger.jsonl` still holds the complete history. Point it
somewhere durable with `ledger_path` in the config:

```toml
[paths]
ledger_path = "//fileserver/migration/vcfa-ledger.jsonl"
```

The HTML report gains a **Moved into VCFA** table with the same data, and
`report --csv` / `ledger --tracker` write the per-VM tracker for a change record.

---

## Safety

Importing is not reversible once committed — a committed VM cannot be exported
back to vCenter management. The tool is built around that fact:

- **Precheck first.** `run` refuses to import a VM that has not passed a
  precheck (`require_precheck = true`). Override per-run with `--no-precheck`.
- **Waves.** Nothing crosses a wave boundary. Pilot with `--wave 1 --limit 10`.
- **Circuit breaker.** If `failure_rate_abort` (default 25%) of a wave's results
  fail, the run halts and applies nothing further. Exit code 3.
- **Concurrency ceilings.** `max_parallel_batches` cluster-wide and
  `max_parallel_batches_per_namespace` cap in-flight work.
- **Explicit confirmation.** `run`, `precheck`, `commit` and `rollback` prompt
  unless `--yes` is given; in a non-interactive shell they refuse without it.
- **`--dry-run`** renders and validates everything without applying it.
- **Ctrl-C** stops new applies, polls what is already in flight to completion,
  and leaves resumable state.
- **In-flight VMs are locked.** Re-running `load` after editing the CSV will not
  change the target of a VM that is mid-import; it reports the conflict instead.

Exit codes: `0` ok · `1` aborted by the operator · `2` config/inventory/preflight
problem · `3` circuit breaker · `4` finished with failures · `5` kubectl error ·
`6` vCenter error.

---

## Reading operator status

The vocabulary the Mobility Operator actually uses (VCF 9.1, `v1alpha3`,
observed 2026-09-18) is handled explicitly:

```
status.conditions[]  type: ReadyForImport | ReadyForCommit | Complete
                     status "True" when reached; otherwise "False" with
                     reason: ObjectNotReady and a message saying why
status.readyCount / readyOps      operations that passed precheck
```

| stage | what the tool reads |
|---|---|
| precheck | `ReadyForImport: True` → `precheck_passed`. `ReadyForCommit`/`Complete` stay False for a precheck-only batch and are ignored |
| import | `ReadyForCommit: True` → `awaiting_commit`; `Complete: True` → `committed` |
| any | `ObjectNotReady` → still running; the condition's message is copied onto the VM row so `vms` shows *why* it is waiting (a DNS failure on the Supervisor node looked like this) |

Two observations from the lab that shape how to use it: a precheck-only batch
creates **no** `ImportOperation` children, so per-VM detail comes from
`readyOps`; and a precheck **passed with an invalid subnet name** — the subnet
reference is evidently only exercised by a real import. The per-operation
lists for failure/commit/rollback outcomes have not been observed yet; the
names the tool tries are in `status.py` (`FAILED_OPS_KEYS` etc.).

Anything outside that vocabulary falls back to the heuristics below.

The Mobility Operator's status schema is at `v1alpha3` and is not fully
documented, so the tool does not hard-code it. Per-VM outcomes are read from,
in order of preference:

1. child `ImportOperation` objects owned by the batch (these carry
   `spec.virtualMachineID`, so the mapping to a VM is exact);
2. an inline per-operation list on the batch's `status`;
3. the batch-level phase, applied to all of its VMs.

Whatever phase string comes back is classified into `succeeded` / `failed` /
`awaiting_commit` / `running` / `unknown` by an ordered regex table. If your
build reports a phase the defaults do not recognise, add it to the config
rather than patching code:

```toml
phase_patterns = [
  ["^(succeeded|committed|imported)$", "succeeded"],
  ["(failed|error|rejected)",          "failed"],
  ["(awaitingcommit|readytocommit)",   "awaiting_commit"],
  ["(pending|running|migrating)",      "running"],
]
```

Patterns are matched case-insensitively against the phase with spaces, dashes
and underscores removed. First match wins. Anything unmatched is treated as
still running, so an unknown phase stalls a batch rather than declaring a false
success — check `vcfa-import vms --state importing` and the `last_phase` column
if a batch sits still.

---

## Running 1800 VMs

```bash
# 0. Discover everything in vCenter, then collect by folder
python vcfa-import.py -c cfg.toml discover
python vcfa-import.py -c cfg.toml browse --folders             # what is where
python vcfa-import.py -c cfg.toml select --folder Migration/Wave1 --powered-on
python vcfa-import.py -c cfg.toml stage --folder-map folder-map.csv --map portgroup-map.csv

# 1. ...or bring your own CSV instead
python vcfa-import.py validate -i inventory.csv
python vcfa-import.py -c cfg.toml load -i inventory.csv
python vcfa-import.py -c cfg.toml preflight            # namespaces, subnets, RBAC

# 2. Validate the manifests server-side without applying anything
python vcfa-import.py -c cfg.toml plan --stage precheck --server-dry-run

# 3. Pilot: ten VMs, watched closely
python vcfa-import.py -c cfg.toml precheck --wave 1 --limit 10
python vcfa-import.py -c cfg.toml run      --wave 1 --limit 10
python vcfa-import.py -c cfg.toml status

# 4. Precheck the whole estate (cheap, non-destructive) and fix what it finds
python vcfa-import.py -c cfg.toml precheck -y
python vcfa-import.py -c cfg.toml vms --state precheck_failed

# 5. Import wave by wave; anything that fails is handed back to vCenter at once
python vcfa-import.py -c cfg.toml run --wave 1 -y --rollback-failed
python vcfa-import.py -c cfg.toml report --html wave1.html
python vcfa-import.py -c cfg.toml retry && python vcfa-import.py -c cfg.toml precheck --wave 1 -y
python vcfa-import.py -c cfg.toml run --wave 1 -y --rollback-failed     # the retry
python vcfa-import.py -c cfg.toml cleanup -y
python vcfa-import.py -c cfg.toml run --wave 2 -y --rollback-failed
```

Sizing: `batch_size` × `max_parallel_batches` is how many VMs can be migrating
at once. Start at 10 × 4 and raise it only after a wave completes cleanly;
`max_parallel_batches_per_namespace` keeps one namespace from monopolising the
operator. With `poll_interval_seconds = 20`, polling costs two `kubectl get`
calls per namespace per interval regardless of VM count.

Long campaigns: run under `screen`/`tmux`, or drive it from a scheduled job —
`run` is idempotent and resumable, so re-invoking it after any interruption
continues the campaign.

---

## Post-import

As the article notes, imported VMs arrive without a VM Class (they keep their
source CPU/RAM), get a generated resource name like `vm-xxxyyyzzzz` with the
original name kept as a display annotation, and cannot be exported back to
vCenter once committed. `report --csv` gives you the moref-to-namespace mapping
to reconcile against your CMDB.

---

## Tests

```bash
python tests/run_tests.py --unit-only   # stdlib only
python tests/run_tests.py               # + end-to-end (needs PyYAML)
python tests/run_tests.py --slow        # + the 1800-VM scale case
```

`tools/fake_kubectl.py` stands in for kubectl and the Mobility Operator and
`tools/fake_vcenter.py` stands in for the vCenter REST API, so discovery,
selection, staging and the apply/poll/commit/retry loop are all exercised offline — including failure
injection, the commit gate, and the circuit breaker. Point the tool at it to
rehearse a campaign without a cluster:

```toml
kubectl = "python /path/to/tools/fake_kubectl.py"
```

