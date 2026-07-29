import sqlite3
from pathlib import Path

import pytest

from digest.state import (
    Item,
    commit_new_items,
    connect,
    create_digest,
    get_cursors,
    get_pending_digest,
    get_unsummarized_items,
    init_db,
    mark_digest_sent,
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


def test_create_digest_rollback_on_failure_leaves_items_unstamped(conn):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    items = get_unsummarized_items(conn)

    # body_md violates NOT NULL -> the whole transaction must roll back,
    # leaving the item's digest_id untouched.
    with pytest.raises(sqlite3.IntegrityError):
        create_digest(conn, None, items)

    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    assert get_unsummarized_items(conn) == items
