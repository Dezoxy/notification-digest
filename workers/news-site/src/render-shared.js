import { esc } from "./http.js";
import { formatDayHeader } from "./dates.js";
import { findArcSectionAnchor } from "./sections.js";

// ── page bodies ──────────────────────────────────────────────────────────

// One day-bucketed ledger shape — a sticky .dayhead per group, then that
// day's rows — shared by the index (twice: lead-card split and archive
// week) and the arc page's timeline, which differ only in the row renderer.
export function renderDayLedger(groups, renderRow) {
  return groups
    .map(
      (group) => `<div class="dayhead">${esc(group.label)}</div>
${group.items.map(renderRow).join("\n")}`,
    )
    .join("\n");
}

export function groupByDay(rows, locale) {
  const groups = [];
  let currentLabel = null;
  let currentItems = null;
  for (const row of rows) {
    const label = formatDayHeader(new Date(row.created_at), locale);
    if (label !== currentLabel) {
      currentLabel = label;
      currentItems = [];
      groups.push({ label, items: currentItems });
    }
    currentItems.push(row);
  }
  return groups;
}

// Kind badge for daily/weekly rows — "" for a window row, and "" for a
// daily row in the daily view or a weekly row in the weekly view (the badge
// is redundant there: every row is already that same kind of brief; only
// the all view needs it to tell the kinds apart at a glance). Weekly is the
// SAME visual family as daily, not a new kind of thing — it's a synthesis
// too, just a wider window — so it reuses the identical `flag-daily` class
// and filled-indigo look, only the label text differs. The redundancy rule
// is symmetric: a daily row hides its badge in the daily view, a weekly row
// hides its badge in the weekly view, and each kind always shows its badge
// elsewhere — daily/ filters strictly to kind='daily' and weekly/ strictly
// to kind='weekly' (see the file-header "Daily-brief view" comment), so a
// daily row never actually reaches the weekly view or vice versa; the check
// below is the simplest faithful form regardless. Used by renderIndexEntry
// and renderLeadCard so the two never drift apart building this separately.
export function kindBadge(row, view, strings) {
  if (row.kind === "daily" && view !== "daily") {
    return `<span class="flag flag-daily">${esc(strings.dailyBrief)}</span>`;
  }
  if (row.kind === "weekly" && view !== "weekly") {
    return `<span class="flag flag-daily">${esc(strings.weeklyBrief)}</span>`;
  }
  return "";
}

// "What changed" block (§11.3 delta persistence, ingest v4): the digest
// page's per-arc previously/now lines, rendered directly below the
// story-arc chip line (renderArcs, just above) and above the TOC — see the
// design guidance's scanning principle ("communicate what changed since the
// reader last looked, don't re-summarize the world every time"). `deltas`
// is handleDigestPage's parseDeltas output (null, or a non-empty array —
// never []); `topicArcs` is the SAME array renderArcs already received,
// which carries {slug, label, count} for EVERY topic on this digest, not
// just the recurring ones renderArcs itself filters down to — reused here
// purely for slug -> label lookup, no extra query.
//
// A delta whose slug has no matching topicArcs entry renders with the raw
// slug as fail-safe last-resort text rather than being dropped or throwing.
// This is a real possibility, not just defensive paranoia: topics and
// deltas are validated as two INDEPENDENT optional fields at ingest time
// (see validateDigestPayload), so a payload could legitimately send deltas
// without topics (or a differently-shaped topics list), and an older stored
// row can predate one field while already carrying the other. Same
// "something imperfect beats nothing/an error" posture as
// findArcSectionAnchor's null-anchor fallback elsewhere in this file.
// Delta prose is English-only today: §11.3 stores exactly ONE `deltas`
// payload, extracted from the English window digest by the app's
// extract_deltas choke point BEFORE translation ever runs, so no Hungarian
// variant of these sentences exists anywhere. Rendering it on a /hu/ page
// produced a Hungarian heading ("MI VÁLTOZOTT") wrapped around entirely
// English sentences — owner-reported, and worse than showing nothing.
// Suppressed there instead, which costs the HU reader nothing substantive:
// the same delta-only stories are already written as prose in the
// Hungarian body right below (the app writes them that way; the fenced
// block is a convenience layer over information the body already carries).
//
// THE single hook for the eventual `deltas_hu` payload (ingest v5): widen
// this to "true when text exists in `lang`", and pass that text through at
// the two call sites below (renderDeltas here, renderArcAppearance's own
// deltaHtml on the arc page). Both consult this, so neither can be
// forgotten.
export function deltasRenderableIn(lang) {
  return lang === "en";
}

// The previously -> now line itself, shared verbatim by the digest page's
// "what changed" block and the arc page's per-appearance delta (the two
// renderers' comments already declared the shapes "shared"; now they are).
export function renderDeltaLine(previously, now) {
  return `<p class="deltatext"><span class="deltaprev">${esc(previously)}</span><span class="deltaarrow">→</span><span class="deltanow">${esc(now)}</span></p>`;
}

// Momentum indicator (§11.1 guardrail: frequency-derived only, NEVER
// severity vocabulary — "↑ more coverage", never "↑ escalating") —
// appearances in the trailing 48h vs. the 48h before that, anchored at the
// CURRENT instant (`nowMs`), not at any one digest's own created_at. This is
// deliberately different from handleDigestPage's per-digest 7-day arc-line
// count just above, which anchors at that digest's own created_at so an old
// digest's arc line stays reproducible history forever (see the comment
// there) — an arc PAGE is a live view of "where does this story stand right
// now", so "now" is the correct anchor here, and this page's momentum is
// expected to change on every visit as time passes, unlike the arc line.
//
// Both windows empty -> null, and the indicator is OMITTED from the page
// (see renderArcPage): a dormant arc has no frequency signal to report, and
// "less coverage" against a prior window that was also silent would be a
// claim the data doesn't make (design guidance: labels state only what's
// provable). With any activity in either window, "recent === 0 -> down" is
// checked BEFORE the recent-vs-prior comparison so a slug that just went
// quiet always reads as declining coverage, never as "steady".
export function computeArcMomentum(appearances, nowMs) {
  const windowStart = nowMs - 48 * 3600000;
  const priorWindowStart = nowMs - 96 * 3600000;
  let recent = 0;
  let prior = 0;
  for (const row of appearances) {
    const t = new Date(row.created_at).getTime();
    if (t >= windowStart && t < nowMs) recent += 1;
    else if (t >= priorWindowStart && t < windowStart) prior += 1;
  }
  if (recent === 0 && prior === 0) return null;
  if (recent === 0) return "down";
  if (recent > prior) return "up";
  if (recent === prior) return "same";
  return "down";
}
