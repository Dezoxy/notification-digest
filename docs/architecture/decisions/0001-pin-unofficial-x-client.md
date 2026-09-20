# 1. Pin the unofficial X client to an exact version and audit every bump

Date: 2026-09-20

## Status

Accepted

## Context

X (Twitter) has no sanctioned notifications API at a price this single-owner
service can justify ([C-03](../requirements/constraints.md)), so collection
goes through `twifork`, a maintained fork of the dead upstream `twikit`
(pinned base 2.3.3, per `pyproject.toml`'s comment). Upstream `twikit` broke
in March 2026 when X changed its webpack bundle, breaking the library's
transaction-ID signing before authentication could even be attempted
("Couldn't get KEY_BYTE indices"); `twifork` fixes exactly that and still
imports as `twikit`, so no import line in this codebase changes
(`digest/collectors/x.py`'s module docstring, per `pyproject.toml`).

`twifork` authenticates with the owner's own X session cookie
([C-02](../requirements/constraints.md)) — a personal, account-level secret,
not a service credential. A single maintainer publishes it. Both facts push
in the same direction: this dependency needs slower, more deliberate
handling than the rest of the dependency tree, which Renovate updates
automatically on its own schedule.

## Decision drivers

- [C-03](../requirements/constraints.md): no sanctioned X API exists; the
  collector must stay behind a flag and degrade gracefully rather than
  retry-loop against an unofficial surface.
- [A-02](../requirements/assumptions.md): the unofficial client keeps
  working only as long as X's frontend doesn't change again without notice.
- Every other dependency in `pyproject.toml` uses `>=`, deliberately
  contrasting with this one pin — see `## Decision` below.

## Considered options

1. Track `twifork` with the same floating `>=` range as every other
   dependency, letting Renovate open ordinary PRs.
2. Exclude `twifork` from Renovate entirely (no automated visibility into
   new releases at all).
3. Pin `twifork` to an exact version and gate its Renovate updates behind
   manual approval with a mandatory diff audit.

## Decision

Pin `twifork` to an exact version (`twifork==2.4.0` at the time of writing,
per `pyproject.toml`) instead of the `>=` range used everywhere else in the
project. Renovate still tracks it (`renovate.json`'s `packageRules` entry
matching `twifork`), but `dependencyDashboardApproval: true` keeps it from
opening a PR on its own — Renovate reports the update on the Dependency
Dashboard and waits. The resulting PR carries a `supply-chain-audit` label.
Ticking the dashboard box is the deliberate act that starts a hand audit of
the diff against the previous pin: no new network hosts, no new
`eval`/`exec`/`base64`/`subprocess`/`pickle`. Per `renovate.json`'s own
description, excluding the package entirely (option 2) was rejected because
it makes updates invisible rather than safe — a fork going unmaintained, or
shipping a fix the collector needs, would never surface at all.

## Consequences

Positive:

- A bump can never arrive looking routine; ticking the dashboard box is
  itself the record that the audit was intended to happen.
- The fork's continued existence and activity stay visible (via the
  dashboard) even though nothing merges automatically.
- No import-line churn anywhere in the codebase when the fork is bumped,
  since it still imports as `twikit`.

Negative / accepted trade-offs:

- Every bump is manual, hand-audited effort by the owner — there is no
  automation shortcut for it, by design.
- The service's X collection quality is tied to a single-maintainer fork's
  continued availability; if that maintainer stops publishing fixes for a
  future X-side breakage, this codebase has no fallback client.
- Pinning exactly (rather than a range) means the codebase does not
  automatically receive an unaudited security fix from the fork either —
  the audit gate applies uniformly, with no fast path for a fix that turns
  out to be urgent.

## Risks

- Not recorded: `risks/architecture-risks.md` on this branch currently holds
  generic template content unrelated to this repository (it describes a
  payments/rides system), so no digest-specific risk ID could be confirmed to
  cover an X-account-ban or fork-abandonment scenario at the time of writing.
  That register is being written in parallel with this ADR; link it here once it
  carries a matching entry.

## Related

- Requirements: [C-02](../requirements/constraints.md),
  [C-03](../requirements/constraints.md), [A-02](../requirements/assumptions.md)
- Architecture views: not recorded
- Other ADRs: none
