# Azure digest migration and operations

Status on 2026-10-08: the first preparation PR is merged; the Azure state and
dedicated-vault follow-up is being prepared. Production still runs
on `01-myapps-vm`. This runbook describes the authorized hosting target and the
remaining deployment gates. Returning execution to the VM is outside the plan.

The target runs the existing release image as nine finite Container Apps Jobs
in West Europe, with 0.5 vCPU, 1 GiB, no platform retries and a 50-minute
platform timeout. The runner allows at most ten minutes waiting
for the lease and 35 minutes of active work, then fences the process group.
It preserves `CLAUDE_CODE_OAUTH_TOKEN` subscription authentication, the VM's
measured Claude CLI 2.1.284, models, effort, prompts, translations, verification
and OpenRouter fallbacks. Main independently adopted CLI 2.1.293 during
preparation; adopting that update is deferred until the pilot verifies it.
A hosting change does not remove subscription contention with other
applications using the same account.

## Subscription, state and secret ownership

Use the existing Azure subscription for this single-owner workload. Choose its
subscription ID explicitly: several subscriptions can have the same display
name. A new subscription would add billing/access setup without solving a
current requirement. Reconsider it for a different owner, separate billing,
organization policy or subscription quotas. Isolation here uses dedicated
resource groups, managed identities and narrowly scoped roles.

Both kinds of state live in Azure:

| Store | Purpose | Ownership and access |
|---|---|---|
| Runtime Blob Storage in the app resource group | Canonical SQLite/cookie/archive bundles and retained backups | Terraform manages resources; runner has Blob Data Contributor on `digest-state` only |
| Separate Blob Storage in a backend resource group | Terraform state and Blob-lease locking | Bootstrapped outside the app stack; deployment identity has access to `terraform-state`; runner has none |
| Dedicated digest Key Vault in the app resource group | Subscription OAuth, sessions, publication/collector credentials and GHCR pull token | Terraform manages the vault and roles, never secret values; runner has Secrets User on this vault only |

The runtime manifest is authoritative between jobs; `/data` is the temporary
working copy during an execution. Terraform state is separate from application
state. This stack uses the Azure `azurerm` backend, replacing the original HCP
Terraform choice. The homelab repository's HCP workspaces stay unchanged.

## Bootstrap the Azure Terraform backend

Prepare a dedicated West Europe backend resource group and globally unique
storage account through the established Azure administrative process. Its
lifecycle is independent of the app Terraform stack: retain it during app
changes and recovery. Protect the backend resource group with a `CanNotDelete`
management lock. Record its ownership, permissions and recovery settings
in the deployment record. These commands create resources and grant access;
review the selected subscription, names and principals before executing them.
They are preparation instructions, not actions already performed.

```sh
export AZURE_SUBSCRIPTION_ID=YOUR_CHOSEN_SUBSCRIPTION_ID
export DIGEST_BACKEND_RESOURCE_GROUP=notification-digest-terraform-westeurope
export DIGEST_BACKEND_STORAGE_ACCOUNT=YOUR_UNIQUE_BACKEND_ACCOUNT
export DIGEST_DEPLOY_PRINCIPAL_ID=YOUR_GITHUB_OIDC_SERVICE_PRINCIPAL_OBJECT_ID
export DIGEST_BOOTSTRAP_PRINCIPAL_ID=YOUR_OPERATOR_OBJECT_ID
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
az group create --name "$DIGEST_BACKEND_RESOURCE_GROUP" --location westeurope
az lock create --name protect-digest-tfstate --lock-type CanNotDelete \
  --resource-group "$DIGEST_BACKEND_RESOURCE_GROUP"
az storage account create --name "$DIGEST_BACKEND_STORAGE_ACCOUNT" \
  --resource-group "$DIGEST_BACKEND_RESOURCE_GROUP" --location westeurope \
  --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 \
  --allow-blob-public-access false --allow-shared-key-access false
az storage account blob-service-properties update \
  --account-name "$DIGEST_BACKEND_STORAGE_ACCOUNT" \
  --resource-group "$DIGEST_BACKEND_RESOURCE_GROUP" \
  --enable-versioning true --enable-delete-retention true --delete-retention-days 14 \
  --enable-container-delete-retention true --container-delete-retention-days 14
DIGEST_BACKEND_ACCOUNT_ID=$(az storage account show \
  --name "$DIGEST_BACKEND_STORAGE_ACCOUNT" \
  --resource-group "$DIGEST_BACKEND_RESOURCE_GROUP" --query id --output tsv)
az role assignment create --assignee-object-id "$DIGEST_BOOTSTRAP_PRINCIPAL_ID" \
  --role 'Storage Blob Data Contributor' --scope "$DIGEST_BACKEND_ACCOUNT_ID"
# Wait for this role to propagate before the data-plane operation below.
az storage container create --name terraform-state \
  --account-name "$DIGEST_BACKEND_STORAGE_ACCOUNT" --auth-mode login --public-access off
az role assignment create --assignee-object-id "$DIGEST_DEPLOY_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal --role 'Storage Blob Data Contributor' \
  --scope "$DIGEST_BACKEND_ACCOUNT_ID/blobServices/default/containers/terraform-state"
```

Azure-managed encryption is enabled by Storage by default. Verify private
container access, disabled Shared Key, versioning, soft deletion and successful
Entra data-plane access after role propagation. Retain only the operator access
needed by the recovery process. Never fall back to account keys or local state
when Entra access fails. GitHub OIDC authenticates backend access with
`use_azuread_auth=true` and `use_oidc=true`; no HCP token is needed.

GitHub environment variable `AZURE_TERRAFORM_BACKEND_JSON` contains exactly:

```json
{
  "resource_group_name": "notification-digest-terraform-westeurope",
  "storage_account_name": "YOUR_UNIQUE_BACKEND_ACCOUNT",
  "container_name": "terraform-state",
  "key": "notification-digest.tfstate"
}
```

Check the original HCP workspace before initialization. The expected state has
zero managed resources because no production apply occurred during preparation.
If a workspace contains resources, stop and plan an explicit remote-backend
migration; do not initialize a parallel empty state or dump credentials to a
local state file.

These four fields are nonsecret locations, supplied to `terraform init` through
an ignored backend configuration file. Configure them identically for plan and
apply. Do not add tokens, access keys or SAS URLs. Blob state can contain
provider-computed credentials: restrict backend access and treat state versions
and saved plans as sensitive. Test backend recovery separately from runtime
bundle restore; an app backup does not back up Terraform state.

## Prepare configuration and deployment identity

The stack lives in [infra/azure](../infra/azure/). Create separate GitHub deployment
identity/federated credentials for this app. The workflow uses subjects
`repo:Dezoxy/notification-digest:environment:azure-plan` and
`repo:Dezoxy/notification-digest:environment:azure-production`, audience
`api://AzureADTokenExchange`. Restrict both environments to `main`. Configure
required reviewer approval on `azure-production`; the apply job fails closed
if no required reviewers are configured. Review the saved plan before
approving its apply job. These repository/Entra settings are prerequisites,
not settings that the workflow YAML can enforce by itself.

The deployment identity needs Contributor on the app resource group and
permission to grant the runner's container Blob and dedicated-vault Secrets
User roles. Use a constrained Role Based Access Control Administrator assignment
for those grants; review principal/role/scope conditions with the administrator.
Do not give the deployment identity secret-value access to the homelab vault.
The separate secret-copy operator receives only the temporary source-read and
target-write roles needed for the reviewed migration. Bootstrap resource-group
creation and resource-provider registration separately if the deployment
identity is scoped to an existing group. Register `Microsoft.App`,
`Microsoft.Storage`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights`,
`Microsoft.ManagedIdentity`, `Microsoft.Insights` and `Microsoft.Consumption`
through the account's established administrative process. The provider does not
automatically register providers. Import a precreated app resource group into
the Azure backend before a first plan; do not create a local state file. For a
workstation operator using Azure CLI login, initialize the same remote backend
with `-backend-config=use_oidc=false` to override the CI OIDC setting. Keep
`use_azuread_auth=true`, explicit subscription selection and the exact backend
locations. Then import `azurerm_resource_group.digest` using its ARM ID before
the first workflow plan. The import needs the reviewed Terraform variables;
do not improvise defaults or bypass backend access failures.

Supply `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
`AZURE_TERRAFORM_BACKEND_JSON` and `AZURE_TERRAFORM_VARS_JSON` as GitHub environment
**variables**. Both plan and apply environments need the same identity,
subscription and backend locations. `AZURE_TERRAFORM_VARS_JSON` contains reviewed
nonsecret Terraform inputs, including `key_vault_name` and the environment-name
to secret-name map `secret_names`. Terraform constructs versionless references
to the new vault. Secret values never enter Terraform inputs or managed secret
resources. Do not configure `TF_API_TOKEN` for this Azure stack.

Start from [production.auto.tfvars.example](../infra/azure/production.auto.tfvars.example).
The app configuration was rendered offline from the declared homelab defaults
and host overrides on 2026-10-08. Recheck the baseline against final live
configuration before cutover: collector flags, feeds/allowlists, topics,
model/effort/budgets, translation, verification, context, delivery channels and
fallback omission semantics. Leaving `FALLBACK_MODELS` unset retains the app's
defaults; an empty value disables them. Azure env values are literal: keep
`$FET,$ASI` as single dollars, unlike homelab Compose env-file escaping.

Choose globally unique names for the runtime storage account and digest vault,
plus the alert email; update the budget start to the first day of the deployment
month. Select a tested migration release image before manually executing any
job. The default `0.28.0` records the source baseline and does **not** contain the
cloud runner. Activation also requires `migration_release_verified=true`. The
stack rejects floating tags.

The new Standard Key Vault uses RBAC, purge protection and 90-day soft deletion.
The runner has Secrets User access to the dedicated vault and Blob access to its
runtime container; it cannot read backend state or the source vault. Private
GHCR pulls use a read-packages PAT via a Key Vault-backed registry password
reference; Azure managed identity cannot directly authenticate to GHCR. OAuth,
session strings, cookies and publication credentials never enter Terraform
inputs. AzureRM may store provider-computed Storage/Log Analytics keys in state:
protect backend state and private one-day plan artifacts; do not share binary
plans. Shared Key storage authentication is disabled.

## Copy digest secrets to the dedicated vault

The reviewed [secret manifest](../infra/azure/secret-migration.json) names the
17 runtime/registry secrets plus `digest-x-cookies`, the historical cookie seed.
Keep names stable; the migration validates and copies the latest values/metadata
to the new vault. It does not delete, modify or rotate source secrets. Shared
homelab failure-notification credentials remain in the source vault because
other services still need them; Azure Monitor uses the configured email action
group. No unrelated homelab secrets belong in the digest vault.

Provision the foundation with `jobs_enabled=false` and `schedules_enabled=false`
first. This creates the vault/storage/identities and monitoring support without
job resources. Container Apps can resolve secret references while creating a
Manual job, so delaying execution alone is insufficient. Then review the
metadata-only preview from
[scripts/migrate_azure_secrets.py](../scripts/migrate_azure_secrets.py). Use the
operator's Azure CLI login; never redirect secret JSON into a file or shell
variable. The tool's default preview prints vault/secret names and presence
status, not values. An explicit `--apply` copies missing secrets in memory;
`--verify` checks
values and metadata without writing or printing values. An existing target is
skipped only when value and enabled/expiry/not-before/tags/content-type metadata
match; any difference fails preflight before writes. Review conflicts and source
metadata before proceeding. Pause credential edits during copy, because Key Vault
does not offer a reliable create-only secret write. Both vaults must use the same
Entra tenant; the operator needs source list/get and target list/get/set access.
Run preview again after any source rotation.

```sh
# Preview only: the destination vault must already exist.
uv run python scripts/migrate_azure_secrets.py \
  --target-vault YOUR_DIGEST_VAULT --subscription "$AZURE_SUBSCRIPTION_ID"
# After review: copy missing values, then verify without writing.
uv run python scripts/migrate_azure_secrets.py \
  --target-vault YOUR_DIGEST_VAULT --subscription "$AZURE_SUBSCRIPTION_ID" --apply
uv run python scripts/migrate_azure_secrets.py \
  --target-vault YOUR_DIGEST_VAULT --subscription "$AZURE_SUBSCRIPTION_ID" --verify
```

The default source is `kv-homelab-prod-th`; the default manifest is
`infra/azure/secret-migration.json`. `--source-vault` and `--manifest` override
those explicit names. After the 18 copies pass verification, review a second
plan/apply with `jobs_enabled=true` and `schedules_enabled=false` to create the
nine Manual jobs. No source secret removal is part of this procedure.

The `digest-x-cookies` vault copy preserves the seed only. Production cookie
state comes from the final transferred `x-cookies.json.live` bundle; never
replace it with an older vault seed. A standalone cloud-cookie rotation command
is not yet implemented, so rotation/recovery must follow the guarded state
handoff procedure until that operator workflow is added and tested.

## Validate, release and provision disabled jobs

```sh
uv run ruff check .
uv run pytest
terraform fmt -check -recursive infra/azure
terraform -chdir=infra/azure init -backend=false -input=false -lockfile=readonly
terraform -chdir=infra/azure validate
make check
make docs
```

Backend-free init above downloads providers for validation only; it creates no
state and performs no plan/apply. Review and merge application changes, then
publish the explicit `vX.Y.Z` release through the existing release workflow.
Set the reviewed image pin to its `X.Y.Z` GHCR tag or digest. Run
`azure-deploy` with `operation=plan`. Inspect resource ownership, RBAC, cost,
image/configuration, state retention and the dedicated vault. The foundation
plan must contain
**zero jobs** with `jobs_enabled=false`. After secret copy/verification, the
second plan with `jobs_enabled=true` must contain nine **Manual** jobs and only
dedicated-vault references. Jobs must not execute until state bootstrap is
completed and verified.

For each provisioning stage, dispatch `operation=apply`, review the newly
generated plan artifact, then approve `azure-production`. Apply uses that exact
saved plan.
Artifacts expire after one day. Azure tables/KQL, image pull, CLI authentication,
cloud egress and measured resource use remain live pilot checks; validation
does not prove them. Check a subsequent no-op plan for provider drift around
Key Vault-backed job secrets before activating schedules.

## State handoff and activation

Complete shared homelab alert separation first: jobs-refresh currently uses
`DIGEST_NOTIFY_*` credentials independently of the digest application. Retain
the legacy secret scope until the generic myapps alerts are deployed and tested.
Do not revoke shared Docker/GHCR credentials or remove VM backup machinery.

Pause all nine VM timers: `digest`, `digest-overnight`, `digest-evening`,
`digest-daily`, `digest-weekly`, `digest-positions`, `digest-patreon`,
`digest-relay`, and `digest-backup`. Drain their matching services and Docker
one-shots, including the backup writer. Confirm no digest process or Telegram
session remains. Never run the same Telegram StringSession in the VM and cloud
at once, including a supposedly isolated pilot. Hiding delivery channels marks
them resolved and is not a dry-run switch.

After draining, create a consistent SQLite online-backup snapshot and transfer
the database, live X cookie file and archives through the explicit state
bootstrap procedure. Protect the staging directory (mode 0700, files 0600).
Verify checksum/integrity, table counts, source cursors, latest digest IDs,
delivery flags and archive lineage before the cloud manifest becomes canonical.
Preserve the latest `x-cookies.json.live`: an original Key Vault cookie seed
must not overwrite a rotated cookie from the transferred bundle. Do not set
`X_COOKIES` alongside `X_COOKIES_PATH` in the Azure runtime.

For an operator workstation, use a writable temporary data directory and explicit
Azure CLI authentication; grant the operator Blob Data Contributor on this
container through the established administrative process. Do not grant broader
vault access to run an export. The application reads all cloud configuration
through `CloudConfig`:

```sh
export DIGEST_CLOUD_ACCOUNT_URL=https://YOUR_ACCOUNT.blob.core.windows.net
export DIGEST_CLOUD_CONTAINER=digest-state
export DIGEST_CLOUD_NAMESPACE=production
export DIGEST_CLOUD_DATA_DIR=/tmp/digest-cloud-operator
uv run python -m digest.cloud_state --operator-login bootstrap /PRIVATE/FINAL_STATE_DIR
uv run python -m digest.cloud_state --operator-login export /PRIVATE/VERIFY_EXPORT_DIR
```

Source contains `state.db`, `archive/`, and the existing `x-cookies.json` seed
and/or rotated `x-cookies.json.live`. Preserve their names and modification
times: the collector chooses the current jar using this ordering. Bootstrap
refuses a namespace with an existing manifest.
Exports require an empty private destination and contain `bundle.tar.gz` plus
`manifest.json`; preserve them together. Verify hashes and SQLite integrity and
row/cursor/delivery parity without printing credential or message contents.


Run manual probes with test inputs/destinations before activating production.
The ordinary cloud runner retains its schedule-slot checks when started
manually: use a valid current slot or an explicit reviewed catch-up slot. Run
job commands through Azure job execution overrides on the migration image;
`cloud_run` uses managed identity and is not an operator-login workstation CLI.
The runtime command is `python -m digest.cloud_run JOB`, where `JOB` is one of
`daytime`, `overnight`, `evening`, `daily`, `weekly`, `positions`, `patreon`,
`relay`, `backup`. Each production execution acquires the same renewable state
lease, validates the current bundle, checkpoints progress and fails closed on
absent/corrupt state or lost ownership. Test subscription authentication,
collectors, publication and cloud-only export/restore into a separate namespace.

After state handoff succeeds, set `schedules_enabled=true` through a reviewed
plan/apply. **Manual-to-Scheduled changes replace job resources in AzureRM**;
the protected storage account/container remain. Do not change trigger types
while executions are running. All cron is UTC. Daily/weekly have two UTC
candidates and validate Budapest local time before storage/model work, with a
ten-minute startup grace. Wrong-DST candidates are inexpensive no-ops.

| Job | UTC schedule | Delivery behavior |
|---|---|---|
| daytime | `0 6,12 * * *` | Window; ordinary channels |
| overnight | `0 0 * * *` | Window; hide Telegram |
| evening | `0 18 * * *` | Window; hide Telegram and site |
| daily | `30 18,19 * * *` | 20:30 Europe/Budapest |
| weekly | `45 19,20 * * 0` | Sunday 21:45 Europe/Budapest |
| positions | `25 1,5,9,13,17,21 * * *` | Existing tracked-project lane |
| patreon | `50 * * * *` | Existing per-post lane |
| relay | `40 * * * *` | Verbatim forwarding, deterministic MTProto IDs |
| backup | `15 4 * * *` | Snapshot/retention |

After a successful handoff, deploy all six homelab enable flags false (master,
daily, weekly, positions, Patreon and relay). The master alone does not stop
independent modes. Observe seven days including a weekly digest, validate every
mode, auth, cookie rotation, output quality, alerts and cloud restore, then
retire digest-specific homelab definitions. Physical deletion of source state
and credentials is a separate reviewed cleanup. The shared VM remains for its
other applications. Recovery takes place within Azure.

## Interrupted runs and recovery

Platform retries are disabled. A job waits at most ten minutes for the shared
lease; lease contention/failure must appear in logs and be investigated.
Subsequent cursor-based runs can collect missed input, but a missed daily or
weekly synthesis requires an explicit catch-up slot. For example, after
reviewing the missed summer daily slot:

```sh
python -m digest.cloud_run daily --catch-up-slot 2026-10-08T18:30:00+00:00
```

The slot must match the schedule, be in the past and not already completed.
Run with the same configured Azure identity/namespace and image; do not launch
an unguarded `python -m digest` process against production cloud state.

Telegram sending and Blob checkpoints cannot commit atomically. An uncertain
send blocks automatic resend for that channel. Inspect the destination and
decide whether the digest was received. Record the confirmed outcome under the
lease, or reset to pending only after establishing that no message was sent:

```sh
uv run python -m digest.cloud_reconcile DIGEST_ID telegram confirmed --operator-login
uv run python -m digest.cloud_reconcile DIGEST_ID telegram pending --operator-login
```

Use one decision, not both. Reconciliation updates state without publishing.
Then use the guarded runner to finish pending safe channels. Site publication
reuses the digest ID; relay retries preserve deterministic MTProto `random_id`.
Never blindly resend an unknown Telegram outcome.

For corrupt state or a bad cloud release, disable/drain cloud jobs, preserve
the failed generation for diagnosis, restore a validated compatible cloud
backup and cookie/archive bundle, verify lineage and resume the cloud jobs.
Restore into a fresh namespace, then point the reviewed Azure configuration at
it; never overwrite the damaged canonical manifest without retaining evidence.
For a rehearsal, leave production unchanged and compare a second export:

```sh
# Export current state, or choose one retained daily backup explicitly.
uv run python -m digest.cloud_state --operator-login export /PRIVATE/RESTORE_SOURCE --backup-date YYYY-MM-DD
export DIGEST_CLOUD_NAMESPACE=restore-test
uv run python -m digest.cloud_state --operator-login bootstrap /PRIVATE/RESTORE_SOURCE
uv run python -m digest.cloud_state --operator-login export /PRIVATE/RESTORE_VERIFY
```

Verify bundle/database/cookie hashes and row counts. For actual recovery, choose
a new recovery namespace instead of `restore-test`, update Terraform `namespace`
while jobs are drained, review/apply and resume only after lineage verification.
Re-pin a compatible prior cloud image if required; preserve uncertain-delivery
records across recovery. Restoring an older backup can replay confirmed sends
that happened after that snapshot: reconcile destination history and affected
cursors/delivery intent before resuming publication.

Do not create an empty database to make startup succeed. Test export/restore
into a separate namespace before calling backup complete. Seven daily backups
and 365 days of archives are distinct from 90-day item pruning. Checkpoint
garbage collection must protect the current manifest and retained backups;
ordinary age-only Blob lifecycle deletion is unsafe for referenced objects.

## Alerts, cost and residency

Two aggregate log rules evaluate every 15 minutes: failures/uncertain delivery
and missing successful runs. They have no dimension splitting. Freshness uses
20 hours for daytime, 26 hours for daily/overnight/evening/backup, six hours for
positions, two hours for Patreon/relay; weekly is checked Sunday after 23:00
Budapest through Monday before 22:00 while its expected result is inside Azure's
two-day log-alert lookback. Pilot validation must confirm actual table columns,
system error reasons and notification routing. Alerts stay disabled with
schedules. The Log Analytics quota can drop logs if exceeded; monitor quota
and tune before accepting the alerting evidence.

The pre-migration estimate was approximately $1–2/month including alerts with
available Container Apps grants; reserve $5 for a pilot. Durability checkpoints
upload the approximately 50 MB database repeatedly, and **their real storage
and execution cost has not been measured**. Blob soft deletion retains removed
objects for 14 extra days; reference-aware pruning and seven daily backups bound
ordinary live history. Recalculate transfers, writes, retained bytes, boot time
and logs after the pilot. The monthly budget defaults to 5 **billing-currency
units**, sends alerts at 80/100%, and does not cap spending. Subscription fees
remain separate. Dedicated-vault and backend-storage costs belong in actual
spending reconciliation.

Runtime blobs, Terraform backend blobs, the dedicated vault and 30-day logs
stay in West Europe. The source homelab vault remains in Germany West Central;
copying digest credentials moves their Azure storage within the EU. This does
not establish GDPR compliance or change external providers' processing terms.
Azure managed identity, Container Apps definitions and Blob SDK/leases create
Azure coupling; SQLite and release containers remain
portable. Telegram, X, Claude and the existing Cloudflare site retain their
external processing/residency characteristics. Avoid publishing message bodies,
session/cookie contents, credential-bearing URLs or model inputs in logs.

Reference contracts: [Container Apps Jobs](https://learn.microsoft.com/en-us/azure/container-apps/jobs),
[AzureRM job resource](https://registry.terraform.io/providers/hashicorp/azurerm/5.4.0/docs/resources/container_app_job),
[Blob leases](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-lease),
[Key Vault job secrets](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets),
[Azure budgets](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-acm-create-budgets),
[Azure Terraform backend](https://developer.hashicorp.com/terraform/language/backend/azurerm),
[Blob versioning](https://learn.microsoft.com/en-us/azure/storage/blobs/versioning-overview).
