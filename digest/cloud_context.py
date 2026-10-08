"""Cloud durability hooks; ordinary local runs never install this context."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class CloudSafetyError(BaseException):
    """Stop the execution, bypassing the existing collectors' soft failures."""


@dataclass(frozen=True)
class CloudHooks:
    guard: Callable[[], None]
    checkpoint: Callable[[sqlite3.Connection], object]


_hooks: ContextVar[CloudHooks | None] = ContextVar("digest_cloud_hooks", default=None)


@contextmanager
def cloud_execution(hooks: CloudHooks) -> Iterator[None]:
    """Install lease and persistence callbacks for this one execution."""
    token = _hooks.set(hooks)
    try:
        yield
    finally:
        _hooks.reset(token)


def active() -> bool:
    """Whether cloud delivery safeguards are required."""
    return _hooks.get() is not None


def guard() -> None:
    """Fence a stale worker before changing state or making an external call."""
    hooks = _hooks.get()
    if hooks is not None:
        try:
            hooks.guard()
        except Exception:
            raise CloudSafetyError("cloud lease is unavailable") from None


def checkpoint(conn: sqlite3.Connection) -> None:
    """Persist committed state, or terminate without further publication."""
    hooks = _hooks.get()
    if hooks is not None:
        guard()
        try:
            hooks.checkpoint(conn)
        except Exception:
            raise CloudSafetyError("cloud checkpoint failed") from None
