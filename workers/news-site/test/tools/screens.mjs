// Screenshot harness for visual review — NOT part of `npm test`.
//
//   node test/tools/screens.mjs [label]        # label defaults to "run"
//
// Boots the real Worker router over the fixture DB in-process (frozen clock,
// like README "Driving the real router in a browser"), drives the
// pre-installed Playwright Chromium through a page × viewport × theme × language
// matrix plus the JS-created UI states the goldens never show, and writes PNGs
// (and one PDF) to test/tools/screens-out/<label>/. Compare two labels
// (`before`, `after`) by eye. Exits non-zero on page errors, console errors or
// failed requests. The state selectors below (summary.gear, details.settings,
// summary.searchtoggle, .backfab, .catchup, .cmdpalette-*, a.entry, …) are tied
// to the CURRENT markup and must be updated by the redesign packages. No dependencies are added: Playwright is resolved from the
// global install, so run this from a machine that has it.

import { createServer } from "node:http";
import { existsSync, mkdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

import { loadWorker, makeEnv, fetchWithCtx, ORIGIN, SITE_TOKEN, FROZEN_NOW } from "../env.mjs";

const label = process.argv[2] || "run";
// The label becomes a directory that is wiped below, so it must not be able
// to point anywhere else.
if (!/^[A-Za-z0-9_-]{1,40}$/.test(label)) {
  console.error(
    "usage: node test/tools/screens.mjs [label]\n  label: 1-40 chars of A-Z a-z 0-9 _ -",
  );
  process.exit(2);
}
const ROOT = fileURLToPath(new URL("../../", import.meta.url));
const PUBLIC_DIR = join(ROOT, "public");
const OUT = join(ROOT, "test/tools/screens-out", label);
rmSync(OUT, { recursive: true, force: true });
mkdirSync(OUT, { recursive: true });

// createRequire, not import(): ESM ignores NODE_PATH, CJS require honors it,
// and the global Playwright install is found that way.
const { chromium, devices } = createRequire(import.meta.url)("playwright");

// ---- server ---------------------------------------------------------------

const MIME = {
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".ttf": "font/ttf",
  ".otf": "font/otf",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
  ".html": "text/html; charset=utf-8",
};

// Mimics Workers Static Assets: a file under public/ wins over the worker.
function serveStatic(urlPath, res) {
  if (!existsSync(PUBLIC_DIR)) return false;
  let rel;
  try {
    rel = normalize(decodeURIComponent(urlPath.split("?")[0])).replace(/^[/\\]+/, "");
  } catch {
    return false;
  }
  const file = join(PUBLIC_DIR, rel);
  if (!file.startsWith(PUBLIC_DIR + sep)) return false;
  if (!existsSync(file) || !statSync(file).isFile()) return false;
  res.writeHead(200, { "content-type": MIME[extname(file)] ?? "application/octet-stream" });
  res.end(readFileSync(file));
  return true;
}

const worker = await loadWorker();
const server = createServer(async (req, res) => {
  try {
    if ((req.method === "GET" || req.method === "HEAD") && serveStatic(req.url, res)) return;
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = chunks.length ? Buffer.concat(chunks) : undefined;
    const headers = { ...req.headers };
    delete headers.host;
    const hasBody = body && req.method !== "GET" && req.method !== "HEAD";
    const r = await fetchWithCtx(
      worker,
      new Request(ORIGIN + req.url, {
        method: req.method,
        headers,
        body: hasBody ? body : undefined,
      }),
      makeEnv(),
    );
    const out = {};
    r.headers.forEach((v, k) => (out[k] = v));
    delete out["content-length"];
    delete out["content-encoding"];
    res.writeHead(r.status, out);
    res.end(Buffer.from(await r.arrayBuffer()));
  } catch (err) {
    res.writeHead(500, { "content-type": "text/plain" });
    res.end(String(err?.stack ?? err));
  }
});
await new Promise((ok) => server.listen(0, "127.0.0.1", ok));
const BASE = `http://127.0.0.1:${server.address().port}`;
const T = `${BASE}/t/${SITE_TOKEN}/`;

// ---- bookkeeping ----------------------------------------------------------

const written = [];
const notes = []; // states we could not reach
const problems = [];

function attach(page, where) {
  page.on("pageerror", (e) => problems.push(`[pageerror] ${where}: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error")
      problems.push(`[console.error] ${where}: ${m.text()} (${m.location().url})`);
  });
  page.on("response", (r) => {
    if (r.status() >= 400) problems.push(`[http ${r.status()}] ${where}: ${r.url()}`);
  });
}

async function shot(page, name, opts = {}) {
  const file = join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage: opts.fullPage ?? true });
  written.push(file);
}

async function settle(page) {
  await page.waitForLoadState("load");
  await page.evaluate(() => document.fonts?.ready);
  await page.waitForTimeout(250); // let the client script wire and animations settle
}

const PHONE = { name: "phone", ...devices["iPhone 13"] };
const DESKTOP = { name: "desktop", viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 };
const browser = await chromium.launch();

async function newContext(vp, { theme, reducedMotion, storage = {} } = {}) {
  const { name: _n, ...opts } = vp;
  const ctx = await browser.newContext({
    ...opts,
    colorScheme: theme === "dark" ? "dark" : "light",
    ...(reducedMotion ? { reducedMotion: "reduce" } : {}),
  });
  // Same key chrome.js's head script reads; set before any page script runs.
  await ctx.addInitScript(
    ({ theme, storage }) => {
      try {
        if (theme) localStorage.setItem("theme", theme);
        for (const [k, v] of Object.entries(storage)) {
          if (!localStorage.getItem(k)) localStorage.setItem(k, v);
        }
      } catch {}
    },
    { theme, storage },
  );
  // Freeze the browser clock at the fixture's "now" so relative labels match.
  try {
    await ctx.clock.install({ time: new Date(FROZEN_NOW) });
    await ctx.clock.resume?.();
  } catch (e) {
    notes.push(`browser clock not frozen: ${e.message}`);
  }
  return ctx;
}

// ---- 1. page matrix -------------------------------------------------------

const PAGES = [
  ["index", ""],
  ["daily", "daily/"],
  ["weekly", "weekly/"],
  ["digest-235", "d/235"],
  ["arc", "a/markets-slide"],
  ["search", "search?q=markets"],
  ["about", "about"],
];
const LANGS = [
  ["en", ""],
  ["hu", "hu/"],
];

for (const vp of [PHONE, DESKTOP]) {
  for (const theme of ["light", "dark"]) {
    const ctx = await newContext(vp, { theme });
    const page = await ctx.newPage();
    for (const [lang, prefix] of LANGS) {
      for (const [pname, path] of PAGES) {
        const tag = `${pname}-${lang}-${vp.name}-${theme}`;
        attach(page, tag);
        await page.goto(T + prefix + path);
        await settle(page);
        await shot(page, tag);
        page.removeAllListeners("pageerror");
        page.removeAllListeners("console");
        page.removeAllListeners("response");
      }
    }
    await ctx.close();
  }
}

// ---- 2. JS-created / interactive states ------------------------------------

async function state(vp, theme, name, fn, ctxOpts = {}) {
  const ctx = await newContext(vp, { theme, ...ctxOpts });
  const page = await ctx.newPage();
  attach(page, name);
  try {
    await fn(page);
  } catch (e) {
    notes.push(`${name}: could not capture (${e.message.split("\n")[0]})`);
  }
  await ctx.close();
}

const SEEN = { lastVisit: "2026-08-25T10:00:00Z" }; // between 234 (08:00) and 235 (14:00)

for (const vp of [PHONE, DESKTOP]) {
  for (const theme of ["light", "dark"]) {
    const sfx = `${vp.name}-${theme}`;

    await state(vp, theme, `settings-open-${sfx}`, async (page) => {
      await page.goto(T);
      await settle(page);
      await page.click("summary.gear");
      await page.waitForSelector("details.settings[open] .settingspanel", { state: "visible" });
      await page.waitForTimeout(300);
      await shot(page, `state-settings-open-${sfx}`, { fullPage: false });
    });

    await state(vp, theme, `search-open-${sfx}`, async (page) => {
      await page.goto(T);
      await settle(page);
      await page.click("summary.searchtoggle");
      await page.waitForSelector("details.searchpop[open] .searchpanel", { state: "visible" });
      await page.keyboard.type("markets");
      await page.waitForTimeout(300);
      await shot(page, `state-search-open-${sfx}`, { fullPage: false });
    });

    await state(vp, theme, `palette-open-${sfx}`, async (page) => {
      await page.goto(T);
      await settle(page);
      await page.keyboard.press("Control+k");
      await page.waitForSelector(".cmdpalette-backdrop:not([hidden]) .cmdpalette-item");
      await page.waitForTimeout(300);
      await shot(page, `state-palette-open-${sfx}`, { fullPage: false });
    });

    await state(
      vp,
      theme,
      `compact-${sfx}`,
      async (page) => {
        await page.goto(T);
        await settle(page);
        const d = await page.evaluate(() => document.documentElement.dataset.density);
        if (d !== "compact") throw new Error(`density is "${d}", not compact`);
        await shot(page, `state-compact-${sfx}`);
      },
      { storage: { density: "compact" } },
    );

    await state(
      vp,
      theme,
      `catchup-fence-${sfx}`,
      async (page) => {
        await page.goto(T);
        await settle(page);
        await page.waitForSelector(".catchup:not([hidden])", { timeout: 3000 }).catch(() => {
          throw new Error("catch-up banner did not appear");
        });
        const fence = await page.$(".unreadfence");
        if (!fence) notes.push(`unread fence not present at ${sfx}`);
        await shot(page, `state-catchup-fence-${sfx}`);
      },
      { storage: SEEN },
    );

    // Digest mid-article: sticky headings / floating back-to-top button.
    await state(vp, theme, `digest-scrolled-${sfx}`, async (page) => {
      await page.goto(T + "d/235");
      await settle(page);
      const target = await page.evaluate(() => {
        const h = document.querySelector("article.digest h2#s2");
        return h ? h.getBoundingClientRect().top + window.scrollY - 40 : 600;
      });
      await page.evaluate((y) => window.scrollTo(0, y), target);
      await page.waitForTimeout(500);
      const fab = await page.$(".backfab.show");
      await shot(page, `state-digest-scrolled-${sfx}`, { fullPage: false });
      if (!fab) notes.push(`floating back button (.backfab.show) not shown at ${sfx}`);
    });
  }
}

// Resume chip: only shows while the unread fence is BELOW the viewport, so use
// a short window on the seeded index and scroll to the top.
await state(
  { name: "phone-short", ...devices["iPhone 13"], viewport: { width: 390, height: 360 } },
  "light",
  "resume-chip",
  async (page) => {
    await page.goto(T);
    await settle(page);
    await page.waitForTimeout(300);
    const visible = await page.evaluate(() => {
      const c = document.querySelector(".resumechip");
      return !!c && !c.hidden;
    });
    if (!visible) throw new Error("resume chip not visible (fence in view on a short viewport?)");
    await shot(page, "state-resume-chip-phone-light", { fullPage: false });
  },
  { storage: SEEN },
);

// ---- 3. reduced motion + print -------------------------------------------

{
  const ctx = await newContext(DESKTOP, { theme: "dark", reducedMotion: true });
  const page = await ctx.newPage();
  for (const [name, path] of [
    ["index", ""],
    ["digest-235", "d/235"],
  ]) {
    attach(page, `reduced-${name}`);
    await page.goto(T + path);
    await settle(page);
    await shot(page, `reduced-motion-${name}-desktop-dark`);
    page.removeAllListeners("pageerror");
    page.removeAllListeners("console");
    page.removeAllListeners("response");
  }
  await ctx.close();
}

{
  const ctx = await newContext(DESKTOP, { theme: "light" });
  const page = await ctx.newPage();
  attach(page, "print");
  await page.goto(T + "d/235");
  await settle(page);
  const file = join(OUT, "print-digest-235.pdf");
  await page.pdf({ path: file, format: "A4", printBackground: true });
  written.push(file);
  await ctx.close();
}

// ---- 4. soft navigation ---------------------------------------------------

for (const vp of [PHONE, DESKTOP]) {
  const sfx = `${vp.name}-light`;
  const ctx = await newContext(vp, { theme: "light" });
  const page = await ctx.newPage();
  attach(page, `softnav-${sfx}`);
  try {
    await page.goto(T);
    await settle(page);
    // A marker on window survives only if navigation stays in-document.
    await page.evaluate(() => (window.__softnav = "alive"));
    await shot(page, `softnav-1-index-${sfx}`, { fullPage: false });
    await page.click("a.entry[href$='/d/235']");
    await page.waitForURL(/\/d\/235$/);
    await settle(page);
    const stayed = await page.evaluate(() => window.__softnav === "alive");
    if (!stayed)
      notes.push(`softnav-${sfx}: click into digest was a full page load, not a soft swap`);
    await shot(page, `softnav-2-digest-${sfx}`, { fullPage: false });
    await page.goBack();
    await page.waitForURL(new RegExp(`/t/${SITE_TOKEN}/$`));
    await settle(page);
    const stayedBack = await page.evaluate(() => window.__softnav === "alive");
    if (!stayedBack) notes.push(`softnav-${sfx}: back was a full page load, not a soft swap`);
    await shot(page, `softnav-3-back-index-${sfx}`, { fullPage: false });
  } catch (e) {
    notes.push(`softnav-${sfx}: ${e.message.split("\n")[0]}`);
  }
  await ctx.close();
}

// ---- summary --------------------------------------------------------------

await browser.close();
server.close();

console.log(`\n${written.length} files written to ${OUT}`);
for (const f of written) console.log("  " + f.slice(OUT.length + 1));
if (notes.length) {
  console.log(`\n${notes.length} note(s):`);
  for (const n of notes) console.log("  - " + n);
}
if (problems.length) {
  const uniq = [...new Set(problems)];
  console.error(`\n${uniq.length} problem(s):`);
  for (const p of uniq) console.error("  - " + p);
  process.exit(1);
}
