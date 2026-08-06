import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from digest.state import (
    Item,
    commit_new_items,
    connect,
    count_unsummarized_items,
    create_digest,
    get_cursors,
    get_pending_digest,
    get_pending_digests,
    get_polymarket_probs,
    get_recent_digests,
    get_unsummarized_items,
    get_window_digests_since,
    init_db,
    mark_digest_sent,
    mark_digest_site_published,
    mark_digest_telegram_sent,
)


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(str(tmp_path / "state.db"))
    init_db(c)
    yield c
    c.close()


def _item(source_id: str, fetched_at: str = "2026-07-29T10:00:00+00:00", **overrides) -> Item:
    fields = dict(
        source="telegram",
        source_id=source_id,
        chat_id="123",
        author="alice",
        text="hello",
        url=f"https://t.me/c/123/{source_id}",
        fetched_at=fetched_at,
    )
    fields.update(overrides)
    return Item(**fields)


def test_init_db_twice_is_a_noop(conn):
    init_db(conn)  # second call must not raise or alter schema
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {"items", "digests", "cursors"} <= tables


def test_commit_new_items_dedups_on_source_and_source_id(conn):
    items = [_item("1"), _item("2")]
    inserted = commit_new_items(conn, items, {("telegram", "123"): "2"})
    assert inserted == 2

    # re-inserting the same items (e.g. overlapping fetch window) is a no-op
    inserted_again = commit_new_items(conn, items, {("telegram", "123"): "2"})
    assert inserted_again == 0

    count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert count == 2


def test_commit_new_items_same_msg_id_in_different_chats_both_survive(conn):
    # Telegram message ids are only unique per chat; source_id must be
    # chat-scoped (e.g. "{chat_id}:{msg_id}") so that message 42 in two
    # different chats doesn't collide under the (source, source_id) UNIQUE
    # constraint and get silently dropped by INSERT OR IGNORE.
    items = [
        _item("111:42", chat_id="111"),
        _item("222:42", chat_id="222"),
    ]
    inserted = commit_new_items(conn, items, {("telegram", "111"): "42", ("telegram", "222"): "42"})
    assert inserted == 2

    count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert count == 2


def test_cursor_upsert_overwrites(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    assert get_cursors(conn, "telegram") == {"123": "1"}

    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})
    assert get_cursors(conn, "telegram") == {"123": "2"}


def test_atomicity_rolls_back_items_and_cursors_on_failure(conn):
    good_item = _item("1")
    bad_item = _item("2", source="not-a-real-source")  # violates CHECK(source IN (...))

    with pytest.raises(sqlite3.IntegrityError):
        commit_new_items(conn, [good_item, bad_item], {("telegram", "123"): "2"})

    item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert item_count == 0
    assert get_cursors(conn, "telegram") == {}


def test_atomicity_rolls_back_on_null_source_id(conn):
    good_item = _item("1")
    bad_item = Item(
        source="telegram",
        source_id=None,  # violates NOT NULL on source_id
        chat_id="123",
        author="alice",
        text="hello",
        url="https://t.me/c/123/2",
        fetched_at="2026-07-29T10:00:00+00:00",
    )

    with pytest.raises(sqlite3.IntegrityError):
        commit_new_items(conn, [good_item, bad_item], {("telegram", "123"): "2"})

    item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert item_count == 0
    assert get_cursors(conn, "telegram") == {}


def test_atomicity_rolls_back_on_null_fetched_at(conn):
    good_item = _item("1")
    bad_item = Item(
        source="telegram",
        source_id="2",
        chat_id="123",
        author="alice",
        text="hello",
        url="https://t.me/c/123/2",
        fetched_at=None,  # violates NOT NULL on fetched_at
    )

    with pytest.raises(sqlite3.IntegrityError):
        commit_new_items(conn, [good_item, bad_item], {("telegram", "123"): "2"})

    item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert item_count == 0
    assert get_cursors(conn, "telegram") == {}


def test_get_unsummarized_items_orders_by_fetched_at(conn):
    commit_new_items(
        conn,
        [
            _item("3", fetched_at="2026-07-29T12:00:00+00:00"),
            _item("1", fetched_at="2026-07-29T10:00:00+00:00"),
            _item("2", fetched_at="2026-07-29T11:00:00+00:00"),
        ],
        {},
    )
    # mark one item as already summarized — it must be excluded
    conn.execute(
        "INSERT INTO digests (created_at, item_count, email_sent, body_md) VALUES (?, 1, 1, ?)",
        ("2026-07-29T13:00:00+00:00", "already sent"),
    )
    digest_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("UPDATE items SET digest_id = ? WHERE source_id = '3'", (digest_id,))
    conn.commit()

    items = get_unsummarized_items(conn)
    assert [i.source_id for i in items] == ["1", "2"]


def test_get_unsummarized_items_limit_returns_n_oldest_by_fetched_at(conn):
    # P1 finding: an unbounded backlog can exceed the model context. `limit`
    # must cap the batch to the N OLDEST items, not just any N -- ordering
    # is unaffected by the cap.
    commit_new_items(
        conn,
        [
            _item("3", fetched_at="2026-07-29T12:00:00+00:00"),
            _item("1", fetched_at="2026-07-29T10:00:00+00:00"),
            _item("4", fetched_at="2026-07-29T13:00:00+00:00"),
            _item("2", fetched_at="2026-07-29T11:00:00+00:00"),
        ],
        {},
    )

    items = get_unsummarized_items(conn, limit=2)

    assert [i.source_id for i in items] == ["1", "2"]


def test_get_unsummarized_items_limit_none_returns_all(conn):
    commit_new_items(
        conn,
        [_item("1", fetched_at="2026-07-29T10:00:00+00:00"),
         _item("2", fetched_at="2026-07-29T11:00:00+00:00")],
        {},
    )

    items = get_unsummarized_items(conn, limit=None)

    assert [i.source_id for i in items] == ["1", "2"]


def test_count_unsummarized_items_matches_unstamped_row_count(conn):
    commit_new_items(
        conn,
        [
            _item("1", fetched_at="2026-07-29T10:00:00+00:00"),
            _item("2", fetched_at="2026-07-29T11:00:00+00:00"),
            _item("3", fetched_at="2026-07-29T12:00:00+00:00"),
        ],
        {},
    )
    assert count_unsummarized_items(conn) == 3

    batch = get_unsummarized_items(conn, limit=2)
    digest_id = create_digest(conn, "## Needs attention\n...", batch)
    assert digest_id  # sanity

    assert count_unsummarized_items(conn) == 1


# --- digest bookkeeping (Phase 2) ---


def test_create_digest_stamps_all_unsummarized_items_and_stores_body_md(conn):
    commit_new_items(conn, [_item("1"), _item("2")], {("telegram", "123"): "2"})
    items = get_unsummarized_items(conn)

    digest_id = create_digest(conn, "## Needs attention\n...", items)

    rows = conn.execute("SELECT source_id, digest_id FROM items ORDER BY source_id").fetchall()
    assert rows == [("1", digest_id), ("2", digest_id)]

    digest_row = conn.execute(
        "SELECT item_count, email_sent, body_md FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    assert digest_row == (2, 0, "## Needs attention\n...")

    # newly stamped items no longer show up as unsummarized
    assert get_unsummarized_items(conn) == []


def test_get_pending_digest_returns_newest_unsent_then_none_after_marked_sent(conn):
    assert get_pending_digest(conn) is None

    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    first_id = create_digest(conn, "first digest", get_unsummarized_items(conn))

    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})
    second_id = create_digest(conn, "second digest", get_unsummarized_items(conn))

    # newest unsent digest wins
    assert get_pending_digest(conn) == (second_id, "second digest")

    mark_digest_sent(conn, second_id)
    assert get_pending_digest(conn) == (first_id, "first digest")

    mark_digest_sent(conn, first_id)
    assert get_pending_digest(conn) is None


def test_init_db_migrates_pre_phase2_digests_table_missing_body_md(tmp_path: Path):
    # Simulate a database created by Phase 1 (merged to main), whose digests
    # table predates the body_md column added in Phase 2.
    old_conn = connect(str(tmp_path / "legacy.db"))
    old_conn.executescript(
        """
        CREATE TABLE items (
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

        CREATE TABLE digests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            item_count  INTEGER NOT NULL,
            email_sent  INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE cursors (
            source        TEXT NOT NULL CHECK (source IN ('telegram', 'x')),
            scope         TEXT NOT NULL,
            last_seen_id  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (source, scope)
        );
        """
    )
    old_conn.commit()

    # Must not raise sqlite3.OperationalError: no such column: body_md
    init_db(old_conn)

    # get_pending_digest works against the migrated (empty) table
    assert get_pending_digest(old_conn) is None

    # create_digest + reading back a pending digest round-trips post-migration
    commit_new_items(old_conn, [_item("1")], {("telegram", "123"): "1"})
    items = get_unsummarized_items(old_conn)
    digest_id = create_digest(old_conn, "migrated digest body", items)

    assert get_pending_digest(old_conn) == (digest_id, "migrated digest body")

    old_conn.close()


def test_create_digest_rollback_on_failure_leaves_items_unstamped(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    items = get_unsummarized_items(conn)

    # body_md violates NOT NULL -> the whole transaction must roll back,
    # leaving the item's digest_id untouched.
    with pytest.raises(sqlite3.IntegrityError):
        create_digest(conn, None, items)

    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    assert get_unsummarized_items(conn) == items


def test_create_digest_does_not_stamp_items_inserted_after_snapshot(conn):
    # Regression for the P2 finding: create_digest must stamp exactly the
    # `items` snapshot passed in, not everything with digest_id IS NULL at
    # transaction time. An item committed between the snapshot and the
    # create_digest call must be left untouched.
    commit_new_items(conn, [_item("1"), _item("2")], {("telegram", "123"): "2"})
    snapshot = get_unsummarized_items(conn)
    assert [i.source_id for i in snapshot] == ["1", "2"]

    # Simulate a new item arriving after the snapshot was taken but before
    # create_digest's stamping transaction runs.
    commit_new_items(conn, [_item("3")], {("telegram", "123"): "3"})

    digest_id = create_digest(conn, "## digest body\n...", snapshot)

    remaining = get_unsummarized_items(conn)
    assert [i.source_id for i in remaining] == ["3"]

    digest_row = conn.execute(
        "SELECT item_count FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    assert digest_row == (2,)


# --- chat_title (P2 finding: prompt demands a group name the data didn't carry) ---


def test_item_chat_title_round_trips_through_commit_and_get_unsummarized(conn):
    item = _item("1", chat_title="Homelab Hungary")
    commit_new_items(conn, [item], {("telegram", "123"): "1"})

    items = get_unsummarized_items(conn)

    assert items[0].chat_title == "Homelab Hungary"


def test_item_chat_title_defaults_to_none_when_omitted(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})

    items = get_unsummarized_items(conn)

    assert items[0].chat_title is None


def test_init_db_migrates_pre_chat_title_items_table_missing_column(tmp_path: Path):
    # Simulate a database created before the chat_title column existed on
    # `items` (mirrors test_init_db_migrates_pre_phase2_digests_table_missing_body_md
    # above, but for the items.chat_title migration).
    old_conn = connect(str(tmp_path / "legacy_chat_title.db"))
    old_conn.executescript(
        """
        CREATE TABLE items (
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

        CREATE TABLE digests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            item_count  INTEGER NOT NULL,
            email_sent  INTEGER NOT NULL DEFAULT 0,
            body_md     TEXT NOT NULL
        );

        CREATE TABLE cursors (
            source        TEXT NOT NULL CHECK (source IN ('telegram', 'x')),
            scope         TEXT NOT NULL,
            last_seen_id  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (source, scope)
        );
        """
    )
    old_conn.commit()

    # Must not raise sqlite3.OperationalError: no such column: chat_title
    init_db(old_conn)

    # commit/round-trip works against the migrated table, chat_title included
    commit_new_items(
        old_conn, [_item("1", chat_title="Homelab Hungary")], {("telegram", "123"): "1"}
    )
    items = get_unsummarized_items(old_conn)

    assert items[0].chat_title == "Homelab Hungary"

    old_conn.close()


# --- news source (source CHECK expanded to accept 'telegram' | 'x' | 'news') ---


def test_fresh_db_accepts_news_source_item(conn):
    item = _item(
        "guid-1", source="news", chat_id=None, chat_title="AI Weekly", url="https://example.com/a"
    )
    inserted = commit_new_items(conn, [item], {})

    assert inserted == 1
    items = get_unsummarized_items(conn)
    assert items[0].source == "news"
    assert items[0].chat_title == "AI Weekly"


def test_fresh_db_check_still_rejects_unknown_source(conn):
    bad_item = _item("1", source="rss")  # not one of telegram/x/news

    with pytest.raises(sqlite3.IntegrityError):
        commit_new_items(conn, [bad_item], {})


def test_init_db_migrates_pre_news_source_check_with_preexisting_rows(tmp_path: Path):
    # Build a legacy DB with the OLD two-value CHECK and no chat_title column
    # (predates both the chat_title migration and this one), then run
    # init_db, which must apply the older column migrations FIRST and this
    # source-CHECK migration LAST (see its docstring for why the ordering
    # matters). Seed rows that exercise every part of the rebuild: a
    # telegram item stamped to a digest (FK in play), an unstamped x item,
    # and a cursor row.
    old_conn = connect(str(tmp_path / "legacy_news.db"))
    old_conn.executescript(
        """
        CREATE TABLE items (
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

        CREATE TABLE digests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            item_count  INTEGER NOT NULL,
            email_sent  INTEGER NOT NULL DEFAULT 0,
            body_md     TEXT NOT NULL
        );

        CREATE TABLE cursors (
            source        TEXT NOT NULL CHECK (source IN ('telegram', 'x')),
            scope         TEXT NOT NULL,
            last_seen_id  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (source, scope)
        );

        CREATE INDEX idx_items_digest_id ON items(digest_id);
        """
    )
    old_conn.commit()

    old_conn.execute(
        "INSERT INTO digests (created_at, item_count, email_sent, body_md) "
        "VALUES ('2026-07-29T09:00:00+00:00', 1, 1, 'body')"
    )
    digest_id = old_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    old_conn.execute(
        """
        INSERT INTO items (source, source_id, chat_id, author, text, url, fetched_at, digest_id)
        VALUES ('telegram', '111:1', '111', 'alice', 'hi', 'https://t.me/c/111/1',
                '2026-07-29T09:00:00+00:00', ?)
        """,
        (digest_id,),
    )
    old_conn.execute(
        """
        INSERT INTO items (source, source_id, chat_id, author, text, url, fetched_at)
        VALUES ('x', '999', NULL, 'bob', 'hey', 'https://x.com/bob/status/999',
                '2026-07-29T10:00:00+00:00')
        """
    )
    old_conn.execute(
        "INSERT INTO cursors (source, scope, last_seen_id, updated_at) "
        "VALUES ('telegram', '111', '1', '2026-07-29T09:00:00+00:00')"
    )
    old_conn.commit()

    tg_id, x_id = (
        row[0]
        for row in old_conn.execute(
            "SELECT id FROM items ORDER BY source_id"
        ).fetchall()
    )

    init_db(old_conn)  # applies chat_title migration, THEN the news-CHECK migration

    # news insert now works
    commit_new_items(
        old_conn,
        [_item("guid-1", source="news", chat_id=None, url="https://example.com/a")],
        {},
    )

    rows = {
        row[0]: (row[1], row[2])
        for row in old_conn.execute("SELECT id, source, digest_id FROM items").fetchall()
    }
    assert rows[tg_id] == ("telegram", digest_id)  # same id, digest_id link intact
    assert rows[x_id] == ("x", None)  # same id, still unstamped

    assert get_cursors(old_conn, "telegram") == {"111": "1"}

    index_row = old_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_items_digest_id'"
    ).fetchone()
    assert index_row is not None

    # UNIQUE(source, source_id) still enforced post-migration
    with pytest.raises(sqlite3.IntegrityError):
        old_conn.execute(
            "INSERT INTO items (source, source_id, chat_id, text, url, fetched_at) "
            "VALUES ('news', 'guid-1', NULL, 't', 'https://example.com/a', "
            "'2026-07-29T11:00:00+00:00')"
        )

    old_conn.close()


def test_init_db_news_migration_runs_once_idempotent(tmp_path: Path):
    old_conn = connect(str(tmp_path / "legacy_news_idempotent.db"))
    old_conn.executescript(
        """
        CREATE TABLE items (
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

        CREATE TABLE digests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            item_count  INTEGER NOT NULL,
            email_sent  INTEGER NOT NULL DEFAULT 0,
            body_md     TEXT NOT NULL
        );

        CREATE TABLE cursors (
            source        TEXT NOT NULL CHECK (source IN ('telegram', 'x')),
            scope         TEXT NOT NULL,
            last_seen_id  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (source, scope)
        );
        """
    )
    old_conn.commit()

    init_db(old_conn)  # first migration run
    init_db(old_conn)  # must not raise or alter the schema again

    commit_new_items(
        old_conn, [_item("guid-2", source="news", chat_id=None, url="https://example.com/b")], {}
    )
    items = get_unsummarized_items(old_conn)
    assert items[0].source == "news"

    old_conn.close()


def test_create_digest_raises_and_rolls_back_on_snapshot_mismatch(conn):
    # A snapshot item that doesn't match any unstamped row (stale or
    # duplicated) must abort the whole transaction: no digest row, no
    # existing items stamped.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    real_item = get_unsummarized_items(conn)[0]
    bogus_item = _item("does-not-exist")

    with pytest.raises(ValueError, match="digest stamping affected"):
        create_digest(conn, "## digest body\n...", [real_item, bogus_item])

    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    assert get_unsummarized_items(conn) == [real_item]


# --- get_recent_digests (running-story-memory / "recent coverage" feature) ---


def _insert_digest(
    conn: sqlite3.Connection,
    created_at: str,
    body_md: str,
    email_sent: int = 1,
    kind: str = "window",
) -> None:
    """Insert a `digests` row directly, bypassing create_digest -- these tests only
    care about get_recent_digests' own filtering/ordering, not item stamping."""
    conn.execute(
        "INSERT INTO digests (created_at, item_count, email_sent, body_md, kind) "
        "VALUES (?, 0, ?, ?, ?)",
        (created_at, email_sent, body_md, kind),
    )
    conn.commit()


def test_get_recent_digests_excludes_rows_older_than_the_window(conn):
    _insert_digest(conn, "2026-07-28T10:00:00+00:00", "outside")  # 26h before `since` below
    _insert_digest(conn, "2026-07-29T09:00:00+00:00", "inside")

    since_iso = "2026-07-29T00:00:00+00:00"
    result = get_recent_digests(conn, since_iso)

    assert result == [("2026-07-29T09:00:00+00:00", "inside")]


def test_get_recent_digests_includes_a_row_exactly_at_the_window_boundary(conn):
    since_iso = "2026-07-29T00:00:00+00:00"
    _insert_digest(conn, since_iso, "on the boundary")

    result = get_recent_digests(conn, since_iso)

    assert result == [(since_iso, "on the boundary")]


def test_get_recent_digests_includes_unsent_rows(conn):
    # P1 spec requirement: get_recent_digests must deliberately IGNORE
    # email_sent -- a digest created but not yet sent (e.g. pending SMTP
    # retry) still reaches the reader, so it counts as coverage exactly like
    # a sent one.
    _insert_digest(conn, "2026-07-29T10:00:00+00:00", "pending unsent", email_sent=0)

    result = get_recent_digests(conn, "2026-07-29T00:00:00+00:00")

    assert result == [("2026-07-29T10:00:00+00:00", "pending unsent")]


def test_get_recent_digests_orders_newest_first(conn):
    _insert_digest(conn, "2026-07-29T08:00:00+00:00", "oldest")
    _insert_digest(conn, "2026-07-29T12:00:00+00:00", "newest")
    _insert_digest(conn, "2026-07-29T10:00:00+00:00", "middle")

    result = get_recent_digests(conn, "2026-07-29T00:00:00+00:00")

    assert result == [
        ("2026-07-29T12:00:00+00:00", "newest"),
        ("2026-07-29T10:00:00+00:00", "middle"),
        ("2026-07-29T08:00:00+00:00", "oldest"),
    ]


def test_get_recent_digests_empty_when_nothing_in_window(conn):
    _insert_digest(conn, "2026-07-01T00:00:00+00:00", "ancient history")

    result = get_recent_digests(conn, "2026-07-29T00:00:00+00:00")

    assert result == []


def test_get_recent_digests_excludes_daily_kind_rows(conn):
    # CORRECTNESS CONSTRAINT, not a preference: a daily brief's headings are
    # a re-synthesis of the very window digests get_recent_digests already
    # returns -- including them here would double-count every story a daily
    # brief covers in the next window digest's "recently covered" context.
    _insert_digest(conn, "2026-07-29T09:00:00+00:00", "window body", kind="window")
    _insert_digest(conn, "2026-07-29T10:00:00+00:00", "daily body", kind="daily")

    result = get_recent_digests(conn, "2026-07-29T00:00:00+00:00")

    assert result == [("2026-07-29T09:00:00+00:00", "window body")]


# --- polymarket_probs (swing-anchor state, digest/collectors/polymarket.py) ---


def test_get_polymarket_probs_empty_ids_returns_empty_dict_without_querying(conn):
    assert get_polymarket_probs(conn, []) == {}


def test_get_polymarket_probs_returns_only_requested_ids(conn):
    commit_new_items(
        conn,
        [],
        {},
        polymarket_prob_updates={
            "m1": (0.60, "Will X happen?"),
            "m2": (0.30, "Will Y happen?"),
        },
    )

    assert get_polymarket_probs(conn, ["m1"]) == {"m1": 0.60}
    assert get_polymarket_probs(conn, ["m1", "m2"]) == {"m1": 0.60, "m2": 0.30}
    assert get_polymarket_probs(conn, ["m3"]) == {}


def test_commit_new_items_with_empty_items_list_is_a_supported_baseline_call(conn):
    # Baseline recordings (a market's first sighting) go through
    # commit_new_items with items=[] -- must not raise, and must still
    # upsert the anchor.
    inserted = commit_new_items(
        conn, [], {}, polymarket_prob_updates={"m1": (0.55, "Will X happen?")}
    )

    assert inserted == 0
    assert get_polymarket_probs(conn, ["m1"]) == {"m1": 0.55}


def test_commit_new_items_polymarket_prob_updates_upsert_overwrites_on_repeat(conn):
    commit_new_items(conn, [], {}, polymarket_prob_updates={"m1": (0.40, "Q")})
    assert get_polymarket_probs(conn, ["m1"]) == {"m1": 0.40}

    commit_new_items(conn, [], {}, polymarket_prob_updates={"m1": (0.60, "Q")})
    assert get_polymarket_probs(conn, ["m1"]) == {"m1": 0.60}


def test_commit_new_items_polymarket_prob_updates_defaults_to_none_unaffected(conn):
    # Every pre-existing call shape (no polymarket_prob_updates kwarg at
    # all) must remain completely unaffected -- the parameter is
    # keyword-only with a None default specifically so this stays true.
    inserted = commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})

    assert inserted == 1
    assert (
        conn.execute("SELECT COUNT(*) FROM polymarket_probs").fetchone()[0] == 0
    )


def test_commit_new_items_rolls_back_polymarket_prob_updates_on_item_insert_failure(conn):
    good_item = _item("1")
    bad_item = _item("2", source="not-a-real-source")  # violates CHECK(source IN (...))

    with pytest.raises(sqlite3.IntegrityError):
        commit_new_items(
            conn,
            [good_item, bad_item],
            {("telegram", "123"): "2"},
            polymarket_prob_updates={"m1": (0.60, "Will X happen?")},
        )

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert get_polymarket_probs(conn, ["m1"]) == {}


def test_commit_new_items_prunes_polymarket_probs_older_than_30_days(conn):
    stale_updated_at = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    fresh_updated_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO polymarket_probs (market_id, probability, question, updated_at) "
        "VALUES ('stale', 0.5, 'Old market', ?)",
        (stale_updated_at,),
    )
    conn.execute(
        "INSERT INTO polymarket_probs (market_id, probability, question, updated_at) "
        "VALUES ('fresh', 0.5, 'Recent market', ?)",
        (fresh_updated_at,),
    )
    conn.commit()

    # Pruning happens inside commit_new_items regardless of whether this
    # particular call carries any polymarket_prob_updates of its own.
    commit_new_items(conn, [], {})

    remaining = {
        row[0] for row in conn.execute("SELECT market_id FROM polymarket_probs").fetchall()
    }
    assert remaining == {"fresh"}


# --- items CHECK expanded to accept 'polymarket' ---


def test_fresh_db_accepts_polymarket_source_item(conn):
    item = _item(
        "m1:2026-07-29T10:00:00+00:00",
        source="polymarket",
        chat_id=None,
        chat_title="Polymarket",
        author=None,
        url="https://polymarket.com/market/will-x-happen",
    )
    inserted = commit_new_items(conn, [item], {})

    assert inserted == 1
    items = get_unsummarized_items(conn)
    assert items[0].source == "polymarket"
    assert items[0].chat_title == "Polymarket"


def test_init_db_migrates_pre_polymarket_source_check_with_preexisting_rows(tmp_path: Path):
    # Build a legacy DB with the news-era three-value CHECK (predates the
    # polymarket_probs table and the four-value CHECK), then run init_db,
    # which must apply the news-CHECK migration FIRST (a no-op here, since
    # this fixture is already past it) and the polymarket-CHECK migration
    # LAST. Seed a stamped telegram item and an unstamped x item so the
    # rebuild's row-preservation is exercised, matching the news migration
    # test's precedent.
    old_conn = connect(str(tmp_path / "legacy_polymarket.db"))
    old_conn.executescript(
        """
        CREATE TABLE items (
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
        );

        CREATE TABLE digests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            item_count  INTEGER NOT NULL,
            email_sent  INTEGER NOT NULL DEFAULT 0,
            body_md     TEXT NOT NULL
        );

        CREATE TABLE cursors (
            source        TEXT NOT NULL CHECK (source IN ('telegram', 'x', 'news')),
            scope         TEXT NOT NULL,
            last_seen_id  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (source, scope)
        );

        CREATE INDEX idx_items_digest_id ON items(digest_id);
        """
    )
    old_conn.commit()

    old_conn.execute(
        "INSERT INTO digests (created_at, item_count, email_sent, body_md) "
        "VALUES ('2026-07-29T09:00:00+00:00', 1, 1, 'body')"
    )
    digest_id = old_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    old_conn.execute(
        """
        INSERT INTO items (source, source_id, chat_id, chat_title, author, text, url,
                            fetched_at, digest_id)
        VALUES ('telegram', '111:1', '111', 'Group', 'alice', 'hi', 'https://t.me/c/111/1',
                '2026-07-29T09:00:00+00:00', ?)
        """,
        (digest_id,),
    )
    old_conn.commit()

    tg_id = old_conn.execute("SELECT id FROM items WHERE source_id = '111:1'").fetchone()[0]

    init_db(old_conn)  # applies (no-op) news migration, then the polymarket-CHECK migration

    # polymarket insert now works, via commit_new_items with an accompanying
    # anchor update -- exercising the full new-feature path post-migration.
    commit_new_items(
        old_conn,
        [
            _item(
                "m1:2026-07-29T11:00:00+00:00",
                source="polymarket",
                chat_id=None,
                chat_title="Polymarket",
                author=None,
                url="https://polymarket.com/market/will-x-happen",
            )
        ],
        {},
        polymarket_prob_updates={"m1": (0.6, "Will X happen?")},
    )

    rows = {
        row[0]: row[1] for row in old_conn.execute("SELECT id, source FROM items").fetchall()
    }
    assert rows[tg_id] == "telegram"  # pre-existing row preserved, same id
    assert get_polymarket_probs(old_conn, ["m1"]) == {"m1": 0.6}

    old_conn.close()


# --- items CHECK expanded to accept 'reddit' ---


def test_fresh_db_accepts_reddit_source_item(conn):
    item = _item(
        "h1",
        source="reddit",
        chat_id=None,
        chat_title="r/hungary",
        author="bob",
        url="https://www.reddit.com/r/hungary/comments/h1/napi/",
    )
    inserted = commit_new_items(conn, [item], {})

    assert inserted == 1
    items = get_unsummarized_items(conn)
    assert items[0].source == "reddit"
    assert items[0].chat_title == "r/hungary"


def test_init_db_migrates_pre_reddit_source_check_with_preexisting_rows(tmp_path: Path):
    # Same precedent as test_init_db_migrates_pre_polymarket_source_check_with_preexisting_rows:
    # build a legacy DB with the four-value (pre-reddit) CHECK, then run
    # init_db, which must apply the news and polymarket CHECK migrations
    # first (both no-ops here, since this fixture is already past them) and
    # the reddit-CHECK migration last. Seed a stamped telegram item so the
    # rebuild's row-preservation is exercised.
    old_conn = connect(str(tmp_path / "legacy_reddit.db"))
    old_conn.executescript(
        """
        CREATE TABLE items (
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
        );

        CREATE TABLE digests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            item_count  INTEGER NOT NULL,
            email_sent  INTEGER NOT NULL DEFAULT 0,
            body_md     TEXT NOT NULL
        );

        CREATE TABLE cursors (
            source        TEXT NOT NULL CHECK (source IN ('telegram', 'x', 'news')),
            scope         TEXT NOT NULL,
            last_seen_id  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (source, scope)
        );

        CREATE INDEX idx_items_digest_id ON items(digest_id);
        """
    )
    old_conn.commit()

    old_conn.execute(
        "INSERT INTO digests (created_at, item_count, email_sent, body_md) "
        "VALUES ('2026-07-29T09:00:00+00:00', 1, 1, 'body')"
    )
    digest_id = old_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    old_conn.execute(
        """
        INSERT INTO items (source, source_id, chat_id, chat_title, author, text, url,
                            fetched_at, digest_id)
        VALUES ('telegram', '111:1', '111', 'Group', 'alice', 'hi', 'https://t.me/c/111/1',
                '2026-07-29T09:00:00+00:00', ?)
        """,
        (digest_id,),
    )
    old_conn.commit()

    tg_id = old_conn.execute("SELECT id FROM items WHERE source_id = '111:1'").fetchone()[0]

    init_db(old_conn)  # applies (no-op) news/polymarket migrations, then the reddit-CHECK one

    commit_new_items(
        old_conn,
        [
            _item(
                "h1",
                source="reddit",
                chat_id=None,
                chat_title="r/hungary",
                author="bob",
                url="https://www.reddit.com/r/hungary/comments/h1/napi/",
            )
        ],
        {},
    )

    rows = {
        row[0]: row[1] for row in old_conn.execute("SELECT id, source FROM items").fetchall()
    }
    assert rows[tg_id] == "telegram"  # pre-existing row preserved, same id
    reddit_sources = [
        row[0] for row in old_conn.execute("SELECT source FROM items WHERE source_id = 'h1'")
    ]
    assert reddit_sources == ["reddit"]

    old_conn.close()


# --- site_published / telegram_sent columns (delivery-channels feature) ---


def test_fresh_db_has_site_published_and_telegram_sent_defaulting_to_zero(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    row = conn.execute(
        "SELECT site_published, telegram_sent FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    assert row == (0, 0)


def test_mark_digest_site_published_flips_only_that_flag(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    mark_digest_site_published(conn, digest_id)

    row = conn.execute(
        "SELECT email_sent, site_published, telegram_sent FROM digests WHERE id = ?",
        (digest_id,),
    ).fetchone()
    assert row == (0, 1, 0)


def test_mark_digest_telegram_sent_flips_only_that_flag(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    mark_digest_telegram_sent(conn, digest_id)

    row = conn.execute(
        "SELECT email_sent, site_published, telegram_sent FROM digests WHERE id = ?",
        (digest_id,),
    ).fetchone()
    assert row == (0, 0, 1)


def test_init_db_migrates_pre_delivery_channels_digests_table_missing_new_columns(
    tmp_path: Path,
):
    # Simulate a database created before the multi-channel delivery refactor
    # (predates site_published/telegram_sent -- mirrors the body_md/
    # chat_title migration test precedents above).
    old_conn = connect(str(tmp_path / "legacy_channels.db"))
    old_conn.executescript(
        """
        CREATE TABLE items (
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

        CREATE TABLE digests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            item_count  INTEGER NOT NULL,
            email_sent  INTEGER NOT NULL DEFAULT 0,
            body_md     TEXT NOT NULL
        );

        CREATE TABLE cursors (
            source        TEXT NOT NULL CHECK (source IN ('telegram', 'x', 'news')),
            scope         TEXT NOT NULL,
            last_seen_id  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (source, scope)
        );

        CREATE INDEX idx_items_digest_id ON items(digest_id);
        """
    )
    old_conn.execute(
        "INSERT INTO digests (created_at, item_count, email_sent, body_md) "
        "VALUES ('2026-07-29T09:00:00+00:00', 1, 1, 'body')"
    )
    old_conn.commit()

    # Must not raise sqlite3.OperationalError: no such column: site_published
    init_db(old_conn)
    # Idempotent: a second call must not raise or alter the schema again.
    init_db(old_conn)

    row = old_conn.execute(
        "SELECT site_published, telegram_sent FROM digests"
    ).fetchone()
    assert row == (0, 0)  # pre-existing row defaults to not-yet-delivered on both new channels

    mark_digest_site_published(old_conn, 1)
    mark_digest_telegram_sent(old_conn, 1)
    row = old_conn.execute(
        "SELECT site_published, telegram_sent FROM digests"
    ).fetchone()
    assert row == (1, 1)

    old_conn.close()


# --- get_pending_digests (multi-channel delivery refactor) ---


def _digest_row(
    conn: sqlite3.Connection,
    body_md: str,
    *,
    email_sent: int = 0,
    site_published: int = 0,
    telegram_sent: int = 0,
) -> int:
    """Insert a `digests` row directly with explicit per-channel flags -- these
    tests only care about get_pending_digests' own filtering/ordering, not
    item stamping (mirrors _insert_digest's role for get_recent_digests
    above)."""
    conn.execute(
        "INSERT INTO digests "
        "(created_at, item_count, email_sent, site_published, telegram_sent, body_md) "
        "VALUES ('2026-07-29T10:00:00+00:00', 0, ?, ?, ?, ?)",
        (email_sent, site_published, telegram_sent, body_md),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_get_pending_digests_all_channels_done_is_not_pending(conn):
    _digest_row(conn, "done", email_sent=1, site_published=1, telegram_sent=1)

    assert get_pending_digests(conn, True, True, True) == []


def test_get_pending_digests_any_enabled_channel_incomplete_is_pending(conn):
    email_only = _digest_row(conn, "email pending", email_sent=0, site_published=1, telegram_sent=1)
    site_only = _digest_row(conn, "site pending", email_sent=1, site_published=0, telegram_sent=1)
    telegram_only = _digest_row(
        conn, "telegram pending", email_sent=1, site_published=1, telegram_sent=0
    )

    result = get_pending_digests(conn, True, True, True)

    assert [row[0] for row in result] == [email_only, site_only, telegram_only]


def test_get_pending_digests_disabled_channel_ignored_for_pendingness(conn):
    # The core semantic this function exists for: a legacy row with
    # email_sent = 0 must NOT be pending once EMAIL_ENABLED is turned off,
    # as long as every OTHER enabled channel is already done. Turning a
    # channel off must never resurrect its old unset flags as eternal
    # pending work.
    _digest_row(conn, "email off, others done", email_sent=0, site_published=1, telegram_sent=1)

    result = get_pending_digests(
        conn, email_enabled=False, site_enabled=True, telegram_enabled=True
    )
    assert result == []


def test_get_pending_digests_disabled_channel_ignored_for_retry_but_others_still_pending(conn):
    digest_id = _digest_row(
        conn, "email off, site still pending", email_sent=0, site_published=0, telegram_sent=1
    )

    result = get_pending_digests(
        conn, email_enabled=False, site_enabled=True, telegram_enabled=True
    )

    assert [row[0] for row in result] == [digest_id]
    _, _, done, _kind = result[0]
    # `done` reflects the ACTUAL stored flags, unfiltered by which channels
    # are enabled -- email_sent is still False here even though the email
    # channel is disabled (the caller, not this function, ignores it).
    assert done == {"email": False, "site": False, "telegram": True}


def test_get_pending_digests_orders_oldest_first(conn):
    newer = _digest_row(conn, "newer", email_sent=0)
    older_created_at_but_inserted_second = _digest_row(conn, "also pending", email_sent=0)

    result = get_pending_digests(conn, True, False, False)

    # Insertion (and thus id) order is oldest-first here, matching the
    # function's own ORDER BY id ASC contract.
    assert [row[0] for row in result] == [newer, older_created_at_but_inserted_second]


def test_get_pending_digests_no_channels_enabled_returns_empty_without_querying(conn):
    _digest_row(conn, "irrelevant", email_sent=0, site_published=0, telegram_sent=0)

    assert get_pending_digests(conn, False, False, False) == []


def test_get_pending_digest_wrapper_ignores_site_and_telegram_flags(conn):
    # Backward-compatible wrapper: a digest whose email already went out but
    # whose site/telegram channels are still pending must NOT show up via
    # the old single-channel get_pending_digest -- it only ever asked about
    # email_sent.
    _digest_row(conn, "email done, others pending", email_sent=1, site_published=0, telegram_sent=0)

    assert get_pending_digest(conn) is None


def test_get_pending_digest_wrapper_still_finds_newest_email_unsent(conn):
    first_id = _digest_row(conn, "first", email_sent=0)
    second_id = _digest_row(conn, "second", email_sent=0)

    assert get_pending_digest(conn) == (second_id, "second")

    mark_digest_sent(conn, second_id)
    assert get_pending_digest(conn) == (first_id, "first")


# --- body_md_hu column (Hungarian translation feature) ---


def test_create_digest_stores_and_is_readable_body_md_hu(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    items = get_unsummarized_items(conn)

    digest_id = create_digest(conn, "english body", items, body_md_hu="magyar szöveg")

    row = conn.execute(
        "SELECT body_md, body_md_hu FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    assert row == ("english body", "magyar szöveg")


def test_create_digest_defaults_body_md_hu_to_null(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    items = get_unsummarized_items(conn)

    digest_id = create_digest(conn, "english body", items)

    row = conn.execute(
        "SELECT body_md_hu FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    assert row == (None,)


def test_init_db_migrates_pre_translation_digests_table_missing_body_md_hu(tmp_path: Path):
    # Simulate a database created before the Hungarian translation feature --
    # predates body_md_hu (mirrors the site_published/telegram_sent migration
    # test precedent above).
    old_conn = connect(str(tmp_path / "legacy_translation.db"))
    old_conn.executescript(
        """
        CREATE TABLE items (
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

        CREATE TABLE digests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT NOT NULL,
            item_count      INTEGER NOT NULL,
            email_sent      INTEGER NOT NULL DEFAULT 0,
            site_published  INTEGER NOT NULL DEFAULT 0,
            telegram_sent   INTEGER NOT NULL DEFAULT 0,
            body_md         TEXT NOT NULL
        );

        CREATE TABLE cursors (
            source        TEXT NOT NULL CHECK (source IN ('telegram', 'x', 'news')),
            scope         TEXT NOT NULL,
            last_seen_id  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (source, scope)
        );

        CREATE INDEX idx_items_digest_id ON items(digest_id);
        """
    )
    old_conn.execute(
        "INSERT INTO digests (created_at, item_count, email_sent, body_md) "
        "VALUES ('2026-07-29T09:00:00+00:00', 1, 1, 'body')"
    )
    old_conn.commit()

    # Must not raise sqlite3.OperationalError: no such column: body_md_hu
    init_db(old_conn)
    # Idempotent: a second call must not raise or alter the schema again.
    init_db(old_conn)

    row = old_conn.execute("SELECT body_md_hu FROM digests").fetchone()
    assert row == (None,)  # pre-existing row has no translation, correctly NULL not ''

    # The column is fully usable post-migration.
    commit_new_items(old_conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(
        old_conn, "second body", get_unsummarized_items(old_conn), body_md_hu="fordítás"
    )
    row = old_conn.execute(
        "SELECT body_md_hu FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    assert row == ("fordítás",)

    old_conn.close()


# --- kind column (daily-brief feature) ---


def test_create_digest_defaults_kind_to_window(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    row = conn.execute("SELECT kind FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == ("window",)


def test_create_digest_stores_explicit_daily_kind(conn):
    digest_id = create_digest(conn, "daily body", [], kind="daily")

    row = conn.execute("SELECT kind FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == ("daily",)


def test_create_digest_empty_items_list_is_a_supported_call_shape(conn):
    # A daily brief consumes digests, not items -- create_digest must work
    # with an empty items list and stamp nothing (mirrors the polymarket
    # baseline-recording precedent for commit_new_items' own empty-items
    # support).
    digest_id = create_digest(conn, "daily body", [], kind="daily")

    row = conn.execute(
        "SELECT item_count, kind FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    assert row == (0, "daily")


def test_create_digest_item_count_override_is_used_when_given(conn):
    # A daily brief's item_count is the SUM of its source window digests'
    # own item_counts, not len(items) (which would be 0 -- a daily brief
    # stamps no items at all). The override parameter is what lets that sum
    # reach the stored row.
    digest_id = create_digest(conn, "daily body", [], kind="daily", item_count=412)

    row = conn.execute("SELECT item_count FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (412,)


def test_create_digest_item_count_override_none_falls_back_to_len_items(conn):
    commit_new_items(conn, [_item("1"), _item("2")], {("telegram", "123"): "2"})
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn), item_count=None)

    row = conn.execute("SELECT item_count FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (2,)


def test_init_db_migrates_pre_daily_brief_digests_table_missing_kind_column(tmp_path: Path):
    # Simulate a database created before the daily-brief feature -- predates
    # `kind` (mirrors the body_md_hu/site_published migration test
    # precedents above).
    old_conn = connect(str(tmp_path / "legacy_kind.db"))
    old_conn.executescript(
        """
        CREATE TABLE items (
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

        CREATE TABLE digests (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT NOT NULL,
            item_count      INTEGER NOT NULL,
            email_sent      INTEGER NOT NULL DEFAULT 0,
            site_published  INTEGER NOT NULL DEFAULT 0,
            telegram_sent   INTEGER NOT NULL DEFAULT 0,
            body_md         TEXT NOT NULL,
            body_md_hu      TEXT
        );

        CREATE TABLE cursors (
            source        TEXT NOT NULL CHECK (source IN ('telegram', 'x', 'news')),
            scope         TEXT NOT NULL,
            last_seen_id  TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            PRIMARY KEY (source, scope)
        );

        CREATE INDEX idx_items_digest_id ON items(digest_id);
        """
    )
    old_conn.execute(
        "INSERT INTO digests (created_at, item_count, email_sent, body_md) "
        "VALUES ('2026-07-29T09:00:00+00:00', 1, 1, 'body')"
    )
    old_conn.commit()

    # Must not raise sqlite3.OperationalError: no such column: kind
    init_db(old_conn)
    # Idempotent: a second call must not raise or alter the schema again.
    init_db(old_conn)

    # DEFAULT 'window' backfills every pre-existing row correctly -- it was
    # a window digest before "daily" ever existed as a concept.
    row = old_conn.execute("SELECT kind FROM digests").fetchone()
    assert row == ("window",)

    # The column is fully usable post-migration.
    digest_id = create_digest(old_conn, "daily body", [], kind="daily")
    row = old_conn.execute("SELECT kind FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == ("daily",)

    old_conn.close()


# --- get_pending_digests / get_pending_digest expose kind (daily-brief feature) ---


def test_get_pending_digests_exposes_stored_kind(conn):
    digest_id = create_digest(conn, "daily body", [], kind="daily")

    result = get_pending_digests(
        conn, email_enabled=True, site_enabled=False, telegram_enabled=False
    )

    assert [row[0] for row in result] == [digest_id]
    assert result[0][3] == "daily"


# --- get_window_digests_since (daily-brief feature) ---


def test_get_window_digests_since_returns_only_window_kind(conn):
    window_id = create_digest(conn, "window body", [])
    create_digest(conn, "daily body", [], kind="daily")

    result = get_window_digests_since(conn, "2020-01-01T00:00:00+00:00")

    assert [row[0] for row in result] == [window_id]


def test_get_window_digests_since_excludes_rows_older_than_since(conn):
    _insert_digest(conn, "2026-07-28T10:00:00+00:00", "outside", kind="window")
    _insert_digest(conn, "2026-07-29T09:00:00+00:00", "inside", kind="window")

    result = get_window_digests_since(conn, "2026-07-29T00:00:00+00:00")

    assert [row[3] for row in result] == ["inside"]


def test_get_window_digests_since_orders_ascending_by_id(conn):
    _insert_digest(conn, "2026-07-29T12:00:00+00:00", "newest", kind="window")
    _insert_digest(conn, "2026-07-29T08:00:00+00:00", "oldest", kind="window")
    _insert_digest(conn, "2026-07-29T10:00:00+00:00", "middle", kind="window")

    result = get_window_digests_since(conn, "2026-07-29T00:00:00+00:00")

    # Insertion order (ascending id), NOT created_at order -- ascending id is
    # the function's own documented contract, matching build_daily_prompt's
    # need to read each briefing chronologically.
    assert [row[3] for row in result] == ["newest", "oldest", "middle"]


def test_get_window_digests_since_returns_id_created_at_item_count_body_md(conn):
    commit_new_items(conn, [_item("1"), _item("2")], {("telegram", "123"): "2"})
    digest_id = create_digest(conn, "body text", get_unsummarized_items(conn))

    result = get_window_digests_since(conn, "2020-01-01T00:00:00+00:00")

    assert len(result) == 1
    row_id, created_at, item_count, body_md = result[0]
    assert row_id == digest_id
    assert item_count == 2
    assert body_md == "body text"
    assert isinstance(created_at, str)


def test_get_window_digests_since_empty_when_nothing_in_window(conn):
    _insert_digest(conn, "2026-07-01T00:00:00+00:00", "ancient", kind="window")

    result = get_window_digests_since(conn, "2026-07-29T00:00:00+00:00")

    assert result == []
