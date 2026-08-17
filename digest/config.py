"""Environment-variable loading and validation.

This module is the ONLY place in the codebase allowed to read os.environ /
os.getenv. Every other module receives configuration values passed in.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed.

    Messages must name the offending variable but must never echo its value
    — some of these variables are secrets.
    """


# Valid values for CLAUDE_EFFORT, mirroring the `claude` CLI's own
# `--effort <low|medium|high|xhigh|max>` flag (see run_claude in
# digest/summarize.py). Kept here, next to the validator, rather than
# inlined at the call site, so the allowed set has exactly one place to
# update if the CLI ever adds or removes a level.
_CLAUDE_EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max")

# Matches any C0 control character (0x00-0x1F) or DEL (0x7F) -- used to reject
# header-injection-shaped values for DIGEST_FROM_NAME (see _optional_str).
# CR/LF are the specific concern (an embedded one breaks RFC 5322 header
# folding and email.utils.formataddr/Generator raise HeaderParseError at
# message-serialization time), but every other control character is equally
# invalid inside a header field body, so the check is not narrowed to \r\n
# alone.
_HEADER_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

# Reddit subreddit name validation (REDDIT_SUBREDDITS) -- names are given
# WITHOUT the "r/" prefix (e.g. "Futurology", not "r/Futurology"), matching
# Reddit's own subreddit name charset (letters, digits, underscore). These
# values are interpolated directly into a request path by
# digest/collectors/reddit.py (f"{base}/r/{subreddit}/top?..."), so this is
# validated at startup rather than surfacing as an opaque 404/malformed
# request deep inside a scheduled run.
_SUBREDDIT_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class Config:
    tg_api_id: int
    tg_api_hash: str = field(repr=False)
    tg_session: str = field(repr=False)
    tg_chat_allowlist: tuple[int, ...]
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str = field(repr=False)
    digest_from: str
    digest_to: str
    digest_from_name: str = "Digest"
    state_db_path: str = "./state.db"
    x_enabled: bool = False
    x_cookies_path: str | None = None
    x_cookies: str | None = field(default=None, repr=False)
    anthropic_model: str = "claude-opus-5"
    archive_dir: str = "./archive"
    claude_timeout_seconds: int = 300
    claude_effort: str = "high"
    # Hungarian translation step (digest/translate.py), run after summarize()
    # succeeds -- a soft-failing production step, never a delivery channel of
    # its own (see translate_digest's docstring). Parsed like x_enabled/
    # polymarket_enabled (an explicit on/off flag), not news_feeds' empty-
    # means-disabled shape: there's no natural "unconfigured" sentinel for a
    # pure feature toggle with no accompanying required value.
    translate_hu_enabled: bool = False
    # The summarizer runs the bigger cfg.anthropic_model (editorial judgment:
    # clustering, weighting, cutting); translation is a faithful structural
    # rewrite with no editorial judgment involved, so it deliberately runs a
    # cheaper model tier by default rather than inheriting anthropic_model.
    # "sonnet" is a model ALIAS the `claude` CLI itself resolves (see
    # run_claude in digest/summarize.py) -- not a full model id, mirroring
    # anthropic_model's own default of a bare alias-shaped string.
    translate_model: str = "sonnet"
    # Used only when the primary TRANSLATE_MODEL is refused by the API
    # safety classifier (digest/summarize.py's SafeguardsRefusalError; see
    # digest/translate.py's translate_digest for the fallback call itself).
    # Default is claude-sonnet-4-6 because it predates Sonnet 5's real-time
    # cyber safeguards, is still an active model, and translation needs no
    # frontier capability (see _TRANSLATE_EFFORT's own comment in
    # digest/translate.py) -- an older Sonnet is an acceptable quality
    # trade for keeping the Hungarian channel alive on content the primary
    # model won't touch. Set to an empty string to disable the fallback
    # entirely (translate_digest treats a falsy fallback_model as "none
    # configured" and re-raises the refusal instead of retrying).
    translate_model_fallback: str = "claude-sonnet-4-6"
    # PLAN.md §11.4 -- the optional verified-briefing pass (digest/verify.py)
    # over the daily brief, run between summarize_daily and translate_digest
    # (draft -> verify -> translate -> deliver). Defaults OFF: the entry is
    # APPROVED only through the flag-off-default state -- flag-on live
    # validation against real briefs, and the default-on decision, stay
    # owner-gated (see PLAN.md §11.4's Status line). Parsed like x_enabled/
    # polymarket_enabled (an explicit on/off flag), not news_feeds' empty-
    # means-disabled shape -- there is no natural "unconfigured" sentinel for
    # a pure feature toggle with no accompanying required value.
    verify_daily_enabled: bool = False
    # Generous relative to claude_timeout_seconds' own 300s default: this
    # pass runs WebSearch/WebFetch tool calls in a loop (a full agentic
    # session, not a single completion), which can take materially longer
    # than a toolless summarization call -- PLAN.md §11.4 itself notes
    # "wall-clock on the daily run roughly doubles", and the daily timer is
    # independent of the 6-hourly one (nothing else is blocked on this run
    # finishing), so there is no reason to pick a tight bound here the way
    # CLAUDE_TIMEOUT_SECONDS' 300s suits the 4x/day toolless window calls.
    verify_daily_timeout_seconds: int = 600
    # A cap passed into the verify prompt's own text as GUIDANCE ONLY
    # ({{MAX_WEB_OPS}} in prompts/verify-daily.md) -- the `claude -p` CLI has
    # no flag to enforce a hard tool-call budget itself (confirmed against
    # `claude -p --help`, 2026-08-10: no such flag exists), so this is the
    # model policing its own spend, not something digest/verify.py's code
    # can cut off mid-session. 20 is "a bounded handful" (PLAN.md §11.4's own
    # phrase) with headroom for a handful of arc sections each getting a
    # search plus one or two fetches.
    verify_daily_max_web_ops: int = 20
    # Both default to the SAME model/effort the daily brief's own
    # summarize_daily call already uses (`anthropic_model`/`claude_effort`)
    # -- PLAN.md §11.4: "model/effort... defaulting to the existing daily
    # model/effort values". The dataclass-level defaults below just mirror
    # anthropic_model's/claude_effort's own literal defaults for a
    # directly-constructed Config (tests); from_env resolves the REAL
    # dynamic fallback -- VERIFY_DAILY_MODEL/VERIFY_DAILY_EFFORT unset falls
    # back to whatever ANTHROPIC_MODEL/CLAUDE_EFFORT this run actually
    # resolved to, not to a second hardcoded literal -- so the verify pass
    # silently follows the owner's primary model/effort choice unless
    # explicitly pointed elsewhere. Kept as their OWN env vars (rather than
    # reusing anthropic_model/claude_effort outright) so the verify pass CAN
    # be pointed at a different model/effort later without touching the
    # primary daily/window summarization tier -- e.g. if a cheaper or more
    # search-capable model turns out to suit tool-heavy verification better
    # than the primary editorial model does.
    verify_daily_model: str = "claude-opus-5"
    verify_daily_effort: str = "high"
    # The news collector has no separate NEWS_ENABLED flag -- it is enabled
    # iff this tuple is non-empty (see digest/main.py's _run_news_collector).
    # An empty tuple is the natural "not configured" default, so a second
    # on/off switch would just be a way for the two to disagree.
    news_feeds: tuple[str, ...] = ()
    # Polymarket collector (see digest/collectors/polymarket.py). Like
    # x_enabled, this IS a separate on/off flag rather than an
    # empty-means-disabled sentinel (contrast news_feeds above) -- there is
    # no natural "unconfigured" shape for a single base URL + threshold the
    # way an empty feed list naturally means "no feeds", so an explicit flag
    # is the only unambiguous way to represent "collector present but off".
    polymarket_enabled: bool = False
    polymarket_api_base: str = "https://gamma-api.polymarket.com"
    # SECRET: authenticates to the owner's own Cloudflare Worker proxy
    # (polymarket.com is ISP-blocked in Hungary, per this feature's network
    # context) -- never logged. repr=False keeps it out of any accidental
    # `repr(cfg)`/dataclass-default log line the way smtp_password/x_cookies
    # are NOT currently protected for elsewhere in this dataclass; this is a
    # newly added field, so it gets the protection from day one rather than
    # inheriting the older fields' gap.
    polymarket_proxy_key: str | None = field(default=None, repr=False)
    polymarket_top_n: int = 30
    polymarket_swing_threshold: float = 0.15
    # Delivery channels (see digest/deliver.py's deliver_channels and
    # digest/publish.py). Each channel has its own enabled-ness and its own
    # per-digest sent flag (digest/state.py's digests.email_sent/
    # site_published/telegram_sent) so one channel failing never blocks or
    # duplicates another. email_enabled defaults True (parsed like
    # x_enabled/polymarket_enabled, not news_feeds' empty-tuple sentinel --
    # email has always been unconditionally on, so a boolean OFF switch is
    # the natural shape for turning it off, not an emptiness convention).
    email_enabled: bool = True
    # site_publish_url is None/unset = the site channel is disabled --
    # mirrors news_feeds' empty-means-disabled shape (there is no sane
    # non-empty default for "which Worker to PUT to"), not
    # polymarket_enabled's explicit-flag shape.
    site_publish_url: str | None = None
    # SECRET: sent as the x-ingest-key header on every site publish PUT
    # (digest/publish.py's publish_to_site) -- never logged. Required
    # (ConfigError at startup) whenever site_publish_url is set: an
    # unauthenticated publish attempt against the owner's own ingest
    # endpoint is a misconfiguration, not a runtime failure to degrade
    # into.
    site_ingest_key: str | None = field(default=None, repr=False)
    # SECRET (repr=False): embeds a capability token in its own path (e.g.
    # "https://news.toomhorvath.com/t/<token>"), used ONLY to build the
    # reader-facing link in the Telegram TL;DR message
    # (f"{base}/d/{digest_id}") -- never sent anywhere itself, never part of
    # the site-publish PUT. Required when the Telegram channel is enabled:
    # a TL;DR notification whose entire purpose is "here's the link" is
    # useless without one.
    site_public_base: str | None = field(default=None, repr=False)
    # SECRET (repr=False): authenticates to the Telegram Bot API
    # (digest/publish.py's send_telegram_tldr). Empty/unset = the Telegram
    # channel is disabled -- mirrors site_publish_url's empty-means-
    # disabled shape, not polymarket_enabled's explicit flag, for the same
    # reason: there is no sane non-empty default for "which bot".
    telegram_notify_bot_token: str | None = field(default=None, repr=False)
    # The target group/channel id (e.g. "-1001234567890"). Not a secret --
    # a chat id alone grants no access -- but required (ConfigError) when
    # telegram_notify_bot_token is set: a bot with nowhere to post is a
    # misconfiguration, not a runtime failure.
    telegram_notify_chat_id: str | None = None
    # Forum-topic thread id within telegram_notify_chat_id. 0 (the default)
    # means "post to the group root" -- send_telegram_tldr omits
    # message_thread_id entirely in that case rather than sending a
    # would-be-invalid 0.
    telegram_notify_thread_id: int = 0
    # Optional SEPARATE forum-topic thread for the daily-brief feature
    # (digest/main.py's `run_daily`): the daily brief goes to its own topic,
    # not the window digests' TL;DR topic, so the two don't interleave.
    # `None` (unset, the default) means "no separate topic configured" --
    # main.py falls back to `telegram_notify_thread_id` with an INFO log,
    # rather than a ConfigError, since a single-topic deployment (the
    # pre-daily-brief norm) is a completely valid configuration, not a
    # misconfiguration. This is deliberately `int | None`, NOT
    # `telegram_notify_thread_id`'s own "0 means unset" convention: 0 is a
    # legitimate EXPLICIT choice here too (post the daily brief to the group
    # root while window TL;DRs go to a topic), and that must be
    # distinguishable from "not configured at all" -- which a 0-means-unset
    # convention could never represent.
    telegram_daily_thread_id: int | None = None
    # Optional SEPARATE forum-topic thread for the weekly-brief feature
    # (digest/main.py's `run_weekly`), one editorial rung up from
    # `telegram_daily_thread_id` immediately above -- same rationale, same
    # None-means-unset/0-means-group-root distinction, just for the weekly
    # topic instead of the daily one: the weekly brief goes to its own
    # topic, not the daily briefs' (or window digests') own topic, so the
    # three don't interleave. `None` (unset, the default) means "no separate
    # topic configured" -- main.py falls back to `telegram_notify_thread_id`
    # with an INFO log, rather than a ConfigError, since a single-topic
    # deployment is a completely valid configuration, not a misconfiguration.
    # Deliberately `int | None`, not a "0 means unset" convention, for the
    # identical reason `telegram_daily_thread_id`'s own comment gives: 0 is a
    # legitimate EXPLICIT choice here too (post the weekly brief to the group
    # root while other channels go to topics), and that must be
    # distinguishable from "not configured at all".
    telegram_weekly_thread_id: int | None = None
    # Reddit collector (digest/collectors/reddit.py). Like x_enabled/
    # polymarket_enabled, this is an explicit on/off flag rather than an
    # empty-means-disabled sentinel -- REDDIT_SUBREDDITS has no natural
    # "unconfigured" shape distinct from "the owner hasn't finished setting
    # Reddit up yet" the way an empty NEWS_FEEDS tuple naturally does, so an
    # explicit flag is the only unambiguous way to represent "collector
    # present but off" (same rationale as polymarket_enabled's own comment).
    reddit_enabled: bool = False
    # SECRET (repr=False): the owner's own logged-in Reddit `reddit_session`
    # browser cookie, sent verbatim as a Cookie header by
    # digest/collectors/reddit.py to old.reddit.com's .json endpoints (see
    # that module's docstring for why -- Reddit's Data Team formally refused
    # this owner's OAuth API application, so app-only OAuth is permanently
    # dead) -- never logged. Required (ConfigError via _require_str) when
    # reddit_enabled is True.
    reddit_session_cookie: str | None = field(default=None, repr=False)
    # Comma-separated subreddit names WITHOUT the "r/" prefix (e.g.
    # "Futurology,LocalLLaMA,MachineLearning,news,hungary"). Required,
    # non-empty, when reddit_enabled is True -- there is no sane default
    # subreddit list the way POLYMARKET_API_BASE has a sane default base URL.
    reddit_subreddits: tuple[str, ...] = ()
    # How many top-of-day posts digest/collectors/reddit.py requests per
    # subreddit per run. 10 is a reasonable default for a personal digest;
    # bounded to [1, 25] for the same "catch a typo/misconfiguration at
    # startup, not deep inside a scheduled run" reason as POLYMARKET_TOP_N.
    reddit_posts_per_sub: int = 10
    # Stable-arc-keys feature (app side): whether the site's ingest validator
    # has ALREADY been updated to accept an optional "key" field on each
    # `topics` entry. Verified 2026-08-10 by reading cloudflare-terraform/
    # workers/news-site/worker.js's validateTopics directly (read-only,
    # sibling repo): unlike validateDigestPayload's top-level fields (which
    # silently ignore an unrecognized key, no ...rest check at all),
    # validateTopics destructures each entry with `const { slug, label,
    # ...rest } = entry` and 400s the WHOLE PUT if `rest` is non-empty -- so
    # sending an unrecognized per-entry "key" field would reject site
    # publish OUTRIGHT, every run, not silently drop just that field. This
    # is why arc keys can't follow `topics`/`deltas`/`source_counts`'s own
    # "send unconditionally, older server versions ignore it" precedent.
    # Defaults TRUE, but only because the site half shipped and deployed
    # FIRST (toom-edge PR #140, version 69be9028): its validateTopics now
    # accepts an optional per-entry `key`. That ordering was mandatory, not
    # incidental -- validateTopics rejects UNKNOWN per-entry fields outright
    # (a `...rest` check), so sending `key` to a site that predates #140
    # would 400 the whole publish rather than degrade, unlike the top-level
    # `deltas` field which older validators simply ignored (PR #63).
    #
    # Kept as a flag rather than hardcoded so a site rollback has a kill
    # switch: set ARC_KEYS_SITE_ENABLED=false and publishes go back to
    # key-less topics immediately, no app rollback needed. Arc keys are
    # derived and persisted locally either way (digest/state.py's
    # `arc_keys` table), so toggling it never needs a backfill.
    arc_keys_site_enabled: bool = True
    # PLAN.md §11.6 "context mode": one-shot, durable background primer per
    # story arc (digest/context.py's `generate_arc_context`), generated only
    # from the DAILY run (digest/main.py's `run_daily`), never from a window
    # run -- see that function's own docstring for the cost-bounding
    # rationale. Defaults FALSE: unlike arc_keys_site_enabled (a rollback
    # switch for an already-approved feature), this is NEW GENERATION work
    # -- another `claude -p` call per qualifying arc, on top of the daily
    # brief's own -- so it stays owner-gated, opt-in, matching
    # verify_daily_enabled's own "new work defaults off" precedent, not
    # arc_keys_site_enabled's "already approved, flag exists only for
    # rollback" one.
    context_enabled: bool = False
    # Bounds how many NEW primers one daily run will generate, regardless of
    # how large the qualifying backlog is (digest/state.py's
    # `get_arc_keys_needing_context` applies this as a SQL `LIMIT`, not a
    # post-hoc Python slice) -- this is what keeps the feature's cost at
    # "at most N claude -p calls per DAY", not per window run, however many
    # arcs happen to cross the 2-appearance recurrence threshold on any
    # given day. 3 is a conservative default: qualifying arcs (a story
    # recurring >=2 times in a trailing 7-day window) are inherently rare, a
    # backlog drains oldest-first-seen across successive days regardless of
    # this cap, and a smaller default costs less to validate live before the
    # owner raises it.
    context_max_per_run: int = 3
    # Same model-tier reasoning as translate_model's own comment: a
    # background primer is 3-5 short paragraphs of durable, mostly-
    # already-known factual prose about a single named place/actor/
    # institution -- no editorial judgment (clustering, weighting, deciding
    # what to cut across many competing stories) the way window/daily
    # summarization needs a frontier model for. "sonnet" is the SAME model
    # ALIAS translate_model defaults to (the `claude` CLI resolves it, see
    # run_claude in digest/summarize.py) -- reusing that exact default
    # string, not a dynamic fallback to `cfg.translate_model`, since the two
    # features are independent config knobs that simply happen to agree on
    # the right tier today; either can be pointed elsewhere later without
    # touching the other.
    context_model: str = "sonnet"
    # Its own timeout, not a reuse of claude_timeout_seconds (contrast
    # translate_digest, which reuses that value outright) -- kept as a
    # SEPARATE knob because this call's shape is quite different: a single
    # short label in, a few short paragraphs out, no large items/coverage
    # payload the way a window/daily summarization call carries. 120s is
    # generous headroom over what a toolless, single-topic completion this
    # small should ever need, while still being far tighter than
    # claude_timeout_seconds' 300s default (sized for up to 200 items) or
    # verify_daily_timeout_seconds' 600s (an agentic, tool-calling loop) --
    # neither of those call shapes applies here.
    context_timeout_seconds: int = 120

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
        digest_from_name = _optional_str("DIGEST_FROM_NAME", default="Digest")

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
        claude_effort = _optional_choice(
            "CLAUDE_EFFORT", default="high", choices=_CLAUDE_EFFORT_CHOICES
        )
        news_feeds = _optional_url_tuple("NEWS_FEEDS")

        translate_hu_enabled = _parse_bool(os.environ.get("TRANSLATE_HU_ENABLED", "false"))
        translate_model = os.environ.get("TRANSLATE_MODEL", "sonnet")
        translate_model_fallback = os.environ.get(
            "TRANSLATE_MODEL_FALLBACK", "claude-sonnet-4-6"
        )

        verify_daily_enabled = _parse_bool(os.environ.get("VERIFY_DAILY_ENABLED", "false"))
        verify_daily_timeout_seconds = _optional_positive_int(
            "VERIFY_DAILY_TIMEOUT_SECONDS", default=600
        )
        verify_daily_max_web_ops = _optional_int_in_range(
            "VERIFY_DAILY_MAX_WEB_OPS", default=20, minimum=1, maximum=100
        )
        # Unset/blank falls back to THIS run's own already-resolved
        # anthropic_model/claude_effort (see the field's own comment for
        # why that's a dynamic fallback, not a second hardcoded literal).
        verify_daily_model = os.environ.get("VERIFY_DAILY_MODEL", "").strip() or anthropic_model
        verify_daily_effort = _optional_choice(
            "VERIFY_DAILY_EFFORT", default=claude_effort, choices=_CLAUDE_EFFORT_CHOICES
        )

        polymarket_enabled = _parse_bool(os.environ.get("POLYMARKET_ENABLED", "false"))
        polymarket_api_base = _optional_url(
            "POLYMARKET_API_BASE", default="https://gamma-api.polymarket.com"
        )
        polymarket_proxy_key = _optional_secret("POLYMARKET_PROXY_KEY")
        polymarket_top_n = _optional_int_in_range(
            "POLYMARKET_TOP_N", default=30, minimum=1, maximum=100
        )
        polymarket_swing_threshold = _optional_float_exclusive_range(
            "POLYMARKET_SWING_THRESHOLD", default=0.15, minimum=0.0, maximum=1.0
        )

        reddit_enabled = _parse_bool(os.environ.get("REDDIT_ENABLED", "false"))
        reddit_session_cookie: str | None = None
        reddit_subreddits: tuple[str, ...] = ()
        reddit_posts_per_sub = _optional_int_in_range(
            "REDDIT_POSTS_PER_SUB", default=10, minimum=1, maximum=25
        )
        if reddit_enabled:
            reddit_session_cookie = _require_str("REDDIT_SESSION_COOKIE")
            reddit_subreddits = _require_subreddit_tuple("REDDIT_SUBREDDITS")

        arc_keys_site_enabled = _parse_bool(os.environ.get("ARC_KEYS_SITE_ENABLED", "true"))

        context_enabled = _parse_bool(os.environ.get("CONTEXT_ENABLED", "false"))
        context_max_per_run = _optional_int_in_range(
            "CONTEXT_MAX_PER_RUN", default=3, minimum=1, maximum=20
        )
        context_model = os.environ.get("CONTEXT_MODEL", "sonnet")
        context_timeout_seconds = _optional_positive_int("CONTEXT_TIMEOUT_SECONDS", default=120)

        email_enabled = _parse_bool(os.environ.get("EMAIL_ENABLED", "true"))

        site_publish_url = _optional_url_or_none("SITE_PUBLISH_URL")
        site_ingest_key = _optional_secret("SITE_INGEST_KEY")
        if site_publish_url is not None and site_ingest_key is None:
            raise ConfigError("SITE_INGEST_KEY is required when SITE_PUBLISH_URL is set")

        telegram_notify_bot_token = _optional_secret("TELEGRAM_NOTIFY_BOT_TOKEN")
        telegram_notify_chat_id = _optional_str_or_none("TELEGRAM_NOTIFY_CHAT_ID")
        if telegram_notify_bot_token is not None and telegram_notify_chat_id is None:
            raise ConfigError(
                "TELEGRAM_NOTIFY_CHAT_ID is required when TELEGRAM_NOTIFY_BOT_TOKEN is set"
            )
        telegram_notify_thread_id = _optional_nonnegative_int(
            "TELEGRAM_NOTIFY_THREAD_ID", default=0
        )
        telegram_daily_thread_id = _optional_nonnegative_int_or_none("TELEGRAM_DAILY_THREAD_ID")
        telegram_weekly_thread_id = _optional_nonnegative_int_or_none("TELEGRAM_WEEKLY_THREAD_ID")

        # site_public_base is validated against the TELEGRAM channel (not the
        # site channel): its only consumer is send_telegram_tldr's reader
        # link (see the field's own comment) -- publish_to_site never reads
        # it, so a site-only deployment (no Telegram) has no need to set it.
        site_public_base = _optional_url_or_none("SITE_PUBLIC_BASE")
        telegram_enabled = telegram_notify_bot_token is not None
        if telegram_enabled and site_public_base is None:
            raise ConfigError("SITE_PUBLIC_BASE is required when the Telegram channel is enabled")

        site_enabled = site_publish_url is not None
        if not (email_enabled or site_enabled or telegram_enabled):
            raise ConfigError(
                "all delivery channels disabled: enable EMAIL_ENABLED, SITE_PUBLISH_URL, "
                "or TELEGRAM_NOTIFY_BOT_TOKEN"
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
            digest_from_name=digest_from_name,
            state_db_path=state_db_path,
            x_enabled=x_enabled,
            x_cookies_path=x_cookies_path,
            x_cookies=x_cookies,
            anthropic_model=anthropic_model,
            archive_dir=archive_dir,
            claude_timeout_seconds=claude_timeout_seconds,
            claude_effort=claude_effort,
            translate_hu_enabled=translate_hu_enabled,
            translate_model=translate_model,
            translate_model_fallback=translate_model_fallback,
            verify_daily_enabled=verify_daily_enabled,
            verify_daily_timeout_seconds=verify_daily_timeout_seconds,
            verify_daily_max_web_ops=verify_daily_max_web_ops,
            verify_daily_model=verify_daily_model,
            verify_daily_effort=verify_daily_effort,
            news_feeds=news_feeds,
            polymarket_enabled=polymarket_enabled,
            polymarket_api_base=polymarket_api_base,
            polymarket_proxy_key=polymarket_proxy_key,
            polymarket_top_n=polymarket_top_n,
            polymarket_swing_threshold=polymarket_swing_threshold,
            reddit_enabled=reddit_enabled,
            reddit_session_cookie=reddit_session_cookie,
            reddit_subreddits=reddit_subreddits,
            reddit_posts_per_sub=reddit_posts_per_sub,
            arc_keys_site_enabled=arc_keys_site_enabled,
            context_enabled=context_enabled,
            context_max_per_run=context_max_per_run,
            context_model=context_model,
            context_timeout_seconds=context_timeout_seconds,
            email_enabled=email_enabled,
            site_publish_url=site_publish_url,
            site_ingest_key=site_ingest_key,
            site_public_base=site_public_base,
            telegram_notify_bot_token=telegram_notify_bot_token,
            telegram_notify_chat_id=telegram_notify_chat_id,
            telegram_notify_thread_id=telegram_notify_thread_id,
            telegram_daily_thread_id=telegram_daily_thread_id,
            telegram_weekly_thread_id=telegram_weekly_thread_id,
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


def _require_subreddit_tuple(name: str) -> tuple[str, ...]:
    """Require a comma-separated list of bare subreddit names (no "r/" prefix).

    Used for REDDIT_SUBREDDITS when REDDIT_ENABLED=true -- there is no sane
    default subreddit list, so an empty/unset value is a ConfigError here,
    not a "collector disabled" sentinel (that role belongs to
    REDDIT_ENABLED itself, mirroring polymarket_enabled's explicit-flag
    shape rather than NEWS_FEEDS' empty-means-disabled one). Each entry must
    match `_SUBREDDIT_NAME_RE` -- see that constant's own comment for why:
    these values are interpolated directly into a request path by
    digest/collectors/reddit.py, so a stray "r/" prefix, slash, or space
    would otherwise surface as an opaque request failure deep inside a
    scheduled run instead of a clear startup error.
    """
    raw = _require_str(name)
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    if not parts:
        raise ConfigError(f"{name} must contain at least one subreddit name")
    for part in parts:
        if not _SUBREDDIT_NAME_RE.fullmatch(part):
            raise ConfigError(
                f"{name} entries must match ^[A-Za-z0-9_]+$ (no 'r/' prefix, no spaces)"
            )
    return parts


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


def _optional_choice(name: str, *, default: str, choices: tuple[str, ...]) -> str:
    """Read an optional string env var, falling back to `default` if unset/blank.

    Also rejects any set value that isn't one of `choices` -- used for
    CLAUDE_EFFORT, which is handed straight into the `claude -p` subprocess
    argv (see run_claude in digest/summarize.py) as `--effort <value>`. The
    CLI itself would reject a bad value, but only after spawning the
    subprocess -- validating here at startup instead means a typo'd
    CLAUDE_EFFORT fails fast, before any collector runs, with a clear error
    naming the valid set, rather than surfacing as an opaque non-zero exit
    from `claude -p` deep inside a scheduled run. Unlike ConfigError's usual
    contract, the offending value IS included in the message here: this
    setting is not a secret, and the invalid value is exactly the actionable
    detail a fixer needs.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip()
    if value not in choices:
        raise ConfigError(
            f"{name} must be one of {', '.join(choices)}, got {value!r}"
        )
    return value


def _optional_str(name: str, *, default: str) -> str:
    """Read an optional string env var, falling back to `default` if unset/blank.

    Used for DIGEST_FROM_NAME, purely cosmetic display text for the email
    From header (see emailer.send_digest, which hands it to
    email.utils.formataddr). It is not handed to a subprocess argv or a URL
    fetch, but it IS embedded into an RFC 5322 header field body, and that
    is its own unsafe shape: an embedded CR or LF (Codex review finding on
    PR #20) makes formataddr build a header value that Python's own email
    Generator refuses to serialize, raising HeaderParseError deep inside
    send_digest -- AFTER create_digest has already durably recorded the
    digest row (PLAN.md §4.1), so every subsequent run's
    get_pending_digests retry branch hits the identical failure and the
    digest is stuck retrying forever, never sending. Validating here at
    startup instead means a value with control characters fails fast,
    before any collector runs, with a clear error -- rather than bricking
    delivery only once a real digest tries to go out. Unlike ConfigError's
    usual contract, the offending value is NOT echoed: unlike CLAUDE_EFFORT,
    the actionable detail here is just "which characters", not the value
    itself, and echoing raw control characters back into a log/error message
    is its own minor hygiene problem.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip()
    if _HEADER_CONTROL_CHAR_RE.search(value):
        raise ConfigError(f"{name} must not contain control characters")
    return value


def _optional_url_tuple(name: str) -> tuple[str, ...]:
    """Read an optional comma-separated list of http(s) URLs, defaulting to ().

    Unset or blank -> `()`, same "not configured" meaning
    digest/main.py's `_run_news_collector` uses to skip the news collector
    entirely without a separate NEWS_ENABLED flag (see Config.news_feeds'
    own comment). Each non-empty entry, after split/strip, must start with
    `http://` or `https://` -- these values are handed straight to
    `urllib.request.Request` (digest/collectors/rss.py), and a typo'd or
    non-URL entry there would surface as an opaque per-feed fetch failure
    deep inside a scheduled run instead of a clear startup error. Unlike
    `_optional_choice`, the offending value is NOT echoed in the
    ConfigError -- feed URLs are owner config, not secrets, but there is no
    strong need to echo them either (contrast CLAUDE_EFFORT, where the
    value IS the actionable detail), so this follows the default
    ConfigError contract of naming only the variable.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return ()
    urls = tuple(p.strip() for p in raw.split(",") if p.strip())
    for url in urls:
        if not url.startswith("http://") and not url.startswith("https://"):
            raise ConfigError(f"{name} entries must each start with http:// or https://")
    return urls


def _optional_url(name: str, *, default: str) -> str:
    """Read an optional http(s) URL env var, falling back to `default`, trailing slash stripped.

    Used for POLYMARKET_API_BASE, which digest/collectors/polymarket.py
    interpolates directly into a request URL as f"{base}/markets?...". A
    typo'd or non-URL value there would surface as an opaque request
    failure deep inside a scheduled run instead of a clear startup error --
    validating the scheme here catches that early, mirroring
    _optional_url_tuple's rationale for NEWS_FEEDS. The trailing slash is
    stripped so that interpolation can never accidentally produce a
    double-slash path ("host//markets") if the owner's env var happens to
    include one -- collectors are written assuming a bare, slash-free base.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip()
    if not value.startswith("http://") and not value.startswith("https://"):
        raise ConfigError(f"{name} must start with http:// or https://")
    return value.rstrip("/")


def _optional_secret(name: str) -> str | None:
    """Read an optional secret env var, defaulting to None. Blank counts as unset.

    Used for POLYMARKET_PROXY_KEY -- an opaque shared-secret header value
    the collector sends to the owner's own Cloudflare Worker proxy
    (digest/collectors/polymarket.py). Unlike every other _optional_*
    helper in this module there is no format to validate (it's an arbitrary
    bearer-style string) and, being a secret, its value must never be
    echoed in any ConfigError -- there is also no failure mode for a plain
    optional string, so no ConfigError path exists here at all.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _optional_int_in_range(name: str, *, default: int, minimum: int, maximum: int) -> int:
    """Read an optional int env var, falling back to `default`, constrained to [minimum, maximum].

    Used for POLYMARKET_TOP_N — the POST-filter cut applied by
    digest/collectors/polymarket.py's `collect` after sports/non-binary
    filtering (it never appears in the request itself; the request always
    fetches that module's fixed _OVERFETCH_LIMIT). A value outside a sane
    range would silently distort how many markets each run can report, so
    it is validated here at startup instead of surfacing as a confusing
    collector-level symptom.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer between {minimum} and {maximum}") from exc
    if not (minimum <= value <= maximum):
        raise ConfigError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _optional_float_exclusive_range(
    name: str, *, default: float, minimum: float, maximum: float
) -> float:
    """Read an optional float env var, falling back to `default`, strictly between minimum/maximum.

    Used for POLYMARKET_SWING_THRESHOLD, an absolute probability-point delta
    (digest/collectors/polymarket.py's swing check). The bounds are
    exclusive on purpose: 0 would make every observed market a "swing"
    every run (the anchor would never actually anchor anything), and 1 (or
    above) could never trigger at all since probabilities are bounded to
    [0, 1] -- both ends are degenerate configurations, not merely unusual
    ones, so they are rejected rather than merely discouraged.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name} must be a number strictly between {minimum} and {maximum}"
        ) from exc
    if not (minimum < value < maximum):
        raise ConfigError(f"{name} must be a number strictly between {minimum} and {maximum}")
    return value


def _optional_url_or_none(name: str) -> str | None:
    """Read an optional http(s) URL env var, defaulting to None, trailing slash stripped.

    Unlike `_optional_url` (which falls back to a caller-supplied non-empty
    default string, e.g. POLYMARKET_API_BASE's real default), an unset/blank
    value here means the feature it configures is simply not turned on --
    used for SITE_PUBLISH_URL (site channel: no sane non-empty default for
    "which Worker to PUT to", mirroring NEWS_FEEDS' empty-means-disabled
    shape) and SITE_PUBLIC_BASE (the reader-link base for the Telegram
    channel, required only when that channel is enabled -- see its own
    ConfigError in from_env). Trailing slash is stripped for the same
    double-slash-avoidance reason as `_optional_url`.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if not value.startswith("http://") and not value.startswith("https://"):
        raise ConfigError(f"{name} must start with http:// or https://")
    return value.rstrip("/")


def _optional_str_or_none(name: str) -> str | None:
    """Read an optional string env var, defaulting to None. Blank counts as unset.

    Used for TELEGRAM_NOTIFY_CHAT_ID -- an opaque group/channel identifier
    with no format this module can usefully validate (Telegram chat ids are
    negative integers for groups/supergroups, but treating this as a bare
    string keeps the config layer agnostic to that detail, which belongs to
    digest/publish.py's Bot API call, not here). Structurally identical to
    `_optional_secret` below, but kept as its own function: this value is
    not a secret (a bare chat id grants no access on its own), whereas
    `_optional_secret` exists specifically to mark ITS callers' values as
    things that must never be echoed -- keeping the two separate documents
    that intent at the call site even though today's implementations match.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _optional_nonnegative_int(name: str, *, default: int) -> int:
    """Read an optional int env var, falling back to `default`, rejecting negative values.

    Used for TELEGRAM_NOTIFY_THREAD_ID -- a forum-topic thread id, where 0
    is a legitimate, meaningful value (see the field's own comment: 0 means
    "post to the group root", not "unset"), unlike CLAUDE_TIMEOUT_SECONDS/
    SMTP_PORT where 0 is nonsensical and rejected by `_optional_positive_int`.
    Only negative values (which Telegram's API could never accept as a
    thread id) are rejected here.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ConfigError(f"{name} must be a non-negative integer")
    return value


def _optional_nonnegative_int_or_none(name: str) -> int | None:
    """Read an optional int env var, defaulting to None, rejecting negative values.

    Used for TELEGRAM_DAILY_THREAD_ID, where -- unlike
    TELEGRAM_NOTIFY_THREAD_ID's `_optional_nonnegative_int` (which folds
    "unset" and "explicitly 0" into the same default) -- "unset" and
    "explicitly 0" must be distinguishable: 0 is a legitimate real thread id
    choice (post to the group root) here too, and main.py's fallback to
    telegram_notify_thread_id must trigger only on genuine absence, never on
    an owner who deliberately set this to 0. Only negative values (which
    Telegram's API could never accept as a thread id) are rejected, mirroring
    `_optional_nonnegative_int`'s own validation.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ConfigError(f"{name} must be a non-negative integer")
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
