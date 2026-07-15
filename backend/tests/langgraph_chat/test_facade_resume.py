"""Regression tests for HITL resume behavior through the LangGraph facade."""

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
async def test_facade_resume_from_interrupt_simple_tool():
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
    mock_executor.stream_graph = AsyncMock(return_value=GraphExecutionResult(final_state=final_state))
    mock_adapter = MagicMock()

    facade = LangGraphChatFacade(
        checkpointer_service=DummyCheckpointerService(MagicMock()),
        executor=mock_executor,
        streaming_adapter=mock_adapter,
    )

    with patch(
        "agent.graph.builders.simple_tool_builder.build_simple_tool_graph",
        return_value=MagicMock(),
    ):
        result = await facade.resume_from_interrupt(
            task_id=1,
            graph_thread_id=GRAPH_THREAD_ID,
            response={"action": "approve"},
        )

    assert result.final_text == "done"
    mock_executor.stream_graph.assert_called_once()
    _, _, config_arg, _ = mock_executor.stream_graph.call_args.args
    assert config_arg["configurable"]["graph_name"] == "simple_tool"


@pytest.mark.asyncio
async def test_facade_resume_from_interrupt_deep_reasoning_graph_name():
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

    compiled_graph = MagicMock()
    mock_executor = MagicMock()
    mock_executor.stream_graph = AsyncMock(
        return_value=GraphExecutionResult(final_state=final_state)
    )
    mock_adapter = MagicMock()

    facade = LangGraphChatFacade(
        checkpointer_service=DummyCheckpointerService(MagicMock()),
        executor=mock_executor,
        streaming_adapter=mock_adapter,
    )

    with patch(
        "agent.graph.builders.deep_reasoning_builder.compile_deep_reasoning_graph",
        return_value=compiled_graph,
    ) as compile_deep_reasoning_graph, patch(
        "agent.graph.builders.simple_tool_builder.build_simple_tool_graph",
        return_value=MagicMock(),
    ) as build_simple_tool_graph:
        result = await facade.resume_from_interrupt(
            task_id=1,
            graph_thread_id=GRAPH_THREAD_ID,
            response={"action": "approve"},
            graph_name="deep_reasoning",
        )

    assert result.final_text == "done"
    compile_deep_reasoning_graph.assert_called_once()
    build_simple_tool_graph.assert_not_called()
    mock_executor.stream_graph.assert_called_once()
    compiled_arg, _, config_arg, _ = mock_executor.stream_graph.call_args.args
    assert compiled_arg is compiled_graph
    assert config_arg["configurable"]["graph_name"] == "deep_reasoning"


@pytest.mark.asyncio
async def test_facade_resume_sets_runtime_warm_config_labels():
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
    mock_executor.stream_graph = AsyncMock(return_value=GraphExecutionResult(final_state=final_state))
    mock_adapter = MagicMock()

    facade = LangGraphChatFacade(
        checkpointer_service=DummyCheckpointerService(MagicMock()),
        executor=mock_executor,
        streaming_adapter=mock_adapter,
    )
    warmup_status = {
        "checkpointer": {"ready": True, "skipped": False},
        "tool_catalog": {"ready": True, "skipped": False},
        "pty_session": {"ready": False, "skipped": True},
    }
    warmup_service = MagicMock()
    warmup_service.get_warmup_status.return_value = warmup_status

    with patch(
        "agent.graph.builders.simple_tool_builder.build_simple_tool_graph",
        return_value=MagicMock(),
    ), patch(
        "backend.services.langgraph_chat.runtime.warmup_service.get_shared_runtime_warmup_service",
        return_value=warmup_service,
    ):
        await facade.resume_from_interrupt(
            task_id=1,
            graph_thread_id=GRAPH_THREAD_ID,
            response={"action": "approve"},
        )

    _, _, config_arg, _ = mock_executor.stream_graph.call_args.args
    configurable = config_arg.get("configurable", {})
    assert configurable.get("runtime_path") == "warm"
    assert configurable.get("runtime_warm") is True
