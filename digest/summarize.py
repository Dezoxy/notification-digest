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
# requires, always, verbatim, lowercased for case-insensitive matching.
_REQUIRED_HEADINGS = ("## needs attention", "## worth knowing", "## noise skipped")


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

    if failed_sources:
        collector_status = "Collector status: " + ", ".join(
            f"{source} collection failed this run" for source in failed_sources
        )
    else:
        collector_status = "Collector status: all collectors succeeded this run."

    return template.replace("{{ITEMS_JSON}}", items_json).replace(
        "{{COLLECTOR_STATUS}}", collector_status
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

    Only the heading names are checked, case-insensitively -- never the
    error message includes the offending output itself, since it may
    contain scraped Telegram/X message content (see SummarizeError).

    Links are deliberately NOT validated here: a window with nothing but
    noise legitimately produces zero `[text](url)` links in "Worth
    knowing", and that is correct output, not a contract violation.
    """
    lowered = markdown_text.lower()
    missing = [heading for heading in _REQUIRED_HEADINGS if heading not in lowered]
    if missing:
        names = ", ".join(heading.removeprefix("## ") for heading in missing)
        raise SummarizeError(f"digest output missing required section(s): {names}")


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
