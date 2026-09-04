// Shared test environment. Importing this module has two side effects, both
// required BEFORE worker.js handles a request:
//
// 1. Freezes the clock. handleIndexPage (the NOW section + current-week
//    resolution), handleArcPage (momentum labels), and the week rail all read
//    the real clock; goldens can only be byte-stable if `new Date()` and
//    `Date.now()` return one fixed instant. Explicit-argument construction
//    passes through untouched, so parsing fixture timestamps still works.
//
// 2. Shims crypto.subtle.timingSafeEqual — a Cloudflare Workers extension
//    Node's webcrypto does not have. Constant-time behavior is irrelevant in
//    tests; equality semantics are what the auth path needs.

import { DIGESTS, ARC_CONTEXTS, FROZEN_NOW, SITE_TOKEN, INGEST_KEY } from "./fixtures.mjs";
import { makeDb } from "./stub-db.mjs";

const FROZEN_MS = new Date(FROZEN_NOW).getTime();

const RealDate = Date;
class FrozenDate extends RealDate {
  constructor(...args) {
    if (args.length === 0) super(FROZEN_MS);
    else super(...args);
  }
  static now() {
    return FROZEN_MS;
  }
}
globalThis.Date = FrozenDate;

if (!crypto.subtle.timingSafeEqual) {
  crypto.subtle.timingSafeEqual = (a, b) => {
    const x = new Uint8Array(a);
    const y = new Uint8Array(b);
    if (x.length !== y.length) return false;
    let diff = 0;
    for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
    return diff === 0;
  };
}

// worker.js is imported AFTER the patches above (dynamic import so module
// evaluation order is explicit, not hoisted). golden.mjs --bundle points this
// at the wrangler dry-run output instead, proving the DEPLOYED artifact
// renders identically to the module graph.
let workerPromise = null;
export function loadWorker(entry = new URL("../worker.js", import.meta.url).href) {
  workerPromise ??= import(entry).then((m) => m.default);
  return workerPromise;
}

export function makeEnv({ writes, pushSubs, pushSentIds, pushSentAt, push = true } = {}) {
  return {
    SITE_TOKEN,
    INGEST_KEY,
    // Push (PLAN.md §11.7) is CONFIGURED by default in tests, because the
    // interesting behavior is what the endpoints do when it is. Pass
    // { push: false } to exercise the unconfigured path, which is the state
    // a real deployment is in between this code merging and the secrets
    // being set — the Worker auto-deploys on merge, so that window always
    // exists and must degrade to 503 rather than 500.
    ...(push ? { VAPID_PUBLIC_KEY, VAPID_PRIVATE_JWK } : {}),
    DB: makeDb({ writes, pushSubs, pushSentIds, pushSentAt }),
  };
}

// A REAL P-256 key pair, generated once for the test suite and committed on
// purpose — the same category of fixture as SITE_TOKEN's "goldentesttoken",
// and worth nothing to anyone: it signs pushes to a push service that does
// not exist, and production's keys live in Cloudflare secrets.
//
// Real, rather than a placeholder, because a placeholder cannot sign. The
// first version of this file carried a well-formed public key next to
// VAPID_PRIVATE_JWK: "{}", and every sender test then failed at importKey —
// which is how the fan-out's error handling turned out to be charging
// signing failures to the SUBSCRIPTIONS (see notify.js's signAll).
// The two halves are a matching pair: the suite verifies a produced VAPID
// signature against the advertised public key, so they cannot drift apart
// without a test failing.
export const VAPID_PUBLIC_KEY =
  "BLxWERpS2Y6OKzlO4UWS_jpR63AoABN-lfSBcvjbseelZ88FBa0z31Qw0deRCuOj6OQMm63J4qLJlU1vxI-TS7M";

export const VAPID_PRIVATE_JWK = JSON.stringify({
  key_ops: ["sign"],
  ext: true,
  kty: "EC",
  x: "vFYRGlLZjo4rOU7hRZL-OlHrcCgAE36V9IFy-Nux56U",
  y: "Z88FBa0z31Qw0deRCuOj6OQMm63J4qLJlU1vxI-TS7M",
  crv: "P-256",
  d: "RiSof-pDOCLxruE7SS-WUnmOOXGzc4zhxPsAqXAHew8",
});

export const ORIGIN = "https://news.toomhorvath.com";

// The golden pages. Paths are relative to the token root; `name` is the
// golden filename. Every later refactor PR is judged against these bytes.
export const GOLDEN_PAGES = [
  { name: "index-en", path: "" },
  { name: "index-hu", path: "hu/" },
  { name: "digest-235-en", path: "d/235" },
  { name: "digest-235-hu", path: "hu/d/235" },
  { name: "search-en", path: "search?q=markets" },
  { name: "about-en", path: "about" },
  { name: "arc-en", path: "a/markets-slide" },
  // The daily and weekly index views had NO golden coverage until the
  // 2026-08-27 two-column pass, so any change to how they render produced a
  // zero golden diff -- nothing to review. Their fixture set is thin (one
  // daily, one weekly digest), so these pin the MARKUP rather than
  // demonstrating a dense grid; see the DIGESTS fixture if that ever needs
  // to change.
  { name: "daily-en", path: "daily/" },
  { name: "weekly-en", path: "weekly/" },
];

export async function fetchPath(path, { init, entry, env } = {}) {
  const worker = await loadWorker(entry);
  const url = path.startsWith("/") ? `${ORIGIN}${path}` : `${ORIGIN}/t/${SITE_TOKEN}/${path}`;
  return fetchWithCtx(worker, new Request(url, init), env ?? makeEnv());
}

// The ExecutionContext stub, and the reason it exists at all: handleIngest's
// push fan-out (PLAN.md §11.7) runs inside ctx.waitUntil, so a stub that
// merely ACCEPTED the promise and dropped it would let every claim-once,
// prune-on-410 and newest-only test pass while asserting against work that
// had not happened yet — green, and testing nothing. §11.7 names this as a
// guardrail for exactly that reason; it is the same class of trap as
// stub-db's throw-on-unknown-SQL.
//
// So: collect, then AWAIT, before the response is handed back. Tests can
// assert on the resulting database state immediately, with no polling and
// no sleeps.
export async function fetchWithCtx(worker, request, env) {
  const pending = [];
  const ctx = {
    waitUntil(promise) {
      pending.push(promise);
    },
    passThroughOnException() {},
  };
  const response = await worker.fetch(request, env, ctx);
  await Promise.allSettled(pending);
  return response;
}

export { FROZEN_NOW, SITE_TOKEN, INGEST_KEY, DIGESTS, ARC_CONTEXTS };
