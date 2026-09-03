export const CSS = `
  /* Two type roles, both zero-byte system stacks ("print poster" redesign):
     PROSE for the sit-back-and-read register (article body, TL;DR/leader,
     excerpts) — now the SAME sans stack as chrome, not a separate serif, so
     the whole site reads as one heavy-display/tight-tracked voice instead of
     newspaper-serif-vs-sans — and DATA for anything keyed on time (times,
     counts, datelines, citation chips, mono eyebrows). Time is this site's
     primary key; the typography should say so. */
  :root {
    /* The floating action button's size and its inset from the viewport
       corner. Custom properties rather than literals because .resumechip
       has to steer around this exact lane (see its bottom offset) and the
       two rules sit hundreds of lines apart — a 48px changed in one place
       and not the other is a silent overlap, not a visible error. */
    --fab-size: 48px;
    --fab-inset: 1.1rem;
    /* Owner-reported white flash when stepping between pages: every page
       is a fresh no-store document, and in the network gap before its
       first paint the browser shows its OWN canvas — which defaults to
       WHITE unless the page declares color-scheme. (The navigation
       crossfade usually hides the gap; a slow response outruns the
       snapshot, which is why the flash was only intermittent.) light dark
       lets the UA pick the canvas by OS preference — the Auto case; the
       data-theme override blocks below pin it to one scheme, keeping the
       between-pages canvas in lockstep with the manual theme choice. */
    color-scheme: light dark;
    /* "Print poster" redesign: retired the serif stack — every prose rule
       below (.digest p, .tldr/leader, .entry .excerpt, arc/delta text, …)
       flips to the same heavy sans voice as chrome just by this
       redefinition, no selector changes needed. */
    --font-prose: ui-sans-serif, system-ui, -apple-system, "Helvetica Neue", sans-serif;
    --font-data: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;

    --bg: #ffffff; /* paper */
    --page-bg: #eaedf3; /* the gutter the lifted desktop column sits on — see the >=52em block below */
    --card-shadow: 0 2px 4px rgba(16, 18, 21, 0.07), 0 18px 48px rgba(16, 18, 21, 0.13);
    --text: #101215; /* ink */
    --muted: #5b6270;
    --accent: #0e3fa9; /* press blue */
    --accent-strong: #0a2f80; /* darker blue — hover/strong state */
    --tldr-bg: #eaf0fc;
    --tldr-text: #16234f;
    --chip-bg: #eef1f7;
    --chip-text: #0e3fa9; /* = --accent: cite chips read as press-blue text */
    --hairline: #d8dbe2;
    --h2-border: #0e3fa9; /* = --accent */
    --attention-bg: #fef3c7;
    --attention-text: #78350f;
    /* New tokens (print poster): a heavier rule than --hairline for the
       masthead/section rules, the highlighter mark used inside headlines,
       and a faint mono tone one step quieter than --muted (folios,
       eyebrows). --mark-ink is deliberately identical in every theme copy —
       the mark itself is always a light chip, so its text always wants dark
       ink, never the theme's own --text. */
    --rule-heavy: #101215;
    --mark: #ffe14d;
    --mark-ink: #101215;
    --faint: #8a90a0;
    /* Article body tone (owner follow-up: headline/body/rule all at full
       ink read as one undifferentiated wall, especially dark) — one step
       quieter than --text, clearly brighter than --muted; strong/em inside
       prose stay full --text so the stats pop against it. */
    --prose: #3d4350;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #131418;
      --page-bg: #0a0b0e;
      --card-shadow: 0 2px 5px rgba(0, 0, 0, 0.55), 0 22px 56px rgba(0, 0, 0, 0.55);
      --text: #ecedf0;
      --muted: #9aa1af;
      --accent: #7d9bff;
      --accent-strong: #a8bdff;
      --tldr-bg: #1c2740;
      --tldr-text: #d7e2ff;
      --chip-bg: #1f2127;
      --chip-text: #7d9bff;
      --hairline: #2b2e36;
      --h2-border: #7d9bff;
      --attention-bg: #4d3800;
      --attention-text: #ffe69c;
      --rule-heavy: #ecedf0;
      --mark: #f5cf3a;
      --mark-ink: #101215;
      --faint: #767d8b;
      --prose: #c3c9d4;
    }
  }

  /* Manual theme override (roadmap step 6): a data-theme attribute on <html>,
     set by the early head script from localStorage, has to beat BOTH the
     base :root above and the prefers-color-scheme:dark block above it —
     regardless of which way the OS is set. CSS has no way to reference a
     media block's resolved values from outside it, so with no build step the
     only option is a third, explicit copy of each palette. Three copies is
     the price of a manual override without a build step, and the palette
     changes rarely. Deliberately only the THEME-DEPENDENT variables are
     duplicated — the colors, plus --card-shadow, whose blacks and alphas
     differ per theme (a light-theme shadow under a dark-theme card would
     be invisible, and vice versa). --font-prose/--font-data are identical
     in every theme and stay defined once, above. */
  :root[data-theme="dark"] {
    /* Pin the UA canvas too — see the color-scheme comment in :root. */
    color-scheme: dark;
    --bg: #131418;
    --page-bg: #0a0b0e;
    --card-shadow: 0 2px 5px rgba(0, 0, 0, 0.55), 0 22px 56px rgba(0, 0, 0, 0.55);
    --text: #ecedf0;
    --muted: #9aa1af;
    --accent: #7d9bff;
    --accent-strong: #a8bdff;
    --tldr-bg: #1c2740;
    --tldr-text: #d7e2ff;
    --chip-bg: #1f2127;
    --chip-text: #7d9bff;
    --hairline: #2b2e36;
    --h2-border: #7d9bff;
    --attention-bg: #4d3800;
    --attention-text: #ffe69c;
    --rule-heavy: #ecedf0;
    --mark: #f5cf3a;
    --mark-ink: #101215;
    --faint: #767d8b;
    --prose: #c3c9d4;
  }
  :root[data-theme="light"] {
    /* Pin the UA canvas too — see the color-scheme comment in :root. */
    color-scheme: light;
    --bg: #ffffff;
    --page-bg: #eaedf3;
    --card-shadow: 0 2px 4px rgba(16, 18, 21, 0.07), 0 18px 48px rgba(16, 18, 21, 0.13);
    --text: #101215;
    --muted: #5b6270;
    --accent: #0e3fa9;
    --accent-strong: #0a2f80;
    --tldr-bg: #eaf0fc;
    --tldr-text: #16234f;
    --chip-bg: #eef1f7;
    --chip-text: #0e3fa9;
    --hairline: #d8dbe2;
    --h2-border: #0e3fa9;
    --attention-bg: #fef3c7;
    --attention-text: #78350f;
    --rule-heavy: #101215;
    --mark: #ffe14d;
    --mark-ink: #101215;
    --faint: #8a90a0;
    --prose: #3d4350;
  }

  * { box-sizing: border-box; }
  /* The hidden attribute must actually hide, whatever else is styled.
     Author rules beat the UA stylesheet's own hidden-means-display-none
     rule regardless of specificity, so ANY element this file gives an
     explicit display to stayed VISIBLE when the scripts below set
     el.hidden = true. That silently broke two features: the unread fence
     (patched at the time with a one-off selector) and then the index
     filter, where .entry's own display: block meant a filtered-out entry
     was marked hidden in the DOM and still painted on screen — typing in
     the filter appeared to do nothing at all (owner-reported). One global
     override kills the whole class of bug instead of one selector at a
     time, and !important is what makes it beat the author display rules it
     exists to correct (normalize.css ships the same rule for the same
     reason). Everything toggled by the hidden attribute — the filter
     input, the theme toggle, entries, day headers, the
     fence — is covered by this one line. NOTE: no backticks in this
     comment; the whole CSS block is a JS template literal. */
  [hidden] { display: none !important; }
  /* Reserve the scrollbar's gutter even when the page is too short to
     scroll: the All view scrolls, a near-empty Daily view doesn't, and
     without this the viewport width changes on switch — sliding the
     centered bubble sideways by half a scrollbar (owner-reported). A
     no-op on overlay-scrollbar platforms, which never had the shift. */
  html { scrollbar-gutter: stable; }
  /* Second layer of the anti-flash fix (see :root's color-scheme comment):
     an explicit root background so overscroll and any pre-body-paint gap
     show the theme's own paper/ink, never the UA default. The lifted
     desktop column overrides this to --page-bg below. */
  html { background: var(--bg); }
  /* A single unbreakable token wider than a phone screen (production
     digests carry them — a defanged URL from the link allowlist is one
     long word) widens the LAYOUT viewport past the visual one. On iOS
     Safari that detaches position:fixed elements from the screen edge
     (the back button floats mid-page) and opens pannable blank space
     past the footer (owner-reported, 2026-08-09). Two guards:
     overflow-wrap (inherited everywhere from body) breaks such tokens at
     the container edge, and overflow-x: clip caps the layout viewport at
     device width even if some future shape still overflows — clip, not
     hidden, so html doesn't become a scroll container. */
  html { overflow-x: clip; }
  /* Smooth-scroll the TOC's #sN anchor jumps (roadmap step 5), gated behind
     prefers-reduced-motion so motion-sensitive readers get the instant jump
     instead. */
  @media (prefers-reduced-motion: no-preference) {
    html { scroll-behavior: smooth; }
    /* MPA view transitions (roadmap 2 step 7): one at-rule turns on the
       browser's default crossfade between full-page navigations on this
       origin; browsers without support (most, today) simply ignore an
       at-rule they don't recognize — progressive, no fallback needed. No
       custom ::view-transition-* choreography — ambient feel, not a show
       (restraint, matching the "keep the default crossfade" decision). A
       crossfade IS motion, so it gets the exact same reduced-motion gate as
       scroll-behavior above, not a separate one. */
    @view-transition {
      navigation: auto;
    }
  }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    line-height: 1.6;
    font-size: 17px;
    overflow-wrap: break-word; /* inherited: see the overflow-x note above */
  }
  /* Text size (owner upgrade): S/M/L scales the READING text only — the
     article body (.digest, which carries the TL;DR callout and section
     h2s proportionally inside it) and the index/search excerpts. The
     first cut scaled the whole body instead and the owner immediately
     flagged it: the entire UI zoomed, which reads as a broken viewport,
     not a text-size preference — chrome (masthead, tabs, pills, meta
     rows) must hold its owner-tuned rhythm while only the prose moves.
     em factors, not px, so the 17px/15px desktop/phone bases scale
     without a second media-scoped set of rules. M stays the ABSENCE of
     data-fontsize: the defaults above remain the single source of truth,
     s/l are offsets from them, never a competing "normal". The
     .entry-lead .excerpt variants exist because these :root-prefixed
     rules outrank the lead card's own base font-size — without them,
     compact S would shrink the lead below its deliberate extra weight. */
  /* Body-font toggle (owner-requested; a friend lobbied for the serif):
     Serif resurrects the retired "private wire desk" prose stack by
     re-pointing the ONE variable every prose rule reads — headlines,
     eyebrows, and mono data are untouched, so the print-poster identity
     keeps its display voice either way. Sans is the default, expressed as
     the ABSENCE of data-font, same convention as data-fontsize's M. */
  :root[data-font="serif"] {
    --font-prose: ui-serif, "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  }
  :root[data-fontsize="s"] .digest { font-size: 0.94em; }
  :root[data-fontsize="l"] .digest { font-size: 1.12em; }
  :root[data-fontsize="s"] .entry .excerpt { font-size: 0.87em; }
  :root[data-fontsize="l"] .entry .excerpt { font-size: 1.04em; }
  :root[data-fontsize="s"] .entry-lead .excerpt { font-size: 0.96em; }
  :root[data-fontsize="l"] .entry-lead .excerpt { font-size: 1.14em; }
  a { color: var(--accent); }
  /* overflow-x: clip HERE, on a non-root element, is the actual guarantee
     against the phone layout-viewport bug (owner-reported twice,
     2026-08-09): root-level clip demonstrably does NOT stop wide content
     from expanding the layout viewport (measured: one long pre line took a
     375px viewport to 1500px with the html rule in place), which detaches
     fixed elements and opens pannable dead space past the footer on iOS.
     Clipping inside .wrap means overflow can never widen the page geometry
     again, whatever content shape causes it next. Sticky day headers keep
     working (clip creates no scroll container) and the fixed back button is
     unaffected (no containing-block change). */
  .wrap { max-width: 42em; margin: 0 auto; padding: 0 1.25em 4em; overflow-x: clip; }

  /* Masthead ("print poster" redesign): brand zone left, the view-tab
     capsule as the mast's own MIDDLE flex child, gear zone right (see
     pageChrome). The two side zones carry flex: 1 1 0 so they grow equally
     from nothing — that equal growth is what centers the capsule on the
     row's true midpoint instead of on the leftover space after a wider
     brand. ONE masthead for every page (owner follow-up — it must not
     change shape between the index and a digest page; see pageChrome):
     display-scale brand left, tabs centered, search/settings right, a
     heavy 4px rule underneath (rule weight matches .digest h2's own top
     rule and the colophon's — one "heavy rule = structural divider"
     vocabulary across the page). Desktop is one nowrap row; the ≤40em
     block below keeps it one line at phone sizes too. */
  header.mast {
    display: flex; align-items: center;
    gap: 0.6em 1em; padding: 1.1em 0 0.9em;
    border-bottom: 4px solid var(--rule-heavy); margin-bottom: 1.6em;
  }
  /* True centering: the two side zones get equal flex-grow from a zero
     basis, so the capsule centers on the ROW's midpoint, not on whatever
     space the brand happens to leave over (the phone block below swaps
     this for content-width zones — see its own comment). */
  .mast .mastleft { display: flex; align-items: center; flex: 1 1 0; min-width: 0; }
  /* 800-weight, tight-tracked display type (print-poster identity) in place
     of the old 700/-0.01em body-adjacent wordmark — the brand is now styled
     the same register as every other headline on the site, just at chrome
     scale. Uppercase, matching the big masthead's brand below (.mast-big
     .brand) at a fraction of the size, so the two masthead sizes read as
     one family, not two different logotypes. */
  .mast .brand { font-weight: 800; font-size: 0.95em; letter-spacing: -0.02em; text-transform: uppercase; text-decoration: none; color: var(--text); }
  /* The settings gear (language/theme/size/density, collapsed into one
     details.settings disclosure — see renderSwitchers) sits top-right in
     the masthead via .mastright, right-aligned — same markup at both
     breakpoints and in both masthead layouts (compact and .mast-big both
     use the identical three-zone row, see pageChrome). */
  .mastright {
    display: flex; align-items: center; justify-content: flex-end;
    gap: 0.55em; flex: 1 1 0;
  }

  .mast .langswitch { font-size: 0.85em; font-variant-numeric: tabular-nums; }
  .mast .langswitch a { text-decoration: none; }
  .mast .langswitch strong { color: var(--text); }

  /* Big edition masthead ("mast-big", hardcoded on every page since the
     one-masthead follow-up): the mono issue line sits ABOVE the masthead
     as its own quiet block, and the masthead itself is the SAME three-zone
     row as every other page — brand left, view-tab capsule centered by the
     equal-growth side zones, search/settings right — just with the brand
     at display scale ("NEWS", the bare first host label; the .tld tail was
     dropped with the two-tone treatment). One structure, one CSS block,
     two brand sizes. */
  .issueline {
    font-family: var(--font-data); font-size: 0.66em;
    letter-spacing: 0.11em; text-transform: uppercase; color: var(--muted);
    padding-top: 1.4em;
  }
  /* Slimmer top padding only when an issue line sits above (index/digest);
     a page without one (arc, search) keeps the base masthead padding so the
     brand never crowds the viewport edge. */
  .issueline + header.mast { padding-top: 0.5em; }
  .mast-big { padding-bottom: 0.7em; }
  .mast-big .brand {
    font-size: clamp(1.5em, 4.5vw, 2.1em);
    letter-spacing: -0.03em; line-height: 0.98;
  }

  /* Settings bubble (owner redesign): the gear button collapses language,
     theme, and density into one disclosure. details.settings — NOT
     .mastright — is the positioning anchor for .settingspanel below.
     (Historically load-bearing: the phone block used to erase .mastright
     as a box via display: contents, so only the <details> could anchor the
     absolute panel. The masthead compaction removed that collapse, but the
     anchor choice stays — it was never wrong, and moving it buys nothing.) */
  details.settings { position: relative; }
  /* "Print poster" controls: 1px var(--rule-heavy) border, mono uppercase
     label — the toolbtn recipe shared with summary.searchtoggle below on the
     DESKTOP, where both are text chips ("SEARCH", "SETTINGS"). Still two
     rules rather than one merged selector, because the PHONE pulls them
     apart again: there both collapse to bare, borderless, oversized icons,
     and they get there from different starting metrics (a mono glyph vs an
     svg). See the phone block below summary.searchtoggle.
     Pill-rounded (owner-requested exception to the redesign's otherwise
     square-cornered rule): every INTERACTIVE control — buttons, segmented
     capsules, the FAB — keeps the old 999px pill shape; panels, chips,
     badges and rules stay square, so the poster identity lives in the
     surfaces while the controls stay obviously pressable. */
  summary.gear {
    list-style: none;
    background: none; border: 1px solid var(--rule-heavy); border-radius: 999px;
    color: var(--text); font-family: var(--font-data); font-size: 0.62em;
    letter-spacing: 0.08em; text-transform: uppercase;
    padding: 0.4em 0.75em; cursor: pointer;
  }
  /* iOS Safari draws its own disclosure triangle on <summary> even with
     list-style: none — this is the belt-and-suspenders rule that actually
     suppresses it. */
  summary.gear::-webkit-details-marker { display: none; }
  /* Desktop shows the gear WITH its text label ("Settings"/"Beállítások" —
     owner-requested); the phone masthead is tight, so the label collapses
     there and the icon stands alone (see the mobile block below). */
  /* Desktop: text-only trigger (owner follow-up) — the glyph half of the
     summary is hidden here and shown by the phone block below, where the
     LABEL half hides instead. */
  .gearicon { display: none; }
  /* Quiet hover (owner: no full fill that "highlights all the text" — just
     a light ring around it): the pill border and text pick up the accent,
     background stays put. Same recipe on every toolbar control below
     (searchtoggle/densitytoggle/searchbtn). The OPEN state keeps a solid
     accent fill — that's a state, not a hover, and it matches the active
     view tab. */
  summary.gear:hover { border-color: var(--accent); color: var(--accent); }
  summary.gear:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }
  details.settings[open] > summary.gear { background: var(--accent); border-color: var(--accent); color: var(--bg); }
  .settingspanel {
    position: absolute; right: 0; top: calc(100% + 0.5em);
    /* Must clear the sticky day headers (.dayhead, z-index: 1) or the panel
       would open underneath the ledger once the reader has scrolled. */
    z-index: 20;
    /* Simple soft bubble (owner follow-up: the 2px ink border + hard offset
       shadow read too heavy, especially in dark mode) — hairline border,
       soft drop shadow, card radius. Same recipe as .searchpanel below. */
    background: var(--bg); border: 1px solid var(--hairline);
    border-radius: 14px;
    padding: 1em 1.1em;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28);
    display: flex; flex-direction: column; gap: 0.7em;
    /* Size to CONTENT, not to the anchor (owner-reported bug: the panel is
       absolutely positioned off the tiny details.settings anchor, so
       shrink-to-fit bottomed out at the old min-width — 13em — which the
       body-font row outgrew: "BODY FONT" wrapped to two lines and the
       CLASSIC segment clipped at the panel edge on desktop, where the em
       base is larger. max-content lets the widest row set the panel;
       min-width keeps short-rowed panels (HU has fewer long rows) from
       looking skeletal; the viewport cap keeps phones safe, with the
       label allowed to wrap again only in that capped case. */
    width: max-content; min-width: 13em; max-width: calc(100vw - 2.5em);
  }
  .settingsrow { display: flex; justify-content: space-between; align-items: baseline; gap: 1.2em; }
  /* No-JS resilience: theme/text-size/density controls are server-rendered
     with the hidden attribute and revealed by the bottom script (the
     progressive-enhancement contract, see renderSwitchers). Without JS the
     row was a label next to an EMPTY pill border — a bright "dot" once the
     border went --rule-heavy (owner-reported from a scripts-blocked
     preview). Hide any settings row with no visible control at all: the
     language row keeps its plain <a> links and stays; rows whose only
     controls are still [hidden] disappear until the script reveals them.
     Browsers without :has() just keep the old harmless empty-pill look. */
  .settingsrow:not(:has(a, button:not([hidden]), .pushnote:not([hidden]))) { display: none; }
  .settingslabel {
    font-family: var(--font-data); font-size: 0.7em; text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--faint);
  }

  /* Settings bubble open/close animation (owner-requested). details/summary
     has no transition of its own to hook, so this is a fresh keyframe pair
     rather than a transition. A NEW prefers-reduced-motion: no-preference
     gate — not the global one near the top of this stylesheet, which is
     scroll/view-transition territory and unrelated to this feature.
     Opening plays on details.settings[open] .settingspanel directly (native
     open needs no JS). Closing plays on a "panelclosing" class the
     settings-close IIFE below adds before it sets details.open = false
     itself — <details> snaps shut instantly with no hook to intercept, so
     the class is what buys the mirrored animation time to play before
     removal. Named "panelclosing", not the shorter "closing" the owner's
     brief used, because .closing already exists on this page (the digest
     article's closing-line paragraph, below) — reusing that name would
     have leaked its border/italic/spacing styling onto the settings panel
     for the animation's duration. The close rule below repeats the [open]
     prefix (not just .settingspanel.panelclosing) SPECIFICALLY so it
     outranks the open rule above on specificity: the "panelclosing" class
     is added while open is still true — the JS only flips open to false at
     the end of the delay — so both rules target the same element at once,
     and without the matching prefix the open rule's animation would win by
     cascade order and the close animation would never actually play. */
  @media (prefers-reduced-motion: no-preference) {
    details.settings[open] .settingspanel { animation: settingsopen 160ms ease-out; }
    details.settings[open] .settingspanel.panelclosing { animation: settingsclose 120ms ease-in; }
    @keyframes settingsopen {
      from { opacity: 0; transform: translateY(-4px) scale(0.98); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    @keyframes settingsclose {
      from { opacity: 1; transform: translateY(0) scale(1); }
      to { opacity: 0; transform: translateY(-4px) scale(0.98); }
    }
  }

  /* Density toggle (roadmap 4 step 4): small pill button living inside the
     settings bubble's panel (see .settingspanel above). hidden by default,
     un-hidden by the bottom script — no JS, no button, same progressive-
     enhancement contract as the index filter input below. Theme and text
     size (owner upgrade) moved off this single-pill look onto the miniseg
     control just below — density stays a pill since it's genuinely binary
     (compact/comfortable), not a 3-way choice. */
  .densitytoggle, .pushtoggle {
    background: none; border: 1px solid var(--rule-heavy); border-radius: 999px;
    color: var(--text); font-family: var(--font-data); font-size: 0.75em; padding: 0.15em 0.55em; cursor: pointer;
  }
  .densitytoggle:hover, .pushtoggle:hover { border-color: var(--accent); color: var(--accent); }
  .densitytoggle:focus-visible, .pushtoggle:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }
  /* Push notifications (PLAN.md §11.7): the toggle is the density pill's
     primitive reused, not a new one — same shape, same hover, same focus
     ring, inherited by joining the selectors above rather than copied.
     The ON state fills the pill so "is it on" is answerable at a glance
     rather than by reading the word inside it. .pushnote is the text-only
     state (an iOS reader who has not installed to the Home Screen yet, a
     browser-level block): no border, because it is a statement rather than
     a control, and nothing about it should invite a tap. */
  .pushtoggle[aria-pressed="true"] { border-color: var(--accent); background: var(--accent); color: #fff; }
  .pushnote {
    font-family: var(--font-data); font-size: 0.7em; color: var(--faint); text-align: right;
  }

  /* Mini segmented control (three-state theme, S/M/L text size) — the view
     tabs' segmented language (.viewtabs/.viewtab above) miniaturized to
     panel scale, same pill-capsule/bordered recipe, so the settings bubble
     reads as one family with the site's primary navigation instead of
     inventing a new shape. */
  .miniseg {
    display: inline-flex; border: 1px solid var(--rule-heavy);
    /* Pill capsule (owner-requested, see summary.gear's comment); overflow
       hidden clips the active segment's fill to the rounded ends. */
    border-radius: 999px; overflow: hidden;
  }
  /* Belt and suspenders for the capsule ends (owner-reported from Safari:
     an active END segment's fill poked square corners past the capsule's
     curve — Safari doesn't reliably clip children to a rounded inline-flex
     container). The end segments carry their own matching radii, so the
     fill is rounded at the source and no longer depends on the parent's
     overflow clip. Same treatment on .viewtab below. */
  .minisegbtn:first-child { border-radius: 999px 0 0 999px; }
  .minisegbtn:last-child { border-radius: 0 999px 999px 0; }
  .minisegbtn {
    background: none; border: none; color: var(--muted);
    font-family: var(--font-data); font-size: 0.68em; letter-spacing: 0.06em; text-transform: uppercase;
    padding: 0.3em 0.65em; cursor: pointer;
  }
  .minisegbtn + .minisegbtn { border-left: 1px solid var(--hairline); }
  .minisegbtn.active { background: var(--accent); color: var(--bg); }
  .minisegbtn:not(.active):hover { background: var(--tldr-bg); }
  .minisegbtn:focus-visible { outline: 2px solid var(--text); outline-offset: -2px; }

  /* ⌘K command palette (§11.1 PR C): markup is injected at runtime (see the
     palette IIFE in pageChrome, below) — no server-rendered dialog HTML, so
     everything it needs lives here. Deliberately NOT a card — one dialog,
     thin hairlines, mono input, no drop-shadow-heavy chrome (design
     guidance: "no card soup"). Dark-first like the rest of the site: it
     reads off the same --bg/--text/--muted/--hairline/--accent tokens as
     everything else, so it never needs its own light/dark handling. Hidden
     via the plain [hidden] attribute (see the global rule above), same
     progressive-enhancement contract as the filter input/theme toggle. */
  .cmdpalette-backdrop {
    position: fixed; inset: 0; z-index: 40;
    background: rgba(0, 0, 0, 0.55);
    display: flex; justify-content: center; align-items: flex-start;
    padding: 12vh 1em 0;
  }
  .cmdpalette {
    width: min(34em, 100%); max-height: 70vh;
    /* Simple soft bubble, same family as .settingspanel/.searchpanel (owner
       follow-up) — deeper shadow than the small panels since it floats over
       a dimmed backdrop, not beside its trigger. */
    background: var(--bg); border: 1px solid var(--hairline);
    border-radius: 14px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
    display: flex; flex-direction: column; overflow: hidden;
  }
  @media (prefers-reduced-motion: no-preference) {
    .cmdpalette { animation: cmdpaletteopen 120ms ease-out; }
    @keyframes cmdpaletteopen {
      from { opacity: 0; transform: translateY(-6px); }
      to { opacity: 1; transform: translateY(0); }
    }
  }
  .cmdpalette-input {
    font-family: var(--font-data); font-size: 1em; color: var(--text);
    background: none; border: none; border-bottom: 1px solid var(--hairline);
    padding: 0.85em 1em; outline: none;
  }
  .cmdpalette-input::placeholder { color: var(--muted); }
  .cmdpalette-list { overflow-y: auto; padding: 0.35em 0; }
  .cmdpalette-item {
    padding: 0.55em 1em; font-size: 0.93em; color: var(--text);
    font-family: var(--font-prose); cursor: pointer;
    display: flex; justify-content: space-between; gap: 1em;
  }
  .cmdpalette-item .cmdpalette-group {
    font-family: var(--font-data); font-size: 0.72em; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--muted); align-self: center;
  }
  .cmdpalette-item.active { background: var(--tldr-bg); }
  .cmdpalette-empty { padding: 0.85em 1em; font-size: 0.9em; color: var(--muted); }

  /* Ledger grid ("print poster" redesign, index pages): the <section> the
     lead card + day-grouped entries render inside (see renderIndexPage)
     becomes a two-column grid — single column at/under 640px. Day headers,
     the lead card, and the two client-inserted elements that can land as
     the ledger's own DOM siblings (.unreadfence, the lazily-created
     .empty "nothing matches" message — see the filter IIFE) all carry
     grid-column: 1 / -1 below so they span both columns as full-width
     dividers/rows regardless of where CSS auto-placement would otherwise
     put them; harmless (a no-op) on every OTHER page these same classes
     render on, none of which puts them inside a grid parent. Nothing here
     changes DOM structure or sibling order, so the unread-fence/filter
     IIFEs' own sibling-walking logic (nextElementSibling chains) is
     completely unaffected — see pageChrome's bottom script. */
  section[data-unread-label] {
    display: grid; grid-template-columns: 1fr 1fr; column-gap: 2.2em;
  }
  /* Every index view now uses the two-column grid above. It briefly did not:
     the daily and weekly views were dropped to a single column (a
     data-ledger="single" attribute, since removed) after the owner reported
     "the text has just half the width". The real cause was not the column
     count but the DAY HEADERS -- those views yield one card per day-group, so
     each header forced a new row and left a ~26em card beside a permanently
     empty cell. Removing the headers on those views instead (see
     renderLedgerFor in render-index.js) lets their cards flow two-across and
     fill the grid, which is what the owner asked for on 2026-08-27; each card
     carries its own short date now, so nothing is lost with the header. */
  .dayhead {
    grid-column: 1 / -1;
    font-size: 0.7em; text-transform: uppercase; letter-spacing: 0.11em;
    color: var(--faint); margin: 1.8em 0 0.3em; font-weight: 400;
    font-family: var(--font-data); /* mono uppercase eyebrow = the wire look */
    /* Sticky so mid-scroll position is always visible (roadmap step 4).
       var(--bg) background keeps entry text from showing through as it
       scrolls underneath. */
    position: sticky; top: 0; background: var(--bg); padding: 0.35em 0;
    z-index: 1;
  }
  .entry {
    display: block; text-decoration: none; color: inherit;
    padding: 1.1em 0 1.3em; border-bottom: 1px solid var(--hairline);
  }
  /* Headline ("print poster" redesign): every index card — lead and grid —
     gets a derived display headline (see deriveHeadline), heavy/tight like
     every other headline on the site. The hero's own h2.headline-lead runs
     larger (set below, alongside .entry-lead); a bare h3.headline is the
     grid-card size. mark (highlighter accent) is wired up here even though
     the mechanical deriveHeadline() never emits one today — see the
     function's own comment. */
  .headline {
    margin: 0 0 0.4em; font-weight: 800; letter-spacing: -0.02em;
    line-height: 1.15; color: var(--text); text-wrap: balance;
  }
  .headline mark { background: var(--mark); color: var(--mark-ink); padding: 0 0.14em; }
  /* Quiet card hover (owner follow-up: recoloring the WHOLE card — headline
     AND excerpt — on hover read as a giant highlight, and stuck after taps
     on touch): body text never changes; only the headline picks up a thin
     accent underline, and only where a real hover pointer exists — the
     hover: hover gate keeps touch taps from painting a sticky hover state
     at all. Keyboard focus gets the same underline OUTSIDE the gate (a
     keyboard is not a hover pointer) on top of the outline below. */
  @media (hover: hover) {
    .entry:hover .headline {
      text-decoration: underline; text-decoration-color: var(--accent);
      text-decoration-thickness: 0.06em; text-underline-offset: 0.12em;
    }
  }
  .entry:focus-visible .headline {
    text-decoration: underline; text-decoration-color: var(--accent);
    text-decoration-thickness: 0.06em; text-underline-offset: 0.12em;
  }
  .entry:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
  /* Lead card (the newest digest in the current view): a full-width hero
     spanning both grid columns, mono eyebrow + big headline + deck excerpt
     on the left, an items/sections facts column on the right (task spec:
     "if cheap" — both counts are already selected columns, no extra
     query). Single column under 640px, facts row moves below the deck. */
  .entry-lead {
    grid-column: 1 / -1;
    display: grid; grid-template-columns: 1fr auto; gap: 2em; align-items: start;
    padding: 1.3em 0 1.6em; border-bottom: 1px solid var(--rule-heavy);
  }
  .entry-lead .headline-lead { font-size: clamp(1.5em, 4.2vw, 2.1em); line-height: 1.08; }
  .entry-lead .eyebrow-text {
    font-family: var(--font-data); font-size: 0.68em; letter-spacing: 0.1em;
    color: var(--accent); text-transform: uppercase;
  }
  .leadfacts { border-left: 1px solid var(--hairline); padding-left: 1.6em; align-self: start; }
  .leadfacts dt {
    font-family: var(--font-data); font-size: 0.6em; letter-spacing: 0.1em;
    text-transform: uppercase; color: var(--faint); margin-top: 0.9em;
  }
  .leadfacts dt:first-child { margin-top: 0; }
  .leadfacts dd { margin: 0; font-size: 1.3em; font-weight: 750; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
  .entry .meta {
    display: flex; align-items: baseline; gap: 0.7em; margin-bottom: 0.4em;
    font-variant-numeric: tabular-nums;
  }
  /* 0.85em, not 0.95: mono runs wide, so the time nudges down to keep its
     old visual weight in the meta row now that it's set in --font-data. */
  .entry .time { font-weight: 700; font-size: 0.85em; font-family: var(--font-data); }
  /* Daily-brief entries carry the accent on their time instead of the
     default text color — the "slightly heavier presence" this one entry
     type gets in an otherwise undifferentiated list. */
  .entry .time.time-accent { color: var(--accent); }
  /* 0.75em, not 0.8: same mono-runs-wide compensation as .entry .time. */
  .entry .count { color: var(--muted); font-size: 0.75em; font-family: var(--font-data); }
  .entry .flag {
    font-size: 0.68em; font-weight: 400; font-family: var(--font-data); letter-spacing: 0.04em;
    /* Rounded tag (owner follow-up — tags join the rounded family with the
       controls; only rules/panels/structural chrome stay square). */
    padding: 0.15em 0.55em; border-radius: 99px;
    background: var(--attention-bg); color: var(--attention-text);
    /* Two-word badges ("weekly report", "daily brief") were wrapping into
       two-line pills in the lead card's meta row (owner-reported from the
       first live weekly). A badge is a tag, not a paragraph — one line,
       always. The meta row itself stays nowrap: the pill's min-content
       width just wins, and the eyebrow text (which wraps internally)
       absorbs the squeeze; .wrap's overflow-x clip guards the extreme. */
    white-space: nowrap;
  }
  /* Neutral/muted variant, reused by two chips: the "EN" fallback note on
     untranslated HU index entries, and the degraded-run badge (roadmap 2
     step 8, renderDegradedBadge) — neither is a warning-colored call to
     action, just metadata about the entry; the degraded badge's own ⚠
     prefix (baked into the string, not CSS) is what tells the two apart.
     Reuses .flag's shape/sizing. */
  .entry .flag.flag-muted, .entry .flag.flag-degraded { background: var(--chip-bg); color: var(--chip-text); }
  /* Daily-brief badge — same chip-bg/chip-text tokens as .flag-muted, but
     filled/inverted (solid, not the soft pastel) so it reads as its own
     distinct badge rather than the muted EN language note, and stays
     clearly apart from the amber attention pill. */
  .entry .flag.flag-daily { background: var(--chip-text); color: var(--chip-bg); }
  .entry .excerpt {
    margin: 0; color: var(--muted); font-size: 0.88em;
    font-family: var(--font-prose); line-height: 1.55;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }
  /* Daily-brief entries summarize a whole day, not a 3-hour window — one
     extra clamped line of excerpt room. */
  .entry .excerpt.excerpt-daily { -webkit-line-clamp: 4; }
  .entry .excerpt strong { color: var(--text); }
  /* Placed after .entry .excerpt (same specificity, later wins the cascade)
     so the lead card's excerpt actually loses its clamp instead of being
     silently overridden back to 3 lines. */
  .entry-lead .excerpt {
    font-size: 0.98em; display: block; -webkit-line-clamp: unset; overflow: visible;
  }

  /* Ledger density toggle (roadmap 4 step 4): compact tightens the ledger's
     .entry padding and excerpt clamp when data-density="compact" is set
     (persisted in localStorage, applied pre-paint by the head script, same
     pattern as data-theme). Scoped to .entry:not(.entry-lead) — the lead
     card is the day's headline, not ledger noise, and compaction is for the
     ledger only; without :not() this compact clamp would win on specificity
     over .entry-lead .excerpt's own un-clamp above, since both come later in
     the cascade than plain .entry .excerpt. */
  :root[data-density="compact"] .entry:not(.entry-lead) { padding: 0.55em 0; }
  :root[data-density="compact"] .entry:not(.entry-lead) .excerpt { -webkit-line-clamp: 2; }
  :root[data-density="compact"] .entry:not(.entry-lead) .excerpt.excerpt-daily { -webkit-line-clamp: 3; }

  /* The whole nav is prev/next times plus the "all digests" link — one word
     — so the entire block goes mono rather than singling out the times.
     "Crumbs" in the print-poster redesign (task spec's digest-page nav):
     same markup/hrefs, just mono uppercase with a hairline bottom rule in
     place of the old bare flex row. */
  nav.digestnav {
    display: flex; justify-content: space-between; gap: 1em;
    font-size: 0.68em; letter-spacing: 0.08em; text-transform: uppercase;
    padding: 0.6em 0; margin-bottom: 1.8em; border-bottom: 1px solid var(--hairline);
    font-family: var(--font-data);
  }
  nav.digestnav a { text-decoration: none; color: var(--muted); }
  nav.digestnav a:hover, nav.digestnav a:focus-visible { color: var(--accent); outline: none; }
  nav.digestnav .spacer { flex: 1; }
  /* Bottom mirror of the same nav, after </article> (roadmap step 2) — reads
     as a continuation of the article's closing line, not a new nav block:
     same top-hairline + padding treatment as .closing, font-size/behavior
     otherwise identical to the top nav above. */
  nav.digestnav.digestnav-bottom {
    margin-top: 2.5em; padding-top: 1em; border-top: 1px solid var(--hairline); border-bottom: 0;
  }
  /* Edition eyebrow (digest page): kind · time · items · sections, mono
     uppercase, --accent — sits directly above the derived h1 headline (see
     renderDigestPage/deriveHeadline). font-variant-numeric dropped: --font-
     data is monospace, so digits are already fixed-width. */
  .stamp {
    color: var(--accent); font-size: 0.66em; margin: 0 0 0.6em; font-family: var(--font-data);
    text-transform: uppercase; letter-spacing: 0.1em; font-weight: 400;
  }
  /* Derived h1 headline (digest page) — same heavy/tight display voice as
     .headline (index cards), just bigger; kept as its own rule rather than
     reusing .headline's class since h1 needs no hover-color coupling to a
     parent .entry link the way index cards do. */
  .edhead h1 {
    margin: 0 0 0.8em; font-size: clamp(1.6em, 5vw, 2.3em); font-weight: 800;
    letter-spacing: -0.03em; line-height: 1.08; text-wrap: balance; color: var(--text);
  }
  .edhead h1 mark { background: var(--mark); color: var(--mark-ink); padding: 0 0.14em; }
  .edhead { margin-bottom: 1.2em; }
  /* Index empty-state message — its own class, NOT .stamp: the stamp is now
     the digest page's uppercase wire eyebrow above, and "No digests yet."
     must stay quiet muted prose, not a shouted header. */
  .empty { grid-column: 1 / -1; color: var(--muted); font-size: 0.85em; margin: 2em 0; }
  /* HU digest page, no body_html_hu on file: shown above the article,
     falling back to the English body. */
  .en-only-note { color: var(--muted); font-size: 0.85em; font-style: italic; margin: 0 0 1em; }
  /* Arc page title (§11.1 PR A, renderArcPage) — like the digest page's own
     derived h1 (.edhead h1 above), an editorial per-page headline rather
     than the masthead brand link every OTHER page (index, search) still
     uses in place of a real h1. Same heavy/tight display voice as the rest
     of the "print poster" redesign, not a separate serif register. */
  .archead { display: flex; align-items: baseline; flex-wrap: wrap; gap: 0.7em; }
  .arctitle {
    font-weight: 800; letter-spacing: -0.025em; font-size: 1.7em;
    line-height: 1.15; margin: 0 0 0.5em; text-wrap: balance;
  }
  /* Follow toggle (§11.2, optional feature): client-injected into .archead,
     next to the arc title — see the follow-toggle IIFE in pageChrome. Text
     control per the design guidance, deliberately no button chrome (no
     border/background/pill) — same restraint as the design guidance's
     "calm urgency" principle applied to a control instead of a status. Mono
     metadata register (matches .nowmeta/.catchup) rather than the h1's
     editorial serif, so it visually reads as interface, not headline. */
  .followtoggle {
    background: none; border: 0; padding: 0; margin: 0 0 0.5em;
    font-family: var(--font-data); font-size: 0.75em; color: var(--muted);
    cursor: pointer;
  }
  .followtoggle:hover, .followtoggle:focus-visible { color: var(--accent); }
  .followtoggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  /* Background primer disclosure (§11.6 context mode, renderArcContext):
     native <details>/<summary>, collapsed by default, sitting directly under
     the title/metadata line and above the appearances timeline. No card
     (design guidance) — a thin bottom hairline is the only separator, same
     "typography, not chrome" recipe as .deltas just below. Summary text is
     in the mono metadata register (matches .archivelabel/.followtoggle), NOT
     the h1's editorial serif — this is an interface control revealing
     prose, not a second headline. Body paragraphs reuse .digest p's own
     serif/line-height but in --muted rather than --text: durable background
     reads one register quieter than the article itself (design guidance:
     "prose register matching the article body, muted"). */
  .arccontext { margin: 0 0 1.6em; padding-bottom: 1.2em; border-bottom: 1px solid var(--hairline); }
  .arccontext summary {
    cursor: pointer; font-family: var(--font-data); font-size: 0.75em; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.08em;
  }
  .arccontext summary:hover, .arccontext summary:focus-visible { color: var(--accent); }
  .arccontext summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .arccontextbody { margin-top: 0.9em; }
  .arccontextbody p {
    margin: 0 0 0.9em; font-family: var(--font-prose); font-size: 0.95em;
    line-height: 1.65; color: var(--muted);
  }
  .arccontextbody p:last-child { margin-bottom: 0; }
  /* Section index ("in this edition", roadmap step 5 / print-poster
     redesign): a numbered two-column grid built from the article's own
     <h2>s at render time (see buildSectionToc/renderToc) — replaces the old
     pill-chip row. Each entry is now "NN  Title", the number in
     .toclabel, --accent, mono — same numbering voice as .digest h2's own
     CSS-counter badges below, so the index and the article agree on how
     each section is numbered without the two having to share markup. */
  .toc {
    display: grid; grid-template-columns: 1fr 1fr; gap: 0.5em 2em;
    padding: 1em 0; margin: 0 0 1.6em;
    border-top: 1px solid var(--hairline); border-bottom: 1px solid var(--hairline);
  }
  .toc a {
    display: flex; gap: 0.7em; align-items: baseline;
    font-size: 0.85em; font-weight: 650; letter-spacing: -0.01em;
    color: var(--text); text-decoration: none; background: transparent;
  }
  .toc a .tocnum {
    font-family: var(--font-data); font-size: 0.72em; color: var(--accent);
    font-variant-numeric: tabular-nums;
  }
  .toc a:hover, .toc a:focus-visible { color: var(--accent); outline: none; }
  @media (max-width: 34em) {
    .toc { grid-template-columns: 1fr; }
  }

  /* Story-arc line (roadmap 4 step 8, renderArcs): chips in the mono data
     voice, same family as .sourcekey's .sk swatches below. Each chip is a
     LINK to that slug's arc page (§11.1 PR A) — text-decoration: none plus
     the explicit hover/focus rules below are what keep it reading as a
     provenance chip stating which thread this briefing continues, not as a
     button; see renderArcs. */
  .arcs { display: flex; flex-wrap: wrap; gap: 0.45em; margin: 0 0 1.2em; }
  .arcs .arc {
    font-family: var(--font-data); font-size: 0.72em; text-transform: uppercase;
    /* Rounded tag — same owner follow-up as .entry .flag above. */
    letter-spacing: 0.06em; padding: 0.22em 0.8em; border-radius: 999px;
    background: var(--chip-bg); color: var(--chip-text); text-decoration: none;
    /* Topics derive from section headings, which run headline-length in
       production (owner-reported, 2026-08-09) — cap the chip at one line.
       Only the LABEL ellipsizes (its own span, min-width: 0 so flex lets
       it shrink); the count is the chip's actual information and must
       never be the part the ellipsis eats. The label's tail is always
       recoverable one scroll down in the TOC. */
    max-width: 100%; display: inline-flex; align-items: baseline;
  }
  .arcs .arc .arclabel {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0;
  }
  .arcs .arc .arccount { font-weight: 700; margin-left: 0.45em; flex: none; }
  /* Interactivity signal lives on the LABEL only (an underline on hover),
     not on the chip's shape/color — the chip must not start looking like a
     button just because it became clickable. */
  .arcs .arc:hover .arclabel { text-decoration: underline; }
  .arcs .arc:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }

  /* Inline per-section arc link (this feature, addInlineArcLinks): a small
     link directly under a RECURRING section heading (count >= 2, same
     threshold as the .arcs chips just above), letting a reader already
     mid-section jump straight to that story's arc page instead of
     scrolling back to the top chip line. Same mono data voice as .arcs/
     .toc, deliberately quieter (muted, not a tinted chip) — this is the
     below-the-fold echo of the chip line, not a second competing signal. */
  .secarc {
    display: block; font-family: var(--font-data); font-size: 0.7em;
    text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted);
    text-decoration: none; margin: 0.35em 0 0;
  }
  .secarc:hover { color: var(--accent); text-decoration: underline; }
  .secarc:focus-visible {
    color: var(--accent); text-decoration: underline;
    outline: 2px solid var(--text); outline-offset: 2px;
  }

  /* "What changed" block (§11.3 delta persistence, ingest v4, renderDeltas):
     typography-led per the design guidance — no cards, a hairline between
     rows (same recipe as .now/.nowlist just below in the file), not a
     colored box. Each row is a link (like .now's .nowrow) to that delta's
     arc page. .deltatext/.deltaprev/.deltaarrow/.deltanow are shared with
     the arc page's own per-appearance line (renderArcAppearance) — same
     quiet register in both places, one set of rules for both. */
  .deltas { display: flex; flex-direction: column; margin: 0 0 1.6em; }
  .deltas .delta {
    display: flex; flex-direction: column; gap: 0.3em;
    text-decoration: none; color: inherit;
    padding: 0.6em 0; border-bottom: 1px solid var(--hairline);
  }
  .deltas .delta:last-child { border-bottom: none; }
  .deltalabel {
    font-family: var(--font-data); font-size: 0.72em; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--accent);
  }
  .deltas .delta:hover .deltalabel,
  .deltas .delta:focus-visible .deltalabel { text-decoration: underline; }
  .deltas .delta:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }
  .deltatext {
    margin: 0; font-family: var(--font-prose); font-size: 0.95em; line-height: 1.55;
  }
  .deltaprev { color: var(--muted); }
  .deltaarrow {
    font-family: var(--font-data); color: var(--muted); margin: 0 0.5em;
  }
  .deltanow { color: var(--text); }

  .attention {
    background: var(--attention-bg); color: var(--attention-text);
    padding: 0.8em 1em; margin: 0 0 1.4em;
  }
  .attention h2 { margin: 0 0 0.3em; border: 0; padding: 0; font-size: 0.95em; }
  .attention p { margin: 0; font-size: 0.95em; font-family: var(--font-prose); }

  /* The TL;DR callout ("print poster" redesign): no longer a tinted box —
     restyled as the digest page's LEADER paragraph, sitting directly under
     the derived h1 headline (see renderDigestPage's edhead/deriveHeadline).
     It's still the exact same server-rendered element (the emailer's own
     tldr div from body_html, see the file-header comment on
     stripInlineStyles) — only the presentation changed, so there is no
     second, duplicate TL;DR anywhere on the page. */
  .tldr {
    /* Emphasized bubble (owner follow-up — the plain leader paragraph
       under-sold the one block that summarizes the whole briefing):
       tinted background in the soft-bubble family (14px, like the
       popovers), no border, text in the tint's own readable pair. Still
       the same single server-rendered element from body_html — see the
       stripInlineStyles comment. */
    background: var(--tldr-bg); color: var(--tldr-text);
    padding: 1em 1.2em; border-radius: 14px;
    margin: 0 0 2em; font-weight: 400; font-size: 1.02em;
    font-family: var(--font-prose); line-height: 1.65;
  }
  /* The emailer's callout markup carries small eyebrow label spans
     (tldr-label / attention-label) and a ⚠ collection-failed banner
     paragraph, all previously presented by INLINE email styles the site now
     strips at render time (see stripInlineStyles) — these rules are their
     site-side, theme-aware replacements. Eyebrows in the mono data voice,
     matching the wire dateline. */
  .tldr .tldr-label, .attention .attention-label {
    display: block; font-family: var(--font-data); font-size: 0.68em;
    font-weight: 400; text-transform: uppercase; letter-spacing: 0.13em;
    margin-bottom: 0.5em;
  }
  .tldr .tldr-label { color: var(--accent); }
  .digest .banner {
    background: var(--attention-bg); color: var(--attention-text);
    padding: 0.6em 1em; margin: 0 0 1.2em;
    font-size: 0.92em;
  }
  /* Article section headings ("print poster" redesign): heavy 800-weight,
     tight-tracked display type with a NUMBERED badge — "01", "02", …, in
     --accent mono — and a 2px top rule in place of the old left accent bar,
     matching the numbered "in this edition" TOC above (.toc a .tocnum). The
     number comes from a CSS counter, not markup, so buildSectionToc (which
     only injects #sN ids for anchor targets) needs no change: counter-reset
     lives on .digest itself, incremented once per DIRECT-CHILD h2 — scoped
     to the .digest > h2 combinator specifically so the .attention callout's
     own nested h2 (one level deeper, see .attention h2 above) is never
     counted or numbered, same "never becomes a TOC entry" rule
     buildSectionToc already enforces for the index. */
  .digest { counter-reset: secnum; }
  .digest > h2 {
    counter-increment: secnum;
    font-size: 1.3em; font-weight: 800; letter-spacing: -0.025em; line-height: 1.15;
    /* Hairline, not --rule-heavy (owner follow-up): a bright 2px rule per
       section glared in dark mode and flattened the hierarchy — the heavy
       rule stays reserved for the masthead and colophon; inside the
       article the white belongs to the headlines alone. */
    border-top: 1px solid var(--hairline); padding-top: 0.8em;
    margin: 2.2em 0 0.7em; text-wrap: balance;
    /* So a TOC-jumped-to heading isn't flush against the viewport edge. */
    scroll-margin-top: 0.8em;
    /* Sticky section headings, the same idea as .dayhead on the index: while
       you read a section its heading stays on screen. Opaque background for
       the same reason — the article scrolls UNDERNEATH it. The padding-bottom
       is taken back out of the margin so the background covers the gap the
       body text would otherwise slide through just below the heading.

       Sticky SIBLINGS all pin at the same offset and overlap rather than
       pushing one another out — with headings of different heights (one line
       vs three) the taller previous one juts out below the current one. The
       usual cure is a wrapper element per section, but this article is
       pre-sanitized HTML the Worker inserts verbatim, and the numbering
       counter is scoped to .digest > h2 as a DIRECT child; wrapping would
       mean restructuring that HTML and rewiring the counter. So wirePage
       reproduces the push in script instead — see the sticky-headings block
       there — and the <noscript> block turns this off entirely, since
       without that script the overlap is exactly what you would get. */
    position: sticky; top: var(--mast-h, 0px); z-index: 1;
    background: var(--bg);
    padding-bottom: 0.35em; margin-bottom: 0.35em;
  }
  html.masthid .digest > h2 { top: 0; }
  .digest > h2::before {
    content: counter(secnum, decimal-leading-zero) "  ";
    font-family: var(--font-data); font-size: 0.62em; font-weight: 400;
    letter-spacing: 0.08em; color: var(--accent); vertical-align: 0.15em;
  }
  /* Body prose one tonal step below the headlines (--prose, owner
     follow-up: full-ink body next to full-ink h2s read as one wall);
     strong/em snap back to full --text so bolded stats stand out AGAINST
     the paragraph instead of vanishing into it. */
  .digest p { margin: 0.7em 0; font-family: var(--font-prose); line-height: 1.65; color: var(--prose); }
  .digest p strong, .digest p em { color: var(--text); }
  /* Reading polish (roadmap 4 step 2): hyphenate the prose blocks.
     html lang is already correct per page (en/hu, set by pageChrome) —
     the browser picks the right hyphenation dictionary on its own, this is
     just opting the prose in. Hungarian's long compounds are the motivating
     case on the 15px phone column, where an unbroken word can overflow a
     narrow line; -webkit- is what iOS Safari actually honors. Headings and
     chrome stay un-hyphenated on purpose — this is for reading paragraphs,
     not labels. */
  .digest p, .tldr, .entry .excerpt, .attention p, .arccontextbody p {
    hyphens: auto; -webkit-hyphens: auto;
  }
  /* nh3 allows pre/code through (digest repo, emailer.py's _ALLOWED_TAGS),
     and pre's own white-space: pre is immune to the body's inherited
     overflow-wrap — a fenced code block in a digest was exactly what
     re-triggered the phone layout bug (see .wrap's comment). pre-wrap keeps
     code readable while letting long lines break at the container edge. */
  .digest pre { white-space: pre-wrap; overflow-wrap: break-word; }
  /* Cite chips ("print poster" redesign): tiny mono domain tags — --accent
     text on --chip-bg, 3px radius (chips/badges otherwise stay square;
     interactive CONTROLS are the pill-rounded exception, see summary.gear's
     comment), filling solid --accent with paper text on hover/focus. */
  .cite {
    font-size: 0.7em; vertical-align: super; text-decoration: none;
    background: var(--chip-bg); color: var(--accent);
    padding: 0 0.4em; border-radius: 3px; font-weight: 700; margin-left: 1px;
    font-family: var(--font-data);
  }
  .cite:hover, .cite:focus-visible { background: var(--accent); color: var(--bg); outline: none; }
  /* Search hit highlighting (roadmap 4 step 7, markSnippet): reuses the
     citation chip's own chip-bg/chip-text tokens rather than a new color —
     it's the same "this is metadata the site added, not article content"
     visual family as .cite. */
  mark { background: var(--chip-bg); color: var(--chip-text); padding: 0 0.15em; }
  /* Touch provenance (roadmap 4 step 2): on the phone — where this site is
     mostly read — there's no hover, so the title attribute's domain never
     surfaces; put it on the pill itself instead. Reuses the exact title
     addCiteTitles already sets and the same attr(title) pattern the print
     stylesheet below uses, so the chip reads "1 example.com" instead of a
     bare number. Hover-capable devices are untouched and keep the bare chip
     plus the native hover title. .cite[title], not bare .cite, so a chip
     whose href failed to parse (no title) shows nothing extra — same
     fail-safe as print. The bigger pill this produces is also a bigger,
     easier-to-hit touch target. */
  @media (hover: none) {
    .cite[title]::after {
      content: attr(title);
      margin-left: 0.35em;
      font-weight: 400;
      letter-spacing: 0;
      /* The prose hyphenation above inherits into the chip and was
         auto-hyphenating the domain itself ("ex-ample1.com") — an inserted
         hyphen inside a hostname reads as part of the hostname, which
         misstates the provenance this feature exists to show. Long domains
         still wrap (overflow-wrap), just never with an added hyphen. */
      hyphens: none; -webkit-hyphens: none;
    }
  }
  .closing {
    font-style: italic; color: var(--muted); border-top: 1px solid var(--hairline);
    padding-top: 1em; margin-top: 2.2em; font-size: 0.92em;
  }

  /* Source key (roadmap 2 step 8 follow-up): the digest page's colophon —
     source_counts/failed_sources spelled out as concrete per-source numbers
     with color swatches, sitting right after the article (renderSourceKey
     renders nothing when both fields are absent — see the function for the
     fail-safe JSON.parse contract shared with renderDegradedBadge). The
     index page's own micro-bar counterpart (.spectrum, one <i> per source
     sized by inline flex:N) was removed in the owner-requested index-
     cleanup pass; this colophon is unaffected and still carries the full
     provenance on the digest page. */
  .sourcekey {
    font-family: var(--font-data); font-size: 0.75em; color: var(--muted);
    display: flex; flex-wrap: wrap; gap: 0.5em 1.1em; align-items: center;
    margin: 1.6em 0 0;
  }
  .sklabel { text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
  .sk { display: inline-flex; align-items: center; }
  .sk i { display: inline-block; width: 9px; height: 9px; margin-right: 0.45em; }
  /* .sk-failed gets no color override — muted stays muted, the ⚠ prefix
     baked into the string (not CSS) is what marks it, same "color is not
     the warning signal" decision as .flag-degraded on the index. */

  /* Model provenance (owner-requested "WRITTEN" byline): a SECOND digest-
     page colophon row, sitting directly below .sourcekey (renderProvenance
     renders nothing when the digest carries no provenance data — see the
     function for the fail-safe JSON.parse contract shared with
     renderSourceKey). A sibling ruleset, not a shared class, so a future
     change to one row's spacing/color never silently drags the other — but
     it copies .sourcekey's exact typography/layout (font-data, 0.75em,
     muted, flex-wrap gap) so the two rows read as one family of colophon
     lines. Tighter top margin than .sourcekey's own (which is spaced off
     the article above it): this row is spaced off the ROW above it, not a
     block of prose. */
  .provenance {
    font-family: var(--font-data); font-size: 0.75em; color: var(--muted);
    display: flex; flex-wrap: wrap; gap: 0.5em 1.1em; align-items: center;
    margin: 0.5em 0 0;
  }
  /* .sk-fallback gets no color override either — same "muted stays muted,
     the glyph is the marker" posture .sk-failed established just above: the
     ↻ baked into the chip text (not CSS) is what marks an OpenRouter
     fallback leg, not a color change. */

  /* Floating back-to-index button (digest pages only): fixed bottom-right
     in one-thumb reach, clear of the iPhone home bar via safe-area insets.
     Hidden until the reader scrolls past the top nav (the inline script in
     pageChrome toggles .show), so it never duplicates the visible header
     nav; with JS disabled the <noscript> style keeps it always visible
     instead — the button must never be unreachable. Accent background with
     the page background as the arrow color works in both themes. */
  .backfab {
    position: fixed;
    right: max(var(--fab-inset), env(safe-area-inset-right));
    bottom: calc(var(--fab-inset) + env(safe-area-inset-bottom));
    width: var(--fab-size); height: var(--fab-size); border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    background: var(--accent); color: var(--bg);
    text-decoration: none; font-size: 1.35em; font-weight: 700;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28);
    opacity: 0; pointer-events: none; transition: opacity 0.18s ease;
    z-index: 10;
  }
  .backfab.show { opacity: 1; pointer-events: auto; }
  .backfab:focus-visible { outline: 2px solid var(--text); outline-offset: 3px; opacity: 1; pointer-events: auto; }
  @media (prefers-reduced-motion: reduce) { .backfab { transition: none; } }

  /* View tabs: the primary content navigation. Bigger than the corner
     language toggle by design — switching between the full stream and
     daily briefs is the main choice a reader makes. Owner-requested
     2026-08-09: one connected segmented capsule instead of three detached
     pills. The capsule (.viewtabs) carries the border, radius and
     overflow: hidden; segments (.viewtab, unchanged below — same hrefs,
     active-state fill, data-view attributes the ⌘K palette reads) are
     borderless and share a hairline divider. Owner-requested masthead
     compaction: this used to be centered on its own row below the masthead
     (width: fit-content + auto side margins); it now rides inline in
     .mastleft beside the brand (see pageChrome/the header.mast CSS above),
     so there's no more row of its own to center on — width: fit-content
     stays (the capsule still hugs its own content rather than stretching),
     the centering margin is gone. The view selector remains the primary
     navigation. */
  .viewtabs {
    display: flex; width: fit-content;
    border: 1px solid var(--rule-heavy);
    /* Pill capsule (owner-requested, see summary.gear's comment); overflow
       hidden clips the active segment's fill to the rounded ends. */
    border-radius: 999px; overflow: hidden;
    font-family: var(--font-data); font-size: 0.68em; letter-spacing: 0.08em; text-transform: uppercase;
  }
  .viewtab {
    padding: 0.5em 1.1em;
    font-weight: 400; text-decoration: none;
    color: var(--text);
  }
  .viewtab + .viewtab { border-left: 1px solid var(--hairline); }
  /* Same Safari capsule-clip insurance as .minisegbtn above. */
  .viewtab:first-child { border-radius: 999px 0 0 999px; }
  .viewtab:last-child { border-radius: 0 999px 999px 0; }
  .viewtab.active {
    background: var(--accent); color: var(--bg);
  }
  /* Segments have no border of their own to shift on hover anymore, so hint
     hover with the same soft accent-tinted background the TL;DR block uses
     (picked over an --accent-strong color shift — quieter against the
     filled active segment sitting right next to it). */
  .viewtab:not(.active):hover { background: var(--tldr-bg); }
  /* Inset outline (negative offset): an outset ring would get clipped by
     the capsule's overflow: hidden. */
  .viewtab:focus-visible { outline: 2px solid var(--text); outline-offset: -2px; }

  /* Week rail (roadmap 3 step 2): mono wire-style ← older · WEEK N · range ·
     newer → nav, between the view tabs and the ledger, ALL-view index pages
     only (see renderIndexPage/renderWeekRail). Classic 3-column centering
     trick for the first three spans: rail-older/rail-newer share flex:1 (so
     they're always equal width regardless of their own content length, even
     when one side is an empty spacer), which keeps the center label
     visually centered without needing to measure anything.
     trick at all (flex: none, own rule below), it just claims its own
     natural width at the row's right edge; the gap property below gives it
     breathing room from rail-newer's "→" link on an archive week rather
     than the two abutting directly. */
  .weekrail {
    display: flex; align-items: baseline; gap: 0.6em; margin: 0 0 1.2em;
    font-family: var(--font-data); font-size: 0.78em;
    letter-spacing: 0.06em; text-transform: uppercase;
  }
  .weekrail .rail-older, .weekrail .rail-newer { flex: 1; }
  .weekrail .rail-older { text-align: left; }
  .weekrail .rail-newer { text-align: right; }
  .weekrail .rail-center { flex: 0 1 auto; color: var(--muted); }
  .weekrail a { color: var(--accent); text-decoration: none; }

  /* Archive sparkline (roadmap 4 step 6, renderWeekRail): the pulse strip's
     visual language, sized down to live inside the rail's center span —
     archive weeks only, see renderWeekRail. Zero-count days (.sd0) get their
     height from THIS rule rather than an inline style like the nonzero bars
     get, so the two never end up in a specificity fight over the same
     property. */
  .railspark { display: inline-flex; align-items: flex-end; gap: 2px; height: 14px; margin-left: 0.7em; vertical-align: -2px; }
  .railspark i { display: block; width: 4px; background: var(--chip-bg); }
  .railspark i.sd0 { background: var(--hairline); height: 15%; }

  /* Search bubble (owner-requested index cleanup): the week-rail's compact
     trigger for what used to be the always-visible filterrow — same
     details/summary disclosure pattern as the settings gear just above
     (details.settings/summary.gear/.settingspanel), reusing its exact
     border/elevation/open-animation recipe (the settingsopen keyframe,
     defined above, is referenced again below rather than copied) under NEW
     class names rather than the literal same ones: the settings-close IIFE
     and the soft-nav click interceptor (see pageChrome's bottom script)
     both assume exactly one details.settings element on the page, and this
     is a second, unrelated disclosure that must not collide with either
     lookup. */
  details.searchpop { position: relative; }
  /* Two-faced, the same way summary.gear is: a TEXT chip on the desktop
     ("SEARCH"/"KERESÉS", owner-requested — it reads as a matched pair with
     the SETTINGS chip beside it) and a bare oversized magnifier on the
     phone. Both halves are in the markup (see renderSearchBubble) and each
     breakpoint hides one; this rule is the DESKTOP half and deliberately
     duplicates summary.gear's pill recipe declaration for declaration, so
     the two chips share a border, type, tracking and padding.

     Unlike .gearicon/.gearlabel, the reveal below needs NO specificity
     boost: the phone block sits BELOW this rule, so source order already
     carries it. That is the whole reason it lives down there. */
  summary.searchtoggle {
    list-style: none; display: inline-flex; align-items: center; justify-content: center;
    background: none; border: 1px solid var(--rule-heavy); border-radius: 999px;
    color: var(--text); font-family: var(--font-data); font-size: 0.62em;
    letter-spacing: 0.08em; text-transform: uppercase;
    padding: 0.4em 0.75em; cursor: pointer;
  }
  /* Desktop half-swap: the word shows, the magnifier hides. Reversed in the
     phone block below. */
  .searchicon { display: none; }
  /* Quiet hover / filled open — the same split as summary.gear above. */
  summary.searchtoggle:hover { border-color: var(--accent); color: var(--accent); }
  summary.searchtoggle:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }
  details.searchpop[open] > summary.searchtoggle {
    background: var(--accent); border-color: var(--accent); color: var(--bg);
  }
  summary.searchtoggle::-webkit-details-marker { display: none; }

  /* Hover bridge (hover-to-open, client.js's hoverPopovers). Both bubbles
     sit 0.5em BELOW their chip, and that gap is outside both boxes -- so a
     pointer travelling from chip to panel leaves the details element and
     fires mouseleave halfway there. Covering the gap with a transparent
     pseudo-element makes the trip continuous, which is what lets the close
     timer stay short: a long grace period would leave the panel open after
     the pointer had gone, and an open panel swallows the next click
     anywhere on the page. Pointer-events only -- it paints nothing, and it
     sits inside the panel so it exists only while the panel does. */
  .settingspanel::before,
  .searchpanel::before {
    content: ""; position: absolute; left: 0; right: 0;
    top: -0.55em; height: 0.55em;
  }

  .searchpanel {
    position: absolute; right: 0; top: calc(100% + 0.5em);
    z-index: 20;
    /* Simple soft bubble — same recipe as .settingspanel above (owner
       follow-up), one popover family.

       Owner-reported 2026-08-27 ("the search box is really bad looking").
       The fault was NESTED BOXES, not the bubble: this panel drew a border,
       the input inside drew a second one, and the input's focus ring drew a
       third 2px OUTSIDE its own border (outline-offset), so a focused field
       showed three concentric rounded rectangles a couple of pixels apart.
       The 1em padding against a 16em min-width left the field floating in
       dead space on top of that.

       The bubble now owns the only border. Its padding moves onto the two
       children, which run edge to edge; overflow: hidden clips them to the
       radius so the input's own corners never need to agree with the
       panel's. gap goes to 0 because the divider between them is a border,
       not a space. */
    background: var(--bg); border: 1px solid var(--hairline);
    border-radius: 12px;
    padding: 0;
    overflow: hidden;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28);
    display: flex; flex-direction: column; gap: 0; min-width: 19em;
  }
  /* Focus lives on the BUBBLE, which is the only thing still drawing a
     border — so a focused field highlights the whole control instead of
     stacking another ring inside it. :focus-within, not :focus, because the
     element actually receiving focus is the input. */
  .searchpanel:focus-within { border-color: var(--accent); }
  @media (prefers-reduced-motion: no-preference) {
    /* Reuses .settingspanel's own open keyframe (see above) — same visual
       language, no need for a second identical @keyframes block. Opening
       needs no JS, same as the settings bubble: native <details> flips
       [open] the instant the summary is clicked, and this rule keys off
       that attribute directly. No matching close animation here (unlike
       settings' settingsclose/"panelclosing" dance) — this bubble has no
       language-switcher-style reason to stay open across a navigation, and
       the owner brief's only explicit behavioral ask was opening it with
       focus, not mirroring the settings bubble's close polish too. */
    details.searchpop[open] .searchpanel { animation: settingsopen 160ms ease-out; }
  }
  /* The SAME .filter input class the client-side filter IIFE and the
     archive-search integration already look for (document.querySelector of
     ".filter" — see pageChrome's bottom script), just styled for its new
     home inside the panel instead of a standalone row. */
  /* The icon + input pair. The row owns the padding the input used to carry,
     so the two sit on one baseline with the glyph in the panel's left
     gutter. Its own color property is what the inline SVG's currentColor
     resolves against — muted at rest, accent while the field has focus, so it
     tracks the panel border's own focus state instead of sitting there at a
     fixed weight. */
  .searchfield {
    display: flex; align-items: center; gap: 0.55em;
    padding: 0.7em 0.95em; color: var(--faint);
  }
  .searchpanel:focus-within .searchfield { color: var(--accent); }
  .fieldicon { flex: none; }
  /* Hide the PAIR, not just the input. The input ships [hidden] until the
     filter IIFE reveals it, and on digest/arc pages that reveal never
     happens (no ledger to filter — it bails before the reveal), so without
     this the icon would hang above the archive link as a dead control. :has
     is already used in this stylesheet (see .settingsrow above). */
  .searchpanel:has(.filter[hidden]) .searchfield { display: none; }
  .searchpanel .filter {
    flex: 1; min-width: 0;
    font: inherit; font-size: 0.9em;
    padding: 0;
    /* No border and no radius of its own: the panel draws both now. */
    border: 0; border-radius: 0;
    background: none; color: var(--text);
  }
  .searchpanel .filter::placeholder { color: var(--faint); }
  /* Deliberately none: the panel's own :focus-within border above IS this
     input's focus indicator, and it is the accent against a hairline, so
     the state stays visible without a second ring. */
  .searchpanel .filter:focus-visible { outline: none; }
  /* Entry point into the standalone search page (roadmap 4 step 7) — the
     no-JS fallback, still the row's only visible content when the filter
     input above is hidden (see renderSearchBubble). Mono chrome voice, not
     styled like the data field beside it, same as before this move.

     It is now the panel's FOOTER ROW rather than a link sitting under a
     field: full-bleed, its own padding, a hairline above it. That gives the
     bubble a reason to be a bubble (a field plus a way out of it) instead
     of a box with one control rattling around inside. */
  .searchlink {
    display: block; padding: 0.5em 0.95em;
    font-family: var(--font-data); font-size: 0.78em;
    text-decoration: none; letter-spacing: 0.06em; text-transform: uppercase;
  }
  .searchlink:hover { background: var(--chip-bg); }
  /* The divider belongs to the PAIR, not to the link: with no JS the filter
     input stays [hidden] (see renderSearchBubble) and this link is the
     panel's only content, where a top border would read as a stray rule
     across an otherwise empty bubble. Was an adjacent-sibling selector until
     the icon landed and moved the input inside .searchfield; :has keeps it
     keyed off the same condition without depending on the two being
     siblings. */
  .searchpanel:has(.filter:not([hidden])) .searchlink {
    border-top: 1px solid var(--hairline);
  }

  /* Unified search results (owner UX pass): the index page's own box for
     archive hits fetched in the background by the bottom script — see
     renderIndexPage's archiveResultsHtml. .archivelabel is the file's mono
     eyebrow recipe, kept as its own class rather than shared with any other
     eyebrow — coupling unrelated features to one class just because they
     currently render alike is the kind of thing that bites later, and this
     class survived a neighbouring feature's removal untouched precisely
     because it was never shared. .archiveresults[hidden] needs no rule of its
     own: the global [hidden] override near the top of this stylesheet
     already covers it, same as every other hide-by-attribute element here. */
  .archivelabel {
    font-family: var(--font-data); font-size: 0.7em; text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--muted); margin-bottom: 0.6em;
  }
  .archiveresults { margin-top: 1.6em; }

  /* Catch-up banner (§11.2): one row above the NOW section, built entirely
     by the unread-fence IIFE extension in the bottom script — see
     renderIndexPage's catchupHtml for the hidden shell and why it always
     sits above nowHtml regardless of whether NOW itself has content that
     day. No card (design guidance), mono metadata register matching
     .nowmeta/.archivelabel, a bottom hairline as the only separator — same
     "typography, not chrome" recipe as .now just below. .catchuptext holds
     the composed sentence (may include real <a> links to followed arcs, see
     the script — that's why it's a plain span, not the row's own click
     target: a button/link cannot legally contain another link). The jump
     and dismiss controls are small icon-only buttons, deliberately NOT
     styled like .resumechip's pill — this row is metadata-weight chrome,
     not a floating call to action. */
  .catchup {
    display: flex; align-items: baseline; gap: 0.7em;
    margin: 0 0 1.4em; padding-bottom: 1.1em;
    border-bottom: 1px solid var(--hairline);
    font-family: var(--font-data); font-size: 0.78em; color: var(--muted);
  }
  .catchuptext { flex: 1 1 auto; min-width: 0; }
  .catchuptext a { color: var(--accent); text-decoration: none; }
  .catchuptext a:hover, .catchuptext a:focus-visible { text-decoration: underline; }
  .catchupjump, .catchupdismiss {
    flex: none; background: none; border: 0; padding: 0.1em 0.3em;
    color: var(--muted); font: inherit; font-size: 1em; line-height: 1;
    cursor: pointer;
  }
  .catchupjump:hover, .catchupdismiss:hover,
  .catchupjump:focus-visible, .catchupdismiss:focus-visible { color: var(--text); }
  .catchupjump:focus-visible, .catchupdismiss:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }

  /* NOW section (§11.1 PR B, renderNowSection): the situational-overview
     block at the very top of the current-week all-view index, above the
     rail/search row/ledger — see renderIndexPage. .archivelabel
     is reused for the eyebrow (already shared by the arc timeline and this
     file's own archive-search label above) rather than a fourth near-
     identical mono-eyebrow class. Typography-led per the design guidance:
     each arc is one row, not a card — a bottom hairline on the whole block
     is the only separator between it and the ledger, no box/border around
     the block itself. */
  .now { margin: 0 0 1.8em; padding-bottom: 1.3em; border-bottom: 1px solid var(--hairline); }
  /* Head row: the NOW eyebrow left, the search control right (owner-
     requested placement) — the icon lands on the same right edge the arc
     rows' relative-time meta below it aligns to. The eyebrow (.archivelabel)
     carries its own bottom margin, so the row's own baseline stays where a
     bare eyebrow used to sit; align-items center keeps the icon optically
     on the eyebrow's line rather than riding its cap height. */
  .nowlist { display: flex; flex-direction: column; }
  .nowrow {
    display: flex; align-items: baseline; gap: 0.7em;
    text-decoration: none; color: inherit;
    padding: 0.55em 0; border-bottom: 1px solid var(--hairline);
  }
  .nowlist .nowrow:last-child { border-bottom: none; }
  .nowrow:hover .nowarclabel,
  .nowrow:focus-visible .nowarclabel { color: var(--text); text-decoration: underline; }
  .nowrow:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; }
  /* Momentum arrow: mono, muted — a data-derived signal (design guidance:
     "evidence over certainty"), not a colored/severity cue, so it carries no
     color of its own beyond the row's normal muted register. Fixed width so
     the label column stays aligned across rows regardless of which arrow (or
     none, on the defense-in-depth null case) a given row has. */
  .nowarrow {
    font-family: var(--font-data); font-size: 0.85em; color: var(--muted);
    flex: none; width: 1em; text-align: center;
  }
  .nowarclabel {
    font-family: var(--font-prose); font-size: 0.97em; color: var(--muted);
    flex: 1 1 auto; min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .nowmeta {
    font-family: var(--font-data); font-size: 0.75em; color: var(--muted);
    flex: none; white-space: nowrap;
  }

  /* Search page (roadmap 4 step 7): the form itself reuses .filter's input
     styling (see above) — it's the SAME kind of control, just server-
     functional here instead of a client-side enhancement (see
     renderSearchPage). */
  .searchform { display: flex; gap: 0.5em; margin: 0 0 1.6em; }
  .searchform .filter {
    flex: 1; font: inherit; font-size: 0.9em; padding: 0.5em 0.7em;
    /* Hairline + rounding — same reasoning as .searchpanel .filter above. */
    border: 1px solid var(--hairline); border-radius: 8px;
    background: var(--bg); color: var(--text);
  }
  .searchform .filter::placeholder { color: var(--muted); }
  .searchform .filter:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  /* Styled like .viewtab's outlined pill (own rule above), not a filled
     button — a search submit is a secondary action next to the input, not
     the page's primary call to action. */
  .searchbtn {
    border: 1px solid var(--rule-heavy); border-radius: 999px; color: var(--text);
    font-family: var(--font-data); font-size: 0.7em; letter-spacing: 0.08em; text-transform: uppercase;
    padding: 0.5em 1.2em; background: none; cursor: pointer;
  }
  .searchbtn:hover { border-color: var(--accent); color: var(--accent); }
  .searchbtn:focus-visible { outline: 2px solid var(--text); outline-offset: 2px; }

  /* Unread fence (roadmap 2 step 2): one labeled hairline the bottom script
     inserts between digests that arrived since the reader's last visit and
     everything older — no-JS readers never see this class at all, so no
     hidden-by-default dance is needed here (unlike .filter/.minisegbtn
     above, which exist in the markup from the start). */
  .unreadfence { grid-column: 1 / -1; display: flex; align-items: center; gap: 0.7em; margin: 1.4em 0; }
  .unreadfence .line { flex: 1 1 auto; height: 0; border-top: 1px solid var(--accent); }
  .unreadfence .label {
    flex: 0 0 auto; font-family: var(--font-data); font-size: 0.7em;
    text-transform: uppercase; letter-spacing: 0.08em; color: var(--accent);
  }
  /* (The filter IIFE hides this via the hidden attribute while a query is
     active — the global [hidden] rule near the top of this stylesheet is
     what makes that actually take effect over the display: flex above.) */

  /* Resume chip (roadmap 4 step 3): a floating "jump to the fence" button,
     built entirely by the unread-fence IIFE below and only when a fence was
     actually inserted — it inherits every one of that IIFE's guards for
     free (no-JS, archive week, nothing new, everything new). No display
     rule needed for hiding — the global [hidden] override near the top of
     this stylesheet already handles that, same as .unreadfence above. */
  .resumechip {
    position: fixed;
    left: 50%; transform: translateX(-50%);
    /* One FAB-height above the corner button rather than beside it. The
       chip is centred and the FAB is right-aligned, so on a wide viewport
       they never met — but the chip is only as narrow as its label, and the
       Hungarian one ("↓ ÚJ A LEGUTÓBBI LÁTOGATÁSOD ÓTA", 261px) overlapped
       the FAB by 9px at 375px once every non-digest page gained one. Capping
       the chip's width instead would mean truncating that label, and it is
       nowrap on purpose (see below); stacking costs nothing and is the
       conventional arrangement anyway — a transient chip rides above a
       persistent action button, never under it. */
    bottom: calc(var(--fab-inset) + var(--fab-size) + 0.55rem + env(safe-area-inset-bottom));
    font-family: var(--font-data); font-size: 0.72em;
    text-transform: uppercase; letter-spacing: 0.08em;
    background: var(--bg); color: var(--accent);
    border: 1px solid var(--accent); border-radius: 999px;
    padding: 0.45em 1.1em; cursor: pointer;
    /* One line, always: the body's inherited overflow-wrap breaks the
       label across two lines well before the pill nears the viewport
       edge, and a two-line floating pill reads as a banner, not a chip. */
    white-space: nowrap;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.28);
    z-index: 10;
  }
  .resumechip:focus-visible { outline: 2px solid var(--text); outline-offset: 3px; }


  /* Desktop: the reading column is a LIFTED SHEET — paper (--bg) floating
     on a tinted gutter (--page-bg), with a shadow between them.
     A rounded card here is not new. PR #64 shipped one on a purple page
     background; the print-poster redesign (#144) retired it and left
     --page-bg behind as a token equal to --bg, referenced by no rule at
     all — this block re-points it at its original job. The owner's
     correction over #64 is the important part: the edge is drawn by a
     SHADOW, not #64's hairline border. A border states where the card
     stops; a shadow states that the card is ABOVE something, which is the
     whole point of the ask.
     Mobile is untouched — below 52em the page stays flat paper edge to
     edge. That is also why pageChrome's <meta name="theme-color"> pair
     still tracks --bg and not --page-bg: it only ever colors mobile
     browser chrome, and mobile never sees the gutter. */
  /* Wide-viewport type scale (owner: "the resolution is too low" on a big
     display). The honest lever for a prose-first site is SIZE, not width:
     every dimension here is em-based off body, so stepping the base up
     scales the column, the padding, the chrome and the text together, uses
     more of a large screen, and leaves the character measure exactly where
     it was (~82). Widening the column instead would have pushed the
     measure past 100 characters, which is where reading actually degrades.
     Breakpoint ems resolve against the 16px root, so these are 1200px and
     1600px. The S/M/L preference in Settings still multiplies on top. */
  @media (min-width: 75em) { body { font-size: 18px; } }
  @media (min-width: 100em) { body { font-size: 19px; } }

  @media (min-width: 52em) {
    /* html AND body, deliberately. body paints the gutter; html paints the
       overscroll rubber-band and any pre-body-paint gap — that is the
       second layer of the anti-flash fix above, which pinned html to --bg
       for the flat era. Left on --bg it would flash paper-white behind a
       bounce scroll. */
    html { background: var(--page-bg); }
    body { background: var(--page-bg); padding: 2.5em 1.5em; }
    .wrap {
      background: var(--bg);
      border-radius: 20px;
      box-shadow: var(--card-shadow);
      /* Without this the sheet ENDS where the content ends: /about, a thin
         digest and an empty search result each stopped a third of the way
         down the viewport and read as a truncated stub floating in the
         gutter (caught in the render pass, before this shipped). 5em =
         body's 2.5em top + 2.5em bottom padding, and box-sizing: border-box
         is global, so this is the outer height. dvh over vh costs nothing
         and is simply the non-fragile unit — the mobile dynamic-viewport
         problem it guards against cannot reach a desktop-only block. */
      min-height: calc(100dvh - 5em);
      /* 61em (owner follow-up on the Front Page redesign: "desktop text
         should be wider about 30 percent") — up from 48em, i.e. a ~55em
         text measure after the 3em side padding, ~31% over the old 42em.
         This supersedes the earlier size-not-width decision recorded
         below for the SINGLE-column era: the Front Page index is a
         two-column grid now, so the extra width goes to two honest ~26em
         columns instead of one over-long line, and the digest page's
         longer measure is the owner's explicit call. */
      max-width: 61em;
      padding: 0.4em 3em 3.5em;
    }
    /* NOTE — still no prose max-width clamp here, deliberately. A pass on
       2026-08-10 tried widening the column while holding prose narrower,
       on the theory that chrome should use width prose shouldn't. Live,
       that read as a broken right edge rather than as hierarchy: the
       TL;DR and every ledger excerpt stopped dead mid-column with nothing
       beside them (owner-reported, twice). Whatever the column's width
       is, text fills it: content box and text edge stay one and the same.
       The wide-viewport font-size steps above still apply on top. */
    /* Anchor the floating back button to the COLUMN, not the bare viewport
       edge: the column is 61em centered, so its right edge sits at
       50% + 30.5em — park the button 1rem past it. min() clamps back to
       the viewport edge on narrow desktop windows so the button can never
       be pushed off-screen. Mobile keeps the base viewport-corner
       placement. */
    .backfab {
      /* em would resolve against the fab's own 1.35em font — overshooting
         by ~150px (live-measured). rem resolves against the root: the
         column is 61em of the 17px body = 1037px wide, half = 518px =
         32.4rem at the 16px root default. */
      left: min(calc(50% + 32.4rem + 1rem), calc(100vw - 48px - 1.1rem));
      right: auto;
    }
  }

  /* ── THE phone block ─────────────────────────────────────────────────
     Every max-width-40em override lives HERE, at the end of the
     stylesheet, after every base rule it overrides — so source order can
     never silently disarm one again. That trap bit six separate rules
     over this file's history (gear icon reveal, toolbar resets, sticky
     day-header and section-heading offsets, a brand size override) —
     each time a phone rule written next to its feature lost to an
     equal-specificity base rule defined later in the file.

     THE INVARIANT: do not add a max-width-40em block anywhere else in
     this stylesheet. Add phone overrides to this block, in the section
     matching their feature. (Other query TYPES — hover, reduced-motion,
     min-width, print, the 34em narrow tweak — are not part of the trap:
     their base rules never compete at equal specificity the same way.) */
  @media (max-width: 40em) {
    /* Mobile masthead + phone font size (owner-tuned). */
    /* 15px: phone type ran large even at 16 (owner feedback, twice). The
       S/M/L text-size rules need no phone twin — they're em factors off
       this base, see the data-fontsize block above. */
    body { font-size: 15px; }
    /* Icon-only gear on the phone — the desktop half-swap of .gearicon/
       .gearlabel, reversed. The desktop .gearicon { display: none } rule
       sits LATER in this stylesheet (source order beats equal specificity,
       media block or not — live-caught as an empty settings circle), so
       this reveal needs the extra summary.gear ancestor to outweigh it. */
    .gearlabel { display: none; }
    summary.gear .gearicon { display: inline; }
    /* The two toolbar toggles are EQUAL boxes on the phone (owner follow-up:
       the gear must match the search icon's size) — but their rules do NOT
       live here. They sit in the phone block AFTER summary.gear and
       summary.searchtoggle's own rules further down this stylesheet, which
       is the only place they survive: those base rules set border, padding,
       font-size and letter-spacing at the same specificity, so an override
       written HERE loses on source order. That is the same trap the
       .gearicon note above documents, and it had already eaten this rule's
       own padding, font-size and letter-spacing resets silently. */
    /* Masthead phone posture: ONE line — brand, capsule, search, settings —
       the same row the desktop shows (owner-requested). It used to take two,
       with a forced break (a full-basis ::before pseudo-item at order 3)
       dropping the capsule onto its own centered second line, because the
       three zones genuinely did not fit at 375px and the side zones' flex:
       1 1 0 meant flex would crush them into an overlapping single line
       rather than wrap.

       What makes one line fit now is the sizing below, not a layout trick:
       the capsule's segments lose horizontal padding, the row gap tightens,
       and the toolbar icons come down to the wordmark's own height (see the
       toolbar block further down). The brand keeps its display scale — it
       turned out not to need shrinking once the other three gave ground.
       Measured at 375px that is ~303px of content in a 337.5px box; at 360px
       (the narrowest phone still worth designing for) ~20px still spare, and
       at ~320px it wraps rather than overflows.

       The forced break is GONE but flex-wrap: wrap stays, now as a genuine
       safety net rather than a mechanism: the zones are flex: 0 0 auto here,
       so they cannot be crushed, which means a viewport too narrow to hold
       them (~320px, an SE-1 class device) WRAPS instead of overlapping —
       the failure the forced break originally existed to prevent, handled by
       the wrap itself now that nothing can shrink below its content. */
    header.mast { flex-wrap: wrap; gap: 0.45em 0.5em; justify-content: space-between; }
    /* No flex: 1 1 0 growth here — that is the desktop's true-centering
       trick, and on the phone it is exactly what would crush these back into
       an overlap. Content width, no grow, no shrink. */
    .mast .mastleft, .mast .mastright, .mast .viewtabs { flex: 0 0 auto; }
    /* The capsule is the widest item in the row, so it pays the most: the
       segments keep their type size and lose horizontal padding only. */
    .mast .viewtab { padding: 0.5em 0.8em; }

    /* Sticky masthead (owner-requested: "I don't have to scroll up to see
       the settings, all, daily"). It HIDES on scroll down and comes back on
       scroll up rather than sitting there permanently — on a phone the row
       is ~65px of an ~812px viewport, and a reader scrolling down is reading,
       not navigating. Scrolling up is the gesture that means "I want to get
       somewhere", which is exactly when the controls should appear. wirePage
       toggles html.masthid; see that block for the direction logic.

       Needs an opaque background: the page scrolls UNDER a sticky element,
       so without it the ledger would read straight through the masthead.

       z-index 30 is deliberate — above .dayhead (1), the FAB and resume chip
       (10) and the settings/search popovers (20, which are children of this
       element and so ride in its stacking context), but BELOW the ⌘K palette
       overlay (40), which is a modal and must cover the masthead. */
    header.mast {
      position: sticky; top: 0; z-index: 30;
      background: var(--bg);
      transition: transform 0.2s ease;
    }
    html.masthid header.mast { transform: translateY(-100%); }
    /* Two more rules belong to this feature — .dayhead's sticky offset and
       .digest > h2's scroll-margin — but they CANNOT live here: both base
       rules are defined further down this stylesheet at equal specificity,
       so an override written here loses on source order (the same trap the
       .gearicon and toolbar-button notes above document — and it did bite:
       .dayhead measured top: 0px in both states until they moved). They sit
       in their own phone block just after .digest > h2 instead. */

    /* Ledger: the two-column grid drops to one. */
    section[data-unread-label] { grid-template-columns: 1fr; }

    .entry-lead { grid-template-columns: 1fr; gap: 1.1em; }
    .leadfacts {
      border-left: 0; border-top: 1px solid var(--hairline); padding: 0.9em 0 0;
      display: flex; gap: 1.6em;
    }
    .leadfacts dt { margin-top: 0; }

    /* The sticky-masthead feature's dependent offsets: the day headers and
       digest section headings pin below the masthead while it shows and at
       the viewport edge once it hides (wirePage publishes --mast-h). */
    /* The day headers are sticky too (top: 0, z-index 1), so with a sticky
       masthead above them they would pin UNDERNEATH it and vanish. They
       stick below it instead — and drop back to the top edge in lockstep
       when the masthead hides, because a fixed offset would leave a band of
       page content scrolling through the gap the masthead used to fill.
       --mast-h is measured and published by wirePage, since the compact and
       big mastheads are different heights. */
    .dayhead { top: var(--mast-h, 0px); }
    html.masthid .dayhead { top: 0; }
    /* A TOC jump must clear the masthead, not land under it. Falls back to
       the same 0.8em as the rule above when --mast-h is unset. */
    .digest > h2 { scroll-margin-top: calc(var(--mast-h, 0px) + 0.8em); }

    /* Phone toolbar buttons: borderless, icon-scale (owner-requested — "remove
     the circle, make the icon as big as the circle was"). The ring around the
     search and gear toggles comes off and the marks inside grow to roughly the
     diameter that ring used to occupy, so the icon IS the control instead of a
     small glyph floating in a lot of empty circle.

     Phone-scoped in full: on the desktop BOTH controls are bordered mono
     text chips ("SEARCH", "SETTINGS") and neither has an icon to scale. This
     block is where they become icons instead — it reverses both half-swaps
     and strips the pill recipe those two rules above set. */
    /* Reverse both half-swaps: icons in, words out. .searchicon/.searchlabel
       need no specificity boost (this block is BELOW their rules); .gearicon
       does, because ITS desktop rule sits below the masthead block that
       reveals it — see the note up there. */
    .searchlabel { display: none; }
    .searchicon { display: inline-block; }
    summary.gear, summary.searchtoggle {
      /* Box grows 1.9rem -> 2.2rem: with the border gone the control reads
         LIGHTER than before even slightly larger, and the icons need the room.
         Still a single fixed rem box on both — the gear's mono glyph and the
         search svg have different natural metrics, so equal padding alone
         never lines them up.

         line-height 0 matters on both: each control's desktop rule leaves a
         normal text line box (they are TEXT chips there), and at these font
         sizes that box is taller than the button — 44px inside 35.2px for the
         gear. The glyph itself fits; its line box does not, and the open
         state's accent fill is painted on the BUTTON, so an overflowing line
         box leaves the mark hanging out of its own filled pill (caught live).
         text-transform/letter-spacing are the mono chip's, and on a lone
         glyph the tracking is a TRAILING gap that shoves it ~3px off-centre
         in a fixed box. */
      width: 2rem; height: 2rem; padding: 0; border: none;
      line-height: 0; letter-spacing: 0;
      display: inline-flex; align-items: center; justify-content: center;
    }
    /* Both marks are sized to the WORDMARK, not to each other or to their
       own boxes (owner-requested: "same size as the NEWS text"). "NEWS" inks
       16.5px tall at its phone size, so that is the number all three of these
       resolve to — and each glyph needs a different multiplier to get there,
       which is why this cannot just be one shared font-size:
         · the mono ⚙ inks ~0.603 of its font-size -> 1.7rem  ≈ 16.4px
         · the magnifier inks ~0.82 of its svg box -> 1.25rem ≈ 16.4px
       The 2rem button box is then just tap target around them, not a size. */
    summary.gear { font-size: 1.7rem; }
    .searchicon { width: 1.25rem; height: 1.25rem; }
    /* The magnifier is drawn on a 16-unit viewBox at stroke-width 1.6, tuned
       for the old 13px icon (~1.3px of stroke). At 1.25rem the original 1.6
       would render ~2px, heavy for a mark this size next to the page's
       hairline chrome, so it thins to hold ~1.6px. CSS beats the SVG
       presentation attribute, so the desktop icon keeps its 1.6 untouched. */
    .searchicon circle, .searchicon path { stroke-width: 1.3; }
  }
  /* Reduced motion: the masthead still hides and returns, it just does not
     slide. Written as its own query rather than nested so it reads the same
     way as every other motion gate in this file. */
  @media (max-width: 40em) and (prefers-reduced-motion: reduce) {
    header.mast { transition: none; }
  }
  /* Print (roadmap step 7 — polish): a printed digest page is a read-later/
     archival copy of a briefing, not a screenshot of the site chrome — strip
     everything that only makes sense on-screen and force plain black-on-
     white text regardless of the reader's light/dark theme. The serif prose
     already suits print as-is; day headers and the dateline stay mono
     (--font-data is untouched here, only color is forced). */
  @media print {
    body { background: #fff; }
    /* The lifted-sheet rules land on print too (a printed page is often
       wider than 52em), so the card has to be flattened back to paper
       here: #64's border reset was the first half of this, the background
       and shadow are the rest. */
    .wrap { max-width: none; padding: 0; border: 0; background: #fff; box-shadow: none; min-height: 0; }
    .mast, .mast-big, .issueline, .viewtabs, nav.digestnav, .backfab, .toc,
    .searchpop, .miniseg, .densitytoggle, .resumechip,
    .archiveresults, .catchup, .followtoggle {
      display: none;
    }
    .digest, .digest p, .digest h2, .stamp, .edhead h1, .headline, .dayhead, .empty,
    .en-only-note, .arctitle, .arccontext, .arccontextbody p {
      color: #000;
    }
    /* "What changed" block (§11.3 delta persistence, ingest v4): CONTENT,
       not chrome — it's the same "communicate what changed" information the
       reader would otherwise have to reconstruct from the article prose, so
       it stays visible and gets the same forced-ink treatment as .stamp/
       .digest p above, not the .arcs/.toc/.catchup treatment (a navigation
       aid, safe to omit; a story-thread chip, whose chip-bg/chip-text colors
       are deliberately left un-forced elsewhere in this block since a chip
       is decoration a reader can live without on paper). Every child span
       (.deltaprev/.deltaarrow/.deltanow) gets its own explicit rule because
       each already carries its own on-screen color (var(--muted)/
       var(--text)) that would otherwise beat the ancestor's forced color. */
    .deltalabel, .deltatext, .deltaprev, .deltaarrow, .deltanow {
      color: #000;
    }
    /* Source counts and the model-provenance byline are both record-
       keeping, not decoration, so both colophon rows stay visible in
       print; the swatches print gray (acceptable) but the text itself
       forces to ink like every other digest-page text block above. */
    .sourcekey, .provenance { color: #000; }
    /* TL;DR/attention are tinted boxes on screen — print swaps the fills
       for thin bordered outlines: a colored background wastes ink and
       won't reproduce reliably across printers anyway. */
    .tldr, .attention {
      background: none; border: 1px solid #999; color: #000;
    }
    .attention h2 { color: #000; }
    /* Citation chips print as plain superscript text, no pill styling. */
    .cite { background: none; color: #000; }
    /* Print the destination DOMAIN (via the title attr addCiteTitles adds),
       not the full URL — a full URL would bloat print lines; the domain is
       enough provenance to look something up later. .cite[title], not
       bare .cite, so a chip whose href failed to parse (no title) doesn't
       print an empty " ()". */
    .cite[title]::after {
      content: " (" attr(title) ")";
      font-size: 0.85em;
      color: #333;
    }
  }
`;
