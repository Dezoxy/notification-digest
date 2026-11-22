import test from "node:test";
import assert from "node:assert/strict";
import { loadWorker, fetchWithCtx, ORIGIN, FROZEN_NOW } from "./env.mjs";

// FROZEN_NOW = 2026-08-25T18:00:00Z, i.e. Budapest "today" is 2026-08-25.
assert.equal(FROZEN_NOW.slice(0, 10), "2026-08-25");

const MCP = "mcptesttoken";
const API = "apitesttoken";
const SITE = "goldentesttoken";

// Tiny D1 stand-in for the queries the tools issue (health, latest, by-date
// range, list with LIMIT); throws on anything else.
function makeDb(rows, { fail = false } = {}) {
  return {
    prepare(sql) {
      assert.match(sql, /kind = 'daily'/, `unexpected SQL: ${sql}`);
      let args = [];
      const run = () => {
        if (fail) throw new Error("D1 secret internals");
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

const mkRow = (id, created_at) => ({
  id,
  kind: "daily",
  created_at,
  tldr: `tldr ${id}`,
  item_count: 913,
  section_count: 8,
  body_md: `Body ${id} [¹](https://a.example/${id})`,
  source_counts: JSON.stringify({ telegram: 5, rss: 2 }),
  failed_sources: null,
  topics: JSON.stringify([{ slug: "s", label: "S", key: "k" }]),
  deltas: JSON.stringify([{ slug: "s", previously: "a", now: "b" }]),
});

const ROWS = [
  mkRow(12, "2026-08-24T22:30:00+00:00"), // Budapest 08-25 (today)
  mkRow(11, "2026-08-23T18:00:00+00:00"), // 08-23
  mkRow(10, "2026-08-22T18:00:00+00:00"),
  mkRow(9, "2026-08-21T18:00:00+00:00"),
  mkRow(8, "2026-08-20T18:00:00+00:00"),
  mkRow(7, "2026-08-19T18:00:00+00:00"),
  mkRow(6, "2026-08-18T18:00:00+00:00"),
  mkRow(5, "2026-08-17T18:00:00+00:00"),
];

const envWith = (rows = ROWS, extra = {}) => ({
  MCP_TOKEN: MCP,
  API_TOKEN: API,
  SITE_TOKEN: SITE,
  DB: makeDb(rows),
  ...extra,
});

async function send(path, { env = envWith(), method = "POST", body, headers } = {}) {
  const worker = await loadWorker();
  const init = { method, headers };
  if (body !== undefined) init.body = typeof body === "string" ? body : JSON.stringify(body);
  return fetchWithCtx(worker, new Request(`${ORIGIN}${path}`, init), env);
}

const mcp = (body, opts = {}) => send(`/mcp/${MCP}`, { body, ...opts });
const rpc = (method, params, id = 1) => ({ jsonrpc: "2.0", id, method, params });

const HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "private, no-store",
  "referrer-policy": "no-referrer",
  "x-robots-tag": "noindex, nofollow",
  "x-content-type-options": "nosniff",
};

function assertHeaders(res) {
  for (const [k, v] of Object.entries(HEADERS)) assert.equal(res.headers.get(k), v, k);
}

test("auth failures are the uniform 404, identical to an unknown path", async () => {
  const baseline = await send("/definitely/unknown", { method: "GET" });
  const baseBody = await baseline.text();
  assert.equal(baseBody, "Not Found");
  const cases = [
    [`/mcp/wrongtoken`, envWith()],
    [`/mcp/${MCP}`, { SITE_TOKEN: SITE, API_TOKEN: API, DB: makeDb(ROWS) }], // MCP_TOKEN unset
    [`/mcp/${MCP}`, envWith(ROWS, { MCP_TOKEN: "" })],
    [`/mcp/${API}`, envWith()], // API token as MCP token
    [`/mcp/${SITE}`, envWith()], // site token as MCP token
  ];
  for (const method of ["POST", "GET", "PUT", "DELETE"]) {
    for (const [path, env] of cases) {
      const res = await send(path, {
        env,
        method,
        body: method === "POST" ? rpc("ping") : undefined,
      });
      assert.equal(res.status, baseline.status, `${method} ${path}`);
      assert.equal(await res.text(), baseBody, `${method} ${path}`);
      assert.deepEqual([...res.headers], [...baseline.headers], `${method} ${path}`);
    }
  }
});

test("valid token, non-POST: 405 with allow: POST", async () => {
  for (const method of ["GET", "PUT", "DELETE"]) {
    const res = await mcp(undefined, { method });
    assert.equal(res.status, 405, method);
    assert.equal(res.headers.get("allow"), "POST");
    assert.equal(res.headers.get("content-type"), "text/plain; charset=utf-8");
    for (const [k, v] of Object.entries(HEADERS)) {
      if (k !== "content-type") assert.equal(res.headers.get(k), v, `${method} ${k}`);
    }
  }
});

test("initialize: echoes a supported protocol version, falls back otherwise", async () => {
  for (const v of ["2025-11-25", "2025-06-18", "2025-03-26"]) {
    const res = await mcp(rpc("initialize", { protocolVersion: v }, 7));
    assert.equal(res.status, 200);
    assertHeaders(res);
    const body = await res.json();
    assert.equal(body.jsonrpc, "2.0");
    assert.equal(body.id, 7);
    assert.equal(body.result.protocolVersion, v);
    assert.deepEqual(body.result.capabilities, { tools: {} });
    assert.deepEqual(body.result.serverInfo, { name: "news-digest", version: "0.2.0" });
    assert.equal(typeof body.result.instructions, "string");
  }
  for (const params of [{ protocolVersion: "1999-01-01" }, {}, undefined]) {
    const body = await (await mcp(rpc("initialize", params))).json();
    assert.equal(body.result.protocolVersion, "2025-06-18");
  }
});

test("notification (no id): 202 with an empty body", async () => {
  const res = await mcp({ jsonrpc: "2.0", method: "notifications/initialized" });
  assert.equal(res.status, 202);
  assert.equal(await res.text(), "");
});

test("ping returns an empty result", async () => {
  const body = await (await mcp(rpc("ping", undefined, "abc"))).json();
  assert.deepEqual(body, { jsonrpc: "2.0", id: "abc", result: {} });
});

test("tools/list: the four read-only tools", async () => {
  const res = await mcp(rpc("tools/list"));
  assert.equal(res.status, 200);
  assertHeaders(res);
  const { result } = await res.json();
  assert.deepEqual(
    result.tools.map((t) => t.name),
    ["health", "get_daily_latest", "get_daily_by_date", "list_daily_editions"],
  );
  for (const tool of result.tools) {
    assert.equal(typeof tool.title, "string");
    assert.equal(typeof tool.description, "string");
    assert.equal(tool.inputSchema.type, "object");
    assert.equal(tool.inputSchema.additionalProperties, false);
    assert.deepEqual(tool.annotations, { readOnlyHint: true, openWorldHint: false });
  }
  const [health, latest, byDate, list] = result.tools;
  assert.deepEqual(health.inputSchema.properties, {});
  assert.deepEqual(latest.inputSchema.properties, {});
  assert.match(latest.description, /previous day/);
  assert.deepEqual(byDate.inputSchema, {
    type: "object",
    properties: {
      date: {
        type: "string",
        pattern: "^\\d{4}-\\d{2}-\\d{2}$",
        description: "Europe/Budapest calendar date, YYYY-MM-DD",
      },
    },
    required: ["date"],
    additionalProperties: false,
  });
  assert.deepEqual(list.inputSchema, {
    type: "object",
    properties: {
      limit: {
        type: "integer",
        minimum: 1,
        maximum: 14,
        default: 7,
        description: "How many of the newest daily editions to list",
      },
    },
    additionalProperties: false,
  });
  assert.match(list.description, /without the article body/);
  assert.match(list.description, /get_daily_by_date/);
});

test("tools/call health: structuredContent equals the /api/v1 health JSON", async () => {
  for (const rows of [ROWS, []]) {
    const env = envWith(rows);
    const api = await (await send(`/api/v1/${API}/health`, { env, method: "GET" })).json();
    const res = await mcp(rpc("tools/call", { name: "health", arguments: {} }), { env });
    assert.equal(res.status, 200);
    assertHeaders(res);
    const { result } = await res.json();
    assert.equal(result.isError, false);
    assert.deepEqual(result.structuredContent, api);
    assert.equal(result.content.length, 1);
    assert.equal(result.content[0].type, "text");
    assert.deepEqual(JSON.parse(result.content[0].text), api);
  }
});

test("tools/call get_daily_latest: the same edition object as /api/v1", async () => {
  const api = await (await send(`/api/v1/${API}/digests/daily/latest`, { method: "GET" })).json();
  const res = await mcp(rpc("tools/call", { name: "get_daily_latest", arguments: {} }));
  assert.equal(res.status, 200);
  assertHeaders(res);
  const { result } = await res.json();
  assert.equal(result.isError, false);
  assert.equal(api.digest_id, 12);
  assert.deepEqual(result.structuredContent, api);
  assert.deepEqual(JSON.parse(result.content[0].text), api);
});

test("tools/call get_daily_latest: no edition is a tool error, not a JSON-RPC error", async () => {
  const res = await mcp(rpc("tools/call", { name: "get_daily_latest" }), { env: envWith([]) });
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.error, undefined);
  assert.deepEqual(body.result, {
    content: [{ type: "text", text: "No daily edition has been published yet." }],
    isError: true,
  });
});

const callTool = async (name, args, env) => {
  const res = await mcp(rpc("tools/call", { name, arguments: args }), env ? { env } : {});
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.error, undefined);
  return body.result;
};

test("tools/call get_daily_by_date: the same edition object as the /api/v1 date route", async () => {
  for (const date of ["2026-08-25", "2026-08-23"]) {
    const api = await (
      await send(`/api/v1/${API}/digests/daily/${date}`, { method: "GET" })
    ).json();
    const result = await callTool("get_daily_by_date", { date });
    assert.equal(result.isError, false);
    assert.equal(api.date, date);
    assert.deepEqual(result.structuredContent, api);
    assert.deepEqual(JSON.parse(result.content[0].text), api);
  }
});

test("get_daily_by_date: tool errors for bad, future and missing dates", async () => {
  const msg = {
    invalid_date: "Date must be a real calendar date, YYYY-MM-DD.",
    not_published_yet: (d) =>
      `No daily edition for ${d} yet; it is published around 20:30 Europe/Budapest. Check again later.`,
    not_found: (d) => `No daily edition exists for ${d}.`,
  };
  const check = (result, error, message) => {
    assert.equal(result.isError, true);
    assert.deepEqual(result.structuredContent, { error, message });
    assert.equal(result.content[0].text, message);
  };
  for (const args of [
    { date: "2026-8-25" },
    { date: "26-08-25" },
    { date: "2026-08-25x" },
    { date: "2026-02-30" },
    { date: "2026-13-01" },
    { date: 20260825 },
    { date: null },
    {},
    undefined,
  ]) {
    check(await callTool("get_daily_by_date", args), "invalid_date", msg.invalid_date);
  }
  // Today without a row, and a future date, are both "not yet".
  const empty = envWith([]);
  for (const date of ["2026-08-25", "2027-01-01"]) {
    check(
      await callTool("get_daily_by_date", { date }, empty),
      "not_published_yet",
      msg.not_published_yet(date),
    );
  }
  // A past date with no row is not_found.
  check(
    await callTool("get_daily_by_date", { date: "2026-08-01" }),
    "not_found",
    msg.not_found("2026-08-01"),
  );
});

test("get_daily_by_date: a past DST-transition day finds its edition like the API", async () => {
  // 2026-03-29 is the spring-forward day (the clock is frozen at 2026-08-25, so
  // the 10-25 fall-back day is in the future and cannot hold an edition here).
  const rows = [
    mkRow(1, "2026-03-28T23:30:00+00:00"), // Budapest 03-29 00:30 +01:00
    mkRow(2, "2026-03-29T21:30:00+00:00"), // 23:30 +02:00 after DST, still 03-29
  ];
  const env = envWith(rows);
  const api = await (
    await send(`/api/v1/${API}/digests/daily/2026-03-29`, { env, method: "GET" })
  ).json();
  assert.equal(api.digest_id, 2);
  const result = await callTool("get_daily_by_date", { date: "2026-03-29" }, env);
  assert.equal(result.isError, false);
  assert.deepEqual(result.structuredContent, api);
});

test("tools/call list_daily_editions: summaries equal the /api/v1 list minus content/sources", async () => {
  const apiList = async (q) =>
    await (await send(`/api/v1/${API}/digests/daily${q}`, { method: "GET" })).json();
  for (const [args, q, n] of [
    [undefined, "", 7],
    [{}, "", 7],
    [{ limit: 3 }, "?limit=3", 3],
    [{ limit: "3" }, "?limit=3", 3],
    [{ limit: 14, unknown: "ignored" }, "?limit=14", 8],
  ]) {
    const api = await apiList(q);
    const result = await callTool("list_daily_editions", args);
    assert.equal(result.isError, false, JSON.stringify(args));
    const out = result.structuredContent;
    assert.equal(out.count, n);
    assert.equal(out.editions.length, n);
    assert.equal(out.schema_version, api.schema_version);
    assert.equal(out.timezone, api.timezone);
    assert.deepEqual(JSON.parse(result.content[0].text), out);
    assert.deepEqual(
      out.editions.map((e) => e.date),
      api.editions.map((e) => e.date),
    ); // newest first
    api.editions.forEach((full, i) => {
      const summary = out.editions[i];
      assert.ok(!("content" in summary) && !("sources" in summary));
      const { content: _c, sources: _s, ...rest } = full;
      assert.deepEqual(summary, rest);
      assert.deepEqual(Object.keys(summary), Object.keys(rest)); // same key order
    });
  }
});

test("list_daily_editions: bad limit is an invalid_limit tool error", async () => {
  for (const limit of [0, 15, "abc", 2.5, "0", "15", "2.5", "", -1, null, true, [3]]) {
    const result = await callTool("list_daily_editions", { limit });
    assert.equal(result.isError, true, JSON.stringify(limit));
    assert.deepEqual(result.structuredContent, {
      error: "invalid_limit",
      message: "limit must be an integer from 1 to 14.",
    });
    assert.equal(result.content[0].text, "limit must be an integer from 1 to 14.");
  }
});

test("tools/call: unknown tool -32602, unknown method -32601", async () => {
  let body = await (await mcp(rpc("tools/call", { name: "nope" }, 3))).json();
  assert.deepEqual(body, {
    jsonrpc: "2.0",
    id: 3,
    error: { code: -32602, message: "Unknown tool" },
  });
  body = await (await mcp(rpc("tools/call", undefined, 4))).json();
  assert.equal(body.error.code, -32602);
  body = await (await mcp(rpc("resources/list", undefined, 5))).json();
  assert.deepEqual(body, {
    jsonrpc: "2.0",
    id: 5,
    error: { code: -32601, message: "Method not found" },
  });
});

test("D1 failure: -32603 over HTTP 200, no internals leaked", async () => {
  const errors = [];
  const origErr = console.error;
  console.error = (...a) => errors.push(a.join(" "));
  try {
    const env = { ...envWith(), DB: makeDb(ROWS, { fail: true }) };
    const calls = [
      { name: "health" },
      { name: "get_daily_latest" },
      { name: "get_daily_by_date", arguments: { date: "2026-08-20" } },
      { name: "list_daily_editions" },
    ];
    for (const params of calls) {
      const res = await mcp(rpc("tools/call", params), { env });
      assert.equal(res.status, 200);
      const text = await res.text();
      assert.ok(!text.includes("secret"));
      assert.equal(JSON.parse(text).error.code, -32603);
      assert.equal(JSON.parse(text).error.message, "Internal error");
    }
  } finally {
    console.error = origErr;
  }
  assert.equal(errors.length, 4);
  assert.ok(!errors[0].includes(MCP));
});

test("malformed input: parse error and invalid requests are HTTP 400", async () => {
  let res = await mcp("{not json");
  assert.equal(res.status, 400);
  assertHeaders(res);
  assert.deepEqual(await res.json(), {
    jsonrpc: "2.0",
    id: null,
    error: { code: -32700, message: "Parse error" },
  });
  const invalid = [
    [rpc("ping"), rpc("ping", undefined, 2)], // batch
    [],
    "42",
    "null",
    { id: 1, method: "ping" }, // no jsonrpc
    { jsonrpc: "1.0", id: 1, method: "ping" },
    { jsonrpc: "2.0", id: 1, method: 5 },
    { jsonrpc: "2.0", id: 1 },
  ];
  for (const body of invalid) {
    res = await mcp(body);
    assert.equal(res.status, 400, JSON.stringify(body));
    assert.deepEqual(await res.json(), {
      jsonrpc: "2.0",
      id: null,
      error: { code: -32600, message: "Invalid Request" },
    });
  }
});

test("logs: one structured line per request, never the token, path or body", async () => {
  const lines = [];
  const orig = console.log;
  console.log = (...a) => lines.push(a.join(" "));
  try {
    await mcp(rpc("initialize", { protocolVersion: "2025-06-18" }));
    await mcp({ jsonrpc: "2.0", method: "notifications/initialized" });
    await mcp(rpc("tools/call", { name: "health" }));
    await mcp(rpc("bogus/method"));
    await mcp("{not json");
  } finally {
    console.log = orig;
  }
  assert.equal(lines.length, 5);
  assert.deepEqual(
    lines.map((l) => JSON.parse(l)),
    [
      { event: "mcp", method: "initialize", status: 200 },
      { event: "mcp", method: "notification", status: 202 },
      { event: "mcp", method: "tools/call", status: 200 },
      { event: "mcp", method: "bogus/method", status: 200 },
      { event: "mcp", method: "invalid", status: 400 },
    ],
  );
  for (const l of lines) {
    assert.ok(!l.includes(MCP) && !l.includes("/mcp/") && !l.includes("http"), l);
  }
});

test("body size cap: oversize content-length or body is 413 -32600", async () => {
  const expected = {
    jsonrpc: "2.0",
    id: null,
    error: { code: -32600, message: "Request too large" },
  };
  const big = JSON.stringify({ ...rpc("ping"), pad: "x".repeat(65536) });
  let res = await mcp(big);
  assert.equal(res.status, 413);
  assertHeaders(res);
  assert.deepEqual(await res.json(), expected);
  // Declared length alone is enough, before the body is read.
  res = await mcp(JSON.stringify(rpc("ping")), {
    headers: { "content-length": "65537" },
  });
  assert.equal(res.status, 413);
  assert.deepEqual(await res.json(), expected);
  // Just under the cap still works.
  const ok = JSON.stringify({ ...rpc("ping"), pad: "x".repeat(60000) });
  assert.equal((await mcp(ok)).status, 200);
});

test("lenient on Accept, Origin and MCP-Protocol-Version; no OAuth discovery", async () => {
  const res = await mcp(rpc("initialize", { protocolVersion: "2025-06-18" }), {
    headers: {
      accept: "text/event-stream",
      origin: "https://evil.example",
      "mcp-protocol-version": "1999-01-01",
    },
  });
  assert.equal(res.status, 200);
  assert.equal((await res.json()).result.protocolVersion, "2025-06-18");
  for (const path of [
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
  ]) {
    const r = await send(path, { method: "GET" });
    assert.equal(r.status, 404, path);
    assert.equal(await r.text(), "Not Found");
    assert.equal(r.headers.get("www-authenticate"), null);
  }
});
