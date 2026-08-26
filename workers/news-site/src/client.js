// The page's entire client-side script, as one static string pageChrome
// embeds in a <script> tag. A template-literal export rather than a .js
// asset import so wrangler needs no build config (no text loader) — the
// deployed artifact stays one self-contained script.
//
// INVARIANT (tested in render.test.mjs): this string must never contain a
// backtick, a ${ sequence, or a closing script tag — any of the three
// would corrupt either this literal or the inline embedding.
export const CLIENT_SCRIPT = `  // Animated preference swap (owner-requested), shared by the theme and
  // text-size minisegs below: run a page-state mutation inside a
  // same-document View Transition so the whole page crossfades to the new
  // state instead of snapping — the same-document sibling of the
  // at-view-transition navigation crossfade this site already ships, and
  // the same restraint applies: the browser's default crossfade, no custom
  // choreography. Unsupported browsers and reduced-motion readers get the
  // instant switch they always had — the mutation itself runs either way,
  // so correctness never depends on the animation.
  function withPageTransition(mutate) {
    // The animation must never poison the mutation (owner-reported via the
    // soft-nav rollout: startViewTransition rejects with InvalidStateError
    // in hidden/render-suppressed documents, and that rejection cascaded
    // into the soft-nav promise chain, whose catch-all dutifully fell back
    // to a HARD navigation — the exact reload the soft path exists to
    // avoid). Three layers: skip the API outright when the document is
    // hidden (a snapshot of an invisible page is meaningless), try/catch
    // the call so a synchronous throw degrades to an instant mutate, and
    // swallow the transition's own promise rejections (they are cosmetic —
    // per spec the update callback still runs even when the visual
    // transition is skipped).
    if (
      document.startViewTransition &&
      !document.hidden &&
      !matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      try {
        var t = document.startViewTransition(mutate);
        if (t && t.finished && t.finished.catch) t.finished.catch(function () {});
        if (t && t.ready && t.ready.catch) t.ready.catch(function () {});
        return;
      } catch (e) {
        // fall through to the plain mutate below
      }
    }
    mutate();
  }

  // j/k list-selection navigation (§11.1 PR C): the primary-link-list half
  // of the "Keyboard navigation" IIFE inside wirePage() below — pulled out
  // as its own top-level function (rather than nested inside that IIFE)
  // since it needs no closure state of its own and stays identical across
  // every wirePage() pass; defining it once here, not per pass, costs
  // nothing and avoids re-creating an identical closure on every soft-nav.
  // Queries .wrap fresh on every call — never caches the list — so it
  // always reflects whatever page (and whatever the client-side filter has
  // hidden — see the !el.hidden check) is currently showing. Selection is
  // real DOM focus, not a separate highlight: the existing :focus-visible
  // rule already lights up whichever anchor gets focused, so this needs no
  // CSS of its own. No wrap-around (§11.1 spec: "stop at ends") — stepping
  // past either end of the list is simply a no-op.
  function moveNavSelection(delta) {
    var list = Array.prototype.slice
      .call(document.querySelectorAll(".wrap .nowrow, .wrap .entry"))
      .filter(function (el) {
        return !el.hidden;
      });
    if (list.length === 0) return;
    var idx = list.indexOf(document.activeElement);
    var next;
    if (idx === -1) {
      next = delta > 0 ? 0 : list.length - 1;
    } else {
      next = idx + delta;
      if (next < 0 || next >= list.length) return; // no wrap-around — stop at the end
    }
    list[next].focus();
  }

  // Soft-navigation re-wiring: every feature below used to be a standalone,
  // parse-time IIFE that ran once. A soft nav (see the module at the bottom
  // of this script) swaps .wrap's content in place without a fresh document
  // load, so all of it has to be re-runnable — wirePage() bundles every
  // feature into one function, called once on initial load and again after
  // each soft-nav swap.
  //
  // Listener lifecycle: one AbortController per wirePage() pass. Aborting
  // the previous pass's controller before making a fresh one drops every
  // listener that pass attached, in a single stroke — no duplicate-listener
  // buildup across swaps. Every addEventListener below carries this pass's
  // { signal: signal } for exactly that reason.
  //
  // Non-listener state (a running setInterval, a DOM node parked outside
  // .wrap) doesn't go away just because its listeners did, so it gets its
  // own explicit cleanup: teardown collects one closure per such case, and
  // wirePage runs and clears the whole list before rewiring.
  // ── one scroll listener for the whole page ─────────────────────────────
  // Four features react to scroll (sticky section headings, the phone
  // masthead, the back-to-top button, the resume chip). They used to attach
  // four independent global listeners, and the index filter faked a scroll
  // event to make them all recompute after it hid entries. One manager, a
  // subscriber list: features subscribe per wirePage() pass (their abort
  // signal unsubscribes them on soft-nav rewiring, same lifecycle their own
  // listeners had), and the filter now asks for the recompute by name
  // instead of forging an event.
  var scrollSubs = [];
  function notifyScrollSubs() {
    for (var i = 0; i < scrollSubs.length; i++) scrollSubs[i]();
  }
  addEventListener("scroll", notifyScrollSubs, { passive: true });
  function subscribeScroll(fn, signal) {
    scrollSubs.push(fn);
    signal.addEventListener("abort", function () {
      var ix = scrollSubs.indexOf(fn);
      if (ix !== -1) scrollSubs.splice(ix, 1);
    });
    // Every subscriber also runs once at subscribe time so a mid-page
    // reload (browser scroll restoration) starts in the right state — the
    // convention all four listeners already followed individually.
    fn();
  }

  // Shared by the keyboard-nav and palette shortcuts: no single-key
  // bindings while the reader is typing somewhere.
  function isTypingTarget(t) {
    var tag = t && t.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable);
  }

  // The followed-arcs list (catch-up banner + the arc page's follow
  // toggle read it; the toggle writes it). Absent or unparsable -> [].
  function readFollowedArcs() {
    try {
      return JSON.parse(localStorage.getItem("followedArcs") || "[]");
    } catch (e) {
      return [];
    }
  }

  // The three preference minisegs share one storage contract: the default
  // value is expressed as ABSENCE (empty dataset attr, no localStorage
  // key), so only a non-default choice is ever stored. The head script's
  // || "" reads render absence as the default on the next load.
  function setPref(datasetKey, storageKey, value, defaultValue) {
    document.documentElement.dataset[datasetKey] = value === defaultValue ? "" : value;
    try {
      if (value === defaultValue) localStorage.removeItem(storageKey);
      else localStorage.setItem(storageKey, value);
    } catch (e) {}
  }

  // One miniseg wiring (theme/size/font share it exactly): unhide the
  // buttons (progressive enhancement — no JS, no buttons), reflect the
  // active segment, and on click apply inside a page crossfade. current()
  // resolves the active value; apply(v) performs the swap (each segment's
  // quirks — theme's meta sync, the absence-as-default fold — live in its
  // own closure at the call site).
  function wireMiniseg(name, signal, current, apply) {
    var group = document.querySelector(".miniseg-" + name);
    if (!group) return;
    var buttons = group.querySelectorAll(".minisegbtn");
    for (var i = 0; i < buttons.length; i++) buttons[i].hidden = false;
    var reflect = function () {
      var active = current();
      for (var k = 0; k < buttons.length; k++) {
        var isActive = buttons[k].dataset.set === active;
        buttons[k].classList.toggle("active", isActive);
        buttons[k].setAttribute("aria-pressed", String(isActive));
      }
    };
    reflect();
    for (var j = 0; j < buttons.length; j++) {
      buttons[j].addEventListener(
        "click",
        function () {
          var v = this.dataset.set;
          withPageTransition(function () {
            apply(v);
            reflect();
          });
        },
        { signal: signal },
      );
    }
  }

  var wireController = null;
  var teardown = [];

  function wirePage() {
    if (wireController) wireController.abort();
    wireController = new AbortController();
    var signal = wireController.signal;

    teardown.forEach(function (fn) {
      fn();
    });
    teardown.length = 0;

    // Show the floating back button only after the header nav has scrolled
    // away. Passive listener; runs once immediately so a mid-page reload
    // (browser scroll restoration) starts in the right state.
    // Sticky section headings inside a digest (see .digest > h2 in the CSS).
    // A sticky element normally stops sticking at the bottom of its
    // containing block, which is what makes stacked section headers push each
    // other out of the way. These headings are flat siblings of one article,
    // so they share one containing block and would instead pin on top of each
    // other. This reproduces the missing constraint: each heading is shifted
    // up by exactly the amount the NEXT heading has encroached on it, so it
    // is eased out of view precisely as its successor arrives.
    (function () {
      var heads = [].slice.call(document.querySelectorAll(".digest > h2"));
      if (heads.length < 2) return;
      var root = document.documentElement;
      // What we last shifted each heading by. Kept so a heading's natural
      // position can be recovered from its live rect without clearing the
      // transform first, which would force a reflow on every scroll event.
      var shifted = [];
      var onHeads = function () {
        // Where the headings pin: below the masthead while it is on screen,
        // at the viewport edge once it has slid away. Matches the CSS.
        var line = root.classList.contains("masthid")
          ? 0
          : parseFloat(getComputedStyle(root).getPropertyValue("--mast-h")) || 0;
        var tops = [];
        for (var i = 0; i < heads.length; i++) {
          tops[i] = heads[i].getBoundingClientRect().top + (shifted[i] || 0);
        }
        for (var j = 0; j < heads.length; j++) {
          var push = 0;
          if (j + 1 < heads.length) {
            var h = heads[j].offsetHeight;
            // Room left between the pin line and the next heading. Once that
            // is smaller than this heading, the difference is the overlap.
            var room = tops[j + 1] - line;
            push = Math.min(h, Math.max(0, h - room));
          }
          if (push !== (shifted[j] || 0)) {
            heads[j].style.transform = push ? "translateY(" + -push + "px)" : "";
            shifted[j] = push;
          }
        }
      };
      subscribeScroll(onHeads, signal);
      addEventListener("resize", onHeads, { passive: true, signal: signal });
    })();

    // Sticky masthead on the phone (see header.mast in the CSS): hide while
    // the reader scrolls DOWN, bring it back the moment they scroll UP.
    (function () {
      var mast = document.querySelector("header.mast");
      if (!mast) return;
      var root = document.documentElement;
      var mq = matchMedia("(max-width: 40em)");
      var lastY = window.scrollY;
      var lastH = -1;
      var onMastScroll = function () {
        if (!mq.matches) {
          // Desktop: the masthead is static again, so leave no state behind
          // — a stale masthid class would translate a non-sticky header off
          // the top of the page, and a stale --mast-h would push the sticky
          // day headers and section headings down by a phone masthead's
          // height on a viewport that has none.
          root.classList.remove("masthid");
          root.style.removeProperty("--mast-h");
          lastH = -1;
          return;
        }
        var h = mast.offsetHeight;
        if (h !== lastH) {
          // offsetHeight ignores the transform, so this stays correct while
          // the masthead is translated out of view.
          root.style.setProperty("--mast-h", h + "px");
          lastH = h;
        }
        var y = window.scrollY;
        // Never hide while one of the masthead's own disclosures is open —
        // the reader would lose the panel they just tapped open.
        if (y <= h || mast.querySelector("details[open]")) root.classList.remove("masthid");
        else if (y > lastY + 4) root.classList.add("masthid");
        else if (y < lastY - 4) root.classList.remove("masthid");
        lastY = y;
      };
      subscribeScroll(onMastScroll, signal);
      mq.addEventListener("change", onMastScroll, { signal: signal });
    })();

    (function () {
      var fab = document.querySelector(".backfab");
      if (!fab) return;
      subscribeScroll(function () {
        fab.classList.toggle("show", window.scrollY > 320);
      }, signal);
    })();

    // Theme miniseg (owner upgrade: Light/Auto/Dark, replacing the old
    // two-state toggle). The head script already applied any stored override
    // to <html data-theme> before first paint, so this only wires the
    // buttons. Auto is the ABSENCE of an override (see setPref) — the old
    // toggle's missing third state.
    //
    // theme-color meta values: duplicated from the CSS palette's --bg
    // light/dark values — the third-copy problem again (the palette already
    // lives 3x in CSS for the no-build-step manual override), but it changes
    // rarely and there's no build step here to share one source between CSS
    // and JS. Light/Dark collapse both metas to the SAME value; Auto
    // restores each meta to its OWN media-appropriate color (a meta whose
    // media attr mentions "light" gets the light color) instead of leaving
    // both stuck on whichever value the last manual click set.
    var THEME_COLORS = { light: "#ffffff", dark: "#131418" };
    var syncThemeColorMetas = function (theme) {
      document.querySelectorAll('meta[name="theme-color"]').forEach(function (m) {
        var color =
          theme === "auto"
            ? m.media.indexOf("light") !== -1
              ? THEME_COLORS.light
              : THEME_COLORS.dark
            : THEME_COLORS[theme];
        m.setAttribute("content", color);
      });
    };
    // On load, if a stored override is already in effect, sync the metas to
    // match. A momentary wrong chrome tint before this script runs is the
    // accepted tradeoff — the head script's synchronous one-liner can't
    // reach the metas and keep this logic in one place. Nothing to do for
    // auto — the static per-meta content values already ARE the auto values.
    var storedTheme = document.documentElement.dataset.theme;
    if (storedTheme === "dark" || storedTheme === "light") syncThemeColorMetas(storedTheme);
    wireMiniseg(
      "theme",
      signal,
      function () {
        return document.documentElement.dataset.theme || "auto";
      },
      function (v) {
        setPref("theme", "theme", v, "auto");
        syncThemeColorMetas(v);
      },
    );

    // Text size miniseg: S/M/L. M is the default expressed as the ABSENCE
    // of data-fontsize (see the CSS + setPref), so only s/l touch storage.
    wireMiniseg(
      "size",
      signal,
      function () {
        var stored = document.documentElement.dataset.fontsize;
        return stored === "s" || stored === "l" ? stored : "m";
      },
      function (v) {
        setPref("fontsize", "fontsize", v, "m");
      },
    );

    // Body-font miniseg (owner-requested serif toggle): Sans is the default
    // expressed as the ABSENCE of data-font, so only serif touches storage.
    // The head script applied any stored choice before first paint — no
    // font flash.
    wireMiniseg(
      "font",
      signal,
      function () {
        return document.documentElement.dataset.font === "serif" ? "serif" : "sans";
      },
      function (v) {
        setPref("font", "font", v, "sans");
      },
    );

    // Ledger density toggle (roadmap 4 step 4). Same shape as the theme/size
    // minisegs just above: the head script already applied any stored
    // preference to <html data-density> before first paint, so this only
    // wires the button — unhide it, reflect the current state in
    // aria-pressed, and on click flip compact<->comfortable and persist it.
    // Unlike theme there's no OS-preference fallback to resolve — comfortable
    // (empty string) IS the default, so effective state is just the dataset.
    (function () {
      var btn = document.querySelector(".densitytoggle");
      if (!btn) return;
      btn.hidden = false;
      var reflect = function () {
        btn.setAttribute("aria-pressed", String(document.documentElement.dataset.density === "compact"));
      };
      reflect();
      btn.addEventListener("click", function () {
        var next = document.documentElement.dataset.density === "compact" ? "" : "compact";
        // Crossfade the ledger reflow — same withPageTransition treatment the
        // theme and size minisegs get (owner-reported: density was the one
        // preference left snapping; it predates the helper).
        withPageTransition(function () {
          document.documentElement.dataset.density = next;
          try {
            localStorage.setItem("density", next);
          } catch (e) {}
          reflect();
        });
      }, { signal: signal });
    })();

    // Settings bubble close polish (owner redesign). Opening needs no JS at
    // all — details/summary opens natively, and the CSS above animates that
    // open state directly off the [open] attribute. Closing is different:
    // <details> snaps shut the instant open is set false, with no hook to
    // intercept, so this IIFE's close() gives the mirrored close animation
    // (see the CSS above) somewhere to run before the panel actually leaves —
    // then wires outside-click, Escape, AND the gear's own click (so every
    // path that can close the bubble animates it the same way) through that
    // one helper.
    (function () {
      var settings = document.querySelector("details.settings");
      if (!settings) return;
      var panel = settings.querySelector(".settingspanel");
      var summary = settings.querySelector("summary.gear");
      var closing = false;
      var close = function () {
        if (!settings.open || closing) return;
        closing = true;
        // "panelclosing", not the shorter "closing" — .closing already names
        // the digest article's closing-line style elsewhere on this page,
        // and reusing it here would leak that border/italic/spacing onto the
        // settings panel for the animation's duration (see the CSS above).
        panel.classList.add("panelclosing");
        // setTimeout, not transitionend/animationend: an end event can simply
        // never fire (interrupted mid-animation, reduced motion turning the
        // animation into a no-op, a stray browser quirk) and would strand the
        // panel open with the "panelclosing" class stuck on it forever. A
        // plain timer always fires. Under reduced motion the "panelclosing"
        // class's animation is a no-op (it lives inside the
        // prefers-reduced-motion: no-preference gate above), but this
        // timeout still runs its full 120ms before the panel closes — an
        // imperceptible, acceptable delay rather than a second, motion-aware
        // code path.
        setTimeout(function () {
          panel.classList.remove("panelclosing");
          settings.open = false;
          closing = false;
        }, 120);
      };
      // Dismiss-tap swallowing (owner-reported): with the bubble open, a tap
      // outside used to close it AND activate whatever sat under the finger —
      // closing the panel by tapping an article link opened the article. An
      // open bubble behaves like a modal with an invisible scrim now: the
      // first outside tap only dismisses. Capture phase + preventDefault +
      // stopPropagation is what actually swallows the click before the
      // underlying link/button ever sees it — a bubble-phase listener would
      // run after the link's default navigation was already committed.
      document.addEventListener(
        "click",
        function (e) {
          if (settings.open && !closing && !settings.contains(e.target)) {
            e.preventDefault();
            e.stopPropagation();
            close();
          }
        },
        { capture: true, signal: signal },
      );
      // Keep the bubble open across a language hop (owner-reported: switching
      // language closed the menu — it's a full navigation, so the fresh
      // document rendered with the details in its default closed state).
      // sessionStorage, not localStorage: "the settings were open" is
      // navigation state, not a preference — it must not resurrect the panel
      // tomorrow. Set on language-link click inside the panel, consumed
      // (removed) on the very next load. No animation on the restore —
      // the panel was never closed from the reader's point of view.
      try {
        if (sessionStorage.getItem("settingsOpen")) {
          sessionStorage.removeItem("settingsOpen");
          settings.open = true;
        }
      } catch (e) {}
      panel.addEventListener("click", function (e) {
        var link = e.target.closest ? e.target.closest(".langswitch a") : null;
        if (link) {
          try {
            sessionStorage.setItem("settingsOpen", "1");
          } catch (err) {}
        }
      }, { signal: signal });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && settings.open) {
          close();
          summary.focus();
        }
      }, { signal: signal });
      // Without this, clicking the gear while open would let <details> close
      // itself natively and instantly, skipping the animation entirely — the
      // native toggle already handles OPENING fine (nothing to intercept
      // there), so this only ever preventDefaults the closing half of the
      // click. No-JS readers keep the plain native open/close (progressive
      // enhancement) — this listener simply never attaches for them.
      summary.addEventListener("click", function (e) {
        if (settings.open) {
          e.preventDefault();
          close();
        }
      }, { signal: signal });
    })();

    // Search bubble open-focus (owner-requested index cleanup, index pages
    // only — guarded on details.searchpop existing, since digest/arc pages
    // never render it). Opening itself needs no JS at all — same native
    // <details>/<summary> toggle the settings bubble above relies on — this
    // IIFE only adds the one explicit behavioral ask the brief called out:
    // move focus into the filter input the moment the popover opens, so a
    // pointer click on the trigger lands the reader ready to type without a
    // second, separate focus step. The native "toggle" event fires for BOTH
    // opening and closing (unlike click, which only fires on the summary
    // itself), so this checks details.open rather than assuming direction.
    // Outside-click and Escape dismissal match the settings bubble exactly
    // (owner-reported: "i cant close the search box like at settings, just
    // with another click on the button"). A native <details> only closes via
    // its own summary, which is fine for a menu nobody expects to behave like
    // a popover — but this one sits beside the gear and looks identical to
    // it, so it has to dismiss identically. Capture phase + preventDefault +
    // stopPropagation for the same reason close() uses them above: with the
    // panel open, the first outside tap must ONLY dismiss, never also
    // activate the link or button under the finger. No closing animation
    // dance here — the search panel has no .panelclosing counterpart, so it
    // just closes.
    (function () {
      var pop = document.querySelector("details.searchpop");
      if (!pop) return;
      var input = pop.querySelector(".filter");
      if (!input) return;
      pop.addEventListener("toggle", function () {
        if (pop.open) input.focus();
      }, { signal: signal });
      document.addEventListener(
        "click",
        function (e) {
          if (pop.open && !pop.contains(e.target)) {
            e.preventDefault();
            e.stopPropagation();
            pop.open = false;
          }
        },
        { capture: true, signal: signal },
      );
      // Escape closes regardless of focus target, same unconditional
      // convention the settings bubble and the ⌘K palette both use. Focus
      // returns to the trigger so keyboard users aren't stranded on a
      // detached input.
      document.addEventListener(
        "keydown",
        function (e) {
          if (e.key === "Escape" && pop.open) {
            pop.open = false;
            var toggle = pop.querySelector("summary.searchtoggle");
            if (toggle) toggle.focus();
          }
        },
        { signal: signal },
      );
    })();

    // Unread fence (roadmap 2 step 2, index pages only — guarded on an
    // .entry[data-created] existing, since digest pages carry neither). A
    // localStorage last-visit timestamp turns the index into an inbox: one
    // labeled hairline between digests that arrived since the reader was last
    // here and everything older. No JS = no fence (progressive enhancement —
    // the class is never in the server-rendered markup).
    (function () {
      var entries = Array.prototype.slice.call(document.querySelectorAll(".entry[data-created]"));
      if (entries.length === 0) return;
      var section = document.querySelector("section[data-unread-label]");
      if (!section) return;
      // Archive-week bail (roadmap 3 step 3): data-week-archive marks the
      // <section> on any non-current week (see renderIndexPage). An archive
      // page's "newest" entry is old news by definition, so there's nothing
      // to fence AND nothing here should ever be treated as the reader's most
      // recent visit — bailing before the lastVisit read/write below is what
      // stops a stray archive-page visit from regressing the stamp and
      // spawning a bogus fence around content the reader has long since seen.
      // The forward-only guard on the write itself (below) is the second,
      // independent layer of the same protection.
      if (section.hasAttribute("data-week-archive")) return;

      var lastVisit = null;
      try {
        lastVisit = localStorage.getItem("lastVisit");
      } catch (e) {}

      // entries are in document order = newest first (lead card first, see the
      // renderIndexPage/renderLeadCard comments), so entries[0] is the newest
      // digest in this view overall.
      var newest = entries[0].getAttribute("data-created");

      if (lastVisit) {
        // Find the LAST entry (in document order) newer than lastVisit — i.e.
        // the last "new" one before the "old" run begins. created_at is an
        // ISO UTC string, so > is a correct lexicographic comparison.
        var lastNewIndex = -1;
        for (var i = 0; i < entries.length; i++) {
          if (entries[i].getAttribute("data-created") > lastVisit) lastNewIndex = i;
        }
        // Draw the fence only for a genuine mix: at least one new entry AND at
        // least one old entry after it. lastNewIndex === -1 means nothing is
        // new (skip); lastNewIndex === entries.length - 1 means EVERYTHING is
        // new — typically a first-ever visit — and a fence above zero old
        // entries would just be noise, so skip that too.
        if (lastNewIndex >= 0 && lastNewIndex < entries.length - 1) {
          var fence = document.createElement("div");
          fence.className = "unreadfence";
          fence.setAttribute("role", "separator");
          var lineBefore = document.createElement("span");
          lineBefore.className = "line";
          var label = document.createElement("span");
          label.className = "label";
          label.textContent = section.getAttribute("data-unread-label");
          var lineAfter = document.createElement("span");
          lineAfter.className = "line";
          fence.appendChild(lineBefore);
          fence.appendChild(label);
          fence.appendChild(lineAfter);

          // Insertion point is strictly "after the last new .entry element" —
          // if that entry's next sibling happens to be a .dayhead, the fence
          // lands above the day header, which reads naturally.
          var lastNewEntry = entries[lastNewIndex];
          lastNewEntry.parentNode.insertBefore(fence, lastNewEntry.nextSibling);

          // Resume chip (roadmap 4 step 3): the fence above is passive — a
          // reader landing at the top of a long index has no way to know it
          // exists further down. Built only here, alongside the fence itself,
          // so every guard that got us this far (no-JS, archive week, nothing
          // new, everything new) already applies to it too. Shown only while
          // the fence is below the viewport; tapping it scrolls the fence
          // into view and hides the chip again.
          var chip = document.createElement("button");
          chip.type = "button";
          chip.className = "resumechip";
          chip.textContent = "↓ " + label.textContent;
          document.body.appendChild(chip);
          // The chip lives on document.body, OUTSIDE .wrap — a soft-nav swap
          // replaces .wrap's content but never touches this node, so it must
          // be torn down explicitly before the next wirePage() pass runs.
          teardown.push(function () {
            chip.remove();
          });
          var updateChip = function () {
            // Visible ONLY while the fence sits below the viewport's bottom
            // edge — that's the state where the reader can't know it exists.
            // In view, scrolled past, or filter-hidden: no chip.
            chip.hidden = fence.hidden || fence.getBoundingClientRect().top <= window.innerHeight;
          };
          subscribeScroll(updateChip, signal);
          chip.addEventListener("click", function () {
            fence.scrollIntoView({ block: "center" });
            // The reader just navigated to the fence — hide the chip so it
            // doesn't overlap what they scrolled to. The scroll listener
            // above keeps it hidden for as long as the fence stays in view.
            chip.hidden = true;
          }, { signal: signal });
        }

        // Catch-up banner (§11.2): extends this SAME IIFE rather than adding a
        // second "where was I" store — see PLAN.md §11.2's reconciliation
        // note. Built from the exact lastVisit/entries the fence above
        // just used, still BEFORE the trailing localStorage.setItem further
        // down advances the stamp — so this always reports what changed since
        // the visit that's ENDING now, never the one this load is about to
        // become (same "advance after computing, never before" ordering the
        // fence itself already relies on). Gated on the hidden .catchup shell
        // existing at all — renderIndexPage only emits it on the current-week
        // all-view index (showCatchup, same gate as the NOW section), so this
        // is a no-op everywhere else without a second gate here.
        var catchup = document.querySelector(".catchup");
        if (catchup) {
          // N: every .entry newer than lastVisit. Deliberately NOT reusing
          // lastNewIndex from the fence above — that one requires a genuine
          // mix (at least one old entry too) before it's non- -1, but a
          // fence-less "everything since lastVisit is new" page (e.g. a long
          // gap between visits) is still a legitimate, non-filler catch-up to
          // report, so N is computed independently here.
          var newEntries = entries.filter(function (el) {
            return el.getAttribute("data-created") > lastVisit;
          });
          var n = newEntries.length;

          // M: NOW's .nowrow arcs whose last update is newer than lastVisit —
          // see renderNowSection's data-last-seen (§11.2). Empty (0 rows,
          // ->[]) on a 0-eligible-arcs day, same fail-safe contract every
          // other NOW-derived feature in this file already uses.
          var nowRows = Array.prototype.slice.call(document.querySelectorAll(".nowrow[data-last-seen]"));
          var updatedArcs = nowRows.filter(function (row) {
            return row.getAttribute("data-last-seen") > lastVisit;
          });
          var m = updatedArcs.length;

          // "at least one last-visit value exists" is already guaranteed by
          // this whole block living inside the enclosing if (lastVisit) above;
          // the remaining "N+M > 0" half of the spec's show-condition is this
          // check — together they're exactly "first-ever visit: no banner, no
          // filler; nothing changed: no banner either".
          if (n + m > 0) {
            var textEl = catchup.querySelector(".catchuptext");
            var parts = [];
            // Singular template on exactly one, plural otherwise — "1
            // briefings" was the owner-reported bug. Both forms ride as data
            // attributes so this stays language-agnostic (Hungarian supplies
            // identical values, see the STRINGS comment there).
            if (n > 0) {
              var briefTmpl = catchup.getAttribute(
                n === 1 ? "data-tmpl-briefings-one" : "data-tmpl-briefings",
              );
              parts.push(briefTmpl.replace("{n}", String(n)));
            }
            if (m > 0) {
              // Follow list (§11.2, optional feature): followed arcs among the
              // updated ones get named (up to 3, linked) before the bare
              // count — see the follow-toggle IIFE below for where slugs get
              // written to localStorage. Nothing followed (the default —
              // "automatic-first" guardrail) falls straight through to the
              // bare "{m} arc updates" template below, exactly as if this
              // optional feature didn't exist.
              var followed = readFollowedArcs();
              var followedUpdated = followed.length
                ? updatedArcs.filter(function (row) {
                    return followed.indexOf(row.getAttribute("data-arc-slug")) !== -1;
                  })
                : [];
              if (followedUpdated.length > 0) {
                var named = followedUpdated.slice(0, 3);
                var rest = m - named.length;
                // Built via DOM nodes, not innerHTML — arc labels are
                // LLM-derived text, never trusted as markup, same discipline
                // the fence/resume chip above already follow.
                var arcFrag = document.createDocumentFragment();
                named.forEach(function (row, idx) {
                  if (idx > 0) arcFrag.appendChild(document.createTextNode(", "));
                  var a = document.createElement("a");
                  a.href = row.getAttribute("href");
                  a.textContent = row.querySelector(".nowarclabel").textContent;
                  arcFrag.appendChild(a);
                });
                if (rest > 0) {
                  arcFrag.appendChild(
                    document.createTextNode(" " + catchup.getAttribute("data-tmpl-more").replace("{n}", String(rest))),
                  );
                }
                parts.push(arcFrag);
              } else {
                parts.push(
                  catchup
                    .getAttribute(m === 1 ? "data-tmpl-arcs-one" : "data-tmpl-arcs")
                    .replace("{m}", String(m)),
                );
              }
            }

            // Compose: prefix, then each part (plain string or a link-bearing
            // fragment) joined by ", " — same manual-join-over-locale-join
            // pragmatism as renderNowSection's own join(" · ") meta line.
            textEl.appendChild(document.createTextNode(catchup.getAttribute("data-prefix") + " "));
            parts.forEach(function (part, idx) {
              if (idx > 0) textEl.appendChild(document.createTextNode(", "));
              if (typeof part === "string") textEl.appendChild(document.createTextNode(part));
              else textEl.appendChild(part);
            });

            // Jump control: only meaningful when the fence above actually got
            // built (the "genuine mix" case, fence assigned in the block
            // above) — an "everything new" or "nothing old left" page has no
            // boundary to jump to, so the control just stays hidden rather
            // than jumping nowhere. fence is var-hoisted to this IIFE's
            // top, so referencing it here is safe whether or not that block
            // ran; unassigned reads back as undefined, which is falsy.
            if (fence) {
              var jumpBtn = catchup.querySelector(".catchupjump");
              jumpBtn.hidden = false;
              jumpBtn.addEventListener("click", function () {
                fence.scrollIntoView({ block: "center" });
              }, { signal: signal });
            }

            // Dismiss: removes the banner for THIS page-view only — no
            // localStorage write, no second "seen" concept layered on top of
            // lastVisit. The natural reset is simply the next visit, once the
            // trailing advance below has moved lastVisit forward and N/M
            // recompute from a later stamp.
            catchup.querySelector(".catchupdismiss").addEventListener("click", function () {
              catchup.hidden = true;
            }, { signal: signal });

            catchup.hidden = false;
          }
        }
      }

      // Update AFTER computing the fence above, and to the NEWEST entry's own
      // data-created — not "now" — so clock skew between the reader's device
      // and the server's stamped created_at can never make a digest look
      // newer or older than it is on the next visit. Forward-only (roadmap 3
      // step 3): only ever advance the stamp, never regress it — the
      // archive-week bail above is the primary guard (it keeps this line from
      // running at all on an archive page), this comparison is the second,
      // independent layer in case that ever changes.
      try {
        if (!lastVisit || newest > lastVisit) {
          localStorage.setItem("lastVisit", newest);
        }
      } catch (e) {}
    })();

    // Follow list (§11.2, optional feature, arc pages only — guarded on
    // .archead's data-arc-slug existing, since index/digest pages have no
    // .archead). A localStorage array of followed slugs; the catch-up
    // banner's unread-fence IIFE above cross-references it when computing M's
    // named-arcs list. Zero server involvement — the arc page itself doesn't
    // know or care whether it's followed. No teardown needed: the button gets
    // appended inside .archead, which lives in the swapped .wrap content, so
    // a soft nav removes it along with everything else that pass built.
    (function () {
      var head = document.querySelector(".archead");
      var h1 = head ? head.querySelector(".arctitle[data-arc-slug]") : null;
      if (!head || !h1) return;
      var slug = h1.getAttribute("data-arc-slug");

      var followed = readFollowedArcs();

      // Text control, no button chrome (design guidance) — a plain <button>
      // element for semantics/keyboard support, styled bare by .followtoggle.
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "followtoggle";
      var render = function () {
        var isFollowed = followed.indexOf(slug) !== -1;
        btn.textContent = isFollowed ? h1.getAttribute("data-follow-remove") : h1.getAttribute("data-follow-add");
        btn.setAttribute("aria-pressed", String(isFollowed));
      };
      render();
      btn.addEventListener("click", function () {
        var idx = followed.indexOf(slug);
        if (idx === -1) followed.push(slug);
        else followed.splice(idx, 1);
        try {
          localStorage.setItem("followedArcs", JSON.stringify(followed));
        } catch (e) {}
        render();
      }, { signal: signal });
      head.appendChild(btn);
    })();

    // Index filter (roadmap step 6, index pages only — guarded on the input's
    // existence since digest pages have no .filter). applyFilters is the
    // single place that recomputes visibility from the text query, shared by
    // the input's "input" handler so it doesn't duplicate the group-hiding/
    // fence logic anywhere else. Case-insensitive substring match against each
    // .entry's text content (the lead card is an .entry too); a .dayhead hides
    // once every entry in its group (its following siblings up to the next
    // .dayhead) is hidden. No debounce at these list sizes; an empty query
    // restores everything.
    (function () {
      var input = document.querySelector(".filter");
      if (!input) return;
      // The SEARCH page's form input reuses the .filter class for its look
      // (roadmap 4 step 7) but is a server-functional control, not this
      // client-side filter — without this bail, typing a new query there
      // would live-hide the previous results before the form ever submits.
      // The index page's own filter input is the only .filter with no form.
      if (input.form) return;
      // Digest/arc pages carry the same masthead search bubble as the index
      // (consistent-masthead follow-up) but have no ledger to filter — bail
      // BEFORE the reveal, so their bubble shows only the archive search
      // link and this input never surfaces as a dead control.
      var section = document.querySelector("section[data-empty-filtered]");
      if (!section) return;
      input.hidden = false;
      var entries = Array.prototype.slice.call(document.querySelectorAll(".entry"));
      var dayheads = Array.prototype.slice.call(document.querySelectorAll(".dayhead"));
      var emptyEl = null;

      var applyFilters = function () {
        var q = input.value.trim().toLowerCase();
        var anyVisible = false;
        entries.forEach(function (el) {
          el.hidden = Boolean(q) && !el.textContent.toLowerCase().includes(q);
          if (!el.hidden) anyVisible = true;
        });
        dayheads.forEach(function (dh) {
          var group = [];
          var el = dh.nextElementSibling;
          while (el && !el.classList.contains("dayhead")) {
            if (el.classList.contains("entry")) group.push(el);
            el = el.nextElementSibling;
          }
          dh.hidden = group.length > 0 && group.every(function (e) {
            return e.hidden;
          });
        });
        // The unread fence is a load-time artifact; while the filter is active
        // it can end up orphaned between hidden entries, so just hide it
        // whenever a query is active (roadmap 2 step 2).
        var fence = document.querySelector(".unreadfence");
        if (fence) fence.hidden = Boolean(q);
        // Resume chip (roadmap 4 step 3): the chip's own scroll listener owns
        // its show/hide rule (fence hidden OR fence in view -> chip hidden),
        // so after toggling the fence above, just re-trigger that listener
        // rather than duplicating the rule here — setting chip.hidden
        // directly would wrongly un-hide it on query clear even with the
        // fence already in view. Harmless for the other scroll listeners
        // (backfab, the chip's sibling), which are all idempotent recomputes.
        // Recompute every scroll-dependent UI (chip, fab, masthead, sticky
        // headings) now that entries were hidden/shown — by name, not by
        // forging a scroll event at them.
        notifyScrollSubs();

        // Empty-filtered state (roadmap 2 step 5): lazily create the message
        // the first time the filter hides every entry, reusing .empty's
        // styling; hide it again once at least one entry is visible. Guarded
        // on entries.length so a genuinely-empty index (server already
        // rendered its own .empty message) never gets a second one.
        if (entries.length > 0 && section) {
          if (!anyVisible) {
            if (!emptyEl) {
              emptyEl = document.createElement("p");
              emptyEl.className = "empty";
              emptyEl.textContent = section.getAttribute("data-empty-filtered");
              section.appendChild(emptyEl);
            }
            emptyEl.hidden = false;
          } else if (emptyEl) {
            emptyEl.hidden = true;
          }
        }
      };

      input.addEventListener("input", applyFilters, { signal: signal });

      // Unified search (owner UX pass): the same input also live-queries the
      // full-archive search route in the background and injects results below
      // the ledger — extending THIS IIFE rather than adding a second one,
      // since it already owns the input (a second listener would just fight
      // this one for the same element). Guarded on the archiveresults
      // container existing (see renderIndexPage) — digest/search pages never
      // reach here anyway (both guards above already return before this
      // point), but the query stays defensive rather than assuming that.
      var archiveBox = document.querySelector(".archiveresults");
      if (archiveBox) {
        // Token/lang-scoped search route, read off the container rather than
        // hardcoded — keeps this script token/lang-agnostic like every other
        // data-* consumer here.
        var archiveHref = archiveBox.getAttribute("data-search-href");
        var archiveTimer = null;
        var archiveInFlight = null;

        var clearArchive = function () {
          archiveBox.innerHTML = "";
          archiveBox.hidden = true;
        };

        var runArchiveSearch = function () {
          var query = input.value.trim();
          if (query.length < 2) {
            clearArchive();
            return;
          }
          // Abort whatever's still in flight before starting a new request —
          // a slow earlier response landing after a faster later one must
          // never render stale results over fresh ones.
          if (archiveInFlight) archiveInFlight.abort();
          var controller = new AbortController();
          archiveInFlight = controller;
          fetch(archiveHref + "?q=" + encodeURIComponent(query) + "&fragment=1", { signal: controller.signal })
            .then(function (res) {
              if (!res.ok) throw new Error("archive search fetch failed");
              return res.text();
            })
            .then(function (text) {
              if (!text) {
                clearArchive();
                return;
              }
              // Safe to inject verbatim: this is our own server-rendered,
              // fully-escaped HTML from the fragment route (see
              // handleSearchPage/renderSearchFragment) — same origin, same
              // token path, every string in it already ran through esc().
              archiveBox.innerHTML = text;
              // Dedupe (roadmap: data-id): hide any injected result already
              // present in the rendered ledger above, so the reader never
              // sees the same digest twice on one page.
              var shown = new Set(
                entries.map(function (el) {
                  return el.getAttribute("data-id");
                }),
              );
              var injected = Array.prototype.slice.call(archiveBox.querySelectorAll(".entry"));
              var anyLeft = false;
              injected.forEach(function (el) {
                if (shown.has(el.getAttribute("data-id"))) {
                  el.hidden = true;
                } else {
                  anyLeft = true;
                }
              });
              // Every hit was a dupe of something already on the page: hide
              // the whole container, including its "From the archive" label —
              // an empty-looking label is worse than no box at all.
              archiveBox.hidden = !anyLeft;
            })
            .catch(function (err) {
              if (err && err.name === "AbortError") return; // superseded, not a failure
              // Search degrading to filter-only is the correct quiet failure —
              // a broken archive fetch must never surface as an error to a
              // reader who just wanted to filter the visible ledger.
              clearArchive();
            });
        };

        input.addEventListener("input", function () {
          if (archiveTimer) clearTimeout(archiveTimer);
          // Same <2-char rule as runArchiveSearch's own guard, but applied
          // synchronously here (not through the debounce) so an empty/short
          // query can never leave a stale container visible while a 300ms
          // timer is still pending.
          if (input.value.trim().length < 2) {
            if (archiveInFlight) archiveInFlight.abort();
            clearArchive();
            return;
          }
          archiveTimer = setTimeout(runArchiveSearch, 300);
        }, { signal: signal });

        // Enter triggers the pending search immediately instead of waiting out
        // the debounce. The input has no form, so Enter is otherwise inert —
        // this keeps it that way; no navigation, no submit.
        input.addEventListener("keydown", function (e) {
          if (e.key !== "Enter") return;
          if (archiveTimer) clearTimeout(archiveTimer);
          runArchiveSearch();
        }, { signal: signal });

        // One box, not two: the standalone search link is the no-JS fallback
        // (see renderSearchBubble) — once the enhanced archive box is wired
        // up, hide it. Lives in the same search-bubble panel as the filter
        // input this IIFE already owns, so a null guard costs nothing even
        // though this code path only runs where it's known to exist.
        var searchLink = document.querySelector(".searchlink");
        if (searchLink) searchLink.hidden = true;
      }
    })();


    // Keyboard navigation (roadmap 2 step 4, extended §11.1 PR C): desktop
    // convenience, no visible UI hint — the nav arrows already show the
    // model. j/ArrowLeft hop to the OLDER digest, k/ArrowRight to the NEWER
    // one, via the stable nav-older/nav-newer classes renderDigestPage puts
    // on both the top and bottom digestnav (index/arc pages have neither, see
    // below for what j/k do there instead). "/" focuses the index filter,
    // when one exists on the page. j = older = down-the-archive, matching the
    // index's newest-first reading order (vim-scroll intuition); the arrow
    // keys mirror the nav's own visual ← older / newer → arrows. Never
    // intercepts typing: bails on any input/textarea/select/contentEditable
    // target, and on any ctrl/meta/alt modifier so browser shortcuts stay
    // untouched.
    //
    // §11.1 PR C: on a page with no nav-older/nav-newer (index or arc pages —
    // digest pages always have at least one, unless at the very end of the
    // archive, see below), bare j/k (NOT the arrow keys — those stay
    // digest-nav-only) instead move a focus-based selection through the
    // page's primary link list: NOW's .nowrow rows then the ledger's .entry
    // rows, in DOM order (renderNowSection always renders before the ledger —
    // see renderIndexPage), or an arc page's timeline .entry rows (no .nowrow
    // there). Selection IS real focus (moveNavSelection's list[next].focus()),
    // so the site's existing :focus-visible ring is what shows it — no new
    // highlight styling needed. No wrap-around: stepping past either end
    // simply stops. "o" opens the currently focused row (Enter already does,
    // for free — a focused <a> activates on Enter with no JS involved); "o"
    // exists because letting Enter double as "open" conflicts with nothing
    // here, but a dedicated key some readers may expect from other
    // list-nav UIs costs one more branch.
    //
    // A one-digest archive floor (both nav-older and nav-newer absent on a
    // digest page — the archive's very first or only entry) would otherwise
    // silently fall through into the index/arc branch below; harmless in
    // practice (a digest page has no .nowrow/.entry to move a selection
    // through either, so moveNavSelection's empty-list guard just no-ops),
    // so no extra guard is needed to keep that case distinct.
    (function () {
      addEventListener("keydown", function (e) {
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        if (isTypingTarget(e.target)) return;
        if (e.key === "j" || e.key === "ArrowLeft") {
          var older = document.querySelector(".nav-older");
          // click(), not a location.href assignment: the anchor's click runs
          // through the soft-nav interceptor at the bottom of this script —
          // a direct href assignment is a HARD navigation that bypasses it,
          // which was exactly the residual white-flash path once every
          // pointer navigation had gone soft (keyboard steppers flashed,
          // taps did not).
          if (older) older.click();
          else if (e.key === "j") moveNavSelection(1);
        } else if (e.key === "k" || e.key === "ArrowRight") {
          var newer = document.querySelector(".nav-newer");
          if (newer) newer.click();
          else if (e.key === "k") moveNavSelection(-1);
        } else if (e.key === "o") {
          var active = document.activeElement;
          if (active && (active.classList.contains("nowrow") || active.classList.contains("entry"))) {
            active.click();
          }
        } else if (e.key === "/") {
          var filter = document.querySelector(".filter");
          if (filter) {
            e.preventDefault();
            // The filter input lives inside the closed-by-default search
            // popover (index-cleanup pass) — an element inside a closed
            // details is not rendered, so focus() on it silently no-ops
            // (live-verified in Chromium; there is no auto-open-on-focus
            // fixup to rely on). Open the popover first, then focus; a
            // .filter that is NOT inside the popover (the standalone search
            // page's own form input) has no .searchpop ancestor and skips
            // straight to focus, unchanged.
            var pop = filter.closest("details.searchpop");
            if (pop) pop.open = true;
            filter.focus();
          }
        }
      }, { signal: signal });
    })();

    // ⌘K command palette (§11.1 PR C): markup is created ONCE, lazily, on
    // first wirePage() pass, and left living in document.body OUTSIDE .wrap
    // (like the resume chip elsewhere in this file) — a soft-nav swap only
    // replaces .wrap's innerHTML, so re-creating this dialog on every pass
    // would be wasted work and would drop any state (results scroll position,
    // etc.) for no reason. Its CONTENT is never stale, though: every string
    // and every command is read fresh off the current page's DOM (.paletteconfig,
    // .viewtabs, .langswitch, .archivelink, .searchlink, .now, .entry — see
    // collectPaletteItems) at OPEN time, not at creation time, so a soft-nav
    // page change (including a language hop) is always reflected the next
    // time the palette opens, even though the dialog element itself never
    // gets rebuilt. Listeners on the dialog's own elements ARE rebound every
    // wirePage() pass via signal — same convention as every other feature in
    // this function — which is what "hook into it correctly rather than
    // double-binding" means here: idempotent creation (the "if (!overlay)"
    // guard below) plus per-pass listener rebinding (via signal), never both
    // firing a duplicate DOM append.
    (function () {
      var config = document.querySelector(".paletteconfig");
      if (!config) return; // pageChrome always renders this — defensive only

      var overlay = document.querySelector(".cmdpalette-backdrop");
      var dialog, input, list;
      if (!overlay) {
        overlay = document.createElement("div");
        overlay.className = "cmdpalette-backdrop";
        overlay.hidden = true;
        dialog = document.createElement("div");
        dialog.className = "cmdpalette";
        dialog.setAttribute("role", "dialog");
        dialog.setAttribute("aria-modal", "true");
        // "labelled by the input" (§11.1 PR C spec), not a separate heading —
        // the input doubles as both the dialog's accessible name AND the
        // control the reader actually types into.
        dialog.setAttribute("aria-labelledby", "cmdpalette-input");
        input = document.createElement("input");
        input.type = "text";
        input.id = "cmdpalette-input";
        input.className = "cmdpalette-input";
        input.autocomplete = "off";
        input.spellcheck = false;
        list = document.createElement("div");
        list.className = "cmdpalette-list";
        list.setAttribute("role", "listbox");
        dialog.appendChild(input);
        dialog.appendChild(list);
        overlay.appendChild(dialog);
        document.body.appendChild(overlay);
      } else {
        dialog = overlay.querySelector(".cmdpalette");
        input = overlay.querySelector(".cmdpalette-input");
        list = overlay.querySelector(".cmdpalette-list");
      }

      var currentResults = [];
      var activeIndex = -1;
      var previouslyFocused = null;

      // Assembled fresh on every open — see the IIFE's own comment above for
      // why this must never be cached across soft-navs. Static commands
      // first, then whatever the current page's own DOM contributes (arcs,
      // then briefings) — same "commands, then arcs, then briefings" order
      // the §11.1 spec lists them in.
      function collectPaletteItems() {
        var items = [];
        items.push({
          label: config.getAttribute("data-cmd-top"),
          action: function () {
            window.scrollTo({ top: 0, behavior: "smooth" });
          },
        });
        // View tabs: only the non-active ones carry an href (see
        // renderViewTabs) — reading data-view off each rather than parsing
        // translated tab text is what lets this stay lang-agnostic.
        Array.prototype.slice.call(document.querySelectorAll(".viewtabs .viewtab[href]")).forEach(function (tab) {
          var v = tab.getAttribute("data-view");
          var key = v === "daily" ? "data-cmd-daily" : v === "weekly" ? "data-cmd-weekly" : "data-cmd-all";
          items.push({ label: config.getAttribute(key), el: tab });
        });
        var searchLink = document.querySelector(".searchlink");
        if (searchLink) items.push({ label: config.getAttribute("data-cmd-search"), el: searchLink });
        // The masthead Archive link this used to read (.archivelink) was
        // removed in the index-cleanup pass — this lookup now always comes up
        // empty, so the "Archive" command simply never gets pushed, the same
        // graceful-disappearance behavior every other optional command here
        // already relies on (compare searchLink/langLink/latest just above and
        // below). Left in place rather than deleted: harmless dead code that
        // documents its own absence, and a future masthead Archive link (if
        // one ever comes back) would only need its class restored, not this.
        var archiveLink = document.querySelector(".archivelink");
        if (archiveLink) items.push({ label: config.getAttribute("data-cmd-archive"), el: archiveLink });
        var langLink = document.querySelector(".langswitch a");
        if (langLink) items.push({ label: config.getAttribute("data-cmd-switchlang"), el: langLink });
        var latest = document.querySelector(".wrap .entry");
        if (latest) items.push({ label: config.getAttribute("data-cmd-latest"), el: latest });

        // Arcs: NOW's .nowrow rows (label + href), when present (§11.1 PR C
        // spec 2b) — index pages, current week only, see renderNowSection.
        Array.prototype.slice.call(document.querySelectorAll(".now .nowrow")).forEach(function (row) {
          var labelEl = row.querySelector(".nowarclabel");
          items.push({ label: (labelEl ? labelEl.textContent : row.textContent).trim(), el: row, group: "arc" });
        });

        // Briefings: visible ledger .entry links, capped at 20 (§11.1 PR C
        // spec 2c) — their time + excerpt text as the label, same "read text
        // off the rendered DOM" approach as everywhere else in this function.
        // Deliberately NOT deduped against the "Latest briefing" command
        // above (that command is a convenience shortcut to the same target,
        // not a separate source) — a small, harmless overlap, not worth the
        // extra bookkeeping to avoid.
        Array.prototype.slice
          .call(document.querySelectorAll(".wrap .entry"))
          .slice(0, 20)
          .forEach(function (entry) {
            var timeEl = entry.querySelector(".time, .eyebrow-text");
            var excerptEl = entry.querySelector(".excerpt");
            var label = (timeEl ? timeEl.textContent + " — " : "") + (excerptEl ? excerptEl.textContent : "");
            items.push({ label: label.trim(), el: entry, group: "briefing" });
          });

        return items;
      }

      var allItems = [];

      function renderResults(query) {
        var q = query.trim().toLowerCase();
        var filtered = allItems
          .filter(function (item) {
            return !q || item.label.toLowerCase().indexOf(q) !== -1;
          })
          .slice(0, 12);
        currentResults = filtered;
        activeIndex = filtered.length > 0 ? 0 : -1;
        list.innerHTML = "";
        if (filtered.length === 0) {
          var empty = document.createElement("div");
          empty.className = "cmdpalette-empty";
          empty.textContent = config.getAttribute("data-empty");
          list.appendChild(empty);
          return;
        }
        filtered.forEach(function (item, i) {
          var row = document.createElement("div");
          row.className = "cmdpalette-item" + (i === 0 ? " active" : "");
          row.setAttribute("role", "option");
          row.setAttribute("aria-selected", String(i === 0));
          row.dataset.index = String(i);
          var labelSpan = document.createElement("span");
          labelSpan.textContent = item.label;
          row.appendChild(labelSpan);
          if (item.group) {
            var groupSpan = document.createElement("span");
            groupSpan.className = "cmdpalette-group";
            groupSpan.textContent = item.group;
            row.appendChild(groupSpan);
          }
          list.appendChild(row);
        });
      }

      function reflectActive() {
        var rows = list.querySelectorAll(".cmdpalette-item");
        for (var i = 0; i < rows.length; i++) {
          var isActive = i === activeIndex;
          rows[i].classList.toggle("active", isActive);
          rows[i].setAttribute("aria-selected", String(isActive));
          if (isActive) rows[i].scrollIntoView({ block: "nearest" });
        }
      }

      function moveActive(delta) {
        if (currentResults.length === 0) return;
        activeIndex = (activeIndex + delta + currentResults.length) % currentResults.length;
        reflectActive();
      }

      function activateSelection() {
        var item = currentResults[activeIndex];
        if (!item) return;
        closePalette();
        if (item.action) item.action();
        // el.click() (not a direct navigation): the click event bubbles up
        // to the document-level soft-nav interceptor exactly like a real
        // pointer click on that same anchor would, so results navigate
        // through the soft path — see the "Keyboard navigation" comment
        // above for why click() is used instead of location assignment
        // throughout this file.
        else if (item.el) item.el.click();
      }

      function openPalette() {
        previouslyFocused = document.activeElement;
        // Re-read every string fresh — see the IIFE's own top comment on why
        // this must never rely on values captured at creation time.
        input.placeholder = config.getAttribute("data-placeholder");
        input.setAttribute("aria-label", config.getAttribute("data-label"));
        allItems = collectPaletteItems();
        input.value = "";
        renderResults("");
        overlay.hidden = false;
        input.focus();
      }

      function closePalette() {
        if (overlay.hidden) return;
        overlay.hidden = true;
        if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
      }

      overlay.addEventListener(
        "click",
        function (e) {
          if (e.target === overlay) closePalette();
        },
        { signal: signal },
      );
      input.addEventListener("input", function () { renderResults(input.value); }, { signal: signal });
      input.addEventListener(
        "keydown",
        function (e) {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            moveActive(1);
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            moveActive(-1);
          } else if (e.key === "Enter") {
            e.preventDefault();
            activateSelection();
          }
        },
        { signal: signal },
      );
      list.addEventListener(
        "click",
        function (e) {
          var row = e.target.closest ? e.target.closest(".cmdpalette-item") : null;
          if (!row) return;
          activeIndex = Number(row.dataset.index);
          activateSelection();
        },
        { signal: signal },
      );
      // Escape closes regardless of where focus happens to be — same
      // unconditional-on-focus-target convention the settings-bubble's own
      // Escape handler above already uses (unlike the OPEN trigger below,
      // which — per the §11.1 spec's "no bindings while focus is in an
      // input/textarea/select or contenteditable" guardrail — deliberately
      // does NOT fire while the reader is typing somewhere else on the page).
      document.addEventListener(
        "keydown",
        function (e) {
          if (e.key === "Escape" && !overlay.hidden) closePalette();
        },
        { signal: signal },
      );
      document.addEventListener(
        "keydown",
        function (e) {
          if (isTypingTarget(e.target)) return;
          if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && (e.key === "k" || e.key === "K")) {
            e.preventDefault();
            openPalette();
          }
        },
        { signal: signal },
      );
    })();
  }

  // ── soft navigation ────────────────────────────────────────────────────
  // Every page here is Cache-Control: private, no-store (see the file-header
  // comment / trust model) — load-bearing, never touched — so a plain link
  // click is a full network round trip every time, and on a slow response
  // iOS Safari's canvas gap white-flashes despite color-scheme and the
  // cross-document view transition (see the :root comment above). The fix:
  // for internal links, fetch the next page in the background while the
  // CURRENT page stays fully visible, then swap the new content into the
  // live document inside a same-document View Transition (withPageTransition,
  // above) instead of letting the browser tear down and rebuild everything.
  // Nothing here is ever persisted — in-memory fetch only, gone on reload —
  // and a JS-off reader simply never gets this listener, so every link keeps
  // working as a plain navigation for them.
  //
  // Attached ONCE, at module init, before the first wirePage() call — see
  // the boot sequence at the bottom of this script.
  (function () {
    // The token lives in every href on this page already, but this script
    // never hardcodes it — the prefix is DERIVED from the page's own URL
    // (the first two path segments, "/t/<token>/") once, at module init.
    var pathSegments = location.pathname.split("/");
    var tokenPrefix = "/" + pathSegments[1] + "/" + pathSegments[2] + "/";

    // One AbortController per soft nav: starting a new one aborts whatever
    // was still in flight, and the reference comparison below (controller
    // !== activeNav) catches the rarer case where an old response arrives
    // AFTER a newer nav is already under way but wasn't itself cancelled in
    // time — a rapid string of soft-navs must never let a stale response
    // win the race and overwrite what the reader is now looking at.
    var activeNav = null;

    // The path+search this script has actually rendered, kept in the same
    // "pathname + search" shape doSoftNav takes (deliberately WITHOUT the
    // fragment). The popstate handler at the bottom compares against this
    // to tell a real history move from a same-document fragment move —
    // see there for why that distinction is load-bearing.
    var lastRendered = location.pathname + location.search;

    // hash param (§11.1 PR A follow-on fix): a CROSS-page link that also
    // carries a fragment — e.g. an arc page's deep link into a digest's own
    // #sN section (renderArcAppearance) — used to lose the fragment on the
    // soft-nav path: the click handler below only ever passed
    // dest.pathname + dest.search into this function, so the swap landed on
    // the right PAGE but never scrolled to the right SECTION, silently,
    // with no error — only a plain full navigation (or JS disabled)
    // happened to work by accident, via the browser's own native handling.
    // This was already a latent bug for the TOC's own #sN chips (dead code
    // path before this feature: no chip anywhere on the site pointed at a
    // DIFFERENT page's fragment until arc pages existed to do it) — fixed
    // here rather than shipping a feature whose flagship mechanic silently
    // degrades for every JS-enabled reader. hash is either "" or something
    // like "#sN" (URL.hash's own shape, including the "#"); scrolling only
    // happens once the swap has actually landed, same ordering as
    // wirePage() below.
    function doSoftNav(href, isPopstate, hash) {
      if (activeNav) activeNav.abort();
      var controller = new AbortController();
      activeNav = controller;
      fetch(href, { signal: controller.signal })
        .then(function (res) {
          if (!res.ok) throw new Error("soft-nav: response not ok");
          return res.text();
        })
        .then(function (html) {
          if (controller !== activeNav) return; // superseded — the newer nav owns the screen
          var doc = new DOMParser().parseFromString(html, "text/html");
          var newWrap = doc.querySelector(".wrap");
          var curWrap = document.querySelector(".wrap");
          if (!newWrap || !curWrap) throw new Error("soft-nav: .wrap missing");
          withPageTransition(function () {
            // .wrap is the ENTIRE page body except the masthead-adjacent
            // script tag and the resume chip (both live outside it) — see
            // pageChrome's document skeleton above — so swapping just its
            // innerHTML replaces everything a reader would call "the page"
            // in one move. speculationrules/prefetch artifacts in the
            // fetched document live in head/body root, never inside .wrap
            // (see pageChrome), so this scoped swap sidesteps them for
            // free — nothing to strip.
            curWrap.innerHTML = newWrap.innerHTML;
            document.title = doc.title;
            // Language hops (EN/HU switcher) change the document's lang —
            // carry that onto the live <html> along with the content.
            document.documentElement.lang = doc.documentElement.lang;
            // Deliberately UNCHANGED: html's data-theme/data-density/
            // data-fontsize/data-font. Those are live preference state, not page
            // content — leaving them alone is what makes the swap flicker-
            // free (no re-applying a preference that was already in effect).
            // Same for the theme-color metas in <head> — theme state is
            // live-owned, the fetched document's copies are simply ignored.
            //
            // The fetched document's own <script> tag never runs — an
            // innerHTML assignment inertly skips embedded scripts, and it's
            // moot here anyway since .wrap never contained the script tag
            // to begin with. The LIVE page's script owns behavior; that's
            // the whole point of re-wiring instead of reloading.
            wirePage();
            // Scroll restoration: a plain forward soft-nav or popstate
            // soft-load lands at the top, same as always. A hash-bearing
            // cross-page link (see the function comment above) scrolls the
            // target element into view instead, once it's actually in the
            // freshly-swapped DOM — getElementById, not querySelector, since
            // every id this ever targets (buildSectionToc's "sN") is a
            // literal token, never CSS-special characters. An id that isn't
            // in the fetched page (a stale/bad fragment) falls back to the
            // top, same as no hash at all — never a jump to nowhere.
            // behavior "instant", explicitly: the stylesheet's
            // scroll-behavior: smooth (reduced-motion-gated, see the CSS)
            // turns a bare scrollIntoView()/scrollTo() into an ANIMATED
            // scroll, and an animated scroll started inside this
            // startViewTransition update callback is cancelled when the
            // transition snapshots the new state — observed live on deploy
            // day: the scrollIntoView call fired, the viewport never moved.
            // An explicit behavior overrides the CSS per spec; the
            // transition's own crossfade is this swap's motion story
            // anyway, an animated scroll under it was never wanted.
            var target = hash ? document.getElementById(hash.slice(1)) : null;
            if (target) target.scrollIntoView({ behavior: "instant", block: "start" });
            else window.scrollTo({ top: 0, left: 0, behavior: "instant" });
          });
          // Committed: this address is now what's on screen. Set for BOTH
          // directions (forward soft-nav and popstate soft-load), and only
          // once the swap has actually happened — an aborted or superseded
          // nav returns above and must never move this. The hash rides
          // along in the URL bar (history.pushState) but NOT in
          // lastRendered/href themselves — every existing comparison
          // against those two (the popstate fragment-only bail below,
          // fetch(href) above) is deliberately path+search-only, and adding
          // the hash to either would break that fragment-vs-real-move
          // distinction this whole soft-nav module already depends on.
          lastRendered = href;
          if (!isPopstate) history.pushState({ soft: true }, "", href + (hash || ""));
        })
        .catch(function (err) {
          if (err && err.name === "AbortError") return; // superseded fetch, silent
          // Fetch failure, non-ok status, or any exception during the swap
          // itself: fall back to the ordinary navigation the reader always
          // had. Correctness never depends on the soft path working.
          location.href = href + (hash || "");
        });
    }

    // Capture phase, attached before wirePage's first call — see the
    // settings-dismiss handler inside wirePage above. That handler also
    // listens for document clicks in the capture phase; because this
    // listener is registered first (module init, before the initial
    // wirePage() call at the bottom of this script) and re-registering it
    // inside wirePage never moves this one, this listener always runs
    // FIRST in capture order, on every pass. That ordering is what the
    // settings-open bail below depends on.
    document.addEventListener(
      "click",
      function (e) {
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
        if (e.button !== 0) return;
        var a = e.target.closest ? e.target.closest("a[href]") : null;
        if (!a) return;
        if (a.target) return; // opens elsewhere (new tab, frame) — let it
        if (a.hasAttribute("download")) return;
        var dest;
        try {
          dest = new URL(a.href, location.href);
        } catch (err) {
          return;
        }
        if (dest.origin !== location.origin) return;
        if (dest.pathname.indexOf(tokenPrefix) !== 0) return; // cite links, robots/favicon — not ours
        // Hash-only change on the same path/query: let the browser do its
        // native in-page anchor scroll instead of soft-navving nowhere.
        if (dest.pathname === location.pathname && dest.search === location.search && dest.hash) return;
        // Dismiss-swallow interplay, for BOTH masthead disclosures: with
        // one open, a click OUTSIDE it belongs to that disclosure's dismiss
        // handler behind this listener — bail here with no preventDefault
        // so the click reaches it untouched. Clicks INSIDE an open panel
        // (the settings language switcher) soft-nav normally.
        //
        // This listener is registered at module init, BEFORE wirePage()
        // attaches either dismiss handler, and capture listeners on the
        // same node fire in registration order — so without this bail THIS
        // one wins and navigates before the dismiss handler can swallow
        // anything. That is exactly what the search popover did until now:
        // clicking a headline to dismiss it opened the headline (caught by
        // driving a real browser; the popover's own preventDefault looked
        // correct in isolation).
        var openPanel = document.querySelector("details.settings[open], details.searchpop[open]");
        if (openPanel && !openPanel.contains(a)) return;
        e.preventDefault();
        doSoftNav(dest.pathname + dest.search, false, dest.hash);
      },
      true,
    );

    addEventListener("popstate", function () {
      var here = location.pathname + location.search;
      // Fragment-only move — bail (owner-reported 2026-08-10: every TOC
      // chip scrolled down, then snapped back to the top).
      //
      // A fragment navigation ("#s4" from renderToc's chips, or any
      // in-page anchor) is a SAME-DOCUMENT navigation, and browsers fire
      // popstate for those as well as for real history traversals —
      // popstate first, then hashchange. Unguarded, this handler treated
      // that as a history move and soft-navved to the page the reader was
      // already on: the swap replaced .wrap mid-scroll, destroying the
      // element the browser's smooth scroll was animating toward, and the
      // trailing scrollTo(0, 0) put them back at the top.
      //
      // The click interceptor above already declines hash-only links (see
      // its own bail); this is the SAME condition arriving through the
      // other entry point into doSoftNav. Comparing path+search — neither
      // of which a fragment move changes — is what distinguishes them.
      // The browser's native anchor scroll is exactly right here and
      // needs no help from this script.
      if (here === lastRendered) return;
      // location.hash already reflects wherever the browser just navigated
      // the address bar to (a real history move, not a fragment-only one —
      // ruled out just above) — threaded through so a cross-page hash link
      // (see doSoftNav's own comment) still resolves correctly on a
      // back/forward traversal, not just on the initial click.
      doSoftNav(here, true, location.hash);
    });
  })();

  wirePage();`;
