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

from pathlib import Path

from digest.state import Item
from digest.summarize import SummarizeError, run_claude, validate_output

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "patreon.md"

# Summarizing one already-written post is closer to translate.py's mechanical
# rewrite than to daily.py's editorial synthesis across a day of briefings --
# there is no cross-source judgment to make, just one document to compress.
# Fixed here rather than threaded from Config.claude_effort for that reason:
# raising the window digest's effort should not silently raise the cost of
# every Patreon post, of which there is roughly one a day.
_PATREON_EFFORT = "medium"

# Bounds what reaches the prompt. The collector already caps a post body at
# _MAX_BODY_CHARS; this is the belt to that braces, covering a title long
# enough to matter and any future caller that builds a prompt from an Item
# this module did not collect.
_MAX_PROMPT_TEXT_CHARS = 8000


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


def summarize_post(item: Item, model: str, timeout_seconds: int) -> str:
    """Summarize one Patreon post into Hungarian briefing markdown.

    Raises `SummarizeError` when the model returns something without a real
    `## ` heading -- a refusal, an empty answer, a stray apology. The caller
    must NOT create a digest row in that case: with no row recorded, the
    post's item keeps `digest_id IS NULL`, so `known_source_ids` still
    reports it unknown and the next hourly run simply tries again. That
    self-healing property is the whole reason `known_source_ids` keys on
    the digest link rather than on mere row presence.
    """
    output = run_claude(build_prompt(item), model, timeout_seconds, effort=_PATREON_EFFORT)
    validate_output(output)
    return output


__all__ = ["SummarizeError", "build_prompt", "summarize_post"]
