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
    or a timeout. The error message includes a short stderr tail for
    diagnostics but never the prompt itself (it contains scraped message
    content).
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
        stderr_tail = result.stderr.strip()[-500:]
        raise SummarizeError(
            f"claude -p exited {result.returncode}: {stderr_tail}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise SummarizeError("claude -p returned empty output")

    return stdout


def summarize(
    items: list[Item],
    failed_sources: list[str],
    model: str,
    timeout_seconds: int,
) -> str:
    """Build the prompt and run it through Claude. Never call with an empty item list."""
    prompt = build_prompt(items, failed_sources)
    return run_claude(prompt, model, timeout_seconds)
