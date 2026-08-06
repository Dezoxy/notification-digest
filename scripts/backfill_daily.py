"""One-shot backfill: synthesize daily briefs for past days and publish them to the site.

Runs INSIDE the deployed notification-digest container (same mount trick as
scripts/backfill_translate_hu.py — scripts/ is not baked into the image):

    docker compose --profile oneshot run --rm \
        --entrypoint python \
        -v /srv/appdata/digest/backfill_daily.py:/app/backfill_daily.py \
        digest /app/backfill_daily.py --sleep 10

For every past 20:00-Europe/Budapest boundary from the earliest window
digest up to (but excluding) the most recent 20:00, it takes the window
digests in the preceding 24h and runs the SAME summarize_daily pipeline the
live 20:00 timer uses — framed at that historical evening via the `now`
override — plus the optional Hungarian translation.

Rows are inserted with created_at = the historical 20:00 (UTC ISO), so they
sort into their real evenings on the site (the Worker orders by created_at).
email_sent and telegram_sent are preset to 1 — HISTORY NEVER ANNOUNCES; the
12h freshness guard would enforce that anyway, this just makes it explicit
and keeps the rows out of the pending pass entirely. site_published starts 0
and is marked only after a successful direct publish, so a failed publish is
healed automatically by the next timer run's pending pass.

Idempotent: a boundary is skipped when a kind='daily' row already exists
within ±6h of it (covers both re-runs of this script and live briefs).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from digest.config import Config
from digest.daily import summarize_daily
from digest.emailer import localize_tldr_label_hu, render_body_html
from digest.publish import publish_to_site
from digest.state import connect, get_digest_item_urls, init_db
from digest.summarize import SummarizeError
from digest.translate import translate_digest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_daily")

_BUDAPEST = ZoneInfo("Europe/Budapest")


def _boundaries(conn) -> list[datetime]:
    """Every past 20:00-Budapest instant (as aware UTC) from the earliest window digest onward."""
    row = conn.execute(
        "SELECT MIN(created_at) FROM digests WHERE kind = 'window'"
    ).fetchone()
    if not row or not row[0]:
        return []
    earliest = datetime.fromisoformat(row[0])
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=UTC)
    now = datetime.now(UTC)

    first_local = earliest.astimezone(_BUDAPEST)
    boundary_local = datetime.combine(first_local.date(), dtime(20, 0), tzinfo=_BUDAPEST)
    if boundary_local.astimezone(UTC) <= earliest:
        boundary_local += timedelta(days=1)

    out = []
    while boundary_local.astimezone(UTC) < now:
        out.append(boundary_local.astimezone(UTC))
        boundary_local += timedelta(days=1)
    # The most recent past 20:00 belongs to the LIVE timer once deployed —
    # exclude it only if it is still in the future relative to deploy? No:
    # if it already passed with no live brief, backfilling it is correct;
    # the idempotency check below skips it when a live brief exists.
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleep", type=float, default=10.0, help="seconds between model calls")
    ap.add_argument("--limit", type=int, default=None, help="backfill at most N days")
    args = ap.parse_args()

    cfg = Config.from_env()
    if not (cfg.site_publish_url and cfg.site_ingest_key):
        log.error("site publish is not configured in this environment")
        return 1

    conn = connect(cfg.state_db_path)
    init_db(conn)

    boundaries = _boundaries(conn)
    if args.limit:
        boundaries = boundaries[-args.limit :]
    log.info("candidate boundaries: %d", len(boundaries))

    ok = failed = skipped = 0
    for i, boundary in enumerate(boundaries, 1):
        b_iso = boundary.isoformat()
        exists = conn.execute(
            "SELECT COUNT(*) FROM digests WHERE kind = 'daily' AND created_at BETWEEN ? AND ?",
            (
                (boundary - timedelta(hours=6)).isoformat(),
                (boundary + timedelta(hours=6)).isoformat(),
            ),
        ).fetchone()[0]
        if exists:
            skipped += 1
            continue

        since = (boundary - timedelta(hours=24)).isoformat()
        rows = conn.execute(
            "SELECT id, created_at, item_count, body_md FROM digests "
            "WHERE kind = 'window' AND created_at >= ? AND created_at < ? ORDER BY id ASC",
            (since, b_iso),
        ).fetchall()
        if not rows:
            skipped += 1
            continue

        allowed: set[str] = set()
        for digest_id, _c, _n, _b in rows:
            allowed |= get_digest_item_urls(conn, digest_id)
        item_total = sum(r[2] for r in rows)

        try:
            body_md = summarize_daily(
                rows, allowed, cfg.anthropic_model, cfg.claude_timeout_seconds,
                cfg.claude_effort, now=boundary,
            )
        except SummarizeError as exc:
            failed += 1
            log.warning("boundary %s: synthesis failed: %s", b_iso, type(exc).__name__)
            continue

        body_md_hu = None
        if cfg.translate_hu_enabled:
            body_md_hu = translate_digest(
                body_md, allowed, cfg.translate_model, cfg.claude_timeout_seconds
            )

        cur = conn.execute(
            "INSERT INTO digests (created_at, item_count, email_sent, site_published, "
            "telegram_sent, body_md, body_md_hu, kind) VALUES (?, ?, 1, 0, 1, ?, ?, 'daily')",
            (b_iso, item_total, body_md, body_md_hu),
        )
        conn.commit()
        new_id = cur.lastrowid

        body_html = render_body_html(body_md, allowed)
        body_html_hu = (
            localize_tldr_label_hu(render_body_html(body_md_hu, allowed)) if body_md_hu else None
        )
        try:
            publish_to_site(
                new_id, body_md, body_html, b_iso, item_total,
                cfg.site_publish_url, cfg.site_ingest_key,
                body_md_hu=body_md_hu, body_html_hu=body_html_hu, kind="daily",
            )
            conn.execute("UPDATE digests SET site_published = 1 WHERE id = ?", (new_id,))
            conn.commit()
            ok += 1
            log.info("boundary %s -> daily brief #%d from %d briefings (%d/%d)",
                     b_iso, new_id, len(rows), i, len(boundaries))
        except Exception as exc:
            failed += 1  # row stays site_published=0; the timer's pending pass heals it
            log.error("boundary %s: publish failed: %s (will retry via pending pass)",
                      b_iso, type(exc).__name__)

        if i < len(boundaries) and args.sleep:
            time.sleep(args.sleep)

    log.info("daily backfill done: %d created, %d failed, %d skipped", ok, failed, skipped)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
