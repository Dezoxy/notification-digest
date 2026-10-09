## Integration Architecture

Every contract in this document is with a system the owner does not control.
Several are explicitly **unofficial** — reverse-engineered or ToS-violating
access the owner has accepted the risk of
([C-03](../requirements/constraints.md)) — and can break without notice. None of
them is documented as an OpenAPI spec; there is no `api/` directory in this
repository. The two ends this system does own — `digest/publish.py` and
`workers/news-site/src/ingest.js` — are the closest thing to a contract this
repository can itself change.

### Collection sources (inbound — this system only ever reads from them)

| Source | Contract | Stability | Authentication | On breakage |
|---|---|---|---|---|
| Telegram (`digest/collectors/telegram.py`) | Official MTProto client protocol (Telethon), reading the owner's own allowlisted chats (`TG_CHAT_ALLOWLIST`) | High as a protocol; the exposure is account-level, not protocol-level ([RISK-002](../risks/architecture-risks.md)) | Long-lived `StringSession` (`TG_SESSION`, `TG_API_ID`/`TG_API_HASH`) — the owner's own personal account, not a bot | `AuthKeyUnregisteredError` on a revoked session is detected explicitly; the collector is flagged failed with a banner, the run continues; recovery is a manual re-login, a runbook step that cannot be unattended |
| X / Twitter (`digest/collectors/x.py`, via `twifork`) | Unofficial reverse-engineered REST transport (`client.v11.notifications_all`), notifications timeline only, no home timeline | Low — upstream `twikit` broke completely once already (March 2026, an X webpack bundle change) | Browser-exported cookie jar (`auth_token` + `ct0`); the rotated jar persists after every clean collect | `InvalidSession` is detected explicitly; collector isolated, banner; recovery is the documented cookie-refresh runbook (README, "Runbook: refreshing the X session cookies") |
| Reddit (`digest/collectors/reddit.py`) | Unofficial `.json` endpoints on `old.reddit.com`, cookie session | Low — Reddit's Data Team formally refused the owner's official API application (2026-08-06); the anonymous fallback returns a hard 403 regardless of pacing | `reddit_session` browser cookie (`REDDIT_SESSION_COOKIE`) | A 401/403 response is detected explicitly as an auth failure; collector isolated, banner; recovery is manual cookie refresh |
| Patreon (`digest/collectors/patreon.py`) | The site's own frontend JSON API (`patreon.com/api/posts`), one paid-tier campaign's posts; used only by the separate `patreon` run mode, structurally excluded from the window sweep | Low — Patreon's terms explicitly prohibit automated access; a dead session degrades to HTTP 200 with every post's body empty rather than an error | `session_id` cookie (`PATREON_SESSION_COOKIE`), the owner's own paid subscription | `current_user_can_view` false on every returned post is the explicit signal the collector keys on, never inferred from status code; recovery is manual cookie refresh — a lost session here risks a paid subscription, not a free account ([RISK-007](../risks/architecture-risks.md)) |
| Polymarket (`digest/collectors/polymarket.py`) | Market-odds data behind the owner's own proxy Worker; swing-detection over the top-N markets by volume | Medium — a semi-official market data source reached through a proxy this system also depends on | `POLYMARKET_PROXY_KEY` against `POLYMARKET_API_BASE` | Collector isolated, banner; a market that stops reporting simply ages out of `polymarket_probs` after 30 days rather than erroring |
| Hacker News (`digest/collectors/hackernews.py`) | Public Algolia HN Search API (`hn.algolia.com/api/v1/search`, `tags=front_page`) — chosen over the official Firebase API specifically to avoid one request per story | High — public API, no auth, no documented rate limit for reasonable personal use | None | Collector isolated, banner; the lowest-risk source in the system by construction |
| RSS/Atom feeds (`digest/collectors/rss.py`) | Standard RSS/Atom via `feedparser`, one feed per `NEWS_FEEDS` entry, no cursor axis (time-window based, not id-based) | Medium — publisher-controlled, and any single feed can disappear or change shape without notice | None | Per-feed fault isolation; one broken feed does not affect any other feed or the run |

### Model providers (outbound — summarization)

| Provider | Contract | Stability | Authentication | On breakage |
|---|---|---|---|---|
| Claude CLI, headless (`claude -p`) | Subprocess invocation; the primary summarizer for every lane (window, daily, weekly, patreon, positions, translate, arc-context, verify) | High under normal operation; the login session itself can expire or be revoked with no automated recovery | `CLAUDE_CODE_OAUTH_TOKEN`, a long-lived token from `claude setup-token` on the owner's Max subscription (flat-fee, not metered per token) | Recoverable failure classes (safeguards refusal, usage limit, timeout, empty output) fall back to the Claude API automatically ([ADR 2](../decisions/0002-summarizer-fallback-chain.md), provider changed by [ADR 9](../decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md)); an expired or revoked login fails the run outright — `summarize.py` raises, exit code non-zero |
| Claude API (`digest/anthropic_api.py`) | Messages API (`/v1/messages`) with a token from `/v1/oauth/token`; tried only after the primary Claude call fails, in the order given by `FALLBACK_MODELS` (editorial tier) or `FALLBACK_LIGHT_MODELS` (translation/context tier); `max_tokens` 32000, effort fixed at `high` | Depends on Anthropic's API uptime, the Entra token endpoint and a correct federation setup; a reply ending in `max_tokens` or `refusal` counts as a failed leg; **not** used by `verify.py`, which needs live web tools no fallback model can provide | No API key: the job's managed identity, federated through a dedicated Entra audience (`ANTHROPIC_FEDERATION_*` identifiers, not secrets). Misconfiguration surfaces only at fallback time | If every model in the chain fails within `FALLBACK_TIMEOUT_SECONDS`, the window fails for that run — the same exit-code alerting path as a Claude CLI failure ([RISK-005](../risks/architecture-risks.md)) |

### Delivery and publishing contracts (outbound — this system's own two ends)

| Contract | Direction | Stability | Authentication | On breakage |
|---|---|---|---|---|
| Telegram Bot API (`sendMessage`, `digest/publish.py`) — separate from the Telethon collector above | Digest Runner → Telegram | High — official Bot API | `TELEGRAM_NOTIFY_BOT_TOKEN` + `TELEGRAM_NOTIFY_CHAT_ID`, routed to per-kind forum topics via `TELEGRAM_*_THREAD_ID` | Raises on failure; retried on the next run via the digest's own `telegram_sent` pending flag; a per-run 429 breaker exists after the [2026-08-06 Telegram flood incident](../../incidents/2026-08-06-telegram-flood.md) |
| News-site ingest (`PUT /ingest/:id`, `workers/news-site/src/ingest.js`) | Digest Runner → News Site (Cloudflare Worker) | Owner-controlled on both ends — this repository owns the publisher and the Worker, so the contract only breaks on a deliberate mismatch between the two ([ADR 4](../decisions/0004-multi-channel-delivery.md)) | `x-ingest-key` header, compared against `SITE_INGEST_KEY`/`INGEST_KEY`; the Worker fails closed (401) if the secret was never set | Raises on failure in `publish.py`; retried on the next run via the digest's own `site_published` pending flag. The Worker itself rejects an oversized or malformed payload outright (400) rather than partially applying it |

### What is deliberately not here

API contracts this repository does not own are not restated as OpenAPI or an
equivalent spec — there is no `api/` directory in this repository, and a
previous version of this document's link to one did not correspond to anything
on disk. The exact request/response shapes summarized above live in the source
modules cited in each row (`digest/collectors/*.py`, `digest/publish.py`,
`workers/news-site/src/ingest.js`), not here.
