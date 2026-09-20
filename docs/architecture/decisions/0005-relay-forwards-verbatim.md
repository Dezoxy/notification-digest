# 5. Forward relay-lane posts verbatim, with no summarization

Date: 2026-09-20

## Status

Accepted

## Context

The main Telegram collector (`digest/collectors/telegram.py`) only extracts
`msg.message` — a photo- or video-only post with no caption has none, so it
is silently skipped, and even a caption-bearing post loses its media when
re-posted as summarized text. Some public channels the owner wants to watch
(a disaster-news channel is the motivating case, per `digest/relay.py`'s
module docstring) post overwhelmingly in photos and videos with little or no
caption — neither outcome is acceptable for a channel whose entire value is
the media itself.

`digest/relay.py` implements a separate `relay` run mode: no `items` or
`digests` rows, no Claude call, state limited to a cursor per channel in the
same `cursors` table every other collector uses, under its own `SOURCE =
"relay"` namespace. It builds the raw
`functions.messages.ForwardMessagesRequest` Telethon RPC directly rather
than using `TelegramClient.forward_messages`, because that wrapper exposes
no `top_msg_id`/`reply_to` (so it cannot target a forum topic) and does not
chunk requests (`_FORWARD_CHUNK_SIZE = 100`, Telegram's own per-request id
limit).

## Decision drivers

- [C-04](../requirements/constraints.md): collected content is third-party
  authored; the public site publishes summaries and deep links, never
  verbatim third-party bodies, except in the relay lane the owner
  configures explicitly.
- A native Telegram forward preserves media, album grouping, and formatting
  with no re-upload and no model in the loop — solving a problem
  (media-only posts) that the model-summarization pipeline structurally
  cannot solve, since there is no caption text for the model to work from.

## Considered options

1. Extend the main collector/summarizer to somehow describe or caption
   media-only posts via the model.
2. Re-post extracted caption text only, dropping the media entirely.
3. Forward the original message natively (server-side redelivery: media,
   album grouping, formatting, and the "Forwarded from" header intact), with
   no AI involvement and no state beyond a per-channel cursor.

## Decision

Relay lane: verbatim, native Telegram forwarding, by explicit owner
decision (`digest/relay.py`'s module docstring: "no re-upload and no model
in the loop to summarize, translate, or drop anything"). `chunk_preserving_
albums` never splits an album across two forward requests. Idempotency
rides the same `cursors` table every other collector uses
(`("relay", <chat_id>)` scope), and `deterministic_random_id` derives each
forward's `random_id` from `(chat_id, msg_id)` via `blake2b` so a
crash-and-retry re-forward is deduplicated by Telegram's own server-side
check rather than producing a duplicate post. The cursor advances per chunk,
only after that chunk's RPC succeeds, so a crash between chunks loses at
most one chunk's progress. A freshly configured channel seeds its cursor at
the latest message id and forwards nothing — there is never a history
backfill (`relay_channel`'s docstring, step 3).

This is a structurally different lane from the summarization cascade
(window/daily/weekly): it produces no `items` or `digests` rows at all, so
it never appears in a digest, on the news site, or in any channel other
than the hub Telegram topic it forwards into.

## Consequences

Positive:

- Media-only posts (the case the main collector cannot handle at all) reach
  the owner intact, with no summarization loss and no re-upload cost.
- No model call means no cost, no latency, and no safety-classifier
  exposure for this lane.
- Idempotent by the same mechanisms as every other lane (cursor table,
  deterministic dedup key), so the crash-retry story is consistent across
  the whole codebase rather than inventing a new one for this lane.

Negative / accepted trade-offs:

- Content reaches the hub topic completely unfiltered and unsummarized —
  there is no editorial judgment, no NO-SIGNAL suppression, and no
  translation applied to relay content, unlike every other lane.
- The owner receives no native Telegram notification for their own forward
  (a forward sent from the owner's own user session generates no
  notification for that session); a separate bot-posted `🔔` line exists
  purely to work around this (`README.md`'s relay section) — a workaround
  for a limitation of the chosen mechanism, not something upstream can fix.
- `ChatForwardsRestrictedError` (a channel owner disabling forwarding) has
  no automated recovery — the channel must be removed from
  `RELAY_TG_CHANNELS` by hand (`_forward_one_chunk`'s docstring).
- Relaying third-party content verbatim, even to a private hub the owner
  controls, carries a content-liability/ToS consideration distinct from
  the summarized lanes' "summary and deep link" posture
  ([C-04](../requirements/constraints.md)) — accepted by explicit owner
  decision, not mitigated in code.

## Risks

- Not recorded: `risks/architecture-risks.md` on this branch currently holds
  generic template content unrelated to this repository, so no digest-specific
  risk ID could be confirmed for a verbatim-relay/content-liability scenario
  at the time of writing.

## Related

- Requirements: [C-04](../requirements/constraints.md)
- Architecture views: not recorded
- Other ADRs: none
