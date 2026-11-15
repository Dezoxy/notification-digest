"""Tests for scripts/replay_digest_prompts.py (offline prompt A/B replay). No network, no Claude."""

from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from digest import state
from digest.state import Item

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "replay_digest_prompts", ROOT / "scripts/replay_digest_prompts.py"
)
assert SPEC and SPEC.loader
replay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay
SPEC.loader.exec_module(replay)

T = datetime(2026, 11, 10, 6, 0, 0, tzinfo=UTC)
TEMPLATE = (
    "STATUS {{COLLECTOR_STATUS}}\nCOVERAGE\n{{RECENT_COVERAGE}}\n"
    "ARCS\n{{RECENT_ARCS}}\n{{ITEMS_JSON}}"
)

CANNED = """**TL;DR:** One thing happened. Another did too.

## Story A

- Point one [¹](https://x.com/a/status/1)
- Point two [²](https://x.com/a/status/2)

## Also this window

Minor thing. [³](https://x.com/a/status/3)

```arcs
[{"heading": "Story A", "key": "story-a"}]
```
"""


def iso(dt: datetime) -> str:
    return dt.isoformat()


def add_digest(conn: sqlite3.Connection, created: datetime, body: str, kind: str = "window") -> int:
    cur = conn.execute(
        "INSERT INTO digests (created_at, item_count, body_md, kind) VALUES (?, 0, ?, ?)",
        (iso(created), body, kind),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def add_item(
    conn: sqlite3.Connection,
    n: int,
    digest_id: int | None,
    *,
    source: str = "x",
    author: str | None = "a",
    chat_id: str | None = None,
    chat_title: str | None = None,
    text: str = "hello",
    url: str | None = None,
) -> str:
    url = url or f"https://x.com/a/status/{n}"
    conn.execute(
        "INSERT INTO items (source, source_id, chat_id, chat_title, author, text, url, "
        "fetched_at, digest_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, f"id{n}", chat_id, chat_title, author, text, url, iso(T), digest_id),
    )
    conn.commit()
    return url


@pytest.fixture
def db(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    path = tmp_path / "state.db"
    conn = state.connect(str(path))
    state.init_db(conn)
    return path, conn


def heading_body(title: str) -> str:
    return f"**TL;DR:** x.\n\n## {title}\n\nText.\n"


def args_for(out: Path, db_path: Path, digests: str, **over: object) -> list[str]:
    argv = [
        "run",
        "--db",
        str(db_path),
        "--out",
        str(out),
        "--digests",
        digests,
        "--samples",
        "1",
        "--pace-seconds",
        "0",
        "--model",
        "main-model",
        "--judge-model",
        "judge-model",
    ]
    for key, value in over.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return argv


# --- guards ---------------------------------------------------------------


def test_out_inside_repo_is_refused(tmp_path: Path) -> None:
    with pytest.raises(replay.ReplayError):
        replay.check_out_dir(replay.REPO_ROOT / "replay-out")
    with pytest.raises(replay.ReplayError):
        replay.check_out_dir(replay.REPO_ROOT)
    assert replay.check_out_dir(tmp_path / "ok") == (tmp_path / "ok").resolve()


def test_main_refuses_out_inside_repo(db: tuple[Path, sqlite3.Connection], capsys) -> None:
    code = replay.main(args_for(replay.REPO_ROOT / "replay-out", db[0], "1"))
    assert code == 1
    assert "inside the repository" in capsys.readouterr().err


def test_env_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    good = tmp_path / "cfg"
    good.mkdir()

    def with_env(env: dict[str, str]) -> None:
        monkeypatch.setattr(replay, "claude_subprocess_env", lambda: env)
        replay.check_claude_env()

    with_env({"CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CONFIG_DIR": str(good)})
    with pytest.raises(replay.ReplayError, match="OAUTH"):
        with_env({"CLAUDE_CONFIG_DIR": str(good)})
    with pytest.raises(replay.ReplayError, match="CLAUDE_CONFIG_DIR is not set"):
        with_env({"CLAUDE_CODE_OAUTH_TOKEN": "t"})
    with pytest.raises(replay.ReplayError, match="does not exist"):
        with_env({"CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CONFIG_DIR": str(tmp_path / "nope")})
    with pytest.raises(replay.ReplayError, match=r"~/\.claude"):
        with_env({"CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CONFIG_DIR": str(home / ".claude")})
    (good / "CLAUDE.md").write_text("global instructions")
    with pytest.raises(replay.ReplayError, match="CLAUDE.md"):
        with_env({"CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CONFIG_DIR": str(good)})


def test_db_is_opened_read_only(db: tuple[Path, sqlite3.Connection]) -> None:
    path, conn = db
    conn.close()
    ro = replay.open_ro(path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO digests (created_at, item_count, body_md) VALUES ('x', 0, 'y')")
    ro.close()


def test_script_does_not_import_delivery_modules() -> None:
    source = (ROOT / "scripts/replay_digest_prompts.py").read_text()
    for module in ("digest.deliver", "digest.publish", "digest.emailer"):
        assert module not in source


# --- windows, coverage, arcs ---------------------------------------------


def test_coverage_window_boundaries_and_sentinel(db: tuple[Path, sqlite3.Connection]) -> None:
    _, conn = db
    add_digest(conn, T - timedelta(hours=48), heading_body("Story at 48h"))
    add_digest(conn, T - timedelta(hours=24), heading_body("Story at 24h"))
    add_digest(conn, T - timedelta(hours=96), heading_body("Story at 96h"))
    add_digest(conn, T - timedelta(hours=1), heading_body("Daily noise"), kind="daily")
    add_digest(conn, T, heading_body("Story at T"))
    target = add_digest(conn, T, "**TL;DR:** target.\n\n## Target\n\nText.\n")
    add_item(conn, 1, target)
    windows = replay.load_window_digests(conn)
    plan = replay.build_plan(conn, windows, target, {"old": TEMPLATE, "new": TEMPLATE})
    old, new = plan.prompts["old"], plan.prompts["new"]
    assert "Story at 24h" in old and "Story at 48h" not in old
    assert "Story at 24h" in new and "Story at 48h" in new
    for prompt in (old, new):
        assert "Story at T" not in prompt
        assert "Story at 96h" not in prompt
        assert "Daily noise" not in prompt
    assert [p[0] for p in plan.judge_priors] == [
        w[0] for w in windows if w[1] >= T - timedelta(hours=72) and w[1] < T
    ][::-1]


def test_old_arm_uses_old_sentinel_when_no_coverage(db: tuple[Path, sqlite3.Connection]) -> None:
    _, conn = db
    add_digest(conn, T - timedelta(hours=48), heading_body("Only in new"))
    target = add_digest(conn, T, heading_body("Target"))
    add_item(conn, 1, target)
    plan = replay.build_plan(
        conn, replay.load_window_digests(conn), target, {"old": TEMPLATE, "new": TEMPLATE}
    )
    assert replay.OLD_NO_COVERAGE in plan.prompts["old"]
    assert replay.OLD_NO_COVERAGE not in plan.prompts["new"]
    assert "Only in new" in plan.prompts["new"]


def test_non_window_digest_is_refused(db: tuple[Path, sqlite3.Connection]) -> None:
    _, conn = db
    daily = add_digest(conn, T, heading_body("Daily"), kind="daily")
    with pytest.raises(replay.ReplayError, match="only 'window'"):
        replay.build_plan(conn, replay.load_window_digests(conn), daily, {"new": TEMPLATE})
    with pytest.raises(replay.ReplayError, match="not found"):
        replay.build_plan(conn, replay.load_window_digests(conn), 999, {"new": TEMPLATE})


def test_recent_arc_keys_upper_bound(db: tuple[Path, sqlite3.Connection]) -> None:
    _, conn = db
    inside = add_digest(conn, T - timedelta(days=1), heading_body("a"))
    inside2 = add_digest(conn, T - timedelta(days=2), heading_body("b"))
    at_t = add_digest(conn, T, heading_body("c"))
    too_old = add_digest(conn, T - timedelta(days=8), heading_body("d"))
    daily = add_digest(conn, T - timedelta(hours=3), heading_body("e"), kind="daily")
    rows = [
        (inside, "s1", "alpha"),
        (inside2, "s1", "alpha"),
        (inside, "s2", "beta"),
        (at_t, "s1", "future"),
        (too_old, "s1", "stale"),
        (daily, "s1", "daily-key"),
    ]
    conn.executemany("INSERT INTO arc_keys (digest_id, slug, key) VALUES (?, ?, ?)", rows)
    conn.commit()
    assert replay.recent_arc_keys(conn, T) == [("alpha", 2), ("beta", 1)]


# --- run: resumability, pipeline, judge ----------------------------------


@pytest.fixture
def run_env(db: tuple[Path, sqlite3.Connection], tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path, conn = db
    target = add_digest(conn, T, heading_body("Target"))
    for n in (1, 2, 3):
        add_item(conn, n, target)
    conn.close()
    calls: list[str] = []

    def fake_claude(prompt: str, model: str, timeout: int, effort: str) -> str:
        calls.append(model)
        if model == "judge-model":
            entry = {"passage": "p", "prior_digest_id": 1, "new_fact": False, "evidence": "none"}
            return "Sure:\n```json\n" + json.dumps([entry]) + "\n```"
        return CANNED

    monkeypatch.setattr(replay, "run_claude", fake_claude)
    monkeypatch.setattr(replay, "check_claude_env", lambda: None)
    monkeypatch.setattr(replay, "load_old_template", lambda ref: TEMPLATE)
    return path, tmp_path / "out", target, calls


def test_run_pipeline_old_keeps_bullet_new_flattens(run_env, capsys) -> None:
    path, out, target, calls = run_env
    assert replay.main(args_for(out, path, str(target))) == 0
    d = out / str(target)
    old = (d / "old-s1.shipped.md").read_text()
    new = (d / "new-s1.shipped.md").read_text()
    assert "- Point one" in old
    assert "- Point one" not in new and "Point one" in new
    assert json.loads((d / "new-s1.arcs.json").read_text()) == [
        {"heading": "Story A", "key": "story-a"}
    ]
    judged = json.loads((d / "old-s1.judge.json").read_text())
    assert judged[0]["new_fact"] is False
    # 2 arms x (generation + judge)
    assert calls.count("main-model") == 2 and calls.count("judge-model") == 2
    printed = capsys.readouterr().out
    assert "digest=" in printed and "Point one" not in printed

    # score reads the saved files back
    assert replay.main(["score", "--db", str(path), "--out", str(out)]) == 0
    rows = list(csv.DictReader((out / "scores.csv").open()))
    by_arm = {r["arm"]: r for r in rows}
    assert by_arm["old"]["bullet_lines_shipped"] == "2"
    assert by_arm["new"]["bullet_lines_shipped"] == "0"
    assert by_arm["old"]["repeats_no_new_fact"] == "1"
    assert "| bullet_lines_shipped | 0 |" in (out / "report.md").read_text()


def test_run_is_resumable(run_env) -> None:
    path, out, target, calls = run_env
    assert replay.main(args_for(out, path, str(target))) == 0
    first = len(calls)
    assert replay.main(args_for(out, path, str(target))) == 0
    assert len(calls) == first


def test_error_file_does_not_block_retry(run_env, monkeypatch: pytest.MonkeyPatch) -> None:
    path, out, target, calls = run_env
    d = out / str(target)
    d.mkdir(parents=True)
    (d / "old-s1.error.txt").write_text("SummarizeError: boom")
    assert replay.main(args_for(out, path, str(target), arms="old")) == 0
    assert (d / "old-s1.raw.md").exists()
    assert not (d / "old-s1.error.txt").exists()


def test_claude_exception_writes_error_without_prompt(
    run_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, out, target, calls = run_env

    def boom(prompt: str, model: str, timeout: int, effort: str) -> str:
        raise replay.sm.SummarizeError("claude -p exited 1")

    monkeypatch.setattr(replay, "run_claude", boom)
    assert replay.main(args_for(out, path, str(target), arms="old")) == 0
    d = out / str(target)
    assert not (d / "old-s1.raw.md").exists()
    text = (d / "old-s1.error.txt").read_text()
    assert "SummarizeError" in text and "hello" not in text


def test_existing_raw_skips_the_call(run_env) -> None:
    path, out, target, calls = run_env
    d = out / str(target)
    d.mkdir(parents=True)
    (d / "old-s1.raw.md").write_text(CANNED)
    assert replay.main(args_for(out, path, str(target), arms="old")) == 0
    assert calls == ["judge-model"]  # post-processed + judged from the saved raw only


def test_validate_failure_moves_raw_aside_so_a_rerun_retries(
    run_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, out, target, calls = run_env
    monkeypatch.setattr(replay, "run_claude", lambda *a: "You've hit your usage limit")
    assert replay.main(args_for(out, path, str(target), arms="new")) == 0
    d = out / str(target)
    assert not (d / "new-s1.raw.md").exists() and not (d / "new-s1.shipped.md").exists()
    assert (d / "new-s1.rejected.md").read_text() == "You've hit your usage limit"
    assert (d / "new-s1.error.txt").exists()


def test_run_stops_after_consecutive_failures(
    run_env, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    path, out, target, calls = run_env
    attempts: list[str] = []

    def failing(*args: object) -> str:
        attempts.append("call")
        raise RuntimeError("limit")

    monkeypatch.setattr(replay, "run_claude", failing)
    argv = args_for(out, path, str(target))
    argv[argv.index("--samples") + 1] = "5"  # 5 samples x 2 arms = 10 possible calls
    assert replay.main(argv) == 2
    assert len(attempts) == replay.MAX_CONSECUTIVE_FAILURES
    assert "rerun the same command later" in capsys.readouterr().out


# --- judge parsing ---------------------------------------------------------


def test_parse_judge_json_variants() -> None:
    entry = {"passage": "a", "prior_digest_id": 3, "new_fact": False, "evidence": "e"}
    fenced = "Here:\n```json\n" + json.dumps([entry]) + "\n```\nDone."
    assert replay.parse_judge_json(fenced) == [entry]
    assert replay.parse_judge_json("note [not json] then " + json.dumps([entry])) == [entry]
    assert replay.parse_judge_json("[]") == []
    with pytest.raises(ValueError):
        replay.parse_judge_json("nothing here")


def test_judge_prompt_labels_data() -> None:
    prompt = replay.build_judge_prompt([(7, "2026-11-10T00:00:00+00:00", "prior body")], "cand")
    assert "untrusted DATA" in prompt and "id=7" in prompt and "prior body" in prompt


# --- metrics ---------------------------------------------------------------


def make_item(source: str, n: int, **kw: object) -> Item:
    defaults: dict[str, object] = {
        "chat_id": None,
        "author": None,
        "text": "t",
        "url": f"https://example.org/{source}/{n}",
        "fetched_at": iso(T),
    }
    defaults.update(kw)
    return Item(source=source, source_id=str(n), **defaults)  # type: ignore[arg-type]


def pm_text(question: str, old: int, new: int, resolves: str) -> str:
    # Same shape as collectors/polymarket.py's _build_item.
    return (
        f'Prediction market swing: "{question}" moved {old}% -> {new}% '
        f"(24h volume $1,000, resolves {resolves})."
    )


def cites(items: list[Item]) -> str:
    sup = "⁰¹²³⁴⁵⁶⁷⁸⁹"
    return " ".join(f"[{sup[i % 10]}]({it.url})" for i, it in enumerate(items, 1))


def test_metrics_on_hand_built_body() -> None:
    bob = [make_item("x", n, author="Bob", url=f"https://x.com/bob/status/{n}") for n in range(3)]
    tg = make_item("telegram", 10, chat_id="7", chat_title="Some Chat")
    bobcorp = [
        make_item("x", 20 + n, author="BobCorp", url=f"https://x.com/bobcorp/status/{n}")
        for n in range(4)
    ]
    tg2 = make_item("telegram", 30, chat_id="9", chat_title="Other")
    ecb = [make_item("news", 40 + n, url=f"https://www.ecb.europa.eu/p/{n}") for n in range(4)]
    hn = make_item("hackernews", 50)
    acme = [make_item("news", 60 + n, url=f"https://acme.example/p/{n}") for n in range(4)]
    few = [
        make_item("x", 70 + n, author="zed", url=f"https://x.com/zed/status/{n}") for n in range(3)
    ]
    pm_resolved = make_item(
        "polymarket",
        80,
        text=pm_text("Q?", 40, 99, "2026-12-31"),
    )
    pm_expired = make_item(
        "polymarket",
        81,
        text=pm_text("R?", 50, 60, "2026-11-09"),
    )
    pm_ok = make_item(
        "polymarket",
        82,
        text=pm_text("S?", 50, 60, "2026-11-10"),
    )
    also = make_item("x", 90, author="q", url="https://x.com/q/status/1")
    items = [*bob, tg, *bobcorp, tg2, *ecb, hn, *acme, *few, pm_resolved, pm_expired, pm_ok, also]
    body = f"""**TL;DR:** One two three. Four five.

**Crypto:** a hidden label paragraph.

## Market chatter

{cites([*bob, tg])}

## Bobcorp launches product

{cites([*bobcorp, tg2])}

## Rates decision

{cites([*ecb, hn])}

## New launch

{cites(acme)}

## Too few

{cites(few)}

## Markets

{cites([pm_resolved, pm_expired, pm_ok])}

## Also this window

Thing happened here. [⁵]({also.url}) Another **bold** [link](https://q.example) item.

- a bullet outside a fence
1. numbered line
---
* * *

```text
- bullet inside fence
```
"""
    arcs = [{"heading": "New launch", "key": "acme-launch"}]
    m = replay.compute_metrics(
        None, body, items, arcs, [{"new_fact": False}, {"new_fact": True}], T
    )
    assert m["bullet_lines_shipped"] == 2
    assert m["bold_labels"] == 1
    assert (m["tldr_sentences"], m["tldr_words"]) == (2, 5)
    assert m["also_words"] == 14  # list markers, rules, citations and markdown syntax excluded
    assert m["sections"] == 6  # six story sections, "Also this window" excluded
    assert m["max_share_raw"] == 1.0
    assert m["max_share_exempt"] == 0.75
    assert (m["max_share_heading"], m["max_share_account"]) == ("Market chatter", "x:@bob")
    assert m["expired_market_citations"] == 1
    assert m["resolved_market_citations"] == 1
    assert m["repeats_no_new_fact"] == 1


def test_account_mapping() -> None:
    assert replay.account_of(make_item("x", 1, author="@Bob")) == "x:@bob"
    assert replay.account_of(make_item("telegram", 1, chat_id="-100")) == "tg:-100"
    assert replay.account_of(make_item("news", 1, url="https://www.Foo.com/a")) == "news:foo.com"
    assert replay.account_of(make_item("hackernews", 1)) is None
    assert replay.account_of(make_item("polymarket", 1)) is None


def test_bullets_outside_fences_and_raw_fences_stripped() -> None:
    raw = (
        '## H\n\n- real bullet\n\n```arcs\n[{"heading": "H", "key": "k"}]\n```\n'
        "```deltas\n- not a bullet\n```\n"
    )
    assert replay.compute_metrics(raw, None, [], [], None, T)["bullet_lines_raw"] == 1
    assert replay.bullet_lines("---\n- - -\n- ok") == 1  # both rules excluded
