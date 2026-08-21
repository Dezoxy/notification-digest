"""Tests for the Patreon kind: prompt construction and Telegram rendering.

No Claude CLI and no network. `run_claude` is mocked at the boundary
digest/patreon.py imports it from, and `_send_message` at the boundary
digest/publish.py posts through -- mirroring how CLAUDE.md requires the
collector and emailer boundaries to be mocked.
"""

from __future__ import annotations

import pytest

from digest import patreon as patreon_kind
from digest import publish
from digest.state import Item
from digest.summarize import SummarizeError

POST = Item(
    source="patreon",
    source_id="167028043",
    chat_id=None,
    chat_title="Patreon",
    author=None,
    text=(
        "Az elkerülhető nagy hibák 1. rész\n\n"
        "Kedves KriptoVadász VIP Közösség,\n\nEbben az anyagban..."
    ),
    url="https://www.patreon.com/kriptovadasz/posts/az-elkerulheto-1-167028043",
    embed_url="https://youtu.be/V2F83U2r22A",
    fetched_at="2026-08-21T16:00:00+00:00",
)

GOOD_OUTPUT = """## Az elkerülhető nagy hibák 1. rész

**TL;DR:** A szerző a leggyakoribb befektetői hibákat veszi sorra.

- A veszteség realizálásának halogatása a leggyakoribb hiba.
- A support szint alatti vásárlás ritkán térül meg.
"""


class TestBuildPrompt:
    def test_substitutes_both_placeholders(self):
        prompt = patreon_kind.build_prompt(POST)
        assert "{{POST_TITLE}}" not in prompt
        assert "{{POST_TEXT}}" not in prompt

    def test_title_is_passed_as_its_own_field(self):
        # The prompt asks for the post's real title as the ## heading; giving
        # it as a labelled field is what makes "don't invent one" checkable.
        assert "Cím: Az elkerülhető nagy hibák 1. rész" in patreon_kind.build_prompt(POST)

    def test_body_is_included(self):
        assert "Kedves KriptoVadász VIP Közösség" in patreon_kind.build_prompt(POST)

    def test_title_only_post_falls_back_to_the_title_as_body(self):
        item = Item(**{**POST.__dict__, "text": "Csak egy cím"})
        prompt = patreon_kind.build_prompt(item)
        assert "Csak egy cím" in prompt

    def test_oversized_body_is_bounded(self):
        item = Item(**{**POST.__dict__, "text": "Cím\n\n" + "x" * 50_000})
        assert len(patreon_kind.build_prompt(item)) < 20_000


class TestSummarizePost:
    def test_returns_the_model_output(self, monkeypatch):
        monkeypatch.setattr(patreon_kind, "run_claude", lambda *a, **k: GOOD_OUTPUT)
        assert patreon_kind.summarize_post(POST, "claude-sonnet-5", 300) == GOOD_OUTPUT

    def test_output_without_a_heading_raises(self, monkeypatch):
        # A refusal or stray apology must not become a digest row: with no
        # row, the item keeps digest_id NULL and the next run retries.
        monkeypatch.setattr(patreon_kind, "run_claude", lambda *a, **k: "Sajnálom, nem tudok.")
        with pytest.raises(SummarizeError):
            patreon_kind.summarize_post(POST, "claude-sonnet-5", 300)

    def test_runs_at_the_fixed_patreon_effort(self, monkeypatch):
        seen = {}

        def fake(prompt, model, timeout, effort):
            seen["effort"] = effort
            return GOOD_OUTPUT

        monkeypatch.setattr(patreon_kind, "run_claude", fake)
        patreon_kind.summarize_post(POST, "claude-sonnet-5", 300)
        assert seen["effort"] == patreon_kind._PATREON_EFFORT


class TestRenderPostText:
    def test_heading_loses_its_hashes(self):
        out = publish.render_post_text(GOOD_OUTPUT)
        assert out.startswith("Az elkerülhető nagy hibák 1. rész")
        assert "#" not in out

    def test_tldr_marker_survives_without_asterisks(self):
        out = publish.render_post_text(GOOD_OUTPUT)
        assert "TL;DR:" in out
        assert "**" not in out

    def test_the_space_after_the_tldr_marker_is_preserved(self):
        # The marker regex consumes the whitespace that followed it; a live
        # render produced "TL;DR:A Trump-beszéd" before this was fixed.
        out = publish.render_post_text(GOOD_OUTPUT)
        assert "TL;DR: A szerző" in out
        assert "TL;DR:A" not in out

    def test_bullets_become_dots(self):
        assert "• A veszteség" in publish.render_post_text(GOOD_OUTPUT)

    def test_a_stray_markdown_link_collapses_to_its_text(self):
        # The prompt forbids links, but a model that emits one anyway must
        # not leak a bare URL into a message whose links are buttons.
        body = "## Cím\n\nLásd [a Fed közleményét](https://federalreserve.gov/x)."
        out = publish.render_post_text(body)
        assert "https://federalreserve.gov" not in out
        assert "a Fed közleményét" in out

    def test_output_is_capped_for_telegram(self):
        body = "## Cím\n\n" + ("nagyon hosszú bekezdés. " * 1000)
        assert len(publish.render_post_text(body)) <= publish._TELEGRAM_TEXT_LIMIT

    def test_hungarian_characters_survive(self):
        assert "elkerülhető" in publish.render_post_text(GOOD_OUTPUT)


class TestSendTelegramPost:
    @staticmethod
    def _capture(monkeypatch):
        sent = {}
        monkeypatch.setattr(
            publish, "_send_message", lambda payload, token, timeout: sent.update(payload)
        )
        return sent

    def test_post_button_is_always_present(self, monkeypatch):
        sent = self._capture(monkeypatch)
        publish.send_telegram_post(
            GOOD_OUTPUT, POST.fetched_at, POST.url, None, "token", "-100123", 317
        )
        rows = sent["reply_markup"]["inline_keyboard"]
        assert len(rows) == 1
        assert rows[0][0]["url"] == POST.url

    def test_video_button_is_added_when_the_post_embeds_one(self, monkeypatch):
        sent = self._capture(monkeypatch)
        publish.send_telegram_post(
            GOOD_OUTPUT, POST.fetched_at, POST.url, POST.embed_url, "token", "-100123", 317
        )
        rows = sent["reply_markup"]["inline_keyboard"]
        assert len(rows) == 2
        assert rows[1][0]["url"] == "https://youtu.be/V2F83U2r22A"

    def test_thread_id_is_sent_when_set(self, monkeypatch):
        sent = self._capture(monkeypatch)
        publish.send_telegram_post(
            GOOD_OUTPUT, POST.fetched_at, POST.url, None, "token", "-100123", 317
        )
        assert sent["message_thread_id"] == 317

    def test_thread_id_zero_is_omitted_entirely(self, monkeypatch):
        # Telegram treats an explicit 0 as an invalid thread id, not "no
        # thread" -- the key must be absent, not zero.
        sent = self._capture(monkeypatch)
        publish.send_telegram_post(
            GOOD_OUTPUT, POST.fetched_at, POST.url, None, "token", "-100123", 0
        )
        assert "message_thread_id" not in sent

    def test_no_parse_mode_is_ever_set(self, monkeypatch):
        # A single unescaped character would reject the whole message.
        sent = self._capture(monkeypatch)
        publish.send_telegram_post(
            GOOD_OUTPUT, POST.fetched_at, POST.url, None, "token", "-100123", 317
        )
        assert "parse_mode" not in sent

    def test_link_previews_are_disabled(self, monkeypatch):
        sent = self._capture(monkeypatch)
        publish.send_telegram_post(
            GOOD_OUTPUT, POST.fetched_at, POST.url, None, "token", "-100123", 317
        )
        assert sent["disable_web_page_preview"] is True

    def test_long_button_label_is_trimmed(self):
        button = publish._button("x" * 100, "https://example.com")
        assert len(button["text"]) <= publish._BUTTON_TEXT_LIMIT
        assert button["text"].endswith("…")
