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
  unwrapping needed for a single page. Notifications within a page, and
  pages across successive ``.next()`` calls, are ordered newest-NOTIFICATION-
  time-first: client.py builds each page's list by iterating
  ``global_objects['notifications']`` in the order the raw API response's
  entries were declared, which is X's own (undocumented but empirically
  stable) newest-first notification-time ordering -- this is NOT a sort by
  linked-tweet id or tweet creation time, a distinction load-bearing for the
  cursor design below.
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
- ``Notification`` (notification.py) has ``.id`` (str), ``.timestamp_ms``
  (``int`` -- ``self.timestamp_ms: int = int(data['timestampMs'])`` at
  construction time, i.e. the notification's OWN creation time in
  milliseconds since epoch; unlike ``.id`` this is always present and
  always numeric, never tied to whether a tweet is linked), ``.message``
  (str), ``.tweet`` (``Tweet | None`` -- ``None`` for notifications with no
  linked tweet, e.g. a pure follow event), ``.from_user`` (``User | None``),
  and ``.icon`` (``dict``, built directly from the raw API response's
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

Cursor axis (Codex review findings P1-a/P1-b -- see `collect`'s docstring
for the full contract): the ("x", "notifications") cursor stores a
**notification timestamp_ms** (stringified int, milliseconds since epoch),
NOT a tweet id. Tweet ids are only ever used as `source_id`/url material
now. Nothing has ever been deployed and no live run has happened against
the old tweet-id-cursor scheme, so there is deliberately NO migration/
back-compat handling for old cursor values here -- a fresh cursor value
under the new unit is all that's needed.
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
    owner posted AFTER the cursor would otherwise pass a naive age check
    and leak the owner's own tweet text into the digest as if it were
    incoming content. The notification's KIND is the discriminator here,
    not any timestamp or tweet-age comparison (see `.icon`, module
    docstring) -- kind and chronology are orthogonal: an engagement
    notification's timestamp still counts fully for cursor/bridging
    purposes (see `collect`'s docstring), only its ELIGIBILITY AS AN ITEM
    is affected by kind.

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
) -> tuple[list[int], list[tuple[int, int, str, str]], int, int, int, int, int]:
    """Per-page chronology + item candidates from one page of notifications.

    Returns (page_timestamps, content_candidates, raw_count, skipped_no_tweet,
    skipped_no_screen_name, skipped_malformed, skipped_engagement).

    ``page_timestamps`` is the notification ``timestamp_ms`` of EVERY
    notification on this page whose timestamp could be read -- collected
    unconditionally, regardless of kind (engagement or not) or tweet
    linkage. This is the sole input to `_page_bridges_cursor` and to
    `collect`'s cursor-advancement math: chronology is kind-agnostic (see
    `collect`'s docstring) -- a tweetless follow notification or an
    engagement notification contributes its timestamp to bridging/cursor
    exactly like a mention does, even though neither becomes an item.

    ``content_candidates`` is ``(timestamp_ms, tweet_id, text,
    screen_name)`` for the strict subset of notifications eligible to
    become digest items: non-engagement, with a linked tweet whose id/
    text/author screen_name could all be read. `collect` still applies the
    ``timestamp_ms > cursor`` chronology filter on top of this -- kind and
    chronology are two independent filters applied in sequence, not one
    combined check.

    ``raw_count`` is every notification the iterator actually yielded on
    this page (used by `_page_bridges_cursor`'s "page is empty" check),
    which can exceed `len(content_candidates)` for several independent
    reasons counted separately: tweetless, no author screen_name,
    malformed, or (non-malformed) engagement-kind.

    Split out of `collect` so the same filtering runs identically for every
    page fetched during pagination, not just the first.

    Reading `timestamp_ms` happens FIRST, in its own try/except, separate
    from reading the linked tweet's fields -- a malformed/non-numeric tweet
    id or a tweet missing `.full_text` must not suppress a notification's
    (otherwise perfectly readable) contribution to chronology. Only a
    notification whose OWN `timestamp_ms` can't be read at all is excluded
    from `page_timestamps` (counted as malformed) -- an edge case expected
    to be vanishingly rare, since real twikit `Notification`s set this
    eagerly and unconditionally at construction (module docstring).

    Per-notification malformation of the tweet fields (a non-numeric tweet
    id, a tweet missing `.full_text`, or any other single-notification data
    problem) is caught HERE and skipped with a counted log -- gentle, never
    fails the page (Codex review finding A). By contrast, the surrounding
    `for` loop itself raising (e.g. a broken custom iterator failing
    mid-page) is deliberately NOT caught here: that's a pipeline-level
    surprise, and is left to propagate to `collect`'s own try/except around
    each page's extraction, which sets `failed=True` while keeping whatever
    prior pages (or prior candidates on this page) were already gathered.
    """
    page_timestamps: list[int] = []
    content_candidates: list[tuple[int, int, str, str]] = []
    raw_count = 0
    skipped_no_tweet = 0
    skipped_no_screen_name = 0
    skipped_malformed = 0
    skipped_engagement = 0
    for notification in notifications:
        raw_count += 1
        try:
            timestamp_ms = int(notification.timestamp_ms)
        except Exception as exc:
            logger.info(
                "x notifications: skipped malformed notification: %s", type(exc).__name__
            )
            skipped_malformed += 1
            continue
        page_timestamps.append(timestamp_ms)

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

        if is_engagement:
            skipped_engagement += 1
            continue

        content_candidates.append((timestamp_ms, tweet_id, text, screen_name))
    return (
        page_timestamps,
        content_candidates,
        raw_count,
        skipped_no_tweet,
        skipped_no_screen_name,
        skipped_malformed,
        skipped_engagement,
    )


def _page_bridges_cursor(
    page_notification_count: int,
    page_timestamps: list[int],
    cursor_int: int,
) -> bool:
    """True once this page reaches (or passes) the cursor -- pagination can stop.

    Bridged when either: this page contains a notification whose
    `timestamp_ms` is <= cursor (everything from that notification onward,
    in newest-first order, is already-seen ground), or the page came back
    with zero raw notifications (nothing left to page through). This check
    is deliberately kind-agnostic and tweet-linkage-agnostic -- it runs
    over `page_timestamps` (every notification's timestamp, see
    `_extract_candidates`), not over `content_candidates` (only the subset
    eligible to become items). See `collect`'s docstring for why: bridging
    is about NOTIFICATION chronology, not about what any given notification
    happens to be about.
    """
    if page_notification_count == 0:
        return True
    return any(timestamp_ms <= cursor_int for timestamp_ms in page_timestamps)


async def collect(client: XClientLike, cursor: str | None) -> CollectResult:
    """Fetch the notifications timeline since `cursor`, paginating as needed.

    First run (cursor is None): fetch a single page and seed the cursor from
    it -- no pagination needed to seed from "now" (see below).

    Otherwise: the notifications API is newest-first-only (no way to ask for
    "oldest new item first"), so unlike telegram.py's oldest-first fetching,
    catching up after a backlog means walking pages *from the newest
    downward* until either a page bridges the cursor (see below) or a hard
    cap of `_MAX_NOTIFICATION_PAGES` (5) pages is reached. Each additional
    page fetch is preceded by `asyncio.sleep(_PAGE_FETCH_DELAY_SECONDS)`
    (1.5s) -- gentleness, so a backlog catch-up doesn't burst several
    requests back to back (ban-risk mitigation, PLAN.md §8). If the cap is
    hit while still not bridged, a warning is logged naming how many pages
    were fetched: older notifications beyond the cap are skipped for this
    run (visible truncation, not silent data loss) -- see the cursor
    advancement rules below for how the cursor is chosen in that case so a
    future run can still make progress on the skipped region, though a
    sufficiently large sustained backlog can still outrun the cap.

    Cursor axis -- why notification timestamp, not tweet id (Codex review
    findings P1-a/P1-b): an earlier version of this module used the linked
    tweet's id as the cursor axis, bridging pagination once a page's tweet
    id was <= the cursor. That is UNSOUND, because the 'All' notifications
    timeline is ordered by NOTIFICATION time, not by linked-tweet id or
    tweet creation time:

    - (P1-a) A fresh like/repost on an OLD tweet (e.g. someone likes a post
      from months ago) creates a notification that is newest-first on the
      timeline TODAY, but carries an old, small tweet id. Comparing that
      tweet id against the cursor could satisfy "candidate <= cursor" and
      falsely bridge pagination on page 1, even while an unseen mention
      (with a genuinely new tweet, and thus a large id) sits unread on page
      2 -- pagination would stop too early and silently lose it.
    - (P1-b) Symmetrically, advancing the cursor to "the newest tweet id
      seen" after an unbridged cap-hit skips past the entire unfetched gap
      permanently: the next run's very first page would already contain
      tweet ids <= that inflated cursor (since engagement re-surfaces old
      tweet ids at new notification times, same root cause as P1-a),
      bridging instantly and never revisiting the skipped middle.

    Both failure modes share one cause: tweet id/age is not the axis
    pagination walks. Notification timestamp is. The redesigned contract
    below uses `Notification.timestamp_ms` (confirmed `int`, milliseconds
    since epoch -- module docstring) as the cursor axis throughout, scoped
    to ("x", "notifications") same as before. Tweet ids remain ONLY as
    `source_id`/url material -- and as the de-duplication key via the
    `(source, source_id)` UNIQUE constraint (digest/state.py) -- never as a
    comparison axis. This module is new/unreleased (no live X run has ever
    happened), so there is deliberately no migration/back-compat handling
    for the old tweet-id cursor values here.

    - Item selection: a CONTENT (non-engagement, see
      `_is_engagement_notification` / Codex review finding B) notification
      with a linked tweet becomes an item iff its own `timestamp_ms` is
      strictly greater than the cursor. A like/repost on a tweet the owner
      posted after the cursor is still excluded here by KIND, independent
      of this timestamp check -- excluding it from items but not from
      chronology (next bullet) is deliberate. An old tweet freshly
      mentioned/quoted is a NEW notification with a NEW timestamp linking a
      NEW tweet -- correctly included via this same check, no special
      casing needed.
    - Bridge condition (pagination can stop): the current page contains ANY
      notification -- content or engagement, tweet-linked or not -- whose
      `timestamp_ms` is <= cursor (we've walked back into already-seen
      territory), or the page came back empty. This is intentionally
      kind-agnostic and tweet-linkage-agnostic: chronology doesn't care
      what a notification is about.
    - Cursor advancement, three cases:
        1. Bridged (including an empty page): cursor := max(newest
           notification timestamp_ms seen across all pages fetched this
           run, existing cursor). Mirrors telegram.py's "cursor advances
           past textless messages too", just scoped to notification
           timestamps instead of message ids.
        2. Cap hit while still UNBRIDGED (the deliberate truncation case,
           not a failure): cursor := the OLDEST notification timestamp_ms
           fetched in this batch -- NOT the newest. This preserves the gap
           below the fetched region instead of skipping past it (the P1-b
           fix): a future run re-walks the just-fetched (now already-seen)
           region -- harmless, since re-emitted items are deduplicated for
           free by the `(source, source_id)` UNIQUE constraint downstream
           -- and, as long as new-arrival volume stays under the cap's
           capacity, makes forward progress draining the older backlog
           rather than abandoning it outright. The oldest-fetched
           timestamp is, BY CONSTRUCTION, always > the existing cursor
           here: `_page_bridges_cursor` returning False for every page
           fetched this run means every gathered timestamp (including its
           minimum) is > cursor_int, or bridging would already have
           happened. This is asserted at the call site rather than folded
           into a `max(oldest, cursor)` -- a `max()` would silently paper
           over a violation of that invariant instead of surfacing it.
        3. A genuine pipeline-level failure mid-pagination, OR pagination
           stopping because the API itself ran out of pages (`next_cursor`
           came back falsy -- twikit's own signal for "no more pages",
           module docstring) BEFORE the cap was reached, both use the SAME
           "advance to newest" formula as the bridged case, not the
           gap-preserving one. Neither is the deliberate, bounded
           truncation the cap exists for: a pipeline failure's partial
           results are exactly as trustworthy as a normal bridge, and
           running out of real pages means there genuinely is no more
           history below what was fetched -- no gap to preserve.
    - First run (cursor is None): fetch a single page (no pagination) and
      seed the cursor from the newest `timestamp_ms` anywhere on it, with
      NO items emitted -- consistent with telegram.py's no-history-backfill
      rule (the first digest starts from "now"). Seeding no longer depends
      on any notification having a linked tweet: every notification carries
      a timestamp regardless of kind or tweet-linkage, so ANY non-empty
      page seeds from its newest timestamp. Only a genuinely empty page (or
      one where every notification's timestamp itself is unreadable) has no
      candidate to seed from -- seed "0" so the scope isn't re-treated as
      first-run forever.
    - Notifications without a linked tweet, or whose tweet's author
      screen_name is unavailable, are skipped from ITEMS (logged as a
      count, never individually -- PLAN.md: no linkable content to
      include, and a url cannot be built without a screen_name) but their
      timestamp still counts fully for cursor/bridging chronology -- see
      the kind-agnostic bridge condition above.

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
      pages processed before the failure (see cursor-advancement case 3
      above). None of this ever raises out of `collect` itself --
      `_run_x_collector` in digest/main.py also wraps this call in a
      catch-all as a second line of defense, but `collect` does not rely on
      that backstop.
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
        (
            page_timestamps,
            candidates,
            raw_count,
            skipped_no_tweet,
            skipped_no_screen_name,
            skipped_malformed,
            skipped_engagement,
        ) = _extract_candidates(page)
    except Exception as exc:
        logger.warning(
            "x notification page processing failed on page 1: %s", type(exc).__name__
        )
        result.failed = True
        return result

    _log_skip_counts(
        skipped_no_tweet, skipped_no_screen_name, skipped_malformed, skipped_engagement
    )

    if cursor is None:
        newest_in_page = max(page_timestamps, default=None)
        new_cursor = str(newest_in_page) if newest_in_page is not None else "0"
        logger.info("x notifications: first run, seeded cursor at %s", new_cursor)
        result.cursor_updates[("x", "notifications")] = new_cursor
        return result

    cursor_int = int(cursor)
    pages_fetched = 1
    pagination_failed = False
    exhausted_naturally = False
    all_timestamps = list(page_timestamps)
    all_candidates = list(candidates)
    bridged = _page_bridges_cursor(raw_count, page_timestamps, cursor_int)

    while not bridged and pages_fetched < _MAX_NOTIFICATION_PAGES:
        next_cursor = getattr(page, "next_cursor", None)
        if not next_cursor:
            # twikit's own "no more pages" signal (module docstring) --
            # genuinely nothing further to fetch, not a cap truncation, so
            # there's no gap below this point to preserve (case 3 below).
            exhausted_naturally = True
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
                page_timestamps,
                page_candidates,
                page_raw_count,
                page_skipped_no_tweet,
                page_skipped_no_screen_name,
                page_skipped_malformed,
                page_skipped_engagement,
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

        all_timestamps.extend(page_timestamps)
        all_candidates.extend(page_candidates)
        _log_skip_counts(
            page_skipped_no_tweet,
            page_skipped_no_screen_name,
            page_skipped_malformed,
            page_skipped_engagement,
        )
        bridged = _page_bridges_cursor(page_raw_count, page_timestamps, cursor_int)

    if not bridged and not pagination_failed and pages_fetched >= _MAX_NOTIFICATION_PAGES:
        oldest_for_log = min(all_timestamps) if all_timestamps else None
        logger.warning(
            "x notifications: page cap reached, %d pages fetched; older notifications "
            "beyond the cap are skipped; cursor set to the oldest fetched timestamp "
            "(%s) rather than the newest, to preserve that gap for a future run instead "
            "of skipping past it",
            pages_fetched,
            oldest_for_log,
        )

    try:
        new_items = [
            Item(
                source="x",
                source_id=str(tweet_id),
                chat_id=None,
                author=screen_name,
                text=text,
                url=f"https://x.com/{screen_name}/status/{tweet_id}",
                fetched_at=fetched_at,
            )
            for timestamp_ms, tweet_id, text, screen_name in sorted(
                all_candidates, key=lambda c: c[0]
            )
            if timestamp_ms > cursor_int
        ]
        result.items = new_items

        if bridged or pagination_failed or exhausted_naturally:
            # Bridged, a genuine pipeline failure, or a natural end of the
            # timeline (twikit reporting no further pages) -- none of these
            # is the deliberate, bounded cap truncation case 2 preserves a
            # gap for, so the ordinary "advance to newest" formula applies.
            newest_seen = max(all_timestamps, default=None)
            if newest_seen is not None and newest_seen > cursor_int:
                result.cursor_updates[("x", "notifications")] = str(newest_seen)
        elif all_timestamps:
            oldest_seen = min(all_timestamps)
            # See cursor-advancement case 2 in this function's docstring:
            # `bridged` is False here, which means every timestamp gathered
            # this run -- including `oldest_seen`, its minimum -- is > the
            # existing cursor, or `_page_bridges_cursor` would have already
            # returned True. Asserted (rather than computed via
            # `max(oldest_seen, cursor_int)`) so a future violation of that
            # invariant raises loudly instead of silently discarding this
            # branch's gap-preserving cursor choice.
            assert oldest_seen > cursor_int, (
                "unbridged cap-hit invariant violated: oldest fetched timestamp "
                f"{oldest_seen} <= existing cursor {cursor_int} (should have bridged)"
            )
            result.cursor_updates[("x", "notifications")] = str(oldest_seen)
        # else: not bridged, not a pagination failure, not naturally
        # exhausted (i.e. this really is the cap-hit case), and no timestamp
        # was readable at all across every page fetched this run (every
        # notification was malformed) -- nothing trustworthy to advance the
        # cursor from; leave it untouched rather than guess.
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


def _log_skip_counts(
    skipped_no_tweet: int,
    skipped_no_screen_name: int,
    skipped_malformed: int,
    skipped_engagement: int,
) -> None:
    """Shared counted-skip logging for one page's `_extract_candidates` output.

    Split out since `collect` calls `_extract_candidates` once for page 1
    and again per paginated page, needing identical logging each time.
    """
    if skipped_no_tweet:
        logger.info("x notifications: skipped %d with no linkable tweet", skipped_no_tweet)
    if skipped_no_screen_name:
        logger.info(
            "x notifications: skipped %d with no author screen_name", skipped_no_screen_name
        )
    if skipped_malformed:
        logger.info("x notifications: skipped %d malformed notification(s)", skipped_malformed)
    if skipped_engagement:
        logger.info(
            "x notifications: skipped %d engagement notification(s) as items "
            "(their timestamps still counted for cursor/bridging chronology)",
            skipped_engagement,
        )
