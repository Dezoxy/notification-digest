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
        # Bounded, but generously: measured post bodies run to ~37k and the
        # median is ~19k, so a tight cap here would silently discard most of
        # a typical post before the model read a word.
        item = Item(**{**POST.__dict__, "text": "Cím\n\n" + "x" * 200_000})
        prompt = patreon_kind.build_prompt(item)
        assert len(prompt) < patreon_kind._MAX_PROMPT_TEXT_CHARS + 5_000

    def test_a_full_length_real_post_is_not_truncated(self):
        # The largest real post observed was ~37k characters.
        body = "y" * 37_000
        item = Item(**{**POST.__dict__, "text": f"Cím\n\n{body}"})
        assert body in patreon_kind.build_prompt(item)


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

    def test_long_output_is_not_truncated_here(self):
        # Truncating at this layer would discard exactly the material that
        # split_for_telegram exists to preserve.
        body = "## Cím\n\n" + ("nagyon hosszú bekezdés. " * 1000)
        assert len(publish.render_post_text(body)) > publish._TELEGRAM_TEXT_LIMIT

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


class TestSplitForTelegram:
    def test_short_text_is_returned_unchanged_as_one_part(self):
        # The common case at the prompt's 350-600 word budget: the
        # single-send path, and its flood protections, stay the normal one.
        assert publish.split_for_telegram("rövid") == ["rövid"]

    def test_every_part_fits_telegram_limit(self):
        text = "\n\n".join(f"bekezdés {i} " + "x" * 500 for i in range(30))
        parts = publish.split_for_telegram(text)
        assert len(parts) > 1
        assert all(len(p) <= publish._TELEGRAM_TEXT_LIMIT for p in parts)

    def test_nothing_is_lost_across_the_split(self):
        text = "\n\n".join(f"pont-{i} " + "y" * 400 for i in range(40))
        parts = publish.split_for_telegram(text)
        rejoined = "".join(parts).replace("\n", "")
        assert all(f"pont-{i}" in rejoined for i in range(40))

    def test_splits_on_paragraph_boundaries_when_it_can(self):
        # A cut mid-bullet reads as data loss even though the next part
        # continues it.
        text = "\n\n".join("z" * 1000 for _ in range(10))
        assert all(not p.startswith("\n") for p in publish.split_for_telegram(text))

    def test_a_single_oversized_paragraph_is_hard_cut_not_dropped(self):
        parts = publish.split_for_telegram("q" * 10_000)
        assert len(parts) >= 3
        assert sum(len(p) for p in parts) == 10_000

    def test_line_boundaries_are_tried_before_a_hard_cut(self):
        text = "\n".join("sor " + "w" * 300 for _ in range(30))
        parts = publish.split_for_telegram(text)
        assert all(len(p) <= publish._TELEGRAM_TEXT_LIMIT for p in parts)
        assert parts[0].count("sor") > 1


class TestSendTelegramPostSplitting:
    @staticmethod
    def _capture(monkeypatch, fail_on=None):
        sent = []

        def fake(payload, token, timeout):
            if fail_on is not None and len(sent) == fail_on:
                raise publish.TelegramSendError("boom")
            sent.append(payload)
            return 1000 + len(sent)

        monkeypatch.setattr(publish, "_send_message", fake)
        return sent

    @staticmethod
    def _long_body():
        return "## Cím\n\n" + "\n\n".join(f"- pont {i} " + "x" * 400 for i in range(20))

    def test_a_long_post_becomes_a_reply_chain(self, monkeypatch):
        sent = self._capture(monkeypatch)
        publish.send_telegram_post(
            self._long_body(), POST.fetched_at, POST.url, None, "token", "-100123", 317
        )
        assert len(sent) > 1
        # Each part after the first threads onto the one before it.
        assert "reply_to_message_id" not in sent[0]
        assert sent[1]["reply_to_message_id"] == 1001

    def test_buttons_ride_on_the_last_part_only(self, monkeypatch):
        sent = self._capture(monkeypatch)
        publish.send_telegram_post(
            self._long_body(), POST.fetched_at, POST.url, POST.embed_url, "token", "-100123", 317
        )
        assert all("reply_markup" not in p for p in sent[:-1])
        assert len(sent[-1]["reply_markup"]["inline_keyboard"]) == 2

    def test_parts_are_numbered(self, monkeypatch):
        sent = self._capture(monkeypatch)
        publish.send_telegram_post(
            self._long_body(), POST.fetched_at, POST.url, None, "token", "-100123", 317
        )
        assert f"(1/{len(sent)})" in sent[0]["text"]

    def test_a_single_part_message_is_not_numbered(self, monkeypatch):
        sent = self._capture(monkeypatch)
        publish.send_telegram_post(
            GOOD_OUTPUT, POST.fetched_at, POST.url, None, "token", "-100123", 317
        )
        assert "(1/1)" not in sent[0]["text"]

    def test_failing_on_the_first_part_raises_an_ordinary_error(self, monkeypatch):
        # Nothing landed, so the caller can retry cleanly with no duplicates.
        self._capture(monkeypatch, fail_on=0)
        with pytest.raises(publish.TelegramSendError) as exc:
            publish.send_telegram_post(
                self._long_body(), POST.fetched_at, POST.url, None, "token", "-100123", 317
            )
        assert not isinstance(exc.value, publish.TelegramPartialSend)

    def test_failing_on_a_later_part_raises_partial(self, monkeypatch):
        # Part 1 is already in the topic; a retry would re-send it.
        self._capture(monkeypatch, fail_on=1)
        with pytest.raises(publish.TelegramPartialSend) as exc:
            publish.send_telegram_post(
                self._long_body(), POST.fetched_at, POST.url, None, "token", "-100123", 317
            )
        assert exc.value.parts_sent == 1

    def test_a_mid_chain_429_keeps_its_status_for_the_breaker(self, monkeypatch):
        # The per-run circuit breaker keys on exc.status == 429. A partial
        # send that swallowed the status would let every later post in the
        # same run keep firing into an already-rate-limited API -- quietly
        # bypassing the flood-incident guard for multi-part posts only.
        sent = []

        def fake(payload, token, timeout):
            if len(sent) == 1:
                raise publish.TelegramSendError("rate limited", status=429)
            sent.append(payload)
            return 1000 + len(sent)

        monkeypatch.setattr(publish, "_send_message", fake)
        with pytest.raises(publish.TelegramPartialSend) as exc:
            publish.send_telegram_post(
                self._long_body(), POST.fetched_at, POST.url, None, "token", "-100123", 317
            )
        assert exc.value.status == 429
