# digest — Implementation Plan

## 1. Goal & constraints

- Personal notification-digest service: scrape own Telegram group notifications and own X/Twitter notifications, summarize new items every 3 hours with Claude, email an HTML digest.
- Solo project, homelab-hosted (VM `01-myapps-vm`, Docker Compose), deployed via the existing Ansible `myapps` role in a separate repo — this repo is app source only.
- No official API path exists for X notifications at acceptable cost; twikit (unofficial, cookie-based) is accepted with explicit ToS/ban risk and mitigations (§8).
- Idempotent by design: a crashed run must never lose or duplicate items. Empty window → no email, no noise.
- Partial-failure-tolerant: one collector failing must not suppress the other collector's digest — send what was collected with a failure banner.
- Secrets never committed; injected as env vars at deploy time from Azure Key Vault (`kv-homelab-prod-th`).

## 2. Architecture

```
                        systemd timer (OnCalendar=*/3h, RandomizedDelaySec=600)
                                          │
                                          ▼
                         docker compose run --rm digest
                                          │
                    ┌─────────────────────┴─────────────────────┐
                    │                 main.py                    │
                    │  (orchestrates one run, exit code = health) │
                    └─────────┬───────────────────────┬──────────┘
                               │                       │
                 ┌─────────────▼───────────┐ ┌──────────▼─────────────┐
                 │ collectors/telegram.py  │ │   collectors/x.py       │
                 │ Telethon, StringSession │ │ twikit, cookie session  │
                 │ pulls since last cursor │ │ pulls since last cursor │
                 └─────────────┬───────────┘ └──────────┬─────────────┘
                               │                       │
                               ▼                       ▼
                         ┌─────────────────────────────────┐
                         │   SQLite: /srv/appdata/digest/   │
                         │           state.db               │
                         │   items, digests, cursors tables  │
                         └─────────────────┬─────────────────┘
                                            │ new items since last digest
                                            ▼
                                  ┌───────────────────┐
                                  │  summarize.py      │
                                  │  claude -p (Opus)  │
                                  │  prompts/digest.md │
                                  └─────────┬───────────┘
                                            │ markdown (3 sections)
                                            ▼
                                  ┌───────────────────┐
                                  │  emailer.py         │
                                  │  markdown → HTML    │
                                  │  smtplib send        │
                                  │  archive to disk      │
                                  └─────────┬───────────┘
                                            │
                              ┌─────────────┴─────────────┐
                              ▼                            ▼
                        recipient inbox         /srv/appdata/digest/archive/
```

One run = one process, one exit code. `main.py` runs both collectors (failures isolated per-collector), persists new items and cursors to SQLite, pulls the unsummarized set, calls `claude -p` once with the fixed prompt template, converts the markdown result to HTML, sends it via SMTP, and archives the markdown. The container has no long-running process and no exposed ports — all scheduling, retry visibility, and alerting live in systemd/journal/Loki on the host, not in the app.

## 3. Repo layout

```
x_and_telegram-scrape/
├── .github/
│   └── workflows/
│       └── release.yml         # build + push digest image to GHCR on git tag
├── digest/
│   ├── __init__.py
│   ├── main.py              # entrypoint: orchestrates one run, sets process exit code
│   ├── config.py             # loads/validates env vars into a typed Config object
│   ├── state.py               # SQLite access: schema init, item upsert, cursor read/write, digest bookkeeping
│   ├── collectors/
│   │   ├── __init__.py
│   │   ├── telegram.py        # Telethon collector: fetch new messages per allowlisted chat
│   │   └── x.py                # twikit collector: fetch new notifications, feature-flagged
│   ├── summarize.py           # builds JSON payload from items, invokes `claude -p`, returns markdown
│   └── emailer.py             # markdown→HTML render, smtplib send, archive-to-disk
├── prompts/
│   └── digest.md              # fixed prompt template (3-section output contract)
├── scripts/
│   └── telegram_login.py      # one-time interactive Telethon login → prints StringSession for Key Vault
├── tests/
│   ├── test_state.py           # SQLite idempotency, cursor advance, digest bookkeeping
│   ├── test_collectors.py      # collector output shape, allowlist filtering (mocked clients)
│   └── test_summarize.py       # prompt payload construction, markdown passthrough (mocked claude CLI)
├── Dockerfile                  # slim Python 3.12 image, runs `python -m digest.main`
├── compose.yml                 # local dev: one-shot `digest` service + env file, no host deps
├── pyproject.toml              # uv-managed, Python 3.12, deps: telethon, twikit, markdown, python-dotenv
└── .env.example                 # documents every env var from §4, no real values
```

## 4. Component specs

### 4.1 SQLite schema (`state.py`)

```sql
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL CHECK (source IN ('telegram', 'x')),
    source_id   TEXT NOT NULL,              -- telegram: "{chat_id}:{msg_id}" (msg ids repeat across chats); x: tweet id
    chat_id     TEXT,                       -- telegram chat/thread id; NULL for X
    author      TEXT,
    text        TEXT,
    url         TEXT NOT NULL,              -- t.me/c/<chat_id>/<msg_id> or x.com status URL
    fetched_at  TEXT NOT NULL,              -- ISO8601 UTC
    digest_id   INTEGER REFERENCES digests(id),  -- NULL until included in a sent/attempted digest
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS digests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    item_count  INTEGER NOT NULL,
    email_sent  INTEGER NOT NULL DEFAULT 0, -- 0/1
    body_md     TEXT NOT NULL               -- summarizer output; enables send-retry without re-summarizing
);

CREATE TABLE IF NOT EXISTS cursors (
    source        TEXT NOT NULL CHECK (source IN ('telegram', 'x')),
    scope         TEXT NOT NULL,              -- telegram: chat id; x: 'notifications' or 'posts:{user_id}'
    last_seen_id  TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (source, scope)
);

CREATE INDEX IF NOT EXISTS idx_items_digest_id ON items(digest_id);
```

Idempotency contract:
- Each collector reads its `cursors.last_seen_id` per `(source, scope)` before fetching, and only requests items newer than that — Telegram message IDs are only monotonic within a chat, so each allowlisted chat gets its own cursor row; X uses a single `notifications` scope.
- First run for a chat (no cursor row): seed the cursor from the latest message without emitting items — the first digest starts from "now", never a full-history backfill.
- Inserts use `INSERT OR IGNORE` on the `(source, source_id)` unique constraint — re-fetching an overlapping window is a no-op.
- The cursor only advances after items are durably committed in the same transaction as the insert.
- A digest row is created before the email send attempt (`email_sent=0`) and flipped to `1` only after SMTP confirms; items are stamped with `digest_id` at creation. If the process crashes after commit but before send, the next run detects an existing digest with `email_sent=0` and retries the send instead of re-summarizing (avoids double-billing Opus calls). If it crashes before the digest row commits, next run's "new since last digest" query naturally includes the same items — no loss, no duplicate items (duplicate *summarization* of a stuck row is bounded to one retry pass).

### 4.2 `collectors/telegram.py`

- **Responsibility:** fetch new messages from allowlisted chats since the Telegram cursor, normalize to the `items` shape, return list.
- **Library:** Telethon, authenticated via a persisted `StringSession` (one-time interactive login, done manually to obtain the string, stored thereafter in Key Vault).
- **Input:** `TG_API_ID`, `TG_API_HASH`, `TG_SESSION`, `TG_CHAT_ALLOWLIST`, `cursors.last_seen_id` for `telegram`.
- **Output:** list of normalized item dicts (source, source_id, chat_id, author, text, url, fetched_at).
- **Error handling:** auth failure (`AuthKeyUnregisteredError` / session revoked) → log clearly, return empty list, propagate a distinct "collector failed" flag to `main.py` (do not crash the whole run); rate limiting (`FloodWaitError`) → respect wait if short, otherwise abort collector for this run and flag failure; network errors → same abort-and-flag pattern, no retry loop inside the collector (the next scheduled run is the retry).

### 4.3 `collectors/x.py`

- **Responsibility:** fetch new notification-timeline entries since the X cursor, normalize to the `items` shape, return list. No-ops entirely if `X_ENABLED=false`.
- **Library:** `twifork` (pinned exact version, e.g. `twifork==2.3.5`), not upstream `twikit` — upstream `twikit` 2.3.3 is dead (X changed their webpack bundle in March 2026, breaking twikit's transaction-ID signing before any auth is even attempted). `twifork` is a maintained fork that fixes that and still imports as `twikit`, so no import lines change. Authenticated via persisted cookies (`X_COOKIES_PATH` or inline `X_COOKIES`), never fresh username/password login on a scheduled run. Exact-pinned deliberately (single-maintainer fork handling a live session cookie — bump only after re-auditing the diff).
- **Transport:** talks to the underlying REST call directly — `resp, _ = await client.v11.notifications_all(count, cursor)` — and parses the raw `resp['globalObjects']` JSON itself (`notifications`/`tweets`/`users` dicts, plus a `cursor-bottom` entry for the next page), rather than using twifork's higher-level `get_notifications()`/model layer. Reason: twifork's model layer still breaks separately from the signing bug it fixes — X now returns a plain string for `user.location` while twifork's `User.__init__` calls `.get()` on it, raising `AttributeError` after a successful, authenticated fetch. Bypassing the model layer entirely sidesteps that bug and makes this collector immune to future model-layer breakage too, since it never constructs those objects.
- **Input:** `X_ENABLED`, `X_COOKIES_PATH` or `X_COOKIES`, and the full `(scope -> last_seen_id)` mapping for source `x` (i.e. `state.get_cursors(conn, "x")`), not a single value: this collector owns TWO kinds of cursor scope — `notifications` (the notification timeline's newest `timestamp_ms`) and one `posts:{user_id}` per notification-enabled account (see phase 2).
- **Output:** same normalized item shape as Telegram (`chat_id` NULL).
- **Error handling:** auth/cookie failure → do not retry, do not attempt re-login, log and flag collector failure loudly (this is the signal the account may be locked/challenged — silent retry risks tripping X's automation detection further); response-shape/endpoint changes → catch parse errors, flag failure, do not crash; never runs more than once per scheduled invocation (no internal polling loop) — the 8-runs/day cadence lives in the systemd timer, not in this module.
- **Phase 2 — content-aggregate ("bell") notification post-fetch:** with per-account post notifications enabled in X, an account posting arrives as ONE aggregate notification (`icon.id == 'bell_icon'`) whose `targetObjects` is empty (no linked tweet at all) but whose `fromUsers` names WHO posted. Pre-phase-2 this collector correctly skipped these (nothing linkable) — but that meant X contributed nothing for exactly the notifications the owner enabled the feature for. Two-phase design: **phase 1** (unchanged) parses the notifications page(s), building mention/reply/quote items from linked tweets; **phase 2** scans the same page(s) for content-aggregate notifications (non-engagement icon, no linked tweet, `fromUsers` present), resolves each named user id to a screen_name via the page's own `globalObjects.users`, and calls `client.gql.user_tweets(user_id, count, None)` directly (bypassing twifork's model layer, same rationale as the notifications transport) to fetch that account's own recent posts — `_POSTS_PER_USER` = 40 (X's own page size; halves how often truncation can trigger), for up to `_MAX_POST_FETCH_USERS` = 20 distinct accounts per run, paced with `_PAGE_FETCH_DELAY_SECONDS` between fetches.

  **Per-account post cursors — the load-bearing correctness rule.** Posts are NOT filtered against the notifications cursor. They own `("x", f"posts:{user_id}")` scopes, holding the newest post `timestamp_ms` captured for that account. This decoupling exists because a bell notification is timestamped AFTER the posts it announces: filtering posts against a cursor the notification itself advanced meant any post not captured in the same run (account posted more than one page's worth; account beyond the per-run cap; that account's fetch failed) fell permanently behind the cursor and could never be collected again — silent, unrecoverable loss while the run reported success. With per-account cursors: a **failed** fetch leaves that account's cursor untouched, so the window is retried next run; a **capped-out** account keeps its cursor, so it is picked up next run; **truncation** (a full page whose oldest post is still newer than the account's cursor) advances to newest but logs a WARNING naming the account and the deliberately skipped gap — the same "digest favors newest content, loudly, never silently" decision already documented for the notification page cap. **First sight of an account** (no cursor row) seeds from the newest fetched post and emits nothing, mirroring first-run notifications and first-sight Telegram chats, so enabling notifications on a new account never dumps its history into the next digest.

  Post creation time is decoded from the Snowflake tweet id (`(id >> 22) + 1288834974657`, X's custom epoch) rather than parsed from `created_at` — exact, timezone-free, and directly comparable to the millisecond cursors. Selection is inclusive (`>= cursor`), with boundary re-emits absorbed by the `(source, source_id)` UNIQUE constraint, matching phase 1's tie rule.

  **Retweet/quote containment:** `retweeted_status_result` and `quoted_status_result` subtrees are excluded from both post extraction and author resolution. Their nested originals carry their own `full_text`/`id_str`, so traversing them would emit someone else's post as a separate item — and since author resolution falls back to the notified account's handle (X's payload has no author data inside the post object), that post would be published under the wrong name.

  **Emission validation:** every item's `screen_name` must match `^[A-Za-z0-9_]{1,15}$` and `tweet_id` must be digits-only, in both phases; anything else is skipped with a counted warning. A malformed handle or id would build a URL that no longer exact-matches the emailer's anchor-provenance allowlist, silently degrading the deep link to plain text.

  **Failure containment:** the whole phase-2 block is wrapped like phase 1 — any unexpected exception sets `failed=True` and returns with phase-1 items and cursor intact, never propagating out of `collect()`. A single account failing is counted but does not fail the run; only when *every* attempted fetch fails is it treated as systemic (a signal distinct from phase 1's tweet-link systemic check, since the two cover disjoint failure surfaces).

  **Extra cost:** up to 20 additional GraphQL calls per run on top of the notification pages — bounded, paced, and logged.

### 4.4 `summarize.py`

- **Responsibility:** pull unsummarized items from SQLite, build the JSON payload, invoke `claude -p` with `prompts/digest.md`, return the raw markdown response. Skip entirely (no Claude call) if there are zero new items.
- **Library:** subprocess call to the `claude` CLI (headless mode, `-p`), no SDK dependency.
- **Auth:** the CLI authenticates via the owner's Claude Max subscription (one-time interactive `claude` login performed by the owner on the VM), not an API key. Its config/credentials dir is persisted in a volume (`/srv/appdata/digest/claude-home`), mounted into the container as the CLI's home/config dir — mirrors the existing T3MP3ST pattern on the same VM that persists an agent home at `/srv/appdata/agent`. No `ANTHROPIC_API_KEY` is set.
- **Input:** `ANTHROPIC_MODEL` (default `claude-opus-5`), items JSON, collector failure flags (to inject the "⚠ X collection failed" banner context).
- **Output:** markdown string matching the 3-section contract (§5).
- **Error handling:** non-zero exit / empty stdout from `claude -p` → treat as summarizer failure, do not send a garbage email; log and exit non-zero so systemd/journal record the failure (surfaces via Loki). No automatic retry within the run — next scheduled run picks up the same unsummarized items since `digest_id` was never assigned. A subscription session expiry/revocation fails the same way (non-zero exit → existing Loki alert); recovery is a manual re-login on the VM, not automated (§8).

### 4.5 `emailer.py`

- **Responsibility:** render the summarizer's markdown to HTML, send via SMTP, archive the markdown to disk, mark the digest row `email_sent=1` on success.
- **Library:** `smtplib` (stdlib) + a small markdown→HTML converter (e.g. `markdown` package) — no templating framework needed for 8 emails/day.
- **Provider:** iCloud Custom Email Domain SMTP — `smtp.mail.me.com:587` (STARTTLS), auth = the owner's iCloud account username + an app-specific password (generated at account.apple.com, stored in Key Vault). `DIGEST_FROM` is an alias on the custom domain (e.g. `digest@toomhorvath.com`) that the owner must create in iCloud settings first — a Phase 2 prerequisite. `DIGEST_TO` stays `me@toomhorvath.com`. iCloud signs outgoing mail with `d=toomhorvath.com` DKIM, which passes the domain's existing strict DMARC (`p=reject`, `adkim=s`/`aspf=s`) — no DNS changes needed.
- **Input:** `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO`, `ARCHIVE_DIR`, the markdown body.
- **Output:** sent email + a `.md` file written to `ARCHIVE_DIR` (filename: digest id + timestamp).
- **Error handling:** SMTP failure → do not mark `email_sent=1`, log and exit non-zero (next run retries send, per §4.1 contract); archive write is best-effort but its failure must not block the send (log a warning, continue).

### 4.6 `main.py` / `config.py`

- **`config.py`:** loads all env vars into one validated `Config` dataclass at startup; fails fast (non-zero exit before any collector runs) if a required var is missing or malformed — cheaper to fail loud in journal than to run half-configured.
- **`main.py`:** run order — init/open SQLite → run Telegram collector → run X collector (if enabled) → commit new items + advance cursors → query unsummarized items → if none, exit 0 (no email) → summarize → email → exit 0/non-zero based on outcome. Exit code is the only health signal systemd needs.

### 4.7 Full env var reference

| Var | Used by | Purpose |
|---|---|---|
| `TG_API_ID` | telegram.py | my.telegram.org app id |
| `TG_API_HASH` | telegram.py | my.telegram.org app hash |
| `TG_SESSION` | telegram.py | Telethon StringSession (from one-time interactive login) |
| `TG_CHAT_ALLOWLIST` | telegram.py | comma-separated chat IDs to scrape |
| `X_ENABLED` | main.py, x.py | `true`/`false` feature flag |
| `X_COOKIES_PATH` or `X_COOKIES` | x.py | path to cookie file, or inline cookie JSON |
| `ANTHROPIC_MODEL` | summarize.py | Claude model, default `claude-opus-5` |
| `CLAUDE_CONFIG_DIR` | summarize.py | Points the Claude CLI at the persisted config/credentials dir (`/srv/appdata/digest/claude-home`, volume-mounted) so the Max-subscription login survives across runs; no API key involved |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | emailer.py | iCloud SMTP (`smtp.mail.me.com:587`); user = iCloud account username, password = app-specific password |
| `DIGEST_FROM` | emailer.py | From address |
| `DIGEST_TO` | emailer.py | recipient address (self) |
| `STATE_DB_PATH` | state.py | SQLite file path, default `/srv/appdata/digest/state.db` |
| `ARCHIVE_DIR` | emailer.py | markdown archive dir, default `/srv/appdata/digest/archive/` |

## 5. Summarization prompt design

`prompts/digest.md` is a fixed template (no per-run templating engine — string substitution of the JSON items block is sufficient). Contract:

- **Input to the prompt:** the new items since the last digest, serialized as a JSON array (source, chat/author context, text, url, fetched_at), plus a flag noting whether any collector failed this run.
- **Output from Claude:** markdown only, three fixed sections, in this order:
  1. **Needs attention** — mentions, decisions, deadlines. Terse, one line per item where possible.
  2. **Worth knowing** — grouped by Telegram group / X topic, 1–2 lines each, every item deep-linked (`[text](url)` markdown links using the `url` field verbatim — never reconstructed).
  3. **Noise skipped** — one summary line describing what was filtered and roughly how much.
- If a collector failed this run, the prompt instructs Claude to prepend a `⚠ <source> collection failed this run` banner line before section 1.
- `emailer.py` converts the markdown to HTML for the email body; the same markdown string is archived to disk unmodified. The digest *does* contain third-party text (message content Claude may echo verbatim), so the markdown→HTML conversion must escape raw HTML in the source (e.g. `markdown` with raw-HTML disabled) — the email renders in the owner's own mail client, and a hostile `<img>`/`<a>` smuggled through a group message shouldn't survive to the HTML body.
- Empty item list → `summarize.py` never calls Claude, `main.py` exits 0 with no email — enforced in code, not left to the prompt to "decide."

## 6. Deployment (homelab repo, later phase)

Not part of this repo — tracked here for continuity into `~/developer/homelab`.

**Release pipeline:** this repo is a versioned, released artifact, same as the owner's other self-made apps. `.github/workflows/release.yml` builds the Docker image on git tag push and publishes it to GHCR as `ghcr.io/dezoxy/notification-digest:<tag>`, published from the private repo `Dezoxy/notification-digest`. No image is ever built on the VM. Renovate, already running in the `~/developer/homelab` repo, opens a PR bumping the pinned `digest` tag whenever a new release lands in GHCR; deploy after that is a separate, manual step — either the homelab repo's Makefile/Ansible invocation by hand, or its own GitHub Actions. Changing code in this repo ships nothing until a tag is cut **and** the homelab repo bumps and deploys.

**Ansible `myapps` role additions:**
- New compose service block for `digest` (pulls the pinned `ghcr.io/dezoxy/notification-digest` image tag — never built on the VM, same pattern as `netcheck` pinned to `ghcr.io/dezoxy/netcheck:2.9.0` in the role defaults — env from Key Vault-sourced vars, volumes for `/srv/appdata/digest`).
- New systemd unit + timer files templated into `/etc/systemd/system/` on `01-myapps-vm`.
- Key Vault secret references added to the role's vault-lookup task list.

**Key Vault secret slugs** (`kv-homelab-prod-th`):
- `digest-tg-api-id`, `digest-tg-api-hash`, `digest-tg-session`
- `digest-x-cookies`
- `digest-smtp-user`, `digest-smtp-password`
- No Claude auth secret in Key Vault — the Claude CLI authenticates via a persisted login volume (`/srv/appdata/digest/claude-home`, one-time interactive `claude` login on the VM), not a Key Vault-sourced credential.

**systemd unit + timer (sketch, host-side, not containerized scheduling):**

```ini
# /etc/systemd/system/digest.service
[Unit]
Description=digest - Telegram/X notification summarizer
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/myapps/digest
ExecStart=/usr/bin/docker compose run --rm digest
TimeoutStartSec=600
```

```ini
# /etc/systemd/system/digest.timer
[Unit]
Description=Run digest every 3 hours

[Timer]
OnCalendar=*-*-* 0/3:00:00
RandomizedDelaySec=600
Persistent=true
Unit=digest.service

[Install]
WantedBy=timers.target
```

**Loki alert:** existing systemd-journal-to-Loki pipeline already ships unit logs; add a Grafana alert rule on `{unit="digest.service"} |= "Result: failed"` (or equivalent `systemd[1]` failure line) matched against journal output for `digest.service`, firing on any failed run — no dedicated instrumentation needed in the app itself since exit code is the sole health signal.

## 7. Phases with acceptance criteria

### Phase 1 — Telegram collector + state (local, macOS)
- [ ] Telethon StringSession obtained via one-time interactive login script, works from a local `.env`.
- [ ] `collectors/telegram.py` fetches new messages from allowlisted chats and normalizes them to the item shape.
- [ ] SQLite schema created on first run; re-running with no new messages is a true no-op (no duplicate rows).
- [ ] Cursor advances only after a successful commit; simulated crash mid-run (kill before commit) does not lose or duplicate items on next run.
- [ ] `tests/test_state.py` and `tests/test_collectors.py` (mocked Telethon client) pass.

### Phase 2 — Summarizer + email, end to end (Telegram-only digest delivered)
- [ ] `summarize.py` builds the JSON payload and gets a valid 3-section markdown response from `claude -p` locally.
- [ ] Empty item window produces zero Claude calls and zero emails.
- [ ] `emailer.py` sends a real HTML email to `DIGEST_TO` via configured SMTP and archives the markdown to `ARCHIVE_DIR`.
- [ ] Full `main.py` run against real Telegram data produces one correctly formatted digest email with working deep links.
- [ ] Simulated SMTP failure leaves `email_sent=0`; next run retries the send without re-summarizing.

### Phase 3 — X collector behind `X_ENABLED`
- [ ] `collectors/x.py` authenticates via persisted cookies (no fresh login) and fetches new notifications.
- [ ] `X_ENABLED=false` fully skips the collector with no twikit import side effects.
- [ ] Simulated X auth failure flags the collector as failed without crashing the run; Telegram-only digest still sends with the "⚠ X collection failed" banner.
- [ ] Combined Telegram+X digest groups items correctly by source in the "Worth knowing" section.

### Phase 4 — Ansible deployment + timer + alerting
- [ ] Tagged release (e.g. `v0.1.0`) published to GHCR via `.github/workflows/release.yml` (build + push on git tag).
- [ ] `digest` compose service defined in the `myapps` role, deployed to `01-myapps-vm`, pulling the pinned GHCR image tag (never built on the VM), env vars sourced from Key Vault at deploy time (no secrets in the repo or in plaintext on disk outside the running container's env).
- [ ] systemd timer fires on schedule with jitter; `systemctl status digest.timer` shows correct next-run time.
- [ ] A real scheduled run on the VM produces a digest email and an archived markdown file under `/srv/appdata/digest/archive/`.
- [ ] `/srv/appdata` restic backup includes `digest/state.db` and `digest/archive/` (verify via existing backup job, no new backup config needed).
- [ ] Grafana alert fires within one scheduling cycle of a forced `digest.service` failure.

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| X account suspension (twikit ToS violation) | Low frequency (8 pulls/day), residential home IP, randomized jitter, cookie reuse instead of fresh logins, hard-stop-and-alert on auth errors instead of retry loops. Accepted risk — no technical fix eliminates it. |
| twikit breakage on X GraphQL endpoint changes | Feature-flagged (`X_ENABLED`) so it can be disabled without touching the rest of the pipeline; collector failure is isolated and produces a banner, not a crashed run; pin twikit version, bump deliberately. |
| Telegram session invalidation (revoked StringSession) | Detected via `AuthKeyUnregisteredError`, logged loudly, collector flagged failed (not crashed); re-running the one-time interactive login regenerates the session — documented as a manual runbook step, not automated (re-login can't be unattended). |
| Claude subscription session expiry/revocation | The OAuth session behind the Max-subscription login (§4.4) can expire or be revoked; `claude -p` then fails, `summarize.py` exits non-zero, and the existing Loki alert on `digest.service` failures fires. Recovery is a manual re-login (`claude` interactive auth) on the VM — a runbook step, cannot be unattended. |
| Opus cost creep | Cost is ≈$0 under the Max subscription (no per-token billing); `ANTHROPIC_MODEL` stays env-switchable if a future move to API billing is ever needed; blast radius is further capped by skipping the Claude call entirely on empty item windows (already enforced in §5). |
| Email deliverability (digest lands in spam / provider throttles) | Sending self-to-self via iCloud SMTP with `d=toomhorvath.com` DKIM aligned to the domain's existing SPF (`v=spf1 include:icloud.com ~all`) and strict DMARC (`p=reject`, `adkim=s`/`aspf=s`) — low risk; low volume (8/day) keeps well under any iCloud sending limits. |
| Secrets leakage | No secrets committed to this repo (`.env.example` only, real `.env` gitignored); production secrets live only in Key Vault and are injected as env vars at deploy time on the VM, never written to disk in the container image. |
| Prompt injection via scraped content | Message text from groups/X is untrusted input to the summarizer — a hostile message could try to steer the summary or forge a "needs attention" item. Blast radius is inherently small (output is an email to self; the summarizer has no tools and no ability to act), plus: prompt wraps items in a clearly delimited JSON block and instructs Claude to treat item text strictly as data; deep links are rendered from the stored `url` field, never from URLs inside message text. |

## 9. Decision log

1. **RESOLVED — Claude CLI auth method:** Max subscription login, not an API key. One-time interactive `claude` login on the VM; config/credentials persisted in a volume mounted into the container (§4.4). Marginal Opus cost ≈ $0. Trade-off: the OAuth session can expire/be revoked — summarizer fails, exit non-zero, existing Loki alert fires; recovery is a manual re-login on the VM (§8).
2. **RESOLVED — SMTP provider:** iCloud Custom Email Domain SMTP (`smtp.mail.me.com:587`), not a third-party relay. `toomhorvath.com` mail is already an iCloud Custom Email Domain (MX, SPF, iCloud DKIM, strict DMARC all managed in the owner's cloudflare-terraform repo); sending from an alias on that domain (e.g. `digest@toomhorvath.com`, to be created in iCloud settings) means iCloud's own DKIM already satisfies the domain's strict DMARC — no DNS changes needed. A third-party relay (Resend etc.) was rejected — it would require new DKIM records in the Terraform zone.
3. **OPEN — Telegram group allowlist:** which chat IDs go into `TG_CHAT_ALLOWLIST` — owner will supply before Phase 1 testing.
4. **RESOLVED — X scope:** notifications timeline only, no home timeline. Affects `collectors/x.py` fetch surface and volume/cost assumptions in §8.
