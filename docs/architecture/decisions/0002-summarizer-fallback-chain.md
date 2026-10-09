# 2. Summarize through a fallback chain rather than a single model

Date: 2026-09-20

## Status

Accepted

The OpenRouter provider choice (option 3 and the provider, model lists and
`OPENROUTER_API_KEY` switch in the Decision) is superseded by
[9. Fall back to the Claude API over workload identity federation](0009-fall-back-to-the-claude-api-over-workload-identity-federation.md).
The chain design below stays accepted; the text is kept as written because it
records what was decided on 2026-09-20.

## Context

Every summarization call site (`summarize()` and its six siblings — daily,
weekly, patreon, positions, translate, arc-context — per
`digest/summarize.py`'s `run_with_fallbacks` docstring) primarily invokes the
Claude CLI headless as a subprocess: `claude -p --model <model> --tools ""
--effort <effort>`, authenticated via the owner's persisted Max-subscription
login rather than a metered API key (`digest/summarize.py`'s module
docstring). That path fails in ways unrelated to content quality: the owner's
subscription usage limit can be hit mid-run, the target model's real-time
safety classifier can flag an ordinary security-heavy digest (a live,
reproduced incident — `SafeguardsRefusalError`, `digest/summarize.py`), or the
CLI can time out or exit non-zero transiently. None of those recover by
retrying the same model against the same prompt — see
[C-08](../requirements/constraints.md).

## Decision drivers

- [C-08](../requirements/constraints.md): summarization depends on a
  third-party model that can refuse; a refusal must not fail the run.
- [QA-04](../requirements/quality-attributes.md): summarization availability
  — a model refusal or outage must not lose a window.
- [QA-07](../requirements/quality-attributes.md): cost stays bounded per run,
  but no hard cap is enforced in code; a fallback chain can only add cost —
  tracked as [RISK-004](../risks/architecture-risks.md).

## Considered options

1. Single model, no fallback: a refusal or outage loses that run's items
   until the next scheduled run retries (unsummarized items are not
   deleted, per `state.py`, but delivery is delayed).
2. Retry the same model on failure.
3. Fall back to a chain of different models (OpenRouter) when the primary
   fails, sharing one wall-clock budget across the whole chain.

## Decision

Primary: `claude -p` (the Claude CLI, headless), via `run_claude` in
`digest/summarize.py`. On failure, `run_with_fallbacks` retries the same
prompt against a configured chain of OpenRouter models
(`digest/openrouter.py`'s `run_openrouter`), in order, via `FallbackLeg`
entries. Two tiers exist: an "editorial" tier
(`Config.fallback_models`, default `("openai/gpt-5.6-sol", "z-ai/glm-5.3")`)
for window/daily/weekly/positions/patreon summarization, and a cheaper
"light" tier (`Config.fallback_light_models`, default
`("openai/gpt-5.6-terra", "deepseek/deepseek-v4-flash")`) for
translation/context-primer generation — deliberately two different
frontier-tier providers in the editorial default, so a provider-wide outage
affecting one still leaves the other reachable.

The whole chain shares one budget (`Config.fallback_timeout_seconds`,
default 180s), timed from the moment the primary fails, not from when the
call started — every OpenRouter leg reasons at a fixed `"high"` effort
(`digest/openrouter.py`'s `REASONING_EFFORT`), and a leg with less than
`_MIN_FALLBACK_SECONDS` (15s) of budget left is skipped rather than
attempted. Option 2 (same-model retry) was rejected because
`SafeguardsRefusalError` is deterministic for a given (content, model) pair —
retrying the identical model cannot succeed (`digest/summarize.py`'s
docstring). With `OPENROUTER_API_KEY` unset, `_fallback_legs` returns an
empty tuple and `run_with_fallbacks` re-raises the primary's own exception
unchanged, so an unconfigured deployment behaves byte-for-byte as it did
before this feature existed.

Each digest records which leg actually produced it — `ModelRun(model,
effort, fallback)`, persisted as JSON in `digests.provenance`
(`digest/state.py`, schema version 7) — so the owner (or the site) can tell
"the primary served this" from "the chain reached a fallback model."

## Consequences

Positive:

- A safety-classifier refusal, a usage-limit hit, or a transient CLI failure
  degrades to a different model instead of losing that run's window.
- Model provenance is recorded per digest, making a degraded run visible
  rather than silent.
- Unconfigured (no `OPENROUTER_API_KEY`) behavior is unchanged from before
  the feature existed — verified by construction (empty-fallbacks re-raise).

Negative / accepted trade-offs:

- Cost is unbounded in code: every fallback leg reasons at `"high"`, and
  there is no per-run or per-day spend cap — accepted per
  [QA-07](../requirements/quality-attributes.md) and tracked as
  [RISK-004](../risks/architecture-risks.md).
- A fallback leg's output has no structural refusal signal the way
  `run_claude` does (`SafeguardsRefusalError`); an OpenRouter model
  declining content returns ordinary prose with HTTP 200, so `validate()`
  must run inside the fallback loop rather than after it, which is a subtler
  contract than a plain try/fallback would need.
- Effort is not configurable per fallback leg (fixed at `"high"`) even
  though the primary's own effort (`CLAUDE_EFFORT`) is a tunable knob — a
  fallback call may cost more than the equivalent primary call would have.

## Risks

- [RISK-004](../risks/architecture-risks.md): cost exposure of the fallback
  chain — this linkage is confirmed in this repository's own
  `requirements/quality-attributes.md` (QA-07 cites RISK-004 directly for
  exactly this exposure).

## Related

- Requirements: [C-08](../requirements/constraints.md),
  [QA-04](../requirements/quality-attributes.md),
  [QA-07](../requirements/quality-attributes.md),
  [A-05](../requirements/assumptions.md)
- Architecture views: not recorded
- Other ADRs: none
