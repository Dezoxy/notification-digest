-- Local-D1 mirror of test/fixtures.mjs, for browser-based verification:
--   wrangler d1 execute news-digests --local --file schema.sql
--   wrangler d1 execute news-digests --local --file test/fixtures.sql
--   wrangler dev   (SITE_TOKEN=goldentesttoken INGEST_KEY=goldeningestkey in .dev.vars)
-- Keep in lockstep with fixtures.mjs — the CSS-matrix and client-JS browser
-- checks in later refactor PRs drive wrangler dev against THIS data.
INSERT INTO digests (id, created_at, tldr, item_count, section_count, has_attention, body_html, body_md, tldr_hu, body_html_hu, body_md_hu, kind, source_counts, failed_sources, topics, deltas, provenance) VALUES (235, '2026-08-25T14:00:00Z', 'Markets slid for a second day while the EU package landed.', 12, 3, 0, '<div class="tldr"><strong>TL;DR:</strong> Markets slid for a second day while the EU package landed.</div>
<h2>Markets slide continues</h2>
<p>Equities extended losses. <a href="https://example.com/markets-report">Reuters</a> puts the drawdown at four percent.</p>
<h2>EU agrees new package</h2>
<p>The council signed off <span style="color: red">overnight</span>, with disbursement gated on reforms.</p>
<h2>Chip export rules tighten</h2>
<p>New licensing requirements take effect next quarter.</p>', '## Markets slide continues
Equities extended losses.
## EU agrees new package
Signed overnight.
## Chip export rules tighten
Next quarter.', 'A piacok második napja estek, az EU-csomag megszületett.', '<div class="tldr"><strong>Röviden:</strong> A piacok második napja estek, az EU-csomag megszületett.</div>
<h2>Folytatódik a piaci esés</h2>
<p>A részvények tovább estek. <a href="https://example.com/markets-report">Reuters</a> négy százalékra teszi a visszaesést.</p>
<h2>Az EU új csomagot fogadott el</h2>
<p>A tanács éjjel írta alá, a folyósítás reformokhoz kötött.</p>
<h2>Szigorodnak a chipexport-szabályok</h2>
<p>Az új engedélyezési követelmények a következő negyedévtől élnek.</p>', '## Folytatódik a piaci esés
Tovább estek.
## Az EU új csomagot fogadott el
Éjjel aláírva.
## Szigorodnak a chipexport-szabályok
Jövő negyedévtől.', 'window', '{"telegram":5,"x":4,"rss":3}', '["polymarket"]', '[{"slug":"markets-slide-continues","label":"Markets slide continues","key":"markets-slide"},{"slug":"eu-agrees-new-package","label":"EU agrees new package"}]', '[{"slug":"markets-slide-continues","previously":"Down two percent","now":"Down four percent"}]', '{"summarize":{"model":"claude-opus-5","effort":"high","fallback":false},"translate":{"model":"openai/gpt-5.6-terra","effort":"high","fallback":true}}');
INSERT INTO digests (id, created_at, tldr, item_count, section_count, has_attention, body_html, body_md, tldr_hu, body_html_hu, body_md_hu, kind, source_counts, failed_sources, topics, deltas, provenance) VALUES (234, '2026-08-25T08:00:00Z', 'The sell-off began; two sections follow.', 9, 2, 0, '<div class="tldr"><strong>TL;DR:</strong> The sell-off began; two sections follow.</div>
<h2>Markets slide, day two</h2>
<p>The slide that started yesterday accelerated into the close.</p>
<h2>Energy prices steady</h2>
<p>Gas storage remains above the seasonal norm.</p>', '## Markets slide, day two
Accelerated into the close.
## Energy prices steady
Above the seasonal norm.', NULL, NULL, NULL, 'window', '{"telegram":4,"rss":5}', NULL, '[{"slug":"markets-slide-day-two","label":"Markets slide, day two","key":"markets-slide"}]', NULL, NULL);
INSERT INTO digests (id, created_at, tldr, item_count, section_count, has_attention, body_html, body_md, tldr_hu, body_html_hu, body_md_hu, kind, source_counts, failed_sources, topics, deltas, provenance) VALUES (233, '2026-08-24T20:00:00Z', 'Daily brief for Monday.', 6, 1, 0, '<div class="tldr"><strong>TL;DR:</strong> Daily brief for Monday.</div>
<h2>Quiet Monday</h2>
<p>Nothing demanded attention today.</p>', '## Quiet Monday
Nothing demanded attention.', 'Hétfői napi összefoglaló.', '<div class="tldr"><strong>TL;DR:</strong> Daily brief for Monday.</div>
<h2>Quiet Monday</h2>
<p>Nothing demanded attention today.</p>', '## Csendes hétfő
Semmi nem igényelt figyelmet.', 'daily', '{"rss":6}', NULL, NULL, NULL, NULL);
INSERT INTO digests (id, created_at, tldr, item_count, section_count, has_attention, body_html, body_md, tldr_hu, body_html_hu, body_md_hu, kind, source_counts, failed_sources, topics, deltas, provenance) VALUES (232, '2026-08-23T19:00:00Z', 'The week in one page.', 40, 4, 0, '<div class="tldr"><strong>TL;DR:</strong> The week in one page.</div>
<h2>Week thirty-four wrap</h2>
<p>Summary of the closing week.</p>', '## Week thirty-four wrap
Summary of the closing week.', 'A hét egy oldalon.', '<div class="tldr"><strong>TL;DR:</strong> The week in one page.</div>
<h2>Week thirty-four wrap</h2>
<p>Summary of the closing week.</p>', '## Harmincnegyedik heti összefoglaló
A záruló hét összegzése.', 'weekly', '{"telegram":20,"x":12,"rss":8}', NULL, NULL, NULL, NULL);
INSERT INTO digests (id, created_at, tldr, item_count, section_count, has_attention, body_html, body_md, tldr_hu, body_html_hu, body_md_hu, kind, source_counts, failed_sources, topics, deltas, provenance) VALUES (231, '2026-08-18T10:00:00Z', 'From the previous week.', 7, 1, 0, '<div class="tldr"><strong>TL;DR:</strong> From the previous week.</div>
<h2>Last week''s marker</h2>
<p>Exists so the archive has an older week.</p>', '## Last week''s marker
Older-week fixture.', NULL, NULL, NULL, 'window', '{"rss":7}', NULL, NULL, NULL, NULL);
INSERT INTO arc_context (key, context_md, updated_at) VALUES ('markets-slide', 'A sell-off that began on the 24th; central banks have not reacted yet.', '2026-08-25T09:00:00Z');
