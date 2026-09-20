# 3. Keep SQLite on the VM as the system of record and treat Cloudflare D1 as a disposable rendering copy

Date: 2026-09-20

## Status

Accepted

## Context

This service runs on a single homelab VM ([C-05](../requirements/constraints.md))
and persists all durable state — items, cursors, digests, deltas, arc keys,
arc-context primers — in a local SQLite file, `state.db`
(`digest/state.py`; `STATE_DB_PATH`, default `./state.db`; PLAN.md's
architecture diagram places it at `/srv/appdata/digest/state.db` on the VM).
The public news site (`workers/news-site/`) is a separate Cloudflare Worker
backed by its own D1 database (`workers/news-site/schema.sql`,
`workers/news-site/migrations/`), fed by `digest/deliver.py`'s
`_deliver_site` PUT calls (`publish_to_site`).

D1's `schema.sql` declares tables named `digests` and `arc_context` — the
same names `digest/state.py`'s SQLite schema uses for its own `digests` and
`arc_context` tables, even though the two databases live in different
systems with different schemas and different lifecycles. This is confirmed
by direct comparison of `digest/state.py`'s `_SCHEMA` and
`workers/news-site/schema.sql`.

## Decision drivers

- [QA-01](../requirements/quality-attributes.md): idempotency is an absolute
  repository hard rule, enforced by the SQLite cursor contract.
- [QA-05](../requirements/quality-attributes.md): recoverability — the state
  database must survive loss of the VM (RPO ≤ 24h from the daily 04:15 UTC
  snapshot per that row; RTO is manual and unmeasured).
- [A-03](../requirements/assumptions.md): the site database is assumed
  disposable and rebuildable from the VM at any time — an assumption this
  ADR's decision depends on and that is itself flagged as currently
  untested end to end ([TD-002](../risks/technical-debt.md), if that ID
  maps to this concern in the version of that register merged alongside
  this ADR — not independently confirmed at the time of writing).

## Considered options

1. Treat D1 as the system of record and have the VM read back from it.
2. Run two independent, unsynchronized databases with no defined direction
   of truth.
3. Keep SQLite on the VM as the sole system of record; treat D1 purely as a
   rendering copy the Worker serves from, rebuildable from the VM's own
   published history.

## Decision

`state.db` on the VM is the system of record. D1 is written to only via
`_deliver_site`'s PUT payloads (`digest/deliver.py`) and is never read back
into the digest pipeline — the digest process has no code path that queries
the news site's D1 database. The Worker's own `src/ingest.js` upserts by key
(`digest_id`, `key` for `arc_context`), so a re-delivery after a failed
publish is harmless (`digest/deliver.py`'s `_deliver_site` docstring). The
direction of truth is one-way, VM → D1, always.

The identical table names (`digests`, `arc_context`) between the two schemas
are a naming coincidence carried over from the site payload's shape, not a
shared database or a foreign-key relationship — the two are never joined or
queried across systems.

`digests.site_published`/`arc_context.synced_at` on the VM side track
delivery state one-way; there is no column anywhere that reflects D1's
state back into SQLite. The documented manual escape hatch for a D1 rebuilt
from empty is `UPDATE arc_context SET synced_at = NULL` on the VM's own
`state.db`, which makes the next publish re-send every primer
(`digest/state.py`'s `arc_context` table comment) — recovery flows from the
system of record outward, never the other way.

## Consequences

Positive:

- A D1 rebuild (schema change, corruption, accidental drop) is recoverable
  by re-publishing from the VM; no digest content is lost, since SQLite
  never depended on D1 for anything.
- The idempotency contract ([QA-01](../requirements/quality-attributes.md))
  only has to hold in one place (SQLite's cursor/`UNIQUE` constraints); D1's
  own upsert-by-key behavior only has to be idempotent against re-delivery,
  not against a second source of truth.
- Confusable table names notwithstanding, there is exactly one place an
  operator needs to look to know what actually happened: `state.db`.

Negative / accepted trade-offs:

- The identical table names across two unrelated schemas
  (`digests`, `arc_context`) is a standing hazard for anyone debugging by
  name alone — a query or backup script pointed at the wrong database would
  not fail loudly, it would simply return different data.
- A rebuilt D1 loses every previously published digest and stays
  `site_published = 1` on the VM side regardless (per
  `digest/state.py`'s `arc_context.synced_at` migration comment) — nothing
  backfills the site automatically; recovery for already-delivered digests
  is manual.
- Recoverability end to end (VM backup → restore → D1 rebuild → re-publish)
  is asserted by [A-03](../requirements/assumptions.md) but not verified as
  a rehearsed, end-to-end procedure in this repository's own documentation
  at the time of writing — not recorded here beyond what A-03 already
  states.
- SQLite must not be placed on the VirtIO-FS share: opening a database there
  fails outright with `disk I/O error`, which looks like corruption and is
  not. Nothing in **this** repository records that constraint — it is a hard
  rule in the separate `homelab` repository, which owns the filesystem
  layout, and it is the reason the deployment model puts `state.db` on local
  ext4 rather than on the share. It matters here because the daily snapshot
  is copied *to* that share: the backup must be copied off the mount before
  SQLite can open it. Placement is enforced there, not by any code in this
  repository, so a change to the host layout can violate it without any test
  here failing.

## Risks

- Not recorded: `risks/architecture-risks.md` on this branch currently holds
  generic template content unrelated to this repository, so no digest-specific
  risk ID could be confirmed for a VM-loss or D1/state.db-divergence scenario
  at the time of writing.

## Related

- Requirements: [QA-01](../requirements/quality-attributes.md),
  [QA-05](../requirements/quality-attributes.md),
  [A-03](../requirements/assumptions.md), [C-05](../requirements/constraints.md)
- Architecture views: not recorded
- Other ADRs: none
