"""Two secondary delivery channels: site publish (Cloudflare Worker PUT) and Telegram TL;DR.

See CLAUDE.md and digest/deliver.py's `deliver_channels` for how these fit into
the multi-channel delivery refactor. Kept as one small module (rather than
two) because both channels are thin, stdlib-urllib HTTP calls sharing the
same markdown-derived summary helpers (`extract_tldr`, `count_sections`,
`has_needs_attention`) -- splitting them into separate site.py/telegram.py
modules would just duplicate those three shared functions or force an
import between two otherwise-parallel modules, for no real benefit at this
size.

Both channels raise on failure and never retry internally -- digest/main.py's
`deliver_channels` is the retry boundary (each channel's own per-digest
sent flag drives the next run's retry, see digest/state.py's
get_pending_digests), matching the "gentleness" posture the rest of this
codebase's collectors already use for hitting external services (one
attempt per run, no in-process retry loop).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from digest.summarize import _real_heading_lines

logger = logging.getLogger(__name__)

# Sent on every request this module makes. Mirrors (does not import)
# digest/collectors/polymarket.py's own `_USER_AGENT` constant of the same
# value: that module is a COLLECTOR hitting the owner's Polymarket proxy
# Worker, this one is a PUBLISHER hitting a completely different Worker (the
# news-site ingest endpoint) plus the Telegram Bot API -- there is no shared
# "HTTP client" abstraction between them to hang a single constant off of,
# so duplicating the (live-verified) value here is simpler than importing a
# private constant across an otherwise-unrelated module boundary. The
# requirement itself is real, not cargo-culted: Cloudflare's edge (Browser
# Integrity Check) rejects urllib's default "Python-urllib/3.x" User-Agent
# with error 1010 before the request ever reaches a Worker behind it -- any
# stable, honest product identifier passes.
_USER_AGENT = "notification-digest/1.0"

_TELEGRAM_API_BASE = "https://api.telegram.org"

# The "Needs attention" heading is the prompt's own routing label, not a
# story -- see digest/summarize.py's _NEEDS_ATTENTION_HEADING and
# digest/emailer.py's _NEEDS_ATTENTION_RE for the same exclusion applied at
# the prompt-continuity and HTML-styling layers respectively. count_sections
# excludes it for the identical reason: it is not one of the topic sections
# the site's "N sections" summary is describing.
_NEEDS_ATTENTION_HEADING = "needs attention"

# The banner line summarize.summarize() deterministically prepends ahead of
# the model's own output, one line per failed collector (e.g.
# "⚠ telegram collection failed this run"). Recognized here by its leading
# character alone, matching digest/emailer.py's _BANNER_PARAGRAPH_RE
# convention for the same code-generated (never model-written) text.
_BANNER_PREFIX = "⚠"

# Deliberately WITHOUT a trailing colon: the model can render the marker as
# either `**TL;DR:** ...` (colon inside the bold run) or `**TL;DR**: ...`
# (colon outside it) -- see _TLDR_MARKER_RE just below, which tolerates
# both -- so the paragraph-detection prefix must match both shapes too, not
# just the first.
_TLDR_PREFIX = "**TL;DR"
# Strips a leading `**TL;DR:**`/`**TL;DR**:` marker from the raw MARKDOWN
# source (mirrors digest/emailer.py's _TLDR_PREFIX_RE, which does the same
# job against python-markdown's rendered HTML output instead -- the two
# patterns can't be shared since they operate on different serializations
# of the same content, but they encode the identical "either colon
# placement" tolerance).
_TLDR_MARKER_RE = re.compile(r"^\*\*TL;DR:?\*\*:?\s*", re.IGNORECASE)

# Markdown inline link, `[text](url)` -- deliberately simpler than
# digest/summarize.py's _MARKDOWN_LINK_RE (no title/angle-bracket handling):
# this only ever runs on a short TL;DR sentence or two, which this
# codebase's own prompt contract renders as plain `[text](url)` citations,
# never the fuller CommonMark forms that module's allowlist-enforcement
# pass has to defend against on arbitrary model output.
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")

# Superscript-digit citation markers (prompts/digest.md's `[¹](url)` style
# citations) -- identical character class to digest/emailer.py's
# _SUPERSCRIPT_ONLY_RE, reused here to strip the citation chip entirely
# (rather than pill-style it, which only makes sense in the HTML the email
# renders) once _MARKDOWN_LINK_RE has already reduced the link down to its
# visible (superscript) text.
_SUPERSCRIPT_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+")

# Single `*text*` markdown emphasis -- `**`/`__` (bold) are stripped by a
# plain string replace in _plain_text (see there for why), but a lone `*`
# still needs a real regex: a stray, unpaired `*` must survive untouched
# (deleting every `*` blindly would corrupt one), so this only matches a
# genuine PAIR wrapping some non-asterisk, non-newline text, and _plain_text
# replaces the whole match with just the inner text (group 1).
_SINGLE_EMPHASIS_RE = re.compile(r"\*([^*\n]+)\*")


class TelegramSendError(Exception):
    """Raised when send_telegram_tldr's Bot API call fails.

    The message NEVER includes the response body or the request URL: the
    URL embeds the bot token (`https://api.telegram.org/bot{token}/...`),
    and a token-bearing string reaching a log line (or Loki) is exactly the
    kind of secret leak CLAUDE.md's hard rules forbid. Only a status code or
    an exception type name is ever included.

    `status` carries the same HTTP status code structured (not just baked
    into the message string) so a caller can act on it programmatically --
    specifically digest/main.py's per-run circuit breaker (see
    `_TELEGRAM_MAX_AGE`'s neighboring comment for the incident this guards
    against): a 429 from Telegram's Bot API means "you are being rate
    limited, stop sending for now," and the breaker needs to check for
    exactly that code without re-parsing it back out of the message string.
    `None` for every non-HTTPError failure shape (network error, timeout,
    ...), which has no status code to report at all -- never confused with a
    429 by a caller that checks `exc.status == 429`.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _plain_text(text: str) -> str:
    """Reduce a short markdown snippet to plain text: links/emphasis stripped, citations dropped.

    `[text](url)` becomes `text`; a citation whose entire visible text is
    superscript digits (e.g. the `¹` left behind by `[¹](url)` after the
    link-stripping pass) is removed outright rather than kept as bare text,
    per extract_tldr's contract.

    `**bold**`/`__bold__` and `*italic*` markers are removed, keeping their
    inner text -- found in production on the live site index for digest #54:
    the model had written "**$100B ARR by year end**" INSIDE the TL;DR
    paragraph itself (not just as the `**TL;DR:**` marker _TLDR_MARKER_RE
    already strips off separately), so the literal `**` reached both the
    site excerpt and the Telegram message verbatim. `**`/`__` pairs are
    unambiguous -- neither ever appears in ordinary prose -- so they're
    deleted with a plain string replace, cheaper and safer than a regex for
    a construct that can't be confused with anything else.

    Single `*text*` emphasis gets its own regex (`_SINGLE_EMPHASIS_RE`)
    instead of a blanket `.replace("*", "")`, and single underscores are not
    touched AT ALL (only the doubled `__` form is stripped, identically to
    `**`, above) -- deliberately conservative: a lone `_` is extremely common
    inside a Telegram/X handle or a snake_case identifier (`@user_name`), and
    blindly deleting every `_` would corrupt those into `@username`. `*` has
    no equivalent collision (not a normal English- or handle-character), so
    a regex that only matches a genuine `*...*` PAIR (never a lone `*`) is
    safe to apply unconditionally.

    Otherwise intentionally narrow, not a general markdown-to-plaintext
    converter, since the TL;DR sentence the prompt contract produces never
    contains anything else link- or emphasis-shaped beyond what's handled
    here.
    """
    text = _MARKDOWN_LINK_RE.sub(lambda m: m.group(1), text)
    text = text.replace("**", "").replace("__", "")
    text = _SINGLE_EMPHASIS_RE.sub(r"\1", text)
    text = _SUPERSCRIPT_RE.sub("", text)
    # Removing a citation chip can leave a doubled space (" word  ." where
    # the chip used to sit) or trailing space before punctuation -- collapse
    # runs of whitespace so the result reads as normal prose.
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_tldr(body_md: str) -> str:
    """Extract the digest's TL;DR sentence as plain text, for the site/Telegram summaries.

    Never raises: this feeds two best-effort delivery channels, and a
    malformed/unexpected body_md shape must degrade to an empty or
    truncated string, never abort the run.

    After `body_md.lstrip()` and skipping any leading `⚠ ...` collector-
    failure banner lines (summarize.summarize()'s deterministic, code-
    generated prefix -- see _BANNER_PREFIX), the remaining text is split
    into blank-line-separated paragraphs and searched IN ORDER for the
    first one starting with `**TL;DR:` (the exact prefix
    digest/summarize.py's own soft-check tests for). This is deliberately a
    search over every paragraph, not just the very first one: the prompt
    contract can place a "## Needs attention" section ABOVE the TL;DR
    paragraph (see digest/emailer.py's _NEEDS_ATTENTION_RE docstring), so
    the TL;DR is not always literally the first paragraph after the banner.

    The matched paragraph has its `**TL;DR:**`/`**TL;DR**:` marker stripped
    (_TLDR_MARKER_RE, tolerating either colon placement, mirroring
    digest/emailer.py's own _TLDR_PREFIX_RE for the HTML-rendered form) and
    is reduced to plain text via `_plain_text` (markdown links -> their
    text, citation chips dropped).

    Fallback, when no paragraph starts with the TL;DR marker at all (e.g. a
    digest whose model output omitted it, which summarize() only WARNS
    about, never raises over): the first paragraph that is neither a
    heading (starts with `#`) nor a banner line, reduced to plain text and
    capped at 300 characters. Returns "" if nothing at all is found (an
    empty or pathological body_md).
    """
    try:
        text = body_md.lstrip()
        lines = text.splitlines()
        idx = 0
        while idx < len(lines) and lines[idx].strip().startswith(_BANNER_PREFIX):
            idx += 1
        remainder = "\n".join(lines[idx:])
        paragraphs = re.split(r"\n\s*\n", remainder)

        for para in paragraphs:
            stripped = para.strip()
            if stripped.startswith(_TLDR_PREFIX):
                content = _TLDR_MARKER_RE.sub("", stripped, count=1).strip()
                return _plain_text(content)

        for para in paragraphs:
            stripped = para.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith(_BANNER_PREFIX):
                continue
            return _plain_text(stripped)[:300]

        return ""
    except Exception:
        logger.warning("extract_tldr: failed to parse body_md, returning empty string")
        return ""


def count_sections(body_md: str) -> int:
    """Count the digest's real `## ` topic sections, excluding "Needs attention".

    Reuses digest/summarize.py's `_real_heading_lines` (imported, not
    reimplemented -- see that function's own docstring for why a second,
    subtly different fence/indentation-aware heading scan must never be
    allowed to drift out of sync with the one `validate_output` and
    `format_recent_coverage` already rely on). "Needs attention" is excluded
    because it is the prompt's own routing label for "this needs the
    reader's action", not a topic the digest covered -- see
    _NEEDS_ATTENTION_HEADING's module-level comment.
    """
    return sum(
        1
        for heading in _real_heading_lines(body_md)
        if heading.strip().lower() != _NEEDS_ATTENTION_HEADING
    )


def has_needs_attention(body_md: str) -> bool:
    """True iff the digest carries a real "## Needs attention" heading.

    Same underlying scan as `count_sections` (see its docstring) -- kept as
    a separate function rather than folded into a single call because the
    site's ingest payload wants both `section_count` (excluding this
    heading) and `has_attention` (whether it's present) as two independent
    fields.
    """
    return any(
        heading.strip().lower() == _NEEDS_ATTENTION_HEADING
        for heading in _real_heading_lines(body_md)
    )


def parse_failed_sources(body_md: str) -> list[str]:
    """Recover the failed-collector source list from a digest's own `⚠ ...` banner lines.

    digest/summarize.py's `summarize()` deterministically prepends one
    `⚠ <source> collection failed this run` line per failed source, in the
    given order, followed by a blank line, ahead of the model's own output
    (see that function's docstring, "The `⚠ <source> collection failed this
    run` banner"). This walks `body_md` line by line FROM THE START,
    collecting the source name off of every leading line that matches that
    exact shape, and stops at the first line that neither matches nor is
    blank -- so a banner-lookalike line appearing later in the body (inside
    the model's own output, say) is never picked up, only the genuine
    code-generated block at the very top.

    Deliberately parses this back out of `body_md` rather than reading it
    off a dedicated schema column: the banner is code-generated with a fixed,
    stable shape, already stored durably as part of every digest's `body_md`
    (digest/state.py's `create_digest`), and this same parse works
    identically for a pending resend (digest/deliver.py's `_deliver_site`
    runs on both the fresh-digest and pending-resend paths) with no schema
    migration required to add a new column just to duplicate what `body_md`
    already records.

    Returns `[]` when `body_md` carries no banner at all -- the common case,
    a run where every collector succeeded.
    """
    failed: list[str] = []
    for line in body_md.splitlines():
        match = re.match(r"^⚠ (\S+) collection failed this run$", line)
        if match:
            failed.append(match.group(1))
            continue
        if line.strip():
            break
    return failed


def publish_to_site(
    digest_id: int,
    body_md: str,
    body_html: str,
    created_at: str,
    item_count: int,
    publish_url: str,
    ingest_key: str,
    *,
    body_md_hu: str | None = None,
    body_html_hu: str | None = None,
    kind: str = "window",
    source_counts: dict[str, int] | None = None,
    failed_sources: list[str] | None = None,
    timeout_seconds: int = 30,
) -> None:
    """PUT one digest to the owner's Cloudflare Worker ingest endpoint. Raises on failure.

    `body_html` is the caller's already-rendered, already-sanitized digest
    HTML (digest/emailer.py's `render_body_html`, rendered once by
    digest/deliver.py's `deliver_channels` and handed to both the email and
    site channels) -- this function does no markdown rendering or
    sanitization of its own, it only ships what it's given. `item_count`
    comes from the caller (the `digests` row already carries it; there is
    no reason to recompute it from `body_md`).

    `tldr`/`section_count`/`has_attention` are derived here from `body_md`
    via this module's own `extract_tldr`/`count_sections`/
    `has_needs_attention` -- the site's ingest contract wants these as
    structured fields alongside the full markdown/HTML bodies, not
    re-derived by the Worker itself.

    `body_md_hu`/`body_html_hu` (keyword-only, both default None) are the
    optional Hungarian translation's markdown and its caller-rendered HTML
    (digest/emailer.py's `render_body_html`, rendered by `_deliver_site`
    exactly like `body_html` is -- this function never renders HTML itself,
    for either language). When BOTH are given, this function additionally
    derives `tldr_hu` from `body_md_hu` (via `extract_tldr`, with the
    identical `"(no summary)"` fallback the English `tldr` field gets) and
    adds `tldr_hu`/`body_html_hu`/`body_md_hu` to the payload -- all three or
    none, never a partial set, because the Worker's ingest validator 400s a
    request that carries only some of them. Deliberately no `section_count
    _hu`/`has_attention_hu`: those two fields describe the digest's
    STRUCTURE (how many topic sections, whether anything needs the reader's
    attention), which translation cannot change -- the English-derived
    values already describe the Hungarian body just as accurately, so
    duplicating them would only be redundant, never more correct.

    `kind` (keyword-only, default "window") is the digest's own stored kind
    (digest/state.py's `digests.kind`, "window" or "daily") -- sent
    unconditionally as its own `kind` payload field so the Worker can badge a
    daily brief distinctly on the site (a separate Worker change handles the
    actual display; this function only ever needs to forward the field).
    Threaded from the digest row by the caller on both the fresh-digest and
    pending-retry delivery paths (digest/deliver.py's `_deliver_site`), so a
    daily brief is labeled correctly however many runs it takes to actually
    publish.

    `source_counts` (keyword-only, default None) is the {source: item count}
    map (digest/state.py's `get_digest_source_counts`) the site renders as a
    per-entry source-spectrum bar; `failed_sources` (keyword-only, default
    None) is the list of collectors that failed this run
    (`parse_failed_sources`, above), which the site badges "partial". Both
    are included in the payload ONLY when truthy -- an empty dict/list or
    None all mean the field is left out entirely, matching the site ingest
    endpoint's normalize-empty-to-NULL contract (an explicit `{}`/`[]` and an
    absent field are treated identically there, so there is no correctness
    reason to send the empty shape over the wire).

    Raises whatever `urllib.request.urlopen` raises (network error, a
    non-2xx status via `urllib.error.HTTPError`, ...) completely
    unguarded -- matching digest/collectors/polymarket.py's `_fetch_markets`
    precedent: the caller (`deliver_channels`) is the layer that catches,
    logs (type name only), and marks this channel's own retry state, not
    this function.

    `x-ingest-key` authenticates to the owner's own Worker and is never
    logged by this function (nothing here logs at all -- see the module
    docstring for why raising unguarded is safe: the caller never logs
    `str(exc)`, only the exception's type name). `user-agent` is required to
    pass Cloudflare's Browser Integrity Check in front of the Worker (see
    _USER_AGENT's module-level comment).
    """
    payload: dict[str, Any] = {
        "created_at": created_at,
        # `or "(no summary)"`: the Worker's ingest validator rejects an empty
        # tldr with a 400, and extract_tldr can legitimately return "" on a
        # pathological body (it never raises, by contract). Without this
        # fallback such a digest would fail site publish on EVERY retry,
        # permanently -- a poison pill that keeps the whole run red forever.
        # A placeholder summary for one weird digest is the cheaper failure.
        "tldr": extract_tldr(body_md) or "(no summary)",
        "item_count": item_count,
        "section_count": count_sections(body_md),
        "has_attention": has_needs_attention(body_md),
        "body_html": body_html,
        "body_md": body_md,
        "kind": kind,
    }
    if body_md_hu is not None and body_html_hu is not None:
        payload["tldr_hu"] = extract_tldr(body_md_hu) or "(no summary)"
        payload["body_html_hu"] = body_html_hu
        payload["body_md_hu"] = body_md_hu
    # Truthy-only inclusion: an empty dict/list or None all mean "omit the
    # field", matching the site's normalize-empty-to-NULL ingest contract --
    # see this function's docstring.
    if source_counts:
        payload["source_counts"] = source_counts
    if failed_sources:
        payload["failed_sources"] = failed_sources
    data = json.dumps(payload).encode("utf-8")
    url = f"{publish_url}/ingest/{digest_id}"
    headers = {
        "content-type": "application/json",
        "x-ingest-key": ingest_key,
        "user-agent": _USER_AGENT,
    }
    request = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        response.read()


def _local_header_label(created_at: str) -> str:
    """Render `created_at` (ISO8601 UTC) as an Europe/Budapest label, e.g. "Thu, Jul 31 · 18:07".

    This is the one place in this module tz conversion happens -- per
    CLAUDE.md, storage stays UTC and render/email time is the only place
    that converts, and a Telegram notification's header line IS a
    render-time display, exactly like digest/main.py's `_send_and_finalize`
    building `generated_at_label` for the email masthead. Mirrors that
    function's format string (`%a, %b %-d · %H:%M`) so the two channels
    read consistently. `%-d` (no leading zero) is a glibc/BSD strftime
    extension, safe here for the identical reason noted at that call site:
    both macOS (BSD libc, dev) and the Linux container this deploys to
    (glibc) support it.

    A naive `created_at` (no tzinfo) is treated as UTC before converting --
    every `created_at` this codebase ever writes is
    `datetime.now(UTC).isoformat()` (digest/state.py's `create_digest`), so
    this only guards a hypothetical hand-edited/legacy value, mirroring
    digest/summarize.py's `_format_digest_age` fallback for the same reason.
    """
    parsed = datetime.fromisoformat(created_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone(ZoneInfo("Europe/Budapest"))
    return f"{local:%a, %b %-d · %H:%M}"


def send_telegram_tldr(
    digest_id: int,
    body_md: str,
    created_at: str,
    bot_token: str,
    chat_id: str,
    thread_id: int,
    public_base: str,
    timeout_seconds: int = 30,
) -> None:
    """POST a short TL;DR notification to the Telegram Bot API. Raises on failure.

    Message body: a render-time Europe/Budapest header line (see
    `_local_header_label`), a blank line, the digest's TL;DR text
    (`extract_tldr`), a blank line, and the reader-facing link
    (`f"{public_base}/d/{digest_id}"`). Sent as PLAIN TEXT -- no
    `parse_mode` is set -- deliberately: Telegram's Markdown/MarkdownV2
    parse modes reject the whole message on a single unescaped special
    character, and this text is built from the model's own summarization
    output, which this codebase never asks to produce Telegram-flavored
    markdown. A parse error there would be a classic silent-failure source
    (the sendMessage call itself fails with a 400, for a reason that has
    nothing to do with whether the DIGEST content was fine) -- plain text
    cannot fail to parse.

    `message_thread_id` is included in the request body only when
    `thread_id` is truthy (non-zero) -- 0 means "post to the group root"
    (Config.telegram_notify_thread_id's own contract), and Telegram's API
    treats an explicit 0 there as an invalid thread id, not "no thread", so
    omitting the key entirely (rather than sending `0`) is required, not
    cosmetic. `disable_web_page_preview: true` keeps the message compact --
    a link preview card for the site page adds visual noise a short TL;DR
    notification doesn't need.

    Error handling: the request URL embeds the bot token
    (`.../bot{token}/sendMessage`) and a failure response body could echo
    request content back -- NEITHER may ever reach a log line or an
    exception message (CLAUDE.md's secrets-never-logged hard rule). Every
    failure is caught here and re-raised as `TelegramSendError` carrying
    only a status code (`HTTPError.code`) or the underlying exception's
    type name -- never `str(exc)`, `exc.url`, or a response body read.
    """
    header = _local_header_label(created_at)
    tldr = extract_tldr(body_md)
    link = f"{public_base}/d/{digest_id}"
    text = f"{header}\n\n{tldr}"

    # The link rides as an INLINE KEYBOARD BUTTON, not as a URL in the text:
    # the raw link is long (it embeds the site's capability token) and reads
    # as noise in the topic — and a button needs NO parse_mode, so the
    # message text stays plain and unparseable-proof exactly as before
    # (an owner-requested change after seeing the first live messages).
    # Telegram renders the button below the message; tapping it opens the
    # digest page in the browser.
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[{"text": "Open the digest →", "url": link}]]
        },
    }
    if thread_id:
        payload["message_thread_id"] = thread_id

    data = json.dumps(payload).encode("utf-8")
    url = f"{_TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    request = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json", "user-agent": _USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        logger.warning("telegram sendMessage failed with status %d", exc.code)
        raise TelegramSendError(
            f"telegram sendMessage failed with status {exc.code}", status=exc.code
        ) from None
    except Exception as exc:
        logger.warning("telegram sendMessage failed: %s", type(exc).__name__)
        raise TelegramSendError(f"telegram sendMessage failed: {type(exc).__name__}") from None
