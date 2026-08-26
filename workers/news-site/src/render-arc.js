import { STRINGS } from "./strings.js";
import { CSS } from "./css.js";
import { esc } from "./http.js";
import { formatRelativeTime, formatShortDate, formatTime } from "./dates.js";
import { findArcSectionAnchor } from "./sections.js";
import { digestHref, indexHref, renderSwitchers } from "./hrefs.js";
import {
  computeArcMomentum,
  deltasRenderableIn,
  groupByDay,
  kindBadge,
  renderDayLedger,
  renderDeltaLine,
} from "./render-shared.js";
import { pageChrome } from "./chrome.js";
import { renderIndexEntry } from "./render-index.js";
import { renderArcContext, renderDeltas } from "./render-digest.js";

// One appearance in the arc timeline — reuses the index ledger's own
// `.entry`/`.meta`/`.time`/`.excerpt` vocabulary (same "a link to one
// digest" shape as renderSearchResult) rather than inventing a parallel
// style. `.excerpt` here holds this APPEARANCE's own label for the arc (that
// digest's own heading text, at the time it was published), not a TL;DR —
// deliberately no "TL;DR:" prefix, unlike renderIndexEntry's. Always the
// all-view digest href: an arc spans every kind, so an appearance has no
// daily/weekly-view address of its own, same reasoning as
// renderSearchResult's. `row.anchor` (from findArcSectionAnchor, computed
// once in handleArcPage) is appended as a #sN fragment only when it
// resolved to exactly one section — see that function's comment for the
// fail-safe contract; a null anchor here means a plain link to the digest
// page, never a guessed fragment.
export function renderArcAppearance(row, token, lang) {
  const strings = STRINGS[lang];
  const time = formatTime(new Date(row.created_at), strings.locale);
  // "all": appearances span every kind/view, same reasoning as
  // renderSearchResult's kindBadge call — always show the daily/weekly
  // badge, never suppress it the way a same-kind VIEW page would.
  const badgeHtml = kindBadge(row, "all", strings);
  const href = digestHref(token, lang, "all", row.id) + (row.anchor ? `#${row.anchor}` : "");
  // Delta line (§11.3 delta persistence, ingest v4): `row.delta` is
  // handleArcPage's per-appearance match, already narrowed to this arc's own
  // slug — null on the common case (no delta for this appearance), which
  // renders "", i.e. this appearance looks exactly as it did before this
  // feature. Shares the .deltatext/.deltaprev/.deltaarrow/.deltanow classes
  // with the digest page's own "What changed" block (renderDeltas) — same
  // quiet typographic treatment, no separate label span here since the
  // appearance's own `.excerpt` line right above already names the story.
  // deltasRenderableIn: English-only text, suppressed on /hu/ pages — see
  // that function's comment for why and for the deltas_hu hook.
  const deltaHtml =
    row.delta && deltasRenderableIn(lang)
      ? renderDeltaLine(row.delta.previously, row.delta.now)
      : "";
  return `<a class="entry" href="${href}" data-created="${esc(row.created_at)}">
    <span class="meta"><span class="time">${esc(time)}</span>${badgeHtml}</span>
    <p class="excerpt">${esc(row.label)}</p>
    ${deltaHtml}
  </a>`;
}

// Arc page (§11.1 PR A): `appearances` is handleArcPage's array, ordered
// created_at ASC (oldest first) — first/latest below rely on that order, so
// it's reversed ONLY for the timeline's own display (see the comment at that
// call site). `nowMs` is the request-time instant handleArcPage captured
// once and threads through unchanged, so momentum/relative-time computed
// here can never observe two different "now"s within one render. `identity`
// (stable arc keys) is whatever route segment resolved this page — a `key`
// or, for a pre-key arc, a plain `slug` — carried through opaquely as the
// arc's own address: the data-arc-slug attribute name and the follow-list's
// "slug" vocabulary below are UNCHANGED (client-side string matching, see
// the follow-toggle IIFE in pageChrome), only the value they now carry can
// be a key instead of a slug. `contextMd` (§11.6 context mode) is this arc's
// primer text from arc_context, or null when it has none — see
// renderArcContext for how it renders (or doesn't).
export function renderArcPage(identity, appearances, token, host, lang, nowMs, contextMd) {
  const strings = STRINGS[lang];
  const first = appearances[0];
  const latest = appearances[appearances.length - 1];

  // null on a dormant arc (both 48h windows empty) — the segment is left
  // off the metadata line entirely rather than rendered as a guess; see
  // computeArcMomentum's comment.
  const momentum = computeArcMomentum(appearances, nowMs);
  const momentumSegment =
    momentum === null
      ? null
      : momentum === "up"
        ? `↑ ${strings.arcMomentumUp}`
        : momentum === "down"
          ? `↓ ${strings.arcMomentumDown}`
          : `→ ${strings.arcMomentumSame}`;

  // One mono metadata line, reusing the digest page's own `.stamp` styling
  // (same "wire dateline" register — total appearances, first-seen date,
  // last-updated relative time, momentum when present) rather than a new
  // CSS class.
  const metaLine = [
    (appearances.length === 1 ? strings.arcAppearancesOne : strings.arcAppearances).replace(
      "{n}",
      String(appearances.length),
    ),
    strings.arcFirstSeen.replace(
      "{date}",
      formatShortDate(new Date(first.created_at), strings.locale),
    ),
    strings.arcUpdated.replace(
      "{t}",
      formatRelativeTime(new Date(latest.created_at), strings.locale, nowMs),
    ),
    momentumSegment,
  ]
    .filter(Boolean)
    .join(" · ");

  // Title (H1, the site's first — every other page uses the masthead brand
  // link instead): the arc's MOST RECENT label, not the first — labels can
  // drift across digests as headings get rephrased run to run, and the
  // latest phrasing is the freshest editorial framing of the story.
  const title = latest.label;

  // Timeline, newest-day-first (matches the index ledger's own newest-first
  // convention — groupByDay just buckets consecutive same-day rows in
  // whatever order it's handed, so the DESC order has to come from the
  // caller, same as handleIndexPage's own `created_at DESC` query does for
  // the ledger). `appearances` itself stays ASC (oldest first) throughout
  // this function — only this local copy is reversed, for display only.
  const groups = groupByDay(appearances.slice().reverse(), strings.locale);
  const timelineHtml = renderDayLedger(groups, (row) => renderArcAppearance(row, token, lang));

  // .archead (§11.2, optional "follow list" feature): a flex row wrapping
  // the h1 so the bottom script can inject a text-control follow toggle
  // "next to the arc title" per the spec, without a card/button — see the
  // follow-toggle IIFE in pageChrome. data-arc-slug/data-follow-add/
  // data-follow-remove are the same "data-* carrier" contract as every other
  // client-read attribute in this file (data-created, data-unread-label,
  // paletteconfig, …): the toggle itself is entirely client-side (a
  // localStorage array of followed slugs, never sent here), so this is the
  // only place slug/i18n strings meet the DOM for it to read.
  // Background primer (§11.6 context mode): collapsed disclosure directly
  // under the title/metadata line, above the timeline — see
  // renderArcContext, which renders "" (nothing at all, no empty state) when
  // this arc has no primer.
  const contextHtml = renderArcContext(contextMd, strings);

  const body = `<nav class="digestnav"><a href="${indexHref(token, lang, "all")}">${esc(strings.allDigests)}</a></nav>
<div class="archead"><h1 class="arctitle" data-arc-slug="${esc(identity)}" data-follow-add="${esc(strings.followAdd)}" data-follow-remove="${esc(strings.followRemove)}">${esc(title)}</h1></div>
<p class="stamp">${esc(metaLine)}</p>
${contextHtml}<div class="archivelabel">${esc(strings.arcTimelineLabel)}</div>
${timelineHtml}`;

  // view "all": an arc has no view of its own (see the file-header comment)
  // — the view tabs/brand link just need SOME valid view to target, same
  // reasoning as renderSearchPage's own "all" choice.
  return pageChrome(
    host,
    token,
    lang,
    "all",
    renderSwitchers(token, lang, "all", "arc", identity, null, true),
    body,
    title,
  );
}
