# PLAN2 — proposals not yet accepted

Ideas that have been thought through but NOT approved for implementation.
Nothing here is scheduled; PLAN.md remains the record of the system as built
and the work actually in flight. Each entry states the idea, what it would
cost, which existing design decisions it collides with, and how it would
preserve them. Delete an entry when it ships (moving its substance into
PLAN.md) or when it's rejected (leaving a line in §9's decision log saying
why).

## 1. Verified briefing — cross-reference the daily brief against the open web

**Status:** proposed 2026-08-09, discussion pending. Origin: the same friend
whose "criteria, not names" advice produced the geopolitics prompt rule.

**The suggestion, in his words (paraphrased from Hungarian):** give the daily
brief a framework where each assembled story is checked individually against
the web; where the original source left something out, fill the gap; and add
a safeguard so no bullshit gets through.

**What it would change.** Today the daily brief is a synthesis of what the
owner's own sources said — nothing verifies whether those sources were right,
complete, or alone in saying it. The proposal turns the brief from "what my
sources reported" into "what my sources reported, checked against the world":
per story, a corroboration status; where a source omitted something material,
a cited gap-fill; where accounts conflict, a visible correction.

### The two walls it crosses, and how they stay standing

**Wall 1 — the summarizer has no tools.** PLAN.md §8's prompt-injection row
rests on it: "the summarizer has no tools and no ability to act", so hostile
scraped text has a tiny blast radius. Web verification means the model reads
arbitrary pages — a genuinely new injection surface. The mitigation is
containment, not abstention: a SEPARATE verifier pass with one narrow job,
never tools bolted onto `summarize()`. The window digests (8 runs/day, 50–100
items each) stay toolless and fast — verification belongs only on the daily
brief, which is where the stories that mattered already are.

**Wall 2 — the citation allowlist.** `enforce_link_allowlist` plus
`emailer.py`'s `_enforce_anchor_provenance` guarantee that every live link is
a collected item's URL, verbatim — a hallucinated link can never reach the
reader. Verification needs NEW external citations, which naively means
weakening exactly the invariant this system is proudest of. It doesn't have
to: widen the allowlist ONLY with URLs the verifier actually visited,
extracted deterministically from the CLI's own tool-use transcript
(`--output-format json`), never from model prose. The invariant becomes:
*every live link is either a collected item or a page this pipeline itself
fetched* — same strength, provable the same way, and the two enforcement
layers stay untouched.

### Shape

Two calls inside `run_daily`, in place of today's one:

1. **Draft** — `summarize_daily` exactly as today: no tools, contract
   unchanged, output unchanged.
2. **Verify + enrich** — a second `claude -p` call WITH WebSearch/WebFetch,
   given the draft and a new `prompts/verify-daily.md`:
   - per story, search for independent coverage and classify: corroborated
     (with sources), single-source, or disputed;
   - **mark, never censor** — a disputed claim gets a visible correction
     ("X reports otherwise"), never a silent deletion, matching this
     codebase's every-degradation-is-loud rule;
   - gap-fills only when backed by a fetched page, cited, and inside the
     existing length discipline;
   - a short "Verification notes" closing section carrying per-story status;
   - the strongest injection-resistance preamble in the repo — this is the
     only prompt that ever ingests the open web.
3. **Deterministic post-processing** — visited URLs are parsed out of the
   transcript, unioned into the allowlist, and the EXISTING provenance layers
   run over the result unchanged.

**Failure semantics:** verification soft-fails like `translate_digest`. If
tools flake or the call times out, the brief ships exactly as today with a
code-prepended `⚠ verification unavailable this run` banner (never
model-dependent — same rule as the collector banners, for the same reason).
A verified brief is better; an unverified brief on time beats no brief.

**Config (flag-first, default off):** `VERIFY_DAILY_ENABLED`, a search/fetch
cap, its own timeout, model/effort. Validate against real briefs before
adopting, exactly as the BRIEFING contract was.

### What the safeguard does and doesn't catch

- **Link layer** — already solved; hallucinated URLs are impossible today and
  stay impossible under the transcript-derived widening.
- **Claim layer** — new: catches source error, omission, and single-source
  shakiness. "Three independent outlets vs. one Telegram group's hot take" is
  exactly the signal a briefing should carry.
- **Injection layer** — new risk, contained: hardened prompt, constrained
  output shape, and a blast radius that ends at distorted text in a briefing
  the owner reads himself. It will NOT catch a claim the whole web repeats
  wrongly; no verifier does.

### Cost

One extra Opus-class call per day plus a bounded handful of web operations —
negligible under the Max subscription, and knob-governed. Wall-clock on the
daily run roughly doubles, which nothing depends on (the daily timer is
independent of the 3-hourly one).

### Deliberately out of scope

Per-item verification of window digests ("check each one on the net") at the
3-hourly cadence: 8 web passes/day over 50–100 items each is where cost,
latency, and injection surface all explode for marginal gain. The daily brief
already contains the stories that mattered — verify there.

### If approved, the execution shape

1. PLAN.md §11 roadmap: this design, both walls and their preservation, and a
   new §8 risk row for the injection surface.
2. Transcript plumbing: a `run_claude` variant with tool enablement + JSON
   transcript URL extraction (pure-function testable; existing call sites stay
   byte-identical).
3. The verifier step: `prompts/verify-daily.md` + `run_daily` wiring
   (draft → verify → translate → deliver), widened-allowlist threading, the
   soft-fail banner.
4. Flag on, live validation against real briefs, owner (and friend) judges the
   output, then default on.
5. Release train: tag → homelab bump (new env vars in the role) → deploy. The
   site needs nothing initially — verification notes are just markdown; a later
   polish PR could render per-section status chips.

### Adjustments (2026-08-10 review)

1. **Transcript-extraction contract.** `claude -p --output-format json`'s
   transcript shape is not a stable, versioned API — the plumbing in step 2
   above must pin against it defensively and tolerate drift, never assume
   the shape holds across CLI versions. A mis-parse must fail toward the
   existing safety, not away from it: it soft-fails to the `⚠ verification
   unavailable this run` banner, and must never silently ship an unlabeled
   unverified brief dressed up as a verified one. Separately, redirects and
   URL variants: a fetched page's requested URL and its final URL (after a
   redirect) can differ, and Wall 2's allowlist match is verbatim — decide
   explicitly whether the requested URL, the final URL, or both enter the
   widened allowlist, and write down the normalization rule (scheme,
   trailing slash, tracking params) before this ships, or the existing
   provenance layers will silently strip the verifier's own citations for
   failing to match byte-for-byte.

2. **Label epistemics.** Syndication makes one wire story look like many
   independent sources — a dozen outlets running the same AP feed is not a
   dozen origins. `prompts/verify-daily.md` must ask the model to identify
   distinct reporting origins where discernible, not just count URLs it
   found, and the label vocabulary must not overclaim what it actually
   found: "independently reported (N origins)" and "widely repeated" are
   different claims about different things, and "corroborated" is reserved
   for the former — never used loosely for the latter.

3. **Translation pass.** The pipeline becomes draft → verify → translate →
   deliver, so `prompts/translate-hu.md` ingests web-derived text for the
   first time — not just the summarizer's own output. Confirm the
   translation prompt's existing data-not-instructions framing (it already
   treats the briefing strictly as data to translate, never as commands to
   obey) extends cleanly to text whose ultimate origin is now the open web,
   not only scraped Telegram/X. (One line: it does — the framing is about
   the briefing markdown as a whole, not about where its sentences
   originated, so no prompt change is needed here, only confirmation.)

The Codex site-redesign brief (2026-08-10, see §2) proposes per-story
confidence badges on the site. Those badges are strictly a rendering of THIS
entry's output — the per-story corroboration status above — and must not be
built independently of it; there is no confidence signal to badge on the
site until this entry ships.

## 2. Storyline-first site IA — NOW homepage and arc pages

**Status:** proposed 2026-08-10, discussion pending. Origin: Codex redesign
brief for news.tomhorvath.me, triaged 2026-08-10.

**What it would change.** The site's primary browsing unit stops being the
chronological brief and becomes the story arc — the same story-arc topics
`digest/publish.py` has derived and published since PR #56, promoted from a
secondary label to the front door. The homepage becomes "NOW": the 3–5 arcs
ranked by recency × update volume. Each arc gets a page: current state, a
timeline of its appearances across briefs, and deep links into the briefs
that carried each update. The publish payload already carries what this
needs: per-digest `{slug, label}` topic pairs, which the site already
matches across digests (up to 7 days apart, toom-edge PR #107) to detect
recurrence — an arc page extends that same slug-matching over the full
archive instead of a 7-day window. Site-side reconstruction; no new digest
field. Chronological briefs demote
to an Archive section, still reachable, no longer the front page. A
client-side ⌘K command palette is polish within this entry's scope, not a
project of its own.

### Collisions with existing design decisions, and how they stay standing

1. **Arc slug-stability contract.** `derive_topics`' docstring is explicit:
   a slug must fold the same way on every run, forever, because the site
   keys its story-arc recurrence tracking on `slug` matching across digests.
   Arc pages make that slug a load-bearing PUBLIC URL, not just an internal
   join key — this STRENGTHENS the existing contract, it does not relax it.
   It must not regress: no change to `_slugify` or the fold rules ships
   alongside this entry without re-verifying stability against real
   published slugs first.

2. **Anchor-numbering coupling.** Digest HTML output and site anchors are
   already coupled (the site's section numbering mirrors `_real_heading_lines`
   and the needs-attention exclusion). Arc pages deep-link into specific
   brief sections, which turns that coupling into a PUBLIC contract for the
   first time — a later change to the digest's heading/anchor scheme would
   silently break every arc timeline's deep links, not just cosmetically
   shift a number on the page. Any future anchor-scheme change must treat
   arc timelines as a consumer to check, the same way `enforce_link_allowlist`
   treats every citation as a consumer to check.

3. **Briefs stay the source of truth; arcs stay derived.** This entry must
   not invert the architecture into storyline-first STORAGE — arcs remain a
   view computed from published briefs, never a place new state is written
   first. That inversion was considered and rejected: it is a rewrite of the
   publish pipeline disguised as a front-end feature, and nothing about the
   Codex brief requires it. If arc pages ever need data the current publish
   payload doesn't carry, the fix is exposing more of what `derive_topics`
   or the digest already knows — not building a second, arc-shaped source of
   truth beside it.

### Cost

Almost entirely toom-edge site work — layout, ranking, arc-page templates,
archive demotion. Zero new model calls. On the digest side, at most exposing
ranking signals the pipeline already has (timestamps, appearance counts) to
the existing publish payload; no new pipeline stage.

## 3. Client-side catch-up — since your last visit

**Status:** proposed 2026-08-10, discussion pending. Origin: Codex redesign
brief for news.tomhorvath.me, triaged 2026-08-10.

**What it would change.** Store a last-visit timestamp in `localStorage`; on
load, diff it against published brief/arc timestamps and show "since your
last visit: N briefs, M arc updates" with links straight to what's new. No
accounts, no server-side state, no new privacy surface — the timestamp never
leaves the reader's own browser.

### Collisions with existing design decisions, and how they stay standing

None on the digest side — this is purely site-side, reading data the publish
payload already exposes (brief and topic timestamps). It can ship
independently of §2: without arc pages the catch-up banner just links to
briefs; with §2 it can also link into arc pages. Only the placement of its
call-to-action changes depending on whether §2 has shipped, not its logic.

### Cost

A small amount of client-side JavaScript. No backend, no schema, no new
publish fields beyond what already exists.

## 4. Delta persistence — "what changed" as data, not prose

**Status:** proposed 2026-08-10, discussion pending. Origin: Codex redesign
brief for news.tomhorvath.me, triaged 2026-08-10.

**What it would change.** The delta-only reasoning lives in the WINDOW
digest, not the daily: `prompts/digest.md` instructs each 3-hourly run to
write repeat stories as deltas against `{{RECENT_COVERAGE}}`, the sanitized
block of the last 24h of prior digests' headings that `digest/summarize.py`
renders into the prompt; the daily brief then synthesizes arcs from the
day's briefings (`{{BRIEFINGS}}`) and never sees RECENT_COVERAGE at all. So
"what changed" is already computed eight times a day at the window level —
and survives only as prose that evaporates once the digest is archived.
This entry has the window digest emit that delta as structured data
alongside its prose — per arc: previously / now / changed-at, keyed to the
same slug `derive_topics` already derives for the section — stored in
SQLite, published to the site, where both a "What changed" UI and §2's arc
timelines consume it as data instead of re-deriving it from prose. This is
the single most valuable backend change in the Codex brief: the bridge
between the existing digest pipeline and the storyline UI the rest of that
brief wants to build.

### Collisions with existing design decisions, and how they stay standing

1. **RECENT_COVERAGE is a fenced replay channel — this must not become a
   second one.** `{{RECENT_COVERAGE}}` is deliberately fenced —
   `digest/summarize.py`'s renderer feeds the prompt sanitized headings
   only, line- and length-capped, at a fixed template position — because it
   exists to let the window prompt write delta-only updates, not to let old
   coverage silently replay forward through later syntheses. The structured
   delta this entry emits derives from that same fenced input, and it must
   NOT become an unfenced second replay channel into whatever reads it
   next. In particular, the daily and weekly briefs — which already
   synthesize briefings — must not re-ingest structured deltas unless they
   are fenced identically to how `RECENT_COVERAGE` is fenced today. This is the same two-hop trap already hit once, with the
   weekly synthesis allowlist: data that is safely fenced at hop one can
   still leak unfenced at hop two if each hop is designed in isolation.
   Fence the delta at BOTH hops, or don't build the second hop yet.

2. **Schema changes go through the existing migration sequence.** Any new
   table or column for stored deltas is a `PRAGMA user_version` sequential
   migration, same as every schema change since the 2026-08 improvement
   pass — no ad hoc `ALTER TABLE` outside that sequence.

3. **The window output contract changes — validate before adoption.**
   Adding a structured delta alongside the prose digest changes what the
   window prompt (`prompts/digest.md`, parsed back in `digest/summarize.py`)
   must reliably produce. It gets validated against real digests
   before adoption, exactly as the BRIEFING contract itself was — a schema
   that looks right on a handful of synthetic cases is not enough evidence
   for something the site will render as fact.

4. **Idempotency.** Deltas key on `(arc slug, digest)`, matching the
   codebase's existing idempotency discipline — a re-run over the same
   window must not duplicate delta rows, the same guarantee CLAUDE.md's
   hard rules already demand of items and digests.

### Cost

A prompt-contract change to the window-digest prompt (produced 8×/day, so a
contract failure surfaces fast), a parser for the new structured section,
one schema migration, site-side ingest, and rendering
work for both the "What changed" UI and (if §2 ships) the arc timeline that
consumes the same data.

## 5. Community signals — trending, radar, discuss-in-Telegram

**Status:** proposed 2026-08-10, PARKED. Origin: Codex redesign brief for
news.tomhorvath.me, triaged 2026-08-10.

**What it would change.** Per-arc Telegram message/reaction counts, a
"trending in the community" list, an attention radar, and deep links into
the Telegram discussion behind a story.

### Why parked — two named blockers, in order

(a) **The audience question, unanswered.** Is the site a personal
instrument or a community product? If the real readership is one person
plus a small group who already live in the source Telegram groups, these
features are dashboards of the owner's own attention, not a community
product — a different thing entirely. Nothing here is worth building until
that question is answered; building it first would be designing the answer
by accident.

(b) **The attribution pipeline doesn't exist.** The collector ingests
source groups for items, not for discussion. Mapping a Telegram discussion
message back to a specific arc is a new fuzzy pipeline — its own model
calls, its own error modes, none of it built today. And surfacing per-member
message/reaction counts on a website, even a private one, moves member
activity across a boundary it doesn't cross today: right now the digest
reads groups the owner is in and reports on content, never on who said what
how often. That boundary is a design decision worth naming explicitly before
crossing it, not an accident of scope creep.

### Cost and preservation

Cost: high — a new attribution pipeline plus site surface. Value: unknowable
until (a) is answered. No preservation work needed while this stays parked;
nothing here interacts with any existing design decision because nothing
here gets built.

## 6. Context mode — "60-second context" per arc

**Status:** proposed 2026-08-10, deferred. Origin: Codex redesign brief for
news.tomhorvath.me, triaged 2026-08-10.

**What it would change.** Generated background explainers per arc, at
1-minute / 5-minute / deep-dive depths, so a reader picking up a story mid-
arc gets oriented without reading every prior brief.

### Why deferred

Value is unproven until arc pages (§2) exist and actual reader behavior
shows people need this — an explainer feature bolted onto a page nobody
visits yet answers a question nobody's asked. Every depth level is more
model calls and another prompt file to keep honest over time, and unlike a
brief (written once, read, archived), an explainer must stay current as its
arc evolves — that's a cache-invalidation cost this codebase doesn't carry
anywhere else today. Injection posture is fine as proposed (toolless, same
inputs as today's summarizer), so that isn't the blocker; readiness and
ongoing cost are.

### One architectural note

On-demand generation does not fit the current static-publish architecture —
the toom-edge Worker would have to call a model at request time, which is a
new architecture for that repo, not a feature added to this one. If this
ever happens, it is pre-generated at publish time, for ranked arcs only, the
same "compute once, publish, serve static" shape everything else in this
pipeline already uses.
