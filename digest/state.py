"""SQLite state: schema init, item/cursor persistence, digest bookkeeping.

Stdlib sqlite3 only — no ORM. See PLAN.md §4.1 for the schema and the
idempotency contract this module must uphold.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL CHECK (source IN ('telegram', 'x')),
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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    item_count  INTEGER NOT NULL,
    email_sent  INTEGER NOT NULL DEFAULT 0,
    body_md     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cursors (
    source        TEXT NOT NULL CHECK (source IN ('telegram', 'x')),
    scope         TEXT NOT NULL,
    last_seen_id  TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (source, scope)
);

CREATE INDEX IF NOT EXISTS idx_items_digest_id ON items(digest_id);
"""


@dataclass(frozen=True)
class Item:
    source: str  # "telegram" | "x"
    # telegram: "{chat_id}:{msg_id}" (chat-scoped composite — msg ids repeat across chats);
    # x: bare tweet id (globally unique)
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


def get_cursors(conn: sqlite3.Connection, source: str) -> dict[str, str]:
    """Return {scope: last_seen_id} for the given source."""
    rows = conn.execute(
        "SELECT scope, last_seen_id FROM cursors WHERE source = ?", (source,)
    ).fetchall()
    return {scope: last_seen_id for scope, last_seen_id in rows}


def commit_new_items(
    conn: sqlite3.Connection,
    items: list[Item],
    cursor_updates: dict[tuple[str, str], str],
) -> int:
    """Insert new items and advance cursors in a single transaction.

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


def create_digest(conn: sqlite3.Connection, body_md: str, items: Sequence[Item]) -> int:
    """Durably record a digest and stamp its items, in one transaction.

    Inserts a `digests` row (created_at = now UTC, item_count = len(items),
    email_sent = 0, body_md) and stamps exactly the given `items` snapshot
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
    """
    now = datetime.now(UTC).isoformat()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(
            """
            INSERT INTO digests (created_at, item_count, email_sent, body_md)
            VALUES (?, ?, 0, ?)
            """,
            (now, len(items), body_md),
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


def get_pending_digest(conn: sqlite3.Connection) -> tuple[int, str] | None:
    """Return (id, body_md) of the newest unsent digest, or None if none is pending."""
    row = conn.execute(
        "SELECT id, body_md FROM digests WHERE email_sent = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return (row[0], row[1])


def mark_digest_sent(conn: sqlite3.Connection, digest_id: int) -> None:
    """Flip a digest's email_sent flag to 1 after SMTP confirms delivery."""
    conn.execute("UPDATE digests SET email_sent = 1 WHERE id = ?", (digest_id,))
    conn.commit()
