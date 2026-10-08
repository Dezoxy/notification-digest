"""Offline credential rotation validates private input and preserves canonical state."""

import json
from unittest.mock import Mock

import pytest
from test_cloud_state import FakeContainer, bootstrap, make_state
from test_cloud_state import source as source

from digest import cloud_cookies
from digest.cloud_context import CloudSafetyError
from digest.cloud_run import LeaseWatchdog
from digest.cloud_state import CloudStateError
from digest.state import connect, create_digest, get_delivery_intent, set_delivery_intent


@pytest.fixture
def private_cookies(tmp_path):
    path = tmp_path / "fresh.json"
    path.write_text(json.dumps({"auth_token": "test-fresh-token", "ct0": "test-fresh-csrf"}))
    path.chmod(0o600)
    return path


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "{}",
        "bad json",
        '{"auth_token":"a"}',
        '{"auth_token":"a","ct0":4}',
        '{"auth_token":"","ct0":"c"}',
        '{"auth_token":"a","ct0":"c","extra":null}',
    ],
)
def test_cookie_input_rejects_invalid_mapping(private_cookies, payload):
    private_cookies.write_text(payload)
    with pytest.raises(CloudStateError, match="private valid"):
        cloud_cookies.read_private_cookies(private_cookies)


def test_cookie_input_rejects_public_file_symlink_and_oversize(private_cookies, tmp_path):
    private_cookies.chmod(0o644)
    with pytest.raises(CloudStateError):
        cloud_cookies.read_private_cookies(private_cookies)
    private_cookies.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(private_cookies)
    with pytest.raises(CloudStateError):
        cloud_cookies.read_private_cookies(alias)
    private_cookies.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(CloudStateError):
        cloud_cookies.read_private_cookies(private_cookies)


def _state_with_guarded_watchdog(tmp_path, source, monkeypatch):
    container = FakeContainer()
    state = make_state(container, tmp_path / "cloud")
    bootstrap(state, source)
    monkeypatch.setattr(
        cloud_cookies,
        "LeaseWatchdog",
        lambda state, fence, **kwargs: LeaseWatchdog(state, fence, clock=container.clock, **kwargs),
    )
    return state, container


def test_rotation_preserves_cursors_and_uncertain_delivery_and_publishes_both_jars(
    tmp_path, source, private_cookies, monkeypatch, caplog
):
    conn = connect(str(source / "state.db"))
    digest_id = create_digest(conn, "body", [])
    set_delivery_intent(conn, digest_id, "telegram", "uncertain")
    before = list(conn.iterdump())
    conn.close()
    state, container = _state_with_guarded_watchdog(tmp_path, source, monkeypatch)
    cloud_cookies.rotate_cookies(state, private_cookies)
    assert state._lease is None
    restored = make_state(container, tmp_path / "restored")
    restored.acquire()
    restored.restore()
    conn = connect(str(restored.data_dir / "state.db"))
    assert list(conn.iterdump()) == before
    assert get_delivery_intent(conn, digest_id, "telegram") == "uncertain"
    conn.close()
    for name in ("x-cookies.json", "x-cookies.json.live"):
        cookie = restored.data_dir / name
        assert json.loads(cookie.read_bytes()) == json.loads(private_cookies.read_bytes())
        assert cookie.stat().st_mode & 0o077 == 0
    assert (restored.data_dir / "x-cookies.json.live").stat().st_mtime >= (
        restored.data_dir / "x-cookies.json"
    ).stat().st_mtime
    assert "test-fresh-token" not in caplog.text
    assert "test-fresh-csrf" not in caplog.text
    restored.release()


def test_invalid_input_never_acquires_lease(private_cookies):
    private_cookies.chmod(0o644)
    state = Mock()
    with pytest.raises(CloudStateError):
        cloud_cookies.rotate_cookies(state, private_cookies)
    state.acquire.assert_not_called()


def test_rotation_checkpoint_failure_retains_old_authoritative_cookies(
    tmp_path, source, private_cookies, monkeypatch
):
    state, container = _state_with_guarded_watchdog(tmp_path, source, monkeypatch)
    original_manifest = container.data["production/manifest.json"]
    container.failures.add("manifest")
    with pytest.raises(CloudSafetyError):
        cloud_cookies.rotate_cookies(state, private_cookies)
    assert container.data["production/manifest.json"] == original_manifest
    assert state._lease is None


def test_rotation_stale_guard_never_publishes(tmp_path, source, private_cookies, monkeypatch):
    state, container = _state_with_guarded_watchdog(tmp_path, source, monkeypatch)
    watchdog = Mock()
    watchdog.guard.side_effect = RuntimeError("lease lost")
    monkeypatch.setattr(cloud_cookies, "LeaseWatchdog", Mock(return_value=watchdog))
    original_manifest = container.data["production/manifest.json"]
    with pytest.raises(RuntimeError):
        cloud_cookies.rotate_cookies(state, private_cookies)
    assert container.data["production/manifest.json"] == original_manifest
    watchdog.stop.assert_called_once()
    assert state._lease is None


def test_non_utf8_source_is_normalized_for_collector(private_cookies):
    mapping = {"auth_token": "test-token", "ct0": "test-csrf"}
    private_cookies.write_bytes(json.dumps(mapping).encode("utf-16"))
    normalized = cloud_cookies.read_private_cookies(private_cookies)
    assert json.loads(normalized.decode("utf-8")) == mapping
