"""Reddit collector: top-of-day posts from the owner's chosen subreddits, via a cookie session.

See CLAUDE.md and PLAN.md for the full plan. This module is synchronous
(urllib), matching rss.py/polymarket.py -- the caller (digest/main.py) runs
collectors sequentially, one at a time, so there is nothing else in flight
to block on.

Why a cookie session, not OAuth, not anonymous endpoints (live-verified)
----------------------------------------------------------------------
This collector previously authenticated via Reddit's OAuth app-only
(`client_credentials`) grant. That path is now permanently dead: Reddit's
Data Team formally REFUSED the owner's API access application (email dated
2026-08-06 -- "not in compliance with Reddit's Responsible Builder Policy
and/or lacks necessary details"), so no script-app client_id/secret this
owner could obtain would ever be granted a token. The anonymous fallback --
unauthenticated `.json`/`.rss` endpoints -- is also a dead end, and more
thoroughly than mere throttling: live-verified from the production IP,
anonymous `.json` comes back a HARD 403 regardless of request pacing, not a
429 that gentler pacing could work around. `rss.py`'s per-host-paced RSS
fallback (PR #33) is a genuinely different, degraded surface -- slower,
fragile to markup changes, and carrying no score/comment-count signal at
all -- and stays UNTOUCHED as this collector's safety net; nothing here
changes that module.

The replacement is the owner's own logged-in Reddit session: the browser's
long-lived `reddit_session` cookie, sent as a bare `Cookie` header to
`https://old.reddit.com`'s `.json` endpoints. This mirrors the ToS-risk
posture digest/collectors/x.py already takes for X/Twitter (cookie session,
unofficial, owner accepts the risk) -- same posture, same back-off
semantics on an auth failure (see `collect`'s docstring), applied to a
different site.

Why `old.reddit.com`, not `www.reddit.com` (live-verified)
----------------------------------------------------------------------
`old.reddit.com` is the surface that still accepts a bare `reddit_session`
cookie as full authentication for its `.json` endpoints -- the legacy
session model this cookie was issued under. `www.reddit.com`'s current
frontend instead expects a short-lived `token_v2` JWT (refreshed
client-side by the React app on every page load), which a single exported
`reddit_session` cookie value cannot substitute for. `_BASE_URL` therefore
points at `old.reddit.com`, deliberately, not the modern domain.

User-Agent (unproven for a cookie session, not a live-verified requirement)
----------------------------------------------------------------------
Reddit's API rules mandate a descriptive User-Agent for the OAuth API;
whether `old.reddit.com`'s cookie-session surface prefers (or requires) a
browser-like UA instead of a descriptive one is NOT yet live-verified one
way or the other. `_USER_AGENT` keeps the same descriptive, stable string
this module already used for OAuth -- the honest default until a live run
proves otherwise, at which point this comment (and the constant) should be
revisited.

Failure semantics: session verify vs per-subreddit (mirrors telegram.py/x.py vs rss.py)
----------------------------------------------------------------------
The session-verify call is the single point every subsequent request
depends on -- if it fails, nothing else in this run can possibly succeed,
so ANY failure of it sets `failed=True` for the whole run and returns
immediately (no further network calls are made at all), the same posture
telegram.py/x.py take toward their one chat/timeline (see rss.py's own
module docstring for the contrast this mirrors).

Once a session is confirmed logged in, though, each subreddit is fetched
independently and a single subreddit going private, banned, or renamed must
not permanently trip the digest's failure banner every run thereafter --
that is exactly rss.py's per-feed fault-tolerance rationale, reused here
verbatim: one subreddit's fetch failure is logged and skipped, and
`failed=True` is set on the overall result when EVERY configured subreddit
failed (a total outage), OR when the session dies mid-run (see the next
section) -- whichever comes first.

Auth back-off mid-run (mirrors x.py's hard rule: back off, never retry-loop)
----------------------------------------------------------------------
An HTTP 401/403 from a per-subreddit request, after the session verified
logged-in moments earlier, means the session died DURING this run -- the
owner logged out elsewhere, Reddit flagged/locked the account, or the
cookie simply expired between the verify call and this request. This is
exactly the scenario x.py's own docstring calls out for X: continuing to
hammer a dead credential across the remaining subreddits is how an
unofficial, cookie-based API gets an account flagged or locked harder, not
just a wasted request. So a 401/403 here aborts the REST of this run
immediately (`break`, not `continue`) -- no retry, no re-verify attempt;
the next scheduled run (3h later) is the retry, mirroring x.py's own "next
scheduled run is the retry" contract. Items already collected from earlier,
successful subreddits in this same run are still returned and still
committed (state upsert is idempotent, digest/state.py's
`UNIQUE(source, source_id)` + `INSERT ... ON CONFLICT DO NOTHING`) -- only
the REMAINING, not-yet-fetched subreddits are skipped. `result.failed` is
set True in this case even though earlier subreddits may have succeeded: a
dying session is the whole run's signal (something is wrong with the
credential itself, not with one subreddit), regardless of partial success.

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

import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from digest.collectors.telegram import CollectResult
from digest.state import Item

logger = logging.getLogger(__name__)

# Per-request urllib timeout, in seconds. Matches rss.py's/polymarket.py's own
# constant of the same name/value -- a hung request (the session-verify call
# or a subreddit listing) must not wedge the whole run.
_REQUEST_TIMEOUT_SECONDS = 15

# Sent on every request (session verify AND every old.reddit.com call). See
# module docstring's "User-Agent" section: unlike the OAuth API's documented
# requirement, whether old.reddit.com's cookie-session surface prefers a
# browser-like UA is unproven -- this descriptive, stable string is the
# honest default pending a live-verification run.
_USER_AGENT = "linux:notification-digest:v1 (personal digest)"

# old.reddit.com is the surface that accepts a bare `reddit_session` cookie
# as full authentication for its `.json` endpoints -- see module docstring's
# "Why old.reddit.com, not www.reddit.com" section.
_BASE_URL = "https://old.reddit.com"

# Courtesy pacing between per-subreddit requests. Logged-in old.reddit.com's
# own rate limits are undocumented/unproven for this cookie-session path
# (unlike the now-dead OAuth grant's documented 100 req/min) -- 2s across a
# handful of subreddits once every 3 hours is free insurance, and this
# collector must err polite given the account-risk posture (module
# docstring's "Auth back-off mid-run" section).
_INTER_SUB_SLEEP_SECONDS = 2.0

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


def _session_headers(session_cookie: str) -> dict[str, str]:
    """Build the Cookie + User-Agent headers every request in this module sends.

    `session_cookie` is stored and sent EXACTLY as exported from the
    browser -- it may contain `%`-escapes, and those are sent verbatim,
    never re-encoded (re-encoding an already-escaped value would double-
    escape it and break the cookie). Never logged, directly or via any
    exception message built around it -- callers only log an exception's
    TYPE NAME (see `_verify_session`/`collect`).
    """
    return {"Cookie": f"reddit_session={session_cookie}", "User-Agent": _USER_AGENT}


def _verify_session(session_cookie: str) -> str:
    """Confirm the session cookie is logged in, returning the owner's own username.

    GETs `{_BASE_URL}/api/me.json` with `_session_headers`. A logged-in
    session returns a JSON object with `data.name` (the account's username);
    an anonymous or dead session returns `{}`. Raises `ValueError` when
    `data.name` is missing or blank -- deliberately unguarded otherwise
    (network errors, non-2xx status, non-JSON body all propagate) -- this
    plays EXACTLY the structural role the old OAuth `_fetch_access_token`
    played: the caller (`collect`) wraps this single call in the try/except
    that sets `failed=True` for the whole run and returns immediately with
    zero further network calls (module docstring's "Failure semantics").

    `session_cookie` is never logged by this function, directly or via an
    exception message. The verified username IS logged, but only at debug
    level -- it's the owner's own account name, not a secret, but no need
    for info-level noise on every run.
    """
    url = f"{_BASE_URL}/api/me.json"
    request = urllib.request.Request(url, headers=_session_headers(session_cookie))
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        raw_bytes = response.read()
    data = json.loads(raw_bytes)
    name = data.get("data", {}).get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name:
        raise ValueError("reddit session cookie is not logged in")
    logger.debug("reddit: session verified as u/%s", name)
    return name


def _fetch_subreddit_posts(session_cookie: str, subreddit: str, posts_per_sub: int) -> list[Any]:
    """One GET to `{_BASE_URL}/r/{subreddit}/top`, returning the raw `Listing` children.

    `t=day` + `limit={posts_per_sub}` + `raw_json=1` -- see module
    docstring's "No cursor axis" section for why `t=day` (not a cursor) is
    the right idempotency model here, and CLAUDE.md/the feature spec for why
    `raw_json=1` (Reddit otherwise HTML-entity-escapes text fields).

    Raises on any failure (network error, non-2xx status, non-JSON body, a
    body missing `data.children` as a list) -- deliberately unguarded here,
    matching `_verify_session`: the caller (`collect`) wraps this call in
    the try/except that counts this ONE subreddit as failed, with a special
    case for `urllib.error.HTTPError` 401/403 (module docstring's "Auth
    back-off mid-run" section -- a single subreddit's fetch failing on a
    NON-auth error is routine, not the whole run's signal).
    """
    url = f"{_BASE_URL}/r/{subreddit}/top.json?t=day&limit={posts_per_sub}&raw_json=1"
    request = urllib.request.Request(url, headers=_session_headers(session_cookie))
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        raw_bytes = response.read()
    data = json.loads(raw_bytes)
    children = data.get("data", {}).get("children") if isinstance(data, dict) else None
    if not isinstance(children, list):
        raise ValueError(f"reddit /r/{subreddit}/top response missing data.children")
    return children


def collect(
    session_cookie: str,
    subreddits: Sequence[str],
    posts_per_sub: int,
) -> CollectResult:
    """Fetch today's top posts from every configured subreddit.

    Empty `subreddits` -> a fresh, unfailed `CollectResult()` with no network
    calls at all (not even the session verify) -- mirrors rss.py's own
    `collect(feed_urls=())` no-op, and covers the same "collector wired up
    but nothing configured yet" shape digest/config.py's validation should
    already prevent once `REDDIT_ENABLED=true`, kept here anyway so this
    function's own contract stays total.

    One session verify (`_verify_session`) up front. ANY failure of it sets
    `failed=True` and returns immediately with no items -- see module
    docstring's "Failure semantics": every subsequent request depends on a
    confirmed logged-in session, so its failure is the whole run's signal,
    exactly like telegram.py's single session or x.py's single timeline.
    Only the exception's TYPE NAME is logged, never the cookie or any
    exception message that might embed it.

    Each subreddit is then fetched (`_fetch_subreddit_posts`) independently,
    wrapped in its own try/except. `urllib.error.HTTPError` with
    `exc.code in (401, 403)` is handled separately from every other
    exception: it means the session died MID-RUN (module docstring's "Auth
    back-off mid-run" section) -- a single WARNING is logged, `result.failed`
    is set True, and the loop `break`s immediately, never touching the
    remaining subreddits. Any OTHER exception per subreddit (a transient
    error, the subreddit going private/banned/renamed) is logged as a
    WARNING (the subreddit name is owner config, not a secret) and skipped;
    the rest proceed normally. `failed=True` is otherwise set on the result
    only when subreddits were configured and EVERY single one of them
    failed -- a total outage -- mirroring rss.py's own per-feed
    fault-tolerance contract.

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
        _verify_session(session_cookie)
    except Exception as exc:
        logger.warning("reddit session verify failed: %s", type(exc).__name__)
        return CollectResult(failed=True)

    fetched_at = datetime.now(UTC).isoformat()
    cutoff = datetime.now(UTC).timestamp() - _LOOKBACK_HOURS * 3600

    succeeded = 0
    skipped = 0
    for index, subreddit in enumerate(subreddits):
        if index > 0:
            time.sleep(_INTER_SUB_SLEEP_SECONDS)

        try:
            children = _fetch_subreddit_posts(session_cookie, subreddit, posts_per_sub)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                logger.warning(
                    "reddit session rejected mid-run (HTTP %d) at r/%s; "
                    "aborting remaining subreddits",
                    exc.code,
                    subreddit,
                )
                result.failed = True
                break
            logger.warning(
                "reddit subreddit fetch failed: r/%s (%s)", subreddit, type(exc).__name__
            )
            continue
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
