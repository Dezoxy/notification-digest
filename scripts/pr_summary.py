#!/usr/bin/env python3
"""Print a fast-to-scan, post-merge summary of a GitHub pull request.

Gathers PR overview, commits, files, review threads (including resolved and
outdated, all authors), and issue comments via the `gh` CLI, then renders a
human-scannable digest: what changed, what Codex found, and how it was
addressed.

Usage: uv run python scripts/pr_summary.py <PR_NUMBER> [--repo OWNER/REPO] [--json|--markdown]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

CODEX_LOGINS = {"chatgpt-codex-connector", "chatgpt-codex-connector[bot]"}
SEVERITY_RE = re.compile(r"\b(P[0123])\s+Badge\b", re.IGNORECASE)
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
    # --paginate alone concatenates each page's JSON array back-to-back, which
    # is not valid single-document JSON; --slurp wraps the pages into one
    # outer array (a list of per-page lists), so flatten it here.
    pages: list[list[dict[str, Any]]] = gh_json(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{owner}/{repo}/issues/{number}/comments",
        ]
    )
    return [comment for page in pages for comment in page]


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
              comments(first: 100) {
                pageInfo {
                  hasNextPage
                  endCursor
                }
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


def fetch_thread_comments_page(
    thread_id: str, after: str | None
) -> dict[str, Any]:
    query = """
    query($id: ID!, $after: String) {
      node(id: $id) {
        ... on PullRequestReviewThread {
          comments(first: 100, after: $after) {
            pageInfo {
              hasNextPage
              endCursor
            }
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
    """
    command = [
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-f",
        f"id={thread_id}",
    ]
    if after is not None:
        command.extend(["-f", f"after={after}"])
    return gh_json(command)


def complete_thread_comments(thread: dict[str, Any]) -> None:
    """Fetch any comments past the first page for a single review thread.

    ``comments(first: 100)`` covers realistic threads, but this walks any
    remaining pages via a follow-up node() query so a thread with >100 replies
    never silently drops comments (which would break latest_human_reply and
    Codex-thread classification).
    """
    comments = thread["comments"]
    page_info = comments["pageInfo"]
    while page_info["hasNextPage"]:
        data = fetch_thread_comments_page(thread["id"], page_info["endCursor"])
        next_comments = data["data"]["node"]["comments"]
        comments["nodes"].extend(next_comments["nodes"])
        page_info = next_comments["pageInfo"]


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
    for thread in threads:
        if thread["comments"]["pageInfo"]["hasNextPage"]:
            complete_thread_comments(thread)
    return threads


def gather(number: int, repo: str | None) -> dict[str, Any]:
    owner, name = repo_parts(repo)
    return {
        "owner": owner,
        "repo": name,
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
    """Return the latest non-Codex comment that replies to the Codex finding.

    Only comments strictly after ``first_codex_comment`` (the finding itself)
    are eligible, so a human comment that merely precedes the finding (e.g.
    the human-authored comment that opened the thread, with Codex replying
    later) is never mistaken for a reply to it. Returns None if no non-Codex
    comment occurs after the first Codex comment.
    """
    comments = thread["comments"]["nodes"]
    codex_comment = first_codex_comment(thread)
    if codex_comment is None:
        return None
    codex_index = comments.index(codex_comment)
    for comment in reversed(comments[codex_index + 1 :]):
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
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    return order.get(entry["severity"] or "", 4), entry["thread"]["id"]


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
                "url": codex_comment.get("url"),
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

    body = (overview.get("body") or "").strip()
    print("\nDescription:")
    if body:
        for line in body.splitlines():
            print(f"  {line}")
    else:
        print("  (none)")

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


def merged_date(merged_at: str | None) -> str:
    return merged_at.split("T", 1)[0] if merged_at else "unknown"


def print_markdown(data: dict[str, Any]) -> None:
    overview = data["overview"]
    owner = data["owner"]
    repo = data["repo"]
    commits = data["commits"]
    files = data["files"]
    findings, non_codex_threads = build_review_findings(data["threads"])
    requests, clean_verdicts = count_review_rounds(data["issue_comments"])

    author = overview["author"]["login"]
    merge_sha = (overview.get("mergeCommit") or {}).get("oid", "")[:7]
    merged = merged_date(overview.get("mergedAt"))

    print(f"# PR #{overview['number']}: {overview['title']}")
    print(
        f"[View PR]({overview['url']}) · @{author} · merged {merged} · "
        f"`{merge_sha}` · `{overview['headRefName']} -> {overview['baseRefName']}` · "
        f"+{overview['additions']}/-{overview['deletions']} across {overview['changedFiles']} files"
    )

    body = (overview.get("body") or "").strip()
    print("\n## Description")
    print(body if body else "*(none)*")

    print(f"\n## Commits ({len(commits)})")
    for commit in commits:
        sha = commit["oid"][:7]
        url = f"https://github.com/{owner}/{repo}/commit/{commit['oid']}"
        print(f"- [{sha}]({url}) {commit['messageHeadline']}")

    print(f"\n## Files ({len(files)})")
    for f in sorted(files, key=lambda f: f["additions"] + f["deletions"], reverse=True):
        print(f"- `{f['path']}` `+{f['additions']}/-{f['deletions']}`")

    print(f"\n## Review findings ({len(findings)})")
    if not findings:
        print("- No Codex review threads.")
    for entry in findings:
        severity = f"**[{entry['severity']}]** " if entry["severity"] else ""
        status = "resolved" if entry["resolved"] else "unresolved"
        if entry["outdated"]:
            status += ", outdated"
        line_part = f":{entry['line']}" if entry["line"] is not None else ""
        thread_url = entry["url"] or ""
        print(
            f"- {severity}{entry['title']} — `{entry['path']}{line_part}` "
            f"({status}) — [thread]({thread_url})"
        )
        if entry["fix_note"]:
            print(f"  - fix: {entry['fix_note']}")
    for thread in non_codex_threads:
        authors = sorted(
            {c.get("author", {}).get("login") or "unknown" for c in thread["comments"]["nodes"]}
        )
        line_part = f":{thread['line']}" if thread["line"] is not None else ""
        print(f"- `{thread['path']}{line_part}` — authors: {', '.join(authors)}")

    plural = "s" if clean_verdicts != 1 else ""
    print("\n## Review rounds")
    print(f"{requests} review requests, {clean_verdicts} clean verdict{plural}")

    print("\n*Generated by scripts/pr_summary.py*")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", type=int, help="PR number")
    parser.add_argument("--repo", help="GitHub repo as OWNER/REPO; defaults to gh current repo")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Print raw gathered JSON instead")
    output.add_argument(
        "--markdown", action="store_true", help="Print a standalone markdown document instead"
    )
    args = parser.parse_args()

    data = gather(args.pr, args.repo)
    if args.json:
        print(json.dumps(data, indent=2))
    elif args.markdown:
        print_markdown(data)
    else:
        print_human(data)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
