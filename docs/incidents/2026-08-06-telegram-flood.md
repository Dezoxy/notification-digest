# Incident: 2026-08-06 Telegram flood

## What happened

The first run after the multi-channel delivery cutover found ~50 pre-cutover
digests with `telegram_sent = 0`. The `ALTER TABLE` migration that added the
column (`digest/state.py`'s `_migrate_add_telegram_sent_column`) defaults it
to `0` for every pre-existing row — correct for the site-publish channel,
which should backfill every pending digest regardless of age, but wrong for
Telegram, a live notification channel, not an archive.

The run treated all ~50 as ordinary pending sends and worked through them
oldest-first. Telegram rate-limited the bot (HTTP 429) after ~20 messages.
The group topic ended up flooded with hours-old TL;DRs, across two
consecutive runs, both of which then exited non-zero on top of it.

## Root cause

Two things combined: (1) a column-add migration defaults old rows to
"unsent" — the only sane default for a boolean delivery flag with no prior
history — and (2) a live-ping channel (Telegram) was delivered through the
exact same unconditional pending-retry path as an archive channel (the
site), with nothing distinguishing "stale" from "just failed last run."
Neither is a bug alone; the combination is.

## Generalization

Any backlog reproduces this shape, not just a migration: a restored DB
backup, the Telegram channel re-enabled after a pause, or a long site outage
queueing up retries (site failures block Telegram too — see the ordering
note in `digest/deliver.py`'s `deliver_channels`). Any of these can hand a
live-ping channel a batch of old news to blast out in one run.

## Guards added

Both live in `digest/deliver.py`.

**GUARD 1 — freshness window** (`_TELEGRAM_MAX_AGE`, `_deliver_telegram`).
A digest older than 12h (4 digest windows at the 3-hourly cadence) is never
sent to Telegram — it's marked `telegram_sent` directly, without calling
`send_telegram_tldr`, and counts as done rather than failed. 12h is generous
for ordinary catch-up after a failed run, far below "archive-dump" territory.

**GUARD 2 — per-run 429 circuit breaker** (`TelegramRunState`,
`_deliver_telegram`). The first 429 in a run flips a shared flag; every
later Telegram send that run — across both the pending-retry pass and a
freshly-summarized digest — is skipped without being attempted. One
`TelegramRunState` instance is created per run mode (`_deliver` /
`run_daily` in `main.py`) and threaded through `deliver_pending` and
`deliver_channels` so both passes share the same breaker. Unlike GUARD 1,
this is a real failure — the digest still needs to go out, just not this
run — so the run's exit code still reflects it.

## Deliberate non-guards

Site and email are **not** windowed by age. The site is an archive and
should backfill a pending digest regardless of age — that was correct and
desirable in this very incident: only Telegram flooded, because only
Telegram is a live-ping channel. Windowing site or email would just turn
"channel was down for a while" into "some digests silently never show up."
