# PR #1: Phase 1: Telegram collector + SQLite state
[View PR](https://github.com/Dezoxy/notification-digest/pull/1) · @Dezoxy · merged 2026-07-29 · `5a1b227` · `phase-1-telegram-collector -> main` · +1968/-4 across 15 files

## Description
## Summary
- Project skeleton: uv-managed pyproject (py3.12, ruff, pytest), .env.example
- `digest/config.py` — fail-fast env validation, the only os.environ reader
- `digest/state.py` — PLAN §4.1 schema with per-(source, scope) cursors; `commit_new_items` inserts items and advances cursors in one transaction (rollback on any failure); Python-side validation of CHECK/NOT NULL invariants because `INSERT OR IGNORE` silently swallows constraint violations
- `digest/collectors/telegram.py` — per-chat cursors, first-run seeds cursor from the latest message with no history backfill, textless messages advance the cursor but emit no items, per-chat failure isolation, auth errors / long FloodWaits abort remaining chats while keeping partial results
- `scripts/telegram_login.py` — one-time interactive StringSession login; also lists group/channel dialog ids to pick `TG_CHAT_ALLOWLIST`
- 13 tests (state idempotency/atomicity, collector behavior via a no-network fake client)

## Review process
Implementation written by Sonnet subagent per CLAUDE.md workflow; reviewed line-by-line by the main session. Review fixes applied: top-level `asyncio` import; PLAN.md cursor schema corrected to per-(source, scope) before implementation (Telegram message ids are per-chat).

## Verification
- `uv run ruff check .` — clean
- `uv run pytest` — 13/13
- `uv run python -m digest.main` with no env → exit 2, clear ConfigError naming the missing var, no values echoed

## Phase 1 acceptance criteria still needing your input
Live test against real Telegram needs: api_id/api_hash from my.telegram.org, one login run, and the chat allowlist.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

## Commits (6)
- [df50f39](https://github.com/Dezoxy/notification-digest/commit/df50f396a6d3eeba76cccc3d7bf366acf09464df) PLAN: per-chat cursor scope (Telegram msg IDs are per-chat), no-backfill seeding
- [470fe28](https://github.com/Dezoxy/notification-digest/commit/470fe289cede755afa50680e655b2a567fd124dc) Phase 1: Telegram collector, SQLite state, config, tests
- [dc02252](https://github.com/Dezoxy/notification-digest/commit/dc022528fc04e8124898a834f7c6a1424a40e705) Fix Codex review findings: chat-scoped item ids, oldest-first backlog fetch
- [9eeccf0](https://github.com/Dezoxy/notification-digest/commit/9eeccf08403da70677bde0bc15deb9a8112f172b) Fix Codex round-2 findings: dialog-cache prefetch, empty-chat cursor, non-interactive startup
- [22fcc0e](https://github.com/Dezoxy/notification-digest/commit/22fcc0e08969dba502f851204985ba0444eb2131) Fix Codex round-3 findings: scoped dedup conflict, reject basic groups
- [8b9304b](https://github.com/Dezoxy/notification-digest/commit/8b9304bf0dcb0bd647a0bff653669fcd61abaab3) Fix Codex round-4 findings: range-based peer ids, prefetch FloodWait, __main__

## Files (15)
- `uv.lock` `+491/-0`
- `tests/test_collectors.py` `+417/-0`
- `digest/collectors/telegram.py` `+363/-0`
- `digest/state.py` `+176/-0`
- `tests/test_state.py` `+156/-0`
- `digest/main.py` `+91/-0`
- `digest/config.py` `+77/-0`
- `scripts/telegram_login.py` `+68/-0`
- `tests/test_main.py` `+48/-0`
- `.env.example` `+36/-0`
- `pyproject.toml` `+32/-0`
- `PLAN.md` `+7/-4`
- `digest/__main__.py` `+4/-0`
- `digest/__init__.py` `+1/-0`
- `digest/collectors/__init__.py` `+1/-0`

## Review findings (10)
- **[P1]** Include the chat in Telegram item uniqueness — `digest/state.py:25` (resolved) — [thread](https://github.com/Dezoxy/notification-digest/pull/1#discussion_r3671056286)
  - fix: Fixed in dc02252 (composite Telegram source_id `{chat_id}:{msg_id}`), regression test `test_commit_new_items_same_msg_id_in_different_chats_both_survive`. The schema's UNIQUE(source, source_id) was intentionally left unchanged rather than widened to include chat_id: chat_id is NULL for X items and SQLite treats NULLs as distinct in unique indexes, which would silently break X dedup in Phase 3. The composite key keeps the existing constraint correct for both sources.
- **[P1]** Resolve numeric peers before collecting — `digest/collectors/telegram.py:216` (resolved) — [thread](https://github.com/Dezoxy/notification-digest/pull/1#discussion_r3671189598)
  - fix: Fixed in 9eeccf0: collect() now calls get_dialogs() once before the per-chat loop to populate the entity cache of the StringSession-reconstructed client. Auth errors there abort the run immediately (failed=True, nothing touched); other get_dialogs failures fall through to per-chat handling. Tests: test_get_dialogs_auth_error_aborts_before_any_chat_is_touched + call-count assertion in the seeding test.
- **[P2]** Avoid advancing past a capped Telegram backlog — `digest/collectors/telegram.py` (unresolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/1#discussion_r3671056289)
- **[P2]** Persist a cursor for initially empty chats — `digest/collectors/telegram.py:222` (resolved) — [thread](https://github.com/Dezoxy/notification-digest/pull/1#discussion_r3671189605)
  - fix: Fixed in 9eeccf0: an empty chat on first run now persists cursor "0" instead of no cursor, so subsequent runs are incremental and the chat's first real message is collected rather than consumed as the seed. Test: test_first_run_empty_chat_seeds_zero_cursor_then_next_run_emits_new_message.
- **[P2]** Catch authentication failures during client startup — `digest/main.py` (unresolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/1#discussion_r3671189610)
- **[P2]** Reject all NOT NULL violations before advancing cursors — `digest/state.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/1#discussion_r3671234339)
  - fix: Fixed in 22fcc0e: replaced INSERT OR IGNORE with INSERT ... ON CONFLICT (source, source_id) DO NOTHING, scoping the ignored conflict to the dedup constraint only — NOT NULL/CHECK violations now raise IntegrityError and roll back the whole transaction (items + cursors). The Python-side field validation was deleted as redundant. Tests: test_atomicity_rolls_back_on_null_source_id, test_atomicity_rolls_back_on_null_fetched_at.
- **[P2]** Avoid generating channel links for basic groups — `digest/collectors/telegram.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/1#discussion_r3671234344)
  - fix: Fixed in 22fcc0e: collect() now skips legacy basic groups (negative id, no -100 prefix) before any network call, with a loud warning and failed=True; build_message_url raises ValueError for them as defense in depth, and scripts/telegram_login.py annotates basic groups as unsupported in the dialog listing. Test: test_collect_skips_legacy_basic_group_but_processes_supergroup.
- **[P2]** Classify Telegram peer IDs by range, not prefix — `digest/collectors/telegram.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/1#discussion_r3671284195)
  - fix: Fixed in 8b9304b: classification now uses Telegram's marked-id numeric range via telethon.utils.resolve_id (channel/supergroup marked ids are < -10**12; PeerChat otherwise), and build_message_url computes the internal id arithmetically (-chat_id - 10**12). Test fixtures were also moved to realistic 13-digit marked ids — the old 10-digit fixtures were themselves in the basic-group range and only passed due to the prefix bug. Regression tests: test_is_basic_group_regression_numeric_range_not_string_prefix, test_collect_skips_basic_group_misclassified_by_old_string_prefix_check.
- **[P2]** Handle FloodWait during dialog prefetch — `digest/collectors/telegram.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/1#discussion_r3671284197)
  - fix: Fixed in 8b9304b: the prefetch now mirrors _collect_one_chat's FloodWait policy — wait <= 60s sleeps and retries get_dialogs once; longer (or second) waits abort the run with failed=True like an auth error. Tests: test_get_dialogs_long_floodwait_aborts_before_any_chat_is_touched, test_get_dialogs_short_floodwait_retries_once_then_processes_chats.
- **[P2]** Expose the documented package entrypoint — `digest/__init__.py:1` (resolved) — [thread](https://github.com/Dezoxy/notification-digest/pull/1#discussion_r3671284202)
  - fix: Fixed in 8b9304b: added digest/__main__.py delegating to digest.main:main(). Verified 'uv run python -m digest' now behaves identically to 'python -m digest.main' (exit 2 + ConfigError with unset env).

## Review rounds
4 review requests, 1 clean verdict

*Generated by scripts/pr_summary.py*
