"""Tests for digest/collectors/reddit.py -- no network, ever.

`reddit._open` (the module's one network seam) is monkeypatched to a fake that
resolves each request to pre-built Atom bytes (plus response headers) or an
exception to raise, mirroring tests/test_rss_collector.py's mocking
discipline. `reddit._sleep` (the pacing wait) is monkeypatched to a recorder so
these tests never actually wait. The Atom fixtures are synthetic: fake
subreddits, fake users, fake posts.
"""

from __future__ import annotations

import email.message
import io
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from html import escape

import pytest

import digest.collectors.reddit as reddit_module
from digest.collectors.reddit import _MAX_SELFTEXT_EXCERPT_CHARS, _USER_AGENT, collect

_PROXY_KEY = "super-secret-proxy-key-never-logged"
_ACCESS_ID = "secret-access-id.access"
_ACCESS_SECRET = "secret-access-secret-never-logged"


class _FakeResponse:
    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        self._data = data
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self, amt: int | None = None) -> bytes:
        return self._data if amt is None else self._data[:amt]


def _http_error(
    code: int, reason: str = "x", headers: dict[str, str] | None = None, body: bytes = b""
) -> urllib.error.HTTPError:
    msg = email.message.Message()
    for key, value in (headers or {}).items():
        msg[key] = value
    return urllib.error.HTTPError("http://x", code, reason, msg, io.BytesIO(body))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _entry(
    post_id: str = "abc123",
    title: str = "Something happened",
    *,
    subreddit: str = "news",
    author: str | None = "/u/some_user",
    link: str | None = "default",
    content: str = "<p>Body text</p>",
    published: datetime | None = None,
    include_published: bool = True,
    entry_id: str | None = None,
) -> str:
    published = published or datetime.now(UTC) - timedelta(hours=2)
    link = (
        f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/slug/"
        if link == "default"
        else link
    )
    parts = [f"<id>{entry_id if entry_id is not None else 't3_' + post_id}</id>"]
    parts.append(f"<title>{escape(title)}</title>")
    if link:
        parts.append(f'<link href="{escape(link)}" />')
    if author:
        parts.append(f"<author><name>{author}</name></author>")
    parts.append(f'<content type="html">{escape(content)}</content>')
    parts.append(f"<updated>{_iso(published)}</updated>")
    if include_published:
        parts.append(f"<published>{_iso(published)}</published>")
    return "<entry>" + "".join(parts) + "</entry>"


def _feed(*entries: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>top posts</title>'
        + "".join(entries)
        + "</feed>"
    ).encode()


def _sub_url(subreddit: str, posts_per_sub: int = 10, base: str = reddit_module._BASE_URL) -> str:
    return f"{base}/r/{subreddit}/top/.rss?t=day&limit={posts_per_sub}"


def _patch_open(monkeypatch: pytest.MonkeyPatch, responses: dict) -> list[urllib.request.Request]:
    """Resolve each request's `full_url`; outcome is bytes, (bytes, headers) or an Exception."""
    seen: list[urllib.request.Request] = []

    def fake_open(request: urllib.request.Request) -> _FakeResponse:
        seen.append(request)
        outcome = responses[request.full_url]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, tuple):
            return _FakeResponse(*outcome)
        return _FakeResponse(outcome)

    monkeypatch.setattr(reddit_module, "_open", fake_open)
    return seen


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(reddit_module, "_sleep", calls.append)
    return calls


# --- request shape --------------------------------------------------------


def test_request_url_and_headers_have_no_cookie(monkeypatch, sleeps):
    seen = _patch_open(monkeypatch, {_sub_url("news", 7): _feed(_entry())})

    result = collect(["news"], 7)

    assert not result.failed
    assert len(seen) == 1
    request = seen[0]
    assert request.full_url == _sub_url("news", 7)
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["user-agent"] == _USER_AGENT
    assert "cookie" not in headers
    assert "x-proxy-key" not in headers
    assert "cf-access-client-id" not in headers


def test_proxy_key_and_access_pair_are_sent_with_custom_base(monkeypatch, sleeps):
    base = "https://proxy.example.com"
    seen = _patch_open(monkeypatch, {_sub_url("news", 10, base): _feed(_entry())})

    collect(
        ["news"],
        10,
        api_base=base,
        proxy_key=_PROXY_KEY,
        access_client_id=_ACCESS_ID,
        access_client_secret=_ACCESS_SECRET,
    )

    headers = {k.lower(): v for k, v in seen[0].header_items()}
    assert headers["x-proxy-key"] == _PROXY_KEY
    assert headers["cf-access-client-id"] == _ACCESS_ID
    assert headers["cf-access-client-secret"] == _ACCESS_SECRET
    assert "cookie" not in headers


def test_access_pair_needs_both_halves(monkeypatch, sleeps):
    seen = _patch_open(monkeypatch, {_sub_url("news"): _feed(_entry())})

    collect(["news"], 10, access_client_id=_ACCESS_ID)

    headers = {k.lower() for k, _ in seen[0].header_items()}
    assert "cf-access-client-id" not in headers
    assert "cf-access-client-secret" not in headers


def test_empty_subreddits_makes_no_calls(monkeypatch, sleeps):
    def boom(request):
        raise AssertionError("no network call expected")

    monkeypatch.setattr(reddit_module, "_open", boom)

    result = collect([], 10)

    assert result.items == []
    assert result.failed is False
    assert sleeps == []


# --- item mapping ---------------------------------------------------------


def test_item_mapping_strips_t3_prefix_and_maps_fields(monkeypatch, sleeps):
    feed = _feed(
        _entry(
            "1xcj1wm",
            "Local model released",
            subreddit="LocalLLaMA",
            author="/u/fake_author",
            content="<table><tr><td>Fast &amp; small</td></tr></table>",
        )
    )
    _patch_open(monkeypatch, {_sub_url("LocalLLaMA"): feed})

    result = collect(["LocalLLaMA"], 10)

    assert len(result.items) == 1
    item = result.items[0]
    assert item.source == "reddit"
    # DB continuity: the old JSON collector stored the bare id.
    assert item.source_id == "1xcj1wm"
    assert item.chat_id is None
    assert item.chat_title == "r/LocalLLaMA"
    assert item.author == "fake_author"
    assert item.url == "https://www.reddit.com/r/LocalLLaMA/comments/1xcj1wm/slug/"
    assert item.text == "Local model released Fast & small"
    assert result.cursor_updates == {}


def test_missing_author_is_none(monkeypatch, sleeps):
    _patch_open(monkeypatch, {_sub_url("news"): _feed(_entry(author=None))})

    assert collect(["news"], 10).items[0].author is None


def test_entries_without_usable_id_or_title_are_skipped(monkeypatch, sleeps):
    feed = _feed(
        _entry("good1"),
        _entry(entry_id="t3_"),
        _entry(entry_id="t3_bad id!"),
        _entry("good2", title="   "),
        _entry("good3"),
    )
    _patch_open(monkeypatch, {_sub_url("news"): feed})

    result = collect(["news"], 10)

    assert [i.source_id for i in result.items] == ["good1", "good3"]


@pytest.mark.parametrize(
    "link",
    [
        "http://www.reddit.com/r/news/comments/abc123/slug/",
        "https://evil.example.com/r/news/comments/abc123/slug/",
        "https://www.reddit.com.evil.example.com/r/news/comments/abc123/",
        "https://www.reddit.com/user/someone/",
        "javascript:alert(1)",
        None,
    ],
)
def test_bad_link_falls_back_to_built_url(monkeypatch, sleeps, link):
    _patch_open(monkeypatch, {_sub_url("news"): _feed(_entry("abc123", link=link))})

    item = collect(["news"], 10).items[0]

    assert item.url == "https://www.reddit.com/r/news/comments/abc123/"


def test_bare_reddit_com_link_is_accepted(monkeypatch, sleeps):
    link = "https://reddit.com/r/news/comments/abc123/slug/"
    _patch_open(monkeypatch, {_sub_url("news"): _feed(_entry("abc123", link=link))})

    assert collect(["news"], 10).items[0].url == link


def test_text_excerpt_is_capped_and_tags_stripped(monkeypatch, sleeps):
    long_html = "<p>" + ("word " * 400) + "</p><script>x()</script>"
    _patch_open(monkeypatch, {_sub_url("news"): _feed(_entry(title="T", content=long_html))})

    text = collect(["news"], 10).items[0].text

    assert text.startswith("T word word")
    assert "<" not in text
    assert len(text) <= len("T ") + _MAX_SELFTEXT_EXCERPT_CHARS


def test_posts_per_sub_caps_items(monkeypatch, sleeps):
    feed = _feed(*[_entry(f"p{i}") for i in range(5)])
    _patch_open(monkeypatch, {_sub_url("news", 3): feed})

    assert len(collect(["news"], 3).items) == 3


# --- window ---------------------------------------------------------------


def test_stale_entry_is_dropped_and_fresh_kept(monkeypatch, sleeps):
    old = datetime.now(UTC) - timedelta(hours=30)
    feed = _feed(_entry("fresh"), _entry("stale", published=old))
    _patch_open(monkeypatch, {_sub_url("news"): feed})

    assert [i.source_id for i in collect(["news"], 10).items] == ["fresh"]


def test_published_wins_over_updated(monkeypatch, sleeps):
    now = datetime.now(UTC)
    entry = (
        "<entry><id>t3_p1</id><title>t</title>"
        f"<updated>{_iso(now)}</updated><published>{_iso(now - timedelta(hours=40))}</published>"
        "</entry>"
    )
    _patch_open(monkeypatch, {_sub_url("news"): _feed(entry)})

    assert collect(["news"], 10).items == []


def test_updated_used_when_published_missing(monkeypatch, sleeps):
    old = datetime.now(UTC) - timedelta(hours=40)
    feed = _feed(_entry("stale", published=old, include_published=False))
    _patch_open(monkeypatch, {_sub_url("news"): feed})

    assert collect(["news"], 10).items == []


@pytest.mark.parametrize("stamps", ["", "<updated>not a date</updated>"])
def test_missing_or_unparseable_timestamp_fails_open(monkeypatch, sleeps, stamps):
    entry = f"<entry><id>t3_p1</id><title>t</title>{stamps}</entry>"
    _patch_open(monkeypatch, {_sub_url("news"): _feed(entry)})

    assert [i.source_id for i in collect(["news"], 10).items] == ["p1"]


# --- pacing ---------------------------------------------------------------


def _two(monkeypatch, headers_first: dict[str, str] | None):
    _patch_open(
        monkeypatch,
        {
            _sub_url("a"): (_feed(_entry("a1")), headers_first or {}),
            _sub_url("b"): (_feed(_entry("b1")), {}),
        },
    )


def test_wait_from_reset_header_plus_margin(monkeypatch, sleeps):
    _two(monkeypatch, {"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "38"})

    result = collect(["a", "b"], 10)

    assert sleeps == [40]
    assert [i.source_id for i in result.items] == ["a1", "b1"]


def test_wait_uses_default_interval_when_headers_absent(monkeypatch, sleeps):
    _two(monkeypatch, None)

    collect(["a", "b"], 10, request_interval_seconds=33)

    assert sleeps == [33]


def test_wait_uses_interval_when_reset_not_numeric(monkeypatch, sleeps):
    _two(monkeypatch, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "soon"})

    collect(["a", "b"], 10, request_interval_seconds=33)

    assert sleeps == [33]


def test_no_wait_when_budget_remains(monkeypatch, sleeps):
    _two(monkeypatch, {"x-ratelimit-remaining": "5.0", "x-ratelimit-reset": "300"})

    collect(["a", "b"], 10)

    assert sleeps == []


def test_wait_is_capped(monkeypatch, sleeps):
    _two(monkeypatch, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "5000"})

    collect(["a", "b"], 10, max_wait_seconds=90)

    assert sleeps == [90]


def test_interval_is_capped_too(monkeypatch, sleeps):
    _two(monkeypatch, None)

    collect(["a", "b"], 10, request_interval_seconds=500, max_wait_seconds=60)

    assert sleeps == [60]


def test_no_wait_after_last_subreddit(monkeypatch, sleeps):
    _patch_open(
        monkeypatch,
        {
            _sub_url("a"): (_feed(_entry("a1")), {}),
            _sub_url("b"): (_feed(_entry("b1")), {}),
            _sub_url("c"): (_feed(_entry("c1")), {}),
        },
    )

    collect(["a", "b", "c"], 10)

    assert len(sleeps) == 2


def test_single_subreddit_never_waits(monkeypatch, sleeps):
    _patch_open(monkeypatch, {_sub_url("a"): _feed(_entry())})

    collect(["a"], 10)

    assert sleeps == []


def test_skipped_failure_still_paces_using_error_headers(monkeypatch, sleeps):
    err = _http_error(503, "Unavailable", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "10"})
    _patch_open(monkeypatch, {_sub_url("a"): err, _sub_url("b"): _feed(_entry("b1"))})

    result = collect(["a", "b"], 10)

    assert sleeps == [12]
    assert [i.source_id for i in result.items] == ["b1"]


# --- failure semantics ----------------------------------------------------


@pytest.mark.parametrize("code", [429, 401, 403])
def test_back_off_codes_abort_rest_and_fail(monkeypatch, sleeps, code):
    seen = _patch_open(
        monkeypatch,
        {
            _sub_url("a"): (_feed(_entry("a1")), {}),
            _sub_url("b"): _http_error(code, "nope"),
            _sub_url("c"): (_feed(_entry("c1")), {}),
        },
    )

    result = collect(["a", "b", "c"], 10)

    assert result.failed is True
    # Earlier success is kept; "c" is never requested.
    assert [i.source_id for i in result.items] == ["a1"]
    assert [r.full_url for r in seen] == [_sub_url("a"), _sub_url("b")]
    # One pacing wait after "a"; none after the aborting request.
    assert len(sleeps) == 1


def test_429_is_not_retried_and_retry_after_is_logged(monkeypatch, sleeps, caplog):
    seen = _patch_open(
        monkeypatch,
        {_sub_url("a"): _http_error(429, "Too Many Requests", {"retry-after": "37"})},
    )

    with caplog.at_level(logging.WARNING, logger="digest.collectors.reddit"):
        result = collect(["a"], 10)

    assert result.failed is True
    assert len(seen) == 1
    assert "retry-after 37s" in caplog.text


def test_non_numeric_retry_after_is_not_logged(monkeypatch, sleeps, caplog):
    _patch_open(
        monkeypatch,
        {_sub_url("a"): _http_error(429, "Too Many Requests", {"retry-after": "evil\ntext"})},
    )

    with caplog.at_level(logging.WARNING, logger="digest.collectors.reddit"):
        collect(["a"], 10)

    assert "evil" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        _http_error(500, "Server Error"),
        _http_error(404, "Not Found"),
        urllib.error.URLError("boom"),
        TimeoutError("slow"),
    ],
)
def test_other_errors_skip_that_subreddit_only(monkeypatch, sleeps, error):
    _patch_open(
        monkeypatch,
        {_sub_url("a"): error, _sub_url("b"): (_feed(_entry("b1")), {})},
    )

    result = collect(["a", "b"], 10)

    assert result.failed is False
    assert [i.source_id for i in result.items] == ["b1"]


def test_unparseable_feed_is_skipped(monkeypatch, sleeps):
    _patch_open(
        monkeypatch,
        {_sub_url("a"): b"<html>not a feed", _sub_url("b"): (_feed(_entry("b1")), {})},
    )

    result = collect(["a", "b"], 10)

    assert result.failed is False
    assert [i.source_id for i in result.items] == ["b1"]


def test_oversized_response_is_rejected(monkeypatch, sleeps):
    monkeypatch.setattr(reddit_module, "_MAX_FEED_BYTES", 100)
    _patch_open(monkeypatch, {_sub_url("a"): _feed(_entry())})

    result = collect(["a"], 10)

    assert result.failed is True
    assert result.items == []


def test_every_subreddit_failing_sets_failed(monkeypatch, sleeps):
    _patch_open(
        monkeypatch,
        {_sub_url("a"): _http_error(500), _sub_url("b"): urllib.error.URLError("x")},
    )

    assert collect(["a", "b"], 10).failed is True


def test_empty_valid_feed_is_success_with_no_items(monkeypatch, sleeps):
    _patch_open(monkeypatch, {_sub_url("a"): _feed()})

    result = collect(["a"], 10)

    assert result.failed is False
    assert result.items == []


# --- failure descriptions / secrets ---------------------------------------


def test_describe_failure_labels():
    describe = reddit_module._describe_failure
    assert describe(_http_error(429, "Too Many Requests")) == "HTTPError 429 Too Many Requests"
    assert "proxy rejected key" in describe(
        _http_error(403, "Forbidden", body=b'{"error":"forbidden"}')
    )
    assert "cloudflare 1010" in describe(_http_error(403, "Forbidden", body=b"error code: 1010"))
    assert "reddit network block" in describe(
        _http_error(403, "Blocked", body=b"You are blocked by network security.")
    )
    assert "access login redirect" in describe(
        _http_error(302, "Found", {"Location": "https://team.cloudflareaccess.com/login?x=secret"})
    )
    assert "secret" not in describe(
        _http_error(302, "Found", {"Location": "https://team.cloudflareaccess.com/login?x=secret"})
    )
    assert "redirect without Location" in describe(_http_error(302, "Found"))
    assert describe(urllib.error.URLError("detail with secret")) == "URLError"
    assert "unparseable feed" in describe(ValueError("detail with secret"))


def test_secrets_never_appear_in_logs(monkeypatch, sleeps, caplog):
    leaky = ValueError(f"{_PROXY_KEY} {_ACCESS_ID} {_ACCESS_SECRET}")
    _patch_open(
        monkeypatch,
        {
            _sub_url("a"): leaky,
            _sub_url("b"): _http_error(
                403, "Forbidden", body=f"{_PROXY_KEY} {_ACCESS_SECRET}".encode()
            ),
        },
    )

    with caplog.at_level(logging.DEBUG, logger="digest.collectors.reddit"):
        collect(
            ["a", "b"],
            10,
            proxy_key=_PROXY_KEY,
            access_client_id=_ACCESS_ID,
            access_client_secret=_ACCESS_SECRET,
        )

    assert caplog.text
    for secret in (_PROXY_KEY, _ACCESS_ID, _ACCESS_SECRET):
        assert secret not in caplog.text
