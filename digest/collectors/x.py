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
- ``Notification`` (notification.py) has ``.id`` (str), ``.message`` (str),
  ``.tweet`` (``Tweet | None`` -- ``None`` for notifications with no linked
  tweet, e.g. a pure follow event), and ``.from_user`` (``User | None``).
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


async def collect(client: XClientLike, cursor: str | None) -> CollectResult:
    """Fetch ONE page of the notifications timeline since `cursor`.

    No pagination loop -- `.next()` on the twikit Result is never called.
    Gentleness (one page, once per scheduled run) is a ban-risk mitigation
    (PLAN.md §8); the 3-hourly cadence is the drain, not an internal retry
    or pagination loop in this module.

    Cursor/idempotency contract (mirrors telegram.py's, scoped to
    ("x", "notifications") instead of per-chat):

    - Tweet ids are snowflakes (monotonically increasing), so they are
      comparable as integers. The cursor tracks the newest tweet id seen
      among notifications that HAVE a linked tweet -- a bare notification
      id is not used for comparison since its own format is not confirmed
      to be a clean numeric snowflake, whereas a linked Tweet's `.id` is
      (see module docstring).
    - First run (cursor is None): seed the cursor from the newest such
      tweet id in this page and emit NO items -- consistent with
      telegram.py's no-history-backfill rule (the first digest starts from
      "now"). If the page is empty, or every notification in it lacks a
      linked tweet, there is no candidate id to seed from -- seed "0" so
      the scope isn't re-treated as first-run forever.
    - Otherwise: emit items for every notification whose linked tweet id is
      > cursor, oldest-first (ascending by tweet id) to match the item
      ordering convention used elsewhere in this codebase. The cursor
      advances to the newest tweet id SEEN in this page (not just the ones
      that became items) as long as it's actually newer than the current
      cursor -- mirrors telegram.py's "cursor advances past textless
      messages too" behavior, just for tweetless notifications instead of
      textless messages.
    - Notifications without a linked tweet, or whose tweet's author
      screen_name is unavailable, are skipped from BOTH items and cursor
      candidacy -- logged as a count, never individually (PLAN.md: no
      linkable content to include, and a url cannot be built without a
      screen_name).

    Error handling (PLAN.md §4.3): auth/cookie failures (`Unauthorized`,
    `Forbidden`, `AccountLocked`, `AccountSuspended`) and rate limiting
    (`TooManyRequests`) both set `failed=True` and return immediately --
    NO retry, NO re-login attempt; a silent retry on an unofficial,
    cookie-based API risks tripping X's automation detection further, and
    the next scheduled run (3h later) is the retry. Any other exception
    (e.g. a GraphQL/endpoint shape change) is caught the same way so a
    twikit break never crashes the whole digest run -- only the type name
    is logged, never exception details that might embed cookie/session
    material.
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
        notifications = await client.get_notifications(_NOTIFICATION_TYPE)
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

    candidates: list[tuple[int, Any, str]] = []  # (tweet_id, tweet, screen_name)
    skipped_no_tweet = 0
    skipped_no_screen_name = 0
    for notification in notifications:
        tweet, screen_name = _tweet_and_screen_name(notification)
        if tweet is None:
            skipped_no_tweet += 1
            continue
        if not screen_name:
            skipped_no_screen_name += 1
            continue
        candidates.append((int(tweet.id), tweet, screen_name))

    if skipped_no_tweet:
        logger.info("x notifications: skipped %d with no linkable tweet", skipped_no_tweet)
    if skipped_no_screen_name:
        logger.info(
            "x notifications: skipped %d with no author screen_name", skipped_no_screen_name
        )

    newest_in_page = max((c[0] for c in candidates), default=None)

    if cursor is None:
        new_cursor = str(newest_in_page) if newest_in_page is not None else "0"
        logger.info("x notifications: first run, seeded cursor at %s", new_cursor)
        result.cursor_updates[("x", "notifications")] = new_cursor
        return result

    cursor_int = int(cursor)
    new_candidates = sorted((c for c in candidates if c[0] > cursor_int), key=lambda c: c[0])

    for tweet_id, tweet, screen_name in new_candidates:
        result.items.append(
            Item(
                source="x",
                source_id=str(tweet_id),
                chat_id=None,
                author=screen_name,
                text=tweet.full_text,
                url=f"https://x.com/{screen_name}/status/{tweet_id}",
                fetched_at=fetched_at,
            )
        )

    if newest_in_page is not None and newest_in_page > cursor_int:
        result.cursor_updates[("x", "notifications")] = str(newest_in_page)

    logger.info(
        "x notifications: collected %d items, cursor -> %s",
        len(result.items),
        result.cursor_updates.get(("x", "notifications"), cursor),
    )
    return result
