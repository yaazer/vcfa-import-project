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

The same simulation, driven from the browser instead:

```bash
python tools/web_demo.py --keep ./webdemo     # opens the console on http://127.0.0.1:8765
```

---

## The web console

```bash
python vcfa-import.pyz -c vcfa-import.toml serve --open
```

Everything the CLI does, from a browser: discover, select, map, arrange waves,
execute, then follow progress and triage failures. It is the same engine,
state database and ledger as the CLI. The two can be used interchangeably on
one workspace, and a batch started from either shows up in both.

| page | what you do there |
|---|---|
| **Overview** | Campaign totals, progress by wave, namespace and application (with time left per wave), live batches, recent failures, requests waiting for approval, the next change window, and a *next step* prompt that tells you what to do now |
| **1 Discover** | Connect to vCenter (server/user pre-filled from `VCFA_VC_*`; the password is used once and never stored) and read the inventory, with a live progress bar |
| **2 Select VMs** | Folder tree with tri-state checkboxes (a folder takes its whole subtree), search and facet filters (including **readiness**, vCenter **tag** and **application**), shift-click ranges, bulk namespace/wave/app, saved views |
| **3 Map & Stage** | Edit the folder and portgroup maps in place. Each folder and network of the selection shows what it resolves to, or **unmapped**, with one-click *+ Map*. An optional **tag map** places VMs by vCenter tag. A live preview shows exactly which VMs stage, into which namespace and wave, and which can't be placed. Staging saves the maps to the same CSVs the CLI uses |
| **4 Waves** | A board with one column per wave and its estimated time. Drag a VM or a whole folder between waves, drop onto *New wave*, and reorder waves with ← →. With *Move whole apps* an application moves as one; apps split across waves are flagged, with *Pull into wave N*. VMs already in a batch are locked and cannot move |
| **5 Execute** | Preflight checklist, then precheck or import per wave, with an optional folder scope, batch size, parallelism, dry run and *roll back failures after the run*. You review the batch plan before anything is applied; an import under `commitAction: Auto` makes you type `IMPORT`. Includes a commit gate, a *Watch* for batches left over from an earlier session, an ETA per wave, what is held back and why, post-import verification, and *Schedule…* to run it in a change window instead |
| **Import queue** | Every VM with state, application and verification filters and saved views; bulk retry / skip / move wave / verify / roll back / abandon |
| **Batches** | Every `ImportOperationBatch` with its members, the manifest that was applied, and its events |
| **Triage** | Failures grouped by cause (VM-specific details are normalised away), each with the likely fix and the right buttons: retry, skip, abandon, roll back. Also stalled VMs, rolled-back VMs, and batches safe to clean up |
| **Activity & logs** | Every job's full log (searchable, *problems only*), the event log with who did what, the movement log, and downloads: tracker.csv, transitions.csv, ledger.jsonl, report.html |
| **Change control** | Change windows on a week timeline (plan one and see whether the work fits before it closes), and the approvals queue for the two-person rule |
| **Settings** | Guardrails (pacing, safety, governance, applications, verification) with their file values and allowed ranges, notification channels with a *Test* button, and people: personal links for viewers, operators and admins |
| **Help & guides** | The welcome tour, a step-by-step *first import*, this README and the lab guide rendered offline with a table of contents, and a searchable glossary |

**Look and feel.** The Overview opens on the **Migration Stream**: every VM is a
particle, flowing from vCenter through precheck and import into VCF Automation,
with anything that needs attention pooled underneath. The same stream follows
you to Execute while a run is in progress. Behind the glass, a slow aurora
reacts to the campaign: failures tint it with a hazard colour, held commits
turn it amber, a running job quickens it, and a finished campaign blooms.

- **Theme Studio** (palette icon, top right): seven themes -- Aurora, Nebula,
  Solar Flare, Phosphor, Graphite, Glacier, Daylight -- plus a hue shift, glow
  intensity, and motion (Full / Calm / Off). Palettes are generated in OKLCH so
  every hue stays legible, and status colours keep their meaning in every theme.
  Your operating system's *reduce motion* setting selects Off automatically.
- **Command palette** (`Ctrl+K` or `/`): jump to any page, VM or batch, run
  preflight/refresh/watch/verify, or switch theme, from the keyboard.
- **Stream lanes**: the Migration Stream flows by stage, or regroups its
  particles into one lane per wave, namespace or application, coloured by state.
- **Density**: *Compact* in the Theme Studio fits more rows on a screen.
- **Rendering: Auto / Full / Lite.** Frosted glass and the aurora cost almost
  nothing on a GPU and a great deal on a machine without one -- a jump box VM
  or a remote desktop session, where every frame is composited in software.
  *Auto* (the default) checks for hardware acceleration and switches to
  **Lite** without it: same layout, colours and status meaning, no glass, no
  aurora, no pointer effects. Measured in software rendering, a frame costs
  about 2 ms in Lite against 14-40 ms in Full. If Auto guesses wrong, pick Full
  or Lite in the Theme Studio or with `Ctrl+K` -> *Rendering*.
  (`python tools/ui_perf.py` measures it on your machine.)

**Help while you work.** The first visit opens a two-minute welcome tour
(it changes nothing; re-run it from *Help & guides* or `Ctrl+K`). A small **?**
sits next to every value that matters -- commitAction, batch size, dry run,
the maps, the subnet, readiness and more -- and explains it on hover or tap,
with a link to the right section of the lab guide or this README, which the
console carries offline. Status pills explain themselves too. Experts can
hide the marks under *Help & guides -> Hints*.

Long operations run as background **jobs**. Their log streams into a panel at
the bottom of every page, and is kept under `<workdir>/jobs/` so it survives a
restart. **Stop** acts like the first Ctrl-C: nothing new is applied, and
batches already on the cluster are polled to completion. Only one job runs at a
time.

**Safety.** The console listens on `127.0.0.1` by default and needs the access
token printed at start-up (the link carries it after `#t=`, so it never reaches
a server log). Every API call sends it in a header, so another web page in the
same browser cannot drive the console. Requests naming a foreign `Host` are
refused. Cluster-changing actions need an explicit confirmation, which the
server enforces as well as the UI. The start-up link is the **owner** (an
admin); give colleagues their own links from Settings -> People (see
[Campaign controls](#campaign-controls)). To reach it from your desk, keep it on
loopback and tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 jumpbox        # then open the printed link locally
```

No SSH on the jump box (a Windows jump box over RDP, say)? Use the browser on
the jump box itself. [LAB-GUIDE.md](LAB-GUIDE.md#prefer-a-browser-run-the-same-lab-from-the-web-console)
walks through the whole setup: kubectl login, the map files, the access link,
the ways to reach the console, and each lab step in the console.

```
serve [--host 127.0.0.1] [--port 8765] [--token T] [--open]
      [--folder-map folder-map.csv] [--map portgroup-map.csv] [--tag-map tag-map.csv]   # default: next to the config
```

Like the rest of the tool it is standard library only, and the UI is plain
HTML/CSS/JS with no CDN, so it works on an air-gapped jump box and ships inside
the `.pyz` and `.exe`.

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
vcfa-import discover --powered-on --no-tools --concurrency 24   # large estate, in a hurry
```

That last line is the "thousands of VMs" combination: `--powered-on` caches only
powered-on VMs, `--no-tools` drops the per-VM VM Tools call (discovery makes one
REST call per VM for details and a second for Tools, so this roughly halves the
calls), and `--concurrency 24` raises the worker pool from the default 12 —
values above 32 are clamped.

`--no-tools` is the one with a cost. Without it `tools_status` stays empty, so
discovery cannot print its *"N VM(s) are not running VM Tools (likely precheck
failures)"* warning and `--tools-running` filters match nothing. VM Tools not
running is a common precheck failure, so you are trading an early, cheap warning
for speed — worth it on a first sweep of a large estate, less so on the batch you
are about to import.

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
```

Filters are ANDed. `--folder` takes paths (subtree by default); `--name`,
`--cluster`, `--datacenter`, `--network`, `--guest-os` and `--exclude-name`
take globs; all are repeatable; `--regex` matches VM names. `select` with no
filters refuses to act — pass `--all` if you really mean every VM.

### Unselecting: the selection is cumulative

**`select` only ever adds.** Each run marks its matches as selected and leaves
every other VM untouched, so running `select` again with a narrower filter does
not narrow the selection — it just selects a few more VMs you had already
selected. After `select --all`, a following `select --folder Production/Web` is
effectively a no-op, and the whole estate is still queued.

This surprises people most often in this shape:

```bash
vcfa-import select --all                          # 2,400 VMs selected
vcfa-import select --folder Production/Web        # still 2,400 -- not 80
```

Two ways back:

```bash
vcfa-import select --none                         # clear the selection entirely
vcfa-import select --deselect --folder Lab/Scratch   # subtract just these
vcfa-import select --deselect --powered-off          # any filter works in reverse
```

`--none` resets every row in one step and ignores all other filters. `--deselect`
takes exactly the same filters as a normal `select` and removes the matches
instead of adding them.

To make the selection *exactly* some set — the usual intent — clear first, then
select:

```bash
vcfa-import select --none
vcfa-import select --folder Production/Web
```

That two-step also applies to the picker: `select --from-file selection.csv`
adds the VMs listed in the file but does **not** deselect the ones absent from
it, so re-exporting a narrowed list from `pick` will not shrink an existing
selection on its own.

Check what you actually have at any point with `browse --selected`, or the
`selected` column of `folders`.

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
| `commit` | release VMs held by `commitAction: Wait`; scopes by `--wave` or `--vm` (**no `--folder`**), or commits everything waiting |
| `retry` | return failed VMs to the queue |
| `rollback` | hand failed imports back to vCenter: set `rollbackAction`, wait for the revert, optionally delete |
| `cleanup` | delete batch objects whose rollback the operator has confirmed |
| `abandon` | discard a batch without rollback (precheck-only, or nothing in flight); VMs return to `pending` |
| `vms` | list VMs and their states (`--json` for scripting) |
| `skip` | exclude or re-include VMs |
| `events` | the run's event log |
| `report` | standalone HTML and/or CSV report |
| `readiness` | grade discovered VMs from their vCenter facts before precheck (advisory); exit 4 when some are likely to fail |
| `verify` | check committed VMs: powered on, Tools running, IP kept, ping, TCP ports |
| `schedule` | change windows: `add --start --end`, `list`, `cancel ID`, `tick` (run from Task Scheduler or cron) |
| `settings` | show, `set key=value ...` or `reset key ...` the workspace settings the console edits |
| `users` | personal console links: `add NAME --role viewer\|operator\|admin`, `list`, `disable`, `enable` |
| `approvals` | the two-person rule: `list`, `request --stage`, `approve ID`, `reject ID` |
| `serve` | the web console: every step above from a browser (see [The web console](#the-web-console)) |

### Preflight vs precheck

`preflight` is the **tool** checking the **environment**: read-only `kubectl`
calls from the jump box that confirm the context is a Supervisor with the
Mobility Operator CRDs, the operator pod is healthy, every target namespace in
the queue exists, you may create batches in it, and every subnet the queue
names resolves (in the namespace, or VPC-scoped in the VPC's namespace).
Nothing is created; a failure exits 2. Run it before anything is applied and
again after any change to the config or the maps.

`precheck` is the **operator** checking the **VMs**: a real
`ImportOperationBatch` with `precheckOnly: true`. The operator connects to
vCenter, locates each VM and runs its compatibility checks, then stops with
`ReadyForImport: True` instead of migrating. Each VM lands in
`precheck_passed` or `precheck_failed` with the operator's message. It costs a
batch object on the cluster but moves nothing, and `run` refuses a VM that has
not passed one (`require_precheck = true`).

Preflight finds the mistakes that would fail every batch the same way;
precheck finds the per-VM problems only the operator can see. Note that
precheck does **not** validate the subnet reference — an import with a wrong
subnet name is only caught by the real import, which is why
`commit_action = "Wait"` matters.

### VM lifecycle

```
pending ──precheck──> precheck_passed ──run──> importing ──> committed
   │                        │                      │
   └──> precheck_failed     └──> (retry)           ├──> awaiting_commit ──commit──> committed
                                                   │         │
                                                   └──> failed ──rollback──> rolled_back ──retry──> pending
```

---

## The commit gate

With `commit_action = "Wait"` (the default in the sample config), `run` stops
one step short of finishing. The operator imports each VM, hands ownership to
the Supervisor, and then holds: the VM is running under VCFA but the import is
not final. Those VMs sit in `awaiting_commit` until you release them.

This is the safety gate. Up to this point the import is reversible; after the
commit it is not.

**1. See what is waiting.**

```bash
vcfa-import status                      # "awaiting commit  2"
vcfa-import vms --state awaiting_commit # the VMs, with wave, namespace and batch
```

**2. Release them.**

```bash
vcfa-import commit                                   # everything awaiting commit
vcfa-import commit --wave 1                          # just this wave
vcfa-import commit --vm vm-3082 --vm vm-3083         # named VMs, repeatable
```

`commit` lists what it is about to release, warns that it is irreversible, and
prompts for confirmation (`-y` skips the prompt for scripted runs). It patches
the batch, it does not wait — the operator finishes asynchronously.

> **The gate lives on the batch, not the VM.** `commit` patches
> `commitAction: Auto` onto each batch holding a selected VM, and the operator
> then releases *every* operation still waiting in that batch. So
> `commit --vm vm-3082` commits vm-3082's whole batch, not just vm-3082.
>
> Be aware that the confirmation prompt currently lists only the VMs your filter
> matched, so it **under-reports** what a narrowed `--vm` or `--wave` will
> actually commit. Check the `batch` column in `vms --state awaiting_commit` and
> assume every waiting VM sharing a batch goes with it. To commit a genuine
> subset, the VMs have to be in separate batches (`batch_size`, or a `group`
> column in the inventory).

> **`commit` does not take `--folder`.** `precheck`, `run`, `status` and
> `rollback` all scope by folder; `commit` scopes only by `--wave` or `--vm`. If
> you have been working through a campaign with `--folder`, drop the flag here or
> switch to `--wave`.
>
> Watch the wave number too: a folder called `migration-wave3` says nothing about
> which wave its VMs are in. Check with `vms --state awaiting_commit` rather than
> assuming — `commit --wave 3` on wave-1 VMs reports *"no VMs are awaiting
> commit"* and does nothing, which looks like a failure but is just an empty
> filter.

**3. Confirm it landed.**

```bash
vcfa-import status --refresh
```

The VMs move `awaiting_commit → committed` once the operator reports
`Complete: True`.

### Or hand them back instead

A VM at the gate has not been committed, so it can still be returned to vCenter:

```bash
vcfa-import rollback --batch imp-w1-migration-wave3-001-5271a
```

That is the last moment this is possible. See below.

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
- **One writer per workspace.** `precheck`, `run`, `commit`, `rollback`, `abandon`
  and `cleanup` -- from the CLI or the web console -- take an OS lock on
  `<workdir>/run.lock`. A second one is refused with the holder's name (exit 7)
  instead of applying duplicate batches for the same VMs. The OS releases the lock
  if its holder crashes, so it can never be left stuck. Dry runs and read-only
  commands do not take it.
- **A flapping API server is not a verdict.** Timeouts are retried; preflight
  reports what it could not verify as a warning, never as "missing".

Exit codes: `0` ok · `1` aborted by the operator · `2` config/inventory/preflight
problem · `3` circuit breaker · `4` finished with failures · `5` kubectl error ·
`6` vCenter error · `7` workspace busy (another run holds it).

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

Children use **different condition names than their batch** — a point worth
knowing if you ever read the CRs by hand:

| | batch | child `ImportOperation` |
|---|---|---|
| import finished | `Complete` | `Completed` |
| precheck verdict | `ReadyForImport: True` | `PrecheckSucceeded` |

A precheck-only child carries `PrecheckSucceeded` and nothing else. A finished
import child adds the VM-lifecycle conditions (`VirtualMachineCreated`,
`VirtualMachineReady`, `NetworkBackingReady`, `GuestCustomization`, …) and a
`completionTime`. Per-VM status is read from the children in preference to the
batch, so both spellings matter.
| any | `ObjectNotReady` → still running; the condition's message is copied onto the VM row so `vms` shows *why* it is waiting (a DNS failure on the Supervisor node looked like this) |

Three observations from the lab shape how to use it. A precheck-only batch
**does** create `ImportOperation` children, each named after its operation with
no batch-name prefix and owned by the batch — so two batches that name the same
operation fight over one object, and the loser reports `number of operations
from status: 0 does not match number of operations from spec: N` forever (see
*Operation names* below). A precheck **passed with an invalid subnet name** —
the subnet reference is evidently only exercised by a real import. And the
per-operation lists for failure/commit/rollback outcomes have not been observed
yet; the names the tool tries are in `status.py` (`FAILED_OPS_KEYS` etc.).

#### Operation names

Every operation name carries a five-character digest of its batch's name, so
`ubuntu-3` in moref `vm-3079` becomes `ubuntu-3-3079-be78d` under a precheck
batch and `ubuntu-3-3079-008cf` under the import that follows. The digest is
derived from the batch name, so it is stable across re-planning (a resumed run
still recognises its children) and differs for every distinct batch. A precheck
batch can therefore be left on the cluster as a record; it no longer blocks the
import. Two VMs that slug to the same name inside *one* batch get a counter
appended to the digest.

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

## Campaign controls

Everything in this section is **off until you turn it on** -- a workspace that
does not use it behaves exactly as before. Each works the same from the CLI and
from the console, and each is stored in the same workspace.

### Readiness, before precheck

Discovery also reads what vCenter knows that predicts a precheck failure: VMware
Tools not running, a disk that is not a plain VMDK (RDM), an ISO left connected,
a disconnected NIC, no NICs, old virtual hardware, no guest IP. Each VM gets a
grade -- *ready*, *check first* or *likely to fail* -- with the reason and the fix:

```bash
vcfa-import readiness --selected          # exit 4 when something is likely to fail
```

It is advice: the operator's precheck is the authority. To keep VMs graded
*likely to fail* out of precheck batches, set `readiness_exclude_blocked = true`
(Settings -> Safety); the run says how many it held back.

### vCenter tags and applications

Discovery reads each VM's vCenter tags (`--no-tags` skips it). Tags can select
VMs (`select --tag "Application:Payroll"`, glob allowed) and place them, with a
third map that sits between a VM's own setting and the folder map:

```csv
tag,namespace,wave,group
Application:Payroll,prod-pay-ns,1,
Application:*,prod-apps-ns,,
```

```bash
vcfa-import stage --map portgroup-map.csv --folder-map folder-map.csv --tag-map tag-map.csv
```

An exact tag beats a glob. Name the tag category that identifies applications
with `app_category = "Application"`, or set an application by hand
(`select --app Payroll`, or *Set app* in the console). With
`app_together = true`:

- staging puts every VM of an application in its earliest wave (and says so),
- moving one VM of an application to another wave moves all of it,
- an import takes an application only when **every** VM of it has passed
  precheck and is in scope; otherwise the whole application is held back and
  the run lists why.

### Time estimates

Every plan shows how long it should take: batches are laid out against the
parallel limits exactly as the engine will run them, times the median batch
duration measured in this workspace (a conservative default until batches have
run). The Overview and Waves pages show the time left per wave; `run` and
`precheck` print the estimate before asking to confirm.

### Change windows

A change window runs a precheck or an import between two times, unattended.
No batch is started that would not finish before the window closes, given the
measured batch time; batches already on the cluster are polled to completion,
and what is left waits for the next window. Planning one shows whether the
work fits.

```bash
vcfa-import schedule add --stage import --wave 2 --start "2026-09-26 22:00" --end "2026-09-27 04:00"
vcfa-import schedule list
vcfa-import schedule cancel 3
```

Times without a zone are the machine's local time. Something must be running
to start a window: `serve` checks every 15 seconds, or run `schedule tick`
from Task Scheduler / cron every few minutes (it takes the workspace lock like
any run, so it never overlaps a CLI or console run):

```powershell
schtasks /Create /SC MINUTE /MO 5 /TN vcfa-import-tick /TR "python C:\vcfa\vcfa-import.pyz -c C:\vcfa\vcfa-import.toml schedule tick"
```

A window that closes without starting is marked *missed* (and notified).

### The two-person rule

```toml
[governance]
require_approval = ["import", "rollback"]   # any of import, commit, rollback
```

A gated step becomes a request that someone **other than the requester**
approves; it then runs once, on behalf of both. A dry run needs no approval.
In the console, *Import* turns into *Request approval* and Change control
shows the queue. From the CLI, people are identified by their OS login:

```bash
vcfa-import approvals request --stage import --wave 2     # alice
vcfa-import approvals approve 7 --note "CAB-1234"         # bob
vcfa-import run --wave 2 --approval 7                     # alice or bob
```

A change window for a gated stage waits for approval, and is cancelled if it
is rejected. This is an audit control -- it records two names against every
gated change -- not a security boundary against someone with shell access to
the jump box.

### People, roles and read-only links

The link `serve` prints is the **owner** (admin). Everyone else gets a
personal link from Settings -> People, or:

```bash
vcfa-import users add jsmith --role operator     # prints the link token, once
vcfa-import users add auditor --role viewer
vcfa-import users disable jsmith
```

| role | can |
|---|---|
| viewer | look at everything, change nothing -- for managers and change boards |
| operator | discover, select, stage, run, commit, roll back, approve other people's requests |
| admin | also change settings and notification channels, and manage people |

Only a hash of each token is stored. Every job, event, state change and ledger
line records who did it (the CLI records the OS user).

### Workspace settings

Pacing, safety, governance, application and verification settings can be
changed per workspace -- from Settings in the console, or:

```bash
vcfa-import settings show
vcfa-import settings set batch_size=20 max_parallel_batches=6
vcfa-import settings reset batch_size
```

Precedence: built-in defaults < the TOML file < workspace settings <
flags on a single command. Every value is range-checked, loosening a guardrail
in the console asks first, and every change is an event with who made it.

### Notifications

Nothing is sent anywhere until a channel is configured, in the TOML or in
Settings -> Notifications (with a *Test* button):

```toml
[[notify]]
name   = "migration-ops"
type   = "teams"                 # teams | slack | webhook | email
url    = "https://example.webhook.office.com/webhookb2/..."
events = ["job_failed", "circuit_breaker", "awaiting_commit", "approval_requested", "verify_failed"]

[[notify]]
name         = "on-call"
type         = "email"
smtp_host    = "smtp.corp.local"
smtp_port    = 587
from         = "vcfa-import@corp.local"
to           = ["oncall@corp.local"]
username     = "svc-vcfa"
password_env = "VCFA_SMTP_PASSWORD"   # the password itself is never stored
```

Events: `job_succeeded`, `job_warning`, `job_failed`, `circuit_breaker`,
`awaiting_commit`, `approval_requested`, `approval_decided`,
`schedule_started`, `schedule_finished`, `schedule_missed`, `verify_failed`.
No `events` means all of them. `webhook` posts a JSON document with the event,
severity, title, text and fields; delivery is retried and a failure is logged,
never fatal.

### Post-import verification

```toml
[verify]
verify_after_import = true    # check automatically when an import finishes
verify_ping         = true
verify_ports        = [22, 443, 3389]
```

Each committed VM is checked for: powered on, VMware Tools running, the same
IP as before the move, ping, and the TCP ports listed. The vCenter checks need
a session (`VCFA_VC_*`, or tick *keep credentials in memory* on Discover in the
console); without one only the network checks run. Results appear in the queue
and each VM's drawer, and failures are notified. Verification never changes a
VM's state: a committed import stays committed.

```bash
vcfa-import verify --wave 2 --unverified
```

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
injection, the commit gate, and the circuit breaker. Point the tool at the fake
to rehearse a campaign without a cluster:

```toml
kubectl = "python /path/to/tools/fake_kubectl.py"
```

The web console is tested the same way: its API is driven end to end (discover
through rollback and retry, plus Stop), and its token/Host/confirm guards are
unit-tested.

The `stress/` tests go after failure conditions on purpose: hostile HTTP
(pipelined and smuggled bodies, oversized and malformed requests, a fuzz matrix
over every route, DNS-rebinding Host headers, path traversal), 24 concurrent
clients, a crash of the console mid-run, the CLI and console racing on one
workspace, an API server that times out a quarter of the time, a missing
kubectl, a sick operator, bad vCenter credentials, the circuit breaker, and
1800 VMs end to end (`--slow`).

```bash
python tests/run_tests.py --only=stress/chaos   # any test whose name contains the text
python tools/ui_stress.py [--scale]             # the UI itself, in headless Chrome/Edge
```

`tools/ui_stress.py` drives the real UI -- clicks, typing, drag and drop,
confirmations -- against the fakes. It covers hostile VM names (no markup is
ever injected), a server that disappears and comes back, phone-width layout, an
empty workspace, both themes, and 1800-VM rendering timed on the server's
clock.

