"""Renders the digest markdown to HTML, sends it via SMTP, and archives it to disk.

See PLAN.md §4.5. Stdlib smtplib + the `markdown` package only -- no
templating framework needed for a handful of emails a day.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import markdown

logger = logging.getLogger(__name__)

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


def render_html(body_md: str) -> str:
    """Convert digest markdown to a self-contained HTML document.

    `&` and `<` are entity-escaped BEFORE markdown conversion. This is what
    neutralizes any raw HTML that Claude might echo verbatim from hostile
    message text (e.g. a smuggled `<img onerror=...>`) -- PLAN.md §5
    requires that raw HTML never survive into the rendered email, since it
    renders directly in the owner's mail client. Escaping `<` turns any raw
    tag into inert text (`&lt;img ...&gt;`) before the markdown converter
    ever sees it, while legitimate markdown syntax (`# heading`, `[text](url)`,
    etc.) contains neither character and is unaffected.
    """
    escaped = body_md.replace("&", "&amp;").replace("<", "&lt;")
    body_html = markdown.markdown(escaped)
    return _HTML_TEMPLATE.format(body=body_html)


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

    Raises on any failure -- the caller is responsible for leaving the
    digest row unsent (email_sent=0) so the next run retries the send
    without re-summarizing (PLAN.md §4.1).
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = digest_from
    msg["To"] = digest_to
    msg.attach(MIMEText(body_md, "plain"))
    msg.attach(MIMEText(render_html(body_md), "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)


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
