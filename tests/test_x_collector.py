"""Tests for digest/collectors/x.py -- no network, no real twikit/twifork objects.

Fakes below build RAW `v11.notifications_all` response payloads (see
digest/collectors/x.py's module docstring for the confirmed shape:
`resp['globalObjects']['notifications'|'tweets'|'users']`, plus a
`cursor-bottom` entry inside `entries` for the next-page pagination token) --
this module deliberately never constructs a twifork `Notification`/`Tweet`/
`User` object anywhere, mirroring how the collector itself now bypasses that
model layer entirely and parses raw JSON (see the module docstring's
"Why this module bypasses twifork's model layer" section for why).

Cursor semantics tested here are notification-timestamp-based, not
tweet-id-based (Codex review findings P1-a/P1-b -- see digest/collectors/
x.py's module and `collect` docstrings for the full rationale). Most
fixtures below give a notification's linked tweet the SAME numeric value
for both its id and its `timestamp_ms`, purely so small integers stay easy
to read -- that pairing is NOT meaningful in general (a tweet id and a
notification timestamp are unrelated axes), and the tests that specifically
exercise that distinction (the "old tweet, new notification" class of
tests) deliberately give them different values.

`collect()` is exercised exclusively against `FakeXClient` (a fake
`client.v11.notifications_all`) -- never a real twifork Client, and never
over the network. The `build_client` tests are the one exception: they
exercise the real (installed, local-only) twifork `Client`'s
`load_cookies`/`set_cookies`, which are pure local file/dict operations with
no network I/O -- the strongest available check that this module calls the
package's actual cookie-loading API correctly (twifork imports as `twikit`,
same public surface as upstream for this).
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from digest.collectors import x as x_module
from digest.collectors.x import build_client, collect


class _PageBuilder:
    """Accumulates raw notification/tweet/user entries into one response page.

    Mirrors `resp['globalObjects']` exactly (digest/collectors/x.py's
    module docstring) -- an ergonomic way to build the raw JSON shape
    `collect`/`parse_notifications_page` actually consume, without
    hand-writing the nested dict literal for every test.
    """

    def __init__(self) -> None:
        self.notifications: dict[str, dict] = {}
        self.tweets: dict[str, dict] = {}
        self.users: dict[str, dict] = {}

    def add(
        self,
        notif_id: str,
        timestamp_ms: int,
        *,
        tweet_id: str | None = None,
        text: str | None = None,
        screen_name: str | None = None,
        icon_id: str | None = None,
        icon_missing: bool = False,
        omit_full_text: bool = False,
    ) -> _PageBuilder:
        target_objects = [{"tweet": {"id": tweet_id}}] if tweet_id is not None else []
        self.notifications[notif_id] = {
            "id": notif_id,
            "timestampMs": str(timestamp_ms),
            "icon": None if icon_missing else {"id": icon_id},
            "message": {"text": "notification text"},
            "template": {"aggregateUserActionsV1": {"targetObjects": target_objects}},
        }
        if tweet_id is not None:
            user_id = f"user-{tweet_id}"
            tweet_entry: dict = {"user_id_str": user_id}
            if not omit_full_text:
                tweet_entry["full_text"] = text if text is not None else f"text-{tweet_id}"
            self.tweets[tweet_id] = tweet_entry
            resolved_screen_name = screen_name if screen_name is not None else "alice"
            self.users[user_id] = {"screen_name": resolved_screen_name}
        return self

    def add_bell(
        self,
        notif_id: str,
        timestamp_ms: int,
        *,
        from_user_ids: list[str],
        screen_names: dict[str, str] | None = None,
        icon_id: str | None = None,
    ) -> _PageBuilder:
        """A content-aggregate ("bell") notification: `targetObjects == []`
        (no linked tweet at all) but `fromUsers` names who posted -- exactly
        the shape digest/collectors/x.py's module docstring describes under
        "The problem": the notification names WHO posted, with nothing
        linkable inside it, hence the phase-2 `fetch_user_posts` path.
        """
        screen_names = screen_names or {}
        self.notifications[notif_id] = {
            "id": notif_id,
            "timestampMs": str(timestamp_ms),
            "icon": {"id": icon_id if icon_id is not None else "bell_icon"},
            "message": {"text": "notification text"},
            "template": {
                "aggregateUserActionsV1": {
                    "targetObjects": [],
                    "fromUsers": [{"user": {"id": uid}} for uid in from_user_ids],
                }
            },
        }
        for uid in from_user_ids:
            self.users[uid] = {"screen_name": screen_names.get(uid, f"user{uid}")}
        return self

    def build(self, next_cursor: str | None = None) -> dict:
        entries = []
        if next_cursor is not None:
            # Nested under an arbitrary key -- `_find_first` searches
            # recursively, mirroring real cursor-bottom entries' nesting.
            entries.append({"entryId": "cursor-bottom-0", "content": {"value": next_cursor}})
        return {
            "globalObjects": {
                "notifications": self.notifications,
                "tweets": self.tweets,
                "users": self.users,
            },
            "timeline": {"instructions": [{"addEntries": {"entries": entries}}]},
        }


def _page(next_cursor: str | None = None) -> _PageBuilder:
    return _PageBuilder()


class FakeXClient:
    """Fake honoring the `client.v11.notifications_all` surface `collect` uses.

    `pages` is the sequence of raw resp dicts returned on successive calls
    (call 1 -> pages[0], call 2 -> pages[1], ...) -- one fake, one call
    order, whether it's the very first fetch or a paginated one, matching
    how `collect` now always goes through the same method (unlike the old
    twikit `Result`/`.next()` split). `error_at_call` (1-based) + `error`,
    if given, makes that specific call raise `error` instead of returning a
    page -- covers both a first-fetch failure (auth/rate-limit/generic) and
    a later-page pagination failure with one mechanism.
    """

    def __init__(
        self,
        pages: list[Any] | None = None,
        *,
        error_at_call: int | None = None,
        error: Exception | None = None,
        gql: Any = None,
    ) -> None:
        self._pages = pages if pages is not None else []
        self._error_at_call = error_at_call
        self._error = error
        self.cursors_requested: list[str | None] = []
        self.v11 = self
        # Phase 2 (post-fetch) surface -- a fake honoring
        # `client.gql.user_tweets`, defaulting to one with zero configured
        # responses (tests that never exercise phase 2 never touch this).
        self.gql = gql if gql is not None else FakeGqlClient()

    @property
    def calls(self) -> int:
        return len(self.cursors_requested)

    async def notifications_all(self, count: int, cursor: str | None) -> tuple[Any, None]:
        self.cursors_requested.append(cursor)
        call_index = len(self.cursors_requested)
        if self._error_at_call == call_index and self._error is not None:
            raise self._error
        page_index = call_index - 1
        if page_index >= len(self._pages):
            raise AssertionError(
                "collect() called notifications_all more times than pages configured -- "
                "it should have stopped via the next-cursor guard instead"
            )
        return self._pages[page_index], None


# --- Phase 2 fixtures: content-aggregate ("bell") notifications and the
# per-account `client.gql.user_tweets` fetch they trigger. See
# digest/collectors/x.py's module docstring ("The problem"/"Required
# implementation") and `fetch_user_posts`'s docstring for the verified real
# response shape these mirror.


def _id_for_timestamp(timestamp_ms: int) -> str:
    """Inverse of `_tweet_id_to_timestamp_ms` -- a synthetic Snowflake tweet
    id that decodes back to exactly `timestamp_ms`. Lets tests build posts
    with a specific, known timestamp without depending on real tweet ids.
    """
    return str((timestamp_ms - x_module._X_SNOWFLAKE_EPOCH_MS) << 22)


def _legacy_tweet(tweet_id: str, text: str, **extra: Any) -> dict:
    """One tweet's `legacy` object -- the exact dict shape
    `_find_all(resp, 'full_text')` is verified to yield (`full_text`/
    `id_str` as direct sibling keys). `extra` lets a specific test add more
    fields (e.g. `entities` to probe the mention-shadowing safety fix in
    `_resolve_post_screen_name`).
    """
    return {
        "id_str": tweet_id,
        "full_text": text,
        "created_at": "Wed Jan 01 00:00:00 +0000 2026",
        **extra,
    }


def _user_tweets_response(legacies: list[dict]) -> dict:
    """A realistic nested `client.gql.user_tweets` response.

    Mirrors the verified real shape (`fetch_user_posts`'s docstring):
    tweets nested under `data.user.result.timeline_v2.timeline.
    instructions[].entries[]...tweet_results.result.legacy`. `_find_all`
    doesn't care about the exact intermediate keys -- only that each
    `legacy` dict is reachable somewhere in the tree -- so this fixture
    nests realistically anyway, to exercise depth rather than just bare
    presence.
    """
    entries = [
        {
            "entryId": f"tweet-{legacy.get('id_str', 'unknown')}",
            "content": {
                "itemContent": {
                    "tweet_results": {
                        "result": {
                            "rest_id": legacy.get("id_str", "unknown"),
                            "legacy": legacy,
                        }
                    }
                }
            },
        }
        for legacy in legacies
    ]
    return {
        "data": {
            "user": {
                "result": {"timeline_v2": {"timeline": {"instructions": [{"entries": entries}]}}}
            }
        }
    }


class FakeGqlClient:
    """Fake honoring the `client.gql.user_tweets` surface `fetch_user_posts` uses.

    `responses` maps user_id -> either a raw resp dict (returned as-is) or
    an `Exception` INSTANCE (raised instead of returned) -- one fake, keyed
    by the user id `collect`'s phase-2 loop / `fetch_user_posts` calls with.
    A user_id with no configured entry gets an empty (zero-tweet, but not
    erroring) response, mirroring an account with nothing new to fetch.
    """

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self._responses = responses if responses is not None else {}
        self.calls: list[str] = []

    async def user_tweets(self, user_id: str, count: int, cursor: str | None) -> tuple[Any, None]:
        self.calls.append(user_id)
        outcome = self._responses.get(user_id, _user_tweets_response([]))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, None


# --- first run seeding ---


async def test_first_run_seeds_newest_timestamp_plus_one_and_emits_no_items():
    # Codex review finding B: the seed is newest_seen_timestamp + 1, not the
    # bare newest timestamp -- see digest/collectors/x.py's `collect`
    # docstring, "First run" / "Why +1, not the bare newest timestamp".
    page = (
        _page()
        .add("n2", 2000, tweet_id="200", text="hello")
        .add("n1", 1000, tweet_id="100", text="hi")
        .build()
    )
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={})

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "2001"}
    assert result.failed is False


async def test_first_run_empty_page_seeds_zero_cursor():
    client = FakeXClient(pages=[_page().build()])

    result = await collect(client, cursors={})

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "0"}
    assert result.failed is False


async def test_first_run_tweetless_page_seeds_from_its_newest_timestamp():
    # Every notification lacks a linked tweet (e.g. pure follow events), but
    # each still carries its own timestamp_ms -- seeding no longer depends
    # on any notification having a linked tweet. This is also the Finding A
    # regression case: zero tweet-linked notifications at all
    # (tweet_linked_failed == 0 == tweet_linked_succeeded) is NOT a systemic
    # failure, so this must still seed normally rather than being flagged
    # failed.
    page = _page().add("n2", 1500).add("n1", 1200).build()
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={})

    assert result.items == []
    # Finding B: newest (1500) + 1.
    assert result.cursor_updates == {("x", "notifications"): "1501"}
    assert result.failed is False


async def test_first_run_all_tweet_linked_malformed_is_systemic_failure_no_seed(caplog):
    """Codex review finding A: the ordering bug this fix addresses -- on a
    first run, if EVERY tweet-linked notification on the seed page fails to
    have its tweet's text resolved (e.g. an endpoint/response-shape break)
    and NONE succeed, this must be treated as a systemic failure BEFORE the
    seed commits: failed=True, no cursor entry at all (the scope stays
    first-run for the next attempt), and a warning logged. The old, buggy
    ordering would have seeded a cursor here anyway and returned success,
    permanently losing everything older than that seed once the shape break
    was fixed.
    """
    page = (
        _page()
        .add("n2", 200, tweet_id="200", omit_full_text=True)
        .add("n1", 100, tweet_id="100", omit_full_text=True)
        .build()
    )
    client = FakeXClient(pages=[page])

    with caplog.at_level("WARNING", logger="digest.collectors.x"):
        result = await collect(client, cursors={})

    assert result.items == []
    assert result.failed is True
    # No cursor seeded at all -- scope stays first-run for the next attempt.
    assert result.cursor_updates == {}
    assert any(
        "2 tweet-linked notification(s) all failed to normalize" in record.message
        for record in caplog.records
    )
    assert any("cursor not seeded" in record.message for record in caplog.records)


# --- Codex review finding B: first-run seed's newest+1 offset prevents
# seed-time history from leaking into the first real digest ---


async def test_first_run_seed_plus_one_excludes_seed_time_notification_next_run(monkeypatch):
    """Two-phase regression for Finding B: a first run seeds the cursor at
    newest_seen_timestamp + 1 (not AT the newest timestamp). The very next
    run's inclusive `>=` item-selection check must then EXCLUDE a content
    notification sitting exactly at the seed's newest timestamp (it was
    already visible when the seed was taken, and the seed run stored
    nothing for the (source, source_id) dedup to catch) while still
    INCLUDING a notification one millisecond later (genuinely new relative
    to the seed).
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    # --- Phase 1: first run, seeds from timestamp 5000. ---
    phase1_page = _page().add("n1", 5000, tweet_id="500", text="seed-time tweet").build()
    phase1_client = FakeXClient(pages=[phase1_page])

    phase1_result = await collect(phase1_client, cursors={})

    assert phase1_result.items == []
    assert phase1_result.cursor_updates == {("x", "notifications"): "5001"}
    phase2_cursor = phase1_result.cursor_updates[("x", "notifications")]

    # --- Phase 2: incremental run fed the seed (5001). ---
    phase2_page = (
        _page()
        # One ms later than the seed's newest -- genuinely new, must appear.
        .add("n3", 5001, tweet_id="502", text="one ms later")
        # Exactly at the seed's newest timestamp -- already visible when the
        # seed was taken, and never stored anywhere; must NOT reappear.
        .add("n2", 5000, tweet_id="501", text="at seed time, must not reappear")
        .build()
    )
    phase2_client = FakeXClient(pages=[phase2_page])

    phase2_result = await collect(phase2_client, cursors={"notifications": phase2_cursor})

    assert [i.source_id for i in phase2_result.items] == ["502"]
    assert "501" not in [i.source_id for i in phase2_result.items]
    assert phase2_result.failed is False
    # Bridged on page 1 (5000 < cursor 5001); newest seen (5001) is not
    # strictly greater than the cursor (5001), so no cursor advancement.
    assert phase2_result.cursor_updates == {}


# --- incremental ---


async def test_incremental_only_timestamps_greater_than_cursor_oldest_first():
    page = (
        _page()
        .add("n3", 300, tweet_id="300", text="third")
        .add("n2", 200, tweet_id="200", text="second")
        .add("n1", 100, tweet_id="100", text="first")
        .build()
    )
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "150"})

    assert [i.source_id for i in result.items] == ["200", "300"]
    assert [i.text for i in result.items] == ["second", "third"]
    assert result.cursor_updates == {("x", "notifications"): "300"}
    assert result.failed is False


async def test_incremental_no_new_timestamps_leaves_cursor_untouched():
    page = _page().add("n1", 100, tweet_id="100", text="old").build()
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "500"})

    assert result.items == []
    assert result.cursor_updates == {}


async def test_incremental_notification_without_tweet_is_skipped_but_still_counts_for_cursor():
    page = (
        _page()
        .add("n2", 50)  # followed you: no linked tweet
        .add("n1", 100, tweet_id="100", text="hi")
        .build()
    )
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "0"})

    assert [i.source_id for i in result.items] == ["100"]
    # Newest timestamp across ALL notifications this page (100), even
    # though the tweetless one (50) never became an item -- chronology is
    # kind/linkage-agnostic (redesign point 3/4).
    assert result.cursor_updates == {("x", "notifications"): "100"}


async def test_incremental_notification_without_screen_name_skipped_but_advances_cursor():
    page = (
        _page()
        .add("n2", 200, tweet_id="200", text="no author", screen_name="")
        .add("n1", 100, tweet_id="100", text="has author")
        .build()
    )
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "0"})

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

    page1 = (
        _page()
        .add("n5", 500, tweet_id="500", text="fifth")
        .add("n4", 400, tweet_id="400", text="fourth")
        .build(next_cursor="c1")
    )
    page2 = (
        _page()
        .add("n3", 300, tweet_id="300", text="third")
        .add("n2b", 250, tweet_id="250", text="second-b")
        .build(next_cursor="c2")
    )
    page3 = (
        _page()
        .add("n2", 200, tweet_id="200", text="second")
        # timestamp <= cursor: bridges here
        .add("n1", 100, tweet_id="100", text="first")
        .build(next_cursor="c3")
    )
    client = FakeXClient(pages=[page1, page2, page3])

    result = await collect(client, cursors={"notifications": "150"})

    assert [i.source_id for i in result.items] == ["200", "250", "300", "400", "500"]
    assert result.cursor_updates == {("x", "notifications"): "500"}
    assert result.failed is False
    # 1 initial fetch (page 1) + 2 subsequent fetches (-> page 2, -> page 3).
    assert client.calls == 3
    # sleep awaited once between each pair of page fetches, never before the first.
    assert sleeps == [1.5, 1.5]


async def test_pagination_cursor_bridged_on_first_page_makes_exactly_one_fetch_no_sleep(
    monkeypatch,
):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page = (
        _page()
        .add("n2", 500, tweet_id="500", text="new")
        # timestamp <= cursor: bridged on page 1
        .add("n1", 100, tweet_id="100", text="old")
        .build(next_cursor="c1")
    )
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "150"})

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
    timestamps = (600, 500, 400, 300, 200, 100)
    pages = [
        _page().add(f"n{ts}", ts, tweet_id=str(ts), text=f"t{ts}").build(next_cursor=f"c{ts}")
        for ts in timestamps
    ]
    client = FakeXClient(pages=pages)

    with caplog.at_level("WARNING", logger="digest.collectors.x"):
        result = await collect(client, cursors={"notifications": "50"})

    # Page 6 (timestamp 100) is never fetched -- only 5 total fetches (page 1
    # through page 5).
    assert client.calls == 5
    # Cursor advances to the newest FETCHED timestamp (600) -- plain
    # high-water-mark semantics, same formula as the bridged case. The gap
    # below 200 (the oldest fetched) is gone for good, on purpose.
    assert result.cursor_updates == {("x", "notifications"): "600"}
    # Items are unaffected by the cursor-advancement choice: everything
    # fetched and > the (unchanged, true) input cursor(50) is still collected.
    assert [i.source_id for i in result.items] == ["200", "300", "400", "500", "600"]
    assert any("backlog exceeded the page cap" in record.message for record in caplog.records)
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
    timestamps = (600, 500, 400, 300, 200, 100)
    phase1_pages = [
        _page().add(f"n{ts}", ts, tweet_id=str(ts), text=f"t{ts}").build(next_cursor=f"c{ts}")
        for ts in timestamps
    ]
    phase1_client = FakeXClient(pages=phase1_pages)

    phase1_result = await collect(phase1_client, cursors={"notifications": "50"})

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
    phase2_page = (
        _page()
        .add("n900", 900, tweet_id="900", text="newest")
        .add("n700", 700, tweet_id="700", text="newer")
        # timestamp 550 <= cursor(600): bridges here, on page 1.
        .add("n550", 550, tweet_id="550", text="already-seen boundary")
        .build()
    )
    phase2_client = FakeXClient(pages=[phase2_page])

    phase2_result = await collect(phase2_client, cursors={"notifications": phase2_cursor})

    # Bridged on page 1 -- no further fetches.
    assert phase2_client.calls == 1
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

    page1 = (
        _page()
        .add("n2", 100, tweet_id="100", text="b")
        .add("n1", 80, tweet_id="80", text="a")
        .build(next_cursor="c1")
    )
    client = FakeXClient(
        pages=[page1], error_at_call=2, error=RuntimeError("graphql shape changed")
    )

    result = await collect(client, cursors={"notifications": "50"})

    assert [i.source_id for i in result.items] == ["80", "100"]
    # FAILURE: the cursor is not advanced at all -- the key isn't even
    # present in cursor_updates, not just "unchanged in value".
    assert ("x", "notifications") not in result.cursor_updates
    assert result.cursor_updates == {}
    assert result.failed is True
    assert client.calls == 2


# --- Codex review finding A: per-notification malformation is gentle, never
# fails the page (contrast with the pipeline-level tests further below) ---


async def test_incremental_tweet_missing_full_text_is_skipped_others_collected():
    page = (
        _page()
        .add("n2", 200, tweet_id="200", omit_full_text=True)
        .add("n1", 100, tweet_id="100", text="good")
        .build()
    )
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "0"})

    assert [i.source_id for i in result.items] == ["100"]
    # The malformed notification's OWN timestamp (200) is still readable
    # (its unresolved full_text is independent of its own timestamp) and
    # still counts for cursor chronology.
    assert result.cursor_updates == {("x", "notifications"): "200"}
    assert result.failed is False


async def test_all_tweet_linked_notifications_malformed_is_systemic_failure_no_cursor_advance(
    caplog,
):
    """Codex review finding A: when EVERY tweet-linked notification fetched
    this run fails to have its tweet's text resolved and NONE succeed, this
    is a systemic parsing break (e.g. an endpoint/response-shape change
    breaking `full_text` resolution for every notification), not a pile of
    isolated malformations -- advancing the cursor past the entire lost
    batch would silently erase it. Contrast with
    test_incremental_tweet_missing_full_text_is_skipped_others_collected
    above, where one notification succeeds alongside the failure and the
    existing skip-and-still-advance behavior is preserved unchanged.
    """
    page = (
        _page()
        .add("n2", 200, tweet_id="200", omit_full_text=True)
        .add("n1", 100, tweet_id="100", omit_full_text=True)
        .build()
    )
    client = FakeXClient(pages=[page])

    with caplog.at_level("WARNING", logger="digest.collectors.x"):
        result = await collect(client, cursors={"notifications": "0"})

    assert result.items == []
    assert result.failed is True
    # Systemic failure: the cursor key isn't written at all, not just left
    # unchanged in value -- same "no cursor_updates entry" contract as the
    # pipeline-level FAILURE outcome.
    assert result.cursor_updates == {}
    assert any(
        "2 tweet-linked notification(s) all failed to normalize" in record.message
        for record in caplog.records
    )
    assert any("possible endpoint/shape change" in record.message for record in caplog.records)


# --- Codex review finding A: pipeline-level surprises set failed=True but
# keep whatever was safely gathered before the surprise ---


async def test_pagination_page_processing_exception_mid_pagination_keeps_prior_page_items(
    monkeypatch,
):
    """`_extract_candidates`/`parse_notifications_page` are fully defensive by
    design (see their docstrings) and never raise for any realistic raw
    payload shape -- so there is no way to construct a raw response that
    naturally makes page-processing itself raise (contrast with the
    now-real client-exception test above, which covers the "later page
    fetch fails" case). `collect` still wraps each page's extraction in its
    own try/except as defense-in-depth (mirroring telegram.py's
    partial-results-survive behavior), in case a future change to the
    parsing layer reintroduces a raise -- this test proves that wrapping
    still does the right thing if it ever fires, by forcing
    `_extract_candidates` itself to raise on page 2: page 1's
    already-gathered items must survive, `failed` must be True, and the
    cursor must be left untouched, exactly like the client-level
    pagination failure above, just at a different layer of the pipeline.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page1 = (
        _page()
        .add("n4", 400, tweet_id="400", text="fourth")
        .add("n3", 300, tweet_id="300", text="third")
        .build(next_cursor="c1")
    )
    page2 = _page().add("n2", 200, tweet_id="200", text="second").build()
    client = FakeXClient(pages=[page1, page2])

    real_extract_candidates = x_module._extract_candidates
    call_count = 0

    def fake_extract_candidates(resp):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated page-processing break")
        return real_extract_candidates(resp)

    monkeypatch.setattr(x_module, "_extract_candidates", fake_extract_candidates)

    result = await collect(client, cursors={"notifications": "150"})

    # Page 2 never contributes any items -- extraction raised before
    # anything from it could be merged in -- but page 1's items survive.
    assert [i.source_id for i in result.items] == ["300", "400"]
    # FAILURE: cursor is not advanced at all.
    assert result.cursor_updates == {}
    assert result.failed is True
    assert client.calls == 2


# --- Codex review finding B: engagement notifications (likes, reposts) are
# never emitted as items -- the notification KIND is the discriminator, not
# tweet/notification age. Their timestamps still advance chronology exactly
# like any other notification (bridging + cursor), independent of kind. ---


async def test_like_notification_on_post_cursor_tweet_skipped_as_item_but_advances_cursor():
    page = (
        _page()
        .add("n1", 999, tweet_id="999", text="owner's own tweet", icon_id="heart_icon")
        .build()
    )
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "500"})

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "999"}
    assert result.failed is False


async def test_repost_notification_on_post_cursor_tweet_skipped_as_item():
    page = (
        _page()
        .add("n1", 999, tweet_id="999", text="owner's own tweet", icon_id="retweet_icon")
        .build()
    )
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "500"})

    assert result.items == []
    assert result.cursor_updates == {("x", "notifications"): "999"}


async def test_mention_and_reply_kind_notifications_are_included():
    page = (
        _page()
        .add("n2", 300, tweet_id="300", text="hey @you", icon_id="at_icon")
        .add("n1", 200, tweet_id="200", text="a reply", icon_id="reply_icon")
        .build()
    )
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "0"})

    assert [i.source_id for i in result.items] == ["200", "300"]


async def test_unrecognized_icon_kind_fails_open_and_is_included():
    page = (
        _page()
        .add("n1", 100, tweet_id="100", text="quote maybe?", icon_id="some_future_icon_id")
        .build()
    )
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "0"})

    assert [i.source_id for i in result.items] == ["100"]


async def test_missing_icon_fails_open_and_is_included():
    page = _page().add("n1", 100, tweet_id="100", text="hi", icon_missing=True).build()
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "0"})

    assert [i.source_id for i in result.items] == ["100"]


async def test_follow_notification_no_tweet_unaffected_by_icon_kind():
    page = (
        _page()
        .add("n2", 150, icon_id="user_icon")  # followed you, no tweet
        .add("n1", 100, tweet_id="100", text="hi", icon_id="at_icon")
        .build()
    )
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "0"})

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

    page1 = (
        _page()
        .add("n2", 1500, tweet_id="777", text="own tweet", icon_id="heart_icon")
        .build(next_cursor="c1")
    )
    page2 = (
        _page()
        # timestamp <= cursor(1000): bridges, purely via an engagement
        # notification's chronology -- no content/tweet-linked notification
        # is needed to trigger a bridge.
        .add("n1", 900, tweet_id="888", text="own tweet", icon_id="retweet_icon")
        .build(next_cursor="c2")
    )
    client = FakeXClient(pages=[page1, page2])

    result = await collect(client, cursors={"notifications": "1000"})

    assert client.calls == 2
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
    page1 = (
        _page()
        .add("n1", 2000, tweet_id="5", text="an old tweet of the owner's", icon_id="heart_icon")
        .build(next_cursor="c1")
    )
    # Page 2: a genuinely new, previously-unseen mention (large tweet id,
    # new timestamp) -- must be reached and collected, followed by a
    # notification old enough to legitimately bridge (ends the test cleanly).
    page2 = (
        _page()
        .add("n2", 2100, tweet_id="3000", text="hey @you", icon_id="at_icon")
        # timestamp <= cursor(1000): bridges here.
        .add("n3", 900, tweet_id="10", text="stale", icon_id="reply_icon")
        .build(next_cursor="c2")
    )
    client = FakeXClient(pages=[page1, page2])

    result = await collect(client, cursors={"notifications": cursor})

    # Pagination continued past page 1 -- 2 total fetches -- instead of
    # falsely bridging on the old-tweet-id-but-new-timestamp like.
    assert client.calls == 2
    # The page-2 mention was reached and collected; the page-1 like (pure
    # engagement) and the stale page-2 reply (timestamp <= cursor) were not.
    assert [i.source_id for i in result.items] == ["3000"]
    assert result.cursor_updates == {("x", "notifications"): "2100"}
    assert result.failed is False


# --- Codex review finding B: equal-timestamp boundary -- a tie between a
# notification's timestamp_ms and the cursor cannot distinguish "already
# seen" from "genuinely unseen, coincidentally same millisecond", so item
# selection is inclusive (>=) and bridging requires strictly-older (<),
# never a bare tie. The overlap this creates is absorbed downstream by the
# (source, source_id) UNIQUE constraint. ---


async def test_unseen_notification_tied_with_cursor_timestamp_is_emitted_as_item():
    """An item candidate whose timestamp_ms exactly equals the cursor is
    included, not excluded -- equality alone can't tell an unseen
    notification apart from an already-seen one sharing the cursor's exact
    millisecond, so this module resolves the tie by re-emitting rather than
    risk silently dropping a genuinely new item.
    """
    page = _page().add("n1", 1000, tweet_id="100", text="tied").build()
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "1000"})

    assert [i.source_id for i in result.items] == ["100"]
    # Item selection is inclusive, but cursor advancement is unaffected:
    # 1000 is not > 1000, so the high-water-mark doesn't move (matches
    # test_incremental_no_new_timestamps_leaves_cursor_untouched's pattern).
    assert result.cursor_updates == {}
    assert result.failed is False


async def test_tied_notification_on_later_page_is_still_reached_and_emitted(monkeypatch):
    """A tie alone must not bridge, or a still-unseen notification sharing
    the cursor's exact timestamp on a LATER page would never be reached --
    pagination would have already stopped one page too early on the first
    tied item. Page 1 ties with the cursor (1000 == cursor) and must NOT
    bridge, so pagination continues to page 2, where a second notification
    also tied at 1000 is reached and emitted, before a strictly-older
    notification (900 < 1000) finally bridges.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page1 = _page().add("n1", 1000, tweet_id="100", text="tied-page1").build(next_cursor="c1")
    page2 = (
        _page()
        .add("n2", 1000, tweet_id="200", text="tied-page2")
        # strictly older than the cursor: bridges here.
        .add("n3", 900, tweet_id="300", text="stale")
        .build(next_cursor="c2")
    )
    client = FakeXClient(pages=[page1, page2])

    result = await collect(client, cursors={"notifications": "1000"})

    # Pagination continued past page 1 -- the tie alone did not bridge.
    assert client.calls == 2
    assert [i.source_id for i in result.items] == ["100", "200"]
    assert result.failed is False


def test_page_bridges_cursor_requires_strictly_older_not_equal():
    """Direct unit coverage of `_page_bridges_cursor`'s boundary: a tie
    (timestamp_ms == cursor) must NOT bridge -- only a STRICTLY older
    notification, or an empty page, does. (No pre-existing higher-level
    bridge fixture happened to assert equality-bridging -- they all used
    strictly-older timestamps already -- so this is new coverage, not a
    behavior-changing regression update to an existing test.)
    """
    assert x_module._page_bridges_cursor(1, [1000], 1000) is False
    assert x_module._page_bridges_cursor(1, [999], 1000) is True
    assert x_module._page_bridges_cursor(0, [], 1000) is True


# --- direct unit coverage: parse_notifications_page / _find_first ---
#
# These exercise the pure raw-JSON parse step in isolation, without going
# through collect() at all -- the whole point of splitting it out (see
# digest/collectors/x.py's `parse_notifications_page` docstring).


def test_parse_notifications_page_well_formed_payload():
    resp = (
        _page()
        .add("n1", 100, tweet_id="10", text="hello", screen_name="alice", icon_id="at_icon")
        .build(next_cursor="cur-1")
    )

    records, next_cursor = x_module.parse_notifications_page(resp)

    assert len(records) == 1
    record = records[0]
    assert record.timestamp_ms == 100
    assert record.tweet_id == "10"
    assert record.text == "hello"
    assert record.screen_name == "alice"
    assert record.icon_id == "at_icon"
    assert next_cursor == "cur-1"


def test_parse_notifications_page_missing_global_objects_returns_empty():
    records, next_cursor = x_module.parse_notifications_page({})

    assert records == []
    assert next_cursor is None


def test_parse_notifications_page_malformed_single_notification_skipped_while_siblings_parse():
    resp = _page().build()
    resp["globalObjects"]["notifications"] = {
        "bad": {
            # No timestampMs at all -- unreadable, dropped silently.
            "icon": {},
            "message": {"text": "m"},
            "template": {"aggregateUserActionsV1": {"targetObjects": []}},
        },
        "good": {
            "timestampMs": "500",
            "icon": {},
            "message": {"text": "m"},
            "template": {"aggregateUserActionsV1": {"targetObjects": []}},
        },
    }

    records, _ = x_module.parse_notifications_page(resp)

    assert len(records) == 1
    assert records[0].timestamp_ms == 500


def test_parse_notifications_page_cursor_bottom_extraction():
    resp = _page().add("n1", 100).build(next_cursor="abc123")

    _, next_cursor = x_module.parse_notifications_page(resp)

    assert next_cursor == "abc123"


def test_parse_notifications_page_absent_cursor_returns_none():
    resp = _page().add("n1", 100).build()

    _, next_cursor = x_module.parse_notifications_page(resp)

    assert next_cursor is None


def test_find_first_locates_nested_key():
    obj = {"a": {"b": [{"c": 1}, {"key": "value!"}]}}

    assert x_module._find_first(obj, "key") == "value!"


def test_find_first_returns_none_when_absent():
    assert x_module._find_first({"a": {"b": 1}}, "missing") is None


def test_find_first_returns_none_for_non_dict_non_list():
    assert x_module._find_first("just a string", "key") is None


# --- errors: auth/cookie failures never retry ---


async def test_auth_error_unauthorized_flags_failed_no_retry():
    from twikit.errors import Unauthorized

    client = FakeXClient(error_at_call=1, error=Unauthorized("nope"))

    result = await collect(client, cursors={"notifications": "0"})

    assert result.failed is True
    assert result.items == []
    assert result.cursor_updates == {}
    assert client.calls == 1


async def test_auth_error_forbidden_flags_failed():
    from twikit.errors import Forbidden

    client = FakeXClient(error_at_call=1, error=Forbidden("nope"))

    result = await collect(client, cursors={"notifications": "0"})

    assert result.failed is True
    assert client.calls == 1


async def test_account_locked_flags_failed():
    from twikit.errors import AccountLocked

    client = FakeXClient(error_at_call=1, error=AccountLocked("locked"))

    result = await collect(client, cursors={"notifications": "0"})

    assert result.failed is True
    assert client.calls == 1


async def test_account_suspended_flags_failed():
    from twikit.errors import AccountSuspended

    client = FakeXClient(error_at_call=1, error=AccountSuspended("suspended"))

    result = await collect(client, cursors={"notifications": "0"})

    assert result.failed is True
    assert client.calls == 1


# --- errors: rate limit ---


async def test_rate_limit_error_flags_failed_no_retry():
    from twikit.errors import TooManyRequests

    client = FakeXClient(error_at_call=1, error=TooManyRequests("slow down"))

    result = await collect(client, cursors={"notifications": "0"})

    assert result.failed is True
    assert client.calls == 1


# --- errors: anything else never crashes the run ---


async def test_other_exception_flags_failed_never_crashes():
    client = FakeXClient(error_at_call=1, error=RuntimeError("graphql shape changed"))

    result = await collect(client, cursors={"notifications": "0"})

    assert result.failed is True
    assert result.items == []


# --- url provenance ---


async def test_url_built_only_from_api_returned_screen_name_and_tweet_id():
    page = _page().add("n1", 999, tweet_id="999", text="hi", screen_name="bob").build()
    client = FakeXClient(pages=[page])

    result = await collect(client, cursors={"notifications": "0"})

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


# --- Phase 2: content-aggregate ("bell") notifications -- fetching post
# CONTENT directly for named accounts, since the notification itself
# carries nothing linkable (no tweet, only WHO posted). See
# digest/collectors/x.py's module docstring ("The problem"/"Required
# implementation") for the full rationale.


def test_tweet_id_to_timestamp_ms_known_value():
    # A real tweet id, independently computed against the documented X
    # Snowflake formula (id >> 22) + epoch -- not derived from the
    # collector's own constant, so this isn't a tautological self-check.
    assert x_module._tweet_id_to_timestamp_ms("1460198939211628547") == 1636973530949


def test_tweet_id_to_timestamp_ms_non_numeric_returns_none():
    assert x_module._tweet_id_to_timestamp_ms("not-a-number") is None
    assert x_module._tweet_id_to_timestamp_ms(None) is None


def test_find_all_returns_every_matching_dict():
    obj = {
        "a": {"full_text": "one", "id_str": "1"},
        "b": [{"full_text": "two", "id_str": "2"}, {"other": 1}],
    }

    hits = x_module._find_all(obj, "full_text")

    assert sorted(h["id_str"] for h in hits) == ["1", "2"]


def test_find_all_respects_max_depth():
    innermost = {"full_text": "buried", "id_str": "deep"}
    obj = innermost
    for _ in range(5):
        obj = {"nested": obj}

    assert x_module._find_all(obj, "full_text", max_depth=2) == []
    assert x_module._find_all(obj, "full_text", max_depth=10) == [innermost]


def test_resolve_post_screen_name_prefers_own_subtree_when_present():
    tweet_obj = {"full_text": "hi", "id_str": "1", "author": {"screen_name": "direct-author"}}

    assert x_module._resolve_post_screen_name(tweet_obj) == "direct-author"


def test_resolve_post_screen_name_ignores_mentions_in_entities():
    # A tweet mentioning someone else must not have THAT user's screen_name
    # picked up as if it were the author's -- see `_resolve_post_screen_name`'s
    # docstring / `_SCREEN_NAME_SEARCH_EXCLUDED_KEYS`.
    tweet_obj = {
        "full_text": "hi @someone",
        "id_str": "1",
        "entities": {"user_mentions": [{"screen_name": "someone"}]},
    }

    assert x_module._resolve_post_screen_name(tweet_obj) is None


def test_resolve_post_screen_name_absent_returns_none():
    assert x_module._resolve_post_screen_name({"full_text": "hi", "id_str": "1"}) is None


async def test_fetch_user_posts_parses_valid_and_skips_malformed():
    resp = _user_tweets_response(
        [
            _legacy_tweet("900001", "good"),
            {"id_str": "900002"},  # missing full_text
            {"full_text": "no id"},  # missing id_str
        ]
    )
    client = FakeXClient(gql=FakeGqlClient({"1": resp}))

    posts = await x_module.fetch_user_posts(client, "1", 20)

    assert [p.tweet_id for p in posts] == ["900001"]
    assert posts[0].text == "good"
    assert posts[0].screen_name is None


async def test_fetch_user_posts_never_raises_on_client_exception():
    client = FakeXClient(gql=FakeGqlClient({"1": RuntimeError("boom")}))

    posts = await x_module.fetch_user_posts(client, "1", 20)

    assert posts == []


def test_extract_post_fetch_targets_only_bell_style_notifications():
    resp = (
        _page()
        .add_bell("n1", 500, from_user_ids=["1"], screen_names={"1": "alice"})
        .add("n2", 400, tweet_id="400", text="a mention", icon_id="at_icon")
        # engagement, excluded -- same screen_name as n1 so this call
        # can't accidentally clobber the shared globalObjects.users entry.
        .add_bell("n3", 300, from_user_ids=["1"], screen_names={"1": "alice"}, icon_id="heart_icon")
        .build()
    )

    targets = x_module._extract_post_fetch_targets(resp)

    assert targets == {"1": "alice"}


def test_is_systemic_post_fetch_failure_boundary():
    assert x_module._is_systemic_post_fetch_failure(0, 0) is False
    assert x_module._is_systemic_post_fetch_failure(3, 2) is False
    assert x_module._is_systemic_post_fetch_failure(3, 3) is True


async def test_bell_notification_fetches_and_emits_posts_once_account_cursor_exists(monkeypatch):
    """P1 per-account cursors: an account with a PRE-EXISTING `posts:{id}`
    cursor row (i.e. not its first sight) has its fetched posts emitted
    immediately, filtered against ITS OWN cursor rather than the shared
    notifications cursor. (Contrast with
    test_first_sight_of_account_seeds_cursor_emits_no_items below, covering
    the no-backfill first-sight case.)
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page = _page().add_bell("n1", 500, from_user_ids=["111"], screen_names={"111": "alice"}).build()
    gql = FakeGqlClient({"111": _user_tweets_response([_legacy_tweet("900001", "hello world")])})
    client = FakeXClient(pages=[page], gql=gql)

    result = await collect(client, cursors={"notifications": "0", "posts:111": "0"})

    assert gql.calls == ["111"]
    post_items = [i for i in result.items if i.source_id == "900001"]
    assert len(post_items) == 1
    item = post_items[0]
    assert item.url == "https://x.com/alice/status/900001"
    assert item.author == "alice"
    assert item.text == "hello world"
    assert item.source == "x"
    assert item.chat_id is None
    assert result.failed is False
    # Cursor advances to the newest post captured (Snowflake-decoded from
    # "900001") -- a per-account high-water mark, independent of the
    # notifications cursor.
    assert ("x", "posts:111") in result.cursor_updates


async def test_posts_filtered_by_snowflake_timestamp_vs_account_cursor_inclusive_tie(monkeypatch):
    """P1: posts are filtered against the ACCOUNT's own `posts:{id}` cursor,
    not the shared notifications cursor -- the account cursor here is set
    to the same tie-boundary value the notifications cursor used to double
    as, to prove the (still inclusive, same tie rule as phase 1) filtering
    now reads from the right place.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    cursor_ms = 1700000000000

    def id_for(timestamp_ms: int) -> str:
        return str((timestamp_ms - x_module._X_SNOWFLAKE_EPOCH_MS) << 22)

    old_id = id_for(cursor_ms - 1000)
    tied_id = id_for(cursor_ms)
    new_id = id_for(cursor_ms + 1000)

    page = _page().add_bell("n1", cursor_ms, from_user_ids=["7"], screen_names={"7": "bob"}).build()
    gql = FakeGqlClient(
        {
            "7": _user_tweets_response(
                [
                    _legacy_tweet(old_id, "old post"),
                    _legacy_tweet(tied_id, "tied post"),
                    _legacy_tweet(new_id, "new post"),
                ]
            )
        }
    )
    client = FakeXClient(pages=[page], gql=gql)

    result = await collect(
        client,
        cursors={"notifications": str(cursor_ms), "posts:7": str(cursor_ms)},
    )

    ids = {i.source_id for i in result.items}
    assert old_id not in ids
    assert tied_id in ids
    assert new_id in ids


async def test_first_run_never_fetches_posts_even_with_bell_notification():
    page = _page().add_bell("n1", 500, from_user_ids=["111"]).build()
    gql = FakeGqlClient({"111": _user_tweets_response([_legacy_tweet("1", "should not appear")])})
    client = FakeXClient(pages=[page], gql=gql)

    result = await collect(client, cursors={})

    assert result.items == []
    assert gql.calls == []


async def test_per_user_post_fetch_failure_skips_user_keeps_others_items(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page = (
        _page()
        .add_bell("n1", 500, from_user_ids=["1", "2"], screen_names={"1": "alice", "2": "bob"})
        .build()
    )
    gql = FakeGqlClient(
        {
            "1": RuntimeError("boom"),
            "2": _user_tweets_response([_legacy_tweet("900001", "hi")]),
        }
    )
    client = FakeXClient(pages=[page], gql=gql)

    # Both accounts already have a posts cursor (not first sight) -- user 1's
    # fetch fails and its cursor ("posts:1") must stay untouched; user 2's
    # succeeds and its items/cursor advance normally.
    result = await collect(client, cursors={"notifications": "0", "posts:1": "0", "posts:2": "0"})

    assert result.failed is False
    assert [i.source_id for i in result.items if i.source_id == "900001"] == ["900001"]
    assert ("x", "posts:1") not in result.cursor_updates
    assert ("x", "posts:2") in result.cursor_updates


async def test_all_user_post_fetches_failing_marks_run_failed_notification_items_survive(
    monkeypatch,
):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page = (
        _page()
        .add("n2", 300, tweet_id="300", text="a mention", icon_id="at_icon")
        .add_bell("n1", 500, from_user_ids=["1", "2"])
        .build()
    )
    gql = FakeGqlClient({"1": RuntimeError("boom"), "2": RuntimeError("boom2")})
    client = FakeXClient(pages=[page], gql=gql)

    result = await collect(client, cursors={"notifications": "0"})

    assert result.failed is True
    # Notification-derived item survives: a systemic PHASE-2 post-fetch
    # failure must not roll back the (already trustworthy) notifications
    # half of this run -- see `_is_systemic_post_fetch_failure`'s docstring.
    assert [i.source_id for i in result.items] == ["300"]


async def test_post_fetch_user_cap_respected_and_warns(monkeypatch, caplog):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    user_ids = [str(i) for i in range(25)]
    responses: dict[str, Any] = {uid: _user_tweets_response([]) for uid in user_ids[1:]}
    responses[user_ids[0]] = _user_tweets_response([_legacy_tweet("900001", "hi")])
    page = _page().add_bell("n1", 500, from_user_ids=user_ids).build()
    gql = FakeGqlClient(responses)
    client = FakeXClient(pages=[page], gql=gql)

    cursors = {"notifications": "0", f"posts:{user_ids[0]}": "0"}
    with caplog.at_level("WARNING", logger="digest.collectors.x"):
        result = await collect(client, cursors=cursors)

    assert len(gql.calls) == x_module._MAX_POST_FETCH_USERS
    assert any("exceeded the per-run fetch cap" in r.message for r in caplog.records)
    assert any("skipping 5" in r.message for r in caplog.records)
    # P1: the skipped accounts' cursors are explicitly called out as
    # untouched -- nothing is lost, a future run picks them up.
    assert any("cursors are left untouched" in r.message for r in caplog.records)
    assert [i.source_id for i in result.items if i.source_id == "900001"] == ["900001"]
    # The 5 accounts beyond the cap were never iterated at all -- their
    # posts cursors are absent from cursor_updates entirely.
    skipped_ids = user_ids[x_module._MAX_POST_FETCH_USERS :]
    assert not any(
        scope.startswith("posts:") and scope.split(":", 1)[1] in skipped_ids
        for source, scope in result.cursor_updates
        if source == "x"
    )


async def test_sleep_called_between_but_not_before_first_user_fetch(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page = _page().add_bell("n1", 500, from_user_ids=["1", "2", "3"]).build()
    gql = FakeGqlClient(
        {
            "1": _user_tweets_response([]),
            "2": _user_tweets_response([]),
            "3": _user_tweets_response([]),
        }
    )
    client = FakeXClient(pages=[page], gql=gql)

    await collect(client, cursors={"notifications": "0"})

    assert sleeps == [x_module._PAGE_FETCH_DELAY_SECONDS, x_module._PAGE_FETCH_DELAY_SECONDS]


async def test_engagement_notification_never_triggers_post_fetch(monkeypatch):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    # A like/repost notification's icon marks it as engagement -- even
    # though this fixture gives it `fromUsers` data (via add_bell), it must
    # never be treated as a phase-2 post-fetch target.
    page = _page().add_bell("n1", 500, from_user_ids=["111"], icon_id="heart_icon").build()
    gql = FakeGqlClient({"111": _user_tweets_response([_legacy_tweet("1", "should not fetch")])})
    client = FakeXClient(pages=[page], gql=gql)

    result = await collect(client, cursors={"notifications": "0"})

    assert gql.calls == []
    assert result.items == []


# --- Codex review finding P2: retweeted_status_result / quoted_status_result
# subtrees embed the ORIGINAL tweet a wrapper is reposting/quoting -- these
# must never be discovered as separate items, and the wrapper's own
# screen_name must never be resolved from inside them (misattribution). ---


def test_find_all_excludes_retweeted_and_quoted_status_result_subtrees():
    obj = {
        "full_text": "wrapper text",
        "id_str": "1",
        "retweeted_status_result": {
            "result": {"legacy": {"full_text": "embedded original", "id_str": "2"}}
        },
        "quoted_status_result": {
            "result": {"legacy": {"full_text": "embedded quoted", "id_str": "3"}}
        },
    }

    hits = x_module._find_all(obj, "full_text", exclude_keys=x_module._RETWEET_QUOTE_EXCLUDED_KEYS)

    assert [h["id_str"] for h in hits] == ["1"]


def test_resolve_post_screen_name_ignores_retweeted_status_result():
    tweet_obj = {
        "full_text": "hi",
        "id_str": "1",
        "retweeted_status_result": {"result": {"legacy": {"screen_name": "original_author"}}},
    }

    assert x_module._resolve_post_screen_name(tweet_obj) is None


def test_resolve_post_screen_name_ignores_quoted_status_result():
    tweet_obj = {
        "full_text": "hi",
        "id_str": "1",
        "quoted_status_result": {"result": {"legacy": {"screen_name": "quoted_author"}}},
    }

    assert x_module._resolve_post_screen_name(tweet_obj) is None


async def test_retweeted_status_result_not_emitted_as_separate_item_and_not_misattributed(
    monkeypatch,
):
    """A retweet wrapper embeds the original tweet under
    `retweeted_status_result` -- it must be emitted as exactly ONE item (the
    wrapper), never a second item for the embedded original, and the
    wrapper's item must carry the NOTIFIED account's screen_name (`alice`),
    never the embedded original's author (`original_author`) --
    `_resolve_post_screen_name` returns None in practice (verified live), so
    without the P2 exclusion the embedded original's screen_name would leak
    through and misattribute the wrapper to the wrong account.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    wrapper = _legacy_tweet(
        "500",
        "RT @someone: hello",
        retweeted_status_result={
            "result": {
                "rest_id": "999",
                "legacy": _legacy_tweet("999", "the original tweet text"),
                "core": {
                    "user_results": {"result": {"legacy": {"screen_name": "original_author"}}}
                },
            }
        },
    )
    gql = FakeGqlClient({"1": _user_tweets_response([wrapper])})
    page = _page().add_bell("n1", 500, from_user_ids=["1"], screen_names={"1": "alice"}).build()
    client = FakeXClient(pages=[page], gql=gql)

    result = await collect(client, cursors={"notifications": "0", "posts:1": "0"})

    ids = [i.source_id for i in result.items]
    assert ids == ["500"]
    assert "999" not in ids
    item = next(i for i in result.items if i.source_id == "500")
    assert item.author == "alice"
    assert item.text == "RT @someone: hello"


async def test_quoted_status_result_not_emitted_as_separate_item_and_not_misattributed(
    monkeypatch,
):
    """Sibling of the retweeted_status_result test above, for quote-tweets."""

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    wrapper = _legacy_tweet(
        "600",
        "check this out",
        quoted_status_result={
            "result": {
                "rest_id": "888",
                "legacy": _legacy_tweet("888", "the quoted tweet text"),
                "core": {"user_results": {"result": {"legacy": {"screen_name": "quoted_author"}}}},
            }
        },
    )
    gql = FakeGqlClient({"2": _user_tweets_response([wrapper])})
    page = _page().add_bell("n1", 500, from_user_ids=["2"], screen_names={"2": "bob"}).build()
    client = FakeXClient(pages=[page], gql=gql)

    result = await collect(client, cursors={"notifications": "0", "posts:2": "0"})

    ids = [i.source_id for i in result.items]
    assert ids == ["600"]
    assert "888" not in ids
    item = next(i for i in result.items if i.source_id == "600")
    assert item.author == "bob"


# --- Codex review finding P3.1: malformed screen_name/tweet_id are never
# emitted (would build a url that no longer exact-matches the emailer's
# anchor-provenance allowlist, silently degrading the deep link). ---


def test_is_valid_screen_name_boundaries():
    assert x_module._is_valid_screen_name("alice_123") is True
    assert x_module._is_valid_screen_name("a" * 15) is True
    assert x_module._is_valid_screen_name("a" * 16) is False
    assert x_module._is_valid_screen_name("") is False
    assert x_module._is_valid_screen_name("bad!name") is False
    assert x_module._is_valid_screen_name("bad name") is False
    assert x_module._is_valid_screen_name(None) is False


def test_is_valid_tweet_id_boundaries():
    assert x_module._is_valid_tweet_id("123456789") is True
    assert x_module._is_valid_tweet_id("0") is True
    assert x_module._is_valid_tweet_id("") is False
    assert x_module._is_valid_tweet_id("+123") is False
    assert x_module._is_valid_tweet_id("-123") is False
    assert x_module._is_valid_tweet_id("12a") is False
    assert x_module._is_valid_tweet_id(None) is False


async def test_notification_invalid_screen_name_format_skipped_with_warning(caplog):
    page = _page().add("n1", 100, tweet_id="100", text="hi", screen_name="bad name!").build()
    client = FakeXClient(pages=[page])

    with caplog.at_level("WARNING", logger="digest.collectors.x"):
        result = await collect(client, cursors={"notifications": "0"})

    assert result.items == []
    assert any("malformed screen_name/tweet_id" in r.message for r in caplog.records)


async def test_notification_invalid_tweet_id_format_skipped_with_warning(caplog):
    page = _page().add("n1", 100, tweet_id="abc123", text="hi", screen_name="alice").build()
    client = FakeXClient(pages=[page])

    with caplog.at_level("WARNING", logger="digest.collectors.x"):
        result = await collect(client, cursors={"notifications": "0"})

    assert result.items == []
    assert any("malformed screen_name/tweet_id" in r.message for r in caplog.records)


async def test_post_invalid_screen_name_format_skipped_with_warning(monkeypatch, caplog):
    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page = _page().add_bell("n1", 500, from_user_ids=["1"], screen_names={"1": "bad!name"}).build()
    gql = FakeGqlClient({"1": _user_tweets_response([_legacy_tweet("900001", "hi")])})
    client = FakeXClient(pages=[page], gql=gql)

    with caplog.at_level("WARNING", logger="digest.collectors.x"):
        result = await collect(client, cursors={"notifications": "0", "posts:1": "0"})

    assert result.items == []
    assert any("malformed screen_name/tweet_id" in r.message for r in caplog.records)


async def test_post_invalid_tweet_id_format_skipped_with_warning(monkeypatch, caplog):
    """`int("+900001")` succeeds (so the Snowflake decode in
    `fetch_user_posts` doesn't filter this id out before it ever reaches
    P3.1), but `+900001` is NOT digits-only -- exactly the defense-in-depth
    gap this check closes.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page = _page().add_bell("n1", 500, from_user_ids=["1"], screen_names={"1": "alice"}).build()
    gql = FakeGqlClient({"1": _user_tweets_response([_legacy_tweet("+900001", "hi")])})
    client = FakeXClient(pages=[page], gql=gql)

    with caplog.at_level("WARNING", logger="digest.collectors.x"):
        result = await collect(client, cursors={"notifications": "0", "posts:1": "0"})

    assert result.items == []
    assert any("malformed screen_name/tweet_id" in r.message for r in caplog.records)


# --- Two-run permanence tests (Codex review): each of these proves a
# scenario where the OLD shared-notifications-cursor design for phase 2
# would have permanently lost content, and the new per-account cursor design
# instead makes it retryable/collectible on a follow-up run. ---


async def test_account_truncated_at_page_cap_then_followup_collects_only_newer(monkeypatch, caplog):
    """Run 1: the account posted at least `_POSTS_PER_USER` times since its
    (old) cursor -- only the newest page is fetched, so the oldest of the
    fetched posts is still newer than the cursor -- triggering the
    truncation-honesty warning. All `_POSTS_PER_USER` fetched posts are
    still emitted (the fetch itself succeeded) and the cursor advances to
    the newest fetched. Run 2, fed that advanced cursor: only genuinely
    newer posts appear, with no re-emit and no error -- proving the
    truncated older content is deliberately abandoned exactly once (not
    repeatedly re-attempted), and new content past the truncation point is
    never lost.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    base_ts = 1_700_000_000_000
    old_cursor = base_ts - 10_000_000

    run1_posts = [
        _legacy_tweet(_id_for_timestamp(base_ts + i), f"post {i}")
        for i in range(x_module._POSTS_PER_USER)
    ]
    newest_run1_ts = base_ts + x_module._POSTS_PER_USER - 1

    page1 = _page().add_bell("n1", 500, from_user_ids=["1"], screen_names={"1": "alice"}).build()
    gql1 = FakeGqlClient({"1": _user_tweets_response(run1_posts)})
    client1 = FakeXClient(pages=[page1], gql=gql1)

    with caplog.at_level("WARNING", logger="digest.collectors.x"):
        run1_result = await collect(
            client1, cursors={"notifications": "0", "posts:1": str(old_cursor)}
        )

    assert len(run1_result.items) == x_module._POSTS_PER_USER
    assert run1_result.failed is False
    assert any(
        "posted more than the" in r.message and "page cap" in r.message for r in caplog.records
    )
    assert run1_result.cursor_updates[("x", "posts:1")] == str(newest_run1_ts)

    older_id = _id_for_timestamp(newest_run1_ts - 5)
    newer_id = _id_for_timestamp(newest_run1_ts + 5)
    page2 = _page().add_bell("n2", 600, from_user_ids=["1"], screen_names={"1": "alice"}).build()
    gql2 = FakeGqlClient(
        {
            "1": _user_tweets_response(
                [_legacy_tweet(older_id, "already-seen"), _legacy_tweet(newer_id, "genuinely new")]
            )
        }
    )
    client2 = FakeXClient(pages=[page2], gql=gql2)

    run2_result = await collect(
        client2,
        cursors={
            "notifications": "0",
            "posts:1": run1_result.cursor_updates[("x", "posts:1")],
        },
    )

    assert [i.source_id for i in run2_result.items] == [newer_id]
    assert run2_result.failed is False


async def test_per_account_fetch_failure_then_followup_retries_successfully(monkeypatch):
    """Run 1: account 1's post fetch fails (exception) while account 2's
    succeeds -- account 2's item/cursor are unaffected, account 1's cursor
    is left untouched. Run 2, fed the SAME cursors (account 1's never
    advanced): account 1's fetch now succeeds and its window is collected --
    proving the failed window was retryable, not permanently lost.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    page1 = (
        _page()
        .add_bell("n1", 500, from_user_ids=["1", "2"], screen_names={"1": "alice", "2": "bob"})
        .build()
    )
    gql1 = FakeGqlClient(
        {
            "1": RuntimeError("boom"),
            "2": _user_tweets_response([_legacy_tweet("900002", "bob's post")]),
        }
    )
    client1 = FakeXClient(pages=[page1], gql=gql1)

    run1_cursors = {"notifications": "0", "posts:1": "0", "posts:2": "0"}
    run1_result = await collect(client1, cursors=run1_cursors)

    assert run1_result.failed is False
    assert [i.source_id for i in run1_result.items] == ["900002"]
    assert ("x", "posts:1") not in run1_result.cursor_updates
    assert ("x", "posts:2") in run1_result.cursor_updates

    page2 = _page().add_bell("n2", 600, from_user_ids=["1"], screen_names={"1": "alice"}).build()
    gql2 = FakeGqlClient({"1": _user_tweets_response([_legacy_tweet("900001", "alice's post")])})
    client2 = FakeXClient(pages=[page2], gql=gql2)

    run2_result = await collect(client2, cursors={"notifications": "0", "posts:1": "0"})

    assert run2_result.failed is False
    assert [i.source_id for i in run2_result.items] == ["900001"]
    assert ("x", "posts:1") in run2_result.cursor_updates


async def test_account_beyond_user_cap_uncollected_in_run1_collected_in_run2(monkeypatch):
    """Run 1: an account named beyond `_MAX_POST_FETCH_USERS` is never
    fetched at all -- its cursor is absent from `cursor_updates` entirely
    (not just unchanged). Run 2: that same account, now within the cap
    (fewer competing targets), IS fetched and its cursor gets seeded --
    proving it was skipped, not lost.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    user_ids = [str(i) for i in range(x_module._MAX_POST_FETCH_USERS + 1)]
    overflow_user = user_ids[-1]

    responses = {uid: _user_tweets_response([]) for uid in user_ids[:-1]}
    page1 = _page().add_bell("n1", 500, from_user_ids=user_ids).build()
    gql1 = FakeGqlClient(responses)
    client1 = FakeXClient(pages=[page1], gql=gql1)

    run1_result = await collect(client1, cursors={"notifications": "0"})

    assert ("x", f"posts:{overflow_user}") not in run1_result.cursor_updates
    assert overflow_user not in gql1.calls

    page2 = (
        _page()
        .add_bell("n2", 600, from_user_ids=[overflow_user], screen_names={overflow_user: "zed"})
        .build()
    )
    gql2 = FakeGqlClient(
        {overflow_user: _user_tweets_response([_legacy_tweet("900099", "zed's post")])}
    )
    client2 = FakeXClient(pages=[page2], gql=gql2)

    run2_result = await collect(client2, cursors={"notifications": "0"})

    assert overflow_user in gql2.calls
    assert run2_result.items == []  # first sight of this account -- no backfill
    assert ("x", f"posts:{overflow_user}") in run2_result.cursor_updates


async def test_first_sight_of_account_seeds_cursor_then_followup_emits_only_newer(monkeypatch):
    """Run 1: this ACCOUNT's first sight (no `posts:{id}` cursor row yet,
    even though the notifications cursor already exists, i.e. this is NOT
    the module's overall first run) seeds its cursor from the newest
    fetched post and emits NO items. Run 2, fed that seed: a post exactly at
    the seed's timestamp is excluded (already visible at seed time), a post
    one ms later is included.
    """

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(x_module.asyncio, "sleep", fake_sleep)

    seed_ts = 1_700_000_000_000
    seeded_at_id = _id_for_timestamp(seed_ts)

    page1 = _page().add_bell("n1", 500, from_user_ids=["1"], screen_names={"1": "alice"}).build()
    gql1 = FakeGqlClient(
        {"1": _user_tweets_response([_legacy_tweet(seeded_at_id, "seed-time post")])}
    )
    client1 = FakeXClient(pages=[page1], gql=gql1)

    run1_result = await collect(client1, cursors={"notifications": "0"})

    assert run1_result.items == []
    assert run1_result.cursor_updates.get(("x", "posts:1")) == str(seed_ts)

    older_id = _id_for_timestamp(seed_ts - 1000)
    newer_id = _id_for_timestamp(seed_ts + 1000)
    page2 = _page().add_bell("n2", 600, from_user_ids=["1"], screen_names={"1": "alice"}).build()
    gql2 = FakeGqlClient(
        {
            "1": _user_tweets_response(
                [_legacy_tweet(older_id, "already-seen"), _legacy_tweet(newer_id, "genuinely new")]
            )
        }
    )
    client2 = FakeXClient(pages=[page2], gql=gql2)

    run2_result = await collect(
        client2,
        cursors={
            "notifications": "0",
            "posts:1": run1_result.cursor_updates[("x", "posts:1")],
        },
    )

    assert [i.source_id for i in run2_result.items] == [newer_id]
