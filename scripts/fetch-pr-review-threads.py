#!/usr/bin/env python3
"""Print unresolved GitHub pull request review threads (Codex findings watcher).

Ported from secmes scripts/fetch-pr-review-threads.py. Defaults differ from the
original: this repo only cares about Codex findings, so --codex-only and
--actionable-only behavior is the DEFAULT (unresolved, non-outdated threads
with a Codex comment). Use --all-authors / --include-outdated to widen.

Usage: uv run python scripts/fetch-pr-review-threads.py [PR_NUMBER]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from typing import Any

CODEX_LOGIN = "chatgpt-codex-connector"


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


def current_pr_number() -> int:
    data = gh_json(["pr", "view", "--json", "number"])
    return int(data["number"])


def fetch_thread_page(owner: str, repo: str, number: int, after: str | None) -> dict[str, Any]:
    query = """
    query($owner: String!, $repo: String!, $number: Int!, $after: String) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          number
          url
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


def fetch_threads(owner: str, repo: str, number: int) -> dict[str, Any]:
    pull_request: dict[str, Any] | None = None
    threads: list[dict[str, Any]] = []
    after: str | None = None

    while True:
        data = fetch_thread_page(owner, repo, number, after)
        page_pull_request = data["data"]["repository"]["pullRequest"]
        if pull_request is None:
            pull_request = {
                "number": page_pull_request["number"],
                "url": page_pull_request["url"],
                "reviewThreads": {"nodes": []},
            }

        review_threads = page_pull_request["reviewThreads"]
        threads.extend(review_threads["nodes"])
        page_info = review_threads["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]

    if pull_request is None:
        raise RuntimeError("pull request not found")
    pull_request["reviewThreads"]["nodes"] = threads
    return {"data": {"repository": {"pullRequest": pull_request}}}


def has_codex_comment(thread: dict[str, Any]) -> bool:
    return any(
        comment.get("author", {}).get("login") == CODEX_LOGIN
        for comment in thread["comments"]["nodes"]
    )


def thread_url(thread: dict[str, Any]) -> str | None:
    comments = thread["comments"]["nodes"]
    return comments[-1]["url"] if comments else None


def filtered_threads(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    owner, repo = repo_parts(args.repo)
    number = args.pr or current_pr_number()
    data = fetch_threads(owner, repo, number)
    pull_request = data["data"]["repository"]["pullRequest"]
    threads = pull_request["reviewThreads"]["nodes"]

    selected = []
    for thread in threads:
        if not args.include_resolved and thread["isResolved"]:
            continue
        if not args.include_outdated and thread["isOutdated"]:
            continue
        if not args.all_authors and not has_codex_comment(thread):
            continue
        selected.append(thread)
    return pull_request, selected


def print_human(pull_request: dict[str, Any], threads: list[dict[str, Any]]) -> None:
    print(f"PR #{pull_request['number']}: {pull_request['url']}")
    if not threads:
        print("No matching review threads.")
        return

    for thread in threads:
        print()
        print(
            f"Thread {thread['id']} "
            f"resolved={thread['isResolved']} outdated={thread['isOutdated']} "
            f"path={thread['path']} line={thread['line']}"
        )
        url = thread_url(thread)
        if url:
            print(f"URL: {url}")
        for comment in thread["comments"]["nodes"]:
            author = comment.get("author", {}).get("login") or "unknown"
            body = textwrap.indent(comment["body"].strip(), "    ")
            print(f"- {author} at {comment['createdAt']} ({comment['id']}):")
            print(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", nargs="?", type=int, help="PR number; defaults to current branch PR")
    parser.add_argument("--repo", help="GitHub repo as OWNER/REPO; defaults to gh current repo")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of human text")
    parser.add_argument("--include-resolved", action="store_true", help="Include resolved threads")
    parser.add_argument(
        "--all-authors",
        action="store_true",
        help="Include threads from all reviewers (default: Codex threads only)",
    )
    parser.add_argument(
        "--include-outdated",
        action="store_true",
        help="Include outdated threads (default: only actionable current-diff threads)",
    )
    parser.add_argument(
        "--exit-code", action="store_true", help="Exit 1 if any matching thread exists"
    )
    args = parser.parse_args()

    pull_request, threads = filtered_threads(args)
    if args.json:
        print(
            json.dumps(
                {
                    "pull_request": {
                        "number": pull_request["number"],
                        "url": pull_request["url"],
                    },
                    "threads": threads,
                },
                indent=2,
            )
        )
    else:
        print_human(pull_request, threads)

    return 1 if args.exit_code and threads else 0


if __name__ == "__main__":
    raise SystemExit(main())
