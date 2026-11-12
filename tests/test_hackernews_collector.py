"""Tests for digest/collectors/hackernews.py -- no network, ever.

`urllib.request.urlopen` is monkeypatched to a fake that resolves the single
front-page request to pre-built JSON bytes or an exception to raise,
mirroring tests/test_polymarket_collector.py's mocking discipline.
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


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record time.sleep calls so the retry pause never actually sleeps."""
    recorded: list[float] = []
    monkeypatch.setattr(hn_module.time, "sleep", recorded.append)
    return recorded


def _patch_urlopen_sequence(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[bytes | Exception]
) -> list[object]:
    """Fake urlopen returning/raising `outcomes` in order; returns the call log."""
    calls: list[object] = []
    remaining = list(outcomes)

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        calls.append(request)
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr(hn_module.urllib.request, "urlopen", fake_urlopen)
    return calls


_TOP_URL = f"{hn_module._FIREBASE_BASE}/topstories.json"


def _item_url(item_id: int) -> str:
    return f"{hn_module._FIREBASE_BASE}/item/{item_id}.json"


def _fb_item(item_id: int = 1, **overrides: object) -> dict:
    """One raw Firebase item (a None override drops the field)."""
    data: dict = {
        "by": "alice",
        "descendants": 12,
        "id": item_id,
        "score": 100,
        "time": int(time.time() - 3600),
        "title": "Something happened",
        "type": "story",
        "url": "https://example.com/story",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def _patch_routed(
    monkeypatch: pytest.MonkeyPatch, routes: dict[str, bytes | Exception]
) -> list[str]:
    """Fake urlopen routed by exact URL (Algolia, topstories, item/<id>).

    Returns the ordered list of requested URLs; an unrouted URL fails the test.
    """
    calls: list[str] = []

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        url = request.full_url  # type: ignore[attr-defined]
        calls.append(url)
        assert url in routes, f"unexpected request: {url}"
        outcome = routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr(hn_module.urllib.request, "urlopen", fake_urlopen)
    return calls


def _firebase_routes(items: dict[int, bytes | Exception]) -> dict[str, bytes | Exception]:
    """Algolia stalling, plus a Firebase front page of `items` (in dict order)."""
    routes: dict[str, bytes | Exception] = {
        _expected_url(): TimeoutError("algolia stalled"),
        _TOP_URL: json.dumps(list(items)).encode(),
    }
    for item_id, outcome in items.items():
        routes[_item_url(item_id)] = outcome
    return routes


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(_expected_url(), code, "err", None, None)  # type: ignore[arg-type]


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


# --- failure semantics: Algolia and Firebase both failing ---


def test_network_failure_marks_result_failed_and_no_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both Algolia and the Firebase fallback fail.
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


# --- retry policy: one retry after a transient failure ---


def test_timeout_then_success_retries_once(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _patch_urlopen_sequence(
        monkeypatch, [TimeoutError("timed out"), _hits_response([_hit(object_id="a1")])]
    )

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["a1"]
    assert sleeps == [3]
    assert len(calls) == 2


def test_timeout_twice_marks_result_failed(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _patch_urlopen_sequence(
        monkeypatch, [TimeoutError("t1"), TimeoutError("t2"), TimeoutError("fb")]
    )

    result = collect(15)

    assert result.failed is True
    assert result.items == []
    # Two Algolia attempts, then the Firebase topstories request (also fails).
    urls = [c.full_url for c in calls]  # type: ignore[attr-defined]
    assert urls == [_expected_url(), _expected_url(), _TOP_URL]
    assert sleeps == [3]


def test_url_error_then_success_is_retried(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _patch_urlopen_sequence(
        monkeypatch,
        [urllib.error.URLError("connection reset"), _hits_response([_hit(object_id="b1")])],
    )

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["b1"]
    assert len(calls) == 2
    assert sleeps == [3]


def test_http_503_then_success_is_retried(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _patch_urlopen_sequence(
        monkeypatch, [_http_error(503), _hits_response([_hit(object_id="c1")])]
    )

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["c1"]
    assert len(calls) == 2
    assert sleeps == [3]


def test_http_404_fails_immediately_without_retry(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _patch_urlopen_sequence(monkeypatch, [_http_error(404), _http_error(404)])

    result = collect(15)

    assert result.failed is True
    assert result.items == []
    # One Algolia attempt (no retry), then the Firebase topstories request.
    urls = [c.full_url for c in calls]  # type: ignore[attr-defined]
    assert urls == [_expected_url(), _TOP_URL]
    assert sleeps == []


def test_non_json_body_fails_immediately_without_retry(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    calls = _patch_urlopen_sequence(monkeypatch, [b"not json", b"not json"])

    result = collect(15)

    assert result.failed is True
    assert result.items == []
    # One Algolia attempt (no retry), then the Firebase topstories request.
    urls = [c.full_url for c in calls]  # type: ignore[attr-defined]
    assert urls == [_expected_url(), _TOP_URL]
    assert sleeps == []


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


# --- Firebase fallback ---


def test_algolia_success_never_calls_firebase(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_routed(monkeypatch, {_expected_url(): _hits_response([_hit(object_id="a1")])})

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["a1"]
    assert calls == [_expected_url()]


def test_algolia_twice_failing_falls_back_to_firebase(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    created = int(time.time() - 3600)
    fb = _fb_item(
        50,
        title="A big AI release",
        url="https://example.com/big-ai-release",
        by="submitter1",
        score=842,
        descendants=213,
        time=created,
    )
    calls = _patch_routed(monkeypatch, _firebase_routes({50: json.dumps(fb).encode()}))

    result = collect(15)

    assert result.failed is False
    assert calls == [_expected_url(), _expected_url(), _TOP_URL, _item_url(50)]
    assert sleeps == [3]
    item = result.items[0]
    assert item.source_id == "50"
    assert item.url == "https://news.ycombinator.com/item?id=50"
    assert "links to: https://example.com/big-ai-release" in item.text
    assert "[score 842, 213 comments]" in item.text

    # Same Item as the Algolia path produces for the equivalent story.
    algolia_hit = _hit(
        object_id="50",
        title="A big AI release",
        url="https://example.com/big-ai-release",
        author="submitter1",
        points=842,
        num_comments=213,
        created_at_i=created,
    )
    _patch_routed(monkeypatch, {_expected_url(): _hits_response([algolia_hit])})
    expected = collect(15).items[0]
    assert item.text == expected.text
    assert (item.source, item.source_id, item.chat_id, item.chat_title, item.author, item.url) == (
        expected.source,
        expected.source_id,
        expected.chat_id,
        expected.chat_title,
        expected.author,
        expected.url,
    )


def test_algolia_non_transient_failure_also_falls_back(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    routes = _firebase_routes({7: json.dumps(_fb_item(7)).encode()})
    routes[_expected_url()] = _http_error(404)
    calls = _patch_routed(monkeypatch, routes)

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["7"]
    assert calls == [_expected_url(), _TOP_URL, _item_url(7)]
    assert sleeps == []


def test_firebase_ask_hn_post_has_no_links_to_line(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _fb_item(8, title="Ask HN: What are you working on?", url=None)
    assert "url" not in raw
    _patch_routed(monkeypatch, _firebase_routes({8: json.dumps(raw).encode()}))

    result = collect(15)

    assert result.items[0].text == "Ask HN: What are you working on?\n\n[score 100, 12 comments]"


def test_firebase_topstories_failure_marks_result_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = _firebase_routes({})
    routes[_TOP_URL] = TimeoutError("firebase stalled")
    _patch_routed(monkeypatch, routes)

    result = collect(15)

    assert result.failed is True
    assert result.items == []


def test_firebase_topstories_not_a_list_marks_result_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = _firebase_routes({})
    routes[_TOP_URL] = json.dumps({"error": "x"}).encode()
    _patch_routed(monkeypatch, routes)

    assert collect(15).failed is True


def test_firebase_unusable_items_are_skipped_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items: dict[int, bytes | Exception] = {
        1: b"null",
        2: json.dumps(_fb_item(2, type="job")).encode(),
        3: json.dumps(_fb_item(3, dead=True)).encode(),
        4: json.dumps(_fb_item(4, deleted=True)).encode(),
        5: json.dumps(_fb_item(5)).encode(),
    }
    _patch_routed(monkeypatch, _firebase_routes(items))

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["5"]


def test_firebase_all_items_unusable_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    items: dict[int, bytes | Exception] = {
        1: b"null",
        2: json.dumps(_fb_item(2, type="job")).encode(),
    }
    _patch_routed(monkeypatch, _firebase_routes(items))

    result = collect(15)

    assert result.failed is False
    assert result.items == []


def test_firebase_non_transient_item_failure_skips_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items: dict[int, bytes | Exception] = {
        1: _http_error(404),
        2: b"not json",
        3: json.dumps(_fb_item(3)).encode(),
    }
    calls = _patch_routed(monkeypatch, _firebase_routes(items))

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["3"]
    assert calls[-3:] == [_item_url(1), _item_url(2), _item_url(3)]


def test_firebase_transient_item_failure_mid_list_stops_fetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items: dict[int, bytes | Exception] = {
        1: json.dumps(_fb_item(1)).encode(),
        2: json.dumps(_fb_item(2)).encode(),
        3: TimeoutError("firebase stalled"),
        4: json.dumps(_fb_item(4)).encode(),
    }
    calls = _patch_routed(monkeypatch, _firebase_routes(items))

    result = collect(15)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["1", "2"]
    assert _item_url(4) not in calls
    assert calls[-1] == _item_url(3)


def test_firebase_transient_failure_on_first_item_marks_result_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items: dict[int, bytes | Exception] = {
        1: _http_error(503),
        2: json.dumps(_fb_item(2)).encode(),
    }
    calls = _patch_routed(monkeypatch, _firebase_routes(items))

    result = collect(15)

    assert result.failed is True
    assert result.items == []
    assert _item_url(2) not in calls


def test_firebase_all_non_transient_item_failures_marks_result_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items: dict[int, bytes | Exception] = {1: _http_error(404), 2: b"not json"}
    _patch_routed(monkeypatch, _firebase_routes(items))

    assert collect(15).failed is True


def test_firebase_respects_top_n(monkeypatch: pytest.MonkeyPatch) -> None:
    items: dict[int, bytes | Exception] = {i: json.dumps(_fb_item(i)).encode() for i in range(1, 6)}
    routes = _firebase_routes(items)
    routes[_expected_url(2)] = routes.pop(_expected_url())
    calls = _patch_routed(monkeypatch, routes)

    result = collect(2)

    assert [item.source_id for item in result.items] == ["1", "2"]
    assert [c for c in calls if "/item/" in c] == [_item_url(1), _item_url(2)]
