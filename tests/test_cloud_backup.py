"""Offline independent copies are complete only when their manifest is published."""

from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_cloud_state import FakeContainer, StorageError, bootstrap, make_state
from test_cloud_state import source as source

from digest.cloud_backup import copy_daily_backup
from digest.cloud_context import CloudHooks, CloudSafetyError, cloud_execution
from digest.cloud_state import CloudStateError
from digest.config import CloudConfig, ConfigError

BACKUP_ID = "22222222-2222-2222-2222-222222222222"
RUNTIME_ID = "11111111-1111-1111-1111-111111111111"


def config(tmp_path):
    return CloudConfig(
        "https://digeststore.blob.core.windows.net",
        "digest-state",
        data_dir=str(tmp_path),
        identity_client_id=RUNTIME_ID,
        backup_account_url="https://digestbackup.blob.core.windows.net",
        backup_container="digest-backups",
        backup_identity_client_id=BACKUP_ID,
    )


def state_fixture(tmp_path, source):
    state = make_state(FakeContainer(), tmp_path / "state")
    bootstrap(state, source)
    state.acquire()
    state.restore()
    return state


def test_independent_copy_is_compatible_export_and_streams_bundle_before_marker(tmp_path, source):
    state = state_fixture(tmp_path, source)
    target = FakeContainer()
    prefix = copy_daily_backup(state, config(tmp_path), container_client=target)
    assert prefix.startswith("backups/production/")
    assert list(target.data) == [f"{prefix}/bundle.tar.gz", f"{prefix}/manifest.json"]
    exported = tmp_path / "downloaded"
    exported.mkdir()
    for name in ("bundle.tar.gz", "manifest.json"):
        (exported / name).write_bytes(target.data[f"{prefix}/{name}"][0])
    recovery = make_state(FakeContainer(), tmp_path / "recovery", namespace="recovered")
    recovery.bootstrap_export(exported)
    recovery.acquire()
    recovery.restore()
    assert (recovery.data_dir / "x-cookies.json.live").read_bytes() == (
        source / "x-cookies.json.live"
    ).read_bytes()
    assert not list(state.data_dir.glob(".independent-backup-*"))
    state.release()
    recovery.release()


@pytest.mark.parametrize("point,expected_blobs", [("bundle", 0), ("manifest", 1)])
def test_partial_failed_copy_has_no_completion_marker(tmp_path, source, point, expected_blobs):
    state = state_fixture(tmp_path, source)
    target = FakeContainer()
    target.failures.add(point)
    with pytest.raises(StorageError):
        copy_daily_backup(state, config(tmp_path), container_client=target)
    assert len(target.data) == expected_blobs
    assert not any(name.endswith("manifest.json") for name in target.data)
    assert not list(state.data_dir.glob(".independent-backup-*"))
    state.release()


@pytest.mark.parametrize(
    "changes",
    [
        {"backup_account_url": None},
        {"backup_container": None},
        {"backup_identity_client_id": None},
        {"backup_account_url": "https://digeststore.blob.core.windows.net/"},
        {"backup_identity_client_id": RUNTIME_ID},
        {"namespace": "../../unsafe"},
    ],
)
def test_invalid_independent_backup_configuration_never_exports(tmp_path, changes):
    state = Mock()
    with pytest.raises(CloudStateError):
        copy_daily_backup(state, replace(config(tmp_path), **changes), container_client=Mock())
    state.export.assert_not_called()


def test_stale_guard_prevents_backup_upload(tmp_path, source):
    state = state_fixture(tmp_path, source)
    target = FakeContainer()
    with cloud_execution(CloudHooks(Mock(side_effect=RuntimeError("lost")), Mock())):
        with pytest.raises(CloudSafetyError):
            copy_daily_backup(state, config(tmp_path), container_client=target)
    assert not target.data
    state.release()


def _backup_env(monkeypatch):
    monkeypatch.setenv("DIGEST_CLOUD_ACCOUNT_URL", "https://digeststore.blob.core.windows.net")
    monkeypatch.setenv("DIGEST_CLOUD_CONTAINER", "digest-state")
    monkeypatch.setenv(
        "DIGEST_CLOUD_BACKUP_ACCOUNT_URL", "https://digestbackup.blob.core.windows.net/"
    )
    monkeypatch.setenv("DIGEST_CLOUD_BACKUP_CONTAINER", "digest-backups")
    monkeypatch.setenv("DIGEST_CLOUD_BACKUP_IDENTITY_CLIENT_ID", BACKUP_ID)


def test_backup_configuration_normalizes_azure_url(monkeypatch):
    _backup_env(monkeypatch)
    cfg = CloudConfig.from_env()
    assert cfg.backup_account_url == "https://digestbackup.blob.core.windows.net"
    assert cfg.backup_identity_client_id == BACKUP_ID


@pytest.mark.parametrize(
    "variable,value",
    [
        ("DIGEST_CLOUD_BACKUP_ACCOUNT_URL", "https://attacker.example/session"),
        ("DIGEST_CLOUD_BACKUP_ACCOUNT_URL", "https://digeststore.blob.core.windows.net/"),
        ("DIGEST_CLOUD_BACKUP_CONTAINER", "invalid--container"),
        ("DIGEST_CLOUD_BACKUP_IDENTITY_CLIENT_ID", "not-a-uuid"),
        ("DIGEST_CLOUD_NAMESPACE", "../unsafe"),
    ],
)
def test_backup_env_rejects_bad_settings_without_echo(monkeypatch, variable, value):
    _backup_env(monkeypatch)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ConfigError) as error:
        CloudConfig.from_env()
    assert variable in str(error.value)
    assert value not in str(error.value)


def test_partial_backup_settings_are_rejected(monkeypatch):
    _backup_env(monkeypatch)
    monkeypatch.delenv("DIGEST_CLOUD_BACKUP_CONTAINER")
    with pytest.raises(ConfigError, match="configured together"):
        CloudConfig.from_env()


@pytest.mark.parametrize("corruption", ["same_length", "truncated", "missing"])
def test_corrupt_readback_does_not_publish_completion_marker(tmp_path, source, corruption):
    state = state_fixture(tmp_path, source)
    target = FakeContainer()
    original = target.get_blob_client

    def client(name):
        blob = original(name)
        if name.endswith("bundle.tar.gz"):
            upload = blob.upload_blob

            def corrupt_upload(*args, **kwargs):
                result = upload(*args, **kwargs)
                payload, etag = target.data[name]
                if corruption == "same_length":
                    target.data[name] = bytes([payload[0] ^ 1]) + payload[1:], etag
                elif corruption == "truncated":
                    target.data[name] = payload[:-1], etag
                else:
                    del target.data[name]
                return result

            blob.upload_blob = corrupt_upload
        return blob

    target.get_blob_client = client
    with pytest.raises((CloudStateError, StorageError)):
        copy_daily_backup(state, config(tmp_path), container_client=target)
    assert not any(name.endswith("manifest.json") for name in target.data)
    state.release()
