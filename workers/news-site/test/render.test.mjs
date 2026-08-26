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
import { readFileSync } from "node:fs";

import {
  fetchPath, makeEnv, loadWorker, GOLDEN_PAGES, ORIGIN,
  SITE_TOKEN, INGEST_KEY,
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

test("all seven golden pages render 200 with trust headers", async () => {
  for (const { name, path } of GOLDEN_PAGES) {
    const { res, html } = await page(path);
    assert.equal(res.status, 200, `${name} status`);
    for (const [h, v] of Object.entries(TRUST_HEADERS)) {
      assert.equal(res.headers.get(h), v, `${name} header ${h}`);
    }
    assert.equal(res.headers.get("content-type"), "text/html; charset=utf-8", `${name} content-type`);
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

test("STRINGS en/hu key sets are identical", () => {
  // Until the module split exports STRINGS, extract key names from source:
  // keys are the `identifier:` lines at one indent level inside `en: {` /
  // `hu: {`. Brace-depth scanning keeps nested objects (none today) safe.
  // TODO(split PR): replace with `import { STRINGS } from "../src/strings.js"`.
  const src = readFileSync(new URL("../worker.js", import.meta.url), "utf8");
  const keysOf = (langTag) => {
    const start = src.indexOf(`  ${langTag}: {`);
    assert.ok(start > 0, `found ${langTag} block`);
    let depth = 0;
    let i = src.indexOf("{", start);
    const keys = [];
    for (; i < src.length; i++) {
      const ch = src[i];
      if (ch === "{") depth++;
      else if (ch === "}") {
        depth--;
        if (depth === 0) break;
      } else if (ch === "\n" && depth === 1) {
        const m = src.slice(i + 1, src.indexOf("\n", i + 1)).match(/^\s*([A-Za-z_$][\w$]*):/);
        if (m) keys.push(m[1]);
      }
    }
    return keys;
  };
  const en = keysOf("en");
  const hu = keysOf("hu");
  assert.ok(en.length >= 80, `en has a plausible key count (${en.length})`);
  assert.deepEqual([...en].sort(), [...hu].sort(), "en/hu key parity");
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
  const tocTargets = [...html.matchAll(/class="toc"[\s\S]*?<\/nav>/g)]
    .flatMap((m) => [...m[0].matchAll(/href="#s(\d+)"/g)].map((x) => Number(x[1])));
  assert.deepEqual(tocTargets, ids, "TOC hrefs match section ids");
});

// ── branch markers (one per fixture-covered branch) ───────────────────────

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
  assert.ok(!/<article[\s\S]*?style="color: red"[\s\S]*?<\/article>/.test(html), "inline style stripped from body");
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
