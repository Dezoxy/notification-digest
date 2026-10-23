"""Reddit collector: top-of-day posts from the owner's chosen subreddits, via anonymous Atom feeds.

See CLAUDE.md and PLAN.md for the full plan. This module is synchronous
(urllib), matching rss.py/polymarket.py -- the caller (digest/main.py) runs
collectors sequentially, one at a time, so there is nothing else in flight
to block on.

Why anonymous public Atom feeds, no cookie at all (live-verified 2026-10-23)
----------------------------------------------------------------------
History: Reddit's Data Team refused the owner's OAuth API application
(2026-08-06), so app-only OAuth is permanently dead. This collector then used
the owner's logged-in `reddit_session` cookie against old.reddit.com's `.json`
endpoints. That died too: Reddit now refuses that logged-in cookie from every
Linux client (Azure, Cloudflare Workers, the homelab Mac mini -- container and
host alike) while the same cookie still works from the owner's macOS laptop on
the same public IP. Reddit fingerprints the client below the User-Agent. The
owner decided not to imitate browsers, so there is no cookie, no login and no
client spoofing in this module any more.

What is left is Reddit's ANONYMOUS public Atom feed per subreddit:
`GET {api_base}/r/{sub}/top/.rss?t=day&limit={n}` answers 200
`application/atom+xml`. Per entry: `id` (`t3_<base36>`), `title`, `link` (the
comments page), `author/name` (`/u/<name>`), `category` (`r/<sub>`), `content`
(escaped HTML, a few hundred characters) and `published`/`updated`. There is
NO score and NO comment count, so the item text carries none (the summarizer
loses that community-signal weight; accepted). `REDDIT_SESSION_COOKIE` is no
longer read anywhere; `scripts/rotate_reddit_cookie.py` is obsolete for this
collector.

Proxy (REDDIT_API_BASE / REDDIT_PROXY_KEY / REDDIT_ACCESS_CLIENT_*)
----------------------------------------------------------------------
Reddit answers Azure egress (AS8075) with `403 Blocked` (since 2026-10-21), so
production does not talk to reddit.com directly: it goes through the owner's
homelab proxy, `https://reddit-proxy.toomhorvath.com`, behind a Cloudflare
Access app that admits only a service token. That proxy's only route is
`GET /r/<sub>/top/.rss` with the query parameters `t` and `limit`. The pair
`REDDIT_ACCESS_CLIENT_ID` / `REDDIT_ACCESS_CLIENT_SECRET` is sent as
`CF-Access-Client-Id` / `CF-Access-Client-Secret`, only when both are set, and
`REDDIT_PROXY_KEY` adds `x-proxy-key` when set. All of these are secrets --
never logged. The proxy passes Reddit's `x-ratelimit-*` and `retry-after`
headers through (numeric values only), which the pacing below relies on.

Redirects are never followed (`_open`): behind Access a rejected or missing
service token is a 302 to `<team>.cloudflareaccess.com`, which urllib would
follow and misreport. A 3xx surfaces as `HTTPError` instead, and
`_describe_failure` labels a 3xx by its `Location` host and a 403 by a class
of its body (proxy key rejected, Cloudflare 1010, Reddit network block), so a
failure is attributable from one log line. Only the label is logged, never
header or body text.

User-Agent
----------------------------------------------------------------------
`_USER_AGENT` is the same descriptive, stable string this module always used.
It is honest and is not an attempt to look like a browser.

Pacing: Reddit's anonymous budget is about one request per window
----------------------------------------------------------------------
Every response carries `x-ratelimit-used`, `x-ratelimit-remaining` and
`x-ratelimit-reset` (seconds until the window resets). Anonymous callers get
roughly ONE request per window; a burst is a 429. So after every subreddit
request except the last (`_wait_seconds`):

- `x-ratelimit-remaining` < 1 and a numeric `x-ratelimit-reset`: wait
  `reset + 2` seconds;
- headers absent or unusable: wait `request_interval_seconds`
  (REDDIT_REQUEST_INTERVAL_SECONDS, default 65);
- `remaining` >= 1: no wait, the budget is not spent;
- every wait is capped at `max_wait_seconds` (REDDIT_MAX_WAIT_SECONDS,
  default 120), so a hostile or odd header cannot stall the job.

Five subreddits therefore take about five minutes, which a 6-hourly job
tolerates. The sleep is the module-level seam `_sleep` so tests never wait.

Failure semantics (mirrors x.py's hard rule for a throttled/flagged account)
----------------------------------------------------------------------
A 429, 401 or 403 on any subreddit is Reddit (or the proxy) telling us to back
off: continuing into that signal is how an IP or account gets blocked harder.
The REST of the run is aborted (`break`, never a retry; `retry-after` is only
logged) and `result.failed` is set even if earlier subreddits succeeded. Items
already collected are still returned and committed -- the state upsert is
idempotent (`UNIQUE(source, source_id)` + `INSERT ... ON CONFLICT DO NOTHING`,
digest/state.py). The next scheduled run is the retry. Any OTHER per-subreddit
error (5xx, network, unparseable feed, a subreddit gone private or renamed) is
logged and skipped, as rss.py does per feed; `failed=True` is also set when
EVERY configured subreddit failed. There is no up-front session verify: with
no session there is nothing shared to verify.

No cursor axis; a lookback window instead (mirrors rss.py exactly)
----------------------------------------------------------------------
`t=day` is Reddit's own ROLLING ranked window, re-computed on every request, so
there is no stable "since" a cursor could page through. Idempotency comes from
`UNIQUE(source, source_id)`: re-fetching the same post is a free no-op, which
only works because `source_id` is the BARE post id (`1xcj1wm`) -- the feed's
`t3_` prefix is stripped so ids match those the earlier JSON collector already
stored and nothing is duplicated across the switch. `_LOOKBACK_HOURS` (24) is
also applied client-side to each entry's `published` (falling back to
`updated`) as a belt-and-braces check; an entry with no parseable timestamp is
KEPT (fail open).

`chat_title` is the reddit-specific provenance field
----------------------------------------------------------------------
Every item's `chat_title` is `f"r/{subreddit}"` -- the same "which group/feed"
role Telegram's `chat_title` and rss.py's feed title play. This exact format is
load-bearing: prompts/digest.md's Hungary standing-section rule keys on
`chat_title == "r/hungary"` verbatim.

The feed has no stickied/NSFW flags, so the old skips for those are gone.
"""

from __future__ import annotations

import calendar
import html
import logging
import math
import re
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import feedparser

from digest.collectors.base import CollectResult
from digest.state import Item

logger = logging.getLogger(__name__)

# Per-request urllib timeout, in seconds. Matches rss.py's/polymarket.py's own
# constant of the same name/value -- a hung request must not wedge the run.
_REQUEST_TIMEOUT_SECONDS = 15

# Hard cap on the bytes read from one response. A real feed of 25 entries is
# a few tens of KiB; this only stops a misbehaving upstream from filling memory.
_MAX_FEED_BYTES = 2 * 1024 * 1024

# Sent on every request. Descriptive and honest; see module docstring.
_USER_AGENT = "linux:notification-digest:v1 (personal digest)"

# Default `api_base`: Reddit's public Atom feeds live on www.reddit.com
# (anonymous old.reddit.com answers 302). Production overrides it with the
# homelab proxy (module docstring's "Proxy" section).
_BASE_URL = "https://www.reddit.com"

# Defaults for the pacing knobs (config.py owns the env-facing ones).
_DEFAULT_REQUEST_INTERVAL_SECONDS = 65
_DEFAULT_MAX_WAIT_SECONDS = 120

# Added to `x-ratelimit-reset` so the next request lands just after the
# window rolls over rather than on its edge.
_RESET_MARGIN_SECONDS = 2

# Matches Reddit's own `t=day` ranking window -- see module docstring's "No
# cursor axis" section.
_LOOKBACK_HOURS = 24

# Caps the content excerpt folded into Item.text. The title is kept in full.
_MAX_SELFTEXT_EXCERPT_CHARS = 500

# The pacing sleep. Module-level so tests replace it and never actually wait.
_sleep = time.sleep

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_POST_ID_RE = re.compile(r"[A-Za-z0-9]+")
_COMMENTS_PATH_RE = re.compile(r"/r/[A-Za-z0-9_]+/comments/[A-Za-z0-9]+(?:/|$)")
_REDDIT_HOSTS = frozenset({"www.reddit.com", "reddit.com"})
_RETRY_AFTER_RE = re.compile(r"\d{1,6}")


@dataclass(frozen=True)
class _ParsedPost:
    """One Reddit post's fields, extracted from one Atom entry.

    Deliberately flat -- the only shape `collect`'s filtering and
    `_build_item` consume from `_parse_entry`. `published` is None when the
    entry carried no parseable timestamp (the window check then fails open).
    """

    post_id: str
    title: str
    author: str | None
    url: str
    excerpt: str
    published: float | None


def _clean_text(raw: str) -> str:
    """Strip HTML tags, unescape entities and collapse whitespace.

    Same approach as rss.py's `_clean_summary`: for prompt-budget hygiene, not
    security (every item's `text` is treated as untrusted data downstream and
    escaped again at render time).
    """
    without_tags = _TAG_RE.sub(" ", raw)
    return _WHITESPACE_RE.sub(" ", html.unescape(without_tags)).strip()


def _entry_timestamp(entry: Any) -> float | None:
    """The entry's `published` (else `updated`) as a UTC epoch, or None."""
    for attr in ("published_parsed", "updated_parsed"):
        value = entry.get(attr)
        if isinstance(value, time.struct_time):
            return float(calendar.timegm(value))
    return None


def _safe_url(link: Any, subreddit: str, post_id: str) -> str:
    """The entry link if it is a Reddit comments page over https, else a built one.

    This URL enters the email's link-provenance allowlist later
    (digest/emailer.py), so anything that is not https on reddit.com with a
    `/r/<sub>/comments/<id>` path is replaced by the URL built from the id.
    """
    if isinstance(link, str):
        parts = urlsplit(link)
        if (
            parts.scheme == "https"
            and parts.netloc.lower() in _REDDIT_HOSTS
            and _COMMENTS_PATH_RE.match(parts.path)
        ):
            return link
    return f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"


def _parse_entry(entry: Any, subreddit: str) -> _ParsedPost | None:
    """Parse one feedparser entry into a `_ParsedPost`, or None.

    Returns None (never raises) for an entry without a usable id or title;
    `collect` counts skips and never lets one malformed entry abort the
    subreddit (mirrors polymarket.py's `_parse_binary_market` contract).
    """
    raw_id = entry.get("id")
    if not isinstance(raw_id, str):
        return None
    post_id = raw_id.strip().removeprefix("t3_")
    if not _POST_ID_RE.fullmatch(post_id):
        return None

    title = entry.get("title")
    title = _clean_text(title) if isinstance(title, str) else ""
    if not title:
        return None

    author = entry.get("author")
    author = author.strip().removeprefix("/u/").strip() if isinstance(author, str) else ""

    content = entry.get("content")
    raw_html = ""
    if isinstance(content, list) and content and isinstance(content[0].get("value"), str):
        raw_html = content[0]["value"]
    elif isinstance(entry.get("summary"), str):
        raw_html = entry["summary"]

    return _ParsedPost(
        post_id=post_id,
        title=title,
        author=author or None,
        url=_safe_url(entry.get("link"), subreddit, post_id),
        excerpt=_clean_text(raw_html)[:_MAX_SELFTEXT_EXCERPT_CHARS],
        published=_entry_timestamp(entry),
    )


def _build_item(post: _ParsedPost, subreddit: str, fetched_at: str) -> Item:
    """Build the digest Item for one confirmed, in-window Reddit post.

    See module docstring's "`chat_title` is the reddit-specific provenance
    field" section for why the exact `f"r/{subreddit}"` format is load-bearing.
    """
    return Item(
        source="reddit",
        source_id=post.post_id,
        chat_id=None,
        chat_title=f"r/{subreddit}",
        author=post.author,
        text=f"{post.title} {post.excerpt}".strip(),
        url=post.url,
        fetched_at=fetched_at,
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so a 3xx surfaces as `urllib.error.HTTPError`."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(request: urllib.request.Request) -> Any:
    """Open `request` without following redirects (module docstring's "Proxy" section).

    The single network seam of this module; tests replace it.
    """
    return _OPENER.open(request, timeout=_REQUEST_TIMEOUT_SECONDS)


def _request_headers(
    proxy_key: str | None = None,
    access_client_id: str | None = None,
    access_client_secret: str | None = None,
) -> dict[str, str]:
    """Build the User-Agent (+ proxy/Access) headers every request sends.

    Never a Cookie. `x-proxy-key` is added only when `proxy_key` is set, and
    the Cloudflare Access pair only when BOTH halves are set; all are secrets
    and are never logged.
    """
    headers = {"User-Agent": _USER_AGENT}
    if proxy_key:
        headers["x-proxy-key"] = proxy_key
    if access_client_id and access_client_secret:
        headers["CF-Access-Client-Id"] = access_client_id
        headers["CF-Access-Client-Secret"] = access_client_secret
    return headers


_REASON_UNSAFE_RE = re.compile(r"[^A-Za-z0-9 ._-]")
_REASON_MAX_CHARS = 40


_FORBIDDEN_JSON_RE = re.compile(r'\{\s*"error"\s*:\s*"forbidden"\s*\}')
_ERROR_BODY_MAX_BYTES = 2048


def _classify_forbidden_body(exc: urllib.error.HTTPError) -> str:
    """Label a 403 by (at most) the first 2 KiB of its body; never returns body text."""
    try:
        body = exc.read(_ERROR_BODY_MAX_BYTES) if exc.fp is not None else b""
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return "unclassified"
    if _FORBIDDEN_JSON_RE.search(text):
        return "proxy rejected key"
    if "error code: 1010" in text:
        return "cloudflare 1010 block"
    if "blocked by network security" in text.lower():
        return "reddit network block"
    return "unclassified"


def _classify_redirect(exc: urllib.error.HTTPError) -> str:
    """Label a 3xx by the HOST of its `Location` header only; never the path or query."""
    location = exc.headers.get("Location") if exc.headers is not None else None
    if not location:
        return "redirect without Location"
    host = (urlsplit(location).hostname or "").lower()
    if not host:
        return "relative redirect"
    if host.endswith(".cloudflareaccess.com"):
        return "access login redirect (service token rejected or missing)"
    return f"redirect to {_REASON_UNSAFE_RE.sub('', host)[:_REASON_MAX_CHARS]}"


def _describe_failure(exc: Exception) -> str:
    """Log-safe description of a per-subreddit failure; never a secret.

    `HTTPError` -> status code + sanitized server reason phrase, so a 403
    "Blocked" is distinguishable from a 429 or a 5xx; a 3xx adds a label from
    the `Location` host and a 403 a label from its body (module docstring's
    "Proxy" section). `ValueError` -> the feed did not parse. Anything else ->
    the type name only. Only fixed labels and the sanitized host/reason are
    used: never the message, other headers, body text or url.
    """
    if isinstance(exc, urllib.error.HTTPError):
        reason = _REASON_UNSAFE_RE.sub("", str(exc.reason))[:_REASON_MAX_CHARS]
        text = f"HTTPError {exc.code} {reason}"
        if 300 <= exc.code < 400:
            return f"{text} ({_classify_redirect(exc)})"
        if exc.code == 403:
            return f"{text} ({_classify_forbidden_body(exc)})"
        return text
    if isinstance(exc, ValueError):
        return f"{type(exc).__name__} (unparseable feed)"
    return type(exc).__name__


def _header_number(headers: Any, name: str) -> float | None:
    """A response header as a finite, non-negative float, or None."""
    if headers is None:
        return None
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _wait_seconds(headers: Any, request_interval_seconds: float, max_wait_seconds: float) -> float:
    """How long to wait before the next subreddit request (module docstring's "Pacing").

    `remaining` < 1 with a numeric `reset` -> `reset + 2`; `remaining` >= 1 ->
    0; anything absent or unusable -> the configured interval. Always capped
    at `max_wait_seconds`.
    """
    remaining = _header_number(headers, "x-ratelimit-remaining")
    reset = _header_number(headers, "x-ratelimit-reset")
    if remaining is None:
        wait = request_interval_seconds
    elif remaining >= 1:
        wait = 0.0
    elif reset is None:
        wait = request_interval_seconds
    else:
        wait = reset + _RESET_MARGIN_SECONDS
    return min(wait, max_wait_seconds)


def _fetch_subreddit_feed(
    subreddit: str,
    posts_per_sub: int,
    *,
    api_base: str = _BASE_URL,
    proxy_key: str | None = None,
    access_client_id: str | None = None,
    access_client_secret: str | None = None,
) -> tuple[list[Any], Any]:
    """One GET to `{api_base}/r/{subreddit}/top/.rss`, returning (entries, response headers).

    `t=day` + `limit={posts_per_sub}` -- the only two query parameters the
    homelab proxy accepts (module docstring's "No cursor axis" section for
    why `t=day`). The response is read up to `_MAX_FEED_BYTES` and parsed with
    feedparser, as rss.py does.

    Raises on any failure (network error, non-2xx status, oversized or
    unparseable body) -- deliberately unguarded: the caller (`collect`) wraps
    this in the try/except that counts this ONE subreddit as failed, with a
    special case for `urllib.error.HTTPError` 401/403/429. A feedparser `bozo`
    parse with zero entries is a failure; `bozo` with entries proceeds
    (feedparser's tolerance working as intended, same as rss.py).
    """
    url = f"{api_base}/r/{subreddit}/top/.rss?t=day&limit={posts_per_sub}"
    headers = _request_headers(proxy_key, access_client_id, access_client_secret)
    request = urllib.request.Request(url, headers=headers)
    with _open(request) as response:
        raw_bytes = response.read(_MAX_FEED_BYTES + 1)
        response_headers = getattr(response, "headers", None)
    if len(raw_bytes) > _MAX_FEED_BYTES:
        raise ValueError("reddit feed response too large")
    parsed = feedparser.parse(raw_bytes)
    if parsed.bozo and not parsed.entries:
        raise ValueError("reddit feed did not parse to any entries")
    return list(parsed.entries), response_headers


def collect(
    subreddits: Sequence[str],
    posts_per_sub: int,
    *,
    api_base: str = _BASE_URL,
    proxy_key: str | None = None,
    access_client_id: str | None = None,
    access_client_secret: str | None = None,
    request_interval_seconds: float = _DEFAULT_REQUEST_INTERVAL_SECONDS,
    max_wait_seconds: float = _DEFAULT_MAX_WAIT_SECONDS,
) -> CollectResult:
    """Fetch today's top posts from every configured subreddit, anonymously.

    `api_base` / `proxy_key` / `access_client_*` select the proxy (module
    docstring's "Proxy" section); the defaults talk to www.reddit.com directly.
    Redirects are never followed. No secret is ever logged, and no Cookie is
    ever sent.

    Empty `subreddits` -> a fresh, unfailed `CollectResult()` with no network
    calls at all, mirroring rss.py's `collect(feed_urls=())` no-op.

    Each subreddit is fetched (`_fetch_subreddit_feed`) independently, wrapped
    in its own try/except. HTTP 401/403/429 abort the REST of the run and set
    `failed=True` (module docstring's "Failure semantics"); no retry, and
    `retry-after` is only logged. Any other exception is logged as a WARNING
    (the subreddit name is owner config, not a secret) and skipped. Logged
    failures go through `_describe_failure`: status code and sanitized reason,
    or the exception's type name -- never a message that might embed a secret.
    `failed=True` is also set when subreddits were configured and EVERY one
    failed.

    After every subreddit request except the last, `_sleep` waits
    `_wait_seconds` (module docstring's "Pacing"), including after a skipped
    failure, whose error response carries the same headers.

    Entries are parsed (`_parse_entry`; a malformed one is skipped, never
    fatal) and any whose `published` falls outside `_LOOKBACK_HOURS` is
    dropped; a missing timestamp is kept. Survivors, at most `posts_per_sub`
    per subreddit, become Items via `_build_item`.

    `cursor_updates` is always `{}` (no cursor axis; see module docstring).
    """
    result = CollectResult()

    if not subreddits:
        return result

    fetched_at = datetime.now(UTC).isoformat()
    cutoff = datetime.now(UTC).timestamp() - _LOOKBACK_HOURS * 3600

    succeeded = 0
    skipped = 0
    last_index = len(subreddits) - 1
    for index, subreddit in enumerate(subreddits):
        response_headers: Any = None
        try:
            entries, response_headers = _fetch_subreddit_feed(
                subreddit,
                posts_per_sub,
                api_base=api_base,
                proxy_key=proxy_key,
                access_client_id=access_client_id,
                access_client_secret=access_client_secret,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                retry_after = exc.headers.get("retry-after") if exc.headers is not None else None
                retry_after = retry_after.strip() if isinstance(retry_after, str) else ""
                extra = (
                    f"; retry-after {retry_after}s"
                    if _RETRY_AFTER_RE.fullmatch(retry_after)
                    else ""
                )
                logger.warning(
                    "reddit told us to back off at r/%s (%s)%s; aborting remaining subreddits",
                    subreddit,
                    _describe_failure(exc),
                    extra,
                )
                result.failed = True
                break
            logger.warning(
                "reddit subreddit fetch failed: r/%s (%s)", subreddit, _describe_failure(exc)
            )
            response_headers = exc.headers
        except Exception as exc:
            logger.warning(
                "reddit subreddit fetch failed: r/%s (%s)", subreddit, _describe_failure(exc)
            )
        else:
            succeeded += 1
            for entry in entries[:posts_per_sub]:
                post = _parse_entry(entry, subreddit)
                if post is None:
                    skipped += 1
                    continue
                if post.published is not None and post.published < cutoff:
                    skipped += 1
                    continue
                result.items.append(_build_item(post, subreddit, fetched_at))

        if index < last_index:
            wait = _wait_seconds(response_headers, request_interval_seconds, max_wait_seconds)
            if wait > 0:
                _sleep(wait)

    if skipped:
        logger.debug("reddit: skipped %d malformed/stale posts", skipped)

    if succeeded == 0:
        result.failed = True

    return result
