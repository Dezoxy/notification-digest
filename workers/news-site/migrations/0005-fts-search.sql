-- Adds full-text search over digests to an EXISTING deployed database
-- (roadmap 4 step 7 — "D1 FTS5 search"). Fresh installs don't need this
-- file — schema.sql already includes the same virtual table and triggers
-- for a brand-new database.
--
-- Apply to the deployed D1 database with:
--   wrangler d1 execute news-digests --remote --file migrations/0005-fts-search.sql
--
-- digests_fts is an EXTERNAL CONTENT fts5 table: content='digests' means it
-- indexes tldr/body_md/tldr_hu/body_md_hu without storing a second copy of
-- that text, keeping the row already living in `digests` as the single
-- source of truth. remove_diacritics 2 folds accents at index AND query
-- time, so a diacritic-less query ("kulcsfontossagu") still matches
-- accented Hungarian content ("kulcsfontosságú") — the owner's own
-- keyboard/habits shouldn't gate whether a search hits.
CREATE VIRTUAL TABLE digests_fts USING fts5(
  tldr,
  body_md,
  tldr_hu,
  body_md_hu,
  content='digests',
  content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);

-- An external-content fts5 table is NOT kept in sync automatically — SQLite
-- only owns the index, not the text, so every write to `digests` needs an
-- explicit trigger to tell fts5 what changed. Inserts are a plain mirrored
-- INSERT; deletes and updates use fts5's special 'delete' command form —
-- INSERT INTO digests_fts(digests_fts, rowid, <cols>) VALUES('delete', ...)
-- — which is how fts5 external-content tables receive delete/update
-- notifications (a bare DELETE/UPDATE against the virtual table is not
-- supported for content-linked columns the way it is for a normal table).
-- An update is a delete-then-insert pair, in that order, inside the same
-- trigger.
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

-- One-time backfill: every row that already exists in `digests` predates
-- the triggers above and has no entry in digests_fts yet. The 'rebuild'
-- command repopulates the whole index from the current content table in
-- one pass — the standard fts5 external-content bootstrap, safe to run
-- once here since digests_fts was just created empty.
INSERT INTO digests_fts(digests_fts) VALUES('rebuild');
