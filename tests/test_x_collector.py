"""Tests for digest/collectors/x.py -- no network, no real twikit objects.

Fakes below mirror the twikit shapes confirmed by reading the installed
package source (.venv/lib/python3.12/site-packages/twikit/):
notification.py (Notification.id/.timestamp_ms/.tweet/.from_user/.message
-- `.timestamp_ms` is `int` at construction, `self.timestamp_ms =
int(data['timestampMs'])`), tweet.py (Tweet.id (str)/.full_text/.user),
user.py (User.screen_name), and errors.py (Unauthorized, Forbidden,
AccountLocked, AccountSuspended, TooManyRequests). twikit's own
get_notifications() returns a Result, which supports plain iteration
(utils.py's __iter__/__getitem__/__len__) -- a plain list works identically
for these fakes.

Cursor semantics tested here are notification-timestamp-based, not
tweet-id-based (Codex review findings P1-a/P1-b -- see digest/collectors/
x.py's module and `collect` docstrings for the full rationale). Most
fixtures below give a notification's linked tweet the SAME numeric value
for both its id and its `timestamp_ms`, purely so small integers stay easy
to read -- that pairing is NOT meaningful in general (a tweet id and a
notification timestamp are unrelated axes), and the tests that specifically
exercise that distinction (the "old tweet, new notification" class of
tests) deliberately give them different values.

`collect()` is exercised exclusively against these fakes -- never a real
twikit Client, and never over the network. The `build_client` tests are
the one exception: they exercise the real (installed, local-only)
twikit.Client's `load_cookies`/`set_cookies`, which are pure local
file/dict operations with no network I/O -- the strongest available check
that this module calls twikit's actual cookie-loading API correctly.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from digest.collectors import x as x_module
from digest.collectors.x import build_client, collect


class FakeUser:
    def __init__(self, screen_name: str):
        self.screen_name = screen_name


class FakeTweet:
    def __init__(self, id: str, full_text: str, user: FakeUser | None):
        self.id = id
        self.full_text = full_text
        self.user = user


class FakeNotification:
    def __init__(
        self,
        id: str,
        message: str,
        tweet: FakeTweet | None,
        timestamp_ms: int,
        icon: dict | None = None,
    ):
        self.id = id
        self.message = message
        self.tweet = tweet
        self.timestamp_ms = timestamp_ms
        self.from_user = tweet.user if tweet is not None else None
        self.icon = icon


class FakeXClient:
    """Minimal fake honoring the get_notifications surface `collect` uses."""

    def __init__(
        self,
        notifications: list[FakeNotification] | None = None,
        error: Exception | None = None,
    ):
        self._notifications = notifications if notifications is not None else []
        self.error = error
        self.calls = 0

    async def get_notifications(self, type, count=40, cursor=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self._notifications


def _tweet(id_: str, text: str, screen_name: str = "alice") -> FakeTweet:
    return FakeTweet(id=id_, full_text=text, user=FakeUser(screen_name))


class FakeResultPage:
    """Mirrors twikit.utils.Result enough for collect()'s pagination path.

    Plain iteration (like Result's __iter__), plus an async `.next()` and a
    `.next_cursor` attribute. Real twikit's Result (utils.py) always wires
    `.next()` to an unconditional `functools.partial(self.get_notifications,
    type, count, next_cursor)` -- even when `next_cursor` is None -- so
    calling `.next()` once `next_cursor` is falsy would silently refetch
    page one (cursor=None) rather than raise or come back empty (see
    digest/collectors/x.py's module docstring). `collect()` must therefore
    never call `.next()` in that state; `next_error`/no-`next_page` here
    simulate that misuse loudly (AssertionError) instead of silently
    looping, so a regression fails fast in tests.

    `next_call_log` is a list shared across an entire chain of pages --
    every `.next()` call anywhere in the chain appends to it, so a test can
    assert exactly how many next-page fetches happened.
    """

    def __init__(
        self,
        notifications: list[FakeNotification],
        next_cursor: str | None,
        next_call_log: list[int],
        next_page: FakeResultPage | None = None,
        next_error: Exception | None = None,
    ):
        self._notifications = notifications
        self.next_cursor = next_cursor
        self._next_call_log = next_call_log
        self._next_page = next_page
        self._next_error = next_error

    def __iter__(self):
        return iter(self._notifications)

    def __len__(self):
        return len(self._notifications)

    async def next(self) -> FakeResultPage:
        self._next_call_log.append(1)
        if self._next_error is not None:
            raise self._next_error
        if self._next_page is None:
            raise AssertionError(
                "collect() called .next() with no next page configured -- it should "
                "have stopped via the next_cursor guard instead"
            )
        return self._next_page


def _build_page_chain(
    pages: list[tuple[list[FakeNotification], str | None]],
    *,
    error_after: int | None = None,
    error: Exception | None = None,
) -> tuple[FakeResultPage, list[int]]:
    """Build a linked chain of FakeResultPage from (notifications, next_cursor) pairs.

    ``error_after``/``error``: if set, the page at that 1-based index (i.e.
    the page whose `.next()` is called) raises ``error`` instead of handing
    back the following page -- used to simulate a mid-pagination failure.

    Returns (first_page, next_call_log).
    """
    next_call_log: list[int] = []
    built: list[FakeResultPage] = []
    for idx, (notifications, next_cursor) in enumerate(reversed(pages), start=1):
        real_idx = len(pages) - idx + 1
        raise_here = error_after is not None and real_idx == error_after
        built.append(
            FakeResultPage(
                notifications,
                next_cursor,
                next_call_log,
                next_page=None if raise_here else (built[-1] if built else None),
                next_error=error if raise_here else None,
            )
        )
    built.reverse()
    return built[0], next_call_log


# --- first run seeding ---


async def test_first_run_seeds_newest_timestamp_and_emits_no_items():
    notifications = [
        FakeNotification("n2", "mentioned you", _tweet("200", "hello"), timestamp_ms=2000),
        FakeNotification("n1", "mentioned you", _tweet("100", "hi"), timestamp_ms=1000),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor=None)

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "2000"}
    assert result.failed is False


async def test_first_run_empty_page_seeds_zero_cursor():
    client = FakeXClient(notifications=[])

    result = await collect(client, cursor=None)

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "0"}
    assert result.failed is False


async def test_first_run_tweetless_page_seeds_from_its_newest_timestamp():
    # Every notification lacks a linked tweet (e.g. pure follow events), but
    # each still carries its own timestamp_ms -- seeding no longer depends
    # on any notification having a linked tweet (redesign point 4's "any
    # non-empty page seeds from its newest timestamp", superseding the old
    # "tweetless-only page seeds 0" rule).
    notifications = [
        FakeNotification("n2", "followed you", tweet=None, timestamp_ms=1500),
        FakeNotification("n1", "followed you", tweet=None, timestamp_ms=1200),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor=None)

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "1500"}


# --- incremental ---


async def test_incremental_only_timestamps_greater_than_cursor_oldest_first():
    notifications = [
        FakeNotification("n3", "m", _tweet("300", "third"), timestamp_ms=300),
        FakeNotification("n2", "m", _tweet("200", "second"), timestamp_ms=200),
        FakeNotification("n1", "m", _tweet("100", "first"), timestamp_ms=100),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="150")

    assert [i.source_id for i in result.items] == ["200", "300"]
    assert [i.text for i in result.items] == ["second", "third"]
    assert result.cursor_updates == {("x", "notifications"): "300"}
    assert result.failed is False


async def test_incremental_no_new_timestamps_leaves_cursor_untouched():
    notifications = [
        FakeNotification("n1", "m", _tweet("100", "old"), timestamp_ms=100),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="500")

    assert result.items == []
    assert result.cursor_updates == {}


async def test_incremental_notification_without_tweet_is_skipped_but_still_counts_for_cursor():
    notifications = [
        FakeNotification("n2", "followed you", tweet=None, timestamp_ms=50),
        FakeNotification("n1", "mentioned you", _tweet("100", "hi"), timestamp_ms=100),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]
    # Newest timestamp across ALL notifications this page (100), even
    # though the tweetless one (50) never became an item -- chronology is
    # kind/linkage-agnostic (redesign point 3/4).
    assert result.cursor_updates == {("x", "notifications"): "100"}


async def test_incremental_notification_without_screen_name_skipped_but_advances_cursor():
    notifications = [
        FakeNotification(
            "n2", "mentioned you", _tweet("200", "no author", screen_name=""), timestamp_ms=200
        ),
        FakeNotification("n1", "mentioned you", _tweet("100", "has author"), timestamp_ms=100),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]
    # The screen_name-less notification's timestamp (200) still counts for
    # chronology even though it was excluded as an item.
    assert result.cursor_updates == {("x", "notifications"): "200"}


# --- pagination: bounded catch-up when >1 page of new notifications ---


async def test_pagination_multi_page_collects_all_until_cursor_bridged(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page1_notifications = [
        FakeNotification("n5", "m", _tweet("500", "fifth"), timestamp_ms=500),
        FakeNotification("n4", "m", _tweet("400", "fourth"), timestamp_ms=400),
    ]
    page2_notifications = [
        FakeNotification("n3", "m", _tweet("300", "third"), timestamp_ms=300),
        FakeNotification("n2b", "m", _tweet("250", "second-b"), timestamp_ms=250),
    ]
    page3_notifications = [
        FakeNotification("n2", "m", _tweet("200", "second"), timestamp_ms=200),
        # timestamp <= cursor: bridges here
        FakeNotification("n1", "m", _tweet("100", "first"), timestamp_ms=100),
    ]
    first_page, next_call_log = _build_page_chain(
        [
            (page1_notifications, "c1"),
            (page2_notifications, "c2"),
            (page3_notifications, "c3"),
        ]
    )
    client = FakeXClient(notifications=first_page)

    result = await collect(client, cursor="150")

    assert [i.source_id for i in result.items] == ["200", "250", "300", "400", "500"]
    assert result.cursor_updates == {("x", "notifications"): "500"}
    assert result.failed is False
    # 1 initial get_notifications() call (page 1) + 2 .next() calls (-> page 2, -> page 3).
    assert client.calls == 1
    assert len(next_call_log) == 2
    # sleep awaited once between each pair of page fetches, never before the first.
    assert sleeps == [1.5, 1.5]


async def test_pagination_cursor_bridged_on_first_page_makes_exactly_one_fetch_no_sleep(
    monkeypatch,
):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    notifications = [
        FakeNotification("n2", "m", _tweet("500", "new"), timestamp_ms=500),
        # timestamp <= cursor: bridged on page 1
        FakeNotification("n1", "m", _tweet("100", "old"), timestamp_ms=100),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="150")

    assert [i.source_id for i in result.items] == ["500"]
    assert result.cursor_updates == {("x", "notifications"): "500"}
    assert client.calls == 1
    assert sleeps == []


async def test_pagination_page_cap_reached_unbridged_cursor_advances_to_newest_and_warns(
    monkeypatch, caplog
):
    """Codex review finding P1-b: an unbridged cap-hit is a deliberate,
    product-level truncation, not resumable-catch-up machinery -- a digest
    favors newest content over completeness, not a complete backlog (see
    `collect`'s docstring, "why no resumable catch-up"). The cursor
    advances to the newest FETCHED timestamp, exactly like a normal bridge
    would (the gap below that point -- here: the 6th page, timestamp 100,
    never fetched -- is deliberately abandoned, not preserved), and the
    truncation is surfaced loudly via a WARNING naming the old cursor and
    the oldest timestamp actually fetched, so an operator watching
    journal/Loki can see it happened.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    # 6 pages, all-new timestamps (never <= cursor), forcing the cap at 5 pages.
    pages = [
        ([FakeNotification(f"n{i}", "m", _tweet(str(i), f"t{i}"), timestamp_ms=i)], f"c{i}")
        for i in (600, 500, 400, 300, 200, 100)
    ]
    first_page, next_call_log = _build_page_chain(pages)
    client = FakeXClient(notifications=first_page)

    with caplog.at_level("WARNING", logger="digest.collectors.x"):
        result = await collect(client, cursor="50")

    # Page 6 (timestamp 100) is never fetched -- only 4 `.next()` calls (1->2->3->4->5).
    assert len(next_call_log) == 4
    # Cursor advances to the newest FETCHED timestamp (600) -- plain
    # high-water-mark semantics, same formula as the bridged case. The gap
    # below 200 (the oldest fetched) is gone for good, on purpose.
    assert result.cursor_updates == {("x", "notifications"): "600"}
    # Items are unaffected by the cursor-advancement choice: everything
    # fetched and > the (unchanged, true) input cursor(50) is still collected.
    assert [i.source_id for i in result.items] == ["200", "300", "400", "500", "600"]
    assert any(
        "backlog exceeded the page cap" in record.message for record in caplog.records
    )
    assert any(
        "between 50 and 200 were deliberately skipped" in record.message
        for record in caplog.records
    )
    assert any("digest favors newest content" in record.message for record in caplog.records)


async def test_followup_after_unbridged_cap_collects_only_newer_notifications(monkeypatch):
    """P1-b: a digest is not an archive (see `collect`'s docstring, "why no
    resumable catch-up"). After an unbridged cap-hit run truncates and
    advances the cursor to the newest fetched timestamp, a follow-up run
    makes NO attempt to reach back into the abandoned gap -- it simply
    walks from the new (newest-fetched) cursor like any other incremental
    run, picking up only genuinely newer content and bridging quickly, with
    no error and no re-walk of the skipped region.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    # --- Phase 1: same cap-hit setup as the test above. ---
    phase1_pages = [
        ([FakeNotification(f"n{i}", "m", _tweet(str(i), f"t{i}"), timestamp_ms=i)], f"c{i}")
        for i in (600, 500, 400, 300, 200, 100)
    ]
    phase1_first_page, _ = _build_page_chain(phase1_pages)
    phase1_client = FakeXClient(notifications=phase1_first_page)

    phase1_result = await collect(phase1_client, cursor="50")

    # Unbridged cap-hit -> cursor advances to the newest fetched (600), not
    # the oldest -- see the dedicated cap test above. The gap below 200
    # (i.e. timestamp 100, on the never-fetched 6th page) is abandoned.
    assert phase1_result.cursor_updates == {("x", "notifications"): "600"}
    phase2_cursor = phase1_result.cursor_updates[("x", "notifications")]

    # --- Phase 2: a follow-up run, fed phase 1's cursor (600). ---
    # New content (900, 700) arrived since phase 1, followed by a
    # notification at 550 that's already <= the new cursor -- bridges
    # immediately on page 1. No attempt is made to reach back down to the
    # abandoned gap (100) or even to re-confirm the rest of phase 1's
    # fetched region (200-500): the new cursor is 600, so nothing below it
    # is ever revisited.
    phase2_notifications = [
        FakeNotification("n900", "m", _tweet("900", "newest"), timestamp_ms=900),
        FakeNotification("n700", "m", _tweet("700", "newer"), timestamp_ms=700),
        # timestamp 550 <= cursor(600): bridges here, on page 1.
        FakeNotification("n550", "m", _tweet("550", "already-seen boundary"), timestamp_ms=550),
    ]
    phase2_first_page, phase2_next_call_log = _build_page_chain(
        [(phase2_notifications, None)]
    )
    phase2_client = FakeXClient(notifications=phase2_first_page)

    phase2_result = await collect(phase2_client, cursor=phase2_cursor)

    # Bridged on page 1 -- no `.next()` calls at all.
    assert len(phase2_next_call_log) == 0
    assert phase2_result.failed is False
    # Only the genuinely newer notifications (900, 700) become items; 550
    # is at/below the cursor and excluded, as is everything from the
    # abandoned gap (which was never fetched at all this run).
    assert [i.source_id for i in phase2_result.items] == ["700", "900"]
    assert "100" not in [i.source_id for i in phase2_result.items]
    # Bridged this run -> cursor advances to the newest seen (900).
    assert phase2_result.cursor_updates == {("x", "notifications"): "900"}


async def test_pagination_failure_on_second_page_keeps_first_page_items_cursor_not_advanced(
    monkeypatch,
):
    """Codex review finding P1-a (failure case): a pipeline failure while
    paginating must NOT advance the cursor at all -- not even partially --
    or the region below whatever was fetched before the failure would be
    permanently skipped on the next run (its page 1 would already bridge
    against an advanced cursor). Items gathered before the failure are
    still emitted -- they were already > the existing cursor -- but the
    cursor key is not written at all, so the next run re-walks the overlap
    from the same starting point and relies on the (source, source_id)
    UNIQUE constraint to dedupe for free.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page1_notifications = [
        FakeNotification("n2", "m", _tweet("100", "b"), timestamp_ms=100),
        FakeNotification("n1", "m", _tweet("80", "a"), timestamp_ms=80),
    ]
    first_page, next_call_log = _build_page_chain(
        [(page1_notifications, "c1")],
        error_after=1,
        error=RuntimeError("graphql shape changed"),
    )
    client = FakeXClient(notifications=first_page)

    result = await collect(client, cursor="50")

    assert [i.source_id for i in result.items] == ["80", "100"]
    # FAILURE: the cursor is not advanced at all -- the key isn't even
    # present in cursor_updates, not just "unchanged in value".
    assert ("x", "notifications") not in result.cursor_updates
    assert result.cursor_updates == {}
    assert result.failed is True
    assert len(next_call_log) == 1


# --- Codex review finding A: per-notification malformation is gentle, never
# fails the page (contrast with the pipeline-level tests further below) ---


class _TweetMissingFullText:
    """A tweet-shaped object with no `.full_text` -- triggers AttributeError."""

    def __init__(self, id: str, user: FakeUser):
        self.id = id
        self.user = user


async def test_incremental_nonnumeric_tweet_id_is_skipped_others_collected(caplog):
    notifications = [
        FakeNotification(
            "n2", "m", _tweet("not-a-number", "bad id"), timestamp_ms=200
        ),
        FakeNotification("n1", "m", _tweet("100", "good"), timestamp_ms=100),
    ]
    client = FakeXClient(notifications=notifications)

    with caplog.at_level("INFO", logger="digest.collectors.x"):
        result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]
    # The malformed notification's OWN timestamp (200) is still readable
    # (it's independent of its tweet's malformed id) and still counts for
    # cursor chronology.
    assert result.cursor_updates == {("x", "notifications"): "200"}
    assert result.failed is False
    assert any(
        "skipped 1 malformed notification" in record.message for record in caplog.records
    )


async def test_incremental_tweet_missing_full_text_is_skipped_others_collected():
    notifications = [
        FakeNotification(
            "n2", "m", _TweetMissingFullText("200", FakeUser("alice")), timestamp_ms=200
        ),
        FakeNotification("n1", "m", _tweet("100", "good"), timestamp_ms=100),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]
    assert result.cursor_updates == {("x", "notifications"): "200"}
    assert result.failed is False


# --- Codex review finding A: pipeline-level surprises set failed=True but
# keep whatever was safely gathered before the surprise ---


class _RaisingNotificationsPage:
    """A page whose notification iterator raises partway through iteration.

    Simulates a bug in the underlying iterable itself (as opposed to one
    malformed notification) -- collect() must treat this as a pipeline-level
    surprise: failed=True, but items/candidates already gathered from PRIOR
    pages are preserved rather than discarded.
    """

    def __init__(
        self, notifications_before_raise: list[FakeNotification], error: Exception
    ):
        self._notifications_before_raise = notifications_before_raise
        self._error = error
        self.next_cursor = None

    def __iter__(self):
        yield from self._notifications_before_raise
        raise self._error


class _FirstPageWithRaisingNext:
    """First page: normal iteration, `.next()` hands back a raising page."""

    def __init__(self, notifications: list[FakeNotification], next_page: Any):
        self._notifications = notifications
        self.next_cursor = "c1"
        self._next_page = next_page

    def __iter__(self):
        return iter(self._notifications)

    def __len__(self):
        return len(self._notifications)

    async def next(self):
        return self._next_page


async def test_pagination_iterator_raising_mid_page_keeps_prior_page_items(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page2 = _RaisingNotificationsPage(
        [FakeNotification("n2", "m", _tweet("200", "lost to the raise"), timestamp_ms=200)],
        RuntimeError("notification iterator exploded"),
    )
    page1_notifications = [
        FakeNotification("n4", "m", _tweet("400", "fourth"), timestamp_ms=400),
        FakeNotification("n3", "m", _tweet("300", "third"), timestamp_ms=300),
    ]
    first_page = _FirstPageWithRaisingNext(page1_notifications, page2)
    client = FakeXClient(notifications=first_page)

    result = await collect(client, cursor="150")

    # Page 2's notification ("200") never makes it in -- the whole page 2
    # extraction raised before it could be merged in -- but page 1's items
    # survive intact.
    assert [i.source_id for i in result.items] == ["300", "400"]
    # FAILURE: cursor is not advanced at all (see the dedicated
    # cursor-not-advanced failure test above for the full rationale).
    assert result.cursor_updates == {}
    assert result.failed is True


# --- Codex review finding B: engagement notifications (likes, reposts) are
# never emitted as items -- the notification KIND is the discriminator, not
# tweet/notification age. Their timestamps still advance chronology exactly
# like any other notification (bridging + cursor), independent of kind. ---


async def test_like_notification_on_post_cursor_tweet_skipped_as_item_but_advances_cursor():
    notifications = [
        FakeNotification(
            "n1",
            "liked your Tweet",
            _tweet("999", "owner's own tweet"),
            timestamp_ms=999,
            icon={"id": "heart_icon"},
        ),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="500")

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "999"}
    assert result.failed is False


async def test_repost_notification_on_post_cursor_tweet_skipped_as_item():
    notifications = [
        FakeNotification(
            "n1",
            "reposted your Tweet",
            _tweet("999", "owner's own tweet"),
            timestamp_ms=999,
            icon={"id": "retweet_icon"},
        ),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="500")

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "999"}


async def test_mention_and_reply_kind_notifications_are_included():
    notifications = [
        FakeNotification(
            "n2", "mentioned you", _tweet("300", "hey @you"), timestamp_ms=300,
            icon={"id": "at_icon"},
        ),
        FakeNotification(
            "n1", "replied to you", _tweet("200", "a reply"), timestamp_ms=200,
            icon={"id": "reply_icon"},
        ),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["200", "300"]


async def test_unrecognized_icon_kind_fails_open_and_is_included():
    notifications = [
        FakeNotification(
            "n1", "something new", _tweet("100", "quote maybe?"), timestamp_ms=100,
            icon={"id": "some_future_icon_id"},
        ),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]


async def test_missing_icon_fails_open_and_is_included():
    notifications = [
        FakeNotification("n1", "mentioned you", _tweet("100", "hi"), timestamp_ms=100, icon=None),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]


async def test_follow_notification_no_tweet_unaffected_by_icon_kind():
    notifications = [
        FakeNotification(
            "n2", "followed you", tweet=None, timestamp_ms=150, icon={"id": "user_icon"}
        ),
        FakeNotification(
            "n1", "mentioned you", _tweet("100", "hi"), timestamp_ms=100, icon={"id": "at_icon"}
        ),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]


async def test_engagement_notification_timestamp_bridges_pagination_like_any_other(monkeypatch):
    """Codex review finding P1-a/redesign point 3: bridging is kind-agnostic
    -- an engagement notification's timestamp <= cursor stops pagination
    exactly like a content notification's would, even though the engagement
    notification itself never becomes an item.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page1_notifications = [
        FakeNotification(
            "n2", "liked your Tweet", _tweet("777", "own tweet"), timestamp_ms=1500,
            icon={"id": "heart_icon"},
        ),
    ]
    page2_notifications = [
        # timestamp <= cursor(1000): bridges, purely via an engagement
        # notification's chronology -- no content/tweet-linked notification
        # is needed to trigger a bridge.
        FakeNotification(
            "n1", "reposted your Tweet", _tweet("888", "own tweet"), timestamp_ms=900,
            icon={"id": "retweet_icon"},
        ),
    ]
    first_page, next_call_log = _build_page_chain(
        [(page1_notifications, "c1"), (page2_notifications, "c2")]
    )
    client = FakeXClient(notifications=first_page)

    result = await collect(client, cursor="1000")

    assert len(next_call_log) == 1
    assert result.items == []
    # Bridged -> cursor advances to the newest timestamp seen (1500), even
    # though every notification fetched was pure engagement.
    assert result.cursor_updates == {("x", "notifications"): "1500"}


# --- Codex review finding P1-a: bridging must be by NOTIFICATION timestamp,
# never by linked tweet id -- the 'All' timeline is ordered by notification
# time, so a fresh like/repost on an OLD tweet can land on page 1 with a
# small, "old-looking" tweet id while a genuinely unseen mention (with a
# large, new tweet id) sits unread on page 2. Bridging on tweet id would
# stop pagination on page 1 and silently lose the page-2 mention. ---


async def test_old_tweet_fresh_like_on_page1_does_not_falsely_bridge_past_page2_mention():
    cursor = "1000"

    # Page 1: a LIKE notification that just happened (timestamp 2000, well
    # after the cursor) on a tweet from long ago (id "5" -- if bridging were
    # still done by tweet id, "5 <= 1000" would falsely bridge right here).
    page1_notifications = [
        FakeNotification(
            "n1",
            "liked your Tweet",
            _tweet("5", "an old tweet of the owner's"),
            timestamp_ms=2000,
            icon={"id": "heart_icon"},
        ),
    ]
    # Page 2: a genuinely new, previously-unseen mention (large tweet id,
    # new timestamp) -- must be reached and collected, followed by a
    # notification old enough to legitimately bridge (ends the test cleanly).
    page2_notifications = [
        FakeNotification(
            "n2", "mentioned you", _tweet("3000", "hey @you"), timestamp_ms=2100,
            icon={"id": "at_icon"},
        ),
        # timestamp <= cursor(1000): bridges here.
        FakeNotification(
            "n3", "replied to you", _tweet("10", "stale"), timestamp_ms=900,
            icon={"id": "reply_icon"},
        ),
    ]
    first_page, next_call_log = _build_page_chain(
        [(page1_notifications, "c1"), (page2_notifications, "c2")]
    )
    client = FakeXClient(notifications=first_page)

    result = await collect(client, cursor=cursor)

    # Pagination continued past page 1 -- 1 `.next()` call -- instead of
    # falsely bridging on the old-tweet-id-but-new-timestamp like.
    assert len(next_call_log) == 1
    # The page-2 mention was reached and collected; the page-1 like (pure
    # engagement) and the stale page-2 reply (timestamp <= cursor) were not.
    assert [i.source_id for i in result.items] == ["3000"]
    assert result.cursor_updates == {("x", "notifications"): "2100"}
    assert result.failed is False


# --- errors: auth/cookie failures never retry ---


async def test_auth_error_unauthorized_flags_failed_no_retry():
    from twikit.errors import Unauthorized

    client = FakeXClient(error=Unauthorized("nope"))

    result = await collect(client, cursor="0")

    assert result.failed is True
    assert result.items == []
    assert result.cursor_updates == {}
    assert client.calls == 1


async def test_auth_error_forbidden_flags_failed():
    from twikit.errors import Forbidden

    client = FakeXClient(error=Forbidden("nope"))

    result = await collect(client, cursor="0")

    assert result.failed is True
    assert client.calls == 1


async def test_account_locked_flags_failed():
    from twikit.errors import AccountLocked

    client = FakeXClient(error=AccountLocked("locked"))

    result = await collect(client, cursor="0")

    assert result.failed is True
    assert client.calls == 1


async def test_account_suspended_flags_failed():
    from twikit.errors import AccountSuspended

    client = FakeXClient(error=AccountSuspended("suspended"))

    result = await collect(client, cursor="0")

    assert result.failed is True
    assert client.calls == 1


# --- errors: rate limit ---


async def test_rate_limit_error_flags_failed_no_retry():
    from twikit.errors import TooManyRequests

    client = FakeXClient(error=TooManyRequests("slow down"))

    result = await collect(client, cursor="0")

    assert result.failed is True
    assert client.calls == 1


# --- errors: anything else never crashes the run ---


async def test_other_exception_flags_failed_never_crashes():
    client = FakeXClient(error=RuntimeError("graphql shape changed"))

    result = await collect(client, cursor="0")

    assert result.failed is True
    assert result.items == []


# --- url provenance ---


async def test_url_built_only_from_api_returned_screen_name_and_tweet_id():
    notifications = [
        FakeNotification("n1", "m", _tweet("999", "hi", screen_name="bob"), timestamp_ms=999)
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert result.items[0].url == "https://x.com/bob/status/999"
    assert result.items[0].author == "bob"
    assert result.items[0].chat_id is None
    assert result.items[0].source == "x"


# --- build_client ---


def test_build_client_requires_exactly_one_cookie_source():
    with pytest.raises(ValueError):
        build_client(None, None)


def test_build_client_loads_from_path(tmp_path):
    cookies_file = tmp_path / "cookies.json"
    cookies_file.write_text('{"ct0": "abc", "auth_token": "def"}')

    client = build_client(str(cookies_file), None)

    assert client.get_cookies() == {"ct0": "abc", "auth_token": "def"}


def test_build_client_loads_from_inline_json():
    client = build_client(None, '{"ct0": "abc", "auth_token": "def"}')

    assert client.get_cookies() == {"ct0": "abc", "auth_token": "def"}


# --- X_ENABLED=false: zero twikit import side effects (Phase 3 acceptance)
#
# Run in a fresh subprocess rather than in-process: other tests in this
# file deliberately trigger `from twikit.errors import ...` (lazy imports
# inside collect()), which would leave `twikit` cached in sys.modules for
# the rest of the pytest process and make an in-process check meaningless
# regardless of import order.


def test_importing_collector_module_alone_does_not_import_twikit():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import digest.collectors.x\nimport sys\nassert 'twikit' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_importing_digest_main_does_not_import_twikit():
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import digest.main\nimport sys\nassert 'twikit' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
