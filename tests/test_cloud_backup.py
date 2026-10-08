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
    prefix = copy_daily_backup(
        state,
        config(tmp_path),
        container_client=target,
        clock=state.container.clock,
        sleep=state.container.sleep,
    )
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


@pytest.mark.parametrize("point,expected_blobs", [("bundle", 0), ("manifest", 3)])
def test_partial_failed_copy_has_no_completion_marker(tmp_path, source, point, expected_blobs):
    state = state_fixture(tmp_path, source)
    target = FakeContainer()
    target.failures.add(point)
    with pytest.raises(StorageError):
        copy_daily_backup(
            state,
            config(tmp_path),
            container_client=target,
            clock=state.container.clock,
            sleep=state.container.sleep,
        )
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
            copy_daily_backup(
                state,
                config(tmp_path),
                container_client=target,
                clock=state.container.clock,
                sleep=state.container.sleep,
            )
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
        copy_daily_backup(
            state,
            config(tmp_path),
            container_client=target,
            clock=state.container.clock,
            sleep=state.container.sleep,
        )
    assert not any(name.endswith("manifest.json") for name in target.data)
    state.release()


class FaultyBackupContainer(FakeContainer):
    """Inject SDK failures before/after upload or midway through streaming readback."""

    def __init__(self, stage, error, *, failures_remaining=1, on_failure=lambda: None):
        super().__init__()
        self.stage = stage
        self.error = error
        self.failures_remaining = failures_remaining
        self.on_failure = on_failure
        self.uploads = []
        self.downloads = []
        self.failure_count = 0

    def fault(self, stage):
        if self.stage == stage and self.failures_remaining:
            self.failures_remaining -= 1
            self.failure_count += 1
            self.on_failure()
            raise self.error

    def get_blob_client(self, name):
        blob = super().get_blob_client(name)
        if getattr(blob, "instrumented", False):
            return blob
        blob.instrumented = True
        upload, download = blob.upload_blob, blob.download_blob
        kind = "bundle" if name.endswith("bundle.tar.gz") else "marker"

        def instrumented_upload(*args, **kwargs):
            self.uploads.append((name, kwargs["overwrite"]))
            self.fault(f"{kind}_upload")
            result = upload(*args, **kwargs)
            self.fault(f"{kind}_ack")
            return result

        def instrumented_download(*args, **kwargs):
            self.downloads.append(name)
            result = download(*args, **kwargs)

            def chunks():
                # Exercise failure after consuming part of a response, not only
                # a rejected request before hashing any stored bytes.
                yield result.payload[:10]
                self.fault(f"{kind}_readback")
                yield result.payload[10:]

            result.chunks = chunks
            return result

        blob.upload_blob, blob.download_blob = instrumented_upload, instrumented_download
        return blob


@pytest.mark.parametrize(
    "stage",
    [
        "bundle_upload",
        "bundle_ack",
        "bundle_readback",
        "marker_upload",
        "marker_ack",
        "marker_readback",
    ],
)
def test_transient_backup_copy_retries_whole_export_with_fresh_prefix(tmp_path, source, stage):
    from azure.core.exceptions import ServiceResponseError

    state = state_fixture(tmp_path, source)
    original_export = state.export
    state.export = Mock(wraps=original_export)
    target = FaultyBackupContainer(stage, ServiceResponseError("transient"))
    prefix = copy_daily_backup(
        state,
        config(tmp_path),
        container_client=target,
        clock=state.container.clock,
        sleep=state.container.sleep,
    )
    assert target.failure_count == 1
    assert state.container.seconds == 0.5
    state.export.assert_called_once()
    prefixes = {name.rsplit("/", 1)[0] for name, _ in target.uploads}
    assert len(prefixes) == 2
    assert prefix in prefixes
    assert all(overwrite is False for _, overwrite in target.uploads)
    assert len({name for name, _ in target.uploads}) == len(target.uploads)
    assert f"{prefix}/bundle.tar.gz" in target.data
    assert f"{prefix}/manifest.json" in target.data
    # Completion marker was never uploaded before that attempt's bundle readback.
    assert f"{prefix}/bundle.tar.gz" in target.downloads
    assert target.downloads.index(f"{prefix}/bundle.tar.gz") < target.downloads.index(
        f"{prefix}/manifest.json"
    )
    state.release()


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_backup_statuses_have_bounded_three_attempts(tmp_path, source, status):
    state = state_fixture(tmp_path, source)
    target = FaultyBackupContainer("marker_upload", StorageError(status), failures_remaining=10)
    with pytest.raises(StorageError):
        copy_daily_backup(
            state,
            config(tmp_path),
            container_client=target,
            clock=state.container.clock,
            sleep=state.container.sleep,
        )
    assert target.failure_count == 3
    assert state.container.seconds == 1.5
    prefixes = {name.rsplit("/", 1)[0] for name, _ in target.uploads}
    assert len(prefixes) == 3
    assert not any(name.endswith("manifest.json") for name in target.data)
    assert len(target.data) == 3
    state.release()


@pytest.mark.parametrize("status", [401, 403, 404, 409, 412])
def test_known_backup_authorization_missing_state_or_lease_failure_does_not_retry(
    tmp_path, source, status
):
    state = state_fixture(tmp_path, source)
    target = FaultyBackupContainer("bundle_upload", StorageError(status), failures_remaining=10)
    with pytest.raises(StorageError):
        copy_daily_backup(
            state,
            config(tmp_path),
            container_client=target,
            clock=state.container.clock,
            sleep=state.container.sleep,
        )
    assert len(target.uploads) == 1
    assert target.failure_count == 1
    assert state.container.seconds == 0
    state.release()


def test_backup_corruption_is_never_retried(tmp_path, source):
    state = state_fixture(tmp_path, source)
    target = FaultyBackupContainer(
        "bundle_readback", CloudStateError("corrupt"), failures_remaining=10
    )
    with pytest.raises(CloudStateError, match="corrupt"):
        copy_daily_backup(
            state,
            config(tmp_path),
            container_client=target,
            clock=state.container.clock,
            sleep=state.container.sleep,
        )
    assert len(target.uploads) == 1
    assert target.failure_count == 1
    assert state.container.seconds == 0
    state.release()


def test_backup_retry_refuses_backoff_that_reaches_safety_deadline(tmp_path, source):
    state = state_fixture(tmp_path, source)
    target = FaultyBackupContainer("bundle_upload", StorageError(503), failures_remaining=10)
    state.container.seconds = 49.75
    with pytest.raises(CloudStateError, match="safe lease budget"):
        copy_daily_backup(
            state,
            config(tmp_path),
            container_client=target,
            clock=state.container.clock,
            sleep=state.container.sleep,
        )
    assert len(target.uploads) == 1
    assert state.container.seconds == 49.75
    assert state.lease_deadline == 60
    state.release()


def test_lease_loss_after_transient_failure_prevents_retry_or_wait(tmp_path, source):
    state = state_fixture(tmp_path, source)
    target = FaultyBackupContainer(
        "bundle_upload",
        StorageError(503),
        on_failure=lambda: setattr(state, "_fenced", True),
    )
    with pytest.raises(CloudStateError, match="cannot be proven"):
        copy_daily_backup(
            state,
            config(tmp_path),
            container_client=target,
            clock=state.container.clock,
            sleep=state.container.sleep,
        )
    assert len(target.uploads) == 1
    assert state.container.seconds == 0
    state.release()


def test_safety_loss_during_backoff_prevents_new_attempt(tmp_path, source):
    state = state_fixture(tmp_path, source)
    target = FaultyBackupContainer("bundle_upload", StorageError(503))

    def pause(_):
        state.container.seconds = state.lease_deadline - 10

    with pytest.raises(CloudStateError, match="proven safe lease time"):
        copy_daily_backup(
            state,
            config(tmp_path),
            container_client=target,
            clock=state.container.clock,
            sleep=pause,
        )
    assert len(target.uploads) == 1
    state.release()
