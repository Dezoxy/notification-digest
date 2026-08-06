"""One-shot backfill: translate historical digests to Hungarian and re-publish to the site.

Runs INSIDE the deployed notification-digest container (which has the
authenticated claude CLI, digest.env, and the /data state volume), mounted
in as a file and invoked with an overridden entrypoint:

    docker compose --profile oneshot run --rm \
        --entrypoint python \
        -v /srv/appdata/digest/backfill_hu.py:/backfill_hu.py \
        digest /backfill_hu.py --sleep 5

Idempotent: only rows with body_md_hu IS NULL are translated; a re-run
picks up where a crash left off. Translations are stored in the app DB
BEFORE re-publishing (so a publish failure never wastes the model call);
--republish-only re-PUTs every already-translated row without any model
calls, healing exactly that gap.

Deliberately does NOT touch email_sent/telegram_sent/site_published —
this is a site-content upsert, not a delivery-channel operation; the
Worker's ingest is an upsert by id, so re-publishing is always safe.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from digest.config import Config
from digest.emailer import localize_tldr_label_hu, render_body_html
from digest.publish import publish_to_site
from digest.state import connect, get_digest_item_urls, init_db
from digest.translate import translate_digest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill_hu")


def _publish(cfg: Config, conn, digest_id: int, created_at: str, item_count: int,
             body_md: str, body_md_hu: str) -> bool:
    allowed = get_digest_item_urls(conn, digest_id)
    body_html = render_body_html(body_md, allowed)
    body_html_hu = localize_tldr_label_hu(render_body_html(body_md_hu, allowed))
    try:
        publish_to_site(
            digest_id, body_md, body_html, created_at, item_count,
            cfg.site_publish_url, cfg.site_ingest_key,
            body_md_hu=body_md_hu, body_html_hu=body_html_hu,
        )
        return True
    except Exception as exc:
        log.error("digest %d: re-publish failed: %s", digest_id, type(exc).__name__)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="translate at most N digests")
    ap.add_argument("--sleep", type=float, default=5.0, help="seconds between model calls")
    ap.add_argument("--republish-only", action="store_true",
                    help="no model calls; re-publish every row that already has body_md_hu")
    args = ap.parse_args()

    cfg = Config.from_env()
    if not (cfg.site_publish_url and cfg.site_ingest_key):
        log.error("site publish is not configured in this environment")
        return 1

    conn = connect(cfg.state_db_path)
    init_db(conn)

    if args.republish_only:
        rows = conn.execute(
            "SELECT id, created_at, item_count, body_md, body_md_hu FROM digests "
            "WHERE body_md_hu IS NOT NULL ORDER BY id"
        ).fetchall()
        log.info("republish-only: %d digests", len(rows))
        ok = sum(_publish(cfg, conn, *row) for row in rows)
        log.info("done: %d ok, %d failed", ok, len(rows) - ok)
        return 0 if ok == len(rows) else 1

    rows = conn.execute(
        "SELECT id, created_at, item_count, body_md FROM digests "
        "WHERE body_md_hu IS NULL ORDER BY id"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    log.info("backfill: %d digests to translate", len(rows))

    ok = failed = 0
    for i, (digest_id, created_at, item_count, body_md) in enumerate(rows, 1):
        allowed = get_digest_item_urls(conn, digest_id)
        hu = translate_digest(body_md, allowed, cfg.translate_model, cfg.claude_timeout_seconds)
        if hu is None:
            failed += 1
            log.warning("digest %d: translation failed, staying English-only (%d/%d)",
                        digest_id, i, len(rows))
        else:
            conn.execute("UPDATE digests SET body_md_hu = ? WHERE id = ?", (hu, digest_id))
            conn.commit()
            if _publish(cfg, conn, digest_id, created_at, item_count, body_md, hu):
                ok += 1
                log.info("digest %d: translated and re-published (%d/%d)", digest_id, i, len(rows))
            else:
                failed += 1  # translation kept; heal later with --republish-only
        if i < len(rows) and args.sleep:
            time.sleep(args.sleep)

    log.info("backfill done: %d ok, %d failed", ok, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
