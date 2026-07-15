"""Streaming and queue status endpoints for chat tasks."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models.core import Task, User
from ...services.tenant.authorization import ACTION_CHAT_READ
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from ..tasks.deps import enforce_tenant_action, get_tenant_task_or_404

router = APIRouter()
logger = logging.getLogger(__name__)


def _compat():
    import backend.routers.chat as chat_package

    return chat_package


def _build_run_payload(run: Optional[Any]) -> Dict[str, Any]:
    if run is None:
        return {
            "state": "idle",
            "turn_id": None,
            "cancel_requested": False,
        }
    payload: Dict[str, Any] = {
        "state": run.state,
        "turn_id": run.turn_id,
        "cancel_requested": bool(getattr(run, "cancel_requested", False)),
        "cancel_reason": getattr(run, "cancel_reason", None),
        "started_at": getattr(run, "started_at", None),
        "updated_at": getattr(run, "updated_at", None),
    }
    ended_at = getattr(run, "ended_at", None)
    if ended_at is not None:
        payload["ended_at"] = ended_at
    return payload


@router.get("/tasks/{task_id}/streaming-status")
async def get_streaming_status(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get the current streaming status for a task."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_READ)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)

    try:
        from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

        hub = get_in_memory_stream_hub()
        is_streaming = hub.is_task_streaming(task_id)
        queued_count = hub.get_queued_count(task_id)
        active_run = _compat().get_run_lifecycle_service().get_active_run(task_id, db_session=db)
        run_payload = _compat()._build_run_payload(active_run)

        return {
            "task_id": task_id,
            "is_streaming": is_streaming,
            "queued_count": queued_count,
            "mode": task.mode or "interactive",
            "run": run_payload,
        }
    except Exception as exc:
        logger.error("Failed to get streaming status for task %s: %s", task_id, exc)
        return {
            "task_id": task_id,
            "is_streaming": False,
            "queued_count": 0,
            "run": {"state": "unknown", "turn_id": None, "cancel_requested": False},
            "error": str(exc),
        }


@router.get("/interactive-runs/statuses")
async def get_streaming_statuses(
    task_ids: List[int] = Query(default=[]),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get streaming status for multiple user-owned tasks in one request."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_READ)
    unique_task_ids = sorted({task_id for task_id in task_ids if isinstance(task_id, int) and task_id > 0})
    if not unique_task_ids:
        return {"tasks": []}

    tenant_scoped_ids = {
        row.id
        for row in db.query(Task.id)
        .filter(
            Task.tenant_id == int(tenant_context.tenant_id),
            Task.user_id == int(current_user.id),
            Task.id.in_(unique_task_ids),
        )
        .all()
    }
    filtered_task_ids = [task_id for task_id in unique_task_ids if task_id in tenant_scoped_ids]
    if not filtered_task_ids:
        return {"tasks": []}

    compat = _compat()
    run_by_task = compat.get_run_lifecycle_service().get_runs_for_tasks(filtered_task_ids, db_session=db)
    hub = None
    try:
        from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

        hub = get_in_memory_stream_hub()
    except Exception:
        logger.debug("In-memory stream hub unavailable for batch status", exc_info=True)

    task_rows = db.query(Task.id, Task.mode).filter(Task.id.in_(filtered_task_ids)).all()
    task_mode_by_id = {row.id: row.mode for row in task_rows}

    tasks_payload: List[Dict[str, Any]] = []
    for task_id in filtered_task_ids:
        is_streaming = bool(hub.is_task_streaming(task_id)) if hub is not None else False
        queued_count = int(hub.get_queued_count(task_id)) if hub is not None else 0
        tasks_payload.append(
            {
                "task_id": task_id,
                "is_streaming": is_streaming,
                "queued_count": queued_count,
                "mode": (task_mode_by_id.get(task_id) or "interactive"),
                "run": compat._build_run_payload(run_by_task.get(task_id)),
            }
        )

    return {"tasks": tasks_payload}


@router.get("/tasks/{task_id}/queue-status")
async def get_queue_status(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Get the current queue status for a task (for debugging)."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_READ)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)

    try:
        from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

        hub = get_in_memory_stream_hub()
        return {
            "task_id": task_id,
            "is_streaming": hub.is_task_streaming(task_id),
            "queued_count": hub.get_queued_count(task_id),
        }
    except Exception as exc:
        logger.warning("Failed to get queue status for task %s: %s", task_id, exc)
        return {
            "task_id": task_id,
            "is_streaming": False,
            "queued_count": 0,
            "error": str(exc),
        }


__all__ = [
    "_build_run_payload",
    "get_queue_status",
    "get_streaming_status",
    "get_streaming_statuses",
    "router",
]
