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

export function makeEnv({ writes } = {}) {
  return {
    SITE_TOKEN,
    INGEST_KEY,
    DB: makeDb({ writes }),
  };
}

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

export async function fetchPath(path, { init, entry } = {}) {
  const worker = await loadWorker(entry);
  const url = path.startsWith("/") ? `${ORIGIN}${path}` : `${ORIGIN}/t/${SITE_TOKEN}/${path}`;
  return worker.fetch(new Request(url, init), makeEnv());
}

export { FROZEN_NOW, SITE_TOKEN, INGEST_KEY, DIGESTS, ARC_CONTEXTS };
