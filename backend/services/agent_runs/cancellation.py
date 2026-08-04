"""Bridge async agent-run cancellation state into synchronous executor probes.

This module owns only the reusable polling adapter required by graph executors.
It does not decide cancellation policy or settle agent-run lifecycle state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable


logger = logging.getLogger(__name__)


class AsyncCancellationProbe:
    """Expose an async cancellation check through a non-blocking sync callback."""

    def __init__(self, check: Callable[[], Awaitable[bool]]) -> None:
        self._check = check
        self._cancelled = False
        self._pending: asyncio.Task[bool] | None = None

    def __call__(self) -> bool:
        """Return cached cancellation state while refreshing it asynchronously."""
        if self._cancelled:
            return True
        if self._pending is not None and self._pending.done():
            try:
                self._cancelled = bool(self._pending.result())
            except Exception:
                logger.debug("Agent-run cancellation probe failed", exc_info=True)
            finally:
                self._pending = None
        if self._cancelled:
            return True
        if self._pending is None:
            self._pending = asyncio.create_task(self._check())
        return self._cancelled


__all__ = ["AsyncCancellationProbe"]
