#!/usr/bin/env python3
"""Print a fast-to-scan, post-merge summary of a GitHub pull request.

Gathers PR overview, commits, files, review threads (including resolved and
outdated, all authors), and issue comments via the `gh` CLI, then renders a
human-scannable digest: what changed, what Codex found, and how it was
addressed.

Usage: uv run python scripts/pr_summary.py <PR_NUMBER> [--repo OWNER/REPO] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

CODEX_LOGINS = {"chatgpt-codex-connector", "chatgpt-codex-connector[bot]"}
SEVERITY_RE = re.compile(r"\b(P[123])\s+Badge\b", re.IGNORECASE)
CLEAN_VERDICT = "didn't find any major issues"


def run_gh(args: list[str]) -> str:
    result = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result.stdout


def gh_json(args: list[str]) -> Any:
    return json.loads(run_gh(args))


def repo_parts(repo: str | None) -> tuple[str, str]:
    if repo:
        owner, name = repo.split("/", 1)
        return owner, name
    data = gh_json(["repo", "view", "--json", "owner,name"])
    return data["owner"]["login"], data["name"]


def fetch_overview(number: int, repo: str | None) -> dict[str, Any]:
    args = [
        "pr",
        "view",
        str(number),
        "--json",
        "number,title,body,url,author,state,mergedAt,mergeCommit,"
        "baseRefName,headRefName,additions,deletions,changedFiles",
    ]
    if repo:
        args.extend(["--repo", repo])
    return gh_json(args)


def fetch_commits(number: int, repo: str | None) -> list[dict[str, Any]]:
    args = ["pr", "view", str(number), "--json", "commits"]
    if repo:
        args.extend(["--repo", repo])
    return gh_json(args)["commits"]


def fetch_files(number: int, repo: str | None) -> list[dict[str, Any]]:
    args = ["pr", "view", str(number), "--json", "files"]
    if repo:
        args.extend(["--repo", repo])
    return gh_json(args)["files"]


def fetch_issue_comments(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    return gh_json(["api", f"repos/{owner}/{repo}/issues/{number}/comments"])


def fetch_thread_page(owner: str, repo: str, number: int, after: str | None) -> dict[str, Any]:
    query = """
    query($owner: String!, $repo: String!, $number: Int!, $after: String) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 100, after: $after) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              id
              isResolved
              isOutdated
              path
              line
              comments(first: 20) {
                nodes {
                  id
                  url
                  author { login }
                  body
                  createdAt
                }
              }
            }
          }
        }
      }
    }
    """
    command = [
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-f",
        f"owner={owner}",
        "-f",
        f"repo={repo}",
        "-F",
        f"number={number}",
    ]
    if after is not None:
        command.extend(["-f", f"after={after}"])
    return gh_json(command)


def fetch_threads(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        data = fetch_thread_page(owner, repo, number, after)
        review_threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        threads.extend(review_threads["nodes"])
        page_info = review_threads["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    return threads


def gather(number: int, repo: str | None) -> dict[str, Any]:
    owner, name = repo_parts(repo)
    return {
        "overview": fetch_overview(number, repo),
        "commits": fetch_commits(number, repo),
        "files": fetch_files(number, repo),
        "threads": fetch_threads(owner, name, number),
        "issue_comments": fetch_issue_comments(owner, name, number),
    }


def is_codex(login: str | None) -> bool:
    return login in CODEX_LOGINS


def first_codex_comment(thread: dict[str, Any]) -> dict[str, Any] | None:
    for comment in thread["comments"]["nodes"]:
        if is_codex(comment.get("author", {}).get("login")):
            return comment
    return None


def latest_human_reply(thread: dict[str, Any]) -> dict[str, Any] | None:
    for comment in reversed(thread["comments"]["nodes"]):
        if not is_codex(comment.get("author", {}).get("login")):
            return comment
    return None


def strip_markup(text: str) -> str:
    # Drop badge images/sub tags and leading/trailing markdown emphasis noise.
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"</?sub>", "", text)
    text = text.replace("**", "").strip()
    return text


def finding_title(body: str) -> str:
    first_line = body.strip().splitlines()[0] if body.strip() else ""
    return strip_markup(first_line)


def severity_of(body: str) -> str | None:
    match = SEVERITY_RE.search(body)
    return match.group(1).upper() if match else None


def first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


def severity_sort_key(entry: dict[str, Any]) -> tuple[int, str]:
    order = {"P1": 0, "P2": 1, "P3": 2}
    return order.get(entry["severity"] or "", 3), entry["thread"]["id"]


def build_review_findings(
    threads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    codex_findings = []
    non_codex_threads = []
    for thread in threads:
        codex_comment = first_codex_comment(thread)
        if codex_comment is None:
            non_codex_threads.append(thread)
            continue
        reply = latest_human_reply(thread)
        codex_findings.append(
            {
                "thread": thread,
                "severity": severity_of(codex_comment["body"]),
                "title": finding_title(codex_comment["body"]),
                "path": thread["path"],
                "line": thread["line"],
                "resolved": thread["isResolved"],
                "outdated": thread["isOutdated"],
                "fix_note": first_line(reply["body"]) if reply else None,
            }
        )
    codex_findings.sort(key=severity_sort_key)
    return codex_findings, non_codex_threads


def count_review_rounds(issue_comments: list[dict[str, Any]]) -> tuple[int, int]:
    requests = sum(
        1 for c in issue_comments if c.get("body", "").strip().startswith("@codex review")
    )
    clean_verdicts = sum(
        1
        for c in issue_comments
        if c.get("user", {}).get("login") in CODEX_LOGINS
        and CLEAN_VERDICT in c.get("body", "").lower()
    )
    return requests, clean_verdicts


def print_human(data: dict[str, Any]) -> None:
    overview = data["overview"]
    commits = data["commits"]
    files = data["files"]
    findings, non_codex_threads = build_review_findings(data["threads"])
    requests, clean_verdicts = count_review_rounds(data["issue_comments"])

    author = overview["author"]["login"]
    state = overview["state"]
    print(f"PR #{overview['number']}: {overview['title']}")
    print(overview["url"])
    state_line = f"author={author} state={state}"
    if overview.get("mergedAt"):
        sha = (overview.get("mergeCommit") or {}).get("oid", "")[:7]
        state_line += f" mergedAt={overview['mergedAt']} mergeCommit={sha}"
    print(state_line)
    print(f"{overview['headRefName']} -> {overview['baseRefName']}")
    print(
        f"+{overview['additions']}/-{overview['deletions']} across {overview['changedFiles']} files"
    )

    print(f"\nCommits ({len(commits)}):")
    for commit in commits:
        print(f"  {commit['oid'][:7]}  {commit['messageHeadline']}")

    print(f"\nFiles ({len(files)}):")
    for f in sorted(files, key=lambda f: f["additions"] + f["deletions"], reverse=True):
        print(f"  {f['path']}  +{f['additions']}/-{f['deletions']}")

    print(f"\nReview findings ({len(findings)} threads):")
    if not findings:
        print("  No Codex review threads.")
    for entry in findings:
        severity = f"[{entry['severity']}] " if entry["severity"] else ""
        status = "resolved" if entry["resolved"] else "unresolved"
        if entry["outdated"]:
            status += ", outdated"
        line_part = f":{entry['line']}" if entry["line"] is not None else ""
        print(f"  {severity}{entry['title']}")
        print(f"    {entry['path']}{line_part}  ({status})")
        if entry["fix_note"]:
            print(f"    fix: {entry['fix_note']}")

    if non_codex_threads:
        print(f"\nOther review threads ({len(non_codex_threads)}):")
        for thread in non_codex_threads:
            authors = sorted(
                {c.get("author", {}).get("login") or "unknown" for c in thread["comments"]["nodes"]}
            )
            line_part = f":{thread['line']}" if thread["line"] is not None else ""
            print(f"  {thread['path']}{line_part}  authors={','.join(authors)}")

    plural = "s" if clean_verdicts != 1 else ""
    print(f"\nReview rounds: {requests} review requests, {clean_verdicts} clean verdict{plural}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", type=int, help="PR number")
    parser.add_argument("--repo", help="GitHub repo as OWNER/REPO; defaults to gh current repo")
    parser.add_argument("--json", action="store_true", help="Print raw gathered JSON instead")
    args = parser.parse_args()

    data = gather(args.pr, args.repo)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_human(data)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
