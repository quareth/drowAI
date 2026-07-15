"""Task runtime-input append and container notification service.

This service centralizes writes to ``user_input.jsonl`` and the follow-up
``SIGUSR1`` notification so multiple routers can reuse the same task-runtime
behavior while preserving caller-specific failure policy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from backend.database import SessionLocal
from backend.services.runtime_provider.contracts import RuntimeActorType
from backend.services.runtime_provider.operations import RuntimeOperationService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeInputResult:
    """Execution facts returned from a runtime-input append and signal attempt."""

    persisted: bool
    signal_attempted: bool
    signal_sent: bool
    detail: str | None = None


class TaskRuntimeInputService:
    """Append runtime input entries and notify the task container."""

    async def append_and_signal(
        self,
        task_id: int,
        *,
        message: str,
        strict_persistence: bool,
        metadata: Mapping[str, Any] | None = None,
        user_id: int | None = None,
    ) -> RuntimeInputResult:
        """Append a runtime input entry and optionally signal the running container."""
        db = SessionLocal()
        try:
            runtime_operations = RuntimeOperationService(db)
            context = runtime_operations.context_for_internal_task(
                task_id=task_id,
                actor_type=RuntimeActorType.USER if user_id is not None else RuntimeActorType.SYSTEM,
                actor_id=user_id if user_id is not None else "runtime_input",
                user_id=user_id,
            )
            result = await runtime_operations.run_for_context(
                context=context,
                operation="append_runtime_input",
                call=lambda provider, request: provider.append_runtime_input(request),
                payload={
                    "message": message,
                    "strict_persistence": strict_persistence,
                    "metadata": dict(metadata or {}),
                },
                metadata={
                    "wait_for_result": True,
                    "wait_timeout_seconds": 10.0,
                },
            )
        finally:
            db.close()

        delegate = result.metadata.get("delegate_result")
        if not isinstance(delegate, Mapping):
            if not result.ok:
                return RuntimeInputResult(
                    persisted=False,
                    signal_attempted=False,
                    signal_sent=False,
                    detail=result.error_message,
                )
            delegate = {}

        return RuntimeInputResult(
            persisted=bool(delegate.get("persisted", result.ok)),
            signal_attempted=bool(delegate.get("signal_attempted", result.ok)),
            signal_sent=bool(delegate.get("signal_sent", result.ok)),
            detail=delegate.get("detail") or result.error_message,
        )
