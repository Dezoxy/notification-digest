"""Builds the summarization prompt and invokes the Claude CLI.

See PLAN.md §4.4 and §5. The `claude` CLI authenticates via the owner's
persisted Max-subscription login (CLAUDE_CONFIG_DIR) -- no API key, no SDK
dependency here, just a subprocess call.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path

from digest.config import claude_subprocess_env
from digest.state import Item

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "digest.md"

# Bounds each item's text as it's embedded in the prompt payload. Telegram
# messages can reach 4096 chars; at up to _MAX_ITEMS_PER_DIGEST (200) items
# per run (digest/main.py), 200 x 2000 chars keeps the prompt comfortably
# inside the model's context window. Truncation happens only in the prompt
# payload built here -- the stored item text in the database stays
# full-length, untouched.
_MAX_ITEM_TEXT_CHARS = 2000
_TRUNCATION_MARKER = " …[truncated]"

# Bounds the BUILT prompt's UTF-8 BYTE length, checked by
# select_items_for_prompt. Bytes track tokens far better than characters
# across scripts: ASCII text runs roughly 0.25 tokens/byte (~4 bytes per
# token), while CJK/emoji-heavy text runs roughly 0.5-0.75 tokens/byte, so
# 300_000 bytes is <= ~200k tokens even in the worst case and ~75k tokens
# for a typical ASCII-heavy batch -- deliberately conservative on both ends,
# since a shrunk prefix here just means the remainder drains on a later run
# (the 3-hourly timer is the drain loop -- see _MAX_ITEMS_PER_DIGEST in
# digest/main.py). Per-item text is already capped at _MAX_ITEM_TEXT_CHARS,
# but _MAX_ITEMS_PER_DIGEST (digest/main.py) items of emoji/CJK-heavy source
# text can still serialize to far more BYTES than a naive character count
# would suggest -- measuring characters instead of encoded bytes was exactly
# the gap that let, e.g., 200 items of 2000 emoji each (only ~432k Python
# characters, but ~1.6MB of UTF-8) slip past a 600k-CHAR bound while blowing
# the token budget. See select_items_for_prompt's docstring for why this
# must be measured on the built prompt's ENCODED bytes, not its character
# count.
_MAX_PROMPT_BYTES = 300_000


class SummarizeError(Exception):
    """Raised when the Claude CLI fails to produce a usable digest.

    Messages must stay short and must never include the prompt (which
    contains scraped Telegram/X message content).
    """


def _truncate_item_text(text: str) -> str:
    """Truncate a single item's text to _MAX_ITEM_TEXT_CHARS for the prompt payload.

    Text at or under the limit is returned unchanged; anything longer is cut
    to exactly _MAX_ITEM_TEXT_CHARS characters with a trailing marker
    appended, so the reader can tell the item was clipped. This bounds the
    prompt only -- see _MAX_ITEM_TEXT_CHARS's docstring comment for why, and
    note stored items are never touched by this function.
    """
    if len(text) <= _MAX_ITEM_TEXT_CHARS:
        return text
    return text[:_MAX_ITEM_TEXT_CHARS] + _TRUNCATION_MARKER


def build_prompt(items: list[Item], failed_sources: list[str], recent_coverage: str) -> str:
    """Load prompts/digest.md and substitute the coverage, status, and items-JSON placeholders.

    `recent_coverage` is the pre-rendered {{RECENT_COVERAGE}} block (see
    format_recent_coverage) -- this function does no formatting or
    sanitization of it itself, it only substitutes the string it's given.
    """
    template = _PROMPT_PATH.read_text()

    payload = [
        {
            "source": item.source,
            "chat_id": item.chat_id,
            "chat_title": item.chat_title,
            "author": item.author,
            "text": _truncate_item_text(item.text),
            "url": item.url,
            "fetched_at": item.fetched_at,
        }
        for item in items
    ]
    # ensure_ascii=False: the default (True) escapes every non-ASCII
    # character to a 6-char \uXXXX sequence (12 chars for a supplementary-
    # plane emoji, via a surrogate pair) -- pure serialization inflation with
    # no content benefit, since the payload is written straight to a UTF-8
    # subprocess stdin (see run_claude's encoding="utf-8"), never through
    # anything that only tolerates ASCII. For an emoji/CJK-heavy Telegram
    # batch this inflation alone can multiply prompt size several-fold,
    # which is exactly the gap select_items_for_prompt's size check below
    # closes -- but only if the size it measures is the true serialized
    # size, not one artificially inflated by this flag. The backtick-escape
    # pass just below is unaffected: it runs on the resulting string either
    # way and a literal backtick is ASCII, so it's untouched by this flag.
    items_json = json.dumps(payload, indent=2, ensure_ascii=False)
    # The prompt's consumer is an LLM, not a strict CommonMark parser: a
    # literal ``` inside an item's text can make the model perceive the
    # ```json data block as closed early, presenting whatever follows (in
    # the same item, or the rest of the JSON array) as text outside the
    # advertised data boundary -- i.e. as instructions rather than data.
    # Escaping every backtick to its JSON unicode escape is safe globally:
    # a backtick can only occur inside a JSON string value (never in JSON
    # structural syntax), and ` round-trips through json.loads to the
    # original backtick character, so the payload is unaffected.
    items_json = items_json.replace("`", "\\u0060")

    if failed_sources:
        collector_status = "Collector status: " + ", ".join(
            f"{source} collection failed this run" for source in failed_sources
        )
    else:
        collector_status = "Collector status: all collectors succeeded this run."

    # Substitution order is {{COLLECTOR_STATUS}}, then {{RECENT_COVERAGE}},
    # then {{ITEMS_JSON}} strictly LAST: str.replace scans its input left to
    # right looking for the placeholder, and that scan does not distinguish
    # template text from text just inserted by an earlier .replace() call.
    # If ITEMS_JSON went first and an item's text happened to contain the
    # literal "{{COLLECTOR_STATUS}}", the second .replace() would find that
    # match INSIDE the just-inserted JSON and silently rewrite collected
    # message content. Doing ITEMS_JSON last means no subsequent .replace()
    # ever rescans data it inserted.
    #
    # {{RECENT_COVERAGE}} carries the identical hazard, for the identical
    # reason, and so goes before {{ITEMS_JSON}} for the same reason ITEMS_JSON
    # itself goes last: `recent_coverage` (built by format_recent_coverage,
    # digest/state.py's get_recent_digests) is not static template text --
    # its lines are `## ` headings the MODEL wrote into a PAST digest, and
    # those past headings were themselves generated from the same scraped,
    # untrusted Telegram/X/news text this run's items come from. A prompt
    # injection that survived into a prior heading is exactly as capable of
    # embedding a literal "{{COLLECTOR_STATUS}}" or "{{ITEMS_JSON}}" as a
    # hostile item's raw text is -- so it must never be substituted into a
    # position some LATER .replace() call would rescan. Substituting it
    # before ITEMS_JSON (mirroring COLLECTOR_STATUS's position) means only
    # the deterministic, code-generated COLLECTOR_STATUS line is inserted
    # earlier still, and nothing here ever rescans text this function itself
    # inserted.
    #
    # This is defense in depth on top of a sanitization pass one layer down:
    # format_recent_coverage already neutralizes any "{{" sequence inside a
    # heading (replacing it with "{ {") before this function ever sees
    # `recent_coverage`, so even a successfully-injected past heading cannot
    # smuggle a live "{{...}}" placeholder into the built prompt at all --
    # the ordering rule above is what protects the *item* text from being
    # rescanned, this sanitization is what stops `recent_coverage` itself
    # from ever containing a working placeholder in the first place.
    return (
        template.replace("{{COLLECTOR_STATUS}}", collector_status)
        .replace("{{RECENT_COVERAGE}}", recent_coverage)
        .replace("{{ITEMS_JSON}}", items_json)
    )


def select_items_for_prompt(
    items: list[Item], failed_sources: list[str], recent_coverage: str, max_prompt_bytes: int
) -> list[Item]:
    """Return the longest OLDEST-first prefix of `items` whose built prompt fits.

    `recent_coverage` is threaded straight through to every build_prompt call
    this function makes (the full-list check and every binary-search
    candidate alike): it is embedded in the built prompt exactly like the
    items and the collector-status line are, so its byte length counts
    toward `max_prompt_bytes` automatically -- a large recent_coverage block
    (e.g. a run with an unusually busy last 24 hours) can itself reduce how
    many items fit in this run's prompt, the same way a bigger
    failed_sources banner would.

    The bound is checked against the BUILT prompt's UTF-8 BYTE length (via
    `len(build_prompt(candidate, failed_sources, recent_coverage).encode(
    "utf-8"))`), not its
    Python character count and not the sum of the items' text lengths.
    Bytes, not characters, are what correlates with the model's token
    budget: json.dumps(ensure_ascii=False) serializes each item's text as
    raw UTF-8, and a single non-ASCII character (CJK, emoji) can occupy 2-4
    bytes while still counting as exactly one Python `str` character --
    so a character-length check systematically UNDER-counts an
    emoji/CJK-heavy batch's true size. Concretely: 200 items of 2000 emoji
    each is only ~432k Python characters (comfortably under a naive
    600k-CHARACTER bound) but ~1.6MB of UTF-8 -- far beyond the token budget
    despite "passing" a char-based check. Measuring encoded bytes instead of
    characters is what closes that gap.

    Two things also inflate the built prompt well beyond what a naive sum
    of the items' raw text lengths would suggest: json.dumps still adds
    JSON's own structural characters and per-item key names (~40+ bytes of
    "source"/"chat_id"/"author"/"url"/"fetched_at" scaffolding per item on
    top of the text), and the surrounding prompts/digest.md template adds
    fixed overhead of its own. Summing raw text lengths would systematically
    underestimate the real prompt size and let an oversized batch through
    anyway -- the whole point of this function is to catch that gap by
    measuring the actual thing that gets handed to the model, in the unit
    (bytes) that actually correlates with tokens.

    `items` is assumed already ordered oldest-first (get_unsummarized_items
    returns it that way) -- this function preserves that order and never
    reorders or drops from the middle, it only shrinks from the end.

    Finds the LONGEST fitting prefix, not just *a* fitting one: the full
    list is tried first (the common case -- everything fits, return as-is
    with zero extra build_prompt calls). If that misses, this binary
    searches for the largest n in [1, len(items) - 1] whose built prefix
    fits, rather than halving n on a miss and stopping at the first hit.
    Halving-and-stop can badly under-fill: e.g. if 199 of 200 items would
    have fit, halving jumps straight to 100 and returns it, needlessly
    discarding 99 items (and roughly halving how fast the backlog drains)
    even though a much longer prefix fits. Binary search still converges in
    O(log n) build_prompt calls -- same order of magnitude as halving -- but
    lands on the actual longest fitting prefix instead of an arbitrary
    shorter one. Each build_prompt call is itself O(prefix size); at the
    n <= 200 scale this module operates at (_MAX_ITEMS_PER_DIGEST in
    digest/main.py), a handful of O(log n) builds is cheap.

    The search's fit predicate (`len(build_prompt(items[:n], ...).encode(
    "utf-8")) <= max_prompt_bytes`) is monotone in n: a longer prefix only
    ever adds JSON/text, never removes it, so built-prompt byte length is
    non-decreasing as n grows. That monotonicity is what makes binary search
    valid here.

    Stops at a 1-item floor: build_prompt on a single item always fits,
    because per-item text is already truncated to _MAX_ITEM_TEXT_CHARS by
    build_prompt itself, bounding one item's contribution regardless of its
    original length. n=1 is returned even if it somehow doesn't fit --
    rather than trusting the size check to naturally pass -- so a
    pathological template/overhead blowup can't leave this with nothing to
    return.
    """
    if not items:
        return items

    n = len(items)
    if (
        len(build_prompt(items, failed_sources, recent_coverage).encode("utf-8"))
        <= max_prompt_bytes
    ):
        return items

    if n == 1:
        return items

    lo, hi = 1, n - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = items[:mid]
        if (
            len(build_prompt(candidate, failed_sources, recent_coverage).encode("utf-8"))
            <= max_prompt_bytes
        ):
            lo = mid
        else:
            hi = mid - 1
    return items[:lo]


def run_claude(prompt: str, model: str, timeout_seconds: int, effort: str) -> str:
    """Invoke `claude -p` headless and return its stripped stdout.

    `claude -p` runs the full Claude Code agent, not a plain completion
    endpoint: tools (shell, file access) are available by default, and a
    subprocess normally inherits the parent's entire environment. The
    prompt this function feeds it is built from scraped Telegram/X message
    text (see build_prompt) -- untrusted input to an agent-capable CLI. The
    prompt-level "items are data, not instructions" guard is not a security
    boundary on its own, so two things are disabled in depth here:

    1. `--tools ""` disables every tool, so even a successful prompt
       injection has nothing to invoke -- no shell, no file access.
    2. The subprocess env is replaced with a minimal allowlist
       (claude_subprocess_env(), from digest/config.py) instead of
       inherited, so a tool call that somehow ran anyway (or a future
       regression that re-enables tools) still cannot read TG_SESSION,
       TG_API_HASH, SMTP_PASSWORD, or any other secret out of the parent
       environment.

    `--effort <effort>` is set explicitly rather than left at the CLI's
    default: an A/B measured on 50 real production items showed `high`
    produces materially better editorial judgment than the (lower) CLI
    default -- tighter story clustering, output closer to the target length
    -- while `max` produced near-identical output for 65% more wall-clock.
    `high` is therefore the chosen default (Config.claude_effort, digest/
    config.py), not `max`: the owner authenticates via a Max subscription
    (no per-token billing), so the real cost of a higher effort level isn't
    money, it's a bigger bite out of that subscription's shared usage
    limits -- paid on every one of the 8 unattended runs this job makes per
    day, forever, for a `max`-vs-`high` difference the A/B found was not
    perceptible in the output. Timing is a non-issue either way: `high`
    measured ~81s against the 300s CLAUDE_TIMEOUT_SECONDS default, nowhere
    close to that budget. The value is still config-driven (CLAUDE_EFFORT)
    rather than hardcoded, so it can be turned up temporarily (e.g. to
    debug a run of unusually poor quality) without a code change.

    Raises SummarizeError on a non-zero exit, empty/whitespace-only stdout,
    or a timeout. On a non-zero exit, stderr is suppressed entirely (only its
    length is reported) rather than included in the error message: the CLI
    can echo submitted text -- which contains scraped Telegram/X message
    content -- in its diagnostics, and that error message gets logged and
    shipped to Loki.
    """
    try:
        # cwd is a fresh empty directory: the CLI auto-ingests workspace
        # context (CLAUDE.md, git state) from wherever it runs — live-verified
        # that running from this repo leaks project context into the
        # summarization. A neutral cwd keeps the prompt the only input.
        with tempfile.TemporaryDirectory(prefix="digest-claude-") as neutral_cwd:
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    "--model",
                    model,
                    "--output-format",
                    "text",
                    "--tools",
                    "",
                    "--effort",
                    effort,
                ],
                input=prompt,
                capture_output=True,
                text=True,
                # Explicit encoding="utf-8": build_prompt now serializes with
                # ensure_ascii=False, so the prompt can contain raw non-ASCII
                # (emoji, CJK, etc.) UTF-8 text instead of \uXXXX escapes.
                # text=True alone would pick subprocess's default text
                # encoding, which falls back to locale.getpreferredencoding()
                # -- and claude_subprocess_env() is a scrubbed minimal
                # allowlist that carries no LANG/LC_ALL, so that fallback can
                # land on a non-UTF-8 (even ASCII-only) locale encoding and
                # raise UnicodeEncodeError writing the prompt to stdin. Pinning
                # utf-8 explicitly makes this independent of the parent
                # process's or subprocess's locale entirely.
                encoding="utf-8",
                timeout=timeout_seconds,
                env=claude_subprocess_env(),
                cwd=neutral_cwd,
            )
    except subprocess.TimeoutExpired as exc:
        raise SummarizeError(f"claude -p timed out after {timeout_seconds}s") from exc

    if result.returncode != 0:
        raise SummarizeError(
            f"claude -p exited {result.returncode} (stderr suppressed, "
            f"{len(result.stderr)} chars — rerun manually to inspect)"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise SummarizeError("claude -p returned empty output")

    return stdout


def _leading_whitespace_column(line: str) -> int:
    """Compute the CommonMark indentation column of a line's leading whitespace.

    Per CommonMark, indentation is measured in columns, not characters: a
    space advances the column by exactly 1, but a tab advances to the NEXT
    multiple-of-4 column (`col = col + 4 - (col % 4)`) -- the standard
    tab-stop expansion rule, identical to how a terminal renders a tab.
    Stops at the first character that is neither a space nor a tab. A line
    like " \t## Example" (one space, then a tab) reaches column 4
    from just two leading characters -- neither `line.startswith("\t")` nor
    `line[:4] == "    "` catches that, since the line starts with a space
    and its first four characters aren't four literal spaces, so a
    character-counting check silently misses this case.
    """
    col = 0
    for ch in line:
        if ch == " ":
            col += 1
        elif ch == "\t":
            col += 4 - (col % 4)
        else:
            break
    return col


def _real_heading_lines(markdown_text: str) -> list[str]:
    """Return the raw text (original case, merely stripped) of every real ATX `## ` heading line.

    This is the exact CommonMark-aware scanning logic validate_output has
    always used to decide what counts as a "real" `## ` heading -- fence
    tracking (backtick and tilde, matching delimiter character, closer run
    length >= opener run length, no info string on a closer) and
    column-based, tab-aware indentation exclusion. It was pulled out of
    validate_output verbatim, not reimplemented, specifically so a second
    caller (format_recent_coverage, below) can reuse the identical notion of
    "real heading" without the two ever being able to silently drift apart.
    See validate_output's docstring for the full CommonMark reasoning behind
    every rule enforced here; it is not repeated a second time in this
    docstring.

    Returns heading text in ORIGINAL case, with only surrounding whitespace
    stripped -- callers that need case-insensitive comparison (validate_output,
    to normalize before its emptiness check; format_recent_coverage, to match
    the "Needs attention" routing label case-insensitively) lowercase it
    themselves. This function makes no assumption about how its caller will
    use the text, so it doesn't discard case information the caller might
    need (format_recent_coverage renders the heading verbatim, in its
    original case, into the "recently covered" list).
    """
    heading_lines = []
    in_fence = False
    fence_char = None
    fence_len = 0
    for line in markdown_text.splitlines():
        # CommonMark: leading whitespace reaching column 4 or more makes
        # this an indented code block -- neither a heading nor a fence
        # delimiter can start here, regardless of what follows the
        # indentation. Computed via CommonMark tab-expansion rules (see
        # _leading_whitespace_column), not pattern-matched, so mixed
        # space+tab indentation that reaches column 4 is caught too.
        if _leading_whitespace_column(line) >= 4:
            continue
        stripped = line.strip()
        if in_fence:
            # CommonMark fence-closing rule: a closer must (1) start with a
            # run of the SAME delimiter character that opened the fence,
            # (2) that run must be AT LEAST as long as the opening run, and
            # (3) nothing but whitespace may follow the run -- unlike an
            # opener, a closer may not carry an info string. A ``` line
            # inside a ~~~ fence never matches (wrong character); a 3-tick
            # line inside a 4-tick fence matches too short a run and is just
            # content; "``` python" has trailing non-whitespace and is also
            # just content, not a closer.
            run_len = len(stripped) - len(stripped.lstrip(fence_char))
            remainder = stripped[run_len:]
            if run_len >= fence_len and remainder.strip() == "":
                in_fence = False
                fence_char = None
                fence_len = 0
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence_char = stripped[0]
            fence_len = len(stripped) - len(stripped.lstrip(fence_char))
            in_fence = True
            continue
        if stripped.startswith("## "):
            heading_lines.append(stripped[3:].strip())
    return heading_lines


def validate_output(markdown_text: str) -> None:
    """Enforce that the briefing markdown carries at least one real `## ` heading.

    `run_claude` only guarantees non-empty stdout — a refusal ("I can't help
    with that") would otherwise pass through untouched. If that garbage
    reaches `_deliver`, every item gets stamped with the digest id and, once
    SMTP succeeds, the digest row is final: those items are lost to
    summarization forever. This check must run before anything is
    persisted, and it must raise SummarizeError on failure so the caller
    skips writing a digest row entirely -- with no row recorded, the next
    run re-selects and re-summarizes the same items instead of treating
    them as already handled.

    The prompt's contract (prompts/digest.md) used to mandate three fixed,
    always-present section headings in a fixed order; this function used to
    hard-gate exactly that. That contract is gone: the current briefing
    format is free-form prose whose section headings are chosen per-run from
    whatever the window actually contains (an event's own name, a group's
    name, "## Also this window", ...) -- there is no fixed heading text left
    to check for. Hard-gating stylistic compliance (section names, counts,
    ordering, word budget, citation density, ...) against a model that can
    legitimately vary its wording every run is exactly the failure mode this
    repo already lived through once (see the old three-heading contract this
    replaces, and its git history): when the model drifts from a hard-gated
    stylistic template, retries loop forever instead of shipping something
    useful, because there is no guarantee ANY rerun converges on the exact
    template text. So this gate is now deliberately MINIMAL and STRUCTURAL
    only -- it enforces the one property a legitimate briefing can never
    fail to have (at least one real markdown heading) and nothing about the
    heading's text, count, or position. Everything about quality --
    "TL;DR opens the briefing", "at most ~8 sections", "roughly 900 words",
    the citation format -- lives in the prompt as an instruction to the
    model, not as a raising validator here; see summarize()'s soft checks
    for the two cases (missing TL;DR, zero citations) worth a log line
    without holding the whole run hostage over a nicety.

    A plain substring check (`"## " in text`) is fooled by a refusal that
    merely *mentions* a heading-shaped string inline -- e.g. "I cannot
    produce a ## heading in this case." contains the substring without a
    single real heading line. So instead this parses actual heading LINES: a
    line whose stripped form starts with exactly "## " (two hashes -- "### "
    subgroup headings are not h2 and don't count). At least one such line
    must exist; if none does (a bare refusal, empty prose, or any other
    output with zero real h2 headings), this raises. This is still exactly
    what stops a bare refusal like "I can't help with that" from passing as
    a real briefing -- the whole point of keeping a gate at all.

    The error message never includes the offending output itself, since it
    may contain scraped Telegram/X message content (see SummarizeError).

    Links are deliberately NOT validated here: a window with nothing but
    chatter legitimately produces zero citation links, and that is correct
    output, not a contract violation (see summarize()'s soft warning for
    this case instead).

    Fenced code blocks are also excluded from heading line detection: a
    refusal can legitimately quote heading-shaped text inside a fenced block
    (e.g. "here's the template you asked about:\n```\n## Example\n...") and
    that is not a real section -- it is example text sitting inside a code
    fence. Per CommonMark, a fence can
    be delimited by three-or-more backticks OR three-or-more tildes, and a
    fence only closes on a line starting with three-or-more of the SAME
    delimiter character that opened it -- a ``` line inside a ~~~ fence
    (or vice versa) is just fence content, not a closer. While the in-fence
    flag is set, "## " lines are not counted as headings, and the fence
    delimiter lines themselves are never counted either.

    CommonMark also requires the closing fence to be AT LEAST as long as
    the opening fence, and to contain nothing but the delimiter run plus
    trailing whitespace -- no info string is allowed on a closer (unlike
    the opener, which may carry one, e.g. ```json). So a 4-backtick opener
    is not closed by a 3-backtick line (that line is just fence content),
    and a line like "``` python" or "```extra" never closes a fence at all,
    regardless of run length, because it has non-whitespace after the
    delimiter run. This module tracks both the delimiter character and the
    opening run length to enforce this.

    Indented lines are excluded from heading and fence-delimiter detection,
    before any stripping happens: per CommonMark, an ATX heading (or a
    fence delimiter) may be indented at most 3 columns -- a line whose
    leading whitespace reaches column 4 or more is an indented code block
    instead. A refusal that pads a template with 4-space indentation (e.g.
    "    ## Example") is therefore code content, not a real heading, and
    must not satisfy the contract.

    Column, not character count, is what CommonMark actually measures: a
    tab does not advance by a literal 4 columns, it advances to the NEXT
    multiple-of-4 column (`col = col + 4 - (col % 4)`), the same rule a
    terminal or renderer uses to expand tabs. A single space always
    advances by exactly 1. This means a line can reach column 4 -- and so
    count as indented code -- with fewer than 4 leading characters: " \t"
    (one space, column 1, then a tab that jumps straight to column 4) is
    two characters but column 4, and a naive `line[:4] == "    "` /
    `line.startswith("\t")` check misses it entirely (the line doesn't
    start with a tab, and its first four characters aren't four literal
    spaces) -- letting a mixed space+tab-indented refusal template slip
    past this check as if it were a real heading line. See
    _leading_whitespace_column.

    The scanning itself (fence tracking, indentation exclusion) lives in the
    shared helper _real_heading_lines, not here -- it was pulled out
    verbatim so format_recent_coverage (which needs the actual heading TEXT,
    not just a yes/no) can reuse the identical CommonMark logic rather than
    risk a second, subtly different implementation drifting out of sync with
    this one. This function's own job is just the two things layered on top:
    lowercase for case-insensitive emptiness checking, and raising when
    nothing real is left.
    """
    heading_lines = [h.lower() for h in _real_heading_lines(markdown_text) if h]

    if not heading_lines:
        raise SummarizeError("digest output has no real '## ' heading line")


# format_recent_coverage's routing-label skip: "## Needs attention" is not a
# story, it's the prompt's own routing mechanism for "this needs the
# reader's action" (see prompts/digest.md's "Needs attention" section) --
# every digest that has anything urgent gets one, so treating it as "already
# covered" would suppress a FUTURE window's own Needs attention section just
# because a past one happened to exist, for a completely unrelated reason.
# Compared case-insensitively against _real_heading_lines' output, which
# preserves original case.
_NEEDS_ATTENTION_HEADING = "needs attention"

# format_recent_coverage caps the number of "recently covered" lines it will
# ever render, regardless of how many digests or headings are available.
# This is a hard ceiling on how much of the prompt budget the coverage block
# can consume: at 24h of history and an 8-section-ish briefing every 3 hours,
# a healthy run produces on the order of 8 runs * ~8 headings = ~64 candidate
# lines even before the "Needs attention" skip -- close enough to this cap
# that an unusually busy day (or a pathological run that somehow emits far
# more headings than the prompt's own ~8-section budget asks for) could
# otherwise make this block grow open-endedly every single run. 50 keeps the
# coverage block bounded and cheap relative to _MAX_PROMPT_BYTES's other
# consumers (items, collector status) without needing to special-case why a
# particular day's history was unusually large.
_MAX_RECENT_COVERAGE_LINES = 50

# format_recent_coverage truncates any single sanitized heading longer than
# this many characters. A real `## ` heading (prompts/digest.md's own
# contract) is a short topic label ("Missile strike in Poland", "ASI
# Alliance: token migration questions") -- normal headings are nowhere near
# this length. This exists purely to bound a pathological or hostile model
# output (e.g. a heading that somehow ballooned to paragraph length) so one
# bad past digest can't blow up this run's prompt budget on its own; it is
# not expected to ever trigger on a well-formed heading.
_MAX_RECENT_COVERAGE_HEADING_CHARS = 160

_NO_RECENT_COVERAGE = "(no prior briefings in the last 24 hours)"


def _sanitize_recent_coverage_heading(heading: str) -> str:
    """Neutralize a past heading's three hazards before it is rendered into this run's prompt.

    `heading` came out of _real_heading_lines applied to a PAST digest's
    body_md -- text the model itself generated, but generated FROM the same
    untrusted, scraped Telegram/X/news material this run's items come from
    (see format_recent_coverage's docstring for the full threat model). Three
    specific things are neutralized here, each for a distinct reason:

    1. Backticks are stripped entirely. The recent-coverage block is
       embedded in the prompt inside a fenced ```text block (see
       prompts/digest.md's "Recently covered" section) exactly like the
       items JSON is embedded in a fenced ```json block -- and exactly the
       same hazard build_prompt's backtick-escaping of item text defends
       against applies here: a literal ``` sequence surviving into a past
       heading could make the model perceive the fence as closed early,
       exposing whatever coverage lines follow (or the prompt text after
       them) as if they were outside the "this is data" boundary rather than
       inside it. Removing every backtick (rather than escaping it, the way
       build_prompt does for JSON-embedded text) is enough here because,
       unlike the JSON payload, this text is never parsed back out of the
       fence programmatically -- it only has to read sensibly as plain
       prose, and a topic heading missing a backtick reads identically to a
       reader either way.
    2. Every "{{" is broken into "{ {" (a literal space inserted between the
       braces). RECENT_COVERAGE is substituted into the prompt template via
       str.replace BEFORE {{ITEMS_JSON}} (see build_prompt's ordering
       comment) specifically so that this text is never itself rescanned by
       a LATER .replace() call -- but that ordering rule alone only protects
       against str.replace's own rescanning behavior. This sanitization is
       the complementary, second layer: even if some future refactor changed
       that ordering, or another consumer read this rendered coverage block
       and ran its own placeholder substitution over it, a real
       "{{COLLECTOR_STATUS}}" or "{{ITEMS_JSON}}" token could never have
       survived into the string in the first place, because any "{{" was
       already broken before this function returns.
    3. The heading is truncated to _MAX_RECENT_COVERAGE_HEADING_CHARS. This
       bounds one pathological or hostile heading's contribution to this
       run's prompt size -- see that constant's own comment for why a real
       heading is never expected to be anywhere near this long.

    Returns the sanitized heading with leading/trailing whitespace stripped
    (truncation, in particular, can leave trailing whitespace at the cut
    point). Order matters: backticks are removed and "{{" is broken BEFORE
    truncating, so the final length bound applies to the text that actually
    reaches the prompt, not to a pre-sanitization length that sanitization
    would then shrink further.
    """
    sanitized = heading.replace("`", "")
    # Lookahead, not a plain replace("{{", "{ {"): a plain replace is a
    # single non-overlapping left-to-right pass, so a brace RUN of three or
    # more defeats it -- "{{{COLLECTOR_STATUS}}}" becomes
    # "{ {{COLLECTOR_STATUS}}}", which still contains the live
    # "{{COLLECTOR_STATUS}}" placeholder (the pass consumed the first two
    # braces and never re-examined the pair formed by the second and third).
    # The lookahead consumes only the FIRST brace of each adjacent pair, so
    # every pair in a run of any length gets a space inserted in one pass:
    # "{{{" -> "{ { {". No brace adjacency can survive, of any run length.
    sanitized = re.sub(r"\{(?=\{)", "{ ", sanitized)
    if len(sanitized) > _MAX_RECENT_COVERAGE_HEADING_CHARS:
        sanitized = sanitized[:_MAX_RECENT_COVERAGE_HEADING_CHARS]
    return sanitized.strip()


def _format_digest_age(created_at: str, now: datetime) -> str:
    """Render `created_at` (ISO8601 UTC) as a whole-hour age relative to `now`, e.g. "3h ago".

    Floors to whole hours (via integer division of the elapsed seconds) --
    the reader-facing "recently covered" list only needs a coarse sense of
    how stale a story is ("this was covered a few hours ago" vs "just now"),
    not minute-level precision. Ages under one full hour render as "<1h ago"
    rather than "0h ago", since "0h ago" reads as if no time at all has
    passed, which is misleading for e.g. a digest created 55 minutes ago.
    """
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        # Every created_at this codebase writes is timezone-aware UTC
        # (datetime.now(UTC).isoformat(), see state.py's create_digest) --
        # this fallback only guards a hypothetically naive timestamp (e.g.
        # from a hand-edited test fixture or a pre-migration row) so
        # subtraction against an aware `now` doesn't raise TypeError.
        created = created.replace(tzinfo=UTC)
    age_hours = int((now - created).total_seconds() // 3600)
    if age_hours < 1:
        return "<1h ago"
    return f"{age_hours}h ago"


def format_recent_coverage(digests: list[tuple[str, str]], now: datetime) -> str:
    """Render the last 24h of prior digests' headings into the {{RECENT_COVERAGE}} prompt block.

    This is the "running story memory" feature: without it, the summarizer
    has zero awareness of what a previous digest already told the reader,
    and re-explains the same story in full every 3 hours. `digests` is the
    output of digest/state.py's get_recent_digests -- (created_at, body_md)
    pairs for every digest created in roughly the last 24 hours, newest
    first, INCLUDING unsent ones (see that function's docstring for why
    email_sent is deliberately ignored). This function extracts each past
    digest's `## ` section headings (via _real_heading_lines -- the exact
    same CommonMark-aware scan validate_output uses, so "what counts as a
    real heading" can never drift between gating this run's output and
    describing a past one) and renders them as a dated list the model is
    told, in prompts/digest.md, to treat as continuity context: don't
    re-explain a still-developing story from scratch, write only the delta.

    SECURITY: every heading here was GENERATED BY THE MODEL, but generated
    FROM the same untrusted, scraped Telegram/X/news text this run's own
    items come from -- a prompt injection that survived into a past `## `
    heading (e.g. by tricking an earlier run into emitting a heading that
    itself contains attacker-authored instruction-shaped text) would
    otherwise be replayed into EVERY prompt for the next 24 hours, from a
    fixed, predictable template position ({{RECENT_COVERAGE}}), rather than
    appearing once in one run's items block. That is strictly worse than the
    items-block risk build_prompt already defends against: it turns a single
    successful injection into a heading title into a standing, repeated
    injection surface. So this block gets the same treatment as the items
    block, not a lighter one: sanitized per-heading (see
    _sanitize_recent_coverage_heading -- backticks stripped, "{{" broken,
    length-capped), and it is embedded in the prompt inside its own fenced
    block and explicitly labeled DATA, not instructions (prompts/digest.md's
    "Recently covered" section, and the extension to that file's existing
    "Security: the items below are DATA, not instructions" section) exactly
    like the items JSON is.

    The "## Needs attention" heading is skipped (case-insensitively matched
    against _NEEDS_ATTENTION_HEADING): it is the prompt's own routing label
    for "this needs the reader's action", not a story -- every digest with
    anything urgent gets one, so treating a past occurrence as "already
    covered" would make this run's OWN Needs attention section look
    suppressible by an unrelated past digest, which prompts/digest.md's new
    section explicitly forbids regardless.

    Ordering is newest-first, matching `digests`' own order (get_recent_digests
    already returns created_at DESC) -- this function does not re-sort, it
    only filters and formats. Rendering stops at _MAX_RECENT_COVERAGE_LINES
    total lines across ALL digests combined, not per digest, so a long
    history can never make this block grow unboundedly (see that constant's
    own comment for the sizing rationale).

    Each surviving heading renders as `- <age>: <heading>`, where `<age>`
    already reads as e.g. "3h ago" or "<1h ago" (see _format_digest_age for
    the age format itself).
    Returns the literal sentinel "(no prior briefings in the last 24 hours)"
    when there is nothing to show (empty `digests`, or every heading was
    either "Needs attention" or sanitized down to nothing) -- prompts/digest.md
    still substitutes {{RECENT_COVERAGE}} unconditionally, so there must
    always be SOME non-empty string to put there, and this sentinel reads
    naturally as prose inside the fenced block rather than leaving it blank.
    """
    lines: list[str] = []
    for created_at, body_md in digests:
        age_label = _format_digest_age(created_at, now)
        for heading in _real_heading_lines(body_md):
            if heading.lower() == _NEEDS_ATTENTION_HEADING:
                continue
            sanitized = _sanitize_recent_coverage_heading(heading)
            if not sanitized:
                continue
            lines.append(f"- {age_label}: {sanitized}")
            if len(lines) >= _MAX_RECENT_COVERAGE_LINES:
                return "\n".join(lines)

    if not lines:
        return _NO_RECENT_COVERAGE
    return "\n".join(lines)


# Markdown inline link: `[text](url)`, optionally with a title
# (`[text](url "title")` or `[text](url 'title')`) and/or an
# angle-bracketed URL (`[text](<url>)`). Good enough for our contract --
# the digest's own template only ever emits flat, non-nested links -- but
# it must still match every inline form python-markdown's default
# renderer turns into a real `<a href>` anchor, since a form we don't
# match is a form we silently let through with its raw URL intact.
_MARKDOWN_LINK_RE = re.compile(
    r"\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+(\"[^\"]*\"|'[^']*'))?\s*\)"
)
# CommonMark autolink form: `<scheme:destination>` -- a bare URI wrapped in
# angle brackets, with no link text of its own. Per the CommonMark grammar
# an autolink's scheme is not limited to http/https: any URI scheme works
# (`<mailto:attacker@example.com>`, `<ftp://evil.example/x>`, etc.), and mail
# clients linkify those exactly as readily as an http(s) autolink -- so this
# must match ANY scheme, not just http/https, or a non-HTTP autolink slips
# through this pass untouched. URI schemes are themselves case-insensitive
# (RFC 3986) and mail clients linkify `<HTTP://...>` / `<MAILTO:...>` just as
# readily as their lowercase forms, so scheme matching is case-insensitive
# too -- otherwise an uppercase-scheme autolink would skip this pass entirely
# and never be recognized as an autolink to defang.
_AUTOLINK_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9+.\-]*:[^\s<>]*)>", re.IGNORECASE)
# Reference-style link definition line, e.g. `[id]: https://example.com`
# (optionally indented up to 3 spaces, per CommonMark). This does not match
# the *usage* site (`[text][id]`) -- only the definition. Deliberately
# permissive about what follows the URL (title, trailing text) since the
# only decision made here is whether the URL is allowlisted.
_REFERENCE_DEFINITION_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(\S+).*$", re.MULTILINE)
# Any remaining bare URI token, run as the final pass over the whole text --
# catches URIs mail clients would auto-linkify even though they never went
# through a markdown link construct at all (plain prose, or what survives
# after the passes above run). Two alternatives:
#
# 1. Any `scheme://...` token (not just http/https -- `ftp://evil.example/x`
#    is exactly as linkifiable to a mail client as an http(s) URL and must
#    not survive verbatim).
# 2. Any OTHER scheme-shaped bare token, generically -- `[a-zA-Z][\w+.-]*:`
#    followed by at least two non-whitespace, non-colon payload characters.
#    This is deliberately NOT a denylist of specific non-`//` schemes
#    (`tel:`, `sms:`, `geo:`, `mailto:`, ...) -- a mail/messaging client's
#    set of auto-linkified schemes is neither fixed nor fully known to this
#    codebase, and chasing it one scheme at a time is the same losing game
#    as chasing grammar variants. Going generic on the *shape* of a URI
#    (RFC 3986: `scheme ":" ...`) instead catches any bare
#    `tel:+19005551234`, `sms:+1...`, `geo:...`, or `mailto:...` token
#    without needing to know its name in advance. The payload requires >= 2
#    chars so a lone trailing colon (or a colon immediately followed by
#    another colon or whitespace) can't match; excluding `:` from the
#    payload itself means a colon-separated non-URI token like "12:30" is
#    never mistaken for `scheme:payload` in the first place (`12` isn't
#    letter-led, so it never even reaches this alternative), and a prose
#    colon like "Deadline: tomorrow" doesn't match either, since the
#    payload class demands the two-plus chars sit immediately after the
#    colon with no gap, and a space is not a payload character.
#
#    The `(?!//)` right after this alternative's `:` is the load-bearing
#    guard against corrupting the FIRST alternative's job: an actual
#    `scheme://...` token (allowlisted or not) also matches the generic
#    `scheme:` shape up to its colon, so without this guard the two
#    alternatives would race for the same text. Since Python's `re` tries
#    alternatives left-to-right at each position and stops at the first
#    that matches, alternative 1 already wins that race for any `://` token
#    -- this guard on alternative 2 is defense in depth, and it doubles as
#    the fix for a second, more concrete hazard: it stops this alternative
#    from re-matching output the http/https branch of _defang already
#    produced. That branch defangs by renaming the scheme in place
#    (`https://` -> `hxxps://`) rather than breaking the `:` separator the
#    way the generic-scheme defang does (`ftp://` -> `ftp[:]//`), so
#    `hxxps://...` still looks exactly like `scheme://...` -- letters
#    directly followed by `://` -- and would otherwise match this
#    alternative too (`hxxps` reads as a fine scheme name) and get mangled
#    a SECOND time into `hxxps[:]//...`. `(?!//)` excludes it outright: the
#    colon in `hxxps:` IS followed by `//`, so this alternative never even
#    starts. (A generically-defanged token doesn't need a matching guard
#    here: `ftp[:]//host` has no scheme immediately followed by `:` at all
#    -- the `[` breaks that adjacency -- so neither alternative can match it
#    a second time.)
#
# Both alternatives stop at whitespace and the same closing delimiters the
# other patterns exclude (`)`, `]`, `>`, quotes) so neither swallows
# trailing punctuation from an enclosing markdown/HTML construct. URI
# schemes are case-insensitive (RFC 3986) and mail clients linkify
# `HTTPS://...` / `TEL:...` exactly as readily as their lowercase forms, so
# this must match regardless of scheme case -- a lowercase-only pattern
# lets an uppercase- or mixed-case-scheme URI sail through this final pass
# untouched.
#
# The `\b(?!hxxps?://)` guard on the scheme:// alternative exists for the
# same reason as alternative 2's `(?!//)` guard above: it stops that
# alternative from re-matching `hxxps://...`/`hxxp://...` output the
# http/https branch of _defang already produced, corrupting output that
# was already handled correctly. `hxxp`/`hxxps` are never a real scheme
# this codebase collects or allowlists, so excluding them here is safe. The
# leading `\b` is load-bearing, not decorative: a negative lookahead only
# blocks a match from STARTING at that exact position -- without `\b`, the
# regex engine simply retries one character to the right ("xxps://...",
# still letters followed by "://") and matches that shifted substring
# instead, leaving the leading "h" untouched and producing "hxxps[:]//..."
# anyway. Requiring a word boundary right before the scheme means the only
# position "hxxps://" could ever start a match is at its own "h" -- which
# the lookahead already excludes -- so no shifted, one-character-short
# match is possible either.
_BARE_URL_RE = re.compile(
    r"\b(?!hxxps?://)[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s)\]>\"']+"
    # The generic non-// branch's payload is intentionally broad (any run of
    # 2+ non-delimiter chars) so real delimiter-led URIs still match:
    # `mailto:?to=x@y.z` (payload starts with `?`), `tel:*67` (a real
    # vertical-service-code URI, payload starts with `*`), `geo:47.5,...`,
    # `mailto:__attacker@example.com` (payload starts with `__`), etc. An
    # earlier, tighter version of this branch restricted the FIRST payload
    # char to a URI-plausible class (letter/digit/+/~/%/_), which broke
    # exactly those delimiter-led forms (`?`/`*` aren't in that class) — a P2
    # regression.
    #
    # A later attempt fixed that by excluding payloads that merely START with
    # `**`/`__` (a regex lookahead can only anchor at the match's start
    # position), to stop the digest's own mandated "**TL;DR:**" opener from
    # being misread as `scheme=DR, payload=** ...` and mangled into
    # "TL;DR[:]**". But a lookahead anchored at the start can't see the
    # REST of the payload either -- so `(?!\*\*|__)` excluded not just that
    # emphasis artifact but also every legitimate URI whose payload happens
    # to start with `**`/`__`, e.g. the bare autolink-shaped
    # `mailto:__attacker@example.com`, which would then never match this
    # regex at all and survive linkifiable in the text/plain part.
    #
    # The real discriminator is a property of the WHOLE payload, not just
    # its first two characters: is the payload's entire content markdown
    # emphasis punctuation (only `*`/`_`) and nothing else? That can't be
    # expressed as a regex lookahead anchored at the match start, so this
    # branch stays maximally broad here and the actual whole-payload check
    # happens in code, in _replace_bare_url below, once the full match
    # (and thus the full payload) is known.
    # No trailing lookahead here (an earlier revision had one to stop this
    # branch biting into a following URL's scheme): nested/adjacent cases —
    # `**TL;DR:**https://...` (emphasis-glued) and `custom:abchttps://...`
    # (URI whose payload tail is itself scheme-shaped) — are instead handled
    # by running the whole substitution TO FIXPOINT in enforce_link_allowlist:
    # pass 1 defangs the outer colon, which exposes the inner `scheme://`
    # token for pass 2. A single-pass lookahead can protect only one of the
    # two colons, whichever way it is written.
    r"|\b[a-zA-Z][a-zA-Z0-9+.\-]*:(?!//)[^\s:)\]>\"']{2,}",
    re.IGNORECASE,
)


def _defang(url: str) -> str:
    """Break a URI's scheme separator so mail clients won't auto-linkify it.

    ``https://`` -> ``hxxps://``, ``http://`` -> ``hxxp://`` -- the standard
    security-community defanging convention, kept for exactly those two
    schemes since every URL this codebase ever collects or allowlists is
    https (see enforce_link_allowlist's allowed_urls), so this is the only
    pair of schemes where the familiar hxxp/hxxps convention actually
    applies. This only rewrites the scheme prefix, so the destination stays
    human-readable (useful for the "someone sent a suspicious link" summary
    case) while no longer being a live, clickable URL to any client that
    recognizes the scheme.

    Any OTHER scheme (``ftp://``, ``mailto:``, ``tel:``, ``sms:``, ``geo:``,
    a custom scheme, etc. -- deliberately not enumerated; see _BARE_URL_RE's
    comment for why a denylist of specific schemes is the wrong shape here)
    is defanged generically by breaking the ``:`` scheme separator into
    ``[:]`` instead of renaming the scheme: ``ftp://host/path`` ->
    ``ftp[:]//host/path``, ``mailto:user@host`` -> ``mailto[:]user@host``,
    ``tel:+19005551234`` -> ``tel[:]+19005551234``. This also covers a
    scheme with no ``//`` at all (``mailto:``, ``tel:``, ``sms:``, ...) --
    there's nothing special-cased about the *absence* of ``//`` here, since
    breaking the ``:`` separator kills linkification whether or not a
    ``//`` follows it. Breaking the separator, rather than renaming the
    scheme the way hxxp/hxxps do, is what kills linkification for an
    arbitrary scheme -- there is no equivalent "familiar renamed scheme"
    convention for schemes other than http/https, and renaming an arbitrary
    scheme risks coincidentally landing on another scheme a mail client
    DOES recognize.

    The scheme is detected case-insensitively throughout -- URI schemes are
    case-insensitive per RFC 3986, and mail clients linkify `HTTPS://`,
    `MAILTO:`, or `FTP://` exactly as readily as their lowercase forms -- so
    a lowercase-only check here would leave an uppercase- or mixed-case
    scheme live and clickable. Only the prefix/separator is touched; the
    remainder of the URL (including its original case) is preserved exactly.
    """
    if url[:8].lower() == "https://":
        return "hxxps://" + url[8:]
    if url[:7].lower() == "http://":
        return "hxxp://" + url[7:]
    scheme_end = url.find(":")
    if scheme_end != -1:
        return url[:scheme_end] + "[:]" + url[scheme_end + 1 :]
    return url


def enforce_link_allowlist(markdown_text: str, allowed_urls: Collection[str]) -> str:
    """Strip any link whose URL is not an exact member of ``allowed_urls``.

    The digest's contract is that every link in the output comes verbatim
    from a collected Item.url: build_prompt only ever shows Claude those
    URLs, and the prompt never asks it to invent new ones. But Claude can
    still hallucinate a link, or be induced by a prompt injection in the
    scraped source text to emit one, e.g.
    `[read more](https://attacker.example/phish)`. digest/emailer.py's
    nh3.clean only checks the URL *scheme* (http/https) -- a hostile but
    otherwise valid https URL sails straight through that layer untouched.
    This function is the layer that checks link *provenance* instead of
    just syntax, closing that gap.

    Three distinct markdown forms python-markdown's default renderer turns
    into a real `<a href>` anchor are covered here, all against the same
    allowlist:

    1. Inline links, `[text](url)`, including the variations the CommonMark
       grammar allows within the parens: an optional title in either quote
       style (`[text](url "title")` / `[text](url 'title')`), an optional
       angle-bracketed URL (`[text](<url>)`), and flexible whitespace around
       the URL. An unknown URL is repaired to plain text (`text`); a title,
       if present, is dropped along with the parens -- the title is not
       rendered as visible text by python-markdown either way, so dropping
       it loses nothing a reader would see.
    2. Reference-style links, `[text][id]`, defined elsewhere by a separate
       `[id]: url` definition line. The two-part syntax means the URL never
       appears at the `[text][id]` usage site at all -- there is nothing to
       repair there. Instead, this strips the *definition* line: if its URL
       is not allowlisted, the whole line is deleted. python-markdown then
       has no definition for `id`, so every `[text][id]` referencing it
       renders as literal bracket text, not an anchor -- the same "becomes
       plain text" outcome as the inline case, just achieved by removing the
       definition instead of rewriting each usage site (which may be
       numerous, or precede the definition in document order).
    3. CommonMark autolinks, `<scheme:destination>` -- a bare URI with no
       link text of its own, of ANY URI scheme (not just http/https --
       `<mailto:attacker@example.com>`, `<ftp://evil.example/x>`, etc. are
       all valid CommonMark autolinks a mail client will linkify just as
       readily). Unlike the previous implementation, an unknown autolink's
       `<>` wrapper is stripped *and* its URI is defanged (see below) rather
       than surviving as bare text.

    Finally, a fourth pass runs over the *entire* result: every remaining
    bare URI token -- any `scheme://...` token (not just http/https) plus
    any other bare scheme-shaped token (`mailto:...`, `tel:...`, `sms:...`,
    `geo:...`, and any other scheme a mail/messaging client might
    auto-linkify, whether already bare in the model's prose or left behind
    by the passes above) -- is defanged unless it is an exact member of
    ``allowed_urls``. This pass must run last, after the markdown-link and
    reference-definition passes, so that an allowed URL still embedded in a
    surviving markdown link -- which is necessarily also in
    ``allowed_urls`` -- is spared by the membership check rather than
    mangled.

    Defanging exists because this function's output is used verbatim as the
    digest email's text/plain MIME part (see send_digest in
    digest/emailer.py), and mail clients auto-linkify bare URIs of ANY
    scheme in plain text on their own -- there is no HTML anchor layer in
    that part for allowlist enforcement to intercept, and no scheme
    allowlist either (unlike nh3.clean's http/https-only HTML anchor check
    on the rendered part -- see above). Repairing a markdown link construct
    (dropping it to plain text, or deleting the reference definition) is
    enough to stop python-markdown from turning it into an `<a href>` in the
    HTML part, but it does nothing for the plain-text part: the URI text
    itself is still there, and `<mailto:attacker@example.com>`,
    `<https://attacker.example/phish>`, a bare `ftp://evil.example/x`, or a
    bare `https://attacker.example/...` in prose is exactly as clickable to
    a mail client as a real link. Only defanging the scheme separator (not
    just deleting the URI) closes that gap while keeping the destination
    readable. `https://` -> `hxxps://` and `http://` -> `hxxp://` follow the
    familiar security-community convention; every other scheme (including
    `mailto:`, `tel:`, `sms:`, `geo:`, and anything else letter-scheme-
    shaped) is defanged by breaking its `:` separator into `[:]` instead --
    see _defang's docstring for why that's the right generalization, and
    _BARE_URL_RE's comment for why matching is done by URI *shape* rather
    than by enumerating specific non-`//` schemes.

    A deliberate false-positive trade-off applies to this generic bare-
    scheme alternative: a stray prose token that happens to look
    scheme-shaped (e.g. some hypothetical "Deadline:tomorrow" written with
    no space) would get cosmetically mangled into "Deadline[:]tomorrow" --
    harmless, if odd to read. The alternative failure mode -- a genuinely
    linkifiable URI slipping through this pass untouched -- is a real
    provenance bypass, exactly the class of bug this function exists to
    close. In a personal digest, that asymmetry favors defanging a few
    stray prose tokens over ever missing a live URI.

    This repairs rather than rejects: the digest still goes out with
    unknown links neutralized, rather than raising and discarding the
    whole run's output. A hard validation failure here (the way
    validate_output raises on a missing section) would risk looping
    forever if the model keeps emitting a bad link on every retry --
    section headings are a static template the model can plausibly get
    right on a rerun, but there's no reason to expect a rerun would stop
    hallucinating a URL. Known links (and known reference definitions) are
    left completely untouched.

    Only the COUNTS of stripped/defanged links are logged, never the URLs
    themselves: an attacker-chosen URL reaching the log (and Loki) is itself
    exposure -- e.g. an SSRF probe or a tracking domain encoded in the query
    string.
    """
    allowed = set(allowed_urls)
    stripped = 0
    defanged = 0

    def _replace_inline_link(match: re.Match[str]) -> str:
        nonlocal stripped
        text, url = match.group(1), match.group(2)
        if url in allowed:
            return match.group(0)
        stripped += 1
        return text

    result = _MARKDOWN_LINK_RE.sub(_replace_inline_link, markdown_text)

    def _replace_autolink(match: re.Match[str]) -> str:
        nonlocal defanged
        url = match.group(1)
        if url in allowed:
            return match.group(0)
        defanged += 1
        return _defang(url)

    result = _AUTOLINK_RE.sub(_replace_autolink, result)

    def _replace_reference_definition(match: re.Match[str]) -> str:
        nonlocal stripped
        url = match.group(1).strip("<>")
        if url in allowed:
            return match.group(0)
        stripped += 1
        return ""

    result = _REFERENCE_DEFINITION_RE.sub(_replace_reference_definition, result)

    # Final pass: defang every remaining bare URL not in the allowlist. Runs
    # last so allowed URLs still sitting inside a surviving markdown link
    # (necessarily in `allowed`) are spared by the membership check instead
    # of being mangled by this text-wide regex.
    def _replace_bare_url(match: re.Match[str]) -> str:
        nonlocal defanged
        url = match.group(0)
        if url in allowed:
            return url
        # Markdown-emphasis discrimination lives here, in code, rather than
        # in _BARE_URL_RE's regex: a lookahead can only anchor at the
        # match's START position, so it can rule out a payload that
        # *starts* with `**`/`__` but not one whose whole payload consists
        # of nothing else -- and the two are different sets (see
        # _BARE_URL_RE's comment). `scheme:payload` where `payload` is
        # composed entirely of `*`/`_` characters is markdown emphasis
        # punctuation the model emitted right after a colon (e.g. the
        # `DR:**` token inside "**TL;DR:** text"), not a URI, and must be
        # left untouched. Only the non-`//` form needs this check -- a
        # `scheme://...` match's payload starts with `//`, which is never
        # all `*`/`_`, so it can't accidentally trip this.
        scheme_end = url.find(":")
        if scheme_end != -1 and not url[scheme_end + 1 :].startswith("//"):
            payload = url[scheme_end + 1 :]
            if payload and all(ch in "*_" for ch in payload):
                return url
        defanged += 1
        return _defang(url)

    # Run the bare-URL pass TO FIXPOINT (bounded): a nested construct like
    # `custom:abchttps://attacker.example/x` or emphasis-glued
    # `**TL;DR:**https://...` needs one pass to break the OUTER colon and a
    # second to defang the inner `scheme://` token it exposes. Defanged
    # output never rematches (idempotence is tested), so the loop terminates
    # in practice after <=2 passes; the bound is a pure safety rail.
    for _ in range(5):
        next_result = _BARE_URL_RE.sub(_replace_bare_url, result)
        if next_result == result:
            break
        result = next_result

    if stripped or defanged:
        logger.warning(
            "enforce_link_allowlist: stripped %d link(s) and defanged %d bare "
            "URL(s) with non-allowlisted URLs",
            stripped,
            defanged,
        )

    return result


def summarize(
    items: list[Item],
    failed_sources: list[str],
    recent_coverage: str,
    model: str,
    timeout_seconds: int,
    effort: str,
) -> str:
    """Build the prompt, run it through Claude, validate and repair the
    contract, and deterministically prepend the collector-failure banner.

    `recent_coverage` is passed straight through to build_prompt (see that
    function's docstring for the substitution-ordering hazard it addresses,
    and format_recent_coverage's own docstring, above, for how this string
    is produced and why it must already be sanitized by the time it reaches
    here).

    `effort` is threaded straight through to run_claude's `--effort` flag
    (see that function's docstring for why it's set explicitly and why
    `high`, Config.claude_effort's default, rather than `max`).

    Never call with an empty item list. Raises SummarizeError (via
    run_claude or validate_output) rather than returning malformed output,
    so the caller never persists a digest for content that failed the
    output contract.

    The output is now a free-form prose BRIEFING (prompts/digest.md), not
    the old fixed three-section list. validate_output only enforces the one
    structural property a legitimate briefing can never fail to have (at
    least one real `## ` heading) -- everything else about quality is a
    SOFT check here: logged if missed, never raised, because the model's
    exact wording legitimately varies run to run and hard-gating wording
    against a varying model is what caused the old three-heading contract's
    retry loops. Two soft checks run on the raw model output before link
    repair: a missing `**TL;DR:` opener (degrades one email cosmetically),
    and zero citation links (a window of pure chatter can legitimately cite
    nothing, so this must never raise).

    enforce_link_allowlist runs after validate_output and before the banner:
    every link in the model's output is checked against the URLs of the
    items it was actually given, and any link that doesn't match one
    verbatim is deterministically stripped down to plain text rather than
    failing the whole run (see that function's docstring for why repair,
    not rejection, is the right response here).

    The `⚠ <source> collection failed this run` banner is generated here,
    in code, rather than asked of the model: a live test against real Opus
    showed the model omits the banner even when the prompt explicitly and
    emphatically instructs it to write one. Whether a partial-collection
    run is flagged to the reader is deterministic system state -- it must
    never depend on model compliance. One banner line is emitted per
    failed source, in the given order, followed by a blank line, then the
    (validated) model output unchanged.
    """
    prompt = build_prompt(items, failed_sources, recent_coverage)
    output = run_claude(prompt, model, timeout_seconds, effort)
    validate_output(output)
    # The TL;DR opener is checked SOFTLY, unlike the heading requirement: a
    # missing TL;DR degrades one email cosmetically, while raising here
    # would hold every collected item hostage for a full scheduling cycle
    # over a nicety (the banner saga proved hard-gating model compliance
    # loops when the model persistently misbehaves). The one structural
    # failure (no real heading at all) stays hard; quality misses log and
    # ship.
    if not output.lstrip().startswith("**TL;DR:"):
        logger.warning("digest output missing the TL;DR opener — sending anyway")
    # Same soft-check reasoning for citations: a window that was pure
    # chatter can legitimately produce zero `[text](url)` links (nothing met
    # the citation bar), and that is correct output, not a bug -- so this
    # only logs, checked on the raw model output before enforce_link_allowlist
    # potentially strips any non-allowlisted link down to plain text below.
    if not _MARKDOWN_LINK_RE.search(output):
        logger.warning("digest output contains no citation links — sending anyway")
    output = enforce_link_allowlist(output, allowed_urls={item.url for item in items})
    if failed_sources:
        banner = "".join(
            f"⚠ {source} collection failed this run\n" for source in failed_sources
        )
        return banner + "\n" + output
    return output
