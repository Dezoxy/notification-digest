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
- **`golden.mjs`** asserts *bytes* — the seven page types, rendered and
  compared against `golden/*.html`.

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
| `stub-db.mjs` | Stands in for the D1 binding, dispatching on the SQL the handlers issue. |
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

**The stub throws on unknown SQL.** If a handler starts issuing a query the
stub does not recognise, it raises instead of returning an empty result —
otherwise a query change would quietly render an emptier page and the golden
would be regenerated with the loss baked in.

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
