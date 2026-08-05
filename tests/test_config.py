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


# --- CLAUDE_EFFORT ---


def test_claude_effort_unset_falls_back_to_high(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("CLAUDE_EFFORT", raising=False)

    config = Config.from_env()

    assert config.claude_effort == "high"


@pytest.mark.parametrize("value", ["low", "medium", "high", "xhigh", "max"])
def test_claude_effort_valid_values_round_trip(monkeypatch, value):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_EFFORT", value)

    config = Config.from_env()

    assert config.claude_effort == value


def test_claude_effort_invalid_value_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_EFFORT", "ultra")

    with pytest.raises(ConfigError, match="CLAUDE_EFFORT must be one of"):
        Config.from_env()


# --- DIGEST_FROM_NAME ---


def test_digest_from_name_unset_falls_back_to_default(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("DIGEST_FROM_NAME", raising=False)

    config = Config.from_env()

    assert config.digest_from_name == "Digest"


def test_digest_from_name_custom_value_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("DIGEST_FROM_NAME", "Owner's Digest")

    config = Config.from_env()

    assert config.digest_from_name == "Owner's Digest"


def test_digest_from_name_blank_falls_back_to_default(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("DIGEST_FROM_NAME", "   ")

    config = Config.from_env()

    assert config.digest_from_name == "Digest"


def test_digest_from_name_embedded_crlf_raises_config_error(monkeypatch):
    # Codex review finding on PR #20: an embedded CR/LF makes formataddr
    # build a header value that Python's email Generator refuses to
    # serialize (HeaderParseError), and that failure happens deep inside
    # send_digest AFTER the digest row is already durably recorded -- every
    # subsequent run's pending-digest retry hits the identical failure
    # forever. Must be rejected here, at startup, instead.
    _set_base_env(monkeypatch)
    monkeypatch.setenv("DIGEST_FROM_NAME", "Digest\r\nX-Injected: evil")

    with pytest.raises(ConfigError, match="DIGEST_FROM_NAME must not contain control characters"):
        Config.from_env()


def test_digest_from_name_embedded_control_character_raises_config_error(monkeypatch):
    # Not narrowed to \r\n alone -- any C0 control character or DEL is
    # equally invalid inside a header field body. \x00 is excluded here: the
    # OS itself rejects a NUL byte in an env var value (ValueError on
    # setenv), so it can never reach our validator in the first place --
    # \x0b (vertical tab) exercises the same C0-control-character branch
    # without that unrelated OS-level restriction getting in the way.
    _set_base_env(monkeypatch)
    monkeypatch.setenv("DIGEST_FROM_NAME", "Digest\x0bName")

    with pytest.raises(ConfigError, match="DIGEST_FROM_NAME must not contain control characters"):
        Config.from_env()


def test_digest_from_name_error_does_not_echo_the_offending_value(monkeypatch):
    # Unlike CLAUDE_EFFORT's ConfigError, the raw value (which may contain
    # control characters) must never be echoed back into the error message.
    _set_base_env(monkeypatch)
    monkeypatch.setenv("DIGEST_FROM_NAME", "Digest\r\nX-Injected: evil")

    with pytest.raises(ConfigError) as exc_info:
        Config.from_env()

    assert "X-Injected" not in str(exc_info.value)


# --- NEWS_FEEDS ---


def test_news_feeds_unset_defaults_to_empty_tuple(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("NEWS_FEEDS", raising=False)

    config = Config.from_env()

    assert config.news_feeds == ()


def test_news_feeds_blank_defaults_to_empty_tuple(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("NEWS_FEEDS", "   ")

    config = Config.from_env()

    assert config.news_feeds == ()


def test_news_feeds_two_urls_with_spaces_parsed_and_stripped(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv(
        "NEWS_FEEDS", " https://example.com/feed.xml , https://blog.example.org/rss "
    )

    config = Config.from_env()

    assert config.news_feeds == (
        "https://example.com/feed.xml",
        "https://blog.example.org/rss",
    )


def test_news_feeds_non_http_entry_raises_config_error_naming_the_variable(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("NEWS_FEEDS", "https://example.com/feed.xml,ftp://example.com/bad")

    with pytest.raises(ConfigError, match="NEWS_FEEDS"):
        Config.from_env()


# --- POLYMARKET_ENABLED / POLYMARKET_API_BASE / POLYMARKET_PROXY_KEY /
#     POLYMARKET_TOP_N / POLYMARKET_SWING_THRESHOLD ---


def test_polymarket_defaults_when_unset(monkeypatch):
    _set_base_env(monkeypatch)
    for name in (
        "POLYMARKET_ENABLED",
        "POLYMARKET_API_BASE",
        "POLYMARKET_PROXY_KEY",
        "POLYMARKET_TOP_N",
        "POLYMARKET_SWING_THRESHOLD",
    ):
        monkeypatch.delenv(name, raising=False)

    config = Config.from_env()

    assert config.polymarket_enabled is False
    assert config.polymarket_api_base == "https://gamma-api.polymarket.com"
    assert config.polymarket_proxy_key is None
    assert config.polymarket_top_n == 30
    assert config.polymarket_swing_threshold == 0.15


def test_polymarket_enabled_true_is_parsed(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_ENABLED", "true")

    config = Config.from_env()

    assert config.polymarket_enabled is True


def test_polymarket_api_base_custom_value_strips_trailing_slash(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_API_BASE", "https://proxy.example.com/gamma/")

    config = Config.from_env()

    assert config.polymarket_api_base == "https://proxy.example.com/gamma"


def test_polymarket_api_base_non_http_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_API_BASE", "ftp://proxy.example.com")

    with pytest.raises(ConfigError, match="POLYMARKET_API_BASE"):
        Config.from_env()


def test_polymarket_proxy_key_custom_value_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_PROXY_KEY", "super-secret-key")

    config = Config.from_env()

    assert config.polymarket_proxy_key == "super-secret-key"


def test_polymarket_proxy_key_excluded_from_repr(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_PROXY_KEY", "super-secret-key")

    config = Config.from_env()

    assert "super-secret-key" not in repr(config)


def test_polymarket_top_n_custom_value_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_TOP_N", "50")

    config = Config.from_env()

    assert config.polymarket_top_n == 50


@pytest.mark.parametrize("value", ["0", "101", "not-a-number"])
def test_polymarket_top_n_out_of_range_raises_config_error(monkeypatch, value):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_TOP_N", value)

    with pytest.raises(ConfigError, match="POLYMARKET_TOP_N"):
        Config.from_env()


def test_polymarket_top_n_boundary_values_are_accepted(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_TOP_N", "1")
    assert Config.from_env().polymarket_top_n == 1

    monkeypatch.setenv("POLYMARKET_TOP_N", "100")
    assert Config.from_env().polymarket_top_n == 100


def test_polymarket_swing_threshold_custom_value_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_SWING_THRESHOLD", "0.2")

    config = Config.from_env()

    assert config.polymarket_swing_threshold == 0.2


@pytest.mark.parametrize("value", ["0", "1", "1.5", "-0.1", "not-a-number"])
def test_polymarket_swing_threshold_out_of_range_raises_config_error(monkeypatch, value):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POLYMARKET_SWING_THRESHOLD", value)

    with pytest.raises(ConfigError, match="POLYMARKET_SWING_THRESHOLD"):
        Config.from_env()
