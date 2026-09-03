# news-site

The private digest archive for the notification-digest service — and it now
lives IN that service's repo, which is where you are. Every ~3 hours a digest
run PUTs a newly generated digest into this Worker, which stores it in D1 and
serves it back as a small, readable HTML site.

It moved here (with its full history) from `Dezoxy/toom-edge`, the Cloudflare
infra repo. What stayed there is the Terraform that binds the two hostnames
to this Worker's service name — `news_site.tf`. So a hostname/Access change
is still a toom-edge change; everything about the Worker itself is here.

Live at: `https://news.toomhorvath.com/t/<SITE_TOKEN>/` and
`https://news.tomhorvath.me/t/<SITE_TOKEN>/` (both point at the same Worker +
D1 database via Cloudflare Workers Custom Domains — see `news_site.tf` at the
repo root).

## Source layout

One deployed script, several source files. `worker.js` is the entry —
the authoritative file-header doc, the route patterns, the dispatch — and
everything else lives in `src/`, which wrangler's bundler folds back into a
single self-contained script at deploy time. **No build step and no build
configuration**: `wrangler.jsonc` still just names `worker.js` as `main`.

| | |
| --- | --- |
| `src/config.js` | tunable caps, shape regexes, the arc-identity fold |
| `src/auth.js` | `keyMatches`/`tokenMatches` — timing-safe secret compare |
| `src/http.js` | response constructors and `esc()` |
| `src/dates.js` | Europe/Budapest formatting, ISO-week arithmetic |
| `src/strings.js` | the EN/HU chrome vocabulary (key-for-key parity, tested) |
| `src/hrefs.js` | URL-grammar builders + the language/view switchers |
| `src/sections.js` | the `#sN` anchor contract (`buildSectionToc` et al) |
| `src/css.js` | the stylesheet, one static string |
| `src/client.js` | the client-side script, one static string |
| `src/chrome.js` | `pageChrome` — the shell every page renders into |
| `src/icons.js` | the three base64 PWA PNGs — same mark as `FAVICON_SVG` |
| `src/pwa.js` | `buildManifest` + `buildServiceWorker` (PLAN.md §11.7) |
| `src/push.js` | the `push/*` endpoints and their validators |
| `src/vapid.js` | ES256 JWT signing for the push services (RFC 8292) |
| `src/notify.js` | the fan-out: claim-once, newest-only, delivery, pruning |
| `src/ingest.js` | `PUT /ingest`: handler plus every validator |
| `src/handlers.js` | the GET page handlers |
| `src/render-*.js` | per-page renderers; shared helpers in `render-shared.js` |

Two rules worth knowing before editing:

- **`css.js` and `client.js` are template-literal exports.** Neither string
  may contain a backtick, a `${` sequence, or its own closing tag — any of
  the three corrupts the literal or the inline embedding. `npm test`
  enforces it, but it is easier to simply not write one.
- **Every phone override lives in ONE `max-width: 40em` block** near the end
  of `css.js`, after every base rule it overrides. (Grep finds a second
  match: its `and (prefers-reduced-motion: reduce)` twin, immediately after.
  Print is last.) Scattered phone blocks used to lose silently to
  equal-specificity base rules defined later in the file — that cost six
  separate rules over this file's history. Add phone rules to that block,
  not next to the feature they modify.

## Tests

```bash
npm ci            # once per clone: installs prettier (the only devDependency)
npm test          # invariants + golden byte-check + prettier check
npm run golden    # regenerate the golden pages (only when output should change)
```

The tests themselves need nothing installed — `node --test` plus a
byte-comparison script, against a stubbed D1 and a frozen clock. `npm ci` is
only for the prettier check `npm test` ends with; skip it and that last step
fails with `prettier: command not found` on a fresh clone. A refactor that should not change output must
pass with a **zero** golden diff; a change that should alter output
regenerates the goldens, and that diff is the review artifact.

See `test/README.md` for the fixture/branch contract and the verification
recipes (bundled-artifact check, the CSS computed-style matrix, driving the
real router in a browser).

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

One place the token deliberately DOES come to rest: Workers Observability.
Logs and traces record request URLs, so the capability token lands in
retained observability data (`redact_query_string` is no help — the token
is in the path, not the query string). That is an accepted tradeoff, not an
oversight: the account has one operator, and the debugging value is real.
The lever if that ever stops being true is `head_sampling_rate` in
`wrangler.jsonc`, which records only a fraction of requests. Every
observability setting is declared in that file rather than toggled in the
dashboard, because the file overwrites the dashboard on every deploy — and
deploys are automatic now.

One more place the token comes to rest, new with the installable shell
(PLAN.md §11.7): a reader who adds the site to a Home Screen persists
`start_url` in the installed app record, and the service-worker registration
persists its own script URL. Both carry the capability token. That is
deliberate and it is the *same* exposure class the browser's history and any
bookmark already carry — which is precisely the argument that let PWA stop
being deferred after four roadmaps. What did NOT get waived is the other half:
**the service worker uses no Cache API at all**, so no digest content is ever
written to disk and `Cache-Control: private, no-store` stays honest. `npm test`
enforces this rather than trusting the comment — see the "cache-free" assertion
in `test/render.test.mjs`. What ships is installable + push, explicitly not an
offline reader; adding a cache there is not an increment on this feature, it is
the still-deferred one.

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
- **Weekly brief**: `kind="weekly"` digests (the once-a-week Sunday-evening
  synthesis of the week's daily briefs) follow this exact same translation
  contract and get their own HU stamp label ("heti összefoglaló"); like daily
  briefs, they also get a dedicated URL view — `GET /t/:token/weekly/` and
  `GET /t/:token/hu/weekly/` — filtered to `kind='weekly'` only, alongside
  their unfiltered appearance in the All view, badged the same way a daily
  row is.

## Routes

The URL grammar is `/t/:token/(hu/)?(daily/|weekly/)?(w/YYYY-Www/)?` for index and
digest pages, plus the standalone `search`, `a/:slug`, `about`,
`manifest.webmanifest`, `sw.js`, and the four `push/*` endpoints. The
language segment always comes first; `worker.js`'s file header is the authoritative
list.

The `push/*` four are the exception to the language grammar, deliberately: they
are machine endpoints, not pages, so they have no `hu/` variant. A service
worker only ever knows the EN scope (its own script URL), and a subscription's
language is a stored FIELD rather than a URL segment — a `/hu/` page subscribes
through these same paths and says `{"lang":"hu"}` in the body.

Four routes need no token: `robots.txt` and the three icons. That is not a gap —
an icon reveals nothing about the site's content (`robots.txt` already concedes
something is served here), browsers fetch them before any page-level auth context
exists, and keeping them off the token path means an INSTALLED app's stored record
embeds the capability token once, in the manifest's `start_url`/`scope`/`id`,
instead of in four more icon URLs beside it.

| Route | Serves |
| --- | --- |
| `GET /robots.txt` | Disallow-everything. |
| `GET /favicon.svg` | Tab icon. |
| `GET /icon-512.png`, `/icon-maskable-512.png`, `/apple-touch-icon.png` | PWA icons. |
| `PUT /ingest/:id` | Upsert a digest. Requires the `x-ingest-key` header. |
| `GET /t/:token/` | Index, all kinds, newest first, grouped by day. `hu/` prefix for Hungarian chrome throughout. |
| `GET /t/:token/daily/`, `/weekly/` | Same index filtered to that `kind` only; prev/next on digest pages stays within the kind. |
| `GET /t/:token/w/2026-W32/` | One ISO week's ledger (Monday-start, Europe/Budapest). |
| `GET /t/:token/d/:id` | A single digest. |
| `GET /t/:token/search?q=` | FTS5 full-text search over the whole archive. Also backs the index filter box via `?fragment=1`. |
| `GET /t/:token/a/:slug` | Story-arc page — every digest carrying that arc identity, reconstructed at request time. |
| `GET /t/:token/about` | Static explainer for anyone the capability link is shared with. |
| `GET /t/:token/manifest.webmanifest` | Web app manifest; `hu/` variant differs only in `start_url`. |
| `GET /t/:token/sw.js` | The service worker. Its URL is its scope. |
| `GET /t/:token/push/key` | The VAPID public key. Token-gated, though the key itself is public. |
| `POST /t/:token/push/subscribe` | Store or refresh one device's push subscription. |
| `POST /t/:token/push/unsubscribe` | Forget one device. |
| `POST /t/:token/push/latest` | What the service worker should show, in the subscription's language. |
| anything else | Plain `404`, wrong token included. |

## Deploy

```bash
cd workers/news-site   # in the notification-digest repo

# 1. Create the D1 database
wrangler d1 create news-digests
# -> paste the returned database_id into wrangler.jsonc

# 2. Apply the schema (--remote: the deployed database, not a local replica.
#    wrangler v4 requires one of --remote/--local and will not guess.)
wrangler d1 execute news-digests --remote --file schema.sql

# 3. Deploy the Worker
wrangler deploy

# 4. Set both secrets (paste when prompted)
wrangler secret put SITE_TOKEN
wrangler secret put INGEST_KEY
```

### How a notification actually happens

`handleIngest` calls `notifyForDigest` under `ctx.waitUntil` after every write
has succeeded — not awaited, so a slow or failing push service can never delay
or fail an ingest. The digest is committed either way; this is strictly an
announcement.

Four gates stand between an ingest and a buzzing phone, and each exists for a
specific failure:

1. **Configured?** No VAPID keys, no send. This Worker deploys automatically
   on merge, so the code always lands before the secrets — that window is the
   default path through every deployment, not an edge case.
2. **Newest?** Only the highest `id` in `digests` rings. A historical backfill
   carries lower ids and claims silently; a backlog flush after an outage
   collapses to one notification, which is correct rather than lossy — the
   push is payload-less, so all of them would render the same newest brief
   anyway. Deliberately **not** a `created_at` freshness check: that column is
   backfillable for daily and weekly briefs, so a freshness window would
   suppress exactly the briefs most worth announcing.
3. **Claimed?** `INSERT OR IGNORE INTO push_sent` — an atomic claim, because
   `PUT /ingest/:id` is idempotent by contract and the app retries failed
   publishes. Claiming *before* sending makes this at-most-once: if every send
   then fails, the digest is never announced. That is the intended trade — the
   notification is not the delivery (the site has it, and Telegram still
   pings), whereas at-least-once means re-ringing a phone for a digest already
   read.
4. **Signable?** The VAPID token is minted once per push-service origin,
   before the fan-out. A signing failure aborts the whole run and touches no
   subscription: signing depends only on configuration, so it fails for every
   device or none, and charging it to the per-device error path would delete
   the owner's entire fleet over a typo in a secret.

Delivery outcomes: `404`/`410` deletes the row on the spot (the push service
saying "gone" is authoritative); anything else soft increments `fail_count`,
and a row is dropped after `MAX_PUSH_FAILURES` consecutive failures. A success
resets the counter and stamps `last_ok_at`.

### Sending a test push by hand

`scripts/send-test-push.mjs` sends one body-less push to every stored
subscription, reproducing exactly what `notify.js` sends — same VAPID scheme,
same headers, no body. It exists because the alternative way to test the send
path is to wait up to six hours for a digest.

```bash
VAPID_PRIVATE_JWK='<the JWK>' node scripts/send-test-push.mjs
```

It reads endpoints from D1 itself and DERIVES the public key from the private
JWK, which also proves the pair in Cloudflare matches — a mismatch there is
the commonest cause of a `401` and says nothing about itself in the response.

Note what it does **not** cover: it talks to the push service directly, so
claim-once, newest-only and the `waitUntil` fan-out are all bypassed. A green
run here plus a silent real digest means the fault is upstream of delivery.

### Web Push (optional, PLAN.md §11.7)

Push is entirely optional and the Worker runs fine without it: with no VAPID
keys set, `push/key` answers `503`, the settings row never appears, and (from
PR C) the send is skipped. That is not a degraded mode to fix in a hurry — it
is the state every deployment passes through, because this Worker deploys
automatically on merge and the code therefore always lands before the secrets.

```bash
# 1. Apply the push tables (BEFORE merging the code, ideally)
wrangler d1 execute news-digests --remote --file migrations/0009-push.sql

# 2. Generate a VAPID key pair (P-256; any Node with webcrypto will do)
node -e '
const { subtle } = require("node:crypto").webcrypto;
subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign","verify"]).then(async (kp) => {
  const raw = Buffer.from(await subtle.exportKey("raw", kp.publicKey));
  const b64u = (b) => b.toString("base64").replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
  console.log("VAPID_PUBLIC_KEY:", b64u(raw));
  console.log("VAPID_PRIVATE_JWK:", JSON.stringify(await subtle.exportKey("jwk", kp.privateKey)));
});'

# 3. Set both as SECRETS, not vars — a var in wrangler.jsonc would be
#    committed, and a var set in the dashboard is reverted by the next
#    deploy (this file overwrites the dashboard; see its own comment).
wrangler secret put VAPID_PUBLIC_KEY
wrangler secret put VAPID_PRIVATE_JWK
```

The public key genuinely is public — it is handed to every browser that
subscribes and is useless without the private half — so storing it as a secret
is about having one mechanism rather than about hiding it.

`VAPID_SUBJECT` is optional. RFC 8292 wants a `mailto:` or `https:` URL
identifying whoever operates the sender, so a push service has someone to
contact; with none set this falls back to the site's own origin, which is
always valid and needs no extra secret. Set one only if you want a real
contact address there — Apple validates this field more strictly than the
others.

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
| `0003-kind-column.sql` | `kind` (`NOT NULL DEFAULT 'window'`) on `digests`, distinguishing the once-daily 20:00 synthesis (`daily`) and the once-a-week Sunday-evening synthesis (`weekly`) from the regular 3-hourly window digest (`window`). |
| `0004-source-counts.sql` | `source_counts`, `failed_sources` (nullable) on `digests`, for the ingest v2 source-spectrum micro-bar and degraded-run badge. |
| `0005-fts-search.sql` | `digests_fts`, an external-content FTS5 virtual table over `tldr`/`body_md`/`tldr_hu`/`body_md_hu` plus its sync triggers and a one-time `rebuild` backfill, for the archive search route. |
| `0006-topics.sql` | `topics` (nullable) on `digests`, for the ingest v3 story-arc line. |
| `0007-deltas.sql` | `deltas` (nullable) on `digests`, for ingest v4 delta persistence — the digest page's "What changed" block and the arc page's per-appearance previously/now line. |
| `0008-arc-context.sql` | `arc_context`, a new table (not a column) holding one durable background primer per *arc identity*, pushed as an optional `arc_contexts` field on ingest. |
| `0010-provenance.sql` | `provenance` (nullable) on `digests`, for ingest v5 model provenance — the digest page's "WRITTEN" row naming which model summarized (and, when translated, which model translated) the brief, and whether an OpenRouter fallback model served instead of the primary Claude call. |

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

Most of what used to be checked by hand is now covered offline by `npm test`
(every route, both languages, the 404 indistinguishability, ingest auth and
each validator rejection). The curl pass below is what that cannot prove:
that the **deployed** Worker, its real secrets, and the real D1 agree.

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
must respect the owner-tuned decisions already documented in `src/css.js`'s
CSS comments — the 15px phone font, mobile masthead layout, FAB
bubble-gutter anchoring, `scrollbar-gutter: stable`, and the desktop bubble
card — none of that regresses. Every change ships in both languages (EN/HU)
and both themes. This stays a single-deployed-script Worker with inline CSS and no
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
both themes, and a working no-JS baseline; this stays a single-deployed-script Worker
with no external requests; and `Referrer-Policy: no-referrer` and the rest
of the trust-model headers (see "Trust model" above) stay load-bearing
throughout. Steps 1–8 are site-only and ship with one `wrangler deploy`; the
last two need the digest app's cooperation (separate repo, its own
release/deploy path) and the site side ships first, backward-compatibly.

- [x] **Roadmap (this section)** — record the second design pass in-repo.
  (This very PR.)
- [x] **Unread fence** — a `localStorage` last-visit timestamp; the index
  draws one labeled hairline between digests that arrived since the
  reader's last visit and everything older. The index becomes an inbox at a
  glance. No backend, progressive enhancement (no JS = no fence).
- [x] **Day-pulse strip** — a micro bar strip (one bar per window digest,
  height = item_count) rendered from data the index query already returns;
  the day's news volume readable before a word is read. Pure CSS bars.
- [x] **Living chrome: theme-color + countdown** — `theme-color` metas for
  light/dark (and following the manual toggle) so mobile browser chrome
  melts into the page; a muted mono footer line counting down to the next
  window (newest `created_at` + 3h), refreshed by the existing inline
  script.
- [x] **Keyboard navigation** — j/k (and arrow keys) hop older/newer on
  digest pages; "/" focuses the index filter. Desktop convenience; never
  intercepts typing in the filter input.
- [x] **Attention ledger** — a filter chip by the view tabs showing only
  `has_attention` digests: "what needed me this week", answerable from a
  column the index already selects.
  Removed 2026-08-09: the digest app's prompt contract disabled the "Needs
  attention" section (see `prompts/digest.md`/`prompts/daily.md` in the
  digest repo), so `has_attention` can never be true on a new digest again —
  the chip had become a filter for a permanently-false flag.
- [x] **Navigation feel: prefetch + view transitions** — a
  speculation-rules/prefetch hint for the lead card's digest so the
  most-likely tap opens instantly; `@view-transition` navigation crossfade
  for browsers that support it. Both progressive, both ignored gracefully.
- [x] **Ingest v2: source_counts + failed_sources (site side)** — `PUT
  /ingest/:id` accepts two OPTIONAL fields: `source_counts` (map of source
  → item count) and `failed_sources` (list of collector names); absent =
  old app payloads keep working byte-for-byte. Index entries render a
  five-segment source-spectrum micro-bar and a degraded-run badge when the
  data exists. Needs a numbered migration for the new columns.
- [x] **(digest repo) publish source_counts + failed_sources** — the app
  computes per-source counts from the digest's own stamped items and parses
  failed sources from the deterministic ⚠ banner; ships in the app's own
  release train (tag → homelab bump → deploy), after which new digests
  light up the spectrum/badge.
- [x] **(digest repo) Telegram section deep links** — the TL;DR bot message
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

## Design roadmap 3 (2026-08-09): weekly pagination

The All view's 1000-row backstop becomes real pagination, by ISO calendar
week (Monday-start, Europe/Budapest — matching every other local-time
rendering decision). A week is the natural briefing unit of a wire desk,
week URLs are permanent bookmarkable addresses, and these same `w/` pages
are the foundation the deferred calendar heatmap will link into later. Same
execution contract as roadmaps 1–2 (one squash-merged PR per step; owner-
tuned decisions never regress; EN/HU, both themes, no-JS baseline; single-
deployed-script Worker; trust headers stay load-bearing). No schema change and no
digest-repo involvement.

The URL grammar gains one optional, always-LAST segment:
`/t/:token/(hu/)?(daily/)?(w/2026-W32/)?`. Root index (no segment) = the
CURRENT week, keeping every living-chrome feature; `w/…` pages = that
week's day-grouped ledger only. Strict shape validation; garbage 404s
indistinguishably. Valid-shaped future weeks render empty rather than
special-casing. The daily view stays unpaginated (~3 years from feeling the
backstop; the machinery is view-parameterized if ever wanted).

A week rail sits under the view tabs on every index page: mono wire-style
`← W31 · WEEK 32 · 3–9 AUG · W33 →`; "newer" absent on the current week,
"older" ends at the oldest week with data (one MIN(created_at) probe).
Empty mid-range weeks render as pages (empty state + rail), not
skip-to-nonempty.

Week bounds derive from Budapest-local date parts (never fixed UTC
offsets — DST), compared as UTC ISO strings against created_at,
lexicographically like every existing date comparison. ISO year boundaries
(W52/W53→W01) carry the year in the label/URL.

Feature scoping: lead card, pulse strip, countdown, and prefetch are
CURRENT-WEEK-ONLY (on archive pages a "Latest" card would lie, the
countdown would read "closing about now" forever). Filter + attention chip
naturally scope to the rendered week. Language switcher keeps the week;
view tabs drop it (they already always target a view's root index). Digest
pages untouched — prev/next stays the pure created_at chain and crosses
week boundaries invisibly.

THE one called-out hazard: the unread fence's script advances
`localStorage.lastVisit` to the newest entry on ANY index page today; on an
archive page that would REGRESS the stamp and spawn a bogus fence next
visit. The update becomes forward-only (store only if newer than stored).

- [x] **Roadmap (this section)** — (this very PR).
- [x] **Core week machinery** — Budapest ISO-week helpers; the `w/` route
  segment; week-bounded index query + oldest-week probe; the week rail
  (EN/HU strings); empty-week state.
- [x] **Feature scoping + fence guard** — current-week-only gating of
  lead/pulse/countdown/prefetch; the forward-only lastVisit guard;
  switcher week-mapping; a lang×view×week verification sweep on the seeded
  rig.

Ships with one `wrangler deploy`; the calendar heatmap remains deferred but
now has its link targets waiting.

## Design roadmap 4 (2026-08-09): the deep archive

A fourth pass, agreed 2026-08-09, direction "the deep archive": the archive
has grown past what one screen and one client-side filter can navigate, so
this pass gives it depth (heatmap, search, story arcs) and finishes the
phone-first reading polish. Same execution contract as roadmaps 1–3: one
squash-merged PR per step below, in order; owner-tuned CSS decisions never
regress; every change ships in EN/HU, both themes, and a working no-JS
baseline; single-deployed-script Worker, no external requests; `Referrer-Policy:
no-referrer` and the trust-model headers (see "Trust model" above) stay
load-bearing throughout. Steps 1–7 are site-only and ship with one
`wrangler deploy`; step 8's site side ships first, backward-compatibly, and
the final step needs the digest app's cooperation (separate repo, its own
release train). A pre-pass audit found reduced motion already fully
handled (smooth-scroll and the view-transition crossfade are both gated
behind `prefers-reduced-motion: no-preference`, and `.backfab` drops its
transition under reduce) and `<html lang>` already correct on HU pages —
so this pass contains no motion/lang work.

- [x] **Roadmap (this section)** — record the fourth design pass in-repo.
  (This very PR.)
- [x] **Reading polish: hyphenation + touch provenance** — `hyphens: auto`
  on the serif prose blocks (Hungarian's long compounds especially deserve
  it on the 15px phone column; `<html lang>` is already right so the
  hyphenation dictionaries are too); and citation-chip destination domains
  become visible on touch devices via an `@media (hover: none)` rule
  reusing the `title` attribute addCiteTitles already sets — the hover
  affordance doesn't exist on the phone the site is mostly read on (print
  already does exactly this with `.cite[title]::after`).
- [x] **Resume chip** — the unread fence is passive; a small floating "↓
  new since your last visit" chip appears when the fence exists below the
  viewport and scrolls to it on tap. Progressive enhancement like the
  fence itself (no JS = no chip), current-week index only.
- [x] **Ledger density toggle** — compact/comfortable, a second small
  toggle beside the theme toggle, persisted in `localStorage` and applied
  pre-paint by the same head script pattern as the theme (`data-density`
  on `<html>`); compact tightens `.entry` padding and the excerpt clamp
  for readers who want the wire-ledger look.
- [x] ~~**Calendar heatmap**~~ — **shipped, then removed 2026-08-10.** Was a
  server-rendered trailing-12-week day-grid (columns = ISO weeks, rows =
  Mon–Sun) at the bottom of the CURRENT-week all-view index, cell intensity
  stepped by that Budapest-local day's summed `item_count`, each cell linking
  into its week's `w/` page. Removed as unnecessary: at a personal digest's
  volume the grid carried no signal the week rail and archive search don't
  already give, and it cost one extra bounded D1 query on every current-week
  page load. The `w/YYYY-Www/` URLs it linked into remain — roadmap 3 owns
  those, and the week rail is the archive navigation.
- [x] **Archive week sparkline** — archive `w/` pages lost the pulse strip
  by design (roadmap 3 gated it to the current week); the week rail's
  center gains seven per-day micro-bars for the rendered week, from rows
  the week query already returns — the pulse visual language, sized for
  the rail.
- [x] **D1 FTS5 search** — the deferral finally comes due: the client-side
  filter only sees the rendered week since roadmap 3. A numbered migration
  adds an FTS5 table over `tldr`/`body_md` (+ their `_hu` twins) with sync
  triggers and a one-time rebuild backfill; a `search` route (EN/HU twins,
  same token check/headers/404 philosophy) serves a no-JS `<form
  method=GET>` and server-rendered results (bm25 order, `snippet()`
  excerpts, deep links). Query strings never contain the token beyond the
  path it already lives in; results pages carry the same
  no-store/no-referrer headers as every HTML response. Owner UX pass: the
  index page's own filter box now also live-fetches this route
  (`?fragment=1`, a bare-results partial) and surfaces archive hits in-page
  as you type, deduped against what's already in the rendered ledger; the
  standalone search page above stays the no-JS/deep-link path, unchanged.
- [x] **Ingest v3: story arcs (site side)** — a numbered migration adds a
  nullable `topics` column; `PUT /ingest/:id` accepts an OPTIONAL
  validated `topics` array (slug + label, same shape discipline as
  `source_counts`; absent = old payloads byte-identical). Digest pages
  render an arc line for topics that also appeared in the prior 7 days
  ("×3 this week"), turning isolated briefings into visible threads. Site
  ships first; nothing renders until the app sends data.
- [x] **(digest repo) emit topics** — the app derives per-digest topic
  slugs/labels (prompt contract + deterministic parse), adds them to the
  site ingest payload; ships in the app's own release train (tag →
  homelab bump → deploy), after which arcs light up on new digests.

Steps 1–7 (and the site side of step 8) shipped 2026-08-09: migrations
0005/0006 applied `--remote`, then `wrangler deploy` (version e4511cca);
public-endpoint smoke test passed. The digest-repo step merged as
notification-digest PR #56 and rides that repo's own release train. Still deferred, unchanged:
PWA/offline (the capability-token-in-persistent-storage wrinkle stands).

## Since roadmap 4 (PLAN.md §11)

Roadmaps 1–4 above are a closed historical record. Work after them is tracked in
the digest service's `PLAN.md` (**that lives in the `notification-digest` repo,
not here** — the `PLAN.md §11.x` citations in `schema.sql`, the worker source, and the
`migrations/` headers all point there). What has shipped on the site side:

- **Story arcs** (§11.1) — arc pages at `a/:slug` reconstructed at request time by
  scanning `digests.topics` (JSON1 `json_each`); a `NOW` section on the
  current-week index showing the top active arcs; inline per-section "story so
  far" links from a digest's own headings into the arcs they continue. Briefs stay
  the single source of truth — arcs are a read-time view, never stored.
- **Stable arc identity** — topics gained an optional per-entry `key`. Identity is
  `key` when present, else `slug`, applied everywhere a story is grouped, linked,
  or counted. Rows stored before this keep their slug identity, so old `/a/<slug>`
  URLs resolve forever; this is additive, not a migration.
- **Delta persistence** (§11.3, migration `0007`) — an optional `deltas` array per
  digest, rendering the "What changed" block on digest pages and the
  previously/now line on each arc appearance. Deltas stay keyed on each digest's
  own `slug`, deliberately, because the app matches a delta to a heading *within*
  one digest, never across the arc.
- **Context primers** (§11.6, migration `0008`) — one durable background explainer
  per arc in the `arc_context` table, upserted alongside a normal ingest.
  `context_md` is untrusted model output: this Worker has no markdown renderer and
  renders it as escaped plain-text paragraphs, never HTML.
- **Model provenance** (migration `0010`) — an optional `provenance` object per
  digest (`{summarize: {model, effort, fallback}, translate?: {...}}`),
  rendering a "WRITTEN" colophon row directly below the source key: which
  model summarized the brief, which model translated it (when this digest
  has a Hungarian version), and whether an OpenRouter fallback model served
  in place of the primary Claude call for either leg (marked with `↻` and a
  muted chip, same "glyph is the marker, not color" posture as the source
  key's failed-source pills).
- **Catch-up banner and follow list** (§11.2) — client-side, reusing the unread
  fence's `localStorage` stamp.
- **Navigation** — soft navigation (internal steps swap in place), a ⌘K command
  palette, j/k list navigation, and an Archive nav link.
- **Settings and reading** — a settings bubble holding the three-state theme
  toggle, text size, ledger density, and a Sans | Serif body-font toggle; a Front
  Page print-poster stylesheet; an About page for anyone the capability link gets
  shared with.

The execution contract from roadmaps 1–4 still holds, with one clarified term:
a single DEPLOYED script — the source is split across `worker.js` (entry:
routes + dispatch) and `src/` modules (config, auth, http, dates, strings,
hrefs, css, client, chrome, sections, ingest, handlers, render-*), which
wrangler's built-in bundler folds back into one self-contained script at
deploy time, no build configuration involved. Inline CSS, no external
requests, EN/HU parity, both themes, a working no-JS baseline, and the
trust-model headers stay load-bearing exactly as before. The golden tests
under `test/` (npm test) hold every refactor to byte-identical output.

**No longer deferred: PWA + push** (notification-digest `PLAN.md` §11.7,
shipped and deployed 2026-08-27). Roadmaps 1–4 above deferred this four
times over one objection — "the capability token would end up in a
persisted, cached artifact" — and §11.7 splits that objection in two. The
token landing on disk is already true of browser history and any bookmark,
so an installed shortcut is the same exposure class, not a new one. Private
CONTENT landing on disk is the part that stands, and it becomes the
feature's first guardrail: the service worker uses no Cache API at all.
What ships is installable + push-on-arrival, explicitly NOT the offline
reader the old deferral bundled it with — `Cache-Control: private,
no-store` stays honest. The manifest is served token-scoped at
`/t/<token>/manifest.webmanifest`, behind the same gate and the same
indistinguishable 404 as every other route, which answers the companion
"fetched tokenless" objection outright.
