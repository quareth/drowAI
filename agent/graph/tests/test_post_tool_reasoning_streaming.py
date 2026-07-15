"""Tests for post_tool_reasoning: Streaming Integration.

Tests cover:
- Streaming parser (_stream_and_parse_response)
- Non-streaming call (_non_streaming_call)
- Main node function with writer (streaming path)
- Streaming event emissions
- Error handling during streaming"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from agent.graph.nodes.post_tool_reasoning import (
    PostToolReasoningError,
    PostToolReasoningOutput,
    STREAMING_STEP_NAME,
    _stream_and_parse_response,
    _non_streaming_call,
    post_tool_reasoning,
)
from agent.graph.state import FactsState, InteractiveState, TraceState

pytestmark = pytest.mark.skip(
    reason=(
        "Legacy combined observation+decision streaming contract was removed; "
        "post-tool reasoning now uses decision-only JSON plus separate articulation."
    )
)


# -----------------------------------------------------------------------------
# Test Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def valid_delimiter_response_str() -> str:
    """Create a valid response string with the new delimiter format."""
    return (
        "I analyzed the tool output and discovered important findings. "
        "The target has several open services that warrant further investigation. "
        "This aligns with our initial hypothesis about network exposure. "
        "I will proceed to probe the HTTP service on port 80.\n"
        f"{DECISION_DELIMITER}\n"
        '{"next_action": "call_tool", "action_reasoning": "HTTP service detected, need to enumerate web content."}'
    )


@pytest.fixture
def sample_interactive_state() -> InteractiveState:
    """Create a sample InteractiveState for testing."""
    facts = FactsState(
        task_id=123,
        message="Perform network reconnaissance",
        conversation_id="conv-test-123",
        capability="deep_reasoning",
        selected_tool="nmap",
        tool_parameters={"target": "192.168.1.100"},
        current_goal="Identify open services",
        iterations=3,
        metadata={
            "api_key": "test-api-key",
            "model": "gpt-4o-mini",
            "synthesized_output": {
                "tool": "nmap",
                "summary": "Scan completed",
                "key_findings": ["Port 22 open", "Port 80 open"],
            },
        },
        decision_history=["call_tool: Running port scan"],
    )
    trace = TraceState(
        reasoning=["Started reconnaissance"],
        observations=["Initial scan revealed target is up"],
        decision_log=[],
    )
    return InteractiveState(facts=facts, trace=trace)


@pytest.fixture
def mock_writer():
    """Create a mock StreamWriter for testing."""
    writer = MagicMock()
    # Writer should be callable and accept one argument
    writer.return_value = None
    return writer


@pytest.fixture
def mock_llm_client_streaming():
    """Create a mock LLMClient that supports streaming."""
    client = AsyncMock()
    
    # Default non-streaming response in new delimiter format
    client.chat = AsyncMock(return_value=(
        "I observed the tool completed successfully. The results show interesting patterns. "
        "I will analyze these further for vulnerabilities.\n"
        f"{DECISION_DELIMITER}\n"
        '{"next_action": "think_more", "action_reasoning": "Need to analyze results before next tool."}'
    ))
    
    return client


def create_streaming_iterator(chunks: List[str]) -> AsyncIterator[str]:
    """Create an async iterator from a list of chunks."""
    async def iterator():
        for chunk in chunks:
            yield chunk
    return iterator()


def test_make_fallback_observation_pads_short_summary(
    sample_interactive_state: InteractiveState,
) -> None:
    """Fallback observations shorter than model minimum length should be padded."""
    from agent.graph.nodes.post_tool_reasoning import node as ptr_node
    from agent.graph.nodes.post_tool_reasoning.models import (
        PostToolReasoningDecisionOutput,
    )

    sample_interactive_state.facts.metadata = sample_interactive_state.facts.metadata or {}
    sample_interactive_state.facts.metadata["synthesized_output"] = {
        "summary": "OK",
    }

    decision_output = PostToolReasoningDecisionOutput(
        next_action="finalize",
        action_reasoning="Scan done.",
    )

    observation = ptr_node._make_fallback_observation(
        sample_interactive_state,
        decision_output,
    )

    assert len(observation) >= 10
    assert "Action:" in observation


@pytest.mark.asyncio
async def test_post_tool_reasoning_emits_todo_progress(
    sample_interactive_state: InteractiveState,
    monkeypatch,
) -> None:
    from agent.graph.nodes.post_tool_reasoning import node as ptr_module
    from agent.graph.nodes.post_tool_reasoning.models import TodoProgress

    output = PostToolReasoningOutput(
        observation="I reviewed the results and made progress on the todo items.",
        next_action="think_more",
        action_reasoning="Need to reason further about the output.",
        todo_progress=[
            TodoProgress(
                index=0,
                status="completed",
                completion_type="positive",
                completion_reason="Primary objective completed from this tool output",
            )
        ],
    )

    class DummyAdapter:
        def get_stream_identifiers(self, *_args, **_kwargs):
            return ("conv-1", "turn-1")

        async def stream_observation(self, **_kwargs):
            return output, True, None

    monkeypatch.setattr(ptr_module.StreamingAdapterFactory, "create", lambda *_args, **_kwargs: DummyAdapter())
    monkeypatch.setattr(ptr_module, "resolve_llm_client", lambda *_args, **_kwargs: object())

    # Use a capturing writer that records all emitted events
    captured_events = []
    def capturing_writer(event):
        captured_events.append(event)

    sample_interactive_state.facts.todo_list = ["Todo 1"]
    sample_interactive_state.facts.metadata["todo_id_map"] = ["todo-1"]

    await post_tool_reasoning(sample_interactive_state, writer=capturing_writer)

    # Find the todo_progress event emitted through the emitter
    todo_events = [e for e in captured_events if e.get("type") == "todo_progress"]
    assert todo_events, f"No todo_progress event found in {len(captured_events)} events"
    todo_updates = todo_events[0]["todo_updates"]
    assert todo_updates[0]["id"] == "todo-1"
    assert todo_updates[0]["status"] == "completed"


# -----------------------------------------------------------------------------
# Tests: _non_streaming_call
# -----------------------------------------------------------------------------


class TestNonStreamingCall:
    """Tests for _non_streaming_call function."""
    
    @pytest.mark.asyncio
    async def test_successful_call(self, mock_llm_client_streaming):
        """Should successfully call LLM and parse response."""
        result = await _non_streaming_call(
            mock_llm_client_streaming,
            "System prompt",
            "User prompt",
        )
        
        assert isinstance(result, PostToolReasoningOutput)
        assert result.next_action == "think_more"
        mock_llm_client_streaming.chat.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_passes_temperature_and_tokens(self, mock_llm_client_streaming):
        """Should pass correct temperature and max_tokens."""
        await _non_streaming_call(
            mock_llm_client_streaming,
            "System",
            "User",
        )
        
        call_kwargs = mock_llm_client_streaming.chat.call_args
        assert call_kwargs[1]["temperature"] == 0.3
        assert call_kwargs[1]["max_tokens"] == 500
    
    @pytest.mark.asyncio
    async def test_raises_on_llm_error(self, mock_llm_client_streaming):
        """Should raise PostToolReasoningError on LLM failure."""
        mock_llm_client_streaming.chat.side_effect = Exception("API error")
        
        with pytest.raises(PostToolReasoningError) as exc_info:
            await _non_streaming_call(
                mock_llm_client_streaming,
                "System",
                "User",
            )
        
        assert "LLM call failed" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_raises_on_invalid_response(self, mock_llm_client_streaming):
        """Should raise PostToolReasoningError on invalid response."""
        mock_llm_client_streaming.chat.return_value = "Not JSON"
        
        with pytest.raises(PostToolReasoningError) as exc_info:
            await _non_streaming_call(
                mock_llm_client_streaming,
                "System",
                "User",
            )
        
        # Error message changed: now checks for missing delimiter
        assert "missing" in str(exc_info.value).lower() or "delimiter" in str(exc_info.value).lower()


# -----------------------------------------------------------------------------
# Tests: _stream_and_parse_response
# -----------------------------------------------------------------------------


class TestStreamAndParseResponse:
    """Tests for _stream_and_parse_response function."""
    
    @pytest.mark.asyncio
    async def test_streams_and_parses_valid_response(
        self, mock_llm_client_streaming, mock_writer, valid_delimiter_response_str
    ):
        """Should stream chunks and parse final response."""
        # Split response into chunks to simulate streaming
        chunks = [valid_delimiter_response_str[i:i+50] for i in range(0, len(valid_delimiter_response_str), 50)]
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator(chunks)
        )
        
        output, streamed, usage = await _stream_and_parse_response(
            mock_llm_client_streaming,
            "System prompt",
            "User prompt",
            mock_writer,
            "conv-123",
            "turn-456",
            sequence=1,
        )
        
        assert isinstance(output, PostToolReasoningOutput)
        assert output.next_action == "call_tool"
        assert streamed is True
        # Usage may be None when using mock client without usage-aware streaming
        assert usage is None or isinstance(usage, dict)
    
    @pytest.mark.asyncio
    async def test_emits_start_event(
        self, mock_llm_client_streaming, mock_writer, valid_delimiter_response_str
    ):
        """Should emit observation_start at beginning."""
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator([valid_delimiter_response_str])
        )
        
        with patch("agent.graph.nodes.post_tool_reasoning.node.emit_observation_start") as mock_start:
            await _stream_and_parse_response(
                mock_llm_client_streaming,
                "System",
                "User",
                mock_writer,
                "conv-123",
                "turn-456",
                sequence=1,
            )
            
            mock_start.assert_called_once()
            call_args = mock_start.call_args
            assert call_args[0][0] == mock_writer  # writer
            assert call_args[0][1] == "conv-123"  # conversation_id
            assert call_args[0][2] == "turn-456"  # turn_id
    
    @pytest.mark.asyncio
    async def test_emits_delta_events_for_each_chunk(
        self, mock_llm_client_streaming, mock_writer
    ):
        """Should emit observation_delta for each streaming chunk."""
        chunks = ["chunk1", "chunk2", "chunk3"]
        full_response = json.dumps({
            "observation": "chunk1chunk2chunk3 This is valid observation text.",
            "next_action": "finalize",
            "action_reasoning": "Done.",
        })
        # Split full response
        response_chunks = ["chunk1", "chunk2", full_response[12:]]  # Simulate chunked response
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator(response_chunks)
        )
        
        with patch("agent.graph.nodes.post_tool_reasoning.node.emit_observation_delta") as mock_delta:
            try:
                await _stream_and_parse_response(
                    mock_llm_client_streaming,
                    "System",
                    "User",
                    mock_writer,
                    "conv-123",
                    "turn-456",
                    sequence=1,
                )
            except PostToolReasoningError:
                pass  # May fail on parse, but deltas should still be emitted
            
            # Should have emitted deltas for each chunk
            assert mock_delta.call_count >= 1
    
    @pytest.mark.asyncio
    async def test_emits_snapshot_with_observation(
        self, mock_llm_client_streaming, mock_writer, valid_delimiter_response_str
    ):
        """Should emit observation_snapshot with clean observation text."""
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator([valid_delimiter_response_str])
        )
        
        with patch("agent.graph.nodes.post_tool_reasoning.node.emit_observation_snapshot") as mock_snap:
            output, _, _ = await _stream_and_parse_response(
                mock_llm_client_streaming,
                "System",
                "User",
                mock_writer,
                "conv-123",
                "turn-456",
                sequence=1,
            )
            
            mock_snap.assert_called_once()
            call_args = mock_snap.call_args
            # Second positional arg should be the observation text
            assert call_args[0][1] == output.observation
    
    @pytest.mark.asyncio
    async def test_emits_section_end(
        self, mock_llm_client_streaming, mock_writer, valid_delimiter_response_str
    ):
        """Should emit observation_section_end at completion."""
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator([valid_delimiter_response_str])
        )
        
        with patch("agent.graph.nodes.post_tool_reasoning.node.emit_observation_section_end") as mock_end:
            await _stream_and_parse_response(
                mock_llm_client_streaming,
                "System",
                "User",
                mock_writer,
                "conv-123",
                "turn-456",
                sequence=1,
            )
            
            mock_end.assert_called_once()
            call_args = mock_end.call_args
            assert call_args[0][1] == STREAMING_STEP_NAME
    
    @pytest.mark.asyncio
    async def test_emits_section_end_on_error(
        self, mock_llm_client_streaming, mock_writer
    ):
        """Should emit section_end even when streaming fails."""
        async def failing_iterator():
            yield "some content"
            raise Exception("Stream error")
        
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=failing_iterator()
        )
        
        with patch("agent.graph.nodes.post_tool_reasoning.node.emit_observation_section_end") as mock_end:
            with pytest.raises(PostToolReasoningError):
                await _stream_and_parse_response(
                    mock_llm_client_streaming,
                    "System",
                    "User",
                    mock_writer,
                    "conv-123",
                    "turn-456",
                    sequence=1,
                )
            
            # Should still emit section end
            mock_end.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_raises_on_empty_stream(
        self, mock_llm_client_streaming, mock_writer
    ):
        """Should raise PostToolReasoningError on empty stream."""
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator([])
        )
        
        with pytest.raises(PostToolReasoningError) as exc_info:
            await _stream_and_parse_response(
                mock_llm_client_streaming,
                "System",
                "User",
                mock_writer,
                "conv-123",
                "turn-456",
                sequence=None,
            )
        
        assert "empty response" in str(exc_info.value).lower()
    
    @pytest.mark.asyncio
    async def test_raises_on_invalid_response(
        self, mock_llm_client_streaming, mock_writer
    ):
        """Should raise PostToolReasoningError on invalid response after streaming."""
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator(["Invalid JSON response"])
        )
        
        with pytest.raises(PostToolReasoningError) as exc_info:
            await _stream_and_parse_response(
                mock_llm_client_streaming,
                "System",
                "User",
                mock_writer,
                "conv-123",
                "turn-456",
                sequence=None,
            )
        
        # Error message changed: now checks for missing delimiter
        assert "missing" in str(exc_info.value).lower() or "delimiter" in str(exc_info.value).lower()
    
    @pytest.mark.asyncio
    async def test_handles_none_chunks(
        self, mock_llm_client_streaming, mock_writer
    ):
        """Should skip None/empty chunks during streaming."""
        # Split response so observation comes in chunks before delimiter
        observation_part = "I analyzed the tool output and discovered important findings."
        decision_part = f"\n{DECISION_DELIMITER}\n" + '{"next_action": "call_tool", "action_reasoning": "Need more data"}'
        chunks = [None, "", observation_part, None, "", decision_part]
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator(chunks)
        )
        
        output, streamed, _ = await _stream_and_parse_response(
            mock_llm_client_streaming,
            "System",
            "User",
            mock_writer,
            "conv-123",
            "turn-456",
            sequence=1,
        )
        
        assert isinstance(output, PostToolReasoningOutput)
        assert streamed is True  # Should have streamed the observation part


# -----------------------------------------------------------------------------
# Tests: Main Node with Streaming
# -----------------------------------------------------------------------------


class TestPostToolReasoningWithStreaming:
    """Tests for post_tool_reasoning with streaming enabled."""
    
    @pytest.mark.asyncio
    async def test_uses_streaming_path_with_writer(
        self, sample_interactive_state, mock_llm_client_streaming, mock_writer
    ):
        """Should use streaming path when writer is provided."""
        # Split response into chunks so observation streams before delimiter
        observation_chunk = "I analyzed the tool output and discovered important findings. "
        delimiter_chunk = f"\n{DECISION_DELIMITER}\n" + '{"next_action": "call_tool", "action_reasoning": "Need more data"}'
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator([observation_chunk, delimiter_chunk])
        )
        
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client_streaming,
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.derive_dr_stream_identifiers",
            return_value=("conv-123", "turn-456", None),
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.resolve_turn_sequence",
            return_value=1,
        ):
            result = await post_tool_reasoning(
                sample_interactive_state.as_graph_state(),
                writer=mock_writer,
            )
        
        # Should have used stream_chat_messages, not chat
        mock_llm_client_streaming.stream_chat_messages.assert_called_once()
        mock_llm_client_streaming.chat.assert_not_called()
        
        # Should mark as streamed (observation was streamed before delimiter)
        assert result["facts"]["metadata"]["observation_streamed"] is True
    
    @pytest.mark.asyncio
    async def test_uses_non_streaming_without_writer(
        self, sample_interactive_state, mock_llm_client_streaming
    ):
        """Should use non-streaming path when writer is not provided."""
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client_streaming,
        ):
            result = await post_tool_reasoning(
                sample_interactive_state.as_graph_state(),
                writer=None,
            )
        
        # Should have used chat, not stream_chat_messages
        mock_llm_client_streaming.chat.assert_called_once()
        
        # Should mark as not streamed
        assert result["facts"]["metadata"]["observation_streamed"] is False
    
    @pytest.mark.asyncio
    async def test_records_observation_after_streaming(
        self, sample_interactive_state, mock_llm_client_streaming, mock_writer, valid_delimiter_response_str
    ):
        """Should record observation to state after streaming."""
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator([valid_delimiter_response_str])
        )
        
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client_streaming,
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.derive_dr_stream_identifiers",
            return_value=("conv-123", "turn-456", None),
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.resolve_turn_sequence",
            return_value=1,
        ):
            result = await post_tool_reasoning(
                sample_interactive_state.as_graph_state(),
                writer=mock_writer,
            )
        
        # Should have recorded observation
        observations = result["trace"]["observations"]
        assert len(observations) == 2  # Original + new
        assert "HTTP service" in observations[-1] or "analyzed" in observations[-1].lower()
    
    @pytest.mark.asyncio
    async def test_records_decision_after_streaming(
        self, sample_interactive_state, mock_llm_client_streaming, mock_writer, valid_delimiter_response_str
    ):
        """Should record decision to state after streaming."""
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator([valid_delimiter_response_str])
        )
        
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client_streaming,
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.derive_dr_stream_identifiers",
            return_value=("conv-123", "turn-456", None),
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.resolve_turn_sequence",
            return_value=1,
        ):
            result = await post_tool_reasoning(
                sample_interactive_state.as_graph_state(),
                writer=mock_writer,
            )
        
        # Should have recorded decision
        decision_history = result["facts"]["decision_history"]
        assert "call_tool" in decision_history[-1]
        
        # Metadata should have last action
        assert result["facts"]["metadata"]["last_post_tool_action"] == "call_tool"
    
    @pytest.mark.asyncio
    async def test_streaming_error_propagates(
        self, sample_interactive_state, mock_llm_client_streaming, mock_writer
    ):
        """Streaming errors should propagate as PostToolReasoningError."""
        async def error_iterator():
            yield "partial"
            raise Exception("Network error")
        
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=error_iterator()
        )
        
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client_streaming,
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.derive_dr_stream_identifiers",
            return_value=("conv-123", "turn-456", None),
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.resolve_turn_sequence",
            return_value=1,
        ):
            with pytest.raises(PostToolReasoningError) as exc_info:
                await post_tool_reasoning(
                    sample_interactive_state.as_graph_state(),
                    writer=mock_writer,
                )
        
        assert "streaming failed" in str(exc_info.value).lower()


# -----------------------------------------------------------------------------
# Tests: Streaming Event Order
# -----------------------------------------------------------------------------


class TestStreamingEventOrder:
    """Tests to verify correct order of streaming events."""
    
    @pytest.mark.asyncio
    async def test_event_order_success(
        self, mock_llm_client_streaming, mock_writer, valid_delimiter_response_str
    ):
        """Events should be emitted in correct order: start, deltas, snapshot, end."""
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator(["part1", "part2", valid_delimiter_response_str[10:]])
        )
        
        event_order: List[str] = []
        
        def track_start(*args, **kwargs):
            event_order.append("start")
        
        def track_delta(*args, **kwargs):
            event_order.append("delta")
        
        def track_snapshot(*args, **kwargs):
            event_order.append("snapshot")
        
        def track_end(*args, **kwargs):
            event_order.append("end")
        
        with patch("agent.graph.nodes.post_tool_reasoning.node.emit_observation_start", side_effect=track_start), \
             patch("agent.graph.nodes.post_tool_reasoning.node.emit_observation_delta", side_effect=track_delta), \
             patch("agent.graph.nodes.post_tool_reasoning.node.emit_observation_snapshot", side_effect=track_snapshot), \
             patch("agent.graph.nodes.post_tool_reasoning.node.emit_observation_section_end", side_effect=track_end):
            
            try:
                await _stream_and_parse_response(
                    mock_llm_client_streaming,
                    "System",
                    "User",
                    mock_writer,
                    "conv-123",
                    "turn-456",
                    sequence=1,
                )
            except PostToolReasoningError:
                pass  # May fail on parse due to chunking
        
        # Verify order: start comes first
        assert event_order[0] == "start"
        # End comes last
        assert event_order[-1] == "end"
        # If snapshot emitted, it should be before end
        if "snapshot" in event_order:
            snapshot_idx = event_order.index("snapshot")
            end_idx = event_order.index("end")
            assert snapshot_idx < end_idx


class TestRaceSafety:
    """Race-focused tests for post-tool reasoning streaming boundaries."""

    @pytest.mark.asyncio
    async def test_node_returns_only_after_stream_section_end(
        self,
        sample_interactive_state: InteractiveState,
        mock_writer,
    ) -> None:
        from agent.graph.nodes.post_tool_reasoning import node as ptr_node
        from agent.graph.nodes.post_tool_reasoning.models import PostToolReasoningDecisionOutput

        events: List[str] = []
        original_record_decision = ptr_node._record_decision

        class SequencedAdapter:
            def get_stream_identifiers(self, *_args, **_kwargs):
                return ("conv-123", "turn-456")

            async def stream_observation_text(self, suppress_observation_start: bool = False, **_kwargs):
                if not suppress_observation_start:
                    events.append("observation_start")
                await asyncio.sleep(0)
                events.append("observation_section_end")
                return (
                    "Observed completion evidence and prepared final response.",
                    True,
                    None,
                )

        def _assert_no_decision_before_section_end(*args, **kwargs):
            assert events == ["observation_start", "observation_section_end"]
            return original_record_decision(*args, **kwargs)

        decision_output = PostToolReasoningDecisionOutput(
            next_action="finalize",
            action_reasoning="Task is complete.",
        )

        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=MagicMock(),
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.node.analyze_tool_result",
            return_value=decision_output,
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.node.StreamingAdapterFactory.create",
            return_value=SequencedAdapter(),
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.node._record_decision",
            side_effect=_assert_no_decision_before_section_end,
        ):
            await post_tool_reasoning(sample_interactive_state.as_graph_state(), writer=mock_writer)

        assert events == ["observation_start", "observation_section_end"]


# -----------------------------------------------------------------------------
# Tests: Integration Scenarios
# -----------------------------------------------------------------------------


class TestStreamingIntegration:
    """Integration tests for streaming scenarios."""
    
    @pytest.mark.asyncio
    async def test_complete_streaming_flow(
        self, sample_interactive_state, mock_llm_client_streaming, mock_writer, valid_delimiter_response_str
    ):
        """Complete flow: stream, parse, record, update state."""
        # Setup streaming
        chunks = [valid_delimiter_response_str[i:i+100] for i in range(0, len(valid_delimiter_response_str), 100)]
        mock_llm_client_streaming.stream_chat_messages = MagicMock(
            return_value=create_streaming_iterator(chunks)
        )
        
        with patch(
            "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
            return_value=mock_llm_client_streaming,
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.derive_dr_stream_identifiers",
            return_value=("conv-123", "turn-456", None),
        ), patch(
            "agent.graph.nodes.post_tool_reasoning.resolve_turn_sequence",
            return_value=1,
        ):
            result = await post_tool_reasoning(
                sample_interactive_state.as_graph_state(),
                writer=mock_writer,
            )
        
        # Verify complete state update
        assert result["facts"]["metadata"]["post_tool_reasoning_completed"] is True
        assert result["facts"]["metadata"]["observation_streamed"] is True
        assert result["facts"]["metadata"]["last_post_tool_action"] == "call_tool"
        
        # Verify observation recorded
        assert len(result["trace"]["observations"]) == 2
        
        # Verify decision recorded  
        assert len(result["facts"]["decision_history"]) == 2
        
        # Phase 5 cutover: metadata history log removed.
        assert "history" not in result["facts"]["metadata"]
    
    @pytest.mark.asyncio
    async def test_streaming_with_various_actions(
        self, sample_interactive_state, mock_llm_client_streaming, mock_writer
    ):
        """Test streaming with all action types."""
        actions = ["call_tool", "think_more", "reflect", "finalize"]
        
        for action in actions:
            response = (
                f"Testing {action} action. This is a valid observation with enough content.\n"
                f"{DECISION_DELIMITER}\n"
                f'{{"next_action": "{action}", "action_reasoning": "Reason for {action}."}}'
            )
            
            mock_llm_client_streaming.stream_chat_messages = MagicMock(
                return_value=create_streaming_iterator([response])
            )
            
            # Reset state
            state = sample_interactive_state.as_graph_state()
            
            with patch(
                "agent.graph.nodes.post_tool_reasoning.node.resolve_llm_client",
                return_value=mock_llm_client_streaming,
            ), patch(
                "agent.graph.nodes.post_tool_reasoning.derive_dr_stream_identifiers",
                return_value=("conv-123", "turn-456", None),
            ), patch(
                "agent.graph.nodes.post_tool_reasoning.resolve_turn_sequence",
                return_value=1,
            ):
                result = await post_tool_reasoning(state, writer=mock_writer)
            
            assert result["facts"]["metadata"]["last_post_tool_action"] == action
