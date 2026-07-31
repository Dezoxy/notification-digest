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
| `NEWS_FEEDS` | Comma-separated RSS/Atom feed URLs. Enables the news collector iff non-empty — no separate on/off flag. |

## Container

```
docker compose build          # build the image locally
docker compose run --rm -it --entrypoint claude digest   # ONE-TIME: /login (Max subscription)
docker compose run --rm digest   # one-shot run (no daemon, no ports)
```

The interactive `claude` step is required once per fresh `digest-data`
volume: the summarizer authenticates via the persisted subscription login
in `CLAUDE_CONFIG_DIR` (no API key), and a brand-new volume has no
credentials — without the login, the first run that collects any items
fails at summarization. Complete `/login` in the prompt, exit, and the
credentials persist in the volume for every later run.

Local state (SQLite db, markdown archive, Claude CLI config dir) lands in
the named Docker volume `digest-data`, mounted into the container at
`/data`. A named volume is used instead of a `./local-data:/data` bind mount
because Docker initializes a named volume's contents (and ownership) from
the image on first run: `/data` in the image is already chowned to the
container's uid-1000 (non-root) user, so the volume is writable from the
very first `docker compose run`. A bind mount, by contrast, would have
Docker auto-create the host directory as root on first run, which shadows
that chown and leaves the uid-1000 process unable to write — silently
breaking the container. `compose.yml` here is for local dev only — the VM's
production compose service lives in the separate homelab repo (see
Deployment below). Releases are cut by pushing a git tag (`vX.Y.Z`);
`.github/workflows/release.yml` builds and pushes the image to GHCR.

To inspect the archive without a shell in the running container:

```
docker compose run --rm --entrypoint ls digest -la /data/archive
```

or copy files out via a temporary container:

```
docker run --rm -v digest-data:/data -v "$PWD":/backup busybox \
  cp -r /data/archive /backup/
```

To reset all local state (start clean, e.g. after a schema change):

```
docker volume rm digest-data
```

(`compose.yml` pins the volume's literal name via `name: digest-data`, so
it is *not* project-prefixed — the commands above address the exact volume
the service uses).

## Deployment

Not deployed from this repo. A git tag triggers `.github/workflows/release.yml`,
which builds and publishes the image to GHCR; Renovate in the separate
`~/developer/homelab` repo bumps the pinned tag, and deploy (systemd timer,
Ansible role `myapps`, secrets wiring via Azure Key Vault) happens from there.

## Status

Pre-implementation. See PLAN.md for the phased plan and current progress.
`docs/pr-summaries/` is auto-generated on merge (see `.github/workflows/pr-summary.yml`) — don't hand-edit it.
