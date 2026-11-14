// Class-coverage guard for the CSS rewrite: every class the site emits —
// in the server markup (test/golden/*.html) or created/toggled by the client
// script — must appear as a class selector in src/css.js, unless it is a
// declared JS-only hook. A class that is emitted but unstyled is exactly the
// silent regression a full stylesheet rewrite produces.
//
// Also prints (never fails on) the reverse: CSS class selectors nothing
// emits, as dead-rule candidates. Run `node test/coverage.test.mjs --report`
// or `COVERAGE_REPORT=1 npm test` to see the list.
//
// src/pwa.js is skipped on purpose: its offline page is a self-contained
// document with inline styles and no class attributes, so css.js does not
// style anything it emits.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

import { CSS } from "../src/css.js";
import { CLIENT_SCRIPT } from "../src/client.js";

const GOLDEN_DIR = new URL("./golden/", import.meta.url);

// Classes that exist purely as JS/test hooks and are intentionally unstyled.
// Each entry says how it is used; when the redesign makes one styled, delete it.
export const JS_ONLY_HOOKS = {
  "miniseg-theme":
    "client.js wires the group via querySelector('.miniseg-' + name); styling is the shared .miniseg",
  "miniseg-size": "same: JS lookup key for the text-size group, styled by the shared .miniseg",
  "miniseg-density": "same: JS lookup key for the density group, styled by the shared .miniseg",
  pushrow: "client.js querySelector('.pushrow') to unhide the row; layout comes from .settingsrow",
  paletteconfig: "hidden data-* carrier read by the command palette (client.js); never rendered",
  "nav-older":
    "client.js keyboard-nav lookup for the older-digest link; the link is styled by its container",
  "sk-failed":
    "deliberately no override: muted stays muted, the warning glyph is the marker (see css.js comment)",
  "sk-fallback":
    "deliberately no override: muted stays muted, the arrow glyph is the marker (see css.js comment)",
};

const CLASS_RE = /^[A-Za-z_][\w-]*$/;
const addTokens = (set, str) => {
  for (const t of str.split(/\s+/)) if (CLASS_RE.test(t)) set.add(t);
};

// Classes emitted by server markup: every class="…" in the goldens, outside
// <style>, <script> and comments.
function goldenClasses() {
  const out = new Map(); // class -> first page seen on
  for (const f of readdirSync(GOLDEN_DIR).filter((n) => n.endsWith(".html"))) {
    const html = readFileSync(new URL(f, GOLDEN_DIR), "utf8")
      .replace(/<style[\s\S]*?<\/style>/g, "")
      .replace(/<script[\s\S]*?<\/script>/g, "")
      .replace(/<!--[\s\S]*?-->/g, "");
    for (const m of html.matchAll(/\sclass="([^"]*)"/g)) {
      const s = new Set();
      addTokens(s, m[1]);
      for (const c of s) if (!out.has(c)) out.set(c, f);
    }
  }
  return out;
}

// Classes the client script creates or toggles. CLIENT_SCRIPT is a template
// literal export, so its text is the source as shipped (escapes already
// resolved, so HTML strings show class=" or class=\" depending on quoting).
function clientClasses() {
  const out = new Map();
  const src = CLIENT_SCRIPT.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  const lits = (stmt) => [...stmt.matchAll(/(["'])((?:(?!\1)[^\\\n])*)\1/g)].map((m) => m[2]);
  const note = (s) => {
    const set = new Set();
    addTokens(set, s);
    for (const c of set) if (!out.has(c)) out.set(c, "src/client.js");
  };
  // className = "a" + (cond ? " b" : "");  (every literal up to the ;)
  for (const m of src.matchAll(/className\s*=\s*([^;\n]*)/g)) lits(m[1]).forEach(note);
  // classList.add/remove/toggle("a", "b")  — contains() only reads, but a
  // read of a class nothing styles is still a hook worth listing.
  for (const m of src.matchAll(/classList\.(?:add|remove|toggle|contains)\(([^)]*)\)/g)) {
    lits(m[1]).forEach(note);
  }
  // class="…" / class=\"…\" inside HTML strings
  for (const m of src.matchAll(/class=\\?["']([^"'\\]*)/g)) note(m[1]);
  return out;
}

function cssClasses() {
  const css = CSS.replace(/\/\*[\s\S]*?\*\//g, "").replace(/"(?:[^"\\\n]|\\.)*"/g, '""');
  const out = new Set();
  // A class selector token: "." + ident, ident must not continue (so .entry
  // is not satisfied by .entry-lead — the regex consumes the whole ident).
  for (const m of css.matchAll(/\.(-?[A-Za-z_][\w-]*)/g)) out.add(m[1]);
  return out;
}

function analyse() {
  const emitted = new Map([...goldenClasses()]);
  for (const [c, where] of clientClasses()) if (!emitted.has(c)) emitted.set(c, where);
  const styled = cssClasses();
  const uncovered = [...emitted].filter(([c]) => !styled.has(c) && !(c in JS_ONLY_HOOKS));
  const dead = [...styled].filter((c) => !emitted.has(c)).sort();
  return { emitted, styled, uncovered, dead };
}

test("every emitted class is styled in css.js or declared a JS-only hook", () => {
  const { uncovered } = analyse();
  assert.deepEqual(
    uncovered.map(([c, where]) => `${c} (first seen: ${where})`),
    [],
    "emitted classes with no CSS selector — style them or add them to JS_ONLY_HOOKS with a reason",
  );
});

test("JS_ONLY_HOOKS has no stale entries", () => {
  const { emitted, styled } = analyse();
  for (const c of Object.keys(JS_ONLY_HOOKS)) {
    assert.ok(emitted.has(c), `${c} is no longer emitted — remove it from JS_ONLY_HOOKS`);
    assert.ok(!styled.has(c), `${c} is now styled — remove it from JS_ONLY_HOOKS`);
  }
});

// Non-failing: CSS classes nothing emits (dead-rule candidates). Some are
// legitimately emitted from places this scan cannot see (state classes on
// <html>, classes built by string concatenation), so treat it as a lead list.
test(
  "dead-rule candidates (report only)",
  { skip: !process.env.COVERAGE_REPORT && !process.argv.includes("--report") },
  () => {
    const { dead } = analyse();
    console.log(`dead-rule candidates (${dead.length}):\n  ${dead.join("\n  ")}`);
  },
);
