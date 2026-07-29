import smtplib
import ssl
from pathlib import Path

import pytest

import digest.emailer as emailer_mod
from digest.emailer import archive, render_html, send_digest

# --- render_html ---


def test_render_html_escapes_hostile_raw_html_from_item_text():
    body_md = (
        "## Worth knowing\n\n"
        "- someone posted <img src=x onerror=alert(1)> in the chat "
        "[see it](https://t.me/c/123/1)\n"
    )

    html = render_html(body_md)

    assert "<img" not in html
    assert "onerror" in html  # content survives, just neutralized
    assert "&lt;img" in html


def test_render_html_still_renders_legit_headers_and_links():
    body_md = "## Needs attention\n\n- [ping from Bob](https://t.me/c/123/42): reply needed\n"

    html = render_html(body_md)

    assert "<h2>Needs attention</h2>" in html
    assert '<a href="https://t.me/c/123/42" rel="noopener noreferrer">ping from Bob</a>' in html


def test_render_html_wraps_in_full_document_with_readable_style():
    html = render_html("hello")

    assert "<html>" in html
    assert "max-width: 42em" in html


def test_render_html_strips_img_from_markdown_image_syntax():
    # `![status](url)` is legitimate markdown, but Claude echoes hostile
    # message text verbatim, so this must not become a tracking-pixel <img>.
    body_md = "## Worth knowing\n\n![status](https://attacker.example/pixel.png)\n"

    html = render_html(body_md)

    assert "<img" not in html


def test_render_html_strips_javascript_scheme_from_markdown_link():
    body_md = "[click](javascript:alert(1))\n"

    html = render_html(body_md)

    assert "javascript:" not in html


def test_render_html_still_renders_legit_link_with_safe_rel():
    body_md = "[see it](https://t.me/c/123/45)\n"

    html = render_html(body_md)

    assert '<a href="https://t.me/c/123/45" rel="noopener noreferrer">see it</a>' in html


# --- send_digest ---


class FakeSMTP:
    """Records call order and constructor args; constructed directly (not as
    a context manager) since send_digest drives starttls/login/send_message/
    quit explicitly rather than via `with smtplib.SMTP(...) as smtp:`.
    """

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.sent_message = None
        self.send_message_error: Exception | None = None
        self.quit_error: Exception | None = None
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.calls.append("starttls")
        self.starttls_context = context

    def login(self, user, password):
        self.calls.append("login")
        self.login_user = user
        self.login_password = password

    def send_message(self, msg):
        self.calls.append("send_message")
        if self.send_message_error is not None:
            raise self.send_message_error
        self.sent_message = msg

    def quit(self):
        self.calls.append("quit")
        if self.quit_error is not None:
            raise self.quit_error


def _fake_smtp_cls(*, send_message_error=None, quit_error=None):
    """Build a FakeSMTP subclass that injects errors on construction.

    send_digest constructs smtplib.SMTP(...) itself, so error injection
    can't happen on an already-built instance -- the monkeypatched class
    has to bake the error in via __init__.
    """

    class _ConfiguredFakeSMTP(FakeSMTP):
        def __init__(self, host, port, timeout=None):
            super().__init__(host, port, timeout)
            self.send_message_error = send_message_error
            self.quit_error = quit_error

    return _ConfiguredFakeSMTP


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    FakeSMTP.instances.clear()
    yield
    FakeSMTP.instances.clear()


def test_send_digest_drives_smtp_in_order_with_correct_headers(monkeypatch):
    monkeypatch.setattr(emailer_mod.smtplib, "SMTP", FakeSMTP)

    send_digest(
        "smtp.mail.me.com",
        587,
        "user@example.com",
        "app-specific-password",
        "digest@4rgus.com",
        "me@toomhorvath.com",
        "digest: 3 items · 2026-07-29 10:00",
        "## Needs attention\n\n- nothing much\n",
    )

    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp.mail.me.com"
    assert smtp.port == 587
    assert smtp.timeout == emailer_mod._SMTP_TIMEOUT_SECONDS
    assert smtp.calls == ["starttls", "login", "send_message", "quit"]
    assert smtp.login_user == "user@example.com"
    assert smtp.login_password == "app-specific-password"

    # STARTTLS must use a verifying context, not the unverified compat
    # default -- otherwise an impersonating server could harvest the login.
    assert isinstance(smtp.starttls_context, ssl.SSLContext)
    assert smtp.starttls_context.verify_mode == ssl.CERT_REQUIRED
    assert smtp.starttls_context.check_hostname is True

    msg = smtp.sent_message
    assert msg["From"] == "digest@4rgus.com"
    assert msg["To"] == "me@toomhorvath.com"
    assert msg["Subject"] == "digest: 3 items · 2026-07-29 10:00"

    content_types = {part.get_content_type() for part in msg.walk()}
    assert "text/plain" in content_types
    assert "text/html" in content_types


def test_send_digest_swallows_quit_error_after_successful_send(monkeypatch, caplog):
    # A non-221 QUIT response (or any teardown error) must not surface as an
    # exception once send_message() has already succeeded -- the mail is
    # queued server-side, and raising here would make the caller leave the
    # digest email_sent=0 and re-send a duplicate on the next run.
    quit_error = smtplib.SMTPResponseException(421, b"timeout")
    monkeypatch.setattr(
        emailer_mod.smtplib, "SMTP", _fake_smtp_cls(quit_error=quit_error)
    )

    with caplog.at_level("WARNING"):
        send_digest(
            "smtp.mail.me.com",
            587,
            "user@example.com",
            "app-specific-password",
            "digest@4rgus.com",
            "me@toomhorvath.com",
            "subject",
            "body",
        )  # must not raise

    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert smtp.calls == ["starttls", "login", "send_message", "quit"]
    assert smtp.sent_message is not None
    assert any("teardown" in record.message.lower() for record in caplog.records)


def test_send_digest_raises_send_message_error_even_if_quit_also_fails(monkeypatch):
    # send_message failing means the mail was never accepted, so the caller
    # must see the error and leave the digest pending for retry. A quit()
    # error on the same teardown must not mask the original failure.
    send_error = smtplib.SMTPRecipientsRefused({"me@toomhorvath.com": (550, b"nope")})
    quit_error = smtplib.SMTPResponseException(421, b"timeout")
    monkeypatch.setattr(
        emailer_mod.smtplib,
        "SMTP",
        _fake_smtp_cls(send_message_error=send_error, quit_error=quit_error),
    )

    with pytest.raises(smtplib.SMTPRecipientsRefused):
        send_digest(
            "smtp.mail.me.com",
            587,
            "user@example.com",
            "app-specific-password",
            "digest@4rgus.com",
            "me@toomhorvath.com",
            "subject",
            "body",
        )

    assert len(FakeSMTP.instances) == 1
    smtp = FakeSMTP.instances[0]
    assert smtp.calls == ["starttls", "login", "send_message", "quit"]


def test_send_digest_never_opens_a_real_socket(monkeypatch):
    # Regression guard: if send_digest is ever changed to call smtplib.SMTP
    # directly without the monkeypatched fake, this raises instead of
    # hanging on a real network connection.
    def _boom(*args, **kwargs):
        raise AssertionError("real smtplib.SMTP must never be constructed in tests")

    monkeypatch.setattr(emailer_mod.smtplib, "SMTP", _boom)

    with pytest.raises(AssertionError):
        send_digest(
            "smtp.mail.me.com", 587, "u", "p", "from@x.com", "to@x.com", "subject", "body"
        )


# --- archive ---


def test_archive_writes_the_markdown_file(tmp_path: Path):
    archive("## Needs attention\n...", str(tmp_path / "archive"), digest_id=42)

    files = list((tmp_path / "archive").glob("digest-42-*.md"))
    assert len(files) == 1
    assert files[0].read_text() == "## Needs attention\n..."


def test_archive_with_unwritable_dir_logs_warning_and_does_not_raise(tmp_path, caplog):
    # A file where a directory is expected makes mkdir(parents=True) raise
    # NotADirectoryError/FileExistsError (both OSError subclasses) --
    # archive() must swallow this, not propagate it.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    bad_archive_dir = str(blocker / "nested")

    with caplog.at_level("WARNING"):
        archive("body", bad_archive_dir, digest_id=1)  # must not raise

    assert any("archive" in record.message.lower() for record in caplog.records)


def test_archive_writes_utf8_and_survives_a_roundtrip(tmp_path: Path):
    # Non-ASCII content (e.g. the digest's warning banner) must be written
    # as UTF-8 explicitly rather than relying on the locale-preferred
    # encoding, which can be ASCII/code-page and unable to represent it.
    body_md = "## Needs attention\n\n- ⚠ one collector failed this run\n"

    archive(body_md, str(tmp_path / "archive"), digest_id=7)

    files = list((tmp_path / "archive").glob("digest-7-*.md"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == body_md
    assert "⚠" in files[0].read_text(encoding="utf-8")


def test_archive_swallows_unicode_encode_error_and_logs_warning(tmp_path, caplog, monkeypatch):
    # write_text() using a non-UTF-8 locale encoding can raise
    # UnicodeEncodeError on non-ASCII digest text -- a ValueError, not an
    # OSError. archive()'s contract is "never raises," so this must be
    # caught too, not just OSError.
    def _boom(self, data, *args, **kwargs):
        raise UnicodeEncodeError("ascii", "⚠", 0, 1, "ordinal not in range(128)")

    monkeypatch.setattr(Path, "write_text", _boom)

    with caplog.at_level("WARNING"):
        archive("## Needs attention\n⚠", str(tmp_path / "archive"), digest_id=9)  # must not raise

    assert any("archive" in record.message.lower() for record in caplog.records)
