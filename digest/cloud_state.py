"""Authoritative SQLite bundles in private Blob Storage, guarded by one lease.

The manifest is the only mutable pointer. Bundle uploads are immutable and only
become authoritative after a conditional manifest update. Ordinary execution
cannot create missing state; bootstrap is a separate, explicit operation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

LEASE_SECONDS = 60
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


class CloudStateError(RuntimeError):
    """State cannot safely be used or published; callers must stop execution."""


class LeaseBusyError(CloudStateError):
    """Another healthy writer held the lease throughout the wait budget."""


class CloudState:
    """One execution's lease, manifest and ephemeral local state directory.

    ``container_client`` is an Azure ContainerClient, or an injected equivalent
    in offline tests. Only the runner renews the lease; it must fence the process
    before ``lease_deadline`` when renewal cannot be established.
    """

    def __init__(
        self,
        container_client: Any,
        *,
        namespace: str = "production",
        data_dir: str | Path = "/data",
        lease_wait_seconds: float = 600,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        parts = PurePosixPath(namespace).parts
        if not parts or any(part in (".", "..", "/") for part in parts):
            raise ValueError("Cloud state namespace must be a relative blob prefix")
        if "\\" in namespace or namespace.startswith("/"):
            raise ValueError("Cloud state namespace must be a relative blob prefix")
        if not 0 <= lease_wait_seconds <= 600:
            raise ValueError("Cloud lease wait must be between zero and 600 seconds")
        self.container = container_client
        self.namespace = namespace.rstrip("/")
        self.data_dir = Path(data_dir)
        self.lease_wait_seconds = lease_wait_seconds
        self._clock, self._sleep, self._utc_now = clock, sleep, utc_now
        self._manifest_blob = self.container.get_blob_client(f"{self.namespace}/manifest.json")
        self._lease: Any = None
        self._etag: str | None = None
        self._manifest: dict[str, Any] | None = None
        self.lease_deadline = 0.0
        self._fenced = False
        self._content_digest: str | None = None

    @classmethod
    def from_account_url(
        cls,
        account_url: str,
        container: str,
        *,
        namespace: str = "production",
        data_dir: str | Path = "/data",
        managed_identity_client_id: str | None = None,
        lease_wait_seconds: float = 600,
        operator_login: bool = False,
    ) -> CloudState:
        """Use managed identity; operator CLI access requires an explicit flag."""
        from azure.identity import AzureCliCredential, ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        credential = (
            AzureCliCredential()
            if operator_login
            else ManagedIdentityCredential(client_id=managed_identity_client_id)
        )
        service = BlobServiceClient(
            account_url,
            credential=credential,
            retry_total=0,
            connection_timeout=5,
            read_timeout=10,
        )
        return cls(
            service.get_container_client(container),
            namespace=namespace,
            data_dir=data_dir,
            lease_wait_seconds=lease_wait_seconds,
        )

    def acquire(self) -> None:
        """Wait a bounded time for existing state; missing state fails closed."""
        if self._lease is not None:
            raise CloudStateError("Cloud state lease is already held")
        stop_at = self._clock() + self.lease_wait_seconds
        transient_attempt = 0
        while True:
            started = self._clock()
            try:
                lease = self._manifest_blob.acquire_lease(lease_duration=LEASE_SECONDS)
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    raise CloudStateError("Cloud state is missing; bootstrap is required") from None
                if _transient_storage_error(exc) and transient_attempt < 2:
                    delay = 0.5 * (2**transient_attempt)
                    if self._clock() + delay < stop_at:
                        self._sleep(delay)
                        transient_attempt += 1
                        continue
                if (
                    getattr(exc, "status_code", None) != 409
                    or getattr(exc, "error_code", None) != "LeaseAlreadyPresent"
                ):
                    raise CloudStateError("Cloud state lease acquisition failed") from None
                remaining = stop_at - self._clock()
                if remaining <= 0:
                    raise LeaseBusyError("Cloud state lease wait exceeded its budget") from None
                self._sleep(min(5, remaining))
                continue
            self._lease = lease
            # Lease starts at the server during this call. Using request start
            # conservatively avoids adding network latency to the safe lifetime.
            self.lease_deadline = started + LEASE_SECONDS
            self._fenced = False
            self.check_lease()
            return

    def check_lease(self) -> None:
        """Reject new effects without a known lease and a five-second margin."""
        if self._fenced or self._lease is None or self._clock() >= self.lease_deadline - 5:
            self._fenced = True
            raise CloudStateError("Cloud state lease cannot be proven valid")

    def _retry_read_or_renew(self, operation: Callable[[], Any], *, renewing: bool = False) -> Any:
        """Retry only idempotent effects inside the existing proven lease."""
        attempt = 0
        while True:
            self.check_lease()
            if self._clock() >= self.lease_deadline - 10:
                raise CloudStateError("Cloud state lease retry has no safe time remaining")
            try:
                result = operation()
                self.check_lease()
                return result
            except Exception as exc:
                delay = 0.5 * (2 ** min(attempt, 2))
                if (
                    (not renewing and attempt == 2)
                    or not _transient_storage_error(exc)
                    or self._clock() + delay >= self.lease_deadline - 10
                ):
                    raise
                self._sleep(delay)
                attempt += 1

    def renew(self) -> None:
        """Retry transient renewal failures without extending the old proven deadline."""
        self.check_lease()
        started = self._clock()
        previous_deadline = self.lease_deadline
        try:
            self._retry_read_or_renew(self._lease.renew, renewing=True)
        except Exception:
            self._fenced = True
            raise CloudStateError("Cloud state lease renewal failed") from None
        if self._clock() >= previous_deadline - 10 or self._fenced:
            self._fenced = True
            raise CloudStateError("Cloud state lease renewal completed too late")
        self.lease_deadline = started + LEASE_SECONDS

    def release(self) -> None:
        """Release on normal completion; failure cannot make an expired lease safe."""
        lease, self._lease = self._lease, None
        self.lease_deadline = 0
        if lease is not None:
            try:
                lease.release()
            except Exception:
                raise CloudStateError("Cloud state lease release failed") from None

    def _read_manifest(self) -> dict[str, Any]:
        self.check_lease()
        try:

            def read() -> tuple[bytes, str]:
                download = self._manifest_blob.download_blob(
                    lease=self._lease, offset=0, length=MAX_MANIFEST_BYTES + 1
                )
                return download.readall(), download.properties.etag

            raw, etag = self._retry_read_or_renew(read)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ValueError("Oversized manifest")
            manifest = json.loads(raw)
            self._validate_manifest(manifest)
        except Exception:
            raise CloudStateError("Cloud state manifest is missing or invalid") from None
        self.check_lease()
        self._etag, self._manifest = etag, manifest
        return manifest

    def _validate_bundle_reference(self, reference: Any) -> None:
        if not isinstance(reference, dict):
            raise ValueError("Invalid bundle reference")
        name = reference.get("name", "")
        prefix = f"{self.namespace}/bundles/"
        if (
            not isinstance(name, str)
            or not name.startswith(prefix)
            or "/" in name[len(prefix) :]
            or not name.endswith(".tar.gz")
        ):
            raise ValueError("Invalid bundle name")
        digest = reference.get("sha256", "")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("Invalid bundle hash")
        int(digest, 16)
        size = reference.get("size")
        if type(size) is not int or not 0 < size <= MAX_BUNDLE_BYTES:
            raise ValueError("Invalid bundle size")

    def _validate_manifest(self, manifest: Any) -> None:
        if not isinstance(manifest, dict) or manifest.get("schema") != 1:
            raise ValueError("Unsupported state manifest")
        self._validate_bundle_reference(manifest.get("bundle"))
        datetime.fromisoformat(manifest["created_at"])
        backups = manifest.get("daily_backups", [])
        if not isinstance(backups, list) or len(backups) > 7:
            raise ValueError("Invalid backup references")
        for backup in backups:
            self._validate_bundle_reference(backup["bundle"])
            datetime.fromisoformat(backup["date"])
        if manifest.get("previous_bundle") is not None:
            self._validate_bundle_reference(manifest["previous_bundle"])

    def _download_bundle(self, reference: dict[str, Any], destination: Path) -> None:
        self.check_lease()
        try:
            blob = self.container.get_blob_client(reference["name"])

            def download() -> None:
                # Reopen/truncate on retry: a partial response must never append.
                with destination.open("wb") as output:
                    blob.download_blob(offset=0, length=reference["size"] + 1).readinto(output)

            self._retry_read_or_renew(download)
            destination.chmod(0o600)
            if destination.stat().st_size != reference["size"]:
                raise ValueError("Bundle length differs")
            if _sha256(destination) != reference["sha256"]:
                raise ValueError("Bundle hash differs")
        except Exception:
            raise CloudStateError("Cloud state bundle is missing or corrupt") from None
        self.check_lease()

    def restore(self) -> dict[str, Any]:
        """Validate the whole bundle before installing local SQLite/cookies/archive."""
        manifest = self._read_manifest()
        self._content_digest = None
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        with tempfile.TemporaryDirectory(prefix=".state-", dir=self.data_dir) as temp:
            staging = Path(temp)
            bundle = staging / "bundle.tar.gz"
            self._download_bundle(manifest["bundle"], bundle)
            extracted = staging / "extracted"
            extracted.mkdir(mode=0o700)
            try:
                _extract_bundle(bundle, extracted)
                _validate_database(extracted / "state.db")
                content_digest = _bundle_content_digest(bundle)
                # Legacy imports may contain only a live jar. The collector's
                # resolver requires the seed to exist even when the live wins.
                seed = extracted / "x-cookies.json"
                live = extracted / "x-cookies.json.live"
                if live.exists() and not seed.exists():
                    shutil.copy2(live, seed)
                    seed.chmod(0o600)
            except Exception:
                raise CloudStateError("Cloud state bundle contents are invalid") from None
            self.check_lease()
            # The runner must restore before opening any SQLite connection.
            for name in (
                "state.db",
                "state.db-wal",
                "state.db-shm",
                "x-cookies.json",
                "x-cookies.json.live",
            ):
                (self.data_dir / name).unlink(missing_ok=True)
            archive = self.data_dir / "archive"
            if archive.exists():
                shutil.rmtree(archive)
            for name in ("state.db", "archive", "x-cookies.json", "x-cookies.json.live"):
                source = extracted / name
                if source.exists():
                    source.replace(self.data_dir / name)
        self._content_digest = content_digest
        return manifest

    def checkpoint(
        self, connection: sqlite3.Connection, *, daily_backup: bool = False
    ) -> dict[str, Any]:
        """Publish an online SQLite snapshot and consistent live collector files."""
        self.check_lease()
        if self._manifest is None or self._etag is None:
            raise CloudStateError("Cloud state must be restored before checkpointing")
        if connection.in_transaction:
            raise CloudStateError("Cannot checkpoint an uncommitted SQLite transaction")
        now = self._utc_now().astimezone(UTC)
        with tempfile.TemporaryDirectory(prefix=".checkpoint-", dir=self.data_dir) as temp:
            bundle = self._build_bundle(connection, Path(temp), now)
            content_digest = _bundle_content_digest(bundle)
            if not daily_backup and content_digest == self._content_digest:
                # Guards still run before returning. Any SQLite/cookie/archive
                # change changes this digest, including pending delivery intents.
                self.check_lease()
                return self._manifest
            reference = self._upload_bundle(bundle)
        backups = [
            backup
            for backup in self._manifest.get("daily_backups", [])
            if now.date() - timedelta(days=6)
            <= datetime.fromisoformat(backup["date"]).date()
            <= now.date()
        ]
        if daily_backup:
            backups = [backup for backup in backups if backup["date"] != now.date().isoformat()]
            backups.append({"date": now.date().isoformat(), "bundle": reference})
        manifest = {
            "schema": 1,
            "created_at": now.isoformat(),
            "bundle": reference,
            "previous_bundle": self._manifest["bundle"],
            "daily_backups": backups,
        }
        previous_manifest = self._manifest
        self._publish_manifest(manifest)
        self._content_digest = content_digest
        self._discard_unreferenced(previous_manifest, manifest)
        return manifest

    def _discard_unreferenced(self, previous: dict[str, Any], current: dict[str, Any]) -> None:
        # Bound normal history without listing every blob at each checkpoint.
        # Failed upload/cleanup orphans are swept by the daily backup job.
        for name in _referenced_bundles(previous) - _referenced_bundles(current):
            self.check_lease()
            try:
                self.container.get_blob_client(name).delete_blob()
            except Exception:
                # The state commit has succeeded. A cleanup failure must not
                # turn a confirmed delivery into an uncertain publication.
                logger.warning("Cloud state bundle cleanup deferred to daily maintenance")

    def _build_bundle(
        self,
        connection: sqlite3.Connection,
        staging: Path,
        now: datetime,
        *,
        archive_dir: Path | None = None,
        live_cookies_path: Path | None = None,
    ) -> Path:
        snapshot = staging / "state.db"
        with closing(sqlite3.connect(snapshot)) as target:
            connection.backup(target)
        snapshot.chmod(0o600)
        _validate_database(snapshot)
        bundle = staging / "bundle.tar.gz"
        archive_dir = archive_dir if archive_dir is not None else self.data_dir / "archive"
        live_cookies_path = (
            live_cookies_path
            if live_cookies_path is not None
            else self.data_dir / "x-cookies.json.live"
        )
        cutoff = now.timestamp() - timedelta(days=365).total_seconds()
        with tarfile.open(bundle, "w:gz", compresslevel=1) as tar:
            tar.add(snapshot, arcname="state.db", recursive=False)
            seed_path = live_cookies_path.with_name(live_cookies_path.name.removesuffix(".live"))
            for cookie_path, name in (
                (seed_path, "x-cookies.json"),
                (live_cookies_path, "x-cookies.json.live"),
            ):
                if not cookie_path.exists():
                    continue
                if cookie_path.is_symlink() or not cookie_path.is_file():
                    raise CloudStateError("Cookie state must be a regular file")
                # Validate JSON without including its contents in failures.
                try:
                    json.loads(cookie_path.read_bytes())
                except (ValueError, OSError):
                    raise CloudStateError("Cookie state is invalid") from None
                tar.add(cookie_path, arcname=name, recursive=False)
            if archive_dir.exists():
                if archive_dir.is_symlink() or not archive_dir.is_dir():
                    raise CloudStateError("Archive state must be a real directory")
                for path in sorted(archive_dir.rglob("*")):
                    if path.is_symlink():
                        raise CloudStateError("Archive state cannot contain symbolic links")
                    if path.is_file() and path.stat().st_mtime >= cutoff:
                        tar.add(path, arcname=f"archive/{path.relative_to(archive_dir).as_posix()}")
        bundle.chmod(0o600)
        if bundle.stat().st_size > MAX_BUNDLE_BYTES:
            raise CloudStateError("Cloud state bundle exceeds the supported size")
        return bundle

    def _upload_bundle(self, bundle: Path) -> dict[str, Any]:
        self.check_lease()
        digest, size = _sha256(bundle), bundle.stat().st_size

        def upload() -> dict[str, Any]:
            # Each retry gets a fresh immutable name. An upload whose response
            # was lost may have succeeded; never overwrite or trust that object.
            name = f"{self.namespace}/bundles/{uuid4().hex}.tar.gz"
            with bundle.open("rb") as source:
                self.container.get_blob_client(name).upload_blob(source, overwrite=False)
            return {"name": name, "sha256": digest, "size": size}

        try:
            return self._retry_read_or_renew(upload)
        except Exception:
            raise CloudStateError("Cloud state bundle upload failed") from None

    def _publish_manifest(self, manifest: dict[str, Any]) -> None:
        from azure.core import MatchConditions

        self.check_lease()
        try:
            result = self._manifest_blob.upload_blob(
                json.dumps(manifest, sort_keys=True).encode(),
                overwrite=True,
                lease=self._lease,
                etag=self._etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except Exception:
            self._fenced = True
            raise CloudStateError(
                "Cloud state manifest commit failed; execution must stop"
            ) from None
        self.check_lease()
        self._etag, self._manifest = result["etag"], manifest

    def bootstrap(
        self,
        db_path: str | Path,
        *,
        archive_dir: str | Path | None = None,
        live_cookies_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Explicitly import existing state into an absent, isolated namespace.

        No source database is created or migrated. The caller must first drain
        its previous writer and verify source lineage. Existing manifests cannot
        be overwritten, even if the caller supplies the same source database.
        """
        source_path = Path(db_path).resolve()
        _validate_database(source_path)
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        now = self._utc_now().astimezone(UTC)
        with closing(sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)) as connection:
            with tempfile.TemporaryDirectory(prefix=".bootstrap-", dir=self.data_dir) as temp:
                bundle = self._build_bundle(
                    connection,
                    Path(temp),
                    now,
                    archive_dir=Path(archive_dir) if archive_dir is not None else None,
                    live_cookies_path=(
                        Path(live_cookies_path) if live_cookies_path is not None else None
                    ),
                )
                # Initial immutable upload does not require a lease: publication
                # below atomically creates the absent manifest, without replacing.
                reference = {
                    "name": f"{self.namespace}/bundles/{uuid4().hex}.tar.gz",
                    "sha256": _sha256(bundle),
                    "size": bundle.stat().st_size,
                }
                try:
                    with bundle.open("rb") as payload:
                        self.container.get_blob_client(reference["name"]).upload_blob(
                            payload, overwrite=False
                        )
                    manifest = {
                        "schema": 1,
                        "created_at": now.isoformat(),
                        "bundle": reference,
                        "previous_bundle": None,
                        "daily_backups": [{"date": now.date().isoformat(), "bundle": reference}],
                    }
                    self._manifest_blob.upload_blob(json.dumps(manifest).encode(), overwrite=False)
                except Exception:
                    raise CloudStateError(
                        "Bootstrap failed or cloud state already exists"
                    ) from None
        return manifest

    def export(self, destination: str | Path, *, backup_date: str | None = None) -> dict[str, Any]:
        """Export a validated bundle plus manifest without changing cloud state."""
        manifest = self._read_manifest()
        if backup_date is not None:
            matches = [
                backup
                for backup in manifest.get("daily_backups", [])
                if backup["date"] == backup_date
            ]
            if len(matches) != 1:
                raise CloudStateError("Requested daily backup is not retained")
            manifest = {**manifest, "bundle": matches[0]["bundle"], "backup_date": backup_date}
        destination = Path(destination)
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.chmod(0o700)
        if any(destination.iterdir()):
            raise CloudStateError("State export destination must be empty")
        bundle = destination / "bundle.tar.gz"
        self._download_bundle(manifest["bundle"], bundle)
        with tempfile.TemporaryDirectory(prefix=".verify-", dir=destination) as temp:
            try:
                _extract_bundle(bundle, Path(temp))
                _validate_database(Path(temp) / "state.db")
            except Exception:
                raise CloudStateError("Cloud state export contents are invalid") from None
        manifest_file = destination / "manifest.json"
        manifest_file.write_text(json.dumps(manifest, indent=2) + "\n")
        manifest_file.chmod(0o600)
        return manifest

    def bootstrap_export(self, source_dir: str | Path) -> dict[str, Any]:
        """Validate an exported bundle and import it into a new namespace."""
        source_dir = Path(source_dir)
        try:
            raw = (source_dir / "manifest.json").read_bytes()
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ValueError("Manifest is too large")
            reference = json.loads(raw)["bundle"]
            bundle = source_dir / "bundle.tar.gz"
            if (
                bundle.is_symlink()
                or not bundle.is_file()
                or type(reference["size"]) is not int
                or not 0 < reference["size"] <= MAX_BUNDLE_BYTES
                or bundle.stat().st_size != reference["size"]
                or _sha256(bundle) != reference["sha256"]
            ):
                raise ValueError("Export is invalid")
            with tempfile.TemporaryDirectory(prefix="digest-import-") as temp:
                staging = Path(temp)
                _extract_bundle(bundle, staging)
                _validate_database(staging / "state.db")
                return self.bootstrap(
                    staging / "state.db",
                    archive_dir=staging / "archive",
                    live_cookies_path=staging / "x-cookies.json.live",
                )
        except CloudStateError:
            raise
        except Exception:
            raise CloudStateError("State export is invalid; import refused") from None

    def prune(self) -> int:
        """Delete only unreferenced bundles while holding the canonical lease.

        The current, previous and seven retained daily generations survive. No
        plain age-based lifecycle may delete the bundles prefix independently.
        This is run by the daily backup job, avoiding per-checkpoint list costs.
        """
        manifest = self._read_manifest()
        keep = _referenced_bundles(manifest)
        deleted = 0
        try:
            blobs = self.container.list_blobs(name_starts_with=f"{self.namespace}/bundles/")
            for blob in blobs:
                self.check_lease()
                if blob.name not in keep:
                    self.container.get_blob_client(blob.name).delete_blob()
                    deleted += 1
        except CloudStateError:
            raise
        except Exception:
            raise CloudStateError("Cloud state bundle cleanup failed") from None
        return deleted


def _transient_storage_error(exc: Exception) -> bool:
    from azure.core.exceptions import ServiceRequestError, ServiceResponseError

    # Authorization, missing/corrupt state and 409/412 lease loss never retry.
    status = getattr(exc, "status_code", None)
    if status is not None:
        return status in (408, 429, 500, 502, 503, 504)
    return isinstance(exc, (ServiceRequestError, ServiceResponseError))


def _bundle_content_digest(bundle: Path) -> str:
    """Compare logical files, excluding incidental gzip and SQLite snapshot times."""
    digest = hashlib.sha256()
    with tarfile.open(bundle, "r:gz") as tar:
        for member in tar:
            if not member.isfile():
                continue
            digest.update(json.dumps([member.name, member.size]).encode())
            if member.name != "state.db":
                # Cookie precedence and archive retention depend on file age.
                digest.update(repr(member.mtime).encode())
            content = tar.extractfile(member)
            if content is None:
                raise CloudStateError("Cloud bundle member is unreadable")
            with content:
                for chunk in iter(lambda stream=content: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _referenced_bundles(manifest: dict[str, Any]) -> set[str]:
    keep = {manifest["bundle"]["name"]}
    if manifest.get("previous_bundle"):
        keep.add(manifest["previous_bundle"]["name"])
    keep.update(backup["bundle"]["name"] for backup in manifest.get("daily_backups", []))
    return keep


def _validate_database(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise CloudStateError("An existing regular SQLite database is required")
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("Invalid SQLite database")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
            if not {"items", "digests", "cursors"}.issubset(tables):
                raise ValueError("Not digest state")
    except Exception:
        raise CloudStateError("SQLite state is corrupt or is not a digest database") from None


def _extract_bundle(bundle: Path, destination: Path) -> None:
    """Extract only expected regular files; reject links, traversal and bombs."""
    total = 0
    seen: set[str] = set()
    with tarfile.open(bundle, "r:gz") as tar:
        for member in tar:
            name = PurePosixPath(member.name)
            if (
                member.name in seen
                or member.name != name.as_posix()
                or member.name.startswith("/")
                or ".." in name.parts
                or "\\" in member.name
                or not member.isfile()
                or (
                    member.name not in ("state.db", "x-cookies.json", "x-cookies.json.live")
                    and not member.name.startswith("archive/")
                )
            ):
                raise ValueError("Unexpected bundle member")
            seen.add(member.name)
            total += member.size
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("Bundle is too large")
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            for parent in target.parents:
                parent.chmod(0o700)
                if parent == destination:
                    break
            source = tar.extractfile(member)
            if source is None:
                raise ValueError("Bundle member is unreadable")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o600)
            # Preserve archive retention age and seed/live cookie ordering.
            os.utime(target, (member.mtime, member.mtime))
    if "state.db" not in seen:
        raise ValueError("Bundle lacks authoritative SQLite state")
    for name in ("x-cookies.json", "x-cookies.json.live"):
        cookies = destination / name
        if cookies.exists():
            json.loads(cookies.read_bytes())


def main() -> int:
    """Explicit state bootstrap/export commands; ordinary jobs use cloud_run."""
    import argparse
    import signal

    from digest.cloud_run import LeaseWatchdog, _fence_process_group
    from digest.config import CloudConfig

    parser = argparse.ArgumentParser(description="Import/export authoritative digest cloud state")
    parser.add_argument(
        "--operator-login",
        action="store_true",
        help="Explicitly use the operator's az login instead of managed identity",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap", help="Import into an absent cloud namespace")
    bootstrap.add_argument("source_dir", type=Path)
    export = commands.add_parser("export", help="Export current state or a retained daily backup")
    export.add_argument("destination", type=Path)
    export.add_argument("--backup-date")
    args = parser.parse_args()
    cfg = CloudConfig.from_env()
    # Only the explicit export destination is durable. Source imports and the
    # configured operator data directory remain untouched by working files.
    with tempfile.TemporaryDirectory(prefix="digest-state-operator-") as temp:
        state = CloudState.from_account_url(
            cfg.account_url,
            cfg.container,
            namespace=cfg.namespace,
            data_dir=temp,
            managed_identity_client_id=cfg.identity_client_id,
            operator_login=args.operator_login,
        )
        watchdog = None
        try:
            if args.command == "bootstrap":
                source = args.source_dir
                if (source / "bundle.tar.gz").exists():
                    state.bootstrap_export(source)
                else:
                    state.bootstrap(
                        source / "state.db",
                        archive_dir=source / "archive",
                        live_cookies_path=source / "x-cookies.json.live",
                    )
            else:
                # Own the process group before starting the same fence as jobs.
                if os.getpgrp() != os.getpid():
                    os.setsid()
                signal.signal(signal.SIGTERM, lambda *_: _fence_process_group())
                state.acquire()
                watchdog = LeaseWatchdog(state, _fence_process_group)
                watchdog.start()
                state.export(args.destination, backup_date=args.backup_date)
        finally:
            try:
                if watchdog is not None:
                    watchdog.stop()
            finally:
                state.release()
    if args.command == "bootstrap":
        print("Cloud state bootstrap completed; verify an export before enabling schedules")
    else:
        print("Cloud state export completed; keep the export private")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
