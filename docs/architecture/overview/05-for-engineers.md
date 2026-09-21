## For engineers

A reading path for someone who would build or review this system's design:
what bounds it, what it is built from, what a run actually guarantees, which
trade-offs were made on purpose, what you do not control, and what a change
must satisfy before it merges. Six stops.

### What the system is and what bounds it

Almost every structural choice here follows from two facts: there is exactly
one owner, who is also the only operator and the only reader (C-01), and
Telegram and X are reached with the owner's own personal account sessions,
not service or bot credentials (C-02). Collection is always outbound —
nothing on the home network accepts an inbound connection from a source.

![Context view: who reads the digest, what writes it, and where it comes out](embed:Context)

Eight constraints (C-01 through C-08) fix the option space; six assumptions
(A-01 through A-06) are believed, not proven, each naming what breaks if it
is wrong — A-03 (the site database is rebuildable) and A-04 (the LAN is
trusted) carry the most structural weight. See **Constraints** and
**Assumptions**, later in this document.

### The building blocks and the trust split

Four containers do the work: the Digest Runner collects, summarizes and
delivers; the State Database is the system of record; the News Site serves a
public archive; the Site Database is a disposable rendering copy. The runner
is not a service — it starts on a timer, does one job, and exits, so every
outbound call it makes lives inside a run with a beginning and an end. That
is also the trust split: the runner and its state sit on the owner's home
network holding personal-account secrets, while the site is a public-facing
Cloudflare Worker that only ever receives already-summarized content.

![Containers view: the building blocks and the trust boundary between them](embed:Containers)

### What one run does, and why a re-run is safe

![Runtime view: what happens during one scheduled digest run](embed:DigestRun)

A run reads the last-seen cursor per source before fetching anything, and
advances it only after the new items are committed. That ordering is what
makes a crashed or re-run process harmless: it collects nothing already
recorded and delivers nothing already sent. This is QA-01, idempotency — the
one property this design will not trade away, built from three independent
mechanisms: storage-level uniqueness (`items.UNIQUE(source, source_id)`),
cursor advancement only after commit, and an idempotent delivery upsert
(`PUT /ingest/:id`, plus `push_sent`'s claim-once insert). See **Data
Architecture**, later in this document, for the exact mechanisms.

### The decisions already made

Six ADRs, each accepted, each naming its trade-off:

- **ADR 1** pins the X client exactly, trading automation for an audited bump.
- **ADR 2** falls back across models on refusal, trading a cost cap for uptime.
- **ADR 3** keeps SQLite authoritative; D1 is a disposable copy, unrehearsed.
- **ADR 4** keeps email live but off, trading upkeep for a zero-config start.
- **ADR 5** relays one lane verbatim, trading C-04's rule for uncaptioned media.
- **ADR 6** releases by tag, deploys by pin bump, trading speed for an
  auditable history.

### The contracts you do not control

![Sources view: which accounts and feeds a run collects from](embed:Sources)

Every collection source and model provider is a contract with a system this
repository does not own; several are unofficial or ToS-violating access the
owner has accepted the risk of (C-03). Telegram's MTProto contract is stable
as a protocol; X, Reddit and Patreon are all low-stability and can break
without notice — upstream `twikit`, the base of the `twifork` client this
system depends on, already broke completely once against an X webpack
change. That is why `twifork` is pinned to an **exact version**, not the
`>=` range every other dependency uses, and every bump is gated behind
Renovate's Dependency Dashboard with a mandatory hand audit before it can be
trusted (ADR 1). See **Integration Architecture**, later in this document,
for every source, provider and delivery contract in one table.

### What is already known to be wrong, and what a change must satisfy

Six principles (P-01 through P-06) are read from the codebase's own
conventions, not aspiration, and six items of technical debt (TD-001
through TD-006) are tracked, each with a plan or an explicit non-plan.
**TD-006 matters most before you touch delivery or backup code**: a restore
from backup can re-deliver an already-delivered digest, directly
contradicting QA-01, the one absolute guarantee this system makes — a
restored snapshot's stale delivery flags make an already-sent digest look
pending again to the retry pass, and nothing reconciles the two. See
**Architecture Principles** and **Technical Debt**, later in this document.

![Delivery view: how a change reaches production, and with which identity](embed:Delivery)

Before a change merges: `/docs-sync` audits the branch diff for
documentation it falsifies; `make check` inspects the Structurizr model
whenever `docs/architecture/` changes; `uv run ruff check .` and `uv run
pytest` cover the Python service, whose tests must never call real Telegram
or X APIs or send real email; and the news-site golden-diff contract under
`workers/news-site/` demands a zero diff for any change that should not
alter rendered output. A merge to `main` ships nothing by itself — a release
is a tag, a built image, a homelab pin bump, and a deploy (ADR 6).
