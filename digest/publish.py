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
import unicodedata
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

# Section headings the prompts MANDATE by construction, not headings the
# model chooses editorially -- derive_topics excludes these for the same
# reason it excludes _NEEDS_ATTENTION_HEADING above: a heading that recurs
# because a prompt rule says "include this" every time its trigger
# condition holds, rather than because the same story kept developing, is
# not a story arc. Feeding it to the site's "×N this week" recurrence
# counter would manufacture a permanent false arc that never says anything
# about what actually happened.
#
# One entry per FIXED second-tier/standing section across all three
# briefing prompts. Kept grouped by prompt so an audit against the prompt
# files is a straight read-down, not a hunt:
#
# - "Also this window" -- prompts/digest.md's fixed second-tier section.
#   The highest-frequency one by far: window digests run every 6 hours, so
#   a false arc here reaches "×5 this week" in a bit over a day -- 4 runs a
#   day, so 5 repeats lands early on day two (owner-reported 2026-08-10,
#   when the timer was 3-hourly and the same arc got there within a day).
# - "Also today" -- prompts/daily.md's fixed second-tier section, present
#   in nearly every daily brief by construction.
# - "Also this week" -- prompts/weekly.md's fixed second-tier section, the
#   weekly counterpart of the two above.
# - "Watching next week" -- prompts/weekly.md's fixed forward-looking
#   watchlist section.
# - "What to watch" -- prompts/daily.md's fixed closing forward-looking
#   section (PR "What to watch" pass). Conditional -- the prompt omits it on
#   a day with nothing forward-looking to name -- but exactly like "Hungary"
#   below, its trigger condition is "does today's material contain this
#   kind of fact", not "did the same story keep developing", so a heading
#   that recurs across otherwise-unrelated days must not read as a
#   recurring arc. prompts/weekly.md's own forward-looking section reuses
#   the existing "Watching next week" heading rather than introducing a
#   second one, so it needs no separate entry here.
# - "Hungary" -- the standing rule in prompts/digest.md, prompts/daily.md
#   and prompts/weekly.md: whenever an r/hungary item appears upstream,
#   this section is mandatory, not a developing story. It is a standing
#   PER-WINDOW (or per-day, or per-week) rubric, not an arc -- if the owner
#   ever wants Hungary arcs back, deleting it from this set is the whole
#   change.
# - "Verification notes" -- prompts/verify-daily.md's (PLAN.md §11.4)
#   closing per-story corroboration ledger, appended by the optional
#   VERIFY_DAILY_ENABLED pass. It is a standing rubric on every verified
#   daily brief, not a developing story -- without this entry it would grow
#   a permanent false "Verification notes" arc on every single verified
#   day.
#
# Casefolded (matching the `.casefold()` comparison derive_topics already
# does for _NEEDS_ATTENTION_HEADING) so "## ALSO TODAY" is caught too.
#
# MAINTENANCE COUPLING: a prompt file that adds a new FIXED, mandated
# rubric heading (as opposed to a model-chosen story heading) must add its
# casefolded text here too, or the site will start growing a false arc for
# it. This has now been missed twice -- the first pass covered only the
# daily/weekly headings the owner happened to have seen, leaving the
# window prompt's own "Also this window" (and the weekly's "Also this
# week") to surface later as live false arcs. `test_structural_rubric_
# headings_cover_every_prompt_mandated_heading` in tests/test_publish.py
# now reads the prompt files directly and fails on the next omission.
_STRUCTURAL_RUBRIC_HEADINGS = frozenset(
    {
        "also this window",
        "also today",
        "also this week",
        "watching next week",
        "what to watch",
        "hungary",
        "verification notes",
    }
)

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
# Same shape, but CAPTURING the url, for _plain_text(keep_link_urls=True).
_MARKDOWN_LINK_WITH_URL_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")

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


def _plain_text(text: str, keep_link_urls: bool = False) -> str:
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

    `keep_link_urls` (default False, i.e. every existing caller unchanged)
    renders `[text](url)` as `text (url)` instead of dropping the url. Only
    the positions tracker sets it: its prompt REQUIRES a citation on every
    claim, where the Patreon and TL;DR contracts forbid links outright, so
    for that one caller the url is the payload rather than noise to strip.

    Otherwise intentionally narrow, not a general markdown-to-plaintext
    converter, since the TL;DR sentence the prompt contract produces never
    contains anything else link- or emphasis-shaped beyond what's handled
    here.
    """
    if keep_link_urls:
        # `[text](url)` -> `text (url)`. Telegram auto-links a bare URL in
        # plain text, so the citation stays tappable without a parse_mode --
        # see render_post_text's `keep_link_urls` for who needs this and why.
        text = _MARKDOWN_LINK_WITH_URL_RE.sub(
            lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(1).strip() else m.group(2), text
        )
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


# Characters whose presence in a heading title signals the model wrote
# INLINE MARKUP there (e.g. a backtick-wrapped term, a `[link](url)`, a
# stray `#`) rather than plain prose. The site's TOC-anchor injection
# (cloudflare-terraform/workers/news-site/worker.js -- a separate repo,
# not touched here) assigns `id="s1".."sN"` to rendered h2s by matching
# ONLY the literal shape `<h2>plain text</h2>`; a heading containing any
# of these characters renders with nested tags instead (e.g. `<h2>x
# <code>y</code></h2>`), gets NO id from that regex, and consumes NO
# number -- silently desynchronizing every later anchor. See
# section_link_targets' docstring for the full guard this backs.
_INLINE_MARKUP_CHARS = "`*_[]<>#"


def section_link_targets(body_md: str) -> list[tuple[str, str]]:
    """Return up to 3 (title, anchor) pairs for send_telegram_tldr's section link buttons.

    Headings come from `_real_heading_lines` (imported from digest/
    summarize.py -- the exact fence-aware, indentation-aware `## ` scan
    validate_output and format_recent_coverage already rely on; see that
    function's own docstring for the full CommonMark reasoning). Its output
    already has the leading `## ` stripped, so a heading's title here is
    just that line's remainder, further `.strip()`-ed of surrounding
    whitespace.

    "## Needs attention" is dropped before numbering (matched via
    `.casefold()` against the module's own `_NEEDS_ATTENTION_HEADING`
    constant, the same one `count_sections`/`has_needs_attention` use) and
    consumes NO anchor number. This mirrors the site's own worker.js
    exactly: it splits the "Needs attention" h2 out into its own
    `div.attention` BEFORE walking the rest of the h2s to assign
    `id="s1".."sN"`, so that heading never receives a site anchor either --
    numbering it here would desync every anchor after it.

    FAIL-SAFE GUARD: if ANY remaining heading's title contains a character
    from `_INLINE_MARKUP_CHARS` (backtick, `*`, `_`, `[`, `]`, `<`, `>`,
    `#`), this returns `[]` -- no section links at all for this digest.
    Such a heading renders on the site with nested markup inside the `<h2>`
    (e.g. `<h2>x <code>y</code></h2>`), which the site's TOC regex (see
    _INLINE_MARKUP_CHARS' comment) does not recognize as a plain heading at
    all: it gets no `id` AND consumes no number, which would silently shift
    every later heading's true s-number away from whatever this function
    computed for it. In practice the model's headings are plain prose, so
    this almost never fires; when it does, the Telegram message simply
    falls back to today's shape (just the "Open the digest" button, no
    section rows). Correct links or no links -- never wrong links.

    The surviving headings are numbered sequentially s1, s2, s3, ... in
    document order, matching the site's own sequential assignment, and the
    FIRST THREE are returned as (title, anchor) pairs: the prompt contract
    (prompts/digest.md) orders sections most-important-first, so the first
    three are already the right three to surface as buttons.

    Returns `[]` when there are fewer than 1 remaining heading too (a
    digest with no real, non-"Needs attention" section) -- the same empty
    result the fail-safe guard produces above, since send_telegram_tldr
    treats both cases identically: add no section-link rows.
    """
    headings = [
        heading.strip()
        for heading in _real_heading_lines(body_md)
        if heading.strip().casefold() != _NEEDS_ATTENTION_HEADING
    ]

    if any(char in heading for heading in headings for char in _INLINE_MARKUP_CHARS):
        return []

    return [(title, f"s{i}") for i, title in enumerate(headings[:3], start=1)]


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


# Mirrors the site's own MAX_TOPICS (toom-edge PR #107's `PUT /ingest/:id`
# validator, which 400s a `topics` array past this length) -- capped here
# too so a digest with many sections degrades to "the first 12, in
# document order" instead of failing site publish outright.
_MAX_TOPICS = 12

# The site's label field, trimmed 1-80 chars.
_MAX_TOPIC_LABEL_LEN = 80


def _slugify(heading: str) -> str:
    """Deterministically fold one heading into an ASCII slug, or "" if nothing survives.

    THE SAME HEADING MUST ALWAYS PRODUCE THE SAME SLUG, run over run: the
    site keys its "story arc" recurrence tracking (derive_topics' own
    docstring has the full contract) on slug equality across digests
    published days apart, so this has to be a pure, stable function of the
    heading text alone -- no randomness, no locale dependence, nothing
    that could drift between two runs of the same process or between two
    different deploys of this code.

    Unicode NFKD-normalizes the heading (splitting each precomposed
    accented character into its base letter plus combining marks, e.g.
    "ő" -> "o" + a combining double acute accent) and then encodes to
    ASCII with `errors="ignore"`, which drops every remaining non-ASCII
    codepoint outright -- the combining marks NFKD just split off, plus
    anything with no ASCII decomposition at all (CJK, emoji, em dashes).
    This is why "Középső árfolyam" folds to "kozepso arfolyam": each
    accented letter loses only its diacritic, not the whole letter.

    The result is lowercased, then every RUN of one or more characters
    outside `[a-z0-9]` (whitespace, punctuation, whatever NFKD-ascii-drop
    left behind) collapses to a single "-" -- so "Fed — Watch & Rates!"
    (where the em dash vanishes in the ASCII-drop step, leaving a doubled
    space) folds to "fed-watch-rates", one hyphen per gap, never a run of
    them. Leading/trailing "-" are stripped, the result is truncated to 64
    characters, and trailing "-" is stripped once more (a truncation can
    land right after a hyphen).

    Returns "" when nothing survives the fold (a heading that is entirely
    punctuation, whitespace, or non-ASCII with no NFKD decomposition, e.g.
    plain CJK) -- derive_topics' contract is to skip such a heading
    entirely rather than send the site a topic with an empty slug.
    """
    ascii_text = unicodedata.normalize("NFKD", heading).encode("ascii", "ignore").decode("ascii")
    collapsed = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return collapsed[:64].rstrip("-")


def derive_topics(
    body_md: str, arc_keys: list[dict[str, str]] | None = None
) -> list[dict[str, str]]:
    """Derive the site's `topics` ingest field from a digest's own `## ` section headings.

    One `{"slug": ..., "label": ...}` entry per real heading (via
    `_real_heading_lines`, the same fence-aware scan `section_link_targets`
    and `count_sections` already rely on), in document order, EXCLUDING
    "## Needs attention" via the identical case-insensitive check
    `section_link_targets` uses against this module's own
    `_NEEDS_ATTENTION_HEADING` constant -- it is the prompt's own routing
    label, not a story, so it must never become a topic either. Also
    EXCLUDING every heading in `_STRUCTURAL_RUBRIC_HEADINGS` ("Also this
    window", "Also today", "Also this week", "Watching next week",
    "What to watch", "Hungary") for the identical reason: see that
    constant's own comment for which prompt mandates each and why their
    recurrence is structural, not editorial.

    `label` is the heading's own text, stripped, truncated to 80 characters
    (the site's own label limit). `slug` is `_slugify(heading)` -- see that
    function's docstring for the full fold; THE SLUG-STABILITY CONTRACT IS
    THE WHOLE POINT of this function existing at all: the site counts a
    story arc as "recurring" by matching `slug` across digests published up
    to 7 days apart (toom-edge PR #107), so the same section title must
    fold to the same slug on every run, forever -- there is no migration
    path for a slug that quietly changes shape later.

    A heading whose slug folds to "" (all punctuation, all CJK, ...) is
    SKIPPED outright, never sent with an empty slug -- the site's ingest
    validator 400s an empty slug, and `_slugify`'s docstring covers exactly
    when this happens.

    Deduplicated by slug, FIRST occurrence wins: two headings that happen
    to fold to the same slug (e.g. differing only in punctuation the fold
    strips) would otherwise produce two entries with the same slug, which
    the site's ingest validator also 400s on (duplicate slugs rejected).
    Capped at `_MAX_TOPICS` (12, matching the site's own `MAX_TOPICS`) in
    document order, after dedup -- the prompt contract orders sections
    most-important-first, so truncating here keeps the most relevant ones.

    `arc_keys` (optional, default None; stable-arc-keys feature) is digest/
    summarize.py's `extract_arc_keys` output for this SAME response's raw
    model output -- a list of `{"heading": ..., "key": ...}` entries, one per
    `## ` story section the model tagged with a stable arc key
    (prompts/digest.md's "Story-arc keys" section). When given, each entry's
    "heading" is folded through the IDENTICAL `_slugify` fold this function
    already applies to every real `## ` heading -- reusing
    `map_deltas_to_slugs`' own exact matching rule (fold to a slug, compare
    against THIS function's own produced slug set) rather than a second,
    potentially divergent heading-equality check. When an entry's folded
    slug matches one of this digest's own topic slugs, that topic dict
    additionally carries `"key": <that entry's key>`. A topic with no
    matching `arc_keys` entry is emitted exactly as before, with no "key" at
    all -- this parameter is PURELY ADDITIVE. Passing `None` (the default,
    and every call site before this feature existed) reproduces today's
    `{"slug", "label"}`-only output byte-for-byte -- the slug-stability
    contract above requires the SLUG itself never change shape, and this
    parameter never touches slug computation, only whether a topic dict
    gains one extra key.

    An `arc_keys` entry whose folded heading doesn't match any of this
    digest's own topic slugs -- a heading the model hallucinated, one this
    function already excluded as structural/Needs-attention, or one that
    didn't survive the `_MAX_TOPICS` cap or the slug dedup above -- is
    silently ignored, with no separate exclusion list to maintain, the
    identical defensive posture `map_deltas_to_slugs` already follows for
    the same reason.

    Never raises: like `count_sections`/`has_needs_attention`, this reads
    only off `_real_heading_lines`' already-defensive scan, so a malformed
    or empty `body_md` simply yields `[]`, not an exception -- there is
    nothing here that can throw the way `extract_tldr`'s markdown-shape
    parsing can. Folding `arc_keys` entries through `_slugify` cannot throw
    either, on the shapes `extract_arc_keys` guarantees (a list of dicts with
    string "heading"/"key" fields).

    Returns `[]` for a `body_md` with no real, non-"Needs attention"
    heading at all (matching `parse_failed_sources`' own "no banner, empty
    list" contract for the identical reason: this is a normal, common
    case, not a parse failure).
    """
    # First occurrence wins on a heading-slug collision, mirroring the
    # topics-list dedup below -- if two arc_keys entries somehow fold to the
    # same slug (a duplicate/near-duplicate heading in the model's own
    # ```arcs output), the first one's key is kept rather than silently
    # overwritten by the second.
    key_by_slug: dict[str, str] = {}
    if arc_keys:
        for entry in arc_keys:
            heading_slug = _slugify(entry["heading"])
            if heading_slug and heading_slug not in key_by_slug:
                key_by_slug[heading_slug] = entry["key"]

    topics: list[dict[str, str]] = []
    seen_slugs: set[str] = set()
    for heading in _real_heading_lines(body_md):
        title = heading.strip()
        casefolded = title.casefold()
        if casefolded == _NEEDS_ATTENTION_HEADING or casefolded in _STRUCTURAL_RUBRIC_HEADINGS:
            continue
        slug = _slugify(title)
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        topic: dict[str, str] = {"slug": slug, "label": title[:_MAX_TOPIC_LABEL_LEN]}
        key = key_by_slug.get(slug)
        if key:
            topic["key"] = key
        topics.append(topic)
        if len(topics) >= _MAX_TOPICS:
            break
    return topics


def map_deltas_to_slugs(body_md: str, deltas: list[dict[str, str]]) -> list[dict[str, str]]:
    """Map each parsed delta's heading to THIS digest's own topic slug, dropping any mismatch.

    PLAN.md §11.3: `deltas` is digest/summarize.py's `extract_deltas` output
    -- a list of `{"heading": ..., "previously": ..., "now": ...}` entries,
    already stripped out of the model's ```deltas fence, but still keyed by
    the RAW heading TEXT the model wrote, not a slug. This function is the
    "heading -> slug" half of §11.3's storage step: each entry's `heading` is
    folded through `_slugify` -- the IDENTICAL fold `derive_topics` (above)
    applies to every real `## ` section heading in this same `body_md` -- and
    kept only if that fold lands on a slug `derive_topics(body_md)` itself
    produced for this digest.

    This is a real filter, not a formality: `derive_topics` already excludes
    "## Needs attention" and every `_STRUCTURAL_RUBRIC_HEADINGS` entry ("Also
    this window", "Hungary", ...), caps at `_MAX_TOPICS`, and dedupes by
    slug -- so a delta whose heading doesn't survive into `derive_topics`'
    own output (because the model hallucinated a heading that isn't a real
    section, cited a structural/routing heading, or the digest simply has
    more than `_MAX_TOPICS` sections and this one didn't make the cut) is
    dropped here too, automatically, with no separate exclusion list to keep
    in sync. Defensive by design: the model wrote the RECENT_COVERAGE
    continuity list and the current `## ` headings independently in the same
    response, and prompt-level instructions are never a hard guarantee (this
    module's own PLAN.md §5 lesson) -- a citation to a heading that doesn't
    actually exist in this digest must be silently dropped, not stored as if
    it were a real topic.

    A dropped delta is never an error: only a COUNT is logged (one warning,
    naming how many were dropped) -- never the heading text itself, which
    (like every other model-generated heading string in this codebase, see
    e.g. digest/summarize.py's format_recent_coverage SECURITY note) derives
    from the same untrusted, scraped Telegram/X/news material as everything
    else in this pipeline and must not reach a log line verbatim.

    Returns a list of `{"slug": ..., "previously": ..., "now": ...}` entries
    -- the `heading` key is dropped once it has served its one purpose
    (matching), since digest/state.py's `deltas` table and the site's ingest
    `deltas` payload field are both keyed on `slug`, never on heading text.
    Order is preserved from the input `deltas` list, which is already
    document order and already capped at 12 by `extract_deltas` -- this
    function applies no cap of its own (a strict subset of an
    already-capped, already-ordered list needs none).

    Never raises: `deltas` entries are always well-formed dicts of strings by
    the time they reach here (extract_deltas's own contract), so the only
    thing this function does is a pure string fold and a set-membership
    check, neither of which can throw on the input shapes it's given.
    """
    valid_slugs = {topic["slug"] for topic in derive_topics(body_md)}
    mapped: list[dict[str, str]] = []
    dropped = 0
    for entry in deltas:
        slug = _slugify(entry["heading"])
        if slug not in valid_slugs:
            dropped += 1
            continue
        mapped.append({"slug": slug, "previously": entry["previously"], "now": entry["now"]})

    if dropped:
        logger.warning(
            "map_deltas_to_slugs: dropped %d delta(s) whose heading matched no "
            "derive_topics slug for this digest",
            dropped,
        )

    return mapped


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
    topics: list[dict[str, str]] | None = None,
    deltas: list[dict[str, str]] | None = None,
    arc_contexts: list[dict[str, str]] | None = None,
    provenance: dict | None = None,
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

    `topics` (keyword-only, default None) is the up-to-12
    `{"slug": ..., "label": ...}` list this module's own `derive_topics`
    computes from `body_md` (see that function's docstring for the full
    slug-stability contract) -- the site uses `slug` to detect a story arc
    recurring across a trailing 7-day window of digests, rendering a "×N
    this week" line for it. Included in the payload under the IDENTICAL
    truthy-only rule as `source_counts`/`failed_sources` just above: an
    empty list or None both mean "omit the field entirely", matching the
    Worker's own `topics` contract (toom-edge PR #107), which treats an
    empty array the same as a missing field.

    `deltas` (keyword-only, default None; PLAN.md §11.3) is the up-to-12
    `{"slug": ..., "previously": ..., "now": ...}` list this module's own
    `map_deltas_to_slugs` produces from `body_md` plus
    digest/summarize.py's `extract_deltas` output -- the site's future
    "what changed" rendering and §11.1's arc timelines both key off `slug`,
    the same story-arc identity `topics` already establishes. Included under
    the IDENTICAL truthy-only rule as `source_counts`/`failed_sources`/
    `topics` above.

    Site-validator finding (verified 2026-08-10 by reading
    cloudflare-terraform/workers/news-site/worker.js's
    `validateDigestPayload` directly, read-only, in the sibling repo): that
    function destructures the JSON body into its OWN fixed list of known
    field names and validates only those -- there is no
    `Object.keys(payload)`/`...rest` check anywhere in it (unlike, say,
    `validateTopics`' PER-ENTRY `...rest` check one level down) that would
    reject an unrecognized TOP-LEVEL field. A `deltas` key the site doesn't
    yet know about is therefore silently ignored today (not stored, not
    validated, not a 400) -- NOT rejected. This means, unlike the ordering
    PLAN.md §11.3's own status note assumed by analogy with the "weekly"
    kind rollout, sending this field before the site half lands is
    harmless: the row simply ingests without a `deltas` column until a
    later site PR adds one. No config flag gates this field's inclusion as
    a result -- `topics`/`source_counts`/`failed_sources` set the precedent
    for an unconditionally-sent, server-ignored-until-supported optional
    field, and `deltas` follows the same shape.

    `arc_contexts` (keyword-only, default None; PLAN.md §11.6 "context
    mode") is digest/state.py's `get_unsynced_arc_contexts` output -- the
    primers the site has not confirmed receiving yet, `{"key", "context_md"}`
    dicts, NOT scoped to this `digest_id` at all (unlike `topics`/`deltas`,
    which describe THIS digest's own sections). Included under the IDENTICAL
    truthy-only rule as `source_counts`/`failed_sources`/`topics`/`deltas`
    above -- so the STEADY STATE (nothing generated since the last successful
    publish) omits the field entirely. See digest/deliver.py's
    `_deliver_site` for the other half of the delta: which keys it stamps
    as synced afterwards, and why the degraded 400-retry path stamps none.

    Site-validator finding, RE-VERIFIED 2026-08-10 for this field
    specifically (same method as the `deltas` finding immediately above --
    reading cloudflare-terraform/workers/news-site/worker.js's
    `validateDigestPayload` directly, read-only, in the sibling repo):
    `arc_contexts` is destructured from the same fixed, `...rest`-free
    field list `deltas` already rides on -- an unrecognized TOP-LEVEL field
    is silently ignored, not rejected, so sending this before the site's own
    §11.6 ingest support lands is harmless for the identical reason
    `deltas` shipping early was harmless (PR #63). This is UNLIKE
    `topics`' per-entry `"key"` field (`arc_keys_site_enabled`,
    Config's own field): that one is gated behind a flag because
    `validateTopics` DOES reject an unrecognized PER-ENTRY field via its own
    `...rest` check -- `arc_contexts` is a top-level field like `deltas`,
    not a per-entry addition to `topics`, so it inherits `deltas`'
    unflagged-shipping precedent, not `topics["key"]`'s gated one. No config
    flag gates this field's inclusion as a result.

    `provenance` (keyword-only, default None) is digest/state.py's
    `get_digest_provenance` output -- the `{"summarize": {"model", "effort",
    "fallback"}, "translate": {...}}` object recording which model/effort
    actually produced this digest's summarize and (optional) translate legs
    (digest/main.py's `_provenance` helper builds it from the
    `digest.summarize.ModelRun`(s) those calls return; see `_SCHEMA`'s
    `digests.provenance` comment in digest/state.py for the exact shape and
    why it is one JSON object rather than several fields). Included under
    the IDENTICAL truthy-only rule as `source_counts`/`failed_sources`/
    `topics`/`deltas`/`arc_contexts` above: an empty dict or None both mean
    "omit the field entirely". A field the site does not yet know about is
    silently ignored, not rejected, per the same `validateDigestPayload`
    finding `deltas`'/`arc_contexts`' own docstring paragraphs cite -- so
    sending this before the site's own ingest/rendering support for it
    lands is harmless for the identical reason those two fields shipping
    early was. No config flag gates this field's inclusion either.

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
    if topics:
        payload["topics"] = topics
    if deltas:
        payload["deltas"] = deltas
    if arc_contexts:
        payload["arc_contexts"] = arc_contexts
    if provenance:
        payload["provenance"] = provenance
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

    After the "Open the digest" button row, one additional row is appended
    per `section_link_targets(body_md)` result (up to 3) -- each a single
    button linking straight to that section's anchor on the site
    (`f"{public_base}/d/{digest_id}#{anchor}"`), so a reader can jump
    directly to the story that interests them instead of always landing at
    the top of the page. When `section_link_targets` returns `[]` (the
    digest's headings don't number cleanly against the site's own anchor
    assignment -- see that function's fail-safe guard), the keyboard is
    identical to today: just the one "Open the digest" row. The message
    TEXT itself is unchanged either way; only the keyboard grows.

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
    keyboard_rows: list[list[dict[str, str]]] = [[{"text": "Open the digest →", "url": link}]]
    for title, anchor in section_link_targets(body_md):
        button_text = f"→ {title}"
        # Telegram's Bot API trims long inline-keyboard button text
        # unpredictably (no fixed, documented cutoff observed in practice) --
        # capping to 30 chars here, with a single trailing "…", keeps the
        # rendered button under every truncation behavior seen rather than
        # leaving it to the client's own opaque trimming.
        if len(button_text) > 30:
            button_text = button_text[:29] + "…"
        keyboard_rows.append(
            [{"text": button_text, "url": f"{public_base}/d/{digest_id}#{anchor}"}]
        )

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": keyboard_rows},
    }
    if thread_id:
        payload["message_thread_id"] = thread_id

    _send_message(payload, bot_token, timeout_seconds)


def _send_message(payload: dict[str, Any], bot_token: str, timeout_seconds: int) -> int | None:
    """POST one already-built sendMessage payload. Raises TelegramSendError on failure.

    Returns the sent message's own id when Telegram reports one, so a caller
    splitting a long post across several messages can chain each part as a
    reply to the previous one. `None` on any unexpected response shape --
    the send still succeeded, only the threading of a follow-up is lost.

    Extracted so `send_telegram_tldr` and `send_telegram_post` cannot drift
    apart on this specific path. The secrets posture below is the reason
    that matters more than ordinary de-duplication would: the request URL
    embeds the bot token (`.../bot{token}/sendMessage`) and a failure
    response body can echo request content back, so NEITHER may ever reach
    a log line or an exception message (CLAUDE.md's secrets-never-logged
    hard rule). Every failure is caught here and re-raised carrying only a
    status code (`HTTPError.code`) or the underlying exception's type name
    -- never `str(exc)`, `exc.url`, or a response body read. Two copies of
    that reasoning is one copy too many.
    """
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
            body = response.read()
    except urllib.error.HTTPError as exc:
        logger.warning("telegram sendMessage failed with status %d", exc.code)
        raise TelegramSendError(
            f"telegram sendMessage failed with status {exc.code}", status=exc.code
        ) from None
    except Exception as exc:
        logger.warning("telegram sendMessage failed: %s", type(exc).__name__)
        raise TelegramSendError(f"telegram sendMessage failed: {type(exc).__name__}") from None

    # The sent message's own id, so a caller can chain a reply to it. Parsed
    # defensively and never raised over: a successful send whose response
    # body is an unexpected shape is still a successful send, and the only
    # thing lost is the threading of a follow-up part.
    try:
        parsed = json.loads(body)
        message_id = parsed["result"]["message_id"]
        return int(message_id) if isinstance(message_id, int | str) else None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


# Telegram's own hard cap is 4096 characters for a message. prompts/patreon.md
# asks for ~150 words, so this is a backstop against a model that ignores the
# budget, not an expected path -- truncating is strictly better than a 400
# from the API, which would look like a delivery failure rather than a long post.
_TELEGRAM_TEXT_LIMIT = 3800

# Inline-keyboard button text is trimmed unpredictably by Telegram past
# roughly this length (no fixed documented cutoff observed in practice), so
# `send_telegram_tldr` already caps its section buttons at 30. Same number,
# same reason.
_BUTTON_TEXT_LIMIT = 30


class TelegramPartialSend(TelegramSendError):
    """Some parts of a split post were delivered and a later one failed.

    Distinct from a plain `TelegramSendError` because the CALLER'S CORRECT
    ACTION IS THE OPPOSITE. An ordinary failure means nothing arrived, so
    leaving `telegram_sent = 0` lets a later run retry cleanly. Here the
    reader already has part 1 in the topic, and a retry would re-send it --
    so the digest must be marked sent, and the run must still report
    failure so the OnFailure alert fires and a human looks.

    `parts_sent` / `parts_total` are carried for the log line, so the
    operational record says how much of the post actually landed.

    `status` forwards the UNDERLYING failure's HTTP status, for the same
    reason the base class carries it at all (see its docstring): the per-run
    429 circuit breaker keys on `exc.status == 429`, and a partial send
    caused by a 429 on part 2 is exactly as much a "stop sending this run"
    signal as a 429 on a whole message. Dropping it here would let every
    LATER post in the same run keep firing into an already-rate-limited API
    -- quietly bypassing the flood-incident guard for multi-part posts only.
    """

    def __init__(self, parts_sent: int, parts_total: int, status: int | None = None) -> None:
        super().__init__(
            f"telegram post partially sent ({parts_sent}/{parts_total} parts)", status=status
        )
        self.parts_sent = parts_sent
        self.parts_total = parts_total


def split_for_telegram(text: str, limit: int = _TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split `text` into chunks that each fit one Telegram message.

    Splits on PARAGRAPH boundaries first, falling back to line boundaries,
    and only then to a hard character cut. The ordering matters for
    readability: this text is a bulleted summary, and a cut mid-bullet
    reads as data loss even though the next part continues it.

    Returns `[text]` unchanged when it already fits -- the common case at
    the prompt's 350-600 word budget, so the single-send path (and its
    flood protections) stays the normal one rather than the exception.

    A single paragraph longer than `limit` is hard-cut rather than dropped:
    losing the tail of one oversized bullet is strictly better than losing
    the message.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(block) <= limit:
            current = block
            continue
        # One oversized block: break it on lines, then hard-cut whatever is
        # still too long on its own.
        for line in block.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
    if current:
        chunks.append(current)
    return chunks


def render_post_text(body_md: str, keep_link_urls: bool = False) -> str:
    """Flatten one post's briefing markdown into the plain text Telegram receives.

    Sent as PLAIN TEXT for the identical reason `send_telegram_tldr`
    documents at length: Telegram's Markdown/MarkdownV2 parse modes reject
    the WHOLE message over a single unescaped special character, and this
    text is built from model output that was never asked to produce
    Telegram-flavored markdown. A Hungarian post about a "**support**"
    level at "$65,000" is exactly the shape that trips it. Plain text
    cannot fail to parse.

    So the markdown is reduced rather than escaped: the `## ` heading loses
    its hashes (it becomes the message's first line, which reads as a title
    without any markup), `**TL;DR:**` loses its asterisks but KEEPS the
    marker text, `- ` bullets become `• `, and any inline markdown link
    collapses to its visible text via `_plain_text` -- the prompt forbids
    links, but a model that emits one anyway must not leak a bare URL into
    a message whose whole point is that the links are buttons.

    `keep_link_urls` (default False) flips that last rule for the positions
    tracker, whose message has no buttons at all and whose prompt requires
    a citation per claim: there the url is the point. See `_plain_text`.
    """
    lines: list[str] = []
    for raw in body_md.strip().splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        elif line.startswith(("- ", "* ")):
            line = "• " + line[2:].strip()
        if _TLDR_PREFIX in line:
            # The marker regex consumes the whitespace that followed it, so
            # the replacement has to put a space back -- then collapse, in
            # case a future regex change stops eating it.
            line = _TLDR_MARKER_RE.sub("TL;DR: ", line, count=1)
            line = re.sub(r"TL;DR:\s+", "TL;DR: ", line, count=1)
        lines.append(_plain_text(line, keep_link_urls))

    text = "\n".join(lines).strip()
    # Collapse the runs of blank lines the reduction above can leave behind.
    # Deliberately NOT truncated here: `split_for_telegram` turns an
    # over-length summary into a reply chain, so cutting it at this layer
    # would silently discard exactly the material splitting exists to keep.
    return re.sub(r"\n{3,}", "\n\n", text)


def _button(text: str, url: str) -> dict[str, str]:
    """One inline-keyboard button, with its label capped at Telegram's trim point."""
    if len(text) > _BUTTON_TEXT_LIMIT:
        text = text[: _BUTTON_TEXT_LIMIT - 1] + "…"
    return {"text": text, "url": url}


def _send_as_reply_chain(
    parts: list[str],
    keyboard: list[list[dict[str, str]]] | None,
    bot_token: str,
    chat_id: str,
    thread_id: int,
    timeout_seconds: int,
) -> None:
    """Send `parts` as one reply chain, numbering them when there is more than one.

    Shared by `send_telegram_post` and `send_telegram_tracker` -- both send a
    WHOLE briefing into a topic (unlike `send_telegram_tldr`, which sends one
    short paragraph plus a site link and so never chains). Extracted rather
    than duplicated because the partial-send contract below is subtle enough
    that two copies would drift.

    `keyboard`, when given, rides on the LAST part only: buttons are the call
    to action after reading, and repeating them mid-thread invites tapping
    away before the summary is finished. `None` means no buttons at all --
    the positions tracker's shape, where every citation is already an inline
    url in the text and there is no off-message destination to offer.

    Failure contract, load-bearing for the caller's retry decision: if the
    FIRST part fails, `TelegramSendError` propagates -- nothing landed, so a
    later run can retry the whole thing cleanly with no risk of duplication.
    If a LATER part fails, `TelegramPartialSend` is raised instead, because
    part 1 is already sitting in the topic and a retry would re-send it.
    """
    reply_to: int | None = None
    for index, part in enumerate(parts):
        text = part if len(parts) == 1 else f"{part}\n\n({index + 1}/{len(parts)})"

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            # A preview card would duplicate a button directly beneath it
            # (Patreon), or blow one arbitrary citation up into a card at the
            # end of the message (the tracker). Neither is wanted.
            "disable_web_page_preview": True,
        }
        if keyboard and index == len(parts) - 1:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        if thread_id:
            # Explicit 0 is an invalid thread id to Telegram, not "no
            # thread" -- see send_telegram_tldr's own note. Omit the key.
            payload["message_thread_id"] = thread_id
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to

        try:
            sent_id = _send_message(payload, bot_token, timeout_seconds)
        except TelegramSendError as exc:
            if index == 0:
                raise
            raise TelegramPartialSend(index, len(parts), status=exc.status) from None
        reply_to = sent_id


def send_telegram_post(
    body_md: str,
    created_at: str,
    post_url: str,
    embed_url: str | None,
    bot_token: str,
    chat_id: str,
    thread_id: int,
    timeout_seconds: int = 30,
) -> None:
    """POST one Patreon post's summary to Telegram. Raises TelegramSendError on failure.

    A sibling of `send_telegram_tldr`, not a parameterization of it: that
    function's whole keyboard is built from SITE anchors
    (`{public_base}/d/{digest_id}#{anchor}`), because a window digest is
    many stories whose home is the news site. A Patreon post's home is the
    post itself, so both buttons here point OFF-site, and `public_base` has
    no role at all.

    Two rows, the second conditional:

        [ Megnyitás a Patreonon → ]
        [ ▶ <video title> ]            only when the post embeds a video

    `embed_url` comes from `items.embed_url`, which the collector reads out
    of Patreon's own `attributes.embed` rather than regexing body text --
    so a missing button means the post genuinely has no video, not that a
    pattern failed to match.

    Unlike `send_telegram_tldr` this takes no `digest_id`: there is nothing
    to link to by id, and the freshness guard that function's caller
    applies lives in `deliver.py`, not here.
    """
    header = _local_header_label(created_at)
    parts = split_for_telegram(f"{header}\n\n{render_post_text(body_md)}")

    keyboard: list[list[dict[str, str]]] = [[_button("Megnyitás a Patreonon →", post_url)]]
    if embed_url:
        keyboard.append([_button("▶ Videó megtekintése", embed_url)])

    _send_as_reply_chain(parts, keyboard, bot_token, chat_id, thread_id, timeout_seconds)


def send_relay_ping(
    channel: str,
    count: int,
    bot_token: str,
    chat_id: str,
    thread_id: int,
    timeout_seconds: int = 30,
) -> None:
    """POST one short "N new posts" ping for a relay batch. Raises TelegramSendError on failure.

    WHY THIS EXISTS AT ALL, when the posts it announces are already sitting
    in the very same topic. The relay forwards through the owner's own USER
    session (`TG_SESSION`) -- it has to, because a bot cannot read a public
    channel it is not a member of (measured against the live Bot API on
    2026-09-04: `forwardMessages` with `from_chat_id=@<channel>` answers
    `400 Bad Request: message to forward not found`, since the bot cannot
    see the source message even though the channel itself resolves). But
    Telegram never notifies an account about its OWN outgoing messages, on
    any device -- so the one person guaranteed NOT to be told about a
    forwarded post was the owner it was forwarded for. Every other member of
    the hub got a notification; the owner got silence.

    A second message from a DIFFERENT sender is what fixes that, and the
    notify bot is already configured, already an admin of this group, and
    already posts every other digest kind. So the relay forwards as the
    owner (keeping the media, the album grouping and the "Forwarded from"
    header a bot re-post would destroy) and the bot immediately says, in the
    same topic, that it happened.

    Deliberately ONE ping per channel per run, not one per message: a batch
    of five forwards is one event to the reader, and five pings under five
    posts would be worse than the silence this replaces.

    No link and no button, unlike every other sender in this module. The
    thing being announced is directly above this message in the same topic --
    tapping the notification lands the reader on it. `send_telegram_tldr`
    needs a button because a window digest's home is the site; a relay post's
    home is the very topic this ping is posted into.

    Plain text, no parse_mode, for the identical reason its three siblings
    document at length: a parse mode rejects the whole message over one
    unescaped character, and a public channel username is not something this
    codebase gets to assume is markdown-safe.
    """
    label = "post" if count == 1 else "posts"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": f"\U0001f514 {channel} \u00b7 {count} new {label}",
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = thread_id

    _send_message(payload, bot_token, timeout_seconds)


def send_telegram_tracker(
    body_md: str,
    created_at: str,
    bot_token: str,
    chat_id: str,
    thread_id: int,
    timeout_seconds: int = 30,
) -> None:
    """POST one positions-tracker update to Telegram. Raises TelegramSendError on failure.

    A third sibling of `send_telegram_tldr`/`send_telegram_post`, and for the
    reason that pattern already establishes: WHERE THE CONTENT LIVES decides
    the message shape.

    - A window digest's home is the news site, so `send_telegram_tldr` sends
      one paragraph and links there.
    - A Patreon post's home is the post, so `send_telegram_post` sends the
      summary with an off-site button.
    - A positions update has NO home but this message. The tracker is never
      published to the site (that would broadcast the owner's portfolio --
      digest/main.py's `_POSITIONS_HIDDEN_CHANNELS`), so there is nothing to
      link to and nothing to open. Sending a TL;DR-plus-button here would
      hand the reader two sentences and a button to a page that does not
      exist. The WHOLE body goes into the topic instead.

    That is also exactly what the feature is for: the owner reads the
    project's progress in the topic without opening X or Telegram's own
    channels, so the message has to be self-contained.

    Citations survive as inline urls (`render_post_text(keep_link_urls=True)`)
    rather than becoming buttons: the tracker's prompt requires a source on
    every claim, which is far more links than a keyboard can carry, and
    Telegram auto-links a bare url in plain text. Plain text, no parse_mode,
    for the identical reason its two siblings document at length -- a parse
    mode rejects the whole message over one unescaped character in model
    output that was never asked to produce Telegram-flavored markdown.
    """
    header = _local_header_label(created_at)
    parts = split_for_telegram(f"{header}\n\n{render_post_text(body_md, keep_link_urls=True)}")
    _send_as_reply_chain(parts, None, bot_token, chat_id, thread_id, timeout_seconds)
