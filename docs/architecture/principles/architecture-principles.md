# Architecture Principles

Each principle has a rationale and an implication. A principle without an implication is a slogan. These are the principles this codebase's own conventions and history actually evidence — not an aspirational list.

## P-01 Simplicity over redundancy

- **Rationale:** There is one owner, who is also the only operator and the only reader ([C-01](../requirements/constraints.md)). Time spent on resilience nobody needs is time not spent on the digest itself.
- **Implication:** No HA tier, no on-call rotation, no staging environment ([environments.md](../deployment/environments.md)). A missed run is an accepted outcome, not a failure to engineer around ([QA-06](../requirements/quality-attributes.md)).
- **Exception:** Idempotency (P-02) is never traded away for simplicity — it is the one property this system will not compromise on.

## P-02 Idempotency is a hard, enforced contract

- **Rationale:** A crashed or re-run process must never duplicate an item or a delivery. Silent duplication is worse than a missed run, because the reader has no way to notice it happened.
- **Implication:** Every collector reads its cursor before fetching and advances it only after a successful commit; inserts use `INSERT ... ON CONFLICT (source, source_id) DO NOTHING`; cursors, items and delivery flags all advance together in one transaction ([QA-01](../requirements/quality-attributes.md)). This is enforced in code and covered by `tests/test_state.py`, not left as a documentation promise.

## P-03 Config is read in exactly one place

- **Rationale:** A setting with two ways to reach the code has two ways to disagree with itself.
- **Implication:** `digest/config.py` is the only module allowed to call `os.environ`/`os.getenv`; every other module receives values passed in (CLAUDE.md, Conventions). Adding a setting means adding it to `Config`, never reading it ad hoc wherever it happens to be needed.

## P-04 No secrets in git, logs, or images

- **Rationale:** Every collection credential in this system authenticates as the owner's own personal account, not a service principal ([C-02](../requirements/constraints.md)) — a leaked secret is an account takeover, not a rotatable API key.
- **Implication:** Secrets are injected as environment variables at deploy time from Azure Key Vault, never committed and never baked into the image ([C-06](../requirements/constraints.md)). Secret-bearing config fields carry `repr=False`; error messages name the offending variable and never echo its value.

## P-05 Isolate and surface failure, never suppress it

- **Rationale:** A digest built from several independent, uncontrolled third-party sources will have a source fail on any given run. What matters is whether that failure is visible or silent.
- **Implication:** Each collector's failure is caught, flagged, and reported as a banner in the delivered digest rather than crashing the run or being swallowed. The process exit code is the one signal the host acts on, and nothing in the application second-guesses it with a separate, independently-maintained alert path.

## P-06 Delete or flag a lane rather than half-maintain it

- **Rationale:** A single-owner codebase has no spare capacity to carry a feature that is neither fully supported nor removed.
- **Implication:** New capabilities ship behind an explicit, default-off flag until validated against real output (`X_ENABLED`, `VERIFY_DAILY_ENABLED`, `CONTEXT_ENABLED`, `TRANSLATE_HU_ENABLED`). When a design turns out to rest on a signal that doesn't exist, it is rejected outright rather than shipped half-built — PLAN.md §11.5 (community engagement signals) was designed in full and then dropped entirely once the owner observed the signal it depended on doesn't occur (PLAN.md §9, decision 6), rather than being shipped as a feature that would have quietly rendered nothing but zeros.
