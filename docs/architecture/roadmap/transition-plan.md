## Transition Plan

From [current-state.md](current-state.md) to [target-state.md](target-state.md).
Every item below is independent — none blocks another, and none carries an
owner-set deadline. This differs from a system with a multi-quarter build-out:
PLAN.md's own roadmap (§11) is nearly fully executed, so what remains is a short
list of individually optional closeout items, not a phased plan.

| # | Step | Outcome | Closes | Depends on | Status |
|---|---|---|---|---|---|
| 1 | Owner decides between an extra Hungarian translate call and folding delta text into the existing translate call | `deltas_hu` ships, or the decision is explicitly recorded as "not doing" | [TD-004](../risks/technical-debt.md) | — | Open, owner-gated (PLAN.md §11.3) |
| 2 | Owner decides whether to flip `VERIFY_DAILY_ENABLED`'s in-repo default to `true` | `config.py`'s default matches what actually runs in production | [TD-005](../risks/technical-debt.md) (partially — `CONTEXT_ENABLED` has the identical gap but is not tracked as a separate pending decision in PLAN.md) | — | Open, owner-gated (PLAN.md §11.4) |
| 3 | Decide whether repeat-story sections adopt the four-part structure rider | Either the richer structure ships, or the entry is dropped — PLAN.md treats both as acceptable outcomes | — | — | Optional (PLAN.md §11.3) |
| 4 | Build site-side status chips rendering the verification pass's corroboration output | A reader sees per-story corroboration status on the site itself, not only in the brief's prose | — | §11.4 shipping (already done) | Not started, unscheduled (PLAN.md §11.4) |

### What this plan deliberately does not include

- **No fix for TD-001/TD-002/TD-003.** PLAN.md records these as debt without
  proposing a remediation step; inventing a plan for them here would
  misrepresent what the owner has actually committed to. If the owner schedules
  a fix, it becomes a new row above.
- **No revival of community engagement signals (§11.5).** It was rejected on
  evidence (PLAN.md §9, decision 6), not deferred; reopening it needs new
  evidence, not an implementation step.
- **No infrastructure change** — a second node, a staging tier, a hard spend
  cap. None is planned in PLAN.md, so none appears here
  ([target-state.md](target-state.md)).

### Review

Revisited whenever PLAN.md's own roadmap section is next revised by the owner.
There is no separate quarterly or scheduled review cadence for a single-owner
service — [architecture-risks.md](../risks/architecture-risks.md) and
[technical-debt.md](../risks/technical-debt.md) follow the same rule.
