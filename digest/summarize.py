"""Builds the summarization prompt and invokes the Claude CLI.

See PLAN.md §4.4 and §5. The `claude` CLI authenticates via the owner's
persisted Max-subscription login (CLAUDE_CONFIG_DIR) -- no API key, no SDK
dependency here, just a subprocess call.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

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

    Raises SummarizeError on a non-zero exit, empty/whitespace-only stdout,
    or a timeout. On a non-zero exit, stderr is suppressed entirely (only its
    length is reported) rather than included in the error message: the CLI
    can echo submitted text -- which contains scraped Telegram/X message
    content -- in its diagnostics, and that error message gets logged and
    shipped to Loki.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
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

    Fenced code blocks are also excluded from heading detection: a refusal
    can legitimately quote the required heading text inside a ``` block
    (e.g. "here's the template you asked about:\n```\n## Needs attention\n...")
    and that is not a real section -- it is example text sitting inside a
    code fence. A stripped line starting with three or more backticks
    toggles an in-fence flag; while the flag is set, "## " lines are not
    counted as headings, and the fence delimiter lines themselves are never
    counted as headings either.

    Indented lines are excluded from both heading and fence-delimiter
    detection, before any stripping happens: per CommonMark, an ATX heading
    (or a fence delimiter) may be indented at most 3 spaces -- a line
    starting with a tab, or with 4 or more leading spaces, is an indented
    code block instead. A refusal that pads a template with 4-space
    indentation (e.g. "    ## Needs attention") is therefore code content,
    not a real heading, and must not satisfy the contract.
    """
    heading_lines = []
    in_fence = False
    for line in markdown_text.splitlines():
        # CommonMark: 4+ leading spaces or a leading tab makes this an
        # indented code block -- neither a heading nor a fence delimiter
        # can start here, regardless of what follows the indentation.
        if line.startswith("\t") or line[:4] == "    ":
            continue
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
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


def summarize(
    items: list[Item],
    failed_sources: list[str],
    model: str,
    timeout_seconds: int,
) -> str:
    """Build the prompt, run it through Claude, and validate the contract.

    Never call with an empty item list. Raises SummarizeError (via
    run_claude or validate_output) rather than returning malformed output,
    so the caller never persists a digest for content that failed the
    output contract.
    """
    prompt = build_prompt(items, failed_sources)
    output = run_claude(prompt, model, timeout_seconds)
    validate_output(output)
    return output
