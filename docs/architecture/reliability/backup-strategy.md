## Backup Strategy

The only store this system needs to back up is the SQLite system of record
(`state.db`), which lives as an immutable bundle in the runtime storage account
([ADR 0003](../decisions/0003-sqlite-is-the-source-of-truth.md),
[ADR 0007](../decisions/0007-run-digest-as-azure-jobs.md)). Everything else is
either disposable, replaceable from that file, or out of this repo's scope.

Until 2026-10-08 the state was `/srv/appdata/digest/state.db` on the homelab VM,
backed up by a systemd timer. That VM copy and its old backups were left in
place, frozen, at the cutover. They are history, not a recovery source for the
running system, and nothing below depends on them.

### What is backed up

| What | Method | Schedule | Retention | Where | Verified |
|---|---|---|---|---|---|
| State bundle: `state.db` (items, cursors, digests, delivery state, arc context), the live X cookie jar and the Markdown archive | The `backup` job copies a validated recovery bundle and its manifest | Daily, 04:15 UTC | 30 days (storage lifecycle rule) | Backup storage account, in a separate resource group, same region as the runtime | The copy is size- and SHA-256-checked by readback before the slot can complete. **A restore from this account has not been exercised** |
| Checkpoint generations | Every run checkpoints a new immutable bundle; the runner prunes by reference under the lease | Per run | Seven daily referenced generations in the runtime account | Runtime storage account | Useful local history, but not an independent backup: same account as the live state |
| Site Database (Cloudflare D1) | None. Deliberately not backed up | — | — | — | Disposable rendering copy, rebuildable from the state — but see the gap noted below |
| Runtime secrets (digest Key Vault) | Azure's own soft-delete and purge protection; secret values are copied by a reviewed migration tool, not held in Terraform | — | — | — | Out of scope for this document |
| Infrastructure (Terraform in `infra/azure/`, workflows) | Git history in this repository; Terraform state in Azure Blob Storage in a separate backend resource group | Per change | Repository history | — | Out of scope for this document |

The bundle is self-contained: the Markdown archive and the X cookie jar travel
with the database, so the broader archive backup that PLAN.md records for the VM
era is not a separate Azure concern. The archive inside the bundle keeps
365 days, which is distinct from the 90-day item pruning of the database.

The backup job runs under its own managed identity, attached only to that job,
and writes to a private container in the backup account. Shared-key access to
both storage accounts is disabled. It uploads the bundle first, verifies its
stored size and SHA-256 by streaming readback, then uploads and verifies the
manifest as the completion marker; a copy without a manifest is not a backup. A
final failed copy fails the job, and the `failures` alert e-mails the owner
([observability-architecture.md](../observability/observability-architecture.md)).
The snapshot is a SQLite online backup (`connection.backup`) taken inside a run
that holds the state lease, so it is consistent without stopping anything.

What the backup account does and does not protect against
([migration runbook](../../azure-migration.md#independent-daily-backup-account)):

- It survives loss or corruption of the runtime storage account, and a mistaken
  deletion there, because it is a separate account written by a separate
  identity.
- Blob versioning and 14-day soft deletion cover logical deletion in the backup
  account; the lifecycle rule expires copies and old versions after 30 days.
- It is **not immutable storage**: the backup writer can delete copies, and both
  accounts share one subscription, one region and the same administrative
  authority. Loss of the region or of the subscription loses both. Cross-region,
  cross-subscription or immutable retention is a future requirement decision,
  not claimed protection.

### Restore

The application has no automatic restore path. Restoring is an operator
procedure ([migration runbook](../../azure-migration.md#interrupted-runs-and-recovery)):
drain the jobs, grant a recovery operator temporary read access to the backup
container, download one complete prefix (bundle and manifest together) into a
private directory, verify its hash, SQLite integrity and lineage, then
bootstrap it into a **fresh state namespace**, point the reviewed Terraform
configuration at that namespace and resume the jobs. The damaged manifest is
never overwritten, so the evidence is kept. A rehearsal does the same into a
throwaway namespace and compares a second export.

**This procedure has not been run against the backup account.** The runbook
makes a successful restore from the separate account a pilot acceptance
criterion, and until it is done the restore path is the intended one, not a
rehearsed one. The one restore exercised on the VM
([QA-05](../requirements/quality-attributes.md)) used a VM-era snapshot and does
not carry over to Azure.

Restoring rolls state back to the snapshot's point in time. Because `digests`
rows carry per-channel delivery flags, restoring to a snapshot taken *before* a
digest was marked delivered — followed by a run's normal pending-digest retry
pass — can cause that digest to be resent. This is a real tension with the
system's one hard rule ([QA-01](../requirements/quality-attributes.md), "a
duplicate delivery is not tolerable") that a naive restore does not
automatically avoid; there is no automated safeguard against it today
([TD-006](../risks/technical-debt.md)). The runbook's answer is procedural:
after an older restore, reconcile the destination history and the affected
cursors and delivery intent before resuming publication, and keep any
uncertain-delivery records across the recovery.

### What restoring does *not* recover

- Items collected and delivered between the snapshot and the loss are gone from
  the state, though most sources (notably Telegram, since messages remain in
  the group) can be re-collected once cursors catch up — see
  [disaster-recovery.md](disaster-recovery.md) for the RPO this implies per
  scenario.
- The Site Database (D1) is not restored from any backup of its own; it is
  rebuilt by republishing from the state.
  [A-03](../requirements/assumptions.md) records that this rebuild path is
  currently untested end to end ([TD-002](../risks/technical-debt.md)), so "D1
  is disposable" is a design intent, not a verified recovery procedure.
