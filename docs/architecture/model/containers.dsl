// The system in scope and its containers.
// Groups mark trust boundaries so the Security view can show them.
//
// Four containers, two trust zones. The runner is one process per scheduled
// run, not a long-lived service: every arrow leaving it happens inside a run
// that starts on a timer and exits.

notificationDigest = softwareSystem "Notification Digest" "Collects the owner's notifications on a schedule, summarizes each window with an LLM and delivers a digest with deep links." {

    group "Azure subscription (private)" {
        runner = container "Digest Runner" "Collects new items per source, summarizes the window and delivers the result. One short-lived process per scheduled run." "Python 3.14, Azure Container Apps job" "Layer Run"
        state = container "State Database" "System of record: collected items, per-source cursors, digests and delivery state. Keeps re-runs idempotent. One lease admits one run at a time." "SQLite state in Azure Blob Storage" "Layer Data,Database"
    }

    group "Cloudflare edge (internet-facing)" {
        site = container "News Site" "Serves the digest archive to the owner, a read-only JSON API and MCP endpoint for daily editions, and authenticated ingest from the runner." "Cloudflare Worker, JavaScript" "Layer Deliver,Internet-exposed"
        siteDb = container "Site Database" "Rendering copy of published digests, serving the site. Disposable: rebuilt from the runner." "Cloudflare D1" "Layer Data,Database"
    }
}

// ── Collection (runner pulls; every source is outbound from the job) ─────────
// Polymarket is reached only through the egress proxy (ADR 10);
// the proxy-to-source arrows are in people-systems.dsl.
notificationDigest.runner -> telegram "Fetches new messages from the owner's groups from" "MTProto, user session" "Layer Run"
notificationDigest.runner -> x "Fetches the notifications timeline from" "HTTPS, cookie session" "Layer Run"
notificationDigest.runner -> patreon "Fetches paid-tier posts from" "HTTPS, cookie session" "Layer Run"
notificationDigest.runner -> egressProxies "Fetches Polymarket market prices through" "HTTPS, shared-key header" "Layer Run"
notificationDigest.runner -> hackerNews "Fetches the front page from" "HTTPS/JSON" "Layer Run"
notificationDigest.runner -> newsFeeds "Fetches feed entries from" "HTTPS, RSS/Atom" "Layer Run"

// ── State ────────────────────────────────────────────────────────────────────
notificationDigest.runner -> notificationDigest.state "Restores, reads cursors from and checkpoints items, digests and delivery state to" "SQLite state, Blob lease" "Layer Run"

// ── Summarization, with the fallback chain ──────────────────────────────────
notificationDigest.runner -> anthropic "Summarizes the window with" "Claude CLI, headless subprocess" "Layer Run"
notificationDigest.runner -> anthropic "Retries a failed summarization through" "HTTPS/JSON, Claude API, federated token" "Layer Run"
notificationDigest.runner -> entra "Trades its managed identity for a short-lived app token at" "HTTPS, OAuth 2.0 client assertion" "Layer Run"

// ── Delivery ─────────────────────────────────────────────────────────────────
notificationDigest.runner -> notificationDigest.site "Publishes the rendered digest to" "HTTPS/JSON, shared-secret ingest" "Layer Run"
notificationDigest.runner -> telegram "Sends the digest summary and deep links through" "HTTPS, Telegram Bot API" "Layer Run"
notificationDigest.runner -> smtpRelay "Sends the digest as e-mail through" "SMTP over TLS" "Layer Run"

// ── Site ─────────────────────────────────────────────────────────────────────
notificationDigest.site -> notificationDigest.siteDb "Reads published digests from and applies ingest to" "D1 binding" "Layer Deliver"
owner -> notificationDigest.site "Browses the digest archive on" "HTTPS, web browser" "Person"
chatgpt -> notificationDigest.site "Reads daily editions from" "HTTPS, MCP (JSON-RPC), path capability token"

// ── Secrets and delivery tooling ─────────────────────────────────────────────
notificationDigest.runner -> keyVault "Resolves its runtime secrets from" "Key Vault references, managed identity" "Layer Run"
notificationDigest.runner -> imageRegistry "Is started from the pinned image in" "OCI over HTTPS, read-only token"
githubActions -> notificationDigest.runner "Applies the pinned image, schedules and settings of" "Terraform, Azure OIDC"
