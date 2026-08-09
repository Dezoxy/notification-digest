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
    noDigests:
      "No briefings yet. The next window closes every three hours — the first one lands here on its own.",
    noDailyBriefs: "No daily briefs yet — the first one lands at 20:00.",
    footerPrivate:
      "Private link — anyone with this URL can read. Don't share it outside the group.",
    footerNotIndexed: "Not indexed · generated by the digest service, every 3 hours",
    tldrLabel: "TL;DR:",
    backFabLabel: "Back to all digests",
    enOnlyNote: null,
    dailyBrief: "daily brief",
    viewAll: "All",
    viewDaily: "Daily",
    latest: "Latest",
    filterPlaceholder: "Filter briefings…",
    themeToggle: "Toggle light/dark",
    unreadFence: "new since your last visit",
  },
  hu: {
    locale: "hu-HU",
    attention: "figyelmet igényel",
    itemsWord: "elem",
    sectionsWord: "szakasz",
    allDigests: "← Minden hírlevél",
    noDigests:
      "Még nincs hírlevél. A következő ablak háromóránként zárul — az első magától megjelenik itt.",
    noDailyBriefs: "Még nincs napi összefoglaló — az első 20:00-kor érkezik.",
    footerPrivate:
      "Privát link — bárki olvashatja, akinél megvan ez az URL. Ne oszd meg a csoporton kívül.",
    footerNotIndexed: "Nem indexelt · a digest szolgáltatás generálja, 3 óránként",
    tldrLabel: "Röviden:",
    backFabLabel: "Vissza a hírlevelekhez",
    enOnlyNote: "Csak angolul elérhető",
    dailyBrief: "napi összefoglaló",
    viewAll: "Minden",
    viewDaily: "Napi",
    latest: "Legfrissebb",
    filterPlaceholder: "Szűrés…",
    themeToggle: "Világos/sötét váltás",
    unreadFence: "új a legutóbbi látogatásod óta",
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

// The masthead's right cluster carries the language toggle plus the manual
// theme toggle (roadmap step 6), on both index and digest pages — a reader
// override of the OS theme is useful everywhere, not just on the index. The
// button starts `hidden` (progressive enhancement, same as the filter input
// below) and is un-hidden by the bottom script once it's known to be wired.
function renderSwitchers(token, lang, view, pageKind, id) {
  const strings = STRINGS[lang];
  return `<div class="switchers">${renderLangSwitcher(token, lang, view, pageKind, id)}<button class="themetoggle" aria-label="${esc(strings.themeToggle)}" hidden>◐</button></div>`;
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
  /* Reserve the scrollbar's gutter even when the page is too short to
     scroll: the All view scrolls, a near-empty Daily view doesn't, and
     without this the viewport width changes on switch — sliding the
     centered bubble sideways by half a scrollbar (owner-reported). A
     no-op on overlay-scrollbar platforms, which never had the shift. */
  html { scrollbar-gutter: stable; }
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

  /* Manual theme toggle (roadmap step 6): small pill button after the lang
     switcher in .switchers. hidden by default, un-hidden by the bottom
     script — no JS, no button, same progressive-enhancement contract as the
     index filter input below. */
  .themetoggle {
    background: none; border: 1px solid var(--hairline); border-radius: 999px;
    color: var(--text); font-size: 0.8em; padding: 0.05em 0.5em; cursor: pointer;
  }
  .themetoggle:hover { border-color: var(--accent); }
  .themetoggle:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }

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
  /* Placed after .entry .excerpt (same specificity, later wins the cascade)
     so the lead card's excerpt actually loses its clamp instead of being
     silently overridden back to 3 lines. */
  .entry-lead .excerpt {
    font-size: 1.02em; display: block; -webkit-line-clamp: unset; overflow: visible;
  }

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
    /* So a TOC-jumped-to heading isn't flush against the viewport edge. */
    scroll-margin-top: 0.8em;
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

  /* Index filter (roadmap step 6): tucks under the view tabs — negative
     top margin pulls it snug against .viewtabs' own bottom margin instead
     of stacking two gaps. hidden by default (see renderIndexPage), so
     this rule only ever paints once JS un-hides the input. */
  .filterrow { margin: -0.6em 0 1.4em; }
  .filterrow .filter {
    display: block; width: 100%; font: inherit; font-size: 0.9em;
    padding: 0.5em 0.9em; border-radius: 10px;
    border: 1px solid var(--hairline); background: var(--bg); color: var(--text);
  }
  .filterrow .filter::placeholder { color: var(--muted); }
  /* Plain border otherwise; only :focus-visible gets a visible outline. */
  .filterrow .filter:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  /* Unread fence (roadmap 2 step 2): one labeled hairline the bottom script
     inserts between digests that arrived since the reader's last visit and
     everything older — no-JS readers never see this class at all, so no
     hidden-by-default dance is needed here (unlike .filter/.themetoggle
     above, which exist in the markup from the start). */
  .unreadfence { display: flex; align-items: center; gap: 0.7em; margin: 1.4em 0; }
  .unreadfence .line { flex: 1 1 auto; height: 0; border-top: 1px solid var(--accent); }
  .unreadfence .label {
    flex: 0 0 auto; font-family: var(--font-data); font-size: 0.7em;
    text-transform: uppercase; letter-spacing: 0.08em; color: var(--accent);
  }
  /* The filter IIFE hides the fence via the hidden attribute while a query
     is active (it can get orphaned mid-filter otherwise) — this class sets
     display unconditionally above, so it needs its own [hidden] override to
     actually disappear rather than fight the browser's UA stylesheet. */
  .unreadfence[hidden] { display: none; }

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
    .filterrow, .themetoggle {
      display: none;
    }
    .digest, .digest p, .digest h2, .stamp, .dayhead, .empty, .en-only-note {
      color: #000;
    }
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
<script>try{document.documentElement.dataset.theme=localStorage.getItem("theme")||""}catch(e){}</script>
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

  // Manual theme toggle (roadmap step 6). The head script already applied
  // any stored preference to <html data-theme> before first paint, so this
  // just wires the button: unhide it (progressive enhancement — no JS, no
  // button), reflect the current EFFECTIVE theme (stored, or the OS
  // preference when nothing is stored) in aria-pressed, and on click flip
  // light<->dark and persist it. There's no third "back to system" click —
  // that would need clearing storage, which isn't worth its own UI; a reader
  // who wants system-follow back can clear the site's local storage.
  (function () {
    var btn = document.querySelector(".themetoggle");
    if (!btn) return;
    btn.hidden = false;
    var effectiveTheme = function () {
      var stored = document.documentElement.dataset.theme;
      if (stored === "dark" || stored === "light") return stored;
      return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    };
    var reflect = function () {
      btn.setAttribute("aria-pressed", String(effectiveTheme() === "dark"));
    };
    reflect();
    btn.addEventListener("click", function () {
      var next = effectiveTheme() === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try {
        localStorage.setItem("theme", next);
      } catch (e) {}
      reflect();
    });
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
      }
    }

    // Update AFTER computing the fence above, and to the NEWEST entry's own
    // data-created — not "now" — so clock skew between the reader's device
    // and the server's stamped created_at can never make a digest look
    // newer or older than it is on the next visit.
    try {
      localStorage.setItem("lastVisit", newest);
    } catch (e) {}
  })();

  // Index filter (roadmap step 6, index pages only — guarded on the input's
  // existence since digest pages have no .filter). Case-insensitive
  // substring match against each .entry's text content (the lead card is an
  // .entry too); a .dayhead hides once every entry in its group (its
  // following siblings up to the next .dayhead) is hidden. No debounce at
  // these list sizes; an empty query restores everything.
  (function () {
    var input = document.querySelector(".filter");
    if (!input) return;
    input.hidden = false;
    var entries = Array.prototype.slice.call(document.querySelectorAll(".entry"));
    var dayheads = Array.prototype.slice.call(document.querySelectorAll(".dayhead"));
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      entries.forEach(function (el) {
        el.hidden = q && !el.textContent.toLowerCase().includes(q);
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
      // The unread fence is a load-time artifact; while filtering it can end
      // up orphaned between hidden entries, which isn't worth coupling the
      // two features over — just hide it whenever a query is active
      // (roadmap 2 step 2).
      var fence = document.querySelector(".unreadfence");
      if (fence) fence.hidden = Boolean(q);
    });
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

  const { excerptHtml, langChip } = renderExcerpt(row, lang);

  const counts = `${esc(row.item_count)} ${esc(strings.itemsWord)} · ${esc(row.section_count)} ${esc(strings.sectionsWord)}`;
  const timeClass = isDaily ? "time time-accent" : "time";
  const excerptClass = isDaily ? "excerpt excerpt-daily" : "excerpt";

  // data-created (roadmap 2 step 2, unread fence): the row's own created_at,
  // straight from D1 as an ISO UTC string — lexicographically comparable
  // without parsing, the same trick get_recent_digests (digest repo) relies
  // on. esc()'d like every other D1-sourced value inserted as an attribute.
  return `<a class="entry" href="${digestHref(token, lang, view, row.id)}" data-created="${esc(row.created_at)}">
    <span class="meta"><span class="${timeClass}">${esc(time)}</span><span class="count">${counts}</span>${dailyFlag}${flag}${langChip}</span>
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
  const isDaily = row.kind === "daily";
  const dailyFlag = isDaily && view !== "daily"
    ? `<span class="flag flag-daily">${esc(strings.dailyBrief)}</span>`
    : "";
  const flag = row.has_attention
    ? `<span class="flag">${esc(strings.attention)}</span>`
    : "";

  const { excerptHtml, langChip } = renderExcerpt(row, lang);

  const eyebrow = `${strings.latest} · ${formatShortDate(date, strings.locale)} · ${formatTime(date, strings.locale)} ${tzAbbr(date)} · ${row.item_count} ${strings.itemsWord}`;

  // data-created: same contract as renderIndexEntry's — see comment there.
  return `<a class="entry entry-lead" href="${digestHref(token, lang, view, row.id)}" data-created="${esc(row.created_at)}">
    <span class="meta"><span class="eyebrow-text">${esc(eyebrow)}</span>${dailyFlag}${flag}${langChip}</span>
    <p class="excerpt"><strong>${esc(strings.tldrLabel)}</strong> ${excerptHtml}</p>
  </a>`;
}

function renderIndexPage(rows, token, host, lang, view) {
  const strings = STRINGS[lang];
  const emptyMessage = view === "daily" ? strings.noDailyBriefs : strings.noDigests;

  let body;
  if (rows.length === 0) {
    body = `<p class="empty">${esc(emptyMessage)}</p>`;
  } else {
    // rows are ordered created_at DESC, so rows[0] is the newest digest in
    // this view — it renders as the lead card above the ledger and is
    // excluded from the grouped list below (no duplicate). groupByDay runs
    // on the remainder, so if the newest digest was that day's only entry,
    // no empty day header is left behind.
    const [lead, ...rest] = rows;
    const groups = groupByDay(rest, strings.locale);
    const ledger = groups
      .map(
        (group) => `<div class="dayhead">${esc(group.label)}</div>
${group.items.map((row) => renderIndexEntry(row, token, lang, view)).join("\n")}`,
      )
      .join("\n");
    body = `${renderLeadCard(lead, token, lang, view)}\n${ledger}`;
  }

  // Client-side filter (roadmap step 6, index pages only): sits between the
  // view tabs (rendered by pageChrome, just above this) and the lead card
  // (the first thing inside <section> below). `hidden` by default — no JS,
  // no filter UI — un-hidden by the bottom script in pageChrome.
  const filterRowHtml = `<div class="filterrow"><input class="filter" type="search" placeholder="${esc(strings.filterPlaceholder)}" aria-label="${esc(strings.filterPlaceholder)}" hidden></div>`;

  // data-unread-label (roadmap 2 step 2): the unread-fence label text,
  // rendered server-side so the bottom script that builds the fence stays
  // language-agnostic — it just reads this attribute rather than knowing
  // about STRINGS/lang itself.
  return pageChrome(
    host,
    token,
    lang,
    view,
    renderSwitchers(token, lang, view, "index"),
    `${filterRowHtml}<section data-unread-label="${esc(strings.unreadFence)}">${body}</section>`,
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

  // TOC ids are injected into articleHtml itself (see buildSectionToc), so
  // the <article> below renders the id-bearing version, not the original.
  const { html: articleHtmlWithIds, sections } = buildSectionToc(articleHtml);
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

  // Order: stamp -> en-only note -> TOC -> article. The TOC can't sit inside
  // the TL;DR-bearing article start as first imagined — the TL;DR callout is
  // itself inside body_html — so it renders above <article> instead.
  const body = `<nav class="digestnav">${digestNavLinksHtml}</nav>
<p class="stamp">${esc(stamp)}</p>
${enOnlyNoteHtml}${tocHtml}<article class="digest">
${articleHtmlFinal}
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
