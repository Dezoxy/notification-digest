# PR #6: Richer digest: TL;DR, self-sufficient briefs, branded email styling
[View PR](https://github.com/Dezoxy/notification-digest/pull/6) · @Dezoxy · merged 2026-07-29 · `796b8bc` · `styling-richer-digest -> main` · +871/-16 across 9 files

## Description
## Summary
- **Content contract** (prompts/digest.md): bold `**TL;DR:**` opener; Worth-knowing items are 2–4 sentence self-sufficient mini-briefs carrying the actual facts/numbers/outcomes (links are for digging deeper, never the substance); multi-message topics merge into one synthesized item; subgroup headings are mandated `### Telegram — <group>` / `### X` h3 lines.
- **Email styling** (digest/emailer.py): post-sanitization injection of source chips (Telegram blue #229ED9, 𝕏 black — inline-styled spans, injected only by our code after nh3, never from model output), amber ⚠ banner callout, TL;DR highlight, h2 section borders, dark-mode media query. No images — the anti-tracking sanitizer contract is untouched.
- **Bug found by live Opus testing**: the generic scheme-defang branch mangled our own `**TL;DR:**` opener (`DR:**…` matched as a scheme-shaped token); the non-// branch now requires a URI-plausible first payload character. Second live finding: Opus used bold lines instead of h3 subgroups — fixed by mandating h3 in the prompt; re-verified live end to end.

## Verification
ruff clean; 174 tests (13 new for chips/banner/TL;DR passes + integration); two live Opus runs rendered through the full pipeline.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

## Commits (6)
- [51224e2](https://github.com/Dezoxy/notification-digest/commit/51224e2beb9009984df0a26948a9da2305df6fe3) Richer digest: TL;DR + self-sufficient mini-briefs + branded email styling
- [7da60e4](https://github.com/Dezoxy/notification-digest/commit/7da60e4c3a669b43408e99f793178719c4aa0286) Fix Codex findings: chat titles for real group names, defang precision, dark-mode contrast
- [4ee1e49](https://github.com/Dezoxy/notification-digest/commit/4ee1e49c954b684b3eb815925f1e1ec16fa8971a) Fix Codex finding: emphasis discrimination moved to the replacement callback
- [e2f69ea](https://github.com/Dezoxy/notification-digest/commit/e2f69ea1f3fe948162d209cdb0fe948b71731219) Soft-check the TL;DR opener: warn and ship, never hard-fail a nicety
- [1f1416a](https://github.com/Dezoxy/notification-digest/commit/1f1416abdfa7784cf0f49f958411aa05634808d6) Fix Codex finding: generic defang branch cannot bite into a following URL's scheme
- [5489cf5](https://github.com/Dezoxy/notification-digest/commit/5489cf5842cbc4567ee8bae1481c1707ff47a4d5) Fix Codex finding: defang nested/adjacent schemes to fixpoint

## Files (9)
- `tests/test_emailer.py` `+244/-1`
- `digest/emailer.py` `+197/-0`
- `tests/test_summarize.py` `+168/-0`
- `digest/summarize.py` `+73/-1`
- `tests/test_state.py` `+73/-0`
- `digest/state.py` `+34/-7`
- `prompts/digest.md` `+35/-6`
- `tests/test_collectors.py` `+29/-1`
- `digest/collectors/telegram.py` `+18/-0`

## Review findings (7)
- **[P2]** Keep delimiter-led URIs in the defanging pass — `digest/summarize.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/6#discussion_r3673721647)
  - fix: Fixed: the non-// branch restores the broad payload, gated only by (?!\*\*|__) — excluding exactly the markdown double-emphasis case that caused the TL;DR mangling. tel:*67 (single asterisk, real vertical-service-code URI) and mailto:?to=... both defang again. Tests: ..._bare_tel_service_code_with_asterisk_is_defanged, ..._bare_mailto_query_only_is_defanged, ..._tldr_bold_opener_survives_untouched.
- **[P2]** Provide group names before requiring them — `prompts/digest.md` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/6#discussion_r3673721652)
  - fix: Fixed: items now carry chat_title (nullable column + idempotent migration mirroring body_md's); the Telegram collector captures entity.title once per chat; build_prompt includes it in the payload; and the prompt instructs using chat_title verbatim with a bare '### Telegram' fallback when null — inventing names or printing numeric ids is explicitly forbidden. Tests: migration from pre-change DDL, collector title capture (+None fallback), payload inclusion, DB round-trip.
- **[P2]** Give the TL;DR callout a dark-mode foreground — `digest/emailer.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/6#discussion_r3673721661)
  - fix: Fixed: the TL;DR callout sets an explicit inline color:#1a1a1a (readable on its light background even when <style> is stripped) and both callouts carry classes with dark-mode !important overrides (.tldr #2b2b2b/#e8e8e8, .banner #4d3800/#ffe69c) for clients honoring the media query. Tests cover the inline color, both classes, and the dark-mode block contents.
- **[P2]** Avoid consuming the scheme of an adjacent URL — `digest/summarize.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/6#discussion_r3674114644)
  - fix: Fixed: the generic branch gained a trailing (?![a-zA-Z0-9+.\-]*://) lookahead — it can no longer end while biting into a following URL's scheme (every backtracked ending fails as well), so the token doesn't match at all and the scanner defangs the full https:// URL via the dedicated branch. Tests: test_enforce_link_allowlist_url_glued_to_emphasis_is_fully_defanged (asserts hxxps output and no split token), plus the spaced-TL;DR regression.
- **[P2]** Defang URIs whose payload starts with double markers — `digest/summarize.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/6#discussion_r3673936482)
  - fix: Fixed: the regex is fully broad again and the discrimination lives in the replacement callback, which sees the whole token — only payloads consisting SOLELY of */_ characters (markdown emphasis artifacts like the TL;DR opener's DR:**) are left untouched; mailto:__attacker@example.com now defangs. Tests: ..._bare_mailto_with_double_underscore_payload_is_defanged, ..._emphasis_only_payload_token_is_untouched, plus the TL;DR and tel:*67 regressions.
- **[P2]** Reject responses that omit the required TL;DR — `prompts/digest.md:12` (resolved) — [thread](https://github.com/Dezoxy/notification-digest/pull/6#discussion_r3674011378)
  - fix: Fixed with a deliberate softening rather than a hard gate: summarize() now detects a missing **TL;DR:** opener and logs a Loki-visible warning while still shipping the digest. Rationale (from this repo's banner experience, where live testing showed hard-gating model compliance loops forever when the model persistently omits instructed text): a digest without its TL;DR is cosmetically degraded; NO digest — with every collected item held for another 3h cycle — is strictly worse. Structural failures (missing/misordered sections) remain hard SummarizeError gates. Tests: test_summarize_missing_tldr_logs_warning_but_still_ships, test_summarize_with_tldr_no_warning.
- **[P2]** Defang outer schemes before embedded scheme text — `digest/summarize.py` (resolved, outdated) — [thread](https://github.com/Dezoxy/notification-digest/pull/6#discussion_r3674187919)
  - fix: Fixed: the trailing lookahead is gone (it could only ever protect one of the two colons) — the bare-URL substitution now runs to a bounded fixpoint: pass 1 breaks the outer scheme separator, exposing the inner scheme:// token that pass 2 defangs. custom:abchttps://attacker.example/x → custom[:]abchxxps://... (both dead); the mandated spaced **TL;DR:** opener stays untouched. Idempotence tests bound termination. Test: test_enforce_link_allowlist_nested_scheme_uri_defangs_both_colons.

## Review rounds
6 review requests, 1 clean verdict

*Generated by scripts/pr_summary.py*
