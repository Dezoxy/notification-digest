"""Dependency-changes section of the automatic release notes."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release_dependency_notes.py"
SPEC = importlib.util.spec_from_file_location("release_dependency_notes", SCRIPT)
assert SPEC and SPEC.loader
notes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notes)


def lock(a="1.0", b="2.0", c="3.0", pytest_="8.0", extra_dep="4.0", root_ver="0.1.0", drop_b=False):
    """A tiny uv.lock: demo -> a -> b, demo -> c[ex] -> extra-dep, dev-only pytest."""
    b_pkg = "" if drop_b else f'\n[[package]]\nname = "b"\nversion = "{b}"\n'
    b_dep = "" if drop_b else '    { name = "b" },\n'
    return f"""
[[package]]
name = "a"
version = "{a}"
dependencies = [
{b_dep}]
{b_pkg}
[[package]]
name = "c"
version = "{c}"
[package.optional-dependencies]
ex = [
    {{ name = "extra-dep" }},
]

[[package]]
name = "extra-dep"
version = "{extra_dep}"

[[package]]
name = "demo"
version = "{root_ver}"
source = {{ virtual = "." }}
dependencies = [
    {{ name = "a" }},
    {{ name = "c", extra = ["ex"] }},
]

[package.dev-dependencies]
dev = [
    {{ name = "pytest" }},
]

[[package]]
name = "pytest"
version = "{pytest_}"
"""


DOCKERFILE = """\
FROM node:26-bookworm-slim@sha256:aaaaaaaaaaaabbbbbbbbbbbbcccccccc AS node-runtime
FROM python:3.14-slim-bookworm
COPY --from=ghcr.io/x/uv:0.1 /uv /usr/local/bin/uv
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
RUN npm install -g @anthropic-ai/claude-code@2.1.327 other@1.0.0 \\
    && true
"""


def section(base_lock, head_lock, base_df=DOCKERFILE, head_df=DOCKERFILE):
    return notes.render("vBASE", (base_lock, base_df), (head_lock, head_df))


def test_parse_dockerfile_pins():
    assert notes.parse_dockerfile(DOCKERFILE) == {
        "node": "26-bookworm-slim@sha256:aaaaaaaaaaaa",
        "python": "3.14-slim-bookworm",
        "ghcr.io/x/uv": "0.1",
        "@anthropic-ai/claude-code": "2.1.327",
        "other": "1.0.0",
    }


def test_stage_name_copy_is_ignored():
    assert "node-runtime" not in notes.parse_dockerfile(DOCKERFILE)


def test_digest_only_from_change_is_reported():
    head = DOCKERFILE.replace("aaaaaaaaaaaabbbb", "ddddddddddddbbbb")
    out = section(lock(), lock(), head_df=head)
    assert (
        "| `node` | 26-bookworm-slim@sha256:aaaaaaaaaaaa | 26-bookworm-slim@sha256:dddddddddddd |"
        in out
    )
    assert "Python (shipped)" not in out


def test_npm_pins_added_and_removed():
    head = DOCKERFILE.replace("other@1.0.0", "third@2.0.0")
    out = section(lock(), lock(), head_df=head)
    assert "| `other` | 1.0.0 | — |" in out
    assert "| `third` | — | 2.0.0 |" in out


def test_direct_and_transitive_via_and_dev():
    out = section(lock(), lock(a="1.1", b="2.1", pytest_="8.1"))
    assert "| `a` | 1.0 | 1.1 | direct |" in out
    assert "| `b` | 2.0 | 2.1 | transitive via `a` |" in out
    assert "**Dev only (not in the image):** `pytest` 8.0 → 8.1" in out


def test_extra_edge_is_reached():
    out = section(lock(), lock(extra_dep="4.1"))
    assert "| `extra-dep` | 4.0 | 4.1 | transitive via `c` |" in out


def test_added_and_removed_packages():
    out = section(lock(drop_b=True), lock())
    assert "| `b` | — | 2.0 | transitive via `a` |" in out
    out = section(lock(), lock(drop_b=True))
    assert "| `b` | 2.0 | — |" in out


def test_root_project_is_ignored():
    out = section(lock(), lock(root_ver="0.2.0"))
    assert out.splitlines()[2].startswith("None: no image pin")


def test_empty_diff_message_names_base():
    assert section(lock(), lock()) == (
        "## Dependency changes\n\nNone: no image pin or locked package changed since vBASE.\n"
    )


def test_missing_files_count_as_empty():
    out = section(None, lock(), base_df=None)
    assert "| `a` | — | 1.0 | direct |" in out
    assert "| `python` | — | 3.14-slim-bookworm |" in out


def test_cyclic_extras_terminate():
    cyc = """
[[package]]
name = "x"
version = "1"
dependencies = [{ name = "y", extra = ["e"] }]
[[package]]
name = "y"
version = "1"
[package.optional-dependencies]
e = [{ name = "y", extra = ["e"] }]
[[package]]
name = "demo"
version = "0"
source = { editable = "." }
dependencies = [{ name = "x" }]
"""
    direct, runtime, _ = notes.runtime_closure(notes.parse_lock(cyc))
    assert direct == {"x"} and runtime == {"x", "y"}


def test_main_uses_git_show(monkeypatch, capsys):
    files = {("a", "uv.lock"): lock(), ("b", "uv.lock"): lock(a="1.1"), ("a", "Dockerfile"): None}
    monkeypatch.setattr(notes, "ref_exists", lambda ref: True)
    monkeypatch.setattr(notes, "git_show", lambda ref, path: files.get((ref, path), DOCKERFILE))
    assert notes.main(["a", "b"]) == 0
    out = capsys.readouterr().out
    assert "| `a` | 1.0 | 1.1 | direct |" in out


def test_unknown_ref_fails_instead_of_reporting_everything_added(monkeypatch, capsys):
    """A mistyped base must not read as an empty side: that table would be a lie."""
    monkeypatch.setattr(notes, "ref_exists", lambda ref: ref != "v9.9.9")
    monkeypatch.setattr(notes, "git_show", lambda ref, path: pytest.fail("must not read files"))
    assert notes.main(["v9.9.9", "b"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown ref 'v9.9.9'" in captured.err


def _rev(spec: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", spec], cwd=ROOT, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def test_real_history_lock_maintenance(capsys):
    head = subprocess.run(
        ["git", "log", "--format=%H", "--grep=Lock file maintenance (#292)", "-1"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not head or not _rev(f"{head}^"):
        pytest.skip("commit for Lock file maintenance (#292) not in this clone")
    assert notes.main([f"{head}^", head]) == 0
    out = capsys.readouterr().out
    assert "| `nh3` |" in out
    assert "| `pyotp` | 2.10.0 | 2.12.0 | transitive via `twifork` |" in out
    assert "| `soupsieve` |" in out
