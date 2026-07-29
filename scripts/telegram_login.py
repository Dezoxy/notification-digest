"""One-time interactive Telethon login helper.

Run manually, once, from a local machine (never in the container / on the
VM): `uv run python scripts/telegram_login.py`.

Prompts for the Telegram phone number and login code (and 2FA password if
enabled), then prints the resulting StringSession. That string is a secret —
store it in Azure Key Vault as `digest-tg-session` and never commit it.

This script is intentionally exempt from the "never print secrets" rule that
applies to the rest of the codebase: printing the session string to the
terminal for the owner to copy into Key Vault is its entire purpose.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


def _get_api_id() -> int:
    raw = os.environ.get("TG_API_ID") or input("TG_API_ID (from my.telegram.org): ").strip()
    return int(raw)


def _get_api_hash() -> str:
    return os.environ.get("TG_API_HASH") or input("TG_API_HASH (from my.telegram.org): ").strip()


def main() -> None:
    load_dotenv()

    api_id = _get_api_id()
    api_hash = _get_api_hash()

    with TelegramClient(StringSession(), api_id, api_hash) as client:
        session_string = client.session.save()

        print("\n" + "=" * 70)
        print("SECRET — Telethon StringSession (store as digest-tg-session in")
        print("Azure Key Vault kv-homelab-prod-th; do NOT commit or paste this")
        print("anywhere else):")
        print("=" * 70)
        print(session_string)
        print("=" * 70 + "\n")

        print("Group/channel dialogs (pick TG_CHAT_ALLOWLIST values from these ids):\n")
        print(f"{'id':>16}  title")
        print("-" * 60)
        for dialog in client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                print(f"{dialog.id:>16}  {dialog.name}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
