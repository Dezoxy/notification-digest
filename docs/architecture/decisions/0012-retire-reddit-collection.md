# 12. Retire Reddit collection

Date: 2026-10-23

## Status

Accepted

Supersedes
[11. Move Reddit to a homelab proxy behind Cloudflare Access](0011-move-reddit-to-a-homelab-proxy.md)
entirely, and the Reddit part of
[10. Route blocked collectors through egress proxies on Cloudflare Workers](0010-route-blocked-collectors-through-egress-proxies.md).
ADR 10 stays in force for Polymarket.

Decided on 2026-10-23. Reddit has been disabled in production
(`REDDIT_ENABLED=false`) since about 19:30 UTC on 2026-10-22. This record ends
the collector for good; the code and configuration removal and the operator
teardown below follow it.

## Context

The chain of events, all of it observed:

- **2026-10-21.** Reddit collection from Azure began failing. The first failure
  log line carried no HTTP status code, which led to it being read as a block on
  Azure's IP range.
- **2026-10-22.** [ADR 10](0010-route-blocked-collectors-through-egress-proxies.md)
  routed Reddit through a Cloudflare Worker. It collected 39 items in the 12:00
  UTC run and failed from the 18:00 UTC run on. Reddit was disabled.
- **2026-10-22 to 23.** [ADR 11](0011-move-reddit-to-a-homelab-proxy.md) built a
  proxy on the Mac mini homelab behind Cloudflare Access, on the premise that a
  residential IP would be accepted. Tests on 2026-10-23 disproved it: the same
  logged-in cookie returned 403 from the Mac mini, from a container and from the
  host straight to Reddit, and 200 from the owner's macOS laptop on the same
  public IPv4 address.
- **Cause.** Reddit identifies the client below the User-Agent and refuses
  logged-in sessions from Linux clients, whatever the network. An anonymous RSS
  request from an Azure Cloud Shell on 2026-10-23 returned 200, so Azure was never
  blocked for anonymous traffic. The 2026-10-21 failure was this client block, not
  an IP block.
- **Anonymous RSS works, but is ending.** Reddit announced on r/modnews
  (2026-09-30) that RSS feeds end on 2026-11-13 with no replacement for feed
  readers. New public API access requests stop on 2026-10-31, cut-offs for
  unregistered apps and users begin on 2027-01-12, and the public API is retired
  by March 2027. Only registered API users are unaffected. The owner's official
  API application was refused on 2026-08-06.

An RSS route was drafted as a replacement and dropped when the end date became
known.

## Decision drivers

- No deliberate work-around of Reddit's automated-access block: no browser
  imitation, no account risk ([RISK-007](../risks/architecture-risks.md)).
- Do not build on a channel with a published end date.
- Fewer moving parts and no personal credential in a path that keeps breaking
  ([RISK-002](../risks/architecture-risks.md),
  [C-02](../requirements/constraints.md)).
- The owner's coverage of those subreddits already comes from other sources: news
  RSS, Hacker News and X.

## Considered options

1. Imitate a browser's TLS and network fingerprint from a Linux client. Rejected:
   it deliberately evades Reddit's block and risks a lock on the owner's personal
   account.
2. Anonymous public RSS feeds through the homelab proxy. Rejected: feeds end on
   2026-11-13, and the anonymous budget (about one request per 38 seconds) made
   the collector slow for a few weeks of use.
3. A third-party scraping API (for example a crypto-paid "Reddit fetcher").
   Rejected: opaque collection, no terms, and the same scraping outsourced.
4. Re-apply for the official Data API before 2026-10-31. Open to the owner as a
   personal choice; the digest does not depend on it, and a grant would be a new
   decision.
5. Retire Reddit from the digest. Chosen.

## Decision

Option 5. Reddit is retired as a source in every repository and no replacement is
built.

- **notification-digest.** The collector and all `REDDIT_*` configuration are
  removed.
- **toom-macmini-homelab.** The `reddit-proxy` Compose service is removed; the
  homelab is no longer on the digest's collection path.
- **toom-edge.** The `reddit-proxy` tunnel route, its dedicated Cloudflare Access
  application, the `digest-reddit` service token and its outputs, and the
  `reddit-proxy` Worker code are removed.
- **Data.** Items already collected with source `reddit` stay in the state
  database and on the published site.

Operator teardown, after the code-removal change has merged and been released
(procedure in the
[runbook](../../azure-migration.md#reddit-retired)):

1. Remove `REDDIT_*` and the Reddit `secret_names` from production `app_env`
   through the azure-deploy plan and apply.
2. Delete the Key Vault secrets `digest-reddit-session`,
   `digest-reddit-proxy-key`, `digest-reddit-access-client-id` and
   `digest-reddit-access-client-secret` in the digest vault, and
   `edge-reddit-proxy-key` in the homelab vault.
3. `wrangler delete` the `reddit-proxy` Worker.

## Consequences

Positive:

- Fewer moving parts: one fewer collector, two fewer proxy paths, one fewer
  Access application.
- No personal Reddit credential anywhere, so Reddit leaves
  [RISK-002](../risks/architecture-risks.md) and
  [RISK-007](../risks/architecture-risks.md).
- The Mac mini homelab is no longer on the digest path; Polymarket never used it.
- [RISK-014](../risks/architecture-risks.md) is closed by retirement.

Negative / accepted trade-offs:

- The coverage those subreddits gave is lost. It is accepted: news RSS, Hacker
  News and X cover the same ground for the owner, and no Reddit replacement is
  built.
- A future return to Reddit would need an approved API route and a new decision.

## Risks

- [RISK-014](../risks/architecture-risks.md): closed by retirement.
- [RISK-002](../risks/architecture-risks.md),
  [RISK-006](../risks/architecture-risks.md),
  [RISK-007](../risks/architecture-risks.md): Reddit no longer counts toward them.

## Related

- Requirements: [C-02](../requirements/constraints.md)
- Other ADRs: supersedes
  [11. Move Reddit to a homelab proxy behind Cloudflare Access](0011-move-reddit-to-a-homelab-proxy.md);
  supersedes the Reddit part of
  [10. Route blocked collectors through egress proxies on Cloudflare Workers](0010-route-blocked-collectors-through-egress-proxies.md)
- Operations: [Reddit retired](../../azure-migration.md#reddit-retired)
