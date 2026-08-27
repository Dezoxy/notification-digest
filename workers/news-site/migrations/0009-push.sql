-- Adds the two Web Push tables to an EXISTING deployed database (PLAN.md
-- §11.7, PR B). Fresh installs don't need this file -- schema.sql already
-- includes both tables for a brand-new database.
--
-- Apply to the deployed D1 database with:
--   wrangler d1 execute news-digests --remote --file migrations/0009-push.sql
--
-- Apply this BEFORE merging, not after: this Worker deploys automatically on
-- merge to main (git-connected Cloudflare Workers Build). The code is written
-- to degrade to a no-op when push is unconfigured -- a missing VAPID key makes
-- subscribe return 503 and, from PR C, makes the fan-out skip silently -- so
-- merging first is survivable rather than breaking, but it means the feature
-- is dark until the tables exist.
--
-- Two tables, not one, because they answer two different questions and have
-- two different lifetimes. See schema.sql's comments on each for the full
-- contract.
CREATE TABLE IF NOT EXISTS push_subscriptions (
  endpoint   TEXT PRIMARY KEY,
  p256dh     TEXT NOT NULL,
  auth       TEXT NOT NULL,
  lang       TEXT NOT NULL DEFAULT 'en',
  label      TEXT,
  created_at TEXT NOT NULL,
  last_ok_at TEXT,
  fail_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS push_sent (
  digest_id INTEGER PRIMARY KEY,
  sent_at   TEXT NOT NULL
);
