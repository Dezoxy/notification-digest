## Transition Plan

From [current-state.md](current-state.md) to [target-state.md](target-state.md).
The one large transition, the move from the homelab VM to Azure, is complete
(row 5). Every open item below is independent unless its "Depends on" column
says otherwise, and none carries an owner-set deadline. This differs from a
system with a multi-quarter build-out: PLAN.md's own roadmap (§11) is nearly
fully executed and the Azure move is done, so what remains is a short list of
individually optional closeout items, not a phased plan.

| # | Step | Outcome | Closes | Depends on | Status |
|---|---|---|---|---|---|
| 1 | Owner decides between an extra Hungarian translate call and folding delta text into the existing translate call | `deltas_hu` ships, or the decision is explicitly recorded as "not doing" | [TD-004](../risks/technical-debt.md) | — | Open, owner-gated (PLAN.md §11.3) |
| 2 | Owner decides whether to flip `VERIFY_DAILY_ENABLED`'s in-repo default to `true` | `config.py`'s default matches what actually runs in production | [TD-005](../risks/technical-debt.md) (partially — `CONTEXT_ENABLED` has the identical gap but is not tracked as a separate pending decision in PLAN.md) | — | Open, owner-gated (PLAN.md §11.4) |
| 3 | Decide whether repeat-story sections adopt the four-part structure rider | Either the richer structure ships, or the entry is dropped — PLAN.md treats both as acceptable outcomes | — | — | Optional (PLAN.md §11.3) |
| 4 | Build site-side status chips rendering the verification pass's corroboration output | A reader sees per-story corroboration status on the site itself, not only in the brief's prose | — | §11.4 shipping (already done) | Not started, unscheduled (PLAN.md §11.4) |
| 5 | Move the digest from the homelab VM to Azure Container Apps jobs, and the summarizer fallback from OpenRouter to the Claude API ([ADR 0007](../decisions/0007-run-digest-as-azure-jobs.md), [ADR 0009](../decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md)) | Production runs as nine scheduled Azure jobs with Blob-leased SQLite state; the VM no longer runs the digest | — | — | **Done 2026-10-08** (hosting; VM timers drained and stopped, Azure schedules enabled). Fallback on the Claude API in place; first real fallback pending (row 7) |
| 6 | Rehearse a restore from the separate backup storage account into a fresh state namespace | The restore procedure is proven, not just written, and recovery objectives can start to be measured | — | Row 5 | Open. A pilot acceptance criterion in the [runbook](../../azure-migration.md#independent-daily-backup-account) |
| 7 | Observe a real Claude API fallback serving a digest | The fallback is seen working on a real failure, not only in the smoke test the released image passed on 2026-10-09 | — | Row 5 | Open ([ADR 0009](../decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md)) |
| 8 | Remove the unwired `digest/openrouter.py` | No dead provider code remains | — | Row 7 | Open; waits for the first real fallback |
| 9 | Merge the homelab repository's pull request removing the digest role | Nothing in the homelab repository can still deploy or back up the digest | — | Row 5 | Open, in the other repository |

### What this plan deliberately does not include

- **No fix for TD-001/TD-002/TD-003.** PLAN.md records these as debt without
  proposing a remediation step; inventing a plan for them here would
  misrepresent what the owner has actually committed to. If the owner schedules
  a fix, it becomes a new row above.
- **No revival of community engagement signals (§11.5).** It was rejected on
  evidence (PLAN.md §9, decision 6), not deferred; reopening it needs new
  evidence, not an implementation step.
- **No further infrastructure change** — a second region, a staging tier, a
  hard spend cap, a return of execution to the VM. None is planned, so none
  appears here ([target-state.md](target-state.md)). Rows 6 to 9 close out the
  move that row 5 records as done; they do not extend it.

### Review

Revisited whenever PLAN.md's own roadmap section is next revised by the owner.
There is no separate quarterly or scheduled review cadence for a single-owner
service — [architecture-risks.md](../risks/architecture-risks.md) and
[technical-debt.md](../risks/technical-debt.md) follow the same rule.
