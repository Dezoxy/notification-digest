#!/usr/bin/env python3
"""Print the next patch release tag from `git tag --list` lines on stdin.

Used by auto-release.yml on the runner's system Python (stdlib only). Git tags
`vX.Y.Z` are the only version source: the next tag is the highest existing one by
numeric order with the patch incremented. Anything else (rc tags, other names) is
ignored, and with no valid tag it fails instead of guessing a starting point.
Minor and major bumps stay manual: push `vX.Y.0` by hand and the automatic
releases continue from it.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable

TAG_RE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def next_patch_tag(tags: Iterable[str]) -> str:
    """Return the highest `vX.Y.Z` tag with its patch number incremented."""
    versions = [
        tuple(int(part) for part in match.groups())
        for tag in tags
        if (match := TAG_RE.fullmatch(tag.strip()))
    ]
    if not versions:
        raise ValueError("no existing vX.Y.Z tag to continue from")
    major, minor, patch = max(versions)
    return f"v{major}.{minor}.{patch + 1}"


def main() -> int:
    """Read tags from stdin and print the next one; exit 1 on failure."""
    try:
        print(next_patch_tag(sys.stdin.read().splitlines()))
    except ValueError as exc:
        print(f"Cannot compute the next release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
