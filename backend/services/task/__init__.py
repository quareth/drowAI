"""Task domain services package.

Consolidates task lifecycle, state management, access control, interrupt
handling, cleanup, retry, and runtime transition services.

Heavy modules (cleanup, graph retry, interrupts) load on first attribute access so
lightweight imports (e.g. access_service via deps) avoid pulling knowledge/agent graphs.
"""

from __future__ import annotations

from typing import Any

from .access_service import (
    get_owned_task,
    get_owned_task_or_404,
    get_owned_task_with_engagement,
    get_owned_task_with_engagement_or_404,
    get_tenant_task,
    get_tenant_task_or_404,
    get_tenant_task_with_engagement,
    get_tenant_task_with_engagement_or_404,
)

__all__ = [
    "get_owned_task",
    "get_owned_task_or_404",
    "get_owned_task_with_engagement",
    "get_owned_task_with_engagement_or_404",
    "get_tenant_task",
    "get_tenant_task_or_404",
    "get_tenant_task_with_engagement",
    "get_tenant_task_with_engagement_or_404",
    "RuntimeInputResult",
    "TaskCleanupService",
    "TaskGraphRetryService",
    "TaskInterruptService",
    "TaskLifecycleService",
    "TaskRuntimeInputService",
    "TaskRetirementService",
    "TaskRuntimeService",
    "TaskStateService",
]


def __getattr__(name: str) -> Any:
    if name == "TaskCleanupService":
        from .cleanup_service import TaskCleanupService

        return TaskCleanupService
    if name == "TaskGraphRetryService":
        from .graph_retry_service import TaskGraphRetryService

        return TaskGraphRetryService
    if name == "TaskInterruptService":
        from .interrupt_service import TaskInterruptService

        return TaskInterruptService
    if name == "TaskRetirementService":
        from .retirement_service import TaskRetirementService

        return TaskRetirementService
    if name == "RuntimeInputResult":
        from .runtime_input_service import RuntimeInputResult

        return RuntimeInputResult
    if name == "TaskRuntimeInputService":
        from .runtime_input_service import TaskRuntimeInputService

        return TaskRuntimeInputService
    if name == "TaskRuntimeService":
        from .runtime_service import TaskRuntimeService

        return TaskRuntimeService
    if name == "TaskLifecycleService":
        from .lifecycle_service import TaskLifecycleService

        return TaskLifecycleService
    if name == "TaskStateService":
        from .state_service import TaskStateService

        return TaskStateService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
