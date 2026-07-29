# PR #4: Tooling: post-merge PR summary script + /pr-summary skill
[View PR](https://github.com/Dezoxy/notification-digest/pull/4) · @Dezoxy · merged 2026-07-29 · `c6b0211` · `tooling-pr-summary -> main` · +655/-1 across 8 files

## Description
## Summary
- `scripts/pr_summary.py` — one command for a fast post-merge run-over: PR overview (+/- across files, merge commit), chronological commits, files by churn, **all** review threads with Codex findings severity-parsed (P1/P2/P3) and each finding paired with the fix-note reply, plus a review-round tally (requests vs. clean verdicts). `--json` for machine use. Self-contained stdlib+gh, same style as the watcher script.
- `.claude/skills/pr-summary/SKILL.md` — `/pr-summary <n>` runs the script and produces a four-section narrative: TL;DR / What changed / Review story / Worth a second look.
- CLAUDE.md workflow line for the post-merge step.

## Verification
- ruff clean, 30 tests unchanged and passing
- Real runs against merged PR #1 (all 10 findings with fix notes, '4 review requests, 1 clean verdict') and PR #2 (correctly reported zero Codex rounds — that PR predates the @codex review rule)

🤖 Generated with [Claude Code](https://claude.com/claude-code)

## Commits (8)
- [1f4de14](https://github.com/Dezoxy/notification-digest/commit/1f4de149e8ebd836a5eec556b058f7913eb3e923) Add post-merge PR summary tooling: script + /pr-summary skill
- [e17d4be](https://github.com/Dezoxy/notification-digest/commit/e17d4bed66ebe4926b0ff68885fc2b88e53518d1) Auto-generate per-PR summary markdown in docs/ on merge
- [7b81842](https://github.com/Dezoxy/notification-digest/commit/7b818425f1d579075df98c97c849176d83ef4f34) CLAUDE.md: option-1 hybrid — narrative via /pr-summary after every merge a session touches
- [08561fe](https://github.com/Dezoxy/notification-digest/commit/08561fe5a66c7fee3570cfaba0dcaa784d5cc4fc) Fix Codex findings: P0 severity, full pagination, push retry
- [59abf7f](https://github.com/Dezoxy/notification-digest/commit/59abf7f85dbbe9eb25db0d05f8fe756344abc714) Fix Codex findings: per-PR concurrency group, PR description in output
- [fb60527](https://github.com/Dezoxy/notification-digest/commit/fb605273e6fe0407b65183c35d5b618495960434) Fix Codex finding: fix notes only from replies after the Codex comment
- [9d66e06](https://github.com/Dezoxy/notification-digest/commit/9d66e06e6ca6e008d9a3bbfddeca6b0d38ae806a) Fix Codex finding: paginate commit and file lists
- [7b843d0](https://github.com/Dezoxy/notification-digest/commit/7b843d08e6deefbf2e5c1e22663eef9475f1ebdc) Fix Codex findings: null-safe author access, final pageInfo after drain

## Files (8)
- `scripts/pr_summary.py` `+525/-0`
- `.github/workflows/pr-summary.yml` `+69/-0`
- `.claude/skills/pr-summary/SKILL.md` `+40/-0`
- `CLAUDE.md` `+12/-0`
- `docs/pr-summaries/README.md` `+5/-0`
- `scripts/fetch-pr-review-threads.py` `+3/-1`
- `README.md` `+1/-0`
- `docs/pr-summaries/.gitkeep` `+0/-0`

## Review findings (12)
- **[P2]** Preserve every queued summary run — `.github/workflows/pr-summary.yml` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672209250)
  - fix: Fixed: concurrency group is now pr-summary-${{ number }} (per-PR), cancel-in-progress still false — queued summaries for different PRs no longer replace each other; the push rebase-retry loop handles the main race.
- **[P2]** Include the PR rationale in the skill input — `.claude/skills/pr-summary/SKILL.md:17` (resolved) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672209255)
  - fix: Fixed: both human and markdown modes now print a Description section (verbatim PR body, no truncation) right after the header, and the skill's TL;DR step names it as the rationale source.
- **[P2]** Treat P0 findings as the highest severity — `scripts/pr_summary.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672019601)
  - fix: Fixed in 08561fe: SEVERITY_RE now matches P[0123] and severity_sort_key orders P0 first (verified with a synthetic P2/P0/None/P1/P3 sort → P0,P1,P2,P3,unlabeled).
- **[P2]** Paginate issue comments before counting review rounds — `scripts/pr_summary.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672019604)
  - fix: Fixed in 08561fe: issue comments now fetched with gh api --paginate --slurp and flattened (page lists merged). Verified against live PR 3: 13/13 @codex review requests counted, matching gh api --paginate directly.
- **[P2]** Paginate comments within each review thread — `scripts/pr_summary.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672019608)
  - fix: Fixed in 08561fe: thread comments raised to first:100 with nested pageInfo, plus complete_thread_comments() draining any thread whose connection still reports hasNextPage via a node(id:) follow-up query — no thread's replies can be silently truncated.
- **[P2]** Paginate issue comments before counting review rounds — `scripts/pr_summary.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672063597)
  - fix: Fixed in 08561fe (same fix as the sibling thread): --paginate --slurp + flatten before count_review_rounds.
- **[P2]** Fetch every comment in long review threads — `scripts/pr_summary.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672063605)
  - fix: Fixed in 08561fe (same fix as the sibling thread): first:100 + nested pageInfo + per-thread drain loop; Codex-thread classification and latest_human_reply now see every comment.
- **[P2]** Rebase before pushing generated summaries — `.github/workflows/pr-summary.yml` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672063608)
  - fix: Fixed in 08561fe: the push now runs a bounded 3-attempt loop — on rejection, git pull --rebase origin main and retry; the job fails loudly only if still rejected after 3 tries. Closely timed merges each land their summary.
- **[P2]** Ignore comments that precede the Codex finding — `scripts/pr_summary.py:291` (resolved) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672286236)
  - fix: Fixed: latest_human_reply anchors on the same Codex comment the finding is built from and considers only strictly-later non-Codex comments, returning None when none exist. Verified against live PR 3 data (Codex-opened threads keep all 22 fix notes) plus synthetic human-opened-thread checks in both directions.
- **[P2]** Paginate the commit and file lists — `scripts/pr_summary.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672427330)
  - fix: Fixed: commits and files now come from the paginated REST endpoints (gh api --paginate --slurp, flattened, mapped to the previous field shapes) like issue comments and threads already do — no more silent 100-item cap. Verified live against PR 3 (all 16 commits) and PR 1 (markdown links intact).
- **[P2]** Handle null review-comment authors — `scripts/pr_summary.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672496371)
  - fix: Fixed: every author/user login access is null-safe ((x.get('author') or {}).get('login')) across first_codex_comment, latest_human_reply, count_review_rounds, both print modes, and the PR-overview author; deleted accounts render as 'ghost' per GitHub convention. Verified with a null-author repro (no AttributeError) and a live PR 3 run.
- **[P2]** Update pageInfo after paginating thread comments — `scripts/pr_summary.py:230` (resolved) — [thread](https://github.com/Dezoxy/notification-digest/pull/4#discussion_r3672496379)
  - fix: Fixed: complete_thread_comments now writes the final page's pageInfo back onto the thread after draining, so --json reports hasNextPage: false with the final cursor once nodes are complete.

## Review rounds
8 review requests, 1 clean verdict

*Generated by scripts/pr_summary.py*
