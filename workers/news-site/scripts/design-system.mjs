#!/usr/bin/env node
// Generates the file set for the "Digest News" design-system artifact on
// claude.ai from this Worker's own source, so the artifact's palette, type,
// radii, logos and component cards can be re-synced whenever the site's look
// changes instead of being retyped by hand.
//
// WHY it is a script and not a one-off: the first import was done with a
// scratch script that hard-coded the palette. A hard-coded copy drifts the
// first time someone edits src/css.js, and the artifact then documents a site
// that no longer exists. So every token is PARSED out of the stylesheet, and
// every custom property found must be documented below. A new `--something`
// in css.js with no usage note here makes buildDesignSystem() throw — and
// test/design-system.test.mjs runs it against the real stylesheet, so
// `npm test` fails until the note is written. That is the point: nobody adds
// a token without deciding how it is described.
//
// THE PARSE CONTRACT (written into the head of css.js, which owns it):
//   - exactly one block between `/* tokens:start */` and `/* tokens:end */`,
//     holding one `:root { ... }` rule;
//   - a themed colour is one line, `--name: light-dark(<light>, <dark>);`,
//     split here on the top-level comma (rgba() has commas of its own);
//   - every other token is single-valued in the same block;
//   - `--shadow` is the one themed value that is not a whole colour: two
//     layers with a `light-dark()` inside each. It is classified as a shadow
//     token and resolved per theme by taking one arm of every inner pair;
//   - the data-theme blocks hold `color-scheme` and nothing else (checked).
// A phone-width override of `--gutter` / `--fs-read` lives outside the block
// in the phone media query; the usage notes say so, the parser ignores it.
//
// What is NOT drift-checked: the type styles' names, weights and samples, the
// contrast pairs, and every component card's markup. They mirror class names
// in css.js and the render-*.js files and are curated by hand (sizes are read
// from the parsed tokens where the style uses one). Edit them here when the
// site changes; nothing will tell you if you forget.
//
// What it produces, under <out-dir>/project/ (paths as the artifact expects):
//   README.md, tokens.json, design-system.json
//   components/bundle.css          the site's stylesheet minus its token block
//   components/<Name>/preview.html + README.md   (one per component)
//   components/Cover/preview.html
//   assets/Logos/*, assets/Icons/*  (favicon SVG + the three PWA PNGs)
// It does NOT write tokens.css: the artifact page generates that itself from
// tokens.json. Token names there are the css.js custom-property names.
//
// This script only WRITES FILES. It does not publish anything. Publishing is
// done afterwards from Claude Code against the existing artifact, and two
// things matter there:
//   - On a re-sync the artifact's existing design-system.json must be READ and
//     MERGED (keep its asset records and keys), never overwritten with the
//     fresh one written here: this script's copy has empty asset groups, the
//     artifact's has the uploaded files' ids.
//   - The logo/icon images are uploaded as artifact ASSETS, not as plain
//     project files, so the PNGs and favicon here are the upload sources.
// No artifact URL appears anywhere in the output: this repository is public.
//
// Usage (from workers/news-site):
//
//   npm run design-system                  # writes $TMPDIR/digest-news-design-system/project
//   npm run design-system -- /some/dir     # writes /some/dir/project
//
// Only <out-dir>/project is removed and recreated; anything else in <out-dir>
// is left alone. Prints the output path, the contrast table and the component
// list. Node built-ins only, no dependencies.

import { execFileSync } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// ── Token parsing ───────────────────────────────────────────────────────────
const TOKENS_START = "/* tokens:start */";
const TOKENS_END = "/* tokens:end */";
const BRAND_MARK = "#9db0ff";

const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");

// Index of the `)` that closes the `(` at `open`.
function closeParen(s, open) {
  let depth = 0;
  for (let i = open; i < s.length; i++) {
    if (s[i] === "(") depth++;
    else if (s[i] === ")" && --depth === 0) return i;
  }
  throw new Error(`design-system: unbalanced parentheses in token value: ${s}`);
}

// Split on commas that are not inside parentheses.
function splitTop(s) {
  const parts = [];
  let depth = 0;
  let from = 0;
  for (let i = 0; i < s.length; i++) {
    if (s[i] === "(") depth++;
    else if (s[i] === ")") depth--;
    else if (s[i] === "," && depth === 0) {
      parts.push(s.slice(from, i).trim());
      from = i + 1;
    }
  }
  parts.push(s.slice(from).trim());
  return parts;
}

// `light-dark(a, b)` as the whole value -> [a, b]; anything else -> null.
function wholePair(value) {
  if (!value.startsWith("light-dark(")) return null;
  const open = "light-dark".length;
  if (closeParen(value, open) !== value.length - 1) return null;
  const arms = splitTop(value.slice(open + 1, -1));
  if (arms.length !== 2) {
    throw new Error(`design-system: light-dark() needs exactly two arms: ${value}`);
  }
  return arms;
}

// Replace every light-dark() pair anywhere in `value` with one arm.
function resolve(value, theme) {
  let out = "";
  let at = 0;
  for (;;) {
    const k = value.indexOf("light-dark(", at);
    if (k < 0) return (out + value.slice(at)).replace(/\s+/g, " ").trim();
    const open = k + "light-dark".length;
    const close = closeParen(value, open);
    const arms = splitTop(value.slice(open + 1, close));
    if (arms.length !== 2) throw new Error(`design-system: light-dark() needs two arms: ${value}`);
    out += value.slice(at, k) + resolve(arms[theme === "light" ? 0 : 1], theme);
    at = close + 1;
  }
}

// Every `--name: value;` of the single token block, in source order.
function parseTokens(css) {
  const start = css.indexOf(TOKENS_START);
  const end = css.indexOf(TOKENS_END);
  if (start < 0 || end < start || css.indexOf(TOKENS_START, start + 1) >= 0) {
    throw new Error("design-system: css.js needs exactly one tokens:start ... tokens:end block");
  }
  const block = stripComments(css.slice(start + TOKENS_START.length, end));
  const rule = block.match(/^\s*:root\s*\{([\s\S]*)\}\s*$/);
  if (!rule) throw new Error("design-system: the token block must be a single :root rule");
  const map = new Map();
  for (const [, name, value] of rule[1].matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) {
    if (map.has(name))
      throw new Error(`design-system: --${name} is defined twice in the token block`);
    map.set(name, value.trim().replace(/\s+/g, " "));
  }
  // The theme blocks only switch color-scheme; a colour there would be a
  // second source of truth that this parser never sees.
  const outside = stripComments(css.slice(0, start) + css.slice(end));
  for (const [, theme, body] of outside.matchAll(
    /:root\[data-theme="(light|dark)"\]\s*\{([^}]*)\}/g,
  )) {
    if (!/^\s*color-scheme:\s*[^;]+;?\s*$/.test(body)) {
      throw new Error(
        `design-system: the data-theme="${theme}" block must contain only color-scheme, found: ${body.trim()}`,
      );
    }
  }
  return map;
}

// ── Colour maths (contrast audit) ───────────────────────────────────────────
function parseColor(text) {
  const t = text.trim();
  if (t === "transparent") return [0, 0, 0, 0];
  let m = t.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (m) {
    const h = m[1].length === 3 ? [...m[1]].map((c) => c + c).join("") : m[1];
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)).concat(1);
  }
  m = t.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/);
  if (m) return [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]];
  return null;
}
const over = (top, base) => {
  const a = top[3];
  return [0, 1, 2].map((i) => top[i] * a + base[i] * (1 - a)).concat(1);
};
const lum = ([r, g, b]) => {
  const c = [r, g, b]
    .map((v) => v / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m);
  return (x + 0.05) / (y + 0.05);
};

// [foreground, [surface layers, bottom first]]. Translucent layers are
// composited in order, so "panel on bg" is the glass card as it renders.
const PAIRS = [
  ["text", ["bg"]],
  ["strong", ["bg"]],
  ["body", ["bg"]],
  ["muted", ["bg"]],
  ["link", ["bg"]],
  ["accent", ["bg"]],
  ["accent-text", ["bg"]],
  ["daily", ["bg"]],
  ["text", ["bg", "panel"]],
  ["body", ["bg", "panel"]],
  ["muted", ["bg", "panel"]],
  ["accent-text", ["bg", "panel", "accent-soft"]],
  ["daily", ["bg", "panel", "daily-soft"]],
  ["strong", ["bg", "pill"]],
  ["muted", ["bg", "track"]],
  ["on-accent", ["accent"]],
  ["text", ["sheet-solid"]],
  ["muted", ["sheet-solid"]],
];
const pairKey = (fg, layers) => `${fg}/${layers.join("+")}`;

// ── Usage notes: one per custom property in the token block ─────────────────
// Themed colours (a whole light-dark() pair). The measured ratios are woven
// into a few notes, so this is a function of the audit lookup `r`.
function colorUsage(r) {
  return {
    bg: "The page. Canvas under the dot grid and the glow; the surface every other colour is measured on.",
    dot: "The dot-grid dots on the page background (`body::before`). Decorative.",
    glow: "The soft radial wash at the top of the page (`body::after`), the one slow-drifting layer.",
    text: "Default ink: body copy in controls, list rows, the settings panel.",
    strong: "Strongest ink: headlines, the brand, active tabs, strong/em inside prose.",
    body: `Reading text: excerpts, the hero subline, article paragraphs. One step under \`strong\` so headlines lead (${r("body/bg", "light")}:1 light, ${r("body/bg", "dark")}:1 dark on \`bg\`).`,
    muted: `Secondary text: counts, metadata, settings labels, inactive tabs. On \`bg\` ${r("muted/bg", "light")}:1 light, ${r("muted/bg", "dark")}:1 dark.`,
    panel:
      "The flat glass card: entries, side cards, sections, the TL;DR block. Translucent over the page; never blurred.",
    "panel-2":
      "A quieter panel: nested notes, the week rail, delta rows, the Background disclosure.",
    border: "1px hairline around panels, cards and rows.",
    "border-strong":
      "Hairline on floating surfaces and controls: the bar, popovers, inputs, the follow toggle.",
    pill: "Fill of the active segment (view tab, settings miniseg) and of the hero chip: a raised pill on a `track`.",
    track: "The groove behind a segmented control and the fill of the quiet bar buttons.",
    accent:
      "Indigo-blue brand colour: the settings button, primary CTA, search button, focus ring, live dot, timeline rail dots.",
    "accent-soft":
      "Tint of `accent`: hover wash, thread pills, cite chips, `mark` highlight, the catch-up strip.",
    "accent-line":
      "Border of accent-tinted surfaces: thread pills, the TL;DR card, the timeline rail, the unread fence.",
    "accent-text":
      "Accent used as text on a tint or on `bg`: times, eyebrow labels, counts in pills, section numbers.",
    "on-accent": "Text and glyphs on a solid `accent` fill.",
    sheet:
      "Translucent surface of the three blurred things: the bar, an open settings popover, the command palette.",
    "sheet-solid":
      "Opaque twin of `sheet`: the search popover (it sits inside the blurred bar), the phone sheet, the resume chip, and the fallback when blur is unavailable or reduced.",
    link: "Inline links and link-like rows (search link, settings About link).",
    daily: "Amber text: daily and weekly briefs, the partial-run flag, the attention label.",
    "daily-soft":
      "Amber tint behind daily and weekly entries, the attention callout and the collection-failed banner.",
    "daily-line": "Border of amber-tinted surfaces.",
    scrim: "Dim layer behind the command palette and the phone settings sheet.",
  };
}

// Themed values that are NOT a whole colour (light-dark() inside a longer value).
const SHADOW_USAGE = {
  shadow:
    "Two stacked layers: a cool light-mode lift and a deep dark-mode one, each transparent in the other theme. The bar, popovers, the command palette and the resume chip.",
};

// Single-valued tokens: [group in tokens.json, usage note].
const PLAIN_USAGE = {
  "font-display": [
    "font",
    "Sora with a system fallback. Brand, hero and section headings, entry times, day heads.",
  ],
  "font-body": ["font", "Inter with a system fallback. Everything else, including article prose."],
  "fs-hero": ["type", "Index hero headline. Fluid, 34px to 60px."],
  "fs-hero-sm": ["type", "Digest, thread and page titles. Fluid, 28px to 44px."],
  "fs-h2": ["type", "Section heading in a digest."],
  "fs-card": ["type", "Entry headline and the About card title."],
  "fs-read": [
    "type",
    "Article reading size, before `read-scale`. 16px on phones (the phone media query overrides it).",
  ],
  "lh-read": ["type", "Article reading line height."],
  "fs-ui": ["type", "Controls, nav, settings rows, search."],
  "fs-meta": ["type", "Counts, chips, the issue line, the catch-up strip."],
  "fs-micro": ["type", "Smallest labels: section counts, tags, uppercase eyebrows."],
  "read-scale": [
    "type",
    "Multiplier for reading text; the S and L text sizes redefine it on `html[data-fontsize]` (0.94 and 1.12). Headings and controls never scale.",
  ],
  "sp-1": ["spacing", "4px."],
  "sp-2": ["spacing", "8px. Gap inside control groups."],
  "sp-3": ["spacing", "12px. Gap between ledger cards."],
  "sp-4": ["spacing", "16px. Phone card padding, gap between digest blocks."],
  "sp-5": ["spacing", "24px. Card padding on wide screens."],
  "sp-6": ["spacing", "32px. Column gap between main and side."],
  "sp-7": ["spacing", "48px. Hero top padding."],
  "sp-8": ["spacing", "72px. Page bottom padding."],
  maxw: ["layout", "Maximum width of the whole page."],
  "side-w": ["layout", "Sidebar column width from 1040px."],
  "main-min": ["layout", "Smallest the main column may get beside the sidebar."],
  gutter: ["layout", "Page side padding; 16px on phones (the phone media query overrides it)."],
  "bar-h": ["layout", "Height of the pill bar."],
  "dock-h": ["layout", "Phone dock height, used to keep content clear of the bottom bar."],
  "r-card": ["radius", "Cards: entries, sections, side cards, the TL;DR block, popovers."],
  "r-lg": ["radius", "Top corners of the phone settings sheet."],
  "r-in": ["radius", "Inner rows and notes: list rows, deltas, the banner, palette items."],
  "r-pill": ["radius", "Every control: bar, tabs, buttons, chips, flags, inputs."],
  "dur-fast": ["motion", "Hover border colour."],
  dur: ["motion", "Hover lift, popover and section unfold."],
  "dur-slow": ["motion", "Slower fades. Reserved."],
  "dur-enter": ["motion", "The page-load rise of hero, columns and cards, staggered 60ms apart."],
  "dur-drift": ["motion", "One cycle of the background glow drift."],
  "ease-out": ["motion", "Entrances and lifts."],
  "ease-io": ["motion", "The ambient glow drift."],
  "blur-chrome": ["effects", "Backdrop blur of the bar."],
  "blur-sheet": ["effects", "Backdrop blur of the settings popover and the command palette."],
  "z-bg": ["effects", "Background layers."],
  "z-chip": ["effects", "The resume chip."],
  "z-bar": ["effects", "The bar."],
  "z-pop": ["effects", "Popovers anchored to the bar."],
  "z-palette": ["effects", "The command palette, above everything."],
};
const PLAIN_GROUPS = ["font", "type", "spacing", "layout", "radius", "motion", "effects"];

function parseDesignTokens(css, faviconSvg) {
  const raw = parseTokens(css);
  const colors = new Map(); // name -> { light, dark }
  const shadows = new Map();
  const plain = new Map();
  for (const [name, value] of raw) {
    const pair = wholePair(value);
    if (pair && pair.every((a) => parseColor(a))) {
      colors.set(name, { light: pair[0], dark: pair[1] });
    } else if (value.includes("light-dark(")) {
      shadows.set(name, { light: resolve(value, "light"), dark: resolve(value, "dark") });
    } else {
      plain.set(name, value);
    }
  }
  if (!faviconSvg.includes(`fill="${BRAND_MARK}"`)) {
    throw new Error(
      `design-system: FAVICON_SVG no longer draws the ${BRAND_MARK} dot; update brand-mark`,
    );
  }

  // Usage notes <-> CSS, both directions, per kind. A token that moved kind
  // (a plain value that became a pair, say) shows up as undocumented in the
  // new kind and stale in the old one.
  const kinds = [
    ["colour", [...colors.keys()], Object.keys(colorUsage(() => "0"))],
    ["shadow", [...shadows.keys()], Object.keys(SHADOW_USAGE)],
    ["plain", [...plain.keys()], Object.keys(PLAIN_USAGE)],
  ];
  for (const [kind, found, documented] of kinds) {
    const undocumented = found.filter((n) => !documented.includes(n));
    if (undocumented.length) {
      throw new Error(
        `design-system: css.js defines ${kind} token --${undocumented.join(", --")} with no usage note in scripts/design-system.mjs; document it`,
      );
    }
    const stale = documented.filter((n) => !found.includes(n));
    if (stale.length) {
      throw new Error(
        `design-system: usage note for ${kind} token --${stale.join(", --")} but css.js no longer defines it that way; fix or remove the note`,
      );
    }
  }
  return { colors, shadows, plain };
}

// ── The build (pure: no fs, no git, no clock) ───────────────────────────────
/**
 * @param {{ css: string, faviconSvg: string, icons: { ICON_512: Uint8Array,
 *   ICON_MASKABLE_512: Uint8Array, APPLE_TOUCH_ICON: Uint8Array },
 *   sha: string, by: string, now: Date }} input
 * @returns {{ files: Map<string, string | Uint8Array>,
 *   audit: Record<string, [number, number]>, components: string[] }}
 */
export function buildDesignSystem({ css, faviconSvg, icons, sha, by, now }) {
  const files = new Map();
  const put = (rel, data) => files.set(rel, data);
  const { colors, shadows, plain } = parseDesignTokens(css, faviconSvg);

  const audit = {};
  const solid = (name, theme, layers) => {
    let base = null;
    for (const layer of layers) {
      const c = colors.get(layer);
      const v = parseColor(c[theme]);
      if (base === null) {
        if (v[3] !== 1) throw new Error(`design-system: contrast base --${layer} is not opaque`);
        base = v;
      } else base = over(v, base);
    }
    return base;
  };
  for (const [fg, layers] of PAIRS) {
    for (const n of [fg, ...layers]) {
      if (!colors.has(n))
        throw new Error(`design-system: contrast pair names unknown colour --${n}`);
    }
    const [l, d] = ["light", "dark"].map((theme) => {
      const bg = solid(null, theme, layers);
      const f = parseColor(colors.get(fg)[theme]);
      return ratio(f[3] === 1 ? f : over(f, bg), bg);
    });
    audit[pairKey(fg, layers)] = [l, d];
  }
  const r = (k, t) => audit[k][t === "light" ? 0 : 1].toFixed(1);
  const lowest = Object.entries(audit)
    .flatMap(([k, [l, d]]) => [
      [k, "light", l],
      [k, "dark", d],
    ])
    .sort((a, b) => a[2] - b[2])[0];

  // ── bundle.css: the site's real stylesheet minus its token block ──
  const start = css.indexOf(TOKENS_START);
  const end = css.indexOf(TOKENS_END) + TOKENS_END.length;
  const body = css.slice(0, start) + css.slice(end);
  if (/<\/style/i.test(body)) throw new Error("</style in CSS");
  put(
    "components/bundle.css",
    `/* Digest News — the news site's own stylesheet, lifted verbatim from
   workers/news-site/src/css.js @ ${sha}, minus its token block (the one
   between its two token markers). Those values live in tokens.json now
   and reach this file as tokens.css, so a token edit on the page reaches
   every preview. The font-face rules point at the site's /fonts files, which
   the artifact does not ship: previews fall back to the system stack. */
${body}
/* Preview stage for this design system's cards. Not part of the site. */
body:has(> .ds-stage) { padding: 16px; }
.ds-stage > :first-child { margin-top: 0; }
.ds-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
.ds-note { margin: 18px 0 8px 8px; font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
/* The settings panel is a popover that is only visible while open; cards show
   it as the open popover looks. */
.settingspanel.ds-open { display: flex; flex-direction: column; gap: var(--sp-4); max-width: 340px; padding: 22px; font-size: var(--fs-ui); border: 1px solid var(--border-strong); border-radius: var(--r-card); background: var(--sheet-solid); box-shadow: var(--shadow); }
`,
  );

  // ── Assets (copied byte for byte from the worker's sources) ──
  put("assets/Logos/favicon.svg", faviconSvg);
  put("assets/Logos/icon-512.png", icons.ICON_512);
  put("assets/Logos/icon-maskable-512.png", icons.ICON_MASKABLE_512);
  put("assets/Logos/apple-touch-icon.png", icons.APPLE_TOUCH_ICON);
  put(
    "assets/Icons/search.svg",
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16"><circle cx="7" cy="7" r="4.5" fill="none" stroke="#151823" stroke-width="1.6"/><path d="M10.6 10.6 14 14" stroke="#151823" stroke-width="1.6" stroke-linecap="round"/></svg>\n`,
  );
  put(
    "assets/Logos/README.md",
    `# Logos

The mark is the "n." monogram: a bold white lowercase n with a periwinkle dot (\`brand-mark\`, ${BRAND_MARK}) at its lower right, on a deep-ink rounded tile with a soft indigo glow from the top. It is drawn on a 512-unit grid: the n is one 62-wide round-capped stroke, the dot is r34, and the pair is shifted (-16,-20) so it is visually centred.

- \`favicon.svg\` is the source geometry. The three PNGs are renders of it, and each differs only in framing:
  - \`icon-512.png\`: corners rounded (rx 112), transparent outside. The manifest's \`purpose: "any"\` icon.
  - \`icon-maskable-512.png\`: full-bleed ground, mark scaled to 80% so it sits inside Android's circular safe zone (radius 40%).
  - \`apple-touch-icon.png\`: 180×180, square, full bleed. iOS applies its own squircle.
- The installed app is named **news.** (lowercase, with the full stop), matching the page wordmark.
- Use the mark for the tab, Home Screen and push notifications only. Inside the page the brand is the word **news** in Sora with an \`accent\` full stop, set by the \`brand\` style; never place the tile in the bar.
- The dot is the dark theme's \`accent\` colour on purpose: the mark and the page glow in the same periwinkle. Do not recolour the tile or the dot per theme; the icon is one fixed image.
`,
  );
  put(
    "assets/Icons/README.md",
    `# Icons

The site draws two glyphs, both inline SVG with \`stroke="currentColor"\`, 1.6 stroke: the search magnifier (\`search.svg\`, a 16-unit grid) and the settings sliders (20-unit grid, in the bar's settings button). \`search.svg\` is fixed to \`text\` ink in light (#151823) because \`<img>\` cannot inherit colour.

Every other glyph is a text character in the font stack, never an icon file: ← back and previous, → next and the hero CTA, ↑ moved-up thread, ↓ jump to unread, × dismiss and close, ⚠ degraded run, ↻ fallback model, ☆ / ★ follow. Keep it that way: no icon font, no emoji.
`,
  );

  // ── tokens.json ──
  const usage = colorUsage(r);
  const colorTokens = [...colors].map(([name, value]) => ({ name, value, usage: usage[name] }));
  colorTokens.push({
    name: "brand-mark",
    value: BRAND_MARK,
    usage:
      "App-icon dot periwinkle (favicon, Home Screen and notification icons); equals the dark accent. Not a page colour; never use it in the UI.",
  });
  const group = (g) =>
    Object.entries(PLAIN_USAGE)
      .filter(([, [grp]]) => grp === g)
      .map(([name, [, note]]) => ({ name, value: plain.get(name), usage: note }));
  const T = (name) => plain.get(name);
  const stack = (name) => T(name);

  const tokens = {
    name: "Digest News",
    version: 2,
    meta: {
      source: "github",
      repo: "Dezoxy/notification-digest",
      ref: `main@${sha}`,
      package: "workers/news-site",
      paths: {
        tokens: ["workers/news-site/src/css.js"],
        fonts: ["workers/news-site/public/fonts/"],
        assets: [
          "workers/news-site/src/chrome.js",
          "workers/news-site/src/icons.js",
          "workers/news-site/src/hrefs.js",
        ],
        docs: ["workers/news-site/src/css.js", "workers/news-site/test/golden/"],
      },
      synced: now.toISOString().slice(0, 10),
    },
    color: {
      themes: [
        { id: "light", name: "Light" },
        { id: "dark", name: "Dark" },
      ],
      tokens: colorTokens,
    },
    type: {
      fonts: [],
      families: { display: stack("font-display"), body: stack("font-body") },
      scale: group("type"),
      groups: [
        {
          name: "Display",
          family: "display",
          styles: [
            {
              name: "hero",
              fontSize: T("fs-hero"),
              lineHeight: 1.05,
              fontWeight: 600,
              letterSpacing: "-0.035em",
              sample: "Markets slid for a second day while the EU package landed.",
              usage: "Index hero headline (`.hero .headline-lead`), centred, balanced, 20ch.",
            },
            {
              name: "title",
              fontSize: T("fs-hero-sm"),
              lineHeight: 1.05,
              fontWeight: 600,
              letterSpacing: "-0.035em",
              sample: "Markets slide continues",
              usage: "Digest headline, thread title, Search and About page titles (`.herotitle`).",
            },
            {
              name: "section",
              fontSize: T("fs-h2"),
              lineHeight: 1.25,
              fontWeight: 600,
              letterSpacing: "-0.02em",
              sample: "EU agrees new package",
              usage: "Digest section heading (`.sechead h2`).",
            },
            {
              name: "brand",
              fontSize: "22px",
              lineHeight: 1,
              fontWeight: 700,
              letterSpacing: "-0.02em",
              sample: "news.",
              usage:
                "The wordmark (`.brand`), lowercase, with an `accent` full stop (`.brandmark`).",
            },
            {
              name: "time",
              fontSize: "18px",
              lineHeight: 1.5,
              fontWeight: 600,
              sample: "16:00",
              usage:
                "Entry time (`.entry .time`), tabular, in `accent-text` (amber on the daily brief).",
            },
            {
              name: "dayhead",
              fontSize: "15px",
              lineHeight: 1.5,
              fontWeight: 600,
              sample: "Tuesday, 25 August 2026",
              usage: "Ledger day header (`.dayhead`), in `muted`.",
            },
          ],
        },
        {
          name: "Reading",
          family: "body",
          styles: [
            {
              name: "headline",
              fontSize: T("fs-card"),
              lineHeight: 1.3,
              fontWeight: 600,
              letterSpacing: "-0.015em",
              sample: "The sell-off began; two sections follow.",
              usage: "Entry headline (`.headline`), in `strong`, balanced.",
            },
            {
              name: "prose",
              fontSize: `calc(${T("fs-read")} * ${T("read-scale")})`,
              lineHeight: T("lh-read"),
              fontWeight: 400,
              sample: "Equities extended losses into the close, with the drawdown at four percent.",
              usage:
                "Article text (`.secbody`, `.tldr`, About) in `body`; hyphenated. Scales with the reader's text size.",
            },
            {
              name: "subline",
              fontSize: "19px",
              lineHeight: 1.6,
              fontWeight: 400,
              sample: "The council signed off overnight, with disbursement gated on reforms.",
              usage: "Hero subline (`.herosub`), centred, 54ch; 17px on phones.",
            },
            {
              name: "excerpt",
              fontSize: "15px",
              lineHeight: 1.55,
              fontWeight: 400,
              sample: "TL;DR: The sell-off began; two sections follow.",
              usage:
                "Entry excerpt (`.entry .excerpt`) in `body`, clamped to three lines (two when compact).",
            },
          ],
        },
        {
          name: "Interface",
          family: "body",
          styles: [
            {
              name: "ui",
              fontSize: T("fs-ui"),
              lineHeight: 1.5,
              fontWeight: 500,
              sample: "View tabs, buttons, settings rows",
              usage: "Controls and nav; active states go to 600.",
            },
            {
              name: "meta",
              fontSize: T("fs-meta"),
              lineHeight: 1.5,
              fontWeight: 500,
              sample: "9 items · 2 sections",
              usage: "Counts, hero chip, catch-up strip, colophon.",
            },
            {
              name: "label",
              fontSize: T("fs-micro"),
              lineHeight: 1.5,
              fontWeight: 600,
              letterSpacing: "0.08em",
              sample: "EARLIER · NOW · TL;DR",
              usage:
                "Uppercase labels (`.sidehead`, `.archivelabel`, `.tldr-label`, `.unreadfence .label`).",
            },
            {
              name: "cite",
              fontSize: "0.72em",
              lineHeight: 1,
              fontWeight: 700,
              sample: "1",
              usage: "Citation chip (`.cite`), superscript, on `accent-soft`.",
            },
          ],
        },
      ],
    },
    spacing: { tokens: [...group("spacing"), ...group("layout")] },
    radius: { tokens: group("radius") },
    shadow: {
      tokens: [...shadows].map(([name, value]) => ({ name, value, usage: SHADOW_USAGE[name] })),
    },
    motion: {
      note: "Everything animates under prefers-reduced-motion: no-preference only, and only transform and opacity.",
      tokens: group("motion"),
    },
    effects: { tokens: group("effects") },
  };
  put("tokens.json", JSON.stringify(tokens, null, 2) + "\n");

  // ── README (the brand book) ──
  put(
    "README.md",
    `Digest News is the reading surface of a personal notification digest: every six hours a briefing of the owner's Telegram groups, X notifications, RSS, Hacker News and Polymarket, summarized and published as a page. The look is **Spatial Glass**: a dotted page with a soft indigo glow, flat translucent cards with a hairline border, one floating pill bar, and two typefaces, Sora for headings and Inter for everything else. It runs in English and Hungarian, light and dark, on a phone first.

Load \`tokens.css\` then \`components/bundle.css\`: the bundle is the site's own stylesheet, so markup with the class names in each component card renders exactly as production does.

## Content fundamentals

- **Time leads.** Every entry opens with its time in the display face; every page carries a dated chip. Counts and metadata are small, \`muted\`, tabular.
- **Headlines are sentences.** A briefing's headline is its TL;DR's first sentence, in sentence case, ending with a full stop: "Markets slid for a second day while the EU package landed." Never title case, never a teaser.
- **Labels are short.** Small uppercase labels are written in normal case in the markup and uppercased by CSS. The middle dot \`·\` separates facts: "Latest · Tue 25 Aug · 16:00 CEST · 12 items".
- **Plain, factual voice.** No exclamation marks, no "breaking", no emoji. State counts honestly ("6 items · 1 sections" is a known plural gap, not a style).
- **Every string has a Hungarian twin.** Copy lives in \`strings.js\`; leave room for Hungarian's longer compounds (prose is hyphenated with \`hyphens: auto\`; chrome is not).
- **Arrows and symbols are text.** ← → ↑ ↓ × ⚠ ↻ ☆ ★ come from the font. See Iconography.

## Visual foundations

**Colour.** One block of \`light-dark()\` pairs, so a theme is a single declaration per colour. \`bg\` is the page; \`text\`, \`strong\`, \`body\` and \`muted\` are the four ink steps (headlines in \`strong\`, reading text in \`body\`, metadata in \`muted\`). \`panel\` is the translucent card, \`border\` its hairline. \`accent\` is the brand blue: the settings button, the primary CTA, the focus ring; its tint \`accent-soft\` with \`accent-line\` and \`accent-text\` makes thread pills, cite chips and the TL;DR card. Amber (\`daily\`, \`daily-soft\`, \`daily-line\`) is the one other hue and means "a different kind of edition" (daily and weekly briefs) or "attention" (partial run, the attention callout).

**Contrast.** Measured as rendered, translucent layers composited over \`bg\`, light / dark: \`strong\` on \`bg\` ${r("strong/bg", "light")} / ${r("strong/bg", "dark")}; \`body\` ${r("body/bg", "light")} / ${r("body/bg", "dark")}; \`muted\` ${r("muted/bg", "light")} / ${r("muted/bg", "dark")}; \`accent-text\` ${r("accent-text/bg", "light")} / ${r("accent-text/bg", "dark")}; \`muted\` on a \`panel\` card ${r("muted/bg+panel", "light")} / ${r("muted/bg+panel", "dark")}; \`accent-text\` on \`accent-soft\` ${r("accent-text/bg+panel+accent-soft", "light")} / ${r("accent-text/bg+panel+accent-soft", "dark")}; \`daily\` on \`daily-soft\` ${r("daily/bg+panel+daily-soft", "light")} / ${r("daily/bg+panel+daily-soft", "dark")}; \`on-accent\` on \`accent\` ${r("on-accent/accent", "light")} / ${r("on-accent/accent", "dark")}. The lowest measured pair is \`${lowest[0]}\` in ${lowest[1]} at ${lowest[2].toFixed(1)}:1.

**Type.** Two self-hosted variable faces: \`font-display\` (Sora, weight 600 to 700, tight tracking -0.035 to -0.015em) for the wordmark, hero, section headings, entry times and day heads; \`font-body\` (Inter) for everything else. Sizes come from the \`fs-*\` scale. Article text is \`fs-read\` (17px, 16px on phones) at 1.75 leading, multiplied by \`read-scale\` for the reader's S / M / L; only reading text scales.

**Layout.** Pages are \`maxw\` (1200px) wide, centred, with \`gutter\` padding. From 1040px a page with a sidebar becomes two columns, main (at least \`main-min\`) and a \`side-w\` (360px) side column holding the Now and Earlier cards and the settings panel. Narrower, the sidebar stacks below the main column; pages without one are a single 760px column. Spacing is the \`sp-*\` scale (4 to 72px).

**Shape.** Cards are \`r-card\` (24px); inner rows and notes \`r-in\` (14px); every control is a pill (\`r-pill\`). Nothing is square.

**Rules and borders.** 1px \`border\` on cards, \`border-strong\` on floating surfaces and controls. No left-border accent bars; the timeline rail is the one exception and it is a separate element.

**Glass and shadow.** Blur exists on exactly three surfaces: the bar, an open settings popover and the command palette (\`blur-chrome\`, \`blur-sheet\`, with \`sheet\` as their fill). Everything else is a flat \`panel\`; stacked blurs are expensive and nested ones do not composite. Only floating things cast the \`shadow\` token. Reduced transparency or a browser without backdrop-filter swaps those surfaces to \`sheet-solid\`.

**States.** Hover on a card darkens its border and lifts it 2px (hover devices only); on a row or tab it washes to \`accent-soft\` or \`strong\` text. Focus is one 2px \`accent\` ring. Disabled controls are hidden, not greyed.

**Motion.** Only under \`prefers-reduced-motion: no-preference\`, only transform and opacity: the page-load rise (staggered), the drifting background glow, the live-dot ping, card lift, section unfold, the settings popover. No scroll-triggered reveals.

**Themes.** Light, dark, or the OS preference, chosen in Settings and pinned through \`data-theme\` on \`<html>\`, which only switches \`color-scheme\`; \`light-dark()\` does the rest.

**Phone.** Below 700px the bar becomes a floating bottom dock (a back arrow replaces the wordmark on digest pages), a quiet brand and issue line sit at the top, and Settings opens as a bottom sheet.

## Iconography

The mark is the "n." monogram, a white n and a periwinkle dot on a deep-ink tile (\`assets/Logos\`, colour \`brand-mark\`), used for the tab, Home Screen and push notifications only. In the page the brand is the word "news" in Sora with an accent full stop. Two inline SVG glyphs, the search magnifier and the settings sliders, take \`currentColor\`. All other glyphs are Unicode characters in the current font: ← → ↑ ↓ × ⚠ ↻ ☆ ★. No icon font, no emoji.
`,
  );

  // ── Components ──
  const stage = (inner) => `<main class="ds-stage">\n${inner}\n</main>`;
  const C = [];
  const add = (name, group, height, markup, readme, subtitle) =>
    C.push({ name, group, height, markup, readme, subtitle });
  const searchIcon = `<svg class="searchicon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10.6 10.6 14 14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`;
  const slidersIcon = `<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><path d="M3 6h9M16 6h1M3 14h1M8 14h9" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="14" cy="6" r="2" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="6" cy="14" r="2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`;
  const brand = `<a class="brand" href="#">news<span class="brandmark" aria-hidden="true">.</span></a>`;
  const tabs = (active) =>
    `<nav class="viewtabs">${["All", "Daily", "Weekly"]
      .map((t) =>
        t === active
          ? `<span class="viewtab active">${t}</span>`
          : `<a class="viewtab" href="#">${t}</a>`,
      )
      .join("")}</nav>`;

  add(
    "Bar",
    "Chrome",
    150,
    stage(`<header class="bar">
  ${brand}
  ${tabs("All")}
  <div class="baractions">
    <details class="searchpop"><summary class="searchtoggle" aria-label="Search">${searchIcon}<span class="searchlabel">Search</span></summary></details>
    <button type="button" class="settingsbtn" aria-label="Settings">${slidersIcon}</button>
  </div>
</header>`),
    `# Bar

The one floating pill at the top of every page: wordmark, view tabs, Search and Settings. It is the same \`header.bar\` on every breakpoint; below 700px CSS turns it into the bottom dock.

**Provide** \`header.bar\` with the brand link (\`news\` plus \`span.brandmark\` for the accent full stop), a \`nav.viewtabs\`, and \`.baractions\` holding the search control and \`button.settingsbtn\` (\`popovertarget\` the settings panel). A digest page adds \`a.barback\` after the brand; it is shown only on phones.

- Do keep the bar sticky and never put a transform, filter or backdrop-filter on an ancestor: that breaks sticky and the fixed dock.
- Do keep the search control a \`details.searchpop\` where the page has a ledger to filter, and a plain \`a.barsearch\` icon link elsewhere.
- Don't add items; new destinations go in the command palette.

Source: \`chrome.js\`, \`hrefs.js\`; styles \`.bar\`, \`.brand\`, \`.baractions\`, \`.settingsbtn\`, \`.searchtoggle\`.
`,
  );

  add(
    "ViewTabs",
    "Controls",
    100,
    stage(`<div class="ds-row">${tabs("All")}${tabs("Daily")}</div>`),
    `# ViewTabs

A segmented pill on a \`track\` that switches the index between All, Daily and Weekly.

**Provide** a \`nav.viewtabs\` with one \`.viewtab\` per view: the current one a \`span.viewtab.active\` (a raised \`pill\`), the others links.

- Do mark the current view with a \`span\`, not a link, so it is not focusable.
- Do keep labels short; tabs shrink their padding before they wrap, and Hungarian runs about 30% longer.
- Don't add a fourth view without checking the phone dock still fits on one row.

Source: \`hrefs.js\`; styles \`.viewtabs\`, \`.viewtab\`.
`,
  );

  add(
    "SettingsPanel",
    "Controls",
    470,
    stage(`<section class="settingspanel ds-open" aria-label="Settings">
  <div class="settingshead"><h2 class="settingstitle">Settings</h2><button type="button" class="settingsclose" aria-label="Close">×</button></div>
  <div class="settingsrow"><span class="settingslabel">Language</span><span class="langswitch"><strong>EN</strong> | <a href="#">HU</a></span></div>
  <div class="settingsrow"><span class="settingslabel">Theme</span><span class="miniseg"><button class="minisegbtn">Light</button><button class="minisegbtn active">Auto</button><button class="minisegbtn">Dark</button></span></div>
  <div class="settingsrow"><span class="settingslabel">Text size</span><span class="miniseg"><button class="minisegbtn">S</button><button class="minisegbtn active">M</button><button class="minisegbtn">L</button></span></div>
  <div class="settingsrow"><span class="settingslabel">Density</span><span class="miniseg"><button class="minisegbtn active">Cards</button><button class="minisegbtn">Compact</button></span></div>
  <div class="settingsrow"><span class="settingslabel">Notifications</span><button class="pushtoggle" aria-pressed="false">Enable</button></div>
  <div class="settingsrow"><a href="#">About</a><span class="settingslabel appversion">v1.4.2</span></div>
</section>`),
    `# SettingsPanel

Language, theme, text size, density and notifications, each a labelled row with a segmented pill, plus the About link and app version. One element, three presentations: a card in the desktop sidebar, a popover anchored under the bar on tablets, a bottom sheet on phones.

**Provide** a \`section.settingspanel[popover]#settings\` with a \`.settingshead\` (title and \`.settingsclose\`) and \`.settingsrow\`s. Each row is a \`.settingslabel\` plus a control: \`.miniseg\` of \`.minisegbtn\` (the chosen one \`.active\`), \`.langswitch\`, \`.pushtoggle\`, or a link. The bar's \`.settingsbtn\` opens it with \`popovertarget\`.

- Do ship the buttons with \`hidden\`; the client script reveals them, and a row with nothing visible disappears, so readers without JavaScript never see dead controls.
- Do write each preference to \`localStorage\` and to a \`data-*\` attribute on \`<html>\` (\`data-theme\`, \`data-fontsize\`, \`data-density\`); styles key off the attribute.
- Don't give the bare \`.settingspanel\` a display: a closed popover must stay hidden by the UA.
- Don't add a setting whose effect is not visible on the page that is open.

The preview shows the open popover. Source: \`hrefs.js\`, \`client.js\`; styles \`.settingspanel\`, \`.settingsrow\`, \`.miniseg\`, \`.minisegbtn\`.
`,
    "Shown as the open popover.",
  );

  add(
    "HeroLead",
    "Hero",
    440,
    stage(`<section class="hero">
  <a class="entry entry-lead herolink" href="#">
    <span class="herochip"><span class="livedot" aria-hidden="true"></span><span class="eyebrow-text">Latest · Tue 25 Aug · 16:00 CEST · 12 items</span><span class="flag flag-degraded" title="polymarket">⚠ partial</span></span>
    <h1 class="herotitle headline-lead">Markets slid for a second day while the EU package landed.</h1>
    <p class="herosub excerpt">Equities extended losses into the close; two sections follow on the council vote and chip export rules.</p>
  </a>
  <div class="heropills"><a class="herocta" href="#">Read the edition →</a><a class="threadpill" href="#">Markets slide continues</a><a class="threadpill" href="#">EU agrees new package</a></div>
</section>`),
    `# HeroLead

The newest briefing at the top of the index, centred and large: a chip with a live dot and the dateline, the headline, a subline, a primary CTA and the threads it belongs to.

**Provide** \`section.hero\` with an \`a.entry.entry-lead.herolink\` linking to the digest (containing \`span.herochip\` with \`.livedot\`, \`.eyebrow-text\` and optional \`.flag\`s, an \`h1.herotitle.headline-lead\`, and \`p.herosub.excerpt\`), then \`.heropills\` holding \`a.herocta\` and one \`a.threadpill\` per thread.

- Do use exactly one per index page, above the ledger; it is the page's h1.
- Do let the live dot's ping run only under no-preference motion.
- Don't wrap it in a card; the hero sheds the entry's card surface.

Source: \`render-index.js\`; styles \`.hero\`, \`.herotitle\`, \`.herochip\`, \`.herocta\`, \`.threadpill\`.
`,
  );

  add(
    "DigestHero",
    "Hero",
    330,
    stage(`<nav class="digestnav"><a href="#">← All digests</a><span class="spacer"></span><a class="nav-older" href="#">← 10:00</a></nav>
<header class="hero hero-digest">
  <span class="herochip">Digest · Tue 25 Aug · 16:00 CEST · 12 items · 3 sections</span>
  <h1 class="herotitle">Markets slid for a second day while the EU package landed.</h1>
</header>`),
    `# DigestHero

The top of a digest page has back and older/newer links, a centred metadata chip (kind, date, time, counts), and a headline.

**Provide** a \`nav.digestnav\` (back link, \`.spacer\`, \`.nav-older\` / \`.nav-newer\`) and \`header.hero.hero-digest\` with \`span.herochip\`, \`h1.herotitle\`. The headline is derived from the TL;DR.

- Do repeat \`nav.digestnav.digestnav-bottom\` after the colophon.
- Do omit the older or newer link at either end instead of showing a disabled one.

Source: \`render-digest.js\`; styles \`.digestnav\`, \`.hero-digest\`, \`.herochip\`.
`,
  );

  add(
    "Entry",
    "Ledger",
    430,
    stage(`<section class="ledger">
  <div class="dayhead">Tuesday, 25 August 2026</div>
  <a class="entry" href="#">
    <span class="meta"><span class="time">10:00</span><span class="count">9 items · 2 sections</span></span>
    <h2 class="headline">The sell-off began; two sections follow.</h2>
    <p class="excerpt"><strong>TL;DR:</strong> Equities extended losses into the close, with the drawdown at four percent and two sections on the council vote and chip export rules.</p>
  </a>
  <div class="dayhead">Monday, 24 August 2026</div>
  <a class="entry" href="#">
    <span class="meta"><span class="time time-accent">22:00</span><span class="count">6 items · 1 sections</span><span class="flag flag-daily">daily brief</span></span>
    <h2 class="headline">Daily brief for Monday.</h2>
    <p class="excerpt excerpt-daily"><strong>TL;DR:</strong> A quieter day: the council vote slipped to Tuesday and exporters have ninety days to file.</p>
  </a>
</section>`),
    `# Entry

One briefing in the ledger, a glass card: a time and count line, a headline, a clamped excerpt.

**Provide** an \`a.entry\` (the whole card is the link) with a \`span.meta\` (\`.time\`, \`.count\`, optional \`.flag\`s), an \`h2.headline\` and a \`p.excerpt\`, inside \`section.ledger\`, grouped under a \`.dayhead\` per local day (Europe/Budapest).

- Do add \`.time-accent\` to the latest edition of a day and \`.flag-daily\` + \`.excerpt-daily\` to a daily or weekly brief; the amber tint follows from those.
- Do let \`data-density="compact"\` dissolve the cards into a hairline list: two-line excerpts, the time never hidden.
- Don't nest a card inside a card.

The same card, with search hits wrapped in \`mark\`, is the search result; a thread's appearance is the same row inside a \`.tlitem\`.

Source: \`render-index.js\`, \`render-search.js\`, \`render-arc.js\`; styles \`.entry\`, \`.ledger\`, \`.dayhead\`.
`,
  );

  add(
    "Flag",
    "Ledger",
    90,
    stage(
      `<div class="entry" style="padding:12px 16px"><span class="meta"><span class="flag flag-daily">daily brief</span><span class="flag flag-daily">weekly brief</span><span class="flag flag-degraded" title="polymarket">⚠ partial</span><span class="flag flag-muted">EN</span></span></div>`,
    ),
    `# Flag

A one-line pill on an entry's meta line (or in the hero chip) that says what kind of briefing it is or what went wrong collecting it.

**Provide** a \`span.flag\` with exactly one modifier:
- \`.flag-daily\`: "daily brief" or "weekly brief", on the roll-up editions.
- \`.flag-degraded\`: "⚠ partial" when a collector failed; the failed sources go in \`title\`.
- \`.flag-muted\`: "EN" on a Hungarian page when the entry exists only in English.

- Do keep the text to one or two words; prefix a failure with ⚠ so colour is never the only signal.
- Don't use a flag as a category tag; sections and threads carry topics.

Source: \`render-index.js\`; styles \`.flag\`.
`,
  );

  add(
    "DayHead",
    "Ledger",
    210,
    stage(`<section class="ledger">
  <div class="dayhead">Tuesday, 25 August 2026</div>
  <a class="entry" href="#"><span class="meta"><span class="time">16:00</span></span><h2 class="headline">Markets slid for a second day.</h2></a>
  <div class="unreadfence" role="separator"><span class="line"></span><span class="label">new since your last visit</span><span class="line"></span></div>
  <div class="dayhead">Monday, 24 August 2026</div>
</section>`),
    `# DayHead

The ledger's dividers: a \`muted\` display-face day header per local day, and the "new since your last visit" fence the client inserts after the last unread entry.

**Provide** \`div.dayhead\` with the full local date. The fence (\`.unreadfence\` with two \`.line\`s around a \`.label\`) is built by \`client.js\` from the ledger's \`data-unread-label\`; never render it on the server.

- Do keep the fence in \`accent-text\` on an \`accent-line\` rule: it is a state, not a heading.
- Don't show the fence when everything, or nothing, is new.

Source: \`render-index.js\`, \`client.js\`; styles \`.dayhead\`, \`.unreadfence\`.
`,
  );

  add(
    "SideCard",
    "Ledger",
    360,
    stage(`<aside class="side">
  <section class="sidecard now"><h2 class="sidehead">Now</h2><nav class="nowlist">
    <a class="nowrow" href="#"><span class="nowarrow" aria-hidden="true">↑</span><span class="nowarclabel">Markets slide continues</span><span class="nowmeta">4 hours ago</span></a>
    <a class="nowrow" href="#"><span class="nowarrow" aria-hidden="true">↑</span><span class="nowarclabel">EU agrees new package</span><span class="nowmeta">yesterday</span></a>
  </nav></section>
  <section class="sidecard earlier"><h2 class="sidehead">Earlier</h2>
    <a class="earlierrow" href="#"><span class="earliermeta">10:00 · 9 items</span><span class="earliertitle">The sell-off began; two sections follow.</span></a>
    <a class="earlierrow" href="#"><span class="earliermeta">06:00 · 7 items</span><span class="earliertitle">Overnight: Asian markets open lower.</span></a>
  </section>
</aside>`),
    `# SideCard

The sidebar's cards, shown beside the main column from 1040px and stacked below it on narrower screens: "Now" (threads that moved recently) on the index, "Earlier" (the day's other editions) on a digest.

**Provide** \`aside.side\` of \`section.sidecard\`s, each with an \`h2.sidehead\`. A Now card holds \`nav.nowlist\` of \`a.nowrow\`s (\`.nowarrow\`, \`.nowarclabel\`, \`.nowmeta\`); an Earlier card holds \`a.earlierrow\`s (\`.earliermeta\`, \`.earliertitle\`, clamped to two lines). The settings panel is the last child of \`.side\`.

- Do keep Now to the few threads with fresh coverage; it is a pointer, not a second ledger.
- Do give the card its own top margin; an empty sidebar then adds no height on tablets.

Source: \`render-index.js\`, \`render-digest.js\`; styles \`.sidecard\`, \`.nowrow\`, \`.earlierrow\`.
`,
  );

  add(
    "CatchUp",
    "Ledger",
    150,
    stage(`<div class="catchup"><span class="catchuptext">Since your last visit: <a href="#">3 briefings</a> · <a href="#">2 arc updates</a></span><button type="button" class="catchupjump" aria-label="Jump to where you left off">↓</button><button type="button" class="catchupdismiss" aria-label="Dismiss">×</button></div>
<div style="position:relative;height:64px"><button type="button" class="resumechip" style="position:absolute;bottom:0;left:50%">↓ new since your last visit</button></div>`),
    `# CatchUp

The returning-reader strip, an \`accent-soft\` pill summarising what arrived since the last visit with jump and dismiss, plus the floating "↓ new since your last visit" resume chip.

**Provide** \`div.catchup\` with its \`data-prefix\` and \`data-tmpl-*\` strings, rendered \`hidden\`; \`client.js\` fills \`.catchuptext\` and shows it. The \`.resumechip\` is created by the client on \`document.body\` and is visible only while the unread fence is below the viewport.

- Do render it hidden on the server; it depends on the reader's local history.
- Don't let the chip sit under the phone dock; it offsets by \`dock-h\` there.

Source: \`render-index.js\`, \`client.js\`; styles \`.catchup\`, \`.resumechip\`.
`,
  );

  add(
    "WeekRail",
    "Ledger",
    130,
    stage(
      `<nav class="weekrail" aria-label="Week navigation"><span class="rail-older"><a href="#">← W34</a></span><span class="rail-center">Week 35 · 24–30 Aug<span class="railspark" aria-hidden="true"><i style="height:40%"></i><i style="height:100%"></i><i style="height:65%"></i><i class="sd0"></i><i style="height:15%"></i><i class="sd0"></i><i class="sd0"></i></span></span><span class="rail-newer"><a href="#">W36 →</a></span></nav>`,
    ),
    `# WeekRail

Week-by-week navigation under the weekly ledger: older and newer week pills either side of a centred week label with a seven-bar sparkline of items per day.

**Provide** \`nav.weekrail\` with \`.rail-older\`, \`.rail-center\` and \`.rail-newer\`; the sparkline is a \`span.railspark\` of seven \`i\` bars, each \`height\` a percentage of the week's busiest day (minimum 15%), zero days as \`i.sd0\`.

- Do omit the sparkline for an empty week.
- Leave \`.rail-newer\` empty, not missing, on the current week so the label stays centred.

Source: \`render-index.js\`; styles \`.weekrail\`, \`.railspark\`.
`,
  );

  add(
    "Callouts",
    "Article",
    330,
    stage(`<article class="digest">
  <div class="tldr"><span class="tldr-label">TL;DR</span>Markets slid for a second day while the EU package landed.</div>
  <div class="attention"><span class="attention-label">⚑ Needs attention</span><p>The council vote on the package closes at 18:00 CEST; two of your groups are organising around it.</p></div>
  <p class="banner">⚠ Polymarket could not be collected this run; its items are missing.</p>
</article>`),
    `# Callouts

The digest's three blocks above the sections: the TL;DR card (a \`panel\` with an \`accent-line\` border), the attention callout and the collection-failed banner (both amber).

**Provide** them inside \`article.digest\`: \`div.tldr\` first with a \`.tldr-label\`, an optional \`div.attention\` with \`.attention-label\` and a \`p\`, and \`p.banner\` when a source failed. These arrive in the summarizer's HTML; the site strips its inline email styles and these classes take over.

- Do keep exactly one TL;DR per digest; it is also the headline's source.
- Do reserve \`attention\` for something the reader may act on.
- Don't add a left border or icon; the label carries the meaning.

Source: \`render-digest.js\`, \`sections.js\`; styles \`.tldr\`, \`.attention\`, \`.banner\`.
`,
  );

  add(
    "Toc",
    "Article",
    170,
    stage(
      `<div class="tocrow"><details class="tocpicker" open><summary>Jump to section · 3</summary><nav class="toc" aria-label="Sections"><a href="#s1"><span class="tocnum" aria-hidden="true">01</span><span class="toctitle">Markets slide continues</span></a><a href="#s2"><span class="tocnum" aria-hidden="true">02</span><span class="toctitle">EU agrees new package</span></a><a href="#s3"><span class="tocnum" aria-hidden="true">03</span><span class="toctitle">Chip export rules tighten</span></a></nav></details><button type="button" class="expandall">Expand all</button></div>`,
    ),
    `# Toc

The section index uses a native disclosure with full section titles and an Expand all / Collapse all button. It precedes the complete summary so it remains accessible even when the summary is long.

**Provide** \`div.tocrow\` holding \`details.tocpicker\`, its summary and \`nav.toc\` with numbered \`#sN\` links. The sibling \`button.expandall\` carries \`data-expand\` / \`data-collapse\` labels and is rendered \`hidden\`; the client reveals it.

- Do generate it from the sections; never write it by hand.
- Don't include the attention callout's heading.

Source: \`render-digest.js\`, \`client.js\`; styles \`.tocrow\`, \`.toc\`, \`.tocnum\`, \`.toctitle\`, \`.expandall\`.
`,
  );

  add(
    "Section",
    "Article",
    420,
    stage(`<article class="digest">
  <details class="sec" open><summary class="sechead"><h2 id="s1">Markets slide continues</h2><span class="seccount">2 sources</span></summary><div class="secbody">
    <a class="secarc" href="#">story so far ×2</a>
    <p>Equities extended losses. <a href="#">Reuters</a> puts the drawdown at <strong>four percent</strong>.<a class="cite" href="#" title="reuters.com">1</a></p>
    <div class="storysources"><span>Sources</span><ul role="list" aria-label="Sources"><li><a href="#"><span class="sourcenum">¹</span> <span>reuters.com</span></a></li></ul></div>
  </div></details>
  <details class="sec"><summary class="sechead"><h2 id="s2">EU agrees new package</h2><span class="secpreview">The council signed off overnight, with disbursement gated on reforms.</span><span class="seccount">1 source</span></summary><div class="secbody"><p>The council signed off overnight.</p></div></details>
</article>`),
    `# Section

One story in a digest, a native \`details\` card: a heading with a two-line preview and a source count when closed, a chevron, and the \`body\`-coloured article text when open. The first section ships open.

**Provide** \`details.sec\` containing \`summary.sechead\` (the \`h2\` with \`id="sN"\`, an optional \`span.secpreview\`, an optional \`span.seccount\`) and \`div.secbody\`. Inside the body: paragraphs, \`a.secarc\` for a thread the section belongs to, \`a.cite\` chips and a \`div.storysources\` list of destination hostnames.

- Do keep it a native \`details\`: it works without JavaScript, and print forces every section open.
- Do bold the figure that matters; \`strong\` snaps to \`strong\` ink against \`body\`.
- Don't number sections in the text; the TOC pills do.

Source: \`sections.js\`; styles \`.sec\`, \`.sechead\`, \`.secbody\`, \`.secarc\`.
`,
  );

  add(
    "Cite",
    "Article",
    90,
    stage(
      `<p style="font-size:17px;margin:0">Drawdown at four percent<a class="cite" href="#" title="reuters.com">1</a> and widening<a class="cite" href="#" title="ft.com">2</a>. Hits: <mark>Markets</mark> extended losses.</p>`,
    ),
    `# Cite

A superscript citation chip: an \`accent-soft\` rounded chip with the source number in \`accent-text\`, filling solid \`accent\` on hover or focus.

**Provide** \`a.cite\` linking to the source with the source domain in \`title\` (\`sections.js\` adds it); on touch screens CSS prints the domain after the number.

- Do place it directly after the claim, with no space.
- Don't use it for anything but sources; search hits use plain \`mark\`.

Source: \`sections.js\`; styles \`.cite\`, \`mark\`.
`,
  );

  add(
    "ThreadPills",
    "Article",
    250,
    stage(`<details class="storythreads" open><summary>Story threads · 2</summary><nav class="heropills" aria-label="Story threads"><a class="threadpill" href="#">Markets slide continues <span class="pillcount">×2 this week</span></a><a class="threadpill" href="#">Chip export rules <span class="pillcount">×3 this week</span></a></nav></details>
<div class="archivelabel" style="margin-top:24px">What changed</div>
<nav class="deltas" aria-label="What changed"><a class="delta" href="#"><span class="deltalabel">Markets slide continues</span><p class="deltatext"><span class="deltaprev">Down two percent</span><span class="deltaarrow">→</span><span class="deltanow">Down four percent</span></p></a></nav>`),
    `# ThreadPills

The related-reading controls after a digest article pair recurring story threads with a "What changed" list when a previous and current value are available.

**Provide** a \`details.storythreads\` disclosure containing \`nav.heropills\` of \`a.threadpill\` (title plus \`span.pillcount\`) and, when a thread's key fact moved, \`nav.deltas\` of \`a.delta\` with \`.deltalabel\` and a \`p.deltatext\` (\`.deltaprev\`, \`.deltaarrow\`, \`.deltanow\`). On the index the same pills sit in the hero.

- Do show a delta only when both values are known.
- Don't colour deltas red or green; the previous value is \`muted\`, the current one \`strong\`.

Source: \`render-digest.js\`, \`render-index.js\`; styles \`.threadpill\`, \`.deltas\`.
`,
  );

  add(
    "ArcHead",
    "Hero",
    420,
    stage(`<nav class="digestnav"><a href="#">← All digests</a></nav>
<header class="hero archead"><span class="herochip">2 appearances · first seen Tue 25 Aug · updated 4 hours ago</span><h1 class="herotitle arctitle">Markets slide continues</h1><p class="herosub">A sell-off that began on the 24th; central banks have not reacted yet.</p></header>
<div style="text-align:center"><button type="button" class="followtoggle" aria-pressed="false">☆ Follow</button></div>
<details class="arccontext" open style="margin-top:16px"><summary>Background</summary><div class="arccontextbody"><p>The sell-off began on the 24th after weak export data; central banks have not reacted yet.</p></div></details>`),
    `# ArcHead

The head of a story-thread page: a centred hero with a chip of appearances and freshness, the thread title, a subline, a Follow toggle, and a collapsible Background.

**Provide** \`header.hero.archead\` with \`span.herochip\`, \`h1.herotitle.arctitle\` (carrying \`data-arc-slug\`, \`data-follow-add\` and \`data-follow-remove\`; the client adds the \`.followtoggle\` button) and \`p.herosub\`, then \`details.arccontext\`.

- Do keep Follow a pill that turns \`accent-soft\` when pressed (☆ Follow / ★ Following).

Source: \`render-arc.js\`, \`client.js\`; styles \`.archead\`, \`.followtoggle\`, \`.arccontext\`.
`,
  );

  add(
    "Timeline",
    "Article",
    360,
    stage(`<div class="archivelabel">Timeline</div>
<ol class="timeline">
  <li class="tlitem tlitem-latest"><div class="dayhead">Tuesday, 25 August 2026</div><a class="entry" href="#"><span class="meta"><span class="time">16:00</span></span><p class="excerpt">Markets slid for a second day.</p></a></li>
  <li class="tlitem"><div class="dayhead">Monday, 24 August 2026</div><a class="entry" href="#"><span class="meta"><span class="time">22:00</span></span><p class="excerpt">The sell-off began.</p></a></li>
</ol>`),
    `# Timeline

A thread's appearances, newest first: a vertical rail in \`accent-line\`, one glass card per appearance, a dot on the rail, the newest dot filled and haloed.

**Provide** \`ol.timeline\` of \`li.tlitem\` (add \`.tlitem-latest\` to the first), each with an optional \`.dayhead\` and an \`a.entry\` that sheds its own card surface inside the item.

- Do show the day header only once per day.
- Don't put anything but appearances on the rail.

Source: \`render-arc.js\`; styles \`.timeline\`, \`.tlitem\`.
`,
  );

  add(
    "Colophon",
    "Article",
    230,
    stage(
      `<details class="colophon" open><summary>How this was made</summary><div class="colophonbody"><div class="sourcekey"><span class="sklabel">Sources</span><span class="sk"><i style="background:#4f8fd9"></i>telegram 5</span><span class="sk"><i style="background:#8a8f9e"></i>x 4</span><span class="sk"><i style="background:#9aa0ab"></i>rss 3</span><span class="sk sk-failed">⚠ polymarket</span></div><div class="provenance"><div class="provline"><span class="sklabel">Written by</span><span class="sk" title="claude-opus-5">Opus 5 · high</span></div><div class="provline"><span class="sklabel">Translated with</span><span class="sk sk-fallback">↻ GPT-5.6 Terra · high</span></div></div></div></details>`,
    ),
    `# Colophon

The collapsible "How this was made" footer of a digest: per-source item counts with colour keys, failed sources, and which model wrote and translated it.

**Provide** \`details.colophon\` with a \`.sourcekey\` (\`.sklabel\`, then \`.sk\` per source with an \`i\` swatch; \`.sk-failed\` for a source that failed) and a \`.provenance\` of \`.provline\`s; \`.sk-fallback\` with ↻ marks a fallback model.

- Do keep it collapsed by default and quiet; it is for the curious, not the reader.
- The source swatch colours are per-source literals in \`render-index.js\`, not tokens.

Source: \`render-digest.js\`, \`render-index.js\`; styles \`.colophon\`, \`.sourcekey\`, \`.provenance\`.
`,
  );

  add(
    "Search",
    "Controls",
    330,
    stage(`<form class="searchform" onsubmit="return false"><input class="filter" type="search" value="markets" placeholder="Search all briefings…" aria-label="Search all briefings…"><button class="searchbtn" type="submit">Search</button></form>
<p class="empty">2 results</p>
<a class="entry" href="#"><span class="meta"><span class="time">Tue 25 Aug 16:00</span></span><p class="excerpt"><mark>Markets</mark> extended losses into the close…</p></a>`),
    `# Search

The full-text search page: a pill input beside a filled Search button, then entry cards with hits highlighted.

**Provide** \`form.searchform\` (GET, \`q\`) with \`input.filter\` and \`button.searchbtn\`, a \`p.empty\` result count, and one \`a.entry\` per hit with matches wrapped in \`mark\`. The bar's search popover holds the quick in-page filter and links here.

- Do keep the input 16px: smaller makes iOS zoom the page on focus.

Source: \`render-search.js\`, \`hrefs.js\`; styles \`.searchform\`, \`.searchbtn\`, \`mark\`.
`,
  );

  add(
    "CommandPalette",
    "Controls",
    360,
    `<div class="cmdpalette-backdrop" style="position:absolute;padding-top:24px"><div class="cmdpalette" role="dialog" aria-label="Command palette"><input class="cmdpalette-input" type="text" value="da" placeholder="Type a command or search…" aria-label="Type a command or search…"><div class="cmdpalette-list" role="listbox"><div class="cmdpalette-item active" role="option" aria-selected="true"><span>Daily view</span><span class="cmdpalette-group">View</span></div><div class="cmdpalette-item" role="option"><span>Latest briefing</span><span class="cmdpalette-group">Go</span></div><div class="cmdpalette-item" role="option"><span>Markets slide continues</span><span class="cmdpalette-group">Thread</span></div></div></div></div>
<div style="height:320px"></div>`,
    `# CommandPalette

A keyboard-first command palette (⌘K / Ctrl+K, or /) over a \`scrim\`: a blurred glass sheet with a text input and a filtered list of views, destinations and threads.

**Provide** nothing on the server except \`span.paletteconfig\` carrying the localized labels as \`data-*\` attributes; \`client.js\` builds \`.cmdpalette-backdrop\` > \`.cmdpalette\` with \`.cmdpalette-input\`, \`.cmdpalette-list\` and \`.cmdpalette-item\`s (\`.active\` on the highlighted row, \`.cmdpalette-group\` for its kind).

- Do add new destinations here rather than to the bar.
- Do close on Escape and on a backdrop click.

Source: \`client.js\`; styles \`.cmdpalette*\`.
`,
  );

  // Cover
  put(
    "components/Cover/preview.html",
    `<!-- @dsCard height=300 -->
<style>
  html, body { margin: 0; padding: 0; background: var(--bg); }
  .cv { position: relative; width: 960px; height: 300px; overflow: hidden; background: radial-gradient(ellipse 620px 320px at 50% 0, var(--glow), transparent 70%), radial-gradient(var(--dot) 1px, transparent 1px) 0 0 / 22px 22px, var(--bg); }
  .card { position: absolute; border: 1px solid var(--border); border-radius: var(--r-card); background: var(--panel); }
  .pillbar { position: absolute; left: 40px; top: 32px; display: flex; align-items: center; gap: 14px; height: 48px; padding: 0 8px 0 22px; border: 1px solid var(--border-strong); border-radius: var(--r-pill); background: var(--sheet); box-shadow: var(--shadow); }
  .pillbar b { font-family: var(--font-display); font-size: 20px; color: var(--strong); letter-spacing: -0.02em; }
  .pillbar b i { color: var(--accent); font-style: normal; }
  .tab { padding: 8px 14px; border-radius: var(--r-pill); font-size: 13px; color: var(--muted); }
  .tab.on { background: var(--pill); color: var(--strong); font-weight: 600; box-shadow: 0 0 0 1px var(--border); }
  .dot { width: 36px; height: 36px; border-radius: 50%; background: var(--accent); }
  .name { position: absolute; left: 40px; bottom: 74px; max-width: 520px; margin: 0; font-family: var(--font-display); font-weight: 600; font-size: 84px; line-height: 0.95; letter-spacing: -0.035em; color: var(--strong); }
  .tag { position: absolute; left: 42px; bottom: 40px; margin: 0; font-size: 15px; color: var(--muted); }
</style>
<div class="cv">
  <!--
    blocks: three glass panels (a tall entry card, a short daily card in the amber trio, a thread pill) and a floating pill bar with three tabs and an accent dot.
    arrangement: the product in miniature: the bar floats top left over the glow, the cards stack on the right like a ledger, the name sits bottom left.
    pattern: the dot grid and the top glow of the page background, drawn with the real tokens.
    scales: r-card on the panels, r-pill on every control, one shadow token on the bar only.
  -->
  <div class="pillbar"><b>news<i>.</i></b><span class="tab on">All</span><span class="tab">Daily</span><span class="tab">Weekly</span><span class="dot"></span></div>
  <div class="card" style="left:560px;top:28px;width:360px;height:118px"></div>
  <div class="card" style="left:560px;top:160px;width:360px;height:96px;background:var(--daily-soft);border-color:var(--daily-line)"></div>
  <div class="card" style="left:500px;top:112px;width:150px;height:40px;border-radius:999px;background:var(--accent-soft);border-color:var(--accent-line)"></div>
  <h1 class="name">Digest News</h1>
  <p class="tag">Briefings every six hours, on glass</p>
</div>
`,
  );

  for (const c of C) {
    const sub = c.subtitle ? ` subtitle="${c.subtitle}"` : "";
    put(
      `components/${c.name}/preview.html`,
      `<!-- @dsCard group="${c.group}" height=${c.height}${sub} -->\n${c.markup}\n`,
    );
    put(`components/${c.name}/README.md`, c.readme);
  }

  // Index
  const stamp = now.toISOString().replace(/\.\d+Z$/, "Z");
  put(
    "design-system.json",
    JSON.stringify(
      {
        v: 3,
        layout: "files",
        createdOnFiles: { v: 1, at: stamp },
        title: "Digest News",
        namespace: "DigestNews",
        libraries: [],
        sections: {},
        groups: ["Logos", "Icons"],
        assetGroups: {
          Logos: { name: "Logos", tile: "m", order: [], files: {} },
          Icons: { name: "Icons", tile: "s", order: [], files: {} },
        },
        blobs: {},
        docs: { sections: [] },
        lastChange: {
          by,
          at: stamp,
          via: `GitHub · Dezoxy/notification-digest@${sha}`,
          note: `Generated from workers/news-site: tokens, type, radii, ${C.length} components, logos.`,
        },
      },
      null,
      2,
    ) + "\n",
  );

  return { files, audit, components: C.map((c) => c.name) };
}

// ── CLI ─────────────────────────────────────────────────────────────────────
function gitOr(args) {
  try {
    return (
      execFileSync("git", args, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim() ||
      "unknown"
    );
  } catch {
    return "unknown";
  }
}

async function main() {
  const here = dirname(fileURLToPath(import.meta.url));
  const imp = (f) => import(pathToFileURL(join(here, "..", "src", f)).href);
  const { CSS } = await imp("css.js");
  const { FAVICON_SVG } = await imp("chrome.js");
  const icons = await imp("icons.js");

  const out = process.argv[2] ?? join(tmpdir(), "digest-news-design-system");
  const { files, audit, components } = buildDesignSystem({
    css: CSS,
    faviconSvg: FAVICON_SVG,
    icons,
    sha: gitOr(["rev-parse", "--short", "HEAD"]),
    by: gitOr(["config", "user.name"]),
    now: new Date(),
  });

  const project = join(out, "project");
  rmSync(project, { recursive: true, force: true });
  for (const [rel, data] of files) {
    const f = join(project, rel);
    mkdirSync(dirname(f), { recursive: true });
    writeFileSync(f, data);
  }

  console.log(`wrote ${files.size} files to ${project}\n`);
  for (const [pair, [l, d]] of Object.entries(audit)) {
    console.log(
      `${pair.padEnd(40)} light ${l.toFixed(2).padStart(5)}  dark ${d.toFixed(2).padStart(5)}`,
    );
  }
  console.log("\ncomponents:", components.join(", "));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
