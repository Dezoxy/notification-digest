---
name: pr-summary
description: Summarize a merged PR — commits, files, review findings — for a quick human run-over
---

Produce a post-merge run-over summary for a pull request in this repo.

## Steps

1. Determine the PR number. If the user invoked this as `/pr-summary <n>` or
   otherwise gave a number, use it. Otherwise ask for the PR number.
2. Run `uv run python scripts/pr_summary.py <n>` (the script defaults to the
   current repo via `gh`; only pass `--repo OWNER/REPO` if the user names a
   different repo).
3. Using the raw output as your source of truth, write a compact narrative
   with EXACTLY these four sections, in this order:

   - **TL;DR** — 2-3 sentences: what the PR did and why.
   - **What changed** — grouped by area (e.g. "collector", "state/DB",
     "tests", "config"), not a per-file listing.
   - **Review story** — how many Codex review rounds happened (review
     requests vs. clean verdicts), then the findings that mattered most and
     how they were fixed, ordered by severity (P1 before P2 before P3,
     unlabeled last).
   - **Worth a second look** — anything the reader should manually verify:
     security-relevant changes, schema/migration changes, new dependencies,
     or unresolved/outdated findings that never got a follow-up. Derive this
     from the data — do not invent risks that aren't backed by the output.

4. Keep the whole narrative under about 40 lines. Include the PR URL (from
   the script's header line) so it's one click away.
5. Do not re-paste the raw script output in full — synthesize it. Quote a
   finding's title or fix note only when it's the clearest way to make a
   point.
