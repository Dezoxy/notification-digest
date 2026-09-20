# Backup Strategy

The only store this system needs to back up is `state.db` — the SQLite system of record at `/srv/appdata/digest` ([ADR 0003](../decisions/0003-sqlite-is-the-source-of-truth.md)). Everything else is either disposable, replaceable from that file, or out of this repo's scope.

## What is backed up

| What | Method | Schedule | Retention | Where | Verified |
|---|---|---|---|---|---|
| `state.db` (items, cursors, digests, delivery state, arc context) | `digest-backup.timer` snapshot | Daily, 04:15 UTC | 7 days, aged out by `tmpfiles.d` | A local copy plus an offsite copy | Integrity checked; see below |
| Markdown archive (`ARCHIVE_DIR`) | PLAN.md's own history (§7 checklist) additionally records an existing, broader `/srv/appdata` backup covering `digest/state.db` and `digest/archive/`. Whether that broader mechanism is the same as, or in addition to, `digest-backup.timer` is **not resolvable from this repository** — its configuration lives entirely in the homelab repo | — | — | — | Not independently confirmed here |
| Site Database (Cloudflare D1) | None. Deliberately not backed up | — | — | — | Disposable rendering copy, rebuildable from `state.db` — but see the gap noted below |
| Runtime secrets (Key Vault) | Azure's own backup/versioning, not this system's concern | — | — | — | Out of scope for this document |
| Infrastructure (compose/systemd unit definitions, Ansible role) | Git history in the homelab repo | Per change | Repository history | — | Out of scope for this document |

The backup mechanism itself — the script invoked by `digest-backup.timer` — is not part of this repository; it lives in the homelab repo and its exact commands (e.g. whether it uses `sqlite3 .backup`, a plain file copy, or `restic`) are **not verified here**. What this repository does confirm is that the application is built to make a live, consistent snapshot possible: `digest/state.py`'s `connect()` runs SQLite in `journal_mode=WAL` specifically, per its own docstring, "so an external `sqlite3 .backup` snapshot run against the live file [can run] without blocking or being blocked by a concurrently-running digest process," with `busy_timeout=5000` absorbing any brief lock contention between a backup job and a timer-driven run. Snapshots are root-owned.

## The VirtIO-FS gotcha

SQLite must never be opened directly on the VirtIO-FS share used for the offsite copy — it fails there with `disk I/O error`, which looks like corruption but is not. To inspect or verify the offsite copy, `cp` it off the mount onto local storage first, then open it. This is an operational fact about the host, not something the application code can detect or work around; it belongs here because a backup that cannot be safely opened where it lives is not yet a usable backup.

There is also no `sqlite3` binary on the VM — inspection there is done with `python3`'s `sqlite3` module.

## Restore

The application code has no restore path of its own — restoring means stopping the digest timers, copying the chosen snapshot back over `/srv/appdata/digest/state.db`, and letting the next scheduled run resume from the cursors that snapshot contains. [QA-05](../requirements/quality-attributes.md) records that restore has been exercised once; this repository holds no written transcript of that exercise, so the exact steps and scope of that test are not reconstructable here — the procedure above is the intended one, not a confirmed rehearsed runbook.

Restoring rolls state back to the snapshot's point in time. Because `digests` rows carry per-channel delivery flags, restoring to a snapshot taken *before* a digest was marked delivered — followed by a run's normal pending-digest retry pass — can cause that digest to be resent. This is a real tension with the system's one hard rule ([QA-01](../requirements/quality-attributes.md), "a duplicate delivery is not tolerable") that a naive restore does not automatically avoid; there is no automated safeguard against it today ([TD-006](../risks/technical-debt.md)), and any restore should be followed by a manual check of which digests were pending as of the snapshot before the next scheduled run fires.

## What restoring does *not* recover

- Items collected and delivered between the snapshot and the loss are gone from `state.db`, though most sources (notably Telegram, since messages remain in the group) can be re-collected once cursors catch up — see [disaster-recovery.md](disaster-recovery.md) for the RPO this implies per scenario.
- The Site Database (D1) is not restored from any backup of its own; it is rebuilt by republishing from `state.db`. [A-03](../requirements/assumptions.md) records that this rebuild path is currently untested end to end ([TD-002](../risks/technical-debt.md)), so "D1 is disposable" is a design intent, not a verified recovery procedure.
