"""Renders the digest markdown to HTML, sends it via SMTP, and archives it to disk.

See PLAN.md §4.5. Stdlib smtplib + the `markdown` package only -- no
templating framework needed for a handful of emails a day.
"""

from __future__ import annotations

import html
import logging
import re
import smtplib
import ssl
from collections.abc import Collection
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import markdown
import nh3

logger = logging.getLogger(__name__)

# Without an explicit timeout, smtplib.SMTP() uses the global default socket
# timeout (effectively none), so a hung SMTP endpoint would wedge the
# one-shot run forever instead of failing into the pending-digest retry path.
_SMTP_TIMEOUT_SECONDS = 30

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    max-width: 42em;
    margin: 0 auto;
    padding: 1em;
    line-height: 1.5;
    color: #1a1a1a;
  }}
  a {{ color: #0a58ca; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


# Tags the digest contract can legitimately produce (PLAN.md §4.5 body_md is
# headings/lists/links/emphasis/code/quotes only). No `img`: nothing in the
# digest needs an image, and allowing it would let hostile markdown embed a
# tracking pixel via `![x](https://attacker.example/pixel)`.
_ALLOWED_TAGS = {
    "h1", "h2", "h3", "p", "ul", "ol", "li", "a", "strong", "em", "code",
    "pre", "blockquote", "br", "hr",
}
_ALLOWED_ATTRIBUTES = {"a": {"href"}}
_ALLOWED_URL_SCHEMES = {"http", "https"}

# Matches a single well-formed anchor in nh3-sanitized output: `<a ...
# href="URL" ...>inner</a>`. This is deliberately non-greedy on the inner
# content and is SAFE ONLY because it runs on nh3.clean's output, never on
# raw/untrusted input: nh3 normalizes its output to well-formed HTML with
# quoted attributes and no nested `<a>` tags, so there is no adversarial
# grammar here to evade (unlike markdown source, where regexes chasing
# CommonMark's link-label grammar can be beaten by balanced nested
# brackets -- see enforce_link_allowlist in digest/summarize.py). href is
# always the first attribute nh3 emits, matching `\bhref="..."` immediately
# after the tag name/whitespace.
_ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)


def _enforce_anchor_provenance(sanitized_html: str, allowed_urls: Collection[str]) -> str:
    """Unwrap every anchor in sanitized HTML whose href isn't an allowlisted URL.

    This is the authoritative link-provenance layer: it runs on the HTML the
    renderer actually produced (post markdown-conversion, post nh3.clean),
    so it can't be evaded by a markdown form that a source-level regex
    fails to recognize as a link (e.g. `[review [urgent]](https://attacker
    .example/phish)` -- CommonMark's balanced-nested-bracket label grammar
    renders this as a real anchor, but a naive `[^\\]]*` label regex stops
    at the first `]` and never sees it as a link at all). Inspecting the
    rendered anchor instead of re-deriving the renderer's grammar makes this
    check renderer-grammar-proof.

    nh3 entity-escapes hrefs (e.g. `&` becomes `&amp;`), so the captured
    href is html.unescape()'d before comparing against ``allowed_urls``,
    which holds the raw, unescaped URLs. An anchor whose (unescaped) href is
    not an exact member of ``allowed_urls`` is replaced by just its inner
    content -- the link disappears, the visible text survives.

    Only the COUNT of unwrapped anchors is logged, never the URLs
    themselves, for the same reason enforce_link_allowlist only logs a
    count: a logged attacker-chosen URL is itself exposure.
    """
    allowed = set(allowed_urls)
    unwrapped = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal unwrapped
        href, inner = match.group(1), match.group(2)
        if html.unescape(href) in allowed:
            return match.group(0)
        unwrapped += 1
        return inner

    result = _ANCHOR_RE.sub(_replace, sanitized_html)
    if unwrapped:
        logger.warning(
            "render_html: unwrapped %d anchor(s) with non-allowlisted hrefs",
            unwrapped,
        )
    return result


def render_html(body_md: str, allowed_urls: Collection[str]) -> str:
    """Convert digest markdown to a self-contained HTML document.

    Three distinct threats are neutralized here, in three different layers:

    1. Raw HTML smuggled verbatim in message text (e.g. a hostile chat
       message containing `<img onerror=...>` that Claude echoes into the
       digest). `&` and `<` are entity-escaped BEFORE markdown conversion,
       turning any raw tag into inert text (`&lt;img ...&gt;`) before the
       markdown converter ever sees it. Legitimate markdown syntax
       (`# heading`, `[text](url)`, etc.) contains neither character and is
       unaffected.

    2. Active content that hostile *markdown* (not raw HTML) generates once
       converted -- e.g. `![status](https://attacker.example/pixel)` becomes
       a real `<img>` tracking pixel, or `[click](javascript:...)` becomes a
       `javascript:` link. The pre-escape above does nothing here since the
       source text has no `<`. After conversion, `nh3.clean` sanitizes the
       generated HTML against an allowlist of tags/attributes the digest
       contract can legitimately produce, dropping anything else (including
       `<img>` and non-http(s) URL schemes).

    3. Link *provenance*, regardless of markdown form. `digest/summarize
       .py`'s `enforce_link_allowlist` tries to catch this at the markdown
       source level, but it's a losing game: chasing CommonMark's link-label
       grammar with regexes can always be beaten by some valid-but-unmatched
       form (nested balanced brackets in the label being one concrete
       example). `_enforce_anchor_provenance`, run here after markdown
       conversion AND nh3 sanitization, is the authoritative layer for this
       -- it inspects what the renderer actually produced, so it is
       renderer-grammar-proof by construction rather than by enumerating
       cases. `enforce_link_allowlist` stays in place as defense-in-depth,
       and it remains necessary for a threat this HTML-level pass cannot
       see at all: the text/plain MIME part, where a raw URL in message
       text gets auto-linkified by the recipient's mail client with no
       renderer in between for this layer to inspect.
    """
    escaped = body_md.replace("&", "&amp;").replace("<", "&lt;")
    body_html = markdown.markdown(escaped)
    sanitized = nh3.clean(
        body_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    )
    sanitized = _enforce_anchor_provenance(sanitized, allowed_urls)
    return _HTML_TEMPLATE.format(body=sanitized)


def send_digest(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    digest_from: str,
    digest_to: str,
    subject: str,
    body_md: str,
    allowed_urls: Collection[str],
) -> None:
    """Send the digest as a multipart (plain + HTML) email over SMTP with STARTTLS.

    ``allowed_urls`` is the set of URLs the HTML part's links are allowed to
    point at -- the digest's stamped item URLs -- and is passed straight
    through to render_html's anchor-provenance check (see its docstring).
    The text/plain part is unaffected: it carries body_md verbatim, relying
    on enforce_link_allowlist (digest/summarize.py) having already run at
    the markdown-source level for that MIME part's protection.

    Message acceptance -- send_message() returning without raising -- is the
    success criterion: at that point the server has taken the mail for
    delivery. Failures up to and including send_message() raise, and the
    caller is responsible for leaving the digest row unsent (email_sent=0)
    so the next run retries the send without re-summarizing (PLAN.md §4.1).

    SMTP teardown (quit()) is best-effort and happens after that success
    criterion is met, so a non-221 QUIT response or other teardown error is
    logged and swallowed rather than raised -- otherwise a caller would see
    an exception for an already-sent digest, mark it unsent, and re-send a
    duplicate on the next run.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = digest_from
    msg["To"] = digest_to
    msg.attach(MIMEText(body_md, "plain"))
    msg.attach(MIMEText(render_html(body_md, allowed_urls), "html"))

    smtp = smtplib.SMTP(smtp_host, smtp_port, timeout=_SMTP_TIMEOUT_SECONDS)
    try:
        # An explicit default context enforces certificate-chain and
        # hostname verification. Without it, starttls() falls back to
        # Python's unverified compatibility SSL context, which would let an
        # impersonating server harvest the iCloud username + app password.
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            logger.warning(
                "SMTP teardown failed after send_message; message acceptance is unaffected",
                exc_info=True,
            )


def archive(body_md: str, archive_dir: str, digest_id: int) -> None:
    """Best-effort archive of the digest markdown to disk.

    Never raises: a failed archive write must not block (or roll back) an
    already-sent digest. Failures are logged as a warning only.
    """
    try:
        directory = Path(archive_dir)
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"digest-{digest_id}-{timestamp}.md"
        # Encoding must be explicit: without it, write_text falls back to
        # locale.getpreferredencoding(), and on an ASCII/code-page locale a
        # digest containing non-ASCII text (e.g. the ⚠ collector-failure
        # banner) raises UnicodeEncodeError -- a ValueError, not an OSError.
        path.write_text(body_md, encoding="utf-8")
    except Exception:
        # Broad on purpose: this function's contract is "never raises" --
        # the digest has already been sent (and, once this returns, gets
        # marked sent) by the time archive() runs, so ANY failure here
        # (OSError from disk/permissions, UnicodeEncodeError from encoding
        # issues, or anything else) must degrade to a logged warning rather
        # than escape and fail an otherwise-successful run.
        logger.warning("failed to archive digest %d to %s", digest_id, archive_dir, exc_info=True)
