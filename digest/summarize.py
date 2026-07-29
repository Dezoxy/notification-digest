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


class SummarizeError(Exception):
    """Raised when the Claude CLI fails to produce a usable digest.

    Messages must stay short and must never include the prompt (which
    contains scraped Telegram/X message content).
    """


def build_prompt(items: list[Item], failed_sources: list[str]) -> str:
    """Load prompts/digest.md and substitute the items JSON and status placeholders."""
    template = _PROMPT_PATH.read_text()

    payload = [
        {
            "source": item.source,
            "chat_id": item.chat_id,
            "author": item.author,
            "text": item.text,
            "url": item.url,
            "fetched_at": item.fetched_at,
        }
        for item in items
    ]
    items_json = json.dumps(payload, indent=2)
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
    fence delimiter) may be indented at most 3 spaces -- a line starting
    with a tab, or with 4 or more leading spaces, is an indented code block
    instead. A refusal that pads a template with 4-space indentation (e.g.
    "    ## Needs attention") is therefore code content, not a real
    heading, and must not satisfy the contract.
    """
    heading_lines = []
    in_fence = False
    fence_char = None
    fence_len = 0
    for line in markdown_text.splitlines():
        # CommonMark: 4+ leading spaces or a leading tab makes this an
        # indented code block -- neither a heading nor a fence delimiter
        # can start here, regardless of what follows the indentation.
        if line.startswith("\t") or line[:4] == "    ":
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


# Non-nested markdown inline link: `[text](url)`. Good enough for our
# contract -- the digest's own template only ever emits flat, non-nested
# links, so this does not need a full CommonMark-grade parser.
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
# CommonMark autolink form: `<https://example.com>` -- a bare URL wrapped in
# angle brackets, with no link text of its own.
_AUTOLINK_RE = re.compile(r"<(https?://[^\s<>]+)>")


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

    This repairs rather than rejects: an unknown link is turned into plain
    text (`[text](url)` -> `text`; `<url>` -> `url` with the brackets
    dropped) and the digest still goes out, rather than raising and
    discarding the whole run's output. A hard validation failure here (the
    way validate_output raises on a missing section) would risk looping
    forever if the model keeps emitting a bad link on every retry --
    section headings are a static template the model can plausibly get
    right on a rerun, but there's no reason to expect a rerun would stop
    hallucinating a URL. Known links are left completely untouched.

    Only the COUNT of stripped links is logged, never the URLs themselves:
    an attacker-chosen URL reaching the log (and Loki) is itself exposure
    -- e.g. an SSRF probe or a tracking domain encoded in the query string.
    """
    allowed = set(allowed_urls)
    stripped = 0

    def _replace_inline_link(match: re.Match[str]) -> str:
        nonlocal stripped
        text, url = match.group(1), match.group(2)
        if url in allowed:
            return match.group(0)
        stripped += 1
        return text

    result = _MARKDOWN_LINK_RE.sub(_replace_inline_link, markdown_text)

    def _replace_autolink(match: re.Match[str]) -> str:
        nonlocal stripped
        url = match.group(1)
        if url in allowed:
            return match.group(0)
        stripped += 1
        return url

    result = _AUTOLINK_RE.sub(_replace_autolink, result)

    if stripped:
        logger.warning(
            "enforce_link_allowlist: stripped %d link(s) with non-allowlisted URLs",
            stripped,
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
