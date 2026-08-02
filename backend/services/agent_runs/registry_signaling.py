"""Owner-lock-coupled process-local registry state-change signaling.

This module owns only the process-local state version and notification
condition used to observe committed registry changes. It receives the registry
facade's existing owner lock and does not own or create a run/claim mutation
lock, run storage, claim storage, lifecycle policy, handoff policy, metrics,
logging, or public registry methods.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


class RegistryStateSignal:
    """Coordinate version reads and notifications around one facade owner lock."""

    def __init__(self, owner_lock: asyncio.Lock) -> None:
        self._owner_lock = owner_lock
        self._state_changed = asyncio.Condition()
        self._state_version = 0

    async def state_version(self) -> int:
        """Return the current mutation version under the owner lock."""

        async with self._owner_lock:
            return self._state_version

    def mark_changed_locked(self) -> None:
        """Record one committed mutation while the owner lock is held."""

        self._state_version += 1

    async def notify_after_commit(self) -> None:
        """Wake waiters after the owner-lock-protected mutation has committed."""

        async with self._state_changed:
            self._state_changed.notify_all()

    async def wait_for_state_change(self, *, after_version: int) -> int:
        """Wait until the state version differs from ``after_version``."""

        while True:
            async with self._owner_lock:
                current = self._state_version
            if current != after_version:
                return current
            async with self._state_changed:
                async with self._owner_lock:
                    current = self._state_version
                if current != after_version:
                    return current
                await self._state_changed.wait()

    async def wait_for_predicate(
        self,
        *,
        after_version: int,
        predicate: Callable[[], T | None],
    ) -> T:
        """Wait for a scoped predicate result using current registry ordering."""

        while True:
            async with self._owner_lock:
                status = predicate()
            if status is not None:
                return status

            async with self._state_changed:
                async with self._owner_lock:
                    status = predicate()
                    current = self._state_version
                if status is not None:
                    return status
                if current != after_version:
                    after_version = current
                    continue
                await self._state_changed.wait()


__all__ = ["RegistryStateSignal"]
