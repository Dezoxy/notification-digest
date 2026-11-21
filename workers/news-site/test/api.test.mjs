import test from "node:test";
import assert from "node:assert/strict";
import { loadWorker, fetchWithCtx, ORIGIN, FROZEN_NOW } from "./env.mjs";

// FROZEN_NOW = 2026-08-25T18:00:00Z, i.e. Budapest "today" is 2026-08-25.
assert.equal(FROZEN_NOW.slice(0, 10), "2026-08-25");

const API = "apitesttoken";
const SITE = "goldentesttoken";

const row = (id, created_at, extra = {}) => ({
  id,
  kind: "daily",
  created_at,
  tldr: `tldr ${id}`,
  item_count: 913,
  section_count: 8,
  body_md: `Body ${id} [¹](https://a.example/${id}) [²](https://b.example/x) [again](https://a.example/${id})`,
  source_counts: JSON.stringify({ telegram: 5, rss: 2 }),
  failed_sources: null,
  topics: JSON.stringify([{ slug: "s", label: "S", key: "k", extra: "no" }]),
  deltas: JSON.stringify([{ slug: "s", previously: "a", now: "b", extra: "no" }]),
  provenance: '{"secret":"PROVENANCE"}',
  body_html: "<p>HTML</p>",
  ...extra,
});

const ROWS = [
  row(1, "2026-03-27T19:30:00.123456+00:00"), // winter, +01:00
  row(2, "2026-03-28T22:30:00+00:00"), // 23:30 +01:00, date 03-28
  row(4, "2026-03-28T23:30:00+00:00"), // UTC 03-28, Budapest 03-29 00:30 +01:00
  row(3, "2026-03-29T21:30:00+00:00"), // 23:30 +02:00 (after DST), date 03-29 too
  row(10, "2026-08-20T18:00:00+00:00"), // same created_at as 11: id breaks the tie
  row(11, "2026-08-20T18:00:00+00:00"),
  row(12, "2026-08-24T22:30:00+00:00"), // UTC 08-24, Budapest 08-25 00:30 +02:00
  row(20, "2026-10-24T22:30:00+00:00"), // 00:30 +02:00 on 10-25
  row(21, "2026-10-25T22:30:00+00:00"), // 23:30 +01:00 on 10-25
  row(90, "2026-08-25T10:00:00+00:00", { kind: "window" }),
  row(91, "2026-08-25T11:00:00+00:00", { kind: "weekly" }),
];

// Tiny D1 stand-in for exactly the API's queries; throws on anything else.
function makeDb(rows = ROWS, { fail = false } = {}) {
  return {
    prepare(sql) {
      let args = [];
      const run = () => {
        if (fail) throw new Error("D1 secret internals");
        assert.match(sql, /kind = 'daily'/, `unexpected SQL: ${sql}`);
        assert.doesNotMatch(sql, /provenance|body_html|_hu/);
        let r = rows.filter((x) => x.kind === "daily");
        if (/created_at >= \?/.test(sql)) {
          r = r.filter((x) => x.created_at >= args[0] && x.created_at < args[1]);
        }
        r = [...r].sort((a, b) =>
          a.created_at === b.created_at ? b.id - a.id : a.created_at < b.created_at ? 1 : -1,
        );
        if (/LIMIT \?/.test(sql)) return r.slice(0, args[args.length - 1]);
        assert.match(sql, /LIMIT 1/);
        return r.slice(0, 1);
      };
      const stmt = {
        bind(...a) {
          args = a;
          return stmt;
        },
        async first() {
          return run()[0] ?? null;
        },
        async all() {
          return { results: run() };
        },
      };
      return stmt;
    },
  };
}

const envWith = (rows, extra = {}) => ({
  API_TOKEN: API,
  SITE_TOKEN: SITE,
  DB: makeDb(rows),
  ...extra,
});

async function get(path, { env = envWith(ROWS), method = "GET" } = {}) {
  const worker = await loadWorker();
  return fetchWithCtx(worker, new Request(`${ORIGIN}${path}`, { method }), env);
}

const api = (rest) => `/api/v1/${API}/${rest}`;
const daily = (rest = "") => api(`digests/daily${rest}`);

const HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "private, no-store",
  "referrer-policy": "no-referrer",
  "x-robots-tag": "noindex, nofollow",
  "x-content-type-options": "nosniff",
};

function assertApiHeaders(res) {
  for (const [k, v] of Object.entries(HEADERS)) assert.equal(res.headers.get(k), v, k);
}

async function sha256(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

test("latest: exact shape, key order, winter offset, content hash, sources", async () => {
  const res = await get(daily("/latest"), { env: envWith(ROWS.filter((r) => r.id <= 2)) });
  assert.equal(res.status, 200);
  assertApiHeaders(res);
  const text = await res.text();
  assert.ok(!text.includes("\n"), "compact JSON");
  const e = JSON.parse(text);
  assert.deepEqual(Object.keys(e), [
    "schema_version",
    "edition_id",
    "digest_id",
    "edition_type",
    "status",
    "date",
    "timezone",
    "published_at",
    "published_at_utc",
    "title",
    "tldr",
    "content",
    "sources",
    "topics",
    "deltas",
    "stats",
    "failed_sources",
    "content_hash",
  ]);
  assert.equal(e.schema_version, "1.0");
  assert.equal(e.edition_id, "daily-2026-03-28");
  assert.equal(e.digest_id, 2);
  assert.equal(e.edition_type, "daily");
  assert.equal(e.status, "complete");
  assert.equal(e.date, "2026-03-28");
  assert.equal(e.timezone, "Europe/Budapest");
  assert.equal(e.published_at, "2026-03-28T23:30:00+01:00");
  assert.equal(e.published_at_utc, "2026-03-28T22:30:00Z");
  assert.equal(e.title, "Daily brief — 2026-03-28");
  assert.equal(e.tldr, "tldr 2");
  assert.deepEqual(e.content, { format: "markdown", body: ROWS[1].body_md });
  assert.deepEqual(e.sources, [
    { label: "¹", url: "https://a.example/2" },
    { label: "²", url: "https://b.example/x" },
  ]);
  assert.deepEqual(e.topics, [{ slug: "s", label: "S", key: "k" }]);
  assert.deepEqual(e.deltas, [{ slug: "s", previously: "a", now: "b" }]);
  assert.deepEqual(e.stats, {
    item_count: 913,
    section_count: 8,
    source_counts: { telegram: 5, rss: 2 },
  });
  assert.deepEqual(e.failed_sources, []);
  assert.equal(e.content_hash, `sha256:${await sha256(ROWS[1].body_md)}`);
  for (const bad of ["provenance", "PROVENANCE", "body_html", "HTML", "canonical", API]) {
    assert.ok(!text.includes(bad), `leaks ${bad}`);
  }
});

test("latest picks the newest daily (summer offset +02:00); window/weekly never returned", async () => {
  const res = await get(daily("/latest"), {
    env: envWith(ROWS.filter((r) => r.id < 20)),
  });
  const e = await res.json();
  assert.equal(e.digest_id, 12);
  assert.equal(e.date, "2026-08-25");
  assert.equal(e.published_at, "2026-08-25T00:30:00+02:00");
});

test("NULL / unparsable JSON columns degrade to empty values", async () => {
  const rows = [
    row(1, "2026-08-24T10:00:00+00:00", {
      source_counts: null,
      failed_sources: '["x"]',
      topics: "not json",
      deltas: null,
    }),
  ];
  const e = await (await get(daily("/latest"), { env: envWith(rows) })).json();
  assert.deepEqual(e.topics, []);
  assert.deepEqual(e.deltas, []);
  assert.equal(e.stats.source_counts, null);
  assert.deepEqual(e.failed_sources, ["x"]);
});

test("date lookup uses the Budapest calendar date, across DST boundaries", async () => {
  // UTC date 08-24 but Budapest 08-25.
  let e = await (await get(daily("/2026-08-25"))).json();
  assert.equal(e.digest_id, 12);
  assert.equal(e.edition_id, "daily-2026-08-25");
  // Spring forward: 03-28 holds only id 2; id 4 (UTC 03-28 23:30) is 03-29 local.
  // Local midnight of 03-29 is 23:00Z (offset still +01:00), not noon's +02:00.
  e = await (await get(daily("/2026-03-28"))).json();
  assert.equal(e.digest_id, 2);
  // Two rows on 03-29 across the transition: newest created_at wins.
  e = await (await get(daily("/2026-03-29"))).json();
  assert.equal(e.digest_id, 3);
  assert.equal(e.published_at, "2026-03-29T23:30:00+02:00");
});

test("equal created_at: highest id wins; window/weekly rows are excluded", async () => {
  const e = await (await get(daily("/2026-08-20"))).json();
  assert.equal(e.digest_id, 11);
  // 2026-08-25 has window (90) and weekly (91) rows too, but only daily 12 counts.
  const list = await (await get(daily("?limit=14"))).json();
  assert.ok(list.editions.every((x) => ![90, 91].includes(x.digest_id)));
});

test("list: default 7, limit=2, newest first, empty is 200", async () => {
  let body = await (await get(daily())).json();
  assert.deepEqual(Object.keys(body), ["schema_version", "timezone", "count", "editions"]);
  assert.equal(body.count, 7);
  assert.equal(body.editions.length, 7);
  assert.deepEqual(
    body.editions.map((x) => x.digest_id),
    [21, 20, 12, 11, 10, 3, 4],
  );
  body = await (await get(daily("?limit=2"))).json();
  assert.deepEqual(
    body.editions.map((x) => x.digest_id),
    [21, 20],
  );
  const res = await get(daily(), { env: envWith([]) });
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), {
    schema_version: "1.0",
    timezone: "Europe/Budapest",
    count: 0,
    editions: [],
  });
});

test("list: 14 is allowed, 0/15/abc/1.5/-1/empty are invalid_limit", async () => {
  assert.equal((await get(daily("?limit=14"))).status, 200);
  for (const bad of ["0", "15", "abc", "1.5", "-1", ""]) {
    const res = await get(daily(`?limit=${bad}`));
    assert.equal(res.status, 400, bad);
    assertApiHeaders(res);
    const body = await res.json();
    assert.equal(body.error.code, "invalid_limit");
    assert.equal(body.schema_version, "1.0");
  }
});

test("invalid calendar dates are 400 invalid_date", async () => {
  for (const bad of ["2026-02-30", "2026-13-01", "2026-00-10", "2026-04-31"]) {
    const res = await get(daily(`/${bad}`));
    assert.equal(res.status, 400, bad);
    assert.equal((await res.json()).error.code, "invalid_date");
  }
  assert.equal((await get(daily("/2028-02-29"))).status, 404); // real leap day
});

test("today with no row and future dates: 404 not_published_yet + retry-after", async () => {
  const noToday = envWith(ROWS.filter((r) => r.id !== 12 && r.id < 20));
  for (const [path, env] of [
    ["/2026-08-25", noToday],
    ["/2026-08-26", envWith(ROWS)],
    ["/2027-01-01", envWith(ROWS)],
  ]) {
    const res = await get(daily(path), { env });
    assert.equal(res.status, 404, path);
    assert.equal(res.headers.get("retry-after"), "3600");
    assertApiHeaders(res);
    const body = await res.json();
    assert.equal(body.error.code, "not_published_yet");
  }
});

test("past date or empty latest: 404 not_found, no retry-after", async () => {
  let res = await get(daily("/2026-08-21"));
  assert.equal(res.status, 404);
  assert.equal(res.headers.get("retry-after"), null);
  assertApiHeaders(res);
  assert.equal((await res.json()).error.code, "not_found");
  res = await get(daily("/latest"), { env: envWith([]) });
  assert.equal(res.status, 404);
  assert.equal((await res.json()).error.code, "not_found");
});

test("health: with and without a daily row", async () => {
  let res = await get(api("health"));
  assert.equal(res.status, 200);
  assertApiHeaders(res);
  let body = await res.json();
  assert.deepEqual(body, {
    schema_version: "1.0",
    status: "ok",
    timezone: "Europe/Budapest",
    today: "2026-08-25",
    today_published: false, // newest daily is the (future-dated) October row
    latest_daily: {
      edition_id: "daily-2026-10-25",
      digest_id: 21,
      date: "2026-10-25",
      published_at: "2026-10-25T23:30:00+01:00",
    },
  });
  body = await (await get(api("health"), { env: envWith(ROWS.filter((r) => r.id === 12)) })).json();
  assert.equal(body.today_published, true);
  assert.equal(body.latest_daily.published_at, "2026-08-25T00:30:00+02:00");
  body = await (await get(api("health"), { env: envWith([]) })).json();
  assert.equal(body.latest_daily, null);
  assert.equal(body.today_published, false);
});

test("D1 failure: 500 internal_error with no error text", async () => {
  const errors = [];
  const origErr = console.error;
  console.error = (...a) => errors.push(a.join(" "));
  try {
    const env = { API_TOKEN: API, SITE_TOKEN: SITE, DB: makeDb(ROWS, { fail: true }) };
    for (const path of [daily("/latest"), daily(), daily("/2026-08-21"), api("health")]) {
      const res = await get(path, { env });
      assert.equal(res.status, 500, path);
      assertApiHeaders(res);
      const text = await res.text();
      assert.ok(!text.includes("secret"));
      assert.equal(JSON.parse(text).error.code, "internal_error");
    }
  } finally {
    console.error = origErr;
  }
  assert.ok(errors.every((l) => !l.includes(API)));
});

test("auth failures are the uniform 404, identical to an unknown path", async () => {
  const baseline = await get("/definitely/unknown");
  const baseBody = await baseline.text();
  assert.equal(baseBody, "Not Found");
  assert.equal(baseline.headers.get("content-type"), "text/plain; charset=utf-8");
  const cases = [
    [`/api/v1/wrongtoken/digests/daily/latest`, envWith(ROWS)],
    [`/api/v1/wrongtoken/health`, envWith(ROWS)],
    [daily("/latest"), { SITE_TOKEN: SITE, DB: makeDb() }], // API_TOKEN unset
    [daily("/latest"), envWith(ROWS, { API_TOKEN: "" })],
    [`/api/v1/${SITE}/digests/daily/latest`, envWith(ROWS)], // site token as API token
    [`/api/v1/${SITE}/health`, envWith(ROWS)],
  ];
  for (const [path, env] of cases) {
    const res = await get(path, { env });
    assert.equal(res.status, baseline.status, path);
    assert.equal(await res.text(), baseBody, path);
    assert.deepEqual([...res.headers], [...baseline.headers], path);
  }
  // API token is not a reader token either.
  const site = await get(`/t/${API}/`);
  assert.equal(site.status, 404);
});

test("non-GET methods are the uniform 404", async () => {
  const baseline = await get("/definitely/unknown");
  for (const method of ["POST", "PUT", "DELETE"]) {
    for (const path of [daily("/latest"), daily(), api("health")]) {
      const res = await get(path, { method });
      assert.equal(res.status, 404, `${method} ${path}`);
      assert.equal(await res.text(), "Not Found");
      assert.deepEqual([...res.headers], [...baseline.headers]);
    }
  }
});

test("logs: one structured line per request, never the token or URL", async () => {
  const lines = [];
  const orig = console.log;
  console.log = (...a) => lines.push(a.join(" "));
  try {
    await get(daily("/latest"));
    await get(daily("/2026-08-25"));
    await get(daily("?limit=3"));
    await get(api("health"));
    await get(daily("/2026-08-21"));
    await get(daily("?limit=0"));
  } finally {
    console.log = orig;
  }
  assert.equal(lines.length, 6);
  const parsed = lines.map((l) => JSON.parse(l));
  assert.deepEqual(
    parsed.map((p) => [p.route, p.status]),
    [
      ["daily_latest", 200],
      ["daily_date", 200],
      ["daily_list", 200],
      ["health", 200],
      ["daily_date", 404],
      ["daily_list", 400],
    ],
  );
  assert.equal(parsed[0].digest_id, 21);
  assert.equal(parsed[1].date, "2026-08-25");
  assert.equal(parsed[2].count, 3);
  for (const l of lines) {
    assert.ok(!l.includes(API) && !l.includes("/api/") && !l.includes("http"), l);
  }
});
