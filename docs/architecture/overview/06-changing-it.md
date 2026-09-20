## Changing it

A reading path for the person about to modify this system: what it insists on,
what you don't control, what is already known to be wrong, what's actually
still open, and what a change must satisfy before it merges. Five stops.

### The principles the codebase actually follows

These are read from the codebase's own conventions and history, not aspiration
— each has a concrete implication for a change you're about to make.

- **[P-01](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/principles/architecture-principles.md) Simplicity over redundancy.** No HA tier, no staging environment; idempotency is the one property this never trades away.
- **[P-02](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/principles/architecture-principles.md) Idempotency is a hard, enforced contract.** Cursors advance only after commit, inserts are `ON CONFLICT DO NOTHING`; this is covered by `tests/test_state.py`, not a documentation promise.
- **[P-03](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/principles/architecture-principles.md) Config is read in exactly one place.** `digest/config.py` is the only module allowed to call `os.environ`; a new setting is added to `Config`, never read ad hoc.
- **[P-04](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/principles/architecture-principles.md) No secrets in git, logs, or images.** Every collection credential is a personal account session, so a leak is an account takeover, not a rotatable key.
- **[P-05](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/principles/architecture-principles.md) Isolate and surface failure, never suppress it.** A collector's failure is caught, flagged, and banner-reported — it must never crash the run or be swallowed silently.
- **[P-06](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/principles/architecture-principles.md) Delete or flag a lane rather than half-maintain it.** New capabilities ship behind a default-off flag; a design that turns out to rest on a signal that doesn't exist is dropped outright, not shipped half-built.

### The contracts you do not control

![Containers view: the building blocks and which side of the trust boundary each sits on](embed:Containers)

Every collection source and model provider is a contract with a system this
repository does not own; several are explicitly unofficial, reverse-engineered
or ToS-violating access the owner has accepted the risk of
([C-03](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/constraints.md)).
Telegram's MTProto contract is stable as a protocol; X, Reddit and Patreon are
all low-stability and can break without notice — X's unofficial client has
already broken completely once, when upstream `twikit` failed against an X
webpack change. That is why `twifork` (the fork this system depends on for X)
is pinned to an **exact version**, not the `>=` range every other dependency
uses, and every bump is gated behind Renovate's Dependency Dashboard with a
mandatory hand audit of the diff before it can be trusted
([ADR 1](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0001-pin-unofficial-x-client.md)).
The two contracts this repository owns end-to-end — news-site ingest and
Telegram Bot API delivery — only break on a mismatch between its own two ends.

- [Integration architecture](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/integration/integration-architecture.md)

### What is already known to be wrong

Six items of technical debt are tracked, each with a plan or an explicit
non-plan — no silent gaps:

- **[TD-001](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/technical-debt.md)** — `arc_context` has no prune; it grows without bound and no fix is proposed.
- **[TD-002](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/technical-debt.md)** — the "D1 is disposable and rebuildable" design intent has never been exercised end to end.
- **[TD-003](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/technical-debt.md)** — end-to-end run latency has never been measured; a slow run would currently go unnoticed.
- **[TD-004](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/technical-debt.md)** — Hungarian readers never see the "What changed" delta block; an open, unapproved owner decision.
- **[TD-005](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/technical-debt.md)** — `config.py`'s in-repo defaults for two flags don't match what's actually live in production.
- **[TD-006](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/technical-debt.md)** — a restore from backup can re-deliver an already-delivered digest.

**TD-006 matters most before you touch delivery or backup code.** It directly
contradicts [QA-01](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/quality-attributes.md),
the system's one absolute guarantee: a restored snapshot's stale delivery
flags make an already-sent digest look pending again to the retry pass, and
nothing reconciles the two. Today's mitigation is manual — check which
digests were pending as of the snapshot before the next timer fires. If your
change touches either side of that seam, this is the contract it can break.

### Where it is going

There is no forward-looking target architecture beyond what already runs.
PLAN.md's own roadmap (§11) is almost entirely shipped; the one entry that
didn't ship (§11.5, community engagement signals) was **rejected outright**,
not deferred, once the owner observed the signal it depended on doesn't occur.
What remains is a short list of individually optional, owner-gated items —
not work in flight:

| Item | Status |
|---|---|
| Publish Hungarian delta text (`deltas_hu`) | Open, owner-gated — unapproved (closes TD-004) |
| Flip `VERIFY_DAILY_ENABLED`'s in-repo default to match production | Open, owner-gated (closes part of TD-005) |
| Four-part structure for repeat-story sections | Optional rider — dropping it is an accepted outcome |
| Site-side status chips for the verification pass | Not started, unscheduled |

No second node, no staging tier, no hard spend cap, and no revival of §11.5
appear here — each is explicitly ruled out, not simply unlisted.

- [Target state](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/roadmap/target-state.md)
- [Transition plan](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/roadmap/transition-plan.md)

### What a change must satisfy before it merges

- **`/docs-sync`** before opening or updating any PR — audits the branch diff
  for documentation it falsifies (README, PLAN.md, CLAUDE.md, worker READMEs).
- **`make check`** whenever `docs/architecture/` changes — parses and inspects
  the Structurizr model; `make docs` runs docs-sync's mechanical half.
- **`uv run ruff check . && uv run pytest`** for the Python service — tests
  must never call the real Telegram/X APIs or send real email; mock at the
  collector and emailer boundaries.
- **The news-site golden-diff contract**, for anything under
  `workers/news-site/`: `npm test` runs invariant tests, a byte-comparison
  against committed golden pages, and a prettier check. A change that should
  not alter rendered output must produce a **zero** golden diff; a change that
  should alter it regenerates the goldens (`npm run golden`), so the diff
  itself is the review artifact.
- A merge to `main` **ships nothing by itself** — a release is a tag, a built
  image, a pin bump in the separate homelab repository, and a deploy
  ([ADR 6](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0006-release-by-tag-and-pin.md)).
