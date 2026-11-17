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
// failed requests. The state selectors below (button.settingsbtn, #settings,
// summary.searchtoggle, .catchup, .cmdpalette-*, a.entry, …)
// are tied to the CURRENT markup. Section 5 also asserts client behaviours
// (listener hygiene after two soft navigations, folded sections, the
// settings popover); a failed assertion is reported as a problem and exits
// non-zero. No dependencies are added: Playwright is resolved from the
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

// A behavioural assertion: recorded as a problem (non-zero exit) on failure.
function check(name, ok, detail = "") {
  if (!ok) problems.push(`[check failed] ${name}${detail ? ": " + detail : ""}`);
  else console.log(`  ok  ${name}`);
}

async function shot(page, name, opts = {}) {
  const file = join(OUT, `${name}.png`);
  await page.screenshot({ path: file, fullPage: opts.fullPage ?? true });
  written.push(file);
}

async function settle(page) {
  await page.waitForLoadState("load");
  await page.evaluate(() => document.fonts?.ready);
  // The entrance (html.enter) runs ~1.2s after a hard load; shoot the settled
  // page, not the rise-in. Soft-navigated pages never have the class.
  await page
    .waitForFunction(() => !document.documentElement.classList.contains("enter"), null, {
      timeout: 4000,
    })
    .catch(() => {});
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
  // Pin Date at the fixture's "now" so relative labels match -- by shimming
  // Date in an init script, the same way env.mjs does for the Worker. NOT
  // ctx.clock.install / setFixedTime: both leave setTimeout and
  // requestAnimationFrame stuck in this Playwright version, which silently
  // freezes every timer in the page (entrance removal, theming window,
  // debounces) and made timer-driven behaviour invisible to this harness.
  await ctx.addInitScript((now) => {
    const Real = Date;
    const Frozen = class extends Real {
      constructor(...a) {
        if (a.length === 0) super(now);
        else super(...a);
      }
      static now() {
        return now;
      }
    };
    window.Date = Frozen;
  }, new Date(FROZEN_NOW).getTime());
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

    // Top edge (phone): the iOS status-bar strip is flat theme-color (= --bg),
    // so the topmost rows of the page must be plain --bg: the glow layer is
    // masked to fade in below the edge.
    if (vp === PHONE) {
      await state(vp, theme, `top-edge-${sfx}`, async (page) => {
        await page.goto(T);
        await settle(page);
        const mask = await page.evaluate(() => {
          const cs = getComputedStyle(document.body, "::after");
          return cs.maskImage !== "none" ? cs.maskImage : cs.webkitMaskImage;
        });
        check(`glow layer is masked at the top edge (${sfx})`, !!mask && mask !== "none", String(mask));
        const file = join(OUT, `state-top-edge-${sfx}.png`);
        await page.screenshot({ path: file, clip: { x: 0, y: 0, width: 390, height: 120 } });
        written.push(file);
      });
    }

    // Settings: a native popover. Phone = bottom sheet, desktop arc page =
    // anchored popover under the bar; the desktop index keeps the panel in
    // the sidebar (a closed popover forced visible by CSS), so it is only
    // photographed, never clicked.
    if (vp === PHONE || theme === "light") {
      await state(vp, theme, `settings-open-${sfx}`, async (page) => {
        await page.goto(vp === PHONE ? T : T + "a/markets-slide");
        await settle(page);
        await page.click("button.settingsbtn");
        await page.waitForSelector("#settings:popover-open", { state: "visible" });
        await page.waitForTimeout(400);
        await shot(page, `state-settings-open-${sfx}`, { fullPage: false });
      });
    }
    if (vp === DESKTOP) {
      await state(vp, theme, `settings-sidebar-${sfx}`, async (page) => {
        await page.goto(T);
        await settle(page);
        const vis = await page.isVisible("#settings");
        if (!vis) throw new Error("sidebar settings panel is not visible");
        await shot(page, `state-settings-sidebar-${sfx}`, { fullPage: false });
      });
    }

    // Phone: the panel opens upward from the dock.
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

    // Digest sections start open; exercise collapse/expand and hash navigation.
    await state(vp, theme, `digest-expanded-${sfx}`, async (page) => {
      await page.goto(T + "d/235");
      await settle(page);
      await page.waitForSelector("nav.toc");
      await shot(page, `state-digest-default-${sfx}`, { fullPage: false });
      // A long section title should wrap within the full-width navigation.
      await page.evaluate(() => {
        const t = document.querySelector(".toc a .toctitle");
        if (t) t.textContent = "The ground stop ends, and Duffy wants $" + "9 billion more for the air traffic system";
        document.querySelector(".toc").scrollIntoView({ block: "center" });
      });
      await page.waitForTimeout(200);
      await shot(page, `state-digest-toc-${sfx}`, { fullPage: false });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.locator("summary.sechead").first().click();
      check("individual story collapses", await page.locator("details.sec").first().evaluate((d) => !d.open));
      await page.locator("summary.sechead").first().click();
      check("individual story reopens", await page.locator("details.sec").first().evaluate((d) => d.open));
      await shot(page, `state-digest-reopened-${sfx}`);
    });

    await state(vp, theme, `digest-hash-${sfx}`, async (page) => {
      await page.goto(T + "d/235#s2");
      await settle(page);
      const st = await page.evaluate(() => {
        const h = document.querySelector("h2#s2");
        return {
          open: !!h && h.closest("details.sec").open,
          top: h ? Math.round(h.getBoundingClientRect().top) : null,
          vh: innerHeight,
        };
      });
      if (!st.open) throw new Error("section 2 not open after #s2 load");
      if (st.top === null || st.top < 0 || st.top > st.vh)
        notes.push(`#s2 heading off screen at ${sfx} (top ${st.top}/${st.vh})`);
      await shot(page, `state-digest-hash-s2-${sfx}`, { fullPage: false });
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
  // Collapse sections first; the print handler must open them for the PDF.
  await page.locator("summary.sechead").last().click();
  const closedBefore = await page.evaluate(() => document.querySelectorAll("details.sec:not([open])").length);
  const file = join(OUT, "print-digest-235.pdf");
  await page.pdf({ path: file, format: "A4", printBackground: true });
  written.push(file);
  const restored = await page.evaluate(() => document.querySelectorAll("details.sec:not([open])").length);
  check("print: sections start folded", closedBefore >= 1, String(closedBefore));
  check("print: afterprint restores the folded state", restored === closedBefore, `${restored} vs ${closedBefore}`);
  // The folded-section body text must be IN the PDF.
  const bodyText = await page.evaluate(() => {
    const d = [...document.querySelectorAll("details.sec")].pop();
    return d.querySelector(".secbody").textContent.trim().split(/\s+/).slice(0, 4).join(" ");
  });
  const { spawnSync } = await import("node:child_process");
  const txt = spawnSync("pdftotext", [file, "-"], { encoding: "utf8" });
  if (txt.error) notes.push("pdftotext unavailable: could not verify folded sections are printed");
  else check("print: last (folded) section body is in the PDF", txt.stdout.replace(/\s+/g, " ").includes(bodyText), bodyText);
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

// ---- 5. behaviour after TWO soft navigations ------------------------------
// The wirePage() teardown/rebind cycle is where client.js breaks: prove one
// listener set survives (not one per navigation), html.enter stays gone, and
// an open settings popover is closed before the swap.

console.log("\nbehaviour checks");
{
  const ctx = await newContext(PHONE, { theme: "light" });
  const page = await ctx.newPage();
  attach(page, "behaviour-phone");
  try {
    await page.goto(T);
    await settle(page);
    await page.evaluate(() => (window.__softnav = "alive"));
    // Wait out the entrance window, then hop twice: index -> digest -> arc.
    await page.waitForFunction(() => !document.documentElement.classList.contains("enter"), null, { timeout: 4000 });
    check("enter removed ~1.2s after a hard load", true);

    // Hop 1: open the settings sheet, then follow the language link INSIDE
    // it, so the popover is open when the swap starts.
    await page.click("button.settingsbtn");
    await page.waitForSelector("#settings:popover-open");
    await page.click("#settings .langswitch a");
    await page.waitForURL(/\/hu\//);
    await settle(page);
    check("hop 1 stayed in-document", await page.evaluate(() => window.__softnav === "alive"));
    check(
      "settings popover closed by the soft navigation",
      await page.evaluate(() => !document.querySelector("#settings").matches(":popover-open")),
    );
    check("html.enter not re-added by hop 1", await page.evaluate(() => !document.documentElement.classList.contains("enter")));

    // Hop 2: into a digest (hash target included -> section 3 opens).
    await page.goto(T + "hu/");
    await settle(page);
    await page.evaluate(() => (window.__softnav = "alive"));
    await page.click("a.entry[href*='/d/235']");
    await page.waitForURL(/\/d\/235$/);
    await settle(page);
    check("hop 2 stayed in-document", await page.evaluate(() => window.__softnav === "alive"));
    check("html.enter not re-added by hop 2", await page.evaluate(() => !document.documentElement.classList.contains("enter")));
    // Hop 3 so the digest page has been wired by the SECOND soft swap.
    await page.click(".digestnav a");
    await page.waitForURL(new RegExp(`/t/${SITE_TOKEN}/hu/$`));
    await settle(page);
    await page.click("a.entry[href*='/d/235']");
    await page.waitForURL(/\/d\/235$/);
    await settle(page);
    check("hop 3 stayed in-document", await page.evaluate(() => window.__softnav === "alive"));

    // Individual story controls still work after repeated soft navigation.
    await page.locator("summary.sechead").first().click();
    check("story collapses after soft navigation", await page.locator("details.sec").first().evaluate((d) => !d.open));
    await page.locator("summary.sechead").first().click();
    check("story reopens after soft navigation", await page.locator("details.sec").first().evaluate((d) => d.open));

    // TOC click opens a folded section.
    await page.evaluate(() => document.querySelectorAll("details.sec").forEach((d) => (d.open = false)));
    await page.click('.toc a[href="#s3"]');
    await page.waitForTimeout(200);
    check("TOC #s3 click opens section 3", await page.evaluate(() => document.querySelector("h2#s3").closest("details.sec").open));

    // The #s3 jump scrolled down, so the phone dock is auto-hidden; a small
    // upward scroll brings it back before the settings button is used.
    await page.waitForTimeout(900); // let the smooth #s3 scroll finish first
    await page.evaluate(() => window.scrollBy({ top: -40, behavior: "instant" }));
    await page.waitForTimeout(450);
    // The digest control remains wired after repeated soft navigation.
    const urlBeforeTop = page.url();
    await page.click(".bartop");
    await page.waitForFunction(() => window.scrollY === 0);
    check("top control preserves URL", page.url() === urlBeforeTop);
    // Theme change: html.theming appears, then clears; rapid clicks do not strand it.
    await page.click("button.settingsbtn");
    await page.waitForSelector("#settings:popover-open");
    await page.click(".miniseg-theme .minisegbtn[data-set=dark]");
    // The mutation runs inside the page transition's update callback, i.e.
    // a frame later, so poll for it rather than reading synchronously.
    check(
      "html.theming set on a theme change",
      await page
        .waitForFunction(() => document.documentElement.classList.contains("theming"), null, { timeout: 300, polling: "raf" })
        .then(() => true, () => false),
    );
    await page.click(".miniseg-theme .minisegbtn[data-set=light]");
    await page.waitForTimeout(600);
    check("html.theming cleared afterwards", await page.evaluate(() => !document.documentElement.classList.contains("theming")));
    check(
      "theme-color metas follow THEME_COLORS",
      await page.evaluate(() => [...document.querySelectorAll('meta[name="theme-color"]')].every((m) => m.content === "#eceff6")),
    );
    // Density miniseg lives on the index only; check on the index.
  } catch (e) {
    problems.push(`[behaviour] ${e.message.split("\n").slice(0,12).join(" / ")}`);
  }
  await ctx.close();
}

{
  // Index: density miniseg, hero never filtered, fence placement, typing state.
  const ctx = await newContext(PHONE, { theme: "light", storage: SEEN });
  const page = await ctx.newPage();
  attach(page, "behaviour-index");
  try {
    await page.goto(T);
    await settle(page);
    await page.click("button.settingsbtn");
    await page.waitForSelector("#settings:popover-open");
    await page.click(".miniseg-density .minisegbtn[data-set=compact]");
    check(
      "density compact -> data-density=compact, stored",
      await page
        .waitForFunction(() => document.documentElement.dataset.density === "compact" && localStorage.getItem("density") === "compact", null, { timeout: 2000 })
        .then(() => true, () => false),
    );
    await page.click(".miniseg-density .minisegbtn[data-set=cards]");
    check(
      "density cards -> absence",
      await page
        .waitForFunction(() => document.documentElement.dataset.density === "" && localStorage.getItem("density") === null, null, { timeout: 2000 })
        .then(() => true, () => false),
    );
    await page.keyboard.press("Escape");
    // Fence: after the last NEW entry (lastVisit 10:00: 235 hero + any newer).
    const fence = await page.evaluate(() => {
      const f = document.querySelector(".unreadfence");
      if (!f) return null;
      return { parent: f.parentElement.className, first: f.parentElement.firstElementChild === f };
    });
    check("unread fence present and in the ledger", !!fence && fence.parent.includes("ledger"), JSON.stringify(fence));
    check("fence is the ledger's first child when the hero is the only new entry", !!fence && fence.first, JSON.stringify(fence));
    // Dock auto-hide: scroll down -> html.dockhid and the bar is translated
    // out of view; scroll up -> shown again; an open search popover pins it.
    // The fixture pages are short, so pad .wrap to make them scrollable.
    await page.goto(T + "d/235");
    await settle(page);
    await page.evaluate(() => {
      const pad = document.createElement("div");
      pad.style.height = "3000px";
      document.querySelector(".wrap").appendChild(pad);
    });
    const dockHidden = () => page.evaluate(() => document.documentElement.classList.contains("dockhid"));
    const barOffscreen = () =>
      page.evaluate(() => document.querySelector("header.bar").getBoundingClientRect().top >= window.innerHeight);
    const scrollBy = async (dy) => {
      await page.evaluate((d) => window.scrollBy({ top: d, behavior: "instant" }), dy);
      await page.waitForTimeout(450);
    };
    check("dock shown at the top of the page", !(await dockHidden()));
    await scrollBy(300);
    await scrollBy(300);
    check("scrolling down hides the dock (html.dockhid)", await dockHidden());
    check("hidden dock is translated below the viewport", await barOffscreen());
    await scrollBy(-60);
    check("scrolling up shows the dock again", !(await dockHidden()) && !(await barOffscreen()));
    await page.goto(T);
    await settle(page);
    await page.evaluate(() => {
      const pad = document.createElement("div");
      pad.style.height = "3000px";
      document.querySelector(".wrap").appendChild(pad);
    });
    await page.click("summary.searchtoggle");
    await page.waitForSelector("details.searchpop[open]");
    await scrollBy(400);
    await scrollBy(400);
    check("open search popover keeps the dock visible", !(await dockHidden()));
    await page.keyboard.press("Escape");
    await page.evaluate(() => document.activeElement && document.activeElement.blur());
    await scrollBy(400);
    check("dock hides again once the popover is closed", await dockHidden());
    // Typing: focusing the search page input hides the dock via html.typing
    await page.goto(T + "search?q=markets");
    await settle(page);
    await page.focus(".searchform .filter, form .filter");
    check("html.typing set on phone text focus", await page.evaluate(() => document.documentElement.classList.contains("typing")));
    await page.evaluate(() => document.activeElement.blur());
    await page.waitForTimeout(50);
    check("html.typing cleared on blur", await page.evaluate(() => !document.documentElement.classList.contains("typing")));
  } catch (e) {
    problems.push(`[behaviour-index] ${e.message.split("\n")[0]}`);
  }
  await ctx.close();
}

{
  // Desktop index: filter must never hide the hero; keyboard nav starts at it.
  const ctx = await newContext(DESKTOP, { theme: "light" });
  const page = await ctx.newPage();
  attach(page, "behaviour-desktop");
  try {
    await page.goto(T);
    await settle(page);
    await page.hover("summary.searchtoggle");
    await page.click("summary.searchtoggle").catch(() => {});
    await page.waitForSelector("details.searchpop[open] .filter", { state: "visible" });
    await page.fill("details.searchpop .filter", "zzzznomatch");
    await page.waitForTimeout(200);
    check("filter never hides the hero", await page.evaluate(() => !document.querySelector(".hero .entry").hidden));
    check("filter hides ledger entries", await page.evaluate(() => [...document.querySelectorAll(".ledger .entry")].every((e) => e.hidden)));
    await page.fill("details.searchpop .filter", "");
    await page.keyboard.press("Escape");
    await page.evaluate(() => document.activeElement && document.activeElement.blur());
    await page.keyboard.press("j");
    check("j focuses the hero first", await page.evaluate(() => document.activeElement.closest(".hero") !== null));
    await page.keyboard.press("j");
    check("second j moves to the first ledger entry", await page.evaluate(() => document.activeElement.closest(".ledger") !== null));
    // Widening closes an open popover that lives in .side: only simulable on
    // the arc page's phone->desktop path; here assert the sidebar copy is
    // not left :popover-open.
    check("sidebar settings is not a top-layer popover", await page.evaluate(() => !document.querySelector("#settings").matches(":popover-open")));
  } catch (e) {
    problems.push(`[behaviour-desktop] ${e.message.split("\n")[0]}`);
  }
  await ctx.close();
}

{
  // Viewport resize 700 -> 1280: an open settings popover inside .side closes.
  const ctx = await browser.newContext({ viewport: { width: 900, height: 800 } });
  const page = await ctx.newPage();
  attach(page, "behaviour-resize");
  try {
    await page.goto(T);
    await settle(page);
    await page.click("button.settingsbtn");
    await page.waitForSelector("#settings:popover-open");
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.waitForTimeout(300);
    check("popover in .side closes when the viewport reaches 1040px", await page.evaluate(() => !document.querySelector("#settings").matches(":popover-open")));
  } catch (e) {
    problems.push(`[behaviour-resize] ${e.message.split("\n")[0]}`);
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
