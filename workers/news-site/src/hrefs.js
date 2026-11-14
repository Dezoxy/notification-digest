import { STRINGS } from "./strings.js";
import { CSS } from "./css.js";
import { esc } from "./http.js";

// ── language/view-space path helpers (keep every internal link inside the
// current language×view space: index↔index, digest↔digest, EN pages never
// link into /hu/ and vice versa, all-view pages never link into daily/ or
// weekly/ and vice versa, except via the two explicit switchers) ─────────

// The two URL segments every builder below assembles: "" or "hu/", and ""
// or "daily/"/"weekly/". One definition each — these used to be re-derived
// inline in five builders (and the view fold's inverse lives in the router
// as viewFromSeg).
export function langSeg(lang) {
  return lang === "hu" ? "hu/" : "";
}

export function viewSeg(view) {
  return view === "daily" ? "daily/" : view === "weekly" ? "weekly/" : "";
}

export function indexHref(token, lang, view) {
  return `/t/${encodeURIComponent(token)}/${langSeg(lang)}${viewSeg(view)}`;
}

export function digestHref(token, lang, view, id) {
  return `/t/${encodeURIComponent(token)}/${langSeg(lang)}${viewSeg(view)}d/${esc(id)}`;
}

// Like indexHref, with the ISO week's URL segment appended (roadmap 3 step
// 2) — week zero-padded to 2 digits ("W05", not "W5") so the address always
// matches the ISO 8601 "Www" shape regardless of week number. Only ever
// called with view="all" today (the daily view has no week address), but
// takes `view` like every other href helper here rather than hardcoding it.
export function weekHref(token, lang, view, year, week) {
  const weekSeg = String(week).padStart(2, "0");
  return `${indexHref(token, lang, view)}w/${esc(year)}-W${esc(weekSeg)}/`;
}

// Like indexHref, but to the standalone search route (roadmap 4 step 7) —
// no `view` parameter: search has no daily/week variant (it spans the whole
// archive, see the file-header comment), so there's no view to select.
export function searchHref(token, lang) {
  return `/t/${encodeURIComponent(token)}/${langSeg(lang)}search`;
}

// Like searchHref, to the arc detail page (§11.1 PR A) — no `view`
// parameter either, same reasoning: a story arc spans every kind, not one
// view (see the file-header comment). `slug` is always route-regex-shaped
// (`[a-z0-9-]{1,64}`, see fetch()) by the time this is ever called, so esc()
// here is the same "free safety, not redundant trust" posture as everywhere
// else in this file rather than a defense against a real threat.
export function arcHref(token, lang, slug) {
  return `/t/${encodeURIComponent(token)}/${langSeg(lang)}a/${esc(slug)}`;
}

// Like searchHref, to the web app manifest (PLAN.md §11.7) — no `view`
// parameter, same reasoning as search/arc/about: an installed app is one
// app over the whole archive, not one per view. It DOES take `lang`,
// because the manifest a page links to is the one whose start_url follows
// that page's language (buildManifest pins `id` across both so this stays
// one installed app, not two).
export function manifestHref(token, lang) {
  return `/t/${encodeURIComponent(token)}/${langSeg(lang)}manifest.webmanifest`;
}

// The service worker's script URL, and therefore — by the Service Worker
// spec's default-scope rule — the registration's SCOPE, which is the token
// root. That is load-bearing rather than incidental: the worker reads
// self.registration.scope to address the site (see src/pwa.js), so the
// capability token reaches it through its own URL and never has to be
// stored, messaged, or put inside a push payload. No `lang`, deliberately:
// a per-language script URL would be a second registration, and therefore a
// second push subscription over one archive.
export function swHref(token) {
  return `/t/${encodeURIComponent(token)}/sw.js`;
}

// Like searchHref, to the about page — no `view` parameter either, same
// reasoning: the about text doesn't belong to one view (see the file-header
// comment).
export function aboutHref(token, lang) {
  return `/t/${encodeURIComponent(token)}/${langSeg(lang)}about`;
}

// `pageKind` ("index" | "digest") picks index vs. digest href — distinct
// from a digest row's own `kind` column (window/daily) used elsewhere.
// `archiveWeek` (roadmap 3 step 3): the CURRENT page's own {year, week} when
// it's an index page rendering an ARCHIVE week, else null — index pages on
// the current week or the (week-less) daily view pass null, and digest
// pages always pass null (a digest has no week address). When set, the
// other-language link must stay on that SAME week's index in the other
// language (weekHref) — falling back to that language's root index would
// silently bounce the reader from the archive week they're reading to the
// current week instead.
export function renderLangSwitcher(token, lang, view, pageKind, id, archiveWeek = null) {
  const otherLangIndexHref = (otherLang) =>
    archiveWeek
      ? weekHref(token, otherLang, view, archiveWeek.year, archiveWeek.week)
      : indexHref(token, otherLang, view);
  // Arc pages (§11.1 PR A, pageKind "arc"): the other-language link targets
  // the SAME arc's page in that language space — `id` doubles as the arc's
  // identity here (arc pages have no numeric id; stable arc keys made this a
  // key-or-slug string, but this call site just echoes it back unchanged),
  // and arcHref takes no `view` (an arc spans every view, see arcHref's own
  // comment), same reasoning as why the "digest" branch below uses
  // digestHref instead of the index href.
  const otherLangHref = (otherLang) => {
    // "plain" = the ledger-less pages (search, about): their language hop
    // targets the other language's root index, exactly like "index" — the
    // deliberate lose-the-query simplification documented at their call
    // sites. The kinds differ only in what renderSettingsPanel gates on them.
    if (pageKind === "index" || pageKind === "plain") return otherLangIndexHref(otherLang);
    if (pageKind === "arc") return arcHref(token, otherLang, id);
    return digestHref(token, otherLang, view, id);
  };
  const enHref = otherLangHref("en");
  const huHref = otherLangHref("hu");
  // Current language: plain bold text, not a link (nothing to switch to).
  // Other language: a link to the SAME page (same index row / same digest
  // id) in the other language space, same view.
  const en = lang === "en" ? "<strong>EN</strong>" : `<a href="${enHref}">EN</a>`;
  const hu = lang === "hu" ? "<strong>HU</strong>" : `<a href="${huHref}">HU</a>`;
  return `<span class="langswitch">${en} | ${hu}</span>`;
}

// The view switcher's "other view" link is ALWAYS an index href, on both
// index and digest pages — see the file-header comment ("Daily-brief view")
// for why a digest page can't link into another view's own digest.
// The view selector is the site's PRIMARY navigation (owner decision) —
// rendered as a pill capsule inside the single header.bar (the desktop top
// bar and the phone bottom dock are the same element, CSS moves it; see
// pageChrome). Active tab = filled accent pill (plain text, not a link);
// inactive = outlined link. On a digest page the inactive tab targets that
// view's INDEX (a window digest has no address in the daily or weekly view
// — long-standing design choice).
// Unlike the language switcher, this deliberately does NOT thread a week
// through (roadmap 3 step 3): a week page's tabs still target the view's
// root index with no week segment — a week page has no daily or weekly
// twin to keep the week address for, so there's nothing to preserve here.
export function renderViewTabs(token, lang, view) {
  const strings = STRINGS[lang];
  // data-view (§11.1 PR C, ⌘K palette): lets the palette script identify
  // which view a tab targets without parsing translated label text — see
  // collectPaletteItems in pageChrome. Purely a JS hook, no visual/no-JS
  // effect; carried on both the active <span> and the linked <a> variants
  // for consistency even though the palette only ever reads it off the
  // linked ones (an active tab has no href to offer).
  const tab = (v, label) =>
    view === v
      ? `<span class="viewtab active" data-view="${v}">${esc(label)}</span>`
      : `<a class="viewtab" data-view="${v}" href="${indexHref(token, lang, v)}">${esc(label)}</a>`;
  return `<nav class="viewtabs" aria-label="${esc(strings.viewsNavLabel)}">${tab("all", strings.viewAll)}${tab("daily", strings.viewDaily)}${tab("weekly", strings.viewWeekly)}</nav>`;
}

// The settings panel (language, theme, text size, density, push, About): a
// native popover (`popover` attribute) opened by the bar's settings button via
// `popovertarget="settings"`, so it opens and light-dismisses with NO JS and
// no <details> bookkeeping. The language links inside keep working for
// no-JS readers exactly as before; theme, size and density are miniseg
// button groups with the hidden-until-JS contract (the client script
// unhides and wires them). The body-font row is gone with the retired
// serif/sans preference. The search control is NOT here any more — it lives
// in the bar's .baractions (see pageChrome), because it is a bar control,
// not a setting.
export function renderSettingsPanel({
  token,
  lang,
  view,
  pageKind,
  id,
  archiveWeek = null,
  appVersion = null,
}) {
  const strings = STRINGS[lang];
  const langRow = `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsLanguage)}</span>${renderLangSwitcher(token, lang, view, pageKind, id, archiveWeek)}</div>`;
  // Theme is now a three-state Light/Auto/Dark miniseg (owner redesign),
  // not the old two-state ◐ toggle — see the theme IIFE in pageChrome for
  // why Auto needs to be a real, distinct state rather than an implied
  // default. Buttons start hidden (progressive enhancement, same contract
  // the old toggle had); the IIFE unhides and wires them.
  const themeRow = `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsTheme)}</span><span class="miniseg miniseg-theme" role="group" aria-label="${esc(strings.themeToggle)}"><button class="minisegbtn" data-set="light" hidden>${esc(strings.themeLight)}</button><button class="minisegbtn" data-set="auto" hidden>${esc(strings.themeAuto)}</button><button class="minisegbtn" data-set="dark" hidden>${esc(strings.themeDark)}</button></span></div>`;
  // Text size: XS/S/M/L miniseg, same shape as theme's above — M is the
  // absence of an override (owner-tuned defaults stay the single source of
  // truth), so only s/l ever get set/stored. Letters are literal, not
  // STRINGS-keyed — "S"/"M"/"L" read the same in both languages.
  const sizeRow = `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsTextSize)}</span><span class="miniseg miniseg-size" role="group" aria-label="${esc(strings.settingsTextSize)}"><button class="minisegbtn" data-set="xs" hidden>XS</button><button class="minisegbtn" data-set="s" hidden>S</button><button class="minisegbtn" data-set="m" hidden>M</button><button class="minisegbtn" data-set="l" hidden>L</button></span></div>`;
  // Density: index pages only — it governs the ledger's .entry
  // padding/clamp, which a digest page has none of, so the row would be a
  // dead control there. "plain" (search/about) rides as its own kind for the
  // language hop and so gets no row either. A miniseg like theme and size
  // (same hidden-until-JS contract); data-set values are the stored
  // preference values, the captions are display only.
  const densityRow =
    pageKind === "index"
      ? `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsDensity)}</span><span class="miniseg miniseg-density" role="group" aria-label="${esc(strings.settingsDensity)}"><button class="minisegbtn" data-set="cards" hidden>${esc(strings.densityCards)}</button><button class="minisegbtn" data-set="compact" hidden>${esc(strings.densityCompact)}</button></span></div>`
      : "";
  // About link (discoverability for friends the owner shares the capability
  // link with): lives in this same settings panel, not a dedicated footer —
  // the site has no <footer> element (chrome is the bar + this popover
  // only, see pageChrome), and this panel is already the one place every
  // page (index, digest, search, arc) renders identically, so one row here
  // makes the link reachable everywhere, not just the index. A single link,
  // no settingslabel column — .settingsrow's flex layout degrades cleanly
  // to one child.
  //
  // Deployed digest-app version, bottom-right of the same row (About stays
  // left via .settingsrow's space-between). `appVersion` is already validated
  // X.Y.Z by latestAppVersion (handlers.js); null renders the row exactly as
  // it was before the label existed, so the version is purely additive.
  const versionHtml =
    appVersion === null
      ? ""
      : `<span class="settingslabel appversion" title="${esc(strings.versionTitle)}">v${esc(appVersion)}</span>`;
  const aboutRow = `<div class="settingsrow"><a href="${aboutHref(token, lang)}">${esc(strings.aboutLabel)}</a>${versionHtml}</div>`;
  // Push notifications (PLAN.md §11.7, PR B). The whole row starts hidden —
  // not just its button, unlike every miniseg above — because "can this
  // browser do Web Push at all" is a question only the client can answer,
  // and there are three distinct no's: no Push API, push not configured on
  // the server, and iOS-but-not-installed. Showing the label with a dead
  // control in any of those would be worse than showing nothing, so the
  // push IIFE unhides the row only once it knows which state applies. The
  // data-* carrier pattern (same as .paletteconfig in pageChrome) hands it
  // the strings so the client script stays lang-agnostic.
  //
  // data-base is the TOKEN ROOT, deliberately NOT indexHref(token, lang) —
  // the push/* endpoints have no "hu/" variant (see the route comment in
  // worker.js: they are machine endpoints, and a subscription's language is
  // a stored field rather than a URL segment). A /hu/ page therefore posts
  // to the same paths every other page does and says which language it is
  // in data-lang, which the client sends in the body. Deriving this from
  // `lang` instead would 404 every subscribe attempt made from a Hungarian
  // page — and only from a Hungarian page, which is exactly the kind of
  // half-broken that survives a careless review.
  const pushRow = `<div class="settingsrow pushrow" hidden
    data-label="${esc(strings.pushLabel)}"
    data-enable="${esc(strings.pushEnable)}"
    data-enabled="${esc(strings.pushEnabled)}"
    data-disable="${esc(strings.pushDisable)}"
    data-blocked="${esc(strings.pushBlocked)}"
    data-ioshint="${esc(strings.pushIosHint)}"
    data-failed="${esc(strings.pushFailed)}"
    data-lang="${esc(lang)}"
    data-base="${esc(indexHref(token, "en", "all"))}"
  ><span class="settingslabel">${esc(strings.pushLabel)}</span><button class="pushtoggle" hidden></button><span class="pushnote" hidden></span></div>`;
  return `<section id="settings" class="settingspanel" popover aria-labelledby="settingstitle"><div class="settingshead"><h2 id="settingstitle" class="settingstitle">${esc(strings.settingsLabel)}</h2><button type="button" class="settingsclose" popovertarget="settings" popovertargetaction="hide" aria-label="${esc(strings.settingsClose)}">×</button></div>${langRow}${themeRow}${sizeRow}${densityRow}${pushRow}${aboutRow}</section>`;
}

// Search popover for the bar's .baractions (index, digest and arc pages —
// the pages that pass showSearch; see pageChrome). A native <details>/
// <summary> popover that opens with no JS, so a no-JS reader still reaches
// the "Search ↗" fallback link inside. It carries the .filter input (hidden
// until the filter IIFE reveals it, and forever on pages with no ledger) and
// the .searchlink no-JS fallback; the filter IIFE and the archive-search
// integration in client.js both `document.querySelector(".filter")`/
// `.searchlink` without scoping to an ancestor, so they find them wherever
// the markup sits. Rendered exactly once per page, by pageChrome.
export function renderSearchBubble(token, lang, strings) {
  // BOTH halves ship, and CSS hides one per breakpoint: the WORD
  // ("SEARCH"/"KERESÉS") where there is room, the bare magnifier where there
  // is not.
  //
  // The icon is an inline SVG rather than a glyph character: U+2315/U+26B2
  // render inconsistently across platforms and the emoji magnifier drags its
  // own colour into a deliberately muted palette. currentColor + the stroke
  // keeps it in the same weight register as the settings icon beside it.
  //
  // aria-label stays on the summary even now that a visible label exists: it
  // is the ONLY accessible name where the label is hidden, and elsewhere it
  // is the same string as the visible text, so the two never disagree.
  // The ⌘K palette's own DOM scrape reads it too.
  const icon = `<svg class="searchicon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10.6 10.6 L14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`;
  // The same magnifier again, inside the panel this time, under its own
  // class: .searchicon is display:none on the desktop (the toggle shows the
  // WORD there — see summary.searchtoggle), and this copy must survive at
  // every breakpoint. Wrapped with the input in .searchfield so the pair can
  // be hidden together: the input ships [hidden] until the filter IIFE
  // reveals it (client.js), and on digest/arc pages it stays hidden
  // forever — a bare icon floating above the archive link in either case
  // would be a dead control. See the .searchpanel:has(.filter[hidden]) rule.
  const fieldIcon = `<svg class="fieldicon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true" focusable="false"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10.6 10.6 L14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`;
  return `<details class="searchpop"><summary class="searchtoggle" aria-label="${esc(strings.searchToggleLabel)}" title="${esc(strings.searchToggleLabel)}">${icon}<span class="searchlabel">${esc(strings.searchToggleLabel)}</span></summary><div class="searchpanel"><div class="searchfield">${fieldIcon}<input class="filter" type="search" placeholder="${esc(strings.filterPlaceholder)}" aria-label="${esc(strings.filterPlaceholder)}" hidden></div><a class="searchlink" href="${searchHref(token, lang)}">${esc(strings.searchLink)}</a></div></details>`;
}

// The bar's plain search link, for pages whose search popover would be a dead
// control (the standalone search and about pages: no ledger to filter, and
// the search page IS the destination). Same magnifier as the popover's
// toggle so the control reads identically wherever it appears.
export function renderSearchButton(token, lang, strings) {
  return `<a class="barsearch" href="${searchHref(token, lang)}" aria-label="${esc(strings.searchToggleLabel)}"><svg class="searchicon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10.6 10.6 L14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></a>`;
}
