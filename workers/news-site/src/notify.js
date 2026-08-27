import { MAX_PUSH_FAILURES, PUSH_TTL_SECONDS } from "./config.js";
import { pushConfigured } from "./push.js";
import { vapidAuthorization, vapidSubject } from "./vapid.js";

// ── the sender (PLAN.md §11.7, PR C) ────────────────────────────────────
//
// Runs from handleIngest under ctx.waitUntil, NOT as a delivery channel in
// the digest app. "A digest arrived" and "the site has the digest" are the
// same event, so the app would only be re-deriving something this Worker
// already knows — and doing it here means no tag, no GHCR image, no homelab
// bump, no VM deploy, no Key Vault secret, and no config.py env var. See
// §11.7's "Why the Worker triggers it".
//
// waitUntil rather than awaiting inline: a slow or failing push service
// must never delay, let alone fail, an ingest. The digest is already
// committed by the time this starts.

// Which digests are worth interrupting for. Owner decision, 2026-08-27:
// EVERY digest — window, daily and weekly alike — chosen over "briefs and
// urgent windows only".
//
// Kept as a real function rather than inlined at its one call site, and
// that is deliberate rather than ceremony: if this turns out noisy (a
// window digest lands every six hours, and the Telegram TL;DR already pings
// for it), the fix is this one line plus a test row — no schema change, no
// route change, nothing to re-subscribe. The kind/has_attention distinction
// is not discarded either; it moved to PRESENTATION, in push.js's
// notificationTitle and in the urgent flag below.
export function shouldNotify(_digest) {
  return true;
}

export async function notifyForDigest(env, id, siteOrigin) {
  // Fails closed and silently. This Worker deploys automatically on merge,
  // so the code always lands before the secrets — that window is the
  // default path through every deployment, not an edge case.
  if (!pushConfigured(env)) return { skipped: "unconfigured" };

  try {
    const digest = await env.DB.prepare("SELECT id, kind, has_attention FROM digests WHERE id = ?")
      .bind(id)
      .first();
    if (!digest) return { skipped: "missing" };
    if (!shouldNotify(digest)) return { skipped: "policy" };

    // NEWEST-ONLY. Two things this covers, and one it deliberately does:
    //
    // - A historical BACKFILL republishes old digests with their original
    //   (lower) ids. Those must claim silently and never ring.
    // - A BACKLOG FLUSH after a VM outage republishes N digests in one run.
    //   Only the newest rings, and that is correct rather than lossy:
    //   because the push carries no payload, every one of those N
    //   notifications would fetch push/latest and render the same newest
    //   brief anyway. One ring, one brief, no repetition.
    //
    // Deliberately NOT a created_at freshness window — schema.sql documents
    // created_at as backfillable/historical for daily and weekly briefs, so
    // a freshness check would silently suppress exactly the briefs most
    // worth notifying about. Ingest ordering is the only honest signal.
    const newest = await env.DB.prepare("SELECT MAX(id) AS max_id FROM digests").first();
    if (!newest || Number(newest.max_id) !== Number(id)) return { skipped: "not-newest" };

    // CLAIM-ONCE. PUT /ingest/:id is idempotent by contract — the digest app
    // retries a failed publish on its next run (get_pending_digests) — and a
    // notification has to inherit that idempotency rather than firing once
    // per retry. INSERT OR IGNORE plus meta.changes is an ATOMIC claim; a
    // read-compare-write on a column could let two concurrent ingests of the
    // same id both pass the read.
    //
    // Claiming BEFORE sending makes this at-most-once, not at-least-once,
    // and that is the intended trade: if every send then fails, this digest
    // is never announced. Acceptable because the notification is not the
    // delivery — the digest is already on the site, and the Telegram TL;DR
    // still pings — whereas the failure mode on the other side is a retry
    // loop re-ringing the owner's phone for a digest they already read.
    const claim = await env.DB.prepare(
      "INSERT OR IGNORE INTO push_sent (digest_id, sent_at) VALUES (?, ?)",
    )
      .bind(id, new Date().toISOString())
      .run();
    if (claim?.meta?.changes !== 1) return { skipped: "already-sent" };

    const subs = await env.DB.prepare("SELECT endpoint, fail_count FROM push_subscriptions").all();
    const rows = subs?.results ?? [];
    if (rows.length === 0) return { sent: 0, skipped: "no-subscribers" };

    const subject = vapidSubject(env, siteOrigin);
    const urgent = Boolean(digest.has_attention);

    // Sign BEFORE the fan-out, and treat a signing failure as fatal to the
    // whole run rather than as a per-device error. That distinction is the
    // point, not a refactor: signing depends only on configuration (a
    // malformed VAPID_PRIVATE_JWK, a key that is not P-256), so it either
    // works for every subscription or for none. Folded into the per-device
    // error path instead, a bad key would increment every row's fail_count
    // on every ingest and DELETE the owner's every device inside a day —
    // punishing the subscriptions for a mistake in the secrets. Found by a
    // test that failed for exactly this reason (see test/env.mjs's key
    // fixture comment).
    //
    // One JWT per push-service ORIGIN, not per subscription: `aud` is the
    // endpoint's origin, so devices behind one service share a token.
    let tokens;
    try {
      tokens = await signAll(env, rows, subject);
    } catch {
      return { skipped: "sign-failed", total: rows.length };
    }

    // allSettled, never all: one dead device must not abort the others'
    // delivery, and each outcome is handled individually below.
    const outcomes = await Promise.allSettled(rows.map((row) => deliver(env, row, tokens, urgent)));
    return {
      sent: outcomes.filter((o) => o.status === "fulfilled" && o.value === "ok").length,
      total: rows.length,
    };
  } catch {
    // Nothing upstream can act on this — the caller is waitUntil. Swallow
    // rather than reject: an unhandled rejection in waitUntil is noise in
    // the logs and changes nothing about the already-committed ingest.
    return { skipped: "error" };
  }
}

// One signed Authorization header per distinct push-service origin. Throws
// if the key cannot sign at all — see the call site for why that must not be
// caught per-device.
async function signAll(env, rows, subject) {
  const origins = [...new Set(rows.map((row) => new URL(row.endpoint).origin))];
  const signed = await Promise.all(
    origins.map((origin) => vapidAuthorization(env, origin, subject)),
  );
  return new Map(origins.map((origin, i) => [origin, signed[i]]));
}

async function deliver(env, row, tokens, urgent) {
  let response;
  try {
    response = await fetch(row.endpoint, {
      method: "POST",
      headers: {
        Authorization: tokens.get(new URL(row.endpoint).origin),
        // No body at all — the whole point of the payload-less design. A
        // push service still delivers the wake-up; the service worker then
        // fetches push/latest and renders from live data.
        TTL: String(PUSH_TTL_SECONDS),
        // Push services throttle by urgency to save battery. A flagged
        // brief earns immediate delivery; an ordinary window digest does
        // not need to wake a sleeping phone this second.
        Urgency: urgent ? "high" : "normal",
      },
    });
  } catch {
    await recordFailure(env, row);
    return "error";
  }

  if (response.status === 404 || response.status === 410) {
    // The push service's own way of saying this subscription is dead —
    // the app was uninstalled, or the browser rotated the endpoint. Not a
    // failure to retry: the row is garbage and deleting it is the fix.
    await env.DB.prepare("DELETE FROM push_subscriptions WHERE endpoint = ?")
      .bind(row.endpoint)
      .run();
    return "gone";
  }

  if (response.ok) {
    await env.DB.prepare(
      "UPDATE push_subscriptions SET last_ok_at = ?, fail_count = 0 WHERE endpoint = ?",
    )
      .bind(new Date().toISOString(), row.endpoint)
      .run();
    return "ok";
  }

  // Everything else — 429 rate limiting, a 5xx from the push service, a 401
  // from a VAPID mismatch. Countable rather than fatal: a subscription that
  // keeps failing is eventually dropped, but one bad afternoon does not
  // cost the owner their device.
  await recordFailure(env, row);
  return "failed";
}

async function recordFailure(env, row) {
  const next = Number(row.fail_count ?? 0) + 1;
  if (next >= MAX_PUSH_FAILURES) {
    await env.DB.prepare("DELETE FROM push_subscriptions WHERE endpoint = ?")
      .bind(row.endpoint)
      .run();
    return;
  }
  await env.DB.prepare("UPDATE push_subscriptions SET fail_count = ? WHERE endpoint = ?")
    .bind(next, row.endpoint)
    .run();
}
