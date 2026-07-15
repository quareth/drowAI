"""Task router subpackage (crud, runtime, interrupts, …).

Feature modules (e.g. ``crud``) import without side effects. The composed
``router`` and HITL compatibility callables live in ``router_bundle`` and load
only when accessed (``tasks.router``, ``from tasks import ResumeRequest``, …).
"""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = [
    "router",
    "ResumeRequest",
    "RetryRequest",
    "get_task_interrupt",
    "resume_graph_execution",
    "retry_graph_execution",
    "get_interrupt_state_service",
    "asyncio",
    "_schedule_background_task",
]


def __getattr__(name: str) -> Any:
    if name == "asyncio":
        return asyncio
    if name in (
        "router",
        "ResumeRequest",
        "RetryRequest",
        "get_task_interrupt",
        "resume_graph_execution",
        "retry_graph_execution",
        "get_interrupt_state_service",
        "_schedule_background_task",
    ):
        from . import router_bundle as _bundle

        return getattr(_bundle, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
