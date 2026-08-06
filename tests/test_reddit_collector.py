"""Tests for digest/collectors/reddit.py -- no network, ever.

`urllib.request.urlopen` is monkeypatched to a fake that resolves each
request (the token exchange, and each per-subreddit listing) to pre-built
JSON bytes or an exception to raise, mirroring
tests/test_polymarket_collector.py's and tests/test_rss_collector.py's
mocking discipline. `time.sleep` (the inter-subreddit pacing) is also
monkeypatched to a no-op so these tests don't actually wait.
"""

from __future__ import annotations

import json
import time
import urllib.error

import pytest

import digest.collectors.reddit as reddit_module
from digest.collectors.reddit import _INTER_SUB_SLEEP_SECONDS, _USER_AGENT, collect


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, responses: dict) -> None:
    """Resolve each request's `full_url` to fixture bytes/an exception.

    `responses` maps the request's `full_url` to either raw bytes (the fake
    response body) or an Exception instance to raise. The token endpoint's
    URL is always `reddit_module._TOKEN_URL`; per-subreddit URLs are built
    the same way the collector itself builds them.
    """

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        url = request.full_url  # type: ignore[attr-defined]
        outcome = responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr(reddit_module.urllib.request, "urlopen", fake_urlopen)


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(reddit_module.time, "sleep", lambda s: calls.append(s))
    return calls


def _token_response(token: str = "tok-123") -> bytes:
    return json.dumps({"access_token": token, "token_type": "bearer", "expires_in": 3600}).encode()


def _sub_url(subreddit: str, posts_per_sub: int = 10) -> str:
    return (
        f"https://oauth.reddit.com/r/{subreddit}/top?t=day&limit={posts_per_sub}&raw_json=1"
    )


def _post(
    post_id: str = "p1",
    title: str = "Something happened",
    author: str | None = "alice",
    permalink: str = "/r/news/comments/p1/something_happened/",
    score: int = 100,
    num_comments: int = 12,
    selftext: str = "",
    created_utc: float | None = None,
    stickied: bool = False,
    over_18: bool = False,
) -> dict:
    """One raw `Listing` child object, matching the live API's field shapes."""
    data = {
        "id": post_id,
        "title": title,
        "author": author,
        "permalink": permalink,
        "score": score,
        "num_comments": num_comments,
        "selftext": selftext,
        "created_utc": created_utc if created_utc is not None else time.time() - 3600,
        "stickied": stickied,
        "over_18": over_18,
    }
    return {"kind": "t3", "data": data}


def _listing(posts: list[dict]) -> bytes:
    return json.dumps({"kind": "Listing", "data": {"children": posts}}).encode()


# --- token fetch shape ---


def test_token_request_uses_basic_auth_and_client_credentials_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        if request.full_url == reddit_module._TOKEN_URL:  # type: ignore[attr-defined]
            captured["request"] = request
            return _FakeResponse(_token_response())
        return _FakeResponse(_listing([]))

    monkeypatch.setattr(reddit_module.urllib.request, "urlopen", fake_urlopen)
    _no_sleep(monkeypatch)

    collect("client-id", "client-secret", ["news"], 10)

    request = captured["request"]
    assert request.get_header("Authorization", "").startswith("Basic ")
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert request.get_header("User-agent") == _USER_AGENT
    assert request.data == b"grant_type=client_credentials"
    assert request.get_method() == "POST"


def test_token_fetch_failure_marks_whole_run_failed_and_no_subreddits_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        if request.full_url == reddit_module._TOKEN_URL:  # type: ignore[attr-defined]
            raise urllib.error.URLError("connection refused")
        raise AssertionError("no subreddit request should be made when the token fetch fails")

    monkeypatch.setattr(reddit_module.urllib.request, "urlopen", fake_urlopen)
    _no_sleep(monkeypatch)

    result = collect("client-id", "client-secret", ["news", "hungary"], 10)

    assert result.failed is True
    assert result.items == []


def test_token_response_missing_access_token_marks_run_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(
        monkeypatch,
        {reddit_module._TOKEN_URL: json.dumps({"token_type": "bearer"}).encode()},
    )
    _no_sleep(monkeypatch)

    result = collect("client-id", "client-secret", ["news"], 10)

    assert result.failed is True


# --- per-subreddit request shape ---


def test_subreddit_request_url_params_and_bearer_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        url = request.full_url  # type: ignore[attr-defined]
        if url == reddit_module._TOKEN_URL:
            return _FakeResponse(_token_response("tok-abc"))
        captured["request"] = request
        return _FakeResponse(_listing([]))

    monkeypatch.setattr(reddit_module.urllib.request, "urlopen", fake_urlopen)
    _no_sleep(monkeypatch)

    collect("client-id", "client-secret", ["LocalLLaMA"], 7)

    request = captured["request"]
    assert request.full_url == _sub_url("LocalLLaMA", 7)
    assert request.get_header("Authorization") == "Bearer tok-abc"
    assert request.get_header("User-agent") == _USER_AGENT


# --- per-sub fault tolerance vs total failure ---


def test_one_subreddit_fails_other_succeeds(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    good_post = _post(post_id="good1")
    responses = {
        reddit_module._TOKEN_URL: _token_response(),
        _sub_url("badsub"): urllib.error.URLError("connection refused"),
        _sub_url("goodsub"): _listing([good_post]),
    }
    _patch_urlopen(monkeypatch, responses)
    _no_sleep(monkeypatch)

    with caplog.at_level("WARNING"):
        result = collect("id", "secret", ["badsub", "goodsub"], 10)

    assert result.failed is False
    assert len(result.items) == 1
    assert result.items[0].source_id == "good1"
    assert any("badsub" in record.message for record in caplog.records)


def test_all_subreddits_fail_marks_result_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        reddit_module._TOKEN_URL: _token_response(),
        _sub_url("one"): urllib.error.URLError("dns failure"),
        _sub_url("two"): TimeoutError("timed out"),
    }
    _patch_urlopen(monkeypatch, responses)
    _no_sleep(monkeypatch)

    result = collect("id", "secret", ["one", "two"], 10)

    assert result.failed is True
    assert result.items == []


def test_empty_subreddits_is_a_noop_no_network_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("urlopen must not be called for an empty subreddit list")

    monkeypatch.setattr(reddit_module.urllib.request, "urlopen", boom)

    result = collect("id", "secret", [], 10)

    assert result.items == []
    assert result.cursor_updates == {}
    assert result.failed is False


# --- pacing between subreddit requests ---


def test_pacing_sleep_called_between_subs_not_before_first(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        reddit_module._TOKEN_URL: _token_response(),
        _sub_url("one"): _listing([]),
        _sub_url("two"): _listing([]),
        _sub_url("three"): _listing([]),
    }
    _patch_urlopen(monkeypatch, responses)
    calls = _no_sleep(monkeypatch)

    collect("id", "secret", ["one", "two", "three"], 10)

    assert calls == [_INTER_SUB_SLEEP_SECONDS, _INTER_SUB_SLEEP_SECONDS]


def test_pacing_constant_is_a_small_fixed_value() -> None:
    assert _INTER_SUB_SLEEP_SECONDS == 0.5


# --- item mapping ---


def test_item_mapping_and_chat_title_for_hungary(monkeypatch: pytest.MonkeyPatch) -> None:
    post = _post(
        post_id="h1",
        title="Napi tegnap",
        author="bob",
        permalink="/r/hungary/comments/h1/napi_tegnap/",
        score=4321,
        num_comments=87,
        selftext="Some longer body text.",
    )
    responses = {
        reddit_module._TOKEN_URL: _token_response(),
        _sub_url("hungary"): _listing([post]),
    }
    _patch_urlopen(monkeypatch, responses)
    _no_sleep(monkeypatch)

    result = collect("id", "secret", ["hungary"], 10)

    assert len(result.items) == 1
    item = result.items[0]
    assert item.source == "reddit"
    assert item.source_id == "h1"
    assert item.chat_id is None
    assert item.chat_title == "r/hungary"
    assert item.author == "bob"
    assert item.url == "https://www.reddit.com/r/hungary/comments/h1/napi_tegnap/"
    assert item.text.startswith("Napi tegnap [score 4321, 87 comments]")
    assert "Some longer body text." in item.text


def test_selftext_excerpt_truncated_to_500_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    # No whitespace at all in the filler -- avoids the 500-char cut landing on
    # a space that the final `.strip()` in _build_item would then trim away,
    # which would make this assertion flaky on the exact excerpt length.
    long_selftext = "word" * 300  # far more than 500 chars once cleaned
    post = _post(post_id="p1", selftext=long_selftext)
    responses = {
        reddit_module._TOKEN_URL: _token_response(),
        _sub_url("news"): _listing([post]),
    }
    _patch_urlopen(monkeypatch, responses)
    _no_sleep(monkeypatch)

    result = collect("id", "secret", ["news"], 10)

    text = result.items[0].text
    excerpt = text.split("] ", 1)[1]
    assert len(excerpt) == 500


def test_no_cursor_updates_ever(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = {
        reddit_module._TOKEN_URL: _token_response(),
        _sub_url("news"): _listing([_post()]),
    }
    _patch_urlopen(monkeypatch, responses)
    _no_sleep(monkeypatch)

    result = collect("id", "secret", ["news"], 10)

    assert result.cursor_updates == {}


# --- stickied / over_18 / stale-window filtering ---


def test_stickied_post_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    stickied = _post(post_id="sticky1", stickied=True)
    normal = _post(post_id="normal1")
    responses = {
        reddit_module._TOKEN_URL: _token_response(),
        _sub_url("news"): _listing([stickied, normal]),
    }
    _patch_urlopen(monkeypatch, responses)
    _no_sleep(monkeypatch)

    result = collect("id", "secret", ["news"], 10)

    assert [item.source_id for item in result.items] == ["normal1"]


def test_over_18_post_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    nsfw = _post(post_id="nsfw1", over_18=True)
    normal = _post(post_id="normal1")
    responses = {
        reddit_module._TOKEN_URL: _token_response(),
        _sub_url("news"): _listing([nsfw, normal]),
    }
    _patch_urlopen(monkeypatch, responses)
    _no_sleep(monkeypatch)

    result = collect("id", "secret", ["news"], 10)

    assert [item.source_id for item in result.items] == ["normal1"]


def test_post_older_than_lookback_window_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = _post(post_id="stale1", created_utc=time.time() - (25 * 3600))
    fresh = _post(post_id="fresh1", created_utc=time.time() - 3600)
    responses = {
        reddit_module._TOKEN_URL: _token_response(),
        _sub_url("news"): _listing([stale, fresh]),
    }
    _patch_urlopen(monkeypatch, responses)
    _no_sleep(monkeypatch)

    result = collect("id", "secret", ["news"], 10)

    assert [item.source_id for item in result.items] == ["fresh1"]


def test_malformed_post_is_skipped_never_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    missing_title = _post(post_id="notitle")
    del missing_title["data"]["title"]
    good = _post(post_id="good1")
    responses = {
        reddit_module._TOKEN_URL: _token_response(),
        _sub_url("news"): _listing([missing_title, good]),
    }
    _patch_urlopen(monkeypatch, responses)
    _no_sleep(monkeypatch)

    result = collect("id", "secret", ["news"], 10)

    assert result.failed is False
    assert [item.source_id for item in result.items] == ["good1"]
