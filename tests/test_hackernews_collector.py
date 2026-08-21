"""Tests for digest/collectors/hackernews.py -- no network, ever.

`urllib.request.urlopen` is monkeypatched to a fake that resolves the single
front-page request to pre-built JSON bytes or an exception to raise,
mirroring tests/test_polymarket_collector.py's and tests/test_reddit_
collector.py's mocking discipline.
"""

from __future__ import annotations

import json
import time
import urllib.error

import pytest

import digest.collectors.hackernews as hn_module
from digest.collectors.hackernews import _USER_AGENT, collect


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, outcome: bytes | Exception) -> None:
    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr(hn_module.urllib.request, "urlopen", fake_urlopen)


def _hit(
    object_id: str = "1",
    title: str = "Something happened",
    url: str | None = "https://example.com/story",
    author: str | None = "alice",
    points: int = 100,
    num_comments: int = 12,
    created_at_i: float | None = None,
) -> dict:
    """One raw Algolia hit object, matching the live API's field shapes."""
    data = {
        "objectID": object_id,
        "title": title,
        "points": points,
        "num_comments": num_comments,
        "author": author,
        "created_at_i": (created_at_i if created_at_i is not None else time.time() - 3600),
    }
    if url is not None:
        data["url"] = url
    return data


def _hits_response(hits: list[dict]) -> bytes:
    return json.dumps({"hits": hits, "nbHits": len(hits)}).encode()


def _expected_url(top_n: int = 15) -> str:
    return f"{hn_module._API_BASE}?tags=front_page&hitsPerPage={top_n}"


# --- request shape ---


def test_request_url_params_and_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        return _FakeResponse(_hits_response([]))

    monkeypatch.setattr(hn_module.urllib.request, "urlopen", fake_urlopen)

    collect(15)

    request = captured["request"]
    assert request.full_url == _expected_url(15)  # type: ignore[attr-defined]
    assert request.get_header("User-agent") == _USER_AGENT
    assert captured["timeout"] == hn_module._REQUEST_TIMEOUT_SECONDS


def test_top_n_is_passed_through_as_hits_per_page(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        return _FakeResponse(_hits_response([]))

    monkeypatch.setattr(hn_module.urllib.request, "urlopen", fake_urlopen)

    collect(7)

    assert captured["url"] == _expected_url(7)


# --- failure semantics: one request, one signal ---


def test_network_failure_marks_result_failed_and_no_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, urllib.error.URLError("connection refused"))

    result = collect(15)

    assert result.failed is True
    assert result.items == []


def test_non_json_body_marks_result_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, b"not json")

    result = collect(15)

    assert result.failed is True
    assert result.items == []


def test_response_missing_hits_key_marks_result_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, json.dumps({"nbHits": 0}).encode())

    result = collect(15)

    assert result.failed is True
    assert result.items == []


def test_successful_empty_hits_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, _hits_response([]))

    result = collect(15)

    assert result.failed is False
    assert result.items == []


# --- item mapping ---


def test_item_mapping_with_article_url(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = _hit(
        object_id="123456",
        title="A big AI release",
        url="https://example.com/big-ai-release",
        author="submitter1",
        points=842,
        num_comments=213,
    )
    _patch_urlopen(monkeypatch, _hits_response([hit]))

    result = collect(15)

    assert len(result.items) == 1
    item = result.items[0]
    assert item.source == "hackernews"
    assert item.source_id == "123456"
    assert item.chat_id is None
    assert item.chat_title == "Hacker News"
    assert item.author == "submitter1"
    assert item.url == "https://news.ycombinator.com/item?id=123456"
    assert item.text == (
        "A big AI release\n\n"
        "links to: https://example.com/big-ai-release\n\n"
        "[score 842, 213 comments]"
    )


def test_ask_hn_post_has_no_url_and_no_links_to_line(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = _hit(object_id="999", title="Ask HN: What are you working on?", url=None)
    _patch_urlopen(monkeypatch, _hits_response([hit]))

    result = collect(15)

    assert len(result.items) == 1
    item = result.items[0]
    assert "links to:" not in item.text
    assert item.text == "Ask HN: What are you working on?\n\n[score 100, 12 comments]"
    # The discussion-page URL is still the item's own URL, never absent.
    assert item.url == "https://news.ycombinator.com/item?id=999"


def test_no_cursor_updates_ever(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, _hits_response([_hit()]))

    result = collect(15)

    assert result.cursor_updates == {}


# --- freshness / defensiveness ---


def test_hit_older_than_lookback_window_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = _hit(object_id="stale1", created_at_i=time.time() - (25 * 3600))
    fresh = _hit(object_id="fresh1", created_at_i=time.time() - 3600)
    _patch_urlopen(monkeypatch, _hits_response([stale, fresh]))

    result = collect(15)

    assert [item.source_id for item in result.items] == ["fresh1"]


def test_hit_missing_title_is_skipped_never_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    missing_title = _hit(object_id="notitle")
    del missing_title["title"]
    good = _hit(object_id="good1")
    _patch_urlopen(monkeypatch, _hits_response([missing_title, good]))

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["good1"]


def test_hit_missing_object_id_is_skipped_never_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    missing_id = _hit()
    del missing_id["objectID"]
    good = _hit(object_id="good1")
    _patch_urlopen(monkeypatch, _hits_response([missing_id, good]))

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["good1"]


def test_hit_missing_created_at_i_is_skipped_never_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_created = _hit(object_id="nocreated")
    del missing_created["created_at_i"]
    good = _hit(object_id="good1")
    _patch_urlopen(monkeypatch, _hits_response([missing_created, good]))

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["good1"]


def test_non_dict_hit_entry_is_skipped_never_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _hit(object_id="good1")
    # Inject a malformed (non-dict) entry directly into the raw JSON body,
    # since _hit()/_hits_response() can only build well-formed dict hits.
    raw = json.dumps({"hits": ["not-a-dict", good]}).encode()
    _patch_urlopen(monkeypatch, raw)

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["good1"]


def test_missing_points_and_num_comments_default_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = _hit(object_id="p1")
    del hit["points"]
    del hit["num_comments"]
    _patch_urlopen(monkeypatch, _hits_response([hit]))

    result = collect(15)

    assert len(result.items) == 1
    assert "[score 0, 0 comments]" in result.items[0].text


def test_missing_author_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = _hit(object_id="p1")
    del hit["author"]
    _patch_urlopen(monkeypatch, _hits_response([hit]))

    result = collect(15)

    assert result.items[0].author is None
