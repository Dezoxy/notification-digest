# Azure digest migration and operations

Status on 2026-10-09: the cutover happened on 2026-10-08. The digest's VM
timers were drained and stopped, the state was handed to Azure and the Azure
schedules were enabled, so production runs here and no longer on `01-myapps-vm`.
This runbook keeps the cutover procedure as the historical record and the
template for any rebuild, and it owns the operating and recovery procedures.
Still open: the seven-day observation, an exercised restore from the backup
account, and measured cost. Returning execution to the VM is outside the plan.

Production runs the existing release image as nine finite Container Apps Jobs
in West Europe, with 0.5 vCPU, 1 GiB, no platform retries and a 50-minute
platform timeout. The runner allows at most ten minutes waiting
for the lease and 35 minutes of active work, then fences the process group.
It preserves `CLAUDE_CODE_OAUTH_TOKEN` subscription authentication, models,
effort, prompts, translations and verification; fallbacks move from OpenRouter
to the Claude API over workload identity federation (no key; see
[Claude API fallback](#claude-api-fallback-workload-identity-federation)). The owner
requested the current Claude CLI, so the migration image now pins **2.1.294**
(the official npm latest and Anthropic release verified on 2026-10-08). That
release requires Node >=22: the Dockerfile copies Node/npm from the official
Node 22 Bookworm image pinned by tag and digest, with only its runtime libraries.
The measured VM baseline remains **2.1.284**; its production image and the
separate homelab global CLI pin have not been updated by this preparation.
The temporary Renovate hold is removed; its existing custom manager can again
propose ordinary CLI updates. A build/version/help smoke without credentials
checks installation, not authentication or editorial equivalence. Azure pilot
checks must still prove subscription access, summarization, HU translation,
verification, fallbacks and output quality on 2.1.294 before activation.
Local ARM64 image build passed with Node 22.23.3 and npm 10.9.9. A fresh CI
build for the deployment platform remains required; no live model calls have
been run for this update. Remeasure runtime/memory, state transfers and total
cost on the new CLI during the Azure pilot: old VM sizing is a baseline, not
proof that the new image has identical resource use.

Status on 2026-10-09: the schedules were activated on 2026-10-08, so the
paragraph above is the record of what was planned, not a pending gate. The CLI
and Node pins have since moved on through Renovate; the `Dockerfile` is the
source. Recorded since activation: scheduled digests summarized through the
subscription CLI on the Azure jobs, and the Claude API fallback smoke test
passing on the released image. Not recorded here: Hungarian translation, the
daily verification pass, an output-quality comparison with the VM, and measured
runtime, memory or cost.
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
| Separate backup account/resource group | Daily independent runtime bundle/manifest copies | App Terraform creates the resources; only the backup job has the backup-writer identity; ordinary runner/pruner cannot access it |
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
identity/federated credentials for this app. GitHub issues immutable,
ID-based subjects for this repository:
`repo:Dezoxy@OWNER_ID/notification-digest@REPO_ID:environment:azure-plan` and
the same prefix with `:environment:azure-production`, audience
`api://AzureADTokenExchange`. Read the exact prefix from `sub_claim_prefix` in
`gh api repos/Dezoxy/notification-digest/actions/oidc/customization/sub`; the
name-only form `repo:Dezoxy/notification-digest:...` does not match and fails
`terraform init` with `AADSTS700213`. Restrict both environments to `main`. GitHub
offered no required reviewers on this personal repository while it was private,
and the workflow does not rely on them, so approval is a two-step dispatch:
review the saved plan, then dispatch the apply with that plan run's ID. The
apply job fails closed unless that run is a successful `azure-deploy` plan
from `main` at the same commit. The one exception is a merged image-pin
bump, which `release-apply` deploys without a dispatch once its guard
accepts the plan (see [Image upgrades](#image-upgrades)); its federated
credential is the same `azure-production` environment. These repository/Entra
settings are prerequisites, not settings that the workflow YAML can enforce by
itself.

The deployment identity needs Contributor on the app resource group and
permission to grant the runner's container Blob and dedicated-vault Secrets
User roles. Use a constrained Role Based Access Control Administrator assignment
for those grants. The condition should restrict the assignable roles to Storage
Blob Data Contributor and Key Vault Secrets User, and must not add a
principal-type condition: the stack's role assignments do not send
`principalType`, so such a condition rejects them with `AuthorizationFailed`.
Do not give the deployment identity secret-value access to the homelab vault.
The separate secret-copy operator receives only the temporary source-read and
target-write roles needed for the reviewed migration. Bootstrap resource-group
creation and resource-provider registration separately if the deployment
identity is scoped to an existing group. The separate
`notification-digest-backups-westeurope` group also needs Contributor and a
constrained role-assignment grant for its backup-writer identity/container.
Import a precreated backup group as `azurerm_resource_group.backup` into the same
Azure backend before planning. Register `Microsoft.App`,
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
to secret-name map `secret_names`, but **not** `image`, which is pinned in the
tracked [image.auto.tfvars.json](../infra/azure/image.auto.tfvars.json) (see
[Image upgrades](#image-upgrades)). Terraform constructs versionless references
to the new vault. Secret values never enter Terraform inputs or managed secret
resources. Do not configure `TF_API_TOKEN` for this Azure stack.

Start from [production.auto.tfvars.example](../infra/azure/production.auto.tfvars.example).
The app configuration was rendered offline from the declared homelab defaults
and host overrides on 2026-10-08. Recheck the baseline against final live
configuration before cutover: collector flags, feeds/allowlists, topics,
model/effort/budgets, translation, verification, context, delivery channels and
fallback omission semantics. The fallback chain exists only when the five
`ANTHROPIC_*` federation identifiers are set; with them set, leaving
`FALLBACK_MODELS` unset retains the app's defaults and an empty value disables
that tier. Azure env values are literal: keep
`$FET,$ASI` as single dollars, unlike homelab Compose env-file escaping.

Choose globally unique names for the runtime storage account, **different**
backup storage account (`backup_storage_account_name`) and digest vault,
plus the alert email; update the budget start to the first day of the deployment
month. Pin a tested migration release image in `image.auto.tfvars.json` before
manually executing any job. The variable default `0.28.0` records the source
baseline and does **not** contain the cloud runner. Activation also requires
`migration_release_verified=true`. The stack rejects floating tags.

The new Standard Key Vault uses RBAC, purge protection and **seven-day soft
deletion**. Decide this before the foundation apply: retention is fixed at vault
creation and enabled purge protection cannot be disabled. A deleted secret/vault
name stays reserved during retention; recover it rather than attempting a purge.
The shorter retention suits these re-issuable credentials while preventing
immediate purge.
The runner has Secrets User access to the dedicated vault and Blob access to its
runtime container; it cannot read backend state, the source vault or the backup
account. Only the backup job receives the additional backup-writer identity. Private
GHCR pulls use a read-packages PAT via a Key Vault-backed registry password
reference; Azure managed identity cannot directly authenticate to GHCR. OAuth,
session strings, cookies and publication credentials never enter Terraform
inputs. AzureRM may store provider-computed Storage/Log Analytics keys in state:
protect backend state and short-lived plan artifacts; do not share binary
plans. Shared Key storage authentication is disabled.

### Public repository

The repository has been public since 2026-10-09. Consequences, each checked
against `.github/workflows/` and `infra/azure/`:

- Workflow logs are readable by anyone, and so are the `azure-plan-*` artifacts
  while they exist (`deployment.tfplan`, `deployment-plan.txt`,
  `production.auto.tfvars.json`, `backend.hcl`). They can include resource
  names, Azure IDs and the nonsecret Terraform inputs.
- The plain job settings in `app_env` are not secrets but several are personal
  (Telegram chat and thread ids, mail addresses). `infra/azure/main.tf` marks
  their values `sensitive()`, so plan and apply logs print `(sensitive value)`
  while the variable names stay readable for review. This matters because job
  variables are an ordered list: inserting one shifts every later entry, and
  Terraform then prints all of them as changed. The first plan after the
  marking shows every job as updated in place with "The value is unchanged".
  The marking is partial. It does not cover the `azure-plan-*` artifact, whose
  `production.auto.tfvars.json` and binary `deployment.tfplan` hold the same
  values in clear for as long as the artifact exists (see below); it does not
  cover `alert_email`, a separate input that still prints when the alert or
  budget resources change; and it does not reach logs written before the
  marking, so delete those runs' logs if they matter. Closing the artifact gap
  means encrypting it with an environment secret before upload.
- Secrets are Key Vault references (`key_vault_secret_id` in
  `infra/azure/main.tf`) and never Terraform inputs, so no Key Vault secret value
  appears in the inputs, the plan text or the logs. The binary `deployment.tfplan`
  also embeds the prior state, so treat it as sensitive even though it is
  downloadable. The storage accounts have shared-key access disabled, so their
  keys are unusable. The Log Analytics workspace shared key (ingestion only) is
  the one provider-computed credential that may appear in a plan; it cannot be
  disabled because the Container Apps environment uses it.
- The exposure window is cut to roughly the length of a run. The `release-apply`
  job of an image-pin run deletes that run's `azure-plan-<run_id>` artifact in a
  final `always()` step, so it is removed after success, failure, a refused
  guard, a noop or a cancellation. The artifact of a manual plan lives until
  that plan is applied (the `apply` job deletes it when it ends, whatever the
  outcome) or at most one day (`retention-days: 1`). Deletion is best-effort: a
  failure raises a `::warning::` and the artifact still expires after one day.
  To delete a manual plan's artifact early, find its id and delete it:

  ```sh
  gh api repos/Dezoxy/notification-digest/actions/runs/PLAN_RUN_ID/artifacts \
    --jq '.artifacts[] | {id, name}'
  gh api -X DELETE repos/Dezoxy/notification-digest/actions/artifacts/ARTIFACT_ID
  ```

- Runs triggered by fork pull requests get no secrets and no OIDC token (GitHub
  withholds both), and the repository requires maintainer approval for every
  external contributor. No workflow that runs on `pull_request` references an
  environment, a secret or a repository variable, and `azure-deploy` runs only
  from `main`.
- Both deploy environments, `azure-plan` and `azure-production`, are restricted
  to `main` by deployment branch policy.
- The container image `ghcr.io/dezoxy/notification-digest` stays private; Azure
  pulls it with the Key Vault-backed read-packages token.

## Copy digest secrets to the dedicated vault

The reviewed [secret manifest](../infra/azure/secret-migration.json) names the
**17 runtime/registry secrets**. The unused `digest-x-cookies` seed is excluded:
cloud cookie state comes from the transferred live bundle and the guarded
rotation command below, rather than a second vault copy.
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
Run preview again after any source rotation. Perform the full copy and verify
**immediately before** the Manual-jobs apply, freezing credential edits throughout
that copy and apply. Verify again immediately before the first production start;
if credentials changed, resolve and verify each conflict before proceeding.

### Temporary operator access

Use an administrator authorized to assign these roles. The copy operator is
separate from the GitHub deployment service principal. The following creates
only missing direct, vault-scoped assignments for the signed-in human operator,
and records IDs **only for assignments this procedure created**. Existing direct
assignments are retained; inspect inherited/group access separately. Run in bash,
selecting the intended subscription explicitly:

```bash
set -euo pipefail
export DIGEST_SOURCE_VAULT=kv-homelab-prod-th
export DIGEST_TARGET_VAULT=YOUR_DIGEST_VAULT
export DIGEST_COPY_OPERATOR_ID=YOUR_OPERATOR_ENTRA_OBJECT_ID
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
umask 077
DIGEST_OPERATOR_DIR=$(mktemp -d "${TMPDIR:-/tmp}/digest-operator.XXXXXXXX")
chmod 700 "$DIGEST_OPERATOR_DIR"
DIGEST_SOURCE_SCOPE=$(az keyvault show --name "$DIGEST_SOURCE_VAULT" --query id -o tsv)
DIGEST_TARGET_SCOPE=$(az keyvault show --name "$DIGEST_TARGET_VAULT" --query id -o tsv)
grant_copy_role() {
  local scope="$1" role="$2" marker="$3" existing
  existing=$(az role assignment list --scope "$scope" \
    --query "[?principalId=='$DIGEST_COPY_OPERATOR_ID' && roleDefinitionName=='$role' && scope=='$scope'].id" \
    --output tsv)
  if [ -z "$existing" ]; then
    az role assignment create --assignee-object-id "$DIGEST_COPY_OPERATOR_ID" \
      --assignee-principal-type User --role "$role" --scope "$scope" \
      --query id --output tsv > "$DIGEST_OPERATOR_DIR/$marker"
  fi
}
grant_copy_role "$DIGEST_SOURCE_SCOPE" 'Key Vault Secrets User' source-role.id
grant_copy_role "$DIGEST_TARGET_SCOPE" 'Key Vault Secrets Officer' target-role.id
```

Have the copy operator authenticate as that exact user, then wait for data-plane
RBAC propagation. Do not increase permissions to bypass a propagation failure.
The two roles allow source list/get and target list/get/set, respectively; the
Secrets Officer role also permits deletion, so keep it temporary and use only
the reviewed copy command. These commands do not grant access to the GitHub
runner. After the final full-manifest verification and any authorized conflict
resolution, switch back to the role-assignment administrator and revoke only
the assignments recorded above:

```bash
for marker in source-role.id target-role.id; do
  if [ -s "$DIGEST_OPERATOR_DIR/$marker" ]; then
    IFS= read -r DIGEST_CREATED_ROLE_ID < "$DIGEST_OPERATOR_DIR/$marker"
    az role assignment delete --ids "$DIGEST_CREATED_ROLE_ID"
  fi
done
```

Confirm those assignment IDs are absent, then switch back to the copy operator's
login. Allow
RBAC/token caches to expire and repeat a metadata-filtered read of
`digest-tg-session` in both vaults (`az keyvault secret show --vault-name VAULT
--name digest-tg-session --query id --output tsv`). Expect authorization failure
if the operator has no other secret-read grant; never print `value`. If access
still succeeds, identify inherited, group or pre-existing grants and record the
residual access instead of claiming revocation proved isolation. Do not remove
unrelated grants. Remove the private marker directory after documenting the
assignment IDs and verification outcome.

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
those explicit names. After the 17 copies pass verification, review a second
plan/apply with `jobs_enabled=true` and `schedules_enabled=false` to create the
nine Manual jobs. No source secret removal is part of this procedure.

### Resolve a source rotation before cutover

A differing target is a stop condition by default. Determine which source
version is intended, freeze source/target edits and explicitly authorize replacing
that one target. Do not delete the secret: purge protection would reserve its
name. The tool creates a new version of the exact manifest target in memory,
preserves metadata and verifies it; it cannot replace an unrelated target.
For an authorized Telegram source rotation, for example:

```sh
uv run python scripts/migrate_azure_secrets.py \
  --target-vault YOUR_DIGEST_VAULT --subscription "$AZURE_SUBSCRIPTION_ID" \
  --apply --replace-target digest-tg-session
uv run python scripts/migrate_azure_secrets.py \
  --target-vault YOUR_DIGEST_VAULT --subscription "$AZURE_SUBSCRIPTION_ID" --verify
```

The command is a credential write, not a preview. `--replace-target` requires
`--apply`, selects exactly one configured target, and refuses a missing target.
Record the source/target version IDs and verification result without values.
Other conflicts remain blocked until reviewed individually. Copying an older
source over a deliberately rotated target is not a valid conflict resolution.
After cutover, update the dedicated vault directly using the credential procedure
below; the homelab vault is no longer the source of truth for digest credentials.

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
state and performs no plan/apply. Review and merge application changes; a merge
that touches shipped files is released as the next patch version by
`auto-release.yml`, and an explicit `vX.Y.Z` tag can still be pushed by hand.
Set the tracked image pin to its `X.Y.Z` GHCR tag or digest in a reviewed PR
(Renovate normally opens it; see [Image upgrades](#image-upgrades)). Run
`azure-deploy` with `operation=plan`. Inspect resource ownership, RBAC, cost,
image/configuration, state retention and the dedicated vault. The foundation
plan must contain
**zero jobs** with `jobs_enabled=false`. After secret copy/verification, the
second plan with `jobs_enabled=true` must contain nine **Manual** jobs and only
dedicated-vault references. Jobs must not execute until state bootstrap is
completed and verified.

For each provisioning stage, dispatch `operation=plan`, review the plan
artifact and output, then dispatch `operation=apply` with `plan_run_id` set to
that plan run:

```sh
gh workflow run azure-deploy.yml --ref main -f operation=plan
gh workflow run azure-deploy.yml --ref main -f operation=apply -f plan_run_id=<plan run id>
```

Apply refuses a run that is not a successful `azure-deploy` plan from `main` at
the same commit, and uses that exact saved plan. Artifacts expire after one day,
so an old plan cannot be applied. Only a plan dispatched manually can be applied
this way; the plan of an image-pin push run is consumed by its own guarded
`release-apply` job. Azure tables/KQL, image pull, CLI authentication,
cloud egress and measured resource use remain live pilot checks; validation
does not prove them. Check a subsequent no-op plan for provider drift around
Key Vault-backed job secrets before activating schedules.

## Image upgrades

The whole chain is automatic once jobs exist:

1. A merge to `main` that changes shipped files (`Dockerfile`, `digest/`,
   `prompts/`, `pyproject.toml`, `uv.lock`) starts `auto-release.yml`. It runs
   `ruff` and `pytest`, then tags the next patch version after the highest
   `vX.Y.Z` tag, creates the GitHub release and calls `release.yml` to build the
   image. (A tag created with the built-in token does not trigger the tag-push
   workflow, hence the direct call.) Docs-only, workflow-only and infra-only
   merges release nothing, and the pin file is deliberately outside the path
   filter, so a pin bump can never cut a release. Minor and major versions stay
   manual: push `vX.Y.0` and later releases continue from it.
   If the build fails after the tag exists, re-run the failed job: the tag
   stays, no image means no pin PR, and the next release continues from it.
2. Renovate opens a PR that bumps the pin and merges it once CI passes. Renovate
   keeps one PR open at a time, so the pin PR waits behind any other open
   Renovate PR: merge or close that one first.
3. The merge deploys the image through the guarded apply below.

The image is pinned in one place, the tracked
[infra/azure/image.auto.tfvars.json](../infra/azure/image.auto.tfvars.json), which
Terraform loads automatically. The pin merge is the deploy: a push to `main` that
touches the pin starts `azure-deploy`, whose `plan` job plans as usual and whose
`release-apply` job (environment `azure-production`) applies that same saved plan
only if `scripts/azure_release_guard.py` accepts it.
`release-apply` holds the workflow's concurrency group while it waits, so other
`azure-deploy` runs queue behind it for up to about an hour.

**One-time step.** Remove the `image` key from `AZURE_TERRAFORM_VARS_JSON`. The
workflow writes that variable to `production.auto.tfvars.json`, which Terraform
loads after the pin and would silently override it. Every plan therefore fails
if the variable still sets an image different from the pin, and ignores one equal
to the pin with a warning. Do this before the first pin PR merges.

**What the guard accepts.** An empty plan finishes successfully without applying
(for example while `jobs_enabled=false`). Otherwise all of these must hold:

- every change is an in-place `update` of an `azurerm_container_app_job`; any
  create, delete or replace, and any other resource, is refused;
- every job in the plan is updated: all nine move together, because they share
  one SQLite state behind a lease and a partial bump could let an old image open
  a database that a newer one migrated;
- the only difference between each job's `before` and `after` is the container
  image, with nothing unknown until apply (a short allowlist covers the
  provider-computed `event_stream_endpoint` and `outbound_ip_addresses`);
- the new image is identical across jobs, equals the tracked pin and is an exact
  release tag or digest (the `image` variable's own pattern).

`schedules_enabled`, `jobs_enabled`, environment values, crons and secrets
never change through this path, so schedule activation stays explicit.

**Then it waits and applies.** `release-apply` waits up to 20 minutes for the
pinned image to exist in GHCR (the pin can merge before `release.yml` finishes),
signs in to Azure, and waits up to 45 minutes until no job has a Running or
Processing execution and no job's cron fires within the next five minutes (UTC).
An execution whose status cannot be read counts as busy. Only then does it run
`terraform apply` on the saved plan.

**On refusal** (or a timeout) the job fails and nothing is applied. Read the
verdict in the log, then use the manual path above: `operation=plan`, review,
`operation=apply`. A manual plan after a refused bump includes the image change
together with whatever the guard objected to. The first bump after cutover should be
compared with the guard by hand: Terraform may mark more provider-computed
attributes unknown on an update than the allowlist expects, in which case the
guard refuses safely and the allowlist needs extending.

**There is no human gate** between merging shipped code and production: the
checks are `ruff`, `pytest`, the repository's CI on the pin PR and the guard. To
stop a release, do not merge the code; to stop one that is tagged, close the
Renovate pin PR (or revert the pin on `main`, which deploys through the guard
again). Remember that a reverted pin only helps while the state schema is
unchanged, as below.

**Rollback and schema.** A pin is rolled back by a revert PR, which goes through
the same guard. Schema migrations are one-way, so that is only safe while the
newer image has not migrated the state. Once it has, recovery is a restore from the
daily backup (see [Interrupted runs and recovery](#interrupted-runs-and-recovery)),
not an image rollback.

## Claude API fallback (workload identity federation)

The summarizer's primary call is `claude -p` on the subscription. When it fails
(safeguards refusal, usage limit, timeout, empty output) the same prompt is
retried against the Claude API, billed to prepaid API credits, in the order of
`FALLBACK_MODELS` (editorial tier: `claude-opus-5-5,claude-sonnet-5-5`) or
`FALLBACK_LIGHT_MODELS` (translation and arc context:
`claude-sonnet-5-5,claude-haiku-5-5`). There is **no API key**. The runner's
user-assigned managed identity (`notification-digest-runner`) trades its own
token with Entra for a short-lived token of a dedicated app registration
(`api://<APP_ID>`), exchanges that at `https://api.anthropic.com/v1/oauth/token`
(RFC 7523 jwt-bearer, Anthropic Workload Identity Federation) for a short-lived
access token and calls `/v1/messages` with it, once per fallback call. The
decision and its alternatives are
[ADR 9](architecture/decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md).

**Why two hops inside Entra.** Asking the managed identity for the audience
directly looks simpler and is what Anthropic's Azure guide shows, but it cannot
work here. That token lives 86,700 s between `iat` and `exp` (measured), and
Anthropic rejects an assertion longer than the issuer's maximum JWT lifetime
with the deny reason `jwt_lifetime_too_long`. The Claude Console accepts at
most 86,400 s for that limit, and the Admin API, which accepts up to 176,400 s,
is not available to individual organizations (every call returns 404). So the
managed identity's token is presented to Entra as a client assertion, through a
federated identity credential on the app registration, and Entra returns an
ordinary app token that lives 3,900 s. Anthropic therefore sees the app's
service principal, and the managed identity is trusted only by Entra.

The chain is off until all five required variables are set. Everything below is
identifiers, not secrets: nothing Anthropic-related is stored in the vault, and
the `digest-openrouter-api-key` secret is no longer used. An expired or
misconfigured federation fails **only at fallback time, never at startup**, so
the smoke test in step 5 is part of the setup, not an optional extra.

### Setup

1. **Entra app and federated credential.** Register one app that is both the
   token audience and the identity Anthropic will see, then let the runner's
   managed identity act as it:

   ```sh
   APP_ID=$(az ad app create --display-name claude-api-federation \
     --sign-in-audience AzureADMyOrg --query appId -o tsv)
   az ad sp create --id "$APP_ID"
   az rest --method PATCH \
     --uri "https://graph.microsoft.com/v1.0/applications(appId='$APP_ID')" \
     --headers "Content-Type=application/json" \
     --body "{\"identifierUris\":[\"api://$APP_ID\"],\"api\":{\"requestedAccessTokenVersion\":2}}"
   az ad app show --id "$APP_ID" \
     --query "{uris:identifierUris, tokenVersion:api.requestedAccessTokenVersion}" -o json

   RUNNER_OID=$(az identity show --name notification-digest-runner \
     --resource-group <APP_RESOURCE_GROUP> --query principalId -o tsv)
   az ad app federated-credential create --id "$APP_ID" --parameters "{
     \"name\": \"digest-runner-managed-identity\",
     \"issuer\": \"https://login.microsoftonline.com/<TENANT_ID>/v2.0\",
     \"subject\": \"$RUNNER_OID\",
     \"audiences\": [\"api://AzureADTokenExchange\"]}"

   az ad sp show --id "$APP_ID" --query id -o tsv   # the object ID the rule matches
   ```

   The Graph call sets the identifier URI and `requestedAccessTokenVersion=2`
   in one step, which makes the tokens v2.0 as the rule below assumes.
   Anthropic's guide uses `az ad app update --set
   api.requestedAccessTokenVersion=2` instead, but that fails on a freshly
   created app (`Couldn't find 'api' in ''`) because the `api` property does not
   exist yet, and it leaves the URI and version unset; the `show` is there to
   catch exactly that. The federated credential names exactly one subject, the
   runner identity's object ID, so no other identity in the tenant can act as
   the app. Select the tenant and subscription explicitly.

2. **Claude Console** (Settings, Workload identity, Connect workload, Microsoft
   Entra ID):
   - Issuer `https://login.microsoftonline.com/<TENANT_ID>/v2.0` (the v2.0
     selector, discovery mode). Leave its maximum JWT lifetime at the wizard's
     7500 s: the app token lives 3,900 s.
   - A service account placed in a **dedicated workspace with a monthly spend
     limit**. That limit is the only cap on the cost if every call falls
     through; nothing in the code caps spend.
   - A federation rule on that service account matching the **app's service
     principal**, not the managed identity: subject pattern and claim `oid` =
     the object ID printed by the last command of step 1, expected audience =
     `<APP_ID>` (the bare GUID, not the `api://` form). The wizard's "Object
     (principal) ID" field fills both the subject and `oid`. Never use a
     wildcard or partial subject. Keep the rule's own **Token lifetime** at 10
     minutes: it bounds the Anthropic token that comes back, and the app uses a
     fresh one per call.
   - Note the rule ID (`fdrl_...`), organization ID (a UUID), service account ID
     (`svac_...`) and, optionally, the workspace ID (`wrkspc_...`).

3. **Variables.** Add the non-secret values to `app_env` in the
   `AZURE_TERRAFORM_VARS_JSON` variable of the `azure-plan` GitHub environment
   (the shape is in
   [production.auto.tfvars.example](../infra/azure/production.auto.tfvars.example)):
   `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID`,
   `ANTHROPIC_SERVICE_ACCOUNT_ID`, `ANTHROPIC_FEDERATION_AUDIENCE`
   (`api://<APP_ID>`), `ANTHROPIC_FEDERATION_TENANT_ID` (the Entra tenant), and
   optionally `ANTHROPIC_WORKSPACE_ID`. Set all five required ones or none: a
   partial set stops every job at startup with a `ConfigError` naming the
   missing variables. The managed identity's client id
   is the existing `DIGEST_CLOUD_IDENTITY_CLIENT_ID` that Terraform already
   injects; do not add it. This is a non-image change, so the release guard
   refuses it by design: dispatch `azure-deploy` with `operation=plan`, review
   the plan (only job environment changes), then `operation=apply` with that
   `plan_run_id`, as in [Validate, release and provision disabled
   jobs](#validate-release-and-provision-disabled-jobs).

4. **Remove the OpenRouter key.** Delete `OPENROUTER_API_KEY` from
   `secret_names` in the same variable (the application no longer reads it).
   After that apply, the `digest-openrouter-api-key` secret may be deleted from
   the digest Key Vault; the operator needs the temporary scoped grant described
   under [Temporary operator access](#temporary-operator-access) and should
   revoke it afterwards. The migration manifest
   `infra/azure/secret-migration.json` no longer lists it either.

5. **Smoke test.** Before relying on the chain, start one execution of a job
   with its command overridden to `python -c` plus the program below. It builds
   the real configuration and makes one tiny call through the same code path a
   fallback uses, printing nothing but `OK` on success. It takes no lease, reads
   no state and sends nothing to Telegram or the site, so any job will do. An
   execution override replaces the container wholesale, so copy the job's own
   `env`, `image` and resources and change only the command:

   ```python
   from digest.anthropic_api import run_anthropic
   from digest.config import Config

   cfg = Config.from_env()
   assert cfg.anthropic_federation is not None, "federation variables unset"
   run_anthropic("Reply with one word.", "claude-haiku-5-5", 60, cfg.anthropic_federation)
   print("OK")
   ```

   ```sh
   JOB=digest-relay   # any of the nine; the command override means nothing else runs
   az containerapp job show -g <APP_RESOURCE_GROUP> -n "$JOB" -o json > job.json
   jq --rawfile prog smoke.py '.properties.template.containers[0] |
     {containers: [{name, image, command: ["python", "-c", $prog], env,
                    resources: {cpu: .resources.cpu, memory: .resources.memory}}]}' \
     job.json > body.json
   az rest --method post --body @body.json \
     --url "https://management.azure.com$(jq -r .id job.json)/start?api-version=2024-03-01"
   rm -f job.json body.json
   ```

   Save the program above as `smoke.py` first. `job.json` and `body.json` hold
   only secret references, never values, but delete them anyway. Read the result
   with `az containerapp job execution show` and the execution's console log.

   A traceback names a step and an HTTP status or exception type, never a body,
   token or URL. Every denial at the exchange is the same opaque 401; the real
   reason is recorded only in the Console's authentication history (Settings,
   Workload identity), which also shows the claims Anthropic read. The reasons
   met while setting this up:
   - `jwt_lifetime_too_long`: the assertion lives longer than the issuer's
     limit. With this design the app token is 3,900 s; this reason means the
     managed identity's own 24-hour token reached Anthropic instead.
   - `match_subject_prefix`: the rule's subject is not the app's service
     principal object ID (for example, it still names the managed identity).

   An `entra token request failed` error is raised before Anthropic is
   contacted: check the federated credential's subject and issuer in step 1.
   Repeat the test after any change to the rule, the issuer or the app.

### Operating it

- **Timeout budget.** Production sets `CLAUDE_TIMEOUT_SECONDS=600` and
  `FALLBACK_TIMEOUT_SECONDS=600`. The subscription CLI has its own timeout;
  after it fails, the API fallback chain shares a separate 600-second budget
  across two models, initially reserving roughly 300 seconds for each. Unused
  time carries forward. The application default remains 180 seconds when no
  override is configured. During the 2026-10-10 recovery, both API models
  exhausted their shares of the 180-second budget. With the 600-second override,
  the first API model completed summarization in approximately 191 seconds;
  translation, site publication, Telegram delivery and the completed-slot
  checkpoint then succeeded. The existing 35-minute cloud runtime watchdog
  remains the outer limit.
- **Security boundary.** The identifiers are not secrets. The `claude -p`
  subprocess environment withholds `IDENTITY_ENDPOINT`, `IDENTITY_HEADER` and
  every `ANTHROPIC_*` variable (`claude_subprocess_env`): the first two would let
  content-facing code mint tokens for the runner identity (Blob state, API
  credit), and an `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` reaching the CLI
  would take precedence over `CLAUDE_CODE_OAUTH_TOKEN` and silently move every
  digest from the flat subscription to metered API billing. Never set either of
  those variables in `app_env` or the job.
- **Cost.** A fallback call bills the chosen model's API price for one digest's
  tokens, with `output_config.effort` fixed at `high` and `max_tokens` 32000. The
  earlier OpenRouter estimate does not carry over and no replacement figure is
  claimed here: measure it from the `claude api usage: model=... input_tokens=...
  output_tokens=...` line logged by the first real fallback, then size the
  workspace's monthly limit from it. A reply ending in `max_tokens` or `refusal`
  counts as a failed leg and the chain moves on.
- **Provenance.** A digest served by a fallback records the Claude API model and
  `fallback: true`, shown on the site as the `↻` marker.
- **Rollback.** Remove the five variables through the same plan/apply to return
  to Claude CLI only; nothing else depends on them. Disabling the federation rule
  in the Console stops exchanges immediately.
- **Retired code.** `digest/openrouter.py` and `tests/test_openrouter.py` remain,
  unwired, and are deleted in a follow-up once the first real fallback has proven
  this leg.

## State handoff and activation

This procedure was carried out for the 2026-10-08 cutover. It stays as the
record of what was done and as the template for any later handoff or rebuild;
read its future tense accordingly.

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

Before any migration image or ordinary release initializes the VM database,
create and retain a consistent SQLite online-backup snapshot of the source
schema and record its `PRAGMA user_version`. The migration adds schema v8:
`0.28.0` must never open a database after that upgrade. This is a preservation
and diagnosis artifact, not a return-to-VM procedure. Keep the VM on its current
image until it is drained.

After draining, create a consistent SQLite online-backup snapshot and transfer
the database, live X cookie file and archives through the explicit state
bootstrap procedure. Protect the staging directory (mode 0700, files 0600).
Verify checksum/integrity, table counts, source cursors, latest digest IDs,
delivery flags and archive lineage before the cloud manifest becomes canonical.
Preserve the latest `x-cookies.json.live`: an original Key Vault cookie seed
must not overwrite a rotated cookie from the transferred bundle. Do not set
`X_COOKIES` alongside `X_COOKIES_PATH` in the Azure runtime.

Operator bootstrap/export/reconciliation/cookie commands create private temporary
working directories and remove them after SQLite connections close, watchdogs
stop and leases are released.
They do not touch the configured persistent `DIGEST_CLOUD_DATA_DIR`. Explicit
export destinations remain private durable output and must be removed by the
operator after verification. Use explicit Azure CLI authentication; grant the
operator Blob Data Contributor on this
container through the established administrative process. Do not grant broader
vault access to run an export. The application reads all cloud configuration
through `CloudConfig`:

```sh
export DIGEST_CLOUD_ACCOUNT_URL=https://YOUR_ACCOUNT.blob.core.windows.net
export DIGEST_CLOUD_CONTAINER=digest-state
export DIGEST_CLOUD_NAMESPACE=production
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

### Egress go/no-go gate

Record cloud execution IDs, image/configuration pins and status-only evidence
for **every enabled collector and publication lane**: Telegram/relay, X, RSS,
Reddit, Hacker News, Polymarket, Patreon and positions as configured. Verify
subscription Claude calls, HU translation, daily web verification, configured
Claude API fallback authentication (the federation smoke test, and a real
fallback if one has occurred), Telegram notifications, site ingest and
email only if enabled. A TCP connection or one successful digest is insufficient:
validate authenticated requests and actual parsing/publication, including paid
Patreon access. An empty healthy feed can pass with authenticated/status evidence;
an auth or blocked-egress failure cannot pass merely because other sources work.
Do not print session headers, cookie values or collected bodies in the evidence.

**Go:** representative manual cloud runs exercise every enabled path without
persistent 401/403, challenge pages, IP/region denial or rate-limit failure, output
quality matches the baseline, and alerts plus cloud restore are demonstrated.
Run session-bearing checks only after VM writers are drained, or use separate
test sessions and a separate pilot namespace. Never reuse the production
Telegram session concurrently. Production credentials need their own final
post-drain verification before schedule activation.

**No-go:** any enabled source loses access from Azure, subscription authentication
fails, publication is uncertain, or the representative tests are incomplete.
Keep schedules disabled and preserve state; do not resume execution on the VM.
Investigate and repeat controlled tests. NAT Gateway/static egress or a proxy is
a separate reviewed cost/security decision and cannot be assumed to fix a
provider's data-center block. Disabling a collector requires the owner's explicit
approval of the quality loss. Record actual outbound transfer, Blob operations,
retained bytes and execution minutes before accepting the monthly estimate.

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

## Credential rotation after cutover

After cutover the dedicated digest vault is authoritative for env credentials;
updating `kv-homelab-prod-th` or running homelab Ansible does not update Azure jobs.
Temporarily grant the operator Secrets Officer on the digest vault through the
scoped grant/revoke procedure above. Pause and drain affected jobs, review the
existing secret's metadata without its value, then capture the newly authorized
credential in a private 0600 file under a 0700 directory. Avoid `--value`, shell
history, command substitution and raw secret output.

For Telegram, run `uv run python scripts/telegram_login.py` interactively on the
operator's machine. The helper intentionally displays the new session for the
owner; do not capture its terminal output in logs. Paste the session with a hidden
prompt into the private file, then use `az keyvault secret set --vault-name
YOUR_DIGEST_VAULT --name digest-tg-session --file /PRIVATE/telegram-session.txt
--tags envvar=DIGEST_TG_SESSION folder=digest file-encoding=utf-8 --output none`.
Preserve any additional tags, non-null content type, expiry and not-before from
the metadata preflight through the corresponding CLI flags; those attributes
are per version and must not be silently reset. Verify latest version metadata,
wait for job secret-reference refresh, and prove the next Manual run authenticates
with the new version before reactivating schedules. If refresh fails, use the
reviewed Azure job replacement path while jobs are drained. Preserve delivery
and cursor state. Remove the temporary role, verify lost access and erase the
private input after successful validation. Reddit/Patreon/OAuth rotations follow
the same dedicated-vault procedure using their exact manifest names/tags.

### Rotate the cloud X cookie jar

X has no secret in the dedicated vault: its current jar is in leased runtime
state. Extract `auth_token` and `ct0` from a fresh dedicated browser session using
the README procedure. Save the JSON privately (0700 directory, 0600 regular file;
no symlink), pause/drain cloud jobs, configure the production Blob namespace and
a writable private directory for the verification export, then run:

```sh
uv run python -m digest.cloud_cookies /PRIVATE/cookies.json --operator-login
uv run python -m digest.cloud_state --operator-login export /PRIVATE/COOKIE_VERIFY
```

The command uses a temporary private working copy, Azure CLI authentication
and scoped runtime-container Blob access, acquires the same renewable lease and
watchdog, validates current state and
the JSON, updates both seed/live cookie files, and commits a guarded canonical
checkpoint without contacting X or changing database records. Inspect exported
file hashes/mtime and database parity without printing cookies; then test one
Manual X-enabled run and require successful collection before resuming schedules.
Delete the private JSON/export after verification. Lost ownership or an ambiguous
Blob commit fails closed: inspect/export the canonical generation before deciding
whether to repeat the operation. Never seed an empty cloud database or replace a
newer live jar with the historical homelab vault seed.

## Interrupted runs and recovery

Platform retries are disabled. A job waits at most ten minutes for the shared
lease. Cursor jobs (`daytime`, `overnight`, `evening`, `positions`, `patreon`,
`relay`) skip with a structured log and successful exit when startup is more
than ten minutes late or contention exhausts the wait; the next cursor run can
collect missed input. Investigate repeated skips and verify freshness alerts
still detect missing successful work. Daily, weekly and backup remain hard
failures for these conditions and require an explicit catch-up slot.

Lease acquisition, idempotent Blob reads and immutable bundle uploads allow
three total attempts with 0.5/1-second backoff. Upload retries use fresh names:
a lost acknowledgement can leave an orphan for guarded garbage collection.
Transient lease renewals retry with 0.5/1/2-second capped backoff while the original
proven lease has more than the safety margin remaining. They never extend the
known deadline without a successful renewal response. The independent
watchdog still fences the process before ownership expires. Manifest commits and
external delivery are never blindly retried; ambiguous writes fail closed.
A missed daily or weekly synthesis requires an explicit catch-up slot. For example, after
reviewing the missed summer daily slot:

```sh
python -m digest.cloud_run daily --catch-up-slot 2026-10-08T18:30:00+00:00
```

The slot must match the schedule, be in the past and not already completed.
Run with the same configured Azure identity/namespace and image; do not launch
an unguarded `python -m digest` process against production cloud state.

For a failed window summarization, first inspect a validated copy of the latest
persisted bundle. Check `cloud_completed_slots`, unclaimed `items` (`digest_id IS
NULL`), the relevant `digests` and their per-channel flags, and
`cloud_delivery_intents`. Collection commits items and cursors before synthesis;
a failed summary leaves those items unclaimed and the slot incomplete. A later
window may already have claimed and delivered them. Do not reset cursors, delete
digests or replay an already delivered backlog. Reconcile `inflight` or
`uncertain` Telegram delivery against the destination before any resend.

After reviewing the state and deploying the fix, a daytime catch-up uses an Azure
job execution override with the same configured identity and namespace:

```sh
python -m digest.cloud_run daytime --catch-up-slot 2026-10-10T12:00:00+00:00
```

This processes the current unclaimed backlog and newly collected items, subject
to the normal selection limits; it does not reconstruct an exact historical
window. Verify a successful execution, persisted slot completion, channel flags
and the actual destinations. Repeating a completed slot skips work. An alert
automatically resolving only means its rolling query condition cleared; it does
not prove the missed digest was recovered.

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
Revert the image pin (see [Image upgrades](#image-upgrades)) if a compatible
prior cloud image is required; preserve uncertain-delivery
records across recovery. Restoring an older backup can replay confirmed sends
that happened after that snapshot: reconcile destination history and affected
cursors/delivery intent before resuming publication.

Do not create an empty database to make startup succeed. Test export/restore
into a separate namespace before calling backup complete. Seven daily backups
and 365 days of archives are distinct from 90-day item pruning. Checkpoint
garbage collection must protect the current manifest and retained backups;
ordinary age-only Blob lifecycle deletion is unsafe for referenced objects.

### Independent daily backup account

The backup job must copy a validated `bundle.tar.gz` and `manifest.json` to the
separate private `digest-backups` container/account before its slot can complete.
It uploads the bundle first, verifies its stored size and SHA-256 by streaming
readback, then uploads and verifies the manifest as the completion marker.
Transient storage failures allow up to three attempts with 0.5/1-second
backoff inside the existing lease safety margin. Each copy retry uses a fresh
prefix; an ambiguous partial copy is not recorded as a successful backup and
may leave an orphan for retention cleanup. SDK automatic retries stay disabled
because the guarded
outer loop owns these budgets. Authentication, missing state, integrity errors
and lease loss fail immediately. A final failed copy fails the backup job.
Retained references in the runtime account are useful local
history but are not an independent account backup.

`DIGEST_CLOUD_BACKUP_ACCOUNT_URL`, `DIGEST_CLOUD_BACKUP_CONTAINER` and
`DIGEST_CLOUD_BACKUP_IDENTITY_CLIENT_ID` are configured on the backup job only.
The distinct backup-writer identity can write that container; ordinary runner
and runtime pruning cannot access it. Blob versioning and 14-day soft deletion
protect logical deletion; lifecycle retains independent copies/old versions for
30 days under the backup prefix, with deleted versions retained for up to
14 additional days by soft deletion and billed accordingly. The runtime account
retains seven daily
referenced generations separately. This is not immutable storage: the backup
writer can delete copies, and both accounts share subscription/region and
administrative authority. Cross-subscription/provider or immutable retention is
a future requirement decision, not claimed protection.

For restore, grant a recovery operator temporary **Storage Blob Data Reader**
on the backup container, select an explicitly completed prefix from the backup
record (manifest must exist), and download only its bundle/manifest into an empty
0700 directory with 0600 files. Verify the manifest hash, SQLite integrity and
lineage, then use the existing `digest.cloud_state --operator-login bootstrap
/PRIVATE/RESTORE_SOURCE` into a fresh runtime namespace. Reconcile delivery
history after older restores. For the selected complete prefix, the downloads are:

```sh
umask 077
mkdir -m 700 /PRIVATE/RESTORE_SOURCE
export DIGEST_BACKUP_ACCOUNT=YOUR_BACKUP_ACCOUNT
export DIGEST_BACKUP_PREFIX=backups/production/YYYY-MM-DD/COMPLETE_COPY_UUID
az storage blob download --auth-mode login --account-name "$DIGEST_BACKUP_ACCOUNT" \
  --container-name digest-backups --name "$DIGEST_BACKUP_PREFIX/bundle.tar.gz" \
  --file /PRIVATE/RESTORE_SOURCE/bundle.tar.gz --output none
az storage blob download --auth-mode login --account-name "$DIGEST_BACKUP_ACCOUNT" \
  --container-name digest-backups --name "$DIGEST_BACKUP_PREFIX/manifest.json" \
  --file /PRIVATE/RESTORE_SOURCE/manifest.json --output none
chmod 600 /PRIVATE/RESTORE_SOURCE/bundle.tar.gz /PRIVATE/RESTORE_SOURCE/manifest.json
```

Keep the two files from one prefix together; do not mix dates or generations.
Revoke only the recovery assignment this procedure created and verify access
after propagation. Pilot acceptance requires a restore
from this **separate account**, not merely a second namespace in runtime storage.

## Alerts, cost and residency

Two log rules evaluate every 15 minutes: failures/uncertain delivery
and missing successful runs. Failures split by logical `Job` and `FailureStage`,
so the alert context identifies the affected job and stage. Window jobs report
`collection`, `summarization` or `delivery`; known synthesis failures in aggregate
jobs also report `summarization`. Watchdog failures report `lease` or `runtime`,
entrypoint exceptions report `entrypoint`, and platform system errors report
`platform`. Unclassified and older application logs use `application`. Resource
names supply the job when a log has no `job=` field. Each failing job/stage pair
can produce its own alert. Freshness remains aggregate and uses
20 hours for daytime, 26 hours for daily/overnight/evening/backup, six hours for
positions, two hours for Patreon/relay; weekly is checked Sunday after 23:00
Budapest through Monday before 22:00 while its expected result is inside Azure's
two-day log-alert lookback. Pilot validation must confirm actual table columns,
system error reasons and notification routing. Alerts stay disabled with
schedules. The Log Analytics quota can drop logs if exceeded; monitor quota
and tune before accepting the alerting evidence.

The pre-migration estimate was approximately $1–2/month including alerts with
available Container Apps grants; reserve $5 for a pilot. Durability checkpoints
skip uploads when the database/cookie/archive content is
unchanged and use fast gzip compression. Unchanged checkpoints still snapshot,
compress and hash locally; fast compression trades CPU for larger stored bytes.
A changed checkpoint still uploads the
full approximately 50 MB database bundle; **real storage and execution cost has
not been measured**. Blob soft deletion retains removed
objects for 14 extra days; reference-aware pruning and seven daily backups bound
ordinary live history. Recalculate transfers, writes, retained bytes, boot time
and logs after the pilot. The monthly budget defaults to 5 **billing-currency
units** for the **app resource group only**, sends alerts at 80/100%, and does
not cap spending. The separate backup and backend groups are outside that budget.
Reconcile all three groups, including vault operations and backup versions, when
measuring total monthly hosting cost. Subscription fees
remain separate. Dedicated-vault, backup-account and backend-storage costs belong in actual
spending reconciliation.

Runtime blobs, independent backup blobs, Terraform backend blobs, the dedicated
vault and 30-day logs
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
