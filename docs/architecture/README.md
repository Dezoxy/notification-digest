# Architecture

The architecture of notification-digest: a single-owner service that collects
the owner's own notifications on a schedule, summarizes each window with a
language model, and delivers a digest with deep links.

Start with [the architecture overview](overview/01-notification-digest.md). It is
also the Documentation tab of the Structurizr workspace and the opening pages of
the exported PDF.

## Reading paths

The architecture description lives in [`overview/`](overview/). Structurizr
imports that folder as its Documentation tab and the PDF is built from it, so
the two render the same document — and the PDF is complete on its own: no
section needs a link followed to be understood.

It opens with routes for different readers:

| Section | For |
|---|---|
| [Notification Digest](overview/01-notification-digest.md) | What the system is, its context, building blocks and one run |
| [Scope](overview/02-scope.md) | What is covered, what is not, what was deliberately not built |
| [For stakeholders](overview/03-for-stakeholders.md) | What it produces, what it costs, what could go wrong. No protocols |
| [For the CTO](overview/04-for-the-cto.md) | Exposure, data, recovery, accepted risk, cost |
| [For engineers](overview/05-for-engineers.md) | The design, the runtime, the decisions, the merge gate |
| [For operators](overview/06-for-operators.md) | How a change ships, what fails together, backup and recovery |
| [Glossary](overview/07-glossary.md) | Terms used with a specific meaning here |

Every register below then follows **in the same document**. Each is authored
once, in its own folder where its IDs are owned, and symlinked into `overview/`
as `NN-name.md` so it appears in the tab and the PDF in a fixed order. That is
what `check_overview_complete` enforces: a register nobody symlinked is
invisible in the artifact people are handed, while still looking present here.

`pdf-sections.txt` stays empty. It exists only to append pages the tab does not
have, and nothing should.


## View register

Every view answers one question for one audience. Budgets come from the
`architecture-views` skill: 7 elements/8 arrows for an audience overview, 10/12
for a technical structural view, 7 participants/8 interactions for a runtime
scenario, 12 boxes/8 arrows for deployment. A view is split at its budget rather
than enlarged. Omissions are registered so a focused view is not mistaken for
the whole system.

| Key | Audience | Question | Selection | Omits (and where that lives) | Update trigger | Visual check |
|---|---|---|---|---|---|---|
| Landscape | Everyone | Which systems does the digest sit between? | Everything except delivery tooling | Delivery tooling, which is the `Delivery` view's question | A source or channel is added or removed | Verified 2026-10-09 |
| Context | Everyone | Who reads the digest, what writes it, and where does it come out? | Owner, the system, Claude, Telegram, SMTP | The six non-Telegram sources — they are `Sources`; including all seven blew the overview budget. Entra, which only serves the fallback call and belongs to `Security` | Delivery channels or the model provider change | Verified 2026-10-09 |
| Sources | Engineers | Which accounts and feeds does a run collect from? | The system plus all seven collection sources | Delivery and state, which `Context` and `Containers` cover | A collector is added or retired | Rendered, not reviewed |
| Containers | Engineers, architects | What are the building blocks, and which side of the trust boundary is each on? | All four containers, both trust-boundary groups and Telegram | Non-Telegram sources and SMTP (disabled in production); the model provider and Entra, which `Context`, `Security` and `DigestRun` cover; delivery tooling | A container or interface changes | Verified 2026-10-09 |
| Security | Owner, reviewer | What is reachable from the internet, where do secrets and identity come from, and what stays private? | The four containers plus Key Vault, Entra and the deploy path | Collection sources; their credentials are covered in [trust boundaries](security/trust-boundaries.md) | Exposure, secret flow, identity or network policy changes | Verified 2026-10-09 |
| DigestRun | Engineers | What happens during one scheduled digest run? | Runner, state, Telegram, the summarizer, the site | Fallback path (the Claude API call and its Entra token) and the non-digest lanes; see [ADR 9](decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md) | Run ordering, failure handling or delivery changes | Reviewed 2026-10-09: known defect, see below |
| Delivery | Operators | How does a code change reach the running jobs? | Owner, Actions, registry, Renovate, Key Vault, the runner | Everything that happens during a run — none of this does | The release or deploy procedure changes | Verified 2026-10-09 |
| ProductionDeployment | Operators, reviewer | Where does the digest run, and what fails together? | The Azure region, its jobs environment, both storage accounts, and the Cloudflare edge | Key Vault, Log Analytics and alerting; see [deployment architecture](deployment/deployment-architecture.md) | Hosting, placement or failure domains change | Verified 2026-10-09, minor flaw, see below |

**Visual verification status:** verification is version-specific, so it names
the version. Against **structurizr/structurizr:2026.09.19**, the pin in the
Makefile and the version the homelab Structurizr server runs, the model parses
and inspects clean (`make check`: 0 errors, 0 warnings).

The model moved from the homelab VM to Azure, and from OpenRouter to the Claude
API, on 2026-10-09. Seven views changed and were each rendered from the local
viewer (`make view`) and read at full size the same day. `Sources` did not
change. What that review found:

- **Landscape**, **Context**, **Containers**, **Security** and **Delivery**
  read cleanly. `Context` dropped to five elements once the second provider box
  went. `Delivery` has one arrow crossing (Actions to the runner over Renovate
  to the registry), which stays traceable.
- **ProductionDeployment** was switched from left-to-right to top-to-bottom. In
  the left-to-right layout the Cloudflare frame overlapped a corner of the
  Azure frame and hid its own label, which blurred the one question the view
  answers. One flaw remains: the backup arrow leaves the runner through the
  words "Container Apps environment". Wider spacing did not remove it and
  shrank the text, so the compact layout was kept.
- **DigestRun** has a defect that predates this change and that the earlier
  review missed: the two arrows to Telegram (steps 2 and 6) run through the
  Claude box, because automatic layout places both systems in one rank.
  Neither layout direction fixes it, and coordinates are not placed by hand
  here. The step numbers and labels stay readable; the box overlap is the
  known limitation. Splitting collection and delivery into two scenarios would
  remove it and is the option if this view is shown to an audience.

Two earlier fixes still hold. `Containers` does not carry the model provider:
its long arrows dominated the layout, and that question belongs to `Context`
and `DigestRun`. `DigestRun` gives steps 2 and 6 their own technology, because
two relationships exist between the runner and Telegram (collection over
MTProto, delivery over the Bot API) and without it the view drew the
collection one twice.

**Sources** is rendered but has **not** been reviewed at reading size. Treat
that row as unverified: a valid DSL is not a presentation-ready diagram. Run
`make view` to check it.

`Landscape` sits at 12 elements against the 7/8 audience-overview budget. It is
kept there deliberately: a landscape's job is breadth, the rendered fan is
legible with no crossings, and the focused questions are answered by `Context`
and `Sources`. That is the rendered evidence the budget rule asks for before
exceeding it.

## Decisions

| ADR | Decision | Status |
|---|---|---|
| 1 | [Pin the unofficial X client to an exact version and audit every bump](decisions/0001-pin-unofficial-x-client.md) | Accepted |
| 2 | [Summarize through a fallback chain rather than a single model](decisions/0002-summarizer-fallback-chain.md) | Accepted; OpenRouter provider superseded by ADR 9 |
| 3 | [Keep SQLite as the system of record and D1 as a disposable copy](decisions/0003-sqlite-is-the-source-of-truth.md) | Accepted |
| 4 | [Deliver through independent channels, and keep email implemented](decisions/0004-multi-channel-delivery.md) | Accepted |
| 5 | [Forward relay-lane posts verbatim, with no summarization](decisions/0005-relay-forwards-verbatim.md) | Accepted |
| 6 | [Release by git tag, deploy by pin bump from a separate repository](decisions/0006-release-by-tag-and-pin.md) | Accepted; deployment ownership superseded by ADR 7 for Azure target |
| 7 | [Run digest as app-owned Azure jobs with leased SQLite snapshots](decisions/0007-run-digest-as-azure-jobs.md) | Accepted; state/vault ownership amended by ADR 8; cutover pending |
| 8 | [Isolate digest Azure state and secrets](decisions/0008-isolate-digest-azure-state-and-secrets.md) | Accepted for state/vault; existing subscription recommended; deployment pending |
| 9 | [Fall back to the Claude API over workload identity federation](decisions/0009-fall-back-to-the-claude-api-over-workload-identity-federation.md) | Accepted; implemented, federation not yet configured |

New ADRs use [the template](templates/adr.md) and keep the `NNNN-short-title.md`
numbering without gaps.

## Documents

| Area | Documents |
|---|---|
| Narrative (opens the tab and the PDF) | [01 overview](overview/01-notification-digest.md) · [02 scope](overview/02-scope.md) · [03 stakeholders](overview/03-for-stakeholders.md) · [04 CTO](overview/04-for-the-cto.md) · [05 engineers](overview/05-for-engineers.md) · [06 operators](overview/06-for-operators.md) · [07 glossary](overview/07-glossary.md) |
| Requirements | [Constraints](requirements/constraints.md) · [quality attributes](requirements/quality-attributes.md) · [assumptions](requirements/assumptions.md) |
| Principles | [Architecture principles](principles/architecture-principles.md) |
| Security | [Security architecture](security/security-architecture.md) · [trust boundaries](security/trust-boundaries.md) · [data classification](security/data-classification.md) |
| Data | [Data architecture](data/data-architecture.md) · [data ownership](data/data-ownership.md) |
| Integration | [Integration architecture](integration/integration-architecture.md) |
| Deployment | [Deployment architecture](deployment/deployment-architecture.md) · [environments](deployment/environments.md) |
| Reliability | [Availability](reliability/availability.md) · [backup strategy](reliability/backup-strategy.md) · [disaster recovery](reliability/disaster-recovery.md) |
| Observability | [Observability architecture](observability/observability-architecture.md) |
| Risks | [Architecture risks](risks/architecture-risks.md) · [technical debt](risks/technical-debt.md) |
| Roadmap | [Current state](roadmap/current-state.md) · [target state](roadmap/target-state.md) · [transition plan](roadmap/transition-plan.md) |

## Working on the model

```bash
make check
```

`make check` parses and inspects the workspace with the pinned Structurizr
image; it needs Docker. `make docs` runs the mechanical half of the
[docs-sync](../../.claude/skills/docs-sync/SKILL.md) audit. `make view` serves
the workspace locally for a visual review, and `make export` writes SVG, PNG and
Mermaid into `generated/`, which is gitignored.

Model fragments live in `model/` and are included in order from
[`workspace.dsl`](workspace.dsl). `model/styles-shared.dsl` is copied unchanged
from development-base and must stay that way; this repository's layer mapping
lives in `model/styles.dsl`.
