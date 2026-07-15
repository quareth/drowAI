"""History and context-window endpoints for chat conversations."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...database import get_db
from ...models.core import User
from ...models.hitl import TurnWorkflow
from ...services.chat.transcript_query_service import ChatTranscriptQueryService
from ...services.chat.conversation_history_reader import ConversationHistoryReader
from ...services.langgraph_chat.compression.window_manager import (
    ContextWindowManager,
    resolve_context_window_max_tokens,
)
from ...services.langgraph_chat.compression.window_models import (
    ContextWindowSnapshot,
    parse_persisted_measured_snapshot,
)
from ...services.llm_provider.runtime_config_service import LLMRuntimeConfigService
from ...services.tenant.authorization import ACTION_CHAT_READ
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from ..tasks.deps import enforce_tenant_action, get_tenant_task_or_404
from .readiness import _build_chat_startup_payload, _derive_task_running
from .schemas import (
    ChatContextWindowResponse,
    ChatHistoryResponse,
    ChatHistoryStartupPayload,
    _build_chat_history_response,
)

router = APIRouter()
logger = logging.getLogger(__name__)
CHAT_HISTORY_PAGE_LIMIT = 50
CHAT_HISTORY_MAX_LIMIT = 200
CHAT_HISTORY_STARTUP_LIMIT = 200
CONTEXT_WINDOW_WORKFLOW_BATCH_SIZE = 50

try:
    from backend.services.langgraph_chat.diagnostic_logger import get_diagnostic_logger

    _diag_logger = get_diagnostic_logger()
except Exception:  # pragma: no cover - diagnostics unavailable
    _diag_logger = None


def _diag_info(message: str, *args: object) -> None:
    if _diag_logger is not None:
        _diag_logger.info(message, *args)


def _compat():
    import backend.routers.chat as chat_package

    return chat_package


def _latest_measured_context_window_snapshot(
    db: Session,
    *,
    task_id: int,
    conversation_id: str,
) -> Optional[ContextWindowSnapshot]:
    """Return the newest canonical measured snapshot without loading every row."""

    workflow_metadata_rows = (
        db.query(TurnWorkflow.workflow_metadata)
        .filter(
            TurnWorkflow.task_id == task_id,
            TurnWorkflow.conversation_id == conversation_id,
            TurnWorkflow.turn_sequence.isnot(None),
        )
        .order_by(TurnWorkflow.turn_sequence.desc(), TurnWorkflow.id.desc())
        .yield_per(CONTEXT_WINDOW_WORKFLOW_BATCH_SIZE)
    )
    for (workflow_metadata,) in workflow_metadata_rows:
        snapshot = parse_persisted_measured_snapshot(
            workflow_metadata,
            task_id=task_id,
            fallback_conversation_id=conversation_id,
        )
        if snapshot is not None:
            return snapshot
    return None


@router.get("/tasks/{task_id}/chat/history", response_model=ChatHistoryResponse)
async def get_chat_history(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
    conversation_id: Optional[str] = Query(None, description="Conversation ID; default used if omitted"),
    before_turn: Optional[int] = Query(None, description="Turn cursor for older transcript page"),
    limit: Optional[int] = Query(None, ge=1, le=CHAT_HISTORY_MAX_LIMIT, description="Page size"),
    initial: bool = Query(
        False,
        description=(
            "When true, returns startup readiness metadata plus the latest transcript page; "
            "`before_turn` is disallowed."
        ),
    ),
):
    """Return compact transcript history for the task conversation."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_READ)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    if initial and before_turn is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`before_turn` is not allowed when `initial=true`.",
        )

    query_service = ChatTranscriptQueryService(db)
    resolved_existing_conversation_id = query_service.resolve_existing_conversation_id(
        task_id=task_id,
        requested_conversation_id=conversation_id,
    )

    startup: Optional[ChatHistoryStartupPayload] = None
    if initial:
        active_run = _compat().get_run_lifecycle_service().get_active_run(
            task_id,
            db_session=db,
        )
        task_running = _derive_task_running(task.status, active_run)
        # Release the request transaction before awaiting runtime work. A fresh
        # transaction is opened lazily below for the transcript page query.
        db.rollback()
        startup = await _build_chat_startup_payload(
            task_id=task_id,
            task_running=task_running,
            requested_conversation_id=resolved_existing_conversation_id,
        )

    conv_id = query_service.resolve_conversation_id(
        task_id=task_id,
        requested_conversation_id=(
            conversation_id
            or resolved_existing_conversation_id
            or (startup.conversation_id if startup else None)
        ),
    )

    effective_limit = (
        CHAT_HISTORY_STARTUP_LIMIT
        if initial and limit is None
        else (limit if limit is not None else CHAT_HISTORY_PAGE_LIMIT)
    )
    logger.info(
        "[CHAT_HISTORY] task=%s conv=%s before_turn=%s limit=%s initial=%s",
        task_id,
        conv_id,
        before_turn,
        effective_limit,
        initial,
    )
    _diag_info(
        "CHAT_HISTORY | request | task=%s conv=%s before_turn=%s limit=%s initial=%s",
        task_id,
        conv_id,
        before_turn,
        effective_limit,
        initial,
    )
    if before_turn is not None:
        before_cursor = query_service.resolve_before_cursor(
            task_id=task_id,
            conversation_id=conv_id,
            before_turn_number=before_turn,
        )
        if before_cursor is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="`before_turn` cursor is invalid for this conversation.",
            )
        page = query_service.list_older_transcript_page(
            task_id=task_id,
            requested_conversation_id=conv_id,
            before=before_cursor,
            limit=effective_limit,
        )
    else:
        page = query_service.list_latest_transcript_page(
            task_id=task_id,
            requested_conversation_id=conv_id,
            limit=effective_limit,
        )
    logger.info(
        "[CHAT_HISTORY] transcript items=%s has_more_older=%s task=%s conv=%s initial=%s",
        len(page.items),
        page.has_more_older,
        task_id,
        conv_id,
        initial,
    )
    _diag_info(
        "CHAT_HISTORY | transcript | items=%s has_more_older=%s task=%s conv=%s initial=%s",
        len(page.items),
        page.has_more_older,
        task_id,
        conv_id,
        initial,
    )
    next_before_turn = page.next_before.turn_number if page.next_before is not None else None
    return _build_chat_history_response(
        items=page.items,
        has_more_older=page.has_more_older,
        next_before_turn=next_before_turn,
        startup=startup,
    )


@router.get("/tasks/{task_id}/chat/context-window", response_model=ChatContextWindowResponse)
async def get_chat_context_window(
    task_id: int,
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
    conversation_id: Optional[str] = Query(None, description="Conversation ID; default used if omitted"),
):
    """Return chat-scoped context occupancy snapshot for one conversation."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_READ)
    get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    query_service = ChatTranscriptQueryService(db)
    conv_id = query_service.resolve_conversation_id(
        task_id=task_id,
        requested_conversation_id=conversation_id,
    )
    if not conv_id:
        conv_id = "default"

    snapshot = _latest_measured_context_window_snapshot(
        db,
        task_id=task_id,
        conversation_id=conv_id,
    )
    if snapshot is None:
        history = ConversationHistoryReader(db).build_openai_conversation_history(
            task_id=task_id,
            conversation_id=conv_id,
        )
        runtime_selection = LLMRuntimeConfigService(db).build_runtime_selection(
            user_id=current_user.id,
            require_enabled_credential=False,
        )
        decision = ContextWindowManager(
            max_tokens=resolve_context_window_max_tokens(
                provider=runtime_selection.provider,
                model=runtime_selection.model,
            )
        ).evaluate_history(
            task_id=task_id,
            conversation_id=conv_id,
            history=history,
            provider=runtime_selection.provider,
            model=runtime_selection.model,
        )
        evaluated = decision.snapshot
        snapshot = ContextWindowSnapshot(
            task_id=evaluated.task_id,
            conversation_id=evaluated.conversation_id,
            max_tokens=evaluated.max_tokens,
            used_tokens=evaluated.used_tokens,
            remaining_tokens=evaluated.remaining_tokens,
            ratio=evaluated.ratio,
            ceiling_reached=evaluated.ceiling_reached,
            recommended_next_action=decision.recommended_next_action,
            compression_candidate=decision.compression_candidate,
        )
    return ChatContextWindowResponse(
        task_id=snapshot.task_id,
        conversation_id=snapshot.conversation_id,
        max_tokens=snapshot.max_tokens,
        used_tokens=snapshot.used_tokens,
        remaining_tokens=snapshot.remaining_tokens,
        ratio=snapshot.ratio,
        ceiling_reached=snapshot.ceiling_reached,
        recommended_next_action=snapshot.recommended_next_action,
        compression_candidate=snapshot.compression_candidate,
        turn_sequence=snapshot.turn_sequence,
        revision=snapshot.revision,
        snapshot_kind=snapshot.snapshot_kind,
    )


__all__ = [
    "CHAT_HISTORY_MAX_LIMIT",
    "CHAT_HISTORY_PAGE_LIMIT",
    "CHAT_HISTORY_STARTUP_LIMIT",
    "_diag_info",
    "get_chat_context_window",
    "get_chat_history",
    "router",
]
