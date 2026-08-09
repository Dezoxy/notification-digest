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
