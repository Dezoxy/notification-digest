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
