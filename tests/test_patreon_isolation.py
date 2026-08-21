"""Patreon must not perturb the window / daily / weekly cycles.

Patreon is the first source with its OWN delivery pipeline, so every shared
query is a place its rows could leak into a cycle that predates it. These
tests pin each boundary against the real schema rather than trusting that
the current filters happen to be right.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from digest.collectors.patreon import _MAX_POST_AGE_DAYS, _within_age_cutoff
from digest.state import (
    _ITEMS_PRUNE_DAYS,
    Item,
    commit_new_items,
    connect,
    create_digest,
    get_pending_digests,
    get_recent_digests,
    get_unsummarized_items,
    get_window_digests_since,
    init_db,
    mark_digest_sent,
    mark_digest_site_published,
    mark_digest_telegram_sent,
)


def item(source: str, source_id: str) -> Item:
    return Item(
        source=source,
        source_id=source_id,
        chat_id=None,
        chat_title=source,
        author=None,
        text=f"szöveg {source_id}",
        url=f"https://example.com/{source}/{source_id}",
        fetched_at=datetime.now(UTC).isoformat(),
    )


def db(tmp_path):
    conn = connect(str(tmp_path / "iso.db"))
    init_db(conn)
    return conn


class TestWindowDigestIsolation:
    def test_patreon_items_are_invisible_to_the_window_sweep(self, tmp_path):
        # The 06:00/12:00 briefing selects unsummarized items with no source
        # filter of its own. A Patreon post appearing there would be
        # delivered twice, in two different topics.
        conn = db(tmp_path)
        commit_new_items(conn, [item("patreon", "1"), item("news", "2")], {})

        swept = get_unsummarized_items(conn)

        assert [i.source for i in swept] == ["news"]
        conn.close()

    def test_a_patreon_item_awaiting_retry_still_stays_out(self, tmp_path):
        # A post whose summarization failed keeps digest_id NULL until the
        # next hourly run retries it -- the window run must not adopt it in
        # the meantime.
        conn = db(tmp_path)
        commit_new_items(conn, [item("patreon", "1")], {})
        assert get_unsummarized_items(conn) == []
        conn.close()


class TestDailyAndWeeklyIsolation:
    def test_patreon_digests_never_feed_the_daily_brief(self, tmp_path):
        # run_daily synthesizes a day of WINDOW digests. A patreon row
        # joining them would put paid-post summaries into the daily brief,
        # which goes to a different topic.
        conn = db(tmp_path)
        commit_new_items(conn, [item("patreon", "1"), item("news", "2")], {})
        create_digest(conn, "## Patreon\n\nx", [item("patreon", "1")], kind="patreon")
        create_digest(conn, "## Window\n\ny", [item("news", "2")], kind="window")

        since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        rows = get_window_digests_since(conn, since)

        assert len(rows) == 1
        assert "Window" in rows[0][3]
        conn.close()

    def test_patreon_digests_never_reach_the_continuity_feed(self, tmp_path):
        # get_recent_digests is the "recently covered" context handed to the
        # window summarizer. Patreon rows there would leak paid content into
        # the prompt for a briefing that is published publicly.
        conn = db(tmp_path)
        commit_new_items(conn, [item("patreon", "1")], {})
        create_digest(conn, "## Patreon\n\nx", [item("patreon", "1")], kind="patreon")

        since = (datetime.now(UTC) - timedelta(days=7)).isoformat()

        assert get_recent_digests(conn, since) == []
        conn.close()


class TestPendingRetryIsolation:
    def test_a_fully_marked_patreon_digest_is_never_pending(self, tmp_path):
        # deliver_pending runs inside the WINDOW cycle. A patreon digest it
        # picked up would be re-sent during a window run.
        conn = db(tmp_path)
        commit_new_items(conn, [item("patreon", "1")], {})
        digest_id = create_digest(conn, "## P\n\nx", [item("patreon", "1")], kind="patreon")
        mark_digest_sent(conn, digest_id)
        mark_digest_site_published(conn, digest_id)
        mark_digest_telegram_sent(conn, digest_id)

        assert get_pending_digests(conn, True, True, True) == []
        conn.close()

    def test_a_telegram_failure_is_retried_on_telegram_only(self, tmp_path):
        # run_patreon writes the email/site flags unconditionally, so a
        # patreon digest whose Telegram send failed comes back pending for
        # Telegram alone -- deliver_pending cannot mail or publish it.
        conn = db(tmp_path)
        commit_new_items(conn, [item("patreon", "1")], {})
        digest_id = create_digest(conn, "## P\n\nx", [item("patreon", "1")], kind="patreon")
        mark_digest_sent(conn, digest_id)
        mark_digest_site_published(conn, digest_id)

        pending = get_pending_digests(conn, True, True, True)

        assert len(pending) == 1
        _, _, done, kind = pending[0]
        assert done == {"email": True, "site": True, "telegram": False}
        assert kind == "patreon"
        conn.close()


class TestPruneInteraction:
    def test_the_age_cutoff_sits_well_inside_the_item_prune_window(self):
        # Once prune_delivered_items removes an item, known_source_ids
        # forgets it -- so any post still on the API's first page at that
        # moment would be delivered a second time. The cutoff is what makes
        # that impossible rather than merely unlikely.
        assert _MAX_POST_AGE_DAYS < _ITEMS_PRUNE_DAYS / 2

    def test_a_post_older_than_the_cutoff_is_dropped(self):
        now = datetime.now(UTC)
        old = {
            "attributes": {
                "published_at": (now - timedelta(days=_MAX_POST_AGE_DAYS + 5)).isoformat()
            }
        }
        assert _within_age_cutoff(old, now) is False

    def test_a_recent_post_is_kept(self):
        now = datetime.now(UTC)
        recent = {"attributes": {"published_at": (now - timedelta(days=2)).isoformat()}}
        assert _within_age_cutoff(recent, now) is True

    def test_an_unparseable_timestamp_is_kept_not_dropped(self):
        # Dropping over an odd timestamp loses real content; keeping costs
        # at most one duplicate.
        now = datetime.now(UTC)
        assert _within_age_cutoff({"attributes": {"published_at": "tegnap"}}, now) is True
        assert _within_age_cutoff({"attributes": {}}, now) is True
