// ── config ──────────────────────────────────────────────────────────────

export const TIMEZONE = "Europe/Budapest";

// Per-field caps ("sanely" bounded, not exact science): body_html/body_md are
// full digest bodies and can legitimately run long; tldr is a one-paragraph
// summary and should never approach that size. The optional _hu translation
// counterparts share these exact same caps.
export const MAX_BODY_FIELD_BYTES = 2 * 1024 * 1024;

// 2MB
export const MAX_TLDR_BYTES = 32 * 1024;

// 32KB

// Overall request ceiling. Two 2MB text fields JSON-encoded (escaping can
// expand multi-byte/control characters) plus the smaller fields and JSON
// structure overhead — 8MB leaves comfortable headroom without being an
// effectively unbounded accept-anything limit.
export const MAX_REQUEST_BYTES = 8 * 1024 * 1024;

// Ingest v2 (roadmap 2 step 8): source_counts/failed_sources are OPTIONAL,
// backward-compatible fields on PUT /ingest/:id (see validateDigestPayload).
// Deliberately no hardcoded list of "the app's known sources" here — the
// site doesn't own that list, the digest app does, and a new collector must
// never require a site deploy to start reporting. A source name only has to
// match this shape; both fields cap at 16 entries as a sane ceiling on an
// app that currently has five collectors.
export const SOURCE_NAME_RE = /^[a-z][a-z0-9_-]{0,31}$/;

export const MAX_SOURCE_ENTRIES = 16;

// Ingest v3 (roadmap 4 step 8): topics is an OPTIONAL, backward-compatible
// field on PUT /ingest/:id (see validateDigestPayload/validateTopics), same
// shape-discipline pattern as source_counts/failed_sources above. Slugs are
// caller-chosen (the digest app derives them, the site doesn't own the
// vocabulary), lowercase-and-dash only so they're safe to use as-is if a
// future step ever needs them in a URL; 12 is a sane ceiling on how many
// distinct threads one briefing legitimately touches.
export const TOPIC_SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;

export const MAX_TOPICS = 12;

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
export const ARC_KEY_MAX_LEN = 48;

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
export const ARC_IDENTITY_SQL = "COALESCE(je.value->>'key', je.value->>'slug')";

export function arcIdentity(topicEntry) {
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
export const MAX_DELTAS = MAX_TOPICS;

export const DELTA_TEXT_MAX_LEN = 400;

// arc_contexts (PLAN.md §11.6 context mode): optional, backward-compatible
// TOP-LEVEL field on PUT /ingest/:id (see validateDigestPayload/
// validateArcContexts) — a per-arc durable-background primer, not a
// per-digest field, so it does not live inside the digests row at all (see
// the arc_context table, schema.sql) — this cap only bounds one ingest
// PAYLOAD's array, same "shape discipline" pattern as topics/deltas above.
// MAX_ARC_CONTEXTS deliberately does NOT reuse MAX_TOPICS the way MAX_DELTAS
// does, and the difference is the whole point of this comment. `topics` and
// `deltas` describe THIS digest, so MAX_TOPICS genuinely bounds them.
// `arc_contexts` does not: the app attaches the FULL current snapshot of
// every primer it has ever generated to EVERY publish (digest/deliver.py's
// `_deliver_site` -- an unscoped send is what makes the feature self-healing
// without a "has the site confirmed this primer" column), so this array's
// size tracks the age of the deployment, not the shape of one digest.
// Bounding it by MAX_TOPICS was a false analogy that made every site publish
// 400 the moment the 13th primer was generated -- total, permanent, and
// self-sustaining, since each retry re-sent the same oversized snapshot.
// The value MUST stay in lockstep with the app's own
// `_MAX_ARC_CONTEXTS_PER_PUBLISH` (digest/state.py), which is what actually
// caps the array on the wire. 50 primers at ARC_CONTEXT_MAX_BYTES each is
// ~400KB, an order of magnitude under MAX_REQUEST_BYTES, so this ceiling
// costs nothing. ARC_CONTEXT_MAX_BYTES bounds `context_md`: the spec is
// "3-5 SHORT markdown paragraphs" (a primer, not a full digest body) —
// 8KB is several times the size even a generous reading of "3-5 short
// paragraphs" would produce (roughly 1000-1500 words at typical English
// prose density), while staying two orders of magnitude below
// MAX_BODY_FIELD_BYTES's 2MB full-digest-body cap, which this is nothing
// like.
export const MAX_ARC_CONTEXTS = 50;

export const ARC_CONTEXT_MAX_BYTES = 8 * 1024;

// 8KB

// provenance (model-provenance byline, ingest v5): optional, backward-
// compatible top-level field on PUT /ingest/:id (see
// validateDigestPayload/validateProvenance) — the digest app's own model,
// effort, and OpenRouter-fallback flag for the summarize and (when this
// digest was translated) translate legs, rendered as the digest page's
// "WRITTEN" row directly below the source key (renderProvenance,
// src/render-index.js). Unlike source_counts/topics/deltas, this isn't a
// bounded LIST of typed entries — it's at most two small legs, each a
// handful of short strings — so one whole-object byte cap on the
// JSON-stringified payload is the simplest bound, rather than a
// per-string cap like DELTA_TEXT_MAX_LEN. Sized generously for "two model
// names, two effort words, two booleans" while staying two orders of
// magnitude under MAX_TLDR_BYTES, which bounds actual prose, not a
// handful of identifiers.
export const MAX_PROVENANCE_BYTES = 1024;

// 1KB

// Arc pages (§11.1 PR A, handleArcPage): how many of an arc's NEWEST
// appearances get their digest's body_html fetched for #sN deep-link
// resolution. 24 ≈ three days of 3-hourly windows — the span a reader
// plausibly navigates into from a live arc page; every older appearance
// links to its digest fragment-free (the feature's documented degraded
// mode). Bounds the per-request body_html haul regardless of how many
// hundreds of appearances an arc accumulates over months.
export const ARC_ANCHOR_BODIES = 24;

// Web Push (PLAN.md §11.7, PR B). Same shape-discipline pattern as the
// ingest caps above: bound what is stored, and be explicit about which
// bound is a security control rather than hygiene.
//
// PUSH_ENDPOINT_HOST_SUFFIXES is the one that IS a security control. The
// endpoint a browser mints is chosen by whoever holds the capability token,
// and PR C's sender will POST to it -- so without this list the Worker
// becomes a blind POST proxy for any https host on the internet. An
// allowlist rather than a shape check, and the tradeoff is deliberate: a
// browser vendor moving to a new push host breaks subscribing on that
// browser until a suffix is added here. For a single-owner service with a
// handful of known devices that is the right way round -- a broken
// subscribe is loud and one line to fix, an open proxy is silent. The
// current entries cover the four engines that exist: Chrome/Edge on FCM,
// Firefox on Mozilla's autopush, Safari (macOS and iOS) on Apple, and
// Windows' own WNS.
export const PUSH_ENDPOINT_HOST_SUFFIXES = [
  "fcm.googleapis.com",
  "android.googleapis.com",
  "push.services.mozilla.com",
  "web.push.apple.com",
  "notify.windows.com",
];

// A push endpoint is a long opaque URL (FCM's run ~200 chars, Mozilla's
// carry a UUID path); 2KB is far above any real one while still refusing an
// unbounded string into a PRIMARY KEY column.
export const MAX_PUSH_ENDPOINT_LEN = 2048;

// p256dh is a 65-byte P-256 point and auth is 16 bytes, so base64url runs
// 88 and 24 characters respectively. 256 leaves generous room without
// pretending these are open-ended.
export const MAX_PUSH_KEY_LEN = 256;

// An owner-chosen device nickname ("iPhone"), truncated rather than
// rejected -- it is cosmetic, and a too-long label is not worth failing a
// subscribe over.
export const MAX_PUSH_LABEL_LEN = 64;

// A backstop against a leaked capability token filling D1 with rows, not a
// device budget -- the owner has single digits of devices. Checked only for
// endpoints not already stored (see handlePushSubscribe), so a device at
// the cap can always still refresh its own subscription.
export const MAX_PUSH_SUBSCRIPTIONS = 20;

// How long a push service should hold an undelivered notification (PLAN.md
// §11.7, PR C). Four hours, deliberately shorter than the six-hour window
// cadence: a notification about digest N is not just stale but WRONG once
// N+1 exists, because the payload-less service worker would fetch
// push/latest and render the newer brief under the older ring. Letting it
// expire instead is the honest outcome — the site is right there.
export const PUSH_TTL_SECONDS = 4 * 60 * 60;

// Consecutive soft failures (429, 5xx, network) before a subscription is
// dropped. 404/410 bypass this entirely — a push service saying "gone" is
// authoritative and the row is deleted on the spot. This counter is for the
// ambiguous cases, where the right posture is patience: five failed
// fan-outs is over a day of a device being unreachable, which is a dead
// subscription rather than a bad afternoon.
export const MAX_PUSH_FAILURES = 5;

// How close together two ingests have to be for the SECOND one to ring
// silently (PLAN.md §11.7 follow-up). notify.js's newest-only rule intends
// that a backlog flush produces ONE notification, but it cannot achieve that
// on its own: it compares each digest against MAX(id) at ITS OWN ingest
// moment, and in a flush every digest is briefly the newest, so every one of
// them rings. Measured live on 2026-09-04 -- digests 341 and 344 were
// republished 575ms apart after a site outage and BOTH rang, showing
// identical content (the payload-less service worker fetches push/latest, so
// each notification renders whatever is newest by the time it opens).
//
// 60s is chosen from the same incident's real spacing: the burst was under
// one second apart, while the next GENUINELY new digest that run produced
// landed 8.5 minutes later and deserves its own ring. A minute sits far from
// both, so this can collapse a flush without ever swallowing a real digest.
export const PUSH_COALESCE_SECONDS = 60;
