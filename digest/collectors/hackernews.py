"""Hacker News collector: front-page stories via the public Algolia HN Search API.

See CLAUDE.md and PLAN.md for the full plan. This module is synchronous
(urllib), matching rss.py/polymarket.py -- the caller (digest/main.py) runs
collectors sequentially, one at a time, so there is nothing else in flight
to block on.

Why Algolia, not the official Firebase API (deliberate choice)
----------------------------------------------------------------------
Hacker News' own Firebase API (`hacker-news.firebaseio.com`) exposes the
front page only as a bare list of item ids (`/v0/topstories.json`), which
would need one follow-up request PER STORY to resolve into anything usable
-- 15-30 requests every run for a single collector, against a free public
API, purely to reconstruct what Algolia's HN Search index
(`hn.algolia.com/api/v1/search`) already returns in ONE request via its
`tags=front_page` filter (title, url, points, num_comments, author,
objectID, created_at_i, all in the same hit). No auth, no API key, no rate
limit documented for reasonable personal use -- see module docstring's
"Failure semantics" for what "one request per run" buys this module.

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
`num_comments` default to 0 on anything unparsable, mirroring
digest/collectors/reddit.py's `_parse_post` contract for the exact same
reasons: a malformed field should never disqualify an otherwise-good hit,
and a missing salience number is display-only, not something that should
throw away real content.

Discussion-page URL, not the article URL (deliberate, matches reddit.py)
----------------------------------------------------------------------
`Item.url` is always the HN discussion page
(`f"https://news.ycombinator.com/item?id={objectID}"`), never the story's
own `url` field. Two reasons: the discussion page links out to the article
itself (nothing is lost) AND carries the community thread the digest's
"[score N, M comments]" salience convention is about -- exactly the value
digest/collectors/reddit.py's own `permalink`-based URL provides for a
Reddit post, not the linked article. It is also the STABLE link: an
external article URL can rot (paywalled, deleted, moved) while
`news.ycombinator.com/item?id=...` never does. The article URL, when
present, is instead folded INTO `Item.text` (see `_build_item`) so the
summarizer prompt still knows what the story is actually about -- an
Ask/Show-HN text post simply omits that line, since it never had one.

Score/comments ride IN the item text, not a separate field (matches reddit.py)
----------------------------------------------------------------------
`_build_item` folds `[score N, M comments]` directly into `Item.text`
rather than adding new columns to the shared `Item`/`items` schema -- the
identical convention digest/collectors/reddit.py's own module docstring
documents ("Score/comments ride IN the item text") for the identical
reason: it lets the summarizer prompt (prompts/digest.md) weight a story by
community signal with no changes to digest/state.py at all. Unlike
reddit.py's inline `"{title} [score N, M comments] {selftext}"` (a subreddit
post commonly carries a body worth reading alongside the number), an HN
front-page hit has no body text of its own to append -- so the shape here is
`title`, then (when present) an explicit `links to: <url>` line, then the
bracketed salience marker, each separated by a blank line for the
summarizer's own readability, rather than reddit.py's single-space inline
run-on.

`chat_title` is the hackernews-specific provenance field
----------------------------------------------------------------------
Every item's `chat_title` is the fixed literal `"Hacker News"` -- playing
the same "which group/feed did this come from" role Telegram's `chat_title`,
rss.py's feed-title, and reddit.py's `f"r/{subreddit}"` each play for their
own sources (prompts/digest.md's "News interest filter" section recognizes
an HN item by this exact value, alongside `source == "hackernews"`).

No cursor axis; a lookback window instead (mirrors reddit.py/rss.py exactly)
----------------------------------------------------------------------
The Algolia front-page index is a ROLLING view of "whatever HN's own ranking
currently has on the front page", re-computed fresh on every request -- not
a monotonic timeline a cursor could meaningfully page through (a story's
rank, or presence at all, can change between runs as votes/flags shift), so
there is no stable "since" pagination contract to track, exactly like
rss.py's feeds and reddit.py's `t=day` ranking have none (see either
module's own docstring for the identical reasoning). Idempotency instead
comes entirely from `UNIQUE(source, source_id)` +
`INSERT ... ON CONFLICT DO NOTHING` (digest/state.py): re-fetching the same
story across overlapping runs is a free no-op. `_LOOKBACK_HOURS` (24) is
applied client-side as a defensive filter on each hit's own `created_at_i`
-- belt-and-braces against the front-page index returning something older
than a genuinely fresh run should still be reporting (the same posture
reddit.py's own `_LOOKBACK_HOURS` takes against `t=day`, and the same value,
for consistency across the digest's no-cursor collectors).

Failure semantics -- one request, one signal (mirrors polymarket.py)
----------------------------------------------------------------------
There is exactly one request per run (`_fetch_front_page`) -- unlike
rss.py's/reddit.py's per-feed/per-subreddit list, there is no "one of many"
to be fault-tolerant about. ANY failure of that single request (network
error, non-2xx status, non-JSON body, a body missing a `hits` array) sets
`result.failed = True` and returns immediately with no items -- the same
posture polymarket.py's own single `{base}/markets` request takes (see that
module's own docstring, "Failure semantics"), and the same "no retry loop,
next scheduled run is the retry" contract. Each individual hit is still
parsed defensively and skipped (never fatal) on its own malformed shape --
mirroring reddit.py's `_parse_post` per-post defensiveness -- because a
single bad hit inside an otherwise-successful response is routine, not a
sign the whole request failed.
"""

from __future__ import annotations

import json
import logging
import re
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

# Algolia's public HN Search index. No auth, no API key -- see module
# docstring's "Why Algolia" section.
_API_BASE = "https://hn.algolia.com/api/v1/search"

# Sent on every request. Not verified to be REQUIRED (unlike
# polymarket.py's own _USER_AGENT, which is a live-verified Cloudflare
# Browser-Integrity-Check requirement) -- this is just a stable, honest
# product identifier, matching rss.py's own precautionary rationale (some
# hosts reject urllib's default "Python-urllib/x.y" signature outright).
_USER_AGENT = "digest-hackernews/1.0"

# Matches the front-page index's own "currently on the front page" scope --
# see module docstring's "No cursor axis" section for why this replaces a
# cursor entirely rather than supplementing one. Same value as reddit.py's
# own _LOOKBACK_HOURS, for consistency across this digest's no-cursor
# collectors, though the two exist for unrelated underlying reasons (HN's
# front-page churn vs Reddit's `t=day` window).
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
    newline/tab, mirroring reddit.py's own `_clean_text` rationale (prompt-
    budget hygiene), applied here to a field that essentially never needs it
    in practice.
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
    counts skips, it never lets one malformed hit abort the whole run
    (mirrors reddit.py's `_parse_post` contract).

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


def collect(top_n: int) -> CollectResult:
    """Fetch the current HN front page (up to `top_n` stories) within the lookback window.

    One request (`_fetch_front_page`) per run. ANY failure of it sets
    `result.failed = True` and returns immediately with no items -- see
    module docstring's "Failure semantics". Only the exception's TYPE NAME is
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

    try:
        raw_hits = _fetch_front_page(top_n)
    except Exception as exc:
        logger.warning("hackernews fetch failed: %s", type(exc).__name__)
        return CollectResult(failed=True)

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
