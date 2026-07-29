"""Telegram collector: fetch new messages from allowlisted chats via Telethon.

See PLAN.md §4.2 for the full spec. Error handling and cursor/idempotency
rules are load-bearing — read the docstrings on `collect` before changing
this file.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)
from telethon.tl.types import PeerChat
from telethon.utils import resolve_id

from digest.state import Item

logger = logging.getLogger(__name__)

_MAX_MESSAGES_PER_CHAT = 500
_FLOOD_WAIT_AUTO_RETRY_THRESHOLD_SECONDS = 60

# Telethon's "marked id" scheme (see telethon.utils.get_peer_id /
# resolve_id): channel/supergroup marked ids are encoded as
# -(10**12 + channel_id) with channel_id >= 1, i.e. always < -10**12.
# A negative id >= -10**12 (down to -1) is a legacy basic group (PeerChat).
# Positive ids are users.
_CHANNEL_ID_OFFSET = 10**12

_AUTH_ERRORS: tuple[type[Exception], ...] = (
    AuthKeyUnregisteredError,
    AuthKeyDuplicatedError,
    SessionRevokedError,
    UserDeactivatedError,
    UserDeactivatedBanError,
)


class TelegramClientLike(Protocol):
    """Minimal duck-typed surface of telethon.TelegramClient this module needs.

    Kept loose deliberately so tests can inject a fake client without
    depending on real Telethon network objects.
    """

    async def get_entity(self, chat_id: int) -> Any: ...

    async def get_dialogs(self) -> Any: ...

    def iter_messages(
        self, entity: Any, *, limit: int | None = None, min_id: int = 0, reverse: bool = False
    ) -> Any: ...


@dataclass
class CollectResult:
    items: list[Item] = field(default_factory=list)
    cursor_updates: dict[tuple[str, str], str] = field(default_factory=dict)
    failed: bool = False


def is_basic_group(chat_id: int) -> bool:
    """True for legacy "basic group" chat ids.

    Telegram's marked-id scheme (see module-level `_CHANNEL_ID_OFFSET`):
    channel/supergroup marked ids are `-(10**12 + channel_id)` with
    channel_id >= 1, so any marked id < -10**12 is a channel/supergroup.
    A negative id >= -10**12 (i.e. -1 down to -10**12) is a legacy basic
    group. A naive string-prefix check (e.g. "starts with -100")
    misclassifies basic groups whose decimal digits happen to start with
    100 (e.g. -10012345, a basic group, not a supergroup) — this must use
    the numeric range instead.

    Implemented via telethon.utils.resolve_id so classification matches
    Telethon's own peer resolution exactly: resolve_id returns PeerChat
    for legacy basic groups and PeerChannel for supergroups/channels.

    t.me/c/ deep links only address supergroups/channels, so basic groups
    have no working message link and must be filtered out before we ever
    try to collect or link to them.
    """
    if chat_id >= 0:
        return False
    return resolve_id(chat_id)[1] is PeerChat


def build_message_url(chat_id: int, username: str | None, msg_id: int) -> str:
    """Build a t.me deep link for a message.

    Public chats (username set) -> https://t.me/<username>/<msg_id>
    Private supergroup/channel chats -> https://t.me/c/<internal_id>/<msg_id>,
    where internal_id is computed arithmetically from Telegram's marked-id
    scheme: internal_id = -chat_id - 10**12 (e.g. -1001234567890 ->
    1234567890). See `is_basic_group` for the full range explanation.

    Legacy basic groups (negative id, >= -10**12) raise ValueError: t.me/c/
    links only address supergroups/channels, so there is no valid link to
    build. This is defense in depth — `collect()` already filters basic
    groups out via `is_basic_group` before a message url is ever built for
    one.
    """
    if username:
        return f"https://t.me/{username}/{msg_id}"

    if chat_id < -_CHANNEL_ID_OFFSET:
        internal_id = -chat_id - _CHANNEL_ID_OFFSET
        return f"https://t.me/c/{internal_id}/{msg_id}"
    if is_basic_group(chat_id):
        raise ValueError(
            f"cannot build a t.me message link for legacy basic group chat_id={chat_id}: "
            "t.me/c/ links only support supergroups/channels"
        )
    return f"https://t.me/c/{chat_id}/{msg_id}"


def _author_name(msg: Any) -> str | None:
    """Best-effort sender display name with no extra API round-trips."""
    sender = getattr(msg, "sender", None)
    if sender is not None:
        first = getattr(sender, "first_name", None)
        last = getattr(sender, "last_name", None)
        full = " ".join(p for p in (first, last) if p)
        if full:
            return full
        username = getattr(sender, "username", None)
        if username:
            return username
    return None


def _entity_username(entity: Any) -> str | None:
    return getattr(entity, "username", None)


async def _fetch_first_run_cursor(client: TelegramClientLike, entity: Any) -> int | None:
    """No cursor yet: seed from the single latest message, emit no items.

    Returns None when the chat has zero messages; the caller seeds a "0"
    cursor in that case so the chat isn't treated as first-run forever.
    """
    async for msg in client.iter_messages(entity, limit=1):
        return msg.id
    return None


async def _fetch_incremental(
    client: TelegramClientLike, entity: Any, chat_id: int, min_id: int
) -> tuple[list[Item], int | None]:
    """Fetch messages newer than min_id, capped at _MAX_MESSAGES_PER_CHAT.

    Iterates oldest-first (reverse=True) so that when a backlog exceeds the
    cap, the fetched batch is the OLDEST pending messages rather than the
    newest. The cursor then advances only to the newest id actually fetched,
    so the next run resumes right after the gap instead of permanently
    skipping whatever fell outside the cap. Telethon already yields ascending
    ids with reverse=True, so no manual sort is needed. Textless messages are
    skipped from the item list but still count toward the new cursor (their
    ids advance the "seen" watermark).
    """
    username = _entity_username(entity)
    fetched_at = datetime.now(UTC).isoformat()

    raw_msgs = []
    async for msg in client.iter_messages(
        entity, min_id=min_id, limit=_MAX_MESSAGES_PER_CHAT, reverse=True
    ):
        raw_msgs.append(msg)

    if not raw_msgs:
        return [], None

    newest_id = raw_msgs[-1].id

    items: list[Item] = []
    for msg in raw_msgs:
        text = getattr(msg, "message", None)
        if not text:
            continue
        items.append(
            Item(
                source="telegram",
                source_id=f"{chat_id}:{msg.id}",
                chat_id=str(chat_id),
                author=_author_name(msg),
                text=text,
                url=build_message_url(chat_id, username, msg.id),
                fetched_at=fetched_at,
            )
        )
    return items, newest_id


async def _collect_one_chat(
    client: TelegramClientLike, chat_id: int, cursor: str | None, *, retried_flood: bool = False
) -> tuple[list[Item], str | None, bool, bool]:
    """Process a single chat end to end.

    Returns (items, new_cursor_value, auth_or_flood_abort, other_failure).
    `auth_or_flood_abort` means the caller should stop processing further
    chats entirely (auth revoked, or an unrecoverable FloodWait).
    `other_failure` means this one chat failed but siblings should continue.
    """
    try:
        entity = await client.get_entity(chat_id)

        if cursor is None:
            latest_id = await _fetch_first_run_cursor(client, entity)
            logger.info("telegram chat %s: first run, seeded cursor at %s", chat_id, latest_id)
            new_cursor = str(latest_id) if latest_id is not None else "0"
            return [], new_cursor, False, False

        items, newest_id = await _fetch_incremental(client, entity, chat_id, int(cursor))
        logger.info(
            "telegram chat %s: collected %d items, cursor -> %s", chat_id, len(items), newest_id
        )
        new_cursor = str(newest_id) if newest_id is not None else None
        return items, new_cursor, False, False

    except _AUTH_ERRORS as exc:
        logger.warning("telegram auth error on chat %s: %s", chat_id, type(exc).__name__)
        return [], None, True, True

    except FloodWaitError as exc:
        wait_seconds = getattr(exc, "seconds", None)
        short_wait = (
            wait_seconds is not None and wait_seconds <= _FLOOD_WAIT_AUTO_RETRY_THRESHOLD_SECONDS
        )
        if short_wait and not retried_flood:
            logger.warning(
                "telegram flood wait %ss on chat %s, retrying once", wait_seconds, chat_id
            )
            await asyncio.sleep(wait_seconds)
            return await _collect_one_chat(client, chat_id, cursor, retried_flood=True)
        logger.warning("telegram flood wait %ss on chat %s, aborting run", wait_seconds, chat_id)
        return [], None, True, True

    except Exception:
        logger.warning("telegram collection failed for chat %s", chat_id, exc_info=True)
        return [], None, False, True


async def _prefetch_dialogs(client: TelegramClientLike, *, retried_flood: bool = False) -> bool:
    """Populate the entity cache via get_dialogs(). Returns True if the caller should abort.

    Mirrors `_collect_one_chat`'s FloodWait policy rather than treating
    FloodWaitError as an ordinary cache-population failure: without the
    dialog cache, numeric chat ids are generally unresolvable, so silently
    swallowing a FloodWait here would fail the whole run chat-by-chat
    anyway, just less clearly. A short wait (<= the auto-retry threshold)
    is awaited once and retried; a long wait (or one hit after already
    retrying) aborts immediately, like an auth error. Any other exception
    is logged and ignored, since `get_entity` may still succeed for
    cached/public entities and per-chat error handling covers the rest.
    """
    try:
        await client.get_dialogs()
        return False
    except _AUTH_ERRORS as exc:
        logger.warning("telegram auth error populating dialog cache: %s", type(exc).__name__)
        return True
    except FloodWaitError as exc:
        wait_seconds = getattr(exc, "seconds", None)
        short_wait = (
            wait_seconds is not None and wait_seconds <= _FLOOD_WAIT_AUTO_RETRY_THRESHOLD_SECONDS
        )
        if short_wait and not retried_flood:
            logger.warning(
                "telegram flood wait %ss populating dialog cache, retrying once", wait_seconds
            )
            await asyncio.sleep(wait_seconds)
            return await _prefetch_dialogs(client, retried_flood=True)
        logger.warning(
            "telegram flood wait %ss populating dialog cache, aborting run", wait_seconds
        )
        return True
    except Exception:
        logger.warning("telegram get_dialogs failed, continuing anyway", exc_info=True)
        return False


async def collect(
    client: TelegramClientLike,
    chat_ids: Sequence[int],
    cursors: dict[str, str],
) -> CollectResult:
    """Fetch new messages from each allowlisted chat since its cursor.

    First run for a chat (no cursor row): seed the cursor from the latest
    message and emit no items (never a history backfill). A chat with zero
    messages seeds cursor "0" instead of being skipped, so it isn't
    re-treated as first-run (and its eventual first message permanently
    dropped as the seed) on every subsequent run. Otherwise fetch messages
    with id > cursor, up to 500 per chat per run.

    One bad chat never aborts the others: unexpected per-chat exceptions are
    logged and skipped, with `failed=True` set on the result. Auth errors and
    long FloodWaits abort remaining chats (but keep what was already
    collected). A short FloodWait (<= 60s) is awaited once and retried.

    Legacy basic groups (negative chat id, >= -10**12 — see `is_basic_group`)
    are skipped entirely before any network call: t.me/c/ links only address
    supergroups/channels, so a basic group has no valid message link to
    build. The chat is logged as a warning, `failed=True` is set, and
    neither its cursor nor any items are touched.

    Before the per-chat loop, `get_dialogs()` is called once to populate the
    client's entity cache. This matters because in production the client is
    reconstructed from a StringSession, which persists no entity cache:
    `get_entity` on a numeric chat id then fails since Telethon lacks its
    access hash unless the entity was seen this session (e.g. via dialogs).
    If populating the cache hits an auth error, nothing is reachable without
    auth, so the run aborts immediately. A FloodWait during this prefetch
    gets the same policy as a per-chat FloodWait (see `_prefetch_dialogs`):
    a short wait is retried once, a long wait aborts the run. Any other
    exception is logged and ignored: `get_entity` may still succeed for
    cached/public entities, and per-chat error handling covers whatever
    doesn't.
    """
    result = CollectResult()

    if await _prefetch_dialogs(client):
        result.failed = True
        return result

    for chat_id in chat_ids:
        scope = str(chat_id)

        if is_basic_group(chat_id):
            logger.warning(
                "telegram chat %s is a legacy basic group — unsupported (no t.me message "
                "links); convert it to a supergroup or remove it from TG_CHAT_ALLOWLIST",
                chat_id,
            )
            result.failed = True
            continue

        items, new_cursor, abort, failure = await _collect_one_chat(
            client, chat_id, cursors.get(scope)
        )

        if failure:
            result.failed = True

        if new_cursor is not None:
            result.cursor_updates[("telegram", scope)] = new_cursor
        result.items.extend(items)

        if abort:
            break

    return result
