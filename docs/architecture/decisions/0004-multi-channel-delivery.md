# 4. Deliver through independent channels, and keep email implemented even though it is off

Date: 2026-09-20

## Status

Accepted

## Context

Delivery was email-only at inception; PR #25 ("multi-channel cutover", per
`CLAUDE.md` and `PLAN.md` §1) added a Telegram TL;DR ping and a published
news-site entry as additional channels. On the real VM the live channels are
Telegram and the news site; email is disabled there
(`myapps_digest_email_enabled: false` in the homelab role's host_vars, per
`CLAUDE.md`). `digest/deliver.py`'s `deliver_channels` treats all three
channels (`email`, `site`, `telegram`) as independently enabled/disabled and
independently retryable per digest, with one ordering dependency: Telegram's
message links to the site's own page for that digest, so Telegram is
skipped for a run until the site channel (if enabled) has actually
published (`deliver_channels`'s docstring).

`digest/config.py` raises `ConfigError` at startup
(`if not (email_enabled or site_enabled or telegram_enabled): raise
ConfigError(...)`, `config.py` line 726) — the app refuses to start with
every channel disabled. `EMAIL_ENABLED` defaults to `true` (`README.md`'s
environment-variable table; `config.py`'s `email_enabled: bool = True`), so
a host that configures none of `EMAIL_ENABLED`, `SITE_PUBLISH_URL`, or the
Telegram bot token variables still has a working default channel rather than
failing to start.

## Decision drivers

- [C-01](../requirements/constraints.md): one owner, one reader — no need
  for channel redundancy beyond what avoids a hard startup failure.
- Partial-failure tolerance (`PLAN.md` §1): one channel failing must not
  suppress another channel's delivery of the same digest.
- Backward compatibility for any future or alternate deployment of this
  codebase that has not configured Telegram or the site channel.

## Considered options

1. Single delivery channel (email only), as originally built.
2. Multi-channel, but replace email outright once Telegram/site exist,
   deleting the email code path.
3. Multi-channel with all three channels first-class and independently
   retryable, keeping email fully implemented as the framework default even
   though the owner's own deployment disables it.

## Decision

Implement email, site, and Telegram as three independent channels in
`digest/deliver.py`. Each channel has its own per-digest completion flag
(`digests.email_sent` / `site_published` / `telegram_sent`, `digest/state.py`)
and its own failure handling — a failure on one channel never rolls back or
blocks a channel that already succeeded, and `get_pending_digests` retries
exactly the channels still outstanding on a later run. Email remains the
role default (`EMAIL_ENABLED` defaults `true`) and stays fully implemented
rather than being deleted or reduced to a stub, specifically so that a
deployment which configures neither Telegram nor the site channel still
works out of the box. Option 2 (delete email) was rejected for exactly this
reason: it would leave no working default for an unconfigured deployment,
and the `ConfigError` guard requiring at least one enabled channel would
have no channel left to satisfy on a bare install.

This service must not be described as "emailing a digest" — that stopped
being accurate at the multi-channel cutover, per `CLAUDE.md`'s explicit
framing note.

## Consequences

Positive:

- A host that deploys this codebase with zero delivery configuration still
  gets a working digest (email, the default channel) rather than a
  same-day startup failure.
- Channels fail independently: a broken SMTP path on the owner's own
  deployment (moot in practice, since `EMAIL_ENABLED=false` there) could
  never have blocked Telegram or the site from delivering.
- The `ConfigError` startup guard (`config.py` line 726) makes "every
  channel disabled" a loud, immediate failure instead of a silent no-op
  service.

Negative / accepted trade-offs:

- Email delivery code, SMTP configuration surface (`SMTP_HOST`,
  `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `DIGEST_FROM`, `DIGEST_TO`) and
  its own tests are maintained indefinitely for a channel unused on the
  owner's own production deployment.
- Telegram's dependency on the site channel's success (message links to the
  site page) means an owner running site-disabled-but-telegram-enabled in a
  hypothetical deployment would need `hide:site`/`hide:telegram` argv
  combinations correctly, per `README.md`'s run-mode arguments section, to
  avoid shipping a dead link.
- Three independently retried channels means a digest can sit "partially
  delivered" indefinitely if one channel's failure is persistent (e.g. a
  wrong SMTP password) — there is no alert distinct from the process's own
  exit code for a channel stuck retrying every run.

## Risks

- Not recorded: `risks/architecture-risks.md` on this branch currently holds
  generic template content unrelated to this repository, so no digest-specific
  risk ID could be confirmed for a partial-delivery or channel-drift scenario
  at the time of writing.

## Related

- Requirements: [C-01](../requirements/constraints.md)
- Architecture views: not recorded
- Other ADRs: none
