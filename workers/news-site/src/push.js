import {
  MAX_PUSH_ENDPOINT_LEN,
  MAX_PUSH_KEY_LEN,
  MAX_PUSH_LABEL_LEN,
  MAX_PUSH_SUBSCRIPTIONS,
  PUSH_ENDPOINT_HOST_SUFFIXES,
} from "./config.js";
import { badRequest, json, notFound } from "./http.js";
import { tokenMatches } from "./auth.js";
import { STRINGS } from "./strings.js";
import { digestHref } from "./hrefs.js";

// ── Web Push subscriptions (PLAN.md §11.7, PR B) ────────────────────────
//
// Four endpoints, all token-gated exactly like a page, all returning the
// same indistinguishable notFound() on a wrong token:
//
//   GET  push/key         the VAPID public key the browser needs to subscribe
//   POST push/subscribe   store (or refresh) one device's subscription
//   POST push/unsubscribe forget one device
//   POST push/latest      what the service worker should show, localized
//
// push/latest is a POST and not a GET for one reason worth stating up
// front: it takes the caller's own push endpoint in the body, and that
// endpoint is a capability -- anyone holding it can send that device a
// notification. Query strings end up in Workers Observability logs and
// traces (see the trust-model note in the news-site README about the
// capability token already landing there), and one secret in retained
// observability data is a considered tradeoff while two is just carelessness.
//
// NOTHING here sends a push. The sender -- VAPID JWT signing, the fan-out
// from handleIngest, claim-once against push_sent -- is PR C. What this file
// establishes is who would be sent to.

// Is push configured at all? Both halves are needed: the public key is what
// the browser subscribes with, the private key is what PR C signs with, and
// a deployment with only one of them is misconfigured rather than disabled.
// Everything here fails CLOSED and QUIETLY on a no -- 503 with a machine
// reason, never a 500 -- because this Worker deploys automatically on merge
// to main, so the code lands before the secrets do, every time, by design.
export function pushConfigured(env) {
  return Boolean(env.VAPID_PUBLIC_KEY && env.VAPID_PRIVATE_JWK);
}

function unconfigured() {
  return json({ error: "push not configured" }, 503);
}

// ── validation ──────────────────────────────────────────────────────────

// The endpoint is the one field here that this Worker will later FETCH
// (PR C posts to it), which makes it the only one whose validation is a
// security control rather than hygiene. A capability-token holder chooses
// it, so an unchecked endpoint would turn this Worker into a blind POST
// proxy for any https host on the internet.
//
// A host-suffix allowlist rather than a shape check, deliberately, and the
// cost of that choice is honest: a browser vendor moving to a new push host
// breaks subscription on that browser until a suffix is added here. That is
// the right way round for a single-owner service with a handful of known
// devices -- a broken subscribe is loud, visible, and one line to fix,
// whereas an open proxy is silent. The rejection message names the host it
// refused so the fix is obvious from the failure alone.
export function validateEndpoint(endpoint) {
  if (typeof endpoint !== "string" || !endpoint) return { ok: false, error: "endpoint required" };
  if (endpoint.length > MAX_PUSH_ENDPOINT_LEN) return { ok: false, error: "endpoint too long" };
  let url;
  try {
    url = new URL(endpoint);
  } catch {
    return { ok: false, error: "endpoint must be a URL" };
  }
  if (url.protocol !== "https:") return { ok: false, error: "endpoint must be https" };
  const host = url.hostname.toLowerCase();
  const allowed = PUSH_ENDPOINT_HOST_SUFFIXES.some(
    (suffix) => host === suffix || host.endsWith(`.${suffix}`),
  );
  if (!allowed) {
    return { ok: false, error: `unknown push service host: ${host}` };
  }
  return { ok: true, value: endpoint };
}

// p256dh and auth are base64url from the browser's own subscription object.
// Shape-checked and bounded rather than decoded: this Worker never uses them
// (payload-less push), it only holds them so the RFC 8291 contingency stays
// a code-only change. Validating them as "plausible base64url of about the
// right size" is exactly as much as a store-and-forward field earns.
const BASE64URL_RE = /^[A-Za-z0-9_-]+=*$/;

function validateKey(value, name) {
  if (typeof value !== "string" || !value) return { ok: false, error: `${name} required` };
  if (value.length > MAX_PUSH_KEY_LEN) return { ok: false, error: `${name} too long` };
  if (!BASE64URL_RE.test(value)) return { ok: false, error: `${name} must be base64url` };
  return { ok: true, value };
}

export function validateSubscription(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return { ok: false, error: "body must be a JSON object" };
  }
  const endpoint = validateEndpoint(payload.endpoint);
  if (!endpoint.ok) return endpoint;

  // Accept the browser's own PushSubscription.toJSON() shape (keys nested
  // under `keys`) so the client can post the subscription verbatim without
  // reshaping it — one less place for the two sides to disagree.
  const keys = payload.keys && typeof payload.keys === "object" ? payload.keys : {};
  const p256dh = validateKey(keys.p256dh, "p256dh");
  if (!p256dh.ok) return p256dh;
  const auth = validateKey(keys.auth, "auth");
  if (!auth.ok) return auth;

  // Anything that is not exactly "hu" is English. A language this Worker
  // does not serve must not become a stored value that later selects a
  // STRINGS block that does not exist.
  const lang = payload.lang === "hu" ? "hu" : "en";

  // Optional, owner-facing only: which device this row is, for the day the
  // owner has three of them and wants to know which one stopped working.
  let label = null;
  if (payload.label !== undefined && payload.label !== null) {
    if (typeof payload.label !== "string") return { ok: false, error: "label must be a string" };
    label = payload.label.slice(0, MAX_PUSH_LABEL_LEN) || null;
  }

  return {
    ok: true,
    value: { endpoint: endpoint.value, p256dh: p256dh.value, auth: auth.value, lang, label },
  };
}

// ── handlers ────────────────────────────────────────────────────────────

async function readJson(request) {
  // Subscriptions are small and fixed-shape; anything large is a mistake or
  // an attack, and neither deserves to be parsed. (Compare ingest.js, which
  // accepts megabytes because a digest body legitimately is megabytes.)
  const raw = await request.text().catch(() => null);
  if (raw === null || raw.length > 8 * 1024) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export async function handlePushKey(env, token) {
  if (!(await tokenMatches(env, token))) return notFound();
  if (!pushConfigured(env)) return unconfigured();
  // Public by design — it is handed to every browser that subscribes, and
  // it is useless without the private half. Still served behind the token
  // gate, because an endpoint that answers differently for a valid token is
  // an oracle for whether the token is valid.
  return json({ key: env.VAPID_PUBLIC_KEY }, 200);
}

export async function handlePushSubscribe(request, env, token) {
  if (!(await tokenMatches(env, token))) return notFound();
  if (!pushConfigured(env)) return unconfigured();

  const payload = await readJson(request);
  if (payload === null) return badRequest("body must be valid JSON");
  const validated = validateSubscription(payload);
  if (!validated.ok) return badRequest(validated.error);
  const s = validated.value;

  // The cap is a backstop against a leaked token filling D1, not a device
  // budget -- and it is checked BEFORE the upsert so that re-subscribing an
  // already-stored device can never be refused by it. Without that ordering
  // a device at the cap would be unable to refresh its own row, which is
  // the exact case that matters most (browsers rotate endpoints).
  const existing = await env.DB.prepare("SELECT 1 FROM push_subscriptions WHERE endpoint = ?")
    .bind(s.endpoint)
    .first();
  if (!existing) {
    const { total } = (await env.DB.prepare(
      "SELECT COUNT(*) AS total FROM push_subscriptions",
    ).first()) ?? { total: 0 };
    if (total >= MAX_PUSH_SUBSCRIPTIONS) {
      // Names the remedy: a cap with no eviction policy is otherwise a
      // silent lockout for the owner's next real device.
      return json(
        {
          error: `subscription limit (${MAX_PUSH_SUBSCRIPTIONS}) reached; clear stale rows with: DELETE FROM push_subscriptions`,
        },
        429,
      );
    }
  }

  const now = new Date().toISOString();
  try {
    await env.DB.prepare(
      `INSERT INTO push_subscriptions (endpoint, p256dh, auth, lang, label, created_at, last_ok_at, fail_count)
       VALUES (?, ?, ?, ?, ?, ?, NULL, 0)
       ON CONFLICT(endpoint) DO UPDATE SET
         p256dh     = excluded.p256dh,
         auth       = excluded.auth,
         lang       = excluded.lang,
         label      = excluded.label,
         -- created_at deliberately NOT overwritten: it records when this
         -- device first opted in, and a re-subscribe is the same consent
         -- continuing, not a new one.
         fail_count = 0`,
    )
      .bind(s.endpoint, s.p256dh, s.auth, s.lang, s.label, now)
      .run();
  } catch {
    return json({ error: "database error" }, 500);
  }
  return json({ ok: true }, 200);
}

export async function handlePushUnsubscribe(request, env, token) {
  if (!(await tokenMatches(env, token))) return notFound();
  // Deliberately NOT gated on pushConfigured: a reader must always be able
  // to withdraw, including from a deployment whose keys were pulled after
  // they subscribed. Removing consent is never the thing that fails closed.
  const payload = await readJson(request);
  if (payload === null) return badRequest("body must be valid JSON");
  const endpoint = validateEndpoint(payload && payload.endpoint);
  if (!endpoint.ok) return badRequest(endpoint.error);
  try {
    await env.DB.prepare("DELETE FROM push_subscriptions WHERE endpoint = ?")
      .bind(endpoint.value)
      .run();
  } catch {
    return json({ error: "database error" }, 500);
  }
  // Idempotent by construction: deleting an endpoint that was never stored
  // is the same success as deleting one that was. The caller wanted to end
  // up unsubscribed, and it is.
  return json({ ok: true }, 200);
}

// What the service worker renders. The worker is a dumb renderer by design
// (PLAN.md §11.7, "Why the push carries no payload") -- the push message
// itself is empty, so everything visible comes from here, fetched fresh at
// notification time. Two consequences that are features: the notification
// always describes the NEWEST digest rather than whichever one was current
// when the push was queued, and no digest text ever transits Apple's or
// Google's infrastructure.
export async function handlePushLatest(request, env, token) {
  if (!(await tokenMatches(env, token))) return notFound();

  // The caller identifies itself by its own push endpoint, which is the
  // only thing a service worker knows about itself that this Worker also
  // knows. That lookup exists solely to pick a language: the worker has no
  // page context, and its scope (/t/<token>/) carries no language segment,
  // so without this every notification would be English. An unknown or
  // absent endpoint is not an error -- it just falls back to English, the
  // same as PR A's offline notification does.
  const payload = await readJson(request);
  let lang = "en";
  const endpoint = validateEndpoint(payload && payload.endpoint);
  if (endpoint.ok) {
    const row = await env.DB.prepare("SELECT lang FROM push_subscriptions WHERE endpoint = ?")
      .bind(endpoint.value)
      .first();
    if (row && row.lang === "hu") lang = "hu";
  }

  const digest = await env.DB.prepare(
    "SELECT id, kind, tldr, tldr_hu, has_attention FROM digests ORDER BY id DESC LIMIT 1",
  ).first();
  if (!digest) {
    // No digests at all: a real state on a fresh deployment, and not one
    // worth a 404 (the token was valid). The worker's own fallback copy is
    // the right thing to show.
    return json({ empty: true }, 200);
  }

  const strings = STRINGS[lang];
  return json(
    {
      title: notificationTitle(strings, digest),
      body: notificationBody(lang, digest),
      url: digestHref(token, lang, "all", digest.id),
      // Drives requireInteraction in the worker: an urgent brief should not
      // auto-dismiss off a desktop before it has been read.
      urgent: Boolean(digest.has_attention),
      id: digest.id,
    },
    200,
  );
}

// kind becomes PRESENTATION, not filtering (§11.7: the owner chose "notify
// on every digest"). A window digest, a daily brief and a weekly report all
// ring; the title is how you tell which one arrived without opening it.
function notificationTitle(strings, digest) {
  if (digest.kind === "daily") return capitalize(strings.dailyBrief);
  if (digest.kind === "weekly") return capitalize(strings.weeklyBrief);
  return `${capitalize(strings.pushWindowTitle)} #${digest.id}`;
}

function capitalize(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// The TL;DR, trimmed to something a lock screen will actually show. Falls
// back to the English TL;DR on a Hungarian notification when the app sent
// no translation for this digest -- the same fallback the /hu/ pages
// themselves already do, rather than showing an empty notification.
function notificationBody(lang, digest) {
  const text = (lang === "hu" && digest.tldr_hu) || digest.tldr || "";
  const collapsed = text.replace(/\s+/g, " ").trim();
  return collapsed.length > 160 ? `${collapsed.slice(0, 159)}…` : collapsed;
}
