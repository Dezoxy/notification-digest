"""Next-release computation for the automatic patch release."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/next_release_version.py"
SPEC = importlib.util.spec_from_file_location("next_release_version", SCRIPT)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_patch_bump_of_the_highest_tag():
    assert release.next_patch_tag(["v0.28.0", "v0.29.0", "v0.27.1"]) == "v0.29.1"


def test_numeric_not_lexical_order():
    assert release.next_patch_tag(["v0.9.0", "v0.10.0", "v0.2.0"]) == "v0.10.1"
    assert release.next_patch_tag(["v0.1.9", "v0.1.10"]) == "v0.1.11"
    assert release.next_patch_tag(["v9.9.9", "v10.0.0"]) == "v10.0.1"


def test_continues_from_a_manual_minor_bump():
    assert release.next_patch_tag(["v0.29.4", "v0.30.0"]) == "v0.30.1"


def test_ignores_tags_that_are_not_releases():
    tags = ["architecture-691e780", "v1.0.0-rc1", "v0.30", "v0.31.0\nx", "v01.0.0", "V0.9.0"]
    tags.append("v0.5.0")
    assert release.next_patch_tag(tags) == "v0.5.1"


def test_no_valid_tag_is_an_error():
    for tags in ([], ["architecture-691e780", "v1.0.0-rc1"], [""]):
        with pytest.raises(ValueError, match="no existing"):
            release.next_patch_tag(tags)


def test_main_reads_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("v0.2.0\nv0.10.3\nnoise\n"))
    assert release.main() == 0
    assert capsys.readouterr().out == "v0.10.4\n"


def test_main_fails_closed_without_tags(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("noise\n"))
    assert release.main() == 1
    assert "Cannot compute" in capsys.readouterr().err
