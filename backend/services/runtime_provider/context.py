"""Runtime-provider request context resolver.

Responsibilities:
- Build a canonical runtime identity payload from authorized task records.
- Support user-originated and internal task-id-only resolution paths.
- Keep tenant/placement/actor metadata consistent for provider calls and
  checkpoint continuation worker carriers.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Task
from backend.services.langgraph_chat.checkpoint.thread_identity import require_graph_thread_id
from backend.services.task.access_service import get_tenant_task_or_404

from .contracts import RuntimeActorType, RuntimeCallScope, normalize_runtime_call_scope
from .registry import UnsupportedRuntimePlacementError, resolve_task_runtime_placement_mode


@dataclass(frozen=True, slots=True)
class RuntimeRequestContext:
    """Canonical runtime identity resolved from a task row."""

    tenant_id: int
    task_id: int
    graph_thread_id: str
    workspace_id: str
    runtime_placement_mode: str
    actor_type: RuntimeActorType
    actor_id: str
    user_id: int | None
    runner_id: str | None
    execution_site_id: str | None
    runtime_call_scope: RuntimeCallScope = RuntimeCallScope.PRODUCT_TASK

    def to_worker_payload(self) -> dict[str, object | None]:
        """Return additive worker payload fields for resume/retry carriers."""
        return {
            "tenant_id": self.tenant_id,
            "graph_thread_id": self.graph_thread_id,
            "workspace_id": self.workspace_id,
            "runtime_placement_mode": self.runtime_placement_mode,
            "actor_type": self.actor_type.value,
            "actor_id": self.actor_id,
            "user_id": self.user_id,
            "runner_id": self.runner_id,
            "execution_site_id": self.execution_site_id,
            "runtime_call_scope": self.runtime_call_scope.value,
        }


class RuntimeProviderContextResolver:
    """Resolve runtime identity from task records for provider-bound operations."""

    def __init__(self, db: Session):
        self.db = db

    def resolve_user_task_context(
        self,
        *,
        task_id: int,
        user_id: int,
        tenant_id: int,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeRequestContext:
        """Resolve context for authenticated user-originated task operations."""
        task = get_tenant_task_or_404(
            db=self.db,
            task_id=int(task_id),
            user_id=int(user_id),
            tenant_id=int(tenant_id),
        )
        return self._from_task(
            task=task,
            actor_type=RuntimeActorType.USER,
            actor_id=str(int(user_id)),
            user_id=int(user_id),
            runtime_call_scope=runtime_call_scope,
        )

    @classmethod
    def context_from_task(
        cls,
        *,
        task: Task,
        user_id: int | None,
        actor_type: RuntimeActorType = RuntimeActorType.USER,
        actor_id: str | int | None = None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeRequestContext:
        """Build user-originated runtime context from an already-authorized task."""
        resolved_actor_id = actor_id
        if resolved_actor_id is None:
            resolved_actor_id = user_id if user_id is not None else actor_type.value
        return cls._from_task(
            task=task,
            actor_type=actor_type,
            actor_id=str(resolved_actor_id),
            user_id=int(user_id) if user_id is not None else None,
            runtime_call_scope=runtime_call_scope,
        )

    def resolve_internal_task_context(
        self,
        *,
        task_id: int,
        actor_type: RuntimeActorType,
        actor_id: str | int | None = None,
        user_id: int | None = None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeRequestContext:
        """Resolve context for internal task-id-only operations."""
        task = self.db.execute(select(Task).where(Task.id == int(task_id))).scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        resolved_actor_id = actor_id
        if resolved_actor_id is None:
            resolved_actor_id = user_id if user_id is not None else actor_type.value
        return self._from_task(
            task=task,
            actor_type=actor_type,
            actor_id=str(resolved_actor_id),
            user_id=user_id,
            runtime_call_scope=runtime_call_scope,
        )

    @staticmethod
    def _from_task(
        *,
        task: Task,
        actor_type: RuntimeActorType,
        actor_id: str,
        user_id: int | None,
        runtime_call_scope: RuntimeCallScope | str = RuntimeCallScope.PRODUCT_TASK,
    ) -> RuntimeRequestContext:
        try:
            normalized_scope = normalize_runtime_call_scope(runtime_call_scope)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        tenant_id = getattr(task, "tenant_id", None)
        try:
            resolved_tenant_id = int(tenant_id)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "Task runtime context requires a valid tenant_id. "
                    "Task ownership metadata is missing or invalid."
                ),
            ) from None
        resolved_user_id = user_id
        if resolved_user_id is None:
            task_user_id = getattr(task, "user_id", None)
            try:
                resolved_user_id = int(task_user_id) if task_user_id is not None else None
            except (TypeError, ValueError):
                resolved_user_id = None
        runtime_mode_candidate = getattr(task, "runtime_placement_mode", None)
        if (
            normalized_scope in {RuntimeCallScope.PRODUCT, RuntimeCallScope.PRODUCT_TASK}
            and not str(runtime_mode_candidate or "").strip()
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "MISSING_RUNTIME_PLACEMENT",
                    "task_id": int(task.id),
                    "scope": normalized_scope.value,
                    "message": (
                        "Product task runtime context requires explicit "
                        "runtime_placement_mode."
                    ),
                },
            )
        try:
            runtime_mode = resolve_task_runtime_placement_mode(task).value
        except (UnsupportedRuntimePlacementError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Task runtime context has unsupported runtime_placement_mode. "
                    f"{exc}"
                ),
            ) from exc
        workspace_id = str(getattr(task, "workspace_id", "") or f"task-{int(task.id)}")
        try:
            graph_thread_id = require_graph_thread_id(
                getattr(task, "graph_thread_id", None),
                task_id=int(task.id),
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Task runtime context requires a valid graph_thread_id.",
            ) from exc
        runner_id = getattr(task, "runner_id", None)
        execution_site_id = getattr(task, "execution_site_id", None)
        return RuntimeRequestContext(
            tenant_id=resolved_tenant_id,
            task_id=int(task.id),
            graph_thread_id=graph_thread_id,
            workspace_id=workspace_id,
            runtime_placement_mode=runtime_mode,
            actor_type=actor_type,
            actor_id=str(actor_id),
            user_id=resolved_user_id,
            runner_id=str(runner_id) if isinstance(runner_id, (int, str)) else None,
            execution_site_id=(
                str(execution_site_id)
                if isinstance(execution_site_id, (int, str))
                else None
            ),
            runtime_call_scope=normalized_scope,
        )


__all__ = [
    "RuntimeProviderContextResolver",
    "RuntimeRequestContext",
]
