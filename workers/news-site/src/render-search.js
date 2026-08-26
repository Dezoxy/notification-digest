import { STRINGS } from "./strings.js";
import { CSS } from "./css.js";
import { esc } from "./http.js";
import { formatShortDate, formatTime } from "./dates.js";
import { aboutHref, digestHref, renderLangSwitcher, renderSwitchers, searchHref } from "./hrefs.js";
import { kindBadge } from "./render-shared.js";
import { pageChrome } from "./chrome.js";
import { renderExcerpt, renderIndexEntry, renderLeadCard } from "./render-index.js";

// Search (roadmap 4 step 7): snip/snip_hu are excerpts of body_md/body_md_hu
// — untrusted markdown, never HTML (see the file-header comment: only
// body_html/body_html_hu are pre-sanitized and skip esc()). The SQL query in
// handleSearchPage wraps each matched fragment in CHAR(1)/CHAR(2) sentinel
// bytes rather than real <mark> tags SPECIFICALLY so this function can
// escape the whole snippet FIRST — turning any HTML-like text that happens
// to appear in the matched markdown into inert entities — and only THEN
// replace the (esc()-untouched, since esc() doesn't rewrite control bytes)
// sentinels with the real <mark>/</mark> tags. Escape-then-mark, never
// mark-then-escape: doing it the other way round would esc() the <mark>
// tags themselves right back into visible text.
export function markSnippet(rawSnippet) {
  return esc(rawSnippet).replaceAll("\x01", "<mark>").replaceAll("\x02", "</mark>");
}

// HU page: prefer the translated snippet; if it's empty (untranslated
// digest, so body_md_hu was NULL and snippet() returned an empty string) or
// simply absent, fall back to the English snippet and flag it — same
// fallback contract as renderExcerpt's index-ledger "EN" chip, just for
// search results instead of TL;DR excerpts.
export function renderSnippet(row, lang) {
  if (lang === "hu" && row.snip_hu) {
    return { html: markSnippet(row.snip_hu), usedHu: true };
  }
  return { html: markSnippet(row.snip ?? ""), usedHu: false };
}

// One search result: reuses the index ledger's `.entry`/`.meta`/`.excerpt`
// vocabulary (renderIndexEntry) rather than inventing a parallel result
// style — a search hit and a ledger row are the same kind of thing, a link
// to one digest. Always the all-view digest href (digestHref(..., "all",
// ...)): search spans every kind, so a result has no "daily view" address
// of its own to link into, same reasoning as the searchHref/view split
// throughout this feature.
export function renderSearchResult(row, token, lang) {
  const strings = STRINGS[lang];
  const date = new Date(row.created_at);
  const dateLabel = `${formatShortDate(date, strings.locale)} ${formatTime(date, strings.locale)}`;
  // Search results are always in "all"-view address space (see the function
  // comment above), which is also the exact view kindBadge needs to decide
  // the daily-view redundancy rule — reused as-is rather than duplicating
  // the daily/weekly badge logic here.
  const badgeHtml = kindBadge(row, "all", strings);

  const { html: snippetHtml, usedHu } = renderSnippet(row, lang);
  const langChip = lang === "hu" && !usedHu ? '<span class="flag flag-muted">EN</span>' : "";

  // data-id (unified search, owner UX pass): lets the index page's
  // archive-results script drop fragment entries already visible in the
  // rendered ledger above it — see renderIndexEntry/renderLeadCard, which
  // carry the same attribute for exactly this comparison, and the bottom
  // script's archive-search IIFE that reads it.
  return `<a class="entry" href="${digestHref(token, lang, "all", row.id)}" data-id="${esc(row.id)}">
    <span class="meta"><span class="time">${esc(dateLabel)}</span>${badgeHtml}${langChip}</span>
    <p class="excerpt">${snippetHtml}</p>
  </a>`;
}

// Fragment mode (unified search, owner UX pass): what handleSearchPage's
// ?fragment=1 branch returns — bare results only, no pageChrome, no form.
// Built entirely from the SAME server-side renderers as the full search
// page (renderSearchResult -> markSnippet's escape-then-mark, esc()
// everywhere), so every escaping guarantee documented there is inherited
// unchanged; nothing here bypasses it. This is what makes the client's
// innerHTML injection of this fragment (see the bottom script) safe: it is
// our own server-rendered, fully-escaped HTML from the same origin and
// token path. This function must NEVER be handed anything that hasn't gone
// through esc() first — notably, `q` itself is deliberately not echoed back
// into this fragment (unlike the full search page's form, which must echo
// it into the input's value) for exactly that reason.
export function renderSearchFragment(results, token, lang) {
  if (results.length === 0) return "";
  const strings = STRINGS[lang];
  const items = results.map((row) => renderSearchResult(row, token, lang)).join("\n");
  return `<div class="archivelabel">${esc(strings.archiveResults)}</div>\n${items}`;
}

// `results` is null when no search was attempted yet (empty ?q=, see
// handleSearchPage) — renders just the form, no count line, no no-results
// message. Non-null (possibly empty, including the try/catch error-fallback
// case in handleSearchPage) means a search WAS attempted, so the count/
// no-results line always renders.
export function renderSearchPage(results, q, token, host, lang) {
  const strings = STRINGS[lang];

  // No-JS baseline: a plain GET form, submitting back to this exact route
  // with ?q= as the query string — works with JS entirely off. Reuses the
  // `.filter` input's look (see the CSS) but, unlike the index page's own
  // `.filter` input, is NOT hidden: that one is a client-side-only
  // enhancement with "show everything" as its server fallback, while this
  // input IS the server-functional control itself — hiding it would leave
  // no-JS readers with no way to search at all.
  const formHtml = `<form class="searchform" method="get" action="${searchHref(token, lang)}">
    <input class="filter" type="search" name="q" value="${esc(q)}" placeholder="${esc(strings.searchPlaceholder)}" aria-label="${esc(strings.searchPlaceholder)}">
    <button class="searchbtn" type="submit">${esc(strings.searchButton)}</button>
  </form>`;

  let resultsHtml = "";
  if (results !== null) {
    if (results.length === 0) {
      resultsHtml = `<p class="empty">${esc(strings.searchNone)}</p>`;
    } else {
      // Count line reuses the existing `.empty` muted-metadata style — same
      // "borrow the closest existing thing" approach as the rest of this
      // feature, rather than adding a new CSS class for one line of text.
      const countLabel = (
        results.length === 1 ? strings.searchResultsOne : strings.searchResults
      ).replace("{n}", String(results.length));
      const items = results.map((row) => renderSearchResult(row, token, lang)).join("\n");
      resultsHtml = `<p class="empty">${esc(countLabel)}</p>\n${items}`;
    }
  }

  // Switchers: pageKind "plain" (index-style language hop, no ledger) — the
  // language switch on this page goes to the OTHER language's root index,
  // not to that language's own search results for the same query. Losing
  // the query string on a language hop is an accepted, deliberate
  // simplification (threading `q` through renderLangSwitcher's index-href
  // helpers isn't worth it for a corner every other switcher on this site
  // already treats as "go to that language's home").
  //
  // `view` "all": search has no daily/week variant of its own (see the
  // file-header comment and searchHref), so the view tabs/brand link just
  // need SOME valid view to render against, and "all" is the closest
  // meaning — clicking "Daily" from here goes to the daily index, not to a
  // (nonexistent) daily-scoped search.
  return pageChrome(
    host,
    token,
    lang,
    "all",
    renderSwitchers(token, lang, "all", "plain"),
    `${formHtml}${resultsHtml}`,
    strings.searchLabel,
  );
}

// About page: short, static, no D1 data at all — the simplest page this
// file renders. Reuses the site's existing chrome/typography wholesale
// rather than inventing anything new:
//   - pageChrome for the masthead/CSS/settings popover, same as every other
//     page (see renderSearchPage just above for the closest example).
//   - `.digest` (the article-body class the digest pages use for their own
//     prose — see the CSS) for the h2/p typography, including its numbered-
//     section h2::before counter, so "01 What this is" etc. reads as the
//     same voice as a briefing's own section headings.
//   - `.closing` (the digest article's own italic closing-line style) for
//     the honest-caveat paragraph at the end — same register that class
//     already carries elsewhere, just applied here directly since this page
//     has no body_html of its own to style through.
// No new CSS at all, deliberately — every class below already exists.
export function renderAboutPage(token, host, lang) {
  const strings = STRINGS[lang];
  const bodyHtml = `<div class="digest">
    <h2>${esc(strings.aboutWhatTitle)}</h2>
    <p>${esc(strings.aboutWhatBody)}</p>
    <h2>${esc(strings.aboutRhythmTitle)}</h2>
    <p>${esc(strings.aboutRhythmBody)}</p>
    <h2>${esc(strings.aboutReadingTitle)}</h2>
    <p>${esc(strings.aboutReadingBody)}</p>
    <p class="closing">${esc(strings.aboutCaveat)}</p>
  </div>`;
  // Switchers/view: same "all"/"index" pageKind choice as renderSearchPage
  // just above, and for the same reason — the about page has no
  // daily/weekly/week variant of its own (see aboutHref), so the view tabs
  // just need some valid view to render against.
  return pageChrome(
    host,
    token,
    lang,
    "all",
    renderSwitchers(token, lang, "all", "plain"),
    bodyHtml,
    strings.aboutLabel,
  );
}
