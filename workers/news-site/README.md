# news-site

The private digest archive for the notification-digest service
(github.com/Dezoxy/notification-digest). Every ~3 hours the digest VM POSTs
(technically PUTs) a newly generated digest into this Worker, which stores it
in D1 and serves it back as a small, readable HTML site.

Live at: `https://news.toomhorvath.com/t/<SITE_TOKEN>/` and
`https://news.tomhorvath.me/t/<SITE_TOKEN>/` (both point at the same Worker +
D1 database via Cloudflare Workers Custom Domains — see `news_site.tf` at the
repo root).

## Trust model: capability links, not login

There is **no login form and no Cloudflare Access** in front of this site —
by design. The whole thing lives under an unguessable token path,
`/t/<SITE_TOKEN>/`, shared once in a private Telegram group. Anyone who has
the full URL can read the archive; anyone who doesn't gets a plain 404 that
looks exactly like a 404 for any other nonexistent path or a wrong token —
the site's existence is never hinted at.

This only works if the token never leaks, which is why two headers on every
HTML response are load-bearing, not cosmetic:

- **`Referrer-Policy: no-referrer`** — digest bodies are full of outbound
  citation links (news sources, Polymarket, etc.). Without this header,
  clicking any of them sends the *current page's URL* — which contains the
  capability token — as the `Referer` header to whatever site got clicked.
  That's the single biggest way a token like this leaks in practice.
- **`Cache-Control: private, no-store`** — the token is part of the URL, so
  the response must never be cached by a shared cache, browser disk cache
  survivable across profiles, or any CDN layer that isn't already scoped to
  this one requester.

Also set on every HTML response: `X-Robots-Tag: noindex, nofollow` (belt and
suspenders against a crawler that ignores `robots.txt`) and
`Content-Type: text/html; charset=utf-8`. `GET /robots.txt` itself needs no
token and disallows everything.

Two independent secrets, two independent audiences — a leaked reader link
must never grant write access, and vice versa:

- `SITE_TOKEN` — the reader capability token, embedded in the URL path.
- `INGEST_KEY` — a machine credential the digest VM sends as the
  `x-ingest-key` header on every `PUT /ingest/:id`. Never shown to readers.

Both are compared with a hash-then-`timingSafeEqual` helper (same pattern as
the sibling `workers/polymarket-proxy` Worker), so neither can be recovered
byte-by-byte via response-time measurement. The Worker fails closed if either
secret is unset — an unauthenticated ingest endpoint or reader path is worse
than a broken one.

`body_html` in the digest payload arrives **pre-sanitized by the app**
(Python `nh3`) and is stored and served verbatim inside the article — it is
the only field the Worker ever inserts into a page without HTML-escaping.
Every other stored field (`tldr`, counts, etc.) is escaped on the way out.
`body_html_hu` (see "Hungarian support" below) follows the exact same
contract: pre-sanitized upstream, inserted raw only into the same article
slot.

## Hungarian support (EN | HU)

The digest app can optionally push a Hungarian translation alongside the
English digest. This Worker never translates anything itself — it only
stores and serves whatever the app sends.

- **Ingest**: `PUT /ingest/:id` accepts optional `tldr_hu`, `body_html_hu`,
  and `body_md_hu` fields, validated with the same size/non-empty rules as
  their English counterparts (`tldr_hu` ≤ 32KB, bodies ≤ 2MB). They must
  either all be present or all be absent — a half-translation is rejected
  with `400`. Omitting them entirely (the pre-Hungarian app version's
  payload shape) keeps working unchanged. Re-ingesting a digest without hu
  fields NULLs out any translation stored for it previously — upserts stay
  idempotent and reflect the latest payload exactly, in both languages.
- **Reading**: every reader route has a parameterized Hungarian twin —
  `GET /t/:token/hu/` and `GET /t/:token/hu/d/:id` — with the same token
  check, headers, and 404 philosophy as the English routes. A masthead
  switcher (`EN | HU`) links between the same page in both language spaces.
  If a digest has no Hungarian translation yet, the `/hu/` pages fall back
  to the English `tldr`/`body_html` with a small in-page note rather than
  erroring or showing nothing.

## Deploy

```bash
cd workers/news-site

# 1. Create the D1 database
wrangler d1 create news-digests
# -> paste the returned database_id into wrangler.jsonc

# 2. Apply the schema
wrangler d1 execute news-digests --file schema.sql

# 3. Deploy the Worker
wrangler deploy

# 4. Set both secrets (paste when prompted)
wrangler secret put SITE_TOKEN
wrangler secret put INGEST_KEY
```

The Terraform in this repo's root (`news_site.tf`) only wires up
`news.toomhorvath.com` and `news.tomhorvath.me` as Workers Custom Domains
pointing at the `news-site` service name — it does not create the D1
database, apply the schema, deploy the script, or set secrets. Those steps
above are run by hand (or by whatever deploy tooling wraps `wrangler`) after
`terraform apply`.

Generate the two secret values with, e.g.:

```bash
openssl rand -hex 32
```

`SITE_TOKEN` is the value shared in the private Telegram group as part of the
full URL (`https://news.toomhorvath.com/t/<value>/`). `INGEST_KEY` goes into
the digest service's own secret path (Azure Key Vault → homelab deploy) as
whatever env var its `emailer`/archive client reads — see that repo's
`config.py` for the exact name.

## Schema migrations

`schema.sql` is only for fresh installs (`wrangler d1 execute ... --file
schema.sql` against a brand-new, empty database). An already-deployed
database needs its schema brought forward by hand — this Worker has no
migration runner — via the numbered files in `migrations/`, applied once,
in order, against the **remote** D1 database:

```bash
wrangler d1 execute news-digests --remote --file migrations/0002-hu-columns.sql
```

| Migration | Adds |
| --- | --- |
| `0002-hu-columns.sql` | `tldr_hu`, `body_html_hu`, `body_md_hu` (nullable) on `digests`, for the Hungarian-translation feature. |
| `0003-kind-column.sql` | `kind` (`NOT NULL DEFAULT 'window'`) on `digests`, distinguishing the once-daily 20:00 synthesis (`daily`) from the regular 3-hourly window digest (`window`). |

## Key rotation

Rotate `SITE_TOKEN` if the Telegram group membership changes or the link is
suspected to have leaked outside it; rotate `INGEST_KEY` on a normal
credential-hygiene schedule or if the digest VM's secret store is suspected
compromised. They're independent — rotating one never affects the other.

1. Generate a new value: `openssl rand -hex 32`
2. `wrangler secret put SITE_TOKEN` (or `INGEST_KEY`)
3. For `SITE_TOKEN`: share the new full URL in the Telegram group; the old
   URL now 404s like it never existed.
   For `INGEST_KEY`: update the digest service's secret (Key Vault → homelab
   deploy). Until both sides match, ingest calls fail closed with 401 — no
   silent window where old or new key both work.

## Smoke test

```bash
TOKEN="<SITE_TOKEN>"
KEY="<INGEST_KEY>"
HOST="https://news.toomhorvath.com"

# robots.txt needs no token
curl -s "$HOST/robots.txt"

# wrong token and unknown paths are indistinguishable 404s
curl -s -o /dev/null -w "%{http_code}\n" "$HOST/t/wrong-token/"     # 404
curl -s -o /dev/null -w "%{http_code}\n" "$HOST/definitely-nothing" # 404

# ingest without the key is rejected
curl -s -o /dev/null -w "%{http_code}\n" -X PUT "$HOST/ingest/1"    # 401

# ingest a digest
curl -s -X PUT "$HOST/ingest/1" \
  -H "x-ingest-key: $KEY" \
  -H "content-type: application/json" \
  -d '{
    "created_at": "2026-08-05T16:00:00Z",
    "tldr": "Smoke test digest.",
    "item_count": 1,
    "section_count": 1,
    "has_attention": false,
    "body_html": "<p class=\"tldr\"><strong>TL;DR:</strong> Smoke test digest.</p><h2>Test</h2><p>Hello.</p>",
    "body_md": "**TL;DR:** Smoke test digest.\n\n## Test\n\nHello."
  }'
# -> {"ok":true}

# read it back with the real token
curl -s "$HOST/t/$TOKEN/"        # index page, should list digest #1
curl -s "$HOST/t/$TOKEN/d/1"     # the digest page itself
```

## Design roadmap (2026-08)

A visual/reading-experience upgrade pass, agreed 2026-08-08 under the
direction "the private wire desk": this site is a timestamped briefing wire
for exactly one reader, and the design should encode that data/prose split
instead of reading as a generic indigo dashboard. One PR per step below, in
order, each squash-merged to `main` before the next step starts; after the
last step merges, `wrangler deploy` ships the whole set at once. Every step
must respect the owner-tuned decisions already documented in `worker.js`'s
CSS comments — the 15px phone font, mobile masthead layout, FAB
bubble-gutter anchoring, `scrollbar-gutter: stable`, and the desktop bubble
card — none of that regresses. Every change ships in both languages (EN/HU)
and both themes. This stays a single-file Worker with inline CSS and no
external requests (system font stacks only), and `Referrer-Policy:
no-referrer` and its sibling headers (see "Trust model" above) stay
load-bearing throughout.

- [x] **Roadmap (this section)** — record the design pass in-repo. (This
  very PR.)
- [x] **Must-fixes: page titles + bottom nav** — `<title>` is the bare
  hostname on every page, making history and open tabs indistinguishable;
  digest pages get `digest #N · <date> <time>` (localized, with a "napi
  összefoglaló" label for HU daily briefs), the index keeps the hostname.
  Digest prev/next nav is top-only today; mirror it below the article — after
  a 900-word read the natural gesture is "older/next", not scroll-to-top.
- [x] **Type system + palette** — the signature move: three type roles, all
  zero-byte system stacks. Prose (article body + TL;DR) moves to
  `ui-serif`/Iowan Old Style/Georgia; the data layer (times, counts, dateline
  stamps, citation chips) moves to `ui-monospace`/SF Mono; chrome (masthead,
  tabs, nav, footer) stays system sans. Light theme sharpens to ink
  `#16181D` on barely-warm paper `#FBFAF7` with matching hairlines; dark
  theme unchanged. Time is this site's primary key — the typography should
  say so.
- [x] **Index: latest-briefing lead card + sticky day headers** — the newest
  digest gets a lead card: mono dateline eyebrow (`LATEST · FRI 18:00 CEST ·
  74 ITEMS`), full unclamped TL;DR, visual weight; the reader's most common
  task is "read the newest one". Everything below stays the compact ledger.
  Day headers become `position: sticky` so mid-scroll position is always
  visible.
- [x] **Digest page: wire dateline + section index** — the muted stamp line
  becomes a mono, letter-spaced dateline block (`FRI 08 AUG 2026 · 18:00
  CEST · DIGEST #412 · 74 ITEMS`). Below the TL;DR, a section index of anchor
  chips built from the article's `<h2>`s (ids injected at render time) —
  briefings run ~8 sections and deserve direct jumps.
- [x] **Index filter + theme toggle** — a client-side filter input that hides
  non-matching entries by TL;DR text (inline JS, no backend — answers "where
  did I read about X"); a manual light/dark/system toggle persisted in
  `localStorage` for readers who want to override the OS theme.
- [x] **Polish** — citation chips get a `title` attribute naming the
  destination domain (provenance at a glance); a print stylesheet for digest
  pages (strip chrome, black-on-white); empty states gain a voice ("No daily
  briefs yet — the first one lands at 20:00.", with a proper HU counterpart).

After the last step above merges, `wrangler deploy` from `workers/news-site`
ships everything in this roadmap at once — there is no version tag here, the
deploy *is* the release; smoke-test per the section above before and after.

Deliberately deferred, with reasons: D1 FTS5 full-text search (real work;
revisit when the client-side filter stops being enough), pagination past the
1000-row backstop (~4 months away from being felt at current volume), and
PWA/add-to-home-screen (the manifest would be fetched tokenless and
`start_url` would embed the capability token in a persisted, cached
artifact — needs its own privacy think first).

## Design roadmap 2 (2026-08-09)

A second pass, agreed 2026-08-09, exploiting three unused veins: structured
data the site never sees, the reader's 8-pulses-a-day rhythm, and knowledge
the pipeline computes but discards at render time. Same execution contract
as roadmap 1: one squash-merged PR per step below, in order; the owner-tuned
CSS decisions from roadmap 1 never regress; every change ships in EN/HU,
both themes, and a working no-JS baseline; this stays a single-file Worker
with no external requests; and `Referrer-Policy: no-referrer` and the rest
of the trust-model headers (see "Trust model" above) stay load-bearing
throughout. Steps 1–8 are site-only and ship with one `wrangler deploy`; the
last two need the digest app's cooperation (separate repo, its own
release/deploy path) and the site side ships first, backward-compatibly.

- [ ] **Roadmap (this section)** — record the second design pass in-repo.
  (This very PR.)
- [ ] **Unread fence** — a `localStorage` last-visit timestamp; the index
  draws one labeled hairline between digests that arrived since the
  reader's last visit and everything older. The index becomes an inbox at a
  glance. No backend, progressive enhancement (no JS = no fence).
- [ ] **Day-pulse strip** — a micro bar strip (one bar per window digest,
  height = item_count) rendered from data the index query already returns;
  the day's news volume readable before a word is read. Pure CSS bars.
- [ ] **Living chrome: theme-color + countdown** — `theme-color` metas for
  light/dark (and following the manual toggle) so mobile browser chrome
  melts into the page; a muted mono footer line counting down to the next
  window (newest `created_at` + 3h), refreshed by the existing inline
  script.
- [ ] **Keyboard navigation** — j/k (and arrow keys) hop older/newer on
  digest pages; "/" focuses the index filter. Desktop convenience; never
  intercepts typing in the filter input.
- [ ] **Attention ledger** — a filter chip by the view tabs showing only
  `has_attention` digests: "what needed me this week", answerable from a
  column the index already selects.
- [ ] **Navigation feel: prefetch + view transitions** — a
  speculation-rules/prefetch hint for the lead card's digest so the
  most-likely tap opens instantly; `@view-transition` navigation crossfade
  for browsers that support it. Both progressive, both ignored gracefully.
- [ ] **Ingest v2: source_counts + failed_sources (site side)** — `PUT
  /ingest/:id` accepts two OPTIONAL fields: `source_counts` (map of source
  → item count) and `failed_sources` (list of collector names); absent =
  old app payloads keep working byte-for-byte. Index entries render a
  five-segment source-spectrum micro-bar and a degraded-run badge when the
  data exists. Needs a numbered migration for the new columns.
- [ ] **(digest repo) publish source_counts + failed_sources** — the app
  computes per-source counts from the digest's own stamped items and parses
  failed sources from the deterministic ⚠ banner; ships in the app's own
  release train (tag → homelab bump → deploy), after which new digests
  light up the spectrum/badge.
- [ ] **(digest repo) Telegram section deep links** — the TL;DR bot message
  gains up to three section links targeting the site's `#sN` anchors,
  derived from `body_md`'s `## ` headings with the same needs-attention
  exclusion rule the site's TOC numbering uses — the two implementations
  must agree on numbering or links jump wrong.

Steps 1–8 deploy with one `wrangler deploy` (the deploy is the release); the
two digest-repo steps ride that repo's own versioned release. Explicitly out
of scope, unchanged from roadmap 1's deferrals: FTS search, pagination,
PWA/offline (the capability-token-in-persistent-storage wrinkle) — plus
story arcs and the calendar heatmap, which are wanted but need their own
design pass (story identity is an app-side problem first).
