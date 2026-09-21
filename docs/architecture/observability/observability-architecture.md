## Observability Architecture

This document is deliberately modest. notification-digest has no metrics
pipeline, no tracing, and no dashboard defined in this repository —
`pyproject.toml` pulls in no OpenTelemetry, Prometheus, or APM client of any
kind. What exists is process exit codes, structured log lines written to stdout,
and whatever the host's systemd/journald setup does with them. The honest
summary, stated plainly rather than implied: **a missed or broken run is noticed
because the digest does not arrive, or arrives with a visible failure banner** —
not because an alert fired.

### What exists

| Signal | Mechanism | Where it goes |
|---|---|---|
| Exit code | `sys.exit(0)` success, `sys.exit(1)` a collector or delivery leg failed, `sys.exit(2)` a configuration error before any work started (`digest/main.py`) | Whatever invokes the container — the systemd unit wrapping `docker compose run --rm digest` — records it as the unit's result |
| Human-readable logs | Python `logging`, `basicConfig(level=INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)` (`digest/main.py`) | Container stdout. Since the systemd unit runs `docker compose run` attached (not detached), that output is inherited by the unit's own stdout and captured by journald on the VM |
| `run_summary` (one JSON line per run) | `logger.info("run_summary %s", json.dumps({...}))`, emitted from every run mode (window, daily, weekly, patreon, positions) | Same stdout/journald path. Fields include mode, per-collector or per-source-digest status, item counts, and whether delivery succeeded |
| `digest_delivery` (one JSON line per digest handled) | `digest/deliver.py`, logged once per digest with a per-channel (`email`/`site`/`telegram`) outcome | Same path |

Both structured lines are explicitly designed, by this codebase's own convention
(dozens of comments across `digest/main.py`, `digest/deliver.py`,
`digest/summarize.py`, `digest/collectors/rss.py`, `digest/collectors/x.py` name
it directly), to be queryable in Loki *if* the host ships journald into Loki.
Whether that ingestion pipeline actually exists and is currently working is
homelab-repo infrastructure and **is not verified from this repository**.

### What the exit code alone is trusted to do

The application's own code is explicit that the exit code is meant to be the
*only* signal an alert acts on: `digest/main.py`'s docstrings state outright
that "ANY collector failure must surface as a non-zero exit (the sole signal for
the Loki alert on digest.service) even on a run that delivers nothing at all,"
and every `run_summary`/`digest_delivery` line is described in the same comments
as being "for Loki, not the alert signal itself." That is the application's
design assumption, built in from the start. Whether a Grafana/Loki alert on
`digest.service` failures is actually configured today is a separate,
homelab-repo question this repository cannot answer; PLAN.md §6 describes the
*intended* rule ("add a Grafana alert rule on `{unit=\"digest.service\"}`
matched against journal output for `digest.service`, firing on any failed run")
in terms that read as a plan, not a confirmed deployment. Treat "an alert exists
and pages the owner on a failed run" as **not confirmed**, not as fact.

### How a failure actually becomes visible

In order of what is actually known to happen, most to least reliable:

1. **The digest doesn't arrive**, or arrives late, on Telegram or the news site.
   The owner is the only reader, so this is noticed directly and immediately for
   the channels the owner checks.
2. **The digest arrives with a failure banner** for a specific source —
   window-mode partial failures are deliberately visible in the delivered
   content itself (README, `digest/main.py`), not just in logs.
3. **`systemctl status <unit>`** or `journalctl` on the VM, checked by hand,
   shows a failed run and its log lines (including
   `run_summary`/`digest_delivery`) if the owner goes looking.
4. **An automated alert**, if the homelab Loki/Grafana pipeline that this app's
   log lines are designed for is actually wired up and firing — status not
   confirmed here.

There is no mechanism in this repository that pushes a failure notification to
the owner beyond the digest content itself and whatever the host-level pipeline
in (4) does.

### Gaps — explicitly not covered

- **No metrics.** No counters, gauges, or histograms; no request-rate, latency,
  or resource-usage signal (there is no request rate — see
  [availability.md](../reliability/availability.md)).
- **No tracing.** A single sequential process per run has little need for
  distributed tracing, but there is also no correlation ID threading a run's log
  lines together beyond timestamp proximity in the same journal stream.
- **No dashboard.** No Grafana panel, Loki dashboard, or equivalent is known to
  exist for this specific service in either repository.
- **No confirmed alert.** See above — the code assumes one exists; its current
  existence and correctness cannot be verified from this repository.
- **No log retention policy owned by this system.** Retention of the
  journald/Loki data (if any) is entirely a function of the host's own
  configuration, not this application.
- **Per-collector failures are not surfaced anywhere except the log lines and
  the delivered digest's own banner.** There is no running count of "how often
  has collector X failed this month."
- **No business-level metrics** (item volume trends, model cost per run,
  fallback-chain trigger rate) are tracked or exported anywhere, despite
  [QA-07](../requirements/quality-attributes.md) naming cost as a quality
  attribute and [RISK-004](../risks/architecture-risks.md) tracking the fallback
  chain's unbounded-cost exposure — that exposure is currently invisible until
  the owner notices a bill.

### Where this fits

See [deployment-architecture.md](../deployment/deployment-architecture.md) for
where the container and its logs physically run, and
[availability.md](../reliability/availability.md) for how the absence of
automated alerting interacts with "a missed run is tolerable, a duplicate
delivery is not."
