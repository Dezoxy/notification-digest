"""Scheduled Azure jobs with leased SQLite snapshots and explicit UTC slots."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from digest import cloud_context
from digest.cloud_state import CloudState, LeaseBusyError
from digest.config import CloudConfig, Config
from digest.state import cloud_slot_completed, complete_cloud_slot, connect, init_db

logger = logging.getLogger(__name__)
JOBS = (
    "daytime",
    "overnight",
    "evening",
    "daily",
    "weekly",
    "positions",
    "patreon",
    "relay",
    "backup",
)
CURSOR_JOBS = frozenset({"daytime", "overnight", "evening", "positions", "patreon", "relay"})
_GRACE = timedelta(minutes=10)


def scheduled_slot(job: str, now: datetime, *, catch_up_slot: datetime | None = None) -> str | None:
    """Return the due UTC slot, rejecting wrong DST candidates before any lease."""
    if job not in JOBS or now.tzinfo is None:
        raise ValueError("invalid job or timezone")
    candidate = catch_up_slot or now
    if candidate.tzinfo is None:
        raise ValueError("catch-up slot must include its UTC offset")
    candidate = candidate.astimezone(UTC)
    if catch_up_slot is not None:
        if candidate > now.astimezone(UTC) or candidate.second or candidate.microsecond:
            raise ValueError("catch-up slot must be an elapsed exact scheduled minute")
    minute = {
        "daytime": 0,
        "overnight": 0,
        "evening": 0,
        "daily": 30,
        "weekly": 45,
        "positions": 25,
        "patreon": 50,
        "relay": 40,
        "backup": 15,
    }[job]
    # Look back across an hour boundary so a late :50 execution can start at :00.
    slot = candidate.replace(minute=minute, second=0, microsecond=0)
    if slot > candidate:
        slot -= timedelta(hours=1)
    valid = True
    if job == "daytime":
        valid = slot.hour in (6, 12)
    elif job == "overnight":
        valid = slot.hour == 0
    elif job == "evening":
        valid = slot.hour == 18
    elif job == "positions":
        valid = slot.hour in (1, 5, 9, 13, 17, 21)
    elif job == "backup":
        valid = slot.hour == 4
    elif job in ("daily", "weekly"):
        local = slot.astimezone(ZoneInfo("Europe/Budapest"))
        valid = (local.hour, local.minute) == ((20, 30) if job == "daily" else (21, 45))
        if job == "weekly":
            valid = valid and local.weekday() == 6
    if catch_up_slot is not None:
        if not valid or slot != candidate:
            raise ValueError("catch-up slot does not match the job schedule")
    elif not valid:
        return None
    elif candidate - slot > _GRACE:
        if job in CURSOR_JOBS:
            logger.info("cloud_job event=skipped job=%s reason=late_start", job)
            return None
        raise ValueError(
            "scheduled job started after its ten-minute grace; explicit catch-up required"
        )
    return slot.isoformat()


class LeaseWatchdog:
    """Renew independently and kill this process group before the lease expires."""

    def __init__(
        self,
        state: CloudState,
        fence: Callable[[], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        job: str = "operator",
    ) -> None:
        self.state = state
        self.fence = fence
        self.clock = clock
        self.job = job
        self.run_deadline = clock() + 35 * 60
        self.stopped = threading.Event()
        self.failed = threading.Event()
        self.threads: list[threading.Thread] = []

    def guard(self) -> None:
        if (
            self.failed.is_set()
            or self.clock() >= self.state.lease_deadline - 10
            or self.clock() >= self.run_deadline
        ):
            raise RuntimeError("lease is not proven safe")
        self.state.check_lease()

    def start(self) -> None:
        for target in (self._renew, self._watch):
            thread = threading.Thread(target=target, daemon=True)
            self.threads.append(thread)
            thread.start()

    def _fail(self, reason: str = "LeaseLost") -> None:
        if not self.failed.is_set():
            self.failed.set()
            logger.error(
                "cloud_job event=failure job=%s error_type=%s failure_stage=%s",
                self.job,
                reason,
                "runtime" if reason == "RuntimeBudgetExceeded" else "lease",
            )
            self.fence()

    def _renew(self) -> None:
        while not self.stopped.wait(15):
            try:
                self.state.renew()
            except Exception:
                self._fail()
                return

    def _watch(self) -> None:
        while not self.stopped.wait(0.5):
            if self.clock() >= self.run_deadline:
                self._fail("RuntimeBudgetExceeded")
                return
            if self.clock() >= self.state.lease_deadline - 10:
                self._fail()
                return

    def stop(self) -> None:
        self.stopped.set()
        for thread in self.threads:
            thread.join(timeout=1)


def _fence_process_group() -> None:
    # The CLI inherits this group. Killing the whole group closes Telethon and
    # the active Claude process even when a SDK call or send is blocked.
    # Namespace PID 1 can report ESRCH for its apparent process group. Exiting
    # PID 1 tears down the container and its children; never leave the watchdog
    # thread dead while a stale worker continues sending.
    try:
        os.killpg(os.getpid(), signal.SIGKILL)
    finally:
        os._exit(70)


def _dispatch(job: str, cfg: Config) -> bool:
    from digest import main as application

    cloud_context.guard()
    if job == "daily":
        return application.run_daily(cfg)
    if job == "weekly":
        return application.run_weekly(cfg)
    if job == "positions":
        return application.run_positions(cfg)
    if job == "patreon":
        return application.run_patreon(cfg)
    if job == "relay":
        return application.run_relay(cfg)
    hidden = {"overnight": frozenset({"telegram"}), "evening": frozenset({"telegram", "site"})}.get(
        job, frozenset()
    )
    return asyncio.run(application._run(cfg, hidden))


def run_job(job: str, cloud: CloudConfig, cfg: Config, slot: str, state: CloudState) -> bool:
    """Restore one lineage, run the existing mode and durably mark its slot."""
    if job not in JOBS:
        raise ValueError("unsupported cloud job")
    started = time.monotonic()
    try:
        state.acquire()
    except LeaseBusyError:
        if job not in CURSOR_JOBS:
            raise
        logger.info("cloud_job event=skipped job=%s slot=%s reason=lease_busy", job, slot)
        return True
    watchdog = LeaseWatchdog(state, _fence_process_group, job=job)
    watchdog.start()
    conn = None
    failures: list[cloud_context.FailureStage] = []
    try:
        state.restore()
        data_dir = Path(cloud.data_dir)
        cfg = replace(
            cfg,
            state_db_path=str(data_dir / "state.db"),
            archive_dir=str(data_dir / "archive"),
            x_cookies_path=str(data_dir / "x-cookies.json") if cfg.x_enabled else None,
        )
        conn = connect(cfg.state_db_path)
        # Upgrade once before installing hooks, then persist the complete schema.
        init_db(conn)
        with cloud_context.cloud_execution(
            cloud_context.CloudHooks(watchdog.guard, state.checkpoint, failures.append)
        ):
            cloud_context.checkpoint(conn)
            if cloud_slot_completed(conn, job, slot):
                logger.info("cloud_job event=already_completed job=%s slot=%s", job, slot)
                return True
            if job == "backup":
                state.checkpoint(conn, daily_backup=True)
                state.prune()
                from digest.cloud_backup import copy_daily_backup

                copy_daily_backup(state, cloud)
                ok = True
            else:
                ok = _dispatch(job, cfg)
            cloud_context.checkpoint(conn)
            if ok:
                complete_cloud_slot(conn, job, slot)
            failure_stage = "none" if ok else (failures[-1] if failures else "application")
            logger.info(
                "cloud_job event=%s job=%s slot=%s duration_seconds=%.2f failure_stage=%s",
                "success" if ok else "failure",
                job,
                slot,
                time.monotonic() - started,
                failure_stage,
            )
            return ok
    finally:
        if conn is not None:
            conn.close()
        watchdog.stop()
        state.release()


def main() -> None:
    """Strict cloud entrypoint; wrong timezone candidates exit without work."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=JOBS)
    parser.add_argument("--catch-up-slot", type=datetime.fromisoformat)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        slot = scheduled_slot(args.job, datetime.now(UTC), catch_up_slot=args.catch_up_slot)
        if slot is None:
            logger.info("cloud_job event=not_due job=%s", args.job)
            return
        cloud = CloudConfig.from_env()
        cfg = Config.from_env()
        if os.getpgrp() != os.getpid():
            os.setsid()
        signal.signal(signal.SIGTERM, lambda *_: _fence_process_group())
        state = CloudState.from_account_url(
            cloud.account_url,
            cloud.container,
            namespace=cloud.namespace,
            data_dir=cloud.data_dir,
            managed_identity_client_id=cloud.identity_client_id,
        )
        if not run_job(args.job, cloud, cfg, slot, state):
            raise SystemExit(1)
    except (Exception, cloud_context.CloudSafetyError) as exc:
        logger.error(
            "cloud_job event=failure job=%s error_type=%s failure_stage=entrypoint",
            args.job,
            type(exc).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
