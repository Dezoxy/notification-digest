"""Entrypoint: python -m digest.main — orchestrates one collection run.

Phase 2: Telegram collection + state persistence, then summarization
(§4.4) and email delivery (§4.5) of PLAN.md.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from digest.collectors import telegram as telegram_collector
from digest.collectors.telegram import CollectResult
from digest.config import Config, ConfigError
from digest.emailer import archive, send_digest
from digest.state import (
    commit_new_items,
    connect,
    count_unsummarized_items,
    create_digest,
    get_cursors,
    get_digest_item_urls,
    get_pending_digest,
    get_unsummarized_items,
    init_db,
    mark_digest_sent,
)
from digest.summarize import _MAX_PROMPT_BYTES, SummarizeError, select_items_for_prompt, summarize

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


def _deliver(conn: sqlite3.Connection, cfg: Config, result: CollectResult) -> bool:
    """Post-collection delivery: retry any pending send, then summarize+send new items.

    (a) A pending unsent digest (crash/SMTP failure on a previous run) is
        resent as-is -- summarize is never called twice for the same items.
        If that resend FAILS, we return False immediately without touching
        this run's own items: there is no point attempting a second send on
        a broken SMTP path, and the freshly collected items remain
        unsummarized for a later run to pick up. If it SUCCEEDS, we do NOT
        return -- we fall through to the normal path below so this run's
        own collection (and its own `result.failed` state) still gets
        summarized and sent as a second email in the same run. Without this
        fallthrough, this run's items would sit unsummarized until a later
        run summarizes them with a fresh (possibly healthy) failed_sources,
        silently dropping the partial-collection warning this run should
        have carried.
    (b) No unsummarized items -- nothing left to send, this is a normal
        empty-window run (or the pending resend already covered everything).
    (c) Otherwise: summarize with the CURRENT run's failed_sources, durably
        record the digest (BEFORE sending, so a crash after this point
        retries the send next run instead of re-summarizing), send, mark
        sent, archive.

    Returns True if delivery succeeded or wasn't needed. Deliberately does
    NOT factor in `result.failed` -- the caller combines this with the
    collector's own failure flag, because a Telegram collector failure must
    surface as a non-zero exit (the sole signal for the Loki alert on
    digest.service) even on a run that sends no email at all, e.g. zero
    collected items with no pending/unsummarized backlog to fall back on.
    """
    pending = get_pending_digest(conn)
    if pending is not None:
        digest_id, body_md = pending
        logger.info("retrying send of digest %d", digest_id)
        item_count = conn.execute(
            "SELECT item_count FROM digests WHERE id = ?", (digest_id,)
        ).fetchone()[0]
        if not _send_and_finalize(conn, cfg, digest_id, body_md, item_count):
            return False
        # Pending resend succeeded -- fall through so this run's own
        # collection still gets summarized and sent, instead of discarding
        # this run's failed_sources state.

    items = get_unsummarized_items(conn, limit=_MAX_ITEMS_PER_DIGEST)
    if not items:
        logger.info("no unsummarized items, nothing to send")
        return True

    failed_sources = ["telegram"] if result.failed else []

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
    # and the stamped set identical by construction.
    items = select_items_for_prompt(items, failed_sources, _MAX_PROMPT_BYTES)

    try:
        body_md = summarize(items, failed_sources, cfg.anthropic_model, cfg.claude_timeout_seconds)
    except SummarizeError as exc:
        logger.error("summarization failed: %s", exc)
        return False

    digest_id = create_digest(conn, body_md, items)
    if not _send_and_finalize(conn, cfg, digest_id, body_md, len(items)):
        return False

    # One Opus call per run keeps cost and runtime bounded -- do NOT loop
    # summarize here even if a remainder is left; the 3-hourly timer is the
    # drain loop that picks up the rest on its next invocation.
    remaining = count_unsummarized_items(conn)
    if remaining:
        logger.info("%d unsummarized items remain, will ship in the next digest", remaining)
    return True


def _send_and_finalize(
    conn: sqlite3.Connection, cfg: Config, digest_id: int, body_md: str, item_count: int
) -> bool:
    """Send the digest, mark it sent, and archive it.

    On SMTP failure the digest row is deliberately left email_sent=0 so the
    next run's `get_pending_digest` branch retries the send (PLAN.md §4.1).
    Email subject time is rendered in Europe/Budapest per CLAUDE.md (storage
    stays UTC; only render/email time converts).

    The HTML link-provenance allowlist passed to send_digest is fetched
    fresh from the digest's own stamped items (get_digest_item_urls), not
    threaded through from an in-memory Item list -- that's what makes this
    work identically for both callers of _send_and_finalize: the
    fresh-digest path (create_digest just stamped these items in this same
    call to _deliver) and the pending-resend path (the items were stamped
    by create_digest in a PREVIOUS run; this run never built an Item list
    at all, only read digest_id/body_md back off the `digests` table).
    """
    now_local = datetime.now(UTC).astimezone(ZoneInfo("Europe/Budapest"))
    subject = f"digest: {item_count} items · {now_local:%Y-%m-%d %H:%M}"
    allowed_urls = get_digest_item_urls(conn, digest_id)
    try:
        send_digest(
            cfg.smtp_host,
            cfg.smtp_port,
            cfg.smtp_user,
            cfg.smtp_password,
            cfg.digest_from,
            cfg.digest_to,
            subject,
            body_md,
            allowed_urls,
        )
    except Exception as exc:
        logger.error("email send failed for digest %d: %s", digest_id, type(exc).__name__)
        return False

    mark_digest_sent(conn, digest_id)
    archive(body_md, cfg.archive_dir, digest_id)
    return True


async def _run(cfg: Config) -> bool:
    """Run one collection + delivery cycle. Returns True if it completed without failure."""
    conn = connect(cfg.state_db_path)
    try:
        init_db(conn)

        cursors = get_cursors(conn, "telegram")

        client = TelegramClient(StringSession(cfg.tg_session), cfg.tg_api_id, cfg.tg_api_hash)
        try:
            if not await _client_ready(client):
                logger.warning("telegram session not authorized / connect failed")
                result = telegram_collector.CollectResult(failed=True)
            else:
                result = await telegram_collector.collect(client, cfg.tg_chat_allowlist, cursors)
        finally:
            if client.is_connected():
                await client.disconnect()

        inserted = commit_new_items(conn, result.items, result.cursor_updates)
        logger.info(
            "collected %d new items (%d inserted), cursors advanced for %d chats",
            len(result.items),
            inserted,
            len(result.cursor_updates),
        )

        delivered = _deliver(conn, cfg, result)
        return delivered and not result.failed
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
