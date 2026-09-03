"""Builds the arc-context ("background primer") prompt and invokes the Claude CLI.

PLAN.md §11.6, "context mode": a short, DURABLE background primer per story
arc -- what the Strait of Hormuz is, who controls it, why it structurally
matters -- for a reader landing mid-thread on a long-running story with no
context. This is deliberately scoped as BACKGROUND, not news: it explains the
durable context a reader needs (geography, actors, why it matters
structurally), never a recap of recent events (the briefings and the arc
page's own timeline already do that -- see prompts/arc-context.md's own "must
never be a recap" contract). Background ages in years, not hours, so v1
generates a primer ONCE per arc and never regenerates it -- there is no
"refresh" path anywhere in this module or its caller (digest/main.py's
`run_daily`), on purpose.

A sibling of digest/translate.py, kept as its own small module for the
identical reason that one is: a distinct, single-purpose, TOOLLESS Claude CLI
call with its own prompt contract (prompts/arc-context.md), not another
summarization concern folded into digest/summarize.py. Reuses summarize.py's
`run_claude` exactly like translate.py does -- no tools, no web access, a
plain prompt-in/text-out call -- and `enforce_link_allowlist` for the
identical link-provenance reason given on `generate_arc_context` below.

Unlike `translate_digest`'s "faithful rewrite of already-finished prose",
this module's input is a single short, UNTRUSTED, model-generated story-arc
LABEL (a past digest's own `## ` heading text, resolved by digest/main.py's
caller from digest/state.py's `get_latest_arc_occurrence` plus digest/
publish.py's `derive_topics`) -- nothing else. In particular: no brief text,
no RECENT_COVERAGE, no window items, no prior arc-context content. This is
deliberate fencing discipline, the same "never let an optional feature
become a second, unfenced replay channel" posture PLAN.md §11.3 applies to
`deltas` (see digest/state.py's `write_deltas` GUARDRAIL comment) -- feeding
this call nothing but the label means it can never leak brief or coverage
content into a channel that has no fencing of its own, however the calling
code evolves later.

This module deliberately contains ONLY the prompt-build + single-CLI-call
pipeline (mirroring translate.py's own scope exactly: `build_*_prompt` +
one pipeline function). The SELECTION logic -- which arc keys currently
qualify for a primer, where to read each one's most recent label from, and
persisting the result -- lives in digest/main.py's `run_daily`, alongside
the identical selection/orchestration logic that function already contains
for its sibling steps (e.g. building `allowed_urls` from `rows` inline,
rather than delegating that loop into digest/daily.py). Keeping this module
symmetric with digest/translate.py's own scope, rather than absorbing the
orchestration here too, is a deliberate choice to match established
precedent rather than introduce a second shape for "how a pipeline module
relates to main.py".
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

from digest.summarize import (
    FallbackLeg,
    SummarizeError,
    enforce_link_allowlist,
    run_claude,
    run_with_fallbacks,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "arc-context.md"

# Fixed, not threaded from Config the way digest/summarize.py's `run_claude`
# effort is (CLAUDE_EFFORT): mirrors digest/translate.py's own
# `_TRANSLATE_EFFORT` reasoning exactly -- writing 3-5 paragraphs of
# durable, mostly-already-known background about a single named place/actor/
# institution is closer to translation's "no editorial judgment" shape
# (no clustering, no weighting, no deciding what to cut across many
# competing stories) than to window/daily summarization's editorial work, so
# a fixed, lower effort level is the right default and not worth a second
# config knob on top of CONTEXT_MODEL/CONTEXT_TIMEOUT_SECONDS.
_CONTEXT_EFFORT = "medium"

# The exact, case-sensitive sentinel prompts/arc-context.md instructs the
# model to output, verbatim and alone, when the label is too vague/generic to
# write real background about (e.g. "Also this window" leaking through, or a
# label so thin it names no actual place/actor/institution). Checked after
# `.strip()` -- run_claude already strips its stdout, but this module strips
# again defensively rather than assuming that upstream contract never
# changes.
_INSUFFICIENT_CONTEXT_SENTINEL = "INSUFFICIENT_CONTEXT"

# Caps the returned markdown's length. The prompt asks for 3-5 SHORT
# paragraphs -- at a generous ~120 words/paragraph and ~6 characters/word
# (English prose, including spaces and punctuation), 5 paragraphs lands
# around 3,600 characters; 6,000 leaves comfortable headroom above that
# estimate for a verbose response while still bounding a pathological
# runaway generation (the model ignoring the "short" instruction entirely)
# to a fixed, small size before it is ever persisted or shipped to the site
# -- mirroring this codebase's established "prompt instructions are never a
# hard guarantee, bound the output in code too" posture (e.g.
# digest/summarize.py's _MAX_RECENT_COVERAGE_HEADING_CHARS). Unlike a
# digest's own body, a primer is generated ONCE and stored forever -- an
# unbounded primer would be a permanent, not a one-run, cost.
_MAX_CONTEXT_MD_CHARS = 6000
_TRUNCATION_MARKER = " …[truncated]"


def build_context_prompt(label: str) -> str:
    """Load prompts/arc-context.md and substitute the {{ARC_LABEL}} placeholder.

    `label` gets the identical fencing discipline digest/translate.py's
    `build_translate_prompt` applies to its own `body_md` argument, for the
    identical reason: it rides inside its own fenced ```text block below
    (prompts/arc-context.md), and a literal ``` surviving inside it could
    make the model perceive that fence as closed early, exposing whatever
    follows as if it were outside the "this is data" boundary rather than
    inside it. `label` is a past digest's own `## ` heading text -- this
    codebase's OWN prior summarization output, not raw scraped text -- but,
    exactly like `build_translate_prompt`'s own `body_md` argument, it is
    still derived from the same untrusted Telegram/X/news material as
    everything else in this pipeline, so it gets full fencing discipline
    rather than an assumption that our own prior output is automatically
    safe to embed unescaped.

    Every backtick is escaped to its JSON unicode-escape form (`` ` `` ->
    `\\u0060`); every "{{" is neutralized into "{ {" via the same
    lookahead-based substitution `build_translate_prompt` and
    digest/summarize.py's `_sanitize_recent_coverage_heading` both use.
    `generate_arc_context` reverses the backtick escape on the model's
    output before returning it (mirroring `translate_digest`'s own
    reversal), so a reader never sees a raw `\\u0060` artifact in a
    delivered primer; the brace neutralization is never reversed, for the
    identical reason `build_translate_prompt` never reverses its own (a
    stray "{ {" instead of "{{" is not a visible defect in prose no one
    would type doubled braces into in the first place).

    `{{ARC_LABEL}}` is this template's only placeholder, so there is no
    LATER `.replace()` call here for an untouched "{{" to be rescanned the
    way build_prompt's multi-placeholder ordering discipline worries about
    -- this neutralization is pure defense in depth, matching
    `build_translate_prompt`'s own posture for the identical reason: never
    leave a live-looking placeholder token sitting in text derived from
    someone else's input, regardless of whether the current code path
    happens to rescan it.
    """
    template = _PROMPT_PATH.read_text()
    escaped = label.replace("`", "\\u0060")
    escaped = re.sub(r"\{(?=\{)", "{ ", escaped)
    return template.replace("{{ARC_LABEL}}", escaped)


def _validate_context_output(output: str) -> None:
    """Accept the INSUFFICIENT_CONTEXT sentinel or any non-empty text; reject only emptiness.

    `run_with_fallbacks` (digest/summarize.py) needs a single `validate`
    callable to decide, per leg, whether that leg's output counts as a
    success -- mirroring digest/positions.py's `_validate_positions_output`
    for the identical reason: the model correctly reporting
    `INSUFFICIENT_CONTEXT` (see `_INSUFFICIENT_CONTEXT_SENTINEL`) is a
    CORRECT outcome, not a failed one, exactly like positions' own
    NO-SIGNAL sentinel -- a fallback model that reaches that same correct
    verdict must not be treated as having failed the leg, or the chain
    would keep trying other models on a label that every one of them
    correctly judges too thin to write about. `validate_output` itself
    (the "at least one real `## ` heading" gate) is deliberately NOT reused
    here, unlike every other kind's validate function -- see
    `generate_arc_context`'s own docstring for why a primer's contract is
    the OPPOSITE of that gate's assumption (no headings at all). The only
    thing this function actually rejects is empty/whitespace-only text
    surviving neither case -- a refusal, an empty answer -- which
    `generate_arc_context` itself already treats as a failure once
    `run_with_fallbacks` returns it.
    """
    if output.strip() == _INSUFFICIENT_CONTEXT_SENTINEL:
        return
    if not output.strip():
        raise SummarizeError("generate_arc_context: empty output")


def generate_arc_context(
    label: str,
    model: str,
    timeout_seconds: int,
    fallbacks: Sequence[FallbackLeg] = (),
    fallback_budget_seconds: int = 180,
) -> str | None:
    """Generate one arc's background primer. Never raises; None on any failure.

    This is a PRODUCTION step run from the DAILY run only (digest/main.py's
    `run_daily`, PLAN.md §11.6 rule 1), AFTER that run's own daily brief has
    already been produced and delivered -- generation failing here must never
    affect, delay, or degrade the daily brief itself, which by the time this
    is ever called has already been fully handled. Mirrors
    `translate_digest`'s soft-fail contract exactly: the caller treats `None`
    as "no primer this attempt" and, per PLAN.md §11.6's own accepted
    behavior (documented on digest/main.py's `_generate_arc_context_primers`,
    the caller that decides what to do with a `None`), does NOT store
    anything for it -- the arc simply stays eligible for reconsideration on a
    later daily run, exactly like a `None` from `translate_digest` leaves a
    digest's `body_md_hu` column simply unset rather than storing a sentinel
    "translation failed" value.

    Returns `None` in THREE distinct cases, deliberately collapsed into one
    return value (the caller cannot and does not need to tell them apart --
    see `_generate_arc_context_primers`'s own docstring for why that
    collapsing is actually the right call, not a gap):
    1. `run_claude` itself fails (non-zero exit, empty stdout, a timeout, or
       -- unlike `translate_digest` -- ANY `SafeguardsRefusalError` too,
       including a refusal, with no SAME-PROVIDER fallback-model retry: a
       short, factual background primer about a real place/actor/institution
       has no plausible reason to trip a real-time safety classifier the way
       a security-heavy NEWS digest can, so this module still does not carry
       `translate_digest`'s Claude-to-Claude `fallback_model` machinery --
       there is no observed failure mode here that machinery would
       meaningfully address, and PLAN.md §11.6's own Config list
       (CONTEXT_MODEL, CONTEXT_TIMEOUT_SECONDS -- no fallback field)
       confirms this was the intended shape. This is UNRELATED to, and
       unchanged by, the `fallbacks`/OpenRouter chain documented below: that
       chain reaches for a DIFFERENT provider on ANY primary failure
       (subscription limits, transient CLI errors -- not just a safeguards
       refusal), which is exactly why `run_with_fallbacks` catches every
       failure shape uniformly rather than special-casing
       `SafeguardsRefusalError` the way `translate_digest` still does for
       its own, narrower, same-provider retry.
    2. The model's own `INSUFFICIENT_CONTEXT` sentinel (prompts/
       arc-context.md's escape hatch for a label too vague to write real
       background about) -- logged at INFO, not WARNING, since this is an
       expected, correctly-working outcome, not a failure.
    3. Empty/whitespace-only output surviving both of the above (defensive;
       `run_claude` itself already raises on empty stdout, so this is belt-
       and-suspenders against a hypothetical future change to that
       contract).

    On success, the output goes through `enforce_link_allowlist(output,
    allowed_urls=())` -- an EMPTY allowlist, not the digest's own item URLs
    the way `translate_digest`/`summarize_daily` use theirs. This is
    deliberate, not an oversight: prompts/arc-context.md's own contract
    forbids links/citations entirely ("no links, no citations, no URLs of
    any kind"), and this primer was generated from nothing but a short
    label -- it never had any legitimate source URLs to cite in the first
    place, so there is no real allowlist to check against. But "prompt
    instructions are never a hard guarantee" is this codebase's own
    established posture (see e.g. digest/publish.py's `map_deltas_to_slugs`
    docstring) -- an empty allowlist means ANY link the model emits despite
    the prompt's instruction not to (a hallucination, or one induced by a
    prompt injection surviving into the label from the scraped material it
    was condensed from) gets stripped to plain text or defanged, exactly
    like an unrecognized citation in a digest body would be. This primer is
    generated once and stored/shipped forever, unlike a window digest that
    gets a fresh allowlist every 6 hours -- there is no natural "recheck"
    point later, so this is the one and only chance to neutralize a bad
    link before it becomes permanent, world-readable site content.

    `validate_output` (the "at least one real `## ` heading" gate every
    other briefing-shaped output in this codebase goes through) is
    DELIBERATELY NOT applied here: prompts/arc-context.md's own contract is
    the OPPOSITE of that gate's assumption -- a primer must have NO
    headings at all ("plain markdown, no headings"). Applying
    `validate_output` to this output would reject every well-formed primer
    and pass through a malformed one that happened to contain a stray
    "## " line, exactly backwards.

    Backtick un-escaping (`\\u0060` -> `` ` ``) runs on the raw output BEFORE
    `enforce_link_allowlist`, identically to `translate_digest`: the escape
    `build_context_prompt` applies has no `json.loads` on this side to
    reverse it automatically, so this function does it explicitly, before
    the allowlist pass sees the primer's real final shape.

    Finally, the result is capped at `_MAX_CONTEXT_MD_CHARS` -- see that
    constant's own comment for the sizing rationale -- with
    `_TRUNCATION_MARKER` appended when truncation actually happens, so a
    reader (or this codebase's own tests) can tell a cut-short primer apart
    from one that legitimately ended at exactly the cap.

    `fallbacks` (default `()`, matching every existing direct call and
    test) is this primer's own OpenRouter chain, on the LIGHT tier
    (`Config.fallback_light_models`, the same tier `translate_digest` uses
    -- see digest/main.py's `_fallback_legs`): writing a few short,
    factual paragraphs is closer to translation's "no editorial judgment"
    shape than to window/daily summarization's (`_CONTEXT_EFFORT`'s own
    comment). `fallback_budget_seconds` (default 180) is the SHARED
    wall-clock budget every leg of that chain draws from together -- see
    digest/summarize.py's `run_with_fallbacks` for the full mechanics.
    Validated with `_validate_context_output` (above), NOT the bare
    `validate_output` this function deliberately never applies to its own
    primary output either -- see that function's own docstring for why
    `INSUFFICIENT_CONTEXT` must count as a leg succeeding, not failing.
    """
    try:
        prompt = build_context_prompt(label)
        output = run_with_fallbacks(
            primary=lambda: run_claude(prompt, model, timeout_seconds, effort=_CONTEXT_EFFORT),
            fallbacks=fallbacks,
            prompt=prompt,
            budget_seconds=fallback_budget_seconds,
            validate=_validate_context_output,
        )
    # Broad on purpose, not just SummarizeError: this function's contract is
    # NEVER raises -- a background primer is a nice-to-have enrichment, not
    # a deliverable in its own right, and an unanticipated failure shape (an
    # unreadable prompt file, a pathological template, anything future) must
    # degrade to "no primer this attempt" exactly like an anticipated one,
    # never take down the daily run that already produced and delivered its
    # own brief by the time this is ever called.
    except Exception as exc:
        logger.warning("generate_arc_context: generation failed: %s", type(exc).__name__)
        return None

    output = output.strip()
    if output == _INSUFFICIENT_CONTEXT_SENTINEL:
        logger.info("generate_arc_context: model reported insufficient context for this label")
        return None
    if not output:
        return None

    output = output.replace("\\u0060", "`")
    output = enforce_link_allowlist(output, allowed_urls=())

    if len(output) > _MAX_CONTEXT_MD_CHARS:
        output = output[:_MAX_CONTEXT_MD_CHARS].rstrip() + _TRUNCATION_MARKER

    return output
