"""Regression tests for HITL resume persistence through the facade."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

try:
    import langgraph  # noqa: F401
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not LANGGRAPH_AVAILABLE,
    reason="langgraph not installed",
)

GRAPH_THREAD_ID = "a" * 32


class DummyCheckpointerContext:
    def __init__(self, checkpointer):
        self._checkpointer = checkpointer

    async def __aenter__(self):
        return self._checkpointer

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummyCheckpointerService:
    def __init__(self, checkpointer):
        self._checkpointer = checkpointer

    def get_checkpointer(self, task_id):
        return DummyCheckpointerContext(self._checkpointer)


@pytest.mark.asyncio
async def test_resume_completion_uses_shared_container_persistence() -> None:
    from agent.graph import InteractiveInput, build_initial_state
    from backend.services.langgraph_chat.facade import LangGraphChatFacade
    from backend.services.langgraph_chat.execution.graph_executor import GraphExecutionResult

    payload = InteractiveInput(
        task_id=1,
        message="test",
        conversation_id="conv-1",
        metadata={},
    )
    final_state = build_initial_state(payload)
    final_state["trace"]["final_text"] = "done"

    mock_executor = MagicMock()
    mock_executor.stream_graph = AsyncMock(
        return_value=GraphExecutionResult(final_state=final_state)
    )

    with patch(
        "backend.services.langgraph_chat.execution.completion_callback.persist_chat_message_from_container"
    ) as mock_persist, patch(
        "agent.graph.builders.simple_tool_builder.build_simple_tool_graph",
        return_value=MagicMock(),
    ):
        facade = LangGraphChatFacade(
            checkpointer_service=DummyCheckpointerService(MagicMock()),
            executor=mock_executor,
            streaming_adapter=MagicMock(),
        )
        result = await facade.resume_from_interrupt(
            task_id=1,
            graph_thread_id=GRAPH_THREAD_ID,
            response={"action": "approve"},
            reserved_message_id=123,
        )

    assert result.final_text == "done"
    mock_persist.assert_called_once()
    assert mock_persist.call_args.kwargs["reason"] == "resume_normal"
    assert mock_persist.call_args.kwargs["reserved_message_id"] == 123
    assert mock_persist.call_args.kwargs["conversation_id"] == "conv-1"
    assert mock_persist.call_args.kwargs["turn_number"] == 123


@pytest.mark.asyncio
async def test_resume_propagates_branch_and_turn_to_usage_metadata() -> None:
    """HITL resume must normalize metadata on the real write path.

    Regression for the Task 1.2 blocker: ``_extract_usage_from_state`` was
    called from the resume path with neither ``execution_branch`` nor
    ``turn_index``, so every usage row produced by an interrupt-resume
    continuation was persisted with ``execution_branch="unknown"`` /
    ``turn_index=None`` and the HITL logger crashed on the envelope's
    missing ``total_tokens`` attribute when any LLM usage was present.
    """
    from agent.graph import InteractiveInput, build_initial_state
    from backend.services.langgraph_chat.facade import LangGraphChatFacade
    from backend.services.langgraph_chat.execution.graph_executor import GraphExecutionResult
    from backend.services.langgraph_chat.hitl_constants import (
        GRAPH_NAME_DEEP_REASONING,
        GRAPH_NAME_SIMPLE_TOOL,
    )

    payload = InteractiveInput(
        task_id=1,
        message="test",
        conversation_id="conv-1",
        metadata={},
    )
    final_state = build_initial_state(payload)
    final_state["trace"]["final_text"] = "done"
    # Simulate usage captured by graph nodes during the resumed continuation.
    final_state["trace"]["usage_records"] = [
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "model": "gpt-4o-mini",
            "provider": "openai",
            "source": "simple_tool",
        }
    ]

    mock_executor = MagicMock()
    mock_executor.stream_graph = AsyncMock(
        return_value=GraphExecutionResult(final_state=final_state)
    )

    with patch(
        "backend.services.langgraph_chat.execution.completion_callback.persist_chat_message_from_container"
    ), patch(
        "agent.graph.builders.simple_tool_builder.build_simple_tool_graph",
        return_value=MagicMock(),
    ):
        facade = LangGraphChatFacade(
            checkpointer_service=DummyCheckpointerService(MagicMock()),
            executor=mock_executor,
            streaming_adapter=MagicMock(),
        )
        result = await facade.resume_from_interrupt(
            task_id=1,
            graph_thread_id=GRAPH_THREAD_ID,
            response={"action": "approve"},
            graph_name=GRAPH_NAME_SIMPLE_TOOL,
            reserved_message_id=42,
        )

    assert result.usage is not None and len(result.usage) == 1
    envelope = result.usage[0]
    # Blocker 2: branch/turn must be normalized, not "unknown"/None.
    assert envelope.metadata.execution_branch == GRAPH_NAME_SIMPLE_TOOL
    assert envelope.metadata.turn_index == 42
    # Blocker 1 smoke check: summing ``entry.usage.total_tokens`` (the new
    # logger shape) must not raise AttributeError.
    assert sum(entry.usage.total_tokens for entry in result.usage) == 150


@pytest.mark.asyncio
async def test_resume_maps_deep_reasoning_graph_to_deep_reasoning_branch() -> None:
    """The resume path must map ``graph_name=deep_reasoning`` to the
    ``deep_reasoning`` execution branch so usage metadata stays aligned
    with the non-interrupt handlers."""
    from agent.graph import InteractiveInput, build_initial_state
    from backend.services.langgraph_chat.facade import LangGraphChatFacade
    from backend.services.langgraph_chat.execution.graph_executor import GraphExecutionResult
    from backend.services.langgraph_chat.hitl_constants import (
        GRAPH_NAME_DEEP_REASONING,
    )

    payload = InteractiveInput(
        task_id=1,
        message="test",
        conversation_id="conv-1",
        metadata={},
    )
    final_state = build_initial_state(payload)
    final_state["trace"]["final_text"] = "done"
    final_state["trace"]["usage_records"] = [
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "model": "gpt-4o-mini",
            "provider": "openai",
            "source": "decision_router",
        }
    ]

    mock_executor = MagicMock()
    mock_executor.stream_graph = AsyncMock(
        return_value=GraphExecutionResult(final_state=final_state)
    )

    with patch(
        "backend.services.langgraph_chat.execution.completion_callback.persist_chat_message_from_container"
    ), patch(
        "agent.graph.builders.deep_reasoning_builder.compile_deep_reasoning_graph"
    ) as mock_build, patch(
        "backend.services.langgraph_chat.checkpoint.continuation_service.resolve_resume_turn_number",
        return_value=7,
    ):
        facade = LangGraphChatFacade(
            checkpointer_service=DummyCheckpointerService(MagicMock()),
            executor=mock_executor,
            streaming_adapter=MagicMock(),
        )
        mock_build.return_value = MagicMock()
        result = await facade.resume_from_interrupt(
            task_id=1,
            graph_thread_id=GRAPH_THREAD_ID,
            response={"action": "approve"},
            graph_name=GRAPH_NAME_DEEP_REASONING,
            reserved_message_id=7,
        )

    assert result.usage is not None and len(result.usage) == 1
    envelope = result.usage[0]
    assert envelope.metadata.execution_branch == GRAPH_NAME_DEEP_REASONING
    assert envelope.metadata.turn_index == 7


@pytest.mark.asyncio
async def test_resume_interrupt_uses_shared_container_persistence() -> None:
    from agent.graph import InteractiveInput, build_initial_state
    from backend.services.langgraph_chat.facade import LangGraphChatFacade
    from backend.services.langgraph_chat.execution.graph_executor import GraphExecutionResult

    payload = InteractiveInput(
        task_id=1,
        message="test",
        conversation_id="conv-1",
        metadata={},
    )
    interrupted_state = build_initial_state(payload)

    mock_executor = MagicMock()
    mock_executor.stream_graph = AsyncMock(
        return_value=GraphExecutionResult(
            final_state=interrupted_state,
            interrupt={"type": "tool_approval"},
        )
    )

    with patch(
        "backend.services.langgraph_chat.execution.completion_callback.persist_chat_message_from_container"
    ) as mock_persist, patch(
        "agent.graph.builders.simple_tool_builder.build_simple_tool_graph",
        return_value=MagicMock(),
    ):
        facade = LangGraphChatFacade(
            checkpointer_service=DummyCheckpointerService(MagicMock()),
            executor=mock_executor,
            streaming_adapter=MagicMock(),
        )
        result = await facade.resume_from_interrupt(
            task_id=1,
            graph_thread_id=GRAPH_THREAD_ID,
            response={"action": "approve"},
            reserved_message_id=321,
        )

    assert result.metadata["interrupt"] is True
    mock_persist.assert_called_once()
    assert mock_persist.call_args.kwargs["reason"] == "resume_hitl_interrupt"
    assert mock_persist.call_args.kwargs["error"] == "interrupted"
    assert mock_persist.call_args.kwargs["conversation_id"] == "conv-1"
    assert mock_persist.call_args.kwargs["turn_number"] == 321
