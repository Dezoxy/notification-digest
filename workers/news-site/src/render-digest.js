import { arcIdentity } from "./config.js";
import { STRINGS } from "./strings.js";
import { CSS } from "./css.js";
import { esc } from "./http.js";
import { formatDayHeader, formatShortDate, formatTime, tzAbbr } from "./dates.js";
import {
  addCiteTitles,
  buildSectionToc,
  findArcSectionAnchor,
  stripInlineStyles,
} from "./sections.js";
import { arcHref, digestHref, indexHref, renderSwitchers } from "./hrefs.js";
import { deltasRenderableIn, renderDeltaLine } from "./render-shared.js";
import { pageChrome } from "./chrome.js";
import {
  deriveHeadline,
  renderDegradedBadge,
  renderProvenance,
  renderSourceKey,
} from "./render-index.js";

// Inline per-section arc links (this feature): complements renderArcs' own
// top-of-page "Story threads" chip line by putting a small link right at
// EACH body section whose heading is a RECURRING topic (count >= 2, the
// same threshold renderArcs already filters on) — so a reader already
// mid-section can jump to that story's full arc page without scrolling back
// up. Runs on buildSectionToc's OWN OUTPUT (`html`/`sections` above),
// matching the exact `<h2 id="sN">title</h2>` shape that pass just
// produced. Only ever APPENDS a sibling `<a>` right after a matched
// heading's closing tag — the heading's text and its #sN id are never
// touched, so the buildSectionToc <-> app section_link_targets anchor
// coupling (see the file header) is completely unaffected by this feature.
//
// Matching a heading to a topic is EXACT `section.title === label.trim()`,
// the same comparison findArcSectionAnchor already uses to resolve arc-page
// deep links (see that function's comment): derive_topics (notification-
// digest repo, digest/publish.py) folds a topic's label directly from the
// section heading text, so — for labels under 80 chars, never truncated —
// the two strings are byte-identical, and reusing that proven comparison
// here is deliberate rather than inventing a second matching rule. A label
// matching more than one section, or a section matching more than one
// recurring topic (two headings/labels that happen to collide), is
// ambiguous and is skipped — same fail-safe "wrong link is worse than no
// link" posture as findArcSectionAnchor, never a guess.
//
// Fail-safe by construction, not just by the try/catch: a heading with no
// unambiguous match is simply never added to `linkBySectionId` and the
// regex replace leaves it untouched. The try/catch below exists only to
// guarantee this pass can never turn a rendering hiccup (a malformed topic
// entry, an unexpected label shape) into a broken page — worst case is the
// same "no inline links" degradation as any other unmatched heading.
export function addInlineArcLinks(html, sections, topicArcs, strings, token, lang) {
  if (!topicArcs || topicArcs.length === 0 || !sections || sections.length === 0) return html;
  try {
    const recurring = topicArcs.filter(({ count }) => count >= 2);
    if (recurring.length === 0) return html;
    const linkBySectionId = new Map();
    for (const s of sections) {
      const matches = recurring.filter(
        (t) => typeof t.label === "string" && t.label.trim() === s.title,
      );
      if (matches.length === 1) linkBySectionId.set(s.id, matches[0]);
    }
    if (linkBySectionId.size === 0) return html;
    return html.replace(/<h2 id="(s\d+)">[^<]*<\/h2>/g, (match, id) => {
      const topic = linkBySectionId.get(id);
      if (!topic) return match;
      const label = strings.storySoFar.replace("{n}", String(topic.count));
      return `${match}<a class="secarc" href="${arcHref(token, lang, topic.identity)}">${esc(label)}</a>`;
    });
  } catch {
    return html; // fail-safe: never let this pass break the page
  }
}

// Zero or one section needs no index — a one-section brief has nothing to
// jump between, so skip the nav entirely rather than render a single
// pointless chip. Title text is passed through esc() — it originated from
// pre-sanitized HTML, but re-escaping text content read out of it is free
// safety, not redundant trust.
//
// Numbering (Front Page redesign — "in this edition"): "01", "02", … from
// the entry's own 1-based position in `sections`, which is itself already
// in document order (buildSectionToc assigns ids sequentially as it walks
// the article) — the SAME order .digest > h2's own CSS-counter badges will
// number the actual sections in, so the two numberings always agree without
// sharing any markup.
export function renderToc(sections) {
  if (sections.length < 2) return "";
  const chips = sections
    .map(
      (s, i) =>
        `<a href="#${s.id}"><span class="tocnum">${String(i + 1).padStart(2, "0")}</span>${esc(s.title)}</a>`,
    )
    .join("\n");
  return `<nav class="toc">${chips}</nav>\n`;
}

// Story-arc line (ingest v3, roadmap 4 step 8): the digest page's per-topic
// thread summary, built from handleDigestPage's topicArcs — null (no topics
// on this digest, query never ran) or an empty array both render "", same
// absent-data contract as renderDegradedBadge/renderSourceKey. A topic with
// count 1 (seen only in this digest) renders as the bare label; count >= 2
// appends the "×N this week" suffix via strings.arcRepeat's template
// replace — see the STRINGS comment for why that's a whole-string template,
// not hand-composed pieces.
//
// Each recurring chip is now a LINK to that slug's arc page (§11.1 PR A) —
// `token`/`lang` are needed for arcHref, alongside the strings this function
// already took. Each topic entry carries its own `slug` since handleDigestPage
// started threading it through (see there) specifically so this could link.
export function renderArcs(topicArcs, strings, token, lang) {
  if (!topicArcs || topicArcs.length === 0) return "";
  // RECURRING topics only (count >= 2) — this is what the roadmap 4 step 8
  // spec always said ("topics that ALSO appeared in the prior 7 days"), and
  // the first live digest showed why (owner-reported, 2026-08-09): topics
  // derive from section headings, so a single-appearance topic's chip is a
  // shouted duplicate of the TOC chip right below it. A digest whose topics
  // are all first appearances gets no arc line at all — nothing is
  // continuing, so there is no thread to point at.
  const recurring = topicArcs.filter(({ count }) => count >= 2);
  if (recurring.length === 0) return "";
  // href targets arc IDENTITY (key when present, else slug — see
  // arcIdentity), never the raw per-digest slug: the chip must link to the
  // SAME arc page every appearance of this story links to, regardless of
  // how this digest's own heading happened to be worded.
  const chips = recurring
    .map(
      ({ identity, label, count }) =>
        `<a class="arc" href="${arcHref(token, lang, identity)}"><span class="arclabel">${esc(label)}</span><span class="arccount">${esc(strings.arcRepeat.replace("{n}", String(count)))}</span></a>`,
    )
    .join("");
  return `<nav class="arcs" aria-label="${esc(strings.arcsLabel)}">${chips}</nav>\n`;
}

export function renderDeltas(deltas, topicArcs, strings, token, lang) {
  if (!deltas || deltas.length === 0 || !deltasRenderableIn(lang)) return "";
  const labelBySlug = new Map((topicArcs ?? []).map((t) => [t.slug, t.label]));
  const rows = deltas
    .map(
      (d) => `<a class="delta" href="${arcHref(token, lang, d.slug)}">
    <span class="deltalabel">${esc(labelBySlug.get(d.slug) ?? d.slug)}</span>
    ${renderDeltaLine(d.previously, d.now)}
  </a>`,
    )
    .join("");
  return `<div class="archivelabel">${esc(strings.whatChangedLabel)}</div><nav class="deltas" aria-label="${esc(strings.whatChangedLabel)}">${rows}</nav>\n`;
}

// Arc context primer disclosure (PLAN.md §11.6 context mode): the arc page's
// COLLAPSED background primer, rendered directly under the title/metadata
// line and above the appearances timeline (see renderArcPage's call site).
// Native <details>/<summary>, same "no JS needed" contract as this file's
// other disclosures (.settings, .searchpop) — collapsed by default so a
// reader who already knows the background isn't forced past it, and it still
// works with JS disabled. No primer -> render nothing at all (no empty
// state), same "absent data renders as absence" contract as renderDeltas.
//
// `contextMd` is markdown from a MODEL — UNTRUSTED, same trust posture as
// any other free-text field this app produces. Unlike body_html/body_html_hu
// (the only fields anywhere in this file that skip esc() — see the
// file-header comment), context_md does NOT arrive pre-rendered/pre-
// sanitized: it's raw markdown, and this Worker has no markdown-to-HTML
// renderer anywhere in it. Adding one (a library, or a hand-rolled parser)
// would be new attack surface purpose-built for one field whose input is a
// model's raw text — not justified by what the spec actually asks for (3-5
// SHORT paragraphs of durable background, no rich formatting requirement).
// Instead this gets the same treatment search snippets already get before
// their <mark> tags go back in (see markSnippet's comment) — escape
// everything, then structure: split on blank lines, esc() EVERY paragraph,
// wrap each in a plain <p>. Markdown syntax in the source (e.g. "**word**",
// a bare "<script>") renders as inert escaped text, never as markup or a
// live tag — a deliberate degradation (no bold/links/etc. render), not a
// bug: nothing here can ever inject unescaped model output into the page.
export function renderArcContext(contextMd, strings) {
  if (!contextMd) return "";
  const paragraphs = contextMd
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => `<p>${esc(p)}</p>`)
    .join("");
  if (!paragraphs) return "";
  return `<details class="arccontext"><summary>${esc(strings.arcContextLabel)}</summary><div class="arccontextbody">${paragraphs}</div></details>\n`;
}

export function renderDigestPage(digest, older, newer, token, host, lang, view, topicArcs, deltas) {
  const strings = STRINGS[lang];
  const date = new Date(digest.created_at);
  // "digest" itself stays an untranslated literal (see the STRINGS comment
  // above) — only the daily-brief/weekly-brief labels are real HU
  // vocabulary, swapped in for kind="daily"/kind="weekly" respectively.
  const kindLabel =
    digest.kind === "daily"
      ? strings.dailyBrief
      : digest.kind === "weekly"
        ? strings.weeklyBrief
        : "digest";
  // Edition eyebrow (Front Page redesign): kind · time · items · sections —
  // the full weekday/date is already visible in the compact masthead's own
  // issue line right above (see pageChrome), so it's dropped here rather
  // than repeated twice on the same page.
  const stamp = `${kindLabel} · ${formatTime(date, strings.locale)} ${tzAbbr(date)} · ${digest.item_count} ${strings.itemsWord} · ${digest.section_count} ${strings.sectionsWord}`;
  // Derived h1 headline (Front Page redesign): digest.tldr is an already-
  // selected column that renderDigestPage never rendered directly before
  // this — the article's own embedded TL;DR paragraph (body_html's
  // `.tldr` div, restyled as the leader right below this headline, see the
  // CSS) already carries the full text, so deriving a short title from the
  // SAME source here adds a headline without duplicating any content.
  const headline = deriveHeadline(digest.tldr);
  // <title>: shorter than the stamp (formatShortDate, not formatDayHeader) —
  // browser tab/history width is tight, and the token never appears here.
  // pageChrome esc()s the whole composed string before inserting it.
  const pageTitle = `${kindLabel} #${digest.id} · ${formatShortDate(date, strings.locale)} ${formatTime(date, strings.locale)}`;

  const navLinks = [
    `<a href="${indexHref(token, lang, view)}">${esc(strings.allDigests)}</a>`,
    '<span class="spacer"></span>',
  ];
  // Hide the link entirely at each end (oldest has no older, newest has no
  // newer) rather than showing a disabled placeholder.
  if (older) {
    navLinks.push(
      `<a class="nav-older" href="${digestHref(token, lang, view, older.id)}">← ${esc(formatTime(new Date(older.created_at), strings.locale))}</a>`,
    );
  }
  if (newer) {
    navLinks.push(
      `<a class="nav-newer" href="${digestHref(token, lang, view, newer.id)}">${esc(formatTime(new Date(newer.created_at), strings.locale))} →</a>`,
    );
  }

  // HU page: prefer the translated body; if the app never sent one for this
  // digest, fall back to the English body_html and say so above the article
  // rather than silently presenting untranslated content on a HU URL.
  //
  // `usingEnglishBody` (this feature, addInlineArcLinks below): true both on
  // an EN page AND on a HU page that fell back to the English body — the
  // condition that matters isn't "is this the /hu/ URL", it's "are these
  // headings actually the English ones topics.label was derived from" (see
  // addInlineArcLinks' own comment for why a real HU translation's headings
  // can never match).
  let articleHtml = digest.body_html;
  let enOnlyNoteHtml = "";
  let usingEnglishBody = true;
  if (lang === "hu") {
    if (digest.body_html_hu) {
      articleHtml = digest.body_html_hu;
      usingEnglishBody = false;
    } else {
      enOnlyNoteHtml = `<p class="en-only-note">${esc(strings.enOnlyNote)}</p>`;
    }
  }

  // Strip the emailer's inline styles FIRST (owner-reported, 2026-08-09):
  // the app renders ONE body_html for both the email and this site, and the
  // email half bakes light-theme colors in as style="" attributes (mail
  // clients can't do stylesheets). Served verbatim here, those attributes
  // BEAT the site's class rules — in dark mode the TL;DR card stayed light
  // and its citation chips went white-on-white. The classes (.tldr,
  // .attention, .banner, .cite, .tldr-label, …) arrive alongside the
  // styles, so stripping the attributes hands presentation fully to the
  // site's own themed CSS. Order matters: the email's cite pills are
  // `<a class="cite" style="…" href="…">`, and addCiteTitles' exact-shape
  // regex below never matched that — hover domains (and the print
  // stylesheet's cite[title] domains) were silently missing on real
  // digests; stripping first restores the exact shape every downstream
  // pass expects. The regex is safe here because this markup is
  // nh3-normalized + emailer-generated: attributes are always
  // double-quoted, never single-quoted or bare.
  const articleHtmlThemed = stripInlineStyles(articleHtml);

  // TOC ids are injected into the themed html (see buildSectionToc), so
  // the <article> below renders the id-bearing version, not the original.
  const { html: articleHtmlWithIds, sections } = buildSectionToc(articleHtmlThemed);
  const tocHtml = renderToc(sections);

  // Inline per-section arc links (this feature, addInlineArcLinks): only
  // when `sections` is actually the English heading text topics.label was
  // derived from (see usingEnglishBody above) — a real HU translation's
  // headings are independently worded and would never match, so this pass
  // is skipped there rather than silently rendering zero links every time.
  const articleHtmlWithArcs = usingEnglishBody
    ? addInlineArcLinks(articleHtmlWithIds, sections, topicArcs, strings, token, lang)
    : articleHtmlWithIds;

  // Separate pass, one job each (see addCiteTitles): citation chips gain a
  // hover title naming their destination hostname.
  const articleHtmlFinal = addCiteTitles(articleHtmlWithArcs);

  // Same links, top and bottom: after an ~900-word read the natural gesture
  // is older/next, not scroll-to-top (roadmap step 2) — mirror the nav below
  // the article rather than making the reader travel back to the header.
  // Built once here, wrapped twice below; the bottom copy carries the extra
  // digestnav-bottom class (own CSS: top hairline + spacing, same as
  // .closing, since it follows the article's closing line).
  const digestNavLinksHtml = navLinks.join("\n");

  // Source key (roadmap 2 step 8 follow-up): one row of the article's
  // colophon (see colophonHtml below), same fail-safe absent-data contract
  // as renderDegradedBadge — renders "" on an older digest with no
  // source_counts/failed_sources.
  const sourceKeyHtml = renderSourceKey(digest.source_counts, digest.failed_sources, strings);

  // Model-provenance row ("Written by"/"Translated with" byline): same
  // fail-safe absent-data contract as sourceKeyHtml just above — renders ""
  // on a digest with no provenance (every digest before this feature,
  // forever).
  const provenanceHtml = renderProvenance(digest.provenance, strings);

  // Colophon wrapper (this feature): a divider between the article and its
  // two record-keeping rows, only when at least one of them actually has
  // something to say — an old digest with neither source_counts nor
  // provenance must render no colophon div at all, exactly as it rendered
  // nothing before this feature (see the .colophon CSS comment).
  const colophonHtml =
    sourceKeyHtml || provenanceHtml
      ? `<div class="colophon">${sourceKeyHtml}${provenanceHtml}</div>`
      : "";

  // Story-arc line (roadmap 4 step 8): renders "" on a digest with no topics
  // — see renderArcs and the topicArcs computation in handleDigestPage. Each
  // recurring chip links to that slug's arc page (§11.1 PR A).
  const arcsHtml = renderArcs(topicArcs, strings, token, lang);

  // "What changed" block (§11.3 delta persistence, ingest v4): renders "" on
  // a digest with no deltas — see renderDeltas.
  const deltasHtml = renderDeltas(deltas, topicArcs, strings, token, lang);

  // Order: crumbs -> edition header (eyebrow + derived h1) -> arc line ->
  // what-changed block -> en-only note -> TOC -> article. The numbered TOC
  // can't sit inside the article, AFTER the TL;DR-bearing leader paragraph,
  // as the detail prototype has it — the TL;DR callout is itself the FIRST
  // element of body_html (see the .tldr CSS comment), and repositioning it
  // relative to the rest of the article would mean parsing/rewriting
  // arbitrary pre-sanitized HTML, real risk for a purely cosmetic ordering
  // win — so the TOC keeps rendering above <article>, same position as
  // before this redesign; deviation noted in the PR description.
  const body = `<nav class="digestnav">${digestNavLinksHtml}</nav>
<header class="edhead">
  <p class="stamp">${esc(stamp)}</p>
  <h1>${esc(headline)}</h1>
</header>
${arcsHtml}${deltasHtml}${enOnlyNoteHtml}${tocHtml}<article class="digest">
${articleHtmlFinal}
</article>
${colophonHtml}<nav class="digestnav digestnav-bottom">${digestNavLinksHtml}</nav>
<a class="backfab" href="${indexHref(token, lang, view)}" aria-label="${esc(strings.backFabLabel)}">←</a>`;

  return pageChrome(
    host,
    token,
    lang,
    view,
    // showSearch true (consistent-masthead follow-up): the digest page
    // carries the same search bubble as the index; its client-side filter
    // input stays hidden here (the filter IIFE bails without a ledger
    // section), so the bubble offers just the archive search link.
    renderSwitchers(token, lang, view, "digest", digest.id, null, true),
    body,
    pageTitle,
    null,
    // Same issue-line slot the index fills — the digest's own edition
    // number and full date (the date the edhead eyebrow deliberately
    // dropped lives here now, one line, one place).
    `${strings.issueEdition.replace("{n}", String(digest.id))} · ${formatDayHeader(date, strings.locale)}`,
    // showTopFab false — the ONLY page that opts out: it renders its own
    // .backfab above (← to the index, the more useful action here), and two
    // floating buttons would stack in the same corner.
    false,
  );
}
