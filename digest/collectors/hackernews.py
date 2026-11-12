"""Hacker News collector: front-page stories via Algolia HN Search, Firebase fallback.

See CLAUDE.md and PLAN.md for the full plan. This module is synchronous
(urllib), matching rss.py/polymarket.py -- the caller (digest/main.py) runs
collectors sequentially, one at a time, so there is nothing else in flight
to block on.

Why Algolia primary, Firebase as fallback (deliberate choice)
----------------------------------------------------------------------
Algolia's HN Search index (`hn.algolia.com/api/v1/search`) returns the
front page in ONE request via its `tags=front_page` filter (title, url,
points, num_comments, author, objectID, created_at_i, all in the same
hit). Hacker News' own Firebase API (`hacker-news.firebaseio.com`) exposes
the front page only as a bare list of item ids (`/v0/topstories.json`),
needing one follow-up request PER STORY (`/v0/item/<id>.json`): 1 + top_n
requests against a free public API. That is why Firebase is not the
primary. It is the fallback because it is the OFFICIAL source and an
independent host: from Azure, Algolia intermittently stalls for the full
timeout, while Firebase answered 12/12 probes at ~0.27 s median. No auth,
no API key, no documented rate limit for reasonable personal use on
either. `_firebase_item_to_hit` maps a Firebase item onto the Algolia hit
shape, so `_parse_hit`/`_build_item` are shared and a story is the SAME
Item (same `source_id`, deduped by `UNIQUE(source, source_id)`) whichever
API served it.

Response shape (live-verified against the real endpoint)
----------------------------------------------------------------------
`GET {_API_BASE}?tags=front_page&hitsPerPage=<n>` returns a JSON object with
a top-level `hits` array; each hit is a flat object carrying (among other
fields this module ignores) `objectID` (the story's HN item id, as a
string), `title`, `url` (a string, or ABSENT/null for a text-only post --
"Ask HN", "Show HN" without a link, a poll), `points`, `num_comments`,
`author` (the submitter's username), and `created_at_i` (a Unix timestamp,
seconds). `_parse_hit` treats every field defensively (never raises) --
`objectID`/`title`/`created_at_i` are required (a hit missing any of them is
discarded as unparseable), `url`/`author` are optional, `points`/
`num_comments` default to 0 on anything unparsable: a malformed field
should never disqualify an otherwise-good hit, and a missing salience
number is display-only, not something that should throw away real content.

Discussion-page URL, not the article URL (deliberate)
----------------------------------------------------
`Item.url` is always the HN discussion page
(`f"https://news.ycombinator.com/item?id={objectID}"`), never the story's
own `url` field. Two reasons: the discussion page links out to the article
itself (nothing is lost) AND carries the community thread the digest's
"[score N, M comments]" salience convention is about. It is also the STABLE link: an
external article URL can rot (paywalled, deleted, moved) while
`news.ycombinator.com/item?id=...` never does. The article URL, when
present, is instead folded INTO `Item.text` (see `_build_item`) so the
summarizer prompt still knows what the story is actually about -- an
Ask/Show-HN text post simply omits that line, since it never had one.

Score/comments ride IN the item text, not a separate field
----------------------------------------------------------------------
`_build_item` folds `[score N, M comments]` directly into `Item.text`
rather than adding new columns to the shared `Item`/`items` schema: it lets
the summarizer prompt (prompts/digest.md) weight a story by community signal
with no changes to digest/state.py at all. An HN front-page hit has no
body text of its own to append -- so the shape here is `title`, then (when
present) an explicit `links to: <url>` line, then the bracketed salience
marker, each separated by a blank line for the summarizer's own
readability.

`chat_title` is the hackernews-specific provenance field
----------------------------------------------------------------------
Every item's `chat_title` is the fixed literal `"Hacker News"` -- playing
the same "which group/feed did this come from" role Telegram's `chat_title`
and rss.py's feed-title each play for their own sources
(prompts/digest.md's "News interest filter" section recognizes an HN item
by this exact value, alongside `source == "hackernews"`).

No cursor axis; a lookback window instead (mirrors rss.py)
----------------------------------------------------------
The Algolia front-page index is a ROLLING view of "whatever HN's own ranking
currently has on the front page", re-computed fresh on every request -- not
a monotonic timeline a cursor could meaningfully page through (a story's
rank, or presence at all, can change between runs as votes/flags shift), so
there is no stable "since" pagination contract to track, exactly like
rss.py's feeds have none (see its module docstring for the identical
reasoning). Idempotency instead
comes entirely from `UNIQUE(source, source_id)` +
`INSERT ... ON CONFLICT DO NOTHING` (digest/state.py): re-fetching the same
story across overlapping runs is a free no-op. `_LOOKBACK_HOURS` (24) is
applied client-side as a defensive filter on each hit's own `created_at_i`
-- belt-and-braces against the front-page index returning something older
than a genuinely fresh run should still be reporting (the same value as
rss.py's `_LOOKBACK_HOURS`, for consistency across the digest's no-cursor
collectors).

Failure semantics -- Algolia primary, Firebase fallback
----------------------------------------------------------------------
Primary: one Algolia request (`_fetch_front_page`), plus at most one retry
after a TRANSIENT failure (timeout, connection error, HTTP 5xx), made after
a 3 s pause (`_RETRY_DELAY_SECONDS`). Non-transient failures (HTTP 4xx,
non-JSON body, a body missing a `hits` array) are not retried.

Fallback: ANY failure that survives that policy triggers
`_fetch_firebase_front_page`: one `topstories` request, then one request
per story id (1 + top_n, sequential, no per-item retry). A transient
failure on an item means Firebase itself is stalling, so fetching STOPS
there (bounding the worst case) and the items already fetched are used.
Any other per-item failure (HTTP 4xx, bad JSON) skips just that item. Items
that are null, not a `story`, dead or deleted are skipped silently (not a
failure). Only if zero hits were obtained AND at least one item request
failed is the fallback itself a failure; a topstories failure is always one.

If the fallback fails too, `result.failed = True` and the collector returns
no items. Still no retry loop beyond the above: the next scheduled run is
the retry. Each individual hit is parsed defensively and skipped (never
fatal) on its own malformed shape, because a single bad hit inside an
otherwise-successful response is routine, not a sign the whole request
failed.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from digest.collectors.base import CollectResult
from digest.state import Item

logger = logging.getLogger(__name__)

# Single-request timeout, in seconds. Matches rss.py's/polymarket.py's own
# constant of the same name/value -- a hung request must not wedge the whole
# run, it just means this collector contributes nothing (and the run is
# marked failed, see module docstring's "Failure semantics") this time.
_REQUEST_TIMEOUT_SECONDS = 15

# Pause before the single retry after a transient failure. One extra GET, and
# only after a stall: worst case is ~15 + 3 + 15 s against a ~5-minute run.
_RETRY_DELAY_SECONDS = 3

# Algolia's public HN Search index. No auth, no API key -- see module
# docstring's "Why Algolia" section.
_API_BASE = "https://hn.algolia.com/api/v1/search"

# Official Hacker News API, used only when Algolia fails -- see module
# docstring's "Why Algolia primary, Firebase as fallback" section.
_FIREBASE_BASE = "https://hacker-news.firebaseio.com/v0"

# Sent on every request. Not verified to be REQUIRED (unlike
# polymarket.py's own _USER_AGENT, which is a live-verified Cloudflare
# Browser-Integrity-Check requirement) -- this is just a stable, honest
# product identifier, matching rss.py's own precautionary rationale (some
# hosts reject urllib's default "Python-urllib/x.y" signature outright).
_USER_AGENT = "digest-hackernews/1.0"

# Matches the front-page index's own "currently on the front page" scope --
# see module docstring's "No cursor axis" section for why this replaces a
# cursor entirely rather than supplementing one. Same value as rss.py's
# own _LOOKBACK_HOURS, for consistency across this digest's no-cursor
# collectors.
_LOOKBACK_HOURS = 24

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class _ParsedHit:
    """One HN front-page hit's fields, extracted from one raw Algolia hit object.

    Deliberately flat -- the only shape `collect`'s filtering and
    `_build_item` consume from `_parse_hit`. `url` is the STORY's own link
    (may be None for a text-only Ask/Show HN post) -- never confused with
    the discussion-page URL `_build_item` actually puts on the Item itself
    (see module docstring's "Discussion-page URL" section).
    """

    object_id: str
    title: str
    url: str | None
    author: str | None
    points: int
    num_comments: int
    created_at_i: float


def _clean_title(raw: str) -> str:
    """Collapse whitespace/newline runs in an HN title field.

    HN titles are plain text (no HTML, unlike rss.py's feed summaries), so
    this is purely defensive -- cheap insurance against a stray embedded
    newline/tab (prompt-budget hygiene), applied to a field that
    essentially never needs it in practice.
    """
    return _WHITESPACE_RE.sub(" ", raw).strip()


def _parse_hit(raw: Any) -> _ParsedHit | None:
    """Parse one raw Algolia hit object into a `_ParsedHit`, or None.

    Returns None (never raises) for anything that isn't a well-formed
    front-page hit: a non-dict entry, or a missing/blank `objectID`/`title`,
    or an unparseable `created_at_i` (required for the freshness filter --
    see module docstring's "No cursor axis" section; a hit this module can't
    date can't be windowed, so it can't be trusted at all). Every check
    fails open into `None` rather than propagating -- `collect`'s caller
    counts skips, it never lets one malformed hit abort the whole run.

    `points`/`num_comments` are defensively coalesced to 0 on anything
    unparsable -- they are a display/salience signal in the item text
    (module docstring's "Score/comments ride IN the item text"), not
    something that should disqualify an otherwise valid hit. `url`/`author`
    are optional and simply become None when absent or malformed -- an
    Ask/Show-HN text post has no `url` by design (module docstring's
    "Discussion-page URL" section), not a parse failure.
    """
    if not isinstance(raw, dict):
        return None

    object_id = raw.get("objectID")
    if not isinstance(object_id, str) or not object_id:
        return None

    title = raw.get("title")
    if not isinstance(title, str) or not title:
        return None

    try:
        created_at_i = float(raw.get("created_at_i"))
    except (TypeError, ValueError):
        return None

    url = raw.get("url")
    url = url if isinstance(url, str) and url else None

    author = raw.get("author")
    author = author if isinstance(author, str) and author else None

    try:
        points = int(raw.get("points", 0))
    except (TypeError, ValueError):
        points = 0

    try:
        num_comments = int(raw.get("num_comments", 0))
    except (TypeError, ValueError):
        num_comments = 0

    return _ParsedHit(
        object_id=object_id,
        title=title,
        url=url,
        author=author,
        points=points,
        num_comments=num_comments,
        created_at_i=created_at_i,
    )


def _build_item(hit: _ParsedHit, fetched_at: str) -> Item:
    """Build the digest Item for one confirmed, in-window HN front-page hit.

    See module docstring's "Discussion-page URL", "Score/comments ride IN
    the item text", and "`chat_title` is the hackernews-specific provenance
    field" sections for why the URL choice, text shape, and fixed
    `chat_title` are all load-bearing, not stylistic choices. The article
    URL (`hit.url`) is folded into the text as a `links to: <url>` line only
    when present -- an Ask/Show-HN text post (no `url`) simply omits that
    line, leaving title directly followed by the salience marker.
    """
    title = _clean_title(hit.title)
    lines = [title]
    if hit.url:
        lines.append(f"links to: {hit.url}")
    lines.append(f"[score {hit.points}, {hit.num_comments} comments]")
    text = "\n\n".join(lines)
    return Item(
        source="hackernews",
        source_id=hit.object_id,
        chat_id=None,
        chat_title="Hacker News",
        author=hit.author,
        text=text,
        url=f"https://news.ycombinator.com/item?id={hit.object_id}",
        fetched_at=fetched_at,
    )


def _fetch_front_page(top_n: int) -> list[Any]:
    """One GET to `{_API_BASE}?tags=front_page&hitsPerPage={top_n}`, returning the raw hits.

    Raises on any failure (network error, non-2xx status, non-JSON body, a
    body missing a `hits` array) -- deliberately unguarded here, matching
    polymarket.py's `_fetch_markets`: the caller (`collect`) wraps this
    single call in the try/except that sets `failed=True` for the whole run
    (module docstring's "Failure semantics" -- there is only one request in
    this collector, so any failure here is the whole run's signal).
    """
    url = f"{_API_BASE}?tags=front_page&hitsPerPage={top_n}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        raw_bytes = response.read()
    data = json.loads(raw_bytes)
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list):
        raise ValueError("hackernews search response missing hits")
    return hits


def _is_transient(exc: Exception) -> bool:
    """True for failures worth one retry: timeout, connection error, HTTP 5xx.

    `HTTPError` subclasses `URLError`, so it is checked first: a 4xx is a
    definite answer from the server and is never retried.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(exc, (TimeoutError, urllib.error.URLError))


def _fetch_front_page_with_retry(top_n: int) -> list[Any]:
    """`_fetch_front_page`, retried once after a transient failure.

    Since 2026-11-03 the single Algolia request intermittently hits the full
    `_REQUEST_TIMEOUT_SECONDS` from Azure egress (TimeoutError), failing the
    whole run and firing the Sev2 `notification-digest-failures` alert even
    though the digest was delivered. The same request from elsewhere answers
    in under half a second, so this is a transient stall, not a block.

    A transient failure (see `_is_transient`) is logged by type name only,
    followed by a `_RETRY_DELAY_SECONDS` pause and exactly one more attempt.
    Non-transient failures are re-raised immediately. Raises whatever the
    final attempt raised; `collect` still owns the `failed=True` mapping.
    """
    try:
        return _fetch_front_page(top_n)
    except Exception as exc:
        if not _is_transient(exc):
            raise
        logger.warning(
            "hackernews fetch failed: %s, retrying once in %ss",
            type(exc).__name__,
            _RETRY_DELAY_SECONDS,
        )
    time.sleep(_RETRY_DELAY_SECONDS)
    return _fetch_front_page(top_n)


def _firebase_get_json(path: str) -> Any:
    """GET `{_FIREBASE_BASE}/{path}` and return the decoded JSON body."""
    request = urllib.request.Request(
        f"{_FIREBASE_BASE}/{path}", headers={"User-Agent": _USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def _firebase_item_to_hit(raw: Any) -> dict[str, Any] | None:
    """Map a Firebase item onto the Algolia hit shape `_parse_hit` consumes.

    Returns None for anything that is not a live story: a non-dict (Firebase
    answers JSON `null` for a missing id), a `type` other than "story" (job,
    poll, comment), or a `dead`/`deleted` item. `url` is simply absent for
    Ask/Show HN text posts, which `_parse_hit` already treats as optional.
    """
    if not isinstance(raw, dict) or raw.get("type") != "story":
        return None
    if raw.get("dead") or raw.get("deleted") or raw.get("id") is None:
        return None
    return {
        "objectID": str(raw["id"]),
        "title": raw.get("title"),
        "url": raw.get("url"),
        "author": raw.get("by"),
        "points": raw.get("score"),
        "num_comments": raw.get("descendants"),
        "created_at_i": raw.get("time"),
    }


def _fetch_firebase_front_page(top_n: int) -> list[Any]:
    """Fallback: front page from the official Firebase API, as Algolia-shaped hits.

    One `topstories` GET (raises on failure or a non-list body), then one GET
    per id for the first `top_n` ids, sequentially. A transient item failure
    (see `_is_transient`) means Firebase itself is stalling: stop fetching
    (bounds the worst case; no per-item retry) and keep what we have. Any
    other per-item failure skips that item. If no hit was obtained and at
    least one item request failed, raise (systemic); items that merely mapped
    to None (jobs, dead, deleted) are not a failure.
    """
    ids = _firebase_get_json("topstories.json")
    if not isinstance(ids, list):
        raise ValueError("hackernews topstories response is not a list")

    hits: list[Any] = []
    last_error: Exception | None = None
    for item_id in ids[:top_n]:
        try:
            raw = _firebase_get_json(f"item/{item_id}.json")
        except Exception as exc:
            last_error = exc
            if _is_transient(exc):
                break
            continue
        hit = _firebase_item_to_hit(raw)
        if hit is not None:
            hits.append(hit)

    if not hits and last_error is not None:
        raise last_error
    return hits


def collect(top_n: int) -> CollectResult:
    """Fetch the current HN front page (up to `top_n` stories) within the lookback window.

    Algolia first (`_fetch_front_page_with_retry`); on ANY failure of it,
    falls back to the official Firebase API (`_fetch_firebase_front_page`).
    If that fails too, sets `result.failed = True` and returns immediately
    with no items -- see module docstring's "Failure semantics". One INFO
    line says which source served the run. Only the exception's TYPE NAME is
    logged, consistent with every other collector-crash log line in this
    codebase.

    Each raw hit is parsed (`_parse_hit`; a malformed entry is skipped,
    never fatal) and then filtered: any hit whose own `created_at_i` falls
    outside `_LOOKBACK_HOURS` is silently dropped (a defensive re-check of
    the front-page index's own "currently on the front page" scope, see
    module docstring's "No cursor axis" section). Survivors become Items via
    `_build_item`.

    `cursor_updates` is always `{}` (via the reused `CollectResult` -- see
    module docstring for why this collector has no cursor axis at all).
    """
    result = CollectResult()
    fetched_at = datetime.now(UTC).isoformat()
    cutoff = datetime.now(UTC).timestamp() - _LOOKBACK_HOURS * 3600

    source = "algolia"
    try:
        raw_hits = _fetch_front_page_with_retry(top_n)
    except Exception as exc:
        logger.warning(
            "hackernews algolia failed: %s; falling back to firebase", type(exc).__name__
        )
        source = "firebase"
        try:
            raw_hits = _fetch_firebase_front_page(top_n)
        except Exception as fallback_exc:
            logger.warning("hackernews fetch failed: %s", type(fallback_exc).__name__)
            return CollectResult(failed=True)
    logger.info("hackernews source=%s hits=%d", source, len(raw_hits))

    skipped = 0
    for raw in raw_hits:
        hit = _parse_hit(raw)
        if hit is None:
            skipped += 1
            continue
        if hit.created_at_i < cutoff:
            skipped += 1
            continue
        result.items.append(_build_item(hit, fetched_at))

    if skipped:
        logger.debug("hackernews: skipped %d malformed/stale hits", skipped)

    return result
