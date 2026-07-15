"""Shared readiness and startup helpers for chat router endpoints."""

from __future__ import annotations

import logging
from typing import Any, Optional

from backend.services.langgraph_chat.contracts import (
    RuntimeWarmupStatus,
    runtime_warmup_status_from_steps,
)
from .schemas import ChatHistoryStartupPayload

logger = logging.getLogger(__name__)


def _compat():
    import backend.routers.chat as chat_package

    return chat_package


def _derive_task_running(task_status: object, active_run: Optional[Any]) -> bool:
    """Resolve task running flag from durable lifecycle first, then task row fallback."""
    run_state = str(getattr(active_run, "state", "") or "").strip().lower()
    if run_state in {"running", "waiting_for_human"}:
        return True
    return str(task_status).lower() == "running"


def _get_runtime_warmup_status(task_id: int) -> RuntimeWarmupStatus:
    """Build compact warmup readiness flags for API responses."""
    try:
        from backend.services.langgraph_chat.runtime.warmup_service import (
            get_shared_runtime_warmup_service,
        )

        raw_status = get_shared_runtime_warmup_service().get_warmup_status(task_id)
    except Exception:
        raw_status = {}

    return runtime_warmup_status_from_steps(raw_status)


async def _ensure_chat_prewarm(
    task_id: int,
    *,
    ensure_conversation: bool = True,
    preferred_conversation_id: Optional[str] = None,
) -> tuple[Optional[str], RuntimeWarmupStatus]:
    compat = _compat()
    conversation_id = (preferred_conversation_id or "").strip() or None
    if conversation_id is None and ensure_conversation:
        conversation_id = compat.ConversationManager(task_id).ensure_default_conversation()

    try:
        from backend.services.langgraph_chat.runtime.warmup_service import (
            get_shared_runtime_warmup_service,
        )

        warmup_service = get_shared_runtime_warmup_service()
        await warmup_service.warm_task_runtime(task_id)
    except Exception:
        logger.warning("Runtime warmup failed for task %s", task_id, exc_info=True)

    warmup_status = _get_runtime_warmup_status(task_id)

    try:
        from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

        hub = get_in_memory_stream_hub()
        hub.update_chat_metadata(task_id, conversation_id, warmup_status.checkpointer_ready)
    except Exception:
        logger.debug("Failed to update chat readiness metadata for task %s", task_id, exc_info=True)

    return conversation_id, warmup_status


async def _build_chat_startup_payload(
    *,
    task_id: int,
    task_running: bool,
    requested_conversation_id: Optional[str] = None,
) -> ChatHistoryStartupPayload:
    """Build startup readiness payload for initial chat history requests."""
    prewarmed_conversation_id, warmup_status = await _ensure_chat_prewarm(
        task_id,
        ensure_conversation=task_running,
        preferred_conversation_id=requested_conversation_id,
    )
    try:
        from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

        hub = get_in_memory_stream_hub()
        hub.set_task_running(task_id, task_running)
        ready_payload = hub.get_chat_ready_payload(task_id)
    except Exception:
        logger.debug("Failed to read startup readiness for task %s", task_id, exc_info=True)
        ready_payload = {
            "conversation_id": prewarmed_conversation_id,
            "task_running": task_running,
            "sse_connected": False,
            "chat_ready": task_running,
        }

    resolved_conversation_id = requested_conversation_id or prewarmed_conversation_id

    return ChatHistoryStartupPayload(
        task_id=task_id,
        conversation_id=resolved_conversation_id,
        checkpointer_ready=warmup_status.checkpointer_ready,
        tool_catalog_ready=warmup_status.tool_catalog_ready,
        pty_session_ready=warmup_status.pty_session_ready,
        runtime_warm=warmup_status.runtime_warm,
        pty_warmup_required=warmup_status.pty_warmup_required,
        task_running=bool(ready_payload.get("task_running")),
        sse_connected=bool(ready_payload.get("sse_connected")),
        chat_ready=bool(ready_payload.get("chat_ready")),
    )


__all__ = [
    "_build_chat_startup_payload",
    "_derive_task_running",
    "_ensure_chat_prewarm",
    "_get_runtime_warmup_status",
]
