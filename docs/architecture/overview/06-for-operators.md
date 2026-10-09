## For operators

A reading path for the person who has to keep this service running and fix it
when it breaks. Five stops.

### How a change reaches the jobs

![Delivery view: how a code change reaches the running jobs](embed:Delivery)

A merge to `main` that touches shipped files (`Dockerfile`, `digest/`,
`prompts/`, `pyproject.toml`, `uv.lock`) is gated by lint and tests, tagged as
the next patch version by `.github/workflows/auto-release.yml`, and built and
pushed to GHCR by `.github/workflows/release.yml` with no floating `latest`
tag. Docs-, workflow- and infrastructure-only merges release nothing; minor and
major tags are pushed by hand. The image is private and pulled with a read-only
token held in the digest Key Vault.

The running version is the pin in `infra/azure/image.auto.tfvars.json`. Renovate
opens the pull request that bumps it after each release, and merging that
bump runs `azure-deploy`: Terraform plans with an Azure OIDC identity, so no
cloud credential is stored, and the `release-apply` job applies the plan only if
`scripts/azure_release_guard.py` finds nothing but the image of all nine jobs
changed, and only once no job execution is running. Anything else is refused
and goes through the manual two-step plan and apply, which the owner starts and
reviews. `main` itself is protected: a pull request and four required checks.
So merged shipped code reaches production without a manual step, but a
schedule change or any other infrastructure change never does. See ADR 6, ADR 7
and **Deployment Architecture**, later in this document
([source](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0006-release-by-tag-and-pin.md);
the procedure is [Image upgrades](https://github.com/Dezoxy/notification-digest/blob/main/docs/azure-migration.md#image-upgrades)
in the runbook).

### Where it runs, and what fails together

![Deployment view: the Azure region, its jobs and storage, and the Cloudflare edge](embed:ProductionDeployment)

Production has run in one Azure region, West Europe, since 2026-10-08; the
homelab VM `01-myapps-vm` ran it before that and no longer runs the digest. The
region holds a Container Apps environment with nine scheduled jobs, a runtime
storage account that holds the SQLite state, and a separate backup storage
account in its own resource group. Cloudflare serves the public archive from its
edge. Each execution is one short-lived container running
`python -m digest.cloud_run JOB`, with parallelism 1 and no platform retries.
The jobs, all in UTC:

| Job | Schedule (cron, UTC) |
|---|---|
| `daytime` | `0 6,12 * * *` |
| `overnight` | `0 0 * * *` |
| `evening` | `0 18 * * *` |
| `daily` | `30 18,19 * * *` |
| `weekly` | `45 19,20 * * 0` |
| `positions` | `25 1,5,9,13,17,21 * * *` |
| `patreon` | `50 * * * *` |
| `relay` | `40 * * * *` |
| `backup` | `15 4 * * *` |

`daily` and `weekly` fire at two UTC times so that one of them lands on 20:30
and Sunday 21:45 in Europe/Budapest whatever the daylight-saving state; the job
itself proceeds only at the right local time.

Everything the runner needs fails together with the region: the jobs, the
runtime state and, in the same region but a separate account, the recovery
copy. A Blob lease on the state manifest replaces the old host lock. It is
taken for 60 seconds and renewed every 15 by a watchdog; a run restores the
state bundle to local disk, works on it and checkpoints a new bundle. The lease
exists because every run shares one Telegram user session, and two concurrent
connections on that session previously produced `AuthKeyDuplicatedError`
during a boot storm on the VM. If the lease cannot be proven, the watchdog
kills the whole process group, and a run is capped at 35 minutes. A job that
finds the lease busy waits up to 10 minutes. The cursor-based jobs (`daytime`,
`overnight`, `evening`, `positions`, `patreon`, `relay`) then skip that slot and
catch up on the next; `daily`, `weekly` and `backup` fail and need an explicit
catch-up run. Real contention between two overlapping executions has not been
tested. See **Environments**, later in this document.

### What "available" means for a one-shot job

There is no request to time out and no uptime percentage to hit — QA-06
deliberately sets no availability target. What matters, in order: a digest is
never delivered twice (QA-01, absolute), a missed run is tolerable because
collection is cursor-based and the next run picks up what the last one
missed, and one failing collector must not suppress the rest. In practice
this shows up as isolated, banner-flagged collector failures, a per-run
Telegram circuit breaker that trips on the first HTTP 429 rather than
retrying into a flood, and a freshness guard that marks a digest older than
24h delivered without actually sending it to Telegram. See **Availability**,
later in this document.

### Backups and restore

The only store worth backing up is `state.db` — the SQLite system of record,
which lives in the runtime storage account as an immutable bundle behind one
manifest. The `backup` job runs daily at 04:15 UTC and copies a recovery bundle
to the separate backup storage account, written by a separate identity attached
only to that job; a lifecycle rule deletes copies after 30 days. Shared-key
access to both accounts is disabled. No restore from the backup account has
been exercised yet.

Restoring is a manual procedure in the runbook, not an application feature:
drain the jobs, export the chosen daily backup, bootstrap it into a fresh
namespace, verify bundle, database and cookie hashes and row counts, point the
Terraform `namespace` at it, and resume only after the lineage checks. Recovery
stays within Azure; returning execution to the VM is outside the approved
design. A restore rolls state back to the backup's point in time, with a real,
unresolved consequence (TD-006): a digest already marked delivered after the
backup can look pending again, and the normal retry pass will resend it,
directly against QA-01. There is no automated safeguard for this today — after
any restore, reconcile what was actually delivered with the destination before
the jobs resume. See **Backup Strategy** and **Disaster Recovery**, later in
this document
([procedure](https://github.com/Dezoxy/notification-digest/blob/main/docs/azure-migration.md#interrupted-runs-and-recovery)).

### How a failure actually becomes noticeable

The application emits an exit code (0 success, 1 a collector or delivery leg
failed, 2 a config error before any work started) and structured JSON log lines
to stdout, which the Container Apps environment sends to one Log Analytics
workspace. Two alert rules are declared in `infra/azure/` and evaluated every
15 minutes: `failures` fires on a `cloud_job event=failure`, on a "cloud
delivery uncertain" log line or on a platform error such as an image-pull
failure in the last 30 minutes; `freshness` fires when a job has no success
inside its expected window (20 hours for `daytime`; 26 for `overnight`,
`evening`, `daily`, `weekly` and `backup`; 6 for `positions`; 2 for `patreon`
and `relay`). Both are severity 2 and e-mail the owner through an action group.
A monthly budget on the runtime resource group also notifies the owner; it
does not cap spending. Whether either rule has fired for a real failure is not
recorded, so treat them as declared, not as proven. The homelab Loki and
Grafana no longer receive this service's logs. In practice, in order of what is
known to work: the digest doesn't arrive or arrives with a visible per-source
failure banner, and the alerts above should say so. There is no metrics
pipeline, no tracing, and no dashboard for this service. See **Observability
Architecture**, later in this document.
