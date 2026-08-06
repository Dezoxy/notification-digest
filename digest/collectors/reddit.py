"""Reddit collector: top-of-day posts from the owner's chosen subreddits, via OAuth app-only auth.

See CLAUDE.md and PLAN.md for the full plan. This module is synchronous
(urllib), matching rss.py/polymarket.py -- the caller (digest/main.py) runs
collectors sequentially, one at a time, so there is nothing else in flight
to block on.

Why OAuth app-only, not the public RSS feed (live-verified)
----------------------------------------------------------------------
Reddit's unauthenticated `.rss`/`.json` endpoints are throttled per-IP hard
enough to be useless for a scheduled collector: live-verified from the
production IP, 4 of 5 requests came back 429 even when paced. Reddit's own
documented alternative for a script with no per-user login flow is the
OAuth **app-only** grant: a "script"-type app's client_id+secret are
exchanged via HTTP Basic auth at `https://www.reddit.com/api/v1/access_token`
(`grant_type=client_credentials`) for a bearer token good for about an hour,
which then authenticates every `https://oauth.reddit.com/...` request at up
to 100 requests/minute -- far more headroom than this collector, run once
every 3 hours across a handful of subreddits, could ever need.

One token fetch per run (not cached across runs): this process is a fresh
`python -m digest` invocation every time (no long-lived daemon), so there is
nowhere to cache a ~1h token between runs anyway, and re-fetching costs one
extra request per run against a limit measured in the hundreds.

User-Agent is mandatory, not a nicety (live-verified requirement)
----------------------------------------------------------------------
Reddit's API rules require every application to send a unique, descriptive
User-Agent string; generic ones (a bare library default, or something
indistinguishable from every other client) get throttled far more
aggressively than the documented limits, independent of the OAuth grant
itself. `_USER_AGENT` is a module constant for exactly this reason -- it
must stay descriptive and stable, never revert to urllib's default
"Python-urllib/x.y" signature.

Failure semantics: token vs per-subreddit (mirrors telegram.py/x.py vs rss.py)
----------------------------------------------------------------------
The token fetch is the single point every subsequent request depends on --
if it fails, nothing else in this run can possibly succeed, so ANY failure
of it sets `failed=True` for the whole run and returns immediately (no
network calls are made at all), the same posture telegram.py/x.py take
toward their one chat/timeline (see rss.py's own module docstring for the
contrast this mirrors).

Once a token is in hand, though, each subreddit is fetched independently and
a single subreddit going private, banned, or renamed must not permanently
trip the digest's failure banner every run thereafter -- that is exactly
rss.py's per-feed fault-tolerance rationale, reused here verbatim: one
subreddit's fetch failure is logged and skipped, and `failed=True` is set on
the overall result only when EVERY configured subreddit failed (a total
outage, or -- more likely in practice -- a token that authenticates but has
lost all scope).

No cursor axis; a lookback window instead (mirrors rss.py exactly)
----------------------------------------------------------------------
`t=day` ("top of the last 24 hours") is Reddit's own ROLLING ranked window,
re-computed fresh on every request -- not a monotonic timeline a cursor
could meaningfully page through. A post's rank within that window can change
between runs (new posts overtake it, votes shift), so there is no stable
"since" pagination contract to track, exactly like rss.py's feeds have none
(see that module's own docstring for the identical reasoning). Idempotency
instead comes entirely from `UNIQUE(source, source_id)` +
`INSERT ... ON CONFLICT DO NOTHING` (digest/state.py): re-fetching the same
post across overlapping windows is a free no-op. `_LOOKBACK_HOURS` (24) is
also applied client-side as a defensive filter on each post's own
`created_utc`, matching `t=day`'s own window -- belt-and-braces against a
`top` response that (rarely, but observed on other subreddits historically)
includes something older than the window it claims to rank.

Score/comments ride IN the item text, not a separate field
----------------------------------------------------------------------
`_build_item` folds `[score N, M comments]` directly into `Item.text` rather
than adding new columns to the shared `Item`/`items` schema -- this is what
lets the summarizer prompt (prompts/digest.md) weight a post by community
signal (a 4000-upvote post carries real community weight; a 12-upvote one
does not) with no changes to digest/state.py at all.

`chat_title` is the reddit-specific provenance field
----------------------------------------------------------------------
Every item's `chat_title` is `f"r/{subreddit}"` -- playing the same "which
group/feed did this come from" role Telegram's `chat_title` and rss.py's
feed-title play for their own sources. This exact `f"r/{subreddit}"` format
is load-bearing, not cosmetic: prompts/digest.md's Hungary standing-section
rule keys on `chat_title == "r/hungary"` verbatim.

Stickied and NSFW posts are noise, not content
----------------------------------------------------------------------
A stickied post is a moderator announcement/rules post pinned to the top of
the subreddit regardless of its actual age or relevance this run -- it is
not "top of day" content in any meaningful sense, so it is skipped
(`data.stickied`), counted at debug level, never fatal. `over_18` posts are
excluded outright (skipped, counted at debug level) -- a personal news
digest has no use for NSFW-flagged community content regardless of subject
matter.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from digest.collectors.telegram import CollectResult
from digest.state import Item

logger = logging.getLogger(__name__)

# Per-request urllib timeout, in seconds. Matches rss.py's/polymarket.py's own
# constant of the same name/value -- a hung request (token endpoint or a
# subreddit listing) must not wedge the whole run.
_REQUEST_TIMEOUT_SECONDS = 15

# Sent on every request (token exchange AND every oauth.reddit.com call).
# Live-verified requirement, not a nicety -- see module docstring's "User-
# Agent is mandatory" section: Reddit's API rules require a unique,
# descriptive UA, and generic ones get throttled far harder than the
# documented limits regardless of the OAuth grant itself.
_USER_AGENT = "linux:notification-digest:v1 (personal digest)"

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_OAUTH_BASE = "https://oauth.reddit.com"

# Courtesy pacing between per-subreddit requests. 100 requests/minute (the
# app-only OAuth grant's own limit) gives enormous headroom for a handful of
# subreddits once every 3 hours -- this sleep exists purely so this
# collector doesn't hit Reddit in a tight burst, not because the limit is
# remotely at risk of being hit.
_INTER_SUB_SLEEP_SECONDS = 0.5

# Matches Reddit's own `t=day` ranking window -- see module docstring's "No
# cursor axis" section for why this replaces a cursor entirely rather than
# supplementing one.
_LOOKBACK_HOURS = 24

# Caps the selftext excerpt folded into Item.text (module docstring's
# "Score/comments ride IN the item text" section). The title is kept in full
# regardless -- only a self-post's (often much longer) body is truncated.
_MAX_SELFTEXT_EXCERPT_CHARS = 500

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class _ParsedPost:
    """One Reddit post's fields, extracted from one raw `Listing` child object.

    Deliberately flat -- the only shape `collect`'s filtering and
    `_build_item` consume from `_parse_post`. `stickied`/`over_18` are kept
    here (not applied inside `_parse_post` itself) so `collect` can count
    each skip reason separately at debug level, mirroring
    polymarket.py's `_is_sports_market`/`_parse_binary_market` split between
    "wrong category" and "malformed shape".
    """

    post_id: str
    title: str
    author: str | None
    permalink: str
    score: int
    num_comments: int
    selftext: str
    created_utc: float
    stickied: bool
    over_18: bool


def _clean_text(raw: str) -> str:
    """Collapse whitespace/newline runs in a Reddit title/selftext field.

    Reddit's fields are plain text/markdown, unlike rss.py's HTML feed
    summaries -- no tag-stripping is needed here, just collapsing the
    blank-line/whitespace runs a long selftext post commonly carries, purely
    for prompt-budget hygiene (same rationale as rss.py's `_clean_summary`,
    minus the HTML-specific steps that don't apply to this source).
    """
    return _WHITESPACE_RE.sub(" ", raw).strip()


def _parse_post(raw: Any) -> _ParsedPost | None:
    """Parse one raw `Listing` child object into a `_ParsedPost`, or None.

    Returns None (never raises) for anything that isn't a well-formed post:
    a non-dict entry, a missing/non-dict `data`, or a missing/blank
    `id`/`title`/`permalink`/`created_utc`. Every check fails open into
    `None` rather than propagating -- `collect`'s caller counts skips, it
    never lets one malformed post abort the subreddit's whole batch (mirrors
    polymarket.py's `_parse_binary_market` contract).

    `score`/`num_comments` are defensively coalesced to 0 on anything
    unparsable -- they are a display/salience signal in the item text
    (module docstring), not something that should disqualify an otherwise
    valid post.
    """
    if not isinstance(raw, dict):
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        return None

    post_id = data.get("id")
    if not isinstance(post_id, str) or not post_id:
        return None

    title = data.get("title")
    if not isinstance(title, str) or not title:
        return None

    permalink = data.get("permalink")
    if not isinstance(permalink, str) or not permalink:
        return None

    try:
        created_utc = float(data.get("created_utc"))
    except (TypeError, ValueError):
        return None

    author = data.get("author")
    author = author if isinstance(author, str) and author else None

    try:
        score = int(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0

    try:
        num_comments = int(data.get("num_comments", 0))
    except (TypeError, ValueError):
        num_comments = 0

    selftext = data.get("selftext")
    selftext = selftext if isinstance(selftext, str) else ""

    return _ParsedPost(
        post_id=post_id,
        title=title,
        author=author,
        permalink=permalink,
        score=score,
        num_comments=num_comments,
        selftext=selftext,
        created_utc=created_utc,
        stickied=bool(data.get("stickied")),
        over_18=bool(data.get("over_18")),
    )


def _build_item(post: _ParsedPost, subreddit: str, fetched_at: str) -> Item:
    """Build the digest Item for one confirmed, in-window Reddit post.

    See module docstring's "Score/comments ride IN the item text" and
    "`chat_title` is the reddit-specific provenance field" sections for why
    the text shape and the exact `f"r/{subreddit}"` chat_title format are
    both load-bearing, not stylistic choices.
    """
    title = _clean_text(post.title)
    selftext_excerpt = _clean_text(post.selftext)[:_MAX_SELFTEXT_EXCERPT_CHARS]
    text = f"{title} [score {post.score}, {post.num_comments} comments] {selftext_excerpt}".strip()
    return Item(
        source="reddit",
        source_id=post.post_id,
        chat_id=None,
        chat_title=f"r/{subreddit}",
        author=post.author,
        text=text,
        url=f"https://www.reddit.com{post.permalink}",
        fetched_at=fetched_at,
    )


def _fetch_access_token(client_id: str, client_secret: str) -> str:
    """Exchange the app's client_id/secret for a bearer token via the client_credentials grant.

    POSTs to `_TOKEN_URL` with HTTP Basic auth built from `client_id:client_secret`
    (module docstring's "Why OAuth app-only" section) and body
    `grant_type=client_credentials`. Raises on any failure (network error,
    non-2xx status, non-JSON body, a JSON body with no usable
    `access_token` string) -- deliberately unguarded here, matching
    polymarket.py's `_fetch_markets`: the caller (`collect`) wraps this
    single call in the try/except that sets `failed=True` for the whole run
    (module docstring's "Failure semantics" -- a token failure is the whole
    run's signal, since nothing downstream can work without one).

    Neither `client_id` nor `client_secret` is ever logged by this function,
    directly or via an exception message -- the caller only logs the
    exception's TYPE NAME (see `collect`).
    """
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": _USER_AGENT,
    }
    request = urllib.request.Request(_TOKEN_URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        raw_bytes = response.read()
    data = json.loads(raw_bytes)
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise ValueError("reddit access_token response missing a usable access_token")
    return token


def _fetch_subreddit_posts(access_token: str, subreddit: str, posts_per_sub: int) -> list[Any]:
    """One GET to `{OAUTH_BASE}/r/{subreddit}/top`, returning the raw `Listing` children.

    `t=day` + `limit={posts_per_sub}` + `raw_json=1` -- see module
    docstring's "No cursor axis" section for why `t=day` (not a cursor) is
    the right idempotency model here, and CLAUDE.md/the feature spec for why
    `raw_json=1` (Reddit otherwise HTML-entity-escapes text fields).

    Raises on any failure (network error, non-2xx status, non-JSON body, a
    body missing `data.children` as a list) -- deliberately unguarded here,
    matching `_fetch_access_token`: the caller (`collect`) wraps this call in
    the try/except that counts this ONE subreddit as failed (module
    docstring's "Failure semantics" -- a single subreddit's fetch failing is
    routine, not the whole run's signal).
    """
    url = f"{_OAUTH_BASE}/r/{subreddit}/top?t=day&limit={posts_per_sub}&raw_json=1"
    headers = {"Authorization": f"Bearer {access_token}", "User-Agent": _USER_AGENT}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        raw_bytes = response.read()
    data = json.loads(raw_bytes)
    children = data.get("data", {}).get("children") if isinstance(data, dict) else None
    if not isinstance(children, list):
        raise ValueError(f"reddit /r/{subreddit}/top response missing data.children")
    return children


def collect(
    client_id: str,
    client_secret: str,
    subreddits: Sequence[str],
    posts_per_sub: int,
) -> CollectResult:
    """Fetch today's top posts from every configured subreddit.

    Empty `subreddits` -> a fresh, unfailed `CollectResult()` with no network
    calls at all (not even the token exchange) -- mirrors rss.py's own
    `collect(feed_urls=())` no-op, and covers the same "collector wired up
    but nothing configured yet" shape digest/config.py's validation should
    already prevent once `REDDIT_ENABLED=true`, kept here anyway so this
    function's own contract stays total.

    One token fetch (`_fetch_access_token`) up front. ANY failure of it sets
    `failed=True` and returns immediately with no items -- see module
    docstring's "Failure semantics": every subsequent request depends on
    this token, so its failure is the whole run's signal, exactly like
    telegram.py's single session or x.py's single timeline.

    Each subreddit is then fetched (`_fetch_subreddit_posts`) independently,
    wrapped in its own try/except -- one subreddit failing (private, banned,
    renamed, a transient error) is logged as a WARNING (the subreddit name
    is owner config, not a secret) and skipped; the rest proceed normally.
    `failed=True` is set on the result ONLY when subreddits were configured
    and EVERY single one of them failed -- a total outage -- mirroring
    rss.py's own per-feed fault-tolerance contract exactly.

    A small fixed pause (`_INTER_SUB_SLEEP_SECONDS`) is taken BEFORE each
    subreddit request except the first -- pure courtesy pacing (module
    docstring), not a rate-limit necessity.

    Each raw post is parsed (`_parse_post`; a malformed entry is skipped,
    never fatal) and then filtered: `stickied` posts and `over_18` posts are
    each skipped and counted at debug level (module docstring's "Stickied
    and NSFW posts are noise" section), and any post whose own `created_utc`
    falls outside `_LOOKBACK_HOURS` is silently dropped (a defensive
    re-check of Reddit's own `t=day` window, see module docstring's "No
    cursor axis" section). Survivors become Items via `_build_item`.

    `cursor_updates` is always `{}` (via the reused `CollectResult` -- see
    module docstring for why this collector has no cursor axis at all).
    """
    result = CollectResult()

    if not subreddits:
        return result

    try:
        access_token = _fetch_access_token(client_id, client_secret)
    except Exception as exc:
        logger.warning("reddit token fetch failed: %s", type(exc).__name__)
        return CollectResult(failed=True)

    fetched_at = datetime.now(UTC).isoformat()
    cutoff = datetime.now(UTC).timestamp() - _LOOKBACK_HOURS * 3600

    succeeded = 0
    skipped = 0
    for index, subreddit in enumerate(subreddits):
        if index > 0:
            time.sleep(_INTER_SUB_SLEEP_SECONDS)

        try:
            children = _fetch_subreddit_posts(access_token, subreddit, posts_per_sub)
        except Exception as exc:
            logger.warning(
                "reddit subreddit fetch failed: r/%s (%s)", subreddit, type(exc).__name__
            )
            continue

        succeeded += 1
        for raw in children:
            post = _parse_post(raw)
            if post is None:
                skipped += 1
                continue
            if post.stickied:
                logger.debug("reddit: skipped stickied post in r/%s", subreddit)
                continue
            if post.over_18:
                logger.debug("reddit: skipped over_18 post in r/%s", subreddit)
                continue
            if post.created_utc < cutoff:
                skipped += 1
                continue
            result.items.append(_build_item(post, subreddit, fetched_at))

    if skipped:
        logger.debug("reddit: skipped %d malformed/stale posts", skipped)

    if succeeded == 0:
        result.failed = True

    return result
