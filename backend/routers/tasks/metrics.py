"""Task metrics routes and metrics WebSocket endpoint.

Responsibilities:
- Expose task metrics and metrics history HTTP endpoints.
- Handle real-time metrics WebSocket connectivity and heartbeat flow.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...core.rate_limiter import rate_limit
from ...database import get_db
from ...models import User
from ...services.runtime_provider import RuntimeOperationService, provider_result_detail
from ...services.runtime_provider.snapshot_normalization import normalize_runtime_metrics_snapshot
from ...services.tenant.authorization import ACTION_TASK_READ
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from ...services.websocket.alias_gateway import authorize_alias_websocket
from ...services.websocket.gateway import (
    enforce_ws_task_ownership,
)
from ...services.websocket.channel_handlers import serve_metrics_task_websocket
from .deps import enforce_tenant_action, get_tenant_task_or_404

logger = logging.getLogger(__name__)

router = APIRouter()


def _raise_provider_failure(*, prefix: str, result) -> None:
    """Raise deterministic HTTP errors for runtime provider failures."""
    status_code = (
        status.HTTP_504_GATEWAY_TIMEOUT
        if str(result.error_code or "").strip() == "RUNNER_OPERATION_RESULT_TIMEOUT"
        else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    raise HTTPException(status_code=status_code, detail=provider_result_detail(prefix, result))


@router.get("/{task_id}/metrics")
@rate_limit(max_calls=30, window=60)
async def get_task_metrics(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get real-time container metrics for a task."""
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
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Invalid runtime metrics payload",
        )

    return {"task_id": task_id, "metrics": metrics}


@router.get("/{task_id}/metrics/history")
async def get_task_metrics_history(
    task_id: int,
    hours: int = Query(1, ge=1, le=24),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get historical metrics data for a task."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_TASK_READ)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)

    return {"task_id": task_id, "hours": hours, "history": []}


@router.websocket("/ws/tasks/{task_id}/metrics")
async def websocket_task_metrics(
    websocket: WebSocket,
    task_id: int,
):
    """WebSocket endpoint for real-time metrics streaming."""
    try:
        auth_ctx = await authorize_alias_websocket(
            websocket,
            task_id=task_id,
            endpoint="/api/tasks/ws/tasks/{task_id}/metrics",
            canonical="/ws?type=metrics&taskId=<id>",
        )
        if auth_ctx is None:
            return

        await serve_metrics_task_websocket(
            websocket,
            task_id,
            user_id=auth_ctx.user_id,
            ownership_enforcer=enforce_ws_task_ownership,
        )
    except Exception as e:
        logger.error(f"Metrics WebSocket connection error for task {task_id}: {e}")
        if websocket.client_state.name != "DISCONNECTED":
            await websocket.close()
