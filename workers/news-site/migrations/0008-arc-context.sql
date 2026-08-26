-- Adds the arc_context table to an EXISTING deployed database (PLAN.md
-- §11.6 context mode). Fresh installs don't need this file -- schema.sql
-- already includes this table for a brand-new database.
--
-- Apply to the deployed D1 database with:
--   wrangler d1 execute news-digests --remote --file migrations/0008-arc-context.sql
--
-- A NEW TABLE, not a new column on `digests` -- a primer is per-ARC identity
-- (key/slug, see ARC_IDENTITY_SQL in worker.js), not per-digest: one arc can
-- span hundreds of digest rows, and a primer describes the ongoing story,
-- not any single briefing. See schema.sql's own comment on arc_context for
-- the full contract (ON CONFLICT upsert semantics, untrusted-markdown
-- handling, why updated_at is real ingest time rather than the digest's own
-- created_at).
--
-- digests_fts (0005-fts-search.sql) is untouched here -- arc_context is a
-- separate table with no FTS index of its own; primers are not part of
-- full-text search.
CREATE TABLE IF NOT EXISTS arc_context (
  key        TEXT PRIMARY KEY,
  context_md TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
