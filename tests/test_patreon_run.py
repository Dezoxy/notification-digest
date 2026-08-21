"""Tests for run_patreon: orchestration, seeding, and per-post isolation.

Drives a real SQLite state database (tmp_path) so the dedup and seeding
invariants are exercised against the actual schema rather than a mock. The
collector's HTTP edge and the Claude CLI are the two external boundaries,
and both are monkeypatched -- per CLAUDE.md, tests never reach the network.
"""

from __future__ import annotations

import pytest

from digest import main as digest_main
from digest.collectors.patreon import PatreonCollectResult
from digest.config import Config
from digest.state import Item, connect, get_unsummarized_items, init_db, known_source_ids


def make_item(source_id: str, embed_url: str | None = None) -> Item:
    return Item(
        source="patreon",
        source_id=source_id,
        chat_id=None,
        chat_title="Patreon",
        author=None,
        text=f"Cím {source_id}\n\nTörzsszöveg {source_id}",
        url=f"https://www.patreon.com/kriptovadasz/posts/x-{source_id}",
        embed_url=embed_url,
        fetched_at="2026-08-21T16:00:00+00:00",
    )


SUMMARY = "## Cím\n\n**TL;DR:** Összefoglaló.\n\n- Pont egy.\n"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A REAL Config, not a hand-rolled stand-in.

    A stub class was used here first and it hid a live AttributeError: the
    stub declared `claude_model`, run_patreon read `cfg.claude_model`, and
    both were wrong -- the real field is `anthropic_model`. The fake agreed
    with the bug, so 1085 tests passed and the first real run crashed. A
    dataclass built from env cannot drift from the code that reads it.
    """
    for key, value in {
        "TG_API_ID": "12345",
        "TG_API_HASH": "hash",
        "TG_SESSION": "session",
        "TG_CHAT_ALLOWLIST": "123",
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "587",
        "SMTP_USER": "user@example.com",
        "SMTP_PASSWORD": "pw",
        "DIGEST_FROM": "d@example.com",
        "DIGEST_TO": "me@example.com",
        "STATE_DB_PATH": str(tmp_path / "state.db"),
        "PATREON_CAMPAIGN_ID": "7095842",
        "PATREON_SESSION_COOKIE": "cookie",
        "TELEGRAM_PATREON_THREAD_ID": "317",
    }.items():
        monkeypatch.setenv(key, value)
    return Config.from_env()


@pytest.fixture
def wire(monkeypatch):
    """Patch the collector and the summarizer; capture what would be delivered."""

    def _wire(result, summarize=None, deliver=None):
        monkeypatch.setattr(digest_main.patreon_collector, "collect", lambda *a, **k: result)
        monkeypatch.setattr(digest_main, "summarize_post", summarize or (lambda *a, **k: SUMMARY))
        sent = []
        monkeypatch.setattr(
            digest_main,
            "deliver_channels",
            deliver or (lambda conn, c, did, body, n, ts, done, st, **k: sent.append(did) or True),
        )
        return sent

    return _wire


class TestDisabledAndFailure:
    def test_unconfigured_collector_is_success_not_failure(self, tmp_path, monkeypatch):
        # A timer firing against a deliberately unconfigured collector must
        # not alert.
        for key, value in {
            "TG_API_ID": "12345",
            "TG_API_HASH": "h",
            "TG_SESSION": "s",
            "TG_CHAT_ALLOWLIST": "1",
            "SMTP_HOST": "s",
            "SMTP_PORT": "587",
            "SMTP_USER": "u@e.com",
            "SMTP_PASSWORD": "p",
            "DIGEST_FROM": "d@e.com",
            "DIGEST_TO": "m@e.com",
            "STATE_DB_PATH": str(tmp_path / "s.db"),
        }.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("PATREON_CAMPAIGN_ID", raising=False)
        monkeypatch.delenv("PATREON_SESSION_COOKIE", raising=False)
        assert digest_main.run_patreon(Config.from_env()) is True

    def test_collector_failure_fails_the_run(self, cfg, wire):
        wire(PatreonCollectResult(failed=True))
        assert digest_main.run_patreon(cfg) is False

    def test_no_new_posts_is_success(self, cfg, wire):
        wire(PatreonCollectResult(items=[]))
        assert digest_main.run_patreon(cfg) is True


class TestSeeding:
    def test_seeded_posts_are_recorded_but_never_delivered(self, cfg, wire):
        seeded = [make_item(str(i)) for i in range(5, 20)]
        sent = wire(PatreonCollectResult(items=[make_item("1")], seeded=seeded))

        assert digest_main.run_patreon(cfg) is True

        conn = connect(cfg.state_db_path)
        # Every seeded post counts as known, so the next run skips them...
        known = known_source_ids(conn, "patreon", [i.source_id for i in seeded])
        assert known == {i.source_id for i in seeded}
        # ...and exactly one digest was delivered: the real post, not them.
        assert len(sent) == 1
        conn.close()

    def test_the_seed_digest_is_marked_sent_on_every_channel(self, cfg, wire):
        wire(PatreonCollectResult(items=[], seeded=[make_item("9")]))
        digest_main.run_patreon(cfg)

        conn = connect(cfg.state_db_path)
        row = conn.execute(
            "SELECT email_sent, site_published, telegram_sent, kind FROM digests"
        ).fetchone()
        # get_pending_digests must never pick this up -- nothing may fire.
        assert row[0] == 1 and row[1] == 1 and row[2] == 1
        assert row[3] == "patreon"
        conn.close()

    def test_seeded_items_never_reach_the_window_digest(self, cfg, wire):
        # The bug this guards: a bare `WHERE digest_id IS NULL` sweep would
        # have delivered every Patreon post a second time inside the
        # 06:00/12:00 briefing, in a different topic.
        wire(PatreonCollectResult(items=[make_item("1")], seeded=[make_item("9")]))
        digest_main.run_patreon(cfg)

        conn = connect(cfg.state_db_path)
        assert get_unsummarized_items(conn) == []
        conn.close()


class TestPerPostIsolation:
    def test_each_post_gets_its_own_digest(self, cfg, wire):
        sent = wire(PatreonCollectResult(items=[make_item(str(i)) for i in range(3)]))
        assert digest_main.run_patreon(cfg) is True
        assert len(sent) == 3

    def test_posts_are_delivered_oldest_first(self, cfg, wire, monkeypatch):
        # The collector returns newest-first; a burst should still read in
        # the order the author wrote it.
        seen = []
        monkeypatch.setattr(
            digest_main.patreon_collector,
            "collect",
            lambda *a, **k: PatreonCollectResult(items=[make_item("3"), make_item("2")]),
        )
        monkeypatch.setattr(
            digest_main, "summarize_post", lambda item, *a: seen.append(item.source_id) or SUMMARY
        )
        monkeypatch.setattr(digest_main, "deliver_channels", lambda *a, **k: True)
        digest_main.run_patreon(cfg)
        assert seen == ["2", "3"]

    def test_one_failing_post_does_not_stop_the_others(self, cfg, wire):
        from digest.summarize import SummarizeError

        def flaky(item, *a, **k):
            if item.source_id == "2":
                raise SummarizeError("model refused")
            return SUMMARY

        sent = wire(
            PatreonCollectResult(items=[make_item("1"), make_item("2"), make_item("3")]),
            summarize=flaky,
        )
        # Run reports failure so the OnFailure alert fires...
        assert digest_main.run_patreon(cfg) is False
        # ...but the two healthy posts still went out.
        assert len(sent) == 2

    def test_a_failed_post_is_retried_next_run(self, cfg, wire):
        from digest.summarize import SummarizeError

        def always_fails(item, *a, **k):
            raise SummarizeError("model refused")

        wire(PatreonCollectResult(items=[make_item("1")]), summarize=always_fails)
        digest_main.run_patreon(cfg)

        conn = connect(cfg.state_db_path)
        init_db(conn)
        # No digest row was created, so the item keeps digest_id NULL and
        # known_source_ids still reports it unknown -- self-healing.
        assert known_source_ids(conn, "patreon", ["1"]) == set()
        conn.close()


class TestChannelSuppression:
    def test_email_and_site_are_always_hidden(self, cfg, monkeypatch):
        # These summaries are of PAYWALLED posts. The site channel is a
        # public news site; publishing there would redistribute purchased
        # content. This must not be one env var away from happening.
        captured = {}
        monkeypatch.setattr(
            digest_main.patreon_collector,
            "collect",
            lambda *a, **k: PatreonCollectResult(items=[make_item("1")]),
        )
        monkeypatch.setattr(digest_main, "summarize_post", lambda *a, **k: SUMMARY)
        monkeypatch.setattr(
            digest_main,
            "deliver_channels",
            lambda *a, **k: captured.update(k) or True,
        )
        digest_main.run_patreon(cfg)

        assert captured["hidden"] == frozenset({"email", "site"})
        assert captured["kind"] == "patreon"

    def test_suppression_survives_email_being_enabled_later(self, cfg, wire):
        # deliver_channels only marks a HIDDEN channel's flag when that
        # channel is enabled, and email is disabled on the real VM. Left at
        # 0, every Patreon digest would become pending the moment
        # EMAIL_ENABLED was flipped on -- mailing out summaries of paywalled
        # posts in one burst. The flags must be written regardless.
        wire(PatreonCollectResult(items=[make_item("1")]))
        digest_main.run_patreon(cfg)

        conn = connect(cfg.state_db_path)
        rows = conn.execute(
            "SELECT email_sent, site_published FROM digests WHERE kind = 'patreon'"
        ).fetchall()
        assert rows and all(email == 1 and site == 1 for email, site in rows)
        conn.close()
