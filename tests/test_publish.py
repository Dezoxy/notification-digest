import json
import urllib.error

import pytest

import digest.publish as publish_mod
from digest.publish import (
    TelegramSendError,
    count_sections,
    extract_tldr,
    has_needs_attention,
    publish_to_site,
    send_telegram_tldr,
)

# --- extract_tldr ---


def test_extract_tldr_normal_case_strips_marker_and_links():
    body_md = (
        "**TL;DR:** Big thing happened [¹](https://t.me/c/1/1) and it matters.\n\n"
        "## Worth knowing\n\nmore detail here.\n"
    )

    assert extract_tldr(body_md) == "Big thing happened and it matters."


def test_extract_tldr_banner_prefixed_still_finds_tldr():
    body_md = (
        "⚠ telegram collection failed this run\n\n"
        "**TL;DR:** Everything is fine.\n\n"
        "## Worth knowing\n\nmore.\n"
    )

    assert extract_tldr(body_md) == "Everything is fine."


def test_extract_tldr_multiple_banner_lines_are_all_skipped():
    body_md = (
        "⚠ telegram collection failed this run\n"
        "⚠ x collection failed this run\n\n"
        "**TL;DR:** All good.\n\n## Section\n\nmore.\n"
    )

    assert extract_tldr(body_md) == "All good."


def test_extract_tldr_alternate_colon_placement_is_handled():
    body_md = "**TL;DR**: Alternate colon placement.\n\n## Section\n\nmore.\n"

    assert extract_tldr(body_md) == "Alternate colon placement."


def test_extract_tldr_drops_superscript_citation_chips():
    body_md = "**TL;DR:** Thing happened[¹](https://a) and also[²³](https://b).\n\n## S\n\nx\n"

    result = extract_tldr(body_md)

    assert "¹" not in result
    assert "²" not in result
    assert "³" not in result
    assert "happened" in result
    assert "also" in result


def test_extract_tldr_strips_bold_emphasis_inside_the_tldr_sentence():
    # Production finding (live site index, digest #54): the model can put
    # bold markdown INSIDE the TL;DR paragraph itself, not just as the
    # `**TL;DR:**` marker -- that inner `**...**` must not reach the site
    # excerpt or Telegram message as literal asterisks.
    body_md = (
        "**TL;DR:** Revenue could hit **$100B ARR by year end**, analysts say.\n\n## S\n\nx\n"
    )

    result = extract_tldr(body_md)

    assert "*" not in result
    assert result == "Revenue could hit $100B ARR by year end, analysts say."


def test_extract_tldr_preserves_underscore_handles():
    # A lone `_` inside a Telegram/X handle or snake_case identifier must
    # survive untouched -- only the bold `__...__` form and single `*...*`
    # emphasis are stripped, never a single underscore.
    body_md = "**TL;DR:** Big update from @user_name on the platform.\n\n## S\n\nx\n"

    assert extract_tldr(body_md) == "Big update from @user_name on the platform."


def test_extract_tldr_strips_single_asterisk_emphasis():
    body_md = "**TL;DR:** The *actual* number surprised everyone.\n\n## S\n\nx\n"

    result = extract_tldr(body_md)

    assert "*" not in result
    assert result == "The actual number surprised everyone."


def test_extract_tldr_needs_attention_section_before_tldr_is_skipped():
    # The prompt contract places "## Needs attention" ABOVE the TL;DR
    # paragraph -- extract_tldr must search past it, not stop at the first
    # paragraph found.
    body_md = (
        "## Needs attention\n\nSomething urgent needs a reply.\n\n"
        "**TL;DR:** The actual summary.\n\n## Worth knowing\n\nmore.\n"
    )

    assert extract_tldr(body_md) == "The actual summary."


def test_extract_tldr_missing_tldr_falls_back_to_first_paragraph_capped_at_300(monkeypatch):
    long_para = "x" * 400
    body_md = f"## Worth knowing\n\n{long_para}\n\n## Other\n\nmore.\n"

    result = extract_tldr(body_md)

    assert result == "x" * 300


def test_extract_tldr_fallback_skips_headings_and_banners():
    body_md = (
        "⚠ telegram collection failed this run\n\n"
        "## Worth knowing\n\nThe real first paragraph.\n\n## Other\n\nmore.\n"
    )

    assert extract_tldr(body_md) == "The real first paragraph."


def test_extract_tldr_empty_body_returns_empty_string():
    assert extract_tldr("") == ""


def test_extract_tldr_never_raises_on_pathological_input():
    assert extract_tldr("#" * 10000) == ""
    assert extract_tldr("\n\n\n") == ""


# --- count_sections / has_needs_attention ---


def test_count_sections_counts_real_h2_headings_excluding_needs_attention():
    body_md = (
        "**TL;DR:** hi\n\n"
        "## Needs attention\n\nurgent\n\n"
        "## Story one\n\ntext\n\n"
        "## Story two\n\ntext\n"
    )

    assert count_sections(body_md) == 2
    assert has_needs_attention(body_md) is True


def test_count_sections_zero_when_no_headings():
    assert count_sections("just some prose, no headings at all") == 0
    assert has_needs_attention("just some prose") is False


def test_count_sections_ignores_headings_inside_fenced_code_blocks():
    # Reuses summarize._real_heading_lines, which is fence-aware -- a ##
    # heading-shaped line inside a fenced block must not count as a real
    # section (see digest/summarize.py's validate_output docstring).
    body_md = "## Real section\n\n```\n## Not a real heading\n```\n\n## Another real one\n"

    assert count_sections(body_md) == 2


def test_has_needs_attention_case_insensitive():
    assert has_needs_attention("## NEEDS ATTENTION\n\ntext\n") is True


# --- publish_to_site ---


class _FakeHTTPResponse:
    def __init__(self, body: bytes = b"{}"):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_publish_to_site_sends_expected_payload_and_headers(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        42,
        "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        "<p>hi</p>",
        "2026-07-29T10:00:00+00:00",
        3,
        "https://news-site.example.workers.dev",
        "ingest-secret",
    )

    assert captured["url"] == "https://news-site.example.workers.dev/ingest/42"
    assert captured["method"] == "PUT"
    assert captured["headers"]["x-ingest-key"] == "ingest-secret"
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["user-agent"] == "notification-digest/1.0"
    assert captured["body"] == {
        "created_at": "2026-07-29T10:00:00+00:00",
        "tldr": "hi",
        "item_count": 3,
        "section_count": 1,
        "has_attention": False,
        "body_html": "<p>hi</p>",
        "body_md": "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
    }


def test_publish_to_site_raises_on_http_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        publish_to_site(
            1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
            "https://news-site.example.workers.dev", "key",
        )


def test_publish_to_site_raises_on_network_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.URLError):
        publish_to_site(
            1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
            "https://news-site.example.workers.dev", "key",
        )


# --- publish_to_site: optional Hungarian fields (all three or none) ---


def test_publish_to_site_includes_hu_fields_when_both_hu_args_given(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        42,
        "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        "<p>hi</p>",
        "2026-07-29T10:00:00+00:00",
        3,
        "https://news-site.example.workers.dev",
        "ingest-secret",
        body_md_hu="**TL;DR:** szia\n\n## Érdemes tudni\n\ndolog",
        body_html_hu="<p>szia</p>",
    )

    body = captured["body"]
    assert body["tldr_hu"] == "szia"
    assert body["body_html_hu"] == "<p>szia</p>"
    assert body["body_md_hu"] == "**TL;DR:** szia\n\n## Érdemes tudni\n\ndolog"
    # No duplicated structural fields for the Hungarian body -- translation
    # cannot change section count or attention-flag, so the English-derived
    # values already describe it.
    assert "section_count_hu" not in body
    assert "has_attention_hu" not in body


def test_publish_to_site_omits_hu_fields_when_neither_hu_arg_given(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        42,
        "body",
        "<p>body</p>",
        "2026-07-29T10:00:00+00:00",
        1,
        "https://news-site.example.workers.dev",
        "key",
    )

    body = captured["body"]
    assert "tldr_hu" not in body
    assert "body_html_hu" not in body
    assert "body_md_hu" not in body


def test_publish_to_site_omits_hu_fields_when_only_body_md_hu_given(monkeypatch):
    # Guards the "all three or none" contract at the boundary: a caller bug
    # that supplies only one of the pair must not leak a partial set into
    # the payload (the Worker 400s a partial set).
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        42,
        "body",
        "<p>body</p>",
        "2026-07-29T10:00:00+00:00",
        1,
        "https://news-site.example.workers.dev",
        "key",
        body_md_hu="magyar szöveg",
    )

    body = captured["body"]
    assert "tldr_hu" not in body
    assert "body_html_hu" not in body
    assert "body_md_hu" not in body


def test_publish_to_site_hu_tldr_falls_back_to_no_summary_placeholder(monkeypatch):
    # Mirrors the English tldr's own "(no summary)" fallback -- a
    # pathological Hungarian body that extract_tldr can't find a TL;DR line
    # in must not fail the Worker's ingest validator.
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        42,
        "body",
        "<p>body</p>",
        "2026-07-29T10:00:00+00:00",
        1,
        "https://news-site.example.workers.dev",
        "key",
        body_md_hu="\n\n\n",
        body_html_hu="<p></p>",
    )

    assert captured["body"]["tldr_hu"] == "(no summary)"


# --- send_telegram_tldr ---


def test_send_telegram_tldr_sends_expected_payload_and_headers(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    send_telegram_tldr(
        7,
        "**TL;DR:** big news\n\n## Section\n\nmore.\n",
        "2026-07-29T10:00:00+00:00",
        "12345:bot-token-value",
        "-100123",
        99,
        "https://news.example.com/t/tok",
    )

    assert captured["url"] == "https://api.telegram.org/bot12345:bot-token-value/sendMessage"
    assert captured["method"] == "POST"
    assert captured["headers"]["user-agent"] == "notification-digest/1.0"
    body = captured["body"]
    assert body["chat_id"] == "-100123"
    assert body["disable_web_page_preview"] is True
    assert body["message_thread_id"] == 99
    assert "big news" in body["text"]
    # The link is an inline-keyboard BUTTON, never raw text: the URL embeds
    # the site's capability token and reads as noise in the topic, and a
    # button needs no parse_mode.
    assert "https://news.example.com/t/tok/d/7" not in body["text"]
    button = body["reply_markup"]["inline_keyboard"][0][0]
    assert button["url"] == "https://news.example.com/t/tok/d/7"
    assert button["text"] == "Open the digest →"
    # PLAIN TEXT only -- no parse_mode, ever.
    assert "parse_mode" not in body


def test_send_telegram_tldr_omits_thread_id_when_zero(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    send_telegram_tldr(
        7,
        "**TL;DR:** hi\n\n## S\n\nx\n",
        "2026-07-29T10:00:00+00:00",
        "bot-token",
        "-100123",
        0,
        "https://news.example.com/t/tok",
    )

    assert "message_thread_id" not in captured["body"]


def test_send_telegram_tldr_http_error_raises_sanitized_error_without_url_or_body(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.telegram.org/botSECRET-TOKEN/sendMessage",
            401,
            "Unauthorized: SECRET-TOKEN leaked in body",
            {},
            None,
        )

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(TelegramSendError) as exc_info:
        send_telegram_tldr(
            7, "**TL;DR:** hi\n\n## S\n\nx\n", "2026-07-29T10:00:00+00:00",
            "SECRET-TOKEN", "-100123", 0, "https://news.example.com/t/tok",
        )

    message = str(exc_info.value)
    assert "SECRET-TOKEN" not in message
    assert "401" in message
    assert exc_info.value.status == 401


def test_send_telegram_tldr_network_error_raises_sanitized_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(TelegramSendError) as exc_info:
        send_telegram_tldr(
            7, "**TL;DR:** hi\n\n## S\n\nx\n", "2026-07-29T10:00:00+00:00",
            "SECRET-TOKEN", "-100123", 0, "https://news.example.com/t/tok",
        )

    assert "SECRET-TOKEN" not in str(exc_info.value)
    assert exc_info.value.status is None


def test_send_telegram_tldr_status_matches_http_error_code_for_429_specifically(monkeypatch):
    # digest/main.py's per-run circuit breaker checks exactly `status == 429`
    # to decide whether to trip -- this pins the attribute for the specific
    # code that guard cares about, not just "some status was set."
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(TelegramSendError) as exc_info:
        send_telegram_tldr(
            7, "**TL;DR:** hi\n\n## S\n\nx\n", "2026-07-29T10:00:00+00:00",
            "bot-token", "-100123", 0, "https://news.example.com/t/tok",
        )

    assert exc_info.value.status == 429


def test_send_telegram_tldr_header_uses_europe_budapest_local_time(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    # 2026-07-29T10:00:00+00:00 UTC is 12:00 in Europe/Budapest (CEST, UTC+2).
    send_telegram_tldr(
        7, "**TL;DR:** hi\n\n## S\n\nx\n", "2026-07-29T10:00:00+00:00",
        "bot-token", "-100123", 0, "https://news.example.com/t/tok",
    )

    assert "12:00" in captured["body"]["text"]
