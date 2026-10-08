"""Release guard: which plans deploy automatically, and when the jobs count as idle."""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "azure_release_guard", ROOT / "scripts/azure_release_guard.py"
)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

OLD = "ghcr.io/dezoxy/notification-digest:0.28.0"
NEW = "ghcr.io/dezoxy/notification-digest:0.29.0"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"


def main_tf_crons() -> dict[str, str]:
    """The job -> cron map declared in infra/azure/main.tf."""
    block = re.search(r"jobs = \{(.*?)\n  \}", (ROOT / "infra/azure/main.tf").read_text(), re.S)
    assert block
    return dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]+)"$', block.group(1), re.M))


CRONS = main_tf_crons()


def job_values(name: str, image: str, cron: str | None = None) -> dict:
    return {
        "name": f"digest-{name}",
        "resource_group_name": "notification-digest-westeurope",
        "event_stream_endpoint": "https://example.invalid/stream",
        "outbound_ip_addresses": ["192.0.2.1"],
        "schedule_trigger_config": [{"cron_expression": cron, "parallelism": 1}] if cron else [],
        "template": [
            {
                "container": [
                    {
                        "name": "digest",
                        "image": image,
                        "env": [{"name": "TZ", "value": "UTC", "secret_name": ""}],
                    }
                ]
            }
        ],
    }


def job_change(name: str, before: str = OLD, after: str = NEW, actions=("update",)) -> dict:
    return {
        "address": f'azurerm_container_app_job.digest["{name}"]',
        "type": "azurerm_container_app_job",
        "name": "digest",
        "change": {
            "actions": list(actions),
            "before": job_values(name, before),
            "after": job_values(name, after),
            "after_unknown": {},
        },
    }


def plan(*changes: dict) -> dict:
    return {"resource_changes": list(changes)}


def bump_plan() -> dict:
    return plan(
        {
            "type": "azurerm_resource_group",
            "name": "digest",
            "change": {"actions": ["no-op"]},
        },
        {"type": "azurerm_key_vault", "name": "d", "change": {"actions": ["read"]}},
        *[job_change(name) for name in CRONS],
    )


def check(p: dict, pin: str = NEW) -> tuple[str, str]:
    return guard.check_plan(p, pin)


def refuses(p: dict, match: str, pin: str = NEW) -> None:
    with pytest.raises(guard.GuardError, match=match):
        check(p, pin)


def test_main_tf_declares_nine_jobs():
    assert len(CRONS) == 9


def test_no_changes_is_noop():
    assert check(plan())[0] == "noop"
    assert check(plan({"type": "x", "change": {"actions": ["no-op"]}}))[0] == "noop"


def test_image_only_bump_of_all_jobs_applies():
    decision, reason = check(bump_plan())
    assert decision == "apply"
    assert OLD in reason and NEW in reason and "9 jobs" in reason


def test_computed_outputs_may_be_unknown_after_apply():
    p = bump_plan()
    for change in p["resource_changes"][2:]:
        change["change"]["after"].pop("event_stream_endpoint")
        change["change"]["after_unknown"] = {"event_stream_endpoint": True}
    assert check(p)[0] == "apply"


@pytest.mark.parametrize(
    "action", [["create"], ["delete"], ["delete", "create"], ["create", "delete"]]
)
def test_job_create_delete_replace_is_refused(action):
    p = bump_plan()
    p["resource_changes"][2]["change"]["actions"] = action
    refuses(p, "only in-place updates")


def test_non_job_change_is_refused():
    p = bump_plan()
    p["resource_changes"].append(
        {"type": "azurerm_storage_account", "name": "state", "change": {"actions": ["update"]}}
    )
    refuses(p, "only in-place updates")


def test_partial_bump_is_refused():
    p = bump_plan()
    p["resource_changes"][2]["change"]["actions"] = ["no-op"]
    refuses(p, "all jobs must move together")


def test_job_without_image_change_is_refused():
    p = bump_plan()
    after = p["resource_changes"][2]["change"]["after"]
    after["template"][0]["container"][0]["image"] = OLD
    refuses(p, "without an image change")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v["template"][0]["container"][0]["env"][0].update(value="other"),
        lambda v: v["template"][0]["container"][0]["env"].append({"name": "NEW"}),
        lambda v: v["schedule_trigger_config"].append({"cron_expression": "0 1 * * *"}),
        lambda v: v.update(replica_timeout_in_seconds=1),
        lambda v: v["template"][0]["container"].append({"name": "sidecar", "image": NEW}),
    ],
)
def test_non_image_difference_is_refused(mutate):
    p = bump_plan()
    mutate(p["resource_changes"][3]["change"]["after"])
    refuses(p, "more than its image|same image")


def test_cron_change_is_refused():
    p = plan(job_change("daytime"))
    change = p["resource_changes"][0]["change"]
    change["before"]["schedule_trigger_config"] = [{"cron_expression": "0 6 * * *"}]
    change["after"]["schedule_trigger_config"] = [{"cron_expression": "0 7 * * *"}]
    refuses(p, "more than its image")


def test_images_differing_between_jobs_are_refused():
    p = bump_plan()
    p["resource_changes"][2]["change"]["after"]["template"][0]["container"][0]["image"] = (
        "ghcr.io/dezoxy/notification-digest:0.29.1"
    )
    refuses(p, "same image")


def test_image_other_than_pin_is_refused():
    refuses(
        bump_plan(), "differs from the tracked pin", pin="ghcr.io/dezoxy/notification-digest:0.30.0"
    )


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/dezoxy/notification-digest:latest",
        "ghcr.io/dezoxy/notification-digest:0.29",
        "ghcr.io/dezoxy/notification-digest",
        "ghcr.io/other/notification-digest:0.29.0",
        "ghcr.io/dezoxy/notification-digest:0.29.0\n",
    ],
)
def test_floating_or_foreign_image_is_refused(image):
    p = plan(job_change("daytime", after=image))
    refuses(p, "exact release", pin=image)


def test_digest_image_is_accepted():
    digest = "ghcr.io/dezoxy/notification-digest@sha256:" + "a" * 64
    assert check(plan(job_change("daytime", after=digest)), digest)[0] == "apply"


@pytest.mark.parametrize(
    "unknown",
    [
        {"template": [{"container": [{"image": True}]}]},
        {"template": [{"container": [{"ephemeral_storage": True}]}]},
        {"tags": True},
        {"identity": [{"principal_id": True}]},
        True,
    ],
)
def test_unknown_after_apply_is_refused(unknown):
    p = bump_plan()
    p["resource_changes"][4]["change"]["after_unknown"] = unknown
    refuses(p, "unknown until apply")


@pytest.mark.parametrize("plan_json", [[], {"resource_changes": None}, {"resource_changes": [1]}])
def test_unreadable_plan_is_refused(plan_json):
    refuses(plan_json, "resource_changes")


def test_malformed_template_is_refused():
    p = plan(job_change("daytime"))
    p["resource_changes"][0]["change"]["after"]["template"] = []
    refuses(p, "template shape")


def test_load_pin(tmp_path):
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps({"image": NEW}))
    assert guard.load_pin(pin) == NEW
    for bad in ['{"image": "x"}', json.dumps({"image": NEW, "other": 1}), "[]", "not json"]:
        pin.write_text(bad)
        with pytest.raises(guard.GuardError):
            guard.load_pin(pin)
    with pytest.raises(guard.GuardError):
        guard.load_pin(tmp_path / "missing.json")


def test_tracked_pin_is_valid():
    assert guard.IMAGE_RE.fullmatch(guard.load_pin(ROOT / "infra/azure/image.auto.tfvars.json"))


def test_pin_regex_matches_variables_tf():
    variables = (ROOT / "infra/azure/variables.tf").read_text()
    regex = re.search(r'regex\("(\^ghcr[^"]+)"', variables)
    assert regex
    assert regex.group(1).replace("\\\\", "\\") == f"^{guard.IMAGE_RE.pattern}$"


def run_main(tmp_path, monkeypatch, p, pin=NEW):
    plan_file, pin_file, out = tmp_path / "plan.json", tmp_path / "pin.json", tmp_path / "out"
    plan_file.write_text(json.dumps(p))
    pin_file.write_text(json.dumps({"image": pin}))
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    code = guard.main(["plan-check", str(plan_file), str(pin_file)])
    return code, out.read_text() if out.exists() else ""


def test_main_writes_decision_and_exit_codes(tmp_path, monkeypatch, capsys):
    assert run_main(tmp_path, monkeypatch, bump_plan()) == (0, "decision=apply\n")
    assert run_main(tmp_path, monkeypatch, plan()) == (0, "decision=apply\ndecision=noop\n")
    bad = bump_plan()
    bad["resource_changes"][2]["change"]["actions"] = ["delete", "create"]
    code, output = run_main(tmp_path, monkeypatch, bad)
    assert code == 1 and output.count("decision=") == 2
    assert "refused" in capsys.readouterr().err


def test_verdict_never_prints_env_values(tmp_path, monkeypatch, capsys):
    p = bump_plan()
    for change in p["resource_changes"][2:]:
        change["change"]["after"]["template"][0]["container"][0]["env"][0]["value"] = "SECRET-X"
    code, _ = run_main(tmp_path, monkeypatch, p)
    captured = capsys.readouterr()
    assert code == 1 and "SECRET-X" not in captured.out + captured.err


# --- cron ---------------------------------------------------------------------


def at(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    """October 2026: the 4th is a Sunday, the 5th a Monday."""
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def fire_times(expression: str, day: int = 4) -> list[tuple[int, int]]:
    minutes = [at(day, 0, 0) + timedelta(minutes=i) for i in range(24 * 60)]
    return [(m.hour, m.minute) for m in minutes if guard.cron_matches(expression, m)]


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ("daytime", [(6, 0), (12, 0)]),
        ("overnight", [(0, 0)]),
        ("evening", [(18, 0)]),
        ("daily", [(18, 30), (19, 30)]),
        ("positions", [(h, 25) for h in (1, 5, 9, 13, 17, 21)]),
        ("patreon", [(h, 50) for h in range(24)]),
        ("relay", [(h, 40) for h in range(24)]),
        ("backup", [(4, 15)]),
    ],
)
def test_main_tf_crons_fire_when_declared(job, expected):
    assert fire_times(CRONS[job]) == expected


def test_weekly_fires_only_on_sunday():
    assert fire_times(CRONS["weekly"], day=4) == [(19, 45), (20, 45)]
    for day in range(5, 11):
        assert fire_times(CRONS["weekly"], day=day) == []


def test_fires_within_hour_boundary_and_margin():
    cron = CRONS["overnight"]  # 00:00 daily
    assert guard.fires_within(cron, at(5, 23, 55), 5)
    assert guard.fires_within(cron, at(5, 23, 55, 59), 5)
    assert not guard.fires_within(cron, at(5, 23, 54, 59), 4)
    assert guard.fires_within(cron, at(5, 0, 0, 30), 5)  # the slot that just fired
    assert not guard.fires_within(cron, at(5, 0, 1), 5)


def test_fires_within_sunday_rule():
    weekly = CRONS["weekly"]
    assert guard.fires_within(weekly, at(4, 19, 41), 5)
    assert not guard.fires_within(weekly, at(5, 19, 41), 5)  # Monday


def test_day_of_week_and_day_of_month_either_may_match():
    assert guard.cron_matches("0 0 5 * 0", at(5, 0, 0))  # Monday the 5th: day-of-month
    assert guard.cron_matches("0 0 5 * 0", at(4, 0, 0))  # Sunday the 4th: day-of-week
    assert not guard.cron_matches("0 0 5 * 0", at(6, 0, 0))


@pytest.mark.parametrize(
    "expression",
    [
        "*/5 * * * *",
        "0-5 * * * *",
        "0 6-12 * * *",
        "0 0 * * MON",
        "0 0 * JAN *",
        "@daily",
        "0 0 * * 7",
        "60 * * * *",
        "0 24 * * *",
        "0 0 0 * *",
        "0 0 * 13 *",
        "0 0 ? * *",
        "0 0 * * *  *",
        "0 0 * *",
        "0,,1 * * * *",
        "",
    ],
)
def test_unsupported_cron_raises(expression):
    with pytest.raises(guard.GuardError):
        guard.cron_matches(expression, at(4, 0, 0))


# --- idle wait -----------------------------------------------------------------


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


JOBS = [
    guard.Job("digest-daytime", "rg", "0 6,12 * * *"),
    guard.Job("digest-relay", "rg", "40 * * * *"),
]
MANUAL_JOBS = [guard.Job("digest-daytime", "rg", None)]


def wait(clock, fetch, jobs=JOBS, **kwargs):
    guard.wait_idle(jobs, SUBSCRIPTION, fetch=fetch, clock=clock, sleep=clock.sleep, **kwargs)


def test_idle_returns_immediately(capsys):
    clock = Clock(at(5, 10, 0))
    wait(clock, lambda *_: ["Succeeded", "Failed", "Stopped", "Degraded"])
    assert clock.slept == []
    assert "idle" in capsys.readouterr().out


def test_no_executions_is_idle():
    clock = Clock(at(5, 10, 0))
    wait(clock, lambda *_: [])
    assert clock.slept == []


@pytest.mark.parametrize("status", ["Running", "Processing", "running"])
def test_running_execution_is_busy_until_it_finishes(status):
    clock = Clock(at(5, 10, 0))
    calls = []

    def fetch(subscription, group, name):
        calls.append((subscription, group, name))
        return [status] if clock.now < at(5, 10, 2) else ["Succeeded"]

    wait(clock, fetch)
    assert clock.slept == [30] * 4
    assert (SUBSCRIPTION, "rg", "digest-daytime") in calls


def test_upcoming_slot_is_busy_until_it_has_passed():
    clock = Clock(at(5, 11, 56))  # daytime fires at 12:00
    wait(clock, lambda *_: ["Succeeded"], margin_minutes=5)
    # idle only once 12:00 is behind the clock; the slot's own minute is still busy
    assert clock.now >= at(5, 12, 1)
    assert clock.slept


def test_manual_jobs_have_no_slot():
    clock = Clock(at(5, 11, 59))
    wait(clock, lambda *_: ["Succeeded"], jobs=MANUAL_JOBS)
    assert clock.slept == []


@pytest.mark.parametrize("status", [None, "Unknown", "Pending", 3, ""])
def test_undeterminable_status_counts_as_busy(status, capsys):
    clock = Clock(at(5, 10, 0))
    with pytest.raises(guard.GuardError, match="not idle"):
        wait(clock, lambda *_: ["Succeeded", status], timeout_minutes=2)
    assert "undeterminable" in capsys.readouterr().out


def test_failed_listing_counts_as_busy_then_recovers():
    clock = Clock(at(5, 10, 0))
    attempts = []

    def fetch(*_):
        attempts.append(1)
        if len(attempts) <= 2:
            raise guard.GuardError("Could not list executions of digest-daytime.")
        return []

    wait(clock, fetch)
    assert clock.slept == [30]


def test_timeout_raises():
    clock = Clock(at(5, 10, 0))
    with pytest.raises(guard.GuardError, match="within 3 minutes"):
        wait(clock, lambda *_: ["Running"], timeout_minutes=3)
    assert clock.now == at(5, 10, 3)


def test_invalid_subscription_fails_without_polling():
    clock = Clock(at(5, 10, 0))
    with pytest.raises(guard.GuardError, match="Invalid"):
        guard.wait_idle(JOBS, "", fetch=lambda *_: [], clock=clock, sleep=clock.sleep)
    assert clock.slept == []


def test_unsupported_cron_fails_closed_without_polling():
    clock = Clock(at(5, 10, 0))
    with pytest.raises(guard.GuardError, match="Unsupported"):
        wait(clock, lambda *_: [], jobs=[guard.Job("digest-x", "rg", "*/5 * * * *")])
    assert clock.slept == []


def test_jobs_from_plan_reads_name_group_and_cron():
    p = plan(
        {
            "type": "azurerm_container_app_job",
            "change": {"actions": ["update"], "after": job_values("daytime", NEW, "0 6,12 * * *")},
        },
        {
            "type": "azurerm_container_app_job",
            "change": {"actions": ["update"], "after": job_values("relay", NEW)},
        },
        {"type": "azurerm_storage_account", "change": {"actions": ["no-op"], "after": {}}},
    )
    assert guard.jobs_from_plan(p) == [
        guard.Job("digest-daytime", "notification-digest-westeurope", "0 6,12 * * *"),
        guard.Job("digest-relay", "notification-digest-westeurope", None),
    ]
    with pytest.raises(guard.GuardError):
        guard.jobs_from_plan(plan())


def test_executions_url_rejects_path_tricks():
    url = guard.executions_url(SUBSCRIPTION, "rg", "digest-daytime")
    assert url.startswith("https://management.azure.com/subscriptions/")
    assert "/providers/Microsoft.App/jobs/digest-daytime/executions?api-version=" in url
    for args in [("x", "rg", "j"), (SUBSCRIPTION, "../rg", "j"), (SUBSCRIPTION, "rg", "j/../k")]:
        with pytest.raises(guard.GuardError):
            guard.executions_url(*args)


class Completed:
    def __init__(self, payload) -> None:
        self.stdout = json.dumps(payload).encode()


def test_fetch_statuses_uses_core_az_rest_and_follows_pages(monkeypatch):
    pages = {
        0: {
            "value": [{"properties": {"status": "Running"}}],
            "nextLink": "https://management.azure.com/next",
        },
        1: {"value": [{"properties": {}}]},
    }
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return Completed(pages[len(commands) - 1])

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    assert guard.fetch_statuses(SUBSCRIPTION, "rg", "digest-daytime") == ["Running", None]
    assert all(c[:3] == ["az", "rest", "--method"] and "containerapp" not in c for c in commands)
    assert commands[1][commands[1].index("--url") + 1] == "https://management.azure.com/next"


def test_fetch_statuses_rejects_foreign_pagination_and_az_errors(monkeypatch):
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda *a, **k: Completed({"value": [], "nextLink": "https://evil.example/next"}),
    )
    with pytest.raises(guard.GuardError, match="pagination"):
        guard.fetch_statuses(SUBSCRIPTION, "rg", "digest-daytime")

    def boom(*a, **k):
        raise guard.subprocess.CalledProcessError(1, "az")

    monkeypatch.setattr(guard.subprocess, "run", boom)
    with pytest.raises(guard.GuardError, match="Could not list"):
        guard.fetch_statuses(SUBSCRIPTION, "rg", "digest-daytime")


def test_wait_idle_command_exit_codes(tmp_path, monkeypatch):
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(bump_plan()))
    monkeypatch.setenv("ARM_SUBSCRIPTION_ID", SUBSCRIPTION)
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: Completed({"value": []}))
    monkeypatch.setattr(guard, "fires_within", lambda *a: False)
    assert guard.main(["wait-idle", str(plan_file)]) == 0
    monkeypatch.delenv("ARM_SUBSCRIPTION_ID")
    assert guard.main(["wait-idle", str(plan_file)]) == 1
