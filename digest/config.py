"""Environment-variable loading and validation.

This module is the ONLY place in the codebase allowed to read os.environ /
os.getenv. Every other module receives configuration values passed in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed.

    Messages must name the offending variable but must never echo its value
    — some of these variables are secrets.
    """


@dataclass(frozen=True)
class Config:
    tg_api_id: int
    tg_api_hash: str
    tg_session: str
    tg_chat_allowlist: tuple[int, ...]
    state_db_path: str = "./state.db"
    x_enabled: bool = False

    @classmethod
    def from_env(cls) -> Config:
        tg_api_id = _require_int("TG_API_ID")
        tg_api_hash = _require_str("TG_API_HASH")
        tg_session = _require_str("TG_SESSION")
        tg_chat_allowlist = _require_int_tuple("TG_CHAT_ALLOWLIST")

        state_db_path = os.environ.get("STATE_DB_PATH", "./state.db")
        x_enabled = _parse_bool(os.environ.get("X_ENABLED", "false"))

        return cls(
            tg_api_id=tg_api_id,
            tg_api_hash=tg_api_hash,
            tg_session=tg_session,
            tg_chat_allowlist=tg_chat_allowlist,
            state_db_path=state_db_path,
            x_enabled=x_enabled,
        )


def _require_str(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ConfigError(f"{name} is required but not set")
    return value


def _require_int(name: str) -> int:
    raw = _require_str(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _require_int_tuple(name: str) -> tuple[int, ...]:
    raw = _require_str(name)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ConfigError(f"{name} must contain at least one chat id")
    try:
        return tuple(int(p) for p in parts)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a comma-separated list of integers") from exc


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("true", "1")
