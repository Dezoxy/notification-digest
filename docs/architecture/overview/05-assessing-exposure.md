## Assessing exposure

A reading path for someone asking what could hurt: an owner deciding whether
to trust this system with another account, or a reviewer judging the honest
risk picture. Four stops.

### What is reachable from outside, and what stays on the LAN

Exactly one container takes inbound traffic from the public internet: the
News Site, a Cloudflare Worker with no login and no Cloudflare Access in
front of it — a reader authenticates by possessing the capability URL
(`/t/<SITE_TOKEN>/…`), not by an identity check. Everything else, including
the Digest Runner and the State Database, runs on `01-myapps-vm` on the
owner's home LAN, never reachable from the internet
([A-04](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/assumptions.md))
— the Runner never listens on a port; it starts on a timer and only ever
connects out.

![Security view: what is internet-facing, where secrets come from, and what stays on the LAN](embed:Security)

- [Security architecture](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/security/security-architecture.md)
- [Trust boundaries](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/security/trust-boundaries.md)

### What the credentials actually are

![Sources view: the accounts and feeds one run collects from](embed:Sources)

Every collection credential authenticates as the **owner's own personal
account** — Telegram's `TG_SESSION`, and cookie sessions for X, Reddit and
Patreon ([C-02](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/constraints.md)).
Losing one of these secrets is an **account takeover**, not a
service-credential rotation: there is no key to reissue, only whatever the
account itself can do — reading the owner's DMs, or, for Patreon, spending
against a paid subscription
([RISK-002](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md)).
None carries MFA, a property of the accounts being used unofficially. X,
Reddit and Patreon all sit on this same unofficial, ToS-risk access with no
sanctioned fallback if the platform revokes it
([RISK-003](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md),
[RISK-007](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md)):
Reddit's session exists because Reddit's Data Team refused the owner's
official API application, and Patreon's is a paid subscription, so an
adverse action there costs money, not just access. `twifork`, the
unofficial X client, is pinned to an exact version, with every bump gated
behind a hand audit of the diff
([ADR 1](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/decisions/0001-pin-unofficial-x-client.md))
— worse here than with a rotatable service credential.

### Whose data this is, and where it goes

Collected item text — Telegram messages, X notifications, Reddit and Patreon
posts — is written by people other than the owner
([C-04](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/constraints.md)).
It is classified as confidential third-party personal data, retained for 90
days after its digest delivers (14 if never summarized), and never
republished verbatim: the public News Site gets a model-written summary plus
a deep link back to the original — except the `relay` lane, which forwards
whole posts verbatim into a Telegram topic by explicit owner configuration
and never touches the site.

- [Data classification](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/security/data-classification.md)
- [Data ownership](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/data/data-ownership.md)

Data classification's GDPR section is explicitly **reasoning, not a legal
opinion**: it argues that summarizing one's own feeds for personal reading is
"purely personal" processing outside GDPR's scope, then names where that
strains — the site publishes beyond the owner alone, and `relay` republishes
verbatim rather than summarizing. Nothing here can honor a deletion request
against content already collected or published.

### What is deliberately absent, and what is accepted rather than mitigated

This system runs no WAF, no intrusion detection, no SIEM, and no automated
key rotation — consequences of
[C-01](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/constraints.md),
not gaps waiting to be filled. A compromised session surfaces only as a
collector auth failure in a run's own logs, or not at all if the attacker is
quiet; every secret is rotated by hand. Explicitly **ACCEPTED**:

- [RISK-001](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md)
  — the single Proxmox node is the entire failure domain; no second node is
  planned.
- [RISK-002](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md)
  — personal-account credential compromise; no service-credential model exists
  for a personal account.
- [RISK-003](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md)
  /
  [RISK-006](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md)
  — X account suspension, or any source losing free access; no technical
  mitigation and no budget line to replace one.
- [RISK-004](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md) — unbounded summarization spend; unmeasured, accepted exposure.

Not marked accepted: [RISK-005](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md)
(a model refusal) and [RISK-007](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md)
(Reddit/Patreon's posture unnamed in CLAUDE.md's hard rules) — the register
treats the latter as an open gap, not a resolved item.

- [Architecture risks](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/risks/architecture-risks.md)
