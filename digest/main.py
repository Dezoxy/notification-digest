"""Entrypoint: python -m digest.main — orchestrates one collection run.

Phase 2: Telegram collection + state persistence, then summarization
(§4.4) and email delivery (§4.5) of PLAN.md.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from digest.collectors import polymarket as polymarket_collector
from digest.collectors import reddit as reddit_collector
from digest.collectors import rss as rss_collector
from digest.collectors import telegram as telegram_collector
from digest.collectors import x as x_collector
from digest.collectors.polymarket import PolymarketCollectResult
from digest.collectors.telegram import CollectResult
from digest.config import Config, ConfigError
from digest.daily import summarize_daily
from digest.emailer import archive, localize_tldr_label_hu, render_body_html, send_digest
from digest.publish import TelegramSendError, publish_to_site, send_telegram_tldr
from digest.state import (
    commit_new_items,
    connect,
    count_unsummarized_items,
    create_digest,
    get_cursors,
    get_digest_item_urls,
    get_pending_digests,
    get_polymarket_probs,
    get_recent_digests,
    get_unsummarized_items,
    get_window_digests_since,
    init_db,
    mark_digest_sent,
    mark_digest_site_published,
    mark_digest_telegram_sent,
)
from digest.summarize import (
    _MAX_PROMPT_BYTES,
    SummarizeError,
    format_recent_coverage,
    select_items_for_prompt,
    summarize,
)
from digest.translate import translate_digest

# How far back _deliver looks for prior digests when building the
# {{RECENT_COVERAGE}} prompt block (digest/summarize.py's
# format_recent_coverage) -- the "running story memory" that lets the
# summarizer write delta-only updates for stories it already covered instead
# of re-explaining them every 3 hours. 24 hours is a full day's worth of
# briefings (8 runs at the 3-hourly cadence) -- long enough that a story
# spanning a slow news day is still recognized as "already covered" on its
# second or third mention, short enough that genuinely stale coverage
# eventually ages out and stops suppressing a fresh full write-up.
_RECENT_COVERAGE_WINDOW = timedelta(hours=24)

# How far back run_daily looks for window digests to synthesize into one
# daily brief (digest/state.py's get_window_digests_since). A full 24 hours
# so a brief run at 20:00 covers exactly "since yesterday's brief", with no
# gap or overlap at the boundary -- the scheduling itself (the second
# systemd timer that invokes `python -m digest daily`) lives outside this
# repo, so this window is what actually defines "one day" from this code's
# point of view.
_DAILY_LOOKBACK_WINDOW = timedelta(hours=24)

# A Telegram TL;DR notification is a REAL-TIME ping, not an archive record --
# announcing a stale digest is pure noise. This is not hypothetical: on
# 2026-08-06 the first run after the multi-channel delivery cutover found
# ~50 pre-cutover digests with telegram_sent=0 (the ALTER TABLE migration in
# state.py's `_migrate_add_telegram_sent_column` defaults the new column to
# 0 for every pre-existing row -- correct for site publish, which SHOULD
# backfill, but wrong for a live notification channel), attempted a Telegram
# sendMessage for every one of them oldest-first, and got rate-limited by
# Telegram (HTTP 429) after about 20 messages -- flooding the group topic
# with hours-old TL;DRs across two consecutive runs, both of which then
# exited non-zero on top of it. Any backlog scenario can reproduce this
# shape: a column-add migration defaulting old rows to unsent (exactly what
# happened here), a restored DB backup, the Telegram channel re-enabled
# after a pause, or a long site outage queueing up retries -- none of them
# should ever flood the topic with old news. 12h = 4 digest windows at the
# 3-hourly cadence: generous for ordinary retry-after-a-failed-run catch-up,
# far below "archive dump" territory. Site and email are deliberately NOT
# windowed -- the site is an archive and SHOULD backfill every pending
# digest regardless of age (that was correct and desirable in this very same
# incident: only Telegram flooded, because only Telegram is a live-ping
# channel, not an archive).
_TELEGRAM_MAX_AGE = timedelta(hours=12)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Bounds how many unsummarized items a single Claude call is given. Each
# allowlisted chat can contribute up to 500 messages per run, and a failed
# summarize call carries the backlog forward plus new items on top -- with
# no cap, the prompt for an accumulated backlog can exceed the model's
# context window, and an oversized prompt then fails every subsequent run
# forever (no items ever get stamped). The remainder ships in later runs:
# the 3-hourly timer is the drain loop for a large backlog, at
# _MAX_ITEMS_PER_DIGEST items per digest.
_MAX_ITEMS_PER_DIGEST = 200


async def _client_ready(client: TelegramClient) -> bool:
    """Connect without triggering Telethon's interactive login prompt.

    `async with client` calls `start()`, which prompts for phone/code on an
    invalid session -- fatal in a headless one-shot container. This instead
    connects and checks authorization explicitly, so an invalid session is
    reported as "not ready" rather than blocking on stdin or raising before
    `collect`'s error handling is in scope.
    """
    try:
        await client.connect()
        return await client.is_user_authorized()
    except Exception as exc:
        logger.warning("telegram connect failed: %s", type(exc).__name__)
        return False


def _digest_meta(conn: sqlite3.Connection, digest_id: int) -> tuple[int, str, str | None, str]:
    """Fetch (item_count, created_at, body_md_hu, kind) for an existing digest row.

    Used by the pending-resend path in `_deliver_pending`: `get_pending_digests`
    returns just `(digest_id, body_md, done, kind)` (see its docstring), so a
    pending digest's item_count/created_at/body_md_hu -- needed for the email
    subject, the site/Telegram channels' payloads, and the site channel's
    optional Hungarian fields respectively -- have to be read back
    separately. This is also how a pending resend automatically carries its
    Hungarian translation forward with no extra wiring: `body_md_hu` was
    already durably stored by `create_digest` at this digest's original
    creation (or is NULL if translation was disabled/failed then), so simply
    reading the row back here reproduces it exactly, without this function
    needing to know anything about `translate_digest` or TRANSLATE_HU_ENABLED
    itself. The fresh-digest path also calls this (after `create_digest`) to
    read back `created_at` on the SAME clock convention pending digests use,
    even though it already computed `body_md_hu` moments earlier -- reading
    it back here rather than threading the local variable through keeps both
    paths going through one code path for this data.

    `kind` ("window" or "daily") is returned alongside the other three
    fields for the identical reason `get_pending_digests` now exposes it:
    `_deliver_telegram` needs to know which digest kind it's sending in
    order to pick the right Telegram thread (digest/config.py's
    `telegram_daily_thread_id` vs `telegram_notify_thread_id`), and that
    decision must be correct on BOTH the fresh-digest path (which already
    knows the kind it just created) and the pending-resend path (which does
    not, until it reads the row back here).
    """
    row = conn.execute(
        "SELECT item_count, created_at, body_md_hu, kind FROM digests WHERE id = ?", (digest_id,)
    ).fetchone()
    return row[0], row[1], row[2], row[3]


def _deliver_email(
    conn: sqlite3.Connection,
    cfg: Config,
    digest_id: int,
    body_md: str,
    item_count: int,
    allowed_urls: Collection[str],
) -> bool:
    """Render+send the email channel for one digest. Returns True on success.

    Subject and the HTML masthead's `generated_at_label` are both rendered
    from THIS RUN's own clock (`datetime.now(UTC)`), not the digest's
    `created_at` -- exactly like the pre-multi-channel `_send_and_finalize`
    did, including for a pending resend: the reader should see when the
    email actually went out, not when the digest was originally summarized.
    Both are converted to Europe/Budapest at this render-time boundary per
    CLAUDE.md (storage stays UTC).

    On failure the digest row is left `email_sent = 0` (the caller never
    marks this channel), so the next run's `get_pending_digests` pass
    retries EXACTLY this channel -- site/Telegram, if also enabled, are
    unaffected either way (see `_deliver_channels`). Only the exception's
    type name is logged, never its message: SMTP errors can echo
    credentials or message content.
    """
    now_local = datetime.now(UTC).astimezone(ZoneInfo("Europe/Budapest"))
    subject = f"digest: {item_count} items · {now_local:%Y-%m-%d %H:%M}"
    # %-d (no leading zero) is a glibc/BSD strftime extension, not POSIX --
    # but it's the same extension on both macOS (BSD libc) and the Linux
    # container this actually deploys to (glibc), so it's safe here despite
    # not being portable in general.
    generated_at_label = f"{now_local:%a, %b %-d · %H:%M}"
    try:
        send_digest(
            cfg.smtp_host,
            cfg.smtp_port,
            cfg.smtp_user,
            cfg.smtp_password,
            cfg.digest_from,
            cfg.digest_from_name,
            cfg.digest_to,
            subject,
            body_md,
            allowed_urls,
            generated_at_label,
        )
    except Exception as exc:
        logger.error("email delivery failed for digest %d: %s", digest_id, type(exc).__name__)
        return False

    mark_digest_sent(conn, digest_id)
    return True


def _deliver_site(
    conn: sqlite3.Connection,
    cfg: Config,
    digest_id: int,
    body_md: str,
    item_count: int,
    created_at: str,
    allowed_urls: Collection[str],
    body_md_hu: str | None = None,
    kind: str = "window",
) -> bool:
    """Render+publish the site channel for one digest. Returns True on success.

    `render_body_html` is called here (not passed in) so the site channel
    always gets HTML rendered against ITS OWN correctly-scoped
    `allowed_urls` -- the digest's own stamped item URLs, identical to what
    the email channel's HTML part uses (see digest/emailer.py's
    `render_body_html` docstring: both channels must show the same
    sanitized content, byte-for-byte).

    `body_md_hu`, when not None, is rendered to HTML here too (same
    `render_body_html` call, same `allowed_urls` -- a translation must not
    invent new links, so it is checked for provenance against the identical
    URL set the English body is) and both are handed to `publish_to_site`,
    which decides whether to include them in the ingest payload (see that
    function's "all three or none" contract). This mirrors the English
    body's own division of labor exactly: THIS function renders HTML for
    both languages, `publish_to_site` derives the summary fields
    (tldr/tldr_hu, section_count, has_attention) from whichever markdown
    bodies it's given -- there is no markdown rendering inside
    digest/publish.py at all, for either language.

    The Hungarian HTML additionally gets `localize_tldr_label_hu` applied
    (digest/emailer.py) -- owner decision: the Hungarian site page must read
    fully Hungarian, including the TL;DR callout's visible label, even
    though the underlying MARKDOWN keeps the literal English "**TL;DR:**"
    marker unchanged (see that function's own docstring for why the marker
    itself must never be touched). This is applied ONLY to `body_html_hu`,
    never to the English `body_html` above -- the English site page, the
    email, and the Telegram TL;DR message (which reads `extract_tldr` off
    the ENGLISH `body_md` regardless of whether a translation exists at all,
    see `_deliver_telegram`) all keep the English label untouched.

    On failure the digest row is left `site_published = 0`, so the next
    run's `get_pending_digests` pass retries exactly this channel -- and,
    per `_deliver_channels`'s ordering contract, Telegram is skipped THIS
    run for this digest too (it links to the site page this call just
    failed to publish). Only the exception's type name is logged, never its
    message or the request URL/response body (digest/publish.py's
    `publish_to_site` never logs either itself).

    `kind` ("window" or "daily", default "window") is threaded straight
    through to `publish_to_site`'s own `kind` payload field unchanged -- see
    that function's docstring for why the site wants it (badging a daily
    brief distinctly). The caller (`_deliver_channels`) always passes the
    digest's own actual stored kind; the default here only covers a caller
    that doesn't care to pass it, matching this codebase's habit of
    defaulting "window" everywhere `kind` was retrofitted onto an existing
    signature.
    """
    body_html = render_body_html(body_md, allowed_urls)
    body_html_hu = (
        localize_tldr_label_hu(render_body_html(body_md_hu, allowed_urls))
        if body_md_hu is not None
        else None
    )
    try:
        publish_to_site(
            digest_id,
            body_md,
            body_html,
            created_at,
            item_count,
            cfg.site_publish_url,
            cfg.site_ingest_key,
            body_md_hu=body_md_hu,
            body_html_hu=body_html_hu,
            kind=kind,
        )
    except Exception as exc:
        logger.error("site publish failed for digest %d: %s", digest_id, type(exc).__name__)
        return False

    mark_digest_site_published(conn, digest_id)
    return True


class _TelegramRunState:
    """Run-scoped Telegram circuit-breaker flag, shared by every digest one `_deliver` call handles.

    GUARD 2 of the 2026-08-06 incident (see `_TELEGRAM_MAX_AGE`'s neighboring
    comment): once ANY Telegram send in a run hits HTTP 429, every remaining
    Telegram send for the REST OF THAT RUN must be skipped -- across both the
    pending-digests retry loop and the freshly-summarized digest in the same
    `_deliver` call, not just the rest of whichever loop iteration tripped
    it. That requires state that outlives a single `_deliver_telegram` call
    and is visible to every later one in the same run, without resorting to
    a module-level global (which would leak across runs/tests and isn't
    thread/asyncio-reentrancy-safe). A single `_deliver`-scoped instance,
    created fresh at the top of that function and threaded down through
    `_deliver_channels` into `_deliver_telegram`, gives exactly that lifetime
    with none of a global's downsides. Deliberately a plain mutable object
    (not a frozen dataclass, not a bool return-value threaded back up): every
    call site needs to both read and write the SAME flag, and passing a bool
    by value around a loop would lose the mutation the moment it happened
    inside one `_deliver_telegram` call.
    """

    def __init__(self) -> None:
        self.rate_limited = False


def _telegram_thread_id_for_kind(cfg: Config, kind: str) -> int:
    """Pick the Telegram forum-topic thread id for a digest's `kind` ("window" or "daily").

    A "daily" digest goes to `cfg.telegram_daily_thread_id` when the owner
    configured one -- a separate topic so daily briefs don't interleave with
    the window digests' own TL;DR topic. When unset (`None`, the default),
    this falls back to `cfg.telegram_notify_thread_id` -- the same topic
    every digest used before the daily-brief feature existed -- and logs an
    INFO line noting the fallback, since a single-topic deployment is a
    valid, unremarkable configuration, not a misconfiguration worth a
    WARNING or ConfigError. Every other `kind` (currently only "window")
    always uses `cfg.telegram_notify_thread_id` unconditionally; there is
    only one non-default kind to special-case today.
    """
    if kind == "daily":
        if cfg.telegram_daily_thread_id is not None:
            return cfg.telegram_daily_thread_id
        logger.info(
            "TELEGRAM_DAILY_THREAD_ID unset, falling back to the window digest's "
            "telegram thread for this daily brief"
        )
    return cfg.telegram_notify_thread_id


def _deliver_telegram(
    conn: sqlite3.Connection,
    cfg: Config,
    digest_id: int,
    body_md: str,
    created_at: str,
    telegram_state: _TelegramRunState,
    kind: str = "window",
) -> bool:
    """Send the Telegram TL;DR channel for one digest. Returns True on success.

    Two guards run BEFORE any network call is attempted, both added after
    the 2026-08-06 flood incident (see `_TELEGRAM_MAX_AGE`'s comment for the
    full story):

    GUARD 1 -- freshness window: if `created_at` is older than
    `_TELEGRAM_MAX_AGE`, this digest is never sent to Telegram at all. It is
    instead marked `telegram_sent` directly (skipping `send_telegram_tldr`
    entirely) and this function returns True -- a stale digest silently
    "catching up" is exactly the failure mode this guard exists to prevent,
    so the channel is treated as successfully done, not failed, once this
    decision is made. `created_at` is parsed the same way
    digest/summarize.py's `_format_digest_age` does (including its
    naive-timestamp-treated-as-UTC fallback), since every `created_at` this
    codebase writes is the identical `datetime.now(UTC).isoformat()` shape
    (state.py's `create_digest`).

    GUARD 2 -- per-run 429 circuit breaker: if `telegram_state.rate_limited`
    is already set (an EARLIER digest in this same run hit a 429), this
    digest's send is skipped without even attempting it, and this function
    returns False -- unlike GUARD 1, this is a real failure: the digest
    still needs to go out, just not this run, so the caller's overall result
    must reflect that (the incident's non-zero exit / OnFailure alert must
    still fire). If THIS digest's own send is the one that comes back with a
    429 (`TelegramSendError.status == 429`), `telegram_state.rate_limited` is
    flipped here so every LATER digest in this run also skips, and a single
    WARNING is logged at the moment the breaker trips -- never once per
    skipped digest afterward, since this branch is unreachable once the flag
    is already set (see the early-return above).

    On any other failure the digest row is left `telegram_sent = 0`, retried
    by a later run's `get_pending_digests` pass exactly like the other two
    channels. `send_telegram_tldr` already guarantees its own raised message
    (and any log line it emits) never includes the response body or the
    bot-token-bearing request URL -- this function additionally only logs
    the exception's TYPE NAME, never `str(exc)`, as one more layer against
    that secret ever reaching a log line.

    `kind` ("window" or "daily", default "window") selects which forum
    topic this send targets, via `_telegram_thread_id_for_kind` -- see that
    function's docstring. The caller always passes the digest's own actual
    stored kind, on both the fresh-digest and pending-resend paths (the
    latter is exactly why `get_pending_digests`/`_digest_meta` had to start
    exposing `kind` at all: a pending "daily" row retried in a LATER run
    must still land in the daily topic, not silently fall back to "window"'s
    default here).
    """
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if datetime.now(UTC) - created > _TELEGRAM_MAX_AGE:
        logger.info(
            "digest %d too old for telegram announcement, marking sent without notifying",
            digest_id,
        )
        mark_digest_telegram_sent(conn, digest_id)
        return True

    if telegram_state.rate_limited:
        return False

    try:
        send_telegram_tldr(
            digest_id,
            body_md,
            created_at,
            cfg.telegram_notify_bot_token,
            cfg.telegram_notify_chat_id,
            _telegram_thread_id_for_kind(cfg, kind),
            cfg.site_public_base,
        )
    except TelegramSendError as exc:
        if exc.status == 429:
            telegram_state.rate_limited = True
            logger.warning(
                "telegram rate limited (429); skipping remaining telegram sends this run"
            )
        logger.error("telegram notify failed for digest %d: %s", digest_id, type(exc).__name__)
        return False
    except Exception as exc:
        logger.error("telegram notify failed for digest %d: %s", digest_id, type(exc).__name__)
        return False

    mark_digest_telegram_sent(conn, digest_id)
    return True


def _deliver_channels(
    conn: sqlite3.Connection,
    cfg: Config,
    digest_id: int,
    body_md: str,
    item_count: int,
    created_at: str,
    done: dict[str, bool],
    telegram_state: _TelegramRunState,
    body_md_hu: str | None = None,
    kind: str = "window",
) -> bool:
    """Attempt every ENABLED, not-yet-done channel for one digest, independently.

    `body_md_hu`, when not None, is passed straight through to
    `_deliver_site` -- the only channel that carries a Hungarian field (see
    that function's docstring). Email and Telegram are unaffected: email
    only ever sends the English body, and Telegram's message is a short
    TL;DR pointer to the site page, not a full body -- there is no Hungarian
    variant of either.

    `telegram_state` is purely passed through to `_deliver_telegram` -- see
    `_TelegramRunState`'s docstring for why it has to be the SAME instance
    across every digest `_deliver` handles in one run, not a fresh one per
    call here.

    `done` is the digest's current per-channel completion state (from
    `get_pending_digests`, or `{"email": False, "site": False, "telegram":
    False}` for a brand-new digest -- see `_deliver`). A channel that is
    DISABLED (per `cfg`) or already `done` is skipped entirely: it is never
    attempted, never logged as a failure, and never counted against this
    digest's overall success.

    Each attempted channel gets its own try/except (`_deliver_email`/
    `_deliver_site`/`_deliver_telegram`) and, on success, marks its OWN flag
    immediately via its own `mark_digest_*` call -- each of which is its own
    tiny commit (see digest/state.py). This is what makes the three channels
    independent: email failing can never roll back a site publish that
    already succeeded moments earlier in this same call, because that
    success was already durably committed before email was even attempted.
    A failure only logs (exception type name only, never a message that
    could carry credentials or a bot-token-bearing URL) and this function
    moves on to the next channel.

    Ordering: site is attempted BEFORE Telegram. This is a real dependency,
    not an arbitrary choice -- the Telegram message links to the site's own
    page for this digest (`f"{public_base}/d/{digest_id}"`,
    digest/publish.py's `send_telegram_tldr`). If the site channel is
    ENABLED for this run and still NOT done after its own attempt above
    (either it just failed, or it was already pending from an earlier run
    and this call didn't even reach it because it's disabled -- see below),
    Telegram is skipped for this digest THIS RUN rather than sending a link
    to a page that doesn't exist yet; it will be retried automatically next
    run once site publish succeeds. This skip does NOT apply when the site
    channel is disabled entirely (`cfg.site_publish_url` unset) -- an
    intentionally site-less deployment must not have Telegram permanently
    blocked by a channel it never intended to use.

    Returns True iff every ENABLED channel for this digest is done (already
    was, or just succeeded) by the time this returns -- a disabled channel
    trivially counts as "done" for this purpose, since there is nothing left
    for it to accomplish.

    `kind` ("window" or "daily", default "window") is passed straight
    through to `_deliver_site` (the site's `kind` payload field) and
    `_deliver_telegram` (which forum topic to send to) -- neither channel's
    ENABLED-ness, done-ness, or ordering logic above depends on it at all;
    it only changes what those two channels DO once they're already decided
    to run.
    """
    email_enabled = cfg.email_enabled
    site_enabled = cfg.site_publish_url is not None
    telegram_enabled = cfg.telegram_notify_bot_token is not None

    email_done = done["email"] or not email_enabled
    site_done = done["site"] or not site_enabled
    telegram_done = done["telegram"] or not telegram_enabled

    allowed_urls = get_digest_item_urls(conn, digest_id)

    if email_enabled and not email_done:
        email_done = _deliver_email(conn, cfg, digest_id, body_md, item_count, allowed_urls)

    if site_enabled and not site_done:
        site_done = _deliver_site(
            conn, cfg, digest_id, body_md, item_count, created_at, allowed_urls, body_md_hu,
            kind=kind,
        )

    if telegram_enabled and not telegram_done:
        if site_enabled and not site_done:
            logger.info(
                "digest %d: skipping telegram this run, site publish not done", digest_id
            )
        else:
            telegram_done = _deliver_telegram(
                conn, cfg, digest_id, body_md, created_at, telegram_state, kind=kind
            )

    return email_done and site_done and telegram_done


def _deliver_pending(
    conn: sqlite3.Connection, cfg: Config, telegram_state: _TelegramRunState
) -> bool:
    """Retry channel delivery for every pending digest (any kind), oldest first.

    Shared by `_deliver` (the 3-hourly run's own pending-resend pass) and
    `run_daily` (so a daily brief left partially delivered by a previous
    day's run gets retried before today's new brief is even summarized) --
    extracted specifically so this retry logic has exactly one
    implementation, rather than the window and daily run modes silently
    drifting apart on how a pending digest gets retried.

    `telegram_state` is the caller's OWN `_TelegramRunState` instance (see
    its docstring) -- shared across both this pending pass and whatever
    fresh digest the caller summarizes afterward in the SAME run, so a 429
    hit retrying an old digest here also stops that later fresh send, and
    vice versa.

    Does not filter on digest `kind`: `get_pending_digests` returns every
    digest (window or daily) with at least one ENABLED channel still
    undelivered, and a pending "daily" row is exactly as valid a retry
    candidate as a pending "window" one. The digest's own stored `kind`
    (now returned by `get_pending_digests` alongside everything else -- see
    its docstring) is threaded into `_deliver_channels` so the retry picks
    the right Telegram thread (window vs daily topic -- see
    `_telegram_thread_id_for_kind`) instead of silently defaulting to
    "window".

    Returns True iff every enabled channel of every pending digest is done
    (already was, or just succeeded) by the time this returns -- mirrors
    `_deliver_channels`'s own per-digest contract, ANDed across every
    pending digest. A failure retrying one pending digest does NOT
    short-circuit the rest: channels (and digests) are independent by
    design, so a broken SMTP path must not stop a healthy site/Telegram
    channel from delivering a DIFFERENT pending digest.
    """
    email_enabled = cfg.email_enabled
    site_enabled = cfg.site_publish_url is not None
    telegram_enabled = cfg.telegram_notify_bot_token is not None

    all_ok = True
    pending = get_pending_digests(conn, email_enabled, site_enabled, telegram_enabled)
    for digest_id, body_md, done, kind in pending:
        logger.info("retrying delivery of digest %d (kind=%s)", digest_id, kind)
        item_count, created_at, body_md_hu, _kind = _digest_meta(conn, digest_id)
        ok = _deliver_channels(
            conn, cfg, digest_id, body_md, item_count, created_at, done, telegram_state, body_md_hu,
            kind=kind,
        )
        all_ok = all_ok and ok
    return all_ok


def _deliver(
    conn: sqlite3.Connection, cfg: Config, failed_sources: list[str]
) -> bool:
    """Post-collection delivery: retry every pending digest, then summarize+deliver new items.

    (a) Every digest with at least one ENABLED channel still undelivered
        (`_deliver_pending`, oldest first -- crash/API failure on a
        previous run) has channel delivery retried for it. Unlike the
        pre-multi-channel version of this function, a failure here does NOT
        short-circuit the rest of this function: channels are independent
        by design (a broken SMTP path must not stop a healthy site/Telegram
        channel from delivering a DIFFERENT pending digest, or this run's
        own freshly summarized one), so every pending digest is attempted
        and the overall result is the AND of every attempt.
    (b) No unsummarized items -- nothing left to send, this is a normal
        empty-window run (or the pending pass already covered everything).
    (c) Otherwise: summarize with the CURRENT run's failed_sources, attempt
        the optional Hungarian translation (digest/translate.py's
        translate_digest, soft-failing -- see its own docstring), durably
        record the digest (English body plus whatever translation resulted,
        possibly None) and archive it (BEFORE attempting any channel -- see
        below), then attempt all its channels.

    Archiving: `archive()` is called exactly once, immediately after
    `create_digest` durably records a NEW digest -- never on the
    pending-resend path (that digest was already archived when it was first
    created, in a previous run) and never gated on any channel's success or
    failure. This is a deliberate decoupling from the old single-channel
    behavior, where archiving only happened after a successful email send:
    with EMAIL_ENABLED=false a digest could otherwise never be archived at
    all, even though the site/Telegram channels delivered it just fine.

    `failed_sources` is a list of collector names (e.g. `["telegram"]`,
    `["x"]`, or `["telegram", "x"]`) built by the caller from each
    collector's own CollectResult.failed -- not a single bool -- so a
    Telegram-only failure and an X-only failure produce distinct banner
    text (PLAN.md §5) instead of collapsing to one undifferentiated flag.

    Returns True iff every enabled channel of every digest handled this run
    (pending and freshly created alike) succeeded. Deliberately does NOT
    factor in whether `failed_sources` is non-empty -- the caller combines
    this with the collectors' own failure flags, because ANY collector
    failure must surface as a non-zero exit (the sole signal for the Loki
    alert on digest.service) even on a run that delivers nothing at all.

    `telegram_state` (a single `_TelegramRunState`, see its docstring) is
    created once here and threaded through both `_deliver_pending` and the
    freshly-summarized digest's own `_deliver_channels` call further down --
    so GUARD 2's circuit breaker (main.py's per-run 429 handling) sees every
    Telegram send this run makes as one shared sequence, not a fresh breaker
    per digest.
    """
    telegram_state = _TelegramRunState()
    all_ok = _deliver_pending(conn, cfg, telegram_state)

    items = get_unsummarized_items(conn, limit=_MAX_ITEMS_PER_DIGEST)
    if not items:
        logger.info("no unsummarized items, nothing to send")
        return all_ok

    # "Recently covered" continuity context (digest/summarize.py's
    # format_recent_coverage): every digest created in the last
    # _RECENT_COVERAGE_WINDOW, INCLUDING an unsent one still awaiting the
    # pending pass above -- see get_recent_digests' docstring for why
    # email_sent is deliberately not part of the filter. `now`/`since` are
    # both UTC, per CLAUDE.md's storage-stays-UTC convention (created_at is
    # stored as `datetime.now(UTC).isoformat()`, see state.py's create_digest
    # -- comparing against a UTC `since` keeps that comparison meaningful).
    now = datetime.now(UTC)
    since = now - _RECENT_COVERAGE_WINDOW
    recent_coverage = format_recent_coverage(get_recent_digests(conn, since.isoformat()), now)

    # Shrink to whatever actually fits in one prompt BEFORE both summarize()
    # and create_digest(): the item-count cap above (_MAX_ITEMS_PER_DIGEST)
    # bounds source characters, but json.dumps(ensure_ascii=False) still lets
    # an emoji/CJK-heavy batch serialize to far more UTF-8 BYTES than that
    # count implies, and select_items_for_prompt (measured in bytes, not
    # characters -- see _MAX_PROMPT_BYTES) is what catches that. The shrink
    # has to happen HERE, not inside summarize(), because create_digest stamps
    # whatever list it's given as "handled" -- if summarize() only saw a
    # trimmed subset internally while create_digest stamped the full
    # pre-shrink `items`, the untrimmed remainder would be marked summarized
    # without ever actually being sent to the model. Keeping the shrink in
    # _deliver and passing its result to both calls keeps the summarized set
    # and the stamped set identical by construction. `recent_coverage` is
    # passed through here too: it is embedded in every built prompt exactly
    # like the items are, so its bytes count toward _MAX_PROMPT_BYTES
    # automatically (see select_items_for_prompt's docstring).
    items = select_items_for_prompt(items, failed_sources, recent_coverage, _MAX_PROMPT_BYTES)

    try:
        body_md = summarize(
            items,
            failed_sources,
            recent_coverage,
            cfg.anthropic_model,
            cfg.claude_timeout_seconds,
            cfg.claude_effort,
        )
    except SummarizeError as exc:
        logger.error("summarization failed: %s", exc)
        return False

    # Hungarian translation: a soft-failing PRODUCTION step, run AFTER
    # summarize() succeeds and BEFORE create_digest so the translation (or
    # its absence) is captured in the same durable insert as everything
    # else about this digest -- never a delivery channel of its own, and
    # never allowed to block or delay the English digest (see
    # digest/translate.py's translate_digest docstring). `None` on failure
    # or when the flag is off simply means this digest stays English-only
    # forever; translate_digest already logs its own WARNING on failure, so
    # nothing further is logged here.
    body_md_hu: str | None = None
    if cfg.translate_hu_enabled:
        body_md_hu = translate_digest(
            body_md,
            {item.url for item in items},
            cfg.translate_model,
            cfg.claude_timeout_seconds,
            fallback_model=cfg.translate_model_fallback,
        )

    digest_id = create_digest(conn, body_md, items, body_md_hu=body_md_hu)
    archive(body_md, cfg.archive_dir, digest_id)

    item_count, created_at, body_md_hu, kind = _digest_meta(conn, digest_id)
    done = {"email": False, "site": False, "telegram": False}
    ok = _deliver_channels(
        conn, cfg, digest_id, body_md, item_count, created_at, done, telegram_state, body_md_hu,
        kind=kind,
    )
    all_ok = all_ok and ok

    # One Opus call per run keeps cost and runtime bounded -- do NOT loop
    # summarize here even if a remainder is left; the 3-hourly timer is the
    # drain loop that picks up the rest on its next invocation.
    remaining = count_unsummarized_items(conn)
    if remaining:
        logger.info("%d unsummarized items remain, will ship in the next digest", remaining)
    return all_ok


async def _run_x_collector(conn: sqlite3.Connection, cfg: Config) -> CollectResult:
    """Build the X client and run one notifications-page collect, if enabled.

    No-ops entirely (returns a fresh, unfailed CollectResult without ever
    importing twikit) when `cfg.x_enabled` is False -- the caller only
    invokes this when `cfg.x_enabled` is True, but the check is repeated
    here so this function is safe to call unconditionally too. Any
    exception raised while BUILDING the client (e.g. a malformed cookies
    file/JSON, or the cookies path not existing) is treated the same as an
    in-collector failure: logged (type name only -- the underlying error
    could embed cookie material) and flagged, never allowed to crash the
    run. This mirrors telegram.py's `_client_ready`-then-collect split,
    where a client that can't even be constructed/authorized is just
    another shape of collector failure.

    `x_collector.collect` is already exception-proof internally (Codex
    review finding A -- see its docstring), but the call is wrapped in a
    catch-all here too as a second line of defense: this mirrors how
    `_run`'s telegram path can never raise past `_collect_one_chat`/
    `_client_ready` either, so a not-yet-anticipated bug in the collector
    still can't take down the whole run (and Telegram's already-collected
    items) before `commit_new_items` gets a chance to persist them.
    """
    if not cfg.x_enabled:
        return CollectResult()

    x_cursors = get_cursors(conn, "x")
    try:
        client = x_collector.build_client(cfg.x_cookies_path, cfg.x_cookies)
    except Exception as exc:
        logger.warning("x client setup failed: %s", type(exc).__name__)
        return CollectResult(failed=True)

    try:
        return await x_collector.collect(client, x_cursors)
    except Exception as exc:
        logger.warning("x collection crashed unexpectedly: %s", type(exc).__name__)
        return CollectResult(failed=True)


def _run_news_collector(cfg: Config) -> CollectResult:
    """Run one RSS/Atom collect pass, if any feeds are configured.

    No-ops entirely (returns a fresh, unfailed CollectResult) when
    `cfg.news_feeds` is empty -- there is no separate NEWS_ENABLED flag
    (see Config.news_feeds' own comment), so an empty tuple IS "disabled".

    Plain `def`, called synchronously from inside this `async def _run` --
    not `await`ed, no executor indirection. Collectors already run
    sequentially, one at a time (telegram, then x, then news); nothing else
    is in flight while this runs, so there is nothing for an
    `asyncio.to_thread`/executor wrapper to protect against blocking, and
    adding one would just be indirection with no payoff.

    `rss_collector.collect` is already exception-proof internally (see its
    module docstring -- one bad feed is caught per-feed), but this call is
    wrapped in a catch-all here too as a second line of defense, mirroring
    `_run_x_collector`'s own rationale: a not-yet-anticipated bug in the
    collector still must not take down the whole run (and Telegram/X's
    already-collected items) before `commit_new_items` gets a chance to
    persist them. Only the exception's type name is logged -- consistent
    with how every other collector-crash log line in this module avoids
    echoing exception text that could embed response content.
    """
    if not cfg.news_feeds:
        return CollectResult()

    try:
        return rss_collector.collect(cfg.news_feeds)
    except Exception as exc:
        logger.warning("news collection crashed unexpectedly: %s", type(exc).__name__)
        return CollectResult(failed=True)


def _run_polymarket_collector(
    conn: sqlite3.Connection, cfg: Config
) -> PolymarketCollectResult:
    """Run one Polymarket swing-detection pass, if `cfg.polymarket_enabled`.

    No-ops entirely (returns a fresh, unfailed PolymarketCollectResult)
    when the flag is off -- mirrors `_run_x_collector`'s own flag check,
    not `_run_news_collector`'s empty-tuple-means-disabled shape, because
    Polymarket has no natural "unconfigured" sentinel the way an empty feed
    list does (see Config.polymarket_enabled's own comment).

    The stored-anchor lookup is injected as a closure over `conn`
    (`digest.collectors.polymarket.collect`'s `get_stored_probs` parameter)
    rather than handing the collector the connection directly -- keeps that
    module testable purely against a fake urllib, with no real database in
    the loop, exactly like `get_cursors` is pre-fetched and handed to
    `x_collector.collect` above (the difference here is Polymarket can only
    know WHICH market ids to look up after the fetch, so the lookup itself,
    not just its result, has to be passed through).

    Plain `def`, called synchronously from inside this `async def _run` --
    not `await`ed, matching `_run_news_collector`'s own rationale
    (collectors already run one at a time; there's nothing else in flight
    for an executor wrapper to protect against blocking).

    `polymarket_collector.collect` already never raises past its own
    try/except around the single network request (module docstring's
    "Failure semantics"), but this call is wrapped in a catch-all here too
    as a second line of defense, mirroring every other collector wrapper in
    this module: a not-yet-anticipated bug must not take down the whole run
    (and the other collectors' already-collected items) before
    `commit_new_items` gets a chance to persist them.
    """
    if not cfg.polymarket_enabled:
        return PolymarketCollectResult()

    try:
        return polymarket_collector.collect(
            cfg.polymarket_api_base,
            cfg.polymarket_proxy_key,
            cfg.polymarket_top_n,
            cfg.polymarket_swing_threshold,
            lambda market_ids: get_polymarket_probs(conn, market_ids),
        )
    except Exception as exc:
        logger.warning("polymarket collection crashed unexpectedly: %s", type(exc).__name__)
        return PolymarketCollectResult(failed=True)


def _run_reddit_collector(cfg: Config) -> CollectResult:
    """Run one Reddit top-of-day collect pass, if `cfg.reddit_enabled`.

    No-ops entirely (returns a fresh, unfailed CollectResult) when the flag
    is off -- mirrors `_run_polymarket_collector`'s own flag check, not
    `_run_news_collector`'s empty-tuple-means-disabled shape: Reddit has no
    natural "unconfigured" sentinel the way an empty feed list does (see
    Config.reddit_enabled's own comment).

    Plain `def`, called synchronously from inside this `async def _run` --
    matching `_run_news_collector`/`_run_polymarket_collector`'s own
    rationale (collectors already run one at a time; there's nothing else
    in flight for an executor wrapper to protect against blocking).

    `reddit_collector.collect` already never raises past its own try/except
    around the session verify and each per-subreddit request (module
    docstring's "Failure semantics"), but this call is wrapped in a
    catch-all here too as a second line of defense, mirroring every other
    collector wrapper in this module: a not-yet-anticipated bug must not
    take down the whole run (and the other collectors' already-collected
    items) before `commit_new_items` gets a chance to persist them. Only the
    exception's type name is logged -- the underlying error could otherwise
    embed the session cookie from a malformed request.
    """
    if not cfg.reddit_enabled:
        return CollectResult()

    try:
        return reddit_collector.collect(
            cfg.reddit_session_cookie,
            cfg.reddit_subreddits,
            cfg.reddit_posts_per_sub,
        )
    except Exception as exc:
        logger.warning("reddit collection crashed unexpectedly: %s", type(exc).__name__)
        return CollectResult(failed=True)


async def _run(cfg: Config) -> bool:
    """Run one collection + delivery cycle. Returns True if it completed without failure."""
    conn = connect(cfg.state_db_path)
    try:
        init_db(conn)

        tg_cursors = get_cursors(conn, "telegram")

        client = TelegramClient(StringSession(cfg.tg_session), cfg.tg_api_id, cfg.tg_api_hash)
        try:
            if not await _client_ready(client):
                logger.warning("telegram session not authorized / connect failed")
                tg_result = telegram_collector.CollectResult(failed=True)
            else:
                tg_result = await telegram_collector.collect(
                    client, cfg.tg_chat_allowlist, tg_cursors
                )
        finally:
            if client.is_connected():
                await client.disconnect()

        x_result = await _run_x_collector(conn, cfg)
        news_result = _run_news_collector(cfg)
        polymarket_result = _run_polymarket_collector(conn, cfg)
        reddit_result = _run_reddit_collector(cfg)

        items = (
            tg_result.items
            + x_result.items
            + news_result.items
            + polymarket_result.items
            + reddit_result.items
        )
        # news never contributes cursor_updates (it has no cursor axis, see
        # digest/collectors/rss.py's module docstring) -- merging its
        # (always-empty) dict in here anyway keeps this line generic over
        # every collector rather than special-casing the one with nothing
        # to add. reddit is identical (see digest/collectors/reddit.py's
        # module docstring, "No cursor axis"). polymarket ALSO has no cursor
        # axis (its own state lives in the polymarket_probs table, see
        # digest/collectors/polymarket.py's module docstring), but unlike
        # news/reddit it doesn't even have a cursor_updates field on its
        # result type -- PolymarketCollectResult is a distinct type carrying
        # `prob_updates` instead (handled below, not here).
        cursor_updates = {
            **tg_result.cursor_updates,
            **x_result.cursor_updates,
            **news_result.cursor_updates,
            **reddit_result.cursor_updates,
        }

        # `polymarket_prob_updates` is only passed as a keyword argument when
        # non-empty: commit_new_items' signature already defaults it to None
        # for every other caller, but keeping this call itself two-shaped
        # (rather than always passing the kwarg, even as `{}` or `None`)
        # means a run where the collector is disabled or found nothing to
        # anchor is byte-for-byte the same call this function made before
        # Polymarket existed.
        if polymarket_result.prob_updates:
            inserted = commit_new_items(
                conn,
                items,
                cursor_updates,
                polymarket_prob_updates=polymarket_result.prob_updates,
            )
        else:
            inserted = commit_new_items(conn, items, cursor_updates)
        logger.info(
            "collected %d new items (%d inserted), cursors advanced for %d scopes",
            len(items),
            inserted,
            len(cursor_updates),
        )

        failed_sources = [
            source
            for source, failed in (
                ("telegram", tg_result.failed),
                ("x", x_result.failed),
                ("news", news_result.failed),
                ("polymarket", polymarket_result.failed),
                ("reddit", reddit_result.failed),
            )
            if failed
        ]

        delivered = _deliver(conn, cfg, failed_sources)
        return delivered and not failed_sources
    finally:
        conn.close()


def run_daily(cfg: Config) -> bool:
    """Run the once-a-day brief synthesis + delivery cycle. Returns True if it completed OK.

    Invoked by `python -m digest daily` -- a SEPARATE systemd timer on the
    homelab side (not this repo, see CLAUDE.md's Deploy note) fires this once
    a day, independently of the every-3-hours `python -m digest` timer that
    drives `_run`/`_deliver` above. Deliberately synchronous (plain `def`,
    no `asyncio.run` at the call site in `main()`): unlike `_run`, this mode
    never touches Telethon/twikit -- it only reads already-collected digests
    back out of SQLite and drives the same synchronous summarize/translate/
    deliver machinery `_deliver` does, so there is nothing here that needs an
    event loop.

    Pipeline:
    1. Connect/init_db, then run `_deliver_pending` FIRST -- exactly like
       `_deliver` does for the window-digest run mode, and reusing that same
       helper rather than duplicating its retry logic -- so a previous
       day's daily brief that got summarized but only partially delivered
       (e.g. site published, Telegram 429'd) is retried before today's new
       brief is even summarized. This also opportunistically retries any
       still-pending WINDOW digest this run happens to see, which is
       harmless (idempotent per channel) even though the every-3-hours job
       already covers that case on its own schedule.
    2. `rows = get_window_digests_since(conn, since)`, `since` being
       `_DAILY_LOOKBACK_WINDOW` (24h) before now -- the day's worth of
       already-curated window briefings to synthesize (kind='window' only;
       see that function's docstring for why a prior daily brief can never
       feed a later one). Empty `rows` (no window digest ran in the last
       24h -- e.g. a very early deploy, or the window job was down all day)
       is logged and returns `all_ok` from step 1 as-is: nothing to brief is
       a normal empty day, not a failure, exactly like `_deliver`'s own
       "no unsummarized items" branch.
    3. `allowed_urls` is the UNION of `get_digest_item_urls` over every
       source window digest -- a daily brief may cite any URL any of its
       source briefings could cite, never a fresh set of raw item URLs (a
       daily brief never sees raw items at all).
    4. `summarize_daily` (digest/daily.py) -- UNLIKE `_deliver`'s own
       `summarize()` call, a `SummarizeError` here is caught, logged, and
       turned into a `False` return rather than propagating further: this
       function's contract (like `_deliver`'s) is "return whether the run
       succeeded", not "raise on failure".
    5. The optional Hungarian translation -- identical `translate_digest`
       call, identical soft-failing contract, as `_deliver` uses for a
       window digest.
    6. `create_digest(..., items=[], kind="daily", item_count=...)` -- a
       daily brief stamps NO items (it consumes digests, not items; the
       empty-items path is exercised and supported, see digest/state.py's
       `create_digest` and its test coverage) but its stored `item_count` is
       explicitly overridden to the SUM of the source window digests' own
       item_counts (see `create_digest`'s docstring for why the override
       parameter exists) -- that sum is what the site's "N items" line and
       the closing-line count sanity actually describe for a daily brief,
       not "0 items" (which `len(items)` would otherwise store) and not any
       single source digest's own count.
    7. `archive()` it, exactly like any digest -- unconditional, not gated
       on any channel's success, identical rationale to `_deliver`'s own
       archiving.
    8. `_deliver_channels` with a FRESH `done` map (this digest was just
       created, nothing attempted yet) and this function's own
       `telegram_state` -- shared with step 1's `_deliver_pending` call, for
       the identical GUARD-2-circuit-breaker reason `_deliver` shares one
       `_TelegramRunState` across its own two `_deliver_channels` call
       sites.

    Returns True iff step 1's pending pass AND this run's own fresh delivery
    (when a brief was actually produced) both succeeded -- the AND of the
    same two-part contract `_deliver` upholds for the window-digest run mode.
    """
    conn = connect(cfg.state_db_path)
    try:
        init_db(conn)

        telegram_state = _TelegramRunState()
        all_ok = _deliver_pending(conn, cfg, telegram_state)

        now = datetime.now(UTC)
        since = now - _DAILY_LOOKBACK_WINDOW
        rows = get_window_digests_since(conn, since.isoformat())
        if not rows:
            logger.info("no window digests in the last 24 hours, nothing to brief today")
            return all_ok

        allowed_urls: set[str] = set()
        for source_digest_id, _created_at, _item_count, _body_md in rows:
            allowed_urls |= get_digest_item_urls(conn, source_digest_id)

        try:
            body_md = summarize_daily(
                rows,
                allowed_urls,
                cfg.anthropic_model,
                cfg.claude_timeout_seconds,
                cfg.claude_effort,
            )
        except SummarizeError as exc:
            logger.error("daily brief summarization failed: %s", exc)
            return False

        # Hungarian translation: same optional, soft-failing production step
        # `_deliver` runs for a window digest -- see that call site's own
        # comment for the full rationale, identical here.
        body_md_hu: str | None = None
        if cfg.translate_hu_enabled:
            body_md_hu = translate_digest(
                body_md,
                allowed_urls,
                cfg.translate_model,
                cfg.claude_timeout_seconds,
                fallback_model=cfg.translate_model_fallback,
            )

        total_items = sum(item_count for _, _, item_count, _ in rows)
        digest_id = create_digest(
            conn, body_md, [], body_md_hu=body_md_hu, kind="daily", item_count=total_items
        )
        archive(body_md, cfg.archive_dir, digest_id)

        item_count, created_at, body_md_hu, kind = _digest_meta(conn, digest_id)
        done = {"email": False, "site": False, "telegram": False}
        ok = _deliver_channels(
            conn, cfg, digest_id, body_md, item_count, created_at, done, telegram_state, body_md_hu,
            kind=kind,
        )
        return all_ok and ok
    finally:
        conn.close()


def main() -> None:
    load_dotenv()

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    # argv-based mode dispatch: `python -m digest daily` runs the once-a-day
    # brief (run_daily); no argument (or anything else) keeps today's
    # behavior exactly -- the every-3-hours collect+deliver cycle (_run),
    # unchanged. The scheduling itself (which timer fires which mode, and
    # when) lives entirely outside this repo (see CLAUDE.md's Deploy note);
    # this is just the dispatch a systemd unit's ExecStart invokes into.
    if len(sys.argv) > 1 and sys.argv[1] == "daily":
        ok = run_daily(cfg)
    else:
        ok = asyncio.run(_run(cfg))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
