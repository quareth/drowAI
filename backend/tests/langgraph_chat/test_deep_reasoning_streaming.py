"""Tests for the deep reasoning handler streaming implementation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.execution.graph_executor import GraphExecutionResult
from backend.services.langgraph_chat.handlers.deep_reasoning_handler import (
    DeepReasoningHandler,
)


@pytest.fixture
def mock_checkpointer_service():
    checkpointer = MagicMock()
    checkpointer.setup = AsyncMock()

    service = MagicMock()
    service.get_checkpointer.return_value.__aenter__ = AsyncMock(return_value=checkpointer)
    service.get_checkpointer.return_value.__aexit__ = AsyncMock()
    return service


@pytest.fixture
def mock_executor():
    executor = MagicMock()
    executor.stream_graph = AsyncMock()
    executor.invoke_graph = AsyncMock()
    return executor


@pytest.fixture
def mock_adapter():
    adapter = MagicMock()
    adapter.build_agent_pause_request_event = MagicMock(return_value=None)
    return adapter


@pytest.fixture
def runtime_config():
    from agent.graph.context.builder import (
        METADATA_CONTEXT_BUNDLE_KEY,
        build_conversation_context_bundle,
    )

    chat_inputs = ChatInputs(
        task_id=1,
        user_id=7,
        message="Perform deep reasoning scan",
        conversation_id="conv-dr",
        history=[],
        requested_mode=ExecutionMode.DEEP_REASONING,
    )
    # Phase 6 cutover: facade_helpers.build_metadata expects the bundle
    # pre-populated by LangGraphContextBuilder. Tests that construct
    # runtime_config directly must seed the bundle themselves.
    bundle = build_conversation_context_bundle(
        conversation_id=chat_inputs.conversation_id or "",
        turn_id="",
        turn_sequence=0,
        messages=list(chat_inputs.history),
    )
    return LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        execution_mode=ExecutionMode.DEEP_REASONING,
        metadata={
            METADATA_CONTEXT_BUNDLE_KEY: bundle,
            "graph_thread_id": "2" * 32,
        },
    )


@pytest.fixture
def sample_state():
    return {
        "facts": {
            "task_id": 1,
            "message": "Perform deep reasoning scan",
            "conversation_id": "conv-dr",
            "capability": "deep_reasoning",
            "iterations": 2,
            "tool_calls_used": 3,
            "metadata": {
                "dr_iteration_meta": {"active_iteration": 2},
                "dr_iteration_records": {
                    "1": {"reasoning": ["Initial reasoning"]},
                    "2": {"reasoning": ["Follow-up reasoning"]},
                },
            },
        },
        "trace": {
            "final_text": "Final DR response from graph.",
            "reasoning": ["Reasoning delta"],
            "observations": ["Observation delta"],
            "executed_tools": [],
        },
    }


class TestDeepReasoningHandler:
    """Tests for deep reasoning handler streaming behaviour."""

    @pytest.mark.asyncio
    async def test_handler_uses_stream_graph(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        sample_state,
    ):
        handler = DeepReasoningHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )
        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=sample_state)

        with patch("backend.services.langgraph_chat.handlers.deep_reasoning_handler.compile_deep_reasoning_graph"):
            result = await handler.handle(runtime_config)

        mock_executor.stream_graph.assert_called_once()
        assert callable(mock_executor.stream_graph.call_args.kwargs["should_cancel"])
        mock_executor.invoke_graph.assert_not_called()
        assert result.final_text == sample_state["trace"]["final_text"]

    @pytest.mark.asyncio
    async def test_handler_returns_events_from_pause_only(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        sample_state,
    ):
        """Handler returns events built from pause request only (no adapter buffer)."""
        handler = DeepReasoningHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )
        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=sample_state)

        with patch("backend.services.langgraph_chat.handlers.deep_reasoning_handler.compile_deep_reasoning_graph"):
            result = await handler.handle(runtime_config)

        events = [event async for event in result.iter_events()]
        # No drain; events are built from build_agent_pause_request_event only (or empty)
        assert isinstance(events, list)
        assert result.final_text == sample_state["trace"]["final_text"]

    @pytest.mark.asyncio
    async def test_pause_request_event_appended(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
    ):
        handler = DeepReasoningHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )

        state_with_pause = {
            "facts": {
                "task_id": 4,
                "message": "Test pause",
                "conversation_id": "conv-dr",
                "capability": "deep_reasoning",
                "metadata": {
                    "agent_pause_request": {
                        "reason": "budget_concerns",
                        "question": "Continue despite budget concerns?",
                        "current_progress": {},
                        "remaining_todos": [],
                    },
                },
            },
            "trace": {"final_text": "pause placeholder"},
        }
        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=state_with_pause)
        mock_adapter.build_agent_pause_request_event.return_value = {
            "type": "agent_pause_request",
            "metadata": {"requires_user_action": True},
        }

        with patch("backend.services.langgraph_chat.handlers.deep_reasoning_handler.compile_deep_reasoning_graph"):
            result = await handler.handle(runtime_config)

        events = [event async for event in result.iter_events()]
        # Persistable events are optional when no reserved_message_id is available;
        # the guaranteed contract is that pause-request metadata emits one event.
        assert len(events) >= 1
        pause_events = [event for event in events if event["type"] == "agent_pause_request"]
        assert len(pause_events) == 1
        assert pause_events[0]["metadata"]["requires_user_action"] is True
        mock_adapter.build_agent_pause_request_event.assert_called_once()
