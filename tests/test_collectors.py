from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError

import digest.collectors.telegram as telegram_collector
from digest.collectors.base import CollectResult
from digest.collectors.telegram import build_message_url, collect, is_basic_group


class FakeMessage:
    def __init__(self, id: int, message: str | None = None, sender: SimpleNamespace | None = None):
        self.id = id
        self.message = message
        self.sender = sender


class FakeEntity:
    def __init__(self, id: int, username: str | None = None, title: str | None = None):
        self.id = id
        self.username = username
        self.title = title


class FakeClient:
    """No-network fake honoring the TelegramClientLike duck-typed surface."""

    def __init__(
        self,
        entities: dict[int, FakeEntity],
        messages: dict[int, list[FakeMessage]],
        get_entity_errors: dict[int, Exception] | None = None,
        iter_messages_errors: dict[int, Exception] | None = None,
        get_dialogs_error: Exception | None = None,
        get_dialogs_errors: list[Exception | None] | None = None,
    ):
        self.entities = entities
        self.messages = messages
        self.get_entity_errors = get_entity_errors or {}
        self.iter_messages_errors = iter_messages_errors or {}
        self.get_dialogs_error = get_dialogs_error
        # Configurable sequence of per-call get_dialogs outcomes, popped
        # front-to-back; a None entry means that call succeeds. Takes
        # precedence over `get_dialogs_error` when provided, so tests can
        # exercise e.g. "flood wait once, then succeed on retry".
        self.get_dialogs_errors = (
            list(get_dialogs_errors) if get_dialogs_errors is not None else None
        )
        self.get_dialogs_calls = 0
        self.get_entity_calls = 0
        self.iter_messages_calls: list[tuple[int, int | None, int, bool]] = []

    async def get_dialogs(self) -> list[Any]:
        self.get_dialogs_calls += 1
        if self.get_dialogs_errors is not None:
            if self.get_dialogs_errors:
                err = self.get_dialogs_errors.pop(0)
                if err is not None:
                    raise err
            return []
        if self.get_dialogs_error is not None:
            raise self.get_dialogs_error
        return []

    async def get_entity(self, chat_id: int) -> FakeEntity:
        self.get_entity_calls += 1
        if chat_id in self.get_entity_errors:
            raise self.get_entity_errors[chat_id]
        return self.entities[chat_id]

    def iter_messages(
        self,
        entity: FakeEntity,
        *,
        limit: int | None = None,
        min_id: int = 0,
        reverse: bool = False,
    ):
        self.iter_messages_calls.append((entity.id, limit, min_id, reverse))
        return self._iter(entity.id, limit, min_id, reverse)

    async def _iter(self, chat_id: int, limit: int | None, min_id: int, reverse: bool):
        if chat_id in self.iter_messages_errors:
            raise self.iter_messages_errors[chat_id]
        msgs = [m for m in self.messages.get(chat_id, []) if m.id > min_id]
        # like real Telethon: newest-first by default, ascending when reverse=True
        msgs.sort(key=lambda m: m.id, reverse=not reverse)
        if limit is not None:
            msgs = msgs[:limit]
        for m in msgs:
            yield m


# --- is_basic_group / build_message_url ---
#
# Real Telegram marked ids: channel/supergroup ids are encoded as
# -(10**12 + internal_id) with internal_id >= 1, so any marked id < -10**12
# (13+ digits) is a channel/supergroup. A negative id >= -10**12 (fewer
# digits) is a legacy basic group. Fixtures below use realistic 13+ digit ids for
# supergroups/channels so they land in the correct range -- a naive
# string-prefix check like "starts with -100" would misclassify a basic
# group such as -10012345 as a supergroup, which is exactly the bug this
# suite guards against (see the regression tests at the bottom of this
# section).


def test_build_message_url_public_chat():
    assert build_message_url(-1001234567890, "mygroup", 42) == "https://t.me/mygroup/42"


def test_build_message_url_private_supergroup_computes_internal_id():
    assert build_message_url(-1000001234567, None, 42) == "https://t.me/c/1234567/42"


def test_build_message_url_private_basic_group():
    # plain (non-super) group ids are negative but > -10**12; t.me/c/ links
    # only address supergroups/channels, so there's no valid link to build
    # for a legacy basic group.
    with pytest.raises(ValueError):
        build_message_url(-1234567, None, 42)


def test_is_basic_group_true_for_legacy_basic_group_ids():
    assert is_basic_group(-1234567) is True
    assert is_basic_group(-1) is True


def test_is_basic_group_false_for_channels_users_and_positive_ids():
    assert is_basic_group(-1000001234567) is False
    # Telethon's actual channel range starts just past -10**12 (channel
    # internal ids are >= 1, so the smallest channel marked id is
    # -(10**12 + 1)); -10**12 exactly is still a (very large) basic group.
    assert is_basic_group(-(10**12) - 1) is False
    assert is_basic_group(-(10**12)) is True
    assert is_basic_group(12345) is False


def test_is_basic_group_regression_numeric_range_not_string_prefix():
    # -10012345's decimal digits happen to start with "100", which a
    # string-prefix check misreads as the "-100" supergroup marker. Its
    # magnitude (10012345) is far below 10**12, so it is actually a basic
    # group and must be classified as such.
    assert is_basic_group(-10012345) is True


def test_build_message_url_regression_numeric_range_not_string_prefix():
    with pytest.raises(ValueError):
        build_message_url(-10012345, None, 1)


# --- first run seeding ---


async def test_first_run_seeds_cursor_and_emits_no_items():
    chat_id = -1000000001111
    client = FakeClient(
        entities={chat_id: FakeEntity(chat_id)},
        messages={chat_id: [FakeMessage(10, "hi"), FakeMessage(9, "older")]},
    )

    result = await collect(client, [chat_id], cursors={})

    assert result.items == []
    assert result.cursor_updates == {("telegram", str(chat_id)): "10"}
    assert result.failed is False
    # entity cache must be populated once before any per-chat work
    assert client.get_dialogs_calls == 1
    # first run must only ask for the single latest message
    assert client.iter_messages_calls == [(chat_id, 1, 0, False)]


async def test_first_run_empty_chat_seeds_zero_cursor_then_next_run_emits_new_message():
    chat_id = -1000000001112

    client = FakeClient(
        entities={chat_id: FakeEntity(chat_id)},
        messages={chat_id: []},
    )
    result = await collect(client, [chat_id], cursors={})

    assert result.items == []
    assert result.cursor_updates == {("telegram", str(chat_id)): "0"}
    assert result.failed is False

    client.messages[chat_id] = [FakeMessage(1, "first ever message")]
    result2 = await collect(client, [chat_id], cursors={str(chat_id): "0"})

    assert [i.source_id for i in result2.items] == [f"{chat_id}:1"]
    assert result2.cursor_updates == {("telegram", str(chat_id)): "1"}


# --- incremental run ---


async def test_incremental_run_respects_min_id_orders_ascending_and_skips_textless():
    chat_id = -1000000002222
    client = FakeClient(
        entities={chat_id: FakeEntity(chat_id, username="pubchat")},
        messages={
            chat_id: [
                FakeMessage(
                    12,
                    "third",
                    sender=SimpleNamespace(first_name="Bob", last_name=None, username=None),
                ),
                FakeMessage(11, None),  # sticker/media, no text -> skipped from items
                FakeMessage(
                    10,
                    "first",
                    sender=SimpleNamespace(first_name="Alice", last_name=None, username=None),
                ),
            ]
        },
    )

    result = await collect(client, [chat_id], cursors={str(chat_id): "9"})

    assert [i.source_id for i in result.items] == [f"{chat_id}:10", f"{chat_id}:12"]
    assert [i.text for i in result.items] == ["first", "third"]
    assert result.items[0].author == "Alice"
    assert result.items[0].url == "https://t.me/pubchat/10"
    # cursor advances past the textless message too
    assert result.cursor_updates == {("telegram", str(chat_id)): "12"}
    assert result.failed is False
    # incremental fetches must go oldest-first so a capped backlog never skips messages
    assert client.iter_messages_calls == [
        (chat_id, telegram_collector._MAX_MESSAGES_PER_CHAT, 9, True)
    ]


# --- chat_title (P2 finding: prompt demands a group name the data didn't carry) ---


async def test_incremental_run_sets_chat_title_from_entity_title():
    chat_id = -1000000002223
    client = FakeClient(
        entities={chat_id: FakeEntity(chat_id, title="Homelab Hungary")},
        messages={chat_id: [FakeMessage(10, "hello")]},
    )

    result = await collect(client, [chat_id], cursors={str(chat_id): "9"})

    assert [i.chat_title for i in result.items] == ["Homelab Hungary"]


async def test_incremental_run_chat_title_is_none_when_entity_has_no_title():
    chat_id = -1000000002224
    client = FakeClient(
        entities={chat_id: FakeEntity(chat_id)},  # no title -- e.g. a DM
        messages={chat_id: [FakeMessage(10, "hello")]},
    )

    result = await collect(client, [chat_id], cursors={str(chat_id): "9"})

    assert result.items[0].chat_title is None


# --- legacy basic group filtering ---


async def test_collect_skips_legacy_basic_group_but_processes_supergroup():
    basic_group_id, supergroup_id = -1234567, -1000009999999
    client = FakeClient(
        entities={
            basic_group_id: FakeEntity(basic_group_id),
            supergroup_id: FakeEntity(supergroup_id),
        },
        messages={supergroup_id: [FakeMessage(5, "hello")]},
    )

    result = await collect(
        client,
        [basic_group_id, supergroup_id],
        cursors={str(basic_group_id): "1", str(supergroup_id): "1"},
    )

    assert result.failed is True
    # basic group is never touched: no get_entity call, no cursor update, no items
    assert ("telegram", str(basic_group_id)) not in result.cursor_updates
    assert [i.source_id for i in result.items] == [f"{supergroup_id}:5"]
    assert ("telegram", str(supergroup_id)) in result.cursor_updates
    assert client.get_entity_calls == 1


# --- per-chat crash isolation ---


async def test_per_chat_crash_does_not_block_other_chats():
    bad_chat, good_chat = -1000000003333, -1000000004444
    client = FakeClient(
        entities={bad_chat: FakeEntity(bad_chat), good_chat: FakeEntity(good_chat)},
        messages={good_chat: [FakeMessage(5, "hello")]},
        iter_messages_errors={bad_chat: RuntimeError("boom")},
    )

    result = await collect(
        client, [bad_chat, good_chat], cursors={str(bad_chat): "1", str(good_chat): "1"}
    )

    assert result.failed is True
    assert [i.source_id for i in result.items] == [f"{good_chat}:5"]
    assert ("telegram", str(good_chat)) in result.cursor_updates
    assert ("telegram", str(bad_chat)) not in result.cursor_updates


# --- auth error ---


async def test_auth_error_flags_failed_and_keeps_partial_results():
    good_chat, auth_fail_chat = -1000000005555, -1000000006666
    client = FakeClient(
        entities={good_chat: FakeEntity(good_chat), auth_fail_chat: FakeEntity(auth_fail_chat)},
        messages={good_chat: [FakeMessage(3, "hi")]},
        get_entity_errors={auth_fail_chat: AuthKeyUnregisteredError(request=None)},
    )

    result = await collect(client, [good_chat, auth_fail_chat], cursors={str(good_chat): "1"})

    assert result.failed is True
    assert [i.source_id for i in result.items] == [f"{good_chat}:3"]


async def test_get_dialogs_auth_error_aborts_before_any_chat_is_touched():
    chat_id = -1000000005556
    client = FakeClient(
        entities={chat_id: FakeEntity(chat_id)},
        messages={chat_id: [FakeMessage(1, "hi")]},
        get_dialogs_error=AuthKeyUnregisteredError(request=None),
    )

    result = await collect(client, [chat_id], cursors={})

    assert result.failed is True
    assert result.items == []
    assert result.cursor_updates == {}
    assert client.get_entity_calls == 0


def test_collect_result_defaults():
    r = CollectResult()
    assert r.items == []
    assert r.cursor_updates == {}
    assert r.failed is False


# --- capped backlog must not skip messages (regression for P2) ---


async def test_capped_backlog_fetches_oldest_first_and_leaves_gap_for_next_run(monkeypatch):
    monkeypatch.setattr(telegram_collector, "_MAX_MESSAGES_PER_CHAT", 2)
    chat_id = -1000000007777
    client = FakeClient(
        entities={chat_id: FakeEntity(chat_id)},
        messages={
            chat_id: [
                FakeMessage(10, "oldest"),
                FakeMessage(11, "middle"),
                FakeMessage(12, "newest"),
            ]
        },
    )

    result = await collect(client, [chat_id], cursors={str(chat_id): "9"})

    # only the two OLDEST pending messages are fetched, not the two newest
    assert [i.source_id for i in result.items] == [f"{chat_id}:10", f"{chat_id}:11"]
    # cursor advances only to the newest *fetched* id, leaving msg 12 pending for next run
    assert result.cursor_updates == {("telegram", str(chat_id)): "11"}
    assert client.iter_messages_calls == [(chat_id, 2, 9, True)]


# --- legacy basic group regression: collect() must skip it, not crash (P2) ---


async def test_collect_skips_basic_group_misclassified_by_old_string_prefix_check():
    # -10012345 starts with "-100" as a string, which the old buggy
    # `is_basic_group` used to misread as a supergroup marker. Numerically
    # it is nowhere near the -10**12 channel/supergroup range, so it must
    # be classified (and skipped) as a legacy basic group.
    chat_id = -10012345
    client = FakeClient(
        entities={chat_id: FakeEntity(chat_id)},
        messages={chat_id: [FakeMessage(1, "hi")]},
    )

    result = await collect(client, [chat_id], cursors={str(chat_id): "0"})

    assert result.failed is True
    assert result.items == []
    assert result.cursor_updates == {}
    assert client.get_entity_calls == 0


# --- FloodWait during the get_dialogs prefetch (P2) ---


async def test_get_dialogs_long_floodwait_aborts_before_any_chat_is_touched(monkeypatch):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(telegram_collector.asyncio, "sleep", fake_sleep)

    chat_id = -1000000008888
    client = FakeClient(
        entities={chat_id: FakeEntity(chat_id)},
        messages={chat_id: [FakeMessage(1, "hi")]},
        get_dialogs_errors=[FloodWaitError(request=None, capture=120)],
    )

    result = await collect(client, [chat_id], cursors={})

    assert result.failed is True
    assert result.items == []
    assert result.cursor_updates == {}
    assert client.get_entity_calls == 0
    # a long wait aborts outright, it never sleeps and retries
    assert sleep_calls == []


async def test_get_dialogs_short_floodwait_retries_once_then_processes_chats(monkeypatch):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(telegram_collector.asyncio, "sleep", fake_sleep)

    chat_id = -1000000008889
    client = FakeClient(
        entities={chat_id: FakeEntity(chat_id)},
        messages={chat_id: [FakeMessage(1, "hi")]},
        get_dialogs_errors=[FloodWaitError(request=None, capture=5), None],
    )

    result = await collect(client, [chat_id], cursors={})

    assert sleep_calls == [5]
    assert client.get_dialogs_calls == 2
    assert result.failed is False
    # first run for this chat: cursor seeded, no items yet
    assert result.items == []
    assert result.cursor_updates == {("telegram", str(chat_id)): "1"}
    assert client.get_entity_calls == 1
