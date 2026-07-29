from pathlib import Path

import pytest

from digest.state import (
    Item,
    commit_new_items,
    connect,
    get_cursors,
    get_unsummarized_items,
    init_db,
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

    with pytest.raises(ValueError):
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
        "INSERT INTO digests (created_at, item_count, email_sent) VALUES (?, 1, 1)",
        ("2026-07-29T13:00:00+00:00",),
    )
    digest_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("UPDATE items SET digest_id = ? WHERE source_id = '3'", (digest_id,))
    conn.commit()

    items = get_unsummarized_items(conn)
    assert [i.source_id for i in items] == ["1", "2"]
