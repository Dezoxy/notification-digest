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
 * the standalone /t/:token/(hu/)?search endpoint — the language segment
 * always comes first, then either an optional literal "daily/" or "weekly/"
 * view segment or the literal "search" endpoint (search has no daily/
 * weekly/week variant of its own — it spans the whole archive, not one
 * view). No view segment is the ALL view (every digest, mixed).
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
 *   anything else                 -> plain 404, wrong token included
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
         (id, created_at, tldr, item_count, section_count, has_attention, body_html, body_md, tldr_hu, body_html_hu, body_md_hu, kind, source_counts, failed_sources, topics)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
         topics         = excluded.topics`,
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

  return htmlResponse(
    renderIndexPage(
      results ?? [],
      token,
      url.hostname,
      lang,
      view,
      countdownNewest,
      weekInfo,
    ),
  );
}

async function handleDigestPage(env, token, idParam, url, lang, view) {
  if (!(await tokenMatches(env, token))) return notFound();

  const id = Number(idParam);
  if (!Number.isInteger(id) || id <= 0) return notFound();

  const digest = await env.DB.prepare(
    "SELECT id, created_at, tldr, item_count, section_count, has_attention, body_html, body_html_hu, kind, source_counts, failed_sources, topics FROM digests WHERE id = ?",
  )
    .bind(id)
    .first();
  if (!digest) return notFound();

  // Story-arc counts (ingest v3, roadmap 4 step 8): fail-safe parse, same
  // contract as renderSpectrum — an unparseable or wrong-shaped topics value
  // is treated as "no topics" rather than thrown.
  let topics = null;
  if (digest.topics) {
    try {
      const parsed = JSON.parse(digest.topics);
      if (Array.isArray(parsed)) {
        // Per-entry shape check too, not just "is an array" — same defense
        // against a stored value predating a validation change that every
        // other renderer here applies (renderSpectrum, renderSourceKey); a
        // wrong-shaped entry must drop out, not render "undefined".
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
    // count per slug (a slug set, not a running tally), regardless of how
    // many times that slug might otherwise appear.
    const priorSlugSets = priorRows
      .map((row) => {
        try {
          const parsed = JSON.parse(row.topics);
          if (!Array.isArray(parsed)) return null;
          return new Set(
            parsed.map((t) => t?.slug).filter((slug) => typeof slug === "string"),
          );
        } catch {
          return null;
        }
      })
      .filter((set) => set !== null);

    // count = prior occurrences + 1, i.e. total appearances including this
    // digest itself — a topic seen only here renders as count 1 (bare label,
    // see renderArcs).
    topicArcs = topics.map((t) => ({
      label: t.label,
      count: priorSlugSets.filter((set) => set.has(t.slug)).length + 1,
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

  return htmlResponse(
    renderDigestPage(digest, older, newer, token, url.hostname, lang, view, topicArcs),
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

// topics (ingest v3, roadmap 4 step 8): absent/null is valid (nothing
// reported, stored NULL). Present, it must be a JSON array (not an object —
// Array.isArray, not typeof, same reasoning as source_counts' inverse
// check), at most MAX_TOPICS entries, each entry a plain object (not an
// array, not null — typeof null === "object" too) with EXACTLY two keys:
// slug (matching TOPIC_SLUG_RE) and label (a string whose trimmed length is
// 1..80 — the trimmed form is what gets stored, same "store the normalized
// value" contract as everywhere else in this validator). Duplicate slugs
// within one payload are a caller bug, not something to silently dedupe.
// Same empty-array-normalizes-to-null reasoning as failed_sources — "no
// topics reported" and "an old app that doesn't send this field" would
// otherwise render identically, so storing "[]" is a distinction without a
// difference.
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
    const { slug, label, ...rest } = entry;
    if (Object.keys(rest).length > 0) {
      return { ok: false, error: "each topics entry must have exactly slug and label" };
    }
    if (typeof slug !== "string" || !TOPIC_SLUG_RE.test(slug)) {
      return { ok: false, error: `topics has an invalid slug: "${slug}"` };
    }
    if (typeof label !== "string" || label.trim().length < 1 || label.trim().length > 80) {
      return { ok: false, error: `topics["${slug}"].label must be 1-80 characters` };
    }
    if (seenSlugs.has(slug)) {
      return { ok: false, error: `topics has a duplicate slug: "${slug}"` };
    }
    seenSlugs.add(slug);
    normalized.push({ slug, label: label.trim() });
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
    pulseLabel: "Recent volume",
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
    searchNone: "Nothing found.",
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
    pulseLabel: "Friss mennyiség",
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
    searchNone: "Nincs találat a keresésre.",
    // Owner: please review — new HU string, mirrors searchLabel's pattern.
    archiveResults: "Az archívumból",
    // Story arcs (roadmap 4 step 8) — owner: please review these, flagged HU
    // strings same as everywhere else in this file.
    arcsLabel: "Történetszálak",
    arcRepeat: "×{n} ezen a héten",
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
  const enHref = pageKind === "index" ? otherLangIndexHref("en") : digestHref(token, "en", view, id);
  const huHref = pageKind === "index" ? otherLangIndexHref("hu") : digestHref(token, "hu", view, id);
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
// rendered as centered pill tabs on their own row below the masthead, not
// as a corner micro-link like the language toggle. Active tab = filled
// accent pill (plain text, not a link); inactive = outlined link. On a
// digest page the inactive tab targets that view's INDEX (a window digest
// has no address in the daily or weekly view — long-standing design
// choice).
// Unlike the language switcher, this deliberately does NOT thread a week
// through (roadmap 3 step 3): a week page's tabs still target the view's
// root index with no week segment — a week page has no daily or weekly
// twin to keep the week address for, so there's nothing to preserve here.
function renderViewTabs(token, lang, view) {
  const strings = STRINGS[lang];
  const tab = (v, label) =>
    view === v
      ? `<span class="viewtab active">${esc(label)}</span>`
      : `<a class="viewtab" href="${indexHref(token, lang, v)}">${esc(label)}</a>`;
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
function renderSwitchers(token, lang, view, pageKind, id, archiveWeek = null) {
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
  return `<details class="settings"><summary class="gear" aria-label="${esc(strings.settingsLabel)}">⚙<span class="gearlabel">${esc(strings.settingsLabel)}</span></summary><div class="settingspanel">${langRow}${themeRow}${sizeRow}${densityRow}</div></details>`;
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

  header.mast {
    display: flex; align-items: baseline; justify-content: space-between;
    flex-wrap: wrap; gap: 0.6em 1em; padding: 1.4em 0 1em;
    border-bottom: 1px solid var(--hairline); margin-bottom: 1.6em;
  }
  .mast .brand { font-weight: 700; font-size: 1.05em; letter-spacing: -0.01em; text-decoration: none; color: var(--text); }
  .mast .brand .tld { color: var(--accent); }
  /* The settings gear (language/theme/size/density, collapsed into one
     details.settings disclosure — see renderSwitchers) sits top-right in
     the masthead via .mastright, right-aligned — same markup at both
     breakpoints. */
  .mast .mastright { display: flex; flex-direction: column; align-items: flex-end; gap: 0.2em; }

  /* Mobile masthead + phone font size (owner-tuned). */
  @media (max-width: 40em) {
    /* 15px: phone type ran large even at 16 (owner feedback, twice). The
       S/M/L text-size rules need no phone twin — they're em factors off
       this base, see the data-fontsize block above. */
    body { font-size: 15px; }
    /* Icon-only gear on the phone — see .gearlabel above. */
    .gearlabel { display: none; }
    /* Brand left, EN|HU right on one line (owner: the selector belongs
       on the right; the cadence line was removed entirely at the owner's
       request — the footer already carries the private-link warning). */
    header.mast {
      display: flex; flex-wrap: wrap; justify-content: space-between;
      align-items: baseline;
    }
    .mast .mastright { display: contents; }
  }
  .mast .langswitch, .mast .viewswitch { font-size: 0.85em; font-variant-numeric: tabular-nums; }
  .mast .langswitch a, .mast .viewswitch a { text-decoration: none; }
  .mast .langswitch strong, .mast .viewswitch strong { color: var(--text); }

  /* Settings bubble (owner redesign): the gear button collapses language,
     theme, and density into one disclosure. details.settings — NOT
     .mastright — is the positioning anchor for .settingspanel below:
     .mastright goes display: contents in the ≤40em mobile block just
     below, which erases it as a box entirely, so anything anchored to it
     would have nowhere to be absolute relative to. The <details> element
     itself survives that collapse and stays a real box, so it's the anchor. */
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
  /* Source-spectrum micro-bar (roadmap 2 step 8, renderSpectrum): fixed
     width so the meta row's layout doesn't jump depending on how many
     sources reported this run; segments are sized purely by each <i>'s own
     inline flex:N (N = that source's item count). */
  .spectrum { display: inline-flex; width: 3.2em; height: 6px; border-radius: 3px; overflow: hidden; gap: 0; align-self: center; }
  .spectrum i { display: block; height: 100%; }
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
     voice, same family as .sourcekey's .sk swatches below — these are
     labels, not navigation, so unlike .toc's accent link chips they're not
     links (the site has no topic pages, yet; the arc line is provenance,
     stating which threads this briefing continues). */
  .arcs { display: flex; flex-wrap: wrap; gap: 0.45em; margin: 0 0 1.2em; }
  .arcs .arc {
    font-family: var(--font-data); font-size: 0.72em; text-transform: uppercase;
    letter-spacing: 0.06em; padding: 0.22em 0.8em; border-radius: 999px;
    background: var(--chip-bg); color: var(--chip-text);
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
     the same source_counts/failed_sources data the index's .spectrum bar
     summarizes, spelled out as concrete per-source numbers with color
     swatches, sitting right after the article (renderSourceKey renders
     nothing when both fields are absent — see the function for the
     fail-safe JSON.parse contract shared with renderSpectrum/
     renderDegradedBadge). */
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

  /* View tabs: the primary content navigation, centered on its own row.
     Bigger than the corner language toggle by design — switching between
     the full stream and daily briefs is the main choice a reader makes.
     Owner-requested 2026-08-09: one connected segmented capsule instead of
     three detached pills. The capsule (.viewtabs) carries the border,
     radius and overflow: hidden; segments (.viewtab) are borderless and
     share a hairline divider. No wrapper element — width: fit-content plus
     auto side margins centers the capsule on its own row the same way the
     old flex+justify-content did. The view selector remains the primary
     navigation. */
  .viewtabs {
    display: flex; width: fit-content; margin: 0.2em auto 1.7em;
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
     newer → nav, between the view tabs and the filter row, ALL-view index
     pages only (see renderIndexPage/renderWeekRail). Classic 3-column
     centering trick: the two OUTER spans share flex:1 (so they're always
     equal width regardless of their own content length, even when one side
     is an empty spacer), which keeps the center label visually centered
     without needing to measure anything. */
  .weekrail {
    display: flex; align-items: baseline; margin: 0 0 1.2em;
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

  /* Index filter (roadmap step 6): tucks under the view tabs — negative
     top margin pulls it snug against .viewtabs' own bottom margin instead
     of stacking two gaps. hidden by default (see renderIndexPage), so
     this rule only ever paints once JS un-hides the input. */
  .filterrow { display: flex; gap: 0.5em; margin: -0.6em 0 1.4em; }
  .filterrow .filter {
    display: block; flex: 1; min-width: 0; font: inherit; font-size: 0.9em;
    padding: 0.5em 0.9em; border-radius: 10px;
    border: 1px solid var(--hairline); background: var(--bg); color: var(--text);
  }
  .filterrow .filter::placeholder { color: var(--muted); }
  /* Plain border otherwise; only :focus-visible gets a visible outline. */
  .filterrow .filter:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  /* Entry point into search (roadmap 4 step 7, renderIndexPage's
     filterRowHtml): a small uppercase mono link, NOT styled like the
     filter input beside it — it's chrome/navigation, not a data field, so
     it takes the mono chrome voice already used for eyebrows/dateline
     labels elsewhere on this site. */
  .searchlink {
    font-family: var(--font-data); font-size: 0.78em; align-self: center;
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

  /* Day-pulse strip (roadmap 2 step 3, renderPulseStrip): ambient chrome, not
     a chart with axes — no numbers, no gridlines, no day-boundary markers on
     purpose, just relative bar heights with a title-attribute tooltip per
     bar. Anchor (.pulsebar) is a fixed-height flex box so the span inside
     can be anchored to its bottom via align-items: flex-end and sized purely
     by its own height percentage. */
  .pulse { display: flex; align-items: flex-end; gap: 3px; height: 34px; margin: 0 0 1.6em; }
  .pulsebar { flex: 1 1 0; height: 100%; display: flex; align-items: flex-end; }
  .pulsebar span { display: block; width: 100%; background: var(--chip-bg); border-radius: 2px 2px 0 0; }
  .pulsebar.now span { background: var(--accent); }
  .pulsebar:hover span { background: var(--accent); }
  .pulsebar:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }

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
    .filterrow, .miniseg, .densitytoggle, .pulse, .resumechip,
    .archiveresults {
      display: none;
    }
    .digest, .digest p, .digest h2, .stamp, .dayhead, .empty, .en-only-note {
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
    <a class="brand" href="${indexHref(token, lang, view)}">${esc(first)}<span class="tld">${esc(rest)}</span></a>
    <div class="mastright">
      ${switchersHtml}
    </div>
  </header>
  ${viewTabsHtml}
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
      // (see renderIndexPage) — once the enhanced archive box is wired up,
      // hide it. Lives in the same .filterrow, so a null guard costs
      // nothing even though this code path only runs where it's known to
      // exist.
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

  // Keyboard navigation (roadmap 2 step 4): desktop convenience, no visible
  // UI hint — the nav arrows already show the model. j/ArrowLeft hop to the
  // OLDER digest, k/ArrowRight to the NEWER one, via the stable nav-older/
  // nav-newer classes renderDigestPage puts on both the top and bottom
  // digestnav (index pages have neither, so this silently no-ops there).
  // "/" focuses the index filter, when one exists on the page. j = older =
  // down-the-archive, matching the index's newest-first reading order
  // (vim-scroll intuition); the arrow keys mirror the nav's own visual
  // ← older / newer → arrows. Never intercepts typing: bails on any
  // input/textarea/select/contentEditable target, and on any ctrl/meta/alt
  // modifier so browser shortcuts stay untouched.
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
      } else if (e.key === "k" || e.key === "ArrowRight") {
        var newer = document.querySelector(".nav-newer");
        if (newer) newer.click();
      } else if (e.key === "/") {
        var filter = document.querySelector(".filter");
        if (filter) {
          e.preventDefault();
          filter.focus();
        }
      }
    }, { signal: signal });
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

    function doSoftNav(href, isPopstate) {
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
            // Scroll restoration is deliberately simple: both a forward
            // soft-nav and a popstate soft-load land at the top. Safari's
            // BFCache already resolves many real "back" cases before
            // popstate ever fires; this covers what's left without trying
            // to remember and restore scroll positions.
            window.scrollTo(0, 0);
          });
          // Committed: this address is now what's on screen. Set for BOTH
          // directions (forward soft-nav and popstate soft-load), and only
          // once the swap has actually happened — an aborted or superseded
          // nav returns above and must never move this.
          lastRendered = href;
          if (!isPopstate) history.pushState({ soft: true }, "", href);
        })
        .catch(function (err) {
          if (err && err.name === "AbortError") return; // superseded fetch, silent
          // Fetch failure, non-ok status, or any exception during the swap
          // itself: fall back to the ordinary navigation the reader always
          // had. Correctness never depends on the soft path working.
          location.href = href;
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
        doSoftNav(dest.pathname + dest.search, false);
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
      doSoftNav(here, true);
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

// Source-spectrum micro-bar (roadmap 2 step 8): one <i> per source with
// inline style="flex:N" (N = that source's item count) inside a fixed-width
// flex container, so the segments lay out proportionally without any JS —
// see the .spectrum/.spectrum i CSS. sourceCountsJson is the raw D1 TEXT
// column (JSON string, or null); JSON.parse is wrapped in try/catch and an
// unparseable or wrong-shaped value renders nothing rather than throwing —
// this Worker already validated the shape at ingest time, but rendering
// stays defensive against a stored value that predates a validation change
// or was written some other way. Sources with a zero count are skipped
// entirely (nothing to draw); the whole span is omitted if nothing is left.
function renderSpectrum(sourceCountsJson) {
  if (!sourceCountsJson) return "";
  let counts;
  try {
    counts = JSON.parse(sourceCountsJson);
  } catch {
    return "";
  }
  if (typeof counts !== "object" || counts === null || Array.isArray(counts)) return "";
  // Descending by count for both the visual stacking order and the title
  // attribute's "telegram 40 · x 12 · …" listing.
  const entries = Object.entries(counts)
    .filter(([, n]) => typeof n === "number" && n > 0)
    .sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return "";
  const bars = entries
    .map(([name, n]) => `<i style="flex:${n};background:${SOURCE_COLORS[name] ?? SOURCE_COLOR_FALLBACK}"></i>`)
    .join("");
  const title = entries.map(([name, n]) => `${name} ${n}`).join(" · ");
  return `<span class="spectrum" title="${esc(title)}">${bars}</span>`;
}

// Degraded-run badge (roadmap 2 step 8): shown when failed_sources parses to
// a non-empty array — same fail-safe JSON.parse contract as renderSpectrum
// above, and for the same reason (defense against a stored value that
// predates a validation change). Reuses the existing .flag pill shape, but
// the muted .flag-muted colors rather than the amber attention ones — this
// is a fact about a collection run, not something that needs the reader's
// attention the way has_attention does — so the ⚠ prefix, not color, is what
// marks it. `strings` is the caller's STRINGS[lang] (for the localized
// "partial"/"hiányos" label); the failed source names themselves stay
// untranslated in the title, same as source_counts' names in renderSpectrum.
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

// Source key (digest-page colophon, roadmap 2 step 8 follow-up): the digest
// page's spelled-out counterpart to the index's .spectrum micro-bar —
// concrete per-source numbers with the same color swatches, plus any failed
// sources from a partial run. Same fail-safe JSON.parse contract as
// renderSpectrum/renderDegradedBadge above (unparseable or wrong-shaped ->
// treated as absent, never thrown); renders nothing at all when both fields
// are absent/empty, so an old digest predating this data shows no key.
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

  // Descending by count, same ordering as renderSpectrum's bar/title.
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

  // Source-spectrum micro-bar + degraded-run badge (roadmap 2 step 8): both
  // render "" when the row has no data for them (older digests, or an app
  // version that doesn't send it yet) — see renderSpectrum/renderDegradedBadge.
  const spectrumHtml = renderSpectrum(row.source_counts);
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
    <span class="meta"><span class="${timeClass}">${esc(time)}</span><span class="count">${counts}</span>${spectrumHtml}${degradedHtml}${badgeHtml}${langChip}</span>
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

  // Source-spectrum micro-bar + degraded-run badge: same contract as
  // renderIndexEntry's — see comments there.
  const spectrumHtml = renderSpectrum(row.source_counts);
  const degradedHtml = renderDegradedBadge(row.failed_sources, strings);

  // data-created / data-id: same contract as renderIndexEntry's — see
  // comments there.
  return `<a class="entry entry-lead" href="${digestHref(token, lang, view, row.id)}" data-created="${esc(row.created_at)}" data-id="${esc(row.id)}">
    <span class="meta"><span class="eyebrow-text">${esc(eyebrow)}</span>${spectrumHtml}${degradedHtml}${badgeHtml}${langChip}</span>
    <p class="excerpt"><strong>${esc(strings.tldrLabel)}</strong> ${excerptHtml}</p>
  </a>`;
}

// Day-pulse strip (roadmap 2 step 3): a micro bar chart of recent news
// volume, rendered server-side from data the index query already returns —
// the day's pulse readable before a word is read. ALL view only: the daily
// view's one-brief-per-day cadence has no intra-day pulse to show, so this
// renders nothing there (the daily-brief digests themselves are also
// excluded from the bars below, for the same reason — a weekly brief rides
// the same kind !== "window" exclusion, it re-synthesizes the week's items
// rather than reporting a fresh count of its own). No day-boundary markers
// are drawn — deliberate, the strip is a pulse, not a calendar.
function renderPulseStrip(rows, token, lang, view) {
  if (view !== "all") return "";

  const strings = STRINGS[lang];
  // rows arrive created_at DESC (newest first, see handleIndexPage) — take
  // the most recent 16 window digests, then reverse so time reads
  // left-to-right: oldest of the 16 on the left, newest on the right.
  const windowRows = rows
    .filter((row) => row.kind === "window")
    .slice(0, 16)
    .reverse();
  if (windowRows.length < 2) return ""; // a one-bar chart is noise

  // Heights normalize against the max item_count in the shown set, with a
  // floor so a low-volume window's bar stays visible/tappable rather than
  // collapsing to nothing.
  const max = Math.max(...windowRows.map((row) => row.item_count));

  const bars = windowRows
    .map((row, i) => {
      const date = new Date(row.created_at);
      const time = formatTime(date, strings.locale);
      const pct = max > 0 ? Math.max(8, Math.round((row.item_count / max) * 100)) : 8;
      // Rightmost bar (last after the reverse above) is the newest digest.
      const nowClass = i === windowRows.length - 1 ? " now" : "";
      const title = `${time} · ${row.item_count} ${strings.itemsWord}`;
      return `<a class="pulsebar${nowClass}" href="${digestHref(token, lang, view, row.id)}" title="${esc(title)}"><span style="height:${pct}%"></span></a>`;
    })
    .join("\n");

  return `<nav class="pulse" aria-label="${esc(strings.pulseLabel)}">${bars}</nav>\n`;
}

// Week rail (roadmap 3 step 2): mono wire-style `← W31 · WEEK 32 · 3–9 AUG ·
// W33 →` nav, rendered on ALL-view index pages only — see the call site in
// renderIndexPage, and the file-header roadmap notes on why the daily view
// has no week address. `weekInfo` is handleIndexPage's { year, week,
// isCurrentWeek, older, newer } (older/newer are {year,week} or null — see
// that function). Absent older/newer render as empty (but still flex:1)
// spacer spans, via the shared .rail-older/.rail-newer classes, so the
// center label stays visually centered either way (see the .weekrail CSS).
//
// Archive sparkline (roadmap 4 step 6): `rows` is null on every page except
// an archive week (see the call site in renderIndexPage, which passes null
// on the current week so its rail stays byte-identical to before this
// step) — the current week already has the pulse strip for this job, and
// showing both would say the same thing twice.
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
  // the function comment above. Same Budapest day-bucketing as
  // renderPulseStrip: window digests only (kind === "window" — a daily brief
  // re-synthesizes the same day's items, so counting it too would double the
  // day), summed by budapestDateParts key, then read back per day of the
  // week's own Mon..Sun span off a FRESH per-day proxy copy each iteration —
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

function renderIndexPage(rows, token, host, lang, view, countdownNewest = null, weekInfo = null) {
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

  // Client-side filter (roadmap step 6, index pages only): sits between the
  // view tabs (rendered by pageChrome, just above this) and the lead card
  // (the first thing inside <section> below). `hidden` by default — no JS,
  // no filter UI — un-hidden by the bottom script in pageChrome.
  //
  // Search link (roadmap 4 step 7): rides in the same row, right after the
  // filter input, but is NOT hidden — it's a server-rendered affordance to
  // the full-archive search page, not a client-side enhancement. With JS
  // off the filter input stays hidden and this link is the row's only
  // visible content, which is what finally gives a no-JS reader a working
  // way to search at all.
  const filterRowHtml = `<div class="filterrow"><input class="filter" type="search" placeholder="${esc(strings.filterPlaceholder)}" aria-label="${esc(strings.filterPlaceholder)}" hidden><a class="searchlink" href="${searchHref(token, lang)}">${esc(strings.searchLink)}</a></div>`;

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
  // pageChrome, just above this) and the filter row — ALL-view index pages
  // only (weekInfo is null for the daily view, see handleIndexPage). Sits
  // above the empty-state message too, since both live inside the <section>
  // wrapper assembled below. Renders on the current week too (unlike the
  // lead/pulse/prefetch below) — the rail IS the archive navigation, so it
  // stays regardless of isCurrent.
  //
  // Archive sparkline (roadmap 4 step 6): `rows` is only handed to the rail
  // on an archive week (isCurrent false) — the current week keeps the pulse
  // strip below for this job, so passing rows there too would render the
  // same volume shape twice. null keeps the current-week rail byte-identical
  // to before this step.
  const railHtml =
    view === "all" && weekInfo ? renderWeekRail(token, lang, weekInfo, strings, isCurrent ? null : rows) : "";

  // Day-pulse strip (roadmap 2 step 3): between the filter row and the
  // <section> below, i.e. right above the lead card — CURRENT-WEEK-ONLY as
  // of roadmap 3 step 3 (a pulse of "recent volume" on an archive week
  // would be showing volume from years ago, framed as if it were recent);
  // renderPulseStrip's own all-view-only/too-few-bars checks still apply on
  // top of this gate.
  const pulseHtml = isCurrent ? renderPulseStrip(rows, token, lang, view) : "";

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

  return pageChrome(
    host,
    token,
    lang,
    view,
    renderSwitchers(token, lang, view, "index", undefined, isCurrent ? null : weekInfo),
    `${railHtml}${filterRowHtml}${pulseHtml}<section data-unread-label="${esc(strings.unreadFence)}" data-empty-filtered="${esc(strings.emptyFiltered)}"${archiveAttr}>${body}</section>${archiveResultsHtml}`,
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
// absent-data contract as renderSpectrum/renderSourceKey. A topic with
// count 1 (seen only in this digest) renders as the bare label; count >= 2
// appends the "×N this week" suffix via strings.arcRepeat's template
// replace — see the STRINGS comment for why that's a whole-string template,
// not hand-composed pieces.
function renderArcs(topicArcs, strings) {
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
  const chips = recurring
    .map(
      ({ label, count }) =>
        `<span class="arc"><span class="arclabel">${esc(label)}</span><span class="arccount">${esc(strings.arcRepeat.replace("{n}", String(count)))}</span></span>`,
    )
    .join("");
  return `<nav class="arcs" aria-label="${esc(strings.arcsLabel)}">${chips}</nav>\n`;
}

function renderDigestPage(digest, older, newer, token, host, lang, view, topicArcs) {
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
  // fail-safe absent-data contract as renderSpectrum/renderDegradedBadge —
  // renders "" on an older digest with no source_counts/failed_sources.
  const sourceKeyHtml = renderSourceKey(digest.source_counts, digest.failed_sources, strings);

  // Story-arc line (roadmap 4 step 8): renders "" on a digest with no topics
  // — see renderArcs and the topicArcs computation in handleDigestPage.
  const arcsHtml = renderArcs(topicArcs, strings);

  // Order: stamp -> arc line -> en-only note -> TOC -> article. The TOC
  // can't sit inside the TL;DR-bearing article start as first imagined — the
  // TL;DR callout is itself inside body_html — so it renders above <article>
  // instead.
  const body = `<nav class="digestnav">${digestNavLinksHtml}</nav>
<p class="stamp">${esc(stamp)}</p>
${arcsHtml}${enOnlyNoteHtml}${tocHtml}<article class="digest">
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
      const countLabel = strings.searchResults.replace("{n}", String(results.length));
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
