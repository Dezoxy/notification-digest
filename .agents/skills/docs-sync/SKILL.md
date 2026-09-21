---
name: docs-sync
description: Audit the branch diff for documentation it falsifies and fix those docs in the same branch — run before opening or updating any PR
---

Before a PR leaves this repo, the documentation must still be TRUE. This
skill is a diff-driven audit: find every doc claim the branch falsifies,
fix it in the same branch, and prove the fixes rather than asserting them.

It exists because drift here is not hypothetical: one refactor session left
five separate lies behind — a file header pointing at functions that had
moved ("keyMatches below"), a README contract stated five times that the
code no longer honored ("single-file Worker"), a PLAN.md layout tree rooted
at a folder name that no longer existed, an install command wrangler v4
rejects (`d1 execute` without `--remote`), and a rationale in another repo's
docs explaining why a worker lives where it no longer lives.

## Steps

1. Get the real change surface — not what you remember doing:

   ```
   git diff main...HEAD --stat
   git diff main...HEAD
   ```

   From it, list what the branch changed in these categories: paths
   (files/dirs added, removed, renamed, moved), commands and flags, names
   (functions, modules, env vars, scripts), counts and shapes (tables,
   routes, test counts, module lists), behavior and contracts (what a
   component does, where it deploys, what gates it).

2. Sweep the doc surface for claims that touch anything in that list. The
   surface is bigger than `docs/` — most load-bearing claims here live
   elsewhere:

   | Where | What it claims |
   | --- | --- |
   | `README.md` | architecture diagram, run-mode table, delivery channels, deployment chain, repo pointers |
   | `CLAUDE.md` + `AGENTS.md` | conventions, verification commands, workflow — **twins: check them against each other too** (they drift independently; where they state the same rule, the wording must agree) |
   | `PLAN.md` | the §3 layout tree, phase status |
   | `workers/news-site/README.md` | trust model, routes, deploy steps, source layout table |
   | `workers/news-site/test/README.md` | test contract, verification recipes |
   | `workers/news-site/worker.js` file header | the authoritative URL grammar and source map |
   | `workers/news-site/schema.sql` comments | which module owns each validation |
   | `.env.example` | every env var, matching `digest/config.py` |
   | `docs/` | incidents, design guidance — usually historical, fix only if the diff falsifies a live claim |

   Cheap sweeps that catch most drift: grep the docs for every path,
   command, and name the diff REMOVED or RENAMED. A doc that still mentions
   a deleted thing is lying.

3. Verify claims, don't re-read them. For each doc statement in the blast
   radius, check it against the tree the way a skeptic would:

   - a path the doc names -> `ls` it
   - a command the doc gives -> run it (or `--dry-run`/`--help` it if it
     mutates something)
   - a list or table -> count the real thing and compare
   - a "twins" rule -> diff the two files' overlapping sections
   - an imported architecture document -> `make view`, open the
     Documentation tab, and confirm its title appears in both the page and
     the navigation (the PDF normalises heading levels and cannot show this;
     `make docs` rejects a hidden `#` title)

4. Fix what the diff falsified, in this branch. Scope discipline:

   - Fix drift this branch causes, plus outright falsehoods found in
     passing (note the latter in the PR body — they're a bonus, not the
     point).
   - Do NOT restyle prose, reorganize docs, or "improve" things that are
     merely imperfect. A docs-sync commit that needs its own review defeats
     itself.
   - Historical text (roadmaps, incident write-ups, PLAN.md phase notes)
     describes what WAS true — annotate or leave it; never rewrite history
     to match the present.

5. Mind the two doc locations with side effects:

   - Comments inside `workers/news-site/src/css.js` and `src/client.js`
     SHIP in every page. Editing them changes rendered bytes: run
     `npm run golden` there and let the golden diff be the review artifact.
     Never introduce a backtick or `${` in those files.
   - `docs/pr-summaries/` is CI-generated — never hand-edit it.

6. Close with proof in the PR body: one line per doc touched saying what
   was false and what verified the fix ("README table listed 16 modules,
   `ls src/*.js` counts 17 — table updated"). If the audit found nothing to
   fix, say that explicitly — "docs audited against the diff, no drift" is
   a real result and reviewable claim, silence is not.
