#!/usr/bin/env python3
"""Gate the automatic Azure deploy that follows a merged image-pin bump.

Stdlib only: azure-deploy.yml runs it on the runner's system Python without uv.

plan-check PLAN_JSON PIN_FILE accepts exactly one kind of change: every digest
job moves in place to the single image named by the tracked pin and nothing else
differs. A plan with no changes is a no-op. Anything else is refused, and the
operator uses the manual plan/apply path. Output never includes environment
values, only job counts and image references.

wait-idle PLAN_JSON blocks until no job is executing and none is about to fire,
so the apply does not replace a job under a live run. All jobs share one SQLite
state behind a lease; a partial bump or an overlap could let an older image open
a database that a newer one migrated.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

JOB_TYPE = "azurerm_container_app_job"
# Same pattern as the `image` variable in infra/azure/variables.tf.
IMAGE_RE = re.compile(
    r"ghcr\.io/dezoxy/notification-digest(:[0-9]+\.[0-9]+\.[0-9]+|@sha256:[0-9a-f]{64})"
)
MASK = "<image>"
# Top-level computed, read-only attributes of azurerm_container_app_job (from
# `terraform providers schema -json`, azurerm 5.4.0). They carry no configuration,
# so an unknown value there cannot hide a change. Nested computed attributes
# (identity.principal_id, template.container.ephemeral_storage) are deliberately
# not listed. The schema says which attributes COULD be unknown, not which ones the
# provider marks unknown on an update, so compare the first real image bump against
# this guard by hand and extend the list only for attributes that are provider
# outputs.
UNKNOWN_ALLOWED = frozenset({"event_stream_endpoint", "outbound_ip_addresses"})

API_VERSION = "2024-03-01"
ARM = "https://management.azure.com"
BUSY_STATUSES = frozenset({"running", "processing"})
DONE_STATUSES = frozenset({"succeeded", "failed", "stopped", "degraded"})
MAX_EXECUTION_PAGES = 20
UUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
AZ_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,89}")


class GuardError(Exception):
    """An operator-safe refusal reason, free of environment values."""


class Job(NamedTuple):
    """The parts of a planned job that the idle wait needs."""

    name: str
    resource_group: str
    cron: str | None


def load_pin(path: Path) -> str:
    """Read the tracked image pin and check it against the variable's pattern."""
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or set(data) != {"image"}:
            raise ValueError
        image = data["image"]
    except (OSError, ValueError) as exc:
        raise GuardError("The tracked image pin is missing or malformed.") from exc
    if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
        raise GuardError("The tracked image pin is not an exact release tag or digest.")
    return image


def has_unknown(after_unknown: Any, top_level: bool = True) -> bool:
    """True when `after_unknown` marks any value as unknown after apply."""
    if after_unknown is True:
        return True
    if isinstance(after_unknown, dict):
        return any(
            has_unknown(value, False)
            for key, value in after_unknown.items()
            if not (top_level and value is True and key in UNKNOWN_ALLOWED)
        )
    if isinstance(after_unknown, list):
        return any(has_unknown(value, False) for value in after_unknown)
    return False


def comparable(values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a copy for comparison, and the container images it held.

    Images are masked. Provider-computed outputs are dropped: an unknown one is
    absent from `after`, and no configuration can set them.
    """
    try:
        masked = copy.deepcopy(values)
        for key in UNKNOWN_ALLOWED:
            masked.pop(key, None)
        template = masked["template"]
        if len(template) != 1:
            raise ValueError
        images = []
        for container in template[0]["container"]:
            images.append(container["image"])
            container["image"] = MASK
        if not images or not all(isinstance(image, str) for image in images):
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise GuardError("A job has an unexpected template shape.") from exc
    return masked, images


def check_plan(plan: Any, pin: str) -> tuple[str, str]:
    """Decide `apply` or `noop` for a Terraform plan, or raise GuardError to refuse."""
    changes = plan.get("resource_changes", []) if isinstance(plan, dict) else None
    if not isinstance(changes, list) or not all(isinstance(c, dict) for c in changes):
        raise GuardError("The plan JSON has no readable resource_changes.")
    pending = [
        c for c in changes if (c.get("change") or {}).get("actions") not in (["no-op"], ["read"])
    ]
    if not pending:
        return "noop", "The plan has no changes."
    for change in pending:
        actions = (change.get("change") or {}).get("actions")
        if change.get("type") != JOB_TYPE or actions != ["update"]:
            raise GuardError(
                f"{change.get('type')} {change.get('name')} would be {actions}; only in-place "
                "updates of the digest jobs are automatic."
            )
    jobs = [c for c in changes if c.get("type") == JOB_TYPE]
    if len(pending) != len(jobs):
        raise GuardError(
            f"{len(pending)} of {len(jobs)} jobs would change; all jobs must move together."
        )
    old_images: set[str] = set()
    new_images: set[str] = set()
    for job in pending:
        change = job["change"]
        before, after = change.get("before"), change.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise GuardError("A job update has no readable before/after values.")
        if has_unknown(change.get("after_unknown")):
            raise GuardError("A job update has values that are unknown until apply.")
        masked_before, before_images = comparable(before)
        masked_after, after_images = comparable(after)
        if masked_before != masked_after:
            raise GuardError(f"{job.get('address')} differs in more than its image.")
        if before_images == after_images:
            raise GuardError(f"{job.get('address')} is updated without an image change.")
        old_images.update(before_images)
        new_images.update(after_images)
    if len(new_images) != 1:
        raise GuardError("The jobs would not receive the same image.")
    new_image = next(iter(new_images))
    if not IMAGE_RE.fullmatch(new_image):
        raise GuardError("The new image is not an exact release tag or digest.")
    if new_image != pin:
        raise GuardError("The plan's image differs from the tracked pin.")
    return "apply", f"{len(pending)} jobs, image {', '.join(sorted(old_images))} -> {new_image}"


def parse_field(text: str, low: int, high: int) -> set[int] | None:
    """Parse `*` (None) or a comma list of integers in [low, high]."""
    if text == "*":
        return None
    values = set()
    for part in text.split(","):
        if not re.fullmatch(r"[0-9]{1,2}", part) or not low <= int(part) <= high:
            raise GuardError(f"Unsupported cron field {text!r}.")
        values.add(int(part))
    return values


def cron_matches(expression: str, moment: datetime) -> bool:
    """Evaluate a 5-field UTC cron for one minute; only `*` and integer lists exist."""
    fields = expression.split()
    if len(fields) != 5:
        raise GuardError(f"Unsupported cron expression {expression!r}.")
    minute, hour, dom, month, dow = (
        parse_field(text, low, high)
        for text, (low, high) in zip(
            fields, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)], strict=True
        )
    )
    weekday = (moment.weekday() + 1) % 7  # cron counts Sunday as 0
    day_ok = dom is None or moment.day in dom
    weekday_ok = dow is None or weekday in dow
    # Standard cron: when both day fields are restricted, either one may match.
    days = day_ok or weekday_ok if dom is not None and dow is not None else day_ok and weekday_ok
    return (
        (minute is None or moment.minute in minute)
        and (hour is None or moment.hour in hour)
        and (month is None or moment.month in month)
        and days
    )


def fires_within(expression: str, start: datetime, minutes: int) -> bool:
    """True when the cron has a slot from `start`'s minute through `minutes` later."""
    slot = start.replace(second=0, microsecond=0)
    return any(cron_matches(expression, slot + timedelta(minutes=i)) for i in range(minutes + 1))


def jobs_from_plan(plan: Any) -> list[Job]:
    """Name, resource group and cron (if scheduled) of every job the plan covers."""
    jobs = []
    try:
        for change in plan["resource_changes"]:
            after = change["change"].get("after")
            if change["type"] != JOB_TYPE or not after:
                continue
            triggers = after.get("schedule_trigger_config") or []
            jobs.append(
                Job(
                    after["name"],
                    after["resource_group_name"],
                    triggers[0]["cron_expression"] if triggers else None,
                )
            )
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise GuardError("The plan JSON has no readable job definitions.") from exc
    if not jobs:
        raise GuardError("The plan contains no jobs to wait for.")
    return jobs


def executions_url(subscription: str, resource_group: str, name: str) -> str:
    """Build the executions endpoint, refusing values that could alter the path."""
    if not (
        UUID_RE.fullmatch(subscription)
        and AZ_NAME_RE.fullmatch(resource_group)
        and AZ_NAME_RE.fullmatch(name)
    ):
        raise GuardError("Invalid subscription, resource group or job name.")
    return (
        f"{ARM}/subscriptions/{subscription}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.App/jobs/{name}/executions?api-version={API_VERSION}"
    )


def fetch_statuses(subscription: str, resource_group: str, name: str) -> list[Any]:
    """List a job's execution statuses through core `az rest` (no extensions)."""
    url = executions_url(subscription, resource_group, name)
    statuses: list[Any] = []
    for _ in range(MAX_EXECUTION_PAGES):
        try:
            result = subprocess.run(
                ["az", "rest", "--method", "get", "--url", url, "--only-show-errors"],
                capture_output=True,
                check=True,
                timeout=120,
            )
            page = json.loads(result.stdout)
            statuses += [(item.get("properties") or {}).get("status") for item in page["value"]]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            raise GuardError(f"Could not list executions of {name}.") from exc
        url = page.get("nextLink") or ""
        if not url:
            return statuses
        if not url.startswith(f"{ARM}/"):
            raise GuardError(f"Unexpected execution pagination endpoint for {name}.")
    raise GuardError(f"Execution list of {name} did not end; treating it as busy.")


def busy_reasons(
    jobs: list[Job],
    now: datetime,
    margin_minutes: int,
    subscription: str,
    fetch: Callable[[str, str, str], list[Any]],
) -> list[str]:
    """Why the jobs are not idle yet; an empty list means idle."""
    reasons = []
    for name, group, cron in jobs:
        try:
            statuses = fetch(subscription, group, name)
        except GuardError as exc:
            reasons.append(f"{name}: {exc}")
            statuses = []
        for status in statuses:
            lowered = status.lower() if isinstance(status, str) else None
            if lowered in DONE_STATUSES:
                continue
            kind = f"is {status}" if lowered in BUSY_STATUSES else "has an undeterminable status"
            reasons.append(f"{name}: an execution {kind}")
            break
        if cron and fires_within(cron, now, margin_minutes):
            reasons.append(f"{name}: fires within {margin_minutes} minutes")
    return reasons


def wait_idle(
    jobs: list[Job],
    subscription: str,
    margin_minutes: int = 5,
    timeout_minutes: int = 45,
    *,
    fetch: Callable[[str, str, str], list[Any]] = fetch_statuses,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    interval_seconds: int = 30,
) -> None:
    """Poll until every job is idle; raise GuardError when the timeout passes first."""
    for job in jobs:  # a bad subscription or name is a hard failure, not "busy"
        executions_url(subscription, job.resource_group, job.name)
    deadline = clock() + timedelta(minutes=timeout_minutes)
    while True:
        now = clock()
        reasons = busy_reasons(jobs, now, margin_minutes, subscription, fetch)
        if not reasons:
            print("All jobs are idle and none fires within the margin.")
            return
        for reason in reasons:
            print(f"busy: {reason}")
        if now >= deadline:
            raise GuardError(f"Jobs were not idle within {timeout_minutes} minutes.")
        sleep(interval_seconds)


def load_plan(path: Path) -> Any:
    """Read Terraform's plan JSON."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise GuardError("The plan JSON is missing or unreadable.") from exc


def main(argv: list[str] | None = None) -> int:
    """Run a subcommand; any refusal or failure exits non-zero."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("plan-check", help="Accept only an image-only bump of all jobs")
    check.add_argument("plan_json", type=Path)
    check.add_argument("pin_file", type=Path)
    wait = commands.add_parser("wait-idle", help="Wait until no job runs or is about to fire")
    wait.add_argument("plan_json", type=Path)
    wait.add_argument("--margin-minutes", type=int, default=5)
    wait.add_argument("--timeout-minutes", type=int, default=45)
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan_json)
        if args.command == "plan-check":
            decision, reason = check_plan(plan, load_pin(args.pin_file))
            print(f"Release guard: {decision}. {reason}")
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                    output.write(f"decision={decision}\n")
        else:
            wait_idle(
                jobs_from_plan(plan),
                os.environ.get("ARM_SUBSCRIPTION_ID", ""),
                args.margin_minutes,
                args.timeout_minutes,
            )
        return 0
    except GuardError as exc:
        print(f"Release guard refused: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("Release guard failed unexpectedly; details suppressed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
