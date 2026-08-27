# Tests

No dependencies, no framework — `node --test` and a byte-comparison script.

```bash
cd workers/news-site
npm test          # invariants + golden byte-check + prettier check
npm run golden    # REGENERATE the golden pages (only when output should change)
npm run format    # prettier --write
```

## The contract

Two halves, and the split matters:

- **`render.test.mjs`** asserts *semantics* — the things that must stay true
  no matter how the code is arranged (trust headers, EN/HU parity, `#sN`
  numbering, one marker per fixture-covered render branch).
- **`golden.mjs`** asserts *bytes* — every page type in `GOLDEN_PAGES`
  (`env.mjs`), rendered and compared against `golden/*.html`. Nine as of the
  2026-08-27 two-column pass, which added the daily and weekly index views;
  they had no coverage before it, so changes to how they render produced a
  zero golden diff.

Which one you lean on depends on the change you are making:

| Change | Expectation |
| --- | --- |
| Refactor that must not alter output (hoists, dedupe, module moves, CSS reordering) | `--check` passes with **zero diff**. That is the proof, not a formality. |
| Change that *should* alter output (a new element, a removed rule) | Run `npm run golden`; the resulting diff **is** the review artifact. Read it line by line and confirm it contains only what you intended. |

A golden diff you cannot fully explain means you changed something you did
not mean to.

## Files

| File | Role |
| --- | --- |
| `env.mjs` | Freezes the clock and shims `crypto.subtle.timingSafeEqual`; exports `fetchPath`/`makeEnv`/`loadWorker` and the golden page list. |
| `stub-db.mjs` | Stands in for the D1 binding, dispatching on the SQL the handlers issue. Read-only over fixtures, except the push tables (see below). |
| `fixtures.mjs` | The dataset. Its header lists every render branch it keeps alive. |
| `fixtures.sql` | The same rows for a real local D1 (browser-based checks). |
| `render.test.mjs` | The invariant suite. |
| `golden.mjs` | Golden renderer/checker (`--update` / `--check` / `--bundle`). |
| `golden/*.html` | Committed expected output (~1.2MB). |
| `tools/css-matrix.html` | Computed-style regression matrix for CSS refactors. |

### Two things that are load-bearing, not incidental

**The frozen clock.** The NOW section, the current-week resolution and the
arc-momentum labels all read the real clock. Without a fixed instant the
goldens would differ between runs and the whole byte-comparison contract
collapses. `env.mjs` replaces `Date` with a subclass whose zero-argument
constructor and `Date.now()` return one instant; explicit construction
(parsing fixture timestamps) passes through untouched.

**The `ctx` stub awaits its `waitUntil` promises.** `handleIngest` runs the
push fan-out inside `ctx.waitUntil`, so a stub that merely *accepted* the
promise and dropped it would let every claim-once, newest-only and
prune-on-410 test pass while asserting against work that had not happened yet
— green, and testing nothing. `fetchWithCtx` collects and awaits them before
handing back the response, so tests read the resulting database state directly
with no polling and no sleeps. Same class of trap as the throw-on-unknown-SQL
rule below.

**The push arms are the one piece of mutable state.** Every other arm reads
fixtures. `push_subscriptions` cannot: subscribe/unsubscribe are the first
handlers whose entire behavior *is* what the table contains afterwards —
idempotent re-subscribe, the cap that must never block a device refreshing its
own row, delete-then-look-up. Tests pass their own array in via
`makeEnv({ pushSubs })` and assert against it directly.

**The stub throws on unknown SQL.** If a handler starts issuing a query the
stub does not recognise, it raises instead of returning an empty result —
otherwise a query change would quietly render an emptier page and the golden
would be regenerated with the loss baked in.

## Goldens are environment-coupled — the ICU trap

Byte-comparison assumes the same input renders the same bytes anywhere. That
is true of this Worker's own code, and NOT automatically true of anything it
delegates to `Intl`.

`Intl` output comes from ICU, and different ICU builds disagree. Three are in
play for this project: the machine that runs `npm run golden`, the Linux
image Workers Builds verifies in, and **workerd**, which is what readers
actually see. They are not the same version and do not update together.

This has already bitten once. `Intl.DateTimeFormat.formatRange` renders the
week-rail label as `24-30 Aug` on ICU 77 and `24 - 30 Aug` (spaces around the
dash) on newer builds. Goldens generated on a laptop, verified in CI, failed
the first git-connected build on nothing but a toolchain difference — and the
same drift was silently deciding what the live site displayed.

The fix was not to regenerate the goldens on the right machine, which only
moves the problem: it was to stop letting an ICU version choose user-visible
text. `formatWeekRangeLabel` now normalizes the separator spacing, keeping
ICU's locale intelligence (field order, month abbreviation, which parts
collapse) and pinning the one thing that drifts.

**If a golden fails only in CI and the diff looks like punctuation,
whitespace, or a separator — suspect ICU before suspecting the code.** The
remedy is to make the string deterministic in `src/`, not to loosen the
comparison.

## Verification recipes

### Byte-check a bundled artifact, not just the module graph

Node resolving `src/*.js` is not proof that *wrangler's bundle* behaves the
same. For anything structural (module moves, import changes), check both:

```bash
npx wrangler deploy --dry-run --outdir dist
node test/golden.mjs --check --bundle dist/worker.js
```

### CSS refactors: the computed-style matrix

Golden bytes catch a changed stylesheet, but they cannot tell you whether a
*reordered* stylesheet still computes the same. `tools/css-matrix.html`
loads before/after pages in paired iframes and diffs `getComputedStyle` for
every element and its `::before`/`::after` across 375/700/1280px × light/dark
× masthead shown/hidden.

```bash
mkdir -p /tmp/m/before /tmp/m/after
cp test/golden/*.html /tmp/m/before/          # before = current goldens
# …make the CSS change, then regenerate into /tmp/m/after/
cp test/tools/css-matrix.html /tmp/m/
cd /tmp/m && python3 -m http.server 8791
# open http://localhost:8791/css-matrix.html, then per page in the console:
#   await runPage("index-en")   -> { comparisons, diffCount, diffs }
```

Zero diffs means the change is computationally inert for everything covered.
It does **not** cover `:hover`, `:focus`, or print — check those by hand, or
confirm (as the phone-block consolidation did) that the moved ranges contain
no such selectors.

### Service workers: the browser pane cannot test them

`mcp__Claude_Browser__*` (the in-app browser pane) **blocks service-worker
registration outright**. `navigator.serviceWorker.register()` rejects there
with `An unknown error occurred when fetching the script` — not because of
anything in this Worker's headers or MIME types, but because the environment
does not permit workers at all. A one-line control worker served from a plain
`python3 -m http.server` fails there identically; that is the cheapest way to
confirm you are looking at the environment and not at a bug.

Use real headless Chrome over CDP instead. Node 22+ has a global `WebSocket`,
so no dependency is needed:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-sw \
  --no-first-run --disable-gpu about:blank &
# then: PUT http://127.0.0.1:9222/json/new?<url> for a target, connect to its
# webSocketDebuggerUrl, and drive Page.enable / Runtime.evaluate.
```

Two traps that cost real time here:

- **`Network.emulateNetworkConditions` does not reach the service worker.**
  CDP network emulation is per-target and a worker is its own target, so the
  page goes offline while the worker's own `fetch()` still has live network —
  an offline-fallback test run that way *passes without testing anything*.
  Kill the dev server instead. That is the only honest version of the test.
- **`Browser.grantPermissions`** for `notifications` up front, or a click
  handler calling `Notification.requestPermission()` blocks on a prompt
  nothing can answer.

Verifying a real push subscription end to end is not possible locally — it
needs a live push service. Stub `reg.pushManager.subscribe` in the page to
return a fixed fake subscription: that still exercises everything this repo
owns (the click path, the POST shape, the server storing it), and only the
browser's own registration is faked.

### Driving the real router in a browser

`wrangler dev` may refuse to start when the local `workerd` is older than
`compatibility_date` in `wrangler.jsonc`. A small Node server over the same
test env works and exercises the real routes, so soft navigation behaves:

```js
// /tmp/devserve.mjs
import { createServer } from "node:http";
import { loadWorker, makeEnv } from "<abs path>/workers/news-site/test/env.mjs";
const worker = await loadWorker();
createServer(async (req, res) => {
  const r = await worker.fetch(
    new Request("https://news.toomhorvath.com" + req.url, { method: req.method }),
    makeEnv(),
  );
  res.writeHead(r.status, Object.fromEntries(r.headers));
  res.end(Buffer.from(await r.arrayBuffer()));
}).listen(8791);
```

Then browse `http://localhost:8791/t/goldentesttoken/`.

When touching `src/client.js`, re-run the whole feature list **after two
consecutive soft navigations** — the `wirePage()` teardown/rebind cycle is
where that file breaks. A cheap way to prove subscribers are not
accumulating across rewires: a `MutationObserver` counting `masthid` class
flips over one scroll-down/scroll-up cycle should see exactly **2**.

## Adding a fixture

Add the row to `fixtures.mjs`, mirror it into `fixtures.sql`, add a marker
assertion in `render.test.mjs` for whatever branch it exists to cover, then
`npm run golden`. The marker is the point: without it, a later edit can stop
exercising the branch and nothing fails.
