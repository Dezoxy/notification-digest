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
    // sites. The kinds differ only in what renderSwitchers gates on them.
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
// rendered as a pill capsule that rides in the masthead row itself, beside
// the brand (owner-requested masthead compaction: this used to be its own
// centered band below the masthead; see pageChrome's .mastleft, which now
// groups the two together, and the .mast/.mastleft/.viewtabs CSS for the
// layout). Active tab = filled accent pill (plain text, not a link);
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
  return `<nav class="viewtabs">${tab("all", strings.viewAll)}${tab("daily", strings.viewDaily)}${tab("weekly", strings.viewWeekly)}</nav>`;
}

// The masthead's top-right cluster (language, theme, size, density)
// collapses into one gear button that opens a floating settings bubble
// (owner redesign) — a native <details>/<summary> disclosure, so the panel
// opens with NO JS. The language links inside keep working for no-JS
// readers exactly as before; the density button keeps its existing class
// and hidden-until-JS contract untouched. Theme and size are miniseg button
// groups (owner upgrade: three-state theme, S/M/L text size) — same
// hidden-until-JS contract, wired by their own IIFEs below in pageChrome.
// The masthead Archive link this row used to also carry (§11.1 PR C,
// `showArchive` param) was removed in the index-cleanup pass — archive weeks
// are reachable via the week rail's own ← link now (see renderWeekRail), so
// there's nothing left to gate a link on here.
// `showSearch` (owner-requested): renders the search control immediately
// LEFT of the settings gear in the masthead. An explicit flag rather than
// `pageKind === "index"` for the same reason the archive link needed one —
// renderSearchPage also passes "index" (for its density row) and must not
// get it. Index pages only, because the popover's `.filter` input drives
// the LEDGER: on a digest or arc page it would be a dead control, and the
// ⌘K palette already carries search everywhere.
export function renderSwitchers(
  token,
  lang,
  view,
  pageKind,
  id,
  archiveWeek = null,
  showSearch = false,
) {
  const strings = STRINGS[lang];
  const langRow = `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsLanguage)}</span>${renderLangSwitcher(token, lang, view, pageKind, id, archiveWeek)}</div>`;
  // Theme is now a three-state Light/Auto/Dark miniseg (owner redesign),
  // not the old two-state ◐ toggle — see the theme IIFE in pageChrome for
  // why Auto needs to be a real, distinct state rather than an implied
  // default. Buttons start hidden (progressive enhancement, same contract
  // the old toggle had); the IIFE unhides and wires them.
  const themeRow = `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsTheme)}</span><span class="miniseg miniseg-theme" role="group" aria-label="${esc(strings.themeToggle)}"><button class="minisegbtn" data-set="light" hidden>${esc(strings.themeLight)}</button><button class="minisegbtn" data-set="auto" hidden>${esc(strings.themeAuto)}</button><button class="minisegbtn" data-set="dark" hidden>${esc(strings.themeDark)}</button></span></div>`;
  // Text size: S/M/L miniseg, same shape as theme's above — M is the
  // absence of an override (owner-tuned defaults stay the single source of
  // truth), so only s/l ever get set/stored. Letters are literal, not
  // STRINGS-keyed — "S"/"M"/"L" read the same in both languages.
  const sizeRow = `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsTextSize)}</span><span class="miniseg miniseg-size" role="group" aria-label="${esc(strings.settingsTextSize)}"><button class="minisegbtn" data-set="s" hidden>S</button><button class="minisegbtn" data-set="m" hidden>M</button><button class="minisegbtn" data-set="l" hidden>L</button></span></div>`;
  // Body-font toggle (owner-requested): Sans (default, = absence of
  // data-font) | Serif (the retired wire-desk prose stack, resurrected
  // behind :root[data-font="serif"] — see the CSS). Same hidden-until-JS
  // contract as every miniseg above.
  const fontRow = `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsFont)}</span><span class="miniseg miniseg-font" role="group" aria-label="${esc(strings.settingsFont)}"><button class="minisegbtn" data-set="sans" hidden>${esc(strings.fontModern)}</button><button class="minisegbtn" data-set="serif" hidden>${esc(strings.fontClassic)}</button></span></div>`;
  // Density toggle (roadmap 4 step 4): index pages only — it governs the
  // ledger's .entry padding/clamp, which a digest page has none of, so the
  // row would be a dead control there.
  // Index only: density compacts the .entry ledger, and the index is the
  // only pageKind that has one — "plain" (search/about) used to ride in as
  // "index" for the lang-hop and got a working toggle that affected nothing.
  const densityRow =
    pageKind === "index"
      ? `<div class="settingsrow"><span class="settingslabel">${esc(strings.settingsDensity)}</span><button class="densitytoggle" aria-label="${esc(strings.densityToggle)}" hidden>▤</button></div>`
      : "";
  // About link (discoverability for friends the owner shares the capability
  // link with): lives in this same settings panel, not a dedicated footer —
  // the site has no <footer> element (chrome is masthead + this popover
  // only, see pageChrome), and this panel is already the one place every
  // page (index, digest, search, arc) renders identically, so one row here
  // makes the link reachable everywhere, not just the index. A single link,
  // no settingslabel column — .settingsrow's flex layout degrades cleanly
  // to one child.
  const aboutRow = `<div class="settingsrow"><a href="${aboutHref(token, lang)}">${esc(strings.aboutLabel)}</a></div>`;
  // The settings trigger is TEXT-ONLY on desktop and ICON-ONLY on the phone
  // (owner follow-up: no gear glyph next to the label; the phone button
  // matches the search icon's size). Both halves live in their own spans so
  // each breakpoint hides one — .gearicon desktop-hidden, .gearlabel
  // phone-hidden (see the CSS) — and the aria-label covers it everywhere.
  const searchHtml = showSearch ? renderSearchBubble(token, lang, strings) : "";
  return `${searchHtml}<details class="settings"><summary class="gear" aria-label="${esc(strings.settingsLabel)}"><span class="gearicon" aria-hidden="true">⚙</span><span class="gearlabel">${esc(strings.settingsLabel)}</span></summary><div class="settingspanel">${langRow}${themeRow}${sizeRow}${fontRow}${densityRow}${aboutRow}</div></details>`;
}

// Search bubble (owner-requested index cleanup): replaces the old
// always-visible filterrow (a standalone "Filter briefings…" input row plus
// a "Search ↗" link) with ONE compact control, matching the masthead's own
// settings-gear disclosure (see renderSwitchers/the .settings*/summary.gear
// CSS pattern, extended by the .searchpop/.searchpanel/summary.searchtoggle
// rules alongside it) — a native <details>/<summary> popover that opens
// with no JS, so a no-JS reader still reaches the "Search ↗" fallback link
// inside. Carries the exact SAME .filter input (same class, same
// hidden-until-JS default, same placeholder) and the exact SAME .searchlink
// no-JS fallback the old filterrow had — only their DOM position moved, so
// the filter IIFE and the archive-search integration in pageChrome's bottom
// script (both `document.querySelector(".filter")`/`.searchlink`, neither
// scoped to a particular ancestor) keep working unchanged. Called from
// renderWeekRail (the ALL view, where it rides as a fourth flex child in the
// week-rail row itself) and directly from renderIndexPage on the daily/
// weekly views, which have no week rail to live in — see that call site.
export function renderSearchBubble(token, lang, strings) {
  // BOTH halves ship, and CSS hides one per breakpoint — the same
  // icon/label half-swap summary.gear already uses (.gearicon/.gearlabel).
  // Desktop shows the WORD ("SEARCH"/"KERESÉS"), matching the SETTINGS chip
  // beside it (owner-requested); the phone shows the bare magnifier, where
  // the masthead has no room for two words. See summary.searchtoggle in the
  // CSS for which half wins where.
  //
  // The icon is an inline SVG rather than a glyph character: U+2315/U+26B2
  // render inconsistently across platforms and the emoji magnifier drags its
  // own colour into a deliberately muted palette. currentColor + the stroke
  // keeps it in the same weight register as the ⚙ gear beside it.
  //
  // aria-label stays on the summary even now that a visible label exists: it
  // is the ONLY accessible name at the phone breakpoint, and on the desktop
  // it is the same string as the visible text, so the two never disagree.
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
