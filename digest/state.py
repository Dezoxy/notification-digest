"""SQLite state: schema init, item/cursor persistence, digest bookkeeping.

Stdlib sqlite3 only — no ORM. See PLAN.md §4.1 for the schema and the
idempotency contract this module must uphold.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL CHECK (source IN ('telegram', 'x')),
    source_id   TEXT NOT NULL,
    chat_id     TEXT,
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
    email_sent  INTEGER NOT NULL DEFAULT 0
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
    source_id: str
    chat_id: str | None
    author: str | None
    text: str
    url: str
    fetched_at: str  # ISO8601 UTC


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

    Items are inserted with INSERT OR IGNORE, deduping on (source, source_id).
    Every (source, scope) -> last_seen_id in cursor_updates is upserted with
    updated_at set to now (UTC). On any failure the whole transaction is
    rolled back — the cursor must never advance without the items that
    justify it (idempotency contract, PLAN.md §4.1).

    Returns the number of item rows actually inserted.

    Note: INSERT OR IGNORE also silently swallows CHECK/NOT NULL constraint
    violations (not just the UNIQUE dedup conflict it's meant for), so a
    malformed item would otherwise vanish without ever raising. We validate
    the schema's NOT NULL/CHECK invariants in Python first so a bad item
    surfaces as a real exception and triggers the rollback below, instead of
    being silently dropped.
    """
    now = datetime.now(UTC).isoformat()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        inserted = 0
        for item in items:
            if item.source not in ("telegram", "x"):
                raise ValueError(f"invalid item.source: {item.source!r}")
            if not item.url:
                raise ValueError("item.url must be non-empty (NOT NULL)")
            cur.execute(
                """
                INSERT OR IGNORE INTO items
                    (source, source_id, chat_id, author, text, url, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.source,
                    item.source_id,
                    item.chat_id,
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


def get_unsummarized_items(conn: sqlite3.Connection) -> list[Item]:
    """Return items not yet attached to a digest, ordered by fetched_at ascending."""
    rows = conn.execute(
        """
        SELECT source, source_id, chat_id, author, text, url, fetched_at
        FROM items
        WHERE digest_id IS NULL
        ORDER BY fetched_at ASC
        """
    ).fetchall()
    return [
        Item(
            source=row[0],
            source_id=row[1],
            chat_id=row[2],
            author=row[3],
            text=row[4],
            url=row[5],
            fetched_at=row[6],
        )
        for row in rows
    ]
