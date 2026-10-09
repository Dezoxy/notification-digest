# digest

Personal notification-digest service. Every 6 hours it collects new items from
the owner's own Telegram groups (Telethon, MTProto user session) and own
X/Twitter notifications (twifork — a maintained twikit fork that still imports
as `twikit` — cookie session, unofficial API, ToS risk accepted by the owner),
plus RSS/Reddit/Polymarket/Hacker News collectors, tracks state in SQLite,
summarizes new items with the Claude CLI headless (`claude -p`, falling back to
OpenRouter models when that call fails), and delivers a
structured digest with deep links. Two lanes run outside that cascade on their
own timers and into their own Telegram topics: `patreon` (one paid post per
message) and `positions` (the tracked-project tracker, silent when nothing
material happened). A third mode, `relay`, forwards public-channel posts
verbatim into a hub topic — no summarization, no state beyond a cursor.
See PLAN.md for the full plan and current phase status.

Delivery is multi-channel (PR #25). On the real VM the live channels are a
**Telegram TL;DR ping** and the **news site**; **email is disabled**
(`myapps_digest_email_enabled: false` in the homelab host_vars). Email is still
fully implemented and remains the role DEFAULT, so a host that never configures
the other two keeps working — the app refuses to start with every channel
disabled. Don't describe this service as "emails a digest": that stopped being
true at the multi-channel cutover.

## Conventions

- Python 3.12, dependency management via uv (`uv sync`, `uv run ...`).
- Lint and format with ruff; run tests with pytest.
- Those two rules cover the Python service. `workers/news-site/` is a
  Cloudflare Worker (JavaScript, no build step) with its own conventions:
  npm, prettier, `node --test`. Don't apply ruff/pytest expectations to it,
  and don't add Python tooling that walks into it.
- Type hints on all public functions.
- Small modules, no speculative abstractions — this is a single-owner, single-
  deployment service, not a library.
- Config only via environment variables, read exclusively in `config.py`.
  Never call `os.environ` / `os.getenv` from other modules — pass values in.
- All timestamps are stored in UTC. Convert to Europe/Budapest only at
  render/email time, never in storage or state comparisons.
- Architecture lives in `docs/architecture/`: a Structurizr model, the ADRs and
  the knowledge base around them. Use the `architecture-views` skill
  (.agents/skills/architecture-views/SKILL.md) for model and view work, and
  `architecture-docs` (.agents/skills/architecture-docs/SKILL.md) for the prose.
  Both are copied from development-base, which stays their canonical home;
  `model/styles-shared.dsl` is canonical there and is never edited here.
  A view is split at its budget, never enlarged without rendered evidence
  recorded in the view register.
- **The Documentation tab and the PDF are the same document.** Structurizr
  imports `docs/architecture/overview/` and nothing else — `!docs` does not
  recurse into subfolders — and the PDF is built from that same folder, so the
  architecture description lives there: `01-*.md` carries the single `#`, every
  later file is a `## ` section, and the numeric prefixes are what order them.
  `pdf-sections.txt` is deliberately empty; a line in it is a page the PDF has
  and the tab does not. Grow a reading path instead.
- **The PDF is the deliverable, and it is complete.** It is handed to people at
  different levels, so it must stand alone: no section may require opening a
  link to be understood. `overview/` therefore holds the audience reading paths
  as real files, and **symlinks every register into itself** — `NN-name.md ->
  ../<folder>/<name>.md`. Each document is authored once, in its own folder,
  where its IDs are owned; the symlink only puts it in the imported folder and
  fixes its order. `check_overview_complete` fails when a register has no
  symlink, because an unlinked register is invisible in the artifact people are
  handed while still looking present in the repository.
- Keep every `![alt](embed:Key)` on ONE line. Structurizr tolerates a wrapped
  image; the PDF builder matches an embed per line, so a wrapped one drops the
  view out of the document into the appendix, and the build still exits 0.
- Title documents under `docs/architecture/overview/` with `##`, not `#`.
  Structurizr hides a level-1 heading from the page and the navigation, and the
  PDF will not show you the problem. `make docs` enforces it.
- Files in `overview/` link to the registers by **absolute** URL
  (`https://github.com/Dezoxy/notification-digest/blob/main/...`), because a
  relative link does not resolve inside the rendered tab. `make docs` checks
  those resolve on disk, so they are not exempt from the audit.

## Hard rules

- Never commit or log secrets, cookies, or session strings (Telegram session,
  X cookies, SMTP password, Claude credentials, etc.). Secrets come from Azure
  Key Vault as env vars at deploy time.
- Tests must never call the real Telegram or X APIs, or send real email. Mock
  at the collector boundary (`collectors/telegram.py`, `collectors/x.py`) and
  the emailer boundary (`emailer.py`).
- The SQLite state contract (last-seen ID per source) must stay idempotent: a
  re-run over the same window must not duplicate items or send duplicate
  emails.
- The X collector must stay behind the `X_ENABLED` flag and must back off
  (not retry-loop) on auth errors — twikit is unofficial and accounts can get
  flagged or locked.

## Workflow

- Implementation code and doc drafts are written by Sonnet subagents (Agent
  tool, model sonnet); the main session reviews every line and applies
  corrections itself before anything counts as done.
- All work happens on feature branches; merge to main only via PR. Direct
  pushes to main are blocked by `.githooks/pre-push` (activated per-clone
  with `git config core.hooksPath .githooks`); `ALLOW_MAIN_PUSH=1` exists
  only for bootstrap.
- Dependency bumps arrive as Renovate PRs (`renovate.json`), one at a time,
  weekday mornings. The /docs-sync and /pr-summary rules below are written
  for PRs a session drives and do not apply to them -- a bot cannot run
  either. Review the diff, let CI verify it, merge.
- `twifork` is the standing exception: it is tracked, but gated behind the
  Dependency Dashboard (`dependencyDashboardApproval`) instead of opening a
  PR on its own, and its PR carries a `supply-chain-audit` label. It is a
  single-maintainer fork handling a live X session cookie, so each bump
  needs a hand audit of the diff against the previous pin -- no new network
  hosts, no new eval/exec/base64/subprocess/pickle -- before it can be
  trusted. Ticking the dashboard box IS the decision to do that audit, so
  the bump can never arrive looking routine. See the pin's comment in
  `pyproject.toml` and the rule's own `description` in `renovate.json`.
- Repo: github.com/Dezoxy/notification-digest (private). Container image:
  `ghcr.io/dezoxy/notification-digest`.
- Before opening or updating any PR, run /docs-sync
  (`.agents/skills/docs-sync/SKILL.md`): audit the branch diff for
  documentation it falsifies — README, PLAN.md's layout tree, this file and
  its twin, the worker's READMEs and file header — and fix those docs in the
  same branch, with the proof in the PR body. A PR that changes paths,
  commands, or behavior without the matching doc adjustment is incomplete.
- After opening or updating a PR, wait for the Codex review, then run
  `uv run python scripts/fetch-pr-review-threads.py <pr>` (ported from secmes;
  defaults to unresolved, actionable, Codex-only threads) and address every
  finding it reports — fix or explicitly rebut, never ignore. Only Codex
  findings gate PRs; other reviewers' threads are informational.
- After merging a PR, `/pr-summary <n>` (or
  `uv run python scripts/pr_summary.py <n>`) produces the post-merge run-over
  summary. A markdown copy also lands automatically at
  `docs/pr-summaries/pr-<n>.md` on merge (CI: `.github/workflows/pr-summary.yml`,
  `scripts/pr_summary.py <n> --markdown`) — the skill/script above remain the
  way to get the narrative, human-synthesized version on demand.
- Whenever a session is involved in a merge (it drove the PR, or the owner
  reports merging one), it must run /pr-summary for that PR and deliver the
  four-section narrative to the owner in its reply. The CI file stays
  data-only; the narrative lives in the conversation unless the owner asks to
  persist it, in which case prepend it to `docs/pr-summaries/pr-<n>.md` via a
  small docs PR (direct pushes to main stay hook-blocked).

## Agent harness

- The `ecc@ecc` plugin was disabled globally (user scope) on 2026-09-30, so
  `ecc:<name>` agents, skills and commands are no longer reachable in sessions
  here. The harness is now curated in the owner's development base,
  `~/Developer/development-base` (GitHub `Dezoxy/development-base`, the renamed
  architecture-base). It is copied from the ECC fork at
  `~/Developer/agent-base` (`Dezoxy/ECC`), which only tracks upstream
  `affaan-m/ECC` and is never edited by hand;
  `development-base/.claude/ecc/SOURCE.md` records provenance and every hand
  edit.
- A repository adopts the harness by copying it from the base, following the
  base README's "Agent harness" section. This repo has NOT adopted it yet.
- Do NOT run ECC's `/project-init` against this repo. It was evaluated on
  2026-09-20 and rejected — `install-apply.js --target claude-project` plans
  **825 operations**, copying all 22 languages' rule files, ~130 skill
  directories, 68 agents and 160 script libs into `.claude/`, committed to a
  repo that tracks 281 files in total. The installer copies everything,
  unselected; the curated development base is the selective route.
- ECC's upstream Python rules also contradict the Conventions above outright:
  `black` + `isort` against the ruff-only rule, `os.environ`/`dotenv` at call
  sites against the `config.py`-only rule, and `pytest --cov=src` against a
  package that is `digest/`. The development base's copy already uses ruff but
  still carries the other two, so this repo's Conventions win if it adopts
  them.
- `.claude/settings.json` is the one piece of that surface worth keeping: a
  narrow permission allowlist for the real toolchain (uv, ruff, pytest, the
  worker's npm scripts, read-only git/gh), plus denies for pushes to main,
  package publishes and the secret files `.gitignore` already covers. It is
  project policy; per-clone state belongs in `.claude/settings.local.json`,
  which stays untracked.
- ECC's installer also sets `includeCoAuthoredBy: false`. That is deliberately
  NOT adopted here — it is an attribution choice for the owner to make, not a
  side effect of a tooling install. The development base keeps attribution on
  too.

## Verification

Before declaring any task done:

```
uv run ruff check . && uv run pytest
```

If `Dockerfile` or `compose.yml` changed, also confirm `docker build .`
succeeds.

If anything under `workers/news-site/` changed, that subtree has its OWN
toolchain and its own bar — Node, not Python:

```
cd workers/news-site && npm ci && npm test
```

`npm test` = invariant tests + a byte-comparison against committed golden
pages + a prettier check. The contract there is stricter than "tests pass":
a change that should not alter rendered output must produce a ZERO golden
diff, and a change that should alter it regenerates the goldens
(`npm run golden`) so the diff itself is the review artifact. See
`workers/news-site/test/README.md`.

If anything under `docs/architecture/` changed, validate the model and the docs
(needs Docker for the pinned Structurizr image):

```
make check && make docs
```

`make check` is validate + inspect and must report zero ERROR lines. `make docs`
runs the counted half of /docs-sync. Neither proves a diagram communicates
anything: a view whose layout changed needs `make view` and an actual look, and
the register in `docs/architecture/README.md` records which views have been
verified at reading size and which have not. Do not upgrade a register row to
verified without looking at the rendered view.

## Deploy note

Production currently runs on `01-myapps-vm`, deployed from
`~/Developer/toom-platform-homelab` (Ansible `myapps`, systemd timers and Key Vault
wiring). Changes here do not move or stop the VM runtime by themselves.

The approved Azure target is app-owned in `infra/azure/`, with the migration
and cloud recovery procedure in `docs/azure-migration.md`. Provision the
foundation with `jobs_enabled=false`, copy/verify digest secrets, then enable
its nine jobs in Manual mode with schedules disabled.
`.github/workflows/azure-validate.yml` validates
IaC without credentials; `azure-application.yml` runs lint/tests/container build;
`azure-deploy.yml` produces an OIDC-authenticated saved plan and explicitly
approved apply. Azure Blob Storage holds Terraform state in a separate
bootstrapped backend resource group; never create local state. This stack owns a
dedicated digest Key Vault; secret values are copied by the reviewed migration
tool, never managed through Terraform. The existing Azure subscription remains
the recommended default; select its ID explicitly.

Releases are git tags (`vX.Y.Z` is the only version source; `pyproject.toml`
stays 0.1.0), built by `.github/workflows/release.yml` and published as
`ghcr.io/dezoxy/notification-digest:<version>`. A merge to `main` that touches
shipped files (`Dockerfile`, `digest/`, `prompts/`, `pyproject.toml`, `uv.lock`)
is gated by lint and tests, tagged as the next patch version by
`.github/workflows/auto-release.yml` and built; docs-, workflow- and infra-only
merges release nothing, and minor/major tags are still pushed by hand. Azure pins
the image in the tracked `infra/azure/image.auto.tfvars.json` (not in
`AZURE_TERRAFORM_VARS_JSON`). Renovate bumps it after each release, and a merged
bump DOES deploy the image: `azure-deploy` runs on the push and its
`release-apply` job applies the plan only if `scripts/azure_release_guard.py`
finds nothing but the image of all nine jobs changed (see
`docs/azure-migration.md#image-upgrades`); anything else is refused and goes
through the manual plan/apply. So once jobs exist, merged shipped code reaches
Azure without a manual step. Until cutover, the homelab repository still pins and
deploys its own image. Schedule activation and every non-image infrastructure
change stay manual: a tag or merge alone never activates Azure schedules.
Preserve `CLAUDE_CODE_OAUTH_TOKEN` subscription auth. The owner requested current
CLI 2.1.294 (Node >=22); keep it pinned and verify subscription/editorial behavior
in the Azure pilot. The VM baseline was 2.1.284 and has not been redeployed;
this migration does not introduce Anthropic API billing. Recovery stays within
Azure; returning execution to the VM is outside the approved migration.
Cloud X-cookie rotation uses `python -m digest.cloud_cookies PRIVATE_JSON_FILE`
with explicit `--operator-login` for workstation Azure CLI access; canonical
cookies live in runtime Blob state, not a duplicate dedicated-vault seed. Daily
backup copies use a separate account/writer identity attached only to the backup
job; pilot recovery must prove a restore from that account.

Operator cloud commands use temporary private working directories and clean them
up after closing SQLite connections, stopping watchdogs and releasing leases.
Explicit export destinations remain durable private outputs for operator
verification/cleanup.
