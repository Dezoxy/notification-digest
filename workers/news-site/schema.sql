CREATE TABLE IF NOT EXISTS digests (
  id            INTEGER PRIMARY KEY,
  created_at    TEXT NOT NULL,
  tldr          TEXT NOT NULL,
  item_count    INTEGER NOT NULL,
  section_count INTEGER NOT NULL,
  has_attention INTEGER NOT NULL DEFAULT 0,
  body_html     TEXT NOT NULL,
  body_md       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digests_created_at ON digests(created_at DESC);
