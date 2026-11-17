import { STRINGS } from "./strings.js";
import { CSS } from "./css.js";
import { esc } from "./http.js";
import {
  indexHref,
  manifestHref,
  renderSearchBubble,
  renderSearchButton,
  renderSettingsPanel,
  renderViewTabs,
  swHref,
} from "./hrefs.js";
import { CLIENT_SCRIPT } from "./client.js";
import { APPLE_TOUCH_ICON_HREF, FAVICON_HREF } from "./icons.js";

// ── page chrome (shared bar/layout/CSS — one template, every page) ──────

// The tab icon: the "n." monogram — a bold white n with a periwinkle dot at
// its lower right, on a deep-ink ground with a soft indigo glow from the top.
// Shown RAW by the browser (no OS mask), so the ground carries its own
// rounded corners (rx 112 = 22% of 512); src/icons.js renders the same mark
// full-bleed where the OS does the rounding. The group is shifted (-16,-20)
// so the n + dot pair is visually centred. Gradient only: no filters, no
// external refs, so it stays small and survives 16px rendering.
export const FAVICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs><radialGradient id="g" cx="50%" cy="8%" r="75%"><stop offset="0" stop-color="#3a42a8"/><stop offset=".55" stop-color="#151a3a"/><stop offset="1" stop-color="#0b0d14"/></radialGradient></defs>
<rect width="512" height="512" rx="112" fill="url(#g)"/>
<g transform="translate(-16 -20)"><path d="M150 380V258a86 86 0 0 1 172 0V380" fill="none" stroke="#ffffff" stroke-width="62" stroke-linecap="round" stroke-linejoin="round"/><circle cx="392" cy="364" r="34" fill="#9db0ff"/></g>
</svg>`;

// The first host label is the whole brand ("news") — the .tld tail this
// used to also return died with the two-tone wordmark and nothing ever
// consumed it since. The installed app's name is this plus a literal "."
// (appName below), mirroring the wordmark's accent full stop.
export function brandFirst(host) {
  const idx = host.indexOf(".");
  return idx === -1 ? host : host.slice(0, idx);
}

// The name the OS shows for the installed app ("news."): lowercase like the
// on-page wordmark, with the full stop the wordmark draws as `.brandmark`.
// One function so the manifest and the iOS Home Screen title cannot drift.
export function appName(host) {
  return `${brandFirst(host)}.`;
}

// `title` defaults to null, falling back to the bare host — that default IS
// the index page's title. Bare-hostname titles made every browser tab and
// history entry indistinguishable from each other (roadmap step 2); digest
// pages now pass a per-digest title instead (see renderDigestPage). Escaped
// here, once, same as the host fallback — callers pass the raw string.
//
// `prefetchHref` defaults to null, same contract again (roadmap 2 step 7):
// only renderIndexPage ever passes a value, and only when the index is
// non-empty — the hero's digest is the reader's most likely next tap,
// so hint the browser to fetch it early. Digest pages never pass this
// (deliberate restraint — prev/next COULD be prefetched too, but that's 2
// extra fetches per read × 8 reads/day for what the roadmap scoped as
// "lead only"; not worth it here). Two progressive, independent mechanisms
// render from the one href: a <link rel="prefetch"> in <head> (Firefox and
// other browsers without Speculation Rules support) and a
// <script type="speculationrules"> in <body> (Chrome/Edge, the modern,
// preferred hint). Both are best-effort: this site's pages are
// `Cache-Control: private, no-store` (see the file-header comment / trust
// model), and a browser is free to simply not prefetch a no-store response
// — that's the deal with speculative loading in general, not a bug here, so
// neither mechanism is load-bearing for anything. Privacy-wise this adds
// nothing new: the JSON embeds the same capability-token URL that's already
// sitting in the hero's own href on the same page (same-document
// exposure), and the speculation rules processor doesn't send that URL
// anywhere the visible link wouldn't already send it on a click.
// The shell is ONE layout on every page: a single header.bar (brand, view
// tabs, search, settings button) that CSS renders as the desktop top bar AND
// the phone bottom dock — never a second nav in the markup — a phone-only
// .phonehead row (brand + date), and a <main class="layout"> holding an
// optional full-width `heroHtml`, then .maincol and an optional .side column.
// The hero is a layout child rather than part of .maincol so it can span both
// columns; the page's h1 must still sit inside <main>, hence <main> IS the
// layout. Pages with nothing to put in the sidebar pass `sidebarHtml: null`
// and the settings panel rides after </main> at the end of .wrap instead;
// with a sidebar it lives inside .side. Single-column pages keep their hero
// in `bodyHtml`.
// The phone brand is a real link (CSS shows one of the two brands per
// breakpoint), so the hidden one is display:none, not aria-hidden.
// `issueLineText` is the RAW (unescaped) masthead date, shown in
// .phonehead; pageChrome esc()s it once here, same convention as `title`.
export function pageChrome({
  host,
  token,
  lang,
  view,
  pageKind,
  id,
  archiveWeek,
  showSearch,
  appVersion,
  bodyHtml,
  heroHtml = "",
  sidebarHtml = null,
  title = null,
  prefetchHref = null,
  issueLineText = "",
}) {
  const viewTabsHtml = renderViewTabs(token, lang, view);
  // Only the first host label renders — the brand is just "news" (owner
  // follow-up; see brandFirst).
  const first = brandFirst(host);
  const strings = STRINGS[lang];
  const homeHref = indexHref(token, lang, view);
  // A page HAS a sidebar when it passes any string, empty included: the
  // index and digest pages always want the settings panel in the sidebar on
  // desktop, even when their own card (NOW / Earlier) has nothing to show.
  // null (arc, search, about) means single-column, panel after the layout.
  const hasSide = sidebarHtml !== null && sidebarHtml !== undefined;
  const settingsPanelHtml = renderSettingsPanel({
    token,
    lang,
    view,
    pageKind,
    id,
    archiveWeek,
    appVersion,
  });
  // Search: the popover (with the ledger filter input) where the page has a
  // ledger-or-nothing to drive, a plain link to the search page elsewhere.
  const searchHtml = showSearch
    ? renderSearchBubble(token, lang, strings)
    : renderSearchButton(token, lang, strings);
  // Sliders icon (not a gear: it reads as "preferences" at 18px without the
  // cog's busy teeth). Inline SVG for the same cross-platform reason as the
  // search magnifier; currentColor keeps it theme-aware.
  const settingsIcon = `<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" focusable="false"><path d="M3 6h9M16 6h1M3 14h1M8 14h9" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="14" cy="6" r="2" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="6" cy="14" r="2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`;
  // Hungarian ő/ű exist only in the latin-ext subsets, so HU pages preload
  // those too; EN pages would pay for bytes they never paint.
  const fontFiles = ["inter-latin", "sora-latin"];
  if (lang === "hu") fontFiles.push("inter-latin-ext", "sora-latin-ext");
  const fontPreloadHtml = fontFiles
    .map(
      (f) =>
        `<link rel="preload" href="/fonts/${f}-wght.v1.woff2" as="font" type="font/woff2" crossorigin>`,
    )
    .join("\n");
  const prefetchLinkHtml = prefetchHref ? `<link rel="prefetch" href="${esc(prefetchHref)}">` : "";
  const prefetchScriptHtml = prefetchHref
    ? `<script type="speculationrules">${JSON.stringify({ prefetch: [{ urls: [prefetchHref] }] })}</script>`
    : "";
  // ⌘K command palette strings (§11.1 PR C): the same data-* carrier
  // pattern the unread fence/archiveResultsHtml already use
  // above and elsewhere in this file — a hidden element whose attributes
  // the client script reads, so the palette IIFE in the bottom <script>
  // stays lang-agnostic. Lives INSIDE .wrap (unlike the palette's own
  // dialog markup, created once by that IIFE and left outside .wrap — see
  // its comment) specifically so a soft-nav LANGUAGE hop refreshes these
  // strings along with everything else the swap replaces; the palette
  // re-reads them from here on every open, never caching them at creation
  // time, so a stale EN string can never survive into a HU page.
  const paletteConfigHtml = `<span class="paletteconfig" hidden
    data-label="${esc(strings.paletteLabel)}"
    data-placeholder="${esc(strings.palettePlaceholder)}"
    data-empty="${esc(strings.emptyFiltered)}"
    data-cmd-top="${esc(strings.paletteCmdTop)}"
    data-cmd-all="${esc(strings.paletteCmdAll)}"
    data-cmd-daily="${esc(strings.paletteCmdDaily)}"
    data-cmd-weekly="${esc(strings.paletteCmdWeekly)}"
    data-cmd-search="${esc(strings.searchButton)}"
    data-cmd-archive="${esc(strings.archiveLabel)}"
    data-cmd-switchlang="${esc(strings.paletteCmdSwitchLang)}"
    data-cmd-latest="${esc(strings.paletteCmdLatest)}"
  ></span>`;
  // PWA shell wiring (PLAN.md §11.7), three decisions worth naming since
  // the markup below is terse about them:
  //
  // - The manifest link is TOKEN-SCOPED and language-specific (it carries
  //   start_url); the touch icon is deliberately TOKENLESS, the same
  //   precedent /favicon.svg already set. An icon reveals nothing, and
  //   keeping icons off the token path means the installed app record
  //   embeds the capability token only in the manifest's own
  //   start_url/scope/id rather than in four more URLs beside it.
  // - mobile-web-app-capable is what makes an iOS Home Screen launch open
  //   standalone, which is in turn the ONLY context iOS delivers Web Push
  //   in — so it is load-bearing for the whole feature, not decoration.
  // - apple-mobile-web-app-status-bar-style is deliberately left at its
  //   default (opaque bar in theme-color). black-translucent would draw the
  //   page under the status bar but forces WHITE status text, unreadable on
  //   the near-white light theme's top edge, and a light-mode scrim would
  //   put a visible band back. The default also keeps the safe-area inset
  //   at 0 in standalone, so .phonehead's inset padding is not doubled.
  // - The service-worker registration is its own script tag rather than
  //   part of CLIENT_SCRIPT because the script URL must be absolute and
  //   token-bearing, and CLIENT_SCRIPT is one static string with no access
  //   to the token. Registering the same URL twice is a no-op, so this
  //   needs none of wirePage()'s teardown/rebind discipline — a soft
  //   navigation simply never re-runs it. Failure is swallowed on purpose:
  //   an unavailable worker (private window, iOS Lockdown Mode, a reader
  //   who blocked it) must cost nothing, because every page here works
  //   fully without one.
  return `<!doctype html>
<html lang="${lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<!-- theme-color must track the CSS palette blocks' page background (light
     #eceff6 / dark #0b0d14 — Spatial Glass) so mobile browser chrome (URL
     bar/status bar tint) melts into the page instead of showing a stock
     color. The status-bar strip is FLAT theme-color, so the page's top edge
     must equal --bg (css.js fades the glow in below the edge); any visible
     glow at y=0 shows as a hard seam. client.js THEME_COLORS (the manual-override path) and pwa.js
     (offline page, manifest colors) carry the same values. The prefers-color-scheme media attrs cover the
     automatic (Auto) case; a manual Light/Dark override from the theme
     miniseg (see the bottom script) updates both metas' content directly,
     since a media-query meta can't react to a data-theme attribute switch
     on its own — and clicking back to Auto restores each meta to its own
     media-appropriate value. -->
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#eceff6">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#0b0d14">
<link rel="icon" type="image/svg+xml" href="${FAVICON_HREF}">
<!-- PWA shell — see src/chrome.js for why each line is shaped this way. -->
<link rel="manifest" href="${manifestHref(token, lang)}">
<link rel="apple-touch-icon" href="${APPLE_TOUCH_ICON_HREF}">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="${esc(appName(host))}">
${prefetchLinkHtml}
<title>${esc(title ?? host)}</title>
<!-- Font preloads sit ahead of the stylesheet so the fetch starts while the CSS
     is still being parsed. crossorigin is REQUIRED even though the files are
     same-origin: fonts are always fetched in CORS mode, and a preload
     without it is made in a different mode, never matches the later
     @font-face request, and downloads the file twice. -->
${fontPreloadHtml}
<script>try{document.documentElement.dataset.theme=localStorage.getItem("theme")||"";document.documentElement.dataset.fontsize=localStorage.getItem("fontsize")||"";document.documentElement.dataset.density=localStorage.getItem("density")||"";localStorage.removeItem("font");document.documentElement.classList.add("enter")}catch(e){}</script>
<style id="site-css">${CSS}</style>
</head>
<body>
${prefetchScriptHtml}
<div class="wrap">
  <header class="bar">
    <a class="brand" href="${homeHref}">${esc(first)}<span class="brandmark" aria-hidden="true">.</span></a>
    ${viewTabsHtml}
    <div class="baractions">
      ${searchHtml}
      <button type="button" class="settingsbtn" popovertarget="settings" aria-label="${esc(strings.settingsLabel)}">${settingsIcon}</button>
    </div>
  </header>
  <div class="phonehead">
    <a class="brand" href="${homeHref}">${esc(first)}<span class="brandmark" aria-hidden="true">.</span></a>
    ${issueLineText ? `<div class="issueline">${esc(issueLineText)}</div>` : ""}
  </div>
  ${paletteConfigHtml}
  <main class="layout${hasSide ? " has-side" : ""}" id="main">
    ${heroHtml}
    <div class="maincol">${bodyHtml}</div>
    ${hasSide ? `<aside class="side" aria-label="${esc(strings.sidebarLabel)}">${sidebarHtml}${settingsPanelHtml}</aside>` : ""}
  </main>
  ${hasSide ? "" : settingsPanelHtml}
</div>
<script id="site-client">
${CLIENT_SCRIPT}
</script>
<script>if("serviceWorker" in navigator)addEventListener("load",function(){navigator.serviceWorker.register(${JSON.stringify(swHref(token))}).catch(function(){})});</script>
</body>
</html>`;
}
