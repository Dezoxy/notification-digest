## Data Ownership

One owner, one operator, one reader ([C-01](../requirements/constraints.md)):
there is no cross-team ownership question here in the usual sense. What this
document tracks instead is which store is authoritative for which dataset,
who can read it, whether the content is the owner's own or written by a
third party, and — the sharper question for a single-owner service — what
the owner can and cannot actually delete once it exists.

| Dataset | System of record | Who can read it | Third-party authored? | Can the owner delete it? |
|---|---|---|---|---|
| Collected items (Telegram/X/Reddit/Patreon/Polymarket/Hacker News/RSS content) | `state.db` on the VM | The owner, via direct VM/database access. Never exposed as raw items to any reader | Yes for Telegram/X/Reddit/Patreon (other people's messages and posts); no for Polymarket/Hacker News/RSS (already-public market data and articles) | Yes, by editing `state.db` directly — but the source content still exists at Telegram/X/Reddit/Patreon regardless, and a summary derived from it may already be published (see below) |
| Digest bodies (`body_md`/`body_html`, summaries and deep links) | `state.db` (authoritative); Cloudflare D1 (disposable copy) | Anyone holding the current `SITE_TOKEN` capability link, plus the owner via Telegram/e-mail | Derived — an LLM synthesis of third-party content, not a copy of it | Partially. The owner can edit or delete a `state.db` row and re-publish to overwrite D1's copy of it, and can rotate `SITE_TOKEN` to cut off every existing reader link at once. There is no way to un-publish a digest a reader has already fetched, cached, or forwarded |
| Relay-forwarded posts (`relay` mode) | Not stored as application data at all — only a per-channel cursor | Members of the destination Telegram topic | Yes — forwarded verbatim, by the owner's own explicit configuration ([C-04](../requirements/constraints.md)) | The owner can delete the forwarded Telegram message itself (an ordinary Telegram action); there is no separate application record to delete, because none exists |
| Push subscription records | Cloudflare D1 only (`push_subscriptions`, `push_sent`) | The News Site's own push sender; not exposed to a reader | No — the owner's own device/browser metadata | Yes — a device can unsubscribe itself, or a subscription is dropped automatically after `MAX_PUSH_FAILURES` consecutive failures or a 404/410 from the push service |
| Personal account credentials (`TG_SESSION`, X/Reddit/Patreon session cookies) | Azure Key Vault | The owner only | No — these are the owner's own accounts | Yes — rotated or revoked by the owner at any time; revocation also breaks collection from that source until re-authenticated |
| News Site secrets (`SITE_TOKEN`, `INGEST_KEY`) | Azure Key Vault (source of truth for deploy) and Cloudflare's own secret store (live copy) | The owner only | No | Yes — rotated independently of each other; see `workers/news-site/README.md`, "Key rotation" |

### Rules

- **`state.db` is the only writable system of record.** Cloudflare D1 has no
  write path back into `state.db`; it receives whole-row upserts from the
  Digest Runner and nothing else. A change made directly in D1 (a manual
  `wrangler d1 execute`, for instance) is invisible to `state.db` and will be
  silently overwritten by the next ordinary ingest of that digest id.
- **The News Site never reads `state.db` directly**, and no credential it
  holds would let it. Every piece of content it serves arrived through one
  `PUT /ingest/:id` call.
- **This system does not claim ownership of third-party content.** Item text
  collected from Telegram, X, Reddit, and Patreon remains the property (and,
  where applicable, the personal data) of whoever wrote it. This system
  holds a copy for the sole purpose of summarizing it for the owner, ages
  that copy out ([../security/data-classification.md](../security/data-classification.md)),
  and — outside the `relay` lane — never republishes it verbatim.
- **Deletion here is real but not global.** Removing a row from `state.db`,
  rotating `SITE_TOKEN`, or deleting a forwarded Telegram message all take
  effect immediately within this system's own stores, but none of them can
  reach content a reader has already saved, a search engine has indexed (the
  News Site sends `X-Robots-Tag: noindex, nofollow` specifically to
  discourage this, but cannot guarantee it), or that still exists unchanged
  at the original third-party source. There is no mechanism in this system
  for propagating a deletion request back to Telegram, X, Reddit, or
  Patreon — nor could there be, since this system is a reader of those
  platforms, not their operator.
- **A new consumer of digest data is added to the News Site's ingest payload
  or a new Telegram message, never given direct database access** — the
  same "add to the interface, don't read the store" discipline the
  state.db/D1 split already enforces structurally.
