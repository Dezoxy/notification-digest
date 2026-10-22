# 11. Move Reddit to a homelab proxy behind Cloudflare Access

Date: 2026-10-22

## Status

Accepted

Supersedes the Reddit part of
[10. Route blocked collectors through egress proxies on Cloudflare Workers](0010-route-blocked-collectors-through-egress-proxies.md):
its option 3 as applied to Reddit, and the `reddit-proxy` Worker. ADR 10 stays in
force for Polymarket, whose Worker is unaffected.

Decided on 2026-10-22; **not built**. Until it is, Reddit collection is disabled
in production (`REDDIT_ENABLED=false`). The Structurizr model still draws the
Reddit path as the Worker; it is updated at cutover, when the path actually
changes.

## Context

ADR 10 sent Reddit through the Cloudflare Worker `reddit-proxy` after Reddit
blocked Azure egress (AS8075) on 2026-10-21. It went live with release 0.29.9 at
about 09:00 UTC on 2026-10-22, and the 12:00 UTC run collected 39 Reddit items
through it. From the 18:00 UTC run on, the collector's session verify received
`HTTPError 403 Forbidden`.

Nothing the owner controls had changed: the last proxy-key change on the Worker
was at 08:54 UTC, and no Key Vault grant or job setting changed after about
09:00. Probes on the same day isolated each factor:

- The key alone, without a cookie, through the Worker returned HTTP 302. The
  Worker accepted the key and Reddit answered an anonymous request normally.
- The owner's freshly copied, logged-in browser cookie through the Worker
  returned HTTP 403 with Reddit's HTML page "blocked by network security". The
  result was identical with the collector's User-Agent and with a Chrome
  User-Agent.
- The same cookie sent directly from the owner's home network returned HTTP 200,
  logged in.

So Reddit blocks logged-in sessions that arrive from Cloudflare egress, while
anonymous traffic from the same network is not blocked. The key, the cookie and
the User-Agent are not the cause. This is the event
[RISK-014](../risks/architecture-risks.md) described; it has occurred.

Diagnosis took longer than it should have because three different failures reach
the collector as the same `HTTPError 403 Forbidden`: a Cloudflare 1010 block, the
Worker's own key rejection (`{"error":"forbidden"}`) and Reddit's network block.
The collector now classifies 403s (proxy rejected the key, Cloudflare 1010 block,
Reddit network block, unclassified) and 3xx responses (Access login redirect, not
logged in) in its single verify-failure log line, and no longer follows
redirects.

## Decision drivers

- Reddit must be reached from a network Reddit does not block for a logged-in
  session. The one network known to work is the residential one the owner's own
  browser session already lives on.
- One failing source must not fail the run
  ([P-05](../principles/architecture-principles.md)).
- Execution stays on Azure ([ADR 7](0007-run-digest-as-azure-jobs.md)); only the
  Reddit request leaves it.
- The pattern should already exist in the owner's infrastructure: the jobs
  dashboard in `toom-edge` is protected by Cloudflare Access with a service
  token.

## Considered options

1. Keep the Worker. Rejected: the block is on Cloudflare egress for logged-in
   sessions, so no change on the Worker side can fix it.
2. A proxy on the owner's homelab behind Cloudflare Tunnel, protected by a
   Cloudflare Access service token (ADR 10's option 2). Chosen.
3. Disable Reddit collection permanently. In force as the stop-gap until option 2
   is built; not a lasting answer.
4. The official Reddit Data API: refused on 2026-08-06
   ([RISK-006](../risks/architecture-risks.md)).

## Decision

Option 2, built on the owner's Mac mini homelab (repository
`toom-macmini-homelab`, Ubuntu, a Docker Compose project that already runs the
Cloudflare Tunnel connector `cloudflared`), not on the old Proxmox VM. The
interface below is agreed across this repository, `toom-macmini-homelab` and
`toom-edge`.

- **Hostname and route.** Public hostname `reddit-proxy.toomhorvath.com`, routed
  by the existing homelab Tunnel. In `toom-edge` that is
  `homelab/tunnel-subdomains.txt` plus `local.homelab_origins`, pointing at
  `http://reddit-proxy:8080`.
- **Access.** A dedicated Cloudflare Access application for that hostname, like
  `jobs.toomhorvath.com` in `toom-edge`. Its only policy is Service Auth for a
  new Access service token, `digest-reddit`. No human has access. It must not
  ride the `*.toomhorvath.com` wildcard application, because a service-auth
  policy there would admit the token to every subdomain.
- **Origin.** A Compose service `reddit-proxy` on the Mac mini, listening on 8080
  on the Compose network with no published host port, reachable only through the
  Tunnel. Its behaviour is identical to the Worker's: hardcoded upstream
  `old.reddit.com`; GET only; a path allowlist (`/api/me.json` and
  `/r/<sub>/top.json`, with only the query parameters `t`, `limit` and
  `raw_json`); a timing-safe `x-proxy-key` check against the same value as
  `REDDIT_PROXY_KEY`, kept as defence in depth behind Access; only the cookie,
  user-agent and accept headers forwarded; only content-type returned; `Location`
  and `Set-Cookie` dropped; no redirects followed; nothing logged that carries
  the cookie.
- **Collector.** `REDDIT_API_BASE=https://reddit-proxy.toomhorvath.com`, plus two
  new secrets, `REDDIT_ACCESS_CLIENT_ID` and `REDDIT_ACCESS_CLIENT_SECRET` (Key
  Vault `digest-reddit-access-client-id` and `digest-reddit-access-client-secret`,
  both or neither), sent as `CF-Access-Client-Id` and `CF-Access-Client-Secret`.
  The existing `x-proxy-key` header stays.
- **Why it should work.** Reddit then sees the owner's residential IP, the
  network on which the owner's own browser session already works. This is
  inferred from the direct-from-home probe above; it is not yet observed through
  the new path.

Cutover order:

1. Build and deploy the Mac mini service and the `toom-edge` changes (tunnel
   route, Access application, service token).
2. Store the two token values in the digest Key Vault and verify with a probe.
3. Set `REDDIT_API_BASE` and the new secret names, and `REDDIT_ENABLED=true`,
   through the azure-deploy plan and apply.
4. Update the Structurizr model (Egress Proxies) to show the new Reddit path.
5. Retire the `reddit-proxy` Worker in `toom-edge`.

## Consequences

Positive:

- Reddit sees a residential IP, which is the one source network proven to work
  for a logged-in session.
- Failure stays isolated to the Reddit collector: the run continues and the
  digest ships with a banner, as before.
- The cookie still lives only in the digest Key Vault. It is forwarded per
  request, and the proxy stores nothing.
- The 403 and 3xx classification makes the next block diagnosable from one log
  line.

Negative / accepted trade-offs:

- Reddit collection now depends on the homelab: the Mac mini, the home internet
  connection and the Tunnel. Polymarket does not.
- One more service token to rotate (`digest-reddit`), and one more pair of
  secrets in the digest Key Vault.
- The origin code and the Access application live in other repositories, so
  changes to them are not reviewed with this one.
- The session cookie crosses Cloudflare's edge on the way to the Tunnel, so the
  edge still sees it decrypted, as with the Worker.
- Reddit may block the residential IP as well. The terms-of-service posture of
  the collector is unchanged ([RISK-007](../risks/architecture-risks.md)).
- Until cutover, Reddit items are absent from every digest.

## Risks

- [RISK-014](../risks/architecture-risks.md): materialized on 2026-10-22; this
  record is its fallback.
- [RISK-002](../risks/architecture-risks.md): the session cookie still crosses a
  Cloudflare edge.
- [RISK-006](../risks/architecture-risks.md) and
  [RISK-007](../risks/architecture-risks.md): unchanged.

## Related

- Requirements: [P-05](../principles/architecture-principles.md),
  [C-01](../requirements/constraints.md)
- Other ADRs: partly supersedes
  [10. Route blocked collectors through egress proxies on Cloudflare Workers](0010-route-blocked-collectors-through-egress-proxies.md);
  builds on [7. Run digest as Azure jobs](0007-run-digest-as-azure-jobs.md)
- Operations: [Reddit proxy runbook](../../azure-migration.md#credential-rotation-after-cutover)
