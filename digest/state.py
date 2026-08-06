"""SQLite state: schema init, item/cursor persistence, digest bookkeeping.

Stdlib sqlite3 only — no ORM. See PLAN.md §4.1 for the schema and the
idempotency contract this module must uphold.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL CHECK (source IN ('telegram', 'x', 'news', 'polymarket')),
    source_id   TEXT NOT NULL,
    chat_id     TEXT,
    chat_title  TEXT,
    author      TEXT,
    text        TEXT,
    url         TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    digest_id   INTEGER REFERENCES digests(id),
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS digests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    item_count      INTEGER NOT NULL,
    email_sent      INTEGER NOT NULL DEFAULT 0,
    site_published  INTEGER NOT NULL DEFAULT 0,
    telegram_sent   INTEGER NOT NULL DEFAULT 0,
    body_md         TEXT NOT NULL,
    body_md_hu      TEXT
);

CREATE TABLE IF NOT EXISTS cursors (
    source        TEXT NOT NULL CHECK (source IN ('telegram', 'x', 'news')),
    scope         TEXT NOT NULL,
    last_seen_id  TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (source, scope)
);

-- Polymarket collector's own state axis (digest/collectors/polymarket.py) --
-- NOT a cursor: a market has no "since" pagination contract the way a
-- Telegram chat or the X notifications timeline does, so this is a plain
-- per-market anchor table instead of a cursors row. `probability` is the
-- last REPORTED probability (the swing anchor an incoming observation is
-- compared against -- see polymarket.py's module docstring, "The anchor
-- rule"); it only changes on a first sighting or a confirmed swing.
-- `updated_at` is the last OBSERVED time (bumped every run the market is
-- still among the top-N fetched, whether or not it swung) -- it is what the
-- 30-day prune in commit_new_items compares against, so a market that drops
-- out of the top-N (resolved, delisted, fell in volume) ages out instead of
-- accumulating forever. `question` is refreshed alongside `probability` on
-- every observation, purely so a prune-surviving row's `question` doesn't
-- go stale; it plays no role in swing detection.
CREATE TABLE IF NOT EXISTS polymarket_probs (
    market_id   TEXT PRIMARY KEY,
    probability REAL NOT NULL,
    question    TEXT,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_digest_id ON items(digest_id);
"""

# How long a polymarket_probs row survives without being observed again
# before commit_new_items prunes it (see that function's docstring). 30 days
# comfortably outlives this collector's 3-hourly run cadence many times
# over, so only a market that has genuinely resolved/delisted/fallen out of
# the top-N for a full month ages out -- not a market that merely missed a
# handful of runs.
_POLYMARKET_PROB_PRUNE_DAYS = 30


@dataclass(frozen=True)
class Item:
    source: str  # "telegram" | "x" | "news"
    # telegram: "{chat_id}:{msg_id}" (chat-scoped composite — msg ids repeat across chats);
    # x: bare tweet id (globally unique)
    # news: the feed entry's own GUID (entry.id), falling back to the entry's URL
    source_id: str
    chat_id: str | None
    author: str | None
    text: str
    url: str
    fetched_at: str  # ISO8601 UTC
    # Human-readable chat/group name (Telegram entity.title), when known.
    # Nullable and LAST with a default so every existing keyword-based
    # Item(...) construction across the codebase (collectors, tests) keeps
    # working unchanged. None for chats where the entity carries no title
    # (e.g. a DM) and for rows collected before this field existed.
    chat_title: str | None = None


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating parent dirs as needed) a SQLite connection with FK enforcement."""
    path = Path(db_path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if it doesn't already exist. Safe to call repeatedly."""
    conn.executescript(_SCHEMA)
    conn.commit()
    _migrate_add_body_md_column(conn)
    _migrate_add_chat_title_column(conn)
    _migrate_expand_source_check_for_news(conn)
    _migrate_expand_source_check_for_polymarket(conn)
    _migrate_add_site_published_column(conn)
    _migrate_add_telegram_sent_column(conn)
    _migrate_add_body_md_hu_column(conn)


def _migrate_add_body_md_column(conn: sqlite3.Connection) -> None:
    """Backfill `digests.body_md` on databases created before Phase 2.

    Phase 1 (pre-summarizer) created `digests` without `body_md`.
    `CREATE TABLE IF NOT EXISTS` in _SCHEMA never alters an existing table,
    so an upgraded Phase-1 database would otherwise be missing this column
    and every run would crash in get_pending_digest() with
    "sqlite3.OperationalError: no such column: body_md". This migration is
    idempotent: it only runs the ALTER TABLE when the column isn't present.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(digests)").fetchall()}
    if "body_md" not in columns:
        conn.execute("ALTER TABLE digests ADD COLUMN body_md TEXT NOT NULL DEFAULT ''")
        conn.commit()


def _migrate_add_chat_title_column(conn: sqlite3.Connection) -> None:
    """Backfill `items.chat_title` on databases created before this column existed.

    `CREATE TABLE IF NOT EXISTS` in _SCHEMA never alters an existing table,
    so an upgraded pre-chat_title database would otherwise be missing this
    column and every insert/select touching it would crash with
    "sqlite3.OperationalError: no such column: chat_title". This migration is
    idempotent: it only runs the ALTER TABLE when the column isn't present.
    Nullable, no non-empty default: chat_title is genuinely unknown for rows
    collected before this column existed.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
    if "chat_title" not in columns:
        conn.execute("ALTER TABLE items ADD COLUMN chat_title TEXT")
        conn.commit()


def _migrate_add_site_published_column(conn: sqlite3.Connection) -> None:
    """Backfill `digests.site_published` on databases predating the multi-channel delivery refactor.

    Same idempotent ALTER-TABLE-ADD-COLUMN pattern as
    `_migrate_add_body_md_column`/`_migrate_add_chat_title_column` above --
    `CREATE TABLE IF NOT EXISTS` never alters an existing table, so an
    upgraded pre-refactor database would otherwise be missing this column
    and every read/write touching it would crash with
    "sqlite3.OperationalError: no such column: site_published". `DEFAULT 0`
    means every pre-existing row is treated as "not yet published to the
    site" -- correct, since the site channel didn't exist when those rows
    were written, so they are exactly as pending on it as a brand-new row
    with the channel enabled.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(digests)").fetchall()}
    if "site_published" not in columns:
        conn.execute("ALTER TABLE digests ADD COLUMN site_published INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _migrate_add_telegram_sent_column(conn: sqlite3.Connection) -> None:
    """Backfill `digests.telegram_sent` on databases predating the multi-channel delivery refactor.

    Sibling of `_migrate_add_site_published_column` immediately above --
    see its docstring for the full rationale (identical pattern, identical
    "pre-existing rows default to not-yet-delivered" reasoning), just for
    the Telegram channel's own flag instead.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(digests)").fetchall()}
    if "telegram_sent" not in columns:
        conn.execute("ALTER TABLE digests ADD COLUMN telegram_sent INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _migrate_add_body_md_hu_column(conn: sqlite3.Connection) -> None:
    """Backfill `digests.body_md_hu` on databases predating the Hungarian translation feature.

    Same idempotent ALTER-TABLE-ADD-COLUMN pattern as
    `_migrate_add_site_published_column`/`_migrate_add_telegram_sent_column`
    immediately above -- `CREATE TABLE IF NOT EXISTS` never alters an
    existing table, so an upgraded pre-translation database would otherwise
    be missing this column and every read/write touching it would crash with
    "sqlite3.OperationalError: no such column: body_md_hu". Nullable, no
    default value: unlike `site_published`/`telegram_sent` (booleans with an
    obvious "not yet done" default of 0), a missing translation has no
    equivalent sentinel -- NULL means exactly what it means for a
    freshly-created row with TRANSLATE_HU_ENABLED off or a failed
    translation: no Hungarian text exists for this digest, full stop.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(digests)").fetchall()}
    if "body_md_hu" not in columns:
        conn.execute("ALTER TABLE digests ADD COLUMN body_md_hu TEXT")
        conn.commit()


def _table_ddl(conn: sqlite3.Connection, table_name: str) -> str | None:
    """The CREATE TABLE statement SQLite stored for `table_name`, or None if it doesn't exist."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    return row[0] if row is not None else None


def _migrate_expand_source_check_for_news(conn: sqlite3.Connection) -> None:
    """Rebuild `items`/`cursors` so their `source` CHECK constraint accepts 'news'.

    A database deployed before this migration was created with the
    two-value `CHECK (source IN ('telegram', 'x'))` (see _SCHEMA's history:
    `_migrate_add_body_md_column`/`_migrate_add_chat_title_column` are the
    same kind of upgrade-in-place problem, but ALTER TABLE ADD COLUMN can't
    help here -- SQLite has no `ALTER TABLE ... ALTER CONSTRAINT`, so a
    table with the old CHECK baked in can only be widened by rebuilding it:
    create a new table with the wider CHECK, copy every row across, drop
    the old table, and rename the new one into its place.

    Idempotent per table: each rebuild first reads the table's OWN stored
    DDL back from `sqlite_master` (`_table_ddl`) and does nothing if it
    already mentions `'news'` -- this single check covers both "already
    migrated" (a previous init_db call already rebuilt it) and "freshly
    created by _SCHEMA above" (which already declares the three-value
    CHECK), so there is no separate first-run flag to track.

    MUST run AFTER `_migrate_add_body_md_column`/`_migrate_add_chat_title_column`
    (see init_db's call order): the explicit column list this rebuild uses
    for `items` (`id, source, source_id, chat_id, chat_title, author, text,
    url, fetched_at, digest_id`) assumes `items` already has exactly
    _SCHEMA's current column SET -- the older migrations guarantee the SET,
    not the ORDER: `ALTER TABLE ADD COLUMN` always appends, so a database
    that predates the chat_title column carries it in a DIFFERENT physical
    position than a table built fresh from _SCHEMA, permanently. That is
    exactly why the copy below names its columns explicitly on BOTH sides
    (`INSERT INTO items_new (cols...) SELECT cols... FROM items`): named
    columns match by name, making physical order irrelevant -- whereas a
    wildcard `SELECT *` would pair columns positionally and silently
    misalign them on an append-ordered legacy table. Running this rebuild
    before the column-add migrations would still crash outright, though: a
    `chat_title` reference against a table that doesn't have the column at
    all. Hence the call-order requirement, and never `SELECT *`.

    FK note: `connect()` sets `PRAGMA foreign_keys = ON`. `items` is the
    CHILD of `digests` (`items.digest_id REFERENCES digests(id)`; nothing
    references `items` in turn), so `DROP TABLE items` plus the rename is
    safe with FK enforcement on -- no other table's rows can be left
    dangling by removing it. `cursors` has no FK relationships at all. We
    rely on this fact rather than disabling the pragma for the migration.
    """
    _rebuild_items_table_for_news(conn)
    _rebuild_cursors_table_for_news(conn)


def _rebuild_items_table_for_news(conn: sqlite3.Connection) -> None:
    """The `items` half of `_migrate_expand_source_check_for_news` -- see its docstring."""
    ddl = _table_ddl(conn, "items")
    if ddl is None or "'news'" in ddl:
        return
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(
            """
            CREATE TABLE items_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL CHECK (source IN ('telegram', 'x', 'news')),
                source_id   TEXT NOT NULL,
                chat_id     TEXT,
                chat_title  TEXT,
                author      TEXT,
                text        TEXT,
                url         TEXT NOT NULL,
                fetched_at  TEXT NOT NULL,
                digest_id   INTEGER REFERENCES digests(id),
                UNIQUE (source, source_id)
            )
            """
        )
        cur.execute(
            """
            INSERT INTO items_new
                (id, source, source_id, chat_id, chat_title, author, text,
                 url, fetched_at, digest_id)
            SELECT id, source, source_id, chat_id, chat_title, author, text,
                   url, fetched_at, digest_id
            FROM items
            """
        )
        cur.execute("DROP TABLE items")
        cur.execute("ALTER TABLE items_new RENAME TO items")
        # The index died with the old table -- CREATE TABLE doesn't resurrect
        # indexes on the table it replaces, so it must be re-created here.
        cur.execute("CREATE INDEX IF NOT EXISTS idx_items_digest_id ON items(digest_id)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _rebuild_cursors_table_for_news(conn: sqlite3.Connection) -> None:
    """The `cursors` half of `_migrate_expand_source_check_for_news` -- see its docstring."""
    ddl = _table_ddl(conn, "cursors")
    if ddl is None or "'news'" in ddl:
        return
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(
            """
            CREATE TABLE cursors_new (
                source        TEXT NOT NULL CHECK (source IN ('telegram', 'x', 'news')),
                scope         TEXT NOT NULL,
                last_seen_id  TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                PRIMARY KEY (source, scope)
            )
            """
        )
        cur.execute(
            """
            INSERT INTO cursors_new (source, scope, last_seen_id, updated_at)
            SELECT source, scope, last_seen_id, updated_at
            FROM cursors
            """
        )
        cur.execute("DROP TABLE cursors")
        cur.execute("ALTER TABLE cursors_new RENAME TO cursors")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_expand_source_check_for_polymarket(conn: sqlite3.Connection) -> None:
    """Rebuild `items` so its `source` CHECK constraint additionally accepts 'polymarket'.

    Same rebuild-in-place pattern as `_migrate_expand_source_check_for_news`
    above (see its docstring for the full rationale -- SQLite has no `ALTER
    TABLE ... ALTER CONSTRAINT`, so a CHECK can only be widened by
    rebuilding the table: create a new one with the wider CHECK, copy every
    row across by explicit column name (never `SELECT *` -- see that
    docstring's column-order warning), drop the old table, rename the new
    one into place.

    Only `items` is rebuilt here -- `cursors` is deliberately left at its
    `('telegram', 'x', 'news')` CHECK, unlike the news migration which
    widened both tables. Polymarket has no cursor axis at all (see
    digest/collectors/polymarket.py's module docstring): its state lives
    entirely in the `polymarket_probs` table above, which `_SCHEMA`'s plain
    `CREATE TABLE IF NOT EXISTS` already provisions for both fresh and
    upgraded databases -- no rebuild needed for a brand-new table.

    Idempotent via the same `_table_ddl` + substring check as the news
    migration: does nothing if `items`' own stored DDL already mentions
    'polymarket' (covers both "already migrated" and "freshly created by
    _SCHEMA above", which already declares the four-value CHECK).

    MUST run AFTER `_migrate_expand_source_check_for_news` (see init_db's
    call order): that migration's own docstring explains why the column-add
    migrations must run first (physical column order on a legacy table);
    this migration inherits the identical requirement transitively, since it
    rebuilds from whatever shape `items` is in at the time it runs, using
    the same explicit, order-independent column list.
    """
    ddl = _table_ddl(conn, "items")
    if ddl is None or "'polymarket'" in ddl:
        return
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(
            """
            CREATE TABLE items_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL
                            CHECK (source IN ('telegram', 'x', 'news', 'polymarket')),
                source_id   TEXT NOT NULL,
                chat_id     TEXT,
                chat_title  TEXT,
                author      TEXT,
                text        TEXT,
                url         TEXT NOT NULL,
                fetched_at  TEXT NOT NULL,
                digest_id   INTEGER REFERENCES digests(id),
                UNIQUE (source, source_id)
            )
            """
        )
        cur.execute(
            """
            INSERT INTO items_new
                (id, source, source_id, chat_id, chat_title, author, text,
                 url, fetched_at, digest_id)
            SELECT id, source, source_id, chat_id, chat_title, author, text,
                   url, fetched_at, digest_id
            FROM items
            """
        )
        cur.execute("DROP TABLE items")
        cur.execute("ALTER TABLE items_new RENAME TO items")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_items_digest_id ON items(digest_id)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_cursors(conn: sqlite3.Connection, source: str) -> dict[str, str]:
    """Return {scope: last_seen_id} for the given source."""
    rows = conn.execute(
        "SELECT scope, last_seen_id FROM cursors WHERE source = ?", (source,)
    ).fetchall()
    return {scope: last_seen_id for scope, last_seen_id in rows}


def get_polymarket_probs(conn: sqlite3.Connection, market_ids: Sequence[str]) -> dict[str, float]:
    """Return {market_id: probability} -- the stored swing ANCHOR for each given id.

    `probability` here is the last REPORTED value (see
    digest/collectors/polymarket.py's module docstring, "The anchor rule"),
    not the last observed one -- this is exactly the dict `collect()`'s
    swing comparison is built on. A market_id with no row at all (never
    seen, or pruned after 30 days of no observation, see
    `commit_new_items`) is simply absent from the returned dict, which
    `collect()` treats as "first sighting". Empty `market_ids` short-circuits
    to `{}` without touching the database at all.
    """
    if not market_ids:
        return {}
    placeholders = ",".join("?" for _ in market_ids)
    rows = conn.execute(
        f"SELECT market_id, probability FROM polymarket_probs WHERE market_id IN ({placeholders})",
        tuple(market_ids),
    ).fetchall()
    return {market_id: probability for market_id, probability in rows}


def commit_new_items(
    conn: sqlite3.Connection,
    items: list[Item],
    cursor_updates: dict[tuple[str, str], str],
    *,
    polymarket_prob_updates: dict[str, tuple[float, str]] | None = None,
) -> int:
    """Insert new items, advance cursors, and upsert Polymarket anchors — one transaction.

    Items are inserted with INSERT ... ON CONFLICT (source, source_id) DO
    NOTHING, deduping on that UNIQUE constraint. Every (source, scope) ->
    last_seen_id in cursor_updates is upserted with updated_at set to now
    (UTC). On any failure the whole transaction is rolled back — the cursor
    must never advance without the items that justify it (idempotency
    contract, PLAN.md §4.1).

    Returns the number of item rows actually inserted.

    Note: we deliberately use `INSERT ... ON CONFLICT (source, source_id) DO
    NOTHING` instead of `INSERT OR IGNORE`. INSERT OR IGNORE swallows *any*
    constraint violation on the row — including NOT NULL and CHECK failures
    that have nothing to do with dedup — so a malformed item (e.g. a missing
    source_id or fetched_at) would silently vanish instead of raising, while
    the cursor update still commits: permanent, undetected data loss.
    Scoping the "ignore" to the specific (source, source_id) conflict target
    means only the intended dedup case is swallowed; NOT NULL/CHECK
    violations still raise sqlite3.IntegrityError, which is caught below and
    triggers the rollback like any other failure.

    `polymarket_prob_updates` (keyword-only, default None) is an optional
    market_id -> (probability, question) mapping from
    digest/collectors/polymarket.py's `collect()`. Every entry is upserted
    into `polymarket_probs` UNCONDITIONALLY (probability and question both
    overwritten, updated_at bumped to now) — this single unconditional
    upsert is what implements BOTH halves of that module's anchor contract:
    for a first-sighting or confirmed swing, the caller passes the market's
    CURRENT probability (the anchor genuinely moves); for an
    observed-but-below-threshold market, the caller passes back the SAME
    prior probability it already had (module docstring's "the anchor rule"
    — the reported value must not move), yet the row is still touched here,
    which correctly refreshes `updated_at` ("last observed") even though
    `probability` ("last reported") ends up unchanged. Defaulting to `None`
    (rather than `{}`) means every pre-existing caller of this function
    (tests, other collectors' wiring) is completely unaffected — the
    parameter is skipped entirely, `polymarket_probs` untouched. Applied
    inside the SAME transaction as the item inserts and cursor updates, so a
    failure anywhere in that transaction rolls back the anchor upserts too
    (an item that fails to insert must never let its accompanying anchor
    move regardless).

    Baseline recordings (a market's first sighting, no accompanying item)
    flow through this exact same call with `items=[]` — an empty `items`
    list is handled gracefully above (the for-loop simply does nothing,
    `inserted` stays 0), so `commit_new_items(conn, [], {},
    polymarket_prob_updates={...})` is a fully supported call shape, not a
    special case.

    Regardless of whether `polymarket_prob_updates` is given, this same
    transaction also prunes any `polymarket_probs` row whose `updated_at` is
    older than `_POLYMARKET_PROB_PRUNE_DAYS` (30) — a market that drops out
    of the top-N fetched (resolved, delisted, fell in volume) stops being
    observed and must not accumulate in this table forever. Pruning
    unconditionally (not gated on `polymarket_prob_updates` being truthy)
    means stale rows still age out even on a run where the collector is
    disabled or happened to find nothing to update.
    """
    now = datetime.now(UTC).isoformat()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        inserted = 0
        for item in items:
            cur.execute(
                """
                INSERT INTO items
                    (source, source_id, chat_id, chat_title, author, text, url, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, source_id) DO NOTHING
                """,
                (
                    item.source,
                    item.source_id,
                    item.chat_id,
                    item.chat_title,
                    item.author,
                    item.text,
                    item.url,
                    item.fetched_at,
                ),
            )
            inserted += cur.rowcount

        for (source, scope), last_seen_id in cursor_updates.items():
            cur.execute(
                """
                INSERT INTO cursors (source, scope, last_seen_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (source, scope)
                DO UPDATE SET last_seen_id = excluded.last_seen_id,
                              updated_at = excluded.updated_at
                """,
                (source, scope, last_seen_id, now),
            )

        if polymarket_prob_updates:
            for market_id, (probability, question) in polymarket_prob_updates.items():
                cur.execute(
                    """
                    INSERT INTO polymarket_probs (market_id, probability, question, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (market_id)
                    DO UPDATE SET probability = excluded.probability,
                                  question = excluded.question,
                                  updated_at = excluded.updated_at
                    """,
                    (market_id, probability, question, now),
                )

        prune_cutoff = (datetime.now(UTC) - timedelta(days=_POLYMARKET_PROB_PRUNE_DAYS)).isoformat()
        cur.execute("DELETE FROM polymarket_probs WHERE updated_at < ?", (prune_cutoff,))

        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise


def get_unsummarized_items(
    conn: sqlite3.Connection, limit: int | None = None
) -> list[Item]:
    """Return items not yet attached to a digest, ordered by fetched_at ascending.

    `limit`, when given, caps the number of rows returned to the `limit`
    OLDEST unsummarized items (fetched_at ASC is unaffected -- the bound is
    applied via `LIMIT ?` after ordering, not by changing the order). This
    lets a caller drain a large backlog in bounded batches across multiple
    runs instead of loading everything at once (see digest/main.py's
    _MAX_ITEMS_PER_DIGEST).
    """
    query = """
        SELECT source, source_id, chat_id, chat_title, author, text, url, fetched_at
        FROM items
        WHERE digest_id IS NULL
        ORDER BY fetched_at ASC
        """
    params: tuple[int, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(query, params).fetchall()
    return [
        Item(
            source=row[0],
            source_id=row[1],
            chat_id=row[2],
            chat_title=row[3],
            author=row[4],
            text=row[5],
            url=row[6],
            fetched_at=row[7],
        )
        for row in rows
    ]


def count_unsummarized_items(conn: sqlite3.Connection) -> int:
    """Return the total number of items not yet attached to a digest."""
    return conn.execute(
        "SELECT COUNT(*) FROM items WHERE digest_id IS NULL"
    ).fetchone()[0]


def create_digest(
    conn: sqlite3.Connection,
    body_md: str,
    items: Sequence[Item],
    *,
    body_md_hu: str | None = None,
) -> int:
    """Durably record a digest and stamp its items, in one transaction.

    Inserts a `digests` row (created_at = now UTC, item_count = len(items),
    email_sent = 0, body_md, body_md_hu) and stamps exactly the given `items` snapshot
    with the new digest's id — one `UPDATE ... WHERE source = ? AND
    source_id = ? AND digest_id IS NULL` per item. We deliberately do NOT use
    an unqualified `WHERE digest_id IS NULL` update: `items` is a snapshot
    taken by an earlier call to get_unsummarized_items(), and if a new item
    is inserted between that snapshot and this transaction, an unqualified
    update would silently attach it to this digest even though the digest's
    body (already summarized before this call) never mentions it — the item
    is then permanently skipped by future summarization, and item_count would
    no longer match the number of items actually stamped. Scoping each
    update to the snapshot's own (source, source_id) guarantees only those
    items are touched. We sum the affected rowcounts and require the total to
    equal len(items); a mismatch means the snapshot is stale (an item was
    already stamped/deleted out from under us) or contains a duplicate, and
    we raise so the whole transaction rolls back rather than persisting a
    digest whose item_count disagrees with what got stamped. This must
    happen BEFORE the email send attempt (PLAN.md §4.1): if the process
    crashes after this commits but before the send confirms, the next run
    finds a pending unsent digest and retries the send instead of
    re-summarizing (avoids double-billing the Claude call). On any failure
    the whole transaction is rolled back so a digest row never exists
    without its items stamped, and vice versa.

    `body_md_hu` (keyword-only, default None) is the optional Hungarian
    translation (digest/translate.py's `translate_digest`) -- None means
    either the TRANSLATE_HU_ENABLED flag is off or the translation attempt
    for this digest failed (a soft failure, never raised back to this
    caller). Stored as-is, with no validation: by the time this function is
    called, `translate_digest` has already run the digest through the same
    validate_output/enforce_link_allowlist contract enforcement the English
    body_md was subject to, so there is nothing left for create_digest to
    check.
    """
    now = datetime.now(UTC).isoformat()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(
            """
            INSERT INTO digests (created_at, item_count, email_sent, body_md, body_md_hu)
            VALUES (?, ?, 0, ?, ?)
            """,
            (now, len(items), body_md, body_md_hu),
        )
        digest_id = cur.lastrowid
        stamped = 0
        for item in items:
            cur.execute(
                """
                UPDATE items SET digest_id = ?
                WHERE source = ? AND source_id = ? AND digest_id IS NULL
                """,
                (digest_id, item.source, item.source_id),
            )
            stamped += cur.rowcount
        if stamped != len(items):
            raise ValueError(
                f"digest stamping affected {stamped} of {len(items)} items"
            )
        conn.commit()
        return digest_id
    except Exception:
        conn.rollback()
        raise


def get_digest_item_urls(conn: sqlite3.Connection, digest_id: int) -> set[str]:
    """Return the set of item URLs stamped to the given digest.

    This is how the HTML link-provenance allowlist (digest/emailer.py's
    render_html) is recovered for a PENDING resend: on a resend, the
    original Item objects from the run that summarized and stamped this
    digest are long gone (that run already returned), but the URLs survive
    in the `items` table via the `digest_id` foreign key stamped by
    create_digest at digest-creation time -- before send_digest is ever
    called, on both the fresh-digest and pending-resend paths. So this
    query works identically for both.
    """
    rows = conn.execute(
        "SELECT url FROM items WHERE digest_id = ?", (digest_id,)
    ).fetchall()
    return {row[0] for row in rows}


def get_pending_digests(
    conn: sqlite3.Connection,
    email_enabled: bool,
    site_enabled: bool,
    telegram_enabled: bool,
) -> list[tuple[int, str, dict[str, bool]]]:
    """Return every digest with at least one ENABLED channel still undelivered, oldest first.

    Replaces the old single-channel `get_pending_digest` (kept below as a
    thin backward-compatible wrapper) now that delivery has three
    independent channels (digest/main.py's `_deliver_channels`): email
    (`email_sent`), site (`site_published`), and Telegram (`telegram_sent`).
    A digest is "pending" here iff at least one of its ENABLED channels'
    flags is still 0 -- a DISABLED channel's flag is ignored entirely, both
    for deciding pendingness and (by the caller, which only retries flags it
    reads out of the returned `done` map) for retries. This is what keeps
    turning a channel off from making its old unset-flag rows eternally
    pending: e.g. a digest sent by email before EMAIL_ENABLED was ever
    turned off has `email_sent = 0` forever, but with `email_enabled=False`
    that flag is never consulted, so the row is not pending unless some
    OTHER enabled channel is also incomplete.

    Each returned tuple is `(digest_id, body_md, done)`, where `done` is a
    `{"email": bool, "site": bool, "telegram": bool}` map of the digest's
    ACTUAL stored flags (not filtered by which channels are enabled) --
    `_deliver_channels` needs the raw per-channel completion state to know
    which of the enabled-and-incomplete channels to attempt, and passing the
    unfiltered map (rather than pre-masking it here) keeps this function a
    pure read of stored state, with the enabled/disabled policy decision
    left entirely to the caller.

    Ordered oldest first (`ORDER BY id ASC`) -- unlike the old
    `get_pending_digest`'s "newest unsent wins" ordering, multiple pending
    digests here must be retried in the order they were created, since a
    later digest's "recently covered" continuity context
    (digest/summarize.py's format_recent_coverage) depends on earlier ones
    having already gone out.

    If none of the three channels are enabled, the WHERE clause would be
    empty (no channel to check pendingness against); returns `[]`
    immediately in that case without querying -- this should never actually
    happen in production, since `Config.from_env` raises a ConfigError when
    every channel is disabled, but it keeps this function's own contract
    total rather than relying on that caller-side guarantee.
    """
    conditions = []
    if email_enabled:
        conditions.append("email_sent = 0")
    if site_enabled:
        conditions.append("site_published = 0")
    if telegram_enabled:
        conditions.append("telegram_sent = 0")
    if not conditions:
        return []

    where_sql = " OR ".join(conditions)
    rows = conn.execute(
        "SELECT id, body_md, email_sent, site_published, telegram_sent "
        f"FROM digests WHERE {where_sql} ORDER BY id ASC"
    ).fetchall()
    return [
        (
            digest_id,
            body_md,
            {
                "email": bool(email_sent),
                "site": bool(site_published),
                "telegram": bool(telegram_sent),
            },
        )
        for digest_id, body_md, email_sent, site_published, telegram_sent in rows
    ]


def get_pending_digest(conn: sqlite3.Connection) -> tuple[int, str] | None:
    """Return (id, body_md) of the newest unsent digest, or None if none is pending.

    Thin backward-compatible wrapper around `get_pending_digests`, kept for
    any single-channel-only caller (and the pre-multi-channel test suite)
    that only ever cared about email. Defined with `site_enabled=False,
    telegram_enabled=False` so "pending" here means EXACTLY `email_sent =
    0`, byte-for-byte the same condition this function checked before the
    multi-channel refactor -- site_published/telegram_sent never factor in.
    `get_pending_digests` returns oldest-first; this takes the LAST entry
    (highest id) to preserve this function's own historical "newest unsent
    wins" contract. digest/main.py's `_deliver` no longer calls this
    directly -- it calls `get_pending_digests` for all three channels.
    """
    pending = get_pending_digests(
        conn, email_enabled=True, site_enabled=False, telegram_enabled=False
    )
    if not pending:
        return None
    digest_id, body_md, _done = pending[-1]
    return (digest_id, body_md)


def mark_digest_sent(conn: sqlite3.Connection, digest_id: int) -> None:
    """Flip a digest's email_sent flag to 1 after SMTP confirms delivery."""
    conn.execute("UPDATE digests SET email_sent = 1 WHERE id = ?", (digest_id,))
    conn.commit()


def mark_digest_site_published(conn: sqlite3.Connection, digest_id: int) -> None:
    """Flip a digest's site_published flag to 1 after publish_to_site confirms the PUT."""
    conn.execute("UPDATE digests SET site_published = 1 WHERE id = ?", (digest_id,))
    conn.commit()


def mark_digest_telegram_sent(conn: sqlite3.Connection, digest_id: int) -> None:
    """Flip a digest's telegram_sent flag to 1 after send_telegram_tldr confirms delivery."""
    conn.execute("UPDATE digests SET telegram_sent = 1 WHERE id = ?", (digest_id,))
    conn.commit()


def get_recent_digests(conn: sqlite3.Connection, since_iso: str) -> list[tuple[str, str]]:
    """Return (created_at, body_md) for every digest created at or after `since_iso`, newest first.

    Feeds digest/summarize.py's format_recent_coverage, which is how the
    summarizer learns what it already told the reader in the last 24 hours
    (see that function's docstring for the "recently covered" prompt
    feature this supports).

    Deliberately does NOT filter on `email_sent`: a digest row with
    email_sent = 0 is not abandoned content -- it is either about to be
    retried by _deliver's pending-resend path (see digest/main.py) or was
    already retried and delivered by the time this query runs on a later
    invocation. Either way, that digest's body reaches the reader, so its
    headings are exactly as much "already covered" as a digest that shows
    email_sent = 1 here. Filtering on email_sent = 1 would let the
    summarizer re-explain a story that is sitting in a pending-resend
    digest the reader is about to receive (or already has).

    `since_iso` is compared lexicographically against `created_at` in SQL,
    which is safe here because both are ISO8601 UTC strings produced by
    `datetime.isoformat()` (see create_digest): ISO8601's fixed-width,
    most-significant-field-first layout makes lexicographic order and
    chronological order coincide, so no parsing is needed to filter
    correctly in the query itself.
    """
    rows = conn.execute(
        "SELECT created_at, body_md FROM digests WHERE created_at >= ? ORDER BY created_at DESC",
        (since_iso,),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]
