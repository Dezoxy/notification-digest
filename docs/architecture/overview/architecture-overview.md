# Architecture Overview

## Purpose

notification-digest reads the places one person already has to read, and turns
them into something worth reading once. Every few hours it collects what is new
across the owner's own Telegram groups, X notifications and a handful of public
feeds, summarizes each window with a language model, and delivers a digest with
deep links back to the originals.

It exists to remove an obligation, not to serve users. There is exactly one
owner, who is also the only operator and the only reader ([C-01](../requirements/constraints.md)).
Almost every structural choice below follows from that single fact.

## Context

The system sits between accounts the owner already holds and the two places the
owner actually reads. Collection is always outbound: nothing on the home network
accepts an inbound connection from a source.

![Context view: who reads the digest, what writes it, and where it comes out](embed:Context)

Collection is deliberately kept out of that picture — seven sources would bury
the question it answers. They have their own view, `Sources`, registered in
[the view register](../README.md#view-register).

## Building blocks

| Container | Responsibility | Technology |
|---|---|---|
| Digest Runner | Collects new items per source, summarizes the window, delivers the result. One short-lived process per scheduled run | Python 3.12, one-shot Docker container |
| State Database | System of record: items, per-source cursors, digests, delivery state | SQLite on local ext4 |
| News Site | Serves the public digest archive; accepts authenticated ingest from the runner | Cloudflare Worker, JavaScript |
| Site Database | Rendering copy of published digests. Disposable | Cloudflare D1 |

![Containers view: the building blocks and the trust boundary between them](embed:Containers)

The runner is not a service. It starts on a timer, does one job and exits, so
every arrow leaving it lives inside a run that has a beginning and an end. That
is why there is no queue, no scheduler process and no health endpoint to
monitor: the unit of work is the process itself.

## How one run works

![Runtime view: what happens during one scheduled digest run](embed:DigestRun)

The cursor is read before anything is fetched and only advances once items are
recorded. A re-run over the same window therefore collects nothing new and
delivers nothing twice — the system's one absolute guarantee
([QA-01](../requirements/quality-attributes.md)).

## The shape that follows from one owner

- **No high availability.** One Proxmox node holds the runner, its state and the
  primary backup ([C-05](../requirements/constraints.md)). A missed run is
  tolerable; a duplicate delivery is not.
- **Personal credentials, not service credentials.** The owner's own Telegram
  and X sessions are what make the owner's own groups readable
  ([C-02](../requirements/constraints.md)). This is the system's sharpest
  security property, and it is covered in
  [trust boundaries](../security/trust-boundaries.md).
- **Two databases that are not peers.** The VM's SQLite file is the system of
  record; Cloudflare D1 is a rendering copy that may be thrown away
  ([ADR 3](../decisions/0003-sqlite-is-the-source-of-truth.md)). They share
  table names, which is exactly why the distinction is written down.
- **Releases are deliberate.** A merge here ships nothing. A release is a tag,
  an image, a pinned version in a separate repository and a deploy
  ([ADR 6](../decisions/0006-release-by-tag-and-pin.md)).

## Where to read next

[The architecture README](../README.md) holds the view register and the document
index, including reading paths for an operator and for a reviewer.
