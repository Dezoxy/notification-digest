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
 * Routes:
 *   GET  /robots.txt          -> disallow everything, no token needed
 *   PUT  /ingest/:id          -> upsert a digest (x-ingest-key required)
 *   GET  /t/:token/           -> index, newest-first, grouped by day
 *   GET  /t/:token/d/:id      -> single digest, with prev/next nav
 *   anything else             -> plain 404, wrong token included
 *
 * body_html arrives PRE-SANITIZED by the app (nh3) and is stored/served
 * verbatim — it is the only field ever inserted into a response without
 * HTML-escaping. Every other D1-sourced value goes through esc().
 */

// ── config ──────────────────────────────────────────────────────────────

const TIMEZONE = "Europe/Budapest";

// Per-field caps ("sanely" bounded, not exact science): body_html/body_md are
// full digest bodies and can legitimately run long; tldr is a one-paragraph
// summary and should never approach that size.
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

    const ingestMatch = path.match(/^\/ingest\/(\d+)$/);
    if (ingestMatch) {
      if (request.method !== "PUT") return notFound();
      return handleIngest(request, env, ingestMatch[1]);
    }

    const digestMatch = path.match(/^\/t\/([^/]+)\/d\/(\d+)$/);
    if (digestMatch && request.method === "GET") {
      return handleDigestPage(env, digestMatch[1], digestMatch[2], url);
    }

    const indexMatch = path.match(/^\/t\/([^/]+)\/$/);
    if (indexMatch && request.method === "GET") {
      return handleIndexPage(env, indexMatch[1], url);
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
         (id, created_at, tldr, item_count, section_count, has_attention, body_html, body_md)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET
         created_at    = excluded.created_at,
         tldr          = excluded.tldr,
         item_count    = excluded.item_count,
         section_count = excluded.section_count,
         has_attention = excluded.has_attention,
         body_html     = excluded.body_html,
         body_md       = excluded.body_md`,
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
      )
      .run();
  } catch {
    return json({ error: "database error" }, 500);
  }

  return json({ ok: true }, 200);
}

async function handleIndexPage(env, token, url) {
  if (!(await tokenMatches(env, token))) return notFound();

  // LIMIT 1000 = ~4 months of 3-hourly digests. Not pagination, a page-weight
  // backstop: every row carries a ~paragraph tldr, and an unbounded index
  // would grow by ~1MB/quarter forever. Older digests stay reachable through
  // each digest page's prev/next chain; add real pagination if the cap is
  // ever actually felt.
  const { results } = await env.DB.prepare(
    "SELECT id, created_at, tldr, item_count, section_count, has_attention FROM digests ORDER BY id DESC LIMIT 1000",
  ).all();

  return htmlResponse(renderIndexPage(results ?? [], token, url.hostname));
}

async function handleDigestPage(env, token, idParam, url) {
  if (!(await tokenMatches(env, token))) return notFound();

  const id = Number(idParam);
  if (!Number.isInteger(id) || id <= 0) return notFound();

  const digest = await env.DB.prepare(
    "SELECT id, created_at, tldr, item_count, section_count, has_attention, body_html FROM digests WHERE id = ?",
  )
    .bind(id)
    .first();
  if (!digest) return notFound();

  const [older, newer] = await Promise.all([
    env.DB.prepare(
      "SELECT id, created_at FROM digests WHERE id < ? ORDER BY id DESC LIMIT 1",
    )
      .bind(id)
      .first(),
    env.DB.prepare(
      "SELECT id, created_at FROM digests WHERE id > ? ORDER BY id ASC LIMIT 1",
    )
      .bind(id)
      .first(),
  ]);

  return htmlResponse(
    renderDigestPage(digest, older, newer, token, url.hostname),
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

function formatDayHeader(date) {
  // "Wednesday, 5 August 2026"
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: TIMEZONE,
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  }).format(date);
}

function formatTime(date) {
  // "18:00"
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: TIMEZONE,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function tzAbbr(date) {
  // Recent ICU versions render timeZoneName:"short" for Europe/* zones as a
  // GMT offset ("GMT+1"/"GMT+2") rather than "CET"/"CEST", so derive the
  // abbreviation from the offset ourselves instead of trusting that string.
  // Europe/Budapest only ever has these two offsets, so the mapping is exact.
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

// ── page chrome (shared masthead/footer/CSS — one template, both pages) ─

function brandParts(host) {
  const idx = host.indexOf(".");
  if (idx === -1) return { first: host, rest: "" };
  return { first: host.slice(0, idx), rest: host.slice(idx) };
}

const CSS = `
  :root {
    --bg: #ffffff;
    --page-bg: #e9e9f2;
    --text: #1f2430;
    --muted: #8a8f9e;
    --accent: #4f46e5;
    --accent-strong: #4338ca;
    --tldr-bg: #eef2ff;
    --tldr-text: #262a49;
    --chip-bg: #dde3ff;
    --chip-text: #4338ca;
    --hairline: #e5e7eb;
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
    gap: 1em; padding: 1.4em 0 1em; border-bottom: 1px solid var(--hairline);
    margin-bottom: 1.6em;
  }
  .mast .brand { font-weight: 700; font-size: 1.05em; letter-spacing: -0.01em; text-decoration: none; color: var(--text); }
  .mast .brand .tld { color: var(--accent); }
  .mast .cadence { color: var(--muted); font-size: 0.8em; }

  .dayhead {
    font-size: 0.78em; text-transform: uppercase; letter-spacing: 0.09em;
    color: var(--muted); margin: 2.2em 0 0.4em; font-weight: 600;
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
  .entry .time { font-weight: 700; font-size: 0.95em; }
  .entry .count { color: var(--muted); font-size: 0.8em; }
  .entry .flag {
    font-size: 0.72em; font-weight: 600; padding: 0.1em 0.55em; border-radius: 99px;
    background: var(--attention-bg); color: var(--attention-text);
  }
  .entry .excerpt {
    margin: 0; color: var(--muted); font-size: 0.93em;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }
  .entry .excerpt strong { color: var(--text); }

  nav.digestnav {
    display: flex; justify-content: space-between; gap: 1em;
    font-size: 0.85em; margin-bottom: 1.8em;
  }
  nav.digestnav a { text-decoration: none; }
  nav.digestnav .spacer { flex: 1; }
  .stamp { color: var(--muted); font-size: 0.85em; margin: 0 0 1.2em; font-variant-numeric: tabular-nums; }

  .attention {
    background: var(--attention-bg); color: var(--attention-text);
    padding: 0.8em 1em; border-radius: 8px; margin: 0 0 1.4em;
  }
  .attention h2 { margin: 0 0 0.3em; border: 0; padding: 0; font-size: 0.95em; }
  .attention p { margin: 0; font-size: 0.95em; }

  .tldr {
    background: var(--tldr-bg); color: var(--tldr-text);
    padding: 1em 1.2em; border-radius: 8px; margin: 0 0 2em; font-weight: 600;
  }
  .digest h2 {
    font-size: 1.15em; border-left: 3px solid var(--h2-border);
    padding-left: 0.6em; margin: 1.9em 0 0.6em; text-wrap: balance;
  }
  .digest p { margin: 0.7em 0; }
  .cite {
    font-size: 0.7em; vertical-align: super; text-decoration: none;
    background: var(--chip-bg); color: var(--chip-text);
    padding: 0 0.4em; border-radius: 99px; font-weight: 700; margin-left: 1px;
  }
  .closing {
    font-style: italic; color: var(--muted); border-top: 1px solid var(--hairline);
    padding-top: 1em; margin-top: 2.2em; font-size: 0.92em;
  }

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
  }
`;

function pageChrome(host, token, bodyHtml) {
  const { first, rest } = brandParts(host);
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(host)}</title>
<style>${CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="mast">
    <a class="brand" href="/t/${encodeURIComponent(token)}/">${esc(first)}<span class="tld">${esc(rest)}</span></a>
    <span class="cadence">every 3 hours · private link</span>
  </header>
  ${bodyHtml}
  <footer class="site">
    <p>Private link — anyone with this URL can read. Don't share it outside the group.</p>
    <p>Not indexed · generated by the digest service, every 3 hours</p>
  </footer>
</div>
</body>
</html>`;
}

// ── page bodies ──────────────────────────────────────────────────────────

function groupByDay(rows) {
  const groups = [];
  let currentLabel = null;
  let currentItems = null;
  for (const row of rows) {
    const label = formatDayHeader(new Date(row.created_at));
    if (label !== currentLabel) {
      currentLabel = label;
      currentItems = [];
      groups.push({ label, items: currentItems });
    }
    currentItems.push(row);
  }
  return groups;
}

function renderIndexEntry(row, token) {
  const time = formatTime(new Date(row.created_at));
  const flag = row.has_attention
    ? '<span class="flag">needs attention</span>'
    : "";
  return `<a class="entry" href="/t/${encodeURIComponent(token)}/d/${esc(row.id)}">
    <span class="meta"><span class="time">${esc(time)}</span><span class="count">${esc(row.item_count)} items · ${esc(row.section_count)} sections</span>${flag}</span>
    <p class="excerpt"><strong>TL;DR:</strong> ${esc(row.tldr)}</p>
  </a>`;
}

function renderIndexPage(rows, token, host) {
  const groups = groupByDay(rows);
  const body =
    groups.length === 0
      ? '<p class="stamp">No digests yet.</p>'
      : groups
          .map(
            (group) => `<div class="dayhead">${esc(group.label)}</div>
${group.items.map((row) => renderIndexEntry(row, token)).join("\n")}`,
          )
          .join("\n");

  return pageChrome(host, token, `<section>${body}</section>`);
}

function renderDigestPage(digest, older, newer, token, host) {
  const date = new Date(digest.created_at);
  const stamp = `${formatDayHeader(date)} · ${formatTime(date)} ${tzAbbr(date)} · digest #${digest.id}`;

  const navLinks = [
    `<a href="/t/${encodeURIComponent(token)}/">← All digests</a>`,
    '<span class="spacer"></span>',
  ];
  // Hide the link entirely at each end (oldest has no older, newest has no
  // newer) rather than showing a disabled placeholder.
  if (older) {
    navLinks.push(
      `<a href="/t/${encodeURIComponent(token)}/d/${esc(older.id)}">← ${esc(formatTime(new Date(older.created_at)))}</a>`,
    );
  }
  if (newer) {
    navLinks.push(
      `<a href="/t/${encodeURIComponent(token)}/d/${esc(newer.id)}">${esc(formatTime(new Date(newer.created_at)))} →</a>`,
    );
  }

  const body = `<nav class="digestnav">${navLinks.join("\n")}</nav>
<p class="stamp">${esc(stamp)}</p>
<article class="digest">
${digest.body_html}
</article>`;

  return pageChrome(host, token, body);
}
