## Data Classification

This system has one owner, one operator, and one reader
([C-01](../requirements/constraints.md)); there is no multi-tenant data to
segregate. The classification below exists to answer one question per row:
what is this, where does it live, how long does it stay, and does it ever
reach the public News Site.

| Data | Classification | Where it lives | Retention | Reaches the public site? |
|---|---|---|---|---|
| Personal account sessions (`TG_SESSION`, X/Reddit/Patreon cookies) | **Secret**, account-level ([C-02](../requirements/constraints.md)) | Azure Key Vault; container environment at runtime; X's cookie also persists as a live file at `/srv/appdata/digest/` on the VM's local ext4 | Until rotated by the owner | Never |
| News Site secrets (`SITE_TOKEN`, `INGEST_KEY`) | **Secret** | Azure Key Vault; Cloudflare Worker secret store (`wrangler secret put`) | Until rotated; `workers/news-site/README.md` documents the rotation procedure | `SITE_TOKEN` **is** the reader access path (embedded in every reader URL); `INGEST_KEY` never reaches a reader |
| Claude OAuth token, SMTP password, GHCR pull token | **Secret**, service-style | Azure Key Vault; container/deploy-tooling environment | Until rotated | Never |
| Collected item content (Telegram message text, X notification text, Reddit/Patreon post bodies, author/chat metadata) | **Confidential — third-party personal data.** Written by people other than the owner | `items` table in `state.db` (VM) | 90 days after the owning digest has fully delivered; 14 days if never summarized (`_ITEMS_PRUNE_DAYS`, `_STALE_UNSUMMARIZED_PRUNE_DAYS`, `digest/state.py`) | Indirectly: as a *summarized excerpt plus a deep link* back to the original, never the collected text verbatim — except the `relay` lane, which forwards whole posts verbatim into a Telegram topic **by explicit owner configuration** and never touches the site at all ([C-04](../requirements/constraints.md)) |
| Digest bodies (`body_md`, `body_html`, and their optional `_hu` translations) — the owner-facing synthesis of a window's items | **Internal**, LLM-derived from the row above | `digests` table in `state.db` (system of record, never pruned); `digests` table in Cloudflare D1 (disposable rendering copy, also never pruned); optionally `ARCHIVE_DIR` on the VM as plain markdown | Indefinite in both databases; no scheduled deletion exists for a digest body once created | Yes — this is the content the News Site exists to serve |
| Topics, deltas, model provenance, arc-context primers | **Internal** metadata about a digest, not content in its own right | `digests` columns and the `arc_context` table in `state.db` and D1 | `arc_context` rows are never pruned by design (a primer is durable background, not a recap); everything else follows the owning digest | Yes — rendered as the story-arc line, the "What changed" block, and the "Written by" / "Translated with" provenance lines |
| Push subscription records (device endpoint, keys, language) | **Internal**, device-identifying | `push_subscriptions` table, D1 only (not mirrored to `state.db`) | Until the device unsubscribes, a push service reports it gone (404/410), or it accumulates `MAX_PUSH_FAILURES` consecutive failures | No — never rendered to a reader; consumed only by the Worker's own push sender |
| Application/run logs | **Internal** | journald / Loki on the VM | Not defined by this repository; owned by the homelab deployment | No |
| Digest markdown archive (optional, `ARCHIVE_DIR`) | Same classification as the digest body it copies | Local disk on the VM, best-effort write (`digest/emailer.py:archive`) | No scheduled deletion | No — a local operator convenience, not a delivery channel |

### Rules

- **Collected third-party content is never stored as an end in itself.** It
  exists only to be summarized (or, for `relay`, forwarded once) and then
  aged out; the digest bodies it produces are what this system actually
  retains long-term.
- **The relay lane is the one deliberate exception to "summaries, not
  verbatim text."** It forwards public-channel posts verbatim using
  Telegram's own native forward, and the owner configures which channels are
  eligible ([C-04](../requirements/constraints.md)). It stores no items or
  digest rows at all — only a per-channel cursor.
- **Log messages must never contain a secret value.** `digest/config.py`'s
  own error-handling convention (name the offending variable, never echo its
  value) is the concrete instance of the repository-wide hard rule against
  logging secrets, cookies, or session strings.
- **Personal-account secrets are classified above service secrets** because
  losing one is an account compromise, not a credential rotation — see
  [security-architecture.md](security-architecture.md), "The sharpest risk."

### GDPR posture (unverified beyond this reasoning)

The owner is based in Hungary (EU), and a meaningful share of the data this
system processes — messages and posts collected from Telegram, X, Reddit,
and Patreon — is third-party personal data the owner did not author. This
system currently relies on the position that collecting and summarizing
one's own notification feeds for exclusively personal reading is "purely
personal or household" processing (the kind of activity GDPR Article 2(2)(c)
is understood to place outside the Regulation's scope). That reasoning has
real limits worth naming rather than assuming away:

- The News Site publishes summaries and deep links **beyond the owner
  alone** — to anyone holding the capability link — which is a step past
  purely personal use, even though the audience is small and deliberately
  chosen (currently: whoever the owner has shared the link with in a private
  Telegram group).
- The `relay` lane republishes third-party content verbatim, which is a
  stronger claim on that content than a summary-plus-link.
- This document is not a legal opinion, and no such opinion has been sought.
  If the audience for the News Site or the volume/nature of what it
  publishes changes materially, this posture should be revisited rather than
  assumed to still hold.

There is no automated mechanism in this system for honoring a third party's
deletion request against content already collected or already summarized on
the public site — see [data-ownership.md](../data/data-ownership.md) for what
the owner can and cannot actually remove.
