import { STRINGS } from "./strings.js";
import { brandFirst } from "./chrome.js";
import { indexHref } from "./hrefs.js";

// ── PWA shell: web app manifest + service worker (PLAN.md §11.7) ────────
//
// Two artifacts, both generated per-token and served from token-gated
// routes (see worker.js's dispatch). Neither is a page, so neither goes
// through pageChrome, esc(), or the golden renderer — but both are held to
// the same trust model as every page here, and the manifest is the reason
// the standing PWA deferral could be retired at all: it lives at
// /t/<token>/manifest.webmanifest, behind the same tokenMatches gate and
// the same indistinguishable notFound() as everything else, so nothing
// about this feature is fetched tokenless. (Icons are the deliberate
// exception — see the note above buildManifest.)
//
// THE GUARDRAIL, in one line: the service worker below uses no Cache API.
// Not "caches carefully" — none, anywhere in this file. That is the whole
// reason PWA stopped being deferred (PLAN.md §11.7, "Retiring the standing
// deferral"): the token reaching disk is an exposure class bookmarks and
// history already carry, but private digest CONTENT reaching disk is not,
// and Cache-Control: private, no-store has to stay honest. What ships is
// installable + push, explicitly NOT an offline reader. A future change
// that adds a cache here is not an increment on this feature; it is the
// deferred feature, and it needs its own privacy think first.

// The manifest's icons, and the <link rel="apple-touch-icon"> in pageChrome,
// point at TOKENLESS root paths (/icon-512.png etc, same as /favicon.svg).
// Deliberate, and it cuts both ways in this trust model's favor: an icon
// reveals nothing about the site's content (robots.txt already concedes
// something is served here), and keeping them tokenless means the installed
// app record on the device carries the capability token in exactly ONE
// place — start_url/scope/id — instead of in four more icon URLs beside it.

export function buildManifest(host, token, lang) {
  // Uppercased to match what a reader actually SEES. The masthead wordmark
  // is `brandFirst(host)` rendered through `.brand { text-transform:
  // uppercase }` (src/css.js), so the host label alone ("news") is the
  // brand's source, not its appearance — and a Home Screen label sits
  // directly under an icon carrying that wordmark, where the two
  // disagreeing is conspicuous.
  const name = brandFirst(host).toUpperCase();
  return JSON.stringify(
    {
      // Fixed to the EN root in BOTH language variants, deliberately: `id`
      // is what the browser uses to decide "is this the same installed
      // app". Letting it follow `lang` would make EN and HU two separate
      // Home Screen apps over one archive, each with half the history. One
      // app, whose start_url follows whichever language the reader
      // installed from, and whose in-app EN|HU switcher keeps working
      // because `scope` covers both.
      id: indexHref(token, "en", "all"),
      name,
      short_name: name,
      start_url: indexHref(token, lang, "all"),
      scope: indexHref(token, "en", "all"),
      display: "standalone",
      lang,
      dir: "ltr",
      // Splash-screen ground and (where the OS uses it) title-bar tint.
      // Dark rather than the light palette's #ffffff: an installed app
      // opens to a splash before any CSS runs, and a dark ground is the
      // gentler miss for a reader who turns out to be in light mode than a
      // white flash is for one in dark. The page's own theme-color metas
      // (pageChrome) still win the moment the document paints, so this
      // value only ever governs that pre-paint instant. One-line change if
      // the owner disagrees.
      background_color: "#131418",
      theme_color: "#131418",
      icons: [
        { src: "/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
        {
          src: "/icon-maskable-512.png",
          sizes: "512x512",
          type: "image/png",
          purpose: "maskable",
        },
      ],
    },
    null,
    2,
  );
}

export function buildServiceWorker(token) {
  const config = {
    scope: indexHref(token, "en", "all"),
    icon: "/icon-512.png",
    offline: {
      en: {
        title: STRINGS.en.offlineTitle,
        body: STRINGS.en.offlineBody,
        retry: STRINGS.en.offlineRetry,
      },
      hu: {
        title: STRINGS.hu.offlineTitle,
        body: STRINGS.hu.offlineBody,
        retry: STRINGS.hu.offlineRetry,
      },
    },
    // The generic notification shown when push/latest is unreachable. EN
    // only, and that is not an EN/HU parity gap so much as an honest one:
    // this string fires exactly when the worker could not reach the site,
    // which is the one moment it has no way to learn which language the
    // reader uses. A service worker has no page context, and its scope
    // (/t/<token>/) carries no language segment. From PR B onward the
    // NORMAL path is fully localized without needing any of this, because
    // push/latest returns title/body already rendered in the reader's
    // language and this worker is a dumb renderer of them.
    fallback: { title: STRINGS.en.pushFallbackTitle, body: STRINGS.en.pushFallbackBody },
  };
  return `const CFG = ${JSON.stringify(config)};\n\n${SERVICE_WORKER_BODY}`;
}

// Plain ES5-flavored source, matching src/client.js's own style (var,
// function expressions) — this ships to the same browsers that file does,
// and there is no bundler or transpiler between here and them.
//
// Written as a constant with NO template interpolation of its own: every
// value it needs arrives through the CFG object buildServiceWorker prepends
// above. That is what keeps this readable as service-worker source instead
// of as an escaping puzzle, and it is why neither `${` NOR A BACKTICK ever
// appears below — including inside a comment, where one silently ends this
// template literal and turns the rest of the worker into stray JS.
const SERVICE_WORKER_BODY = `self.addEventListener("install", function () {
  // Take over immediately rather than waiting for every tab to close: this
  // worker holds no cached state that a half-updated client could disagree
  // with, so the usual reason to wait does not apply.
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

// Navigation-only, network-only, and there is no cache read or write in
// this handler by design (see the file-header guardrail in src/pwa.js).
// Its ONLY job is to answer a navigation the network could not, so the
// installed app shows something owned by this site instead of the browser's
// dinosaur. Subresource requests are left entirely alone — returning early
// means the browser handles them exactly as it would with no worker
// installed.
self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.mode !== "navigate") return;
  event.respondWith(
    fetch(request).catch(function () {
      return offlineResponse(request.url);
    }),
  );
});

function offlineResponse(url) {
  // The one piece of language the worker CAN resolve on its own: a /hu/
  // page's own URL says so. Nothing is stored to learn this.
  var hu = url.indexOf("/hu/") !== -1;
  var s = hu ? CFG.offline.hu : CFG.offline.en;
  var html =
    '<!doctype html><html lang="' +
    (hu ? "hu" : "en") +
    '"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<meta name="theme-color" content="#131418"><title>' +
    s.title +
    "</title><style>" +
    "html{color-scheme:light dark}" +
    "body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;" +
    "background:#fff;color:#131418;font:16px/1.5 ui-sans-serif,system-ui,sans-serif;" +
    "padding:2rem;text-align:center}" +
    "@media(prefers-color-scheme:dark){body{background:#131418;color:#e9e9ee}}" +
    "h1{font-size:1.25rem;margin:0 0 .5rem}p{margin:0 0 1.5rem;opacity:.75}" +
    "a{color:#4f46e5;font-weight:600;text-decoration:none}" +
    "@media(prefers-color-scheme:dark){a{color:#a5b4fc}}" +
    "</style></head><body><div><h1>" +
    s.title +
    "</h1><p>" +
    s.body +
    '</p><a href="' +
    CFG.scope +
    '">' +
    s.retry +
    "</a></div></body></html>";
  // 503, not 200: this is a real "the site could not be reached", and a
  // navigation that lies with 200 teaches the browser the wrong thing.
  // Same no-store/no-referrer posture as every response the site itself
  // serves — a locally generated page is still a page about private
  // content's absence.
  return new Response(html, {
    status: 503,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "private, no-store",
      "referrer-policy": "no-referrer",
    },
  });
}

// Payload-less push (PLAN.md §11.7, "Why the push carries no payload"): the
// message itself is empty, and everything shown is fetched fresh from the
// site. self.registration.scope IS the capability-token root, so this
// worker addresses the site without the token ever being stored here or
// travelling inside a push message.
//
// As of PR B this handler is fully wired — push/latest exists and devices
// can subscribe — but nothing SENDS a push until PR C adds the VAPID
// signer and the fan-out from handleIngest. So it still cannot fire in
// production today; what changed is that it now would work if it did.
self.addEventListener("push", function (event) {
  event.waitUntil(showLatest());
});

function showLatest() {
  // Identify by our own push endpoint. It is the one fact a service worker
  // knows about itself that the site also knows, and the site uses it for
  // exactly one purpose: to look up which LANGUAGE this device subscribed
  // in. A worker has no page context and its scope carries no language
  // segment, so without this every notification would be English.
  // POST, not a query string: the endpoint is a capability (whoever holds
  // it can notify this device), and URLs land in Workers Observability.
  return self.registration.pushManager
    .getSubscription()
    .catch(function () {
      return null;
    })
    .then(function (sub) {
      return fetch(self.registration.scope + "push/latest", {
        method: "POST",
        cache: "no-store",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ endpoint: sub ? sub.endpoint : null }),
      });
    })
    .then(function (response) {
      if (!response.ok) throw new Error("push/latest " + response.status);
      return response.json();
    })
    .then(function (data) {
      // "empty" means the token was valid but there are no digests yet — a
      // real state on a fresh deployment, not a failure. The generic copy
      // is the right thing to show, so route it through the same fallback.
      if (!data || data.empty) throw new Error("no digest");
      return show(data.title, data.body, data.url, data.urgent);
    })
    .catch(function () {
      // MUST still show something. A push event whose waitUntil settles
      // without a notification makes the browser show its own "site was
      // updated in the background" notice, and repeated silent pushes are
      // grounds for revoking the permission outright.
      return show(CFG.fallback.title, CFG.fallback.body, self.registration.scope, false);
    });
}

function show(title, body, url, urgent) {
  return self.registration.showNotification(title, {
    body: body,
    icon: CFG.icon,
    // One live notification, always the newest. A fixed tag makes a second
    // push REPLACE the first rather than stack beside it, which matters on
    // the Sunday 20:00/20:30/21:00 cascade (three digests inside an hour)
    // and matters more because the push carries no payload: every stacked
    // copy would fetch and display the same newest brief anyway. renotify
    // re-alerts on the replacement so a newer digest is not silent.
    tag: "digest",
    renotify: true,
    requireInteraction: Boolean(urgent),
    data: { url: url || self.registration.scope },
  });
}

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var target = (event.notification.data && event.notification.data.url) || CFG.scope;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
      // Reuse an already-open window when there is one — opening a second
      // copy of a single-page archive is never what the tap meant.
      for (var i = 0; i < list.length; i++) {
        if (list[i].url.indexOf(CFG.scope) !== -1 && "focus" in list[i]) {
          return list[i].navigate ? list[i].navigate(target).then(focusOf(list[i])) : list[i].focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});

function focusOf(client) {
  return function (navigated) {
    return (navigated || client).focus();
  };
}
`;
