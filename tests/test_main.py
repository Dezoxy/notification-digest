import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import digest.main as main_mod
from digest.collectors.polymarket import PolymarketCollectResult
from digest.collectors.telegram import CollectResult
from digest.config import Config
from digest.main import (
    _client_ready,
    _deliver,
    _deliver_channels,
    _deliver_email,
    _deliver_site,
    _deliver_telegram,
    _run_news_collector,
    _run_polymarket_collector,
    _run_x_collector,
    _TelegramRunState,
)
from digest.publish import TelegramSendError
from digest.state import (
    Item,
    commit_new_items,
    connect,
    count_unsummarized_items,
    create_digest,
    get_digest_item_urls,
    get_pending_digest,
    get_unsummarized_items,
    init_db,
)

_NO_CHANNELS_DONE = {"email": False, "site": False, "telegram": False}


def _fresh_telegram_state() -> _TelegramRunState:
    """A brand-new, untripped circuit breaker -- most tests below want one call's worth."""
    return _TelegramRunState()


def _recent_created_at(hours_ago: float = 1) -> str:
    """An ISO8601 UTC created_at `hours_ago` in the past, relative to the real wall clock.

    Used (instead of a fixed historical literal like "2026-07-29T...") by
    every test that exercises a Telegram SEND path: digest/main.py's GUARD 1
    freshness window (`_TELEGRAM_MAX_AGE`) compares `created_at` against
    `datetime.now(UTC)` at call time, so a fixed literal would eventually
    age out of the window and start failing these tests for a reason that
    has nothing to do with the behavior under test. Default of 1 hour ago is
    comfortably inside the 12h window.
    """
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


def _stale_created_at(hours_ago: float = 13) -> str:
    """An ISO8601 UTC created_at `hours_ago` ago -- outside GUARD 1's 12h window by default."""
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


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
        digest_from_name="Digest",
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
    create_digest(conn, "## Needs attention\n...", get_unsummarized_items(conn))

    def boom_summarize(*args, **kwargs):
        raise AssertionError("summarize must not be called when a digest is pending resend")

    sent = {}

    def fake_send_digest(
        host,
        port,
        user,
        password,
        from_,
        from_name,
        to,
        subject,
        body_md,
        allowed_urls,
        generated_at_label,
    ):
        sent["body_md"] = body_md
        sent["allowed_urls"] = allowed_urls

    def boom_archive(*args, **kwargs):
        raise AssertionError(
            "archive must not run on a pending resend -- it already ran once, "
            "at this digest's original creation in a previous run"
        )

    monkeypatch.setattr(main_mod, "summarize", boom_summarize)
    monkeypatch.setattr(main_mod, "send_digest", fake_send_digest)
    monkeypatch.setattr(main_mod, "archive", boom_archive)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    assert sent["body_md"] == "## Needs attention\n..."
    assert sent["allowed_urls"] == {"https://t.me/c/123/1"}
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
    # P1 spec change: archive() now runs once, unconditionally, right after
    # create_digest -- it is no longer gated on any channel's delivery
    # succeeding (a channel failing must not also lose the archive copy).
    assert archived["called"] is True


def test_deliver_success_path_creates_digest_sends_marks_sent_and_archives(conn, monkeypatch):
    commit_new_items(conn, [_item("1"), _item("2")], {("telegram", "123"): "2"})

    summarize_calls = []

    def fake_summarize(items, failed_sources, recent_coverage, model, timeout_seconds, effort):
        # cfg.claude_effort must reach summarize() unchanged -- the only hop
        # between Config.claude_effort and the eventual `--effort` argv flag
        # in digest/summarize.py's run_claude.
        summarize_calls.append(effort)
        return "## Needs attention\n..."

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)

    sent = {}

    def fake_send_digest(
        host,
        port,
        user,
        pw,
        from_,
        from_name,
        to,
        subject,
        body_md,
        allowed_urls,
        generated_at_label,
    ):
        sent.update(
            subject=subject,
            body_md=body_md,
            allowed_urls=allowed_urls,
            generated_at_label=generated_at_label,
        )

    monkeypatch.setattr(main_mod, "send_digest", fake_send_digest)
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
    # Exact formatting is emailer/render_html's concern (tested there); here
    # we only need proof _send_and_finalize actually built and threaded a
    # real label through, not the empty-string default of an unwired param.
    assert isinstance(sent["generated_at_label"], str)
    assert sent["generated_at_label"] != ""
    assert get_pending_digest(conn) is None
    assert archived["id"] == 1


def test_deliver_threads_real_recent_coverage_from_prior_digests(conn, monkeypatch):
    # Wiring test: _deliver must DERIVE recent_coverage from the digests
    # table (get_recent_digests -> format_recent_coverage), not just accept
    # the parameter. Without this, a regression that always passes "" (or
    # drops the get_recent_digests call) would leave every signature-level
    # test green while silently disabling the running-story-memory feature.
    prior_items = [_item("90")]
    commit_new_items(conn, prior_items, {("telegram", "123"): "90"})
    create_digest(conn, "## Prior story headline\n\nOld coverage.", prior_items)

    commit_new_items(conn, [_item("91")], {("telegram", "123"): "91"})

    captured = {}

    def fake_summarize(items, failed_sources, recent_coverage, model, timeout_seconds, effort):
        captured["recent_coverage"] = recent_coverage
        return "## Needs attention\n..."

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    # The prior digest was created moments ago by this test, so it is inside
    # the 24h window and its heading must appear with an age label.
    assert "Prior story headline" in captured["recent_coverage"]
    assert "<1h ago: " in captured["recent_coverage"]


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

    def fake_summarize(items, failed_sources, recent_coverage, model, timeout_seconds, effort):
        summarize_calls.append((items, failed_sources))
        return "## Needs attention\n...new..."

    sends = []

    def fake_send_digest(
        host,
        port,
        user,
        password,
        from_,
        from_name,
        to,
        subject,
        body_md,
        allowed_urls,
        generated_at_label,
    ):
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
    # The pending digest was archived when IT was created, in a (simulated)
    # previous run -- only the freshly created digest is archived here.
    assert pending_digest_id not in archived
    assert archived == [pending_digest_id + 1]


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

    def fake_summarize(items, failed_sources, recent_coverage, model, timeout_seconds, effort):
        summarize_calls.append(failed_sources)
        return "## Needs attention\n...new..."

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), ["telegram"])

    assert ok is True
    assert summarize_calls == [["telegram"]]


def test_deliver_pending_digest_send_fails_new_items_still_summarized_and_delivered(
    conn, monkeypatch
):
    # P1 spec change (multi-channel delivery refactor): channels -- and,
    # by extension, DIGESTS -- are independent. A broken SMTP path on one
    # pending digest must not stop this run from summarizing and attempting
    # delivery of its own freshly collected items; the old single-channel
    # behavior bailed out early here specifically because there was only
    # ever one channel and no point in a second doomed send. That
    # early-bail no longer applies: this run now summarizes and attempts
    # the new digest too (its email attempt fails the same way, since
    # send_digest is broken for the whole run either way), and the overall
    # result is False because BOTH digests are left pending.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    pending_digest_id = create_digest(
        conn, "## Needs attention\n...pending...", get_unsummarized_items(conn)
    )

    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})

    summarize_calls = []

    def fake_summarize(items, failed_sources, recent_coverage, model, timeout_seconds, effort):
        summarize_calls.append(items)
        return "## Needs attention\n...new..."

    def failing_send(*args, **kwargs):
        raise OSError("smtp connection refused")

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "send_digest", failing_send)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), [])

    assert ok is False
    assert len(summarize_calls) == 1  # the new item WAS summarized despite the pending failure
    pending = get_pending_digest(conn)
    assert pending is not None  # still unsent, left for the next run's retry
    # Both the original pending digest and the newly created one remain
    # pending -- neither one's email attempt succeeded.
    row_count = conn.execute("SELECT COUNT(*) FROM digests WHERE email_sent = 0").fetchone()[0]
    assert row_count == 2
    assert pending_digest_id != pending[0]  # the newest-pending id is the freshly created one


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

    def fake_summarize(items, failed_sources, recent_coverage, model, timeout_seconds, effort):
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

    def fake_select_items_for_prompt(items, failed_sources, recent_coverage, max_prompt_bytes):
        return selected_subset

    summarize_received = {}

    def fake_summarize(items, failed_sources, recent_coverage, model, timeout_seconds, effort):
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


# --- _deliver_email / _deliver_channels (link-provenance allowlist wiring, P1 fix) ---


def test_deliver_email_passes_the_digests_stamped_item_urls_to_send_digest(conn, monkeypatch):
    # P1 fix (pre-multi-channel-refactor): send_digest's HTML link-provenance
    # allowlist must come from the digest's own stamped items
    # (get_digest_item_urls), not threaded through from an in-memory Item
    # list -- that's what makes the same code path work for a fresh digest
    # and a later pending resend alike (see get_digest_item_urls's docstring
    # in digest/state.py). _deliver_channels is what fetches this now (see
    # the test below); _deliver_email just takes whatever it's given, which
    # this test verifies is passed straight through to send_digest.
    commit_new_items(
        conn,
        [_item("1"), _item("2")],
        {("telegram", "123"): "2"},
    )
    items = get_unsummarized_items(conn)
    digest_id = create_digest(conn, "## Needs attention\n...", items)
    allowed_urls = get_digest_item_urls(conn, digest_id)

    captured = {}

    def fake_send_digest(
        host,
        port,
        user,
        password,
        from_,
        from_name,
        to,
        subject,
        body_md,
        allowed_urls,
        generated_at_label,
    ):
        captured["allowed_urls"] = allowed_urls

    monkeypatch.setattr(main_mod, "send_digest", fake_send_digest)

    ok = _deliver_email(
        conn, _cfg(), digest_id, "## Needs attention\n...", len(items), allowed_urls
    )

    assert ok is True
    assert captured["allowed_urls"] == {"https://t.me/c/123/1", "https://t.me/c/123/2"}


def test_deliver_channels_recovers_urls_for_a_pending_resend_from_a_prior_run(conn, monkeypatch):
    # Simulates the pending-resend path: _deliver_channels has no in-memory
    # Item list at all (unlike the fresh-digest path) -- only digest_id and
    # body_md read back off the `digests` table, exactly like
    # get_pending_digests returns in _deliver. The allowlist must still be
    # recoverable purely from the digest_id, via the items already stamped
    # by a create_digest call that ran in a "previous run" (here, earlier
    # in this test, but nothing about _deliver_channels depends on that).
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(
        conn, "## Needs attention\n...pending...", get_unsummarized_items(conn)
    )

    captured = {}

    def fake_send_digest(
        host,
        port,
        user,
        password,
        from_,
        from_name,
        to,
        subject,
        body_md,
        allowed_urls,
        generated_at_label,
    ):
        captured["allowed_urls"] = allowed_urls

    monkeypatch.setattr(main_mod, "send_digest", fake_send_digest)

    ok = _deliver_channels(
        conn,
        _cfg(),
        digest_id,
        "## Needs attention\n...pending...",
        1,
        "2026-07-29T10:00:00+00:00",
        _NO_CHANNELS_DONE,
        _fresh_telegram_state(),
    )

    assert ok is True
    assert captured["allowed_urls"] == {"https://t.me/c/123/1"}


def test_deliver_site_publishes_rendered_html_and_marks_site_published(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Worth knowing\n\nstuff"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))
    allowed_urls = get_digest_item_urls(conn, digest_id)

    captured = {}

    def fake_publish(digest_id_, body_md_, body_html, created_at, item_count, publish_url, key):
        captured.update(
            digest_id=digest_id_,
            body_html=body_html,
            created_at=created_at,
            item_count=item_count,
            publish_url=publish_url,
            ingest_key=key,
        )

    monkeypatch.setattr(main_mod, "publish_to_site", fake_publish)

    cfg = _multichannel_cfg()
    ok = _deliver_site(
        conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls
    )

    assert ok is True
    assert captured["digest_id"] == digest_id
    assert "<h2>Worth knowing</h2>" in captured["body_html"]
    assert captured["created_at"] == "2026-07-29T10:00:00+00:00"
    assert captured["item_count"] == 1
    assert captured["publish_url"] == cfg.site_publish_url
    assert captured["ingest_key"] == cfg.site_ingest_key
    row = conn.execute("SELECT site_published FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (1,)


def test_deliver_telegram_delegates_with_configured_params_and_marks_telegram_sent(
    conn, monkeypatch
):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Worth knowing\n\nstuff"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))

    captured = {}

    def fake_send(digest_id_, body_md_, created_at, bot_token, chat_id, thread_id, public_base):
        captured.update(
            digest_id=digest_id_,
            created_at=created_at,
            bot_token=bot_token,
            chat_id=chat_id,
            thread_id=thread_id,
            public_base=public_base,
        )

    monkeypatch.setattr(main_mod, "send_telegram_tldr", fake_send)

    cfg = _multichannel_cfg()
    ok = _deliver_telegram(
        conn, cfg, digest_id, body_md, _recent_created_at(), _fresh_telegram_state()
    )

    assert ok is True
    assert captured["digest_id"] == digest_id
    assert captured["bot_token"] == cfg.telegram_notify_bot_token
    assert captured["chat_id"] == cfg.telegram_notify_chat_id
    assert captured["thread_id"] == cfg.telegram_notify_thread_id
    assert captured["public_base"] == cfg.site_public_base
    row = conn.execute("SELECT telegram_sent FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (1,)


# --- _deliver_channels: multi-channel independence (delivery-channels feature) ---


def _multichannel_cfg(**overrides) -> Config:
    """_cfg() plus all three channels turned on, for independence tests below."""
    return replace(
        _cfg(),
        site_publish_url="https://news-site.example.workers.dev",
        site_ingest_key="ingest-secret",
        site_public_base="https://news.example.com/t/tok",
        telegram_notify_bot_token="bot-token",
        telegram_notify_chat_id="-100123",
        **overrides,
    )


def test_deliver_channels_email_failure_does_not_block_site_or_telegram(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Worth knowing\n\nstuff", [])

    monkeypatch.setattr(
        main_mod, "send_digest", lambda *a, **k: (_ for _ in ()).throw(OSError("smtp down"))
    )
    site_calls = []
    telegram_calls = []
    monkeypatch.setattr(
        main_mod, "publish_to_site", lambda *a, **k: site_calls.append(a[0])
    )
    monkeypatch.setattr(
        main_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(a[0])
    )

    ok = _deliver_channels(
        conn,
        _multichannel_cfg(),
        digest_id,
        "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        1,
        _recent_created_at(),
        _NO_CHANNELS_DONE,
        _fresh_telegram_state(),
    )

    assert ok is False  # email never succeeded
    assert site_calls == [digest_id]
    assert telegram_calls == [digest_id]
    row = conn.execute(
        "SELECT email_sent, site_published, telegram_sent FROM digests WHERE id = ?",
        (digest_id,),
    ).fetchone()
    assert row == (0, 1, 1)


def test_deliver_channels_site_failure_skips_telegram_this_run(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Worth knowing\n\nstuff", [])

    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(
        main_mod, "publish_to_site", lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
    )
    telegram_calls = []
    monkeypatch.setattr(
        main_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(a[0])
    )

    ok = _deliver_channels(
        conn,
        _multichannel_cfg(),
        digest_id,
        "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        1,
        "2026-07-29T10:00:00+00:00",
        _NO_CHANNELS_DONE,
        _fresh_telegram_state(),
    )

    assert ok is False
    assert telegram_calls == []  # never attempted -- site publish isn't done yet
    row = conn.execute(
        "SELECT email_sent, site_published, telegram_sent FROM digests WHERE id = ?",
        (digest_id,),
    ).fetchone()
    assert row == (1, 0, 0)


def test_deliver_channels_second_run_only_retries_the_failed_channel(conn, monkeypatch):
    # Simulates a partial success persisting across two runs: email and
    # telegram succeeded on "run 1" (site failed), so "run 2" must retry
    # ONLY site -- email/telegram must not be attempted (and thus not
    # duplicated) a second time.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Worth knowing\n\nstuff", [])

    email_calls = []
    telegram_calls = []
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: email_calls.append(1))
    monkeypatch.setattr(
        main_mod, "publish_to_site", lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
    )
    monkeypatch.setattr(
        main_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(1)
    )

    cfg = _multichannel_cfg()
    body_md = "**TL;DR:** hi\n\n## Worth knowing\n\nstuff"

    ok_run1 = _deliver_channels(
        conn, cfg, digest_id, body_md, 1, _recent_created_at(),
        _NO_CHANNELS_DONE, _fresh_telegram_state(),
    )
    assert ok_run1 is False
    assert email_calls == [1]
    assert telegram_calls == []  # site wasn't done yet, so telegram was skipped run 1

    # Run 2: fix the site publish, re-derive `done` the way _deliver does.
    def working_publish(*a, **k):
        return None

    monkeypatch.setattr(main_mod, "publish_to_site", working_publish)
    row = conn.execute(
        "SELECT email_sent, site_published, telegram_sent FROM digests WHERE id = ?",
        (digest_id,),
    ).fetchone()
    done = {"email": bool(row[0]), "site": bool(row[1]), "telegram": bool(row[2])}

    ok_run2 = _deliver_channels(
        conn, cfg, digest_id, body_md, 1, _recent_created_at(),
        done, _fresh_telegram_state(),
    )

    assert ok_run2 is True
    assert email_calls == [1]  # not attempted again -- already done
    assert telegram_calls == [1]  # attempted exactly once, now that site is done
    row = conn.execute(
        "SELECT email_sent, site_published, telegram_sent FROM digests WHERE id = ?",
        (digest_id,),
    ).fetchone()
    assert row == (1, 1, 1)


def test_deliver_channels_telegram_not_blocked_when_site_channel_disabled(conn, monkeypatch):
    # Telegram enabled, site channel NOT configured at all: the "skip
    # telegram until site publish succeeds" rule must not apply when this
    # run never intended to publish to a site in the first place.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Worth knowing\n\nstuff", [])

    cfg = replace(
        _cfg(),
        telegram_notify_bot_token="bot-token",
        telegram_notify_chat_id="-100123",
        site_public_base="https://news.example.com/t/tok",
    )
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: None)
    telegram_calls = []
    monkeypatch.setattr(
        main_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(1)
    )

    ok = _deliver_channels(
        conn,
        cfg,
        digest_id,
        "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        1,
        _recent_created_at(),
        _NO_CHANNELS_DONE,
        _fresh_telegram_state(),
    )

    assert ok is True
    assert telegram_calls == [1]


def test_deliver_email_disabled_site_only_completes_the_digest(conn, monkeypatch):
    # EMAIL_ENABLED=false end-to-end: site publish success alone must
    # complete the digest -- email must never be attempted at all.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Worth knowing\n\nstuff", [])

    monkeypatch.setattr(
        main_mod, "send_digest", lambda *a, **k: (_ for _ in ()).throw(AssertionError("boom"))
    )
    monkeypatch.setattr(main_mod, "publish_to_site", lambda *a, **k: None)

    cfg = replace(
        _cfg(),
        email_enabled=False,
        site_publish_url="https://news-site.example.workers.dev",
        site_ingest_key="ingest-secret",
    )

    ok = _deliver_channels(
        conn,
        cfg,
        digest_id,
        "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        1,
        "2026-07-29T10:00:00+00:00",
        _NO_CHANNELS_DONE,
        _fresh_telegram_state(),
    )

    assert ok is True
    row = conn.execute(
        "SELECT email_sent, site_published FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    assert row == (0, 1)  # email left at its default 0 -- never attempted


def test_deliver_run_failure_propagates_from_a_single_failed_channel(conn, monkeypatch):
    # Run-success semantics: a failed channel must keep the overall _deliver
    # result False, the same way an SMTP failure does today, so the
    # systemd OnFailure alert still fires.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})

    monkeypatch.setattr(main_mod, "summarize", lambda *a, **k: "**TL;DR:** hi\n\n## Section")
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(
        main_mod, "publish_to_site", lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
    )
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = replace(
        _cfg(),
        site_publish_url="https://news-site.example.workers.dev",
        site_ingest_key="ingest-secret",
    )

    ok = _deliver(conn, cfg, [])

    assert ok is False


# --- GUARD 1 (freshness window) + GUARD 2 (per-run 429 circuit breaker) --
# See _TELEGRAM_MAX_AGE's module-level comment in digest/main.py for the
# 2026-08-06 incident these two guards exist to make unrepeatable.


def _telegram_only_cfg(**overrides) -> Config:
    """_cfg() with ONLY the Telegram channel enabled -- email and site both off.

    Isolates the guard tests below from the site-must-publish-first ordering
    rule in `_deliver_channels` (which has its own dedicated tests above) and
    from email's own retry semantics -- neither is what GUARD 1/GUARD 2 are
    about.
    """
    return replace(
        _cfg(),
        email_enabled=False,
        telegram_notify_bot_token="bot-token",
        telegram_notify_chat_id="-100123",
        site_public_base="https://news.example.com/t/tok",
        **overrides,
    )


def test_deliver_channels_telegram_freshness_window_skips_old_digest_without_sending(
    conn, monkeypatch
):
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Worth knowing\n\nstuff", [])

    telegram_calls = []
    monkeypatch.setattr(
        main_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(a[0])
    )

    ok = _deliver_channels(
        conn,
        _telegram_only_cfg(),
        digest_id,
        "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        1,
        _stale_created_at(),
        _NO_CHANNELS_DONE,
        _fresh_telegram_state(),
    )

    assert ok is True  # a too-old digest counts as done -- the run can be fully green
    assert telegram_calls == []  # send_telegram_tldr NEVER called
    row = conn.execute("SELECT telegram_sent FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (1,)  # mark_digest_telegram_sent was still called


def test_deliver_channels_telegram_freshness_window_still_sends_a_fresh_digest(
    conn, monkeypatch
):
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Worth knowing\n\nstuff", [])

    telegram_calls = []
    monkeypatch.setattr(
        main_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(a[0])
    )

    ok = _deliver_channels(
        conn,
        _telegram_only_cfg(),
        digest_id,
        "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        1,
        _recent_created_at(),
        _NO_CHANNELS_DONE,
        _fresh_telegram_state(),
    )

    assert ok is True
    assert telegram_calls == [digest_id]
    row = conn.execute("SELECT telegram_sent FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (1,)


def test_deliver_incident_replay_stale_backlog_all_skip_telegram_only_fresh_one_sends(
    conn, monkeypatch
):
    # Replays the 2026-08-06 incident shape at the `_deliver` level: a
    # backlog of ~10 pre-cutover digests with telegram_sent=0 (standing in
    # for the ALTER TABLE migration's DEFAULT 0 on every pre-existing row)
    # plus one genuinely fresh digest. GUARD 1 must mark every stale one
    # sent WITHOUT ever calling send_telegram_tldr, and still deliver the
    # fresh one normally -- and the whole run must come back green.
    stale_ids = []
    for i in range(10):
        digest_id = create_digest(conn, f"**TL;DR:** old {i}\n\n## Worth knowing\n\nstuff", [])
        conn.execute(
            "UPDATE digests SET created_at = ? WHERE id = ?", (_stale_created_at(), digest_id)
        )
        conn.commit()
        stale_ids.append(digest_id)

    fresh_id = create_digest(conn, "**TL;DR:** fresh\n\n## Worth knowing\n\nstuff", [])
    conn.execute(
        "UPDATE digests SET created_at = ? WHERE id = ?", (_recent_created_at(), fresh_id)
    )
    conn.commit()

    telegram_calls = []
    monkeypatch.setattr(
        main_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(a[0])
    )
    monkeypatch.setattr(
        main_mod,
        "summarize",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no new items expected")),
    )

    ok = _deliver(conn, _telegram_only_cfg(), [])

    assert ok is True
    assert telegram_calls == [fresh_id]  # exactly one real send, for the fresh digest
    telegram_sent_by_id = dict(
        conn.execute("SELECT id, telegram_sent FROM digests ORDER BY id").fetchall()
    )
    for digest_id in stale_ids:
        assert telegram_sent_by_id[digest_id] == 1  # marked sent without ever notifying
    assert telegram_sent_by_id[fresh_id] == 1


def test_deliver_circuit_breaker_429_skips_remaining_telegram_sends_this_run(conn, monkeypatch):
    digest_ids = []
    for i in range(3):
        digest_id = create_digest(conn, f"**TL;DR:** item {i}\n\n## Worth knowing\n\nstuff", [])
        conn.execute(
            "UPDATE digests SET created_at = ? WHERE id = ?", (_recent_created_at(), digest_id)
        )
        conn.commit()
        digest_ids.append(digest_id)

    send_calls = []

    def fake_send(digest_id_, *args, **kwargs):
        send_calls.append(digest_id_)
        raise TelegramSendError("telegram sendMessage failed with status 429", status=429)

    monkeypatch.setattr(main_mod, "send_telegram_tldr", fake_send)

    ok = _deliver(conn, _telegram_only_cfg(), [])

    assert ok is False  # a 429-triggered skip still counts as a failure for the run
    assert send_calls == [digest_ids[0]]  # remaining two never attempted
    telegram_sent_by_id = dict(
        conn.execute("SELECT id, telegram_sent FROM digests ORDER BY id").fetchall()
    )
    for digest_id in digest_ids:
        assert telegram_sent_by_id[digest_id] == 0  # left unsent, will retry next run


def test_deliver_non_429_telegram_error_does_not_trip_circuit_breaker(conn, monkeypatch):
    digest_ids = []
    for i in range(2):
        digest_id = create_digest(conn, f"**TL;DR:** item {i}\n\n## Worth knowing\n\nstuff", [])
        conn.execute(
            "UPDATE digests SET created_at = ? WHERE id = ?", (_recent_created_at(), digest_id)
        )
        conn.commit()
        digest_ids.append(digest_id)

    send_calls = []

    def fake_send(digest_id_, *args, **kwargs):
        send_calls.append(digest_id_)
        raise TelegramSendError("telegram sendMessage failed: URLError")  # status=None

    monkeypatch.setattr(main_mod, "send_telegram_tldr", fake_send)

    ok = _deliver(conn, _telegram_only_cfg(), [])

    assert ok is False
    assert send_calls == digest_ids  # BOTH attempted -- a non-429 failure never trips it


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


# --- _run_polymarket_collector (Polymarket collector, Config.polymarket_enabled-gated) ---


def test_run_polymarket_disabled_never_calls_collector(conn):
    cfg = replace(_cfg(), polymarket_enabled=False)

    result = _run_polymarket_collector(conn, cfg)

    assert result == PolymarketCollectResult()


def test_run_polymarket_disabled_collect_function_untouched(monkeypatch, conn):
    cfg = replace(_cfg(), polymarket_enabled=False)

    def boom_collect(*a, **k):
        raise AssertionError("polymarket_collector.collect must not be called when disabled")

    monkeypatch.setattr(main_mod.polymarket_collector, "collect", boom_collect)

    result = _run_polymarket_collector(conn, cfg)

    assert result == PolymarketCollectResult()


def test_run_polymarket_enabled_delegates_to_collect_with_configured_params(monkeypatch, conn):
    cfg = replace(
        _cfg(),
        polymarket_enabled=True,
        polymarket_api_base="https://proxy.example.com",
        polymarket_proxy_key="secret123",
        polymarket_top_n=10,
        polymarket_swing_threshold=0.2,
    )
    expected = PolymarketCollectResult(
        items=[
            Item(
                source="polymarket",
                source_id="m1:2026-07-29T10:00:00+00:00",
                chat_id=None,
                chat_title="Polymarket",
                author=None,
                text="swing",
                url="https://polymarket.com/market/will-x-happen",
                fetched_at="2026-07-29T10:00:00+00:00",
            )
        ],
        prob_updates={"m1": (0.6, "Will X happen?")},
    )

    captured = {}

    def fake_collect(api_base, proxy_key, top_n, threshold, get_stored_probs):
        captured["api_base"] = api_base
        captured["proxy_key"] = proxy_key
        captured["top_n"] = top_n
        captured["threshold"] = threshold
        # The injected closure must actually reach the real conn/state layer.
        assert get_stored_probs(["m1"]) == {}
        return expected

    monkeypatch.setattr(main_mod.polymarket_collector, "collect", fake_collect)

    result = _run_polymarket_collector(conn, cfg)

    assert result is expected
    assert captured == {
        "api_base": "https://proxy.example.com",
        "proxy_key": "secret123",
        "top_n": 10,
        "threshold": 0.2,
    }


def test_run_polymarket_collector_crash_is_caught_returns_failed_result(monkeypatch, conn):
    cfg = replace(_cfg(), polymarket_enabled=True)

    def boom_collect(*a, **k):
        raise RuntimeError("gamma api shape changed")

    monkeypatch.setattr(main_mod.polymarket_collector, "collect", boom_collect)

    result = _run_polymarket_collector(conn, cfg)

    assert result.failed is True
    assert result.items == []
    assert result.prob_updates == {}


# --- _run: polymarket wiring end to end (merge into commit, prob updates, failed_sources) ---


def test_run_merges_polymarket_items_and_prob_updates_into_commit(monkeypatch, tmp_path):
    cfg = replace(
        _cfg(), state_db_path=str(tmp_path / "state.db"), polymarket_enabled=True
    )

    tg_item = _item("1")
    polymarket_item = Item(
        source="polymarket",
        source_id="m1:2026-07-29T10:00:00+00:00",
        chat_id=None,
        chat_title="Polymarket",
        author=None,
        text="swing",
        url="https://polymarket.com/market/will-x-happen",
        fetched_at="2026-07-29T10:00:00+00:00",
    )

    _patch_telegram_client(
        monkeypatch, CollectResult(items=[tg_item], cursor_updates={("telegram", "123"): "1"})
    )

    def fake_polymarket_collect(api_base, proxy_key, top_n, threshold, get_stored_probs):
        return PolymarketCollectResult(
            items=[polymarket_item], prob_updates={"m1": (0.6, "Will X happen?")}
        )

    monkeypatch.setattr(main_mod.polymarket_collector, "collect", fake_polymarket_collect)

    captured = {}

    def fake_commit_new_items(conn, items, cursor_updates, *, polymarket_prob_updates=None):
        captured["items"] = items
        captured["cursor_updates"] = cursor_updates
        captured["polymarket_prob_updates"] = polymarket_prob_updates
        return len(items)

    monkeypatch.setattr(main_mod, "commit_new_items", fake_commit_new_items)
    monkeypatch.setattr(main_mod, "_deliver", lambda conn, cfg, failed_sources: True)

    ok = asyncio.run(main_mod._run(cfg))

    assert ok is True
    assert captured["items"] == [tg_item, polymarket_item]
    # polymarket never contributes a cursor_update -- its state axis is
    # prob_updates, handled separately.
    assert captured["cursor_updates"] == {("telegram", "123"): "1"}
    assert captured["polymarket_prob_updates"] == {"m1": (0.6, "Will X happen?")}


def test_run_polymarket_disabled_commit_call_shape_is_unaffected(monkeypatch, tmp_path):
    # Regression: when polymarket contributes no prob_updates (disabled, or
    # enabled but nothing to anchor), commit_new_items must be called
    # WITHOUT the polymarket_prob_updates kwarg at all -- existing callers/
    # test doubles with the pre-polymarket 3-argument signature must keep
    # working unmodified.
    cfg = replace(
        _cfg(), state_db_path=str(tmp_path / "state.db"), polymarket_enabled=False
    )
    _patch_telegram_client(monkeypatch, CollectResult())

    def fake_commit_new_items(conn, items, cursor_updates):
        return len(items)

    monkeypatch.setattr(main_mod, "commit_new_items", fake_commit_new_items)
    monkeypatch.setattr(main_mod, "_deliver", lambda conn, cfg, failed_sources: True)

    ok = asyncio.run(main_mod._run(cfg))

    assert ok is True


def test_run_polymarket_failure_surfaces_in_failed_sources(monkeypatch, tmp_path):
    cfg = replace(
        _cfg(), state_db_path=str(tmp_path / "state.db"), polymarket_enabled=True
    )

    _patch_telegram_client(monkeypatch, CollectResult())
    monkeypatch.setattr(
        main_mod.polymarket_collector,
        "collect",
        lambda *a, **k: PolymarketCollectResult(failed=True),
    )

    captured = {}

    def fake_deliver(conn, cfg, failed_sources):
        captured["failed_sources"] = failed_sources
        return True

    monkeypatch.setattr(main_mod, "_deliver", fake_deliver)

    ok = asyncio.run(main_mod._run(cfg))

    assert captured["failed_sources"] == ["polymarket"]
    assert ok is False
