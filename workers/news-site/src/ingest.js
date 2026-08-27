import {
  ARC_CONTEXT_MAX_BYTES,
  ARC_IDENTITY_SQL,
  ARC_KEY_MAX_LEN,
  DELTA_TEXT_MAX_LEN,
  MAX_ARC_CONTEXTS,
  MAX_BODY_FIELD_BYTES,
  MAX_DELTAS,
  MAX_REQUEST_BYTES,
  MAX_SOURCE_ENTRIES,
  MAX_TLDR_BYTES,
  MAX_TOPICS,
  SOURCE_NAME_RE,
  TOPIC_SLUG_RE,
  arcIdentity,
} from "./config.js";
import { badRequest, json } from "./http.js";
import { keyMatches } from "./auth.js";
import { renderDeltas } from "./render-digest.js";
import { renderArcAppearance } from "./render-arc.js";
import { notifyForDigest } from "./notify.js";

// ── route handlers ──────────────────────────────────────────────────────

export async function handleIngest(request, env, idParam, ctx) {
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

  // arc_contexts (PLAN.md §11.6 context mode): a SECOND write, into the
  // separate arc_context table — unlike every other optional ingest field,
  // this one is not a digests column (see the arc_context table's own
  // comment in schema.sql for why: a primer is per-ARC, not per-digest).
  // Sequential, not batched: this file has no env.DB.batch() call anywhere
  // (the digests upsert just above is the only other write here, also a
  // single sequential .run()) — this follows that same established
  // sequential pattern rather than introducing batching for one call site.
  // That means the two writes are NOT atomic with each other, and an arc
  // with several primers in one payload is not atomic across its own
  // entries either: a failure partway through this loop leaves the digest
  // row committed and only the earlier arc_context entries upserted. A
  // retried ingest (the app's own recovery path — this endpoint is already
  // idempotent by id) re-runs everything and self-heals, so this is a
  // narrow, self-correcting window, not a lasting inconsistency — but it IS
  // a real partial-failure possibility worth flagging rather than silently
  // assuming atomicity that D1 (without an explicit .batch()) doesn't
  // provide.
  if (d.arc_contexts) {
    const upsertedAt = new Date().toISOString();
    try {
      for (const entry of d.arc_contexts) {
        await env.DB.prepare(
          `INSERT INTO arc_context (key, context_md, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
             context_md = excluded.context_md,
             updated_at = excluded.updated_at`,
        )
          .bind(entry.key, entry.context_md, upsertedAt)
          .run();
      }
    } catch {
      return json({ error: "database error" }, 500);
    }
  }

  // Push fan-out (PLAN.md §11.7, PR C). AFTER every write has succeeded, so
  // a notification can never announce a digest that failed to store.
  //
  // waitUntil, not await: the ingest response must not wait on a push
  // service, and a push service having a bad day must not turn a successful
  // ingest into a failed one. The digest is committed either way — this is
  // strictly an announcement. Everything that decides whether to actually
  // send anything (newest-only, claim-once, whether push is even
  // configured) lives in notifyForDigest, which swallows its own failures
  // for the same reason.
  //
  // ctx is optional so a caller without one — any direct handleIngest call
  // that is not the Worker entry point — still ingests correctly, just
  // without notifying.
  ctx?.waitUntil(notifyForDigest(env, id, new URL(request.url).origin));

  return json({ ok: true }, 200);
}

// ── validation ──────────────────────────────────────────────────────────

export const TEXT_ENCODER = new TextEncoder();

// The check every required-or-present text field of the ingest payload
// shares: a real string, non-empty, within its byte cap. Byte length, not
// code units — the caps are storage caps and UTF-8 is what D1 stores.
export function isNonEmptyString(value, maxBytes) {
  return typeof value === "string" && value.length > 0 && byteLength(value) <= maxBytes;
}

export function byteLength(str) {
  return TEXT_ENCODER.encode(str).length;
}

export function validateDigestPayload(payload) {
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
    arc_contexts,
  } = payload;

  if (
    typeof created_at !== "string" ||
    created_at.trim() === "" ||
    Number.isNaN(Date.parse(created_at))
  ) {
    return { ok: false, error: "created_at must be a valid date string" };
  }
  if (!isNonEmptyString(tldr, MAX_TLDR_BYTES)) {
    return { ok: false, error: "tldr must be a non-empty string within size limits" };
  }
  if (!Number.isInteger(item_count) || item_count < 0) {
    return { ok: false, error: "item_count must be a non-negative integer" };
  }
  if (!Number.isInteger(section_count) || section_count < 0) {
    return { ok: false, error: "section_count must be a non-negative integer" };
  }
  if (typeof has_attention !== "boolean" && has_attention !== 0 && has_attention !== 1) {
    return { ok: false, error: "has_attention must be a boolean" };
  }
  if (!isNonEmptyString(body_html, MAX_BODY_FIELD_BYTES)) {
    return { ok: false, error: "body_html must be a non-empty string within size limits" };
  }
  if (!isNonEmptyString(body_md, MAX_BODY_FIELD_BYTES)) {
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
      error: "tldr_hu, body_html_hu, and body_md_hu must all be present or all absent",
    };
  }
  const huEnabled = huFieldsPresent === 3;

  if (huEnabled) {
    if (!isNonEmptyString(tldr_hu, MAX_TLDR_BYTES)) {
      return { ok: false, error: "tldr_hu must be a non-empty string within size limits" };
    }
    if (!isNonEmptyString(body_html_hu, MAX_BODY_FIELD_BYTES)) {
      return { ok: false, error: "body_html_hu must be a non-empty string within size limits" };
    }
    if (!isNonEmptyString(body_md_hu, MAX_BODY_FIELD_BYTES)) {
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

  // arc_contexts (PLAN.md §11.6 context mode): optional, independent of
  // every field above — see validateArcContexts for the per-entry shape
  // rules. UNLIKE source_counts/failed_sources/topics/deltas, this is not a
  // digests-row column — it's a per-arc primer, upserted into the separate
  // arc_context table by handleIngest — so the parsed array itself rides
  // through below (arc_contexts), not a JSON-stringified string.
  const arcContextsResult = validateArcContexts(arc_contexts);
  if (!arcContextsResult.ok) {
    return { ok: false, error: arcContextsResult.error };
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
      arc_contexts: arcContextsResult.value,
    },
  };
}

// source_counts: absent/null is valid (nothing reported, stored NULL — see
// the schema.sql comment). Present, it must be a plain JSON object (not an
// array — typeof [] === "object" too, hence the explicit Array.isArray
// check), every key matching SOURCE_NAME_RE, every value a non-negative
// integer, at most MAX_SOURCE_ENTRIES keys. Any other shape is a 400, not a
// value to silently coerce or drop keys from.
export function validateSourceCounts(value) {
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
export function validateFailedSources(value) {
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
export function validateTopics(value) {
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
      return {
        ok: false,
        error: "each topics entry must have exactly slug, label, and the optional key",
      };
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
export function validateDeltas(value) {
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
      return {
        ok: false,
        error: `deltas["${slug}"].now must be 1-${DELTA_TEXT_MAX_LEN} characters`,
      };
    }
    normalized.push({ slug, previously: previously.trim(), now: now.trim() });
  }
  return { ok: true, value: normalized.length === 0 ? null : normalized };
}

// arc_contexts (PLAN.md §11.6 context mode): absent/null is valid (no
// primers this run — every app version before this feature, and any run
// that didn't generate one). Present, it must be a JSON array (Array.isArray,
// not typeof — same reasoning as topics/deltas above), at most
// MAX_ARC_CONTEXTS entries, each entry a plain object (not an array, not
// null) with EXACTLY two keys: `key` (the SAME stable-arc-identity shape
// topics' own optional `key` field uses — TOPIC_SLUG_RE, capped at
// ARC_KEY_MAX_LEN — since a primer is looked up by that same arc identity,
// see arcIdentity/ARC_IDENTITY_SQL) and `context_md` (a non-empty string,
// trimmed length capped at ARC_CONTEXT_MAX_BYTES bytes). Unknown per-entry
// keys are rejected via the `...rest` check, mirroring validateTopics/
// validateDeltas' own discipline. Unlike topics, a duplicate `key` across two
// entries in one payload is NOT rejected here — same posture validateDeltas
// already takes on duplicate delta slugs: this validator polices shape, not
// cross-entry semantics, and handleIngest's upsert loop just applies entries
// in order, so a later duplicate naturally wins (ON CONFLICT DO UPDATE would
// make that true even if it didn't).
export function validateArcContexts(value) {
  if (value === undefined || value === null) return { ok: true, value: null };
  if (!Array.isArray(value)) {
    return { ok: false, error: "arc_contexts must be a JSON array" };
  }
  if (value.length > MAX_ARC_CONTEXTS) {
    return { ok: false, error: `arc_contexts must have at most ${MAX_ARC_CONTEXTS} entries` };
  }
  const normalized = [];
  for (const entry of value) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) {
      return { ok: false, error: "each arc_contexts entry must be an object" };
    }
    const { key, context_md, ...rest } = entry;
    if (Object.keys(rest).length > 0) {
      return { ok: false, error: "each arc_contexts entry must have exactly key and context_md" };
    }
    if (typeof key !== "string" || !TOPIC_SLUG_RE.test(key) || key.length > ARC_KEY_MAX_LEN) {
      return { ok: false, error: `arc_contexts has an invalid key: "${key}"` };
    }
    if (
      typeof context_md !== "string" ||
      context_md.trim().length === 0 ||
      byteLength(context_md) > ARC_CONTEXT_MAX_BYTES
    ) {
      return {
        ok: false,
        error: `arc_contexts["${key}"].context_md must be a non-empty string within size limits`,
      };
    }
    normalized.push({ key, context_md: context_md.trim() });
  }
  return { ok: true, value: normalized.length === 0 ? null : normalized };
}
