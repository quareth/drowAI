"""Bridge synchronous callers to async runtime-provider operations safely.

Provider reads are often invoked from synchronous provenance or memory code while
the graph event loop is already running. This module runs coroutines on a worker
thread with a fresh coroutine factory so SQLAlchemy sessions are never shared
across threads.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def run_provider_operation_sync(
    coro_factory: Callable[[], Coroutine[Any, Any, T]],
    *,
    log_context: str = "provider operation",
) -> T | None:
    """Run an async provider operation from synchronous code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    if not loop.is_running():
        return loop.run_until_complete(coro_factory())

    result: dict[str, T] = {}
    error: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error["value"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        exc = error["value"]
        logger.warning(
            "%s failed in provider sync bridge: %s",
            log_context,
            type(exc).__name__,
        )
        return None
    return result.get("value")
