#!/usr/bin/env node
// Generates the file set for the "Digest News" design-system artifact on
// claude.ai from this Worker's own source, so the artifact's palette, type,
// radii, logos and component cards can be re-synced whenever the site's look
// changes instead of being retyped by hand.
//
// WHY it is a script and not a one-off: the first import was done with a
// scratch script that hard-coded the palette. A hard-coded copy drifts the
// first time someone edits src/css.js, and the artifact then documents a site
// that no longer exists. So the palette is PARSED out of the stylesheet (the
// first `:root {` block for light, `:root[data-theme="dark"] {` for dark),
// and every custom property found there must be classified and documented
// below. A new `--something` in css.js with no usage note here makes
// buildDesignSystem() throw — and test/design-system.test.mjs runs it against
// the real stylesheet, so `npm test` fails until the note is written. That is
// the point: nobody adds a colour without deciding how it is described.
//
// What is NOT drift-checked: the type styles, the non-FAB spacing tokens, the
// radius tokens, the two literal shadows (float-shadow, palette-shadow) and
// every component card's markup. They mirror values and class names in
// css.js and the render-*.js files and are curated by hand. Edit them here
// when the site changes; nothing will tell you if you forget.
//
// What it produces, under <out-dir>/project/ (paths as the artifact expects):
//   README.md, tokens.json, design-system.json
//   components/bundle.css          the site's stylesheet minus its palette
//   components/<Name>/preview.html + README.md   (one per component)
//   components/Cover/preview.html
//   assets/Logos/*, assets/Icons/*  (favicon SVG + the three PWA PNGs)
// It does NOT write tokens.css: the artifact page generates that itself from
// tokens.json.
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

// ── Palette parsing ─────────────────────────────────────────────────────────
// The families below are the ones css.js does not define as custom properties
// (chrome is the body font-family; serif is the data-font="serif" override).
// The serif stack is cross-checked against the parsed override.
const CHROME_STACK = '-apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif';
const SERIF_STACK = 'ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif';

// Custom properties that are the same in both themes (defined once in :root).
const THEME_INVARIANT = ["fab-size", "fab-inset", "font-prose", "font-data"];

const FAB_USAGE = {
  "fab-size": "Back FAB diameter; the resume chip steers around it.",
  "fab-inset": "Back FAB inset from the viewport corner.",
};

// One usage note per hex colour custom property in css.js. chip-text and
// h2-border are aliases of accent (checked below). The faint note quotes
// measured ratios, so the map is built from the audit.
function colorUsage(r) {
  return {
    bg: "Paper. Page and card background; also the text colour on solid `accent` fills (active view tab, back FAB, hovered cite chip).",
    "page-bg": "The gutter the lifted desktop column sits on (viewport ≥ 52em). Never behind text.",
    text: "Ink. Headlines, brand, active controls and strong/em inside prose, on `bg`.",
    muted:
      "Secondary text on `bg` and `chip-bg`: counts, excerpts, settings labels, metadata, inactive segments.",
    accent:
      "Press blue. Links, eyebrows, section numbers, times on the newest entry, active view tab fill, focus rings. On `bg`, `tldr-bg` and `chip-bg`.",
    "accent-strong": "Hover and pressed state of `accent` text.",
    "tldr-bg": "The TL;DR leader block and the hover wash on inactive view tabs.",
    "tldr-text": "Text inside the TL;DR block, on `tldr-bg`.",
    "chip-bg": "Citation chips, story-thread chips, search-hit highlight and sparkline bars.",
    "chip-text": "Text on `chip-bg`; an alias of `accent`.",
    hairline:
      "1px separators: between entries, under nav, around panels and inputs. Decorative, not a control boundary.",
    "h2-border": "Section-heading rule colour; an alias of `accent`.",
    "attention-bg": "Attention callout, the collection-failed banner and entry flags.",
    "attention-text": "Text on `attention-bg`.",
    "rule-heavy":
      "The 4px masthead rule, the lead-entry rule and pill-control outlines (view tabs, segmented, gear, search button).",
    mark: "Highlighter behind a phrase inside a headline (`.headline mark`, `.edhead h1 mark`).",
    "mark-ink":
      "Text on `mark`. Identical in both themes on purpose: the highlighter is always a light chip.",
    faint: `One step quieter than \`muted\`: day headers, lead-fact labels, folios. Small uppercase mono only. Misses 4.5:1 on \`bg\` (${r("faint/bg", "light")}:1 light, ${r("faint/bg", "dark")}:1 dark); kept exact from the source.`,
    prose:
      "Article body paragraphs, one step under `text` so headlines and bold stats stand out. On `bg`.",
  };
}

const ALIASES = ["chip-text", "h2-border"];
const isHex = (v) => /^#[0-9a-fA-F]{3,8}$/.test(v);

const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");

// The text between `opener` and the first closing brace. css.js palette
// blocks contain no nested braces.
function blockBody(css, opener) {
  const at = css.indexOf(opener);
  if (at < 0) throw new Error(`design-system: block not found in css.js: ${opener}`);
  const from = at + opener.length;
  const to = css.indexOf("}", from);
  if (to < 0) throw new Error(`design-system: unterminated block in css.js: ${opener}`);
  return css.slice(from, to);
}

// Every `--name: value;` in a block body, in source order. `color-scheme` and
// other real properties do not match and are ignored.
function declarations(body) {
  const map = new Map();
  for (const [, name, value] of body.matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) {
    map.set(name, value.trim().replace(/\s+/g, " "));
  }
  return map;
}

function need(map, name, where) {
  if (!map.has(name))
    throw new Error(`design-system: css.js no longer defines --${name} (${where})`);
  return map.get(name);
}

function parsePalette(css, faviconSvg) {
  const clean = stripComments(css);
  const light = declarations(blockBody(clean, ":root {"));
  const dark = declarations(blockBody(clean, ':root[data-theme="dark"] {'));
  const serif = declarations(blockBody(clean, ':root[data-font="serif"] {'));

  // Light and dark must define the same theme-varying names.
  const lightVarying = [...light.keys()].filter((n) => !THEME_INVARIANT.includes(n));
  const darkNames = [...dark.keys()];
  const onlyLight = lightVarying.filter((n) => !dark.has(n));
  const onlyDark = darkNames.filter((n) => !light.has(n) || THEME_INVARIANT.includes(n));
  if (onlyLight.length || onlyDark.length) {
    throw new Error(
      `design-system: light and dark palettes differ — only in light: [${onlyLight.join(", ")}]; only in dark: [${onlyDark.join(", ")}]`,
    );
  }

  // Classify every light custom property.
  const names = [];
  for (const [name, value] of light) {
    if (THEME_INVARIANT.includes(name) || name === "card-shadow") continue;
    if (!isHex(value)) {
      throw new Error(
        `design-system: --${name} (${value}) is a custom property in css.js's palette block that is not a hex colour and has no classification; handle it in scripts/design-system.mjs`,
      );
    }
    names.push(name);
  }

  // Usage notes <-> CSS, both directions. The faint ratio is irrelevant here.
  const documented = Object.keys(colorUsage(() => "0"));
  const undocumented = names.filter((n) => !documented.includes(n));
  if (undocumented.length) {
    throw new Error(
      `design-system: css.js defines --${undocumented.join(", --")} with no usage note in scripts/design-system.mjs (colorUsage); document it`,
    );
  }
  const stale = documented.filter((n) => !names.includes(n));
  if (stale.length) {
    throw new Error(
      `design-system: usage note for --${stale.join(", --")} but css.js no longer defines it; remove the note`,
    );
  }

  for (const alias of ALIASES) {
    for (const [theme, map] of [
      ["light", light],
      ["dark", dark],
    ]) {
      if (map.get(alias) !== map.get("accent")) {
        throw new Error(
          `design-system: --${alias} (${map.get(alias)}) no longer equals --accent (${map.get("accent")}) in ${theme}; it can no longer be emitted as an alias`,
        );
      }
    }
  }

  const fontProse = need(light, "font-prose", ":root");
  const fontData = need(light, "font-data", ":root");
  if (need(serif, "font-prose", 'data-font="serif"') !== SERIF_STACK) {
    throw new Error("design-system: the data-font=serif stack in css.js differs from SERIF_STACK");
  }
  if (!faviconSvg.includes('fill="#4f46e5"')) {
    throw new Error("design-system: FAVICON_SVG no longer uses #4f46e5; update brand-mark");
  }

  return {
    names,
    light,
    dark,
    fontProse,
    fontData,
    cardShadow: {
      light: need(light, "card-shadow", ":root"),
      dark: need(dark, "card-shadow", 'data-theme="dark"'),
    },
    fab: ["fab-size", "fab-inset"].map((name) => ({ name, value: need(light, name, ":root") })),
  };
}

// ── Contrast audit ──────────────────────────────────────────────────────────
const lum = (hex) => {
  const c = hex
    .slice(1)
    .match(/../g)
    .map((h) => parseInt(h, 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
};
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m);
  return (x + 0.05) / (y + 0.05);
};
const PAIRS = [
  ["text", "bg"],
  ["prose", "bg"],
  ["muted", "bg"],
  ["faint", "bg"],
  ["accent", "bg"],
  ["accent-strong", "bg"],
  ["tldr-text", "tldr-bg"],
  ["accent", "tldr-bg"],
  ["accent", "chip-bg"],
  ["muted", "chip-bg"],
  ["attention-text", "attention-bg"],
  ["mark-ink", "mark"],
  ["bg", "accent"],
  ["text", "page-bg"],
  ["hairline", "bg"],
  ["rule-heavy", "bg"],
];

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
  const palette = parsePalette(css, faviconSvg);
  const L = palette.light;
  const D = palette.dark;

  const audit = {};
  for (const [fg, bg] of PAIRS) {
    for (const n of [fg, bg]) {
      if (!L.has(n)) throw new Error(`design-system: contrast pair names unknown colour --${n}`);
    }
    audit[`${fg}/${bg}`] = [ratio(L.get(fg), L.get(bg)), ratio(D.get(fg), D.get(bg))];
  }
  const r = (k, t) => audit[k][t === "light" ? 0 : 1].toFixed(1);

  // ── bundle.css: the site's real stylesheet minus the four palette blocks ──
  const start = css.indexOf("  :root {");
  const lightPin = css.indexOf(':root[data-theme="light"] {');
  const end = css.indexOf("\n  }\n", lightPin) + 5;
  if (start < 0 || lightPin < 0 || end < 5) throw new Error("palette blocks not found");
  const body = css.slice(0, start) + css.slice(end);
  if (/<\/style/i.test(body)) throw new Error("</style in CSS");
  put(
    "components/bundle.css",
    `/* Digest News — the news site's own stylesheet, lifted verbatim from
   workers/news-site/src/css.js @ ${sha}, minus its four palette blocks
   (:root, the prefers-color-scheme dark copy, and both data-theme pins).
   Those values live in tokens.json now and reach this file as tokens.css,
   so a palette edit on the page reaches every preview. */
:root { color-scheme: light; }
:root[data-theme="dark"] { color-scheme: dark; }
${body}
/* Preview stage for this design system's cards. Not part of the site. */
body:has(> .ds-stage) { padding: 16px; background: var(--page-bg); }
.ds-stage { background: var(--bg); color: var(--text); padding: 1.1em 1.5em; border-radius: 20px; box-shadow: var(--card-shadow); }
.ds-stage > :first-child { margin-top: 0; }
.ds-row { display: flex; flex-wrap: wrap; gap: 0.8em; align-items: center; }
.ds-note { font-family: var(--font-data); font-size: 0.66em; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); margin: 1.2em 0 0.4em; }
`,
  );

  // ── Assets (copied byte for byte from the worker's sources) ──
  put("assets/Logos/favicon.svg", faviconSvg);
  put("assets/Logos/icon-512.png", icons.ICON_512);
  put("assets/Logos/icon-maskable-512.png", icons.ICON_MASKABLE_512);
  put("assets/Logos/apple-touch-icon.png", icons.APPLE_TOUCH_ICON);
  put(
    "assets/Icons/search.svg",
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16"><circle cx="7" cy="7" r="4.5" fill="none" stroke="#101215" stroke-width="1.6"/><path d="M10.6 10.6 14 14" stroke="#101215" stroke-width="1.6" stroke-linecap="round"/></svg>\n`,
  );
  put(
    "assets/Logos/README.md",
    `# Logos

The mark is an indigo (\`brand-mark\`, #4f46e5) rounded tile carrying three white bars of falling length and opacity: a stack of headlines, longest first. It is a 64-unit grid: tile rx 14, bars 6 high with rx 3, at widths 36 / 28 / 20 and opacity 1 / 0.85 / 0.70.

- \`favicon.svg\` is the source geometry. The three PNGs are renders of it, and each differs only in framing:
  - \`icon-512.png\`: corners rounded, transparent outside. The manifest's \`purpose: "any"\` icon.
  - \`icon-maskable-512.png\`: full-bleed tile, art inset 10%. Android crops it to its own mask.
  - \`apple-touch-icon.png\`: 180×180, square, full bleed. iOS applies its own squircle.
- Use the mark for the tab, Home Screen and push notifications only. Inside the page the brand is the word **NEWS** set in the \`brand\` style; never place the tile in the masthead.
- Never recolor the tile to \`accent\`. Indigo is the app-icon colour and press blue is the page colour; they are deliberately different.
`,
  );
  put(
    "assets/Icons/README.md",
    `# Icons

The site carries one drawn icon: \`search.svg\`, a 16-unit magnifier stroked at 1.6. In the page it is inlined with \`stroke="currentColor"\` so it takes the toggle's text colour; this file is fixed to \`text\` ink (#101215) because \`<img>\` cannot inherit colour.

Every other glyph is a text character in the font stack, never an icon file: ⚙ settings, ↑ / ← back-to-top and back FAB, → delta arrow, ↓ jump, × dismiss, ⚠ degraded run, ↻ fallback model, ▤ density, ☆ / ★ follow. Keep it that way: no icon font, no emoji.
`,
  );

  // ── tokens.json ──
  const usage = colorUsage(r);
  const colorTokens = palette.names.map((name) =>
    ALIASES.includes(name)
      ? { name, value: { light: "{accent}", dark: "{accent}" }, usage: usage[name] }
      : { name, value: { light: L.get(name), dark: D.get(name) }, usage: usage[name] },
  );
  colorTokens.push({
    name: "brand-mark",
    value: "#4f46e5",
    usage:
      "App-icon indigo (favicon, Home Screen and notification icons). Not a page colour; never use it in the UI.",
  });

  // Type styles, the non-FAB spacing tokens, the radius tokens and the two
  // literal shadows below mirror values in css.js and are NOT drift-checked:
  // edit them here by hand when the site's look changes.
  const px = (em) => `${+(em * 17).toFixed(2)}px`;
  const tokens = {
    name: "Digest News",
    version: 1,
    meta: {
      source: "github",
      repo: "Dezoxy/notification-digest",
      ref: `main@${sha}`,
      package: "workers/news-site",
      paths: {
        tokens: ["workers/news-site/src/css.js"],
        fonts: [],
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
      families: {
        chrome: CHROME_STACK,
        prose: palette.fontProse,
        data: palette.fontData,
        serif: SERIF_STACK,
      },
      groups: [
        {
          name: "Display",
          family: "chrome",
          styles: [
            {
              name: "edition-title",
              fontSize: px(2.3),
              lineHeight: 1.08,
              fontWeight: 800,
              letterSpacing: "-0.03em",
              sample: "Markets slid for a second day while the EU package landed.",
              usage: "Digest page h1 (`.edhead h1`). clamp(1.6em, 5vw, 2.3em); max shown.",
            },
            {
              name: "brand-big",
              fontSize: px(2.1),
              lineHeight: 0.98,
              fontWeight: 800,
              letterSpacing: "-0.03em",
              sample: "NEWS",
              usage:
                "Masthead wordmark on the index (`.mast-big .brand`), uppercase. clamp(1.5em, 4.5vw, 2.1em).",
            },
            {
              name: "headline-lead",
              fontSize: px(2.1),
              lineHeight: 1.08,
              fontWeight: 800,
              letterSpacing: "-0.02em",
              sample: "Markets slid for a second day.",
              usage:
                "Newest briefing on the index (`.entry-lead .headline-lead`). clamp(1.5em, 4.2vw, 2.1em).",
            },
            {
              name: "arc-title",
              fontSize: px(1.7),
              lineHeight: 1.15,
              fontWeight: 800,
              letterSpacing: "-0.025em",
              sample: "Markets slide continues",
              usage: "Story-thread page h1 (`.arctitle`).",
            },
            {
              name: "section-heading",
              fontSize: px(1.3),
              lineHeight: 1.15,
              fontWeight: 800,
              letterSpacing: "-0.025em",
              sample: "EU agrees new package",
              usage: "Numbered digest section (`.digest > h2`), sticky while its section is read.",
            },
            {
              name: "headline",
              fontSize: px(1.17),
              lineHeight: 1.15,
              fontWeight: 800,
              letterSpacing: "-0.02em",
              sample: "The sell-off began; two sections follow.",
              usage: "Ledger entry headline (`.headline` on an h3).",
            },
            {
              name: "brand",
              fontSize: px(0.95),
              lineHeight: 1.6,
              fontWeight: 800,
              letterSpacing: "-0.02em",
              sample: "NEWS",
              usage: "Masthead wordmark on inner pages (`.mast .brand`), uppercase.",
            },
          ],
        },
        {
          name: "Reading",
          family: "prose",
          styles: [
            {
              name: "body",
              family: "chrome",
              fontSize: "17px",
              lineHeight: 1.6,
              fontWeight: 400,
              sample: "Settings, navigation and every chrome label sit on this base.",
              usage: "`body`. Steps to 18px at ≥ 75em and 19px at ≥ 100em; 15px on phones.",
            },
            {
              name: "tldr",
              fontSize: px(1.02),
              lineHeight: 1.65,
              fontWeight: 400,
              sample: "Markets slid for a second day while the EU package landed.",
              usage: "TL;DR leader (`.tldr`), on `tldr-bg`.",
            },
            {
              name: "prose",
              fontSize: "17px",
              lineHeight: 1.65,
              fontWeight: 400,
              sample: "Equities extended losses into the close, with the drawdown at four percent.",
              usage: "Digest paragraphs (`.digest p`) in `prose` colour. Hyphenated.",
            },
            {
              name: "excerpt",
              fontSize: px(0.88),
              lineHeight: 1.55,
              fontWeight: 400,
              sample: "TL;DR: The sell-off began; two sections follow.",
              usage: "Entry excerpt (`.entry .excerpt`) in `muted`, clamped to a few lines.",
            },
          ],
        },
        {
          name: "Data",
          family: "data",
          styles: [
            {
              name: "time",
              fontSize: px(0.85),
              lineHeight: 1.6,
              fontWeight: 700,
              sample: "16:00",
              usage:
                "Entry time (`.entry .time`), tabular. `accent` on the newest edition of a day.",
            },
            {
              name: "count",
              fontSize: px(0.75),
              lineHeight: 1.6,
              fontWeight: 400,
              sample: "9 items · 2 sections",
              usage: "Entry counts, colophon source key, follow toggle.",
            },
            {
              name: "eyebrow",
              fontSize: px(0.68),
              lineHeight: 1.6,
              fontWeight: 400,
              letterSpacing: "0.1em",
              sample: "LATEST · TUE 25 AUG · 16:00 CEST",
              usage:
                "Uppercase. Lead eyebrow, view tabs, digest nav, TL;DR label, flags. The wire dateline.",
            },
            {
              name: "dateline",
              fontSize: px(0.66),
              lineHeight: 1.6,
              fontWeight: 400,
              letterSpacing: "0.11em",
              sample: "NO. 235 · TUESDAY, 25 AUGUST 2026",
              usage: "Uppercase. The issue line above the masthead and the digest stamp.",
            },
            {
              name: "cite",
              fontSize: px(0.7),
              lineHeight: 1.6,
              fontWeight: 700,
              sample: "1",
              usage: "Citation chip (`.cite`), superscript, on `chip-bg`.",
            },
          ],
        },
      ],
    },
    spacing: {
      tokens: [
        {
          name: "rule-heavy-w",
          value: "4px",
          usage: "Masthead bottom rule thickness, in `rule-heavy`.",
        },
        {
          name: "gutter",
          value: "1.25em",
          usage: "Side padding of the reading column (`.wrap`) on phones and tablets.",
        },
        {
          name: "gutter-desktop",
          value: "3em",
          usage: "Side padding inside the lifted desktop column.",
        },
        {
          name: "entry-pad",
          value: "1.1em",
          usage: "Top padding of a ledger entry (1.3em below).",
        },
        {
          name: "section-gap",
          value: "2.2em",
          usage: "Space above each numbered digest section and the closing note.",
        },
        { name: "column", value: "42em", usage: "Reading column max width below 52em." },
        { name: "column-desktop", value: "61em", usage: "Lifted desktop column max width." },
        ...palette.fab.map(({ name, value }) => ({ name, value, usage: FAB_USAGE[name] })),
      ],
    },
    radius: {
      tokens: [
        {
          name: "radius-square",
          value: "0",
          usage: "Surfaces: entries, attention callout, rules, sections. The poster default.",
        },
        { name: "radius-chip", value: "3px", usage: "Citation chips only." },
        { name: "radius-input", value: "8px", usage: "Search inputs." },
        {
          name: "radius-panel",
          value: "14px",
          usage: "TL;DR block, settings and search popovers, command palette.",
        },
        { name: "radius-column", value: "20px", usage: "The lifted desktop column." },
        {
          name: "radius-pill",
          value: "999px",
          usage:
            "Every interactive control: view tabs, segmented, gear, search button, thread chips, flags, resume chip.",
        },
        { name: "radius-fab", value: "50%", usage: "Back-to-top FAB." },
      ],
    },
    shadow: {
      tokens: [
        { name: "card-shadow", value: palette.cardShadow, usage: "The lifted desktop column." },
        {
          name: "float-shadow",
          value: "0 4px 14px rgba(0, 0, 0, 0.28)",
          usage:
            "Popovers, the back FAB and the resume chip (a literal in css.js, same in both themes).",
        },
        {
          name: "palette-shadow",
          value: "0 12px 40px rgba(0, 0, 0, 0.35)",
          usage: "Command palette over its dimmed backdrop (a literal in css.js).",
        },
      ],
    },
  };
  put("tokens.json", JSON.stringify(tokens, null, 2) + "\n");

  // ── README (the brand book) ──
  put(
    "README.md",
    `Digest News is the reading surface of a personal notification digest: every six hours a briefing of the owner's Telegram groups, X notifications, RSS, Hacker News and Polymarket, summarized and published as a page. The look is a **print poster**: one heavy sans voice, a monospaced wire dateline for anything keyed on time, square surfaces, pill-shaped controls, press blue for links and a yellow highlighter for the phrase that matters. It runs in English and Hungarian, light and dark, on a phone first.

Load \`tokens.css\` then \`components/bundle.css\`: the bundle is the site's own stylesheet, so markup with the class names in each component card renders exactly as production does.

## Content fundamentals

- **Time is the primary key.** Every entry leads with its time; every page carries a dateline. Set times, counts, datelines, citations and eyebrows in the \`data\` family (mono, tabular) and everything a person reads at length in \`prose\`.
- **Headlines are sentences.** A briefing's headline is its TL;DR's first sentence, in sentence case, ending with a full stop: "Markets slid for a second day while the EU package landed." Never title case, never a teaser.
- **Labels are short and uppercase** through CSS (\`text-transform\`), written in normal case in the markup: "Latest · Tue 25 Aug · 16:00 CEST · 12 items". The middle dot \`·\` separates facts.
- **Plain, factual voice.** No exclamation marks, no "breaking", no emoji. State counts honestly ("6 items · 1 sections" is a known plural gap, not a style).
- **Every string has a Hungarian twin.** Copy lives in \`strings.js\`; leave room for Hungarian's longer compounds (prose is hyphenated with \`hyphens: auto\`; chrome is not).
- **Arrows and symbols are text.** ← → ↑ ↓ × ⚠ ↻ ☆ ★ come from the font. See Iconography.

## Visual foundations

**Colour.** Paper \`bg\`, ink \`text\`, press blue \`accent\`. Body copy in an article is \`prose\`, one step under ink, so headlines and \`strong\` figures (which snap back to \`text\`) stand out. Secondary metadata is \`muted\`; small uppercase folios are \`faint\`. The highlighter \`mark\` with \`mark-ink\` marks a phrase inside a headline; the search hit highlight is \`chip-bg\` + \`chip-text\` instead. \`attention-bg\` + \`attention-text\` carry every warning: the attention callout, the collection-failed banner, entry flags. Solid \`accent\` fills (active view tab, back FAB, hovered cite) take \`bg\` as their text colour.

**Contrast.** Measured pairs, light / dark: \`text\` on \`bg\` ${r("text/bg", "light")} / ${r("text/bg", "dark")}; \`prose\` ${r("prose/bg", "light")} / ${r("prose/bg", "dark")}; \`muted\` ${r("muted/bg", "light")} / ${r("muted/bg", "dark")}; \`accent\` ${r("accent/bg", "light")} / ${r("accent/bg", "dark")}; \`bg\` on \`accent\` ${r("bg/accent", "light")} / ${r("bg/accent", "dark")}; \`tldr-text\` on \`tldr-bg\` ${r("tldr-text/tldr-bg", "light")} / ${r("tldr-text/tldr-bg", "dark")}; \`attention-text\` on \`attention-bg\` ${r("attention-text/attention-bg", "light")} / ${r("attention-text/attention-bg", "dark")}; \`mark-ink\` on \`mark\` ${r("mark-ink/mark", "light")} / ${r("mark-ink/mark", "dark")}. One known miss, kept exact from the source: \`faint\` on \`bg\` is ${r("faint/bg", "light")} / ${r("faint/bg", "dark")}, so use it only for decorative folios and repeat anything important in \`muted\`.

**Type.** System faces only, zero bytes downloaded: \`chrome\` (the body stack) for UI, \`prose\` for reading, \`data\` (mono) for time and metadata. Readers can switch reading text to \`serif\` ("Classic") in Settings, which redefines \`--font-prose\` and nothing else. Display styles are weight 800 with negative tracking (-0.02 to -0.03em) and tight leading (0.98–1.15); never set a headline lighter than 750. Base size is 17px, 18px at ≥ 75em, 19px at ≥ 100em, 15px on phones; readers add S / M / L on top through \`data-fontsize\`.

**Layout.** One reading column: \`column\` (42em) with \`gutter\` side padding, centred. From 52em the column lifts off the \`page-bg\` gutter as a paper card (\`radius-column\`, \`card-shadow\`, up to \`column-desktop\` 61em). Ledger entries stack with \`hairline\` separators; there are no cards in the list. Sections inside a digest are separated by space (\`section-gap\`) and a hairline top rule, numbered 01, 02, … in \`accent\` mono by a CSS counter, and stay sticky while read.

**Shape.** Surfaces are square: entries, callouts, rules, sections. Every interactive control is a pill (\`radius-pill\`) outlined in \`rule-heavy\`: view tabs, segmented toggles, the gear, the search button. Chips are pills on \`chip-bg\` except citation chips (\`radius-chip\`, 3px). Floating panels (TL;DR, popovers, command palette) use \`radius-panel\`. Do not round an entry or a section.

**Rules and borders.** The masthead sits on a \`rule-heavy-w\` (4px) \`rule-heavy\` rule; the lead entry on a 1px \`rule-heavy\` rule; everything else on 1px \`hairline\`. No left-border accent bars anywhere.

**Shadows.** Only floating things cast one: \`float-shadow\` for popovers, the FAB and the resume chip; \`palette-shadow\` for the command palette; \`card-shadow\` for the lifted column. Entries and sections never.

**States.** Hover on a link or entry moves it to \`accent\` (\`accent-strong\` when it was already blue); inactive view tabs wash to \`tldr-bg\`. Focus is a solid 2px outline in \`accent\` (on entries, offset 4px) or \`text\` (on accent-filled controls). Disabled controls are hidden, not greyed.

**Motion.** Ambient only, and only under \`prefers-reduced-motion: no-preference\`: smooth anchor scrolling, the browser's default cross-page fade, a 120ms drop-in for the command palette, a fade for the back FAB. No scroll-triggered reveals.

**Themes.** Light, dark, or the OS preference, chosen in Settings and pinned through \`data-theme\` on \`<html>\`. Every colour reads from a token; set \`color-scheme\` with the theme so the canvas between pages never flashes white.

## Iconography

The mark is the indigo tile with three falling bars (\`assets/Logos\`, colour \`brand-mark\`), used for the tab, Home Screen and push notifications only. In the page the brand is the word NEWS in \`brand\` / \`brand-big\`. The one drawn icon is the search magnifier (\`assets/Icons/search.svg\`, inlined with \`currentColor\`). All other glyphs are Unicode characters in the current font: ⚙ ↑ ← → ↓ × ⚠ ⚑ ↻ ▤ ☆ ★. No icon font, no emoji.
`,
  );
  // ── Components ──
  const stage = (inner, extra = "") => `<main class="ds-stage"${extra}>\n${inner}\n</main>`;
  const C = [];
  const add = (name, group, height, markup, readme, subtitle) =>
    C.push({ name, group, height, markup, readme, subtitle });

  add(
    "Masthead",
    "Chrome",
    200,
    stage(`<div class="issueline">No. 235 · Tuesday, 25 August 2026 · 2 editions today</div>
<header class="mast mast-big">
  <div class="mastleft"><a class="brand" href="#">news</a></div>
  <nav class="viewtabs"><span class="viewtab active">All</span><a class="viewtab" href="#">Daily</a><a class="viewtab" href="#">Weekly</a></nav>
  <div class="mastright">
    <details class="searchpop"><summary class="searchtoggle" aria-label="Search"><svg class="searchicon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10.6 10.6 14 14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg><span class="searchlabel">Search</span></summary></details>
    <details class="settings"><summary class="gear" aria-label="Settings"><span class="gearicon" aria-hidden="true">⚙</span><span class="gearlabel">Settings</span></summary></details>
  </div>
</header>
<div class="ds-note">Inner pages: base masthead</div>
<header class="mast"><div class="mastleft"><a class="brand" href="#">news</a></div><div class="mastright"><details class="settings"><summary class="gear"><span class="gearicon" aria-hidden="true">⚙</span><span class="gearlabel">Settings</span></summary></details></div></header>`),
    `# Masthead

The top of every page: an optional mono issue line, the NEWS wordmark, the view tabs on the index, and the Search and Settings pills, sitting on the 4px \`rule-heavy\` rule.

**Use** \`header.mast\` on every page. Add \`.mast-big\` and a preceding \`.issueline\` on the index views (All, Daily, Weekly), which carry the issue number and date; inner pages (digest, arc, search, about) use the base masthead.

**Provide** the brand link (lowercase "news" in markup; CSS uppercases it), the \`.viewtabs\` nav when on an index view, and the two \`details\` popovers (\`.searchpop\`, \`.settings\`) in \`.mastright\`.

- Do keep the wordmark as text. The indigo tile is the app icon, not a page logo.
- Do keep Search and Settings as pills outlined in \`rule-heavy\`; on phones they collapse to bare icons by CSS alone.
- Don't add items to the masthead; new destinations go in the command palette.

Source: \`chrome.js\`, \`hrefs.js\`; styles \`header.mast\`, \`.issueline\`, \`summary.gear\`, \`summary.searchtoggle\`.
`,
  );

  add(
    "ViewTabs",
    "Controls",
    110,
    stage(`<div class="ds-row"><nav class="viewtabs"><span class="viewtab active">All</span><a class="viewtab" href="#">Daily</a><a class="viewtab" href="#">Weekly</a></nav>
<nav class="viewtabs"><a class="viewtab" href="#">All</a><span class="viewtab active">Daily</span><a class="viewtab" href="#">Weekly</a></nav></div>`),
    `# ViewTabs

A pill-shaped segmented nav that switches the index between All, Daily and Weekly.

**Provide** a \`nav.viewtabs\` with one \`.viewtab\` per view: the current one a \`span.viewtab.active\` (solid \`accent\`, \`bg\` text), the others links. Labels are mono uppercase by CSS.

- Do use it only in the masthead of index views.
- Do mark the current view with a \`span\`, not a link, so it is not focusable.
- Don't add a fourth view without checking the phone masthead still fits on one row.

Source: \`hrefs.js\`; styles \`.viewtabs\`, \`.viewtab\`.
`,
  );

  add(
    "SettingsPanel",
    "Controls",
    330,
    stage(`<div style="display:flex;justify-content:flex-end;align-items:flex-start;min-height:270px">
<details class="settings" open><summary class="gear" aria-label="Settings"><span class="gearicon" aria-hidden="true">⚙</span><span class="gearlabel">Settings</span></summary><div class="settingspanel">
<div class="settingsrow"><span class="settingslabel">Language</span><span class="langswitch"><strong>EN</strong> | <a href="#">HU</a></span></div>
<div class="settingsrow"><span class="settingslabel">Theme</span><span class="miniseg miniseg-theme" role="group" aria-label="Toggle light/dark"><button class="minisegbtn">Light</button><button class="minisegbtn active">Auto</button><button class="minisegbtn">Dark</button></span></div>
<div class="settingsrow"><span class="settingslabel">Text size</span><span class="miniseg" role="group" aria-label="Text size"><button class="minisegbtn">S</button><button class="minisegbtn active">M</button><button class="minisegbtn">L</button></span></div>
<div class="settingsrow"><span class="settingslabel">Body font</span><span class="miniseg" role="group" aria-label="Body font"><button class="minisegbtn active">Modern</button><button class="minisegbtn">Classic</button></span></div>
<div class="settingsrow"><span class="settingslabel">Density</span><button class="densitytoggle" aria-label="Toggle compact list">▤</button></div>
<div class="settingsrow"><a href="#">About</a></div>
</div></details></div>`),
    `# SettingsPanel

The Settings popover: language, theme, text size, reading font, density and notifications, each a labelled row with a segmented pill control.

**Provide** a \`details.settings\` whose \`summary.gear\` opens a \`.settingspanel\` of \`.settingsrow\`s. Each row is a \`.settingslabel\` plus a control: \`.miniseg\` with \`.minisegbtn\` buttons (the chosen one \`.active\`), \`.densitytoggle\`, \`.pushtoggle\`, or a plain link.

- Do ship the buttons with \`hidden\` in markup; the client script reveals them, so readers without JavaScript never see dead controls.
- Do write each preference to \`localStorage\` and to a \`data-*\` attribute on \`<html>\` (\`data-theme\`, \`data-fontsize\`, \`data-font\`, \`data-density\`); styles key off the attribute.
- Don't add a setting whose effect is not visible on the page that is open.

Source: \`hrefs.js\`, \`client.js\`; styles \`.settingspanel\`, \`.miniseg\`, \`.minisegbtn\`.
`,
  );

  add(
    "LeadEntry",
    "Ledger",
    230,
    stage(`<a class="entry entry-lead" href="#">
  <div class="leadmain">
    <span class="meta"><span class="eyebrow-text">Latest · Tue 25 Aug · 16:00 CEST · 12 items</span><span class="flag flag-degraded" title="polymarket">⚠ partial</span></span>
    <h2 class="headline headline-lead">Markets slid for a second day while the <mark>EU package</mark> landed.</h2>
    <p class="excerpt"><strong>TL;DR:</strong> Markets slid for a second day while the EU package landed.</p>
  </div>
  <dl class="leadfacts"><dt>items</dt><dd>12</dd><dt>sections</dt><dd>3</dd></dl>
</a>`),
    `# LeadEntry

The newest briefing at the top of the index, set large: an \`accent\` eyebrow dateline, a display headline, its TL;DR, and a facts column of counts.

**Provide** an \`a.entry.entry-lead\` linking to the digest, containing \`.leadmain\` (a \`.meta\` with \`.eyebrow-text\` and optional \`.flag\`, an \`h2.headline.headline-lead\`, a \`p.excerpt\`) and a \`dl.leadfacts\` of \`dt\`/\`dd\` pairs.

- Do use exactly one per index page, first in the list.
- Do highlight at most one phrase of the headline with \`mark\`.
- Don't wrap it in a card; it sits on a 1px \`rule-heavy\` rule like a newspaper lead.

Source: \`render-index.js\`; styles \`.entry-lead\`, \`.leadfacts\`.
`,
  );

  add(
    "Entry",
    "Ledger",
    330,
    stage(`<div class="dayhead">Tuesday, 25 August 2026</div>
<a class="entry" href="#">
  <span class="meta"><span class="time">10:00</span><span class="count">9 items · 2 sections</span></span>
  <h3 class="headline">The sell-off began; two sections follow.</h3>
  <p class="excerpt"><strong>TL;DR:</strong> The sell-off began; two sections follow.</p>
</a>
<div class="dayhead">Monday, 24 August 2026</div>
<a class="entry" href="#">
  <span class="meta"><span class="time time-accent">22:00</span><span class="count">6 items · 1 sections</span><span class="flag flag-daily">daily brief</span></span>
  <h3 class="headline">Daily brief for Monday.</h3>
  <p class="excerpt excerpt-daily"><strong>TL;DR:</strong> Daily brief for Monday.</p>
</a>`),
    `# Entry

One briefing in the ledger: a mono time and count line, a headline, and a clamped excerpt, separated from the next by a hairline.

**Provide** an \`a.entry\` (the whole row is the link) with a \`span.meta\` (\`.time\`, \`.count\`, optional \`.flag\`), an \`h3.headline\` and a \`p.excerpt\`. Group entries under a \`.dayhead\` per local day (Europe/Budapest).

- Do add \`.time-accent\` to the latest edition of a day and \`.flag-daily\` + \`.excerpt-daily\` to the daily brief.
- Do use a \`.flag\` for run state: \`.flag-degraded\` (⚠ partial) when a collector failed, \`.flag-muted\` ("EN") on a Hungarian page for an English-only entry.
- Do let \`data-density="compact"\` tighten padding and clamp the excerpt to two lines; never hide the time.
- Don't box entries; the hairline is the only separator.

The same row, with search hits wrapped in \`mark\`, is the search result and the arc appearance.

Source: \`render-index.js\`, \`render-search.js\`, \`render-arc.js\`; styles \`.entry\`, \`.dayhead\`, \`.flag\`.
`,
  );

  add(
    "Flag",
    "Ledger",
    90,
    stage(
      `<div class="entry" style="border:0;padding:0"><span class="meta"><span class="flag flag-daily">daily brief</span><span class="flag flag-daily">weekly brief</span><span class="flag flag-degraded" title="polymarket">⚠ partial</span><span class="flag flag-muted">EN</span></span></div>`,
    ),
    `# Flag

A small mono pill on an entry's meta line that says what kind of briefing it is or what went wrong collecting it.

**Provide** a \`span.flag\` inside an entry's \`.meta\` with exactly one modifier:
- \`.flag-daily\`: "daily brief" or "weekly brief", on the roll-up editions.
- \`.flag-degraded\`: "⚠ partial" when a collector failed; the failed sources go in \`title\`.
- \`.flag-muted\`: "EN" on a Hungarian page when the entry exists only in English.

- Do keep the text to one or two words; prefix a failure with ⚠.
- Don't use a flag as a category tag; sections and threads carry topics.

Source: \`render-index.js\`; styles \`.entry .flag\`.
`,
  );

  add(
    "DayHead",
    "Ledger",
    150,
    stage(`<div class="dayhead">Tuesday, 25 August 2026</div>
<a class="entry" href="#"><span class="meta"><span class="time">16:00</span></span><h3 class="headline">Markets slid for a second day.</h3></a>
<div class="unreadfence" role="separator"><span class="line"></span><span class="label">new since your last visit</span><span class="line"></span></div>
<div class="dayhead">Monday, 24 August 2026</div>`),
    `# DayHead

The ledger's dividers: a sticky \`faint\` mono day header per local day, and the \`accent\` "new since your last visit" fence the client inserts after the last unread entry.

**Provide** \`div.dayhead\` with the full local date. The fence (\`.unreadfence\` with two \`.line\`s around a \`.label\`) is built by \`client.js\` from the section's \`data-unread-label\`; never render it on the server.

- Do keep day headers sticky so a long day keeps its date on screen.
- Don't show the fence when everything, or nothing, is new.

Source: \`render-index.js\`, \`client.js\`; styles \`.dayhead\`, \`.unreadfence\`.
`,
  );

  add(
    "NowList",
    "Ledger",
    170,
    stage(`<div class="now"><div class="archivelabel">Now</div><nav class="nowlist" aria-label="Now">
<a class="nowrow" href="#"><span class="nowarrow" aria-hidden="true">↑</span><span class="nowarclabel">Markets slide continues</span><span class="nowmeta">4 hours ago</span></a>
<a class="nowrow" href="#"><span class="nowarrow" aria-hidden="true">↑</span><span class="nowarclabel">EU agrees new package</span><span class="nowmeta">yesterday</span></a>
</nav></div>`),
    `# NowList

A short, quiet list of story threads that moved recently, shown above the ledger under a "Now" label.

**Provide** a \`.now\` block with an \`.archivelabel\` and a \`nav.nowlist\` of \`a.nowrow\`s: an arrow (\`.nowarrow\`), the thread title (\`.nowarclabel\`) and a relative time (\`.nowmeta\`).

- Do keep it to the few threads with fresh coverage; it is a pointer, not a second ledger.
- Don't style rows as chips; they are plain \`muted\` rows on hairlines.

Source: \`render-index.js\`; styles \`.now\`, \`.nowrow\`.
`,
  );

  add(
    "CatchUp",
    "Ledger",
    110,
    stage(`<div class="catchup"><span class="catchuptext">Since your last visit: <a href="#">3 briefings</a> · <a href="#">2 arc updates</a></span><button type="button" class="catchupjump" aria-label="Jump to where you left off">↓</button><button type="button" class="catchupdismiss" aria-label="Dismiss">×</button></div>
<div style="position:relative;height:40px"><button type="button" class="resumechip" style="position:absolute;bottom:0">↓ new since your last visit</button></div>`),
    `# CatchUp

The returning-reader strip: a mono line summarising what arrived since the last visit, with jump and dismiss, plus the floating "↓ new since your last visit" resume chip.

**Provide** \`div.catchup\` with its \`data-prefix\` and \`data-tmpl-*\` strings; \`client.js\` fills \`.catchuptext\` and unhides it. The \`.resumechip\` is created by the client on \`document.body\` and is visible only while the unread fence is below the viewport.

- Do render both hidden on the server; they depend on the reader's local history.
- Don't let the resume chip overlap the back FAB; it offsets by \`fab-size\` and \`fab-inset\`.

Source: \`render-index.js\`, \`client.js\`; styles \`.catchup\`, \`.resumechip\`.
`,
  );

  add(
    "EditionHead",
    "Article",
    170,
    stage(`<nav class="digestnav"><a href="#">← All digests</a><span class="spacer"></span><a class="nav-older" href="#">← 10:00</a></nav>
<header class="edhead"><p class="stamp">digest · 16:00 CEST · 12 items · 3 sections</p><h1>Markets slid for a second day while the <mark>EU package</mark> landed.</h1></header>`),
    `# EditionHead

The top of a digest page: the mono back/older nav, an \`accent\` stamp with time and counts, and the headline as the page's h1.

**Provide** a \`nav.digestnav\` (back link, \`.spacer\`, \`.nav-older\` / newer links), then \`header.edhead\` with \`p.stamp\` and an \`h1\` derived from the TL;DR's first sentence.

- Do repeat \`nav.digestnav.digestnav-bottom\` after the colophon.
- Do allow one \`mark\` in the h1, as on the lead entry.

Source: \`render-digest.js\`; styles \`nav.digestnav\`, \`.stamp\`, \`.edhead\`.
`,
  );

  add(
    "Callouts",
    "Article",
    230,
    stage(`<article class="digest">
<div class="tldr"><span class="tldr-label">TL;DR</span>Markets slid for a second day while the EU package landed.</div>
<div class="attention"><span class="attention-label">⚑ Needs attention</span><p>The council vote on the package closes at 18:00 CEST; two of your groups are organising around it.</p></div>
<p class="banner">⚠ Polymarket could not be collected this run; its items are missing.</p>
</article>`),
    `# Callouts

The digest's three tinted blocks: the TL;DR leader (\`tldr-bg\`, \`radius-panel\`), the attention callout and the collection-failed banner (both \`attention-bg\`, square).

**Provide** them inside \`article.digest\`: \`div.tldr\` first, an optional \`div.attention\` with an \`h2\` or \`.attention-label\` and \`p\`, and \`p.banner\` when a source failed. These arrive in the summarizer's HTML; the site strips inline email styles and these classes take over.

- Do keep exactly one TL;DR per digest; it is also the headline's source.
- Do reserve \`attention\` for something the reader may act on.
- Don't add a left border or icon to any of them.

Source: \`render-digest.js\`, \`sections.js\`; styles \`.tldr\`, \`.attention\`, \`.digest .banner\`.
`,
  );

  add(
    "DigestSection",
    "Article",
    260,
    stage(`<article class="digest" style="counter-reset:secnum">
<h2 id="s1" style="position:static;margin-top:0">Markets slide continues</h2><a class="secarc" href="#">story so far ×2</a>
<p>Equities extended losses. <a href="#">Reuters</a> puts the drawdown at <strong>four percent</strong>.<a class="cite" href="#" title="reuters.com">1</a></p>
<h2 id="s2" style="position:static">EU agrees new package</h2>
<p>The council signed off overnight, with disbursement gated on reforms.<a class="cite" href="#" title="consilium.europa.eu">2</a></p>
</article>`),
    `# DigestSection

A numbered section of a briefing: a display h2 with an automatic 01, 02 … counter in \`accent\` mono, an optional "story so far" thread link, and \`prose\` paragraphs with citation chips.

**Provide** direct-child \`h2\`s (with \`id="sN"\`) inside \`article.digest\`, paragraphs as \`p\`, citations as \`a.cite\` with the source domain in \`title\`, and \`a.secarc\` right after an h2 that belongs to a thread.

- Do keep h2s as direct children of \`.digest\`; the counter and sticky behaviour depend on it.
- Do bold the figure that matters; \`strong\` snaps to full \`text\` against \`prose\`.
- Don't number sections in the text; the counter does it.

Source: \`render-digest.js\`, \`sections.js\`; styles \`.digest > h2\`, \`.digest p\`, \`.cite\`, \`.secarc\`.
`,
    "Sticky positioning is switched off in this preview.",
  );

  add(
    "Cite",
    "Article",
    90,
    stage(
      `<p style="font-family:var(--font-prose);margin:0">Drawdown at four percent<a class="cite" href="#" title="reuters.com">1</a> and widening<a class="cite" href="#" title="ft.com">2</a>. Hits: <mark>Markets</mark> extended losses.</p>`,
    ),
    `# Cite

A superscript citation chip: a 3px-radius \`chip-bg\` pill with the source number in \`accent\` mono, filling solid \`accent\` on hover.

**Provide** \`a.cite\` linking to the source with the source domain in \`title\` (\`sections.js\` adds it); on touch screens CSS shows the domain inside the chip.

- Do place it directly after the claim, with no space.
- Don't use it for anything but sources; search hits use plain \`mark\` on \`chip-bg\`.

Source: \`sections.js\`; styles \`.cite\`, \`mark\`.
`,
  );

  add(
    "TOC",
    "Article",
    150,
    stage(`<nav class="toc"><a href="#s1"><span class="tocnum">01</span>Markets slide continues</a>
<a href="#s2"><span class="tocnum">02</span>EU agrees new package</a>
<a href="#s3"><span class="tocnum">03</span>Chip export rules tighten</a></nav>`),
    `# TOC

The "in this edition" list of numbered section links above a digest, matching the 01, 02 … section numbers.

**Provide** \`nav.toc\` with one \`a\` per section: a \`span.tocnum\` (two-digit) and the section title, linking to \`#sN\`.

- Do generate it from the sections; never write it by hand.
- Don't include the attention callout's heading.

Source: \`render-digest.js\` (\`buildSectionToc\`); styles \`.toc\`.
`,
  );

  add(
    "ThreadChips",
    "Article",
    150,
    stage(`<nav class="arcs" aria-label="Story threads"><a class="arc" href="#"><span class="arclabel">Markets slide continues</span><span class="arccount">×2 this week</span></a><a class="arc" href="#"><span class="arclabel">Chip export rules</span><span class="arccount">×3 this week</span></a></nav>
<div class="archivelabel" style="margin-top:1.2em">What changed</div><nav class="deltas" aria-label="What changed"><a class="delta" href="#"><span class="deltalabel">Markets slide continues</span><p class="deltatext"><span class="deltaprev">Down two percent</span><span class="deltaarrow">→</span><span class="deltanow">Down four percent</span></p></a></nav>`),
    `# ThreadChips

Story threads ("arcs") on a digest: pill chips on \`chip-bg\` naming each thread with its weekly count, and a "What changed" list showing a previous → current value.

**Provide** \`nav.arcs\` of \`a.arc\` (\`.arclabel\`, \`.arccount\`) and, when a thread's key fact moved, \`nav.deltas\` of \`a.delta\` with \`.deltalabel\` and a \`p.deltatext\` (\`.deltaprev\`, \`.deltaarrow\`, \`.deltanow\`).

- Do show a delta only when both values are known.
- Don't colour deltas red or green; the previous value is \`muted\`, the current one \`text\`.

Source: \`render-digest.js\`; styles \`.arcs\`, \`.deltas\`.
`,
  );

  add(
    "ArcHead",
    "Article",
    190,
    stage(`<div class="archead"><h1 class="arctitle">Markets slide continues</h1></div><button type="button" class="followtoggle" aria-pressed="false">☆ Follow</button>
<p class="stamp">2 appearances · first seen Tue 25 Aug · updated 4 hours ago · ↑ more coverage</p>
<details class="arccontext" open><summary>Background</summary><div class="arccontextbody"><p>A sell-off that began on the 24th; central banks have not reacted yet.</p></div></details>`),
    `# ArcHead

The head of a story-thread page: the thread title, a bare Follow text toggle, an \`accent\` stamp of appearances and freshness, and a collapsible Background.

**Provide** \`.archead\` with \`h1.arctitle\` (carrying \`data-follow-add\` / \`data-follow-remove\`; the client adds the \`.followtoggle\` button), a \`p.stamp\`, and \`details.arccontext\`.

- Do keep Follow a text control (☆ Follow / ★ Following) with no button chrome.

Source: \`render-arc.js\`, \`client.js\`; styles \`.arctitle\`, \`.followtoggle\`, \`.arccontext\`.
`,
  );

  add(
    "Colophon",
    "Article",
    190,
    stage(
      `<details class="colophon" open><summary>How this was made</summary><div class="colophonbody"><div class="sourcekey"><span class="sklabel">Sources</span><span class="sk"><i style="background:#4f8fd9"></i>telegram 5</span><span class="sk"><i style="background:#8a8f9e"></i>x 4</span><span class="sk"><i style="background:#9aa0ab"></i>rss 3</span><span class="sk sk-failed">⚠ polymarket</span></div><div class="provenance"><div class="provline"><span class="sklabel">Written by</span><span class="sk" title="claude-opus-5">Opus 5 · high</span></div><div class="provline"><span class="sklabel">Translated with</span><span class="sk sk-fallback">↻ GPT-5.6 Terra · high</span></div></div></div></details>`,
    ),
    `# Colophon

The collapsible "How this was made" footer of a digest: per-source item counts with colour keys, failed sources, and which model wrote and translated it.

**Provide** \`details.colophon\` with a \`.sourcekey\` (\`.sklabel\`, then \`.sk\` per source with an \`i\` swatch; \`.sk-failed\` for a source that failed) and a \`.provenance\` of \`.provline\`s; \`.sk-fallback\` with ↻ marks a fallback model.

- Do keep it collapsed by default; it is for the curious, not the reader.
- The source swatch colours are per-source literals in \`render-digest.js\`, not tokens.

Source: \`render-digest.js\`; styles \`.colophon\`, \`.sourcekey\`, \`.provenance\`.
`,
  );

  add(
    "WeekRail",
    "Navigation",
    120,
    stage(
      `<nav class="weekrail" aria-label="Week navigation"><span class="rail-older"><a href="#">← W34</a></span><span class="rail-center">Week 35 · 24–30 Aug<span class="railspark" aria-hidden="true"><i style="height:40%"></i><i style="height:100%"></i><i style="height:65%"></i><i class="sd0"></i><i style="height:15%"></i><i class="sd0"></i><i class="sd0"></i></span></span><span class="rail-newer"><a href="#">W36 →</a></span></nav>`,
    ),
    `# WeekRail

Week-by-week navigation for the index: older and newer week links either side of a centred week label with a seven-bar sparkline of items per day.

**Provide** \`nav.weekrail\` with \`.rail-older\`, \`.rail-center\` and \`.rail-newer\`; the sparkline is a \`span.railspark\` of seven \`i\` bars, each \`height\` a percentage of the week's busiest day (minimum 15%), zero days as \`i.sd0\`.

- Do omit the sparkline for an empty week.
- Leave \`.rail-newer\` empty, not missing, on the current week so the label stays centred.

Source: \`render-index.js\`; styles \`.weekrail\`, \`.railspark\`.
`,
  );

  add(
    "Search",
    "Controls",
    200,
    stage(`<form class="searchform" onsubmit="return false"><input class="filter" type="search" value="markets" placeholder="Search all briefings…" aria-label="Search all briefings…"><button class="searchbtn" type="submit">Search</button></form>
<p class="empty">2 results</p>
<a class="entry" href="#"><span class="meta"><span class="time">Tue 25 Aug 16:00</span></span><p class="excerpt"><mark>Markets</mark> extended losses into the close…</p></a>`),
    `# Search

The full-text search form and its results: a rounded input beside a pill Search button, then entry rows with hits highlighted.

**Provide** \`form.searchform\` (GET, \`q\`) with \`input.filter\` and \`button.searchbtn\`, a \`p.empty\` result count, and one \`a.entry\` per hit with matches wrapped in \`mark\`. The masthead's \`.searchpop\` holds the quick in-page filter and links here.

- Do keep the button secondary: outlined, never filled.

Source: \`render-search.js\`, \`hrefs.js\`; styles \`.searchform\`, \`.searchbtn\`.
`,
  );

  add(
    "CommandPalette",
    "Controls",
    320,
    `<div class="cmdpalette-backdrop" style="position:absolute;padding-top:24px"><div class="cmdpalette" role="dialog" aria-label="Command palette"><input class="cmdpalette-input" type="text" value="da" placeholder="Type a command or search…" aria-label="Type a command or search…"><div class="cmdpalette-list" role="listbox"><div class="cmdpalette-item active" role="option" aria-selected="true"><span>Daily view</span><span class="cmdpalette-group">View</span></div><div class="cmdpalette-item" role="option"><span>Latest briefing</span><span class="cmdpalette-group">Go</span></div><div class="cmdpalette-item" role="option"><span>Markets slide continues</span><span class="cmdpalette-group">Thread</span></div></div></div></div>
<div style="height:290px"></div>`,
    `# CommandPalette

A keyboard-first command palette (⌘K / Ctrl+K, or /) over a dimmed backdrop: a text input and a filtered list of views, destinations and threads.

**Provide** nothing on the server except \`span.paletteconfig\` carrying the localized labels as \`data-*\` attributes; \`client.js\` builds \`.cmdpalette-backdrop\` > \`.cmdpalette\` with \`.cmdpalette-input\`, \`.cmdpalette-list\` and \`.cmdpalette-item\`s (\`.active\` on the highlighted row, \`.cmdpalette-group\` for its kind).

- Do add new destinations here rather than to the masthead.
- Do close on Escape and on a backdrop click.

Source: \`client.js\`; styles \`.cmdpalette*\`.
`,
  );

  add(
    "BackFab",
    "Navigation",
    110,
    stage(
      `<div style="position:relative;height:64px"><a class="backfab show" href="#" aria-label="Back to top" style="position:absolute;right:0;bottom:0;left:auto">↑</a></div>`,
    ),
    `# BackFab

The round floating button in the corner: back to top on long pages (↑), back to all digests on a digest (←).

**Provide** \`a.backfab\` with an \`aria-label\`; the client toggles \`.show\` after the reader scrolls. It is \`fab-size\` across, inset \`fab-inset\`, solid \`accent\` with \`bg\` glyph, \`float-shadow\`.

- Do keep exactly one per page.
- Don't put text in it; one arrow glyph.

Source: \`render-*.js\`, \`client.js\`; styles \`.backfab\`.
`,
  );

  // Cover
  put(
    "components/Cover/preview.html",
    `<!-- @dsCard height=300 -->
<style>
  html, body { margin: 0; padding: 0; background: var(--bg); }
  .cv { position: relative; width: 960px; height: 300px; overflow: hidden; background: var(--bg); }
  .cv svg { position: absolute; inset: 0; }
  .b-accent { fill: var(--accent); }
  .b-ink { fill: var(--rule-heavy); }
  .b-mark { fill: var(--mark); }
  .b-tint { fill: var(--tldr-bg); }
  .b-hair { stroke: var(--hairline); stroke-width: 1; }
  .numeral { fill: var(--bg); font-family: var(--font-data); font-size: 230px; font-weight: 700; letter-spacing: -0.04em; }
  .name { position: absolute; left: 40px; bottom: 74px; max-width: 420px; margin: 0; font-family: var(--font-chrome); font-weight: 800; font-size: 100px; line-height: 0.92; letter-spacing: -0.03em; color: var(--text); }
  .tag { position: absolute; left: 42px; bottom: 40px; max-width: 420px; margin: 0; font-family: var(--font-data); font-size: 13px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }
</style>
<div class="cv">
  <!--
    blocks: accent slab 360x196 (bleeds top and right); tldr-bg tint 120x196; rule-heavy bar 480x8 (2x rule-heavy-w); mark band 300x96 and rule-heavy block 180x96, both bleeding the bottom edge. All radius-square.
    arrangement: a masthead read sideways. Tint and slab on top, the heavy rule under them, highlighter and ink below, like a lead story over its rule.
    pattern: the oversized numeral row. The section counter 01 in the data face, cut out of the accent slab in bg, because numbered sections in accent mono are this site's signature.
    scales: radius-square everywhere (surfaces are square); rule-heavy-w doubled for the rule; two hairline rules on the tint at the column gutter pitch.
  -->
  <svg width="960" height="300" viewBox="0 0 960 300" aria-hidden="true">
    <rect class="b-tint" x="480" y="0" width="120" height="196"/>
    <line class="b-hair" x1="480" y1="64" x2="600" y2="64"/>
    <line class="b-hair" x1="480" y1="128" x2="600" y2="128"/>
    <rect class="b-accent" x="600" y="0" width="360" height="196"/>
    <text class="numeral" x="626" y="186">01</text>
    <rect class="b-ink" x="480" y="196" width="480" height="8"/>
    <rect class="b-mark" x="480" y="204" width="300" height="96"/>
    <rect class="b-ink" x="780" y="204" width="180" height="96"/>
  </svg>
  <h1 class="name">Digest News</h1>
  <p class="tag">Briefings every six hours, set like a poster</p>
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
          note: `Generated from workers/news-site: palette, type, radii, ${C.length} components, logos.`,
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
    const [fg, bg] = pair.split("/");
    console.log(
      `${(fg + " on " + bg).padEnd(30)} light ${l.toFixed(2).padStart(5)}  dark ${d.toFixed(2).padStart(5)}`,
    );
  }
  console.log("\ncomponents:", components.join(", "));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main();
}
