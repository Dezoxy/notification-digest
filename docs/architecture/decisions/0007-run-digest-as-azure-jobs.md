# 7. Run digest as app-owned Azure jobs with leased SQLite snapshots

Date: 2026-10-08

## Status

Accepted

Amends the hosting portion of [3. SQLite is the source of truth](0003-sqlite-is-the-source-of-truth.md).
Supersedes the deployment ownership portion of [6. Release by tag and pin](0006-release-by-tag-and-pin.md).

Implementation is being prepared; production remains on the VM until the
verified state handoff and explicit schedule activation.

## Context

The owner approved moving notification-digest off the shared homelab VM,
keeping the existing Claude CLI subscription. Nine finite scheduled modes
already run from one container image. A 2026-10-08 read-only measurement
projected approximately 17.86 execution hours/month before cloud startup and
state transfer, with a sampled working-set peak near 489 MiB. These observations
support starting at 0.5 vCPU/1 GiB, but are not cloud sizing or cost proof.

SQLite holds cursors, digest IDs and delivery history. An ephemeral scheduled
container therefore needs a durable handoff of state and exclusive ownership
across all modes. Telegram acknowledgement and database persistence cannot
commit atomically. The original VM filesystem/lock and separate homelab deploy
ownership no longer meet the intended target, while SQLite authority, the D1
rendering projection and explicit image releases remain appropriate.

## Decision drivers

- Owner request: remove digest dependence on homelab hosting and scheduling.
- Owner requirement: preserve subscription auth, models and editorial behavior.
- Preserve state lineage and prevent concurrent use of the Telegram session.
- Keep cloud spending and operational complexity proportional to one owner.
- Owner decision: recovery stays in Azure; no VM return procedure.

## Considered options

1. Continue the current self-hosted VM jobs.
2. Move the container onto an always-running Azure VM.
3. Use scheduled Consumption Container Apps Jobs with Blob-backed SQLite bundles.
4. Rewrite around a provider function runtime and managed transactional database.

## Decision

Use app-owned [Terraform](../../../infra/azure/) and an explicit reviewed
[plan/apply workflow](../../../.github/workflows/azure-deploy.yml). Publish exact
GHCR releases through the existing tag workflow and pin the selected migration
release in Azure configuration. Initialize schedules disabled, transfer final
state after pausing/draining the VM, verify the cloud pilot and activate the
nine jobs. The [migration runbook](../../azure-migration.md) owns exact commands,
activation gates and recovery details.

Run SQLite locally during each finite job. A shared renewable Blob lease and
guarded canonical manifest serialize all modes. Immutable consistent snapshots
carry the database, live collector cookies and archives. Checkpoint before
external delivery and after acknowledgements/cursor changes. Unknown Telegram
send outcomes require reconciliation before automatic resend. Missing/corrupt
state or lost lease ownership fails closed.

Use the existing Key Vault through explicit per-secret managed-identity access.
Use a Key Vault-backed read-packages PAT for private GHCR pulls. Keep production
state and logs in EU regions, bounded retention, aggregate failure/freshness
alerts and a small monthly budget notification. Preserve Claude subscription
authentication and its isolated subprocess environment.

## Consequences

- Digest hosting/configuration can be reviewed and released from this repository;
  the remaining homelab services keep their deployment ownership.
- Finite jobs avoid paying for an always-on VM; full snapshot checkpoints add
  storage transfer/write cost that must be measured during the pilot.
- SQLite remains authoritative; Cloudflare D1 remains a disposable projection.
- Global lease fencing, durable checkpoints and uncertain-delivery handling
  become required runtime contracts. Exactly-once Telegram delivery is not
  promised.
- Cloud state recovery replaces returning execution to the VM. Shared alert
  credentials must be separated before digest retirement from homelab.
- Azure jobs, identity and Blob leases add provider coupling, while the database
  and release container remain portable.

## Risks

Cloud egress/session acceptance, cold starts, actual checkpoint costs, provider
secret-reference drift, monitor log schema and restore behavior need pilot
evidence. A validation/test pass alone is not production proof. Shared Key Vault
and subscription rate limits remain external dependencies.

## Related

- [3. SQLite authority](0003-sqlite-is-the-source-of-truth.md)
- [6. Exact image release](0006-release-by-tag-and-pin.md)
- [Migration, operations and recovery](../../azure-migration.md)
