## Environments

There are two environments, not a graduated pipeline: production, and whatever a
developer runs locally. There is no staging tier and no separate integration
environment — the homelab deploy invocation (`make deploy TARGET=01-myapps-vm
MODE=config`, [deployment-architecture.md](deployment-architecture.md)) has no
review gate between a local `make deploy` run and the production host. `MODE` is
mandatory there; there is no `MODE=staging`.

| | Production | Local development |
|---|---|---|
| Where | `01-myapps-vm`, single Proxmox node, home LAN only | Developer's own machine |
| How it's started | Nine systemd timers, each through the `digest-run` flock wrapper | `docker compose run --rm digest` (`compose.yml`), by hand |
| Image | Pinned exact tag from GHCR, built by `.github/workflows/release.yml` | Built locally from `Dockerfile` (`docker compose build`) |
| Config | Real secrets, injected as environment variables from Azure Key Vault at deploy time | A local `.env` file (see `.env.example`), never committed |
| Telegram / X sessions | The owner's real, long-lived personal sessions | Whatever session the developer supplies — typically the same owner's credentials, since there is no synthetic Telegram/X sandbox |
| State | `/srv/appdata/digest/state.db` on local ext4 — the system of record | A throwaway file inside the named Docker volume `digest-data` (`compose.yml`); reset per README's inspection/reset instructions, never touching production data |
| Delivery channels | Telegram TL;DR ping + news site live; email implemented but disabled (`myapps_digest_email_enabled: false` in homelab host_vars) | Whatever the developer's `.env` enables — typically a subset, or `hide:` flags, to avoid posting test digests to the real Telegram topic |
| External calls | Real Telegram, X, Claude CLI, OpenRouter, news site | Also real, unless the developer disables a collector — this is not a mocked sandbox. Automated tests are the only place a hard rule applies: CLAUDE.md requires tests to never call the real Telegram or X APIs, or send real email; that mocking happens at the collector/emailer boundary in `tests/`, not in a local `docker compose run` |

### Rules

- Production data (`state.db`, archived digests, session secrets) is never
  copied into local development. A local run starts from an empty volume.
- Secrets never appear in this repository. Production secrets come from Azure
  Key Vault at deploy time; local secrets live only in the developer's own
  untracked `.env`.
- `X_ENABLED`, `REDDIT_ENABLED`, `POLYMARKET_ENABLED`, `HACKERNEWS_ENABLED` and
  the Patreon/positions/relay allowlists are empty/off unless explicitly
  configured — a fresh local checkout runs with almost every optional collector
  disabled until the developer opts in, same as a fresh production deploy would.
- There is nothing to rehearse disaster recovery against in a lower environment
  ([disaster-recovery.md](../reliability/disaster-recovery.md) is exercised
  directly against production backups, because no scaled-down production
  look-alike exists).
- `scripts/pr_summary.py` produces post-merge PR summaries on demand
  (`/pr-summary`); no CI job runs it. Like the CI workflows, it is a
  documentation tool, not a deployment or a third environment.
