"""The shared result contract every collector returns.

`CollectResult` lives here, not in `collectors/telegram.py`, because no
collector is the "base" of the others -- telegram is just the collector
written first, and x.py/rss.py/reddit.py importing it from telegram would be
a misleading dependency edge. (`polymarket.py`'s `PolymarketCollectResult` is
a deliberately distinct type -- see its own module docstring -- and does not
use this class.)

`items` carries this run's new items in the normalized `Item` shape
(`digest.state.Item`) -- what actually gets summarized and delivered.
`cursor_updates` is keyed by `(source, scope)` (e.g. `("telegram",
"<chat_id>")`, `("x", "notifications")`) -- `digest/main.py` merges every
collector's dict together and persists it via `digest/state.py`'s
`commit_new_items`, advancing each source/scope's watermark exactly to what
this run actually consumed. `failed` is a single per-collector flag, not a
list of errors -- `digest/main.py`'s per-source wrappers turn it into that
source's failure banner in the digest, deliberately coarse-grained (see
`collectors/telegram.py`'s `collect` docstring for why one bad chat, e.g.,
still leaves `failed=True` even though the run keeps going).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from digest.state import Item


@dataclass
class CollectResult:
    items: list[Item] = field(default_factory=list)
    cursor_updates: dict[tuple[str, str], str] = field(default_factory=dict)
    failed: bool = False
