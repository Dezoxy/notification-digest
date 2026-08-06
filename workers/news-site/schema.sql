CREATE TABLE IF NOT EXISTS digests (
  id            INTEGER PRIMARY KEY,
  created_at    TEXT NOT NULL,
  tldr          TEXT NOT NULL,
  item_count    INTEGER NOT NULL,
  section_count INTEGER NOT NULL,
  has_attention INTEGER NOT NULL DEFAULT 0,
  body_html     TEXT NOT NULL,
  body_md       TEXT NOT NULL,
  -- Optional Hungarian translations, app-produced. NULL when the digest
  -- app didn't send a translation for this digest (English-only). All
  -- three are always NULL or all three populated together — see the
  -- ingest validation in worker.js.
  tldr_hu       TEXT,
  body_html_hu  TEXT,
  body_md_hu    TEXT,
  -- Distinguishes the once-daily 20:00 synthesis ("daily") from the regular
  -- 3-hourly window digest ("window", the default). Same ingest path, same
  -- table — see the ingest validation in worker.js for the allowed values.
  kind          TEXT NOT NULL DEFAULT 'window'
);
CREATE INDEX IF NOT EXISTS idx_digests_created_at ON digests(created_at DESC);
