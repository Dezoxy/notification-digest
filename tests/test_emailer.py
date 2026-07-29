import smtplib
import ssl
from pathlib import Path

import pytest

import digest.emailer as emailer_mod
from digest.emailer import (
    _highlight_tldr_paragraph,
    _inject_source_badges,
    _wrap_banner_paragraph,
    archive,
    render_html,
    send_digest,
)

# --- render_html ---


def test_render_html_escapes_hostile_raw_html_from_item_text():
    body_md = (
        "## Worth knowing\n\n"
        "- someone posted <img src=x onerror=alert(1)> in the chat "
        "[see it](https://t.me/c/123/1)\n"
    )

    html = render_html(body_md, allowed_urls={"https://t.me/c/123/1"})

    assert "<img" not in html
    assert "onerror" in html  # content survives, just neutralized
    assert "&lt;img" in html


def test_render_html_still_renders_legit_headers_and_links():
    body_md = "## Needs attention\n\n- [ping from Bob](https://t.me/c/123/42): reply needed\n"

    html = render_html(body_md, allowed_urls={"https://t.me/c/123/42"})

    assert "<h2>Needs attention</h2>" in html
    assert '<a href="https://t.me/c/123/42" rel="noopener noreferrer">ping from Bob</a>' in html


def test_render_html_wraps_in_full_document_with_readable_style():
    html = render_html("hello", allowed_urls=set())

    assert "<html>" in html
    assert "max-width: 42em" in html


def test_render_html_strips_img_from_markdown_image_syntax():
    # `![status](url)` is legitimate markdown, but Claude echoes hostile
    # message text verbatim, so this must not become a tracking-pixel <img>.
    body_md = "## Worth knowing\n\n![status](https://attacker.example/pixel.png)\n"

    html = render_html(body_md, allowed_urls=set())

    assert "<img" not in html


def test_render_html_strips_javascript_scheme_from_markdown_link():
    body_md = "[click](javascript:alert(1))\n"

    html = render_html(body_md, allowed_urls=set())

    assert "javascript:" not in html


def test_render_html_still_renders_legit_link_with_safe_rel():
    body_md = "[see it](https://t.me/c/123/45)\n"

    html = render_html(body_md, allowed_urls={"https://t.me/c/123/45"})

    assert '<a href="https://t.me/c/123/45" rel="noopener noreferrer">see it</a>' in html


# --- render_html: HTML-level anchor-provenance (P1 fix) ---


def test_render_html_unwraps_anchor_with_nested_bracket_label_markdown_regex_would_miss():
    # P1 finding: `[review [urgent]](https://attacker.example/phish)` has
    # balanced nested brackets in its label. CommonMark's link-label grammar
    # (which python-markdown implements) renders this as a real anchor, but
    # a markdown-level regex like `\[([^\]]*)\]\(...\)` (see
    # summarize.enforce_link_allowlist's _MARKDOWN_LINK_RE) stops at the
    # FIRST `]` and never recognizes this as a link at all -- the attacker
    # URL would sail through untouched at that layer. Feeding this straight
    # into render_html as raw markdown (bypassing enforce_link_allowlist
    # entirely) simulates exactly that miss: the HTML-level anchor-
    # provenance check must still catch it, because it inspects what the
    # renderer actually produced rather than re-deriving its grammar.
    body_md = "[review [urgent]](https://attacker.example/phish)\n"

    html = render_html(body_md, allowed_urls=set())

    assert "attacker.example" not in html
    assert "<a " not in html
    # The label text survives, just unwrapped from the anchor.
    assert "review" in html
    assert "urgent" in html


def test_render_html_keeps_allowlisted_anchor_intact():
    body_md = "[see it](https://t.me/c/123/99)\n"

    html = render_html(body_md, allowed_urls={"https://t.me/c/123/99"})

    assert '<a href="https://t.me/c/123/99" rel="noopener noreferrer">see it</a>' in html


def test_render_html_keeps_allowlisted_url_containing_ampersand_after_entity_roundtrip():
    # nh3 entity-escapes hrefs (`&` becomes `&amp;` in the sanitized HTML).
    # The anchor-provenance check must html.unescape() the href before
    # comparing against the allowlist (which holds the raw, unescaped URL),
    # or a legitimate allowlisted link containing `&` would be wrongly
    # unwrapped as if its URL were unknown.
    url = "https://t.me/c/1/2?a=1&b=2"
    body_md = f"[see it]({url})\n"

    html = render_html(body_md, allowed_urls={url})

    assert 'href="https://t.me/c/1/2?a=1&amp;b=2"' in html
    assert "<a " in html
    assert "see it</a>" in html


# --- _inject_source_badges ---


def test_inject_source_badges_wraps_telegram_group_heading():
    html = "<h3>Telegram — Homelab Hungary</h3>"

    result = _inject_source_badges(html)

    assert "background-color:#229ED9" in result
    assert ">Telegram</span>" in result
    assert "— Homelab Hungary</h3>" in result
    # The literal "Telegram" prefix must not also survive un-chipped outside
    # the span (it should appear exactly once, inside the chip).
    assert result.count("Telegram") == 1


def test_inject_source_badges_wraps_bare_x_heading():
    html = "<h3>X</h3>"

    result = _inject_source_badges(html)

    assert "background-color:#000000" in result
    assert "𝕏</span>" in result
    assert result.endswith("</h3>")


def test_inject_source_badges_wraps_x_heading_with_topic():
    html = "<h3>X — Some Topic</h3>"

    result = _inject_source_badges(html)

    assert "𝕏</span>" in result
    assert "— Some Topic</h3>" in result


def test_inject_source_badges_leaves_non_source_h3_untouched():
    html = "<h3>Random Section</h3>"

    result = _inject_source_badges(html)

    assert result == html


def test_inject_source_badges_does_not_match_prefix_that_is_a_different_word():
    # "Xavier" and "Telegramish" share a literal prefix with "X"/"Telegram"
    # but are a different word entirely -- must not get chipped.
    html = "<h3>Xavier's update</h3><h3>Telegramish thing</h3>"

    result = _inject_source_badges(html)

    assert result == html


def test_inject_source_badges_ignores_h2_headings():
    html = "<h2>Telegram</h2>"

    result = _inject_source_badges(html)

    assert result == html


# --- _wrap_banner_paragraph ---


def test_wrap_banner_paragraph_styles_leading_warning_paragraph():
    html = "<p>⚠ telegram collection failed this run</p>\n<h2>Needs attention</h2>"

    result = _wrap_banner_paragraph(html)

    assert "background-color:#fff3cd" in result
    assert "color:#664d03" in result
    assert "⚠ telegram collection failed this run" in result
    assert "<h2>Needs attention</h2>" in result


def test_wrap_banner_paragraph_converts_embedded_newlines_between_multiple_sources():
    # Multiple failed-source banner lines collapse into ONE <p> joined by a
    # literal "\n" (python-markdown does not insert <br> between lines of
    # the same paragraph) -- those must become <br> so they still read as
    # separate lines.
    html = "<p>⚠ telegram collection failed this run\n⚠ x collection failed this run</p>"

    result = _wrap_banner_paragraph(html)

    assert "<br>" in result
    assert "\n" not in result


def test_wrap_banner_paragraph_has_banner_class_for_dark_mode_override():
    html = "<p>⚠ telegram collection failed this run</p>"

    result = _wrap_banner_paragraph(html)

    assert 'class="banner"' in result
    # The existing explicit inline foreground/background must be untouched.
    assert "background-color:#fff3cd" in result
    assert "color:#664d03" in result


def test_wrap_banner_paragraph_absent_is_a_noop():
    html = "<p><strong>TL;DR:</strong> quiet day.</p><h2>Needs attention</h2>"

    result = _wrap_banner_paragraph(html)

    assert result == html


# --- _highlight_tldr_paragraph ---


def test_highlight_tldr_paragraph_styles_when_present():
    html = "<p><strong>TL;DR:</strong> quiet day, nothing urgent.</p><h2>Needs attention</h2>"

    result = _highlight_tldr_paragraph(html)

    assert "background-color:#f0f0f0" in result
    assert "<strong>TL;DR:</strong> quiet day, nothing urgent." in result
    assert "<h2>Needs attention</h2>" in result


def test_highlight_tldr_paragraph_sets_explicit_readable_color():
    # P2 finding: the original inline style set only a light background
    # (#f0f0f0) with no explicit foreground, so in a client that honors
    # `prefers-color-scheme: dark` this paragraph inherited the dark-mode
    # body foreground (#e8e8e8) -- landing #e8e8e8 text on an unchanged
    # light #f0f0f0 background, ~1:1 contrast. An explicit inline color must
    # be set so this paragraph reads correctly in any client, including one
    # that strips <style> blocks (inline styles survive that).
    html = "<p><strong>TL;DR:</strong> quiet day, nothing urgent.</p>"

    result = _highlight_tldr_paragraph(html)

    assert "color:#1a1a1a" in result


def test_highlight_tldr_paragraph_has_tldr_class_for_dark_mode_override():
    html = "<p><strong>TL;DR:</strong> quiet day.</p>"

    result = _highlight_tldr_paragraph(html)

    assert 'class="tldr"' in result


def test_highlight_tldr_paragraph_absent_is_a_noop():
    html = "<p>just a regular paragraph</p><h2>Needs attention</h2>"

    result = _highlight_tldr_paragraph(html)

    assert result == html


def test_highlight_tldr_paragraph_skips_past_a_leading_banner_paragraph():
    html = (
        "<p>⚠ telegram collection failed this run</p>"
        "<p><strong>TL;DR:</strong> quiet day.</p>"
        "<h2>Needs attention</h2>"
    )

    result = _highlight_tldr_paragraph(html)

    assert "background-color:#f0f0f0" in result
    # The banner paragraph itself must be untouched by this pass.
    assert "<p>⚠ telegram collection failed this run</p>" in result


# --- render_html: full integration of the visual-upgrade passes ---


def test_render_html_integration_all_visual_passes_coexist_with_anchor_provenance():
    body_md = (
        "⚠ telegram collection failed this run\n\n"
        "**TL;DR:** Homelab discussion wrapped up, one X thread flagged for review.\n\n"
        "## Needs attention\n\n"
        "- [reply to Bob](https://t.me/c/123/1): confirm the maintenance window\n\n"
        "## Worth knowing\n\n"
        "### Telegram — Homelab Hungary\n\n"
        "- [see the thread](https://t.me/c/123/2): the group agreed to move the "
        "backup job to 3am after discussing disk contention.\n"
        "- [phishing attempt](https://attacker.example/phish): someone posted a "
        "suspicious link, not from an allowlisted item.\n\n"
        "### X\n\n"
        "- [see the post](https://x.com/foo/status/1): announcement of a new release.\n\n"
        "## Noise skipped\n\n"
        "- a handful of low-signal reaction messages were filtered.\n"
    )
    allowed_urls = {
        "https://t.me/c/123/1",
        "https://t.me/c/123/2",
        "https://x.com/foo/status/1",
    }

    html = render_html(body_md, allowed_urls)

    # Banner styling applied.
    assert "background-color:#fff3cd" in html
    assert "⚠ telegram collection failed this run" in html
    # TL;DR highlight applied.
    assert "background-color:#f0f0f0" in html
    assert "Homelab discussion wrapped up" in html
    # Telegram and X chips both present.
    assert "background-color:#229ED9" in html
    assert ">Telegram</span>" in html
    assert "background-color:#000000" in html
    assert "𝕏</span>" in html
    # Allowed links survive as real anchors (anchor-provenance pass intact).
    assert 'href="https://t.me/c/123/1"' in html
    assert 'href="https://t.me/c/123/2"' in html
    assert 'href="https://x.com/foo/status/1"' in html
    # The non-allowlisted link is unwrapped, never a live href.
    assert "attacker.example" not in html
    assert "phishing attempt" in html
    # Dark-mode media query present in the document.
    assert "prefers-color-scheme: dark" in html


def test_render_html_dark_mode_block_has_tldr_and_banner_class_overrides():
    # P2 finding: the TL;DR and banner callouts need dark-mode-specific
    # class overrides in the <style> block, for clients that honor it
    # (rather than relying solely on the inline light-mode style, which the
    # separate inline `color:#1a1a1a` addition covers for clients that
    # strip <style> entirely).
    body_md = (
        "⚠ telegram collection failed this run\n\n"
        "**TL;DR:** quiet day.\n\n"
        "## Needs attention\n- nothing\n\n"
        "## Worth knowing\n- nothing\n\n"
        "## Noise skipped\n- nothing\n"
    )

    html = render_html(body_md, allowed_urls=set())

    dark_block = html[html.index("prefers-color-scheme: dark") :]
    assert ".tldr" in dark_block
    assert ".banner" in dark_block


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
        set(),
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
            set(),
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
            set(),
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
            "smtp.mail.me.com", 587, "u", "p", "from@x.com", "to@x.com", "subject", "body", set()
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
