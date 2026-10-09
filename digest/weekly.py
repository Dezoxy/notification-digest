"""Builds the weekly-brief prompt and invokes the Claude CLI.

A sibling of digest/daily.py, one editorial rung further up: where a daily
brief synthesizes a day's worth of already-summarized WINDOW digests, a
weekly brief synthesizes a WEEK's worth of already-summarized DAILY briefs
(digest/state.py's `get_daily_digests_since`, never raw items or raw window
digests -- see digest/main.py's `run_weekly`). It reuses summarize.py's
`run_claude`, `validate_output`, and `enforce_link_allowlist` for the exact
same reason digest/daily.py does: the weekly brief's output is still
"briefing markdown that must have a real `## ` heading" and "a markdown
document whose links must be checked for provenance", the same two
contracts every level of this pipeline has to satisfy.

Like digest/daily.py's `summarize_daily` (and UNLIKE digest/translate.py's
`translate_digest`), this module's `summarize_weekly` does NOT soft-fail: a
weekly brief is a deliverable in its own right (the reader's once-a-week
synthesis), not a cosmetic nicety layered on top of an already-delivered
digest, so its failure must propagate and fail the run/alert exactly like
digest/daily.py's `summarize_daily` does for a daily brief.
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

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "weekly.md"

# The weekly brief is editorial work (a third pass of judgment, on top of the
# window and daily passes already applied below it) -- it runs at the same
# effort tier as window summarization and the daily brief (Config.claude_effort),
# never translate.py's fixed, cheaper `_TRANSLATE_EFFORT`. `effort` is
# threaded in from the caller (digest/main.py's `run_weekly`) rather than
# hardcoded here, exactly like digest/daily.py's `summarize_daily` does.
_BUDAPEST_TZ = ZoneInfo("Europe/Budapest")


def _parse_utc(created_at: str) -> datetime:
    """Parse an ISO8601 `created_at` as an aware UTC datetime.

    Identical to digest/daily.py's `_parse_utc` -- not reimported from there
    because it's a private, single-line helper, not a shared contract (unlike
    the summarize.py functions this module DOES import); duplicating a
    three-line naive-timestamp guard is cheaper than introducing a cross-
    import for it. A naive value (no tzinfo) is treated as UTC -- every
    `created_at` this codebase ever writes is `datetime.now(UTC).isoformat()`
    (digest/state.py's `create_digest`), so this only guards a hypothetical
    hand-edited/legacy value.
    """
    parsed = datetime.fromisoformat(created_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _daily_separator_label(created_at: str) -> str:
    """Render `created_at` as the weekly prompt's "Weekday, D Month" separator label.

    Europe/Budapest, per CLAUDE.md's storage-stays-UTC / convert-at-render-
    time convention and identical to digest/daily.py's `_briefing_separator_label`
    -- the source daily brief's own creation instant, not "now" (a daily
    brief written on Tuesday keeps that label regardless of when the weekly
    brief that includes it later runs). Unlike a daily brief's own
    `HH:MM CET/CEST` separator (a daily brief runs several times a day, so
    the reader needs the TIME to place each one within the day), a weekly
    brief's inputs are one-per-day, so the reader needs the DAY, not a time
    of day -- hence "Monday, 4 August" rather than a clock time. `%-d` (no
    leading zero) is the same glibc/BSD strftime extension already relied on
    elsewhere in this codebase (see digest/deliver.py's `_deliver_email`),
    safe here for the identical reason: it behaves identically on macOS (BSD
    libc, local dev) and the Linux container this actually deploys to (glibc).
    """
    return f"{_parse_utc(created_at).astimezone(_BUDAPEST_TZ):%A, %-d %B}"


def build_weekly_prompt(daily_rows: list[tuple[int, str, int, str]], now: datetime) -> str:
    """Load prompts/weekly.md and substitute its NOW_LABEL/DAILY_COUNT/ITEM_TOTAL/DAILIES.

    `daily_rows` is digest/state.py's `get_daily_digests_since` output --
    `(id, created_at, item_count, body_md)` tuples for the week's daily
    briefs, ASCENDING by id (oldest first) -- rendered in that same order so
    a thread's course reads chronologically ("X was first reported Monday;
    by Friday Y"), never re-sorted here. Mirrors digest/daily.py's
    `build_daily_prompt` in every respect below; only the per-row separator
    label and the substituted placeholder names differ (day label instead of
    time-of-day label; DAILY_COUNT instead of BRIEFING_COUNT; DAILIES instead
    of BRIEFINGS).

    Each row renders as a `--- Weekday, D Month ---` separator
    (`_daily_separator_label`, a code-generated, deterministic line -- safe
    to embed unescaped) followed by that daily brief's `body_md`, blank-line
    separated from the next. `now` is used only for `{{NOW_LABEL}}` -- the
    wall-clock instant this brief is being WRITTEN at, which is NOT the same
    as any individual daily brief's own `created_at` -- giving the model
    grounding for "this is Sunday evening, the end of the week" framing
    (the prompt's own "weekly report" instruction) independent of when the
    weekly job actually happens to run.

    Each daily brief's `body_md` gets the IDENTICAL fencing discipline
    digest/daily.py's `build_daily_prompt` applies to its own `body_md`
    argument (and, one level further down, digest/translate.py's
    `build_translate_prompt` applies to ITS `body_md` argument), for the
    identical reason: every daily brief here is this codebase's OWN prior
    synthesis output, but derived (two levels removed) from the same
    untrusted, scraped Telegram/X/news material as everything else in this
    pipeline, so it gets full fencing discipline rather than an assumption
    that our own prior output is automatically safe to embed unescaped
    inside the outer ```text fence prompts/weekly.md wraps {{DAILIES}} in:

    1. Every backtick is escaped to its JSON unicode-escape form
       (`` ` `` -> `\\u0060`) -- a literal ``` surviving inside a daily brief
       could otherwise make the model perceive the outer fence as closed
       early. `summarize_weekly` reverses this exact substitution on the
       model's OUTPUT before doing anything else with it (mirroring
       `summarize_daily`'s own reversal), so the reader never sees a raw
       `\\u0060` in a delivered weekly brief.
    2. Every "{{" is neutralized into "{ {" via the same lookahead-based
       substitution `_sanitize_recent_coverage_heading` (digest/summarize.py),
       `build_translate_prompt` (digest/translate.py), and `build_daily_prompt`
       (digest/daily.py) all use -- defense in depth so a run of 3+ braces
       can't leave a live placeholder-shaped pair behind, even though
       {{DAILIES}} is the last substitution this function performs (see
       below) and so nothing here rescans it either way.

    Substitution order: {{NOW_LABEL}}, {{DAILY_COUNT}}, {{ITEM_TOTAL}} first
    (all three are code-generated, deterministic strings with no untrusted
    content in them -- safe to substitute in any order relative to each
    other), then {{DAILIES}} strictly LAST -- identical ordering discipline
    to digest/daily.py's `build_daily_prompt` and digest/summarize.py's
    `build_prompt`, and for the identical reason: str.replace rescans its
    whole input on every call, so if the untrusted, daily-brief-derived
    {{DAILIES}} block went in before a later placeholder's substitution, a
    daily brief that happened to contain the literal text "{{DAILY_COUNT}}"
    could get rewritten by that later call. Doing {{DAILIES}} last means no
    subsequent `.replace()` ever rescans text this function itself inserted
    from untrusted-derived content.
    """
    template = _PROMPT_PATH.read_text()

    rendered = []
    for _digest_id, created_at, _item_count, body_md in daily_rows:
        separator = f"--- {_daily_separator_label(created_at)} ---"
        escaped_body = body_md.replace("`", "\\u0060")
        escaped_body = re.sub(r"\{(?=\{)", "{ ", escaped_body)
        rendered.append(f"{separator}\n{escaped_body}")
    dailies_block = "\n\n".join(rendered)

    daily_count = len(daily_rows)
    item_total = sum(item_count for _, _, item_count, _ in daily_rows)
    now_label = f"{now.astimezone(_BUDAPEST_TZ):%H:%M %Z}"

    return (
        template.replace("{{NOW_LABEL}}", now_label)
        .replace("{{DAILY_COUNT}}", str(daily_count))
        .replace("{{ITEM_TOTAL}}", str(item_total))
        .replace("{{DAILIES}}", dailies_block)
    )


def summarize_weekly(
    daily_rows: list[tuple[int, str, int, str]],
    allowed_urls: Collection[str],
    model: str,
    timeout_seconds: int,
    effort: str,
    *,
    now: datetime | None = None,
    fallbacks: Sequence[FallbackLeg] = (),
    fallback_budget_seconds: int = 180,
) -> tuple[str, ModelRun]:
    """Build the weekly prompt, run it through Claude, validate and repair the contract.

    Returns `(body_md, model_run)`, NOT a bare string -- `model_run` (a
    `digest.summarize.ModelRun`) reports which model/effort actually
    produced this brief and whether a fallback leg fired, exactly like
    `summarize_daily()`'s own second return value (which itself mirrors
    `summarize()`'s fourth) -- see `run_with_fallbacks`'s docstring (point
    6) for the full shape. digest/main.py's `run_weekly` persists it
    alongside the digest (`create_digest`'s `provenance` column).

    `fallbacks` (default `()`, matching every existing direct call and
    test) is this weekly brief's own Claude API fallback chain -- see
    digest/summarize.py's `run_with_fallbacks` for the full mechanics, and
    `fallback_budget_seconds` (default 180) the SHARED wall-clock budget
    every leg of that chain draws from together. digest/main.py's
    `run_weekly` passes `cfg.fallback_timeout_seconds` and the
    editorial-tier chain built by its own `_fallback_legs` helper -- the
    same tier `summarize`/`summarize_daily`/`summarize_positions`/
    `summarize_post` all use, since a weekly brief is equally editorial
    work (see this module's own docstring).

    Pipeline mirrors digest/daily.py's `summarize_daily` (which itself
    mirrors digest/summarize.py's `summarize`): build the prompt
    (`build_weekly_prompt`, called with `datetime.now(UTC)` -- the weekly
    brief is always written "now", unlike a daily brief's own `created_at`
    which is a stored past instant), run it through `claude -p` (`run_claude`,
    at the caller-supplied `model`/`effort` -- always `cfg.anthropic_model` /
    `cfg.claude_effort`, the SAME tier window summarization and the daily
    brief both use, since this is equally editorial work: clustering
    threads, weighting significance, deciding what to drop), then
    `validate_output` (at least one real `## ` heading -- a refusal must not
    be mistaken for a real brief).

    Like `digest/daily.py`'s `summarize_daily` (and UNLIKE `digest/translate
    .py`'s `translate_digest`), this function does NOT catch and soft-fail:
    `SummarizeError` from either `run_claude` or `validate_output` propagates
    straight to the caller. A weekly brief is the deliverable itself (the
    reader's once-a-week synthesis), not a cosmetic layer on top of an
    already-delivered digest -- so its failure must fail the run and alert,
    exactly like a daily brief's own summarization failure does.

    Backtick un-escaping runs BEFORE `enforce_link_allowlist`, identically to
    `summarize_daily`: the escape `build_weekly_prompt` applies has no
    `json.loads` on this side to reverse it automatically, so this function
    does it explicitly, before the allowlist pass sees the brief's real
    final shape.

    `enforce_link_allowlist` then checks every link in the model's output
    against `allowed_urls` -- the caller passes the TRANSITIVE union of every
    in-window WINDOW digest's own stamped item URLs (digest/main.py's
    `run_weekly`), NOT the union of the source daily briefs' own stamped item
    URLs: a daily brief stamps no items of its own (digest/state.py's
    `create_digest` `kind` docstring), so `get_digest_item_urls` against a
    daily digest_id is always empty -- the caller has to reach one hop
    further down, to the WINDOW digests underneath those dailies, for the
    URLs a weekly brief is actually allowed to cite. See `run_weekly`'s own
    docstring for the full two-hop derivation; this function itself is
    agnostic to where `allowed_urls` came from, it only enforces membership.

    `strip_tldr_citations` then `renumber_citations` (imported from
    digest/summarize.py, not reimplemented here -- the weekly brief is
    "briefing markdown" under the identical TL;DR/citation contract a window
    digest and a daily brief both have) run last, after link repair, for the
    identical reason digest/daily.py's own `summarize_daily` applies them in
    that position: the owner's requirement that the TL;DR paragraph carry no
    citation links applies to every briefing this codebase produces, weekly
    brief included, and renumbering only rewrites link TEXT -- never a URL --
    so it cannot affect `enforce_link_allowlist`'s provenance guarantee.
    """
    # `now` is overridable (keyword-only) for a hypothetical weekly BACKFILL
    # script, mirroring digest/daily.py's own `summarize_daily` -- see its
    # docstring for why this exists even though no such script exists yet
    # for the weekly brief. Live callers omit it.
    prompt = build_weekly_prompt(daily_rows, now if now is not None else datetime.now(UTC))
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
