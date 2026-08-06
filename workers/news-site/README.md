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
