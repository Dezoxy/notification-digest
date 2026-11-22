# 13. Serve the digest to ChatGPT through an MCP connector

Date: 2026-11-22

## Status

Accepted

Decided on 2026-11-22. The connector is configured and answered real tool calls
that morning. The scheduled briefing it was built for is planned, not live.

## Context

The goal is a ChatGPT briefing that reads the evening daily edition and gives
the owner an analysis of it. Today's edition is published at about 20:30
Europe/Budapest year-round (the Azure cron `30 18,19 * * *` UTC has two
entries, one of which matches local time). The planned briefing runs at 21:30
with a 22:30 recovery run. It needs a machine-readable source, not the HTML
reader pages.

What was tried, all observed:

- **2026-11-21,
  [PR #309](https://github.com/Dezoxy/notification-digest/pull/309).** The News
  Site Worker gained a read-only JSON API,
  `GET /api/v1/<API_TOKEN>/digests/daily/{latest|YYYY-MM-DD}`, a `?limit=N`
  list and `/health`, behind a separate secret `API_TOKEN` in the path. Its
  intended client was ChatGPT's web browsing.
- **ChatGPT refuses all three News Site hostnames client-side.** The browsing
  tool answers `DisabledError` ("not accessible via this tool"). In three tests
  on 2026-11-21 (12:44, 13:03 and 13:08 UTC, one in a fresh chat) the Cloudflare
  Worker and firewall logs showed no ChatGPT request, while marked control
  requests made at the same time appeared within a minute. The refusal happens
  before any request leaves ChatGPT. Its cause is inside ChatGPT and unknown.
- **[PR #310](https://github.com/Dezoxy/notification-digest/pull/310)** made
  `robots.txt` deterministic, with named groups: `ChatGPT-User` allowed only on
  `/api/v1/`, `OAI-SearchBot` and `*` disallowed. It did not change the refusal.

The JSON API is therefore deployed and working, but ChatGPT cannot reach it.

## Decision drivers

- The briefing must read the digest without the owner pasting it in.
- No new service or database for a single-owner, read-only use
  ([C-01](../requirements/constraints.md)).
- Digest content is derived from third-party messages, so every new recipient
  is a data decision ([data classification](../security/data-classification.md)).
- The existing capability-link model (a secret in the path, a uniform 404
  otherwise) is the accepted access control of the News Site
  ([trust boundaries](../security/trust-boundaries.md)).

## Considered options

1. **The JSON API through ChatGPT's browsing tool**
   ([PR #309](https://github.com/Dezoxy/notification-digest/pull/309)).
   Rejected: the tool refuses the hostnames before sending a request.
2. **Fix `robots.txt`**
   ([PR #310](https://github.com/Dezoxy/notification-digest/pull/310)).
   Rejected: done, and it had no effect.
3. **A custom GPT Action over the JSON API.** Not pursued: it is unclear whether
   a scheduled task can call an Action, and it would add an OpenAPI description
   to keep in step with the Worker.
4. **An MCP connector on the existing Worker.** Chosen.
5. **Manual paste, from the Telegram TL;DR ping.** Kept as the fallback: it
   needs no change, but it is the thing the briefing was meant to replace.

## Decision

Option 4. The News Site Worker serves a Model Context Protocol endpoint,
`POST /mcp/<MCP_TOKEN>`
([PR #311](https://github.com/Dezoxy/notification-digest/pull/311) and
[PR #312](https://github.com/Dezoxy/notification-digest/pull/312)).

- **Shape.** Streamable HTTP, stateless, hand-written JSON-RPC, JSON responses
  only: no sessions, no SSE, no writes. Four read-only tools, `health`,
  `get_daily_latest`, `get_daily_by_date` and `list_daily_editions`, built from
  the same functions as `/api/v1` so the two cannot drift. The list tool returns
  summaries without body or sources, so a week costs about 1.6 KB per edition
  instead of 28 KB; `get_daily_by_date` fetches one full edition.
- **Authentication.** A path token with ChatGPT's "No authentication" option,
  matching the News Site's capability-link model. OAuth was rejected: it needs a
  KV store, a login surface and a consent flow, all for a one-user, read-only
  endpoint. The upgrade path is header authentication if ChatGPT offers it for
  custom connectors; until then, rotation is the answer to a leak.
- **A separate secret.** `MCP_TOKEN` is independent of `SITE_TOKEN`,
  `INGEST_KEY` and `API_TOKEN`, so a leak of any one grants nothing over the
  others. The Worker now holds four secrets. A wrong token is the same plain 404
  as any unknown path, for every method. Logs carry method and status only.
- **The workers.dev hostname, not the custom domains.** The connector URL uses
  `news-site.toomhorvath.workers.dev`. workers.dev is outside the
  `tomhorvath.me` Cloudflare zone, whose Bot Fight Mode and AI-bot blocking
  challenge the cloud-provider addresses ChatGPT calls from, and whose AI
  Labyrinth firewall log records full request paths, tokens included. That log
  has already recorded `API_TOKEN` once, in a path on the custom domain.
- **Observed.** On 2026-11-22 between 06:52 and 06:58 UTC, OpenAI's client
  (`openai-mcp/1.0.0`), calling from Microsoft Azure addresses, completed
  handshakes and tool calls (200 and 202).

## Consequences

Positive:

- ChatGPT can read the edition with no change to the runner, the state database
  or the delivery path. No new service and no new database.
- The endpoint is read-only by construction: no tool writes, and the Worker has
  no path into the Azure subscription.

Negative / accepted trade-offs:

- **OpenAI is a new external recipient** of daily-edition content (summaries of
  the owner's Telegram groups, X notifications and feeds, with source links),
  alongside Anthropic. It receives it on request, under the owner's consumer
  account. Where it is processed is not controlled by this repository.
- **The digest text is untrusted third-party content entering ChatGPT's
  context.** A message written by someone else can carry instructions. The
  briefing task should run with this one read-only connector and no others.
- **OpenAI stores the connector URL, so it holds `MCP_TOKEN`.** Cutting that
  access off means rotating the token and updating the connector.
- **`API_TOKEN` has no consumer now.** The JSON API is still deployed. It is a
  candidate for deletion (`wrangler secret delete API_TOKEN`), which also
  closes the route. Its rotation is pending in any case, because the
  firewall log recorded it.
- **Scheduled-task support is unverified.** Interactive tool calls work. Whether
  a ChatGPT scheduled task can call a developer-mode connector has not been
  tested, and the 21:30 and 22:30 runs are not live until it is.

## Risks

- [RISK-015](../risks/architecture-risks.md): untrusted digest content reaches
  ChatGPT, and the token sits in a URL that a third party stores.

## Related

- Requirements: [C-01](../requirements/constraints.md)
- Architecture views: `Context`, `Containers`, `Security`, `Landscape`
- Other ADRs: builds on
  [4. Deliver through independent channels, and keep email implemented](0004-multi-channel-delivery.md)
  (ChatGPT is a reader, not a channel)
- Source: `workers/news-site/README.md`, "JSON API" and "MCP endpoint";
  `workers/news-site/src/mcp.js`
