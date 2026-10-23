# 12. Collect Reddit from public RSS feeds through the homelab proxy

Date: 2026-10-23

## Status

Accepted

Supersedes the logged-in cookie route of
[11. Move Reddit to a homelab proxy behind Cloudflare Access](0011-move-reddit-to-a-homelab-proxy.md):
its premise (Reddit blocks by network) was disproved on 2026-10-23, and the
cookie is no longer used. The homelab proxy, the Cloudflare Access application,
the service token `digest-reddit` and the `x-proxy-key` header from ADR 11 are
reused unchanged. The Reddit part of
[10. Route blocked collectors through egress proxies on Cloudflare Workers](0010-route-blocked-collectors-through-egress-proxies.md)
stays superseded; ADR 10 still holds for Polymarket.

Decided on 2026-10-23. The edge and homelab side (tunnel route, Access
application, service token, `reddit-proxy` Compose service) and the Access-header
support in the collector (release 0.29.10) are already deployed. The anonymous
feed route and the collector change are **not yet released or enabled**: Reddit
stays disabled in production (`REDDIT_ENABLED=false`) until the cutover below.
The Structurizr model still draws the Worker path; it is updated at cutover.

## Context

ADR 11 assumed that Reddit refuses logged-in sessions from Cloudflare egress and
that a residential IP would fix it. It built that route and deployed it. Tests on
2026-10-23, with the owner's freshly copied logged-in cookie against
`/api/me.json`, disproved the assumption (full table in the amendment to ADR 11):
the Mac mini proxy returned 403, and so did curl on the Mac mini host and in
another container, straight to Reddit with no Cloudflare in the path. The same
request from the owner's macOS laptop returned 200 logged in, including with the
collector's own Python verify code and with the proxy script itself. The laptop
and the Mac mini share one public IPv4 address and resolve the same Reddit edge
addresses.

So the block is not about the IP or about Cloudflare. Reddit identifies the
client below the User-Agent (the TLS and operating-system network fingerprint)
and refuses logged-in sessions from these Linux clients. Every Linux source
failed (Azure, Workers, Mac mini); every macOS source worked. The Worker timing
on 2026-10-22 (working at 12:00 UTC, failing at 18:00 UTC) is better read as
this filtering rolling out than as a block on Cloudflare egress. Anonymous
requests are not refused: without a cookie the same chain returns 302, Reddit's
normal anonymous answer.

Anonymous access has a very small budget. Responses carry
`x-ratelimit-remaining: 0.0` and an `x-ratelimit-reset` of about 38 seconds
after a single request, and a burst of requests received HTTP 429.

## Decision drivers

- No deliberate work-around of Reddit's automated-access block. The owner will
  not imitate a browser fingerprint, because that is evasion and puts the owner's
  personal Reddit account at risk of a lock
  ([RISK-007](../risks/architecture-risks.md)).
- Keep the personal session cookie out of every path where it can leak
  ([RISK-002](../risks/architecture-risks.md), [C-02](../requirements/constraints.md)).
- One failing source must not fail the run
  ([P-05](../principles/architecture-principles.md)).
- Reuse what is already built and deployed: the Access application, the token
  and the Mac mini service.
- Azure egress is still blocked by Reddit, so a proxy stays on the path.

## Considered options

1. Keep the logged-in route and imitate a browser's TLS and network fingerprint
   from a Linux client. Rejected: it would deliberately work around Reddit's
   automated-access block and would risk a lock on the owner's personal account.
   It would also keep the cookie in the vault and on the wire.
2. Drop Reddit. Not chosen now; it is the fallback if the anonymous feeds also
   stop working or the pacing cost becomes unacceptable.
3. The official Reddit Data API. Refused by Reddit's Data Team on 2026-08-06
   ([RISK-006](../risks/architecture-risks.md)); unchanged.
4. Anonymous public Atom feeds
   (`https://www.reddit.com/r/<sub>/top/.rss?t=day&limit=N`), fetched through
   the existing homelab proxy. Chosen.

## Decision

Option 4. Reddit is collected from its anonymous public RSS (Atom) feeds, with
no cookie anywhere, through the proxy that ADR 11 introduced.

- **Proxy route.** The `reddit-proxy` service on the Mac mini is now anonymous
  and RSS-only. Its one route is `/r/<sub>/top/.rss`, with only the query
  parameters the collector sends (`t`, `limit`). It forwards no cookie. The
  upstream is hardcoded, the method is GET only, and the `x-proxy-key` check
  stays as defence in depth. Redirects are not followed.
- **Access and headers.** Unchanged from ADR 11: the dedicated Access
  application for `reddit-proxy.toomhorvath.com` admits only the service token
  `digest-reddit`. The collector sends `CF-Access-Client-Id`,
  `CF-Access-Client-Secret` and `x-proxy-key`. `REDDIT_API_BASE` is
  `https://reddit-proxy.toomhorvath.com`.
- **No session.** The collector no longer reads `REDDIT_SESSION_COOKIE`, and no
  longer verifies a login. The cookie secret and
  `scripts/rotate_reddit_cookie.py` are obsolete for the collector.
- **Pacing.** The anonymous budget allows roughly one request per reset window,
  so the collector waits between subreddits. It reads
  `x-ratelimit-reset` and waits that long plus 2 seconds. When the header is
  absent it waits `REDDIT_REQUEST_INTERVAL_SECONDS` (default 65). No single wait
  exceeds `REDDIT_MAX_WAIT_SECONDS` (default 120). For five subreddits this adds
  roughly 5 minutes to a run, against a job timeout of 50 minutes.
- **Failure.** HTTP 429, 401 or 403 abort the rest of that run's Reddit
  collection: Reddit is marked failed, the digest ships with a banner, and the
  next run retries. The collector does not retry in a loop.
- **Item identity.** `source_id` stays the bare post id, so switching from the
  JSON route creates no duplicates.
- **What the items lose.** The feed carries no score and no comment count, so
  Reddit items no longer have them.

Cutover order:

1. Deploy the proxy change on the Mac mini (the edge stage recreates only
   `reddit-proxy`).
2. Release and deploy the digest version with the feed collector.
3. Through the azure-deploy plan and apply, set `app_env`
   `REDDIT_API_BASE=https://reddit-proxy.toomhorvath.com`, add the two Access
   secret names (`REDDIT_ACCESS_CLIENT_ID`, `REDDIT_ACCESS_CLIENT_SECRET`) and
   set `REDDIT_ENABLED=true`.
4. Verify that the next window run's `run_summary` reports `"reddit": "ok"`.
5. Later: remove `REDDIT_SESSION_COOKIE` from `secret_names` and delete the
   secret from the vault; retire the `reddit-proxy` Worker in `toom-edge`;
   update the Structurizr model (Egress Proxies) to the new path.

## Consequences

Positive:

- No deliberate evasion of Reddit's block, and no risk to the personal account
  from this collector.
- The personal Reddit cookie no longer leaves the Key Vault at all and no
  longer crosses Cloudflare's edge. The Reddit share of RISK-002 and RISK-007
  exposure ends.
- Everything built for ADR 11 is reused.
- The feeds are public, so the collector has one credential fewer to rotate.

Negative / accepted trade-offs:

- Reddit items carry no score or comment count, so the summary can rank them
  only by the feed's own top-of-day order.
- Reddit collection still depends on the homelab: the Mac mini, the home
  internet connection and the Tunnel. Polymarket does not.
- Pacing adds roughly 5 minutes to every run for five subreddits. A run that
  gets HTTP 429 loses Reddit for that run.
- Anonymous access is the least stable contract Reddit offers; it can be
  tightened or removed without notice ([RISK-006](../risks/architecture-risks.md),
  [RISK-014](../risks/architecture-risks.md)).
- The origin code and the Access application stay in other repositories.
- Until the cutover, Reddit items are absent from every digest.

## Risks

- [RISK-014](../risks/architecture-risks.md): re-described; the cause was the
  client fingerprint, not the network, and the residual risk is now the
  anonymous feeds being limited or removed.
- [RISK-002](../risks/architecture-risks.md): the Reddit cookie no longer
  leaves the vault, so its exposure drops once `REDDIT_SESSION_COOKIE` is
  removed.
- [RISK-006](../risks/architecture-risks.md) and
  [RISK-007](../risks/architecture-risks.md): anonymous public feeds are a lower
  ToS exposure than a personal cookie session, but the posture is not formally
  accepted for them; unchanged otherwise.

## Related

- Requirements: [P-05](../principles/architecture-principles.md),
  [C-01](../requirements/constraints.md), [C-02](../requirements/constraints.md)
- Other ADRs: supersedes the cookie route of
  [11. Move Reddit to a homelab proxy behind Cloudflare Access](0011-move-reddit-to-a-homelab-proxy.md);
  Reddit part of
  [10. Route blocked collectors through egress proxies on Cloudflare Workers](0010-route-blocked-collectors-through-egress-proxies.md)
  stays superseded
- Operations: [Reddit cutover](../../azure-migration.md#credential-rotation-after-cutover)
