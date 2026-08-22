"""List a forum group's topics and their thread ids, for TELEGRAM_*_THREAD_ID.

A sibling of scripts/telegram_login.py: a one-off, interactive operator tool,
not part of any scheduled run. Reads the same TG_API_ID/TG_API_HASH/TG_SESSION
the collector uses, connects read-only, and prints each topic's id.

    uv run python scripts/list_telegram_topics.py            # every forum group
    uv run python scripts/list_telegram_topics.py "toom"      # one group

The argument filters on the GROUP name, and each matching group's topics are
printed beneath it with their ids. With no argument every forum group the
session can see is listed, so neither name has to be guessed. Nothing is
written and no message is sent.

Why a script rather than a note in the README: a Telegram forum topic's thread
id is the id of the topic's own first service message, which the UI never shows
directly. The usual manual recipe -- post a message in the topic, copy its
link, read the middle number out of `t.me/c/<chat>/<thread>/<msg>` -- requires
posting into the topic to learn its id, which is both awkward and visible to
anyone else in the group.
"""

from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.utils import get_peer_id


async def main() -> int:
    load_dotenv()
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    session = os.environ.get("TG_SESSION", "").strip()
    if not (api_id and api_hash and session):
        print("TG_API_ID, TG_API_HASH and TG_SESSION must be set (.env)", file=sys.stderr)
        return 2

    wanted = " ".join(sys.argv[1:]).strip().casefold()

    client = TelegramClient(StringSession(session), int(api_id), api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("session is not authorized -- re-run scripts/telegram_login.py", file=sys.stderr)
            return 1

        forums = [
            dialog
            for dialog in await client.get_dialogs()
            if getattr(dialog.entity, "forum", False)
        ]
        if not forums:
            print("no forum-enabled groups visible to this session")
            return 0

        matched = [d for d in forums if not wanted or wanted in (d.name or "").casefold()]
        if not matched:
            print(f"no forum group matching {wanted!r}. Visible forum groups:")
            for dialog in forums:
                print(f"  - {dialog.name}")
            return 1

        for dialog in matched:
            chat_id = get_peer_id(dialog.entity)
            print(f"\n{dialog.name}  (TELEGRAM_NOTIFY_CHAT_ID={chat_id})")
            result = await client(
                GetForumTopicsRequest(
                    peer=dialog.entity,
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100,
                )
            )
            for topic in result.topics:
                # The General topic is id 1 and is the group root; the app's
                # own convention for "root" is thread id 0, not 1.
                note = "  <- group root, use 0" if topic.id == 1 else ""
                print(f"  {topic.id:>8}  {topic.title}{note}")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
