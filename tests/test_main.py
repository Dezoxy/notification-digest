import asyncio
import json
import logging
import urllib.error
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import digest.deliver as deliver_mod
import digest.main as main_mod
import digest.summarize as summarize_mod
from digest.collectors.base import CollectResult
from digest.collectors.polymarket import PolymarketCollectResult
from digest.config import Config
from digest.deliver import (
    TelegramRunState,
    _deliver_email,
    _deliver_site,
    _deliver_telegram,
    _telegram_thread_id_for_kind,
    deliver_channels,
    deliver_pending,
)
from digest.main import (
    _client_ready,
    _deliver,
    _run_hackernews_collector,
    _run_news_collector,
    _run_polymarket_collector,
    _run_reddit_collector,
    _run_x_collector,
    run_daily,
    run_weekly,
)
from digest.publish import TelegramSendError
from digest.state import (
    Item,
    commit_new_items,
    connect,
    count_unsummarized_items,
    create_digest,
    get_arc_context,
    get_arc_keys,
    get_arc_keys_needing_context,
    get_digest_item_urls,
    get_pending_digests,
    get_recent_arc_keys,
    get_unsummarized_items,
    get_unsynced_arc_contexts,
    init_db,
    write_arc_context,
    write_arc_keys,
)
from digest.summarize import ModelRun, SummarizeError
from digest.verify import VerificationUnavailable

_NO_CHANNELS_DONE = {"email": False, "site": False, "telegram": False}

# A stand-in ModelRun for fakes/monkeypatches of summarize()/summarize_daily()/
# summarize_weekly()/translate_digest() that don't care about provenance
# specifics -- most call sites in this file. Tests that DO care (asserting
# what create_digest/publish_to_site received) build their own.
_MODEL_RUN = ModelRun(model="claude-opus-5", effort="high", fallback=False)


def _fresh_telegram_state() -> TelegramRunState:
    """A brand-new, untripped circuit breaker -- most tests below want one call's worth."""
    return TelegramRunState()


def _recent_created_at(hours_ago: float = 1) -> str:
    """An ISO8601 UTC created_at `hours_ago` in the past, relative to the real wall clock.

    Used (instead of a fixed historical literal like "2026-07-29T...") by
    every test that exercises a Telegram SEND path: digest/main.py's GUARD 1
    freshness window (`_TELEGRAM_MAX_AGE`) compares `created_at` against
    `datetime.now(UTC)` at call time, so a fixed literal would eventually
    age out of the window and start failing these tests for a reason that
    has nothing to do with the behavior under test. Default of 1 hour ago is
    comfortably inside the 24h window.
    """
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


def _stale_created_at(hours_ago: float = 25) -> str:
    """An ISO8601 UTC created_at `hours_ago` ago -- outside GUARD 1's 24h window by default."""
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
    monkeypatch.setattr(deliver_mod, "send_digest", fake_send_digest)
    monkeypatch.setattr(main_mod, "archive", boom_archive)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    assert sent["body_md"] == "## Needs attention\n..."
    assert sent["allowed_urls"] == {"https://t.me/c/123/1"}
    assert get_pending_digests(conn, True, False, False) == []  # marked sent


def test_deliver_zero_unsummarized_items_sends_nothing(conn, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("must not be called when there is nothing to deliver")

    monkeypatch.setattr(main_mod, "summarize", boom)
    monkeypatch.setattr(deliver_mod, "send_digest", boom)
    monkeypatch.setattr(main_mod, "archive", boom)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    assert get_pending_digests(conn, True, False, False) == []


def test_deliver_smtp_failure_leaves_digest_row_unsent(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})

    monkeypatch.setattr(
        main_mod, "summarize", lambda *a, **k: ("## Needs attention\n...", [], [], _MODEL_RUN)
    )

    def failing_send(*args, **kwargs):
        raise OSError("smtp connection refused")

    archived = {"called": False}
    monkeypatch.setattr(deliver_mod, "send_digest", failing_send)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: archived.__setitem__("called", True))

    ok = _deliver(conn, _cfg(), [])

    assert ok is False
    pending = get_pending_digests(conn, True, False, False)
    assert len(pending) == 1
    digest_id, body_md, _done, _kind = pending[0]
    assert body_md == "## Needs attention\n..."
    # P1 spec change: archive() now runs once, unconditionally, right after
    # create_digest -- it is no longer gated on any channel's delivery
    # succeeding (a channel failing must not also lose the archive copy).
    assert archived["called"] is True


def test_deliver_success_path_creates_digest_sends_marks_sent_and_archives(conn, monkeypatch):
    commit_new_items(conn, [_item("1"), _item("2")], {("telegram", "123"): "2"})

    summarize_calls = []

    def fake_summarize(
        items,
        failed_sources,
        recent_coverage,
        model,
        timeout_seconds,
        effort,
        recent_arcs="",
        **_kwargs,
    ):
        # cfg.claude_effort must reach summarize() unchanged -- the only hop
        # between Config.claude_effort and the eventual `--effort` argv flag
        # in digest/summarize.py's run_claude.
        summarize_calls.append(effort)
        return "## Needs attention\n...", [], [], _MODEL_RUN

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

    monkeypatch.setattr(deliver_mod, "send_digest", fake_send_digest)
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
    assert get_pending_digests(conn, True, False, False) == []
    assert archived["id"] == 1


def test_deliver_end_to_end_strips_deltas_fence_before_storage_and_persists_them(conn, monkeypatch):
    # PLAN.md §11.3 integration: runs the REAL summarize() (only run_claude
    # is mocked, unlike every other test in this file which mocks
    # main_mod.summarize directly) so extract_deltas, map_deltas_to_slugs,
    # and write_deltas all execute for real. Proves the single-choke-point
    # claim: the raw model output's ```deltas fence must never reach
    # digests.body_md -- the only thing run_daily/run_weekly's prompt
    # builders ever read -- while the parsed entry still lands in the
    # `deltas` table, mapped to this digest's own real topic slug.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})

    raw_model_output = (
        "**TL;DR:** The Fed held rates steady.\n\n"
        "## Fed rate decision\n\n"
        "The Fed held rates steady this week.\n\n"
        "```deltas\n"
        '[{"heading": "Fed rate decision", "previously": "Markets expected a cut.", '
        '"now": "The Fed held instead."}]\n'
        "```\n"
    )
    monkeypatch.setattr(summarize_mod, "run_claude", lambda *a, **k: raw_model_output)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    digest_id, body_md = conn.execute("SELECT id, body_md FROM digests").fetchone()
    # Stripped-body invariant: the machine-facing block never reaches the
    # stored body_md -- the single source every downstream consumer
    # (translation input, email/site rendering, and critically
    # get_window_digests_since's own output, which run_daily/run_weekly feed
    # straight into their prompt builders) reads from.
    assert "```deltas" not in body_md
    assert "Markets expected a cut" not in body_md
    assert "## Fed rate decision" in body_md  # the real prose section survives untouched

    deltas_rows = conn.execute(
        "SELECT slug, previously, now FROM deltas WHERE digest_id = ?", (digest_id,)
    ).fetchall()
    assert deltas_rows == [
        ("fed-rate-decision", "Markets expected a cut.", "The Fed held instead.")
    ]


def test_deliver_end_to_end_strips_arcs_fence_before_storage_and_persists_them(conn, monkeypatch):
    # Stable-arc-keys integration, mirrors the deltas end-to-end test
    # immediately above: runs the REAL summarize() (only run_claude is
    # mocked) so extract_arc_keys, derive_topics(body_md, arc_keys), and
    # write_arc_keys all execute for real. Proves the single-choke-point
    # claim: the raw model output's ```arcs fence must never reach
    # digests.body_md, while the parsed entry still lands in the `arc_keys`
    # table, mapped to this digest's own real topic slug.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})

    raw_model_output = (
        "**TL;DR:** Tension over the Strait of Hormuz continued.\n\n"
        "## Hormuz tension escalates\n\n"
        "More vessels were diverted this week.\n\n"
        '```arcs\n[{"heading": "Hormuz tension escalates", "key": "hormuz"}]\n```\n'
    )
    monkeypatch.setattr(summarize_mod, "run_claude", lambda *a, **k: raw_model_output)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    digest_id, body_md = conn.execute("SELECT id, body_md FROM digests").fetchone()
    # Stripped-body invariant, identical to the deltas contract above.
    assert "```arcs" not in body_md
    assert '"key"' not in body_md
    assert "## Hormuz tension escalates" in body_md  # the real prose section survives untouched

    arc_key_rows = conn.execute(
        "SELECT slug, key FROM arc_keys WHERE digest_id = ?", (digest_id,)
    ).fetchall()
    assert arc_key_rows == [("hormuz-tension-escalates", "hormuz")]


def test_deliver_arc_keys_dropped_when_heading_matches_no_real_section(conn, monkeypatch):
    # An arcs-fence entry citing a heading that isn't one of this digest's
    # own `## ` sections (model drift, or a hallucinated citation) must be
    # dropped -- never stored as if it were a real topic's key.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})

    raw_model_output = (
        "**TL;DR:** Quiet window.\n\n"
        "## Real section\n\nSomething happened.\n\n"
        '```arcs\n[{"heading": "A heading that does not exist", "key": "ghost-key"}]\n```\n'
    )
    monkeypatch.setattr(summarize_mod, "run_claude", lambda *a, **k: raw_model_output)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    digest_id = conn.execute("SELECT id FROM digests").fetchone()[0]
    count = conn.execute(
        "SELECT COUNT(*) FROM arc_keys WHERE digest_id = ?", (digest_id,)
    ).fetchone()[0]
    assert count == 0


def test_deliver_reuses_recent_arc_key_across_two_runs(conn, monkeypatch):
    # End-to-end proof of the feature's whole point: a second window whose
    # model output reuses a key from get_recent_arc_keys' {{RECENT_ARCS}}
    # rendering (fed by the FIRST run's own write_arc_keys) lands in the
    # `arc_keys` table under the SAME key, even though the heading text
    # differs completely between the two runs.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    first_output = (
        "**TL;DR:** Tension near the Strait of Hormuz.\n\n"
        "## Hormuz tension escalates\n\nMore vessels diverted.\n\n"
        '```arcs\n[{"heading": "Hormuz tension escalates", "key": "hormuz"}]\n```\n'
    )
    monkeypatch.setattr(summarize_mod, "run_claude", lambda *a, **k: first_output)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)
    assert _deliver(conn, _cfg(), []) is True

    # Second window, differently worded heading, reusing the SAME key --
    # exactly the live incident this feature fixes (Iran/Hormuz recurring
    # under many different slugs).
    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})
    captured_prompts = []

    def fake_run_claude(prompt, model, timeout_seconds, effort):
        captured_prompts.append(prompt)
        return (
            "**TL;DR:** Naval buildup continues near Hormuz.\n\n"
            "## Naval buildup near the strait\n\nMore ships arrived.\n\n"
            '```arcs\n[{"heading": "Naval buildup near the strait", "key": "hormuz"}]\n```\n'
        )

    monkeypatch.setattr(summarize_mod, "run_claude", fake_run_claude)
    assert _deliver(conn, _cfg(), []) is True

    # The second run's own prompt must have offered "hormuz" back for reuse.
    assert "hormuz" in captured_prompts[0]

    rows = conn.execute("SELECT digest_id, slug, key FROM arc_keys ORDER BY digest_id").fetchall()
    assert [r[2] for r in rows] == ["hormuz", "hormuz"]
    assert rows[0][1] == "hormuz-tension-escalates"
    assert rows[1][1] == "naval-buildup-near-the-strait"


def test_deliver_deltas_dropped_when_heading_matches_no_real_section(conn, monkeypatch):
    # A delta citing a heading that isn't one of this digest's own `## `
    # sections (model drift, or a stale/hallucinated citation) must be
    # dropped -- never stored as if it were a real topic.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})

    raw_model_output = (
        "**TL;DR:** Quiet window.\n\n"
        "## Real section\n\nSomething happened.\n\n"
        "```deltas\n"
        '[{"heading": "A heading that does not exist", "previously": "old", "now": "new"}]\n'
        "```\n"
    )
    monkeypatch.setattr(summarize_mod, "run_claude", lambda *a, **k: raw_model_output)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    digest_id = conn.execute("SELECT id FROM digests").fetchone()[0]
    assert (
        conn.execute("SELECT COUNT(*) FROM deltas WHERE digest_id = ?", (digest_id,)).fetchone()[0]
        == 0
    )


def test_run_daily_prompt_never_ingests_deltas_content(conn, monkeypatch, tmp_path):
    # PLAN.md §11.3 fencing guardrail: a WINDOW digest's stored body_md (the
    # only thing run_daily's build_daily_prompt embeds) must carry no delta
    # content by the time run_daily reads it back -- proven end-to-end here
    # by running the real summarize() pipeline for the window digest (so
    # extract_deltas' stripping is exercised for real, not assumed), then
    # capturing exactly what build_daily_prompt renders for the daily brief.
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    commit_new_items(real_conn, [_item("1")], {("telegram", "123"): "1"})

    raw_model_output = (
        "**TL;DR:** The Fed held rates steady.\n\n"
        "## Fed rate decision\n\nThe Fed held rates steady this week.\n\n"
        "```deltas\n"
        '[{"heading": "Fed rate decision", "previously": "Markets expected a cut.", '
        '"now": "The Fed held instead."}]\n'
        "```\n"
    )
    monkeypatch.setattr(summarize_mod, "run_claude", lambda *a, **k: raw_model_output)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)
    ok = _deliver(real_conn, _cfg(), [])
    assert ok is True
    real_conn.close()

    captured_prompts = []

    def fake_run_claude(prompt, model, timeout_seconds, effort):
        captured_prompts.append(prompt)
        return "**TL;DR:** the day\n\n## An arc\n\nstuff"

    # Deliberately do NOT monkeypatch main_mod.summarize_daily here (unlike
    # every other run_daily test in this file) -- the real summarize_daily
    # must run so build_daily_prompt actually executes and this test can
    # capture its real output via daily_mod.run_claude below.
    from digest import daily as daily_mod

    monkeypatch.setattr(daily_mod, "run_claude", fake_run_claude)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = _daily_cfg(state_db_path=db_path)
    ok = run_daily(cfg)

    assert ok is True
    assert len(captured_prompts) == 1
    assert "```deltas" not in captured_prompts[0]
    assert "Markets expected a cut" not in captured_prompts[0]


def test_deliver_logs_digest_delivery_line_matching_success_path(conn, monkeypatch, caplog):
    # All three channels enabled and succeeding for a freshly summarized
    # digest: the digest_delivery Loki line (digest/deliver.py's
    # deliver_channels) must report every channel as "sent" -- mirrors
    # test_deliver_success_path_creates_digest_sends_marks_sent_and_archives's
    # own setup, plus the site/telegram channels turned on via
    # _multichannel_cfg so all three statuses are exercised at once.
    commit_new_items(conn, [_item("1"), _item("2")], {("telegram", "123"): "2"})

    monkeypatch.setattr(
        main_mod, "summarize", lambda *a, **k: ("## Needs attention\n...", [], [], _MODEL_RUN)
    )
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = _multichannel_cfg()
    with caplog.at_level("INFO", logger=deliver_mod.logger.name):
        ok = _deliver(conn, cfg, [])

    assert ok is True

    line = next(r.message for r in caplog.records if r.message.startswith("digest_delivery "))
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["kind"] == "window"
    assert payload["email"] == "sent"
    assert payload["site"] == "sent"
    assert payload["telegram"] == "sent"


def test_deliver_translate_hu_disabled_skips_translation_entirely(conn, monkeypatch):
    # TRANSLATE_HU_ENABLED=false (the default, via plain _cfg()) must mean
    # translate_digest -- and by extension the run_claude call inside it --
    # is never even invoked.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    monkeypatch.setattr(
        main_mod, "summarize", lambda *a, **k: ("## Needs attention\n...", [], [], _MODEL_RUN)
    )
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    def boom_translate(*args, **kwargs):
        raise AssertionError("translate_digest must not be called when TRANSLATE_HU_ENABLED=false")

    monkeypatch.setattr(main_mod, "translate_digest", boom_translate)

    cfg = _cfg()
    assert cfg.translate_hu_enabled is False
    ok = _deliver(conn, cfg, [])

    assert ok is True
    row = conn.execute("SELECT body_md_hu FROM digests").fetchone()
    assert row == (None,)


def test_deliver_translate_hu_enabled_stores_translation_before_channels(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    monkeypatch.setattr(
        main_mod, "summarize", lambda *a, **k: ("**TL;DR:** hi\n\n## S\n\nx", [], [], _MODEL_RUN)
    )
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    translate_calls = []

    def fake_translate(
        body_md, allowed_urls, model, timeout_seconds, fallback_model=None, **_kwargs
    ):
        translate_calls.append((body_md, allowed_urls, model, timeout_seconds, fallback_model))
        return "**TL;DR:** szia\n\n## Sz\n\ny", _MODEL_RUN

    monkeypatch.setattr(main_mod, "translate_digest", fake_translate)

    cfg = replace(_cfg(), translate_hu_enabled=True, translate_model="sonnet")
    ok = _deliver(conn, cfg, [])

    assert ok is True
    assert len(translate_calls) == 1
    body_md, allowed_urls, model, timeout_seconds, fallback_model = translate_calls[0]
    assert body_md == "**TL;DR:** hi\n\n## S\n\nx"
    assert allowed_urls == {"https://t.me/c/123/1"}
    assert model == "sonnet"
    assert timeout_seconds == cfg.claude_timeout_seconds
    # _deliver must thread cfg.translate_model_fallback through to
    # translate_digest's fallback_model kwarg.
    assert fallback_model == cfg.translate_model_fallback

    row = conn.execute("SELECT body_md_hu FROM digests").fetchone()
    assert row == ("**TL;DR:** szia\n\n## Sz\n\ny",)


# --- provenance column (model-provenance feature) ---


def test_deliver_stores_provenance_naming_summarize_and_translate_models(conn, monkeypatch):
    # End-to-end through _provenance/create_digest: a window run with
    # translation enabled must persist BOTH legs' ModelRuns, keyed by name,
    # exactly as digest/state.py's `provenance` column comment promises.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    summarize_run = ModelRun(model="claude-opus-5", effort="high", fallback=False)
    translate_run = ModelRun(model="openai/gpt-5.6-terra", effort="high", fallback=True)

    monkeypatch.setattr(
        main_mod,
        "summarize",
        lambda *a, **k: ("**TL;DR:** hi\n\n## S\n\nx", [], [], summarize_run),
    )
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)
    monkeypatch.setattr(
        main_mod,
        "translate_digest",
        lambda *a, **k: ("**TL;DR:** szia\n\n## Sz\n\ny", translate_run),
    )

    cfg = replace(_cfg(), translate_hu_enabled=True, translate_model="sonnet")
    ok = _deliver(conn, cfg, [])

    assert ok is True
    row = conn.execute("SELECT provenance FROM digests").fetchone()
    stored = json.loads(row[0])
    assert stored == {
        "summarize": {"model": "claude-opus-5", "effort": "high", "fallback": False},
        "translate": {"model": "openai/gpt-5.6-terra", "effort": "high", "fallback": True},
    }


def test_deliver_translation_disabled_stores_no_translate_key_in_provenance(conn, monkeypatch):
    # `translate_hu_enabled=False` means translate_digest is never called at
    # all -- the stored provenance object must carry no "translate" key,
    # never a null placeholder for it (see _provenance's own docstring).
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    summarize_run = ModelRun(model="claude-opus-5", effort="high", fallback=False)

    monkeypatch.setattr(
        main_mod,
        "summarize",
        lambda *a, **k: ("**TL;DR:** hi\n\n## S\n\nx", [], [], summarize_run),
    )
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    def boom_translate(*a, **k):
        raise AssertionError("translate_digest must not be called when the flag is off")

    monkeypatch.setattr(main_mod, "translate_digest", boom_translate)

    cfg = replace(_cfg(), translate_hu_enabled=False)
    ok = _deliver(conn, cfg, [])

    assert ok is True
    row = conn.execute("SELECT provenance FROM digests").fetchone()
    stored = json.loads(row[0])
    assert stored == {"summarize": {"model": "claude-opus-5", "effort": "high", "fallback": False}}
    assert "translate" not in stored


def test_deliver_translate_hu_failure_leaves_body_md_hu_null_english_still_ships(conn, monkeypatch):
    # Soft-fail contract: translate_digest returning None must not affect
    # the English digest's own success at all.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    monkeypatch.setattr(
        main_mod, "summarize", lambda *a, **k: ("## Needs attention\n...", [], [], _MODEL_RUN)
    )
    sent = {}
    monkeypatch.setattr(
        deliver_mod, "send_digest", lambda *a, **k: sent.update(called=True) or None
    )
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "translate_digest", lambda *a, **k: None)

    cfg = replace(_cfg(), translate_hu_enabled=True)
    ok = _deliver(conn, cfg, [])

    assert ok is True
    assert sent["called"] is True
    row = conn.execute("SELECT body_md_hu FROM digests").fetchone()
    assert row == (None,)


def test_deliver_pending_resend_carries_body_md_hu_to_site(conn, monkeypatch):
    # A digest already carrying a stored translation (from an earlier run)
    # must have that translation reach the site payload on a pending resend,
    # with zero extra wiring -- _digest_meta simply reads it back off the row.
    items = [_item("1")]
    commit_new_items(conn, items, {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## S\n\nx"
    body_md_hu = "**TL;DR:** szia\n\n## Sz\n\ny"
    create_digest(conn, body_md, get_unsummarized_items(conn), body_md_hu=body_md_hu)

    captured = {}

    def fake_publish(
        digest_id_,
        body_md_,
        body_html,
        created_at,
        item_count,
        publish_url,
        key,
        *,
        body_md_hu=None,
        body_html_hu=None,
        kind="window",
        source_counts=None,
        failed_sources=None,
        topics=None,
        deltas=None,
        arc_contexts=None,
        provenance=None,
    ):
        captured.update(body_md_hu=body_md_hu, body_html_hu=body_html_hu)

    monkeypatch.setattr(deliver_mod, "publish_to_site", fake_publish)

    def boom_summarize(*args, **kwargs):
        raise AssertionError("summarize must not be called on a pending resend")

    monkeypatch.setattr(main_mod, "summarize", boom_summarize)

    cfg = replace(
        _cfg(),
        email_enabled=False,
        site_publish_url="https://news-site.example.workers.dev",
        site_ingest_key="ingest-secret",
    )
    ok = _deliver(conn, cfg, [])

    assert ok is True
    assert captured["body_md_hu"] == body_md_hu
    assert "<h2>Sz</h2>" in captured["body_html_hu"]
    assert ">Röviden</span>" in captured["body_html_hu"]


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

    def fake_summarize(
        items,
        failed_sources,
        recent_coverage,
        model,
        timeout_seconds,
        effort,
        recent_arcs="",
        **_kwargs,
    ):
        captured["recent_coverage"] = recent_coverage
        return "## Needs attention\n...", [], [], _MODEL_RUN

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
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

    def fake_summarize(
        items,
        failed_sources,
        recent_coverage,
        model,
        timeout_seconds,
        effort,
        recent_arcs="",
        **_kwargs,
    ):
        summarize_calls.append((items, failed_sources))
        return "## Needs attention\n...new...", [], [], _MODEL_RUN

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
    monkeypatch.setattr(deliver_mod, "send_digest", fake_send_digest)
    monkeypatch.setattr(
        main_mod, "archive", lambda body_md, archive_dir, digest_id: archived.append(digest_id)
    )

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    assert len(sends) == 2
    assert sends[0] == "## Needs attention\n...pending..."
    assert sends[1] == "## Needs attention\n...new..."
    assert len(summarize_calls) == 1  # only for the new items, never the pending digest
    assert get_pending_digests(conn, True, False, False) == []
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

    def fake_summarize(
        items,
        failed_sources,
        recent_coverage,
        model,
        timeout_seconds,
        effort,
        recent_arcs="",
        **_kwargs,
    ):
        summarize_calls.append(failed_sources)
        return "## Needs attention\n...new...", [], [], _MODEL_RUN

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
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

    def fake_summarize(
        items,
        failed_sources,
        recent_coverage,
        model,
        timeout_seconds,
        effort,
        recent_arcs="",
        **_kwargs,
    ):
        summarize_calls.append(items)
        return "## Needs attention\n...new...", [], [], _MODEL_RUN

    def failing_send(*args, **kwargs):
        raise OSError("smtp connection refused")

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(deliver_mod, "send_digest", failing_send)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), [])

    assert ok is False
    assert len(summarize_calls) == 1  # the new item WAS summarized despite the pending failure
    pending = get_pending_digests(conn, True, False, False)
    assert pending  # still unsent, left for the next run's retry
    # Both the original pending digest and the newly created one remain
    # pending -- neither one's email attempt succeeded.
    row_count = conn.execute("SELECT COUNT(*) FROM digests WHERE email_sent = 0").fetchone()[0]
    assert row_count == 2
    # get_pending_digests orders oldest first, so the last entry is newest.
    assert pending_digest_id != pending[-1][0]  # the newest-pending id is the freshly created one


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

    def fake_summarize(
        items,
        failed_sources,
        recent_coverage,
        model,
        timeout_seconds,
        effort,
        recent_arcs="",
        **_kwargs,
    ):
        summarize_calls.append(items)
        return "## Needs attention\n...batch...", [], [], _MODEL_RUN

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), [])

    assert ok is True
    assert len(summarize_calls) == 1  # exactly one Opus call this run
    assert [i.source_id for i in summarize_calls[0]] == ["1", "2"]  # oldest batch, size-bounded
    assert count_unsummarized_items(conn) == 1  # item "3" left for the next run
    assert [i.source_id for i in get_unsummarized_items(conn)] == ["3"]


def test_deliver_passes_the_same_selected_subset_to_summarize_and_create_digest(conn, monkeypatch):
    # P1 fix: the selection pass (select_balanced_items_for_prompt) must run
    # BEFORE both summarize() and create_digest(), and both must receive its
    # output -- not the pre-shrink batch -- so the summarized set and the
    # stamped set never diverge. Monkeypatching the selection to a known,
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

    def fake_select_items_for_prompt(
        items,
        budget,
        failed_sources,
        recent_coverage,
        max_prompt_bytes,
        recent_arcs="",
    ):
        return selected_subset

    summarize_received = {}

    def fake_summarize(
        items,
        failed_sources,
        recent_coverage,
        model,
        timeout_seconds,
        effort,
        recent_arcs="",
        **_kwargs,
    ):
        summarize_received["items"] = items
        return "## Needs attention\n...selected...", [], [], _MODEL_RUN

    monkeypatch.setattr(main_mod, "select_balanced_items_for_prompt", fake_select_items_for_prompt)
    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
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

    monkeypatch.setattr(deliver_mod, "send_digest", fake_send_digest)

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

    monkeypatch.setattr(deliver_mod, "send_digest", fake_send_digest)

    ok = deliver_channels(
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


def test_deliver_channels_daily_kind_uses_get_daily_allowed_urls(conn, monkeypatch):
    # Regression test for the daily-brief link-provenance bug (the reason
    # get_daily_allowed_urls exists at all, see its docstring in
    # digest/state.py): a "daily" digest stamps NO items of its own, so
    # get_digest_item_urls(conn, digest_id) against it always returns the
    # empty set. deliver_channels used to derive its allowlist that way
    # UNCONDITIONALLY, which silently defanged every citation link on every
    # channel for a daily brief. For kind="daily" it must instead derive the
    # allowlist via get_daily_allowed_urls -- the union of item URLs across
    # in-window WINDOW digests -- which is non-empty here and contains the
    # source window digest's own stamped item URL.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    window_digest_id = create_digest(conn, "window body", get_unsummarized_items(conn))
    conn.execute(
        "UPDATE digests SET created_at = ? WHERE id = ?",
        (_recent_created_at(hours_ago=1), window_digest_id),
    )
    conn.commit()
    daily_digest_id = create_digest(
        conn, "**TL;DR:** the day\n\n## An arc\n\nstuff", [], kind="daily"
    )
    daily_created_at = _recent_created_at(hours_ago=0.5)
    conn.execute(
        "UPDATE digests SET created_at = ? WHERE id = ?", (daily_created_at, daily_digest_id)
    )
    conn.commit()

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

    monkeypatch.setattr(deliver_mod, "send_digest", fake_send_digest)

    ok = deliver_channels(
        conn,
        _cfg(),
        daily_digest_id,
        "**TL;DR:** the day\n\n## An arc\n\nstuff",
        1,
        daily_created_at,
        _NO_CHANNELS_DONE,
        _fresh_telegram_state(),
        kind="daily",
    )

    assert ok is True
    assert captured["allowed_urls"] == {"https://t.me/c/123/1"}


def test_deliver_site_publishes_rendered_html_and_marks_site_published(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Worth knowing\n\nstuff"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))
    allowed_urls = get_digest_item_urls(conn, digest_id)

    captured = {}

    def fake_publish(
        digest_id_,
        body_md_,
        body_html,
        created_at,
        item_count,
        publish_url,
        key,
        *,
        body_md_hu=None,
        body_html_hu=None,
        kind="window",
        source_counts=None,
        failed_sources=None,
        topics=None,
        deltas=None,
        arc_contexts=None,
        provenance=None,
    ):
        captured.update(
            digest_id=digest_id_,
            body_html=body_html,
            created_at=created_at,
            item_count=item_count,
            publish_url=publish_url,
            ingest_key=key,
            body_md_hu=body_md_hu,
            body_html_hu=body_html_hu,
            source_counts=source_counts,
            failed_sources=failed_sources,
            topics=topics,
        )

    monkeypatch.setattr(deliver_mod, "publish_to_site", fake_publish)

    cfg = _multichannel_cfg()
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is True
    assert captured["digest_id"] == digest_id
    assert "<h2>Worth knowing</h2>" in captured["body_html"]
    assert captured["created_at"] == "2026-07-29T10:00:00+00:00"
    assert captured["item_count"] == 1
    assert captured["publish_url"] == cfg.site_publish_url
    assert captured["ingest_key"] == cfg.site_ingest_key
    assert captured["body_md_hu"] is None
    assert captured["body_html_hu"] is None
    # English body_html must never get the Hungarian display-time label
    # swap -- localize_tldr_label_hu is only ever applied to body_html_hu.
    assert "Röviden" not in captured["body_html"]
    # _deliver_site computes these itself (from conn/digest_id/body_md) and
    # forwards them -- this digest has one telegram item and no failure
    # banner, so that's exactly what publish_to_site should have received.
    assert captured["source_counts"] == {"telegram": 1}
    assert captured["failed_sources"] == []
    row = conn.execute("SELECT site_published FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (1,)


def test_deliver_site_renders_and_forwards_hu_fields_when_body_md_hu_given(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Worth knowing\n\nstuff"
    body_md_hu = "**TL;DR:** szia\n\n## Érdemes tudni\n\ndolog"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn), body_md_hu=body_md_hu)
    allowed_urls = get_digest_item_urls(conn, digest_id)

    captured = {}

    def fake_publish(
        digest_id_,
        body_md_,
        body_html,
        created_at,
        item_count,
        publish_url,
        key,
        *,
        body_md_hu=None,
        body_html_hu=None,
        kind="window",
        source_counts=None,
        failed_sources=None,
        topics=None,
        deltas=None,
        arc_contexts=None,
        provenance=None,
    ):
        captured.update(body_md_hu=body_md_hu, body_html_hu=body_html_hu)

    monkeypatch.setattr(deliver_mod, "publish_to_site", fake_publish)

    cfg = _multichannel_cfg()
    ok = _deliver_site(
        conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls, body_md_hu
    )

    assert ok is True
    assert captured["body_md_hu"] == body_md_hu
    assert "<h2>Érdemes tudni</h2>" in captured["body_html_hu"]
    # The rendered HTML the site displays gets the Hungarian callout label
    # swap applied -- the underlying markdown (asserted above via
    # captured["body_md_hu"] == body_md_hu) keeps the literal "**TL;DR:**"
    # marker completely untouched.
    assert ">Röviden</span>" in captured["body_html_hu"]
    assert ">TL;DR</span>" not in captured["body_html_hu"]


def test_deliver_site_forwards_derive_topics_output_to_publish_to_site(conn, monkeypatch):
    # _deliver_site computes topics itself (digest/publish.py's
    # derive_topics, off this same English body_md) and forwards it --
    # mirrors test_deliver_site_publishes_rendered_html_and_marks_site_
    # published's own source_counts/failed_sources assertions above.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Story one\n\ntext\n\n## Story two\n\ntext"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))
    allowed_urls = get_digest_item_urls(conn, digest_id)

    captured = {}

    def fake_publish(
        digest_id_,
        body_md_,
        body_html,
        created_at,
        item_count,
        publish_url,
        key,
        *,
        body_md_hu=None,
        body_html_hu=None,
        kind="window",
        source_counts=None,
        failed_sources=None,
        topics=None,
        deltas=None,
        arc_contexts=None,
        provenance=None,
    ):
        captured.update(topics=topics)

    monkeypatch.setattr(deliver_mod, "publish_to_site", fake_publish)

    cfg = _multichannel_cfg()
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is True
    assert captured["topics"] == [
        {"slug": "story-one", "label": "Story one"},
        {"slug": "story-two", "label": "Story two"},
    ]


def test_deliver_site_forwards_arc_contexts_from_storage(conn, monkeypatch):
    # PLAN.md §11.6: unlike topics/deltas above, arc_contexts is NOT scoped
    # to this digest_id at all -- _deliver_site attaches the FULL current
    # arc_context table snapshot to every site publish, whichever digest it
    # happens to be for (see get_unsynced_arc_contexts' own docstring for why).
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Worth knowing\n\nstuff"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))
    write_arc_context(conn, "hormuz", "Background about Hormuz.")
    allowed_urls = get_digest_item_urls(conn, digest_id)

    captured = {}

    def fake_publish(
        digest_id_,
        body_md_,
        body_html,
        created_at,
        item_count,
        publish_url,
        key,
        *,
        body_md_hu=None,
        body_html_hu=None,
        kind="window",
        source_counts=None,
        failed_sources=None,
        topics=None,
        deltas=None,
        arc_contexts=None,
        provenance=None,
    ):
        captured.update(arc_contexts=arc_contexts)

    monkeypatch.setattr(deliver_mod, "publish_to_site", fake_publish)

    cfg = _multichannel_cfg()
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is True
    assert captured["arc_contexts"] == [{"key": "hormuz", "context_md": "Background about Hormuz."}]


def test_deliver_site_omits_arc_contexts_when_none_generated(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Worth knowing\n\nstuff"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))
    allowed_urls = get_digest_item_urls(conn, digest_id)

    captured = {}
    monkeypatch.setattr(
        deliver_mod,
        "publish_to_site",
        lambda *a, arc_contexts=None, **k: captured.update(arc_contexts=arc_contexts),
    )

    cfg = _multichannel_cfg()
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is True
    assert captured["arc_contexts"] == []


def _http_error(code: int) -> urllib.error.HTTPError:
    """An HTTPError carrying just a status code -- what _deliver_site branches on."""
    return urllib.error.HTTPError("https://site.example/ingest/1", code, "nope", {}, None)


def _site_fixture(conn):
    """One digest with a stored primer, plus the args _deliver_site wants."""
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Worth knowing\n\nstuff"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))
    write_arc_context(conn, "hormuz", "Background about Hormuz.")
    return digest_id, body_md, get_digest_item_urls(conn, digest_id)


def test_deliver_site_retries_without_arc_contexts_on_400(conn, monkeypatch):
    # The 2026-08-27 outage shape (digest 249): the site's validator 400s the
    # whole PUT over the optional arc_contexts field, and because every retry
    # re-sends the identical payload, "retry next run" becomes fail-forever.
    # The digest itself must still get published, with the field dropped.
    digest_id, body_md, allowed_urls = _site_fixture(conn)

    attempts = []

    def fake_publish(*a, arc_contexts=None, **k):
        attempts.append(arc_contexts)
        if arc_contexts:
            raise _http_error(400)

    monkeypatch.setattr(deliver_mod, "publish_to_site", fake_publish)

    cfg = _multichannel_cfg()
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is True
    # Exactly two attempts: the full payload, then the degraded one.
    assert len(attempts) == 2
    assert attempts[0] == [{"key": "hormuz", "context_md": "Background about Hormuz."}]
    assert attempts[1] is None
    # A degraded publish is still a publish -- the flag must be set, or the
    # next run would retry a digest the site already has.
    row = conn.execute("SELECT site_published FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (1,)


def test_deliver_site_stamps_synced_only_for_keys_it_carried(conn, monkeypatch):
    # The delta's closing half: a primer that made it onto the wire must stop
    # riding subsequent publishes.
    digest_id, body_md, allowed_urls = _site_fixture(conn)
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)

    cfg = _multichannel_cfg()
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is True
    assert get_unsynced_arc_contexts(conn) == []


def test_deliver_site_degraded_publish_does_not_stamp_synced(conn, monkeypatch):
    # The trap this whole design turns on: the degraded 400-retry path
    # publishes with arc_contexts=None and SUCCEEDS. Stamping on success
    # alone would mark primers the site never received as delivered --
    # stranding them permanently, a nastier bug than the outage the degraded
    # path exists to survive. They must stay unsynced and ride the next
    # healthy publish.
    digest_id, body_md, allowed_urls = _site_fixture(conn)

    def fake_publish(*a, arc_contexts=None, **k):
        if arc_contexts:
            raise _http_error(400)

    monkeypatch.setattr(deliver_mod, "publish_to_site", fake_publish)

    cfg = _multichannel_cfg()
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is True
    # The digest published; the primer did not.
    assert conn.execute(
        "SELECT site_published FROM digests WHERE id = ?", (digest_id,)
    ).fetchone() == (1,)
    assert get_unsynced_arc_contexts(conn) == [
        {"key": "hormuz", "context_md": "Background about Hormuz."}
    ]


def test_deliver_site_degraded_publish_logs_at_error(conn, monkeypatch, caplog):
    # Owner decision: the request SUCCEEDED, but the app/site contract
    # mismatch behind it does not heal on its own and shows up nowhere else,
    # so a silent (or merely WARNING-level) degrade would repeat exactly the
    # invisibility that let the 2026-08-27 outage sit unnoticed.
    digest_id, body_md, allowed_urls = _site_fixture(conn)

    def fake_publish(*a, arc_contexts=None, **k):
        if arc_contexts:
            raise _http_error(400)

    monkeypatch.setattr(deliver_mod, "publish_to_site", fake_publish)

    cfg = _multichannel_cfg()
    with caplog.at_level(logging.ERROR, logger="digest.deliver"):
        ok = _deliver_site(
            conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls
        )

    assert ok is True
    assert [r.levelname for r in caplog.records] == ["ERROR"]
    assert "PUBLISHED WITHOUT" in caplog.records[0].getMessage()


def test_deliver_site_does_not_retry_400_when_no_arc_contexts_were_sent(conn, monkeypatch):
    # Nothing to degrade: the 400 is about some OTHER field, so a second
    # identical-minus-nothing request would just be a doomed extra call.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Worth knowing\n\nstuff"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))
    allowed_urls = get_digest_item_urls(conn, digest_id)

    attempts = []

    def fake_publish(*a, arc_contexts=None, **k):
        attempts.append(arc_contexts)
        raise _http_error(400)

    monkeypatch.setattr(deliver_mod, "publish_to_site", fake_publish)

    cfg = _multichannel_cfg()
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is False
    assert len(attempts) == 1
    row = conn.execute("SELECT site_published FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (0,)


@pytest.mark.parametrize("code", [401, 500])
def test_deliver_site_does_not_degrade_on_non_400(conn, monkeypatch, code):
    # A wrong ingest key (401) or a site that is down (5xx) cannot be fixed by
    # dropping a payload field -- degrading there buys a second doomed request
    # and nothing else. Only 400 is retried.
    digest_id, body_md, allowed_urls = _site_fixture(conn)

    attempts = []

    def fake_publish(*a, arc_contexts=None, **k):
        attempts.append(arc_contexts)
        raise _http_error(code)

    monkeypatch.setattr(deliver_mod, "publish_to_site", fake_publish)

    cfg = _multichannel_cfg()
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is False
    assert len(attempts) == 1
    row = conn.execute("SELECT site_published FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (0,)


def test_deliver_site_fails_when_degraded_retry_also_fails(conn, monkeypatch):
    # Degrading is one attempt, not a retry loop: if the stripped payload is
    # rejected too, the digest stays pending for the next run as before.
    digest_id, body_md, allowed_urls = _site_fixture(conn)

    attempts = []

    def fake_publish(*a, arc_contexts=None, **k):
        attempts.append(arc_contexts)
        raise _http_error(400)

    monkeypatch.setattr(deliver_mod, "publish_to_site", fake_publish)

    cfg = _multichannel_cfg()
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is False
    assert len(attempts) == 2
    row = conn.execute("SELECT site_published FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (0,)


def test_deliver_site_omits_key_when_arc_keys_site_enabled_is_false(conn, monkeypatch):
    # Kill-switch behaviour. The flag now DEFAULTS on (the site half shipped
    # first and accepts the per-entry "key"), so this pins the off case
    # explicitly rather than leaning on the default: with the flag off,
    # "key" must not reach the payload even though the digest's OWN
    # arc_keys table already has the mapping persisted (write_arc_keys runs
    # unconditionally, mirroring write_deltas). That is what makes a site
    # rollback survivable without an app rollback -- validateTopics 400s the
    # WHOLE PUT on a per-entry field it doesn't recognise.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Story one\n\ntext"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))
    write_arc_keys(conn, digest_id, [{"slug": "story-one", "label": "Story one", "key": "arc-1"}])
    allowed_urls = get_digest_item_urls(conn, digest_id)

    captured = {}
    monkeypatch.setattr(
        deliver_mod,
        "publish_to_site",
        lambda *a, topics=None, **k: captured.update(topics=topics),
    )

    cfg = replace(_multichannel_cfg(), arc_keys_site_enabled=False)
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is True
    assert captured["topics"] == [{"slug": "story-one", "label": "Story one"}]


def test_deliver_site_includes_key_when_arc_keys_site_enabled_is_true(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Story one\n\ntext\n\n## Story two\n\ntext"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))
    write_arc_keys(conn, digest_id, [{"slug": "story-one", "label": "Story one", "key": "arc-1"}])
    allowed_urls = get_digest_item_urls(conn, digest_id)

    captured = {}
    monkeypatch.setattr(
        deliver_mod,
        "publish_to_site",
        lambda *a, topics=None, **k: captured.update(topics=topics),
    )

    cfg = _multichannel_cfg(arc_keys_site_enabled=True)
    ok = _deliver_site(conn, cfg, digest_id, body_md, 1, "2026-07-29T10:00:00+00:00", allowed_urls)

    assert ok is True
    # story-one carries the persisted key; story-two has none stored, so it
    # is emitted exactly as before, with no "key" at all.
    assert captured["topics"] == [
        {"slug": "story-one", "label": "Story one", "key": "arc-1"},
        {"slug": "story-two", "label": "Story two"},
    ]


def test_deliver_site_pending_resend_reads_arc_keys_back_from_storage(conn, monkeypatch):
    # A pending resend never re-summarizes, so the raw ```arcs fence is long
    # gone -- the "key" must come back from the arc_keys table, the same
    # "read state back instead of re-deriving it" pattern deltas uses (see
    # get_arc_keys' own docstring).
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    body_md = "**TL;DR:** hi\n\n## Story one\n\ntext"
    digest_id = create_digest(conn, body_md, get_unsummarized_items(conn))
    write_arc_keys(conn, digest_id, [{"slug": "story-one", "label": "Story one", "key": "arc-1"}])

    assert get_arc_keys(conn, digest_id) == {"story-one": "arc-1"}

    captured = {}
    monkeypatch.setattr(
        deliver_mod,
        "publish_to_site",
        lambda *a, topics=None, **k: captured.update(topics=topics),
    )

    cfg = _multichannel_cfg(arc_keys_site_enabled=True)
    # Simulate a MUCH LATER pending-resend call -- digest_id and body_md are
    # all that survive, exactly what deliver_pending passes in.
    ok = _deliver_site(
        conn,
        cfg,
        digest_id,
        body_md,
        1,
        "2026-07-29T10:00:00+00:00",
        get_digest_item_urls(conn, digest_id),
    )

    assert ok is True
    assert captured["topics"] == [{"slug": "story-one", "label": "Story one", "key": "arc-1"}]


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

    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", fake_send)

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
        deliver_mod, "send_digest", lambda *a, **k: (_ for _ in ()).throw(OSError("smtp down"))
    )
    site_calls = []
    telegram_calls = []
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: site_calls.append(a[0]))
    monkeypatch.setattr(
        deliver_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(a[0])
    )

    ok = deliver_channels(
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


def test_deliver_channels_site_failure_skips_telegram_this_run(conn, monkeypatch, caplog):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Worth knowing\n\nstuff", [])

    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(
        deliver_mod, "publish_to_site", lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
    )
    telegram_calls = []
    monkeypatch.setattr(
        deliver_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(a[0])
    )

    with caplog.at_level("INFO", logger=deliver_mod.logger.name):
        ok = deliver_channels(
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

    # digest_delivery Loki line: telegram must read "skipped" (attempted
    # nothing, distinct from "failed") since it's the site publish that
    # actually failed here.
    line = next(r.message for r in caplog.records if r.message.startswith("digest_delivery "))
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["email"] == "sent"
    assert payload["site"] == "failed"
    assert payload["telegram"] == "skipped"


def test_deliver_channels_second_run_only_retries_the_failed_channel(conn, monkeypatch):
    # Simulates a partial success persisting across two runs: email and
    # telegram succeeded on "run 1" (site failed), so "run 2" must retry
    # ONLY site -- email/telegram must not be attempted (and thus not
    # duplicated) a second time.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Worth knowing\n\nstuff", [])

    email_calls = []
    telegram_calls = []
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: email_calls.append(1))
    monkeypatch.setattr(
        deliver_mod, "publish_to_site", lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
    )
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(1))

    cfg = _multichannel_cfg()
    body_md = "**TL;DR:** hi\n\n## Worth knowing\n\nstuff"

    ok_run1 = deliver_channels(
        conn,
        cfg,
        digest_id,
        body_md,
        1,
        _recent_created_at(),
        _NO_CHANNELS_DONE,
        _fresh_telegram_state(),
    )
    assert ok_run1 is False
    assert email_calls == [1]
    assert telegram_calls == []  # site wasn't done yet, so telegram was skipped run 1

    # Run 2: fix the site publish, re-derive `done` the way _deliver does.
    def working_publish(*a, **k):
        return None

    monkeypatch.setattr(deliver_mod, "publish_to_site", working_publish)
    row = conn.execute(
        "SELECT email_sent, site_published, telegram_sent FROM digests WHERE id = ?",
        (digest_id,),
    ).fetchone()
    done = {"email": bool(row[0]), "site": bool(row[1]), "telegram": bool(row[2])}

    ok_run2 = deliver_channels(
        conn,
        cfg,
        digest_id,
        body_md,
        1,
        _recent_created_at(),
        done,
        _fresh_telegram_state(),
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
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    telegram_calls = []
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(1))

    ok = deliver_channels(
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
        deliver_mod, "send_digest", lambda *a, **k: (_ for _ in ()).throw(AssertionError("boom"))
    )
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)

    cfg = replace(
        _cfg(),
        email_enabled=False,
        site_publish_url="https://news-site.example.workers.dev",
        site_ingest_key="ingest-secret",
    )

    ok = deliver_channels(
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

    monkeypatch.setattr(
        main_mod, "summarize", lambda *a, **k: ("**TL;DR:** hi\n\n## Section", [], [], _MODEL_RUN)
    )
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(
        deliver_mod, "publish_to_site", lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
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
# See docs/incidents/2026-08-06-telegram-flood.md for the incident these
# two guards exist to make unrepeatable.


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
        deliver_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(a[0])
    )

    ok = deliver_channels(
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


def test_deliver_channels_telegram_freshness_window_still_sends_a_fresh_digest(conn, monkeypatch):
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Worth knowing\n\nstuff", [])

    telegram_calls = []
    monkeypatch.setattr(
        deliver_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(a[0])
    )

    ok = deliver_channels(
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


def test_deliver_channels_telegram_freshness_window_sends_between_the_old_and_new_bound(
    conn, monkeypatch
):
    """Pins `_TELEGRAM_MAX_AGE` at 24h specifically, not merely "some window".

    18h ago is INSIDE the current 24h window but OUTSIDE the 12h one the
    guard held while the timer ran 3-hourly. The two tests above both pass
    at either value (1h is fresh under both, 25h is stale under both), so
    without this one nothing in the suite would notice the window being
    narrowed back to 12h -- which would silently drop the catch-up budget
    from ~3 failed runs to ~1 at the 6-hourly cadence.
    """
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Worth knowing\n\nstuff", [])

    telegram_calls = []
    monkeypatch.setattr(
        deliver_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(a[0])
    )

    ok = deliver_channels(
        conn,
        _telegram_only_cfg(),
        digest_id,
        "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        1,
        _recent_created_at(hours_ago=18),
        _NO_CHANNELS_DONE,
        _fresh_telegram_state(),
    )

    assert ok is True
    assert telegram_calls == [digest_id]


def test_deliver_incident_replay_stale_backlog_all_skip_telegram_only_fresh_one_sends(
    conn, monkeypatch
):
    # Replays the 2026-08-06 incident shape at the `_deliver` level: a
    # backlog of ~10 pre-cutover digests with telegram_sent=0 (standing in
    # for the ALTER TABLE migration's DEFAULT 0 on every pre-existing row)
    # plus one genuinely fresh digest. GUARD 1 must mark every stale one
    # sent WITHOUT ever calling send_telegram_tldr, and still deliver the
    # fresh one normally -- and the whole run must come back green. Full
    # incident writeup: docs/incidents/2026-08-06-telegram-flood.md
    stale_ids = []
    for i in range(10):
        digest_id = create_digest(conn, f"**TL;DR:** old {i}\n\n## Worth knowing\n\nstuff", [])
        conn.execute(
            "UPDATE digests SET created_at = ? WHERE id = ?", (_stale_created_at(), digest_id)
        )
        conn.commit()
        stale_ids.append(digest_id)

    fresh_id = create_digest(conn, "**TL;DR:** fresh\n\n## Worth knowing\n\nstuff", [])
    conn.execute("UPDATE digests SET created_at = ? WHERE id = ?", (_recent_created_at(), fresh_id))
    conn.commit()

    telegram_calls = []
    monkeypatch.setattr(
        deliver_mod, "send_telegram_tldr", lambda *a, **k: telegram_calls.append(a[0])
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

    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", fake_send)

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

    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", fake_send)

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


def test_run_calls_prune_delivered_items_with_the_configured_channel_flags(monkeypatch, tmp_path):
    # Window-mode _run must prune old, fully-delivered items after delivery,
    # every run -- not just when there happens to be something to prune.
    # `_cfg()`'s defaults (no `replace` overrides here) are email_enabled=True
    # (Config's own default) with site_publish_url/telegram_notify_bot_token
    # both unset, so `_run` must derive site_enabled/telegram_enabled as
    # False exactly the way digest/deliver.py's own _deliver_channels does.
    cfg = replace(_cfg(), state_db_path=str(tmp_path / "state.db"))

    _patch_telegram_client(monkeypatch, CollectResult())

    prune_calls = []

    def fake_prune(conn, *, email_enabled, site_enabled, telegram_enabled):
        prune_calls.append((email_enabled, site_enabled, telegram_enabled))
        return 0

    monkeypatch.setattr(main_mod, "prune_delivered_items", fake_prune)

    ok = asyncio.run(main_mod._run(cfg))

    assert ok is True
    assert prune_calls == [(True, False, False)]


def test_run_logs_run_summary_line_with_exactly_the_enabled_collectors(
    monkeypatch, tmp_path, caplog
):
    # Telegram is always enabled; X is turned on here via x_enabled=True.
    # news/polymarket/reddit/hackernews stay off (plain _cfg() defaults), so
    # the run_summary line's "collectors" dict must contain exactly telegram+x,
    # not every collector this module knows how to run. Reuses this test
    # file's own end-to-end _run() wiring pattern (see the test right below,
    # which this one is placed ahead of).
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

    monkeypatch.setattr(
        main_mod, "commit_new_items", lambda conn, items, cursor_updates: len(items)
    )
    monkeypatch.setattr(
        main_mod, "_deliver", lambda conn, cfg, failed_sources, hidden=frozenset(): True
    )

    with caplog.at_level("INFO", logger=main_mod.logger.name):
        ok = asyncio.run(main_mod._run(cfg))

    assert ok is True

    line = next(r.message for r in caplog.records if r.message.startswith("run_summary "))
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["mode"] == "window"
    assert payload["collectors"] == {"telegram": "ok", "x": "ok"}
    assert payload["items_collected"] == 2
    assert payload["items_inserted"] == 2
    assert payload["delivered"] is True
    assert payload["ok"] is True


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
    monkeypatch.setattr(
        main_mod, "_deliver", lambda conn, cfg, failed_sources, hidden=frozenset(): True
    )

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

    def fake_deliver(conn, cfg, failed_sources, hidden=frozenset()):
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
    monkeypatch.setattr(
        main_mod, "_deliver", lambda conn, cfg, failed_sources, hidden=frozenset(): True
    )

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
    monkeypatch.setattr(
        main_mod, "_deliver", lambda conn, cfg, failed_sources, hidden=frozenset(): True
    )

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

    def fake_deliver(conn, cfg, failed_sources, hidden=frozenset()):
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
    cfg = replace(_cfg(), state_db_path=str(tmp_path / "state.db"), polymarket_enabled=True)

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
    monkeypatch.setattr(
        main_mod, "_deliver", lambda conn, cfg, failed_sources, hidden=frozenset(): True
    )

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
    cfg = replace(_cfg(), state_db_path=str(tmp_path / "state.db"), polymarket_enabled=False)
    _patch_telegram_client(monkeypatch, CollectResult())

    def fake_commit_new_items(conn, items, cursor_updates):
        return len(items)

    monkeypatch.setattr(main_mod, "commit_new_items", fake_commit_new_items)
    monkeypatch.setattr(
        main_mod, "_deliver", lambda conn, cfg, failed_sources, hidden=frozenset(): True
    )

    ok = asyncio.run(main_mod._run(cfg))

    assert ok is True


def test_run_polymarket_failure_surfaces_in_failed_sources(monkeypatch, tmp_path):
    cfg = replace(_cfg(), state_db_path=str(tmp_path / "state.db"), polymarket_enabled=True)

    _patch_telegram_client(monkeypatch, CollectResult())
    monkeypatch.setattr(
        main_mod.polymarket_collector,
        "collect",
        lambda *a, **k: PolymarketCollectResult(failed=True),
    )

    captured = {}

    def fake_deliver(conn, cfg, failed_sources, hidden=frozenset()):
        captured["failed_sources"] = failed_sources
        return True

    monkeypatch.setattr(main_mod, "_deliver", fake_deliver)

    ok = asyncio.run(main_mod._run(cfg))

    assert captured["failed_sources"] == ["polymarket"]
    assert ok is False


# --- _run_reddit_collector (Reddit collector, Config.reddit_enabled-gated) ---


def test_run_reddit_disabled_never_calls_collector():
    cfg = replace(_cfg(), reddit_enabled=False)

    result = _run_reddit_collector(cfg)

    assert result == CollectResult()


def test_run_reddit_disabled_collect_function_untouched(monkeypatch):
    cfg = replace(_cfg(), reddit_enabled=False)

    def boom_collect(*a, **k):
        raise AssertionError("reddit_collector.collect must not be called when disabled")

    monkeypatch.setattr(main_mod.reddit_collector, "collect", boom_collect)

    result = _run_reddit_collector(cfg)

    assert result == CollectResult()


def test_run_reddit_enabled_delegates_to_collect_with_configured_params(monkeypatch):
    cfg = replace(
        _cfg(),
        reddit_enabled=True,
        reddit_session_cookie="cookie-value",
        reddit_subreddits=("news", "hungary"),
        reddit_posts_per_sub=15,
    )
    expected = CollectResult(items=[_item("reddit-1")])

    captured = {}

    def fake_collect(session_cookie, subreddits, posts_per_sub):
        captured["session_cookie"] = session_cookie
        captured["subreddits"] = subreddits
        captured["posts_per_sub"] = posts_per_sub
        return expected

    monkeypatch.setattr(main_mod.reddit_collector, "collect", fake_collect)

    result = _run_reddit_collector(cfg)

    assert result is expected
    assert captured == {
        "session_cookie": "cookie-value",
        "subreddits": ("news", "hungary"),
        "posts_per_sub": 15,
    }


def test_run_reddit_collector_crash_is_caught_returns_failed_result(monkeypatch):
    cfg = replace(_cfg(), reddit_enabled=True)

    def boom_collect(*a, **k):
        raise RuntimeError("reddit api shape changed")

    monkeypatch.setattr(main_mod.reddit_collector, "collect", boom_collect)

    result = _run_reddit_collector(cfg)

    assert result.failed is True
    assert result.items == []


# --- _run: reddit wiring end to end (merge into commit, failed_sources) ---


def test_run_merges_reddit_items_into_commit_alongside_telegram(monkeypatch, tmp_path):
    cfg = replace(_cfg(), state_db_path=str(tmp_path / "state.db"), reddit_enabled=True)

    tg_item = _item("1")
    reddit_item = Item(
        source="reddit",
        source_id="h1",
        chat_id=None,
        chat_title="r/hungary",
        author="bob",
        text="headline [score 100, 5 comments] body",
        url="https://www.reddit.com/r/hungary/comments/h1/napi/",
        fetched_at="2026-07-29T10:00:00+00:00",
    )

    _patch_telegram_client(
        monkeypatch, CollectResult(items=[tg_item], cursor_updates={("telegram", "123"): "1"})
    )

    def fake_reddit_collect(session_cookie, subreddits, posts_per_sub):
        return CollectResult(items=[reddit_item])

    monkeypatch.setattr(main_mod.reddit_collector, "collect", fake_reddit_collect)

    captured = {}

    def fake_commit_new_items(conn, items, cursor_updates):
        captured["items"] = items
        captured["cursor_updates"] = cursor_updates
        return len(items)

    monkeypatch.setattr(main_mod, "commit_new_items", fake_commit_new_items)
    monkeypatch.setattr(
        main_mod, "_deliver", lambda conn, cfg, failed_sources, hidden=frozenset(): True
    )

    ok = asyncio.run(main_mod._run(cfg))

    assert ok is True
    assert captured["items"] == [tg_item, reddit_item]
    # reddit never contributes a cursor_update -- only telegram's shows up.
    assert captured["cursor_updates"] == {("telegram", "123"): "1"}


def test_run_reddit_collector_failure_surfaces_in_failed_sources(monkeypatch, tmp_path):
    cfg = replace(_cfg(), state_db_path=str(tmp_path / "state.db"), reddit_enabled=True)

    _patch_telegram_client(monkeypatch, CollectResult())
    monkeypatch.setattr(
        main_mod.reddit_collector, "collect", lambda *a, **k: CollectResult(failed=True)
    )

    captured = {}

    def fake_deliver(conn, cfg, failed_sources, hidden=frozenset()):
        captured["failed_sources"] = failed_sources
        return True

    monkeypatch.setattr(main_mod, "_deliver", fake_deliver)

    ok = asyncio.run(main_mod._run(cfg))

    assert captured["failed_sources"] == ["reddit"]
    assert ok is False


# --- _run_hackernews_collector (Hacker News collector,
#     Config.hackernews_enabled-gated) ---


def test_run_hackernews_disabled_never_calls_collector():
    cfg = replace(_cfg(), hackernews_enabled=False)

    result = _run_hackernews_collector(cfg)

    assert result == CollectResult()


def test_run_hackernews_disabled_collect_function_untouched(monkeypatch):
    cfg = replace(_cfg(), hackernews_enabled=False)

    def boom_collect(*a, **k):
        raise AssertionError("hackernews_collector.collect must not be called when disabled")

    monkeypatch.setattr(main_mod.hackernews_collector, "collect", boom_collect)

    result = _run_hackernews_collector(cfg)

    assert result == CollectResult()


def test_run_hackernews_enabled_delegates_to_collect_with_configured_top_n(monkeypatch):
    cfg = replace(_cfg(), hackernews_enabled=True, hackernews_top_n=20)
    expected = CollectResult(items=[_item("hackernews-1")])

    captured = {}

    def fake_collect(top_n):
        captured["top_n"] = top_n
        return expected

    monkeypatch.setattr(main_mod.hackernews_collector, "collect", fake_collect)

    result = _run_hackernews_collector(cfg)

    assert result is expected
    assert captured == {"top_n": 20}


def test_run_hackernews_collector_crash_is_caught_returns_failed_result(monkeypatch):
    cfg = replace(_cfg(), hackernews_enabled=True)

    def boom_collect(*a, **k):
        raise RuntimeError("hackernews api shape changed")

    monkeypatch.setattr(main_mod.hackernews_collector, "collect", boom_collect)

    result = _run_hackernews_collector(cfg)

    assert result.failed is True
    assert result.items == []


# --- _run: hackernews wiring end to end (merge into commit, failed_sources) ---


def test_run_merges_hackernews_items_into_commit_alongside_telegram(monkeypatch, tmp_path):
    cfg = replace(_cfg(), state_db_path=str(tmp_path / "state.db"), hackernews_enabled=True)

    tg_item = _item("1")
    hn_item = Item(
        source="hackernews",
        source_id="123456",
        chat_id=None,
        chat_title="Hacker News",
        author="submitter1",
        text="headline\n\n[score 500, 200 comments]",
        url="https://news.ycombinator.com/item?id=123456",
        fetched_at="2026-07-29T10:00:00+00:00",
    )

    _patch_telegram_client(
        monkeypatch, CollectResult(items=[tg_item], cursor_updates={("telegram", "123"): "1"})
    )

    def fake_hackernews_collect(top_n):
        return CollectResult(items=[hn_item])

    monkeypatch.setattr(main_mod.hackernews_collector, "collect", fake_hackernews_collect)

    captured = {}

    def fake_commit_new_items(conn, items, cursor_updates):
        captured["items"] = items
        captured["cursor_updates"] = cursor_updates
        return len(items)

    monkeypatch.setattr(main_mod, "commit_new_items", fake_commit_new_items)
    monkeypatch.setattr(
        main_mod, "_deliver", lambda conn, cfg, failed_sources, hidden=frozenset(): True
    )

    ok = asyncio.run(main_mod._run(cfg))

    assert ok is True
    assert captured["items"] == [tg_item, hn_item]
    # hackernews never contributes a cursor_update -- only telegram's shows up.
    assert captured["cursor_updates"] == {("telegram", "123"): "1"}


def test_run_hackernews_collector_failure_surfaces_in_failed_sources(monkeypatch, tmp_path):
    cfg = replace(_cfg(), state_db_path=str(tmp_path / "state.db"), hackernews_enabled=True)

    _patch_telegram_client(monkeypatch, CollectResult())
    monkeypatch.setattr(
        main_mod.hackernews_collector, "collect", lambda *a, **k: CollectResult(failed=True)
    )

    captured = {}

    def fake_deliver(conn, cfg, failed_sources, hidden=frozenset()):
        captured["failed_sources"] = failed_sources
        return True

    monkeypatch.setattr(main_mod, "_deliver", fake_deliver)

    ok = asyncio.run(main_mod._run(cfg))

    assert captured["failed_sources"] == ["hackernews"]
    assert ok is False


# --- _telegram_thread_id_for_kind / kind-aware Telegram thread selection
#     (daily-brief feature) ---


def test_telegram_thread_id_for_kind_daily_uses_configured_daily_thread():
    cfg = replace(_multichannel_cfg(), telegram_notify_thread_id=1, telegram_daily_thread_id=555)

    assert _telegram_thread_id_for_kind(cfg, "daily") == 555


def test_telegram_thread_id_for_kind_daily_falls_back_when_unset(caplog):
    cfg = replace(_multichannel_cfg(), telegram_notify_thread_id=42, telegram_daily_thread_id=None)

    with caplog.at_level("INFO"):
        thread_id = _telegram_thread_id_for_kind(cfg, "daily")

    assert thread_id == 42
    assert "TELEGRAM_DAILY_THREAD_ID" in caplog.text


def test_telegram_thread_id_for_kind_daily_explicit_zero_is_honored():
    # 0 is a legitimate real thread id -- it must NOT be treated the same as
    # "unset" and trigger the fallback.
    cfg = replace(_multichannel_cfg(), telegram_notify_thread_id=42, telegram_daily_thread_id=0)

    assert _telegram_thread_id_for_kind(cfg, "daily") == 0


def test_telegram_thread_id_for_kind_window_always_uses_notify_thread():
    cfg = replace(_multichannel_cfg(), telegram_notify_thread_id=42, telegram_daily_thread_id=555)

    assert _telegram_thread_id_for_kind(cfg, "window") == 42


def test_deliver_telegram_daily_kind_uses_daily_thread(conn, monkeypatch):
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Section\n\nstuff", [], kind="daily")

    captured = {}
    monkeypatch.setattr(
        deliver_mod,
        "send_telegram_tldr",
        lambda digest_id_, body_md_, created_at, bot_token, chat_id, thread_id, public_base: (
            captured.update(thread_id=thread_id)
        ),
    )

    cfg = replace(_multichannel_cfg(), telegram_notify_thread_id=1, telegram_daily_thread_id=555)
    ok = _deliver_telegram(
        conn,
        cfg,
        digest_id,
        "**TL;DR:** hi\n\n## Section\n\nstuff",
        _recent_created_at(),
        _fresh_telegram_state(),
        kind="daily",
    )

    assert ok is True
    assert captured["thread_id"] == 555


# --- _telegram_thread_id_for_kind / kind-aware Telegram thread selection
#     (weekly-brief feature) ---


def test_telegram_thread_id_for_kind_weekly_uses_configured_weekly_thread():
    cfg = replace(_multichannel_cfg(), telegram_notify_thread_id=1, telegram_weekly_thread_id=141)

    assert _telegram_thread_id_for_kind(cfg, "weekly") == 141


def test_telegram_thread_id_for_kind_weekly_falls_back_when_unset(caplog):
    cfg = replace(_multichannel_cfg(), telegram_notify_thread_id=42, telegram_weekly_thread_id=None)

    with caplog.at_level("INFO"):
        thread_id = _telegram_thread_id_for_kind(cfg, "weekly")

    assert thread_id == 42
    assert "TELEGRAM_WEEKLY_THREAD_ID" in caplog.text


def test_telegram_thread_id_for_kind_weekly_explicit_zero_is_honored():
    # 0 is a legitimate real thread id -- it must NOT be treated the same as
    # "unset" and trigger the fallback.
    cfg = replace(_multichannel_cfg(), telegram_notify_thread_id=42, telegram_weekly_thread_id=0)

    assert _telegram_thread_id_for_kind(cfg, "weekly") == 0


def test_deliver_telegram_weekly_kind_uses_weekly_thread(conn, monkeypatch):
    digest_id = create_digest(conn, "**TL;DR:** hi\n\n## Section\n\nstuff", [], kind="weekly")

    captured = {}
    monkeypatch.setattr(
        deliver_mod,
        "send_telegram_tldr",
        lambda digest_id_, body_md_, created_at, bot_token, chat_id, thread_id, public_base: (
            captured.update(thread_id=thread_id)
        ),
    )

    cfg = replace(_multichannel_cfg(), telegram_notify_thread_id=1, telegram_weekly_thread_id=141)
    ok = _deliver_telegram(
        conn,
        cfg,
        digest_id,
        "**TL;DR:** hi\n\n## Section\n\nstuff",
        _recent_created_at(),
        _fresh_telegram_state(),
        kind="weekly",
    )

    assert ok is True
    assert captured["thread_id"] == 141


def test_deliver_channels_weekly_kind_uses_get_weekly_allowed_urls(conn, monkeypatch):
    # Regression test for the weekly-brief link-provenance bug, one editorial
    # rung up from the daily-brief bug get_daily_allowed_urls fixes (see
    # test_deliver_channels_daily_kind_uses_get_daily_allowed_urls above): a
    # "weekly" digest stamps NO items of its own, and neither does the daily
    # digest it would otherwise fall back to, so get_digest_item_urls against
    # either always returns the empty set. deliver_channels must derive the
    # allowlist via get_weekly_allowed_urls for kind="weekly" -- the union of
    # item URLs across in-window WINDOW digests, two hops down -- which is
    # non-empty here and contains the source window digest's own stamped
    # item URL.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    window_digest_id = create_digest(conn, "window body", get_unsummarized_items(conn))
    conn.execute(
        "UPDATE digests SET created_at = ? WHERE id = ?",
        (_recent_created_at(hours_ago=1), window_digest_id),
    )
    conn.commit()
    weekly_digest_id = create_digest(
        conn, "**TL;DR:** the week\n\n## A thread\n\nstuff", [], kind="weekly"
    )
    weekly_created_at = _recent_created_at(hours_ago=0.5)
    conn.execute(
        "UPDATE digests SET created_at = ? WHERE id = ?", (weekly_created_at, weekly_digest_id)
    )
    conn.commit()

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

    monkeypatch.setattr(deliver_mod, "send_digest", fake_send_digest)

    ok = deliver_channels(
        conn,
        _cfg(),
        weekly_digest_id,
        "**TL;DR:** the week\n\n## A thread\n\nstuff",
        1,
        weekly_created_at,
        _NO_CHANNELS_DONE,
        _fresh_telegram_state(),
        kind="weekly",
    )

    assert ok is True
    assert captured["allowed_urls"] == {"https://t.me/c/123/1"}


# --- run_daily / _deliver_pending (daily-brief feature) ---


def _daily_cfg(**overrides) -> Config:
    """_multichannel_cfg() plus a distinct daily Telegram thread, for run_daily tests.

    email_enabled=False isolates these tests from the email channel entirely
    (mirrors `_telegram_only_cfg`'s own isolation rationale) -- run_daily's
    email behavior is identical to _deliver's own (EMAIL_ENABLED gates it
    exactly the same way, see run_daily's docstring), so it doesn't need its
    own coverage here.
    """
    return replace(
        _multichannel_cfg(), email_enabled=False, telegram_daily_thread_id=555, **overrides
    )


def _window_digest(conn, body_md: str, item_count: int, created_at: str) -> int:
    """Create a real, fully-delivered window digest with `item_count` stamped items.

    run_daily's own item_count-sum and allowed_urls-union logic reads real
    stamped item URLs and the real item_count column back off the digests
    table (via get_window_digests_since / get_digest_item_urls) -- so these
    tests need genuine window digests, not hand-inserted rows missing that
    data. All three channel flags are marked done (as if a prior 6-hourly
    run already delivered it) so run_daily's own `_deliver_pending` pass
    finds nothing left to retry for it -- isolating these tests to run_daily's
    OWN fresh daily-digest delivery, which is what they're checking.
    """
    items = [_item(f"{body_md}-{i}", fetched_at=created_at) for i in range(item_count)]
    commit_new_items(conn, items, {})
    digest_id = create_digest(conn, body_md, items)
    conn.execute(
        "UPDATE digests SET created_at = ?, email_sent = 1, site_published = 1, "
        "telegram_sent = 1 WHERE id = ?",
        (created_at, digest_id),
    )
    conn.commit()
    return digest_id


def test_run_daily_empty_window_is_a_no_op_returns_true(monkeypatch, tmp_path, caplog):
    def boom(*args, **kwargs):
        raise AssertionError("must not be called when there are no window digests to brief")

    cfg = replace(_cfg(), state_db_path=str(tmp_path / "state.db"))
    real_conn = connect(cfg.state_db_path)
    init_db(real_conn)
    real_conn.close()

    monkeypatch.setattr(main_mod, "summarize_daily", boom)
    monkeypatch.setattr(deliver_mod, "publish_to_site", boom)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", boom)
    monkeypatch.setattr(main_mod, "archive", boom)

    with caplog.at_level("INFO", logger=main_mod.logger.name):
        ok = run_daily(cfg)

    assert ok is True

    # run_summary must still fire on this early-return, empty-day path, with
    # source_digests: 0 -- Loki's only way to tell "empty day" apart from a
    # daily run that actually failed further along.
    line = next(r.message for r in caplog.records if r.message.startswith("run_summary "))
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["mode"] == "daily"
    assert payload["source_digests"] == 0
    assert payload["delivered"] is True
    assert payload["ok"] is True


def test_run_daily_happy_path_creates_and_delivers_daily_digest(conn, monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    _window_digest(real_conn, "window one", 3, _recent_created_at(hours_ago=6))
    _window_digest(real_conn, "window two", 5, _recent_created_at(hours_ago=2))
    real_conn.close()

    daily_calls = []

    def fake_summarize_daily(digest_rows, allowed_urls, model, timeout_seconds, effort, **_kwargs):
        daily_calls.append(
            dict(
                digest_rows=digest_rows,
                allowed_urls=allowed_urls,
                model=model,
                timeout_seconds=timeout_seconds,
                effort=effort,
            )
        )
        return "**TL;DR:** the day\n\n## An arc\n\nstuff", _MODEL_RUN

    monkeypatch.setattr(main_mod, "summarize_daily", fake_summarize_daily)

    site_calls = []
    telegram_calls = []
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: site_calls.append((a, k)))
    monkeypatch.setattr(
        deliver_mod,
        "send_telegram_tldr",
        lambda digest_id_, body_md_, created_at, bot_token, chat_id, thread_id, public_base: (
            telegram_calls.append(thread_id)
        ),
    )
    archived = {}
    monkeypatch.setattr(
        main_mod, "archive", lambda body_md, archive_dir, digest_id: archived.update(id=digest_id)
    )

    cfg = _daily_cfg(state_db_path=db_path)
    ok = run_daily(cfg)

    assert ok is True
    assert len(daily_calls) == 1
    call = daily_calls[0]
    assert call["model"] == cfg.anthropic_model
    assert call["timeout_seconds"] == cfg.claude_timeout_seconds
    assert call["effort"] == cfg.claude_effort
    # allowed_urls is the UNION of both source window digests' stamped item URLs.
    assert len(call["allowed_urls"]) == 8

    # The daily digest's own thread, not the window thread.
    assert telegram_calls == [555]
    assert len(site_calls) == 1
    assert archived["id"] is not None

    verify_conn = connect(db_path)
    row = verify_conn.execute(
        "SELECT kind, item_count FROM digests WHERE id = ?", (archived["id"],)
    ).fetchone()
    assert row == ("daily", 8)  # SUM of the two source digests' item_counts (3 + 5)
    verify_conn.close()


# --- run_daily duplicate-fire guard (a double-fire of the once-a-day timer
#     must be a harmless no-op, not a second daily brief) ---


def _existing_daily_digest(
    conn, hours_ago: float, *, telegram_sent: bool = True, site_published: bool = True
) -> int:
    """A real `kind='daily'` row `hours_ago` in the past, for the guard tests below.

    `telegram_sent`/`site_published` default to already-delivered (mirrors
    `_daily_digest`'s own "fully delivered" default further down, used by
    the run_weekly tests) so a test that only cares about the guard itself
    doesn't also have to reason about deliver_pending retrying this row --
    the one test that DOES care (the "still retries pending delivery" test
    below) passes `telegram_sent=False` explicitly.
    """
    digest_id = create_digest(conn, "**TL;DR:** yesterday\n\n## An arc\n\nstuff", [], kind="daily")
    conn.execute(
        "UPDATE digests SET created_at = ?, email_sent = 1, site_published = ?, "
        "telegram_sent = ? WHERE id = ?",
        (
            _recent_created_at(hours_ago=hours_ago),
            1 if site_published else 0,
            1 if telegram_sent else 0,
            digest_id,
        ),
    )
    conn.commit()
    return digest_id


def test_run_daily_duplicate_within_guard_window_is_skipped_returns_true(
    conn, monkeypatch, tmp_path, caplog
):
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    # Well within the guard window -- this is what a genuine double-fire of
    # the once-a-day timer looks like (the 2026-08-09 incident this guard
    # exists for: two runs 31 minutes apart).
    within_delta = main_mod._DAILY_DUPLICATE_GUARD_WINDOW - timedelta(hours=1)
    within_hours = within_delta.total_seconds() / 3600
    _existing_daily_digest(real_conn, within_hours)
    _window_digest(real_conn, "window one", 3, _recent_created_at(hours_ago=2))
    real_conn.close()

    def boom(*args, **kwargs):
        raise AssertionError("must not be called: the duplicate guard should skip this run")

    monkeypatch.setattr(main_mod, "summarize_daily", boom)
    monkeypatch.setattr(deliver_mod, "publish_to_site", boom)
    monkeypatch.setattr(main_mod, "archive", boom)

    cfg = _daily_cfg(state_db_path=db_path)
    with caplog.at_level("INFO", logger=main_mod.logger.name):
        ok = run_daily(cfg)

    assert ok is True

    verify_conn = connect(db_path)
    count = verify_conn.execute("SELECT COUNT(*) FROM digests WHERE kind = 'daily'").fetchone()[0]
    verify_conn.close()
    assert count == 1  # no second daily digest was created

    line = next(r.message for r in caplog.records if r.message.startswith("run_summary "))
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["mode"] == "daily"
    assert payload["skipped_duplicate"] is True
    assert payload["ok"] is True


def test_run_daily_older_than_guard_window_proceeds_normally(conn, monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    # Just outside the guard window -- a legitimate consecutive-day gap.
    outside_delta = main_mod._DAILY_DUPLICATE_GUARD_WINDOW + timedelta(hours=1)
    outside_hours = outside_delta.total_seconds() / 3600
    _existing_daily_digest(real_conn, outside_hours)
    _window_digest(real_conn, "window one", 3, _recent_created_at(hours_ago=2))
    real_conn.close()

    monkeypatch.setattr(
        main_mod,
        "summarize_daily",
        lambda *a, **k: ("**TL;DR:** today\n\n## An arc\n\nstuff", _MODEL_RUN),
    )
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = _daily_cfg(state_db_path=db_path)
    ok = run_daily(cfg)

    assert ok is True
    verify_conn = connect(db_path)
    count = verify_conn.execute("SELECT COUNT(*) FROM digests WHERE kind = 'daily'").fetchone()[0]
    verify_conn.close()
    assert count == 2  # yesterday's daily plus today's freshly created one


def test_run_daily_first_ever_run_no_prior_daily_digest_proceeds_normally(
    conn, monkeypatch, tmp_path
):
    # No `kind='daily'` row exists at all yet -- get_daily_digests_since must
    # return empty and the guard must not mistake "no prior runs" for
    # "recently ran".
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    _window_digest(real_conn, "window one", 3, _recent_created_at(hours_ago=2))
    real_conn.close()

    summarize_calls = []
    monkeypatch.setattr(
        main_mod,
        "summarize_daily",
        lambda *a, **k: (
            summarize_calls.append(1),
            ("**TL;DR:** today\n\n## An arc\n\nstuff", _MODEL_RUN),
        )[1],
    )
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = _daily_cfg(state_db_path=db_path)
    ok = run_daily(cfg)

    assert ok is True
    assert len(summarize_calls) == 1


def test_run_daily_force_bypasses_duplicate_guard(conn, monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    # Minutes-old duplicate -- would normally be skipped, but --force must
    # override the guard.
    _existing_daily_digest(real_conn, hours_ago=0.1)
    _window_digest(real_conn, "window one", 3, _recent_created_at(hours_ago=2))
    real_conn.close()

    summarize_calls = []
    monkeypatch.setattr(
        main_mod,
        "summarize_daily",
        lambda *a, **k: (
            summarize_calls.append(1),
            ("**TL;DR:** forced\n\n## An arc\n\nstuff", _MODEL_RUN),
        )[1],
    )
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = _daily_cfg(state_db_path=db_path)
    ok = run_daily(cfg, force=True)

    assert ok is True
    assert len(summarize_calls) == 1
    verify_conn = connect(db_path)
    count = verify_conn.execute("SELECT COUNT(*) FROM digests WHERE kind = 'daily'").fetchone()[0]
    verify_conn.close()
    assert count == 2


def test_run_daily_duplicate_skip_still_retries_pending_delivery(conn, monkeypatch, tmp_path):
    # The duplicate-fire guard must never suppress step 1 (deliver_pending):
    # a daily brief stuck pending on one channel must still be retried on a
    # run the guard is about to skip, or it would never be retried until the
    # NEXT day's daily run.
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    pending_id = _existing_daily_digest(
        real_conn, hours_ago=1, telegram_sent=False, site_published=True
    )
    real_conn.close()

    def boom_summarize(*args, **kwargs):
        raise AssertionError("must not be called: the duplicate guard should skip this run")

    monkeypatch.setattr(main_mod, "summarize_daily", boom_summarize)
    telegram_calls = []
    monkeypatch.setattr(
        deliver_mod,
        "send_telegram_tldr",
        lambda digest_id_, body_md_, created_at, bot_token, chat_id, thread_id, public_base: (
            telegram_calls.append(digest_id_)
        ),
    )

    cfg = _daily_cfg(state_db_path=db_path)
    ok = run_daily(cfg)

    assert ok is True
    assert telegram_calls == [pending_id]  # deliver_pending retried it despite the skip

    verify_conn = connect(db_path)
    row = verify_conn.execute(
        "SELECT telegram_sent FROM digests WHERE id = ?", (pending_id,)
    ).fetchone()
    verify_conn.close()
    assert row == (1,)


def test_daily_and_weekly_modules_never_reference_arc_key_functions():
    # Structural guardrail, belt-and-suspenders alongside the two behavioral
    # tests below: digest/daily.py and digest/weekly.py must not so much as
    # NAME write_arc_keys/get_arc_keys/get_recent_arc_keys/derive_topics'
    # arc_keys parameter anywhere in their source -- there is nothing here
    # for a future daily/weekly change to accidentally wire up, since the
    # names themselves don't appear.
    daily_source = (Path(__file__).resolve().parent.parent / "digest" / "daily.py").read_text()
    weekly_source = (Path(__file__).resolve().parent.parent / "digest" / "weekly.py").read_text()

    for name in ("write_arc_keys", "get_arc_keys", "get_recent_arc_keys", "arc_keys"):
        assert name not in daily_source, f"digest/daily.py must never reference {name!r}"
        assert name not in weekly_source, f"digest/weekly.py must never reference {name!r}"


def test_run_daily_never_writes_to_arc_keys_table(conn, monkeypatch, tmp_path):
    # Stable-arc-keys guardrail, mirrors the deltas fencing discipline
    # (test_run_daily_prompt_never_ingests_deltas_content): arc keys are a
    # window-level concept only (summarize_daily produces a bare body_md, no
    # arc_keys value at all) -- run_daily must never write a row to
    # `arc_keys`, even though its own daily digest gets a real `## ` section
    # a naive implementation might be tempted to tag.
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    window_id = _window_digest(real_conn, "window one", 3, _recent_created_at(hours_ago=6))
    write_arc_keys(real_conn, window_id, [{"slug": "an-arc", "label": "An arc", "key": "hormuz"}])
    before_count = real_conn.execute("SELECT COUNT(*) FROM arc_keys").fetchone()[0]
    assert before_count == 1
    real_conn.close()

    monkeypatch.setattr(
        main_mod,
        "summarize_daily",
        lambda *a, **k: ("**TL;DR:** the day\n\n## An arc\n\nstuff", _MODEL_RUN),
    )
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = _daily_cfg(state_db_path=db_path)
    ok = run_daily(cfg)

    assert ok is True
    verify_conn = connect(db_path)
    after_count = verify_conn.execute("SELECT COUNT(*) FROM arc_keys").fetchone()[0]
    assert after_count == before_count  # unchanged -- run_daily wrote nothing
    assert get_recent_arc_keys(verify_conn, "2000-01-01T00:00:00+00:00") == [("hormuz", 1)]
    verify_conn.close()


# --- run_daily arc-context primer generation (PLAN.md §11.6, "context mode") ---


def test_generate_arc_context_primers_disabled_returns_zero_without_querying(conn, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not query for qualifying arcs when context_enabled is False")

    monkeypatch.setattr(main_mod, "get_arc_keys_needing_context", boom)

    cfg = replace(_cfg(), context_enabled=False)
    count = main_mod._generate_arc_context_primers(conn, cfg, datetime.now(UTC))

    assert count == 0


def test_generate_arc_context_primers_none_result_stores_nothing_and_key_stays_eligible(
    conn, monkeypatch
):
    # PLAN.md §11.6's accepted behavior for a None generation result (no
    # tombstone -- see _generate_arc_context_primers' own docstring): the
    # key simply stays eligible for reconsideration on a later run.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id_1 = create_digest(
        conn, "**TL;DR:** x\n\n## An arc\n\ntext 1", get_unsummarized_items(conn)
    )
    write_arc_keys(conn, digest_id_1, [{"slug": "an-arc", "label": "An arc", "key": "hormuz"}])
    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})
    digest_id_2 = create_digest(
        conn, "**TL;DR:** x\n\n## An arc\n\ntext 2", get_unsummarized_items(conn)
    )
    write_arc_keys(conn, digest_id_2, [{"slug": "an-arc", "label": "An arc", "key": "hormuz"}])

    monkeypatch.setattr(main_mod, "generate_arc_context", lambda *a, **k: None)

    cfg = replace(_cfg(), context_enabled=True, context_max_per_run=3)
    count = main_mod._generate_arc_context_primers(conn, cfg, datetime.now(UTC))

    assert count == 0
    assert get_arc_context(conn, "hormuz") is None
    assert get_arc_keys_needing_context(conn, "2000-01-01T00:00:00+00:00", 10) == ["hormuz"]


def test_generate_arc_context_primers_success_writes_and_returns_count(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id_1 = create_digest(
        conn, "**TL;DR:** x\n\n## An arc\n\ntext 1", get_unsummarized_items(conn)
    )
    write_arc_keys(conn, digest_id_1, [{"slug": "an-arc", "label": "An arc", "key": "hormuz"}])
    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})
    digest_id_2 = create_digest(
        conn, "**TL;DR:** x\n\n## An arc\n\ntext 2", get_unsummarized_items(conn)
    )
    write_arc_keys(conn, digest_id_2, [{"slug": "an-arc", "label": "An arc", "key": "hormuz"}])

    captured = {}

    def fake_generate(label, model, timeout_seconds, **_kwargs):
        captured.update(label=label, model=model, timeout_seconds=timeout_seconds)
        return "Background about the strait."

    monkeypatch.setattr(main_mod, "generate_arc_context", fake_generate)

    cfg = replace(
        _cfg(),
        context_enabled=True,
        context_max_per_run=3,
        context_model="sonnet",
        context_timeout_seconds=45,
    )
    count = main_mod._generate_arc_context_primers(conn, cfg, datetime.now(UTC))

    assert count == 1
    # The arc's most recent LABEL -- from the digest whose created_at is
    # latest -- not the first-seen occurrence's own text.
    assert captured["label"] == "An arc"
    assert captured["model"] == "sonnet"
    assert captured["timeout_seconds"] == 45
    assert get_arc_context(conn, "hormuz") == "Background about the strait."
    # Generated -- no longer eligible for reconsideration.
    assert get_arc_keys_needing_context(conn, "2000-01-01T00:00:00+00:00", 10) == []


def test_run_daily_context_disabled_never_calls_generate_arc_context(conn, monkeypatch, tmp_path):
    # Prove the false branch really is why no generation happens -- not
    # simply an absence of qualifying data -- by seeding a genuinely
    # qualifying arc (>=2 window-digest appearances) before running with
    # CONTEXT_ENABLED left at its default (False).
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    for i in range(2):
        digest_id = _window_digest(
            real_conn,
            f"**TL;DR:** x\n\n## An arc\n\ntext {i}",
            1,
            _recent_created_at(hours_ago=10 + i),
        )
        write_arc_keys(
            real_conn, digest_id, [{"slug": "an-arc", "label": "An arc", "key": "hormuz"}]
        )
    real_conn.close()

    monkeypatch.setattr(
        main_mod,
        "summarize_daily",
        lambda *a, **k: ("**TL;DR:** the day\n\n## Today\n\nstuff", _MODEL_RUN),
    )
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    def boom(*a, **k):
        raise AssertionError(
            "generate_arc_context must not be called when CONTEXT_ENABLED is false"
        )

    monkeypatch.setattr(main_mod, "generate_arc_context", boom)

    cfg = _daily_cfg(state_db_path=db_path)  # context_enabled defaults False
    ok = run_daily(cfg)

    assert ok is True
    verify_conn = connect(db_path)
    assert verify_conn.execute("SELECT COUNT(*) FROM arc_context").fetchone()[0] == 0
    verify_conn.close()


def test_run_daily_context_enabled_generates_bounded_to_max_per_run(conn, monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    # Three DIFFERENT qualifying arcs (each with >=2 appearances in the
    # trailing 7 days), to prove CONTEXT_MAX_PER_RUN actually bounds the
    # batch rather than processing every qualifying arc in one run.
    for key in ("arc-a", "arc-b", "arc-c"):
        for i in range(2):
            digest_id = _window_digest(
                real_conn,
                f"**TL;DR:** x\n\n## An arc\n\ntext for {key} attempt {i}",
                1,
                _recent_created_at(hours_ago=10 + i),
            )
            write_arc_keys(
                real_conn, digest_id, [{"slug": "an-arc", "label": "An arc", "key": key}]
            )
    real_conn.close()

    monkeypatch.setattr(
        main_mod,
        "summarize_daily",
        lambda *a, **k: ("**TL;DR:** the day\n\n## Today\n\nstuff", _MODEL_RUN),
    )
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    calls = []

    def fake_generate(label, model, timeout_seconds, **_kwargs):
        calls.append((label, model, timeout_seconds))
        return f"background for {label}"

    monkeypatch.setattr(main_mod, "generate_arc_context", fake_generate)

    cfg = _daily_cfg(
        state_db_path=db_path,
        context_enabled=True,
        context_max_per_run=2,
        context_model="sonnet",
        context_timeout_seconds=45,
    )
    ok = run_daily(cfg)

    assert ok is True
    # Bounded by context_max_per_run, not all 3 qualifying arcs.
    assert len(calls) == 2
    for _label, model, timeout_seconds in calls:
        assert model == "sonnet"
        assert timeout_seconds == 45

    verify_conn = connect(db_path)
    stored_keys = {row[0] for row in verify_conn.execute("SELECT key FROM arc_context").fetchall()}
    assert len(stored_keys) == 2
    verify_conn.close()


def test_run_daily_delivery_uses_non_empty_allowed_urls_from_source_window_digest(
    conn, monkeypatch, tmp_path
):
    # Regression test, end to end, for the daily-brief link-provenance bug:
    # before the fix, deliver_channels derived a daily digest's HTML
    # link-provenance allowlist via get_digest_item_urls(conn, digest_id),
    # which is always empty for a "daily" digest (it stamps no items of its
    # own, see state.py's create_digest kind docstring) -- so publish_to_site
    # was handed body_html with EVERY citation anchor already stripped. This
    # drives the real delivery path (run_daily -> deliver_channels ->
    # _deliver_site -> render_body_html) with a daily body_md that cites the
    # source window digest's own item URL, and checks that citation survives
    # into the HTML publish_to_site receives -- which is only possible if
    # deliver_channels built a non-empty allowlist containing that URL.
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    item_url = "https://t.me/c/123/window-item"
    item = Item(
        source="telegram",
        source_id="window-item",
        chat_id="123",
        author="alice",
        text="hello",
        url=item_url,
        fetched_at=_recent_created_at(hours_ago=2),
    )
    commit_new_items(real_conn, [item], {})
    window_digest_id = create_digest(real_conn, "window body", [item])
    real_conn.execute(
        "UPDATE digests SET created_at = ?, email_sent = 1, site_published = 1, "
        "telegram_sent = 1 WHERE id = ?",
        (_recent_created_at(hours_ago=2), window_digest_id),
    )
    real_conn.commit()
    real_conn.close()

    body_md = f"**TL;DR:** the day\n\n## An arc\n\n[cite]({item_url})"
    monkeypatch.setattr(main_mod, "summarize_daily", lambda *a, **k: (body_md, _MODEL_RUN))

    render_calls = []
    real_render_body_html = deliver_mod.render_body_html
    monkeypatch.setattr(
        deliver_mod,
        "render_body_html",
        lambda md, allowed_urls: (
            render_calls.append(set(allowed_urls)) or real_render_body_html(md, allowed_urls)
        ),
    )
    site_calls = []
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: site_calls.append((a, k)))
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = _daily_cfg(state_db_path=db_path)
    ok = run_daily(cfg)

    assert ok is True
    assert len(render_calls) == 1
    assert render_calls[0] == {item_url}  # non-empty, contains the source item's URL

    assert len(site_calls) == 1
    body_html = site_calls[0][0][2]  # publish_to_site's positional body_html arg
    assert f'href="{item_url}"' in body_html


def test_run_daily_translation_enabled_threads_hu_body_to_site(conn, monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    _window_digest(real_conn, "window one", 1, _recent_created_at(hours_ago=2))
    real_conn.close()

    monkeypatch.setattr(
        main_mod,
        "summarize_daily",
        lambda *a, **k: ("**TL;DR:** the day\n\n## An arc\n\nstuff", _MODEL_RUN),
    )
    translate_calls = []

    def fake_translate(
        body_md, allowed_urls, model, timeout_seconds, fallback_model=None, **_kwargs
    ):
        translate_calls.append((body_md, fallback_model))
        return "**TL;DR:** a nap\n\n## Egy szál\n\ndolog", _MODEL_RUN

    monkeypatch.setattr(main_mod, "translate_digest", fake_translate)

    site_calls = []
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: site_calls.append(k))
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = _daily_cfg(state_db_path=db_path, translate_hu_enabled=True)
    ok = run_daily(cfg)

    assert ok is True
    assert len(translate_calls) == 1
    assert site_calls[0]["body_md_hu"] == "**TL;DR:** a nap\n\n## Egy szál\n\ndolog"
    # run_daily must thread cfg.translate_model_fallback through too.
    assert translate_calls[0][1] == cfg.translate_model_fallback


def test_run_daily_summarize_failure_returns_false(conn, monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    _window_digest(real_conn, "window one", 1, _recent_created_at(hours_ago=2))
    real_conn.close()

    def boom(*args, **kwargs):
        raise SummarizeError("claude -p exited 1")

    monkeypatch.setattr(main_mod, "summarize_daily", boom)

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("must not create/archive/deliver a digest on summarize failure")

    monkeypatch.setattr(main_mod, "archive", must_not_be_called)

    cfg = _daily_cfg(state_db_path=db_path)
    ok = run_daily(cfg)

    assert ok is False
    verify_conn = connect(db_path)
    count = verify_conn.execute("SELECT COUNT(*) FROM digests WHERE kind = 'daily'").fetchone()[0]
    assert count == 0
    verify_conn.close()


def test_run_daily_retry_path_picks_daily_thread_from_stored_kind(conn, monkeypatch):
    # A daily digest that was created and got its site publish through, but
    # whose Telegram send failed in a PREVIOUS run, must be retried through
    # the pending pass with its Telegram send going to the DAILY thread --
    # not the default "window" thread -- because get_pending_digests/
    # _digest_meta now expose the row's own stored kind.
    digest_id = create_digest(conn, "**TL;DR:** the day\n\n## An arc\n\nstuff", [], kind="daily")
    conn.execute(
        "UPDATE digests SET site_published = 1, created_at = ? WHERE id = ?",
        (_recent_created_at(), digest_id),
    )
    conn.commit()

    telegram_calls = []
    monkeypatch.setattr(
        deliver_mod,
        "send_telegram_tldr",
        lambda digest_id_, body_md_, created_at, bot_token, chat_id, thread_id, public_base: (
            telegram_calls.append(thread_id)
        ),
    )

    cfg = _daily_cfg()
    ok = deliver_pending(conn, cfg, _fresh_telegram_state())

    assert ok is True
    assert telegram_calls == [555]
    row = conn.execute("SELECT telegram_sent FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (1,)


# --- run_daily verification pass (PLAN.md §11.4, VERIFY_DAILY_ENABLED) ---


def _verify_daily_setup(
    conn, tmp_path, monkeypatch, *, draft_body_md="**TL;DR:** the day\n\n## An arc\n\nstuff"
):
    """Shared fixture setup for the run_daily verification-wiring tests below.

    One window digest, summarize_daily faked to return `draft_body_md`
    unconditionally, and the delivery/archive side effects faked out --
    identical shape to `test_run_daily_happy_path_creates_and_delivers_daily_digest`,
    factored out since every verification test below needs the same
    scaffolding and only differs in how `main_mod.verify_daily` behaves.
    """
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    _window_digest(real_conn, "window one", 3, _recent_created_at(hours_ago=6))
    real_conn.close()

    monkeypatch.setattr(main_mod, "summarize_daily", lambda *a, **k: (draft_body_md, _MODEL_RUN))
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)
    return db_path


def test_run_daily_flag_off_never_calls_verify_daily(conn, monkeypatch, tmp_path):
    db_path = _verify_daily_setup(conn, tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("verify_daily must not be called when VERIFY_DAILY_ENABLED is off")

    monkeypatch.setattr(main_mod, "verify_daily", boom)

    cfg = _daily_cfg(state_db_path=db_path, verify_daily_enabled=False)
    ok = run_daily(cfg)

    assert ok is True
    verify_conn = connect(db_path)
    body_md = verify_conn.execute("SELECT body_md FROM digests WHERE kind = 'daily'").fetchone()[0]
    verify_conn.close()
    # Byte-identical to the pre-§11.4 draft: no banner, no verifier-added
    # "Verification notes" section.
    assert body_md == "**TL;DR:** the day\n\n## An arc\n\nstuff"
    assert "verification unavailable" not in body_md


def test_run_daily_flag_on_success_ships_verified_text_without_banner(conn, monkeypatch, tmp_path):
    db_path = _verify_daily_setup(conn, tmp_path, monkeypatch)

    verified_text = "**TL;DR:** the day\n\n## An arc\n\nstuff\n\n## Verification notes\n\nchecked."
    verify_calls = []

    def fake_verify_daily(draft_body_md, allowed_urls, model, timeout_seconds, effort, max_web_ops):
        verify_calls.append(
            dict(
                draft_body_md=draft_body_md,
                allowed_urls=set(allowed_urls),
                model=model,
                timeout_seconds=timeout_seconds,
                effort=effort,
                max_web_ops=max_web_ops,
            )
        )
        return verified_text, set(allowed_urls) | {"https://verifier-fetched.example/x"}

    monkeypatch.setattr(main_mod, "verify_daily", fake_verify_daily)

    cfg = _daily_cfg(state_db_path=db_path, verify_daily_enabled=True)
    ok = run_daily(cfg)

    assert ok is True
    assert len(verify_calls) == 1
    call = verify_calls[0]
    assert call["draft_body_md"] == "**TL;DR:** the day\n\n## An arc\n\nstuff"
    assert call["model"] == cfg.verify_daily_model
    assert call["timeout_seconds"] == cfg.verify_daily_timeout_seconds
    assert call["effort"] == cfg.verify_daily_effort
    assert call["max_web_ops"] == cfg.verify_daily_max_web_ops

    verify_conn = connect(db_path)
    row = verify_conn.execute("SELECT body_md FROM digests WHERE kind = 'daily'").fetchone()
    verify_conn.close()
    assert row[0] == verified_text
    assert "verification unavailable" not in row[0]


def test_run_daily_flag_on_success_widens_allowlist_at_delivery_time(conn, monkeypatch, tmp_path):
    # Regression guard for the same class of bug
    # test_run_daily_delivery_uses_non_empty_allowed_urls_from_source_window_digest
    # exists for: deliver_channels must receive the WIDENED allowlist for a
    # verified daily brief, or a verifier-only citation would be stripped a
    # second time at render time, in the very run that produced it.
    db_path = _verify_daily_setup(conn, tmp_path, monkeypatch)

    verified_url = "https://verifier-fetched.example/report"
    verified_text = f"**TL;DR:** the day\n\n## An arc\n\nNew fact[¹]({verified_url}).\n"

    def fake_verify_daily(draft_body_md, allowed_urls, model, timeout_seconds, effort, max_web_ops):
        return verified_text, set(allowed_urls) | {verified_url}

    monkeypatch.setattr(main_mod, "verify_daily", fake_verify_daily)

    render_calls = []
    real_render_body_html = deliver_mod.render_body_html
    monkeypatch.setattr(
        deliver_mod,
        "render_body_html",
        lambda md, allowed_urls: (
            render_calls.append(set(allowed_urls)) or real_render_body_html(md, allowed_urls)
        ),
    )
    site_calls = []
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: site_calls.append((a, k)))

    cfg = _daily_cfg(state_db_path=db_path, verify_daily_enabled=True)
    ok = run_daily(cfg)

    assert ok is True
    assert len(render_calls) == 1
    assert verified_url in render_calls[0]
    assert len(site_calls) == 1
    body_html = site_calls[0][0][2]
    assert f'href="{verified_url}"' in body_html


def test_run_daily_flag_on_verification_unavailable_ships_draft_with_banner(
    conn, monkeypatch, tmp_path
):
    db_path = _verify_daily_setup(conn, tmp_path, monkeypatch)

    def boom(*a, **k):
        raise VerificationUnavailable("claude -p (verify) timed out after 600s")

    monkeypatch.setattr(main_mod, "verify_daily", boom)

    translate_calls = []
    monkeypatch.setattr(
        main_mod, "translate_digest", lambda *a, **k: translate_calls.append(a) or None
    )

    cfg = _daily_cfg(state_db_path=db_path, verify_daily_enabled=True, translate_hu_enabled=True)
    ok = run_daily(cfg)

    assert ok is True  # verification unavailability never fails the run
    assert len(translate_calls) == 1  # translate/deliver still run

    verify_conn = connect(db_path)
    body_md = verify_conn.execute("SELECT body_md FROM digests WHERE kind = 'daily'").fetchone()[0]
    verify_conn.close()
    assert body_md.startswith("⚠ verification unavailable this run\n\n")
    assert body_md.endswith("**TL;DR:** the day\n\n## An arc\n\nstuff")


def test_run_daily_flag_on_summarize_error_from_verify_ships_draft_with_banner(
    conn, monkeypatch, tmp_path
):
    # The other of the two documented failure shapes: verify_daily's own
    # validate_output call raised SummarizeError (output that fails the
    # daily contract) rather than run_claude_verify raising
    # VerificationUnavailable -- both must route to the identical soft-fail.
    db_path = _verify_daily_setup(conn, tmp_path, monkeypatch)

    def boom(*a, **k):
        raise SummarizeError("verify output had no real heading")

    monkeypatch.setattr(main_mod, "verify_daily", boom)

    cfg = _daily_cfg(state_db_path=db_path, verify_daily_enabled=True)
    ok = run_daily(cfg)

    assert ok is True
    verify_conn = connect(db_path)
    body_md = verify_conn.execute("SELECT body_md FROM digests WHERE kind = 'daily'").fetchone()[0]
    verify_conn.close()
    assert body_md.startswith("⚠ verification unavailable this run\n\n")


def test_run_daily_flag_on_banner_is_code_prepended_not_model_text(conn, monkeypatch, tmp_path):
    # The banner must come from digest/main.py's own constant, never from
    # anything the (faked) model returned -- the draft here contains no
    # banner-shaped text of its own.
    draft = "**TL;DR:** quiet day\n\n## Nothing much\n\nstuff"
    db_path = _verify_daily_setup(conn, tmp_path, monkeypatch, draft_body_md=draft)
    monkeypatch.setattr(
        main_mod,
        "verify_daily",
        lambda *a, **k: (_ for _ in ()).throw(VerificationUnavailable("boom")),
    )

    cfg = _daily_cfg(state_db_path=db_path, verify_daily_enabled=True)
    run_daily(cfg)

    verify_conn = connect(db_path)
    body_md = verify_conn.execute("SELECT body_md FROM digests WHERE kind = 'daily'").fetchone()[0]
    verify_conn.close()
    assert body_md == main_mod._VERIFY_UNAVAILABLE_BANNER + draft


def test_run_daily_flag_on_translation_uses_widened_allowlist(conn, monkeypatch, tmp_path):
    # A verified brief's Hungarian translation must be checked against the
    # SAME widened allowlist the English verified body was -- not the
    # draft's own narrower set -- or a verifier-only citation the English
    # body kept would get stripped from the Hungarian one alone.
    db_path = _verify_daily_setup(conn, tmp_path, monkeypatch)
    widened_extra = {"https://verifier-fetched.example/x"}

    monkeypatch.setattr(
        main_mod,
        "verify_daily",
        lambda draft_body_md, allowed_urls, *a, **k: (
            "**TL;DR:** the day\n\n## An arc\n\nstuff",
            set(allowed_urls) | widened_extra,
        ),
    )

    translate_calls = []

    def fake_translate(
        body_md, allowed_urls, model, timeout_seconds, fallback_model=None, **_kwargs
    ):
        translate_calls.append(set(allowed_urls))
        return "**TL;DR:** a nap\n\n## Egy szál\n\ndolog", _MODEL_RUN

    monkeypatch.setattr(main_mod, "translate_digest", fake_translate)

    cfg = _daily_cfg(state_db_path=db_path, verify_daily_enabled=True, translate_hu_enabled=True)
    ok = run_daily(cfg)

    assert ok is True
    assert len(translate_calls) == 1
    assert widened_extra <= translate_calls[0]


# --- run_weekly / _deliver_pending (weekly-brief feature) ---


def _weekly_cfg(**overrides) -> Config:
    """_multichannel_cfg() plus a distinct weekly Telegram thread, for run_weekly tests.

    Mirrors `_daily_cfg`'s own isolation rationale -- run_weekly's email
    behavior is identical to run_daily's own (EMAIL_ENABLED gates it exactly
    the same way), so it doesn't need its own coverage here.
    """
    return replace(
        _multichannel_cfg(), email_enabled=False, telegram_weekly_thread_id=141, **overrides
    )


def _daily_digest(conn, body_md: str, item_count: int, created_at: str) -> int:
    """Create a real, fully-delivered DAILY digest with no stamped items but a given item_count.

    A "daily" digest genuinely stamps NO items of its own (see state.py's
    create_digest `kind` docstring), unlike `_window_digest` above -- so
    this passes `item_count` straight to create_digest's own override
    parameter, exactly what run_daily itself does when it creates a real
    daily digest. All three channel flags are marked done (as if the daily
    job already delivered it) so run_weekly's own `_deliver_pending` pass
    finds nothing left to retry for it -- isolating these tests to
    run_weekly's OWN fresh weekly-digest delivery, which is what they're
    checking.
    """
    digest_id = create_digest(conn, body_md, [], kind="daily", item_count=item_count)
    conn.execute(
        "UPDATE digests SET created_at = ?, email_sent = 1, site_published = 1, "
        "telegram_sent = 1 WHERE id = ?",
        (created_at, digest_id),
    )
    conn.commit()
    return digest_id


def test_run_weekly_empty_week_is_a_no_op_returns_true(monkeypatch, tmp_path, caplog):
    def boom(*args, **kwargs):
        raise AssertionError("must not be called when there are no daily briefs to brief")

    cfg = replace(_cfg(), state_db_path=str(tmp_path / "state.db"))
    real_conn = connect(cfg.state_db_path)
    init_db(real_conn)
    real_conn.close()

    monkeypatch.setattr(main_mod, "summarize_weekly", boom)
    monkeypatch.setattr(deliver_mod, "publish_to_site", boom)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", boom)
    monkeypatch.setattr(main_mod, "archive", boom)

    with caplog.at_level("INFO", logger=main_mod.logger.name):
        ok = run_weekly(cfg)

    assert ok is True

    # run_summary must still fire on this early-return, empty-week path, with
    # source_digests: 0 -- Loki's only way to tell "empty week" apart from a
    # weekly run that actually failed further along.
    line = next(r.message for r in caplog.records if r.message.startswith("run_summary "))
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["mode"] == "weekly"
    assert payload["source_digests"] == 0
    assert payload["delivered"] is True
    assert payload["ok"] is True


def test_run_weekly_happy_path_creates_and_delivers_weekly_digest(conn, monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    _daily_digest(real_conn, "daily one", 3, _recent_created_at(hours_ago=48))
    _daily_digest(real_conn, "daily two", 5, _recent_created_at(hours_ago=24))
    # A window digest inside the same 7-day lookback: this is what
    # allowed_urls is actually derived from (see run_weekly's own
    # docstring), NOT the daily rows above -- those stamp no items at all.
    _window_digest(real_conn, "window one", 8, _recent_created_at(hours_ago=12))
    real_conn.close()

    weekly_calls = []

    def fake_summarize_weekly(daily_rows, allowed_urls, model, timeout_seconds, effort, **_kwargs):
        weekly_calls.append(
            dict(
                daily_rows=daily_rows,
                allowed_urls=allowed_urls,
                model=model,
                timeout_seconds=timeout_seconds,
                effort=effort,
            )
        )
        return "**TL;DR:** the week\n\n## A thread\n\nstuff", _MODEL_RUN

    monkeypatch.setattr(main_mod, "summarize_weekly", fake_summarize_weekly)

    site_calls = []
    telegram_calls = []
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: site_calls.append((a, k)))
    monkeypatch.setattr(
        deliver_mod,
        "send_telegram_tldr",
        lambda digest_id_, body_md_, created_at, bot_token, chat_id, thread_id, public_base: (
            telegram_calls.append(thread_id)
        ),
    )
    archived = {}
    monkeypatch.setattr(
        main_mod, "archive", lambda body_md, archive_dir, digest_id: archived.update(id=digest_id)
    )

    cfg = _weekly_cfg(state_db_path=db_path)
    ok = run_weekly(cfg)

    assert ok is True
    assert len(weekly_calls) == 1
    call = weekly_calls[0]
    assert call["model"] == cfg.anthropic_model
    assert call["timeout_seconds"] == cfg.claude_timeout_seconds
    assert call["effort"] == cfg.claude_effort
    # summarize_weekly is given the two DAILY rows, in ascending id order.
    assert [row[3] for row in call["daily_rows"]] == ["daily one", "daily two"]
    # allowed_urls is the union of the WINDOW digest's own stamped item
    # URLs -- 8 items, matching that window digest's own item_count -- NOT
    # anything derived from the (item-less) daily rows above.
    assert len(call["allowed_urls"]) == 8

    # The weekly digest's own thread, not the window or daily thread.
    assert telegram_calls == [141]
    assert len(site_calls) == 1
    assert archived["id"] is not None

    verify_conn = connect(db_path)
    row = verify_conn.execute(
        "SELECT kind, item_count FROM digests WHERE id = ?", (archived["id"],)
    ).fetchone()
    assert row == ("weekly", 8)  # SUM of the two source DAILY digests' item_counts (3 + 5)
    verify_conn.close()


def test_run_weekly_never_writes_to_arc_keys_table(conn, monkeypatch, tmp_path):
    # Stable-arc-keys guardrail, one editorial rung up from
    # test_run_daily_never_writes_to_arc_keys_table: run_weekly must never
    # write a row to `arc_keys` either -- summarize_weekly produces a bare
    # body_md, no arc_keys value at all, and write_arc_keys is called from
    # exactly one place in this codebase (digest/main.py's `_deliver`).
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    _daily_digest(real_conn, "daily one", 3, _recent_created_at(hours_ago=48))
    window_id = _window_digest(real_conn, "window one", 8, _recent_created_at(hours_ago=12))
    write_arc_keys(real_conn, window_id, [{"slug": "an-arc", "label": "An arc", "key": "hormuz"}])
    before_count = real_conn.execute("SELECT COUNT(*) FROM arc_keys").fetchone()[0]
    assert before_count == 1
    real_conn.close()

    monkeypatch.setattr(
        main_mod,
        "summarize_weekly",
        lambda *a, **k: ("**TL;DR:** the week\n\n## A thread\n\nstuff", _MODEL_RUN),
    )
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = _weekly_cfg(state_db_path=db_path)
    ok = run_weekly(cfg)

    assert ok is True
    verify_conn = connect(db_path)
    after_count = verify_conn.execute("SELECT COUNT(*) FROM arc_keys").fetchone()[0]
    assert after_count == before_count  # unchanged -- run_weekly wrote nothing
    assert get_recent_arc_keys(verify_conn, "2000-01-01T00:00:00+00:00") == [("hormuz", 1)]
    verify_conn.close()


def test_run_weekly_delivery_uses_allowed_urls_from_window_digest_not_daily(
    conn, monkeypatch, tmp_path
):
    # Regression test, end to end, for the weekly-brief link-provenance
    # derivation: allowed_urls must come from the week's WINDOW digests, not
    # from the source DAILY rows (which stamp no items of their own -- see
    # state.py's create_digest `kind` docstring, so their own
    # get_digest_item_urls is always empty). This proves it two ways: (a) a
    # real window item's URL survives into the delivered HTML, and (b) an
    # item adversarially stamped directly onto a DAILY digest (bypassing the
    # normal create_digest(items=[]) contract, mirroring
    # tests/test_state.py's own get_weekly_allowed_urls exclusion test) does
    # NOT survive -- proving the allowlist is scoped to `kind = 'window'`,
    # not "any digest in the lookback window".
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)

    window_item_url = "https://t.me/c/123/window-item"
    window_item = Item(
        source="telegram",
        source_id="window-item",
        chat_id="123",
        author="alice",
        text="hello",
        url=window_item_url,
        fetched_at=_recent_created_at(hours_ago=12),
    )
    commit_new_items(real_conn, [window_item], {})
    window_digest_id = create_digest(real_conn, "window body", [window_item])
    real_conn.execute(
        "UPDATE digests SET created_at = ?, email_sent = 1, site_published = 1, "
        "telegram_sent = 1 WHERE id = ?",
        (_recent_created_at(hours_ago=12), window_digest_id),
    )
    real_conn.commit()

    # Adversarially stamp an item directly onto a DAILY digest -- nothing at
    # the SQL level stops this, and it is exactly what proves the `kind =
    # 'window'` filter (not merely "any digest in range") is what keeps this
    # URL out of the allowlist.
    daily_item_url = "https://t.me/c/123/daily-item"
    daily_item = Item(
        source="telegram",
        source_id="daily-item",
        chat_id="123",
        author="alice",
        text="hello",
        url=daily_item_url,
        fetched_at=_recent_created_at(hours_ago=24),
    )
    commit_new_items(real_conn, [daily_item], {})
    daily_digest_id = create_digest(real_conn, "daily body", [daily_item], kind="daily")
    real_conn.execute(
        "UPDATE digests SET created_at = ?, email_sent = 1, site_published = 1, "
        "telegram_sent = 1 WHERE id = ?",
        (_recent_created_at(hours_ago=24), daily_digest_id),
    )
    real_conn.commit()
    real_conn.close()

    body_md = f"**TL;DR:** the week\n\n## A thread\n\n[cite]({window_item_url})"
    monkeypatch.setattr(main_mod, "summarize_weekly", lambda *a, **k: (body_md, _MODEL_RUN))

    render_calls = []
    real_render_body_html = deliver_mod.render_body_html
    monkeypatch.setattr(
        deliver_mod,
        "render_body_html",
        lambda md, allowed_urls: (
            render_calls.append(set(allowed_urls)) or real_render_body_html(md, allowed_urls)
        ),
    )
    site_calls = []
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: site_calls.append((a, k)))
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = _weekly_cfg(state_db_path=db_path)
    ok = run_weekly(cfg)

    assert ok is True
    assert len(render_calls) == 1
    # Contains the WINDOW item's URL, and ONLY that URL -- the DAILY digest's
    # own (adversarially stamped) item URL is excluded.
    assert render_calls[0] == {window_item_url}

    assert len(site_calls) == 1
    body_html = site_calls[0][0][2]  # publish_to_site's positional body_html arg
    assert f'href="{window_item_url}"' in body_html


def test_run_weekly_translation_enabled_threads_hu_body_to_site(conn, monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    _daily_digest(real_conn, "daily one", 1, _recent_created_at(hours_ago=24))
    _window_digest(real_conn, "window one", 1, _recent_created_at(hours_ago=12))
    real_conn.close()

    monkeypatch.setattr(
        main_mod,
        "summarize_weekly",
        lambda *a, **k: ("**TL;DR:** the week\n\n## A thread\n\nstuff", _MODEL_RUN),
    )
    translate_calls = []

    def fake_translate(
        body_md, allowed_urls, model, timeout_seconds, fallback_model=None, **_kwargs
    ):
        translate_calls.append((body_md, fallback_model))
        return "**TL;DR:** a het\n\n## Egy szal\n\ndolog", _MODEL_RUN

    monkeypatch.setattr(main_mod, "translate_digest", fake_translate)

    site_calls = []
    monkeypatch.setattr(deliver_mod, "publish_to_site", lambda *a, **k: site_calls.append(k))
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    cfg = _weekly_cfg(state_db_path=db_path, translate_hu_enabled=True)
    ok = run_weekly(cfg)

    assert ok is True
    assert len(translate_calls) == 1
    assert site_calls[0]["body_md_hu"] == "**TL;DR:** a het\n\n## Egy szal\n\ndolog"
    # run_weekly must thread cfg.translate_model_fallback through too.
    assert translate_calls[0][1] == cfg.translate_model_fallback


def test_run_weekly_summarize_failure_returns_false(conn, monkeypatch, tmp_path):
    db_path = str(tmp_path / "state.db")
    real_conn = connect(db_path)
    init_db(real_conn)
    _daily_digest(real_conn, "daily one", 1, _recent_created_at(hours_ago=24))
    real_conn.close()

    def boom(*args, **kwargs):
        raise SummarizeError("claude -p exited 1")

    monkeypatch.setattr(main_mod, "summarize_weekly", boom)

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("must not create/archive/deliver a digest on summarize failure")

    monkeypatch.setattr(main_mod, "archive", must_not_be_called)

    cfg = _weekly_cfg(state_db_path=db_path)
    ok = run_weekly(cfg)

    assert ok is False
    verify_conn = connect(db_path)
    count = verify_conn.execute("SELECT COUNT(*) FROM digests WHERE kind = 'weekly'").fetchone()[0]
    assert count == 0
    verify_conn.close()


def test_run_weekly_retry_path_picks_weekly_thread_from_stored_kind(conn, monkeypatch):
    # A weekly digest that was created and got its site publish through, but
    # whose Telegram send failed in a PREVIOUS run, must be retried through
    # the pending pass with its Telegram send going to the WEEKLY thread --
    # not the default "window" thread -- because get_pending_digests/
    # digest_meta expose the row's own stored kind.
    digest_id = create_digest(
        conn, "**TL;DR:** the week\n\n## A thread\n\nstuff", [], kind="weekly"
    )
    conn.execute(
        "UPDATE digests SET site_published = 1, created_at = ? WHERE id = ?",
        (_recent_created_at(), digest_id),
    )
    conn.commit()

    telegram_calls = []
    monkeypatch.setattr(
        deliver_mod,
        "send_telegram_tldr",
        lambda digest_id_, body_md_, created_at, bot_token, chat_id, thread_id, public_base: (
            telegram_calls.append(thread_id)
        ),
    )

    cfg = _weekly_cfg()
    ok = deliver_pending(conn, cfg, _fresh_telegram_state())

    assert ok is True
    assert telegram_calls == [141]
    row = conn.execute("SELECT telegram_sent FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row == (1,)


# --- hidden delivery channels (hide:<channel> window runs) ---


def test_parse_hidden_channels_accepts_a_single_channel():
    assert main_mod._parse_hidden_channels(["hide:telegram"]) == frozenset({"telegram"})


def test_parse_hidden_channels_accepts_a_comma_list_and_normalizes_case():
    assert main_mod._parse_hidden_channels(["hide:Telegram, SITE"]) == frozenset(
        {"telegram", "site"}
    )


def test_parse_hidden_channels_is_empty_without_the_argument():
    # The default window run must be completely unaffected.
    assert main_mod._parse_hidden_channels([]) == frozenset()
    assert main_mod._parse_hidden_channels(["--force"]) == frozenset()


def test_parse_hidden_channels_rejects_an_unknown_channel():
    # A typo must fail loudly at startup, not silently deliver everywhere:
    # the whole point of the flag is that something is NOT shown.
    with pytest.raises(ValueError, match="unknown delivery channel 'telegran'"):
        main_mod._parse_hidden_channels(["hide:telegran"])


def test_parse_hidden_channels_rejects_hiding_the_site_without_telegram():
    # deliver_channels only defers Telegram while the site publish is NOT
    # done; a suppressed site reads as done there, so this combination would
    # send a real ping linking to a page that was never published.
    with pytest.raises(ValueError, match="hide:site requires hide:telegram"):
        main_mod._parse_hidden_channels(["hide:site"])


def test_deliver_channels_hidden_telegram_is_marked_done_and_never_sent(conn, monkeypatch):
    # The load-bearing behaviour: a hidden channel must be STAMPED, not just
    # skipped. get_pending_digests treats an enabled channel with a 0 flag as
    # an incomplete delivery, so a merely-skipped channel would be picked up
    # by the next run's deliver_pending and shown then -- turning "hidden"
    # into "delivered late".
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "## Heading\n...body...", get_unsummarized_items(conn))

    sent: list[int] = []
    monkeypatch.setattr(deliver_mod, "send_telegram_tldr", lambda *a, **k: sent.append(digest_id))
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)

    cfg = replace(
        _cfg(), telegram_notify_bot_token="tok", telegram_notify_chat_id="-100", email_enabled=True
    )
    ok = deliver_channels(
        conn,
        cfg,
        digest_id,
        "## Heading\n...body...",
        1,
        "2026-07-29T10:00:00+00:00",
        dict(_NO_CHANNELS_DONE),
        _fresh_telegram_state(),
        hidden=frozenset({"telegram"}),
    )

    assert ok is True
    assert sent == []  # never transmitted...
    # ...but recorded as resolved, so no later run resurrects it.
    row = conn.execute("SELECT telegram_sent FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row[0] == 1
    assert get_pending_digests(conn, True, False, True) == []


def test_deliver_channels_hidden_channel_does_not_suppress_the_others(conn, monkeypatch):
    # Hiding one channel must leave every other enabled channel delivering
    # normally -- this is a per-channel suppression, not a quiet mode.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "## Heading\n...body...", get_unsummarized_items(conn))

    emailed: list[str] = []
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: emailed.append("sent"))
    monkeypatch.setattr(
        deliver_mod, "send_telegram_tldr", lambda *a, **k: pytest.fail("telegram was hidden")
    )

    cfg = replace(
        _cfg(), telegram_notify_bot_token="tok", telegram_notify_chat_id="-100", email_enabled=True
    )
    ok = deliver_channels(
        conn,
        cfg,
        digest_id,
        "## Heading\n...body...",
        1,
        "2026-07-29T10:00:00+00:00",
        dict(_NO_CHANNELS_DONE),
        _fresh_telegram_state(),
        hidden=frozenset({"telegram"}),
    )

    assert ok is True
    assert emailed == ["sent"]


# --- OpenRouter fallback chain wiring (digest/main.py's _fallback_legs) ---


def test_deliver_summarize_receives_fallback_legs_when_openrouter_configured(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    captured = {}

    def fake_summarize(*args, **kwargs):
        captured["fallbacks"] = kwargs.get("fallbacks")
        return "## Needs attention\n...", [], [], _MODEL_RUN

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)

    cfg = replace(_cfg(), openrouter_api_key="sk-or-test-key")
    ok = _deliver(conn, cfg, [])

    assert ok is True
    assert captured["fallbacks"] != ()
    assert all(leg.model in cfg.fallback_models for leg in captured["fallbacks"])


def test_deliver_summarize_receives_empty_fallbacks_when_openrouter_unconfigured(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    captured = {}

    def fake_summarize(*args, **kwargs):
        captured["fallbacks"] = kwargs.get("fallbacks")
        return "## Needs attention\n...", [], [], _MODEL_RUN

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)

    cfg = _cfg()  # openrouter_api_key defaults to None -- unconfigured

    ok = _deliver(conn, cfg, [])

    assert ok is True
    assert captured["fallbacks"] == ()


def test_deliver_translate_digest_receives_fallback_legs_when_openrouter_configured(
    conn, monkeypatch
):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    monkeypatch.setattr(
        main_mod, "summarize", lambda *a, **k: ("**TL;DR:** hi\n\n## S\n\nx", [], [], _MODEL_RUN)
    )
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    captured = {}

    def fake_translate(*args, **kwargs):
        captured["fallbacks"] = kwargs.get("fallbacks")
        return "**TL;DR:** szia\n\n## Sz\n\ny", _MODEL_RUN

    monkeypatch.setattr(main_mod, "translate_digest", fake_translate)

    cfg = replace(_cfg(), translate_hu_enabled=True, openrouter_api_key="sk-or-test-key")
    ok = _deliver(conn, cfg, [])

    assert ok is True
    assert captured["fallbacks"] != ()
    assert all(leg.model in cfg.fallback_light_models for leg in captured["fallbacks"])


def test_deliver_translate_digest_receives_empty_fallbacks_when_openrouter_unconfigured(
    conn, monkeypatch
):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    monkeypatch.setattr(
        main_mod, "summarize", lambda *a, **k: ("**TL;DR:** hi\n\n## S\n\nx", [], [], _MODEL_RUN)
    )
    monkeypatch.setattr(deliver_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    captured = {}

    def fake_translate(*args, **kwargs):
        captured["fallbacks"] = kwargs.get("fallbacks")
        return "**TL;DR:** szia\n\n## Sz\n\ny", _MODEL_RUN

    monkeypatch.setattr(main_mod, "translate_digest", fake_translate)

    cfg = replace(_cfg(), translate_hu_enabled=True)  # openrouter_api_key unset
    ok = _deliver(conn, cfg, [])

    assert ok is True
    assert captured["fallbacks"] == ()
