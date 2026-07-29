"""X (Twitter) collector: fetch new notification-timeline entries via twifork.

See PLAN.md §4.3 for the full spec. twikit (import name -- see "Library" below)
is imported LAZILY, inside functions only -- never at module level -- so that
X_ENABLED=false runs have zero import side effects (Phase 3 acceptance
criterion). Error handling and cursor/idempotency rules are load-bearing --
read the docstrings on `collect` before changing this file.

Library: twifork, not twikit -- why (verified live, not from memory)
----------------------------------------------------------------------
Published `twikit` 2.3.3 (Feb 2025) is dead: X changed their webpack bundle
in March 2026, so twikit's transaction-ID signing raises
``Exception: Couldn't get KEY_BYTE indices`` before any auth is even
attempted. `twifork` 2.3.5 (2026-07-05) is an actively maintained fork that
fixes exactly that -- it still imports as ``twikit`` (see pyproject.toml's
dependency comment for the audit/pinning rationale), so no import lines in
this module change on account of the switch.

Why this module bypasses twifork's model layer entirely
----------------------------------------------------------------------
twifork's higher-level ``client.get_notifications(...)`` fixes the dead
transaction-signing bug, but its model layer still breaks on a separate,
unrelated fault: X now returns ``user.location`` as a plain string while
twifork's own ``User.__init__`` calls ``.get()`` on it (a leftover
assumption that ``location`` is always a dict), so ``get_notifications()``
raises ``AttributeError`` AFTER a successful, authenticated fetch --
whenever a notification's linked tweet's author has a `location` string set
on their profile. Rather than patch around (or wait on) that one-off model
bug, this module drops down to the underlying REST transport
(``client.v11.notifications_all``, confirmed by reading the installed
twifork package source -- .venv/lib/python3.12/site-packages/twikit/) and
parses the raw JSON response itself, never touching twifork's
``Notification``/``Tweet``/``User`` classes at all. This has a second,
durable benefit beyond dodging today's bug: this module becomes immune to
*future* model-layer breakage too, since it never constructs those objects
in the first place -- only the raw JSON shape (`globalObjects`, confirmed
stable across the notifications_all/verified/mentions family) has to hold.

Raw response shape (confirmed by reading twifork's own
``client/v11.py``/``client/client.py`` source, corroborated by a live
call):

- ``resp, _ = await client.v11.notifications_all(count, cursor)`` -- ``count``
  (int, how many notifications to request -- ``_NOTIFICATION_COUNT`` below,
  40, matching twikit's own historical default) and ``cursor`` (str | None,
  the API's OWN opaque pagination token -- entirely distinct from this
  module's persisted notification-timestamp cursor, see "Cursor axis"
  below; ``None`` for the first/freshest page). Covers mentions/replies/
  likes/follows etc. -- the PLAN's "notifications timeline" scope (§4.3,
  decision log #4: notifications only, no home timeline); this is the exact
  transport twifork's own ``get_notifications(..., type='All', ...)`` calls
  internally, so nothing about the fetched SCOPE changes versus before,
  only how the response is consumed.
- ``resp['globalObjects']['notifications']`` -- dict of id -> raw
  notification dict, each with:
    - ``['timestampMs']`` (str or int -- always coerced via ``int()``; this
      is the notification's OWN creation time in milliseconds since epoch,
      never tied to whether a tweet is linked)
    - ``['icon']['id']`` (e.g. ``'heart_icon'``, ``'retweet_icon'``,
      ``'bell_icon'`` -- see "Kind mapping" below)
    - ``['message']['text']`` (str -- the notification's free-text display
      message, e.g. "X liked your Tweet". Deliberately NOT stored in
      `_RawNotification` or used anywhere in this module, same as before
      this rewrite: parsing human-readable, locale-dependent notification
      text is brittle across languages/wording changes, whereas `.icon`'s
      id is a stable-ish machine token -- not worth the fragility for a
      fail-open-anyway path.)
    - ``['template']['aggregateUserActionsV1']['targetObjects']`` -- list;
      ``[0]['tweet']['id']`` is the linked tweet's id when present (absent/
      empty for notifications with no linked tweet, e.g. a pure follow
      event).
- ``resp['globalObjects']['tweets']`` -- dict of tweet id -> raw tweet dict,
  with (at minimum) ``'user_id_str'`` and ``'full_text'``.
- ``resp['globalObjects']['users']`` -- dict of user id -> raw user dict,
  with (at minimum) ``'screen_name'``.
- Next-page cursor: recursively find the FIRST ``'entries'`` key anywhere in
  `resp`, take the entry (if any) whose ``entryId`` starts with
  ``'cursor-bottom'``, and recursively find the FIRST ``'value'`` key inside
  THAT entry. Absent (no cursor-bottom entry, or no entries at all) means
  "no more pages" -- ``None``. This module implements its own small
  recursive finder (`_find_first`, directly unit-tested) rather than
  importing twifork's internal ``utils.find_dict`` -- that helper is not
  part of any documented/stable public surface, and the traversal needed
  here is simple enough not to warrant depending on an internal.
- Exceptions (``twikit.errors``, unchanged by the twifork fork -- confirmed
  by reading the installed package): ``Unauthorized`` (401), ``Forbidden``
  (403), ``AccountLocked`` (Arkose challenge lock), ``AccountSuspended`` --
  all auth/cookie-failure signals per PLAN §4.3 ("account may be locked/
  challenged"). ``TooManyRequests`` (429) is the rate-limit signal. Neither
  twikit nor twifork retries these internally when no ``captcha_solver`` is
  configured, which this module never configures -- so a single failed
  call here really is a single network round trip, matching the "no retry
  loop" requirement.

Kind mapping (Codex review finding B -- distinguishing engagement
notifications, e.g. likes/reposts on the owner's OWN tweets, from
notifications that carry genuine incoming content, e.g. mentions/replies/
quotes): the id strings below are grounded in X's own (undocumented)
notification-icon vocabulary observed in the wild, NOT in any twikit/
twifork source -- this module treats them as best-effort, not
guaranteed-stable:
  - ``heart_icon`` -- like
  - ``retweet_icon`` -- repost/retweet
Only these two ids are classified as "engagement" (see
``_is_engagement_icon``). Everything else -- a mention's or reply's icon
id, an icon id this module has never seen, or a missing/non-dict ``icon``
-- is treated as content and fails OPEN (included as an item): an
occasional own-tweet leaking through is more recoverable than a silently
dropped mention.

Cursor axis (Codex review findings P1-a/P1-b -- see `collect`'s docstring
for the full contract): the ("x", "notifications") cursor stores a
**notification timestamp_ms** (stringified int, milliseconds since epoch),
NOT a tweet id. Tweet ids are only ever used as `source_id`/url material
now. Nothing has ever been deployed and no live run has happened against
the old tweet-id-cursor scheme, so there is deliberately NO migration/
back-compat handling for old cursor values here -- a fresh cursor value
under the new unit is all that's needed. This axis, and everything built on
it (bridging, systemic-failure detection, cap truncation, tie handling), is
this module's OWN design and is completely independent of whether
notifications are fetched via twifork's models or via raw JSON -- the
rewrite in this file changes ONLY how a page's records are obtained and
parsed, never the algorithm built on top of them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from digest.collectors.telegram import CollectResult
from digest.state import Item

logger = logging.getLogger(__name__)

# Notifications requested per page -- matches twikit's own historical
# default for get_notifications(); nothing about this module's scope
# depends on a specific value, this just keeps page sizes familiar.
_NOTIFICATION_COUNT = 40

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


class _V11ClientLike(Protocol):
    """Minimal duck-typed surface of twifork's ``Client.v11`` this module needs."""

    async def notifications_all(self, count: int, cursor: str | None) -> tuple[dict, Any]: ...


class XClientLike(Protocol):
    """Minimal duck-typed surface of a twifork Client this module needs.

    Only the raw ``v11`` REST transport is used -- see the module docstring
    for why this module bypasses twifork's higher-level
    `get_notifications`/model layer entirely and parses the raw JSON
    response itself. Kept loose deliberately so tests can inject a fake
    client without depending on real twifork/network objects.
    """

    v11: _V11ClientLike


def build_client(cookies_path: str | None, cookies_inline: str | None) -> Any:
    """Build a twifork Client authenticated via persisted cookies.

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

    twikit (the import name -- see module docstring for why the actual
    dependency is the `twifork` package) is imported here, not at module
    level -- see this module's docstring for why that matters for
    X_ENABLED=false runs.
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


@dataclass(frozen=True)
class _RawNotification:
    """One notification's fields, extracted directly from raw JSON.

    Deliberately flat and twifork-model-free (see module docstring) -- this
    is the ONLY shape `_extract_candidates`/`collect` consume from a parsed
    page, so nothing downstream of `parse_notifications_page` ever touches
    a twifork `Notification`/`Tweet`/`User` object.

    ``tweet_id``/``screen_name``/``text`` are independently optional: a
    notification can have a tweet reference but an unresolvable tweet (no
    ``full_text`` in ``globalObjects.tweets``, or the referenced tweet
    missing from that dict entirely) or an unresolvable author -- see
    `_parse_one_notification`'s docstring for exactly how each is derived
    and what `None` means for each field.
    """

    timestamp_ms: int
    icon_id: str | None
    tweet_id: str | None
    screen_name: str | None
    text: str | None


def _find_first(obj: Any, key: str) -> Any:
    """Depth-first search for the first occurrence of `key` in nested dict/list data.

    Returns `None` if `key` is never found anywhere in `obj`. Deliberately
    minimal and local rather than importing twifork's own internal
    ``utils.find_dict`` (module docstring) -- this module only ever needs
    the FIRST match, never the full multi-match list `find_dict` returns.

    Used for exactly two things in this module, both inside
    `_extract_next_cursor`: locating the first `'entries'` list anywhere in
    a raw response, and locating the first `'value'` inside one already-
    matched cursor-bottom entry dict.
    """
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = _find_first(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_first(item, key)
            if found is not None:
                return found
    return None


def _extract_next_cursor(resp: dict) -> str | None:
    """The API's own next-page pagination token, or None if exhausted.

    Mirrors twifork's own (internal) cursor-bottom extraction exactly (see
    module docstring's "Next-page cursor" fact): find the first `'entries'`
    list anywhere in `resp`, find the entry whose `entryId` starts with
    `'cursor-bottom'`, and pull that entry's first `'value'`. Any missing
    piece along the way (no entries, no cursor-bottom entry, no value) is
    treated as "no more pages" -- `None` -- never raised; defensive
    throughout since X changes response shapes without notice.

    This is an opaque API-internal pagination token, entirely distinct from
    this module's own persisted notification-timestamp cursor (see module
    docstring, "Cursor axis") -- never compared against or derived from it.
    """
    entries = _find_first(resp, "entries")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("entryId")
        if isinstance(entry_id, str) and entry_id.startswith("cursor-bottom"):
            value = _find_first(entry, "value")
            return value if isinstance(value, str) else None
    return None


def _extract_tweet_ref(notification: dict) -> str | None:
    """The linked tweet's id from one raw notification dict, or None.

    Defensive `.get()` chain the whole way down
    (`template.aggregateUserActionsV1.targetObjects[0].tweet.id`) -- a pure
    follow event (or any notification shape this module hasn't seen) simply
    has no linked tweet, which is not an error. The id is coerced to `str`
    (X's JSON already represents snowflake ids as strings, but this module
    doesn't rely on that -- see module docstring, ids are never compared
    numerically anywhere).
    """
    if not isinstance(notification, dict):
        return None
    template = notification.get("template")
    user_actions = template.get("aggregateUserActionsV1") if isinstance(template, dict) else None
    target_objects = (
        user_actions.get("targetObjects") if isinstance(user_actions, dict) else None
    )
    if not isinstance(target_objects, list) or not target_objects:
        return None
    first_target = target_objects[0]
    tweet_ref = first_target.get("tweet") if isinstance(first_target, dict) else None
    if not isinstance(tweet_ref, dict):
        return None
    tweet_id = tweet_ref.get("id")
    return str(tweet_id) if tweet_id is not None else None


def _parse_one_notification(
    notification: dict, tweets: dict, users: dict
) -> _RawNotification | None:
    """Parse one raw notification dict into a `_RawNotification`, or None.

    Returns `None` only when the notification's OWN `timestampMs` can't be
    read at all (missing, or not int-coercible) -- an edge case expected to
    be vanishingly rare, since real API responses set this eagerly and
    unconditionally per notification. This is the ONLY case
    `parse_notifications_page` drops a record silently; every other field
    below fails open into `None` on its own, since a notification whose
    timestamp IS readable must still count toward chronology/bridging
    regardless of whether its tweet/author could be resolved (see
    `collect`'s docstring -- chronology is kind- and linkage-agnostic).

    ``tweet_id`` reflects structural presence of a tweet reference in the
    notification itself (via `_extract_tweet_ref`) -- independent of
    whether that tweet can actually be resolved via `tweets`/`users`.
    ``screen_name``/``text`` are only populated when the full lookup chain
    (notification -> tweet id -> `tweets` entry -> `user_id_str` -> `users`
    entry -> `screen_name`; notification -> tweet id -> `tweets` entry ->
    `full_text`) resolves cleanly -- any missing link anywhere in either
    chain leaves the corresponding field `None` rather than raising.
    `_extract_candidates` is what turns these `None`s into the counted skip
    categories (no tweet / no screen_name / malformed) -- this function
    itself never counts or logs anything, staying pure.
    """
    if not isinstance(notification, dict):
        return None
    raw_timestamp = notification.get("timestampMs")
    if raw_timestamp is None:
        return None
    try:
        timestamp_ms = int(raw_timestamp)
    except (TypeError, ValueError):
        return None

    icon = notification.get("icon")
    icon_id = icon.get("id") if isinstance(icon, dict) else None

    tweet_id = _extract_tweet_ref(notification)
    screen_name: str | None = None
    text: str | None = None
    if tweet_id is not None:
        tweet_data = tweets.get(tweet_id)
        if isinstance(tweet_data, dict):
            raw_text = tweet_data.get("full_text")
            text = raw_text if isinstance(raw_text, str) else None
            user_id = tweet_data.get("user_id_str")
            user_data = users.get(user_id) if user_id is not None else None
            if isinstance(user_data, dict):
                raw_screen_name = user_data.get("screen_name")
                screen_name = raw_screen_name if isinstance(raw_screen_name, str) else None

    return _RawNotification(
        timestamp_ms=timestamp_ms,
        icon_id=icon_id if isinstance(icon_id, str) else None,
        tweet_id=tweet_id,
        screen_name=screen_name,
        text=text,
    )


def parse_notifications_page(resp: dict) -> tuple[list[_RawNotification], str | None]:
    """Pure parse of one raw `v11.notifications_all` response page.

    Returns (records, next_cursor). PURE -- no I/O, no logging, never
    raises -- so it's directly unit-testable against captured payloads
    without a fake client at all. Defensive `.get()`/`isinstance` checks
    throughout, at every level of `resp`'s structure: X changes response
    shapes without notice, and this function must degrade to "fewer/no
    records" rather than ever propagate an exception.

    Per-notification parse failures inside it are silent by design: a
    notification whose own `timestampMs` can't be read at all is simply
    left out of the returned list (see `_parse_one_notification`'s
    docstring) -- this function does not itself count or log that drop.
    Instead, the caller (`_extract_candidates`) derives the count by
    comparing this function's output length against
    `len(resp['globalObjects']['notifications'])` (the raw per-page total),
    and folds it into the SAME "malformed" counter -- and, transitively,
    the same systemic-failure detection (`_is_systemic_tweet_link_failure`)
    -- that already exists for the (structurally different, but equally
    "some notifications on this page couldn't be normalized") case of a
    tweet-linked notification whose author/text couldn't be resolved. One
    counter, fed from two structurally different but semantically
    equivalent origins, rather than two parallel tracking mechanisms that
    could drift apart.

    A missing/malformed `globalObjects` (or a missing/malformed
    `notifications` dict within it) yields an empty record list -- the same
    "page came back with nothing readable" outcome `_page_bridges_cursor`
    already treats as bridged (see its docstring), without needing any
    special-casing here.
    """
    global_objects = resp.get("globalObjects") if isinstance(resp, dict) else None
    if not isinstance(global_objects, dict):
        return [], _extract_next_cursor(resp) if isinstance(resp, dict) else None

    raw_tweets = global_objects.get("tweets")
    raw_users = global_objects.get("users")
    tweets = raw_tweets if isinstance(raw_tweets, dict) else {}
    users = raw_users if isinstance(raw_users, dict) else {}

    raw_notifications = global_objects.get("notifications")
    records: list[_RawNotification] = []
    if isinstance(raw_notifications, dict):
        for notification in raw_notifications.values():
            record = _parse_one_notification(notification, tweets, users)
            if record is not None:
                records.append(record)

    return records, _extract_next_cursor(resp)


# Only icon ids confirmed to mean "pure engagement, no readable content" --
# see the module docstring's "Kind mapping" facts for provenance and the
# fail-open rationale.
_ENGAGEMENT_ICON_IDS = frozenset({"heart_icon", "retweet_icon"})


def _is_engagement_icon(icon_id: str | None) -> bool:
    """True only for like/repost icon ids (Codex review finding B).

    These are excluded from digest items -- a like/retweet on a tweet the
    owner posted AFTER the cursor would otherwise pass a naive age check
    and leak the owner's own tweet text into the digest as if it were
    incoming content. The notification's KIND is the discriminator here,
    not any timestamp or tweet-age comparison -- kind and chronology are
    orthogonal: an engagement notification's timestamp still counts fully
    for cursor/bridging purposes (see `collect`'s docstring), only its
    ELIGIBILITY AS AN ITEM is affected by kind.

    Fails OPEN: `icon_id` being `None` (missing/non-dict `icon`, or an icon
    dict with no `id`) or an id this module doesn't recognize both return
    `False` here -- treated as content rather than silently dropped, since
    an occasional own-tweet leaking through (false positive) is more
    recoverable than a missed mention (false negative).
    """
    return icon_id in _ENGAGEMENT_ICON_IDS


def _extract_candidates(
    resp: dict,
) -> tuple[
    list[int],
    list[tuple[int, str, str, str]],
    int,
    int,
    int,
    int,
    int,
    int,
    int,
    str | None,
]:
    """Per-page chronology + item candidates from one raw notifications response.

    Returns (page_timestamps, content_candidates, raw_count, skipped_no_tweet,
    skipped_no_screen_name, skipped_malformed, skipped_engagement,
    tweet_linked_failed, tweet_linked_succeeded, next_cursor).

    Thin wrapper around the pure `parse_notifications_page` that adds the
    counting/classification this module's `collect` contract depends on --
    kept separate from the parse step so the parse step itself stays pure
    and independently unit-testable (see `parse_notifications_page`'s
    docstring).

    ``page_timestamps`` is the `timestamp_ms` of EVERY successfully-parsed
    notification on this page -- collected unconditionally, regardless of
    kind (engagement or not) or tweet linkage. This is the sole input to
    `_page_bridges_cursor` and to `collect`'s cursor-advancement math:
    chronology is kind-agnostic (see `collect`'s docstring) -- a tweetless
    follow notification or an engagement notification contributes its
    timestamp to bridging/cursor exactly like a mention does, even though
    neither becomes an item.

    ``content_candidates`` is ``(timestamp_ms, tweet_id, text,
    screen_name)`` for the strict subset of notifications eligible to
    become digest items: non-engagement, with a linked tweet whose id/text/
    author screen_name could all be resolved. `collect` still applies the
    ``timestamp_ms > cursor`` chronology filter on top of this -- kind and
    chronology are two independent filters applied in sequence, not one
    combined check.

    ``raw_count`` is the total number of notification entries this page's
    `globalObjects.notifications` dict actually contained (used by
    `_page_bridges_cursor`'s "page is empty" check), which can exceed
    `len(content_candidates)` for several independent reasons counted
    separately: tweetless, no author screen_name, malformed (including a
    notification `parse_notifications_page` couldn't even read a timestamp
    for), or (non-malformed) engagement-kind.

    ``tweet_linked_failed`` / ``tweet_linked_succeeded`` (Codex review
    finding A -- systemic vs. isolated parsing failure): counts scoped to
    records that DO carry a `tweet_id` and a resolved `screen_name` --
    i.e. notifications for which this module actually attempted to resolve
    the linked tweet's `full_text` and classify its kind. A record reaching
    this stage with `text is None` (the tweet wasn't found in
    `globalObjects.tweets`, or was found but had no `full_text`) increments
    both `tweet_linked_failed` and `skipped_malformed` (folding in, per
    `parse_notifications_page`'s docstring, the same bucket used for a
    notification whose own timestamp couldn't be read at all); otherwise
    `tweet_linked_succeeded` increments, regardless of whether the
    notification goes on to become an item or is filtered out as
    engagement. `collect` sums these across every page fetched this run:
    failures > 0 with zero successes means EVERY tweet-linked notification
    this run saw failed to normalize -- a signal of a systemic parsing
    break (e.g. an endpoint/response-shape change), not isolated
    per-notification malformation (which has some successes alongside the
    failures) -- see `collect`'s docstring for the full rule.
    """
    records, next_cursor = parse_notifications_page(resp)

    global_objects = resp.get("globalObjects") if isinstance(resp, dict) else None
    raw_notifications = (
        global_objects.get("notifications") if isinstance(global_objects, dict) else None
    )
    raw_count = len(raw_notifications) if isinstance(raw_notifications, dict) else 0

    page_timestamps: list[int] = []
    content_candidates: list[tuple[int, str, str, str]] = []
    skipped_no_tweet = 0
    skipped_no_screen_name = 0
    # Notifications parse_notifications_page couldn't even read a timestamp
    # for are silently absent from `records` (its docstring) -- folded into
    # this same counter, per this function's own docstring.
    skipped_malformed = max(raw_count - len(records), 0)
    skipped_engagement = 0
    tweet_linked_failed = 0
    tweet_linked_succeeded = 0

    for record in records:
        page_timestamps.append(record.timestamp_ms)

        if record.tweet_id is None:
            skipped_no_tweet += 1
            continue
        if not record.screen_name:
            skipped_no_screen_name += 1
            continue
        if record.text is None:
            skipped_malformed += 1
            tweet_linked_failed += 1
            continue
        tweet_linked_succeeded += 1

        if _is_engagement_icon(record.icon_id):
            skipped_engagement += 1
            continue

        content_candidates.append(
            (record.timestamp_ms, record.tweet_id, record.text, record.screen_name)
        )

    return (
        page_timestamps,
        content_candidates,
        raw_count,
        skipped_no_tweet,
        skipped_no_screen_name,
        skipped_malformed,
        skipped_engagement,
        tweet_linked_failed,
        tweet_linked_succeeded,
        next_cursor,
    )


def _page_bridges_cursor(
    page_notification_count: int,
    page_timestamps: list[int],
    cursor_int: int,
) -> bool:
    """True once this page walks unambiguously past the cursor -- pagination can stop.

    Bridged when either: this page contains a notification whose
    `timestamp_ms` is STRICTLY LESS THAN cursor (everything from that
    notification onward, in newest-first order, is unambiguously
    already-seen ground), or the page came back with zero raw
    notifications (nothing left to page through). This check is
    deliberately kind-agnostic and tweet-linkage-agnostic -- it runs over
    `page_timestamps` (every successfully-timestamped notification, see
    `_extract_candidates`), not over `content_candidates` (only the subset
    eligible to become items). See `collect`'s docstring for why: bridging
    is about NOTIFICATION chronology, not about what any given notification
    happens to be about.

    Deliberately STRICT `<`, not `<=` (Codex review finding B): a tie
    (`timestamp_ms == cursor_int`) alone must NOT bridge. Millisecond
    timestamps can collide, and equality can't distinguish "this is the
    same notification we already saw last run" from "this is a genuinely
    new, unseen notification that happens to share a millisecond with the
    cursor". If a tie bridged, pagination could stop one page too early and
    permanently miss an unseen tied notification sitting on a later page
    (see `collect`'s docstring for the paired item-selection half of this
    fix and why overlap+dedup is preferred over composite cursor state).
    """
    if page_notification_count == 0:
        return True
    return any(timestamp_ms < cursor_int for timestamp_ms in page_timestamps)


def _is_systemic_tweet_link_failure(
    total_tweet_linked_failed: int, total_tweet_linked_succeeded: int
) -> bool:
    """True iff every tweet-linked notification seen this run failed to normalize.

    Shared by BOTH the first-run and incremental paths in `collect` (Codex
    review finding A) -- one predicate, one place to get the boundary
    right, rather than two copies that could drift. `total_tweet_linked_failed
    > 0` alone is not enough: a page can have some malformed notifications
    alongside successfully-parsed ones (isolated malformation, the ordinary
    skip-and-still-advance case) -- it's the combination of "at least one
    failure" AND "zero successes" that signals every tweet-linked
    notification this run saw failed the same way, i.e. a systemic parsing
    break (e.g. an endpoint/response-shape change) rather than scattered bad
    data. `total_tweet_linked_failed == 0` (no tweet-linked notifications at
    all this run -- e.g. only follow events) is explicitly NOT systemic:
    there is nothing to have failed.

    CALLER ORDERING REQUIREMENT: this check MUST be evaluated before ANY
    cursor value is committed -- an incremental high-water-mark advance or a
    first-run seed alike. A first-run seed under systemic failure is the
    subtler of the two ways to get this wrong: seeding "succeeds" (no
    exception, `result.failed` would default False) and looks like a normal
    first run, but once the underlying shape break is fixed, every
    notification older than that seed is permanently excluded -- the
    following run is no longer treated as a first run, so nothing ever
    re-walks the lost region. See `collect`'s docstring, "Systemic vs.
    isolated per-notification parsing failure".
    """
    return total_tweet_linked_failed > 0 and total_tweet_linked_succeeded == 0


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
    hit while still not bridged (a sustained burst of roughly more than
    `_MAX_NOTIFICATION_PAGES` * 40 notifications since the last run --
    with the current 3h schedule, north of ~200 notifications in one
    window), this module makes a deliberate PRODUCT decision rather than
    trying to resume later (see "why no resumable catch-up" under cursor
    advancement below): it accepts the truncation explicitly, advances the
    cursor past everything fetched this run exactly like a normal bridge
    would, and logs a WARNING naming the old cursor and the oldest
    timestamp actually fetched -- the gap between them is gone for good,
    on purpose, and loudly, not silently.

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
    below uses each notification's `timestamp_ms` (module docstring) as the
    cursor axis throughout, scoped to ("x", "notifications") same as
    before. Tweet ids remain ONLY as `source_id`/url material -- and as the
    de-duplication key via the `(source, source_id)` UNIQUE constraint
    (digest/state.py) -- never as a comparison axis.

    - Item selection: a CONTENT (non-engagement, see `_is_engagement_icon` /
      Codex review finding B) notification with a linked tweet becomes an
      item iff its own `timestamp_ms` is greater than OR EQUAL TO the
      cursor (Codex review finding B -- millisecond-resolution ties, see
      "Equal-timestamp boundary" below for the full rationale). A
      like/repost on a tweet the owner posted after the cursor is still
      excluded here by KIND, independent of this timestamp check --
      excluding it from items but not from chronology (next bullet) is
      deliberate. An old tweet freshly mentioned/quoted is a NEW
      notification with a NEW timestamp linking a NEW tweet -- correctly
      included via this same check, no special casing needed.
    - Bridge condition (pagination can stop): the current page contains ANY
      notification -- content or engagement, tweet-linked or not -- whose
      `timestamp_ms` is STRICTLY LESS THAN cursor (we've unambiguously
      walked back into already-seen territory), or the page came back
      empty. A tie alone (`timestamp_ms == cursor`) does NOT bridge (Codex
      review finding B -- see below). This is intentionally kind-agnostic
      and tweet-linkage-agnostic: chronology doesn't care what a
      notification is about.

    Equal-timestamp boundary -- why overlap+dedup instead of composite
    cursor state (Codex review finding B): a notification's `timestamp_ms`
    is only millisecond-resolution, so two DIFFERENT notifications can
    legitimately tie. Given a tie at exactly the cursor value, a strict `>`
    item-selection check would silently exclude a genuinely unseen
    notification that happens to share the cursor's millisecond, and a `<=`
    bridge check would let that same tie stop pagination one page too
    early -- both failure modes lose an unseen item with no trace. Telling
    "the same notification as last run" apart from "a different, unseen
    notification with the same millisecond" would need a composite cursor
    (timestamp plus e.g. the set of notification/tweet ids already seen AT
    that exact timestamp, persisted across runs) -- real, ongoing state for
    an edge case expected to be rare. Instead, this module resolves the tie
    by re-fetching and re-emitting: item selection is INCLUSIVE (`>=`, see
    above) and bridging requires STRICT `<` (see above), so a tied-but-
    unseen notification is always reached and always emitted, at the cost
    of also re-emitting the previously-seen tied notification(s). That
    repeat is absorbed for free downstream by the `(source, source_id)`
    UNIQUE constraint (digest/state.py, same INSERT..DO NOTHING dedup
    `collect`'s FAILURE-outcome overlap already relies on) -- a no-op for
    an already-seen id. The cost of this design is bounded and cheap:
    re-fetching/re-emitting at most one boundary tick's worth of
    notifications per run, versus the alternative's open-ended persisted
    state for a collision that millisecond resolution makes genuinely
    uncommon. Simplest mechanism that cannot lose a tied item.

    - Cursor advancement -- exactly two outcomes (redesigned per Codex
      review finding P1-b; an earlier version of this module had a third,
      gap-preserving outcome for the cap-hit case -- see "why no resumable
      catch-up" below for why that was removed rather than fixed):
        1. FAILURE (a genuine pipeline-level exception mid-pagination,
           `pagination_failed`): the cursor is NOT advanced AT ALL -- left
           exactly as the caller passed it in, `result.failed` stays
           `True`, and whatever items were already gathered from pages
           processed before the failure are still emitted below (they're
           already > the existing cursor, or they wouldn't have made it
           into `all_candidates`). An exception-truncated walk cannot
           support "everything up to here has been seen" -- an unread page
           might have held real content -- so this module refuses to
           guess. The next scheduled run starts from the same, unmoved
           cursor and re-walks the overlap; re-emitted items are
           deduplicated for free by the `(source, source_id)` UNIQUE
           constraint downstream (digest/state.py), so nothing already
           collected is lost, nothing is double-counted, and nothing below
           the failure point is ever silently skipped. (The pre-fix
           behavior advanced to "newest seen" even on a later-page
           failure -- silently and permanently losing whatever lay in the
           unfetched region below page 1, since the next run's page 1
           would then already bridge against the advanced cursor.)
        2. Otherwise -- bridged (including an empty page), naturally
           exhausted (the API's own next-page cursor falsy signal, "no more
           pages"), OR the deliberate page-cap truncation described above
           -- cursor := max(newest notification timestamp_ms seen across
           all pages fetched this run, existing cursor). Plain
           high-water-mark semantics, the same formula for all three:
           each of these endings reflects a trustworthy account of "how
           far this run walked", so the cursor advances past it. Mirrors
           telegram.py's "cursor advances past textless messages too",
           just scoped to notification timestamps instead of message ids.

      Why no resumable catch-up (rejecting a "cursor := oldest fetched,
      preserve the gap for a future run to drain" outcome for the cap-hit
      case, which is what an earlier version of this module did): a
      digest is not an archive. The cap is only hit by a sustained burst
      of roughly `_MAX_NOTIFICATION_PAGES` * 40 notifications since the
      last run -- with the current 3h schedule (PLAN.md), north of ~200
      notifications in one window. When that happens, the oldest
      unfetched tail is the LEAST valuable content in a digest product:
      it is already hours old, already buried under everything newer by
      the time a subscriber reads it, and a digest is read for what's new,
      not consulted as a complete unabridged backlog. Building real
      resumable catch-up (a persisted "still owe you this range" token,
      drained across however many future runs it takes) would need to
      keep working across an unknown number of future scheduled runs on
      X's cookie-based, unofficial, undocumented-lifetime pagination
      tokens (whose validity window past a single run was never
      confirmed) -- trading a small amount of guaranteed-stale content for
      open-ended token-lifetime fragility of unknown blast radius. That
      trade isn't worth it here. This module instead accepts the
      truncation explicitly, advances past it exactly like any other run,
      and makes the loss impossible to miss: a WARNING naming the old
      cursor and the oldest timestamp actually fetched, which reaches the
      operator via journal/Loki -- loud, not silent.
    - First run (cursor is None): fetch a single page (no pagination) and,
      unless that page is a systemic tweet-link failure (see below), seed
      the cursor from `newest timestamp_ms anywhere on it, PLUS ONE
      millisecond` (Codex review finding B), with NO items emitted --
      consistent with telegram.py's no-history-backfill rule (the first
      digest starts from "now"). Seeding no longer depends on any
      notification having a linked tweet: every successfully-parsed
      notification carries a timestamp regardless of kind or
      tweet-linkage, so ANY non-empty page seeds from its newest
      timestamp. Only a genuinely empty page (or one where every
      notification's timestamp itself is unreadable) has no candidate to
      seed from -- seed "0" (unchanged, no +1) so the scope isn't
      re-treated as first-run forever.

      Why +1, not the bare newest timestamp (Codex review finding B): the
      seed run stores no items, so the next run's INCLUSIVE `>=`
      item-selection check (see "Equal-timestamp boundary" below) would
      otherwise re-emit every content notification tied with the seed's
      newest timestamp as if it were new -- guaranteed history leaking into
      the very first digest, with nothing stored on the seed run for the
      `(source, source_id)` UNIQUE constraint to dedupe against. Seeding at
      `newest + 1` makes the next run's `>=` exclude everything visible at
      seed time, while every run AFTER that keeps the normal incremental
      tie-overlap semantics intact (those rely on boundary items having
      actually been STORED on the run that advanced the cursor, which is
      true from the second run onward). Accepted edge case: a genuinely
      unseen notification that lands in the exact same millisecond as the
      seed's newest timestamp, but arrives on X's side after this seed
      fetch, is lost for good -- a millisecond collision AND a race against
      this exact fetch, vanishingly rare next to the guaranteed re-emit
      this fixes.

      ORDERING REQUIREMENT (Codex review finding A): the systemic-failure
      check below MUST run BEFORE this seed commits -- see
      `_is_systemic_tweet_link_failure`'s docstring. Getting this backwards
      (seed first, check later) is exactly the bug this module used to
      have: seeding "succeeds" silently during a first-run systemic
      failure (e.g. an endpoint/response-shape break where every
      tweet-linked notification fails to normalize), and once the shape
      break is fixed, everything older than that seed is permanently
      excluded, because the following run is no longer treated as a first
      run.
    - Notifications without a linked tweet, or whose tweet's author
      screen_name is unavailable, are skipped from ITEMS (logged as a
      count, never individually -- PLAN.md: no linkable content to
      include, and a url cannot be built without a screen_name) but their
      timestamp still counts fully for cursor/bridging chronology -- see
      the kind-agnostic bridge condition above.

    Systemic vs. isolated per-notification parsing failure (Codex review
    finding A): `_extract_candidates` counts, per page, how many
    tweet-linked notifications (`tweet_id` present, `screen_name` resolved)
    failed to have their linked tweet's text resolved (`tweet_linked_failed`)
    versus how many succeeded (`tweet_linked_succeeded`) -- summed across
    every page fetched this run (just page 1 on a first run, since first
    runs never paginate). If an endpoint/response-shape change breaks
    tweet-text resolution for EVERY tweet-linked notification, each one
    individually looks like an isolated malformed item (its own skip fires,
    its timestamp still counts for chronology, the page doesn't fail) --
    but treating a run where `tweet_linked_failed > 0` and
    `tweet_linked_succeeded == 0` as "just a pile of isolated malformations"
    would still commit a cursor value past the entire lost batch, silently
    erasing it -- true whether that cursor value is an incremental
    high-water-mark advance OR a first-run seed (see the ordering
    requirement under "First run" above). This module instead treats
    "failures with zero successes" as a hard failure of its own via the
    single shared `_is_systemic_tweet_link_failure` predicate, called from
    BOTH paths: the incremental path checks it once after pagination
    completes (skipped when `pagination_failed` is already `True`, since
    that outcome already refuses to advance) and the first-run path checks
    it before its seed commits. Either path, on a positive check:
    `result.failed = True`, no items (there are none to emit anyway --
    `content_candidates` only gains entries on success), no cursor commit,
    and a WARNING naming the failed count so an endpoint/shape change
    surfaces loudly. Isolated malformation -- `tweet_linked_succeeded > 0`
    alongside some failures -- is unaffected and keeps the existing
    skip-and-count-but-still-advance behavior.

    Error handling (PLAN.md §4.3; Codex review finding A): auth/cookie
    failures (`Unauthorized`, `Forbidden`, `AccountLocked`,
    `AccountSuspended`) and rate limiting (`TooManyRequests`) on the FIRST
    page both set `failed=True` and return immediately -- NO retry, NO
    re-login attempt; a silent retry on an unofficial, cookie-based API
    risks tripping X's automation detection further, and the next scheduled
    run (3h later) is the retry. Any other exception fetching the first page
    (e.g. an endpoint/response-shape change) is caught the same way so a
    twikit/twifork break never crashes the whole digest run -- only the
    type name is logged, never exception details that might embed cookie/
    session material.

    Beyond the fetch itself, this function is exception-proof end to end, at
    two distinct granularities:

    - Per-notification malformation is handled entirely inside
      `parse_notifications_page`/`_extract_candidates` (both defensive,
      neither ever raises) -- gentle, never fails the page.
    - Pipeline-level surprises -- fetching a page raising (page 1 or any
      subsequent page), or the final item-construction step raising -- are
      each caught at the point they can occur, mirroring telegram.py's
      partial-results-survive behavior: `failed=True` is set, processing
      stops at that point, and the result is finalized from whatever items
      were already safely gathered from pages processed before the failure
      -- though, per the FAILURE cursor-advancement rule above, the cursor
      itself is deliberately left untouched in this case, unlike
      telegram.py. None of this ever raises out of `collect` itself --
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
        resp, _ = await client.v11.notifications_all(_NOTIFICATION_COUNT, None)
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
            tweet_linked_failed,
            tweet_linked_succeeded,
            api_cursor,
        ) = _extract_candidates(resp)
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
        # Codex review finding A -- ORDERING REQUIREMENT: the systemic-
        # failure check runs BEFORE the first-run seed commits (see
        # `_is_systemic_tweet_link_failure`'s docstring for why this
        # ordering matters and why getting it backwards is dangerous). A
        # first run where nothing tweet-linked exists at all
        # (tweet_linked_failed == 0, e.g. only follow-event notifications)
        # is NOT systemic -- that's the ordinary case and seeds normally
        # below, same as before this fix.
        if _is_systemic_tweet_link_failure(tweet_linked_failed, tweet_linked_succeeded):
            logger.warning(
                "x notifications: %d tweet-linked notification(s) all failed to "
                "normalize on the first run and zero succeeded -- possible "
                "endpoint/shape change; cursor not seeded",
                tweet_linked_failed,
            )
            result.failed = True
            result.items = []
            return result

        newest_in_page = max(page_timestamps, default=None)
        # Codex review finding B -- seed one millisecond PAST the newest
        # seen timestamp, not AT it. A first run stores no items (no
        # history backfill, see this function's docstring), so the very
        # next run's INCLUSIVE `>=` item-selection check (see "Equal-
        # timestamp boundary" below) would otherwise re-emit every content
        # notification tied with the seed's newest timestamp -- unseen
        # history leaking into the first digest, with nothing having been
        # stored on the seed run for the (source, source_id) dedup to
        # catch. Seeding at newest+1 makes the next run's `>=` exclude
        # everything visible at seed time, while the incremental tie-
        # overlap semantics for every run AFTER that (which rely on
        # boundary items having actually been STORED on the run that
        # advanced the cursor) are untouched. Accepted edge case: an unseen
        # notification landing in the exact same millisecond as the seed's
        # newest, but arriving on X's side after this seed fetch, is lost
        # for good -- vanishingly rare (a millisecond collision AND a race
        # against this exact fetch) versus the guaranteed history re-emit
        # this fixes. An empty page (or one whose timestamps are all
        # unreadable) has no newest timestamp to offset and still seeds
        # "0" unchanged.
        new_cursor = str(newest_in_page + 1) if newest_in_page is not None else "0"
        logger.info("x notifications: first run, seeded cursor at %s", new_cursor)
        result.cursor_updates[("x", "notifications")] = new_cursor
        return result

    cursor_int = int(cursor)
    pages_fetched = 1
    pagination_failed = False
    all_timestamps = list(page_timestamps)
    all_candidates = list(candidates)
    # Codex review finding A: run-wide totals, not per-page -- a systemic
    # parsing break must be detected across every page fetched this run,
    # not just page 1 (see the "Systemic vs. isolated" section of this
    # function's docstring).
    total_tweet_linked_failed = tweet_linked_failed
    total_tweet_linked_succeeded = tweet_linked_succeeded
    bridged = _page_bridges_cursor(raw_count, page_timestamps, cursor_int)

    while not bridged and pages_fetched < _MAX_NOTIFICATION_PAGES:
        if not api_cursor:
            # The API's own "no more pages" signal (module docstring) --
            # genuinely nothing further to fetch. Falls into the same
            # "advance to newest" cursor bucket as a normal bridge (see
            # this function's docstring) -- no special-casing needed here.
            break

        await asyncio.sleep(_PAGE_FETCH_DELAY_SECONDS)
        try:
            resp, _ = await client.v11.notifications_all(_NOTIFICATION_COUNT, api_cursor)
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
                page_tweet_linked_failed,
                page_tweet_linked_succeeded,
                api_cursor,
            ) = _extract_candidates(resp)
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
        total_tweet_linked_failed += page_tweet_linked_failed
        total_tweet_linked_succeeded += page_tweet_linked_succeeded
        _log_skip_counts(
            page_skipped_no_tweet,
            page_skipped_no_screen_name,
            page_skipped_malformed,
            page_skipped_engagement,
        )
        bridged = _page_bridges_cursor(page_raw_count, page_timestamps, cursor_int)

    # Codex review finding A: a systemic parsing failure (every tweet-linked
    # notification fetched this run failed required-field normalization,
    # none succeeded) is treated as a hard failure of its own -- see the
    # "Systemic vs. isolated" section of this function's docstring. Skipped
    # when `pagination_failed` is already True: that outcome already
    # refuses to advance the cursor, so there's nothing more to decide
    # here. This check runs (and, if it fires, returns) before the page-cap
    # warning below -- a systemic parsing break is a more fundamental
    # problem than the cap truncation and takes priority in the log output.
    if not pagination_failed and _is_systemic_tweet_link_failure(
        total_tweet_linked_failed, total_tweet_linked_succeeded
    ):
        logger.warning(
            "x notifications: %d tweet-linked notification(s) all failed to normalize "
            "this run and zero succeeded -- possible endpoint/shape change; "
            "cursor not advanced",
            total_tweet_linked_failed,
        )
        result.failed = True
        result.items = []
        return result

    if not bridged and not pagination_failed and pages_fetched >= _MAX_NOTIFICATION_PAGES:
        # Deliberate, bounded truncation (see `collect`'s docstring, "why no
        # resumable catch-up") -- a digest favors newest content over
        # completeness, so this is surfaced loudly rather than silently
        # eaten: the operator-facing log line is the whole mitigation.
        oldest_for_log = min(all_timestamps) if all_timestamps else None
        logger.warning(
            "x notifications: backlog exceeded the page cap; older notifications "
            "between %s and %s were deliberately skipped (digest favors newest content)",
            cursor_int,
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
            # Codex review finding B: INCLUSIVE (>=), not strict (>) --
            # millisecond-resolution ties can't be told apart from the
            # timestamp alone, so a candidate exactly at the cursor is
            # re-emitted rather than risk silently dropping a genuinely
            # unseen tied notification. Absorbed for free downstream by the
            # (source, source_id) UNIQUE constraint (digest/state.py) if
            # it's actually a repeat -- see `collect`'s docstring,
            # "Equal-timestamp boundary".
            if timestamp_ms >= cursor_int
        ]
        result.items = new_items

        # Cursor advancement -- exactly two outcomes (see `collect`'s
        # docstring's "Cursor advancement" section for the full rationale,
        # including why FAILURE never advances and why the cap-hit case is
        # a deliberate truncation rather than resumable catch-up):
        if pagination_failed:
            # FAILURE: leave the cursor exactly as it was passed in. Do NOT
            # advance it even partially -- an exception-truncated walk
            # can't prove "everything up to here has been seen". Items
            # gathered before the failure are still emitted above (they're
            # already > cursor); the next run re-walks the overlap for
            # free, deduplicated downstream via the (source, source_id)
            # UNIQUE constraint.
            pass
        else:
            # Bridged, naturally exhausted, or a deliberate page-cap
            # truncation -- all three advance to the newest timestamp seen,
            # same high-water-mark formula.
            newest_seen = max(all_timestamps, default=None)
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
