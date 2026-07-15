"""Cancellation endpoint for active interactive chat runs."""

from typing import Optional

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models.core import User
from ...services.langgraph_chat.runtime.tool_cancel_service import ChatToolCancelProjectionService
from ...services.langgraph_chat.runtime.tool_cancel_stream_projection import (
    ChatToolCancelStreamProjectionService,
)
from ...services.tenant.authorization import ACTION_CHAT_CANCEL
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from ..tasks.deps import enforce_tenant_action, get_tenant_task_or_404
from .schemas import ChatCancelRequest

router = APIRouter()


def _compat():
    import backend.routers.chat as chat_package

    return chat_package


@router.post("/tasks/{task_id}/chat/cancel")
async def cancel_chat_run(
    task_id: int,
    payload: Optional[ChatCancelRequest] = Body(None),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Request explicit cancellation for the task's active interactive run."""
    if payload is None:
        payload = ChatCancelRequest()
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_CANCEL)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    lifecycle = _compat().get_run_lifecycle_service()
    result = lifecycle.request_cancel(
        task_id=task_id,
        turn_id=payload.turn_id,
        reason=payload.reason,
        db_session=db,
    )
    resolved_turn_id = str(result.get("turn_id") or payload.turn_id or "").strip() or None
    cancel_reason = str(result.get("cancel_reason") or payload.reason or "explicit_cancel").strip() or "explicit_cancel"
    terminalized = False
    cancel_accepted = bool(result.get("cancelled") or result.get("already_cancelled"))
    tool_cancel_service = ChatToolCancelProjectionService(db)
    if cancel_accepted:
        tool_projection = await tool_cancel_service.mark_turn_cancel_requested(
            tenant_id=tenant_context.tenant_id,
            task_id=task_id,
            turn_id=resolved_turn_id,
            reason=cancel_reason,
        )
    else:
        tool_projection = tool_cancel_service.empty_result()
    if resolved_turn_id and cancel_accepted:
        lifecycle.end_run(
            task_id=task_id,
            turn_id=resolved_turn_id,
            status="cancelled",
            db_session=db,
        )
        await ChatToolCancelStreamProjectionService(db).publish_cancelled_turn(
            tenant_id=tenant_context.tenant_id,
            task_id=task_id,
            turn_id=resolved_turn_id,
            tool_cancellation=tool_projection,
        )
        terminalized = True
    if result.get("cancelled"):
        status_value = "cancelled" if terminalized else "cancel_requested"
    elif result.get("already_cancelled"):
        status_value = "cancelled" if terminalized else "already_cancelled"
    else:
        reason_value = str(result.get("reason") or "").strip()
        status_value = reason_value or "not_running"
    return {
        "task_id": task_id,
        "turn_id": resolved_turn_id,
        "cancelled": bool(result.get("cancelled")),
        "already_cancelled": bool(result.get("already_cancelled")),
        "active": bool(result.get("active")),
        "status": status_value,
        "reason": result.get("reason"),
        "cancel_reason": cancel_reason,
        "terminalized": terminalized,
        "tool_cancellation": {
            "marked_count": tool_projection.marked_count,
            "execution_ids": list(tool_projection.execution_ids),
            "tool_call_ids": list(tool_projection.tool_call_ids),
            "command_ids": list(tool_projection.command_ids),
            "runtime_job_ids": list(tool_projection.runtime_job_ids),
            "process_state": tool_projection.process_state,
            "runtime_kill_attempted": tool_projection.runtime_kill_attempted,
            "runtime_kill_supported": tool_projection.runtime_kill_supported,
        },
    }


__all__ = ["cancel_chat_run", "router"]
