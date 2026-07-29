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
    x_cookies_path: str | None = None
    x_cookies: str | None = None
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
        smtp_port = _require_positive_int("SMTP_PORT")
        smtp_user = _require_str("SMTP_USER")
        smtp_password = _require_str("SMTP_PASSWORD")
        digest_from = _require_str("DIGEST_FROM")
        digest_to = _require_str("DIGEST_TO")

        state_db_path = os.environ.get("STATE_DB_PATH", "./state.db")
        x_enabled = _parse_bool(os.environ.get("X_ENABLED", "false"))
        x_cookies_path: str | None = None
        x_cookies: str | None = None
        if x_enabled:
            x_cookies_path, x_cookies = _require_exactly_one_x_cookie_source()
        anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
        archive_dir = os.environ.get("ARCHIVE_DIR", "./archive")
        claude_timeout_seconds = _optional_positive_int(
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
            x_cookies_path=x_cookies_path,
            x_cookies=x_cookies,
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


def _require_positive_int(name: str) -> int:
    """Like _require_int, but also rejects zero and negative values.

    Used for settings that get handed straight to something that hangs or
    misbehaves at 0/negative (e.g. SMTP_PORT, CLAUDE_TIMEOUT_SECONDS as
    subprocess.run(timeout=...) -- a non-positive timeout there fires
    instantly on every summarize call, forever retrying the same backlog).
    A non-integer value is folded into the same "must be a positive
    integer" message rather than the generic "must be an integer" one, so
    callers get one consistent error shape for this class of setting.
    """
    raw = _require_str(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


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


def _require_exactly_one_x_cookie_source() -> tuple[str | None, str | None]:
    """When X_ENABLED=true, require exactly one of X_COOKIES_PATH / X_COOKIES.

    twikit is cookie-only in this codebase -- never a fresh username/password
    login on a scheduled run (PLAN.md §4.3) -- so one of these two must
    supply the session. Both set is ambiguous about which one wins; neither
    set leaves the X collector unable to authenticate at all. Either case is
    a ConfigError naming the offending vars, never their values (some of
    these are secrets).
    """
    path = os.environ.get("X_COOKIES_PATH")
    inline = os.environ.get("X_COOKIES")
    path = path if path and path.strip() else None
    inline = inline if inline and inline.strip() else None

    if path is None and inline is None:
        raise ConfigError(
            "exactly one of X_COOKIES_PATH or X_COOKIES is required when X_ENABLED=true"
        )
    if path is not None and inline is not None:
        raise ConfigError("only one of X_COOKIES_PATH or X_COOKIES may be set, not both")
    return path, inline


def _optional_positive_int(name: str, *, default: int) -> int:
    """Read an optional int env var, falling back to `default` if unset/blank.

    Also rejects zero and negative values. An unset/blank value still falls
    back to `default` (assumed positive by the caller) without validation. A
    value that IS set but is 0, negative, or non-integer raises the same
    "must be a positive integer" message -- see _require_positive_int's
    docstring for why 0/negative must not reach the caller
    (subprocess.run(timeout=...) for CLAUDE_TIMEOUT_SECONDS).
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


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
