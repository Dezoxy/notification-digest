"""Tests for the `relay` run mode: pure helpers, relay_channel, config, and run_relay.

Telethon is the external boundary and is monkeypatched throughout -- per
CLAUDE.md, tests never reach the network. `FakeRelayClient` honors
`digest.relay.RelayClientLike`'s duck-typed surface (plus the connection
lifecycle methods `_client_ready`/`_prefetch_dialogs` need, for the
`run_relay` end-to-end tests), the same "no real Telethon object in the
loop" approach tests/test_collectors.py's own `FakeClient` uses for the
window collector.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError

import digest.relay as relay_mod
from digest import main as digest_main
from digest.config import Config, ConfigError
from digest.publish import TelegramSendError
from digest.state import commit_new_items, connect, get_cursors, init_db

HUB_PEER = object()


class FakeMessage:
    def __init__(self, id: int, grouped_id: Any | None = None, action: Any | None = None):
        self.id = id
        self.grouped_id = grouped_id
        self.action = action


class FakeRelayClient:
    """No-network fake honoring RelayClientLike, plus the connect/is_user_authorized/
    is_connected/disconnect/get_dialogs surface `_client_ready` and
    `telegram_collector._prefetch_dialogs` need -- so the same fake serves
    both `relay_channel`'s own unit tests and `run_relay`'s end-to-end ones.
    """

    def __init__(
        self,
        channels: dict[str, int],
        messages: dict[int, list[FakeMessage]],
        get_entity_errors: dict[str, Exception] | None = None,
    ):
        self.channels = channels
        self.messages = messages
        self.get_entity_errors = get_entity_errors or {}
        self.requests: list[Any] = []
        # Queue of per-call outcomes for __call__, popped front-to-back;
        # None means that call succeeds. Empty (the default) means every
        # call succeeds.
        self.call_errors: list[Exception | None] = []
        self._connected = False
        self.get_dialogs_calls = 0

    # --- connection lifecycle (_client_ready / _prefetch_dialogs) ---

    async def connect(self) -> None:
        self._connected = True

    async def is_user_authorized(self) -> bool:
        return True

    def is_connected(self) -> bool:
        return self._connected

    async def disconnect(self) -> None:
        self._connected = False

    async def get_dialogs(self) -> list[Any]:
        self.get_dialogs_calls += 1
        return []

    # --- RelayClientLike ---

    async def get_entity(self, username: str) -> int:
        if username in self.get_entity_errors:
            raise self.get_entity_errors[username]
        return self.channels[username]

    async def get_input_entity(self, peer: Any) -> Any:
        return peer

    def iter_messages(
        self, entity: int, *, limit: int | None = None, min_id: int = 0, reverse: bool = False
    ):
        return self._iter(entity, limit, min_id, reverse)

    async def _iter(self, chat_id: int, limit: int | None, min_id: int, reverse: bool):
        msgs = [m for m in self.messages.get(chat_id, []) if m.id > min_id]
        msgs.sort(key=lambda m: m.id, reverse=not reverse)
        if limit is not None:
            msgs = msgs[:limit]
        for m in msgs:
            yield m

    async def __call__(self, request: Any) -> None:
        self.requests.append(request)
        if self.call_errors:
            err = self.call_errors.pop(0)
            if err is not None:
                raise err


# --- deterministic_random_id ---


def test_deterministic_random_id_is_stable():
    a = relay_mod.deterministic_random_id(-1001111, 42)
    b = relay_mod.deterministic_random_id(-1001111, 42)
    assert a == b


def test_deterministic_random_id_fits_signed_64_bit():
    value = relay_mod.deterministic_random_id(-1001111, 42)
    assert -(2**63) <= value <= 2**63 - 1


def test_deterministic_random_id_differs_across_msg_ids():
    assert relay_mod.deterministic_random_id(-1001111, 1) != relay_mod.deterministic_random_id(
        -1001111, 2
    )


def test_deterministic_random_id_differs_across_chat_ids():
    assert relay_mod.deterministic_random_id(-1001111, 1) != relay_mod.deterministic_random_id(
        -1002222, 1
    )


# --- chunk_preserving_albums ---


def test_chunk_preserving_albums_splits_plain_messages_at_the_size_boundary():
    msgs = [FakeMessage(i) for i in range(1, 251)]

    chunks = relay_mod.chunk_preserving_albums(msgs)

    assert [len(c) for c in chunks] == [100, 100, 50]


def test_chunk_preserving_albums_keeps_a_straddling_album_whole_in_the_next_chunk():
    # 98 plain messages (ids 1..98) fill the first chunk to just under the
    # 100 boundary; a 4-message album (ids 99..102) would straddle it if
    # split naively -- it must be pushed whole into the NEXT chunk instead.
    msgs = [FakeMessage(i) for i in range(1, 99)]
    msgs += [FakeMessage(i, grouped_id="album-1") for i in range(99, 103)]
    msgs += [FakeMessage(i) for i in range(103, 120)]

    chunks = relay_mod.chunk_preserving_albums(msgs)

    assert [m.id for m in chunks[0]] == list(range(1, 99))
    assert [m.id for m in chunks[1][:4]] == [99, 100, 101, 102]


def test_chunk_preserving_albums_leaves_an_interior_album_untouched():
    msgs = [FakeMessage(i) for i in range(1, 10)]
    msgs += [FakeMessage(i, grouped_id="album-1") for i in range(10, 13)]
    msgs += [FakeMessage(i) for i in range(13, 20)]

    chunks = relay_mod.chunk_preserving_albums(msgs)

    assert len(chunks) == 1
    ids_in_chunk = [m.id for m in chunks[0]]
    assert ids_in_chunk == list(range(1, 20))
    assert [ids_in_chunk.index(i) for i in (10, 11, 12)] == [9, 10, 11]


def test_chunk_preserving_albums_empty_input():
    assert relay_mod.chunk_preserving_albums([]) == []


# --- is_forwardable ---


def test_is_forwardable_false_for_a_service_message():
    assert relay_mod.is_forwardable(FakeMessage(1, action=SimpleNamespace())) is False


def test_is_forwardable_true_for_an_ordinary_message():
    assert relay_mod.is_forwardable(FakeMessage(1)) is True


# --- relay_channel ---


async def test_relay_channel_first_run_seeds_cursor_at_latest_id_and_forwards_nothing():
    chat_id = -1001111
    client = FakeRelayClient(
        channels={"newschannel": chat_id},
        messages={chat_id: [FakeMessage(1), FakeMessage(2), FakeMessage(5)]},
    )
    advanced: list[tuple[int, int]] = []

    ok, forwarded = await relay_mod.relay_channel(
        client, "newschannel", HUB_PEER, None, {}, lambda c, i: advanced.append((c, i))
    )

    assert ok is True
    assert forwarded == 0
    assert advanced == [(chat_id, 5)]
    assert client.requests == []


async def test_relay_channel_first_run_on_an_empty_channel_seeds_zero():
    chat_id = -1002222
    client = FakeRelayClient(channels={"empty": chat_id}, messages={})
    advanced: list[tuple[int, int]] = []

    ok, forwarded = await relay_mod.relay_channel(
        client, "empty", HUB_PEER, None, {}, lambda c, i: advanced.append((c, i))
    )

    assert ok is True
    assert forwarded == 0
    assert advanced == [(chat_id, 0)]


async def test_relay_channel_incremental_forwards_new_messages_oldest_first():
    chat_id = -1003333
    client = FakeRelayClient(
        channels={"ch": chat_id}, messages={chat_id: [FakeMessage(i) for i in range(1, 6)]}
    )
    advanced: list[tuple[int, int]] = []

    ok, forwarded = await relay_mod.relay_channel(
        client, "ch", HUB_PEER, 77, {str(chat_id): "2"}, lambda c, i: advanced.append((c, i))
    )

    assert ok is True
    assert forwarded == 3
    assert len(client.requests) == 1
    req = client.requests[0]
    assert req.id == [3, 4, 5]
    assert req.random_id == [relay_mod.deterministic_random_id(chat_id, i) for i in (3, 4, 5)]
    assert req.top_msg_id == 77
    assert req.to_peer is HUB_PEER
    assert advanced == [(chat_id, 5)]


async def test_relay_channel_chunks_150_messages_into_two_requests():
    chat_id = -1004444
    client = FakeRelayClient(
        channels={"ch": chat_id}, messages={chat_id: [FakeMessage(i) for i in range(1, 151)]}
    )
    advanced: list[tuple[int, int]] = []

    ok, forwarded = await relay_mod.relay_channel(
        client, "ch", HUB_PEER, None, {str(chat_id): "0"}, lambda c, i: advanced.append((c, i))
    )

    assert ok is True
    assert forwarded == 150
    assert [len(r.id) for r in client.requests] == [100, 50]
    assert advanced == [(chat_id, 100), (chat_id, 150)]


async def test_relay_channel_second_chunk_failure_keeps_first_chunks_progress():
    chat_id = -1005555
    client = FakeRelayClient(
        channels={"ch": chat_id}, messages={chat_id: [FakeMessage(i) for i in range(1, 151)]}
    )
    client.call_errors = [None, RuntimeError("boom")]
    advanced: list[tuple[int, int]] = []

    ok, forwarded = await relay_mod.relay_channel(
        client, "ch", HUB_PEER, None, {str(chat_id): "0"}, lambda c, i: advanced.append((c, i))
    )

    assert ok is False
    assert forwarded == 100
    assert advanced == [(chat_id, 100)]


async def test_relay_channel_service_message_advances_cursor_without_being_forwarded():
    chat_id = -1006666
    msgs = [FakeMessage(1), FakeMessage(2, action=SimpleNamespace()), FakeMessage(3)]
    client = FakeRelayClient(channels={"ch": chat_id}, messages={chat_id: msgs})
    advanced: list[tuple[int, int]] = []

    ok, forwarded = await relay_mod.relay_channel(
        client, "ch", HUB_PEER, None, {str(chat_id): "0"}, lambda c, i: advanced.append((c, i))
    )

    assert ok is True
    assert forwarded == 2
    assert len(client.requests) == 1
    assert client.requests[0].id == [1, 3]
    assert advanced == [(chat_id, 3)]


async def test_relay_channel_all_service_chunk_sends_no_rpc_but_still_advances():
    chat_id = -1007777
    msgs = [FakeMessage(1, action=SimpleNamespace()), FakeMessage(2, action=SimpleNamespace())]
    client = FakeRelayClient(channels={"ch": chat_id}, messages={chat_id: msgs})
    advanced: list[tuple[int, int]] = []

    ok, forwarded = await relay_mod.relay_channel(
        client, "ch", HUB_PEER, None, {str(chat_id): "0"}, lambda c, i: advanced.append((c, i))
    )

    assert ok is True
    assert forwarded == 0
    assert client.requests == []
    assert advanced == [(chat_id, 2)]


async def test_relay_channel_short_flood_wait_retries_once_and_succeeds(monkeypatch):
    chat_id = -1008888
    client = FakeRelayClient(
        channels={"ch": chat_id}, messages={chat_id: [FakeMessage(1), FakeMessage(2)]}
    )
    client.call_errors = [FloodWaitError(request=None, capture=5)]
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(relay_mod.asyncio, "sleep", fake_sleep)
    advanced: list[tuple[int, int]] = []

    ok, forwarded = await relay_mod.relay_channel(
        client, "ch", HUB_PEER, None, {str(chat_id): "0"}, lambda c, i: advanced.append((c, i))
    )

    assert sleep_calls == [5]
    assert ok is True
    assert forwarded == 2
    assert len(client.requests) == 2  # first attempt (failed) + retry (succeeded)
    assert advanced == [(chat_id, 2)]


async def test_relay_channel_long_flood_wait_aborts_without_advancing(monkeypatch):
    chat_id = -1009999
    client = FakeRelayClient(
        channels={"ch": chat_id}, messages={chat_id: [FakeMessage(1), FakeMessage(2)]}
    )
    client.call_errors = [FloodWaitError(request=None, capture=61)]
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr(relay_mod.asyncio, "sleep", fake_sleep)
    advanced: list[tuple[int, int]] = []

    ok, forwarded = await relay_mod.relay_channel(
        client, "ch", HUB_PEER, None, {str(chat_id): "0"}, lambda c, i: advanced.append((c, i))
    )

    assert ok is False
    assert forwarded == 0
    assert advanced == []
    assert sleep_calls == []  # a long wait aborts outright, never sleeps and retries


async def test_relay_channel_forwarding_restricted_aborts_cleanly():
    chat_id = -1010000
    client = FakeRelayClient(channels={"ch": chat_id}, messages={chat_id: [FakeMessage(1)]})
    client.call_errors = [ChatForwardsRestrictedError(request=None)]
    advanced: list[tuple[int, int]] = []

    ok, forwarded = await relay_mod.relay_channel(
        client, "ch", HUB_PEER, None, {str(chat_id): "0"}, lambda c, i: advanced.append((c, i))
    )

    assert ok is False
    assert forwarded == 0
    assert advanced == []


async def test_relay_channel_unresolvable_channel_returns_false():
    client = FakeRelayClient(
        channels={}, messages={}, get_entity_errors={"ghost": ValueError("nope")}
    )
    advanced: list[tuple[int, int]] = []

    ok, forwarded = await relay_mod.relay_channel(
        client, "ghost", HUB_PEER, None, {}, lambda c, i: advanced.append((c, i))
    )

    assert ok is False
    assert forwarded == 0
    assert advanced == []


# --- state: the "relay" cursors namespace ---


def test_relay_cursor_round_trips_through_commit_and_get(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_db(conn)

    commit_new_items(conn, [], {("relay", "-100123"): "7"})

    assert get_cursors(conn, "relay") == {"-100123": "7"}
    conn.close()


# --- config ---

BASE_ENV = {
    "TG_API_ID": "12345",
    "TG_API_HASH": "hash",
    "TG_SESSION": "session",
    "TG_CHAT_ALLOWLIST": "123",
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "user@example.com",
    "SMTP_PASSWORD": "pw",
    "DIGEST_FROM": "d@example.com",
    "DIGEST_TO": "me@example.com",
}


def _set_env(monkeypatch, tmp_path, overrides: dict[str, str]) -> None:
    for key, value in {**BASE_ENV, "STATE_DB_PATH": str(tmp_path / "s.db"), **overrides}.items():
        monkeypatch.setenv(key, value)
    for key in ("RELAY_TG_CHANNELS", "TELEGRAM_NOTIFY_CHAT_ID", "TELEGRAM_RELAY_THREAD_ID"):
        if key not in overrides:
            monkeypatch.delenv(key, raising=False)


class TestConfig:
    def test_relay_channels_parse_bare_usernames(self, tmp_path, monkeypatch):
        _set_env(
            monkeypatch,
            tmp_path,
            {
                "RELAY_TG_CHANNELS": "NewsChannel,other_chan",
                "TELEGRAM_NOTIFY_CHAT_ID": "-100123",
            },
        )

        assert Config.from_env().relay_tg_channels == ("NewsChannel", "other_chan")

    def test_relay_channels_reject_an_at_sign(self, tmp_path, monkeypatch):
        _set_env(monkeypatch, tmp_path, {"RELAY_TG_CHANNELS": "@NewsChannel"})

        with pytest.raises(ConfigError, match="RELAY_TG_CHANNELS"):
            Config.from_env()

    def test_relay_channels_reject_a_t_me_prefix(self, tmp_path, monkeypatch):
        _set_env(monkeypatch, tmp_path, {"RELAY_TG_CHANNELS": "t.me/NewsChannel"})

        with pytest.raises(ConfigError, match="RELAY_TG_CHANNELS"):
            Config.from_env()

    def test_relay_thread_id_unset_is_none(self, tmp_path, monkeypatch):
        _set_env(monkeypatch, tmp_path, {})

        assert Config.from_env().telegram_relay_thread_id is None

    def test_relay_thread_id_zero_is_an_explicit_group_root_choice(self, tmp_path, monkeypatch):
        _set_env(monkeypatch, tmp_path, {"TELEGRAM_RELAY_THREAD_ID": "0"})

        assert Config.from_env().telegram_relay_thread_id == 0

    def test_relay_thread_id_positive(self, tmp_path, monkeypatch):
        _set_env(monkeypatch, tmp_path, {"TELEGRAM_RELAY_THREAD_ID": "42"})

        assert Config.from_env().telegram_relay_thread_id == 42

    def test_relay_channels_require_a_notify_chat_id(self, tmp_path, monkeypatch):
        _set_env(monkeypatch, tmp_path, {"RELAY_TG_CHANNELS": "NewsChannel"})

        with pytest.raises(ConfigError, match="TELEGRAM_NOTIFY_CHAT_ID"):
            Config.from_env()

    def test_relay_channels_require_a_numeric_notify_chat_id(self, tmp_path, monkeypatch):
        _set_env(
            monkeypatch,
            tmp_path,
            {"RELAY_TG_CHANNELS": "NewsChannel", "TELEGRAM_NOTIFY_CHAT_ID": "@myhub"},
        )

        with pytest.raises(ConfigError, match="numeric chat id"):
            Config.from_env()

    def test_a_username_notify_chat_id_is_still_fine_without_relay(self, tmp_path, monkeypatch):
        # The Bot API senders accept "@username"; only relay needs the int.
        _set_env(monkeypatch, tmp_path, {"TELEGRAM_NOTIFY_CHAT_ID": "@myhub"})

        assert Config.from_env().telegram_notify_chat_id == "@myhub"

    def test_relay_channels_are_fine_without_a_bot_token(self, tmp_path, monkeypatch):
        _set_env(
            monkeypatch,
            tmp_path,
            {"RELAY_TG_CHANNELS": "NewsChannel", "TELEGRAM_NOTIFY_CHAT_ID": "-100123"},
        )
        monkeypatch.delenv("TELEGRAM_NOTIFY_BOT_TOKEN", raising=False)

        cfg = Config.from_env()

        assert cfg.relay_tg_channels == ("NewsChannel",)
        assert cfg.telegram_notify_bot_token is None


# --- run_relay ---


class TestRelayPing:
    """The bot ping that exists because the owner's own forwards never notify them."""

    def _cfg_with_bot(self, tmp_path, monkeypatch):
        _set_env(
            monkeypatch,
            tmp_path,
            {
                "RELAY_TG_CHANNELS": "newschannel",
                "TELEGRAM_NOTIFY_CHAT_ID": "-1009999",
                "TELEGRAM_NOTIFY_BOT_TOKEN": "bot-token",
                "SITE_PUBLIC_BASE": "https://news.example.com",
            },
        )
        return Config.from_env()

    def test_ping_is_sent_once_per_channel_that_forwarded(self, tmp_path, monkeypatch):
        cfg = self._cfg_with_bot(tmp_path, monkeypatch)
        sent = []
        monkeypatch.setattr(
            digest_main,
            "send_relay_ping",
            lambda channel, count, token, chat, thread, **k: sent.append((channel, count, thread)),
        )

        digest_main._ping_relay_batch(cfg, "newschannel", 3, 555)

        assert sent == [("newschannel", 3, 555)]

    def test_no_ping_when_the_notify_bot_is_unconfigured(self, tmp_path, monkeypatch):
        _set_env(
            monkeypatch,
            tmp_path,
            {"RELAY_TG_CHANNELS": "newschannel", "TELEGRAM_NOTIFY_CHAT_ID": "-1009999"},
        )
        monkeypatch.delenv("TELEGRAM_NOTIFY_BOT_TOKEN", raising=False)
        cfg = Config.from_env()

        def boom(*a, **k):
            raise AssertionError("send_relay_ping must not be called without a bot token")

        monkeypatch.setattr(digest_main, "send_relay_ping", boom)

        digest_main._ping_relay_batch(cfg, "newschannel", 1, 555)  # must not raise

    def test_a_failing_ping_never_raises(self, tmp_path, monkeypatch, caplog):
        # The forwarded posts and their cursor are already committed by the
        # time the ping runs, so a send failure must cost one notification --
        # never the run's exit code.
        cfg = self._cfg_with_bot(tmp_path, monkeypatch)

        def boom(*a, **k):
            raise TelegramSendError("telegram sendMessage failed with status 429")

        monkeypatch.setattr(digest_main, "send_relay_ping", boom)

        with caplog.at_level(logging.WARNING):
            digest_main._ping_relay_batch(cfg, "newschannel", 1, 555)

        assert "TelegramSendError" in caplog.text
        # The failure's own message must not widen the secrets posture.
        assert "429" not in caplog.text

    def test_run_relay_pings_after_a_real_forward(self, tmp_path, monkeypatch):
        cfg = self._cfg_with_bot(tmp_path, monkeypatch)
        chat_id = -1001111
        client = FakeRelayClient(
            channels={"newschannel": chat_id}, messages={chat_id: [FakeMessage(1)]}
        )
        monkeypatch.setattr(digest_main, "StringSession", lambda *a, **k: None)
        monkeypatch.setattr(digest_main, "TelegramClient", lambda *a, **k: client)
        sent = []
        monkeypatch.setattr(
            digest_main,
            "send_relay_ping",
            lambda channel, count, *a, **k: sent.append((channel, count)),
        )

        assert digest_main.run_relay(cfg) is True  # first run seeds
        assert sent == []  # nothing forwarded, so nothing to announce

        client.messages[chat_id].extend([FakeMessage(2), FakeMessage(3)])
        assert digest_main.run_relay(cfg) is True

        assert sent == [("newschannel", 2)]


class TestRunRelay:
    def test_unconfigured_is_success_and_never_touches_telegram(self, tmp_path, monkeypatch):
        _set_env(monkeypatch, tmp_path, {})

        def boom(*args, **kwargs):
            raise AssertionError(
                "TelegramClient must not be constructed when relay is unconfigured"
            )

        monkeypatch.setattr(digest_main, "TelegramClient", boom)

        assert digest_main.run_relay(Config.from_env()) is True

    def test_first_run_seeds_then_second_run_forwards_and_advances_the_cursor(
        self, tmp_path, monkeypatch
    ):
        _set_env(
            monkeypatch,
            tmp_path,
            {"RELAY_TG_CHANNELS": "newschannel", "TELEGRAM_NOTIFY_CHAT_ID": "-1009999"},
        )
        chat_id = -1001111
        client = FakeRelayClient(
            channels={"newschannel": chat_id}, messages={chat_id: [FakeMessage(1)]}
        )
        monkeypatch.setattr(digest_main, "StringSession", lambda *a, **k: None)
        monkeypatch.setattr(digest_main, "TelegramClient", lambda *a, **k: client)
        cfg = Config.from_env()

        assert digest_main.run_relay(cfg) is True
        conn = connect(cfg.state_db_path)
        assert get_cursors(conn, "relay") == {str(chat_id): "1"}
        conn.close()
        assert client.requests == []

        client.messages[chat_id].append(FakeMessage(2))
        client.messages[chat_id].append(FakeMessage(3))

        assert digest_main.run_relay(cfg) is True
        assert len(client.requests) == 1
        assert client.requests[0].id == [2, 3]
        conn = connect(cfg.state_db_path)
        assert get_cursors(conn, "relay") == {str(chat_id): "3"}
        conn.close()

    @pytest.mark.parametrize(
        ("relay_thread", "notify_thread", "expected_top_msg_id"),
        [
            # Relay topic set: it wins outright.
            ("42", "5", 42),
            # Relay topic unset: falls back to the window digest's topic.
            (None, "5", 5),
            # Explicit 0 (group root) must reach Telegram as an OMITTED
            # top_msg_id, never a literal 0.
            ("0", "5", None),
            # Nothing configured anywhere: group root.
            (None, None, None),
        ],
    )
    def test_top_msg_id_follows_the_thread_id_fallback_contract(
        self, tmp_path, monkeypatch, relay_thread, notify_thread, expected_top_msg_id
    ):
        overrides = {"RELAY_TG_CHANNELS": "newschannel", "TELEGRAM_NOTIFY_CHAT_ID": "-1009999"}
        if relay_thread is not None:
            overrides["TELEGRAM_RELAY_THREAD_ID"] = relay_thread
        if notify_thread is not None:
            overrides["TELEGRAM_NOTIFY_THREAD_ID"] = notify_thread
        else:
            monkeypatch.delenv("TELEGRAM_NOTIFY_THREAD_ID", raising=False)
        _set_env(monkeypatch, tmp_path, overrides)
        chat_id = -1001111
        client = FakeRelayClient(
            channels={"newschannel": chat_id}, messages={chat_id: [FakeMessage(1)]}
        )
        monkeypatch.setattr(digest_main, "StringSession", lambda *a, **k: None)
        monkeypatch.setattr(digest_main, "TelegramClient", lambda *a, **k: client)
        cfg = Config.from_env()
        assert digest_main.run_relay(cfg) is True  # seeds
        client.messages[chat_id].append(FakeMessage(2))

        assert digest_main.run_relay(cfg) is True

        assert client.requests[0].top_msg_id == expected_top_msg_id

    def test_hub_peer_is_resolved_from_the_numeric_notify_chat_id(self, tmp_path, monkeypatch):
        _set_env(
            monkeypatch,
            tmp_path,
            {"RELAY_TG_CHANNELS": "newschannel", "TELEGRAM_NOTIFY_CHAT_ID": "-1009999"},
        )
        chat_id = -1001111
        client = FakeRelayClient(
            channels={"newschannel": chat_id}, messages={chat_id: [FakeMessage(1)]}
        )
        monkeypatch.setattr(digest_main, "StringSession", lambda *a, **k: None)
        monkeypatch.setattr(digest_main, "TelegramClient", lambda *a, **k: client)
        cfg = Config.from_env()
        assert digest_main.run_relay(cfg) is True  # seeds
        client.messages[chat_id].append(FakeMessage(2))

        assert digest_main.run_relay(cfg) is True

        # FakeRelayClient.get_input_entity is the identity, so the peer that
        # reached the request is exactly int(TELEGRAM_NOTIFY_CHAT_ID).
        assert client.requests[0].to_peer == -1009999
        assert client.get_dialogs_calls == 2  # once per run, before resolving the hub


# --- main() argv dispatch ---


def test_main_dispatches_relay_argv_to_run_relay(tmp_path, monkeypatch):
    _set_env(
        monkeypatch,
        tmp_path,
        {"RELAY_TG_CHANNELS": "newschannel", "TELEGRAM_NOTIFY_CHAT_ID": "-100123"},
    )
    monkeypatch.setattr(sys, "argv", ["digest", "relay"])
    calls = []
    monkeypatch.setattr(digest_main, "run_relay", lambda cfg: calls.append(cfg) or True)

    with pytest.raises(SystemExit) as exc_info:
        digest_main.main()

    assert exc_info.value.code == 0
    assert len(calls) == 1
