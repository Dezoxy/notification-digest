"""Operator commands isolate restored credentials and clean up lease users first."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from digest import cloud_reconcile, cloud_run, cloud_state
from digest.config import CloudConfig


@pytest.fixture
def operator(monkeypatch, tmp_path):
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    marker = persistent / "state.db"
    marker.write_bytes(b"existing-local-state")
    cfg = CloudConfig(
        account_url="https://runtimestore.blob.core.windows.net",
        container="digest-state",
        data_dir=str(persistent),
        identity_client_id="runner-identity",
    )
    monkeypatch.setattr(CloudConfig, "from_env", lambda: cfg)
    monkeypatch.setattr(cloud_reconcile.os, "getpgrp", cloud_reconcile.os.getpid)
    monkeypatch.setattr(cloud_reconcile.signal, "signal", Mock())
    events = []
    workspaces = []
    state = Mock()
    watchdog = Mock()
    conn = Mock()

    def record(event):
        assert workspaces[-1].is_dir(), "Workspace removed before cleanup"
        events.append(event)

    def factory(*args, **kwargs):
        workspace = Path(kwargs["data_dir"])
        assert workspace != persistent
        assert workspace.is_dir()
        assert workspace.stat().st_mode & 0o077 == 0
        assert kwargs["managed_identity_client_id"] == cfg.identity_client_id
        state.data_dir = workspace
        workspaces.append(workspace)
        return state

    state.acquire.side_effect = lambda: record("acquire")
    state.restore.side_effect = lambda: (
        record("restore"),
        (state.data_dir / "cookie").write_text("credential"),
    )
    state.release.side_effect = lambda: record("release")
    watchdog.start.side_effect = lambda: record("start")
    watchdog.stop.side_effect = lambda: record("stop")
    conn.close.side_effect = lambda: record("close")
    state_factory = Mock(side_effect=factory)
    monkeypatch.setattr(cloud_state.CloudState, "from_account_url", state_factory)
    monkeypatch.setattr(cloud_reconcile, "LeaseWatchdog", Mock(return_value=watchdog))
    monkeypatch.setattr(cloud_run, "LeaseWatchdog", Mock(return_value=watchdog))

    def connection(path):
        assert Path(path) == workspaces[-1] / "state.db"
        return conn

    monkeypatch.setattr(cloud_reconcile, "connect", connection)
    monkeypatch.setattr(cloud_reconcile, "init_db", Mock())
    monkeypatch.setattr(cloud_reconcile, "reconcile_delivery", Mock())
    yield SimpleNamespace(
        persistent=persistent,
        events=events,
        workspaces=workspaces,
        state=state,
        watchdog=watchdog,
        conn=conn,
        factory=state_factory,
    )
    assert marker.read_bytes() == b"existing-local-state"
    assert list(persistent.iterdir()) == [marker]
    assert all(not workspace.exists() for workspace in workspaces)


@pytest.mark.parametrize(
    "failure", [None, "restore", "reconcile", "close", "start", "acquire", "stop", "release"]
)
def test_reconcile_private_workspace_cleanup_and_resource_order(operator, monkeypatch, failure):
    monkeypatch.setattr(
        "sys.argv", ["cloud_reconcile", "12", "telegram", "confirmed", "--operator-login"]
    )
    if failure == "reconcile":
        cloud_reconcile.reconcile_delivery.side_effect = RuntimeError("private-error")
    elif failure is not None:
        target = (
            operator.conn
            if failure == "close"
            else operator.watchdog
            if failure in {"start", "stop"}
            else operator.state
        )
        getattr(target, failure).side_effect = RuntimeError("private-error")
    if failure is None:
        cloud_reconcile.main()
    else:
        with pytest.raises(SystemExit) as exc:
            cloud_reconcile.main()
        assert exc.value.code == 1
    operator.state.release.assert_called_once()
    if failure != "release":
        assert operator.events[-1] == "release"
    if failure not in {"start", "acquire", "stop"}:
        assert "stop" in operator.events
    if failure in {None, "reconcile"}:
        assert operator.events[-3:] == ["close", "stop", "release"]
    if failure == "close":
        assert operator.events[-2:] == ["stop", "release"]
    assert operator.factory.call_args.kwargs["operator_login"] is True


@pytest.mark.parametrize("failure", [None, "export", "start", "stop", "acquire", "release"])
def test_export_retains_destination_and_cleans_workspace_after_lease(
    operator, monkeypatch, tmp_path, failure
):
    destination = tmp_path / "export"
    monkeypatch.setattr(
        "sys.argv", ["cloud_state", "export", str(destination), "--backup-date", "2026-10-08"]
    )

    def export(path, *, backup_date):
        assert path == destination
        assert backup_date == "2026-10-08"
        assert operator.workspaces[-1].exists()
        destination.mkdir(mode=0o700)
        (destination / "bundle.tar.gz").write_bytes(b"private-bundle")
        if failure == "export":
            raise RuntimeError("private-error")

    operator.state.export.side_effect = export
    if failure in {"start", "stop"}:
        getattr(operator.watchdog, failure).side_effect = RuntimeError("private-error")
    elif failure in {"acquire", "release"}:
        getattr(operator.state, failure).side_effect = RuntimeError("private-error")
    if failure is None:
        assert cloud_state.main() == 0
    else:
        with pytest.raises(RuntimeError, match="private-error"):
            cloud_state.main()
    operator.state.release.assert_called_once()
    if failure != "acquire":
        operator.watchdog.stop.assert_called_once()
    if failure in {None, "export", "stop"}:
        assert (destination / "bundle.tar.gz").read_bytes() == b"private-bundle"
    if failure in {None, "export", "start"}:
        assert operator.events[-2:] == ["stop", "release"]


@pytest.mark.parametrize("exported", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_bootstrap_private_workspace_leaves_source_untouched(
    operator, monkeypatch, tmp_path, exported, fail
):
    source = tmp_path / "source"
    source.mkdir()
    filename = "bundle.tar.gz" if exported else "state.db"
    (source / filename).write_bytes(b"private-source")
    monkeypatch.setattr("sys.argv", ["cloud_state", "--operator-login", "bootstrap", str(source)])
    operation = operator.state.bootstrap_export if exported else operator.state.bootstrap

    def bootstrap(*args, **kwargs):
        assert operator.workspaces[-1].exists()
        assert args[0] == (source if exported else source / "state.db")
        if not exported:
            assert kwargs == {
                "archive_dir": source / "archive",
                "live_cookies_path": source / "x-cookies.json.live",
            }
        (operator.workspaces[-1] / "private-working-copy").write_bytes(b"scratch")
        if fail:
            raise RuntimeError("private-error")

    operation.side_effect = bootstrap
    if fail:
        with pytest.raises(RuntimeError):
            cloud_state.main()
    else:
        assert cloud_state.main() == 0
    assert (source / filename).read_bytes() == b"private-source"
    assert len(list(source.iterdir())) == 1
    assert operator.events == ["release"]
    operator.watchdog.start.assert_not_called()
