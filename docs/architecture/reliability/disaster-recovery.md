## Disaster Recovery

Scope: scenarios where the running system, its host, or its state is lost
outright — as opposed to [availability.md](availability.md), which covers a
single run or collector failing while the system as a whole is intact.

There is no second site, no standby compute, and no automated failover for any
of these scenarios. That is a deliberate consequence of a single-owner,
single-node service ([C-01](../requirements/constraints.md),
[C-05](../requirements/constraints.md)) — not a gap to apologize for. Recovery
is manual in every case below, and **no RTO has been measured for any
scenario**: there has been no timed rehearsal, so any duration below is a rough,
unverified estimate of the steps involved, not a committed target. Where an RPO
is stated, it follows directly from [backup-strategy.md](backup-strategy.md) or
from how the source system itself retains data — it is not a target either.

### Scenarios

#### 1. The VM is lost, the Proxmox node is fine

Disk failure, accidental deletion, or a bad update inside `01-myapps-vm` itself.

- **RPO:** up to ~24h of `state.db` writes (the gap since the last daily 04:15
  UTC snapshot), plus whatever Telegram/X items can be re-collected once cursors
  resume (near-zero for Telegram, since messages remain in the group; less
  certain for X's notification timeline, which has its own retention).
- **RTO:** UNVERIFIED / not measured. Outline: re-provision the VM via the
  homelab Ansible role, deploy the pinned image
  ([deployment-architecture.md](../deployment/deployment-architecture.md)),
  restore the latest `state.db` snapshot to `/srv/appdata/digest`, restart the
  timers.
- **Gap:** see [backup-strategy.md](backup-strategy.md)'s note on a restore
  re-triggering an already-delivered digest — this scenario is exactly where
  that matters.

#### 2. The Proxmox node itself is lost

Hardware failure. Because the runner, its state, and the *local* backup copy all
sit on this one node ([C-05](../requirements/constraints.md)), this scenario
depends entirely on the **offsite** copy of the snapshot surviving.

- **RPO:** same bound as scenario 1 (~24h), provided the offsite copy is intact
  and reachable.
- **RTO:** UNVERIFIED / not measured, and materially longer than scenario 1 —
  new or replacement hardware/hypervisor must exist before anything else can
  happen. No procedure for this step exists in either repository as far as this
  document can confirm.

#### 3. `state.db` is corrupt

A bad shutdown mid-write, a mistaken write to the VirtIO-FS share (see
[backup-strategy.md](backup-strategy.md)'s gotcha), or a disk-level fault on
`/srv/appdata`.

- **RPO:** up to ~24h, same snapshot bound as scenario 1.
- **RTO:** UNVERIFIED / not measured. Outline: stop the digest timers, restore
  the latest known-good snapshot over the corrupt file, restart.
- **Gap:** identical to scenario 1's restore/duplicate-delivery tension.

#### 4. Cloudflare D1 (Site Database) is lost

D1 is explicitly disposable by design — the rendering copy, not the source of
truth ([ADR 0003](../decisions/0003-sqlite-is-the-source-of-truth.md)).

- **RPO:** effectively none for the underlying data — everything D1 holds is
  derivable from `state.db`.
- **RTO:** UNVERIFIED, and the recovery path itself is unproven:
  [A-03](../requirements/assumptions.md) records that rebuilding D1 from the VM
  has never been exercised end to end ([TD-002](../risks/technical-debt.md)).
  "Disposable" describes the intended architecture, not a tested recovery.

#### 5. A source session or cookie dies

Not an infrastructure loss, but the failure this system is most exposed to day
to day: the Telegram `StringSession` or the X cookie jar stops working
([A-01](../requirements/assumptions.md),
[A-02](../requirements/assumptions.md)).

- **Detection:** that collector's own auth-failure handling; no alert beyond the
  digest visibly missing that source, or the collector's failure banner
  ([availability.md](availability.md)).
- **Recovery — Telegram:** the owner runs `scripts/telegram_login.py`
  interactively (one-time, from a local machine, never on the VM or in the
  container per that script's own docstring), generates a fresh `StringSession`,
  and updates the `digest-tg-session` secret in Azure Key Vault; the homelab
  repo then redeploys with the new secret.
- **Recovery — X:** the owner extracts a fresh cookie from an authenticated
  browser session and updates `X_COOKIES` / `X_COOKIES_PATH` in Key Vault the
  same way. This is manual and has no scripted equivalent in this repository.
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

Out of scope for this document — Azure Key Vault's own availability and recovery
are Microsoft's responsibility, not this system's. If Key Vault is unreachable
at deploy time, the affected deploy fails closed (no secrets, no start); this
has no bearing on an already-running deployment.

### What this document does not cover

Command-by-command restore procedures belong in an operational runbook, not here
— none is known to exist in either repository at the time of writing, which is
itself a gap: none of the scenarios above has a written, executable recovery
script committed anywhere this document could link to.
