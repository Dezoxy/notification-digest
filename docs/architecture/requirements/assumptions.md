## Assumptions

Each row is believed, not proven. An assumption that breaks is a change of
architecture, not a bug, so each one names what would break and what it would
cost to find out.

| ID | Assumption | If it is false | How it would surface |
|---|---|---|---|
| A-01 | The owner's personal Telegram and X sessions stay valid until deliberately rotated | Collection stops for that source until the owner re-authenticates by hand | The source silently returns nothing, or the collector records an auth failure and backs off |
| A-02 | The unofficial X client keeps working against a site that can change without notice | The X lane stops; nothing else is affected | Collector errors after an upstream site change, historically a broken transaction-ID signing step |
| A-03 | The site database is disposable and can be rebuilt from the Azure state database at any time | A rebuild would lose published history that exists nowhere else | Only discovered during a rebuild; the assumption is currently untested end to end ([TD-002](../risks/technical-debt.md)) |
| A-04 | The Azure subscription's access control holds: the jobs expose no listener, and the runner and backup-writer identities, the digest Key Vault, the state accounts and the deploy path are reachable only through the owner's own Entra account and `main`-protected workflows. Restated 2026-10-08; until then the same assumption covered the home LAN and the VM | Every personal session secret and the whole state bundle are exposed | No detection exists inside this system; it depends on the owner's Entra account, the role assignments and the repository's branch protection |
| A-05 | The summarizer's long-lived credential stays valid until the owner rotates it | Every lane falls back to the Claude API (federated, prepaid credits), at higher cost | The primary call fails and the digest records a fallback model as its provenance |
| A-06 | Third-party sources keep their content reachable without a paid API | The affected collector stops; the design has no budget line to replace it | A collector starts returning empty or unauthorized results |
