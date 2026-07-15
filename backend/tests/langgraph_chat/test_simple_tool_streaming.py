"""
Note: Retry and failure recovery tests have moved to post_tool_reasoning tests.
See:
- file:agent/graph/tests/test_post_tool_reasoning_core.py
- file:agent/graph/nodes/post_tool_reasoning/tests/test_failure_detection.py

Simple tool graph now follows a direct execution path without retry logic.
Retry and recovery are handled by the deep reasoning graph via post_tool_reasoning.

Tests for simple tool handler streaming implementation.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent.graph import InteractiveInput, InteractiveState
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.execution.graph_executor import GraphExecutionResult
from backend.services.langgraph_chat.handlers.simple_tool_handler import SimpleToolHandler


@pytest.fixture
def mock_checkpointer_service():
    """Mock checkpointer service with async context manager."""
    checkpointer = MagicMock()
    checkpointer.setup = AsyncMock()
    
    service = MagicMock()
    service.get_checkpointer = MagicMock()
    service.get_checkpointer.return_value.__aenter__ = AsyncMock(return_value=checkpointer)
    service.get_checkpointer.return_value.__aexit__ = AsyncMock()
    
    return service


@pytest.fixture
def mock_executor():
    """Mock LangGraph executor."""
    executor = MagicMock()
    executor.stream_graph = AsyncMock()
    executor.invoke_graph = AsyncMock()
    executor._forward_streaming_event = AsyncMock()
    return executor


@pytest.fixture
def mock_adapter():
    """Mock streaming adapter."""
    adapter = MagicMock()
    adapter.build_tool_events = MagicMock(return_value=[])
    return adapter


@pytest.fixture
def runtime_config():
    """Build runtime config for tests."""
    from agent.graph.context.builder import (
        METADATA_CONTEXT_BUNDLE_KEY,
        build_conversation_context_bundle,
    )

    chat_inputs = ChatInputs(
        task_id=1,
        user_id=1,
        message="Scan network for open ports",
        conversation_id="conv-123",
        api_key="test-key",
        model="gpt-4",
        history=[],
        requested_mode=ExecutionMode.SIMPLE_TOOL,
    )

    # Phase 6 cutover: facade_helpers.build_metadata expects the bundle
    # pre-populated by LangGraphContextBuilder.
    bundle = build_conversation_context_bundle(
        conversation_id=chat_inputs.conversation_id or "",
        turn_id="",
        turn_sequence=0,
        messages=list(chat_inputs.history),
    )
    return LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        execution_mode=ExecutionMode.SIMPLE_TOOL,
        metadata={
            METADATA_CONTEXT_BUNDLE_KEY: bundle,
            "graph_thread_id": "1" * 32,
        },
    )


@pytest.fixture
def sample_state():
    """Sample interactive state for tests."""
    return {
        "facts": {
            "task_id": 1,
            "message": "Scan network for open ports",
            "conversation_id": "conv-123",
            "selected_tool": "nmap",
            "tool_parameters": {"nmap": {"ports": "5000-6000"}},
            "metadata": {
                "api_key": "test-key",
                "model": "gpt-4",
                "last_tool_result": {
                    "tool": "nmap",
                    "status": "success",
                    "stdout_excerpt": "PORT 5432/tcp open postgresql",
                },
            },
            "capability": "simple_tool_execution",
            "iterations": 1,
            "tool_calls_used": 1,
        },
        "trace": {
            "final_text": "Tool executed successfully. Found PostgreSQL on port 5432.",
            "reasoning": ["[ARTICULATION] To scan the network, I will execute nmap."],
            "observations": ["nmap -> Found PostgreSQL on port 5432"],
            "executed_tools": [],
            "usage_records": [
                {
                    "source": "simple_chat",
                    "model": "gpt-4",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                }
            ],
        },
    }


@pytest.fixture
def failure_state():
    """Sample failure interactive state without retry metadata."""
    return {
        "facts": {
            "task_id": 1,
            "message": "Scan network for open ports",
            "conversation_id": "conv-123",
            "selected_tool": "nmap",
            "tool_parameters": {"nmap": {"ports": "5000-6000"}},
            "metadata": {
                "api_key": "test-key",
                "model": "gpt-4",
                "last_tool_result": {
                    "tool": "nmap",
                    "status": "failed",
                    "success": False,
                    "stderr_excerpt": "connection timed out",
                },
                "synthesized_output": {
                    "success": False,
                    "status": "failed",
                    "summary": "Tool failed: timeout",
                    "key_findings": [],
                },
            },
            "capability": "simple_tool_execution",
            "iterations": 1,
            "tool_calls_used": 1,
        },
        "trace": {
            "final_text": "Tool failed but run completed.",
            "reasoning": ["[ARTICULATION] Running nmap"],
            "observations": ["nmap -> timeout"],
            "executed_tools": [],
        },
    }


class TestSimpleToolStreaming:
    """Test simple tool handler streaming implementation."""
    
    @pytest.mark.asyncio
    async def test_handler_uses_stream_graph(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        sample_state,
    ):
        """Test handler calls stream_graph instead of invoke_graph."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )
        
        # Mock stream_graph to return final state
        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=sample_state)
        
        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            result = await handler.handle(runtime_config)
        
        # Verify stream_graph was called
        mock_executor.stream_graph.assert_called_once()
        assert callable(mock_executor.stream_graph.call_args.kwargs["should_cancel"])
        
        # Verify invoke_graph was NOT called (streaming succeeded)
        mock_executor.invoke_graph.assert_not_called()
        
        # Verify result contains final text
        assert result.final_text is not None
        assert "PostgreSQL" in result.final_text
    
    @pytest.mark.asyncio
    async def test_handler_falls_back_on_streaming_error(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        sample_state,
    ):
        """Streaming errors should propagate without synthetic fallback."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )
        
        mock_executor.stream_graph.side_effect = RuntimeError("Stream connection error")
        
        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            with pytest.raises(RuntimeError):
                await handler.handle(runtime_config)
        
        mock_executor.invoke_graph.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_handler_captures_final_state_from_stream(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        sample_state,
    ):
        """Test handler captures final state from streaming."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )
        
        # Mock stream_graph to return complete state
        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=sample_state)
        
        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            result = await handler.handle(runtime_config)
        
        # Verify final text matches state
        assert result.final_text == sample_state["trace"]["final_text"]
        
        # Verify interactive state was captured
        assert result.interactive_state is not None
        assert result.interactive_state.facts.task_id == 1
        assert result.interactive_state.facts.selected_tool == "nmap"
    
    @pytest.mark.asyncio
    async def test_handler_returns_no_synthetic_events(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        sample_state,
    ):
        """Test handler builds events for backward compatibility."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )
        
        # Mock stream_graph to return final state
        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=sample_state)
        
        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            result = await handler.handle(runtime_config)
        
        mock_adapter.build_tool_events.assert_not_called()
        
        events = [event async for event in result.iter_events()]
        assert events == []
    
    @pytest.mark.asyncio
    async def test_handler_raises_on_null_final_state(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        sample_state,
    ):
        """Test handler raises error when streaming returns None."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )
        
        # Mock stream_graph to return None (no state captured)
        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=None)
        
        # Mock invoke_graph for fallback
        mock_executor.invoke_graph.return_value = sample_state
        
        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            with pytest.raises(RuntimeError):
                await handler.handle(runtime_config)
    
    @pytest.mark.asyncio
    async def test_handler_persists_intent_context(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        sample_state,
    ):
        """Test handler persists intent context after execution."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )
        
        # Mock stream_graph to return final state
        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=sample_state)
        
        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.persist_intent_context") as mock_persist:
                result = await handler.handle(runtime_config)
                
                # Verify intent context was persisted
                mock_persist.assert_called_once()
                
                # Verify correct arguments
                call_args = mock_persist.call_args
                assert call_args[0][0] == runtime_config  # First arg is runtime_config
                assert isinstance(call_args[0][1], InteractiveState)  # Second arg is state
    
    @pytest.mark.asyncio
    async def test_handler_attaches_conversation_metadata(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        sample_state,
    ):
        """Test handler attaches conversation IDs to result metadata."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )
        
        # Mock stream_graph to return final state
        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=sample_state)
        
        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            result = await handler.handle(runtime_config)
        
        # Verify metadata contains conversation ID
        assert result.metadata is not None
        assert "conversationId" in result.metadata or "conversation_id" in result.metadata
        
        # Verify execution mode is set
        assert result.metadata["mode"] == ExecutionMode.SIMPLE_TOOL.value
        
        # Verify role is set
        assert result.metadata["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_handler_excludes_tool_execution_metadata(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        sample_state,
    ):
        """Result metadata should not include tool execution summary (tool cards stream separately)."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )

        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=sample_state)

        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            result = await handler.handle(runtime_config)

        assert result.metadata is not None
        assert "tool_execution" not in result.metadata
        assert "retry_tracking" not in result.metadata


    @pytest.mark.asyncio
    async def test_simple_tool_completes_on_failure(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        failure_state,
    ):
        """Handler completes without retry even when tool fails."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )

        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=failure_state)

        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            result = await handler.handle(runtime_config)

        assert result.final_text == failure_state["trace"]["final_text"]
        assert result.interactive_state.facts.metadata["last_tool_result"]["success"] is False

    @pytest.mark.asyncio
    async def test_simple_tool_failure_metadata_in_result(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        failure_state,
    ):
        """Failure information should be surfaced without retry metadata."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )

        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=failure_state)

        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            result = await handler.handle(runtime_config)

        metadata = result.metadata
        assert metadata is not None
        assert "retry_tracking" not in metadata
        assert "tool_execution" not in metadata

    @pytest.mark.asyncio
    async def test_simple_tool_retry_events_from_post_tool_reasoning(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        failure_state,
    ):
        """Retry events should be emitted from post_tool_reasoning (DR graph), not simple tool."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )

        # Mock executor to emit retry events during streaming
        async def mock_stream_with_retry_events(*args, **kwargs):
            # Simulate post_tool_reasoning emitting retry events
            mock_executor._forward_streaming_event.call_count = 0
            
            # Emit retry_start event
            await mock_executor._forward_streaming_event({
                "type": "retry_start",
                "failure_category": "timeout",
                "retry_count": 1,
            })
            
            # Emit retry_attempt event
            await mock_executor._forward_streaming_event({
                "type": "retry_attempt",
                "tool": "nmap",
                "retry_count": 1,
            })
            
            return GraphExecutionResult(final_state=failure_state)

        mock_executor.stream_graph.side_effect = mock_stream_with_retry_events
        mock_executor._forward_streaming_event = AsyncMock()

        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            result = await handler.handle(runtime_config)

        # Verify retry events were forwarded from post_tool_reasoning
        assert mock_executor._forward_streaming_event.call_count >= 2
        
        # Verify event types
        call_args_list = [call[0][0] for call in mock_executor._forward_streaming_event.call_args_list]
        event_types = [event.get("type") for event in call_args_list]
        assert "retry_start" in event_types
        assert "retry_attempt" in event_types

    @pytest.mark.asyncio
    async def test_simple_tool_smoke_preserves_terminal_contract(
        self,
        mock_checkpointer_service,
        mock_executor,
        mock_adapter,
        runtime_config,
        sample_state,
    ):
        """Task 6.2 smoke: simple-tool terminates via stream-only terminal path."""
        handler = SimpleToolHandler(
            checkpointer_service=mock_checkpointer_service,
            executor=mock_executor,
            streaming_adapter=mock_adapter,
        )

        mock_executor.stream_graph.return_value = GraphExecutionResult(final_state=sample_state)

        with patch("backend.services.langgraph_chat.handlers.simple_tool_handler.build_simple_tool_graph"):
            result = await handler.handle(runtime_config)

        assert result.final_text == sample_state["trace"]["final_text"]
        assert mock_executor.stream_graph.call_count == 1
        assert mock_executor.invoke_graph.call_count == 0
        assert result.metadata is not None
        assert result.metadata["mode"] == ExecutionMode.SIMPLE_TOOL.value
        usage_sources = [
            str(record.get("source") or "").lower()
            for record in result.interactive_state.trace.usage_records
            if isinstance(record, dict)
        ]
        assert "decision_router" not in usage_sources


__all__ = [
    "TestSimpleToolStreaming",
]
