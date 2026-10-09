## Target State

There is no forward-looking target architecture beyond what is already running.
The one architectural move that was a target, running the digest as scheduled
Azure Container Apps jobs instead of on the homelab VM, is done: it took effect
on 2026-10-08 ([ADR 0007](../decisions/0007-run-digest-as-azure-jobs.md)) and is
described in [current-state.md](current-state.md), not here. The same goes for
the summarizer's fallback moving from OpenRouter to the Claude API
([ADR 0009](../decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md)).
PLAN.md's redesign roadmap (§11) is, as of its own latest status lines, almost
entirely executed: §11.1 through §11.7 are shipped, and the one entry that
didn't ship (§11.5, community engagement signals) was rejected outright rather
than left half-built, once the owner observed that the community doesn't react
or reply on Telegram at all (PLAN.md §9, decision 6). No second region, no
horizontal scaling, and no multi-tenancy appear anywhere in PLAN.md — each is
explicitly ruled out as a consequence of
[C-01](../requirements/constraints.md) and
[C-05](../requirements/constraints.md), not an omission this document is filling
in.

What follows is the complete list of items PLAN.md records as open, in whatever
form it records them — an owner decision not yet taken, an explicitly optional
rider, or work not yet started. None of these should be read as already
deployed.

### Open decisions (owner-gated, no target date)

| Item | Status | Reference |
|---|---|---|
| Publish Hungarian delta text (`deltas_hu`) | **Unapproved.** The owner has not chosen between an extra translate call per window (cost) and folding delta text into the existing translate call (routes machine-facing JSON through the translation prompt) | PLAN.md §11.3, [TD-004](../risks/technical-debt.md) |
| Flip `VERIFY_DAILY_ENABLED`'s in-repo default to match production | **Open, owner-gated.** The flag is already on for the real deployment; the in-repo default stays off deliberately until the owner makes this call | PLAN.md §11.4, [TD-005](../risks/technical-debt.md) |
| Adopt a four-part structure (what happened / what changed / why it matters / what to watch) for repeat-story sections | **Optional rider**, explicitly: "skipping it is a fine outcome" per PLAN.md | PLAN.md §11.3 |

### Open closeout of the Azure move

The move itself is complete; these are the checks and cleanups that its own
records leave open. None adds architecture, and none has a date.

| Item | Status | Reference |
|---|---|---|
| Restore from the separate backup storage account, rehearsed into a fresh namespace | Not done. The runbook makes it a pilot acceptance criterion; until then recovery objectives are targets, not measurements | [backup-strategy.md](../reliability/backup-strategy.md), [migration runbook](../../azure-migration.md#independent-daily-backup-account) |
| A real Claude API fallback serves a digest | Not yet observed. The released image passed the runbook's smoke test on 2026-10-09; a fallback only runs when `claude -p` fails | [ADR 0009](../decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md) |
| Remove the unwired `digest/openrouter.py` | Waits for the first real fallback to have worked | [ADR 0009](../decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md) |
| Remove the digest role from the homelab repository | An open pull request there, not merged at the time of writing | [current-state.md](current-state.md) |

### Not started, no blocker beyond scheduling

| Item | Status | Reference |
|---|---|---|
| Site-side status chips rendering the verification pass's per-story corroboration output | Not started; explicitly deferred until after §11.4 shipped, which it now has | PLAN.md §11.4 |

### Technical debt with no proposed remediation

PLAN.md logs these as debt without committing to a fix. The honest target state
is that they remain accepted and unscheduled unless the owner revisits them:

- [TD-001](../risks/technical-debt.md) — `arc_context` has no prune.
- [TD-002](../risks/technical-debt.md) — the D1 rebuild-from-state path has never been exercised end to end.
- [TD-003](../risks/technical-debt.md) — end-to-end run latency has never been measured.

### What is explicitly not the target

| Not doing | Why |
|---|---|
| A second region or subscription, or immutable cross-region backup retention | No requirement exists; [C-01](../requirements/constraints.md)/[C-05](../requirements/constraints.md) make this a single-owner, single-region system by design. The single-region failure domain is accepted, not scheduled work ([disaster-recovery.md](../reliability/disaster-recovery.md)) |
| Returning execution to the homelab VM | Outside the approved migration; recovery stays within Azure ([ADR 0007](../decisions/0007-run-digest-as-azure-jobs.md)) |
| A staging environment | [environments.md](../deployment/environments.md) — there is production and a local throwaway run, with no review gate planned between them |
| Reviving community engagement signals (§11.5) as designed | Rejected on evidence, not deferred — see PLAN.md §9, decision 6. A revival needs new evidence that Telegram engagement actually happens, not an implementation retry |
| A hard spend cap on summarization or on Azure | Recorded as an accepted, unmeasured exposure ([RISK-004](../risks/architecture-risks.md), [QA-07](../requirements/quality-attributes.md)); a monthly budget notification warns the owner but caps nothing. Not planned work |

A target-state document with no "not doing" section invites scope creep from
every direction — there isn't one missing here because PLAN.md itself already
draws these lines explicitly.
