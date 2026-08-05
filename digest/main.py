"""Entrypoint: python -m digest.main — orchestrates one collection run.

Phase 2: Telegram collection + state persistence, then summarization
(§4.4) and email delivery (§4.5) of PLAN.md.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from digest.collectors import polymarket as polymarket_collector
from digest.collectors import rss as rss_collector
from digest.collectors import telegram as telegram_collector
from digest.collectors import x as x_collector
from digest.collectors.polymarket import PolymarketCollectResult
from digest.collectors.telegram import CollectResult
from digest.config import Config, ConfigError
from digest.emailer import archive, render_body_html, send_digest
from digest.publish import publish_to_site, send_telegram_tldr
from digest.state import (
    commit_new_items,
    connect,
    count_unsummarized_items,
    create_digest,
    get_cursors,
    get_digest_item_urls,
    get_pending_digests,
    get_polymarket_probs,
    get_recent_digests,
    get_unsummarized_items,
    init_db,
    mark_digest_sent,
    mark_digest_site_published,
    mark_digest_telegram_sent,
)
from digest.summarize import (
    _MAX_PROMPT_BYTES,
    SummarizeError,
    format_recent_coverage,
    select_items_for_prompt,
    summarize,
)

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


def _digest_meta(conn: sqlite3.Connection, digest_id: int) -> tuple[int, str]:
    """Fetch (item_count, created_at) for an existing digest row.

    Used only by the pending-resend path in `_deliver`: `get_pending_digests`
    returns just `(digest_id, body_md, done)` (see its docstring), so a
    pending digest's item_count/created_at -- needed for the email subject
    and the site/Telegram channels' payloads respectively -- have to be
    read back separately. The fresh-digest path never calls this: it already
    has both values on hand (`len(items)` and the row `create_digest` just
    inserted).
    """
    row = conn.execute(
        "SELECT item_count, created_at FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    return row[0], row[1]


def _deliver_email(
    conn: sqlite3.Connection,
    cfg: Config,
    digest_id: int,
    body_md: str,
    item_count: int,
    allowed_urls: Collection[str],
) -> bool:
    """Render+send the email channel for one digest. Returns True on success.

    Subject and the HTML masthead's `generated_at_label` are both rendered
    from THIS RUN's own clock (`datetime.now(UTC)`), not the digest's
    `created_at` -- exactly like the pre-multi-channel `_send_and_finalize`
    did, including for a pending resend: the reader should see when the
    email actually went out, not when the digest was originally summarized.
    Both are converted to Europe/Budapest at this render-time boundary per
    CLAUDE.md (storage stays UTC).

    On failure the digest row is left `email_sent = 0` (the caller never
    marks this channel), so the next run's `get_pending_digests` pass
    retries EXACTLY this channel -- site/Telegram, if also enabled, are
    unaffected either way (see `_deliver_channels`). Only the exception's
    type name is logged, never its message: SMTP errors can echo
    credentials or message content.
    """
    now_local = datetime.now(UTC).astimezone(ZoneInfo("Europe/Budapest"))
    subject = f"digest: {item_count} items · {now_local:%Y-%m-%d %H:%M}"
    # %-d (no leading zero) is a glibc/BSD strftime extension, not POSIX --
    # but it's the same extension on both macOS (BSD libc) and the Linux
    # container this actually deploys to (glibc), so it's safe here despite
    # not being portable in general.
    generated_at_label = f"{now_local:%a, %b %-d · %H:%M}"
    try:
        send_digest(
            cfg.smtp_host,
            cfg.smtp_port,
            cfg.smtp_user,
            cfg.smtp_password,
            cfg.digest_from,
            cfg.digest_from_name,
            cfg.digest_to,
            subject,
            body_md,
            allowed_urls,
            generated_at_label,
        )
    except Exception as exc:
        logger.error("email delivery failed for digest %d: %s", digest_id, type(exc).__name__)
        return False

    mark_digest_sent(conn, digest_id)
    return True


def _deliver_site(
    conn: sqlite3.Connection,
    cfg: Config,
    digest_id: int,
    body_md: str,
    item_count: int,
    created_at: str,
    allowed_urls: Collection[str],
) -> bool:
    """Render+publish the site channel for one digest. Returns True on success.

    `render_body_html` is called here (not passed in) so the site channel
    always gets HTML rendered against ITS OWN correctly-scoped
    `allowed_urls` -- the digest's own stamped item URLs, identical to what
    the email channel's HTML part uses (see digest/emailer.py's
    `render_body_html` docstring: both channels must show the same
    sanitized content, byte-for-byte).

    On failure the digest row is left `site_published = 0`, so the next
    run's `get_pending_digests` pass retries exactly this channel -- and,
    per `_deliver_channels`'s ordering contract, Telegram is skipped THIS
    run for this digest too (it links to the site page this call just
    failed to publish). Only the exception's type name is logged, never its
    message or the request URL/response body (digest/publish.py's
    `publish_to_site` never logs either itself).
    """
    body_html = render_body_html(body_md, allowed_urls)
    try:
        publish_to_site(
            digest_id,
            body_md,
            body_html,
            created_at,
            item_count,
            cfg.site_publish_url,
            cfg.site_ingest_key,
        )
    except Exception as exc:
        logger.error("site publish failed for digest %d: %s", digest_id, type(exc).__name__)
        return False

    mark_digest_site_published(conn, digest_id)
    return True


def _deliver_telegram(
    conn: sqlite3.Connection, cfg: Config, digest_id: int, body_md: str, created_at: str
) -> bool:
    """Send the Telegram TL;DR channel for one digest. Returns True on success.

    On failure the digest row is left `telegram_sent = 0`, retried by a
    later run's `get_pending_digests` pass exactly like the other two
    channels. `send_telegram_tldr` already guarantees its own raised message
    (and any log line it emits) never includes the response body or the
    bot-token-bearing request URL -- this function additionally only logs
    the exception's TYPE NAME, never `str(exc)`, as one more layer against
    that secret ever reaching a log line.
    """
    try:
        send_telegram_tldr(
            digest_id,
            body_md,
            created_at,
            cfg.telegram_notify_bot_token,
            cfg.telegram_notify_chat_id,
            cfg.telegram_notify_thread_id,
            cfg.site_public_base,
        )
    except Exception as exc:
        logger.error("telegram notify failed for digest %d: %s", digest_id, type(exc).__name__)
        return False

    mark_digest_telegram_sent(conn, digest_id)
    return True


def _deliver_channels(
    conn: sqlite3.Connection,
    cfg: Config,
    digest_id: int,
    body_md: str,
    item_count: int,
    created_at: str,
    done: dict[str, bool],
) -> bool:
    """Attempt every ENABLED, not-yet-done channel for one digest, independently.

    `done` is the digest's current per-channel completion state (from
    `get_pending_digests`, or `{"email": False, "site": False, "telegram":
    False}` for a brand-new digest -- see `_deliver`). A channel that is
    DISABLED (per `cfg`) or already `done` is skipped entirely: it is never
    attempted, never logged as a failure, and never counted against this
    digest's overall success.

    Each attempted channel gets its own try/except (`_deliver_email`/
    `_deliver_site`/`_deliver_telegram`) and, on success, marks its OWN flag
    immediately via its own `mark_digest_*` call -- each of which is its own
    tiny commit (see digest/state.py). This is what makes the three channels
    independent: email failing can never roll back a site publish that
    already succeeded moments earlier in this same call, because that
    success was already durably committed before email was even attempted.
    A failure only logs (exception type name only, never a message that
    could carry credentials or a bot-token-bearing URL) and this function
    moves on to the next channel.

    Ordering: site is attempted BEFORE Telegram. This is a real dependency,
    not an arbitrary choice -- the Telegram message links to the site's own
    page for this digest (`f"{public_base}/d/{digest_id}"`,
    digest/publish.py's `send_telegram_tldr`). If the site channel is
    ENABLED for this run and still NOT done after its own attempt above
    (either it just failed, or it was already pending from an earlier run
    and this call didn't even reach it because it's disabled -- see below),
    Telegram is skipped for this digest THIS RUN rather than sending a link
    to a page that doesn't exist yet; it will be retried automatically next
    run once site publish succeeds. This skip does NOT apply when the site
    channel is disabled entirely (`cfg.site_publish_url` unset) -- an
    intentionally site-less deployment must not have Telegram permanently
    blocked by a channel it never intended to use.

    Returns True iff every ENABLED channel for this digest is done (already
    was, or just succeeded) by the time this returns -- a disabled channel
    trivially counts as "done" for this purpose, since there is nothing left
    for it to accomplish.
    """
    email_enabled = cfg.email_enabled
    site_enabled = cfg.site_publish_url is not None
    telegram_enabled = cfg.telegram_notify_bot_token is not None

    email_done = done["email"] or not email_enabled
    site_done = done["site"] or not site_enabled
    telegram_done = done["telegram"] or not telegram_enabled

    allowed_urls = get_digest_item_urls(conn, digest_id)

    if email_enabled and not email_done:
        email_done = _deliver_email(conn, cfg, digest_id, body_md, item_count, allowed_urls)

    if site_enabled and not site_done:
        site_done = _deliver_site(
            conn, cfg, digest_id, body_md, item_count, created_at, allowed_urls
        )

    if telegram_enabled and not telegram_done:
        if site_enabled and not site_done:
            logger.info(
                "digest %d: skipping telegram this run, site publish not done", digest_id
            )
        else:
            telegram_done = _deliver_telegram(conn, cfg, digest_id, body_md, created_at)

    return email_done and site_done and telegram_done


def _deliver(
    conn: sqlite3.Connection, cfg: Config, failed_sources: list[str]
) -> bool:
    """Post-collection delivery: retry every pending digest, then summarize+deliver new items.

    (a) Every digest with at least one ENABLED channel still undelivered
        (`get_pending_digests`, oldest first -- crash/API failure on a
        previous run) has `_deliver_channels` retried for it. Unlike the
        pre-multi-channel version of this function, a failure here does NOT
        short-circuit the rest of this function: channels are independent
        by design (a broken SMTP path must not stop a healthy site/Telegram
        channel from delivering a DIFFERENT pending digest, or this run's
        own freshly summarized one), so every pending digest is attempted
        and the overall result is the AND of every attempt.
    (b) No unsummarized items -- nothing left to send, this is a normal
        empty-window run (or the pending pass already covered everything).
    (c) Otherwise: summarize with the CURRENT run's failed_sources, durably
        record the digest and archive it (BEFORE attempting any channel --
        see below), then attempt all its channels.

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
    """
    email_enabled = cfg.email_enabled
    site_enabled = cfg.site_publish_url is not None
    telegram_enabled = cfg.telegram_notify_bot_token is not None

    all_ok = True
    pending = get_pending_digests(conn, email_enabled, site_enabled, telegram_enabled)
    for digest_id, body_md, done in pending:
        logger.info("retrying delivery of digest %d", digest_id)
        item_count, created_at = _digest_meta(conn, digest_id)
        ok = _deliver_channels(conn, cfg, digest_id, body_md, item_count, created_at, done)
        all_ok = all_ok and ok

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

    digest_id = create_digest(conn, body_md, items)
    archive(body_md, cfg.archive_dir, digest_id)

    item_count, created_at = _digest_meta(conn, digest_id)
    done = {"email": False, "site": False, "telegram": False}
    ok = _deliver_channels(conn, cfg, digest_id, body_md, item_count, created_at, done)
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
                tg_result = telegram_collector.CollectResult(failed=True)
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

        items = tg_result.items + x_result.items + news_result.items + polymarket_result.items
        # news never contributes cursor_updates (it has no cursor axis, see
        # digest/collectors/rss.py's module docstring) -- merging its
        # (always-empty) dict in here anyway keeps this line generic over
        # every collector rather than special-casing the one with nothing
        # to add. polymarket ALSO has no cursor axis (its own state lives in
        # the polymarket_probs table, see digest/collectors/polymarket.py's
        # module docstring), but unlike news it doesn't even have a
        # cursor_updates field on its result type -- PolymarketCollectResult
        # is a distinct type carrying `prob_updates` instead (handled below,
        # not here).
        cursor_updates = {
            **tg_result.cursor_updates,
            **x_result.cursor_updates,
            **news_result.cursor_updates,
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
            )
            if failed
        ]

        delivered = _deliver(conn, cfg, failed_sources)
        return delivered and not failed_sources
    finally:
        conn.close()


def main() -> None:
    load_dotenv()

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    ok = asyncio.run(_run(cfg))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
