# Notification Digest

## Overview

notification-digest reads the places one person already has to read, and turns
them into something worth reading once. Every few hours it collects what is new
across the owner's own Telegram groups, X notifications and a handful of public
feeds, summarizes each window with a language model, and delivers a digest with
deep links back to the originals.

It exists to remove an obligation, not to serve users. There is exactly one
owner, who is also the only operator and the only reader
([C-01](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/constraints.md)). Almost every structural choice below
follows from that single fact.

This document is the whole architecture description: the Documentation tab in
Structurizr and the architecture PDF are built from the same folder, so they
say the same thing. The registers they link to — requirements, security, data,
reliability, risks — stay in the repository as the detail behind each claim.

## Context

The system sits between accounts the owner already holds and the two places the
owner actually reads. Collection is always outbound: nothing the system runs
accepts an inbound connection from a source.

![Context view: who reads the digest, what writes it, and where it comes out](embed:Context)

Collection is deliberately kept out of that picture — seven sources would bury
the question it answers. They have their own view:

![Sources view: the accounts and feeds one run collects from](embed:Sources)

## Building blocks

| Container | Responsibility | Technology |
|---|---|---|
| Digest Runner | Collects new items per source, summarizes the window, delivers the result. One short-lived process per scheduled run | Python 3.14, one-shot container started by an Azure Container Apps job |
| State Database | System of record: items, per-source cursors, digests, delivery state | SQLite file, kept as a bundle in private Azure Blob Storage |
| News Site | Serves the public digest archive; accepts authenticated ingest from the runner | Cloudflare Worker, JavaScript |
| Site Database | Rendering copy of published digests. Disposable | Cloudflare D1 |

![Containers view: the building blocks and the trust boundary between them](embed:Containers)

The runner is not a service. It starts on a schedule, does one job and exits, so
every arrow leaving it lives inside a run that has a beginning and an end. That
is why there is no queue, no scheduler process and no health endpoint to
monitor: the unit of work is the process itself.

## How one run works

![Runtime view: what happens during one scheduled digest run](embed:DigestRun)

The cursor is read before anything is fetched and only advances once items are
recorded. A re-run over the same window therefore collects nothing new and
delivers nothing twice — the system's one absolute guarantee
([QA-01](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/quality-attributes.md)).

## The shape that follows from one owner

- **No high availability.** One Azure region holds the runner and its state; the
  recovery copy sits in a separate storage account in the same region
  ([C-05](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/constraints.md)). A missed run is
  tolerable; a duplicate delivery is not.
- **Personal credentials, not service credentials.** The owner's own Telegram
  and X sessions are what make the owner's own groups readable
  ([C-02](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/constraints.md)). This is the system's sharpest
  security property.
- **Two databases that are not peers.** The SQLite state is the system of
  record; Cloudflare D1 is a rendering copy that may be thrown away
  ([ADR 3](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0003-sqlite-is-the-source-of-truth.md)). They share
  table names, which is exactly why the distinction is written down.
- **Releases are traceable.** A merge to shipped code becomes a tag and an
  image, the image is pinned by a reviewed change in this repository, and only
  that pin may be applied without a manual plan
  ([ADR 6](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0006-release-by-tag-and-pin.md)).

## Reading paths

This document is complete on its own. The sections below are routes through it
for different readers; after them, every reference section they cite follows in
full, so nothing here depends on opening a link.

| Section | For |
|---|---|
| **Scope** | What this system covers, and what it deliberately does not |
| **For stakeholders** | What it is, what it produces, what it costs, what could go wrong. No protocols |
| **For the CTO** | Exposure, data, recovery, the risks accepted rather than mitigated, and cost |
| **For engineers** | The design, the runtime, the decisions, and what a change must satisfy |
| **For operators** | How a change reaches the jobs, what fails together, backups and recovery |
| **Glossary** | Terms used with a specific meaning here |

Then the reference sections, in order: the requirements and principles the
design answers to; security, data and integration; deployment, reliability and
observability; and finally the risks, the debt and the roadmap. Each is the
single authored copy of that material — the reading paths above cite them
rather than repeating them.
