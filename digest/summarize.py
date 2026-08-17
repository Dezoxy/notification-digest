"""Builds the summarization prompt and invokes the Claude CLI.

See PLAN.md §4.4 and §5. The `claude` CLI authenticates via the owner's
persisted Max-subscription login (CLAUDE_CONFIG_DIR) -- no API key, no SDK
dependency here, just a subprocess call.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
import subprocess
import tempfile
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path

from digest.config import claude_subprocess_env
from digest.state import Item

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "digest.md"

# Bounds each item's text as it's embedded in the prompt payload. Telegram
# messages can reach 4096 chars; at up to _MAX_ITEMS_PER_DIGEST (200) items
# per run (digest/main.py), 200 x 2000 chars keeps the prompt comfortably
# inside the model's context window. Truncation happens only in the prompt
# payload built here -- the stored item text in the database stays
# full-length, untouched.
_MAX_ITEM_TEXT_CHARS = 2000
_TRUNCATION_MARKER = " …[truncated]"

# Bounds the BUILT prompt's UTF-8 BYTE length, checked by
# select_items_for_prompt. Bytes track tokens far better than characters
# across scripts: ASCII text runs roughly 0.25 tokens/byte (~4 bytes per
# token), while CJK/emoji-heavy text runs roughly 0.5-0.75 tokens/byte, so
# 300_000 bytes is <= ~200k tokens even in the worst case and ~75k tokens
# for a typical ASCII-heavy batch -- deliberately conservative on both ends,
# since a shrunk prefix here just means the remainder drains on a later run
# (the 6-hourly timer is the drain loop -- see _MAX_ITEMS_PER_DIGEST in
# digest/main.py). Per-item text is already capped at _MAX_ITEM_TEXT_CHARS,
# but _MAX_ITEMS_PER_DIGEST (digest/main.py) items of emoji/CJK-heavy source
# text can still serialize to far more BYTES than a naive character count
# would suggest -- measuring characters instead of encoded bytes was exactly
# the gap that let, e.g., 200 items of 2000 emoji each (only ~432k Python
# characters, but ~1.6MB of UTF-8) slip past a 600k-CHAR bound while blowing
# the token budget. See select_items_for_prompt's docstring for why this
# must be measured on the built prompt's ENCODED bytes, not its character
# count.
_MAX_PROMPT_BYTES = 300_000


class SummarizeError(Exception):
    """Raised when the Claude CLI fails to produce a usable digest.

    Messages must stay short and must never include the prompt (which
    contains scraped Telegram/X message content).
    """


class SafeguardsRefusalError(SummarizeError):
    """Raised when the CLI's non-zero exit was an API-side safety-classifier refusal.

    Distinct from the generic SummarizeError a non-zero exit otherwise
    raises: live incident (digests 20 and 60, 2026-08-01, verified by manual
    reproduction against `claude` CLI 2.1.220) showed a security-heavy
    digest can trip the target model's real-time safety classifier
    ("Sonnet 5's safeguards flagged this message"), and that refusal is
    DETERMINISTIC for a given (content, model) pair -- retrying the exact
    same model against the exact same prompt can never succeed, so a caller
    catching this specifically (rather than the generic SummarizeError) can
    skip straight to a different model instead of wasting a call on a
    same-model retry. A different model -- in particular one that predates
    that classifier -- has independent odds and may well succeed on the
    identical content; see digest/translate.py's translate_digest for the
    caller that acts on this distinction.

    The message carries no prompt content, same rule as SummarizeError.
    """


def _truncate_item_text(text: str) -> str:
    """Truncate a single item's text to _MAX_ITEM_TEXT_CHARS for the prompt payload.

    Text at or under the limit is returned unchanged; anything longer is cut
    to exactly _MAX_ITEM_TEXT_CHARS characters with a trailing marker
    appended, so the reader can tell the item was clipped. This bounds the
    prompt only -- see _MAX_ITEM_TEXT_CHARS's docstring comment for why, and
    note stored items are never touched by this function.
    """
    if len(text) <= _MAX_ITEM_TEXT_CHARS:
        return text
    return text[:_MAX_ITEM_TEXT_CHARS] + _TRUNCATION_MARKER


def build_prompt(
    items: list[Item], failed_sources: list[str], recent_coverage: str, recent_arcs: str = ""
) -> str:
    """Load prompts/digest.md and substitute the coverage/arcs/status/items-JSON placeholders.

    `recent_coverage` is the pre-rendered {{RECENT_COVERAGE}} block (see
    format_recent_coverage) -- this function does no formatting or
    sanitization of it itself, it only substitutes the string it's given.

    `recent_arcs` (default "") is the pre-rendered {{RECENT_ARCS}} block (see
    format_recent_arcs) -- the stable-arc-keys feature's "story continuity"
    list, the same shape of prompt input as `recent_coverage` and subject to
    the identical substitution-ordering hazard (see below). Defaults to ""
    only so callers that don't care about the feature (most of this module's
    own byte-budget tests) don't have to pass it; digest/main.py's real
    caller always threads through the actual rendered block.
    """
    template = _PROMPT_PATH.read_text()

    payload = [
        {
            "source": item.source,
            "chat_id": item.chat_id,
            "chat_title": item.chat_title,
            "author": item.author,
            "text": _truncate_item_text(item.text),
            "url": item.url,
            "fetched_at": item.fetched_at,
        }
        for item in items
    ]
    # ensure_ascii=False: the default (True) escapes every non-ASCII
    # character to a 6-char \uXXXX sequence (12 chars for a supplementary-
    # plane emoji, via a surrogate pair) -- pure serialization inflation with
    # no content benefit, since the payload is written straight to a UTF-8
    # subprocess stdin (see run_claude's encoding="utf-8"), never through
    # anything that only tolerates ASCII. For an emoji/CJK-heavy Telegram
    # batch this inflation alone can multiply prompt size several-fold,
    # which is exactly the gap select_items_for_prompt's size check below
    # closes -- but only if the size it measures is the true serialized
    # size, not one artificially inflated by this flag. The backtick-escape
    # pass just below is unaffected: it runs on the resulting string either
    # way and a literal backtick is ASCII, so it's untouched by this flag.
    items_json = json.dumps(payload, indent=2, ensure_ascii=False)
    # The prompt's consumer is an LLM, not a strict CommonMark parser: a
    # literal ``` inside an item's text can make the model perceive the
    # ```json data block as closed early, presenting whatever follows (in
    # the same item, or the rest of the JSON array) as text outside the
    # advertised data boundary -- i.e. as instructions rather than data.
    # Escaping every backtick to its JSON unicode escape is safe globally:
    # a backtick can only occur inside a JSON string value (never in JSON
    # structural syntax), and ` round-trips through json.loads to the
    # original backtick character, so the payload is unaffected.
    items_json = items_json.replace("`", "\\u0060")

    if failed_sources:
        collector_status = "Collector status: " + ", ".join(
            f"{source} collection failed this run" for source in failed_sources
        )
    else:
        collector_status = "Collector status: all collectors succeeded this run."

    # Substitution order is {{COLLECTOR_STATUS}}, then {{RECENT_COVERAGE}},
    # then {{ITEMS_JSON}} strictly LAST: str.replace scans its input left to
    # right looking for the placeholder, and that scan does not distinguish
    # template text from text just inserted by an earlier .replace() call.
    # If ITEMS_JSON went first and an item's text happened to contain the
    # literal "{{COLLECTOR_STATUS}}", the second .replace() would find that
    # match INSIDE the just-inserted JSON and silently rewrite collected
    # message content. Doing ITEMS_JSON last means no subsequent .replace()
    # ever rescans data it inserted.
    #
    # {{RECENT_COVERAGE}} carries the identical hazard, for the identical
    # reason, and so goes before {{ITEMS_JSON}} for the same reason ITEMS_JSON
    # itself goes last: `recent_coverage` (built by format_recent_coverage,
    # digest/state.py's get_recent_digests) is not static template text --
    # its lines are `## ` headings the MODEL wrote into a PAST digest, and
    # those past headings were themselves generated from the same scraped,
    # untrusted Telegram/X/news text this run's items come from. A prompt
    # injection that survived into a prior heading is exactly as capable of
    # embedding a literal "{{COLLECTOR_STATUS}}" or "{{ITEMS_JSON}}" as a
    # hostile item's raw text is -- so it must never be substituted into a
    # position some LATER .replace() call would rescan. Substituting it
    # before ITEMS_JSON (mirroring COLLECTOR_STATUS's position) means only
    # the deterministic, code-generated COLLECTOR_STATUS line is inserted
    # earlier still, and nothing here ever rescans text this function itself
    # inserted.
    #
    # This is defense in depth on top of a sanitization pass one layer down:
    # format_recent_coverage already neutralizes any "{{" sequence inside a
    # heading (replacing it with "{ {") before this function ever sees
    # `recent_coverage`, so even a successfully-injected past heading cannot
    # smuggle a live "{{...}}" placeholder into the built prompt at all --
    # the ordering rule above is what protects the *item* text from being
    # rescanned, this sanitization is what stops `recent_coverage` itself
    # from ever containing a working placeholder in the first place.
    #
    # {{RECENT_ARCS}} goes here too, right after {{RECENT_COVERAGE}} and
    # still strictly before {{ITEMS_JSON}}, for the identical ordering
    # reason: `recent_arcs` (built by format_recent_arcs, digest/state.py's
    # get_recent_arc_keys) is also MODEL-GENERATED text replayed from past
    # runs, not static template text, so it must never sit in a position a
    # later .replace() call could rescan. Its own defense-in-depth layer is
    # actually stronger than RECENT_COVERAGE's: format_recent_arcs validates
    # every key against the same strict `_ARC_KEY_RE` the model's ```arcs
    # fence is parsed with (lowercase ASCII letters/digits/hyphens only), a
    # charset that cannot contain "{" or a backtick at all -- so there is no
    # "{{" to break in the first place, unlike a free-form past heading.
    return (
        template.replace("{{COLLECTOR_STATUS}}", collector_status)
        .replace("{{RECENT_COVERAGE}}", recent_coverage)
        .replace("{{RECENT_ARCS}}", recent_arcs)
        .replace("{{ITEMS_JSON}}", items_json)
    )


# Per-lane item quotas for allocate_by_source. Values sum to
# digest/main.py's _MAX_ITEMS_PER_DIGEST (250) -- the two are coupled on
# purpose: the quotas ARE the allocation of that budget, so change them
# together. The "positions" lane is not a collector source: it is carved
# OUT of "telegram" for items from the owner's POSITIONS_TG_CHANNELS
# (config.py), feeding the standing `## Positions` section
# (prompts/digest.md) -- reserved in BOTH directions, so those channels can
# neither be starved by a busy window nor crowd everything else out (the
# live incident behind this feature: 2026-08-13T07:20Z, a Telegram backlog
# burst produced a briefing built from 200 Telegram items and 0 of every
# other source). Weights are the owner's editorial mix: curated journalism
# ("news") deliberately outweighs raw group chatter now that NEWS_FEEDS
# carries general-news desks, not just the AI/robotics feeds.
#
# Adding the "hackernews" lane (digest/collectors/hackernews.py) required
# shrinking every other lane's share to keep the sum at 250, rather than
# raising the total budget itself: news 60->55, telegram 55->50, x 55->50,
# reddit 40->35, freeing exactly 20 slots for the new lane. "positions" and
# "polymarket" are untouched by the rebalance -- "positions" is a reserved
# carve-out with its own fixed rationale above, not an ordinary source lane
# to shrink, and "polymarket"'s swing-detection lane was already
# deliberately small.
_SOURCE_QUOTAS: dict[str, int] = {
    "news": 55,
    "telegram": 50,
    "x": 50,
    "reddit": 35,
    "hackernews": 20,
    "positions": 30,
    "polymarket": 10,
}


def _allocation_lane(item: Item, positions_prefixes: tuple[str, ...]) -> str:
    """The quota lane an item draws from: its source, or the reserved "positions" lane.

    A telegram item whose t.me URL belongs to one of the owner's configured
    positions channels (POSITIONS_TG_CHANNELS -> lowercase
    `https://t.me/<name>/` prefixes) draws from "positions" instead of
    "telegram". URL prefix, not chat_title, is the matching axis: the URL's
    username segment comes from Telegram's own stable public username
    (collectors/telegram.py's build_message_url), while a channel TITLE is
    free-form text the channel owner can change any day.
    """
    if item.source == "telegram" and item.url:
        url = item.url.lower()
        for prefix in positions_prefixes:
            if url.startswith(prefix):
                return "positions"
    return item.source


def allocate_by_source(
    items: list[Item],
    budget: int,
    positions_channels: Collection[str] = (),
) -> list[Item]:
    """Pick up to `budget` items with per-lane quotas, so no source can crowd out the rest.

    `items` is assumed oldest-first (get_unsummarized_items returns it that
    way); the returned list preserves that order -- it is a FILTER of the
    input, never a reorder -- because select_items_for_prompt (called next)
    and create_digest both rely on it.

    Each item draws from its lane's quota (_SOURCE_QUOTAS via
    _allocation_lane; a lane with no quota entry gets 0 and competes only in
    redistribution). Quota a lane can't fill -- fewer items than slots --
    is redistributed one slot at a time, round-robin in _SOURCE_QUOTAS'
    declaration order, to lanes with items left over, until the budget is
    spent or no eligible lane has anything left. So a quiet window still
    fills up with whatever DID arrive, and the quotas only ever bind when a
    window is genuinely oversubscribed. The "positions" lane is the one
    EXCEPTION: it never receives redistributed slots -- its quota is a hard
    cap, not a floor, because the lane is reserved in BOTH directions
    (guaranteed its slots, AND barred from crowding the general mix -- the
    crowding-out this whole function exists to prevent was driven by
    exactly these channels). Its own unfilled quota still redistributes
    outward to the other lanes normally.

    Within a lane, OLDEST first -- deliberately matching the pre-quota
    behavior rather than preferring freshness: unselected items stay
    unsummarized (digest_id NULL) and drain on later runs, and picking
    newest-first would let a persistently oversubscribed lane starve its own
    oldest backlog forever, breaking the drain-loop contract
    (_MAX_ITEMS_PER_DIGEST's docstring in digest/main.py). The trade-off --
    during a burst, a lane summarizes its stalest pending items first and
    defers the freshest to the next run -- is the same one the pre-quota
    LIMIT already made globally.

    When `budget` is SMALLER than the quotas' sum (select_balanced_items_
    for_prompt shrinking to fit the prompt byte cap), each lane's quota is
    scaled down proportionally (floor, min 1 for a lane with a quota) so
    the reduced budget keeps the same editorial mix -- NOT consumed
    first-come in lane-encounter order, which would hand the whole reduced
    budget to whichever collector ran first and reintroduce exactly the
    ordering bias this function exists to remove.

    When everything fits (`len(items) <= budget`), returns `items` as-is:
    quotas exist to arbitrate scarcity, not to thin a window that was never
    oversubscribed. (The allocation log line below still fires either way
    -- lane pressure should be Loki-visible on every run, not only
    oversubscribed ones, or a quiet-window positions burst is invisible.)
    """
    positions_prefixes = tuple(
        f"https://t.me/{channel.lower()}/" for channel in positions_channels
    )

    lanes: dict[str, list[Item]] = {}
    for item in items:
        lanes.setdefault(_allocation_lane(item, positions_prefixes), []).append(item)

    if len(items) <= budget:
        logger.info(
            "allocation %s",
            json.dumps(
                {
                    lane: {"taken": len(lane_items), "available": len(lane_items)}
                    for lane, lane_items in sorted(lanes.items())
                },
                sort_keys=True,
            ),
        )
        return items

    # Scale quotas proportionally when the budget is below their sum (see
    # docstring) -- at the full budget the scale is 1.0 and every quota is
    # its literal configured value.
    total_quota = sum(_SOURCE_QUOTAS.values())
    quota_scale = min(1.0, budget / total_quota)

    def _lane_quota(lane: str) -> int:
        quota = _SOURCE_QUOTAS.get(lane, 0)
        if quota == 0:
            return 0
        return max(1, int(quota * quota_scale))

    # First pass: every lane takes min(scaled quota, available). Lanes stay
    # oldest-first because `items` was.
    taken: dict[str, int] = {}
    spent = 0
    for lane, lane_items in lanes.items():
        take = min(_lane_quota(lane), len(lane_items), budget - spent)
        taken[lane] = take
        spent += take

    # Redistribution: hand leftover budget out one slot per lane per round,
    # in _SOURCE_QUOTAS' declaration order first (stable, owner-chosen
    # priority), then any unknown lanes in first-seen order. "positions" is
    # excluded -- its quota is a hard cap (see docstring), so it never grows
    # past its reservation no matter how much budget is left over.
    lane_order = [
        lane for lane in _SOURCE_QUOTAS if lane in lanes and lane != "positions"
    ] + [lane for lane in lanes if lane not in _SOURCE_QUOTAS]
    while spent < budget:
        progressed = False
        for lane in lane_order:
            if spent >= budget:
                break
            if taken[lane] < len(lanes[lane]):
                taken[lane] += 1
                spent += 1
                progressed = True
        if not progressed:
            break

    # Counts only, never content -- one line per run (the under-budget fast
    # path above logs its own) so a Loki query can see WHICH lane is under
    # pressure (e.g. a positions backlog growing because the hard cap keeps
    # binding run after run) without a log dive.
    logger.info(
        "allocation %s",
        json.dumps(
            {
                lane: {"taken": taken[lane], "available": len(lanes[lane])}
                for lane in sorted(lanes)
            },
            sort_keys=True,
        ),
    )

    chosen: set[int] = set()
    for lane, lane_items in lanes.items():
        for item in lane_items[: taken[lane]]:
            chosen.add(id(item))
    return [item for item in items if id(item) in chosen]


def select_items_for_prompt(
    items: list[Item],
    failed_sources: list[str],
    recent_coverage: str,
    max_prompt_bytes: int,
    recent_arcs: str = "",
) -> list[Item]:
    """Return the longest OLDEST-first prefix of `items` whose built prompt fits.

    `recent_coverage` is threaded straight through to every build_prompt call
    this function makes (the full-list check and every binary-search
    candidate alike): it is embedded in the built prompt exactly like the
    items and the collector-status line are, so its byte length counts
    toward `max_prompt_bytes` automatically -- a large recent_coverage block
    (e.g. a run with an unusually busy last 24 hours) can itself reduce how
    many items fit in this run's prompt, the same way a bigger
    failed_sources banner would.

    `recent_arcs` (default "") is threaded through identically -- the
    stable-arc-keys feature's {{RECENT_ARCS}} block is embedded in every
    built prompt exactly like `recent_coverage` is, so it counts toward
    `max_prompt_bytes` the same way. Defaults to "" purely so callers that
    don't exercise this feature (most of this function's own tests) don't
    have to pass it; digest/main.py's real caller always threads through the
    actual rendered block.

    The bound is checked against the BUILT prompt's UTF-8 BYTE length (via
    `len(build_prompt(candidate, failed_sources, recent_coverage).encode(
    "utf-8"))`), not its
    Python character count and not the sum of the items' text lengths.
    Bytes, not characters, are what correlates with the model's token
    budget: json.dumps(ensure_ascii=False) serializes each item's text as
    raw UTF-8, and a single non-ASCII character (CJK, emoji) can occupy 2-4
    bytes while still counting as exactly one Python `str` character --
    so a character-length check systematically UNDER-counts an
    emoji/CJK-heavy batch's true size. Concretely: 200 items of 2000 emoji
    each is only ~432k Python characters (comfortably under a naive
    600k-CHARACTER bound) but ~1.6MB of UTF-8 -- far beyond the token budget
    despite "passing" a char-based check. Measuring encoded bytes instead of
    characters is what closes that gap.

    Two things also inflate the built prompt well beyond what a naive sum
    of the items' raw text lengths would suggest: json.dumps still adds
    JSON's own structural characters and per-item key names (~40+ bytes of
    "source"/"chat_id"/"author"/"url"/"fetched_at" scaffolding per item on
    top of the text), and the surrounding prompts/digest.md template adds
    fixed overhead of its own. Summing raw text lengths would systematically
    underestimate the real prompt size and let an oversized batch through
    anyway -- the whole point of this function is to catch that gap by
    measuring the actual thing that gets handed to the model, in the unit
    (bytes) that actually correlates with tokens.

    `items` is assumed already ordered oldest-first (get_unsummarized_items
    returns it that way) -- this function preserves that order and never
    reorders or drops from the middle, it only shrinks from the end.

    Finds the LONGEST fitting prefix, not just *a* fitting one: the full
    list is tried first (the common case -- everything fits, return as-is
    with zero extra build_prompt calls). If that misses, this binary
    searches for the largest n in [1, len(items) - 1] whose built prefix
    fits, rather than halving n on a miss and stopping at the first hit.
    Halving-and-stop can badly under-fill: e.g. if 199 of 200 items would
    have fit, halving jumps straight to 100 and returns it, needlessly
    discarding 99 items (and roughly halving how fast the backlog drains)
    even though a much longer prefix fits. Binary search still converges in
    O(log n) build_prompt calls -- same order of magnitude as halving -- but
    lands on the actual longest fitting prefix instead of an arbitrary
    shorter one. Each build_prompt call is itself O(prefix size); at the
    n <= 200 scale this module operates at (_MAX_ITEMS_PER_DIGEST in
    digest/main.py), a handful of O(log n) builds is cheap.

    The search's fit predicate (`len(build_prompt(items[:n], ...).encode(
    "utf-8")) <= max_prompt_bytes`) is monotone in n: a longer prefix only
    ever adds JSON/text, never removes it, so built-prompt byte length is
    non-decreasing as n grows. That monotonicity is what makes binary search
    valid here.

    Stops at a 1-item floor: build_prompt on a single item always fits,
    because per-item text is already truncated to _MAX_ITEM_TEXT_CHARS by
    build_prompt itself, bounding one item's contribution regardless of its
    original length. n=1 is returned even if it somehow doesn't fit --
    rather than trusting the size check to naturally pass -- so a
    pathological template/overhead blowup can't leave this with nothing to
    return.
    """
    if not items:
        return items

    n = len(items)
    if (
        len(build_prompt(items, failed_sources, recent_coverage, recent_arcs).encode("utf-8"))
        <= max_prompt_bytes
    ):
        return items

    if n == 1:
        return items

    lo, hi = 1, n - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = items[:mid]
        if (
            len(
                build_prompt(candidate, failed_sources, recent_coverage, recent_arcs).encode(
                    "utf-8"
                )
            )
            <= max_prompt_bytes
        ):
            lo = mid
        else:
            hi = mid - 1
    return items[:lo]


def select_balanced_items_for_prompt(
    pool: list[Item],
    budget: int,
    positions_channels: Collection[str],
    failed_sources: list[str],
    recent_coverage: str,
    max_prompt_bytes: int,
    recent_arcs: str = "",
) -> list[Item]:
    """Allocate a source-balanced batch from `pool` that also fits the prompt byte cap.

    Composes the two selection concerns this module previously handled
    separately -- and, when both bound at once, INCOMPATIBLY:
    allocate_by_source balances lanes by quota, while select_items_for_prompt
    shrinks to the byte cap by keeping the longest fitting PREFIX of the
    fetched_at-ordered list. Because collectors batch-stamp fetched_at in
    run order (telegram, x, news, polymarket, reddit -- digest/main.py's
    `_run`), that prefix cut always discards the LAST collectors first: the
    exact windows fat enough to trip the byte cap were the ones where the
    quota's balance got chopped from the reddit/news end. This function
    closes that gap by shrinking the ALLOCATION BUDGET instead of the list's
    tail: when the allocated batch doesn't fit, it re-allocates at a smaller
    budget (proportionally scaled quotas -- see allocate_by_source), so
    every lane gives up items in its quota's own ratio rather than the last
    collectors giving up everything.

    Search strategy: the first over-budget miss reuses select_items_for_
    prompt's binary search to learn HOW MANY items actually fit (the longest
    fitting prefix's length is a solid byte-informed estimate of the right
    budget, at O(log n) build_prompt calls), re-allocates at that budget,
    then walks down by ~10% steps for the rare case where the balanced batch
    at that count is byte-heavier than the prefix was (different items, so
    the fit isn't guaranteed transferable). Floors at a 1-item budget,
    returned even if it somehow doesn't fit -- the same floor
    select_items_for_prompt itself guarantees, for the same reason.

    Like both parents: a pure filter of `pool`, order preserved, so the
    summarized set and create_digest's stamped set stay identical by
    construction.
    """
    items = allocate_by_source(pool, budget, positions_channels)

    def _fits(candidate: list[Item]) -> bool:
        built = build_prompt(candidate, failed_sources, recent_coverage, recent_arcs)
        return len(built.encode("utf-8")) <= max_prompt_bytes

    if _fits(items):
        return items

    # Byte-informed budget estimate: how many of THESE items fit as a
    # prefix. The re-allocated batch at this budget usually fits too, since
    # it has the same count drawn from the same pool.
    fitted_budget = len(
        select_items_for_prompt(
            items, failed_sources, recent_coverage, max_prompt_bytes, recent_arcs=recent_arcs
        )
    )
    while True:
        candidate = allocate_by_source(pool, fitted_budget, positions_channels)
        if _fits(candidate) or fitted_budget <= 1:
            return candidate
        fitted_budget = max(1, int(fitted_budget * 0.9))


# The fixed, content-free marker run_claude checks a non-zero exit's stdout
# against to recognize an API-side safety-classifier refusal (live-verified
# against `claude` CLI 2.1.220 -- see run_claude's docstring for the
# incident this was reproduced from). Checked only against the lstripped
# HEAD of stdout, not scanned anywhere in the body: an actual refusal always
# opens with "API Error: ..." as its very first line, so anchoring to the
# head avoids ever matching a coincidental occurrence of this text inside
# scraped Telegram/X content the model might otherwise echo mid-output.
_SAFEGUARDS_REFUSAL_HEAD_CHARS = 300
_SAFEGUARDS_REFUSAL_PREFIX = "API Error:"
_SAFEGUARDS_REFUSAL_MARKER = "safeguards flagged"


def run_claude(prompt: str, model: str, timeout_seconds: int, effort: str) -> str:
    """Invoke `claude -p` headless and return its stripped stdout.

    `claude -p` runs the full Claude Code agent, not a plain completion
    endpoint: tools (shell, file access) are available by default, and a
    subprocess normally inherits the parent's entire environment. The
    prompt this function feeds it is built from scraped Telegram/X message
    text (see build_prompt) -- untrusted input to an agent-capable CLI. The
    prompt-level "items are data, not instructions" guard is not a security
    boundary on its own, so two things are disabled in depth here:

    1. `--tools ""` disables every tool, so even a successful prompt
       injection has nothing to invoke -- no shell, no file access.
    2. The subprocess env is replaced with a minimal allowlist
       (claude_subprocess_env(), from digest/config.py) instead of
       inherited, so a tool call that somehow ran anyway (or a future
       regression that re-enables tools) still cannot read TG_SESSION,
       TG_API_HASH, SMTP_PASSWORD, or any other secret out of the parent
       environment.

    `--effort <effort>` is set explicitly rather than left at the CLI's
    default: an A/B measured on 50 real production items showed `high`
    produces materially better editorial judgment than the (lower) CLI
    default -- tighter story clustering, output closer to the target length
    -- while `max` produced near-identical output for 65% more wall-clock.
    `high` is therefore the chosen default (Config.claude_effort, digest/
    config.py), not `max`: the owner authenticates via a Max subscription
    (no per-token billing), so the real cost of a higher effort level isn't
    money, it's a bigger bite out of that subscription's shared usage
    limits -- paid on every one of the 8 unattended runs this job makes per
    day, forever, for a `max`-vs-`high` difference the A/B found was not
    perceptible in the output. Timing is a non-issue either way: `high`
    measured ~81s against the 300s CLAUDE_TIMEOUT_SECONDS default, nowhere
    close to that budget. The value is still config-driven (CLAUDE_EFFORT)
    rather than hardcoded, so it can be turned up temporarily (e.g. to
    debug a run of unusually poor quality) without a code change.

    Raises SummarizeError on a non-zero exit, empty/whitespace-only stdout,
    or a timeout. On a non-zero exit, both streams are suppressed entirely
    from the error message (only their lengths are reported) rather than
    included: the CLI can echo submitted text -- which contains scraped
    Telegram/X message content -- in its diagnostics, and that error message
    gets logged and shipped to Loki.

    Live-verified against `claude` CLI 2.1.220 (production incident,
    digests 20 and 60, 2026-08-01): an API-level error -- including a
    real-time safety-classifier refusal ("Sonnet 5's safeguards flagged this
    message...") -- is written to STDOUT with exit 1 and EMPTY stderr, the
    reverse of what the original error message here assumed. A non-zero
    exit is therefore checked against a fixed, content-free marker (stdout's
    head starting with "API Error:" and containing "safeguards flagged") to
    tell a deterministic safety-classifier refusal apart from every other
    non-zero-exit failure shape, and raises SafeguardsRefusalError instead
    of the generic SummarizeError when it matches -- so callers can tell
    "retrying this exact model is pointless" from "this may just be
    transient" without parsing the (secret-bearing) output themselves. The
    marker is checked, never logged or echoed: only its match/no-match
    verdict crosses into the raised exception.
    """
    try:
        # cwd is a fresh empty directory: the CLI auto-ingests workspace
        # context (CLAUDE.md, git state) from wherever it runs — live-verified
        # that running from this repo leaks project context into the
        # summarization. A neutral cwd keeps the prompt the only input.
        with tempfile.TemporaryDirectory(prefix="digest-claude-") as neutral_cwd:
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    "--model",
                    model,
                    "--output-format",
                    "text",
                    "--tools",
                    "",
                    "--effort",
                    effort,
                ],
                input=prompt,
                capture_output=True,
                text=True,
                # Explicit encoding="utf-8": build_prompt now serializes with
                # ensure_ascii=False, so the prompt can contain raw non-ASCII
                # (emoji, CJK, etc.) UTF-8 text instead of \uXXXX escapes.
                # text=True alone would pick subprocess's default text
                # encoding, which falls back to locale.getpreferredencoding()
                # -- and claude_subprocess_env() is a scrubbed minimal
                # allowlist that carries no LANG/LC_ALL, so that fallback can
                # land on a non-UTF-8 (even ASCII-only) locale encoding and
                # raise UnicodeEncodeError writing the prompt to stdin. Pinning
                # utf-8 explicitly makes this independent of the parent
                # process's or subprocess's locale entirely.
                encoding="utf-8",
                timeout=timeout_seconds,
                env=claude_subprocess_env(),
                cwd=neutral_cwd,
            )
    except subprocess.TimeoutExpired as exc:
        raise SummarizeError(f"claude -p timed out after {timeout_seconds}s") from exc

    if result.returncode != 0:
        stdout_head = result.stdout.lstrip()[:_SAFEGUARDS_REFUSAL_HEAD_CHARS]
        if (
            stdout_head.startswith(_SAFEGUARDS_REFUSAL_PREFIX)
            and _SAFEGUARDS_REFUSAL_MARKER in stdout_head
        ):
            raise SafeguardsRefusalError(
                f"claude -p exited {result.returncode}: API safety classifier flagged "
                "the prompt content (deterministic refusal — a same-model retry cannot "
                "succeed)"
            )
        raise SummarizeError(
            f"claude -p exited {result.returncode} (stdout suppressed, "
            f"{len(result.stdout)} chars; stderr suppressed, {len(result.stderr)} "
            "chars — rerun manually to inspect)"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise SummarizeError("claude -p returned empty output")

    return stdout


def _leading_whitespace_column(line: str) -> int:
    """Compute the CommonMark indentation column of a line's leading whitespace.

    Per CommonMark, indentation is measured in columns, not characters: a
    space advances the column by exactly 1, but a tab advances to the NEXT
    multiple-of-4 column (`col = col + 4 - (col % 4)`) -- the standard
    tab-stop expansion rule, identical to how a terminal renders a tab.
    Stops at the first character that is neither a space nor a tab. A line
    like " \t## Example" (one space, then a tab) reaches column 4
    from just two leading characters -- neither `line.startswith("\t")` nor
    `line[:4] == "    "` catches that, since the line starts with a space
    and its first four characters aren't four literal spaces, so a
    character-counting check silently misses this case.
    """
    col = 0
    for ch in line:
        if ch == " ":
            col += 1
        elif ch == "\t":
            col += 4 - (col % 4)
        else:
            break
    return col


def _real_heading_lines(markdown_text: str) -> list[str]:
    """Return the raw text (original case, merely stripped) of every real ATX `## ` heading line.

    This is the exact CommonMark-aware scanning logic validate_output has
    always used to decide what counts as a "real" `## ` heading -- fence
    tracking (backtick and tilde, matching delimiter character, closer run
    length >= opener run length, no info string on a closer) and
    column-based, tab-aware indentation exclusion. It was pulled out of
    validate_output verbatim, not reimplemented, specifically so a second
    caller (format_recent_coverage, below) can reuse the identical notion of
    "real heading" without the two ever being able to silently drift apart.
    See validate_output's docstring for the full CommonMark reasoning behind
    every rule enforced here; it is not repeated a second time in this
    docstring.

    Returns heading text in ORIGINAL case, with only surrounding whitespace
    stripped -- callers that need case-insensitive comparison (validate_output,
    to normalize before its emptiness check; format_recent_coverage, to match
    the "Needs attention" routing label case-insensitively) lowercase it
    themselves. This function makes no assumption about how its caller will
    use the text, so it doesn't discard case information the caller might
    need (format_recent_coverage renders the heading verbatim, in its
    original case, into the "recently covered" list).
    """
    heading_lines = []
    in_fence = False
    fence_char = None
    fence_len = 0
    for line in markdown_text.splitlines():
        # CommonMark: leading whitespace reaching column 4 or more makes
        # this an indented code block -- neither a heading nor a fence
        # delimiter can start here, regardless of what follows the
        # indentation. Computed via CommonMark tab-expansion rules (see
        # _leading_whitespace_column), not pattern-matched, so mixed
        # space+tab indentation that reaches column 4 is caught too.
        if _leading_whitespace_column(line) >= 4:
            continue
        stripped = line.strip()
        if in_fence:
            # CommonMark fence-closing rule: a closer must (1) start with a
            # run of the SAME delimiter character that opened the fence,
            # (2) that run must be AT LEAST as long as the opening run, and
            # (3) nothing but whitespace may follow the run -- unlike an
            # opener, a closer may not carry an info string. A ``` line
            # inside a ~~~ fence never matches (wrong character); a 3-tick
            # line inside a 4-tick fence matches too short a run and is just
            # content; "``` python" has trailing non-whitespace and is also
            # just content, not a closer.
            run_len = len(stripped) - len(stripped.lstrip(fence_char))
            remainder = stripped[run_len:]
            if run_len >= fence_len and remainder.strip() == "":
                in_fence = False
                fence_char = None
                fence_len = 0
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence_char = stripped[0]
            fence_len = len(stripped) - len(stripped.lstrip(fence_char))
            in_fence = True
            continue
        if stripped.startswith("## "):
            heading_lines.append(stripped[3:].strip())
    return heading_lines


def validate_output(markdown_text: str) -> None:
    """Enforce that the briefing markdown carries at least one real `## ` heading.

    `run_claude` only guarantees non-empty stdout — a refusal ("I can't help
    with that") would otherwise pass through untouched. If that garbage
    reaches `_deliver`, every item gets stamped with the digest id and, once
    SMTP succeeds, the digest row is final: those items are lost to
    summarization forever. This check must run before anything is
    persisted, and it must raise SummarizeError on failure so the caller
    skips writing a digest row entirely -- with no row recorded, the next
    run re-selects and re-summarizes the same items instead of treating
    them as already handled.

    The prompt's contract (prompts/digest.md) used to mandate three fixed,
    always-present section headings in a fixed order; this function used to
    hard-gate exactly that. That contract is gone: the current briefing
    format is free-form prose whose section headings are chosen per-run from
    whatever the window actually contains (an event's own name, a group's
    name, "## Also this window", ...) -- there is no fixed heading text left
    to check for. Hard-gating stylistic compliance (section names, counts,
    ordering, word budget, citation density, ...) against a model that can
    legitimately vary its wording every run is exactly the failure mode this
    repo already lived through once (see the old three-heading contract this
    replaces, and its git history): when the model drifts from a hard-gated
    stylistic template, retries loop forever instead of shipping something
    useful, because there is no guarantee ANY rerun converges on the exact
    template text. So this gate is now deliberately MINIMAL and STRUCTURAL
    only -- it enforces the one property a legitimate briefing can never
    fail to have (at least one real markdown heading) and nothing about the
    heading's text, count, or position. Everything about quality --
    "TL;DR opens the briefing", "at most ~8 sections", "roughly 900 words",
    the citation format -- lives in the prompt as an instruction to the
    model, not as a raising validator here; see summarize()'s soft checks
    for the two cases (missing TL;DR, zero citations) worth a log line
    without holding the whole run hostage over a nicety.

    A plain substring check (`"## " in text`) is fooled by a refusal that
    merely *mentions* a heading-shaped string inline -- e.g. "I cannot
    produce a ## heading in this case." contains the substring without a
    single real heading line. So instead this parses actual heading LINES: a
    line whose stripped form starts with exactly "## " (two hashes -- "### "
    subgroup headings are not h2 and don't count). At least one such line
    must exist; if none does (a bare refusal, empty prose, or any other
    output with zero real h2 headings), this raises. This is still exactly
    what stops a bare refusal like "I can't help with that" from passing as
    a real briefing -- the whole point of keeping a gate at all.

    The error message never includes the offending output itself, since it
    may contain scraped Telegram/X message content (see SummarizeError).

    Links are deliberately NOT validated here: a window with nothing but
    chatter legitimately produces zero citation links, and that is correct
    output, not a contract violation (see summarize()'s soft warning for
    this case instead).

    Fenced code blocks are also excluded from heading line detection: a
    refusal can legitimately quote heading-shaped text inside a fenced block
    (e.g. "here's the template you asked about:\n```\n## Example\n...") and
    that is not a real section -- it is example text sitting inside a code
    fence. Per CommonMark, a fence can
    be delimited by three-or-more backticks OR three-or-more tildes, and a
    fence only closes on a line starting with three-or-more of the SAME
    delimiter character that opened it -- a ``` line inside a ~~~ fence
    (or vice versa) is just fence content, not a closer. While the in-fence
    flag is set, "## " lines are not counted as headings, and the fence
    delimiter lines themselves are never counted either.

    CommonMark also requires the closing fence to be AT LEAST as long as
    the opening fence, and to contain nothing but the delimiter run plus
    trailing whitespace -- no info string is allowed on a closer (unlike
    the opener, which may carry one, e.g. ```json). So a 4-backtick opener
    is not closed by a 3-backtick line (that line is just fence content),
    and a line like "``` python" or "```extra" never closes a fence at all,
    regardless of run length, because it has non-whitespace after the
    delimiter run. This module tracks both the delimiter character and the
    opening run length to enforce this.

    Indented lines are excluded from heading and fence-delimiter detection,
    before any stripping happens: per CommonMark, an ATX heading (or a
    fence delimiter) may be indented at most 3 columns -- a line whose
    leading whitespace reaches column 4 or more is an indented code block
    instead. A refusal that pads a template with 4-space indentation (e.g.
    "    ## Example") is therefore code content, not a real heading, and
    must not satisfy the contract.

    Column, not character count, is what CommonMark actually measures: a
    tab does not advance by a literal 4 columns, it advances to the NEXT
    multiple-of-4 column (`col = col + 4 - (col % 4)`), the same rule a
    terminal or renderer uses to expand tabs. A single space always
    advances by exactly 1. This means a line can reach column 4 -- and so
    count as indented code -- with fewer than 4 leading characters: " \t"
    (one space, column 1, then a tab that jumps straight to column 4) is
    two characters but column 4, and a naive `line[:4] == "    "` /
    `line.startswith("\t")` check misses it entirely (the line doesn't
    start with a tab, and its first four characters aren't four literal
    spaces) -- letting a mixed space+tab-indented refusal template slip
    past this check as if it were a real heading line. See
    _leading_whitespace_column.

    The scanning itself (fence tracking, indentation exclusion) lives in the
    shared helper _real_heading_lines, not here -- it was pulled out
    verbatim so format_recent_coverage (which needs the actual heading TEXT,
    not just a yes/no) can reuse the identical CommonMark logic rather than
    risk a second, subtly different implementation drifting out of sync with
    this one. This function's own job is just the two things layered on top:
    lowercase for case-insensitive emptiness checking, and raising when
    nothing real is left.
    """
    heading_lines = [h.lower() for h in _real_heading_lines(markdown_text) if h]

    if not heading_lines:
        raise SummarizeError("digest output has no real '## ' heading line")


# format_recent_coverage's routing-label skip: "## Needs attention" is not a
# story, it's the prompt's own routing mechanism for "this needs the
# reader's action" (see prompts/digest.md's "Needs attention" section) --
# every digest that has anything urgent gets one, so treating it as "already
# covered" would suppress a FUTURE window's own Needs attention section just
# because a past one happened to exist, for a completely unrelated reason.
# Compared case-insensitively against _real_heading_lines' output, which
# preserves original case.
_NEEDS_ATTENTION_HEADING = "needs attention"

# format_recent_coverage's skip list: structural/rubric headings that appear
# by STANDING RULE rather than because a story happened (prompts/digest.md).
# "Needs attention" is the only member: it is a routing label, not a story
# (see _NEEDS_ATTENTION_HEADING's comment above). The portfolio and
# Hungarian standing-coverage rules deliberately do NOT add entries here --
# their sections are STORY-TITLED by contract (never a fixed "Positions"/
# "Hungary" label to match on), and having them participate in the
# "Recently covered" delta rule is the POINT: the reader wants what's new
# from those channels each window, not a re-explanation, and their
# guaranteed presence is enforced by the prompt's own presence rule (one
# sentence in Also-this-window minimum), never by hiding them from
# coverage. A briefly-labeled experiment (2026-08-17, PR #75) that DID skip
# literal "positions"/"hungary" headings here was reverted the same day
# when the sections went story-first.
_STANDING_RUBRIC_HEADINGS = frozenset({_NEEDS_ATTENTION_HEADING})

# format_recent_coverage caps the number of "recently covered" lines it will
# ever render, regardless of how many digests or headings are available.
# This is a hard ceiling on how much of the prompt budget the coverage block
# can consume: at 24h of history and an 8-section-ish briefing every 6 hours,
# a healthy run produces on the order of 4 runs * ~8 headings = ~32 candidate
# lines even before the "Needs attention" skip -- comfortably under this cap.
# It was NOT comfortable while the timer ran 3-hourly: the same math gave
# 8 runs * ~8 = ~64, which pressed against the cap on any ordinary day. The
# cap deliberately stays 50 anyway, because what it defends against did not
# get any rarer with the cadence -- an unusually busy day, or a pathological
# run that emits far more headings than the prompt's own ~8-section budget
# asks for, could still make this block grow open-endedly. 50 keeps the
# coverage block bounded and cheap relative to _MAX_PROMPT_BYTES's other
# consumers (items, collector status) without needing to special-case why a
# particular day's history was unusually large.
_MAX_RECENT_COVERAGE_LINES = 50

# format_recent_coverage truncates any single sanitized heading longer than
# this many characters. A real `## ` heading (prompts/digest.md's own
# contract) is a short topic label ("Missile strike in Poland", "ASI
# Alliance: token migration questions") -- normal headings are nowhere near
# this length. This exists purely to bound a pathological or hostile model
# output (e.g. a heading that somehow ballooned to paragraph length) so one
# bad past digest can't blow up this run's prompt budget on its own; it is
# not expected to ever trigger on a well-formed heading.
_MAX_RECENT_COVERAGE_HEADING_CHARS = 160

_NO_RECENT_COVERAGE = "(no prior briefings in the last 24 hours)"


def _sanitize_recent_coverage_heading(heading: str) -> str:
    """Neutralize a past heading's three hazards before it is rendered into this run's prompt.

    `heading` came out of _real_heading_lines applied to a PAST digest's
    body_md -- text the model itself generated, but generated FROM the same
    untrusted, scraped Telegram/X/news material this run's items come from
    (see format_recent_coverage's docstring for the full threat model). Three
    specific things are neutralized here, each for a distinct reason:

    1. Backticks are stripped entirely. The recent-coverage block is
       embedded in the prompt inside a fenced ```text block (see
       prompts/digest.md's "Recently covered" section) exactly like the
       items JSON is embedded in a fenced ```json block -- and exactly the
       same hazard build_prompt's backtick-escaping of item text defends
       against applies here: a literal ``` sequence surviving into a past
       heading could make the model perceive the fence as closed early,
       exposing whatever coverage lines follow (or the prompt text after
       them) as if they were outside the "this is data" boundary rather than
       inside it. Removing every backtick (rather than escaping it, the way
       build_prompt does for JSON-embedded text) is enough here because,
       unlike the JSON payload, this text is never parsed back out of the
       fence programmatically -- it only has to read sensibly as plain
       prose, and a topic heading missing a backtick reads identically to a
       reader either way.
    2. Every "{{" is broken into "{ {" (a literal space inserted between the
       braces). RECENT_COVERAGE is substituted into the prompt template via
       str.replace BEFORE {{ITEMS_JSON}} (see build_prompt's ordering
       comment) specifically so that this text is never itself rescanned by
       a LATER .replace() call -- but that ordering rule alone only protects
       against str.replace's own rescanning behavior. This sanitization is
       the complementary, second layer: even if some future refactor changed
       that ordering, or another consumer read this rendered coverage block
       and ran its own placeholder substitution over it, a real
       "{{COLLECTOR_STATUS}}" or "{{ITEMS_JSON}}" token could never have
       survived into the string in the first place, because any "{{" was
       already broken before this function returns.
    3. The heading is truncated to _MAX_RECENT_COVERAGE_HEADING_CHARS. This
       bounds one pathological or hostile heading's contribution to this
       run's prompt size -- see that constant's own comment for why a real
       heading is never expected to be anywhere near this long.

    Returns the sanitized heading with leading/trailing whitespace stripped
    (truncation, in particular, can leave trailing whitespace at the cut
    point). Order matters: backticks are removed and "{{" is broken BEFORE
    truncating, so the final length bound applies to the text that actually
    reaches the prompt, not to a pre-sanitization length that sanitization
    would then shrink further.
    """
    sanitized = heading.replace("`", "")
    # Lookahead, not a plain replace("{{", "{ {"): a plain replace is a
    # single non-overlapping left-to-right pass, so a brace RUN of three or
    # more defeats it -- "{{{COLLECTOR_STATUS}}}" becomes
    # "{ {{COLLECTOR_STATUS}}}", which still contains the live
    # "{{COLLECTOR_STATUS}}" placeholder (the pass consumed the first two
    # braces and never re-examined the pair formed by the second and third).
    # The lookahead consumes only the FIRST brace of each adjacent pair, so
    # every pair in a run of any length gets a space inserted in one pass:
    # "{{{" -> "{ { {". No brace adjacency can survive, of any run length.
    sanitized = re.sub(r"\{(?=\{)", "{ ", sanitized)
    if len(sanitized) > _MAX_RECENT_COVERAGE_HEADING_CHARS:
        sanitized = sanitized[:_MAX_RECENT_COVERAGE_HEADING_CHARS]
    return sanitized.strip()


def _format_digest_age(created_at: str, now: datetime) -> str:
    """Render `created_at` (ISO8601 UTC) as a whole-hour age relative to `now`, e.g. "3h ago".

    Floors to whole hours (via integer division of the elapsed seconds) --
    the reader-facing "recently covered" list only needs a coarse sense of
    how stale a story is ("this was covered a few hours ago" vs "just now"),
    not minute-level precision. Ages under one full hour render as "<1h ago"
    rather than "0h ago", since "0h ago" reads as if no time at all has
    passed, which is misleading for e.g. a digest created 55 minutes ago.
    """
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        # Every created_at this codebase writes is timezone-aware UTC
        # (datetime.now(UTC).isoformat(), see state.py's create_digest) --
        # this fallback only guards a hypothetically naive timestamp (e.g.
        # from a hand-edited test fixture or a pre-migration row) so
        # subtraction against an aware `now` doesn't raise TypeError.
        created = created.replace(tzinfo=UTC)
    age_hours = int((now - created).total_seconds() // 3600)
    if age_hours < 1:
        return "<1h ago"
    return f"{age_hours}h ago"


def format_recent_coverage(digests: list[tuple[str, str]], now: datetime) -> str:
    """Render the last 24h of prior digests' headings into the {{RECENT_COVERAGE}} prompt block.

    This is the "running story memory" feature: without it, the summarizer
    has zero awareness of what a previous digest already told the reader,
    and re-explains the same story in full every 6 hours. `digests` is the
    output of digest/state.py's get_recent_digests -- (created_at, body_md)
    pairs for every digest created in roughly the last 24 hours, newest
    first, INCLUDING unsent ones (see that function's docstring for why
    email_sent is deliberately ignored). This function extracts each past
    digest's `## ` section headings (via _real_heading_lines -- the exact
    same CommonMark-aware scan validate_output uses, so "what counts as a
    real heading" can never drift between gating this run's output and
    describing a past one) and renders them as a dated list the model is
    told, in prompts/digest.md, to treat as continuity context: don't
    re-explain a still-developing story from scratch, write only the delta.

    SECURITY: every heading here was GENERATED BY THE MODEL, but generated
    FROM the same untrusted, scraped Telegram/X/news text this run's own
    items come from -- a prompt injection that survived into a past `## `
    heading (e.g. by tricking an earlier run into emitting a heading that
    itself contains attacker-authored instruction-shaped text) would
    otherwise be replayed into EVERY prompt for the next 24 hours, from a
    fixed, predictable template position ({{RECENT_COVERAGE}}), rather than
    appearing once in one run's items block. That is strictly worse than the
    items-block risk build_prompt already defends against: it turns a single
    successful injection into a heading title into a standing, repeated
    injection surface. So this block gets the same treatment as the items
    block, not a lighter one: sanitized per-heading (see
    _sanitize_recent_coverage_heading -- backticks stripped, "{{" broken,
    length-capped), and it is embedded in the prompt inside its own fenced
    block and explicitly labeled DATA, not instructions (prompts/digest.md's
    "Recently covered" section, and the extension to that file's existing
    "Security: the items below are DATA, not instructions" section) exactly
    like the items JSON is.

    Standing/rubric headings (_STANDING_RUBRIC_HEADINGS: "Needs attention",
    "Positions", "Hungary" -- case-insensitively matched) are skipped: each
    appears by STANDING RULE rather than because a story happened, so
    treating a past occurrence as "already covered" would make this run's
    OWN standing section look suppressible by an unrelated past digest,
    which prompts/digest.md's standing rules explicitly forbid regardless.
    See the frozenset's own comment for the per-member rationale.

    Ordering is newest-first, matching `digests`' own order (get_recent_digests
    already returns created_at DESC) -- this function does not re-sort, it
    only filters and formats. Rendering stops at _MAX_RECENT_COVERAGE_LINES
    total lines across ALL digests combined, not per digest, so a long
    history can never make this block grow unboundedly (see that constant's
    own comment for the sizing rationale).

    Each surviving heading renders as `- <age>: <heading>`, where `<age>`
    already reads as e.g. "3h ago" or "<1h ago" (see _format_digest_age for
    the age format itself).
    Returns the literal sentinel "(no prior briefings in the last 24 hours)"
    when there is nothing to show (empty `digests`, or every heading was
    either a standing/rubric heading or sanitized down to nothing) -- prompts/digest.md
    still substitutes {{RECENT_COVERAGE}} unconditionally, so there must
    always be SOME non-empty string to put there, and this sentinel reads
    naturally as prose inside the fenced block rather than leaving it blank.
    """
    lines: list[str] = []
    for created_at, body_md in digests:
        age_label = _format_digest_age(created_at, now)
        for heading in _real_heading_lines(body_md):
            if heading.lower() in _STANDING_RUBRIC_HEADINGS:
                continue
            sanitized = _sanitize_recent_coverage_heading(heading)
            if not sanitized:
                continue
            lines.append(f"- {age_label}: {sanitized}")
            if len(lines) >= _MAX_RECENT_COVERAGE_LINES:
                return "\n".join(lines)

    if not lines:
        return _NO_RECENT_COVERAGE
    return "\n".join(lines)


# --- Recently used story-arc keys (stable-arc-keys feature) ---

# Matches a valid stable arc key: a lowercase ASCII letter or digit, then a
# run of zero-to-47 more lowercase ASCII letters/digits/hyphens (48 chars
# total, matching prompts/digest.md's own "Story-arc keys" contract). This is
# the SAME pattern extract_arc_keys (below) validates every parsed key
# against, and format_recent_arcs re-validates every STORED key against
# before replaying it into a future prompt -- see that function's own
# SECURITY note for why a single regex check is enough here, unlike
# format_recent_coverage's free-form heading text.
_ARC_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

_NO_RECENT_ARCS = "(no arcs recorded yet)"

# Caps how many keys format_recent_arcs will ever render into one prompt.
# digest/state.py's get_recent_arc_keys already caps its own query (see that
# function's docstring) -- this is defense in depth, mirroring
# _MAX_RECENT_COVERAGE_LINES' identical belt-and-suspenders posture for the
# sibling RECENT_COVERAGE block, not the primary bound.
_MAX_RECENT_ARCS = 50


def format_recent_arcs(arc_keys: list[tuple[str, int]]) -> str:
    """Render the {{RECENT_ARCS}} prompt block from digest/state.py's get_recent_arc_keys output.

    `arc_keys` is a list of (stable arc key, appearance count) pairs across
    window digests in roughly the last 7 days (see get_recent_arc_keys),
    already ordered most-frequent-first and capped by that query. This is
    the "story continuity" list prompts/digest.md's "Story-arc keys" section
    tells the model to check before minting a brand-new key: if a story in
    this window continues one already on this list, reuse it verbatim.

    Each line renders as `- <key> (covered in N briefings)` -- the count is
    the model's over-coverage signal (prompts/digest.md's section-budget
    rule leans on it to demote a heavily-repeated arc to delta-only
    treatment), so it must survive into the prompt, not just order the
    query. Counts are clamped to >= 1 defensively; the count is
    model-facing display data, never parsed back.

    SECURITY: every key here was MODEL-GENERATED, in a past run, from the
    same untrusted scraped material this run's items come from -- the
    identical threat model format_recent_coverage's own SECURITY note
    describes for past `## ` headings. Unlike a heading, though, a key is
    not free-form prose: it can only ever reach this list after already
    surviving extract_arc_keys' `_ARC_KEY_RE` validation once, at parse
    time, and being stored and read back unchanged. This function
    re-validates every key against that SAME regex anyway, as defense in
    depth -- and because `_ARC_KEY_RE`'s charset (lowercase ASCII letters,
    digits, hyphens only) cannot contain a backtick or a "{" in the first
    place, this single check is a STRICTER guarantee than
    format_recent_coverage's own strip-backticks/break-braces sanitizing
    pass: there is nothing of either shape left to strip or break. Any key
    that somehow fails the check (a future storage-layer bug, a hand-edited
    row) is silently skipped rather than rendered raw.

    Capped at `_MAX_RECENT_ARCS` entries -- defense in depth, see that
    constant's own comment.

    Returns the literal sentinel "(no arcs recorded yet)" when there is
    nothing to show (empty `arc_keys`, or every key failed validation) --
    prompts/digest.md still substitutes {{RECENT_ARCS}} unconditionally, so
    there must always be some non-empty string to put there.
    """
    lines: list[str] = []
    for key, count in arc_keys:
        if not _ARC_KEY_RE.match(key):
            continue
        appearances = max(1, count)
        noun = "briefing" if appearances == 1 else "briefings"
        lines.append(f"- {key} (covered in {appearances} {noun})")
        if len(lines) >= _MAX_RECENT_ARCS:
            break

    if not lines:
        return _NO_RECENT_ARCS
    return "\n".join(lines)


# Markdown inline link: `[text](url)`, optionally with a title
# (`[text](url "title")` or `[text](url 'title')`) and/or an
# angle-bracketed URL (`[text](<url>)`). Good enough for our contract --
# the digest's own template only ever emits flat, non-nested links -- but
# it must still match every inline form python-markdown's default
# renderer turns into a real `<a href>` anchor, since a form we don't
# match is a form we silently let through with its raw URL intact.
_MARKDOWN_LINK_RE = re.compile(
    r"\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+(\"[^\"]*\"|'[^']*'))?\s*\)"
)
# CommonMark autolink form: `<scheme:destination>` -- a bare URI wrapped in
# angle brackets, with no link text of its own. Per the CommonMark grammar
# an autolink's scheme is not limited to http/https: any URI scheme works
# (`<mailto:attacker@example.com>`, `<ftp://evil.example/x>`, etc.), and mail
# clients linkify those exactly as readily as an http(s) autolink -- so this
# must match ANY scheme, not just http/https, or a non-HTTP autolink slips
# through this pass untouched. URI schemes are themselves case-insensitive
# (RFC 3986) and mail clients linkify `<HTTP://...>` / `<MAILTO:...>` just as
# readily as their lowercase forms, so scheme matching is case-insensitive
# too -- otherwise an uppercase-scheme autolink would skip this pass entirely
# and never be recognized as an autolink to defang.
_AUTOLINK_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9+.\-]*:[^\s<>]*)>", re.IGNORECASE)
# Reference-style link definition line, e.g. `[id]: https://example.com`
# (optionally indented up to 3 spaces, per CommonMark). This does not match
# the *usage* site (`[text][id]`) -- only the definition. Deliberately
# permissive about what follows the URL (title, trailing text) since the
# only decision made here is whether the URL is allowlisted.
_REFERENCE_DEFINITION_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(\S+).*$", re.MULTILINE)
# Any remaining bare URI token, run as the final pass over the whole text --
# catches URIs mail clients would auto-linkify even though they never went
# through a markdown link construct at all (plain prose, or what survives
# after the passes above run). Two alternatives:
#
# 1. Any `scheme://...` token (not just http/https -- `ftp://evil.example/x`
#    is exactly as linkifiable to a mail client as an http(s) URL and must
#    not survive verbatim).
# 2. Any OTHER scheme-shaped bare token, generically -- `[a-zA-Z][\w+.-]*:`
#    followed by at least two non-whitespace, non-colon payload characters.
#    This is deliberately NOT a denylist of specific non-`//` schemes
#    (`tel:`, `sms:`, `geo:`, `mailto:`, ...) -- a mail/messaging client's
#    set of auto-linkified schemes is neither fixed nor fully known to this
#    codebase, and chasing it one scheme at a time is the same losing game
#    as chasing grammar variants. Going generic on the *shape* of a URI
#    (RFC 3986: `scheme ":" ...`) instead catches any bare
#    `tel:+19005551234`, `sms:+1...`, `geo:...`, or `mailto:...` token
#    without needing to know its name in advance. The payload requires >= 2
#    chars so a lone trailing colon (or a colon immediately followed by
#    another colon or whitespace) can't match; excluding `:` from the
#    payload itself means a colon-separated non-URI token like "12:30" is
#    never mistaken for `scheme:payload` in the first place (`12` isn't
#    letter-led, so it never even reaches this alternative), and a prose
#    colon like "Deadline: tomorrow" doesn't match either, since the
#    payload class demands the two-plus chars sit immediately after the
#    colon with no gap, and a space is not a payload character.
#
#    The `(?!//)` right after this alternative's `:` is the load-bearing
#    guard against corrupting the FIRST alternative's job: an actual
#    `scheme://...` token (allowlisted or not) also matches the generic
#    `scheme:` shape up to its colon, so without this guard the two
#    alternatives would race for the same text. Since Python's `re` tries
#    alternatives left-to-right at each position and stops at the first
#    that matches, alternative 1 already wins that race for any `://` token
#    -- this guard on alternative 2 is defense in depth, and it doubles as
#    the fix for a second, more concrete hazard: it stops this alternative
#    from re-matching output the http/https branch of _defang already
#    produced. That branch defangs by renaming the scheme in place
#    (`https://` -> `hxxps://`) rather than breaking the `:` separator the
#    way the generic-scheme defang does (`ftp://` -> `ftp[:]//`), so
#    `hxxps://...` still looks exactly like `scheme://...` -- letters
#    directly followed by `://` -- and would otherwise match this
#    alternative too (`hxxps` reads as a fine scheme name) and get mangled
#    a SECOND time into `hxxps[:]//...`. `(?!//)` excludes it outright: the
#    colon in `hxxps:` IS followed by `//`, so this alternative never even
#    starts. (A generically-defanged token doesn't need a matching guard
#    here: `ftp[:]//host` has no scheme immediately followed by `:` at all
#    -- the `[` breaks that adjacency -- so neither alternative can match it
#    a second time.)
#
# Both alternatives stop at whitespace and the same closing delimiters the
# other patterns exclude (`)`, `]`, `>`, quotes) so neither swallows
# trailing punctuation from an enclosing markdown/HTML construct. URI
# schemes are case-insensitive (RFC 3986) and mail clients linkify
# `HTTPS://...` / `TEL:...` exactly as readily as their lowercase forms, so
# this must match regardless of scheme case -- a lowercase-only pattern
# lets an uppercase- or mixed-case-scheme URI sail through this final pass
# untouched.
#
# The `\b(?!hxxps?://)` guard on the scheme:// alternative exists for the
# same reason as alternative 2's `(?!//)` guard above: it stops that
# alternative from re-matching `hxxps://...`/`hxxp://...` output the
# http/https branch of _defang already produced, corrupting output that
# was already handled correctly. `hxxp`/`hxxps` are never a real scheme
# this codebase collects or allowlists, so excluding them here is safe. The
# leading `\b` is load-bearing, not decorative: a negative lookahead only
# blocks a match from STARTING at that exact position -- without `\b`, the
# regex engine simply retries one character to the right ("xxps://...",
# still letters followed by "://") and matches that shifted substring
# instead, leaving the leading "h" untouched and producing "hxxps[:]//..."
# anyway. Requiring a word boundary right before the scheme means the only
# position "hxxps://" could ever start a match is at its own "h" -- which
# the lookahead already excludes -- so no shifted, one-character-short
# match is possible either.
_BARE_URL_RE = re.compile(
    r"\b(?!hxxps?://)[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s)\]>\"']+"
    # The generic non-// branch's payload is intentionally broad (any run of
    # 2+ non-delimiter chars) so real delimiter-led URIs still match:
    # `mailto:?to=x@y.z` (payload starts with `?`), `tel:*67` (a real
    # vertical-service-code URI, payload starts with `*`), `geo:47.5,...`,
    # `mailto:__attacker@example.com` (payload starts with `__`), etc. An
    # earlier, tighter version of this branch restricted the FIRST payload
    # char to a URI-plausible class (letter/digit/+/~/%/_), which broke
    # exactly those delimiter-led forms (`?`/`*` aren't in that class) — a P2
    # regression.
    #
    # A later attempt fixed that by excluding payloads that merely START with
    # `**`/`__` (a regex lookahead can only anchor at the match's start
    # position), to stop the digest's own mandated "**TL;DR:**" opener from
    # being misread as `scheme=DR, payload=** ...` and mangled into
    # "TL;DR[:]**". But a lookahead anchored at the start can't see the
    # REST of the payload either -- so `(?!\*\*|__)` excluded not just that
    # emphasis artifact but also every legitimate URI whose payload happens
    # to start with `**`/`__`, e.g. the bare autolink-shaped
    # `mailto:__attacker@example.com`, which would then never match this
    # regex at all and survive linkifiable in the text/plain part.
    #
    # The real discriminator is a property of the WHOLE payload, not just
    # its first two characters: is the payload's entire content markdown
    # emphasis punctuation (only `*`/`_`) and nothing else? That can't be
    # expressed as a regex lookahead anchored at the match start, so this
    # branch stays maximally broad here and the actual whole-payload check
    # happens in code, in _replace_bare_url below, once the full match
    # (and thus the full payload) is known.
    # No trailing lookahead here (an earlier revision had one to stop this
    # branch biting into a following URL's scheme): nested/adjacent cases —
    # `**TL;DR:**https://...` (emphasis-glued) and `custom:abchttps://...`
    # (URI whose payload tail is itself scheme-shaped) — are instead handled
    # by running the whole substitution TO FIXPOINT in enforce_link_allowlist:
    # pass 1 defangs the outer colon, which exposes the inner `scheme://`
    # token for pass 2. A single-pass lookahead can protect only one of the
    # two colons, whichever way it is written.
    r"|\b[a-zA-Z][a-zA-Z0-9+.\-]*:(?!//)[^\s:)\]>\"']{2,}",
    re.IGNORECASE,
)


def _defang(url: str) -> str:
    """Break a URI's scheme separator so mail clients won't auto-linkify it.

    ``https://`` -> ``hxxps://``, ``http://`` -> ``hxxp://`` -- the standard
    security-community defanging convention, kept for exactly those two
    schemes since every URL this codebase ever collects or allowlists is
    https (see enforce_link_allowlist's allowed_urls), so this is the only
    pair of schemes where the familiar hxxp/hxxps convention actually
    applies. This only rewrites the scheme prefix, so the destination stays
    human-readable (useful for the "someone sent a suspicious link" summary
    case) while no longer being a live, clickable URL to any client that
    recognizes the scheme.

    Any OTHER scheme (``ftp://``, ``mailto:``, ``tel:``, ``sms:``, ``geo:``,
    a custom scheme, etc. -- deliberately not enumerated; see _BARE_URL_RE's
    comment for why a denylist of specific schemes is the wrong shape here)
    is defanged generically by breaking the ``:`` scheme separator into
    ``[:]`` instead of renaming the scheme: ``ftp://host/path`` ->
    ``ftp[:]//host/path``, ``mailto:user@host`` -> ``mailto[:]user@host``,
    ``tel:+19005551234`` -> ``tel[:]+19005551234``. This also covers a
    scheme with no ``//`` at all (``mailto:``, ``tel:``, ``sms:``, ...) --
    there's nothing special-cased about the *absence* of ``//`` here, since
    breaking the ``:`` separator kills linkification whether or not a
    ``//`` follows it. Breaking the separator, rather than renaming the
    scheme the way hxxp/hxxps do, is what kills linkification for an
    arbitrary scheme -- there is no equivalent "familiar renamed scheme"
    convention for schemes other than http/https, and renaming an arbitrary
    scheme risks coincidentally landing on another scheme a mail client
    DOES recognize.

    The scheme is detected case-insensitively throughout -- URI schemes are
    case-insensitive per RFC 3986, and mail clients linkify `HTTPS://`,
    `MAILTO:`, or `FTP://` exactly as readily as their lowercase forms -- so
    a lowercase-only check here would leave an uppercase- or mixed-case
    scheme live and clickable. Only the prefix/separator is touched; the
    remainder of the URL (including its original case) is preserved exactly.
    """
    if url[:8].lower() == "https://":
        return "hxxps://" + url[8:]
    if url[:7].lower() == "http://":
        return "hxxp://" + url[7:]
    scheme_end = url.find(":")
    if scheme_end != -1:
        return url[:scheme_end] + "[:]" + url[scheme_end + 1 :]
    return url


def enforce_link_allowlist(markdown_text: str, allowed_urls: Collection[str]) -> str:
    """Strip any link whose URL is not an exact member of ``allowed_urls``.

    The digest's contract is that every link in the output comes verbatim
    from a collected Item.url: build_prompt only ever shows Claude those
    URLs, and the prompt never asks it to invent new ones. But Claude can
    still hallucinate a link, or be induced by a prompt injection in the
    scraped source text to emit one, e.g.
    `[read more](https://attacker.example/phish)`. digest/emailer.py's
    nh3.clean only checks the URL *scheme* (http/https) -- a hostile but
    otherwise valid https URL sails straight through that layer untouched.
    This function is the layer that checks link *provenance* instead of
    just syntax, closing that gap.

    Three distinct markdown forms python-markdown's default renderer turns
    into a real `<a href>` anchor are covered here, all against the same
    allowlist:

    1. Inline links, `[text](url)`, including the variations the CommonMark
       grammar allows within the parens: an optional title in either quote
       style (`[text](url "title")` / `[text](url 'title')`), an optional
       angle-bracketed URL (`[text](<url>)`), and flexible whitespace around
       the URL. An unknown URL is repaired to plain text (`text`); a title,
       if present, is dropped along with the parens -- the title is not
       rendered as visible text by python-markdown either way, so dropping
       it loses nothing a reader would see.
    2. Reference-style links, `[text][id]`, defined elsewhere by a separate
       `[id]: url` definition line. The two-part syntax means the URL never
       appears at the `[text][id]` usage site at all -- there is nothing to
       repair there. Instead, this strips the *definition* line: if its URL
       is not allowlisted, the whole line is deleted. python-markdown then
       has no definition for `id`, so every `[text][id]` referencing it
       renders as literal bracket text, not an anchor -- the same "becomes
       plain text" outcome as the inline case, just achieved by removing the
       definition instead of rewriting each usage site (which may be
       numerous, or precede the definition in document order).
    3. CommonMark autolinks, `<scheme:destination>` -- a bare URI with no
       link text of its own, of ANY URI scheme (not just http/https --
       `<mailto:attacker@example.com>`, `<ftp://evil.example/x>`, etc. are
       all valid CommonMark autolinks a mail client will linkify just as
       readily). Unlike the previous implementation, an unknown autolink's
       `<>` wrapper is stripped *and* its URI is defanged (see below) rather
       than surviving as bare text.

    Finally, a fourth pass runs over the *entire* result: every remaining
    bare URI token -- any `scheme://...` token (not just http/https) plus
    any other bare scheme-shaped token (`mailto:...`, `tel:...`, `sms:...`,
    `geo:...`, and any other scheme a mail/messaging client might
    auto-linkify, whether already bare in the model's prose or left behind
    by the passes above) -- is defanged unless it is an exact member of
    ``allowed_urls``. This pass must run last, after the markdown-link and
    reference-definition passes, so that an allowed URL still embedded in a
    surviving markdown link -- which is necessarily also in
    ``allowed_urls`` -- is spared by the membership check rather than
    mangled.

    Defanging exists because this function's output is used verbatim as the
    digest email's text/plain MIME part (see send_digest in
    digest/emailer.py), and mail clients auto-linkify bare URIs of ANY
    scheme in plain text on their own -- there is no HTML anchor layer in
    that part for allowlist enforcement to intercept, and no scheme
    allowlist either (unlike nh3.clean's http/https-only HTML anchor check
    on the rendered part -- see above). Repairing a markdown link construct
    (dropping it to plain text, or deleting the reference definition) is
    enough to stop python-markdown from turning it into an `<a href>` in the
    HTML part, but it does nothing for the plain-text part: the URI text
    itself is still there, and `<mailto:attacker@example.com>`,
    `<https://attacker.example/phish>`, a bare `ftp://evil.example/x`, or a
    bare `https://attacker.example/...` in prose is exactly as clickable to
    a mail client as a real link. Only defanging the scheme separator (not
    just deleting the URI) closes that gap while keeping the destination
    readable. `https://` -> `hxxps://` and `http://` -> `hxxp://` follow the
    familiar security-community convention; every other scheme (including
    `mailto:`, `tel:`, `sms:`, `geo:`, and anything else letter-scheme-
    shaped) is defanged by breaking its `:` separator into `[:]` instead --
    see _defang's docstring for why that's the right generalization, and
    _BARE_URL_RE's comment for why matching is done by URI *shape* rather
    than by enumerating specific non-`//` schemes.

    A deliberate false-positive trade-off applies to this generic bare-
    scheme alternative: a stray prose token that happens to look
    scheme-shaped (e.g. some hypothetical "Deadline:tomorrow" written with
    no space) would get cosmetically mangled into "Deadline[:]tomorrow" --
    harmless, if odd to read. The alternative failure mode -- a genuinely
    linkifiable URI slipping through this pass untouched -- is a real
    provenance bypass, exactly the class of bug this function exists to
    close. In a personal digest, that asymmetry favors defanging a few
    stray prose tokens over ever missing a live URI.

    This repairs rather than rejects: the digest still goes out with
    unknown links neutralized, rather than raising and discarding the
    whole run's output. A hard validation failure here (the way
    validate_output raises on a missing section) would risk looping
    forever if the model keeps emitting a bad link on every retry --
    section headings are a static template the model can plausibly get
    right on a rerun, but there's no reason to expect a rerun would stop
    hallucinating a URL. Known links (and known reference definitions) are
    left completely untouched.

    Only the COUNTS of stripped/defanged links are logged, never the URLs
    themselves: an attacker-chosen URL reaching the log (and Loki) is itself
    exposure -- e.g. an SSRF probe or a tracking domain encoded in the query
    string.
    """
    allowed = set(allowed_urls)
    stripped = 0
    defanged = 0

    def _replace_inline_link(match: re.Match[str]) -> str:
        nonlocal stripped
        text, url = match.group(1), match.group(2)
        if url in allowed:
            return match.group(0)
        stripped += 1
        return text

    result = _MARKDOWN_LINK_RE.sub(_replace_inline_link, markdown_text)

    def _replace_autolink(match: re.Match[str]) -> str:
        nonlocal defanged
        url = match.group(1)
        if url in allowed:
            return match.group(0)
        defanged += 1
        return _defang(url)

    result = _AUTOLINK_RE.sub(_replace_autolink, result)

    def _replace_reference_definition(match: re.Match[str]) -> str:
        nonlocal stripped
        url = match.group(1).strip("<>")
        if url in allowed:
            return match.group(0)
        stripped += 1
        return ""

    result = _REFERENCE_DEFINITION_RE.sub(_replace_reference_definition, result)

    # Final pass: defang every remaining bare URL not in the allowlist. Runs
    # last so allowed URLs still sitting inside a surviving markdown link
    # (necessarily in `allowed`) are spared by the membership check instead
    # of being mangled by this text-wide regex.
    def _replace_bare_url(match: re.Match[str]) -> str:
        nonlocal defanged
        url = match.group(0)
        if url in allowed:
            return url
        # Markdown-emphasis discrimination lives here, in code, rather than
        # in _BARE_URL_RE's regex: a lookahead can only anchor at the
        # match's START position, so it can rule out a payload that
        # *starts* with `**`/`__` but not one whose whole payload consists
        # of nothing else -- and the two are different sets (see
        # _BARE_URL_RE's comment). `scheme:payload` where `payload` is
        # composed entirely of `*`/`_` characters is markdown emphasis
        # punctuation the model emitted right after a colon (e.g. the
        # `DR:**` token inside "**TL;DR:** text"), not a URI, and must be
        # left untouched. Only the non-`//` form needs this check -- a
        # `scheme://...` match's payload starts with `//`, which is never
        # all `*`/`_`, so it can't accidentally trip this.
        scheme_end = url.find(":")
        if scheme_end != -1 and not url[scheme_end + 1 :].startswith("//"):
            payload = url[scheme_end + 1 :]
            if payload and all(ch in "*_" for ch in payload):
                return url
        defanged += 1
        return _defang(url)

    # Run the bare-URL pass TO FIXPOINT (bounded): a nested construct like
    # `custom:abchttps://attacker.example/x` or emphasis-glued
    # `**TL;DR:**https://...` needs one pass to break the OUTER colon and a
    # second to defang the inner `scheme://` token it exposes. Defanged
    # output never rematches (idempotence is tested), so the loop terminates
    # in practice after <=2 passes; the bound is a pure safety rail.
    for _ in range(5):
        next_result = _BARE_URL_RE.sub(_replace_bare_url, result)
        if next_result == result:
            break
        result = next_result

    if stripped or defanged:
        logger.warning(
            "enforce_link_allowlist: stripped %d link(s) and defanged %d bare "
            "URL(s) with non-allowlisted URLs",
            stripped,
            defanged,
        )

    return result


# Matches a citation link exactly as prompts/digest.md's contract emits one:
# `[<superscript-digit(s)>](<url>)`, where the bracketed link TEXT is nothing
# but one or more characters from the superscript-digit set and nothing
# else. This is the identical discriminator digest/emailer.py's
# `_style_citation_anchors` uses (`_SUPERSCRIPT_ONLY_RE.fullmatch`) to tell a
# real citation apart from a normal-text link: matching on the link's exact
# SHAPE, never on position or URL pattern, is what keeps this from ever
# touching a link whose visible text is ordinary prose (a citation is never
# anything else in this codebase's output) or a bare superscript character
# sitting in prose that isn't link text at all -- e.g. "10²⁵ FLOPs" has a
# superscript character but no enclosing `[...](...)`, so it can never match
# this pattern regardless of what surrounds it.
_CITATION_LINK_RE = re.compile(r"\[([⁰¹²³⁴⁵⁶⁷⁸⁹]+)\](\([^)]*\))")

# Identifies the TL;DR paragraph: a line starting with the literal `**TL;DR`
# marker (mirrors digest/publish.py's `_TLDR_PREFIX`/`extract_tldr`, which
# cannot be imported here without introducing an import cycle -- publish.py
# already imports `_real_heading_lines` from this module), captured through
# to the first blank line, or the end of the string (`\Z`) for a briefing
# that is nothing but the TL;DR. `re.MULTILINE` makes `^` match the start of
# any line, not just the start of the string, since the TL;DR paragraph is
# not always literally the first paragraph (a currently-disabled "## Needs
# attention" section can precede it -- see prompts/digest.md's own comment
# on that section). `re.DOTALL` makes `.` match newlines too, so a TL;DR
# paragraph that wraps across multiple physical lines is captured whole; the
# non-greedy `.*?` still stops at the FIRST blank-line boundary rather than
# swallowing the rest of the document.
_TLDR_PARAGRAPH_RE = re.compile(r"^\*\*TL;DR.*?(?=\n[ \t]*\n|\Z)", re.MULTILINE | re.DOTALL)

# Collapses a run of two-or-more horizontal-whitespace characters left behind
# by a deleted citation into a single space -- e.g. two adjacent citations
# each deleted leaves a doubled gap. `[ \t]` only, never `\s`: a citation's
# own removal can never span a newline, and this pass must never touch one
# either -- paragraph structure is not this function's business.
_TLDR_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")

# Removes a single space that now sits directly before a closing-punctuation
# character, left behind when a SPACE-separated citation (`5.21% [¹](u).`)
# is deleted -- an ATTACHED citation (`5.21%[¹](u).`) never had this space
# to begin with, so this is a no-op for that form. `[ \t]` only, for the
# same reason as _TLDR_MULTI_SPACE_RE.
_TLDR_SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"[ \t]+([.,;:)])")


def strip_tldr_citations(markdown_text: str) -> str:
    """Delete every citation link inside the TL;DR paragraph, leaving the body untouched.

    Why this is CODE, not a prompt instruction: PLAN.md §5's documented
    lesson (see the file's ⚠ banner) is that a required output PROPERTY must
    never depend on model compliance -- prompts/digest.md already instructs
    the model at length about citation format and numbering, and the model
    still drifts on formatting details run to run (see, e.g., summarize()'s
    own soft TL;DR-opener check, or the banner saga in run_claude's
    docstring). "The TL;DR paragraph carries zero citation links" is exactly
    this kind of hard guarantee -- it must hold on every digest regardless of
    what the model actually wrote -- so it is enforced here, deterministically,
    after the model has already run. The companion prompt edit
    (prompts/digest.md, prompts/daily.md) exists only to stop the model from
    wasting citation numbers on a paragraph they will be deleted from anyway;
    it is never relied on for the guarantee itself.

    Operates ONLY on the TL;DR paragraph (`_TLDR_PARAGRAPH_RE`: the paragraph
    whose first line starts with the literal `**TL;DR` marker, ending at the
    first blank line or the end of the string) -- every other paragraph,
    including every `## ` body section's own citations, is left
    byte-for-byte untouched. Within that paragraph, every citation link
    matching `_CITATION_LINK_RE` -- a link whose ENTIRE visible text is one
    or more superscript digits and nothing else -- is deleted outright. This
    is the same discriminator digest/emailer.py's `_style_citation_anchors`
    uses to recognize a real citation (see `_CITATION_LINK_RE`'s own comment
    for the full reasoning): matching on shape, not position, is what keeps
    this from ever touching a prose-text link or a bare superscript
    character sitting in prose that is not link text at all.

    Cleans up the two whitespace shapes a deletion can leave behind: an
    ATTACHED citation (`5.21%[¹](u).`) leaves no gap at all, while a
    SPACE-separated one (`5.21% [¹](u).`) leaves a run that would otherwise
    read as "5.21% ." -- a stray space before the period. After every
    citation in the paragraph is deleted, a run of two-or-more horizontal-
    whitespace characters collapses to one (`_TLDR_MULTI_SPACE_RE`), and a
    single space directly before `.`/`,`/`;`/`:`/`)` is removed
    (`_TLDR_SPACE_BEFORE_PUNCTUATION_RE`). Both passes match `[ \t]` only,
    never `\\s` -- newlines are never touched.

    Returns `markdown_text` completely unchanged when there is no TL;DR
    paragraph (`_TLDR_PARAGRAPH_RE` finds no match -- e.g. a digest whose
    model output omitted the TL;DR opener, which summarize()'s own soft
    check already tolerates and ships anyway) or when the TL;DR paragraph
    has no citations to strip in the first place.
    """

    def _clean(match: re.Match[str]) -> str:
        paragraph = match.group(0)
        cleaned = _CITATION_LINK_RE.sub("", paragraph)
        if cleaned == paragraph:
            return paragraph
        cleaned = _TLDR_MULTI_SPACE_RE.sub(" ", cleaned)
        cleaned = _TLDR_SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", cleaned)
        return cleaned

    return _TLDR_PARAGRAPH_RE.sub(_clean, markdown_text, count=1)


# Only the digit-to-superscript direction is ever exercised by
# renumber_citations (it counts matches by POSITION, never by reading a
# citation's existing number back out of its superscript form -- see that
# function's docstring), but the mapping is the same digit<->superscript
# character correspondence used throughout this codebase's citation handling
# (`0123456789` <-> `⁰¹²³⁴⁵⁶⁷⁸⁹`, matching _CITATION_LINK_RE's character
# class), spelled out once here rather than re-derived per call.
_DIGIT_TO_SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def renumber_citations(markdown_text: str) -> str:
    """Renumber every remaining citation link sequentially from 1, in order of appearance.

    Meant to run immediately after `strip_tldr_citations`: once the TL;DR's
    own citations are gone, the body's surviving numbering starts mid-
    sequence (e.g. at ⁵, if four citations were removed from the TL;DR) --
    this closes that gap by rewriting every surviving citation's visible
    number, first-to-last through the whole document, to ¹, ², ³, ... ,
    regardless of what number it originally carried.

    Only rewrites the link TEXT (the bracketed superscript digits), never
    the URL: the captured `(...)` group from `_CITATION_LINK_RE` is
    re-emitted verbatim, untouched. This is why summarize() and
    summarize_daily() call this AFTER enforce_link_allowlist: renumbering
    changes nothing about which URLs appear in the output, so it cannot
    reintroduce a non-allowlisted link or otherwise affect that pass's
    provenance guarantee -- provenance is a property of the URL, and the URL
    never changes here.

    Matches the same `_CITATION_LINK_RE` shape `strip_tldr_citations` uses --
    a link whose entire visible text is superscript digits and nothing else
    -- so a bare superscript sitting in ordinary prose (not link text at
    all, e.g. "10²⁵ FLOPs") is never touched, for the identical reason given
    in that function's docstring.

    `str(n).translate(_DIGIT_TO_SUPERSCRIPT)` naturally produces the right
    multi-digit sequence for n >= 10 (e.g. "10" -> "¹⁰" via per-character
    translation) with no special-casing needed for the 10+ case -- this
    function never needs to parse an EXISTING (possibly multi-digit)
    superscript back into an int, since the new number comes from a plain
    position counter, not from reading the old one.
    """
    counter = itertools.count(1)

    def _renumber(match: re.Match[str]) -> str:
        n = next(counter)
        superscript = str(n).translate(_DIGIT_TO_SUPERSCRIPT)
        return f"[{superscript}]{match.group(2)}"

    return _CITATION_LINK_RE.sub(_renumber, markdown_text)


# Matches the machine-facing ```arcs fenced block (stable-arc-keys feature):
# prompts/digest.md's "Story-arc keys" section instructs the model to append,
# immediately before the ```deltas block described below (both at the very
# end of the response), one fenced code block tagged `arcs` containing a
# JSON array of {"heading", "key"} objects -- one entry per `## ` story
# section. Same shape as _DELTAS_FENCE_RE just below, for the same reasons
# (DOTALL so `.` spans embedded newlines, optional-newline-prefixed closer,
# not anchored to the end of the string since extract_arc_keys tolerates the
# fence appearing anywhere in the document even though the prompt instructs
# the model to place it last).
_ARCS_FENCE_RE = re.compile(r"```arcs\s*\n(.*?)\n?```", re.DOTALL)

# Caps how many arc-key entries extract_arc_keys keeps from one ```arcs
# block, mirroring _MAX_DELTAS/digest/publish.py's _MAX_TOPICS parity
# constant for the identical reason: a digest has at most _MAX_TOPICS=12
# topics in the first place, so no legitimate response can ever produce more
# than 12 genuine arc-key entries either.
_MAX_ARC_KEYS = 12


def extract_arc_keys(body_md: str) -> tuple[str, list[dict[str, str]]]:
    """Extract and strip the model's machine-facing ```arcs fence, returning (body, entries).

    Mirrors `extract_deltas` (just below) EXACTLY -- same fence discovery
    (anywhere in the document, not only at the end), same "more than one
    fence is malformed" handling, same per-entry defensive validation, same
    post-strip whitespace cleanup, same never-raises contract. See that
    function's own docstring for the full defensive-parsing rationale; it
    applies here verbatim, just for the ```arcs fence (prompts/digest.md's
    "Story-arc keys" section) instead of ```deltas.

    Called from `summarize()` at the SAME choke point as `extract_deltas`
    (immediately after `run_claude` returns, before every other pass), so no
    downstream consumer of `body_md` -- storage, translation, email/site/
    Telegram rendering, or the daily/weekly prompt builders that embed a
    stored window digest's body_md verbatim -- ever sees the raw fence.

    Return value: `(body_md_with_fence_removed, parsed_entries)`, entries
    shaped `{"heading": ..., "key": ...}`.

    - No fence at all: `(body_md, [])` unchanged -- the normal case for a
      window with no real `## ` story sections to tag.
    - More than one fence: malformed (the model was only ever asked for
      one) -- every fence is stripped, one warning logged, all entries
      discarded.
    - The fence's content fails `json.loads` or is not a JSON array: one
      warning, fence stripped, `[]` entries.
    - More than `_MAX_ARC_KEYS` (12) entries: only the first 12, in document
      order, are considered.
    - A malformed entry -- not an object; missing, non-string, or blank
      "heading"; missing or non-string "key"; or a "key" that fails
      `_ARC_KEY_RE` (not lowercase ASCII letters/digits/hyphens, over 48
      characters, or not starting with a letter/digit) -- is dropped
      individually; one batched warning names the count, every well-formed
      entry among the rest still survives. An invalid key is treated as a
      malformed entry, not as a separate error class: there is no principled
      way to "repair" a key the model got wrong, and a dropped entry simply
      means that story's `## ` section still ships (derive_topics never
      depends on this data), it just isn't tagged with a stable key this
      run.

    Every surviving entry's "heading" and "key" are `.strip()`ped. Heading
    matching against this digest's own topic slugs (dropping an entry whose
    heading doesn't correspond to a real `## ` section) is NOT this
    function's job -- see digest/publish.py's `derive_topics`, which
    consumes this function's `entries` output together with the same
    (already-stripped) `body_md` to do that matching, one layer up, mirroring
    `map_deltas_to_slugs`' identical division of labor for deltas.
    """
    matches = list(_ARCS_FENCE_RE.finditer(body_md))
    if not matches:
        return body_md, []

    stripped_body = _ARCS_FENCE_RE.sub("", body_md)
    stripped_body = re.sub(r"\n{3,}", "\n\n", stripped_body).rstrip()

    if len(matches) > 1:
        logger.warning(
            "extract_arc_keys: found %d ```arcs fences (expected at most 1) -- "
            "treating as malformed, stripping all and discarding every parsed entry",
            len(matches),
        )
        return stripped_body, []

    try:
        parsed = json.loads(matches[0].group(1))
    except (json.JSONDecodeError, ValueError):
        logger.warning("extract_arc_keys: ```arcs fence content is not valid JSON -- discarding")
        return stripped_body, []

    if not isinstance(parsed, list):
        logger.warning("extract_arc_keys: ```arcs fence content is not a JSON array -- discarding")
        return stripped_body, []

    candidates = parsed[:_MAX_ARC_KEYS]
    entries: list[dict[str, str]] = []
    dropped = 0
    for candidate in candidates:
        if (
            isinstance(candidate, dict)
            and isinstance(candidate.get("heading"), str)
            and candidate["heading"].strip()
            and isinstance(candidate.get("key"), str)
            and _ARC_KEY_RE.match(candidate["key"].strip())
        ):
            entries.append(
                {
                    "heading": candidate["heading"].strip(),
                    "key": candidate["key"].strip(),
                }
            )
        else:
            dropped += 1

    if dropped:
        logger.warning(
            "extract_arc_keys: dropped %d malformed entr%s out of %d considered",
            dropped,
            "y" if dropped == 1 else "ies",
            len(candidates),
        )

    return stripped_body, entries


# Matches the machine-facing ```deltas fenced block (PLAN.md §11.3):
# prompts/digest.md instructs the model to append, at the very end of its
# response, one fenced code block tagged `deltas` containing a JSON array of
# {"heading", "previously", "now"} objects -- one entry per story that is a
# DELTA-ONLY update to a RECENT_COVERAGE entry. DOTALL so `.` spans the
# newlines inside the fenced content; the trailing `\n?` makes the closing
# ``` optional-newline-prefixed so a fence with no blank line before the
# closer still matches. Deliberately NOT anchored to the end of the string
# (`\Z`) -- extract_deltas's own contract tolerates the fence appearing
# anywhere in the document (see its docstring's "fence not at end" case),
# even though the prompt instructs the model to always put it last.
_DELTAS_FENCE_RE = re.compile(r"```deltas\s*\n(.*?)\n?```", re.DOTALL)

# Caps how many delta entries extract_deltas keeps from one ```deltas block,
# mirroring digest/publish.py's MAX_TOPICS parity constant (_MAX_TOPICS,
# itself matching the site's own MAX_TOPICS ingest cap): a digest has at most
# _MAX_TOPICS=12 topics in the first place (derive_topics), so no legitimate
# response can ever produce more than 12 genuine delta entries either -- a
# ```deltas array longer than this is either a pathological/hostile model
# output or a bug, and capping here bounds this parser's own output exactly
# like every other size-bounding constant in this module.
_MAX_DELTAS = 12


def extract_deltas(body_md: str) -> tuple[str, list[dict[str, str]]]:
    """Extract and strip the model's machine-facing ```deltas fence, returning (body, entries).

    PLAN.md §11.3: for each story that is a DELTA-ONLY update to a
    RECENT_COVERAGE entry, prompts/digest.md instructs the model to append
    one fenced ```deltas code block, as the LAST thing in its response,
    containing a JSON array of `{"heading": ..., "previously": ...,
    "now": ...}` objects. This is the pure parser for that block -- called
    from `summarize()` immediately after `run_claude` returns, BEFORE any
    other pass runs (validate_output, the TL;DR/citation soft checks,
    enforce_link_allowlist, strip_tldr_citations/renumber_citations) -- so
    every one of those passes, and every downstream consumer of the returned
    body (digests.body_md storage, the Hungarian translation input, email/
    site/Telegram rendering, and critically the daily/weekly prompt builders
    which embed a WINDOW digest's stored body_md verbatim -- see PLAN.md
    §11.3's fencing guardrail), sees a body with the raw ```deltas fence
    already gone, by construction, rather than by a second fence someone has
    to remember to add at each downstream layer.

    Never raises: like this module's other output-parsing helpers
    (validate_output aside, which raises only on the one hard structural
    contract), a malformed machine-facing block must never kill a digest run
    that otherwise shipped a perfectly good prose briefing -- see this
    function's callers' own "never let a required output property depend on
    model compliance" lesson. Every failure mode below degrades to "keep the
    body, drop the deltas, log ONE loud warning" rather than raising.

    Return value: `(body_md_with_fence_removed, parsed_entries)`.

    - No fence at all: returns `(body_md, [])` unchanged -- the normal case
      for a digest with no repeat-story deltas this window (the prompt
      contract says to omit the block entirely when nothing qualifies).
    - Exactly one fence, found ANYWHERE in the document (not only at the very
      end -- the prompt instructs the model to place it last, but this parser
      doesn't trust that placement blindly): the fence is stripped from
      wherever it sits, and its JSON content is parsed.
    - MORE than one fence: treated as malformed. The model was only ever
      asked for one block, so a second one signals either a confused
      response or an injection attempt riding along in the untrusted source
      material this prompt summarizes -- there is no principled way to know
      which fence (if either) is the "real" one. Both (all) fences are
      stripped from the body regardless (a raw, unparsed ```deltas block must
      never reach the reader on any channel), one warning is logged, and
      every entry is discarded -- the prose briefing itself is unaffected and
      still ships.
    - The single fence's content fails `json.loads` (not valid JSON) or
      parses to something other than a JSON array: one warning, fence
      stripped, `[]` entries.
    - The array parses, but some entries are malformed (not an object, or
      missing/non-string/blank `heading`/`previously`/`now`): only THOSE
      entries are dropped (one warning naming the count), every well-formed
      entry among the rest still survives.
    - More than `_MAX_DELTAS` (12) array entries: only the first 12, in
      document order, are ever considered (mirroring `derive_topics`' own
      `_MAX_TOPICS` cap and its "prompt orders most-important-first" logic) --
      entries beyond the cap are silently dropped, never counted toward the
      "malformed" warning (being over the cap is not malformed).

    Every surviving entry's three string fields are `.strip()`ped. Heading
    matching against this digest's own topic slugs (dropping a delta whose
    heading doesn't correspond to a real `## ` section) is NOT this
    function's job -- see digest/publish.py's `map_deltas_to_slugs`, which
    consumes this function's `entries` output together with the same
    (already-stripped) `body_md` to do that matching, one layer up.

    Whitespace left behind by removing the fence (a run of 3+ newlines, most
    commonly two blank lines where the fence used to sit between the closing
    prose and the document's end) is collapsed to a single blank line
    (`\\n\\n`) and trailing whitespace is trimmed -- so the returned body
    reads as a normal, cleanly-terminated markdown document whether the fence
    sat at the very end (the common case) or was excised from the middle.
    """
    matches = list(_DELTAS_FENCE_RE.finditer(body_md))
    if not matches:
        return body_md, []

    stripped_body = _DELTAS_FENCE_RE.sub("", body_md)
    stripped_body = re.sub(r"\n{3,}", "\n\n", stripped_body).rstrip()

    if len(matches) > 1:
        logger.warning(
            "extract_deltas: found %d ```deltas fences (expected at most 1) -- "
            "treating as malformed, stripping all and discarding every parsed entry",
            len(matches),
        )
        return stripped_body, []

    try:
        parsed = json.loads(matches[0].group(1))
    except (json.JSONDecodeError, ValueError):
        logger.warning("extract_deltas: ```deltas fence content is not valid JSON -- discarding")
        return stripped_body, []

    if not isinstance(parsed, list):
        logger.warning("extract_deltas: ```deltas fence content is not a JSON array -- discarding")
        return stripped_body, []

    candidates = parsed[:_MAX_DELTAS]
    entries: list[dict[str, str]] = []
    dropped = 0
    for candidate in candidates:
        if (
            isinstance(candidate, dict)
            and isinstance(candidate.get("heading"), str)
            and candidate["heading"].strip()
            and isinstance(candidate.get("previously"), str)
            and candidate["previously"].strip()
            and isinstance(candidate.get("now"), str)
            and candidate["now"].strip()
        ):
            entries.append(
                {
                    "heading": candidate["heading"].strip(),
                    "previously": candidate["previously"].strip(),
                    "now": candidate["now"].strip(),
                }
            )
        else:
            dropped += 1

    if dropped:
        logger.warning(
            "extract_deltas: dropped %d malformed delta entr%s out of %d considered",
            dropped,
            "y" if dropped == 1 else "ies",
            len(candidates),
        )

    return stripped_body, entries


def summarize(
    items: list[Item],
    failed_sources: list[str],
    recent_coverage: str,
    model: str,
    timeout_seconds: int,
    effort: str,
    recent_arcs: str = "",
) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    """Build the prompt, run it through Claude, validate and repair the
    contract, and deterministically prepend the collector-failure banner.

    Returns `(body_md, deltas, arc_keys)`, NOT a bare string -- PLAN.md §11.3
    added a second return value, `deltas`, the `extract_deltas`-parsed list
    of `{"heading", "previously", "now"}` entries pulled off the model's
    machine-facing ```deltas fence (see that function's own docstring); the
    stable-arc-keys feature adds a THIRD, `arc_keys`, the
    `extract_arc_keys`-parsed list of `{"heading", "key"}` entries pulled off
    the model's ```arcs fence (see that function's own docstring). Both
    parses happen HERE, immediately after `run_claude` returns and before
    every other pass in this function, specifically so `body_md` -- the
    first tuple element, and the only thing every downstream consumer ever
    sees (digests.body_md storage, translate_digest's input, email/site/
    Telegram rendering, and the daily/weekly prompt builders, which embed a
    stored WINDOW digest's body_md verbatim) -- NEVER carries either raw
    fence. This is the single choke point both the §11.3 deltas fencing
    guardrail and the identical one for arc keys rely on: strip once, here,
    before anything downstream can see either fence, rather than trusting
    every future consumer to re-implement the same fence exclusion
    independently. `deltas`/`arc_keys` are returned separately so the caller
    (digest/main.py's `_deliver`) can map each entry's heading to this
    digest's own topic slug (digest/publish.py's `map_deltas_to_slugs` for
    deltas, `derive_topics(body_md, arc_keys)` for arc keys) and persist the
    result (digest/state.py's `write_deltas`/`write_arc_keys`) --
    summarize() itself does no slug mapping or persistence for either; it
    only parses and strips.

    `recent_coverage` is passed straight through to build_prompt (see that
    function's docstring for the substitution-ordering hazard it addresses,
    and format_recent_coverage's own docstring, above, for how this string
    is produced and why it must already be sanitized by the time it reaches
    here). `recent_arcs` (default "") is threaded through identically, for
    the {{RECENT_ARCS}} block (see format_recent_arcs and build_prompt's own
    docstring) -- defaults to "" so a caller not exercising this feature
    doesn't have to pass it; digest/main.py's real caller always threads
    through the actual rendered block.

    `effort` is threaded straight through to run_claude's `--effort` flag
    (see that function's docstring for why it's set explicitly and why
    `high`, Config.claude_effort's default, rather than `max`).

    Never call with an empty item list. Raises SummarizeError (via
    run_claude or validate_output) rather than returning malformed output,
    so the caller never persists a digest for content that failed the
    output contract.

    The output is now a free-form prose BRIEFING (prompts/digest.md), not
    the old fixed three-section list. validate_output only enforces the one
    structural property a legitimate briefing can never fail to have (at
    least one real `## ` heading) -- everything else about quality is a
    SOFT check here: logged if missed, never raised, because the model's
    exact wording legitimately varies run to run and hard-gating wording
    against a varying model is what caused the old three-heading contract's
    retry loops. Two soft checks run on the raw model output before link
    repair: a missing `**TL;DR:` opener (degrades one email cosmetically),
    and zero citation links (a window of pure chatter can legitimately cite
    nothing, so this must never raise).

    enforce_link_allowlist runs after validate_output and before the banner:
    every link in the model's output is checked against the URLs of the
    items it was actually given, and any link that doesn't match one
    verbatim is deterministically stripped down to plain text rather than
    failing the whole run (see that function's docstring for why repair,
    not rejection, is the right response here).

    `strip_tldr_citations` then `renumber_citations` run last, after link
    repair: the owner's requirement is that the TL;DR paragraph read as
    clean prose with NO citation links anywhere, on every channel (site,
    email, archive, Hungarian translation) -- so this is enforced here in
    code rather than left to the prompt, per the same "never let a required
    output property depend on model compliance" lesson the banner above
    already follows. Every downstream consumer (create_digest, publish/
    archive, translate_digest) reads this already-cleaned `body_md`, so the
    guarantee propagates to all of them for free.

    The `⚠ <source> collection failed this run` banner is generated here,
    in code, rather than asked of the model: a live test against real Opus
    showed the model omits the banner even when the prompt explicitly and
    emphatically instructs it to write one. Whether a partial-collection
    run is flagged to the reader is deterministic system state -- it must
    never depend on model compliance. One banner line is emitted per
    failed source, in the given order, followed by a blank line, then the
    (validated) model output unchanged.
    """
    prompt = build_prompt(items, failed_sources, recent_coverage, recent_arcs)
    output = run_claude(prompt, model, timeout_seconds, effort)
    # extract_arc_keys and extract_deltas both run FIRST, before
    # validate_output or any other pass: see this function's own docstring
    # for why this exact position is the single choke point both the
    # arc-keys and the §11.3 deltas fencing guardrails depend on. arc_keys
    # runs first, mirroring the ```arcs-then-```deltas order the prompt
    # itself asks the model to emit -- order between the two extractions
    # doesn't affect correctness (each matches its own fence tag), but
    # keeping it makes the code read in the same order as the contract it
    # implements. Running both here also means validate_output, the
    # TL;DR/citation soft checks, and enforce_link_allowlist below all
    # operate on the fence-free `output`, so nothing in the
    # (untrusted-derived) fence content can be misread as a citation link or
    # a TL;DR paragraph by those passes.
    output, arc_keys = extract_arc_keys(output)
    output, deltas = extract_deltas(output)
    validate_output(output)
    # The TL;DR opener is checked SOFTLY, unlike the heading requirement: a
    # missing TL;DR degrades one email cosmetically, while raising here
    # would hold every collected item hostage for a full scheduling cycle
    # over a nicety (the banner saga proved hard-gating model compliance
    # loops when the model persistently misbehaves). The one structural
    # failure (no real heading at all) stays hard; quality misses log and
    # ship.
    if not output.lstrip().startswith("**TL;DR:"):
        logger.warning("digest output missing the TL;DR opener — sending anyway")
    # Same soft-check reasoning for citations: a window that was pure
    # chatter can legitimately produce zero `[text](url)` links (nothing met
    # the citation bar), and that is correct output, not a bug -- so this
    # only logs, checked on the raw model output before enforce_link_allowlist
    # potentially strips any non-allowlisted link down to plain text below.
    if not _MARKDOWN_LINK_RE.search(output):
        logger.warning("digest output contains no citation links — sending anyway")
    output = enforce_link_allowlist(output, allowed_urls={item.url for item in items})
    # strip_tldr_citations/renumber_citations run last, after link-allowlist
    # repair: the TL;DR paragraph must never carry citation links (owner
    # requirement -- clean prose only), enforced deterministically here
    # rather than left to prompt compliance, per this module's own PLAN.md
    # §5 lesson. Renumbering only rewrites link TEXT, never URLs, so running
    # it after enforce_link_allowlist cannot reintroduce a non-allowlisted
    # link or otherwise affect that pass's provenance guarantee -- see
    # renumber_citations' own docstring.
    output = renumber_citations(strip_tldr_citations(output))
    if failed_sources:
        banner = "".join(
            f"⚠ {source} collection failed this run\n" for source in failed_sources
        )
        return banner + "\n" + output, deltas, arc_keys
    return output, deltas, arc_keys
