## Glossary

Terms used across this repository with a specific meaning. Where a word is
overloaded elsewhere, the row says what it does **not** mean here.

| Term | Meaning here |
|---|---|
| **Run** | One execution of the one-shot container: collect, summarize, deliver, exit. The unit of work and the unit of failure |
| **Window** | The period a digest covers, closing when its run starts |
| **Lane** | A run mode with its own schedule and its own delivery target. `patreon`, `positions` and `relay` are lanes that sit outside the main digest cascade |
| **Cursor** | The last-seen item identifier for one source. Read before fetching, advanced only after items are recorded. The mechanism behind the idempotency guarantee |
| **Relay** | The lane that forwards public-channel posts verbatim into a hub topic. No summarization, no model call, no state beyond a cursor ([ADR 5](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0005-relay-forwards-verbatim.md)) |
| **Positions** | The tracked-project lane. Silent by design when nothing material happened — its prompt has an explicit NO-SIGNAL contract |
| **NO-SIGNAL** | The positions lane's contract for "nothing worth sending". It produces silence, not an empty message |
| **Arc context** | A short background primer attached to a running story so a digest entry makes sense without its history |
| **Provenance** | The record of which model produced a given digest, written alongside it because the fallback chain means it is not always the same one |
| **state.db** | The SQLite file. The system of record. In production it is kept as a bundle in private Azure Blob Storage, restored to local disk by each run and checkpointed back |
| **Lease** | The Blob lease on the state manifest. Each run holds it for its whole duration, so two runs never overlap or share the Telegram session. It replaced the VM's host lock ([ADR 7](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0007-run-digest-as-azure-jobs.md)) |
| **D1** | Cloudflare's database behind the news site. A rendering copy with the same table names as `state.db`, and disposable ([ADR 3](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0003-sqlite-is-the-source-of-truth.md)) |
| **TL;DR ping** | The short Telegram message announcing a digest, with deep links. One of the two live delivery channels |
| **Pin** | The exact image version the deployment runs. Since the Azure cutover (2026-10-08) it is recorded in this repository's `infra/azure/image.auto.tfvars.json`; before that it was recorded in the separate `homelab` repository. Deployment changes that value; it is never `latest` ([ADR 6](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0006-release-by-tag-and-pin.md)) |
