import {
  ARC_ANCHOR_BODIES,
  ARC_IDENTITY_SQL,
  MAX_DELTAS,
  MAX_SOURCE_ENTRIES,
  MAX_TOPICS,
  arcIdentity,
} from "./config.js";
import { htmlResponse, notFound } from "./http.js";
import { tokenMatches } from "./auth.js";
import { adjacentWeek, compareIsoWeek, isoWeekOf, weekBoundsUtc } from "./dates.js";
import { findArcSectionAnchor, sectionsOfBody } from "./sections.js";
import { arcHref } from "./hrefs.js";
import { groupByDay } from "./render-shared.js";
import { pageChrome } from "./chrome.js";
import {
  computeNowArcs,
  renderDegradedBadge,
  renderIndexPage,
  renderSourceKey,
} from "./render-index.js";
import { renderArcContext, renderArcs, renderDeltas, renderDigestPage } from "./render-digest.js";
import { renderArcAppearance, renderArcPage } from "./render-arc.js";
import {
  markSnippet,
  renderAboutPage,
  renderSearchFragment,
  renderSearchPage,
} from "./render-search.js";
import { handleIngest } from "./ingest.js";

// The index/daily/weekly ledger row shape — one definition for the three
// list queries below, which are the same SELECT contract with different
// WHERE clauses. The digest PAGE query deliberately stays separate: it
// fetches the body/topics/deltas/provenance columns this list never needs.
export const DIGEST_LIST_COLUMNS =
  "id, created_at, tldr, tldr_hu, item_count, section_count, has_attention, kind, source_counts, failed_sources";

export async function handleIndexPage(env, token, url, lang, view, weekParam) {
  if (!(await tokenMatches(env, token))) return notFound();

  // Weekly pagination (roadmap 3 step 2): the daily and weekly views stay
  // unpaginated (~3 years from feeling the old LIMIT-1000 backstop, and a
  // weekly brief a week is even further out — see the weekly branch below)
  // — the route match above still grammatically allows "daily/w/…" or
  // "weekly/w/…" (the "w/" segment can follow any view prefix), so an
  // unpaginated view being asked for a week address 404s here rather than
  // silently ignoring the segment or rendering something misleading for a
  // URL that has no real page behind it.
  if (view !== "all" && weekParam) return notFound();

  // The week actually being rendered: the URL's w/ segment if present,
  // otherwise the current Budapest-local ISO week. NOTE: this step (roadmap
  // 3 "Core week machinery") deliberately does NOT gate the lead
  // card/prefetch hint to the current week only —
  // that's the next step ("Feature scoping"). They keep rendering
  // unconditionally here, which can look a little odd on an archive week
  // page (e.g. a "Latest" card that isn't) — expected and fine for now.
  const current = isoWeekOf(new Date());
  const effective = weekParam ?? current;
  const isCurrentWeek = compareIsoWeek(effective, current) === 0;

  let results;
  let weekInfo = null;
  if (view === "daily") {
    // kind='daily' ONLY — a weekly brief never appears here, it lives in
    // the All view (see the file-header "Daily-brief view" comment). Also
    // unbounded, exactly as before this step — see the all-view branch
    // below for why the old flat LIMIT 1000 was a page-weight backstop, not
    // real pagination; the daily view isn't getting real pagination here.
    // tldr_hu is always selected (cheap) even for the EN page — only the HU
    // renderer reads it.
    const { results: dailyResults } = await env.DB.prepare(
      `SELECT ${DIGEST_LIST_COLUMNS} FROM digests WHERE kind = 'daily' ORDER BY created_at DESC, id DESC LIMIT 1000`,
    ).all();
    results = dailyResults;
  } else if (view === "weekly") {
    // Same shape as the daily branch above, kind='weekly' ONLY — a daily (or
    // window) digest never appears here, it lives in the All view. Also
    // unbounded and unpaginated, same reasoning as daily — a weekly brief a
    // week is ~50 rows a year, even further from the LIMIT-1000 backstop
    // than the daily view's ~365 rows a year, so real pagination is even
    // less warranted here.
    const { results: weeklyResults } = await env.DB.prepare(
      `SELECT ${DIGEST_LIST_COLUMNS} FROM digests WHERE kind = 'weekly' ORDER BY created_at DESC, id DESC LIMIT 1000`,
    ).all();
    results = weeklyResults;
  } else {
    // Week-bounded query replaces the old flat LIMIT-1000 backstop for the
    // all view. created_at is UTC ISO text, so a lexicographic >=/< against
    // the UTC boundary strings from weekBoundsUtc is a correct comparison
    // without parsing — same trick every other created_at comparison in
    // this file already relies on. Order by created_at, not id: daily
    // briefs get BACKFILLED for past days, so a backfilled row can have a
    // high id but an old, historical created_at; `id DESC` stays only as a
    // deterministic tiebreak for same-instant rows. groupByDay relies on
    // this ordering to put each row in its correct day bucket.
    const { startIso, endIso } = weekBoundsUtc(effective.year, effective.week);
    const { results: weekResults } = await env.DB.prepare(
      `SELECT ${DIGEST_LIST_COLUMNS} FROM digests WHERE created_at >= ? AND created_at < ? ORDER BY created_at DESC, id DESC LIMIT 1000`,
    )
      .bind(startIso, endIso)
      .all();
    results = weekResults;

    // Oldest-week probe: one MIN(created_at) over the WHOLE table — not
    // week-bounded, not kind-filtered, mirroring the all view's own "every
    // digest, mixed" scope — locating the earliest week that has ever had
    // data. The rail's "older" link renders only when the previous week is
    // still >= that floor; a mid-range week with no data in between still
    // gets its own page (empty state + rail), it just isn't itself a valid
    // "older" TARGET past the floor.
    const oldestRow = await env.DB.prepare("SELECT MIN(created_at) AS oldest FROM digests").first();
    const oldestWeek = oldestRow?.oldest ? isoWeekOf(new Date(oldestRow.oldest)) : null;
    const prevWeek = adjacentWeek(effective.year, effective.week, -1);
    const olderAllowed = Boolean(oldestWeek) && compareIsoWeek(prevWeek, oldestWeek) >= 0;
    // "Newer" only ever points toward the present: a future week (valid-
    // shaped but effective > current) gets no newer link either, same as
    // the current week itself.
    const newerAllowed = compareIsoWeek(effective, current) < 0;

    weekInfo = {
      year: effective.year,
      week: effective.week,
      isCurrentWeek,
      older: olderAllowed ? prevWeek : null,
      newer: newerAllowed ? adjacentWeek(effective.year, effective.week, 1) : null,
    };
  }

  // NOW section (§11.1 PR B, "homepage becomes NOW"): only queried on the
  // page that will actually render it — the CURRENT-week ALL view, no w/
  // segment (weekParam === null implies effective === current, see above,
  // but the check is on weekParam itself, not isCurrentWeek: the section is
  // gated on "no w/ segment" specifically, the exact URL grammar the §11.1
  // spec calls out, not merely "happens to resolve to the current week").
  // Every other view/week pays zero extra roundtrip for this section — see
  // renderIndexPage's own no-op fallback when nowArcs stays [].
  //
  // showCatchup (§11.2): the catch-up banner's gate, written out as its own
  // const — literally the same boolean expression as the NOW section's just
  // above ("same gate as the NOW section" per the §11.2 spec, not merely
  // "happens to agree with it today"). Passed through to renderIndexPage
  // separately from nowArcs because the banner must still be ABLE to render
  // (N briefings only, M omitted) in the 0-eligible-arcs case where nowArcs
  // stays [] and the NOW section itself renders nothing — inferring the gate
  // from nowArcs.length would wrongly suppress the banner shell then.
  const showCatchup = view === "all" && weekParam === null;
  let nowArcs = [];
  const nowMs = Date.now();
  if (view === "all" && weekParam === null) {
    // One query, JS-side aggregation (see computeNowArcs's comment for why
    // GROUP BY + window functions are deliberately NOT used here): every
    // topic appearance in the trailing 7 days, anchored at THIS request's
    // own instant, not any digest's created_at — a live "now" section is
    // expected to re-rank on every visit, unlike a digest's own reproducible
    // arc line (contrast handleDigestPage's topicArcs windowStartIso, which
    // anchors at the digest's own created_at instead).
    const nowWindowStartIso = new Date(nowMs - 7 * 24 * 3600000).toISOString();
    // `identity` (stable arc keys, ARC_IDENTITY_SQL) is what computeNowArcs
    // actually groups by — the fix for the bug this section exists to solve:
    // a story recurring under several differently-worded (and therefore
    // differently-slugged) headings now clusters into ONE arc instead of
    // several never-eligible 1-appearance ones.
    const { results: nowRows } = await env.DB.prepare(
      `SELECT je.value->>'label' AS label, ${ARC_IDENTITY_SQL} AS identity, d.created_at, d.id
         FROM digests d, json_each(d.topics) je
        WHERE d.topics IS NOT NULL AND d.created_at >= ?1
        ORDER BY d.created_at ASC`,
    )
      .bind(nowWindowStartIso)
      .all();
    nowArcs = computeNowArcs(nowRows ?? [], nowMs);
  }

  return htmlResponse(
    renderIndexPage(
      results ?? [],
      token,
      url.hostname,
      lang,
      view,
      weekInfo,
      nowArcs,
      nowMs,
      showCatchup,
    ),
  );
}

// Deltas (§11.3 delta persistence, ingest v4): fail-safe parse of the
// `deltas` JSON column — same "unparseable or wrong-shaped -> treated as
// absent" contract as renderDegradedBadge/the topics parse just below
// (never throws, drops individually malformed entries rather than the whole
// array). A shared function, not inlined per call site like the topics
// parse below, because TWO read paths need it: handleDigestPage (this
// digest's own "What changed" block) and handleArcPage (each appearance's
// own delta entry, matched by slug) — one fail-safe rule for both, rather
// than two copies that could drift.
export function parseDeltas(deltasJson) {
  if (!deltasJson) return null;
  let parsed;
  try {
    parsed = JSON.parse(deltasJson);
  } catch {
    return null;
  }
  if (!Array.isArray(parsed)) return null;
  const wellFormed = parsed.filter(
    (d) =>
      d !== null &&
      typeof d === "object" &&
      !Array.isArray(d) &&
      typeof d.slug === "string" &&
      typeof d.previously === "string" &&
      typeof d.now === "string",
  );
  return wellFormed.length > 0 ? wellFormed : null;
}

export async function handleDigestPage(env, token, idParam, url, lang, view) {
  if (!(await tokenMatches(env, token))) return notFound();

  const id = Number(idParam);
  if (!Number.isInteger(id) || id <= 0) return notFound();

  // provenance (model-provenance byline, ingest v5) rides along here, not
  // in DIGEST_LIST_COLUMNS above — same "digest PAGE only" carve-out that
  // comment already calls out for body/topics/deltas: the ledger/NOW-arc
  // list queries never render the provenance lines, only handleDigestPage does
  // (renderProvenance, called from renderDigestPage).
  const digest = await env.DB.prepare(
    "SELECT id, created_at, tldr, item_count, section_count, has_attention, body_html, body_html_hu, kind, source_counts, failed_sources, topics, deltas, provenance FROM digests WHERE id = ?",
  )
    .bind(id)
    .first();
  if (!digest) return notFound();

  // Story-arc counts (ingest v3, roadmap 4 step 8): fail-safe parse, same
  // contract as renderDegradedBadge — an unparseable or wrong-shaped topics
  // value is treated as "no topics" rather than thrown.
  let topics = null;
  if (digest.topics) {
    try {
      const parsed = JSON.parse(digest.topics);
      if (Array.isArray(parsed)) {
        // Per-entry shape check too, not just "is an array" — same defense
        // against a stored value predating a validation change that every
        // other renderer here applies (renderDegradedBadge, renderSourceKey);
        // a wrong-shaped entry must drop out, not render "undefined".
        const wellFormed = parsed.filter(
          (t) =>
            t !== null &&
            typeof t === "object" &&
            !Array.isArray(t) &&
            typeof t.slug === "string" &&
            typeof t.label === "string",
        );
        if (wellFormed.length > 0) topics = wellFormed;
      }
    } catch {
      // unparseable -> treat as absent
    }
  }

  let topicArcs = null;
  if (topics) {
    // One extra query, paid only when the digest actually has topics. The
    // window is a TRAILING 7 days ending at THIS digest's own created_at
    // (exclusive upper bound, and id != this digest so it never counts
    // itself here) — not "now" — so an old digest's arc line is reproducible
    // history: it must not change as newer digests arrive after it.
    const windowStartIso = new Date(
      new Date(digest.created_at).getTime() - 7 * 86400000,
    ).toISOString();
    const { results: priorRows } = await env.DB.prepare(
      "SELECT topics FROM digests WHERE topics IS NOT NULL AND created_at >= ? AND created_at < ? AND id != ? LIMIT 200",
    )
      .bind(windowStartIso, digest.created_at, id)
      .all();

    // Per-digest, not per-occurrence: each prior row contributes at most one
    // count per arc IDENTITY (an identity set, not a running tally),
    // regardless of how many times that identity might otherwise appear.
    // Counting by identity (key when present, else slug — see arcIdentity)
    // rather than raw slug is the actual fix this recurrence count needed:
    // a story whose heading gets reworded every run now still accumulates
    // one shared count instead of fragmenting into several 1-count topics.
    const priorIdentitySets = priorRows
      .map((row) => {
        try {
          const parsed = JSON.parse(row.topics);
          if (!Array.isArray(parsed)) return null;
          return new Set(
            parsed.map((t) => arcIdentity(t)).filter((identity) => typeof identity === "string"),
          );
        } catch {
          return null;
        }
      })
      .filter((set) => set !== null);

    // count = prior occurrences + 1, i.e. total appearances including this
    // digest itself — a topic seen only here renders as count 1 (bare label,
    // see renderArcs). `identity` rides alongside `slug` on each entry:
    // renderArcs uses `identity` for its arcHref link target, while `slug`
    // stays available for renderDeltas' own slug-keyed label lookup (deltas
    // are the deliberate exception to arc identity — see ARC_IDENTITY_SQL's
    // comment) — neither renderer has to recompute what the other needs.
    topicArcs = topics.map((t) => {
      // One fold per topic — the old inline form re-ran arcIdentity(t) inside
      // the per-prior-set filter, O(topics x priors) recomputations of the
      // same value.
      const identity = arcIdentity(t);
      let priors = 0;
      for (const set of priorIdentitySets) if (set.has(identity)) priors++;
      return { slug: t.slug, label: t.label, identity, count: priors + 1 };
    });
  }

  // In the daily or weekly view, prev/next stay within that same kind so a
  // reader hops brief-to-brief rather than through every window digest (or
  // the other brief kind) in between — the all view keeps today's
  // unconstrained chronological prev/next.
  //
  // Neighbor = adjacent by (created_at, id) tuple order, not by id: daily
  // briefs get BACKFILLED for past days with historical created_at values,
  // so a backfilled row's id says nothing about its chronological position.
  // SQLite row-value comparison ((created_at, id) < (?, ?)) does the tuple
  // compare/tiebreak in one expression — supported since SQLite 3.15, and
  // D1's SQLite is far newer.
  const kindFilter =
    view === "daily" ? " AND kind = 'daily'" : view === "weekly" ? " AND kind = 'weekly'" : "";
  const [older, newer] = await Promise.all([
    env.DB.prepare(
      `SELECT id, created_at FROM digests WHERE (created_at, id) < (?, ?)${kindFilter} ORDER BY created_at DESC, id DESC LIMIT 1`,
    )
      .bind(digest.created_at, id)
      .first(),
    env.DB.prepare(
      `SELECT id, created_at FROM digests WHERE (created_at, id) > (?, ?)${kindFilter} ORDER BY created_at ASC, id ASC LIMIT 1`,
    )
      .bind(digest.created_at, id)
      .first(),
  ]);

  // Deltas (§11.3 delta persistence, ingest v4): renders "" on a digest with
  // no deltas — see parseDeltas and renderDeltas's own "absent-data"
  // contract, matching topics/source_counts elsewhere on this page.
  const deltas = parseDeltas(digest.deltas);

  return htmlResponse(
    renderDigestPage(digest, older, newer, token, url.hostname, lang, view, topicArcs, deltas),
  );
}

export async function handleSearchPage(env, token, url, lang) {
  if (!(await tokenMatches(env, token))) return notFound();

  // Query text lives in ?q=, not the path — see the URL-grammar header
  // comment. Truncated to 200 chars: nothing legitimate needs more, and it
  // bounds how much work buildFtsMatch and the D1 query below ever do for
  // one request.
  const q = (url.searchParams.get("q") ?? "").trim().slice(0, 200);

  // Fragment mode (unified search, owner UX pass): ?fragment=1 asks for just
  // the results markup, no pageChrome, no form — this is what the index
  // page's own filter box fetches in the background (see the bottom
  // script's archive-search IIFE) so a reader never has to leave the index
  // to see full-archive hits. The standalone route above (no fragment=1)
  // stays exactly as before: full page, no-JS form, deep-linkable.
  const isFragment = url.searchParams.get("fragment") === "1";

  // Empty query: with fragment=1 there's nothing to show — the ledger's own
  // empty-filtered message already speaks, so an empty body is correct, not
  // a degraded case. Full-page mode keeps its existing behavior: render
  // just the form, no search attempted (`results` staying null is what
  // tells renderSearchPage "no count line, no no-results message either" —
  // see there).
  if (q === "") {
    if (isFragment) return htmlResponse("");
    return htmlResponse(renderSearchPage(null, q, token, url.hostname, lang));
  }

  const matchQuery = buildFtsMatch(q);

  let results;
  try {
    // tldr/tldr_hu (snippet column indexes 0/2) weighted 10x over
    // body_md/body_md_hu (1/3) in the bm25 ranking — a title-word match
    // should outrank one buried in the body. snippet() wraps each matched
    // fragment in CHAR(1)/CHAR(2) sentinel bytes rather than real HTML tags
    // — body_md/body_md_hu are untrusted markdown, never HTML (see the
    // file-header comment), so the actual <mark> tags get added later, in
    // renderSearchPage/markSnippet, AFTER escaping the snippet text.
    const { results: rows } = await env.DB.prepare(
      `SELECT d.id, d.created_at, d.kind,
              snippet(digests_fts, 1, CHAR(1), CHAR(2), '…', 12) AS snip,
              snippet(digests_fts, 3, CHAR(1), CHAR(2), '…', 12) AS snip_hu
         FROM digests_fts
         JOIN digests d ON d.id = digests_fts.rowid
        WHERE digests_fts MATCH ?
        ORDER BY bm25(digests_fts, 10.0, 1.0, 10.0, 1.0)
        LIMIT 50`,
    )
      // Bound as a parameter even though buildFtsMatch already neutralizes
      // FTS5 syntax below — never string-interpolate user input into SQL,
      // belt and suspenders.
      .bind(matchQuery)
      .all();
    results = rows;
  } catch {
    // A MATCH string we built ourselves (see buildFtsMatch) should never
    // error, but a 500 on a search box is a worse failure mode than an
    // empty result — degrade to the no-results state instead of surfacing
    // whatever went wrong.
    results = [];
  }

  if (isFragment) return htmlResponse(renderSearchFragment(results, token, lang));

  return htmlResponse(renderSearchPage(results, q, token, url.hostname, lang));
}

// About page: a short static page explaining what the site is, for the
// friends the owner shares a capability link with — same token gate/404
// contract as every other route (see the file-header comment), no D1 query
// at all beyond that check. `url` is only used for the hostname (renderer
// signature parity with the other simple pages, e.g. renderSearchPage), same
// as handleSearchPage/handleArcPage.
export async function handleAboutPage(env, token, url, lang) {
  if (!(await tokenMatches(env, token))) return notFound();
  return htmlResponse(renderAboutPage(token, url.hostname, lang));
}

// Arc page (§11.1 PR A): reconstructs the full appearance chain for one arc
// IDENTITY at request time — see the file-header comment for why this is
// DERIVED, not stored, and the "Stable arc keys" file-header section for what
// `identity` means (key when present, else slug — arcIdentity/
// ARC_IDENTITY_SQL). Identity shape is already guaranteed by the route regex
// in fetch() (`[a-z0-9-]{1,64}`) before this ever runs — the same shape a
// slug or a key is independently validated to at ingest time.
export async function handleArcPage(env, token, identity, url, lang) {
  if (!(await tokenMatches(env, token))) return notFound();

  // json_each cross-joins each digest's `topics` JSON array; the WHERE
  // clause (topics IS NOT NULL, then ARC_IDENTITY_SQL matching the route's
  // `identity` segment) is what actually narrows the cross join down to at
  // most one row per digest — SQLite JSON1, available in D1 (verified
  // against the stubbed-D1 smoke test; ->> is standard SQLite 3.38+, and
  // D1's SQLite is far newer; json_extract(je.value, '$.slug'/'$.key') is
  // the fallback shape if a future D1 runtime ever regresses that operator).
  // `identity` is arc identity (stable arc keys, see ARC_IDENTITY_SQL's
  // comment), not necessarily a slug: `/a/hormuz` matches every digest whose
  // topic carries key "hormuz" regardless of that digest's own (differently
  // worded) slug, and `/a/<old-slug>` still resolves unchanged for any row
  // stored before `key` existed, since COALESCE falls back to slug there.
  //
  // DESC + LIMIT, then reversed in JS: if an arc ever exceeds the ceiling,
  // the rows that must survive are the NEWEST — the H1 (latest label),
  // "updated ...", and momentum are all computed off the latest end, so an
  // ASC LIMIT would silently freeze this page in the arc's distant past the
  // day it overflowed. LIMIT 500 is a sane ceiling in the spirit of
  // MAX_TOPICS/MAX_SOURCE_ENTRIES elsewhere in this file — not real
  // pagination, just a backstop against a pathological arc that recurs in
  // every digest ever ingested.
  //
  // body_html deliberately does NOT ride along here: a weekly's body_html
  // runs large, and an arc can legitimately accumulate hundreds of
  // appearances over months — hauling every body through one query is an
  // unbounded-memory shape no matter how normal each row is. Anchor
  // resolution (the only body_html consumer) is capped to the newest
  // ARC_ANCHOR_BODIES appearances via the second, id-bounded query below.
  // d.deltas rides along here (unlike body_html below): it's a small JSON
  // array capped at MAX_DELTAS entries, nothing like body_html's unbounded-
  // per-row cost, so there's no reason to defer it to a second, bounded
  // query the way anchor resolution is deferred — see parseDeltas/the
  // appearances mapping below for how each appearance picks out its own
  // slug's entry (§11.3 delta persistence, ingest v4).
  //
  // `topicSlug` (this appearance's OWN topic slug, not `identity`) rides
  // along too, for the delta match just below — deltas stay keyed on each
  // digest's own slug regardless of arc identity, see ARC_IDENTITY_SQL's
  // comment on why that's a deliberate exception, not an oversight.
  const { results: chainRows } = await env.DB.prepare(
    `SELECT d.id, d.created_at, d.kind, je.value->>'label' AS label, je.value->>'slug' AS topicSlug, d.deltas
       FROM digests d, json_each(d.topics) je
      WHERE d.topics IS NOT NULL AND ${ARC_IDENTITY_SQL} = ?1
      ORDER BY d.created_at DESC, d.id DESC
      LIMIT 500`,
  )
    .bind(identity)
    .all();

  // No digest currently carries this identity: unknown arc, same
  // indistinguishable 404 as a bad token or a nonexistent digest id (see the
  // file-header trust model — a well-shaped-but-unknown identity must reveal
  // nothing either).
  if (!chainRows || chainRows.length === 0) return notFound();

  // Deep-link anchors only for the newest ARC_ANCHOR_BODIES appearances —
  // the ones a reader actually navigates into from a live arc page. Older
  // appearances render with a fragment-less digest link, which is already
  // this feature's documented degraded mode (findArcSectionAnchor returning
  // null), not a new behavior — the fail-safe contract stays one contract.
  const anchorIds = chainRows.slice(0, ARC_ANCHOR_BODIES).map((r) => r.id);
  // Arc context primer (§11.6 context mode): one extra query, ONLY on this
  // page — never fetched for the index/digest/search pages, which have no
  // single arc identity to look one up by. Run alongside the body-anchor
  // query below (Promise.all, not a second sequential round trip) since
  // neither depends on the other's result. `identity` is the exact primary
  // key arc_context.key is upserted under (handleIngest's arc_contexts
  // upsert uses the SAME `key` field topics' own optional key uses — see
  // arcIdentity/ARC_IDENTITY_SQL), so this is a direct lookup, not a fold
  // over topics like the chain query below. No row (the common case: most
  // arcs have no primer, or never will) leaves contextMd null — renderArcPage
  // renders nothing for the disclosure in that case, see renderArcContext.
  const [{ results: bodyRows }, contextRow] = await Promise.all([
    env.DB.prepare(
      `SELECT id, body_html FROM digests WHERE id IN (${anchorIds.map(() => "?").join(", ")})`,
    )
      .bind(...anchorIds)
      .all(),
    env.DB.prepare("SELECT context_md FROM arc_context WHERE key = ?1").bind(identity).first(),
  ]);
  const bodyById = new Map((bodyRows ?? []).map((r) => [r.id, r.body_html]));
  // One section scan per fetched body (see sectionsOfBody) — every
  // appearance of that digest then does a plain array match against it.
  const sectionsById = new Map(Array.from(bodyById, ([id, body]) => [id, sectionsOfBody(body)]));
  const contextMd = contextRow?.context_md ?? null;

  // Reversed to ASC (oldest first): renderArcPage's first/latest handling
  // and its own display-only re-reversal both rely on ASC input — see there.
  const rows = chainRows.slice().reverse();
  const appearances = rows.map((row) => {
    // Each appearance's own delta, if it has one, matched against THIS
    // ROW'S OWN topic slug (row.topicSlug), never against `identity`
    // (§11.3 delta persistence, ingest v4 + ARC_IDENTITY_SQL's comment on
    // why deltas are the deliberate exception to arc identity) — a digest's
    // `deltas` column can carry entries for several arcs at once, so
    // parseDeltas' fail-safe array is filtered down to at most the one entry
    // matching this appearance's own slug. No match (the common case: most
    // appearances predate the feature, or simply weren't a delta-only
    // update) leaves `delta` null — renderArcAppearance renders exactly as
    // it did before this feature in that case.
    const deltas = parseDeltas(row.deltas);
    const delta = deltas ? (deltas.find((d) => d.slug === row.topicSlug) ?? null) : null;
    return {
      id: row.id,
      created_at: row.created_at,
      kind: row.kind,
      label: row.label,
      anchor: sectionsById.has(row.id)
        ? findArcSectionAnchor(sectionsById.get(row.id), row.label)
        : null,
      delta,
    };
  });

  return htmlResponse(
    renderArcPage(identity, appearances, token, url.hostname, lang, Date.now(), contextMd),
  );
}

// Turns free-text user input into a SAFE fts5 MATCH string. Raw user input
// must never reach FTS5 query syntax directly: FTS5 has its own operators
// (OR, NEAR, *, parentheses, "quoted phrases"), so an unquoted term like
// `NEAR(` or `tldr:*` is either a syntax error (crashes the query) or a
// query hijack (turns a reader's plain search into someone else's boolean
// expression). Phrase-quoting every term neutralizes all of it — each term
// becomes a literal string match, joined with implicit AND — and doubling
// any internal double quote (fts5's own escape convention) keeps a quote
// inside a term from closing the phrase early rather than being treated as
// a special character. At most 8 terms: plenty for a briefing search, and a
// hard cap on how many implicit-AND clauses one query can generate.
export function buildFtsMatch(q) {
  const terms = q.split(/\s+/).filter(Boolean).slice(0, 8);
  return terms.map((term) => `"${term.replaceAll('"', '""')}"`).join(" ");
}
