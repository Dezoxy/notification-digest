# For stakeholders

A reading path for someone who wants to know what this system is, what it
produces, what it costs and what could go wrong — without host names,
protocols or code. Four stops.

## What it is, in one paragraph

One person follows a lot of sources: private group chats, a notifications
timeline, a few paid and public feeds. Reading all of them is an obligation
that grows and never finishes. This system reads them instead, every few
hours, and produces one short digest with links back to anything worth
opening. Nothing is invented: it summarizes what was posted and points at the
original.

It serves exactly one person, who is also the person who runs it. That is not
a limitation to be fixed later — it is the decision that explains almost every
other one in this document
([C-01](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/constraints.md), and **Constraints**, later in this
document).

![Context view: who reads the digest, what writes it, and where it comes out](embed:Context)

## What it produces, and where it goes

Two things reach a reader:

| Output | Who sees it |
|---|---|
| A short message with the digest and its links | The owner, in a chat app |
| A public archive of past digests on a website | Anyone with the address |

The website is the only part of this system the public can reach. Everything
else runs on one machine in the owner's home and is not reachable from the
internet.

Three separate lanes run on their own schedules and never mix: the main
digest, one for paid posts, and one that tracks a small set of projects and
stays **silent** when nothing meaningful happened. Silence is a feature — a
tracker that reports "nothing today" every day stops being read.

## What it costs, and what is not controlled

The running costs are a single machine the owner already owns, a free-tier
website host, and payment per summary to a language-model provider.

The model spend is the one cost with no ceiling. There is no cap enforced in
the system: an unusually busy window costs more, and a failure that falls back
to a second provider costs more again. This is a known, accepted exposure
rather than an oversight — see **Architecture Risks** later in this document
([RISK-004](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md)).

## What could go wrong

Stated plainly, because a reader deserves the real list rather than a
reassuring one:

- **The sources can stop working.** Several are read in ways their owners do
  not officially support. When one changes, that source goes quiet until
  somebody fixes it. Nothing else breaks.
- **The credentials are personal.** The system reads the owner's own accounts
  using the owner's own sessions. If those leaked, the loss would be access to
  those accounts — not merely to this system. This is the sharpest risk here
  and it is covered in **Trust Boundaries**.
- **There is one machine.** No second site, no automatic failover. If it is
  lost, the service is down until it is rebuilt by hand, and up to a day of
  collected history may be gone. Recovery has never been timed, so no honest
  estimate of "how long" exists — see **Disaster Recovery**.
- **A missed digest is acceptable; a repeated one is not.** The system is
  built so that re-running it cannot deliver the same digest twice. One known
  gap remains: restoring from a backup can re-send something already sent
  ([TD-006](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/technical-debt.md)).

What this system does **not** try to be: always-on, multi-user, or staffed.
Those were not missed. They were declined, and **Scope** says why.
