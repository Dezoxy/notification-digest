"""Tests for digest/collectors/rss.py -- no network, ever.

`urllib.request.urlopen` is monkeypatched to a fake that resolves each
feed URL to either pre-built RSS/Atom bytes or an exception to raise --
`feedparser.parse` itself always runs for real (over the fake bytes) so
these tests exercise the actual RSS 2.0 / Atom parsing this module relies
on, not a mocked-away version of it.
"""

from __future__ import annotations

import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

import digest.collectors.rss as rss_module
from digest.collectors.rss import _MAX_ENTRIES_PER_FEED, _MAX_SUMMARY_CHARS, collect

_NOW = datetime.now(UTC)


def _rfc822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rss(items_xml: str, feed_title: str = "Test Feed") -> bytes:
    return f"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>{feed_title}</title>
{items_xml}
</channel></rss>
""".encode()


def _atom(entries_xml: str, feed_title: str = "Atom Test Feed") -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>{feed_title}</title>
{entries_xml}
</feed>
""".encode()


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch, responses: dict[str, bytes | Exception]
) -> None:
    """Resolve each feed URL in `responses` to fixture bytes, or raise the given exception."""

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        url = request.full_url  # type: ignore[attr-defined]
        outcome = responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.setattr(rss_module.urllib.request, "urlopen", fake_urlopen)


# --- empty input ---


def test_empty_feed_urls_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("urlopen must not be called for an empty feed list")

    monkeypatch.setattr(rss_module.urllib.request, "urlopen", boom)

    result = collect([])

    assert result.items == []
    assert result.cursor_updates == {}
    assert result.failed is False


# --- RSS 2.0 ---


def test_rss_feed_produces_items_with_correct_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = _rfc822(_NOW - timedelta(hours=2))
    body = _rss(
        f"""
        <item>
          <title>Recent Item</title>
          <link>https://example.com/a</link>
          <guid>guid-a</guid>
          <description><![CDATA[<p>Hello <b>world</b></p>]]></description>
          <pubDate>{recent}</pubDate>
          <author>author@example.com (Jane Doe)</author>
        </item>
        """,
        feed_title="My RSS Feed",
    )
    _patch_urlopen(monkeypatch, {"https://feed.example/rss": body})

    result = collect(["https://feed.example/rss"])

    assert result.failed is False
    assert len(result.items) == 1
    item = result.items[0]
    assert item.source == "news"
    assert item.source_id == "guid-a"
    assert item.url == "https://example.com/a"
    assert item.chat_id is None
    assert item.chat_title == "My RSS Feed"
    assert item.author == "author@example.com (Jane Doe)"
    assert item.text.startswith("Recent Item\n\n")
    assert "Hello world" in item.text


def test_rss_entry_link_used_as_source_id_when_no_guid(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = _rfc822(_NOW - timedelta(hours=1))
    body = _rss(
        f"""
        <item>
          <title>No Guid</title>
          <link>https://example.com/noguid</link>
          <pubDate>{recent}</pubDate>
        </item>
        """
    )
    _patch_urlopen(monkeypatch, {"https://feed.example/rss": body})

    result = collect(["https://feed.example/rss"])

    assert len(result.items) == 1
    assert result.items[0].source_id == "https://example.com/noguid"


# --- Atom ---


def test_atom_feed_id_and_summary_and_updated_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = _iso(_NOW - timedelta(hours=1))
    body = _atom(
        f"""
        <entry>
          <title>Atom Item</title>
          <link href="https://example.com/atom-a"/>
          <id>urn:uuid:atom-a</id>
          <updated>{recent}</updated>
          <summary>&lt;p&gt;Atom &amp; summary&lt;/p&gt;</summary>
          <author><name>Jane Atom</name></author>
        </entry>
        """,
        feed_title="My Atom Feed",
    )
    _patch_urlopen(monkeypatch, {"https://feed.example/atom": body})

    result = collect(["https://feed.example/atom"])

    assert len(result.items) == 1
    item = result.items[0]
    assert item.source_id == "urn:uuid:atom-a"
    assert item.url == "https://example.com/atom-a"
    assert item.chat_title == "My Atom Feed"
    assert item.author == "Jane Atom"
    assert "Atom & summary" in item.text
    # Atom entries with no <published> rely on <updated> via
    # entry.updated_parsed -- this entry has only <updated>, and it still
    # produced an item, proving the updated_parsed fallback path works.


# --- lookback window ---


def test_entry_older_than_lookback_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    old = _rfc822(_NOW - timedelta(hours=20))
    body = _rss(
        f"""
        <item>
          <title>Old</title>
          <link>https://example.com/old</link>
          <guid>guid-old</guid>
          <pubDate>{old}</pubDate>
        </item>
        """
    )
    _patch_urlopen(monkeypatch, {"https://feed.example/rss": body})

    result = collect(["https://feed.example/rss"])

    assert result.items == []
    assert result.failed is False


def test_entry_without_any_date_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _rss(
        """
        <item>
          <title>No Date</title>
          <link>https://example.com/nodate</link>
          <guid>guid-nodate</guid>
        </item>
        """
    )
    _patch_urlopen(monkeypatch, {"https://feed.example/rss": body})

    result = collect(["https://feed.example/rss"])

    assert result.items == []
    assert result.failed is False


# --- link validation ---


def test_entry_with_non_http_link_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = _rfc822(_NOW - timedelta(hours=1))
    body = _rss(
        f"""
        <item>
          <title>FTP Link</title>
          <link>ftp://example.com/file</link>
          <guid>guid-ftp</guid>
          <pubDate>{recent}</pubDate>
        </item>
        """
    )
    _patch_urlopen(monkeypatch, {"https://feed.example/rss": body})

    result = collect(["https://feed.example/rss"])

    assert result.items == []


def test_entry_with_missing_link_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = _rfc822(_NOW - timedelta(hours=1))
    body = _rss(
        f"""
        <item>
          <title>No Link</title>
          <guid isPermaLink="false">guid-nolink</guid>
          <pubDate>{recent}</pubDate>
        </item>
        """
    )
    _patch_urlopen(monkeypatch, {"https://feed.example/rss": body})

    result = collect(["https://feed.example/rss"])

    assert result.items == []


# --- summary cleaning ---


def test_summary_html_stripped_entities_unescaped_whitespace_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recent = _rfc822(_NOW - timedelta(hours=1))
    body = _rss(
        f"""
        <item>
          <title>Cleaning Test</title>
          <link>https://example.com/clean</link>
          <guid>guid-clean</guid>
          <description><![CDATA[<div>Tom  &amp;   Jerry\n\n  are
          <em>friends</em>.</div>]]></description>
          <pubDate>{recent}</pubDate>
        </item>
        """
    )
    _patch_urlopen(monkeypatch, {"https://feed.example/rss": body})

    result = collect(["https://feed.example/rss"])

    assert len(result.items) == 1
    text = result.items[0].text
    assert "<" not in text
    assert ">" not in text
    assert "Tom & Jerry are friends ." in text
    assert "  " not in text  # no doubled whitespace survives collapsing


def test_summary_truncated_to_max_chars_title_kept_in_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recent = _rfc822(_NOW - timedelta(hours=1))
    long_summary = "word " * 400  # far more than _MAX_SUMMARY_CHARS once cleaned
    title = "A Very Important Headline"
    body = _rss(
        f"""
        <item>
          <title>{title}</title>
          <link>https://example.com/long</link>
          <guid>guid-long</guid>
          <description>{long_summary}</description>
          <pubDate>{recent}</pubDate>
        </item>
        """
    )
    _patch_urlopen(monkeypatch, {"https://feed.example/rss": body})

    result = collect(["https://feed.example/rss"])

    assert len(result.items) == 1
    text = result.items[0].text
    assert text.startswith(title + "\n\n")
    summary_part = text[len(title) + 2 :]
    assert len(summary_part) == _MAX_SUMMARY_CHARS


def test_missing_summary_yields_title_only_text(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = _rfc822(_NOW - timedelta(hours=1))
    body = _rss(
        f"""
        <item>
          <title>Just A Title</title>
          <link>https://example.com/titleonly</link>
          <guid>guid-titleonly</guid>
          <pubDate>{recent}</pubDate>
        </item>
        """
    )
    _patch_urlopen(monkeypatch, {"https://feed.example/rss": body})

    result = collect(["https://feed.example/rss"])

    assert len(result.items) == 1
    assert result.items[0].text == "Just A Title"


# --- per-feed entry cap ---


def test_per_feed_entry_cap_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = _rfc822(_NOW - timedelta(hours=1))
    total_entries = _MAX_ENTRIES_PER_FEED + 2
    items_xml = "\n".join(
        f"""
        <item>
          <title>Entry {i}</title>
          <link>https://example.com/entry-{i}</link>
          <guid>guid-{i}</guid>
          <pubDate>{recent}</pubDate>
        </item>
        """
        for i in range(total_entries)
    )
    body = _rss(items_xml)
    _patch_urlopen(monkeypatch, {"https://feed.example/rss": body})

    result = collect(["https://feed.example/rss"])

    assert len(result.items) == _MAX_ENTRIES_PER_FEED


# --- per-feed failure isolation ---


def test_one_feed_fails_other_succeeds(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    recent = _rfc822(_NOW - timedelta(hours=1))
    good_body = _rss(
        f"""
        <item>
          <title>Good Item</title>
          <link>https://example.com/good</link>
          <guid>guid-good</guid>
          <pubDate>{recent}</pubDate>
        </item>
        """
    )
    _patch_urlopen(
        monkeypatch,
        {
            "https://feed.example/bad": urllib.error.URLError("connection refused"),
            "https://feed.example/good": good_body,
        },
    )

    with caplog.at_level("WARNING"):
        result = collect(["https://feed.example/bad", "https://feed.example/good"])

    assert result.failed is False
    assert len(result.items) == 1
    assert result.items[0].source_id == "guid-good"
    assert any("feed.example/bad" in record.message for record in caplog.records)


def test_all_feeds_fail_marks_result_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(
        monkeypatch,
        {
            "https://feed.example/one": urllib.error.URLError("dns failure"),
            "https://feed.example/two": TimeoutError("timed out"),
        },
    )

    result = collect(["https://feed.example/one", "https://feed.example/two"])

    assert result.failed is True
    assert result.items == []


# --- bozo handling ---


def test_bozo_with_zero_entries_counts_as_feed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    garbage = b"this is not xml or rss at all, just garbage text"
    _patch_urlopen(monkeypatch, {"https://feed.example/garbage": garbage})

    result = collect(["https://feed.example/garbage"])

    assert result.failed is True
    assert result.items == []


def test_bozo_with_entries_still_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = _rfc822(_NOW - timedelta(hours=1))
    # Malformed (unclosed </rss>) but with one recoverable, well-formed item --
    # feedparser is deliberately tolerant of this.
    bad_xml = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Bad</title>
<item><title>Recoverable</title><link>https://example.com/recoverable</link>
<guid>guid-recoverable</guid><pubDate>{recent}</pubDate></item>
</channel></rss
""".encode()
    _patch_urlopen(monkeypatch, {"https://feed.example/malformed": bad_xml})

    result = collect(["https://feed.example/malformed"])

    assert result.failed is False
    assert len(result.items) == 1
    assert result.items[0].source_id == "guid-recoverable"


# --- no cursor axis ---


def test_no_cursor_updates_ever(monkeypatch: pytest.MonkeyPatch) -> None:
    recent = _rfc822(_NOW - timedelta(hours=1))
    body = _rss(
        f"""
        <item>
          <title>Item</title>
          <link>https://example.com/item</link>
          <guid>guid-item</guid>
          <pubDate>{recent}</pubDate>
        </item>
        """
    )
    _patch_urlopen(monkeypatch, {"https://feed.example/rss": body})

    result = collect(["https://feed.example/rss"])

    assert result.cursor_updates == {}
