// Golden-page renderer/checker — the byte-level half of the test suite
// (render.test.mjs owns the semantic half).
//
//   node test/golden.mjs --update            render + write test/golden/*.html
//   node test/golden.mjs --check             render + byte-compare (CI-of-one)
//   node test/golden.mjs --check --bundle dist/worker.js
//                                            same, but against a bundled
//                                            artifact (wrangler deploy
//                                            --dry-run --outdir dist) instead
//                                            of the source module graph
//
// The refactor contract: a PR that claims to be behavior-preserving must pass
// --check with ZERO diff; a PR that legitimately changes output regenerates
// with --update and the golden diff IS the review artifact.

import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { fetchPath, GOLDEN_PAGES } from "./env.mjs";

const args = process.argv.slice(2);
const update = args.includes("--update");
const check = args.includes("--check");
const bundleIx = args.indexOf("--bundle");
const entry = bundleIx >= 0 ? new URL(args[bundleIx + 1], `file://${process.cwd()}/`).href : undefined;

if (update === check) {
  console.error("usage: node test/golden.mjs --update | --check [--bundle dist/worker.js]");
  process.exit(2);
}

const goldenDir = new URL("./golden/", import.meta.url);
mkdirSync(goldenDir, { recursive: true });

function firstDiff(a, b) {
  const la = a.split("\n");
  const lb = b.split("\n");
  for (let i = 0; i < Math.max(la.length, lb.length); i++) {
    if (la[i] !== lb[i]) {
      return [
        `  line ${i + 1}:`,
        `  - ${la[i] ?? "<missing>"}`,
        `  + ${lb[i] ?? "<missing>"}`,
      ].join("\n");
    }
  }
  return "  (same lines, byte difference — check line endings)";
}

let failed = 0;
for (const { name, path } of GOLDEN_PAGES) {
  const res = await fetchPath(path, { entry });
  if (res.status !== 200) {
    console.error(`FAIL ${name}: status ${res.status}`);
    failed++;
    continue;
  }
  const html = await res.text();
  const file = new URL(`./${name}.html`, goldenDir);
  if (update) {
    writeFileSync(file, html);
    console.log(`wrote ${name}.html (${html.length} bytes)`);
  } else {
    if (!existsSync(file)) {
      console.error(`FAIL ${name}: golden missing — run --update once`);
      failed++;
      continue;
    }
    const want = readFileSync(file, "utf8");
    if (want === html) {
      console.log(`ok   ${name} (${html.length} bytes)`);
    } else {
      console.error(`FAIL ${name}: output differs from golden\n${firstDiff(want, html)}`);
      failed++;
    }
  }
}

if (failed > 0) {
  console.error(`\n${failed} golden page(s) failed`);
  process.exit(1);
}
