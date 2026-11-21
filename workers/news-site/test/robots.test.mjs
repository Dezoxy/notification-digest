import test from "node:test";
import assert from "node:assert/strict";
import { fetchPath } from "./env.mjs";

// env.mjs must load first (clock + crypto shims) before worker.js evaluates.
const { ROBOTS_TXT } = await import("../worker.js");

// Parse robots.txt into { agent: [directive lines] } groups.
function groups(text) {
  const out = {};
  for (const block of text.trim().split("\n\n")) {
    const [first, ...rest] = block.split("\n");
    out[first.replace("User-agent: ", "")] = rest;
  }
  return out;
}

test("GET /robots.txt serves ROBOTS_TXT as text/plain", async () => {
  const res = await fetchPath("/robots.txt");
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("content-type"), "text/plain; charset=utf-8");
  assert.equal(await res.text(), ROBOTS_TXT);
});

test("robots.txt groups: ChatGPT-User only /api/v1/, everyone else disallowed", () => {
  const g = groups(ROBOTS_TXT);
  assert.deepEqual(Object.keys(g).sort(), ["*", "ChatGPT-User", "OAI-SearchBot"]);
  assert.deepEqual(g["ChatGPT-User"], ["Allow: /api/v1/", "Disallow: /"]);
  assert.deepEqual(g["OAI-SearchBot"], ["Disallow: /"]);
  assert.deepEqual(g["*"], ["Disallow: /"]);
  assert.ok(!g["*"].some((l) => l.startsWith("Allow")));
});

test("non-GET /robots.txt falls through to the uniform 404", async () => {
  const res = await fetchPath("/robots.txt", { init: { method: "POST" } });
  assert.equal(res.status, 404);
  assert.equal(await res.text(), "Not Found");
});
