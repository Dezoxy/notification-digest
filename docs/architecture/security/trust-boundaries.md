## Trust Boundaries

See the **Security** view (if registered) in
[`../workspace.dsl`](../workspace.dsl) for the modelled version; the group
boundaries below match those declared in
[`../model/containers.dsl`](../model/containers.dsl) and
[`../model/deployment.dsl`](../model/deployment.dsl) — "Homelab VM (private)"
and "Cloudflare edge (internet-facing)".

```text
  Third-party sources (Telegram, X, Reddit, Patreon,        [outbound only —
  Polymarket, Hacker News, RSS feeds)                        nothing here ever
        ^                                                    calls in]
        | (1) collection: outbound HTTPS/MTProto,
        |     personal-account credentials
        |
  ============ Home LAN boundary =====================================
  Owner's flat, one Proxmox node, one VM (01-myapps-vm)     [A-04]
  Digest Runner  --(2) local file I/O-->  State Database (state.db)
        |
        | (3) delivery: outbound HTTPS
        v
  ============ Internet =============================================
        |                                  |
        v                                  v
  Telegram Bot API                 SMTP relay (implemented,
  (TL;DR ping)                      disabled on the live host)
        |
        | (4) publish: outbound HTTPS, shared-secret ingest
        v
  ============ Cloudflare edge boundary ==============================
  News Site (Worker)  <-- (5) reader traffic, capability-token auth
        |
        v
  Site Database (D1) — disposable rendering copy
```

A second, unrelated set of crossings moves code and configuration, not
digest data — see "Delivery tooling" below.

### Boundaries

| # | Boundary | Direction | What crosses it | Control |
|---|---|---|---|---|
| 1 | Third-party sources ↔ home LAN | Outbound only, from the VM | Telegram messages (MTProto), X notifications, Reddit/Patreon posts (HTTPS + cookie), Polymarket prices, Hacker News stories, RSS entries | Personal-account credentials held by the Digest Runner; nothing here ever accepts an inbound connection ([C-02](../requirements/constraints.md), [C-03](../requirements/constraints.md)) |
| 2 | Digest Runner ↔ State Database | Local, within the VM | Collected items, cursors, digests, delivery flags | Local filesystem permissions only; both containers run on the same VM and the same non-root user. `state.db` sits on local ext4 — SQLite fails to open on the VM's virtiofs share, so this path is also a reliability boundary, not just a security one |
| 3 | Digest Runner → internet (summarization/delivery) | Outbound only, from the VM | The window's collected text, sent to `claude -p` (and, on failure, OpenRouter); the rendered digest, sent to the News Site, Telegram, and (if enabled) SMTP | TLS on every leg; `SITE_INGEST_KEY` / `TELEGRAM_NOTIFY_BOT_TOKEN` / SMTP credentials authenticate the Runner to each destination |
| 4 | Digest Runner → News Site | Outbound only, from the VM | One digest per `PUT /ingest/:id` call: `tldr`, `body_html`/`body_md` (pre-sanitized), counts, optional Hungarian fields, topics, deltas, provenance, arc-context primers | `x-ingest-key` header, hash-then-`timingSafeEqual` compare, fails closed if unset. The News Site has no corresponding path back into the VM or `state.db` |
| 5 | Public internet ↔ News Site | Inbound to Cloudflare, outbound response | Reader HTTP requests and rendered HTML/JSON pages | Capability token in the URL path (`/t/<SITE_TOKEN>/…`); `Referrer-Policy: no-referrer`, `Cache-Control: private, no-store`, `X-Robots-Tag: noindex, nofollow` on every HTML response; a wrong token 404s indistinguishably from any other unknown path (`workers/news-site/README.md`, "Trust model") |
| 6 | News Site ↔ Site Database (D1) | Local to Cloudflare's platform | Digest rows, arc-context primers, push subscriptions | Cloudflare D1 binding; no external access path. D1 is a disposable rendering copy — `state.db` on the VM is the system of record ([`../data/data-architecture.md`](../data/data-architecture.md)) |

### What never crosses a boundary

- Nothing on the home LAN accepts an inbound connection from the internet;
  every source relationship in boundary 1 and every delivery relationship in
  boundaries 3–4 is initiated by the Digest Runner, outbound
  ([A-04](../requirements/assumptions.md)).
- The News Site has no write path into `state.db` and no credential that
  would let it reach the home LAN. Ingest is one-directional: VM → site.
- The reader capability token (`SITE_TOKEN`) authenticates only to the News
  Site. It is never sent to, or accepted by, the Digest Runner, Telegram, or
  any collection source.

### Where a boundary is weaker than it looks

- **The capability token is not a secret to every system that touches it.**
  It appears in the URL path, so it lands in Cloudflare's own request logs
  and traces (`workers/news-site/wrangler.jsonc`'s `observability` block),
  in a reader's browser history, and — since the PWA shell shipped — in an
  installed shortcut's `start_url` and the service worker's own registered
  scope. This is accepted, not overlooked: the account has one operator, and
  `Referrer-Policy: no-referrer` is what stops the token leaking onward to a
  third-party site through an outbound citation link.
- **The home LAN boundary (A-04) is not enforced by anything inside this
  repository.** This system assumes the VM is unreachable from the internet;
  it has no firewall configuration, no network segmentation, and no
  intrusion detection of its own. That assumption is carried by the homelab
  network itself, outside this repository's scope.
- **Delivery tooling crosses a different boundary than any digest run.** A
  release (`git tag` → GitHub Actions → GHCR → the separate homelab
  repository → Ansible over SSH → the VM) moves code and secrets-fetching
  configuration, not digest content, and uses its own credentials (a GHCR
  pull token, Key Vault access via Azure AD). See
  [`../model/people-systems.dsl`](../model/people-systems.dsl) for those
  relationships; they are modelled separately from a digest run on purpose.

### Rules

- No component outside the Digest Runner and the State Database reads
  `state.db` directly; every other consumer of digest content goes through
  the News Site, Telegram, or e-mail.
- The News Site's two secrets (`SITE_TOKEN`, `INGEST_KEY`) stay independent:
  compromising the reader-facing token must never grant ingest write access,
  and vice versa.
- Support for a new collection source only ever adds an outbound
  relationship from the Digest Runner; it must never require an inbound port
  on the VM.
