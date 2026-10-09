## Disaster Recovery

Scope: scenarios where the running system, its host, or its state is lost
outright — as opposed to [availability.md](availability.md), which covers a
single run or collector failing while the system as a whole is intact.

There is no second region, no standby compute, and no automated failover for
any of these scenarios. That is a deliberate consequence of a single-owner,
single-region service ([C-01](../requirements/constraints.md),
[C-05](../requirements/constraints.md)) — not a gap to apologize for. Recovery
is manual in every case below, and **no RTO has been measured for any
scenario**: there has been no timed rehearsal, so any duration below is a rough,
unverified estimate of the steps involved, not a committed target. Where an RPO
is stated, it follows directly from [backup-strategy.md](backup-strategy.md) or
from how the source system itself retains data — it is not a target either.
**No restore from the backup storage account has been exercised yet**, so every
scenario that depends on it rests on a designed procedure, not an observed one.

Recovery stays within Azure. Until 2026-10-08 production ran on a homelab VM;
that VM no longer runs the digest, its frozen state is not a recovery source,
and returning execution to it is outside the approved migration
([ADR 0007](../decisions/0007-run-digest-as-azure-jobs.md)).

### Scenarios

#### 1. The runtime state is lost or damaged, the region is fine

The state bundle or its manifest in the runtime storage account is corrupt,
deleted by mistake, or left unusable by a bad release that migrated it. The jobs
fail closed rather than create an empty database to make startup succeed.

- **RPO:** up to ~24h of state writes (the gap since the last daily 04:15 UTC
  recovery copy in the backup account), plus whatever Telegram/X items can be
  re-collected once cursors resume (near-zero for Telegram, since messages
  remain in the group; less certain for X's notification timeline, which has
  its own retention). The runtime account also keeps seven daily referenced
  generations, which may be fresher if they are intact; they are not an
  independent copy.
- **RTO:** UNVERIFIED / not measured. Outline, per the
  [runbook](../../azure-migration.md#interrupted-runs-and-recovery): drain the
  jobs, preserve the failed generation for diagnosis, download one complete
  recovery prefix from the backup account, verify it, bootstrap it into a fresh
  state namespace, point the reviewed Terraform configuration at that namespace
  and resume. If a bad release caused it, revert the image pin too, which only
  helps while the state schema has not been migrated by the newer image.
- **Gap:** see [backup-strategy.md](backup-strategy.md)'s note on a restore
  re-triggering an already-delivered digest — this scenario is exactly where
  that matters.

#### 2. The Azure region, or the subscription, is lost or unavailable

The jobs, the runtime storage account, the digest Key Vault and the backup
storage account all sit in West Europe and in one subscription
([C-05](../requirements/constraints.md)). The backup account is a separate
account and resource group, which protects against account-level loss, not
against loss of the region or of the subscription: both copies of the state go
together, and neither storage account is immutable.

- **RPO:** if the region is merely unavailable, nothing is lost and the system
  resumes from its last checkpoint when Azure returns. If the data is
  destroyed, there is no copy outside the region, so the state is **not
  recoverable from this system's own backups**. The sources (Telegram messages,
  RSS feeds) still hold their content, so a rebuilt state can re-collect what
  they retain, but delivery history and cursors would be lost.
- **RTO:** UNVERIFIED / not measured, and bounded by Azure's own recovery. No
  procedure for rebuilding in another region or subscription exists in this
  repository, and none is planned; cross-region or immutable retention is a
  future requirement decision, not a current protection.

#### 3. The state is corrupt in a way a restore must repair

A bad checkpoint written before the damage was noticed, or a lost lease that
left an ambiguous commit. Lost lease ownership or a missing or corrupt state
fails closed, and an ambiguous manifest write is never retried blindly.

- **RPO:** up to ~24h, same recovery-copy bound as scenario 1.
- **RTO:** UNVERIFIED / not measured. Outline: the scenario 1 procedure, with
  the damaged canonical manifest retained rather than overwritten.
- **Gap:** identical to scenario 1's restore/duplicate-delivery tension, plus a
  Telegram send whose outcome is unknown: Telegram sending and Blob checkpoints
  cannot commit atomically, so an uncertain send blocks automatic resend for
  that channel until the owner reconciles it against the destination.

#### 4. Cloudflare D1 (Site Database) is lost

D1 is explicitly disposable by design — the rendering copy, not the source of
truth ([ADR 0003](../decisions/0003-sqlite-is-the-source-of-truth.md)).

- **RPO:** effectively none for the underlying data — everything D1 holds is
  derivable from the state.
- **RTO:** UNVERIFIED, and the recovery path itself is unproven:
  [A-03](../requirements/assumptions.md) records that rebuilding D1 from the state
  has never been exercised end to end ([TD-002](../risks/technical-debt.md)).
  "Disposable" describes the intended architecture, not a tested recovery.

#### 5. A source session or cookie dies

Not an infrastructure loss, but the failure this system is most exposed to day
to day: the Telegram `StringSession` or the X cookie jar stops working
([A-01](../requirements/assumptions.md),
[A-02](../requirements/assumptions.md)).

- **Detection:** that collector's own auth-failure handling and the collector's
  failure banner in the digest ([availability.md](availability.md)). A failed
  collector makes the run exit non-zero, which the `failures` alert is built to
  catch ([observability-architecture.md](../observability/observability-architecture.md));
  whether it has caught a real auth failure is not recorded.
- **Recovery — Telegram:** the owner runs `scripts/telegram_login.py`
  interactively (one-time, from a local machine, never in the container per
  that script's own docstring) and generates a fresh `StringSession`. The new
  session goes into the digest Key Vault through the
  [credential rotation procedure](../../azure-migration.md#credential-rotation-after-cutover):
  drain the jobs, set the secret, verify a Manual Azure run authenticates, then
  resume the schedules. Updating the homelab vault or running homelab deploy no
  longer changes anything the digest uses.
- **Recovery — X:** the owner extracts a fresh cookie from an authenticated
  browser session and applies it with the leased
  [cloud cookie command](../../azure-migration.md#rotate-the-cloud-x-cookie-jar),
  which updates canonical Blob state, then verifies a Manual run. No Key Vault
  copy of the cookies is used. Browser re-authentication remains manual.
- **RPO:** near-zero for Telegram once auth is restored (messages remain in the
  group and are re-collected on the next cursor advance); not well-defined for
  X, since the notifications timeline's own history depth is outside this
  system's control.
- **RTO:** entirely dependent on the owner's availability to re-authenticate by
  hand; not measured, and structurally cannot be automated given C-02
  (personal-account sessions, not service credentials).
- **Isolation:** a dead session affects only that one collector. The other five
  window collectors, and the `patreon`/`positions`/`relay` lanes that do not
  depend on the same session state in the same way, are unaffected.

#### 6. Runtime secrets (Key Vault) become unavailable

Largely out of scope for this document — Azure Key Vault's own availability and
recovery are Microsoft's responsibility, not this system's. The jobs reference
the digest Key Vault's secrets through their managed identity; a job that cannot
resolve them cannot run, which surfaces as failed or missing runs and so as the
`failures` or `freshness` alert rather than as silent loss. The vault has purge
protection and soft delete, but this system has no second copy of the secret
values: losing one means re-issuing it at the source (a new Telegram session, a
new token), the same manual re-authentication as scenario 5.

### What this document does not cover

Command-by-command restore procedures belong in an operational runbook, not here.
The [migration runbook](../../azure-migration.md) now holds the cloud ones — the
interrupted-run and restore steps, the independent backup account, credential and
cookie rotation — which the VM era is not known to have had in either repo. Its
restore procedure is written, not rehearsed: no scenario above has been timed,
and a restore from the separate backup account has not been run end to end. The
region-loss scenario has no procedure at all.
