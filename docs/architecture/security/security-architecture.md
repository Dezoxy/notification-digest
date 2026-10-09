## Security Architecture

Scope: the security posture of the notification-digest system — a single-owner
service with one operator, one reader, and no other users
([C-01](../requirements/constraints.md)). There is no security team, no SOC,
and no on-call rotation; the controls below are what a solo homelab operator
maintains by hand, not an enterprise baseline.

### What is exposed

Exactly one container in this system accepts inbound traffic from the public
internet: the **News Site**, a Cloudflare Worker. Everything else — the Digest
Runner, the State Database — lives on `01-myapps-vm`, a Debian VM on the
owner's home LAN that is never reachable from the internet
([A-04](../requirements/assumptions.md)). The Digest Runner never listens on a
port; it is a one-shot process that starts on a systemd timer, makes only
outbound connections (to Telegram, X, Reddit, Patreon, Polymarket, Hacker
News, RSS feeds, the Claude CLI, the Claude API (fallback), the News Site, and
optionally an SMTP relay), and exits. See [trust-boundaries.md](trust-boundaries.md) for the
boundary-by-boundary detail and the model in
[`../model/containers.dsl`](../model/containers.dsl).

The News Site itself has no login and no Cloudflare Access in front of it. Its
reader-facing routes sit under an unguessable capability token in the URL path
(`/t/<SITE_TOKEN>/…`), not behind an identity check — see "Who authenticates
to what" below.

### Who authenticates to what

| Actor / caller | Authenticates to | Mechanism | What it grants |
|---|---|---|---|
| Digest Runner | Telegram | MTProto, `TG_SESSION` (Telethon `StringSession`) — the owner's own **personal account**, not a bot | Read access to the owner's own groups/channels; also used to send relay forwards |
| Digest Runner | Telegram Bot API | `TELEGRAM_NOTIFY_BOT_TOKEN` | Send-only access to post the TL;DR digest and section links into the owner's chat/topics |
| Digest Runner | X | `X_COOKIES_PATH` / `X_COOKIES` (owner's session cookie), via `twifork` (twikit fork) | Read access to the owner's own notifications timeline. Unofficial API; no sanctioned alternative exists at this tier ([C-03](../requirements/constraints.md)) |
| Digest Runner | Reddit | `REDDIT_SESSION_COOKIE` (owner's own logged-in session) | Read access to top-of-day posts in configured subreddits. Reddit formally declined the owner's official API application; the cookie session is the only remaining path |
| Digest Runner | Patreon | `PATREON_SESSION_COOKIE` (owner's own paid-tier session) | Read access to one campaign's paid-tier posts |
| Digest Runner | Claude CLI | `CLAUDE_CODE_OAUTH_TOKEN`, forwarded to the `claude -p` subprocess | Summarizes each window on the owner's flat-fee subscription ([C-08](../requirements/constraints.md)) |
| Digest Runner | Claude API (fallback only) | No stored credential: the job's managed identity trades its token with Entra, through a federated credential on a dedicated audience app registration, for a short-lived app token, then exchanges that at Anthropic (Workload Identity Federation) for a short-lived access token, once per fallback call ([ADR 9](../decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md)) | Re-runs a failed summarization against prepaid API credits. The federation rule matches the audience app's service principal object ID (the managed identity is trusted only by Entra, via the federated credential on that app); the service account sits in a dedicated workspace with a monthly spend limit |
| Digest Runner | News Site | `SITE_INGEST_KEY` sent as the `x-ingest-key` header on `PUT /ingest/:id` | Write-only: upsert one digest by id. Compared with hash-then-`timingSafeEqual`; the Worker fails closed (401) if the key is unset |
| Digest Runner | SMTP relay | `SMTP_USER` / `SMTP_PASSWORD` | Sends the digest as e-mail. Implemented and still the deployment role's default, but disabled on the real VM |
| Reader (owner) | News Site | Possession of the full URL, `https://…/t/<SITE_TOKEN>/…` | Read access to the whole digest archive. No account, no password — the token in the path **is** the authorization |
| Homelab deploy tooling | Azure Key Vault | Azure AD, from a separate repository | Fetches every runtime secret at deploy time |
| Homelab deploy tooling | GHCR | A read-only pull token | Pulls the pinned, versioned runtime image |

Two secrets on the News Site are deliberately independent, so that a leak of
one grants nothing over the other: `SITE_TOKEN` (reader capability, embedded
in the URL) and `INGEST_KEY` (write credential, sent only by the Digest
Runner, never shown to a reader). Both are compared with the same
hash-then-`timingSafeEqual` pattern in `workers/news-site/src/auth.js`.

### Secret flow

Every runtime secret this system uses is injected as an environment variable
at container start, sourced from Azure Key Vault by the separate homelab
deployment repository ([C-06](../requirements/constraints.md),
[C-07](../requirements/constraints.md)). None is ever committed, baked into
the runtime image, or passed as a build argument —
[`Dockerfile`](../../../Dockerfile) copies only application code and prompt
templates. `digest/config.py` is the **only** module in the codebase allowed
to read `os.environ`; every other module receives values passed in, and its
own error messages are written to name the offending variable without ever
echoing its value.

Secrets in play, by where they end up:

- **Personal account sessions/cookies** (`TG_SESSION`, X/Reddit/Patreon
  cookies): the sharpest secret class in this system — see "What is
  deliberately absent" below.
- **The News Site's own secrets** (`SITE_TOKEN`, `INGEST_KEY`): set with
  `wrangler secret put`, never in `wrangler.jsonc` or source.
- **The `claude -p` OAuth token, the SMTP password, the GHCR pull token**:
  standard service-style credentials, all Key Vault-sourced.
- **No Anthropic API credential exists.** The Claude API fallback uses
  workload identity federation: the rule, organization, service-account,
  audience and Entra tenant identifiers are configuration, not secrets, and
  the access token it yields lives only in memory for one call. The
  `claude -p` subprocess environment allowlist withholds
  `IDENTITY_ENDPOINT`/`IDENTITY_HEADER` (the managed identity's token service, which could mint tokens for the runner
  identity) and every `ANTHROPIC_*` variable. That is also a billing control:
  an `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` reaching the CLI would
  override `CLAUDE_CODE_OAUTH_TOKEN` and move every digest onto metered API
  billing. `AnthropicApiError` never carries a response body, token or URL.

At rest on the VM, one secret persists as a plain file rather than only living
in process memory: the X cookie jar. `X_COOKIES_PATH` seeds a "live" copy at
`/srv/appdata/digest/` that the collector rewrites as the session refreshes
(`digest/collectors/x.py`, `persist_cookies`) — the same local ext4 directory
that holds `state.db`. `.gitignore` excludes `*cookies*.json` and `*.session`
repository-wide as a backstop, not because either is expected to appear in
a working tree.

### Content-integrity control: the link allowlist

The summarizer's output is treated as untrusted before it is published. Two
independent controls sit between a model's markdown and a rendered page:

- **HTML sanitization.** `digest/emailer.py` runs the converted HTML through
  `nh3` (a Rust/Ammonia-based sanitizer) before it is stored or sent. The News
  Site never re-sanitizes `body_html`/`body_html_hu` on ingest — they are the
  only fields it inserts into a page without escaping, on the explicit
  assumption that the app already cleaned them
  (`workers/news-site/README.md`, "Trust model").
- **Anchor provenance.** `_enforce_anchor_provenance`
  (`digest/emailer.py`) unwraps any link in the sanitized HTML whose `href`
  is not one of the URLs actually collected for that digest (or, for a
  daily/weekly synthesis, re-derived from the window digests it summarizes —
  `get_daily_allowed_urls` / `get_weekly_allowed_urls` in
  `digest/state.py`). A model that fabricates or misattributes a citation
  produces plain text, never a live link.

This matters specifically because the content being defended against is the
model's own output, not an external attacker's input — the closest thing this
system has to an LLM-output sanitization boundary.

### What is deliberately absent, and why

This system runs no WAF, no intrusion detection, no SIEM, no automated key
rotation, and no MFA on any of its cookie/session-based collector
credentials. None of these are gaps waiting to be filled; each follows from
[C-01](../requirements/constraints.md) (one owner, one operator, one reader):

- **No WAF / rate limiting beyond what Cloudflare provides by default** in
  front of the News Site — there is no team to tune one, and the site's
  actual access control is the capability token, not request filtering.
- **No SIEM or centralized alerting on auth events** — a compromised
  Telegram/X/Reddit/Patreon session surfaces only as a collector auth
  failure in the run's own logs, or not at all, if the attacker uses the
  session quietly. This is a known, accepted blind spot, not a monitored one.
- **No automated secret rotation.** Every secret above is rotated by hand,
  on the owner's own schedule or on suspected compromise. The News Site's
  key-rotation runbook (`workers/news-site/README.md`, "Key rotation") is the
  only rotation procedure that exists in writing.
- **No MFA on the collector sessions**, because none of Telegram's MTProto
  user sessions, X's cookie auth, Reddit's session cookie, or Patreon's
  session cookie expose an MFA step to a long-lived, non-interactive
  session — this is a property of the accounts being used unofficially, not
  a control this system chose to skip.

### The sharpest risk: account-level compromise, not credential rotation

Every collection secret in this system (`TG_SESSION`, the X/Reddit/Patreon
cookies) authenticates as the **owner's own personal account**
([C-02](../requirements/constraints.md)), not a service principal or a bot.
Losing one of these secrets is an account takeover, not a service-credential
rotation: the blast radius is whatever that personal account can do (read the
owner's DMs and groups, post as the owner, in X's and Patreon's cases spend
the owner's own subscription standing), and recovery may require the
account's own recovery flow rather than issuing a new key. `twifork`'s pin
policy ([`../decisions/0001-pin-unofficial-x-client.md`](../decisions/0001-pin-unofficial-x-client.md))
exists partly because of this: an unaudited dependency bump touching the
authentication path is a materially worse event here than in a system backed
by rotatable service credentials.

See [trust-boundaries.md](trust-boundaries.md) for where these secrets travel
and [data-classification.md](data-classification.md) for how the content they
unlock is classified and where it ends up.

### Open points

- The X, Reddit, and Patreon collectors all depend on an unofficial,
  ToS-violating access path the owner has explicitly accepted the risk of
  ([C-03](../requirements/constraints.md),
  [A-02](../requirements/assumptions.md),
  [A-06](../requirements/assumptions.md)); none of the three has an
  official fallback if access is revoked.
- No detection exists inside this system for a compromised home-network
  boundary — that assumption is carried entirely by the homelab network,
  outside this repository's scope ([A-04](../requirements/assumptions.md)).
