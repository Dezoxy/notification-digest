# 14. Checkpoint changed chunks instead of full bundles

Date: 2026-11-26

## Status

Accepted

Amends [7. Run digest as Azure jobs](0007-run-digest-as-azure-jobs.md): the
state is still checkpointed under the lease and published by a conditional
manifest update, but a checkpoint no longer has to upload the whole state.

Decided on 2026-11-26. The release that reads and writes the new format ships
with `DIGEST_CLOUD_STATE_FORMAT=bundle`, so it changes no behaviour on its own.
Switching the jobs to `objects` is a separate, later infrastructure change; until
it is applied, production still writes full bundles.

## Context

Every changed checkpoint uploaded the complete state as one gzip
`bundles/<uuid>.tar.gz` (the SQLite snapshot, the cookie jars and the Markdown
archive). Measured over seven days in November 2026:

- 144 full-bundle uploads a day, about 4.6 GB a day, each about 32 MB.
- Inside a bundle, `state.db` is 71.7 MB raw, 87% of the bundle. The archive
  (679 files) is 9.6 MB raw, 13%.
- Building a bundle takes about 3.6 s and uploading it about 0.6 s per
  checkpoint. The `positions` job spends about 55% of its runtime checkpointing.
- Two consecutive checkpoints differed in 2 of the 69 1-MiB chunks of
  `state.db`: 0.74 MB instead of 32.4 MB. Day over day, 64 of 69 chunks differ.

Almost all of that traffic re-uploads bytes the previous generation already
holds. It also drove the retained-bytes cost: deleted bundles reached about
60 GB before the soft-delete window was cut (see the cost section of the
[migration runbook](../../azure-migration.md#alerts-cost-and-residency)).

## Decision drivers

- Keep the every-commit recovery boundary: a checkpoint after each committed
  step ([QA-01](../requirements/quality-attributes.md), no duplicate delivery).
- Keep the lease and fencing model of ADR 7: one mutable manifest, everything
  else immutable and only authoritative after the conditional manifest update.
- Fail closed on any hash, size or integrity mismatch, as before.
- No new service or database ([C-01](../requirements/constraints.md)); one
  storage account, the existing identity and roles.
- The independent daily copy and the restore runbook must keep working
  unchanged.
- Reversible: a rollout step must have a way back.

## Considered options

1. **Keep full bundles.** Simple, proven, and the cost is real but small in
   absolute terms. Rejected: about 98% of the uploaded bytes are unchanged, and
   it is the main source of checkpoint time and retained bytes.
2. **Split only the archive out of the bundle.** The archive is at most 13% of
   a bundle, so the saving is capped at that figure and `state.db` is still
   uploaded whole every time. Rejected.
3. **WAL shipping (Litestream style).** Needs control over SQLite
   checkpointing and validation of WAL frames; its generation model does not
   compose with our lease and fencing; and restore time grows with the number
   of segments. Rejected as more machinery than the problem earns.
4. **Checkpoint less often.** Cheap, but it weakens the every-commit recovery
   boundary that exists to prevent duplicate delivery. Rejected.
5. **Content-addressed chunks behind a small immutable index.** Chosen.

## Decision

Add a second generation format, selected by `DIGEST_CLOUD_STATE_FORMAT`
(`bundle`, the default, or `objects`). The setting decides only what **new**
checkpoints write. This release reads both formats always.

- **Layout.** Per checkpoint, an immutable `snapshots/<uuid>.json` index lists
  two streams: the SQLite `backup()` snapshot (`state.db`) and a deterministic
  uncompressed tar of the cookie jars and the archive (`aux.tar`: sorted
  members, fixed ownership and mode, exact modification times). Each stream is
  cut into fixed 1 MiB chunks, stored once as `objects/<sha256 of the raw
  chunk>`, zlib-compressed.
- **Upload.** Only chunks not listed by a currently published generation are
  uploaded. A blob that merely exists is not trusted: an existing object
  (HTTP 409) is accepted only after it is downloaded and its content hash
  verified, and accepting it changes its etag so that an earlier prune that
  listed it as an orphan cannot delete it any more. An existing object whose
  content does not match its name is deleted and rewritten from the local
  bytes under the lease, with a warning; otherwise every later checkpoint would
  fail on it. A checkpoint refuses state larger than the daily export can
  carry (512 MiB), so every generation stays exportable.
- **Manifest.** Still the only mutable blob (lease and etag). It is written as
  schema 1 while every reference is a legacy bundle, and as schema 2, with each
  reference typed `kind: bundle|snapshot`, as soon as any reference is a
  snapshot. Retention is unchanged: current, previous and up to seven dailies.
- **Restore.** Read the index, download the chunks in parallel (8 workers),
  verify every chunk and both streams by hash, run SQLite `quick_check`,
  extract the aux tar with the same allow-list as a bundle, then replace the
  local files. Fail closed as before.
- **Cleanup.** The per-checkpoint cleanup deletes only dropped legacy bundles
  and snapshot index blobs. Chunk objects are shared between generations and
  are removed only by the daily `prune()` in the backup job, which keeps every
  object any retained generation's index lists and deletes nothing if any
  referenced index cannot be read. Each delete is conditional on the etag
  seen when listing, so an object a live writer has reused since is skipped.
  The backup job copies to the backup account before it prunes, so a prune
  failure never costs the independent copy.
- **The daily copy is unchanged.** `export()` reassembles a snapshot generation
  into the same self-contained `bundle.tar.gz` plus `manifest.json`. The
  exported manifest's `bundle` describes the produced file and carries a
  `source` reference to the generation it came from. `bootstrap_export` and the
  restore runbook work as before, and the backup account stays in the legacy
  format.
- **Rollout.** Deploy this release with the default (`bundle`). A separate
  infrastructure change then sets `DIGEST_CLOUD_STATE_FORMAT=objects` on the
  jobs; as a non-image change it goes through the manual `azure-deploy`
  plan/apply. Rollback is setting it back to `bundle`, which still reads
  snapshots.

Design choices inside the format:

- **1 MiB chunks.** Small enough that a few-row change dirties few chunks
  (2 of 69 between consecutive checkpoints), large enough that a 72 MB database
  is about 70 objects rather than thousands. The size was not tuned further.
- **Two streams.** Keeping the archive out of the `state.db` stream means an
  archive file added or removed never shifts the database's chunk boundaries.

## Consequences

Positive:

- A checkpoint uploads the changed chunks and a small index instead of about
  32 MB: 0.74 MB in the measured pair. Daily upload volume and the
  build-and-upload time of every job fall accordingly, most visibly in
  `positions`.
- Fewer retained bytes: unchanged chunks are stored once across generations.
- The lease, fencing, retention window, fail-closed verification and the
  independent daily copy are unchanged. Rollback of the setting is one
  variable.

Negative / accepted trade-offs:

- **More blob operations.** A checkpoint is a handful of object writes, an
  index write and the manifest update, and a restore downloads about 70 objects
  instead of one blob. Cost is per operation as well as per byte; it is
  expected to be small, but it is not measured yet.
- **Image rollback limit.** An image older than this release cannot read a
  schema-2 manifest and fails closed ("Unsupported state manifest"). Once
  `objects` has written a snapshot, never roll the image back past this release
  without first returning to a bundle-only manifest: set the format back to
  `bundle` and wait until the last snapshot daily ages out (up to seven days),
  after which the manifest is schema 1 again. The exported backups stay in the
  legacy format throughout, as the fallback of last resort.
- **Chunk objects outlive checkpoints.** They are reclaimed only by the daily
  prune, so storage briefly holds chunks no retained generation needs.
  A prune that cannot read a referenced index deletes nothing and leaves
  the garbage for the next day.
- **Restore is more steps than one download**, and a missing or corrupt chunk
  makes a generation unusable. The hash check turns that into a loud failure
  rather than a wrong restore, and the daily copy is an independent fallback.
- **Two formats to maintain** for as long as legacy bundles can still be
  referenced or restored.

## Risks

- The image-rollback limit above is the main operational risk. It has a
  bounded exit (back to `bundle`, then at most seven days), so no entry is
  tracked in [risks/architecture-risks.md](../risks/architecture-risks.md); the
  runbook records the procedure.

## Related

- Requirements: [QA-01](../requirements/quality-attributes.md),
  [C-01](../requirements/constraints.md)
- Architecture views: none change; the State Database container is the same
  element, only its stored form differs.
- Other ADRs: amends
  [7. Run digest as Azure jobs](0007-run-digest-as-azure-jobs.md); the storage
  account layout of
  [8. Isolate digest Azure state and secrets](0008-isolate-digest-azure-state-and-secrets.md)
  is unchanged.
- Source: `digest/cloud_state.py`, `digest/config.py`;
  [migration runbook](../../azure-migration.md#state-formats-and-rollout)
