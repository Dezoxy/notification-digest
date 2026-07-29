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
from pathlib import Path

from digest.config import claude_subprocess_env
from digest.state import Item

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "digest.md"

# The three section headings the output contract in prompts/digest.md
# requires, always, in this relative order, lowercased for case-insensitive
# matching. These are the heading TEXT only (no "## " prefix) -- see
# validate_output, which parses actual heading lines rather than substrings.
_REQUIRED_HEADINGS = ("needs attention", "worth knowing", "noise skipped")

# Bounds each item's text as it's embedded in the prompt payload. Telegram
# messages can reach 4096 chars; at up to _MAX_ITEMS_PER_DIGEST (200) items
# per run (digest/main.py), 200 x 2000 chars keeps the prompt comfortably
# inside the model's context window. Truncation happens only in the prompt
# payload built here -- the stored item text in the database stays
# full-length, untouched.
_MAX_ITEM_TEXT_CHARS = 2000
_TRUNCATION_MARKER = " …[truncated]"

# Bounds the BUILT prompt's serialized character length, checked by
# select_items_for_prompt. ~150k tokens at the usual ~4 chars/token -- well
# inside the model's context window while leaving ample room for the
# response (the digest markdown output). Per-item text is already capped at
# _MAX_ITEM_TEXT_CHARS, but _MAX_ITEMS_PER_DIGEST (digest/main.py) items of
# emoji/CJK-heavy source text can still serialize far larger than a naive
# sum of text lengths would suggest -- see select_items_for_prompt's
# docstring for why.
_MAX_PROMPT_CHARS = 600_000


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


def build_prompt(items: list[Item], failed_sources: list[str]) -> str:
    """Load prompts/digest.md and substitute the items JSON and status placeholders."""
    template = _PROMPT_PATH.read_text()

    payload = [
        {
            "source": item.source,
            "chat_id": item.chat_id,
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

    # Substitute {{COLLECTOR_STATUS}} before {{ITEMS_JSON}}, and always
    # substitute {{ITEMS_JSON}} last: str.replace scans its input left to
    # right looking for the placeholder, and that scan does not distinguish
    # template text from text just inserted by an earlier .replace() call.
    # If ITEMS_JSON went first and an item's text happened to contain the
    # literal "{{COLLECTOR_STATUS}}", the second .replace() would find that
    # match INSIDE the just-inserted JSON and silently rewrite collected
    # message content. Doing ITEMS_JSON last means no subsequent .replace()
    # ever rescans data it inserted.
    return template.replace("{{COLLECTOR_STATUS}}", collector_status).replace(
        "{{ITEMS_JSON}}", items_json
    )


def select_items_for_prompt(
    items: list[Item], failed_sources: list[str], max_prompt_chars: int
) -> list[Item]:
    """Return the longest OLDEST-first prefix of `items` whose built prompt fits.

    The bound is checked against the BUILT prompt (via build_prompt on the
    candidate prefix), not the sum of the items' text lengths. Two things
    inflate the built prompt well beyond what a naive character sum would
    suggest: json.dumps with ensure_ascii=False still expands JSON's own
    structural characters and per-item key names (~40+ chars of
    "source"/"chat_id"/"author"/"url"/"fetched_at" scaffolding per item on
    top of the text), and the surrounding prompts/digest.md template adds
    fixed overhead of its own. Summing raw text lengths would systematically
    underestimate the real prompt size and let an oversized batch through
    anyway -- the whole point of this function is to catch that gap by
    measuring the actual thing that gets handed to the model.

    `items` is assumed already ordered oldest-first (get_unsummarized_items
    returns it that way) -- this function preserves that order and never
    reorders or drops from the middle, it only shrinks from the end.

    Halves the candidate count on each miss (`n = max(1, n // 2)`) rather
    than decrementing one at a time: a single oversized item (or a batch
    saturated with multi-byte text) could otherwise take hundreds of
    build_prompt calls -- each one re-serializing the whole candidate slice
    -- to converge. Halving reaches a fit in O(log n) builds.

    Stops at a 1-item floor: build_prompt on a single item always fits,
    because per-item text is already truncated to _MAX_ITEM_TEXT_CHARS by
    build_prompt itself, bounding one item's contribution regardless of its
    original length. The loop still explicitly returns the 1-item prefix
    once n reaches 1, rather than trusting the size check to naturally pass,
    so a pathological template/overhead blowup can't spin this into an
    infinite loop.
    """
    if not items:
        return items

    n = len(items)
    while True:
        candidate = items[:n]
        prompt = build_prompt(candidate, failed_sources)
        if len(prompt) <= max_prompt_chars or n <= 1:
            return candidate
        n = max(1, n // 2)


def run_claude(prompt: str, model: str, timeout_seconds: int) -> str:
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
                ["claude", "-p", "--model", model, "--output-format", "text", "--tools", ""],
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
    like " \t## Needs attention" (one space, then a tab) reaches column 4
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


def validate_output(markdown_text: str) -> None:
    """Enforce that the digest markdown carries all three required section headings.

    `run_claude` only guarantees non-empty stdout — a refusal ("I can't help
    with that"), truncated prose, or output that silently drops a section
    would otherwise pass through untouched. If that garbage reaches
    `_deliver`, every item gets stamped with the digest id and, once SMTP
    succeeds, the digest row is final: those items are lost to
    summarization forever. This check must run before anything is
    persisted, and it must raise SummarizeError on failure so the caller
    skips writing a digest row entirely -- with no row recorded, the next
    run re-selects and re-summarizes the same items instead of treating
    them as already handled.

    A plain substring check (`heading in text.lower()`) is fooled by a
    refusal that merely *mentions* the headings inline -- e.g. "I cannot
    produce ## Needs attention, ## Worth knowing, or ## Noise skipped in
    this case." contains all three substrings without a single real
    heading line. So instead this parses actual heading LINES: a line
    whose stripped form starts with exactly "## " (two hashes -- "###  "
    subgroup headings are not h2 and are ignored). Each of the three
    required sections must appear exactly once among those heading lines,
    and in the fixed relative order given by _REQUIRED_HEADINGS. Unknown
    extra h2 headings are allowed.

    Only the heading names are checked, case-insensitively -- never the
    error message includes the offending output itself, since it may
    contain scraped Telegram/X message content (see SummarizeError).

    Links are deliberately NOT validated here: a window with nothing but
    noise legitimately produces zero `[text](url)` links in "Worth
    knowing", and that is correct output, not a contract violation.

    Fenced code blocks are also excluded from heading line detection: a
    refusal can legitimately quote the required heading text inside a
    fenced block (e.g. "here's the template you asked about:\n```
    \n## Needs attention\n...") and that is not a real section -- it is
    example text sitting inside a code fence. Per CommonMark, a fence can
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
    "    ## Needs attention") is therefore code content, not a real
    heading, and must not satisfy the contract.

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
            heading_lines.append(stripped[3:].strip().lower())

    missing = [heading for heading in _REQUIRED_HEADINGS if heading not in heading_lines]
    duplicated = [
        heading for heading in _REQUIRED_HEADINGS if heading_lines.count(heading) > 1
    ]
    if missing or duplicated:
        parts = []
        if missing:
            parts.append(f"missing required section(s): {', '.join(missing)}")
        if duplicated:
            parts.append(f"duplicated section(s): {', '.join(duplicated)}")
        raise SummarizeError("digest output " + "; ".join(parts))

    positions = [heading_lines.index(heading) for heading in _REQUIRED_HEADINGS]
    if positions != sorted(positions):
        raise SummarizeError("digest output sections out of order")


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
# CommonMark autolink form: `<https://example.com>` -- a bare URL wrapped in
# angle brackets, with no link text of its own. URI schemes are
# case-insensitive (RFC 3986) and mail clients linkify `<HTTP://...>` just as
# readily as `<http://...>`, so this must match regardless of scheme case --
# otherwise an uppercase-scheme autolink skips this pass entirely and is
# never recognized as an autolink to defang.
_AUTOLINK_RE = re.compile(r"<(https?://[^\s<>]+)>", re.IGNORECASE)
# Reference-style link definition line, e.g. `[id]: https://example.com`
# (optionally indented up to 3 spaces, per CommonMark). This does not match
# the *usage* site (`[text][id]`) -- only the definition. Deliberately
# permissive about what follows the URL (title, trailing text) since the
# only decision made here is whether the URL is allowlisted.
_REFERENCE_DEFINITION_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(\S+).*$", re.MULTILINE)
# Any remaining bare URL token, run as the final pass over the whole text --
# catches URLs mail clients would auto-linkify even though they never went
# through a markdown link construct at all (plain prose, or what survives
# after the passes above run). Stops at whitespace and the same closing
# delimiters the other patterns exclude (`)`, `]`, `>`, quotes) so it doesn't
# swallow trailing punctuation from an enclosing markdown/HTML construct. URI
# schemes are case-insensitive (RFC 3986) and mail clients linkify
# `HTTPS://...` exactly as readily as `https://...`, so this must match
# regardless of scheme case -- a lowercase-only pattern lets an uppercase- or
# mixed-case-scheme URL sail through this final pass untouched.
_BARE_URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)


def _defang(url: str) -> str:
    """Replace a URL's scheme so mail clients won't auto-linkify it.

    ``https://`` -> ``hxxps://``, ``http://`` -> ``hxxp://`` -- the standard
    security-community defanging convention. This only rewrites the scheme
    prefix, so the destination stays human-readable (useful for the "someone
    sent a suspicious link" summary case) while no longer being a live,
    clickable URL to any client that recognizes the scheme.

    The scheme is detected case-insensitively -- URI schemes are
    case-insensitive per RFC 3986, and mail clients linkify `HTTPS://` or
    `hTtPs://` exactly as readily as `https://` -- so a lowercase-only check
    here would leave an uppercase- or mixed-case-scheme URL live and
    clickable. The output scheme is always written lowercase
    (`hxxps://`/`hxxp://`); only the prefix is touched, so the remainder of
    the URL is preserved exactly, case included.
    """
    if url[:8].lower() == "https://":
        return "hxxps://" + url[8:]
    if url[:7].lower() == "http://":
        return "hxxp://" + url[7:]
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
    3. CommonMark autolinks, `<url>` -- a bare URL with no link text of its
       own. Unlike the previous implementation, an unknown autolink's `<>`
       wrapper is stripped *and* its URL is defanged (see below) rather than
       surviving as bare text.

    Finally, a fourth pass runs over the *entire* result: every remaining
    bare URL token (anything matching `https?://[^\\s)\\]>"']+`, whether it
    was already bare in the model's prose or is what's left after the passes
    above ran) is defanged unless it is an exact member of ``allowed_urls``.
    This pass must run last, after the markdown-link and reference-definition
    passes, so that an allowed URL still embedded in a surviving markdown
    link -- which is necessarily also in ``allowed_urls`` -- is spared by the
    membership check rather than mangled.

    Defanging (`https://` -> `hxxps://`, `http://` -> `hxxp://`) exists
    because this function's output is used verbatim as the digest email's
    text/plain MIME part (see send_digest in digest/emailer.py), and mail
    clients auto-linkify bare URLs in plain text on their own -- there is no
    HTML anchor layer in that part for allowlist enforcement to intercept.
    Repairing a markdown link construct (dropping it to plain text, or
    deleting the reference definition) is enough to stop python-markdown
    from turning it into an `<a href>` in the HTML part, but it does nothing
    for the plain-text part: the URL text itself is still there, and
    `<https://attacker.example/phish>` or a bare `https://attacker.example/...`
    in prose is exactly as clickable to a mail client as a real link. Only
    defanging the scheme (not just deleting the URL) closes that gap while
    keeping the destination readable.

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
        defanged += 1
        return _defang(url)

    result = _BARE_URL_RE.sub(_replace_bare_url, result)

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
    model: str,
    timeout_seconds: int,
) -> str:
    """Build the prompt, run it through Claude, validate and repair the
    contract, and deterministically prepend the collector-failure banner.

    Never call with an empty item list. Raises SummarizeError (via
    run_claude or validate_output) rather than returning malformed output,
    so the caller never persists a digest for content that failed the
    output contract.

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
    prompt = build_prompt(items, failed_sources)
    output = run_claude(prompt, model, timeout_seconds)
    validate_output(output)
    output = enforce_link_allowlist(output, allowed_urls={item.url for item in items})
    if failed_sources:
        banner = "".join(
            f"⚠ {source} collection failed this run\n" for source in failed_sources
        )
        return banner + "\n" + output
    return output
