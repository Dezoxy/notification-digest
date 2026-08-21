"""Patreon collector: the owner's paid-tier posts, via the site's own JSON API.

WHY A COOKIE AND NOT A FEED, AN EMAIL, OR A BROWSER -- all four were tried
against the real account on 2026-08-21:

  - Native RSS is audio/podcast only and must be enabled by the CREATOR.
    This creator offers none, so there is no feed for NEWS_FEEDS.
  - The notification emails carry a truncated teaser, not the post body.
  - The public API v2 is creator-oriented: a patron token reaches
    `identity` and its own memberships, never a campaign's post list.
  - Driving the logged-in Chromium (the `jobs-refresh` pattern) DOES work,
    but costs a `claude -p` browser session per run, contends with
    jobs-refresh over the single browser, and -- measured -- returned a
    listing with 13 of 22 timestamps missing, every body empty, and posts
    out of chronological order from pinning and lazy-loading.

The site's own frontend API returns all of it correctly in ONE request:

    GET https://www.patreon.com/api/posts?json-api-version=1.0
        &filter[campaign_id]=<id>&sort=-published_at&page[count]=<n>
    Cookie: session_id=<secret>

Live-verified: `session_id` ALONE is sufficient (no __cf_bm / _cfuvid
Cloudflare cookie, no device id), the cookie's own expiry is a full year
out, and 25 of 25 posts came back with populated bodies, exact
`published_at`, and strictly monotonic ids. The browser remains how that
cookie is obtained and refreshed -- once a year, not once an hour.

AUTHORIZATION IS AN EXPLICIT FIELD, NOT AN INFERENCE. Verified against an
unauthenticated control request: Patreon answers 200 with the posts still
listed, but every `current_user_can_view` false and every body empty. It
DEGRADES rather than erroring, so a dead session would otherwise look like
a successful run. `_authorization_state` keys on that field directly --
there is nothing to infer from page markers, and no ambiguity between "no
new posts" and "no longer a patron".

ToS POSTURE: Patreon's terms prohibit automated access. This is the same
accepted-risk stance CLAUDE.md records for the X collector ("twikit,
cookie session, unofficial API -- ToS risk accepted by the owner"), with
one difference worth keeping in view: the X account is free, whereas an
adverse action here costs a PAID subscription. Volume is one request per
run against the site's own frontend endpoint, no crawling, no media fetch.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from digest.collectors.base import CollectResult
from digest.state import Item

logger = logging.getLogger(__name__)

_API_BASE = "https://www.patreon.com/api/posts"

# Patreon 403s a default urllib User-Agent. Mirrors rss.py's own _USER_AGENT
# note, except this endpoint is stricter: it wants something browser-shaped.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

_TIMEOUT_SECONDS = 20

# One page is all a steady-state hourly run needs -- this creator posts
# roughly daily (25 posts across the 32 days sampled), so 25 covers even a
# multi-day outage without paging. Deliberately NOT a backfill knob: the
# lookback that matters is the items table, not this number.
_PAGE_COUNT = 25

# Caps the body text handed to the summarizer. Bodies observed ran to ~37k
# characters; the summarizer prompt has no use for the tail of a long post
# and every character is prompt budget. Mirrors rss.py's _MAX_SUMMARY_CHARS,
# larger because these are full articles rather than feed blurbs.
_MAX_BODY_CHARS = 6000


# How many posts the FIRST run delivers. The API page is 25 deep and this
# creator posts roughly daily, so an uncapped first run would fire about a
# month of backlog into the topic at once. Five gives the reader a little
# history without a flood.
_FIRST_RUN_LIMIT = 5


@dataclass
class PatreonCollectResult(CollectResult):
    """A CollectResult that also carries first-run posts to absorb silently.

    `seeded` posts are real, valid items that must be RECORDED as seen but
    NOT delivered -- see `collect`'s first-run note. They ride on the result
    rather than being handled inside the collector because writing to the
    database is the caller's job here, exactly as it is for `items`.

    Subclasses CollectResult rather than replacing it so every existing
    caller-side helper that accepts a CollectResult keeps working; a caller
    that does not know about seeding simply ignores the extra field, which
    is the correct degradation (it delivers the full first page once).
    """

    seeded: list[Item] = field(default_factory=list)


class PatreonUnavailable(Exception):
    """The API could not be read, or answered in a shape that cannot be trusted.

    Distinct from `VerificationUnavailable`/`SummarizeError` for the same
    reason those are distinct from each other: the caller's handling
    differs. Raised for transport failures, non-JSON answers, and -- most
    importantly -- for a session that is no longer authorized.
    """


def _authorization_state(posts: Sequence[dict[str, object]]) -> str:
    """Return "ok", "empty", or "unauthorized" for one page of API results.

    This replaces what a browser-based collector would have had to GUESS
    from page markers. Patreon states it outright per post, so the rule is
    a reading rather than a heuristic:

      no posts at all      -> "empty". Trustworthy on its own: an
                              unauthorized request still LISTS posts
                              (verified against a no-cookie control), so
                              an empty page cannot be a masked auth
                              failure. It means the campaign genuinely has
                              nothing, which for this creator would be
                              remarkable but is not an error.

      no post viewable     -> "unauthorized". The session expired, the
                              pledge lapsed, or the cookie was revoked.
                              Never treated as "nothing new".

      at least one viewable-> "ok". A mixed page is normal: a higher-tier
                              post the owner's pledge does not cover sits
                              alongside ones it does. `collect` drops the
                              unviewable ones individually.
    """
    if not posts:
        return "empty"
    for post in posts:
        attributes = post.get("attributes")
        if isinstance(attributes, dict) and attributes.get("current_user_can_view") is True:
            return "ok"
    return "unauthorized"


def _prosemirror_text(content_json_string: str) -> str:
    """Flatten Patreon's ProseMirror document JSON into plain text.

    `content_json_string` is a serialized ProseMirror/TipTap doc --
    `{"type":"doc","content":[{"type":"paragraph","content":[{"type":
    "text","text":"..."}]}]}` -- not HTML, so rss.py's tag-stripping regex
    approach does not apply. Walks the node tree collecting `text` leaves,
    inserting a blank line at each block boundary so paragraph structure
    survives into the summarizer prompt.

    Returns "" for anything unparseable rather than raising: a post whose
    body cannot be read is still worth delivering by title and link, and
    `_item_from_post` already treats a bodyless post as valid. This is the
    one place in the module where degrading beats failing -- it concerns a
    single post's formatting, never whether the run can be trusted.
    """
    try:
        doc = json.loads(content_json_string)
    except (json.JSONDecodeError, TypeError):
        return ""

    parts: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        if node_type == "text":
            text = node.get("text")
            if isinstance(text, str):
                parts.append(text)
            return
        if node_type == "hard_break":
            parts.append("\n")
            return
        walk(node.get("content"))
        # Block-level nodes end with a paragraph break; inline marks and
        # text nodes above have already returned by this point.
        if node_type in ("paragraph", "heading", "blockquote", "list_item"):
            parts.append("\n\n")

    walk(doc.get("content") if isinstance(doc, dict) else doc)
    return "".join(parts).strip()


def _embed_link(attributes: dict[str, object]) -> str | None:
    """The post's embedded-video URL, or None.

    Patreon states this STRUCTURALLY in `attributes.embed` -- verified
    2026-08-21: every `post_type == "video_embed"` post carried an embed
    object with `url` ("https://youtu.be/V2F83U2r22A"), `provider`
    ("YouTube"), and `subject` (the video's own title). So this is a field
    read, never a regex over body text.

    Body prose DOES also carry links -- ProseMirror link marks pointing at
    treasury.gov, bls.gov, federalreserve.gov and the like -- but those are
    the post's source citations, not the thing the reader wants a button
    for. They are deliberately NOT considered here: a button per citation
    would bury the video under five Fed speeches.

    Provider-agnostic on purpose. `provider` was "YouTube" on every sample,
    but the owner's account also has Vimeo connected, and a Vimeo embed
    deserves the same button. Any http(s) URL in `embed.url` qualifies; the
    scheme/netloc check is the same guard `rss.py`'s `_entry_link` applies,
    for the same reason -- this URL becomes a Telegram button target.
    """
    embed = attributes.get("embed")
    if not isinstance(embed, dict):
        return None
    url = embed.get("url")
    if not isinstance(url, str) or not url:
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return url


def _item_from_post(post: dict[str, object], fetched_at: str) -> Item | None:
    """Normalize one API post object into an Item, or None if unusable.

    `source_id` is Patreon's own post id -- the JSON:API resource id,
    which is exactly the numeric tail of the post's URL slug. Verified
    strictly monotonic with `published_at` across the newest 25 posts, but
    dedup does NOT rely on that ordering: UNIQUE(source, source_id) is the
    contract, so an edited or backdated post cannot slip past the way it
    could past a watermark.

    A post the pledge does not cover is dropped here rather than delivered
    as a teaser -- the point of this collector is the content, and a
    locked post's title alone is what the notification emails already give.
    """
    attributes = post.get("attributes")
    if not isinstance(attributes, dict):
        return None
    if attributes.get("current_user_can_view") is not True:
        return None

    post_id = post.get("id")
    if not isinstance(post_id, str) or not post_id:
        return None

    url = attributes.get("url")
    if not isinstance(url, str) or not url.startswith("https://www.patreon.com/"):
        return None

    title = attributes.get("title")
    title = title.strip() if isinstance(title, str) else ""

    raw_body = attributes.get("content_json_string")
    body = _prosemirror_text(raw_body) if isinstance(raw_body, str) else ""
    body = body[:_MAX_BODY_CHARS]

    text = f"{title}\n\n{body}".strip() if body else title
    if not text:
        return None

    return Item(
        source="patreon",
        source_id=post_id,
        chat_id=None,
        chat_title="Patreon",
        author=None,
        text=text,
        url=url,
        embed_url=_embed_link(attributes),
        fetched_at=fetched_at,
    )


def _fetch_page(campaign_id: str, session_cookie: str) -> list[dict[str, object]]:
    """Fetch one page of newest-first posts. Raises PatreonUnavailable on any failure.

    Only `session_id` is sent. Live-verified as sufficient on its own --
    forwarding the whole browser jar would additionally hand Patreon's
    Cloudflare bot-management cookies and analytics ids to every request
    for no functional gain, and would make the secret harder to rotate.
    """
    query = urllib.parse.urlencode(
        {
            "json-api-version": "1.0",
            "filter[campaign_id]": campaign_id,
            "filter[is_draft]": "false",
            "sort": "-published_at",
            "page[count]": str(_PAGE_COUNT),
            # `embed` is load-bearing, not decorative: without it in this
            # list the API simply omits the key and every post looks like it
            # has no video (caught by a live render, where a known
            # video_embed post produced no button).
            "fields[post]": (
                "title,content_json_string,published_at,url,post_type,current_user_can_view,embed"
            ),
        }
    )
    request = urllib.request.Request(
        f"{_API_BASE}?{query}",
        headers={
            "cookie": f"session_id={session_cookie}",
            "user-agent": _USER_AGENT,
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # Status only -- never the body. A Patreon error page can echo
        # request content, and the request carries the session cookie.
        raise PatreonUnavailable(f"patreon api returned HTTP {exc.code}") from exc
    except Exception as exc:
        raise PatreonUnavailable(f"patreon api request failed ({type(exc).__name__})") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PatreonUnavailable("patreon api response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PatreonUnavailable("patreon api response was not a JSON object")

    data = payload.get("data")
    if not isinstance(data, list):
        raise PatreonUnavailable("patreon api response carried no data array")
    return [post for post in data if isinstance(post, dict)]


def collect(
    campaign_id: str,
    session_cookie: str,
    known_ids_lookup: Callable[[Sequence[str]], set[str]],
    first_run_limit: int = _FIRST_RUN_LIMIT,
) -> CollectResult:
    """Fetch the newest posts and return the ones not already stored.

    ONE request per run. No browser, no `claude -p`, no lock against
    `jobs-refresh` -- see the module docstring for what that replaced.

    `known_ids_lookup` is injected rather than taking a sqlite3.Connection
    so this module never touches the database (matching every other
    collector) and so tests can drive it with a plain set. It is the dedup
    AUTHORITY: a `published_at` watermark would be workable given the
    verified ordering, but the id set costs one local query, needs no
    stored cursor, and cannot be defeated by an edited or backdated post.

    `cursor_updates` is always `{}` -- identical rationale to `rss.py`.

    FIRST RUN: when the lookup reports NOTHING known -- an empty state, a
    restored backup, a newly enabled collector -- only the newest
    `first_run_limit` posts are returned. The API's page is 25 deep and
    this creator posts roughly daily, so without this the collector's very
    first run would deliver about a month of backlog as ~25 separate
    Telegram messages in one burst. That is both unreadable and precisely
    the send pattern the 2026-08-06 flood guards exist to prevent.

    The posts BEYOND that limit are returned as `seeded` on the result --
    NOT silently dropped. The caller must record them as seen, or the next
    run finds them unknown again and the cap achieves nothing but a delay.

    `failed=True` on any `PatreonUnavailable`, including an unauthorized
    session. This is a SINGLE-source collector, so rss.py's
    majority-of-feeds tolerance has no analogue: there is one thing being
    read and it either worked or it did not. An authorized run with nothing
    new is NOT a failure and returns a clean, itemless result.
    """
    try:
        posts = _fetch_page(campaign_id, session_cookie)
    except PatreonUnavailable as exc:
        logger.error("patreon fetch failed: %s", exc)
        return CollectResult(failed=True)

    state = _authorization_state(posts)
    if state == "unauthorized":
        # Back off, never retry-loop -- CLAUDE.md's standing rule for
        # session-based collectors. The failure banner and the unit's
        # OnFailure alert are what tell the owner to refresh the cookie
        # from the chromium container.
        logger.error(
            "patreon session is not authorized for any of the %d posts returned; "
            "cookie likely expired or pledge lapsed",
            len(posts),
        )
        return CollectResult(failed=True)

    listed_ids = [post["id"] for post in posts if isinstance(post.get("id"), str)]
    already_known = known_ids_lookup(listed_ids)

    fetched_at = datetime.now(UTC).isoformat()
    fresh = [post for post in posts if post.get("id") not in already_known]

    # `posts` arrives newest-first (sort=-published_at), so slicing the head
    # takes the most recent -- the ones worth a little history in the topic.
    seeded_posts: list[dict[str, object]] = []
    if not already_known and len(fresh) > first_run_limit:
        seeded_posts = fresh[first_run_limit:]
        fresh = fresh[:first_run_limit]

    items = [item for post in fresh if (item := _item_from_post(post, fetched_at)) is not None]
    seeded = [
        item for post in seeded_posts if (item := _item_from_post(post, fetched_at)) is not None
    ]

    logger.info(
        "patreon_health %s",
        json.dumps(
            {
                "state": state,
                "listed": len(posts),
                "already_known": len(already_known),
                "items_kept": len(items),
                "seeded": len(seeded),
                "with_embed": sum(1 for item in items if item.embed_url),
            },
            sort_keys=True,
        ),
    )
    if seeded:
        # Never let a first-run cap look like "that was everything".
        logger.info(
            "patreon: first run -- delivering the newest %d post(s), "
            "absorbing %d older one(s) as already-seen",
            len(items),
            len(seeded),
        )
    return PatreonCollectResult(items=items, seeded=seeded)
