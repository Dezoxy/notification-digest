#!/usr/bin/env python3
"""Print the next release tag from `git tag --list` lines on stdin.

Used by auto-release.yml on the runner's system Python (stdlib only). Releases use
calendar versioning, `vYYYY.M.N`: UTC year, month WITHOUT zero padding, and a
per-month counter that starts at 0 (v2026.11.0, v2026.11.1, ..., v2026.12.0).
The month is not padded because SemVer, docker/metadata-action's `type=semver` and
the tag regexes downstream all reject leading zeros (`2027.01.0` is not a version).

The next tag is the highest existing tag with its counter incremented, unless that
would not reach the current month: `vYYYY.M.0` is the floor. So a new month starts
at `.0`, and the legacy `v0.x.y` tags sort below the floor and need no bootstrap
tag. A tag pushed by hand (for example a hotfix `vYYYY.M.N`) is respected and
continued from, and a clock behind the latest tag never moves the version backwards.
Anything that is not `vX.Y.Z` (rc tags, other names) is ignored, and with no valid
tag it fails instead of guessing: an empty list means a broken checkout, not a
fresh repository.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime

TAG_RE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def next_release_tag(tags: Iterable[str], today: date) -> str:
    """Return the next `vYYYY.M.N` tag for `today` (UTC), continuing from `tags`."""
    versions = [
        tuple(int(part) for part in match.groups())
        for tag in tags
        if (match := TAG_RE.fullmatch(tag.strip()))
    ]
    if not versions:
        raise ValueError("no existing vX.Y.Z tag to continue from")
    candidate = (today.year, today.month, 0)
    highest = max(versions)
    if highest >= candidate:
        year, month, patch = highest
        return f"v{year}.{month}.{patch + 1}"
    return f"v{today.year}.{today.month}.0"


def main(argv: Sequence[str] | None = None) -> int:
    """Read tags from stdin and print the next one; exit 1 on failure."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        help="override today's date (YYYY-MM-DD, UTC); for tests and ops",
    )
    args = parser.parse_args(argv)
    today = args.date or datetime.now(UTC).date()
    try:
        print(next_release_tag(sys.stdin.read().splitlines(), today))
    except ValueError as exc:
        print(f"Cannot compute the next release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
