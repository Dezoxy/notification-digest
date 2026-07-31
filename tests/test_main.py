import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

import digest.main as main_mod
from digest.collectors.telegram import CollectResult
from digest.config import Config
from digest.main import (
    _client_ready,
    _deliver,
    _run_news_collector,
    _run_x_collector,
    _send_and_finalize,
)
from digest.state import (
    Item,
    commit_new_items,
    connect,
    count_unsummarized_items,
    create_digest,
    get_pending_digest,
    get_unsummarized_items,
    init_db,
)


class FakeReadyClient:
    """Minimal fake honoring the connect/is_user_authorized surface _client_ready uses."""

    def __init__(
        self,
        connect_error: Exception | None = None,
        authorized: bool = True,
    ):
        self.connect_error = connect_error
        self.authorized = authorized
        self.connect_calls = 0
        self.is_user_authorized_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error

    async def is_user_authorized(self) -> bool:
        self.is_user_authorized_calls += 1
        return self.authorized


async def test_client_ready_connect_ok_and_authorized_returns_true():
    client = FakeReadyClient(authorized=True)

    assert await _client_ready(client) is True
    assert client.connect_calls == 1
    assert client.is_user_authorized_calls == 1


async def test_client_ready_connect_ok_but_not_authorized_returns_false():
    client = FakeReadyClient(authorized=False)

    assert await _client_ready(client) is False
    assert client.connect_calls == 1
    assert client.is_user_authorized_calls == 1


async def test_client_ready_connect_raises_returns_false():
    client = FakeReadyClient(connect_error=ConnectionError("network down"))

    assert await _client_ready(client) is False
    assert client.connect_calls == 1
    assert client.is_user_authorized_calls == 0


# --- _deliver (Phase 2 post-collection pipeline) ---


def _cfg() -> Config:
    return Config(
        tg_api_id=1,
        tg_api_hash="hash",
        tg_session="session",
        tg_chat_allowlist=(123,),
        smtp_host="smtp.mail.me.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="app-password",
        digest_from="digest@4rgus.com",
        digest_to="me@toomhorvath.com",
        anthropic_model="claude-opus-5",
        archive_dir="./archive",
        claude_timeout_seconds=300,
        claude_effort="high",
    )


def _item(source_id: str = "1", fetched_at: str = "2026-07-29T10:00:00+00:00") -> Item:
    return Item(
        source="telegram",
        source_id=source_id,
        chat_id="123",
        author="alice",
        text="hello",
        url=f"https://t.me/c/123/{source_id}",
        fetched_at=fetched_at,
    )


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(str(tmp_path / "state.db"))
    init_db(c)
    yield c
    c.close()


def test_deliver_retries_pending_digest_and_never_calls_summarize(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "## Needs attention\n...", get_unsummarized_items(conn))

    def boom_summarize(*args, **kwargs):
        raise AssertionError("summarize must not be called when a digest is pending resend")

    sent = {}

    def fake_send_digest(host, port, user, password, from_, to, subject, body_md, allowed_urls):
        sent["body_md"] = body_md
        sent["allowed_urls"] = allowed_urls

    archived = {}

    def fake_archive(body_md, archive_dir, digest_id):
        archived["digest_id"] = digest_id

    monkeypatch.setattr(main_mod, "summarize", boom_summarize)
    monkeypatch.setattr(main_mod, "send_digest", fake_send_digest)
    monkeypatch.setattr(main_mod, "archive", fake_archive)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    assert sent["body_md"] == "## Needs attention\n..."
    assert sent["allowed_urls"] == {"https://t.me/c/123/1"}
    assert archived["digest_id"] == digest_id
    assert get_pending_digest(conn) is None  # marked sent


def test_deliver_zero_unsummarized_items_sends_nothing(conn, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("must not be called when there is nothing to deliver")

    monkeypatch.setattr(main_mod, "summarize", boom)
    monkeypatch.setattr(main_mod, "send_digest", boom)
    monkeypatch.setattr(main_mod, "archive", boom)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    assert get_pending_digest(conn) is None


def test_deliver_smtp_failure_leaves_digest_row_unsent(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})

    monkeypatch.setattr(main_mod, "summarize", lambda *a, **k: "## Needs attention\n...")

    def failing_send(*args, **kwargs):
        raise OSError("smtp connection refused")

    archived = {"called": False}
    monkeypatch.setattr(main_mod, "send_digest", failing_send)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: archived.__setitem__("called", True))

    ok = _deliver(conn, _cfg(), [])

    assert ok is False
    pending = get_pending_digest(conn)
    assert pending is not None
    digest_id, body_md = pending
    assert body_md == "## Needs attention\n..."
    assert archived["called"] is False  # never reached on send failure


def test_deliver_success_path_creates_digest_sends_marks_sent_and_archives(conn, monkeypatch):
    commit_new_items(conn, [_item("1"), _item("2")], {("telegram", "123"): "2"})

    summarize_calls = []

    def fake_summarize(items, failed_sources, model, timeout_seconds, effort):
        # cfg.claude_effort must reach summarize() unchanged -- the only hop
        # between Config.claude_effort and the eventual `--effort` argv flag
        # in digest/summarize.py's run_claude.
        summarize_calls.append(effort)
        return "## Needs attention\n..."

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)

    sent = {}
    monkeypatch.setattr(
        main_mod,
        "send_digest",
        lambda host, port, user, pw, from_, to, subject, body_md, allowed_urls: sent.update(
            subject=subject, body_md=body_md, allowed_urls=allowed_urls
        ),
    )
    archived = {}
    monkeypatch.setattr(
        main_mod, "archive", lambda body_md, archive_dir, digest_id: archived.update(id=digest_id)
    )

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    assert summarize_calls == ["high"]  # cfg.claude_effort threaded through
    assert sent["body_md"] == "## Needs attention\n..."
    assert "2 items" in sent["subject"]
    assert sent["allowed_urls"] == {"https://t.me/c/123/1", "https://t.me/c/123/2"}
    assert get_pending_digest(conn) is None
    assert archived["id"] == 1


def test_deliver_pending_digest_and_new_items_sends_both_in_same_run(conn, monkeypatch):
    # A pending digest from a previous run plus freshly collected items that
    # are still unsummarized: the pending resend must not swallow this run's
    # own items -- both must go out, as two separate sends.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    pending_digest_id = create_digest(
        conn, "## Needs attention\n...pending...", get_unsummarized_items(conn)
    )

    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})

    summarize_calls = []

    def fake_summarize(items, failed_sources, model, timeout_seconds, effort):
        summarize_calls.append((items, failed_sources))
        return "## Needs attention\n...new..."

    sends = []

    def fake_send_digest(host, port, user, password, from_, to, subject, body_md, allowed_urls):
        sends.append(body_md)

    archived = []
    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "send_digest", fake_send_digest)
    monkeypatch.setattr(
        main_mod, "archive", lambda body_md, archive_dir, digest_id: archived.append(digest_id)
    )

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    assert len(sends) == 2
    assert sends[0] == "## Needs attention\n...pending..."
    assert sends[1] == "## Needs attention\n...new..."
    assert len(summarize_calls) == 1  # only for the new items, never the pending digest
    assert get_pending_digest(conn) is None
    assert pending_digest_id in archived


def test_deliver_pending_digest_sent_then_current_collection_failed_passes_failed_sources(
    conn, monkeypatch
):
    # The pending resend succeeds, but THIS run's own collection failed --
    # the new digest created for this run's items must carry this run's own
    # failed_sources, not an empty list.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    create_digest(conn, "## Needs attention\n...pending...", get_unsummarized_items(conn))

    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})

    summarize_calls = []

    def fake_summarize(items, failed_sources, model, timeout_seconds, effort):
        summarize_calls.append(failed_sources)
        return "## Needs attention\n...new..."

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), ["telegram"])

    assert ok is True
    assert summarize_calls == [["telegram"]]


def test_deliver_pending_digest_send_fails_no_summarize_and_returns_false(conn, monkeypatch):
    # If the pending resend itself fails, we must not attempt to summarize
    # or send a second email on what's evidently a broken SMTP path.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    create_digest(conn, "## Needs attention\n...pending...", get_unsummarized_items(conn))

    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})

    def boom_summarize(*args, **kwargs):
        raise AssertionError("summarize must not be called when the pending resend fails")

    def failing_send(*args, **kwargs):
        raise OSError("smtp connection refused")

    monkeypatch.setattr(main_mod, "summarize", boom_summarize)
    monkeypatch.setattr(main_mod, "send_digest", failing_send)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), [])

    assert ok is False
    pending = get_pending_digest(conn)
    assert pending is not None  # still unsent, left for the next run's retry


def test_deliver_bounds_batch_to_max_items_per_digest_leaving_remainder_unsummarized(
    conn, monkeypatch
):
    # P1 finding: an unbounded backlog can exceed the model context and wedge
    # the pipeline forever. With more unsummarized items than the per-run
    # cap, _deliver must hand summarize exactly the cap-sized OLDEST batch,
    # send it successfully, and leave the remainder unsummarized for the
    # next run -- never looping summarize within a single run.
    monkeypatch.setattr(main_mod, "_MAX_ITEMS_PER_DIGEST", 2)

    commit_new_items(
        conn,
        [
            _item("1", fetched_at="2026-07-29T10:00:00+00:00"),
            _item("2", fetched_at="2026-07-29T11:00:00+00:00"),
            _item("3", fetched_at="2026-07-29T12:00:00+00:00"),
        ],
        {("telegram", "123"): "3"},
    )

    summarize_calls = []

    def fake_summarize(items, failed_sources, model, timeout_seconds, effort):
        summarize_calls.append(items)
        return "## Needs attention\n...batch..."

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    assert len(summarize_calls) == 1  # exactly one Opus call this run
    assert [i.source_id for i in summarize_calls[0]] == ["1", "2"]  # oldest batch, size-bounded
    assert count_unsummarized_items(conn) == 1  # item "3" left for the next run
    assert [i.source_id for i in get_unsummarized_items(conn)] == ["3"]


def test_deliver_passes_the_same_selected_subset_to_summarize_and_create_digest(
    conn, monkeypatch
):
    # P1 fix: select_items_for_prompt must run BEFORE both summarize() and
    # create_digest(), and both must receive its output -- not the
    # pre-shrink batch -- so the summarized set and the stamped set never
    # diverge. Monkeypatching select_items_for_prompt to a known,
    # deliberately different subset (just item "2", dropping item "1")
    # proves this: if create_digest were still called with the full
    # pre-shrink batch, item "1" would show up stamped despite never having
    # been sent to summarize.
    commit_new_items(
        conn,
        [_item("1"), _item("2")],
        {("telegram", "123"): "2"},
    )
    full_batch = get_unsummarized_items(conn)
    assert [i.source_id for i in full_batch] == ["1", "2"]

    selected_subset = [i for i in full_batch if i.source_id == "2"]

    def fake_select_items_for_prompt(items, failed_sources, max_prompt_bytes):
        return selected_subset

    summarize_received = {}

    def fake_summarize(items, failed_sources, model, timeout_seconds, effort):
        summarize_received["items"] = items
        return "## Needs attention\n...selected..."

    monkeypatch.setattr(main_mod, "select_items_for_prompt", fake_select_items_for_prompt)
    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    assert summarize_received["items"] == selected_subset

    # create_digest stamped only the selected subset -- item "1" must remain
    # unsummarized, not silently marked handled despite never being
    # summarized.
    assert count_unsummarized_items(conn) == 1
    assert [i.source_id for i in get_unsummarized_items(conn)] == ["1"]


# --- _send_and_finalize (link-provenance allowlist wiring, P1 fix) ---


def test_send_and_finalize_passes_the_digests_stamped_item_urls_to_send_digest(
    conn, monkeypatch
):
    # P1 fix: send_digest's HTML link-provenance allowlist must come from
    # the digest's own stamped items (get_digest_item_urls), fetched fresh
    # off the `items` table rather than threaded through from an in-memory
    # Item list -- that's what makes the same code path work for a fresh
    # digest and a later pending resend alike (see get_digest_item_urls's
    # docstring in digest/state.py). Seeded against a real tmp DB so the
    # stamping performed by create_digest is exercised for real, not faked.
    commit_new_items(
        conn,
        [_item("1"), _item("2")],
        {("telegram", "123"): "2"},
    )
    items = get_unsummarized_items(conn)
    digest_id = create_digest(conn, "## Needs attention\n...", items)

    captured = {}

    def fake_send_digest(host, port, user, password, from_, to, subject, body_md, allowed_urls):
        captured["allowed_urls"] = allowed_urls

    monkeypatch.setattr(main_mod, "send_digest", fake_send_digest)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _send_and_finalize(conn, _cfg(), digest_id, "## Needs attention\n...", len(items))

    assert ok is True
    assert captured["allowed_urls"] == {"https://t.me/c/123/1", "https://t.me/c/123/2"}


def test_send_and_finalize_recovers_urls_for_a_pending_resend_from_a_prior_run(
    conn, monkeypatch
):
    # Simulates the pending-resend path: this call has no in-memory Item
    # list at all (unlike the fresh-digest path) -- it only has digest_id
    # and body_md read back off the `digests` table, exactly like
    # get_pending_digest returns in _deliver. The allowlist must still be
    # recoverable purely from the digest_id, via the items already stamped
    # by a create_digest call that ran in a "previous run" (here, earlier
    # in this test, but nothing about _send_and_finalize depends on that).
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(
        conn, "## Needs attention\n...pending...", get_unsummarized_items(conn)
    )

    captured = {}

    def fake_send_digest(host, port, user, password, from_, to, subject, body_md, allowed_urls):
        captured["allowed_urls"] = allowed_urls

    monkeypatch.setattr(main_mod, "send_digest", fake_send_digest)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _send_and_finalize(conn, _cfg(), digest_id, "## Needs attention\n...pending...", 1)

    assert ok is True
    assert captured["allowed_urls"] == {"https://t.me/c/123/1"}


# --- _run: Phase 3 collector orchestration (Telegram + X merge, failed_sources) ---
#
# These patch main_mod.TelegramClient / main_mod.StringSession (real Telethon
# construction needs a real session string and would otherwise raise) and
# main_mod.telegram_collector.collect / main_mod.x_collector.build_client /
# main_mod.x_collector.collect (the actual collector entry points _run calls),
# then run the real `_run` coroutine end to end against a tmp-path SQLite db.


class FakeTelegramClient:
    """Stands in for TelegramClient(...) inside _run -- never touches the network."""

    def __init__(self, *args, **kwargs):
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def is_user_authorized(self) -> bool:
        return True

    def is_connected(self) -> bool:
        return self._connected

    async def disconnect(self) -> None:
        self._connected = False


def _patch_telegram_client(monkeypatch, tg_result: CollectResult) -> None:
    monkeypatch.setattr(main_mod, "StringSession", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "TelegramClient", lambda *a, **k: FakeTelegramClient())

    async def fake_collect(client, chat_ids, cursors):
        return tg_result

    monkeypatch.setattr(main_mod.telegram_collector, "collect", fake_collect)


def _patch_x_client(monkeypatch, x_result: CollectResult, *, build_client_raises: bool = False):
    def fake_build_client(cookies_path, cookies_inline):
        if build_client_raises:
            raise AssertionError("x_collector.build_client must not be called")
        return object()

    async def fake_collect(client, cursors):
        return x_result

    monkeypatch.setattr(main_mod.x_collector, "build_client", fake_build_client)
    monkeypatch.setattr(main_mod.x_collector, "collect", fake_collect)


def test_run_x_disabled_never_calls_x_collector(monkeypatch, tmp_path):
    cfg = replace(_cfg(), state_db_path=str(tmp_path / "state.db"), x_enabled=False)

    _patch_telegram_client(monkeypatch, CollectResult())

    def boom_build_client(*a, **k):
        raise AssertionError("build_client must not be called when X_ENABLED=false")

    async def boom_collect(*a, **k):
        raise AssertionError("x collect must not be called when X_ENABLED=false")

    monkeypatch.setattr(main_mod.x_collector, "build_client", boom_build_client)
    monkeypatch.setattr(main_mod.x_collector, "collect", boom_collect)

    ok = asyncio.run(main_mod._run(cfg))

    assert ok is True


def test_run_merges_telegram_and_x_items_into_one_commit(monkeypatch, tmp_path):
    cfg = replace(
        _cfg(),
        state_db_path=str(tmp_path / "state.db"),
        x_enabled=True,
        x_cookies_path="/tmp/x-cookies.json",
    )

    tg_item = _item("1")
    x_item = Item(
        source="x",
        source_id="999",
        chat_id=None,
        author="bob",
        text="hey",
        url="https://x.com/bob/status/999",
        fetched_at="2026-07-29T10:00:00+00:00",
    )

    _patch_telegram_client(
        monkeypatch, CollectResult(items=[tg_item], cursor_updates={("telegram", "123"): "1"})
    )
    _patch_x_client(
        monkeypatch, CollectResult(items=[x_item], cursor_updates={("x", "notifications"): "999"})
    )

    captured = {}

    def fake_commit_new_items(conn, items, cursor_updates):
        captured["items"] = items
        captured["cursor_updates"] = cursor_updates
        return len(items)

    monkeypatch.setattr(main_mod, "commit_new_items", fake_commit_new_items)
    monkeypatch.setattr(main_mod, "_deliver", lambda conn, cfg, failed_sources: True)

    ok = asyncio.run(main_mod._run(cfg))

    assert ok is True
    assert captured["items"] == [tg_item, x_item]
    assert captured["cursor_updates"] == {
        ("telegram", "123"): "1",
        ("x", "notifications"): "999",
    }


@pytest.mark.parametrize(
    "tg_failed,x_failed,expected_failed_sources",
    [
        (True, False, ["telegram"]),
        (False, True, ["x"]),
        (True, True, ["telegram", "x"]),
        (False, False, []),
    ],
)
def test_run_failed_sources_reflects_exactly_the_failing_collectors(
    monkeypatch, tmp_path, tg_failed, x_failed, expected_failed_sources
):
    cfg = replace(
        _cfg(),
        state_db_path=str(tmp_path / "state.db"),
        x_enabled=True,
        x_cookies_path="/tmp/x-cookies.json",
    )

    _patch_telegram_client(monkeypatch, CollectResult(failed=tg_failed))
    _patch_x_client(monkeypatch, CollectResult(failed=x_failed))

    captured = {}

    def fake_deliver(conn, cfg, failed_sources):
        captured["failed_sources"] = failed_sources
        return True

    monkeypatch.setattr(main_mod, "_deliver", fake_deliver)

    ok = asyncio.run(main_mod._run(cfg))

    assert captured["failed_sources"] == expected_failed_sources
    assert ok == (not expected_failed_sources)


# --- Codex review finding A: an unexpected x_collector.collect() crash must
# never take down the whole run (telegram's already-collected items still
# need to reach commit_new_items).


def test_run_x_collector_crash_is_caught_returns_failed_result(monkeypatch, tmp_path):
    cfg = replace(
        _cfg(),
        state_db_path=str(tmp_path / "state.db"),
        x_enabled=True,
        x_cookies_path="/tmp/x-cookies.json",
    )
    conn = connect(cfg.state_db_path)
    init_db(conn)

    def fake_build_client(cookies_path, cookies_inline):
        return object()

    async def boom_collect(client, cursors):
        raise RuntimeError("twikit graphql shape changed")

    monkeypatch.setattr(main_mod.x_collector, "build_client", fake_build_client)
    monkeypatch.setattr(main_mod.x_collector, "collect", boom_collect)

    result = asyncio.run(_run_x_collector(conn, cfg))

    assert result.failed is True
    assert result.items == []
    assert result.cursor_updates == {}
    conn.close()


def test_run_x_collector_crash_does_not_prevent_telegram_items_from_committing(
    monkeypatch, tmp_path
):
    cfg = replace(
        _cfg(),
        state_db_path=str(tmp_path / "state.db"),
        x_enabled=True,
        x_cookies_path="/tmp/x-cookies.json",
    )

    tg_item = _item("1")
    _patch_telegram_client(
        monkeypatch, CollectResult(items=[tg_item], cursor_updates={("telegram", "123"): "1"})
    )

    def fake_build_client(cookies_path, cookies_inline):
        return object()

    async def boom_collect(client, cursors):
        raise RuntimeError("twikit graphql shape changed")

    monkeypatch.setattr(main_mod.x_collector, "build_client", fake_build_client)
    monkeypatch.setattr(main_mod.x_collector, "collect", boom_collect)

    captured = {}

    def fake_commit_new_items(conn, items, cursor_updates):
        captured["items"] = items
        captured["cursor_updates"] = cursor_updates
        return len(items)

    monkeypatch.setattr(main_mod, "commit_new_items", fake_commit_new_items)
    monkeypatch.setattr(main_mod, "_deliver", lambda conn, cfg, failed_sources: True)

    ok = asyncio.run(main_mod._run(cfg))

    assert ok is False
    assert captured["items"] == [tg_item]
    assert captured["cursor_updates"] == {("telegram", "123"): "1"}


# --- _run_news_collector (news collector, Config.news_feeds-gated) ---


def test_run_news_collector_empty_feeds_never_calls_rss_collect(monkeypatch):
    cfg = _cfg()
    assert cfg.news_feeds == ()

    def boom_collect(feed_urls):
        raise AssertionError("rss_collector.collect must not be called when news_feeds is empty")

    monkeypatch.setattr(main_mod.rss_collector, "collect", boom_collect)

    result = _run_news_collector(cfg)

    assert result == CollectResult()


def test_run_news_collector_delegates_to_rss_collect_with_configured_feeds(monkeypatch):
    cfg = replace(_cfg(), news_feeds=("https://example.com/feed.xml",))
    expected = CollectResult(items=[_item("news-1")])

    def fake_collect(feed_urls):
        assert feed_urls == cfg.news_feeds
        return expected

    monkeypatch.setattr(main_mod.rss_collector, "collect", fake_collect)

    assert _run_news_collector(cfg) is expected


def test_run_news_collector_crash_is_caught_returns_failed_result(monkeypatch):
    cfg = replace(_cfg(), news_feeds=("https://example.com/feed.xml",))

    def boom_collect(feed_urls):
        raise RuntimeError("feedparser exploded")

    monkeypatch.setattr(main_mod.rss_collector, "collect", boom_collect)

    result = _run_news_collector(cfg)

    assert result.failed is True
    assert result.items == []
    assert result.cursor_updates == {}


# --- _run: news wiring end to end (merge into commit, failed_sources) ---


def test_run_merges_news_items_into_commit_alongside_telegram(monkeypatch, tmp_path):
    cfg = replace(
        _cfg(),
        state_db_path=str(tmp_path / "state.db"),
        news_feeds=("https://example.com/feed.xml",),
    )

    tg_item = _item("1")
    news_item = Item(
        source="news",
        source_id="guid-1",
        chat_id=None,
        chat_title="AI Weekly",
        author=None,
        text="headline\n\nsummary",
        url="https://example.com/article",
        fetched_at="2026-07-29T10:00:00+00:00",
    )

    _patch_telegram_client(
        monkeypatch, CollectResult(items=[tg_item], cursor_updates={("telegram", "123"): "1"})
    )

    def fake_news_collect(feed_urls):
        assert feed_urls == cfg.news_feeds
        return CollectResult(items=[news_item])

    monkeypatch.setattr(main_mod.rss_collector, "collect", fake_news_collect)

    captured = {}

    def fake_commit_new_items(conn, items, cursor_updates):
        captured["items"] = items
        captured["cursor_updates"] = cursor_updates
        return len(items)

    monkeypatch.setattr(main_mod, "commit_new_items", fake_commit_new_items)
    monkeypatch.setattr(main_mod, "_deliver", lambda conn, cfg, failed_sources: True)

    ok = asyncio.run(main_mod._run(cfg))

    assert ok is True
    assert captured["items"] == [tg_item, news_item]
    # news never contributes a cursor_update -- only telegram's shows up.
    assert captured["cursor_updates"] == {("telegram", "123"): "1"}


def test_run_news_collector_failure_surfaces_in_failed_sources(monkeypatch, tmp_path):
    cfg = replace(
        _cfg(),
        state_db_path=str(tmp_path / "state.db"),
        news_feeds=("https://example.com/feed.xml",),
    )

    _patch_telegram_client(monkeypatch, CollectResult())
    monkeypatch.setattr(
        main_mod.rss_collector, "collect", lambda feed_urls: CollectResult(failed=True)
    )

    captured = {}

    def fake_deliver(conn, cfg, failed_sources):
        captured["failed_sources"] = failed_sources
        return True

    monkeypatch.setattr(main_mod, "_deliver", fake_deliver)

    ok = asyncio.run(main_mod._run(cfg))

    assert captured["failed_sources"] == ["news"]
    assert ok is False
