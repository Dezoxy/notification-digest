// Read-only JSON API over the daily editions: /api/v1/:token/...
//
// Auth is API_TOKEN in the path (see apiTokenMatches). A wrong or missing
// token returns the site's uniform notFound(), so the API's existence is not
// revealed. An edition's `date` is the Europe/Budapest calendar date of its
// created_at; storage and range comparisons stay UTC text, like every other
// created_at query in this Worker.

import { apiTokenMatches } from "./auth.js";
import { notFound } from "./http.js";
import { TIMEZONE } from "./config.js";
import { budapestDateParts, budapestOffsetMinutes } from "./dates.js";

const SCHEMA_VERSION = "1.0";
const DEFAULT_LIMIT = 7;
const MAX_LIMIT = 14;

// Never select provenance (internal), body_html or the *_hu columns.
const EDITION_COLUMNS =
  "id, created_at, tldr, item_count, section_count, body_md, source_counts, failed_sources, topics, deltas";

const API_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "private, no-store",
  "referrer-policy": "no-referrer",
  "x-robots-tag": "noindex, nofollow",
  "x-content-type-options": "nosniff",
};

function apiJson(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), { status, headers: { ...API_HEADERS, ...extra } });
}

function apiError(status, code, message, extra) {
  return apiJson({ schema_version: SCHEMA_VERSION, error: { code, message } }, status, extra);
}

const notPublishedYet = () =>
  apiError(404, "not_published_yet", "This edition has not been published yet.", {
    "retry-after": "3600",
  });

// One line per handled request; deliberately never the token, URL or headers.
function logApi(route, status, extra = {}) {
  console.log(JSON.stringify({ event: "api", route, status, ...extra }));
}

const pad = (n) => String(n).padStart(2, "0");
const ymdString = ({ y, m, d }) => `${y}-${pad(m)}-${pad(d)}`;

function parseJson(text, fallback) {
  if (text == null) return fallback;
  try {
    const value = JSON.parse(text);
    return value ?? fallback;
  } catch {
    return fallback;
  }
}

// created_at as Budapest wall-clock time with its real offset (DST-correct).
function budapestIso(date) {
  const offset = budapestOffsetMinutes(date);
  const local = new Date(date.getTime() + offset * 60000).toISOString().slice(0, 19);
  const sign = offset < 0 ? "-" : "+";
  const abs = Math.abs(offset);
  return `${local}${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

const LINK_RE = /\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g;

function extractSources(md) {
  const seen = new Set();
  const sources = [];
  for (const [, label, url] of md.matchAll(LINK_RE)) {
    if (seen.has(url)) continue;
    seen.add(url);
    sources.push({ label, url });
  }
  return sources;
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function pick(obj, keys) {
  return Object.fromEntries(keys.filter((k) => obj[k] !== undefined).map((k) => [k, obj[k]]));
}

function arrayOfObjects(text, keys) {
  const parsed = parseJson(text, []);
  if (!Array.isArray(parsed)) return [];
  return parsed.filter((x) => x && typeof x === "object").map((x) => pick(x, keys));
}

async function buildEdition(row) {
  const created = new Date(row.created_at);
  const date = ymdString(budapestDateParts(created));
  const body = row.body_md ?? "";
  const sourceCounts = parseJson(row.source_counts, null);
  const failed = parseJson(row.failed_sources, []);
  return {
    schema_version: SCHEMA_VERSION,
    edition_id: `daily-${date}`,
    digest_id: row.id,
    edition_type: "daily",
    status: "complete",
    date,
    timezone: TIMEZONE,
    published_at: budapestIso(created),
    published_at_utc: created.toISOString().slice(0, 19) + "Z",
    title: `Daily brief — ${date}`,
    tldr: row.tldr,
    content: { format: "markdown", body },
    sources: extractSources(body),
    topics: arrayOfObjects(row.topics, ["slug", "label", "key"]),
    deltas: arrayOfObjects(row.deltas, ["slug", "previously", "now"]),
    stats: {
      item_count: row.item_count,
      section_count: row.section_count,
      source_counts:
        sourceCounts && typeof sourceCounts === "object" && !Array.isArray(sourceCounts)
          ? sourceCounts
          : null,
    },
    failed_sources: Array.isArray(failed) ? failed : [],
    content_hash: `sha256:${await sha256Hex(body)}`,
  };
}

function todayYmd() {
  return ymdString(budapestDateParts(new Date()));
}

// Strict calendar validation: Date.UTC rolls 2026-02-30 over to March, so
// the round trip must reproduce the input.
function parseDateParam(s) {
  const [y, m, d] = s.split("-").map(Number);
  const probe = new Date(Date.UTC(y, m - 1, d));
  if (probe.getUTCFullYear() !== y || probe.getUTCMonth() !== m - 1 || probe.getUTCDate() !== d) {
    return null;
  }
  return { y, m, d };
}

// Budapest-local midnight as a UTC ISO string. dates.js's budapestMidnightUtcIso
// samples the offset at NOON, which is wrong on a DST transition day (the
// transition falls between midnight and noon), so a row near the boundary would
// land on the wrong date. Here the offset is re-checked at the candidate instant.
function budapestMidnightUtc(y, m, d) {
  const naive = Date.UTC(y, m - 1, d);
  let t = naive - budapestOffsetMinutes(new Date(naive + 12 * 3600000)) * 60000;
  t = naive - budapestOffsetMinutes(new Date(t)) * 60000;
  return new Date(t).toISOString();
}

async function dailyForDate({ y, m, d }, env) {
  const next = new Date(Date.UTC(y, m - 1, d + 1));
  const start = budapestMidnightUtc(y, m, d);
  const end = budapestMidnightUtc(next.getUTCFullYear(), next.getUTCMonth() + 1, next.getUTCDate());
  // Deterministic when a date holds several daily rows: newest wins.
  return env.DB.prepare(
    `SELECT ${EDITION_COLUMNS} FROM digests
     WHERE kind = 'daily' AND created_at >= ? AND created_at < ?
     ORDER BY created_at DESC, id DESC LIMIT 1`,
  )
    .bind(start, end)
    .first();
}

function internalError(route, err) {
  // The error text stays in the Worker log; the client gets nothing from it.
  console.error(JSON.stringify({ event: "api_error", route, error: String(err?.message ?? err) }));
  logApi(route, 500);
  return apiError(500, "internal_error", "Internal error.");
}

export async function handleDailyApi(env, token, which, url) {
  if (!(await apiTokenMatches(env, token))) return notFound();

  const route = which === "latest" ? "daily_latest" : which ? "daily_date" : "daily_list";
  try {
    if (which === "latest") {
      const row = await env.DB.prepare(
        `SELECT ${EDITION_COLUMNS} FROM digests WHERE kind = 'daily'
         ORDER BY created_at DESC, id DESC LIMIT 1`,
      ).first();
      if (!row) {
        logApi(route, 404);
        return apiError(404, "not_found", "No daily edition exists.");
      }
      const edition = await buildEdition(row);
      logApi(route, 200, { digest_id: edition.digest_id, date: edition.date });
      return apiJson(edition);
    }

    if (which) {
      const parts = parseDateParam(which);
      if (!parts) {
        logApi(route, 400);
        return apiError(400, "invalid_date", "Date must be a real calendar date, YYYY-MM-DD.");
      }
      const date = ymdString(parts);
      const today = todayYmd();
      // A future date can have no row; skip the query. Same answer as today
      // before the 20:00 run, so a poller treats both as "check back later".
      const row = date > today ? null : await dailyForDate(parts, env);
      if (!row) {
        logApi(route, 404, { date });
        return date >= today
          ? notPublishedYet()
          : apiError(404, "not_found", "No daily edition exists for this date.");
      }
      const edition = await buildEdition(row);
      logApi(route, 200, { digest_id: edition.digest_id, date: edition.date });
      return apiJson(edition);
    }

    const raw = url.searchParams.get("limit");
    let limit = DEFAULT_LIMIT;
    if (raw !== null) {
      limit = /^\d+$/.test(raw) ? Number(raw) : NaN;
      if (!(limit >= 1 && limit <= MAX_LIMIT)) {
        logApi(route, 400);
        return apiError(400, "invalid_limit", `limit must be an integer from 1 to ${MAX_LIMIT}.`);
      }
    }
    const { results } = await env.DB.prepare(
      `SELECT ${EDITION_COLUMNS} FROM digests WHERE kind = 'daily'
       ORDER BY created_at DESC, id DESC LIMIT ?`,
    )
      .bind(limit)
      .all();
    const editions = await Promise.all((results ?? []).map(buildEdition));
    logApi(route, 200, { count: editions.length });
    return apiJson({
      schema_version: SCHEMA_VERSION,
      timezone: TIMEZONE,
      count: editions.length,
      editions,
    });
  } catch (err) {
    return internalError(route, err);
  }
}

export async function handleApiHealth(env, token) {
  if (!(await apiTokenMatches(env, token))) return notFound();
  try {
    const row = await env.DB.prepare(
      `SELECT id, created_at FROM digests WHERE kind = 'daily'
       ORDER BY created_at DESC, id DESC LIMIT 1`,
    ).first();
    const today = todayYmd();
    let latest = null;
    if (row) {
      const created = new Date(row.created_at);
      const date = ymdString(budapestDateParts(created));
      latest = {
        edition_id: `daily-${date}`,
        digest_id: row.id,
        date,
        published_at: budapestIso(created),
      };
    }
    logApi("health", 200);
    return apiJson({
      schema_version: SCHEMA_VERSION,
      status: "ok",
      timezone: TIMEZONE,
      today,
      today_published: latest?.date === today,
      latest_daily: latest,
    });
  } catch (err) {
    return internalError("health", err);
  }
}
