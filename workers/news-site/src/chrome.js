import { STRINGS } from "./strings.js";
import { CSS } from "./css.js";
import { esc } from "./http.js";
import { indexHref, renderViewTabs } from "./hrefs.js";
import { CLIENT_SCRIPT } from "./client.js";

// ── page chrome (shared masthead/footer/CSS — one template, both pages) ─

// The tab icon: the site's indigo accent as a rounded square, three white
// "digest lines" of tapering width — reads as a summary/list at 16px, and
// the indigo works against both light and dark browser chrome.
export const FAVICON_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#4f46e5"/>
<rect x="14" y="18" width="36" height="6" rx="3" fill="#ffffff"/>
<rect x="14" y="30" width="28" height="6" rx="3" fill="#ffffff" opacity="0.85"/>
<rect x="14" y="42" width="20" height="6" rx="3" fill="#ffffff" opacity="0.7"/>
</svg>`;

// The first host label is the whole brand ("NEWS") — the .tld tail this
// used to also return died with the two-tone wordmark and nothing ever
// consumed it since.
export function brandFirst(host) {
  const idx = host.indexOf(".");
  return idx === -1 ? host : host.slice(0, idx);
}

// `title` defaults to null, falling back to the bare host — that default IS
// the index page's title. Bare-hostname titles made every browser tab and
// history entry indistinguishable from each other (roadmap step 2); digest
// pages now pass a per-digest title instead (see renderDigestPage). Escaped
// here, once, same as the host fallback — callers pass the raw string.
//
// `prefetchHref` defaults to null, same contract again (roadmap 2 step 7):
// only renderIndexPage ever passes a value, and only when the index is
// non-empty — the lead card's digest is the reader's most likely next tap,
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
// sitting in the lead card's own href on the same page (same-document
// exposure), and the speculation rules processor doesn't send that URL
// anywhere the visible link wouldn't already send it on a click.
// The masthead is ONE layout on every page (owner follow-up: it must not
// change shape between the index and a digest page): big brand left,
// view-tab capsule centered, search/settings right, with an optional mono
// issue line above — the index passes buildIssueLine's edition line, the
// digest page passes its own "No. {id} · {date}", and pages with nothing
// to say (arc, search) pass none, keeping the row itself identical.
// `issueLineText` is the RAW (unescaped) issue-line string; pageChrome
// esc()s it once here, same convention as `title` just below.
export function pageChrome(
  host,
  token,
  lang,
  view,
  switchersHtml,
  bodyHtml,
  title = null,
  prefetchHref = null,
  issueLineText = "",
  showTopFab = true,
) {
  const viewTabsHtml = renderViewTabs(token, lang, view);
  // Only the first host label renders — the brand is just "NEWS" (owner
  // follow-up; see brandFirst).
  const first = brandFirst(host);
  const strings = STRINGS[lang];
  const prefetchLinkHtml = prefetchHref ? `<link rel="prefetch" href="${esc(prefetchHref)}">` : "";
  // Scroll-to-top FAB for every page that is not a digest (owner-requested:
  // "on a main page where there is no button I want an up button"). The
  // digest page renders its own .backfab — an arrow BACK to the index, which
  // is the more useful action there — and opts out via showTopFab, so no page
  // ever shows two.
  //
  // Same .backfab class as that one on purpose, not a new one: the class is
  // the FAB *primitive* here (shape, placement, the scroll-past-320px reveal
  // in wirePage(), the safe-area insets, the print rule, the <noscript>
  // always-visible fallback). Reusing it means this button inherits all of
  // that and needs no CSS or JS of its own — wirePage() re-queries .backfab
  // on every soft-nav pass, so it rebinds across navigations for free.
  //
  // href="#top" rather than a JS scroll handler: with no element of that id,
  // HTML defines "#top" as the top of the document, so it works with JS off,
  // and html { scroll-behavior: smooth } (already reduced-motion-gated)
  // animates it. It also survives the soft-nav interceptor untouched — that
  // handler explicitly bails on a same-path link carrying a hash, handing it
  // back to the browser instead of re-fetching the page.
  const topFabHtml = showTopFab
    ? `<a class="backfab" href="#top" aria-label="${esc(strings.topFabLabel)}">↑</a>`
    : "";
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
  return `<!doctype html>
<html lang="${lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- theme-color must track the CSS palette blocks' --bg values above (light
     #ffffff / dark #131418 — "print poster" redesign) so mobile browser
     chrome (URL bar/status bar tint) melts into the page instead of showing
     a stock color. The prefers-color-scheme media attrs cover the automatic
     (Auto) case; a manual Light/Dark override from the theme miniseg (see
     the bottom script) updates both metas' content directly, since a
     media-query meta can't react to a data-theme attribute switch on its
     own — and clicking back to Auto restores each meta to its own
     media-appropriate value. -->
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#ffffff">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#131418">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
${prefetchLinkHtml}
<title>${esc(title ?? host)}</title>
<script>try{document.documentElement.dataset.theme=localStorage.getItem("theme")||"";document.documentElement.dataset.fontsize=localStorage.getItem("fontsize")||"";document.documentElement.dataset.font=localStorage.getItem("font")||"";document.documentElement.dataset.density=localStorage.getItem("density")||""}catch(e){}</script>
<style>${CSS}</style>
</head>
<body>
${prefetchScriptHtml}
<div class="wrap">
  ${issueLineText ? `<div class="issueline">${esc(issueLineText)}</div>` : ""}
  <header class="mast mast-big">
    <div class="mastleft">
      <a class="brand" href="${indexHref(token, lang, view)}">${esc(first)}</a>
    </div>
    ${viewTabsHtml}
    <div class="mastright">
      ${switchersHtml}
    </div>
  </header>
  ${paletteConfigHtml}
  ${bodyHtml}
  ${topFabHtml}
</div>
<noscript><style>.backfab { opacity: 1; pointer-events: auto; }
  /* The sticky section headings need wirePage's push loop to stop them
     stacking on top of one another; with no script they go back to being
     ordinary headings rather than a pile at the top of the viewport. */
  .digest > h2 { position: static; }</style></noscript>
<script>
${CLIENT_SCRIPT}
</script>
</body>
</html>`;
}
