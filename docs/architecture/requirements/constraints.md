## Constraints

Constraints are fixed. They are not traded off; they limit the options. Most of
this system's shape follows from C-01 and C-02 rather than from any preference.

| ID | Constraint | Source | Architectural consequence |
|---|---|---|---|
| C-01 | One owner, who is also the only operator and the only reader | The service exists for one person | No HA tier, no on-call rotation, no multi-tenancy. Operational simplicity outranks redundancy everywhere it competes with it |
| C-02 | Telegram and X are reached with the owner's **personal account sessions**, not bot or application credentials | Neither source exposes the owner's own groups or notification timeline to a bot account | Secrets are long-lived and account-level; losing one is an account compromise, not a service credential rotation ([trust boundaries](../security/trust-boundaries.md)) |
| C-03 | X has no sanctioned API at this tier; collection uses an unofficial client | X API pricing | The collector stays behind a flag and must back off rather than retry-loop, so a flagged account fails quietly ([ADR 1](../decisions/0001-pin-unofficial-x-client.md)) |
| C-04 | Collected content is written by third parties | The sources are other people's posts and messages | The public site publishes summaries and deep links, never verbatim third-party bodies, except in the relay lane the owner configures explicitly ([ADR 5](../decisions/0005-relay-forwards-verbatim.md)) |
| C-05 | The service runs on a single Proxmox node in the owner's home | No second site exists and none is planned | One failure domain holds the runner, its state and the primary backup ([disaster recovery](../reliability/disaster-recovery.md)) |
| C-06 | No secret may exist in git, in a log or in an image | Repository hard rule | Every secret is injected as an environment variable at deploy time from Azure Key Vault |
| C-07 | This repository cannot deploy itself | Deployment lives in the separate homelab repository | A merge here ships nothing; a release is a tag, an image, a pin bump and a deploy ([ADR 6](../decisions/0006-release-by-tag-and-pin.md)) |
| C-08 | Summarization depends on a third-party model that can refuse | Provider acceptable-use policies apply to the content being summarized | A refusal must degrade to a fallback model rather than fail the run ([ADR 2](../decisions/0002-summarizer-fallback-chain.md)) |
