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
from email.utils import formataddr, formatdate, make_msgid, parseaddr
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
    line-height: 1.55;
    color: #1f2430;
    background-color: #ffffff;
  }}
  a {{ color: #4f46e5; }}
  h2 {{
    font-size: 1.1em;
    line-height: 1.3;
    margin: 1.6em 0 0.5em;
    padding-left: 10px;
    border-left: 3px solid #4f46e5;
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
  /* The masthead is template-level markup (below, directly above {{body}})
     rather than a post-sanitize pass -- it never contains digest content,
     so there's no sanitization boundary to cross here. Same
     belt-and-suspenders pattern as the rest of this file though: an inline
     style on the element itself for clients that strip <style> blocks,
     plus this class-keyed rule (and, for masthead-brand, the dark-mode
     override further down) for clients that honor <style>. */
  .masthead {{ margin: 0 0 14px; }}
  .masthead-brand {{
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: #4f46e5;
  }}
  /* Everything below styles markup injected by this module's post-sanitize
     regex passes (_style_citation_anchors, _wrap_needs_attention_section,
     _highlight_tldr_paragraph, _style_footer_line) -- see render_html's
     docstring for the full pipeline and ordering. Each pass already sets a
     matching inline style directly on the element for clients that strip
     <style> blocks entirely (e.g. Outlook desktop); these rules are for
     clients that DO honor <style>, and exist alongside the dark-mode
     overrides further down rather than replacing the inline styles. */
  .cite {{
    display: inline-block;
    text-decoration: none;
    font-size: 0.75em;
    font-weight: 600;
    line-height: 1.4;
    padding: 0 6px;
    border-radius: 999px;
    margin: 0 1px;
    vertical-align: 2px;
    background-color: #eef2ff;
    color: #4f46e5;
  }}
  .attention {{
    background-color: #fef3c7;
    color: #78350f;
    padding: 0.85em 1em;
    border-radius: 10px;
    margin: 0 0 1em 0;
  }}
  .attention-label {{
    display: block;
    font-size: 0.72em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 0.35em;
    color: #b45309;
  }}
  /* Cosmetic-only descendant rules: there is no inline-style equivalent (an
     inline style can't target a *descendant* element), so these have no
     effect at all in a client that strips <style> -- acceptable, since they
     only refine spacing/citation-pill contrast inside an already-styled,
     already-readable callout, never carry a legibility requirement of their
     own. */
  .attention p {{ margin: 0.4em 0 0; }}
  .attention .cite {{ background-color: #fde68a; color: #92400e; }}
  .tldr {{
    background-color: #eef2ff;
    color: #262a49;
    padding: 0.85em 1em;
    border-radius: 10px;
    margin: 0 0 1em 0;
  }}
  .tldr-label {{
    display: block;
    font-size: 0.72em;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 0.35em;
    color: #4f46e5;
  }}
  .tldr .cite {{ background-color: #dde3ff; color: #4338ca; }}
  .foot {{
    margin-top: 1.6em;
    padding-top: 0.8em;
    border-top: 1px solid #e5e7eb;
    font-size: 0.85em;
    color: #8a8f9e;
  }}
  /* Clients that support prefers-color-scheme (Apple/iOS Mail and others)
     apply this automatically; clients that don't simply ignore the whole
     block and keep the light-mode rules above. */
  @media (prefers-color-scheme: dark) {{
    body {{ background-color:#17181c !important; color:#e6e6ea !important; }}
    a {{ color:#a5b4fc !important; }}
    h2 {{ border-left-color:#6366f1 !important; }}
    /* The banner/attention/TL;DR callouts and the footer line below all set
       an explicit light-mode foreground inline (readable regardless of mode
       in clients that strip <style>, e.g. Outlook desktop). For clients
       that DO honor a <style> block, override each to a dark-mode-
       appropriate palette instead of inheriting the dark `body` foreground
       on top of an unchanged light inline background, which can drop
       contrast to near-1:1 and make the text effectively invisible. */
    .banner {{ background:#4d3800 !important; color:#ffe69c !important; }}
    .tldr {{ background:#262841 !important; color:#dfe3ff !important; }}
    .tldr-label {{ color:#a5b4fc !important; }}
    .attention {{ background:#3a2e08 !important; color:#fcd34d !important; }}
    .attention-label {{ color:#f59e0b !important; }}
    .cite {{ background-color:#2b2d4a !important; color:#a5b4fc !important; }}
    .attention .cite {{ background-color:#57430c !important; color:#fde68a !important; }}
    .tldr .cite {{ background-color:#34376a !important; color:#c7d2fe !important; }}
    .foot {{ border-top-color:#33343c !important; color:#8b8f9c !important; }}
    .masthead-brand {{ color:#a5b4fc !important; }}
    .masthead-when {{ color:#8b8f9c !important; }}
    .masthead-rule {{ background-color:#6366f1 !important; background-image:none !important; }}
  }}
</style>
</head>
<body>
<div class="masthead" style="margin: 0 0 14px;">
<span
  class="masthead-brand"
  style="font-size:12px;font-weight:700;letter-spacing:0.14em;text-transform:uppercase;
    color:#4f46e5;"
>Digest</span>
<span
  class="masthead-when"
  style="font-size:12px;float:right;color:#8a8f9e;"
>{generated_at}</span>
<div
  class="masthead-rule"
  style="clear:both;height:3px;border-radius:2px;margin-top:8px;
    background-color:#4f46e5;background-image:linear-gradient(90deg,#4f46e5,#a5b4fc);"
></div>
</div>
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
            'padding:0.75em 1em;border-radius:10px;margin:0 0 1em 0;">'
            f"{content}</p>"
        )

    return _BANNER_PARAGRAPH_RE.sub(_replace, sanitized_html, count=1)


# Matches `<h2>Needs attention</h2>` (the literal heading python-markdown
# produces from the prompt contract's `## Needs attention` section --
# prompts/digest.md) plus everything after it up to (not including) the next
# `<h2>`, the TL;DR paragraph, or the end of the document, whichever comes
# first. `re.DOTALL` is required for `.` to cross the `<p>...</p>` block
# boundaries between the heading and whatever follows; `\Z` (not `$`)
# anchors the "end of document" branch to the true end of the string rather
# than merely the end of a line, which matters if the sanitized HTML
# happens to end in a trailing newline.
#
# The `<p><strong>TL;DR` branch in the lookahead exists because the prompt
# contract places this section directly ABOVE the TL;DR paragraph with NO
# heading in between (prompts/digest.md: "put it FIRST ... ABOVE the TL;DR
# paragraph") -- without this branch, a document containing both would have
# nothing but "the next `<h2>`" to stop at, which is the FIRST REAL section
# heading further down, so this pass would swallow the still-unprocessed
# TL;DR paragraph into the captured content. _highlight_tldr_paragraph (which
# runs after this pass) would then still find and wrap that paragraph
# wherever it ended up, nesting a `.tldr` card inside this `.attention` card
# instead of the two rendering as siblings. Stopping the lookahead at the
# TL;DR paragraph's own opening tag keeps the two cards siblings regardless
# of whether both are present in the same digest.
_NEEDS_ATTENTION_RE = re.compile(
    r"<h2>Needs attention</h2>(.*?)(?=<h2>|<p><strong>TL;DR|\Z)", re.DOTALL
)


def _wrap_needs_attention_section(sanitized_html: str) -> str:
    """Style the optional "Needs attention" section as a standout callout card.

    A no-op (returns the input unchanged) when there is no such heading --
    the common case, since the prompt contract omits this section entirely
    when nothing needs the reader's action (prompts/digest.md never emits an
    empty heading). `count=1` on the substitution below means only the FIRST
    match is ever transformed: combined with the regex's non-capturing
    lookahead stopping at the next `<h2>` (see _NEEDS_ATTENTION_RE), this
    guarantees the section that follows is left completely untouched even in
    the pathological case of the model emitting a second, later heading that
    happens to read "Needs attention" verbatim -- only the section that is
    actually first in the document gets wrapped.

    The label span reuses the same visual language (uppercase, letter-
    spaced, small-caps-style label above the content) as
    _highlight_tldr_paragraph's card below, so the two read as one family of
    callouts rather than two different designs -- see that function's
    docstring for why the shapes match.
    """

    def _replace(match: re.Match[str]) -> str:
        content = match.group(1)
        return (
            '<div class="attention" style="background-color:#fef3c7;color:#78350f;'
            'padding:0.85em 1em;border-radius:10px;margin:0 0 1em 0;">'
            '<span class="attention-label" style="display:block;font-size:0.72em;'
            "font-weight:700;letter-spacing:0.1em;text-transform:uppercase;"
            'margin-bottom:0.35em;color:#b45309;">⚑ Needs attention</span>'
            f"{content}</div>"
        )

    return _NEEDS_ATTENTION_RE.sub(_replace, sanitized_html, count=1)


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

# Strips the leading `**TL;DR:**`/`**TL;DR**:` prefix (as rendered by
# python-markdown: `<strong>TL;DR:</strong>` or `<strong>TL;DR</strong>:`,
# either optionally followed by whitespace) from the captured TL;DR
# paragraph content, because the card _highlight_tldr_paragraph wraps it in
# below carries its own "TL;DR" label -- leaving the original prefix in
# would show the word twice. Both colon placements are handled since
# neither is more "correct" markdown for `**TL;DR:** ...` than the other in
# practice.
_TLDR_PREFIX_RE = re.compile(r"^<strong>TL;DR:?</strong>:?\s*")


def _highlight_tldr_paragraph(sanitized_html: str) -> str:
    """Style the TL;DR paragraph as a standout callout card matching "Needs attention".

    A no-op (returns the input unchanged) when there is no such paragraph --
    e.g. a digest that predates this prompt change, or a rerun of stored
    archive markdown -- so absence is fine, never an error.

    The original `**TL;DR:**` prefix is stripped from the content before
    it's placed in the card (see _TLDR_PREFIX_RE) since the card's own
    `tldr-label` span replaces it -- if the strip regex doesn't match (an
    unexpected shape, e.g. a future prompt-contract change to this
    paragraph's exact wording), the content is kept as-is rather than
    dropped or raising, so a mismatch degrades to "TL;DR" appearing twice
    rather than losing the paragraph's content.

    P2 contrast fix (carried over from the original inline-only version of
    this pass): the card's inline style sets an explicit foreground
    (`color:#262a49`) alongside its background, so it reads correctly in
    ANY client, including one that strips `<style>` blocks entirely (inline
    styles survive that; a client relying solely on inherited body color
    would otherwise land dark-mode body text on an unchanged light card
    background, or vice versa, at near-1:1 contrast). The `class="tldr"`
    attribute is added on top for clients that DO honor the document's
    `<style>` block: see the `.tldr` dark-mode override in _HTML_TEMPLATE,
    which swaps to a dark-appropriate background/foreground pair instead of
    relying on the light-mode inline style everywhere.
    """

    def _replace(match: re.Match[str]) -> str:
        content = _TLDR_PREFIX_RE.sub("", match.group(1), count=1)
        return (
            '<div class="tldr" style="background-color:#eef2ff;color:#262a49;'
            'padding:0.85em 1em;border-radius:10px;margin:0 0 1em 0;">'
            '<span class="tldr-label" style="display:block;font-size:0.72em;'
            "font-weight:700;letter-spacing:0.1em;text-transform:uppercase;"
            'margin-bottom:0.35em;color:#4f46e5;">TL;DR</span>'
            f"{content}</div>"
        )

    return _TLDR_PARAGRAPH_RE.sub(_replace, sanitized_html, count=1)


# A citation's ENTIRE visible text is one or more superscript-digit
# characters and nothing else (prompts/digest.md: `[¹](url)`, `[¹⁰](url)`,
# etc.) -- `fullmatch` against this (not `search`/`match`) is what makes
# _style_citation_anchors reject anything that isn't purely that: extra
# characters before/after, or nested tags (which would introduce characters
# outside this set, e.g. `<` from a nested `<em>`), both disqualify it.
_SUPERSCRIPT_ONLY_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+")


def _style_citation_anchors(sanitized_html: str) -> str:
    """Style citation anchors (superscript-digit link text) as tappable pills.

    The prompt contract (prompts/digest.md) cites sources as a markdown link
    whose entire visible text is a single superscript-digit character (e.g.
    `[¹](https://t.me/c/123/456)`), possibly multi-digit (`[¹⁰]...`). This
    reuses _ANCHOR_RE -- the same well-formed-anchor pattern
    _enforce_anchor_provenance matches on nh3-sanitized output -- to find
    every anchor, and pills only the ones whose captured inner content is
    NOTHING but one or more characters from the superscript-digit set
    (`_SUPERSCRIPT_ONLY_RE.fullmatch`, not `.search`, so trailing/leading
    extra characters or nested tags disqualify it): a citation is never
    anything else, so this can't mistake a normal-text link (e.g. a
    hypothetical future link whose visible text is ordinary prose) for one.

    MUST run AFTER _enforce_anchor_provenance in render_html's pipeline: a
    non-allowlisted anchor is unwrapped down to its bare inner text by that
    pass (the `<a>` tag disappears entirely), so by the time this pass runs,
    a non-allowlisted citation is no longer an anchor at all and _ANCHOR_RE
    simply won't match it -- it can never be mistakenly pill-styled. Running
    this pass first would defeat that: a hostile, non-allowlisted citation
    anchor would get styled as a legitimate-looking pill before provenance
    enforcement ever had a chance to unwrap it.

    The opening `<a ...>` tag is rewritten in place via a nested `re.sub` on
    `match.group(0)` (the whole matched anchor) rather than being
    reconstructed from `href`/inner text -- this preserves nh3's own
    `href`/`rel` attribute output byte-for-byte (including nh3's own
    entity-escaping of the href) instead of risking a subtly different
    re-serialization. Anchors whose inner text is anything else are
    returned unchanged (`match.group(0)` as-is).
    """

    def _replace(match: re.Match[str]) -> str:
        inner = match.group(2)
        if not _SUPERSCRIPT_ONLY_RE.fullmatch(inner):
            return match.group(0)
        return re.sub(
            r"^<a\b",
            '<a class="cite" style="display:inline-block;text-decoration:none;'
            "font-size:0.75em;font-weight:600;line-height:1.4;padding:0 6px;"
            "border-radius:999px;margin:0 1px;vertical-align:2px;"
            'background-color:#eef2ff;color:#4f46e5;"',
            match.group(0),
            count=1,
        )

    return _ANCHOR_RE.sub(_replace, sanitized_html)


# Matches the final `<p><em>...</em></p>` in the document -- the closing
# italic "*From N items; ...*" line the prompt contract always ends the
# briefing with (prompts/digest.md) -- allowing trailing whitespace after
# it. `\Z` (not `$`) anchors this to the true end of the string; combined
# with the non-greedy `[\s\S]*?` being forced to extend all the way to that
# anchor, this can only ever match a `<p><em>...</em></p>` that is the LAST
# element in the document, never one that merely happens to also be
# em-only but has further content after it.
_FOOTER_PARAGRAPH_RE = re.compile(r"<p>(<em>[\s\S]*?</em>)</p>\s*\Z")


def _style_footer_line(sanitized_html: str) -> str:
    """Style the closing "*From N items; ...*" line as a muted footer.

    A no-op (returns the input unchanged) when there is no such trailing
    paragraph -- e.g. a digest that predates this prompt change, or a rerun
    of stored archive markdown -- so absence is fine, never an error.

    Only the `<p>` is touched; the `<em>...</em>` it wraps is passed through
    intact (captured as one group and re-emitted verbatim) rather than being
    unwrapped or restyled itself, so the line still reads as italic exactly
    as python-markdown rendered it. The inline style sets an explicit
    foreground (`color:#8a8f9e`) so this line reads correctly in any client,
    dark-mode or not, including one that strips `<style>` blocks entirely;
    the `class="foot"` attribute is added on top for clients that DO honor
    `<style>`, giving them the `.foot` dark-mode override in _HTML_TEMPLATE
    instead of the light-mode inline values everywhere.
    """

    def _replace(match: re.Match[str]) -> str:
        return (
            '<p class="foot" style="margin-top:1.6em;padding-top:0.8em;'
            'border-top:1px solid #e5e7eb;font-size:0.85em;color:#8a8f9e;">'
            f"{match.group(1)}</p>"
        )

    return _FOOTER_PARAGRAPH_RE.sub(_replace, sanitized_html, count=1)


def render_html(body_md: str, allowed_urls: Collection[str], generated_at_label: str) -> str:
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

    After the security-critical passes above, five purely cosmetic passes
    run over the same nh3-sanitized HTML, in this fixed order:
    _wrap_banner_paragraph (styles the collector-failure banner, if
    present), _wrap_needs_attention_section (styles the "Needs attention"
    section, if present), _highlight_tldr_paragraph (styles the TL;DR
    paragraph, if present), _style_citation_anchors (pills every
    superscript-digit citation anchor), and _style_footer_line (styles the
    closing italic "From N items" line, if present). All five follow the same
    safety pattern as _enforce_anchor_provenance -- they only ever inject
    this module's own constant markup around text nh3 already sanitized,
    never re-parse or re-emit attacker-controlled HTML -- and each is a
    no-op when its target isn't present, so none of them can raise or
    change behavior for a digest that doesn't happen to contain that shape.

    _style_citation_anchors specifically MUST run after
    _enforce_anchor_provenance, not before: a non-allowlisted anchor is
    unwrapped down to bare text by that pass, and it must stay bare text --
    never get dressed up as a legitimate-looking citation pill. See
    _style_citation_anchors's own docstring for why the ordering enforces
    this by construction rather than by convention.

    ``generated_at_label`` is a pre-formatted, already-localized display
    string for the masthead timestamp (e.g. "Thu, Jul 31 · 18:07"). This
    function does no timezone conversion of its own -- per CLAUDE.md,
    storage stays UTC and only render/email time converts, and the actual
    conversion happens in the caller (digest/main.py's _send_and_finalize),
    which is what has access to the run's local clock in the first place.

    Earlier revisions also injected colored Telegram/X source chips (one
    pass for a per-source h3 subgroup heading, one for an inline
    `**[Source/Name]**` bullet tag). The current briefing contract
    (prompts/digest.md) is prose clustered by story/topic, not by source --
    it emits neither shape -- so both passes were dead code and have been
    removed along with their tests.
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
    sanitized = _wrap_banner_paragraph(sanitized)
    sanitized = _wrap_needs_attention_section(sanitized)
    sanitized = _highlight_tldr_paragraph(sanitized)
    sanitized = _style_citation_anchors(sanitized)
    sanitized = _style_footer_line(sanitized)
    return _HTML_TEMPLATE.format(body=sanitized, generated_at=generated_at_label)


def _message_id_domain(digest_from: str) -> str | None:
    """Extract the domain to stamp on the Message-ID from a From address.

    ``parseaddr`` handles both a bare address and the ``Display Name
    <addr@domain>`` form, so this doesn't need its own address-grammar
    parsing. Returns None -- rather than raising or building a malformed
    domain -- when the address has no usable "@"-delimited domain (a
    parseaddr miss, or a trailing-bare "user@"); the caller then falls back
    to make_msgid()'s own hostname-based default, since a missing-but-
    intentional domain there is strictly better than a broken Message-ID,
    and send_digest must not start failing over a header nicety.
    """
    _, addr = parseaddr(digest_from)
    if "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1] or None


def send_digest(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    digest_from: str,
    digest_from_name: str,
    digest_to: str,
    subject: str,
    body_md: str,
    allowed_urls: Collection[str],
    generated_at_label: str,
) -> None:
    """Send the digest as a multipart (plain + HTML) email over SMTP with STARTTLS.

    ``allowed_urls`` is the set of URLs the HTML part's links are allowed to
    point at -- the digest's stamped item URLs -- and is passed straight
    through to render_html's anchor-provenance check (see its docstring).
    The text/plain part is unaffected: it carries body_md verbatim, relying
    on enforce_link_allowlist (digest/summarize.py) having already run at
    the markdown-source level for that MIME part's protection.

    ``generated_at_label`` is likewise passed straight through to
    render_html for the masthead timestamp (see its docstring) -- this
    function has no opinion on its format or timezone, it just carries it
    to the one place that renders it.

    The From header carries a display name via email.utils.formataddr, which
    also gets the quoting/escaping right if ``digest_from_name`` ever
    contains a comma or other RFC 5322 special character, rather than the
    bare address a mail client would otherwise show in place of a friendly
    sender name.

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

    ``Date`` and ``Message-ID`` are set explicitly rather than left for the
    relay to synthesize: `Date` is required by RFC 5322, and a missing
    `Message-ID` is a routine spam-scoring signal. iCloud's submission
    server currently patches both in on the way out, so mail still flows
    without this -- but a message that already carries them from the sender
    reads as materially more legitimate to spam filters, and a relay
    patching up your headers isn't something to depend on. The Message-ID's
    domain is derived from ``digest_from`` (see _message_id_domain) rather
    than left to default to the container's hostname, which is a random
    Docker hex string with no relation to the sending domain -- exactly the
    opposite of the legitimacy signal this is for.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    # digest_from may already be in "Display Name <addr@domain>" form (DIGEST_FROM
    # is a free-text env var with no format enforcement in config.py, and
    # _message_id_domain below already anticipates this shape). Passing it to
    # formataddr as-is would nest it -- "Digest <Existing Name <addr@domain>>" --
    # which email.utils.getaddresses parses as an EMPTY sender, so smtplib would
    # submit with a null envelope sender and a broken From header instead of
    # rejecting outright. parseaddr first extracts just the addr-spec, so
    # formataddr always combines a bare address with the one display name we
    # actually want.
    msg["From"] = formataddr((digest_from_name, parseaddr(digest_from)[1]))
    msg["To"] = digest_to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=_message_id_domain(digest_from))
    msg.attach(MIMEText(body_md, "plain"))
    msg.attach(MIMEText(render_html(body_md, allowed_urls, generated_at_label), "html"))

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
