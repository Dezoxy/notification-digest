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
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    digest_from: str
    digest_to: str
    state_db_path: str = "./state.db"
    x_enabled: bool = False
    anthropic_model: str = "claude-opus-5"
    archive_dir: str = "./archive"
    claude_timeout_seconds: int = 300

    @classmethod
    def from_env(cls) -> Config:
        tg_api_id = _require_int("TG_API_ID")
        tg_api_hash = _require_str("TG_API_HASH")
        tg_session = _require_str("TG_SESSION")
        tg_chat_allowlist = _require_int_tuple("TG_CHAT_ALLOWLIST")

        smtp_host = _require_str("SMTP_HOST")
        smtp_port = _require_int("SMTP_PORT")
        smtp_user = _require_str("SMTP_USER")
        smtp_password = _require_str("SMTP_PASSWORD")
        digest_from = _require_str("DIGEST_FROM")
        digest_to = _require_str("DIGEST_TO")

        state_db_path = os.environ.get("STATE_DB_PATH", "./state.db")
        x_enabled = _parse_bool(os.environ.get("X_ENABLED", "false"))
        anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
        archive_dir = os.environ.get("ARCHIVE_DIR", "./archive")
        claude_timeout_seconds = _optional_int(
            "CLAUDE_TIMEOUT_SECONDS", default=300
        )

        return cls(
            tg_api_id=tg_api_id,
            tg_api_hash=tg_api_hash,
            tg_session=tg_session,
            tg_chat_allowlist=tg_chat_allowlist,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            digest_from=digest_from,
            digest_to=digest_to,
            state_db_path=state_db_path,
            x_enabled=x_enabled,
            anthropic_model=anthropic_model,
            archive_dir=archive_dir,
            claude_timeout_seconds=claude_timeout_seconds,
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


def _optional_int(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def claude_subprocess_env() -> dict[str, str]:
    """Build a minimal environment ALLOWLIST for the `claude -p` subprocess.

    This reads os.environ, but it is not configuration reading -- it exists
    so digest/summarize.py's run_claude() does not hand its child process the
    full parent environment (which holds TG_SESSION, TG_API_HASH,
    SMTP_PASSWORD, and friends). `claude -p` runs the full Claude Code agent
    over scraped, untrusted Telegram/X text; a successful prompt injection
    that induces a tool call would otherwise be able to read those secrets
    straight out of its own environment. Only PATH and HOME (needed for the
    CLI binary and its on-disk config to resolve) and CLAUDE_CONFIG_DIR (the
    CLI's own auth/config directory override, if the caller set one) are
    passed through -- everything else, all secrets included, is deliberately
    withheld. USER is included because the CLI's macOS Keychain-backed auth
    fails ("Not logged in") without it -- found by live-testing the scrubbed
    env against the real CLI.
    """
    allowed = ("PATH", "HOME", "USER", "CLAUDE_CONFIG_DIR")
    return {k: os.environ[k] for k in allowed if k in os.environ}
