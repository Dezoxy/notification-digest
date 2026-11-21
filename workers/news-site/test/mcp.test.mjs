import test from "node:test";
import assert from "node:assert/strict";
import { loadWorker, fetchWithCtx, ORIGIN, FROZEN_NOW } from "./env.mjs";

// FROZEN_NOW = 2026-08-25T18:00:00Z, i.e. Budapest "today" is 2026-08-25.
assert.equal(FROZEN_NOW.slice(0, 10), "2026-08-25");

const MCP = "mcptesttoken";
const API = "apitesttoken";
const SITE = "goldentesttoken";

// Tiny D1 stand-in for the two queries the tools issue (health and latest
// edition); throws on anything else.
function makeDb(rows, { fail = false } = {}) {
  return {
    prepare(sql) {
      assert.match(sql, /kind = 'daily'/, `unexpected SQL: ${sql}`);
      return {
        async first() {
          if (fail) throw new Error("D1 secret internals");
          return rows[0] ?? null;
        },
      };
    },
  };
}

const ROWS = [
  {
    id: 12,
    kind: "daily",
    created_at: "2026-08-24T22:30:00+00:00", // Budapest 08-25
    tldr: "tldr 12",
    item_count: 913,
    section_count: 8,
    body_md: "Body 12 [¹](https://a.example/12)",
    source_counts: JSON.stringify({ telegram: 5, rss: 2 }),
    failed_sources: null,
    topics: JSON.stringify([{ slug: "s", label: "S", key: "k" }]),
    deltas: JSON.stringify([{ slug: "s", previously: "a", now: "b" }]),
  },
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
    assert.deepEqual(body.result.serverInfo, { name: "news-digest", version: "0.1.0" });
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

test("tools/list: the two read-only tools", async () => {
  const res = await mcp(rpc("tools/list"));
  assert.equal(res.status, 200);
  assertHeaders(res);
  const { result } = await res.json();
  assert.deepEqual(
    result.tools.map((t) => t.name),
    ["health", "get_daily_latest"],
  );
  for (const tool of result.tools) {
    assert.equal(typeof tool.title, "string");
    assert.equal(typeof tool.description, "string");
    assert.deepEqual(tool.inputSchema, {
      type: "object",
      properties: {},
      additionalProperties: false,
    });
    assert.deepEqual(tool.annotations, { readOnlyHint: true, openWorldHint: false });
  }
  assert.match(result.tools[1].description, /previous day/);
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
    for (const name of ["health", "get_daily_latest"]) {
      const res = await mcp(rpc("tools/call", { name }), { env });
      assert.equal(res.status, 200);
      const text = await res.text();
      assert.ok(!text.includes("secret"));
      assert.equal(JSON.parse(text).error.code, -32603);
      assert.equal(JSON.parse(text).error.message, "Internal error");
    }
  } finally {
    console.error = origErr;
  }
  assert.equal(errors.length, 2);
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
