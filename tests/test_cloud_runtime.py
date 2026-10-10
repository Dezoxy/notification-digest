"""Offline failure injection for cloud execution and publication boundaries."""

import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from digest import cloud_context, deliver
from digest.cloud_context import CloudHooks, CloudSafetyError, cloud_execution
from digest.cloud_run import LeaseWatchdog, _dispatch, run_job, scheduled_slot
from digest.config import CloudConfig, Config, ConfigError
from digest.publish import TelegramSendError
from digest.state import (
    Item,
    cloud_slot_completed,
    commit_new_items,
    complete_cloud_slot,
    connect,
    create_digest,
    get_cursors,
    get_delivery_intent,
    init_db,
    reconcile_delivery,
    set_delivery_intent,
)


@pytest.fixture
def conn():
    database = connect(":memory:")
    init_db(database)
    yield database
    database.close()


def _config() -> Config:
    return Config(
        tg_api_id=1,
        tg_api_hash="mock",
        tg_session="mock",
        tg_chat_allowlist=(1,),
        smtp_host="mock",
        smtp_port=587,
        smtp_user="mock",
        smtp_password="mock",
        digest_from="mock@example.com",
        digest_to="mock@example.com",
        telegram_notify_bot_token="mock",
        telegram_notify_chat_id=-1,
    )


def _send(conn, digest_id):
    return deliver._deliver_telegram(
        conn,
        _config(),
        digest_id,
        "body",
        datetime.now(UTC).isoformat(),
        deliver.TelegramRunState(),
    )


@pytest.mark.parametrize(
    "job,stamp,expected",
    [
        ("daily", "2026-07-05T18:35:00+00:00", "2026-07-05T18:30:00+00:00"),
        ("daily", "2026-01-04T19:40:00+00:00", "2026-01-04T19:30:00+00:00"),
        ("daily", "2026-01-04T18:30:00+00:00", None),
        ("daily", "2026-07-05T19:30:00+00:00", None),
        ("weekly", "2026-07-05T19:50:00+00:00", "2026-07-05T19:45:00+00:00"),
        ("weekly", "2026-01-04T20:55:00+00:00", "2026-01-04T20:45:00+00:00"),
        ("weekly", "2026-07-06T19:45:00+00:00", None),
        ("patreon", "2026-07-06T01:00:00+00:00", "2026-07-06T00:50:00+00:00"),
        ("overnight", "2026-07-06T00:05:00+00:00", "2026-07-06T00:00:00+00:00"),
    ],
)
def test_slot_timezone_and_delayed_start(job, stamp, expected):
    assert scheduled_slot(job, datetime.fromisoformat(stamp)) == expected


def test_late_due_slot_is_failure_and_explicit_catchup_remains_possible():
    now = datetime(2026, 7, 5, 18, 41, tzinfo=UTC)
    with pytest.raises(ValueError, match="grace"):
        scheduled_slot("daily", now)
    slot = datetime(2026, 7, 5, 18, 30, tzinfo=UTC)
    assert scheduled_slot("daily", now, catch_up_slot=slot) == slot.isoformat()


@pytest.mark.parametrize(
    "job,slot",
    [
        ("daily", datetime(2026, 1, 4, 18, 30, tzinfo=UTC)),
        ("relay", datetime(2026, 1, 4, 18, 41, tzinfo=UTC)),
        ("weekly", datetime(2026, 1, 5, 20, 45, tzinfo=UTC)),
    ],
)
def test_manual_catchup_cannot_bypass_schedule_validation(job, slot):
    with pytest.raises(ValueError, match="schedule"):
        scheduled_slot(job, slot + timedelta(days=1), catch_up_slot=slot)


def test_unknown_job_rejected():
    with pytest.raises(ValueError):
        scheduled_slot("typo", datetime.now(UTC))


def test_committed_cursor_is_checkpointed_before_continuing(conn):
    snapshots = []
    with cloud_execution(
        CloudHooks(lambda: None, lambda db: snapshots.append(get_cursors(db, "x")))
    ):
        commit_new_items(conn, [], {("x", "notifications"): "12"})
    assert snapshots[-1] == {"notifications": "12"}


def test_missing_lease_blocks_commit_and_cannot_be_caught_as_soft_failure(conn):
    def lost():
        raise RuntimeError("lease lost")

    with cloud_execution(CloudHooks(lost, Mock())):
        with pytest.raises(CloudSafetyError):
            complete_cloud_slot(conn, "relay", "slot")
    conn.rollback()
    assert not cloud_slot_completed(conn, "relay", "slot")


def test_pre_send_checkpoint_failure_prevents_api_call(conn, monkeypatch):
    digest_id = create_digest(conn, "body", [])
    send = Mock()
    monkeypatch.setattr(deliver, "send_telegram_tldr", send)

    def unavailable(_):
        raise RuntimeError("storage unavailable")

    with cloud_execution(CloudHooks(lambda: None, unavailable)):
        with pytest.raises(CloudSafetyError):
            _send(conn, digest_id)
    send.assert_not_called()


def test_acknowledged_send_with_failed_checkpoint_never_blindly_resends(conn, monkeypatch):
    digest_id = create_digest(conn, "body", [])
    durable = sqlite3.connect(":memory:")
    sent = []
    monkeypatch.setattr(deliver, "send_telegram_tldr", lambda *args: sent.append(args))

    def snapshot(db):
        if sent:
            raise RuntimeError("ack checkpoint unavailable")
        db.backup(durable)

    with cloud_execution(CloudHooks(lambda: None, snapshot)):
        with pytest.raises(CloudSafetyError):
            _send(conn, digest_id)
    # The restored generation contains inflight, never the unpersisted flag.
    assert get_delivery_intent(durable, digest_id, "telegram") == "inflight"
    send = Mock()
    monkeypatch.setattr(deliver, "send_telegram_tldr", send)
    with cloud_execution(CloudHooks(lambda: None, lambda _: None)):
        assert _send(durable, digest_id) is False
    send.assert_not_called()
    assert get_delivery_intent(durable, digest_id, "telegram") == "uncertain"
    durable.close()


@pytest.mark.parametrize(
    "status,outcome", [(400, "pending"), (429, "pending"), (500, "uncertain"), (None, "uncertain")]
)
def test_rejected_send_is_retryable_but_ambiguous_send_requires_reconciliation(
    conn, monkeypatch, status, outcome
):
    digest_id = create_digest(conn, "body", [])
    monkeypatch.setattr(
        deliver,
        "send_telegram_tldr",
        Mock(
            side_effect=TelegramSendError(
                "mock",
                status=status,
            )
        ),
    )
    with cloud_execution(CloudHooks(lambda: None, lambda _: None)):
        assert _send(conn, digest_id) is False
    assert get_delivery_intent(conn, digest_id, "telegram") == outcome


def test_manual_reconciliation_atomically_confirms_flag_and_intent(conn):
    digest_id = create_digest(conn, "body", [])
    set_delivery_intent(conn, digest_id, "telegram", "uncertain")
    snapshots = []

    def snapshot(db):
        snapshots.append(
            (
                get_delivery_intent(db, digest_id, "telegram"),
                db.execute(
                    "SELECT telegram_sent FROM digests WHERE id=?",
                    (digest_id,),
                ).fetchone()[0],
            )
        )

    with cloud_execution(CloudHooks(lambda: None, snapshot)):
        reconcile_delivery(conn, digest_id, "telegram", "confirmed")
    assert snapshots == [("confirmed", 1)]


def test_watchdog_fences_before_expiry_even_if_renewal_thread_is_blocked():
    state = Mock(lease_deadline=60)
    fence = Mock()
    watcher = LeaseWatchdog(state, fence, clock=lambda: 50)
    watcher.stopped.wait = Mock(return_value=False)
    # Arrange one iteration then stop, without real sleep or signals.
    fence.side_effect = lambda: watcher.stopped.wait.configure_mock(return_value=True)
    watcher._watch()
    fence.assert_called_once()
    with pytest.raises(RuntimeError):
        watcher.guard()


def test_context_defaults_to_local_after_failure():
    with pytest.raises(CloudSafetyError):
        with cloud_execution(CloudHooks(Mock(side_effect=RuntimeError()), Mock())):
            cloud_context.guard()
    assert not cloud_context.active()


def test_failure_reporting_is_scoped_to_cloud_execution():
    outer, inner = [], []
    cloud_context.report_failure("collection")
    with cloud_execution(CloudHooks(lambda: None, Mock(), outer.append)):
        cloud_context.report_failure("collection")
        with cloud_execution(CloudHooks(lambda: None, Mock(), inner.append)):
            cloud_context.report_failure("summarization")
        cloud_context.report_failure("delivery")
    cloud_context.report_failure("delivery")
    assert outer == ["collection", "delivery"]
    assert inner == ["summarization"]


def test_cloud_config_rejects_endpoint_injection_without_echoing_value(monkeypatch):
    monkeypatch.setenv("DIGEST_CLOUD_ACCOUNT_URL", "https://attacker.example/token")
    with pytest.raises(ConfigError, match="DIGEST_CLOUD_ACCOUNT_URL") as error:
        CloudConfig.from_env()
    assert "attacker" not in str(error.value)


def test_cloud_config_accepts_normalized_azure_primary_endpoint(monkeypatch):
    monkeypatch.setenv("DIGEST_CLOUD_ACCOUNT_URL", "https://digeststore.blob.core.windows.net/")
    monkeypatch.setenv("DIGEST_CLOUD_CONTAINER", "digest-state")
    assert CloudConfig.from_env().account_url == "https://digeststore.blob.core.windows.net"


def test_job_resume_skips_completed_slot_and_backup_prunes(tmp_path, monkeypatch):
    db = connect(str(tmp_path / "state.db"))
    init_db(db)
    db.close()
    cloud = CloudConfig(
        "https://digeststore.blob.core.windows.net", "digest", data_dir=str(tmp_path)
    )
    state = Mock(lease_deadline=time.monotonic() + 60)
    dispatch = Mock(return_value=True)
    monkeypatch.setattr("digest.cloud_run._dispatch", dispatch)
    assert run_job("relay", cloud, _config(), "2026-07-05T19:40:00+00:00", state)
    assert run_job("relay", cloud, _config(), "2026-07-05T19:40:00+00:00", state)
    dispatch.assert_called_once()
    assert state.release.call_count == 2
    copy = Mock()
    monkeypatch.setattr("digest.cloud_backup.copy_daily_backup", copy)
    assert run_job("backup", cloud, _config(), "2026-07-05T04:15:00+00:00", state)
    copy.assert_called_once_with(state, cloud)
    state.prune.assert_called_once()
    assert any(call.kwargs.get("daily_backup") for call in state.checkpoint.call_args_list)


def test_restore_failure_never_dispatches_and_releases_lease(monkeypatch, tmp_path):
    cloud = CloudConfig(
        "https://digeststore.blob.core.windows.net", "digest", data_dir=str(tmp_path)
    )
    state = Mock(lease_deadline=time.monotonic() + 60)
    state.restore.side_effect = RuntimeError("corrupt state")
    dispatch = Mock(return_value=True)
    monkeypatch.setattr("digest.cloud_run._dispatch", dispatch)
    with pytest.raises(RuntimeError):
        run_job("relay", cloud, _config(), "2026-07-05T19:40:00+00:00", state)
    dispatch.assert_not_called()
    state.release.assert_called_once()
    assert not (tmp_path / "state.db").exists()


def test_renewal_loss_fences_immediately():
    state = Mock(lease_deadline=60)
    state.renew.side_effect = RuntimeError("lost lease")
    fence = Mock()
    watcher = LeaseWatchdog(state, fence, clock=lambda: 20)
    watcher.stopped.wait = Mock(return_value=False)
    watcher._renew()
    fence.assert_called_once()
    assert watcher.failed.is_set()


def test_runtime_budget_fences_and_logs_before_kill(caplog):
    state = Mock(lease_deadline=5000)
    fence = Mock()
    watcher = LeaseWatchdog(state, fence, clock=lambda: 0, job="daily")
    watcher.clock = lambda: 2100
    watcher.stopped.wait = Mock(return_value=False)
    fence.side_effect = lambda: watcher.stopped.wait.configure_mock(return_value=True)
    watcher._watch()
    fence.assert_called_once()
    assert "cloud_job event=failure job=daily error_type=RuntimeBudgetExceeded" in caplog.text
    assert "failure_stage=runtime" in caplog.text


async def test_stale_lease_prevents_mtproto_forward():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from digest.relay import _forward_one_chunk

    client = AsyncMock()
    with cloud_execution(CloudHooks(Mock(side_effect=RuntimeError("lease lost")), Mock())):
        with pytest.raises(CloudSafetyError):
            await _forward_one_chunk(
                client,
                object(),
                object(),
                None,
                123,
                [SimpleNamespace(id=1, action=None)],
                "mock",
            )
    client.assert_not_awaited()


@pytest.mark.parametrize(
    "job,hidden",
    [
        ("daytime", frozenset()),
        ("overnight", frozenset({"telegram"})),
        ("evening", frozenset({"telegram", "site"})),
    ],
)
def test_window_dispatch_preserves_hidden_channels(monkeypatch, job, hidden):
    from unittest.mock import AsyncMock

    run = AsyncMock(return_value=True)
    monkeypatch.setattr("digest.main._run", run)
    cfg = _config()
    assert _dispatch(job, cfg) is True
    run.assert_awaited_once_with(cfg, hidden)


def test_failed_work_retains_checkpoint_but_does_not_complete_slot(tmp_path, monkeypatch):
    db = connect(str(tmp_path / "state.db"))
    init_db(db)
    db.close()
    cloud = CloudConfig(
        "https://digeststore.blob.core.windows.net", "digest", data_dir=str(tmp_path)
    )
    state = Mock(lease_deadline=time.monotonic() + 60)
    slot = "2026-07-05T19:40:00+00:00"

    def dispatch(job, cfg):
        db = connect(cfg.state_db_path)
        commit_new_items(db, [], {("telegram", "group"): "100"})
        db.close()
        return False

    monkeypatch.setattr("digest.cloud_run._dispatch", dispatch)
    assert run_job("relay", cloud, _config(), slot, state) is False
    db = connect(str(tmp_path / "state.db"))
    assert not cloud_slot_completed(db, "relay", slot)
    assert get_cursors(db, "telegram") == {"group": "100"}
    db.close()
    assert state.checkpoint.call_count >= 3


def test_failed_summary_can_recover_persisted_items_without_duplicate_delivery(
    tmp_path, monkeypatch, caplog
):
    from digest import main
    from digest.summarize import ModelRun, SummarizeError

    cfg = replace(
        _config(),
        email_enabled=False,
        site_publish_url="https://site.example/ingest",
        site_ingest_key="mock",
        site_public_base="https://site.example",
    )
    cloud = CloudConfig(
        "https://digeststore.blob.core.windows.net", "digest", data_dir=str(tmp_path)
    )
    state = Mock(lease_deadline=time.monotonic() + 60)
    durable = sqlite3.connect(":memory:")
    init_db(durable)

    def restore():
        restored = sqlite3.connect(tmp_path / "state.db")
        durable.backup(restored)
        restored.close()

    state.restore.side_effect = restore
    state.checkpoint.side_effect = lambda db: db.backup(durable)
    item = Item(
        source="telegram",
        source_id="1:1",
        chat_id="1",
        author="mock",
        text="A collected story",
        url="https://t.me/example/1",
        fetched_at=datetime.now(UTC).isoformat(),
    )
    body = "**TL;DR:** A recovered story.\n\n## Updates\n\nA collected story."
    summarize = Mock(
        side_effect=[
            SummarizeError("fallback budget exhausted"),
            (body, {}, [], ModelRun("claude-sonnet-5-5", "high", True)),
        ]
    )
    monkeypatch.setattr(main, "summarize", summarize)
    monkeypatch.setattr(main, "archive", Mock())
    site, telegram = Mock(), Mock()
    monkeypatch.setattr(deliver, "publish_to_site", site)
    monkeypatch.setattr(deliver, "send_telegram_tldr", telegram)

    def dispatch(job, config):
        db = connect(config.state_db_path)
        try:
            commit_new_items(db, [item], {("telegram", "1"): "1"})
            return main._deliver(db, config, [])
        finally:
            db.close()

    dispatch_mock = Mock(side_effect=dispatch)
    monkeypatch.setattr("digest.cloud_run._dispatch", dispatch_mock)
    missed = datetime(2026, 7, 5, 12, tzinfo=UTC)
    slot = scheduled_slot("daytime", missed + timedelta(hours=1), catch_up_slot=missed)
    caplog.set_level("INFO")
    try:
        assert run_job("daytime", cloud, cfg, slot, state) is False
        assert durable.execute(
            "SELECT count(*) FROM items WHERE digest_id IS NULL"
        ).fetchone()[0] == 1
        assert durable.execute("SELECT count(*) FROM digests").fetchone()[0] == 0
        assert get_cursors(durable, "telegram") == {"1": "1"}
        assert not cloud_slot_completed(durable, "daytime", slot)
        assert "failure_stage=summarization" in caplog.text
        site.assert_not_called()
        telegram.assert_not_called()

        assert run_job("daytime", cloud, cfg, slot, state) is True
        assert durable.execute("SELECT count(*) FROM items").fetchone()[0] == 1
        assert durable.execute(
            "SELECT count(*) FROM items WHERE digest_id IS NULL"
        ).fetchone()[0] == 0
        digest_id, site_done, telegram_done = durable.execute(
            "SELECT id, site_published, telegram_sent FROM digests"
        ).fetchone()
        assert (site_done, telegram_done) == (1, 1)
        assert get_delivery_intent(durable, digest_id, "telegram") == "confirmed"
        assert cloud_slot_completed(durable, "daytime", slot)

        assert run_job("daytime", cloud, cfg, slot, state) is True
        assert dispatch_mock.call_count == 2
        assert summarize.call_count == 2
        site.assert_called_once()
        telegram.assert_called_once()
    finally:
        durable.close()


def test_fence_exits_even_when_namespace_process_group_is_missing(monkeypatch):
    from digest.cloud_run import _fence_process_group

    kill = Mock(side_effect=ProcessLookupError("namespace PID 1"))
    exit_process = Mock(side_effect=SystemExit(70))
    monkeypatch.setattr("digest.cloud_run.os.killpg", kill)
    monkeypatch.setattr("digest.cloud_run.os._exit", exit_process)
    with pytest.raises(SystemExit) as result:
        _fence_process_group()
    assert result.value.code == 70
    exit_process.assert_called_once_with(70)


@pytest.mark.parametrize(
    "job", ["daytime", "overnight", "evening", "positions", "patreon", "relay"]
)
def test_cursor_lane_lease_contention_is_clean_skip_without_dispatch(job, tmp_path, monkeypatch):
    from digest.cloud_state import LeaseBusyError

    state = Mock()
    state.acquire.side_effect = LeaseBusyError("busy")
    dispatch = Mock()
    monkeypatch.setattr("digest.cloud_run._dispatch", dispatch)
    cloud = CloudConfig(
        "https://digeststore.blob.core.windows.net", "digest", data_dir=str(tmp_path)
    )
    assert run_job(job, cloud, _config(), "slot", state)
    dispatch.assert_not_called()
    state.restore.assert_not_called()
    state.release.assert_not_called()


@pytest.mark.parametrize("job", ["daily", "weekly", "backup"])
def test_aggregate_and_backup_lease_contention_requires_catchup(job, tmp_path):
    from digest.cloud_state import LeaseBusyError

    state = Mock()
    state.acquire.side_effect = LeaseBusyError("busy")
    cloud = CloudConfig(
        "https://digeststore.blob.core.windows.net", "digest", data_dir=str(tmp_path)
    )
    with pytest.raises(LeaseBusyError):
        run_job(job, cloud, _config(), "slot", state)


@pytest.mark.parametrize(
    "job", ["daytime", "overnight", "evening", "positions", "patreon", "relay"]
)
def test_late_cursor_lanes_skip_and_explicit_catchup_still_valid(job):
    hour, minute = {
        "daytime": (6, 0),
        "overnight": (0, 0),
        "evening": (18, 0),
        "positions": (1, 25),
        "patreon": (0, 50),
        "relay": (0, 40),
    }[job]
    due = datetime(2026, 7, 5, hour, minute, tzinfo=UTC)
    assert scheduled_slot(job, due + timedelta(minutes=11)) is None
    assert scheduled_slot(job, due + timedelta(minutes=11), catch_up_slot=due) == due.isoformat()


def test_backup_copy_failure_does_not_complete_slot(tmp_path, monkeypatch):
    db = connect(str(tmp_path / "state.db"))
    init_db(db)
    db.close()
    cloud = CloudConfig(
        "https://digeststore.blob.core.windows.net", "digest", data_dir=str(tmp_path)
    )
    state = Mock(lease_deadline=time.monotonic() + 60)
    monkeypatch.setattr(
        "digest.cloud_backup.copy_daily_backup", Mock(side_effect=RuntimeError("copy"))
    )
    with pytest.raises(RuntimeError):
        run_job("backup", cloud, _config(), "slot", state)
    db = connect(str(tmp_path / "state.db"))
    assert not cloud_slot_completed(db, "backup", "slot")
    db.close()
    state.release.assert_called_once()
