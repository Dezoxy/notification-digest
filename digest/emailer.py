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
<!-- Apple/iOS Mail does NOT evaluate `@media (prefers-color-scheme: dark)`
     at all unless the document explicitly opts in via these two meta tags
     -- without them the dark-mode block below is simply never applied, and
     Apple Mail may instead apply its OWN automatic color-inversion heuristic
     on top of the light-mode-only styles, which can look worse than doing
     nothing. `color-scheme` is the standard (also used by Gmail and other
     clients); `supported-color-schemes` is Apple's own legacy name for the
     same thing, kept for older Mail versions that only recognize that one.
     Both are required in practice; neither alone is reliably sufficient
     across Apple Mail versions. -->
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<style>
  /* Mirrors the two meta tags above at the CSS level -- some clients (and
     the in-app Mail preview pane specifically) key dark-mode opt-in off
     this property on the root element rather than (or in addition to) the
     <meta> tags. Belt-and-suspenders for the same Apple Mail opt-in
     requirement described above. */
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    max-width: 42em;
    margin: 0 auto;
    padding: 1em;
    line-height: 1.5;
    color: #1a1a1a;
    background-color: #ffffff;
  }}
  a {{ color: #0a58ca; }}
  h2 {{
    border-bottom: 1px solid #d0d7de;
    padding-bottom: 0.3em;
    margin-top: 1.5em;
  }}
  /* "## Noise skipped" is deliberately NOT muted here: the only pure-CSS
     way to target just that section's content (e.g. `h2:last-of-type + ul`)
     depends on it always being the last h2/ul pair in the document, which
     is fragile -- an extra unknown h2 the model emits, or a future section,
     would silently break the selector or mute the wrong content in an
     email client's often-partial CSS support. Not worth that fragility for
     a cosmetic dimming effect, so this section keeps the same styling as
     the others. -- see prompts/digest.md upgrade notes.
  */
  /* Clients that support prefers-color-scheme (Apple/iOS Mail and others)
     apply this automatically; clients that don't simply ignore the whole
     block and keep the light-mode rules above. */
  @media (prefers-color-scheme: dark) {{
    body {{ background-color: #1a1a1a !important; color: #e8e8e8 !important; }}
    a {{ color: #6ea8fe !important; }}
    h2 {{ border-bottom-color: #3a3a3a !important; }}
    /* The TL;DR and banner callouts below set an explicit light-mode
       foreground inline (readable regardless of mode in clients that strip
       <style>, e.g. Outlook desktop). For clients that DO honor a <style>
       block, override to a dark-mode-appropriate palette instead of
       inheriting the dark `body` foreground on top of an unchanged light
       inline background -- #e8e8e8 on #f0f0f0 (TL;DR's light background) is
       ~1:1 contrast, effectively invisible. */
    .tldr {{ background:#2b2b2b !important; color:#e8e8e8 !important; }}
    .banner {{ background:#4d3800 !important; color:#ffe69c !important; }}
  }}
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


# Matches a single `<h3>...</h3>` heading in nh3-sanitized output. Under the
# CURRENT prompts/digest.md contract, "## Worth knowing" h3 headings name a
# TOPIC the model chose (e.g. "Markets", "Geopolitics") and never start with
# "Telegram"/"X" -- so _inject_source_badges below is a no-op in normal
# operation now. It stays wired in as a harmless fallback in case the model
# ever reverts to the OLDER source-grouped heading form this regex/function
# pair was written for (a bare source name, or the source name followed by
# " -- <group/topic name>", e.g. "Telegram -- Homelab Hungary") -- current
# per-bullet source attribution is handled instead by the inline
# `**[Source/Name]**` tags _inject_inline_source_tags converts.
_H3_HEADING_RE = re.compile(r"<h3>(.*?)</h3>")

# Inline-styled chip spans injected in place of the literal "Telegram"/"X"
# prefix inside a recognized h3 (see _inject_source_badges below). Colors
# are the platforms' own brand colors so the source is recognizable at a
# glance without an image.
_TELEGRAM_CHIP_HTML = (
    '<span style="background-color:#229ED9;color:#ffffff;'
    "border-radius:4px;padding:1px 6px;margin-right:6px;"
    'font-size:0.75em;font-variant:small-caps;letter-spacing:0.02em;">'
    "Telegram</span>"
)
_X_CHIP_HTML = (
    '<span style="background-color:#000000;color:#ffffff;'
    "border-radius:4px;padding:1px 6px;margin-right:6px;"
    'font-size:0.75em;font-weight:bold;">𝕏</span>'
)


def _chip_html_for_source(source: str) -> str:
    """Return the constant chip markup for `source` ("Telegram" or "X").

    Shared by _inject_source_badges (h3 subgroup headings, the legacy
    source-grouped contract) and _inject_inline_source_tags (per-bullet
    inline tags, the current topic-grouped contract) so both places render
    the IDENTICAL chip -- same brand color, same padding/border-radius --
    rather than each hand-rolling its own copy of the inline styles.
    """
    if source == "Telegram":
        return _TELEGRAM_CHIP_HTML
    return _X_CHIP_HTML  # the only other value _INLINE_SOURCE_TAG_RE/_H3_HEADING_RE match


def _inject_source_badges(sanitized_html: str) -> str:
    """Turn a "Telegram"/"X" h3 subgroup heading prefix into a colored chip.

    This MUST run on nh3's OUTPUT, never before sanitization: nh3's tag
    allowlist (_ALLOWED_TAGS above) does not include `span`, so a `<span>`
    written into the markdown before nh3.clean would simply be stripped as
    an unrecognized tag. Running here instead is the same safety argument
    as _enforce_anchor_provenance above: this function only ever injects
    OUR OWN constant markup (the two chip templates above) around text nh3
    already sanitized -- it never re-parses or re-emits anything
    attacker-controlled, so there is nothing here for hostile input to
    subvert.

    Only an h3 whose text starts with exactly "Telegram" or "X" -- the
    literal source name, followed by either nothing or a non-word character
    (a space before " -- Group Name", for instance) -- gets a chip. This is
    a `\\b` word-boundary check specifically so "Telegram" doesn't also
    match some hypothetical "Telegramish" heading, and "X" doesn't match
    "XAI" or similar: both would share the literal prefix but are a
    different word, not this source. Any other h3 (a heading the model
    invented, or one of the required `## `-level sections misrendered as
    h3, though that shouldn't happen per the prompt contract) is returned
    unchanged.
    """

    def _replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if re.match(r"^Telegram\b", inner):
            return f"<h3>{_TELEGRAM_CHIP_HTML}{inner[len('Telegram') :]}</h3>"
        if re.match(r"^X\b", inner):
            return f"<h3>{_X_CHIP_HTML}{inner[len('X') :]}</h3>"
        return match.group(0)

    return _H3_HEADING_RE.sub(_replace, sanitized_html)


# Matches an inline per-bullet source tag the topic-grouped "## Worth
# knowing" contract emits (prompts/digest.md): `**[Telegram/<chat_title>]**`
# or `**[X/@<handle>]**`, markdown emphasis around a `[Source/Name]` bracket
# that python-markdown renders as `<strong>[Source/Name]</strong>`. A bullet
# confirmed by multiple sources carries several of these tags in a row,
# space-separated -- re.sub's default (replace all non-overlapping matches,
# left to right) converts every one of them, not just the first.
#
# Only the two literal source names this codebase actually collects from
# ("Telegram", "X") are recognized. Anything else -- an invented
# `[Slack/foo]`, say -- is left completely untouched: there is no third
# chip color/glyph to render it with, and inventing one would misleadingly
# imply Slack is a source this digest collects from.
_INLINE_SOURCE_TAG_RE = re.compile(r"<strong>\[(Telegram|X)/([^\]]{1,80})\]</strong>")


def _inject_inline_source_tags(sanitized_html: str) -> str:
    """Turn an inline `**[Telegram/<name>]**` / `**[X/<handle>]**` bullet tag into a chip.

    The current "## Worth knowing" contract groups items by TOPIC, not
    source (prompts/digest.md), so the per-subgroup h3 heading
    _inject_source_badges targets no longer identifies an individual
    bullet's source -- and a bullet merging several sources' reports of the
    same story has no single h3 to live under at all. The model instead
    tags each bullet inline, one `**[Source/Name]**` marker per contributing
    source, right at the start of the bullet text.

    MUST run on nh3's OUTPUT, never before sanitization -- same reason as
    _inject_source_badges: nh3's tag allowlist (_ALLOWED_TAGS) has no
    `span`, so a `<span>` written into the markdown pre-sanitization would
    just be stripped as an unrecognized tag. This function only ever injects
    OUR OWN constant markup (the chip templates, via _chip_html_for_source)
    around text nh3 already sanitized -- it never re-parses or re-emits
    anything attacker-controlled -- so, like the other cosmetic passes
    here, there is nothing for hostile input to subvert.

    Only a `<strong>` whose ENTIRE content is exactly `[Telegram/...]` or
    `[X/...]` is converted. Any other `<strong>` -- genuine emphasis inside
    a mini-brief, or a tag naming a source this codebase doesn't collect
    from -- is left completely untouched, matching _inject_source_badges'
    same conservative default of "not a recognized source name -> don't
    touch it."
    """

    def _replace(match: re.Match[str]) -> str:
        source, name = match.group(1), match.group(2)
        chip = _chip_html_for_source(source)
        return (
            f"{chip}"
            '<span style="color:#6e7781;font-size:0.85em;margin-right:6px;">'
            f"{name}</span>"
        )

    return _INLINE_SOURCE_TAG_RE.sub(_replace, sanitized_html)


# Matches the leading `<p>` paragraph if (and only if) it starts with the
# ⚠ collector-failure banner text summarize.summarize() deterministically
# prepends (see that function's docstring -- the banner is code-generated,
# never model-written). count=1 in the caller below means only this FIRST
# paragraph is ever considered, so nothing downstream in "Worth knowing" or
# elsewhere that happens to start a line with ⚠ (e.g. quoted hostile
# message text) gets caught by this.
_BANNER_PARAGRAPH_RE = re.compile(r"<p>(⚠[\s\S]*?)</p>")


def _wrap_banner_paragraph(sanitized_html: str) -> str:
    """Style the leading collector-failure banner paragraph as a warning callout.

    One or more `⚠ <source> collection failed this run` lines (one per
    failed source) render as a single `<p>` with the lines joined by a
    literal newline -- python-markdown does not insert `<br>` between
    consecutive lines of the same paragraph by default. Those embedded
    newlines are converted to `<br>` here so multiple banner lines still
    read as separate lines instead of running together with only
    whitespace between them.

    A no-op (returns the input unchanged) when there is no such paragraph,
    which is the common case (no failed collectors that run).

    The inline style already sets an explicit foreground (#664d03 on
    #fff3cd) that stays readable in any client, dark-mode or not -- audited
    alongside the TL;DR contrast fix (see _highlight_tldr_paragraph) and
    left as-is. The `class="banner"` attribute added here exists only so
    clients that DO honor the document's <style> block (rather than just
    inheriting this inline style) get a dark-mode-appropriate override too
    -- see the `.banner` rule in _HTML_TEMPLATE's dark-mode media query.
    """

    def _replace(match: re.Match[str]) -> str:
        content = match.group(1).replace("\n", "<br>")
        return (
            '<p class="banner" style="background-color:#fff3cd;color:#664d03;'
            'padding:0.75em 1em;border-radius:6px;margin:0 0 1em 0;">'
            f"{content}</p>"
        )

    return _BANNER_PARAGRAPH_RE.sub(_replace, sanitized_html, count=1)


# Matches the first `<p>` whose content starts with the literal
# `<strong>TL;DR` prefix python-markdown produces from the prompt
# contract's `**TL;DR:** ...` paragraph (prompts/digest.md). Matching this
# literal, post-nh3 prefix -- rather than trying to recognize "the first
# paragraph" positionally -- means this is unaffected by whether a banner
# paragraph precedes it: the banner's content never starts with this
# prefix, so the regex naturally skips past it to find the real TL;DR
# paragraph (or finds nothing, if the model omitted TL;DR entirely, which
# is a content-quality issue for the prompt to police, not something this
# rendering step raises an error over).
_TLDR_PARAGRAPH_RE = re.compile(r"<p>(<strong>TL;DR[\s\S]*?)</p>")


def _highlight_tldr_paragraph(sanitized_html: str) -> str:
    """Give the TL;DR paragraph a subtle highlight so it stands out at a glance.

    A no-op (returns the input unchanged) when there is no such paragraph --
    e.g. a digest that predates this prompt change, or a rerun of stored
    archive markdown -- so absence is fine, never an error.

    P2 fix: the original inline style set only `background-color:#f0f0f0`
    with no explicit foreground, so this paragraph inherited whatever
    foreground was in scope -- in a client that honors
    `@media (prefers-color-scheme: dark)`, that's the dark-mode `body`
    foreground (#e8e8e8) from _HTML_TEMPLATE, landing #e8e8e8 text on an
    unchanged light #f0f0f0 background: roughly 1:1 contrast, effectively
    unreadable. `color:#1a1a1a` is now set explicitly inline so this
    paragraph reads correctly in ANY mode, including clients that strip
    `<style>` blocks entirely (inline styles survive those). The
    `class="tldr"` attribute is added on top for clients that DO honor the
    document's `<style>` block: see the `.tldr` dark-mode override in
    _HTML_TEMPLATE, which swaps to a dark-appropriate background/foreground
    pair instead of relying on the light-mode inline style everywhere.
    """

    def _replace(match: re.Match[str]) -> str:
        return (
            '<p class="tldr" style="background-color:#f0f0f0;color:#1a1a1a;'
            'padding:0.75em 1em;border-radius:6px;margin:0 0 1em 0;">'
            f"{match.group(1)}</p>"
        )

    return _TLDR_PARAGRAPH_RE.sub(_replace, sanitized_html, count=1)


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

    After the security-critical passes above, four purely cosmetic passes
    run over the same nh3-sanitized HTML: _inject_source_badges (Telegram/X
    chips on a legacy subgroup h3 heading, if the model still emits one),
    _inject_inline_source_tags (the same chips on the current topic-grouped
    contract's per-bullet inline `**[Source/Name]**` tags),
    _wrap_banner_paragraph (styles the collector-failure banner, if
    present), and _highlight_tldr_paragraph (styles the TL;DR paragraph, if
    present). All four follow the same safety pattern as
    _enforce_anchor_provenance -- they only ever inject this module's own
    constant markup around text nh3 already sanitized, never re-parse or
    re-emit attacker-controlled HTML -- and each is a no-op when its target
    isn't present, so none of them can raise or change behavior for a
    digest that doesn't happen to contain a banner, a TL;DR paragraph, a
    Telegram/X subgroup heading, or an inline source tag.
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
    sanitized = _inject_source_badges(sanitized)
    sanitized = _inject_inline_source_tags(sanitized)
    sanitized = _wrap_banner_paragraph(sanitized)
    sanitized = _highlight_tldr_paragraph(sanitized)
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
