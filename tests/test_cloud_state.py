"""Offline failure injection for the authoritative cloud state contract."""

import io
import json
import os
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from digest.cloud_state import CloudState, CloudStateError, LeaseBusyError
from digest.state import connect, init_db


class StorageError(Exception):
    def __init__(self, status_code: int, error_code: str = "") -> None:
        self.status_code, self.error_code = status_code, error_code


class FakeLease:
    def __init__(self, blob: "FakeBlob") -> None:
        self.blob = blob
        self.id = f"lease-{blob.container.counter}"
        self.deadline = blob.container.clock() + 60

    def renew(self) -> None:
        self.blob.container.fail("renew")
        if self.blob.lease is not self or self.blob.container.clock() >= self.deadline:
            raise StorageError(412)
        self.deadline = self.blob.container.clock() + 60

    def release(self) -> None:
        if self.blob.lease is self:
            self.blob.lease = None


class FakeDownload:
    def __init__(self, payload: bytes, etag: str) -> None:
        self.payload = payload
        self.properties = SimpleNamespace(etag=etag)

    def readall(self) -> bytes:
        return self.payload

    def readinto(self, stream: io.BytesIO) -> int:
        return stream.write(self.payload)

    def chunks(self):
        yield self.payload


class FakeBlob:
    def __init__(self, container: "FakeContainer", name: str) -> None:
        self.container, self.name = container, name
        self.lease: FakeLease | None = None

    def acquire_lease(self, *, lease_duration: int) -> FakeLease:
        assert lease_duration == 60
        self.container.fail("acquire")
        if self.name not in self.container.data:
            raise StorageError(404)
        if self.lease is not None and self.container.clock() < self.lease.deadline:
            raise StorageError(409, "LeaseAlreadyPresent")
        self.lease = FakeLease(self)
        return self.lease

    def download_blob(
        self,
        *,
        lease: FakeLease | None = None,
        length: int | None = None,
        offset: int | None = None,
    ):
        self.container.fail("download")
        if self.name not in self.container.data:
            raise StorageError(404)
        payload, etag = self.container.data[self.name]
        return FakeDownload(payload if length is None else payload[:length], etag)

    def upload_blob(self, payload, *, overwrite: bool, lease=None, etag=None, **kwargs):
        failure = "manifest" if self.name.endswith("manifest.json") else "bundle"
        self.container.fail(failure)
        if not overwrite and self.name in self.container.data:
            raise StorageError(409)
        if self.lease is not None:
            if lease is not self.lease or self.container.clock() >= self.lease.deadline:
                raise StorageError(412)
        if etag is not None:
            assert kwargs["match_condition"].name == "IfNotModified"
            if self.container.data[self.name][1] != etag:
                raise StorageError(412)
        if hasattr(payload, "read"):
            payload = payload.read()
        self.container.counter += 1
        new_etag = f"etag-{self.container.counter}"
        self.container.data[self.name] = (payload, new_etag)
        self.container.fail("after_manifest" if failure == "manifest" else "after_bundle")
        return {"etag": new_etag}

    def delete_blob(self) -> None:
        self.container.fail("delete")
        del self.container.data[self.name]


class FakeContainer:
    def __init__(self) -> None:
        self.data: dict[str, tuple[bytes, str]] = {}
        self.blobs: dict[str, FakeBlob] = {}
        self.counter = 0
        self.seconds = 0.0
        self.failures: set[str] = set()

    def clock(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds

    def fail(self, point: str) -> None:
        if point in self.failures:
            raise StorageError(503)

    def get_blob_client(self, name: str) -> FakeBlob:
        if name not in self.blobs:
            self.blobs[name] = FakeBlob(self, name)
        return self.blobs[name]

    def list_blobs(self, *, name_starts_with: str):
        self.fail("list")
        return [
            SimpleNamespace(name=name) for name in self.data if name.startswith(name_starts_with)
        ]


@pytest.fixture
def source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    connection = connect(str(source / "state.db"))
    init_db(connection)
    connection.execute(
        "INSERT INTO cursors(source, scope, last_seen_id, updated_at) "
        "VALUES ('telegram', '42', '91', '2026-10-08T00:00:00Z')"
    )
    connection.commit()
    connection.close()
    (source / "archive" / "2026").mkdir(parents=True)
    (source / "archive" / "2026" / "digest.md").write_text("A preserved digest")
    (source / "x-cookies.json").write_text('{"auth_token":"fake-seed"}')
    (source / "x-cookies.json.live").write_text('{"auth_token":"fake-rotated"}')
    # A newer live jar must remain newer after export/bootstrap/restore.
    os.utime(source / "x-cookies.json", (100, 100))
    os.utime(source / "x-cookies.json.live", (200, 200))
    return source


def make_state(container: FakeContainer, directory: Path, **kwargs) -> CloudState:
    return CloudState(
        container, data_dir=directory, clock=container.clock, sleep=container.sleep, **kwargs
    )


def bootstrap(state: CloudState, source: Path) -> dict:
    return state.bootstrap(
        source / "state.db",
        archive_dir=source / "archive",
        live_cookies_path=source / "x-cookies.json.live",
    )


def test_missing_cloud_state_never_initializes(tmp_path):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    with pytest.raises(CloudStateError, match="bootstrap"):
        state.acquire()
    assert not container.data
    assert not (tmp_path / "data" / "state.db").exists()


def test_online_snapshot_cookies_archives_and_private_modes(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    state.restore()
    connection = connect(str(state.data_dir / "state.db"))
    assert connection.execute("SELECT last_seen_id FROM cursors").fetchone()[0] == "91"
    # The change is committed in WAL; copying state.db alone would lose it.
    connection.execute("UPDATE cursors SET last_seen_id = '92'")
    connection.commit()
    state.checkpoint(connection)
    connection.close()
    state.restore()
    with sqlite3.connect(state.data_dir / "state.db") as restored:
        assert restored.execute("SELECT last_seen_id FROM cursors").fetchone()[0] == "92"
    assert (state.data_dir / "archive" / "2026" / "digest.md").read_text() == "A preserved digest"
    assert json.loads((state.data_dir / "x-cookies.json.live").read_bytes())["auth_token"] == (
        "fake-rotated"
    )
    assert (state.data_dir / "x-cookies.json.live").stat().st_mtime == 200
    assert (state.data_dir / "x-cookies.json").stat().st_mtime == 100
    for path in state.data_dir.rglob("*"):
        assert path.stat().st_mode & 0o077 == 0
    state.release()


def test_seed_only_bootstrap_preserves_authentication(tmp_path, source):
    (source / "x-cookies.json.live").unlink()
    state = make_state(FakeContainer(), tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    state.restore()
    assert (state.data_dir / "x-cookies.json").exists()
    assert not (state.data_dir / "x-cookies.json.live").exists()


def test_bootstrap_cannot_replace_existing_manifest_or_accept_wrong_db(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    manifest = bootstrap(state, source)
    with pytest.raises(CloudStateError, match="already exists"):
        bootstrap(state, source)
    assert json.loads(container.data["production/manifest.json"][0]) == manifest
    wrong = tmp_path / "empty.db"
    sqlite3.connect(wrong).close()
    other = make_state(container, tmp_path / "other", namespace="isolated")
    with pytest.raises(CloudStateError, match="not a digest"):
        other.bootstrap(wrong)
    assert "isolated/manifest.json" not in container.data
    with pytest.raises(CloudStateError, match="existing regular"):
        other.bootstrap(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_lease_serializes_modes_and_wait_is_bounded(tmp_path, source):
    container = FakeContainer()
    first = make_state(container, tmp_path / "first")
    bootstrap(first, source)
    first.acquire()
    second = make_state(container, tmp_path / "second", lease_wait_seconds=10)
    with pytest.raises(CloudStateError, match="budget"):
        second.acquire()
    assert container.seconds == 10
    first.release()
    second.acquire()
    second.release()


def test_lease_renew_failure_permanently_fences_writes(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    state.restore()
    container.seconds = 15
    container.failures.add("renew")
    with pytest.raises(CloudStateError, match="renewal failed"):
        state.renew()
    container.failures.clear()
    with pytest.raises(CloudStateError, match="cannot be proven"):
        state.check_lease()
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        with pytest.raises(CloudStateError):
            state.checkpoint(connection)


def test_expired_process_cannot_publish_after_new_owner_acquires(tmp_path, source):
    container = FakeContainer()
    first = make_state(container, tmp_path / "first")
    bootstrap(first, source)
    first.acquire()
    first.restore()
    container.seconds = 61
    second = make_state(container, tmp_path / "second")
    second.acquire()
    second.restore()
    with pytest.raises(CloudStateError, match="cannot be proven"):
        first.prune()
    with sqlite3.connect(first.data_dir / "state.db") as connection:
        with pytest.raises(CloudStateError):
            first.checkpoint(connection)


@pytest.mark.parametrize("failure", ["bundle", "manifest"])
def test_failed_checkpoint_keeps_old_authoritative_manifest(tmp_path, source, failure):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    original = bootstrap(state, source)
    state.acquire()
    state.restore()
    container.failures.add(failure)
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        connection.execute("UPDATE cursors SET last_seen_id='92'")
        connection.commit()
        with pytest.raises(CloudStateError):
            state.checkpoint(connection)
    assert json.loads(container.data["production/manifest.json"][0]) == original


def test_manifest_ack_loss_fences_even_when_server_committed(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    original = bootstrap(state, source)
    state.acquire()
    state.restore()
    container.failures.add("after_manifest")
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        connection.execute("UPDATE cursors SET last_seen_id='92'")
        connection.commit()
        with pytest.raises(CloudStateError, match="commit failed"):
            state.checkpoint(connection)
    assert json.loads(container.data["production/manifest.json"][0])["bundle"] != original["bundle"]
    with pytest.raises(CloudStateError):
        state.check_lease()


def test_etag_change_rejects_stale_manifest_commit(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    state.restore()
    payload, _ = container.data["production/manifest.json"]
    container.data["production/manifest.json"] = payload, "unexpected-etag"
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        connection.execute("UPDATE cursors SET last_seen_id='92'")
        connection.commit()
        with pytest.raises(CloudStateError, match="commit failed"):
            state.checkpoint(connection)


@pytest.mark.parametrize("corruption", ["manifest", "bundle", "missing_bundle"])
def test_corrupt_cloud_state_does_not_install_or_create_database(tmp_path, source, corruption):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    manifest = bootstrap(state, source)
    if corruption == "manifest":
        container.data["production/manifest.json"] = b"{}", "etag-corrupt"
    elif corruption == "bundle":
        container.data[manifest["bundle"]["name"]] = b"corrupt", "etag-corrupt"
    else:
        del container.data[manifest["bundle"]["name"]]
    state.acquire()
    with pytest.raises(CloudStateError):
        state.restore()
    assert not (state.data_dir / "state.db").exists()


def test_checkpoint_refuses_uncommitted_connection(tmp_path, source):
    state = make_state(FakeContainer(), tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    state.restore()
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        connection.execute("UPDATE cursors SET last_seen_id = '99'")
        with pytest.raises(CloudStateError, match="uncommitted"):
            state.checkpoint(connection)


def test_daily_backups_reference_aware_pruning_and_namespace_isolation(tmp_path, source):
    container = FakeContainer()
    today = datetime.now(UTC).replace(hour=4, minute=15)
    date = [today]
    state = make_state(container, tmp_path / "data", utc_now=lambda: date[0])
    bootstrap(state, source)
    state.acquire()
    state.restore()
    container.data["other/bundles/untouched.tar.gz"] = b"other", "other"
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        for day in range(9):
            date[0] = today + timedelta(days=day)
            state.checkpoint(connection, daily_backup=True)
            state.checkpoint(connection)
    container.data["production/bundles/orphan.tar.gz"] = b"orphan", "orphan"
    assert state.prune() > 0
    manifest = json.loads(container.data["production/manifest.json"][0])
    keep = {manifest["bundle"]["name"], manifest["previous_bundle"]["name"]}
    keep.update(backup["bundle"]["name"] for backup in manifest["daily_backups"])
    assert len(manifest["daily_backups"]) == 7
    assert {name for name in container.data if name.startswith("production/bundles/")} == keep
    assert "other/bundles/untouched.tar.gz" in container.data


def test_checkpoint_bounds_history_without_daily_sweep(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    state.restore()
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        for index in range(12):
            connection.execute("UPDATE cursors SET last_seen_id=?", (str(index),))
            connection.commit()
            state.checkpoint(connection)
    # Current, previous, and the bootstrap's daily backup; normal checkpoints
    # do not accumulate full databases even if the daily maintenance job fails.
    assert len([name for name in container.data if "/bundles/" in name]) == 3


def test_cleanup_failure_does_not_discard_successful_commit(tmp_path, source, caplog):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    state.restore()
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        state.checkpoint(connection)
        connection.execute("UPDATE cursors SET last_seen_id='92'")
        connection.commit()
        state.checkpoint(connection)
        connection.execute("UPDATE cursors SET last_seen_id='93'")
        connection.commit()
        state.checkpoint(connection)
        connection.execute("UPDATE cursors SET last_seen_id='94'")
        connection.commit()
        container.failures.add("delete")
        current = state.checkpoint(connection)
    assert json.loads(container.data["production/manifest.json"][0]) == current
    state.check_lease()
    assert "cleanup deferred" in caplog.text
    container.failures.clear()
    assert state.prune() == 1


def test_archive_retention_keeps_year_and_mtime(tmp_path, source):
    now = datetime.now(UTC)
    old = source / "archive" / "expired.md"
    old.write_text("Expired archive")
    old_age = (now - timedelta(days=366)).timestamp()
    os.utime(old, (old_age, old_age))
    preserved = source / "archive" / "2026" / "digest.md"
    preserved_age = (now - timedelta(days=300)).timestamp()
    os.utime(preserved, (preserved_age, preserved_age))
    state = make_state(FakeContainer(), tmp_path / "data", utc_now=lambda: now)
    bootstrap(state, source)
    state.acquire()
    state.restore()
    assert not (state.data_dir / "archive" / "expired.md").exists()
    assert (state.data_dir / "archive" / "2026" / "digest.md").stat().st_mtime == preserved_age


def test_export_and_restore_into_separate_namespace(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    manifest = bootstrap(state, source)
    state.acquire()
    state.export(tmp_path / "export", backup_date=manifest["daily_backups"][0]["date"])
    with pytest.raises(CloudStateError, match="must be empty"):
        state.export(tmp_path / "export")
    state.release()
    recovered = make_state(container, tmp_path / "restored", namespace="restore-test")
    recovered.bootstrap_export(tmp_path / "export")
    recovered.acquire()
    recovered.restore()
    with sqlite3.connect(recovered.data_dir / "state.db") as connection:
        assert connection.execute("SELECT last_seen_id FROM cursors").fetchone()[0] == "91"
    assert (recovered.data_dir / "x-cookies.json.live").read_bytes() == (
        source / "x-cookies.json.live"
    ).read_bytes()
    assert (recovered.data_dir / "archive" / "2026" / "digest.md").exists()


@pytest.mark.parametrize("member_type", ["traversal", "symlink", "duplicate"])
def test_hash_valid_but_unsafe_tar_is_rejected(tmp_path, source, member_type):
    import hashlib

    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    manifest = bootstrap(state, source)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        name = "../escape" if member_type == "traversal" else "state.db"
        info = tarfile.TarInfo(name)
        if member_type == "symlink":
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(info, io.BytesIO())
        if member_type == "duplicate":
            tar.addfile(info, io.BytesIO())
    raw = payload.getvalue()
    reference = manifest["bundle"]
    reference["size"], reference["sha256"] = len(raw), hashlib.sha256(raw).hexdigest()
    container.data[reference["name"]] = raw, "etag-malicious"
    container.data["production/manifest.json"] = json.dumps(manifest).encode(), "etag-malicious"
    state.acquire()
    with pytest.raises(CloudStateError, match="contents are invalid"):
        state.restore()
    assert not (tmp_path / "escape").exists()
    assert not (state.data_dir / "state.db").exists()


def test_invalid_export_hash_cannot_bootstrap_namespace(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    state.export(tmp_path / "export")
    (tmp_path / "export" / "bundle.tar.gz").write_bytes(b"corruption")
    destination = make_state(container, tmp_path / "restore", namespace="restore-test")
    with pytest.raises(CloudStateError, match="import refused"):
        destination.bootstrap_export(tmp_path / "export")
    assert "restore-test/manifest.json" not in container.data


def run_azurite_smoke() -> None:
    """SDK protocol smoke invoked by test_cloud_state_azurite with --azurite.

    Requires Azurite bound only to 127.0.0.1:10000. Its documented public
    development credential never authenticates to an Azure production account.
    Each invocation creates and finally deletes its own unique test container.
    """
    from azure.storage.blob import ContainerClient

    client = ContainerClient.from_connection_string(
        "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
        "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
        "K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;",
        f"digest-state-test-{uuid4().hex}",
        retry_total=0,
        connection_timeout=5,
        read_timeout=10,
        api_version="2023-11-03",
    )
    client.create_container()
    try:
        with tempfile.TemporaryDirectory(prefix="digest-azurite-test-") as directory:
            root = Path(directory)
            imported = source.__wrapped__(root)
            state = CloudState(client, data_dir=root / "data")
            initial = bootstrap(state, imported)
            state.acquire()
            state.restore()
            contender = CloudState(client, data_dir=root / "other", lease_wait_seconds=0)
            with pytest.raises(CloudStateError, match="budget"):
                contender.acquire()
            state.renew()
            with sqlite3.connect(state.data_dir / "state.db") as connection:
                connection.execute("UPDATE cursors SET last_seen_id = '100'")
                connection.commit()
                current = state.checkpoint(connection, daily_backup=True)
            assert initial["bundle"] != current["bundle"]
            state.export(root / "export")
            state.release()
            recovery = CloudState(client, namespace="restore-test", data_dir=root / "recovery")
            recovery.bootstrap_export(root / "export")
            recovery.acquire()
            recovery.restore()
            with sqlite3.connect(recovery.data_dir / "state.db") as connection:
                assert connection.execute("SELECT last_seen_id FROM cursors").fetchone()[0] == "100"
            recovery.release()
            state.acquire()
            state.restore()
            manifest_blob = client.get_blob_client("production/manifest.json")
            payload = manifest_blob.download_blob(lease=state._lease).readall()
            manifest_blob.upload_blob(payload, overwrite=True, lease=state._lease)
            with sqlite3.connect(state.data_dir / "state.db") as connection:
                connection.execute("UPDATE cursors SET last_seen_id = '101'")
                connection.commit()
                with pytest.raises(CloudStateError, match="commit failed"):
                    state.checkpoint(connection)
            state.release()
            state.acquire()
            state.restore()
            assert state.prune() >= 1
            state.release()
    finally:
        client.delete_container()
        client.close()
    print("Azurite SDK protocol smoke passed: lease exclusion, renew, ETag, snapshot, restore, GC")


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_lease_renewal_recovers_without_optimistic_extension(tmp_path, source, status):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    calls = []
    original = state._lease.renew

    def renew():
        calls.append(state.lease_deadline)
        if len(calls) == 1:
            raise StorageError(status)
        original()

    state._lease.renew = renew
    container.seconds = 15
    state.renew()
    assert calls == [60, 60]
    # Request start is conservative even though retry succeeds at 15.5.
    assert state.lease_deadline == 75
    assert container.seconds == 15.5


@pytest.mark.parametrize("status", [401, 403, 404, 409, 412])
def test_known_lost_or_unauthorized_lease_never_retries(tmp_path, source, status):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    from unittest.mock import Mock

    state._lease.renew = Mock(side_effect=StorageError(status))
    with pytest.raises(CloudStateError, match="renewal failed"):
        state.renew()
    state._lease.renew.assert_called_once()
    assert state.lease_deadline == 60
    with pytest.raises(CloudStateError):
        state.check_lease()


def test_transient_renewal_at_safety_deadline_is_not_retried(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    from unittest.mock import Mock

    state._lease.renew = Mock(side_effect=StorageError(503))
    container.seconds = 49.75
    with pytest.raises(CloudStateError, match="renewal failed"):
        state.renew()
    state._lease.renew.assert_called_once()
    assert container.seconds == 49.75
    assert state.lease_deadline == 60


def test_manifest_and_bundle_reads_retry_transient_transport_failures(tmp_path, source):
    from azure.core.exceptions import ServiceResponseError

    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    original = container.fail
    attempts = []

    def fail_once(point):
        if point == "download":
            attempts.append(point)
            if len(attempts) in (1, 3):
                raise ServiceResponseError("mock response truncated")
        original(point)

    container.fail = fail_once
    state.restore()
    assert len(attempts) == 4
    assert container.seconds == 1
    assert (state.data_dir / "state.db").exists()


def test_unchanged_checkpoint_skips_upload_but_every_material_change_persists(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    state.restore()
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        original = state.checkpoint(connection)
        writes = container.counter
        assert state.checkpoint(connection) is original
        assert container.counter == writes
        for change in ("cursor", "cookie", "cookie_age", "archive", "archive_age"):
            if change == "cursor":
                connection.execute("UPDATE cursors SET last_seen_id='92'")
                connection.commit()
            elif change == "cookie":
                (state.data_dir / "x-cookies.json.live").write_text('{"auth_token":"new"}')
            elif change == "cookie_age":
                os.utime(state.data_dir / "x-cookies.json.live", (300, 300))
            elif change == "archive":
                (state.data_dir / "archive" / "new.md").write_text("new")
            else:
                os.utime(state.data_dir / "archive" / "new.md", (400, 400))
            updated = state.checkpoint(connection)
            assert updated["bundle"] != original["bundle"]
            original = updated
        writes = container.counter
        state.checkpoint(connection, daily_backup=True)
        assert container.counter > writes


def test_lease_busy_error_is_distinct_from_missing_state(tmp_path, source):
    container = FakeContainer()
    owner = make_state(container, tmp_path / "first")
    bootstrap(owner, source)
    owner.acquire()
    contender = make_state(container, tmp_path / "second", lease_wait_seconds=0)
    with pytest.raises(LeaseBusyError):
        contender.acquire()
    missing = make_state(container, tmp_path / "missing", namespace="absent")
    with pytest.raises(CloudStateError) as error:
        missing.acquire()
    assert not isinstance(error.value, LeaseBusyError)


def test_renewal_recovers_after_more_than_three_transient_failures(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    count = 0
    original = state._lease.renew

    def renew():
        nonlocal count
        count += 1
        assert state.lease_deadline == 60
        if count <= 4:
            raise StorageError(503)
        original()

    state._lease.renew = renew
    container.seconds = 15
    state.renew()
    assert count == 5
    assert state.lease_deadline == 75
    assert container.seconds == 20.5


def test_restored_unchanged_state_reuses_authoritative_bundle_without_upload(tmp_path, source):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    original = bootstrap(state, source)
    state.acquire()
    state.restore()
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        writes = container.counter
        state.checkpoint(connection)
        assert container.counter == writes
    state.release()
    other = make_state(container, tmp_path / "other")
    other.acquire()
    other.restore()
    with sqlite3.connect(other.data_dir / "state.db") as connection:
        assert other.checkpoint(connection) == original
        assert container.counter == writes
    other.release()


@pytest.mark.parametrize("operation", ["acquire", "bundle", "after_bundle"])
def test_transient_acquire_and_immutable_upload_ack_loss_recover(tmp_path, source, operation):
    container = FakeContainer()
    state = make_state(container, tmp_path / "data")
    bootstrap(state, source)
    original = container.fail
    count = 0

    def fail_once(point):
        nonlocal count
        if point == operation:
            count += 1
            if count == 1:
                raise StorageError(503)
        original(point)

    container.fail = fail_once
    state.acquire()
    state.restore()
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        connection.execute("UPDATE cursors SET last_seen_id='92'")
        connection.commit()
        current = state.checkpoint(connection)
    assert count == 2
    assert json.loads(container.data["production/manifest.json"][0]) == current
    state.restore()
    with sqlite3.connect(state.data_dir / "state.db") as connection:
        assert connection.execute("SELECT last_seen_id FROM cursors").fetchone()[0] == "92"
    if operation == "after_bundle":
        assert state.prune() == 1
    state.release()


def test_restore_live_only_cookie_state_normalizes_seed_for_collector(tmp_path, source):
    from digest.collectors.x import resolve_cookie_path

    (source / "x-cookies.json").unlink()
    state = make_state(FakeContainer(), tmp_path / "data")
    bootstrap(state, source)
    state.acquire()
    state.restore()
    seed = state.data_dir / "x-cookies.json"
    live = state.data_dir / "x-cookies.json.live"
    assert seed.read_bytes() == live.read_bytes()
    assert resolve_cookie_path(str(seed)) == str(live)
    state.release()
