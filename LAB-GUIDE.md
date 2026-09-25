# vcfa-import — Lab Test Guide

A sequenced first run against a real Supervisor. It is ordered so that the
irreversible step (commit) comes last, and so that rollback is exercised on a
VM that is *held*, not one that has failed — which means you can test the whole
recovery path without stranding anything.

Budget about an hour. You need 3–5 throwaway VMs.

---

## 0. What you need

| | |
|---|---|
| VCF | 9.1+ Supervisor with the Mobility Operator (`kubectl api-resources --api-group mobility-operator.vmware.com` lists `importoperationbatches`) |
| `kubectl` | on `PATH`, logged in to the Supervisor (`kubectl vsphere login ...`), able to create batches in the target namespace |
| A target namespace | already created in VCF Automation |
| A subnet | reachable from that namespace, matching the lab VMs' network — set up per the [article](https://fojta.wordpress.com/2026/06/19/how-to-import-vms-into-vcf-automation/) (VLAN extension or distributed VLAN connection) |
| Lab VMs | 3–5 powered-on VMs with VM Tools running, on that network, that you do not mind importing |
| vCenter credentials | read-only is enough for discovery |

Discovery talks to vCenter over HTTPS on 443. Everything else goes through
`kubectl`.

---

## 1. Install

**Linux jump box (Ubuntu) or macOS:** use `vcfa-import.pyz`, a single file
that runs with the machine's own Python -- 3.11 or newer, nothing else to
install. Copy it over (or unzip the whole bundle there):

```bash
scp vcfa-import.pyz <user>@<jumpbox>:~/        # from where you downloaded it
```

Then, on the jump box:

```bash
python3 --version              # Ubuntu 24.04: 3.12 -- good as it is
sudo apt install python3.11    # Ubuntu 22.04 ships 3.10: add 3.11 next to it, and
                               # use python3.11 wherever this guide says python3
python3 ~/vcfa-import.pyz --version
alias vcfa-import='python3 ~/vcfa-import.pyz'   # optional; add it to ~/.bashrc to keep it
```

With too old a Python the tool says so and names the fix. `kubectl` and its
vSphere plugin for Linux come from the Supervisor's own download page: open
`https://<supervisor-ip>` in a browser, download *CLI Plugin Linux*, and put
`kubectl` and `kubectl-vsphere` on the `PATH` (for example `~/bin`).

**Windows jump box, no Python required:** unzip the bundle and use `vcfa-import.exe`.

Either way, confirm it runs and can see your cluster:

```bash
vcfa-import --version
kubectl config current-context
kubectl api-resources --api-group mobility-operator.vmware.com
```

Below, `vcfa-import` means whichever of the two you are using (with the alias
above, it is literally that on Linux).

---

## 2. Optional: rehearse offline first

If Python and PyYAML are available (`pip install pyyaml`), the source tree in
the bundle includes a full simulation. It takes two minutes and shows you what
every step's output looks like before you touch anything real:

```bash
python tools/demo.py --keep ./rehearsal
python tools/web_demo.py --keep ./webdemo     # the same, in the browser console
```

---

## 3. Create the lab workspace

```bash
mkdir vcfa-lab && cd vcfa-lab
vcfa-import init
```

Edit `vcfa-import.toml`. For the lab, these settings matter:

```toml
[cluster]
context = "<your-supervisor-context>"   # from `kubectl config current-context`

[import]
commit_action = "Wait"       # <-- IMPORTANT for the lab: nothing commits until you say so
subnet_api_group = "crd.nsx.vmware.com"
subnet_kind = "Subnet"

[batching]
batch_size = 2               # small, so each batch is easy to inspect
max_parallel_batches = 1
poll_interval_seconds = 10

[safety]
require_precheck = true
failure_rate_abort = 0.5
```

`commit_action = "Wait"` is the key lab setting. Imported VMs stop at
`awaiting_commit`; you can roll them back or commit them, and either is a
deliberate choice.

### Prefer a browser? Run the same lab from the web console

Every step below can be done in the web console instead of the CLI. It is the
same tool, reading the same `vcfa-import.toml` and the same workspace (state
database, ledger, map files), so you can switch between the two at any point.
Do step 3 above first, then this.

**a. Prepare the jump box.** In the user session that will run the console:

1. Log in to the Supervisor and select its context. The console runs `kubectl`
   as this user, with this user's kubeconfig:

   ```bash
   kubectl vsphere login --server <supervisor-ip> --vsphere-username <user> --insecure-skip-tls-verify
   kubectl config use-context <your-supervisor-context>
   ```

   The login expires (10 hours by default). If a console job later fails with
   `Unauthorized`, log in again; the console picks the new login up without a
   restart.
2. Optional: pre-fill the vCenter server and user on the Discover page. The
   password is typed into the page and never stored.

   ```powershell
   $env:VCFA_VC_SERVER = "vcenter.lab.local"                # PowerShell
   $env:VCFA_VC_USER   = "administrator@vsphere.local"
   ```

   ```bash
   export VCFA_VC_SERVER=vcenter.lab.local                  # bash
   export VCFA_VC_USER=administrator@vsphere.local
   ```
3. **Remove the sample rows `init` wrote** into `folder-map.csv` and
   `portgroup-map.csv` (keep each file's first line, the header). The console
   loads both files on Map & Stage and would otherwise apply the sample
   `redbull-*` namespaces -- `VLAN2*`, for one, can match a lab portgroup. You
   can also delete them on that page, with the **x** next to each row.

**b. Start the console** in the workspace folder.

On a **Linux jump box** you are usually connected over SSH, and closing that
session would stop the console. Run it inside `tmux` so it keeps running
(`sudo apt install tmux` if it is missing):

```bash
tmux new -s vcfa                                      # a session that outlives your SSH connection
cd ~/vcfa-lab
python3 ~/vcfa-import.pyz -c vcfa-import.toml serve   # no --open: a server has no browser
# Ctrl-b then d leaves it running; `tmux attach -t vcfa` brings it back
```

On a **Windows jump box**:

```powershell
cd vcfa-lab
vcfa-import.exe -c vcfa-import.toml serve --open
```

It prints a link like `http://127.0.0.1:8765/#t=<token>`; on a jump box with a
desktop, `--open` opens it in the local browser (without one, `--open` says so
and does nothing). The token is new each time the console starts, so use
the link from the latest start -- or pass `--token <a-long-random-string>` to
keep one link for the whole lab. If port 8765 is taken, add `--port 8780`.
Closing the window or Ctrl-C stops the console; batches already on the cluster
keep running, and the next run (or **Execute → Watch**) records their outcome.

**c. Open it from where you sit.**

| you are | do this |
|---|---|
| at your desk, the jump box is Linux (or Windows with OpenSSH Server) | **recommended.** Open an SSH tunnel and leave it open while you work: `ssh -L 8765:127.0.0.1:8765 <user>@<jumpbox>` (PowerShell and Windows Terminal have `ssh` built in; in PuTTY: *Connection → SSH → Tunnels*, source port `8765`, destination `127.0.0.1:8765`, *Add*). Then open the printed link in your desk's browser. The tunnel can be a second SSH session; the console keeps running in `tmux` either way |
| on the jump box itself, with a desktop (RDP or its console) | use the jump box's browser -- nothing else to set up |
| at your desk, and SSH tunnels are not allowed | start it with `serve --host 0.0.0.0 --token <long-random-string>`, open the port (`sudo ufw allow 8765/tcp` if `ufw` is on), and open the printed link with `127.0.0.1` replaced by the jump box's address. **Anyone who can reach the port and has the link can drive imports** -- lab networks only; the console warns about this when it starts |

Through a tunnel, the pages are drawn by your desk's browser, which usually
has a GPU: expect the full look. The Lite rendering below matters when the
browser itself runs on the jump box.

**d. Check before you touch anything.** The top bar must show **ctx** = your
Supervisor context and **commit Wait**. If either is wrong, stop the console,
fix `vcfa-import.toml`, and start it again. The first visit offers a
two-minute tour that changes nothing. On a jump box without GPU acceleration
the console switches to Lite rendering on its own; if it still feels slow,
open the Theme Studio (palette icon) and choose **Rendering: Lite**.

**e. Then follow the lab steps in the console:**

| lab step | in the console |
|---|---|
| 4 discover / select | **Discover** (type the password in the page), then **Select VMs**: tick `Migration/Wave1` in the folder tree. The **Ready** column warns about VMs likely to fail precheck |
| 4 stage | **Map & Stage**: map `Migration/Wave1` → `migration-testing-ns-kcvm5` in the folder map, and the VM's portgroup → subnet `migration-testing` with its namespace left blank. The *What you can map to* card above the maps lists the namespaces, the subnets (`migration-testing` shows under its VPC namespace, `testing-vpc-k826r`) and the vCenter portgroups: click one to fill the field you are in, or type to get suggestions; a name that is not there is flagged. Check the preview -- it also warns if two maps name different namespaces for a VM -- then **Stage** |
| 5 preflight / plan | **Execute → Run preflight**; the batch plan is shown before every run |
| 6 precheck | **Execute → Precheck → wave 1**; follow the log panel. A DNS stall on the Supervisor shows up in **Triage** as *DNS lookup from the Supervisor timed out* |
| 7 import, held | **Execute → Import** (dry run first if you like); VMs stop at *awaiting commit* |
| 8 roll back | **Execute → Commit gate → Roll back instead**, or the VM's drawer → **Roll back** |
| 9 commit | **Execute → Commit** (type `COMMIT`) |
| 9 verify | **Execute → Post-import verification → Verify**. The vCenter checks need a session: tick *Keep these credentials in memory* on Discover, or set `VCFA_VC_PASSWORD` before starting the console; without it only ping runs. Ping goes out from the jump box (`sudo apt install iputils-ping` if `ping` is missing; without it that check is skipped) |
| 10 tracker | **Activity & logs → Exports** |
| 10b start over | **Batches** → the batch → **Abandon** |

The CLI still works alongside it on the same workspace, for example
`vcfa-import status` in a second window. Only one run happens at a time: a
second one is refused (the console says the workspace is busy; the CLI exits
with code 7) rather than applying batches twice.

Job logs from the console are kept under `run/jobs/`. Include them when you
report back (step 11).

The console carries this guide: **Help & guides → Lab guide**, and every **?**
next to a setting links to the step it matters in.

### Campaign controls: off unless you turn them on

This build adds change windows, the two-person rule, named users,
notifications, tags/applications, readiness and post-import verification
(README, *Campaign controls*). **None of them is on by default**, so the steps
below run exactly as before. A few are worth a look during the lab, and none
of them changes the cluster:

- after step 4: `vcfa-import -c vcfa-import.toml readiness --selected` --
  does `ubuntu-2` grade *ready*? If it says *likely to fail*, note the reason
  and compare it with what precheck says in step 6.
- after step 9: `vcfa-import -c vcfa-import.toml verify` -- powered on, Tools
  running, the same IP as in vCenter before the move, and ping from the jump
  box. Report the result; it is the first time these checks meet a real
  imported VM.
- `discover` now also reads vCenter tags. If it logs *tags not read (...)*, note the
  message (the account may lack tag read rights); discovery itself carries on.

---

## 4. Discover and select the lab VMs

```bash
export VCFA_VC_SERVER=vcenter.lab.local
export VCFA_VC_USER=administrator@vsphere.local
vcfa-import discover --insecure          # --insecure if the vCenter cert is self-signed
```

It prompts for the password (never stored). Then find your lab VMs by folder:

```bash
vcfa-import browse --folders             # the VM folder tree with counts
vcfa-import browse --folder Lab/Import   # or wherever they live; subfolders included
```

If the lab VMs are scattered, put them in one vCenter folder first and re-run
`discover` — folders are how VMs are collected for import, and it is worth
using the same convention in the lab that you will use for the real waves.

**Check the NIC device keys here.** `browse --json --name lab-01` shows each
adapter's `device_key` as vCenter reports it. This is the value that goes into
the manifest; the article's example uses 4000, but a VM whose first NIC was
ever removed and re-added may have 4001.

Select them and assign the target. `vcfa-import namespaces` lists the
namespaces you can use -- from the Supervisor, or from your kubeconfig's
contexts if this login may not list them. `vcfa-import subnets` and
`vcfa-import portgroups` do the same for subnets and portgroups:

```bash
vcfa-import select --folder Lab/Import --namespace <target-namespace> --wave 1
```

`init` wrote a `portgroup-map.csv` template with every column it accepts.
Replace the sample rows with yours — only `portgroup` and `namespace` are
required; the rest can be left blank:

```csv
portgroup,namespace,subnet,wave,device_key,subnet_kind,subnet_api_group
<the VMs' portgroup name>,<target-namespace>,<subnet name>,1,,,
```

`wave` here applies to VMs that got no wave from the picker or the folder map.

```bash
vcfa-import stage --map portgroup-map.csv
vcfa-import status
```

(For the real waves you will use `--folder-map` as well, so that each folder's
namespace and wave come from a file rather than from `select --namespace`.
The lab is small enough that the flag is fine.)

Every lab VM should now be `pending`. If `stage` reports "no target namespace"
for a VM, its portgroup did not match the map — check `browse --facets` for
the exact spelling.

---

## 5. Preflight and validate — nothing applied yet

Two words come up from here on that sound alike but are different things:

| | `preflight` | `precheck` |
|---|---|---|
| who does the checking | **the tool**, from the jump box, with read-only `kubectl` calls | **the Mobility Operator**, on the Supervisor |
| what is created on the cluster | nothing | an `ImportOperationBatch` with `precheckOnly: true` (and its child `ImportOperation`) |
| what it looks at | your config and queue against the cluster: is this the Supervisor context, is the operator installed and its pod healthy, do the target namespaces exist, may you create batches in them, do the subnets you named exist | the VM itself: the operator connects to vCenter, finds the VM by moref, and checks whether it can be imported (powered on, VM Tools, supported hardware, NIC/portgroup resolvable, and so on) |
| when to run it | before anything is applied; re-run after any config or map change | once per VM before its real import; `run` refuses a VM without a passed precheck (`require_precheck = true`) |
| result | `[ok]`/`[FAIL]` per check; exit code 2 stops you | VM moves to `precheck_passed` or `precheck_failed` with the operator's message |

Preflight catches the mistakes that would make every batch fail the same way
(wrong context, wrong namespace name, wrong subnet object name, no RBAC).
Precheck catches the per-VM problems (a VM without Tools, an unsupported
device) that only the operator can see. Both are non-destructive: nothing is
migrated until `run`.

```bash
vcfa-import preflight
```

All checks must be `[ok]`. The subnet check is the one most likely to fail:
it looks for `subnets.crd.nsx.vmware.com/<name>` in the namespace, and the
name you gave the network in the VCFA UI is usually **not** the Kubernetes
object name (the article's VLAN 197 extension was `subnet-vlan197`). When the
check fails, preflight lists the subnet-like objects the namespace actually
holds, with their display names — put the object name in `portgroup-map.csv`
and re-run. If it lists nothing, the VLAN extension has not been made
available to that namespace yet.

```bash
vcfa-import plan --stage precheck --show
vcfa-import plan --stage precheck --server-dry-run
```

`--show` prints the first manifest — compare it against the article's example.
`--server-dry-run` sends every manifest to the API server with
`--dry-run=server`; the operator's admission webhook validates the shape
without creating anything. **If this fails, stop and read the error**: it is
the cheapest possible signal that the CRD schema differs from what the tool
emits.

---

## 6. Precheck — first contact with the operator

```bash
vcfa-import precheck --folder Lab/Import --limit 1     # one VM from the lab folder
```

(`--folder` works on every execution command — `run`, `rollback`, `retry`,
`status` — so the lab folder can be driven in isolation even if other VMs are
queued.)

This applies a batch with `precheckOnly: true`. The operator handles it like
a real import batch — it connects to vCenter, locates each VM and runs its
compatibility checks — but stops before moving anything. A precheck-only
batch ends at `ReadyForImport: True`; `ReadyForCommit` and `Complete` never
turn true for it, which is expected. Watch the output; the tool polls and
reports when it settles. Then:

```bash
vcfa-import vms
vcfa-import history --vm <moref>
```

**What to check** — this is the compatibility test for the status parser:

- The VM reached `precheck_passed` (or `precheck_failed` with a message).
- `last_phase` in `vms` shows the operator's actual phase word.
- If the VM sits in `precheck_running` for longer than the operator takes,
  the phase word is one the tool does not recognise. Run
  `kubectl get importoperationbatch -n <ns> -o yaml` and look at `.status`;
  then add the word to `phase_patterns` in the config (see README, *Reading
  operator status*) and run `vcfa-import status --refresh`.

Once one works, precheck the rest:

```bash
vcfa-import precheck --wave 1
```

**Before moving on to `run`, delete the finished precheck batch.** The
operator names the child `ImportOperation` after the VM (`<vmname>-<moref
digits>`), so a precheck batch and an import batch for the same VM collide:
the import batch cannot create its child and sits at `ReadyForImport: False`
with `number of operations from status: 0 does not match number of operations
from spec: 1`. Seen in the lab on 2026-09-18. Until the tool does this itself:

```bash
kubectl get importoperationbatches -n <ns>            # pre-w1-... is the precheck batch
kubectl delete importoperationbatch pre-w1-<...> -n <ns>
kubectl wait --for=delete importoperation/<vmname>-<digits> -n <ns> --timeout=60s
```

Do not use `abandon` for this — it also returns the VMs to `pending`, and you
want them to keep `precheck_passed`. The tool's own record of the precheck is
in `state.db` and the ledger, not in the batch object.

---

## 7. Import one VM, held at the gate

```bash
vcfa-import run --wave 1 --limit 1
```

Because `commit_action = "Wait"`, the VM should stop at `awaiting_commit`:

```bash
vcfa-import status
vcfa-import vms --state awaiting_commit
```

Check vCenter: the VM should still be running, now under Supervisor ownership
but not yet committed. Check the namespace: `kubectl get vm -n <ns>` should
show a new `vm-xxxxxxxx` resource.

**Note that resource name** — see section 10.

---

## 8. Roll it back — the recovery test

Rather than commit it, hand it back:

```bash
vcfa-import rollback --vm <moref>
```

The tool patches `rollbackAction: Immediate` onto the batch and waits. You
should see:

```
-> rollbackAction=Immediate on <ns>/<batch> (1 VM(s) to revert, 0 committed untouched)
   waiting up to 30m for the operator to revert ownership...
<- <ns>/<batch>: 1 VM(s) reverted to vCenter
```

Then verify in vCenter that the VM is back under vCenter management, and:

```bash
vcfa-import vms --state rolled_back
vcfa-import history --vm <moref>
kubectl get importoperationbatch -n <ns>         # the batch object is still there
vcfa-import cleanup                              # ...until you delete it
```

**If the tool waits and never confirms** but vCenter shows the VM reverted,
the operator's rollback phase word is not in the pattern table. Look at the
batch `.status` with kubectl, add the word to `phase_patterns` under
`"rolled_back"`, then `vcfa-import status --refresh`. Please note the word
down — it is worth adding to the defaults.

The rolled-back VM is retryable:

```bash
vcfa-import retry --vm <moref>
```

**Optional, on a throwaway VM:** confirm first-hand that `rollbackAction` is an
instruction and not a policy. Take a manifest from `plan --show`, add
`rollbackAction: Immediate` under `controlAction` by hand, `kubectl apply` it,
and watch: the expectation is that the VM reverts (or the batch is rejected)
rather than importing. This is why the tool never writes the field at creation
time and why the config refuses `rollback_action`.

---

## 9. Import and commit for real

```bash
vcfa-import precheck --wave 1        # the retried VM needs a fresh precheck
vcfa-import run --wave 1             # all lab VMs -> awaiting_commit
vcfa-import commit --wave 1          # irreversible from here
vcfa-import status --refresh
```

All VMs should reach `committed`. Confirm in VCF Automation that they appear
in the namespace, and in vCenter that they are now Supervisor-managed.

Optionally, test the auto-commit path on the last VM by setting
`commit_action = "Auto"` before `run`.

---

## 10. Check the tracker

```bash
vcfa-import history --vm <moref>
vcfa-import ledger --tracker tracker.csv
vcfa-import report --html report.html
```

Open `tracker.csv`. Two columns to check against reality:

- `target_resource` — should be the `vm-xxxxxxxx` name you saw in section 7.
  If it is blank, the operator reports that name under a field the tool does
  not look for. Run `kubectl get importoperation -n <ns> -o yaml` and find the
  field holding the new VM name; it is a one-line addition to `TARGET_KEYS`
  in `vcfaimport/status.py`.
- `src_cluster`, `src_networks` — should match what vCenter showed.

---

## 10b. Starting a batch over

If a precheck batch needs to be discarded — wrong subnet name, wrong VM, or
you simply want a clean run — do not delete it with `kubectl` and do not
delete `run/`. `abandon` does both halves in the right order:

```bash
vcfa-import abandon --batch <name>        # or --vm <moref>, or --stage precheck
```

It deletes the batch object (no rollback is involved: a precheck migrates
nothing) and returns its VMs to `pending`, which also unlocks them so a
corrected `portgroup-map.csv` + `stage` (or `load`) takes effect. Import
batches are only accepted by `abandon` when none of their VMs is still
importing or held at the commit gate — those need `rollback` first.

---

## 11. Report back

The heuristics that matter are the operator's vocabulary. After the lab, the
useful things to capture are:

```bash
kubectl get importoperationbatch -n <ns> -o yaml > lab-batch-status.yaml
kubectl get importoperation      -n <ns> -o yaml > lab-operation-status.yaml
```

taken (a) after precheck, (b) while a VM is `awaiting_commit`, (c) after
rollback, (d) after commit — plus the `ledger.jsonl` from the workspace. With
those four snapshots the phase table and target-resource lookup can be made
exact rather than heuristic.

---

## Troubleshooting

| symptom | likely cause | what to do |
|---|---|---|
| `preflight`: CRD not found | wrong kubectl context, or operator not installed | `kubectl config use-context <supervisor>` |
| `preflight`: subnet not found, but the namespace UI shows it | VPC-mode namespace: the `Subnet` lives in the **VPC's** namespace (`kubectl get subnets.crd.nsx.vmware.com -A`). preflight now reports this as "VPC-scoped" and passes | reference it by plain name; `subnetInfo` has no namespace field |
| `preflight`: subnet not found anywhere | name or kind mismatch | preflight lists the subnet-like objects the namespace holds; use that name, or set `subnet_kind` |
| `preflight`: Mobility Operator pod not healthy | operator crash-looping / not scheduled | `kubectl get pods -A \| grep -i mobility`, then its logs |
| `--server-dry-run` rejects the manifest | CRD schema differs from the article | read the error; fields are in `vcfaimport/render.py` |
| `precheck running` with message `lookup <vcenter> on 127.0.0.53:53: i/o timeout` | **systemd-resolved is wedged on the Supervisor control-plane node** running the operator. Seen in the lab: the upstream DNS answered directly (`dig @<dns>`) but the node's stub did not | on that CP node (`decryptK8Pwd.py` on the VCSA for access): `systemctl restart systemd-resolved`; check the other CP nodes too. The operator retries on its own |
| `precheck running` with `Operations are not ready` for many minutes | operator still working, or it is stalled on something in its logs | `kubectl describe importoperationbatch -n <ns>`; the message column in `vms` now shows the reason |
| VM stuck in `precheck_running` / `importing` with a phase the tool does not name | unrecognised phase word | inspect `.status`, extend `phase_patterns`, `status --refresh` |
| a precheck passed but the manifest named a wrong subnet | precheck does **not** validate the subnet reference (observed) | the subnet is only exercised by a real import: keep `commit_action = "Wait"` so the first import can be rolled back |
| need a clean slate on a batch | precheck-only batch, or nothing in flight | `vcfa-import abandon --batch <name>` (or `--stage precheck`): deletes it, VMs back to `pending`, inventory edits then land |
| batch deleted with `kubectl` behind the tool's back | | the tool marks its VMs failed with "disappeared from the cluster"; `retry` requeues them |
| `rollback` never confirms | unrecognised rollback phase | same, under `"rolled_back"` |
| `target_resource` blank | field name unknown | extend `TARGET_KEYS` in `status.py` |
| `discover`: certificate error | self-signed vCenter | `--insecure` |
| `discover`: 401 | wrong user format | try `user@vsphere.local` |
| the web console feels slow or laggy | the browser has no GPU acceleration (typical on a jump box VM or over RDP) and is compositing the glass effects in software | Theme Studio (palette icon) → **Rendering: Lite**. *Auto* normally picks it by itself; the Studio says what it detected |

Every command is safe to re-run. State is in `run/state.db`; the movement log
in `run/ledger.jsonl` survives even if you delete the database.

---

## Cleanup

Committed VMs cannot be returned to vCenter by this tool (or by the operator).
To reset the lab, delete them from the namespace and recreate them in vCenter.
Batch objects for rolled-back imports: `vcfa-import cleanup`. The workspace
directory can simply be deleted.
