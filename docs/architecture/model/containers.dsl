// The system in scope and its containers.
// Groups mark trust boundaries so the Security view can show them.
//
// Four containers, two trust zones. The runner is one process per scheduled
// run, not a long-lived service: every arrow leaving it happens inside a run
// that starts on a timer and exits.

notificationDigest = softwareSystem "Notification Digest" "Collects the owner's notifications on a schedule, summarizes each window with an LLM and delivers a digest with deep links." {

    group "Homelab VM (private)" {
        runner = container "Digest Runner" "Collects new items per source, summarizes the window and delivers the result. One short-lived process per scheduled run." "Python 3.12, one-shot Docker container" "Layer Run"
        state = container "State Database" "System of record: collected items, per-source cursors, digests and delivery state. Keeps re-runs idempotent." "SQLite file on local ext4" "Layer Data,Database"
    }

    group "Cloudflare edge (internet-facing)" {
        site = container "News Site" "Serves the public digest archive and accepts authenticated ingest from the runner." "Cloudflare Worker, JavaScript" "Layer Deliver,Internet-exposed"
        siteDb = container "Site Database" "Rendering copy of published digests, serving the site. Disposable: rebuilt from the runner." "Cloudflare D1" "Layer Data,Database"
    }
}

// ── Collection (runner pulls; every source is outbound from the VM) ──────────
notificationDigest.runner -> telegram "Fetches new messages from the owner's groups from" "MTProto, user session" "Layer Run"
notificationDigest.runner -> x "Fetches the notifications timeline from" "HTTPS, cookie session" "Layer Run"
notificationDigest.runner -> reddit "Fetches top-of-day posts from" "HTTPS, cookie session" "Layer Run"
notificationDigest.runner -> patreon "Fetches paid-tier posts from" "HTTPS, cookie session" "Layer Run"
notificationDigest.runner -> polymarket "Fetches market prices from" "HTTPS/JSON" "Layer Run"
notificationDigest.runner -> hackerNews "Fetches the front page from" "HTTPS/JSON" "Layer Run"
notificationDigest.runner -> newsFeeds "Fetches feed entries from" "HTTPS, RSS/Atom" "Layer Run"

// ── State ────────────────────────────────────────────────────────────────────
notificationDigest.runner -> notificationDigest.state "Reads cursors from and records items, digests and delivery state in" "SQLite, local file" "Layer Run"

// ── Summarization, with the fallback chain ──────────────────────────────────
notificationDigest.runner -> anthropic "Summarizes the window with" "Claude CLI, headless subprocess" "Layer Run"
notificationDigest.runner -> openRouter "Falls back to a secondary model at" "HTTPS/JSON" "Layer Run"

// ── Delivery ─────────────────────────────────────────────────────────────────
notificationDigest.runner -> notificationDigest.site "Publishes the rendered digest to" "HTTPS/JSON, shared-secret ingest" "Layer Run"
notificationDigest.runner -> telegram "Sends the digest summary and deep links through" "HTTPS, Telegram Bot API" "Layer Run"
notificationDigest.runner -> smtpRelay "Sends the digest as e-mail through" "SMTP over TLS" "Layer Run"

// ── Site ─────────────────────────────────────────────────────────────────────
notificationDigest.site -> notificationDigest.siteDb "Reads published digests from and applies ingest to" "D1 binding" "Layer Deliver"
owner -> notificationDigest.site "Browses the digest archive on" "HTTPS, web browser" "Person"

// ── Delivery tooling ─────────────────────────────────────────────────────────
homelabDeploy -> notificationDigest.runner "Installs the pinned image and its schedule on the host for" "Ansible over SSH"
