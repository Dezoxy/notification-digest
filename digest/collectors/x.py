"""X (Twitter) collector: fetch new notification-timeline entries via twikit.

See PLAN.md §4.3 for the full spec. twikit is imported LAZILY, inside
functions only -- never at module level -- so that X_ENABLED=false runs
have zero twikit import side effects (Phase 3 acceptance criterion).
Error handling and cursor/idempotency rules are load-bearing -- read the
docstrings on `collect` before changing this file.

twikit API facts below were confirmed by reading the installed package
source (.venv/lib/python3.12/site-packages/twikit/), not from memory:

- ``Client()`` takes no required constructor args; auth is cookie-based via
  ``client.load_cookies(path)`` (reads+parses the file itself) or
  ``client.set_cookies(dict)`` for an already-parsed cookie mapping
  (client/client.py).
- ``await client.get_notifications(type, count=40, cursor=None)`` where
  ``type`` is one of the literals ``'All' | 'Verified' | 'Mentions'``
  (client/client.py). ``'All'`` is used here to cover mentions/replies (the
  PLAN's "notifications timeline" scope) without a second, narrower call.
  It returns a ``Result[Notification]``, which supports plain iteration
  (``__iter__``/``__getitem__``/``__len__`` -- utils.py) -- no special
  unwrapping needed for a single page.
- ``Result[Notification]`` (utils.py) supports plain iteration for a single
  page, plus pagination: ``await result.next()`` fetches the following page
  as a new ``Result``, and ``.next_cursor`` exposes the cursor that page was
  (or the following page will be) built from. Critically,
  ``get_notifications`` (client/client.py) ALWAYS builds its ``Result`` with
  an unconditional ``functools.partial(self.get_notifications, type, count,
  next_cursor)`` as the next-page fetcher -- even when ``next_cursor`` is
  ``None`` (no ``cursor-bottom`` entry in the response, i.e. genuinely no
  more pages). That means calling ``.next()`` when ``next_cursor`` is falsy
  does NOT raise or return an empty ``Result`` -- it silently refetches
  page one from the top (``cursor=None`` again). This module therefore
  always checks ``next_cursor`` truthiness itself BEFORE calling
  ``.next()``, rather than trusting ``.next()``'s return value to signal
  "no more pages".
- ``Notification`` (notification.py) has ``.id`` (str), ``.message`` (str),
  ``.tweet`` (``Tweet | None`` -- ``None`` for notifications with no linked
  tweet, e.g. a pure follow event), ``.from_user`` (``User | None``), and
  ``.icon`` (``dict``, built directly from the raw API response's
  ``data['icon']`` -- notification.py does NO further parsing or typing of
  it beyond "it's a dict"; twikit defines no vocabulary for its contents).
- Kind mapping (Codex review finding B -- distinguishing engagement
  notifications, e.g. likes/reposts on the owner's OWN tweets, from
  notifications that carry genuine incoming content, e.g. mentions/
  replies/quotes): since twikit itself attaches no meaning to ``.icon``'s
  contents, the id strings below are grounded in X's own (undocumented)
  notification-icon vocabulary observed in the wild, NOT in twikit source
  -- this module treats them as best-effort, not guaranteed-stable:
    - ``heart_icon`` -- like
    - ``retweet_icon`` -- repost/retweet
  Only these two ids are classified as "engagement" (see
  ``_is_engagement_notification``). Everything else -- a mention's or
  reply's icon id, an icon id this module has never seen, or a
  missing/non-dict ``.icon`` -- is treated as content and fails OPEN
  (included as an item): an occasional own-tweet leaking through is more
  recoverable than a silently dropped mention. ``.message`` (free-text,
  locale-dependent) was deliberately NOT used as a secondary kind signal
  here -- parsing human-readable notification text is brittle across
  languages/wording changes, whereas ``.icon``'s id is a stable-ish
  machine token; not worth the fragility for a fail-open-anyway path.
- ``Tweet`` (tweet.py) has ``.id`` (str, a snowflake id -- ``rest_id`` off
  the raw API payload), ``.full_text`` (the untruncated tweet text, unlike
  ``.text`` which can be clipped for "Note Tweets" over the legacy length
  limit), and ``.user`` (the tweet author, a ``User``).
- ``User`` (user.py) has ``.screen_name`` (str).
- Exceptions (errors.py), all subclassing ``TwitterException``:
  ``Unauthorized`` (401), ``Forbidden`` (403), ``AccountLocked`` (Arkose
  challenge lock), ``AccountSuspended`` -- all auth/cookie-failure signals
  per PLAN §4.3 ("account may be locked/challenged"). ``TooManyRequests``
  (429) is the rate-limit signal. None of these are retried internally by
  twikit when no ``captcha_solver`` is configured (client.py's
  ``request()``), which this module never configures -- so a single failed
  call here really is a single network round trip, matching the "no
  retry loop" requirement.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from digest.collectors.telegram import CollectResult
from digest.state import Item

logger = logging.getLogger(__name__)

# 'All' covers mentions, replies, likes, follows etc. -- the PLAN's
# "notifications timeline" scope (§4.3, decision log #4: notifications
# only, no home timeline). 'Mentions' would be narrower than what the PLAN
# describes as the surface to collect.
_NOTIFICATION_TYPE = "All"

# Bounded pagination cap (see `collect`'s docstring): the notifications API
# is newest-first-only, so catching up after a backlog means walking pages
# oldest-ward until the cursor is bridged. Capped rather than unbounded so a
# huge backlog can't turn one scheduled run into an unbounded number of
# requests -- gentleness/ban-risk mitigation (PLAN.md §8), same rationale as
# telegram.py's _MAX_MESSAGES_PER_CHAT cap.
_MAX_NOTIFICATION_PAGES = 5

# Paced delay between successive page fetches within one collect() call --
# gentleness (don't burst several requests back to back), matching the
# ban-risk mitigation posture already applied to the single-page case.
_PAGE_FETCH_DELAY_SECONDS = 1.5


class XClientLike(Protocol):
    """Minimal duck-typed surface of twikit.Client this module needs.

    Kept loose deliberately so tests can inject a fake client without
    depending on real twikit network objects.
    """

    async def get_notifications(
        self, type: str, count: int = 40, cursor: str | None = None
    ) -> Any: ...


def build_client(cookies_path: str | None, cookies_inline: str | None) -> Any:
    """Build a twikit Client authenticated via persisted cookies.

    Never username/password: a scheduled run must reuse an existing cookie
    session rather than performing anything login-shaped -- a fresh login
    on an unofficial API is exactly the kind of activity that risks
    tripping X's automation detection (PLAN.md §4.3, §8).

    ``cookies_path`` (a file path) is preferred; ``cookies_inline`` (an
    already-serialized JSON object as a string, e.g. from a Key
    Vault-sourced env var) is parsed and applied via ``set_cookies``.
    digest/config.py already enforces "exactly one of the two" when
    X_ENABLED=true, but this function still fails loudly on the
    neither-set case rather than silently building an unauthenticated
    client.

    twikit is imported here, not at module level -- see this module's
    docstring for why that matters for X_ENABLED=false runs.
    """
    from twikit import Client

    if not cookies_path and not cookies_inline:
        raise ValueError("either cookies_path or cookies_inline must be provided")

    client = Client()
    if cookies_path:
        client.load_cookies(cookies_path)
    else:
        client.set_cookies(json.loads(cookies_inline))
    return client


def _tweet_and_screen_name(notification: Any) -> tuple[Any, str | None]:
    """Best-effort (tweet, author screen_name) pair for a notification.

    Returns (None, None) when the notification has no linked tweet (e.g. a
    pure follow event) -- these carry no linkable content and are skipped
    by the caller. Also returns a None screen_name if the tweet's author is
    somehow unavailable, since a url cannot be built without it (defense in
    depth -- twikit's own get_notifications always attaches a user to a
    tweet it returns, per client/client.py's tweets-dict construction).
    """
    tweet = getattr(notification, "tweet", None)
    if tweet is None:
        return None, None
    user = getattr(tweet, "user", None)
    screen_name = getattr(user, "screen_name", None) if user is not None else None
    return tweet, screen_name


# Only icon ids confirmed to mean "pure engagement, no readable content" --
# see the module docstring's "Kind mapping" facts for provenance and the
# fail-open rationale.
_ENGAGEMENT_ICON_IDS = frozenset({"heart_icon", "retweet_icon"})


def _is_engagement_notification(notification: Any) -> bool:
    """True only for like/repost notifications (Codex review finding B).

    These are excluded from digest items -- a like/retweet on a tweet the
    owner posted AFTER the cursor would otherwise pass the `tweet_id >
    cursor` check and leak the owner's own tweet text into the digest as
    if it were incoming content. Tweet age is the wrong discriminator for
    that; the notification kind is (see `.icon`, module docstring).

    Fails OPEN: a notification with no `.icon` dict, or an icon id this
    module doesn't recognize, is treated as content (returns False here)
    rather than silently dropped -- an occasional own-tweet leaking
    through (false positive) is more recoverable than a missed mention
    (false negative).
    """
    icon = getattr(notification, "icon", None)
    if not isinstance(icon, dict):
        return False
    return icon.get("id") in _ENGAGEMENT_ICON_IDS


def _extract_candidates(
    notifications: Any,
) -> tuple[list[tuple[int, str, str, bool]], int, int, int, int]:
    """(tweet_id, text, screen_name, is_engagement) candidates from one page.

    Returns (candidates, raw_count, skipped_no_tweet, skipped_no_screen_name,
    skipped_malformed). `raw_count` is every notification the iterator
    actually yielded on this page (used by `_page_bridges_cursor`), which
    can be larger than `len(candidates)` since malformed/tweetless/authorless
    notifications are counted but not included as candidates.

    Split out of `collect` so the same filtering runs identically for every
    page fetched during pagination, not just the first.

    Per-notification malformation (a non-numeric tweet id, a tweet missing
    `.full_text`, or any other single-notification data problem) is caught
    HERE and skipped with a counted log -- gentle, never fails the page
    (Codex review finding A). By contrast, the surrounding `for` loop
    itself raising (e.g. a broken custom iterator failing mid-page) is
    deliberately NOT caught here: that's a pipeline-level surprise, and is
    left to propagate to `collect`'s own try/except around each page's
    extraction, which sets `failed=True` while keeping whatever prior pages
    (or prior candidates on this page) were already gathered.
    """
    candidates: list[tuple[int, str, str, bool]] = []
    raw_count = 0
    skipped_no_tweet = 0
    skipped_no_screen_name = 0
    skipped_malformed = 0
    for notification in notifications:
        raw_count += 1
        try:
            tweet, screen_name = _tweet_and_screen_name(notification)
            if tweet is None:
                skipped_no_tweet += 1
                continue
            if not screen_name:
                skipped_no_screen_name += 1
                continue
            tweet_id = int(tweet.id)
            text = tweet.full_text
            is_engagement = _is_engagement_notification(notification)
        except Exception as exc:
            logger.info(
                "x notifications: skipped malformed notification: %s", type(exc).__name__
            )
            skipped_malformed += 1
            continue
        candidates.append((tweet_id, text, screen_name, is_engagement))
    return candidates, raw_count, skipped_no_tweet, skipped_no_screen_name, skipped_malformed


def _page_bridges_cursor(
    page_notification_count: int,
    page_candidates: list[tuple[int, str, str, bool]],
    cursor_int: int,
) -> bool:
    """True once this page reaches (or passes) the cursor -- pagination can stop.

    Bridged when either: a candidate on this page has a tweet id <= cursor
    (everything older is already-seen ground), or the page came back with
    zero raw notifications (nothing left to page through, regardless of
    whether any of them happened to be candidates).
    """
    if page_notification_count == 0:
        return True
    return any(tweet_id <= cursor_int for tweet_id, _, _, _ in page_candidates)


async def collect(client: XClientLike, cursor: str | None) -> CollectResult:
    """Fetch the notifications timeline since `cursor`, paginating as needed.

    First run (cursor is None): fetch a single page and seed the cursor from
    it -- no pagination needed to seed from "now" (see below).

    Otherwise: the notifications API is newest-first-only (no way to ask for
    "oldest new item first"), so unlike telegram.py's oldest-first fetching,
    catching up after a backlog means walking pages *from the newest
    downward* until either a page's candidate tweet id is <= cursor (the
    cursor is "bridged" -- everything past that point is already-seen) or a
    hard cap of `_MAX_NOTIFICATION_PAGES` (5) pages is reached. Each
    additional page fetch is preceded by `asyncio.sleep(_PAGE_FETCH_DELAY_SECONDS)`
    (1.5s) -- gentleness, so a backlog catch-up doesn't burst several
    requests back to back (ban-risk mitigation, PLAN.md §8). If the cap is
    hit while still not bridged, a warning is logged naming how many pages
    were fetched: older notifications beyond the cap are skipped for this
    run (visible truncation, not silent data loss) -- they remain fetchable
    on a future run once the cursor has caught up far enough, though a
    sufficiently large sustained backlog can still outrun the cap.

    Cursor/idempotency contract (mirrors telegram.py's, scoped to
    ("x", "notifications") instead of per-chat):

    - Tweet ids are snowflakes (monotonically increasing), so they are
      comparable as integers. The cursor tracks the newest tweet id seen
      among notifications that HAVE a linked tweet -- a bare notification
      id is not used for comparison since its own format is not confirmed
      to be a clean numeric snowflake, whereas a linked Tweet's `.id` is
      (see module docstring).
    - First run (cursor is None): seed the cursor from the newest such
      tweet id in the first page and emit NO items -- consistent with
      telegram.py's no-history-backfill rule (the first digest starts from
      "now"). If the page is empty, or every notification in it lacks a
      linked tweet, there is no candidate id to seed from -- seed "0" so
      the scope isn't re-treated as first-run forever.
    - Otherwise: emit items for every CONTENT notification (across all pages
      fetched) whose linked tweet id is > cursor, oldest-first (ascending by
      tweet id) to match the item ordering convention used elsewhere in
      this codebase. "Content" excludes engagement notifications (likes,
      reposts -- see `_is_engagement_notification` / Codex review finding
      B): a like or retweet on a tweet the owner posted after the cursor
      still has a linked tweet id > cursor, but that tweet is the OWNER's
      own content, not incoming content, so it must not become an item.
      Engagement notifications still count toward cursor candidacy exactly
      like content ones (see next bullet) -- excluding them from items but
      not from the cursor is deliberate: skipping their (genuinely newer)
      tweet ids for cursor purposes would be wrong, since the max()
      semantics below already ensure an engagement notification about an
      OLD tweet can never regress the cursor.
    - The cursor advances to the newest tweet id SEEN across all pages
      fetched (not just the ones that became items -- this includes
      engagement notifications' tweet ids too) as long as it's actually
      newer than the current cursor -- mirrors telegram.py's "cursor
      advances past textless messages too" behavior, just for
      tweetless/engagement notifications instead of textless messages.
    - Notifications without a linked tweet, or whose tweet's author
      screen_name is unavailable, are skipped from BOTH items and cursor
      candidacy -- logged as a count, never individually (PLAN.md: no
      linkable content to include, and a url cannot be built without a
      screen_name).

    Error handling (PLAN.md §4.3; Codex review finding A): auth/cookie
    failures (`Unauthorized`, `Forbidden`, `AccountLocked`,
    `AccountSuspended`) and rate limiting (`TooManyRequests`) on the FIRST
    page both set `failed=True` and return immediately -- NO retry, NO
    re-login attempt; a silent retry on an unofficial, cookie-based API
    risks tripping X's automation detection further, and the next scheduled
    run (3h later) is the retry. Any other exception fetching the first page
    (e.g. a GraphQL/endpoint shape change) is caught the same way so a
    twikit break never crashes the whole digest run -- only the type name is
    logged, never exception details that might embed cookie/session
    material.

    Beyond the fetch itself, this function is exception-proof end to end, at
    two distinct granularities:

    - Per-notification malformation (a non-numeric tweet id, a tweet missing
      `.full_text`, or any other single-notification data problem) is
      caught inside `_extract_candidates` and skipped with a counted log --
      gentle, never fails the page.
    - Pipeline-level surprises -- extracting a page's candidates raising
      (e.g. a broken custom notification iterator failing mid-page),
      fetching page 2+ raising, or the final item-construction step raising
      -- are each caught at the point they can occur, mirroring
      telegram.py's partial-results-survive behavior: `failed=True` is set,
      processing stops at that point, and the result is finalized from
      whatever items/cursor progress were already safely gathered from
      pages processed before the failure. None of this ever raises out of
      `collect` itself -- `_run_x_collector` in digest/main.py also wraps
      this call in a catch-all as a second line of defense, but `collect`
      does not rely on that backstop.
    """
    from twikit.errors import (
        AccountLocked,
        AccountSuspended,
        Forbidden,
        TooManyRequests,
        Unauthorized,
    )

    result = CollectResult()

    try:
        page = await client.get_notifications(_NOTIFICATION_TYPE)
    except (Unauthorized, Forbidden, AccountLocked, AccountSuspended) as exc:
        logger.warning("x auth/cookie error: %s", type(exc).__name__)
        result.failed = True
        return result
    except TooManyRequests as exc:
        logger.warning("x rate limited: %s", type(exc).__name__)
        result.failed = True
        return result
    except Exception as exc:
        logger.warning("x notification collection failed: %s", type(exc).__name__)
        result.failed = True
        return result

    fetched_at = datetime.now(UTC).isoformat()

    try:
        candidates, raw_count, skipped_no_tweet, skipped_no_screen_name, skipped_malformed = (
            _extract_candidates(page)
        )
    except Exception as exc:
        logger.warning(
            "x notification page processing failed on page 1: %s", type(exc).__name__
        )
        result.failed = True
        return result

    if skipped_no_tweet:
        logger.info("x notifications: skipped %d with no linkable tweet", skipped_no_tweet)
    if skipped_no_screen_name:
        logger.info(
            "x notifications: skipped %d with no author screen_name", skipped_no_screen_name
        )
    if skipped_malformed:
        logger.info("x notifications: skipped %d malformed notification(s)", skipped_malformed)

    if cursor is None:
        newest_in_page = max((c[0] for c in candidates), default=None)
        new_cursor = str(newest_in_page) if newest_in_page is not None else "0"
        logger.info("x notifications: first run, seeded cursor at %s", new_cursor)
        result.cursor_updates[("x", "notifications")] = new_cursor
        return result

    cursor_int = int(cursor)
    pages_fetched = 1
    pagination_failed = False
    bridged = _page_bridges_cursor(raw_count, candidates, cursor_int)

    while not bridged and pages_fetched < _MAX_NOTIFICATION_PAGES:
        next_cursor = getattr(page, "next_cursor", None)
        if not next_cursor:
            break

        await asyncio.sleep(_PAGE_FETCH_DELAY_SECONDS)
        try:
            page = await page.next()
        except Exception as exc:
            logger.warning(
                "x notification pagination failed on page %d: %s",
                pages_fetched + 1,
                type(exc).__name__,
            )
            result.failed = True
            pagination_failed = True
            break

        pages_fetched += 1
        try:
            (
                page_candidates,
                page_raw_count,
                page_skipped_no_tweet,
                page_skipped_no_screen_name,
                page_skipped_malformed,
            ) = _extract_candidates(page)
        except Exception as exc:
            logger.warning(
                "x notification page processing failed on page %d: %s",
                pages_fetched,
                type(exc).__name__,
            )
            result.failed = True
            pagination_failed = True
            break

        candidates.extend(page_candidates)
        if page_skipped_no_tweet:
            logger.info(
                "x notifications: skipped %d with no linkable tweet", page_skipped_no_tweet
            )
        if page_skipped_no_screen_name:
            logger.info(
                "x notifications: skipped %d with no author screen_name",
                page_skipped_no_screen_name,
            )
        if page_skipped_malformed:
            logger.info(
                "x notifications: skipped %d malformed notification(s)", page_skipped_malformed
            )
        bridged = _page_bridges_cursor(page_raw_count, page_candidates, cursor_int)

    if not bridged and not pagination_failed and pages_fetched >= _MAX_NOTIFICATION_PAGES:
        logger.warning(
            "x notifications: page cap reached, %d pages fetched; older notifications "
            "beyond the cap are skipped",
            pages_fetched,
        )

    try:
        newest_seen = max((c[0] for c in candidates), default=None)
        new_candidates = sorted((c for c in candidates if c[0] > cursor_int), key=lambda c: c[0])

        skipped_engagement = 0
        for tweet_id, text, screen_name, is_engagement in new_candidates:
            if is_engagement:
                skipped_engagement += 1
                continue
            result.items.append(
                Item(
                    source="x",
                    source_id=str(tweet_id),
                    chat_id=None,
                    author=screen_name,
                    text=text,
                    url=f"https://x.com/{screen_name}/status/{tweet_id}",
                    fetched_at=fetched_at,
                )
            )
        if skipped_engagement:
            logger.info(
                "x notifications: skipped %d engagement notification(s) as items "
                "(cursor still credits their tweet ids)",
                skipped_engagement,
            )

        if newest_seen is not None and newest_seen > cursor_int:
            result.cursor_updates[("x", "notifications")] = str(newest_seen)
    except Exception as exc:
        logger.warning("x notification item construction failed: %s", type(exc).__name__)
        result.failed = True
        return result

    logger.info(
        "x notifications: collected %d items across %d page(s), cursor -> %s",
        len(result.items),
        pages_fetched,
        result.cursor_updates.get(("x", "notifications"), cursor),
    )
    return result
