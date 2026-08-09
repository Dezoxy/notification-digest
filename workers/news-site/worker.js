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
 * Routes: URL grammar is /t/:token/(hu/)?(daily/)?( | d/:id) — the language
 * segment always comes first, then an optional literal "daily/" view
 * segment. No "daily/" segment is the ALL view (every digest, mixed).
 *   GET  /robots.txt              -> disallow everything, no token needed
 *   PUT  /ingest/:id              -> upsert a digest (x-ingest-key required)
 *   GET  /t/:token/               -> index (EN, all view), newest-first, grouped by day
 *   GET  /t/:token/d/:id          -> single digest (EN, all view), with prev/next nav
 *   GET  /t/:token/daily/         -> same index, EN, filtered to kind='daily' only
 *   GET  /t/:token/daily/d/:id    -> same digest page, EN, prev/next stays within kind='daily'
 *   GET  /t/:token/hu/            -> same index, Hungarian chrome + translations, all view
 *   GET  /t/:token/hu/d/:id       -> same digest page, Hungarian chrome + translations, all view
 *   GET  /t/:token/hu/daily/      -> same index, Hungarian chrome, daily view
 *   GET  /t/:token/hu/daily/d/:id -> same digest page, Hungarian chrome, daily view
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
 * Daily-brief view (All | Daily switcher in the masthead, next to EN | HU):
 * every digest carries a `kind` column, 'window' (the regular 3-hourly
 * digest, the default) or 'daily' (the once-daily 20:00 synthesis). The
 * "daily/" URL segment filters the index to kind='daily' and constrains a
 * digest page's prev/next to kind='daily' too, so a reader in that view hops
 * brief-to-brief instead of through every window digest in between. Since a
 * window digest has no home in the daily view, the view switcher's "other
 * view" link ALWAYS points at that view's index, never at a digest page —
 * true on the index itself (index -> index, the obvious case) and also when
 * switching view away from a digest page (digest -> that view's index,
 * because the current digest may not exist in the target view).
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
    // and the optional "daily/" segment (only ever AFTER "hu/", never
    // before) selects the daily-brief-only view; everything else about the
    // route (token check, id shape, 404s) is identical across all four
    // language×view combinations — see handleIndexPage/handleDigestPage,
    // which take `lang` and `view` as plain parameters rather than being
    // duplicated four times.
    const digestMatch = path.match(/^\/t\/([^/]+)\/(hu\/)?(daily\/)?d\/(\d+)$/);
    if (digestMatch && request.method === "GET") {
      const lang = digestMatch[2] ? "hu" : "en";
      const view = digestMatch[3] ? "daily" : "all";
      return handleDigestPage(env, digestMatch[1], digestMatch[4], url, lang, view);
    }

    const indexMatch = path.match(/^\/t\/([^/]+)\/(hu\/)?(daily\/)?$/);
    if (indexMatch && request.method === "GET") {
      const lang = indexMatch[2] ? "hu" : "en";
      const view = indexMatch[3] ? "daily" : "all";
      return handleIndexPage(env, indexMatch[1], url, lang, view);
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
         (id, created_at, tldr, item_count, section_count, has_attention, body_html, body_md, tldr_hu, body_html_hu, body_md_hu, kind)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET
         created_at    = excluded.created_at,
         tldr          = excluded.tldr,
         item_count    = excluded.item_count,
         section_count = excluded.section_count,
         has_attention = excluded.has_attention,
         body_html     = excluded.body_html,
         body_md       = excluded.body_md,
         tldr_hu       = excluded.tldr_hu,
         body_html_hu  = excluded.body_html_hu,
         body_md_hu    = excluded.body_md_hu,
         kind          = excluded.kind`,
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
      )
      .run();
  } catch {
    return json({ error: "database error" }, 500);
  }

  return json({ ok: true }, 200);
}

async function handleIndexPage(env, token, url, lang, view) {
  if (!(await tokenMatches(env, token))) return notFound();

  // LIMIT 1000 = ~4 months of 3-hourly digests. Not pagination, a page-weight
  // backstop: every row carries a ~paragraph tldr, and an unbounded index
  // would grow by ~1MB/quarter forever. Older digests stay reachable through
  // each digest page's prev/next chain; add real pagination if the cap is
  // ever actually felt. tldr_hu is always selected (cheap) even for the EN
  // page — only the HU renderer reads it.
  //
  // Order by created_at, not id: daily briefs get BACKFILLED for past days,
  // so a backfilled row can have a high id but an old, historical
  // created_at — id order and chronological order are no longer the same
  // thing. `id DESC` stays only as a deterministic tiebreak for same-instant
  // rows. groupByDay below relies on this ordering to put each row in its
  // correct day bucket.
  const kindFilter = view === "daily" ? "WHERE kind = 'daily' " : "";
  const { results } = await env.DB.prepare(
    `SELECT id, created_at, tldr, tldr_hu, item_count, section_count, has_attention, kind FROM digests ${kindFilter}ORDER BY created_at DESC, id DESC LIMIT 1000`,
  ).all();

  return htmlResponse(
    renderIndexPage(results ?? [], token, url.hostname, lang, view),
  );
}

async function handleDigestPage(env, token, idParam, url, lang, view) {
  if (!(await tokenMatches(env, token))) return notFound();

  const id = Number(idParam);
  if (!Number.isInteger(id) || id <= 0) return notFound();

  const digest = await env.DB.prepare(
    "SELECT id, created_at, tldr, item_count, section_count, has_attention, body_html, body_html_hu, kind FROM digests WHERE id = ?",
  )
    .bind(id)
    .first();
  if (!digest) return notFound();

  // In the daily view, prev/next stay within kind='daily' so a reader hops
  // brief-to-brief rather than through every window digest in between — the
  // all view keeps today's unconstrained chronological prev/next.
  //
  // Neighbor = adjacent by (created_at, id) tuple order, not by id: daily
  // briefs get BACKFILLED for past days with historical created_at values,
  // so a backfilled row's id says nothing about its chronological position.
  // SQLite row-value comparison ((created_at, id) < (?, ?)) does the tuple
  // compare/tiebreak in one expression — supported since SQLite 3.15, and
  // D1's SQLite is far newer.
  const kindFilter = view === "daily" ? " AND kind = 'daily'" : "";
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
    renderDigestPage(digest, older, newer, token, url.hostname, lang, view),
  );
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
  // anything other than the two known values is a caller bug, not a value to
  // silently coerce.
  if (kind !== undefined && kind !== "window" && kind !== "daily") {
    return { ok: false, error: 'kind must be "window" or "daily"' };
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
    },
  };
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

function tzAbbr(date) {
  // Recent ICU versions render timeZoneName:"short" for Europe/* zones as a
  // GMT offset ("GMT+1"/"GMT+2") rather than "CET"/"CEST", so derive the
  // abbreviation from the offset ourselves instead of trusting that string.
  // Europe/Budapest only ever has these two offsets, so the mapping is exact.
  // This is a locale-independent numeric parse (not user-facing text), so it
  // stays on "en-GB" regardless of the page's language.
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: TIMEZONE,
    timeZoneName: "shortOffset",
  }).formatToParts(date);
  const offset = parts.find((p) => p.type === "timeZoneName")?.value ?? "";
  // Match "+2" AND "+02" (ICU emits "GMT+2" for shortOffset today, but a
  // runtime that ever hands back the padded "GMT+02:00" long form must not
  // silently fall through to CET in August — the exact bug this function
  // exists to avoid).
  const m = offset.match(/[+-]0?(\d)/);
  return m && m[1] === "2" ? "CEST" : "CET";
}

// ── chrome strings (EN|HU) ──────────────────────────────────────────────
//
// This is the ENTIRE Hungarian vocabulary this Worker knows — everything
// else Hungarian-language on a /hu/ page is either digest content the app
// already translated (body_html_hu/tldr_hu) or a handful of untranslated
// micro-labels ("digest #N") left as-is; the TL;DR excerpt label localizes
// via STRINGS.tldrLabel ("Röviden:"). See README/PR notes for
// the reasoning. dailyBrief is the one exception on the stamp line: for
// kind="daily" it replaces the untranslated "digest" label, so the HU stamp
// reads "napi összefoglaló #N" instead of "digest #N". Owner: please read
// these for correctness, they're the only hardcoded Hungarian text in the
// codebase.
const STRINGS = {
  en: {
    locale: "en-GB",
    attention: "needs attention",
    itemsWord: "items",
    sectionsWord: "sections",
    allDigests: "← All digests",
    noDigests: "No digests yet.",
    noDailyBriefs: "No daily briefs yet.",
    footerPrivate:
      "Private link — anyone with this URL can read. Don't share it outside the group.",
    footerNotIndexed: "Not indexed · generated by the digest service, every 3 hours",
    tldrLabel: "TL;DR:",
    backFabLabel: "Back to all digests",
    enOnlyNote: null,
    dailyBrief: "daily brief",
    viewAll: "All",
    viewDaily: "Daily",
  },
  hu: {
    locale: "hu-HU",
    attention: "figyelmet igényel",
    itemsWord: "elem",
    sectionsWord: "szakasz",
    allDigests: "← Minden hírlevél",
    noDigests: "Még nincs hírlevél.",
    noDailyBriefs: "Még nincs napi összefoglaló.",
    footerPrivate:
      "Privát link — bárki olvashatja, akinél megvan ez az URL. Ne oszd meg a csoporton kívül.",
    footerNotIndexed: "Nem indexelt · a digest szolgáltatás generálja, 3 óránként",
    tldrLabel: "Röviden:",
    backFabLabel: "Vissza a hírlevelekhez",
    enOnlyNote: "Csak angolul elérhető",
    dailyBrief: "napi összefoglaló",
    viewAll: "Minden",
    viewDaily: "Napi",
  },
};

// ── language/view-space path helpers (keep every internal link inside the
// current language×view space: index↔index, digest↔digest, EN pages never
// link into /hu/ and vice versa, all-view pages never link into daily/ and
// vice versa, except via the two explicit switchers) ────────────────────

function indexHref(token, lang, view) {
  const langSeg = lang === "hu" ? "hu/" : "";
  const viewSeg = view === "daily" ? "daily/" : "";
  return `/t/${encodeURIComponent(token)}/${langSeg}${viewSeg}`;
}

function digestHref(token, lang, view, id) {
  const langSeg = lang === "hu" ? "hu/" : "";
  const viewSeg = view === "daily" ? "daily/" : "";
  return `/t/${encodeURIComponent(token)}/${langSeg}${viewSeg}d/${esc(id)}`;
}

// `pageKind` ("index" | "digest") picks index vs. digest href — distinct
// from a digest row's own `kind` column (window/daily) used elsewhere.
function renderLangSwitcher(token, lang, view, pageKind, id) {
  const enHref = pageKind === "index" ? indexHref(token, "en", view) : digestHref(token, "en", view, id);
  const huHref = pageKind === "index" ? indexHref(token, "hu", view) : digestHref(token, "hu", view, id);
  // Current language: plain bold text, not a link (nothing to switch to).
  // Other language: a link to the SAME page (same index row / same digest
  // id) in the other language space, same view.
  const en = lang === "en" ? "<strong>EN</strong>" : `<a href="${enHref}">EN</a>`;
  const hu = lang === "hu" ? "<strong>HU</strong>" : `<a href="${huHref}">HU</a>`;
  return `<span class="langswitch">${en} | ${hu}</span>`;
}

// The view switcher's "other view" link is ALWAYS an index href, on both
// index and digest pages — see the file-header comment ("Daily-brief view")
// for why a digest page can't link into the other view's own digest.
// The view selector is the site's PRIMARY navigation (owner decision) —
// rendered as centered pill tabs on their own row below the masthead, not
// as a corner micro-link like the language toggle. Active tab = filled
// accent pill (plain text, not a link); inactive = outlined link. On a
// digest page the inactive tab targets that view's INDEX (a window digest
// has no address in the daily view — long-standing design choice).
function renderViewTabs(token, lang, view) {
  const strings = STRINGS[lang];
  const tab = (v, label) =>
    view === v
      ? `<span class="viewtab active">${esc(label)}</span>`
      : `<a class="viewtab" href="${indexHref(token, lang, v)}">${esc(label)}</a>`;
  return `<nav class="viewtabs">${tab("all", strings.viewAll)}${tab("daily", strings.viewDaily)}</nav>`;
}

// The masthead's right cluster now carries ONLY the language toggle (the
// view tabs moved to their own centered row — see renderViewTabs).
function renderSwitchers(token, lang, view, pageKind, id) {
  return `<div class="switchers">${renderLangSwitcher(token, lang, view, pageKind, id)}</div>`;
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

  * { box-sizing: border-box; }
  /* Reserve the scrollbar's gutter even when the page is too short to
     scroll: the All view scrolls, a near-empty Daily view doesn't, and
     without this the viewport width changes on switch — sliding the
     centered bubble sideways by half a scrollbar (owner-reported). A
     no-op on overlay-scrollbar platforms, which never had the shift. */
  html { scrollbar-gutter: stable; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    line-height: 1.6;
    font-size: 17px;
  }
  a { color: var(--accent); }
  .wrap { max-width: 42em; margin: 0 auto; padding: 0 1.25em 4em; }

  header.mast {
    display: flex; align-items: baseline; justify-content: space-between;
    flex-wrap: wrap; gap: 0.6em 1em; padding: 1.4em 0 1em;
    border-bottom: 1px solid var(--hairline); margin-bottom: 1.6em;
  }
  .mast .brand { font-weight: 700; font-size: 1.05em; letter-spacing: -0.01em; text-decoration: none; color: var(--text); }
  .mast .brand .tld { color: var(--accent); }
  /* Switchers (EN | HU, All | Daily) sit top-right in the masthead, paired
     on one row via .switchers,
     both right-aligned — same markup at both breakpoints, .switchers'
     own flex-wrap (not header.mast's) is what keeps two small switchers
     from crowding the brand on narrow viewports: they wrap onto their own
     line under mastright rather than squeezing the header itself. */
  .mast .mastright { display: flex; flex-direction: column; align-items: flex-end; gap: 0.2em; }

  /* Mobile masthead + phone font size (owner-tuned). */
  @media (max-width: 40em) {
    /* 15px: phone type ran large even at 16 (owner feedback, twice). */
    body { font-size: 15px; }
    /* Brand left, EN|HU right on one line (owner: the selector belongs
       on the right; the cadence line was removed entirely at the owner's
       request — the footer already carries the private-link warning). */
    header.mast {
      display: flex; flex-wrap: wrap; justify-content: space-between;
      align-items: baseline;
    }
    .mast .mastright { display: contents; }
  }
  .mast .switchers { display: flex; gap: 0.6em; align-items: baseline; flex-wrap: wrap; justify-content: flex-end; }
  .mast .langswitch, .mast .viewswitch { font-size: 0.85em; font-variant-numeric: tabular-nums; }
  .mast .langswitch a, .mast .viewswitch a { text-decoration: none; }
  .mast .langswitch strong, .mast .viewswitch strong { color: var(--text); }

  .dayhead {
    font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.09em;
    color: var(--muted); margin: 2.2em 0 0.4em; font-weight: 600;
    font-family: var(--font-data); /* mono uppercase eyebrow = the wire look */
  }
  .entry {
    display: block; text-decoration: none; color: inherit;
    padding: 1.05em 0; border-bottom: 1px solid var(--hairline);
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
  }
  /* Neutral/muted variant for the "EN" fallback chip on untranslated HU
     index entries — deliberately NOT the amber attention colors, this isn't
     a warning, just a language note. Reuses .flag's shape/sizing. */
  .entry .flag.flag-muted { background: var(--chip-bg); color: var(--chip-text); }
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
     are already fixed-width — tabular-nums would be redundant. */
  .stamp { color: var(--muted); font-size: 0.8em; margin: 0 0 1.2em; font-family: var(--font-data); }
  /* HU digest page, no body_html_hu on file: shown above the article,
     falling back to the English body. */
  .en-only-note { color: var(--muted); font-size: 0.85em; font-style: italic; margin: 0 0 1em; }

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
  /* Article h2s deliberately stay sans while the body below them goes serif
     (.digest p) — the newspaper pattern: sans heads announce, serif body
     reads. Not an omission. */
  .digest h2 {
    font-size: 1.15em; border-left: 3px solid var(--h2-border);
    padding-left: 0.6em; margin: 1.9em 0 0.6em; text-wrap: balance;
  }
  .digest p { margin: 0.7em 0; font-family: var(--font-prose); line-height: 1.65; }
  .cite {
    font-size: 0.7em; vertical-align: super; text-decoration: none;
    background: var(--chip-bg); color: var(--chip-text);
    padding: 0 0.4em; border-radius: 99px; font-weight: 700; margin-left: 1px;
    font-family: var(--font-data);
  }
  .closing {
    font-style: italic; color: var(--muted); border-top: 1px solid var(--hairline);
    padding-top: 1em; margin-top: 2.2em; font-size: 0.92em;
  }

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
     the full stream and daily briefs is the main choice a reader makes. */
  .viewtabs {
    display: flex; justify-content: center; gap: 0.6em;
    margin: 0.2em 0 1.7em;
  }
  .viewtab {
    padding: 0.42em 1.6em; border-radius: 999px;
    font-size: 0.95em; font-weight: 600; text-decoration: none;
    border: 1px solid var(--hairline); color: var(--accent);
  }
  .viewtab.active {
    background: var(--accent); color: var(--bg); border-color: var(--accent);
  }
  .viewtab:not(.active):hover { border-color: var(--accent); }
  .viewtab:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }

  footer.site {
    margin-top: 3.5em; padding-top: 1.2em; border-top: 1px solid var(--hairline);
    color: var(--muted); font-size: 0.8em;
  }
  footer.site p { margin: 0.3em 0; }

  /* Desktop: the content column becomes a rounded "bubble" card hugging the
     42em text measure, floating on a darker, purple-tinted page background.
     Mobile keeps the full-bleed layout above untouched — the card chrome
     only exists from 52em up. * { box-sizing: border-box } is set globally,
     so max-width 48em with 3em side padding keeps the text at the same
     42em measure it has on mobile. */
  @media (min-width: 52em) {
    body { background: var(--page-bg); padding: 2.5em 1.5em; }
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
`;

// `title` defaults to null, falling back to the bare host — that default IS
// the index page's title. Bare-hostname titles made every browser tab and
// history entry indistinguishable from each other (roadmap step 2); digest
// pages now pass a per-digest title instead (see renderDigestPage). Escaped
// here, once, same as the host fallback — callers pass the raw string.
function pageChrome(host, token, lang, view, switchersHtml, bodyHtml, title = null) {
  const viewTabsHtml = renderViewTabs(token, lang, view);
  const { first, rest } = brandParts(host);
  const strings = STRINGS[lang];
  return `<!doctype html>
<html lang="${lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<title>${esc(title ?? host)}</title>
<style>${CSS}</style>
</head>
<body>
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
  </footer>
</div>
<noscript><style>.backfab { opacity: 1; pointer-events: auto; }</style></noscript>
<script>
  // Show the floating back button only after the header nav has scrolled
  // away. Passive listener; runs once immediately so a mid-page reload
  // (browser scroll restoration) starts in the right state.
  (function () {
    var fab = document.querySelector(".backfab");
    if (!fab) return;
    var onScroll = function () {
      fab.classList.toggle("show", window.scrollY > 320);
    };
    addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  })();
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

function renderIndexEntry(row, token, lang, view) {
  const strings = STRINGS[lang];
  const time = formatTime(new Date(row.created_at), strings.locale);
  const isDaily = row.kind === "daily";
  // The badge is redundant in the daily view itself (every row there is
  // already a daily brief) — only the all view needs it to tell the two
  // kinds apart at a glance.
  const dailyFlag = isDaily && view !== "daily"
    ? `<span class="flag flag-daily">${esc(strings.dailyBrief)}</span>`
    : "";
  const flag = row.has_attention
    ? `<span class="flag">${esc(strings.attention)}</span>`
    : "";

  // HU page: prefer the translated tldr; if the app never sent one for this
  // digest, fall back to the English tldr and mark it with a muted "EN"
  // chip rather than silently presenting English text as if translated.
  let excerptHtml = esc(row.tldr);
  let langChip = "";
  if (lang === "hu") {
    if (row.tldr_hu) {
      excerptHtml = esc(row.tldr_hu);
    } else {
      langChip = '<span class="flag flag-muted">EN</span>';
    }
  }

  const counts = `${esc(row.item_count)} ${esc(strings.itemsWord)} · ${esc(row.section_count)} ${esc(strings.sectionsWord)}`;
  const timeClass = isDaily ? "time time-accent" : "time";
  const excerptClass = isDaily ? "excerpt excerpt-daily" : "excerpt";

  return `<a class="entry" href="${digestHref(token, lang, view, row.id)}">
    <span class="meta"><span class="${timeClass}">${esc(time)}</span><span class="count">${counts}</span>${dailyFlag}${flag}${langChip}</span>
    <p class="${excerptClass}"><strong>${esc(strings.tldrLabel)}</strong> ${excerptHtml}</p>
  </a>`;
}

function renderIndexPage(rows, token, host, lang, view) {
  const strings = STRINGS[lang];
  const groups = groupByDay(rows, strings.locale);
  const emptyMessage = view === "daily" ? strings.noDailyBriefs : strings.noDigests;
  const body =
    groups.length === 0
      ? `<p class="stamp">${esc(emptyMessage)}</p>`
      : groups
          .map(
            (group) => `<div class="dayhead">${esc(group.label)}</div>
${group.items.map((row) => renderIndexEntry(row, token, lang, view)).join("\n")}`,
          )
          .join("\n");

  return pageChrome(
    host,
    token,
    lang,
    view,
    renderSwitchers(token, lang, view, "index"),
    `<section>${body}</section>`,
  );
}

function renderDigestPage(digest, older, newer, token, host, lang, view) {
  const strings = STRINGS[lang];
  const date = new Date(digest.created_at);
  // "digest" itself stays an untranslated literal (see the STRINGS comment
  // above) — only the daily-brief label is real HU vocabulary, swapped in
  // for kind="daily".
  const kindLabel = digest.kind === "daily" ? strings.dailyBrief : "digest";
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
      `<a href="${digestHref(token, lang, view, older.id)}">← ${esc(formatTime(new Date(older.created_at), strings.locale))}</a>`,
    );
  }
  if (newer) {
    navLinks.push(
      `<a href="${digestHref(token, lang, view, newer.id)}">${esc(formatTime(new Date(newer.created_at), strings.locale))} →</a>`,
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

  // Same links, top and bottom: after an ~900-word read the natural gesture
  // is older/next, not scroll-to-top (roadmap step 2) — mirror the nav below
  // the article rather than making the reader travel back to the header.
  // Built once here, wrapped twice below; the bottom copy carries the extra
  // digestnav-bottom class (own CSS: top hairline + spacing, same as
  // .closing, since it follows the article's closing line).
  const digestNavLinksHtml = navLinks.join("\n");

  const body = `<nav class="digestnav">${digestNavLinksHtml}</nav>
<p class="stamp">${esc(stamp)}</p>
${enOnlyNoteHtml}<article class="digest">
${articleHtml}
</article>
<nav class="digestnav digestnav-bottom">${digestNavLinksHtml}</nav>
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
