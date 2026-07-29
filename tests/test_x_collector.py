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

import pytest

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
    def __init__(self, id: str, message: str, tweet: FakeTweet | None):
        self.id = id
        self.message = message
        self.tweet = tweet
        self.from_user = tweet.user if tweet is not None else None


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
