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
    -- Source validity is enforced in code (commit_new_items's _KNOWN_SOURCES
    -- check below), not by a CHECK here -- SQLite has no ALTER CONSTRAINT,
    -- so the old CHECK made every new source a full-table rebuild migration
    -- (three of which exist below as history/legacy-DB bootstrap:
    -- _migrate_expand_source_check_for_{news,polymarket,reddit}).
    source      TEXT NOT NULL,
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
    body_md_hu      TEXT,
    -- 'window' (the every-3-hours item digest, the only kind that ever
    -- existed before the daily-brief feature) or 'daily' (the once-a-day
    -- synthesis of a day's worth of window digests, digest/daily.py).
    -- DEFAULT 'window' means every pre-existing row -- every digest this
    -- codebase ever created before this column existed -- is correctly
    -- classified as a window digest, with no separate backfill needed.
    kind            TEXT NOT NULL DEFAULT 'window'
);

CREATE TABLE IF NOT EXISTS cursors (
    source        TEXT NOT NULL,
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

-- Delta-only "what changed" storage (PLAN.md §11.3): one row per
-- (digest, arc slug) whose window-digest story was a DELTA-ONLY update to a
-- RECENT_COVERAGE entry this run, as the model itself reported via the
-- ```deltas machine-facing block (digest/summarize.py's `extract_deltas`).
-- `slug` is the SAME fold `derive_topics`/`_slugify` apply to the digest's
-- own `## ` section headings (digest/publish.py's `map_deltas_to_slugs`
-- does the heading->slug mapping before a row ever reaches here) -- never a
-- raw heading string, so this table shares story-arc identity with `topics`
-- rather than inventing a second one. PRIMARY KEY (digest_id, slug) is what
-- makes `write_deltas`' INSERT OR REPLACE idempotent: a re-run over the
-- same window (crash-and-retry, or a caller invoking this twice for the
-- same digest) upserts in place instead of duplicating rows -- the
-- idempotency contract CLAUDE.md's hard rules require of all state here.
-- No FK to `digests(id)` -- deliberately matching this table's spec exactly
-- (PLAN.md §11.3); every row is written in the same call that already holds
-- a just-created, valid `digest_id` (see write_deltas), so the omission
-- costs nothing in practice.
CREATE TABLE IF NOT EXISTS deltas (
    digest_id  INTEGER NOT NULL,
    slug       TEXT NOT NULL,
    previously TEXT NOT NULL,
    now        TEXT NOT NULL,
    PRIMARY KEY (digest_id, slug)
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

# How long an `items` row survives after its digest has fully delivered
# before `prune_delivered_items` (below) removes it, so state.db (and its
# restic backups, see the homelab repo's deploy note) stays bounded instead
# of growing forever. 90 days comfortably outlives every read path that
# still goes back to `items` for its TEXT, not just its existence:
# get_digest_item_urls only reconstructs URLs for a digest that is still
# PENDING on some channel (a resend needs ITS OWN items, never an
# arbitrarily old digest's -- see that function's docstring), and the daily
# brief (digest/main.py's run_daily, via get_window_digests_since) only ever
# looks at a same-day 24h window. 90 days is a wide safety margin over both
# -- wide enough that a digest stuck pending for weeks (a channel outage, a
# stuck retry) still has its items around by the time delivery finally
# succeeds.
_ITEMS_PRUNE_DAYS = 90

# How far back a daily brief looks for the window digests it synthesizes,
# and -- via `get_daily_allowed_urls` below -- for the item URLs it is
# allowed to cite. This one constant defines BOTH halves of that
# relationship: digest/main.py's `run_daily` uses it (via
# `get_window_digests_since`) to pick which window digests to summarize
# into today's brief, and digest/deliver.py's `deliver_channels` uses it
# (via `get_daily_allowed_urls`) to re-derive the link-provenance allowlist
# for that same brief at delivery time. They MUST be the same window: if
# they were allowed to diverge, a daily brief could legitimately cite a URL
# that its own delivery-time allowlist doesn't cover (a window digest
# included in synthesis but excluded from the allowlist, or vice versa) --
# exactly the class of bug a single shared constant rules out by
# construction, rather than by two call sites happening to agree. Public
# (no leading underscore) because both main.py and deliver.py import it
# directly -- state.py sits below both in the import graph, so there is no
# cycle risk either way.
DAILY_LOOKBACK_WINDOW = timedelta(hours=24)

# How far back a weekly brief looks for the daily briefs it synthesizes, and
# -- via `get_weekly_allowed_urls` below -- for the item URLs it is allowed
# to cite. One editorial rung up from `DAILY_LOOKBACK_WINDOW` immediately
# above, and the same shared-constant relationship applies for the same
# reason: digest/main.py's `run_weekly` uses it (via `get_daily_digests_since`)
# to pick which daily briefs to synthesize into this week's report, and to
# bound the SAME `get_window_digests_since` call it reuses to derive the
# transitive citation allowlist (a daily brief stamps no items of its own
# either, see `get_daily_allowed_urls`'s own docstring, so the real URLs live
# one hop further down, on the WINDOW digests underneath); digest/deliver.py's
# `deliver_channels` uses it (via `get_weekly_allowed_urls`) to re-derive that
# identical allowlist at delivery time. They MUST be the same window, for the
# identical reason `DAILY_LOOKBACK_WINDOW`'s own docstring gives -- letting
# them diverge would let a weekly brief legitimately cite a URL its own
# delivery-time allowlist doesn't cover, or vice versa. 7 days (not 24 hours)
# because a weekly brief synthesizes a full week of daily briefs, one per
# day, mirroring how a daily brief synthesizes a full day of window digests.
WEEKLY_LOOKBACK_WINDOW = timedelta(days=7)

# The complete set of valid `Item.source` / cursor-source values. This is
# where source validity is enforced now that _SCHEMA's `source` columns have
# no CHECK constraint (see _SCHEMA's comment on items.source and
# commit_new_items's docstring) -- adding a new source is a one-line edit
# here instead of a full-table rebuild migration.
_KNOWN_SOURCES = frozenset({"telegram", "x", "news", "polymarket", "reddit"})


@dataclass(frozen=True)
class Item:
    source: str  # one of _KNOWN_SOURCES
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


# The schema version this code knows how to run against, tracked via
# SQLite's built-in `PRAGMA user_version` (a plain integer stored in the
# database file itself -- no extra table needed). Bump this and add an
# `if version < N: ...` migration step in init_db when the schema next
# changes; see init_db's own docstring for the full versioning scheme.
#
# Version 2 (PLAN.md §11.3): adds the `deltas` table -- see
# `_migrate_add_deltas_table` and init_db's `if version < 2:` step.
_LATEST_SCHEMA_VERSION = 2


def init_db(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema to `_LATEST_SCHEMA_VERSION`. Safe to call repeatedly.

    Version 0 (SQLite's own default for a database that has never had
    `PRAGMA user_version` set) covers two very different cases at once: a
    brand-new database with no `items` table at all, and every legacy
    database created before this versioning scheme existed. `fresh`
    distinguishes them by checking `items`' presence before _SCHEMA runs --
    a fresh DB is created directly at the latest shape by _SCHEMA and needs
    no migrations; a legacy DB gets the full probe-style bootstrap chain
    that used to run unconditionally on every init_db call (each step still
    idempotent, still individually guarded), ending with the CHECK-removal
    rebuild (`_migrate_drop_source_checks`) so an old two- or five-value-CHECK
    database ends up at the exact same checkless shape as a fresh one.

    Once at `_LATEST_SCHEMA_VERSION`, every later init_db call takes the
    fast path at the top and returns immediately -- O(1) instead of the nine
    schema probes (`PRAGMA table_info` / `sqlite_master` lookups) the old
    unconditional-chain version paid on every single call, fresh or not.

    A FUTURE schema change bumps `_LATEST_SCHEMA_VERSION` and adds one more
    `if version < N: ...` step below the legacy-bootstrap block -- no more
    probe-style "does this column/CHECK already exist" detection needed,
    since from here on every database's version is known exactly. Version 2
    (PLAN.md §11.3, `deltas` table) is the first such step: guarded by
    `if version < 2:`, not folded into the `version == 0` legacy-bootstrap
    block above it, precisely because it must also run for a database
    already sitting at version 1 (fully migrated under the ORIGINAL
    versioning scheme, before this second step existed) -- a case the
    legacy-bootstrap block's own `version == 0` guard deliberately excludes,
    since re-running nine already-satisfied idempotent probes on every
    version-1 database forever would be pure waste once every database's
    version is precisely known.

    The version is stamped via `PRAGMA user_version` only AFTER the
    migrations above it have committed -- so a crash mid-migration leaves
    the on-disk version unchanged, and the next start simply re-runs the
    (idempotent) chain from scratch rather than silently skipping it.

    Raises `RuntimeError` if the database's stored version is NEWER than
    this code's `_LATEST_SCHEMA_VERSION` -- that means a newer release of
    this codebase already upgraded the schema, and running this (older)
    code against it is undefined: better to fail loudly at startup than
    silently query/write columns this code doesn't know about.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == _LATEST_SCHEMA_VERSION:
        return
    if version > _LATEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is newer than this code's "
            f"latest known version {_LATEST_SCHEMA_VERSION} -- refusing to "
            "run older code against a database a newer release already "
            "migrated; upgrade before pointing this code at it"
        )

    fresh = _table_ddl(conn, "items") is None
    conn.executescript(_SCHEMA)
    conn.commit()
    if version == 0 and not fresh:
        # Pre-versioning database (version 0, `items` already existed): run
        # the old probe-style bootstrap chain in its historical order, all
        # of it still idempotent, ending with the CHECK removal. Gated on
        # `version == 0` (not just `not fresh`, which is all this check used
        # to be before version 2 existed): a version-1 database is already
        # fully migrated, and now that FUTURE steps genuinely know the exact
        # version, there is no reason to re-run nine already-satisfied probes
        # against it just because `items` happens to already exist.
        _migrate_add_body_md_column(conn)
        _migrate_add_chat_title_column(conn)
        _migrate_expand_source_check_for_news(conn)
        _migrate_expand_source_check_for_polymarket(conn)
        _migrate_expand_source_check_for_reddit(conn)
        _migrate_add_site_published_column(conn)
        _migrate_add_telegram_sent_column(conn)
        _migrate_add_body_md_hu_column(conn)
        _migrate_add_kind_column(conn)
        _migrate_drop_source_checks(conn)

    if version < 2:
        # PLAN.md §11.3: the `deltas` table. _SCHEMA above already created it
        # (CREATE TABLE IF NOT EXISTS) for every path that reaches this
        # point -- fresh, legacy version-0, and existing version-1 alike --
        # so this call is a documented no-op in practice; it exists so the
        # step is explicit and versioned here, matching the "one more
        # `if version < N: ...` step" pattern this docstring commits future
        # schema changes to, rather than relying solely on _SCHEMA's
        # blanket idempotent execution to carry it silently.
        _migrate_add_deltas_table(conn)

    conn.execute(f"PRAGMA user_version = {_LATEST_SCHEMA_VERSION}")
    conn.commit()


def _migrate_add_body_md_column(conn: sqlite3.Connection) -> None:
    """Backfill `digests.body_md` on databases created before Phase 2.

    Phase 1 (pre-summarizer) created `digests` without `body_md`.
    `CREATE TABLE IF NOT EXISTS` in _SCHEMA never alters an existing table,
    so an upgraded Phase-1 database would otherwise be missing this column
    and every run would crash in get_pending_digests() with
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


def _migrate_add_kind_column(conn: sqlite3.Connection) -> None:
    """Backfill `digests.kind` on databases predating the daily-brief feature.

    Same idempotent ALTER-TABLE-ADD-COLUMN pattern as
    `_migrate_add_site_published_column`/`_migrate_add_telegram_sent_column`/
    `_migrate_add_body_md_hu_column` above -- `CREATE TABLE IF NOT EXISTS`
    never alters an existing table, so an upgraded pre-daily-brief database
    would otherwise be missing this column and every read/write touching it
    would crash with "sqlite3.OperationalError: no such column: kind".
    `DEFAULT 'window'` is a correctness requirement, not just a convenient
    placeholder: every digest ever created before this column existed WAS a
    window digest (the daily kind didn't exist yet), so this default
    correctly classifies every pre-existing row with no separate backfill
    UPDATE needed.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(digests)").fetchall()}
    if "kind" not in columns:
        conn.execute("ALTER TABLE digests ADD COLUMN kind TEXT NOT NULL DEFAULT 'window'")
        conn.commit()


def _migrate_add_deltas_table(conn: sqlite3.Connection) -> None:
    """Create `deltas` on databases predating the delta-persistence feature (PLAN.md §11.3).

    Unlike every migration above (all `ALTER TABLE ADD COLUMN` on an
    existing table, since SQLite has no `ALTER TABLE ... ALTER CONSTRAINT`
    but does support adding a column in place), this one adds a whole NEW
    table with no prior shape to preserve -- so there is no rebuild-in-place
    dance, no column list, no data to copy across. `CREATE TABLE IF NOT
    EXISTS` (identical DDL to _SCHEMA's own `deltas` declaration, kept
    byte-identical so the two can never drift into two different shapes for
    the same table) is idempotent on its own: this function's own `if
    _table_ddl(...) is not None: return` guard is redundant with that IF NOT
    EXISTS in practice, but kept anyway to match this module's established
    "check before touching, not just rely on the DDL's own guard" style
    (every ALTER-COLUMN migration above checks `PRAGMA table_info` first
    rather than relying solely on try/except).
    """
    if _table_ddl(conn, "deltas") is not None:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deltas (
            digest_id  INTEGER NOT NULL,
            slug       TEXT NOT NULL,
            previously TEXT NOT NULL,
            now        TEXT NOT NULL,
            PRIMARY KEY (digest_id, slug)
        )
        """
    )
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


def _migrate_expand_source_check_for_reddit(conn: sqlite3.Connection) -> None:
    """Rebuild `items` so its `source` CHECK constraint additionally accepts 'reddit'.

    Same rebuild-in-place pattern as `_migrate_expand_source_check_for_polymarket`
    immediately above (see its docstring, and `_migrate_expand_source_check_for_news`'s
    for the full rationale -- SQLite has no `ALTER TABLE ... ALTER CONSTRAINT`,
    so a CHECK can only be widened by rebuilding the table: create a new one
    with the wider CHECK, copy every row across by explicit column name
    (never `SELECT *` -- see the news migration's docstring for why), drop
    the old table, rename the new one into place.

    Only `items` is rebuilt here -- `cursors` is deliberately left at its
    `('telegram', 'x', 'news')` CHECK, unchanged since the polymarket
    migration. The Reddit collector has no cursor axis at all (see
    digest/collectors/reddit.py's module docstring, "No cursor axis"): its
    idempotency comes entirely from `UNIQUE(source, source_id)` on `items`
    itself, exactly like rss.py's `news` source -- there is no reddit-specific
    state table the way Polymarket has `polymarket_probs`, and no reason to
    ever insert a 'reddit' row into `cursors`.

    Idempotent via the same `_table_ddl` + substring check as the news and
    polymarket migrations: does nothing if `items`' own stored DDL already
    mentions 'reddit' (covers both "already migrated" and "freshly created
    by _SCHEMA above", which already declares the five-value CHECK).

    MUST run AFTER `_migrate_expand_source_check_for_polymarket` (see
    init_db's call order): that migration's own docstring (and the news
    migration's, transitively) explains why the column-add migrations must
    run first (physical column order on a legacy table); this migration
    inherits the identical requirement, since it rebuilds from whatever
    shape `items` is in at the time it runs, using the same explicit,
    order-independent column list.
    """
    ddl = _table_ddl(conn, "items")
    if ddl is None or "'reddit'" in ddl:
        return
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(
            """
            CREATE TABLE items_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL
                            CHECK (source IN ('telegram', 'x', 'news', 'polymarket', 'reddit')),
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


def _migrate_drop_source_checks(conn: sqlite3.Connection) -> None:
    """Rebuild `items`/`cursors` to drop their `source` CHECK constraint entirely.

    Same rebuild-in-place pattern as `_migrate_expand_source_check_for_news`
    and its siblings above (see those docstrings for the full rationale --
    SQLite has no `ALTER TABLE ... ALTER CONSTRAINT`, so a CHECK can only be
    removed by rebuilding the table: create a new one without the CHECK,
    copy every row across by explicit column name (never `SELECT *` -- see
    the news migration's docstring for why), drop the old table, rename the
    new one into place.

    Unlike the three `_migrate_expand_source_check_for_*` migrations, which
    each WIDENED the CHECK to admit one more source, this one REMOVES it --
    source validity moved to code (`commit_new_items`'s `_KNOWN_SOURCES`
    check) precisely because the CHECK made every new source a full-table
    rebuild migration. This is the FINAL source-related rebuild: with the
    CHECK gone, adding a source becomes a one-line `_KNOWN_SOURCES` edit, and
    the three expand-CHECK migrations above stay only as the version-0
    bootstrap chain for databases that predate this one.

    Both `items` AND `cursors` are rebuilt here (unlike the polymarket/reddit
    migrations, which only widened `items` -- `cursors` was deliberately left
    on its three-value CHECK, since neither of those sources ever gets a
    cursor row). `cursors` still carries its original `('telegram', 'x',
    'news')` CHECK at this point in the chain, so it needs the same
    CHECK-removal treatment as `items`.

    Idempotent per table via the same `_table_ddl` + substring check as the
    migrations above, generalized from "does the DDL mention my new source"
    to "does the DDL mention the CHECK constraint syntax at all" -- does
    nothing for a table whose stored DDL no longer contains `CHECK (source`
    (covers both "already migrated" and "freshly created by _SCHEMA above",
    which no longer declares one). The check looks for `CHECK (source`, not
    a bare `CHECK`, deliberately: `sqlite_master.sql` stores a table's DDL
    verbatim, comments included, and _SCHEMA's own comment on `items.source`
    (explaining this exact history) mentions the word "CHECK" in prose -- a
    bare substring match would misfire on that comment for a table that
    never had the constraint to begin with.

    MUST run LAST in init_db's legacy-bootstrap chain (see its call order):
    it inherits the same column-order requirement as the three migrations
    above (rebuilding `items` needs `chat_title` etc. to already exist in
    SOME position -- see `_migrate_expand_source_check_for_news`'s docstring
    for why `SELECT *` would be unsafe here), and it must run after the
    news/polymarket/reddit CHECK-widening migrations so this rebuild's fixed
    checkless column list is copying from a table that already has every
    column _SCHEMA expects.
    """
    _rebuild_items_table_dropping_source_check(conn)
    _rebuild_cursors_table_dropping_source_check(conn)


def _rebuild_items_table_dropping_source_check(conn: sqlite3.Connection) -> None:
    """The `items` half of `_migrate_drop_source_checks` -- see its docstring."""
    ddl = _table_ddl(conn, "items")
    if ddl is None or "CHECK (source" not in ddl:
        return
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(
            """
            CREATE TABLE items_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL,
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


def _rebuild_cursors_table_dropping_source_check(conn: sqlite3.Connection) -> None:
    """The `cursors` half of `_migrate_drop_source_checks` -- see its docstring."""
    ddl = _table_ddl(conn, "cursors")
    if ddl is None or "CHECK (source" not in ddl:
        return
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(
            """
            CREATE TABLE cursors_new (
                source        TEXT NOT NULL,
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

    Every `item.source` in `items` and every source half of a `cursor_updates`
    key is checked against `_KNOWN_SOURCES` BEFORE the transaction even
    opens; an unrecognized value raises `ValueError` naming the offending
    source and writes nothing at all. This is the exact protection the
    dropped `source` CHECK constraint used to provide (see _SCHEMA) --
    commit_new_items is the single choke point every item/cursor row passes
    through on its way into the DB, so validating here in code preserves the
    same "malformed data must fail loudly, the cursor must not advance"
    contract described below for NOT NULL violations, just enforced before
    BEGIN instead of by SQLite mid-transaction.

    Items are inserted with INSERT ... ON CONFLICT (source, source_id) DO
    NOTHING, deduping on that UNIQUE constraint. Every (source, scope) ->
    last_seen_id in cursor_updates is upserted with updated_at set to now
    (UTC). On any failure the whole transaction is rolled back — the cursor
    must never advance without the items that justify it (idempotency
    contract, PLAN.md §4.1).

    Returns the number of item rows actually inserted.

    Note: we deliberately use `INSERT ... ON CONFLICT (source, source_id) DO
    NOTHING` instead of `INSERT OR IGNORE`. INSERT OR IGNORE swallows *any*
    constraint violation on the row — including NOT NULL failures that have
    nothing to do with dedup — so a malformed item (e.g. a missing
    source_id or fetched_at) would silently vanish instead of raising, while
    the cursor update still commits: permanent, undetected data loss.
    Scoping the "ignore" to the specific (source, source_id) conflict target
    means only the intended dedup case is swallowed; NOT NULL violations
    still raise sqlite3.IntegrityError, which is caught below and triggers
    the rollback like any other failure. (Source validity is no longer a
    CHECK/IntegrityError case at all -- see the _KNOWN_SOURCES paragraph
    above.)

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
    for item in items:
        if item.source not in _KNOWN_SOURCES:
            raise ValueError(f"unknown item source: {item.source!r}")
    for source, _scope in cursor_updates:
        if source not in _KNOWN_SOURCES:
            raise ValueError(f"unknown cursor source: {source!r}")

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
    kind: str = "window",
    item_count: int | None = None,
) -> int:
    """Durably record a digest and stamp its items, in one transaction.

    Inserts a `digests` row (created_at = now UTC, item_count = len(items)
    unless overridden -- see `item_count` below --, email_sent = 0, body_md,
    body_md_hu, kind) and stamps exactly the given `items` snapshot with the
    new digest's id — one `UPDATE ... WHERE source = ? AND
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

    `kind` (keyword-only, default "window") distinguishes the every-3-hours
    item digest (the only kind that existed before the daily-brief feature)
    from a "daily" brief (digest/daily.py's `summarize_daily`, synthesized
    from a day's worth of window digests, never from raw items). Stored
    as-is; digest/state.py's `get_recent_digests` filters on it (a daily
    row's headings must never pollute the window digests' own continuity
    memory) and digest/main.py's delivery path reads it back to pick the
    right Telegram thread (window vs daily) on both the fresh and
    pending-retry paths.

    `item_count` (keyword-only, default None) overrides the stored
    `item_count` away from `len(items)` when given. A window digest always
    wants `len(items)` (the items it actually stamps) -- the default covers
    that case with zero call-site changes. A daily brief is different: it
    stamps NO items at all (`items=[]`, since a daily brief consumes
    *digests*, not items -- see the item-stamping note above), but its
    `item_count` must still reflect something meaningful to the reader: the
    SUM of the source window digests' own item_counts, which the site's "N
    items" line and the closing-line count sanity both key off of. Passing
    that sum here is the only way to get a non-zero, non-len(items)
    `item_count` onto a kind="daily" row.
    """
    now = datetime.now(UTC).isoformat()
    stored_item_count = item_count if item_count is not None else len(items)
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(
            """
            INSERT INTO digests (created_at, item_count, email_sent, body_md, body_md_hu, kind)
            VALUES (?, ?, 0, ?, ?, ?)
            """,
            (now, stored_item_count, body_md, body_md_hu, kind),
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


def write_deltas(conn: sqlite3.Connection, digest_id: int, deltas: list[dict[str, str]]) -> None:
    """Idempotently persist this digest's delta-only "what changed" entries (PLAN.md §11.3).

    `deltas` is digest/publish.py's `map_deltas_to_slugs` output --
    `{"slug": ..., "previously": ..., "now": ...}` dicts, already mapped from
    the model's raw headings to this digest's own real topic slugs (a delta
    whose heading didn't match a real section was already dropped one layer
    up, with its own warning -- this function trusts every entry it's given).

    Written with `INSERT OR REPLACE`, keyed on the table's own PRIMARY KEY
    `(digest_id, slug)` -- the §11.3 guardrail this directly upholds: a
    re-run over the same window must not duplicate rows (CLAUDE.md's
    idempotent-state hard rule, the same contract `commit_new_items`'s
    `ON CONFLICT ... DO NOTHING` dedup and `create_digest`'s
    snapshot-scoped item stamping already uphold elsewhere in this module).
    Calling this twice for the same `(digest_id, slug)` pair leaves exactly
    one row, with the SECOND call's `previously`/`now` values winning --
    "last write wins" is the correct behavior here since there is only ever
    one legitimate writer per digest_id (this function is called exactly
    once per fresh digest, from digest/main.py's `_deliver`, immediately
    after `create_digest` -- never on the pending-resend path, which retries
    CHANNEL delivery only and never re-summarizes).

    GUARDRAIL (PLAN.md §11.3): this table is written ONLY from the WINDOW
    digest path. The daily and weekly synthesis paths (digest/daily.py's
    `summarize_daily`, digest/weekly.py's `summarize_weekly`, and their
    respective callers `run_daily`/`run_weekly` in digest/main.py) never call
    this function and never read this table -- deltas are a window-level
    concept only (RECENT_COVERAGE, the continuity list this feature reasons
    about, is itself window-only; see digest/summarize.py's
    format_recent_coverage). Keeping the write scoped to one call site, here,
    is what keeps a daily/weekly prompt build from ever being able to
    accidentally join against this table and leak delta content into a
    second, unfenced channel (the exact two-hop trap PLAN.md §11.3 names by
    reference to the weekly-synthesis-allowlist incident) -- there is
    structurally nothing here for a daily/weekly build to read even if it
    tried, since nothing in digest/daily.py or digest/weekly.py imports this
    function or queries this table.

    Silently returns without opening a transaction when `deltas` is empty --
    the common case (most digests have zero delta-only stories in a given
    window), and an empty list has nothing to write.

    Rolls back the whole batch on any failure, matching every other
    multi-row write in this module (`commit_new_items`, `create_digest`):
    a partially-written batch of deltas for one digest is worse than none at
    all, since a caller re-deriving `deltas` from the same `body_md` on a
    hypothetical retry would expect either all of them present or none.
    """
    if not deltas:
        return
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        for entry in deltas:
            cur.execute(
                """
                INSERT OR REPLACE INTO deltas (digest_id, slug, previously, now)
                VALUES (?, ?, ?, ?)
                """,
                (digest_id, entry["slug"], entry["previously"], entry["now"]),
            )
        conn.commit()
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


def get_daily_allowed_urls(conn: sqlite3.Connection, created_at: str) -> set[str]:
    """Return the link-provenance allowlist for a DAILY brief, re-derived from the DB.

    A "daily" digest (digest/daily.py's `summarize_daily`) stamps NO items of
    its own -- it consumes a day's worth of window digests, never raw items
    (see `create_digest`'s `kind` docstring) -- so `get_digest_item_urls`
    against a daily digest_id always returns the empty set. Before this
    function existed, digest/deliver.py's `deliver_channels` derived its
    link-provenance allowlist the SAME way for every digest kind, which for
    a daily brief meant an empty allowlist and every citation link defanged
    to plain text on every channel (site, email, and the Hungarian
    translation alike) -- this function exists to fix exactly that.

    The allowlist is the union of item URLs across every WINDOW digest
    created within `DAILY_LOOKBACK_WINDOW` (24h) before `created_at`:

        SELECT DISTINCT i.url FROM items i JOIN digests d ON i.digest_id = d.id
        WHERE d.kind = 'window' AND d.created_at >= ? AND d.created_at < ?

    with bounds = [created_at - DAILY_LOOKBACK_WINDOW, created_at) -- one
    query, no per-digest loop, mirroring `get_window_digests_since`'s own
    `kind = 'window'` scoping (a daily row must never feed into, or here
    license the citations of, another daily row).

    Re-deriving from the DB by `created_at` (rather than requiring the
    caller to thread the original run's `allowed_urls` set through) is what
    makes this work IDENTICALLY on a fresh delivery (main.py's `run_daily`,
    moments after `create_digest`) and on a pending resend in a much later
    run (`deliver_pending`, which by then has only the digest_id and its
    stored `created_at` to go on) -- the same reason `get_digest_item_urls`
    exists for window digests in the first place, see its own docstring.

    The upper bound (`< created_at`, strict) is what makes a RESEND
    deterministic. Without it, a window digest created AFTER this brief was
    originally written -- but before a later resend attempt re-runs this
    query -- would widen the resend's allowlist beyond what the brief was
    actually written against: a resend must reproduce the exact same
    allowlist the original delivery would have used, not a bigger one just
    because more time has since passed.

    Honest caveat: `created_at` here is the daily digest's OWN stored
    `created_at`, which is within seconds of -- but not byte-for-byte
    identical to -- the `now` the original `run_daily` call used to compute
    its `since` bound for `get_window_digests_since`. A source window digest
    created within that few-second sliver right at the 24h boundary could
    therefore fall on the wrong side of one of these two independently
    computed windows. The consequence is narrow and non-crashing: at most
    one legitimately-cited URL ends up outside this re-derived allowlist, so
    its anchor gets defanged to plain text for that one citation -- not a
    crash, not a wrong-content bug elsewhere.

    Also depends on the source window digests' `items` rows still existing:
    a very late resend, after `prune_delivered_items`'s 90-day prune has
    already removed them, can thin or even empty this allowlist -- the same
    accepted trade-off already documented on `get_digest_source_counts` for
    an old digest's `source_counts`, not a new one introduced here.

    `created_at` is parsed with `datetime.fromisoformat`, naive timestamps
    treated as UTC -- the same fallback digest/deliver.py's Telegram
    freshness guard (`_deliver_telegram`) and digest/summarize.py's
    `_format_digest_age` both use. Every `created_at` this codebase writes is
    `datetime.now(UTC).isoformat()` (see `create_digest`), so the
    naive-fallback branch is defensive, not the expected path.
    """
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    window_start = created - DAILY_LOOKBACK_WINDOW
    rows = conn.execute(
        """
        SELECT DISTINCT i.url FROM items i JOIN digests d ON i.digest_id = d.id
        WHERE d.kind = 'window' AND d.created_at >= ? AND d.created_at < ?
        """,
        (window_start.isoformat(), created.isoformat()),
    ).fetchall()
    return {row[0] for row in rows}


def get_weekly_allowed_urls(conn: sqlite3.Connection, created_at: str) -> set[str]:
    """Return the link-provenance allowlist for a WEEKLY brief, re-derived from the DB.

    Sibling of `get_daily_allowed_urls` immediately above, one editorial rung
    up -- see its docstring for the shared rationale (identical here: this
    function exists to fix the identical class of bug for the "weekly" kind).
    A "weekly" digest (digest/weekly.py's `summarize_weekly`) stamps NO items
    of its own either -- it consumes a week's worth of DAILY briefs, never
    raw items or window digests directly (see `create_digest`'s `kind`
    docstring) -- so `get_digest_item_urls` against a weekly digest_id always
    returns the empty set too. But going through the DAILY rows themselves
    would be a dead end: a daily digest ALSO stamps no items of its own (see
    `get_daily_allowed_urls`'s own docstring), so `get_digest_item_urls` of a
    source daily digest is *also* always empty. The only place actual item
    URLs live is on the WINDOW digests underneath both layers -- so this
    function skips the daily layer entirely and unions item URLs across
    every WINDOW digest created within `WEEKLY_LOOKBACK_WINDOW` (7 days)
    before `created_at`:

        SELECT DISTINCT i.url FROM items i JOIN digests d ON i.digest_id = d.id
        WHERE d.kind = 'window' AND d.created_at >= ? AND d.created_at < ?

    with bounds = [created_at - WEEKLY_LOOKBACK_WINDOW, created_at) -- the
    exact same query shape `get_daily_allowed_urls` runs, just widened to a
    7-day window instead of 24 hours, mirroring `run_weekly`'s own build-time
    derivation of this identical set (digest/main.py): a weekly brief cites
    what its dailies cited, and a daily brief cites what its own windows
    cited, so a weekly brief's real allowlist is the union of window item
    URLs across the whole week -- this two-hop transitive derivation IS the
    one structural difference `run_weekly` has from `run_daily` (see that
    function's own docstring).

    Re-deriving from the DB by `created_at` (rather than requiring the
    caller to thread the original run's `allowed_urls` set through) is what
    makes this work IDENTICALLY on a fresh delivery (main.py's `run_weekly`,
    moments after `create_digest`) and on a pending resend in a much later
    run (`deliver_pending`, which by then has only the digest_id and its
    stored `created_at` to go on) -- the identical reason
    `get_daily_allowed_urls` exists in the first place, see its own
    docstring.

    The upper bound (`< created_at`, strict) is what makes a RESEND
    deterministic, for the identical reason `get_daily_allowed_urls`'s own
    docstring gives: a window digest created AFTER this brief was originally
    written -- but before a later resend attempt re-runs this query -- must
    never widen the resend's allowlist beyond what the brief was actually
    written against.

    The same honest caveats `get_daily_allowed_urls` documents apply here
    unchanged, just at the weekly boundary instead of the daily one: a
    source window digest created within the few-second sliver right at the
    7-day boundary could fall on the wrong side of two independently
    computed windows (narrow, non-crashing -- at most one legitimately-cited
    URL gets defanged to plain text for that one citation), and a very late
    resend can find the source window digests' `items` rows already pruned
    by `prune_delivered_items`'s 90-day prune, thinning or emptying this
    allowlist.

    `created_at` is parsed with `datetime.fromisoformat`, naive timestamps
    treated as UTC -- the identical fallback `get_daily_allowed_urls` uses,
    for the identical reason (every `created_at` this codebase writes is
    `datetime.now(UTC).isoformat()`, so the naive-fallback branch is
    defensive, not the expected path).
    """
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    window_start = created - WEEKLY_LOOKBACK_WINDOW
    rows = conn.execute(
        """
        SELECT DISTINCT i.url FROM items i JOIN digests d ON i.digest_id = d.id
        WHERE d.kind = 'window' AND d.created_at >= ? AND d.created_at < ?
        """,
        (window_start.isoformat(), created.isoformat()),
    ).fetchall()
    return {row[0] for row in rows}


def get_digest_source_counts(conn: sqlite3.Connection, digest_id: int) -> dict[str, int]:
    """Return {source: item count} for the given digest.

    Feeds the site channel's `source_counts` ingest field.

    A "daily" digest (digest/daily.py's `summarize_daily`) stamps no items at
    all (see create_digest's `item_count` docstring), so this returns `{}`
    for one; digest/publish.py's `publish_to_site` then omits the
    `source_counts` field entirely for an empty dict rather than sending one
    (see that function's truthy-only inclusion contract). Note also that
    `prune_delivered_items` can thin (or empty) this count for an OLD,
    already-fully-delivered digest on a late re-publish -- accepted, since
    these counts are display metadata, not the digest body itself.
    """
    rows = conn.execute(
        "SELECT source, COUNT(*) FROM items WHERE digest_id = ? GROUP BY source",
        (digest_id,),
    ).fetchall()
    return {source: count for source, count in rows}


def get_deltas(conn: sqlite3.Connection, digest_id: int) -> list[dict[str, str]]:
    """Return this digest's stored delta entries as `{"slug", "previously", "now"}` dicts.

    Feeds the site channel's optional `deltas` ingest field (PLAN.md §11.3),
    the same "read durable per-digest state back by id" pattern
    `get_digest_source_counts` (above) and `get_digest_item_urls` (below)
    already use -- and for the SAME reason `get_digest_item_urls` exists in
    the first place: the raw model output this data originally came from
    (the ```deltas fence digest/summarize.py's `extract_deltas` parses) is
    NEVER stored anywhere -- it is stripped out of `body_md` before
    `create_digest` ever persists it (see `extract_deltas`'s own docstring)
    -- so a PENDING RESEND (digest/deliver.py's `_deliver_site`, called with
    only a `digest_id` and the already-stored, already-fence-free `body_md`)
    has no way to re-derive this digest's deltas from `body_md` the way
    `derive_topics` re-derives `topics` from it. This table, written once by
    `write_deltas` right after the digest's original creation, is the only
    place this information survives -- so both the fresh-digest delivery
    path and a much-later pending resend read it back identically, from
    here, rather than the fresh path threading an in-memory list through
    that the resend path could never reconstruct.

    Ordered by `slug` for a deterministic, stable result (there is no
    natural insertion-order column on this table's own primary key
    `(digest_id, slug)` to sort by instead) -- order has no semantic meaning
    to the site's ingest payload (an array of independent per-arc entries),
    this is purely for reproducible output.

    Returns `[]` for a digest with no delta entries at all (the common case
    -- most windows have zero delta-only repeat stories), which
    digest/publish.py's `publish_to_site` then omits from the payload
    entirely rather than sending an empty array (its truthy-only inclusion
    contract, matching `source_counts`/`failed_sources`/`topics`).
    """
    rows = conn.execute(
        "SELECT slug, previously, now FROM deltas WHERE digest_id = ? ORDER BY slug",
        (digest_id,),
    ).fetchall()
    return [{"slug": slug, "previously": previously, "now": now} for slug, previously, now in rows]


def get_pending_digests(
    conn: sqlite3.Connection,
    email_enabled: bool,
    site_enabled: bool,
    telegram_enabled: bool,
) -> list[tuple[int, str, dict[str, bool], str]]:
    """Return every digest with at least one ENABLED channel still undelivered, oldest first.

    Replaces the old single-channel `get_pending_digest` (removed) now that
    delivery has three independent channels (digest/deliver.py's
    `deliver_channels`): email (`email_sent`), site (`site_published`), and
    Telegram (`telegram_sent`). A digest is "pending" here iff at least one
    of its ENABLED channels' flags is still 0 -- a DISABLED channel's flag
    is ignored entirely, both for deciding pendingness and (by the caller,
    which only retries flags it reads out of the returned `done` map) for
    retries. This is what keeps turning a channel off from making its old
    unset-flag rows eternally pending: e.g. a digest sent by email before
    EMAIL_ENABLED was ever turned off has `email_sent = 0` forever, but
    with `email_enabled=False` that flag is never consulted, so the row is
    not pending unless some OTHER enabled channel is also incomplete.

    Each returned tuple is `(digest_id, body_md, done, kind)`, where `done`
    is a `{"email": bool, "site": bool, "telegram": bool}` map of the
    digest's ACTUAL stored flags (not filtered by which channels are
    enabled) -- `deliver_channels` needs the raw per-channel completion
    state to know which of the enabled-and-incomplete channels to attempt,
    and passing the unfiltered map (rather than pre-masking it here) keeps
    this function a pure read of stored state, with the enabled/disabled
    policy decision left entirely to the caller. `kind` ("window" or
    "daily") is the digest's own stored kind -- the retry path needs it to
    pick the right Telegram thread for a pending "daily" row (see
    digest/deliver.py's `_deliver_telegram`), the same way the fresh-digest
    path already knows its own kind at creation time.

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
        "SELECT id, body_md, email_sent, site_published, telegram_sent, kind "
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
            kind,
        )
        for digest_id, body_md, email_sent, site_published, telegram_sent, kind in rows
    ]


def prune_delivered_items(
    conn: sqlite3.Connection,
    *,
    email_enabled: bool,
    site_enabled: bool,
    telegram_enabled: bool,
) -> int:
    """Delete `items` rows whose digest has fully delivered, past `_ITEMS_PRUNE_DAYS`.

    A row is eligible for deletion only when ALL of:
      - `fetched_at` is older than `_ITEMS_PRUNE_DAYS` (90) days -- see that
        constant's own docstring for why 90 is safely wider than every read
        path that still needs an old row's TEXT.
      - `digest_id IS NOT NULL` -- an item never attached to a digest is
        unsummarized BACKLOG, not delivered content, no matter how old. It
        must survive until get_unsummarized_items/create_digest eventually
        claims it, however long that takes (a stuck summarizer, a
        long-disabled collector's backlog draining slowly, etc.); pruning it
        here would silently discard content the reader was never actually
        shown.
      - its digest is NOT pending on any ENABLED channel.

    The "not pending" half is `digest_id NOT IN (SELECT id FROM digests
    WHERE <pending condition>)`, and `<pending condition>` is built with the
    EXACT same per-channel logic as get_pending_digests -- the same three
    `email_sent = 0` / `site_published = 0` / `telegram_sent = 0` fragments,
    each included only when its channel is enabled, joined with OR. This
    duplication is deliberate: pruning and pending-retry read the same three
    flag columns to answer the same question ("does this digest still need
    something?") from opposite directions, and if the two conditions were
    ever allowed to drift apart the failure mode is silent and severe --
    either this prune deletes the items behind a digest get_pending_digests
    still considers pending (e.g. a pending SITE backfill of an arbitrarily
    old digest reads its item URLs via get_digest_item_urls; pruning out from
    under it would corrupt or empty that resend), or rows survive that both
    functions already agree are done, quietly defeating the point of this
    prune. Any future change to get_pending_digests' pendingness rule must be
    mirrored here in the same commit.

    Known, accepted trade-off: pendingness is evaluated with the channels
    enabled NOW. A channel re-enabled after a months-long pause makes old
    digests pending again -- but their items may already be pruned, so such
    a late backfill renders with an empty URL allowlist (citations defang
    to plain text; see digest/summarize.py's enforce_link_allowlist). The
    digest BODY is intact -- body_md lives on the digests row, which is
    never pruned -- so this degrades links only, on content months stale,
    and only after a deliberate config flip. Not worth keeping every item
    forever to prevent.

    When none of the three channels are enabled, get_pending_digests treats
    NO digest as pending at all (its own `if not conditions: return []`
    short-circuit) -- mirrored here by dropping the `NOT IN (...)` exclusion
    entirely rather than querying it with an empty condition list, so every
    digest_id-carrying row past the cutoff is eligible. This should never
    actually happen in production (`Config.from_env` raises a ConfigError
    when every channel is disabled), but it keeps this function's own
    contract total rather than leaning on that caller-side guarantee -- the
    same reasoning get_pending_digests applies to itself for the identical
    edge case.

    `fetched_at` is compared lexicographically against the cutoff, safe for
    the same reason get_recent_digests relies on: both are ISO8601 UTC
    strings from `datetime.isoformat()`, whose fixed-width,
    most-significant-field-first layout makes lexicographic and
    chronological order coincide.

    Deliberately NOT folded into commit_new_items alongside the
    polymarket_probs prune it already runs (`_POLYMARKET_PROB_PRUNE_DAYS`):
    that prune needs no input beyond the current time, so it can run
    unconditionally inside commit_new_items' own already-open transaction on
    every call. This prune needs the three channel-enabled flags, which live
    on Config (digest/config.py) -- commit_new_items has no config coupling
    today (it's called from collector wiring alone) and should not grow one
    just to host an unrelated prune. Keeping this as its own top-level
    function, called explicitly from digest/main.py's `_run` after delivery,
    keeps that separation intact.

    Runs in its own transaction (BEGIN/commit, rollback on any failure) --
    a standalone one, unlike the polymarket prune above which piggybacks on
    commit_new_items' already-open transaction. Returns the number of rows
    deleted.
    """
    conditions = []
    if email_enabled:
        conditions.append("email_sent = 0")
    if site_enabled:
        conditions.append("site_published = 0")
    if telegram_enabled:
        conditions.append("telegram_sent = 0")

    cutoff = (datetime.now(UTC) - timedelta(days=_ITEMS_PRUNE_DAYS)).isoformat()
    query = "DELETE FROM items WHERE fetched_at < ? AND digest_id IS NOT NULL"
    if conditions:
        where_sql = " OR ".join(conditions)
        query += f" AND digest_id NOT IN (SELECT id FROM digests WHERE {where_sql})"

    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(query, (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise


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

    CORRECTNESS CONSTRAINT, not a preference: this DOES filter on
    `kind = 'window'`, excluding any "daily" brief row. A daily brief's `## `
    headings are a SYNTHESIS of the very same window digests this query
    already returns -- they name the same stories again, just re-clustered
    into the day's arcs. Without this filter, every story a daily brief
    covers would show up TWICE in the next window digest's "recently
    covered" continuity context (once from the window digest that first
    reported it, once more from the daily brief that re-told it hours
    later), which is not additional information, just noise that makes the
    coverage block bigger for nothing. Excluding daily rows here is what
    keeps this "running story memory" scoped to what it was built for:
    tracking which WINDOW-level stories the summarizer has already told the
    reader, not re-deriving that from every level of this codebase's own
    output.

    `since_iso` is compared lexicographically against `created_at` in SQL,
    which is safe here because both are ISO8601 UTC strings produced by
    `datetime.isoformat()` (see create_digest): ISO8601's fixed-width,
    most-significant-field-first layout makes lexicographic order and
    chronological order coincide, so no parsing is needed to filter
    correctly in the query itself.
    """
    rows = conn.execute(
        "SELECT created_at, body_md FROM digests "
        "WHERE created_at >= ? AND kind = 'window' ORDER BY created_at DESC",
        (since_iso,),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def get_window_digests_since(
    conn: sqlite3.Connection, since_iso: str
) -> list[tuple[int, str, int, str]]:
    """Return (id, created_at, item_count, body_md) for every WINDOW digest at/after `since_iso`.

    Feeds digest/daily.py's `build_daily_prompt`/`summarize_daily`: a daily
    brief is synthesized from the day's already-curated window briefings,
    never from raw items (see digest/main.py's `run_daily`). Only
    `kind = 'window'` rows are ever returned -- a "daily" row must never
    feed a later daily brief as one of its own inputs, both because that
    would be summarizing a summary of a summary (compounding information
    loss for no benefit) and because a prior daily run failing mid-delivery
    and being retried must not make it appear, to a LATER daily run, as one
    more window digest to synthesize.

    Ordered ASCENDING by id (oldest first) -- the opposite of
    `get_recent_digests`' newest-first order. `get_recent_digests` feeds a
    "here's what you already told the reader, newest first" list where
    order barely matters beyond age labeling; `build_daily_prompt` instead
    renders each briefing under a chronological separator so the model can
    trace a story's ARC across the day ("X said A in the morning; by
    evening B") -- that only reads correctly oldest-to-newest.

    `since_iso` is compared lexicographically against `created_at`, safe for
    the identical reason `get_recent_digests` relies on: both are ISO8601
    UTC strings from `datetime.isoformat()`, whose fixed-width,
    most-significant-field-first layout makes lexicographic and
    chronological order coincide.
    """
    rows = conn.execute(
        "SELECT id, created_at, item_count, body_md FROM digests "
        "WHERE created_at >= ? AND kind = 'window' ORDER BY id ASC",
        (since_iso,),
    ).fetchall()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


def get_daily_digests_since(
    conn: sqlite3.Connection, since_iso: str
) -> list[tuple[int, str, int, str]]:
    """Return (id, created_at, item_count, body_md) for every DAILY digest at/after `since_iso`.

    Sibling of `get_window_digests_since` immediately above, one editorial
    rung up: feeds digest/weekly.py's `build_weekly_prompt`/`summarize_weekly`,
    the same way `get_window_digests_since` feeds the daily brief. A weekly
    brief is synthesized from the week's already-curated DAILY briefs, never
    from raw window digests or raw items directly (see digest/main.py's
    `run_weekly`). Only `kind = 'daily'` rows are ever returned -- the same
    one-way editorial ladder `get_window_digests_since` enforces one rung
    down, for the identical two reasons: a "weekly" row must never feed a
    LATER weekly brief as one of its own inputs, both because that would be
    summarizing a summary of a summary of a summary (compounding information
    loss for no benefit) and because a prior weekly run failing mid-delivery
    and being retried must not make it appear, to a LATER weekly run, as one
    more daily brief to synthesize.

    Ordered ASCENDING by id (oldest first), for the identical reason
    `get_window_digests_since` is: `build_weekly_prompt` renders each source
    daily brief under a chronological per-day separator so the model can
    trace a thread's course across the week ("X was first reported Monday;
    by Friday Y") -- that only reads correctly oldest-to-newest.

    `since_iso` is compared lexicographically against `created_at`, safe for
    the identical reason `get_window_digests_since` relies on: both are
    ISO8601 UTC strings from `datetime.isoformat()`, whose fixed-width,
    most-significant-field-first layout makes lexicographic and
    chronological order coincide.
    """
    rows = conn.execute(
        "SELECT id, created_at, item_count, body_md FROM digests "
        "WHERE created_at >= ? AND kind = 'daily' ORDER BY id ASC",
        (since_iso,),
    ).fetchall()
    return [(row[0], row[1], row[2], row[3]) for row in rows]
