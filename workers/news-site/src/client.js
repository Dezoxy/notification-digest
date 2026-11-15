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
  // Scroll-dependent UI (today just the resume chip) subscribes to one
  // manager instead of attaching its own global listener, and the index
  // filter asks for a recompute by name rather than forging a scroll event.
  // Features subscribe per wirePage() pass (their abort signal unsubscribes
  // them on soft-nav rewiring, same lifecycle their own listeners have).
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
    // reload (browser scroll restoration) starts in the right state.
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

  // The preference minisegs (theme, size, density) share one storage contract: the default
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

  // One miniseg wiring (theme/size/density share it exactly): unhide the
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

  // Theme changes crossfade colours only while <html class="theming"> is
  // present (see the CSS). One module-level timer, so rapid clicks extend the
  // window instead of the first click's timeout cutting the second short.
  var themingTimer = null;
  function markTheming() {
    var root = document.documentElement;
    root.classList.add("theming");
    if (themingTimer) clearTimeout(themingTimer);
    themingTimer = setTimeout(function () {
      themingTimer = null;
      root.classList.remove("theming");
    }, 350);
  }

  // Open the folded digest section (details.sec) that contains "el" — the
  // heading itself sits in the always-visible <summary>, so a #sN link
  // reaches it either way, but the reader following it wants the section
  // BODY. Returns true when it had to open one. Opening happens BEFORE any
  // scroll the caller does, so the layout the scroll targets is final.
  function openSectionContaining(el) {
    var sec = el && el.closest ? el.closest("details.sec") : null;
    if (sec && !sec.open) {
      sec.open = true;
      return true;
    }
    return false;
  }

  // Resolve a "#id" hash to its element (null when absent). decodeURIComponent
  // can throw on a malformed escape; fall back to the raw text.
  function hashTarget(hash) {
    if (!hash || hash.length < 2) return null;
    var id = hash.slice(1);
    try {
      id = decodeURIComponent(id);
    } catch (e) {}
    return document.getElementById(id);
  }

  // Same-document hash arrival (page load with #sN, hashchange): open the
  // target's section, then — only if the browser's own anchor jump left the
  // heading off screen — scroll it into view. The jump usually already
  // landed (the h2 lives in the summary, which never hides), so this is a
  // safety net for the ordering where the jump ran before the section was
  // open, not a second scroll fighting the first.
  function revealHash(hash) {
    var target = hashTarget(hash);
    if (!target) return;
    if (!openSectionContaining(target)) return;
    requestAnimationFrame(function () {
      var r = target.getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight) {
        target.scrollIntoView({ behavior: "instant", block: "start" });
      }
    });
  }

  // Close the settings popover if it is open. Called before a soft-nav swap
  // so the top layer never holds a node that is about to be replaced. The
  // panel is looked up fresh every time — never a cached reference — because
  // each swap replaces it along with the rest of .wrap. Guarded: browsers
  // without the Popover API have no hidePopover (and no :popover-open).
  function closeSettingsPopover() {
    var panel = document.getElementById("settings");
    if (!panel || typeof panel.hidePopover !== "function") return;
    try {
      if (panel.matches(":popover-open")) panel.hidePopover();
    } catch (e) {}
  }

  // Fixed-body locking also works on iOS Safari, where overflow:hidden alone
  // can leave the background scrollable. Release the lock synchronously as
  // the popover closes so soft navigation can set the next page position.
  (function () {
    if (!window.matchMedia) return;
    var phone = matchMedia("(max-width: 699.98px)");
    var lockedY = null;
    var drag = null;
    var releaseAnimation = null;
    function lock() {
      if (lockedY !== null) return;
      lockedY = window.scrollY;
      document.documentElement.style.setProperty("--settings-scroll-top", -lockedY + "px");
      document.documentElement.classList.add("settingslocked");
    }
    function unlock() {
      if (lockedY === null) return;
      var y = lockedY;
      lockedY = null;
      document.documentElement.classList.remove("settingslocked");
      document.documentElement.style.removeProperty("--settings-scroll-top");
      window.scrollTo({ top: y, behavior: "instant" });
    }
    function resetDrag() {
      if (releaseAnimation) {
        releaseAnimation.cancel();
        releaseAnimation = null;
      }
      if (!drag) return;
      var previous = drag;
      drag = null;
      previous.panel.classList.remove("settings-dragging");
      previous.panel.style.removeProperty("transform");
      if (previous.head.hasPointerCapture(previous.id)) previous.head.releasePointerCapture(previous.id);
    }
    document.addEventListener("beforetoggle", function (event) {
      if (event.target.id !== "settings") return;
      resetDrag();
      if (event.newState === "open" && phone.matches) lock();
      else unlock();
    }, true);
    phone.addEventListener("change", function () {
      resetDrag();
      var panel = document.getElementById("settings");
      if (phone.matches && panel && panel.matches(":popover-open")) lock();
      else unlock();
    });
    document.addEventListener("pointerdown", function (event) {
      if (!phone.matches || !event.isPrimary || event.button !== 0 || !(event.target instanceof Element)) return;
      var head = event.target.closest(".settingshead");
      if (!head || event.target.closest("button")) return;
      var panel = head.closest("#settings:popover-open");
      if (!panel) return;
      resetDrag();
      drag = { head: head, panel: panel, id: event.pointerId, start: event.clientY, distance: 0 };
      panel.classList.add("settings-dragging");
      head.setPointerCapture(event.pointerId);
    });
    document.addEventListener("pointermove", function (event) {
      if (!drag || event.pointerId !== drag.id) return;
      drag.distance = Math.max(0, event.clientY - drag.start);
      drag.panel.style.transform = "translateY(" + drag.distance + "px)";
    });
    document.addEventListener("pointerup", function (event) {
      if (!drag || event.pointerId !== drag.id) return;
      var panel = drag.panel;
      var distance = drag.distance;
      var dismiss = distance >= Math.min(120, panel.offsetHeight * 0.3);
      resetDrag();
      var reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (reduced || !panel.animate) {
        if (dismiss) panel.hidePopover();
        return;
      }
      var animation = panel.animate(
        [{ transform: "translateY(" + distance + "px)" }, { transform: dismiss ? "translateY(100%)" : "translateY(0)" }],
        { duration: 200, easing: "ease-out", fill: "both" },
      );
      releaseAnimation = animation;
      animation.onfinish = function () {
        if (releaseAnimation !== animation) return;
        if (dismiss && panel.matches(":popover-open")) panel.hidePopover();
        resetDrag();
      };
    });
    function cancelPointer(event) {
      if (drag && event.pointerId === drag.id) resetDrag();
    }
    document.addEventListener("pointercancel", cancelPointer);
    document.addEventListener("lostpointercapture", cancelPointer);
    addEventListener("pagehide", closeSettingsPopover);
  })();

  // ── page-level state that outlives a wirePage() pass ───────────────────
  // Everything in this block is registered ONCE at module init: it either
  // listens on window/document for something unrelated to the swapped DOM
  // or looks its elements up fresh when it fires.

  // Sidebar mode (>= 1040px): CSS forces the settings panel visible inside
  // .side while it is NOT :popover-open, so a popover opened at a narrower
  // width must be closed the moment the viewport widens, or it would sit in
  // the top layer on top of its own sidebar copy. The client never calls
  // showPopover() — opening is the declarative popovertarget's job.
  (function () {
    if (!window.matchMedia) return;
    var wide = matchMedia("(min-width: 1040px)");
    wide.addEventListener("change", function () {
      if (!wide.matches) return;
      var panel = document.getElementById("settings");
      if (!panel || typeof panel.hidePopover !== "function") return;
      if (!panel.closest(".side")) return;
      try {
        if (panel.matches(":popover-open")) panel.hidePopover();
      } catch (e) {}
    });
  })();

  // Print: every folded section must be on paper. Open whatever is closed
  // (remembering which), restore on afterprint. beforeprint can fire more
  // than once; only still-closed elements are recorded, so the second pass
  // is a no-op and afterprint restores exactly what the reader had.
  var printOpened = [];
  addEventListener("beforeprint", function () {
    closeSettingsPopover();
    var closed = document.querySelectorAll(
      "details.sec:not([open]), details.colophon:not([open]), details.arccontext:not([open])",
    );
    for (var i = 0; i < closed.length; i++) {
      closed[i].open = true;
      printOpened.push(closed[i]);
    }
  });
  addEventListener("afterprint", function () {
    for (var i = 0; i < printOpened.length; i++) printOpened[i].open = false;
    printOpened = [];
  });

  // Entrance choreography: the head script adds <html class="enter"> on a
  // hard load and the CSS staggers the rise-in while it is present. Drop it
  // once the animation has had time to finish, so later DOM changes cannot
  // replay it. This runs at module init only — never in wirePage() — and a
  // soft navigation can never bring the class back (the head script does not
  // re-run). A bfcache restore replays no head script either, so remove it
  // immediately there instead of re-running the entrance.
  setTimeout(function () {
    document.documentElement.classList.remove("enter");
  }, 1200);
  addEventListener("pageshow", function (e) {
    if (e.persisted) document.documentElement.classList.remove("enter");
  });

  // Typing state (phone only): while a text field has focus, <html
  // class="typing"> lets the CSS tuck the dock away so the on-screen keyboard
  // never pushes it over the field. Fields INSIDE the bar (the dock's own
  // search input) are excluded — hiding the dock would hide the field being
  // typed in. Derived from document.activeElement every time rather than
  // toggled per event, so a field removed by a soft-nav swap cannot leave the
  // class stuck. Registered once, on document.
  var isTextField = function (el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.closest && el.closest(".bar")) return false;
    if (el.isContentEditable) return true;
    if (el.tagName === "TEXTAREA") return true;
    if (el.tagName !== "INPUT") return false;
    var type = (el.getAttribute("type") || "text").toLowerCase();
    return (
      ["checkbox", "radio", "button", "submit", "reset", "image", "file", "range", "color", "hidden"].indexOf(type) === -1
    );
  };
  var phoneMq = window.matchMedia ? matchMedia("(max-width: 699.98px)") : null;
  function syncTyping() {
    var on = Boolean(phoneMq && phoneMq.matches && isTextField(document.activeElement));
    document.documentElement.classList.toggle("typing", on);
  }
  document.addEventListener("focusin", syncTyping);
  // focusout fires BEFORE the next element is focused; read activeElement
  // after the move has happened, so tabbing between two fields never flickers.
  document.addEventListener("focusout", function () {
    setTimeout(syncTyping, 0);
  });
  if (phoneMq) phoneMq.addEventListener("change", syncTyping);

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

    // A focused field the swap just removed would otherwise leave
    // <html class="typing"> behind (a removed element fires no focusout).
    syncTyping();

    // Phone dock auto-hide: the fixed bottom bar slides away while the
    // reader scrolls down and returns on any upward scroll (html.dockhid, see
    // the phone block in css.js). A soft nav starts with the dock shown.
    // Deltas accumulate in one direction so jitter never flips it; near the
    // top and near the bottom (end of an article) the dock is always shown, and
    // it never hides while something of the bar's own is in use.
    (function () {
      var root = document.documentElement;
      root.classList.remove("dockhid");
      var phone = window.matchMedia("(max-width: 699.98px)");
      var bar = document.querySelector("header.bar");
      var lastY = window.scrollY;
      var acc = 0;
      var queued = false;
      function show() {
        root.classList.remove("dockhid");
        acc = 0;
      }
      function frame() {
        queued = false;
        if (!phone.matches || root.classList.contains("settingslocked")) {
          // The fixed body temporarily sets window.scrollY to zero while
          // Settings is open. Leave the dock's scroll baseline alone.
          show();
          return;
        }
        // One layout read per frame.
        var y = window.scrollY;
        var nearBottom = document.documentElement.scrollHeight - (y + window.innerHeight) <= 120;
        var dy = y - lastY;
        lastY = y;
        var cmd = document.querySelector(".cmdpalette-backdrop");
        var pinned =
          document.querySelector("details.searchpop[open]") ||
          (bar && bar.contains(document.activeElement)) ||
          document.querySelector("#settings:popover-open") ||
          (cmd && !cmd.hidden);
        if (pinned || y <= 80 || nearBottom) {
          show();
          return;
        }
        if (dy > 0) {
          acc = acc > 0 ? acc + dy : dy;
          if (acc > 12) root.classList.add("dockhid");
        } else if (dy < 0) {
          acc = acc < 0 ? acc + dy : dy;
          if (acc < -8) show();
        }
      }
      subscribeScroll(function () {
        if (queued) return;
        queued = true;
        window.requestAnimationFrame(frame);
      }, signal);
      phone.addEventListener("change", show, { signal: signal });
      // Keyboard focus landing on a hidden dock must bring it back.
      if (bar) bar.addEventListener("focusin", show, { signal: signal });
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
    var THEME_COLORS = { light: "#eceff6", dark: "#0b0d14" };
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
        markTheming();
        setPref("theme", "theme", v, "auto");
        syncThemeColorMetas(v);
      },
    );

    // Text size miniseg: XS/S/M/L. M is the default expressed as the ABSENCE
    // of data-fontsize (see the CSS + setPref), so only xs/s/l touch storage.
    wireMiniseg(
      "size",
      signal,
      function () {
        var stored = document.documentElement.dataset.fontsize;
        return stored === "xs" || stored === "s" || stored === "l" ? stored : "m";
      },
      function (v) {
        setPref("fontsize", "fontsize", v, "m");
      },
    );

    // Site density miniseg (Cards / Compact): same shape as the theme and
    // size minisegs above. Cards is the default and is expressed as the
    // ABSENCE of data-density (see setPref); only "compact" touches storage.
    // The head script already applied any stored choice before first paint.
    wireMiniseg(
      "density",
      signal,
      function () {
        return document.documentElement.dataset.density === "compact" ? "compact" : "cards";
      },
      function (v) {
        setPref("density", "density", v === "compact" ? "compact" : "", "");
      },
    );

    // Folded digest sections (details.sec). Three pieces, all per-pass:
    //   1. An in-page link to a section (TOC chips, in-article #sN links)
    //      opens that section on click — synchronously, BEFORE the browser's
    //      default anchor jump, so the jump targets the final layout.
    //   2. hashchange (back/forward over a fragment, a hand-edited URL, a
    //      link the click handler never saw) does the same. A page that LOADS
    //      with #sN is handled once, at boot (see the end of this script),
    //      and a soft nav that lands on a hash in doSoftNav.
    //   3. The "expand all" button in the TOC: server-rendered hidden (no
    //      JS, no button), unhidden here only when there are sections to
    //      fold. One click toggles every section; its label and
    //      aria-expanded follow the ACTUAL state, so sections the reader
    //      opens or closes by hand keep it honest. "toggle" does not bubble,
    //      so it is caught in the capture phase on document.
    document.addEventListener(
      "click",
      function (e) {
        var a = e.target.closest ? e.target.closest('a[href^="#s"]') : null;
        if (!a) return;
        var target = hashTarget(a.getAttribute("href"));
        if (target) openSectionContaining(target);
      },
      { capture: true, signal: signal },
    );
    addEventListener(
      "hashchange",
      function () {
        revealHash(location.hash);
      },
      { signal: signal },
    );
    (function () {
      var btn = document.querySelector("button.expandall");
      if (!btn) return;
      var secs = function () {
        return Array.prototype.slice.call(document.querySelectorAll("details.sec"));
      };
      if (secs().length === 0) return;
      btn.hidden = false;
      var reflect = function () {
        var all = secs().every(function (d) {
          return d.open;
        });
        btn.textContent = all ? btn.dataset.collapse : btn.dataset.expand;
        btn.setAttribute("aria-expanded", String(all));
      };
      reflect();
      btn.addEventListener("click", function () {
        var open = !secs().every(function (d) {
          return d.open;
        });
        secs().forEach(function (d) {
          d.open = open;
        });
        reflect();
      }, { signal: signal });
      document.addEventListener(
        "toggle",
        function (e) {
          if (e.target && e.target.matches && e.target.matches("details.sec")) reflect();
        },
        { capture: true, signal: signal },
      );
    })();

    // Push notifications (PLAN.md §11.7, PR B). The row is server-rendered
    // HIDDEN — the whole row, not just its control, unlike every miniseg
    // above — because whether push is possible at all is a question only
    // the client can answer, and there are several distinct no's. This
    // decides which one applies and reveals the row only when it has
    // something true to say.
    (function () {
      var row = document.querySelector(".pushrow");
      if (!row) return;
      var btn = row.querySelector(".pushtoggle");
      var note = row.querySelector(".pushnote");
      var d = row.dataset;

      function say(text) {
        note.textContent = text;
        note.hidden = false;
        row.hidden = false;
      }

      // navigator.standalone is a Safari-only boolean, and it is a far
      // better signal than sniffing the user agent: its mere PRESENCE says
      // "this is iOS/iPadOS Safari", and its value says whether the page is
      // running from the Home Screen. That matters because iOS exposes
      // Notification and PushManager ONLY inside an installed web app — so
      // in Safari the capability check below would fail and the row would
      // stay silently hidden, when the honest thing to tell the reader is
      // the one action that would work.
      var iosSafari = typeof navigator.standalone === "boolean";
      var installed =
        navigator.standalone === true ||
        (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches);

      if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
        if (iosSafari && !installed) say(d.ioshint);
        return;
      }

      // Is push configured on the SERVER? A deployment without VAPID keys
      // answers 503 here, and the right response is to show nothing at all
      // rather than a control that cannot work. This is also what keeps the
      // feature dark between this PR merging and the keys being set.
      var aborted = function () {
        return signal.aborted;
      };
      fetch(d.base + "push/key", { signal: signal })
        .then(function (res) {
          if (!res.ok) throw new Error("push not configured");
          return res.json();
        })
        .then(function (cfg) {
          if (aborted()) return;
          if (Notification.permission === "denied") {
            // Nothing this page can do — the reader has to undo it in
            // browser settings, so say that rather than offering a button
            // whose only outcome is silence.
            say(d.blocked);
            return;
          }
          return navigator.serviceWorker.ready.then(function (reg) {
            return reg.pushManager.getSubscription().then(function (sub) {
              if (aborted()) return;
              wire(reg, cfg.key, sub);
            });
          });
        })
        .catch(function () {
          // Unconfigured, offline, or no worker — leave the row hidden.
        });

      function reflect(on) {
        btn.textContent = on ? d.enabled : d.enable;
        btn.setAttribute("aria-pressed", String(on));
        btn.title = on ? d.disable : d.enable;
        btn.hidden = false;
        note.hidden = true;
        row.hidden = false;
      }

      function wire(reg, key, sub) {
        reflect(Boolean(sub));
        btn.addEventListener("click", function () {
          // Notification.requestPermission() MUST be called synchronously
          // inside the gesture handler — iOS rejects it otherwise, and it
          // is the one call here that cannot be moved behind an await.
          var permission = sub ? Promise.resolve("granted") : Notification.requestPermission();
          btn.disabled = true;
          permission
            .then(function (result) {
              if (result !== "granted") throw new Error("permission " + result);
              return sub ? disable() : enable();
            })
            .then(function (next) {
              if (aborted()) return;
              sub = next;
              reflect(Boolean(sub));
            })
            .catch(function () {
              if (aborted()) return;
              // Distinguish the two failures the reader can act on: a hard
              // browser-level block is permanent until they change it, any
              // other error is worth retrying.
              if (Notification.permission === "denied") say(d.blocked);
              else say(d.failed);
            })
            .then(function () {
              btn.disabled = false;
            });
        }, { signal: signal });

        function enable() {
          return reg.pushManager
            .subscribe({ userVisibleOnly: true, applicationServerKey: decodeKey(key) })
            .then(function (created) {
              var body = created.toJSON();
              body.lang = d.lang;
              body.label = deviceLabel();
              return fetch(d.base + "push/subscribe", {
                method: "POST",
                headers: { "content-type": "application/json" },
                body: JSON.stringify(body),
              }).then(function (res) {
                if (!res.ok) {
                  // Do not leave a browser-side subscription the server
                  // does not know about: it would receive nothing and the
                  // toggle would lie about being on.
                  return created.unsubscribe().then(function () {
                    throw new Error("subscribe rejected");
                  });
                }
                return created;
              });
            });
        }

        function disable() {
          var endpoint = sub.endpoint;
          return sub.unsubscribe().then(function () {
            return fetch(d.base + "push/unsubscribe", {
              method: "POST",
              headers: { "content-type": "application/json" },
              body: JSON.stringify({ endpoint: endpoint }),
            }).then(function () {
              return null;
            });
          });
        }
      }

      // VAPID keys travel as base64url; the Push API wants raw bytes.
      function decodeKey(base64url) {
        var padded = base64url.replace(/-/g, "+").replace(/_/g, "/");
        while (padded.length % 4) padded += "=";
        var raw = atob(padded);
        var bytes = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
        return bytes;
      }

      // Coarse enough to be useful when the owner has three devices and one
      // stops working, coarse enough not to be a fingerprint. Stored only
      // in the owner's own D1, never shown to anyone else.
      function deviceLabel() {
        var ua = navigator.userAgent || "";
        if (/iPhone/.test(ua)) return "iPhone";
        if (/iPad/.test(ua)) return "iPad";
        if (/Android/.test(ua)) return "Android";
        if (/Macintosh/.test(ua)) return "Mac";
        if (/Windows/.test(ua)) return "Windows";
        return "Other";
      }
    })();

    // Search bubble (index pages only — guarded on details.searchpop
    // carrying a .filter input, since the other pages render a plain link
    // instead). It stays a native <details>/<summary> that CSS lays out as a
    // popover under the bar (upward from the dock on the phone), so opening
    // needs no JS; this IIFE adds the behaviours <details> lacks:
    //   - focus moves into the filter input the moment the panel opens (the
    //     native "toggle" event fires for both directions, so it checks
    //     pop.open rather than assuming one);
    //   - hover-to-open on a real hovering pointer, DESKTOP ONLY (below);
    //   - outside-click and Escape dismissal. Capture phase + preventDefault
    //     + stopPropagation is what swallows the dismissing click before the
    //     link or button under the pointer ever sees it — a bubble-phase
    //     listener would run after a link's navigation was already
    //     committed. Escape returns focus to the trigger so keyboard users
    //     are not stranded on a detached input.
    // The settings panel is NOT handled here: it is a native [popover]
    // (popovertarget), which light-dismisses and closes on Escape by itself.
    (function () {
      var pop = document.querySelector("details.searchpop");
      if (!pop) return;
      var input = pop.querySelector(".filter");
      if (!input) return;
      pop.addEventListener("toggle", function () {
        if (pop.open) input.focus();
        else pop.removeAttribute("data-hoveropen");
      }, { signal: signal });

      // Hover-to-open/close. Gated on a real hovering pointer: a touch
      // device synthesises mouseenter on tap, which would open the panel and
      // then leave it open with no pointer to move away. pointer: fine
      // excludes coarse styluses/remotes for the same reason. Two
      // cancellable timers: the open delay ignores a pointer merely
      // crossing the chip, the (short) close delay covers the seam between
      // the summary and the panel so the bubble does not blink.
      //
      // data-hoveropen marks a panel the reader never asked for: the outside
      // click and the soft-nav bail skip a panel wearing it, so a bubble the
      // pointer merely drifted across cannot eat the next click. It is
      // dropped when the panel closes or on a deliberate pointerdown on the
      // summary, after which the modal behaviour is right again.
      //
      // keepOpen is "a typed query exists": the input focuses itself on
      // open, so "focus is inside" alone would pin the panel open forever.
      if (window.matchMedia && matchMedia("(hover: hover) and (pointer: fine)").matches) {
        var openTimer = null;
        var closeTimer = null;
        var clearTimers = function () {
          if (openTimer) { clearTimeout(openTimer); openTimer = null; }
          if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
        };
        pop.addEventListener("mouseenter", function () {
          clearTimers();
          if (pop.open) return;
          openTimer = setTimeout(function () {
            pop.setAttribute("data-hoveropen", "");
            pop.open = true;
          }, 120);
        }, { signal: signal });
        var summaryEl = pop.querySelector("summary");
        if (summaryEl) {
          summaryEl.addEventListener("pointerdown", function () {
            pop.removeAttribute("data-hoveropen");
          }, { signal: signal });
        }
        pop.addEventListener("mouseleave", function () {
          clearTimers();
          if (!pop.open) return;
          closeTimer = setTimeout(function () {
            if (input.value !== "") return;
            // Blur first when focus is inside: details hides its content on
            // close, and focus parked on a hidden element strands the keyboard.
            if (pop.contains(document.activeElement)) document.activeElement.blur();
            pop.open = false;
          }, 140);
        }, { signal: signal });
      }

      document.addEventListener(
        "click",
        function (e) {
          if (pop.open && !pop.contains(e.target)) {
            // A hover-opened bubble still DISMISSES on an outside click, it
            // just does not swallow it.
            if (!pop.hasAttribute("data-hoveropen")) {
              e.preventDefault();
              e.stopPropagation();
            }
            pop.open = false;
          }
        },
        { capture: true, signal: signal },
      );
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
      // Document order = hero lead first (it sits OUTSIDE the ledger section,
      // above it), then the ledger's entries — newest first throughout.
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

      // entries are in document order = newest first (the hero lead first, see
      // the renderIndexPage/renderLeadCard comments), so entries[0] is the
      // newest digest in this view overall.
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
        // Where the fence goes: after the last new .entry in the ledger — or,
        // when that entry IS the hero lead (it lives above the ledger, not in
        // it), as the first child of the ledger section, which is exactly
        // "between the lead and everything older". A ledger with no entries
        // has nothing older to fence off, so no fence then.
        var fenceParent = null;
        var fenceBefore = null;
        if (lastNewIndex >= 0 && lastNewIndex < entries.length - 1) {
          var lastNewEntry = entries[lastNewIndex];
          if (lastNewEntry.closest(".hero")) {
            var ledger = section.classList.contains("ledger")
              ? section
              : document.querySelector("section.ledger");
            if (ledger && ledger.querySelector(".entry")) {
              fenceParent = ledger;
              fenceBefore = ledger.firstChild;
            }
          } else {
            // If that entry's next sibling happens to be a .dayhead, the
            // fence lands above the day header, which reads naturally.
            fenceParent = lastNewEntry.parentNode;
            fenceBefore = lastNewEntry.nextSibling;
          }
        }
        if (fenceParent) {
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

          fenceParent.insertBefore(fence, fenceBefore);

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
      // Digest/arc pages carry the same bar search bubble as the index
      // (the bar renders it on every page) but have no ledger to filter — bail
      // BEFORE the reveal, so their bubble shows only the archive search
      // link and this input never surfaces as a dead control.
      var section = document.querySelector("section[data-empty-filtered]");
      if (!section) return;
      input.hidden = false;
      // Every entry on the page, hero lead included, feeds the archive-result
      // dedupe below; only the LEDGER's entries are filterable. The hero is
      // the page's headline and lives outside the ledger section — the filter
      // must never hide it.
      var allEntries = Array.prototype.slice.call(document.querySelectorAll(".entry"));
      var entries = allEntries.filter(function (el) {
        return !el.closest(".hero");
      });
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
        // fence already in view.
        // Recompute every scroll-dependent UI (the resume chip) now that
        // entries were hidden/shown — by name, not by forging a scroll event.
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
                allEntries.map(function (el) {
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
    // page's primary link list in DOM order: the hero lead (an .entry that
    // sits above the ledger), then the ledger's .entry rows, then NOW's
    // .nowrow rows (the sidebar comes after the main column in the markup);
    // or an arc page's timeline .entry rows (no .nowrow there). Selection IS real focus (moveNavSelection's list[next].focus()),
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
        // The bar Archive link this used to read (.archivelink) was
        // removed in the index-cleanup pass — this lookup now always comes up
        // empty, so the "Archive" command simply never gets pushed, the same
        // graceful-disappearance behavior every other optional command here
        // already relies on (compare searchLink/langLink/latest just above and
        // below). Left in place rather than deleted: harmless dead code that
        // documents its own absence, and a future bar Archive link (if
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
      // unconditional-on-focus-target convention the search bubble's own
      // Escape handler above uses (unlike the OPEN trigger below,
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
  // On phones, changing the active view swaps immediately and animates only
  // the incoming layout so the fixed dock is never blended with its old state.
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
          var oldView = curWrap.querySelector(".viewtab.active[data-view]");
          var newView = newWrap.querySelector(".viewtab.active[data-view]");
          var phoneViewChange = Boolean(
            phoneMq && phoneMq.matches && oldView && newView &&
            oldView.getAttribute("data-view") !== newView.getAttribute("data-view")
          );
          // An open settings popover sits in the TOP LAYER: leave it open and
          // the swap would strand a detached node there (and its backdrop
          // would hang over the new page). Close it before the transition
          // snapshots anything.
          closeSettingsPopover();
          function swapPage() {
            // .wrap is the ENTIRE page body except the script tag and the
            // palette/resume-chip nodes (all live outside it) — see
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
            // data-fontsize and its state classes (enter/theming/typing).
            // Those are live preference/UI state, not page content — leaving
            // them alone is what makes the swap flicker-free, and it is why
            // a soft nav can never replay the entrance animation: the head
            // script that adds html.enter only runs on a hard load.
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
            var target = hashTarget(hash);
            // A folded digest section opens first, so the scroll below lands
            // on the final layout (the heading itself is always visible).
            if (target) openSectionContaining(target);
            if (target) target.scrollIntoView({ behavior: "instant", block: "start" });
            else window.scrollTo({ top: 0, left: 0, behavior: "instant" });
          }
          if (phoneViewChange) {
            // A tap during the startup animation must not replay it on the
            // replacement content. Position the new page before its motion.
            document.documentElement.classList.remove("enter");
            swapPage();
            var layout = curWrap.querySelector("main.layout");
            if (layout) layout.classList.add("view-switch-enter");
          } else withPageTransition(swapPage);
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
    // search-dismiss handler inside wirePage above. That handler also
    // listens for document clicks in the capture phase; because this
    // listener is registered first (module init, before the initial
    // wirePage() call at the bottom of this script) and re-registering it
    // inside wirePage never moves this one, this listener always runs
    // FIRST in capture order, on every pass. That ordering is what the
    // search-open bail below depends on. (The settings panel needs no bail:
    // a native popover light-dismisses itself.)
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
        // Dismiss-swallow interplay for the search bubble: with it open, a
        // click OUTSIDE it belongs to its dismiss handler behind this
        // listener — bail here with no preventDefault so the click reaches
        // it untouched. Clicks INSIDE the open panel soft-nav normally.
        //
        // This listener is registered at module init, BEFORE wirePage()
        // attaches the dismiss handler, and capture listeners on the same
        // node fire in registration order — so without this bail THIS one
        // wins and navigates before the dismiss handler can swallow
        // anything (clicking a headline to dismiss the popover opened the
        // headline; caught by driving a real browser).
        var openPanel = document.querySelector("details.searchpop[open]:not([data-hoveropen])");
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

  wirePage();
  // A hard load that arrives with a #sN hash: open the target's folded
  // section. Once per document, so it lives here and not in wirePage() (a
  // soft nav handles its own hash in doSoftNav).
  revealHash(location.hash);`;
