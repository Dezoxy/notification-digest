"""RSS/Atom collector: fetch new articles from owner-curated news feeds.

See PLAN.md for the full plan. This module is synchronous (urllib), unlike
telegram.py/x.py -- the caller (digest/main.py) runs collectors sequentially,
one at a time, so there is nothing else in flight to block on.

No interest filtering happens here: EVERY entry within the lookback window
from EVERY configured feed becomes an Item. Whether a given article is about
frontier models, robotics, or something the reader doesn't care about is
decided later, in the summarizer prompt (prompts/digest.md's "News interest
filter" section) -- this module's only job is faithful, undifferentiated
collection.

Failure semantics deliberately DIVERGE from telegram.py/x.py: those set
`failed=True` on ANY per-chat/pagination hiccup, because a single Telegram
chat or the one X notifications timeline failing is itself the whole
signal. Here, `failed=True` only when EVERY configured feed failed (a total
outage) -- a lone flaky publisher (dead DNS, a 500, a five-second timeout) is
routine and expected across a curated feed list, and must not trip the ⚠
banner / non-zero exit / Loki alert for what is, in practice, background
noise. See `collect`'s docstring for the exact rule.
"""

from __future__ import annotations

import html
import logging
import re
import time
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from time import struct_time
from typing import Any
from urllib.parse import urlsplit

import feedparser

from digest.collectors.base import CollectResult
from digest.state import Item

logger = logging.getLogger(__name__)

# Per-feed urllib timeout, in seconds. A hung publisher (dead connection,
# stalled TLS handshake, ...) must not wedge the whole run -- one feed
# timing out just means that feed contributes nothing this run.
_FEED_TIMEOUT_SECONDS = 15

# Minimum spacing, in seconds, between two fetches of the SAME host within
# one `collect()` call -- see `collect`'s per-host pacing block. Reddit's
# public `.rss` feeds are what make this necessary: live-verified from the
# production IP, requests spaced 3s apart to reddit.com got 429 on 4 of 5
# subreddits, while 45s spacing got 200 on 4 of 5 (the 5th still 429'd --
# Reddit's throttling isn't perfectly deterministic, but 45s is what turns
# "mostly blocked" into "mostly works"). A host that appears only once in
# NEWS_FEEDS -- true of every ordinary news feed today -- never triggers a
# wait at all; this only fires on a REPEAT of the same host later in the
# same feed_urls list. Five reddit.com subreddits at 45s apart adds ~3
# minutes of wall clock to a run, comfortably inside digest.service's 600s
# TimeoutStartSec.
_MIN_SECONDS_BETWEEN_SAME_HOST = 45

# Entries published within this many hours of "now" are candidates. This
# REPLACES cursors entirely (news has none, see `collect`'s docstring): the
# items table's UNIQUE(source, source_id) + INSERT..ON CONFLICT DO NOTHING
# (digest/state.py) is the idempotency layer, so an overlapping window
# across runs costs nothing -- already-seen entries are just no-ops. A 24h
# window is 4x the 6h run interval, which tolerates missed runs (a skipped
# scheduled run, a redeploy window) and late-published entries (a feed that
# backdates or slowly propagates) without ever needing to persist state.
# Entries older than this are an accepted, permanent miss.
#
# Track the run interval if it changes: this was 12h while the timer ran
# 3-hourly (homelab's myapps_digest_on_calendar). Keeping the 4x ratio, not
# the absolute 12h, is what preserves the missed-run tolerance -- at 6h,
# a 12h window would leave only 2 runs of slack. 24h also lines up with
# reddit.py's own _LOOKBACK_HOURS, which is 24 for unrelated reasons
# (Reddit's t=day window), so the two no-cursor collectors now agree.
_LOOKBACK_HOURS = 24

# Bounds per-feed work. The lookback window trims real feeds far below this
# anyway -- this is a hard backstop against a misbehaving feed that returns
# an enormous number of entries in one page.
_MAX_ENTRIES_PER_FEED = 50

# Caps the cleaned summary text handed to the summarizer prompt. The title
# is kept in full regardless -- only the (often much longer, HTML-laden)
# summary/description body is truncated.
_MAX_SUMMARY_CHARS = 1000

# Some publishers 403 urllib's default User-Agent (it identifies as
# "Python-urllib/x.y", which a fair number of feeds block outright).
_USER_AGENT = "digest-rss/1.0"

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# Matches the `/r/<sub>` path segment in a reddit.com URL. Restricted to
# subreddit-legal characters (letters, digits, underscore) rather than "up
# to the next slash" so it stops cleanly before a `.rss`/`.json` suffix or a
# sort segment glued on without a separating slash (e.g. `/r/hungary.rss`).
_REDDIT_SUBREDDIT_PATH_RE = re.compile(r"/r/([A-Za-z0-9_]+)")


def _clean_summary(raw_summary: str | None) -> str:
    """Strip HTML, unescape entities, collapse whitespace, and truncate.

    Feeds carry arbitrary publisher HTML in their summary/description field
    (embedded tags, escaped entities, styling cruft). This cleaning is for
    PROMPT-BUDGET hygiene, not security -- the digest pipeline already
    treats every item's `text` as untrusted data regardless of source (it
    is escaped again at render time, digest/emailer.py), so a raw '<script>'
    surviving here would not itself be a vulnerability. The point is just
    to keep the summarizer prompt from being bloated with markup and
    entity noise it has no use for.
    """
    if not raw_summary:
        return ""
    without_tags = _TAG_RE.sub(" ", raw_summary)
    unescaped = html.unescape(without_tags)
    collapsed = _WHITESPACE_RE.sub(" ", unescaped).strip()
    return collapsed[:_MAX_SUMMARY_CHARS]


def _entry_published_at(entry: Any) -> datetime | None:
    """The entry's publish time as a UTC datetime, or None if unknown.

    Prefers `published_parsed` (feedparser's normalized struct_time, UTC)
    over `updated_parsed`, falling back to the latter for feeds that only
    set an update time (common on Atom feeds with no distinct publish date).
    An entry with neither can't be windowed against the lookback, so the
    caller skips it entirely.
    """
    for attr in ("published_parsed", "updated_parsed"):
        value = entry.get(attr)
        if isinstance(value, struct_time):
            # struct_time from feedparser is already normalized to UTC.
            return datetime(*value[:6], tzinfo=UTC)
    return None


def _entry_link(entry: Any) -> str | None:
    """The entry's link if it's a well-formed http(s) URL, else None.

    This URL enters the email's link-provenance allowlist later
    (digest/emailer.py's render_html) -- the scheme/netloc check here is
    load-bearing, not defensive fluff: an item whose `url` isn't a genuine
    http(s) link with a network location must never reach that allowlist.
    """
    link = entry.get("link")
    if not isinstance(link, str) or not link:
        return None
    parts = urlsplit(link)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return link


def _entry_source_id(entry: Any, link: str) -> str:
    """The feed's own stable identity for this entry, falling back to its link.

    A GUID (`entry.id`) is the feed's own stable identity for the entry --
    preferred because, unlike a URL, publishers don't usually change it on
    a re-publish/edit. When absent (some feeds omit `<guid>`/`<id>` entirely),
    the link is the next best stable identifier. Either way, the
    UNIQUE(source, source_id) constraint (digest/state.py) makes a
    re-seen entry (same id or link, inside a later overlapping window) a
    silent no-op rather than a duplicate.
    """
    entry_id = entry.get("id")
    if isinstance(entry_id, str) and entry_id:
        return entry_id
    return link


def _reddit_chat_title(feed_url: str, feed_title: str | None) -> str | None:
    """The `r/<sub>` chat_title override for a reddit.com feed URL, or `feed_title` unchanged.

    Reddit's own `<title>` on a subreddit `.rss` feed is publisher copy like
    "top scoring links : hungary" -- not usable as chat_title. For any feed
    URL whose host is `reddit.com` or a subdomain of it, this instead derives
    `f"r/{sub}"` from the URL's own `/r/<sub>/` path segment (case-preserved
    exactly as it appears in the URL). This is load-bearing, not cosmetic:
    prompts/digest.md's standing Hungary rule keys on `chat_title` being
    exactly `r/hungary`, and digest/collectors/reddit.py's OAuth collector
    (PR #32) produces that identical `f"r/{subreddit}"` shape -- so a
    subreddit ingested via either path presents identically to the prompt.

    Falls back to `feed_title` (still possibly None) for a non-reddit host,
    or for a reddit.com URL with no parseable `/r/<sub>/` segment.
    """
    host = urlsplit(feed_url).hostname
    if host is None or not (host == "reddit.com" or host.endswith(".reddit.com")):
        return feed_title
    match = _REDDIT_SUBREDDIT_PATH_RE.search(urlsplit(feed_url).path)
    if match is None:
        return feed_title
    return f"r/{match.group(1)}"


def _entries_from_feed(parsed: Any, feed_url: str) -> list[Item]:
    """Extract candidate Items from one already-parsed feed, newest-first, capped.

    Per-entry extraction is defensive (missing attributes are treated as
    absent via `.get()`, never raise) but each entry is NOT individually
    wrapped in its own try/except -- feedparser normalizes hard enough that
    handling missing/malformed attributes is sufficient; an entry whose
    processing genuinely raises is a sign of something wrong with the WHOLE
    feed, which the caller's per-feed try/except already catches.
    """
    feed_title = parsed.feed.get("title")
    feed_title = feed_title if isinstance(feed_title, str) and feed_title else None
    chat_title = _reddit_chat_title(feed_url, feed_title)
    fetched_at = datetime.now(UTC).isoformat()
    cutoff = datetime.now(UTC).timestamp() - _LOOKBACK_HOURS * 3600

    items: list[Item] = []
    for entry in parsed.entries[:_MAX_ENTRIES_PER_FEED]:
        published_at = _entry_published_at(entry)
        if published_at is None:
            continue
        if published_at.timestamp() < cutoff:
            continue

        link = _entry_link(entry)
        if link is None:
            continue

        title = entry.get("title") or ""
        summary = _clean_summary(entry.get("summary"))
        text = f"{title}\n\n{summary}" if summary else title

        author = entry.get("author")
        author = author if isinstance(author, str) and author else None

        items.append(
            Item(
                source="news",
                source_id=_entry_source_id(entry, link),
                chat_id=None,
                chat_title=chat_title,
                author=author,
                text=text,
                url=link,
                fetched_at=fetched_at,
            )
        )
    return items


def _fetch_and_parse_one_feed(feed_url: str) -> list[Item]:
    """Fetch + parse one feed end to end. Raises on total failure; never partially.

    The whole fetch+parse+extract sequence is deliberately UNGUARDED here --
    the caller (`collect`) wraps this single call in the try/except that
    counts this feed as failed, so any exception anywhere in this function
    (network error, feedparser choking, an unexpected attribute shape)
    propagates up to exactly one place.

    A parse where feedparser sets `bozo` (its own "this wasn't well-formed"
    flag) AND yields zero entries is treated as a failure -- something is
    wrong enough that nothing usable came out. `bozo` WITH entries proceeds
    normally: feedparser is deliberately tolerant of malformed real-world
    feeds, and a `bozo` flag alongside successfully parsed entries is
    exactly that tolerance working as intended, not a reason to discard
    otherwise-good data.
    """
    request = urllib.request.Request(feed_url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_FEED_TIMEOUT_SECONDS) as response:
        raw_bytes = response.read()

    parsed = feedparser.parse(raw_bytes)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"feed did not parse to any entries (bozo): {feed_url}")

    return _entries_from_feed(parsed, feed_url)


def collect(feed_urls: Sequence[str]) -> CollectResult:
    """Fetch new articles from every configured feed, within the lookback window.

    No cursor axis at all (unlike telegram.py/x.py): `cursor_updates` is
    always `{}`. Idempotency comes entirely from the items table's
    UNIQUE(source, source_id) + INSERT..ON CONFLICT DO NOTHING
    (digest/state.py) -- an overlapping `_LOOKBACK_HOURS` window across runs
    just re-offers already-seen entries, which commit_new_items silently
    drops. This is a deliberate simplification over a per-feed cursor: a
    news feed has no meaningful "since" pagination contract the way a
    Telegram chat or the X notifications timeline does, and the dedup layer
    already makes a cursor's only real job (don't re-emit) redundant here.

    Empty/no `feed_urls` -> a fresh, unfailed `CollectResult()` (no-op): news
    is enabled purely by NEWS_FEEDS being non-empty (digest/config.py), so an
    empty tuple must behave as "collector not in use", not as a failure.

    Each feed URL is fetched (urllib, `_FEED_TIMEOUT_SECONDS` timeout) and
    parsed (`feedparser.parse`) independently, wrapped in its own
    try/except Exception -- one bad feed (DNS failure, HTTP error, timeout,
    a malformed response feedparser can't salvage) is logged as a WARNING
    (the feed URL is owner config, not a secret, so logging it is fine --
    but entry CONTENT is never logged) and skipped; the rest of the run
    proceeds normally.

    `failed=True` on the result ONLY when feeds were configured and EVERY
    single one of them failed -- i.e. a total outage. This deliberately
    DIVERGES from telegram.py/x.py's per-chat/pagination failure semantics
    (see module docstring): a curated feed list realistically has an
    occasionally-flaky publisher in it at any given time, and that must not
    trip the digest's ⚠ collection-failed banner, non-zero exit, or Loki
    alert -- those are reserved for "the news source is unusable this run",
    not "one of N feeds hiccupped".

    Entries are yielded newest-first per feed (feedparser's own entry order),
    capped at `_MAX_ENTRIES_PER_FEED`, with `chat_title` set to the feed's
    own title (`parsed.feed.title`), playing the same "which group" digest
    provenance role Telegram's `chat_title` plays for a channel -- except for
    a reddit.com feed URL, where `_reddit_chat_title` overrides it with
    `f"r/{sub}"` instead (see that function's docstring).

    Before each feed is fetched, if its host was already fetched EARLIER IN
    THIS SAME `collect()` call less than `_MIN_SECONDS_BETWEEN_SAME_HOST`
    seconds ago, this sleeps the remainder first (see that constant's own
    docstring for the measured numbers behind it). Tracked in a plain local
    dict of per-host last-fetch `time.monotonic()` readings -- deliberately
    NOT module state, so nothing leaks between collect() calls or between
    test runs. A host that appears only once in `feed_urls` never sleeps at
    all, so today's ordinary (one-feed-per-host) news list is unaffected;
    this only matters once multiple feed URLs share a host, as owner config
    now does for reddit.com subreddit feeds.
    """
    result = CollectResult()

    if not feed_urls:
        return result

    succeeded = 0
    last_fetch_monotonic: dict[str, float] = {}
    for feed_url in feed_urls:
        host = urlsplit(feed_url).hostname
        if host is not None:
            last_fetch = last_fetch_monotonic.get(host)
            if last_fetch is not None:
                remaining = _MIN_SECONDS_BETWEEN_SAME_HOST - (time.monotonic() - last_fetch)
                if remaining > 0:
                    time.sleep(remaining)
            last_fetch_monotonic[host] = time.monotonic()

        try:
            items = _fetch_and_parse_one_feed(feed_url)
        except Exception as exc:
            logger.warning("rss feed failed: %s (%s)", feed_url, type(exc).__name__)
            continue

        succeeded += 1
        result.items.extend(items)

    if succeeded == 0:
        result.failed = True

    return result
