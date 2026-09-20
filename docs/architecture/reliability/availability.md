# Availability

## What "available" means here

notification-digest is not a request-serving system, so an uptime percentage does not apply to it ([QA-06](../requirements/quality-attributes.md)). "Available" means: each scheduled run starts on its timer, completes, and exits 0. There is no traffic to lose, no request to time out, and no customer waiting on a response — so the framing used for a service (SLO, error budget) does not transfer, and this document does not invent one.

What matters instead, in order:

1. **A digest is never delivered twice.** This is a hard rule, not a target ([QA-01](../requirements/quality-attributes.md)). A duplicate Telegram ping or a duplicate site entry is a worse failure than a missed run, because it is visible and cannot be un-sent.
2. **A missed run is tolerable.** The next window's collectors pick up everything the failed run didn't, because collection is cursor-based, not queue-based. Nothing is lost as long as the source itself (Telegram, X, an RSS feed) still has the content when the next run reads it.
3. **One failing source must not suppress everything else.** Each of the six window collectors is isolated and failure-flagged independently ([README](../../../README.md), `digest/main.py`); a Telegram outage still lets the X/news/Reddit/Polymarket/Hacker News items ship, with a failure banner rather than silence.

## What is deliberately absent

- **No high availability.** One process, one Proxmox node, no redundant compute, no failover target. This is a direct consequence of [C-01](../requirements/constraints.md) and [C-05](../requirements/constraints.md) — a single-owner, single-reader service — not an oversight. Building HA for a job that runs a few times a day for one reader would add operational complexity nothing in this system needs.
- **No health check or liveness probe.** There is no long-running process to probe; the unit of work is the process itself, and its exit code is the entire health signal ([observability-architecture.md](../observability/observability-architecture.md)).
- **No load balancing or autoscaling.** There is no load to balance.
- **No SLA.** Nobody outside the owner depends on this system, so there is nothing to commit to.

## Failure modes

| Failure | Detection | Behavior | Impact |
|---|---|---|---|
| One collector fails (e.g. Telegram or X session invalid, a feed unreachable) | That collector's own exception handling; recorded in the `run_summary` log line and, for Telegram/X, an auth-failure log line | Run continues with the remaining collectors; digest ships with a failure banner for the affected source | That source's items are missing from this window; picked up next run if the source still has them, otherwise lost for that window |
| Claude CLI call fails or refuses (safeguards refusal, usage limit, timeout, empty output) | `summarize.py`'s call wrapper | Falls back through `FALLBACK_MODELS` in order via OpenRouter; digest still ships, provenance recorded | Possible quality/cost difference, not a missed digest ([QA-04](../requirements/quality-attributes.md)) |
| Telegram rate-limits the bot (HTTP 429) | Per-run circuit breaker (`TelegramRunState`, `digest/deliver.py`) | First 429 in a run flips the breaker; every later Telegram send that run is skipped rather than retried, so the failure is contained to one run instead of a burst | The Telegram ping for that run is delayed to the next run's pending-retry pass, not duplicated (post-incident guard, see [2026-08-06 Telegram flood](../../incidents/2026-08-06-telegram-flood.md)) |
| A pending-digest backlog exists (e.g. after a restored backup, a re-enabled channel, or an extended site outage) | `_TELEGRAM_MAX_AGE` freshness guard (`digest/deliver.py`) | A pending digest older than 24h is never sent to Telegram — marked delivered without actually pinging, rather than blasting stale news | Old TL;DRs are not flooded into the live channel; the site (an archive channel) still backfills them |
| Two runs would otherwise overlap | Host-level `flock` in `/usr/local/sbin/digest-run` ([deployment-architecture.md](../deployment/deployment-architecture.md)) | The lock prevents two processes from sharing one Telegram session concurrently | Contention delays a run rather than corrupting the session; exact wait/skip behavior of the wrapper is **not verified from this repository** (the script lives in the homelab repo) |
| The VM or its host node is lost | Nothing automatic in this repo; the owner notices the digest stopped arriving | No failover. Recovery is a manual rebuild | See [disaster-recovery.md](disaster-recovery.md) |

## Degraded operation

There is no degraded mode distinct from "some collectors failed, digest ships anyway with a banner" — that partial-failure tolerance *is* the degraded mode, and it is the normal, designed behavior described above, not an exception path.

## Not measured

- Run success rate (fraction of scheduled runs that exit 0) — no dashboard or report in this repo computes it.
- End-to-end freshness (time from window close to delivery) — the timer schedule is declared in the homelab role; latency has not been measured ([QA-02](../requirements/quality-attributes.md)).
- Any historical count of missed or duplicate runs.
