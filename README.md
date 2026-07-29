# digest

A personal notification-digest service. It collects new messages from the
owner's own Telegram groups and new notifications from the owner's own
X/Twitter account, summarizes what's new with Claude, and emails a single
HTML digest every few hours. Telegram collection uses the official MTProto
API (Telethon). X collection uses `twikit`, an unofficial scraper driven by a
cookie session — this carries ToS and account-ban risk, which the owner has
explicitly accepted.

## Architecture

```
Telegram (Telethon) ─┐
                      ├─> collectors ─> SQLite state ─> Claude summarize ─> SMTP email + markdown archive
X/Twitter (twikit)  ─┘
```

Runs as a one-shot container (`docker compose run --rm digest`) on a systemd
timer, not a long-running service.

## Quickstart (local dev)

```
git config core.hooksPath .githooks   # enable the no-direct-push-to-main hook
uv sync
cp .env.example .env        # fill in the values below
uv run python scripts/telegram_login.py   # one-time Telethon login, Phase 1
uv run python -m digest
```

## Environment variables

| Variable | Description |
|---|---|
| `TG_API_ID` | Telegram API ID from my.telegram.org. |
| `TG_API_HASH` | Telegram API hash from my.telegram.org. |
| `TG_SESSION` | Telethon user session string (secret). |
| `TG_CHAT_ALLOWLIST` | Comma-separated chat/group IDs to collect from. |
| `X_ENABLED` | `true`/`false` — master switch for the X collector. |
| `X_COOKIES_PATH` | Path to the X/Twitter session cookies file (secret). |
| `ANTHROPIC_MODEL` | Claude model used for headless summarization (e.g. `claude-opus-5`). |
| `SMTP_HOST` | iCloud SMTP server, `smtp.mail.me.com`. |
| `SMTP_PORT` | `587` (STARTTLS). |
| `SMTP_USER` | iCloud account username. |
| `SMTP_PASSWORD` | iCloud app-specific password (secret, generated at account.apple.com). |
| `DIGEST_FROM` | From address for the digest email. |
| `DIGEST_TO` | Recipient address for the digest email. |
| `STATE_DB_PATH` | Path to the SQLite state database. |
| `ARCHIVE_DIR` | Directory where markdown digest copies are archived. |

## Deployment

Not deployed from this repo. A git tag triggers `.github/workflows/release.yml`,
which builds and publishes the image to GHCR; Renovate in the separate
`~/developer/homelab` repo bumps the pinned tag, and deploy (systemd timer,
Ansible role `myapps`, secrets wiring via Azure Key Vault) happens from there.

## Status

Pre-implementation. See PLAN.md for the phased plan and current progress.
`docs/pr-summaries/` is auto-generated on merge (see `.github/workflows/pr-summary.yml`) — don't hand-edit it.
