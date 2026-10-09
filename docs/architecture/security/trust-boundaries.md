## Trust Boundaries

See the **Security** view (if registered) in
[`../workspace.dsl`](../workspace.dsl) for the modelled version; the group
boundaries below match those declared in
[`../model/containers.dsl`](../model/containers.dsl) and
[`../model/deployment.dsl`](../model/deployment.dsl) — "Azure subscription (private)"
and "Cloudflare edge (internet-facing)".

```text
  Third-party sources (Telegram, X, Reddit, Patreon,        [outbound only —
  Polymarket, Hacker News, RSS feeds)                        nothing here ever
        ^                                                    calls in]
        | (1) collection: outbound HTTPS/MTProto,
        |     personal-account credentials
        |
  ============ Azure subscription boundary ===========================
  Owner's subscription, one region (West Europe)            [A-04]
  Digest Runner  --(2) Blob state, lease-->  State Database (state.db bundle)
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
| 1 | Third-party sources ↔ Azure subscription | Outbound only, from the job | Telegram messages (MTProto), X notifications, Reddit/Patreon posts (HTTPS + cookie), Polymarket prices, Hacker News stories, RSS entries | Personal-account credentials held by the Digest Runner; nothing here ever accepts an inbound connection ([C-02](../requirements/constraints.md), [C-03](../requirements/constraints.md)) |
| 2 | Digest Runner ↔ State Database | Within the subscription, between the job and the runtime storage account | Collected items, cursors, digests, delivery flags, the live X cookie jar, as one immutable state bundle restored to the job's local disk and checkpointed back | The runner's user-assigned managed identity holds Blob Data Contributor on the runtime container only; shared-key access is disabled, so no key opens the account. An exclusive Blob lease on the manifest admits one run at a time, and a run that cannot prove it still holds the lease is killed. `state.db` itself is opened on the job's local disk, never over a network share |
| 3 | Digest Runner → internet (summarization/delivery) | Outbound only, from the job | The window's collected text, sent to `claude -p` (and, on failure, to the Claude API through a federated, short-lived token that the managed identity obtains by trading its own token with Entra, with no stored key); the rendered digest, sent to the News Site, Telegram, and (if enabled) SMTP | TLS on every leg; `SITE_INGEST_KEY` / `TELEGRAM_NOTIFY_BOT_TOKEN` / SMTP credentials authenticate the Runner to each destination |
| 4 | Digest Runner → News Site | Outbound only, from the job | One digest per `PUT /ingest/:id` call: `tldr`, `body_html`/`body_md` (pre-sanitized), counts, optional Hungarian fields, topics, deltas, provenance, arc-context primers | `x-ingest-key` header, hash-then-`timingSafeEqual` compare, fails closed if unset. The News Site has no corresponding path back into the subscription or `state.db` |
| 5 | Public internet ↔ News Site | Inbound to Cloudflare, outbound response | Reader HTTP requests and rendered HTML/JSON pages | Capability token in the URL path (`/t/<SITE_TOKEN>/…`); `Referrer-Policy: no-referrer`, `Cache-Control: private, no-store`, `X-Robots-Tag: noindex, nofollow` on every HTML response; a wrong token 404s indistinguishably from any other unknown path (`workers/news-site/README.md`, "Trust model") |
| 6 | News Site ↔ Site Database (D1) | Local to Cloudflare's platform | Digest rows, arc-context primers, push subscriptions | Cloudflare D1 binding; no external access path. D1 is a disposable rendering copy — the `state.db` bundle in Azure Blob Storage is the system of record ([`../data/data-architecture.md`](../data/data-architecture.md)) |

### What never crosses a boundary

- Nothing in the Azure subscription accepts an inbound connection from the
  internet: the jobs have no ingress, and the storage accounts and the digest
  Key Vault are reached only with an Entra identity. Every source relationship
  in boundary 1 and every delivery relationship in boundaries 3–4 is initiated
  by the Digest Runner, outbound ([A-04](../requirements/assumptions.md)).
- The News Site has no write path into `state.db` and no credential that
  would let it reach the Azure subscription. Ingest is one-directional:
  runner → site.
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
- **The Azure boundary (A-04) is an identity boundary, not a network one.**
  The jobs have no listener, but the storage accounts and the digest Key
  Vault answer on their public endpoints to anyone holding a valid Entra
  token with a role assignment; the repository declares no private endpoint or
  network rule, and no intrusion detection of its own. Access control is the
  role assignments on the runner and backup-writer identities and the
  owner's own Entra account, which this repository cannot monitor.
- **Delivery tooling crosses a different boundary than any digest run.** A
  release (`git tag` → GitHub Actions → GHCR → a Renovate-merged image pin →
  `azure-deploy` → Azure) moves code and job configuration, not digest
  content. It authenticates to Azure with OIDC federation from GitHub
  environments restricted to `main`, holding no stored cloud credential, and
  Azure pulls the image with a read-only token kept in the digest Key Vault.
  There is no SSH or Ansible path. See
  [`../model/people-systems.dsl`](../model/people-systems.dsl) for those
  relationships; they are modelled separately from a digest run on purpose.
- **The deploy path runs in public.** The repository and its workflow logs are
  readable by anyone. Plan logs mask the job settings' values, and secrets are
  Key Vault references, never Terraform inputs. The short-lived plan artifact
  is the exception: it holds the nonsecret settings in clear until the plan
  is applied, at most a day ([RISK-012](../risks/architecture-risks.md)).

### Rules

- No component outside the Digest Runner and the State Database reads
  `state.db` directly; every other consumer of digest content goes through
  the News Site, Telegram, or e-mail.
- The News Site's two secrets (`SITE_TOKEN`, `INGEST_KEY`) stay independent:
  compromising the reader-facing token must never grant ingest write access,
  and vice versa.
- Support for a new collection source only ever adds an outbound
  relationship from the Digest Runner; it must never require ingress on the
  job.
