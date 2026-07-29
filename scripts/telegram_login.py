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
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

# Running a script inside scripts/ puts scripts/ on sys.path -- NOT the repo
# root -- and `[tool.uv] package = false` (pyproject.toml) means `digest` is
# never installed into the venv either. pytest only imports `digest` because
# it sets `pythonpath = ["."]`; a directly-run script gets no such help, so
# `uv run python scripts/telegram_login.py` fails at the import below without
# this. Add the repo root explicitly rather than requiring callers to
# remember PYTHONPATH=.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digest.collectors.telegram import is_basic_group  # noqa: E402


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
                title = dialog.name
                if is_basic_group(dialog.id):
                    title += " (basic group — unsupported; convert to supergroup)"
                print(f"{dialog.id:>16}  {title}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
