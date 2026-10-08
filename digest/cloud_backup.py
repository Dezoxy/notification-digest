"""Copy a validated daily export to a separate account with a backup-only identity."""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from digest import cloud_context
from digest.cloud_state import CloudState, CloudStateError
from digest.config import CloudConfig

logger = logging.getLogger(__name__)


def copy_daily_backup(state: CloudState, cfg: CloudConfig, *, container_client: Any = None) -> str:
    """Upload the bundle first and a compatible export manifest last as the marker."""
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
    prefix = f"backups/{cfg.namespace}/{datetime.now(UTC).date().isoformat()}/{uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix=".independent-backup-", dir=state.data_dir) as temp:
        exported = Path(temp)
        state.export(exported)
        for name in ("bundle.tar.gz", "manifest.json"):
            cloud_context.guard()
            state.check_lease()
            file_path = exported / name
            blob = container_client.get_blob_client(f"{prefix}/{name}")
            expected_digest = hashlib.sha256()
            with file_path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    expected_digest.update(chunk)
                    cloud_context.guard()
                    state.check_lease()
            with file_path.open("rb") as payload:
                blob.upload_blob(payload, overwrite=False)
            state.check_lease()
            cloud_context.guard()
            actual_digest = hashlib.sha256()
            actual_bytes = 0
            for chunk in blob.download_blob().chunks():
                cloud_context.guard()
                state.check_lease()
                actual_bytes += len(chunk)
                if actual_bytes > file_path.stat().st_size:
                    raise CloudStateError("Independent backup readback is oversized")
                actual_digest.update(chunk)
            if (
                actual_bytes != file_path.stat().st_size
                or actual_digest.digest() != expected_digest.digest()
            ):
                raise CloudStateError("Independent backup readback integrity failed")
            state.check_lease()
            cloud_context.guard()
    logger.info("cloud_backup event=success prefix=%s", prefix)
    return prefix
