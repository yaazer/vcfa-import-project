# Incident: a rollback that never finished (CNS unregister NotSupported)

- **Lab:** VCF 9.1 Supervisor, context `supervisor`, workspace `vcfa-atlas/` (console)
- **VM:** `8CS-S04-C004001` (`vm-16890`), Windows Server 2008 R2, 4 vCPU / 16 GiB, one 200 GiB disk
- **Target:** namespace `8cs-servers-yg83h`, subnet `c4isrtdc-1245-csni-dev-ngdc-application`, storage class `c4isrtdc-otg-mcd-ds2`
- **Batch:** `imp-w1-migration-testing-wave-001-9d59f`, operation `8cs-s04-c004001-16890-b78af`, target VM resource `vm-xk6gba33ac`
- **Imported:** Fri 2026-11-13; rollback requested the same day; stuck until it was torn down by hand Mon 2026-11-16
- **Outcome:** The rollback never completed. The VM was released from the Supervisor by deleting its `VirtualMachine` object, and that detached its disk. Neither the VM nor a copy of its disk would boot afterwards, and the VM, being expendable, was deleted. Some Supervisor leftovers were **left in place on purpose** (see *State left behind*).

## Summary

The import worked and stopped at the commit gate (`commitAction: Wait`) as designed. The rollback patch was accepted. The operator's first rollback step then failed: it could not unregister the VM's disk from CNS. vCenter refused the CNS operation *"Unregister a virtual disk object to be a legacy disk"* with *"The operation is not supported on the object"*. The storage driver retried about every 4.5 minutes for three days (864 attempts by 11-16 18:21 UTC) and never succeeded.

The tool never reported any of this. It waited 30 minutes, then left the VM at `rolling_back` with "rollback not confirmed", and stopped polling. Triage stayed empty. Running Refresh at any point would have relabelled the VM `awaiting_commit`, because the failed rollback step is not in the tool's vocabulary.

Why vCenter refused is **still unknown**. Before the import, the disk had been moved from vSAN (thin) to a VMFS SAN datastore whose policy requires thick provisioning. The storage policy was edited **while the import was running**. The import ended with the disk Thick Provision Eager Zeroed, CNS-registered as a static PV via `cnsregistervolume`, and storage-policy **Compliant**. So the policy change is a suspect, but nothing proves it.

Getting out took manual work outside the tool. Delete the `VirtualMachine` in Automation (this releases the vCenter VM but **detaches its disk**). Delete the batch with kubectl. Refresh and Skip in the tool. Delete the VM and the CNS volume in vCenter. The `CnsUnregisterVolume` finalizer can't be removed by a namespace user. It cleared by itself once the CNS volume was deleted.

## Timeline (UTC)

| When | What happened | Source |
|---|---|---|
| 11-13 18:20:28 | Precheck 1 applied. | tool |
| 18:20:49 | Precheck failed. The mutating webhook denied the operation: *"no default storage class due to more than 1 storage policy exists"*. | tool transition |
| (before import) | Disk moved vSAN (thin) → `Datastore-WLD-tenant-LUN001-LDEV00:00:02` (VMFS, SAN). DS2 policy requires thick. | user |
| 18:25:44 | VM requeued. | tool |
| 18:26:07–18:26:27 | Precheck 2 passed (`PrecheckSucceeded`). Precheck batch deleted to release its reservation. | tool |
| 18:26:56 | Import batch applied. `commitAction: Wait`, no storage class in the manifest. | tool |
| during import | **Storage policy edited while the import was running.** | user |
| 18:33:16 | Stop pressed on the execute job. The in-flight batch was still polled. | job log |
| 19:00:41 | Child: `ReadyForCommit`, `VirtualMachineCreated`, `VirtualMachineReady` all True. vCenter compliance check: Compliant. Disk now thick eager-zeroed. | child status, vCenter |
| 19:00:55 | Tool: `awaiting_commit` (34 min import; probably mostly the thin→thick conversion). | tool |
| 19:01:27 | `rollback`: `rollbackAction: Immediate` patched (accepted, no "immutable" refusal). The operator creates `CnsUnregisterVolume unregister-vm-xk6gba33ac-3fa16c23` (`forceUnregister: true`, owned by the child). | tool, cluster |
| 19:01:28 | Child: **`RollbackCustomResourceCompleted=False ObjectDeletionFailure`**: *failed to unregister PVCs from CNS … failed to unregister associated volume f126123a-89a5-46e8-afd4-53cc9e8e10f7*. PVC and PV go `Terminating` and stay there (PV reclaim `Retain`). | child status |
| 19:31:41 | Tool: "rollback not confirmed within 30m; 1 VM(s) still reverting". **Last time the tool looked.** | job log |
| 11-13 → 11-16 | CNS retries every ~4.5 min. vCenter shows a "Reconfigure virtual machine" task by the mobility-operator user every few minutes. | events, vCenter tasks |
| 11-16 ~16:00 | Troubleshooting starts. The CLI on the jump box shows **0 VMs**: it was run outside `vcfa-atlas/`, so it opened a fresh `run/state.db`. | |
| 11-16 | vCenter task *"Unregister a virtual disk object to be a legacy disk"*, initiator `vsansvc-…`, on host `c4l-otg-jp71h16`: **"The operation is not supported on the object."** | vCenter |
| 11-16 | VM powered off from VCF Automation (`powerOffMode: TrySoft`). The unregister kept failing with the VM off (event count 852 at 17:26:43). **Power state ruled out.** | events |
| 11-16 | `VirtualMachine vm-xk6gba33ac` deleted from Automation (persistent volumes **not** ticked). The vCenter VM stayed: its `skip-delete-platform-resource: import in progress` annotation protected it. It came back powered off and **no longer managed by WCP**, but **Hard disk 1 was gone**: deleting the object detached the CNS volume. | Automation, vCenter |
| 11-16 | An **empty `vm-xk6gba33ac` reappeared** within seconds, most likely recreated by the still-running child. Status Unknown, with `CreateFailure: cannot synthesize class from nil ConfigInfo, NotSet, StorageClass is required but not specified`. Leftover events: `subnetport … VirtualMachine not found` and `cnsnodevmbatchattachment NodeVmBatchAttachFailed … Precondition failed: UID`. | events |
| 11-16 17:40:00 | Batch deleted with `kubectl delete importoperationbatch` (the tool's `abandon` refuses a `rolling_back` VM). The child went with it. The `CnsUnregisterVolume` got a `deletionTimestamp` but kept its `cns.vmware.com/unregister-volume` finalizer. | kubectl |
| 11-16 17:45:36 | Tool Refresh: batch gone → VM `rolling_back` → `failed` ("batch … disappeared from the cluster during refresh"); batch recorded `deleted`. | tool transition |
| 11-16 17:46:21 | Tool: VM `failed` → `skipped` ("manually skipped"). | tool transition |
| 11-16 | The released VM refused most operations: no add-existing-disk, and *Remove from Inventory* greyed out. *Delete from Disk* was still offered. | vCenter |
| 11-16 | Old VM deleted (Delete from Disk). The detached VMDK survived it. A new VM registered with the same VMDK **does not boot**. | user |
| 11-16 18:06–18:11 | The `CnsUnregisterVolume` is still retrying while marked for deletion (attempts 861, 862) and now acts on the disk attached to the new VM. Removing its finalizer was attempted: **`Forbidden`** for `sso:Administrator@vsphere.local` (namespace users can't patch `cnsunregistervolumes`). | kubectl |
| 11-16 | The CNS volume `f126123a` (CNS name `static-pv-86a35f87-df94-11f1-84f2-005056a5ca39`) deleted through Container Volumes; the VMDK goes with it. | vCenter |
| 11-16 18:21:48 | `CnsUnregisterVolume` attempt 864, same error. | events |
| 11-16 later | **`CnsUnregisterVolume` gone** (`No resources found`) with no finalizer removed by hand: once the volume no longer existed, the storage driver let go. | kubectl |
| 11-16 | A copy of the VMDK, taken earlier, attached to a VM: boot says **"no media found"**. Investigation stopped here. | user |

## What is known, and what isn't

**Known**
- The import itself succeeded; the commit gate held.
- The rollback request was accepted. Its first step, removing the CNS registration (PVC → `CnsUnregisterVolume`), is what failed. The Location and Property rollback steps never started.
- vCenter answers NotSupported to "unregister FCD to legacy disk" for this disk, with the VM on or off, attached to the original VM or to a new one.
- After the import the disk was eager-zeroed thick, on VMFS, CNS-registered as a static PV, and policy-compliant.
- Deleting the `VirtualMachine` object released the vCenter VM **and detached its CNS disk**. The child then recreated an empty `VirtualMachine` with the same name until the batch was deleted.
- The `CnsUnregisterVolume` keeps retrying after its owner is gone. It holds its finalizer until the unregister succeeds, and a namespace user can't remove that finalizer. **But deleting the CNS volume releases it:** after the volume was deleted in Container Volumes, the object disappeared by itself.
- *Delete from Disk* on the released VM did not delete the detached VMDK in its folder.

**Not known**
- **Whether the data survived.** Neither the new VM nor a copy would boot ("no media found"). Nobody looked at the copy's partitions, and the new VMs' firmware and controller weren't confirmed to match the original (2008 R2: BIOS, LSI Logic SAS, SCSI 0:0). "No media found" is also what a BIOS/EFI or controller mismatch looks like. If the data *was* destroyed, that is the most serious finding here, and the step that did it needs a hard guardrail. Candidates: the import with thin→thick under a mid-import policy edit, the unregister retries, the detach on VM-object delete, the VM delete. **Next time: inspect a copy read-only before deleting anything.**
- Why the unregister is NotSupported. Candidates: the disk's state after the storage policy changed mid-import; eager-zeroed thick on VMFS; registration through `cnsregistervolume`; the ESXi build of `c4l-otg-jp71h16`.
- Whether the released VM's operations were disabled (vCenter `AuthorizationManager` disabled methods), and under which source key. The lock was partial: Delete stayed available.
- Whether a rollback succeeds for a VM that stays on its original datastore with an unchanged policy. **The rollback path has never been seen to succeed in this lab on a SAN/VMFS target.**

## What the tool did badly

1. **Didn't recognise a failed rollback step.** `status.operator_status` only knows `RollbackCompleted`. A `False` `RollbackCustomResourceCompleted` with a real reason fell through to the import rules (`ReadyForCommit: True` → `AWAITING_COMMIT`). `_apply_rollback_status` counted that as "still reverting", and `_blocking_condition` only runs for RUNNING. The error the operator stated in its own conditions never reached the tool.
2. **Stopped watching.** After `rollback_timeout_minutes` (30), nothing polled the batch again. No alert, no Triage entry, no "stuck" badge: for three days the console said "rolling back".
3. **Refresh would have mislabelled the VM.** `refresh` → `_apply_status_to_vms` applies import rules to a `rolling_back` VM and would have set `awaiting_commit` ([engine.py:826](../../vcfaimport/engine.py)). The UI would then have offered Commit on a batch that already had `rollbackAction` set.
4. **Triage was empty.** No rule for a stalled rollback and no `KNOWN_ISSUES` pattern for `CnsUnregisterVolume` / `failed to unregister`.
5. **The UI enables "Abandon batch" for a `rolling_back` VM** ([core.js:807](../../vcfaimport/web/static/core.js)). The server refuses it (`abandon_targets`), so the button promises an action that will be refused. The engine is right; the UI should match it.
6. **No supported way out of a stuck rollback.** The only exit was to delete the VM object in Automation, then the batch with kubectl, then Refresh (which records "batch disappeared") and Skip. That sequence should be a documented, guarded path, and it has a cost the tool never mentioned: the disk detaches.
7. **Easy to look at the wrong workspace.** The CLI run from another directory silently opened an empty `run/state.db` and reported 0 VMs and no history. The job log prints `state : run/state.db` (relative).

## Guardrails to build

Ordered by how directly they would have changed this outcome. Each operator behaviour needs a fake and a test (CLAUDE.md: "the fakes should model what was observed").

### Status and polling (the tool has to see it)
- [ ] **Read every `Rollback*` condition.** Add `RollbackCustomResourceCompleted`, `RollbackVirtualMachineLocationCompleted` and `RollbackVirtualMachinePropertyCompleted` to `status.py`. Any one `False` with a non-working reason means rollback FAILED, and its message goes to the VM. Any one present with no `RollbackCompleted` means rollback running, not import state.
- [ ] **Rollback outranks import in refresh.** A VM in `rolling_back` must never move to `awaiting_commit` or `committed` from import conditions. Only a rollback verdict, or a vanished batch, moves it.
- [ ] **Fake:** in `tools/fake_kubectl.py`, a mode where rollback stops at `RollbackCustomResourceCompleted=False ObjectDeletionFailure` with the CNS message. Tests: `rollback` reports ROLLBACK FAILED within one poll, and `refresh` on that batch keeps the VM out of `awaiting_commit`.
- [ ] **Keep following unconfirmed rollbacks.** After the 30-minute wait, `watch` and the console's live view should still poll `rolling_back` batches, and pulse should raise an alert ("rollback unconfirmed for Nh").

### Triage
- [ ] Stalled-rollback group: `rolling_back` longer than `rollback_timeout_minutes`, showing the child's blocking condition.
- [ ] A `KNOWN_ISSUES` entry for `failed to unregister (PVCs|associated volume)|CnsUnregisterVolume`. Advice: check the vCenter task "Unregister a virtual disk object to be a legacy disk", and point to the runbook below.

### Before import (avoid getting here)
- [ ] **Preflight: storage classes of the target namespace.** More than one storage policy and no default means the webhook will refuse (seen at 18:20). Say so before the precheck, and name the classes.
- [ ] **Readiness: disk provisioning vs the target policy.** A thin source disk and a thick-requiring policy means conversion during import: a longer import, more space, and a rollback path that has not been proven. Warn, don't block (opt-in, per CLAUDE.md).
- [ ] **Snapshot the namespace's storage classes / policies at import start and compare at the end.** If they changed mid-import, flag the batch: "storage changed while importing; rollback may not be possible".
- [ ] **Rollback rehearsal flag.** Record per (datastore type, storage class) whether a rollback has ever been confirmed. Before the first real import on an unproven combination, warn that `commit_action = "Wait"` is only a safety net once a rollback has been seen to work there.
- [ ] **Runbook rule:** no storage-policy or datastore changes while an import is in flight.

### Getting out (when it happens anyway)
- [ ] **Match the UI to the engine:** disable "Abandon batch" for `importing` / `awaiting_commit` / `rolling_back`, with the reason in the tooltip.
- [ ] **Rollback preview text:** "rollback is best-effort; if a step fails the VM stays with the Supervisor".
- [ ] **A guided "release a stuck rollback" path,** documented in LAB-GUIDE Troubleshooting, and maybe as a command that only *checks* and *records* (it never deletes the VM itself):
  0. **If the VM matters, stop here and go to VMware support.** Everything below costs the VM its disk attachment. Before anything is deleted, copy the VMDK and inspect the copy read-only (partitions, NTFS).
  1. Confirm the child shows a failed `Rollback*` step.
  2. Delete the `VirtualMachine` in Automation, leaving the persistent-volumes box unticked. The vCenter VM stays (`skip-delete-platform-resource`) but **its CNS disk is detached**, and the VM may be left with some operations disabled (here: no add-disk, no Remove from Inventory; Delete still allowed).
  3. Delete the batch. Recreation of an empty `VirtualMachine` stops once the child is gone. The tool's `abandon` refuses this case, so it's `kubectl delete importoperationbatch`.
  4. In the tool: Refresh (records the VM failed, "batch disappeared"), then Skip.
  5. vCenter: delete the CNS volume through **Container Volumes** (its file and record go together), then delete the VM. *Remove from Inventory* may be greyed out; *Delete from Disk* on a VM whose disk is already detached leaves that VMDK alone.
  6. Leftovers: the empty `VirtualMachine` (delete in Automation), the PVC/PV in `Terminating` (`Retain`: they can't take data with them), and the **`CnsUnregisterVolume`**. That one keeps retrying the failing unregister (about every 4.5 minutes) after its owner is gone, and holds `cns.vmware.com/unregister-volume` until it succeeds. **A namespace user can't remove that finalizer** (`Forbidden … cannot patch resource "cnsunregistervolumes"`), but they don't need to: **deleting the CNS volume in step 5 makes it go away by itself** (observed). Only if the volume must be kept does it need cluster-admin on the Supervisor control plane (`decryptK8Pwd.py` on the VCSA → SSH to a CP node → its kubectl) or VMware support. Until then it's noise: it only ever tries to drop a CNS registration.

### Workspace
- [ ] Print the **absolute** workdir in the CLI status header and job logs.
- [ ] When a command finds an empty or new `state.db` but the config's maps or inventory are populated, warn: "empty workspace at <path>: is the campaign elsewhere (console started from another directory)?"

## Operator facts to add to CLAUDE.md (observed 2026-11-13 → 16)

- A rollback creates a `CnsUnregisterVolume` (cns.vmware.com/v1alpha1, `forceUnregister: true`, `spec.pvcName`, owned by the child, finalizer `cns.vmware.com/unregister-volume`). Its failure shows on the child as `RollbackCustomResourceCompleted: False (ObjectDeletionFailure)`. The Location and Property rollback steps don't start until it succeeds. The storage driver retries about every 4.5 minutes, indefinitely, even after its owner is deleted, and namespace users can't remove its finalizer. Deleting the CNS volume releases it.
- vCenter can refuse the underlying "Unregister a virtual disk object to be a legacy disk" with NotSupported. Seen on an eager-zeroed thick disk on VMFS whose storage policy had been edited during the import.
- Imported VMs carry `vmoperator.vmware.com.protected/skip-delete-platform-resource: import in progress`. Deleting the `VirtualMachine` while the import is unfinished leaves the vCenter VM in place, unmanaged, **with its CNS disk detached**.
- While the child exists, a deleted `VirtualMachine` is recreated empty (no class, image or storage class → `CreateFailure`). Deleting the batch stops that.
- Imported-VM PVCs are static PVs created through `cnsregistervolume`, reclaim policy `Retain`, Block mode. The disk stays in place in the VM's folder. CNS lists it under its own name (`static-pv-<uuid>`), different from the PV name. The VM carries `vmoperator.vmware.com/no-unmanaged-volumes-register: "true"`.

## State left behind (2026-11-16, decision: leave it)

Nothing below holds data any more.

| Where | Object | State |
|---|---|---|
| Supervisor `8cs-servers-yg83h` | `cnsunregistervolume/unregister-vm-xk6gba33ac-3fa16c23` | **Gone**: cleared by itself after the CNS volume was deleted |
| Supervisor `8cs-servers-yg83h` | PVC `vm-xk6gba33ac-3fa16c23`, PV `static-pv-f126123a-…` | Last seen `Terminating`; expected to clear now that the `CnsUnregisterVolume` is gone (not rechecked) |
| Supervisor `8cs-servers-yg83h` | `virtualmachine/vm-xk6gba33ac` (the empty recreation) | Last seen present. Delete it in Automation if it's still there. |
| vCenter | The new test VM, and the copy of the VMDK | User's to delete |
| Tool (`vcfa-atlas/run/state.db`) | `vm-16890` | `skipped`; batch `deleted`; the full history is in the ledger |

To check later: `kubectl --context supervisor get cnsunregistervolume,virtualmachine,pvc -n 8cs-servers-yg83h` and `kubectl --context supervisor get pv | grep f126123a`.
