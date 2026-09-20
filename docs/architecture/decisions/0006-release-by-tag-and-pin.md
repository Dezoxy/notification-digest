# 6. Release by git tag, deploy by pin bump from a separate repository

Date: 2026-09-20

## Status

Accepted

## Context

This repository is application source only — deployment (the Ansible
`myapps` role, the systemd timers, secrets wiring) lives in a separate
`homelab` repository ([C-07](../requirements/constraints.md)). A merge to
`main` here ships nothing by itself: `.github/workflows/release.yml`
triggers only on a pushed `v*` tag, builds the image, and pushes it to
`ghcr.io/dezoxy/notification-digest` with `docker/metadata-action`'s
`type=semver,pattern={{version}}` — a tag `v0.26.4` becomes image tag
`0.26.4`, with no leading `v`. `flavor: latest=false` in that same step means
there is deliberately no floating `latest` tag (the workflow's own comment:
"the homelab repo pins an exact version ..., so there's nothing that would
ever consume it"). The Dockerfile itself documents that it is "not built on
the VM: this image is only ever built by `.github/workflows/release.yml`."

## Decision drivers

- [C-07](../requirements/constraints.md): this repository cannot deploy
  itself; deployment lives in the separate homelab repository.
- [C-06](../requirements/constraints.md): no secret may exist in git, in a
  log, or in an image — a versioned, reproducible image build separates
  "what code shipped" from "what secrets a running container has," which
  are injected only at deploy time by the homelab repo.
- [C-01](../requirements/constraints.md): a single owner/operator; a
  two-repo, tag-then-pin flow trades a manual coordination step for a
  reviewable, reproducible deployment history, which is an acceptable cost
  at this scale.

## Considered options

1. Build and push the image on every merge to `main` (CI/CD on push), with a
   floating `latest` tag the VM pulls automatically.
2. Build the image directly on the VM from a checkout of this repository.
3. Build and push an exact-version image only on an explicit `v*` tag push,
   with no floating tag; a separate repository pins the exact version and
   performs the actual deploy.

## Decision

Release is a git tag (`v*`) push, handled entirely by
`.github/workflows/release.yml`: checkout, log in to GHCR, extract metadata
via `docker/metadata-action` with `type=semver,pattern={{version}}` and
`flavor: latest=false`, then `docker/build-push-action` builds and pushes
`ghcr.io/dezoxy/notification-digest:<version>` (no `v` prefix, no `latest`).
Deploy is a separate act in the homelab repository: it pins the exact
version, and Renovate opens the bump PR there (per `CLAUDE.md`'s deploy
note). Building on the VM (option 2) is explicitly rejected — the
Dockerfile's own header states the image is only ever built by this
workflow. Changing code in this repository, merging to `main`, or even
tagging without a homelab-side pin bump and deploy ships nothing to
production.

## Consequences

Positive:

- The deployed image tag is always an exact, reproducible version — there
  is never ambiguity about "which commit is running" the way a floating
  `latest` tag would create.
- No image is ever built with the VM's own secrets or environment in scope,
  since the image build (GitHub Actions) and the secret injection (Ansible,
  at deploy time from Azure Key Vault) are fully separate steps in fully
  separate repositories.
- A bad release can be rolled back by re-pinning the homelab repo to a
  previous tag's image, without touching this repository at all.

Negative / accepted trade-offs:

- Every release requires a human (or a scripted follow-up) to notice a new
  tag was cut, open/merge the homelab pin-bump PR, and deploy — there is no
  automatic propagation from "tag pushed" to "running in production," by
  design ([C-07](../requirements/constraints.md)).
- A merge to `main` here that is never tagged is invisible to production
  indefinitely — there is no drift detection between what's merged and what
  version tag actually exists.
- The homelab repository's own pin file
  (`ansible/roles/myapps/defaults/main.yml`, per `CLAUDE.md`'s deploy note)
  is outside this repository and was not read for this ADR — its exact
  contents and Renovate wiring are not independently verified here; not
  recorded beyond what `CLAUDE.md` states.

## Risks

- Not recorded: `risks/architecture-risks.md` on this branch currently holds
  generic template content unrelated to this repository, so no digest-specific
  risk ID could be confirmed for a tag/deploy-lag scenario at the time of
  writing.

## Related

- Requirements: [C-06](../requirements/constraints.md),
  [C-07](../requirements/constraints.md), [C-01](../requirements/constraints.md)
- Architecture views: not recorded
- Other ADRs: none
