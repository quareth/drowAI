"""Task container management routes.

Responsibilities:
- Expose container status/create/list endpoints for task containers.
- Enforce tenant/user-owned task access and action policy before runtime operations.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...config import E2E_RUNTIME_LOCAL_MODE
from ...database import get_db
from ...models import Task, User
from ...services.tenant.authorization import ACTION_TASK_CONTROL, ACTION_TASK_READ
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from ...services.runtime_provider import RuntimeOperationService, provider_result_detail
from ...services.runtime_provider.contracts import RuntimeCallScope
from ...services.runtime_provider.snapshot_normalization import normalize_runtime_status_snapshot
from ...services.task.runtime_service import TaskRuntimeService
from .deps import enforce_tenant_action, get_tenant_task_or_404, map_admission_exception

logger = logging.getLogger(__name__)

router = APIRouter()


def _runtime_read_scope() -> RuntimeCallScope:
    """Use test scope only inside the explicit real-Docker browser canary process."""
    return RuntimeCallScope.TEST if E2E_RUNTIME_LOCAL_MODE else RuntimeCallScope.PRODUCT_TASK


def _raise_provider_failure(*, prefix: str, result) -> None:
    """Raise deterministic HTTP errors for runtime provider failures."""
    status_code = (
        status.HTTP_504_GATEWAY_TIMEOUT
        if str(result.error_code or "").strip() == "RUNNER_OPERATION_RESULT_TIMEOUT"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    raise HTTPException(status_code=status_code, detail=provider_result_detail(prefix, result))


@router.get("/{task_id}/container/status")
async def get_container_status(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get container status for a task."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
    task = get_tenant_task_or_404(
        db=db,
        task_id=task_id,
        tenant_context=tenant_context,
    )
    runtime_operations = RuntimeOperationService(db)
    context = RuntimeOperationService.context_from_authorized_task(
        task=task,
        user_id=current_user.id,
        runtime_call_scope=_runtime_read_scope(),
    )
    result = await runtime_operations.run_for_context(
        context=context,
        operation="get_runtime_status",
        call=lambda provider, request: provider.get_runtime_status(request),
        metadata={"wait_for_result": True, "wait_timeout_seconds": 8.0},
    )
    if not result.ok:
        _raise_provider_failure(prefix=f"Failed to get runtime status for task {task_id}", result=result)
    normalized = normalize_runtime_status_snapshot(result.metadata.get("delegate_result"))
    if normalized is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Invalid runtime status payload",
        )
    exists, status_info, details = normalized

    return {
        "task_id": task_id,
        "container_exists": exists,
        "status": status_info,
        "details": details,
    }


@router.post("/{task_id}/container/create")
async def create_task_container(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Create container for a task."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
    get_tenant_task_or_404(
        db=db,
        task_id=task_id,
        tenant_context=tenant_context,
    )

    try:
        task = await TaskRuntimeService(db).start_task(
            task_id=task_id,
            user_id=current_user.id,
            tenant_id=int(tenant_context.tenant_id),
        )

        return {
            "message": "Container created successfully",
            "container_id": getattr(task, "container_id", None),
            "container_name": f"drowai-task-{task_id}",
        }
    except HTTPException as exc:
        raise map_admission_exception(exc) from exc
    except Exception as e:
        logger.error(f"Failed to create container for task {task_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create container",
        )


@router.get("/containers/list")
async def list_all_containers(
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """List all containers for tasks in the active tenant."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
    try:
        tasks = db.query(Task).filter(Task.tenant_id == int(tenant_context.tenant_id)).all()
        runtime_operations = RuntimeOperationService(db)
        containers = []
        for task in tasks:
            context = RuntimeOperationService.context_from_authorized_task(
                task=task,
                user_id=current_user.id,
                runtime_call_scope=_runtime_read_scope(),
            )
            result = await runtime_operations.run_for_context(
                context=context,
                operation="get_runtime_status",
                call=lambda provider, request: provider.get_runtime_status(request),
                metadata={"wait_for_result": True, "wait_timeout_seconds": 8.0},
            )
            containers.append(
                {
                    "task_id": int(task.id),
                    "container_name": f"drowai-task-{int(task.id)}",
                    "status": result.metadata.get("delegate_result") if result.ok else "unknown",
                }
            )
        return {
            "containers": containers,
            "total": len(containers),
        }
    except Exception as e:
        logger.error(f"Failed to list containers: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list containers",
        )
