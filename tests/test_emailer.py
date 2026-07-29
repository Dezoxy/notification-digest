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
    assert '<a href="https://t.me/c/123/42">ping from Bob</a>' in html


def test_render_html_wraps_in_full_document_with_readable_style():
    html = render_html("hello")

    assert "<html>" in html
    assert "max-width: 42em" in html


# --- send_digest ---


class FakeSMTP:
    """Records call order and constructor args; supports the `with` protocol."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.sent_message = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append("login")
        self.login_user = user
        self.login_password = password

    def send_message(self, msg):
        self.calls.append("send_message")
        self.sent_message = msg


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
    assert smtp.calls == ["starttls", "login", "send_message"]
    assert smtp.login_user == "user@example.com"
    assert smtp.login_password == "app-specific-password"

    msg = smtp.sent_message
    assert msg["From"] == "digest@4rgus.com"
    assert msg["To"] == "me@toomhorvath.com"
    assert msg["Subject"] == "digest: 3 items · 2026-07-29 10:00"

    content_types = {part.get_content_type() for part in msg.walk()}
    assert "text/plain" in content_types
    assert "text/html" in content_types


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
