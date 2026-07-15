"""Task runtime retirement orchestration service.

Responsibilities:
- Retire task runtime artifacts (container + workspace) without deleting task data.
- Reuse existing docker/workspace/stream abstractions for teardown side effects.
- Surface explicit success/failure messages so callers can map outcomes to lifecycle status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeCallScope,
    normalize_runtime_call_scope,
)
from ..runtime_provider.operations import RuntimeOperationService, provider_result_detail
from ..runtime_provider.product_policy import (
    ProductRuntimePolicyError,
    decide_runtime_placement,
    resolve_product_runtime_policy,
)
from ..streaming.in_memory_hub import get_in_memory_stream_hub

logger = logging.getLogger(__name__)

PRODUCT_LOCAL_RUNTIME_PLACEMENT_REJECTED = "PRODUCT_LOCAL_RUNTIME_PLACEMENT_REJECTED"


@dataclass
class RuntimeRetirementResult:
    """Outcome of runtime retirement for a single task."""

    success: bool
    message: str


class TaskRetirementService:
    """Service that retires task runtime resources while preserving durable rows."""

    def __init__(self, *, runtime_operations_factory=RuntimeOperationService):
        self._runtime_operations_factory = runtime_operations_factory

    async def retire_runtime(
        self,
        *,
        task_id: int,
        user_id: int | None = None,
        engagement_id: int | None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeRetirementResult:
        """Retire container, workspace, and in-memory stream runtime state for a task."""
        try:
            from backend.database import SessionLocal

            normalized_scope = self._normalize_runtime_call_scope(runtime_call_scope)
            db = SessionLocal()
            try:
                runtime_operations = self._runtime_operations_factory(db)
                context = runtime_operations.context_for_internal_task(
                    task_id=task_id,
                    actor_type=RuntimeActorType.SYSTEM,
                    actor_id="task_retirement",
                    user_id=user_id,
                    runtime_call_scope=normalized_scope,
                )
                blocked_result = self._reject_product_local_context(
                    context=context,
                    task_id=task_id,
                    runtime_call_scope=normalized_scope,
                )
                if blocked_result is not None:
                    return blocked_result
                result = await runtime_operations.run_for_context(
                    context=context,
                    operation="retire_task_runtime",
                    call=lambda provider, request: provider.retire_task_runtime(request),
                    payload={
                        "force": True,
                        "engagement_id": engagement_id,
                        "wait_for_result": True,
                    },
                    metadata={
                        "wait_for_result": True,
                        "wait_timeout_seconds": 45.0,
                    },
                    runtime_call_scope=normalized_scope,
                )
            finally:
                db.close()
            if not result.ok:
                return RuntimeRetirementResult(
                    success=False,
                    message=provider_result_detail(
                        f"Failed to retire runtime for task {task_id}",
                        result,
                    ),
                )
        except Exception as exc:
            return RuntimeRetirementResult(
                success=False,
                message=f"Unexpected runtime retirement error for task {task_id}: {exc}",
            )

        await self.cleanup_runtime_stream_state(task_id=task_id)

        return RuntimeRetirementResult(
            success=True,
            message=f"Runtime retired for task {task_id}",
        )

    @staticmethod
    def _normalize_runtime_call_scope(
        runtime_call_scope: RuntimeCallScope | str,
    ) -> RuntimeCallScope:
        return normalize_runtime_call_scope(runtime_call_scope)

    @staticmethod
    def _reject_product_local_context(
        *,
        context,
        task_id: int,
        runtime_call_scope: RuntimeCallScope,
    ) -> RuntimeRetirementResult | None:
        try:
            decision = decide_runtime_placement(
                policy=resolve_product_runtime_policy(),
                scope=runtime_call_scope,
                requested_placement=getattr(context, "runtime_placement_mode", None),
            )
        except ProductRuntimePolicyError as exc:
            return RuntimeRetirementResult(
                success=False,
                message=f"PRODUCT_RUNTIME_POLICY_INVALID: task_id={int(task_id)} {exc}",
            )
        if not decision.allowed:
            return RuntimeRetirementResult(
                success=False,
                message=(
                    f"{PRODUCT_LOCAL_RUNTIME_PLACEMENT_REJECTED}: task_id={int(task_id)} "
                    "local runtime retirement is blocked in product scope"
                ),
            )
        return None

    @staticmethod
    async def cleanup_runtime_stream_state(*, task_id: int) -> None:
        """Remove in-memory stream state for one task after runtime retirement."""
        try:
            from backend.services.terminal.manager import terminal_session_manager

            await terminal_session_manager.close_task_sessions(task_id)
        except Exception:
            logger.debug(
                "Failed to cleanup terminal sessions during runtime retirement for task %s",
                task_id,
                exc_info=True,
            )
        try:
            from backend.services.runner_control.terminal_frame_buffer import (
                get_runner_terminal_frame_buffer,
            )

            get_runner_terminal_frame_buffer().clear_task(task_id=task_id)
        except Exception:
            logger.debug(
                "Failed to cleanup terminal frame buffers during runtime retirement for task %s",
                task_id,
                exc_info=True,
            )
        try:
            await get_in_memory_stream_hub().remove_task(task_id)
        except Exception:
            logger.debug(
                "Failed to cleanup in-memory stream state during runtime retirement for task %s",
                task_id,
                exc_info=True,
            )


__all__ = ["RuntimeRetirementResult", "TaskRetirementService"]
