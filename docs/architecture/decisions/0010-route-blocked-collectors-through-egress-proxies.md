# 10. Route blocked collectors through egress proxies on Cloudflare Workers

Date: 2026-10-22

## Status

Accepted for Polymarket; the Reddit part is superseded by
[11. Move Reddit to a homelab proxy behind Cloudflare Access](0011-move-reddit-to-a-homelab-proxy.md)
and then by [12. Retire Reddit collection](0012-retire-reddit-collection.md)

Records the existing Polymarket proxy choice and extends it to Reddit. It
amends nothing: no earlier ADR decided how a blocked source is reached.

Implemented and deployed on 2026-10-22 (release 0.29.9, then the production
configuration switched to the Reddit proxy at about 09:00 UTC).

### Amendment, 2026-10-22: the Reddit path failed

The text below is the original decision and is left as written. The 12:00 UTC run
collected 39 Reddit items through the Worker. From the 18:00 UTC run on, Reddit
answered logged-in requests from Cloudflare egress with its "blocked by network
security" page (HTTP 403), while anonymous requests from the same network were
answered normally. [RISK-014](../risks/architecture-risks.md), which this record
named, had occurred. Reddit was disabled in production
(`REDDIT_ENABLED=false`, about 19:30 UTC) and moves to this record's option 2,
built on the homelab; see ADR 11 for the evidence and the interface. ADR 11's
own amendment of 2026-10-23 corrects the cause: the block was on the client
fingerprint, not on Cloudflare egress, and ADR 12 retires Reddit collection. The
Polymarket Worker is unaffected and this record stays in force for it. The
"unverified end to end" consequence below no longer applies: the path was
observed and failed.

## Context

Two collection sources cannot be reached directly from where the digest runs.

Polymarket: polymarket.com is blocked by ISPs in Hungary, where the digest ran
on the homelab VM until the 2026-10-08 Azure cutover. The collector reaches the
Gamma API through a small Cloudflare Worker (`polymarket-proxy`) set as
`POLYMARKET_API_BASE`, authenticated with `POLYMARKET_PROXY_KEY`. Both settings
exist since the collector's first commit (76d5e07, 2026-08-05), which is the
earliest date this repository shows for the choice; no decision record existed
before this one. Production still points at it.

Reddit: from about 2026-10-21 (between 12:06 and 18:01 UTC) Reddit answers every
request from Azure egress (AS8075) with `403 Blocked`. Probes with the
collector's User-Agent, anonymous:

- from a temporary job in the digest's own Container Apps environment,
  `old.reddit.com/api/me.json` returned `403 Blocked`;
- from a Cloudflare Worker (AS13335) it returned a `302` to the login page;
- from a residential IP it returned a `302` to the login page.

A rotated, valid session cookie (JWT expiry 2027-04) still failed from Azure. So
the block is on the network, not the credential. Reddit collection had already
been reduced to a cookie session after the Data Team refused the owner's
official API application on 2026-08-06
([RISK-006](../risks/architecture-risks.md)).

## Decision drivers

- Reddit collection must work again from Azure without moving execution off
  Azure ([ADR 7](0007-run-digest-as-azure-jobs.md)).
- One failing source must not fail the run
  ([P-05](../principles/architecture-principles.md)); a new dependency has to
  keep that isolation.
- No new infrastructure to run for a single-owner service
  ([C-01](../requirements/constraints.md)).
- Reuse a pattern already in production rather than invent a second one.

## Considered options

1. The official Reddit Data API with OAuth. The Data Team formally refused the
   owner's application on 2026-08-06, so this is not available.
2. A proxy on the homelab behind Cloudflare Tunnel, protected by a Cloudflare
   Access service token (the jobs-dashboard pattern in the owner's `toom-edge`
   repository). Kept as the fallback if Reddit also blocks Cloudflare.
3. A Cloudflare Worker proxy that mirrors `polymarket-proxy`.
4. Disable Reddit collection. Considered as a stop-gap only.

## Decision

Option 3. `reddit-proxy`, a Cloudflare Worker, relays the collector's two Reddit
requests. Like `polymarket-proxy`, it lives in the owner's `Dezoxy/toom-edge`
repository (`workers/polymarket-proxy`, `workers/reddit-proxy`), is deployed on
workers.dev and is outside Terraform. The collector reads
`REDDIT_API_BASE` and the secret `REDDIT_PROXY_KEY` the same way it reads the
Polymarket pair, and sends the key in an `x-proxy-key` header.

Neither Worker is an open relay:

- the upstream host is hardcoded;
- GET only;
- a path allowlist (Polymarket: `/markets`; Reddit: `/api/me.json` and
  `/r/<sub>/top.json`, with only the query parameters `t`, `limit` and
  `raw_json`);
- a timing-safe comparison of the shared key.

`reddit-proxy` forwards only the cookie, user-agent and accept headers upstream
and returns only content-type. It drops `Location` and `Set-Cookie`, so a
not-logged-in session surfaces in the collector as HTTP 302. It logs nothing and
has Workers Logs disabled, because its requests carry the owner's Reddit session
cookie. The cookie stays in the digest's Key Vault and is sent with every
request; the Worker stores nothing.

The model draws both Workers as one external system, Egress Proxies, between the
runner and Reddit/Polymarket. No direct runner-to-source arrow remains for
those two sources.

Shared-key rotation: generate a key with `openssl rand -hex 32`, set it on the
Worker with `wrangler secret put PROXY_KEY` (no trailing newline), and update the
vault secret with `scripts/rotate_reddit_cookie.py --proxy-key --key-file ...
--proxy-url ...`.

## Consequences

Positive:

- No homelab dependency and nothing new to host; the pattern already exists for
  Polymarket.
- Failure stays isolated per collector as today: a Cloudflare outage fails only
  the Reddit and Polymarket collectors, each flagged on its own.
- Returning to a direct path, or to option 2, changes only `REDDIT_API_BASE`.

Negative / accepted trade-offs:

- Reddit and Polymarket collection now depend on Cloudflare Workers.
- The owner's Reddit session cookie transits the owner's own Cloudflare account,
  decrypted at the edge. It is the same account that already serves the news
  site and the Polymarket proxy. The Worker keeps no copy and logs nothing, but
  that is a property of its code and settings, not of this repository's
  controls.
- Both Workers are internet-reachable workers.dev endpoints guarded only by the
  shared key.
- Reddit may block Cloudflare egress as well
  ([RISK-014](../risks/architecture-risks.md)). The fallback is option 2.
- The terms-of-service posture of the Reddit collector is unchanged
  ([RISK-007](../risks/architecture-risks.md)); a proxy does not make an
  unofficial cookie session official.
- The Worker code is in another repository, so a change to either Worker is not
  reviewed with this one.
- The Reddit path is unverified end to end until the first proxied run is
  observed.

## Risks

- [RISK-006](../risks/architecture-risks.md): the Azure block is the second
  observed loss of free Reddit access.
- [RISK-014](../risks/architecture-risks.md): Reddit extends the block to
  Cloudflare egress.
- [RISK-002](../risks/architecture-risks.md): the session cookie now also
  crosses the Cloudflare edge.

## Related

- Requirements: [P-05](../principles/architecture-principles.md),
  [C-01](../requirements/constraints.md)
- Architecture views: `Sources` and `Landscape` show Egress Proxies; `Containers`
  omits it with the sources.
- Other ADRs: builds on [7. Run digest as Azure jobs](0007-run-digest-as-azure-jobs.md)
- Why the Polymarket proxy was chosen over alternatives when it was introduced:
  not recorded.
