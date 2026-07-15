"""LangGraph chat facade package.

This module exposes the public entry points required by the backend router.
Heavy subsystems (checkpointer, executor, facade) load on first attribute access so
imports like ``langgraph_chat.checkpoint.hitl_schemas`` do not bootstrap the full LangGraph graph.
"""

from __future__ import annotations

from typing import Any

from .contracts import (
    AgentMode,
    ChatInputs,
    ExecutionMode,
    LangGraphChatResult,
    LangGraphRuntimeConfig,
    PersistenceContext,
    ToolingContext,
)

__all__ = [
    "CheckpointerService",
    "ContextWindowManager",
    "ContextWindowDecision",
    "ContextWindowSnapshot",
    "AgentMode",
    "ChatInputs",
    "ExecutionMode",
    "LangGraphChatResult",
    "LangGraphRuntimeConfig",
    "LangGraphChatFacade",
    "LangGraphExecutor",
    "IntentClassifier",
    "PersistenceContext",
    "resolve_context_window_max_tokens",
    "ToolingContext",
]


def __getattr__(name: str) -> Any:
    if name == "CheckpointerService":
        from .checkpoint.checkpointer_service import CheckpointerService

        return CheckpointerService
    if name == "ContextWindowManager":
        from .compression.window_manager import ContextWindowManager

        return ContextWindowManager
    if name == "resolve_context_window_max_tokens":
        from .compression.window_manager import resolve_context_window_max_tokens

        return resolve_context_window_max_tokens
    if name == "ContextWindowDecision":
        from .compression.window_models import ContextWindowDecision

        return ContextWindowDecision
    if name == "ContextWindowSnapshot":
        from .compression.window_models import ContextWindowSnapshot

        return ContextWindowSnapshot
    if name == "LangGraphExecutor":
        from backend.services.langgraph_chat.execution.graph_executor import LangGraphExecutor

        return LangGraphExecutor
    if name == "IntentClassifier":
        from backend.services.langgraph_chat.intent.classifier import IntentClassifier

        return IntentClassifier
    if name == "LangGraphChatFacade":
        from .facade import LangGraphChatFacade

        return LangGraphChatFacade
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
