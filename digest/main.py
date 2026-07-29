"""Entrypoint: python -m digest.main — orchestrates one collection run.

Phase 1 scope: Telegram collection + state persistence only. Summarization
and email (§4.4, §4.5 of PLAN.md) land in Phase 2.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from digest.collectors import telegram as telegram_collector
from digest.config import Config, ConfigError
from digest.state import commit_new_items, connect, get_cursors, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def _run(cfg: Config) -> bool:
    """Run one collection cycle. Returns True if it completed without failure."""
    conn = connect(cfg.state_db_path)
    try:
        init_db(conn)

        cursors = get_cursors(conn, "telegram")

        client = TelegramClient(StringSession(cfg.tg_session), cfg.tg_api_id, cfg.tg_api_hash)
        async with client:
            result = await telegram_collector.collect(client, cfg.tg_chat_allowlist, cursors)

        inserted = commit_new_items(conn, result.items, result.cursor_updates)
        logger.info(
            "collected %d new items (%d inserted), cursors advanced for %d chats",
            len(result.items),
            inserted,
            len(result.cursor_updates),
        )
        return not result.failed
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
