## Availability

### What "available" means here

notification-digest is not a request-serving system, so an uptime percentage
does not apply to it ([QA-06](../requirements/quality-attributes.md)).
"Available" means: each scheduled job starts on its cron schedule, completes,
and exits 0. There is no traffic to lose, no request to time out, and no
customer waiting on a response — so the framing used for a service (SLO, error
budget) does not transfer, and this document does not invent one.

What matters instead, in order:

1. **A digest is never delivered twice.** This is a hard rule, not a target
   ([QA-01](../requirements/quality-attributes.md)). A duplicate Telegram ping
   or a duplicate site entry is a worse failure than a missed run, because it is
   visible and cannot be un-sent.
2. **A missed run is tolerable.** The next window's collectors pick up
   everything the failed run didn't, because collection is cursor-based, not
   queue-based. Nothing is lost as long as the source itself (Telegram, X, an
   RSS feed) still has the content when the next run reads it.
3. **One failing source must not suppress everything else.** Each of the six
   window collectors is isolated and failure-flagged independently
   ([README](../../../README.md), `digest/main.py`); a Telegram outage still
   lets the X/news/Reddit/Polymarket/Hacker News items ship, with a failure
   banner rather than silence.

### What is deliberately absent

- **No high availability.** One process per run, one Azure region (West Europe),
  no second region, no failover target. Azure restarts nothing on its own here:
  platform retries are disabled, so a failed execution stays failed until the
  next schedule or an explicit catch-up run. This is a direct consequence of
  [C-01](../requirements/constraints.md) and
  [C-05](../requirements/constraints.md) — a single-owner, single-reader service
  — not an oversight. Building HA for a job that runs a few times a day for one
  reader would add operational complexity nothing in this system needs.
- **No health check or liveness probe.** There is no long-running process to
  probe; the unit of work is the process itself, and its exit code and log lines
  are the health signal. Two log-based alerts watch for failed and for missing
  runs ([observability-architecture.md](../observability/observability-architecture.md));
  they are a detection mechanism, not a probe.
- **No load balancing or autoscaling.** There is no load to balance.
- **No SLA.** Nobody outside the owner depends on this system, so there is
  nothing to commit to.

### Failure modes

| Failure | Detection | Behavior | Impact |
|---|---|---|---|
| One collector fails (e.g. Telegram or X session invalid, a feed unreachable) | That collector's own exception handling; recorded in the `run_summary` log line and, for Telegram/X, an auth-failure log line | Run continues with the remaining collectors; digest ships with a failure banner for the affected source | That source's items are missing from this window; picked up next run if the source still has them, otherwise lost for that window |
| Claude CLI call fails or refuses (safeguards refusal, usage limit, timeout, empty output) | `summarize.py`'s call wrapper | Falls back through `FALLBACK_MODELS` in order via the Claude API (federated managed identity, prepaid credits) when the federation is configured; digest still ships, provenance recorded. A misconfigured federation fails only here, never at startup, so it must be smoke-tested ([runbook](../../azure-migration.md#claude-api-fallback-workload-identity-federation)) | Possible quality/cost difference, not a missed digest ([QA-04](../requirements/quality-attributes.md)) |
| Telegram rate-limits the bot (HTTP 429) | Per-run circuit breaker (`TelegramRunState`, `digest/deliver.py`) | First 429 in a run flips the breaker; every later Telegram send that run is skipped rather than retried, so the failure is contained to one run instead of a burst | The Telegram ping for that run is delayed to the next run's pending-retry pass, not duplicated (post-incident guard, see [2026-08-06 Telegram flood](../../incidents/2026-08-06-telegram-flood.md)) |
| A pending-digest backlog exists (e.g. after a restored backup, a re-enabled channel, or an extended site outage) | `_TELEGRAM_MAX_AGE` freshness guard (`digest/deliver.py`) | A pending digest older than 24h is never sent to Telegram — marked delivered without actually pinging, rather than blasting stale news | Old TL;DRs are not flooded into the live channel; the site (an archive channel) still backfills them |
| Two runs would otherwise overlap | The Blob lease on the state manifest, renewed by a watchdog ([deployment-architecture.md](../deployment/deployment-architecture.md)) | A job that finds the lease busy waits up to 10 minutes. The cursor-based jobs then skip that slot and exit 0; `daily`, `weekly` and `backup` fail and need an explicit catch-up run. If a running job cannot prove its lease, the watchdog kills its process group | Contention delays or skips a run rather than corrupting the session or the state. The lease is the only overlap guard, and real contention between two overlapping executions has **not been tested** |
| A job fails, or its slot is missed | The `failures` and `freshness` alerts, evaluated every 15 minutes, e-mail the owner ([observability-architecture.md](../observability/observability-architecture.md)); otherwise the owner notices the digest stopped arriving | No platform retry. Cursor-based jobs pick the missed input up on the next run; `daily` and `weekly` need an explicit catch-up run | A skipped cursor slot is invisible as a failure (it exits 0), so the freshness alert is what notices repeated skips |
| The Azure region, or the runtime storage, is lost | Nothing automatic in this repo; the failure alert fires if jobs run and fail, the freshness alert if they stop succeeding | No failover. Recovery is manual, within Azure | See [disaster-recovery.md](disaster-recovery.md) |

### Degraded operation

There is no degraded mode distinct from "some collectors failed, digest ships
anyway with a banner" — that partial-failure tolerance *is* the degraded mode,
and it is the normal, designed behavior described above, not an exception path.

### Not measured

- Run success rate (fraction of scheduled runs that exit 0) — no dashboard or
  report in this repo computes it. The freshness alert only fires when a job has
  no success inside its window; it does not count runs.
- End-to-end freshness (time from window close to delivery) — the cron schedule
  is declared in `infra/azure/main.tf`; latency has not been measured
  ([QA-02](../requirements/quality-attributes.md)).
- Any historical count of missed or duplicate runs. The Azure schedules have run
  only since 2026-10-08, so the history is short.
- The Claude API fallback has not yet served a real digest. The chain was proven
  by a one-off smoke test in the job environment on 2026-10-09, so the row above
  describes designed, not observed, behavior.
