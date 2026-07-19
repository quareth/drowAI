"""Chat submission endpoint and submission orchestration helpers."""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...auth import get_current_user
from ...config import E2E_DETERMINISTIC_MODE
from ...core.rate_limiter import rate_limit
from ...database import get_db
from ...models.core import Task, User
from ...services.chat.event_builders import attach_conversation_ids, build_user_message_event
from ...services.chat.turn_orchestrator import ChatTurnOrchestrator
from ...services.chat.conversation_history_reader import ConversationHistoryReader
from ...services.langgraph_chat.contracts import AgentMode, ExecutionMode
from ...services.llm_provider import (
    CredentialNotFoundError,
    LLMProviderServiceError,
    LLMRuntimeConfigService,
    ProviderConfigurationError,
)
from ...services.llm_provider.types import DeploymentRef
from ...services.tenant.authorization import ACTION_CHAT_WRITE
from ...services.tenant.context import TenantRequestContext
from ...services.tenant.dependencies import get_tenant_request_context
from ..tasks.deps import enforce_tenant_action, get_tenant_task_or_404
from .schemas import ChatRequest, MAX_MESSAGE_LEN

router = APIRouter()
logger = logging.getLogger(__name__)


def _compat():
    import backend.routers.chat as chat_package

    return chat_package


def _schedule_background_task(coro: Any) -> asyncio.Task[Any]:
    """Create a background task via a patchable seam for tests."""
    return asyncio.create_task(coro)


def _parse_requested_mode(raw_mode: Optional[str]) -> Optional[ExecutionMode]:
    """Map incoming mode strings to ExecutionMode values."""
    from ...services.langgraph_chat.routing.mode_policy import parse_execution_mode

    return parse_execution_mode(raw_mode)


def _parse_agent_mode(raw_mode: Optional[str]) -> Optional[AgentMode]:
    """Map incoming agent mode strings to AgentMode values."""
    from ...services.langgraph_chat.routing.mode_policy import parse_agent_mode

    return parse_agent_mode(raw_mode)


def _normalize_agent_and_plan_mode(
    *,
    agent_mode: Optional[AgentMode],
    plan_mode: Optional[bool],
) -> tuple[Optional[AgentMode], bool]:
    """Normalize the request-boundary agent/plan mode pair with HTTP semantics."""
    from ...services.langgraph_chat.routing.mode_policy import ModePolicyError, normalize_agent_plan_pair

    try:
        return normalize_agent_plan_pair(
            agent_mode=agent_mode,
            plan_mode=plan_mode,
        )
    except ModePolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


def _deterministic_requested_mode(
    *,
    plan_mode: bool,
) -> Optional[ExecutionMode]:
    """Select an offline scenario branch only in server-owned E2E mode."""
    if not E2E_DETERMINISTIC_MODE:
        return None
    return ExecutionMode.DEEP_REASONING if plan_mode else ExecutionMode.SIMPLE_TOOL


def _requested_provider_for_chat(payload: ChatRequest) -> Optional[str]:
    """Resolve the request-boundary provider override without legacy fallback."""
    provider = payload.provider.strip() if isinstance(payload.provider, str) else None
    if provider:
        return provider
    return None


def _selection_provider(selection: Any) -> str:
    """Return the compatibility provider snapshot for legacy or V2 selections."""

    return str(
        getattr(selection, "provider", None)
        or getattr(selection, "legacy_provider", None)
        or ""
    )


def _selection_model(selection: Any) -> str:
    """Return the compatibility model snapshot for legacy or V2 selections."""

    return str(
        getattr(selection, "model", None)
        or getattr(selection, "legacy_model", None)
        or ""
    )


def _selection_credential_ref_payload(selection: Any) -> dict[str, Any] | None:
    """Return legacy credential metadata when present."""

    credential_ref = getattr(selection, "credential_ref", None)
    if credential_ref is None:
        return None
    return credential_ref.to_dict()


def _build_conversation_history(
    db: Session,
    task_id: int,
    conversation_id: Optional[str],
    *,
    exclude_message_ids: Optional[set[int]] = None,
    source_message_ids_out: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """Build multi-turn history from ChatMessage only."""
    aligned_history = ConversationHistoryReader(
        db
    ).build_aligned_openai_conversation_history(
        task_id=task_id,
        conversation_id=conversation_id,
        exclude_message_ids=exclude_message_ids,
    )
    history = list(aligned_history.messages)
    if source_message_ids_out is not None:
        source_message_ids_out.extend(aligned_history.source_message_ids)
    if history:
        logger.info("Using ChatMessage store for conv %s", conversation_id)
    return history


def _reserve_chat_turn(
    db: Session,
    *,
    task_id: int,
    conversation_id: str,
    user_message: str,
) -> tuple[int, int, str, int]:
    """Reserve ChatMessage rows for user + assistant and return identifiers."""
    return ChatTurnOrchestrator(db).reserve_chat_turn_pair(
        task_id=task_id,
        conversation_id=conversation_id,
        user_message=user_message,
    )


@router.post("/tasks/{task_id}/chat", status_code=status.HTTP_202_ACCEPTED)
@rate_limit(max_calls=20, window=60)
async def chat(
    task_id: int,
    payload: ChatRequest = Body(...),
    current_user: User = Depends(get_current_user),
    tenant_context: TenantRequestContext = Depends(get_tenant_request_context),
    db: Session = Depends(get_db),
):
    """Start a basic LLM chat turn and stream output via existing SSE."""
    enforce_tenant_action(tenant_context=tenant_context, action=ACTION_CHAT_WRITE)
    task = get_tenant_task_or_404(db=db, task_id=task_id, tenant_context=tenant_context)
    return await _submit_chat_request(
        task_id=task_id,
        payload=payload,
        current_user=current_user,
        db=db,
        task=task,
    )


async def _submit_chat_request(
    *,
    task_id: int,
    payload: ChatRequest,
    current_user: User,
    db: Session,
    task: Task,
) -> Dict[str, Any]:
    """Execute chat submission orchestration for the chat route."""
    compat = _compat()
    if not payload.message or not isinstance(payload.message, str):
        raise HTTPException(status_code=400, detail="'message' is required")
    if len(payload.message) > MAX_MESSAGE_LEN:
        raise HTTPException(status_code=400, detail=f"Message exceeds {MAX_MESSAGE_LEN} characters")

    deterministic_mode = E2E_DETERMINISTIC_MODE
    runtime_config_service = LLMRuntimeConfigService(db)
    runtime_provider = _requested_provider_for_chat(payload)
    raw_requested_model = payload.model.strip() if isinstance(payload.model, str) else None
    requested_model = raw_requested_model
    deployment_ref = None
    try:
        deployment_ref = (
            DeploymentRef.from_mapping(payload.deployment_ref.model_dump())
            if payload.deployment_ref is not None
            else None
        )
        runtime_selection = runtime_config_service.build_conversation_runtime_selection(
            user_id=current_user.id,
            deployment_ref=deployment_ref,
            provider=runtime_provider,
            model=requested_model,
            reasoning_effort=payload.reasoning_effort,
            require_enabled_credential=not deterministic_mode,
        )
    except CredentialNotFoundError as exc:
        missing_provider = runtime_provider or ""
        exc_detail = str(exc)
        if not missing_provider and exc_detail.lower().startswith("openai "):
            missing_provider = OPENAI_PROVIDER_ID
        detail = (
            "OpenAI API key not configured"
            if missing_provider.strip().lower() == OPENAI_PROVIDER_ID
            else exc_detail
        )
        raise HTTPException(status_code=400, detail=detail) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except ProviderConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except LLMProviderServiceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    selection_provider = _selection_provider(runtime_selection)
    model = _selection_model(runtime_selection)
    if selection_provider == OPENAI_PROVIDER_ID:
        if (
            deployment_ref is None
            and requested_model
            and not compat.is_supported_openai_model(requested_model)
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid model. Only GPT-5 family models are supported.",
            )
        if requested_model:
            model = _selection_model(runtime_selection)
        try:
            normalized_reasoning_effort = compat.validate_reasoning_effort_for_model(
                effort=payload.reasoning_effort,
                model=model,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
    else:
        try:
            normalized_reasoning_effort = compat.validate_reasoning_effort_for_model(
                effort=payload.reasoning_effort,
                provider=selection_provider,
                model=model,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
    runtime_selection_payload = runtime_selection.to_dict()
    runtime_selection_payload["reasoning_effort"] = normalized_reasoning_effort

    conv_id = payload.conversation_id
    if not conv_id:
        conv_id = compat.ConversationManager(task_id).ensure_default_conversation()
    client_message_id = payload.client_message_id.strip() if isinstance(payload.client_message_id, str) else None

    history_source_message_ids: List[int] = []
    history = compat._build_conversation_history(
        db,
        task_id,
        conv_id,
        source_message_ids_out=history_source_message_ids,
    )

    try:
        user_message_id, assistant_message_id, turn_id, turn_number = compat._reserve_chat_turn(
            db,
            task_id=task_id,
            conversation_id=conv_id,
            user_message=payload.message,
        )
    except Exception as reserve_exc:
        logger.error(
            "Failed to reserve ChatMessage for task %s: %s",
            task_id,
            reserve_exc,
            exc_info=True,
        )
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to reserve chat messages") from reserve_exc

    user_sequence = user_message_id
    anchor_sequence = assistant_message_id
    reserved_message_id = assistant_message_id
    turn_sequence = turn_number

    hub = None
    try:
        from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub

        hub = get_in_memory_stream_hub()
    except Exception:
        logger.debug("In-memory stream hub unavailable for task %s", task_id, exc_info=True)

    if hub is not None:
        try:
            event_meta = {"client_message_id": client_message_id} if client_message_id else {}
            event_meta = attach_conversation_ids(event_meta, conv_id)
            event_meta.setdefault("id", turn_id)
            event_meta.setdefault("ind", -1)
            event_meta.setdefault("turn_sequence", turn_sequence)
            user_event = build_user_message_event(payload.message, conv_id, event_meta)
            user_event["sequence"] = user_sequence
            user_event["metadata"]["sequence"] = user_sequence
            user_event["metadata"]["turn_sequence"] = turn_sequence
            compat._schedule_background_task(hub.publish(task_id, user_event))
        except Exception:
            logger.debug("Failed to publish user event for task %s", task_id, exc_info=True)

    requested_mode: Optional[ExecutionMode] = None
    if isinstance(payload.mode, str) and payload.mode.strip():
        logger.info(
            "Ignoring client-supplied chat mode for task %s (mode=%s); routing is backend-authoritative",
            task_id,
            payload.mode.strip(),
        )
    agent_mode = compat._parse_agent_mode(payload.agent_mode)
    agent_mode, plan_mode = compat._normalize_agent_and_plan_mode(
        agent_mode=agent_mode,
        plan_mode=payload.plan_mode,
    )
    requested_mode = _deterministic_requested_mode(
        plan_mode=plan_mode,
    )

    if hub is not None:
        try:
            is_streaming = hub.is_task_streaming(task_id)
            queued_count = hub.get_queued_count(task_id)
            logger.info("Task %s streaming state: %s, queued: %s", task_id, is_streaming, queued_count)
            if is_streaming or queued_count > 0:
                logger.info("Queuing message for task %s: %s...", task_id, payload.message[:50])
                hub.queue_message(
                    task_id,
                    payload.message,
                    conv_id,
                    current_user.id,
                    client_message_id=client_message_id,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    turn_id=turn_id,
                    turn_number=turn_number,
                    user_sequence=user_sequence,
                    anchor_sequence=anchor_sequence,
                    user_event_published=True,
                    requested_mode=requested_mode,
                    provider=selection_provider,
                    model=model,
                    credential_ref=_selection_credential_ref_payload(runtime_selection),
                    runtime_selection=runtime_selection_payload,
                    reasoning_effort=normalized_reasoning_effort,
                    deterministic_mode=deterministic_mode,
                    agent_mode=agent_mode,
                    plan_mode=plan_mode,
                )
                return {
                    "success": True,
                    "conversation_id": conv_id,
                    "turn_id": turn_id,
                    "queued": True,
                }
        except Exception as exc:
            logger.warning("Failed to check streaming state for task %s: %s", task_id, exc)

    compat._schedule_background_task(
        compat.run_langgraph_generation(
            task_id=task_id,
            user_id=current_user.id,
            tenant_id=task.tenant_id,
            provider=selection_provider,
            model=model,
            runtime_selection=runtime_selection_payload,
            message=payload.message,
            conversation_id=conv_id,
            history=history,
            history_source_message_ids=history_source_message_ids,
            anchor_sequence=anchor_sequence,
            requested_mode=requested_mode,
            agent_mode=agent_mode,
            plan_mode=plan_mode,
            turn_id=turn_id,
            turn_number=turn_number,
            reserved_message_id=reserved_message_id,
            reasoning_effort=normalized_reasoning_effort,
            deterministic_mode=deterministic_mode,
        )
    )

    return {"success": True, "conversation_id": conv_id, "turn_id": turn_id}


__all__ = [
    "_build_conversation_history",
    "_deterministic_requested_mode",
    "_normalize_agent_and_plan_mode",
    "_parse_agent_mode",
    "_parse_requested_mode",
    "_reserve_chat_turn",
    "_schedule_background_task",
    "_submit_chat_request",
    "chat",
    "router",
]
