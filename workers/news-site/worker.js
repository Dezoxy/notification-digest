/**
 * news-site: the private digest archive for the notification-digest service
 * (github.com/Dezoxy/notification-digest).
 *
 * ACCESS MODEL — capability links, not login:
 *   The whole site lives under an unguessable token path (`/t/<SITE_TOKEN>/`)
 *   shared once in a private Telegram group. There is deliberately no login
 *   form and no Cloudflare Access on this hostname (see
 *   cloudflare-terraform's news_site.tf and modules/access's
 *   local.public_bypass_apps carve-out) — the token in the URL IS the auth.
 *   Anyone who has the URL can read; anyone who doesn't gets an
 *   indistinguishable 404, same as any other wrong path.
 *
 * Two separate secrets, two separate audiences:
 *   - SITE_TOKEN — the reader-facing capability token, part of the URL path.
 *   - INGEST_KEY — a machine credential the digest VM sends as `x-ingest-key`
 *     to push new digests. Never exposed to readers.
 *   Both are compared timing-safely (hash-then-timingSafeEqual — see
 *   keyMatches below), the same pattern as the sibling polymarket-proxy
 *   Worker. Neither secret lives in source or wrangler.jsonc; both are set
 *   with `wrangler secret put`.
 *
 * Routes: URL grammar is /t/:token/(hu/)?(daily/|weekly/)?( | d/:id) plus
 * the standalone /t/:token/(hu/)?search, /t/:token/(hu/)?a/:slug, and
 * /t/:token/(hu/)?about endpoints — the language segment always comes
 * first, then either an optional literal "daily/" or "weekly/" view
 * segment, the literal "search" endpoint, the literal "a/:slug" arc-page
 * endpoint, or the literal "about" endpoint (search, arc, and about pages
 * have no daily/weekly/week variant of their own — each spans, or stands
 * outside, the whole archive rather than belonging to one view). No view
 * segment is the ALL view (every digest, mixed).
 *   GET  /robots.txt              -> disallow everything, no token needed
 *   PUT  /ingest/:id              -> upsert a digest (x-ingest-key required)
 *   GET  /t/:token/               -> index (EN, all view), newest-first, grouped by day
 *   GET  /t/:token/d/:id          -> single digest (EN, all view), with prev/next nav
 *   GET  /t/:token/daily/         -> same index, EN, filtered to kind='daily' only
 *   GET  /t/:token/daily/d/:id    -> same digest page, EN, prev/next stays within kind='daily'
 *   GET  /t/:token/weekly/        -> same index, EN, filtered to kind='weekly' only
 *   GET  /t/:token/weekly/d/:id   -> same digest page, EN, prev/next stays within kind='weekly'
 *   GET  /t/:token/hu/            -> same index, Hungarian chrome + translations, all view
 *   GET  /t/:token/hu/d/:id       -> same digest page, Hungarian chrome + translations, all view
 *   GET  /t/:token/hu/daily/      -> same index, Hungarian chrome, daily view
 *   GET  /t/:token/hu/daily/d/:id -> same digest page, Hungarian chrome, daily view
 *   GET  /t/:token/hu/weekly/     -> same index, Hungarian chrome, weekly view
 *   GET  /t/:token/hu/weekly/d/:id -> same digest page, Hungarian chrome, weekly view
 *   GET  /t/:token/search         -> full-text search (EN), query text in ?q=, whole archive
 *   GET  /t/:token/hu/search      -> same search, Hungarian chrome
 *   GET  /t/:token/a/:slug        -> arc page (EN): every digest carrying that topic slug
 *   GET  /t/:token/hu/a/:slug     -> same arc page, Hungarian chrome
 *   GET  /t/:token/about          -> about page (EN): static, no D1 query beyond the token check
 *   GET  /t/:token/hu/about       -> same about page, Hungarian chrome
 *   anything else                 -> plain 404, wrong token included
 *
 * Story-arc pages (PLAN.md §11.1 PR A, this feature): a slug is the digest
 * app's own `derive_topics` fold of a section heading (notification-digest
 * repo, digest/publish.py) — the SAME slug already used for the "×N this
 * week" arc line on digest pages (ingest v3, roadmap 4 step 8). An arc page
 * is DERIVED, never stored: handleArcPage scans `digests.topics` (SQLite
 * JSON1 via json_each) for every digest carrying the slug and reconstructs
 * the chain at request time — briefs stay the single source of truth, arcs
 * are just a read-time view over them (§11.1 guardrail: no storyline-first
 * storage inversion). The slug-stability contract this depends on
 * (`_slugify`, notification-digest repo) and the digest-HTML-to-#sN-anchor
 * coupling (buildSectionToc below) are now PUBLIC URL/link contracts, not
 * just internal join keys — see findArcSectionAnchor's comment for how a
 * deep link degrades safely (never wrongly) when that coupling can't be
 * resolved unambiguously.
 *
 * Stable arc keys (PLAN.md §11.1 site half, this feature's second cut): a
 * heading gets reworded run to run, so plain slug identity fragments one
 * continuing story into many never-recurring slugs. Topics gained an
 * OPTIONAL per-entry `key` for this reason — a short, stable identifier the
 * digest app derives once and carries across runs. "Arc identity" is now
 * `key` when present, else `slug` (see arcIdentity/ARC_IDENTITY_SQL), applied
 * everywhere a story is grouped, linked, or counted: handleArcPage's chain
 * query, computeNowArcs' grouping, every arcHref call site, and
 * handleDigestPage's topicArcs recurrence count. Every row stored before
 * this change has topics WITHOUT `key`, so its identity is unchanged (its
 * own slug) and `/a/<that-slug>` keeps resolving forever — this is additive
 * identity plumbing, not a slug migration. Deltas are the deliberate
 * exception: they stay keyed on each digest's own `slug` (see renderDeltas
 * and the per-appearance delta match in handleArcPage), because
 * map_deltas_to_slugs matches a delta to a heading WITHIN one digest, never
 * across the arc.
 *
 * Hungarian support (EN | HU switcher in the masthead): this Worker only
 * STORES and SERVES translations — it never translates anything itself.
 * The digest app optionally sends tldr_hu/body_html_hu/body_md_hu alongside
 * the English fields on PUT /ingest/:id; all three are NULL together when
 * the app didn't produce a translation for that digest, and the /hu/ pages
 * fall back to the English tldr/body_html with a small in-page note when
 * that happens. The /hu/ routes are parameterized variants of the same
 * handlers, same token check, same headers, same 404 philosophy — a wrong
 * token on a /hu/ path 404s byte-identically to a wrong token anywhere else.
 *
 * Daily-brief view (All | Daily | Weekly switcher in the masthead, next to
 * EN | HU): every digest carries a `kind` column, 'window' (the regular
 * 3-hourly digest, the default), 'daily' (the once-daily 20:00 synthesis),
 * or 'weekly' (the once-a-week Sunday-evening synthesis of the week's daily
 * briefs). The "daily/" URL segment filters the index to kind='daily' ONLY,
 * and "weekly/" filters it to kind='weekly' ONLY — each kind is never in
 * the OTHER kind's view, only in the All view alongside every other kind,
 * badged the same way — and constrains a digest page's prev/next to that
 * same kind too, so a reader in one of those views hops brief-to-brief
 * instead of through every window digest (or the other brief kind) in
 * between. Since a digest of a different kind has no home in the daily or
 * weekly view, the view switcher's "other view" link ALWAYS points at that
 * view's index, never at a digest page — true on the index itself
 * (index -> index, the obvious case) and also when switching view away from
 * a digest page (digest -> that view's index, because the current digest
 * may not exist in the target view).
 *
 * body_html and body_html_hu both arrive PRE-SANITIZED by the app (nh3) and
 * are stored/served verbatim — they are the only fields ever inserted into
 * a response without HTML-escaping, and only ever into the <article> slot.
 * Every other D1-sourced value goes through esc().
 */

import { notFound } from "./src/http.js";
import { isoWeeksInYear } from "./src/dates.js";
import { viewSeg } from "./src/hrefs.js";
import { FAVICON_SVG } from "./src/chrome.js";
import { handleIngest } from "./src/ingest.js";
import {
  handleAboutPage,
  handleArcPage,
  handleDigestPage,
  handleIndexPage,
  handleSearchPage,
} from "./src/handlers.js";

// ── entry point ─────────────────────────────────────────────────────────

// Route patterns, hoisted: a regex literal inside fetch() is re-created on
// every request. The URL grammar itself is documented in the file header —
// these are that table, in the dispatch order below.
// The URL grammar's daily/weekly segment folded to a view name — the same
// fold both index and digest dispatch need, and the exact inverse of
// viewSeg() over in the href builders.
function viewFromSeg(seg) {
  return seg === "daily/" ? "daily" : seg === "weekly/" ? "weekly" : "all";
}

const ROUTE_INGEST = /^\/ingest\/(\d+)$/;

const ROUTE_DIGEST = /^\/t\/([^/]+)\/(hu\/)?(daily\/|weekly\/)?d\/(\d+)$/;

const ROUTE_SEARCH = /^\/t\/([^/]+)\/(hu\/)?search$/;

const ROUTE_ARC = /^\/t\/([^/]+)\/(hu\/)?a\/([a-z0-9-]{1,64})$/;

const ROUTE_ABOUT = /^\/t\/([^/]+)\/(hu\/)?about$/;

const ROUTE_INDEX = /^\/t\/([^/]+)\/(hu\/)?(daily\/|weekly\/)?(?:w\/(\d{4})-W(\d{2})\/)?$/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (request.method === "GET" && path === "/robots.txt") {
      return new Response("User-agent: *\nDisallow: /", {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    // Tokenless like robots.txt: an icon reveals nothing about the site's
    // content (robots.txt already concedes "something is served here"), and
    // browsers fetch favicons before any page-level auth context exists.
    if (request.method === "GET" && path === "/favicon.svg") {
      return new Response(FAVICON_SVG, {
        headers: {
          "content-type": "image/svg+xml",
          // Icons are immutable-ish and requested constantly — long cache,
          // unlike the no-store pages (the icon carries no private data).
          "cache-control": "public, max-age=86400",
        },
      });
    }

    const ingestMatch = path.match(ROUTE_INGEST);
    if (ingestMatch) {
      if (request.method !== "PUT") return notFound();
      return handleIngest(request, env, ingestMatch[1]);
    }

    // The optional "hu/" segment selects the Hungarian chrome/translations,
    // and the optional "daily/" or "weekly/" segment (only ever AFTER "hu/",
    // never before) selects that brief-only view; everything else about the
    // route (token check, id shape, 404s) is identical across every
    // language×view combination — see handleIndexPage/handleDigestPage,
    // which take `lang` and `view` as plain parameters rather than being
    // duplicated per combination.
    const digestMatch = path.match(ROUTE_DIGEST);
    if (digestMatch && request.method === "GET") {
      const lang = digestMatch[2] ? "hu" : "en";
      const view = viewFromSeg(digestMatch[3]);
      return handleDigestPage(env, digestMatch[1], digestMatch[4], url, lang, view);
    }

    // Search (roadmap 4 step 7): a standalone endpoint, not part of the
    // index/digest grammar below — no "daily/"/"weekly/" or "w/" variant
    // (search spans the whole archive, see the file-header comment). Query
    // text comes from url.searchParams, never the path.
    const searchMatch = path.match(ROUTE_SEARCH);
    if (searchMatch && request.method === "GET") {
      const lang = searchMatch[2] ? "hu" : "en";
      return handleSearchPage(env, searchMatch[1], url, lang);
    }

    // Arc page (§11.1 PR A): also standalone, no daily/weekly/w/ variant,
    // same reasoning as search — a story arc spans the whole archive, not
    // one view. The slug shape (`[a-z0-9-]{1,64}`) is enforced RIGHT HERE,
    // in the route regex, the same way the digest-id route above enforces
    // `\d+` — anything a shorter/looser check would catch never even
    // reaches handleArcPage, so a malformed slug 404s before touching the
    // DB by construction, not by a separate guard the handler has to
    // remember to run first.
    const arcMatch = path.match(ROUTE_ARC);
    if (arcMatch && request.method === "GET") {
      const lang = arcMatch[2] ? "hu" : "en";
      return handleArcPage(env, arcMatch[1], arcMatch[3], url, lang);
    }

    // About page (static, no-JS, no D1 query beyond the token check): also
    // standalone, same shape as search/arc just above — no daily/weekly/w/
    // variant, since the about text doesn't depend on which view a reader
    // came from. Reuses the exact same token-gate/404 contract as every
    // other route here (see handleAboutPage) — a wrong token 404s
    // byte-identically to a wrong token anywhere else.
    const aboutMatch = path.match(ROUTE_ABOUT);
    if (aboutMatch && request.method === "GET") {
      const lang = aboutMatch[2] ? "hu" : "en";
      return handleAboutPage(env, aboutMatch[1], url, lang);
    }

    // Roadmap 3 (weekly pagination): one optional, always-LAST segment,
    // `w/YYYY-Www/` — the digest-page regex above stays untouched, digest
    // pages have no week address (prev/next crosses week boundaries
    // invisibly, unchanged). Root index (no w/ segment) = the current week.
    const indexMatch = path.match(ROUTE_INDEX);
    if (indexMatch && request.method === "GET") {
      const lang = indexMatch[2] ? "hu" : "en";
      const view = viewFromSeg(indexMatch[3]);
      let weekParam = null;
      if (indexMatch[4] !== undefined) {
        const year = Number(indexMatch[4]);
        const week = Number(indexMatch[5]);
        // Strict shape/value validation: week 1-53, and 53 only for ISO
        // years that actually have a 53rd week. Anything else 404s
        // indistinguishably from an unknown path (same trust model as
        // every other route here — no hint that the shape was "close").
        // Valid-shaped FUTURE weeks are deliberately allowed through (they
        // just render empty) — see handleIndexPage.
        if (week < 1 || week > 53 || (week === 53 && isoWeeksInYear(year) !== 53)) {
          return notFound();
        }
        weekParam = { year, week };
      }
      return handleIndexPage(env, indexMatch[1], url, lang, view, weekParam);
    }

    // Unknown path, or a token-gated route hit with the wrong method — same
    // response either way, so route shape never leaks anything either.
    return notFound();
  },
};
