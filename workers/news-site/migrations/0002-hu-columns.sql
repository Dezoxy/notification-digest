-- Adds the optional Hungarian-translation columns to an EXISTING deployed
-- digests table. Fresh installs don't need this file — schema.sql already
-- includes these columns for a brand-new database.
--
-- Apply to the deployed D1 database with:
--   wrangler d1 execute news-digests --remote --file migrations/0002-hu-columns.sql
--
-- All three are nullable and default to NULL, so existing rows (all
-- English-only so far) come through unaffected, and the current app
-- version's ingest payload (no hu fields) keeps working unchanged.
ALTER TABLE digests ADD COLUMN tldr_hu TEXT;
ALTER TABLE digests ADD COLUMN body_html_hu TEXT;
ALTER TABLE digests ADD COLUMN body_md_hu TEXT;
