# Target State

There is no forward-looking target architecture beyond what is already running. PLAN.md's redesign roadmap (§11) is, as of its own latest status lines, almost entirely executed: §11.1 through §11.7 are shipped, and the one entry that didn't ship (§11.5, community engagement signals) was rejected outright rather than left half-built, once the owner observed that the community doesn't react or reply on Telegram at all (PLAN.md §9, decision 6). No second region, no second Proxmox node, no horizontal scaling, and no multi-tenancy appear anywhere in PLAN.md — each is explicitly ruled out as a consequence of [C-01](../requirements/constraints.md) and [C-05](../requirements/constraints.md), not an omission this document is filling in.

What follows is the complete list of items PLAN.md records as open, in whatever form it records them — an owner decision not yet taken, an explicitly optional rider, or work not yet started. None of these should be read as already deployed.

## Open decisions (owner-gated, no target date)

| Item | Status | Reference |
|---|---|---|
| Publish Hungarian delta text (`deltas_hu`) | **Unapproved.** The owner has not chosen between an extra translate call per window (cost) and folding delta text into the existing translate call (routes machine-facing JSON through the translation prompt) | PLAN.md §11.3, [TD-004](../risks/technical-debt.md) |
| Flip `VERIFY_DAILY_ENABLED`'s in-repo default to match production | **Open, owner-gated.** The flag is already on for the real deployment; the in-repo default stays off deliberately until the owner makes this call | PLAN.md §11.4, [TD-005](../risks/technical-debt.md) |
| Adopt a four-part structure (what happened / what changed / why it matters / what to watch) for repeat-story sections | **Optional rider**, explicitly: "skipping it is a fine outcome" per PLAN.md | PLAN.md §11.3 |

## Not started, no blocker beyond scheduling

| Item | Status | Reference |
|---|---|---|
| Site-side status chips rendering the verification pass's per-story corroboration output | Not started; explicitly deferred until after §11.4 shipped, which it now has | PLAN.md §11.4 |

## Technical debt with no proposed remediation

PLAN.md logs these as debt without committing to a fix. The honest target state is that they remain accepted and unscheduled unless the owner revisits them:

- [TD-001](../risks/technical-debt.md) — `arc_context` has no prune.
- [TD-002](../risks/technical-debt.md) — the D1 rebuild-from-VM path has never been exercised end to end.
- [TD-003](../risks/technical-debt.md) — end-to-end run latency has never been measured.

## What is explicitly not the target

| Not doing | Why |
|---|---|
| A second Proxmox node or region | No requirement exists; [C-01](../requirements/constraints.md)/[C-05](../requirements/constraints.md) make this a single-owner, single-node system by design |
| A staging environment | [environments.md](../deployment/environments.md) — there is production and a local throwaway run, with no review gate planned between them |
| Reviving community engagement signals (§11.5) as designed | Rejected on evidence, not deferred — see PLAN.md §9, decision 6. A revival needs new evidence that Telegram engagement actually happens, not an implementation retry |
| A hard spend cap on summarization | Recorded as an accepted, unmeasured exposure ([RISK-004](../risks/architecture-risks.md), [QA-07](../requirements/quality-attributes.md)), not as planned work |

A target-state document with no "not doing" section invites scope creep from every direction — there isn't one missing here because PLAN.md itself already draws these lines explicitly.
