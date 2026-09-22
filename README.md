# digest

A personal notification-digest service. It collects new messages from the
owner's own Telegram groups and new notifications from the owner's own
X/Twitter account, summarizes what's new with Claude, and delivers a single
digest every six hours. Delivery is multi-channel: a Telegram TL;DR ping and
a published news-site entry are the live channels, and email — the original
and still-implemented channel — is disabled on the owner's deployment.
Telegram collection uses the official MTProto API (Telethon). X collection
uses `twifork` (a maintained fork of the dead `twikit`, still imported as
`twikit`), an unofficial scraper driven by a cookie session — this carries
ToS and account-ban risk, which the owner has explicitly accepted.

## Architecture

```
Telegram (Telethon) ─┐                                              ┌─> Telegram TL;DR ping
X/Twitter (twifork)  │                                              │
RSS / news feeds     ├─> collectors ─> SQLite state ─> Claude ──────┼─> news site (workers/news-site/)
Reddit               │                  (items, cursors, summarize  │
Polymarket           │                    digests, deltas, + trans- ├─> SMTP email (implemented,
Hacker News         ─┘                    arc keys/context)  late   │   disabled on the deployment)
                                                                    └─> markdown archive

Patreon (own collector) ─┐
Telegram/X/keyword       ├─> own run ─> Claude ─> its own Telegram topic
  position matches ──────┘             (never the site — see below)
```

Each run mode has its own systemd timer on the VM (scheduling lives in the
homelab repo, not here):

| Mode | Command | Cadence | Input | Telegram topic |
|---|---|---|---|---|
| window | `python -m digest` | every 6h (00/06/12/18 UTC) | raw items | TL;DR |
| daily | `python -m digest daily` | 20:30 Budapest | that day's window digests | daily |
| weekly | `python -m digest weekly` | Sun 21:45 Budapest | the week's daily briefs | weekly |
| patreon | `python -m digest patreon` | hourly (:50) | one paid post each | patreon |
| positions | `python -m digest positions` | every 4h (:25) | raw items matching the tracked channels, accounts or keywords | positions |
| relay | `python -m digest relay` | hourly | new posts in `RELAY_TG_CHANNELS`, forwarded verbatim (no summarization) | relay |
| backfill/ops | `scripts/*.py` | manual | — | — |

The first three form a cascade: each rung consumes the one below, so the
daily never sees raw items and the weekly never sees anything but daily
briefs.

`patreon` and `positions` are NOT part of that cascade. Each owns its items
end to end — collected, summarized and delivered into its own topic, and
structurally excluded from the window sweep so nothing is ever covered
twice. `positions` additionally stays SILENT when the window held only
chatter (see `prompts/positions.md`), which is what lets it run every 4
hours without becoming noise.

`relay` is not a lane at all: it forwards every new post from the configured
public channels into its topic with Telegram's native forward (media,
albums and the "Forwarded from" header intact), no model in the loop, no
`items`/`digests` rows — only a cursor per channel. First run seeds the
cursor and forwards nothing; there is never a history backfill.

Because the forward is sent by your OWN user session, Telegram gives *you* no
notification for it (it does notify everyone else in the group). So after a
batch lands, the notify bot posts one short `🔔 <channel> · N new posts` line
into the same topic — a different sender, therefore a real notification. It is
soft-failing: the posts and their cursor are already committed, so a failed
ping costs one notification, never the run.

### Run-mode arguments

```
python -m digest                       # window run, all channels
python -m digest hide:telegram         # window run, no Telegram ping
python -m digest hide:telegram,site    # window run, stored + archived only
python -m digest daily --force         # bypass the daily duplicate-fire guard
```

`hide:<channel>` (`email`, `site`, `telegram`, comma-separated) produces and
stores the digest exactly as normal — the row, its stamped items, the archive
copy and every downstream consumer are untouched — and only suppresses the
reader-facing channel. `hide:site` without `hide:telegram` is rejected at
startup: the Telegram TL;DR links to the digest's own site page, so hiding
only the site would ship a real ping pointing at a page that was never
published. It is accepted on the window path only.

Which runs use it is a homelab scheduling decision, not this repo's: the VM
splits the 6-hourly window into three timers — 06/12 UTC plain, 00 UTC
`hide:telegram` (no 2am Budapest ping, but the site still carries overnight
news for morning readers), and 18 UTC `hide:telegram,site` (the 20:30 daily
brief republishes that same window 30 minutes later).

`--force` is the owner's manual escape hatch past `run_daily`'s duplicate-fire
guard, for a deliberate second daily run on the same day. It is read only
alongside `daily`.

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

Read exclusively in `config.py` — no other module touches `os.environ`.
Defaults in parentheses; **(secret)** means it comes from Azure Key Vault at
deploy time and must never be committed or logged.

**Core**

| Variable | Description |
|---|---|
| `TG_API_ID` / `TG_API_HASH` | Telegram API credentials from my.telegram.org. **(secret)** |
| `TG_SESSION` | Telethon user session string. **(secret)** |
| `TG_CHAT_ALLOWLIST` | Comma-separated chat/group IDs to collect from. |
| `STATE_DB_PATH` | SQLite state database (`./state.db`). |
| `ARCHIVE_DIR` | Where markdown digest copies are archived (`./archive`). |

**Collectors** — each is off unless enabled; `NEWS_FEEDS`, `PATREON_*`,
`POSITIONS_TG_CHANNELS`, `POSITIONS_X_ACCOUNTS`, `POSITIONS_KEYWORDS` and
`RELAY_TG_CHANNELS` use an empty-means-disabled shape instead of a flag.

| Variable | Description |
|---|---|
| `X_ENABLED` | Master switch for the X collector (`false`). |
| `X_COOKIES_PATH` / `X_COOKIES` | X session cookies — supply exactly one. **(secret)** With `X_COOKIES_PATH`, the rotated jar is persisted alongside it as `<path>.live` after every successful collect, and whichever of the two is newer is used — so refreshing the seed file automatically takes precedence again. |
| `NEWS_FEEDS` | Comma-separated RSS/Atom URLs. Enables the news collector iff non-empty. |
| `REDDIT_ENABLED` | Reddit collector switch (`false`). |
| `REDDIT_SESSION_COOKIE` | Required when Reddit is enabled. **(secret)** |
| `REDDIT_SUBREDDITS` | Comma-separated names, no `r/` prefix. Required when enabled. |
| `REDDIT_POSTS_PER_SUB` | Posts pulled per subreddit per run. |
| `POLYMARKET_ENABLED` | Polymarket collector switch (`false`). |
| `POLYMARKET_API_BASE` / `POLYMARKET_PROXY_KEY` | Endpoint and its key. **(secret)** |
| `POLYMARKET_TOP_N` / `POLYMARKET_SWING_THRESHOLD` | How many markets, and the probability move that makes one notable. |
| `HACKERNEWS_ENABLED` / `HACKERNEWS_TOP_N` | Hacker News front-page collector (`false`) and how many front-page stories per run (`15`, range 1–30). No auth. |
| `PATREON_CAMPAIGN_ID` / `PATREON_SESSION_COOKIE` | Patreon collector, used only by the `patreon` run mode. Enabled iff **both** are set — setting exactly one is a startup error, not a half-disabled collector. The cookie is the owner's own `session_id` for a paid account. **(secret)** |
| `POSITIONS_TG_CHANNELS` | Telegram usernames (no `@`) claimed by the positions tracker instead of the window digest. |
| `POSITIONS_X_ACCOUNTS` | X screen names (`@` optional) claimed the same way. **Requires post notifications (the bell) enabled for each account in the X app** — the collector only fetches an account's posts when a notification names it. |
| `POSITIONS_KEYWORDS` | Free-text terms (min 4 chars) that claim an item from **any** source, so the project's news is pulled out of general channels and feeds too. Keep them distinctive — cashtags and proper names, never a bare `ASI`/`FET`; an over-broad term can make unrelated stories vanish from every channel. |
| `RELAY_TG_CHANNELS` | Telegram usernames (no `@`) whose new posts the `relay` run mode forwards verbatim into the hub topic via the user session. Requires a numeric `TELEGRAM_NOTIFY_CHAT_ID`. The bot token is optional but recommended: without it the forwards arrive **silently for you**, since Telegram never notifies an account about its own messages. |

**Summarization** — every model call goes to `claude -p` first; if that fails
for any reason (safeguards refusal, usage limit, timeout, empty output) and
`OPENROUTER_API_KEY` is set, the same prompt is retried against each
`FALLBACK_MODELS` entry in order. `verify.py` is excluded: it is the only call
that needs live web tools, which no fallback can provide.


| Variable | Description |
|---|---|
| `ANTHROPIC_MODEL` | Model for headless summarization (`claude-opus-5-5`). |
| `CLAUDE_TIMEOUT_SECONDS` | Per summarize call (`300`; the deployment sets 600). |
| `CLAUDE_EFFORT` | `low`/`medium`/`high`/`xhigh`/`max` (`high`). |
| `TRANSLATE_HU_ENABLED` | Hungarian translation pass (`false`). |
| `TRANSLATE_MODEL` / `TRANSLATE_MODEL_FALLBACK` | Primary (`claude-sonnet-5`, pinned rather than the `sonnet` alias so the site's "Translated with" line names the model that actually ran) and the model retried on a safeguards refusal (`claude-sonnet-4-6`; empty disables the retry). |
| `TRANSLATE_EFFORT` | `low`/`medium`/`high`/`xhigh`/`max` (`high`). Independent of `CLAUDE_EFFORT`, which drives the summarizer. |
| `TRANSLATE_TIMEOUT_SECONDS` | Per translate leg (`300`) — **applies to each leg, so the worst case is 2×**. |
| `VERIFY_DAILY_ENABLED` | Web-verification pass over the daily brief (`false`). |
| `VERIFY_DAILY_TIMEOUT_SECONDS` / `VERIFY_DAILY_MAX_WEB_OPS` | Its budget (`600`) and its self-policed tool-call guidance (`20`). |
| `VERIFY_DAILY_MODEL` / `VERIFY_DAILY_EFFORT` | Default to `ANTHROPIC_MODEL` / `CLAUDE_EFFORT`. |
| `OPENROUTER_API_KEY` | Enables the OpenRouter fallback chain. Unset = Claude only, exactly as before. **(secret)** |
| `FALLBACK_MODELS` | Editorial-tier chain, tried in order when the Claude call fails (`openai/gpt-5.6-sol,z-ai/glm-5.3`). Explicitly empty disables this tier. |
| `FALLBACK_LIGHT_MODELS` | Same for translation and context primers (`openai/gpt-5.6-terra,deepseek/deepseek-v4-flash`). |
| `FALLBACK_TIMEOUT_SECONDS` | Wall-clock budget **shared by all legs of one call** (`180`), started when the Claude call fails. |
| `CONTEXT_ENABLED` | Story-arc context primers, generated after the daily brief ships (`false`). |
| `CONTEXT_MAX_PER_RUN` / `CONTEXT_MODEL` / `CONTEXT_TIMEOUT_SECONDS` | Primer bounds: calls per daily run (`3`), model (`sonnet`), timeout (`120`). |

**Delivery** — the app refuses to start with every channel disabled.

| Variable | Description |
|---|---|
| `EMAIL_ENABLED` | Email channel (`true` — the role default; **disabled on the owner's deployment**). |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` | iCloud SMTP (`smtp.mail.me.com`, `587` STARTTLS). |
| `SMTP_PASSWORD` | iCloud app-specific password. **(secret)** |
| `DIGEST_FROM` / `DIGEST_TO` / `DIGEST_FROM_NAME` | Envelope fields (`Digest`). |
| `SITE_PUBLISH_URL` / `SITE_INGEST_KEY` | News-site ingest endpoint and its key. **(secret)** |
| `SITE_PUBLIC_BASE` | Public base URL used to build reader-facing links. |
| `ARC_KEYS_SITE_ENABLED` | Send story-arc keys with the site payload (`true`). |
| `TELEGRAM_NOTIFY_BOT_TOKEN` | Bot token for the TL;DR ping. **(secret)** |
| `TELEGRAM_NOTIFY_CHAT_ID` | Target chat/group. |
| `TELEGRAM_NOTIFY_THREAD_ID` | Forum topic for window digests. |
| `TELEGRAM_DAILY_THREAD_ID` / `TELEGRAM_WEEKLY_THREAD_ID` | Separate topics for the daily and weekly briefs; unset means they land in the window topic. |
| `TELEGRAM_PATREON_THREAD_ID` / `TELEGRAM_POSITIONS_THREAD_ID` / `TELEGRAM_RELAY_THREAD_ID` | Same, for the Patreon posts, the positions tracker and the relay. `0` is a real value (post to the group root), so unset is the only "not configured" state. |

**Operations**

| Variable | Description |
|---|---|
| `STALE_BACKLOG_WARN_HOURS` | Age at which an unsummarized item makes a run log a `stale_backlog` WARNING naming its source (`24`). Report-only — it never fails a run. Detects a source that has stopped draining, which is otherwise invisible because the run still succeeds. |

## Container

```
docker compose build          # build the image locally
docker compose run --rm digest   # one-shot run (no daemon, no ports)
```

The summarizer authenticates with `CLAUDE_CODE_OAUTH_TOKEN` — a long-lived
token from `claude setup-token`, on the owner's Max subscription, no API key.
Set it in the environment; without it the first run that collects any items
fails at summarization with "Not logged in".

This replaced a one-time interactive `claude` login persisted in
`CLAUDE_CONFIG_DIR`. That login could not be kept alive: the session carries
a hard ceiling roughly 30 days after it is created which refreshing does not
extend, and at the ceiling the CLI wipes its own credentials file. Nothing is
persisted for auth any more, so a fresh volume needs no setup step.

Local state (SQLite db, markdown archive) lands in
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

## The news site (`workers/news-site/`)

One of the delivery channels lives in this repo as source: the Cloudflare
Worker that receives each finished digest over `PUT /ingest/:id` and serves
the private archive readers actually browse. It moved here from the
`Dezoxy/toom-edge` infra repo (with its full history) because it is a
component of this service, not of that estate — what stays there is the
Terraform that binds the two hostnames (`news_site.tf`).

It is a separate program with a separate toolchain and a separate deploy:

| | This service | The Worker |
|---|---|---|
| Language | Python 3.12 (uv) | JavaScript (no build step) |
| Tests | `uv run pytest` | `cd workers/news-site && npm ci && npm test` |
| Ships via | git tag -> GHCR image -> homelab | `wrangler deploy` from `workers/news-site` |

The two are coupled only by the ingest contract (`INGEST_KEY`, the payload
shape validated in `workers/news-site/src/ingest.js`) and by the `#sN`
section-anchor numbering both sides derive independently — see that repo
directory's `README.md`, which is the authoritative document for the site.

## Deployment

Not deployed from this repo. A git tag triggers `.github/workflows/release.yml`,
which builds and publishes the image to GHCR; Renovate in the separate
`~/developer/homelab` repo bumps the pinned tag, and deploy (systemd timer,
Ansible role `myapps`, secrets wiring via Azure Key Vault) happens from there.

The Worker deploys on its own track and is not part of that chain: a tag
here ships the Python service only. Deploying the site is `wrangler deploy`
from `workers/news-site` (see its README) — cutting a release tag does not
touch it, and vice versa.

## Runbook: refreshing the X session cookies

The X collector authenticates with a browser session cookie jar, never a
login (`build_client` deliberately never performs anything login-shaped —
see its docstring). Sessions die eventually, and when one does the collector
fails with `x auth/cookie error: InvalidSession` and X contributes zero items
until the jar is replaced. Since 0.26.3 the rotated jar is persisted after
every clean collect, so this should be rare — but it is still the recovery
procedure when it happens.

**Symptom:** `x auth/cookie error: InvalidSession` in the journal, `"x": "failed"`
in `run_summary`, and `failed_sources: ["x"]` on the digest.

### 1. Use a dedicated browser session

Logging out of the browser session these cookies came from invalidates them.
Export from a **separate Chrome profile** (or an Incognito window), then close
the window *without* logging out — that session then lives independently of
everyday browsing. Avoid X → Settings → Security → "Log out of all other
sessions" afterwards, which revokes it.

### 2. Read the cookies from DevTools

`auth_token` is **HttpOnly**, so `document.cookie` in the Console returns only
`ct0` — a jar built that way looks plausible and fails with `InvalidSession`.
Use the Application panel, which reads the cookie store below that boundary:

1. On `https://x.com`, logged in → `F12` → **Application**
2. **Storage → Cookies → `https://x.com`**
3. Copy the **Value** of `auth_token` and `ct0`, in one sitting — `ct0` is the
   CSRF token paired to that session.

### 3. Store the jar

Exactly two keys, which is what `load_cookies` expects:

```json
{"auth_token":"...","ct0":"..."}
```

Capture the two values without either the shell or your history seeing
them — the prompts are hidden, and the readout at the end is how you
confirm the paste landed. (Pasting raw JSON at a zsh prompt is a trap:
`{...}` is brace expansion, so it is mangled into a "command not found"
that embeds the credential in your history.)

```bash
python3 - <<'PY'
import getpass, json, os, sys

DEST = "/tmp/x-cookies.json"
at  = getpass.getpass("paste auth_token, then Enter: ").strip()
ct0 = getpass.getpass("paste ct0,        then Enter: ").strip()

errs = []
if not at:  errs.append("auth_token is empty")
if not ct0: errs.append("ct0 is empty")
if at  and any(c.isspace() for c in at):  errs.append("auth_token contains whitespace")
if ct0 and any(c.isspace() for c in ct0): errs.append("ct0 contains whitespace")
if at and at == ct0: errs.append("same value pasted twice")
if at.startswith('{') or ct0.startswith('{'):
    errs.append("paste the bare VALUES, one per prompt - not the whole JSON")
if errs:
    print("\nNOT written:", file=sys.stderr)
    for e in errs: print("  -", e, file=sys.stderr)
    sys.exit(1)

fd = os.open(DEST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"auth_token": at, "ct0": ct0}, f)
print(f"\nwrote {DEST} (0600)")
print(f"  auth_token: {len(at)} chars, ends ...{at[-4:]}")
print(f"  ct0:        {len(ct0)} chars, ends ...{ct0[-4:]}")
PY
```

**`--tags` is mandatory, not decoration.** `az keyvault secret set` creates a
new secret VERSION, and versions do not inherit tags. The deploy discovers
secrets with `[?tags.folder=='digest'].{id:name, envvar:tags.envvar}`
(`.github/actions/azure-secrets` in the homelab repo), so a rotation that
drops those tags makes the secret invisible to the fetch: `DIGEST_X_COOKIES`
is never exported, the Ansible task's `when` guard fails, and
`Write notification-digest X cookies file` reports **`skipping`** while the
deploy still goes green. That is exactly what happened on 2026-09-06 —
`file-encoding` survived only because `--file` re-adds it automatically.

```bash
az keyvault secret set \
  --vault-name kv-homelab-prod-th \
  --name digest-x-cookies \
  --file /tmp/x-cookies.json \
  --tags envvar=DIGEST_X_COOKIES folder=digest file-encoding=utf-8 \
  --output none && echo uploaded

rm -f /tmp/x-cookies.json
```

`--output none` because the default prints the secret value back at you.

Verify BOTH the shape and the tags — the tags are what the deploy needs, and
a jar with the right keys but no tags will silently not deploy:

```bash
az keyvault secret show --vault-name kv-homelab-prod-th --name digest-x-cookies \
  --query "tags" -o json

az keyvault secret show --vault-name kv-homelab-prod-th --name digest-x-cookies \
  --query value -o tsv | python3 -c "import json,sys; print('keys:', sorted(json.load(sys.stdin)))"
```

Want `envvar`/`folder`/`file-encoding` present, and
`keys: ['auth_token', 'ct0']`. If the tags were already lost, restore them
without touching the value:

```bash
az keyvault secret set-attributes --vault-name kv-homelab-prod-th \
  --name digest-x-cookies \
  --tags envvar=DIGEST_X_COOKIES folder=digest file-encoding=utf-8
```

### 4. Deploy

Deploy `01-myapps-vm` (Configuration only) from the homelab repo. Ansible
rewrites the seed file, whose mtime then jumps ahead of the stale
`x-cookies.json.live`, so `resolve_cookie_path` picks the fresh seed
automatically — no need to delete the old live jar by hand. From the first
clean collect onward the app persists rotations again.

Check the deploy summary artifact before trusting it: the task
`Write notification-digest X cookies file` must report **`changed`**. If it
says `skipping`, the tags are missing (see above) — the deploy will still be
green, and the old jar will still be in place.

Then confirm with the next digest's `source_counts`, which should carry an
`x` key.

### The other session credentials

`REDDIT_SESSION_COOKIE`, `PATREON_SESSION_COOKIE` and `TG_SESSION` are also
long-lived session credentials refreshed the same way (update the Key Vault
secret, deploy), but they have none of the mechanics above: they are env vars
rather than files, sent as hand-built headers over stateless `urllib` (or, for
Telegram, an MTProto auth key), so nothing rotates and there is no live jar.
Each already reports its own auth death explicitly — see `reddit.py`'s 401/403
branch, `patreon.py`'s `current_user_can_view` check (Patreon degrades to
HTTP 200 with empty bodies rather than erroring), and `telegram.py`'s
prefetch abort.

## Status

In production on the owner's VM. Five run modes — window (three timers,
two of them `hide:`-suppressed), daily, weekly, patreon and positions — are on
their own systemd timers; `relay` (added 2026-09) runs there only once the
homelab repo adds its hourly timer. The live delivery channels are the Telegram TL;DR
ping and the news site, with email implemented but disabled there. Hungarian
translation, the daily verification pass and story-arc context primers are all
enabled on that deployment, though each defaults off here.
See PLAN.md for the phased plan and per-phase progress.
`docs/pr-summaries/` is auto-generated on merge (see `.github/workflows/pr-summary.yml`) — don't hand-edit it.
