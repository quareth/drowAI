"""Chat router package with compatibility exports for the split router modules."""

from __future__ import annotations

from typing import Any

__all__ = [
    "CHAT_HISTORY_CONTRACT_VERSION",
    "MAX_MESSAGE_LEN",
    "ChatCancelRequest",
    "ChatContextWindowResponse",
    "ChatHistoryResponse",
    "ChatHistoryStartupPayload",
    "ChatPrewarmResponse",
    "ChatReadyResponse",
    "ChatRequest",
    "ChatTranscriptItem",
    "ChatTurnOrchestrator",
    "ConversationHistoryReader",
    "ConversationManager",
    "_build_chat_history_response",
    "_build_chat_startup_payload",
    "_build_conversation_history",
    "_build_run_payload",
    "_derive_task_running",
    "_ensure_chat_prewarm",
    "_get_runtime_warmup_status",
    "_normalize_agent_and_plan_mode",
    "_parse_agent_mode",
    "_parse_requested_mode",
    "_reserve_chat_turn",
    "_schedule_background_task",
    "_submit_chat_request",
    "attach_conversation_ids",
    "build_user_message_event",
    "cancel_chat_run",
    "chat",
    "chat_ready",
    "get_current_user",
    "get_db",
    "get_tenant_request_context",
    "get_queue_status",
    "get_run_lifecycle_service",
    "get_streaming_status",
    "get_streaming_statuses",
    "get_user_openai_key",
    "get_user_openai_model",
    "get_chat_context_window",
    "get_chat_history",
    "is_supported_openai_model",
    "normalize_openai_model_identifier",
    "prewarm_chat",
    "router",
    "run_langgraph_generation",
    "validate_reasoning_effort_for_model",
]


def _prime_auth_imports() -> None:
    """Load model exports before auth-dependent route modules on cold imports."""
    import backend.models  # noqa: F401


def __getattr__(name: str) -> Any:
    if name in {
        "MAX_MESSAGE_LEN",
        "CHAT_HISTORY_CONTRACT_VERSION",
        "ChatRequest",
        "ChatPrewarmResponse",
        "ChatCancelRequest",
        "ChatReadyResponse",
        "ChatHistoryStartupPayload",
        "ChatTranscriptItem",
        "ChatHistoryResponse",
        "ChatContextWindowResponse",
        "_build_chat_history_response",
    }:
        from . import schemas as _schemas

        return getattr(_schemas, name)
    if name in {
        "_derive_task_running",
        "_ensure_chat_prewarm",
        "_get_runtime_warmup_status",
        "_build_chat_startup_payload",
    }:
        from . import readiness as _readiness

        return getattr(_readiness, name)
    if name in {"prewarm_chat", "chat_ready"}:
        _prime_auth_imports()
        from . import prewarm_ready as _prewarm_ready

        return getattr(_prewarm_ready, name)
    if name in {"get_chat_history", "get_chat_context_window"}:
        _prime_auth_imports()
        from . import history as _history

        return getattr(_history, name)
    if name in {
        "_schedule_background_task",
        "_parse_requested_mode",
        "_parse_agent_mode",
        "_normalize_agent_and_plan_mode",
        "_build_conversation_history",
        "_reserve_chat_turn",
        "_submit_chat_request",
        "chat",
    }:
        _prime_auth_imports()
        from . import submit as _submit

        return getattr(_submit, name)
    if name in {"cancel_chat_run"}:
        _prime_auth_imports()
        from . import cancel as _cancel

        return getattr(_cancel, name)
    if name in {
        "_build_run_payload",
        "get_streaming_status",
        "get_streaming_statuses",
        "get_queue_status",
    }:
        _prime_auth_imports()
        from . import status as _status

        return getattr(_status, name)
    if name == "router":
        _prime_auth_imports()
        from .router_bundle import router

        return router
    if name == "get_current_user":
        _prime_auth_imports()
        from ...auth import get_current_user

        return get_current_user
    if name == "get_db":
        from ...database import get_db

        return get_db
    if name == "get_tenant_request_context":
        from ...services.tenant.dependencies import get_tenant_request_context

        return get_tenant_request_context
    if name == "ConversationManager":
        from agent.chat import ConversationManager

        return ConversationManager
    if name == "ChatTurnOrchestrator":
        from ...services.chat.turn_orchestrator import ChatTurnOrchestrator

        return ChatTurnOrchestrator
    if name == "ConversationHistoryReader":
        from ...services.chat.conversation_history_reader import ConversationHistoryReader

        return ConversationHistoryReader
    if name in {
        "get_user_openai_key",
        "get_user_openai_model",
        "is_supported_openai_model",
        "normalize_openai_model_identifier",
    }:
        from ..settings import (
            get_user_openai_key,
            get_user_openai_model,
            is_supported_openai_model,
            normalize_openai_model_identifier,
        )

        return {
            "get_user_openai_key": get_user_openai_key,
            "get_user_openai_model": get_user_openai_model,
            "is_supported_openai_model": is_supported_openai_model,
            "normalize_openai_model_identifier": normalize_openai_model_identifier,
        }[name]
    if name == "validate_reasoning_effort_for_model":
        from ...services.langgraph_chat.model_role_registry import validate_reasoning_effort_for_model

        return validate_reasoning_effort_for_model
    if name == "get_run_lifecycle_service":
        from ...services.langgraph_chat.runtime.run_lifecycle import get_run_lifecycle_service

        return get_run_lifecycle_service
    if name == "run_langgraph_generation":
        from ...services.langgraph_chat.execution.turn_service import run_langgraph_generation

        return run_langgraph_generation
    if name in {"attach_conversation_ids", "build_user_message_event"}:
        from ...services.chat.event_builders import attach_conversation_ids, build_user_message_event

        return {
            "attach_conversation_ids": attach_conversation_ids,
            "build_user_message_event": build_user_message_event,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
