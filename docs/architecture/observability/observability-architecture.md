## Observability Architecture

This document is deliberately modest. notification-digest has no metrics
pipeline, no tracing, and no dashboard defined in this repository —
`pyproject.toml` pulls in no OpenTelemetry, Prometheus, or APM client of any
kind. What exists is process exit codes, structured log lines written to stdout,
a Log Analytics workspace that collects them, and two log-based alerts and a
budget notification that e-mail the owner. The honest summary, stated plainly
rather than implied: **a missed or broken run is noticed because an alert
e-mails the owner, or because the digest does not arrive or arrives with a
visible failure banner**. On 2026-10-10, the failure alert notified the owner
after a daytime synthesis exhausted its API fallback budget.

Until 2026-10-08 the digest ran on a homelab VM and its logs went to journald
and, if the host shipped them, Loki and Grafana. That path no longer receives
digest logs. Observability is Azure's now, declared in `infra/azure/` (the
alerts and budget in `infra/azure/monitoring.tf`); this document explains the
design and does not copy the queries or thresholds.

### What exists

| Signal | Mechanism | Where it goes |
|---|---|---|
| Exit code | `sys.exit(0)` success, `sys.exit(1)` a collector or delivery leg failed, `sys.exit(2)` a configuration error before any work started (`digest/main.py`) | The Container Apps job execution that ran the container records it as that execution's result. The wrapper `digest/cloud_run.py` adds its own line per execution: `cloud_job event=success`, `failure`, `skipped`, `already_completed` or `not_due` |
| Human-readable logs | Python `logging`, `basicConfig(level=INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)` (`digest/main.py`) | Container stdout and stderr, collected by the Container Apps environment into one Log Analytics workspace (console and system logs) |
| `run_summary` (one JSON line per run) | `logger.info("run_summary %s", json.dumps({...}))`, emitted from every run mode (window, daily, weekly, patreon, positions) | Same path. Fields include mode, per-collector or per-source-digest status, item counts, and whether delivery succeeded |
| `digest_delivery` (one JSON line per digest handled) | `digest/deliver.py`, logged once per digest with a per-channel (`email`/`site`/`telegram`) outcome | Same path |
| Platform events | Container Apps system logs: image-pull failures, crashing containers, failed executions | Same workspace |

The workspace keeps logs for 30 days, in West Europe, with a daily ingestion
quota: if the quota is exceeded, logs are dropped. The volume against that quota
has not been measured over a long enough period to say it is sufficient.

Both structured lines are explicitly designed, by this codebase's own
convention (dozens of comments across `digest/main.py`, `digest/deliver.py`,
`digest/summarize.py`, `digest/collectors/rss.py`, `digest/collectors/x.py` name
it directly), to be queryable. Those comments still say "Loki" because they were
written for the VM; the lines are queried in Log Analytics now. The comments
have not been reworded, and nothing in the code depends on the log backend.

### What the exit code and the log lines are trusted to do

The application's own code is explicit that the exit code is meant to be the
*only* signal an alert acts on: `digest/main.py`'s docstrings state outright
that "ANY collector failure must surface as a non-zero exit" even on a run that
delivers nothing at all, and every `run_summary`/`digest_delivery` line is
described in the same comments as being for the log backend, "not the alert
signal itself." That design assumption survived the move: the Azure `failures`
alert keys off the wrapper's `cloud_job event=failure` line and a "cloud
delivery uncertain" line, which a failed run produces, plus platform errors.

### Alerts and budget

All three e-mail the owner through one Azure Monitor action group
([`infra/azure/monitoring.tf`](../../../infra/azure/monitoring.tf)). The alert
rules are evaluated every 15 minutes at severity 2 and are enabled together with
the job schedules.

| Alert | Fires when | Why it exists |
|---|---|---|
| `failures` | In the last 30 minutes, any `cloud_job event=failure`, any "cloud delivery uncertain" line, or a platform error such as an image-pull failure or a crashing container | A run that failed, or a delivery whose outcome is unknown and must be reconciled by hand before it is resent |
| `freshness` | A job has no success inside its expected window: `daytime` 20 hours; `overnight`, `evening`, `daily`, `weekly` and `backup` 26 hours; `positions` 6 hours; `patreon` and `relay` 2 hours | A job that did not run, or kept skipping or failing, produces no failure line at all. This is the alert that notices silence, including a skipped cursor slot, which exits 0 |
| Monthly budget | Actual spend on the application resource group reaches 80% and 100% of the monthly amount | Early notice of unexpected cost. It does **not** cap spending, and it covers the application resource group only, not the backup or Terraform-backend groups |

The failure rule now declares dimensions for the logical job and failure stage;
the freshness rule remains aggregate. Application logs identify known collection,
synthesis and delivery failures, with generic categories for unclassified or
older logs. Platform failures use the resource name to identify the job. These
dimensions are implemented and query-tested; deployment and a real notification
with those dimensions still need confirmation. Query validation remains skipped
in Terraform because the log tables do not exist before the first execution.

On 2026-10-09, shortly after activation, the `freshness`
alert e-mailed the owner because jobs that had not yet had their first
scheduled run counted as stale. That shows the rule evaluates and its
notification is delivered; it was not a failed run. On 2026-10-10, `failures`
also fired after the daytime job failed synthesis. The alert later automatically
resolved as its rolling condition cleared; that does not prove digest recovery.

### How a failure actually becomes visible

In order of what is actually known to happen, most to least reliable:

1. **The digest doesn't arrive**, or arrives late, on Telegram or the news site.
   The owner is the only reader, so this is noticed directly and immediately for
   the channels the owner checks.
2. **The digest arrives with a failure banner** for a specific source —
   window-mode partial failures are deliberately visible in the delivered
   content itself (README, `digest/main.py`), not just in logs.
3. **The Container Apps job execution history** and its Log Analytics lines,
   checked by hand, show a failed or skipped run and its log lines (including
   `run_summary`/`digest_delivery`) if the owner goes looking.
4. **An alert e-mail** from `failures` or `freshness`, as above — both notification
   paths have been observed, including a real synthesis failure.

Beyond the digest content, the alerts above are the only mechanism that pushes a
failure notification to the owner.

### Gaps — explicitly not covered

- **No metrics.** No counters, gauges, or histograms; no request-rate, latency,
  or resource-usage signal (there is no request rate — see
  [availability.md](../reliability/availability.md)).
- **No tracing.** A single sequential process per run has little need for
  distributed tracing, but there is also no correlation ID threading a run's log
  lines together beyond timestamp proximity within one execution's logs.
- **No dashboard.** No Azure workbook or equivalent is defined for this service.
- **Freshness has no per-job dimensions.** Its aggregate alert still requires
  inspecting the logs to identify stale jobs. Failure dimensions identify the
  job and known stage, but generic stages still need log investigation.
- **Skipped slots are quiet.** A cursor-based job that skips a slot because the
  lease was busy exits 0 and logs a `skipped` line. Only `freshness` notices
  repeated skips, and only after its window passes.
- **Log retention is 30 days and quota-bound.** Nothing older than that is
  queryable, and a day over quota loses logs.
- **Per-collector failures are not surfaced anywhere except the log lines and
  the delivered digest's own banner.** There is no running count of "how often
  has collector X failed this month."
- **No business-level metrics** (item volume trends, model cost per run,
  fallback-chain trigger rate) are tracked or exported anywhere, despite
  [QA-07](../requirements/quality-attributes.md) naming cost as a quality
  attribute and [RISK-004](../risks/architecture-risks.md) tracking the fallback
  chain's cost exposure. The monthly budget notifies the owner of Azure spend,
  not of Claude API spend, so that exposure is still invisible until the owner
  notices a bill.

### Where this fits

See [deployment-architecture.md](../deployment/deployment-architecture.md) for
where the container and its logs physically run, and
[availability.md](../reliability/availability.md) for how detection interacts
with "a missed run is tolerable, a duplicate delivery is not."
