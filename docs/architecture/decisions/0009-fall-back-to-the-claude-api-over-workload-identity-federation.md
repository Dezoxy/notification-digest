# 9. Fall back to the Claude API over workload identity federation

Date: 2026-10-09

## Status

Accepted

Supersedes the OpenRouter provider choice in
[2. Summarize through a fallback chain](0002-summarizer-fallback-chain.md): its
option 3 and the provider, model lists and `OPENROUTER_API_KEY` switch in its
Decision. The chain design itself (a primary, ordered fallback legs, one shared
budget, per-leg validation, per-digest provenance) stays accepted there.

The code is implemented, tested and deployed. On 2026-10-09 the runbook's smoke
test passed on the released image with the job's own settings: one execution
went through the whole chain (managed identity, Entra app token, Anthropic
exchange, one Messages call). No real fallback has served a digest yet. See
[the runbook](../../azure-migration.md#claude-api-fallback-workload-identity-federation).

## Context

The fallback leg of ADR 2 sends the same prompt to OpenRouter models, which
needs a static `OPENROUTER_API_KEY` in the digest Key Vault, a second vendor
that receives the window's scraped text, and models whose editorial behaviour
differs from the primary. The Azure target ([ADR 7](0007-run-digest-as-azure-jobs.md))
already gives every job a user-assigned managed identity, and Anthropic's
Workload Identity Federation accepts an Entra-issued token in exchange for a
short-lived API access token, so the same provider can serve the fallback with
no stored credential.

The primary path stays `claude -p` on the owner's flat-fee subscription with
`CLAUDE_CODE_OAUTH_TOKEN`. Moving the fallback to the Claude API must not move
that primary path onto metered billing.

## Decision drivers

- [C-08](../requirements/constraints.md) and [QA-04](../requirements/quality-attributes.md):
  a refusal, usage-limit hit or timeout must not lose a window.
- [QA-07](../requirements/quality-attributes.md) and
  [RISK-004](../risks/architecture-risks.md): fallback cost stays visible and
  bounded.
- Owner preference: no static credential for a path that runs rarely, and one
  fewer third-party processor of collected text.
- Keep the subscription as the only billing path of the primary call.

## Considered options

1. Keep OpenRouter and its API key (ADR 2 as written).
2. Call the Claude API with a static `ANTHROPIC_API_KEY` stored in Key Vault.
3. Call the Claude API with an access token obtained by federating the job's
   managed identity (no stored credential).

Rationale for rejecting option 1 beyond the points above: not recorded.

## Decision

Option 3. `digest/anthropic_api.py`'s `run_anthropic` trades the job's managed
identity token with Entra for a short-lived token of a dedicated app
registration (`api://<APP_ID>`), exchanges that at
`https://api.anthropic.com/v1/oauth/token` (RFC 7523 jwt-bearer) for a
short-lived access token, and calls `/v1/messages`.

The Entra step has two hops, found necessary while setting it up. A token the
managed identity requests for the audience directly lives 86,700 s between
`iat` and `exp`, and Anthropic rejects an assertion longer than the issuer's
maximum JWT lifetime (`jwt_lifetime_too_long`). The Claude Console accepts at
most 86,400 s there, and the Admin API that accepts more is unavailable to
individual organizations. So the managed identity's token is presented to Entra
as a client assertion, through a federated identity credential on the app
registration, and Entra returns an app token that lives 3,900 s. Anthropic sees
the app's service principal, which the federation rule matches; the managed
identity is trusted only by Entra, and only as that one subject.

It makes one fresh exchange per fallback call, uses plain `urllib` and no SDK,
fixes `output_config.effort` at `high` with `max_tokens` 32000, and counts a
reply that ends in `max_tokens` or `refusal` as a failed leg, so
`run_with_fallbacks` moves on to the next one.

Configuration is the non-secret identifiers `ANTHROPIC_FEDERATION_RULE_ID`,
`ANTHROPIC_ORGANIZATION_ID`, `ANTHROPIC_SERVICE_ACCOUNT_ID`,
`ANTHROPIC_FEDERATION_AUDIENCE` and `ANTHROPIC_FEDERATION_TENANT_ID`, required
together (a partial set is a startup
`ConfigError`), plus an optional `ANTHROPIC_WORKSPACE_ID`. With none set there
is no fallback chain and the Claude CLI behaves exactly as before. The default
chains are now `claude-opus-5-5,claude-sonnet-5-5` (editorial tier) and
`claude-sonnet-5-5,claude-haiku-5-5` (light tier). The shared
`FALLBACK_TIMEOUT_SECONDS`, the per-leg validation and the provenance record
from ADR 2 are unchanged.

Two controls keep the primary path on the subscription and the federation
inside the runner process. `claude_subprocess_env` withholds
`IDENTITY_ENDPOINT`, `IDENTITY_HEADER` and every `ANTHROPIC_*` variable from
the `claude -p` subprocess: the first two would let content-facing code mint
tokens for the runner identity, and an `ANTHROPIC_API_KEY` or
`ANTHROPIC_AUTH_TOKEN` reaching the CLI would take precedence over
`CLAUDE_CODE_OAUTH_TOKEN` and silently move every digest to metered API
billing. `AnthropicApiError` never carries a response body, token or URL.

Cost is capped outside the code: the federation's service account belongs to a
dedicated Claude Console workspace with a monthly spend limit, so a run where
every call falls through is bounded by the limit rather than by this code.

## Consequences

Positive:

- No Anthropic credential exists to store, rotate or leak; the identifiers are
  not secrets. `OPENROUTER_API_KEY` leaves `secret_names` and the vault.
- The fallback processor is the same vendor as the primary, so one fewer party
  receives collected text.
- A fallback to the first editorial model repeats the primary's model on
  prepaid API credits, which is the right answer to a subscription usage limit;
  the second model gives a classifier refusal somewhere different to go.

Negative / accepted trade-offs:

- A fallback is billed per token at API prices. The OpenRouter estimate of
  [ADR 2](0002-summarizer-fallback-chain.md) does not carry over and no new
  figure is claimed: measure it from the first real fallback's logged
  `claude api usage:` line ([RISK-004](../risks/architecture-risks.md)).
- A refusal specific to Anthropic's classifier is no longer escaped by a
  different vendor's model; the chain's diversity is now across Claude models.
- The chain depends on Azure and Anthropic federation both being healthy and
  correctly configured. A bad rule, expired issuer setting or deleted audience
  fails only at fallback time, never at startup, so a smoke test is a required
  operational step.
- The federation rule matches the runner identity's object ID; that identity
  now also gates API spend, not only Blob state.

## Risks

- [RISK-004](../risks/architecture-risks.md): fallback spend is now bounded by
  the workspace limit, not unbounded, but still unmeasured.
- [RISK-005](../risks/architecture-risks.md): the residual refusal case where
  every Claude model in the chain also refuses.

## Related

- Requirements: [C-08](../requirements/constraints.md),
  [QA-04](../requirements/quality-attributes.md),
  [QA-07](../requirements/quality-attributes.md)
- Architecture views: not updated here. The Structurizr model still draws an
  OpenRouter external system and is changed separately.
- Other ADRs: supersedes the provider choice in
  [2. Summarize through a fallback chain](0002-summarizer-fallback-chain.md);
  builds on [7. Run digest as Azure jobs](0007-run-digest-as-azure-jobs.md)
- Operations: [Claude API fallback runbook](../../azure-migration.md#claude-api-fallback-workload-identity-federation)
