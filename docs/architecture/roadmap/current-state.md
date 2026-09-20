# Current State

What follows is what is actually deployed on the owner's VM today, not what the code is capable of if every flag were turned on.

## Run modes and their schedule

Nine systemd timers exist on `01-myapps-vm`, each serialized through the same host-level `flock` wrapper because every mode shares one Telegram user session ([deployment-architecture.md](../deployment/deployment-architecture.md)):

| Mode | Cadence | Delivers into |
|---|---|---|
| window (`digest`, `digest-overnight`, `digest-evening`) | ~6-hourly, split across three timer/flag combinations (plain, `hide:telegram`, `hide:telegram,site`) | Telegram TL;DR + news site |
| daily (`digest-daily`) | Once/day, 20:30 Budapest | Telegram daily topic + news site |
| weekly (`digest-weekly`) | Sunday, 21:45 Budapest | Telegram weekly topic + news site |
| patreon (`digest-patreon`) | Hourly | Its own Telegram topic only — never the site |
| positions (`digest-positions`) | Every 4 hours | Its own Telegram topic only, silent when nothing material happened (NO-SIGNAL contract) |
| relay (`digest-relay`) | Hourly | Its own Telegram topic, a verbatim forward, no model call |

The exact timer-unit-to-flag mapping for the three window-mode variants is homelab-repo configuration and **not verifiable from this repository**; the existence of all nine units, including `digest-relay`, is confirmed in [deployment-architecture.md](../deployment/deployment-architecture.md).

## What's collecting

Seven collectors exist; five run inside the six-hourly window sweep, one runs only in its own dedicated mode, and one (Patreon) is excluded from the sweep entirely:

- Window sweep: Telegram, X (`X_ENABLED`), RSS/Atom (`NEWS_FEEDS`), Polymarket (`POLYMARKET_ENABLED`), Reddit (`REDDIT_ENABLED`), Hacker News (`HACKERNEWS_ENABLED`) — each isolated per [P-05](../principles/architecture-principles.md), so one failing never suppresses the others.
- Patreon collects only in `patreon` mode, structurally excluded from the window sweep.
- Positions and relay read from the same sources as the window collectors but claim their own items via a separate membership rule — they are not additional collectors.

A single six-hourly window carries on the order of 100–200 items (PLAN.md §5) — the only volume figure this repository has actually measured, against a live prompt-design validation.

## What's delivering

- **Telegram TL;DR ping** — live.
- **News site** (`workers/news-site/`) — live; installable as a PWA with push notifications since 2026-08-27 (PLAN.md §11.7).
- **Email** — implemented, and still the Ansible role's default, but disabled on the real deployment (`myapps_digest_email_enabled: false`).

## What's enabled beyond the shipped defaults

Three features ship default-off in this repository but are turned on for the real deployment, per the separate homelab repository's host variables: Hungarian translation (`TRANSLATE_HU_ENABLED`), the daily web-verification pass (`VERIFY_DAILY_ENABLED`), and story-arc context primers (`CONTEXT_ENABLED`). The gap between the shipped default and the running configuration is tracked as [TD-005](../risks/technical-debt.md).

## What's shipped on the site side

Per PLAN.md §11, every redesign-roadmap entry except one is shipped: storyline-first arc pages with stable arc keys (§11.1), client-side catch-up (§11.2), delta persistence for repeat stories (§11.3 — the Hungarian half is open, [TD-004](../risks/technical-debt.md)), the verified daily briefing (§11.4, flag on in production), story-arc background primers (§11.6), and the installable PWA with push notifications (§11.7). Community engagement signals (§11.5) were designed in full and then rejected before any code was written, once the owner observed the community doesn't react or reply on Telegram at all (PLAN.md §9, decision 6).

## What works

- A re-run over the same window collects and delivers nothing twice — the one absolute guarantee ([QA-01](../requirements/quality-attributes.md)), enforced by the cursor contract and covered by tests.
- One collector failing does not suppress the others or the digest itself ([P-05](../principles/architecture-principles.md)).
- Every digest records which model actually produced it, so a fallback is visible rather than silent ([ADR 2](../decisions/0002-summarizer-fallback-chain.md)).

## What doesn't (yet)

| Gap | Reference |
|---|---|
| End-to-end run latency has never been measured | [TD-003](../risks/technical-debt.md) |
| The D1 rebuild-from-VM path has never been exercised end to end | [TD-002](../risks/technical-debt.md) |
| `arc_context` has no prune | [TD-001](../risks/technical-debt.md) |
| Hungarian readers never see the "What changed" delta block | [TD-004](../risks/technical-debt.md) |
| Summarization spend has no hard cap | [RISK-004](../risks/architecture-risks.md) |

## Numbers

| Measure | Value | Evidence |
|---|---|---|
| Items per 6-hour window | ~100–200 | PLAN.md §5, validated against real prompt-design output |
| X collection frequency | 4 pulls/day (window mode is the only mode that ever touches X) | PLAN.md §8 |
| Run success rate | **Not measured** — no dashboard computes it | [availability.md](../reliability/availability.md) |
| End-to-end freshness (window close → delivery) | **Not measured** | [QA-02](../requirements/quality-attributes.md), [TD-003](../risks/technical-debt.md) |
| Backup restore | Exercised once | [QA-05](../requirements/quality-attributes.md) |
