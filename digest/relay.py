"""The `relay` run mode: verbatim forwarding from public Telegram channels into the hub group.

A sibling of digest/positions.py and digest/patreon.py in shape (its own
run_* in digest/main.py, its own systemd timer, its own cursors-table
namespace) but NOT a summarization lane at all -- no `items` rows, no
`digests` rows, no Claude call. It exists for a different problem than the
rest of this service solves: some public channels the owner wants to watch
(a disaster-news channel is the motivating case) post overwhelmingly in
PHOTOS and VIDEOS with little or no caption text. The main collector
(digest/collectors/telegram.py) only ever extracts `msg.message` -- a
photo-only post has none, so it is silently skipped (see that module's
`_fetch_incremental` docstring) -- and even where there IS caption text, a
bot re-post of just that text throws away the media entirely. Neither
outcome is acceptable for a channel whose entire value is the photo/video
itself.

A native Telegram forward solves this for free: the server re-delivers the
ORIGINAL message -- media, album grouping, formatting and the "Forwarded
from <channel>" header all intact -- with no re-upload and no model in the
loop to summarize, translate, or drop anything. The raw
`functions.messages.ForwardMessagesRequest` is built here directly rather
than going through `TelegramClient.forward_messages`, for two reasons, in
order of weight: that wrapper exposes no `top_msg_id`/`reply_to` at all
(telethon 1.44, telethon/client/messages.py `forward_messages`'s
signature), so it cannot post into a forum topic; and it does not chunk
(see `_FORWARD_CHUNK_SIZE`). This module is deliberately dumb -- it decides
WHICH message ids to forward and WHERE the cursor stands, nothing more.

Idempotency rides on the SAME `cursors` table every other collector uses,
under its own `SOURCE = "relay"` namespace, scoped per chat id exactly like
`("telegram", <chat_id>)` -- see `relay_channel`'s docstring for why the
cursor only advances once an RPC has actually succeeded.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from telethon import utils as telethon_utils
from telethon.errors import ChatForwardsRestrictedError, FloodWaitError
from telethon.tl import functions

from digest import cloud_context

logger = logging.getLogger(__name__)

# Runaway guard only -- mirrors digest/collectors/telegram.py's own
# `_MAX_MESSAGES_PER_CHAT` and exists for the identical reason: a single run
# must never try to pull an unbounded backlog out of one chat. NOT a config
# knob (there is no RELAY_MAX_MESSAGES env var) -- a channel this far behind
# needs the owner's attention, not a bigger cap; the remainder is picked up
# by next hour's run exactly like telegram.py's own cap lets the 6-hourly
# timer drain a backlog over several runs.
_MAX_MESSAGES_PER_CHANNEL = 500

# Telegram's OWN cap on how many ids a single messages.forwardMessages RPC
# may carry in its `id` list -- not a tunable, a server-side limit.
# `TelegramClient.forward_messages` does not chunk on our behalf: it groups
# by source chat (`itertools.groupby` over `chat_id`, telethon/client/
# messages.py ~line 1061) and fires ONE `ForwardMessagesRequest` per
# contiguous same-chat run, with no size cap of its own -- so a >100-message
# backlog handed to that helper as one call would fail server-side. Since
# the raw request is built here anyway (module docstring), the chunking is
# applied before it is ever constructed.
_FORWARD_CHUNK_SIZE = 100

# Same policy, same threshold, as digest/collectors/telegram.py's own
# `_FLOOD_WAIT_AUTO_RETRY_THRESHOLD_SECONDS`: a wait this short is worth
# sleeping through and retrying once; anything longer means the run backs
# off rather than blocking an hourly timer slot on a multi-minute sleep.
_FLOOD_WAIT_AUTO_RETRY_THRESHOLD_SECONDS = 60

# The cursors-table namespace this run mode owns, parallel to "telegram",
# "x", "patreon", etc in digest/state.py's `_KNOWN_SOURCES`. Scoped per chat
# id (`str(chat_id)`), exactly like the window collector's own telegram
# cursors -- but a SEPARATE row, never shared: relay forwards verbatim and
# advances on ITS OWN read of a chat, independent of whatever the window
# collector's `("telegram", <chat_id>)` cursor happens to be doing (most
# relay channels are public broadcast channels the window collector never
# touches at all).
SOURCE = "relay"


class RelayClientLike(Protocol):
    """Minimal duck-typed surface of telethon.TelegramClient this module needs.

    Kept loose deliberately, mirroring digest/collectors/telegram.py's own
    `TelegramClientLike` -- so tests can inject a fake client with no real
    Telethon network object in the loop. `__call__` is the raw RPC invoke
    Telethon's own `TelegramClient` supports directly (`await client(req)`);
    it is what lets this module send a `ForwardMessagesRequest` without a
    matching high-level client method.
    """

    async def get_entity(self, username: str) -> Any: ...

    async def get_input_entity(self, peer: Any) -> Any: ...

    def iter_messages(
        self, entity: Any, *, limit: int | None = None, min_id: int = 0, reverse: bool = False
    ) -> Any: ...

    async def __call__(self, request: Any) -> Any: ...


def deterministic_random_id(chat_id: int, msg_id: int) -> int:
    """Derive `ForwardMessagesRequest.random_id` deterministically from (chat_id, msg_id).

    Telethon's own `ForwardMessagesRequest.__init__` (telethon/tl/functions/
    messages.py) defaults `random_id` to a FRESH `os.urandom(8)` value per
    id when none is given, and Telegram dedupes forwards server-side on that
    value -- so a caller that supplies the SAME random_id for the same
    (chat_id, msg_id) pair twice gets a harmless no-op on the second attempt
    instead of a duplicate post. That is exactly the shape of a crash-retry
    here: the RPC can succeed on Telegram's side while this process dies
    before `advance_cursor` commits, so the next run re-attempts the same
    chunk. Deriving `random_id` from (chat_id, msg_id) rather than trusting
    Telethon's own random default makes that retry idempotent instead of a
    second forwarded copy.

    The limit: Telegram's server-side random_id dedupe window is
    time-bounded, not permanent -- this makes a retry on the NEXT hourly run
    harmless, not a replay months later (e.g. after `state.db` is restored
    from an old backup). Acceptable here, since a crash-retry is exactly the
    next-run case, and strictly better than Telethon's fresh-random default,
    which dedupes nothing.

    blake2b with an 8-byte digest gives exactly 64 bits, matching the TL
    `long` type `random_id` entries are serialized as -- `signed=True`
    covers the full int64 range Telegram accepts (a `long` is signed), the
    same convention Telethon's own default generator uses.
    """
    digest = hashlib.blake2b(f"{chat_id}:{msg_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def chunk_preserving_albums(
    msgs: Sequence[Any], size: int = _FORWARD_CHUNK_SIZE
) -> list[list[Any]]:
    """Split oldest-first messages into <= `size`-id chunks, one RPC's worth each.

    Never splits a run of consecutive messages sharing a non-None
    `grouped_id` (a Telegram album) across two chunks: `TelegramClient.
    forward_messages`'s `as_album` argument is a deprecated no-op (see the
    module docstring), so today request boundaries are the ONLY thing that
    keeps an album's photos grouped together on the receiving end -- forward
    two halves of one album in two separate requests and the hub topic shows
    two broken partial albums instead of one whole one.

    When an album would straddle a chunk boundary, the chunk in progress is
    closed BEFORE the album starts (rather than splitting the album, or
    growing the chunk past `size`), and the whole album opens the next
    chunk. This can make one chunk larger than `size` only in the
    pathological case of a single album bigger than `size` itself -- real
    Telegram albums cap at 10 items, far under `_FORWARD_CHUNK_SIZE`, so
    that never happens in practice; the rule still holds because "never
    split an album" is the invariant this function exists to guarantee, and
    it must win even in a case this unlikely.

    Pure function, no I/O -- every message here is already fetched.
    """
    chunks: list[list[Any]] = []
    current: list[Any] = []
    i = 0
    n = len(msgs)
    while i < n:
        msg = msgs[i]
        grouped_id = getattr(msg, "grouped_id", None)
        if grouped_id is None:
            if len(current) >= size:
                chunks.append(current)
                current = []
            current.append(msg)
            i += 1
            continue

        # Collect the full run of consecutive messages sharing this
        # grouped_id -- an album's messages arrive back to back in Telethon's
        # own iteration order, so a single forward scan finds the whole run.
        run = [msg]
        j = i + 1
        while j < n and getattr(msgs[j], "grouped_id", None) == grouped_id:
            run.append(msgs[j])
            j += 1

        if current and len(current) + len(run) > size:
            chunks.append(current)
            current = []
        current.extend(run)
        i = j

    if current:
        chunks.append(current)
    return chunks


def is_forwardable(msg: Any) -> bool:
    """False for service messages (member joined/left, pinned, etc), True otherwise.

    A `MessageService` carries a non-None `.action`; an ordinary `Message`'s
    is always None. Service messages still ADVANCE the cursor (see
    `relay_channel`) -- they were genuinely seen, there is just nothing on
    them worth forwarding into the hub topic.
    """
    return getattr(msg, "action", None) is None


async def _forward_one_chunk(
    client: RelayClientLike,
    entity: Any,
    hub_peer: Any,
    top_msg_id: int | None,
    chat_id: int,
    chunk: Sequence[Any],
    username: str,
    *,
    retried_flood: bool = False,
) -> bool:
    """Forward one chunk's forwardable ids in a single RPC. Returns True iff it may advance.

    A chunk made up ENTIRELY of service messages (`is_forwardable` false for
    all of them) sends no RPC at all and reports success trivially -- there
    is nothing to forward, but nothing failed either.

    FloodWait policy mirrors digest/collectors/telegram.py's own
    `_collect_one_chat` exactly: a wait <=
    `_FLOOD_WAIT_AUTO_RETRY_THRESHOLD_SECONDS` is slept and this SAME chunk
    retried once (via recursion, like that function); a longer wait, a
    repeat flood, a `ChatForwardsRestrictedError` (the channel owner has
    disabled forwarding -- no retry could ever help), or any other exception
    aborts just this chunk. None of these raise past this function: the
    caller (`relay_channel`) decides what "this chunk failed" means for the
    channel's cursor.
    """
    ids = [m.id for m in chunk if is_forwardable(m)]
    if not ids:
        return True

    req = functions.messages.ForwardMessagesRequest(
        from_peer=entity,
        id=ids,
        to_peer=hub_peer,
        random_id=[deterministic_random_id(chat_id, msg_id) for msg_id in ids],
        top_msg_id=top_msg_id,
    )
    try:
        cloud_context.guard()
        await client(req)
        return True
    except FloodWaitError as exc:
        wait_seconds = getattr(exc, "seconds", None)
        short_wait = (
            wait_seconds is not None and wait_seconds <= _FLOOD_WAIT_AUTO_RETRY_THRESHOLD_SECONDS
        )
        if short_wait and not retried_flood:
            logger.warning(
                "relay: flood wait %ss forwarding channel %s, retrying once",
                wait_seconds,
                username,
            )
            await asyncio.sleep(wait_seconds)
            return await _forward_one_chunk(
                client,
                entity,
                hub_peer,
                top_msg_id,
                chat_id,
                chunk,
                username,
                retried_flood=True,
            )
        logger.warning(
            "relay: flood wait %ss forwarding channel %s, aborting this chunk",
            wait_seconds,
            username,
        )
        return False
    except ChatForwardsRestrictedError:
        logger.warning(
            "relay: channel %s has forwarding disabled -- remove it from RELAY_TG_CHANNELS",
            username,
        )
        return False
    except Exception:
        logger.warning("relay: forwarding a chunk for channel %s failed", username, exc_info=True)
        return False


async def relay_channel(
    client: RelayClientLike,
    username: str,
    hub_peer: Any,
    top_msg_id: int | None,
    cursors: dict[str, str],
    advance_cursor: Callable[[int, int], None],
) -> tuple[bool, int]:
    """Forward every message newer than this channel's cursor into the hub topic.

    Returns `(ok, forwarded_count)`.

    Takes the WHOLE `cursors` map (`{scope: last_seen_id}`, `SOURCE`'s own
    namespace), not a single already-looked-up cursor value, because the
    lookup key is `str(chat_id)` -- and the chat id is only known AFTER
    `get_entity(username)` resolves inside this function. The caller
    (digest/main.py's `_relay_all`) configures channels by USERNAME
    (`RELAY_TG_CHANNELS`), so it cannot pre-resolve that key itself without
    duplicating the resolution this function already has to do.

    `advance_cursor(chat_id, last_seen_msg_id)` is a plain synchronous
    callback, not a return value the caller commits once at the end: it is
    called once per CHUNK (see step 4 below), immediately after that
    chunk's forward RPC succeeds, and the caller commits it to the `cursors`
    table right there (digest/main.py's `_relay_all` wires it to
    `commit_new_items(conn, [], {(SOURCE, str(chat_id)): str(last_id)})`).
    That means a crash between chunks loses at most one chunk's worth of
    progress -- the next run resumes from the last chunk that actually
    committed -- and `deterministic_random_id` (see its own docstring) makes
    re-forwarding that one chunk on the retry harmless rather than a
    duplicate post.

    One unresolvable or misbehaving channel never aborts the run for its
    siblings -- `digest/main.py`'s `_relay_all` calls this once per
    configured channel and keeps going regardless of what any single call
    returns, mirroring `_collect_positions_chats`'s own "one bad channel
    must not cost the others" rule.

    Steps:
    1. Resolve `username` via `get_entity`. ANY exception here (renamed,
       deleted, or newly-private channel; a typo) is logged by NAME --
       a public channel username is not a secret -- and returns
       `(False, 0)` without touching the cursor.
    2. `chat_id = telethon_utils.get_peer_id(entity)` -- the marked id this
       channel's cursor row is scoped under.
    3. No cursor yet (`cursors.get(str(chat_id))` is None): first run. Seed
       the cursor from the single latest message id (0 if the channel has
       zero messages, mirroring digest/collectors/telegram.py's own
       `_fetch_first_run_cursor` contract), forward NOTHING, and return
       `(True, 0)`. Never backfills -- a freshly added channel does not dump
       its entire history into the hub topic the moment it is configured.
    4. Otherwise fetch every message with id > cursor, oldest-first
       (`reverse=True`, capped at `_MAX_MESSAGES_PER_CHANNEL`), split it into
       `chunk_preserving_albums` chunks, and forward each chunk in turn via
       `_forward_one_chunk`. The cursor advances to that chunk's LAST
       message id -- including any skipped service message id, so a service
       message can never be re-seen as "new" on the next run -- but ONLY
       after that chunk's RPC (or no-op, for an all-service chunk) actually
       succeeded. The first chunk that fails stops the loop right there:
       this function returns `(False, forwarded_so_far)`, and every chunk
       after the failed one is left for the next run to pick up from the
       cursor's last successful position.
    """
    try:
        entity = await client.get_entity(username)
    except Exception as exc:
        logger.warning(
            "relay: cannot resolve telegram channel %s: %s", username, type(exc).__name__
        )
        return False, 0

    chat_id = telethon_utils.get_peer_id(entity)
    cursor = cursors.get(str(chat_id))

    if cursor is None:
        latest_id: int | None = None
        async for msg in client.iter_messages(entity, limit=1):
            latest_id = msg.id
            break
        seeded = latest_id if latest_id is not None else 0
        advance_cursor(chat_id, seeded)
        logger.info(
            "relay: channel %s (chat %d): first run, seeded cursor at %d",
            username,
            chat_id,
            seeded,
        )
        return True, 0

    msgs = [
        m
        async for m in client.iter_messages(
            entity, min_id=int(cursor), limit=_MAX_MESSAGES_PER_CHANNEL, reverse=True
        )
    ]
    if not msgs:
        logger.info("relay: channel %s (chat %d): no new messages", username, chat_id)
        return True, 0

    forwarded = 0
    for chunk in chunk_preserving_albums(msgs):
        if not await _forward_one_chunk(
            client, entity, hub_peer, top_msg_id, chat_id, chunk, username
        ):
            return False, forwarded
        forwarded += sum(1 for m in chunk if is_forwardable(m))
        advance_cursor(chat_id, chunk[-1].id)

    logger.info(
        "relay: channel %s (chat %d): forwarded %d message(s), cursor -> %d",
        username,
        chat_id,
        forwarded,
        msgs[-1].id,
    )
    return True, forwarded
