"""Builds the Hungarian translation prompt and invokes the Claude CLI.

A thin sibling of digest/summarize.py, kept as its own module rather than
folded into that one: translation is a distinct, optional, soft-failing
PRODUCTION step (see translate_digest's docstring) with its own prompt
contract (prompts/translate-hu.md), not another summarization concern. It
reuses summarize.py's `run_claude`, `validate_output`, and
`enforce_link_allowlist` rather than re-implementing any of them -- the
translated output is still "briefing markdown that must have a real `## `
heading" and "a markdown document whose links must be checked for
provenance", exactly the same two contracts the English output already has
to satisfy, so the identical enforcement functions apply unchanged.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Sequence
from dataclasses import replace
from pathlib import Path

from digest.summarize import (
    FallbackLeg,
    ModelRun,
    SafeguardsRefusalError,
    enforce_link_allowlist,
    run_claude,
    run_with_fallbacks,
    validate_output,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "translate-hu.md"

# Fixed at `medium`, not threaded through from Config the way
# digest/summarize.py's `run_claude` effort is (CLAUDE_EFFORT): translation
# is mechanically easier than summarization -- there is no editorial
# judgment to exercise (clustering stories, weighting significance, deciding
# what to cut), only a faithful rewrite of already-finished prose into
# Hungarian while leaving structure/links/numbers untouched -- so a lower,
# fixed effort level is the right default and not worth a second config knob
# on top of CLAUDE_EFFORT.
_TRANSLATE_EFFORT = "medium"


def build_translate_prompt(body_md: str) -> str:
    """Load prompts/translate-hu.md and substitute the {{DIGEST_MD}} placeholder.

    `body_md` gets the identical fencing discipline digest/summarize.py's
    `build_prompt` applies to ITEMS_JSON, for the identical reason: it rides
    inside its own fenced ```markdown block below (prompts/translate-hu.md),
    and a literal ``` surviving inside it could make the model perceive that
    fence as closed early, exposing whatever follows as if it were outside
    the "this is data" boundary rather than inside it. `body_md` is this
    codebase's OWN prior summarization output, not raw scraped text -- but it
    is derived from the same untrusted Telegram/X/news material as
    everything else in this pipeline (a prompt injection that survived
    summarize()'s own defenses would ride straight through here otherwise),
    so it gets full fencing discipline rather than an assumption that our own
    prior output is automatically safe to embed unescaped.

    Every backtick is escaped to its JSON unicode-escape form (`` ` `` ->
    `\\u0060`), exactly the token build_prompt substitutes for ITEMS_JSON.
    Unlike ITEMS_JSON, this text is never run back through `json.loads` --
    there is no parser on the other end to silently undo the escape -- so a
    translation model asked to preserve structure exactly is expected to
    reproduce the literal `\\u0060` token verbatim (it has no special meaning
    to a model reading surrounding Hungarian prose, so there's nothing to
    tempt it into "helpfully" altering it). `translate_digest` reverses this
    exact substitution on the model's output before it does anything else
    with it, so the reader never sees a raw `\\u0060` in a delivered digest --
    see that function's docstring for why the reversal has to be explicit
    here, unlike the JSON case.

    Every "{{" is also neutralized the same way
    `_sanitize_recent_coverage_heading` neutralizes it for a past digest
    heading (digest/summarize.py) -- broken into "{ {" via the same
    lookahead-based substitution, so a run of 3+ braces can't leave a live
    pair behind. `{{DIGEST_MD}}` is the only placeholder this template
    defines, so there is no LATER `.replace()` call here for an untouched
    "{{" to be rescanned by the way build_prompt's ordering comment worries
    about -- this neutralization is pure defense in depth, for the same
    reason `_sanitize_recent_coverage_heading` applies it to text that also
    derives from untrusted material: never leave a live-looking placeholder
    token sitting in text this codebase generated from someone else's input,
    regardless of whether the CURRENT code path happens to rescan it.
    Unlike the backtick escape, this one is never reversed -- a stray "{ {"
    instead of "{{" is not a visible defect in translated prose (no reader
    would ever type doubled braces in the first place), so there's nothing
    to restore.
    """
    template = _PROMPT_PATH.read_text()
    escaped = body_md.replace("`", "\\u0060")
    escaped = re.sub(r"\{(?=\{)", "{ ", escaped)
    return template.replace("{{DIGEST_MD}}", escaped)


def translate_digest(
    body_md: str,
    allowed_urls: Collection[str],
    model: str,
    timeout_seconds: int,
    fallback_model: str | None = None,
    fallbacks: Sequence[FallbackLeg] = (),
    fallback_budget_seconds: int = 180,
) -> tuple[str, ModelRun] | None:
    """Translate a validated English digest to Hungarian. Never raises; None on any failure.

    This is a PRODUCTION step run on the VM after summarize() has already
    produced and validated the English `body_md` (digest/main.py's
    `_deliver`) -- it is deliberately soft-failing and NEVER a delivery
    channel of its own: a translation failure must never block, delay, or
    degrade the English digest, which has already been fully handled by the
    time this is called. Callers (see digest/main.py) treat `None` as "this
    digest stays English-only, forever" -- there is no retry mechanism, on
    purpose: unlike a transient SMTP or HTTP failure, a rerun of the exact
    same prompt against the exact same model has no natural convergence
    guarantee (nothing about a second `claude -p` call makes it more likely
    to produce valid output than the first), so retrying would just spend
    another model call per attempt for no expected improvement in odds. And
    the failure mode is genuinely cosmetic, not a lost delivery: the site
    (digest/publish.py's `publish_to_site`) always has the English body/HTML
    to fall back to when the Hungarian fields are absent from the ingest
    payload -- a missing translation is invisible to the reader unless they
    specifically look for the Hungarian version, whereas a lost English
    digest would be a real gap in the record.

    That "no retry, no convergence guarantee" rationale explicitly does NOT
    apply to `fallback_model`: live incident (digests 20 and 60,
    2026-08-01) showed `model` (TRANSLATE_MODEL, typically the "sonnet"
    alias resolving to the newest Sonnet) can deterministically REFUSE a
    security-heavy digest's content, because that model ships a real-time
    safety classifier the content trips (see
    digest/summarize.py's `SafeguardsRefusalError`). A same-model retry
    against that exact refusal genuinely has zero expected improvement in
    odds, which is exactly why `run_claude` is not simply called twice with
    `model` here -- but `fallback_model` is a DIFFERENT model, one without
    that classifier, so its odds of succeeding on the identical content are
    independent of the first call's outcome, not a repeat of the same
    doomed bet. Translation needs no frontier capability -- it is a
    faithful structural rewrite, not an editorial judgment call (see
    `_TRANSLATE_EFFORT`'s own comment) -- so falling back to an older Sonnet
    is an acceptable quality trade for keeping the Hungarian channel alive
    on content the primary model won't touch.

    Pipeline: build the prompt (`build_translate_prompt`), run it through
    `claude -p` at a fixed `medium` effort (see `_TRANSLATE_EFFORT`'s
    comment -- translation is mechanically easier than summarization, no
    editorial judgment involved), then apply the IDENTICAL two-stage
    contract enforcement digest/summarize.py's `summarize()` applies to the
    English output: `validate_output` (at least one real `## ` heading -- a
    refusal or empty output must not be mistaken for a translated briefing)
    and `enforce_link_allowlist` (every link's URL checked against
    `allowed_urls`, exactly the same set of stamped item URLs the English
    digest was checked against). The translator can mangle or hallucinate a
    URL exactly as readily as the summarizer can -- prompts/translate-hu.md's
    contract asks it not to, but that's a request, not a guarantee, so the
    translated markdown gets the identical provenance pass rather than an
    assumption that "just translating" is a lower-risk operation than
    summarizing.

    If the `model` call raises `SafeguardsRefusalError` specifically, this
    is not folded into the generic failure path below: when `fallback_model`
    is falsy, it is re-raised (the broad `except Exception` below still
    catches it and returns `None`, exactly like any other failure -- there
    is simply no fallback configured). When `fallback_model` is set, a
    WARNING is logged (naming both models -- these are operator config, a
    handful of fixed model-alias strings, not scraped content, so logging
    them carries none of the secrecy concern the rest of this pipeline's
    error paths are built around) and `run_claude` is called a second time
    with `fallback_model`, the identical prompt/timeout_seconds/effort. Any
    failure of that fallback call -- another refusal, a timeout, a
    validation failure, anything -- falls straight through to the same
    broad `except Exception` below and returns `None`, exactly like a
    single-attempt failure always has.

    `run_claude`'s own failure (a non-zero exit, empty stdout, or a timeout)
    and `validate_output`'s failure both raise `SummarizeError` (of which
    `SafeguardsRefusalError` is a subclass), but the catch below is
    deliberately broader than that one type -- see its inline comment: the
    NEVER-raises contract has to hold for failure shapes nobody
    anticipated, too. Only the exception's TYPE NAME is logged (a WARNING),
    never its message or the prompt/output content -- identical secrecy
    posture to every other error path touching this pipeline's model calls
    (see SummarizeError's own docstring).

    Backtick un-escaping runs BEFORE `enforce_link_allowlist`: the escape
    `build_translate_prompt` applies (backtick -> `\\u0060`) has no `json
    .loads` on this side to reverse it automatically, so this function does
    it explicitly -- a straight string replace back to a literal backtick --
    so a delivered Hungarian digest never shows the raw escape token to the
    reader. Doing this before the allowlist pass (rather than after) means
    `enforce_link_allowlist`'s markdown-link/autolink/bare-URL regexes see
    the digest's REAL final shape, not a shape still carrying an artifact
    from the fencing defense.

    `fallbacks` (default `()`, matching every existing direct call and
    test) is this translation's own OpenRouter chain, on the LIGHT tier
    (`Config.fallback_light_models`, the same tier `generate_arc_context`
    uses -- see digest/main.py's `_fallback_legs`): translation is a
    mechanical rewrite, not editorial judgment (`_TRANSLATE_EFFORT`'s own
    comment), so it does not need the heavier editorial-tier models
    `summarize`/`summarize_daily`/etc. fall back to. `fallback_budget_seconds`
    (default 180) is the SHARED wall-clock budget every leg of THAT chain
    draws from together -- see digest/summarize.py's `run_with_fallbacks`
    for the full mechanics. The primary closure passed to it is the
    EXISTING two-step call above (`model`, then -- on a
    `SafeguardsRefusalError` specifically -- `fallback_model`) UNCHANGED: that
    is a same-provider, same-mechanism retry this function has always done
    on its own, orthogonal to reaching for an entirely different provider
    via OpenRouter, so it stays exactly where it was, just wrapped in one
    more layer of retry rather than replaced by it. Because this whole
    function's own `except Exception` below still catches anything
    `run_with_fallbacks` itself ultimately raises (every leg, OpenRouter
    included, exhausted) and soft-fails to English-only, `fallbacks` can
    only ever IMPROVE this function's odds of producing a Hungarian digest
    -- it can never turn a translation that would have succeeded before
    this feature existed into one that now fails the run, matching this
    function's own "never raises" contract exactly as it always has.

    Returns `(body_md_hu, model_run)`, NOT a bare string, on success -- see
    `run_with_fallbacks`' own docstring (point 6) for why `model_run` (a
    `digest.summarize.ModelRun`) exists at all and how it's normally built.
    THIS function's `model_run` needs one correction `run_with_fallbacks`
    itself cannot make: `_primary` below is ITSELF a two-step retry (`model`,
    then -- on a `SafeguardsRefusalError` -- `fallback_model`), so a
    primary-path success (`model_run.fallback is False`) does not always
    mean `model` produced the output -- it can just as well mean
    `fallback_model` did, if the SAME-PROVIDER retry inside `_primary` is
    what actually succeeded. `run_with_fallbacks` has no visibility into
    that inner retry at all (point 1 of its own docstring: `primary` is an
    opaque closure), so this function tracks which of the two actually ran
    itself (`served_model`, mutated by `_primary` via `nonlocal`) and
    corrects `model_run.model` after the call, via `dataclasses.replace`,
    whenever the primary path served but the model that served isn't the
    one `run_with_fallbacks` was told to label it with. digest/main.py's
    `_deliver`/`run_daily`/`run_weekly` persist the corrected result
    unchanged (`create_digest`'s `provenance` column).
    """

    try:
        prompt = build_translate_prompt(body_md)

        # Mutated by `_primary` below (via `nonlocal`) the moment the
        # SAME-PROVIDER `fallback_model` retry actually runs -- see this
        # function's own docstring for why `run_with_fallbacks` cannot see
        # or report this on its own, and why this function corrects
        # `model_run.model` against it after the call returns.
        served_model = model

        def _primary() -> str:
            nonlocal served_model
            try:
                return run_claude(prompt, model, timeout_seconds, effort=_TRANSLATE_EFFORT)
            except SafeguardsRefusalError:
                if not fallback_model:
                    raise
                logger.warning(
                    "translate_digest: %s refused by the API safety classifier; "
                    "retrying with fallback model %s",
                    model,
                    fallback_model,
                )
                served_model = fallback_model
                return run_claude(prompt, fallback_model, timeout_seconds, effort=_TRANSLATE_EFFORT)

        output, model_run = run_with_fallbacks(
            primary=_primary,
            fallbacks=fallbacks,
            prompt=prompt,
            budget_seconds=fallback_budget_seconds,
            primary_model=model,
            primary_effort=_TRANSLATE_EFFORT,
            validate=validate_output,
        )
    # Broad on purpose, not just SummarizeError: this function's contract is
    # NEVER raises -- translation is cosmetic, and an unanticipated failure
    # shape (an unreadable prompt file, a pathological template, anything
    # future) must degrade to English-only exactly like an anticipated one,
    # never take down the run that already produced a valid English digest.
    except Exception as exc:
        logger.warning("translate_digest: translation failed: %s", type(exc).__name__)
        return None

    if not model_run.fallback and model_run.model != served_model:
        # `run_with_fallbacks` labeled the primary path with `model` (the
        # only identity it was told about), but `_primary`'s own inner
        # safeguards-refusal retry is what actually served this call -- fix
        # the label up to the model that really produced `output`. See this
        # function's own docstring for the full "why run_with_fallbacks
        # can't do this itself" reasoning.
        model_run = replace(model_run, model=served_model)

    output = output.replace("\\u0060", "`")
    return enforce_link_allowlist(output, allowed_urls), model_run
