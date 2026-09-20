# Assumptions

Each row is believed, not proven. An assumption that breaks is a change of
architecture, not a bug, so each one names what would break and what it would
cost to find out.

| ID | Assumption | If it is false | How it would surface |
|---|---|---|---|
| A-01 | The owner's personal Telegram and X sessions stay valid until deliberately rotated | Collection stops for that source until the owner re-authenticates by hand | The source silently returns nothing, or the collector records an auth failure and backs off |
| A-02 | The unofficial X client keeps working against a site that can change without notice | The X lane stops; nothing else is affected | Collector errors after an upstream site change, historically a broken transaction-ID signing step |
| A-03 | The site database is disposable and can be rebuilt from the VM at any time | A rebuild would lose published history that exists nowhere else | Only discovered during a rebuild; the assumption is currently untested end to end ([TD-002](../risks/technical-debt.md)) |
| A-04 | The home LAN is trusted, and the VM is never reachable from the internet | Every personal session secret on the VM is exposed | No detection exists inside this system; it depends on the homelab network boundary |
| A-05 | The summarizer's long-lived credential stays valid between deploys | Every lane falls back to the secondary provider, at higher cost and different quality | The primary call fails and the digest records a fallback model as its provenance |
| A-06 | Third-party sources keep their content reachable without a paid API | The affected collector stops; the design has no budget line to replace it | A collector starts returning empty or unauthorized results |
