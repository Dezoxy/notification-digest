import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from digest.state import (
    Item,
    commit_new_items,
    connect,
    count_stale_unsummarized_by_source,
    count_unsummarized_items,
    create_digest,
    get_all_arc_contexts,
    get_arc_context,
    get_arc_keys,
    get_arc_keys_needing_context,
    get_cursors,
    get_daily_allowed_urls,
    get_daily_digests_since,
    get_deltas,
    get_digest_source_counts,
    get_latest_arc_occurrence,
    get_pending_digests,
    get_polymarket_probs,
    get_recent_arc_keys,
    get_recent_digests,
    get_unsummarized_items,
    get_weekly_allowed_urls,
    get_window_digests_since,
    init_db,
    mark_digest_sent,
    mark_digest_site_published,
    mark_digest_telegram_sent,
    prune_delivered_items,
    prune_stale_unsummarized,
    write_arc_context,
    write_arc_keys,
    write_deltas,
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
    bad_item = _item("2", source="not-a-real-source")  # not in _KNOWN_SOURCES

    with pytest.raises(ValueError, match="not-a-real-source"):
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


# --- count_stale_unsummarized_by_source (drain-failure detection) ---

_STALE_CUTOFF = "2026-07-29T12:00:00+00:00"


def test_count_stale_unsummarized_reports_only_items_older_than_the_cutoff(conn):
    commit_new_items(
        conn,
        [
            _item("old", fetched_at="2026-07-27T10:00:00+00:00"),
            _item("also-old", fetched_at="2026-07-28T10:00:00+00:00"),
            # After the cutoff -- a normal remainder, not a stalled lane.
            _item("fresh", fetched_at="2026-07-29T13:00:00+00:00"),
        ],
        {},
    )

    assert count_stale_unsummarized_by_source(conn, _STALE_CUTOFF) == [
        ("telegram", 2, "2026-07-27T10:00:00+00:00")
    ]


def test_count_stale_unsummarized_ignores_items_already_in_a_digest(conn):
    commit_new_items(conn, [_item("1", fetched_at="2026-07-27T10:00:00+00:00")], {})
    create_digest(conn, "## Heading\n...", get_unsummarized_items(conn, limit=None))

    # Stamped items are summarized by definition -- age is irrelevant.
    assert count_stale_unsummarized_by_source(conn, _STALE_CUTOFF) == []


def test_count_stale_unsummarized_excludes_the_positions_lane(conn):
    commit_new_items(
        conn,
        [
            _item("pos", url="https://t.me/ASI_Alliance/1", fetched_at="2026-07-20T10:00:00+00:00"),
            _item(
                "news-1", source="news", url="https://ex.com/1",
                fetched_at="2026-07-27T10:00:00+00:00",
            ),
        ],
        {},
    )

    result = count_stale_unsummarized_by_source(
        conn, _STALE_CUTOFF, ("https://t.me/asi_alliance/",)
    )

    # The positions lane is hard-capped and deliberately newest-first (PR #85):
    # a permanently starved tail is its designed steady state, so counting it
    # would fire this warning on every run forever. Case-insensitive, matching
    # allocate_by_source's own lowercase-prefix comparison.
    assert result == [("news", 1, "2026-07-27T10:00:00+00:00")]


def test_count_stale_unsummarized_keeps_non_positions_telegram_items(conn):
    # The exclusion must be narrow. A private-chat item carries the
    # `t.me/c/<internal_id>/` form (collectors/telegram.py's
    # build_message_url), which matches no positions prefix -- it is ordinary
    # telegram traffic and must still be counted, or the exclusion would
    # blind the warning to the whole source rather than to one lane.
    commit_new_items(
        conn,
        [
            _item(
                "private", url="https://t.me/c/1234567890/7",
                fetched_at="2026-07-27T10:00:00+00:00",
            ),
            _item("pos", url="https://t.me/ASI_Alliance/1", fetched_at="2026-07-26T10:00:00+00:00"),
        ],
        {},
    )

    result = count_stale_unsummarized_by_source(
        conn, _STALE_CUTOFF, ("https://t.me/asi_alliance/",)
    )

    # Only the private-chat item -- and its own fetched_at is the reported
    # oldest, proving the positions row was excluded from MIN() too, not just
    # from the count.
    assert result == [("telegram", 1, "2026-07-27T10:00:00+00:00")]


def test_count_stale_unsummarized_returns_empty_when_everything_is_draining(conn):
    commit_new_items(conn, [_item("fresh", fetched_at="2026-07-29T13:00:00+00:00")], {})

    assert count_stale_unsummarized_by_source(conn, _STALE_CUTOFF) == []


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

    # get_pending_digests works against the migrated (empty) table
    assert (
        get_pending_digests(
            old_conn, email_enabled=True, site_enabled=False, telegram_enabled=False
        )
        == []
    )

    # create_digest + reading back a pending digest round-trips post-migration
    commit_new_items(old_conn, [_item("1")], {("telegram", "123"): "1"})
    items = get_unsummarized_items(old_conn)
    digest_id = create_digest(old_conn, "migrated digest body", items)

    pending = get_pending_digests(
        old_conn, email_enabled=True, site_enabled=False, telegram_enabled=False
    )
    assert [(row[0], row[1]) for row in pending] == [(digest_id, "migrated digest body")]

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


def test_commit_new_items_rejects_unknown_item_source(conn):
    # Source validity is no longer a DB-level CHECK (see _SCHEMA) -- it's
    # enforced by _KNOWN_SOURCES at the top of commit_new_items instead, and
    # nothing is written when it rejects.
    bad_item = _item("1", source="rss")  # not in _KNOWN_SOURCES

    with pytest.raises(ValueError, match="rss"):
        commit_new_items(conn, [bad_item], {})

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_commit_new_items_rejects_unknown_cursor_source(conn):
    with pytest.raises(ValueError, match="rss"):
        commit_new_items(conn, [], {("rss", "scope"): "1"})

    assert get_cursors(conn, "rss") == {}


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
    # The failure must happen MID-TRANSACTION (a NOT NULL violation raised by
    # SQLite itself), not at the _KNOWN_SOURCES pre-check before BEGIN --
    # this test exists to prove the polymarket upserts participate in the
    # same rollback as items/cursors, so the transaction has to actually
    # open first.
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


# --- source CHECK dropped entirely; PRAGMA user_version schema versioning ---


def test_fresh_db_has_no_source_check_and_is_stamped_at_latest_version(conn):
    items_ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'"
    ).fetchone()[0]
    cursors_ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cursors'"
    ).fetchone()[0]
    # Not a bare "CHECK" substring check: sqlite_master.sql stores the DDL
    # verbatim, comments included, and _SCHEMA's own comment on
    # items.source mentions the word "CHECK" in prose explaining this exact
    # history -- the actual constraint syntax is "CHECK (source ...)".
    assert "CHECK (source" not in items_ddl
    assert "CHECK (source" not in cursors_ddl
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4

    # Fast path: a second call is a no-op and leaves the version unchanged.
    init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


def test_items_table_accepts_unknown_source_at_the_sql_level_post_migration(conn):
    # Proves the CHECK is really gone: a raw INSERT with a source not in
    # _KNOWN_SOURCES succeeds at the SQL level now -- only commit_new_items's
    # code-level guard rejects it, not the schema.
    conn.execute(
        "INSERT INTO items (source, source_id, url, fetched_at) "
        "VALUES ('mastodon', '1', 'https://example.com/1', '2026-07-29T10:00:00+00:00')"
    )
    conn.commit()

    row = conn.execute("SELECT source FROM items WHERE source_id = '1'").fetchone()
    assert row == ("mastodon",)


def test_init_db_migrates_legacy_v0_two_value_check_db_dropping_check_entirely(tmp_path: Path):
    # A v0 database (SQLite defaults PRAGMA user_version to 0 when it's
    # never been set) with the ORIGINAL two-value CHECK, predating every
    # migration in the probe chain -- mirrors the news-migration fixture
    # above. init_db must run the whole legacy bootstrap chain, including
    # the final CHECK-drop, in one call.
    old_conn = connect(str(tmp_path / "legacy_v0.db"))
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
    old_conn.execute(
        "INSERT INTO items (source, source_id, chat_id, author, text, url, fetched_at) "
        "VALUES ('telegram', '111:1', '111', 'alice', 'hi', 'https://t.me/c/111/1', "
        "'2026-07-29T09:00:00+00:00')"
    )
    old_conn.commit()
    row_id = old_conn.execute("SELECT id FROM items").fetchone()[0]

    init_db(old_conn)

    items_ddl = old_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'"
    ).fetchone()[0]
    cursors_ddl = old_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cursors'"
    ).fetchone()[0]
    assert "CHECK (source" not in items_ddl
    assert "CHECK (source" not in cursors_ddl

    preserved = old_conn.execute(
        "SELECT id, source FROM items WHERE id = ?", (row_id,)
    ).fetchone()
    assert preserved == (row_id, "telegram")  # same id, row survives the rebuild chain

    assert old_conn.execute("PRAGMA user_version").fetchone()[0] == 4

    deltas_ddl = old_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'deltas'"
    ).fetchone()
    assert deltas_ddl is not None  # the v0->v4 jump also creates `deltas`

    arc_keys_ddl = old_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'arc_keys'"
    ).fetchone()
    assert arc_keys_ddl is not None  # the v0->v4 jump also creates `arc_keys`

    arc_context_ddl = old_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'arc_context'"
    ).fetchone()
    assert arc_context_ddl is not None  # the v0->v4 jump also creates `arc_context`

    old_conn.close()


def test_init_db_migrates_v1_db_predating_deltas_table_by_adding_it(tmp_path: Path):
    # A database already fully migrated under the ORIGINAL versioning scheme
    # (stamped user_version=1, no `deltas` table -- PLAN.md §11.3 didn't
    # exist yet) must gain the `deltas` table on the next init_db call,
    # without re-running any of the legacy version-0 bootstrap chain (see
    # init_db's own docstring for why that chain is now gated on
    # `version == 0`, not just `not fresh`).
    db_path = str(tmp_path / "v1.db")
    old_conn = connect(db_path)
    init_db(old_conn)
    # Force the DB back down to a "just migrated under the old scheme"
    # shape: drop `deltas` (which a fresh init_db already created) and
    # re-stamp the version to 1, the way every v1 database that predates
    # this PR actually looks on disk.
    old_conn.execute("DROP TABLE deltas")
    old_conn.execute("PRAGMA user_version = 1")
    old_conn.commit()
    assert (
        old_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'deltas'"
        ).fetchone()
        is None
    )

    init_db(old_conn)

    assert old_conn.execute("PRAGMA user_version").fetchone()[0] == 4
    deltas_ddl = old_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'deltas'"
    ).fetchone()
    assert deltas_ddl is not None

    old_conn.close()


def test_init_db_migrates_v2_db_predating_arc_keys_table_by_adding_it(tmp_path: Path):
    # Sibling of test_init_db_migrates_v1_db_predating_deltas_table_by_adding_it
    # immediately above, one version rung up: a database already fully
    # migrated through version 2 (deltas exists, arc_keys does not -- the
    # stable-arc-keys feature didn't exist yet) must gain the `arc_keys`
    # table on the next init_db call, without re-running any earlier step.
    db_path = str(tmp_path / "v2.db")
    old_conn = connect(db_path)
    init_db(old_conn)
    # Force the DB back down to a "just migrated through version 2" shape:
    # drop `arc_keys` (which a fresh init_db already created) and re-stamp
    # the version to 2, the way every v2 database that predates this feature
    # actually looks on disk.
    old_conn.execute("DROP TABLE arc_keys")
    old_conn.execute("PRAGMA user_version = 2")
    old_conn.commit()
    assert (
        old_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'arc_keys'"
        ).fetchone()
        is None
    )

    init_db(old_conn)

    assert old_conn.execute("PRAGMA user_version").fetchone()[0] == 4
    arc_keys_ddl = old_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'arc_keys'"
    ).fetchone()
    assert arc_keys_ddl is not None

    old_conn.close()


def test_init_db_migrates_v3_db_predating_arc_context_table_by_adding_it(tmp_path: Path):
    # Sibling of test_init_db_migrates_v2_db_predating_arc_keys_table_by_adding_it
    # immediately above, one version rung up: a database already fully
    # migrated through version 3 (arc_keys exists, arc_context does not --
    # PLAN.md §11.6 didn't exist yet) must gain the `arc_context` table on
    # the next init_db call, without re-running any earlier step.
    db_path = str(tmp_path / "v3.db")
    old_conn = connect(db_path)
    init_db(old_conn)
    # Force the DB back down to a "just migrated through version 3" shape:
    # drop `arc_context` (which a fresh init_db already created) and
    # re-stamp the version to 3, the way every v3 database that predates
    # this feature actually looks on disk.
    old_conn.execute("DROP TABLE arc_context")
    old_conn.execute("PRAGMA user_version = 3")
    old_conn.commit()
    assert (
        old_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'arc_context'"
        ).fetchone()
        is None
    )

    init_db(old_conn)

    assert old_conn.execute("PRAGMA user_version").fetchone()[0] == 4
    arc_context_ddl = old_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'arc_context'"
    ).fetchone()
    assert arc_context_ddl is not None

    old_conn.close()


def test_init_db_raises_runtime_error_when_db_version_is_newer_than_latest(conn):
    # A database stamped by a NEWER release than this code knows about --
    # running old code against it is undefined, so init_db must refuse
    # rather than silently query/write columns it doesn't know about.
    conn.execute("PRAGMA user_version = 999")

    with pytest.raises(RuntimeError):
        init_db(conn)


# --- site_published / telegram_sent columns (delivery-channels feature) ---


def test_fresh_db_has_site_published_and_telegram_sent_defaulting_to_zero(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    row = conn.execute(
        "SELECT site_published, telegram_sent FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    assert row == (0, 0)


def test_mark_digest_sent_flips_only_that_flag(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    mark_digest_sent(conn, digest_id)

    row = conn.execute(
        "SELECT email_sent, site_published, telegram_sent FROM digests WHERE id = ?",
        (digest_id,),
    ).fetchone()
    assert row == (1, 0, 0)


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


# --- get_pending_digests exposes kind (daily-brief feature) ---


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


# --- get_daily_allowed_urls (daily-brief delivery link-provenance fix) ---


def _window_digest_with_url(
    conn: sqlite3.Connection, created_at: str, source_id: str, url: str
) -> int:
    """Create a real WINDOW digest stamping one item at `url`, with `created_at` forced.

    Mirrors tests/test_main.py's own `_window_digest` helper: get_daily_allowed_urls
    reads real `items.url` rows via a SQL JOIN, not anything a hand-inserted
    `_insert_digest` row (no items table involvement) could exercise.
    """
    item = _item(source_id, fetched_at=created_at, url=url)
    commit_new_items(conn, [item], {})
    digest_id = create_digest(conn, f"window {source_id}", [item])
    conn.execute("UPDATE digests SET created_at = ? WHERE id = ?", (created_at, digest_id))
    conn.commit()
    return digest_id


def test_get_daily_allowed_urls_unions_in_window_window_digest_urls(conn):
    _window_digest_with_url(conn, "2026-07-29T06:00:00+00:00", "1", "https://example.com/a")
    _window_digest_with_url(conn, "2026-07-29T12:00:00+00:00", "2", "https://example.com/b")

    urls = get_daily_allowed_urls(conn, "2026-07-29T20:00:00+00:00")

    assert urls == {"https://example.com/a", "https://example.com/b"}


def test_get_daily_allowed_urls_excludes_digest_older_than_lookback(conn):
    _window_digest_with_url(conn, "2026-07-28T00:00:00+00:00", "1", "https://example.com/old")
    _window_digest_with_url(conn, "2026-07-29T12:00:00+00:00", "2", "https://example.com/new")

    # since = 2026-07-29T20:00 - 24h = 2026-07-28T20:00 -- the "old" digest
    # (2026-07-28T00:00) falls outside that window, the "new" one doesn't.
    urls = get_daily_allowed_urls(conn, "2026-07-29T20:00:00+00:00")

    assert urls == {"https://example.com/new"}


def test_get_daily_allowed_urls_excludes_digest_created_after_brief(conn):
    _window_digest_with_url(conn, "2026-07-29T12:00:00+00:00", "1", "https://example.com/before")
    _window_digest_with_url(conn, "2026-07-29T21:00:00+00:00", "2", "https://example.com/after")

    # The upper bound is strict (< created_at): a window digest created AFTER
    # the brief must never widen a later resend's allowlist -- see
    # get_daily_allowed_urls' own docstring on why this must be deterministic.
    urls = get_daily_allowed_urls(conn, "2026-07-29T20:00:00+00:00")

    assert urls == {"https://example.com/before"}


def test_get_daily_allowed_urls_excludes_items_of_another_daily_digest(conn):
    # Nothing at the SQL level stops a "daily" digests row from carrying a
    # digest_id on an items row -- this proves the `kind = 'window'` filter,
    # not merely "any digest in range", is what keeps one daily brief's own
    # items out of a LATER daily brief's allowlist.
    item = _item("1", fetched_at="2026-07-29T12:00:00+00:00", url="https://example.com/daily-item")
    commit_new_items(conn, [item], {})
    daily_digest_id = create_digest(conn, "daily body", [item], kind="daily")
    conn.execute(
        "UPDATE digests SET created_at = ? WHERE id = ?",
        ("2026-07-29T12:00:00+00:00", daily_digest_id),
    )
    conn.commit()

    urls = get_daily_allowed_urls(conn, "2026-07-29T20:00:00+00:00")

    assert urls == set()


def test_get_daily_allowed_urls_empty_when_nothing_in_range(conn):
    urls = get_daily_allowed_urls(conn, "2026-07-29T20:00:00+00:00")

    assert urls == set()


# --- get_daily_digests_since (weekly-brief feature) ---


def test_get_daily_digests_since_returns_only_daily_kind(conn):
    daily_id = create_digest(conn, "daily body", [], kind="daily")
    create_digest(conn, "window body", [])
    create_digest(conn, "weekly body", [], kind="weekly")

    result = get_daily_digests_since(conn, "2020-01-01T00:00:00+00:00")

    assert [row[0] for row in result] == [daily_id]


def test_get_daily_digests_since_excludes_rows_older_than_since(conn):
    _insert_digest(conn, "2026-07-28T10:00:00+00:00", "outside", kind="daily")
    _insert_digest(conn, "2026-07-29T09:00:00+00:00", "inside", kind="daily")

    result = get_daily_digests_since(conn, "2026-07-29T00:00:00+00:00")

    assert [row[3] for row in result] == ["inside"]


def test_get_daily_digests_since_orders_ascending_by_id(conn):
    _insert_digest(conn, "2026-07-29T12:00:00+00:00", "newest", kind="daily")
    _insert_digest(conn, "2026-07-29T08:00:00+00:00", "oldest", kind="daily")
    _insert_digest(conn, "2026-07-29T10:00:00+00:00", "middle", kind="daily")

    result = get_daily_digests_since(conn, "2026-07-29T00:00:00+00:00")

    # Insertion order (ascending id), NOT created_at order -- ascending id is
    # the function's own documented contract, matching build_weekly_prompt's
    # need to read each source daily brief chronologically.
    assert [row[3] for row in result] == ["newest", "oldest", "middle"]


def test_get_daily_digests_since_returns_id_created_at_item_count_body_md(conn):
    daily_id = create_digest(conn, "body text", [], kind="daily", item_count=8)

    result = get_daily_digests_since(conn, "2020-01-01T00:00:00+00:00")

    assert len(result) == 1
    row_id, created_at, item_count, body_md = result[0]
    assert row_id == daily_id
    assert item_count == 8
    assert body_md == "body text"
    assert isinstance(created_at, str)


def test_get_daily_digests_since_empty_when_nothing_in_window(conn):
    _insert_digest(conn, "2026-07-01T00:00:00+00:00", "ancient", kind="daily")

    result = get_daily_digests_since(conn, "2026-07-29T00:00:00+00:00")

    assert result == []


# --- get_weekly_allowed_urls (weekly-brief delivery link-provenance fix) ---


def test_get_weekly_allowed_urls_unions_in_window_window_digest_urls(conn):
    _window_digest_with_url(conn, "2026-07-25T06:00:00+00:00", "1", "https://example.com/a")
    _window_digest_with_url(conn, "2026-07-28T12:00:00+00:00", "2", "https://example.com/b")

    urls = get_weekly_allowed_urls(conn, "2026-07-29T20:00:00+00:00")

    assert urls == {"https://example.com/a", "https://example.com/b"}


def test_get_weekly_allowed_urls_excludes_digest_older_than_lookback(conn):
    _window_digest_with_url(conn, "2026-07-20T00:00:00+00:00", "1", "https://example.com/old")
    _window_digest_with_url(conn, "2026-07-28T12:00:00+00:00", "2", "https://example.com/new")

    # since = 2026-07-29T20:00 - 7d = 2026-07-22T20:00 -- the "old" digest
    # (2026-07-20T00:00) falls outside that window, the "new" one doesn't.
    urls = get_weekly_allowed_urls(conn, "2026-07-29T20:00:00+00:00")

    assert urls == {"https://example.com/new"}


def test_get_weekly_allowed_urls_excludes_digest_created_after_brief(conn):
    _window_digest_with_url(conn, "2026-07-28T12:00:00+00:00", "1", "https://example.com/before")
    _window_digest_with_url(conn, "2026-07-29T21:00:00+00:00", "2", "https://example.com/after")

    # The upper bound is strict (< created_at): a window digest created AFTER
    # the brief must never widen a later resend's allowlist -- see
    # get_weekly_allowed_urls' own docstring on why this must be
    # deterministic (mirrors get_daily_allowed_urls' identical reasoning).
    urls = get_weekly_allowed_urls(conn, "2026-07-29T20:00:00+00:00")

    assert urls == {"https://example.com/before"}


def test_get_weekly_allowed_urls_excludes_items_of_a_daily_or_weekly_digest(conn):
    # Nothing at the SQL level stops a "daily"/"weekly" digests row from
    # carrying a digest_id on an items row -- this proves the `kind =
    # 'window'` filter, not merely "any digest in range", is what keeps a
    # daily or weekly brief's own items out of a weekly brief's allowlist.
    daily_item = _item(
        "1", fetched_at="2026-07-28T12:00:00+00:00", url="https://example.com/daily-item"
    )
    commit_new_items(conn, [daily_item], {})
    daily_digest_id = create_digest(conn, "daily body", [daily_item], kind="daily")
    conn.execute(
        "UPDATE digests SET created_at = ? WHERE id = ?",
        ("2026-07-28T12:00:00+00:00", daily_digest_id),
    )
    conn.commit()

    urls = get_weekly_allowed_urls(conn, "2026-07-29T20:00:00+00:00")

    assert urls == set()


def test_get_weekly_allowed_urls_empty_when_nothing_in_range(conn):
    urls = get_weekly_allowed_urls(conn, "2026-07-29T20:00:00+00:00")

    assert urls == set()


# --- prune_delivered_items (bounded state.db) ---


def _insert_item_row(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    fetched_at: str,
    digest_id: int | None = None,
) -> None:
    """Insert an `items` row directly with an explicit `fetched_at`/`digest_id` --
    these tests only care about prune_delivered_items' own age/pendingness
    filtering, not real item collection (mirrors _insert_digest's role for
    get_recent_digests above)."""
    conn.execute(
        "INSERT INTO items (source, source_id, chat_id, author, text, url, fetched_at, digest_id) "
        "VALUES ('telegram', ?, '123', 'alice', 'hello', ?, ?, ?)",
        (source_id, f"https://t.me/c/123/{source_id}", fetched_at, digest_id),
    )
    conn.commit()


def test_prune_delivered_items_deletes_old_fully_delivered_row(conn):
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    digest_id = _digest_row(conn, "done", email_sent=1, site_published=1, telegram_sent=1)
    _insert_item_row(conn, "1", fetched_at=old, digest_id=digest_id)

    deleted = prune_delivered_items(
        conn, email_enabled=True, site_enabled=True, telegram_enabled=True
    )

    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_prune_delivered_items_keeps_unsummarized_backlog_regardless_of_age(conn):
    # digest_id IS NULL -- never attached to a digest, so it must never be
    # pruned no matter how old: it hasn't shipped yet.
    ancient = (datetime.now(UTC) - timedelta(days=900)).isoformat()
    _insert_item_row(conn, "1", fetched_at=ancient, digest_id=None)

    deleted = prune_delivered_items(
        conn, email_enabled=True, site_enabled=True, telegram_enabled=True
    )

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_prune_delivered_items_keeps_row_whose_digest_is_pending_on_an_enabled_channel(conn):
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    digest_id = _digest_row(
        conn, "site pending", email_sent=1, site_published=0, telegram_sent=1
    )
    _insert_item_row(conn, "1", fetched_at=old, digest_id=digest_id)

    deleted = prune_delivered_items(
        conn, email_enabled=True, site_enabled=True, telegram_enabled=True
    )

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_prune_delivered_items_deletes_when_the_only_pending_channel_is_disabled(conn):
    # Same row/flags as the test above, but the site channel is now
    # DISABLED -- mirroring get_pending_digests, a disabled channel's unset
    # flag must never block a prune, so this row is now eligible.
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    digest_id = _digest_row(
        conn, "site pending but disabled", email_sent=1, site_published=0, telegram_sent=1
    )
    _insert_item_row(conn, "1", fetched_at=old, digest_id=digest_id)

    deleted = prune_delivered_items(
        conn, email_enabled=True, site_enabled=False, telegram_enabled=True
    )

    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_prune_delivered_items_keeps_fresh_rows_even_if_fully_delivered(conn):
    fresh = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    digest_id = _digest_row(conn, "done", email_sent=1, site_published=1, telegram_sent=1)
    _insert_item_row(conn, "1", fetched_at=fresh, digest_id=digest_id)

    deleted = prune_delivered_items(
        conn, email_enabled=True, site_enabled=True, telegram_enabled=True
    )

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


# --- prune_stale_unsummarized ---


def test_prune_stale_unsummarized_deletes_old_never_selected_rows(conn):
    stale = (datetime.now(UTC) - timedelta(days=15)).isoformat()
    _insert_item_row(conn, "1", fetched_at=stale, digest_id=None)

    deleted = prune_stale_unsummarized(conn)

    assert deleted == 1
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_prune_stale_unsummarized_keeps_recent_backlog(conn):
    # 13 days old: still inside the retention window -- genuine backlog that
    # the drain loop may yet select.
    recent = (datetime.now(UTC) - timedelta(days=13)).isoformat()
    _insert_item_row(conn, "1", fetched_at=recent, digest_id=None)

    deleted = prune_stale_unsummarized(conn)

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_prune_stale_unsummarized_never_touches_delivered_rows(conn):
    # A stamped row -- however old -- is prune_delivered_items' territory:
    # it backs citation rendering for pending resends and has its own,
    # longer retention rule.
    ancient = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    digest_id = _digest_row(conn, "done", email_sent=1, site_published=1, telegram_sent=1)
    _insert_item_row(conn, "1", fetched_at=ancient, digest_id=digest_id)

    deleted = prune_stale_unsummarized(conn)

    assert deleted == 0
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


# --- get_digest_source_counts ---


def test_get_digest_source_counts_returns_counts_across_sources(conn):
    items = [
        _item("1"),
        _item("2"),
        _item("100", source="x", chat_id=None, url="https://x.com/i/status/100"),
    ]
    commit_new_items(
        conn, items, {("telegram", "123"): "2", ("x", "notifications"): "100"}
    )
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    assert get_digest_source_counts(conn, digest_id) == {"telegram": 2, "x": 1}


def test_get_digest_source_counts_empty_for_daily_digest_with_no_items(conn):
    # A daily brief stamps no items at all (see create_digest's docstring) --
    # this must return {} rather than raise or query nothing meaningful.
    digest_id = create_digest(conn, "daily body", [], kind="daily")

    assert get_digest_source_counts(conn, digest_id) == {}


# --- write_deltas / get_deltas (PLAN.md §11.3) ---


def test_get_deltas_empty_for_digest_with_no_deltas(conn):
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    assert get_deltas(conn, digest_id) == []


def test_write_deltas_then_get_deltas_round_trips(conn):
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    write_deltas(
        conn,
        digest_id,
        [
            {"slug": "story-one", "previously": "old one", "now": "new one"},
            {"slug": "story-two", "previously": "old two", "now": "new two"},
        ],
    )

    assert get_deltas(conn, digest_id) == [
        {"slug": "story-one", "previously": "old one", "now": "new one"},
        {"slug": "story-two", "previously": "old two", "now": "new two"},
    ]


def test_write_deltas_empty_list_is_a_no_op(conn):
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    write_deltas(conn, digest_id, [])

    assert get_deltas(conn, digest_id) == []
    assert conn.execute("SELECT COUNT(*) FROM deltas").fetchone()[0] == 0


def test_write_deltas_same_digest_and_slug_twice_upserts_last_write_wins(conn):
    # The §11.3 idempotency guardrail: a re-run over the same window must
    # not duplicate rows -- PRIMARY KEY (digest_id, slug) plus INSERT OR
    # REPLACE means calling this twice for the same (digest_id, slug) leaves
    # exactly one row, with the second call's values winning.
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    write_deltas(conn, digest_id, [{"slug": "story-one", "previously": "a", "now": "b"}])
    write_deltas(conn, digest_id, [{"slug": "story-one", "previously": "c", "now": "d"}])

    rows = conn.execute(
        "SELECT slug, previously, now FROM deltas WHERE digest_id = ?", (digest_id,)
    ).fetchall()
    assert rows == [("story-one", "c", "d")]


def test_write_deltas_scoped_per_digest_id(conn):
    digest_id_1 = create_digest(conn, "body one", get_unsummarized_items(conn))
    write_deltas(conn, digest_id_1, [{"slug": "story-one", "previously": "a", "now": "b"}])

    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})
    digest_id_2 = create_digest(conn, "body two", get_unsummarized_items(conn))
    write_deltas(conn, digest_id_2, [{"slug": "story-one", "previously": "x", "now": "y"}])

    assert get_deltas(conn, digest_id_1) == [
        {"slug": "story-one", "previously": "a", "now": "b"}
    ]
    assert get_deltas(conn, digest_id_2) == [
        {"slug": "story-one", "previously": "x", "now": "y"}
    ]


# --- write_arc_keys / get_arc_keys (stable-arc-keys feature) ---


def test_get_arc_keys_empty_for_digest_with_no_arc_keys(conn):
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    assert get_arc_keys(conn, digest_id) == {}


def test_write_arc_keys_then_get_arc_keys_round_trips(conn):
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    write_arc_keys(
        conn,
        digest_id,
        [
            {"slug": "hormuz-tension", "label": "Hormuz tension", "key": "hormuz"},
            {"slug": "story-two", "label": "Story two", "key": "story-two-key"},
        ],
    )

    assert get_arc_keys(conn, digest_id) == {
        "hormuz-tension": "hormuz",
        "story-two": "story-two-key",
    }


def test_write_arc_keys_skips_topics_without_a_key(conn):
    # A topic dict with no "key" (a brand-new story this run, or one the
    # model didn't tag) has nothing to persist -- only entries carrying a
    # truthy "key" are written.
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    write_arc_keys(
        conn,
        digest_id,
        [
            {"slug": "tagged", "label": "Tagged", "key": "tagged-key"},
            {"slug": "untagged", "label": "Untagged"},
        ],
    )

    assert get_arc_keys(conn, digest_id) == {"tagged": "tagged-key"}


def test_write_arc_keys_empty_list_is_a_no_op(conn):
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    write_arc_keys(conn, digest_id, [])

    assert get_arc_keys(conn, digest_id) == {}
    assert conn.execute("SELECT COUNT(*) FROM arc_keys").fetchone()[0] == 0


def test_write_arc_keys_no_keyed_topics_is_a_no_op(conn):
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    write_arc_keys(conn, digest_id, [{"slug": "untagged", "label": "Untagged"}])

    assert get_arc_keys(conn, digest_id) == {}
    assert conn.execute("SELECT COUNT(*) FROM arc_keys").fetchone()[0] == 0


def test_write_arc_keys_same_digest_and_slug_twice_upserts_last_write_wins(conn):
    # The idempotency guardrail (mirrors write_deltas' own): a re-run over
    # the same window must not duplicate rows -- PRIMARY KEY (digest_id,
    # slug) plus INSERT OR REPLACE means calling this twice for the same
    # (digest_id, slug) leaves exactly one row, second call's value wins.
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))

    write_arc_keys(conn, digest_id, [{"slug": "story-one", "label": "x", "key": "key-a"}])
    write_arc_keys(conn, digest_id, [{"slug": "story-one", "label": "x", "key": "key-b"}])

    rows = conn.execute(
        "SELECT slug, key FROM arc_keys WHERE digest_id = ?", (digest_id,)
    ).fetchall()
    assert rows == [("story-one", "key-b")]


def test_write_arc_keys_scoped_per_digest_id(conn):
    digest_id_1 = create_digest(conn, "body one", get_unsummarized_items(conn))
    write_arc_keys(conn, digest_id_1, [{"slug": "story-one", "label": "x", "key": "key-a"}])

    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})
    digest_id_2 = create_digest(conn, "body two", get_unsummarized_items(conn))
    write_arc_keys(conn, digest_id_2, [{"slug": "story-one", "label": "x", "key": "key-b"}])

    assert get_arc_keys(conn, digest_id_1) == {"story-one": "key-a"}
    assert get_arc_keys(conn, digest_id_2) == {"story-one": "key-b"}


# --- get_recent_arc_keys (stable-arc-keys feature: {{RECENT_ARCS}} source) ---


def test_get_recent_arc_keys_empty_when_nothing_written(conn):
    assert get_recent_arc_keys(conn, "2026-07-01T00:00:00+00:00") == []


def test_get_recent_arc_keys_returns_counted_keys_most_frequent_first(conn):
    digest_id_1 = create_digest(conn, "body one", get_unsummarized_items(conn))
    write_arc_keys(
        conn,
        digest_id_1,
        [
            {"slug": "story-a", "label": "a", "key": "hormuz"},
            {"slug": "story-b", "label": "b", "key": "openai"},
        ],
    )
    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})
    digest_id_2 = create_digest(conn, "body two", get_unsummarized_items(conn))
    # Same key reused across two digests -- must appear once, with its
    # appearance COUNT, and ahead of the single-appearance key: frequency
    # ordering is what keeps the cap from shedding the most-covered arcs
    # (the alphabetical-order bug this replaced).
    write_arc_keys(conn, digest_id_2, [{"slug": "story-c", "label": "c", "key": "hormuz"}])

    assert get_recent_arc_keys(conn, "2000-01-01T00:00:00+00:00") == [
        ("hormuz", 2),
        ("openai", 1),
    ]


def test_get_recent_arc_keys_excludes_rows_older_than_since(conn):
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))
    write_arc_keys(conn, digest_id, [{"slug": "story-a", "label": "a", "key": "hormuz"}])

    created_at = conn.execute(
        "SELECT created_at FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()[0]
    created = datetime.fromisoformat(created_at)
    since_after = (created + timedelta(seconds=1)).isoformat()

    assert get_recent_arc_keys(conn, since_after) == []


def test_get_recent_arc_keys_excludes_daily_kind_digests(conn):
    # Belt-and-suspenders guardrail: arc keys are written only from the
    # window path in practice, but this filter still excludes any daily-kind
    # digest's arc_keys row defensively, mirroring get_recent_digests' own
    # kind filter.
    window_id = create_digest(conn, "window body", get_unsummarized_items(conn))
    write_arc_keys(conn, window_id, [{"slug": "story-a", "label": "a", "key": "window-key"}])

    daily_id = create_digest(conn, "daily body", [], kind="daily")
    # Force a row into arc_keys directly (write_arc_keys is never called from
    # the daily path in production -- this simulates a hypothetical stray
    # row to prove the query-level filter, not just the write-side guardrail).
    conn.execute(
        "INSERT INTO arc_keys (digest_id, slug, key) VALUES (?, ?, ?)",
        (daily_id, "story-b", "daily-key"),
    )
    conn.commit()

    assert get_recent_arc_keys(conn, "2000-01-01T00:00:00+00:00") == [("window-key", 1)]


def test_get_recent_arc_keys_caps_at_fifty(conn):
    digest_id = create_digest(conn, "body", get_unsummarized_items(conn))
    write_arc_keys(
        conn,
        digest_id,
        [{"slug": f"story-{i}", "label": f"s{i}", "key": f"key-{i:03d}"} for i in range(60)],
    )

    result = get_recent_arc_keys(conn, "2000-01-01T00:00:00+00:00")

    assert len(result) == 50


def test_get_recent_arc_keys_cap_sheds_rare_keys_not_alphabetically_late_ones(conn):
    # THE bug this ordering fixed (live, 2026-08-17): 135 distinct keys in
    # the 7-day window and an `ORDER BY key ASC LIMIT 50` cut every key from
    # "h" onward -- including `hormuz` (29 appearances) -- while keeping
    # dozens of one-off keys that happened to sort early. A frequently-used
    # key must survive the cap regardless of where it sorts alphabetically.
    digest_id_1 = create_digest(conn, "body one", get_unsummarized_items(conn))
    write_arc_keys(
        conn,
        digest_id_1,
        # 60 one-off keys that all sort BEFORE "zz-hot-story"...
        [{"slug": f"story-{i}", "label": f"s{i}", "key": f"key-{i:03d}"} for i in range(60)]
        + [{"slug": "hot", "label": "hot", "key": "zz-hot-story"}],
    )
    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})
    digest_id_2 = create_digest(conn, "body two", get_unsummarized_items(conn))
    # ...and the alphabetically-last key recurs in a second digest.
    write_arc_keys(conn, digest_id_2, [{"slug": "hot2", "label": "hot", "key": "zz-hot-story"}])

    result = get_recent_arc_keys(conn, "2000-01-01T00:00:00+00:00")

    assert len(result) == 50
    assert result[0] == ("zz-hot-story", 2)


# --- write_arc_context / get_arc_context / get_arc_keys_needing_context /
# get_latest_arc_occurrence / get_all_arc_contexts (PLAN.md §11.6, "context mode") ---


def _tagged_window_digest(
    conn: sqlite3.Connection, source_id: str, key: str, slug: str, label: str, created_at: str
) -> int:
    """A window digest whose single `## <label>` section is tagged with `key`/`slug`.

    `created_at` is forced to an exact value (see below).

    Mirrors this file's established pattern (e.g.
    test_get_recent_arc_keys_returns_distinct_keys_sorted): commit a fresh
    item, create_digest off it, then force created_at to an exact value so
    tests can construct precise chronological orderings rather than relying
    on real-clock timing. `label` is embedded as a real `## ` heading so
    digest/publish.py's `derive_topics` (which `get_latest_arc_occurrence`'s
    callers rely on) can actually recover it from `body_md`.
    """
    commit_new_items(conn, [_item(source_id, fetched_at=created_at)], {})
    body_md = f"**TL;DR:** x\n\n## {label}\n\ntext"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))
    conn.execute("UPDATE digests SET created_at = ? WHERE id = ?", (created_at, digest_id))
    conn.commit()
    write_arc_keys(conn, digest_id, [{"slug": slug, "label": label, "key": key}])
    return digest_id


def test_get_arc_context_none_when_never_generated(conn):
    assert get_arc_context(conn, "hormuz") is None


def test_write_arc_context_then_get_arc_context_round_trips(conn):
    write_arc_context(conn, "hormuz", "Background about Hormuz.")

    assert get_arc_context(conn, "hormuz") == "Background about Hormuz."


def test_write_arc_context_upserts_last_write_wins(conn):
    # Idempotency guardrail, mirroring write_arc_keys'/write_deltas' own:
    # PRIMARY KEY (key) plus INSERT OR REPLACE means calling this twice for
    # the same key leaves exactly one row, second call's value wins.
    write_arc_context(conn, "hormuz", "first version")
    write_arc_context(conn, "hormuz", "second version")

    assert get_arc_context(conn, "hormuz") == "second version"
    assert conn.execute("SELECT COUNT(*) FROM arc_context").fetchone()[0] == 1


def test_write_arc_context_scoped_per_key(conn):
    write_arc_context(conn, "hormuz", "hormuz background")
    write_arc_context(conn, "openai", "openai background")

    assert get_arc_context(conn, "hormuz") == "hormuz background"
    assert get_arc_context(conn, "openai") == "openai background"


def test_get_arc_keys_needing_context_requires_at_least_two_appearances(conn):
    _tagged_window_digest(conn, "1", "hormuz", "hormuz", "Hormuz", "2026-08-01T00:00:00+00:00")
    # Only one appearance so far -- must not qualify yet.
    assert get_arc_keys_needing_context(conn, "2000-01-01T00:00:00+00:00", 10) == []

    _tagged_window_digest(conn, "2", "hormuz", "hormuz", "Hormuz", "2026-08-02T00:00:00+00:00")
    # A second appearance -- now qualifies.
    assert get_arc_keys_needing_context(conn, "2000-01-01T00:00:00+00:00", 10) == ["hormuz"]


def test_get_arc_keys_needing_context_excludes_already_stored_keys(conn):
    _tagged_window_digest(conn, "1", "hormuz", "hormuz", "Hormuz", "2026-08-01T00:00:00+00:00")
    _tagged_window_digest(conn, "2", "hormuz", "hormuz", "Hormuz", "2026-08-02T00:00:00+00:00")
    write_arc_context(conn, "hormuz", "already has a primer")

    # Qualifies on recurrence alone, but a stored primer already exists --
    # a primer is generated ONCE per arc, never regenerated.
    assert get_arc_keys_needing_context(conn, "2000-01-01T00:00:00+00:00", 10) == []


def test_get_arc_keys_needing_context_respects_limit_and_oldest_first_ordering(conn):
    _tagged_window_digest(conn, "1", "arc-a", "arc-a", "Arc A", "2026-08-01T00:00:00+00:00")
    _tagged_window_digest(conn, "2", "arc-a", "arc-a", "Arc A", "2026-08-02T00:00:00+00:00")
    _tagged_window_digest(conn, "3", "arc-b", "arc-b", "Arc B", "2026-08-03T00:00:00+00:00")
    _tagged_window_digest(conn, "4", "arc-b", "arc-b", "Arc B", "2026-08-04T00:00:00+00:00")
    _tagged_window_digest(conn, "5", "arc-c", "arc-c", "Arc C", "2026-08-05T00:00:00+00:00")
    _tagged_window_digest(conn, "6", "arc-c", "arc-c", "Arc C", "2026-08-06T00:00:00+00:00")

    # All three qualify; ordered by each key's OWN first-seen instant --
    # arc-a (first seen 08-01), then arc-b (first seen 08-03), then arc-c
    # (first seen 08-05) -- so a backlog drains oldest-arc-first.
    assert get_arc_keys_needing_context(conn, "2000-01-01T00:00:00+00:00", 10) == [
        "arc-a",
        "arc-b",
        "arc-c",
    ]
    # `limit` is a hard SQL-level cap, not a post-hoc slice -- bounding to 2
    # returns exactly the two oldest-first-seen qualifying keys.
    assert get_arc_keys_needing_context(conn, "2000-01-01T00:00:00+00:00", 2) == [
        "arc-a",
        "arc-b",
    ]


def test_get_arc_keys_needing_context_excludes_rows_older_than_since(conn):
    _tagged_window_digest(conn, "1", "hormuz", "hormuz", "Hormuz", "2026-08-01T00:00:00+00:00")
    _tagged_window_digest(conn, "2", "hormuz", "hormuz", "Hormuz", "2026-08-02T00:00:00+00:00")

    assert get_arc_keys_needing_context(conn, "2026-08-03T00:00:00+00:00", 10) == []


def test_get_arc_keys_needing_context_excludes_daily_kind_digests(conn):
    # Belt-and-suspenders guardrail mirroring get_recent_arc_keys' own kind
    # filter: arc keys are written only from the window path in practice,
    # but the query still excludes a daily-kind digest's row defensively.
    _tagged_window_digest(conn, "1", "hormuz", "hormuz", "Hormuz", "2026-08-01T00:00:00+00:00")
    daily_id = create_digest(conn, "daily body", [], kind="daily")
    conn.execute(
        "UPDATE digests SET created_at = ? WHERE id = ?", ("2026-08-02T00:00:00+00:00", daily_id)
    )
    conn.execute(
        "INSERT INTO arc_keys (digest_id, slug, key) VALUES (?, ?, ?)",
        (daily_id, "hormuz", "hormuz"),
    )
    conn.commit()

    # Only ONE real (window) appearance -- the daily-kind row must not count
    # toward the >=2 recurrence threshold.
    assert get_arc_keys_needing_context(conn, "2000-01-01T00:00:00+00:00", 10) == []


def test_get_latest_arc_occurrence_returns_most_recent_body_and_slug(conn):
    _tagged_window_digest(
        conn, "1", "hormuz", "hormuz", "Hormuz tension", "2026-08-01T00:00:00+00:00"
    )
    _tagged_window_digest(
        conn, "2", "hormuz", "hormuz", "Hormuz escalation", "2026-08-05T00:00:00+00:00"
    )

    result = get_latest_arc_occurrence(conn, "hormuz")

    assert result is not None
    body_md, slug = result
    assert slug == "hormuz"
    # The MOST RECENT occurrence's own heading text, not the first one --
    # a story's headline wording legitimately drifts while its key stays
    # stable.
    assert "Hormuz escalation" in body_md
    assert "Hormuz tension" not in body_md


def test_get_latest_arc_occurrence_none_when_key_unknown(conn):
    assert get_latest_arc_occurrence(conn, "nonexistent") is None


def test_get_all_arc_contexts_empty_when_nothing_generated(conn):
    assert get_all_arc_contexts(conn) == []


def test_get_all_arc_contexts_returns_newest_first(conn):
    write_arc_context(conn, "older", "older background")
    write_arc_context(conn, "newer", "newer background")
    # Pin generated_at explicitly rather than relying on real-clock
    # granularity between the two calls above (which could tie within the
    # same second).
    conn.execute(
        "UPDATE arc_context SET generated_at = ? WHERE key = ?",
        ("2026-08-01T00:00:00+00:00", "older"),
    )
    conn.execute(
        "UPDATE arc_context SET generated_at = ? WHERE key = ?",
        ("2026-08-05T00:00:00+00:00", "newer"),
    )
    conn.commit()

    assert get_all_arc_contexts(conn) == [
        {"key": "newer", "context_md": "newer background"},
        {"key": "older", "context_md": "older background"},
    ]


def test_get_all_arc_contexts_respects_limit(conn):
    for i in range(5):
        write_arc_context(conn, f"key-{i}", f"background {i}")

    result = get_all_arc_contexts(conn, limit=2)

    assert len(result) == 2
