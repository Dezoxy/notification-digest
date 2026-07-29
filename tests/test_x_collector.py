"""Tests for digest/collectors/x.py -- no network, no real twikit objects.

Fakes below mirror the twikit shapes confirmed by reading the installed
package source (.venv/lib/python3.12/site-packages/twikit/):
notification.py (Notification.id/.tweet/.from_user/.message), tweet.py
(Tweet.id (str)/.full_text/.user), user.py (User.screen_name), and
errors.py (Unauthorized, Forbidden, AccountLocked, AccountSuspended,
TooManyRequests). twikit's own get_notifications() returns a Result, which
supports plain iteration (utils.py's __iter__/__getitem__/__len__) -- a
plain list works identically for these fakes.

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
        icon: dict | None = None,
    ):
        self.id = id
        self.message = message
        self.tweet = tweet
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


async def test_first_run_seeds_newest_id_and_emits_no_items():
    notifications = [
        FakeNotification("n2", "mentioned you", _tweet("200", "hello")),
        FakeNotification("n1", "mentioned you", _tweet("100", "hi")),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor=None)

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "200"}
    assert result.failed is False


async def test_first_run_empty_page_seeds_zero_cursor():
    client = FakeXClient(notifications=[])

    result = await collect(client, cursor=None)

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "0"}
    assert result.failed is False


async def test_first_run_page_with_only_tweetless_notifications_seeds_zero_cursor():
    # No candidate id exists anywhere in the page (every notification lacks
    # a linked tweet) -- treated the same as an empty page.
    notifications = [FakeNotification("n1", "followed you", tweet=None)]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor=None)

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "0"}


# --- incremental ---


async def test_incremental_only_ids_greater_than_cursor_oldest_first():
    notifications = [
        FakeNotification("n3", "m", _tweet("300", "third")),
        FakeNotification("n2", "m", _tweet("200", "second")),
        FakeNotification("n1", "m", _tweet("100", "first")),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="150")

    assert [i.source_id for i in result.items] == ["200", "300"]
    assert [i.text for i in result.items] == ["second", "third"]
    assert result.cursor_updates == {("x", "notifications"): "300"}
    assert result.failed is False


async def test_incremental_no_new_ids_leaves_cursor_untouched():
    notifications = [FakeNotification("n1", "m", _tweet("100", "old"))]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="500")

    assert result.items == []
    assert result.cursor_updates == {}


async def test_incremental_notification_without_tweet_is_skipped():
    notifications = [
        FakeNotification("n2", "followed you", tweet=None),
        FakeNotification("n1", "mentioned you", _tweet("100", "hi")),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]
    assert result.cursor_updates == {("x", "notifications"): "100"}


async def test_incremental_notification_without_screen_name_is_skipped():
    notifications = [
        FakeNotification("n2", "mentioned you", _tweet("200", "no author", screen_name="")),
        FakeNotification("n1", "mentioned you", _tweet("100", "has author")),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]


# --- pagination: bounded catch-up when >1 page of new notifications ---


async def test_pagination_multi_page_collects_all_until_cursor_bridged(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page1_notifications = [
        FakeNotification("n5", "m", _tweet("500", "fifth")),
        FakeNotification("n4", "m", _tweet("400", "fourth")),
    ]
    page2_notifications = [
        FakeNotification("n3", "m", _tweet("300", "third")),
        FakeNotification("n2b", "m", _tweet("250", "second-b")),
    ]
    page3_notifications = [
        FakeNotification("n2", "m", _tweet("200", "second")),
        FakeNotification("n1", "m", _tweet("100", "first")),  # <= cursor: bridges here
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
        FakeNotification("n2", "m", _tweet("500", "new")),
        FakeNotification("n1", "m", _tweet("100", "old")),  # <= cursor: bridged on page 1
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="150")

    assert [i.source_id for i in result.items] == ["500"]
    assert result.cursor_updates == {("x", "notifications"): "500"}
    assert client.calls == 1
    assert sleeps == []


async def test_pagination_page_cap_reached_logs_warning_but_cursor_still_advances(
    monkeypatch, caplog
):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    # 6 pages, all-new ids (never <= cursor), forcing the cap at 5 pages.
    pages = [
        ([FakeNotification(f"n{i}", "m", _tweet(str(i), f"t{i}"))], f"c{i}")
        for i in (600, 500, 400, 300, 200, 100)
    ]
    first_page, next_call_log = _build_page_chain(pages)
    client = FakeXClient(notifications=first_page)

    with caplog.at_level("WARNING", logger="digest.collectors.x"):
        result = await collect(client, cursor="50")

    # Page 6 (id 100) is never fetched -- only 4 `.next()` calls (1->2->3->4->5).
    assert len(next_call_log) == 4
    assert result.cursor_updates == {("x", "notifications"): "600"}
    # Ids 600,500,400,300,200 came from the 5 fetched pages; 100 (page 6) never arrives.
    assert [i.source_id for i in result.items] == ["200", "300", "400", "500", "600"]
    assert any(
        "page cap reached, 5 pages fetched" in record.message for record in caplog.records
    )


async def test_pagination_exception_on_second_page_keeps_first_page_items(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page1_notifications = [
        FakeNotification("n2", "m", _tweet("100", "b")),
        FakeNotification("n1", "m", _tweet("80", "a")),
    ]
    first_page, next_call_log = _build_page_chain(
        [(page1_notifications, "c1")],
        error_after=1,
        error=RuntimeError("graphql shape changed"),
    )
    client = FakeXClient(notifications=first_page)

    result = await collect(client, cursor="50")

    assert [i.source_id for i in result.items] == ["80", "100"]
    assert result.cursor_updates == {("x", "notifications"): "100"}
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
        FakeNotification("n2", "m", _tweet("not-a-number", "bad id")),
        FakeNotification("n1", "m", _tweet("100", "good")),
    ]
    client = FakeXClient(notifications=notifications)

    with caplog.at_level("INFO", logger="digest.collectors.x"):
        result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]
    assert result.cursor_updates == {("x", "notifications"): "100"}
    assert result.failed is False
    assert any(
        "skipped 1 malformed notification" in record.message for record in caplog.records
    )


async def test_incremental_tweet_missing_full_text_is_skipped_others_collected():
    notifications = [
        FakeNotification("n2", "m", _TweetMissingFullText("200", FakeUser("alice"))),
        FakeNotification("n1", "m", _tweet("100", "good")),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]
    assert result.cursor_updates == {("x", "notifications"): "100"}
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
        [FakeNotification("n2", "m", _tweet("200", "lost to the raise"))],
        RuntimeError("notification iterator exploded"),
    )
    page1_notifications = [
        FakeNotification("n4", "m", _tweet("400", "fourth")),
        FakeNotification("n3", "m", _tweet("300", "third")),
    ]
    first_page = _FirstPageWithRaisingNext(page1_notifications, page2)
    client = FakeXClient(notifications=first_page)

    result = await collect(client, cursor="150")

    # Page 2's notification ("200") never makes it in -- the whole page 2
    # extraction raised before it could be merged in -- but page 1's items
    # survive intact.
    assert [i.source_id for i in result.items] == ["300", "400"]
    assert result.cursor_updates == {("x", "notifications"): "400"}
    assert result.failed is True


# --- Codex review finding B: engagement notifications (likes, reposts) are
# never emitted as items -- the notification KIND is the discriminator, not
# tweet age, since a like/repost on a post-cursor tweet is the OWNER's own
# content, not incoming content. ---


async def test_like_notification_on_post_cursor_tweet_skipped_as_item_but_advances_cursor():
    notifications = [
        FakeNotification(
            "n1", "liked your Tweet", _tweet("999", "owner's own tweet"), icon={"id": "heart_icon"}
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
            "n1", "reposted your Tweet", _tweet("999", "owner's own tweet"),
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
            "n2", "mentioned you", _tweet("300", "hey @you"), icon={"id": "at_icon"}
        ),
        FakeNotification(
            "n1", "replied to you", _tweet("200", "a reply"), icon={"id": "reply_icon"}
        ),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["200", "300"]


async def test_unrecognized_icon_kind_fails_open_and_is_included():
    notifications = [
        FakeNotification(
            "n1", "something new", _tweet("100", "quote maybe?"),
            icon={"id": "some_future_icon_id"},
        ),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]


async def test_missing_icon_fails_open_and_is_included():
    notifications = [
        FakeNotification("n1", "mentioned you", _tweet("100", "hi"), icon=None),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]


async def test_follow_notification_no_tweet_unaffected_by_icon_kind():
    notifications = [
        FakeNotification("n2", "followed you", tweet=None, icon={"id": "user_icon"}),
        FakeNotification("n1", "mentioned you", _tweet("100", "hi"), icon={"id": "at_icon"}),
    ]
    client = FakeXClient(notifications=notifications)

    result = await collect(client, cursor="0")

    assert [i.source_id for i in result.items] == ["100"]


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
    notifications = [FakeNotification("n1", "m", _tweet("999", "hi", screen_name="bob"))]
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


# --- X_ENABLED=false: zero twikit import side effects (Phase 3 acceptance) ---
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
