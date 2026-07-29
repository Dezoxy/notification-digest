import subprocess
from types import SimpleNamespace

import pytest

import digest.summarize as summarize_mod
from digest.state import Item
from digest.summarize import SummarizeError, build_prompt, run_claude, summarize


def _item(source_id: str = "1") -> Item:
    return Item(
        source="telegram",
        source_id=source_id,
        chat_id="123",
        author="alice",
        text="hello world",
        url=f"https://t.me/c/123/{source_id}",
        fetched_at="2026-07-29T10:00:00+00:00",
    )


# --- build_prompt ---


def test_build_prompt_embeds_items_json():
    prompt = build_prompt([_item("1"), _item("2")], failed_sources=[])

    assert '"source": "telegram"' in prompt
    assert '"author": "alice"' in prompt
    assert '"text": "hello world"' in prompt
    assert '"url": "https://t.me/c/123/1"' in prompt
    assert '"url": "https://t.me/c/123/2"' in prompt
    # source_id is not part of the payload contract
    assert '"source_id"' not in prompt


def test_build_prompt_status_line_reports_success_when_nothing_failed():
    prompt = build_prompt([_item()], failed_sources=[])

    assert "Collector status: all collectors succeeded this run." in prompt
    assert "Collector status: telegram" not in prompt


def test_build_prompt_status_line_names_the_failed_source_only_when_failures_given():
    prompt = build_prompt([_item()], failed_sources=["telegram"])

    assert "Collector status: telegram collection failed this run" in prompt
    # the banner-prepend instruction itself is static template text present
    # either way; what changes per-run is the collector-status line above,
    # which is what actually tells Claude whether to act on it
    assert "prepend a single banner" in prompt


def test_build_prompt_empty_items_still_produces_valid_json_array():
    prompt = build_prompt([], failed_sources=[])
    assert "[]" in prompt


# --- run_claude ---


def _fake_completed(returncode=0, stdout="## Needs attention\n...", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_claude_success_returns_stripped_stdout(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _fake_completed(stdout="  ## Needs attention\nsome text  \n")

    monkeypatch.setattr(summarize_mod.subprocess, "run", fake_run)

    result = run_claude("the prompt", model="claude-opus-5", timeout_seconds=300)

    assert result == "## Needs attention\nsome text"
    assert captured["cmd"] == [
        "claude",
        "-p",
        "--model",
        "claude-opus-5",
        "--output-format",
        "text",
    ]
    assert captured["kwargs"]["input"] == "the prompt"
    assert captured["kwargs"]["timeout"] == 300
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True


def test_run_claude_nonzero_exit_raises_summarize_error_without_stderr_content(monkeypatch):
    fake_stderr = "auth error: session expired SECRET_STDERR_MARKER_98765"

    def fake_run(cmd, **kwargs):
        return _fake_completed(returncode=1, stdout="", stderr=fake_stderr)

    monkeypatch.setattr(summarize_mod.subprocess, "run", fake_run)

    with pytest.raises(SummarizeError, match="exited 1") as exc_info:
        run_claude("the prompt", model="claude-opus-5", timeout_seconds=300)

    assert "SECRET_STDERR_MARKER_98765" not in str(exc_info.value)


def test_run_claude_empty_stdout_raises_summarize_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _fake_completed(returncode=0, stdout="   \n  ")

    monkeypatch.setattr(summarize_mod.subprocess, "run", fake_run)

    with pytest.raises(SummarizeError, match="empty"):
        run_claude("the prompt", model="claude-opus-5", timeout_seconds=300)


def test_run_claude_timeout_raises_summarize_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(summarize_mod.subprocess, "run", fake_run)

    with pytest.raises(SummarizeError, match="timed out"):
        run_claude("the prompt", model="claude-opus-5", timeout_seconds=5)


def test_run_claude_error_never_includes_the_prompt(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _fake_completed(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(summarize_mod.subprocess, "run", fake_run)

    secret_prompt = "SECRET_MESSAGE_CONTENT_12345"
    with pytest.raises(SummarizeError) as exc_info:
        run_claude(secret_prompt, model="claude-opus-5", timeout_seconds=300)

    assert secret_prompt not in str(exc_info.value)


# --- summarize (composition) ---


def test_summarize_builds_prompt_and_runs_claude(monkeypatch):
    calls = {}

    def fake_build_prompt(items, failed_sources):
        calls["build_prompt"] = (items, failed_sources)
        return "built prompt"

    def fake_run_claude(prompt, model, timeout_seconds):
        calls["run_claude"] = (prompt, model, timeout_seconds)
        return "## Needs attention\n..."

    monkeypatch.setattr(summarize_mod, "build_prompt", fake_build_prompt)
    monkeypatch.setattr(summarize_mod, "run_claude", fake_run_claude)

    items = [_item()]
    result = summarize(items, ["telegram"], "claude-opus-5", 300)

    assert result == "## Needs attention\n..."
    assert calls["build_prompt"] == (items, ["telegram"])
    assert calls["run_claude"] == ("built prompt", "claude-opus-5", 300)
