## For the CTO

A reading path for whoever is accountable for what could hurt this system and
what it costs: an owner deciding whether to trust it with another account, or
a reviewer judging the honest risk picture. Five stops.

### What is reachable from outside, and what stays private

Exactly one container takes inbound traffic from the public internet: the News
Site, a Cloudflare Worker with no login and no Cloudflare Access in front of
it — a reader authenticates by possessing the capability URL
(`/t/<SITE_TOKEN>/…`), not by an identity check. Everything else, including
the Digest Runner and the State Database, runs inside the owner's Azure
subscription and is never reachable from the internet (A-04) — the Runner
never listens on a port; it starts on a schedule and only ever connects out.
Its state sits in private Blob Storage with shared-key access disabled, so
only a managed identity can read it. Secrets come from a dedicated digest Key
Vault, resolved through that identity. A deploy authenticates to Microsoft
Entra ID from GitHub Actions with a short-lived OIDC token; no cloud
credential is stored in the repository.

![Security view: what is internet-facing, where secrets and identity come from, and what stays private](embed:Security)

Detail lives in **Security Architecture** and **Trust Boundaries**, later in
this document
([source](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/security/security-architecture.md)).

### What the credentials actually are, and why that is the sharpest risk

![Sources view: the accounts and feeds one run collects from](embed:Sources)

Every collection credential authenticates as the **owner's own personal
account** — Telegram's `TG_SESSION`, and cookie sessions for X, Reddit and
Patreon (C-02). Losing one of these secrets is an **account takeover**, not a
service-credential rotation: there is no key to reissue, only whatever the
account itself can do — reading the owner's DMs, or, for Patreon, spending
against a paid subscription (RISK-002). The Telegram session is a secret in
the digest Key Vault; the X cookies live in the runtime state in Blob Storage.
None carries MFA, a property of the accounts being used unofficially. X,
Reddit and Patreon all sit on this same
unofficial, ToS-risk access with no sanctioned fallback if the platform
revokes it (RISK-003, RISK-007): Reddit's session exists because Reddit's Data
Team refused the owner's official API application, and Patreon's is a paid
subscription, so an adverse action there costs money, not just access.
RISK-007 is **not** marked accepted — the risk register calls it an open gap,
because CLAUDE.md's hard rules name only the X collector explicitly, not
Reddit or Patreon.

### Whose data this is, and where it goes

Collected item text — Telegram messages, X notifications, Reddit and Patreon
posts — is written by people other than the owner (C-04). It is classified as
confidential third-party personal data, retained for 90 days after its digest
delivers (14 if never summarized), and never republished verbatim: the public
News Site gets a model-written summary plus a deep link back to the original
— except the `relay` lane, which forwards whole posts verbatim into a
Telegram topic by explicit owner configuration and never touches the site.

**Data Classification**'s GDPR section is explicitly reasoning, not a legal
opinion: it argues that summarizing one's own feeds for personal reading is
"purely personal" processing outside GDPR's scope, then names where that
strains — the site publishes beyond the owner alone, and `relay` republishes
verbatim rather than summarizing. Nothing here can honor a deletion request
against content already collected or published. See **Data Classification**
and **Data Ownership**, later in this document.

### Recovery, and what is not guaranteed

No scenario in **Disaster Recovery** has a measured RTO — every duration
there is a rough, unrehearsed estimate, not a target. RPO is bounded by the
daily 04:15 UTC recovery bundle: up to ~24h of `state.db` writes are simply
gone if the runtime state is lost between bundles. The bundle is written to a
separate storage account, in a separate resource group, by a separate identity
— but in the same region, so it protects against a bad write to the runtime
state, not against losing the region. Restoring is a manual operator procedure
(the Azure migration runbook in the repository), no restore from that account
has been exercised yet, and it has a sharper problem than slow recovery:
because delivery flags travel with the bundle, restoring to a point before a
digest was marked delivered lets the normal retry pass resend it (TD-006) — a
direct contradiction of QA-01, the one absolute guarantee this system makes.
There is no automated safeguard against this today. See **Backup Strategy** and
**Disaster Recovery**, later in this document.

### Risks accepted rather than mitigated, and cost exposure

This system runs no WAF, no intrusion detection, no SIEM, and no automated key
rotation (C-01); a compromised session surfaces only as a collector auth
failure in a run's own logs, or not at all if the attacker is quiet.
Explicitly **accepted**: the single Azure region as the entire failure
domain (RISK-008, which superseded RISK-001, the single Proxmox node);
personal-account credential compromise, for which no service-credential model
exists (RISK-002); X account suspension or any
source losing free access, with no budget line to replace one (RISK-003,
RISK-006); and unbounded summarization spend (RISK-004) — QA-07 states plainly
that no hard cap is enforced in code, so a fallback-chain storm can only add
cost, never be stopped by the system itself. Cloud spend is the same shape: a
monthly budget alert on the resource group notifies the owner and stops
nothing (RISK-013). Hosting on Azure also adds three things that are designed
but not yet proven in practice: a restore from the backup account (RISK-010), a
Claude API fallback that has served a real digest (a one-off smoke test passed,
nothing more; RISK-011), and two overlapping executions contending for the one
state lease that keeps them apart (RISK-009). Not marked accepted: RISK-005 (a
model refusal) and RISK-007, above. See **Architecture Risks** and **Quality
Attributes**, later in this document.
