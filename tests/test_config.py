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


# --- X_ENABLED / X_COOKIES_PATH / X_COOKIES (Phase 3) ---


def test_x_enabled_false_does_not_require_cookie_vars(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("X_ENABLED", "false")
    monkeypatch.delenv("X_COOKIES_PATH", raising=False)
    monkeypatch.delenv("X_COOKIES", raising=False)

    config = Config.from_env()

    assert config.x_enabled is False
    assert config.x_cookies_path is None
    assert config.x_cookies is None


def test_x_enabled_true_with_neither_cookie_var_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("X_ENABLED", "true")
    monkeypatch.delenv("X_COOKIES_PATH", raising=False)
    monkeypatch.delenv("X_COOKIES", raising=False)

    with pytest.raises(
        ConfigError, match="exactly one of X_COOKIES_PATH or X_COOKIES is required"
    ):
        Config.from_env()


def test_x_enabled_true_with_both_cookie_vars_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("X_ENABLED", "true")
    monkeypatch.setenv("X_COOKIES_PATH", "/tmp/x-cookies.json")
    monkeypatch.setenv("X_COOKIES", '{"ct0": "abc"}')

    with pytest.raises(ConfigError, match="only one of X_COOKIES_PATH or X_COOKIES"):
        Config.from_env()


def test_x_enabled_true_with_only_cookies_path_is_ok(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("X_ENABLED", "true")
    monkeypatch.setenv("X_COOKIES_PATH", "/tmp/x-cookies.json")
    monkeypatch.delenv("X_COOKIES", raising=False)

    config = Config.from_env()

    assert config.x_enabled is True
    assert config.x_cookies_path == "/tmp/x-cookies.json"
    assert config.x_cookies is None


def test_x_enabled_true_with_only_inline_cookies_is_ok(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("X_ENABLED", "true")
    monkeypatch.delenv("X_COOKIES_PATH", raising=False)
    monkeypatch.setenv("X_COOKIES", '{"ct0": "abc", "auth_token": "def"}')

    config = Config.from_env()

    assert config.x_enabled is True
    assert config.x_cookies_path is None
    assert config.x_cookies == '{"ct0": "abc", "auth_token": "def"}'
