"""Copy a validated daily export to a separate account with a backup-only identity."""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from digest import cloud_context
from digest.cloud_state import CloudState, CloudStateError, _transient_storage_error
from digest.config import CloudConfig

logger = logging.getLogger(__name__)


def _guard_backup(state: CloudState, clock: Callable[[], float]) -> None:
    cloud_context.guard()
    state.check_lease()
    if clock() >= state.lease_deadline - 10:
        raise CloudStateError("Independent backup has no proven safe lease time remaining")


def _copy_attempt(
    state: CloudState,
    container_client: Any,
    files: list[tuple[Path, int, bytes]],
    prefix: str,
    clock: Callable[[], float],
) -> None:
    for file_path, expected_size, expected_digest in files:
        _guard_backup(state, clock)
        blob = container_client.get_blob_client(f"{prefix}/{file_path.name}")
        with file_path.open("rb") as payload:
            blob.upload_blob(payload, overwrite=False)
        _guard_backup(state, clock)
        actual_digest = hashlib.sha256()
        actual_bytes = 0
        for chunk in blob.download_blob().chunks():
            _guard_backup(state, clock)
            actual_bytes += len(chunk)
            if actual_bytes > expected_size:
                raise CloudStateError("Independent backup readback is oversized")
            actual_digest.update(chunk)
        if actual_bytes != expected_size or actual_digest.digest() != expected_digest:
            raise CloudStateError("Independent backup readback integrity failed")
        _guard_backup(state, clock)


def copy_daily_backup(
    state: CloudState,
    cfg: CloudConfig,
    *,
    container_client: Any = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Copy one private export, retrying transient failures into fresh immutable prefixes.

    The marker is uploaded only after bundle readback verifies. Lost upload or
    marker acknowledgements can leave an abandoned complete or partial prefix;
    retries never overwrite it, and retention eventually removes it.
    """
    if not all((cfg.backup_account_url, cfg.backup_container, cfg.backup_identity_client_id)):
        raise CloudStateError("Independent cloud backup settings are required for the backup job")
    if cfg.backup_account_url.rstrip("/") == cfg.account_url.rstrip("/"):
        raise CloudStateError("Independent backup must use a separate storage account")
    if (cfg.identity_client_id or "").lower() == cfg.backup_identity_client_id.lower():
        raise CloudStateError("Independent backup must use a separate managed identity")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", cfg.namespace):
        raise CloudStateError("Independent backup namespace must be a safe path segment")
    if container_client is None:
        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient(
            cfg.backup_account_url,
            credential=ManagedIdentityCredential(client_id=cfg.backup_identity_client_id),
            retry_total=0,
            connection_timeout=5,
            read_timeout=10,
        )
        container_client = service.get_container_client(cfg.backup_container)
    with tempfile.TemporaryDirectory(prefix=".independent-backup-", dir=state.data_dir) as temp:
        exported = Path(temp)
        _guard_backup(state, clock)
        state.export(exported)
        files = []
        for name in ("bundle.tar.gz", "manifest.json"):
            file_path = exported / name
            expected_digest = hashlib.sha256()
            with file_path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    _guard_backup(state, clock)
                    expected_digest.update(chunk)
            files.append((file_path, file_path.stat().st_size, expected_digest.digest()))
        for attempt in range(3):
            _guard_backup(state, clock)
            prefix = f"backups/{cfg.namespace}/{datetime.now(UTC).date().isoformat()}/{uuid4().hex}"
            try:
                _copy_attempt(state, container_client, files, prefix, clock)
            except Exception as exc:
                if (
                    attempt == 2
                    or isinstance(exc, CloudStateError)
                    or not _transient_storage_error(exc)
                ):
                    raise
                _guard_backup(state, clock)
                delay = 0.5 * (2**attempt)
                if clock() + delay >= state.lease_deadline - 10:
                    raise CloudStateError(
                        "Independent backup retry exceeds the safe lease budget"
                    ) from None
                logger.warning(
                    "cloud_backup event=retry attempt=%d error_type=%s",
                    attempt + 1,
                    type(exc).__name__,
                )
                sleep(delay)
                _guard_backup(state, clock)
            else:
                logger.info("cloud_backup event=success prefix=%s", prefix)
                return prefix
    raise AssertionError("Unreachable backup retry state")
