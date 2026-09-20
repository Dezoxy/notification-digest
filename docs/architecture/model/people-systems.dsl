// People and software systems outside the system in scope.
// The system in scope (Notification Digest) is defined in containers.dsl.
//
// Runtime dependencies and delivery tooling are deliberately kept apart: the
// runtime views answer "what does one digest run touch", the Delivery view
// answers "how does a change reach the VM". Both are tagged "External" so the
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
anthropic = softwareSystem "Claude" "Summarizes a window of collected items. Primary summarizer, invoked headless." "External"
openRouter = softwareSystem "OpenRouter" "Serves fallback models when the primary summarizer call fails." "External"

// ── Delivery channels ────────────────────────────────────────────────────────
smtpRelay = softwareSystem "SMTP Relay" "Accepts the digest as e-mail. Implemented and still the role default, but disabled on the live host." "External"

// ── Delivery tooling (how a change reaches the VM, not part of a run) ────────
githubActions = softwareSystem "GitHub Actions" "Builds the runtime image from a version tag and publishes it." "External"
imageRegistry = softwareSystem "GHCR" "Stores the versioned runtime image. Private; pulled with a read-only token." "External"
homelabDeploy = softwareSystem "Homelab Deploy" "Ansible role that pins the image version and configures the host. Lives in a separate repository." "External"
keyVault = softwareSystem "Azure Key Vault" "Holds every runtime secret. Injected as environment variables at deploy time." "External"

// ── Relationships that do not involve the system in scope ────────────────────
owner -> telegram "Reads the delivered digest in" "Telegram app" "Person"
owner -> githubActions "Cuts a release by pushing a version tag to" "Git" "Person"
owner -> homelabDeploy "Bumps the pinned image version and deploys with" "Ansible" "Person"
githubActions -> imageRegistry "Publishes the versioned runtime image to" "OCI over HTTPS"
homelabDeploy -> imageRegistry "Pulls the pinned runtime image from" "OCI over HTTPS"
homelabDeploy -> keyVault "Fetches runtime secrets from" "HTTPS, Azure AD"
