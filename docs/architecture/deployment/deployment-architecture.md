## Deployment Architecture

Modelled in [`../model/deployment.dsl`](../model/deployment.dsl); see the
**ProductionDeployment** view (`docs/architecture/model/views.dsl`). This
document adds what the diagram cannot carry: the chain from a git tag to a
running container, and the schedule/lease model of the nine jobs. Local
development is covered separately in [environments.md](environments.md) — it is
not part of this model.

Production ran on a homelab VM (`01-myapps-vm`) until 2026-10-08. On that date
its digest timers were drained and stopped, the state was handed to Azure and
the Azure schedules were enabled
([ADR 0007](../decisions/0007-run-digest-as-azure-jobs.md)). The VM no longer
runs the digest and there is no plan to return execution to it. This document
describes Azure; where the VM matters it is named as history.

### What runs where

| Component | Runs on | Technology | Notes |
|---|---|---|---|
| Digest Runner | Azure Container Apps jobs, West Europe | Python 3.14, one short-lived container per execution, non-root (`uid 1000`) | Nine scheduled jobs, 0.5 vCPU / 1 GiB each, parallelism 1, no platform retries. A job restores the state, does one piece of work and exits. Never long-running |
| State Database | Runtime storage account (private Azure Blob Storage) | SQLite file, stored as an immutable bundle pointed to by one `manifest.json` | System of record. Each run restores the bundle to local disk, works on it and checkpoints a new bundle ([ADR 0007](../decisions/0007-run-digest-as-azure-jobs.md)). Shared-key access is disabled; access is by managed identity |
| Recovery copies | Backup storage account in a separate resource group, same region | Azure Blob Storage | Written daily by the `backup` job under a separate identity ([backup-strategy.md](../reliability/backup-strategy.md)) |
| Secrets | A dedicated digest Key Vault | Azure Key Vault | Resolved at job start through Key Vault references and the runner's managed identity. No secret value is a Terraform input |
| News Site | Cloudflare Workers | Cloudflare Worker, JavaScript (`workers/news-site/`) | Deployed independently with `wrangler`, not from this repo's release pipeline |
| Site Database | Cloudflare D1 | Managed | Rendering copy only; disposable, rebuilt from the state database ([ADR 0003](../decisions/0003-sqlite-is-the-source-of-truth.md)) |

The jobs and their state live in one Azure region, West Europe, in one
subscription: that region is the whole failure domain for the runner and its
state ([C-05](../requirements/constraints.md)). The recovery copies are in the
same region, so they protect against loss of the state, not against loss of the
region. There is exactly one environment
([model/deployment.dsl](../model/deployment.dsl)); it is not staged or
replicated. See [availability.md](../reliability/availability.md) and
[disaster-recovery.md](../reliability/disaster-recovery.md) for the
consequences.

The infrastructure is declared in this repository's `infra/azure/` (Terraform),
with Terraform state in Azure Blob Storage in a separate, bootstrapped backend
resource group. Exact settings are in that folder and in the
[migration runbook](../../azure-migration.md); they are not repeated here.

### Tag → image → pin → deploy

This repository owns its own deployment now ([ADR
0007](../decisions/0007-run-digest-as-azure-jobs.md), which supersedes the
deployment-ownership part of [ADR
0006](../decisions/0006-release-by-tag-and-pin.md)). The release is still an
exact, pinned image; what changed is who does each step. The
[migration runbook](../../azure-migration.md#image-upgrades) owns the commands.

1. **Tag.** A merge to `main` that touches shipped files (`Dockerfile`,
   `digest/`, `prompts/`, `pyproject.toml`, `uv.lock`) is gated by lint and
   tests and then tagged as the next patch version by
   `.github/workflows/auto-release.yml`. Docs-, workflow- and infra-only merges
   release nothing. Minor and major tags are still pushed by hand.
2. **Build.** The release workflow (`.github/workflows/release.yml`) builds the
   image from `Dockerfile` and pushes it to the private
   `ghcr.io/dezoxy/notification-digest`. `docker/metadata-action`'s
   `type=semver,pattern={{version}}` strips the leading `v`, so tag `v0.26.4`
   becomes image tag `0.26.4`. There is deliberately no floating `latest` tag:
   the pin is always an exact version.
3. **Never built where it runs.** The image is built only by that workflow.
   `docker build .` / `docker compose build` in this repo (`compose.yml`) are
   for local development only.
4. **Pin.** The version is pinned in the tracked file
   `infra/azure/image.auto.tfvars.json`. Renovate opens the bump PR when a new
   tag lands in GHCR and merges it once CI passes. The jobs pull the private
   image with a read-only token held in Key Vault.
5. **Deploy.** Merging the pin bump runs `.github/workflows/azure-deploy.yml`.
   It plans with Azure OIDC (no stored cloud credential), and its
   `release-apply` job applies that plan only if
   `scripts/azure_release_guard.py` finds nothing but the image of all nine jobs
   changed, and only once no job execution is running. Anything else is refused
   and goes through the manual two-step plan/apply that the owner starts and
   reviews. Schedule activation and every non-image infrastructure change stay
   manual.

So merged shipped code reaches Azure without a manual step, and there is no
human gate between the pin merge and production beyond CI, the guard and the
protection on `main` (pull request required, four required checks). This is a
different risk from the VM era, when a separate manual deploy sat between a tag
and production. See the **Delivery** view for the full chain outside a run.

Until 2026-10-08 the chain ended elsewhere: the separate homelab repository
pinned the image as `myapps_digest_image` and a manual `make deploy` there put
it on the VM.

### Schedule and lease model

Scheduling is declared in `infra/azure/main.tf`: one Container Apps job per run
mode, each with a cron expression in UTC. The command is
`python -m digest.cloud_run JOB`, which wraps the run modes the
[README](../../../README.md) documents.

| Job | Cron (UTC) | Notes |
|---|---|---|
| `daytime` | `0 6,12 * * *` | Plain window run |
| `overnight` | `0 0 * * *` | Window run with `hide:telegram` |
| `evening` | `0 18 * * *` | Window run with `hide:telegram,site` |
| `daily` | `30 18,19 * * *` | Proceeds only when it is 20:30 in Europe/Budapest, so one of the two fires each day whatever the DST state |
| `weekly` | `45 19,20 * * 0` | Same mechanism, Sunday 21:45 Budapest |
| `positions` | `25 1,5,9,13,17,21 * * *` | Every four hours |
| `patreon` | `50 * * * *` | Hourly |
| `relay` | `40 * * * *` | Hourly |
| `backup` | `15 4 * * *` | Daily recovery copy |

What guarantees that two runs never overlap:

- Every job takes a **Blob lease** on the state's `manifest.json` (60 seconds,
  renewed every 15 seconds by a watchdog) before it touches the state, and
  checkpoints a new bundle with a conditional manifest update
  ([ADR 0007](../decisions/0007-run-digest-as-azure-jobs.md)). The lease
  replaces the `flock` wrapper the VM used, and is stronger: it is held in
  storage, so it spans any number of containers, not one host.
- The reason it exists is unchanged: every job shares one Telegram user session
  (`TG_SESSION`, a single `StringSession`). Telethon does not tolerate two
  concurrent connections on one session — a prior `Persistent=true` boot-storm
  on the VM caused exactly that (`AuthKeyDuplicatedError`).
- If the lease cannot be proven, the watchdog kills the whole process group. A
  run is capped at 35 minutes.
- A job that finds the lease busy waits up to 10 minutes. The cursor-based jobs
  (`daytime`, `overnight`, `evening`, `positions`, `patreon`, `relay`) then skip
  that slot and succeed, because the next run collects what they missed.
  `daily`, `weekly` and `backup` fail instead and need an explicit catch-up run.
- The lease is the **only** overlap guard. Real contention between two
  overlapping executions has not been tested, so this is a designed guarantee,
  not an observed one.
- Inside a single run, the runner itself has no internal concurrency to
  serialize: it is one process, collectors run sequentially, and the run ends
  with exactly one exit code
  ([observability-architecture.md](../observability/observability-architecture.md)).

### Container shape

- `Dockerfile` builds a non-root (`uid 1000`) image with Python 3.14,
  `uv`-managed dependencies, Node.js + the pinned Claude Code CLI. The CLI is
  pinned in `Dockerfile` and bumped there; subscription authentication is
  preserved (`CLAUDE_CODE_OAUTH_TOKEN`, from Key Vault).
- The Container Apps job runs `python -m digest.cloud_run JOB`; there is no
  server process and no exposed port. The image's own `ENTRYPOINT` is
  `python -m digest`, which `compose.yml` uses locally.
- `compose.yml` in this repo is explicitly local-dev-only (its own header
  comment says so); production is the Terraform in `infra/azure/` and is never
  built from `compose.yml`.
