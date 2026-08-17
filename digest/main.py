"""Entrypoint: python -m digest.main — orchestrates one collection run.

Phase 2: Telegram collection + state persistence, then summarization
(§4.4) and email delivery (§4.5) of PLAN.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from digest.collectors import hackernews as hackernews_collector
from digest.collectors import polymarket as polymarket_collector
from digest.collectors import reddit as reddit_collector
from digest.collectors import rss as rss_collector
from digest.collectors import telegram as telegram_collector
from digest.collectors import x as x_collector
from digest.collectors.base import CollectResult
from digest.collectors.polymarket import PolymarketCollectResult
from digest.config import Config, ConfigError
from digest.context import generate_arc_context
from digest.daily import summarize_daily
from digest.deliver import TelegramRunState, deliver_channels, deliver_pending, digest_meta
from digest.emailer import archive
from digest.publish import derive_topics, map_deltas_to_slugs
from digest.state import (
    _ITEMS_PRUNE_DAYS,
    DAILY_LOOKBACK_WINDOW,
    WEEKLY_LOOKBACK_WINDOW,
    commit_new_items,
    connect,
    count_unsummarized_items,
    create_digest,
    get_arc_keys_needing_context,
    get_cursors,
    get_daily_digests_since,
    get_digest_item_urls,
    get_latest_arc_occurrence,
    get_polymarket_probs,
    get_recent_arc_keys,
    get_recent_digests,
    get_unsummarized_items,
    get_window_digests_since,
    init_db,
    prune_delivered_items,
    prune_stale_unsummarized,
    write_arc_context,
    write_arc_keys,
    write_deltas,
)
from digest.summarize import (
    _MAX_PROMPT_BYTES,
    SummarizeError,
    format_recent_arcs,
    format_recent_coverage,
    select_balanced_items_for_prompt,
    summarize,
)
from digest.translate import translate_digest
from digest.verify import VerificationUnavailable, verify_daily
from digest.weekly import summarize_weekly

# How far back _deliver looks for prior digests when building the
# {{RECENT_COVERAGE}} prompt block (digest/summarize.py's
# format_recent_coverage) -- the "running story memory" that lets the
# summarizer write delta-only updates for stories it already covered instead
# of re-explaining them every 6 hours. 24 hours is a full day's worth of
# briefings (4 runs at the 6-hourly cadence) -- long enough that a story
# spanning a slow news day is still recognized as "already covered" on its
# second or third mention, short enough that genuinely stale coverage
# eventually ages out and stops suppressing a fresh full write-up.
_RECENT_COVERAGE_WINDOW = timedelta(hours=24)

# How far back _deliver looks for prior digests' story-arc keys when
# building the {{RECENT_ARCS}} prompt block (digest/summarize.py's
# format_recent_arcs) -- the "story continuity" list that lets the
# summarizer reuse a key for a story that's still developing instead of
# minting a new one every run (see prompts/digest.md's "Story-arc keys"
# section). 7 days, not 24 hours like _RECENT_COVERAGE_WINDOW just above:
# RECENT_COVERAGE exists to avoid re-explaining a story from scratch within
# roughly one day, but a story's KEY needs to stay recognizable across a much
# longer arc -- the live incident this feature fixes (Iran/Hormuz recurring
# 11 times under 11 different slugs over 7 days, PLAN.md) spans a week, not
# a day, so the reuse window has to match that timescale or the feature
# would only ever catch same-day repeats, missing the exact multi-day arcs
# it exists to unify.
_RECENT_ARCS_WINDOW = timedelta(days=7)

# How far back run_daily looks for window digests to synthesize into one
# daily brief (digest/state.py's get_window_digests_since). A full 24 hours
# so a brief run at 20:00 covers exactly "since yesterday's brief", with no
# gap or overlap at the boundary -- the scheduling itself (the second
# systemd timer that invokes `python -m digest daily`) lives outside this
# repo, so this window is what actually defines "one day" from this code's
# point of view.
#
# This constant lives in digest/state.py as `DAILY_LOOKBACK_WINDOW` (imported
# above), not here -- digest/deliver.py's deliver_channels needs the exact
# same window (via state.py's get_daily_allowed_urls) to re-derive a daily
# brief's link-provenance allowlist at delivery time, and the two uses must
# never drift apart (see that constant's own docstring for why).

# How far back run_weekly looks for daily briefs to synthesize into one
# weekly report (digest/state.py's get_daily_digests_since), one editorial
# rung up from DAILY_LOOKBACK_WINDOW immediately above. A full 7 days so a
# report run Sunday evening covers exactly "since last Sunday's report",
# with no gap or overlap at the boundary -- the scheduling itself (a third
# systemd timer that invokes `python -m digest weekly`) lives outside this
# repo, so this window is what actually defines "one week" from this code's
# point of view.
#
# Also the SAME window run_weekly reuses for its own `get_window_digests_since`
# call, to derive the transitive citation allowlist (see run_weekly's own
# docstring for why a weekly brief's allowlist has to go two hops down, past
# the daily layer, to the window digests underneath it).
#
# This constant lives in digest/state.py as `WEEKLY_LOOKBACK_WINDOW` (imported
# above), not here -- for the identical reason DAILY_LOOKBACK_WINDOW does:
# digest/deliver.py's deliver_channels needs the exact same window (via
# state.py's get_weekly_allowed_urls) to re-derive a weekly brief's
# link-provenance allowlist at delivery time, and the two uses must never
# drift apart (see that constant's own docstring for why).

# Minimum spacing `run_daily` requires between "now" and the most recent
# `kind='daily'` digest's `created_at` before it will produce a NEW one --
# below this spacing, a `run_daily` invocation is treated as a duplicate
# fire of the once-a-day timer and skipped as a no-op (see run_daily's own
# docstring, step 2). Exists because the scheduling that decides WHEN
# `python -m digest daily` runs is a systemd timer that lives OUTSIDE this
# repo (see CLAUDE.md's Deploy note) -- this code cannot assume that timer
# only ever fires once a day, so it has to defend against a double-fire
# itself, the same way `_TELEGRAM_MAX_AGE`/GUARD 1 defends the Telegram send
# path against a different failure mode.
#
# 20 hours, not `DAILY_LOOKBACK_WINDOW`'s 24: the incident this guards
# against (2026-08-09, two daily briefs 31 minutes apart -- 20:06 and 20:37
# Europe/Budapest) shows the timer's own run-time jitter is on the order of
# ~30 minutes, so a LEGITIMATE consecutive-day gap between two daily briefs
# is roughly 23.5h (24h minus that jitter) -- comfortably clear of a 20h
# threshold on either side -- while a same-evening double-fire (minutes to a
# few hours apart) is always caught. Any threshold from roughly 21h up to
# just under 23.5h would work equally well; 20h was chosen for a wide, easy
# margin rather than cutting it close.
#
# A pure UTC recency comparison against `digests.created_at` -- via
# `get_daily_digests_since` (digest/state.py), reused rather than a new
# query (see that function's own docstring: it already returns `kind =
# 'daily'` rows at/after a given ISO instant, exactly the shape this guard
# needs) -- NOT a Budapest-calendar-day check: CLAUDE.md requires timestamps
# stay UTC and be converted to Europe/Budapest only at render/email time,
# never in storage or state comparisons, so this guard has no
# daylight-saving or local-midnight boundary edge cases of its own.
_DAILY_DUPLICATE_GUARD_WINDOW = timedelta(hours=20)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# PLAN.md §11.4: the code-prepended banner `run_daily` stamps on the DRAFT
# when the optional verification pass (VERIFY_DAILY_ENABLED) is on but fails
# for any reason (CLI failure, transcript surprise, or output that fails the
# daily contract -- digest/verify.py's `verify_daily` folds all three into
# either `VerificationUnavailable` or `SummarizeError`). Deliberately
# code-generated, never model-written -- mirrors digest/summarize.py's own
# `⚠ <source> collection failed this run` collector-failure banner (see
# `summarize()`'s docstring for why a required output property must never
# depend on model compliance) and reuses the IDENTICAL rendering mechanism
# for free: digest/emailer.py's `_BANNER_PARAGRAPH_RE`/`_wrap_banner_paragraph`
# style ANY leading `⚠`-prefixed paragraph as a warning callout, regardless
# of its exact wording, so this banner gets the same visual treatment with
# no rendering-side change needed. Deliberately WORDED DIFFERENTLY from the
# collector banner (no "collection failed", no per-source name) so
# digest/publish.py's `parse_failed_sources` -- which matches the stricter
# `^⚠ (\S+) collection failed this run$` shape -- correctly does NOT treat
# this as a failed-collector banner; verification unavailability is its own
# distinct condition, not a collector failure.
_VERIFY_UNAVAILABLE_BANNER = "⚠ verification unavailable this run\n\n"

# Bounds how many unsummarized items a single Claude call is given. Each
# allowlisted chat can contribute up to 500 messages per run, and a failed
# summarize call carries the backlog forward plus new items on top -- with
# no cap, the prompt for an accumulated backlog can exceed the model's
# context window, and an oversized prompt then fails every subsequent run
# forever (no items ever get stamped). The remainder ships in later runs:
# the 6-hourly timer is the drain loop for a large backlog, at
# _MAX_ITEMS_PER_DIGEST items per digest. 250 (was 200 at the 3-hourly
# cadence): the 6-hourly windows carry roughly double the items, and this
# value must equal the sum of digest/summarize.py's _SOURCE_QUOTAS -- the
# quotas ARE the allocation of this budget (see that constant's comment).
_MAX_ITEMS_PER_DIGEST = 250

# How many unsummarized items are read back as the CANDIDATE POOL that
# allocate_by_source picks _MAX_ITEMS_PER_DIGEST from. Deliberately much
# larger than the per-digest cap: the pre-quota code passed
# `limit=_MAX_ITEMS_PER_DIGEST` straight to get_unsummarized_items, which
# meant the cap was applied BEFORE any notion of source balance existed --
# and since every collector stamps one fetched_at for its whole batch and
# Telegram runs first, a Telegram backlog burst filled the entire window
# with Telegram alone (live incident: 2026-08-13T07:20Z, a briefing built
# from 200 telegram items and 0 news/x/reddit/polymarket, and the three
# runs after it were nearly as skewed). The quota can only balance sources
# it can actually see, so the pool read must be wide enough to reach past
# one source's backlog to the items behind it.
_MAX_ITEMS_FETCH_POOL = 1000


async def _client_ready(client: TelegramClient) -> bool:
    """Connect without triggering Telethon's interactive login prompt.

    `async with client` calls `start()`, which prompts for phone/code on an
    invalid session -- fatal in a headless one-shot container. This instead
    connects and checks authorization explicitly, so an invalid session is
    reported as "not ready" rather than blocking on stdin or raising before
    `collect`'s error handling is in scope.
    """
    try:
        await client.connect()
        return await client.is_user_authorized()
    except Exception as exc:
        logger.warning("telegram connect failed: %s", type(exc).__name__)
        return False


def _deliver(
    conn: sqlite3.Connection, cfg: Config, failed_sources: list[str]
) -> bool:
    """Post-collection delivery: retry every pending digest, then summarize+deliver new items.

    (a) Every digest with at least one ENABLED channel still undelivered
        (`deliver_pending`, oldest first -- crash/API failure on a
        previous run) has channel delivery retried for it. Unlike the
        pre-multi-channel version of this function, a failure here does NOT
        short-circuit the rest of this function: channels are independent
        by design (a broken SMTP path must not stop a healthy site/Telegram
        channel from delivering a DIFFERENT pending digest, or this run's
        own freshly summarized one), so every pending digest is attempted
        and the overall result is the AND of every attempt.
    (b) No unsummarized items -- nothing left to send, this is a normal
        empty-window run (or the pending pass already covered everything).
    (c) Otherwise: summarize with the CURRENT run's failed_sources, attempt
        the optional Hungarian translation (digest/translate.py's
        translate_digest, soft-failing -- see its own docstring), durably
        record the digest (English body plus whatever translation resulted,
        possibly None) and archive it (BEFORE attempting any channel -- see
        below), then attempt all its channels.

    Archiving: `archive()` is called exactly once, immediately after
    `create_digest` durably records a NEW digest -- never on the
    pending-resend path (that digest was already archived when it was first
    created, in a previous run) and never gated on any channel's success or
    failure. This is a deliberate decoupling from the old single-channel
    behavior, where archiving only happened after a successful email send:
    with EMAIL_ENABLED=false a digest could otherwise never be archived at
    all, even though the site/Telegram channels delivered it just fine.

    `failed_sources` is a list of collector names (e.g. `["telegram"]`,
    `["x"]`, or `["telegram", "x"]`) built by the caller from each
    collector's own CollectResult.failed -- not a single bool -- so a
    Telegram-only failure and an X-only failure produce distinct banner
    text (PLAN.md §5) instead of collapsing to one undifferentiated flag.

    Returns True iff every enabled channel of every digest handled this run
    (pending and freshly created alike) succeeded. Deliberately does NOT
    factor in whether `failed_sources` is non-empty -- the caller combines
    this with the collectors' own failure flags, because ANY collector
    failure must surface as a non-zero exit (the sole signal for the Loki
    alert on digest.service) even on a run that delivers nothing at all.

    `telegram_state` (a single `TelegramRunState`, see its docstring) is
    created once here and threaded through both `deliver_pending` and the
    freshly-summarized digest's own `deliver_channels` call further down --
    so GUARD 2's circuit breaker (digest/deliver.py's per-run 429 handling)
    sees every Telegram send this run makes as one shared sequence, not a
    fresh breaker per digest.
    """
    telegram_state = TelegramRunState()
    all_ok = deliver_pending(conn, cfg, telegram_state)

    items = get_unsummarized_items(conn, limit=_MAX_ITEMS_FETCH_POOL)
    if not items:
        logger.info("no unsummarized items, nothing to send")
        return all_ok


    # "Recently covered" continuity context (digest/summarize.py's
    # format_recent_coverage): every digest created in the last
    # _RECENT_COVERAGE_WINDOW, INCLUDING an unsent one still awaiting the
    # pending pass above -- see get_recent_digests' docstring for why
    # email_sent is deliberately not part of the filter. `now`/`since` are
    # both UTC, per CLAUDE.md's storage-stays-UTC convention (created_at is
    # stored as `datetime.now(UTC).isoformat()`, see state.py's create_digest
    # -- comparing against a UTC `since` keeps that comparison meaningful).
    now = datetime.now(UTC)
    since = now - _RECENT_COVERAGE_WINDOW
    recent_coverage = format_recent_coverage(get_recent_digests(conn, since.isoformat()), now)

    # Story-arc-key continuity context (digest/summarize.py's
    # format_recent_arcs): the distinct arc keys used by window digests in
    # the last _RECENT_ARCS_WINDOW (7 days), so the model can reuse a key for
    # a story that's still developing instead of minting a new one every
    # run. Unlike `recent_coverage` above, this is NOT gated on
    # `email_sent`/pending status either -- `get_recent_arc_keys` reads off
    # `arc_keys` rows written by ANY window digest in the lookback window,
    # sent or not, matching `get_recent_digests`' own "email_sent is not a
    # completion signal" reasoning.
    arcs_since = now - _RECENT_ARCS_WINDOW
    recent_arcs = format_recent_arcs(get_recent_arc_keys(conn, arcs_since.isoformat()))

    # One combined selection pass (digest/summarize.py's
    # select_balanced_items_for_prompt): the candidate pool is deliberately
    # wider than one digest's budget (_MAX_ITEMS_FETCH_POOL vs
    # _MAX_ITEMS_PER_DIGEST, see both constants' comments); this turns it
    # into a source-balanced batch -- per-lane quotas including the reserved
    # "positions" lane for POSITIONS_TG_CHANNELS -- that ALSO fits the
    # prompt's byte cap (_MAX_PROMPT_BYTES; when the byte cap binds, the
    # allocation budget shrinks so every lane gives up items in quota ratio,
    # never a tail-chop of whichever collectors ran last -- see that
    # function's docstring). The selection has to happen HERE, not inside
    # summarize(), because create_digest stamps whatever list it's given as
    # "handled" -- the summarized set and the stamped set must stay
    # identical by construction. `recent_coverage`/`recent_arcs` are
    # embedded in every built prompt exactly like the items are, so their
    # bytes count toward the cap automatically.
    items = select_balanced_items_for_prompt(
        items,
        _MAX_ITEMS_PER_DIGEST,
        cfg.positions_tg_channels,
        failed_sources,
        recent_coverage,
        _MAX_PROMPT_BYTES,
        recent_arcs=recent_arcs,
    )

    try:
        body_md, deltas, arc_keys = summarize(
            items,
            failed_sources,
            recent_coverage,
            cfg.anthropic_model,
            cfg.claude_timeout_seconds,
            cfg.claude_effort,
            recent_arcs=recent_arcs,
        )
    except SummarizeError as exc:
        logger.error("summarization failed: %s", exc)
        return False

    # Hungarian translation: a soft-failing PRODUCTION step, run AFTER
    # summarize() succeeds and BEFORE create_digest so the translation (or
    # its absence) is captured in the same durable insert as everything
    # else about this digest -- never a delivery channel of its own, and
    # never allowed to block or delay the English digest (see
    # digest/translate.py's translate_digest docstring). `None` on failure
    # or when the flag is off simply means this digest stays English-only
    # forever; translate_digest already logs its own WARNING on failure, so
    # nothing further is logged here.
    body_md_hu: str | None = None
    if cfg.translate_hu_enabled:
        body_md_hu = translate_digest(
            body_md,
            {item.url for item in items},
            cfg.translate_model,
            cfg.translate_timeout_seconds,
            fallback_model=cfg.translate_model_fallback,
        )

    digest_id = create_digest(conn, body_md, items, body_md_hu=body_md_hu)
    # PLAN.md §11.3 fencing guardrail: this is the ONLY call to write_deltas
    # in this codebase -- deltas are a WINDOW-digest-only concept (they
    # reason about {{RECENT_COVERAGE}}, which is itself window-only, see
    # digest/summarize.py's format_recent_coverage), so run_daily/run_weekly
    # (below) never call it and never read the `deltas` table. Mapped
    # against the ENGLISH `body_md`'s own topic slugs (never body_md_hu),
    # matching digest/deliver.py's `_deliver_site`'s identical rule for
    # `topics` -- see map_deltas_to_slugs's docstring for the full heading
    # -> slug matching contract, and write_deltas's own docstring for the
    # idempotency guarantee this write upholds.
    write_deltas(conn, digest_id, map_deltas_to_slugs(body_md, deltas))
    # Stable-arc-keys feature: the SAME window-digest-only fencing discipline
    # as write_deltas immediately above -- run_daily/run_weekly never call
    # derive_topics with an `arc_keys` argument or write_arc_keys at all (see
    # both functions' own GUARDRAIL docstrings). derive_topics(body_md,
    # arc_keys) folds each ```arcs-fence entry's heading into this digest's
    # own topic slugs (the real heading -> key mapping only exists here,
    # this run, before the fence is stripped from storage); write_arc_keys
    # then persists just the (slug, key) pairs that carried a "key" so
    # digest/deliver.py's `_deliver_site` can read them back later, on both
    # the fresh-digest and pending-resend paths (see that function's own
    # docstring).
    write_arc_keys(conn, digest_id, derive_topics(body_md, arc_keys))
    archive(body_md, cfg.archive_dir, digest_id)

    item_count, created_at, body_md_hu, kind = digest_meta(conn, digest_id)
    done = {"email": False, "site": False, "telegram": False}
    ok = deliver_channels(
        conn, cfg, digest_id, body_md, item_count, created_at, done, telegram_state, body_md_hu,
        kind=kind,
    )
    all_ok = all_ok and ok

    # One Opus call per run keeps cost and runtime bounded -- do NOT loop
    # summarize here even if a remainder is left; the 6-hourly timer is the
    # drain loop that picks up the rest on its next invocation.
    remaining = count_unsummarized_items(conn)
    if remaining:
        logger.info("%d unsummarized items remain, will ship in the next digest", remaining)
    return all_ok


async def _run_x_collector(conn: sqlite3.Connection, cfg: Config) -> CollectResult:
    """Build the X client and run one notifications-page collect, if enabled.

    No-ops entirely (returns a fresh, unfailed CollectResult without ever
    importing twikit) when `cfg.x_enabled` is False -- the caller only
    invokes this when `cfg.x_enabled` is True, but the check is repeated
    here so this function is safe to call unconditionally too. Any
    exception raised while BUILDING the client (e.g. a malformed cookies
    file/JSON, or the cookies path not existing) is treated the same as an
    in-collector failure: logged (type name only -- the underlying error
    could embed cookie material) and flagged, never allowed to crash the
    run. This mirrors telegram.py's `_client_ready`-then-collect split,
    where a client that can't even be constructed/authorized is just
    another shape of collector failure.

    `x_collector.collect` is already exception-proof internally (Codex
    review finding A -- see its docstring), but the call is wrapped in a
    catch-all here too as a second line of defense: this mirrors how
    `_run`'s telegram path can never raise past `_collect_one_chat`/
    `_client_ready` either, so a not-yet-anticipated bug in the collector
    still can't take down the whole run (and Telegram's already-collected
    items) before `commit_new_items` gets a chance to persist them.
    """
    if not cfg.x_enabled:
        return CollectResult()

    x_cursors = get_cursors(conn, "x")
    try:
        client = x_collector.build_client(cfg.x_cookies_path, cfg.x_cookies)
    except Exception as exc:
        logger.warning("x client setup failed: %s", type(exc).__name__)
        return CollectResult(failed=True)

    try:
        return await x_collector.collect(client, x_cursors)
    except Exception as exc:
        logger.warning("x collection crashed unexpectedly: %s", type(exc).__name__)
        return CollectResult(failed=True)


def _run_news_collector(cfg: Config) -> CollectResult:
    """Run one RSS/Atom collect pass, if any feeds are configured.

    No-ops entirely (returns a fresh, unfailed CollectResult) when
    `cfg.news_feeds` is empty -- there is no separate NEWS_ENABLED flag
    (see Config.news_feeds' own comment), so an empty tuple IS "disabled".

    Plain `def`, called synchronously from inside this `async def _run` --
    not `await`ed, no executor indirection. Collectors already run
    sequentially, one at a time (telegram, then x, then news); nothing else
    is in flight while this runs, so there is nothing for an
    `asyncio.to_thread`/executor wrapper to protect against blocking, and
    adding one would just be indirection with no payoff.

    `rss_collector.collect` is already exception-proof internally (see its
    module docstring -- one bad feed is caught per-feed), but this call is
    wrapped in a catch-all here too as a second line of defense, mirroring
    `_run_x_collector`'s own rationale: a not-yet-anticipated bug in the
    collector still must not take down the whole run (and Telegram/X's
    already-collected items) before `commit_new_items` gets a chance to
    persist them. Only the exception's type name is logged -- consistent
    with how every other collector-crash log line in this module avoids
    echoing exception text that could embed response content.
    """
    if not cfg.news_feeds:
        return CollectResult()

    try:
        return rss_collector.collect(cfg.news_feeds)
    except Exception as exc:
        logger.warning("news collection crashed unexpectedly: %s", type(exc).__name__)
        return CollectResult(failed=True)


def _run_polymarket_collector(
    conn: sqlite3.Connection, cfg: Config
) -> PolymarketCollectResult:
    """Run one Polymarket swing-detection pass, if `cfg.polymarket_enabled`.

    No-ops entirely (returns a fresh, unfailed PolymarketCollectResult)
    when the flag is off -- mirrors `_run_x_collector`'s own flag check,
    not `_run_news_collector`'s empty-tuple-means-disabled shape, because
    Polymarket has no natural "unconfigured" sentinel the way an empty feed
    list does (see Config.polymarket_enabled's own comment).

    The stored-anchor lookup is injected as a closure over `conn`
    (`digest.collectors.polymarket.collect`'s `get_stored_probs` parameter)
    rather than handing the collector the connection directly -- keeps that
    module testable purely against a fake urllib, with no real database in
    the loop, exactly like `get_cursors` is pre-fetched and handed to
    `x_collector.collect` above (the difference here is Polymarket can only
    know WHICH market ids to look up after the fetch, so the lookup itself,
    not just its result, has to be passed through).

    Plain `def`, called synchronously from inside this `async def _run` --
    not `await`ed, matching `_run_news_collector`'s own rationale
    (collectors already run one at a time; there's nothing else in flight
    for an executor wrapper to protect against blocking).

    `polymarket_collector.collect` already never raises past its own
    try/except around the single network request (module docstring's
    "Failure semantics"), but this call is wrapped in a catch-all here too
    as a second line of defense, mirroring every other collector wrapper in
    this module: a not-yet-anticipated bug must not take down the whole run
    (and the other collectors' already-collected items) before
    `commit_new_items` gets a chance to persist them.
    """
    if not cfg.polymarket_enabled:
        return PolymarketCollectResult()

    try:
        return polymarket_collector.collect(
            cfg.polymarket_api_base,
            cfg.polymarket_proxy_key,
            cfg.polymarket_top_n,
            cfg.polymarket_swing_threshold,
            lambda market_ids: get_polymarket_probs(conn, market_ids),
        )
    except Exception as exc:
        logger.warning("polymarket collection crashed unexpectedly: %s", type(exc).__name__)
        return PolymarketCollectResult(failed=True)


def _run_reddit_collector(cfg: Config) -> CollectResult:
    """Run one Reddit top-of-day collect pass, if `cfg.reddit_enabled`.

    No-ops entirely (returns a fresh, unfailed CollectResult) when the flag
    is off -- mirrors `_run_polymarket_collector`'s own flag check, not
    `_run_news_collector`'s empty-tuple-means-disabled shape: Reddit has no
    natural "unconfigured" sentinel the way an empty feed list does (see
    Config.reddit_enabled's own comment).

    Plain `def`, called synchronously from inside this `async def _run` --
    matching `_run_news_collector`/`_run_polymarket_collector`'s own
    rationale (collectors already run one at a time; there's nothing else
    in flight for an executor wrapper to protect against blocking).

    `reddit_collector.collect` already never raises past its own try/except
    around the session verify and each per-subreddit request (module
    docstring's "Failure semantics"), but this call is wrapped in a
    catch-all here too as a second line of defense, mirroring every other
    collector wrapper in this module: a not-yet-anticipated bug must not
    take down the whole run (and the other collectors' already-collected
    items) before `commit_new_items` gets a chance to persist them. Only the
    exception's type name is logged -- the underlying error could otherwise
    embed the session cookie from a malformed request.
    """
    if not cfg.reddit_enabled:
        return CollectResult()

    try:
        return reddit_collector.collect(
            cfg.reddit_session_cookie,
            cfg.reddit_subreddits,
            cfg.reddit_posts_per_sub,
        )
    except Exception as exc:
        logger.warning("reddit collection crashed unexpectedly: %s", type(exc).__name__)
        return CollectResult(failed=True)


def _run_hackernews_collector(cfg: Config) -> CollectResult:
    """Run one Hacker News front-page collect pass, if `cfg.hackernews_enabled`.

    No-ops entirely (returns a fresh, unfailed CollectResult) when the flag
    is off -- mirrors `_run_reddit_collector`'s/`_run_polymarket_collector`'s
    own flag check, not `_run_news_collector`'s empty-tuple-means-disabled
    shape: Hacker News has no natural "unconfigured" sentinel the way an
    empty feed list does (see Config.hackernews_enabled's own comment).

    Plain `def`, called synchronously from inside this `async def _run` --
    matching every other synchronous collector wrapper's own rationale
    (collectors already run one at a time; there's nothing else in flight
    for an executor wrapper to protect against blocking).

    `hackernews_collector.collect` already never raises past its own
    try/except around its single request (module docstring's "Failure
    semantics"), but this call is wrapped in a catch-all here too as a
    second line of defense, mirroring every other collector wrapper in this
    module: a not-yet-anticipated bug must not take down the whole run (and
    the other collectors' already-collected items) before `commit_new_items`
    gets a chance to persist them. Only the exception's type name is logged,
    consistent with how every other collector-crash log line in this module
    avoids echoing exception text that could embed response content.
    """
    if not cfg.hackernews_enabled:
        return CollectResult()

    try:
        return hackernews_collector.collect(cfg.hackernews_top_n)
    except Exception as exc:
        logger.warning("hackernews collection crashed unexpectedly: %s", type(exc).__name__)
        return CollectResult(failed=True)


async def _run(cfg: Config) -> bool:
    """Run one collection + delivery cycle. Returns True if it completed without failure."""
    conn = connect(cfg.state_db_path)
    try:
        init_db(conn)

        tg_cursors = get_cursors(conn, "telegram")

        client = TelegramClient(StringSession(cfg.tg_session), cfg.tg_api_id, cfg.tg_api_hash)
        try:
            if not await _client_ready(client):
                logger.warning("telegram session not authorized / connect failed")
                tg_result = CollectResult(failed=True)
            else:
                tg_result = await telegram_collector.collect(
                    client, cfg.tg_chat_allowlist, tg_cursors
                )
        finally:
            if client.is_connected():
                await client.disconnect()

        x_result = await _run_x_collector(conn, cfg)
        news_result = _run_news_collector(cfg)
        polymarket_result = _run_polymarket_collector(conn, cfg)
        reddit_result = _run_reddit_collector(cfg)
        hackernews_result = _run_hackernews_collector(cfg)

        items = (
            tg_result.items
            + x_result.items
            + news_result.items
            + polymarket_result.items
            + reddit_result.items
            + hackernews_result.items
        )
        # news never contributes cursor_updates (it has no cursor axis, see
        # digest/collectors/rss.py's module docstring) -- merging its
        # (always-empty) dict in here anyway keeps this line generic over
        # every collector rather than special-casing the one with nothing
        # to add. reddit is identical (see digest/collectors/reddit.py's
        # module docstring, "No cursor axis"), and so is hackernews (see
        # digest/collectors/hackernews.py's module docstring, "No cursor
        # axis"). polymarket ALSO has no cursor axis (its own state lives in
        # the polymarket_probs table, see digest/collectors/polymarket.py's
        # module docstring), but unlike news/reddit/hackernews it doesn't
        # even have a cursor_updates field on its result type --
        # PolymarketCollectResult is a distinct type carrying `prob_updates`
        # instead (handled below, not here).
        cursor_updates = {
            **tg_result.cursor_updates,
            **x_result.cursor_updates,
            **news_result.cursor_updates,
            **reddit_result.cursor_updates,
            **hackernews_result.cursor_updates,
        }

        # `polymarket_prob_updates` is only passed as a keyword argument when
        # non-empty: commit_new_items' signature already defaults it to None
        # for every other caller, but keeping this call itself two-shaped
        # (rather than always passing the kwarg, even as `{}` or `None`)
        # means a run where the collector is disabled or found nothing to
        # anchor is byte-for-byte the same call this function made before
        # Polymarket existed.
        if polymarket_result.prob_updates:
            inserted = commit_new_items(
                conn,
                items,
                cursor_updates,
                polymarket_prob_updates=polymarket_result.prob_updates,
            )
        else:
            inserted = commit_new_items(conn, items, cursor_updates)
        logger.info(
            "collected %d new items (%d inserted), cursors advanced for %d scopes",
            len(items),
            inserted,
            len(cursor_updates),
        )

        failed_sources = [
            source
            for source, failed in (
                ("telegram", tg_result.failed),
                ("x", x_result.failed),
                ("news", news_result.failed),
                ("polymarket", polymarket_result.failed),
                ("reddit", reddit_result.failed),
                ("hackernews", hackernews_result.failed),
            )
            if failed
        ]

        delivered = _deliver(conn, cfg, failed_sources)

        pruned = prune_delivered_items(
            conn,
            email_enabled=cfg.email_enabled,
            site_enabled=cfg.site_publish_url is not None,
            telegram_enabled=cfg.telegram_notify_bot_token is not None,
        )
        if pruned:
            logger.info("pruned %d delivered items older than %d days", pruned, _ITEMS_PRUNE_DAYS)

        # Companion prune for rows the one above can never touch: items that
        # NEVER got selected into a digest (quota-lane overflow, byte-shrink
        # casualties) -- see prune_stale_unsummarized's docstring for why 14
        # days of consecutive non-selection means "never going to ship".
        stale_pruned = prune_stale_unsummarized(conn)
        if stale_pruned:
            logger.info("pruned %d stale unsummarized items", stale_pruned)

        ok = delivered and not failed_sources

        # Exit code stays the sole alert trigger (see main()'s sys.exit(0 if
        # ok else 1)); this line exists purely so a Loki query can see WHICH
        # leg of a window run failed -- a specific collector, delivery, or
        # both -- without a log dive across each collector's own lines above.
        collectors = {"telegram": "failed" if tg_result.failed else "ok"}
        if cfg.x_enabled:
            collectors["x"] = "failed" if x_result.failed else "ok"
        if cfg.news_feeds:
            collectors["news"] = "failed" if news_result.failed else "ok"
        if cfg.polymarket_enabled:
            collectors["polymarket"] = "failed" if polymarket_result.failed else "ok"
        if cfg.reddit_enabled:
            collectors["reddit"] = "failed" if reddit_result.failed else "ok"
        if cfg.hackernews_enabled:
            collectors["hackernews"] = "failed" if hackernews_result.failed else "ok"
        logger.info(
            "run_summary %s",
            json.dumps(
                {
                    "mode": "window",
                    "collectors": collectors,
                    "items_collected": len(items),
                    "items_inserted": inserted,
                    "delivered": delivered,
                    "ok": ok,
                },
                sort_keys=True,
            ),
        )

        return ok
    finally:
        conn.close()


def _generate_arc_context_primers(conn: sqlite3.Connection, cfg: Config, now: datetime) -> int:
    """Generate and persist background primers for qualifying arcs (PLAN.md §11.6).

    Returns the count of NEW primers generated this call.

    Called from `run_daily` ONLY, and only AFTER that run's own daily brief
    has already been produced and delivered (see `run_daily`'s own call
    site) -- generation here must never affect, delay, or degrade the daily
    brief itself. A no-op returning 0 when `cfg.context_enabled` is False
    (the default): this is new generation work the owner enables
    deliberately, not an always-on step (see `Config.context_enabled`'s own
    comment).

    Reuses `_RECENT_ARCS_WINDOW` (7 days) as the qualification window --
    the SAME trailing window the stable-arc-keys feature's own
    {{RECENT_ARCS}} prompt block uses, and the SAME window the site's own
    recurring-arc threshold uses (verified 2026-08-10 against
    cloudflare-terraform/workers/news-site/worker.js, see digest/state.py's
    `get_arc_keys_needing_context` docstring) -- so "qualifies for a
    primer" agrees with "the site would call this a recurring arc" by
    construction, not by a second, potentially-drifting constant.

    For each key `get_arc_keys_needing_context` returns (already bounded to
    `cfg.context_max_per_run`, oldest-first-seen):
    1. `get_latest_arc_occurrence` resolves which digest's `body_md` most
       recently tagged this key, plus that occurrence's own topic `slug`.
    2. That `body_md` is run back through `derive_topics` (the SAME
       heading->slug->label fold `_deliver_site` already applies to every
       digest) to recover this arc's own most recent LABEL -- the ONLY
       input `generate_arc_context` is ever given (PLAN.md §11.6 rule 3:
       "no brief text, no RECENT_COVERAGE, no window items", so this can
       never become a second, unfenced replay channel the way §11.3's
       `deltas` guardrails exist to prevent for a different feature). A
       missing occurrence or an unresolvable label is skipped defensively
       (should not happen in practice -- every `arc_keys` row this queries
       was itself written FROM a `derive_topics` call whose slug set is
       exactly what this re-derives -- but a skip here costs nothing and a
       raise would risk the whole daily run over one arc's data).
    3. `generate_arc_context` (digest/context.py) does the actual toolless
       `claude -p` call and never raises -- see its own docstring for the
       three internally-collapsed reasons a call can return `None`.

    ACCEPTED BEHAVIOR on a `None` result (PLAN.md §11.6 rule 4's explicit
    call to make and document): NOTHING is written for that key. No
    tombstone. The key simply stays eligible and is reconsidered on the
    NEXT daily run that still sees it qualify. This was a deliberate choice
    over a tombstone, for two reasons:
    1. `generate_arc_context`'s return value collapses THREE distinct
       causes (a CLI/timeout failure, the model's own INSUFFICIENT_CONTEXT
       sentinel, and defensive empty-output handling) into one `None` --
       there is no way for this caller to tell "genuinely, permanently too
       vague to ever write about" apart from "hit a transient CLI hiccup
       today" without either widening that function's return contract
       (which PLAN.md §11.6 fixes as `str | None`) or storing a tombstone
       on EVERY failure reason alike, which would also permanently and
       silently give up on an otherwise-fine arc that simply had one bad
       `claude -p` call.
    2. This cannot loop forever in the unbounded sense the instruction
       warns about: `get_arc_keys_needing_context`'s own qualification is
       itself bounded by the SAME rolling 7-day recurrence window that made
       the key eligible in the first place -- a key stops being
       reconsidered the moment it stops recurring, not after some fixed
       retry count. The realistic worst case is a story that keeps
       genuinely recurring for weeks under a label the model consistently
       finds too thin to write about: that wastes at most
       `cfg.context_max_per_run` calls PER DAY (a small, fixed, owner-tuned
       number, default 3) for as long as it keeps recurring, never an
       unbounded or growing cost, and it self-corrects the instant the
       story stops appearing.

    Never raises: every step above already soft-fails on its own (a missing
    occurrence/label is skipped; `generate_arc_context` never raises by its
    own contract), so there is nothing left here that could propagate an
    exception into `run_daily` and jeopardize a brief that has, by this
    point, already shipped.
    """
    if not cfg.context_enabled:
        return 0

    since_iso = (now - _RECENT_ARCS_WINDOW).isoformat()
    generated = 0
    for key in get_arc_keys_needing_context(conn, since_iso, cfg.context_max_per_run):
        occurrence = get_latest_arc_occurrence(conn, key)
        if occurrence is None:
            continue
        body_md, slug = occurrence
        label = next((t["label"] for t in derive_topics(body_md) if t["slug"] == slug), None)
        if label is None:
            continue

        context_md = generate_arc_context(label, cfg.context_model, cfg.context_timeout_seconds)
        if context_md is None:
            continue

        write_arc_context(conn, key, context_md)
        generated += 1

    return generated


def run_daily(cfg: Config, *, force: bool = False) -> bool:
    """Run the once-a-day brief synthesis + delivery cycle. Returns True if it completed OK.

    Invoked by `python -m digest daily` -- a SEPARATE systemd timer on the
    homelab side (not this repo, see CLAUDE.md's Deploy note) fires this once
    a day, independently of the every-6-hours `python -m digest` timer that
    drives `_run`/`_deliver` above. Deliberately synchronous (plain `def`,
    no `asyncio.run` at the call site in `main()`): unlike `_run`, this mode
    never touches Telethon/twikit -- it only reads already-collected digests
    back out of SQLite and drives the same synchronous summarize/translate/
    deliver machinery `_deliver` does, so there is nothing here that needs an
    event loop.

    `force` (keyword-only, default False) is the escape hatch for a
    deliberate manual re-run -- wired through from `python -m digest daily
    --force` (see `main()`'s dispatch) -- and bypasses step 2's
    duplicate-fire guard below ONLY; every other step runs exactly as normal.

    Pipeline:
    1. Connect/init_db, then run `deliver_pending` FIRST -- exactly like
       `_deliver` does for the window-digest run mode, and reusing that same
       helper rather than duplicating its retry logic -- so a previous
       day's daily brief that got summarized but only partially delivered
       (e.g. site published, Telegram 429'd) is retried before today's new
       brief is even summarized. This also opportunistically retries any
       still-pending WINDOW digest this run happens to see, which is
       harmless (idempotent per channel) even though the every-6-hours job
       already covers that case on its own schedule. Runs BEFORE step 2's
       duplicate-fire guard, and unconditionally of it (`force` never skips
       this step): a stuck partially-delivered brief must still be retried
       even on a run this guard is about to skip as a duplicate, or it would
       never get retried until the NEXT day's daily run.
    2. Duplicate-fire guard (unless `force`): the scheduling that decides
       WHEN this function runs is a systemd timer living OUTSIDE this repo
       (see CLAUDE.md's Deploy note), so this code cannot assume the timer
       only ever fires once a day. `get_daily_digests_since(conn, since)`,
       `since` being `_DAILY_DUPLICATE_GUARD_WINDOW` (20h, this module) before
       now, is reused (rather than a new query -- see that function's own
       docstring: it already returns `kind = 'daily'` rows at/after a given
       ISO instant, exactly this guard's shape) to check whether a daily
       brief already exists within that window. If one does, this run is
       treated as a duplicate fire and skipped as a harmless no-op: logged,
       `run_summary` stamped with `skipped_duplicate: true`, and step 1's
       `all_ok` returned as-is -- no window digests are read, no `claude -p`
       call is made, no new digest row is created. See
       `_DAILY_DUPLICATE_GUARD_WINDOW`'s own docstring for why 20h (not
       `DAILY_LOOKBACK_WINDOW`'s 24h) is the right threshold, and
       `scripts/backfill_daily.py`'s module docstring for why a backfilled
       historical row never trips this guard on a later live run (its
       `created_at` is a past evening's boundary, not "now").
    3. `rows = get_window_digests_since(conn, since)`, `since` being
       `DAILY_LOOKBACK_WINDOW` (24h, digest/state.py) before now -- the day's worth of
       already-curated window briefings to synthesize (kind='window' only;
       see that function's docstring for why a prior daily brief can never
       feed a later one). Empty `rows` (no window digest ran in the last
       24h -- e.g. a very early deploy, or the window job was down all day)
       is logged and returns `all_ok` from step 1 as-is: nothing to brief is
       a normal empty day, not a failure, exactly like `_deliver`'s own
       "no unsummarized items" branch.
    4. `allowed_urls` is the UNION of `get_digest_item_urls` over every
       source window digest -- a daily brief may cite any URL any of its
       source briefings could cite, never a fresh set of raw item URLs (a
       daily brief never sees raw items at all).
    5. `summarize_daily` (digest/daily.py) -- UNLIKE `_deliver`'s own
       `summarize()` call, a `SummarizeError` here is caught, logged, and
       turned into a `False` return rather than propagating further: this
       function's contract (like `_deliver`'s) is "return whether the run
       succeeded", not "raise on failure".
    6. PLAN.md §11.4's OPTIONAL verification pass (`VERIFY_DAILY_ENABLED`,
       default off): `digest/verify.py`'s `verify_daily` cross-checks the
       draft against the open web and returns a corrected/corroborated
       brief plus a WIDENED allowlist. Any failure (CLI/transcript trouble,
       or output failing the daily contract) soft-fails -- the draft ships
       unchanged except for a code-prepended `⚠ verification unavailable
       this run` banner (`_VERIFY_UNAVAILABLE_BANNER`), and the allowlist
       stays un-widened. Never raises, never returns `False` -- unlike step
       5's `summarize_daily`, an unverified brief on time still beats no
       brief at all.
    7. The optional Hungarian translation -- identical `translate_digest`
       call, identical soft-failing contract, as `_deliver` uses for a
       window digest -- checked against step 6's (possibly widened)
       allowlist, not step 4's original one, so a verified brief's
       translation isn't stripped of citations the English verified body
       was allowed to keep.
    8. `create_digest(..., items=[], kind="daily", item_count=...)` -- a
       daily brief stamps NO items (it consumes digests, not items; the
       empty-items path is exercised and supported, see digest/state.py's
       `create_digest` and its test coverage) but its stored `item_count` is
       explicitly overridden to the SUM of the source window digests' own
       item_counts (see `create_digest`'s docstring for why the override
       parameter exists) -- that sum is what the site's "N items" line and
       the closing-line count sanity actually describe for a daily brief,
       not "0 items" (which `len(items)` would otherwise store) and not any
       single source digest's own count.
    9. `archive()` it, exactly like any digest -- unconditional, not gated
       on any channel's success, identical rationale to `_deliver`'s own
       archiving.
    10. `deliver_channels` with a FRESH `done` map (this digest was just
        created, nothing attempted yet), this function's own
        `telegram_state` -- shared with step 1's `deliver_pending` call, for
        the identical GUARD-2-circuit-breaker reason `_deliver` shares one
        `TelegramRunState` across its own two `deliver_channels` call
        sites -- and step 6's allowlist as `extra_allowed_urls`, so a
        verified brief's own widened citations survive render time too, not
        just this function's own translation call in step 7.

    Returns True iff step 1's pending pass AND this run's own fresh delivery
    (when a brief was actually produced) both succeeded -- the AND of the
    same two-part contract `_deliver` upholds for the window-digest run mode.
    A run skipped by step 2's duplicate-fire guard returns step 1's `all_ok`
    alone (there is no "fresh delivery" half to AND it with): a skipped
    duplicate is a SUCCESSFUL no-op, and `main()` uses this return value only
    to pick the process exit code (0 vs 1, the sole Loki alert trigger -- see
    `main()`'s own comment), so returning anything other than step 1's own
    result here would either mask a genuine pending-delivery failure (if
    hardcoded True) or spuriously alert on a correctly-skipped duplicate (if
    hardcoded False).
    """
    conn = connect(cfg.state_db_path)
    try:
        init_db(conn)

        telegram_state = TelegramRunState()
        all_ok = deliver_pending(conn, cfg, telegram_state)

        now = datetime.now(UTC)

        # Step 2 (see docstring): unless `force`, skip this run as a
        # duplicate no-op when a daily brief already exists within the last
        # _DAILY_DUPLICATE_GUARD_WINDOW. Deliberately BEFORE step 3 reads any
        # window digests and BEFORE step 5's `claude -p` call -- the whole
        # point is to avoid the expensive work, not just avoid a duplicate
        # digest row.
        if not force:
            guard_since = (now - _DAILY_DUPLICATE_GUARD_WINDOW).isoformat()
            if get_daily_digests_since(conn, guard_since):
                logger.info(
                    "a daily brief already ran within the last %s, skipping "
                    "as a duplicate no-op (use --force to override)",
                    _DAILY_DUPLICATE_GUARD_WINDOW,
                )
                logger.info(
                    "run_summary %s",
                    json.dumps(
                        {
                            "mode": "daily",
                            "source_digests": 0,
                            "delivered": all_ok,
                            "ok": all_ok,
                            "context_generated": 0,
                            "skipped_duplicate": True,
                        },
                        sort_keys=True,
                    ),
                )
                return all_ok

        since = now - DAILY_LOOKBACK_WINDOW
        rows = get_window_digests_since(conn, since.isoformat())
        if not rows:
            logger.info("no window digests in the last 24 hours, nothing to brief today")
            # Exit code stays the sole alert trigger; this line lets a Loki
            # query see this was an empty-day no-op (source_digests: 0)
            # rather than a failed daily brief. Nothing was freshly
            # delivered this run -- both fields fall back to the pending
            # pass's own result, matching `delivered`/`ok`'s meaning below.
            # `context_generated` is always 0 on this path (PLAN.md §11.6):
            # no brief was produced this run at all, so there is nothing to
            # generate primers "after" -- see `run_daily`'s own call site
            # further down for the non-empty-day path.
            logger.info(
                "run_summary %s",
                json.dumps(
                    {
                        "mode": "daily",
                        "source_digests": 0,
                        "delivered": all_ok,
                        "ok": all_ok,
                        "context_generated": 0,
                    },
                    sort_keys=True,
                ),
            )
            return all_ok

        allowed_urls: set[str] = set()
        for source_digest_id, _created_at, _item_count, _body_md in rows:
            allowed_urls |= get_digest_item_urls(conn, source_digest_id)

        try:
            body_md = summarize_daily(
                rows,
                allowed_urls,
                cfg.anthropic_model,
                cfg.claude_timeout_seconds,
                cfg.claude_effort,
            )
        except SummarizeError as exc:
            logger.error("daily brief summarization failed: %s", exc)
            return False

        # PLAN.md §11.4: optional verification pass, draft -> verify ->
        # translate -> deliver. `delivery_allowed_urls` starts as the
        # draft's own allowlist and is only ever WIDENED (never narrowed) if
        # verification succeeds -- both `translate_digest` below and
        # `deliver_channels`' own `extra_allowed_urls` (further down) must
        # see the SAME widened set the verified `body_md`'s own citations
        # were checked against, or a citation `verify_daily` already
        # enforced successfully would get stripped again downstream, on the
        # Hungarian translation or at render time, defeating verification
        # within the very run that produced it.
        #
        # ANY failure (a `VerificationUnavailable` from `run_claude_verify`
        # -- CLI failure, timeout, or a transcript-shape surprise -- or a
        # `SummarizeError` from `validate_output` -- the verified text
        # failed the same structural contract every briefing in this
        # codebase has to satisfy) soft-fails to the SAME place: the
        # unmodified draft ships, with the code-prepended banner, and
        # `delivery_allowed_urls` stays the draft's own un-widened set --
        # PLAN.md §11.4's own contract, matching `translate_digest`'s
        # existing soft-fail semantics: an unverified brief on time beats no
        # brief.
        delivery_allowed_urls = allowed_urls
        verified = False
        if cfg.verify_daily_enabled:
            try:
                verified_body_md, widened_urls = verify_daily(
                    body_md,
                    allowed_urls,
                    cfg.verify_daily_model,
                    cfg.verify_daily_timeout_seconds,
                    cfg.verify_daily_effort,
                    cfg.verify_daily_max_web_ops,
                )
            except (VerificationUnavailable, SummarizeError) as exc:
                logger.warning("daily brief verification unavailable: %s", exc)
                body_md = _VERIFY_UNAVAILABLE_BANNER + body_md
            else:
                body_md = verified_body_md
                delivery_allowed_urls = widened_urls
                verified = True

        # Hungarian translation: same optional, soft-failing production step
        # `_deliver` runs for a window digest -- see that call site's own
        # comment for the full rationale, identical here. Checked against
        # `delivery_allowed_urls` (see above), not the draft's own narrower
        # `allowed_urls`, so a verified brief's translation isn't stripped
        # of citations the English verified body was allowed to keep.
        body_md_hu: str | None = None
        if cfg.translate_hu_enabled:
            body_md_hu = translate_digest(
                body_md,
                delivery_allowed_urls,
                cfg.translate_model,
                cfg.translate_timeout_seconds,
                fallback_model=cfg.translate_model_fallback,
            )

        total_items = sum(item_count for _, _, item_count, _ in rows)
        digest_id = create_digest(
            conn, body_md, [], body_md_hu=body_md_hu, kind="daily", item_count=total_items
        )
        archive(body_md, cfg.archive_dir, digest_id)

        item_count, created_at, body_md_hu, kind = digest_meta(conn, digest_id)
        done = {"email": False, "site": False, "telegram": False}
        ok = deliver_channels(
            conn, cfg, digest_id, body_md, item_count, created_at, done, telegram_state, body_md_hu,
            kind=kind,
            extra_allowed_urls=delivery_allowed_urls,
        )
        result = all_ok and ok

        # PLAN.md §11.6 "context mode": AFTER this run's own daily brief has
        # been produced and delivered above -- generating primers here must
        # never affect, delay, or degrade that delivery, which by this point
        # has already fully happened either way. A no-op (0) when
        # `cfg.context_enabled` is False, the default -- see
        # `_generate_arc_context_primers`'s own docstring for the full
        # qualification/generation/persistence pipeline and its accepted
        # "no tombstone" failure behavior.
        context_generated = _generate_arc_context_primers(conn, cfg, now)

        # Exit code stays the sole alert trigger; this line is for Loki
        # queries to see WHICH leg failed -- the pending-retry pass vs this
        # run's own fresh brief -- without a log dive. `verified` (PLAN.md
        # §11.4) is always `false` while VERIFY_DAILY_ENABLED defaults off;
        # once flagged on, it's the one field that lets a Loki query tell
        # "verification ran and succeeded" apart from "shipped the draft
        # with the banner" without grepping the WARNING line above -- exactly
        # the visibility the flag-on live-validation step needs.
        # `context_generated` (PLAN.md §11.6) is always 0 while
        # CONTEXT_ENABLED defaults off; once flagged on, it's the Loki-visible
        # count of NEW background primers this run generated (bounded by
        # CONTEXT_MAX_PER_RUN), independent of `ok`/`result` above -- a
        # primer-generation outcome never affects the daily brief's own
        # success/failure.
        logger.info(
            "run_summary %s",
            json.dumps(
                {
                    "mode": "daily",
                    "source_digests": len(rows),
                    "delivered": ok,
                    "ok": result,
                    "verified": verified,
                    "context_generated": context_generated,
                },
                sort_keys=True,
            ),
        )

        return result
    finally:
        conn.close()


def run_weekly(cfg: Config) -> bool:
    """Run the once-a-week (Sunday-evening) brief synthesis + delivery cycle.

    Returns True if it completed OK.

    Invoked by `python -m digest weekly` -- a THIRD, separate systemd timer
    on the homelab side (not this repo, see CLAUDE.md's Deploy note) fires
    this once a week, independently of both the every-6-hours `python -m
    digest` timer (`_run`/`_deliver`) and the once-a-day `python -m digest
    daily` timer (`run_daily`). Deliberately synchronous (plain `def`, no
    `asyncio.run` at the call site in `main()`), for the identical reason
    `run_daily` is: this mode never touches Telethon/twikit either -- it
    only reads already-summarized digests back out of SQLite and drives the
    same synchronous summarize/translate/deliver machinery `run_daily` does,
    so there is nothing here that needs an event loop.

    Mirrors `run_daily` step-for-step -- same `deliver_pending`-first
    ordering, same empty-input no-op contract, same `SummarizeError`-catches-
    to-`False` contract, same soft-failing Hungarian translation step, same
    unconditional `archive()`, same fresh `done` map into `deliver_channels`,
    same `run_summary` logging shape -- with exactly ONE structural
    difference, in step 3 below (`allowed_urls`); every other step differs
    from `run_daily` only in which table/kind it reads or writes.

    Pipeline:
    1. Connect/init_db, then run `deliver_pending` FIRST -- identical
       rationale to `run_daily`'s own step 1: a previous week's report that
       got summarized but only partially delivered (e.g. site published,
       Telegram 429'd) is retried before this week's new report is even
       summarized. This also opportunistically retries any still-pending
       WINDOW or DAILY digest this run happens to see, harmless (idempotent
       per channel) for the identical reason `run_daily`'s own step 1 is.
    2. `rows = get_daily_digests_since(conn, since)`, `since` being
       `WEEKLY_LOOKBACK_WINDOW` (7 days, digest/state.py) before now -- the
       week's worth of already-curated DAILY briefs to synthesize
       (kind='daily' only; see that function's docstring for why a prior
       weekly report can never feed a later one). Empty `rows` (no daily
       brief ran in the last 7 days -- e.g. a very early deploy, or the
       daily job was down all week) is logged and returns `all_ok` from
       step 1 as-is: nothing to brief is a normal empty week, not a
       failure, exactly like `run_daily`'s own empty-day branch.
    3. `allowed_urls` -- THE ONE STRUCTURAL DIFFERENCE FROM `run_daily`. A
       daily brief stamps NO items of its own (digest/state.py's
       `create_digest` `kind` docstring), so `get_digest_item_urls` against
       one of THIS week's source daily digests is always empty -- unlike
       `run_daily`, which can union `get_digest_item_urls` straight over its
       source (window) digests, that same approach here would produce an
       allowlist that is always the empty set, defanging every citation the
       model writes. The URLs a weekly brief is actually allowed to cite
       live one hop further down: on the WINDOW digests underneath those
       dailies. So this derives the TRANSITIVE provenance set instead --
       `get_window_digests_since(conn, since)` over the SAME `since` bound
       used for step 2, unioned with `get_digest_item_urls` per window
       digest -- i.e. "every URL any of the week's window digests could
       cite, hence every URL the week's dailies could cite, hence every URL
       this weekly brief can cite." `get_weekly_allowed_urls`
       (digest/state.py) re-derives this identical set at delivery time
       (fresh or pending-resend), the same way `get_daily_allowed_urls` does
       for `run_daily` one rung down.
    4. `summarize_weekly` (digest/weekly.py) -- UNLIKE `_deliver`'s own
       `summarize()` call, but exactly like `run_daily`'s own
       `summarize_daily` call, a `SummarizeError` here is caught, logged,
       and turned into a `False` return rather than propagating further:
       this function's contract (like `run_daily`'s) is "return whether the
       run succeeded", not "raise on failure".
    5. The optional Hungarian translation -- identical `translate_digest`
       call, identical soft-failing contract, as `run_daily` uses for a
       daily brief (and `_deliver` for a window digest).
    6. `create_digest(..., items=[], kind="weekly", item_count=...)` -- a
       weekly report stamps NO items (it consumes daily digests, not items,
       exactly like a daily brief consumes window digests, not items) but
       its stored `item_count` is explicitly overridden to the SUM of the
       source DAILY rows' own item_counts (each of which is ITSELF already
       the sum of ITS OWN source window digests' item_counts -- see
       `create_digest`'s docstring for why the override parameter exists) --
       that sum is what the site's "N items" line and the closing-line count
       sanity actually describe for a weekly report.
    7. `archive()` it, exactly like any digest -- unconditional, not gated
       on any channel's success, identical rationale to `run_daily`'s own
       archiving.
    8. `deliver_channels` with a FRESH `done` map (this digest was just
       created, nothing attempted yet) and this function's own
       `telegram_state` -- shared with step 1's `deliver_pending` call, for
       the identical GUARD-2-circuit-breaker reason `run_daily` shares one
       `TelegramRunState` across its own two `deliver_channels` call sites.
       `kind="weekly"` here is what makes `deliver_channels` pick
       `cfg.telegram_weekly_thread_id` (digest/deliver.py's
       `_telegram_thread_id_for_kind`) and `get_weekly_allowed_urls` (step 3's
       delivery-time counterpart) instead of the window/daily equivalents.

    No same-week dedupe guard -- the systemd timer is the idempotency,
    exactly like `run_daily` (which itself has no same-day dedupe guard, for
    the identical reason): running this twice in the same week produces two
    weekly digests, and nothing here prevents that on purpose. Preventing it
    is the scheduling's job (one weekly timer firing once a week), not this
    function's.

    Returns True iff step 1's pending pass AND this run's own fresh delivery
    (when a report was actually produced) both succeeded -- the AND of the
    same two-part contract `run_daily` upholds for the daily-brief run mode.
    """
    conn = connect(cfg.state_db_path)
    try:
        init_db(conn)

        telegram_state = TelegramRunState()
        all_ok = deliver_pending(conn, cfg, telegram_state)

        now = datetime.now(UTC)
        since = now - WEEKLY_LOOKBACK_WINDOW
        rows = get_daily_digests_since(conn, since.isoformat())
        if not rows:
            logger.info("no daily briefs in the last 7 days, nothing to brief this week")
            # Exit code stays the sole alert trigger; this line lets a Loki
            # query see this was an empty-week no-op (source_digests: 0)
            # rather than a failed weekly report. Nothing was freshly
            # delivered this run -- both fields fall back to the pending
            # pass's own result, matching `delivered`/`ok`'s meaning below.
            logger.info(
                "run_summary %s",
                json.dumps(
                    {
                        "mode": "weekly",
                        "source_digests": 0,
                        "delivered": all_ok,
                        "ok": all_ok,
                    },
                    sort_keys=True,
                ),
            )
            return all_ok

        # See this function's own docstring, step 3: the TRANSITIVE
        # provenance set -- the union of item URLs across every WINDOW
        # digest in the same 7-day lookback, NOT the (always-empty) union of
        # the source DAILY rows' own stamped item URLs.
        allowed_urls: set[str] = set()
        window_rows = get_window_digests_since(conn, since.isoformat())
        for source_digest_id, _created_at, _item_count, _body_md in window_rows:
            allowed_urls |= get_digest_item_urls(conn, source_digest_id)

        try:
            body_md = summarize_weekly(
                rows,
                allowed_urls,
                cfg.anthropic_model,
                cfg.claude_timeout_seconds,
                cfg.claude_effort,
            )
        except SummarizeError as exc:
            logger.error("weekly brief summarization failed: %s", exc)
            return False

        # Hungarian translation: same optional, soft-failing production step
        # `run_daily` runs for a daily brief -- see that call site's own
        # comment for the full rationale, identical here.
        body_md_hu: str | None = None
        if cfg.translate_hu_enabled:
            body_md_hu = translate_digest(
                body_md,
                allowed_urls,
                cfg.translate_model,
                cfg.translate_timeout_seconds,
                fallback_model=cfg.translate_model_fallback,
            )

        total_items = sum(item_count for _, _, item_count, _ in rows)
        digest_id = create_digest(
            conn, body_md, [], body_md_hu=body_md_hu, kind="weekly", item_count=total_items
        )
        archive(body_md, cfg.archive_dir, digest_id)

        item_count, created_at, body_md_hu, kind = digest_meta(conn, digest_id)
        done = {"email": False, "site": False, "telegram": False}
        ok = deliver_channels(
            conn, cfg, digest_id, body_md, item_count, created_at, done, telegram_state, body_md_hu,
            kind=kind,
        )
        result = all_ok and ok

        # Exit code stays the sole alert trigger; this line is for Loki
        # queries to see WHICH leg failed -- the pending-retry pass vs this
        # run's own fresh report -- without a log dive.
        logger.info(
            "run_summary %s",
            json.dumps(
                {
                    "mode": "weekly",
                    "source_digests": len(rows),
                    "delivered": ok,
                    "ok": result,
                },
                sort_keys=True,
            ),
        )

        return result
    finally:
        conn.close()


def main() -> None:
    load_dotenv()

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    # argv-based mode dispatch: `python -m digest daily` runs the once-a-day
    # brief (run_daily); `python -m digest weekly` runs the once-a-week
    # report (run_weekly); no argument (or anything else) keeps today's
    # behavior exactly -- the every-6-hours collect+deliver cycle (_run),
    # unchanged. The scheduling itself (which timer fires which mode, and
    # when) lives entirely outside this repo (see CLAUDE.md's Deploy note);
    # this is just the dispatch a systemd unit's ExecStart invokes into.
    #
    # `python -m digest daily --force` bypasses run_daily's own
    # duplicate-fire guard (see that function's docstring, step 2, and
    # `_DAILY_DUPLICATE_GUARD_WINDOW`) -- the owner's manual escape hatch for
    # a deliberate second daily run on the same day. No argparse: this
    # dispatch is already a plain positional-argv check, not a flag parser,
    # so `--force` is checked the same way, as a bare substring match against
    # the remaining argv -- only meaningful (and only checked) alongside
    # `daily`.
    if len(sys.argv) > 1 and sys.argv[1] == "daily":
        ok = run_daily(cfg, force="--force" in sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == "weekly":
        ok = run_weekly(cfg)
    else:
        ok = asyncio.run(_run(cfg))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
