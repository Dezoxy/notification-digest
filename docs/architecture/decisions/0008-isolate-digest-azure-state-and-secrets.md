# 8. Isolate digest Azure state and secrets

Date: 2026-10-08

## Status

Accepted

Supersedes the Terraform backend and shared-vault choices in
[7. Run digest as Azure jobs](0007-run-digest-as-azure-jobs.md).

The owner requires Azure state and a dedicated digest vault. Using the existing
subscription is the preparation recommendation, subject to the owner's final
subscription selection. Implementation is being prepared; no backend, vault,
secret copy or production cutover has been applied.

## Context

The first migration preparation retained the homelab Key Vault and selected HCP
Terraform for remote infrastructure state. Runtime SQLite bundles already used
Azure Blob Storage. The owner requested that state be on Azure and that digest
secrets move to their own vault, and asked whether another Azure subscription
is needed. The source homelab vault also serves applications staying on the VM.

## Decision drivers

- Owner requirement: persist state on Azure and isolate digest credentials.
- Preserve subscription-based Claude authentication and existing credential values.
- Remove cloud runtime access to the shared homelab vault.
- Keep infrastructure state independent of app-stack changes and runtime identity.
- Keep complexity and cost proportionate to a single-owner finite workload.
- Recovery remains in Azure; no return-to-VM procedure.

## Considered options

1. Retain HCP Terraform and the shared homelab vault.
2. Use dedicated Azure state/storage/vault resources in the existing subscription.
3. Create another subscription for separate billing and administrative boundaries.

## Decision

Keep the existing subscription as the recommended default, selecting its ID
explicitly. Dedicated resource groups, identities and scoped roles provide the
needed separation. A separate backup resource group/account isolates daily
runtime copies from routine state pruning. Only the backup job receives its
additional container-scoped writer identity; other jobs cannot access backup
state. Versioning/14-day soft deletion and 30-day copy retention are prepared,
but there is no immutable retention or independent subscription/admin boundary.
A restore from that account is a pilot gate. A separate subscription earns its
extra setup when ownership,
billing policy or quotas require that boundary; those requirements are absent.

Store Terraform state in a separate West Europe Azure Blob backend, bootstrapped
outside the app stack, with Entra/OIDC access, Blob-lease locking, disabled Shared
Key, private container access, versioning, soft deletion and independent deletion
protection. The deployment identity can access this backend; the runner cannot.
Confirm any prior HCP workspace contains no managed resources before fresh
initialization. An unexpected existing state requires explicit remote-backend
migration rather than starting a parallel empty state. Homelab HCP state is
unaffected.

Create an app-owned Standard Key Vault in West Europe with RBAC, purge protection
and seven-day soft deletion. Retention is fixed at creation and purge protection
cannot be disabled after enabling it; decide before the foundation apply.
The runner receives Secrets User on this dedicated
vault and Blob Data Contributor on its runtime container. Terraform contains
secret names/references and metadata only; no secret values or managed secret
resources. Use a separate operator tool for metadata preview, explicit in-memory
copy and verification of the reviewed digest-only manifest. Preserve source
secrets, values and metadata; refuse conflicting target values/metadata by
default. An explicitly reviewed `--apply --replace-target NAME` selects exactly
one manifest target for a new version after source rotation. The 17-entry
manifest excludes the unused historical X cookie seed. Keep
shared homelab failure-alert credentials where remaining applications need them.

Provision in two reviewed stages: first the foundation with no jobs; then, after
secret copy/verification, create nine Manual jobs with schedules disabled.
Container Apps can resolve vault references during resource creation, so a
Manual trigger alone is not a safe empty-vault bootstrap strategy. State handoff,
pilot validation and explicit schedule activation remain separate gates.

## Consequences

- Runtime state, infrastructure state and digest vault storage are on Azure in
  West Europe, with separate access and recovery responsibilities.
- The homelab vault stays intact for remaining applications; cloud jobs no longer
  need its secrets or its RBAC grants.
- Backend/backup storage add cost and bootstrap/recovery responsibility; vault
  operations and state versions must be included in measured cloud spending.
  The app-resource-group budget excludes both separate groups and is not a cap.
- One subscription shares billing and subscription-level administrative authority;
  resource-group isolation does not create a new billing/security account.
- Azure backend locks and vault references add Azure coupling. SQLite and release
  containers remain portable. External services retain their processing terms;
  EU Azure storage alone does not establish GDPR compliance.

## Risks

Role propagation, job secret-reference validation, OIDC backend access and backup
restore need cloud pilot evidence. Secret copying is not rotation: compromised
source values require a separately authorized rotation. Keep credential edits
paused during copy because Key Vault lacks reliable create-only secret writes.
Protect Terraform state and private plan artifacts because provider-computed
credentials can appear there. The guarded `digest.cloud_cookies` command rotates
the canonical seed/live cookie pair under the state lease; the transferred
live-cookie bundle stays authoritative. Egress/authentication for every enabled
source and publication lane is an explicit go/no-go pilot gate.
The owner requested current CLI 2.1.294 with official Node 22 for the migration
image. Its credential-free installation smoke does not establish live
subscription access or equivalent editorial output; both remain pilot gates.
Operator commands use temporary private workspaces; explicit export files remain
operator-owned verification/recovery material.

## Related

- [7. App-owned cloud runtime](0007-run-digest-as-azure-jobs.md)
- [Terraform backend and dedicated-vault runbook](../../azure-migration.md)
- [Azure resources and secret manifest](../../../infra/azure/)
- [Metadata preview, copy and verification tool](../../../scripts/migrate_azure_secrets.py)
