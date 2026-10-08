"""Explicit manual resolution of an uncertain cloud delivery; never sends."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import tempfile
from pathlib import Path

from digest import cloud_context
from digest.cloud_run import LeaseWatchdog, _fence_process_group
from digest.cloud_state import CloudState
from digest.config import CloudConfig
from digest.state import connect, init_db, reconcile_delivery


def main() -> None:
    """Restore under the same lease and atomically save a verified send outcome."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("digest_id", type=int)
    parser.add_argument("channel", choices=("telegram", "email"))
    parser.add_argument("outcome", choices=("confirmed", "pending"))
    parser.add_argument("--operator-login", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        cfg = CloudConfig.from_env()
        if os.getpgrp() != os.getpid():
            os.setsid()
        signal.signal(signal.SIGTERM, lambda *_: _fence_process_group())
        # Restored credentials must not remain in an operator's persistent data
        # directory. Close all lease users before removing this private workspace.
        with tempfile.TemporaryDirectory(prefix="digest-reconcile-") as temp:
            state = CloudState.from_account_url(
                cfg.account_url,
                cfg.container,
                namespace=cfg.namespace,
                data_dir=temp,
                managed_identity_client_id=cfg.identity_client_id,
                operator_login=args.operator_login,
            )
            watchdog = None
            conn = None
            try:
                state.acquire()
                watchdog = LeaseWatchdog(state, _fence_process_group)
                watchdog.start()
                state.restore()
                conn = connect(str(Path(temp) / "state.db"))
                init_db(conn)
                with cloud_context.cloud_execution(
                    cloud_context.CloudHooks(watchdog.guard, state.checkpoint)
                ):
                    reconcile_delivery(conn, args.digest_id, args.channel, args.outcome)
            finally:
                try:
                    if conn is not None:
                        conn.close()
                finally:
                    try:
                        if watchdog is not None:
                            watchdog.stop()
                    finally:
                        state.release()
        logging.info(
            "cloud delivery reconciled: digest=%d channel=%s outcome=%s",
            args.digest_id,
            args.channel,
            args.outcome,
        )
    except (Exception, cloud_context.CloudSafetyError) as exc:
        logging.error("cloud reconciliation failed: error_type=%s", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
