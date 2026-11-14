// Self-hosted fonts (public/fonts): the committed binaries must be real
// woff2 files whose sha256 matches SOURCES.md, and wrangler.jsonc must
// expose ./public as the Workers Static Assets directory.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";

const root = new URL("../", import.meta.url);
const FILES = [
  "inter-latin-wght.v1.woff2",
  "inter-latin-ext-wght.v1.woff2",
  "sora-latin-wght.v1.woff2",
  "sora-latin-ext-wght.v1.woff2",
];

const sources = readFileSync(new URL("public/fonts/SOURCES.md", root), "utf8");

function recordedSha(name) {
  const section = sources.split(/^## /m).find((s) => s.startsWith(name));
  assert.ok(section, `SOURCES.md has a section for ${name}`);
  const m = section.match(/^- sha256: ([0-9a-f]{64})$/m);
  assert.ok(m, `SOURCES.md records a sha256 for ${name}`);
  return m[1];
}

for (const name of FILES) {
  test(`font ${name} is woff2 and matches SOURCES.md`, () => {
    const buf = readFileSync(new URL(`public/fonts/${name}`, root));
    assert.equal(buf.subarray(0, 4).toString("latin1"), "wOF2");
    const sha = createHash("sha256").update(buf).digest("hex");
    assert.equal(sha, recordedSha(name));
  });
}

test("wrangler.jsonc serves ./public as static assets", () => {
  const raw = readFileSync(new URL("wrangler.jsonc", root), "utf8");
  const stripped = raw
    .split("\n")
    .filter((l) => !l.trimStart().startsWith("//"))
    .join("\n");
  const cfg = JSON.parse(stripped);
  assert.equal(cfg.assets.directory, "./public");
});
