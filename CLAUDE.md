# digest

Personal notification-digest service. Every 3 hours it collects new items from
the owner's own Telegram groups (Telethon, MTProto user session) and own
X/Twitter notifications (twikit, cookie session, unofficial API — ToS risk
accepted by the owner), tracks state in SQLite, summarizes new items with the
Claude CLI headless (`claude -p`), and emails a structured HTML digest with
deep links. See PLAN.md for the full plan and current phase status.

## Conventions

- Python 3.12, dependency management via uv (`uv sync`, `uv run ...`).
- Lint and format with ruff; run tests with pytest.
- Type hints on all public functions.
- Small modules, no speculative abstractions — this is a single-owner, single-
  deployment service, not a library.
- Config only via environment variables, read exclusively in `config.py`.
  Never call `os.environ` / `os.getenv` from other modules — pass values in.
- All timestamps are stored in UTC. Convert to Europe/Budapest only at
  render/email time, never in storage or state comparisons.

## Hard rules

- Never commit or log secrets, cookies, or session strings (Telegram session,
  X cookies, SMTP password, Claude API key, etc.). Secrets come from Azure
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
- Repo: github.com/Dezoxy/notification-digest (private). Container image:
  `ghcr.io/dezoxy/notification-digest`.
- After opening a PR — and again after every push to it — request a Codex
  review with `gh pr comment <pr> --body "@codex review"` (Codex does not
  re-review on its own). Once the review lands, run
  `uv run python scripts/fetch-pr-review-threads.py <pr>` (ported from secmes;
  defaults to unresolved, actionable, Codex-only threads) and address every
  finding it reports — fix or explicitly rebut, never ignore. Only Codex
  findings gate PRs; other reviewers' threads are informational.

## Verification

Before declaring any task done:

```
uv run ruff check . && uv run pytest
```

If `Dockerfile` or `compose.yml` changed, also confirm `docker build .`
succeeds.

## Deploy note

Deployment (Ansible role `myapps`, the systemd timer, and secrets wiring)
lives in the separate `~/developer/homelab` repo. Changes here never touch
the VM by themselves — a deploy from the homelab repo is a separate step.

Releases are cut by git tag: GitHub Actions in this repo publishes a
versioned image to GHCR (`ghcr.io/dezoxy/notification-digest:<tag>`), the homelab repo
pins that tag (Renovate opens the bump PR), and deploy happens from there.
Changing code here ships nothing until a tag is cut **and** the homelab repo
bumps and deploys — never build the image on the VM.
