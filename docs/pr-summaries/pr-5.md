# PR #5: Phase 3: X notifications collector behind X_ENABLED
[View PR](https://github.com/Dezoxy/notification-digest/pull/5) · @Dezoxy · merged 2026-07-29 · `287a344` · `phase-3-x-collector -> main` · +2607/-31 across 8 files

## Description
## Summary
- `digest/collectors/x.py` — twikit-based notifications collector: cookie session only (never login), ONE page per 3-hourly run (no pagination loops — gentleness is the primary ban-risk mitigation), snowflake cursor scoped to ("x", "notifications"), first run seeds from newest with no backfill, auth/locked/suspended/rate-limit errors flag the run failed with no retry. All twikit imports are lazy: `X_ENABLED=false` runs have zero twikit import side effects.
- twikit API usage is grounded in the installed package source (methods, notification/tweet/user fields, exception classes — documented in the module docstring), not guessed.
- `config.py` — `X_ENABLED=true` requires exactly one of `X_COOKIES_PATH` / `X_COOKIES`.
- `main.py` — X items/cursors merge into the same atomic commit as Telegram's; `failed_sources` is now per-collector, so the digest banner names exactly which source failed.
- 191 tests (fake client/notification objects mirroring confirmed twikit shapes; no network).

## Intentional semantics
The cursor tracks **linked tweet ids**: notifications that carry a new tweet (mentions, replies, quotes) become digest items; engagement events on old tweets (likes/RTs) and tweetless events (follows) advance nothing and are skipped with a logged count — the digest is for readable content, not engagement counters. Notification ids themselves are not used for ordering because their format isn't a confirmed numeric snowflake.

## Not in this PR
Live verification against real X requires the owner's exported cookies (deferred, like Telegram's live test). Deploy can ship X-disabled.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

## Commits (8)
- [abd4fe4](https://github.com/Dezoxy/notification-digest/commit/abd4fe460d699835c7d407e3e3860a6eb6513432) Phase 3: X notifications collector behind X_ENABLED
- [69f92ec](https://github.com/Dezoxy/notification-digest/commit/69f92ecfca9b0c5b9d57ea31d7936403bc544a6a) Silence js2py's py3.12 deprecation warning in pytest (third-party import noise)
- [6e80b97](https://github.com/Dezoxy/notification-digest/commit/6e80b9742457cd86cfaa45136f9daefa58ccaa2f) X collector: bounded pagination until the cursor is bridged
- [4e9d1e8](https://github.com/Dezoxy/notification-digest/commit/4e9d1e86f6884216b5156a8ad76714526f8fdfa5) Fix Codex findings: contain normalization failures, kind-based engagement filter
- [bb92df5](https://github.com/Dezoxy/notification-digest/commit/bb92df588c7cf1ad16e2fd09b5493ea2ac54816e) Fix Codex P1s: cursor axis is notification chronology, gap-preserving cap
- [3c3f7fb](https://github.com/Dezoxy/notification-digest/commit/3c3f7fb56edf0dd0f1b7c6b294c19be1c217cc0e) Fix Codex P1s: failures never advance the cursor; cap truncation is deliberate
- [3795fb3](https://github.com/Dezoxy/notification-digest/commit/3795fb3b7457420739771129787aa239b448368c) Fix Codex findings: systemic-parse detection, equal-timestamp boundary safety
- [8708e90](https://github.com/Dezoxy/notification-digest/commit/8708e90f9a9b5a839c004166a3ea9c0ec0548099) Fix Codex findings: systemic check before first-run seed, seed at newest+1

## Files (8)
- `tests/test_x_collector.py` `+1199/-0`
- `digest/collectors/x.py` `+983/-0`
- `tests/test_main.py` `+237/-10`
- `digest/main.py` `+79/-18`
- `tests/test_config.py` `+64/-0`
- `digest/config.py` `+32/-0`
- `.env.example` `+8/-3`
- `pyproject.toml` `+5/-0`

## Review findings (12)
- **[P1]** Contain normalization failures inside the X collector — `digest/collectors/x.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673326547)
- **[P1]** Fail the page when notification parsing breaks systemically — `digest/collectors/x.py:330` (resolved) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673766811)
  - fix: Fixed: tweet-linked normalization successes/failures are counted run-wide; failures > 0 with zero successes is treated as systemic (endpoint/shape change) — failed=True, no cursor advance, warning naming the malformed count. Isolated malformations among successes keep the skip-and-count behavior. Test: test_all_tweet_linked_notifications_malformed_is_systemic_failure_no_cursor_advance.
- **[P1]** Preserve the cursor gap when the page cap is reached — `digest/collectors/x.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673413252)
  - fix: Fixed: an unbridged cap-hit now advances the cursor only to the OLDEST fetched timestamp — the gap below the fetched region stays above the high-water mark, successive runs re-walk the overlap (INSERT..DO NOTHING dedup makes it free) and drain it. Natural exhaustion (next_cursor exhausted before the cap) is explicitly distinguished and advances to newest as before. Two-phase regression test: test_unbridged_cap_followup_collect_re_walks_and_extends_past_prior_reach.
- **[P1]** Do not bridge using linked tweet IDs — `digest/collectors/x.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673413248)
  - fix: Fixed: the cursor axis is now notification timestamp_ms — the axis the All timeline is actually ordered by (confirmed in twikit source: timestampMs, always present, int). Bridging compares timestamps kind-agnostically, so a fresh like on an old tweet no longer falsely bridges past an unseen page-2 mention. Regression test: test_old_tweet_fresh_like_on_page1_does_not_falsely_bridge_past_page2_mention.
- **[P1]** Preserve the cursor when pagination is incomplete — `digest/collectors/x.py` (unresolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673595990)
- **[P1]** Exclude engagement events when detecting the cursor boundary — `digest/collectors/x.py` (unresolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673595996)
- **[P1]** Preserve the cursor when pagination fails — `digest/collectors/x.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673629324)
  - fix: Fixed: a pagination failure now advances NOTHING — no cursor entry at all; already-gathered items still ship (they were > cursor; the next run re-walks the overlap and the (source, source_id) constraint dedups). Tests: test_pagination_failure_on_second_page_keeps_first_page_items_cursor_not_advanced (+ the iterator-raising sibling updated to the same semantics).
- **[P1]** Avoid advancing the watermark into a truncated backlog — `digest/collectors/x.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673629332)
  - fix: Fixed differently than suggested, as a documented product decision: you're right that the oldest-fetched watermark never actually drained the gap (equality-bridging stops at the boundary), so that machinery is deleted. Rather than resumable pagination state (X cursor-token lifetime across sessions is an unknown-fragility surface for marginal value), an unbridged cap now advances to the newest fetched timestamp and the truncation is LOUD: a warning names the exact skipped range (old cursor -> oldest fetched). Rationale in the docstring: this is a digest, not an archive — in a sustained >200-notifications/3h burst the oldest tail is the least valuable content, and the failure mode is an explicit logged trade-off, never silent loss. Tests: test_pagination_page_cap_reached_unbridged_cursor_advances_to_newest_and_warns, test_followup_after_unbridged_cap_collects_only_newer_notifications.
- **[P1]** Check systemic parse failures before seeding the cursor — `digest/collectors/x.py:791` (resolved) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673876753)
  - fix: Fixed: a shared systemic-failure predicate is evaluated BEFORE the first-run seed — first-enable during a shape break now fails loudly with no cursor seeded (scope stays first-run for the next attempt). A first run with zero tweet-linked notifications (only follow events) is correctly non-systemic and seeds normally. Tests: test_first_run_all_tweet_linked_malformed_is_systemic_failure_no_seed + tweetless-seed regression.
- **[P2]** Exclude engagement notifications by kind, not tweet age — `digest/collectors/x.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673326554)
  - fix: Fixed in 4e9d1e8: engagement is now excluded by KIND (heart_icon/retweet_icon), not tweet age — a like on your own post-cursor tweet no longer emails your own words back. Unrecognized/missing icon kinds fail open to content (a missed mention is worse than an occasional own-tweet; the icon-id vocabulary is X's undocumented convention, provenance documented in the module). Engagement still advances chronology. Tests: like/repost skipped-as-item cases, mention/reply included, fail-open cases.
- **[P2]** Preserve notifications that share the cursor timestamp — `digest/collectors/x.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673766819)
  - fix: Fixed via overlap+dedup rather than composite cursor state: item selection is now inclusive (timestamp_ms >= cursor; the re-emitted boundary item is absorbed by the (source, source_id) INSERT..DO NOTHING constraint) and bridging requires a STRICTLY older timestamp, so a tied-but-unseen notification on any page is still reached and emitted. Cost: refetching at most one boundary tick per run. Tests: tied-on-page-1 emitted, tied-split-across-pages reached, strict-bridge unit check.
- **[P2]** Avoid re-emitting the first-run boundary notification — `digest/collectors/x.py:791` (resolved) — [thread](https://github.com/Dezoxy/notification-digest/pull/5#discussion_r3673876765)
  - fix: Fixed: the first-run seed is newest_seen_timestamp + 1 — the next run's inclusive >= selection excludes everything visible at seed time (no stored items needed for dedup), while incremental tie-overlap semantics are untouched (their boundary items WERE stored by the run that advanced the cursor). Documented accepted edge: an unseen event in the seed's exact millisecond arriving after the seed fetch is lost — vanishingly rare vs the guaranteed history re-emit. Two-phase test: ..._seed_plus_one_excludes_seed_time_notification_next_run.

## Review rounds
6 review requests, 1 clean verdict

*Generated by scripts/pr_summary.py*
