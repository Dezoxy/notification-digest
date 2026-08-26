// Fixture dataset for the render tests and golden pages.
//
// One frozen instant governs everything: FROZEN_NOW (see env.mjs). Every
// created_at below is chosen RELATIVE to that instant on purpose — the index
// week-bounds, the NOW section's trailing-7-day window, the arc momentum
// labels, and the oldest-week probe all compare against "now", and a fixture
// drifting out of the current ISO week would silently stop exercising its
// branch. Frozen clock + fixed rows = byte-stable goldens.
//
// Branch coverage this dataset must keep alive (render.test.mjs asserts a
// marker for each, so losing one FAILS loudly rather than rotting):
//   - topics WITH a stable `key` (235) and WITHOUT one (234) — arcIdentity's
//     COALESCE(key, slug) fold, and a 2-appearance arc across differently
//     slugged headings (the exact bug stable keys exist to fix)
//   - deltas (235) -> "what changed" block on the EN digest page, suppressed
//     on the HU twin (deltasRenderableIn)
//   - failed_sources (235) -> degraded badge + source-key failed pills
//   - source_counts (all) -> source key swatches
//   - NULL _hu fields (234) -> the /hu/ EN-fallback note
//   - arc_context row -> the arc page's context primer disclosure
//   - a body with an inline style attr (235 s2) -> stripInlineStyles
//   - a body with an external citation link (235 s1) -> addCiteTitles
//   - a previous-ISO-week digest (231) -> week-rail "older" link + oldest
//     probe; also 235's prev/next neighbors within the current week
//   - daily (233) and weekly (232) kinds -> those views' lists + kind badges

export const FROZEN_NOW = "2026-08-25T18:00:00Z";

export const SITE_TOKEN = "goldentesttoken";
export const INGEST_KEY = "goldeningestkey";

// Topic identity shared by 235 and 234 through the stable `key` — two
// different slugs, one arc.
export const ARC_IDENTITY = "markets-slide";

const BODY_235 = [
  '<div class="tldr"><strong>TL;DR:</strong> Markets slid for a second day while the EU package landed.</div>',
  "<h2>Markets slide continues</h2>",
  '<p>Equities extended losses. <a href="https://example.com/markets-report">Reuters</a> puts the drawdown at four percent.</p>',
  "<h2>EU agrees new package</h2>",
  '<p>The council signed off <span style="color: red">overnight</span>, with disbursement gated on reforms.</p>',
  "<h2>Chip export rules tighten</h2>",
  "<p>New licensing requirements take effect next quarter.</p>",
].join("\n");

const BODY_235_HU = [
  '<div class="tldr"><strong>Röviden:</strong> A piacok második napja estek, az EU-csomag megszületett.</div>',
  "<h2>Folytatódik a piaci esés</h2>",
  '<p>A részvények tovább estek. <a href="https://example.com/markets-report">Reuters</a> négy százalékra teszi a visszaesést.</p>',
  "<h2>Az EU új csomagot fogadott el</h2>",
  "<p>A tanács éjjel írta alá, a folyósítás reformokhoz kötött.</p>",
  "<h2>Szigorodnak a chipexport-szabályok</h2>",
  "<p>Az új engedélyezési követelmények a következő negyedévtől élnek.</p>",
].join("\n");

const BODY_234 = [
  '<div class="tldr"><strong>TL;DR:</strong> The sell-off began; two sections follow.</div>',
  "<h2>Markets slide, day two</h2>",
  "<p>The slide that started yesterday accelerated into the close.</p>",
  "<h2>Energy prices steady</h2>",
  "<p>Gas storage remains above the seasonal norm.</p>",
].join("\n");

const BODY_233 = [
  '<div class="tldr"><strong>TL;DR:</strong> Daily brief for Monday.</div>',
  "<h2>Quiet Monday</h2>",
  "<p>Nothing demanded attention today.</p>",
].join("\n");

const BODY_232 = [
  '<div class="tldr"><strong>TL;DR:</strong> The week in one page.</div>',
  "<h2>Week thirty-four wrap</h2>",
  "<p>Summary of the closing week.</p>",
].join("\n");

const BODY_231 = [
  '<div class="tldr"><strong>TL;DR:</strong> From the previous week.</div>',
  "<h2>Last week's marker</h2>",
  "<p>Exists so the archive has an older week.</p>",
].join("\n");

// Column shape mirrors schema.sql's digests table (TEXT JSON blobs stay
// strings here, exactly as D1 returns them).
export const DIGESTS = [
  {
    id: 235,
    created_at: "2026-08-25T14:00:00Z",
    tldr: "Markets slid for a second day while the EU package landed.",
    item_count: 12,
    section_count: 3,
    has_attention: 0,
    body_html: BODY_235,
    body_md: "## Markets slide continues\nEquities extended losses.\n## EU agrees new package\nSigned overnight.\n## Chip export rules tighten\nNext quarter.",
    tldr_hu: "A piacok második napja estek, az EU-csomag megszületett.",
    body_html_hu: BODY_235_HU,
    body_md_hu: "## Folytatódik a piaci esés\nTovább estek.\n## Az EU új csomagot fogadott el\nÉjjel aláírva.\n## Szigorodnak a chipexport-szabályok\nJövő negyedévtől.",
    kind: "window",
    source_counts: JSON.stringify({ telegram: 5, x: 4, rss: 3 }),
    failed_sources: JSON.stringify(["polymarket"]),
    topics: JSON.stringify([
      { slug: "markets-slide-continues", label: "Markets slide continues", key: ARC_IDENTITY },
      { slug: "eu-agrees-new-package", label: "EU agrees new package" },
    ]),
    deltas: JSON.stringify([
      { slug: "markets-slide-continues", previously: "Down two percent", now: "Down four percent" },
    ]),
  },
  {
    id: 234,
    created_at: "2026-08-25T08:00:00Z",
    tldr: "The sell-off began; two sections follow.",
    item_count: 9,
    section_count: 2,
    has_attention: 0,
    body_html: BODY_234,
    body_md: "## Markets slide, day two\nAccelerated into the close.\n## Energy prices steady\nAbove the seasonal norm.",
    tldr_hu: null,
    body_html_hu: null,
    body_md_hu: null,
    kind: "window",
    source_counts: JSON.stringify({ telegram: 4, rss: 5 }),
    failed_sources: null,
    topics: JSON.stringify([
      { slug: "markets-slide-day-two", label: "Markets slide, day two", key: ARC_IDENTITY },
    ]),
    deltas: null,
  },
  {
    id: 233,
    created_at: "2026-08-24T20:00:00Z",
    tldr: "Daily brief for Monday.",
    item_count: 6,
    section_count: 1,
    has_attention: 0,
    body_html: BODY_233,
    body_md: "## Quiet Monday\nNothing demanded attention.",
    tldr_hu: "Hétfői napi összefoglaló.",
    body_html_hu: BODY_233,
    body_md_hu: "## Csendes hétfő\nSemmi nem igényelt figyelmet.",
    kind: "daily",
    source_counts: JSON.stringify({ rss: 6 }),
    failed_sources: null,
    topics: null,
    deltas: null,
  },
  {
    id: 232,
    created_at: "2026-08-23T19:00:00Z",
    tldr: "The week in one page.",
    item_count: 40,
    section_count: 4,
    has_attention: 0,
    body_html: BODY_232,
    body_md: "## Week thirty-four wrap\nSummary of the closing week.",
    tldr_hu: "A hét egy oldalon.",
    body_html_hu: BODY_232,
    body_md_hu: "## Harmincnegyedik heti összefoglaló\nA záruló hét összegzése.",
    kind: "weekly",
    source_counts: JSON.stringify({ telegram: 20, x: 12, rss: 8 }),
    failed_sources: null,
    topics: null,
    deltas: null,
  },
  {
    id: 231,
    created_at: "2026-08-18T10:00:00Z",
    tldr: "From the previous week.",
    item_count: 7,
    section_count: 1,
    has_attention: 0,
    body_html: BODY_231,
    body_md: "## Last week's marker\nOlder-week fixture.",
    tldr_hu: null,
    body_html_hu: null,
    body_md_hu: null,
    kind: "window",
    source_counts: JSON.stringify({ rss: 7 }),
    failed_sources: null,
    topics: null,
    deltas: null,
  },
];

export const ARC_CONTEXTS = [
  {
    key: ARC_IDENTITY,
    context_md: "A sell-off that began on the 24th; central banks have not reacted yet.",
    updated_at: "2026-08-25T09:00:00Z",
  },
];

// A minimal VALID ingest payload (the required fields per
// validateDigestPayload). Tests mutate copies of this to hit each rejection.
export const VALID_INGEST_PAYLOAD = {
  created_at: "2026-08-25T20:00:00Z",
  tldr: "A fresh digest arriving over ingest.",
  body_html: "<h2>One section</h2><p>Body.</p>",
  body_md: "## One section\nBody.",
  item_count: 3,
  section_count: 1,
  has_attention: false,
};
