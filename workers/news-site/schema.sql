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
  -- Distinguishes the once-daily 20:00 synthesis ("daily") and the once-a-
  -- week Sunday-evening synthesis of the week's daily briefs ("weekly") from
  -- the regular 3-hourly window digest ("window", the default). Same ingest
  -- path, same table — see the ingest validation in worker.js for the
  -- allowed values.
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
  failed_sources TEXT,
  -- Optional ingest v3 field (roadmap 4 step 8), app-produced. NULL when the
  -- digest app didn't send topics for this digest (older app version, or
  -- genuinely no topics to report). JSON array of {slug,label} objects — see
  -- validateTopics in worker.js for the shape rules, and renderArcs for how
  -- it turns into the digest page's story-arc line.
  topics TEXT,
  -- Optional ingest v4 field (PLAN.md §11.3 delta persistence), app-produced.
  -- NULL when the digest app didn't send deltas for this digest (older app
  -- version, or genuinely no repeat-story deltas this window — the common
  -- case even on a current app version). JSON array of {slug,previously,now}
  -- objects, keyed on the SAME topic-slug vocabulary `topics` above already
  -- establishes — see validateDeltas in worker.js for the shape rules, and
  -- renderDeltas/renderArcAppearance for how it turns into the digest page's
  -- "What changed" block and the arc page's per-appearance previously/now
  -- line.
  deltas TEXT
);
CREATE INDEX IF NOT EXISTS idx_digests_created_at ON digests(created_at DESC);

-- Full-text search (roadmap 4 step 7) — see migrations/0005-fts-search.sql
-- for the external-content/trigger rationale; identical here, just against
-- an empty table, so a fresh install needs no 'rebuild' backfill.
CREATE VIRTUAL TABLE digests_fts USING fts5(
  tldr,
  body_md,
  tldr_hu,
  body_md_hu,
  content='digests',
  content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER digests_fts_ai AFTER INSERT ON digests BEGIN
  INSERT INTO digests_fts(rowid, tldr, body_md, tldr_hu, body_md_hu)
  VALUES (new.id, new.tldr, new.body_md, new.tldr_hu, new.body_md_hu);
END;

CREATE TRIGGER digests_fts_ad AFTER DELETE ON digests BEGIN
  INSERT INTO digests_fts(digests_fts, rowid, tldr, body_md, tldr_hu, body_md_hu)
  VALUES ('delete', old.id, old.tldr, old.body_md, old.tldr_hu, old.body_md_hu);
END;

CREATE TRIGGER digests_fts_au AFTER UPDATE ON digests BEGIN
  INSERT INTO digests_fts(digests_fts, rowid, tldr, body_md, tldr_hu, body_md_hu)
  VALUES ('delete', old.id, old.tldr, old.body_md, old.tldr_hu, old.body_md_hu);
  INSERT INTO digests_fts(rowid, tldr, body_md, tldr_hu, body_md_hu)
  VALUES (new.id, new.tldr, new.body_md, new.tldr_hu, new.body_md_hu);
END;
