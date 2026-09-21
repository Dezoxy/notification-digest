## Data Architecture

### Stores

| Store | Purpose | Technology | Consistency |
|---|---|---|---|
| State Database (`state.db`) | System of record: collected items, per-source cursors, digests, delivery flags, and every derived artifact (deltas, arc keys, arc-context primers) | SQLite file on the VM's local ext4 (never the virtiofs share — SQLite fails to open there) | Strong, single writer — one Digest Runner process at a time, serialized by a host-level `flock` wrapper ([QA-06](../requirements/quality-attributes.md)) |
| Site Database | Rendering copy of published digests and push subscriptions, serving the public News Site | Cloudflare D1 | Eventual from the VM's perspective — populated only by `PUT /ingest/:id`; disposable and rebuildable from `state.db` in principle ([A-03](../requirements/assumptions.md), not yet tested end to end) |

Modelled in [`../model/containers.dsl`](../model/containers.dsl) (the
"Layer Data,Database" containers) and
[`../model/deployment.dsl`](../model/deployment.dsl) (placement).

### Schema, conceptually

`state.db` (`digest/state.py`):

```text
items        one row per collected item, UNIQUE(source, source_id)
             -- the idempotency key: a re-collected item upserts nothing, it
             -- simply fails the uniqueness check and is skipped.
digests      one row per delivered (or pending) digest: kind (window/daily/
             weekly/patreon/positions/positions-quiet), body_md(+_hu), the
             per-channel sent flags (email_sent/site_published/telegram_sent),
             and optional provenance (which model/effort produced it).
cursors      PRIMARY KEY (source, scope) -> last_seen_id. The resume point
             every collector reads before fetching and advances only after
             a successful commit.
polymarket_probs   one row per tracked market: NOT a cursor (a market has no
             "since" pagination) -- a swing-detection anchor keyed on
             market_id, pruned after 30 days unobserved.
deltas       PRIMARY KEY (digest_id, slug) -- "what changed" for a
             delta-only story update within one digest.
arc_keys     PRIMARY KEY (digest_id, slug) -- maps a digest's section to a
             stable story-arc key, when the model supplied one.
arc_context  PRIMARY KEY (key) -- ONE row per story arc (not per digest): a
             durable background primer, generated once, never regenerated.
             synced_at is NULL until the News Site has confirmed it, which
             is what lets a re-publish send only the primers the site
             doesn't have yet, instead of a full snapshot every time.
```

`items.digest_id` (nullable, `REFERENCES digests(id)`) is the join that marks
an item as summarized; `digests.kind` distinguishes the six kinds of digest
this system produces (see `PLAN.md` §2–4 for what each mode does).

Cloudflare D1 (`workers/news-site/schema.sql`) is a **separate schema that
happens to share table names with `state.db`** — the single easiest way to
confuse the two stores. Its `digests` table holds only what a reader needs:
`tldr`, `body_html`/`body_md`(+`_hu`), counts, `kind`, and the same optional
`topics`/`deltas`/`provenance` JSON columns, upserted whole by id
(`ON CONFLICT(id) DO UPDATE`). It has no `items`, `cursors`, or
`polymarket_probs` table at all — those exist only because the Digest Runner
needs them to decide what is new; a reader never needs to see uncollated raw
items. D1 additionally holds `push_subscriptions` and `push_sent`, which have
no `state.db` counterpart at all, because push delivery is a site-only
concern.

### The idempotency contract

A re-run over the same window must never duplicate items or re-deliver a
digest ([QA-01](../requirements/quality-attributes.md)) — the one absolute
guarantee this system makes. It is built from three independent mechanisms,
not one:

1. **Storage-level uniqueness.** `items.UNIQUE(source, source_id)` makes
   re-collecting an already-seen item a no-op at the database layer, not
   something application logic has to remember to check.
2. **Cursor advancement only after commit.** Each `cursors` row advances in
   the same transaction that commits the items it made visible — a crash
   between fetch and commit leaves the cursor exactly where it was, so the
   next run re-fetches (and the uniqueness constraint above absorbs the
   duplicates) rather than skipping anything.
3. **Idempotent delivery upsert.** `PUT /ingest/:id` on the News Site is an
   upsert by id; a retried publish (`get_pending_digests` /
   `deliver_pending` in `digest/state.py`) overwrites the same row rather
   than creating a second one, and `push_sent`'s claim-once insert
   (`INSERT OR IGNORE`) makes even the notification fan-out at-most-once
   against that same retry.

### Retention and pruning

| What | Cutoff | Mechanism |
|---|---|---|
| `items` rows whose digest has fully delivered | 90 days after `fetched_at` (`_ITEMS_PRUNE_DAYS`) | `prune_delivered_items` |
| `items` rows that never got summarized at all | 14 days after `fetched_at` (`_STALE_UNSUMMARIZED_PRUNE_DAYS`) | `prune_stale_unsummarized` |
| `polymarket_probs` rows | 30 days without a fresh observation (`_POLYMARKET_PROB_PRUNE_DAYS`) | Runs inside `commit_new_items`'s transaction |
| `digests`, `cursors`, `deltas`, `arc_keys`, `arc_context` | **Not pruned.** No deletion path exists for any of these tables | — |

The 90-day items cutoff is sized against the longest read path that still
needs item text: a resend of a digest stuck pending for weeks, and a daily
brief's 24-hour lookback (`DAILY_LOOKBACK_WINDOW`) — both comfortably inside
90 days. `digests` rows growing forever is a deliberate consequence of
`state.db` being the system of record for content the public site is meant
to serve indefinitely, not an oversight; nothing in this repository currently
bounds that table's size.

### Content-integrity, not just storage

Two checks run between a model's output and a stored/published digest body,
described fully in
[../security/security-architecture.md](../security/security-architecture.md#content-integrity-control-the-link-allowlist):
HTML sanitization (`nh3`) and an anchor allowlist
(`_enforce_anchor_provenance`) that strips any link the model produced that
doesn't correspond to an actually-collected item URL for that digest (or,
for a daily/weekly synthesis, one re-derived transitively from the window
digests underneath it — `get_daily_allowed_urls` /
`get_weekly_allowed_urls`). This is a data-architecture concern as much as a
security one: it is what keeps a digest body's outbound links traceable back
to `items` rows that actually exist.

### state.db vs. D1 — which one is authoritative

`state.db` on the VM is the **system of record**. Cloudflare D1 is a
**rendering copy**, populated only by the Digest Runner's own `PUT
/ingest/:id` calls and never written to by anything else. In principle a
lost or corrupted D1 database can be rebuilt by re-publishing every digest
from `state.db` — that is the entire justification for treating D1 as
disposable — but this rebuild path has never been exercised end to end
([A-03](../requirements/assumptions.md)); its correctness is assumed, not
demonstrated.

See [data-ownership.md](data-ownership.md) for who is allowed to read or
delete which side of this split.
