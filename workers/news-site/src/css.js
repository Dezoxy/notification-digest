export const CSS = `
  /* Fonts: self-hosted variable woff2 from public/fonts (served immutable,
     preloaded by chrome.js). unicode-range is copied from SOURCES.md so the
     latin-ext files only download for pages that use ő/ű. */
  @font-face {
    font-family: "Inter";
    font-style: normal;
    font-weight: 100 900;
    font-display: swap;
    src: url(/fonts/inter-latin-wght.v1.woff2) format("woff2");
    unicode-range: U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD;
  }
  @font-face {
    font-family: "Inter";
    font-style: normal;
    font-weight: 100 900;
    font-display: swap;
    src: url(/fonts/inter-latin-ext-wght.v1.woff2) format("woff2");
    unicode-range: U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF;
  }
  @font-face {
    font-family: "Sora";
    font-style: normal;
    font-weight: 100 800;
    font-display: swap;
    src: url(/fonts/sora-latin-wght.v1.woff2) format("woff2");
    unicode-range: U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD;
  }
  @font-face {
    font-family: "Sora";
    font-style: normal;
    font-weight: 100 800;
    font-display: swap;
    src: url(/fonts/sora-latin-ext-wght.v1.woff2) format("woff2");
    unicode-range: U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF;
  }

  /* Spatial Glass design system. One stylesheet, embedded in every page.
     Rules of the house:
     - Colours live ONLY in the token block below, as light-dark() pairs, so
       a theme is one declaration per colour and the data-theme override is
       just a color-scheme switch.
     - Blur exists on exactly three floating surfaces (.bar, an open settings
       popover, the command palette). Everything else is a flat translucent
       panel with a hairline border; stacked blurs are expensive and nested
       backdrop-filter does not composite anyway.
     - Nothing on .wrap or any ancestor of .bar may carry transform, filter
       or backdrop-filter: that would make it the containing block of the
       fixed dock and break sticky positioning.
     - Motion is transform and opacity only, and every bit of it lives in the
       single no-preference block further down.
     NOTE: no backticks and no dollar-brace in this file; it is a JS template
     literal. */

  /* tokens:start */
  :root {
    --bg: light-dark(#eceff6, #0b0d14);
    --dot: light-dark(#d3d8e4, #1a1e2b);
    --glow: light-dark(#cdd5ff, #262c66);
    --text: light-dark(#151823, #eef0f7);
    --strong: light-dark(#0b0d14, #ffffff);
    --body: light-dark(#3a4052, #c3c8d6);
    --muted: light-dark(#596074, #a3a9bc);
    --panel: light-dark(rgba(255, 255, 255, 0.66), rgba(255, 255, 255, 0.055));
    --panel-2: light-dark(rgba(255, 255, 255, 0.46), rgba(255, 255, 255, 0.03));
    --border: light-dark(rgba(20, 28, 60, 0.09), rgba(255, 255, 255, 0.1));
    --border-strong: light-dark(rgba(20, 28, 60, 0.14), rgba(255, 255, 255, 0.16));
    --pill: light-dark(#ffffff, rgba(255, 255, 255, 0.15));
    --track: light-dark(rgba(20, 28, 60, 0.06), rgba(0, 0, 0, 0.28));
    --accent: light-dark(#4458d8, #9db0ff);
    --accent-soft: light-dark(rgba(68, 88, 216, 0.1), rgba(157, 176, 255, 0.14));
    --accent-line: light-dark(rgba(68, 88, 216, 0.28), rgba(157, 176, 255, 0.3));
    --accent-text: light-dark(#3243b5, #c9d3ff);
    --on-accent: light-dark(#ffffff, #0b0d14);
    --sheet: light-dark(rgba(255, 255, 255, 0.88), rgba(26, 30, 44, 0.88));
    --sheet-solid: light-dark(#ffffff, #1a1e2c);
    --link: light-dark(#3243b5, #b9c6ff);
    --daily: light-dark(#8a5a00, #f5c76b);
    --daily-soft: light-dark(rgba(214, 150, 30, 0.12), rgba(245, 199, 107, 0.1));
    --daily-line: light-dark(rgba(214, 150, 30, 0.35), rgba(245, 199, 107, 0.3));
    --scrim: light-dark(rgba(20, 28, 60, 0.35), rgba(0, 0, 0, 0.55));
    --shadow: 0 24px 60px light-dark(rgba(40, 52, 100, 0.13), transparent), 0 30px 80px light-dark(transparent, rgba(0, 0, 0, 0.38));

    --font-display: "Sora", "Inter", system-ui, sans-serif;
    --font-body: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;

    --fs-hero: clamp(34px, 2.2vw + 26px, 60px);
    --fs-hero-sm: clamp(28px, 1.6vw + 22px, 44px);
    --fs-h2: 22px;
    --fs-card: 19px;
    --fs-read: 17px;
    --lh-read: 1.75;
    --fs-ui: 14px;
    --fs-meta: 13px;
    --fs-micro: 12px;
    --read-scale: 1;

    --sp-1: 4px;
    --sp-2: 8px;
    --sp-3: 12px;
    --sp-4: 16px;
    --sp-5: 24px;
    --sp-6: 32px;
    --sp-7: 48px;
    --sp-8: 72px;

    --r-card: 24px;
    --r-lg: 28px;
    --r-in: 14px;
    --r-pill: 999px;

    --maxw: 1200px;
    --side-w: 360px;
    --main-min: 560px;
    --gutter: 32px;
    --bar-h: 56px;
    --dock-h: 60px;

    --dur-fast: 140ms;
    --dur: 240ms;
    --dur-slow: 420ms;
    --dur-enter: 600ms;
    --dur-drift: 28s;
    --ease-out: cubic-bezier(0.22, 1, 0.36, 1);
    --ease-io: cubic-bezier(0.65, 0, 0.35, 1);

    --blur-chrome: 22px;
    --blur-sheet: 28px;

    --z-bg: -1;
    --z-chip: 30;
    --z-bar: 40;
    --z-pop: 50;
    --z-palette: 100;
  }
  /* tokens:end */

  /* Theme = which half of every light-dark() pair resolves. Auto (empty
     data-theme) follows the OS. color-scheme also pins the UA canvas, so the
     gap between two page loads never flashes the wrong colour. */
  :root { color-scheme: light dark; }
  :root[data-theme="light"] { color-scheme: light; }
  :root[data-theme="dark"] { color-scheme: dark; }

  /* ── Reset and base ───────────────────────────────────────────────── */
  *, *::before, *::after { box-sizing: border-box; }
  /* An author display value beats the UA rule behind the hidden attribute,
     so without this every element the stylesheet gives a display to would
     ignore el.hidden = true. !important is what makes it win. */
  [hidden] { display: none !important; }
  html {
    background: var(--bg);
    /* A single unbreakable token wider than the screen (a defanged URL) can
       widen the layout viewport on iOS and detach fixed elements from the
       edge; clip here and on .layout caps it. */
    overflow-x: clip;
    /* Reserve the scrollbar gutter so a short page and a long page share one
       viewport width: nothing slides sideways between views. */
    scrollbar-gutter: stable;
    -webkit-text-size-adjust: 100%;
    text-size-adjust: 100%;
  }
  body {
    margin: 0;
    background: transparent;
    color: var(--text);
    font-family: var(--font-body);
    font-size: 16px;
    line-height: 1.5;
    overflow-wrap: break-word;
    -webkit-font-smoothing: antialiased;
  }
  a { color: var(--link); text-underline-offset: 0.18em; }
  /* One ring for every interactive element; components only change the
     offset where a clipping ancestor or a pill edge needs it. */
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: inherit; }
  h1, h2, h3, p { margin: 0; }
  button { font: inherit; color: inherit; }
  ::selection { background: var(--accent-line); }
  mark { background: var(--accent-soft); color: var(--strong); padding: 0 0.15em; border-radius: 4px; }

  /* ── Background layers ────────────────────────────────────────────── */
  body::before {
    content: "";
    position: fixed; inset: 0; z-index: calc(var(--z-bg) - 1);
    pointer-events: none;
    background: radial-gradient(var(--dot) 1px, transparent 1px) 0 0 / 22px 22px;
  }
  /* Oversized on the sides so the slow drift never exposes an edge. The
     status-bar strip behind it on iOS is flat theme-color (= --bg), so the
     top edge must BE the --bg colour. The mask fades the glow in over the first
     140px, and the ellipse sits below the edge, so no bright band meets the
     flat strip. currentColor only supplies opaque alpha; the mask never shows it. */
  body::after {
    content: "";
    position: fixed; top: 0; left: -10%; right: -10%; height: 100lvh; z-index: var(--z-bg);
    pointer-events: none;
    background: radial-gradient(ellipse 900px 520px at 50% 60px, var(--glow), transparent 70%);
    -webkit-mask-image: linear-gradient(to bottom, transparent 0, currentColor 140px);
    mask-image: linear-gradient(to bottom, transparent 0, currentColor 140px);
  }

  /* ── Shell: the bar ───────────────────────────────────────────────── */
  /* max() with the side insets: viewport-fit=cover lets a landscape iPhone
     (tablet layout, 32px gutter) draw under the notch otherwise. */
  .wrap {
    max-width: var(--maxw); margin: 0 auto;
    padding: 12px max(var(--gutter), env(safe-area-inset-right)) var(--sp-8) max(var(--gutter), env(safe-area-inset-left));
  }
  .bar {
    position: sticky; top: 12px; z-index: var(--z-bar);
    display: flex; align-items: center; gap: var(--sp-3);
    height: var(--bar-h);
    padding: 0 6px 0 var(--sp-5);
    border: 1px solid var(--border-strong);
    border-radius: var(--r-pill);
    background: var(--sheet);
    box-shadow: var(--shadow);
    -webkit-backdrop-filter: blur(var(--blur-chrome)) saturate(1.5);
    backdrop-filter: blur(var(--blur-chrome)) saturate(1.5);
  }
  .brand {
    font-family: var(--font-display); font-weight: 700; font-size: 22px;
    letter-spacing: -0.02em; line-height: 1;
    color: var(--strong); text-decoration: none; white-space: nowrap;
  }
  .brandmark { color: var(--accent); }
  .barback, .phonehead { display: none; }

  /* View tabs: a segmented pill on a track. The active tab is a span (the
     current page is not a link), the others are anchors. Labels may be 30%
     longer in Hungarian; the tabs shrink their padding before they wrap. */
  .viewtabs {
    display: flex; gap: 2px; padding: 2px; margin-inline: auto;
    border-radius: var(--r-pill); background: var(--track);
    min-width: 0;
  }
  .viewtab {
    display: inline-flex; align-items: center; justify-content: center;
    min-height: 44px; padding: 0 18px;
    border-radius: var(--r-pill);
    color: var(--muted); font-size: var(--fs-ui); font-weight: 500;
    text-decoration: none; white-space: nowrap;
  }
  .viewtab:hover { color: var(--strong); }
  .viewtab.active {
    background: var(--pill); color: var(--strong); font-weight: 600;
    box-shadow: 0 0 0 1px var(--border);
  }
  .viewtab:focus-visible { outline-offset: -2px; }

  .baractions { display: flex; align-items: center; gap: var(--sp-2); }
  /* The three bar buttons are one family: 44px circles (the search toggle
     grows a label on wide bars). The settings button is the filled one. */
  .barsearch, .searchtoggle, .settingsbtn {
    display: inline-flex; align-items: center; justify-content: center; gap: var(--sp-2);
    min-width: 44px; height: 44px; padding: 0;
    border: 0; border-radius: var(--r-pill);
    background: var(--track); color: var(--text);
    font-size: var(--fs-ui); font-weight: 500; line-height: 1;
    text-decoration: none; cursor: pointer;
  }
  .settingsbtn { background: var(--accent); color: var(--on-accent); }
  .barsearch:hover, .searchtoggle:hover { color: var(--strong); background: var(--accent-soft); }
  .settingsbtn svg, .searchicon { width: 18px; height: 18px; flex: none; }

  .searchpop { position: relative; }
  summary.searchtoggle { list-style: none; padding: 0 16px 0 14px; }
  summary.searchtoggle::-webkit-details-marker { display: none; }
  .searchpop[open] > summary.searchtoggle { background: var(--accent); color: var(--on-accent); }
  /* Opaque on purpose: this panel lives inside the blurred bar, and a
     backdrop-filter nested in a backdrop-filter does not render. */
  .searchpanel {
    position: absolute; top: calc(100% + var(--sp-3)); right: 0; z-index: var(--z-pop);
    width: min(340px, calc(100vw - 2 * var(--gutter)));
    overflow: hidden;
    border: 1px solid var(--border-strong); border-radius: var(--r-card);
    background: var(--sheet-solid); box-shadow: var(--shadow);
  }
  .searchpanel:focus-within { border-color: var(--accent); }
  .searchfield { display: flex; align-items: center; gap: 10px; padding: 0 16px; min-height: 52px; color: var(--muted); }
  .searchpanel:focus-within .searchfield { color: var(--accent); }
  .fieldicon { flex: none; width: 16px; height: 16px; }
  /* Hide the icon with its input: on pages with no ledger the input never
     un-hides and the icon would hang there as a dead control. */
  .searchpanel:has(.filter[hidden]) .searchfield { display: none; }
  .searchpanel .filter {
    flex: 1; min-width: 0; min-height: 44px; padding: 0;
    border: 0; background: none; color: var(--text);
    /* 16px: below that iOS zooms the page when the field takes focus. */
    font: inherit; font-size: 16px;
  }
  .searchpanel .filter::placeholder { color: var(--muted); }
  /* The panel's own focus-within border is this field's focus indicator. */
  .searchpanel .filter:focus-visible { outline: 0; }
  .searchlink {
    display: flex; align-items: center; min-height: 48px; padding: 0 16px;
    color: var(--link); font-size: var(--fs-ui); font-weight: 500; text-decoration: none;
  }
  .searchlink:hover { background: var(--accent-soft); }
  .searchlink:focus-visible { outline-offset: -3px; }
  .searchpanel:has(.filter:not([hidden])) .searchlink { border-top: 1px solid var(--border); }

  /* ── Layout ───────────────────────────────────────────────────────── */
  .layout { display: block; min-width: 0; overflow-x: clip; }
  /* Pages with no sidebar are a single reading column, centred. */
  .layout:not(.has-side) > .maincol { max-width: 760px; margin-inline: auto; }
  .maincol { min-width: 0; }
  /* Sidecards carry their own top margin so a sidebar with no cards in it
     adds zero height below the desktop breakpoint. */
  .sidecard {
    margin-top: var(--sp-5); padding: 22px;
    border: 1px solid var(--border); border-radius: var(--r-card);
    background: var(--panel);
  }
  .sidehead {
    margin: 0 0 var(--sp-3);
    font-size: var(--fs-meta); font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--muted);
  }
  .now .sidehead::before {
    content: ""; display: inline-block; width: 7px; height: 7px; margin: 0 8px 1px 0;
    border-radius: 50%; background: var(--accent);
  }
  .nowlist { display: flex; flex-direction: column; gap: 2px; }
  .nowrow {
    display: flex; align-items: center; gap: 10px; min-height: 44px;
    margin: 0 -10px; padding: 8px 10px;
    border-radius: var(--r-in); color: var(--text); text-decoration: none;
  }
  .nowrow:hover { background: var(--accent-soft); }
  .nowrow:focus-visible { outline-offset: -2px; }
  .nowarrow { flex: none; width: 1em; text-align: center; color: var(--accent); }
  .nowarclabel { flex: 1; min-width: 0; font-size: 15px; line-height: 1.35; }
  .nowmeta { flex: none; font-size: var(--fs-micro); color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .earlierrow {
    display: block; margin: 0 -10px; padding: 10px;
    border-radius: var(--r-in); color: var(--text); text-decoration: none;
  }
  .earlierrow:hover { background: var(--accent-soft); }
  .earlierrow:focus-visible { outline-offset: -2px; }
  .earliermeta {
    display: flex; align-items: center; flex-wrap: wrap; gap: 4px 8px;
    font-size: var(--fs-micro); color: var(--muted); font-variant-numeric: tabular-nums;
  }
  .earlier .earlierrow + .earlierrow { margin-top: 2px; }
  .earliertitle {
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    margin-top: 2px; font-size: 14.5px; font-weight: 500; line-height: 1.4; color: var(--strong);
  }

  /* ── Settings panel ───────────────────────────────────────────────────
     One element, three presentations (see the media blocks): a card in the
     desktop sidebar, an anchored popover on a tablet, a bottom sheet on a
     phone. Reset the UA popover look first, then NEVER give the bare
     .settingspanel a display: a closed popover must stay display:none from
     the UA, and an author display would pin it open. */
  .settingspanel {
    border: 0; padding: 0; margin: 0; inset: auto; width: auto; height: auto;
    color: inherit; background: none; overflow: visible;
  }
  .settingspanel:popover-open {
    display: flex; flex-direction: column; gap: var(--sp-4);
    padding: 22px; font-size: var(--fs-ui);
    border: 1px solid var(--border-strong);
    background: var(--sheet); box-shadow: var(--shadow);
    -webkit-backdrop-filter: blur(var(--blur-sheet)) saturate(1.5);
    backdrop-filter: blur(var(--blur-sheet)) saturate(1.5);
  }
  .settingspanel::backdrop { background: transparent; }
  .settingshead { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3); }
  .settingstitle { font-family: var(--font-display); font-weight: 600; font-size: 17px; color: var(--strong); }
  .settingsclose {
    display: inline-flex; align-items: center; justify-content: center;
    width: 44px; height: 44px; margin: -8px -10px -8px 0; padding: 0;
    border: 0; border-radius: var(--r-pill);
    background: var(--track); color: var(--text); font-size: 22px; line-height: 1; cursor: pointer;
  }
  .settingsrow { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3); }
  /* No-JS: theme, size and density controls ship [hidden] and are revealed by
     the script. A row whose controls are all still hidden would show a label
     next to nothing, so drop it until there is something to press. */
  .settingsrow:not(:has(a, button:not([hidden]), .pushnote:not([hidden]))) { display: none; }
  .settingslabel { color: var(--muted); }
  .settingsrow:has(.appversion) { padding-top: var(--sp-4); border-top: 1px solid var(--border); }
  .settingsrow > a { color: var(--link); font-weight: 500; text-decoration: none; }
  .settingsrow > a:hover { text-decoration: underline; }
  .appversion { font-size: var(--fs-micro); }

  .miniseg, .langswitch {
    display: inline-flex; padding: 3px; border-radius: var(--r-pill); background: var(--track);
  }
  .minisegbtn {
    min-height: 34px; padding: 0 13px;
    border: 0; border-radius: var(--r-pill); background: transparent;
    color: var(--muted); font-size: var(--fs-meta); font-weight: 500; cursor: pointer;
  }
  .minisegbtn:hover { color: var(--strong); }
  .minisegbtn.active { background: var(--pill); color: var(--strong); font-weight: 600; box-shadow: 0 0 0 1px var(--border); }
  .minisegbtn:focus-visible { outline-offset: -2px; }
  /* The language switch is server-rendered as EN | HU text. The separator is
     a bare text node, so font-size 0 on the group erases it and the two
     links become a segmented control without changing the markup. */
  .langswitch { font-size: 0; }
  .langswitch strong, .langswitch a {
    display: inline-flex; align-items: center; min-height: 34px; padding: 0 13px;
    border-radius: var(--r-pill); font-size: var(--fs-meta); font-weight: 500;
    color: var(--muted); text-decoration: none;
  }
  .langswitch strong { background: var(--pill); color: var(--strong); font-weight: 600; box-shadow: 0 0 0 1px var(--border); }
  .langswitch a:hover { color: var(--strong); }
  .langswitch a:focus-visible { outline-offset: -2px; }
  .pushtoggle {
    min-height: 34px; padding: 0 14px;
    border: 1px solid var(--accent-line); border-radius: var(--r-pill);
    background: var(--accent-soft); color: var(--accent-text);
    font-size: var(--fs-meta); font-weight: 600; cursor: pointer;
  }
  .pushtoggle:hover { border-color: var(--accent); }
  /* ON is a filled pill so the state reads at a glance, not by the label. */
  .pushtoggle[aria-pressed="true"] { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
  .pushnote { max-width: 60%; font-size: var(--fs-micro); line-height: 1.35; color: var(--muted); text-align: right; }

  /* ── Hero ─────────────────────────────────────────────────────────── */
  .layout > .hero, .layout > .digestnav { grid-column: 1 / -1; }
  .hero { max-width: 920px; margin-inline: auto; padding: var(--sp-7) 0 var(--sp-6); text-align: center; }
  header.hero { padding: var(--sp-6) 0 var(--sp-5); }
  .herotitle {
    margin: var(--sp-5) auto 0; max-width: 22ch;
    font-family: var(--font-display); font-weight: 600; font-size: var(--fs-hero-sm);
    line-height: 1.05; letter-spacing: -0.035em; color: var(--strong); text-wrap: balance;
  }
  section.hero .herotitle, .hero .headline-lead { font-size: var(--fs-hero); max-width: 20ch; }
  .hero-digest .herotitle, .archead .arctitle { max-width: 24ch; }
  .digest-dateline { margin: 0; color: var(--muted); font-size: var(--fs-meta); line-height: 1.3; }
  .digest-dateline::first-letter { text-transform: uppercase; }
  .hero-digest .herotitle { margin-top: var(--sp-3); }
  .eyebrow-text { text-align: center; }
  .hero > .herotitle:first-child { margin-top: 0; }
  .herochip {
    display: inline-flex; align-items: center; justify-content: center; flex-wrap: wrap; gap: 4px 10px;
    max-width: 100%; padding: 8px 16px;
    border: 1px solid var(--border); border-radius: 22px;
    background: var(--pill); color: var(--accent-text);
    font-size: var(--fs-meta); font-weight: 500; line-height: 1.3;
  }
  .livedot {
    position: relative; flex: none; width: 8px; height: 8px;
    border-radius: 50%; background: var(--accent);
    box-shadow: 0 0 0 4px var(--accent-soft);
  }
  .herosub, .hero .herosub.excerpt {
    display: block; max-width: 54ch; margin: 18px auto 0; overflow: visible;
    font-size: calc(19px * var(--read-scale)); line-height: 1.6; color: var(--body);
    -webkit-line-clamp: unset;
  }
  .hero .herolink {
    display: block; padding: 0; border: 0; border-radius: 0; background: none;
    box-shadow: none; color: inherit; text-decoration: none; transform: none;
  }
  .hero .entry-lead:hover { border-color: transparent; }
  .hero .herolink:focus-visible { border-radius: var(--r-card); outline-offset: 8px; }
  .heropills { display: flex; flex-wrap: wrap; justify-content: center; gap: 10px; margin-top: 26px; }
  .herocta, .threadpill {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    min-height: 44px; max-width: 100%; padding: 8px 20px;
    border: 1px solid var(--border); border-radius: var(--r-pill);
    font-size: var(--fs-ui); line-height: 1.25; text-decoration: none;
  }
  .herocta { border-color: var(--accent); background: var(--accent); color: var(--on-accent); font-size: 15px; font-weight: 600; }
  .threadpill { padding-inline: 16px; background: var(--accent-soft); border-color: var(--accent-line); color: var(--accent-text); }
  .threadpill:hover { border-color: var(--accent); }
  .pillcount { color: var(--muted); font-size: var(--fs-meta); }
  .hero a.herolink { color: inherit; }

  /* ── Index: ledger, entry cards, flags ────────────────────────────── */
  .ledger { display: flex; flex-direction: column; gap: var(--sp-3); }
  .dayhead {
    margin: var(--sp-4) 0 0 var(--sp-2);
    font-family: var(--font-display); font-weight: 600; font-size: 15px; color: var(--muted);
  }
  .ledger > .dayhead:first-child { margin-top: 0; }
  .entry {
    display: block; padding: 20px var(--sp-5);
    border: 1px solid var(--border); border-radius: var(--r-card);
    background: var(--panel); color: var(--text); text-decoration: none;
  }
  /* Daily and weekly entry cards carry the amber trio; the hero keeps its
     transparent surface even when its badge marks a brief. */
  .entry:not(:where(.herolink)):has(.time-accent, .flag-daily) { background: var(--daily-soft); border-color: var(--daily-line); }
  .maincol > .entry { margin-top: var(--sp-3); }
  .entry .meta {
    display: flex; align-items: center; flex-wrap: wrap; gap: 4px 12px;
    margin-bottom: 6px; font-variant-numeric: tabular-nums;
  }
  .entry .time { font-family: var(--font-display); font-weight: 600; font-size: 18px; color: var(--accent-text); }
  .entry .time-accent { color: var(--daily); }
  .entry .count { font-size: var(--fs-micro); font-weight: 500; letter-spacing: 0.02em; color: var(--muted); }
  .headline { font-size: var(--fs-card); font-weight: 600; line-height: 1.3; letter-spacing: -0.015em; color: var(--strong); text-wrap: balance; }
  .headline mark { background: var(--accent-soft); color: inherit; }
  .entry .excerpt {
    margin-top: 6px; font-size: calc(15px * var(--read-scale)); line-height: 1.55; color: var(--body);
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }
  .entry .excerpt strong { color: var(--strong); font-weight: 600; }
  .entry .excerpt-daily { -webkit-line-clamp: 4; }
  /* Prose hyphenates (Hungarian compounds overflow a narrow phone column);
     headings and chrome never do. */
  .entry .excerpt, .herosub, .tldr, .attention p, .secbody, .arccontextbody p, .aboutcard p {
    hyphens: auto; -webkit-hyphens: auto;
  }
  .entry:hover { border-color: var(--border-strong); }

  /* Flags are tags, not paragraphs: one line, always. */
  .flag {
    display: inline-flex; align-items: center; padding: 2px 10px;
    border-radius: var(--r-pill); background: var(--track); color: var(--muted);
    font-size: 11.5px; font-weight: 600; line-height: 1.4; white-space: nowrap;
  }
  .flag-daily, .flag-degraded { background: var(--daily-soft); color: var(--daily); box-shadow: inset 0 0 0 1px var(--daily-line); }
  .flag-muted { background: var(--track); color: var(--muted); }
  .herochip .flag { font-size: var(--fs-micro); }

  .empty { margin: 0 0 var(--sp-3); font-size: var(--fs-ui); color: var(--muted); }
  .en-only-note { margin: 0 0 var(--sp-4); padding: 10px 16px; border-radius: var(--r-in); background: var(--panel-2); border: 1px solid var(--border); font-size: var(--fs-ui); color: var(--muted); }

  .archiveresults { display: flex; flex-direction: column; gap: var(--sp-3); margin-top: var(--sp-5); }
  .archivelabel {
    margin: 0 0 var(--sp-2) var(--sp-2);
    font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted);
  }

  /* Catch-up banner: built by the script, so every state is styled here. */
  .catchup {
    display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-2);
    margin: 0 0 var(--sp-4); padding: 6px 6px 6px 18px;
    border: 1px solid var(--accent-line); border-radius: var(--r-pill);
    background: var(--accent-soft); color: var(--accent-text); font-size: var(--fs-ui);
  }
  .catchuptext { flex: 1 1 200px; min-width: 0; }
  .catchuptext a { color: inherit; font-weight: 600; }
  .catchupjump, .catchupdismiss {
    display: inline-flex; align-items: center; justify-content: center;
    min-width: 40px; height: 40px; padding: 0 14px;
    border: 0; border-radius: var(--r-pill); cursor: pointer;
    font-size: var(--fs-meta); font-weight: 600; line-height: 1;
  }
  .catchupjump { background: var(--accent); color: var(--on-accent); }
  .catchupdismiss { padding: 0; background: transparent; color: var(--accent-text); font-size: 20px; }
  .catchupdismiss:hover { background: var(--accent-soft); }

  /* Unread fence: a labelled rule between new and already-seen entries. */
  .unreadfence { display: flex; align-items: center; gap: var(--sp-3); margin: var(--sp-2) 0; }
  .unreadfence .line { flex: 1 1 auto; height: 0; border-top: 1px solid var(--accent-line); }
  .unreadfence .label { flex: none; font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent-text); }

  /* Week rail: older, week label, newer. */
  .weekrail {
    display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3);
    margin-top: var(--sp-4); padding: var(--sp-2);
    border: 1px solid var(--border); border-radius: var(--r-pill); background: var(--panel-2);
    font-size: var(--fs-ui);
  }
  .weekrail a {
    display: inline-flex; align-items: center; min-height: 40px; padding: 0 16px;
    border-radius: var(--r-pill); background: var(--panel); color: var(--text); text-decoration: none; white-space: nowrap;
  }
  .weekrail a:hover { background: var(--accent-soft); }
  .rail-older, .rail-newer { flex: 1; display: flex; }
  .rail-newer { justify-content: flex-end; }
  .rail-center { flex: 0 1 auto; text-align: center; color: var(--muted); font-weight: 500; }
  .railspark { display: inline-flex; align-items: flex-end; gap: 2px; height: 14px; margin-left: 10px; vertical-align: -2px; }
  .railspark i { display: block; width: 4px; border-radius: 2px; background: var(--accent); }
  .railspark i.sd0 { height: 15%; background: var(--border-strong); }

  /* ── Digest ───────────────────────────────────────────────────────── */
  .digestnav { display: flex; align-items: center; gap: var(--sp-4); margin-top: var(--sp-3); }
  .digestnav a {
    display: inline-flex; align-items: center; min-height: 44px; padding: 0 var(--sp-2);
    border-radius: var(--r-pill); color: var(--muted); font-size: var(--fs-ui); text-decoration: none;
  }
  .digestnav a:hover { color: var(--strong); background: var(--accent-soft); }
  .digestnav .spacer { flex: 1; }
  .digestnav-bottom { margin-top: var(--sp-5); }

  .deltas { display: flex; flex-direction: column; gap: var(--sp-2); margin: 0 0 var(--sp-5); }
  .delta {
    display: flex; flex-direction: column; gap: 4px; padding: 14px 18px;
    border: 1px solid var(--border); border-radius: var(--r-in);
    background: var(--panel-2); color: inherit; text-decoration: none;
  }
  .delta:hover { border-color: var(--border-strong); }
  .deltalabel { font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--accent-text); }
  .deltatext { font-size: var(--fs-ui); line-height: 1.5; }
  .deltaprev { color: var(--muted); }
  .deltaarrow { margin: 0 8px; color: var(--accent); }
  .deltanow { color: var(--strong); font-weight: 500; }

  /* Native section picker remains usable without JavaScript. */
  .tocrow { display: flex; align-items: flex-start; gap: var(--sp-2); }
  .tocpicker { flex: 1; min-width: 0; }
  .tocpicker > summary, .storythreads > summary {
    display: flex; align-items: center; gap: 8px; min-height: 44px; padding: 0 16px;
    border: 1px solid var(--border); border-radius: var(--r-pill);
    background: var(--panel); cursor: pointer; font-size: var(--fs-ui); font-weight: 600;
  }
  .tocpicker > summary::after, .storythreads > summary::after { content: "+"; margin-left: auto; }
  .tocpicker[open] > summary::after, .storythreads[open] > summary::after { content: "−"; }
  .toc { display: flex; flex-direction: column; gap: 4px; padding: 8px 0; }
  .toc a { display: flex; align-items: center; gap: 10px; min-height: 44px; padding: 8px 16px; border-radius: var(--r-in); color: var(--text); text-decoration: none; }
  .toc a:hover { background: var(--accent-soft); }
  .tocnum { flex: none; font-size: var(--fs-micro); font-weight: 600; color: var(--accent-text); font-variant-numeric: tabular-nums; }
  .toctitle { overflow-wrap: anywhere; }
  .expandall {
    flex: none; min-height: 44px; padding: 0 16px;
    border: 1px solid var(--border-strong); border-radius: var(--r-pill);
    background: var(--sheet-solid); color: var(--text); font-size: var(--fs-meta); font-weight: 600; white-space: nowrap; cursor: pointer;
  }
  .expandall:hover { border-color: var(--accent); }
  .storythreads .heropills { justify-content: flex-start; margin-top: 8px; }

  .digest { display: flex; flex-direction: column; gap: var(--sp-4); }
  .banner {
    padding: 10px 16px; border: 1px solid var(--daily-line); border-radius: var(--r-in);
    background: var(--daily-soft); color: var(--daily); font-size: var(--fs-ui); font-weight: 500;
  }
  .tldr {
    padding: 22px var(--sp-5);
    border: 1px solid var(--accent-line); border-radius: var(--r-card); background: var(--panel);
    font-size: calc(var(--fs-read) * var(--read-scale)); line-height: 1.7; color: var(--text);
  }
  .attention {
    padding: 20px var(--sp-5);
    border: 1px solid var(--daily-line); border-radius: var(--r-card); background: var(--daily-soft); color: var(--text);
  }
  .attention p { font-size: calc(15px * var(--read-scale)); line-height: 1.6; }
  .tldr-label, .attention-label {
    display: block; margin-bottom: 10px;
    font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase;
  }
  .tldr-label { color: var(--accent-text); }
  .attention-label { color: var(--daily); }

  /* Sections: a native details per story. Flat panel, no blur. */
  .sec { border: 1px solid var(--border); border-radius: var(--r-card); background: var(--panel); }
  .sechead {
    display: grid; grid-template-columns: 1fr auto auto; align-items: center; gap: 4px 12px;
    list-style: none; cursor: pointer; padding: 18px var(--sp-5);
    border-radius: var(--r-card);
  }
  .sechead::-webkit-details-marker { display: none; }
  .sechead:focus-visible { outline-offset: -4px; }
  .sechead h2 {
    margin: 0; grid-column: 1; grid-row: 1;
    font-family: var(--font-display); font-weight: 600; font-size: var(--fs-h2); line-height: 1.25;
    letter-spacing: -0.02em; color: var(--strong); text-wrap: balance;
  }
  .sechead h2[id] { scroll-margin-top: calc(var(--bar-h) + 32px); }
  .seccount { grid-column: 2; grid-row: 1; font-size: var(--fs-micro); color: var(--muted); white-space: nowrap; }
  .sechead::after {
    content: ""; grid-column: 3; grid-row: 1; width: 8px; height: 8px; margin: 0 4px 4px 0;
    border: solid var(--muted); border-width: 0 2px 2px 0; transform: rotate(45deg);
  }
  .sec[open] > .sechead::after { transform: rotate(-135deg); margin: 4px 4px 0 0; }
  .secpreview {
    grid-column: 1 / -1; grid-row: 2;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    font-size: var(--fs-ui); line-height: 1.5; color: var(--muted);
  }
  .sec[open] > .sechead :is(.secpreview, .seccount) { display: none; }

  .secbody {
    padding: 0 var(--sp-5) var(--sp-5);
    font-size: calc(var(--fs-read) * var(--read-scale)); line-height: var(--lh-read); color: var(--body);
  }
  .secbody > :first-child { margin-top: 0; }
  .secbody > :last-child { margin-bottom: 0; }
  .secbody p { margin: 0 0 1em; }
  .secbody ul, .secbody ol { margin: 0 0 1em; padding-left: 1.3em; }
  .secbody li { margin-bottom: 0.4em; }
  .secbody strong, .secbody em { color: var(--strong); }
  .secbody a { color: var(--link); }
  /* nh3 lets pre/code through, and pre's own white-space is immune to the
     body's overflow-wrap: a fenced block once re-triggered the phone
     layout-viewport bug. pre-wrap keeps it readable and contained. */
  .secbody pre, .tldr pre { white-space: pre-wrap; overflow-wrap: break-word; }
  .secarc {
    display: inline-flex; align-items: center; min-height: 28px; margin: 0 0 var(--sp-3); padding: 0 12px;
    border-radius: var(--r-pill); background: var(--accent-soft);
    color: var(--accent-text); font-size: var(--fs-meta); font-weight: 600; line-height: 1; text-decoration: none;
  }
  .secarc:hover { text-decoration: underline; }
  /* The model's closing coverage line duplicates the colophon's source key. */
  .digest p.foot { display: none; }

  .cite {
    margin-left: 1px; padding: 0 0.35em; border-radius: 6px;
    background: var(--accent-soft); color: var(--accent-text);
    font-size: 0.72em; font-weight: 700; text-decoration: none; vertical-align: super; line-height: 1;
  }
  .cite:hover, .cite:focus-visible { background: var(--accent); color: var(--on-accent); }
  .arccontext { margin: 0 0 var(--sp-4); border: 1px solid var(--border); border-radius: var(--r-in); background: var(--panel-2); }
  .arccontext summary {
    display: flex; align-items: center; min-height: 44px; padding: 0 18px; cursor: pointer;
    font-size: var(--fs-meta); font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted);
  }
  .arccontext summary:hover { color: var(--strong); }
  .arccontext summary:focus-visible, .colophon summary:focus-visible { outline-offset: -3px; }
  .arccontextbody { padding: 0 18px 16px; font-size: calc(15px * var(--read-scale)); line-height: 1.65; color: var(--body); }
  .arccontextbody p + p { margin-top: 0.9em; }

  /* Colophon: record-keeping, collapsed by default, deliberately quiet. */
  .colophon { margin-top: var(--sp-5); padding-top: var(--sp-2); border-top: 1px solid var(--border); }
  .colophon summary { display: flex; align-items: center; min-height: 44px; cursor: pointer; font-size: var(--fs-meta); color: var(--muted); }
  .colophon-warning { color: var(--daily); font-weight: 700; margin-left: 6px; }
  .visually-hidden { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
  .colophonbody .banner { margin-bottom: var(--sp-2); }
  .colophon summary:hover { color: var(--strong); }
  .colophonbody { padding: var(--sp-2) 0 var(--sp-3); }
  .sourcekey { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 16px; font-size: var(--fs-meta); color: var(--muted); }
  .sklabel { font-size: var(--fs-micro); font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; }
  .sk { display: inline-flex; align-items: center; }
  .sk i { display: inline-block; width: 9px; height: 9px; margin-right: 6px; border-radius: 50%; }
  /* .sk-failed and .sk-fallback get no colour override: muted stays muted and
     the glyph baked into the string is the marker, so colour is never the
     only warning signal. */
  .provenance { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px; align-items: baseline; margin-top: var(--sp-3); font-size: var(--fs-meta); color: var(--muted); }
  .provline { display: contents; }

  /* ── Arc ──────────────────────────────────────────────────────────── */
  .archead { display: block; }
  .followtoggle {
    display: inline-flex; align-items: center; min-height: 40px; margin-top: var(--sp-4); padding: 0 18px;
    border: 1px solid var(--border-strong); border-radius: var(--r-pill);
    background: var(--panel); color: var(--text); font-size: var(--fs-ui); font-weight: 500; cursor: pointer;
  }
  .followtoggle:hover { border-color: var(--accent); }
  .followtoggle[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent-line); color: var(--accent-text); }
  /* The thread: a left rail in accent-line, one panel per appearance, the
     newest marked by a filled dot. */
  .timeline { position: relative; list-style: none; margin: var(--sp-3) 0 0 6px; padding: 0 0 0 30px; }
  .timeline::before { content: ""; position: absolute; left: 0; top: 10px; bottom: 10px; width: 2px; border-radius: 2px; background: var(--accent-line); }
  .tlitem {
    position: relative; margin: 0 0 var(--sp-3); padding: 16px 20px;
    border: 1px solid var(--border); border-radius: var(--r-card); background: var(--panel);
  }
  .tlitem::before {
    content: ""; position: absolute; left: -35px; top: 22px; width: 10px; height: 10px;
    border: 2px solid var(--accent-line); border-radius: 50%; background: var(--bg);
  }
  .tlitem-latest::before { border-color: var(--accent); background: var(--accent); box-shadow: 0 0 0 4px var(--accent-soft); }
  .tlitem .dayhead { margin: 0 0 var(--sp-2); }
  /* The appearance link is the card; the .tlitem panel already supplies the
     surface, so the nested .entry sheds its own. */
  .tlitem .entry { padding: 0; border: 0; border-radius: 0; background: none; box-shadow: none; transform: none; }
  .tlitem .entry .excerpt { margin-top: 2px; font-size: var(--fs-card); font-weight: 600; line-height: 1.3; color: var(--strong); display: block; }
  .tlitem .deltatext { margin-top: 6px; }

  /* ── Search and About ─────────────────────────────────────────────── */
  .searchform { display: flex; gap: var(--sp-2); margin: 0 0 var(--sp-5); }
  .searchform .filter {
    flex: 1; min-width: 0; min-height: 52px; padding: 0 20px;
    border: 1px solid var(--border-strong); border-radius: var(--r-pill);
    background: var(--panel); color: var(--text); font: inherit; font-size: 16px;
  }
  .searchform .filter::placeholder { color: var(--muted); }
  .searchbtn {
    min-height: 52px; padding: 0 24px; border: 0; border-radius: var(--r-pill);
    background: var(--accent); color: var(--on-accent); font-size: 15px; font-weight: 600; cursor: pointer;
  }
  .aboutcard { padding: var(--sp-5); border: 1px solid var(--border); border-radius: var(--r-card); background: var(--panel); }
  .aboutcard h2 { margin: 0 0 var(--sp-2); font-family: var(--font-display); font-weight: 600; font-size: var(--fs-card); letter-spacing: -0.015em; color: var(--strong); }
  .aboutcard p { font-size: calc(var(--fs-read) * var(--read-scale)); line-height: var(--lh-read); color: var(--body); }
  .closing { font-style: italic; color: var(--muted); }

  /* ── Floating: resume chip, command palette ───────────────────────── */
  .resumechip {
    position: fixed; left: 50%; bottom: var(--sp-5); z-index: var(--z-chip); translate: -50% 0;
    min-height: 44px; padding: 0 20px; max-width: calc(100vw - 2 * var(--sp-4));
    border: 1px solid var(--accent-line); border-radius: var(--r-pill);
    background: var(--sheet-solid); color: var(--accent-text);
    font-size: var(--fs-meta); font-weight: 600; white-space: nowrap; box-shadow: var(--shadow); cursor: pointer;
  }
  .cmdpalette-backdrop {
    position: fixed; inset: 0; z-index: var(--z-palette);
    display: flex; justify-content: center; align-items: flex-start;
    padding: 12vh var(--sp-4) 0; background: var(--scrim);
  }
  .cmdpalette {
    display: flex; flex-direction: column; overflow: hidden;
    width: min(560px, 100%); max-height: 70dvh;
    border: 1px solid var(--border-strong); border-radius: var(--r-card);
    background: var(--sheet); box-shadow: var(--shadow);
    -webkit-backdrop-filter: blur(var(--blur-sheet)) saturate(1.5);
    backdrop-filter: blur(var(--blur-sheet)) saturate(1.5);
  }
  .cmdpalette-input {
    padding: 18px 20px; border: 0; border-bottom: 1px solid var(--border);
    background: none; color: var(--text); font: inherit; font-size: 16px;
  }
  .cmdpalette-input:focus-visible { outline-offset: -3px; }
  .cmdpalette-input::placeholder { color: var(--muted); }
  .cmdpalette-list { overflow-y: auto; padding: 6px; }
  .cmdpalette-item {
    display: flex; align-items: center; justify-content: space-between; gap: var(--sp-4);
    min-height: 44px; padding: 8px 14px; border-radius: var(--r-in); cursor: pointer; font-size: 15px; color: var(--text);
  }
  .cmdpalette-item.active { background: var(--accent-soft); color: var(--strong); }
  .cmdpalette-group { font-size: var(--fs-micro); color: var(--muted); }
  .cmdpalette-empty { padding: 14px 16px; font-size: var(--fs-ui); color: var(--muted); }

  /* ── Reading size (XS/S/L): M is the absence of data-fontsize ─────── */
  html[data-fontsize="xs"] { --read-scale: 0.86; }
  html[data-fontsize="s"] { --read-scale: 0.94; }
  html[data-fontsize="l"] { --read-scale: 1.12; }

  /* ── Motion: the only place anything animates ─────────────────────── */
  @media (prefers-reduced-motion: no-preference) {
    html { scroll-behavior: smooth; }
    @view-transition { navigation: auto; }
    body::after { animation: drift var(--dur-drift) var(--ease-io) infinite alternate; }
    @keyframes drift {
      from { transform: translate3d(-4%, 0, 0); }
      /* Downward only: drifting UP would drag the masked-out top rows above
         the viewport and let the glow touch the top edge again. */
      to { transform: translate3d(4%, 3%, 0); }
    }
    /* Hard loads only: html.enter is removed ~1.2s after the first paint and
       never comes back on soft navigation. No "to" keyframe so a hover lift
       is never frozen out by the fill. */
    html.enter :is(.hero, .maincol > *, .ledger > *, .side > :not(.settingspanel)) {
      animation: rise var(--dur-enter) var(--ease-out) both;
    }
    @keyframes rise { from { opacity: 0; transform: translateY(14px); } }
    html.enter .layout > .hero, html.enter .maincol > .digest { animation-name: rise-visible; }
    @keyframes rise-visible { from { transform: translateY(14px); } }
    html.enter :is(.hero, .maincol > :nth-child(1), .ledger > :nth-child(1), .side > :nth-child(1)) { animation-delay: 60ms; }
    html.enter :is(.maincol > :nth-child(2), .ledger > :nth-child(2), .side > :nth-child(2)) { animation-delay: 120ms; }
    html.enter :is(.maincol > :nth-child(3), .ledger > :nth-child(3)) { animation-delay: 180ms; }
    html.enter :is(.maincol > :nth-child(4), .ledger > :nth-child(4)) { animation-delay: 240ms; }
    html.enter :is(.maincol > :nth-child(5), .ledger > :nth-child(5)) { animation-delay: 300ms; }
    html.enter :is(.maincol > :nth-child(n + 6), .ledger > :nth-child(n + 6)) { animation-delay: 360ms; }

    .livedot::after {
      content: ""; position: absolute; inset: 0; border-radius: 50%; background: var(--accent);
      animation: ping 2.4s var(--ease-out) infinite;
    }
    @keyframes ping { from { opacity: 0.5; transform: scale(1); } to { opacity: 0; transform: scale(3); } }

    .entry, .tlitem, .sidecard, .delta {
      transition: transform var(--dur) var(--ease-out), border-color var(--dur-fast) linear;
    }
    @media (hover: hover) {
      .ledger .entry:hover, .maincol > .entry:hover, .archiveresults .entry:hover, .delta:hover { transform: translateY(-2px); }
    }
    .ledger .entry:focus-visible, .maincol > .entry:focus-visible { transform: translateY(-2px); }
    .sechead::after { transition: transform var(--dur) var(--ease-out); }
    /* Unfold is a keyframe, not a height transition: a details element has
       no stable height to animate from. */
    .sec[open] > .secbody { animation: unfold var(--dur) var(--ease-out); }
    @keyframes unfold { from { opacity: 0; transform: translateY(6px); } }

    .settingspanel:popover-open {
      transition: opacity var(--dur) var(--ease-out), transform var(--dur) var(--ease-out);
      transition-behavior: allow-discrete;
    }
    @starting-style {
      .settingspanel:popover-open { opacity: 0; transform: translateY(-8px) scale(0.98); }
    }
    html.theming, html.theming * {
      transition: background-color 0.3s, color 0.3s, border-color 0.3s !important;
    }
  }

  /* ── Desktop: sidebar layout ──────────────────────────────────────── */
  @media (min-width: 1040px) {
    .layout.has-side {
      display: grid; grid-template-columns: minmax(var(--main-min), 1fr) var(--side-w);
      column-gap: var(--sp-6); align-items: start;
    }
    .side > :first-child { margin-top: 0; }
    /* The settings panel is a closed popover forced visible: it is a card in
       the sidebar, so the client never opens it here. */
    .side .settingspanel:not(:popover-open) {
      display: flex; flex-direction: column; gap: var(--sp-4); position: static;
      margin-top: var(--sp-5); padding: 22px; font-size: var(--fs-ui);
      border: 1px solid var(--border); border-radius: var(--r-card); background: var(--panel);
    }
    .side .settingspanel:not(:popover-open):first-child { margin-top: 0; }
    .side .settingsclose { display: none; }
    .side .settingstitle {
      font-family: var(--font-body); font-size: var(--fs-meta); font-weight: 600;
      letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted);
    }
    .wrap:has(.layout.has-side) .settingsbtn { display: none; }
  }

  /* ── Anchored settings popover (>= 700) ───────────────────────────── */
  @media (min-width: 700px) {
    .settingspanel:popover-open {
      position: fixed;
      top: calc(12px + var(--bar-h) + var(--sp-2));
      right: max(var(--gutter), env(safe-area-inset-right), calc((100vw - var(--maxw)) / 2 + var(--gutter)));
      width: 340px; border-radius: var(--r-card);
    }
  }

  /* ── Tablet: top bar, one column, side stacked below main ─────────── */
  @media (min-width: 700px) and (max-width: 1039.98px) {
    .side { margin-top: var(--sp-2); }
  }

  /* ── Phone: dock, bottom sheet ────────────────────────────────────── */
  @media (max-width: 699.98px) {
    :root { --gutter: 16px; --fs-read: 16px; }
    .wrap { padding-bottom: calc(var(--dock-h) + 34px + env(safe-area-inset-bottom)); }
    .phonehead {
      display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3);
      min-height: 44px; padding: env(safe-area-inset-top) 4px 0 8px;
    }
    .issueline { font-size: var(--fs-meta); color: var(--muted); text-align: right; }
    .bar {
      position: fixed; top: auto; left: 12px; right: 12px;
      bottom: calc(10px + env(safe-area-inset-bottom));
      gap: 6px; padding: 0 4px 0 4px;
    }
    /* The on-screen keyboard must never push the dock over the field. */
    html.typing .bar { display: none; }
    .bar .brand, .searchlabel { display: none; }
    .barback {
      display: inline-flex; align-items: center; justify-content: center; flex: none;
      width: 44px; height: 44px; border-radius: var(--r-pill);
      background: var(--track); color: var(--text); font-size: 20px; text-decoration: none;
    }
    .viewtabs { flex: 1; margin-inline: 0; }
    .viewtab { flex: 1; padding: 0 6px; }
    /* Positioned against the bar, not the small toggle, and opens upward. */
    .searchpop { position: static; }
    summary.searchtoggle { padding: 0; }
    .searchpanel { top: auto; bottom: calc(100% + var(--sp-3)); left: 0; right: 0; width: auto; }
    /* Typing in the dock's search field: iOS does not shrink the layout
       viewport for the keyboard, so a bottom-anchored field would start under
       it. While the field has focus the dock moves to the top and the panel
       opens downward. */
    html:has(.bar .filter:focus) .bar { top: calc(12px + env(safe-area-inset-top)); bottom: auto; transform: none; }
    html:has(.bar .filter:focus) .searchpanel { top: calc(100% + var(--sp-3)); bottom: auto; }
    /* No top bar on phones (the dock is at the bottom): a #sN jump needs only
       a little air above the heading. */
    .sechead h2[id] { scroll-margin-top: 16px; }

    .hero { padding: 36px 0 28px; }
    header.hero { padding: var(--sp-5) 0 var(--sp-4); }
    header.hero-digest { padding: var(--sp-2) 0; }
    .hero-digest .herotitle { margin: 8px auto 0; font-size: 28px; }
    .herosub, .hero .herosub.excerpt { font-size: calc(17px * var(--read-scale)); }
    .entry { padding: var(--sp-4); }
    .tldr, .attention, .aboutcard { padding: 18px var(--sp-4); }
    .sechead { padding: 16px var(--sp-4); }
    .secbody { padding: 0 var(--sp-4) var(--sp-4); }
    .digestnav { margin-top: 0; }
    .timeline { padding-left: 24px; }
    .tlitem::before { left: -29px; }
    .tlitem { padding: 14px var(--sp-4); }
    .weekrail a { padding: 0 12px; }
    .resumechip { bottom: calc(10px + var(--dock-h) + 14px + env(safe-area-inset-bottom)); }
    /* html.dockhid (client.js) slides the dock away while the reader scrolls
       down. A transform on the bar itself is fine; only its ancestors must
       stay transform-free. It is inert while hidden so it cannot catch taps.
       The chip drops to the dock's resting spot via translate (not bottom),
       keeping motion transform-only. */
    html.dockhid .bar { transform: translateY(calc(100% + 10px + env(safe-area-inset-bottom) + 8px)); pointer-events: none; }
    html.dockhid .resumechip { translate: -50% calc(var(--dock-h) + 14px); }
    .pushnote { max-width: 55%; }

    .settingspanel:popover-open {
      position: fixed; inset: auto 0 0 0; width: auto; max-height: 85dvh; overflow: auto;
      padding-bottom: calc(22px + env(safe-area-inset-bottom));
      border-radius: var(--r-lg) var(--r-lg) 0 0;
      /* Opaque on the phone: the sheet covers the dock and the page's text,
         and a translucent sheet lets both show through its lower half. */
      background: var(--sheet-solid);
    }
    .settingspanel::backdrop { background: var(--scrim); }
    .settingsrow { min-height: 44px; }
    .minisegbtn, .langswitch strong, .langswitch a, .pushtoggle { min-height: 40px; }
    @media (prefers-reduced-motion: no-preference) {
      .bar { transition: transform var(--dur) var(--ease-out); }
      .resumechip { transition: translate var(--dur) var(--ease-out); }
      @starting-style {
        .settingspanel:popover-open { opacity: 0; transform: translateY(32px); }
      }
    }
  }

  /* ── No blur: reduced transparency, or a browser without backdrop-filter.
     The blurred surfaces fall back to their opaque twin; without the blur a
     translucent sheet over the dot grid is unreadable. ────────────────── */
  @media (prefers-reduced-transparency: reduce) {
    .bar, .settingspanel:popover-open, .cmdpalette {
      background: var(--sheet-solid); -webkit-backdrop-filter: none; backdrop-filter: none;
    }
  }
  @supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
    .bar, .settingspanel:popover-open, .cmdpalette { background: var(--sheet-solid); }
  }

  /* Compact spacing is screen-only; reading text and touch targets retain their size. */
  @media screen {
    /* Story threads has its own border; omit adjacent separators. */
    article.digest:has(> .storythreads:last-child) + .colophon { border-top: 0; }
    html[data-density="compact"] .ledger { gap: 0; }
    html[data-density="compact"] :is(.ledger, .archiveresults) .entry,
    html[data-density="compact"] .maincol > .entry {
      padding: 10px 0; border: 0; border-bottom: 1px solid var(--border); border-radius: 0; background: none;
    }
    html[data-density="compact"] .ledger .entry:has(.time-accent, .flag-daily) { background: none; }
    html[data-density="compact"] .ledger .entry .excerpt { -webkit-line-clamp: 2; }
    html[data-density="compact"] .ledger .entry .excerpt-daily { -webkit-line-clamp: 3; }
    html[data-density="compact"] .ledger > .dayhead { margin-top: var(--sp-5); }
    html[data-density="compact"] .archiveresults { gap: 0; margin-top: var(--sp-3); }
    html[data-density="compact"] .hero { padding: var(--sp-5) 0 var(--sp-4); }
    html[data-density="compact"] .hero .heropills { margin-top: var(--sp-3); }
    html[data-density="compact"] .digest { gap: var(--sp-2); }
    html[data-density="compact"] .toc { gap: 0; padding: 4px 0; }
    html[data-density="compact"] :is(.tldr, .attention, .aboutcard) { padding: 14px var(--sp-4); }
    html[data-density="compact"] article.digest > .sec {
      border: 0; border-bottom: 1px solid var(--border); border-radius: 0; background: none;
    }
    html[data-density="compact"] article.digest > .sec:has(+ .storythreads, + p.foot + .storythreads) {
      border-bottom: 0;
    }
    html[data-density="compact"] article.digest > .tldr {
      padding: var(--sp-1) 0 var(--sp-1) var(--sp-3);
      border: 0; border-inline-start: 3px solid var(--accent-line); border-radius: 0; background: none;
    }
    html[data-density="compact"] article.digest > .tldr .tldr-label { margin-bottom: var(--sp-1); }
    html[data-density="compact"] .sechead { padding: 12px var(--sp-4); min-height: 44px; }
    html[data-density="compact"] .secbody { padding: 0 var(--sp-4) var(--sp-4); }
    html[data-density="compact"] article.digest .sechead { padding-inline: 0; border-radius: 0; }
    html[data-density="compact"] article.digest .secbody { padding-inline: 0; }
    html[data-density="compact"] .secbody > :is(p, ul, ol):not(:last-child) { margin-bottom: 0.7em; }
    html[data-density="compact"] .secbody li { margin-bottom: 0.2em; }
    html[data-density="compact"] .deltas { margin-bottom: var(--sp-3); }
    html[data-density="compact"] .delta { padding: 10px 14px; }
    html[data-density="compact"] .tlitem { margin-bottom: var(--sp-2); padding: 12px 16px; }
    html[data-density="compact"] .tlitem::before { top: 18px; }
    html[data-density="compact"] .searchform { margin-bottom: var(--sp-3); }
  }

  @media screen and (max-width: 699.98px) {
    .digest-dateline-date { display: none; }
    .toc a { padding-inline: var(--sp-2); font-size: var(--fs-meta); line-height: 1.35; }
    /* Size the list to the whole row while the summary shares it with Collapse all. */
    @supports (container-type: inline-size) and (width: 100cqi) {
      .tocrow { container-type: inline-size; }
      .toc { width: 100cqi; }
    }
    html[data-density="compact"] header.hero { padding: var(--sp-4) 0 var(--sp-2); }
    html[data-density="compact"] header.hero-digest { padding: var(--sp-1) 0; }
  }

  /* ── Print: a briefing as a plain black-on-white document ─────────── */
  @media print {
    :root, :root[data-theme] { color-scheme: light; }
    html, body { background: #fff; color: #000; }
    body::before, body::after { display: none; }
    .wrap { max-width: none; padding: 0; }
    .bar, .phonehead, .side, .tocrow, .storythreads, .heropills, .digestnav, .catchup, .secpreview, .seccount,
    .resumechip, .cmdpalette-backdrop, .settingspanel, .followtoggle, .archiveresults, .sechead::after {
      display: none;
    }
    .layout { display: block; overflow: visible; }
    .sec, .tldr, .attention, .banner, .aboutcard, .tlitem, .entry, .delta, .arccontext {
      background: none; box-shadow: none; border-color: #999; color: #000;
    }
    .herotitle, .headline, .sechead h2, .secbody, .secbody p, .tldr, .attention, .deltatext, .deltaprev,
    .deltanow, .deltalabel, .arccontextbody, .sourcekey, .provenance, .herosub, .excerpt, .digest-dateline { color: #000; }
    h2 { break-after: avoid; }
    .sec { break-inside: auto; }
    /* A closed details hides its body through ::details-content; print forces
       it visible. A browser without the pseudo-element prints it collapsed
       (client.js opens every section on beforeprint). */
    details.sec::details-content, .colophon::details-content { content-visibility: visible; }
    .colophon summary { display: none; }
    .cite { background: none; color: #000; }
  }
`;
