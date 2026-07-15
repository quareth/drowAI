"""Task runtime control routes.

Responsibilities:
- Expose start/pause/resume/stop endpoints.
- Coordinate task state transitions with container runtime operations.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models import TaskResponse, TaskStatus, User
from ...services.task.runtime_service import TaskRuntimeService
from ...services.tenant.authorization import ACTION_TASK_CONTROL
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from .deps import (
    enforce_tenant_action,
    get_tenant_task_or_404,
    get_tenant_task_with_engagement_or_404,
    map_admission_exception,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _broadcast_status_update(task_id: int, status_value: str) -> None:
    """Best-effort task status broadcast to metrics subscribers."""
    try:
        from backend.services.websocket.connection_manager import websocket_manager

        await websocket_manager.broadcast_to_task(
            task_id,
            {"type": "status_update", "status": status_value},
        )
    except Exception:
        logger.debug("Failed to broadcast status update for task %s", task_id, exc_info=True)


@router.post("/{task_id}/start")
async def start_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Start a specific task with state validation."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    runtime_service = TaskRuntimeService(db)
    try:
        task = await runtime_service.start_task(
            task_id=task_id,
            user_id=current_user.id,
            tenant_id=tenant_context.tenant_id,
        )
    except HTTPException as exc:
        raise map_admission_exception(exc) from exc
    task_with_engagement = get_tenant_task_with_engagement_or_404(
        db=db,
        task_id=task.id,
        tenant_context=tenant_context,
    )
    return TaskResponse.model_validate(task_with_engagement)


@router.post("/{task_id}/pause")
async def pause_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Pause a specific task with state validation."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    runtime_service = TaskRuntimeService(db)
    result = await runtime_service.pause_task(
        task_id=task_id,
        user_id=current_user.id,
        tenant_id=tenant_context.tenant_id,
    )

    if isinstance(result, dict):
        return result

    if getattr(result, "status", None) == TaskStatus.PAUSED.value:
        await _broadcast_status_update(task_id=task_id, status_value=TaskStatus.PAUSED.value)
    task_with_engagement = get_tenant_task_with_engagement_or_404(
        db=db,
        task_id=result.id,
        tenant_context=tenant_context,
    )
    return TaskResponse.model_validate(task_with_engagement)


@router.post("/{task_id}/resume")
async def resume_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Resume a paused task with state validation."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    runtime_service = TaskRuntimeService(db)
    result = await runtime_service.resume_task(
        task_id=task_id,
        user_id=current_user.id,
        tenant_id=tenant_context.tenant_id,
    )

    if isinstance(result, dict):
        return result

    if getattr(result, "status", None) == TaskStatus.RUNNING.value:
        await _broadcast_status_update(task_id=task_id, status_value=TaskStatus.RUNNING.value)
    task_with_engagement = get_tenant_task_with_engagement_or_404(
        db=db,
        task_id=result.id,
        tenant_context=tenant_context,
    )
    return TaskResponse.model_validate(task_with_engagement)


@router.post("/{task_id}/stop")
async def stop_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Stop a specific task with state validation."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    runtime_service = TaskRuntimeService(db)
    task = await runtime_service.stop_task(
        task_id=task_id,
        user_id=current_user.id,
        tenant_id=tenant_context.tenant_id,
    )
    task_with_engagement = get_tenant_task_with_engagement_or_404(
        db=db,
        task_id=task.id,
        tenant_context=tenant_context,
    )
    return TaskResponse.model_validate(task_with_engagement)
