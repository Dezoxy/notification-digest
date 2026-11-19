"""Next-release computation for the automatic CalVer release (vYYYY.M.N)."""

from __future__ import annotations

import importlib.util
import io
import re
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/next_release_version.py"
SPEC = importlib.util.spec_from_file_location("next_release_version", SCRIPT)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)

NOV = date(2026, 11, 18)


def test_legacy_tags_start_the_month_at_zero():
    assert release.next_release_tag(["v0.28.0", "v0.29.25", "v0.27.1"], NOV) == "v2026.11.0"


def test_same_month_increments_the_counter():
    assert release.next_release_tag(["v0.29.25", "v2026.11.0"], NOV) == "v2026.11.1"
    assert release.next_release_tag(["v2026.11.0", "v2026.11.3"], NOV) == "v2026.11.4"


def test_new_month_resets_the_counter():
    assert release.next_release_tag(["v2026.11.7"], date(2026, 12, 1)) == "v2026.12.0"


def test_year_rollover_has_no_zero_padding():
    assert release.next_release_tag(["v2026.12.4"], date(2027, 1, 3)) == "v2027.1.0"


def test_numeric_not_lexical_order():
    assert release.next_release_tag(["v2026.9.5", "v2026.10.0"], date(2026, 10, 2)) == "v2026.10.1"
    assert (
        release.next_release_tag(["v2026.10.9", "v2026.10.10"], date(2026, 10, 20)) == "v2026.10.11"
    )


def test_clock_behind_the_latest_tag_never_goes_backwards():
    assert release.next_release_tag(["v2026.12.2"], date(2026, 11, 30)) == "v2026.12.3"


def test_ignores_tags_that_are_not_releases():
    tags = [
        "architecture-691e780",
        "v1.0.0-rc1",
        "v0.30",
        "v0.31.0\nx",
        "v01.0.0",
        "V0.9.0",
        "v2026.01.0",
        "v9999.1.0-rc1",
    ]
    tags.append("v0.5.0")
    assert release.next_release_tag(tags, NOV) == "v2026.11.0"


def test_no_valid_tag_is_an_error():
    for tags in ([], ["architecture-691e780", "v1.0.0-rc1", "v2026.01.0"], [""]):
        with pytest.raises(ValueError, match="no existing"):
            release.next_release_tag(tags, NOV)


def test_main_reads_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("v0.2.0\nv2026.11.3\nnoise\n"))
    assert release.main(["--date", "2026-11-18"]) == 0
    assert capsys.readouterr().out == "v2026.11.4\n"


def test_main_defaults_to_todays_utc_date(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("v0.29.25\n"))
    assert release.main([]) == 0
    assert re.fullmatch(r"v\d{4}\.(1[0-2]|[1-9])\.\d+\n", capsys.readouterr().out)


def test_main_fails_closed_without_tags(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("noise\n"))
    assert release.main(["--date", "2026-11-18"]) == 1
    assert "Cannot compute" in capsys.readouterr().err


def test_current_is_the_highest_valid_tag():
    assert (
        release.current_release_tag(["v0.29.25", "v2026.9.5", "v2026.10.0", "junk"]) == "v2026.10.0"
    )


def test_main_current_prints_current_tag(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("v0.2.0\nv2026.11.3\nnoise\n"))
    assert release.main(["--current"]) == 0
    assert capsys.readouterr().out == "v2026.11.3\n"


def test_main_current_fails_closed_without_tags(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("noise\n"))
    assert release.main(["--current"]) == 1
    assert "Cannot compute" in capsys.readouterr().err
