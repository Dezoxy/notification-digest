"""Builds the daily-brief prompt and invokes the Claude CLI.

A sibling of digest/summarize.py and digest/translate.py, kept as its own
module for the same reason translate.py is: this is a distinct feature with
its own prompt contract (prompts/daily.md) and its own input shape (a day's
worth of already-summarized WINDOW digests, never raw items -- see
digest/main.py's `run_daily`). It reuses summarize.py's `run_claude`,
`validate_output`, and `enforce_link_allowlist` rather than re-implementing
any of them: the daily brief's output is still "briefing markdown that must
have a real `## ` heading" and "a markdown document whose links must be
checked for provenance", exactly the same two contracts the window digest and
the Hungarian translation already have to satisfy.

Unlike digest/translate.py's `translate_digest`, this module's `summarize_daily`
does NOT soft-fail: a daily brief is a deliverable in its own right (the
reader's once-a-day synthesis), not a cosmetic nicety layered on top of an
already-delivered English digest, so its failure must propagate and fail the
run/alert exactly like digest/summarize.py's `summarize` does for a window
digest.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from digest.summarize import (
    FallbackLeg,
    ModelRun,
    enforce_link_allowlist,
    renumber_citations,
    run_claude,
    run_with_fallbacks,
    strip_tldr_citations,
    validate_output,
)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "daily.md"

# The daily brief is editorial work (a second pass of judgment over the day's
# already-curated briefings, not a mechanical rewrite) -- it runs at the same
# effort tier as window summarization (Config.claude_effort), never
# translate.py's fixed, cheaper `_TRANSLATE_EFFORT`. `effort` is threaded in
# from the caller (digest/main.py's `run_daily`) rather than hardcoded here,
# exactly like digest/summarize.py's `summarize` does.
_BUDAPEST_TZ = ZoneInfo("Europe/Budapest")


def _parse_utc(created_at: str) -> datetime:
    """Parse an ISO8601 `created_at` as an aware UTC datetime.

    A naive value (no tzinfo) is treated as UTC -- every `created_at` this
    codebase ever writes is `datetime.now(UTC).isoformat()` (digest/state.py's
    `create_digest`), so this only guards a hypothetical hand-edited/legacy
    value, mirroring digest/summarize.py's `_format_digest_age` and
    digest/publish.py's `_local_header_label`, which apply the identical
    fallback for the identical reason.
    """
    parsed = datetime.fromisoformat(created_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _briefing_separator_label(created_at: str) -> str:
    """Render `created_at` as the daily prompt's "HH:MM CET/CEST" separator time.

    Europe/Budapest, per CLAUDE.md's storage-stays-UTC / convert-at-render-
    time convention -- the digest's own creation instant, not "now" (a
    briefing written at 09:00 keeps that label regardless of when the daily
    brief that includes it later runs). `%Z` on a zoneinfo-aware datetime
    resolves to the correct DST-aware abbreviation ("CET" in winter, "CEST"
    in summer) for the exact date being formatted, unlike a hardcoded
    offset or a single fixed abbreviation.
    """
    return f"{_parse_utc(created_at).astimezone(_BUDAPEST_TZ):%H:%M %Z}"


def build_daily_prompt(digest_rows: list[tuple[int, str, int, str]], now: datetime) -> str:
    """Load prompts/daily.md and substitute its NOW_LABEL/BRIEFING_COUNT/ITEM_TOTAL/BRIEFINGS.

    `digest_rows` is digest/state.py's `get_window_digests_since` output --
    `(id, created_at, item_count, body_md)` tuples for the day's window
    digests, ASCENDING by id (oldest first) -- rendered in that same order so
    a story's arc reads chronologically ("X said A in the morning; by
    evening B"), never re-sorted here.

    Each row renders as a `--- briefing @ HH:MM CET/CEST ---` separator
    (`_briefing_separator_label`, a code-generated, deterministic line -- safe
    to embed unescaped) followed by that briefing's `body_md`, blank-line
    separated from the next. `now` is used only for `{{NOW_LABEL}}` -- the
    wall-clock instant this brief is being WRITTEN at, which is NOT the same
    as any individual briefing's own `created_at` -- giving the model
    grounding for "this is the end of the day" framing (the prompt's own
    "evening brief" instruction) independent of when the daily job actually
    happens to run.

    Each briefing's `body_md` gets the IDENTICAL fencing discipline
    digest/translate.py's `build_translate_prompt` applies to its own
    `body_md` argument, for the identical reason (see that function's
    docstring in full): every briefing here is this codebase's OWN prior
    summarization output, but derived from the same untrusted, scraped
    Telegram/X/news material as everything else in this pipeline, so it gets
    full fencing discipline rather than an assumption that our own prior
    output is automatically safe to embed unescaped inside the outer
    ```text fence prompts/daily.md wraps {{BRIEFINGS}} in:

    1. Every backtick is escaped to its JSON unicode-escape form
       (`` ` `` -> `\\u0060`) -- a literal ``` surviving inside a briefing
       could otherwise make the model perceive the outer fence as closed
       early. `summarize_daily` reverses this exact substitution on the
       model's OUTPUT before doing anything else with it (mirroring
       `translate_digest`'s own reversal), so the reader never sees a raw
       `\\u0060` in a delivered daily brief.
    2. Every "{{" is neutralized into "{ {" via the same lookahead-based
       substitution `_sanitize_recent_coverage_heading`
       (digest/summarize.py) and `build_translate_prompt`
       (digest/translate.py) both use -- defense in depth so a run of 3+
       braces can't leave a live placeholder-shaped pair behind, even though
       {{BRIEFINGS}} is the last substitution this function performs (see
       below) and so nothing here rescans it either way.

    Substitution order: {{NOW_LABEL}}, {{BRIEFING_COUNT}}, {{ITEM_TOTAL}}
    first (all three are code-generated, deterministic strings with no
    untrusted content in them -- safe to substitute in any order relative to
    each other), then {{BRIEFINGS}} strictly LAST -- identical ordering
    discipline to digest/summarize.py's `build_prompt` (COLLECTOR_STATUS,
    then RECENT_COVERAGE, then ITEMS_JSON last) and for the identical reason:
    str.replace rescans its whole input on every call, so if the untrusted,
    briefing-derived {{BRIEFINGS}} block went in before a later placeholder's
    substitution, a briefing that happened to contain the literal text
    "{{BRIEFING_COUNT}}" could get rewritten by that later call. Doing
    {{BRIEFINGS}} last means no subsequent `.replace()` ever rescans text
    this function itself inserted from untrusted-derived content.
    """
    template = _PROMPT_PATH.read_text()

    rendered = []
    for _digest_id, created_at, _item_count, body_md in digest_rows:
        separator = f"--- briefing @ {_briefing_separator_label(created_at)} ---"
        escaped_body = body_md.replace("`", "\\u0060")
        escaped_body = re.sub(r"\{(?=\{)", "{ ", escaped_body)
        rendered.append(f"{separator}\n{escaped_body}")
    briefings_block = "\n\n".join(rendered)

    briefing_count = len(digest_rows)
    item_total = sum(item_count for _, _, item_count, _ in digest_rows)
    now_label = f"{now.astimezone(_BUDAPEST_TZ):%H:%M %Z}"

    return (
        template.replace("{{NOW_LABEL}}", now_label)
        .replace("{{BRIEFING_COUNT}}", str(briefing_count))
        .replace("{{ITEM_TOTAL}}", str(item_total))
        .replace("{{BRIEFINGS}}", briefings_block)
    )


def summarize_daily(
    digest_rows: list[tuple[int, str, int, str]],
    allowed_urls: Collection[str],
    model: str,
    timeout_seconds: int,
    effort: str,
    *,
    now: datetime | None = None,
    fallbacks: Sequence[FallbackLeg] = (),
    fallback_budget_seconds: int = 180,
) -> tuple[str, ModelRun]:
    """Build the daily prompt, run it through Claude, validate and repair the contract.

    Returns `(body_md, model_run)`, NOT a bare string -- `model_run` (a
    `digest.summarize.ModelRun`) reports which model/effort actually
    produced this brief and whether a fallback leg fired, exactly like
    `summarize()`'s own fourth return value -- see that function's
    docstring and `run_with_fallbacks`'s (point 6) for the full shape.
    digest/main.py's `run_daily` persists it alongside the digest
    (`create_digest`'s `provenance` column).

    `fallbacks` (default `()`, matching every existing direct call and
    test) is this daily brief's own OpenRouter fallback chain -- see
    digest/summarize.py's `run_with_fallbacks` for the full mechanics, and
    `fallback_budget_seconds` (default 180) the SHARED wall-clock budget
    every leg of that chain draws from together. digest/main.py's
    `run_daily` passes `cfg.fallback_timeout_seconds` and the editorial-tier
    chain built by its own `_fallback_legs` helper -- the same tier
    `summarize`/`summarize_weekly`/`summarize_positions`/`summarize_post`
    all use, since a daily brief is equally editorial work (see this
    module's own docstring).

    Pipeline mirrors digest/summarize.py's `summarize`: build the prompt
    (`build_daily_prompt`, called with `datetime.now(UTC)` -- the daily brief
    is always written "now", unlike a window digest's own `created_at` which
    is a stored past instant), run it through `claude -p` (`run_claude`, at
    the caller-supplied `model`/`effort` -- always `cfg.anthropic_model` /
    `cfg.claude_effort`, the SAME tier window summarization uses, since this
    is equally editorial work: clustering arcs, weighting significance,
    deciding what to drop), then `validate_output` (at least one real `## `
    heading -- a refusal must not be mistaken for a real brief).

    Unlike `digest/translate.py`'s `translate_digest`, this function does
    NOT catch and soft-fail: `SummarizeError` from either `run_claude` or
    `validate_output` propagates straight to the caller. A daily brief is
    the deliverable itself (the reader's once-a-day synthesis), not a
    cosmetic layer on top of an already-delivered English digest the way a
    translation is -- so its failure must fail the run and alert, exactly
    like a window digest's own summarization failure does.

    Backtick un-escaping runs BEFORE `enforce_link_allowlist`, identically to
    `translate_digest`: the escape `build_daily_prompt` applies has no
    `json.loads` on this side to reverse it automatically, so this function
    does it explicitly, before the allowlist pass sees the brief's real
    final shape.

    `enforce_link_allowlist` then checks every link in the model's output
    against `allowed_urls` -- the caller passes the UNION of every source
    window digest's own stamped item URLs (digest/main.py's `run_daily`), not
    a fresh set of raw item URLs: a daily brief cites the SAME URLs its
    source briefings already cited (this prompt's own contract: citations
    must be copied verbatim from the input, never invented), so the
    provenance check is against exactly the URLs those briefings were
    themselves allowed to cite.

    `strip_tldr_citations` then `renumber_citations` (imported from
    digest/summarize.py, not reimplemented here -- the daily brief is
    "briefing markdown" under the identical TL;DR/citation contract a window
    digest has) run last, after link repair, for the identical reason
    digest/summarize.py's own `summarize` applies them in that position: the
    owner's requirement that the TL;DR paragraph carry no citation links
    applies to every briefing this codebase produces, daily brief included,
    and renumbering only rewrites link TEXT -- never a URL -- so it cannot
    affect `enforce_link_allowlist`'s provenance guarantee.
    """
    # `now` is overridable (keyword-only) for the daily BACKFILL script,
    # which synthesizes briefs for PAST evenings and must frame NOW_LABEL at
    # the historical 20:00 being briefed, not the wall-clock moment the
    # backfill happens to run. Live callers omit it.
    prompt = build_daily_prompt(digest_rows, now if now is not None else datetime.now(UTC))
    output, model_run = run_with_fallbacks(
        primary=lambda: run_claude(prompt, model, timeout_seconds, effort),
        fallbacks=fallbacks,
        prompt=prompt,
        budget_seconds=fallback_budget_seconds,
        primary_model=model,
        primary_effort=effort,
        validate=validate_output,
    )
    output = output.replace("\\u0060", "`")
    output = enforce_link_allowlist(output, allowed_urls)
    return renumber_citations(strip_tldr_citations(output)), model_run
