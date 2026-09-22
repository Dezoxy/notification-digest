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

    with pytest.raises(ConfigError, match="exactly one of X_COOKIES_PATH or X_COOKIES is required"):
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


def test_legacy_secrets_excluded_from_repr(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("TG_SESSION", "super-secret-tg-session")
    monkeypatch.setenv("TG_API_HASH", "super-secret-tg-api-hash")
    monkeypatch.setenv("SMTP_PASSWORD", "super-secret-smtp-password")
    monkeypatch.setenv("X_ENABLED", "true")
    monkeypatch.delenv("X_COOKIES_PATH", raising=False)
    monkeypatch.setenv("X_COOKIES", "super-secret-x-cookies")

    config = Config.from_env()

    text = repr(config)
    assert "super-secret-tg-session" not in text
    assert "super-secret-tg-api-hash" not in text
    assert "super-secret-smtp-password" not in text
    assert "super-secret-x-cookies" not in text


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


# --- POSITIONS_TG_CHANNELS ---


def test_positions_tg_channels_unset_defaults_to_empty_tuple(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("POSITIONS_TG_CHANNELS", raising=False)

    config = Config.from_env()

    assert config.positions_tg_channels == ()


def test_positions_tg_channels_two_names_parsed_and_stripped(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POSITIONS_TG_CHANNELS", " ASI_Alliance , fetchunofficial ")

    config = Config.from_env()

    assert config.positions_tg_channels == ("ASI_Alliance", "fetchunofficial")


def test_positions_tg_channels_prefixed_entry_raises_config_error(monkeypatch):
    # An "@" or "t.me/" prefix would silently never match any item URL in
    # allocate_by_source -- exactly the quiet misconfiguration the startup
    # validation exists to catch.
    _set_base_env(monkeypatch)
    monkeypatch.setenv("POSITIONS_TG_CHANNELS", "@ASI_Alliance")

    with pytest.raises(ConfigError, match="POSITIONS_TG_CHANNELS"):
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


# --- REDDIT_ENABLED / REDDIT_SESSION_COOKIE /
#     REDDIT_SUBREDDITS / REDDIT_POSTS_PER_SUB ---


def test_reddit_defaults_when_unset(monkeypatch):
    _set_base_env(monkeypatch)
    for name in (
        "REDDIT_ENABLED",
        "REDDIT_SESSION_COOKIE",
        "REDDIT_SUBREDDITS",
        "REDDIT_POSTS_PER_SUB",
    ):
        monkeypatch.delenv(name, raising=False)

    config = Config.from_env()

    assert config.reddit_enabled is False
    assert config.reddit_session_cookie is None
    assert config.reddit_subreddits == ()
    assert config.reddit_posts_per_sub == 10


def test_reddit_enabled_without_session_cookie_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("REDDIT_ENABLED", "true")
    monkeypatch.delenv("REDDIT_SESSION_COOKIE", raising=False)
    monkeypatch.setenv("REDDIT_SUBREDDITS", "news")

    with pytest.raises(ConfigError, match="REDDIT_SESSION_COOKIE"):
        Config.from_env()


def test_reddit_enabled_without_subreddits_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("REDDIT_ENABLED", "true")
    monkeypatch.setenv("REDDIT_SESSION_COOKIE", "cookie-value")
    monkeypatch.delenv("REDDIT_SUBREDDITS", raising=False)

    with pytest.raises(ConfigError, match="REDDIT_SUBREDDITS"):
        Config.from_env()


def test_reddit_enabled_with_valid_config_is_parsed(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("REDDIT_ENABLED", "true")
    monkeypatch.setenv("REDDIT_SESSION_COOKIE", "cookie-value")
    monkeypatch.setenv("REDDIT_SUBREDDITS", "Futurology, LocalLLaMA,hungary")

    config = Config.from_env()

    assert config.reddit_enabled is True
    assert config.reddit_session_cookie == "cookie-value"
    assert config.reddit_subreddits == ("Futurology", "LocalLLaMA", "hungary")


@pytest.mark.parametrize("value", ["r/hungary", "hun gary", "hungary!"])
def test_reddit_subreddits_invalid_token_raises_config_error(monkeypatch, value):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("REDDIT_ENABLED", "true")
    monkeypatch.setenv("REDDIT_SESSION_COOKIE", "cookie-value")
    monkeypatch.setenv("REDDIT_SUBREDDITS", f"news,{value}")

    with pytest.raises(ConfigError, match="REDDIT_SUBREDDITS"):
        Config.from_env()


def test_reddit_subreddits_blank_entries_are_dropped_not_errors(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("REDDIT_ENABLED", "true")
    monkeypatch.setenv("REDDIT_SESSION_COOKIE", "cookie-value")
    monkeypatch.setenv("REDDIT_SUBREDDITS", "news,,")

    config = Config.from_env()

    assert config.reddit_subreddits == ("news",)


def test_reddit_session_cookie_excluded_from_repr(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("REDDIT_ENABLED", "true")
    monkeypatch.setenv("REDDIT_SESSION_COOKIE", "super-secret-cookie-value")
    monkeypatch.setenv("REDDIT_SUBREDDITS", "news")

    config = Config.from_env()

    assert "super-secret-cookie-value" not in repr(config)


def test_reddit_posts_per_sub_custom_value_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("REDDIT_POSTS_PER_SUB", "15")

    config = Config.from_env()

    assert config.reddit_posts_per_sub == 15


@pytest.mark.parametrize("value", ["0", "26", "not-a-number"])
def test_reddit_posts_per_sub_out_of_range_raises_config_error(monkeypatch, value):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("REDDIT_POSTS_PER_SUB", value)

    with pytest.raises(ConfigError, match="REDDIT_POSTS_PER_SUB"):
        Config.from_env()


def test_reddit_posts_per_sub_boundary_values_are_accepted(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("REDDIT_POSTS_PER_SUB", "1")
    assert Config.from_env().reddit_posts_per_sub == 1

    monkeypatch.setenv("REDDIT_POSTS_PER_SUB", "25")
    assert Config.from_env().reddit_posts_per_sub == 25


# --- HACKERNEWS_ENABLED / HACKERNEWS_TOP_N ---


def test_hackernews_defaults_when_unset(monkeypatch):
    _set_base_env(monkeypatch)
    for name in ("HACKERNEWS_ENABLED", "HACKERNEWS_TOP_N"):
        monkeypatch.delenv(name, raising=False)

    config = Config.from_env()

    assert config.hackernews_enabled is False
    assert config.hackernews_top_n == 15


def test_hackernews_enabled_is_parsed(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("HACKERNEWS_ENABLED", "true")

    config = Config.from_env()

    assert config.hackernews_enabled is True


def test_hackernews_top_n_custom_value_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("HACKERNEWS_TOP_N", "20")

    config = Config.from_env()

    assert config.hackernews_top_n == 20


@pytest.mark.parametrize("value", ["0", "31", "not-a-number"])
def test_hackernews_top_n_out_of_range_raises_config_error(monkeypatch, value):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("HACKERNEWS_TOP_N", value)

    with pytest.raises(ConfigError, match="HACKERNEWS_TOP_N"):
        Config.from_env()


def test_hackernews_top_n_boundary_values_are_accepted(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("HACKERNEWS_TOP_N", "1")
    assert Config.from_env().hackernews_top_n == 1

    monkeypatch.setenv("HACKERNEWS_TOP_N", "30")
    assert Config.from_env().hackernews_top_n == 30


# --- EMAIL_ENABLED / SITE_PUBLISH_URL / SITE_INGEST_KEY / SITE_PUBLIC_BASE /
#     TELEGRAM_NOTIFY_BOT_TOKEN / TELEGRAM_NOTIFY_CHAT_ID /
#     TELEGRAM_NOTIFY_THREAD_ID (delivery-channels feature) ---


def _clear_delivery_channel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "EMAIL_ENABLED",
        "SITE_PUBLISH_URL",
        "SITE_INGEST_KEY",
        "SITE_PUBLIC_BASE",
        "TELEGRAM_NOTIFY_BOT_TOKEN",
        "TELEGRAM_NOTIFY_CHAT_ID",
        "TELEGRAM_NOTIFY_THREAD_ID",
        "TELEGRAM_DAILY_THREAD_ID",
        "TELEGRAM_WEEKLY_THREAD_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def test_delivery_channel_defaults_email_only(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)

    config = Config.from_env()

    assert config.email_enabled is True
    assert config.site_publish_url is None
    assert config.site_ingest_key is None
    assert config.site_public_base is None
    assert config.telegram_notify_bot_token is None
    assert config.telegram_notify_chat_id is None
    assert config.telegram_notify_thread_id == 0


def test_email_enabled_false_is_parsed(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    # Some other channel must stay enabled, or the all-disabled guard fires
    # (see test_all_channels_disabled_raises_config_error below).
    monkeypatch.setenv("SITE_PUBLISH_URL", "https://news-site.example.workers.dev")
    monkeypatch.setenv("SITE_INGEST_KEY", "ingest-secret")

    config = Config.from_env()

    assert config.email_enabled is False


def test_all_channels_disabled_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("EMAIL_ENABLED", "false")

    with pytest.raises(ConfigError, match="all delivery channels disabled"):
        Config.from_env()


def test_site_publish_url_without_ingest_key_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("SITE_PUBLISH_URL", "https://news-site.example.workers.dev")

    with pytest.raises(ConfigError, match="SITE_INGEST_KEY is required"):
        Config.from_env()


def test_site_publish_url_with_ingest_key_is_ok(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("SITE_PUBLISH_URL", "https://news-site.example.workers.dev/")
    monkeypatch.setenv("SITE_INGEST_KEY", "ingest-secret")

    config = Config.from_env()

    assert config.site_publish_url == "https://news-site.example.workers.dev"  # trailing / stripped
    assert config.site_ingest_key == "ingest-secret"


def test_site_publish_url_non_http_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("SITE_PUBLISH_URL", "ftp://news-site.example.com")
    monkeypatch.setenv("SITE_INGEST_KEY", "ingest-secret")

    with pytest.raises(ConfigError, match="SITE_PUBLISH_URL"):
        Config.from_env()


def test_telegram_bot_token_without_chat_id_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok")

    with pytest.raises(ConfigError, match="TELEGRAM_NOTIFY_CHAT_ID is required"):
        Config.from_env()


def test_telegram_bot_token_without_site_public_base_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")

    with pytest.raises(ConfigError, match="SITE_PUBLIC_BASE is required"):
        Config.from_env()


def test_telegram_channel_fully_configured_is_ok(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok/")
    monkeypatch.setenv("TELEGRAM_NOTIFY_THREAD_ID", "42")

    config = Config.from_env()

    assert config.telegram_notify_bot_token == "bot-token"
    assert config.telegram_notify_chat_id == "-100123"
    assert config.site_public_base == "https://news.example.com/t/tok"  # trailing / stripped
    assert config.telegram_notify_thread_id == 42


def test_telegram_notify_thread_id_defaults_to_zero(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok")

    config = Config.from_env()

    assert config.telegram_notify_thread_id == 0


def test_telegram_notify_thread_id_negative_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok")
    monkeypatch.setenv("TELEGRAM_NOTIFY_THREAD_ID", "-1")

    with pytest.raises(ConfigError, match="TELEGRAM_NOTIFY_THREAD_ID must be a non-negative"):
        Config.from_env()


# --- TELEGRAM_DAILY_THREAD_ID (daily-brief feature) ---


def test_telegram_daily_thread_id_defaults_to_none(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.delenv("TELEGRAM_DAILY_THREAD_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok")

    config = Config.from_env()

    assert config.telegram_daily_thread_id is None


def test_telegram_daily_thread_id_explicit_value_is_parsed(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok")
    monkeypatch.setenv("TELEGRAM_DAILY_THREAD_ID", "77")

    config = Config.from_env()

    assert config.telegram_daily_thread_id == 77


def test_telegram_daily_thread_id_explicit_zero_is_distinguishable_from_unset(monkeypatch):
    # 0 is a legitimate real thread id (post to the group root) here too --
    # it must round-trip as 0, not collapse to the same None the unset case
    # produces, since main.py's fallback-to-window-thread logic keys off
    # exactly that distinction.
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok")
    monkeypatch.setenv("TELEGRAM_DAILY_THREAD_ID", "0")

    config = Config.from_env()

    assert config.telegram_daily_thread_id == 0
    assert config.telegram_daily_thread_id is not None


def test_telegram_daily_thread_id_negative_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok")
    monkeypatch.setenv("TELEGRAM_DAILY_THREAD_ID", "-1")

    with pytest.raises(ConfigError, match="TELEGRAM_DAILY_THREAD_ID must be a non-negative"):
        Config.from_env()


# --- TELEGRAM_WEEKLY_THREAD_ID (weekly-brief feature) ---


def test_telegram_weekly_thread_id_defaults_to_none(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.delenv("TELEGRAM_WEEKLY_THREAD_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok")

    config = Config.from_env()

    assert config.telegram_weekly_thread_id is None


def test_telegram_weekly_thread_id_explicit_value_is_parsed(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok")
    monkeypatch.setenv("TELEGRAM_WEEKLY_THREAD_ID", "141")

    config = Config.from_env()

    assert config.telegram_weekly_thread_id == 141


def test_telegram_weekly_thread_id_explicit_zero_is_distinguishable_from_unset(monkeypatch):
    # 0 is a legitimate real thread id (post to the group root) here too --
    # it must round-trip as 0, not collapse to the same None the unset case
    # produces, mirroring TELEGRAM_DAILY_THREAD_ID's identical distinction.
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok")
    monkeypatch.setenv("TELEGRAM_WEEKLY_THREAD_ID", "0")

    config = Config.from_env()

    assert config.telegram_weekly_thread_id == 0
    assert config.telegram_weekly_thread_id is not None


def test_telegram_weekly_thread_id_negative_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/tok")
    monkeypatch.setenv("TELEGRAM_WEEKLY_THREAD_ID", "-1")

    with pytest.raises(ConfigError, match="TELEGRAM_WEEKLY_THREAD_ID must be a non-negative"):
        Config.from_env()


def test_site_and_telegram_secrets_excluded_from_repr(monkeypatch):
    _set_base_env(monkeypatch)
    _clear_delivery_channel_env(monkeypatch)
    monkeypatch.setenv("SITE_PUBLISH_URL", "https://news-site.example.workers.dev")
    monkeypatch.setenv("SITE_INGEST_KEY", "super-secret-ingest-key")
    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "super-secret-bot-token")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "-100123")
    monkeypatch.setenv("SITE_PUBLIC_BASE", "https://news.example.com/t/super-secret-token")

    config = Config.from_env()

    text = repr(config)
    assert "super-secret-ingest-key" not in text
    assert "super-secret-bot-token" not in text
    assert "super-secret-token" not in text


# --- TRANSLATE_HU_ENABLED / TRANSLATE_MODEL (Hungarian translation step) ---


def test_translate_hu_enabled_defaults_to_false(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("TRANSLATE_HU_ENABLED", raising=False)

    config = Config.from_env()

    assert config.translate_hu_enabled is False


def test_translate_hu_enabled_true_is_parsed(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("TRANSLATE_HU_ENABLED", "true")

    config = Config.from_env()

    assert config.translate_hu_enabled is True


def test_translate_model_defaults_to_pinned_sonnet_5(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("TRANSLATE_MODEL", raising=False)

    config = Config.from_env()

    assert config.translate_model == "claude-sonnet-5"


def test_anthropic_model_defaults_to_pinned_opus_5_5(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("VERIFY_DAILY_MODEL", raising=False)

    config = Config.from_env()

    assert config.anthropic_model == "claude-opus-5-5"
    assert config.verify_daily_model == "claude-opus-5-5"


def test_translate_model_override_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("TRANSLATE_MODEL", "claude-haiku-5")

    config = Config.from_env()

    assert config.translate_model == "claude-haiku-5"


def test_translate_model_fallback_defaults_to_claude_sonnet_4_6(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("TRANSLATE_MODEL_FALLBACK", raising=False)

    config = Config.from_env()

    assert config.translate_model_fallback == "claude-sonnet-4-6"


def test_translate_model_fallback_override_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("TRANSLATE_MODEL_FALLBACK", "claude-haiku-5")

    config = Config.from_env()

    assert config.translate_model_fallback == "claude-haiku-5"


def test_translate_model_fallback_empty_string_disables_it(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("TRANSLATE_MODEL_FALLBACK", "")

    config = Config.from_env()

    assert config.translate_model_fallback == ""


# --- VERIFY_DAILY_* (PLAN.md §11.4 verified briefing) ---


def test_verify_daily_enabled_defaults_to_false(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("VERIFY_DAILY_ENABLED", raising=False)

    config = Config.from_env()

    assert config.verify_daily_enabled is False


def test_verify_daily_enabled_true_is_parsed(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("VERIFY_DAILY_ENABLED", "true")

    config = Config.from_env()

    assert config.verify_daily_enabled is True


def test_verify_daily_timeout_seconds_defaults_to_600(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("VERIFY_DAILY_TIMEOUT_SECONDS", raising=False)

    config = Config.from_env()

    assert config.verify_daily_timeout_seconds == 600


def test_verify_daily_timeout_seconds_override_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("VERIFY_DAILY_TIMEOUT_SECONDS", "900")

    config = Config.from_env()

    assert config.verify_daily_timeout_seconds == 900


def test_translate_timeout_seconds_defaults_to_300(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("TRANSLATE_TIMEOUT_SECONDS", raising=False)

    config = Config.from_env()

    assert config.translate_timeout_seconds == 300


def test_translate_timeout_seconds_override_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("TRANSLATE_TIMEOUT_SECONDS", "120")

    config = Config.from_env()

    assert config.translate_timeout_seconds == 120


def test_translate_timeout_is_independent_of_claude_timeout(monkeypatch):
    """The whole point of the knob: translation must NOT inherit the summarizer's budget.

    translate_digest can call the model twice (primary, then
    translate_model_fallback on a safeguards refusal), each at the full
    timeout. While it reused claude_timeout_seconds, raising the summarizer
    budget silently doubled the daily run's translation worst case too --
    which is what pushed digest-daily.service past its systemd
    TimeoutStartSec. Setting them to different values here fails if anyone
    re-couples them.
    """
    _set_base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("TRANSLATE_TIMEOUT_SECONDS", "300")

    config = Config.from_env()

    assert config.claude_timeout_seconds == 600
    assert config.translate_timeout_seconds == 300


def test_verify_daily_timeout_seconds_zero_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("VERIFY_DAILY_TIMEOUT_SECONDS", "0")

    with pytest.raises(
        ConfigError, match="VERIFY_DAILY_TIMEOUT_SECONDS must be a positive integer"
    ):
        Config.from_env()


def test_verify_daily_max_web_ops_defaults_to_20(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("VERIFY_DAILY_MAX_WEB_OPS", raising=False)

    config = Config.from_env()

    assert config.verify_daily_max_web_ops == 20


def test_verify_daily_max_web_ops_override_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("VERIFY_DAILY_MAX_WEB_OPS", "5")

    config = Config.from_env()

    assert config.verify_daily_max_web_ops == 5


def test_verify_daily_max_web_ops_out_of_range_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("VERIFY_DAILY_MAX_WEB_OPS", "0")

    with pytest.raises(ConfigError, match="VERIFY_DAILY_MAX_WEB_OPS must be an integer between"):
        Config.from_env()


def test_verify_daily_model_defaults_to_anthropic_model(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-custom-9")
    monkeypatch.delenv("VERIFY_DAILY_MODEL", raising=False)

    config = Config.from_env()

    assert config.verify_daily_model == "claude-custom-9"


def test_verify_daily_model_override_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5")
    monkeypatch.setenv("VERIFY_DAILY_MODEL", "claude-sonnet-5")

    config = Config.from_env()

    assert config.verify_daily_model == "claude-sonnet-5"


def test_verify_daily_effort_defaults_to_claude_effort(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_EFFORT", "xhigh")
    monkeypatch.delenv("VERIFY_DAILY_EFFORT", raising=False)

    config = Config.from_env()

    assert config.verify_daily_effort == "xhigh"


def test_verify_daily_effort_override_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_EFFORT", "high")
    monkeypatch.setenv("VERIFY_DAILY_EFFORT", "max")

    config = Config.from_env()

    assert config.verify_daily_effort == "max"


def test_verify_daily_effort_invalid_choice_raises_config_error(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("VERIFY_DAILY_EFFORT", "ultra")

    with pytest.raises(ConfigError, match="VERIFY_DAILY_EFFORT must be one of"):
        Config.from_env()


# --- ARC_KEYS_SITE_ENABLED (stable-arc-keys feature) ---


def test_arc_keys_site_enabled_defaults_to_true(monkeypatch):
    # Defaults ON because the site half shipped and deployed FIRST
    # (toom-edge PR #140): its validateTopics accepts the optional
    # per-entry "key". That ordering was mandatory -- validateTopics 400s
    # the whole PUT on an UNKNOWN per-entry field, so this could never have
    # defaulted on before #140 landed. The flag survives as a kill switch
    # for a site rollback; see digest/config.py's own docstring.
    _set_base_env(monkeypatch)
    monkeypatch.delenv("ARC_KEYS_SITE_ENABLED", raising=False)

    config = Config.from_env()

    assert config.arc_keys_site_enabled is True


def test_arc_keys_site_enabled_true_is_parsed(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("ARC_KEYS_SITE_ENABLED", "true")

    config = Config.from_env()

    assert config.arc_keys_site_enabled is True


# --- Patreon collector (PATREON_CAMPAIGN_ID / PATREON_SESSION_COOKIE) ---


def test_patreon_is_disabled_when_neither_var_is_set(monkeypatch):
    # The empty-means-disabled default matters operationally: digest.env is
    # rendered by the homelab Ansible role, so an unrendered or reverted
    # file must degrade to "collector off", never to a crash.
    _set_base_env(monkeypatch)
    monkeypatch.delenv("PATREON_CAMPAIGN_ID", raising=False)
    monkeypatch.delenv("PATREON_SESSION_COOKIE", raising=False)

    cfg = Config.from_env()

    assert cfg.patreon_campaign_id == ""
    assert cfg.patreon_session_cookie == ""


def test_patreon_campaign_id_without_cookie_raises(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("PATREON_CAMPAIGN_ID", "7095842")
    monkeypatch.delenv("PATREON_SESSION_COOKIE", raising=False)

    with pytest.raises(ConfigError, match="PATREON_SESSION_COOKIE is required"):
        Config.from_env()


def test_patreon_cookie_without_campaign_id_raises(monkeypatch):
    # A cookie with nothing to point it at is a half-finished deploy, not a
    # disabled collector -- fail loudly rather than silently doing nothing.
    _set_base_env(monkeypatch)
    monkeypatch.setenv("PATREON_SESSION_COOKIE", "cookie-value")
    monkeypatch.delenv("PATREON_CAMPAIGN_ID", raising=False)

    with pytest.raises(ConfigError, match="PATREON_CAMPAIGN_ID is required"):
        Config.from_env()


def test_patreon_fully_configured_is_accepted(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("PATREON_CAMPAIGN_ID", "7095842")
    monkeypatch.setenv("PATREON_SESSION_COOKIE", "cookie-value")

    cfg = Config.from_env()

    assert cfg.patreon_campaign_id == "7095842"
    assert cfg.patreon_session_cookie == "cookie-value"


def test_patreon_cookie_is_kept_out_of_repr(monkeypatch):
    # It is a live credential to a paid account; repr=False keeps it out of
    # any accidental repr(cfg) log line.
    _set_base_env(monkeypatch)
    monkeypatch.setenv("PATREON_CAMPAIGN_ID", "7095842")
    monkeypatch.setenv("PATREON_SESSION_COOKIE", "super-secret-cookie")

    assert "super-secret-cookie" not in repr(Config.from_env())


def test_telegram_patreon_thread_id_defaults_to_none(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("TELEGRAM_PATREON_THREAD_ID", raising=False)

    assert Config.from_env().telegram_patreon_thread_id is None


def test_telegram_patreon_thread_id_is_parsed(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_PATREON_THREAD_ID", "317")

    assert Config.from_env().telegram_patreon_thread_id == 317


# --- OpenRouter fallback chain (OPENROUTER_API_KEY / FALLBACK_MODELS / FALLBACK_LIGHT_MODELS) ---


def test_openrouter_api_key_unset_defaults_to_none(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert Config.from_env().openrouter_api_key is None


def test_openrouter_api_key_custom_value_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-key")

    assert Config.from_env().openrouter_api_key == "sk-or-secret-key"


def test_openrouter_api_key_excluded_from_repr(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-key")

    assert "sk-or-secret-key" not in repr(Config.from_env())


def test_fallback_models_unset_falls_back_to_the_owner_default(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("FALLBACK_MODELS", raising=False)

    assert Config.from_env().fallback_models == ("openai/gpt-5.6-sol", "z-ai/glm-5.3")


def test_fallback_models_explicitly_empty_means_no_legs(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("FALLBACK_MODELS", "   ")

    assert Config.from_env().fallback_models == ()


def test_fallback_models_custom_list_is_parsed_and_stripped(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("FALLBACK_MODELS", " openai/gpt-5.6-sol , mistralai/mistral-large ")

    assert Config.from_env().fallback_models == (
        "openai/gpt-5.6-sol",
        "mistralai/mistral-large",
    )


def test_fallback_models_bad_model_id_raises_config_error_naming_the_variable(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("FALLBACK_MODELS", "not-a-valid-model-id")

    with pytest.raises(ConfigError, match="FALLBACK_MODELS"):
        Config.from_env()


def test_fallback_light_models_unset_falls_back_to_the_owner_default(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("FALLBACK_LIGHT_MODELS", raising=False)

    assert Config.from_env().fallback_light_models == (
        "openai/gpt-5.6-terra",
        "deepseek/deepseek-v4-flash",
    )


def test_fallback_light_models_explicitly_empty_means_no_legs(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("FALLBACK_LIGHT_MODELS", "")

    assert Config.from_env().fallback_light_models == ()


def test_fallback_light_models_bad_model_id_raises_config_error_naming_the_variable(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("FALLBACK_LIGHT_MODELS", "UPPER/case")

    with pytest.raises(ConfigError, match="FALLBACK_LIGHT_MODELS"):
        Config.from_env()


def test_fallback_timeout_seconds_unset_falls_back_to_180(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.delenv("FALLBACK_TIMEOUT_SECONDS", raising=False)

    assert Config.from_env().fallback_timeout_seconds == 180


def test_fallback_timeout_seconds_custom_value_is_used(monkeypatch):
    _set_base_env(monkeypatch)
    monkeypatch.setenv("FALLBACK_TIMEOUT_SECONDS", "90")

    assert Config.from_env().fallback_timeout_seconds == 90


def test_openrouter_api_key_not_in_claude_subprocess_env_allowlist(monkeypatch):
    # The `claude -p` subprocess must never see a different provider's own
    # credential -- see claude_subprocess_env's own docstring.
    from digest.config import claude_subprocess_env

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-key")

    assert "OPENROUTER_API_KEY" not in claude_subprocess_env()
