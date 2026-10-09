## Environments

There are two environments, not a graduated pipeline: production, and whatever a
developer runs locally. There is no staging tier and no separate integration
environment. Production changes through the guarded release chain in
[deployment-architecture.md](deployment-architecture.md) — pull request, required
checks, image pin, guarded apply — and nothing between a merged pin bump and the
production jobs is a staging step. Until 2026-10-08 production was a homelab VM
and a manual deploy from a separate repository; it is Azure now.

| | Production | Local development |
|---|---|---|
| Where | Nine scheduled Azure Container Apps jobs in West Europe, one subscription, one region (`infra/azure/`) | Developer's own machine |
| How it's started | Each job's cron schedule, through `python -m digest.cloud_run JOB`; a Blob lease admits one run at a time | `docker compose run --rm digest` (`compose.yml`), by hand |
| Image | Pinned exact tag from GHCR, built by `.github/workflows/release.yml`, pinned in `infra/azure/image.auto.tfvars.json` | Built locally from `Dockerfile` (`docker compose build`) |
| Config | Real secrets from the digest Key Vault, resolved by the runner's managed identity; non-secret settings as job environment variables from reviewed Terraform input | A local `.env` file (see `.env.example`), never committed |
| Telegram / X sessions | The owner's real, long-lived personal sessions. The Telegram session string is a Key Vault secret; X cookies live in the runtime Blob state | Whatever session the developer supplies — typically the same owner's credentials, since there is no synthetic Telegram/X sandbox |
| State | The SQLite file stored as an immutable bundle in private Blob Storage, restored to local disk by each run and checkpointed back — the system of record | A throwaway file inside the named Docker volume `digest-data` (`compose.yml`); reset per README's inspection/reset instructions, never touching production data |
| Delivery channels | Telegram TL;DR ping + news site live; email implemented but disabled (`EMAIL_ENABLED=false` in the job settings) | Whatever the developer's `.env` enables — typically a subset, or `hide:` flags, to avoid posting test digests to the real Telegram topic |
| External calls | Real Telegram, X, Claude CLI, the Claude API (only if the federation variables are set; otherwise no fallback), news site | Also real, unless the developer disables a collector — this is not a mocked sandbox. Automated tests are the only place a hard rule applies: CLAUDE.md requires tests to never call the real Telegram or X APIs, or send real email; that mocking happens at the collector/emailer boundary in `tests/`, not in a local `docker compose run` |

### Rules

- Production data (the state bundle, archived digests, session secrets) is never
  copied into local development. A local run starts from an empty volume.
- Secrets never appear in this repository, in a Terraform input or in a plan
  log. Production secrets live in the digest Key Vault and are referenced by the
  jobs; the Terraform plan prints non-secret job settings as sensitive, because
  this repository and its workflow logs are public. Local secrets live only in
  the developer's own untracked `.env`.
- `X_ENABLED`, `REDDIT_ENABLED`, `POLYMARKET_ENABLED`, `HACKERNEWS_ENABLED` and
  the Patreon/positions/relay allowlists are empty/off unless explicitly
  configured — a fresh local checkout runs with almost every optional collector
  disabled until the developer opts in, same as a fresh production deploy would.
- There is nothing to rehearse disaster recovery against in a lower environment
  by default: a rehearsal restores into a separate state namespace of the
  production storage account, never over the live state
  ([disaster-recovery.md](../reliability/disaster-recovery.md)). A restore from
  the separate backup account has not been exercised yet.
- `scripts/pr_summary.py` produces post-merge PR summaries on demand
  (`/pr-summary`); no CI job runs it. Like the CI workflows, it is a
  documentation tool, not a deployment or a third environment.
