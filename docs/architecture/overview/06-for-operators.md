# For operators

A reading path for the person who has to keep this service running and fix it
when it breaks. Five stops.

## How a change reaches the host

![Delivery view: how a code change reaches the running host](embed:Delivery)

A merge to `main` in this repository ships nothing by itself (C-07). A release
is a pushed `v*` tag, built and pushed to GHCR by
`.github/workflows/release.yml` with no floating `latest` tag; a separate
`homelab` repository pins that exact version and performs the deploy as its
own manual step. Nothing here propagates a tag automatically into production.
See ADR 6 and **Deployment Architecture**, later in this document
([source](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0006-release-by-tag-and-pin.md)).

## Where it runs, and what fails together

![Deployment view: the single node, the VM, its two paths and the Cloudflare edge](embed:ProductionDeployment)

Everything the runner needs — the process, its state, and the primary backup
copy — sits on one Proxmox node on the home LAN, never internet-reachable
(C-05). Nine systemd timers drive the various run modes (the six-hourly
window, daily, weekly, evening, overnight, patreon, positions, relay, backup),
and every one of them goes through a single host wrapper,
`/usr/local/sbin/digest-run`, which takes an `flock`. The lock exists because
every run shares one Telegram user session; two concurrent connections on
that session previously produced `AuthKeyDuplicatedError` during a boot storm.
The wrapper's own script lives in the homelab repository, so its exact
blocking behavior is not verifiable from here. See **Environments**, later in
this document.

## What "available" means for a one-shot job

There is no request to time out and no uptime percentage to hit — QA-06
deliberately sets no availability target. What matters, in order: a digest is
never delivered twice (QA-01, absolute), a missed run is tolerable because
collection is cursor-based and the next run picks up what the last one
missed, and one failing collector must not suppress the rest. In practice
this shows up as isolated, banner-flagged collector failures, a per-run
Telegram circuit breaker that trips on the first HTTP 429 rather than
retrying into a flood, and a freshness guard that marks a digest older than
24h delivered without actually sending it to Telegram. See **Availability**,
later in this document.

## Backups and restore

The only store worth backing up is `state.db` — the SQLite system of record.
A daily 04:15 UTC snapshot goes local and offsite, kept for 7 days; WAL mode
lets the snapshot run without blocking a concurrently-running digest. The one
sharp gotcha: the offsite copy sits on a VirtIO-FS share, and SQLite cannot
open a database file there directly — it fails with `disk I/O error`, which
looks like corruption and is not. Copy the file off the mount before opening
it. Restoring is a manual file copy back over
`/srv/appdata/digest/state.db` and a timer restart — there is no restore path
in the application itself, and it rolls state back to the snapshot's point in
time. That has a real, unresolved consequence (TD-006): a digest already
marked delivered before the snapshot can look pending again after a restore,
and the normal retry pass will resend it, directly against QA-01. There is no
automated safeguard for this today — after any restore, check which digests
were pending as of the snapshot before the next timer fires. See **Backup
Strategy** and **Disaster Recovery**, later in this document.

## How a failure actually becomes noticeable

Honestly: mostly by the digest not arriving. The application emits an exit
code (0 success, 1 a collector or delivery leg failed, 2 a config error before
any work started) and structured JSON log lines to stdout, captured by
journald on the VM. The code's own comments assume a Loki/Grafana alert fires
on a failed `digest.service` run, but whether that alert is actually
configured and working cannot be confirmed from this repository — treat it as
unconfirmed, not as fact. In practice, in order of what is actually known to
work: the digest doesn't arrive or arrives with a visible per-source failure
banner; `systemctl status` or `journalctl` shows a failed unit if the owner
goes looking; an automated alert fires only if the homelab-side pipeline is
actually wired up. There is no metrics pipeline, no tracing, and no dashboard
for this service. See **Observability Architecture**, later in this document.
