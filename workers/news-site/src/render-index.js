import { ARC_IDENTITY_SQL, SOURCE_NAME_RE, arcIdentity } from "./config.js";
import { STRINGS } from "./strings.js";
import { CSS } from "./css.js";
import { esc } from "./http.js";
import {
  budapestDateParts,
  compareIsoWeek,
  formatDayHeader,
  formatRelativeTime,
  formatShortDate,
  formatTime,
  formatWeekRangeLabel,
  isoWeekOf,
  mondayOfIsoWeek,
  tzAbbr,
} from "./dates.js";
import {
  arcHref,
  digestHref,
  indexHref,
  renderSearchBubble,
  renderSwitchers,
  searchHref,
  weekHref,
} from "./hrefs.js";
import { computeArcMomentum, groupByDay, kindBadge, renderDayLedger } from "./render-shared.js";
import { pageChrome } from "./chrome.js";

// Shared TL;DR excerpt logic — HU page: prefer the translated tldr; if the
// app never sent one for this digest, fall back to the English tldr and mark
// it with a muted "EN" chip rather than silently presenting English text as
// if translated. Used by both the compact ledger entries (renderIndexEntry)
// and the lead card (renderLeadCard) so the two never drift apart. `rawText`
// is the SAME string `excerptHtml` was esc()'d from — surfaced unescaped so
// callers can feed it to deriveHeadline (below) without re-decoding HTML
// entities out of the already-escaped copy.
export function renderExcerpt(row, lang) {
  let rawText = row.tldr;
  let excerptHtml = esc(row.tldr);
  let langChip = "";
  if (lang === "hu") {
    if (row.tldr_hu) {
      rawText = row.tldr_hu;
      excerptHtml = esc(row.tldr_hu);
    } else {
      langChip = '<span class="flag flag-muted">EN</span>';
    }
  }
  return { excerptHtml, langChip, rawText };
}

// Front Page redesign: no headline field exists in the stored data — a
// digest carries only a TL;DR paragraph, never a distinct display title —
// so every index card and the digest page's own <h1> (see renderIndexEntry/
// renderLeadCard/renderDigestPage) derive one from it: the first sentence,
// clipped at a WORD boundary to at most 110 characters with a trailing
// ellipsis when clipping was needed. Pure/no I/O, so the smoke script
// exercises it directly with unit-style assertions rather than only via a
// rendered page. `tldr` is the raw (unescaped) TL;DR text — esc() happens at
// the call site once the headline is inserted into HTML, same convention as
// every other derived string in this file.
export function deriveHeadline(tldr) {
  if (!tldr) return "";
  const text = tldr.trim();
  if (!text) return "";
  // First sentence: up to and including the first ./!/? that actually ENDS
  // a sentence — i.e. is followed by whitespace or the end of the string.
  // The lookahead is what keeps decimal numbers intact: real TL;DRs lead
  // with things like "A magnitude 7.4 earthquake struck Colombia…", and a
  // bare [^.!?]*[.!?] match would cut the headline off at "A magnitude 7."
  // (live data, digest #105). Lazy .*? finds the EARLIEST qualifying end.
  const sentenceMatch = text.match(/^.*?[.!?](?=\s|$)/s);
  const sentence = (sentenceMatch ? sentenceMatch[0] : text).trim();
  if (sentence.length <= 110) return sentence;
  let clipped = sentence.slice(0, 110);
  const lastSpace = clipped.lastIndexOf(" ");
  // Only back off to the word boundary when one actually exists inside the
  // clip — a single 110+ character "word" (unusual, but not impossible)
  // clips at the raw character limit rather than not clipping at all.
  if (lastSpace > 0) clipped = clipped.slice(0, lastSpace);
  return `${clipped.trim()}…`;
}

// Source-spectrum palette (roadmap 2 step 8): a STABLE per-source hue for
// the five collectors the digest app currently has, muted so the bar reads
// as metadata rather than a call to action — distinct hues so sources stay
// tellable apart, not a sequential/brand ramp. One palette for both light
// and dark themes; a softened dark-mode variant isn't worth the complexity
// for a 3.2em bar (see the CSS block below). Unknown source names (the app
// ships a new collector before this map is updated — deliberately allowed,
// see SOURCE_NAME_RE's comment) fall back to a neutral gray rather than
// erroring or being dropped from the bar.
export const SOURCE_COLORS = {
  telegram: "#4f8fd9",
  x: "#8a8f9e",
  news: "#c58f5a",
  polymarket: "#7a5ad9",
  reddit: "#d95a4f",
};

export const SOURCE_COLOR_FALLBACK = "#9aa0ab";

// Degraded-run badge (roadmap 2 step 8): shown when failed_sources parses to
// a non-empty array — fail-safe JSON.parse contract (unparseable or
// wrong-shaped -> treated as absent, never thrown), same posture
// renderSourceKey below and this file's other D1-JSON-column readers all
// share, defending against a stored value that predates a validation
// change. Reuses the existing .flag pill shape, but the muted .flag-muted
// colors rather than the amber attention ones — this is a fact about a
// collection run, not something that needs the reader's attention the way
// has_attention does — so the ⚠ prefix, not color, is what marks it.
// `strings` is the caller's STRINGS[lang] (for the localized
// "partial"/"hiányos" label); the failed source names themselves stay
// untranslated in the title, same as source_counts' names in
// renderSourceKey.
export function renderDegradedBadge(failedSourcesJson, strings) {
  if (!failedSourcesJson) return "";
  let names;
  try {
    names = JSON.parse(failedSourcesJson);
  } catch {
    return "";
  }
  if (!Array.isArray(names) || names.length === 0) return "";
  const title = names.join(", ");
  return `<span class="flag flag-degraded" title="${esc(title)}">⚠ ${esc(strings.degraded)}</span>`;
}

// Source key (digest-page colophon, roadmap 2 step 8 follow-up): concrete
// per-source numbers with color swatches, plus any failed sources from a
// partial run. Same fail-safe JSON.parse contract as renderDegradedBadge
// above (unparseable or wrong-shaped -> treated as absent, never thrown);
// renders nothing at all when both fields are absent/empty, so an old
// digest predating this data shows no key. (The index page's own micro-bar
// counterpart, .spectrum/renderSpectrum, was removed in the owner-requested
// index-cleanup pass; this digest-page colophon is unaffected.)
export function renderSourceKey(sourceCountsJson, failedSourcesJson, strings) {
  let counts = null;
  if (sourceCountsJson) {
    try {
      const parsed = JSON.parse(sourceCountsJson);
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
        counts = parsed;
      }
    } catch {
      // unparseable -> treat as absent
    }
  }
  let failed = null;
  if (failedSourcesJson) {
    try {
      const parsed = JSON.parse(failedSourcesJson);
      if (Array.isArray(parsed) && parsed.length > 0) failed = parsed;
    } catch {
      // unparseable -> treat as absent
    }
  }

  // Descending by count — reads as a ranked list, highest-volume source first.
  const countEntries = counts
    ? Object.entries(counts)
        .filter(([, n]) => typeof n === "number" && n > 0)
        .sort((a, b) => b[1] - a[1])
    : [];

  if (countEntries.length === 0 && !failed) return "";

  const countSpans = countEntries
    .map(
      ([name, n]) =>
        `<span class="sk"><i style="background:${SOURCE_COLORS[name] ?? SOURCE_COLOR_FALLBACK}"></i>${esc(name)} ${esc(n)}</span>`,
    )
    .join("");
  // No count next to a failed source — it failed, nothing to count; the ⚠
  // is the marker, same "no new color" decision as renderDegradedBadge.
  const failedSpans = failed
    ? failed.map((name) => `<span class="sk sk-failed">⚠ ${esc(name)}</span>`).join("")
    : "";

  return `<div class="sourcekey"><span class="sklabel">${esc(strings.sourcesLabel)}</span>${countSpans}${failedSpans}</div>`;
}

// Model display names (model-display-names pass): the provenance row below
// used to show the raw recorded model id verbatim ("claude-opus-5",
// "openai/gpt-5.6-terra") — accurate, but not something a reader parses at a
// glance. This maps a raw id to a short human name; the raw id itself is
// never dropped, it just moves to the chip's `title` attribute (see
// renderProvenance below) so it's one hover away. Pure and total: every
// input, including ones this map has never seen, produces SOME string —
// never throws, never returns blank. Order matters — first matching rule
// wins:
//   1. non-string (or empty string) -> returned unchanged, fail-safe for a
//      caller that skips its own guard.
//   2. provider prefix ("openai/", "z-ai/", …) stripped: everything up to
//      and including the LAST "/".
//   3. Claude family (claude-opus-5, claude-sonnet-4-6, claude-haiku-4-5,
//      …) -> "<Family> <version>", version's internal "-" joined as ".".
//   4. the historical bare alias "sonnet" -> "Sonnet 5" — see the dedicated
//      comment on that branch below for why this one alias is safe to map.
//   5. GPT ids (gpt-5, gpt-5.6-sol, …) -> "GPT-<version>[ <Capitalized
//      suffix words>]".
//   6. GLM ids (glm-5.3) -> "GLM-<version>".
//   7. anything else (an id this map has never seen, or a hostile string) ->
//      the ORIGINAL id, untouched — never blank, never mangled, and safe
//      because the caller esc()'s whatever this returns before it reaches
//      the page.
export function modelDisplayName(id) {
  if (typeof id !== "string" || id.length === 0) return id;

  const lastSlash = id.lastIndexOf("/");
  const stripped = lastSlash === -1 ? id : id.slice(lastSlash + 1);

  const claudeMatch = stripped.match(/^claude-(opus|sonnet|haiku)-(\d+(?:-\d+)*)$/);
  if (claudeMatch) {
    const family = claudeMatch[1].charAt(0).toUpperCase() + claudeMatch[1].slice(1);
    const version = claudeMatch[2].split("-").join(".");
    return `${family} ${version}`;
  }

  // Historical alias: model provenance only started being recorded on
  // 2026-09-04, and back then the app pinned the CLI alias "sonnet" rather
  // than an explicit dated id — every row carrying this bare alias falls in
  // the Sonnet 5 era (translate_model_fallback's own comment in
  // digest/config.py contrasts this alias's Sonnet 5 against the older
  // 4.6-era pin). The app now records "claude-sonnet-5" directly instead of
  // the alias, so this mapping only ever serves existing historical rows and
  // will never need to grow a new one. Deliberately NOT extended to "opus"
  // or "haiku" bare aliases — no such rows exist, and guessing a mapping for
  // an alias that was never actually recorded would just go stale.
  if (stripped === "sonnet") return "Sonnet 5";

  const gptMatch = stripped.match(/^gpt-(\d+(?:\.\d+)*)(?:-(.+))?$/);
  if (gptMatch) {
    const [, version, suffix] = gptMatch;
    if (!suffix) return `GPT-${version}`;
    const words = suffix
      .split("-")
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ");
    return `GPT-${version} ${words}`;
  }

  const glmMatch = stripped.match(/^glm-(\d+(?:\.\d+)*)$/);
  if (glmMatch) return `GLM-${glmMatch[1]}`;

  return id;
}

// Model provenance ("Written by"/"Translated with" byline, PLAN.md
// OpenRouter-fallback work): a SECOND digest-page colophon row, directly
// below the source key above — which model wrote this brief, and (when the
// digest was also translated) which model translated it, plus whether an
// OpenRouter fallback model served in place of the primary Claude call for
// that leg. Same fail-safe JSON.parse contract as renderSourceKey above:
// unparseable, wrong-shaped, or an unrecognizable leg -> that leg (or the
// whole row) renders nothing rather than throwing, so an old digest
// predating this data (or a stored value from before a future validation
// change) still renders cleanly. `summarize` is always present when
// `provenance` parses at all (app contract); `translate` only shows up on a
// digest that also got a Hungarian translation — see validateProvenance in
// src/ingest.js for the shape this trusts but re-checks structurally
// anyway.
//
// Two labelled lines (this feature, replacing the old single glyph-prefixed
// row): each leg gets its own line — "Written by" for summarize,
// "Translated with" for translate — instead of the old ✎/⇄ directional
// glyphs. The label now carries the meaning those glyphs used to (which leg
// this is), so they're gone; the in-chip "HU" marker on the translate leg
// is gone too, for the same reason — "Translated with" already says this is
// the translation line, and a bare language code next to it was redundant.
// ↻ stays: it doesn't say WHICH leg this is, it says WHAT HAPPENED — an
// OpenRouter fallback model served instead of the primary Claude call for
// that leg — a fact the label can't carry, so it keeps its own marker,
// shown only when that leg's `fallback === true`. See .provline/.provenance
// in the CSS for the two-line grid layout this markup lays out into.
//
// Model display names (model-display-names pass): the visible chip text is
// now a short human name (modelDisplayName(leg.model)) rather than the raw
// recorded id — "Opus 5" instead of "claude-opus-5", "GPT-5.6 Terra"
// instead of "openai/gpt-5.6-terra". The raw id isn't lost: it lands on the
// chip's `title` attribute, one hover away. Both the display name and the
// raw-id title go through esc() — the model id is stored data, not markup,
// same trust posture as every other D1-sourced string this file inserts.
export function renderProvenance(provenanceJson, strings) {
  if (!provenanceJson) return "";
  let parsed;
  try {
    parsed = JSON.parse(provenanceJson);
  } catch {
    return "";
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return "";

  // One leg's line: a label span plus a chip span. A fallback prefixes the
  // chip with "↻ " and gets the muted .sk-fallback class — same "no new
  // color, the glyph is the marker" posture renderSourceKey's .sk-failed
  // pills already established for a failed source; a leg that did NOT fall
  // back gets no glyph at all, just "model · effort".
  const legLine = (leg, label) => {
    if (
      typeof leg !== "object" ||
      leg === null ||
      Array.isArray(leg) ||
      typeof leg.model !== "string" ||
      typeof leg.effort !== "string"
    ) {
      return "";
    }
    const fallback = leg.fallback === true;
    const prefix = fallback ? "↻ " : "";
    const cls = fallback ? "sk sk-fallback" : "sk";
    const displayName = modelDisplayName(leg.model);
    return `<div class="provline"><span class="sklabel">${esc(label)}</span><span class="${cls}" title="${esc(leg.model)}">${prefix}${esc(displayName)} · ${esc(leg.effort)}</span></div>`;
  };

  const summarizeHtml = legLine(parsed.summarize, strings.provenanceLabel);
  const translateHtml = parsed.translate
    ? legLine(parsed.translate, strings.provenanceTranslateLabel)
    : "";

  if (!summarizeHtml && !translateHtml) return "";

  return `<div class="provenance">${summarizeHtml}${translateHtml}</div>`;
}

export function renderIndexEntry(row, token, lang, view) {
  const strings = STRINGS[lang];
  const created = new Date(row.created_at);
  // The daily and weekly views drop their day headers (see renderIndexPage)
  // so their cards can flow two-across instead of one-per-row against an
  // empty second column. The day those headers carried has to survive that,
  // so it moves INTO the card here -- "Mon 24 Aug · 20:43" instead of a bare
  // "20:43". The all view keeps time only: its cards still sit under a day
  // header, and repeating the date on every one of a day's ~8 window
  // digests would be noise.
  const time =
    view === "all"
      ? formatTime(created, strings.locale)
      : `${formatShortDate(created, strings.locale)} · ${formatTime(created, strings.locale)}`;
  // Weekly gets the same accent/clamp treatment as daily — both are a
  // synthesis, just a different window — so this checks "not a plain
  // window digest" rather than "is daily" specifically; see kindBadge for
  // the badge itself.
  const isSynthesis = row.kind !== "window";
  const badgeHtml = kindBadge(row, view, strings);

  const { excerptHtml, langChip, rawText } = renderExcerpt(row, lang);
  // Front Page redesign: a mechanically-derived display headline, see
  // deriveHeadline — every grid card gets one, in place of the old bare
  // time+count meta row being the card's only "title".
  const headline = deriveHeadline(rawText);

  const counts = `${esc(row.item_count)} ${esc(strings.itemsWord)} · ${esc(row.section_count)} ${esc(strings.sectionsWord)}`;
  const timeClass = isSynthesis ? "time time-accent" : "time";
  const excerptClass = isSynthesis ? "excerpt excerpt-daily" : "excerpt";

  // Degraded-run badge (roadmap 2 step 8): renders "" when the row has no
  // failed_sources data (older digests, or an app version that doesn't send
  // it yet) — see renderDegradedBadge. The source-spectrum micro-bar this
  // used to render alongside (renderSpectrum) was removed from the index in
  // the owner-requested index-cleanup pass — the ledger's own recency
  // already communicates what the bar did; the digest page's own
  // renderSourceKey colophon (unaffected by this pass) still carries that
  // provenance in full.
  const degradedHtml = renderDegradedBadge(row.failed_sources, strings);

  // data-created (roadmap 2 step 2, unread fence): the row's own created_at,
  // straight from D1 as an ISO UTC string — lexicographically comparable
  // without parsing, the same trick get_recent_digests (digest repo) relies
  // on. esc()'d like every other D1-sourced value inserted as an attribute.
  //
  // data-id (unified search, owner UX pass): same contract as
  // renderSearchResult's — see comments there. Lets the archive-results
  // script tell "already in this ledger" from "genuinely archive-only".
  return `<a class="entry" href="${digestHref(token, lang, view, row.id)}" data-created="${esc(row.created_at)}" data-id="${esc(row.id)}">
    <span class="meta"><span class="${timeClass}">${esc(time)}</span><span class="count">${counts}</span>${degradedHtml}${badgeHtml}${langChip}</span>
    <h3 class="headline">${esc(headline)}</h3>
    <p class="${excerptClass}"><strong>${esc(strings.tldrLabel)}</strong> ${excerptHtml}</p>
  </a>`;
}

// The lead card (roadmap step 4): the newest digest in the current view,
// rendered full-weight above the compact ledger — the reader's most common
// task is "read the newest one". Structurally still one big clickable
// `.entry` <a>, same pattern as renderIndexEntry, but the usual time+count
// `.meta` row is replaced by a mono dateline eyebrow (same flex layout,
// class="meta" reused) and the excerpt runs unclamped at a slightly larger
// size (see the .entry-lead CSS).
export function renderLeadCard(row, token, lang, view) {
  const strings = STRINGS[lang];
  const date = new Date(row.created_at);
  // See kindBadge — same daily/weekly badge as renderIndexEntry's, so the
  // two never drift apart building it separately.
  const badgeHtml = kindBadge(row, view, strings);

  const { excerptHtml, langChip, rawText } = renderExcerpt(row, lang);
  // Front Page redesign: derived hero headline — see deriveHeadline.
  const headline = deriveHeadline(rawText);

  const eyebrow = `${strings.latest} · ${formatShortDate(date, strings.locale)} · ${formatTime(date, strings.locale)} ${tzAbbr(date)} · ${row.item_count} ${strings.itemsWord}`;

  // Degraded-run badge: same contract as renderIndexEntry's — see comments
  // there (including why there's no source-spectrum bar alongside it here).
  const degradedHtml = renderDegradedBadge(row.failed_sources, strings);

  // Facts column (Front Page redesign, "if cheap" per spec): items/sections
  // are already-selected row columns (see handleIndexPage's SELECT), so this
  // is free — no extra query. Reuses itemsWord/sectionsWord (already
  // localized) as the <dt> labels rather than minting new strings for them.
  const factsHtml = `<dl class="leadfacts">
    <dt>${esc(strings.itemsWord)}</dt><dd>${esc(row.item_count)}</dd>
    <dt>${esc(strings.sectionsWord)}</dt><dd>${esc(row.section_count)}</dd>
  </dl>`;

  // data-created / data-id: same contract as renderIndexEntry's — see
  // comments there.
  return `<a class="entry entry-lead" href="${digestHref(token, lang, view, row.id)}" data-created="${esc(row.created_at)}" data-id="${esc(row.id)}">
    <div class="leadmain">
      <span class="meta"><span class="eyebrow-text">${esc(eyebrow)}</span>${degradedHtml}${badgeHtml}${langChip}</span>
      <h2 class="headline headline-lead">${esc(headline)}</h2>
      <p class="excerpt"><strong>${esc(strings.tldrLabel)}</strong> ${excerptHtml}</p>
    </div>
    ${factsHtml}
  </a>`;
}

// Week rail (roadmap 3 step 2): mono wire-style `← W31 · WEEK 32 · 3–9 AUG ·
// W33 →` nav, rendered on ALL-view index pages only — see the call site in
// renderIndexPage, and the file-header roadmap notes on why the daily view
// has no week address. `weekInfo` is handleIndexPage's { year, week,
// isCurrentWeek, older, newer } (older/newer are {year,week} or null — see
// that function). Absent older/newer render as empty (but still flex:1)
// spacer spans, via the shared .rail-older/.rail-newer classes, so the
// center label stays visually centered either way (see the .weekrail CSS).
// The search bubble (see renderSearchBubble just above) rides as a fourth,
// non-growing flex child after rail-newer — the two flex:1 spacer spans
// still grow to equal widths regardless, so the center label stays centered
// between them; the search control just sits further right, clear of the
// rail-newer "→" link on an archive week (§11 index-cleanup spec: "must not
// collide with the rail-newer link").
//
// Archive sparkline (roadmap 4 step 6): `rows` is null on every page except
// an archive week (see the call site in renderIndexPage, which passes null
// on the current week so its rail stays byte-identical to before this
// step).
export function renderWeekRail(token, lang, weekInfo, strings, rows = null) {
  const olderLink = weekInfo.older
    ? `<a href="${weekHref(token, lang, "all", weekInfo.older.year, weekInfo.older.week)}">← W${esc(String(weekInfo.older.week).padStart(2, "0"))}</a>`
    : "";

  // The CURRENT week's own "newer" target is, by definition, the current
  // week itself — recomputed here (isoWeekOf is a cheap pure function)
  // rather than threaded through weekInfo, so weekInfo stays a plain
  // description of THIS page's own week. When the newer target IS the
  // current week, link to the ROOT index instead of a w/YYYY-Www/ address
  // for it — one canonical URL for the current week, not two addresses for
  // the same page.
  const current = isoWeekOf(new Date());
  let newerLink = "";
  if (weekInfo.newer) {
    const newerHref =
      compareIsoWeek(weekInfo.newer, current) === 0
        ? indexHref(token, lang, "all")
        : weekHref(token, lang, "all", weekInfo.newer.year, weekInfo.newer.week);
    newerLink = `<a href="${newerHref}">W${esc(String(weekInfo.newer.week).padStart(2, "0"))} →</a>`;
  }

  const centerLabel = strings.weekLabel
    .replace("{w}", String(weekInfo.week))
    .replace("{range}", formatWeekRangeLabel(weekInfo.year, weekInfo.week, strings.locale));

  // Archive sparkline (roadmap 4 step 6): seven per-day micro-bars for THIS
  // week only, built when (and only when) the caller handed us rows — see
  // the function comment above. Window digests only (kind === "window" — a
  // daily brief re-synthesizes the same day's items, so counting it too
  // would double the day), summed by budapestDateParts key, then read back
  // per day of the week's own Mon..Sun span off a FRESH per-day proxy copy
  // each iteration —
  // mondayOfIsoWeek's proxy must never be mutated in place across
  // iterations, or every day would collapse onto the same Monday.
  let sparkHtml = "";
  if (Array.isArray(rows)) {
    const daySums = new Map();
    for (const row of rows) {
      if (row.kind !== "window") continue;
      const { y, m, d } = budapestDateParts(new Date(row.created_at));
      const key = `${y}-${m}-${d}`;
      daySums.set(key, (daySums.get(key) ?? 0) + row.item_count);
    }

    const monday = mondayOfIsoWeek(weekInfo.year, weekInfo.week);
    const days = [];
    for (let offset = 0; offset < 7; offset += 1) {
      const day = new Date(monday);
      day.setUTCDate(day.getUTCDate() + offset);
      days.push(day);
    }
    const counts = days.map((day) => {
      const { y, m, d } = budapestDateParts(day);
      return daySums.get(`${y}-${m}-${d}`) ?? 0;
    });

    // Normalized against the WEEK'S OWN max day-sum — this bar cluster only
    // ever shows one week at a time, so there's no wider window to be
    // comparable against. An empty week (every day zero) renders no sparkline
    // at all rather than seven identical hairline bars — noise, not signal.
    const max = Math.max(0, ...counts);
    if (max > 0) {
      const bars = days
        .map((day, i) => {
          const count = counts[i];
          const title = `${formatShortDate(day, strings.locale)} · ${count} ${strings.itemsWord}`;
          // Zero-count days still render a bar (class "sd0", no inline
          // height — .railspark's CSS gives sd0 a fixed 15% so there's no
          // specificity fight with the inline height below) so the week
          // always reads as seven days, not a gappy row.
          if (count === 0) {
            return `<i class="sd0" title="${esc(title)}"></i>`;
          }
          const pct = Math.max(15, Math.round((count / max) * 100));
          return `<i style="height:${pct}%" title="${esc(title)}"></i>`;
        })
        .join("\n");
      sparkHtml = `<span class="railspark" aria-hidden="true">${bars}</span>`;
    }
  }

  return `<nav class="weekrail" aria-label="${esc(strings.weekRailLabel)}"><span class="rail-older">${olderLink}</span><span class="rail-center">${esc(centerLabel)}${sparkHtml}</span><span class="rail-newer">${newerLink}</span></nav>`;
}

// NOW section ranking (§11.1 PR B, "decide + implement the NOW ranking
// rule"): pure aggregation over handleIndexPage's now-arcs query rows —
// (identity, label, created_at, id) for every topic appearance in the
// trailing 7 days, one row per digest×topic. The query deliberately leaves
// aggregation to JS instead of GROUP BY + window functions (see its own
// comment in handleIndexPage) specifically so "label of the latest
// appearance" is a plain forward scan, not a second query or a window-
// function fight — bounded input (<=12 topics x ~80 digests/week is a few
// hundred rows) makes that scan cheap on every current-week-all-view
// render.
//
// Grouped by arc IDENTITY (stable arc keys, see arcIdentity/ARC_IDENTITY_SQL)
// rather than raw slug — this is the actual bug fix: a story recurring
// under several differently-worded headings used to fragment into several
// 1-appearance, never-eligible "arcs", one per distinct slug; grouping by
// identity clusters every keyed appearance into the SAME arc regardless of
// how its heading was worded that run. A pre-key story (no digest carries a
// `key` for it yet) groups exactly as before — its identity is still its
// slug — so this is additive, not a behavior change for existing data.
//
// Eligibility mirrors renderArcs' own "recurring" threshold (count >= 2) —
// a single appearance in the window is a mention, not an arc. Ranking is
// appearances in the trailing 72h (desc), then most recent appearance
// (desc), then identity (asc) as the deterministic tiebreak two arcs can
// otherwise share on both count and recency.
//
// Momentum reuses computeArcMomentum VERBATIM (§11.1 PR A) on each arc's
// own appearances-in-window — same 48h/96h-vs-`nowMs` computation an arc
// page itself uses, just fed a different (still ASC-ordered-by-construction)
// appearances array. Its null/dormant case is expected to be rare here (an
// arc scoring >0 in the trailing 72h always has recent activity) but NOT
// impossible: an eligible arc can still rank into the top 5 by recency
// alone with zero 72h appearances and both its 48h/96h windows empty (e.g.
// its two appearances both fall between 4 and 7 days ago) — renderNowSection
// handles that by simply omitting the arrow, the same fail-safe contract
// renderArcPage's own momentumSegment already uses.
export function computeNowArcs(rows, nowMs) {
  const trailing72hStart = nowMs - 72 * 3600000;
  const byIdentity = new Map();
  for (const row of rows) {
    if (!row.identity) continue;
    let arc = byIdentity.get(row.identity);
    if (!arc) {
      arc = { identity: row.identity, label: row.label, appearances: [], recent72h: 0 };
      byIdentity.set(row.identity, arc);
    }
    // rows arrive created_at ASC (see the query's ORDER BY in
    // handleIndexPage) — each successive row's label overwrites the last,
    // so by the time the scan finishes `label` holds the MOST RECENT
    // appearance's label, matching renderArcPage's own "latest label wins"
    // choice (see the comment on its `title` there).
    arc.label = row.label;
    arc.appearances.push({ created_at: row.created_at });
    if (new Date(row.created_at).getTime() >= trailing72hStart) arc.recent72h += 1;
  }

  const eligible = Array.from(byIdentity.values()).filter((arc) => arc.appearances.length >= 2);

  eligible.sort((a, b) => {
    if (b.recent72h !== a.recent72h) return b.recent72h - a.recent72h;
    const aLast = a.appearances[a.appearances.length - 1].created_at;
    const bLast = b.appearances[b.appearances.length - 1].created_at;
    if (aLast !== bLast) return aLast > bLast ? -1 : 1;
    return a.identity < b.identity ? -1 : a.identity > b.identity ? 1 : 0;
  });

  return eligible.slice(0, 5).map((arc) => ({
    identity: arc.identity,
    label: arc.label,
    count: arc.appearances.length,
    lastSeen: arc.appearances[arc.appearances.length - 1].created_at,
    momentum: computeArcMomentum(arc.appearances, nowMs),
  }));
}

// Day grouping is an ALL-view affordance, not a universal one. That view
// yields ~8 window digests a day, so a day header is a real divider between
// dense runs of cards. The daily view yields ONE brief per day and the
// weekly view one per week, so a header before every single card turned the
// two-column grid into one card beside a permanently empty cell -- the
// owner-reported "the text has just half the width", originally answered by
// dropping those views to a single column (see section[data-ledger] in
// css.js). Dropping the headers instead lets the cards flow two-across and
// actually fill the grid; each card carries its own date now, see
// renderIndexEntry.
export function renderLedgerFor(view, rows, locale, renderRow) {
  if (view !== "all") return rows.map(renderRow).join("\n");
  return renderDayLedger(groupByDay(rows, locale), renderRow);
}

// NOW section (§11.1 PR B): the situational-overview block rendered at the
// top of renderIndexPage, above the ledger — see handleIndexPage for the
// current-week-all-view-only gate that decides whether `nowArcs` is ever
// non-empty. Absent entirely when nowArcs is empty (0 eligible arcs -> no
// section markup at all, not an empty-state message — the ledger below
// already covers "nothing to show").
//
// Each arc renders as ONE row, not a card (design guidance: no card soup) —
// a single link carrying the momentum arrow, the arc's own label
// (arcHref, §11.1 PR A), and a mono metadata tail. The metadata tail is the
// relative last-updated time ONLY (owner-requested index cleanup, dropped
// the "×{n} this week" appearance count this row used to lead with) —
// arcRepeat itself is untouched and stays in active use on the digest
// page's own arc chips (see renderArcs), this is just NOW's own metadata
// getting quieter. Reuses .archivelabel for the eyebrow, same mono-eyebrow
// recipe already shared by the arc timeline and archive-search labels (see
// there) rather than a fourth near-identical class.
// `searchHtml` (owner-requested placement): the search control rides in
// THIS section's head row, right-aligned — the same right edge the arc
// rows' own relative-time meta aligns to, so the icon reads as belonging
// to the top of the page rather than to the week rail below it. Empty
// string on any page where the caller placed the control elsewhere (see
// renderIndexPage: the week rail keeps it whenever this section is
// absent, so the control never disappears with the section).
export function renderNowSection(nowArcs, strings, token, lang, nowMs) {
  if (!nowArcs || nowArcs.length === 0) return "";
  const rowsHtml = nowArcs
    .map((arc) => {
      // Defense in depth (see computeNowArcs's comment on momentum): omit
      // the arrow entirely on the null/dormant case rather than guessing —
      // same fail-safe contract as renderArcPage's own momentumSegment.
      const arrow =
        arc.momentum === "up"
          ? "↑"
          : arc.momentum === "down"
            ? "↓"
            : arc.momentum === "same"
              ? "→"
              : "";
      const meta = formatRelativeTime(new Date(arc.lastSeen), strings.locale, nowMs);
      // data-arc-slug / data-last-seen (§11.2): rendering attributes, not
      // server state — the catch-up banner's client script (the unread-fence
      // IIFE extension in pageChrome) reads these to compute M (arcs updated
      // since last visit) and to cross-reference the follow list's localStorage
      // slugs, the exact same "data-* carrier" contract data-created already
      // uses for .entry rows. The attribute NAME stays data-arc-slug (client
      // script/follow-list vocabulary, unchanged) but its VALUE is now arc
      // IDENTITY (arc.identity — key when present, else slug), the same value
      // arcHref links to and renderArcPage's own data-arc-slug carries — so a
      // NOW row and its arc page always agree on the follow-list's matching
      // key. arc.lastSeen is the same ISO UTC string used above for the
      // relative-time meta, so the lexicographic compare against lastVisit is
      // correct for the same reason data-created's is.
      return `<a class="nowrow" href="${arcHref(token, lang, arc.identity)}" data-arc-slug="${esc(arc.identity)}" data-last-seen="${esc(arc.lastSeen)}">${arrow ? `<span class="nowarrow" aria-hidden="true">${arrow}</span>` : ""}<span class="nowarclabel">${esc(arc.label)}</span><span class="nowmeta">${esc(meta)}</span></a>`;
    })
    .join("\n");
  return `<div class="now"><div class="archivelabel">${esc(strings.nowLabel)}</div><nav class="nowlist" aria-label="${esc(strings.nowLabel)}">${rowsHtml}</nav></div>\n`;
}

// Big masthead issue line (Front Page redesign, index pages only): "No.
// {n} · {full date} · {n} editions today" — every piece is derived from
// data the page already has, no new plumbing (per the task spec):
//   - edition number: the lead digest's own `id` (an existing, already-
//     unique, monotonically-issued column — not invented for this).
//   - date: formatDayHeader on the lead's created_at, same formatter the
//     digest page's own eyebrow used before this redesign.
//   - editions today: how many of the ALREADY-FETCHED `rows` (the same
//     array renderIndexPage received — the current week for the all view,
//     up to 1000 rows for daily/weekly) share the lead's Budapest calendar
//     day. ALL view only — a daily/weekly view's "editions" are, by
//     definition, at most one a day, so the count would only ever read 1
//     and add nothing.
// Returns "" when there's no lead row at all (empty index, or an archive
// week — see renderIndexPage's own isCurrent gate), same "absent data
// renders as absence" contract as this file's other optional-fragment
// helpers.
export function buildIssueLine(leadRow, rows, view, strings) {
  if (!leadRow) return "";
  const parts = [
    strings.issueEdition.replace("{n}", String(leadRow.id)),
    formatDayHeader(new Date(leadRow.created_at), strings.locale),
  ];
  if (view === "all") {
    const leadDay = budapestDateParts(new Date(leadRow.created_at));
    const editionsToday = rows.filter((row) => {
      const day = budapestDateParts(new Date(row.created_at));
      return day.y === leadDay.y && day.m === leadDay.m && day.d === leadDay.d;
    }).length;
    if (editionsToday > 0) {
      const tmpl = editionsToday === 1 ? strings.issueEditionsTodayOne : strings.issueEditionsToday;
      parts.push(tmpl.replace("{n}", String(editionsToday)));
    }
  }
  return parts.join(" · ");
}

export function renderIndexPage(
  rows,
  token,
  host,
  lang,
  view,
  weekInfo = null,
  nowArcs = [],
  nowMs = null,
  showCatchup = false,
) {
  const strings = STRINGS[lang];
  const emptyMessage =
    view === "daily"
      ? strings.noDailyBriefs
      : view === "weekly"
        ? strings.noWeeklyBriefs
        : strings.noDigests;

  // Current-week-only features (roadmap 3 step 3): the lead card, pulse
  // strip, and prefetch hint below all imply "this is what's happening
  // right now" — a "Latest" card on an archive week would lie, so they're
  // gated on isCurrent, true on the (week-less) daily view and on the
  // CURRENT week of the all view, false on any archive week. handleIndexPage
  // this function (see there), so no separate check is needed for that one.
  const isCurrent = weekInfo === null || weekInfo.isCurrentWeek;

  // Tracked outside the branch below so it's reachable for the prefetch
  // href (roadmap 2 step 7) further down — null on the empty-index path and
  // on an archive week (no lead there at all), same as everywhere else in
  // this function.
  let leadRow = null;
  let body;
  if (rows.length === 0) {
    body = `<p class="empty">${esc(emptyMessage)}</p>`;
  } else if (isCurrent) {
    // rows are ordered created_at DESC, so rows[0] is the newest digest in
    // this view — it renders as the lead card above the ledger and is
    // excluded from the grouped list below (no duplicate). groupByDay runs
    // on the remainder, so if the newest digest was that day's only entry,
    // no empty day header is left behind.
    const [lead, ...rest] = rows;
    leadRow = lead;
    const ledger = renderLedgerFor(view, rest, strings.locale, (row) =>
      renderIndexEntry(row, token, lang, view),
    );
    body = `${renderLeadCard(lead, token, lang, view)}\n${ledger}`;
  } else {
    // Archive week (roadmap 3 step 3): no lead card — every row, including
    // rows[0], goes through the plain day-grouped ledger, same as the
    // "rest" branch above minus the exclusion.
    body = renderLedgerFor(view, rows, strings.locale, (row) =>
      renderIndexEntry(row, token, lang, view),
    );
  }

  // Unified search (owner UX pass): the container the bottom script's
  // archive-search IIFE fills with fragments fetched from the search route
  // (see handleSearchPage's fragment=1 branch and renderSearchFragment).
  // `hidden` by default, same "no-JS/pre-fetch default state" contract as
  // the filter input above — un-hidden only once a fetch actually returns
  // results. data-search-href carries the token/lang-scoped search route so
  // the script itself stays token/lang-agnostic, same pattern as every
  // other data-* hook on this page (data-created, data-unread-label, …).
  const archiveResultsHtml = `<div class="archiveresults" data-search-href="${searchHref(token, lang)}" hidden></div>`;

  // Week rail (roadmap 3 step 2): between the view tabs (rendered by
  // pageChrome, just above this) and the ledger — ALL-view index pages only
  // (weekInfo is null for the daily/weekly views, see handleIndexPage). Sits
  // above the empty-state message too, since both live inside the <section>
  // wrapper assembled below. Renders on the current week too (unlike the
  // lead/prefetch below) — the rail IS the archive navigation, so it stays
  // regardless of isCurrent.
  //
  // Archive sparkline (roadmap 4 step 6): `rows` is only handed to the rail
  // on an archive week (isCurrent false) — the current week's rail stays
  // sparkline-less (null), the same "nothing to show yet, the ledger below
  // is the current week's own record" posture the day-pulse strip this
  // sparkline was originally paired against used to carry (that strip was
  // removed in the owner-requested index cleanup; the current-week rail was
  // deliberately left as-is rather than backfilling a sparkline onto it —
  // out of scope for that pass).
  // Owner-requested placement: whenever the NOW section renders, IT carries
  // the search control (in its own head row, right-aligned) and the rail
  // below goes without — exactly one search control per page, always.
  const railHtml =
    view === "all" && weekInfo
      ? renderWeekRail(token, lang, weekInfo, strings, isCurrent ? null : rows)
      : "";

  // Search row (owner-requested index cleanup): the daily/weekly views have
  // no week rail to carry the search bubble (see railHtml just above), so
  // they get a minimal standalone row instead — same renderSearchBubble
  // markup, just without the week-nav spans around it, in the same
  // between-tabs-and-ledger slot the old filterrow occupied. Skipped
  // whenever railHtml is truthy (the ALL view) so the bubble never renders
  // twice on the same page. This keeps the ⌘K palette's "Search" command
  // (which reads `.searchlink` off the DOM, see collectPaletteItems)
  // available on every view, not just ALL — losing it silently on daily/
  // weekly would have been a real regression, not just a cosmetic one.

  // data-week-archive (roadmap 3 step 3): marks the <section> on any
  // non-current week so the bottom script's unread-fence IIFE can bail out
  // entirely — see that script for why an archive page must never draw a
  // fence or advance the lastVisit stamp.
  const archiveAttr = isCurrent ? "" : ' data-week-archive="1"';

  // data-unread-label (roadmap 2 step 2): the unread-fence label text,
  // rendered server-side so the bottom script that builds the fence stays
  // language-agnostic — it just reads this attribute rather than knowing
  // about STRINGS/lang itself.
  //
  // data-empty-filtered (roadmap 2 step 5): same pattern, for the
  // client-only "nothing matches the active filter(s)" message the bottom
  // script creates lazily — see applyFilters.
  //
  // Prefetch hint (roadmap 2 step 7): only when a lead exists, i.e. a
  // non-empty CURRENT-week index — see pageChrome's prefetchHref param
  // comment for the full mechanism/rationale, and isCurrent above for why
  // an archive week never has a leadRow to prefetch in the first place.
  const prefetchHref = leadRow ? digestHref(token, lang, view, leadRow.id) : null;

  // NOW section (§11.1 PR B): the very top of the page, above the rail/
  // filter row/pulse strip/ledger — a situational overview, not part of the
  // chronological archive chrome below it. `nowArcs` is only ever non-empty
  // when handleIndexPage ran the query (current-week all view — see there);
  // renderNowSection itself also fails safe to "" on an empty array, so this
  // stays a no-op on every other view/week without a second gate here.
  const nowHtml = renderNowSection(nowArcs, strings, token, lang, nowMs);

  // Catch-up banner (§11.2): a hidden shell, same "data-* carrier" contract
  // as paletteConfigHtml/the filter input above — no content
  // rendered server-side (the reader's lastVisit timestamp never leaves
  // their browser, so N/M can only ever be computed client-side), just the
  // i18n templates the bottom script's unread-fence IIFE extension needs to
  // stay language-agnostic. `hidden` by default: a no-JS reader, a first-
  // ever visit (no lastVisit yet), and every non-eligible page (showCatchup
  // false, so this whole const is "") all see nothing, same "inert until JS
  // proves it's warranted" contract as every other progressive-enhancement
  // shell in this file. Positioned as the very first element of the body
  // markup — above nowHtml — so it always renders "one row above the NOW
  // section" per the §11.2 spec, whether or not the NOW section itself has
  // any content that day (0-eligible-arcs still leaves this shell in place).
  const catchupHtml = showCatchup
    ? `<div class="catchup" hidden data-prefix="${esc(strings.catchupPrefix)}" data-tmpl-briefings="${esc(strings.catchupBriefings)}" data-tmpl-briefings-one="${esc(strings.catchupBriefingsOne)}" data-tmpl-arcs="${esc(strings.catchupArcUpdates)}" data-tmpl-arcs-one="${esc(strings.catchupArcUpdatesOne)}" data-tmpl-more="${esc(strings.catchupArcMore)}"><span class="catchuptext"></span><button type="button" class="catchupjump" hidden aria-label="${esc(strings.catchupJumpLabel)}">↓</button><button type="button" class="catchupdismiss" aria-label="${esc(strings.catchupDismissLabel)}">×</button></div>`
    : "";

  return pageChrome(
    host,
    token,
    lang,
    view,
    renderSwitchers(token, lang, view, "index", undefined, isCurrent ? null : weekInfo, true),
    `${catchupHtml}${nowHtml}${railHtml}<section data-unread-label="${esc(strings.unreadFence)}" data-empty-filtered="${esc(strings.emptyFiltered)}"${archiveAttr}>${body}</section>${archiveResultsHtml}`,
    null,
    prefetchHref,
    buildIssueLine(leadRow, rows, view, strings),
  );
}
