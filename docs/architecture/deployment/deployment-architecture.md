# Deployment Architecture

Modelled in [`../model/deployment.dsl`](../model/deployment.dsl); see the
**ProductionDeployment** view (`docs/architecture/model/views.dsl`). This
document adds what the diagram cannot carry: the chain from a git tag to a
running container, and the schedule/locking model on the host. Local development
is covered separately in [environments.md](environments.md) — it is not part of
this model.

## What runs where

| Component | Runs on | Technology | Notes |
|---|---|---|---|
| Digest Runner | `01-myapps-vm` (Debian VM, LAN-only) | Python 3.14, one-shot Docker container, non-root (`uid 1000`) | Started by a systemd timer, does one job, exits. Never long-running, never part of `docker compose up -d` |
| State Database | Same VM, `/srv/appdata/digest` | SQLite file on local ext4 | System of record. Never placed on the VirtIO-FS share (see [backup-strategy.md](../reliability/backup-strategy.md)) |
| News Site | Cloudflare Workers | Cloudflare Worker, JavaScript (`workers/news-site/`) | Deployed independently with `wrangler`, not from this repo's release pipeline |
| Site Database | Cloudflare D1 | Managed | Rendering copy only; disposable, rebuilt from the state database ([ADR 0003](../decisions/0003-sqlite-is-the-source-of-truth.md)) |

The Proxmox node hosting `01-myapps-vm` is a single node on the owner's home
LAN, not internet-reachable, and is the whole failure domain for the runner and
its state ([C-05](../requirements/constraints.md)). There is exactly one
environment ([model/deployment.dsl](../model/deployment.dsl)); it is not staged
or replicated. See [availability.md](../reliability/availability.md) and
[disaster-recovery.md](../reliability/disaster-recovery.md) for the
consequences.

## Tag → image → pin → deploy

This repository cannot deploy itself ([C-07](../requirements/constraints.md),
[ADR 0006](../decisions/0006-release-by-tag-and-pin.md)). A merge to `main` here
ships nothing on its own:

1. **Tag.** The owner pushes a `v*` tag (e.g. `v0.26.4`) to this repo.
2. **Build.** `.github/workflows/release.yml` triggers on that tag, builds the
   image from `Dockerfile`, and pushes it to
   `ghcr.io/dezoxy/notification-digest`. `docker/metadata-action`'s
   `type=semver,pattern={{version}}` strips the leading `v`, so tag `v0.26.4`
   becomes image tag `0.26.4`. `flavor: latest=false` is set explicitly — there
   is deliberately no floating `latest` tag, because nothing would ever consume
   one: the homelab repo always pins an exact version.
3. **Never built on the VM.** The image is built only by that workflow. `docker
   build .` / `docker compose build` in this repo (`compose.yml`) are for local
   development only.
4. **Pin.** The separate `homelab` repository (Ansible role `myapps`) pins the
   exact image version as `myapps_digest_image` in
   `ansible/roles/myapps/defaults/main.yml`. Renovate opens the bump PR for this
   pin when a new tag lands in GHCR; nothing in this repo does that
   automatically.
5. **Deploy.** A deploy is a separate, manual step in the homelab repo: `make
   deploy TARGET=01-myapps-vm MODE=config`. `MODE` is mandatory there; there is
   no staging tier and no review gate between a local deploy invocation and
   production ([environments.md](environments.md)).

Changing code in this repo ships nothing until a tag is cut **and** the homelab
repo bumps and deploys. See `homelabDeploy` in
[model/people-systems.dsl](../model/people-systems.dsl) and the **Delivery**
view for the full chain outside a run.

## Schedule and locking model

Scheduling itself — which systemd timer fires which mode, and when — is
homelab-repo configuration, not this repo's. This repo only defines what each
run mode does (`python -m digest
[daily|weekly|patreon|positions|relay|hide:<channel>]`, see the
[README](../../../README.md)). What is confirmed:

- Nine systemd timers exist on the VM: `digest` (the ~6-hourly window run),
  `digest-daily`, `digest-weekly`, `digest-evening`, `digest-overnight`,
  `digest-patreon`, `digest-positions`, `digest-relay` and `digest-backup`. The
  README documents that the 6-hourly window is actually split across three
  separate timer/flag combinations (plain 06/12 UTC, a 00 UTC `hide:telegram`
  run, and an 18 UTC `hide:telegram,site` run); the exact timer-unit-to-flag
  mapping (presumably `digest`, `digest-overnight` and `digest-evening`
  respectively) is homelab-side and not verifiable from this repository.
- Every one of those units runs through a single host wrapper,
  `/usr/local/sbin/digest-run`, which takes an `flock`. This repo's own code
  comments confirm the wrapper's purpose and name it directly —
  `digest/relay.py`'s design notes for the `relay` mode state that its timer
  "must go through the homelab `digest-run` flock like every other unit, or it
  reintroduces the concurrent-session `AuthKeyDuplicatedError` that flock was
  added to stop" (PLAN.md §4.22). The wrapper's own script is not part of this
  repository, so its exact locking semantics (blocking wait vs. skip-if-held,
  timeout) are **not verified here**.
- The reason the lock exists: every digest unit shares one Telegram user session
  (`TG_SESSION`, a single `StringSession`). Telethon does not tolerate two
  concurrent connections on one session — a prior `Persistent=true` boot-storm
  caused exactly that (`AuthKeyDuplicatedError`). The flock guarantees at most
  one digest process touches that session at a time, across all nine timers.
- Inside a single run, the runner itself has no internal concurrency to
  serialize: it is one process, collectors run sequentially, and the run ends
  with exactly one exit code
  ([observability-architecture.md](../observability/observability-architecture.md)).

## Container shape

- `Dockerfile` builds a non-root (`uid 1000`) image with Python 3.14,
  `uv`-managed dependencies, Node.js + the Claude Code CLI (pinned to the same
  version the homelab repo pins as `claude_cli_version` for its other
  Claude-based services — the two are bumped together, deliberately, not
  independently).
- `ENTRYPOINT ["python", "-m", "digest"]` — the container's whole job is one
  invocation; there is no server process and no exposed port.
- `compose.yml` in this repo is explicitly local-dev-only (its own header
  comment says so); the production compose service definition lives in the
  homelab repo and is never built from here.
