from types import SimpleNamespace

from telethon.errors import AuthKeyUnregisteredError

from digest.collectors.telegram import CollectResult, build_message_url, collect


class FakeMessage:
    def __init__(self, id: int, message: str | None = None, sender: SimpleNamespace | None = None):
        self.id = id
        self.message = message
        self.sender = sender


class FakeEntity:
    def __init__(self, id: int, username: str | None = None):
        self.id = id
        self.username = username


class FakeClient:
    """No-network fake honoring the TelegramClientLike duck-typed surface."""

    def __init__(
        self,
        entities: dict[int, FakeEntity],
        messages: dict[int, list[FakeMessage]],
        get_entity_errors: dict[int, Exception] | None = None,
        iter_messages_errors: dict[int, Exception] | None = None,
    ):
        self.entities = entities
        self.messages = messages
        self.get_entity_errors = get_entity_errors or {}
        self.iter_messages_errors = iter_messages_errors or {}
        self.iter_messages_calls: list[tuple[int, int | None, int]] = []

    async def get_entity(self, chat_id: int) -> FakeEntity:
        if chat_id in self.get_entity_errors:
            raise self.get_entity_errors[chat_id]
        return self.entities[chat_id]

    def iter_messages(self, entity: FakeEntity, *, limit: int | None = None, min_id: int = 0):
        self.iter_messages_calls.append((entity.id, limit, min_id))
        return self._iter(entity.id, limit, min_id)

    async def _iter(self, chat_id: int, limit: int | None, min_id: int):
        if chat_id in self.iter_messages_errors:
            raise self.iter_messages_errors[chat_id]
        msgs = [m for m in self.messages.get(chat_id, []) if m.id > min_id]
        msgs.sort(key=lambda m: m.id, reverse=True)  # newest-first, like real Telethon
        if limit is not None:
            msgs = msgs[:limit]
        for m in msgs:
            yield m


# --- build_message_url ---


def test_build_message_url_public_chat():
    assert build_message_url(-1001234567890, "mygroup", 42) == "https://t.me/mygroup/42"


def test_build_message_url_private_supergroup_strips_minus100():
    assert build_message_url(-1001234567, None, 42) == "https://t.me/c/1234567/42"


def test_build_message_url_private_basic_group():
    # plain (non-super) group ids are just negative, no -100 prefix
    assert build_message_url(-1234567, None, 42) == "https://t.me/c/1234567/42"


# --- first run seeding ---


async def test_first_run_seeds_cursor_and_emits_no_items():
    chat_id = -1001111
    client = FakeClient(
        entities={chat_id: FakeEntity(chat_id)},
        messages={chat_id: [FakeMessage(10, "hi"), FakeMessage(9, "older")]},
    )

    result = await collect(client, [chat_id], cursors={})

    assert result.items == []
    assert result.cursor_updates == {("telegram", str(chat_id)): "10"}
    assert result.failed is False
    # first run must only ask for the single latest message
    assert client.iter_messages_calls == [(chat_id, 1, 0)]


# --- incremental run ---


async def test_incremental_run_respects_min_id_orders_ascending_and_skips_textless():
    chat_id = -1002222
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

    assert [i.source_id for i in result.items] == ["10", "12"]
    assert [i.text for i in result.items] == ["first", "third"]
    assert result.items[0].author == "Alice"
    assert result.items[0].url == "https://t.me/pubchat/10"
    # cursor advances past the textless message too
    assert result.cursor_updates == {("telegram", str(chat_id)): "12"}
    assert result.failed is False


# --- per-chat crash isolation ---


async def test_per_chat_crash_does_not_block_other_chats():
    bad_chat, good_chat = -1003333, -1004444
    client = FakeClient(
        entities={bad_chat: FakeEntity(bad_chat), good_chat: FakeEntity(good_chat)},
        messages={good_chat: [FakeMessage(5, "hello")]},
        iter_messages_errors={bad_chat: RuntimeError("boom")},
    )

    result = await collect(
        client, [bad_chat, good_chat], cursors={str(bad_chat): "1", str(good_chat): "1"}
    )

    assert result.failed is True
    assert [i.source_id for i in result.items] == ["5"]
    assert ("telegram", str(good_chat)) in result.cursor_updates
    assert ("telegram", str(bad_chat)) not in result.cursor_updates


# --- auth error ---


async def test_auth_error_flags_failed_and_keeps_partial_results():
    good_chat, auth_fail_chat = -1005555, -1006666
    client = FakeClient(
        entities={good_chat: FakeEntity(good_chat), auth_fail_chat: FakeEntity(auth_fail_chat)},
        messages={good_chat: [FakeMessage(3, "hi")]},
        get_entity_errors={auth_fail_chat: AuthKeyUnregisteredError(request=None)},
    )

    result = await collect(client, [good_chat, auth_fail_chat], cursors={str(good_chat): "1"})

    assert result.failed is True
    assert [i.source_id for i in result.items] == ["3"]


def test_collect_result_defaults():
    r = CollectResult()
    assert r.items == []
    assert r.cursor_updates == {}
    assert r.failed is False
