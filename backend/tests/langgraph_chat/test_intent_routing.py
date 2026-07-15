"""Unit tests for LangGraph facade branch selection helpers."""

from __future__ import annotations

from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.routing.selectors import ChatBranch, select_branch


def _runtime_config(mode: ExecutionMode) -> LangGraphRuntimeConfig:
    return LangGraphRuntimeConfig(
        chat_inputs=ChatInputs(
            task_id=1,
            user_id=1,
            message="test",
            conversation_id="conv-1",
            history=[],
        ),
        execution_mode=mode,
        metadata={},
    )


def test_select_branch_normal_chat() -> None:
    assert select_branch(_runtime_config(ExecutionMode.NORMAL_CHAT)) is ChatBranch.NORMAL_CHAT


def test_select_branch_deep_reasoning() -> None:
    assert select_branch(_runtime_config(ExecutionMode.DEEP_REASONING)) is ChatBranch.DEEP_REASONING


def test_select_branch_simple_tool() -> None:
    assert select_branch(_runtime_config(ExecutionMode.SIMPLE_TOOL)) is ChatBranch.SIMPLE_TOOL

