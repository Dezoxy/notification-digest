"""Renders the digest markdown to HTML, sends it via SMTP, and archives it to disk.

See PLAN.md §4.5. Stdlib smtplib + the `markdown` package only -- no
templating framework needed for a handful of emails a day.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
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


def render_html(body_md: str) -> str:
    """Convert digest markdown to a self-contained HTML document.

    Two distinct threats are neutralized here, in two different layers:

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
       `<img>` and non-http(s) URL schemes) -- this is the authority for
       markdown-generated content, with the pre-escape acting as defense in
       depth for raw HTML.
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
) -> None:
    """Send the digest as a multipart (plain + HTML) email over SMTP with STARTTLS.

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
    msg.attach(MIMEText(render_html(body_md), "html"))

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
        path.write_text(body_md)
    except OSError:
        logger.warning("failed to archive digest %d to %s", digest_id, archive_dir, exc_info=True)
