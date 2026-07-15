"""Tests for streaming adapters.

Updated to work with UnifiedEventEmitter-based adapters (Issue 13.5 migration).
Adapters now emit events through EventEmitterFactory.create_from_identity(),
so tests verify events via MockWriter instead of patching deprecated helpers.
"""

import asyncio
import pytest
from unittest.mock import MagicMock, patch

from ..dr_adapter import DRStreamingAdapter
from ..simple_adapter import SimpleStreamingAdapter
from ...models import PostToolReasoningOutput

pytestmark = pytest.mark.skip(
    reason=(
        "Legacy combined observation+decision streaming contract was removed; "
        "post-tool reasoning now uses decision-only JSON plus separate articulation."
    )
)


class MockLLMClient:
    """Mock LLMClient for testing streaming."""

    def __init__(self, chunks: list, should_fail: bool = False):
        self.chunks = chunks
        self.should_fail = should_fail

    async def stream_chat_messages(self, messages, temperature=0.7, max_tokens=500):
        """Mock streaming method."""
        if self.should_fail:
            raise Exception("Mock streaming error")

        for chunk in self.chunks:
            yield chunk


class MockWriter:
    """Mock StreamWriter for testing."""

    def __init__(self):
        self.events = []

    def __call__(self, event):
        """Capture emitted events."""
        self.events.append(event)

    def has_event_type(self, event_type: str) -> bool:
        """Check if an event type was emitted."""
        return any(e.get("type") == event_type for e in self.events)

    def get_events_by_type(self, event_type: str) -> list:
        """Get all events of a specific type."""
        return [e for e in self.events if e.get("type") == event_type]


class MockInteractiveState:
    """Mock InteractiveState for testing."""

    def __init__(self, capability="deep_reasoning"):
        self.facts = MagicMock()
        self.facts.capability = capability
        self.facts.task_id = "test-task"
        self.facts.iterations = 1
        self.facts.conversation_id = "conv-test"
        self.facts.metadata = {}


@pytest.mark.asyncio
class TestDRStreamingAdapter:
    """Tests for DRStreamingAdapter."""

    async def test_stream_observation_success(self):
        """Verify DR adapter streams observation events successfully."""
        valid_chunks = [
            "The scan ",
            "completed successfully.\n",
            "===DECISION===\n",
            '{"next_action": "finalize", "action_reasoning": "Scan complete, goal achieved", "user_goal_achieved": true}',
        ]

        mock_client = MockLLMClient(valid_chunks)
        mock_writer = MockWriter()
        adapter = DRStreamingAdapter()

        output, streamed, usage = await adapter.stream_observation(
            mock_writer,
            mock_client,
            "system prompt",
            "user prompt",
            "conv-123",
            "turn-456",
            1,
            sub_turn_index=2,
        )

        assert isinstance(output, PostToolReasoningOutput)
        assert output.next_action == "finalize"
        assert streamed is True
        # Usage is None when using mock client without usage-aware streaming
        assert usage is None
        # Verify events were emitted via writer
        assert mock_writer.has_event_type("observation_start")
        assert mock_writer.has_event_type("observation_delta")
        assert mock_writer.has_event_type("observation_section_end")
        # All events should have identity and sub_turn_index
        for event in mock_writer.events:
            assert event.get("turn_id") == "turn-456"
            assert event.get("conversation_id") == "conv-123"
            assert event.get("sub_turn_index") == 2

    async def test_stream_observation_handles_empty_response(self):
        """Verify DR adapter raises error on empty response."""
        mock_client = MockLLMClient([])
        mock_writer = MockWriter()
        adapter = DRStreamingAdapter()

        with pytest.raises(Exception) as exc_info:
            await adapter.stream_observation(
                mock_writer,
                mock_client,
                "system prompt",
                "user prompt",
                "conv-123",
                "turn-456",
                1,
                sub_turn_index=0,
            )

        assert "empty response" in str(exc_info.value).lower()
        # Section end should still be emitted on error
        assert mock_writer.has_event_type("observation_section_end")

    async def test_stream_observation_recovers_truncated_decision_json(self):
        """Verify DR adapter can parse truncated decision JSON."""
        truncated_chunks = [
            "The scan produced decisive evidence for closure.\n",
            "===DECISION===\n",
            '{"next_action":"finalize","action_reasoning":"The scan found an online host and confirmed the target port is closed, so user request is satisfied and',
        ]

        mock_client = MockLLMClient(truncated_chunks)
        mock_writer = MockWriter()
        adapter = DRStreamingAdapter()

        output, streamed, usage = await adapter.stream_observation(
            mock_writer,
            mock_client,
            "system prompt",
            "user prompt",
            "conv-123",
            "turn-456",
            1,
            sub_turn_index=2,
        )

        assert isinstance(output, PostToolReasoningOutput)
        assert output.next_action == "finalize"
        assert "request is satisfied" in output.action_reasoning.lower()
        assert streamed is True
        assert usage is None

    def test_get_stream_identifiers_dr(self):
        """Verify DR adapter returns identifiers from config."""
        adapter = DRStreamingAdapter()
        mock_state = MockInteractiveState("deep_reasoning")
        mock_state.facts.metadata = {"dr_iteration_meta": {"counter": 0, "active_iteration": 1}}

        config = {"configurable": {
            "canonical_conversation_id": "conv-123",
            "canonical_turn_id": "turn-456",
        }}

        result = adapter.get_stream_identifiers(mock_state, config)

        assert len(result) >= 2
        assert result[0] == "conv-123"
        assert result[1] == "turn-456"

    async def test_stream_observation_streaming_error(self):
        """Verify DR adapter propagates streaming errors."""
        mock_client = MockLLMClient([], should_fail=True)
        mock_writer = MockWriter()
        adapter = DRStreamingAdapter()

        with pytest.raises(Exception):
            await adapter.stream_observation(
                mock_writer,
                mock_client,
                "system prompt",
                "user prompt",
                "conv-123",
                "turn-456",
                1,
                sub_turn_index=0,
            )

    async def test_stream_observation_all_events_have_identity(self):
        """Verify all emitted events carry conversation_id, turn_id, and sub_turn_index."""
        valid_chunks = [
            "Observation text.\n",
            "===DECISION===\n",
            '{"next_action": "finalize", "action_reasoning": "Scan complete and goal achieved", "user_goal_achieved": true}',
        ]

        mock_client = MockLLMClient(valid_chunks)
        mock_writer = MockWriter()
        adapter = DRStreamingAdapter()

        await adapter.stream_observation(
            mock_writer, mock_client,
            "sys", "usr", "conv-abc", "turn-xyz", 5,
            sub_turn_index=1,
        )

        assert len(mock_writer.events) > 0
        for event in mock_writer.events:
            assert event.get("conversation_id") == "conv-abc"
            assert event.get("turn_id") == "turn-xyz"
            assert event.get("sub_turn_index") == 1

    async def test_stream_observation_text_success(self):
        """Verify DR adapter streams plain-text observation without parsing decision payload."""
        valid_chunks = [
            "Tool output showed the host was reachable and port 22 was open.",
            "The scan also exposed an unexpected service.",
        ]

        mock_client = MockLLMClient(valid_chunks)
        mock_writer = MockWriter()
        adapter = DRStreamingAdapter()

        text, streamed, usage = await adapter.stream_observation_text(
            mock_writer,
            mock_client,
            "system prompt",
            "user prompt",
            "conv-123",
            "turn-456",
            1,
            sub_turn_index=2,
        )

        assert text == "".join(valid_chunks).strip()
        assert streamed is True
        assert usage is None
        assert mock_writer.has_event_type("observation_start")
        assert mock_writer.has_event_type("observation_snapshot")
        assert mock_writer.has_event_type("observation_section_end")
        snapshots = mock_writer.get_events_by_type("observation_snapshot")
        assert snapshots
        snapshot = snapshots[0]
        assert (
            snapshot.get("content")
            or snapshot.get("observation")
            or snapshot.get("message")
            or ""
        ) == text

    async def test_stream_observation_text_emits_start_before_first_delta_when_first_chunk_delayed(self):
        """Delayed first chunk still emits observation_start before any delta."""
        class SlowStartLLMClient:
            async def stream_chat_messages(self, messages, temperature=0.3, max_tokens=500):
                await asyncio.sleep(0.001)
                yield "Observation starts after delay."

                # Another delta after an additional delay to avoid a synchronous burst
                await asyncio.sleep(0.001)
                yield " Continued."

        mock_client = SlowStartLLMClient()
        mock_writer = MockWriter()
        adapter = DRStreamingAdapter()

        text, streamed, usage = await adapter.stream_observation_text(
            mock_writer,
            mock_client,
            "system prompt",
            "user prompt",
            "conv-123",
            "turn-456",
            1,
            sub_turn_index=2,
        )

        assert text == "Observation starts after delay. Continued."
        assert streamed is True
        assert usage is None

        types = [event.get("type") for event in mock_writer.events]
        assert types[0] == "observation_start"
        assert types[1] == "observation_delta"
        assert "observation_section_end" in types

    async def test_stream_observation_text_empty_response(self):
        """Verify DR adapter raises error when no streamed text arrives."""
        mock_client = MockLLMClient([])
        mock_writer = MockWriter()
        adapter = DRStreamingAdapter()

        with pytest.raises(Exception) as exc_info:
            await adapter.stream_observation_text(
                mock_writer,
                mock_client,
                "system prompt",
                "user prompt",
                "conv-123",
                "turn-456",
                1,
                sub_turn_index=0,
            )

        assert "empty response" in str(exc_info.value).lower()
        assert mock_writer.has_event_type("observation_section_end")


@pytest.mark.asyncio
class TestSimpleStreamingAdapter:
    """Tests for SimpleStreamingAdapter."""

    async def test_stream_observation_success(self):
        """Verify simple adapter streams observation events successfully."""
        valid_chunks = [
            "Tool executed ",
            "successfully.\n",
            "===DECISION===\n",
            '{"next_action": "finalize", "action_reasoning": "Tool execution complete", "user_goal_achieved": true}',
        ]

        mock_client = MockLLMClient(valid_chunks)
        mock_writer = MockWriter()
        adapter = SimpleStreamingAdapter()

        output, streamed, usage = await adapter.stream_observation(
            mock_writer,
            mock_client,
            "system prompt",
            "user prompt",
            "conv-789",
            "turn-012",
            None,
        )

        assert isinstance(output, PostToolReasoningOutput)
        assert output.next_action == "finalize"
        assert streamed is True
        # Usage is None when using mock client without usage-aware streaming
        assert usage is None
        # Verify events were emitted via writer
        assert mock_writer.has_event_type("observation_start")
        assert mock_writer.has_event_type("observation_delta")
        assert mock_writer.has_event_type("observation_section_end")
        # All events should have identity; sub_turn_index should be absent (None)
        for event in mock_writer.events:
            assert event.get("turn_id") == "turn-012"
            assert event.get("conversation_id") == "conv-789"
            assert "sub_turn_index" not in event

    def test_get_stream_identifiers_simple(self):
        """Verify simple adapter returns identifiers from config."""
        adapter = SimpleStreamingAdapter()
        mock_state = MockInteractiveState("simple_tool_execution")

        config = {"configurable": {
            "canonical_conversation_id": "conv-789",
            "canonical_turn_id": "turn-012",
        }}

        result = adapter.get_stream_identifiers(mock_state, config)

        assert len(result) == 2
        assert result[0] == "conv-789"
        assert result[1] == "turn-012"

    async def test_stream_observation_stops_at_delimiter(self):
        """Verify simple adapter stops streaming deltas at delimiter."""
        chunks_with_delimiter = [
            "Observation text here",
            "===DECISION===",
            '{"next_action": "finalize", "action_reasoning": "Done", "user_goal_achieved": true}',
        ]

        mock_client = MockLLMClient(chunks_with_delimiter)
        mock_writer = MockWriter()
        adapter = SimpleStreamingAdapter()

        try:
            output, streamed, _ = await adapter.stream_observation(
                mock_writer,
                mock_client,
                "system prompt",
                "user prompt",
                "conv-789",
                "turn-012",
                None,
            )
        except Exception:
            pass  # Parsing may fail depending on chunk boundaries

        # Verify "Should not be streamed" content after delimiter was not emitted as delta
        delta_events = mock_writer.get_events_by_type("observation_delta")
        for event in delta_events:
            content = event.get("content", "")
            assert "DECISION" not in content

    async def test_stream_observation_all_events_have_identity(self):
        """Verify all emitted events carry conversation_id and turn_id."""
        valid_chunks = [
            "Result analyzed.\n",
            "===DECISION===\n",
            '{"next_action": "think_more", "action_reasoning": "Need more analysis"}',
        ]

        mock_client = MockLLMClient(valid_chunks)
        mock_writer = MockWriter()
        adapter = SimpleStreamingAdapter()

        await adapter.stream_observation(
            mock_writer, mock_client,
            "sys", "usr", "conv-simple", "turn-simple", 3,
        )

        assert len(mock_writer.events) > 0
        for event in mock_writer.events:
            assert event.get("conversation_id") == "conv-simple"
            assert event.get("turn_id") == "turn-simple"

    async def test_stream_observation_with_sub_turn_index(self):
        """Verify simple adapter passes sub_turn_index when provided."""
        valid_chunks = [
            "Result analyzed.\n",
            "===DECISION===\n",
            '{"next_action": "finalize", "action_reasoning": "Analysis complete and goal achieved", "user_goal_achieved": true}',
        ]

        mock_client = MockLLMClient(valid_chunks)
        mock_writer = MockWriter()
        adapter = SimpleStreamingAdapter()

        await adapter.stream_observation(
            mock_writer, mock_client,
            "sys", "usr", "conv-simple", "turn-simple", 3,
            sub_turn_index=5,
        )

        assert len(mock_writer.events) > 0
        for event in mock_writer.events:
            assert event.get("sub_turn_index") == 5

    async def test_stream_observation_text_success(self):
        """Verify simple adapter streams plain-text observation text only."""
        valid_chunks = [
            "Initial discovery found 2 live hosts.",
            "The response included host metadata and potential DNS entries.",
        ]

        mock_client = MockLLMClient(valid_chunks)
        mock_writer = MockWriter()
        adapter = SimpleStreamingAdapter()

        text, streamed, usage = await adapter.stream_observation_text(
            mock_writer,
            mock_client,
            "system prompt",
            "user prompt",
            "conv-789",
            "turn-012",
            None,
            sub_turn_index=7,
        )

        assert text == "".join(valid_chunks).strip()
        assert streamed is True
        assert usage is None
        assert mock_writer.has_event_type("observation_start")
        assert mock_writer.has_event_type("observation_section_end")
        snapshot_events = mock_writer.get_events_by_type("observation_snapshot")
        assert snapshot_events
        snapshot = snapshot_events[0]
        assert (
            snapshot.get("content")
            or snapshot.get("observation")
            or snapshot.get("message")
            or ""
        ) == text
        for event in mock_writer.events:
            assert event.get("sub_turn_index") == 7
