"""Replace X cookie credentials in authoritative cloud state without calling X."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import stat
import tempfile
from pathlib import Path

from digest import cloud_context
from digest.cloud_run import LeaseWatchdog, _fence_process_group
from digest.cloud_state import CloudState, CloudStateError
from digest.config import CloudConfig
from digest.state import connect

logger = logging.getLogger(__name__)
_MAX_COOKIE_BYTES = 1024 * 1024


def read_private_cookies(path: Path) -> bytes:
    """Accept only a private regular file containing the collector's cookie mapping."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_uid != os.getuid()
                or metadata.st_size > _MAX_COOKIE_BYTES
            ):
                raise ValueError("Cookie source must be a private regular file")
            payload = source.read(_MAX_COOKIE_BYTES + 1)
        cookies = json.loads(payload)
        if (
            len(payload) > _MAX_COOKIE_BYTES
            or not isinstance(cookies, dict)
            or not cookies
            or any(
                not isinstance(key, str) or not key or not isinstance(value, str)
                for key, value in cookies.items()
            )
            or not cookies.get("auth_token")
            or not cookies.get("ct0")
        ):
            raise ValueError("Cookie source must contain auth_token and ct0")
    except (OSError, ValueError):
        raise CloudStateError("Cookie source is not a private valid X cookie mapping") from None
    # Only the file path appears in argv. Never display the credential contents.
    return json.dumps(cookies, ensure_ascii=True, sort_keys=True).encode("utf-8")


def rotate_cookies(state: CloudState, source_path: Path) -> None:
    """Publish both jars under one guarded lease, preserving all SQLite state."""
    payload = read_private_cookies(source_path)
    state.acquire()
    watchdog = LeaseWatchdog(state, _fence_process_group, job="cookie_rotation")
    watchdog.start()
    conn = None
    try:
        state.restore()
        watchdog.guard()
        conn = connect(str(state.data_dir / "state.db"))
        with cloud_context.cloud_execution(
            cloud_context.CloudHooks(watchdog.guard, state.checkpoint)
        ):
            with tempfile.TemporaryDirectory(prefix=".cookies-", dir=state.data_dir) as temp:
                staging = Path(temp)
                for name in ("x-cookies.json", "x-cookies.json.live"):
                    target = staging / name
                    with target.open("xb") as output:
                        target.chmod(0o600)
                        output.write(payload)
                    cloud_context.guard()
                    target.replace(state.data_dir / name)
                # Equality intentionally selects the live jar and avoids relying
                # on filesystem timestamp resolution. Both hold the same values.
                modified = (state.data_dir / "x-cookies.json.live").stat().st_mtime_ns
                for name in ("x-cookies.json", "x-cookies.json.live"):
                    os.utime(state.data_dir / name, ns=(modified, modified))
            cloud_context.checkpoint(conn)
        logger.info("cloud_cookie_rotation event=success")
    finally:
        if conn is not None:
            conn.close()
        watchdog.stop()
        state.release()


def main() -> None:
    """Operator-only rotation using managed identity or explicitly selected az login."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cookie_file", type=Path)
    parser.add_argument("--operator-login", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        cfg = CloudConfig.from_env()
        if os.getpgrp() != os.getpid():
            os.setsid()
        signal.signal(signal.SIGTERM, lambda *_: _fence_process_group())
        # Operator commands must not overwrite a persistent local state copy.
        with tempfile.TemporaryDirectory(prefix="digest-cookie-rotation-") as temp:
            state = CloudState.from_account_url(
                cfg.account_url,
                cfg.container,
                namespace=cfg.namespace,
                data_dir=temp,
                managed_identity_client_id=cfg.identity_client_id,
                operator_login=args.operator_login,
            )
            rotate_cookies(state, args.cookie_file)
    except (Exception, cloud_context.CloudSafetyError) as exc:
        logger.error("cloud_cookie_rotation event=failure error_type=%s", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
