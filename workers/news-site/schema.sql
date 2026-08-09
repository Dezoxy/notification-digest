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
  kind          TEXT NOT NULL DEFAULT 'window',
  -- Optional ingest v2 fields (roadmap 2 step 8), app-produced. NULL when the
  -- digest app didn't report them (older app version, or genuinely nothing to
  -- report). Stored as JSON strings — this Worker never parses their shape at
  -- write time beyond the ingest validation in worker.js, only at render
  -- time. source_counts: JSON object of source name -> item count, used for
  -- the index's per-entry source-spectrum micro-bar. failed_sources: JSON
  -- array of source names that failed to collect this run, used for the
  -- degraded-run badge; normalized to NULL rather than "[]" when empty (see
  -- the ingest validation for why that distinction isn't worth storing).
  source_counts  TEXT,
  failed_sources TEXT
);
CREATE INDEX IF NOT EXISTS idx_digests_created_at ON digests(created_at DESC);
