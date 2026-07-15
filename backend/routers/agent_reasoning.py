"""Reasoning stream and history routes with SSE replay/recovery orchestration.

SSE endpoints in this module are compatibility/fallback transport surfaces.
Interactive primary live transport is the multiplex WebSocket path (`/ws`,
`agent-multi`) after cutover. These routes must not control or couple task run
lifecycle (start/stop/cancel); they only expose stream/history delivery.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.graph.contracts.streaming_constants import ANSWER_PHASE_INDEX, STEP_MESSAGE_DELTA
from backend.auth import extract_active_tenant_hint, resolve_user_from_token_payload, verify_token_with_error
from backend.database import SessionLocal, get_db
from backend.models.core import User
from backend.services.streaming.reasoning_history_service import AgentReasoningHistoryService
from backend.services.streaming.event_store import StreamEventStore
from backend.services.streaming.reasoning_sse_service import PersistedListAfter, ReasoningSSEService
from backend.services.task.access_service import get_owned_task_or_404
from backend.services.task.runtime_input_service import TaskRuntimeInputService
from backend.services.tenant.authorization import (
    ACTION_CHAT_WRITE,
    ACTION_STREAM_REPLAY,
    ACTION_STREAM_SUBSCRIBE,
    decide_action,
)
from backend.services.tenant.context import (
    TenantContextResolutionError,
    TenantContextService,
    TenantRequestContext,
)
from backend.services.tenant.dependencies import (
    ACTIVE_TENANT_HEADER,
    map_tenant_context_error,
    parse_requested_tenant_id,
)
from backend.services.tenant.rls import (
    clear_rls_session_context,
    set_tenant_rls_context,
    set_user_lookup_rls_context,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_reasoning_sse_service = ReasoningSSEService()
_runtime_input_service = TaskRuntimeInputService()


class UserMessage(BaseModel):
    """Payload schema for user provided messages."""

    message: str
    client_message_id: Optional[str] = None


def _close_short_lived_session(db: Session) -> None:
    """Reset RLS context and close short-lived router-owned sessions safely."""
    try:
        clear_rls_session_context(db)
    finally:
        db.close()


def _prepare_reasoning_stream_preflight(task_id: int, request: Request) -> None:
    """Authorize the stream request in a short-lived DB session."""
    db = SessionLocal()
    try:
        _authorize_task_action(
            task_id=task_id,
            request=request,
            db=db,
            action=ACTION_STREAM_SUBSCRIBE,
        )
    finally:
        try:
            _close_short_lived_session(db)
        except Exception:
            pass


def _list_after_persisted_stream_events(task_id: int, after: int, limit: int) -> list[Any]:
    """Read persisted stream packets using a short-lived DB session."""
    db = SessionLocal()
    try:
        return StreamEventStore(db).list_after(task_id, after, limit)
    finally:
        try:
            _close_short_lived_session(db)
        except Exception:
            pass


def _create_interactive_chunking_config() -> Dict[str, Any]:
    """Create optimized chunking configuration for Interactive mode."""
    return {
        "group_size": 1,
        "base_ms": 0,
        "chunking_strategy": "realtime",
        "delay_enabled": False,
        "optimization_level": "maximum",
        "buffer_size": 1,
        "async_processing": True,
    }


def _create_automatic_chunking_config(use_db_stream: bool, base_ms: int) -> Dict[str, Any]:
    """Compatibility shim for legacy callers; returns interactive-style config."""
    config = _create_interactive_chunking_config()
    config["compat_mode"] = "automatic"
    return config


async def _stream_interactive_chunks(
    content: str,
    task_id: int,
    conv_id: str,
    anchor_seq: int,
    *,
    delta_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Stream chunks for Interactive mode without artificial delays."""
    config = _create_interactive_chunking_config()
    logger.debug("[SSE] Interactive streaming: %s chars (config=%s)", len(content), config)

    try:
        from backend.services.metrics import metrics

        metrics.inc("interactive_streaming_chunks")
        metrics.inc("interactive_streaming_chars", len(content))
        metrics.inc("interactive_realtime_chunks")
    except Exception:
        pass

    for i in range(0, len(content), config["group_size"]):
        piece = content[i : i + config["group_size"]]
        if not piece:
            continue
        delta = {
            "id": (delta_id or f"stream-{task_id}-{conv_id}"),
            "object": "chat.completion.chunk",
            "taskId": task_id,
            "sequence": anchor_seq,
            "choices": [{"delta": {"content": piece}}],
        }
        yield f"id: {anchor_seq}\n"
        yield f"data: {json.dumps(delta)}\n\n"


async def _stream_chunks_with_config(
    content: str,
    task_id: int,
    conv_id: str,
    anchor_seq: int,
    config: Dict[str, Any],
    *,
    delta_id: Optional[str] = None,
    ind: Optional[int] = None,
    step_type: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Stream OpenAI-style chunks while preserving frontend grouping metadata."""
    logger.debug("[SSE] Streaming with config: %s", config)
    _ind = ind if isinstance(ind, int) else ANSWER_PHASE_INDEX
    _step_type = step_type if isinstance(step_type, str) else STEP_MESSAGE_DELTA

    try:
        from backend.services.metrics import metrics

        metrics.inc("interactive_streaming_chunks")
        metrics.inc("interactive_streaming_chars", len(content))
        metrics.inc("interactive_realtime_chunks")
    except Exception:
        pass

    async for chunk in _stream_optimized_realtime(
        content,
        task_id,
        conv_id,
        anchor_seq,
        config,
        delta_id=delta_id,
        ind=_ind,
        step_type=_step_type,
    ):
        yield chunk


def _build_chunk_metadata(conv_id: str, ind: int, step_type: str) -> Dict[str, Any]:
    """Build metadata for OpenAI-style chunks."""
    return {
        "ind": ind,
        "step_type": step_type,
        "conversation_id": conv_id,
        "conversationId": conv_id,
        "streaming": True,
    }


async def _stream_optimized_realtime(
    content: str,
    task_id: int,
    conv_id: str,
    anchor_seq: int,
    config: Dict[str, Any],
    *,
    delta_id: Optional[str] = None,
    ind: int = ANSWER_PHASE_INDEX,
    step_type: str = STEP_MESSAGE_DELTA,
) -> AsyncGenerator[str, None]:
    """Optimized real-time chunk streaming with metadata preservation."""
    import time

    start_time = time.time()
    delta_template = {
        "id": (delta_id or f"stream-{task_id}-{conv_id}"),
        "object": "chat.completion.chunk",
        "taskId": task_id,
        "sequence": anchor_seq,
        "metadata": _build_chunk_metadata(conv_id, ind, step_type),
        "choices": [{"delta": {"content": ""}}],
    }

    chunk_count = 0
    for i in range(0, len(content), config["group_size"]):
        piece = content[i : i + config["group_size"]]
        if not piece:
            continue

        delta = delta_template.copy()
        delta["choices"] = [{"delta": {"content": piece}}]

        yield f"id: {anchor_seq}\n"
        yield f"data: {json.dumps(delta)}\n\n"
        chunk_count += 1

    processing_time = time.time() - start_time
    try:
        from backend.services.metrics import metrics

        metrics.inc("interactive_realtime_chunks", chunk_count)
        metrics.gauge("interactive_streaming_latency_ms", processing_time * 1000)
        metrics.gauge(
            "interactive_chunks_per_second",
            chunk_count / processing_time if processing_time > 0 else 0,
        )
        logger.debug(
            "[SSE] Interactive streaming performance: %s chunks in %.3fs (%.1f chunks/s)",
            chunk_count,
            processing_time,
            chunk_count / processing_time if processing_time > 0 else 0,
        )
    except Exception:
        pass


async def _stream_standard_with_delays(
    content: str,
    task_id: int,
    conv_id: str,
    anchor_seq: int,
    config: Dict[str, Any],
    *,
    delta_id: Optional[str] = None,
    ind: int = ANSWER_PHASE_INDEX,
    step_type: str = STEP_MESSAGE_DELTA,
) -> AsyncGenerator[str, None]:
    """Compatibility shim that delegates to optimized realtime streaming."""
    async for chunk in _stream_optimized_realtime(
        content,
        task_id,
        conv_id,
        anchor_seq,
        config,
        delta_id=delta_id,
        ind=ind,
        step_type=step_type,
    ):
        yield chunk


def _get_user_from_request(request: Request, db: Session) -> tuple[User, Dict[str, Any]]:
    """Resolve `(user, token_payload)` from the Authorization bearer header."""
    auth = request.headers.get("Authorization")
    token = None
    if auth and auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    payload, error_code = verify_token_with_error(token)
    if payload is None:
        detail = "Token has expired" if error_code == "token_expired" else "Invalid token"
        raise HTTPException(status_code=401, detail=detail)
    try:
        user = resolve_user_from_token_payload(db, payload)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            raise
        raise HTTPException(status_code=401, detail="Invalid token")
    return user, payload


def _resolve_tenant_context_for_request(
    *,
    request: Request,
    db: Session,
    user: User,
    token_payload: Dict[str, Any],
) -> TenantRequestContext:
    """Resolve strict tenant context for reasoning HTTP routes."""
    requested_tenant_id = parse_requested_tenant_id(request.headers.get(ACTIVE_TENANT_HEADER))
    preferred_tenant_id = None
    if requested_tenant_id is None:
        preferred_tenant_id = extract_active_tenant_hint(token_payload)
    try:
        tenant_context = TenantContextService(db).resolve_for_user(
            user_id=int(user.id),
            requested_tenant_id=requested_tenant_id,
            requested_source="header" if requested_tenant_id is not None else "token_hint",
            preferred_tenant_id=preferred_tenant_id,
            allow_ambiguous=False,
        )
    except TenantContextResolutionError as exc:
        raise map_tenant_context_error(exc) from exc

    if tenant_context is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Explicit tenant selection is required for this user.",
        )
    return tenant_context


def _enforce_tenant_action(*, tenant_context: TenantRequestContext, action: str) -> None:
    """Fail closed when tenant role is not allowed for the requested reasoning action."""
    decision = decide_action(role=tenant_context.role, action=action)
    if decision.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Tenant policy denied action '{action}'.",
    )


def _authorize_task_action(
    *,
    task_id: int,
    request: Request,
    db: Session,
    action: str,
) -> tuple[User, TenantRequestContext]:
    """Authorize one reasoning route action and enforce tenant/user task ownership."""
    current_user, token_payload = _get_user_from_request(request, db)
    set_user_lookup_rls_context(
        db,
        user_id=int(current_user.id),
        actor_type="user",
    )
    tenant_context = _resolve_tenant_context_for_request(
        request=request,
        db=db,
        user=current_user,
        token_payload=token_payload,
    )
    set_tenant_rls_context(
        db,
        tenant_id=int(tenant_context.tenant_id),
        user_id=int(current_user.id),
        actor_type="user",
    )
    _enforce_tenant_action(tenant_context=tenant_context, action=action)
    get_owned_task_or_404(
        db=db,
        task_id=task_id,
        user_id=int(current_user.id),
        tenant_id=int(tenant_context.tenant_id),
    )
    return current_user, tenant_context


@router.get("/tasks/{task_id}/reasoning/stream")
async def stream_agent_reasoning(
    task_id: int,
    request: Request,
    after: Optional[int] = Query(default=0),
):
    """Stream agent reasoning logs via the SSE compatibility transport."""
    _prepare_reasoning_stream_preflight(task_id, request)
    try:
        last_event_id = request.headers.get("Last-Event-ID") or request.headers.get("last-event-id")
        if last_event_id is not None:
            candidate = str(last_event_id).strip()
            if candidate.isdigit():
                after = max(after or 0, int(candidate))
            else:
                logger.debug("[SSE] ignoring non-numeric Last-Event-ID=%s", candidate)
    except Exception:
        pass

    logger.info("[SSE] connect task_id=%s after=%s", task_id, after)
    try:
        from backend.services.metrics import metrics

        metrics.inc("sse_connects")
        from backend.services.streaming.db_stream_service import get_db_stream_service

        try:
            svc = get_db_stream_service()
            service_metrics = svc.get_metrics()
            metrics.gauge("db_stream_active_tasks", service_metrics.get("active_tasks", 0))
            metrics.gauge("db_stream_polling_tasks", service_metrics.get("polling_tasks", 0))
        except Exception:
            pass
    except Exception:
        pass

    return StreamingResponse(
        _reasoning_sse_service.generate(
            task_id,
            after=after or 0,
            persisted_list_after=_list_after_persisted_stream_events,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/tasks/{task_id}/reasoning/history")
async def get_reasoning_history(
    task_id: int,
    after: Optional[int] = Query(default=None),
    before: Optional[int] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Return reasoning history from stream DB, legacy DB, or file fallback."""
    if after is not None and before is not None:
        raise HTTPException(status_code=400, detail="Use either 'after' or 'before', not both")

    _authorize_task_action(
        task_id=task_id,
        request=request,
        db=db,
        action=ACTION_STREAM_REPLAY,
    )
    history_service = AgentReasoningHistoryService(db)
    return history_service.get_history(
        task_id,
        after=after,
        before=before,
        limit=limit,
        order=order,
    )


@router.get("/tasks/{task_id}/reasoning/replay")
async def get_reasoning_replay(
    task_id: int,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    request: Request = None,
):
    """Return unfiltered persisted stream packets for cursor-based replay."""
    _authorize_task_action(
        task_id=task_id,
        request=request,
        db=db,
        action=ACTION_STREAM_REPLAY,
    )
    return AgentReasoningHistoryService(db).get_replay_history(
        task_id,
        after=after,
        limit=limit,
    )


@router.post("/tasks/{task_id}/send-message", status_code=status.HTTP_201_CREATED)
async def send_user_message(
    task_id: int,
    message: UserMessage,
    request: Request,
    db: Session = Depends(get_db),
):
    """Persist a user message for history and notify the agent container if possible."""
    current_user, _tenant_context = _authorize_task_action(
        task_id=task_id,
        request=request,
        db=db,
        action=ACTION_CHAT_WRITE,
    )

    try:
        from agent.chat import ConversationManager
        from backend.services.chat.turn_orchestrator import ChatTurnOrchestrator

        conversation_id = ConversationManager(task_id).ensure_default_conversation()
        user_message_id, _turn_number = ChatTurnOrchestrator(db).reserve_user_message(
            task_id=task_id,
            conversation_id=conversation_id,
            user_message=message.message,
        )
        logger.info(
            "User message persisted to ChatMessage: task_id=%s message_id=%s",
            task_id,
            user_message_id,
        )
    except Exception as exc:
        db.rollback()
        logger.error("Failed to persist ChatMessage for task %s: %s", task_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to persist user message: {exc}")

    result = await _runtime_input_service.append_and_signal(
        task_id,
        message=message.message,
        strict_persistence=False,
        user_id=current_user.id,
    )
    if not result.signal_sent:
        return {"success": True, "signal_sent": False, "detail": result.detail}
    return {"success": True, "signal_sent": True}


__all__ = [
    "PersistedListAfter",
    "UserMessage",
    "_build_chunk_metadata",
    "_create_automatic_chunking_config",
    "_create_interactive_chunking_config",
    "_list_after_persisted_stream_events",
    "_prepare_reasoning_stream_preflight",
    "_reasoning_sse_service",
    "_runtime_input_service",
    "_stream_chunks_with_config",
    "_stream_interactive_chunks",
    "_stream_optimized_realtime",
    "_stream_standard_with_delays",
    "get_reasoning_history",
    "get_reasoning_replay",
    "router",
    "send_user_message",
    "stream_agent_reasoning",
]
