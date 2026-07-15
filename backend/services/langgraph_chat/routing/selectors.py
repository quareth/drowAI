"""Helpers that determine which branch of the LangGraph facade to execute."""

from __future__ import annotations

from enum import Enum
import logging

from backend.services.langgraph_chat.contracts import (
    ExecutionMode,
    LangGraphRuntimeConfig,
)


class ChatBranch(str, Enum):
    """High-level branch identifiers for the facade."""

    NORMAL_CHAT = "normal_chat"
    DEEP_REASONING = "deep_reasoning"
    SIMPLE_TOOL = "simple_tool_execution"


def select_branch(config: LangGraphRuntimeConfig) -> ChatBranch:
    """Select the branch that should handle the turn."""

    mapping = {
        ExecutionMode.NORMAL_CHAT: ChatBranch.NORMAL_CHAT,
        ExecutionMode.DEEP_REASONING: ChatBranch.DEEP_REASONING,
        ExecutionMode.SIMPLE_TOOL: ChatBranch.SIMPLE_TOOL,
    }
    return mapping.get(config.execution_mode, ChatBranch.NORMAL_CHAT)


def resolve_branch(
    runtime_config: LangGraphRuntimeConfig,
    *,
    deep_reasoning_enabled: bool,
    simple_tool_enabled: bool,
) -> ChatBranch:
    """Resolve which branch handles this turn.

    Args:
        runtime_config: Runtime config for the current chat turn.
        deep_reasoning_enabled: Whether the deep-reasoning handler is enabled.
        simple_tool_enabled: Whether the simple-tool handler is enabled.

    Returns:
        The chat branch that should execute the turn.
    """
    logger = logging.getLogger("backend.services.langgraph_chat.facade")
    deterministic_mode = bool(runtime_config.metadata.get("deterministic_mode"))
    if deterministic_mode:
        requested_mode = runtime_config.chat_inputs.requested_mode
        if requested_mode == ExecutionMode.DEEP_REASONING:
            branch = ChatBranch.DEEP_REASONING
        elif requested_mode == ExecutionMode.SIMPLE_TOOL:
            branch = ChatBranch.SIMPLE_TOOL
        else:
            # Deterministic tests default to tool-path scenario if caller omits mode.
            branch = ChatBranch.SIMPLE_TOOL
    else:
        branch = select_branch(runtime_config)

    if branch is ChatBranch.DEEP_REASONING and not deep_reasoning_enabled:
        logger.warning("Deep reasoning disabled, falling back to normal chat")
        branch = ChatBranch.NORMAL_CHAT
    if branch is ChatBranch.SIMPLE_TOOL and not simple_tool_enabled:
        logger.warning("Simple tool disabled, falling back to normal chat")
        branch = ChatBranch.NORMAL_CHAT

    return branch


__all__ = ["ChatBranch", "resolve_branch", "select_branch"]
