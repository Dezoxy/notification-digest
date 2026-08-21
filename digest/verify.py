"""PLAN.md §11.4 -- the optional verified-briefing pass over the daily brief.

A sibling of digest/daily.py and digest/translate.py, kept as its own module
rather than folded into digest/summarize.py: this is the ONLY code path in
the whole pipeline that ever invokes `claude -p` with tools enabled (see
run_claude_verify's own docstring for the security posture that demands),
which makes it a genuinely different kind of call from every other one in
summarize.py -- different CLI flags, a different output format, a different
parsing contract (a JSON-lines transcript, not plain text), and a dedicated
failure type (VerificationUnavailable) that its caller soft-fails on. Living
in its own module keeps digest/summarize.py's `run_claude` (used by every
window/daily/translate call site today) byte-identical, and keeps this
pass's much larger, more defensive parsing logic from bloating a module that
already exceeds 1600 lines.

It still reuses digest/summarize.py's `validate_output`, `enforce_link_
allowlist`, `strip_tldr_citations`, and `renumber_citations` rather than
reimplementing any of them: the verified brief is still "briefing markdown
that must have a real `## ` heading" and "a markdown document whose links
must be checked for provenance", exactly the same two contracts every other
briefing in this codebase has to satisfy.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import urllib.parse
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from digest.config import claude_subprocess_env
from digest.summarize import (
    enforce_link_allowlist,
    renumber_citations,
    strip_tldr_citations,
    validate_output,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "verify-daily.md"
_BUDAPEST_TZ = ZoneInfo("Europe/Budapest")


class VerificationUnavailable(Exception):
    """Raised when the verification pass could not produce a trustworthy result.

    Covers three distinct failure classes, deliberately folded into ONE
    exception type rather than three: a `claude -p` process-level failure
    (non-zero exit, timeout, empty stdout -- the same shapes
    digest/summarize.py's `run_claude` guards against for the toolless
    calls), and a transcript-shape surprise (the JSON-lines stream didn't
    look like what this module was written against -- see
    parse_verify_transcript's own docstring for the full defensive
    philosophy). The caller (digest/main.py's `run_daily`) does not need to
    distinguish WHY verification failed -- PLAN.md §11.4's contract is
    "unverified-on-time beats no brief" regardless of which of these three
    things went wrong, so every one of them routes to the identical
    soft-fail: ship the draft, prepend the code-generated
    "⚠ verification unavailable this run" banner, keep going. Messages must
    never include the verify prompt's own text or the CLI's raw
    stdout/stderr, mirroring digest/summarize.py's `SummarizeError` -- the
    prompt embeds the day's own briefing content, and a future transcript
    could embed fetched web content too.
    """


# The built-in tool set this pass enables -- and the ONLY tools enabled
# anywhere in this codebase's `claude -p` calls (every other call site,
# window/daily/translate, passes `--tools ""`; see digest/summarize.py's
# `run_claude`). WebSearch to find independent coverage, WebFetch to read a
# specific candidate page -- nothing else: no Bash, no file access, nothing
# that could act on the local machine even if a fetched page's prompt
# injection somehow induced a tool call outside what this prompt asks for.
_VERIFY_TOOLS = "WebSearch,WebFetch"

# `claude -p --output-format stream-json` (live-verified against the
# installed CLI, 2026-08-10) requires `--verbose` or the CLI refuses to
# start at all ("Error: When using --print, --output-format=stream-json
# requires --verbose") -- this is not optional plumbing, it's a hard
# precondition of the flag combination this function depends on.
#
# `--permission-mode bypassPermissions` is required too, separately from
# `--tools`: live-verified that WITHOUT it, WebSearch/WebFetch calls are
# silently DENIED (they show up in the result's `permission_denials` array)
# and the model falls back to answering from its own training data instead
# of anything it actually fetched -- exactly the failure mode Wall 2 exists
# to prevent, and one that would otherwise fail SILENTLY (the CLI still
# exits 0 with a plausible-looking answer). `--tools` alone only controls
# which tools EXIST for this session; whether an existing tool may actually
# run without an interactive approval prompt is `--permission-mode`'s job.
# This is safe specifically because `--tools` has already narrowed the
# session to WebSearch/WebFetch -- there is nothing more dangerous for
# `bypassPermissions` to unlock here.
_VERIFY_CLI_ARGS = (
    "--output-format",
    "stream-json",
    "--verbose",
    "--tools",
    _VERIFY_TOOLS,
    "--permission-mode",
    "bypassPermissions",
)


def run_claude_verify(
    prompt: str, model: str, timeout_seconds: int, effort: str
) -> tuple[str, list[str]]:
    """Invoke `claude -p` with WebSearch/WebFetch enabled; return (text, visited_urls).

    Mirrors digest/summarize.py's `run_claude` on subprocess safety --
    neutral empty cwd (so the CLI's auto-ingested workspace context,
    CLAUDE.md/git state, never leaks into the prompt), the scrubbed
    `claude_subprocess_env()` allowlist (PATH/HOME/USER/CLAUDE_CONFIG_DIR
    only, so a tool call has nothing to read TG_SESSION/SMTP_PASSWORD/etc.
    out of), `text=True, encoding="utf-8"` for the same non-ASCII-safe
    reason -- but genuinely diverges on the flags themselves, which is why
    this is its own function rather than a `run_claude` parameter: `--tools
    ""` becomes `--tools "WebSearch,WebFetch"`, `--output-format text`
    becomes `--output-format stream-json --verbose` (needed to get a
    tool-use transcript at all -- `--output-format json`'s single result
    object carries no transcript, only the final text; live-verified
    against the installed CLI), and `--permission-mode bypassPermissions`
    is added (see `_VERIFY_CLI_ARGS`'s own comment for why it's required
    and why it's safe here specifically).

    `text` is the model's final response text (the CLI's terminal `result`
    record's own `result` field). `visited_urls` is the deduplicated,
    order-preserving list of every URL this run's WebFetch tool calls
    actually requested -- see `parse_verify_transcript` for the full
    extraction contract, in particular why WebSearch's own candidate links
    are deliberately NOT included.

    Raises `VerificationUnavailable` -- never `SummarizeError`, this
    function's failure surface is deliberately separate from every other
    `claude -p` call site's -- on a non-zero exit, a timeout, or any
    transcript-shape surprise `parse_verify_transcript` detects. Never
    raises for "the model didn't find anything to verify" -- that is
    normal, valid output (an empty `visited_urls`, non-empty `text`), not a
    failure.
    """
    try:
        # cwd is a fresh empty directory, identical rationale to run_claude's
        # own neutral_cwd: the CLI auto-ingests workspace context from
        # wherever it runs, and this call's prompt must be the only input.
        with tempfile.TemporaryDirectory(prefix="digest-claude-verify-") as neutral_cwd:
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    "--model",
                    model,
                    *_VERIFY_CLI_ARGS,
                    "--effort",
                    effort,
                ],
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_seconds,
                env=claude_subprocess_env(),
                cwd=neutral_cwd,
            )
    except subprocess.TimeoutExpired as exc:
        raise VerificationUnavailable(
            f"claude -p (verify) timed out after {timeout_seconds}s"
        ) from exc

    if result.returncode != 0:
        # stdout/stderr suppressed from the message entirely, identical
        # posture to run_claude's own non-zero-exit branch: this prompt
        # embeds the day's own briefing content, and the CLI can echo
        # submitted text in its diagnostics.
        raise VerificationUnavailable(
            f"claude -p (verify) exited {result.returncode} (stdout suppressed, "
            f"{len(result.stdout)} chars; stderr suppressed, {len(result.stderr)} "
            "chars -- rerun manually to inspect)"
        )

    return parse_verify_transcript(result.stdout)


def parse_verify_transcript(stdout: str) -> tuple[str, list[str]]:
    """Deterministically extract (final_text, visited_urls) from a `stream-json` transcript.

    PLAN.md §11.4's Wall 2: "the allowlist widens ONLY with URLs the
    verifier provably fetched, extracted deterministically from the CLI
    transcript, never from model prose." This function is that extraction.
    `stdout` is `claude -p --output-format stream-json --verbose`'s raw
    output: one JSON object per line (a "system" init record, alternating
    "assistant"/"user" turn records, and a terminal "result" record --
    live-verified against the installed CLI, 2026-08-10).

    DEFENSIVE PHILOSOPHY (PLAN.md §11.4's "transcript is not a stable API"
    guardrail): this transcript shape is observed behavior, not a
    documented contract the CLI promises to keep across versions. Two
    different strictness levels apply, deliberately:

    STRICT -- raises `VerificationUnavailable` -- on anything this function
    structurally DEPENDS on to produce a trustworthy result at all:
    - `stdout` empty or every line blank.
    - any non-blank line that isn't valid JSON.
    - not exactly one "result"-type record (zero, or more than one).
    - the result record's own `is_error` field is truthy.
    - the result record's `result` field is missing, not a string, or
      blank.
    - an "assistant"-type record whose `message` isn't a dict, or whose
      `message["content"]` isn't a list -- this is the shape every
      tool-use lives inside, so a record built differently means this
      function's whole extraction strategy no longer applies and any
      `visited_urls` it produced could be silently incomplete rather than
      genuinely empty.
    - a `tool_use` content block named exactly "WebFetch" whose `input`
      isn't a dict, or whose `input["url"]` is missing / not a
      non-blank string -- WebFetch's `url` parameter is exactly what Wall
      2 depends on; if THIS specific shape drifts, the fetched-URL
      guarantee is no longer provable and must not be silently
      approximated.

    LENIENT -- skipped, never raised -- on everything this function does
    NOT depend on:
    - any record whose `type` isn't "assistant" (system/user/
      rate_limit_event/other records -- this function only needs the
      assistant turns' own tool calls).
    - any content block whose `type` isn't "tool_use" (text/thinking/other
      block kinds -- not this function's concern).
    - any `tool_use` block whose `name` isn't "WebFetch" (WebSearch calls
      in particular -- see below for why their results are excluded on
      purpose, not by omission).

    WHY ONLY WebFetch, NEVER WebSearch: a WebSearch tool_result carries
    candidate links the model has SEEN, not pages it has READ -- the model
    could cite one of those links without ever having verified what's
    actually on the page, which is exactly the unverified-citation problem
    this whole pass exists to fix. Only a WebFetch call is "provably
    fetched" in PLAN.md §11.4's sense: the pipeline itself retrieved that
    URL's content. prompts/verify-daily.md's own contract ("cite ONLY pages
    you actually fetched... WebSearch surfaces candidate sources only")
    is written to match this extraction exactly -- the model is told the
    same rule this function enforces mechanically, so a citation that
    skips the WebFetch step gets stripped downstream by
    `enforce_link_allowlist` regardless of what the prompt asked for.

    The extracted URL is WebFetch's own `input.url` -- the URL the pipeline
    REQUESTED, not any "final URL after redirects" the tool might have
    landed on. The transcript's `tool_result` for a WebFetch call is a
    prose summary of page content, not a structured record carrying a
    post-redirect URL, so the requested URL is the only value this function
    can extract with any confidence -- `digest/verify.py`'s
    `normalize_url`/widening layer (see `widen_allowed_urls`) is what
    absorbs small mismatches (tracking params, trailing slash, default
    port) between that requested URL and however the model ends up citing
    it, rather than this function guessing at a URL it never actually saw.

    URLs are deduplicated, first-occurrence order preserved (a story
    verified against the same page from two different angles doesn't need
    two allowlist entries, and `set` iteration order is not itself
    something later code should depend on).

    `text` is the terminal result record's own `result` field, stripped --
    this function never reads the assistant turns' own text blocks for the
    final answer (the CLI's own terminal summary is the authoritative
    "what did this session conclude" value, live-verified to match the
    final assistant turn's text exactly). Model PROSE is never scanned for
    URLs anywhere in this function -- the only URLs this function ever
    returns come from a `tool_use` block's structured `input`, never from
    parsing a string of text for anything URL-shaped.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise VerificationUnavailable("verify transcript: empty output")

    records: list[dict] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, ValueError) as exc:
            raise VerificationUnavailable("verify transcript: a line was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise VerificationUnavailable("verify transcript: a line was not a JSON object")
        records.append(parsed)

    result_records = [r for r in records if r.get("type") == "result"]
    if len(result_records) != 1:
        raise VerificationUnavailable(
            f"verify transcript: expected exactly one 'result' record, found {len(result_records)}"
        )
    result_record = result_records[0]

    if result_record.get("is_error"):
        raise VerificationUnavailable("verify transcript: CLI reported is_error on the result")

    text = result_record.get("result")
    if not isinstance(text, str) or not text.strip():
        raise VerificationUnavailable(
            "verify transcript: result record's own 'result' field is missing or blank"
        )

    visited_urls: list[str] = []
    seen: set[str] = set()
    for record in records:
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            raise VerificationUnavailable(
                "verify transcript: an assistant record's 'message' was not an object"
            )
        content = message.get("content")
        if not isinstance(content, list):
            raise VerificationUnavailable(
                "verify transcript: an assistant message's 'content' was not a list"
            )
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "WebFetch":
                continue
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                raise VerificationUnavailable(
                    "verify transcript: a WebFetch tool_use block's 'input' was not an object"
                )
            url = tool_input.get("url")
            if not isinstance(url, str) or not url.strip():
                raise VerificationUnavailable(
                    "verify transcript: a WebFetch tool_use block had no usable 'url'"
                )
            if url not in seen:
                seen.add(url)
                visited_urls.append(url)

    return text.strip(), visited_urls


# Tracking query-parameter names normalize_url strips outright, per PLAN.md
# §11.4's locked-down rule set ("nothing else"). `utm_*` is a prefix (utm_
# source/medium/campaign/term/content, and any future utm_ variant);
# fbclid/gclid/ref_src are exact param names (Facebook/Google click-id
# trackers, and X's own referral-source tag).
_TRACKING_PARAM_EXACT = {"fbclid", "gclid", "ref_src"}


def normalize_url(url: str) -> str:
    """Pure, idempotent URL normalization for allowlist-widening comparisons only.

    PLAN.md §11.4's locked URL-normalization rule, implemented EXACTLY and
    ONLY as specified -- "nothing else":

    1. Lowercase the scheme and host (URI schemes/hosts are case-
       insensitive per RFC 3986; `HTTPS://Example.COM/x` and
       `https://example.com/x` are the same resource).
    2. Strip a default port (`:80` on `http`, `:443` on `https`) -- an
       explicit default port and no port at all address the same resource.
    3. Strip the fragment (`#section`) -- never sent to the server, never
       part of what was fetched.
    4. Strip tracking query params (`utm_*`, `fbclid`, `gclid`, `ref_src`)
       -- present or absent, they never change which resource a URL
       identifies.
    5. Collapse a trailing slash on a NON-root path (`/a/` -> `/a`;
       `/` stays `/`) -- a bare-origin URL's root path is left untouched,
       since stripping it there would change `scheme://host/` into
       `scheme://host`, which is a different (if equivalent-looking)
       string this function's contract does not claim to also normalize
       (that direction -- adding/removing a trailing slash regardless of
       depth -- is `widen_allowed_urls`'s separate "trailing-slash variant"
       step, not this function's job).

    Everything else survives untouched: query param VALUES, query param
    ORDER (besides removing the tracking ones), path case, userinfo. This
    is a narrow, auditable rule set for allowlist-widening comparisons, not
    a general-purpose URL canonicalizer -- widening the rule set later
    needs a deliberate PLAN.md decision, not a silent expansion here.

    Idempotent by construction: every rule above, reapplied to its own
    already-normalized output, is a no-op (an already-lowercase string
    lowercases to itself, an already-stripped default port has no port left
    to strip, etc.) -- `normalize_url(normalize_url(x)) == normalize_url(x)`
    holds for every input, not just well-formed ones (tested).
    """
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    default_port = {"http": 80, "https": 443}.get(scheme)

    netloc = hostname
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"
    if parsed.port is not None and parsed.port != default_port:
        netloc = f"{netloc}:{parsed.port}"

    kept_params = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAM_EXACT
    ]
    query = urllib.parse.urlencode(kept_params)

    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def _toggle_trailing_slash(url: str) -> str:
    """Return the OPPOSITE-trailing-slash form of `url`: add one if absent, strip one if present.

    Private to this module -- used only by `widen_allowed_urls`, never
    exported as part of `normalize_url`'s own contract (that function only
    ever COLLAPSES a trailing slash on a non-root path; it never adds one).
    A verifier citing `https://example.com/report` when the pipeline
    fetched `https://example.com/report/` (or vice versa) is a real,
    expected mismatch shape `normalize_url` alone does not close -- that
    function strips a trailing slash from a NON-root path, but a URL with
    NO trailing slash at all is already "normalized" by that rule and never
    gains one. This function exists specifically to add the missing
    direction: the widened allowlist needs BOTH the as-fetched form and its
    slash-toggled sibling, since `enforce_link_allowlist`/
    `_enforce_anchor_provenance` are both exact-string-membership checks
    with no normalization of their own.

    Guards against mangling a bare `scheme://` with no host/path at all
    (not a URL shape this codebase's collectors or WebFetch calls ever
    produce, but cheap to guard defensively rather than assume).
    """
    if url.endswith("/") and len(url) > 1 and not url.endswith("://"):
        return url[:-1]
    return url + "/"


def widen_allowed_urls(existing_urls: Collection[str], visited_urls: Collection[str]) -> set[str]:
    """Build the widened link-provenance allowlist, per PLAN.md §11.4's locked formula.

    widened = existing_urls (verbatim, UNTOUCHED)
            ∪ visited_urls (as-is)
            ∪ {normalize_url(u) for u in visited_urls}
            ∪ {trailing-slash-toggled(u) for u in visited_urls}

    `existing_urls` -- the draft's own allowlist (the union of the source
    window digests' stamped item URLs, unchanged from what `summarize_daily`
    was already checked against) -- is deliberately NEVER normalized here:
    those URLs are already the ground truth this pipeline has always
    enforced, and running them through `normalize_url` could theoretically
    ADD a normalized form that happens to collide with something that
    should NOT be allowed (astronomically unlikely, but there is no reason
    to take on that risk for URLs that were already exactly right).
    `visited_urls` gets three forms specifically because those are new,
    freshly-fetched URLs the verifier's own citations have to match
    byte-for-byte against, and a citation can plausibly diverge from the
    as-fetched URL in either of two independent ways (tracking-param/case/
    port noise, or a trailing-slash toggle) -- covering both, plus the raw
    as-fetched form itself, is what makes `enforce_link_allowlist`'s
    exact-membership check tolerant of the mismatches PLAN.md §11.4
    anticipated without weakening what it rejects: a citation to a URL the
    pipeline never fetched at all still has no path into this set.

    Passed straight to the EXISTING, UNCHANGED enforcement functions
    (`enforce_link_allowlist`, `digest/emailer.py`'s
    `_enforce_anchor_provenance`) -- this function only builds the set they
    check against; it enforces nothing itself.
    """
    widened = set(existing_urls)
    for url in visited_urls:
        widened.add(url)
        widened.add(normalize_url(url))
        widened.add(_toggle_trailing_slash(url))
    return widened


def build_verify_prompt(draft_body_md: str, max_web_ops: int, now: datetime) -> str:
    """Load prompts/verify-daily.md and substitute its MAX_WEB_OPS/TODAY_LABEL/DRAFT_MD.

    `draft_body_md` is today's daily brief draft (digest/daily.py's
    `summarize_daily` output) -- this codebase's OWN prior output, but
    derived from the same untrusted, scraped Telegram/X/news material as
    everything else in this pipeline, so it gets the IDENTICAL fencing
    discipline `digest/daily.py`'s `build_daily_prompt` and
    `digest/translate.py`'s `build_translate_prompt` both apply to their own
    body_md-shaped inputs: every backtick escaped to its JSON unicode-escape
    form (`` ` `` -> `\\u0060`, reversed on the model's OUTPUT by
    `verify_daily` before anything else touches it, mirroring those two
    functions' own callers) and every "{{" neutralized into "{ {" via the
    same lookahead-based substitution, so a run of 3+ braces can't leave a
    live placeholder-shaped pair behind. See `build_daily_prompt`'s own
    docstring for the full "why fencing discipline applies even to our own
    prior output" reasoning; it is not repeated a second time here.

    `max_web_ops`/`now` are both code-generated, deterministic values with
    no untrusted content in them -- substituted in ANY order relative to
    each other, but both strictly BEFORE `{{DRAFT_MD}}`, identical ordering
    discipline to every other prompt-builder in this codebase
    (`digest/summarize.py`'s `build_prompt`, `digest/daily.py`'s
    `build_daily_prompt`): str.replace rescans its whole input on every
    call, so the untrusted-derived substitution must always go last, or a
    draft that happened to contain the literal text "{{MAX_WEB_OPS}}" could
    get rewritten by a later .replace() call.

    `now` renders as a bare Europe/Budapest date (`{{TODAY_LABEL}}`, e.g.
    "2026-08-10") -- search recency grounding only ("what does 'today'
    mean when you search"), not a full timestamp; the verify pass has no
    "evening brief" framing of its own the way `build_daily_prompt`'s
    `{{NOW_LABEL}}` does; that framing belongs to the draft it is checking,
    not to this pass.
    """
    template = _PROMPT_PATH.read_text()
    today_label = f"{now.astimezone(_BUDAPEST_TZ):%Y-%m-%d}"
    escaped_draft = draft_body_md.replace("`", "\\u0060")
    escaped_draft = re.sub(r"\{(?=\{)", "{ ", escaped_draft)
    return (
        template.replace("{{MAX_WEB_OPS}}", str(max_web_ops))
        .replace("{{TODAY_LABEL}}", today_label)
        .replace("{{DRAFT_MD}}", escaped_draft)
    )


def verify_daily(
    draft_body_md: str,
    allowed_urls: Collection[str],
    model: str,
    timeout_seconds: int,
    effort: str,
    max_web_ops: int,
    *,
    now: datetime | None = None,
) -> tuple[str, set[str]]:
    """Run the verification pass over an already-validated daily draft.

    Pipeline mirrors `digest/daily.py`'s `summarize_daily` and
    `digest/translate.py`'s `translate_digest`: build the prompt
    (`build_verify_prompt`), run it (`run_claude_verify`, this module's own
    tool-enabled CLI call), validate the structural contract
    (`validate_output`, reused unchanged from `digest/summarize.py` -- at
    least one real `## ` heading, exactly the same bar every other briefing
    in this codebase has to clear), reverse the backtick escape
    `build_verify_prompt` applied to the draft (mirroring
    `summarize_daily`'s own identical reversal), widen the allowlist
    (`widen_allowed_urls`), enforce it (`enforce_link_allowlist`, reused
    UNCHANGED -- this function only ever WIDENS what set gets passed in,
    never touches the enforcement logic itself), then
    `strip_tldr_citations`/`renumber_citations` last, identical ordering to
    `summarize_daily` and for the identical reason (the owner's requirement
    that the TL;DR paragraph carry no citation links applies to every
    briefing this codebase produces, and renumbering only rewrites link
    TEXT, never a URL, so it cannot affect the provenance guarantee link
    enforcement just established).

    Returns `(verified_body_md, widened_allowed_urls)` -- the caller
    (digest/main.py's `run_daily`) needs the widened set too, to thread it
    into `deliver_channels` so the SAME widened allowlist is used at
    render/delivery time, not just during this function's own
    `enforce_link_allowlist` pass (see `digest/deliver.py`'s
    `deliver_channels` `extra_allowed_urls` parameter).

    Raises `VerificationUnavailable` (from `run_claude_verify`, covering
    every CLI/transcript failure shape) or `SummarizeError` (from
    `validate_output`, covering "the model's output failed the daily
    contract") -- this function does NOT catch either; PLAN.md §11.4's
    soft-fail decision (ship the draft with the code-prepended banner
    instead) is the CALLER's job, exactly like `summarize_daily`'s own
    `SummarizeError` propagates rather than being caught here. A verified
    brief is better; an unverified brief on time beats no brief -- but that
    trade-off is decided once, at the call site, not duplicated into every
    function along the way.
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    prompt = build_verify_prompt(draft_body_md, max_web_ops, resolved_now)
    text, visited_urls = run_claude_verify(prompt, model, timeout_seconds, effort)
    validate_output(text)
    text = text.replace("\\u0060", "`")
    widened = widen_allowed_urls(allowed_urls, visited_urls)
    text = enforce_link_allowlist(text, widened)
    text = renumber_citations(strip_tldr_citations(text))
    return text, widened
