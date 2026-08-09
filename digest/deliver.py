"""Channel delivery: per-channel senders, the per-digest channel loop, the
pending-retry pass, and the run-scoped Telegram circuit breaker.

Owns getting an already-recorded digest out the door across its three
channels (email, site, Telegram) -- summarizing and durably persisting a
digest in the first place is main.py's job (see its `_deliver`/`run_daily`),
this module only ever operates on a `digest_id` that already exists in the
`digests` table. main.py's two run modes (`_deliver` for the 3-hourly window
cycle, `run_daily` for the once-a-day brief) both compose `deliver_pending`
and `deliver_channels` from here after producing a digest, threading their
own `TelegramRunState` instance through both calls in the same run (see that
class's docstring for why one shared instance per run matters).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from digest.config import Config
from digest.emailer import localize_tldr_label_hu, render_body_html, send_digest
from digest.publish import (
    TelegramSendError,
    derive_topics,
    parse_failed_sources,
    publish_to_site,
    send_telegram_tldr,
)
from digest.state import (
    get_daily_allowed_urls,
    get_digest_item_urls,
    get_digest_source_counts,
    get_pending_digests,
    mark_digest_sent,
    mark_digest_site_published,
    mark_digest_telegram_sent,
)

# A Telegram TL;DR notification is a REAL-TIME ping, not an archive record --
# announcing a stale digest is pure noise. 12h = 4 digest windows at the
# 3-hourly cadence: generous for ordinary retry-after-a-failed-run catch-up,
# far below "archive dump" territory. Site and email are deliberately NOT
# windowed -- the site is an archive and SHOULD backfill every pending
# digest regardless of age; only a live-ping channel needs this guard.
# Full incident story (why this exists at all):
# docs/incidents/2026-08-06-telegram-flood.md
_TELEGRAM_MAX_AGE = timedelta(hours=12)

logger = logging.getLogger(__name__)


def digest_meta(conn: sqlite3.Connection, digest_id: int) -> tuple[int, str, str | None, str]:
    """Fetch (item_count, created_at, body_md_hu, kind) for an existing digest row.

    Used by the pending-resend path in `deliver_pending`: `get_pending_digests`
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
    unaffected either way (see `deliver_channels`). Only the exception's
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
    per `deliver_channels`'s ordering contract, Telegram is skipped THIS
    run for this digest too (it links to the site page this call just
    failed to publish). Only the exception's type name is logged, never its
    message or the request URL/response body (digest/publish.py's
    `publish_to_site` never logs either itself).

    `kind` ("window" or "daily", default "window") is threaded straight
    through to `publish_to_site`'s own `kind` payload field unchanged -- see
    that function's docstring for why the site wants it (badging a daily
    brief distinctly). The caller (`deliver_channels`) always passes the
    digest's own actual stored kind; the default here only covers a caller
    that doesn't care to pass it, matching this codebase's habit of
    defaulting "window" everywhere `kind` was retrofitted onto an existing
    signature.

    Also computes `source_counts` (digest/state.py's
    `get_digest_source_counts`), `failed_sources` (digest/publish.py's
    `parse_failed_sources`, parsed off this same `body_md`), and `topics`
    (digest/publish.py's `derive_topics`, also derived off this same
    `body_md`) and forwards all three to `publish_to_site`, which decides
    whether to include each in the payload. All three are derived HERE,
    from `conn`/`digest_id`/`body_md`, rather than threaded in by the
    caller -- this covers the fresh-digest and pending-resend delivery
    paths identically, since both ultimately call this same function with
    the digest's own `digest_id`/`body_md`. `topics` is deliberately
    derived from the ENGLISH `body_md` only, never `body_md_hu` -- the site
    stores one topics list per digest, and `derive_topics`' slug-stability
    contract (see its own docstring) needs one single, stable source of
    heading text to fold, not two independently-translated ones that could
    fold to different slugs for what is really the same story.
    """
    body_html = render_body_html(body_md, allowed_urls)
    body_html_hu = (
        localize_tldr_label_hu(render_body_html(body_md_hu, allowed_urls))
        if body_md_hu is not None
        else None
    )
    source_counts = get_digest_source_counts(conn, digest_id)
    failed_sources = parse_failed_sources(body_md)
    topics = derive_topics(body_md)
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
            source_counts=source_counts,
            failed_sources=failed_sources,
            topics=topics,
        )
    except Exception as exc:
        logger.error("site publish failed for digest %d: %s", digest_id, type(exc).__name__)
        return False

    mark_digest_site_published(conn, digest_id)
    return True


class TelegramRunState:
    """Run-scoped Telegram circuit-breaker flag, shared by every digest one `_deliver` call handles.

    GUARD 2 of the 2026-08-06 incident (full story:
    docs/incidents/2026-08-06-telegram-flood.md): once ANY Telegram send in
    a run hits HTTP 429, every remaining Telegram send for the REST OF THAT
    RUN must be skipped -- across both the pending-digests retry loop and
    the freshly-summarized digest in the same `_deliver` call, not just the
    rest of whichever loop iteration tripped it. That requires state that
    outlives a single `_deliver_telegram` call and is visible to every
    later one in the same run, without resorting to
    a module-level global (which would leak across runs/tests and isn't
    thread/asyncio-reentrancy-safe). A single instance, created fresh at the
    top of a run mode in main.py (`_deliver` or `run_daily`) and threaded
    down through `deliver_channels` (directly, or via `deliver_pending`)
    into `_deliver_telegram`, gives exactly that lifetime with none of a
    global's downsides. Deliberately a plain mutable object (not a frozen
    dataclass, not a bool return-value threaded back up): every call site
    needs to both read and write the SAME flag, and passing a bool by value
    around a loop would lose the mutation the moment it happened inside one
    `_deliver_telegram` call.
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
    telegram_state: TelegramRunState,
    kind: str = "window",
) -> bool:
    """Send the Telegram TL;DR channel for one digest. Returns True on success.

    Two guards run BEFORE any network call is attempted, both added after
    the 2026-08-06 flood incident (full story:
    docs/incidents/2026-08-06-telegram-flood.md):

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
    latter is exactly why `get_pending_digests`/`digest_meta` had to start
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


def deliver_channels(
    conn: sqlite3.Connection,
    cfg: Config,
    digest_id: int,
    body_md: str,
    item_count: int,
    created_at: str,
    done: dict[str, bool],
    telegram_state: TelegramRunState,
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
    `TelegramRunState`'s docstring for why it has to be the SAME instance
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

    # Per-channel status for the digest_delivery log line at the bottom of
    # this function -- seeded from the same enabled/entry-done facts as the
    # *_done variables above, then overwritten below only inside the branch
    # that actually attempts (or explicitly skips) that channel this call.
    email_status = "disabled" if not email_enabled else "done" if email_done else None
    site_status = "disabled" if not site_enabled else "done" if site_done else None
    telegram_status = "disabled" if not telegram_enabled else "done" if telegram_done else None

    # A "daily" digest stamps NO items of its own (it consumes a day's worth
    # of window digests, never raw items -- see state.py's create_digest
    # `kind` docstring), so get_digest_item_urls against it always returns
    # the empty set -- deriving the allowlist that way here unconditionally
    # used to defang EVERY citation link on a daily brief, on every channel
    # (the bug get_daily_allowed_urls exists to fix; see its docstring).
    # Both email and site still receive the SAME set either way -- they
    # must render identical content, see this function's own docstring.
    allowed_urls = (
        get_daily_allowed_urls(conn, created_at)
        if kind == "daily"
        else get_digest_item_urls(conn, digest_id)
    )

    if email_enabled and not email_done:
        email_done = _deliver_email(conn, cfg, digest_id, body_md, item_count, allowed_urls)
        email_status = "sent" if email_done else "failed"

    if site_enabled and not site_done:
        site_done = _deliver_site(
            conn, cfg, digest_id, body_md, item_count, created_at, allowed_urls, body_md_hu,
            kind=kind,
        )
        site_status = "sent" if site_done else "failed"

    if telegram_enabled and not telegram_done:
        if site_enabled and not site_done:
            logger.info(
                "digest %d: skipping telegram this run, site publish not done", digest_id
            )
            telegram_status = "skipped"
        else:
            telegram_done = _deliver_telegram(
                conn, cfg, digest_id, body_md, created_at, telegram_state, kind=kind
            )
            telegram_status = "sent" if telegram_done else "failed"

    # Loki-queryable per-channel outcome for this one digest -- the
    # human-readable per-failure logs above (_deliver_email/_deliver_site/
    # _deliver_telegram) stay as they are; this line is what lets a Loki
    # query answer "what happened to each channel of digest N" (or "which
    # channel kind keeps failing") without a log dive across those separate
    # lines.
    logger.info(
        "digest_delivery %s",
        json.dumps(
            {
                "digest_id": digest_id,
                "kind": kind,
                "email": email_status,
                "site": site_status,
                "telegram": telegram_status,
            },
            sort_keys=True,
        ),
    )

    return email_done and site_done and telegram_done


def deliver_pending(
    conn: sqlite3.Connection, cfg: Config, telegram_state: TelegramRunState
) -> bool:
    """Retry channel delivery for every pending digest (any kind), oldest first.

    Shared by `_deliver` (the 3-hourly run's own pending-resend pass) and
    `run_daily` (so a daily brief left partially delivered by a previous
    day's run gets retried before today's new brief is even summarized) --
    extracted specifically so this retry logic has exactly one
    implementation, rather than the window and daily run modes silently
    drifting apart on how a pending digest gets retried.

    `telegram_state` is the caller's OWN `TelegramRunState` instance (see
    its docstring) -- shared across both this pending pass and whatever
    fresh digest the caller summarizes afterward in the SAME run, so a 429
    hit retrying an old digest here also stops that later fresh send, and
    vice versa.

    Does not filter on digest `kind`: `get_pending_digests` returns every
    digest (window or daily) with at least one ENABLED channel still
    undelivered, and a pending "daily" row is exactly as valid a retry
    candidate as a pending "window" one. The digest's own stored `kind`
    (now returned by `get_pending_digests` alongside everything else -- see
    its docstring) is threaded into `deliver_channels` so the retry picks
    the right Telegram thread (window vs daily topic -- see
    `_telegram_thread_id_for_kind`) instead of silently defaulting to
    "window".

    Returns True iff every enabled channel of every pending digest is done
    (already was, or just succeeded) by the time this returns -- mirrors
    `deliver_channels`'s own per-digest contract, ANDed across every
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
        item_count, created_at, body_md_hu, _kind = digest_meta(conn, digest_id)
        ok = deliver_channels(
            conn, cfg, digest_id, body_md, item_count, created_at, done, telegram_state, body_md_hu,
            kind=kind,
        )
        all_ok = all_ok and ok
    return all_ok
