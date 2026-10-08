# Azure digest migration and operations

Status on 2026-10-08: implementation prepared for review; production still runs
on `01-myapps-vm`. This runbook describes the authorized hosting target and the
remaining deployment gates. Returning execution to the VM is outside the plan.

The target runs the existing release image as nine finite Container Apps Jobs
in West Europe, with 0.5 vCPU, 1 GiB, no platform retries and a 50-minute
platform timeout. The runner allows at most ten minutes waiting
for the lease and 35 minutes of active work, then fences the process group.
It preserves `CLAUDE_CODE_OAUTH_TOKEN` subscription authentication, Claude CLI
version, models, effort, prompts, translations, verification and OpenRouter
fallbacks. A hosting change does not remove subscription contention with other
applications using the same account.

## Prepare configuration and deployment identity

The stack lives in [infra/azure](../infra/azure/). Its dedicated HCP Terraform
workspace is `toomhorvath/notification-digest-azure`; configure **Local execution**
with remote state before initialization. GitHub runners perform Azure operations
using OIDC; HCP Terraform stores and locks state. Never remove the `cloud` block
or fall back to a local state file.

Create separate GitHub deployment identity/federated credentials for this app.
The workflow uses subjects
`repo:Dezoxy/notification-digest:environment:azure-plan` and
`repo:Dezoxy/notification-digest:environment:azure-production`, audience
`api://AzureADTokenExchange`. Restrict both environments to `main`. Configure
required reviewer approval on `azure-production`; the apply job fails closed
if no required reviewers are configured. Review the saved plan before
approving its apply job. These repository/Entra settings are prerequisites,
not settings that the workflow YAML can enforce by itself.

The deployment identity needs resource creation/update permissions for the
dedicated resource group, and permission to grant the runner's container-scoped
Blob role and per-secret Key Vault roles. Bootstrap resource-group creation and
resource-provider registration separately if the identity is scoped to an
existing group. Register `Microsoft.App`, `Microsoft.Storage`,
`Microsoft.OperationalInsights`, `Microsoft.ManagedIdentity`, `Microsoft.Insights`
and `Microsoft.Consumption` through the account's established administrative
process. The provider does not automatically register providers. Import a
precreated resource group into the remote workspace before a first plan.

Supply environment variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and
`AZURE_SUBSCRIPTION_ID` as GitHub environment **variables**, and `TF_API_TOKEN`
as an environment **secret** limited to the dedicated workspace. Both plan and
apply environments need those credentials. `AZURE_TERRAFORM_VARS_JSON` on
`azure-plan` contains the reviewed Terraform inputs as JSON. Its contents must
only be nonsecret values and versionless Key Vault URLs.

Start from [production.auto.tfvars.example](../infra/azure/production.auto.tfvars.example).
The app configuration was rendered offline from the declared homelab defaults
and host overrides on 2026-10-08. All 17 referenced secret names/enabled flags
were verified using vault metadata; no values were read. The existing vault
`kv-homelab-prod-th` remains externally managed, in Germany West Central.
Recheck the baseline against the final live configuration before cutover:
collector flags, feeds/allowlists, topics, model/effort/budgets, translation,
verification, context, delivery channels and fallback omission semantics.
Leaving `FALLBACK_MODELS` unset retains the app's defaults; an empty value would
disable them. Azure env values are literal: keep `$FET,$ASI` as single dollars,
unlike the homelab Compose env-file escaping.

Choose the globally unique storage account name and alert email; update the
budget start to the first day of the deployment month. Select a tested migration
release image before manually executing any job. The default `0.28.0` records
the source baseline and does **not** contain the cloud runner. Activation also
requires `migration_release_verified=true`. The stack rejects floating tags.

The shared vault uses per-secret RBAC, and the runner identity has access only
to its state container and declared secrets. Private GHCR pulls use the existing
read-packages PAT via a Key Vault-backed registry password reference; Azure
managed identity cannot directly authenticate to GHCR. OAuth, session strings,
cookies and publication credentials never enter Terraform inputs. AzureRM may
store provider-computed Storage/Log Analytics keys in state: restrict and encrypt
the remote workspace and the private one-day plan artifacts; do not share or
publish binary plans. Shared Key storage authentication is
disabled; runtime storage access uses managed identity.

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
image/configuration, state retention and the nine **Manual** jobs.

For provisioning, dispatch `operation=apply`, review the newly generated plan
artifact, then approve `azure-production`. Apply uses that exact saved plan.
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
objects for one extra day; reference-aware pruning and seven daily backups bound
ordinary live history. Recalculate transfers, writes, retained bytes, boot time
and logs after the pilot. The monthly budget defaults to 5 **billing-currency
units**, sends alerts at 80/100%, and does not cap spending. Subscription fees
remain separate. Shared-vault costs and any provider-managed resource-group
charges should be included when reconciling actual spending.

Private blobs and 30-day logs stay in West Europe; the existing shared vault is
in Germany West Central. Azure managed identity, Container Apps definitions
and Blob SDK/leases create Azure coupling; SQLite and release containers remain
portable. Telegram, X, Claude and the existing Cloudflare site retain their
external processing/residency characteristics. Avoid publishing message bodies,
session/cookie contents, credential-bearing URLs or model inputs in logs.

Reference contracts: [Container Apps Jobs](https://learn.microsoft.com/en-us/azure/container-apps/jobs),
[AzureRM job resource](https://registry.terraform.io/providers/hashicorp/azurerm/5.4.0/docs/resources/container_app_job),
[Blob leases](https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blob-lease),
[Key Vault job secrets](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets),
[Azure budgets](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-acm-create-budgets).
