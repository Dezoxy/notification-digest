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
 * the standalone /t/:token/(hu/)?search and /t/:token/(hu/)?a/:slug
 * endpoints — the language segment always comes first, then either an
 * optional literal "daily/" or "weekly/" view segment, the literal "search"
 * endpoint, or the literal "a/:slug" arc-page endpoint (search and arc pages
 * have no daily/weekly/week variant of their own — both span the whole
 * archive, not one view). No view segment is the ALL view (every digest,
 * mixed).
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

// ── config ──────────────────────────────────────────────────────────────

const TIMEZONE = "Europe/Budapest";

// Per-field caps ("sanely" bounded, not exact science): body_html/body_md are
// full digest bodies and can legitimately run long; tldr is a one-paragraph
// summary and should never approach that size. The optional _hu translation
// counterparts share these exact same caps.
const MAX_BODY_FIELD_BYTES = 2 * 1024 * 1024; // 2MB
const MAX_TLDR_BYTES = 32 * 1024; // 32KB

// Overall request ceiling. Two 2MB text fields JSON-encoded (escaping can
// expand multi-byte/control characters) plus the smaller fields and JSON
// structure overhead — 8MB leaves comfortable headroom without being an
// effectively unbounded accept-anything limit.
const MAX_REQUEST_BYTES = 8 * 1024 * 1024;

// Ingest v2 (roadmap 2 step 8): source_counts/failed_sources are OPTIONAL,
// backward-compatible fields on PUT /ingest/:id (see validateDigestPayload).
// Deliberately no hardcoded list of "the app's known sources" here — the
// site doesn't own that list, the digest app does, and a new collector must
// never require a site deploy to start reporting. A source name only has to
// match this shape; both fields cap at 16 entries as a sane ceiling on an
// app that currently has five collectors.
const SOURCE_NAME_RE = /^[a-z][a-z0-9_-]{0,31}$/;
const MAX_SOURCE_ENTRIES = 16;

// Ingest v3 (roadmap 4 step 8): topics is an OPTIONAL, backward-compatible
// field on PUT /ingest/:id (see validateDigestPayload/validateTopics), same
// shape-discipline pattern as source_counts/failed_sources above. Slugs are
// caller-chosen (the digest app derives them, the site doesn't own the
// vocabulary), lowercase-and-dash only so they're safe to use as-is if a
// future step ever needs them in a URL; 12 is a sane ceiling on how many
// distinct threads one briefing legitimately touches.
const TOPIC_SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
const MAX_TOPICS = 12;

// Stable arc keys (PLAN.md §11.1 site half): topics gains an OPTIONAL
// per-entry `key` alongside slug/label (validateTopics) — a short, stable
// identifier for the ongoing story that the digest app (parallel change,
// notification-digest repo) derives once and carries across runs, so a
// story survives its section heading — and therefore its `_slugify`-folded
// slug — being reworded run to run. No new top-level ingest field and no
// schema change: `key` rides inside the existing `topics` JSON column, same
// shape discipline as `slug` (TOPIC_SLUG_RE) plus a tighter length cap (a
// key is a short hand-chosen identifier, never a folded full heading like a
// slug can be). Every row ingested before this change has topics WITHOUT
// `key` — those keep behaving exactly as before, which is what arcIdentity/
// ARC_IDENTITY_SQL just below exist to guarantee.
const ARC_KEY_MAX_LEN = 48;

// Arc identity: the ONE notion of "which story is this" used everywhere an
// arc is grouped, linked, or counted — handleArcPage's chain query,
// computeNowArcs' grouping, arcHref call sites (renderArcs' chips,
// renderNowSection's rows, renderArcPage's own language switcher), and
// handleDigestPage's topicArcs recurrence count. `key` wins when the topic
// entry carries one; else `slug` — so a pre-key row (every row stored before
// this change) resolves to the same slug it always had, and `/a/<that-slug>`
// keeps resolving unchanged forever. Deltas are the one deliberate exception
// (see renderDeltas and handleArcPage's per-appearance delta match): they
// stay keyed on each digest's OWN `slug`, because map_deltas_to_slugs
// (notification-digest repo, digest/publish.py) matches a delta to a heading
// WITHIN one digest, never across the arc — identity has nothing to do with
// that match, and nothing here changes it.
//
// Two equivalent forms of the same fold: arcIdentity() for JS objects
// already parsed out of a `topics` column, ARC_IDENTITY_SQL for the same
// fold expressed as SQL over json_each(d.topics)'s `je.value` — one named
// concept, not reimplemented ad hoc at each call site.
const ARC_IDENTITY_SQL = "COALESCE(je.value->>'key', je.value->>'slug')";

function arcIdentity(topicEntry) {
  return topicEntry?.key ?? topicEntry?.slug ?? null;
}

// Deltas (§11.3 delta persistence, ingest v4): optional, backward-compatible
// field on PUT /ingest/:id (see validateDigestPayload/validateDeltas), same
// shape-discipline pattern as topics just above. MAX_DELTAS reuses
// MAX_TOPICS' own value rather than a second hardcoded 12: a digest can have
// at most MAX_TOPICS topics in the first place, so it can never legitimately
// carry more deltas than that either — mirrors digest/summarize.py's own
// _MAX_DELTAS=12 parity constant on the app side, which reasons the same
// way. DELTA_TEXT_MAX_LEN bounds `previously`/`now`: each is ONE
// LLM-generated sentence by prompt contract (prompts/digest.md, "one
// sentence: what changed"), and English news prose runs roughly 100-200
// characters per sentence, so 400 gives a genuinely long sentence several
// times its usual headroom without accepting a whole paragraph. Picked as a
// generous multiple of the 80-char topic LABEL cap above (a label is a
// truncated heading fragment; a delta sentence is a full clause, so it
// earns a bigger cap) rather than reusing MAX_TLDR_BYTES's 32KB, which is
// sized for a multi-sentence paragraph, not one sentence.
const MAX_DELTAS = MAX_TOPICS;
const DELTA_TEXT_MAX_LEN = 400;

// Arc pages (§11.1 PR A, handleArcPage): how many of an arc's NEWEST
// appearances get their digest's body_html fetched for #sN deep-link
// resolution. 24 ≈ three days of 3-hourly windows — the span a reader
// plausibly navigates into from a live arc page; every older appearance
// links to its digest fragment-free (the feature's documented degraded
// mode). Bounds the per-request body_html haul regardless of how many
// hundreds of appearances an arc accumulates over months.
const ARC_ANCHOR_BODIES = 24;

// ── entry point ─────────────────────────────────────────────────────────

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

    const ingestMatch = path.match(/^\/ingest\/(\d+)$/);
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
    const digestMatch = path.match(/^\/t\/([^/]+)\/(hu\/)?(daily\/|weekly\/)?d\/(\d+)$/);
    if (digestMatch && request.method === "GET") {
      const lang = digestMatch[2] ? "hu" : "en";
      const view = digestMatch[3] === "daily/" ? "daily" : digestMatch[3] === "weekly/" ? "weekly" : "all";
      return handleDigestPage(env, digestMatch[1], digestMatch[4], url, lang, view);
    }

    // Search (roadmap 4 step 7): a standalone endpoint, not part of the
    // index/digest grammar below — no "daily/"/"weekly/" or "w/" variant
    // (search spans the whole archive, see the file-header comment). Query
    // text comes from url.searchParams, never the path.
    const searchMatch = path.match(/^\/t\/([^/]+)\/(hu\/)?search$/);
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
    const arcMatch = path.match(/^\/t\/([^/]+)\/(hu\/)?a\/([a-z0-9-]{1,64})$/);
    if (arcMatch && request.method === "GET") {
      const lang = arcMatch[2] ? "hu" : "en";
      return handleArcPage(env, arcMatch[1], arcMatch[3], url, lang);
    }

    // Roadmap 3 (weekly pagination): one optional, always-LAST segment,
    // `w/YYYY-Www/` — the digest-page regex above stays untouched, digest
    // pages have no week address (prev/next crosses week boundaries
    // invisibly, unchanged). Root index (no w/ segment) = the current week.
    const indexMatch = path.match(/^\/t\/([^/]+)\/(hu\/)?(daily\/|weekly\/)?(?:w\/(\d{4})-W(\d{2})\/)?$/);
    if (indexMatch && request.method === "GET") {
      const lang = indexMatch[2] ? "hu" : "en";
      const view = indexMatch[3] === "daily/" ? "daily" : indexMatch[3] === "weekly/" ? "weekly" : "all";
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

// ── route handlers ──────────────────────────────────────────────────────

async function handleIngest(request, env, idParam) {
  const id = Number(idParam);
  if (!Number.isInteger(id) || id <= 0) {
    return badRequest("id must be a positive integer");
  }

  // Fail closed if the secret was never set — an unauthenticated ingest
  // endpoint is worse than a broken one.
  const provided = request.headers.get("x-ingest-key") ?? "";
  if (!env.INGEST_KEY || !(await keyMatches(provided, env.INGEST_KEY))) {
    return json({ error: "unauthorized" }, 401);
  }

  const contentLength = Number(request.headers.get("content-length") ?? "0");
  if (contentLength > MAX_REQUEST_BYTES) {
    return badRequest("payload too large");
  }

  let raw;
  try {
    raw = await request.text();
  } catch {
    return badRequest("could not read request body");
  }
  if (byteLength(raw) > MAX_REQUEST_BYTES) {
    return badRequest("payload too large");
  }

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    return badRequest("body must be valid JSON");
  }

  const validated = validateDigestPayload(payload);
  if (!validated.ok) {
    return badRequest(validated.error);
  }
  const d = validated.value;

  try {
    await env.DB.prepare(
      `INSERT INTO digests
         (id, created_at, tldr, item_count, section_count, has_attention, body_html, body_md, tldr_hu, body_html_hu, body_md_hu, kind, source_counts, failed_sources, topics, deltas)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET
         created_at     = excluded.created_at,
         tldr           = excluded.tldr,
         item_count     = excluded.item_count,
         section_count  = excluded.section_count,
         has_attention  = excluded.has_attention,
         body_html      = excluded.body_html,
         body_md        = excluded.body_md,
         tldr_hu        = excluded.tldr_hu,
         body_html_hu   = excluded.body_html_hu,
         body_md_hu     = excluded.body_md_hu,
         kind           = excluded.kind,
         source_counts  = excluded.source_counts,
         failed_sources = excluded.failed_sources,
         topics         = excluded.topics,
         deltas         = excluded.deltas`,
    )
      .bind(
        id,
        d.created_at,
        d.tldr,
        d.item_count,
        d.section_count,
        d.has_attention ? 1 : 0,
        d.body_html,
        d.body_md,
        // NULL when the app didn't send a translation for this digest —
        // ON CONFLICT's excluded.* means a re-ingest of a previously
        // translated digest without hu fields correctly NULLs them back out,
        // same idempotent-upsert contract as every other column here.
        d.tldr_hu,
        d.body_html_hu,
        d.body_md_hu,
        d.kind,
        // Same NULL-on-absence, NULL-back-out-on-re-ingest contract as the hu
        // fields above (roadmap 2 step 8) — already JSON-stringified (or
        // null) by validateDigestPayload.
        d.source_counts,
        d.failed_sources,
        // Same NULL-on-absence, NULL-back-out-on-re-ingest contract (ingest
        // v3, roadmap 4 step 8) — already JSON-stringified (or null) by
        // validateDigestPayload.
        d.topics,
        // Same NULL-on-absence, NULL-back-out-on-re-ingest contract (§11.3
        // delta persistence, ingest v4) — already JSON-stringified (or null)
        // by validateDigestPayload.
        d.deltas,
      )
      .run();
  } catch {
    return json({ error: "database error" }, 500);
  }

  return json({ ok: true }, 200);
}

async function handleIndexPage(env, token, url, lang, view, weekParam) {
  if (!(await tokenMatches(env, token))) return notFound();

  // Weekly pagination (roadmap 3 step 2): the daily and weekly views stay
  // unpaginated (~3 years from feeling the old LIMIT-1000 backstop, and a
  // weekly brief a week is even further out — see the weekly branch below)
  // — the route match above still grammatically allows "daily/w/…" or
  // "weekly/w/…" (the "w/" segment can follow any view prefix), so an
  // unpaginated view being asked for a week address 404s here rather than
  // silently ignoring the segment or rendering something misleading for a
  // URL that has no real page behind it.
  if (view !== "all" && weekParam) return notFound();

  // The week actually being rendered: the URL's w/ segment if present,
  // otherwise the current Budapest-local ISO week. NOTE: this step (roadmap
  // 3 "Core week machinery") deliberately does NOT gate the lead
  // card/pulse strip/countdown/prefetch hint to the current week only —
  // that's the next step ("Feature scoping"). They keep rendering
  // unconditionally here, which can look a little odd on an archive week
  // page (e.g. a "Latest" card that isn't) — expected and fine for now.
  const current = isoWeekOf(new Date());
  const effective = weekParam ?? current;
  const isCurrentWeek = compareIsoWeek(effective, current) === 0;

  let results;
  let weekInfo = null;
  if (view === "daily") {
    // kind='daily' ONLY — a weekly brief never appears here, it lives in
    // the All view (see the file-header "Daily-brief view" comment). Also
    // unbounded, exactly as before this step — see the all-view branch
    // below for why the old flat LIMIT 1000 was a page-weight backstop, not
    // real pagination; the daily view isn't getting real pagination here.
    // tldr_hu is always selected (cheap) even for the EN page — only the HU
    // renderer reads it.
    const { results: dailyResults } = await env.DB.prepare(
      `SELECT id, created_at, tldr, tldr_hu, item_count, section_count, has_attention, kind, source_counts, failed_sources FROM digests WHERE kind = 'daily' ORDER BY created_at DESC, id DESC LIMIT 1000`,
    ).all();
    results = dailyResults;
  } else if (view === "weekly") {
    // Same shape as the daily branch above, kind='weekly' ONLY — a daily (or
    // window) digest never appears here, it lives in the All view. Also
    // unbounded and unpaginated, same reasoning as daily — a weekly brief a
    // week is ~50 rows a year, even further from the LIMIT-1000 backstop
    // than the daily view's ~365 rows a year, so real pagination is even
    // less warranted here.
    const { results: weeklyResults } = await env.DB.prepare(
      `SELECT id, created_at, tldr, tldr_hu, item_count, section_count, has_attention, kind, source_counts, failed_sources FROM digests WHERE kind = 'weekly' ORDER BY created_at DESC, id DESC LIMIT 1000`,
    ).all();
    results = weeklyResults;
  } else {
    // Week-bounded query replaces the old flat LIMIT-1000 backstop for the
    // all view. created_at is UTC ISO text, so a lexicographic >=/< against
    // the UTC boundary strings from weekBoundsUtc is a correct comparison
    // without parsing — same trick every other created_at comparison in
    // this file already relies on. Order by created_at, not id: daily
    // briefs get BACKFILLED for past days, so a backfilled row can have a
    // high id but an old, historical created_at; `id DESC` stays only as a
    // deterministic tiebreak for same-instant rows. groupByDay relies on
    // this ordering to put each row in its correct day bucket.
    const { startIso, endIso } = weekBoundsUtc(effective.year, effective.week);
    const { results: weekResults } = await env.DB.prepare(
      `SELECT id, created_at, tldr, tldr_hu, item_count, section_count, has_attention, kind, source_counts, failed_sources FROM digests WHERE created_at >= ? AND created_at < ? ORDER BY created_at DESC, id DESC LIMIT 1000`,
    )
      .bind(startIso, endIso)
      .all();
    results = weekResults;

    // Oldest-week probe: one MIN(created_at) over the WHOLE table — not
    // week-bounded, not kind-filtered, mirroring the all view's own "every
    // digest, mixed" scope — locating the earliest week that has ever had
    // data. The rail's "older" link renders only when the previous week is
    // still >= that floor; a mid-range week with no data in between still
    // gets its own page (empty state + rail), it just isn't itself a valid
    // "older" TARGET past the floor.
    const oldestRow = await env.DB.prepare("SELECT MIN(created_at) AS oldest FROM digests").first();
    const oldestWeek = oldestRow?.oldest ? isoWeekOf(new Date(oldestRow.oldest)) : null;
    const prevWeek = adjacentWeek(effective.year, effective.week, -1);
    const olderAllowed = Boolean(oldestWeek) && compareIsoWeek(prevWeek, oldestWeek) >= 0;
    // "Newer" only ever points toward the present: a future week (valid-
    // shaped but effective > current) gets no newer link either, same as
    // the current week itself.
    const newerAllowed = compareIsoWeek(effective, current) < 0;

    weekInfo = {
      year: effective.year,
      week: effective.week,
      isCurrentWeek,
      older: olderAllowed ? prevWeek : null,
      newer: newerAllowed ? adjacentWeek(effective.year, effective.week, 1) : null,
    };
  }

  // Next-briefing countdown (roadmap 2 step 3): the newest WINDOW digest's
  // created_at, found in the rows already fetched above rather than an extra
  // query — rows are created_at DESC, so this is just the first kind='window'
  // row. In the daily or weekly view, `results` is already restricted to
  // kind='daily' or kind='weekly' only, so no window row is ever found there
  // and the countdown paragraph is simply omitted (see pageChrome's
  // countdownNewest param) — cheap, no special-casing needed for those views.
  const newestWindow = (results ?? []).find((row) => row.kind === "window");

  // Current-week-only (roadmap 3 step 3): a countdown to "the next window"
  // only makes sense on the page showing the actual present — on an archive
  // week it would read "closing about now" forever, since that week's
  // newest window digest closed long ago. isCurrentWeek is true
  // unconditionally in the daily and weekly views too (weekParam is always
  // null there — see the route match in fetch() — so effective === current
  // above), which is exactly the "daily/weekly view keeps every living-
  // chrome feature" contract.
  const countdownNewest = isCurrentWeek ? (newestWindow?.created_at ?? null) : null;

  // NOW section (§11.1 PR B, "homepage becomes NOW"): only queried on the
  // page that will actually render it — the CURRENT-week ALL view, no w/
  // segment (weekParam === null implies effective === current, see above,
  // but the check is on weekParam itself, not isCurrentWeek: the section is
  // gated on "no w/ segment" specifically, the exact URL grammar the §11.1
  // spec calls out, not merely "happens to resolve to the current week").
  // Every other view/week pays zero extra roundtrip for this section — see
  // renderIndexPage's own no-op fallback when nowArcs stays [].
  //
  // showCatchup (§11.2): the catch-up banner's gate, written out as its own
  // const — literally the same boolean expression as the NOW section's just
  // above ("same gate as the NOW section" per the §11.2 spec, not merely
  // "happens to agree with it today"). Passed through to renderIndexPage
  // separately from nowArcs because the banner must still be ABLE to render
  // (N briefings only, M omitted) in the 0-eligible-arcs case where nowArcs
  // stays [] and the NOW section itself renders nothing — inferring the gate
  // from nowArcs.length would wrongly suppress the banner shell then.
  const showCatchup = view === "all" && weekParam === null;
  let nowArcs = [];
  const nowMs = Date.now();
  if (view === "all" && weekParam === null) {
    // One query, JS-side aggregation (see computeNowArcs's comment for why
    // GROUP BY + window functions are deliberately NOT used here): every
    // topic appearance in the trailing 7 days, anchored at THIS request's
    // own instant, not any digest's created_at — a live "now" section is
    // expected to re-rank on every visit, unlike a digest's own reproducible
    // arc line (contrast handleDigestPage's topicArcs windowStartIso, which
    // anchors at the digest's own created_at instead).
    const nowWindowStartIso = new Date(nowMs - 7 * 24 * 3600000).toISOString();
    // `identity` (stable arc keys, ARC_IDENTITY_SQL) is what computeNowArcs
    // actually groups by — the fix for the bug this section exists to solve:
    // a story recurring under several differently-worded (and therefore
    // differently-slugged) headings now clusters into ONE arc instead of
    // several never-eligible 1-appearance ones.
    const { results: nowRows } = await env.DB.prepare(
      `SELECT je.value->>'label' AS label, ${ARC_IDENTITY_SQL} AS identity, d.created_at, d.id
         FROM digests d, json_each(d.topics) je
        WHERE d.topics IS NOT NULL AND d.created_at >= ?1
        ORDER BY d.created_at ASC`,
    )
      .bind(nowWindowStartIso)
      .all();
    nowArcs = computeNowArcs(nowRows ?? [], nowMs);
  }

  return htmlResponse(
    renderIndexPage(
      results ?? [],
      token,
      url.hostname,
      lang,
      view,
      countdownNewest,
      weekInfo,
      nowArcs,
      nowMs,
      showCatchup,
    ),
  );
}

// Deltas (§11.3 delta persistence, ingest v4): fail-safe parse of the
// `deltas` JSON column — same "unparseable or wrong-shaped -> treated as
// absent" contract as renderDegradedBadge/the topics parse just below
// (never throws, drops individually malformed entries rather than the whole
// array). A shared function, not inlined per call site like the topics
// parse below, because TWO read paths need it: handleDigestPage (this
// digest's own "What changed" block) and handleArcPage (each appearance's
// own delta entry, matched by slug) — one fail-safe rule for both, rather
// than two copies that could drift.
function parseDeltas(deltasJson) {
  if (!deltasJson) return null;
  let parsed;
  try {
    parsed = JSON.parse(deltasJson);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed)) return null;
  const wellFormed = parsed.filter(
    (d) =>
      d !== null &&
      typeof d === "object" &&
      !Array.isArray(d) &&
      typeof d.slug === "string" &&
      typeof d.previously === "string" &&
      typeof d.now === "string",
  );
  return wellFormed.length > 0 ? wellFormed : null;
}

async function handleDigestPage(env, token, idParam, url, lang, view) {
  if (!(await tokenMatches(env, token))) return notFound();

  const id = Number(idParam);
  if (!Number.isInteger(id) || id <= 0) return notFound();

  const digest = await env.DB.prepare(
    "SELECT id, created_at, tldr, item_count, section_count, has_attention, body_html, body_html_hu, kind, source_counts, failed_sources, topics, deltas FROM digests WHERE id = ?",
  )
    .bind(id)
    .first();
  if (!digest) return notFound();

  // Story-arc counts (ingest v3, roadmap 4 step 8): fail-safe parse, same
  // contract as renderDegradedBadge — an unparseable or wrong-shaped topics
  // value is treated as "no topics" rather than thrown.
  let topics = null;
  if (digest.topics) {
    try {
      const parsed = JSON.parse(digest.topics);
      if (Array.isArray(parsed)) {
        // Per-entry shape check too, not just "is an array" — same defense
        // against a stored value predating a validation change that every
        // other renderer here applies (renderDegradedBadge, renderSourceKey);
        // a wrong-shaped entry must drop out, not render "undefined".
        const wellFormed = parsed.filter(
          (t) => t !== null && typeof t === "object" && !Array.isArray(t) &&
            typeof t.slug === "string" && typeof t.label === "string",
        );
        if (wellFormed.length > 0) topics = wellFormed;
      }
    } catch {
      // unparseable -> treat as absent
    }
  }

  let topicArcs = null;
  if (topics) {
    // One extra query, paid only when the digest actually has topics. The
    // window is a TRAILING 7 days ending at THIS digest's own created_at
    // (exclusive upper bound, and id != this digest so it never counts
    // itself here) — not "now" — so an old digest's arc line is reproducible
    // history: it must not change as newer digests arrive after it.
    const windowStartIso = new Date(
      new Date(digest.created_at).getTime() - 7 * 86400000,
    ).toISOString();
    const { results: priorRows } = await env.DB.prepare(
      "SELECT topics FROM digests WHERE topics IS NOT NULL AND created_at >= ? AND created_at < ? AND id != ? LIMIT 200",
    )
      .bind(windowStartIso, digest.created_at, id)
      .all();

    // Per-digest, not per-occurrence: each prior row contributes at most one
    // count per arc IDENTITY (an identity set, not a running tally),
    // regardless of how many times that identity might otherwise appear.
    // Counting by identity (key when present, else slug — see arcIdentity)
    // rather than raw slug is the actual fix this recurrence count needed:
    // a story whose heading gets reworded every run now still accumulates
    // one shared count instead of fragmenting into several 1-count topics.
    const priorIdentitySets = priorRows
      .map((row) => {
        try {
          const parsed = JSON.parse(row.topics);
          if (!Array.isArray(parsed)) return null;
          return new Set(
            parsed.map((t) => arcIdentity(t)).filter((identity) => typeof identity === "string"),
          );
        } catch {
          return null;
        }
      })
      .filter((set) => set !== null);

    // count = prior occurrences + 1, i.e. total appearances including this
    // digest itself — a topic seen only here renders as count 1 (bare label,
    // see renderArcs). `identity` rides alongside `slug` on each entry:
    // renderArcs uses `identity` for its arcHref link target, while `slug`
    // stays available for renderDeltas' own slug-keyed label lookup (deltas
    // are the deliberate exception to arc identity — see ARC_IDENTITY_SQL's
    // comment) — neither renderer has to recompute what the other needs.
    topicArcs = topics.map((t) => ({
      slug: t.slug,
      label: t.label,
      identity: arcIdentity(t),
      count: priorIdentitySets.filter((set) => set.has(arcIdentity(t))).length + 1,
    }));
  }

  // In the daily or weekly view, prev/next stay within that same kind so a
  // reader hops brief-to-brief rather than through every window digest (or
  // the other brief kind) in between — the all view keeps today's
  // unconstrained chronological prev/next.
  //
  // Neighbor = adjacent by (created_at, id) tuple order, not by id: daily
  // briefs get BACKFILLED for past days with historical created_at values,
  // so a backfilled row's id says nothing about its chronological position.
  // SQLite row-value comparison ((created_at, id) < (?, ?)) does the tuple
  // compare/tiebreak in one expression — supported since SQLite 3.15, and
  // D1's SQLite is far newer.
  const kindFilter = view === "daily" ? " AND kind = 'daily'" : view === "weekly" ? " AND kind = 'weekly'" : "";
  const [older, newer] = await Promise.all([
    env.DB.prepare(
      `SELECT id, created_at FROM digests WHERE (created_at, id) < (?, ?)${kindFilter} ORDER BY created_at DESC, id DESC LIMIT 1`,
    )
      .bind(digest.created_at, id)
      .first(),
    env.DB.prepare(
      `SELECT id, created_at FROM digests WHERE (created_at, id) > (?, ?)${kindFilter} ORDER BY created_at ASC, id ASC LIMIT 1`,
    )
      .bind(digest.created_at, id)
      .first(),
  ]);

  // Deltas (§11.3 delta persistence, ingest v4): renders "" on a digest with
  // no deltas — see parseDeltas and renderDeltas's own "absent-data"
  // contract, matching topics/source_counts elsewhere on this page.
  const deltas = parseDeltas(digest.deltas);

  return htmlResponse(
    renderDigestPage(digest, older, newer, token, url.hostname, lang, view, topicArcs, deltas),
  );
}

async function handleSearchPage(env, token, url, lang) {
  if (!(await tokenMatches(env, token))) return notFound();

  // Query text lives in ?q=, not the path — see the URL-grammar header
  // comment. Truncated to 200 chars: nothing legitimate needs more, and it
  // bounds how much work buildFtsMatch and the D1 query below ever do for
  // one request.
  const q = (url.searchParams.get("q") ?? "").trim().slice(0, 200);

  // Fragment mode (unified search, owner UX pass): ?fragment=1 asks for just
  // the results markup, no pageChrome, no form — this is what the index
  // page's own filter box fetches in the background (see the bottom
  // script's archive-search IIFE) so a reader never has to leave the index
  // to see full-archive hits. The standalone route above (no fragment=1)
  // stays exactly as before: full page, no-JS form, deep-linkable.
  const isFragment = url.searchParams.get("fragment") === "1";

  // Empty query: with fragment=1 there's nothing to show — the ledger's own
  // empty-filtered message already speaks, so an empty body is correct, not
  // a degraded case. Full-page mode keeps its existing behavior: render
  // just the form, no search attempted (`results` staying null is what
  // tells renderSearchPage "no count line, no no-results message either" —
  // see there).
  if (q === "") {
    if (isFragment) return htmlResponse("");
    return htmlResponse(renderSearchPage(null, q, token, url.hostname, lang));
  }

  const matchQuery = buildFtsMatch(q);

  let results;
  try {
    // tldr/tldr_hu (snippet column indexes 0/2) weighted 10x over
    // body_md/body_md_hu (1/3) in the bm25 ranking — a title-word match
    // should outrank one buried in the body. snippet() wraps each matched
    // fragment in CHAR(1)/CHAR(2) sentinel bytes rather than real HTML tags
    // — body_md/body_md_hu are untrusted markdown, never HTML (see the
    // file-header comment), so the actual <mark> tags get added later, in
    // renderSearchPage/markSnippet, AFTER escaping the snippet text.
    const { results: rows } = await env.DB.prepare(
      `SELECT d.id, d.created_at, d.kind,
              snippet(digests_fts, 1, CHAR(1), CHAR(2), '…', 12) AS snip,
              snippet(digests_fts, 3, CHAR(1), CHAR(2), '…', 12) AS snip_hu
         FROM digests_fts
         JOIN digests d ON d.id = digests_fts.rowid
        WHERE digests_fts MATCH ?
        ORDER BY bm25(digests_fts, 10.0, 1.0, 10.0, 1.0)
        LIMIT 50`,
    )
      // Bound as a parameter even though buildFtsMatch already neutralizes
      // FTS5 syntax below — never string-interpolate user input into SQL,
      // belt and suspenders.
      .bind(matchQuery)
      .all();
    results = rows;
  } catch {
    // A MATCH string we built ourselves (see buildFtsMatch) should never
    // error, but a 500 on a search box is a worse failure mode than an
    // empty result — degrade to the no-results state instead of surfacing
    // whatever went wrong.
    results = [];
  }

  if (isFragment) return htmlResponse(renderSearchFragment(results, token, lang));

  return htmlResponse(renderSearchPage(results, q, token, url.hostname, lang));
}

// Arc page (§11.1 PR A): reconstructs the full appearance chain for one arc
// IDENTITY at request time — see the file-header comment for why this is
// DERIVED, not stored, and the "Stable arc keys" file-header section for what
// `identity` means (key when present, else slug — arcIdentity/
// ARC_IDENTITY_SQL). Identity shape is already guaranteed by the route regex
// in fetch() (`[a-z0-9-]{1,64}`) before this ever runs — the same shape a
// slug or a key is independently validated to at ingest time.
async function handleArcPage(env, token, identity, url, lang) {
  if (!(await tokenMatches(env, token))) return notFound();

  // json_each cross-joins each digest's `topics` JSON array; the WHERE
  // clause (topics IS NOT NULL, then ARC_IDENTITY_SQL matching the route's
  // `identity` segment) is what actually narrows the cross join down to at
  // most one row per digest — SQLite JSON1, available in D1 (verified
  // against the stubbed-D1 smoke test; ->> is standard SQLite 3.38+, and
  // D1's SQLite is far newer; json_extract(je.value, '$.slug'/'$.key') is
  // the fallback shape if a future D1 runtime ever regresses that operator).
  // `identity` is arc identity (stable arc keys, see ARC_IDENTITY_SQL's
  // comment), not necessarily a slug: `/a/hormuz` matches every digest whose
  // topic carries key "hormuz" regardless of that digest's own (differently
  // worded) slug, and `/a/<old-slug>` still resolves unchanged for any row
  // stored before `key` existed, since COALESCE falls back to slug there.
  //
  // DESC + LIMIT, then reversed in JS: if an arc ever exceeds the ceiling,
  // the rows that must survive are the NEWEST — the H1 (latest label),
  // "updated ...", and momentum are all computed off the latest end, so an
  // ASC LIMIT would silently freeze this page in the arc's distant past the
  // day it overflowed. LIMIT 500 is a sane ceiling in the spirit of
  // MAX_TOPICS/MAX_SOURCE_ENTRIES elsewhere in this file — not real
  // pagination, just a backstop against a pathological arc that recurs in
  // every digest ever ingested.
  //
  // body_html deliberately does NOT ride along here: a weekly's body_html
  // runs large, and an arc can legitimately accumulate hundreds of
  // appearances over months — hauling every body through one query is an
  // unbounded-memory shape no matter how normal each row is. Anchor
  // resolution (the only body_html consumer) is capped to the newest
  // ARC_ANCHOR_BODIES appearances via the second, id-bounded query below.
  // d.deltas rides along here (unlike body_html below): it's a small JSON
  // array capped at MAX_DELTAS entries, nothing like body_html's unbounded-
  // per-row cost, so there's no reason to defer it to a second, bounded
  // query the way anchor resolution is deferred — see parseDeltas/the
  // appearances mapping below for how each appearance picks out its own
  // slug's entry (§11.3 delta persistence, ingest v4).
  //
  // `topicSlug` (this appearance's OWN topic slug, not `identity`) rides
  // along too, for the delta match just below — deltas stay keyed on each
  // digest's own slug regardless of arc identity, see ARC_IDENTITY_SQL's
  // comment on why that's a deliberate exception, not an oversight.
  const { results: chainRows } = await env.DB.prepare(
    `SELECT d.id, d.created_at, d.kind, je.value->>'label' AS label, je.value->>'slug' AS topicSlug, d.deltas
       FROM digests d, json_each(d.topics) je
      WHERE d.topics IS NOT NULL AND ${ARC_IDENTITY_SQL} = ?1
      ORDER BY d.created_at DESC, d.id DESC
      LIMIT 500`,
  )
    .bind(identity)
    .all();

  // No digest currently carries this identity: unknown arc, same
  // indistinguishable 404 as a bad token or a nonexistent digest id (see the
  // file-header trust model — a well-shaped-but-unknown identity must reveal
  // nothing either).
  if (!chainRows || chainRows.length === 0) return notFound();

  // Deep-link anchors only for the newest ARC_ANCHOR_BODIES appearances —
  // the ones a reader actually navigates into from a live arc page. Older
  // appearances render with a fragment-less digest link, which is already
  // this feature's documented degraded mode (findArcSectionAnchor returning
  // null), not a new behavior — the fail-safe contract stays one contract.
  const anchorIds = chainRows.slice(0, ARC_ANCHOR_BODIES).map((r) => r.id);
  const { results: bodyRows } = await env.DB.prepare(
    `SELECT id, body_html FROM digests WHERE id IN (${anchorIds.map(() => "?").join(", ")})`,
  )
    .bind(...anchorIds)
    .all();
  const bodyById = new Map((bodyRows ?? []).map((r) => [r.id, r.body_html]));

  // Reversed to ASC (oldest first): renderArcPage's first/latest handling
  // and its own display-only re-reversal both rely on ASC input — see there.
  const rows = chainRows.slice().reverse();
  const appearances = rows.map((row) => {
    // Each appearance's own delta, if it has one, matched against THIS
    // ROW'S OWN topic slug (row.topicSlug), never against `identity`
    // (§11.3 delta persistence, ingest v4 + ARC_IDENTITY_SQL's comment on
    // why deltas are the deliberate exception to arc identity) — a digest's
    // `deltas` column can carry entries for several arcs at once, so
    // parseDeltas' fail-safe array is filtered down to at most the one entry
    // matching this appearance's own slug. No match (the common case: most
    // appearances predate the feature, or simply weren't a delta-only
    // update) leaves `delta` null — renderArcAppearance renders exactly as
    // it did before this feature in that case.
    const deltas = parseDeltas(row.deltas);
    const delta = deltas ? (deltas.find((d) => d.slug === row.topicSlug) ?? null) : null;
    return {
      id: row.id,
      created_at: row.created_at,
      kind: row.kind,
      label: row.label,
      anchor: bodyById.has(row.id) ? findArcSectionAnchor(bodyById.get(row.id), row.label) : null,
      delta,
    };
  });

  return htmlResponse(renderArcPage(identity, appearances, token, url.hostname, lang, Date.now()));
}

// Fail-safe slug->section mapping for arc-page deep links (§11.1 guardrail:
// "a wrong deep link is worse than a missing one"). Runs the exact same
// stripInlineStyles -> buildSectionToc pipeline renderDigestPage itself runs
// on body_html (see there) so the #sN ids this produces line up byte-for-
// byte with what that digest's OWN page actually renders — this is never
// reimplemented or approximated, just re-run on the same input.
//
// Always the ENGLISH body_html, never body_html_hu, regardless of which
// language this arc page is being read in: derive_topics (notification-
// digest repo, digest/publish.py) derives every topic label from the
// English body_md's own `## ` headings, so an English heading is the only
// text a topic label can ever have come from — matching against the HU
// translation's (differently worded) headings would just never match.
//
// Exact match preferred (labels under 80 chars are never truncated, so the
// heading and the label are identical strings); falls back to startsWith
// (the label IS the heading truncated to 80 chars — see derive_topics) only
// when that resolves to exactly one candidate. Zero or multiple candidates
// (ambiguous — two sections whose text happens to collide) both return
// null, which renders as a fragment-less link to the digest page itself
// (see renderArcAppearance) rather than a guess that might land on the
// wrong section.
function findArcSectionAnchor(bodyHtml, label) {
  const { sections } = buildSectionToc(stripInlineStyles(bodyHtml));
  const trimmed = label.trim();
  const exact = sections.filter((s) => s.title === trimmed);
  if (exact.length === 1) return exact[0].id;
  if (exact.length > 1) return null;
  const partial = sections.filter((s) => s.title.startsWith(trimmed));
  return partial.length === 1 ? partial[0].id : null;
}

// Turns free-text user input into a SAFE fts5 MATCH string. Raw user input
// must never reach FTS5 query syntax directly: FTS5 has its own operators
// (OR, NEAR, *, parentheses, "quoted phrases"), so an unquoted term like
// `NEAR(` or `tldr:*` is either a syntax error (crashes the query) or a
// query hijack (turns a reader's plain search into someone else's boolean
// expression). Phrase-quoting every term neutralizes all of it — each term
// becomes a literal string match, joined with implicit AND — and doubling
// any internal double quote (fts5's own escape convention) keeps a quote
// inside a term from closing the phrase early rather than being treated as
// a special character. At most 8 terms: plenty for a briefing search, and a
// hard cap on how many implicit-AND clauses one query can generate.
function buildFtsMatch(q) {
  const terms = q.split(/\s+/).filter(Boolean).slice(0, 8);
  return terms.map((term) => `"${term.replaceAll('"', '""')}"`).join(" ");
}

async function tokenMatches(env, token) {
  return Boolean(env.SITE_TOKEN) && (await keyMatches(token, env.SITE_TOKEN));
}

// ── validation ──────────────────────────────────────────────────────────

function byteLength(str) {
  return new TextEncoder().encode(str).length;
}

function validateDigestPayload(payload) {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    return { ok: false, error: "body must be a JSON object" };
  }
  const {
    created_at,
    tldr,
    item_count,
    section_count,
    has_attention,
    body_html,
    body_md,
    tldr_hu,
    body_html_hu,
    body_md_hu,
    kind,
    source_counts,
    failed_sources,
    topics,
    deltas,
  } = payload;

  if (
    typeof created_at !== "string" ||
    created_at.trim() === "" ||
    Number.isNaN(Date.parse(created_at))
  ) {
    return { ok: false, error: "created_at must be a valid date string" };
  }
  if (
    typeof tldr !== "string" ||
    tldr.length === 0 ||
    byteLength(tldr) > MAX_TLDR_BYTES
  ) {
    return { ok: false, error: "tldr must be a non-empty string within size limits" };
  }
  if (!Number.isInteger(item_count) || item_count < 0) {
    return { ok: false, error: "item_count must be a non-negative integer" };
  }
  if (!Number.isInteger(section_count) || section_count < 0) {
    return { ok: false, error: "section_count must be a non-negative integer" };
  }
  if (
    typeof has_attention !== "boolean" &&
    has_attention !== 0 &&
    has_attention !== 1
  ) {
    return { ok: false, error: "has_attention must be a boolean" };
  }
  if (
    typeof body_html !== "string" ||
    body_html.length === 0 ||
    byteLength(body_html) > MAX_BODY_FIELD_BYTES
  ) {
    return { ok: false, error: "body_html must be a non-empty string within size limits" };
  }
  if (
    typeof body_md !== "string" ||
    body_md.length === 0 ||
    byteLength(body_md) > MAX_BODY_FIELD_BYTES
  ) {
    return { ok: false, error: "body_md must be a non-empty string within size limits" };
  }

  // `kind` is optional and backward-compatible: the pre-daily-brief app
  // version never sends it, and that must keep working unchanged, so absence
  // defaults to "window" rather than failing. Presence is strict, though —
  // anything other than the three known values is a caller bug, not a value
  // to silently coerce. "weekly" is the once-a-week Sunday-evening synthesis
  // of the week's daily briefs, one editorial rung above "daily" — same
  // backward-compatible, opt-in shape as "daily" was when it was added.
  if (kind !== undefined && kind !== "window" && kind !== "daily" && kind !== "weekly") {
    return { ok: false, error: 'kind must be "window", "daily", or "weekly"' };
  }

  // Hungarian translation fields are entirely optional (older/untranslated
  // callers omit them), but a half-translation is a caller bug, not a valid
  // partial state: all three must show up together or not at all. `null` is
  // treated the same as "absent" so a caller can send explicit nulls.
  const huFieldsPresent = [tldr_hu, body_html_hu, body_md_hu].filter(
    (v) => v !== undefined && v !== null,
  ).length;
  if (huFieldsPresent !== 0 && huFieldsPresent !== 3) {
    return {
      ok: false,
      error:
        "tldr_hu, body_html_hu, and body_md_hu must all be present or all absent",
    };
  }
  const huEnabled = huFieldsPresent === 3;

  if (huEnabled) {
    if (
      typeof tldr_hu !== "string" ||
      tldr_hu.length === 0 ||
      byteLength(tldr_hu) > MAX_TLDR_BYTES
    ) {
      return { ok: false, error: "tldr_hu must be a non-empty string within size limits" };
    }
    if (
      typeof body_html_hu !== "string" ||
      body_html_hu.length === 0 ||
      byteLength(body_html_hu) > MAX_BODY_FIELD_BYTES
    ) {
      return { ok: false, error: "body_html_hu must be a non-empty string within size limits" };
    }
    if (
      typeof body_md_hu !== "string" ||
      body_md_hu.length === 0 ||
      byteLength(body_md_hu) > MAX_BODY_FIELD_BYTES
    ) {
      return { ok: false, error: "body_md_hu must be a non-empty string within size limits" };
    }
  }

  // source_counts/failed_sources (ingest v2, roadmap 2 step 8): both entirely
  // optional and independent of each other and of everything above — see
  // validateSourceCounts/validateFailedSources for the per-field shape
  // rules. Stored as JSON-stringified TEXT (or null), same as every other
  // "optional structured data" column on this table.
  const sourceCountsResult = validateSourceCounts(source_counts);
  if (!sourceCountsResult.ok) {
    return { ok: false, error: sourceCountsResult.error };
  }
  const failedSourcesResult = validateFailedSources(failed_sources);
  if (!failedSourcesResult.ok) {
    return { ok: false, error: failedSourcesResult.error };
  }

  // topics (ingest v3, roadmap 4 step 8): optional, independent of every
  // field above — see validateTopics for the per-entry shape rules. Same
  // JSON-stringified-TEXT-or-null storage as source_counts/failed_sources.
  const topicsResult = validateTopics(topics);
  if (!topicsResult.ok) {
    return { ok: false, error: topicsResult.error };
  }

  // deltas (§11.3 delta persistence, ingest v4): optional, independent of
  // every field above — see validateDeltas for the per-entry shape rules.
  // Same JSON-stringified-TEXT-or-null storage as source_counts/
  // failed_sources/topics.
  const deltasResult = validateDeltas(deltas);
  if (!deltasResult.ok) {
    return { ok: false, error: deltasResult.error };
  }

  return {
    ok: true,
    value: {
      created_at,
      tldr,
      item_count,
      section_count,
      has_attention: has_attention === true || has_attention === 1,
      body_html,
      body_md,
      tldr_hu: huEnabled ? tldr_hu : null,
      body_html_hu: huEnabled ? body_html_hu : null,
      body_md_hu: huEnabled ? body_md_hu : null,
      kind: kind ?? "window",
      source_counts: sourceCountsResult.value ? JSON.stringify(sourceCountsResult.value) : null,
      failed_sources: failedSourcesResult.value ? JSON.stringify(failedSourcesResult.value) : null,
      topics: topicsResult.value ? JSON.stringify(topicsResult.value) : null,
      deltas: deltasResult.value ? JSON.stringify(deltasResult.value) : null,
    },
  };
}

// source_counts: absent/null is valid (nothing reported, stored NULL — see
// the schema.sql comment). Present, it must be a plain JSON object (not an
// array — typeof [] === "object" too, hence the explicit Array.isArray
// check), every key matching SOURCE_NAME_RE, every value a non-negative
// integer, at most MAX_SOURCE_ENTRIES keys. Any other shape is a 400, not a
// value to silently coerce or drop keys from.
function validateSourceCounts(value) {
  if (value === undefined || value === null) return { ok: true, value: null };
  if (typeof value !== "object" || Array.isArray(value)) {
    return { ok: false, error: "source_counts must be a JSON object" };
  }
  const keys = Object.keys(value);
  if (keys.length > MAX_SOURCE_ENTRIES) {
    return { ok: false, error: `source_counts must have at most ${MAX_SOURCE_ENTRIES} keys` };
  }
  for (const key of keys) {
    if (!SOURCE_NAME_RE.test(key)) {
      return { ok: false, error: `source_counts has an invalid source name: "${key}"` };
    }
    const count = value[key];
    if (!Number.isInteger(count) || count < 0) {
      return { ok: false, error: `source_counts["${key}"] must be a non-negative integer` };
    }
  }
  return { ok: true, value };
}

// failed_sources: same optional/shape contract as source_counts, but a JSON
// array of source-name strings rather than an object. A valid-but-empty
// array is normalized to null here — "no failures reported" and "an old app
// that doesn't send this field at all" would otherwise render identically
// (no degraded badge either way), so storing "[]" would be a distinction
// without a difference; see the schema.sql comment on the column itself.
function validateFailedSources(value) {
  if (value === undefined || value === null) return { ok: true, value: null };
  if (!Array.isArray(value)) {
    return { ok: false, error: "failed_sources must be a JSON array" };
  }
  if (value.length > MAX_SOURCE_ENTRIES) {
    return { ok: false, error: `failed_sources must have at most ${MAX_SOURCE_ENTRIES} entries` };
  }
  for (const name of value) {
    if (typeof name !== "string" || !SOURCE_NAME_RE.test(name)) {
      return { ok: false, error: `failed_sources has an invalid source name: "${name}"` };
    }
  }
  return { ok: true, value: value.length === 0 ? null : value };
}

// topics (ingest v3, roadmap 4 step 8; `key` added for stable arc keys, see
// ARC_KEY_MAX_LEN's comment): absent/null is valid (nothing reported, stored
// NULL). Present, it must be a JSON array (not an object — Array.isArray,
// not typeof, same reasoning as source_counts' inverse check), at most
// MAX_TOPICS entries, each entry a plain object (not an array, not null —
// typeof null === "object" too) with EXACTLY slug (matching TOPIC_SLUG_RE),
// label (a string whose trimmed length is 1..80 — the trimmed form is what
// gets stored, same "store the normalized value" contract as everywhere else
// in this validator), and the OPTIONAL key (same TOPIC_SLUG_RE shape, capped
// at ARC_KEY_MAX_LEN — see arcIdentity for how it's consumed). Any other key
// is still rejected via the `...rest` check below, same strict-shape
// discipline as before this field existed. Duplicate slugs within one
// payload are a caller bug, not something to silently dedupe; a duplicate
// key is NOT checked here — two topics sharing a key within one digest is
// unusual but not a shape violation, the same posture validateDeltas already
// takes on duplicate delta slugs. Same empty-array-normalizes-to-null
// reasoning as failed_sources — "no topics reported" and "an old app that
// doesn't send this field" would otherwise render identically, so storing
// "[]" is a distinction without a difference.
function validateTopics(value) {
  if (value === undefined || value === null) return { ok: true, value: null };
  if (!Array.isArray(value)) {
    return { ok: false, error: "topics must be a JSON array" };
  }
  if (value.length > MAX_TOPICS) {
    return { ok: false, error: `topics must have at most ${MAX_TOPICS} entries` };
  }
  const seenSlugs = new Set();
  const normalized = [];
  for (const entry of value) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      return { ok: false, error: "each topics entry must be an object" };
    }
    const { slug, label, key, ...rest } = entry;
    if (Object.keys(rest).length > 0) {
      return { ok: false, error: "each topics entry must have exactly slug, label, and the optional key" };
    }
    if (typeof slug !== "string" || !TOPIC_SLUG_RE.test(slug)) {
      return { ok: false, error: `topics has an invalid slug: "${slug}"` };
    }
    if (typeof label !== "string" || label.trim().length < 1 || label.trim().length > 80) {
      return { ok: false, error: `topics["${slug}"].label must be 1-80 characters` };
    }
    // key (stable arc keys): absent/null stays absent — a caller that never
    // sends it (every pre-key payload, forever) is not an error, same
    // optional-field convention as every other field in this validator.
    // Present, it must be non-empty, TOPIC_SLUG_RE-shaped, and within the
    // tighter ARC_KEY_MAX_LEN cap — an invalid key is a caller bug (400), not
    // a value to silently drop, since silently dropping it would silently
    // fall back this topic to slug-only identity without telling the caller.
    if (key !== undefined && key !== null) {
      if (typeof key !== "string" || !TOPIC_SLUG_RE.test(key) || key.length > ARC_KEY_MAX_LEN) {
        return { ok: false, error: `topics["${slug}"].key is invalid: "${key}"` };
      }
    }
    if (seenSlugs.has(slug)) {
      return { ok: false, error: `topics has a duplicate slug: "${slug}"` };
    }
    seenSlugs.add(slug);
    // `key` rides along only when present (and non-null) — a stored entry
    // never carries a `key: null`/`key: undefined` field, same "absence is
    // absence, not a null placeholder" contract topics/deltas already keep
    // for every other optional value in this file.
    normalized.push(key ? { slug, label: label.trim(), key } : { slug, label: label.trim() });
  }
  return { ok: true, value: normalized.length === 0 ? null : normalized };
}

// deltas (§11.3 delta persistence, ingest v4): absent/null is valid (no
// repeat-story deltas this window — the common case even on a current app
// version). Present, it must be a JSON array (Array.isArray, not typeof —
// same reasoning as topics/source_counts above), at most MAX_DELTAS
// entries, each entry a plain object (not an array, not null) with EXACTLY
// three keys: slug (matching TOPIC_SLUG_RE — the SAME slug vocabulary
// topics already establishes; the app's own digest/publish.py
// map_deltas_to_slugs is what guarantees that on the sending side, but this
// validator enforces the shape independently, the same way it never trusts
// the app to have gotten topics right either), previously, and now (each a
// non-empty string, trimmed length 1..DELTA_TEXT_MAX_LEN). Unknown
// per-entry keys are rejected via the `...rest` check, mirroring
// validateTopics' own discipline just above. Same empty-array-normalizes-
// to-null reasoning as topics/failed_sources — "no deltas reported" and "an
// app version that doesn't send this field" would otherwise render
// identically, so storing "[]" is a distinction without a difference.
//
// Unlike topics, a duplicate slug across two entries is NOT rejected here:
// the app side never produces one (map_deltas_to_slugs runs after
// derive_topics' own dedupe-by-slug), but two entries citing the same slug
// is not a SHAPE violation this validator exists to police — the render
// side (parseDeltas + renderDeltas/renderArcAppearance, both matching by
// slug) simply uses whichever entry it finds, the same fail-safe posture as
// every other read-time consumer in this file.
function validateDeltas(value) {
  if (value === undefined || value === null) return { ok: true, value: null };
  if (!Array.isArray(value)) {
    return { ok: false, error: "deltas must be a JSON array" };
  }
  if (value.length > MAX_DELTAS) {
    return { ok: false, error: `deltas must have at most ${MAX_DELTAS} entries` };
  }
  const normalized = [];
  for (const entry of value) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      return { ok: false, error: "each deltas entry must be an object" };
    }
    const { slug, previously, now, ...rest } = entry;
    if (Object.keys(rest).length > 0) {
      return { ok: false, error: "each deltas entry must have exactly slug, previously, and now" };
    }
    if (typeof slug !== "string" || !TOPIC_SLUG_RE.test(slug)) {
      return { ok: false, error: `deltas has an invalid slug: "${slug}"` };
    }
    if (
      typeof previously !== "string" ||
      previously.trim().length < 1 ||
      previously.trim().length > DELTA_TEXT_MAX_LEN
    ) {
      return {
        ok: false,
        error: `deltas["${slug}"].previously must be 1-${DELTA_TEXT_MAX_LEN} characters`,
      };
    }
    if (
      typeof now !== "string" ||
      now.trim().length < 1 ||
      now.trim().length > DELTA_TEXT_MAX_LEN
    ) {
      return { ok: false, error: `deltas["${slug}"].now must be 1-${DELTA_TEXT_MAX_LEN} characters` };
    }
    normalized.push({ slug, previously: previously.trim(), now: now.trim() });
  }
  return { ok: true, value: normalized.length === 0 ? null : normalized };
}

// ── auth helper (shared pattern with workers/polymarket-proxy) ─────────

async function keyMatches(provided, expected) {
  // Hash both sides first: crypto.subtle.timingSafeEqual requires
  // equal-length inputs, and comparing digests leaks nothing about the
  // secret's length or content.
  const enc = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(provided)),
    crypto.subtle.digest("SHA-256", enc.encode(expected)),
  ]);
  return crypto.subtle.timingSafeEqual(a, b);
}

// ── plain responses ─────────────────────────────────────────────────────

function json(body, status) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function badRequest(message) {
  return json({ error: message }, 400);
}

function notFound() {
  // Deliberately generic: identical for an unknown path, a wrong token, or
  // a valid token with a nonexistent digest id. No hint the site exists.
  return new Response("Not Found", {
    status: 404,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "referrer-policy": "no-referrer",
      "x-robots-tag": "noindex, nofollow",
      "cache-control": "private, no-store",
    },
  });
}

function htmlResponse(html) {
  return new Response(html, {
    status: 200,
    headers: {
      // LOAD-BEARING: digest bodies are full of outbound citation links.
      // Without no-referrer, every click on one leaks the capability token
      // (it's part of the page's own URL) to whatever site gets clicked.
      "referrer-policy": "no-referrer",
      "x-robots-tag": "noindex, nofollow",
      "cache-control": "private, no-store",
      "content-type": "text/html; charset=utf-8",
    },
  });
}

// ── HTML escaping (only body_html, pre-sanitized upstream, skips this) ──

const HTML_ESCAPES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

// ── time formatting (Europe/Budapest hardcoded, per project convention:
// storage/comparisons stay UTC, only rendering converts) ────────────────
//
// Both formatters take `locale` explicitly (STRINGS[lang].locale below)
// rather than deriving it from lang themselves — Intl does all the actual
// EN/HU formatting work (weekday/month names, date ordering) once given the
// right locale tag; this file never hand-builds a Hungarian date string.

function formatDayHeader(date, locale) {
  // en-GB: "Wednesday, 5 August 2026" · hu-HU: "2026. augusztus 6., csütörtök"
  return new Intl.DateTimeFormat(locale, {
    timeZone: TIMEZONE,
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  }).format(date);
}

function formatTime(date, locale) {
  // "18:00" in both locales (hour12: false makes the locale irrelevant here,
  // but it's threaded through for consistency/future-proofing).
  return new Intl.DateTimeFormat(locale, {
    timeZone: TIMEZONE,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatShortDate(date, locale) {
  // A compact form for <title> (see pageChrome's `title` param): en-GB
  // "Fri 8 Aug" · hu-HU "aug. 8., P" — same fields as formatDayHeader, just
  // abbreviated, so a browser tab/history entry stays legible without
  // eating the whole title budget.
  return new Intl.DateTimeFormat(locale, {
    timeZone: TIMEZONE,
    weekday: "short",
    day: "numeric",
    month: "short",
  }).format(date);
}

// Budapest's UTC offset, in minutes, AT the given instant — the numeric
// generalization of tzAbbr's CET/CEST lookup below, also reused by
// weekBoundsUtc (roadmap 3 step 2) to convert Budapest-local midnight to a
// UTC instant. Recent ICU versions render timeZoneName:"short" for Europe/*
// zones as a GMT offset ("GMT+1"/"GMT+2") rather than "CET"/"CEST", so the
// offset is derived ourselves instead of trusting a zone-abbreviation
// string; Europe/Budapest only ever has these two (whole-hour) offsets, so
// the parse is exact. Locale-independent numeric parse (not user-facing
// text), so it stays on "en-GB" regardless of the page's language.
function budapestOffsetMinutes(date) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: TIMEZONE,
    timeZoneName: "shortOffset",
  }).formatToParts(date);
  const offset = parts.find((p) => p.type === "timeZoneName")?.value ?? "";
  // Match "+2" AND "+02" (ICU emits "GMT+2" for shortOffset today, but a
  // runtime that ever hands back the padded "GMT+02:00" long form must not
  // silently fall through to +1h in August — the exact bug this function
  // exists to avoid).
  const m = offset.match(/([+-])0?(\d)/);
  const sign = m && m[1] === "-" ? -1 : 1;
  const hours = m ? Number(m[2]) : 1; // fail-safe default: CET, +1h
  return sign * hours * 60;
}

function tzAbbr(date) {
  return budapestOffsetMinutes(date) === 120 ? "CEST" : "CET";
}

// ── ISO week helpers (roadmap 3 step 2 — "weekly pagination"): the
// correctness core of this step. All Budapest-local, DST-safe, and built on
// the same "convert to Budapest calendar y/m/d, then do plain date
// arithmetic on a UTC-noon PROXY date" technique throughout — noon rather
// than midnight so none of the day-shift arithmetic below can ever cross a
// UTC calendar-date boundary next to an actual DST transition (which always
// happens near local midnight, never near local noon). A "proxy" date's
// UTC y/m/d fields are read back as the intended Budapest calendar date;
// its actual instant-in-time value is never used for anything else. ────────

// The Budapest-local calendar date (year/month/day) of a Date/instant, via
// Intl.formatToParts rather than a fixed offset — DST-correct year-round.
// Locale is irrelevant here (parts are picked by `type`, not parsed as
// text), so "en-GB" is used unconditionally, same reasoning as tzAbbr.
function budapestDateParts(date) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const get = (type) => Number(parts.find((p) => p.type === type).value);
  return { y: get("year"), m: get("month"), d: get("day") };
}

// The ISO 8601 week (Monday-first, week 1 is the week containing Jan 4) of
// the Budapest-local calendar date of `date`. Standard algorithm: shift the
// proxy date to "this week's Thursday" (Thursday's calendar year is always
// the correct ISO year, including at year boundaries), then count whole
// weeks from that ISO year's own Jan 1.
function isoWeekOf(date) {
  const { y, m, d } = budapestDateParts(date);
  const proxy = new Date(Date.UTC(y, m - 1, d, 12));
  const isoWeekday = proxy.getUTCDay() || 7; // Mon=1 .. Sun=7 (Sun is 0 in JS)
  proxy.setUTCDate(proxy.getUTCDate() + 4 - isoWeekday); // -> this week's Thursday
  const isoYear = proxy.getUTCFullYear();
  // Noon-vs-noon (not noon-vs-midnight) so the difference below is an exact
  // whole-day count — see the block comment above on why noon is used
  // throughout rather than midnight.
  const yearStart = new Date(Date.UTC(isoYear, 0, 1, 12));
  const diffDays = (proxy - yearStart) / 86400000;
  const week = Math.ceil((diffDays + 1) / 7);
  return { year: isoYear, week };
}

// Whether ISO year `year` has 53 weeks (rather than the usual 52) — the
// standard rule: true iff Jan 1 falls on a Thursday, or (in a leap year) on
// a Wednesday. Pure calendar arithmetic, no timezone involved — an ISO week
// YEAR is not a Budapest-local concept, just a numbering scheme. Used to
// validate a "w/YYYY-W53/" URL: week 53 is only a real week for years this
// returns true for (see the route match in fetch()).
function isoWeeksInYear(year) {
  const jan1Weekday = new Date(Date.UTC(year, 0, 1)).getUTCDay(); // 0=Sun..6=Sat
  const isLeap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
  return jan1Weekday === 4 || (isLeap && jan1Weekday === 3) ? 53 : 52;
}

// (year, week) tuple comparison, ISO-week-ordinal-aware (a week always
// compares by year first, then week number within it) — same "compare the
// tuple" pattern handleDigestPage already uses for (created_at, id) via
// SQLite row values, just done in JS since these are two small integers,
// not a SQL expression.
function compareIsoWeek(a, b) {
  return a.year !== b.year ? a.year - b.year : a.week - b.week;
}

// The Budapest calendar date of Jan 4 of `year`, as a UTC-noon proxy — Jan 4
// is always in ISO week 1 by definition, which is what anchors the whole
// per-year week grid below (mondayOfIsoWeek).
function jan4Proxy(year) {
  return new Date(Date.UTC(year, 0, 4, 12));
}

// Monday of ISO week `week` of `year`, as a UTC-noon proxy holding that
// Monday's Budapest calendar date — the shared anchor for weekBoundsUtc,
// adjacentWeek, and formatWeekRangeLabel below. Back up from Jan 4 (always
// in week 1) to ITS OWN Monday to get week 1's Monday; every other week's
// Monday is exactly (week-1)*7 days later. Plain day-count arithmetic, valid
// across ISO year boundaries too with no special-casing: the Monday-to-
// Monday sequence has no gaps, so "week 53's Monday + 7d" lands correctly on
// next year's week-1 Monday on its own (see weekBoundsUtc's comment for why
// this matters there). `week` may be 0 or negative or run past a year's own
// week count — callers (weekBoundsUtc, adjacentWeek) rely on exactly that to
// step across year boundaries without their own carry logic.
function mondayOfIsoWeek(year, week) {
  const jan4 = jan4Proxy(year);
  const jan4Weekday = jan4.getUTCDay() || 7; // Mon=1 .. Sun=7
  const monday = new Date(jan4);
  monday.setUTCDate(monday.getUTCDate() - (jan4Weekday - 1) + (week - 1) * 7);
  return monday;
}

// Budapest-local midnight of the given (UTC-noon-proxy-derived) calendar
// date, as a UTC ISO string. Date.UTC(y, m-1, d) is a naive UTC-midnight
// guess for that calendar date; subtracting Budapest's actual UTC offset at
// that boundary shifts it to the real instant. The offset is sampled at the
// NOON proxy of that same calendar date (not at the naive midnight guess
// itself) so a DST transition landing exactly at local midnight can never
// make the offset lookup read the wrong side of the transition.
function budapestMidnightUtcIso(y, m, d) {
  const offsetMinutes = budapestOffsetMinutes(new Date(Date.UTC(y, m - 1, d, 12)));
  return new Date(Date.UTC(y, m - 1, d) - offsetMinutes * 60000).toISOString();
}

// UTC ISO bounds of ISO week `week` of `year`: Budapest-local Monday 00:00
// of that week through Budapest-local Monday 00:00 of the following week —
// a half-open [start, end) range, matching how every other created_at
// comparison in this file works. Passing `week + 1` into mondayOfIsoWeek
// for the end boundary — rather than computing "next week" via a separate
// adjacentWeek call — is deliberate: it's the exact same plain day-count
// arithmetic that makes ISO-year rollovers (week 52/53 -> next year's week 1)
// fall out correctly with no extra branching, see mondayOfIsoWeek's comment.
function weekBoundsUtc(year, week) {
  const start = mondayOfIsoWeek(year, week);
  const end = mondayOfIsoWeek(year, week + 1);
  return {
    startIso: budapestMidnightUtcIso(start.getUTCFullYear(), start.getUTCMonth() + 1, start.getUTCDate()),
    endIso: budapestMidnightUtcIso(end.getUTCFullYear(), end.getUTCMonth() + 1, end.getUTCDate()),
  };
}

// (year, week) shifted by `delta` ISO weeks — used for the week rail's
// older/newer targets (delta ±1) in handleIndexPage. Goes through the
// Monday-date + isoWeekOf round trip (add delta*7 days, then re-derive which
// ISO week that Monday falls in) rather than hand-rolling year/week carry
// arithmetic, so ISO year boundaries reuse the exact same logic as
// weekBoundsUtc/isoWeekOf instead of a second, possibly-diverging copy of it.
function adjacentWeek(year, week, delta) {
  const monday = mondayOfIsoWeek(year, week);
  monday.setUTCDate(monday.getUTCDate() + delta * 7);
  return isoWeekOf(monday);
}

// The week rail's date-range text, e.g. "3–9 Aug" (en-GB) / "aug. 3–9."
// (hu-HU) — Intl's formatRange collapses the shared month between the two
// boundary dates on its own (this is NOT hand-built by formatting each date
// separately and joining strings, which would repeat the month/produce the
// wrong shape). A week's Mon-Sun span crosses a CALENDAR year boundary near
// ISO year edges (ISO week 1 can start in late December) — show the year in
// that one case so the range isn't ambiguous about which year each date
// falls in; the common case omits it, matching the plain "3–9 Aug" shape.
function formatWeekRangeLabel(year, week, locale) {
  const monday = mondayOfIsoWeek(year, week);
  const sunday = new Date(monday);
  sunday.setUTCDate(sunday.getUTCDate() + 6);
  const spansCalendarYearBoundary = monday.getUTCFullYear() !== sunday.getUTCFullYear();
  const fmt = new Intl.DateTimeFormat(locale, {
    timeZone: TIMEZONE,
    day: "numeric",
    month: "short",
    year: spansCalendarYearBoundary ? "numeric" : undefined,
  });
  return fmt.formatRange(monday, sunday);
}

// ── chrome strings (EN|HU) ──────────────────────────────────────────────
//
// This is the ENTIRE Hungarian vocabulary this Worker knows — everything
// else Hungarian-language on a /hu/ page is either digest content the app
// already translated (body_html_hu/tldr_hu) or a handful of untranslated
// micro-labels ("digest #N") left as-is; the TL;DR excerpt label localizes
// via STRINGS.tldrLabel ("Röviden:"). See README/PR notes for
// the reasoning. dailyBrief and weeklyBrief are the exceptions on the stamp
// line: for kind="daily"/"weekly" they replace the untranslated "digest"
// label, so the HU stamp reads "napi összefoglaló #N" / "heti összefoglaló
// #N" instead of "digest #N". Owner: please read these for correctness,
// they're the only hardcoded Hungarian text in the codebase.
const STRINGS = {
  en: {
    locale: "en-GB",
    itemsWord: "items",
    sectionsWord: "sections",
    allDigests: "← All digests",
    noDigests:
      "No briefings yet. The next window closes every three hours — the first one lands here on its own.",
    noDailyBriefs: "No daily briefs yet — the first one lands at 20:00.",
    noWeeklyBriefs: "No weekly briefs yet — the first one lands Sunday at 21:00.",
    footerPrivate:
      "Private link — anyone with this URL can read. Don't share it outside the group.",
    footerNotIndexed: "Not indexed · generated by the digest service, every 3 hours",
    tldrLabel: "TL;DR:",
    backFabLabel: "Back to all digests",
    enOnlyNote: null,
    dailyBrief: "daily brief",
    weeklyBrief: "weekly report",
    viewAll: "All",
    viewDaily: "Daily",
    viewWeekly: "Weekly",
    latest: "Latest",
    filterPlaceholder: "Filter briefings…",
    emptyFiltered: "Nothing matches.",
    themeToggle: "Toggle light/dark",
    densityToggle: "Toggle compact list",
    // Settings bubble (owner redesign): the gear button that collapses the
    // language/theme/size/density cluster, plus its panel row labels.
    settingsLabel: "Settings",
    settingsLanguage: "Language",
    settingsTheme: "Theme",
    // Three-state theme miniseg labels (owner redesign: light/auto/dark
    // replaces the old two-state ◐ toggle) — see renderSwitchers.
    themeLight: "Light",
    themeAuto: "Auto",
    themeDark: "Dark",
    settingsTextSize: "Text size",
    settingsDensity: "Density",
    unreadFence: "new since your last visit",
    countdownNext: "next window closes in about {t}",
    countdownDue: "next window closing about now",
    countdownHourUnit: "h",
    countdownMinuteUnit: "m",
    degraded: "partial",
    // Week rail (roadmap 3 step 2). weekLabel is a placeholder template
    // ({w} = week number, {range} = formatWeekRangeLabel's output) rather
    // than hardcoded word order, so EN "Week 32 · 3–9 Aug" and HU
    // "32. hét · aug. 3–9." can each put the week word/number on their own
    // natural side of the range.
    weekRailLabel: "Week navigation",
    weekLabel: "Week {w} · {range}",
    // Digest-page source key label (rendered uppercase via .sklabel's CSS).
    sourcesLabel: "Sources",
    // Search (roadmap 4 step 7). searchResults is a placeholder template
    // ({n} = result count), same convention as weekLabel above.
    searchLabel: "Search the archive",
    searchPlaceholder: "Search all briefings…",
    searchButton: "Search",
    searchLink: "Search ↗",
    searchResults: "{n} results",
    searchResultsOne: "{n} result",
    searchNone: "Nothing found.",
    // Search bubble trigger (owner-requested index cleanup, renderSearchBubble):
    // the compact button that opens the filter/search popover — both its
    // visible text and its aria-label, same "one string, two surfaces" reuse
    // as archiveLabel above. Deliberately its own string, not a reuse of
    // searchButton (the standalone search page's submit label) — a shared
    // string would couple two independently-changeable controls just
    // because their text happens to match today.
    searchToggleLabel: "Search",
    // Unified search (owner UX pass): eyebrow label above the archive
    // results the index page's own filter box surfaces in-page — see
    // renderSearchFragment/the .archivelabel CSS. Mono-eyebrow voice, not a
    // template (no {n} — the count line stays on the standalone search page
    // only).
    archiveResults: "From the archive",
    // Story-arc line (roadmap 4 step 8, renderArcs). arcRepeat is a
    // placeholder template ({n} = total appearances including this digest),
    // same convention as weekLabel/searchResults above — the whole "×{n}
    // this week" string comes from the template, never hand-composed.
    arcsLabel: "Story threads",
    arcRepeat: "×{n} this week",
    // Arc page (§11.1 PR A, renderArcPage). arcAppearances/arcFirstSeen/
    // arcUpdated are placeholder templates ({n}/{date}/{t}), same convention
    // as arcRepeat/weekLabel/searchResults above — each whole metadata
    // fragment comes from its own template, never hand-composed pieces.
    // arcMomentum{Up,Same,Down} are the frequency-only labels the §11.1
    // guardrail requires (never severity words like "escalating") — see
    // computeArcMomentum.
    arcAppearances: "{n} appearances",
    arcAppearancesOne: "{n} appearance",
    arcFirstSeen: "first seen {date}",
    arcUpdated: "updated {t}",
    arcMomentumUp: "more coverage",
    arcMomentumSame: "steady",
    arcMomentumDown: "less coverage",
    arcTimelineLabel: "Appearances",
    // NOW section (§11.1 PR B, renderNowSection): the mono eyebrow above the
    // situational-overview block at the top of the current-week all-view
    // index — see computeNowArcs/renderNowSection. Each row's momentum
    // arrow reuses the same computeArcMomentum classification arcMomentum
    // {Up,Same,Down} above label on the arc page, but renders it as a bare
    // arrow glyph, not those text strings — no separate now* string needed.
    // (The row's metadata used to also reuse arcRepeat's "×{n} this week"
    // count; that was dropped in the owner-requested index cleanup — see
    // renderNowSection's own comment.)
    nowLabel: "Now",
    // Archive command label (§11.1 PR C): the ⌘K palette's "Archive" entry
    // (data-cmd-archive, see pageChrome/collectPaletteItems) used to double
    // as the masthead Archive link's own text too — that link was removed in
    // the index-cleanup pass (archive weeks are reached via the week rail's
    // ← link now, see renderWeekRail), so this string's only remaining
    // consumer is the palette label, which quietly never renders its row
    // anymore since collectPaletteItems' `.archivelink` lookup always comes
    // up empty — see that function's own comment.
    archiveLabel: "Archive",
    // ⌘K command palette (§11.1 PR C): client-side only, progressive
    // enhancement — see the palette IIFE in pageChrome. paletteLabel is the
    // palette's accessible name (the input's own aria-label — the dialog
    // itself is labelled BY the input via aria-labelledby, per the §11.1
    // spec, so this string only has to live in one place). paletteCmd* are
    // the static commands assembled at open time alongside whatever the
    // current page's own DOM contributes (arcs, briefings) — see
    // collectPaletteItems. paletteEmpty reuses emptyFiltered rather than
    // minting a near-duplicate "nothing matches" string; the "Search" and
    // "Archive" commands reuse searchButton/archiveLabel above for the same
    // reason.
    paletteLabel: "Command palette",
    palettePlaceholder: "Type a command or search…",
    paletteCmdTop: "Go to top",
    paletteCmdAll: "All view",
    paletteCmdDaily: "Daily view",
    paletteCmdWeekly: "Weekly view",
    paletteCmdSwitchLang: "Switch language",
    paletteCmdLatest: "Latest briefing",
    // Catch-up banner (§11.2, current-week all-view index only — same gate
    // as the NOW section, see handleIndexPage's showCatchup). Client-built
    // from the SAME lastVisit stamp the unread-fence IIFE already reads —
    // see that IIFE's extension for the reconciliation. catchupBriefings/
    // catchupArcUpdates/catchupArcMore are placeholder templates ({n}/{m}),
    // same convention as arcRepeat/weekLabel above; catchupJumpLabel/
    // catchupDismissLabel are aria-labels for the two icon-only controls
    // (jump to the "you were here" marker below, dismiss for this page-view
    // only — see the CSS/script for both).
    catchupPrefix: "Since your last visit:",
    catchupBriefings: "{n} briefings",
    catchupBriefingsOne: "{n} briefing",
    catchupArcUpdates: "{m} arc updates",
    catchupArcUpdatesOne: "{m} arc update",
    catchupArcMore: "+{n} more",
    catchupJumpLabel: "Jump to where you left off",
    catchupDismissLabel: "Dismiss",
    // Follow list (§11.2, optional feature, arc pages only): client-
    // injected text control next to the arc title (renderArcPage's
    // .archead) — no button chrome, matches the design guidance's "text
    // control" instruction. followAdd/followRemove are the two toggle
    // states, same star-glyph convention brief specified.
    followAdd: "☆ Follow",
    followRemove: "★ Following",
    // "What changed" block (§11.3 delta persistence, ingest v4): the mono
    // eyebrow above the digest page's per-arc previously/now lines — see
    // renderDeltas. Reuses the SAME .archivelabel eyebrow recipe as
    // arcTimelineLabel/archiveResults/nowLabel above, so this is a plain
    // one-off string, not a template.
    whatChangedLabel: "What changed",
  },
  hu: {
    locale: "hu-HU",
    itemsWord: "elem",
    sectionsWord: "szakasz",
    allDigests: "← Minden hírlevél",
    noDigests:
      "Még nincs hírlevél. A következő ablak háromóránként zárul — az első magától megjelenik itt.",
    noDailyBriefs: "Még nincs napi összefoglaló — az első 20:00-kor érkezik.",
    // Owner: please review — new HU string, mirrors noDailyBriefs's pattern.
    noWeeklyBriefs: "Még nincs heti összefoglaló — az első vasárnap 21:00-kor érkezik.",
    footerPrivate:
      "Privát link — bárki olvashatja, akinél megvan ez az URL. Ne oszd meg a csoporton kívül.",
    footerNotIndexed: "Nem indexelt · a digest szolgáltatás generálja, 3 óránként",
    tldrLabel: "Röviden:",
    backFabLabel: "Vissza a hírlevelekhez",
    enOnlyNote: "Csak angolul elérhető",
    dailyBrief: "napi összefoglaló",
    // Owner: please review — new HU string, mirrors dailyBrief's pattern.
    weeklyBrief: "heti összefoglaló",
    viewAll: "Minden",
    viewDaily: "Napi",
    // Owner: please review — new HU string, mirrors viewDaily's pattern.
    viewWeekly: "Heti",
    latest: "Legfrissebb",
    filterPlaceholder: "Szűrés…",
    emptyFiltered: "Nincs találat.",
    themeToggle: "Világos/sötét váltás",
    densityToggle: "Kompakt lista be/ki",
    // Owner: please review — new HU strings, settings bubble (gear button +
    // panel row labels), mirrors the EN block's pattern.
    settingsLabel: "Beállítások",
    settingsLanguage: "Nyelv",
    settingsTheme: "Téma",
    themeLight: "Világos",
    themeAuto: "Auto",
    themeDark: "Sötét",
    settingsTextSize: "Betűméret",
    settingsDensity: "Sűrűség",
    unreadFence: "új a legutóbbi látogatásod óta",
    countdownNext: "a következő ablak kb. {t} múlva zárul",
    countdownDue: "a következő ablak kb. most zárul",
    countdownHourUnit: "ó",
    countdownMinuteUnit: "p",
    degraded: "hiányos",
    weekRailLabel: "Heti navigáció",
    weekLabel: "{w}. hét · {range}",
    sourcesLabel: "Források",
    // Search (roadmap 4 step 7) — owner: please review these, flagged HU
    // strings same as everywhere else in this file.
    searchLabel: "Keresés az archívumban",
    searchPlaceholder: "Keresés az összes hírlevélben…",
    searchButton: "Keresés",
    searchLink: "Keresés ↗",
    searchResults: "{n} találat",
    // Hungarian does not pluralise a noun after a numeral — "1 találat" and
    // "5 találat" are both correct — so every *One key below is deliberately
    // identical to its plural twin. They exist so the EN side can differ;
    // dropping them here would make the lookup lang-conditional for no gain.
    searchResultsOne: "{n} találat",
    searchNone: "Nincs találat a keresésre.",
    // Owner: please review — new HU string, search bubble trigger
    // (owner-requested index cleanup), mirrors the EN block's pattern.
    searchToggleLabel: "Keresés",
    // Owner: please review — new HU string, mirrors searchLabel's pattern.
    archiveResults: "Az archívumból",
    // Story arcs (roadmap 4 step 8) — owner: please review these, flagged HU
    // strings same as everywhere else in this file.
    arcsLabel: "Történetszálak",
    arcRepeat: "×{n} ezen a héten",
    // Arc page (§11.1 PR A) — owner: please review these, flagged HU
    // strings same as everywhere else in this file.
    arcAppearances: "{n} előfordulás",
    arcAppearancesOne: "{n} előfordulás",
    arcFirstSeen: "először: {date}",
    arcUpdated: "frissítve: {t}",
    arcMomentumUp: "több lefedettség",
    arcMomentumSame: "változatlan",
    arcMomentumDown: "kevesebb lefedettség",
    arcTimelineLabel: "Előfordulások",
    // Owner: please review — new HU string, NOW section (§11.1 PR B),
    // mirrors the EN block's pattern.
    nowLabel: "Most",
    // Owner: please review — new HU strings, Archive nav affordance +
    // ⌘K command palette (§11.1 PR C), mirror the EN block's pattern.
    archiveLabel: "Archívum",
    paletteLabel: "Parancspaletta",
    palettePlaceholder: "Parancs vagy keresés…",
    paletteCmdTop: "Fel",
    paletteCmdAll: "Teljes nézet",
    paletteCmdDaily: "Napi nézet",
    paletteCmdWeekly: "Heti nézet",
    paletteCmdSwitchLang: "Nyelv váltása",
    paletteCmdLatest: "Legfrissebb hírlevél",
    // Owner: please review — new HU strings, catch-up banner + follow list
    // (§11.2), mirror the EN block's pattern.
    catchupPrefix: "Legutóbbi látogatásod óta:",
    catchupBriefings: "{n} hírlevél",
    catchupBriefingsOne: "{n} hírlevél",
    catchupArcUpdates: "{m} történetfrissítés",
    catchupArcUpdatesOne: "{m} történetfrissítés",
    catchupArcMore: "+{n} további",
    catchupJumpLabel: "Ugrás oda, ahol abbahagytad",
    catchupDismissLabel: "Elrejtés",
    followAdd: "☆ Követés",
    followRemove: "★ Követve",
    // Owner: please review — new HU string, "What changed" block (§11.3
    // delta persistence, ingest v4), mirrors the EN block's pattern.
    whatChangedLabel: "Mi változott",
  },
};

// ── language/view-space path helpers (keep every internal link inside the
// current language×view space: index↔index, digest↔digest, EN pages never
// link into /hu/ and vice versa, all-view pages never link into daily/ or
// weekly/ and vice versa, except via the two explicit switchers) ─────────

function indexHref(token, lang, view) {
  const langSeg = lang === "hu" ? "hu/" : "";
  const viewSeg = view === "daily" ? "daily/" : view === "weekly" ? "weekly/" : "";
  return `/t/${encodeURIComponent(token)}/${langSeg}${viewSeg}`;
}

function digestHref(token, lang, view, id) {
  const langSeg = lang === "hu" ? "hu/" : "";
  const viewSeg = view === "daily" ? "daily/" : view === "weekly" ? "weekly/" : "";
  return `/t/${encodeURIComponent(token)}/${langSeg}${viewSeg}d/${esc(id)}`;
}

// Like indexHref, with the ISO week's URL segment appended (roadmap 3 step
// 2) — week zero-padded to 2 digits ("W05", not "W5") so the address always
// matches the ISO 8601 "Www" shape regardless of week number. Only ever
// called with view="all" today (the daily view has no week address), but
// takes `view` like every other href helper here rather than hardcoding it.
function weekHref(token, lang, view, year, week) {
  const weekSeg = String(week).padStart(2, "0");
  return `${indexHref(token, lang, view)}w/${esc(year)}-W${esc(weekSeg)}/`;
}

// Like indexHref, but to the standalone search route (roadmap 4 step 7) —
// no `view` parameter: search has no daily/week variant (it spans the whole
// archive, see the file-header comment), so there's no view to select.
function searchHref(token, lang) {
  const langSeg = lang === "hu" ? "hu/" : "";
  return `/t/${encodeURIComponent(token)}/${langSeg}search`;
}

// Like searchHref, to the arc detail page (§11.1 PR A) — no `view`
// parameter either, same reasoning: a story arc spans every kind, not one
// view (see the file-header comment). `slug` is always route-regex-shaped
// (`[a-z0-9-]{1,64}`, see fetch()) by the time this is ever called, so esc()
// here is the same "free safety, not redundant trust" posture as everywhere
// else in this file rather than a defense against a real threat.
function arcHref(token, lang, slug) {
  const langSeg = lang === "hu" ? "hu/" : "";
  return `/t/${encodeURIComponent(token)}/${langSeg}a/${esc(slug)}`;
}

// `pageKind` ("index" | "digest") picks index vs. digest href — distinct
// from a digest row's own `kind` column (window/daily) used elsewhere.
// `archiveWeek` (roadmap 3 step 3): the CURRENT page's own {year, week} when
// it's an index page rendering an ARCHIVE week, else null — index pages on
// the current week or the (week-less) daily view pass null, and digest
// pages always pass null (a digest has no week address). When set, the
// other-language link must stay on that SAME week's index in the other
// language (weekHref) — falling back to that language's root index would
// silently bounce the reader from the archive week they're reading to the
// current week instead.
function renderLangSwitcher(token, lang, view, pageKind, id, archiveWeek = null) {
  const otherLangIndexHref = (otherLang) =>
    archiveWeek
      ? weekHref(token, otherLang, view, archiveWeek.year, archiveWeek.week)
      : indexHref(token, otherLang, view);
  // Arc pages (§11.1 PR A, pageKind "arc"): the other-language link targets
  // the SAME arc's page in that language space — `id` doubles as the arc's
  // identity here (arc pages have no numeric id; stable arc keys made this a
  // key-or-slug string, but this call site just echoes it back unchanged),
  // and arcHref takes no `view` (an arc spans every view, see arcHref's own
  // comment), same reasoning as why the "digest" branch below uses
  // digestHref instead of the index href.
  const otherLangHref = (otherLang) => {
    if (pageKind === "index") return otherLangIndexHref(otherLang);
    if (pageKind === "arc") return arcHref(token, otherLang, id);
    return digestHref(token, otherLang, view, id);
  };
  const enHref = otherLangHref("en");
  const huHref = otherLangHref("hu");
  // Current language: plain bold text, not a link (nothing to switch to).
  // Other language: a link to the SAME page (same index row / same digest
  // id) in the other language space, same view.
  const en = lang === "en" ? "<strong>EN</strong>" : `<a href="${enHref}">EN</a>`;
  const hu = lang === "hu" ? "<strong>HU</strong>" : `<a href="${huHref}">HU</a>`;
  return `<span class="langswitch">${en} | ${hu}</span>`;
}

// The view switcher's "other view" link is ALWAYS an index href, on both
// index and digest pages — see the file-header comment ("Daily-brief view")
// for why a digest page can't link into another view's own digest.
// The view selector is the site's PRIMARY navigation (owner decision) —
// rendered as a pill capsule that rides in the masthead row itself, beside
// the brand (owner-requested masthead compaction: this used to be its own
// centered band below the masthead; see pageChrome's .mastleft, which now
// groups the two together, and the .mast/.mastleft/.viewtabs CSS for the
// layout). Active tab = filled accent pill (plain text, not a link);
// inactive = outlined link. On a digest page the inactive tab targets that
// view's INDEX (a window digest has no address in the daily or weekly view
// — long-standing design choice).
// Unlike the language switcher, this deliberately does NOT thread a week
// through (roadmap 3 step 3): a week page's tabs still target the view's
// root index with no week segment — a week page has no daily or weekly
// twin to keep the week address for, so there's nothing to preserve here.
function renderViewTabs(token, lang, view) {
  const strings = STRINGS[lang];
  // data-view (§11.1 PR C, ⌘K palette): lets the palette script identify
  // which view a tab targets without parsing translated label text — see
  // collectPaletteItems in pageChrome. Purely a JS hook, no visual/no-JS
  // effect; carried on both the active <span> and the linked <a> variants
  // for consistency even though the palette only ever reads it off the
  // linked ones (an active tab has no href to offer).
  const tab = (v, label) =>
    view === v
      ? `<span class="viewtab active" data-view="${v}">${esc(label)}</span>`
      : `<a class="viewtab" data-view="${v}" href="${indexHref(token, lang, v)}">${esc(label)}</a>`;
  return `<nav class="viewtabs">${tab("all", strings.viewAll)}${tab("daily", strings.viewDaily)}${tab("weekly", strings.viewWeekly)}</nav>`;
}

// The masthead's top-right cluster (language, theme, size, density)
// collapses into one gear button that opens a floating settings bubble
// (owner redesign) — a native <details>/<summary> disclosure, so the panel
// opens with NO JS. The language links inside keep working for no-JS
// readers exactly as before; the density button keeps its existing class
// and hidden-until-JS contract untouched. Theme and size are miniseg button
// groups (owner upgrade: three-state theme, S/M/L text size) — same
// hidden-until-JS contract, wired by their own IIFEs below in pageChrome.
// The masthead Archive link this row used to also carry (§11.1 PR C,
// `showArchive` param) was removed in the index-cleanup pass — archive weeks
// are reachable via the week rail's own ← link now (see renderWeekRail), so
// there's nothing left to gate a link on here.
// `showSearch` (owner-requested): renders the search control immediately
// LEFT of the settings gear in the masthead. An explicit flag rather than
// `pageKind === "index"` for the same reason the archive link needed one —
// renderSearchPage also passes "index" (for its density row) and must not
// get it. Index pages only, because the popover's `.filter` input drives
// the LEDGER: on a digest or arc page it would be a dead control, and the
// ⌘K palette already carries search everywhere.
function renderSwitchers(token, lang, view, pageKind, id, archiveWeek = null, showSearch = false) {
  const strings = STRINGS[lang];
  const langRow = `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsLanguage)}</span>${renderLangSwitcher(token, lang, view, pageKind, id, archiveWeek)}</div>`;
  // Theme is now a three-state Light/Auto/Dark miniseg (owner redesign),
  // not the old two-state ◐ toggle — see the theme IIFE in pageChrome for
  // why Auto needs to be a real, distinct state rather than an implied
  // default. Buttons start hidden (progressive enhancement, same contract
  // the old toggle had); the IIFE unhides and wires them.
  const themeRow = `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsTheme)}</span><span class="miniseg miniseg-theme" role="group" aria-label="${esc(strings.themeToggle)}"><button class="minisegbtn" data-set="light" hidden>${esc(strings.themeLight)}</button><button class="minisegbtn" data-set="auto" hidden>${esc(strings.themeAuto)}</button><button class="minisegbtn" data-set="dark" hidden>${esc(strings.themeDark)}</button></span></div>`;
  // Text size: S/M/L miniseg, same shape as theme's above — M is the
  // absence of an override (owner-tuned defaults stay the single source of
  // truth), so only s/l ever get set/stored. Letters are literal, not
  // STRINGS-keyed — "S"/"M"/"L" read the same in both languages.
  const sizeRow = `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsTextSize)}</span><span class="miniseg miniseg-size" role="group" aria-label="${esc(strings.settingsTextSize)}"><button class="minisegbtn" data-set="s" hidden>S</button><button class="minisegbtn" data-set="m" hidden>M</button><button class="minisegbtn" data-set="l" hidden>L</button></span></div>`;
  // Density toggle (roadmap 4 step 4): index pages only — it governs the
  // ledger's .entry padding/clamp, which a digest page has none of, so the
  // row would be a dead control there.
  const densityRow =
    pageKind === "index"
      ? `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsDensity)}</span><button class="densitytoggle" aria-label="${esc(strings.densityToggle)}" hidden>▤</button></div>`
      : "";
  // The gear carries a visible text label on desktop (owner-requested) and
  // collapses to the bare icon on the phone — the label span is hidden by
  // the mobile media block, the aria-label covers it everywhere.
  const searchHtml = showSearch ? renderSearchBubble(token, lang, strings) : "";
  return `${searchHtml}<details class="settings"><summary class="gear" aria-label="${esc(strings.settingsLabel)}">⚙<span class="gearlabel">${esc(strings.settingsLabel)}</span></summary><div class="settingspanel">${langRow}${themeRow}${sizeRow}${densityRow}</div></details>`;
}

// ── page chrome (shared masthead/footer/CSS — one template, both pages) ─

// The tab icon: the site's indigo accent as a rounded square, three white
// "digest lines" of tapering width — reads as a summary/list at 16px, and
// the indigo works against both light and dark browser chrome.
const FAVICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#4f46e5"/>
<rect x="14" y="18" width="36" height="6" rx="3" fill="#ffffff"/>
<rect x="14" y="30" width="28" height="6" rx="3" fill="#ffffff" opacity="0.85"/>
<rect x="14" y="42" width="20" height="6" rx="3" fill="#ffffff" opacity="0.7"/>
</svg>`;

function brandParts(host) {
  const idx = host.indexOf(".");
  if (idx === -1) return { first: host, rest: "" };
  return { first: host.slice(0, idx), rest: host.slice(idx) };
}

const CSS = `
  /* Three type roles, all zero-byte system stacks (roadmap step 3 — "the
     private wire desk"): PROSE for the sit-back-and-read register (article
     body, TL;DR, excerpts), DATA for anything keyed on time (times, counts,
     datelines, citation chips), and chrome — the masthead/tabs/nav/footer —
     which stays the existing body sans stack with no variable of its own.
     Time is this site's primary key; the typography should say so. */
  :root {
    /* Owner-reported white flash when stepping between pages: every page
       is a fresh no-store document, and in the network gap before its
       first paint the browser shows its OWN canvas — which defaults to
       WHITE unless the page declares color-scheme. (The navigation
       crossfade usually hides the gap; a slow response outruns the
       snapshot, which is why the flash was only intermittent.) light dark
       lets the UA pick the canvas by OS preference — the Auto case; the
       data-theme override blocks below pin it to one scheme, keeping the
       between-pages canvas in lockstep with the manual theme choice. */
    color-scheme: light dark;
    --font-prose: ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
    --font-data: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;

    --bg: #fbfaf7; /* barely-warm paper — deliberately NOT cream */
    --page-bg: #e9e9f2;
    --text: #16181d; /* ink */
    --muted: #6e7380; /* old #8a8f9e was ~3.4:1 on the new paper; this clears 4.5:1 */
    --accent: #4f46e5;
    --accent-strong: #4338ca;
    --tldr-bg: #eef2ff;
    --tldr-text: #262a49;
    --chip-bg: #dde3ff;
    --chip-text: #4338ca;
    --hairline: #e7e5e0; /* warmed to match the new paper */
    --h2-border: #4f46e5;
    --attention-bg: #fef3c7;
    --attention-text: #78350f;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #17181c;
      --page-bg: #0f0d17;
      --text: #e6e6ea;
      --muted: #8a8f9e;
      --accent: #a5b4fc;
      --accent-strong: #c7d2fe;
      --tldr-bg: #262841;
      --tldr-text: #dfe3ff;
      --chip-bg: #33355c;
      --chip-text: #c7d2fe;
      --hairline: #2a2c33;
      --h2-border: #6366f1;
      --attention-bg: #4d3800;
      --attention-text: #ffe69c;
    }
  }

  /* Manual theme override (roadmap step 6): a data-theme attribute on <html>,
     set by the early head script from localStorage, has to beat BOTH the
     base :root above and the prefers-color-scheme:dark block above it —
     regardless of which way the OS is set. CSS has no way to reference a
     media block's resolved values from outside it, so with no build step the
     only option is a third, explicit copy of each palette. Three copies is
     the price of a manual override without a build step, and the palette
     changes rarely. Deliberately only the COLOR variables are duplicated —
     --font-prose/--font-data are identical in every theme and stay defined
     once, above. */
  :root[data-theme="dark"] {
    /* Pin the UA canvas too — see the color-scheme comment in :root. */
    color-scheme: dark;
    --bg: #17181c;
    --page-bg: #0f0d17;
    --text: #e6e6ea;
    --muted: #8a8f9e;
    --accent: #a5b4fc;
    --accent-strong: #c7d2fe;
    --tldr-bg: #262841;
    --tldr-text: #dfe3ff;
    --chip-bg: #33355c;
    --chip-text: #c7d2fe;
    --hairline: #2a2c33;
    --h2-border: #6366f1;
    --attention-bg: #4d3800;
    --attention-text: #ffe69c;
  }
  :root[data-theme="light"] {
    /* Pin the UA canvas too — see the color-scheme comment in :root. */
    color-scheme: light;
    --bg: #fbfaf7;
    --page-bg: #e9e9f2;
    --text: #16181d;
    --muted: #6e7380;
    --accent: #4f46e5;
    --accent-strong: #4338ca;
    --tldr-bg: #eef2ff;
    --tldr-text: #262a49;
    --chip-bg: #dde3ff;
    --chip-text: #4338ca;
    --hairline: #e7e5e0;
    --h2-border: #4f46e5;
    --attention-bg: #fef3c7;
    --attention-text: #78350f;
  }

  * { box-sizing: border-box; }
  /* The hidden attribute must actually hide, whatever else is styled.
     Author rules beat the UA stylesheet's own hidden-means-display-none
     rule regardless of specificity, so ANY element this file gives an
     explicit display to stayed VISIBLE when the scripts below set
     el.hidden = true. That silently broke two features: the unread fence
     (patched at the time with a one-off selector) and then the index
     filter, where .entry's own display: block meant a filtered-out entry
     was marked hidden in the DOM and still painted on screen — typing in
     the filter appeared to do nothing at all (owner-reported). One global
     override kills the whole class of bug instead of one selector at a
     time, and !important is what makes it beat the author display rules it
     exists to correct (normalize.css ships the same rule for the same
     reason). Everything toggled by the hidden attribute — the filter
     input, the theme toggle, the countdown, entries, day headers, the
     fence — is covered by this one line. NOTE: no backticks in this
     comment; the whole CSS block is a JS template literal. */
  [hidden] { display: none !important; }
  /* Reserve the scrollbar's gutter even when the page is too short to
     scroll: the All view scrolls, a near-empty Daily view doesn't, and
     without this the viewport width changes on switch — sliding the
     centered bubble sideways by half a scrollbar (owner-reported). A
     no-op on overlay-scrollbar platforms, which never had the shift. */
  html { scrollbar-gutter: stable; }
  /* Second layer of the anti-flash fix (see :root's color-scheme comment):
     an explicit root background so overscroll and any pre-body-paint gap
     show the theme's own paper/ink, never the UA default. The desktop
     bubble layout overrides this to the purple page background below. */
  html { background: var(--bg); }
  /* A single unbreakable token wider than a phone screen (production
     digests carry them — a defanged URL from the link allowlist is one
     long word) widens the LAYOUT viewport past the visual one. On iOS
     Safari that detaches position:fixed elements from the screen edge
     (the back button floats mid-page) and opens pannable blank space
     past the footer (owner-reported, 2026-08-09). Two guards:
     overflow-wrap (inherited everywhere from body) breaks such tokens at
     the container edge, and overflow-x: clip caps the layout viewport at
     device width even if some future shape still overflows — clip, not
     hidden, so html doesn't become a scroll container. */
  html { overflow-x: clip; }
  /* Smooth-scroll the TOC's #sN anchor jumps (roadmap step 5), gated behind
     prefers-reduced-motion so motion-sensitive readers get the instant jump
     instead. */
  @media (prefers-reduced-motion: no-preference) {
    html { scroll-behavior: smooth; }
    /* MPA view transitions (roadmap 2 step 7): one at-rule turns on the
       browser's default crossfade between full-page navigations on this
       origin; browsers without support (most, today) simply ignore an
       at-rule they don't recognize — progressive, no fallback needed. No
       custom ::view-transition-* choreography — ambient feel, not a show
       (restraint, matching the "keep the default crossfade" decision). A
       crossfade IS motion, so it gets the exact same reduced-motion gate as
       scroll-behavior above, not a separate one. */
    @view-transition {
      navigation: auto;
    }
  }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    line-height: 1.6;
    font-size: 17px;
    overflow-wrap: break-word; /* inherited: see the overflow-x note above */
  }
  /* Text size (owner upgrade): S/M/L scales the READING text only — the
     article body (.digest, which carries the TL;DR callout and section
     h2s proportionally inside it) and the index/search excerpts. The
     first cut scaled the whole body instead and the owner immediately
     flagged it: the entire UI zoomed, which reads as a broken viewport,
     not a text-size preference — chrome (masthead, tabs, pills, meta
     rows) must hold its owner-tuned rhythm while only the prose moves.
     em factors, not px, so the 17px/15px desktop/phone bases scale
     without a second media-scoped set of rules. M stays the ABSENCE of
     data-fontsize: the defaults above remain the single source of truth,
     s/l are offsets from them, never a competing "normal". The
     .entry-lead .excerpt variants exist because these :root-prefixed
     rules outrank the lead card's own base font-size — without them,
     compact S would shrink the lead below its deliberate extra weight. */
  :root[data-fontsize="s"] .digest { font-size: 0.94em; }
  :root[data-fontsize="l"] .digest { font-size: 1.12em; }
  :root[data-fontsize="s"] .entry .excerpt { font-size: 0.87em; }
  :root[data-fontsize="l"] .entry .excerpt { font-size: 1.04em; }
  :root[data-fontsize="s"] .entry-lead .excerpt { font-size: 0.96em; }
  :root[data-fontsize="l"] .entry-lead .excerpt { font-size: 1.14em; }
  a { color: var(--accent); }
  /* overflow-x: clip HERE, on a non-root element, is the actual guarantee
     against the phone layout-viewport bug (owner-reported twice,
     2026-08-09): root-level clip demonstrably does NOT stop wide content
     from expanding the layout viewport (measured: one long pre line took a
     375px viewport to 1500px with the html rule in place), which detaches
     fixed elements and opens pannable dead space past the footer on iOS.
     Clipping inside .wrap means overflow can never widen the page geometry
     again, whatever content shape causes it next. Sticky day headers keep
     working (clip creates no scroll container) and the fixed back button is
     unaffected (no containing-block change). */
  .wrap { max-width: 42em; margin: 0 auto; padding: 0 1.25em 4em; overflow-x: clip; }

  /* Masthead compaction (owner-requested), three-zone revision (owner
     follow-up: "the switcher isn't at the middle"): brand zone left, the
     view-tab capsule as the mast's own MIDDLE flex child, gear zone right
     (see pageChrome). The two side zones carry flex: 1 1 0 so they grow
     equally from nothing — that equal growth is what centers the capsule
     on the row's true midpoint instead of on the leftover space after a
     wider brand. center, not baseline: a wordmark next to a rounded pill
     capsule reads better lined up on vertical centers than on a shared
     text baseline. Desktop is one nowrap row; the ≤40em block below
     rewraps the capsule onto its own centered second line. */
  header.mast {
    display: flex; align-items: center;
    gap: 0.6em 1em; padding: 1.4em 0 1em;
    border-bottom: 1px solid var(--hairline); margin-bottom: 1.6em;
  }
  /* True centering (owner follow-up: "the switcher isn't at the middle"):
     the tabs are now the mast's own MIDDLE flex child (see pageChrome —
     they moved out of .mastleft), and the two side zones get equal
     flex-grow from a zero basis, so the capsule centers on the ROW's
     midpoint, not on whatever space the brand happens to leave over. The
     brand zone and the gear zone are never the same natural width; without
     the 1 1 0 pair the capsule visibly hangs left. Desktop keeps one
     nowrap row; the phone block below rewraps the tabs onto their own
     centered second line instead of squeezing three zones into 375px. */
  .mast .mastleft { display: flex; align-items: center; flex: 1 1 0; min-width: 0; }
  /* 0.95em, down from the pre-compaction 1.05em: a ~10% trim, not a demotion
     — still bolder (font-weight 700) and letter-spaced tighter than
     everything else in the row, so the wordmark still reads first, it just
     no longer visually outweighs the tab capsule sitting right beside it.
     Landed on 0.95em specifically because it matches .viewtab's own
     font-size (also 0.95em) — brand and tabs now share one type step, which
     is what makes the merged row read as ONE compact band instead of a
     big label with small chrome tacked on. */
  .mast .brand { font-weight: 700; font-size: 0.95em; letter-spacing: -0.01em; text-decoration: none; color: var(--text); }
  .mast .brand .tld { color: var(--accent); }
  /* The settings gear (language/theme/size/density, collapsed into one
     details.settings disclosure — see renderSwitchers) sits top-right in
     the masthead via .mastright, right-aligned — same markup at both
     breakpoints. */
  /* flex: 1 1 0 pairs with .mastleft's — the two equal-growth side zones
     are what make the middle tab capsule center on the row's true midpoint
     (see the header.mast comment above). */
  /* Row, not column: the search control now sits beside the gear
     (owner-requested placement), and the column direction only ever
     existed to stack the since-removed archive link above it. justify-end
     keeps the pair pinned right; flex: 1 1 0 pairs with .mastleft's to
     centre the view-tab capsule between them. */
  .mast .mastright {
    display: flex; align-items: center; justify-content: flex-end;
    gap: 0.55em; flex: 1 1 0;
  }

  /* Mobile masthead + phone font size (owner-tuned). */
  @media (max-width: 40em) {
    /* 15px: phone type ran large even at 16 (owner feedback, twice). The
       S/M/L text-size rules need no phone twin — they're em factors off
       this base, see the data-fontsize block above. */
    body { font-size: 15px; }
    /* Icon-only gear on the phone — see .gearlabel above. */
    .gearlabel { display: none; }
    /* Masthead phone posture (true-centering revision): three zones don't
       fit at 375px, so the capsule takes its own SECOND line, centered.
       The line break must be FORCED, not hoped for: the side zones carry
       min-width: 0 / flex-basis 0, so flex would happily crush them to
       nothing and cram all three items onto one overlapping line
       (observed live at 375px) — a zero-height, full-basis pseudo-item at
       order 3 breaks the line deterministically instead. Line one is then
       brand left + gear right (the gear can never end up alone under the
       tabs); the order-4 capsule lands on line two, auto side margins
       centering it while it keeps its fit-content width — flex-basis:100%
       on the capsule itself would have stretched its border full-bleed.
       .mastright stays a real box (the settings bubble never depended on
       it — details.settings is its own anchor, see that comment below). */
    header.mast { flex-wrap: wrap; }
    header.mast::before { content: ""; flex-basis: 100%; order: 3; }
    .mast .viewtabs { order: 4; margin-left: auto; margin-right: auto; }
  }
  .mast .langswitch, .mast .viewswitch { font-size: 0.85em; font-variant-numeric: tabular-nums; }
  .mast .langswitch a, .mast .viewswitch a { text-decoration: none; }
  .mast .langswitch strong, .mast .viewswitch strong { color: var(--text); }

  /* Settings bubble (owner redesign): the gear button collapses language,
     theme, and density into one disclosure. details.settings — NOT
     .mastright — is the positioning anchor for .settingspanel below.
     (Historically load-bearing: the phone block used to erase .mastright
     as a box via display: contents, so only the <details> could anchor the
     absolute panel. The masthead compaction removed that collapse, but the
     anchor choice stays — it was never wrong, and moving it buys nothing.) */
  details.settings { position: relative; }
  summary.gear {
    list-style: none;
    background: none; border: 1px solid var(--hairline); border-radius: 999px;
    color: var(--text); font-size: 0.9em; padding: 0.05em 0.5em; cursor: pointer;
  }
  /* iOS Safari draws its own disclosure triangle on <summary> even with
     list-style: none — this is the belt-and-suspenders rule that actually
     suppresses it. */
  summary.gear::-webkit-details-marker { display: none; }
  /* Desktop shows the gear WITH its text label ("Settings"/"Beállítások" —
     owner-requested); the phone masthead is tight, so the label collapses
     there and the icon stands alone (see the mobile block below). */
  .gearlabel { margin-left: 0.4em; font-size: 0.92em; }
  summary.gear:hover { border-color: var(--accent); }
  summary.gear:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }
  details.settings[open] > summary.gear { border-color: var(--accent); }
  .settingspanel {
    position: absolute; right: 0; top: calc(100% + 0.5em);
    /* Must clear the sticky day headers (.dayhead, z-index: 1) or the panel
       would open underneath the ledger once the reader has scrolled. */
    z-index: 20;
    background: var(--bg); border: 1px solid var(--hairline); border-radius: 14px;
    padding: 0.9em 1.1em;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28); /* same recipe as .backfab's */
    display: flex; flex-direction: column; gap: 0.7em; min-width: 13em;
  }
  .settingsrow { display: flex; justify-content: space-between; align-items: baseline; gap: 1.2em; }
  .settingslabel {
    font-family: var(--font-data); font-size: 0.7em; text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--muted);
  }

  /* Settings bubble open/close animation (owner-requested). details/summary
     has no transition of its own to hook, so this is a fresh keyframe pair
     rather than a transition. A NEW prefers-reduced-motion: no-preference
     gate — not the global one near the top of this stylesheet, which is
     scroll/view-transition territory and unrelated to this feature.
     Opening plays on details.settings[open] .settingspanel directly (native
     open needs no JS). Closing plays on a "panelclosing" class the
     settings-close IIFE below adds before it sets details.open = false
     itself — <details> snaps shut instantly with no hook to intercept, so
     the class is what buys the mirrored animation time to play before
     removal. Named "panelclosing", not the shorter "closing" the owner's
     brief used, because .closing already exists on this page (the digest
     article's closing-line paragraph, below) — reusing that name would
     have leaked its border/italic/spacing styling onto the settings panel
     for the animation's duration. The close rule below repeats the [open]
     prefix (not just .settingspanel.panelclosing) SPECIFICALLY so it
     outranks the open rule above on specificity: the "panelclosing" class
     is added while open is still true — the JS only flips open to false at
     the end of the delay — so both rules target the same element at once,
     and without the matching prefix the open rule's animation would win by
     cascade order and the close animation would never actually play. */
  @media (prefers-reduced-motion: no-preference) {
    details.settings[open] .settingspanel { animation: settingsopen 160ms ease-out; }
    details.settings[open] .settingspanel.panelclosing { animation: settingsclose 120ms ease-in; }
    @keyframes settingsopen {
      from { opacity: 0; transform: translateY(-4px) scale(0.98); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    @keyframes settingsclose {
      from { opacity: 1; transform: translateY(0) scale(1); }
      to { opacity: 0; transform: translateY(-4px) scale(0.98); }
    }
  }

  /* Density toggle (roadmap 4 step 4): small pill button living inside the
     settings bubble's panel (see .settingspanel above). hidden by default,
     un-hidden by the bottom script — no JS, no button, same progressive-
     enhancement contract as the index filter input below. Theme and text
     size (owner upgrade) moved off this single-pill look onto the miniseg
     control just below — density stays a pill since it's genuinely binary
     (compact/comfortable), not a 3-way choice. */
  .densitytoggle {
    background: none; border: 1px solid var(--hairline); border-radius: 999px;
    color: var(--text); font-size: 0.8em; padding: 0.05em 0.5em; cursor: pointer;
  }
  .densitytoggle:hover { border-color: var(--accent); }
  .densitytoggle:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }

  /* Mini segmented control (owner upgrade: three-state theme, S/M/L text
     size) — the view tabs' segmented language (.viewtabs/.viewtab above)
     miniaturized to panel scale, so the settings bubble reads as one family
     with the site's primary navigation instead of inventing a new shape. */
  .miniseg {
    display: inline-flex; border: 1px solid var(--hairline); border-radius: 999px;
    overflow: hidden;
  }
  .minisegbtn {
    background: none; border: none; color: var(--accent);
    font: inherit; font-size: 0.78em; padding: 0.18em 0.7em; cursor: pointer;
  }
  .minisegbtn + .minisegbtn { border-left: 1px solid var(--hairline); }
  .minisegbtn.active { background: var(--accent); color: var(--bg); }
  .minisegbtn:not(.active):hover { background: var(--tldr-bg); }
  /* Inset outline: an outset ring would get clipped by .miniseg's
     overflow: hidden — same note as .viewtab:focus-visible above. */
  .minisegbtn:focus-visible { outline: 2px solid var(--text); outline-offset: -2px; }

  /* ⌘K command palette (§11.1 PR C): markup is injected at runtime (see the
     palette IIFE in pageChrome, below) — no server-rendered dialog HTML, so
     everything it needs lives here. Deliberately NOT a card — one dialog,
     thin hairlines, mono input, no drop-shadow-heavy chrome (design
     guidance: "no card soup"). Dark-first like the rest of the site: it
     reads off the same --bg/--text/--muted/--hairline/--accent tokens as
     everything else, so it never needs its own light/dark handling. Hidden
     via the plain [hidden] attribute (see the global rule above), same
     progressive-enhancement contract as the filter input/theme toggle. */
  .cmdpalette-backdrop {
    position: fixed; inset: 0; z-index: 40;
    background: rgba(0, 0, 0, 0.55);
    display: flex; justify-content: center; align-items: flex-start;
    padding: 12vh 1em 0;
  }
  .cmdpalette {
    width: min(34em, 100%); max-height: 70vh;
    background: var(--bg); border: 1px solid var(--hairline); border-radius: 10px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
    display: flex; flex-direction: column; overflow: hidden;
  }
  @media (prefers-reduced-motion: no-preference) {
    .cmdpalette { animation: cmdpaletteopen 120ms ease-out; }
    @keyframes cmdpaletteopen {
      from { opacity: 0; transform: translateY(-6px); }
      to { opacity: 1; transform: translateY(0); }
    }
  }
  .cmdpalette-input {
    font-family: var(--font-data); font-size: 1em; color: var(--text);
    background: none; border: none; border-bottom: 1px solid var(--hairline);
    padding: 0.85em 1em; outline: none;
  }
  .cmdpalette-input::placeholder { color: var(--muted); }
  .cmdpalette-list { overflow-y: auto; padding: 0.35em 0; }
  .cmdpalette-item {
    padding: 0.55em 1em; font-size: 0.93em; color: var(--text);
    font-family: var(--font-prose); cursor: pointer;
    display: flex; justify-content: space-between; gap: 1em;
  }
  .cmdpalette-item .cmdpalette-group {
    font-family: var(--font-data); font-size: 0.72em; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--muted); align-self: center;
  }
  .cmdpalette-item.active { background: var(--tldr-bg); }
  .cmdpalette-empty { padding: 0.85em 1em; font-size: 0.9em; color: var(--muted); }

  .dayhead {
    font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.09em;
    color: var(--muted); margin: 2.2em 0 0.4em; font-weight: 600;
    font-family: var(--font-data); /* mono uppercase eyebrow = the wire look */
    /* Sticky so mid-scroll position is always visible (roadmap step 4).
       var(--bg) background keeps entry text from showing through as it
       scrolls underneath — correct on both mobile (full-bleed) and desktop
       (the bubble card is the scroll context's background too). */
    position: sticky; top: 0; background: var(--bg); padding: 0.35em 0;
    z-index: 1;
  }
  .entry {
    display: block; text-decoration: none; color: inherit;
    padding: 1.05em 0; border-bottom: 1px solid var(--hairline);
  }
  /* Lead card (roadmap step 4): the newest digest in the current view,
     rendered above the ledger with visual weight but no new color — bigger
     unclamped excerpt and a mono dateline eyebrow in place of the usual
     time+count meta line. It's the first thing in the section, so no extra
     top border beyond the shared .entry bottom hairline. */
  .entry-lead { padding: 1.2em 0 1.4em; }
  .entry-lead .eyebrow-text {
    font-family: var(--font-data); font-size: 0.75em; letter-spacing: 0.08em;
    color: var(--accent); text-transform: uppercase;
  }
  .entry:hover .excerpt, .entry:focus-visible .excerpt { color: var(--text); }
  .entry:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; border-radius: 4px; }
  .entry .meta {
    display: flex; align-items: baseline; gap: 0.7em; margin-bottom: 0.25em;
    font-variant-numeric: tabular-nums;
  }
  /* 0.85em, not 0.95: mono runs wide, so the time nudges down to keep its
     old visual weight in the meta row now that it's set in --font-data. */
  .entry .time { font-weight: 700; font-size: 0.85em; font-family: var(--font-data); }
  /* Daily-brief entries carry the indigo accent on their time instead of the
     default text color — the "slightly heavier presence" this one entry
     type gets in an otherwise undifferentiated list. */
  .entry .time.time-accent { color: var(--accent); }
  /* 0.75em, not 0.8: same mono-runs-wide compensation as .entry .time. */
  .entry .count { color: var(--muted); font-size: 0.75em; font-family: var(--font-data); }
  .entry .flag {
    font-size: 0.72em; font-weight: 600; padding: 0.1em 0.55em; border-radius: 99px;
    background: var(--attention-bg); color: var(--attention-text);
    /* Two-word badges ("weekly report", "daily brief") were wrapping into
       two-line pills in the lead card's meta row (owner-reported from the
       first live weekly). A badge is a tag, not a paragraph — one line,
       always. The meta row itself stays nowrap: the pill's min-content
       width just wins, and the eyebrow text (which wraps internally)
       absorbs the squeeze; .wrap's overflow-x clip guards the extreme. */
    white-space: nowrap;
  }
  /* Neutral/muted variant, reused by two chips: the "EN" fallback note on
     untranslated HU index entries, and the degraded-run badge (roadmap 2
     step 8, renderDegradedBadge) — neither is a warning-colored call to
     action, just metadata about the entry; the degraded badge's own ⚠
     prefix (baked into the string, not CSS) is what tells the two apart.
     Reuses .flag's shape/sizing. */
  .entry .flag.flag-muted, .entry .flag.flag-degraded { background: var(--chip-bg); color: var(--chip-text); }
  /* Daily-brief badge — same indigo chip-bg/chip-text tokens as .flag-muted,
     but filled/inverted (solid indigo, not the soft pastel) so it reads as
     its own distinct badge rather than the muted EN language note, and
     stays clearly apart from the amber attention pill. */
  .entry .flag.flag-daily { background: var(--chip-text); color: var(--chip-bg); }
  .entry .excerpt {
    margin: 0; color: var(--muted); font-size: 0.93em;
    font-family: var(--font-prose);
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }
  /* Daily-brief entries summarize a whole day, not a 3-hour window — one
     extra clamped line of excerpt room. */
  .entry .excerpt.excerpt-daily { -webkit-line-clamp: 4; }
  .entry .excerpt strong { color: var(--text); }
  /* Placed after .entry .excerpt (same specificity, later wins the cascade)
     so the lead card's excerpt actually loses its clamp instead of being
     silently overridden back to 3 lines. */
  .entry-lead .excerpt {
    font-size: 1.02em; display: block; -webkit-line-clamp: unset; overflow: visible;
  }

  /* Ledger density toggle (roadmap 4 step 4): compact tightens the ledger's
     .entry padding and excerpt clamp when data-density="compact" is set
     (persisted in localStorage, applied pre-paint by the head script, same
     pattern as data-theme). Scoped to .entry:not(.entry-lead) — the lead
     card is the day's headline, not ledger noise, and compaction is for the
     ledger only; without :not() this compact clamp would win on specificity
     over .entry-lead .excerpt's own un-clamp above, since both come later in
     the cascade than plain .entry .excerpt. */
  :root[data-density="compact"] .entry:not(.entry-lead) { padding: 0.55em 0; }
  :root[data-density="compact"] .entry:not(.entry-lead) .excerpt { -webkit-line-clamp: 2; }
  :root[data-density="compact"] .entry:not(.entry-lead) .excerpt.excerpt-daily { -webkit-line-clamp: 3; }

  /* The whole nav is prev/next times plus the "all digests" link — one word
     — so the entire block goes mono rather than singling out the times. */
  nav.digestnav {
    display: flex; justify-content: space-between; gap: 1em;
    font-size: 0.85em; margin-bottom: 1.8em;
    font-family: var(--font-data);
  }
  nav.digestnav a { text-decoration: none; }
  nav.digestnav .spacer { flex: 1; }
  /* Bottom mirror of the same nav, after </article> (roadmap step 2) — reads
     as a continuation of the article's closing line, not a new nav block:
     same top-hairline + padding treatment as .closing, font-size/behavior
     otherwise identical to the top nav above. */
  nav.digestnav.digestnav-bottom {
    margin-top: 2.5em; padding-top: 1em;
    border-top: 1px solid var(--hairline);
  }
  /* font-variant-numeric dropped here: --font-data is monospace, so digits
     are already fixed-width — tabular-nums would be redundant. The dateline
     IS the page's identity line, promoted from muted metadata to the wire
     header (roadmap step 5). */
  .stamp {
    color: var(--text); font-size: 0.78em; margin: 0 0 1.4em; font-family: var(--font-data);
    text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600;
    padding-bottom: 0.9em; border-bottom: 1px solid var(--hairline);
  }
  /* Index empty-state message — its own class, NOT .stamp: the stamp became
     the digest page's uppercase wire dateline above, and "No digests yet."
     must stay quiet muted prose, not a shouted header. */
  .empty { color: var(--muted); font-size: 0.85em; margin: 2em 0; }
  /* HU digest page, no body_html_hu on file: shown above the article,
     falling back to the English body. */
  .en-only-note { color: var(--muted); font-size: 0.85em; font-style: italic; margin: 0 0 1em; }
  /* Arc page title (§11.1 PR A, renderArcPage) — the site's first real <h1>;
     every other page uses the masthead brand link instead. Editorial
     register (design guidance: headlines are prose, not chrome), so serif
     like the article body, not the sans/mono chrome voice — sized down from
     a typical article h1 to stay in proportion with this site's otherwise
     restrained type scale (the digest article's own h2s top out at 1.15em). */
  .archead { display: flex; align-items: baseline; flex-wrap: wrap; gap: 0.7em; }
  .arctitle {
    font-family: var(--font-prose); font-size: 1.5em; font-weight: 700;
    line-height: 1.3; margin: 0 0 0.5em; text-wrap: balance;
  }
  /* Follow toggle (§11.2, optional feature): client-injected into .archead,
     next to the arc title — see the follow-toggle IIFE in pageChrome. Text
     control per the design guidance, deliberately no button chrome (no
     border/background/pill) — same restraint as the design guidance's
     "calm urgency" principle applied to a control instead of a status. Mono
     metadata register (matches .nowmeta/.catchup) rather than the h1's
     editorial serif, so it visually reads as interface, not headline. */
  .followtoggle {
    background: none; border: 0; padding: 0; margin: 0 0 0.5em;
    font-family: var(--font-data); font-size: 0.75em; color: var(--muted);
    cursor: pointer;
  }
  .followtoggle:hover, .followtoggle:focus-visible { color: var(--accent); }
  .followtoggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
  /* Section index (TOC, roadmap step 5): chip-link row built from the
     article's own <h2>s at render time (see buildSectionToc). Chips speak
     for themselves — no label string. */
  .toc { display: flex; flex-wrap: wrap; gap: 0.45em; margin: 0 0 1.6em; }
  .toc a {
    font-size: 0.78em; padding: 0.22em 0.8em; border-radius: 999px;
    border: 1px solid var(--hairline); color: var(--accent);
    text-decoration: none; background: transparent;
  }
  .toc a:hover { border-color: var(--accent); }
  .toc a:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }

  /* Story-arc line (roadmap 4 step 8, renderArcs): chips in the mono data
     voice, same family as .sourcekey's .sk swatches below. Each chip is a
     LINK to that slug's arc page (§11.1 PR A) — text-decoration: none plus
     the explicit hover/focus rules below are what keep it reading as a
     provenance chip stating which thread this briefing continues, not as a
     button; see renderArcs. */
  .arcs { display: flex; flex-wrap: wrap; gap: 0.45em; margin: 0 0 1.2em; }
  .arcs .arc {
    font-family: var(--font-data); font-size: 0.72em; text-transform: uppercase;
    letter-spacing: 0.06em; padding: 0.22em 0.8em; border-radius: 999px;
    background: var(--chip-bg); color: var(--chip-text); text-decoration: none;
    /* Topics derive from section headings, which run headline-length in
       production (owner-reported, 2026-08-09) — cap the chip at one line.
       Only the LABEL ellipsizes (its own span, min-width: 0 so flex lets
       it shrink); the count is the chip's actual information and must
       never be the part the ellipsis eats. The label's tail is always
       recoverable one scroll down in the TOC. */
    max-width: 100%; display: inline-flex; align-items: baseline;
  }
  .arcs .arc .arclabel {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;
  }
  .arcs .arc .arccount { font-weight: 700; margin-left: 0.45em; flex: none; }
  /* Interactivity signal lives on the LABEL only (an underline on hover),
     not on the chip's shape/color — the chip must not start looking like a
     button just because it became clickable. */
  .arcs .arc:hover .arclabel { text-decoration: underline; }
  .arcs .arc:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }

  /* "What changed" block (§11.3 delta persistence, ingest v4, renderDeltas):
     typography-led per the design guidance — no cards, a hairline between
     rows (same recipe as .now/.nowlist just below in the file), not a
     colored box. Each row is a link (like .now's .nowrow) to that delta's
     arc page. .deltatext/.deltaprev/.deltaarrow/.deltanow are shared with
     the arc page's own per-appearance line (renderArcAppearance) — same
     quiet register in both places, one set of rules for both. */
  .deltas { display: flex; flex-direction: column; margin: 0 0 1.6em; }
  .deltas .delta {
    display: flex; flex-direction: column; gap: 0.3em;
    text-decoration: none; color: inherit;
    padding: 0.6em 0; border-bottom: 1px solid var(--hairline);
  }
  .deltas .delta:last-child { border-bottom: none; }
  .deltalabel {
    font-family: var(--font-data); font-size: 0.72em; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--accent);
  }
  .deltas .delta:hover .deltalabel,
  .deltas .delta:focus-visible .deltalabel { text-decoration: underline; }
  .deltas .delta:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }
  .deltatext {
    margin: 0; font-family: var(--font-prose); font-size: 0.95em; line-height: 1.55;
  }
  .deltaprev { color: var(--muted); }
  .deltaarrow {
    font-family: var(--font-data); color: var(--muted); margin: 0 0.5em;
  }
  .deltanow { color: var(--text); }

  .attention {
    background: var(--attention-bg); color: var(--attention-text);
    padding: 0.8em 1em; border-radius: 8px; margin: 0 0 1.4em;
  }
  .attention h2 { margin: 0 0 0.3em; border: 0; padding: 0; font-size: 0.95em; }
  .attention p { margin: 0; font-size: 0.95em; font-family: var(--font-prose); }

  .tldr {
    background: var(--tldr-bg); color: var(--tldr-text);
    padding: 1em 1.2em; border-radius: 8px; margin: 0 0 2em; font-weight: 600;
    font-family: var(--font-prose); line-height: 1.65;
  }
  /* The emailer's callout markup carries small eyebrow label spans
     (tldr-label / attention-label) and a ⚠ collection-failed banner
     paragraph, all previously presented by INLINE email styles the site now
     strips at render time (see stripInlineStyles) — these rules are their
     site-side, theme-aware replacements. Eyebrows in the mono data voice,
     matching the wire dateline. */
  .tldr .tldr-label, .attention .attention-label {
    display: block; font-family: var(--font-data); font-size: 0.72em;
    font-weight: 700; text-transform: uppercase; letter-spacing: 0.12em;
    margin-bottom: 0.4em;
  }
  .tldr .tldr-label { color: var(--accent); }
  .digest .banner {
    background: var(--attention-bg); color: var(--attention-text);
    padding: 0.6em 1em; border-radius: 8px; margin: 0 0 1.2em;
    font-size: 0.92em;
  }
  /* Article h2s deliberately stay sans while the body below them goes serif
     (.digest p) — the newspaper pattern: sans heads announce, serif body
     reads. Not an omission. */
  .digest h2 {
    font-size: 1.15em; border-left: 3px solid var(--h2-border);
    padding-left: 0.6em; margin: 1.9em 0 0.6em; text-wrap: balance;
    /* So a TOC-jumped-to heading isn't flush against the viewport edge. */
    scroll-margin-top: 0.8em;
  }
  .digest p { margin: 0.7em 0; font-family: var(--font-prose); line-height: 1.65; }
  /* Reading polish (roadmap 4 step 2): hyphenate the serif prose blocks.
     html lang is already correct per page (en/hu, set by pageChrome) —
     the browser picks the right hyphenation dictionary on its own, this is
     just opting the prose in. Hungarian's long compounds are the motivating
     case on the 15px phone column, where an unbroken word can overflow a
     narrow line; -webkit- is what iOS Safari actually honors. Headings and
     chrome stay un-hyphenated on purpose — this is for reading paragraphs,
     not labels. */
  .digest p, .tldr, .entry .excerpt, .attention p {
    hyphens: auto; -webkit-hyphens: auto;
  }
  /* nh3 allows pre/code through (digest repo, emailer.py's _ALLOWED_TAGS),
     and pre's own white-space: pre is immune to the body's inherited
     overflow-wrap — a fenced code block in a digest was exactly what
     re-triggered the phone layout bug (see .wrap's comment). pre-wrap keeps
     code readable while letting long lines break at the container edge. */
  .digest pre { white-space: pre-wrap; overflow-wrap: break-word; }
  .cite {
    font-size: 0.7em; vertical-align: super; text-decoration: none;
    background: var(--chip-bg); color: var(--chip-text);
    padding: 0 0.4em; border-radius: 99px; font-weight: 700; margin-left: 1px;
    font-family: var(--font-data);
  }
  /* Search hit highlighting (roadmap 4 step 7, markSnippet): reuses the
     citation chip's own chip-bg/chip-text tokens rather than a new color —
     it's the same "this is metadata the site added, not article content"
     visual family as .cite. */
  mark { background: var(--chip-bg); color: var(--chip-text); border-radius: 3px; padding: 0 0.15em; }
  /* Touch provenance (roadmap 4 step 2): on the phone — where this site is
     mostly read — there's no hover, so the title attribute's domain never
     surfaces; put it on the pill itself instead. Reuses the exact title
     addCiteTitles already sets and the same attr(title) pattern the print
     stylesheet below uses, so the chip reads "1 example.com" instead of a
     bare number. Hover-capable devices are untouched and keep the bare chip
     plus the native hover title. .cite[title], not bare .cite, so a chip
     whose href failed to parse (no title) shows nothing extra — same
     fail-safe as print. The bigger pill this produces is also a bigger,
     easier-to-hit touch target. */
  @media (hover: none) {
    .cite[title]::after {
      content: attr(title);
      margin-left: 0.35em;
      font-weight: 400;
      letter-spacing: 0;
      /* The prose hyphenation above inherits into the chip and was
         auto-hyphenating the domain itself ("ex-ample1.com") — an inserted
         hyphen inside a hostname reads as part of the hostname, which
         misstates the provenance this feature exists to show. Long domains
         still wrap (overflow-wrap), just never with an added hyphen. */
      hyphens: none; -webkit-hyphens: none;
    }
  }
  .closing {
    font-style: italic; color: var(--muted); border-top: 1px solid var(--hairline);
    padding-top: 1em; margin-top: 2.2em; font-size: 0.92em;
  }

  /* Source key (roadmap 2 step 8 follow-up): the digest page's colophon —
     source_counts/failed_sources spelled out as concrete per-source numbers
     with color swatches, sitting right after the article (renderSourceKey
     renders nothing when both fields are absent — see the function for the
     fail-safe JSON.parse contract shared with renderDegradedBadge). The
     index page's own micro-bar counterpart (.spectrum, one <i> per source
     sized by inline flex:N) was removed in the owner-requested index-
     cleanup pass; this colophon is unaffected and still carries the full
     provenance on the digest page. */
  .sourcekey {
    font-family: var(--font-data); font-size: 0.75em; color: var(--muted);
    display: flex; flex-wrap: wrap; gap: 0.5em 1.1em; align-items: center;
    margin: 1.6em 0 0;
  }
  .sklabel { text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
  .sk { display: inline-flex; align-items: center; }
  .sk i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 0.45em; }
  /* .sk-failed gets no color override — muted stays muted, the ⚠ prefix
     baked into the string (not CSS) is what marks it, same "color is not
     the warning signal" decision as .flag-degraded on the index. */

  /* Floating back-to-index button (digest pages only): fixed bottom-right
     in one-thumb reach, clear of the iPhone home bar via safe-area insets.
     Hidden until the reader scrolls past the top nav (the inline script in
     pageChrome toggles .show), so it never duplicates the visible header
     nav; with JS disabled the <noscript> style keeps it always visible
     instead — the button must never be unreachable. Accent background with
     the page background as the arrow color works in both themes. */
  .backfab {
    position: fixed;
    right: max(1.1rem, env(safe-area-inset-right));
    bottom: calc(1.1rem + env(safe-area-inset-bottom));
    width: 48px; height: 48px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    background: var(--accent); color: var(--bg);
    text-decoration: none; font-size: 1.35em; font-weight: 700;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28);
    opacity: 0; pointer-events: none; transition: opacity 0.18s ease;
    z-index: 10;
  }
  .backfab.show { opacity: 1; pointer-events: auto; }
  .backfab:focus-visible { outline: 2px solid var(--text); outline-offset: 3px; opacity: 1; pointer-events: auto; }
  @media (prefers-reduced-motion: reduce) { .backfab { transition: none; } }

  /* View tabs: the primary content navigation. Bigger than the corner
     language toggle by design — switching between the full stream and
     daily briefs is the main choice a reader makes. Owner-requested
     2026-08-09: one connected segmented capsule instead of three detached
     pills. The capsule (.viewtabs) carries the border, radius and
     overflow: hidden; segments (.viewtab, unchanged below — same hrefs,
     active-state fill, data-view attributes the ⌘K palette reads) are
     borderless and share a hairline divider. Owner-requested masthead
     compaction: this used to be centered on its own row below the masthead
     (width: fit-content + auto side margins); it now rides inline in
     .mastleft beside the brand (see pageChrome/the header.mast CSS above),
     so there's no more row of its own to center on — width: fit-content
     stays (the capsule still hugs its own content rather than stretching),
     the centering margin is gone. The view selector remains the primary
     navigation. */
  .viewtabs {
    display: flex; width: fit-content;
    border: 1px solid var(--hairline); border-radius: 999px; overflow: hidden;
  }
  .viewtab {
    padding: 0.42em 1.6em;
    font-size: 0.95em; font-weight: 600; text-decoration: none;
    color: var(--accent);
  }
  .viewtab + .viewtab { border-left: 1px solid var(--hairline); }
  .viewtab.active {
    /* .viewtabs' overflow: hidden clips this fill to the capsule's own
       rounded corner whenever the active segment is first or last. */
    background: var(--accent); color: var(--bg);
  }
  /* Segments have no border of their own to shift on hover anymore, so hint
     hover with the same soft accent-tinted background the TL;DR block uses
     (picked over an --accent-strong color shift — quieter against the
     filled active segment sitting right next to it). */
  .viewtab:not(.active):hover { background: var(--tldr-bg); }
  /* Inset outline (negative offset): an outset ring would get clipped by
     the capsule's overflow: hidden. */
  .viewtab:focus-visible { outline: 2px solid var(--text); outline-offset: -2px; }

  /* Week rail (roadmap 3 step 2): mono wire-style ← older · WEEK N · range ·
     newer → nav, between the view tabs and the ledger, ALL-view index pages
     only (see renderIndexPage/renderWeekRail). Classic 3-column centering
     trick for the first three spans: rail-older/rail-newer share flex:1 (so
     they're always equal width regardless of their own content length, even
     when one side is an empty spacer), which keeps the center label
     visually centered without needing to measure anything.
     trick at all (flex: none, own rule below), it just claims its own
     natural width at the row's right edge; the gap property below gives it
     breathing room from rail-newer's "→" link on an archive week rather
     than the two abutting directly. */
  .weekrail {
    display: flex; align-items: baseline; gap: 0.6em; margin: 0 0 1.2em;
    font-family: var(--font-data); font-size: 0.78em;
    letter-spacing: 0.06em; text-transform: uppercase;
  }
  .weekrail .rail-older, .weekrail .rail-newer { flex: 1; }
  .weekrail .rail-older { text-align: left; }
  .weekrail .rail-newer { text-align: right; }
  .weekrail .rail-center { flex: 0 1 auto; color: var(--muted); }
  .weekrail a { color: var(--accent); text-decoration: none; }

  /* Archive sparkline (roadmap 4 step 6, renderWeekRail): the pulse strip's
     visual language, sized down to live inside the rail's center span —
     archive weeks only, see renderWeekRail. Zero-count days (.sd0) get their
     height from THIS rule rather than an inline style like the nonzero bars
     get, so the two never end up in a specificity fight over the same
     property. */
  .railspark { display: inline-flex; align-items: flex-end; gap: 2px; height: 14px; margin-left: 0.7em; vertical-align: -2px; }
  .railspark i { display: block; width: 4px; background: var(--chip-bg); border-radius: 1px 1px 0 0; }
  .railspark i.sd0 { background: var(--hairline); height: 15%; }

  /* Search bubble (owner-requested index cleanup): the week-rail's compact
     trigger for what used to be the always-visible filterrow — same
     details/summary disclosure pattern as the settings gear just above
     (details.settings/summary.gear/.settingspanel), reusing its exact
     border/elevation/open-animation recipe (the settingsopen keyframe,
     defined above, is referenced again below rather than copied) under NEW
     class names rather than the literal same ones: the settings-close IIFE
     and the soft-nav click interceptor (see pageChrome's bottom script)
     both assume exactly one details.settings element on the page, and this
     is a second, unrelated disclosure that must not collide with either
     lookup. */
  details.searchpop { position: relative; }
  /* Icon-only (owner-requested): sized off the icon itself, so the pill
     collapses to a round tap target instead of keeping the old text
     control's horizontal padding. line-height 0 keeps the SVG from
     inheriting a text box taller than itself. */
  summary.searchtoggle {
    list-style: none; display: inline-flex; align-items: center; justify-content: center;
    background: none; border: 1px solid var(--hairline); border-radius: 999px;
    color: var(--muted); line-height: 0;
    padding: 0.4em; cursor: pointer;
  }
  summary.searchtoggle:hover, details.searchpop[open] > summary.searchtoggle { color: var(--accent); }
  summary.searchtoggle::-webkit-details-marker { display: none; }
  summary.searchtoggle:hover { border-color: var(--accent); }
  summary.searchtoggle:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }
  details.searchpop[open] > summary.searchtoggle { border-color: var(--accent); }
  .searchpanel {
    position: absolute; right: 0; top: calc(100% + 0.5em);
    z-index: 20;
    background: var(--bg); border: 1px solid var(--hairline); border-radius: 14px;
    padding: 0.9em 1.1em;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28); /* same recipe as .settingspanel's */
    display: flex; flex-direction: column; gap: 0.7em; min-width: 16em;
  }
  @media (prefers-reduced-motion: no-preference) {
    /* Reuses .settingspanel's own open keyframe (see above) — same visual
       language, no need for a second identical @keyframes block. Opening
       needs no JS, same as the settings bubble: native <details> flips
       [open] the instant the summary is clicked, and this rule keys off
       that attribute directly. No matching close animation here (unlike
       settings' settingsclose/"panelclosing" dance) — this bubble has no
       language-switcher-style reason to stay open across a navigation, and
       the owner brief's only explicit behavioral ask was opening it with
       focus, not mirroring the settings bubble's close polish too. */
    details.searchpop[open] .searchpanel { animation: settingsopen 160ms ease-out; }
  }
  /* The SAME .filter input class the client-side filter IIFE and the
     archive-search integration already look for (document.querySelector of
     ".filter" — see pageChrome's bottom script), just styled for its new
     home inside the panel instead of a standalone row. */
  .searchpanel .filter {
    display: block; font: inherit; font-size: 0.9em;
    padding: 0.5em 0.9em; border-radius: 10px;
    border: 1px solid var(--hairline); background: var(--bg); color: var(--text);
  }
  .searchpanel .filter::placeholder { color: var(--muted); }
  .searchpanel .filter:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  /* Entry point into the standalone search page (roadmap 4 step 7) — the
     no-JS fallback, still the row's only visible content when the filter
     input above is hidden (see renderSearchBubble). Mono chrome voice, not
     styled like the data field beside it, same as before this move. */
  .searchlink {
    font-family: var(--font-data); font-size: 0.78em;
    text-decoration: none; letter-spacing: 0.06em; text-transform: uppercase;
  }

  /* Unified search results (owner UX pass): the index page's own box for
     archive hits fetched in the background by the bottom script — see
     renderIndexPage's archiveResultsHtml. .archivelabel is the file's mono
     eyebrow recipe, kept as its own class rather than shared with any other
     eyebrow — coupling unrelated features to one class just because they
     currently render alike is the kind of thing that bites later, and this
     class survived a neighbouring feature's removal untouched precisely
     because it was never shared. .archiveresults[hidden] needs no rule of its
     own: the global [hidden] override near the top of this stylesheet
     already covers it, same as every other hide-by-attribute element here. */
  .archivelabel {
    font-family: var(--font-data); font-size: 0.7em; text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--muted); margin-bottom: 0.6em;
  }
  .archiveresults { margin-top: 1.6em; }

  /* Catch-up banner (§11.2): one row above the NOW section, built entirely
     by the unread-fence IIFE extension in the bottom script — see
     renderIndexPage's catchupHtml for the hidden shell and why it always
     sits above nowHtml regardless of whether NOW itself has content that
     day. No card (design guidance), mono metadata register matching
     .nowmeta/.archivelabel, a bottom hairline as the only separator — same
     "typography, not chrome" recipe as .now just below. .catchuptext holds
     the composed sentence (may include real <a> links to followed arcs, see
     the script — that's why it's a plain span, not the row's own click
     target: a button/link cannot legally contain another link). The jump
     and dismiss controls are small icon-only buttons, deliberately NOT
     styled like .resumechip's pill — this row is metadata-weight chrome,
     not a floating call to action. */
  .catchup {
    display: flex; align-items: baseline; gap: 0.7em;
    margin: 0 0 1.4em; padding-bottom: 1.1em;
    border-bottom: 1px solid var(--hairline);
    font-family: var(--font-data); font-size: 0.78em; color: var(--muted);
  }
  .catchuptext { flex: 1 1 auto; min-width: 0; }
  .catchuptext a { color: var(--accent); text-decoration: none; }
  .catchuptext a:hover, .catchuptext a:focus-visible { text-decoration: underline; }
  .catchupjump, .catchupdismiss {
    flex: none; background: none; border: 0; padding: 0.1em 0.3em;
    color: var(--muted); font: inherit; font-size: 1em; line-height: 1;
    cursor: pointer;
  }
  .catchupjump:hover, .catchupdismiss:hover,
  .catchupjump:focus-visible, .catchupdismiss:focus-visible { color: var(--text); }
  .catchupjump:focus-visible, .catchupdismiss:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px;
  }

  /* NOW section (§11.1 PR B, renderNowSection): the situational-overview
     block at the very top of the current-week all-view index, above the
     rail/search row/ledger — see renderIndexPage. .archivelabel
     is reused for the eyebrow (already shared by the arc timeline and this
     file's own archive-search label above) rather than a fourth near-
     identical mono-eyebrow class. Typography-led per the design guidance:
     each arc is one row, not a card — a bottom hairline on the whole block
     is the only separator between it and the ledger, no box/border around
     the block itself. */
  .now { margin: 0 0 1.8em; padding-bottom: 1.3em; border-bottom: 1px solid var(--hairline); }
  /* Head row: the NOW eyebrow left, the search control right (owner-
     requested placement) — the icon lands on the same right edge the arc
     rows' relative-time meta below it aligns to. The eyebrow (.archivelabel)
     carries its own bottom margin, so the row's own baseline stays where a
     bare eyebrow used to sit; align-items center keeps the icon optically
     on the eyebrow's line rather than riding its cap height. */
  .nowlist { display: flex; flex-direction: column; }
  .nowrow {
    display: flex; align-items: baseline; gap: 0.7em;
    text-decoration: none; color: inherit;
    padding: 0.55em 0; border-bottom: 1px solid var(--hairline);
  }
  .nowlist .nowrow:last-child { border-bottom: none; }
  .nowrow:hover .nowarclabel,
  .nowrow:focus-visible .nowarclabel { color: var(--text); text-decoration: underline; }
  .nowrow:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; border-radius: 4px; }
  /* Momentum arrow: mono, muted — a data-derived signal (design guidance:
     "evidence over certainty"), not a colored/severity cue, so it carries no
     color of its own beyond the row's normal muted register. Fixed width so
     the label column stays aligned across rows regardless of which arrow (or
     none, on the defense-in-depth null case) a given row has. */
  .nowarrow {
    font-family: var(--font-data); font-size: 0.85em; color: var(--muted);
    flex: none; width: 1em; text-align: center;
  }
  .nowarclabel {
    font-family: var(--font-prose); font-size: 0.97em; color: var(--muted);
    flex: 1 1 auto; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .nowmeta {
    font-family: var(--font-data); font-size: 0.75em; color: var(--muted);
    flex: none; white-space: nowrap;
  }

  /* Search page (roadmap 4 step 7): the form itself reuses .filter's input
     styling (see above) — it's the SAME kind of control, just server-
     functional here instead of a client-side enhancement (see
     renderSearchPage). */
  .searchform { display: flex; gap: 0.5em; margin: 0 0 1.6em; }
  .searchform .filter { flex: 1; }
  /* Styled like .viewtab's outlined pill (own rule above), not a filled
     button — a search submit is a secondary action next to the input, not
     the page's primary call to action. */
  .searchbtn {
    border: 1px solid var(--hairline); border-radius: 999px; color: var(--accent);
    padding: 0.42em 1.2em; background: none; cursor: pointer; font: inherit; font-size: 0.9em;
  }
  .searchbtn:hover { border-color: var(--accent); }
  .searchbtn:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }

  /* Unread fence (roadmap 2 step 2): one labeled hairline the bottom script
     inserts between digests that arrived since the reader's last visit and
     everything older — no-JS readers never see this class at all, so no
     hidden-by-default dance is needed here (unlike .filter/.minisegbtn
     above, which exist in the markup from the start). */
  .unreadfence { display: flex; align-items: center; gap: 0.7em; margin: 1.4em 0; }
  .unreadfence .line { flex: 1 1 auto; height: 0; border-top: 1px solid var(--accent); }
  .unreadfence .label {
    flex: 0 0 auto; font-family: var(--font-data); font-size: 0.7em;
    text-transform: uppercase; letter-spacing: 0.08em; color: var(--accent);
  }
  /* (The filter IIFE hides this via the hidden attribute while a query is
     active — the global [hidden] rule near the top of this stylesheet is
     what makes that actually take effect over the display: flex above.) */

  /* Resume chip (roadmap 4 step 3): a floating "jump to the fence" button,
     built entirely by the unread-fence IIFE below and only when a fence was
     actually inserted — it inherits every one of that IIFE's guards for
     free (no-JS, archive week, nothing new, everything new). No display
     rule needed for hiding — the global [hidden] override near the top of
     this stylesheet already handles that, same as .unreadfence above. */
  .resumechip {
    position: fixed;
    left: 50%; transform: translateX(-50%);
    bottom: calc(1.1rem + env(safe-area-inset-bottom));
    font-family: var(--font-data); font-size: 0.72em;
    text-transform: uppercase; letter-spacing: 0.08em;
    background: var(--bg); color: var(--accent);
    border: 1px solid var(--accent); border-radius: 999px;
    padding: 0.45em 1.1em; cursor: pointer;
    /* One line, always: the body's inherited overflow-wrap breaks the
       label across two lines well before the pill nears the viewport
       edge, and a two-line floating pill reads as a banner, not a chip. */
    white-space: nowrap;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28);
    z-index: 10;
  }
  .resumechip:focus-visible { outline: 2px solid var(--text); outline-offset: 3px; }

  footer.site {
    margin-top: 3.5em; padding-top: 1.2em; border-top: 1px solid var(--hairline);
    color: var(--muted); font-size: 0.8em;
  }
  footer.site p { margin: 0.3em 0; }
  /* Next-briefing countdown (roadmap 2 step 3): just another footer line —
     size/color already inherited from footer.site — except set in the mono
     data family since it's a live-updating time value, same family as every
     other time-keyed piece of chrome on this site. */
  .countdown { font-family: var(--font-data); }

  /* Desktop: the content column becomes a rounded "bubble" card hugging the
     42em text measure, floating on a darker, purple-tinted page background.
     Mobile keeps the full-bleed layout above untouched — the card chrome
     only exists from 52em up. * { box-sizing: border-box } is set globally,
     so max-width 48em with 3em side padding keeps the text at the same
     42em measure it has on mobile. */
  /* Wide-viewport type scale (owner: "the resolution is too low" on a big
     display). The honest lever for a prose-first site is SIZE, not width:
     every dimension here is em-based off body, so stepping the base up
     scales the card, the padding, the chrome and the text together, uses
     more of a large screen, and leaves the character measure exactly where
     it was (~82). Widening the column instead would have pushed the
     measure past 100 characters, which is where reading actually degrades.
     Breakpoint ems resolve against the 16px root, so these are 1200px and
     1600px. The S/M/L preference in Settings still multiplies on top. */
  @media (min-width: 75em) { body { font-size: 18px; } }
  @media (min-width: 100em) { body { font-size: 19px; } }

  @media (min-width: 52em) {
    body { background: var(--page-bg); padding: 2.5em 1.5em; }
    /* Keep the root canvas in step with the desktop page background — see
       the html background rule above. */
    html { background: var(--page-bg); }
    .wrap {
      background: var(--bg);
      max-width: 48em;
      padding: 0.4em 3em 3.5em;
      border-radius: 28px;
      border: 1px solid var(--hairline);
    }
    /* NOTE — no prose max-width clamp here, deliberately. A pass on
       2026-08-10 tried widening this card to 56em while holding prose at
       41em, on the theory that chrome should use width prose shouldn't.
       Live, that read as a broken right edge rather than as hierarchy:
       the TL;DR box and every ledger excerpt stopped dead mid-card with
       nothing beside them (owner-reported, twice). Even at this 48em card
       a clamp left the smaller-font ledger excerpts ~74px short of the
       content box — same artifact, smaller. This site is mostly prose, so
       the width it can honestly fill is the width its text wants: content
       box and text edge are one and the same again. Screen real estate is
       bought with TYPE SIZE instead — see the wide-viewport font-size
       steps above, which scale the whole em-based layout together and
       leave the character measure where it is. */
    /* Anchor the floating back button to the BUBBLE, not the viewport:
       the card is 48em centered, so its right edge sits at 50% + 24em —
       park the button 1rem into the purple gutter beside its bottom
       corner. min() clamps back to the viewport edge on narrow desktop
       windows so the button can never be pushed off-screen. Mobile keeps
       the base viewport-corner placement (no gutter exists there). */
    .backfab {
      /* em would resolve against the fab's own 1.35em font — overshooting
         by ~150px (live-measured). rem resolves against the root: the card
         is 48em of the 17px body = 816px wide, half = 408px = 25.5rem at
         the 16px root default. */
      left: min(calc(50% + 25.5rem + 1rem), calc(100vw - 48px - 1.1rem));
      right: auto;
    }
  }

  /* Print (roadmap step 7 — polish): a printed digest page is a read-later/
     archival copy of a briefing, not a screenshot of the site chrome — strip
     everything that only makes sense on-screen and force plain black-on-
     white text regardless of the reader's light/dark theme. The serif prose
     already suits print as-is; day headers and the dateline stay mono
     (--font-data is untouched here, only color is forced). */
  @media print {
    body { background: #fff; }
    .wrap { max-width: none; padding: 0; border: 0; border-radius: 0; }
    .mast, .viewtabs, nav.digestnav, .backfab, .toc, footer.site,
    .searchpop, .miniseg, .densitytoggle, .resumechip,
    .archiveresults, .catchup, .followtoggle {
      display: none;
    }
    .digest, .digest p, .digest h2, .stamp, .dayhead, .empty, .en-only-note, .arctitle {
      color: #000;
    }
    /* "What changed" block (§11.3 delta persistence, ingest v4): CONTENT,
       not chrome — it's the same "communicate what changed" information the
       reader would otherwise have to reconstruct from the article prose, so
       it stays visible and gets the same forced-ink treatment as .stamp/
       .digest p above, not the .arcs/.toc/.catchup treatment (a navigation
       aid, safe to omit; a story-thread chip, whose chip-bg/chip-text colors
       are deliberately left un-forced elsewhere in this block since a chip
       is decoration a reader can live without on paper). Every child span
       (.deltaprev/.deltaarrow/.deltanow) gets its own explicit rule because
       each already carries its own on-screen color (var(--muted)/
       var(--text)) that would otherwise beat the ancestor's forced color. */
    .deltalabel, .deltatext, .deltaprev, .deltaarrow, .deltanow {
      color: #000;
    }
    /* Numbers are provenance and stay visible in print; the swatches print
       gray (acceptable) but the text itself forces to ink like every other
       digest-page text block above. */
    .sourcekey { color: #000; }
    /* TL;DR/attention stay boxes, but thin bordered outlines instead of
       tinted fills — a colored background wastes ink and won't reproduce
       reliably across printers anyway. */
    .tldr, .attention {
      background: none; border: 1px solid #999; color: #000;
    }
    .attention h2 { color: #000; }
    /* Citation chips print as plain superscript text, no pill styling. */
    .cite { background: none; color: #000; }
    /* Print the destination DOMAIN (via the title attr addCiteTitles adds),
       not the full URL — a full URL would bloat print lines; the domain is
       enough provenance to look something up later. .cite[title], not
       bare .cite, so a chip whose href failed to parse (no title) doesn't
       print an empty " ()". */
    .cite[title]::after {
      content: " (" attr(title) ")";
      font-size: 0.85em;
      color: #333;
    }
  }
`;

// `title` defaults to null, falling back to the bare host — that default IS
// the index page's title. Bare-hostname titles made every browser tab and
// history entry indistinguishable from each other (roadmap step 2); digest
// pages now pass a per-digest title instead (see renderDigestPage). Escaped
// here, once, same as the host fallback — callers pass the raw string.
// `countdownNewest` defaults to null, same contract as `title`: null means
// no countdown paragraph at all. Only handleIndexPage/renderIndexPage ever
// pass a value — digest pages never do (see renderDigestPage's call site;
// the reader is mid-read, a ticking countdown would just be noise there).
//
// `prefetchHref` defaults to null, same contract again (roadmap 2 step 7):
// only renderIndexPage ever passes a value, and only when the index is
// non-empty — the lead card's digest is the reader's most likely next tap,
// so hint the browser to fetch it early. Digest pages never pass this
// (deliberate restraint — prev/next COULD be prefetched too, but that's 2
// extra fetches per read × 8 reads/day for what the roadmap scoped as
// "lead only"; not worth it here). Two progressive, independent mechanisms
// render from the one href: a <link rel="prefetch"> in <head> (Firefox and
// other browsers without Speculation Rules support) and a
// <script type="speculationrules"> in <body> (Chrome/Edge, the modern,
// preferred hint). Both are best-effort: this site's pages are
// `Cache-Control: private, no-store` (see the file-header comment / trust
// model), and a browser is free to simply not prefetch a no-store response
// — that's the deal with speculative loading in general, not a bug here, so
// neither mechanism is load-bearing for anything. Privacy-wise this adds
// nothing new: the JSON embeds the same capability-token URL that's already
// sitting in the lead card's own href on the same page (same-document
// exposure), and the speculation rules processor doesn't send that URL
// anywhere the visible link wouldn't already send it on a click.
function pageChrome(host, token, lang, view, switchersHtml, bodyHtml, title = null, countdownNewest = null, prefetchHref = null) {
  const viewTabsHtml = renderViewTabs(token, lang, view);
  const { first, rest } = brandParts(host);
  const strings = STRINGS[lang];
  // Next-briefing countdown (roadmap 2 step 3): strings/unit letters are
  // baked in server-side as data-* attributes so the bottom script stays
  // language-agnostic — same pattern as the unread-fence's data-unread-label
  // above. `hidden` by default; the bottom script computes and un-hides it
  // (progressive enhancement — no JS, no countdown text, same contract as
  // the filter input and theme toggle).
  const countdownHtml = countdownNewest
    ? `<p class="countdown" data-newest="${esc(countdownNewest)}" data-tmpl-next="${esc(strings.countdownNext)}" data-tmpl-due="${esc(strings.countdownDue)}" data-unit-hour="${esc(strings.countdownHourUnit)}" data-unit-minute="${esc(strings.countdownMinuteUnit)}" hidden></p>`
    : "";
  // Prefetch hints (roadmap 2 step 7) — see the param comment above for the
  // full rationale. prefetchHref is already a same-origin, server-built path
  // (digestHref: encodeURIComponent'd token + digit id), but esc()/
  // JSON.stringify are applied anyway, same "free safety, not redundant
  // trust" posture as addCiteTitles elsewhere in this file.
  const prefetchLinkHtml = prefetchHref
    ? `<link rel="prefetch" href="${esc(prefetchHref)}">`
    : "";
  const prefetchScriptHtml = prefetchHref
    ? `<script type="speculationrules">${JSON.stringify({ prefetch: [{ urls: [prefetchHref] }] })}</script>`
    : "";
  // ⌘K command palette strings (§11.1 PR C): the same data-* carrier
  // pattern countdownHtml/the unread fence/archiveResultsHtml already use
  // above and elsewhere in this file — a hidden element whose attributes
  // the client script reads, so the palette IIFE in the bottom <script>
  // stays lang-agnostic. Lives INSIDE .wrap (unlike the palette's own
  // dialog markup, created once by that IIFE and left outside .wrap — see
  // its comment) specifically so a soft-nav LANGUAGE hop refreshes these
  // strings along with everything else the swap replaces; the palette
  // re-reads them from here on every open, never caching them at creation
  // time, so a stale EN string can never survive into a HU page.
  const paletteConfigHtml = `<span class="paletteconfig" hidden
    data-label="${esc(strings.paletteLabel)}"
    data-placeholder="${esc(strings.palettePlaceholder)}"
    data-empty="${esc(strings.emptyFiltered)}"
    data-cmd-top="${esc(strings.paletteCmdTop)}"
    data-cmd-all="${esc(strings.paletteCmdAll)}"
    data-cmd-daily="${esc(strings.paletteCmdDaily)}"
    data-cmd-weekly="${esc(strings.paletteCmdWeekly)}"
    data-cmd-search="${esc(strings.searchButton)}"
    data-cmd-archive="${esc(strings.archiveLabel)}"
    data-cmd-switchlang="${esc(strings.paletteCmdSwitchLang)}"
    data-cmd-latest="${esc(strings.paletteCmdLatest)}"
  ></span>`;
  return `<!doctype html>
<html lang="${lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- theme-color must track the CSS palette blocks' --bg values above (light
     #fbfaf7 / dark #17181c) so mobile browser chrome (URL bar/status bar
     tint) melts into the page instead of showing a stock color. The
     prefers-color-scheme media attrs cover the automatic (Auto) case; a
     manual Light/Dark override from the theme miniseg (see the bottom
     script) updates both metas' content directly, since a media-query meta
     can't react to a data-theme attribute switch on its own — and clicking
     back to Auto restores each meta to its own media-appropriate value. -->
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#fbfaf7">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#17181c">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
${prefetchLinkHtml}
<title>${esc(title ?? host)}</title>
<script>try{document.documentElement.dataset.theme=localStorage.getItem("theme")||"";document.documentElement.dataset.fontsize=localStorage.getItem("fontsize")||"";document.documentElement.dataset.density=localStorage.getItem("density")||""}catch(e){}</script>
<style>${CSS}</style>
</head>
<body>
${prefetchScriptHtml}
<div class="wrap">
  <header class="mast">
    <div class="mastleft">
      <a class="brand" href="${indexHref(token, lang, view)}">${esc(first)}<span class="tld">${esc(rest)}</span></a>
    </div>
    ${viewTabsHtml}
    <div class="mastright">
      ${switchersHtml}
    </div>
  </header>
  ${paletteConfigHtml}
  ${bodyHtml}
  <footer class="site">
    <p>${esc(strings.footerPrivate)}</p>
    <p>${esc(strings.footerNotIndexed)}</p>
    ${countdownHtml}
  </footer>
</div>
<noscript><style>.backfab { opacity: 1; pointer-events: auto; }</style></noscript>
<script>
  // Animated preference swap (owner-requested), shared by the theme and
  // text-size minisegs below: run a page-state mutation inside a
  // same-document View Transition so the whole page crossfades to the new
  // state instead of snapping — the same-document sibling of the
  // at-view-transition navigation crossfade this site already ships, and
  // the same restraint applies: the browser's default crossfade, no custom
  // choreography. Unsupported browsers and reduced-motion readers get the
  // instant switch they always had — the mutation itself runs either way,
  // so correctness never depends on the animation.
  function withPageTransition(mutate) {
    // The animation must never poison the mutation (owner-reported via the
    // soft-nav rollout: startViewTransition rejects with InvalidStateError
    // in hidden/render-suppressed documents, and that rejection cascaded
    // into the soft-nav promise chain, whose catch-all dutifully fell back
    // to a HARD navigation — the exact reload the soft path exists to
    // avoid). Three layers: skip the API outright when the document is
    // hidden (a snapshot of an invisible page is meaningless), try/catch
    // the call so a synchronous throw degrades to an instant mutate, and
    // swallow the transition's own promise rejections (they are cosmetic —
    // per spec the update callback still runs even when the visual
    // transition is skipped).
    if (
      document.startViewTransition &&
      !document.hidden &&
      !matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      try {
        var t = document.startViewTransition(mutate);
        if (t && t.finished && t.finished.catch) t.finished.catch(function () {});
        if (t && t.ready && t.ready.catch) t.ready.catch(function () {});
        return;
      } catch (e) {
        // fall through to the plain mutate below
      }
    }
    mutate();
  }

  // j/k list-selection navigation (§11.1 PR C): the primary-link-list half
  // of the "Keyboard navigation" IIFE inside wirePage() below — pulled out
  // as its own top-level function (rather than nested inside that IIFE)
  // since it needs no closure state of its own and stays identical across
  // every wirePage() pass; defining it once here, not per pass, costs
  // nothing and avoids re-creating an identical closure on every soft-nav.
  // Queries .wrap fresh on every call — never caches the list — so it
  // always reflects whatever page (and whatever the client-side filter has
  // hidden — see the !el.hidden check) is currently showing. Selection is
  // real DOM focus, not a separate highlight: the existing :focus-visible
  // rule already lights up whichever anchor gets focused, so this needs no
  // CSS of its own. No wrap-around (§11.1 spec: "stop at ends") — stepping
  // past either end of the list is simply a no-op.
  function moveNavSelection(delta) {
    var list = Array.prototype.slice
      .call(document.querySelectorAll(".wrap .nowrow, .wrap .entry"))
      .filter(function (el) {
        return !el.hidden;
      });
    if (list.length === 0) return;
    var idx = list.indexOf(document.activeElement);
    var next;
    if (idx === -1) {
      next = delta > 0 ? 0 : list.length - 1;
    } else {
      next = idx + delta;
      if (next < 0 || next >= list.length) return; // no wrap-around — stop at the end
    }
    list[next].focus();
  }

  // Soft-navigation re-wiring: every feature below used to be a standalone,
  // parse-time IIFE that ran once. A soft nav (see the module at the bottom
  // of this script) swaps .wrap's content in place without a fresh document
  // load, so all of it has to be re-runnable — wirePage() bundles every
  // feature into one function, called once on initial load and again after
  // each soft-nav swap.
  //
  // Listener lifecycle: one AbortController per wirePage() pass. Aborting
  // the previous pass's controller before making a fresh one drops every
  // listener that pass attached, in a single stroke — no duplicate-listener
  // buildup across swaps. Every addEventListener below carries this pass's
  // { signal: signal } for exactly that reason.
  //
  // Non-listener state (a running setInterval, a DOM node parked outside
  // .wrap) doesn't go away just because its listeners did, so it gets its
  // own explicit cleanup: teardown collects one closure per such case, and
  // wirePage runs and clears the whole list before rewiring.
  var wireController = null;
  var teardown = [];

  function wirePage() {
    if (wireController) wireController.abort();
    wireController = new AbortController();
    var signal = wireController.signal;

    teardown.forEach(function (fn) {
      fn();
    });
    teardown.length = 0;

    // Show the floating back button only after the header nav has scrolled
    // away. Passive listener; runs once immediately so a mid-page reload
    // (browser scroll restoration) starts in the right state.
    (function () {
      var fab = document.querySelector(".backfab");
      if (!fab) return;
      var onScroll = function () {
        fab.classList.toggle("show", window.scrollY > 320);
      };
      addEventListener("scroll", onScroll, { passive: true, signal: signal });
      onScroll();
    })();

  // Theme miniseg (owner upgrade: Light/Auto/Dark, replacing the old
  // two-state ◐ toggle). The head script already applied any stored
  // override to <html data-theme> before first paint, so this only wires
  // the three buttons: unhide them (progressive enhancement — no JS, no
  // buttons), reflect the active segment (stored theme, or "auto" when
  // nothing is stored), and on click either set an override or, for Auto,
  // clear it — the override is GONE, not stored-as-"auto" (the head
  // script's || "" already renders absence as auto, same contract density
  // and now size use).
  (function () {
    var group = document.querySelector(".miniseg-theme");
    if (!group) return;
    var buttons = group.querySelectorAll(".minisegbtn");
    for (var i = 0; i < buttons.length; i++) buttons[i].hidden = false;
    // theme-color meta values (roadmap 2 step 3): duplicated from the CSS
    // palette's --bg light/dark values above — the third-copy problem again
    // (the palette already lives 3x in CSS for the no-build-step manual
    // override), but it changes rarely and there's no build step here to
    // share one source between CSS and JS.
    var THEME_COLORS = { light: "#fbfaf7", dark: "#17181c" };
    // Light/Dark collapse both metas to the SAME value — media queries stop
    // mattering once both metas say the same thing, same as the old
    // two-state toggle did. Auto is the fix that toggle never had: it
    // restores each meta to its OWN media-appropriate color (iterate the
    // metas; a meta whose media attr mentions "light" gets the light color,
    // otherwise the dark one) instead of leaving both stuck on whichever
    // value the last manual click set. The old design collapsed both metas
    // to one value and had no way back to "let the OS decide" — this is
    // exactly what the Auto segment fixes.
    var syncThemeColorMetas = function (theme) {
      document.querySelectorAll('meta[name="theme-color"]').forEach(function (m) {
        var color =
          theme === "auto"
            ? m.media.indexOf("light") !== -1
              ? THEME_COLORS.light
              : THEME_COLORS.dark
            : THEME_COLORS[theme];
        m.setAttribute("content", color);
      });
    };
    var reflect = function () {
      var active = document.documentElement.dataset.theme || "auto";
      for (var i = 0; i < buttons.length; i++) {
        var isActive = buttons[i].dataset.set === active;
        buttons[i].classList.toggle("active", isActive);
        buttons[i].setAttribute("aria-pressed", String(isActive));
      }
    };
    reflect();
    // On load, if a stored override is already in effect (the head script
    // set data-theme from localStorage before first paint), sync the metas
    // to match too. A momentary wrong chrome tint before this script runs is
    // an acceptable tradeoff — there's no way to read localStorage and touch
    // the DOM from the head script's synchronous one-liner and still keep
    // this logic in one place. Nothing to do here for the auto case — the
    // static per-meta content values above already ARE the auto values.
    var stored = document.documentElement.dataset.theme;
    if (stored === "dark" || stored === "light") syncThemeColorMetas(stored);
    for (var j = 0; j < buttons.length; j++) {
      buttons[j].addEventListener("click", function () {
        var v = this.dataset.set;
        // Crossfade the palette swap — see withPageTransition at the top
        // of this script.
        withPageTransition(function () {
        if (v === "auto") {
          document.documentElement.dataset.theme = "";
          try {
            localStorage.removeItem("theme");
          } catch (e) {}
        } else {
          document.documentElement.dataset.theme = v;
          try {
            localStorage.setItem("theme", v);
          } catch (e) {}
        }
        syncThemeColorMetas(v);
        reflect();
        });
      }, { signal: signal });
    }
  })();

  // Text size miniseg (owner upgrade): S/M/L, same shape as the theme
  // miniseg just above — unhide the buttons, reflect the active segment
  // (stored size, or "m" when nothing is stored), and on click either set
  // an override or, for M, clear it. M is the default expressed as the
  // ABSENCE of data-fontsize (see the CSS), so only s/l ever touch storage.
  (function () {
    var group = document.querySelector(".miniseg-size");
    if (!group) return;
    var buttons = group.querySelectorAll(".minisegbtn");
    for (var i = 0; i < buttons.length; i++) buttons[i].hidden = false;
    var reflect = function () {
      var stored = document.documentElement.dataset.fontsize;
      var active = stored === "s" || stored === "l" ? stored : "m";
      for (var i = 0; i < buttons.length; i++) {
        var isActive = buttons[i].dataset.set === active;
        buttons[i].classList.toggle("active", isActive);
        buttons[i].setAttribute("aria-pressed", String(isActive));
      }
    };
    reflect();
    for (var j = 0; j < buttons.length; j++) {
      buttons[j].addEventListener("click", function () {
        var v = this.dataset.set;
        // Crossfade the reflow — see withPageTransition at the top of this
        // script. A size change reflows the prose, and the crossfade turns
        // that layout jump into a dissolve, same as the theme swap.
        withPageTransition(function () {
          document.documentElement.dataset.fontsize = v === "m" ? "" : v;
          try {
            if (v === "m") localStorage.removeItem("fontsize");
            else localStorage.setItem("fontsize", v);
          } catch (e) {}
          reflect();
        });
      }, { signal: signal });
    }
  })();

  // Ledger density toggle (roadmap 4 step 4). Same shape as the theme/size
  // minisegs just above: the head script already applied any stored
  // preference to <html data-density> before first paint, so this only
  // wires the button — unhide it, reflect the current state in
  // aria-pressed, and on click flip compact<->comfortable and persist it.
  // Unlike theme there's no OS-preference fallback to resolve — comfortable
  // (empty string) IS the default, so effective state is just the dataset.
  (function () {
    var btn = document.querySelector(".densitytoggle");
    if (!btn) return;
    btn.hidden = false;
    var reflect = function () {
      btn.setAttribute("aria-pressed", String(document.documentElement.dataset.density === "compact"));
    };
    reflect();
    btn.addEventListener("click", function () {
      var next = document.documentElement.dataset.density === "compact" ? "" : "compact";
      // Crossfade the ledger reflow — same withPageTransition treatment the
      // theme and size minisegs get (owner-reported: density was the one
      // preference left snapping; it predates the helper).
      withPageTransition(function () {
        document.documentElement.dataset.density = next;
        try {
          localStorage.setItem("density", next);
        } catch (e) {}
        reflect();
      });
    }, { signal: signal });
  })();

  // Settings bubble close polish (owner redesign). Opening needs no JS at
  // all — details/summary opens natively, and the CSS above animates that
  // open state directly off the [open] attribute. Closing is different:
  // <details> snaps shut the instant open is set false, with no hook to
  // intercept, so this IIFE's close() gives the mirrored close animation
  // (see the CSS above) somewhere to run before the panel actually leaves —
  // then wires outside-click, Escape, AND the gear's own click (so every
  // path that can close the bubble animates it the same way) through that
  // one helper.
  (function () {
    var settings = document.querySelector("details.settings");
    if (!settings) return;
    var panel = settings.querySelector(".settingspanel");
    var summary = settings.querySelector("summary.gear");
    var closing = false;
    var close = function () {
      if (!settings.open || closing) return;
      closing = true;
      // "panelclosing", not the shorter "closing" — .closing already names
      // the digest article's closing-line style elsewhere on this page,
      // and reusing it here would leak that border/italic/spacing onto the
      // settings panel for the animation's duration (see the CSS above).
      panel.classList.add("panelclosing");
      // setTimeout, not transitionend/animationend: an end event can simply
      // never fire (interrupted mid-animation, reduced motion turning the
      // animation into a no-op, a stray browser quirk) and would strand the
      // panel open with the "panelclosing" class stuck on it forever. A
      // plain timer always fires. Under reduced motion the "panelclosing"
      // class's animation is a no-op (it lives inside the
      // prefers-reduced-motion: no-preference gate above), but this
      // timeout still runs its full 120ms before the panel closes — an
      // imperceptible, acceptable delay rather than a second, motion-aware
      // code path.
      setTimeout(function () {
        panel.classList.remove("panelclosing");
        settings.open = false;
        closing = false;
      }, 120);
    };
    // Dismiss-tap swallowing (owner-reported): with the bubble open, a tap
    // outside used to close it AND activate whatever sat under the finger —
    // closing the panel by tapping an article link opened the article. An
    // open bubble behaves like a modal with an invisible scrim now: the
    // first outside tap only dismisses. Capture phase + preventDefault +
    // stopPropagation is what actually swallows the click before the
    // underlying link/button ever sees it — a bubble-phase listener would
    // run after the link's default navigation was already committed.
    document.addEventListener(
      "click",
      function (e) {
        if (settings.open && !closing && !settings.contains(e.target)) {
          e.preventDefault();
          e.stopPropagation();
          close();
        }
      },
      { capture: true, signal: signal },
    );
    // Keep the bubble open across a language hop (owner-reported: switching
    // language closed the menu — it's a full navigation, so the fresh
    // document rendered with the details in its default closed state).
    // sessionStorage, not localStorage: "the settings were open" is
    // navigation state, not a preference — it must not resurrect the panel
    // tomorrow. Set on language-link click inside the panel, consumed
    // (removed) on the very next load. No animation on the restore —
    // the panel was never closed from the reader's point of view.
    try {
      if (sessionStorage.getItem("settingsOpen")) {
        sessionStorage.removeItem("settingsOpen");
        settings.open = true;
      }
    } catch (e) {}
    panel.addEventListener("click", function (e) {
      var link = e.target.closest ? e.target.closest(".langswitch a") : null;
      if (link) {
        try {
          sessionStorage.setItem("settingsOpen", "1");
        } catch (err) {}
      }
    }, { signal: signal });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && settings.open) {
        close();
        summary.focus();
      }
    }, { signal: signal });
    // Without this, clicking the gear while open would let <details> close
    // itself natively and instantly, skipping the animation entirely — the
    // native toggle already handles OPENING fine (nothing to intercept
    // there), so this only ever preventDefaults the closing half of the
    // click. No-JS readers keep the plain native open/close (progressive
    // enhancement) — this listener simply never attaches for them.
    summary.addEventListener("click", function (e) {
      if (settings.open) {
        e.preventDefault();
        close();
      }
    }, { signal: signal });
  })();

  // Search bubble open-focus (owner-requested index cleanup, index pages
  // only — guarded on details.searchpop existing, since digest/arc pages
  // never render it). Opening itself needs no JS at all — same native
  // <details>/<summary> toggle the settings bubble above relies on — this
  // IIFE only adds the one explicit behavioral ask the brief called out:
  // move focus into the filter input the moment the popover opens, so a
  // pointer click on the trigger lands the reader ready to type without a
  // second, separate focus step. The native "toggle" event fires for BOTH
  // opening and closing (unlike click, which only fires on the summary
  // itself), so this checks details.open rather than assuming direction.
  // No outside-click/Escape handling here (unlike the settings bubble's own
  // close() dance above) — that polish was owner-requested specifically for
  // settings; native <details> behavior (click the summary again, or
  // anywhere outside via no special handling at all) is left as-is for this
  // one, matching the brief's only explicit ask for this control.
  (function () {
    var pop = document.querySelector("details.searchpop");
    if (!pop) return;
    var input = pop.querySelector(".filter");
    if (!input) return;
    pop.addEventListener("toggle", function () {
      if (pop.open) input.focus();
    }, { signal: signal });
  })();

  // Unread fence (roadmap 2 step 2, index pages only — guarded on an
  // .entry[data-created] existing, since digest pages carry neither). A
  // localStorage last-visit timestamp turns the index into an inbox: one
  // labeled hairline between digests that arrived since the reader was last
  // here and everything older. No JS = no fence (progressive enhancement —
  // the class is never in the server-rendered markup).
  (function () {
    var entries = Array.prototype.slice.call(document.querySelectorAll(".entry[data-created]"));
    if (entries.length === 0) return;
    var section = document.querySelector("section[data-unread-label]");
    if (!section) return;
    // Archive-week bail (roadmap 3 step 3): data-week-archive marks the
    // <section> on any non-current week (see renderIndexPage). An archive
    // page's "newest" entry is old news by definition, so there's nothing
    // to fence AND nothing here should ever be treated as the reader's most
    // recent visit — bailing before the lastVisit read/write below is what
    // stops a stray archive-page visit from regressing the stamp and
    // spawning a bogus fence around content the reader has long since seen.
    // The forward-only guard on the write itself (below) is the second,
    // independent layer of the same protection.
    if (section.hasAttribute("data-week-archive")) return;

    var lastVisit = null;
    try {
      lastVisit = localStorage.getItem("lastVisit");
    } catch (e) {}

    // entries are in document order = newest first (lead card first, see the
    // renderIndexPage/renderLeadCard comments), so entries[0] is the newest
    // digest in this view overall.
    var newest = entries[0].getAttribute("data-created");

    if (lastVisit) {
      // Find the LAST entry (in document order) newer than lastVisit — i.e.
      // the last "new" one before the "old" run begins. created_at is an
      // ISO UTC string, so > is a correct lexicographic comparison.
      var lastNewIndex = -1;
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].getAttribute("data-created") > lastVisit) lastNewIndex = i;
      }
      // Draw the fence only for a genuine mix: at least one new entry AND at
      // least one old entry after it. lastNewIndex === -1 means nothing is
      // new (skip); lastNewIndex === entries.length - 1 means EVERYTHING is
      // new — typically a first-ever visit — and a fence above zero old
      // entries would just be noise, so skip that too.
      if (lastNewIndex >= 0 && lastNewIndex < entries.length - 1) {
        var fence = document.createElement("div");
        fence.className = "unreadfence";
        fence.setAttribute("role", "separator");
        var lineBefore = document.createElement("span");
        lineBefore.className = "line";
        var label = document.createElement("span");
        label.className = "label";
        label.textContent = section.getAttribute("data-unread-label");
        var lineAfter = document.createElement("span");
        lineAfter.className = "line";
        fence.appendChild(lineBefore);
        fence.appendChild(label);
        fence.appendChild(lineAfter);

        // Insertion point is strictly "after the last new .entry element" —
        // if that entry's next sibling happens to be a .dayhead, the fence
        // lands above the day header, which reads naturally.
        var lastNewEntry = entries[lastNewIndex];
        lastNewEntry.parentNode.insertBefore(fence, lastNewEntry.nextSibling);

        // Resume chip (roadmap 4 step 3): the fence above is passive — a
        // reader landing at the top of a long index has no way to know it
        // exists further down. Built only here, alongside the fence itself,
        // so every guard that got us this far (no-JS, archive week, nothing
        // new, everything new) already applies to it too. Shown only while
        // the fence is below the viewport; tapping it scrolls the fence
        // into view and hides the chip again.
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "resumechip";
        chip.textContent = "↓ " + label.textContent;
        document.body.appendChild(chip);
        // The chip lives on document.body, OUTSIDE .wrap — a soft-nav swap
        // replaces .wrap's content but never touches this node, so it must
        // be torn down explicitly before the next wirePage() pass runs.
        teardown.push(function () {
          chip.remove();
        });
        var updateChip = function () {
          // Visible ONLY while the fence sits below the viewport's bottom
          // edge — that's the state where the reader can't know it exists.
          // In view, scrolled past, or filter-hidden: no chip.
          chip.hidden = fence.hidden || fence.getBoundingClientRect().top <= window.innerHeight;
        };
        addEventListener("scroll", updateChip, { passive: true, signal: signal });
        updateChip();
        chip.addEventListener("click", function () {
          fence.scrollIntoView({ block: "center" });
          // The reader just navigated to the fence — hide the chip so it
          // doesn't overlap what they scrolled to. The scroll listener
          // above keeps it hidden for as long as the fence stays in view.
          chip.hidden = true;
        }, { signal: signal });
      }

      // Catch-up banner (§11.2): extends this SAME IIFE rather than adding a
      // second "where was I" store — see PLAN.md §11.2's reconciliation
      // note. Built from the exact lastVisit/entries the fence above
      // just used, still BEFORE the trailing localStorage.setItem further
      // down advances the stamp — so this always reports what changed since
      // the visit that's ENDING now, never the one this load is about to
      // become (same "advance after computing, never before" ordering the
      // fence itself already relies on). Gated on the hidden .catchup shell
      // existing at all — renderIndexPage only emits it on the current-week
      // all-view index (showCatchup, same gate as the NOW section), so this
      // is a no-op everywhere else without a second gate here.
      var catchup = document.querySelector(".catchup");
      if (catchup) {
        // N: every .entry newer than lastVisit. Deliberately NOT reusing
        // lastNewIndex from the fence above — that one requires a genuine
        // mix (at least one old entry too) before it's non- -1, but a
        // fence-less "everything since lastVisit is new" page (e.g. a long
        // gap between visits) is still a legitimate, non-filler catch-up to
        // report, so N is computed independently here.
        var newEntries = entries.filter(function (el) {
          return el.getAttribute("data-created") > lastVisit;
        });
        var n = newEntries.length;

        // M: NOW's .nowrow arcs whose last update is newer than lastVisit —
        // see renderNowSection's data-last-seen (§11.2). Empty (0 rows,
        // ->[]) on a 0-eligible-arcs day, same fail-safe contract every
        // other NOW-derived feature in this file already uses.
        var nowRows = Array.prototype.slice.call(document.querySelectorAll(".nowrow[data-last-seen]"));
        var updatedArcs = nowRows.filter(function (row) {
          return row.getAttribute("data-last-seen") > lastVisit;
        });
        var m = updatedArcs.length;

        // "at least one last-visit value exists" is already guaranteed by
        // this whole block living inside the enclosing if (lastVisit) above;
        // the remaining "N+M > 0" half of the spec's show-condition is this
        // check — together they're exactly "first-ever visit: no banner, no
        // filler; nothing changed: no banner either".
        if (n + m > 0) {
          var textEl = catchup.querySelector(".catchuptext");
          var parts = [];
          // Singular template on exactly one, plural otherwise — "1
          // briefings" was the owner-reported bug. Both forms ride as data
          // attributes so this stays language-agnostic (Hungarian supplies
          // identical values, see the STRINGS comment there).
          if (n > 0) {
            var briefTmpl = catchup.getAttribute(
              n === 1 ? "data-tmpl-briefings-one" : "data-tmpl-briefings",
            );
            parts.push(briefTmpl.replace("{n}", String(n)));
          }
          if (m > 0) {
            // Follow list (§11.2, optional feature): followed arcs among the
            // updated ones get named (up to 3, linked) before the bare
            // count — see the follow-toggle IIFE below for where slugs get
            // written to localStorage. Nothing followed (the default —
            // "automatic-first" guardrail) falls straight through to the
            // bare "{m} arc updates" template below, exactly as if this
            // optional feature didn't exist.
            var followed = [];
            try {
              followed = JSON.parse(localStorage.getItem("followedArcs") || "[]");
            } catch (e) {}
            var followedUpdated = followed.length
              ? updatedArcs.filter(function (row) {
                  return followed.indexOf(row.getAttribute("data-arc-slug")) !== -1;
                })
              : [];
            if (followedUpdated.length > 0) {
              var named = followedUpdated.slice(0, 3);
              var rest = m - named.length;
              // Built via DOM nodes, not innerHTML — arc labels are
              // LLM-derived text, never trusted as markup, same discipline
              // the fence/resume chip above already follow.
              var arcFrag = document.createDocumentFragment();
              named.forEach(function (row, idx) {
                if (idx > 0) arcFrag.appendChild(document.createTextNode(", "));
                var a = document.createElement("a");
                a.href = row.getAttribute("href");
                a.textContent = row.querySelector(".nowarclabel").textContent;
                arcFrag.appendChild(a);
              });
              if (rest > 0) {
                arcFrag.appendChild(
                  document.createTextNode(" " + catchup.getAttribute("data-tmpl-more").replace("{n}", String(rest))),
                );
              }
              parts.push(arcFrag);
            } else {
              parts.push(
                catchup
                  .getAttribute(m === 1 ? "data-tmpl-arcs-one" : "data-tmpl-arcs")
                  .replace("{m}", String(m)),
              );
            }
          }

          // Compose: prefix, then each part (plain string or a link-bearing
          // fragment) joined by ", " — same manual-join-over-locale-join
          // pragmatism as renderNowSection's own join(" · ") meta line.
          textEl.appendChild(document.createTextNode(catchup.getAttribute("data-prefix") + " "));
          parts.forEach(function (part, idx) {
            if (idx > 0) textEl.appendChild(document.createTextNode(", "));
            if (typeof part === "string") textEl.appendChild(document.createTextNode(part));
            else textEl.appendChild(part);
          });

          // Jump control: only meaningful when the fence above actually got
          // built (the "genuine mix" case, fence assigned in the block
          // above) — an "everything new" or "nothing old left" page has no
          // boundary to jump to, so the control just stays hidden rather
          // than jumping nowhere. fence is var-hoisted to this IIFE's
          // top, so referencing it here is safe whether or not that block
          // ran; unassigned reads back as undefined, which is falsy.
          if (fence) {
            var jumpBtn = catchup.querySelector(".catchupjump");
            jumpBtn.hidden = false;
            jumpBtn.addEventListener("click", function () {
              fence.scrollIntoView({ block: "center" });
            }, { signal: signal });
          }

          // Dismiss: removes the banner for THIS page-view only — no
          // localStorage write, no second "seen" concept layered on top of
          // lastVisit. The natural reset is simply the next visit, once the
          // trailing advance below has moved lastVisit forward and N/M
          // recompute from a later stamp.
          catchup.querySelector(".catchupdismiss").addEventListener("click", function () {
            catchup.hidden = true;
          }, { signal: signal });

          catchup.hidden = false;
        }
      }
    }

    // Update AFTER computing the fence above, and to the NEWEST entry's own
    // data-created — not "now" — so clock skew between the reader's device
    // and the server's stamped created_at can never make a digest look
    // newer or older than it is on the next visit. Forward-only (roadmap 3
    // step 3): only ever advance the stamp, never regress it — the
    // archive-week bail above is the primary guard (it keeps this line from
    // running at all on an archive page), this comparison is the second,
    // independent layer in case that ever changes.
    try {
      if (!lastVisit || newest > lastVisit) {
        localStorage.setItem("lastVisit", newest);
      }
    } catch (e) {}
  })();

  // Follow list (§11.2, optional feature, arc pages only — guarded on
  // .archead's data-arc-slug existing, since index/digest pages have no
  // .archead). A localStorage array of followed slugs; the catch-up
  // banner's unread-fence IIFE above cross-references it when computing M's
  // named-arcs list. Zero server involvement — the arc page itself doesn't
  // know or care whether it's followed. No teardown needed: the button gets
  // appended inside .archead, which lives in the swapped .wrap content, so
  // a soft nav removes it along with everything else that pass built.
  (function () {
    var head = document.querySelector(".archead");
    var h1 = head ? head.querySelector(".arctitle[data-arc-slug]") : null;
    if (!head || !h1) return;
    var slug = h1.getAttribute("data-arc-slug");

    var followed = [];
    try {
      followed = JSON.parse(localStorage.getItem("followedArcs") || "[]");
    } catch (e) {}

    // Text control, no button chrome (design guidance) — a plain <button>
    // element for semantics/keyboard support, styled bare by .followtoggle.
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "followtoggle";
    var render = function () {
      var isFollowed = followed.indexOf(slug) !== -1;
      btn.textContent = isFollowed ? h1.getAttribute("data-follow-remove") : h1.getAttribute("data-follow-add");
      btn.setAttribute("aria-pressed", String(isFollowed));
    };
    render();
    btn.addEventListener("click", function () {
      var idx = followed.indexOf(slug);
      if (idx === -1) followed.push(slug);
      else followed.splice(idx, 1);
      try {
        localStorage.setItem("followedArcs", JSON.stringify(followed));
      } catch (e) {}
      render();
    }, { signal: signal });
    head.appendChild(btn);
  })();

  // Index filter (roadmap step 6, index pages only — guarded on the input's
  // existence since digest pages have no .filter). applyFilters is the
  // single place that recomputes visibility from the text query, shared by
  // the input's "input" handler so it doesn't duplicate the group-hiding/
  // fence logic anywhere else. Case-insensitive substring match against each
  // .entry's text content (the lead card is an .entry too); a .dayhead hides
  // once every entry in its group (its following siblings up to the next
  // .dayhead) is hidden. No debounce at these list sizes; an empty query
  // restores everything.
  (function () {
    var input = document.querySelector(".filter");
    if (!input) return;
    // The SEARCH page's form input reuses the .filter class for its look
    // (roadmap 4 step 7) but is a server-functional control, not this
    // client-side filter — without this bail, typing a new query there
    // would live-hide the previous results before the form ever submits.
    // The index page's own filter input is the only .filter with no form.
    if (input.form) return;
    input.hidden = false;
    var entries = Array.prototype.slice.call(document.querySelectorAll(".entry"));
    var dayheads = Array.prototype.slice.call(document.querySelectorAll(".dayhead"));
    var section = document.querySelector("section[data-empty-filtered]");
    var emptyEl = null;

    var applyFilters = function () {
      var q = input.value.trim().toLowerCase();
      var anyVisible = false;
      entries.forEach(function (el) {
        el.hidden = Boolean(q) && !el.textContent.toLowerCase().includes(q);
        if (!el.hidden) anyVisible = true;
      });
      dayheads.forEach(function (dh) {
        var group = [];
        var el = dh.nextElementSibling;
        while (el && !el.classList.contains("dayhead")) {
          if (el.classList.contains("entry")) group.push(el);
          el = el.nextElementSibling;
        }
        dh.hidden = group.length > 0 && group.every(function (e) {
          return e.hidden;
        });
      });
      // The unread fence is a load-time artifact; while the filter is active
      // it can end up orphaned between hidden entries, so just hide it
      // whenever a query is active (roadmap 2 step 2).
      var fence = document.querySelector(".unreadfence");
      if (fence) fence.hidden = Boolean(q);
      // Resume chip (roadmap 4 step 3): the chip's own scroll listener owns
      // its show/hide rule (fence hidden OR fence in view -> chip hidden),
      // so after toggling the fence above, just re-trigger that listener
      // rather than duplicating the rule here — setting chip.hidden
      // directly would wrongly un-hide it on query clear even with the
      // fence already in view. Harmless for the other scroll listeners
      // (backfab, the chip's sibling), which are all idempotent recomputes.
      dispatchEvent(new Event("scroll"));

      // Empty-filtered state (roadmap 2 step 5): lazily create the message
      // the first time the filter hides every entry, reusing .empty's
      // styling; hide it again once at least one entry is visible. Guarded
      // on entries.length so a genuinely-empty index (server already
      // rendered its own .empty message) never gets a second one.
      if (entries.length > 0 && section) {
        if (!anyVisible) {
          if (!emptyEl) {
            emptyEl = document.createElement("p");
            emptyEl.className = "empty";
            emptyEl.textContent = section.getAttribute("data-empty-filtered");
            section.appendChild(emptyEl);
          }
          emptyEl.hidden = false;
        } else if (emptyEl) {
          emptyEl.hidden = true;
        }
      }
    };

    input.addEventListener("input", applyFilters, { signal: signal });

    // Unified search (owner UX pass): the same input also live-queries the
    // full-archive search route in the background and injects results below
    // the ledger — extending THIS IIFE rather than adding a second one,
    // since it already owns the input (a second listener would just fight
    // this one for the same element). Guarded on the archiveresults
    // container existing (see renderIndexPage) — digest/search pages never
    // reach here anyway (both guards above already return before this
    // point), but the query stays defensive rather than assuming that.
    var archiveBox = document.querySelector(".archiveresults");
    if (archiveBox) {
      // Token/lang-scoped search route, read off the container rather than
      // hardcoded — keeps this script token/lang-agnostic like every other
      // data-* consumer here.
      var archiveHref = archiveBox.getAttribute("data-search-href");
      var archiveTimer = null;
      var archiveInFlight = null;

      var clearArchive = function () {
        archiveBox.innerHTML = "";
        archiveBox.hidden = true;
      };

      var runArchiveSearch = function () {
        var query = input.value.trim();
        if (query.length < 2) {
          clearArchive();
          return;
        }
        // Abort whatever's still in flight before starting a new request —
        // a slow earlier response landing after a faster later one must
        // never render stale results over fresh ones.
        if (archiveInFlight) archiveInFlight.abort();
        var controller = new AbortController();
        archiveInFlight = controller;
        fetch(archiveHref + "?q=" + encodeURIComponent(query) + "&fragment=1", { signal: controller.signal })
          .then(function (res) {
            if (!res.ok) throw new Error("archive search fetch failed");
            return res.text();
          })
          .then(function (text) {
            if (!text) {
              clearArchive();
              return;
            }
            // Safe to inject verbatim: this is our own server-rendered,
            // fully-escaped HTML from the fragment route (see
            // handleSearchPage/renderSearchFragment) — same origin, same
            // token path, every string in it already ran through esc().
            archiveBox.innerHTML = text;
            // Dedupe (roadmap: data-id): hide any injected result already
            // present in the rendered ledger above, so the reader never
            // sees the same digest twice on one page.
            var shown = new Set(
              entries.map(function (el) {
                return el.getAttribute("data-id");
              }),
            );
            var injected = Array.prototype.slice.call(archiveBox.querySelectorAll(".entry"));
            var anyLeft = false;
            injected.forEach(function (el) {
              if (shown.has(el.getAttribute("data-id"))) {
                el.hidden = true;
              } else {
                anyLeft = true;
              }
            });
            // Every hit was a dupe of something already on the page: hide
            // the whole container, including its "From the archive" label —
            // an empty-looking label is worse than no box at all.
            archiveBox.hidden = !anyLeft;
          })
          .catch(function (err) {
            if (err && err.name === "AbortError") return; // superseded, not a failure
            // Search degrading to filter-only is the correct quiet failure —
            // a broken archive fetch must never surface as an error to a
            // reader who just wanted to filter the visible ledger.
            clearArchive();
          });
      };

      input.addEventListener("input", function () {
        if (archiveTimer) clearTimeout(archiveTimer);
        // Same <2-char rule as runArchiveSearch's own guard, but applied
        // synchronously here (not through the debounce) so an empty/short
        // query can never leave a stale container visible while a 300ms
        // timer is still pending.
        if (input.value.trim().length < 2) {
          if (archiveInFlight) archiveInFlight.abort();
          clearArchive();
          return;
        }
        archiveTimer = setTimeout(runArchiveSearch, 300);
      }, { signal: signal });

      // Enter triggers the pending search immediately instead of waiting out
      // the debounce. The input has no form, so Enter is otherwise inert —
      // this keeps it that way; no navigation, no submit.
      input.addEventListener("keydown", function (e) {
        if (e.key !== "Enter") return;
        if (archiveTimer) clearTimeout(archiveTimer);
        runArchiveSearch();
      }, { signal: signal });

      // One box, not two: the standalone search link is the no-JS fallback
      // (see renderSearchBubble) — once the enhanced archive box is wired
      // up, hide it. Lives in the same search-bubble panel as the filter
      // input this IIFE already owns, so a null guard costs nothing even
      // though this code path only runs where it's known to exist.
      var searchLink = document.querySelector(".searchlink");
      if (searchLink) searchLink.hidden = true;
    }
  })();

  // Next-briefing countdown (roadmap 2 step 3, index pages only — guarded on
  // .countdown existing, since digest pages never render the paragraph; see
  // pageChrome's countdownNewest param). data-newest is the newest WINDOW
  // digest's created_at (ISO UTC); the next window closes exactly 3h later,
  // the digest service's own cadence. Ambient chrome, not a stopwatch — no
  // seconds, refreshed once a minute.
  (function () {
    var el = document.querySelector(".countdown");
    if (!el) return;
    var next = new Date(new Date(el.getAttribute("data-newest")).getTime() + 3 * 60 * 60 * 1000);
    var tmplNext = el.getAttribute("data-tmpl-next");
    var tmplDue = el.getAttribute("data-tmpl-due");
    var unitHour = el.getAttribute("data-unit-hour");
    var unitMinute = el.getAttribute("data-unit-minute");
    var render = function () {
      var remainingMs = next.getTime() - Date.now();
      if (remainingMs <= 0) {
        el.textContent = tmplDue;
      } else {
        var totalMinutes = Math.round(remainingMs / 60000);
        var hours = Math.floor(totalMinutes / 60);
        var minutes = totalMinutes % 60;
        var t = hours > 0
          ? hours + unitHour + " " + minutes + unitMinute
          : minutes + unitMinute;
        el.textContent = tmplNext.replace("{t}", t);
      }
      el.hidden = false;
    };
    render();
    // The interval keeps ticking after a soft-nav swap unless torn down —
    // it doesn't belong to any listener the AbortController above would
    // drop, so it gets its own teardown closure like the resume chip does.
    var intervalId = setInterval(render, 60000);
    teardown.push(function () {
      clearInterval(intervalId);
    });
  })();

  // Keyboard navigation (roadmap 2 step 4, extended §11.1 PR C): desktop
  // convenience, no visible UI hint — the nav arrows already show the
  // model. j/ArrowLeft hop to the OLDER digest, k/ArrowRight to the NEWER
  // one, via the stable nav-older/nav-newer classes renderDigestPage puts
  // on both the top and bottom digestnav (index/arc pages have neither, see
  // below for what j/k do there instead). "/" focuses the index filter,
  // when one exists on the page. j = older = down-the-archive, matching the
  // index's newest-first reading order (vim-scroll intuition); the arrow
  // keys mirror the nav's own visual ← older / newer → arrows. Never
  // intercepts typing: bails on any input/textarea/select/contentEditable
  // target, and on any ctrl/meta/alt modifier so browser shortcuts stay
  // untouched.
  //
  // §11.1 PR C: on a page with no nav-older/nav-newer (index or arc pages —
  // digest pages always have at least one, unless at the very end of the
  // archive, see below), bare j/k (NOT the arrow keys — those stay
  // digest-nav-only) instead move a focus-based selection through the
  // page's primary link list: NOW's .nowrow rows then the ledger's .entry
  // rows, in DOM order (renderNowSection always renders before the ledger —
  // see renderIndexPage), or an arc page's timeline .entry rows (no .nowrow
  // there). Selection IS real focus (moveNavSelection's list[next].focus()),
  // so the site's existing :focus-visible ring is what shows it — no new
  // highlight styling needed. No wrap-around: stepping past either end
  // simply stops. "o" opens the currently focused row (Enter already does,
  // for free — a focused <a> activates on Enter with no JS involved); "o"
  // exists because letting Enter double as "open" conflicts with nothing
  // here, but a dedicated key some readers may expect from other
  // list-nav UIs costs one more branch.
  //
  // A one-digest archive floor (both nav-older and nav-newer absent on a
  // digest page — the archive's very first or only entry) would otherwise
  // silently fall through into the index/arc branch below; harmless in
  // practice (a digest page has no .nowrow/.entry to move a selection
  // through either, so moveNavSelection's empty-list guard just no-ops),
  // so no extra guard is needed to keep that case distinct.
  (function () {
    addEventListener("keydown", function (e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      var t = e.target;
      var tag = t && t.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
      if (e.key === "j" || e.key === "ArrowLeft") {
        var older = document.querySelector(".nav-older");
        // click(), not a location.href assignment: the anchor's click runs
        // through the soft-nav interceptor at the bottom of this script —
        // a direct href assignment is a HARD navigation that bypasses it,
        // which was exactly the residual white-flash path once every
        // pointer navigation had gone soft (keyboard steppers flashed,
        // taps did not).
        if (older) older.click();
        else if (e.key === "j") moveNavSelection(1);
      } else if (e.key === "k" || e.key === "ArrowRight") {
        var newer = document.querySelector(".nav-newer");
        if (newer) newer.click();
        else if (e.key === "k") moveNavSelection(-1);
      } else if (e.key === "o") {
        var active = document.activeElement;
        if (active && (active.classList.contains("nowrow") || active.classList.contains("entry"))) {
          active.click();
        }
      } else if (e.key === "/") {
        var filter = document.querySelector(".filter");
        if (filter) {
          e.preventDefault();
          // The filter input lives inside the closed-by-default search
          // popover (index-cleanup pass) — an element inside a closed
          // details is not rendered, so focus() on it silently no-ops
          // (live-verified in Chromium; there is no auto-open-on-focus
          // fixup to rely on). Open the popover first, then focus; a
          // .filter that is NOT inside the popover (the standalone search
          // page's own form input) has no .searchpop ancestor and skips
          // straight to focus, unchanged.
          var pop = filter.closest("details.searchpop");
          if (pop) pop.open = true;
          filter.focus();
        }
      }
    }, { signal: signal });
  })();

  // ⌘K command palette (§11.1 PR C): markup is created ONCE, lazily, on
  // first wirePage() pass, and left living in document.body OUTSIDE .wrap
  // (like the resume chip elsewhere in this file) — a soft-nav swap only
  // replaces .wrap's innerHTML, so re-creating this dialog on every pass
  // would be wasted work and would drop any state (results scroll position,
  // etc.) for no reason. Its CONTENT is never stale, though: every string
  // and every command is read fresh off the current page's DOM (.paletteconfig,
  // .viewtabs, .langswitch, .archivelink, .searchlink, .now, .entry — see
  // collectPaletteItems) at OPEN time, not at creation time, so a soft-nav
  // page change (including a language hop) is always reflected the next
  // time the palette opens, even though the dialog element itself never
  // gets rebuilt. Listeners on the dialog's own elements ARE rebound every
  // wirePage() pass via signal — same convention as every other feature in
  // this function — which is what "hook into it correctly rather than
  // double-binding" means here: idempotent creation (the "if (!overlay)"
  // guard below) plus per-pass listener rebinding (via signal), never both
  // firing a duplicate DOM append.
  (function () {
    var config = document.querySelector(".paletteconfig");
    if (!config) return; // pageChrome always renders this — defensive only

    var overlay = document.querySelector(".cmdpalette-backdrop");
    var dialog, input, list;
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.className = "cmdpalette-backdrop";
      overlay.hidden = true;
      dialog = document.createElement("div");
      dialog.className = "cmdpalette";
      dialog.setAttribute("role", "dialog");
      dialog.setAttribute("aria-modal", "true");
      // "labelled by the input" (§11.1 PR C spec), not a separate heading —
      // the input doubles as both the dialog's accessible name AND the
      // control the reader actually types into.
      dialog.setAttribute("aria-labelledby", "cmdpalette-input");
      input = document.createElement("input");
      input.type = "text";
      input.id = "cmdpalette-input";
      input.className = "cmdpalette-input";
      input.autocomplete = "off";
      input.spellcheck = false;
      list = document.createElement("div");
      list.className = "cmdpalette-list";
      list.setAttribute("role", "listbox");
      dialog.appendChild(input);
      dialog.appendChild(list);
      overlay.appendChild(dialog);
      document.body.appendChild(overlay);
    } else {
      dialog = overlay.querySelector(".cmdpalette");
      input = overlay.querySelector(".cmdpalette-input");
      list = overlay.querySelector(".cmdpalette-list");
    }

    var currentResults = [];
    var activeIndex = -1;
    var previouslyFocused = null;

    // Assembled fresh on every open — see the IIFE's own comment above for
    // why this must never be cached across soft-navs. Static commands
    // first, then whatever the current page's own DOM contributes (arcs,
    // then briefings) — same "commands, then arcs, then briefings" order
    // the §11.1 spec lists them in.
    function collectPaletteItems() {
      var items = [];
      items.push({
        label: config.getAttribute("data-cmd-top"),
        action: function () {
          window.scrollTo({ top: 0, behavior: "smooth" });
        },
      });
      // View tabs: only the non-active ones carry an href (see
      // renderViewTabs) — reading data-view off each rather than parsing
      // translated tab text is what lets this stay lang-agnostic.
      Array.prototype.slice.call(document.querySelectorAll(".viewtabs .viewtab[href]")).forEach(function (tab) {
        var v = tab.getAttribute("data-view");
        var key = v === "daily" ? "data-cmd-daily" : v === "weekly" ? "data-cmd-weekly" : "data-cmd-all";
        items.push({ label: config.getAttribute(key), el: tab });
      });
      var searchLink = document.querySelector(".searchlink");
      if (searchLink) items.push({ label: config.getAttribute("data-cmd-search"), el: searchLink });
      // The masthead Archive link this used to read (.archivelink) was
      // removed in the index-cleanup pass — this lookup now always comes up
      // empty, so the "Archive" command simply never gets pushed, the same
      // graceful-disappearance behavior every other optional command here
      // already relies on (compare searchLink/langLink/latest just above and
      // below). Left in place rather than deleted: harmless dead code that
      // documents its own absence, and a future masthead Archive link (if
      // one ever comes back) would only need its class restored, not this.
      var archiveLink = document.querySelector(".archivelink");
      if (archiveLink) items.push({ label: config.getAttribute("data-cmd-archive"), el: archiveLink });
      var langLink = document.querySelector(".langswitch a");
      if (langLink) items.push({ label: config.getAttribute("data-cmd-switchlang"), el: langLink });
      var latest = document.querySelector(".wrap .entry");
      if (latest) items.push({ label: config.getAttribute("data-cmd-latest"), el: latest });

      // Arcs: NOW's .nowrow rows (label + href), when present (§11.1 PR C
      // spec 2b) — index pages, current week only, see renderNowSection.
      Array.prototype.slice.call(document.querySelectorAll(".now .nowrow")).forEach(function (row) {
        var labelEl = row.querySelector(".nowarclabel");
        items.push({ label: (labelEl ? labelEl.textContent : row.textContent).trim(), el: row, group: "arc" });
      });

      // Briefings: visible ledger .entry links, capped at 20 (§11.1 PR C
      // spec 2c) — their time + excerpt text as the label, same "read text
      // off the rendered DOM" approach as everywhere else in this function.
      // Deliberately NOT deduped against the "Latest briefing" command
      // above (that command is a convenience shortcut to the same target,
      // not a separate source) — a small, harmless overlap, not worth the
      // extra bookkeeping to avoid.
      Array.prototype.slice
        .call(document.querySelectorAll(".wrap .entry"))
        .slice(0, 20)
        .forEach(function (entry) {
          var timeEl = entry.querySelector(".time, .eyebrow-text");
          var excerptEl = entry.querySelector(".excerpt");
          var label = (timeEl ? timeEl.textContent + " — " : "") + (excerptEl ? excerptEl.textContent : "");
          items.push({ label: label.trim(), el: entry, group: "briefing" });
        });

      return items;
    }

    var allItems = [];

    function renderResults(query) {
      var q = query.trim().toLowerCase();
      var filtered = allItems
        .filter(function (item) {
          return !q || item.label.toLowerCase().indexOf(q) !== -1;
        })
        .slice(0, 12);
      currentResults = filtered;
      activeIndex = filtered.length > 0 ? 0 : -1;
      list.innerHTML = "";
      if (filtered.length === 0) {
        var empty = document.createElement("div");
        empty.className = "cmdpalette-empty";
        empty.textContent = config.getAttribute("data-empty");
        list.appendChild(empty);
        return;
      }
      filtered.forEach(function (item, i) {
        var row = document.createElement("div");
        row.className = "cmdpalette-item" + (i === 0 ? " active" : "");
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", String(i === 0));
        row.dataset.index = String(i);
        var labelSpan = document.createElement("span");
        labelSpan.textContent = item.label;
        row.appendChild(labelSpan);
        if (item.group) {
          var groupSpan = document.createElement("span");
          groupSpan.className = "cmdpalette-group";
          groupSpan.textContent = item.group;
          row.appendChild(groupSpan);
        }
        list.appendChild(row);
      });
    }

    function reflectActive() {
      var rows = list.querySelectorAll(".cmdpalette-item");
      for (var i = 0; i < rows.length; i++) {
        var isActive = i === activeIndex;
        rows[i].classList.toggle("active", isActive);
        rows[i].setAttribute("aria-selected", String(isActive));
        if (isActive) rows[i].scrollIntoView({ block: "nearest" });
      }
    }

    function moveActive(delta) {
      if (currentResults.length === 0) return;
      activeIndex = (activeIndex + delta + currentResults.length) % currentResults.length;
      reflectActive();
    }

    function activateSelection() {
      var item = currentResults[activeIndex];
      if (!item) return;
      closePalette();
      if (item.action) item.action();
      // el.click() (not a direct navigation): the click event bubbles up
      // to the document-level soft-nav interceptor exactly like a real
      // pointer click on that same anchor would, so results navigate
      // through the soft path — see the "Keyboard navigation" comment
      // above for why click() is used instead of location assignment
      // throughout this file.
      else if (item.el) item.el.click();
    }

    function openPalette() {
      previouslyFocused = document.activeElement;
      // Re-read every string fresh — see the IIFE's own top comment on why
      // this must never rely on values captured at creation time.
      input.placeholder = config.getAttribute("data-placeholder");
      input.setAttribute("aria-label", config.getAttribute("data-label"));
      allItems = collectPaletteItems();
      input.value = "";
      renderResults("");
      overlay.hidden = false;
      input.focus();
    }

    function closePalette() {
      if (overlay.hidden) return;
      overlay.hidden = true;
      if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
    }

    overlay.addEventListener(
      "click",
      function (e) {
        if (e.target === overlay) closePalette();
      },
      { signal: signal },
    );
    input.addEventListener("input", function () { renderResults(input.value); }, { signal: signal });
    input.addEventListener(
      "keydown",
      function (e) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          moveActive(1);
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          moveActive(-1);
        } else if (e.key === "Enter") {
          e.preventDefault();
          activateSelection();
        }
      },
      { signal: signal },
    );
    list.addEventListener(
      "click",
      function (e) {
        var row = e.target.closest ? e.target.closest(".cmdpalette-item") : null;
        if (!row) return;
        activeIndex = Number(row.dataset.index);
        activateSelection();
      },
      { signal: signal },
    );
    // Escape closes regardless of where focus happens to be — same
    // unconditional-on-focus-target convention the settings-bubble's own
    // Escape handler above already uses (unlike the OPEN trigger below,
    // which — per the §11.1 spec's "no bindings while focus is in an
    // input/textarea/select or contenteditable" guardrail — deliberately
    // does NOT fire while the reader is typing somewhere else on the page).
    document.addEventListener(
      "keydown",
      function (e) {
        if (e.key === "Escape" && !overlay.hidden) closePalette();
      },
      { signal: signal },
    );
    document.addEventListener(
      "keydown",
      function (e) {
        var t = e.target;
        var tag = t && t.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
        if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && (e.key === "k" || e.key === "K")) {
          e.preventDefault();
          openPalette();
        }
      },
      { signal: signal },
    );
  })();
  }

  // ── soft navigation ────────────────────────────────────────────────────
  // Every page here is Cache-Control: private, no-store (see the file-header
  // comment / trust model) — load-bearing, never touched — so a plain link
  // click is a full network round trip every time, and on a slow response
  // iOS Safari's canvas gap white-flashes despite color-scheme and the
  // cross-document view transition (see the :root comment above). The fix:
  // for internal links, fetch the next page in the background while the
  // CURRENT page stays fully visible, then swap the new content into the
  // live document inside a same-document View Transition (withPageTransition,
  // above) instead of letting the browser tear down and rebuild everything.
  // Nothing here is ever persisted — in-memory fetch only, gone on reload —
  // and a JS-off reader simply never gets this listener, so every link keeps
  // working as a plain navigation for them.
  //
  // Attached ONCE, at module init, before the first wirePage() call — see
  // the boot sequence at the bottom of this script.
  (function () {
    // The token lives in every href on this page already, but this script
    // never hardcodes it — the prefix is DERIVED from the page's own URL
    // (the first two path segments, "/t/<token>/") once, at module init.
    var pathSegments = location.pathname.split("/");
    var tokenPrefix = "/" + pathSegments[1] + "/" + pathSegments[2] + "/";

    // One AbortController per soft nav: starting a new one aborts whatever
    // was still in flight, and the reference comparison below (controller
    // !== activeNav) catches the rarer case where an old response arrives
    // AFTER a newer nav is already under way but wasn't itself cancelled in
    // time — a rapid string of soft-navs must never let a stale response
    // win the race and overwrite what the reader is now looking at.
    var activeNav = null;

    // The path+search this script has actually rendered, kept in the same
    // "pathname + search" shape doSoftNav takes (deliberately WITHOUT the
    // fragment). The popstate handler at the bottom compares against this
    // to tell a real history move from a same-document fragment move —
    // see there for why that distinction is load-bearing.
    var lastRendered = location.pathname + location.search;

    // hash param (§11.1 PR A follow-on fix): a CROSS-page link that also
    // carries a fragment — e.g. an arc page's deep link into a digest's own
    // #sN section (renderArcAppearance) — used to lose the fragment on the
    // soft-nav path: the click handler below only ever passed
    // dest.pathname + dest.search into this function, so the swap landed on
    // the right PAGE but never scrolled to the right SECTION, silently,
    // with no error — only a plain full navigation (or JS disabled)
    // happened to work by accident, via the browser's own native handling.
    // This was already a latent bug for the TOC's own #sN chips (dead code
    // path before this feature: no chip anywhere on the site pointed at a
    // DIFFERENT page's fragment until arc pages existed to do it) — fixed
    // here rather than shipping a feature whose flagship mechanic silently
    // degrades for every JS-enabled reader. hash is either "" or something
    // like "#sN" (URL.hash's own shape, including the "#"); scrolling only
    // happens once the swap has actually landed, same ordering as
    // wirePage() below.
    function doSoftNav(href, isPopstate, hash) {
      if (activeNav) activeNav.abort();
      var controller = new AbortController();
      activeNav = controller;
      fetch(href, { signal: controller.signal })
        .then(function (res) {
          if (!res.ok) throw new Error("soft-nav: response not ok");
          return res.text();
        })
        .then(function (html) {
          if (controller !== activeNav) return; // superseded — the newer nav owns the screen
          var doc = new DOMParser().parseFromString(html, "text/html");
          var newWrap = doc.querySelector(".wrap");
          var curWrap = document.querySelector(".wrap");
          if (!newWrap || !curWrap) throw new Error("soft-nav: .wrap missing");
          withPageTransition(function () {
            // .wrap is the ENTIRE page body except the masthead-adjacent
            // script tag and the resume chip (both live outside it) — see
            // pageChrome's document skeleton above — so swapping just its
            // innerHTML replaces everything a reader would call "the page"
            // in one move. speculationrules/prefetch artifacts in the
            // fetched document live in head/body root, never inside .wrap
            // (see pageChrome), so this scoped swap sidesteps them for
            // free — nothing to strip.
            curWrap.innerHTML = newWrap.innerHTML;
            document.title = doc.title;
            // Language hops (EN/HU switcher) change the document's lang —
            // carry that onto the live <html> along with the content.
            document.documentElement.lang = doc.documentElement.lang;
            // Deliberately UNCHANGED: html's data-theme/data-density/
            // data-fontsize. Those are live preference state, not page
            // content — leaving them alone is what makes the swap flicker-
            // free (no re-applying a preference that was already in effect).
            // Same for the theme-color metas in <head> — theme state is
            // live-owned, the fetched document's copies are simply ignored.
            //
            // The fetched document's own <script> tag never runs — an
            // innerHTML assignment inertly skips embedded scripts, and it's
            // moot here anyway since .wrap never contained the script tag
            // to begin with. The LIVE page's script owns behavior; that's
            // the whole point of re-wiring instead of reloading.
            wirePage();
            // Scroll restoration: a plain forward soft-nav or popstate
            // soft-load lands at the top, same as always. A hash-bearing
            // cross-page link (see the function comment above) scrolls the
            // target element into view instead, once it's actually in the
            // freshly-swapped DOM — getElementById, not querySelector, since
            // every id this ever targets (buildSectionToc's "sN") is a
            // literal token, never CSS-special characters. An id that isn't
            // in the fetched page (a stale/bad fragment) falls back to the
            // top, same as no hash at all — never a jump to nowhere.
            // behavior "instant", explicitly: the stylesheet's
            // scroll-behavior: smooth (reduced-motion-gated, see the CSS)
            // turns a bare scrollIntoView()/scrollTo() into an ANIMATED
            // scroll, and an animated scroll started inside this
            // startViewTransition update callback is cancelled when the
            // transition snapshots the new state — observed live on deploy
            // day: the scrollIntoView call fired, the viewport never moved.
            // An explicit behavior overrides the CSS per spec; the
            // transition's own crossfade is this swap's motion story
            // anyway, an animated scroll under it was never wanted.
            var target = hash ? document.getElementById(hash.slice(1)) : null;
            if (target) target.scrollIntoView({ behavior: "instant", block: "start" });
            else window.scrollTo({ top: 0, left: 0, behavior: "instant" });
          });
          // Committed: this address is now what's on screen. Set for BOTH
          // directions (forward soft-nav and popstate soft-load), and only
          // once the swap has actually happened — an aborted or superseded
          // nav returns above and must never move this. The hash rides
          // along in the URL bar (history.pushState) but NOT in
          // lastRendered/href themselves — every existing comparison
          // against those two (the popstate fragment-only bail below,
          // fetch(href) above) is deliberately path+search-only, and adding
          // the hash to either would break that fragment-vs-real-move
          // distinction this whole soft-nav module already depends on.
          lastRendered = href;
          if (!isPopstate) history.pushState({ soft: true }, "", href + (hash || ""));
        })
        .catch(function (err) {
          if (err && err.name === "AbortError") return; // superseded fetch, silent
          // Fetch failure, non-ok status, or any exception during the swap
          // itself: fall back to the ordinary navigation the reader always
          // had. Correctness never depends on the soft path working.
          location.href = href + (hash || "");
        });
    }

    // Capture phase, attached before wirePage's first call — see the
    // settings-dismiss handler inside wirePage above. That handler also
    // listens for document clicks in the capture phase; because this
    // listener is registered first (module init, before the initial
    // wirePage() call at the bottom of this script) and re-registering it
    // inside wirePage never moves this one, this listener always runs
    // FIRST in capture order, on every pass. That ordering is what the
    // settings-open bail below depends on.
    document.addEventListener(
      "click",
      function (e) {
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
        if (e.button !== 0) return;
        var a = e.target.closest ? e.target.closest("a[href]") : null;
        if (!a) return;
        if (a.target) return; // opens elsewhere (new tab, frame) — let it
        if (a.hasAttribute("download")) return;
        var dest;
        try {
          dest = new URL(a.href, location.href);
        } catch (err) {
          return;
        }
        if (dest.origin !== location.origin) return;
        if (dest.pathname.indexOf(tokenPrefix) !== 0) return; // cite links, robots/favicon — not ours
        // Hash-only change on the same path/query: let the browser do its
        // native in-page anchor scroll instead of soft-navving nowhere.
        if (dest.pathname === location.pathname && dest.search === location.search && dest.hash) return;
        // Settings dismiss-swallow interplay: with the bubble open, a click
        // OUTSIDE it belongs to the dismiss handler behind this listener
        // (see above) — bail here with no preventDefault so that click
        // reaches it untouched. The one exception is a click INSIDE the
        // open panel itself (the language switcher) — those soft-nav
        // normally, same as any other internal link.
        var openSettings = document.querySelector("details.settings[open]");
        if (openSettings && !openSettings.contains(a)) return;
        e.preventDefault();
        doSoftNav(dest.pathname + dest.search, false, dest.hash);
      },
      true,
    );

    addEventListener("popstate", function () {
      var here = location.pathname + location.search;
      // Fragment-only move — bail (owner-reported 2026-08-10: every TOC
      // chip scrolled down, then snapped back to the top).
      //
      // A fragment navigation ("#s4" from renderToc's chips, or any
      // in-page anchor) is a SAME-DOCUMENT navigation, and browsers fire
      // popstate for those as well as for real history traversals —
      // popstate first, then hashchange. Unguarded, this handler treated
      // that as a history move and soft-navved to the page the reader was
      // already on: the swap replaced .wrap mid-scroll, destroying the
      // element the browser's smooth scroll was animating toward, and the
      // trailing scrollTo(0, 0) put them back at the top.
      //
      // The click interceptor above already declines hash-only links (see
      // its own bail); this is the SAME condition arriving through the
      // other entry point into doSoftNav. Comparing path+search — neither
      // of which a fragment move changes — is what distinguishes them.
      // The browser's native anchor scroll is exactly right here and
      // needs no help from this script.
      if (here === lastRendered) return;
      // location.hash already reflects wherever the browser just navigated
      // the address bar to (a real history move, not a fragment-only one —
      // ruled out just above) — threaded through so a cross-page hash link
      // (see doSoftNav's own comment) still resolves correctly on a
      // back/forward traversal, not just on the initial click.
      doSoftNav(here, true, location.hash);
    });
  })();

  wirePage();
</script>
</body>
</html>`;
}

// ── page bodies ──────────────────────────────────────────────────────────

function groupByDay(rows, locale) {
  const groups = [];
  let currentLabel = null;
  let currentItems = null;
  for (const row of rows) {
    const label = formatDayHeader(new Date(row.created_at), locale);
    if (label !== currentLabel) {
      currentLabel = label;
      currentItems = [];
      groups.push({ label, items: currentItems });
    }
    currentItems.push(row);
  }
  return groups;
}

// Shared TL;DR excerpt logic — HU page: prefer the translated tldr; if the
// app never sent one for this digest, fall back to the English tldr and mark
// it with a muted "EN" chip rather than silently presenting English text as
// if translated. Used by both the compact ledger entries (renderIndexEntry)
// and the lead card (renderLeadCard) so the two never drift apart.
function renderExcerpt(row, lang) {
  let excerptHtml = esc(row.tldr);
  let langChip = "";
  if (lang === "hu") {
    if (row.tldr_hu) {
      excerptHtml = esc(row.tldr_hu);
    } else {
      langChip = '<span class="flag flag-muted">EN</span>';
    }
  }
  return { excerptHtml, langChip };
}

// Kind badge for daily/weekly rows — "" for a window row, and "" for a
// daily row in the daily view or a weekly row in the weekly view (the badge
// is redundant there: every row is already that same kind of brief; only
// the all view needs it to tell the kinds apart at a glance). Weekly is the
// SAME visual family as daily, not a new kind of thing — it's a synthesis
// too, just a wider window — so it reuses the identical `flag-daily` class
// and filled-indigo look, only the label text differs. The redundancy rule
// is symmetric: a daily row hides its badge in the daily view, a weekly row
// hides its badge in the weekly view, and each kind always shows its badge
// elsewhere — daily/ filters strictly to kind='daily' and weekly/ strictly
// to kind='weekly' (see the file-header "Daily-brief view" comment), so a
// daily row never actually reaches the weekly view or vice versa; the check
// below is the simplest faithful form regardless. Used by renderIndexEntry
// and renderLeadCard so the two never drift apart building this separately.
function kindBadge(row, view, strings) {
  if (row.kind === "daily" && view !== "daily") {
    return `<span class="flag flag-daily">${esc(strings.dailyBrief)}</span>`;
  }
  if (row.kind === "weekly" && view !== "weekly") {
    return `<span class="flag flag-daily">${esc(strings.weeklyBrief)}</span>`;
  }
  return "";
}

// Source-spectrum palette (roadmap 2 step 8): a STABLE per-source hue for
// the five collectors the digest app currently has, muted so the bar reads
// as metadata rather than a call to action — distinct hues so sources stay
// tellable apart, not a sequential/brand ramp. One palette for both light
// and dark themes; a softened dark-mode variant isn't worth the complexity
// for a 3.2em bar (see the CSS block below). Unknown source names (the app
// ships a new collector before this map is updated — deliberately allowed,
// see SOURCE_NAME_RE's comment) fall back to a neutral gray rather than
// erroring or being dropped from the bar.
const SOURCE_COLORS = {
  telegram: "#4f8fd9",
  x: "#8a8f9e",
  news: "#c58f5a",
  polymarket: "#7a5ad9",
  reddit: "#d95a4f",
};
const SOURCE_COLOR_FALLBACK = "#9aa0ab";

// Degraded-run badge (roadmap 2 step 8): shown when failed_sources parses to
// a non-empty array — fail-safe JSON.parse contract (unparseable or
// wrong-shaped -> treated as absent, never thrown), same posture
// renderSourceKey below and this file's other D1-JSON-column readers all
// share, defending against a stored value that predates a validation
// change. Reuses the existing .flag pill shape, but the muted .flag-muted
// colors rather than the amber attention ones — this is a fact about a
// collection run, not something that needs the reader's attention the way
// has_attention does — so the ⚠ prefix, not color, is what marks it.
// `strings` is the caller's STRINGS[lang] (for the localized
// "partial"/"hiányos" label); the failed source names themselves stay
// untranslated in the title, same as source_counts' names in
// renderSourceKey.
function renderDegradedBadge(failedSourcesJson, strings) {
  if (!failedSourcesJson) return "";
  let names;
  try {
    names = JSON.parse(failedSourcesJson);
  } catch {
    return "";
  }
  if (!Array.isArray(names) || names.length === 0) return "";
  const title = names.join(", ");
  return `<span class="flag flag-degraded" title="${esc(title)}">⚠ ${esc(strings.degraded)}</span>`;
}

// Source key (digest-page colophon, roadmap 2 step 8 follow-up): concrete
// per-source numbers with color swatches, plus any failed sources from a
// partial run. Same fail-safe JSON.parse contract as renderDegradedBadge
// above (unparseable or wrong-shaped -> treated as absent, never thrown);
// renders nothing at all when both fields are absent/empty, so an old
// digest predating this data shows no key. (The index page's own micro-bar
// counterpart, .spectrum/renderSpectrum, was removed in the owner-requested
// index-cleanup pass; this digest-page colophon is unaffected.)
function renderSourceKey(sourceCountsJson, failedSourcesJson, strings) {
  let counts = null;
  if (sourceCountsJson) {
    try {
      const parsed = JSON.parse(sourceCountsJson);
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
        counts = parsed;
      }
    } catch {
      // unparseable -> treat as absent
    }
  }
  let failed = null;
  if (failedSourcesJson) {
    try {
      const parsed = JSON.parse(failedSourcesJson);
      if (Array.isArray(parsed) && parsed.length > 0) failed = parsed;
    } catch {
      // unparseable -> treat as absent
    }
  }

  // Descending by count — reads as a ranked list, highest-volume source first.
  const countEntries = counts
    ? Object.entries(counts)
        .filter(([, n]) => typeof n === "number" && n > 0)
        .sort((a, b) => b[1] - a[1])
    : [];

  if (countEntries.length === 0 && !failed) return "";

  const countSpans = countEntries
    .map(
      ([name, n]) =>
        `<span class="sk"><i style="background:${SOURCE_COLORS[name] ?? SOURCE_COLOR_FALLBACK}"></i>${esc(name)} ${esc(n)}</span>`,
    )
    .join("");
  // No count next to a failed source — it failed, nothing to count; the ⚠
  // is the marker, same "no new color" decision as renderDegradedBadge.
  const failedSpans = failed
    ? failed.map((name) => `<span class="sk sk-failed">⚠ ${esc(name)}</span>`).join("")
    : "";

  return `<div class="sourcekey"><span class="sklabel">${esc(strings.sourcesLabel)}</span>${countSpans}${failedSpans}</div>`;
}

function renderIndexEntry(row, token, lang, view) {
  const strings = STRINGS[lang];
  const time = formatTime(new Date(row.created_at), strings.locale);
  // Weekly gets the same accent/clamp treatment as daily — both are a
  // synthesis, just a different window — so this checks "not a plain
  // window digest" rather than "is daily" specifically; see kindBadge for
  // the badge itself.
  const isSynthesis = row.kind !== "window";
  const badgeHtml = kindBadge(row, view, strings);

  const { excerptHtml, langChip } = renderExcerpt(row, lang);

  const counts = `${esc(row.item_count)} ${esc(strings.itemsWord)} · ${esc(row.section_count)} ${esc(strings.sectionsWord)}`;
  const timeClass = isSynthesis ? "time time-accent" : "time";
  const excerptClass = isSynthesis ? "excerpt excerpt-daily" : "excerpt";

  // Degraded-run badge (roadmap 2 step 8): renders "" when the row has no
  // failed_sources data (older digests, or an app version that doesn't send
  // it yet) — see renderDegradedBadge. The source-spectrum micro-bar this
  // used to render alongside (renderSpectrum) was removed from the index in
  // the owner-requested index-cleanup pass — the ledger's own recency
  // already communicates what the bar did; the digest page's own
  // renderSourceKey colophon (unaffected by this pass) still carries that
  // provenance in full.
  const degradedHtml = renderDegradedBadge(row.failed_sources, strings);

  // data-created (roadmap 2 step 2, unread fence): the row's own created_at,
  // straight from D1 as an ISO UTC string — lexicographically comparable
  // without parsing, the same trick get_recent_digests (digest repo) relies
  // on. esc()'d like every other D1-sourced value inserted as an attribute.
  //
  // data-id (unified search, owner UX pass): same contract as
  // renderSearchResult's — see comments there. Lets the archive-results
  // script tell "already in this ledger" from "genuinely archive-only".
  return `<a class="entry" href="${digestHref(token, lang, view, row.id)}" data-created="${esc(row.created_at)}" data-id="${esc(row.id)}">
    <span class="meta"><span class="${timeClass}">${esc(time)}</span><span class="count">${counts}</span>${degradedHtml}${badgeHtml}${langChip}</span>
    <p class="${excerptClass}"><strong>${esc(strings.tldrLabel)}</strong> ${excerptHtml}</p>
  </a>`;
}

// The lead card (roadmap step 4): the newest digest in the current view,
// rendered full-weight above the compact ledger — the reader's most common
// task is "read the newest one". Structurally still one big clickable
// `.entry` <a>, same pattern as renderIndexEntry, but the usual time+count
// `.meta` row is replaced by a mono dateline eyebrow (same flex layout,
// class="meta" reused) and the excerpt runs unclamped at a slightly larger
// size (see the .entry-lead CSS).
function renderLeadCard(row, token, lang, view) {
  const strings = STRINGS[lang];
  const date = new Date(row.created_at);
  // See kindBadge — same daily/weekly badge as renderIndexEntry's, so the
  // two never drift apart building it separately.
  const badgeHtml = kindBadge(row, view, strings);

  const { excerptHtml, langChip } = renderExcerpt(row, lang);

  const eyebrow = `${strings.latest} · ${formatShortDate(date, strings.locale)} · ${formatTime(date, strings.locale)} ${tzAbbr(date)} · ${row.item_count} ${strings.itemsWord}`;

  // Degraded-run badge: same contract as renderIndexEntry's — see comments
  // there (including why there's no source-spectrum bar alongside it here).
  const degradedHtml = renderDegradedBadge(row.failed_sources, strings);

  // data-created / data-id: same contract as renderIndexEntry's — see
  // comments there.
  return `<a class="entry entry-lead" href="${digestHref(token, lang, view, row.id)}" data-created="${esc(row.created_at)}" data-id="${esc(row.id)}">
    <span class="meta"><span class="eyebrow-text">${esc(eyebrow)}</span>${degradedHtml}${badgeHtml}${langChip}</span>
    <p class="excerpt"><strong>${esc(strings.tldrLabel)}</strong> ${excerptHtml}</p>
  </a>`;
}

// Search bubble (owner-requested index cleanup): replaces the old
// always-visible filterrow (a standalone "Filter briefings…" input row plus
// a "Search ↗" link) with ONE compact control, matching the masthead's own
// settings-gear disclosure (see renderSwitchers/the .settings*/summary.gear
// CSS pattern, extended by the .searchpop/.searchpanel/summary.searchtoggle
// rules alongside it) — a native <details>/<summary> popover that opens
// with no JS, so a no-JS reader still reaches the "Search ↗" fallback link
// inside. Carries the exact SAME .filter input (same class, same
// hidden-until-JS default, same placeholder) and the exact SAME .searchlink
// no-JS fallback the old filterrow had — only their DOM position moved, so
// the filter IIFE and the archive-search integration in pageChrome's bottom
// script (both `document.querySelector(".filter")`/`.searchlink`, neither
// scoped to a particular ancestor) keep working unchanged. Called from
// renderWeekRail (the ALL view, where it rides as a fourth flex child in the
// week-rail row itself) and directly from renderIndexPage on the daily/
// weekly views, which have no week rail to live in — see that call site.
function renderSearchBubble(token, lang, strings) {
  // Icon, not the word (owner-requested): an inline SVG magnifier rather
  // than a glyph character — U+2315/U+26B2 render inconsistently across
  // platforms and the emoji magnifier drags its own colour into a
  // deliberately muted palette. currentColor + the 1.6 stroke keeps it in
  // the same weight register as the ⚙ gear beside it. The label survives
  // as the accessible name (aria-label), so nothing is lost to a screen
  // reader or the ⌘K palette's own DOM scrape.
  const icon = `<svg class="searchicon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10.6 10.6 L14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`;
  return `<details class="searchpop"><summary class="searchtoggle" aria-label="${esc(strings.searchToggleLabel)}" title="${esc(strings.searchToggleLabel)}">${icon}</summary><div class="searchpanel"><input class="filter" type="search" placeholder="${esc(strings.filterPlaceholder)}" aria-label="${esc(strings.filterPlaceholder)}" hidden><a class="searchlink" href="${searchHref(token, lang)}">${esc(strings.searchLink)}</a></div></details>`;
}

// Week rail (roadmap 3 step 2): mono wire-style `← W31 · WEEK 32 · 3–9 AUG ·
// W33 →` nav, rendered on ALL-view index pages only — see the call site in
// renderIndexPage, and the file-header roadmap notes on why the daily view
// has no week address. `weekInfo` is handleIndexPage's { year, week,
// isCurrentWeek, older, newer } (older/newer are {year,week} or null — see
// that function). Absent older/newer render as empty (but still flex:1)
// spacer spans, via the shared .rail-older/.rail-newer classes, so the
// center label stays visually centered either way (see the .weekrail CSS).
// The search bubble (see renderSearchBubble just above) rides as a fourth,
// non-growing flex child after rail-newer — the two flex:1 spacer spans
// still grow to equal widths regardless, so the center label stays centered
// between them; the search control just sits further right, clear of the
// rail-newer "→" link on an archive week (§11 index-cleanup spec: "must not
// collide with the rail-newer link").
//
// Archive sparkline (roadmap 4 step 6): `rows` is null on every page except
// an archive week (see the call site in renderIndexPage, which passes null
// on the current week so its rail stays byte-identical to before this
// step).
function renderWeekRail(token, lang, weekInfo, strings, rows = null) {
  const olderLink = weekInfo.older
    ? `<a href="${weekHref(token, lang, "all", weekInfo.older.year, weekInfo.older.week)}">← W${esc(String(weekInfo.older.week).padStart(2, "0"))}</a>`
    : "";

  // The CURRENT week's own "newer" target is, by definition, the current
  // week itself — recomputed here (isoWeekOf is a cheap pure function)
  // rather than threaded through weekInfo, so weekInfo stays a plain
  // description of THIS page's own week. When the newer target IS the
  // current week, link to the ROOT index instead of a w/YYYY-Www/ address
  // for it — one canonical URL for the current week, not two addresses for
  // the same page.
  const current = isoWeekOf(new Date());
  let newerLink = "";
  if (weekInfo.newer) {
    const newerHref =
      compareIsoWeek(weekInfo.newer, current) === 0
        ? indexHref(token, lang, "all")
        : weekHref(token, lang, "all", weekInfo.newer.year, weekInfo.newer.week);
    newerLink = `<a href="${newerHref}">W${esc(String(weekInfo.newer.week).padStart(2, "0"))} →</a>`;
  }

  const centerLabel = strings.weekLabel
    .replace("{w}", String(weekInfo.week))
    .replace("{range}", formatWeekRangeLabel(weekInfo.year, weekInfo.week, strings.locale));

  // Archive sparkline (roadmap 4 step 6): seven per-day micro-bars for THIS
  // week only, built when (and only when) the caller handed us rows — see
  // the function comment above. Window digests only (kind === "window" — a
  // daily brief re-synthesizes the same day's items, so counting it too
  // would double the day), summed by budapestDateParts key, then read back
  // per day of the week's own Mon..Sun span off a FRESH per-day proxy copy
  // each iteration —
  // mondayOfIsoWeek's proxy must never be mutated in place across
  // iterations, or every day would collapse onto the same Monday.
  let sparkHtml = "";
  if (Array.isArray(rows)) {
    const daySums = new Map();
    for (const row of rows) {
      if (row.kind !== "window") continue;
      const { y, m, d } = budapestDateParts(new Date(row.created_at));
      const key = `${y}-${m}-${d}`;
      daySums.set(key, (daySums.get(key) ?? 0) + row.item_count);
    }

    const monday = mondayOfIsoWeek(weekInfo.year, weekInfo.week);
    const days = [];
    for (let offset = 0; offset < 7; offset += 1) {
      const day = new Date(monday);
      day.setUTCDate(day.getUTCDate() + offset);
      days.push(day);
    }
    const counts = days.map((day) => {
      const { y, m, d } = budapestDateParts(day);
      return daySums.get(`${y}-${m}-${d}`) ?? 0;
    });

    // Normalized against the WEEK'S OWN max day-sum — this bar cluster only
    // ever shows one week at a time, so there's no wider window to be
    // comparable against. An empty week (every day zero) renders no sparkline
    // at all rather than seven identical hairline bars — noise, not signal.
    const max = Math.max(0, ...counts);
    if (max > 0) {
      const bars = days
        .map((day, i) => {
          const count = counts[i];
          const title = `${formatShortDate(day, strings.locale)} · ${count} ${strings.itemsWord}`;
          // Zero-count days still render a bar (class "sd0", no inline
          // height — .railspark's CSS gives sd0 a fixed 15% so there's no
          // specificity fight with the inline height below) so the week
          // always reads as seven days, not a gappy row.
          if (count === 0) {
            return `<i class="sd0" title="${esc(title)}"></i>`;
          }
          const pct = Math.max(15, Math.round((count / max) * 100));
          return `<i style="height:${pct}%" title="${esc(title)}"></i>`;
        })
        .join("\n");
      sparkHtml = `<span class="railspark" aria-hidden="true">${bars}</span>`;
    }
  }

  return `<nav class="weekrail" aria-label="${esc(strings.weekRailLabel)}"><span class="rail-older">${olderLink}</span><span class="rail-center">${esc(centerLabel)}${sparkHtml}</span><span class="rail-newer">${newerLink}</span></nav>`;
}

// NOW section ranking (§11.1 PR B, "decide + implement the NOW ranking
// rule"): pure aggregation over handleIndexPage's now-arcs query rows —
// (identity, label, created_at, id) for every topic appearance in the
// trailing 7 days, one row per digest×topic. The query deliberately leaves
// aggregation to JS instead of GROUP BY + window functions (see its own
// comment in handleIndexPage) specifically so "label of the latest
// appearance" is a plain forward scan, not a second query or a window-
// function fight — bounded input (<=12 topics x ~80 digests/week is a few
// hundred rows) makes that scan cheap on every current-week-all-view
// render.
//
// Grouped by arc IDENTITY (stable arc keys, see arcIdentity/ARC_IDENTITY_SQL)
// rather than raw slug — this is the actual bug fix: a story recurring
// under several differently-worded headings used to fragment into several
// 1-appearance, never-eligible "arcs", one per distinct slug; grouping by
// identity clusters every keyed appearance into the SAME arc regardless of
// how its heading was worded that run. A pre-key story (no digest carries a
// `key` for it yet) groups exactly as before — its identity is still its
// slug — so this is additive, not a behavior change for existing data.
//
// Eligibility mirrors renderArcs' own "recurring" threshold (count >= 2) —
// a single appearance in the window is a mention, not an arc. Ranking is
// appearances in the trailing 72h (desc), then most recent appearance
// (desc), then identity (asc) as the deterministic tiebreak two arcs can
// otherwise share on both count and recency.
//
// Momentum reuses computeArcMomentum VERBATIM (§11.1 PR A) on each arc's
// own appearances-in-window — same 48h/96h-vs-`nowMs` computation an arc
// page itself uses, just fed a different (still ASC-ordered-by-construction)
// appearances array. Its null/dormant case is expected to be rare here (an
// arc scoring >0 in the trailing 72h always has recent activity) but NOT
// impossible: an eligible arc can still rank into the top 5 by recency
// alone with zero 72h appearances and both its 48h/96h windows empty (e.g.
// its two appearances both fall between 4 and 7 days ago) — renderNowSection
// handles that by simply omitting the arrow, the same fail-safe contract
// renderArcPage's own momentumSegment already uses.
function computeNowArcs(rows, nowMs) {
  const trailing72hStart = nowMs - 72 * 3600000;
  const byIdentity = new Map();
  for (const row of rows) {
    if (!row.identity) continue;
    let arc = byIdentity.get(row.identity);
    if (!arc) {
      arc = { identity: row.identity, label: row.label, appearances: [], recent72h: 0 };
      byIdentity.set(row.identity, arc);
    }
    // rows arrive created_at ASC (see the query's ORDER BY in
    // handleIndexPage) — each successive row's label overwrites the last,
    // so by the time the scan finishes `label` holds the MOST RECENT
    // appearance's label, matching renderArcPage's own "latest label wins"
    // choice (see the comment on its `title` there).
    arc.label = row.label;
    arc.appearances.push({ created_at: row.created_at });
    if (new Date(row.created_at).getTime() >= trailing72hStart) arc.recent72h += 1;
  }

  const eligible = Array.from(byIdentity.values()).filter((arc) => arc.appearances.length >= 2);

  eligible.sort((a, b) => {
    if (b.recent72h !== a.recent72h) return b.recent72h - a.recent72h;
    const aLast = a.appearances[a.appearances.length - 1].created_at;
    const bLast = b.appearances[b.appearances.length - 1].created_at;
    if (aLast !== bLast) return aLast > bLast ? -1 : 1;
    return a.identity < b.identity ? -1 : a.identity > b.identity ? 1 : 0;
  });

  return eligible.slice(0, 5).map((arc) => ({
    identity: arc.identity,
    label: arc.label,
    count: arc.appearances.length,
    lastSeen: arc.appearances[arc.appearances.length - 1].created_at,
    momentum: computeArcMomentum(arc.appearances, nowMs),
  }));
}

// NOW section (§11.1 PR B): the situational-overview block rendered at the
// top of renderIndexPage, above the ledger — see handleIndexPage for the
// current-week-all-view-only gate that decides whether `nowArcs` is ever
// non-empty. Absent entirely when nowArcs is empty (0 eligible arcs -> no
// section markup at all, not an empty-state message — the ledger below
// already covers "nothing to show").
//
// Each arc renders as ONE row, not a card (design guidance: no card soup) —
// a single link carrying the momentum arrow, the arc's own label
// (arcHref, §11.1 PR A), and a mono metadata tail. The metadata tail is the
// relative last-updated time ONLY (owner-requested index cleanup, dropped
// the "×{n} this week" appearance count this row used to lead with) —
// arcRepeat itself is untouched and stays in active use on the digest
// page's own arc chips (see renderArcs), this is just NOW's own metadata
// getting quieter. Reuses .archivelabel for the eyebrow, same mono-eyebrow
// recipe already shared by the arc timeline and archive-search labels (see
// there) rather than a fourth near-identical class.
// `searchHtml` (owner-requested placement): the search control rides in
// THIS section's head row, right-aligned — the same right edge the arc
// rows' own relative-time meta aligns to, so the icon reads as belonging
// to the top of the page rather than to the week rail below it. Empty
// string on any page where the caller placed the control elsewhere (see
// renderIndexPage: the week rail keeps it whenever this section is
// absent, so the control never disappears with the section).
function renderNowSection(nowArcs, strings, token, lang, nowMs) {
  if (!nowArcs || nowArcs.length === 0) return "";
  const rowsHtml = nowArcs
    .map((arc) => {
      // Defense in depth (see computeNowArcs's comment on momentum): omit
      // the arrow entirely on the null/dormant case rather than guessing —
      // same fail-safe contract as renderArcPage's own momentumSegment.
      const arrow =
        arc.momentum === "up" ? "↑" : arc.momentum === "down" ? "↓" : arc.momentum === "same" ? "→" : "";
      const meta = formatRelativeTime(new Date(arc.lastSeen), strings.locale, nowMs);
      // data-arc-slug / data-last-seen (§11.2): rendering attributes, not
      // server state — the catch-up banner's client script (the unread-fence
      // IIFE extension in pageChrome) reads these to compute M (arcs updated
      // since last visit) and to cross-reference the follow list's localStorage
      // slugs, the exact same "data-* carrier" contract data-created already
      // uses for .entry rows. The attribute NAME stays data-arc-slug (client
      // script/follow-list vocabulary, unchanged) but its VALUE is now arc
      // IDENTITY (arc.identity — key when present, else slug), the same value
      // arcHref links to and renderArcPage's own data-arc-slug carries — so a
      // NOW row and its arc page always agree on the follow-list's matching
      // key. arc.lastSeen is the same ISO UTC string used above for the
      // relative-time meta, so the lexicographic compare against lastVisit is
      // correct for the same reason data-created's is.
      return `<a class="nowrow" href="${arcHref(token, lang, arc.identity)}" data-arc-slug="${esc(arc.identity)}" data-last-seen="${esc(arc.lastSeen)}">${arrow ? `<span class="nowarrow" aria-hidden="true">${arrow}</span>` : ""}<span class="nowarclabel">${esc(arc.label)}</span><span class="nowmeta">${esc(meta)}</span></a>`;
    })
    .join("\n");
  return `<div class="now"><div class="archivelabel">${esc(strings.nowLabel)}</div><nav class="nowlist" aria-label="${esc(strings.nowLabel)}">${rowsHtml}</nav></div>\n`;
}

function renderIndexPage(
  rows,
  token,
  host,
  lang,
  view,
  countdownNewest = null,
  weekInfo = null,
  nowArcs = [],
  nowMs = null,
  showCatchup = false,
) {
  const strings = STRINGS[lang];
  const emptyMessage =
    view === "daily" ? strings.noDailyBriefs : view === "weekly" ? strings.noWeeklyBriefs : strings.noDigests;

  // Current-week-only features (roadmap 3 step 3): the lead card, pulse
  // strip, and prefetch hint below all imply "this is what's happening
  // right now" — a "Latest" card on an archive week would lie, so they're
  // gated on isCurrent, true on the (week-less) daily view and on the
  // CURRENT week of the all view, false on any archive week. handleIndexPage
  // applies the same gate to countdownNewest itself before it ever reaches
  // this function (see there), so no separate check is needed for that one.
  const isCurrent = weekInfo === null || weekInfo.isCurrentWeek;

  // Tracked outside the branch below so it's reachable for the prefetch
  // href (roadmap 2 step 7) further down — null on the empty-index path and
  // on an archive week (no lead there at all), same as everywhere else in
  // this function.
  let leadRow = null;
  let body;
  if (rows.length === 0) {
    body = `<p class="empty">${esc(emptyMessage)}</p>`;
  } else if (isCurrent) {
    // rows are ordered created_at DESC, so rows[0] is the newest digest in
    // this view — it renders as the lead card above the ledger and is
    // excluded from the grouped list below (no duplicate). groupByDay runs
    // on the remainder, so if the newest digest was that day's only entry,
    // no empty day header is left behind.
    const [lead, ...rest] = rows;
    leadRow = lead;
    const groups = groupByDay(rest, strings.locale);
    const ledger = groups
      .map(
        (group) => `<div class="dayhead">${esc(group.label)}</div>
${group.items.map((row) => renderIndexEntry(row, token, lang, view)).join("\n")}`,
      )
      .join("\n");
    body = `${renderLeadCard(lead, token, lang, view)}\n${ledger}`;
  } else {
    // Archive week (roadmap 3 step 3): no lead card — every row, including
    // rows[0], goes through the plain day-grouped ledger, same as the
    // "rest" branch above minus the exclusion.
    const groups = groupByDay(rows, strings.locale);
    body = groups
      .map(
        (group) => `<div class="dayhead">${esc(group.label)}</div>
${group.items.map((row) => renderIndexEntry(row, token, lang, view)).join("\n")}`,
      )
      .join("\n");
  }

  // Unified search (owner UX pass): the container the bottom script's
  // archive-search IIFE fills with fragments fetched from the search route
  // (see handleSearchPage's fragment=1 branch and renderSearchFragment).
  // `hidden` by default, same "no-JS/pre-fetch default state" contract as
  // the filter input above — un-hidden only once a fetch actually returns
  // results. data-search-href carries the token/lang-scoped search route so
  // the script itself stays token/lang-agnostic, same pattern as every
  // other data-* hook on this page (data-created, data-unread-label, …).
  const archiveResultsHtml = `<div class="archiveresults" data-search-href="${searchHref(token, lang)}" hidden></div>`;

  // Week rail (roadmap 3 step 2): between the view tabs (rendered by
  // pageChrome, just above this) and the ledger — ALL-view index pages only
  // (weekInfo is null for the daily/weekly views, see handleIndexPage). Sits
  // above the empty-state message too, since both live inside the <section>
  // wrapper assembled below. Renders on the current week too (unlike the
  // lead/prefetch below) — the rail IS the archive navigation, so it stays
  // regardless of isCurrent.
  //
  // Archive sparkline (roadmap 4 step 6): `rows` is only handed to the rail
  // on an archive week (isCurrent false) — the current week's rail stays
  // sparkline-less (null), the same "nothing to show yet, the ledger below
  // is the current week's own record" posture the day-pulse strip this
  // sparkline was originally paired against used to carry (that strip was
  // removed in the owner-requested index cleanup; the current-week rail was
  // deliberately left as-is rather than backfilling a sparkline onto it —
  // out of scope for that pass).
  // Owner-requested placement: whenever the NOW section renders, IT carries
  // the search control (in its own head row, right-aligned) and the rail
  // below goes without — exactly one search control per page, always.
  const railHtml =
    view === "all" && weekInfo
      ? renderWeekRail(token, lang, weekInfo, strings, isCurrent ? null : rows)
      : "";

  // Search row (owner-requested index cleanup): the daily/weekly views have
  // no week rail to carry the search bubble (see railHtml just above), so
  // they get a minimal standalone row instead — same renderSearchBubble
  // markup, just without the week-nav spans around it, in the same
  // between-tabs-and-ledger slot the old filterrow occupied. Skipped
  // whenever railHtml is truthy (the ALL view) so the bubble never renders
  // twice on the same page. This keeps the ⌘K palette's "Search" command
  // (which reads `.searchlink` off the DOM, see collectPaletteItems)
  // available on every view, not just ALL — losing it silently on daily/
  // weekly would have been a real regression, not just a cosmetic one.

  // data-week-archive (roadmap 3 step 3): marks the <section> on any
  // non-current week so the bottom script's unread-fence IIFE can bail out
  // entirely — see that script for why an archive page must never draw a
  // fence or advance the lastVisit stamp.
  const archiveAttr = isCurrent ? "" : ' data-week-archive="1"';

  // data-unread-label (roadmap 2 step 2): the unread-fence label text,
  // rendered server-side so the bottom script that builds the fence stays
  // language-agnostic — it just reads this attribute rather than knowing
  // about STRINGS/lang itself.
  //
  // data-empty-filtered (roadmap 2 step 5): same pattern, for the
  // client-only "nothing matches the active filter(s)" message the bottom
  // script creates lazily — see applyFilters.
  //
  // Prefetch hint (roadmap 2 step 7): only when a lead exists, i.e. a
  // non-empty CURRENT-week index — see pageChrome's prefetchHref param
  // comment for the full mechanism/rationale, and isCurrent above for why
  // an archive week never has a leadRow to prefetch in the first place.
  const prefetchHref = leadRow ? digestHref(token, lang, view, leadRow.id) : null;

  // NOW section (§11.1 PR B): the very top of the page, above the rail/
  // filter row/pulse strip/ledger — a situational overview, not part of the
  // chronological archive chrome below it. `nowArcs` is only ever non-empty
  // when handleIndexPage ran the query (current-week all view — see there);
  // renderNowSection itself also fails safe to "" on an empty array, so this
  // stays a no-op on every other view/week without a second gate here.
  const nowHtml = renderNowSection(
    nowArcs,
    strings,
    token,
    lang,
    nowMs,
  );

  // Catch-up banner (§11.2): a hidden shell, same "data-* carrier" contract
  // as countdownHtml/paletteConfigHtml/the filter input above — no content
  // rendered server-side (the reader's lastVisit timestamp never leaves
  // their browser, so N/M can only ever be computed client-side), just the
  // i18n templates the bottom script's unread-fence IIFE extension needs to
  // stay language-agnostic. `hidden` by default: a no-JS reader, a first-
  // ever visit (no lastVisit yet), and every non-eligible page (showCatchup
  // false, so this whole const is "") all see nothing, same "inert until JS
  // proves it's warranted" contract as every other progressive-enhancement
  // shell in this file. Positioned as the very first element of the body
  // markup — above nowHtml — so it always renders "one row above the NOW
  // section" per the §11.2 spec, whether or not the NOW section itself has
  // any content that day (0-eligible-arcs still leaves this shell in place).
  const catchupHtml = showCatchup
    ? `<div class="catchup" hidden data-prefix="${esc(strings.catchupPrefix)}" data-tmpl-briefings="${esc(strings.catchupBriefings)}" data-tmpl-briefings-one="${esc(strings.catchupBriefingsOne)}" data-tmpl-arcs="${esc(strings.catchupArcUpdates)}" data-tmpl-arcs-one="${esc(strings.catchupArcUpdatesOne)}" data-tmpl-more="${esc(strings.catchupArcMore)}"><span class="catchuptext"></span><button type="button" class="catchupjump" hidden aria-label="${esc(strings.catchupJumpLabel)}">↓</button><button type="button" class="catchupdismiss" aria-label="${esc(strings.catchupDismissLabel)}">×</button></div>`
    : "";

  return pageChrome(
    host,
    token,
    lang,
    view,
    renderSwitchers(token, lang, view, "index", undefined, isCurrent ? null : weekInfo, true),
    `${catchupHtml}${nowHtml}${railHtml}<section data-unread-label="${esc(strings.unreadFence)}" data-empty-filtered="${esc(strings.emptyFiltered)}"${archiveAttr}>${body}</section>${archiveResultsHtml}`,
    null,
    countdownNewest,
    prefetchHref,
  );
}

// Section index (TOC, roadmap step 5): scan the article body actually being
// shown (EN or HU — the caller passes whichever articleHtml it already
// resolved) for bare, attribute-free `<h2>title</h2>` headings. nh3 strips
// attributes upstream, so a real section heading arrives in exactly that
// shape; an h2 with any attribute or nested tag doesn't match `[^<]*` and is
// silently skipped — failing safe on an unexpected shape rather than
// guessing at it.
//
// The `.attention` callout's own h2 must never become a TOC entry: split the
// html on that div first (non-greedy — the div isn't expected to nest) and
// only scan/inject the segments outside it, passing the div's own segment
// through byte-for-byte. String.prototype.split with a capturing group
// interleaves the captured separator back into the result at the odd
// indices, so those are exactly the attention-div segments to skip.
//
// ids are "s1".."sN", sequential and positional across the whole article
// (numbering continues across any attention-div gap) — the id VALUE is
// generated by us, never derived from heading text, so no sanitization
// question arises for the id itself; the heading text stays untouched other
// than gaining the id attribute.
function buildSectionToc(articleHtml) {
  const parts = articleHtml.split(/(<div class="attention">[\s\S]*?<\/div>)/);
  const sections = [];
  let n = 0;
  const html = parts
    .map((part, i) => {
      if (i % 2 === 1) return part; // attention-div segment: pass through
      return part.replace(/<h2>([^<]*)<\/h2>/g, (_match, title) => {
        n += 1;
        const id = `s${n}`;
        sections.push({ id, title: title.trim() });
        return `<h2 id="${id}">${title}</h2>`;
      });
    })
    .join("");
  return { html, sections };
}

// Zero or one section needs no index — a one-section brief has nothing to
// jump between, so skip the nav entirely rather than render a single
// pointless chip. Title text is passed through esc() — it originated from
// pre-sanitized HTML, but re-escaping text content read out of it is free
// safety, not redundant trust.
function renderToc(sections) {
  if (sections.length < 2) return "";
  const chips = sections
    .map((s) => `<a href="#${s.id}">${esc(s.title)}</a>`)
    .join("\n");
  return `<nav class="toc">${chips}</nav>\n`;
}

// Citation-chip hover titles (roadmap step 7 — polish): a bare `<a
// class="cite" href="...">¹</a>` chip tells the reader "there's a citation
// here" but not where it goes. Add a `title` naming the destination
// hostname so hover answers that without a click. Separate pass from
// buildSectionToc (one job each), run right after it in renderDigestPage —
// not folded into that function even though both walk the same articleHtml.
//
// The regex matches the exact shape body_html's citation chips arrive in
// (nh3-sanitized upstream, hrefs are exact copies of allowlisted item
// URLs); anything that doesn't match that shape is left alone. `new URL()`
// inside try/catch fails safe: an unparseable href leaves the tag
// untouched rather than emitting a broken title. The hostname is esc()'d
// before insertion — it derives from an attribute nh3 already sanitized as
// a URL, but escaping again is free. A leading "www." is stripped for a
// slightly cleaner display string.
function addCiteTitles(html) {
  return html.replace(/<a class="cite" href="([^"]*)">/g, (match, href) => {
    let hostname;
    try {
      hostname = new URL(href).hostname;
    } catch {
      return match; // unparseable href: leave the tag untouched, fail safe
    }
    const display = hostname.replace(/^www\./, "");
    return `<a class="cite" href="${href}" title="${esc(display)}">`;
  });
}

// Remove every style="…" attribute from the article html — see the call
// site in renderDigestPage for the full story (email-oriented inline styles
// fighting the site's theme CSS, and breaking downstream exact-shape
// regexes). Runs on nh3-normalized + emailer-generated markup only, where
// attributes are guaranteed double-quoted.
function stripInlineStyles(html) {
  return html.replace(/ style="[^"]*"/g, "");
}

// Story-arc line (ingest v3, roadmap 4 step 8): the digest page's per-topic
// thread summary, built from handleDigestPage's topicArcs — null (no topics
// on this digest, query never ran) or an empty array both render "", same
// absent-data contract as renderDegradedBadge/renderSourceKey. A topic with
// count 1 (seen only in this digest) renders as the bare label; count >= 2
// appends the "×N this week" suffix via strings.arcRepeat's template
// replace — see the STRINGS comment for why that's a whole-string template,
// not hand-composed pieces.
//
// Each recurring chip is now a LINK to that slug's arc page (§11.1 PR A) —
// `token`/`lang` are needed for arcHref, alongside the strings this function
// already took. Each topic entry carries its own `slug` since handleDigestPage
// started threading it through (see there) specifically so this could link.
function renderArcs(topicArcs, strings, token, lang) {
  if (!topicArcs || topicArcs.length === 0) return "";
  // RECURRING topics only (count >= 2) — this is what the roadmap 4 step 8
  // spec always said ("topics that ALSO appeared in the prior 7 days"), and
  // the first live digest showed why (owner-reported, 2026-08-09): topics
  // derive from section headings, so a single-appearance topic's chip is a
  // shouted duplicate of the TOC chip right below it. A digest whose topics
  // are all first appearances gets no arc line at all — nothing is
  // continuing, so there is no thread to point at.
  const recurring = topicArcs.filter(({ count }) => count >= 2);
  if (recurring.length === 0) return "";
  // href targets arc IDENTITY (key when present, else slug — see
  // arcIdentity), never the raw per-digest slug: the chip must link to the
  // SAME arc page every appearance of this story links to, regardless of
  // how this digest's own heading happened to be worded.
  const chips = recurring
    .map(
      ({ identity, label, count }) =>
        `<a class="arc" href="${arcHref(token, lang, identity)}"><span class="arclabel">${esc(label)}</span><span class="arccount">${esc(strings.arcRepeat.replace("{n}", String(count)))}</span></a>`,
    )
    .join("");
  return `<nav class="arcs" aria-label="${esc(strings.arcsLabel)}">${chips}</nav>\n`;
}

// "What changed" block (§11.3 delta persistence, ingest v4): the digest
// page's per-arc previously/now lines, rendered directly below the
// story-arc chip line (renderArcs, just above) and above the TOC — see the
// design guidance's scanning principle ("communicate what changed since the
// reader last looked, don't re-summarize the world every time"). `deltas`
// is handleDigestPage's parseDeltas output (null, or a non-empty array —
// never []); `topicArcs` is the SAME array renderArcs already received,
// which carries {slug, label, count} for EVERY topic on this digest, not
// just the recurring ones renderArcs itself filters down to — reused here
// purely for slug -> label lookup, no extra query.
//
// A delta whose slug has no matching topicArcs entry renders with the raw
// slug as fail-safe last-resort text rather than being dropped or throwing.
// This is a real possibility, not just defensive paranoia: topics and
// deltas are validated as two INDEPENDENT optional fields at ingest time
// (see validateDigestPayload), so a payload could legitimately send deltas
// without topics (or a differently-shaped topics list), and an older stored
// row can predate one field while already carrying the other. Same
// "something imperfect beats nothing/an error" posture as
// findArcSectionAnchor's null-anchor fallback elsewhere in this file.
// Delta prose is English-only today: §11.3 stores exactly ONE `deltas`
// payload, extracted from the English window digest by the app's
// extract_deltas choke point BEFORE translation ever runs, so no Hungarian
// variant of these sentences exists anywhere. Rendering it on a /hu/ page
// produced a Hungarian heading ("MI VÁLTOZOTT") wrapped around entirely
// English sentences — owner-reported, and worse than showing nothing.
// Suppressed there instead, which costs the HU reader nothing substantive:
// the same delta-only stories are already written as prose in the
// Hungarian body right below (the app writes them that way; the fenced
// block is a convenience layer over information the body already carries).
//
// THE single hook for the eventual `deltas_hu` payload (ingest v5): widen
// this to "true when text exists in `lang`", and pass that text through at
// the two call sites below (renderDeltas here, renderArcAppearance's own
// deltaHtml on the arc page). Both consult this, so neither can be
// forgotten.
function deltasRenderableIn(lang) {
  return lang === "en";
}

function renderDeltas(deltas, topicArcs, strings, token, lang) {
  if (!deltas || deltas.length === 0 || !deltasRenderableIn(lang)) return "";
  const labelBySlug = new Map((topicArcs ?? []).map((t) => [t.slug, t.label]));
  const rows = deltas
    .map(
      (d) => `<a class="delta" href="${arcHref(token, lang, d.slug)}">
    <span class="deltalabel">${esc(labelBySlug.get(d.slug) ?? d.slug)}</span>
    <p class="deltatext"><span class="deltaprev">${esc(d.previously)}</span><span class="deltaarrow">→</span><span class="deltanow">${esc(d.now)}</span></p>
  </a>`,
    )
    .join("");
  return `<div class="archivelabel">${esc(strings.whatChangedLabel)}</div><nav class="deltas" aria-label="${esc(strings.whatChangedLabel)}">${rows}</nav>\n`;
}

function renderDigestPage(digest, older, newer, token, host, lang, view, topicArcs, deltas) {
  const strings = STRINGS[lang];
  const date = new Date(digest.created_at);
  // "digest" itself stays an untranslated literal (see the STRINGS comment
  // above) — only the daily-brief/weekly-brief labels are real HU
  // vocabulary, swapped in for kind="daily"/kind="weekly" respectively.
  const kindLabel =
    digest.kind === "daily"
      ? strings.dailyBrief
      : digest.kind === "weekly"
        ? strings.weeklyBrief
        : "digest";
  const stamp = `${formatDayHeader(date, strings.locale)} · ${formatTime(date, strings.locale)} ${tzAbbr(date)} · ${kindLabel} #${digest.id}`;
  // <title>: shorter than the stamp (formatShortDate, not formatDayHeader) —
  // browser tab/history width is tight, and the token never appears here.
  // pageChrome esc()s the whole composed string before inserting it.
  const pageTitle = `${kindLabel} #${digest.id} · ${formatShortDate(date, strings.locale)} ${formatTime(date, strings.locale)}`;

  const navLinks = [
    `<a href="${indexHref(token, lang, view)}">${esc(strings.allDigests)}</a>`,
    '<span class="spacer"></span>',
  ];
  // Hide the link entirely at each end (oldest has no older, newest has no
  // newer) rather than showing a disabled placeholder.
  if (older) {
    navLinks.push(
      `<a class="nav-older" href="${digestHref(token, lang, view, older.id)}">← ${esc(formatTime(new Date(older.created_at), strings.locale))}</a>`,
    );
  }
  if (newer) {
    navLinks.push(
      `<a class="nav-newer" href="${digestHref(token, lang, view, newer.id)}">${esc(formatTime(new Date(newer.created_at), strings.locale))} →</a>`,
    );
  }

  // HU page: prefer the translated body; if the app never sent one for this
  // digest, fall back to the English body_html and say so above the article
  // rather than silently presenting untranslated content on a HU URL.
  let articleHtml = digest.body_html;
  let enOnlyNoteHtml = "";
  if (lang === "hu") {
    if (digest.body_html_hu) {
      articleHtml = digest.body_html_hu;
    } else {
      enOnlyNoteHtml = `<p class="en-only-note">${esc(strings.enOnlyNote)}</p>`;
    }
  }

  // Strip the emailer's inline styles FIRST (owner-reported, 2026-08-09):
  // the app renders ONE body_html for both the email and this site, and the
  // email half bakes light-theme colors in as style="" attributes (mail
  // clients can't do stylesheets). Served verbatim here, those attributes
  // BEAT the site's class rules — in dark mode the TL;DR card stayed light
  // and its citation chips went white-on-white. The classes (.tldr,
  // .attention, .banner, .cite, .tldr-label, …) arrive alongside the
  // styles, so stripping the attributes hands presentation fully to the
  // site's own themed CSS. Order matters: the email's cite pills are
  // `<a class="cite" style="…" href="…">`, and addCiteTitles' exact-shape
  // regex below never matched that — hover domains (and the print
  // stylesheet's cite[title] domains) were silently missing on real
  // digests; stripping first restores the exact shape every downstream
  // pass expects. The regex is safe here because this markup is
  // nh3-normalized + emailer-generated: attributes are always
  // double-quoted, never single-quoted or bare.
  const articleHtmlThemed = stripInlineStyles(articleHtml);

  // TOC ids are injected into the themed html (see buildSectionToc), so
  // the <article> below renders the id-bearing version, not the original.
  const { html: articleHtmlWithIds, sections } = buildSectionToc(articleHtmlThemed);
  const tocHtml = renderToc(sections);

  // Separate pass, one job each (see addCiteTitles): citation chips gain a
  // hover title naming their destination hostname.
  const articleHtmlFinal = addCiteTitles(articleHtmlWithIds);

  // Same links, top and bottom: after an ~900-word read the natural gesture
  // is older/next, not scroll-to-top (roadmap step 2) — mirror the nav below
  // the article rather than making the reader travel back to the header.
  // Built once here, wrapped twice below; the bottom copy carries the extra
  // digestnav-bottom class (own CSS: top hairline + spacing, same as
  // .closing, since it follows the article's closing line).
  const digestNavLinksHtml = navLinks.join("\n");

  // Source key (roadmap 2 step 8 follow-up): the article's colophon, same
  // fail-safe absent-data contract as renderDegradedBadge — renders "" on an
  // older digest with no source_counts/failed_sources.
  const sourceKeyHtml = renderSourceKey(digest.source_counts, digest.failed_sources, strings);

  // Story-arc line (roadmap 4 step 8): renders "" on a digest with no topics
  // — see renderArcs and the topicArcs computation in handleDigestPage. Each
  // recurring chip links to that slug's arc page (§11.1 PR A).
  const arcsHtml = renderArcs(topicArcs, strings, token, lang);

  // "What changed" block (§11.3 delta persistence, ingest v4): renders "" on
  // a digest with no deltas — see renderDeltas.
  const deltasHtml = renderDeltas(deltas, topicArcs, strings, token, lang);

  // Order: stamp -> arc line -> what-changed block -> en-only note -> TOC ->
  // article. The TOC can't sit inside the TL;DR-bearing article start as
  // first imagined — the TL;DR callout is itself inside body_html — so it
  // renders above <article> instead.
  const body = `<nav class="digestnav">${digestNavLinksHtml}</nav>
<p class="stamp">${esc(stamp)}</p>
${arcsHtml}${deltasHtml}${enOnlyNoteHtml}${tocHtml}<article class="digest">
${articleHtmlFinal}
</article>
${sourceKeyHtml}<nav class="digestnav digestnav-bottom">${digestNavLinksHtml}</nav>
<a class="backfab" href="${indexHref(token, lang, view)}" aria-label="${esc(strings.backFabLabel)}">←</a>`;

  return pageChrome(
    host,
    token,
    lang,
    view,
    renderSwitchers(token, lang, view, "digest", digest.id),
    body,
    pageTitle,
  );
}

// Momentum indicator (§11.1 guardrail: frequency-derived only, NEVER
// severity vocabulary — "↑ more coverage", never "↑ escalating") —
// appearances in the trailing 48h vs. the 48h before that, anchored at the
// CURRENT instant (`nowMs`), not at any one digest's own created_at. This is
// deliberately different from handleDigestPage's per-digest 7-day arc-line
// count just above, which anchors at that digest's own created_at so an old
// digest's arc line stays reproducible history forever (see the comment
// there) — an arc PAGE is a live view of "where does this story stand right
// now", so "now" is the correct anchor here, and this page's momentum is
// expected to change on every visit as time passes, unlike the arc line.
//
// Both windows empty -> null, and the indicator is OMITTED from the page
// (see renderArcPage): a dormant arc has no frequency signal to report, and
// "less coverage" against a prior window that was also silent would be a
// claim the data doesn't make (design guidance: labels state only what's
// provable). With any activity in either window, "recent === 0 -> down" is
// checked BEFORE the recent-vs-prior comparison so a slug that just went
// quiet always reads as declining coverage, never as "steady".
function computeArcMomentum(appearances, nowMs) {
  const windowStart = nowMs - 48 * 3600000;
  const priorWindowStart = nowMs - 96 * 3600000;
  let recent = 0;
  let prior = 0;
  for (const row of appearances) {
    const t = new Date(row.created_at).getTime();
    if (t >= windowStart && t < nowMs) recent += 1;
    else if (t >= priorWindowStart && t < windowStart) prior += 1;
  }
  if (recent === 0 && prior === 0) return null;
  if (recent === 0) return "down";
  if (recent > prior) return "up";
  if (recent === prior) return "same";
  return "down";
}

// "Last updated" relative label (arc pages only). Intl.RelativeTimeFormat
// formats ONE unit at a time — it doesn't pick the unit for you — so the
// diff is bucketed by hand into minutes/hours/days, the smallest unit that
// keeps the magnitude under 60/24 respectively; numeric:"auto" lets a locale
// with an idiom for it (e.g. "yesterday") use it instead of a bare count.
// `nowMs` is threaded in from the caller (handleArcPage's `Date.now()`)
// rather than read here, same "inject the current instant" convention
// isoWeekOf/handleIndexPage already use elsewhere in this file — keeps this
// function a pure, stub-testable computation.
function formatRelativeTime(date, locale, nowMs) {
  const diffMs = date.getTime() - nowMs; // negative here: always a past appearance
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const minutes = Math.round(diffMs / 60000);
  if (Math.abs(minutes) < 60) return rtf.format(minutes, "minute");
  const hours = Math.round(diffMs / 3600000);
  if (Math.abs(hours) < 24) return rtf.format(hours, "hour");
  const days = Math.round(diffMs / 86400000);
  return rtf.format(days, "day");
}

// One appearance in the arc timeline — reuses the index ledger's own
// `.entry`/`.meta`/`.time`/`.excerpt` vocabulary (same "a link to one
// digest" shape as renderSearchResult) rather than inventing a parallel
// style. `.excerpt` here holds this APPEARANCE's own label for the arc (that
// digest's own heading text, at the time it was published), not a TL;DR —
// deliberately no "TL;DR:" prefix, unlike renderIndexEntry's. Always the
// all-view digest href: an arc spans every kind, so an appearance has no
// daily/weekly-view address of its own, same reasoning as
// renderSearchResult's. `row.anchor` (from findArcSectionAnchor, computed
// once in handleArcPage) is appended as a #sN fragment only when it
// resolved to exactly one section — see that function's comment for the
// fail-safe contract; a null anchor here means a plain link to the digest
// page, never a guessed fragment.
function renderArcAppearance(row, token, lang) {
  const strings = STRINGS[lang];
  const time = formatTime(new Date(row.created_at), strings.locale);
  // "all": appearances span every kind/view, same reasoning as
  // renderSearchResult's kindBadge call — always show the daily/weekly
  // badge, never suppress it the way a same-kind VIEW page would.
  const badgeHtml = kindBadge(row, "all", strings);
  const href = digestHref(token, lang, "all", row.id) + (row.anchor ? `#${row.anchor}` : "");
  // Delta line (§11.3 delta persistence, ingest v4): `row.delta` is
  // handleArcPage's per-appearance match, already narrowed to this arc's own
  // slug — null on the common case (no delta for this appearance), which
  // renders "", i.e. this appearance looks exactly as it did before this
  // feature. Shares the .deltatext/.deltaprev/.deltaarrow/.deltanow classes
  // with the digest page's own "What changed" block (renderDeltas) — same
  // quiet typographic treatment, no separate label span here since the
  // appearance's own `.excerpt` line right above already names the story.
  // deltasRenderableIn: English-only text, suppressed on /hu/ pages — see
  // that function's comment for why and for the deltas_hu hook.
  const deltaHtml =
    row.delta && deltasRenderableIn(lang)
      ? `<p class="deltatext"><span class="deltaprev">${esc(row.delta.previously)}</span><span class="deltaarrow">→</span><span class="deltanow">${esc(row.delta.now)}</span></p>`
      : "";
  return `<a class="entry" href="${href}" data-created="${esc(row.created_at)}">
    <span class="meta"><span class="time">${esc(time)}</span>${badgeHtml}</span>
    <p class="excerpt">${esc(row.label)}</p>
    ${deltaHtml}
  </a>`;
}

// Arc page (§11.1 PR A): `appearances` is handleArcPage's array, ordered
// created_at ASC (oldest first) — first/latest below rely on that order, so
// it's reversed ONLY for the timeline's own display (see the comment at that
// call site). `nowMs` is the request-time instant handleArcPage captured
// once and threads through unchanged, so momentum/relative-time computed
// here can never observe two different "now"s within one render. `identity`
// (stable arc keys) is whatever route segment resolved this page — a `key`
// or, for a pre-key arc, a plain `slug` — carried through opaquely as the
// arc's own address: the data-arc-slug attribute name and the follow-list's
// "slug" vocabulary below are UNCHANGED (client-side string matching, see
// the follow-toggle IIFE in pageChrome), only the value they now carry can
// be a key instead of a slug.
function renderArcPage(identity, appearances, token, host, lang, nowMs) {
  const strings = STRINGS[lang];
  const first = appearances[0];
  const latest = appearances[appearances.length - 1];

  // null on a dormant arc (both 48h windows empty) — the segment is left
  // off the metadata line entirely rather than rendered as a guess; see
  // computeArcMomentum's comment.
  const momentum = computeArcMomentum(appearances, nowMs);
  const momentumSegment =
    momentum === null
      ? null
      : momentum === "up"
        ? `↑ ${strings.arcMomentumUp}`
        : momentum === "down"
          ? `↓ ${strings.arcMomentumDown}`
          : `→ ${strings.arcMomentumSame}`;

  // One mono metadata line, reusing the digest page's own `.stamp` styling
  // (same "wire dateline" register — total appearances, first-seen date,
  // last-updated relative time, momentum when present) rather than a new
  // CSS class.
  const metaLine = [
    (appearances.length === 1 ? strings.arcAppearancesOne : strings.arcAppearances).replace(
      "{n}",
      String(appearances.length),
    ),
    strings.arcFirstSeen.replace("{date}", formatShortDate(new Date(first.created_at), strings.locale)),
    strings.arcUpdated.replace("{t}", formatRelativeTime(new Date(latest.created_at), strings.locale, nowMs)),
    momentumSegment,
  ]
    .filter(Boolean)
    .join(" · ");

  // Title (H1, the site's first — every other page uses the masthead brand
  // link instead): the arc's MOST RECENT label, not the first — labels can
  // drift across digests as headings get rephrased run to run, and the
  // latest phrasing is the freshest editorial framing of the story.
  const title = latest.label;

  // Timeline, newest-day-first (matches the index ledger's own newest-first
  // convention — groupByDay just buckets consecutive same-day rows in
  // whatever order it's handed, so the DESC order has to come from the
  // caller, same as handleIndexPage's own `created_at DESC` query does for
  // the ledger). `appearances` itself stays ASC (oldest first) throughout
  // this function — only this local copy is reversed, for display only.
  const groups = groupByDay(appearances.slice().reverse(), strings.locale);
  const timelineHtml = groups
    .map(
      (group) => `<div class="dayhead">${esc(group.label)}</div>
${group.items.map((row) => renderArcAppearance(row, token, lang)).join("\n")}`,
    )
    .join("\n");

  // .archead (§11.2, optional "follow list" feature): a flex row wrapping
  // the h1 so the bottom script can inject a text-control follow toggle
  // "next to the arc title" per the spec, without a card/button — see the
  // follow-toggle IIFE in pageChrome. data-arc-slug/data-follow-add/
  // data-follow-remove are the same "data-* carrier" contract as every other
  // client-read attribute in this file (data-created, data-unread-label,
  // paletteconfig, …): the toggle itself is entirely client-side (a
  // localStorage array of followed slugs, never sent here), so this is the
  // only place slug/i18n strings meet the DOM for it to read.
  const body = `<nav class="digestnav"><a href="${indexHref(token, lang, "all")}">${esc(strings.allDigests)}</a></nav>
<div class="archead"><h1 class="arctitle" data-arc-slug="${esc(identity)}" data-follow-add="${esc(strings.followAdd)}" data-follow-remove="${esc(strings.followRemove)}">${esc(title)}</h1></div>
<p class="stamp">${esc(metaLine)}</p>
<div class="archivelabel">${esc(strings.arcTimelineLabel)}</div>
${timelineHtml}`;

  // view "all": an arc has no view of its own (see the file-header comment)
  // — the view tabs/brand link just need SOME valid view to target, same
  // reasoning as renderSearchPage's own "all" choice.
  return pageChrome(
    host,
    token,
    lang,
    "all",
    renderSwitchers(token, lang, "all", "arc", identity),
    body,
    title,
  );
}

// Search (roadmap 4 step 7): snip/snip_hu are excerpts of body_md/body_md_hu
// — untrusted markdown, never HTML (see the file-header comment: only
// body_html/body_html_hu are pre-sanitized and skip esc()). The SQL query in
// handleSearchPage wraps each matched fragment in CHAR(1)/CHAR(2) sentinel
// bytes rather than real <mark> tags SPECIFICALLY so this function can
// escape the whole snippet FIRST — turning any HTML-like text that happens
// to appear in the matched markdown into inert entities — and only THEN
// replace the (esc()-untouched, since esc() doesn't rewrite control bytes)
// sentinels with the real <mark>/</mark> tags. Escape-then-mark, never
// mark-then-escape: doing it the other way round would esc() the <mark>
// tags themselves right back into visible text.
function markSnippet(rawSnippet) {
  return esc(rawSnippet).replaceAll("\x01", "<mark>").replaceAll("\x02", "</mark>");
}

// HU page: prefer the translated snippet; if it's empty (untranslated
// digest, so body_md_hu was NULL and snippet() returned an empty string) or
// simply absent, fall back to the English snippet and flag it — same
// fallback contract as renderExcerpt's index-ledger "EN" chip, just for
// search results instead of TL;DR excerpts.
function renderSnippet(row, lang) {
  if (lang === "hu" && row.snip_hu) {
    return { html: markSnippet(row.snip_hu), usedHu: true };
  }
  return { html: markSnippet(row.snip ?? ""), usedHu: false };
}

// One search result: reuses the index ledger's `.entry`/`.meta`/`.excerpt`
// vocabulary (renderIndexEntry) rather than inventing a parallel result
// style — a search hit and a ledger row are the same kind of thing, a link
// to one digest. Always the all-view digest href (digestHref(..., "all",
// ...)): search spans every kind, so a result has no "daily view" address
// of its own to link into, same reasoning as the searchHref/view split
// throughout this feature.
function renderSearchResult(row, token, lang) {
  const strings = STRINGS[lang];
  const date = new Date(row.created_at);
  const dateLabel = `${formatShortDate(date, strings.locale)} ${formatTime(date, strings.locale)}`;
  // Search results are always in "all"-view address space (see the function
  // comment above), which is also the exact view kindBadge needs to decide
  // the daily-view redundancy rule — reused as-is rather than duplicating
  // the daily/weekly badge logic here.
  const badgeHtml = kindBadge(row, "all", strings);

  const { html: snippetHtml, usedHu } = renderSnippet(row, lang);
  const langChip = lang === "hu" && !usedHu ? '<span class="flag flag-muted">EN</span>' : "";

  // data-id (unified search, owner UX pass): lets the index page's
  // archive-results script drop fragment entries already visible in the
  // rendered ledger above it — see renderIndexEntry/renderLeadCard, which
  // carry the same attribute for exactly this comparison, and the bottom
  // script's archive-search IIFE that reads it.
  return `<a class="entry" href="${digestHref(token, lang, "all", row.id)}" data-id="${esc(row.id)}">
    <span class="meta"><span class="time">${esc(dateLabel)}</span>${badgeHtml}${langChip}</span>
    <p class="excerpt">${snippetHtml}</p>
  </a>`;
}

// Fragment mode (unified search, owner UX pass): what handleSearchPage's
// ?fragment=1 branch returns — bare results only, no pageChrome, no form.
// Built entirely from the SAME server-side renderers as the full search
// page (renderSearchResult -> markSnippet's escape-then-mark, esc()
// everywhere), so every escaping guarantee documented there is inherited
// unchanged; nothing here bypasses it. This is what makes the client's
// innerHTML injection of this fragment (see the bottom script) safe: it is
// our own server-rendered, fully-escaped HTML from the same origin and
// token path. This function must NEVER be handed anything that hasn't gone
// through esc() first — notably, `q` itself is deliberately not echoed back
// into this fragment (unlike the full search page's form, which must echo
// it into the input's value) for exactly that reason.
function renderSearchFragment(results, token, lang) {
  if (results.length === 0) return "";
  const strings = STRINGS[lang];
  const items = results.map((row) => renderSearchResult(row, token, lang)).join("\n");
  return `<div class="archivelabel">${esc(strings.archiveResults)}</div>\n${items}`;
}

// `results` is null when no search was attempted yet (empty ?q=, see
// handleSearchPage) — renders just the form, no count line, no no-results
// message. Non-null (possibly empty, including the try/catch error-fallback
// case in handleSearchPage) means a search WAS attempted, so the count/
// no-results line always renders.
function renderSearchPage(results, q, token, host, lang) {
  const strings = STRINGS[lang];

  // No-JS baseline: a plain GET form, submitting back to this exact route
  // with ?q= as the query string — works with JS entirely off. Reuses the
  // `.filter` input's look (see the CSS) but, unlike the index page's own
  // `.filter` input, is NOT hidden: that one is a client-side-only
  // enhancement with "show everything" as its server fallback, while this
  // input IS the server-functional control itself — hiding it would leave
  // no-JS readers with no way to search at all.
  const formHtml = `<form class="searchform" method="get" action="${searchHref(token, lang)}">
    <input class="filter" type="search" name="q" value="${esc(q)}" placeholder="${esc(strings.searchPlaceholder)}" aria-label="${esc(strings.searchPlaceholder)}">
    <button class="searchbtn" type="submit">${esc(strings.searchButton)}</button>
  </form>`;

  let resultsHtml = "";
  if (results !== null) {
    if (results.length === 0) {
      resultsHtml = `<p class="empty">${esc(strings.searchNone)}</p>`;
    } else {
      // Count line reuses the existing `.empty` muted-metadata style — same
      // "borrow the closest existing thing" approach as the rest of this
      // feature, rather than adding a new CSS class for one line of text.
      const countLabel = (results.length === 1 ? strings.searchResultsOne : strings.searchResults).replace(
        "{n}",
        String(results.length),
      );
      const items = results.map((row) => renderSearchResult(row, token, lang)).join("\n");
      resultsHtml = `<p class="empty">${esc(countLabel)}</p>\n${items}`;
    }
  }

  // Switchers: pageKind "index" (not a dedicated "search" kind) — the
  // language switch on this page goes to the OTHER language's root index,
  // not to that language's own search results for the same query. Losing
  // the query string on a language hop is an accepted, deliberate
  // simplification (threading `q` through renderLangSwitcher's index-href
  // helpers isn't worth it for a corner every other switcher on this site
  // already treats as "go to that language's home").
  //
  // `view` "all": search has no daily/week variant of its own (see the
  // file-header comment and searchHref), so the view tabs/brand link just
  // need SOME valid view to render against, and "all" is the closest
  // meaning — clicking "Daily" from here goes to the daily index, not to a
  // (nonexistent) daily-scoped search.
  return pageChrome(
    host,
    token,
    lang,
    "all",
    renderSwitchers(token, lang, "all", "index"),
    `${formHtml}${resultsHtml}`,
    strings.searchLabel,
  );
}
