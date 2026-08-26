-- Adds the optional source_counts/failed_sources columns to an EXISTING
-- deployed digests table (ingest v2, roadmap 2 step 8). Fresh installs don't
-- need this file — schema.sql already includes these columns for a brand-new
-- database.
--
-- Apply to the deployed D1 database with:
--   wrangler d1 execute news-digests --remote --file migrations/0004-source-counts.sql
--
-- Both are nullable and default to NULL, so existing rows (all pre-v2
-- payloads) come through unaffected, and the current app version's ingest
-- payload (no source_counts/failed_sources fields) keeps working unchanged.
ALTER TABLE digests ADD COLUMN source_counts TEXT;
ALTER TABLE digests ADD COLUMN failed_sources TEXT;
