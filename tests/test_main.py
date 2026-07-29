from pathlib import Path

import pytest

import digest.main as main_mod
from digest.collectors.telegram import CollectResult
from digest.config import Config
from digest.main import _client_ready, _deliver
from digest.state import (
    Item,
    commit_new_items,
    connect,
    count_unsummarized_items,
    create_digest,
    get_pending_digest,
    get_unsummarized_items,
    init_db,
)


class FakeReadyClient:
    """Minimal fake honoring the connect/is_user_authorized surface _client_ready uses."""

    def __init__(
        self,
        connect_error: Exception | None = None,
        authorized: bool = True,
    ):
        self.connect_error = connect_error
        self.authorized = authorized
        self.connect_calls = 0
        self.is_user_authorized_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error

    async def is_user_authorized(self) -> bool:
        self.is_user_authorized_calls += 1
        return self.authorized


async def test_client_ready_connect_ok_and_authorized_returns_true():
    client = FakeReadyClient(authorized=True)

    assert await _client_ready(client) is True
    assert client.connect_calls == 1
    assert client.is_user_authorized_calls == 1


async def test_client_ready_connect_ok_but_not_authorized_returns_false():
    client = FakeReadyClient(authorized=False)

    assert await _client_ready(client) is False
    assert client.connect_calls == 1
    assert client.is_user_authorized_calls == 1


async def test_client_ready_connect_raises_returns_false():
    client = FakeReadyClient(connect_error=ConnectionError("network down"))

    assert await _client_ready(client) is False
    assert client.connect_calls == 1
    assert client.is_user_authorized_calls == 0


# --- _deliver (Phase 2 post-collection pipeline) ---


def _cfg() -> Config:
    return Config(
        tg_api_id=1,
        tg_api_hash="hash",
        tg_session="session",
        tg_chat_allowlist=(123,),
        smtp_host="smtp.mail.me.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="app-password",
        digest_from="digest@4rgus.com",
        digest_to="me@toomhorvath.com",
        anthropic_model="claude-opus-5",
        archive_dir="./archive",
        claude_timeout_seconds=300,
    )


def _item(source_id: str = "1", fetched_at: str = "2026-07-29T10:00:00+00:00") -> Item:
    return Item(
        source="telegram",
        source_id=source_id,
        chat_id="123",
        author="alice",
        text="hello",
        url=f"https://t.me/c/123/{source_id}",
        fetched_at=fetched_at,
    )


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(str(tmp_path / "state.db"))
    init_db(c)
    yield c
    c.close()


def test_deliver_retries_pending_digest_and_never_calls_summarize(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    digest_id = create_digest(conn, "## Needs attention\n...", get_unsummarized_items(conn))

    def boom_summarize(*args, **kwargs):
        raise AssertionError("summarize must not be called when a digest is pending resend")

    sent = {}

    def fake_send_digest(host, port, user, password, from_, to, subject, body_md):
        sent["body_md"] = body_md
        sent["subject"] = subject

    archived = {}

    def fake_archive(body_md, archive_dir, digest_id):
        archived["digest_id"] = digest_id

    monkeypatch.setattr(main_mod, "summarize", boom_summarize)
    monkeypatch.setattr(main_mod, "send_digest", fake_send_digest)
    monkeypatch.setattr(main_mod, "archive", fake_archive)

    ok = _deliver(conn, _cfg(), CollectResult())

    assert ok is True
    assert sent["body_md"] == "## Needs attention\n..."
    assert archived["digest_id"] == digest_id
    assert get_pending_digest(conn) is None  # marked sent


def test_deliver_zero_unsummarized_items_sends_nothing(conn, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("must not be called when there is nothing to deliver")

    monkeypatch.setattr(main_mod, "summarize", boom)
    monkeypatch.setattr(main_mod, "send_digest", boom)
    monkeypatch.setattr(main_mod, "archive", boom)

    ok = _deliver(conn, _cfg(), CollectResult())

    assert ok is True
    assert get_pending_digest(conn) is None


def test_deliver_smtp_failure_leaves_digest_row_unsent(conn, monkeypatch):
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})

    monkeypatch.setattr(main_mod, "summarize", lambda *a, **k: "## Needs attention\n...")

    def failing_send(*args, **kwargs):
        raise OSError("smtp connection refused")

    archived = {"called": False}
    monkeypatch.setattr(main_mod, "send_digest", failing_send)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: archived.__setitem__("called", True))

    ok = _deliver(conn, _cfg(), CollectResult())

    assert ok is False
    pending = get_pending_digest(conn)
    assert pending is not None
    digest_id, body_md = pending
    assert body_md == "## Needs attention\n..."
    assert archived["called"] is False  # never reached on send failure


def test_deliver_success_path_creates_digest_sends_marks_sent_and_archives(conn, monkeypatch):
    commit_new_items(conn, [_item("1"), _item("2")], {("telegram", "123"): "2"})

    monkeypatch.setattr(main_mod, "summarize", lambda *a, **k: "## Needs attention\n...")

    sent = {}
    monkeypatch.setattr(
        main_mod,
        "send_digest",
        lambda host, port, user, pw, from_, to, subject, body_md: sent.update(
            subject=subject, body_md=body_md
        ),
    )
    archived = {}
    monkeypatch.setattr(
        main_mod, "archive", lambda body_md, archive_dir, digest_id: archived.update(id=digest_id)
    )

    ok = _deliver(conn, _cfg(), CollectResult())

    assert ok is True
    assert sent["body_md"] == "## Needs attention\n..."
    assert "2 items" in sent["subject"]
    assert get_pending_digest(conn) is None
    assert archived["id"] == 1


def test_deliver_pending_digest_and_new_items_sends_both_in_same_run(conn, monkeypatch):
    # A pending digest from a previous run plus freshly collected items that
    # are still unsummarized: the pending resend must not swallow this run's
    # own items -- both must go out, as two separate sends.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    pending_digest_id = create_digest(
        conn, "## Needs attention\n...pending...", get_unsummarized_items(conn)
    )

    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})

    summarize_calls = []

    def fake_summarize(items, failed_sources, model, timeout_seconds):
        summarize_calls.append((items, failed_sources))
        return "## Needs attention\n...new..."

    sends = []

    def fake_send_digest(host, port, user, password, from_, to, subject, body_md):
        sends.append(body_md)

    archived = []
    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "send_digest", fake_send_digest)
    monkeypatch.setattr(
        main_mod, "archive", lambda body_md, archive_dir, digest_id: archived.append(digest_id)
    )

    ok = _deliver(conn, _cfg(), CollectResult())

    assert ok is True
    assert len(sends) == 2
    assert sends[0] == "## Needs attention\n...pending..."
    assert sends[1] == "## Needs attention\n...new..."
    assert len(summarize_calls) == 1  # only for the new items, never the pending digest
    assert get_pending_digest(conn) is None
    assert pending_digest_id in archived


def test_deliver_pending_digest_sent_then_current_collection_failed_passes_failed_sources(
    conn, monkeypatch
):
    # The pending resend succeeds, but THIS run's own collection failed --
    # the new digest created for this run's items must carry this run's own
    # failed_sources, not an empty list.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    create_digest(conn, "## Needs attention\n...pending...", get_unsummarized_items(conn))

    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})

    summarize_calls = []

    def fake_summarize(items, failed_sources, model, timeout_seconds):
        summarize_calls.append(failed_sources)
        return "## Needs attention\n...new..."

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), CollectResult(failed=True))

    assert ok is True
    assert summarize_calls == [["telegram"]]


def test_deliver_pending_digest_send_fails_no_summarize_and_returns_false(conn, monkeypatch):
    # If the pending resend itself fails, we must not attempt to summarize
    # or send a second email on what's evidently a broken SMTP path.
    commit_new_items(conn, [_item("1")], {("telegram", "123"): "1"})
    create_digest(conn, "## Needs attention\n...pending...", get_unsummarized_items(conn))

    commit_new_items(conn, [_item("2")], {("telegram", "123"): "2"})

    def boom_summarize(*args, **kwargs):
        raise AssertionError("summarize must not be called when the pending resend fails")

    def failing_send(*args, **kwargs):
        raise OSError("smtp connection refused")

    monkeypatch.setattr(main_mod, "summarize", boom_summarize)
    monkeypatch.setattr(main_mod, "send_digest", failing_send)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), CollectResult())

    assert ok is False
    pending = get_pending_digest(conn)
    assert pending is not None  # still unsent, left for the next run's retry


def test_deliver_bounds_batch_to_max_items_per_digest_leaving_remainder_unsummarized(
    conn, monkeypatch
):
    # P1 finding: an unbounded backlog can exceed the model context and wedge
    # the pipeline forever. With more unsummarized items than the per-run
    # cap, _deliver must hand summarize exactly the cap-sized OLDEST batch,
    # send it successfully, and leave the remainder unsummarized for the
    # next run -- never looping summarize within a single run.
    monkeypatch.setattr(main_mod, "_MAX_ITEMS_PER_DIGEST", 2)

    commit_new_items(
        conn,
        [
            _item("1", fetched_at="2026-07-29T10:00:00+00:00"),
            _item("2", fetched_at="2026-07-29T11:00:00+00:00"),
            _item("3", fetched_at="2026-07-29T12:00:00+00:00"),
        ],
        {("telegram", "123"): "3"},
    )

    summarize_calls = []

    def fake_summarize(items, failed_sources, model, timeout_seconds):
        summarize_calls.append(items)
        return "## Needs attention\n...batch..."

    monkeypatch.setattr(main_mod, "summarize", fake_summarize)
    monkeypatch.setattr(main_mod, "send_digest", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "archive", lambda *a, **k: None)

    ok = _deliver(conn, _cfg(), CollectResult())

    assert ok is True
    assert len(summarize_calls) == 1  # exactly one Opus call this run
    assert [i.source_id for i in summarize_calls[0]] == ["1", "2"]  # oldest batch, size-bounded
    assert count_unsummarized_items(conn) == 1  # item "3" left for the next run
    assert [i.source_id for i in get_unsummarized_items(conn)] == ["3"]
