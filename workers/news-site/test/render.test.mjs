// Invariant tests: the contracts every refactor step must preserve, asserted
// against live renders of the fixture dataset. Run with `node --test test/`.
//
// These are deliberately MARKER tests, not snapshot tests — golden.mjs owns
// byte-level comparison. A marker here proves a fixture still exercises its
// branch; if a fixture drifts (or a refactor drops a branch), the marker
// disappears and this file fails loudly instead of the golden quietly
// shrinking.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  fetchPath,
  makeEnv,
  loadWorker,
  GOLDEN_PAGES,
  ORIGIN,
  SITE_TOKEN,
  INGEST_KEY,
} from "./env.mjs";
import { VALID_INGEST_PAYLOAD } from "./fixtures.mjs";
import { VAPID_PUBLIC_KEY } from "./env.mjs";

const TRUST_HEADERS = {
  "referrer-policy": "no-referrer",
  "x-robots-tag": "noindex, nofollow",
  "cache-control": "private, no-store",
};

async function page(path) {
  const res = await fetchPath(path);
  return { res, html: await res.text() };
}

// ── every page renders, with the load-bearing headers ─────────────────────

test("every golden page renders 200 with trust headers", async () => {
  for (const { name, path } of GOLDEN_PAGES) {
    const { res, html } = await page(path);
    assert.equal(res.status, 200, `${name} status`);
    for (const [h, v] of Object.entries(TRUST_HEADERS)) {
      assert.equal(res.headers.get(h), v, `${name} header ${h}`);
    }
    assert.equal(
      res.headers.get("content-type"),
      "text/html; charset=utf-8",
      `${name} content-type`,
    );
    assert.ok(html.includes("<!doctype html>"), `${name} is a full page`);
  }
});

test("wrong token is byte-identical to an unknown path (no existence hint)", async () => {
  const wrongToken = await fetchPath("/t/definitely-not-the-token/");
  const unknownPath = await fetchPath("/no/such/path");
  assert.equal(wrongToken.status, 404);
  assert.equal(unknownPath.status, 404);
  assert.equal(await wrongToken.text(), await unknownPath.text());
  for (const h of [...Object.keys(TRUST_HEADERS), "content-type"]) {
    assert.equal(wrongToken.headers.get(h), unknownPath.headers.get(h), `404 header ${h}`);
  }
  for (const [h, v] of Object.entries(TRUST_HEADERS)) {
    assert.equal(wrongToken.headers.get(h), v, `404 trust header ${h}`);
  }
});

// ── i18n parity ───────────────────────────────────────────────────────────

test("STRINGS en/hu key sets are identical", async () => {
  const { STRINGS } = await import("../src/strings.js");
  const en = Object.keys(STRINGS.en);
  const hu = Object.keys(STRINGS.hu);
  assert.ok(en.length >= 80, `en has a plausible key count (${en.length})`);
  assert.deepEqual([...en].sort(), [...hu].sort(), "en/hu key parity");
});

// ── the two extracted string modules ─────────────────────────────────────

test("CSS and CLIENT_SCRIPT literals stay embeddable", async () => {
  // These two files hold the page's stylesheet and client script as
  // template-literal exports (no build config that way). A future edit that
  // introduces any of these sequences would corrupt the literal itself or
  // the inline <style>/<script> embedding — fail here, not in production.
  const { CSS } = await import("../src/css.js");
  const { CLIENT_SCRIPT } = await import("../src/client.js");
  for (const [name, text, closer] of [
    ["CSS", CSS, /<\/style/i],
    ["CLIENT_SCRIPT", CLIENT_SCRIPT, /<\/script/i],
  ]) {
    assert.ok(!text.includes("`"), `${name}: no backticks`);
    assert.ok(!text.includes("${"), `${name}: no template interpolation`);
    assert.ok(!closer.test(text), `${name}: no closing tag`);
  }
});

// ── self-containment ──────────────────────────────────────────────────────

test("no external URLs outside the article body", async () => {
  for (const { name, path } of GOLDEN_PAGES) {
    const { html } = await page(path);
    const outside = html.replace(/<article[\s\S]*?<\/article>/g, "");
    const external = outside.match(/https?:\/\/[^"'\s<>]+/g) ?? [];
    assert.deepEqual(external, [], `${name}: external URLs outside <article>`);
  }
});

// ── the #sN anchor contract ───────────────────────────────────────────────

test("digest section ids are sequential and agree with the TOC", async () => {
  const { html } = await page("d/235");
  const ids = [...html.matchAll(/<h2 id="s(\d+)">/g)].map((m) => Number(m[1]));
  assert.deepEqual(ids, [1, 2, 3], "sequential #sN ids");
  const tocTargets = [...html.matchAll(/class="toc"[\s\S]*?<\/nav>/g)].flatMap((m) =>
    [...m[0].matchAll(/href="#s(\d+)"/g)].map((x) => Number(x[1])),
  );
  assert.deepEqual(tocTargets, ids, "TOC hrefs match section ids");
});

// ── branch markers (one per fixture-covered branch) ───────────────────────

test("hover-to-open is wired for both popovers and gated to hovering pointers", async () => {
  // Marker test, not a behaviour test: the client script is inlined as TEXT
  // and never executed here, so there is no DOM to dispatch mouseenter on.
  // What this pins is the part whose loss would be silent and bad -- the
  // pointer gate. Without it a touch device synthesises mouseenter on tap,
  // opening a panel that then has no pointer to move away from it.
  const { CLIENT_SCRIPT } = await import("../src/client.js");
  assert.match(CLIENT_SCRIPT, /hover: hover\) and \(pointer: fine/, "pointer gate present");
  assert.match(CLIENT_SCRIPT, /hoverPopovers\(\s*settings/, "settings wired");
  assert.match(CLIENT_SCRIPT, /hoverPopovers\(\s*pop/, "search popover wired");
  // Settings must close through its animated close() helper, never by
  // assigning open = false, or the pointer path skips the animation every
  // other close path runs.
  assert.match(
    CLIENT_SCRIPT,
    /hoverPopovers\(\s*settings,[\s\S]*?\n\s*close,\n/,
    "settings closes via close()",
  );
});

test("day headers are an ALL-view affordance; daily/weekly flow flat", async () => {
  // The golden pages CANNOT review this: the fixture set has one daily and
  // one weekly digest, each of which becomes its view's lead card, so
  // daily-en/weekly-en contain zero ledger entries and zero day headers.
  // renderLedgerFor is therefore asserted directly.
  //
  // Why it matters: those views yield at most one card per day-group, so a
  // day header before every card forced each into its own grid row beside a
  // permanently empty second column -- the owner-reported "the text has just
  // half the width". Dropping the headers lets the cards flow two-across.
  const { renderLedgerFor } = await import("../src/render-index.js");
  const rows = [{ created_at: "2026-08-24T20:00:00Z" }, { created_at: "2026-08-23T20:00:00Z" }];
  const row = (r) => `<a class="entry" data-created="${r.created_at}"></a>`;

  const all = renderLedgerFor("all", rows, "en-GB", row);
  assert.ok(all.includes('class="dayhead"'), "all view groups by day");
  assert.equal(all.match(/class="dayhead"/g).length, 2, "one header per day");

  for (const view of ["daily", "weekly"]) {
    const flat = renderLedgerFor(view, rows, "en-GB", row);
    assert.ok(!flat.includes("dayhead"), `${view} view emits no day headers`);
    assert.equal(flat.match(/class="entry"/g).length, 2, `${view} keeps every row`);
  }
});

test("index EN: NOW section, week rail, degraded badge, kind badges", async () => {
  const { html } = await page("");
  assert.match(html, /class="now"/, "NOW section (2-appearance arc via stable key)");
  assert.match(html, /class="weekrail"/, "week rail");
  assert.match(html, /flag-degraded/, "degraded badge from failed_sources");
  assert.match(html, /flag-daily/, "daily/weekly kind badge");
});

test("digest 235 EN: arcs line, what-changed block, source key, stripped styles, cite titles", async () => {
  const { html } = await page("d/235");
  assert.match(html, /class="arc"/, "story-arc chip (recurring topic)");
  assert.match(html, /class="deltas"/, "what-changed block from deltas");
  assert.match(html, /class="sourcekey"/, "source key colophon");
  assert.match(html, /sk-failed/, "failed source pill in the source key");
  assert.ok(
    !/<article[\s\S]*?style="color: red"[\s\S]*?<\/article>/.test(html),
    "inline style stripped from body",
  );
  assert.match(html, /example\.com\/markets-report/, "citation link survives");
});

test("digest 235 HU: deltas suppressed, translated body used", async () => {
  const { html } = await page("hu/d/235");
  assert.ok(!html.includes('class="deltas"'), "what-changed suppressed on /hu/ (EN-only prose)");
  assert.match(html, /Folytatódik a piaci esés/, "HU body rendered");
  // class= form, not the bare name — the shipped CSS defines .en-only-note
  // on every page, so a bare-substring check would always match.
  assert.ok(!html.includes('class="en-only-note"'), "no fallback note when translation exists");
});

test("digest 234 HU: falls back to EN body with the note", async () => {
  const { res, html } = await page("hu/d/234");
  assert.equal(res.status, 200);
  assert.match(html, /class="en-only-note"/, "EN-fallback note on untranslated digest");
  assert.match(html, /Markets slide, day two/, "EN body served");
});

test("arc page: chain of 2, momentum, context primer, per-appearance delta", async () => {
  const { html } = await page("a/markets-slide");
  assert.match(html, /Markets slide continues/, "latest appearance label");
  assert.match(html, /Markets slide, day two/, "earlier appearance (different slug, same key)");
  assert.match(html, /class="arccontext"/, "context primer disclosure");
  assert.match(html, /deltaprev/, "appearance-level delta markup");
});

test("unknown arc identity 404s indistinguishably", async () => {
  const res = await fetchPath("a/never-heard-of-it");
  assert.equal(res.status, 404);
  assert.equal(await res.text(), "Not Found");
});

test("daily and weekly views list only their kind", async () => {
  const daily = await page("daily/");
  assert.match(daily.html, /Daily brief for Monday/);
  assert.ok(!daily.html.includes("The week in one page"), "no weekly rows in daily view");
  const weekly = await page("weekly/");
  assert.match(weekly.html, /The week in one page/);
  assert.ok(!weekly.html.includes("Daily brief for Monday"), "no daily rows in weekly view");
});

test("search: results for a match, empty state otherwise", async () => {
  const hit = await page("search?q=markets");
  assert.match(hit.html, /<mark>/, "FTS snippet marks re-mapped to <mark>");
  const miss = await page("search?q=zzzznothing");
  assert.equal(miss.res.status, 200);
  assert.ok(!miss.html.includes("<mark>"), "no marks on empty result");
});

// ── ingest ────────────────────────────────────────────────────────────────

function ingestInit(payload, key = INGEST_KEY) {
  return {
    method: "PUT",
    headers: { "x-ingest-key": key, "content-type": "application/json" },
    body: JSON.stringify(payload),
  };
}

test("ingest: happy path writes and 200s", async () => {
  const worker = await loadWorker();
  const writes = [];
  const res = await worker.fetch(
    new Request(`${ORIGIN}/ingest/300`, ingestInit(VALID_INGEST_PAYLOAD)),
    makeEnv({ writes }),
  );
  assert.equal(res.status, 200, await res.clone().text());
  assert.equal(writes.length, 1);
  assert.equal(writes[0].table, "digests");
});

test("ingest: wrong key is 401, missing key is 401", async () => {
  for (const key of ["wrong-key", ""]) {
    const res = await fetchPath("/ingest/300", { init: ingestInit(VALID_INGEST_PAYLOAD, key) });
    assert.equal(res.status, 401);
  }
});

test("ingest: each validator rejects its malformed field", async () => {
  const cases = [
    ["tldr", { ...VALID_INGEST_PAYLOAD, tldr: "" }],
    ["created_at", { ...VALID_INGEST_PAYLOAD, created_at: "not-a-date" }],
    ["item_count", { ...VALID_INGEST_PAYLOAD, item_count: -1 }],
    ["half-translation", { ...VALID_INGEST_PAYLOAD, tldr_hu: "csak ez" }],
    ["source_counts", { ...VALID_INGEST_PAYLOAD, source_counts: [] }],
    ["failed_sources", { ...VALID_INGEST_PAYLOAD, failed_sources: "rss" }],
    ["topics", { ...VALID_INGEST_PAYLOAD, topics: [{ label: "no slug" }] }],
    ["deltas", { ...VALID_INGEST_PAYLOAD, deltas: [{ slug: "x" }] }],
  ];
  for (const [label, payload] of cases) {
    const res = await fetchPath("/ingest/300", { init: ingestInit(payload) });
    assert.equal(res.status, 400, `${label} rejected`);
  }
});

// ── PWA shell (PLAN.md §11.7) ─────────────────────────────────────────────

test("manifest: token-scoped, one app id across both languages", async () => {
  const en = await fetchPath("manifest.webmanifest");
  assert.equal(en.status, 200);
  assert.equal(
    en.headers.get("content-type"),
    "application/manifest+json; charset=utf-8",
    "manifest content-type",
  );
  for (const [h, v] of Object.entries(TRUST_HEADERS)) {
    assert.equal(en.headers.get(h), v, `manifest header ${h}`);
  }
  const m = JSON.parse(await en.text());
  assert.equal(m.start_url, `/t/${SITE_TOKEN}/`);
  assert.equal(m.scope, `/t/${SITE_TOKEN}/`);
  assert.equal(m.display, "standalone");
  assert.ok(m.icons.length >= 1, "declares icons");
  assert.ok(
    m.icons.some((i) => i.purpose === "maskable"),
    "declares a maskable icon (Android crops the 'any' one)",
  );

  // start_url follows the language the reader installed from, but `id`
  // does NOT — otherwise EN and HU become two Home Screen apps over one
  // archive, each with half the history.
  const hu = JSON.parse(await (await fetchPath("hu/manifest.webmanifest")).text());
  assert.equal(hu.start_url, `/t/${SITE_TOKEN}/hu/`);
  assert.equal(hu.id, m.id, "one installed app, not one per language");
  assert.equal(hu.scope, m.scope);
});

test("manifest icons stay OFF the token path", async () => {
  // The installed app record persists these URLs. Keeping them tokenless
  // means it embeds the capability token once (start_url/scope/id), not
  // once more per icon.
  const m = JSON.parse(await (await fetchPath("manifest.webmanifest")).text());
  for (const icon of m.icons) {
    assert.ok(!icon.src.includes(SITE_TOKEN), `${icon.src} must not carry the token`);
    assert.ok(icon.src.startsWith("/"), `${icon.src} is root-absolute`);
  }
});

test("service worker: served, scoped to the token root, and cache-free", async () => {
  const res = await fetchPath("sw.js");
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("content-type"), "text/javascript; charset=utf-8");
  // no-store, not a long cache: the update check for a worker is a byte
  // comparison of this very response.
  assert.equal(res.headers.get("cache-control"), "private, no-store");
  const src = await res.text();

  assert.match(src, /addEventListener\("push"/, "handles push");
  assert.match(src, /addEventListener\("fetch"/, "handles fetch (Chrome installability)");
  assert.match(src, /addEventListener\("notificationclick"/, "handles the tap");
  assert.match(src, /showNotification/, "always shows something");
  assert.ok(src.includes(`/t/${SITE_TOKEN}/`), "CFG carries the token root as its scope");

  // THE guardrail of §11.7, as a test rather than a comment: no Cache API,
  // anywhere. This is the line that let PWA stop being deferred — private
  // digest content must never reach disk, so Cache-Control: private,
  // no-store stays honest. A future change that adds caching here is not an
  // increment on this feature, it is the still-deferred offline reader.
  for (const forbidden of ["caches.", "CacheStorage", "cache.put", "cache.match", "addAll("]) {
    assert.ok(!src.includes(forbidden), `service worker must not use ${forbidden}`);
  }
});

test("PWA routes 404 indistinguishably on a wrong token", async () => {
  const unknownPath = await fetchPath("/no/such/path");
  const unknownBody = await unknownPath.text();
  for (const path of [
    "/t/definitely-not-the-token/manifest.webmanifest",
    "/t/definitely-not-the-token/hu/manifest.webmanifest",
    "/t/definitely-not-the-token/sw.js",
  ]) {
    const res = await fetchPath(path);
    assert.equal(res.status, 404, `${path} status`);
    assert.equal(await res.text(), unknownBody, `${path} body`);
  }
});

test("icons: real PNGs, tokenless, long-cached", async () => {
  for (const path of ["/icon-512.png", "/icon-maskable-512.png", "/apple-touch-icon.png"]) {
    const res = await fetchPath(path);
    assert.equal(res.status, 200, `${path} status`);
    assert.equal(res.headers.get("content-type"), "image/png", `${path} content-type`);
    assert.equal(res.headers.get("cache-control"), "public, max-age=86400", `${path} cache`);
    const bytes = new Uint8Array(await res.arrayBuffer());
    // PNG magic number — proves the base64 survived source formatting.
    assert.deepEqual([...bytes.slice(0, 4)], [0x89, 0x50, 0x4e, 0x47], `${path} is a PNG`);
  }
});

test("every page links its own language's manifest and registers the worker", async () => {
  for (const { name, path } of GOLDEN_PAGES) {
    const { html } = await page(path);
    const lang = path.startsWith("hu/") ? "hu/" : "";
    assert.ok(
      html.includes(`<link rel="manifest" href="/t/${SITE_TOKEN}/${lang}manifest.webmanifest">`),
      `${name} links its own manifest`,
    );
    assert.ok(
      html.includes(`serviceWorker.register("/t/${SITE_TOKEN}/sw.js")`),
      `${name} registers`,
    );
    assert.ok(html.includes('name="mobile-web-app-capable"'), `${name} is standalone-capable`);
  }
});

// ── Web Push subscriptions (PLAN.md §11.7, PR B) ──────────────────────────

const FCM = "https://fcm.googleapis.com/fcm/send/abc123";

function subPayload(over = {}) {
  return {
    endpoint: FCM,
    keys: {
      p256dh: "BLc4xRzKlKORKWlbdgFaBrrPK3ydWAHo4M0gs0i1oEKgPpWG5F",
      auth: "8eDyX_uCN0XRhSbY5hs7Hg",
    },
    ...over,
  };
}

async function pushFetch(action, body, env) {
  const worker = await loadWorker();
  return worker.fetch(
    new Request(`${ORIGIN}/t/${SITE_TOKEN}/push/${action}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }),
    env,
  );
}

test("push/key: serves the VAPID key, 503 when unconfigured", async () => {
  const worker = await loadWorker();
  const ok = await worker.fetch(new Request(`${ORIGIN}/t/${SITE_TOKEN}/push/key`), makeEnv());
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).key, VAPID_PUBLIC_KEY);

  // The state every deployment passes through: this Worker auto-deploys on
  // merge, so the code always lands before the secrets. It must degrade,
  // not fault.
  const off = await worker.fetch(
    new Request(`${ORIGIN}/t/${SITE_TOKEN}/push/key`),
    makeEnv({ push: false }),
  );
  assert.equal(off.status, 503);
});

test("push/subscribe: stores, and re-subscribing is idempotent", async () => {
  const pushSubs = [];
  const env = makeEnv({ pushSubs });
  assert.equal((await pushFetch("subscribe", subPayload({ lang: "hu" }), env)).status, 200);
  assert.equal(pushSubs.length, 1);
  assert.equal(pushSubs[0].lang, "hu");
  assert.equal(pushSubs[0].endpoint, FCM);

  // Same device, new language — one row, updated, not a second row.
  assert.equal((await pushFetch("subscribe", subPayload({ lang: "en" }), env)).status, 200);
  assert.equal(pushSubs.length, 1);
  assert.equal(pushSubs[0].lang, "en");
});

test("push/subscribe: rejects an endpoint outside the push-service allowlist", async () => {
  // THE security control in this PR: PR C's sender POSTs to whatever is
  // stored here, so an unchecked endpoint makes this Worker a blind proxy.
  const pushSubs = [];
  const env = makeEnv({ pushSubs });
  for (const endpoint of [
    "https://evil.example.com/collect",
    "http://fcm.googleapis.com/fcm/send/x", // right host, wrong scheme
    "https://fcm.googleapis.com.evil.example/x", // suffix-looking, not a suffix
    "not-a-url",
  ]) {
    const res = await pushFetch("subscribe", subPayload({ endpoint }), env);
    assert.equal(res.status, 400, `${endpoint} rejected`);
  }
  assert.equal(pushSubs.length, 0, "nothing stored");

  // Every real engine's host still works.
  for (const endpoint of [
    "https://fcm.googleapis.com/fcm/send/x",
    "https://updates.push.services.mozilla.com/wpush/v2/x",
    "https://web.push.apple.com/x",
    "https://xyz.notify.windows.com/w/?token=x",
  ]) {
    const res = await pushFetch("subscribe", subPayload({ endpoint }), env);
    assert.equal(res.status, 200, `${endpoint} accepted`);
  }
});

test("push/subscribe: each validator rejects its malformed field", async () => {
  const env = makeEnv({ pushSubs: [] });
  const cases = [
    ["no endpoint", subPayload({ endpoint: undefined })],
    ["no keys", { endpoint: FCM }],
    ["bad p256dh", subPayload({ keys: { p256dh: "not base64!", auth: "aGk" } })],
    ["no auth", subPayload({ keys: { p256dh: "aGk" } })],
    ["label not a string", subPayload({ label: 42 })],
  ];
  for (const [label, payload] of cases) {
    assert.equal((await pushFetch("subscribe", payload, env)).status, 400, label);
  }
});

test("push/subscribe: the cap never blocks a device refreshing its own row", async () => {
  // A cap checked before the upsert without this ordering would lock out
  // the exact case that matters most — browsers rotate endpoints, and a
  // full table would refuse the owner's own device its refresh.
  const pushSubs = Array.from({ length: 20 }, (_, i) => ({
    endpoint: `https://fcm.googleapis.com/fcm/send/seed${i}`,
    lang: "en",
  }));
  const env = makeEnv({ pushSubs });

  const refresh = await pushFetch(
    "subscribe",
    subPayload({ endpoint: pushSubs[3].endpoint, lang: "hu" }),
    env,
  );
  assert.equal(refresh.status, 200, "existing device refreshes at the cap");
  assert.equal(pushSubs.length, 20);
  assert.equal(pushSubs[3].lang, "hu");

  const newDevice = await pushFetch("subscribe", subPayload(), env);
  assert.equal(newDevice.status, 429, "a NEW device is refused at the cap");
  assert.match(
    (await newDevice.json()).error,
    /DELETE FROM push_subscriptions/,
    "names the remedy",
  );
});

test("push/unsubscribe: removes, is idempotent, and works unconfigured", async () => {
  const pushSubs = [{ endpoint: FCM, lang: "en" }];
  const env = makeEnv({ pushSubs });
  assert.equal((await pushFetch("unsubscribe", { endpoint: FCM }, env)).status, 200);
  assert.equal(pushSubs.length, 0);
  // Deleting what is not there is the same success — the caller wanted to
  // end up unsubscribed, and they are.
  assert.equal((await pushFetch("unsubscribe", { endpoint: FCM }, env)).status, 200);

  // Withdrawing consent must never be the thing that fails closed.
  const off = makeEnv({ pushSubs: [{ endpoint: FCM, lang: "en" }], push: false });
  assert.equal((await pushFetch("unsubscribe", { endpoint: FCM }, off)).status, 200);
});

test("push/latest: answers in the subscription's language, EN when unknown", async () => {
  const pushSubs = [{ endpoint: FCM, lang: "hu" }];
  const env = makeEnv({ pushSubs });

  const hu = await (await pushFetch("latest", { endpoint: FCM }, env)).json();
  assert.ok(hu.url.startsWith(`/t/${SITE_TOKEN}/hu/d/`), "deep-links into the HU page");
  assert.ok(hu.title.length > 0);
  assert.ok(hu.body.length <= 160, "trimmed for a lock screen");

  // A worker with no subscription (or an unrecognized one) is not an
  // error — English is the honest fallback, same as the offline copy.
  const en = await (await pushFetch("latest", { endpoint: null }, env)).json();
  assert.ok(en.url.startsWith(`/t/${SITE_TOKEN}/d/`), "EN deep link");
  assert.notEqual(en.url, hu.url, "the deep links differ by language");
  assert.notEqual(en.body, hu.body, "the TL;DR is the translated one");

  // The TITLES are deliberately identical here, and that is the contract
  // rather than an accident: the newest fixture is a window digest, and
  // this file's strings header records that "digest #N" is one of the
  // micro-labels the HU pages leave in English. A notification must not
  // name the thing differently from the page it opens.
  assert.equal(en.title, hu.title, "window titles match across languages, on purpose");
  assert.equal(en.id, hu.id);
});

test("push routes: wrong token and wrong method 404 indistinguishably", async () => {
  const worker = await loadWorker();
  const unknown = await fetchPath("/no/such/path");
  const unknownBody = await unknown.text();

  for (const action of ["key", "subscribe", "unsubscribe", "latest"]) {
    const res = await worker.fetch(
      new Request(`${ORIGIN}/t/definitely-not-the-token/push/${action}`, {
        method: action === "key" ? "GET" : "POST",
      }),
      makeEnv(),
    );
    assert.equal(res.status, 404, `${action} wrong token`);
    assert.equal(await res.text(), unknownBody, `${action} body`);
  }

  // GET on a POST-only endpoint reveals nothing either — including that
  // push/latest takes a body carrying a capability, which is why it is a
  // POST in the first place.
  for (const action of ["subscribe", "unsubscribe", "latest"]) {
    const res = await worker.fetch(
      new Request(`${ORIGIN}/t/${SITE_TOKEN}/push/${action}`),
      makeEnv(),
    );
    assert.equal(res.status, 404, `${action} via GET`);
  }
});

test("the settings row ships hidden, in both languages, on every page", async () => {
  for (const { name, path } of GOLDEN_PAGES) {
    const { html } = await page(path);
    const lang = path.startsWith("hu/") ? "hu" : "en";
    assert.ok(html.includes('class="settingsrow pushrow" hidden'), `${name} row starts hidden`);
    assert.ok(html.includes(`data-lang="${lang}"`), `${name} carries its language`);
    // The endpoints have no /hu/ variant, so EVERY page — Hungarian
    // included — must post to the token root. Deriving this from the page's
    // own language would 404 every subscribe made from a /hu/ page, and
    // only from a /hu/ page.
    assert.ok(
      html.includes(`data-base="/t/${SITE_TOKEN}/"`),
      `${name} posts to the token root, not its language root`,
    );
    // Whether push is possible is a client-side question with several
    // distinct no's; the row must not appear until the script knows which.
    assert.ok(html.includes('class="pushtoggle" hidden'), `${name} control starts hidden`);
  }
});

// ── the sender (PLAN.md §11.7, PR C) ──────────────────────────────────────

// Intercept the outbound push. Returns the recorded requests so a test can
// assert on the VAPID header, the TTL, and the fact that there is no body.
function stubPushService(responder = () => new Response(null, { status: 201 })) {
  const calls = [];
  const real = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    if (/googleapis|mozilla|apple|windows/.test(url)) {
      calls.push({ url, init });
      return responder(url, init, calls.length);
    }
    return real(input, init);
  };
  return { calls, restore: () => (globalThis.fetch = real) };
}

// The newest fixture digest — the sender only ever notifies for that id.
const NEWEST_ID = 235;

async function ingestNewest(env) {
  return fetchPath(`/ingest/${NEWEST_ID}`, {
    env,
    init: {
      method: "PUT",
      headers: { "x-ingest-key": INGEST_KEY, "content-type": "application/json" },
      body: JSON.stringify(VALID_INGEST_PAYLOAD),
    },
  });
}

test("sender: fans out on ingest, with a verifiable VAPID token and no body", async () => {
  const pushSubs = [{ endpoint: FCM, lang: "en", fail_count: 0 }];
  const env = makeEnv({ pushSubs, pushSentIds: new Set() });
  const push = stubPushService();
  try {
    assert.equal((await ingestNewest(env)).status, 200);
    assert.equal(push.calls.length, 1, "one device, one push");
  } finally {
    push.restore();
  }

  const { init } = push.calls[0];
  assert.equal(init.method, "POST");
  assert.ok(!init.body, "payload-less: nothing to encrypt, nothing to leak");
  assert.equal(init.headers.TTL, String(4 * 60 * 60));
  assert.equal(init.headers.Urgency, "normal");

  // Actually VERIFY the signature rather than pattern-matching the header.
  // A JWT that is well-shaped but wrongly signed is the single most likely
  // way this feature fails silently in production: push services answer 401
  // and nothing else ever says why.
  const auth = init.headers.Authorization;
  const [, token, key] = auth.match(/^vapid t=([^,]+), k=(.+)$/);
  const [header, payload, signature] = token.split(".");

  const b64uToBytes = (s) =>
    Uint8Array.from(
      atob(
        s
          .replace(/-/g, "+")
          .replace(/_/g, "/")
          .padEnd(Math.ceil(s.length / 4) * 4, "="),
      ),
      (c) => c.charCodeAt(0),
    );
  const pub = await crypto.subtle.importKey(
    "raw",
    b64uToBytes(key),
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["verify"],
  );
  const valid = await crypto.subtle.verify(
    { name: "ECDSA", hash: "SHA-256" },
    pub,
    b64uToBytes(signature),
    new TextEncoder().encode(`${header}.${payload}`),
  );
  assert.ok(valid, "the VAPID signature verifies against the advertised key");

  const claims = JSON.parse(new TextDecoder().decode(b64uToBytes(payload)));
  // `aud` is the PUSH SERVICE's origin, not this site's — the most common
  // cause of a 401 is getting that backwards.
  assert.equal(claims.aud, "https://fcm.googleapis.com");
  assert.equal(claims.sub, ORIGIN, "falls back to the site origin as contact");
  assert.ok(claims.exp > Math.floor(Date.now() / 1000), "not already expired");
  assert.ok(claims.exp - Math.floor(Date.now() / 1000) <= 24 * 60 * 60, "Apple rejects exp > 24h");
});

test("sender: claim-once — a retried ingest does not re-notify", async () => {
  // PUT /ingest/:id is idempotent BY CONTRACT (the app retries a failed
  // publish on its next run), so the notification has to inherit that.
  const pushSubs = [{ endpoint: FCM, lang: "en", fail_count: 0 }];
  const env = makeEnv({ pushSubs, pushSentIds: new Set() });
  const push = stubPushService();
  try {
    await ingestNewest(env);
    await ingestNewest(env);
    await ingestNewest(env);
    assert.equal(push.calls.length, 1, "three ingests of the same id, one push");
  } finally {
    push.restore();
  }
});

test("sender: newest-only — a backfilled older digest claims silently", async () => {
  const pushSubs = [{ endpoint: FCM, lang: "en", fail_count: 0 }];
  const env = makeEnv({ pushSubs, pushSentIds: new Set() });
  const push = stubPushService();
  try {
    // 234 exists in the fixtures and is NOT the newest. A historical
    // backfill of it must not ring — the reader would open push/latest and
    // be shown 235 anyway.
    const res = await fetchPath("/ingest/234", {
      env,
      init: {
        method: "PUT",
        headers: { "x-ingest-key": INGEST_KEY, "content-type": "application/json" },
        body: JSON.stringify(VALID_INGEST_PAYLOAD),
      },
    });
    assert.equal(res.status, 200, "the ingest itself still succeeds");
    assert.equal(push.calls.length, 0, "but nothing rings");
  } finally {
    push.restore();
  }
});

test("sender: 410 Gone prunes the subscription, 429 only counts against it", async () => {
  const gone = "https://fcm.googleapis.com/fcm/send/dead";
  const flaky = "https://web.push.apple.com/flaky";
  const pushSubs = [
    { endpoint: gone, lang: "en", fail_count: 0 },
    { endpoint: flaky, lang: "en", fail_count: 0 },
  ];
  const env = makeEnv({ pushSubs, pushSentIds: new Set() });
  const push = stubPushService((url) =>
    url === gone ? new Response(null, { status: 410 }) : new Response(null, { status: 429 }),
  );
  try {
    await ingestNewest(env);
  } finally {
    push.restore();
  }

  assert.equal(pushSubs.length, 1, "the dead endpoint is deleted outright");
  assert.equal(pushSubs[0].endpoint, flaky);
  // A rate-limited push service is a bad afternoon, not a dead device.
  assert.equal(pushSubs[0].fail_count, 1, "counted, not dropped");
});

test("sender: a success resets fail_count and stamps last_ok_at", async () => {
  const pushSubs = [{ endpoint: FCM, lang: "en", fail_count: 3 }];
  const env = makeEnv({ pushSubs, pushSentIds: new Set() });
  const push = stubPushService();
  try {
    await ingestNewest(env);
  } finally {
    push.restore();
  }
  assert.equal(pushSubs[0].fail_count, 0, "a recovered device is forgiven");
  assert.ok(pushSubs[0].last_ok_at, "and its last success is recorded");
});

test("sender: unconfigured push never reaches a push service", async () => {
  const pushSubs = [{ endpoint: FCM, lang: "en", fail_count: 0 }];
  const env = makeEnv({ pushSubs, pushSentIds: new Set(), push: false });
  const push = stubPushService();
  try {
    assert.equal((await ingestNewest(env)).status, 200, "ingest is unaffected");
    assert.equal(push.calls.length, 0);
  } finally {
    push.restore();
  }
});

test("sender: a push service failing never fails the ingest", async () => {
  const pushSubs = [{ endpoint: FCM, lang: "en", fail_count: 0 }];
  const env = makeEnv({ pushSubs, pushSentIds: new Set() });
  const push = stubPushService(() => {
    throw new Error("network down");
  });
  try {
    // The digest is already committed by the time the fan-out starts; an
    // announcement that cannot be delivered must not undo it.
    assert.equal((await ingestNewest(env)).status, 200);
  } finally {
    push.restore();
  }
  assert.equal(pushSubs[0].fail_count, 1);
});

test("sender: a broken VAPID key costs no subscriptions", async () => {
  // The failure mode this guards against: signing depends only on
  // configuration, so a malformed key fails for EVERY device, every time.
  // Charged to the per-device error path, it would increment every row's
  // fail_count on every ingest and delete the owner's entire fleet within a
  // day — over a typo in a secret. The subscriptions are not at fault and
  // must not pay.
  const pushSubs = [
    { endpoint: FCM, lang: "en", fail_count: 4 }, // one away from the cull
    { endpoint: "https://web.push.apple.com/x", lang: "en", fail_count: 4 },
  ];
  const env = makeEnv({ pushSubs, pushSentIds: new Set() });
  env.VAPID_PRIVATE_JWK = "{}"; // configured-looking, unusable
  const push = stubPushService();
  try {
    assert.equal((await ingestNewest(env)).status, 200, "ingest is unaffected");
    assert.equal(push.calls.length, 0, "nothing is sent");
  } finally {
    push.restore();
  }
  assert.equal(pushSubs.length, 2, "both devices survive");
  assert.deepEqual(
    pushSubs.map((s) => s.fail_count),
    [4, 4],
    "and are not blamed for it",
  );
});
