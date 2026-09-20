## Scope

### In scope

- Collecting new items from the owner's own accounts and from public feeds.
- Keeping enough state to guarantee that a re-run duplicates nothing.
- Summarizing a window of items into a digest, including the fallback path when
  the primary model is unavailable or refuses.
- Delivering that digest to the channels the owner actually reads, and serving
  the public archive that one of those channels is.
- The Cloudflare Worker in `workers/news-site/`, because it is a delivery
  channel of this system and lives in this repository.

### Out of scope

| Not covered here | Where it lives instead |
|---|---|
| Provisioning the VM, the systemd timers, and secret wiring | The separate `homelab` repository (Ansible role `myapps`) |
| The Proxmox node, the home network and its boundary | The same `homelab` repository |
| Anything that happens inside a source platform | Telegram, X, Reddit, Patreon, Polymarket, Hacker News and the feed publishers |
| Model behaviour and pricing | The model providers |
| The content itself | Third parties wrote it; this system summarizes and links to it ([C-04](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/requirements/constraints.md)) |

### Deliberately not built

These are decisions, not gaps. Each one is a consequence of a single owner and
a single node.

- **No high-availability or failover tier.** See
  [availability](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/reliability/availability.md).
- **No staging environment.** There is production and there is a local
  throwaway run; see [environments](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/deployment/environments.md).
- **No multi-tenancy, no accounts, no login on the public site.** The site
  publishes; it does not authenticate readers.
- **No alerting stack in this repository.** What failure actually looks like is
  described honestly in
  [observability](https://github.com/Dezoxy/notification-digest/blob/main/docs/architecture/observability/observability-architecture.md).
