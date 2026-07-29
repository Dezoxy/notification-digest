import pytest

from digest.config import Config, ConfigError

_REQUIRED_ENV = {
    "TG_API_ID": "12345",
    "TG_API_HASH": "hash",
    "TG_SESSION": "session",
    "TG_CHAT_ALLOWLIST": "123,456",
    "SMTP_HOST": "smtp.mail.me.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "user@example.com",
    "SMTP_PASSWORD": "app-password",
    "DIGEST_FROM": "digest@4rgus.com",
    "DIGEST_TO": "me@toomhorvath.com",
}


def _set_base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


# --- CLAUDE_TIMEOUT_SECONDS (P2 fix: reject non-positive values) ---


def test_claude_timeout_seconds_zero_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_TIMEOUT_SECONDS", "0")

    with pytest.raises(ConfigError, match="CLAUDE_TIMEOUT_SECONDS must be a positive integer"):
        Config.from_env()


def test_claude_timeout_seconds_negative_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_TIMEOUT_SECONDS", "-5")

    with pytest.raises(ConfigError, match="CLAUDE_TIMEOUT_SECONDS must be a positive integer"):
        Config.from_env()


def test_claude_timeout_seconds_non_integer_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_TIMEOUT_SECONDS", "not-a-number")

    with pytest.raises(ConfigError, match="CLAUDE_TIMEOUT_SECONDS must be a positive integer"):
        Config.from_env()


def test_claude_timeout_seconds_valid_positive_value_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_TIMEOUT_SECONDS", "120")

    config = Config.from_env()

    assert config.claude_timeout_seconds == 120


def test_claude_timeout_seconds_unset_falls_back_to_default(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("CLAUDE_TIMEOUT_SECONDS", raising=False)

    config = Config.from_env()

    assert config.claude_timeout_seconds == 300


# --- SMTP_PORT (same non-positive-int guard, trivially consistent to add) ---


def test_smtp_port_zero_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("SMTP_PORT", "0")

    with pytest.raises(ConfigError, match="SMTP_PORT must be a positive integer"):
        Config.from_env()


def test_smtp_port_negative_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("SMTP_PORT", "-1")

    with pytest.raises(ConfigError, match="SMTP_PORT must be a positive integer"):
        Config.from_env()


def test_smtp_port_valid_positive_value_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("SMTP_PORT", "587")

    config = Config.from_env()

    assert config.smtp_port == 587
