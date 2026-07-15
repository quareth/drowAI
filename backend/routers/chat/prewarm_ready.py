"""Prewarm and readiness endpoints for task chat sessions."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models.core import User
from ...services.tenant.authorization import ACTION_CHAT_READ
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from ..tasks.deps import enforce_tenant_action, get_tenant_task_or_404
from .readiness import _derive_task_running, _ensure_chat_prewarm
from .schemas import ChatPrewarmResponse, ChatReadyResponse

router = APIRouter()
logger = logging.getLogger(__name__)


def _compat():
    import backend.routers.chat as chat_package

    return chat_package


@router.post("/tasks/{task_id}/chat/prewarm", response_model=ChatPrewarmResponse)
async def prewarm_chat(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Warm up per-task chat resources to avoid first-message latency."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_READ)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    db.rollback()

    conversation_id, warmup_status = await _ensure_chat_prewarm(task_id, ensure_conversation=True)
    if not conversation_id:
        conversation_id = _compat().ConversationManager(task_id).ensure_default_conversation()

    return ChatPrewarmResponse(
        task_id=task_id,
        conversation_id=conversation_id,
        checkpointer_ready=warmup_status.checkpointer_ready,
        tool_catalog_ready=warmup_status.tool_catalog_ready,
        pty_session_ready=warmup_status.pty_session_ready,
        runtime_warm=warmup_status.runtime_warm,
        pty_warmup_required=warmup_status.pty_warmup_required,
    )


@router.get("/tasks/{task_id}/chat/ready", response_model=ChatReadyResponse)
async def chat_ready(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Return whether chat can accept immediate sends for the task."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_READ)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    active_run = _compat().get_run_lifecycle_service().get_active_run(task_id, db_session=db)
    run_state = str(getattr(active_run, "state", "") or "").strip().lower()
    task_running = _derive_task_running(task.status, active_run)
    task_status = str(task.status).lower()
    db.rollback()
    conversation_id, warmup_status = await _ensure_chat_prewarm(
        task_id,
        ensure_conversation=task_running,
    )
    logger.debug(
        (
            "Chat readiness check start for task %s: running=%s "
            "run_state=%s task_status=%s checkpointer_ready=%s conv=%s"
        ),
        task_id,
        task_running,
        run_state or "none",
        task_status,
        warmup_status.checkpointer_ready,
        "set" if conversation_id else "missing",
    )
    try:
        from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

        hub = get_in_memory_stream_hub()
        hub.set_task_running(task_id, task_running)
        payload = hub.get_chat_ready_payload(task_id)
        logger.info(
            "Chat readiness payload for task %s: ready=%s running=%s checkpointer=%s sse=%s",
            task_id,
            payload.get("chat_ready"),
            payload.get("task_running"),
            payload.get("checkpointer_ready"),
            payload.get("sse_connected"),
        )
        return ChatReadyResponse(
            task_id=task_id,
            conversation_id=payload.get("conversation_id"),
            checkpointer_ready=warmup_status.checkpointer_ready,
            tool_catalog_ready=warmup_status.tool_catalog_ready,
            pty_session_ready=warmup_status.pty_session_ready,
            runtime_warm=warmup_status.runtime_warm,
            pty_warmup_required=warmup_status.pty_warmup_required,
            task_running=bool(payload.get("task_running")),
            sse_connected=bool(payload.get("sse_connected")),
            chat_ready=bool(payload.get("chat_ready")),
        )
    except Exception:
        logger.debug("Failed to read chat readiness for task %s", task_id, exc_info=True)
        chat_ready_value = task_running and bool(conversation_id)
        return ChatReadyResponse(
            task_id=task_id,
            conversation_id=conversation_id,
            checkpointer_ready=warmup_status.checkpointer_ready,
            tool_catalog_ready=warmup_status.tool_catalog_ready,
            pty_session_ready=warmup_status.pty_session_ready,
            runtime_warm=warmup_status.runtime_warm,
            pty_warmup_required=warmup_status.pty_warmup_required,
            task_running=task_running,
            sse_connected=False,
            chat_ready=chat_ready_value,
        )


__all__ = ["chat_ready", "prewarm_chat", "router"]
