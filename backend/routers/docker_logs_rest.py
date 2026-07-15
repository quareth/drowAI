"""Runtime logs and status compatibility REST routes.

Responsibilities:
- Expose legacy log/status/metrics REST endpoints under `/api/docker/*`.
- Delegate task runtime operations through the runtime-provider boundary.

Boundary:
- No websocket transport handling.
- No terminal session lifecycle endpoints.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.auth import get_current_user
from backend.core.time_utils import format_iso, utc_now
from backend.database import get_db
from backend.models.core import TaskStatus, User
from backend.services.runtime_provider import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationRequest,
    RuntimeOperationService,
    RuntimePlacementMode,
    RuntimeProviderRegistry,
    provider_result_detail,
)
from backend.services.runtime_provider.snapshot_normalization import (
    normalize_runtime_logs_snapshot,
    normalize_runtime_metrics_snapshot,
    normalize_runtime_startup_progress_snapshot,
    normalize_runtime_status_snapshot,
)
from backend.services.tenant.authorization import ACTION_TASK_CONTROL, ACTION_TASK_READ
from backend.services.tenant.context import TenantRequestContext
from backend.services.tenant.dependencies import get_tenant_request_context
from backend.services.task.runtime_service import TaskRuntimeService
from backend.routers.tasks.deps import enforce_tenant_action, get_tenant_task_or_404

logger = logging.getLogger(__name__)
router = APIRouter()


def _raise_provider_failure(*, prefix: str, result) -> None:
    """Raise deterministic HTTP errors for provider operation failures."""
    status_code = (
        status.HTTP_504_GATEWAY_TIMEOUT
        if str(result.error_code or "").strip() == "RUNNER_OPERATION_RESULT_TIMEOUT"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    raise HTTPException(status_code=status_code, detail=provider_result_detail(prefix, result))


@router.get("/docker-compose/logs/{task_id}")
async def get_docker_compose_logs(
    task_id: int,
    lines: int = 50,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Get runtime logs for a specific task.
    """
    try:
        enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
        task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
        runtime_operations = RuntimeOperationService(db)
        result = await runtime_operations.run_authorized_task_operation(
            task=task,
            user_id=current_user.id,
            operation="get_runtime_logs",
            call=lambda provider, request: provider.get_runtime_logs(request),
            payload={"lines": lines},
            metadata={"wait_for_result": True, "wait_timeout_seconds": 8.0},
        )
        if not result.ok:
            _raise_provider_failure(prefix=f"Failed to get runtime logs for task {task_id}", result=result)
        logs = normalize_runtime_logs_snapshot(result.metadata.get("delegate_result"))

        return {
            "task_id": task_id,
            "container_name": f"drowai-task-{task_id}",
            "logs": logs,
            "total_lines": len(logs),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get runtime logs: {str(e)}")


@router.get("/docker-compose/progress/{task_id}")
async def get_container_startup_progress(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Get detailed runtime startup progress information.
    """
    try:
        enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
        task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
        runtime_operations = RuntimeOperationService(db)
        result = await runtime_operations.run_authorized_task_operation(
            task=task,
            user_id=current_user.id,
            operation="get_runtime_startup_progress",
            call=lambda provider, request: provider.get_runtime_startup_progress(request),
            metadata={"wait_for_result": True, "wait_timeout_seconds": 8.0},
        )
        if not result.ok:
            _raise_provider_failure(prefix=f"Failed to get startup progress for task {task_id}", result=result)
        progress = normalize_runtime_startup_progress_snapshot(result.metadata.get("delegate_result"))
        if progress is None:
            raise HTTPException(status_code=502, detail="Invalid runtime startup progress payload")
        return progress
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get runtime progress: {str(e)}")


@router.get("/docker-compose/status")
async def get_docker_compose_status(current_user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """
    Return a safe local runtime diagnostic summary.
    """
    request = RuntimeOperationRequest(
        tenant_id="diagnostic",
        task_id=0,
        actor_type=RuntimeActorType.USER,
        actor_id=current_user.id,
        user_id=current_user.id,
        runtime_placement_mode=RuntimePlacementMode.LOCAL,
        workspace_id="diagnostic",
        operation="list_runtime_inventory",
        runtime_call_scope=RuntimeCallScope.DIAGNOSTIC,
    )
    try:
        provider = RuntimeProviderRegistry().get_provider(runtime_placement_mode=RuntimePlacementMode.LOCAL)
        result = await provider.list_runtime_inventory(request)
    except Exception:
        logger.exception("Runtime diagnostic status query failed")
        return {
            "status": "unavailable",
            "docker_available": False,
            "scope": "local_diagnostic",
        }

    delegate_result = result.metadata.get("delegate_result")
    inventory_total = (
        delegate_result.get("total")
        if isinstance(delegate_result, dict) and isinstance(delegate_result.get("total"), int)
        else None
    )
    return {
        "status": "diagnostic" if result.ok else "unavailable",
        "docker_available": bool(result.ok),
        "scope": "local_diagnostic",
        "provider": result.provider,
        "inventory_total": inventory_total,
    }


@router.post("/execute-command/{task_id}")
async def execute_command(
    task_id: int,
    command: str,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Execute a command in the task runtime and return output.
    """
    try:
        enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
        task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
        runtime_operations = RuntimeOperationService(db)
        result = await runtime_operations.run_authorized_task_operation(
            task=task,
            user_id=current_user.id,
            operation="execute_runtime_command",
            call=lambda provider, request: provider.execute_runtime_command(request),
            payload={"command": command},
        )
        if not result.ok:
            _raise_provider_failure(
                prefix=f"Failed to execute runtime command for task {task_id}",
                result=result,
            )
        logs = result.metadata.get("delegate_result")
        return {
            "task_id": task_id,
            "command": command,
            "logs": logs,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to execute command: {str(e)}")


@router.post("/workspace/cleanup/{task_id}")
async def cleanup_runtime_workspace(
    task_id: int,
    cleanup_scope: str = "workspace",
    retain_outputs: bool = True,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Run provider-mediated runtime workspace cleanup for one task."""
    try:
        enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
        task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
        runtime_operations = RuntimeOperationService(db)
        result = await runtime_operations.run_authorized_task_operation(
            task=task,
            user_id=current_user.id,
            operation="cleanup_runtime_workspace",
            call=lambda provider, request: provider.cleanup_runtime_workspace(request),
            payload={
                "cleanup_scope": cleanup_scope,
                "retain_outputs": retain_outputs,
            },
            metadata={"wait_for_result": True, "wait_timeout_seconds": 8.0},
        )
        if not result.ok:
            _raise_provider_failure(prefix=f"Failed to cleanup runtime workspace for task {task_id}", result=result)
        delegate_result = result.metadata.get("delegate_result") if result.metadata else None
        success = True
        if isinstance(delegate_result, Mapping):
            if "success" in delegate_result:
                success = bool(delegate_result.get("success"))
            elif "cleaned" in delegate_result:
                success = bool(delegate_result.get("cleaned"))
        return {
            "task_id": task_id,
            "success": success,
            "cleanup_scope": cleanup_scope,
            "retain_outputs": retain_outputs,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cleanup runtime workspace: {str(e)}")


@router.delete("/stop-container/{task_id}")
async def stop_container(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Stop a running task runtime.
    """
    try:
        enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_CONTROL)
        get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
        stopped_task = await TaskRuntimeService(db).stop_task(
            task_id=task_id,
            user_id=current_user.id,
            tenant_id=int(tenant_context.tenant_id),
        )
        task_status = str(getattr(stopped_task, "status", "") or "").strip()
        is_stopped = task_status == TaskStatus.STOPPED.value
        is_stopping = task_status == TaskStatus.STOPPING.value
        success = True
        message = "Runtime stopped" if is_stopped else "Runtime stop accepted; awaiting runner confirmation"
        logs = [
            {
                "timestamp": format_iso(utc_now()),
                "service": "runtime-provider",
                "level": "info" if success else "error",
                "message": message,
            }
        ]
        return {
            "task_id": task_id,
            "logs": logs,
            "status": "stopping" if is_stopping else ("stopped" if is_stopped else "accepted"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to stop runtime: {str(e)}")


@router.get("/container/metrics/{task_id}")
async def get_container_metrics(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Get real-time runtime resource metrics.
    """
    try:
        enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
        task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
        runtime_operations = RuntimeOperationService(db)
        result = await runtime_operations.run_authorized_task_operation(
            task=task,
            user_id=current_user.id,
            operation="get_runtime_metrics",
            call=lambda provider, request: provider.get_runtime_metrics(request),
            metadata={"wait_for_result": True, "wait_timeout_seconds": 8.0},
        )
        if not result.ok:
            _raise_provider_failure(prefix=f"Failed to get runtime metrics for task {task_id}", result=result)
        metrics = normalize_runtime_metrics_snapshot(result.metadata.get("delegate_result"))
        if metrics is None:
            raise HTTPException(status_code=502, detail="Invalid runtime metrics payload")
        return {"task_id": task_id, "metrics": metrics}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get runtime metrics: {str(e)}")


@router.get("/container/status/{task_id}")
async def get_container_status(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Get runtime status for a specific task.
    """
    try:
        enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
        task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
        runtime_operations = RuntimeOperationService(db)
        result = await runtime_operations.run_authorized_task_operation(
            task=task,
            user_id=current_user.id,
            operation="get_runtime_status",
            call=lambda provider, request: provider.get_runtime_status(request),
            metadata={"wait_for_result": True, "wait_timeout_seconds": 8.0},
        )
        if not result.ok:
            _raise_provider_failure(prefix=f"Failed to get runtime status for task {task_id}", result=result)
        normalized = normalize_runtime_status_snapshot(result.metadata.get("delegate_result"))
        if normalized is None:
            raise HTTPException(status_code=502, detail="Invalid runtime status payload")
        exists, status_text, _details = normalized
        return {
            "task_id": task_id,
            "container_name": f"drowai-task-{task_id}",
            "status": status_text,
            "docker_available": True,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get runtime status: {str(e)}")
