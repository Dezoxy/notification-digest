## Reviewing the design

A reading path for someone deciding whether this design is sound rather than
building or operating it. It asks what bounds the system, what it is built
from, what a run actually guarantees, which trade-offs were made on purpose,
and what was deliberately left out. Five stops.

### The system, and what bounds it

Almost every structural choice here follows from two facts: there is exactly
one owner, who is also the only operator and the only reader (C-01), and
Telegram and X are reached with the owner's own personal account sessions,
not service or bot credentials (C-02). Collection is always outbound — nothing
on the home network accepts an inbound connection from a source.

![Context view: who reads the digest, what writes it, and where it comes out](embed:Context)

Eight constraints (C-01 through C-08) are not traded off; they fix the option
space — see
[constraints](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/constraints.md).
Six assumptions (A-01 through A-06) are believed, not proven, and each names
what breaks if it's wrong. A-03 (the site database is rebuildable from the VM)
and A-04 (the home LAN is trusted) carry the most structural weight — see
[assumptions](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/assumptions.md).

### The building blocks, and the trust split

Four containers do the work: the Digest Runner collects, summarizes and
delivers; the State Database is the system of record; the News Site serves a
public archive; the Site Database is a disposable rendering copy. The runner
is not a service — it starts on a timer, does one job, and exits, so every
outbound call it makes lives inside a run with a beginning and an end. That is
also the trust split: the runner and its state sit on the owner's home
network holding personal-account secrets, while the site is a public-facing
Cloudflare Worker that only ever receives already-summarized content.

![Containers view: the building blocks and the trust boundary between them](embed:Containers)

### What one run does, and why a re-run is safe

![Runtime view: what happens during one scheduled digest run](embed:DigestRun)

A run reads the last-seen cursor per source before fetching anything, and
advances it only after the new items are committed. That ordering is what
makes a crashed or re-run process harmless: it collects nothing already
recorded and delivers nothing already sent. This is QA-01, idempotency — the
one property this design will not trade away, enforced by the cursor contract
and a `INSERT ... ON CONFLICT DO NOTHING` insert, not left as a documentation
promise. See
[quality attributes](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/quality-attributes.md).

### The decisions already made, and their trade-offs

Six ADRs, each accepted, each naming what it gave up:

- [ADR
  1](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0001-pin-unofficial-x-client.md)
  — pin the unofficial X client exactly and gate every bump behind a manual
  hand-audit, trading automation for a bump that can never look routine.
- [ADR
  2](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0002-summarizer-fallback-chain.md)
  — fall back through a chain of different models on refusal or outage, trading
  a hard cost cap for availability.
- [ADR
  3](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0003-sqlite-is-the-source-of-truth.md)
  — SQLite on the VM is the one system of record; Cloudflare D1 is a one-way,
  disposable rendering copy, trading a real recovery rehearsal (never done) for
  a simple ownership rule.
- [ADR
  4](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0004-multi-channel-delivery.md)
  — keep email fully implemented as the default channel even though the live
  deployment disables it, trading ongoing maintenance of an unused path for a
  deployment that never fails to start with zero configuration.
- [ADR
  5](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0005-relay-forwards-verbatim.md)
  — one lane forwards public posts verbatim with no model in the loop, trading
  the summarized lanes' "never verbatim" posture (C-04) for handling media the
  model cannot caption.
- [ADR
  6](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0006-release-by-tag-and-pin.md)
  — release by git tag, deploy by a separate pin bump in another repository,
  trading one-step deploy convenience for a reproducible, auditable release
  history.

### What the design does not do, and why

No high-availability tier, no on-call rotation, no staging environment. One
Proxmox node holds the runner, its state, and the primary backup copy (C-05) —
a single failure domain, by design, not by oversight. This is P-01,
simplicity over redundancy: time spent on resilience nobody needs is time not
spent on the digest itself, and a missed run is an accepted outcome rather
than something to engineer around (QA-06). The one named exception is
idempotency (P-02), which is never traded for simplicity. See
[principles](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/principles/architecture-principles.md)
and, for what this trade-off costs if the node is actually lost,
[RISK-001](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md).
