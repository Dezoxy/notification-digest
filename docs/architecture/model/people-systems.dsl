// People and software systems outside the system in scope.
// The system in scope (Notification Digest) is defined in containers.dsl.
//
// Runtime dependencies and delivery tooling are deliberately kept apart: the
// runtime views answer "what does one digest run touch", the Delivery view
// answers "how does a change reach the jobs". Both are tagged "External" so the
// shared palette keeps its meaning; the separation is made by the views.

owner = person "Owner" "Single owner, operator and only reader. Reads the digest in Telegram and on the news site."

// ── Collection sources ───────────────────────────────────────────────────────
// Telegram appears once, deliberately: it is both a source (MTProto user
// session) and a delivery channel (Bot API). Two boxes would mean two
// identities for one real system.
telegram = softwareSystem "Telegram" "Hosts the owner's own groups and channels, and the bot that delivers the digest." "External"
x = softwareSystem "X" "Hosts the owner's notifications timeline. Unofficial API, cookie session." "External"
reddit = softwareSystem "Reddit" "Serves top-of-day posts for the configured subreddits. Cookie session." "External"
patreon = softwareSystem "Patreon" "Hosts paid-tier posts of one campaign. Cookie session." "External"
polymarket = softwareSystem "Polymarket" "Publishes prediction-market prices used for swing detection." "External"
hackerNews = softwareSystem "Hacker News" "Publishes the front page through the public Algolia API." "External"
newsFeeds = softwareSystem "News Feeds" "Third-party RSS and Atom feeds followed for general news." "External"

// ── Summarization ────────────────────────────────────────────────────────────
// One box for one real system. The runner reaches Claude two ways: the headless
// CLI on the owner's subscription, and the API when that call fails (ADR 9).
anthropic = softwareSystem "Claude" "Summarizes a window of collected items. Reached through the headless CLI, and through the API when that call fails." "External"
entra = softwareSystem "Microsoft Entra ID" "Issues the short-lived token the runner presents to the Claude API. The runner's managed identity is the only credential." "External"

// ── Delivery channels ────────────────────────────────────────────────────────
smtpRelay = softwareSystem "SMTP Relay" "Accepts the digest as e-mail. Implemented and still the application default, but disabled in production." "External"

// ── Delivery tooling (how a change reaches the jobs, not part of a run) ──────
githubActions = softwareSystem "GitHub Actions" "Tests each merge, releases and publishes the runtime image, and applies the Azure deployment." "External"
imageRegistry = softwareSystem "GHCR" "Stores the versioned runtime image. Private; pulled with a read-only token." "External"
renovate = softwareSystem "Renovate" "Moves the pinned image version to each new release through a pull request." "External"
keyVault = softwareSystem "Azure Key Vault" "Holds every runtime secret. The jobs reference them; no value is stored in the deployment." "External"

// ── Relationships that do not involve the system in scope ────────────────────
owner -> telegram "Reads the delivered digest in" "Telegram app" "Person"
owner -> githubActions "Merges a change that is released by" "Git, pull request" "Person"
githubActions -> imageRegistry "Publishes the versioned runtime image to" "OCI over HTTPS"
renovate -> imageRegistry "Detects each new image release in" "OCI over HTTPS"
renovate -> githubActions "Merges the image pin bump that starts a deployment in" "Git, pull request"
