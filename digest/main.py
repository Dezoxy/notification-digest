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

from digest.collectors import polymarket as polymarket_collector
from digest.collectors import reddit as reddit_collector
from digest.collectors import rss as rss_collector
from digest.collectors import telegram as telegram_collector
from digest.collectors import x as x_collector
from digest.collectors.base import CollectResult
from digest.collectors.polymarket import PolymarketCollectResult
from digest.config import Config, ConfigError
from digest.daily import summarize_daily
from digest.deliver import TelegramRunState, deliver_channels, deliver_pending, digest_meta
from digest.emailer import archive
from digest.state import (
    commit_new_items,
    connect,
    count_unsummarized_items,
    create_digest,
    get_cursors,
    get_digest_item_urls,
    get_polymarket_probs,
    get_recent_digests,
    get_unsummarized_items,
    get_window_digests_since,
    init_db,
)
from digest.summarize import (
    _MAX_PROMPT_BYTES,
    SummarizeError,
    format_recent_coverage,
    select_items_for_prompt,
    summarize,
)
from digest.translate import translate_digest

# How far back _deliver looks for prior digests when building the
# {{RECENT_COVERAGE}} prompt block (digest/summarize.py's
# format_recent_coverage) -- the "running story memory" that lets the
# summarizer write delta-only updates for stories it already covered instead
# of re-explaining them every 3 hours. 24 hours is a full day's worth of
# briefings (8 runs at the 3-hourly cadence) -- long enough that a story
# spanning a slow news day is still recognized as "already covered" on its
# second or third mention, short enough that genuinely stale coverage
# eventually ages out and stops suppressing a fresh full write-up.
_RECENT_COVERAGE_WINDOW = timedelta(hours=24)

# How far back run_daily looks for window digests to synthesize into one
# daily brief (digest/state.py's get_window_digests_since). A full 24 hours
# so a brief run at 20:00 covers exactly "since yesterday's brief", with no
# gap or overlap at the boundary -- the scheduling itself (the second
# systemd timer that invokes `python -m digest daily`) lives outside this
# repo, so this window is what actually defines "one day" from this code's
# point of view.
_DAILY_LOOKBACK_WINDOW = timedelta(hours=24)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Bounds how many unsummarized items a single Claude call is given. Each
# allowlisted chat can contribute up to 500 messages per run, and a failed
# summarize call carries the backlog forward plus new items on top -- with
# no cap, the prompt for an accumulated backlog can exceed the model's
# context window, and an oversized prompt then fails every subsequent run
# forever (no items ever get stamped). The remainder ships in later runs:
# the 3-hourly timer is the drain loop for a large backlog, at
# _MAX_ITEMS_PER_DIGEST items per digest.
_MAX_ITEMS_PER_DIGEST = 200


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

    items = get_unsummarized_items(conn, limit=_MAX_ITEMS_PER_DIGEST)
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

    # Shrink to whatever actually fits in one prompt BEFORE both summarize()
    # and create_digest(): the item-count cap above (_MAX_ITEMS_PER_DIGEST)
    # bounds source characters, but json.dumps(ensure_ascii=False) still lets
    # an emoji/CJK-heavy batch serialize to far more UTF-8 BYTES than that
    # count implies, and select_items_for_prompt (measured in bytes, not
    # characters -- see _MAX_PROMPT_BYTES) is what catches that. The shrink
    # has to happen HERE, not inside summarize(), because create_digest stamps
    # whatever list it's given as "handled" -- if summarize() only saw a
    # trimmed subset internally while create_digest stamped the full
    # pre-shrink `items`, the untrimmed remainder would be marked summarized
    # without ever actually being sent to the model. Keeping the shrink in
    # _deliver and passing its result to both calls keeps the summarized set
    # and the stamped set identical by construction. `recent_coverage` is
    # passed through here too: it is embedded in every built prompt exactly
    # like the items are, so its bytes count toward _MAX_PROMPT_BYTES
    # automatically (see select_items_for_prompt's docstring).
    items = select_items_for_prompt(items, failed_sources, recent_coverage, _MAX_PROMPT_BYTES)

    try:
        body_md = summarize(
            items,
            failed_sources,
            recent_coverage,
            cfg.anthropic_model,
            cfg.claude_timeout_seconds,
            cfg.claude_effort,
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
            cfg.claude_timeout_seconds,
            fallback_model=cfg.translate_model_fallback,
        )

    digest_id = create_digest(conn, body_md, items, body_md_hu=body_md_hu)
    archive(body_md, cfg.archive_dir, digest_id)

    item_count, created_at, body_md_hu, kind = digest_meta(conn, digest_id)
    done = {"email": False, "site": False, "telegram": False}
    ok = deliver_channels(
        conn, cfg, digest_id, body_md, item_count, created_at, done, telegram_state, body_md_hu,
        kind=kind,
    )
    all_ok = all_ok and ok

    # One Opus call per run keeps cost and runtime bounded -- do NOT loop
    # summarize here even if a remainder is left; the 3-hourly timer is the
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

        items = (
            tg_result.items
            + x_result.items
            + news_result.items
            + polymarket_result.items
            + reddit_result.items
        )
        # news never contributes cursor_updates (it has no cursor axis, see
        # digest/collectors/rss.py's module docstring) -- merging its
        # (always-empty) dict in here anyway keeps this line generic over
        # every collector rather than special-casing the one with nothing
        # to add. reddit is identical (see digest/collectors/reddit.py's
        # module docstring, "No cursor axis"). polymarket ALSO has no cursor
        # axis (its own state lives in the polymarket_probs table, see
        # digest/collectors/polymarket.py's module docstring), but unlike
        # news/reddit it doesn't even have a cursor_updates field on its
        # result type -- PolymarketCollectResult is a distinct type carrying
        # `prob_updates` instead (handled below, not here).
        cursor_updates = {
            **tg_result.cursor_updates,
            **x_result.cursor_updates,
            **news_result.cursor_updates,
            **reddit_result.cursor_updates,
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
            )
            if failed
        ]

        delivered = _deliver(conn, cfg, failed_sources)
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


def run_daily(cfg: Config) -> bool:
    """Run the once-a-day brief synthesis + delivery cycle. Returns True if it completed OK.

    Invoked by `python -m digest daily` -- a SEPARATE systemd timer on the
    homelab side (not this repo, see CLAUDE.md's Deploy note) fires this once
    a day, independently of the every-3-hours `python -m digest` timer that
    drives `_run`/`_deliver` above. Deliberately synchronous (plain `def`,
    no `asyncio.run` at the call site in `main()`): unlike `_run`, this mode
    never touches Telethon/twikit -- it only reads already-collected digests
    back out of SQLite and drives the same synchronous summarize/translate/
    deliver machinery `_deliver` does, so there is nothing here that needs an
    event loop.

    Pipeline:
    1. Connect/init_db, then run `deliver_pending` FIRST -- exactly like
       `_deliver` does for the window-digest run mode, and reusing that same
       helper rather than duplicating its retry logic -- so a previous
       day's daily brief that got summarized but only partially delivered
       (e.g. site published, Telegram 429'd) is retried before today's new
       brief is even summarized. This also opportunistically retries any
       still-pending WINDOW digest this run happens to see, which is
       harmless (idempotent per channel) even though the every-3-hours job
       already covers that case on its own schedule.
    2. `rows = get_window_digests_since(conn, since)`, `since` being
       `_DAILY_LOOKBACK_WINDOW` (24h) before now -- the day's worth of
       already-curated window briefings to synthesize (kind='window' only;
       see that function's docstring for why a prior daily brief can never
       feed a later one). Empty `rows` (no window digest ran in the last
       24h -- e.g. a very early deploy, or the window job was down all day)
       is logged and returns `all_ok` from step 1 as-is: nothing to brief is
       a normal empty day, not a failure, exactly like `_deliver`'s own
       "no unsummarized items" branch.
    3. `allowed_urls` is the UNION of `get_digest_item_urls` over every
       source window digest -- a daily brief may cite any URL any of its
       source briefings could cite, never a fresh set of raw item URLs (a
       daily brief never sees raw items at all).
    4. `summarize_daily` (digest/daily.py) -- UNLIKE `_deliver`'s own
       `summarize()` call, a `SummarizeError` here is caught, logged, and
       turned into a `False` return rather than propagating further: this
       function's contract (like `_deliver`'s) is "return whether the run
       succeeded", not "raise on failure".
    5. The optional Hungarian translation -- identical `translate_digest`
       call, identical soft-failing contract, as `_deliver` uses for a
       window digest.
    6. `create_digest(..., items=[], kind="daily", item_count=...)` -- a
       daily brief stamps NO items (it consumes digests, not items; the
       empty-items path is exercised and supported, see digest/state.py's
       `create_digest` and its test coverage) but its stored `item_count` is
       explicitly overridden to the SUM of the source window digests' own
       item_counts (see `create_digest`'s docstring for why the override
       parameter exists) -- that sum is what the site's "N items" line and
       the closing-line count sanity actually describe for a daily brief,
       not "0 items" (which `len(items)` would otherwise store) and not any
       single source digest's own count.
    7. `archive()` it, exactly like any digest -- unconditional, not gated
       on any channel's success, identical rationale to `_deliver`'s own
       archiving.
    8. `deliver_channels` with a FRESH `done` map (this digest was just
       created, nothing attempted yet) and this function's own
       `telegram_state` -- shared with step 1's `deliver_pending` call, for
       the identical GUARD-2-circuit-breaker reason `_deliver` shares one
       `TelegramRunState` across its own two `deliver_channels` call
       sites.

    Returns True iff step 1's pending pass AND this run's own fresh delivery
    (when a brief was actually produced) both succeeded -- the AND of the
    same two-part contract `_deliver` upholds for the window-digest run mode.
    """
    conn = connect(cfg.state_db_path)
    try:
        init_db(conn)

        telegram_state = TelegramRunState()
        all_ok = deliver_pending(conn, cfg, telegram_state)

        now = datetime.now(UTC)
        since = now - _DAILY_LOOKBACK_WINDOW
        rows = get_window_digests_since(conn, since.isoformat())
        if not rows:
            logger.info("no window digests in the last 24 hours, nothing to brief today")
            # Exit code stays the sole alert trigger; this line lets a Loki
            # query see this was an empty-day no-op (source_digests: 0)
            # rather than a failed daily brief. Nothing was freshly
            # delivered this run -- both fields fall back to the pending
            # pass's own result, matching `delivered`/`ok`'s meaning below.
            logger.info(
                "run_summary %s",
                json.dumps(
                    {
                        "mode": "daily",
                        "source_digests": 0,
                        "delivered": all_ok,
                        "ok": all_ok,
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

        # Hungarian translation: same optional, soft-failing production step
        # `_deliver` runs for a window digest -- see that call site's own
        # comment for the full rationale, identical here.
        body_md_hu: str | None = None
        if cfg.translate_hu_enabled:
            body_md_hu = translate_digest(
                body_md,
                allowed_urls,
                cfg.translate_model,
                cfg.claude_timeout_seconds,
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
        )
        result = all_ok and ok

        # Exit code stays the sole alert trigger; this line is for Loki
        # queries to see WHICH leg failed -- the pending-retry pass vs this
        # run's own fresh brief -- without a log dive.
        logger.info(
            "run_summary %s",
            json.dumps(
                {
                    "mode": "daily",
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
    # brief (run_daily); no argument (or anything else) keeps today's
    # behavior exactly -- the every-3-hours collect+deliver cycle (_run),
    # unchanged. The scheduling itself (which timer fires which mode, and
    # when) lives entirely outside this repo (see CLAUDE.md's Deploy note);
    # this is just the dispatch a systemd unit's ExecStart invokes into.
    if len(sys.argv) > 1 and sys.argv[1] == "daily":
        ok = run_daily(cfg)
    else:
        ok = asyncio.run(_run(cfg))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
