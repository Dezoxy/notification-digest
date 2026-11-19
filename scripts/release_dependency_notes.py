#!/usr/bin/env python3
"""Print a "Dependency changes" markdown section for a release: BASE_REF to HEAD_REF.

Used by auto-release.yml on the runner's system Python (stdlib only) so each
GitHub release lists exactly which shipped dependencies changed, including
transitive ones that Renovate's lock-maintenance PR hides behind "All locks
refreshed". It reads `uv.lock` and `Dockerfile` at both refs with `git show`
and prints versions and names only.

Python packages are classified by the HEAD lock (the BASE lock for removed ones).
The runtime closure starts at the root project package (its `source` is
`editable` or `virtual`): its `dependencies` are DIRECT, and everything
reachable through `dependencies` (plus a dependency's `extra = [...]` entries in
the target's `optional-dependencies`) is runtime. The root's `dev-dependencies`
are ignored, so any other locked package is DEV and not in the image (the
Dockerfile runs `uv sync --frozen --no-dev`). A runtime package that is not
direct is reported "transitive via X", X being the alphabetically first
runtime package (or the root's direct dependency) that depends on it.

Image pins are the Dockerfile `FROM` lines, `COPY --from=<image>:<tag>` (a stage
name is not an image and is skipped) and `npm install -g pkg@ver`. A digest-only
change of the same tag counts as a change.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence

FROM_RE = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)", re.IGNORECASE)
COPY_FROM_RE = re.compile(r"^\s*COPY\s+.*?--from=(\S+)", re.IGNORECASE)
NPM_RE = re.compile(r"npm\s+install\s+(?:-\S+\s+)*?-g\s+(.*)|npm\s+install\s+.*?--global\s+(.*)")
NPM_PIN_RE = re.compile(r"(@?[^@\s]+)@(\S+)")
DIGEST_LEN = 12


def ref_exists(ref: str) -> bool:
    """Whether `ref` names a commit in this repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def git_show(ref: str, path: str) -> str | None:
    """Return the content of `path` at `ref`, or None when it does not exist there."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"], capture_output=True, text=True, check=False
    )
    return result.stdout if result.returncode == 0 else None


def parse_image_ref(ref: str) -> tuple[str, str]:
    """Split `image[:tag][@sha256:...]` into (image, "tag[@sha256:<12 hex>]")."""
    name, _, digest = ref.partition("@")
    # A colon after the last slash is a tag; one before it is a registry port.
    head, sep, tag = name.rpartition(":")
    if not sep or "/" in tag:
        head, tag = name, ""
    value = tag
    if digest:
        algo, _, hexdigest = digest.partition(":")
        value += f"@{algo}:{hexdigest[:DIGEST_LEN]}"
    return head, value


def parse_dockerfile(text: str | None) -> dict[str, str]:
    """Return image/npm pins of a Dockerfile as {key: value}."""
    pins: dict[str, str] = {}
    for line in (text or "").splitlines():
        if match := FROM_RE.match(line):
            key, value = parse_image_ref(match.group(1))
            pins[key] = value
        elif (match := COPY_FROM_RE.match(line)) and (
            "/" in match.group(1) or ":" in match.group(1)
        ):
            key, value = parse_image_ref(match.group(1))
            pins[key] = value
        elif match := NPM_RE.search(line):
            args = (match.group(1) or match.group(2)).split("&&")[0]
            for pin in NPM_PIN_RE.finditer(args):
                pins[pin.group(1)] = pin.group(2)
    return pins


def parse_lock(text: str | None) -> list[dict]:
    """Return the `[[package]]` entries of a uv.lock, or [] when it is missing."""
    return tomllib.loads(text).get("package", []) if text else []


def lock_versions(packages: list[dict]) -> dict[str, str]:
    """Return {name: version} for every locked package."""
    return {p["name"]: p.get("version", "") for p in packages}


def _edges(package: dict) -> list[str]:
    """Names this package depends on (extras are expanded by `runtime_closure`)."""
    return [d["name"] for d in package.get("dependencies", [])]


def runtime_closure(packages: list[dict]) -> tuple[set[str], set[str], dict[str, str]]:
    """Return (direct, runtime, parent) for a lock; `parent[x]` is the first dependent of x."""
    by_name = {p["name"]: p for p in packages}
    roots = [p for p in packages if {"editable", "virtual"} & set(p.get("source", {}))]
    direct = {n for root in roots for n in _edges(root)}
    runtime: set[str] = set()
    parent: dict[str, str] = {}
    seen: set[tuple[str, tuple[str, ...]]] = set()
    stack = [(root["name"], dep) for root in roots for dep in root.get("dependencies", [])]
    while stack:
        via, dep = stack.pop()
        name = dep["name"]
        if name not in direct and (name not in parent or via < parent[name]):
            parent[name] = via
        key = (name, tuple(sorted(dep.get("extra", []))))
        if key in seen:
            continue
        seen.add(key)
        # An extra only widens what the target reaches, so it is expanded per extra set.
        target = by_name.get(name, {})
        extra_edges = [
            e
            for extra in dep.get("extra", [])
            for e in target.get("optional-dependencies", {}).get(extra, [])
        ]
        runtime.add(name)
        stack.extend((name, e) for e in target.get("dependencies", []) + extra_edges)
    return direct, runtime, parent


def classify(name: str, packages: list[dict]) -> str:
    """Return `direct`, `transitive via X` or `dev` for a package of this lock."""
    direct, runtime, parent = runtime_closure(packages)
    if name in direct:
        return "direct"
    if name in runtime:
        return f"transitive via `{parent[name]}`" if name in parent else "transitive"
    return "dev"


def diff(base: dict[str, str], head: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return sorted (name, from, to) rows; a missing side is the em dash."""
    return [
        (name, base.get(name, "—"), head.get(name, "—"))
        for name in sorted(base.keys() | head.keys())
        if base.get(name) != head.get(name)
    ]


def table(header: str, rows: list[tuple[str, ...]]) -> list[str]:
    """Render rows as a markdown table whose first column is the name."""
    cols = header.split("|")
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join((f"`{r[0]}`", *r[1:])) + " |" for r in rows]
    return lines


def render(
    base_ref: str,
    base_files: tuple[str | None, str | None],
    head_files: tuple[str | None, str | None],
) -> str:
    """Build the markdown section from (uv.lock, Dockerfile) texts at both refs."""
    base_pkgs, head_pkgs = parse_lock(base_files[0]), parse_lock(head_files[0])
    image_rows = diff(parse_dockerfile(base_files[1]), parse_dockerfile(head_files[1]))
    root_names = {
        p["name"]
        for p in base_pkgs + head_pkgs
        if {"editable", "virtual"} & set(p.get("source", {}))
    }
    changes = [
        row
        for row in diff(lock_versions(base_pkgs), lock_versions(head_pkgs))
        if row[0] not in root_names
    ]
    shipped: list[tuple[str, ...]] = []
    dev: list[str] = []
    for name, old, new in changes:
        kind = classify(name, head_pkgs if name in lock_versions(head_pkgs) else base_pkgs)
        if kind == "dev":
            dev.append(f"`{name}` {old} → {new}")
        else:
            shipped.append((name, old, new, kind))
    out = ["## Dependency changes", ""]
    if not (image_rows or shipped or dev):
        out.append(f"None: no image pin or locked package changed since {base_ref}.")
        return "\n".join(out) + "\n"
    if image_rows:
        out += ["**Image**", *table("Pin|From|To", image_rows), ""]
    if shipped:
        out += ["**Python (shipped)**", *table("Package|From|To|Kind", shipped), ""]
    if dev:
        out += ["**Dev only (not in the image):** " + ", ".join(dev), ""]
    return "\n".join(out).rstrip("\n") + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the dependency-changes section for BASE_REF..HEAD_REF."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_ref")
    parser.add_argument("head_ref")
    args = parser.parse_args(argv)
    # A mistyped or unfetched ref must fail, not read as "every file missing":
    # that would publish a table showing every dependency as newly added.
    for ref in (args.base_ref, args.head_ref):
        if not ref_exists(ref):
            print(f"Cannot compute dependency notes: unknown ref {ref!r}", file=sys.stderr)
            return 2
    files = ("uv.lock", "Dockerfile")
    base = tuple(git_show(args.base_ref, f) for f in files)
    head = tuple(git_show(args.head_ref, f) for f in files)
    sys.stdout.write(render(args.base_ref, base, head))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
