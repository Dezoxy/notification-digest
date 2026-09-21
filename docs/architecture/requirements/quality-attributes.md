## Quality attributes

What "good" means here, and how it is measured. Where a target has never been
measured, the row says so: a target is not a result.

| ID | Attribute | Target | Evidence |
|---|---|---|---|
| QA-01 | **Idempotency** — a re-run over the same window must not duplicate items or re-deliver a digest | Absolute. This is a repository hard rule, not a goal | Enforced by the per-source cursor contract in the state database and covered by the test suite |
| QA-02 | **Freshness** — a digest covers the window that just closed | A run starts on its timer and delivers within the same run | Timer schedule is declared in the homelab role; end-to-end latency has **not** been measured |
| QA-03 | **Silence when nothing happened** | The positions lane sends nothing when no tracked project moved materially | Implemented as an explicit NO-SIGNAL contract in its prompt |
| QA-04 | **Summarization availability** — a model refusal or outage must not lose a window | The run falls back through a model chain and records which model produced the digest | Fallback chain and per-digest model provenance are implemented ([ADR 2](../decisions/0002-summarizer-fallback-chain.md)) |
| QA-05 | **Recoverability** — the state database survives loss of the VM | RPO ≤ 24h, from the daily 04:15 UTC snapshot; RTO is manual and unmeasured | Backups run and have been restore-tested once ([backup strategy](../reliability/backup-strategy.md)) |
| QA-06 | **Run availability** — a missed run is tolerable, a duplicate delivery is not | No target for uptime. QA-01 outranks this row wherever the two compete | Runs are serialized by a host-level lock so two never share one account session |
| QA-07 | **Cost** — summarization spend stays bounded per run | No hard cap is enforced in code | The fallback chain can only add cost; this is an accepted, unmeasured exposure ([RISK-004](../risks/architecture-risks.md)) |
