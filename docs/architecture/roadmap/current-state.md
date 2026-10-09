## Current State

What follows is what is actually deployed today, not what the code is capable
of if every flag were turned on.

### Where it runs

Production runs as nine scheduled Azure Container Apps jobs in West Europe,
declared in `infra/azure/` ([ADR
0007](../decisions/0007-run-digest-as-azure-jobs.md)). It has done so since
2026-10-08, when the homelab VM's digest timers were drained and stopped, the
state was handed to Azure and the Azure schedules were enabled. The VM no longer
runs the digest and there is no plan to return execution to it; its old state
directory and backups were left in place, frozen, and removing the digest role
from the homelab repository is an open pull request there. The summarizer's
fallback moved the same way: OpenRouter is gone, and a failed `claude -p` call
is retried against the Claude API with no stored key, through a federated
managed identity ([ADR
0009](../decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md)).

The state is the same SQLite file, now stored as an immutable bundle in private
Blob Storage and restored, used and checkpointed by every run under a Blob
lease. Recovery copies go daily to a separate storage account and resource group
in the same region. One region is the whole failure domain
([C-05](../requirements/constraints.md)).

### Run modes and their schedule

Nine Container Apps jobs exist, all serialized by the same Blob lease because
every mode shares one Telegram user session
([deployment-architecture.md](../deployment/deployment-architecture.md)):

| Mode | Cadence | Delivers into |
|---|---|---|
| window (`daytime`, `overnight`, `evening`) | ~6-hourly, split across three jobs (06 and 12 UTC plain; 00 UTC `hide:telegram`; 18 UTC `hide:telegram,site`) | Telegram TL;DR + news site |
| daily (`daily`) | Once/day, 20:30 Budapest | Telegram daily topic + news site |
| weekly (`weekly`) | Sunday, 21:45 Budapest | Telegram weekly topic + news site |
| patreon (`patreon`) | Hourly | Its own Telegram topic only — never the site |
| positions (`positions`) | Every 4 hours | Its own Telegram topic only, silent when nothing material happened (NO-SIGNAL contract) |
| relay (`relay`) | Hourly | Its own Telegram topic, a verbatim forward, no model call |
| backup (`backup`) | Daily, 04:15 UTC | Not a digest: copies a recovery bundle to the backup account |

`daily` and `weekly` are scheduled twice in UTC (one of the two fires per DST
state) and proceed only when the Budapest local time matches. The cron
expressions are in `infra/azure/main.tf` and are summarized in
[deployment-architecture.md](../deployment/deployment-architecture.md).

### What's collecting

Seven collectors exist; five run inside the six-hourly window sweep, one runs
only in its own dedicated mode, and one (Patreon) is excluded from the sweep
entirely:

- Window sweep: Telegram, X (`X_ENABLED`), RSS/Atom (`NEWS_FEEDS`), Polymarket
  (`POLYMARKET_ENABLED`), Reddit (`REDDIT_ENABLED`), Hacker News
  (`HACKERNEWS_ENABLED`) — each isolated per
  [P-05](../principles/architecture-principles.md), so one failing never
  suppresses the others.
- Patreon collects only in `patreon` mode, structurally excluded from the window sweep.
- Positions and relay read from the same sources as the window collectors but
  claim their own items via a separate membership rule — they are not additional
  collectors.

A single six-hourly window carries on the order of 100–200 items (PLAN.md §5) —
the only volume figure this repository has actually measured, against a live
prompt-design validation.

### What's delivering

- **Telegram TL;DR ping** — live.
- **News site** (`workers/news-site/`) — live; installable as a PWA with push
  notifications since 2026-08-27 (PLAN.md §11.7).
- **Email** — implemented, and still the in-repo default, but disabled on the
  real deployment (`EMAIL_ENABLED=false` in the job settings). The app refuses
  to start with every channel disabled.

### What's enabled beyond the shipped defaults

Three features ship default-off in this repository but are turned on for the
real deployment, per the reviewed non-secret job settings in `infra/azure/`
(`production.auto.tfvars.example` shows their shape): Hungarian
translation (`TRANSLATE_HU_ENABLED`), the daily web-verification pass
(`VERIFY_DAILY_ENABLED`), and story-arc context primers (`CONTEXT_ENABLED`). The
gap between the shipped default and the running configuration is tracked as
[TD-005](../risks/technical-debt.md).

### What's shipped on the site side

Per PLAN.md §11, every redesign-roadmap entry except one is shipped:
storyline-first arc pages with stable arc keys (§11.1), client-side catch-up
(§11.2), delta persistence for repeat stories (§11.3 — the Hungarian half is
open, [TD-004](../risks/technical-debt.md)), the verified daily briefing (§11.4,
flag on in production), story-arc background primers (§11.6), and the
installable PWA with push notifications (§11.7). Community engagement signals
(§11.5) were designed in full and then rejected before any code was written,
once the owner observed the community doesn't react or reply on Telegram at all
(PLAN.md §9, decision 6).

### What works

- A re-run over the same window collects and delivers nothing twice — the one
  absolute guarantee ([QA-01](../requirements/quality-attributes.md)), enforced
  by the cursor contract and covered by tests.
- One collector failing does not suppress the others or the digest itself ([P-05](../principles/architecture-principles.md)).
- Every digest records which model actually produced it, so a fallback is
  visible rather than silent ([ADR
  2](../decisions/0002-summarizer-fallback-chain.md)).

### What doesn't (yet)

| Gap | Reference |
|---|---|
| End-to-end run latency has never been measured | [TD-003](../risks/technical-debt.md) |
| A restore from the backup storage account has never been exercised | [backup-strategy.md](../reliability/backup-strategy.md) |
| The Claude API fallback has not yet served a real digest; the chain was proven by a one-off smoke test in the job environment on 2026-10-09 | [ADR 9](../decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md), [availability.md](../reliability/availability.md) |
| Real lease contention between two overlapping executions has not been tested; the lease is the only overlap guard | [deployment-architecture.md](../deployment/deployment-architecture.md) |
| The D1 rebuild-from-state path has never been exercised end to end | [TD-002](../risks/technical-debt.md) |
| `arc_context` has no prune | [TD-001](../risks/technical-debt.md) |
| Hungarian readers never see the "What changed" delta block | [TD-004](../risks/technical-debt.md) |
| Summarization spend has no hard cap, and Azure spend is bounded only by a budget notification, not a cap | [RISK-004](../risks/architecture-risks.md), [observability-architecture.md](../observability/observability-architecture.md) |

### Numbers

| Measure | Value | Evidence |
|---|---|---|
| Items per 6-hour window | ~100–200 | PLAN.md §5, validated against real prompt-design output |
| X collection frequency | 4 pulls/day (window mode is the only mode that ever touches X) | PLAN.md §8 |
| Run success rate | **Not measured** — no dashboard computes it | [availability.md](../reliability/availability.md) |
| End-to-end freshness (window close → delivery) | **Not measured** | [QA-02](../requirements/quality-attributes.md), [TD-003](../risks/technical-debt.md) |
| Backup restore | Exercised once on the VM before the move; **not exercised on Azure** | [QA-05](../requirements/quality-attributes.md), [backup-strategy.md](../reliability/backup-strategy.md) |
| Recovery objectives (RPO ~24h, RTO manual) | **Targets, not measurements** — no timed rehearsal exists | [disaster-recovery.md](../reliability/disaster-recovery.md) |
