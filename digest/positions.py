"""The positions tracker: which items belong to it, and how one is summarized.

A sibling of digest/daily.py, digest/weekly.py and digest/patreon.py, and
kept as its own module for the same reason those are: a distinct feature
with its own prompt contract (prompts/positions.md), its own input shape,
and its own delivery topic.

WHAT THIS REPLACES. Before this module, the owner's position channels fed a
reserved 30-slot "positions" lane in digest/summarize.py's
`allocate_by_source`, and a standing "portfolio coverage" rule in
prompts/digest.md forced their news into a story-titled section of the
ordinary 6-hourly briefing. Both are gone. The lane's own comment recorded
why that arrangement could not hold: measured 2026-08-20, the two channels
arrived at ~77 items per 6h run against a 30-slot HARD cap -- a backlog
growing ~188/day whose only relief was `prune_stale_unsummarized` deleting
the unread tail after 14 days. Giving these items their own run removes
every competing lane, so the cap disappears along with the starvation. The
topic separation the owner asked for and the fix for that backlog are the
same change.

THE MEMBERSHIP PREDICATE IS THE LOAD-BEARING PART. `is_positions_item`
decides which items this pipeline claims, and digest/state.py excludes
exactly the same set from the window digest's own sweep. Those two must be
EXACT complements: an item both claim is delivered twice in two different
topics, and an item neither claims is silently lost until the 14-day prune
deletes it. That is why state.py derives its two queries from one shared
SQL fragment (`positions_match_sql`) rather than restating the predicate --
see that function. This module's Python predicate exists for callers
holding an `Item` in memory rather than a cursor, and is unit-tested
against the SQL one for agreement.

SILENCE IS A PRODUCT FEATURE, NOT A FAILURE. These are high-volume
community channels; most windows carry only price talk. `summarize_positions`
returns None when the model reports the window held nothing material, and
the caller delivers nothing at all. That is the whole reason this can run
every 4 hours without becoming noise -- see `run_positions` in
digest/main.py for what happens to a quiet window's items.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection, Iterable
from pathlib import Path

from digest.state import Item
from digest.summarize import (
    SummarizeError,
    run_claude,
    truncate_item_text,
    validate_output,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "positions.md"

# The tracker's editorial job -- decide whether a window held real news,
# then cluster and write it -- is the same KIND of judgment the window
# briefing makes, so it gets the same effort the window digest's own A/B
# settled on. Fixed here rather than threaded from Config.claude_effort for
# the reason digest/patreon.py fixes its own: this run fires 6x/day on its
# own timer, and tuning the window digest's effort should not silently
# change the cost of a separate, independently-scheduled feature.
_POSITIONS_EFFORT = "high"

# Hard ceiling on items reaching one prompt, newest kept. At the measured
# ~77 items per 6h (see the module docstring) a 4-hourly run collects ~51,
# so this is roughly 3x normal headroom -- it binds only after a genuine
# burst or a run the timer missed. Newest-first on overflow for the reason
# the old quota lane used newest-first: a channel that outruns one run's
# capacity has a queue that never drains, and oldest-first there degrades
# into reporting the stalest thing available. Dropped items are logged, not
# silently discarded.
_MAX_ITEMS_PER_PROMPT = 150

# The model's "this window held nothing worth sending" sentinel
# (prompts/positions.md, "First decision"). Matched against the FIRST
# non-empty output line only, and tolerantly -- a stray period, quotes,
# bold markers or a `NO SIGNAL`/`no-signal` casing must all still read as
# the sentinel. Getting this WRONG in the strict direction is the expensive
# failure: an unrecognized sentinel has no `## ` heading, so it fails
# `validate_output`, raises, and the same quiet window is retried forever
# while the stale-backlog warning fires.
_NO_SIGNAL_RE = re.compile(r"^[\s\W]*no[-\s_]?signal[\s\W]*$", re.IGNORECASE)


def positions_tg_prefixes(channels: Iterable[str]) -> tuple[str, ...]:
    """Render POSITIONS_TG_CHANNELS usernames as lowercase t.me URL prefixes.

    The Telegram collector builds a public channel's item url as
    `https://t.me/<username>/<msg_id>` (digest/collectors/telegram.py's
    `_message_link`), so a url prefix is the exact, and only, structural
    handle on "this item came from that channel" -- the item's `chat_id` is
    a Telethon marked id the config never sees, and `chat_title` is a
    display name the channel owner can change at will.

    Lowercased on both sides because Telegram usernames are
    case-insensitive: the owner may write `ASI_Alliance` in config while the
    collector rendered `asi_alliance` into a url, and those are the same
    channel.
    """
    return tuple(
        f"https://t.me/{channel.strip().lower()}/" for channel in channels if channel.strip()
    )


def positions_x_handles(accounts: Iterable[str]) -> frozenset[str]:
    """Normalize POSITIONS_X_ACCOUNTS to a lowercase, @-less screen-name set.

    Matched against an X item's `author`, which digest/collectors/x.py sets
    to the tweet author's bare `screen_name` at both of its Item
    construction sites. Matching on `author` rather than on the url prefix
    is deliberate even though the url is built from that same screen_name:
    `author` is a column state.py can index and compare directly, whereas a
    url prefix match needs a LIKE, and the two would be equivalent only for
    as long as that url format never changes.

    A leading `@` is stripped so both `@Fetch_ai` and `Fetch_ai` work in
    config -- the owner will copy these out of X, where they carry the @.
    """
    return frozenset(
        account.strip().lstrip("@").lower() for account in accounts if account.strip().lstrip("@")
    )


def positions_keyword_terms(keywords: Iterable[str]) -> tuple[str, ...]:
    """Lowercase POSITIONS_KEYWORDS for case-insensitive substring matching.

    The third membership axis, and the only CONTENT-based one: where the two
    normalizers above identify whole channels and accounts, this reaches
    into every other source, so a story about the project is claimed out of
    a general news feed or an unrelated crypto channel rather than staying
    in the window briefing.

    No further normalization -- no stripping of `$`, no punctuation folding.
    A cashtag's `$` is exactly what makes `$FET` precise where a bare `FET`
    would be ruinous, so removing it would defeat the entry's own purpose.
    Config length-floors these for the same reason
    (Config.positions_keywords).
    """
    return tuple(keyword.strip().lower() for keyword in keywords if keyword.strip())


def is_positions_item(
    item: Item,
    tg_prefixes: Collection[str],
    x_handles: Collection[str],
    keywords: Collection[str] = (),
) -> bool:
    """True when this item belongs to the positions tracker rather than the window digest.

    `tg_prefixes`, `x_handles` and `keywords` are the OUTPUT of the three
    normalizers above, not raw config -- passing raw config here silently
    fails to match anything whose casing differs, which is a data-loss bug
    that looks like a quiet channel. Callers in digest/main.py normalize
    once per run.

    `keywords` defaults to `()` so callers predating the keyword axis keep
    working unchanged; digest/main.py always passes it.

    Must agree exactly with digest/state.py's `positions_match_sql`; see
    this module's docstring for why, and tests/test_positions.py for the
    property test that holds the two together.
    """
    url = (item.url or "").lower()
    if any(url.startswith(prefix) for prefix in tg_prefixes):
        return True
    if item.source == "x" and (item.author or "").lower() in x_handles:
        return True
    text = (item.text or "").lower()
    return any(keyword in text for keyword in keywords)


def build_prompt(items: list[Item], recent_coverage: str) -> str:
    """Render prompts/positions.md with the continuity block and this window's items.

    `recent_coverage` is the pre-rendered {{RECENT_COVERAGE}} block from
    digest/summarize.py's `format_recent_coverage`, fed from THIS kind's own
    prior digests (see state.get_recent_positions_digests) -- never the
    window digest's. Mixing them would tell the tracker that a story the
    6-hourly briefing covered is "already reported" to a reader who, since
    the clean cut, no longer sees positions news there at all.

    Substitution order is {{RECENT_COVERAGE}}, then {{ITEM_COUNT}}, then
    {{ITEMS_JSON}} strictly LAST -- the identical ordering hazard
    digest/summarize.py's `build_prompt` documents at length: `str.replace`
    cannot tell template text from text an earlier `.replace()` just
    inserted, so any block built from untrusted scraped text must go in
    after every placeholder it could otherwise forge.
    """
    template = _PROMPT_PATH.read_text(encoding="utf-8")

    payload = [
        {
            "source": item.source,
            "chat_title": item.chat_title,
            "author": item.author,
            "text": truncate_item_text(item.text),
            "url": item.url,
            "fetched_at": item.fetched_at,
        }
        for item in items
    ]
    # ensure_ascii=False and the backtick escape are both carried over from
    # summarize.py's build_prompt for its stated reasons: the payload goes
    # to a UTF-8 subprocess stdin, so \uXXXX escaping is pure inflation, and
    # a literal ``` inside a scraped message would otherwise let the model
    # perceive the json data block as closed early -- presenting the rest of
    # the array as instructions rather than data.
    items_json = json.dumps(payload, indent=2, ensure_ascii=False).replace("`", "\\u0060")

    return (
        template.replace("{{RECENT_COVERAGE}}", recent_coverage)
        .replace("{{ITEM_COUNT}}", str(len(items)))
        .replace("{{ITEMS_JSON}}", items_json)
    )


def select_items(items: list[Item]) -> list[Item]:
    """Cap the prompt at `_MAX_ITEMS_PER_PROMPT`, keeping the NEWEST on overflow.

    Returned in chronological (oldest-first) order regardless, so the model
    reads a window as it happened; only WHICH items survive an overflow is
    newest-first. Input is assumed ordered by `fetched_at` ascending, which
    is what every `get_unsummarized_*` reader in digest/state.py returns.
    """
    if len(items) <= _MAX_ITEMS_PER_PROMPT:
        return items
    logger.warning(
        "positions: %d items exceed the %d-item prompt cap; dropping the %d oldest",
        len(items),
        _MAX_ITEMS_PER_PROMPT,
        len(items) - _MAX_ITEMS_PER_PROMPT,
    )
    return items[-_MAX_ITEMS_PER_PROMPT:]


def is_no_signal(output: str) -> bool:
    """True when the model reported this window held nothing material.

    Checks the FIRST non-empty line rather than the whole output: the prompt
    asks for the bare sentinel and nothing else, but a model that prefaces
    or follows it with a sentence of explanation still MEANT the sentinel,
    and treating that as a malformed briefing instead would retry the same
    quiet window forever (see `_NO_SIGNAL_RE`'s own comment).
    """
    for line in output.splitlines():
        if line.strip():
            return bool(_NO_SIGNAL_RE.match(line))
    return False


def summarize_positions(
    items: list[Item], recent_coverage: str, model: str, timeout_seconds: int
) -> str | None:
    """Summarize one positions window. Returns None when the window held no news.

    None is the NO-SIGNAL outcome (see `is_no_signal`) and is a SUCCESS: the
    caller delivers nothing, and the run exits 0. Distinguishing it from
    failure is the whole point of the return type -- a failure must leave
    the items unclaimed so the next run retries them, whereas a quiet window
    is genuinely finished and its items must be consumed.

    Raises SummarizeError when the model returns something that is neither
    the sentinel nor a briefing with a real `## ` heading -- a refusal, an
    empty answer, a stray apology. Like digest/patreon.py's `summarize_post`
    and unlike digest/translate.py's soft-failing `translate_digest`, this
    does NOT swallow that: the summary is the deliverable itself, so its
    failure must propagate and fail the run.
    """
    output = run_claude(
        build_prompt(items, recent_coverage), model, timeout_seconds, effort=_POSITIONS_EFFORT
    )
    if is_no_signal(output):
        return None
    validate_output(output)
    return output


__all__ = [
    "SummarizeError",
    "build_prompt",
    "is_no_signal",
    "is_positions_item",
    "positions_keyword_terms",
    "positions_tg_prefixes",
    "positions_x_handles",
    "select_items",
    "summarize_positions",
]
