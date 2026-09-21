// ── chrome strings (EN|HU) ──────────────────────────────────────────────
//
// This is the ENTIRE Hungarian vocabulary this Worker knows — everything
// else Hungarian-language on a /hu/ page is either digest content the app
// already translated (body_html_hu/tldr_hu) or a handful of untranslated
// micro-labels ("digest #N") left as-is; the TL;DR excerpt label localizes
// via STRINGS.tldrLabel ("Röviden:"). See README/PR notes for
// the reasoning. dailyBrief and weeklyBrief are the exceptions on the stamp
// line: for kind="daily"/"weekly" they replace the untranslated "digest"
// label, so the HU stamp reads "napi összefoglaló #N" / "heti összefoglaló
// #N" instead of "digest #N". Owner: please read these for correctness,
// they're the only hardcoded Hungarian text in the codebase.
export const STRINGS = {
  en: {
    locale: "en-GB",
    itemsWord: "items",
    sectionsWord: "sections",
    allDigests: "← All digests",
    noDigests:
      "No briefings yet. The next window closes every three hours — the first one lands here on its own.",
    noDailyBriefs: "No daily briefs yet — the first one lands at 20:00.",
    noWeeklyBriefs: "No weekly briefs yet — the first one lands Sunday at 21:00.",
    tldrLabel: "TL;DR:",
    backFabLabel: "Back to all digests",
    // The digest page's FAB goes BACK to the index; every other page has
    // nowhere to go back to, so its FAB scrolls UP instead. Two labels,
    // one button shape — see .backfab in the CSS.
    topFabLabel: "Back to top",
    enOnlyNote: null,
    dailyBrief: "daily brief",
    weeklyBrief: "weekly report",
    viewAll: "All",
    viewDaily: "Daily",
    viewWeekly: "Weekly",
    latest: "Latest",
    filterPlaceholder: "Filter briefings…",
    emptyFiltered: "Nothing matches.",
    themeToggle: "Toggle light/dark",
    densityToggle: "Toggle compact list",
    // Settings bubble (owner redesign): the gear button that collapses the
    // language/theme/size/density cluster, plus its panel row labels.
    settingsLabel: "Settings",
    settingsLanguage: "Language",
    settingsTheme: "Theme",
    // Three-state theme miniseg labels (owner redesign: light/auto/dark
    // replaces the old two-state ◐ toggle) — see renderSwitchers.
    themeLight: "Light",
    themeAuto: "Auto",
    themeDark: "Dark",
    settingsTextSize: "Text size",
    // Body-font miniseg (owner-requested serif toggle — a friend argued
    // for the retired serif body; now both camps get their way). The
    // captions describe the reading register, not the letterform (owner:
    // no font jargon in the UI) — the INTERNAL values stay sans/serif
    // (data-set, data-font, the localStorage `font` key), so shipped
    // preferences survive any future caption rewording.
    settingsFont: "Body font",
    fontModern: "Modern",
    fontClassic: "Classic",
    settingsDensity: "Density",
    unreadFence: "new since your last visit",
    degraded: "partial",
    // Week rail (roadmap 3 step 2). weekLabel is a placeholder template
    // ({w} = week number, {range} = formatWeekRangeLabel's output) rather
    // than hardcoded word order, so EN "Week 32 · 3–9 Aug" and HU
    // "32. hét · aug. 3–9." can each put the week word/number on their own
    // natural side of the range.
    weekRailLabel: "Week navigation",
    weekLabel: "Week {w} · {range}",
    // Digest-page source key label (rendered uppercase via .sklabel's CSS).
    sourcesLabel: "Sources",
    // Model-provenance row ("Written by"/"Translated with" byline, both
    // rendered uppercase via the same .sklabel CSS as sourcesLabel above):
    // two lines, one label each — see renderProvenance. provenanceLabel
    // names the summarize leg, provenanceTranslateLabel the translate leg;
    // the old in-chip "HU" marker naming the translate leg is gone — the
    // label itself now says which line this is.
    provenanceLabel: "Written by",
    provenanceTranslateLabel: "Translated with",
    // Search (roadmap 4 step 7). searchResults is a placeholder template
    // ({n} = result count), same convention as weekLabel above.
    searchLabel: "Search the archive",
    searchPlaceholder: "Search all briefings…",
    searchButton: "Search",
    searchLink: "Search ↗",
    searchResults: "{n} results",
    searchResultsOne: "{n} result",
    searchNone: "Nothing found.",
    // Search bubble trigger (owner-requested index cleanup, renderSearchBubble):
    // the compact button that opens the filter/search popover — both its
    // visible text and its aria-label, same "one string, two surfaces" reuse
    // as archiveLabel above. Deliberately its own string, not a reuse of
    // searchButton (the standalone search page's submit label) — a shared
    // string would couple two independently-changeable controls just
    // because their text happens to match today.
    searchToggleLabel: "Search",
    // Unified search (owner UX pass): eyebrow label above the archive
    // results the index page's own filter box surfaces in-page — see
    // renderSearchFragment/the .archivelabel CSS. Mono-eyebrow voice, not a
    // template (no {n} — the count line stays on the standalone search page
    // only).
    archiveResults: "From the archive",
    // Story-arc line (roadmap 4 step 8, renderArcs). arcRepeat is a
    // placeholder template ({n} = total appearances including this digest),
    // same convention as weekLabel/searchResults above — the whole "×{n}
    // this week" string comes from the template, never hand-composed.
    arcsLabel: "Story threads",
    arcRepeat: "×{n} this week",
    // Inline per-section arc link (this feature, addInlineArcLinks): a small
    // link right at a RECURRING section heading, pointing to that story's
    // arc page — same {n} = total appearances convention as arcRepeat, just
    // worded for a link sitting inline in the body rather than a chip in the
    // top-of-page line.
    storySoFar: "story so far ×{n}",
    // Arc page (§11.1 PR A, renderArcPage). arcAppearances/arcFirstSeen/
    // arcUpdated are placeholder templates ({n}/{date}/{t}), same convention
    // as arcRepeat/weekLabel/searchResults above — each whole metadata
    // fragment comes from its own template, never hand-composed pieces.
    // arcMomentum{Up,Same,Down} are the frequency-only labels the §11.1
    // guardrail requires (never severity words like "escalating") — see
    // computeArcMomentum.
    arcAppearances: "{n} appearances",
    arcAppearancesOne: "{n} appearance",
    arcFirstSeen: "first seen {date}",
    arcUpdated: "updated {t}",
    arcMomentumUp: "more coverage",
    arcMomentumSame: "steady",
    arcMomentumDown: "less coverage",
    arcTimelineLabel: "Appearances",
    // NOW section (§11.1 PR B, renderNowSection): the mono eyebrow above the
    // situational-overview block at the top of the current-week all-view
    // index — see computeNowArcs/renderNowSection. Each row's momentum
    // arrow reuses the same computeArcMomentum classification arcMomentum
    // {Up,Same,Down} above label on the arc page, but renders it as a bare
    // arrow glyph, not those text strings — no separate now* string needed.
    // (The row's metadata used to also reuse arcRepeat's "×{n} this week"
    // count; that was dropped in the owner-requested index cleanup — see
    // renderNowSection's own comment.)
    nowLabel: "Now",
    // Archive command label (§11.1 PR C): the ⌘K palette's "Archive" entry
    // (data-cmd-archive, see pageChrome/collectPaletteItems) used to double
    // as the masthead Archive link's own text too — that link was removed in
    // the index-cleanup pass (archive weeks are reached via the week rail's
    // ← link now, see renderWeekRail), so this string's only remaining
    // consumer is the palette label, which quietly never renders its row
    // anymore since collectPaletteItems' `.archivelink` lookup always comes
    // up empty — see that function's own comment.
    archiveLabel: "Archive",
    // ⌘K command palette (§11.1 PR C): client-side only, progressive
    // enhancement — see the palette IIFE in pageChrome. paletteLabel is the
    // palette's accessible name (the input's own aria-label — the dialog
    // itself is labelled BY the input via aria-labelledby, per the §11.1
    // spec, so this string only has to live in one place). paletteCmd* are
    // the static commands assembled at open time alongside whatever the
    // current page's own DOM contributes (arcs, briefings) — see
    // collectPaletteItems. paletteEmpty reuses emptyFiltered rather than
    // minting a near-duplicate "nothing matches" string; the "Search" and
    // "Archive" commands reuse searchButton/archiveLabel above for the same
    // reason.
    paletteLabel: "Command palette",
    palettePlaceholder: "Type a command or search…",
    paletteCmdTop: "Go to top",
    paletteCmdAll: "All view",
    paletteCmdDaily: "Daily view",
    paletteCmdWeekly: "Weekly view",
    paletteCmdSwitchLang: "Switch language",
    paletteCmdLatest: "Latest briefing",
    // Catch-up banner (§11.2, current-week all-view index only — same gate
    // as the NOW section, see handleIndexPage's showCatchup). Client-built
    // from the SAME lastVisit stamp the unread-fence IIFE already reads —
    // see that IIFE's extension for the reconciliation. catchupBriefings/
    // catchupArcUpdates/catchupArcMore are placeholder templates ({n}/{m}),
    // same convention as arcRepeat/weekLabel above; catchupJumpLabel/
    // catchupDismissLabel are aria-labels for the two icon-only controls
    // (jump to the "you were here" marker below, dismiss for this page-view
    // only — see the CSS/script for both).
    catchupPrefix: "Since your last visit:",
    catchupBriefings: "{n} briefings",
    catchupBriefingsOne: "{n} briefing",
    catchupArcUpdates: "{m} arc updates",
    catchupArcUpdatesOne: "{m} arc update",
    catchupArcMore: "+{n} more",
    catchupJumpLabel: "Jump to where you left off",
    catchupDismissLabel: "Dismiss",
    // Follow list (§11.2, optional feature, arc pages only): client-
    // injected text control next to the arc title (renderArcPage's
    // .archead) — no button chrome, matches the design guidance's "text
    // control" instruction. followAdd/followRemove are the two toggle
    // states, same star-glyph convention brief specified.
    followAdd: "☆ Follow",
    followRemove: "★ Following",
    // "What changed" block (§11.3 delta persistence, ingest v4): the mono
    // eyebrow above the digest page's per-arc previously/now lines — see
    // renderDeltas. Reuses the SAME .archivelabel eyebrow recipe as
    // arcTimelineLabel/archiveResults/nowLabel above, so this is a plain
    // one-off string, not a template.
    whatChangedLabel: "What changed",
    // Background primer disclosure (PLAN.md §11.6 context mode, renderArcContext):
    // the <summary> text for the arc page's collapsed durable-context panel.
    // Deliberately calm/factual, not a marketing verb ("Learn more") — the
    // design guidance's "evidence over certainty" / "calm urgency" register
    // applies to interface labels too, not just status text.
    arcContextLabel: "Background",
    // Big edition masthead issue line (Front Page redesign — the issue line
    // renders on pages that pass pageChrome a non-empty issueLineText; see
    // renderIndexPage's buildIssueLine): "No. {n}" reuses the digest's own numeric `id` as
    // the edition number (an existing column, not new plumbing) — a
    // placeholder template, same {n} convention as arcRepeat/weekLabel
    // above. issueEditionsToday(One) is the "{n} editions today" segment,
    // ALL view only (see buildIssueLine) — plural/singular split for the
    // same reason arcAppearances/catchupBriefings above split.
    issueEdition: "No. {n}",
    issueEditionsToday: "{n} editions today",
    issueEditionsTodayOne: "{n} edition today",
    // About page: a short static page explaining what the site is, for the
    // friends the owner shares a capability link with (see
    // renderAboutPage). aboutLabel does double duty as the page <title> and
    // as the link text in the settings panel (renderSwitchers) — both
    // surfaces want the exact same word, so one string covers both rather
    // than two identical keys.
    aboutLabel: "About",
    aboutWhatTitle: "What this is",
    aboutWhatBody:
      "A private, automatically curated news briefing, built for a small circle the owner shares this link with. Every six hours, a pipeline pulls new items from the owner's own sources — their Telegram groups, their X/Twitter notifications, a set of curated news desks (world wire services and Hungarian outlets), Reddit, prediction markets, and Hacker News — and an AI editor summarizes and organizes them into a briefing. Every claim links back to the source it came from.",
    aboutRhythmTitle: "The rhythm",
    aboutRhythmBody:
      "A window briefing lands four times a day, covering whatever's new since the last one. Every evening, a daily brief re-checks the day's stories against the open web and writes a verified summary. On Sundays, a weekly report ties the week together.",
    aboutReadingTitle: "How to read it",
    aboutReadingBody:
      "Each briefing is organized under story headings. The small numbers next to a claim are citations — hover or tap one to see where it came from. Stories that keep developing get their own story-arc page, reachable from a “story so far” link, so you can catch up without re-reading every briefing. To read in Hungarian, use the EN/HU switcher in the settings menu.",
    aboutCaveat:
      "Everything on this site is written by an AI, working only from the sources listed above — it can misread a source or miss context. If something matters, follow the citation and check it yourself.",
    // PWA shell (PLAN.md §11.7). Two audiences, both reached without a
    // page: the offline* three are the service worker's own fallback
    // document, shown when a navigation inside the installed app cannot
    // reach the network; the pushFallback* pair is the generic notification
    // shown only when push/latest is unreachable. None of these render
    // through pageChrome — see src/pwa.js, which reads them straight off
    // STRINGS and serializes them into the worker's CFG so this file stays
    // the single vocabulary source even for text that never touches HTML.
    offlineTitle: "Offline",
    offlineBody: "This briefing archive lives online only — nothing is stored on your device.",
    offlineRetry: "Try again",
    pushFallbackTitle: "New briefing",
    pushFallbackBody: "Tap to read it.",
    // Push notifications (PLAN.md §11.7, PR B) — the settings-bubble row.
    // Every one of these is hidden-until-JS like the minisegs beside it:
    // with no script, or on a browser without the Push API, the row never
    // appears at all rather than showing a dead control.
    // pushIosHint is the state that matters most in practice. On iOS, Web
    // Push is delivered ONLY inside a PWA opened from the Home Screen, so a
    // reader in Safari cannot enable anything no matter what they tap — the
    // honest answer is to tell them the one action that would work.
    pushLabel: "Notifications",
    pushEnable: "Turn on",
    pushEnabled: "On",
    pushDisable: "Turn off",
    pushBlocked: "Blocked in browser settings",
    pushIosHint: "Add to Home Screen first",
    pushFailed: "Could not enable",
    // The notification title for a plain window digest ("Digest #412"),
    // capitalized by notificationTitle in src/push.js. daily/weekly reuse
    // dailyBrief/weeklyBrief above rather than adding two more keys — the
    // notification and the stamp line should not be able to disagree about
    // what a brief is called.
    pushWindowTitle: "digest",
  },
  hu: {
    locale: "hu-HU",
    itemsWord: "elem",
    sectionsWord: "szakasz",
    allDigests: "← Minden hírlevél",
    noDigests:
      "Még nincs hírlevél. A következő ablak háromóránként zárul — az első magától megjelenik itt.",
    noDailyBriefs: "Még nincs napi összefoglaló — az első 20:00-kor érkezik.",
    // Owner: please review — new HU string, mirrors noDailyBriefs's pattern.
    noWeeklyBriefs: "Még nincs heti összefoglaló — az első vasárnap 21:00-kor érkezik.",
    tldrLabel: "Röviden:",
    backFabLabel: "Vissza a hírlevelekhez",
    // Owner: please review — new HU string.
    topFabLabel: "Vissza az elejére",
    enOnlyNote: "Csak angolul elérhető",
    dailyBrief: "napi összefoglaló",
    // Owner: please review — new HU string, mirrors dailyBrief's pattern.
    weeklyBrief: "heti összefoglaló",
    viewAll: "Minden",
    viewDaily: "Napi",
    // Owner: please review — new HU string, mirrors viewDaily's pattern.
    viewWeekly: "Heti",
    latest: "Legfrissebb",
    filterPlaceholder: "Szűrés…",
    emptyFiltered: "Nincs találat.",
    themeToggle: "Világos/sötét váltás",
    densityToggle: "Kompakt lista be/ki",
    // Owner: please review — new HU strings, settings bubble (gear button +
    // panel row labels), mirrors the EN block's pattern.
    settingsLabel: "Beállítások",
    settingsLanguage: "Nyelv",
    settingsTheme: "Téma",
    themeLight: "Világos",
    themeAuto: "Auto",
    themeDark: "Sötét",
    settingsTextSize: "Betűméret",
    // Owner: please review — new HU strings, body-font toggle row label +
    // register captions ("Klasszikus" is the longest miniseg caption on
    // the site; checked at phone width, the panel accommodates it).
    settingsFont: "Betűtípus",
    fontModern: "Modern",
    fontClassic: "Klasszikus",
    settingsDensity: "Sűrűség",
    unreadFence: "új a legutóbbi látogatásod óta",
    degraded: "hiányos",
    weekRailLabel: "Heti navigáció",
    weekLabel: "{w}. hét · {range}",
    sourcesLabel: "Források",
    // Owner: please review — new HU strings, model-provenance row, mirrors
    // sourcesLabel's pattern. provenanceLabel ("Írta") already meant
    // "written by" and needed no change for the two-line redesign.
    provenanceLabel: "Írta",
    // Owner: please review
    provenanceTranslateLabel: "Fordította",
    // Search (roadmap 4 step 7) — owner: please review these, flagged HU
    // strings same as everywhere else in this file.
    searchLabel: "Keresés az archívumban",
    searchPlaceholder: "Keresés az összes hírlevélben…",
    searchButton: "Keresés",
    searchLink: "Keresés ↗",
    searchResults: "{n} találat",
    // Hungarian does not pluralise a noun after a numeral — "1 találat" and
    // "5 találat" are both correct — so every *One key below is deliberately
    // identical to its plural twin. They exist so the EN side can differ;
    // dropping them here would make the lookup lang-conditional for no gain.
    searchResultsOne: "{n} találat",
    searchNone: "Nincs találat a keresésre.",
    // Owner: please review — new HU string, search bubble trigger
    // (owner-requested index cleanup), mirrors the EN block's pattern.
    searchToggleLabel: "Keresés",
    // Owner: please review — new HU string, mirrors searchLabel's pattern.
    archiveResults: "Az archívumból",
    // Story arcs (roadmap 4 step 8) — owner: please review these, flagged HU
    // strings same as everywhere else in this file.
    arcsLabel: "Történetszálak",
    arcRepeat: "×{n} ezen a héten",
    // Owner: please review — new HU string, inline per-section arc link
    // (this feature), mirrors arcRepeat's {n} convention.
    storySoFar: "eddig ×{n} alkalommal",
    // Arc page (§11.1 PR A) — owner: please review these, flagged HU
    // strings same as everywhere else in this file.
    arcAppearances: "{n} előfordulás",
    arcAppearancesOne: "{n} előfordulás",
    arcFirstSeen: "először: {date}",
    arcUpdated: "frissítve: {t}",
    arcMomentumUp: "több lefedettség",
    arcMomentumSame: "változatlan",
    arcMomentumDown: "kevesebb lefedettség",
    arcTimelineLabel: "Előfordulások",
    // Owner: please review — new HU string, NOW section (§11.1 PR B),
    // mirrors the EN block's pattern.
    nowLabel: "Most",
    // Owner: please review — new HU strings, Archive nav affordance +
    // ⌘K command palette (§11.1 PR C), mirror the EN block's pattern.
    archiveLabel: "Archívum",
    paletteLabel: "Parancspaletta",
    palettePlaceholder: "Parancs vagy keresés…",
    paletteCmdTop: "Fel",
    paletteCmdAll: "Teljes nézet",
    paletteCmdDaily: "Napi nézet",
    paletteCmdWeekly: "Heti nézet",
    paletteCmdSwitchLang: "Nyelv váltása",
    paletteCmdLatest: "Legfrissebb hírlevél",
    // Owner: please review — new HU strings, catch-up banner + follow list
    // (§11.2), mirror the EN block's pattern.
    catchupPrefix: "Legutóbbi látogatásod óta:",
    catchupBriefings: "{n} hírlevél",
    catchupBriefingsOne: "{n} hírlevél",
    catchupArcUpdates: "{m} történetfrissítés",
    catchupArcUpdatesOne: "{m} történetfrissítés",
    catchupArcMore: "+{n} további",
    catchupJumpLabel: "Ugrás oda, ahol abbahagytad",
    catchupDismissLabel: "Elrejtés",
    followAdd: "☆ Követés",
    followRemove: "★ Követve",
    // Owner: please review — new HU string, "What changed" block (§11.3
    // delta persistence, ingest v4), mirrors the EN block's pattern.
    whatChangedLabel: "Mi változott",
    // Owner: please review — new HU string, background primer disclosure
    // (PLAN.md §11.6 context mode), mirrors the EN block's pattern. "Háttér"
    // ("Background/context") — a plain, calm noun, no verb/CTA framing.
    arcContextLabel: "Háttér",
    // Owner: please review — new HU strings, big edition masthead issue
    // line (Front Page redesign), mirror the EN block's pattern. Hungarian
    // does not pluralize a noun after a numeral (same reasoning as
    // searchResultsOne above), so issueEditionsTodayOne is deliberately
    // identical to its plural twin.
    issueEdition: "{n}. szám",
    issueEditionsToday: "{n} kiadás ma",
    issueEditionsTodayOne: "{n} kiadás ma",
    // Owner: please review — new HU strings, about page (this feature),
    // mirror the EN block's pattern. Machine-drafted translation, not yet
    // read by a native speaker.
    aboutLabel: "Névjegy",
    aboutWhatTitle: "Miről van szó",
    aboutWhatBody:
      "Ez egy privát, automatikusan összeállított hírösszefoglaló, egy szűk körnek, akikkel a tulajdonos megosztja ezt a linket. Hat óránként egy folyamat összegyűjti az újdonságokat a tulajdonos saját forrásaiból — Telegram-csoportjaiból, X/Twitter-értesítéseiből, egy válogatott hírforrás-készletből (nemzetközi hírügynökségek és magyar hírportálok), a Redditből, előrejelzési piacokról és a Hacker Newsból —, egy AI szerkesztő pedig összefoglalóvá szerkeszti és rendezi őket. Minden állítás visszalinkel a forrására.",
    aboutRhythmTitle: "A ritmus",
    aboutRhythmBody:
      "Naponta négyszer érkezik egy ablak-összefoglaló, amely az előző óta történteket gyűjti össze. Minden este egy napi összefoglaló újra ellenőrzi a nap híreit a nyílt weben, és egy hitelesített összegzést ír. Vasárnaponként egy heti jelentés fogja össze a hetet.",
    aboutReadingTitle: "Hogyan olvasd",
    aboutReadingBody:
      "Minden összefoglaló témák szerinti címsorok alá van rendezve. Az állítások melletti kis számok hivatkozások — vidd rájuk az egeret, vagy koppints rájuk, hogy lásd, honnan származnak. A tovább fejlődő történeteknek saját sztori-oldaluk van, egy „eddig történt” hivatkozással elérve, hogy ne kelljen minden korábbi összefoglalót újraolvasnod. Ha inkább magyarul olvasnál, használd az EN/HU váltót a beállítások menüben.",
    aboutCaveat:
      "Ezen az oldalon minden szöveget egy AI ír, kizárólag a fent felsorolt forrásokból dolgozva — előfordulhat, hogy félreért egy forrást, vagy kihagy egy összefüggést. Ha valami fontos, kövesd a hivatkozást, és nézd meg magad.",
    // Owner: please review — new HU strings, PWA shell (this feature),
    // mirror the EN block's pattern. Machine-drafted translation, not yet
    // read by a native speaker. Note that pushFallbackTitle/Body are
    // currently UNREACHABLE in Hungarian by design (a service worker cannot
    // know the reader's language when it has just failed to reach the
    // site) — they are defined here so the vocabulary stays complete and so
    // PR B, which localizes the normal push path server-side, has them
    // ready.
    offlineTitle: "Nincs kapcsolat",
    offlineBody:
      "Ez az összefoglaló-archívum csak online érhető el — az eszközödön semmi nem tárolódik.",
    offlineRetry: "Újra",
    pushFallbackTitle: "Új összefoglaló",
    pushFallbackBody: "Koppints az olvasáshoz.",
    // Owner: please review — new HU strings, push notification settings row
    // (this feature), mirror the EN block's pattern. Machine-drafted
    // translation, not yet read by a native speaker.
    pushLabel: "Értesítések",
    pushEnable: "Bekapcsolás",
    pushEnabled: "Bekapcsolva",
    pushDisable: "Kikapcsolás",
    pushBlocked: "A böngésző letiltotta",
    pushIosHint: "Előbb add hozzá a kezdőképernyőhöz",
    pushFailed: "Nem sikerült bekapcsolni",
    // Deliberately NOT translated, and not an oversight: this file's own
    // header records that "digest #N" is one of the micro-labels the HU
    // pages leave in English. A notification that said "Összefoglaló #412"
    // while the page it opens says "digest #412" would be the two surfaces
    // disagreeing about the same object.
    pushWindowTitle: "digest",
  },
};
