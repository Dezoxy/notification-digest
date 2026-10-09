"""Builds the per-post Patreon prompt and invokes the Claude CLI.

A sibling of digest/daily.py and digest/translate.py, kept as its own module
for the same reason those are: a distinct feature with its own prompt
contract (prompts/patreon.md) and its own input shape. It reuses
digest/summarize.py's `run_claude` and `validate_output` rather than
re-implementing them -- the output is still "briefing markdown that must
have a real `## ` heading", the same structural contract every other kind
satisfies.

TWO THINGS DIVERGE from every other kind here.

ONE POST, ONE DIGEST. Every other kind summarizes MANY items into ONE
document. This one summarizes ONE post into ONE document, and a run
produces as many digests as there were new posts. That is what makes the
owner's "one Telegram message per post" shape fall out of the existing
delivery loop rather than needing a parallel one -- and it means each
message independently inherits deliver.py's freshness guard and its
per-run 429 circuit breaker, both written after the 2026-08-06 flood.

NO TRANSLATION PASS. The source is already Hungarian, so this prompt
produces Hungarian directly. Routing it through the English summarizer and
then digest/translate.py would cost two Claude calls and a lossy round trip
to arrive back where the source started. `prompts/translate-hu.md` exists
for briefings written in English about English-language material; it is not
the right tool for a Hungarian post.

Like digest/daily.py's `summarize_daily` and unlike digest/translate.py's
`translate_digest`, this does NOT soft-fail: a post's summary is the
deliverable itself, not a nicety layered on an already-delivered message,
so its failure must propagate and fail the run.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from digest.state import Item
from digest.summarize import (
    FallbackLeg,
    SummarizeError,
    run_claude,
    run_with_fallbacks,
    validate_output,
)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "patreon.md"

# Summarizing one already-written post is closer to translate.py's mechanical
# rewrite than to daily.py's editorial synthesis across a day of briefings --
# there is no cross-source judgment to make, just one document to compress.
# Fixed here rather than threaded from Config.claude_effort for that reason:
# raising the window digest's effort should not silently raise the cost of
# every Patreon post, of which there is roughly one a day.
_PATREON_EFFORT = "medium"

# Bounds what reaches the prompt. The collector already caps a post body at
# its own _MAX_BODY_CHARS; this is the belt to that braces, covering a title
# long enough to matter and any future caller that builds a prompt from an
# Item this module did not collect. Kept ABOVE the collector's cap so this
# never becomes the binding limit by accident -- when the two disagree, the
# collector's is the one that should win, because it is the one documented
# against measured post sizes.
_MAX_PROMPT_TEXT_CHARS = 56_000


def build_prompt(item: Item) -> str:
    """Render prompts/patreon.md with this post's title and body substituted in.

    The title is passed SEPARATELY from the body rather than relying on the
    model to find it inside the text, even though `collectors/patreon.py`
    already prepends it: the prompt's contract asks for the post's real
    title as the `## ` heading, and giving it as its own labelled field is
    what makes "don't invent a new one" checkable by the model.

    Both substitutions are positional string replacements into a template
    read from disk, matching how daily.py/translate.py render theirs. The
    post text is untrusted -- the prompt's own security section is what
    handles that, and `run_claude` disables every tool as defence in depth.
    """
    template = _PROMPT_PATH.read_text(encoding="utf-8")
    title, _, body = item.text.partition("\n\n")
    return template.replace("{{POST_TITLE}}", title.strip()[:300]).replace(
        "{{POST_TEXT}}", (body.strip() or title.strip())[:_MAX_PROMPT_TEXT_CHARS]
    )


def summarize_post(
    item: Item,
    model: str,
    timeout_seconds: int,
    fallbacks: Sequence[FallbackLeg] = (),
    fallback_budget_seconds: int = 180,
) -> str:
    """Summarize one Patreon post into Hungarian briefing markdown.

    Raises `SummarizeError` when the model returns something without a real
    `## ` heading -- a refusal, an empty answer, a stray apology. The caller
    must NOT create a digest row in that case: with no row recorded, the
    post's item keeps `digest_id IS NULL`, so `known_source_ids` still
    reports it unknown and the next hourly run simply tries again. That
    self-healing property is the whole reason `known_source_ids` keys on
    the digest link rather than on mere row presence.

    `fallbacks` (default `()`, matching every existing direct call and
    test) is this post's own Claude API fallback chain -- see
    digest/summarize.py's `run_with_fallbacks` for the full mechanics, and
    `fallback_budget_seconds` (default 180) the SHARED wall-clock budget
    every leg of that chain draws from together. digest/main.py's
    `_deliver_one_post` passes `cfg.fallback_timeout_seconds` and the
    editorial-tier chain built by its own `_fallback_legs` helper -- a
    post's summary is this feature's own deliverable (see this module's own
    docstring), the same reasoning that puts it on the editorial tier
    alongside `summarize`/`summarize_daily`/`summarize_weekly`/
    `summarize_positions` rather than the lighter translate/context tier.
    """
    prompt = build_prompt(item)
    # `run_with_fallbacks` now also returns a `ModelRun` reporting which
    # model/effort actually served -- discarded here, not threaded through
    # `summarize_post`'s own return: a Patreon digest is Telegram-only
    # (`_PATREON_HIDDEN_CHANNELS`, digest/main.py), never published to the
    # site, and model provenance is a site-facing feature (digest/main.py's
    # `_deliver`/`run_daily`/`run_weekly` are the only callers that persist
    # it via `create_digest`'s `provenance` column) -- there is nowhere for
    # this kind's provenance to go.
    output, _model_run = run_with_fallbacks(
        primary=lambda: run_claude(prompt, model, timeout_seconds, effort=_PATREON_EFFORT),
        fallbacks=fallbacks,
        prompt=prompt,
        budget_seconds=fallback_budget_seconds,
        primary_model=model,
        primary_effort=_PATREON_EFFORT,
        validate=validate_output,
    )
    return output


__all__ = ["SummarizeError", "build_prompt", "summarize_post"]
