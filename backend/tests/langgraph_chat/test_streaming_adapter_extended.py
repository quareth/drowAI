"""Contract tests for LangGraph streaming adapter.

Ensures the adapter emits canonical `step_type` and segment index (`ind`)
metadata so front-end cards work across graphs."""

from __future__ import annotations

import os
import pytest
from unittest.mock import Mock, patch

# Set mock DATABASE_URL before any imports
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

from agent.graph.contracts import streaming_constants as stream_consts
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.streaming import build_tool_event_sequence
from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter


@pytest.fixture
def adapter():
    """Create streaming adapter instance."""
    return LangGraphStreamingAdapter()


class TestReasoningEventProcessing:
    """Test reasoning event processing for articulation node events."""
    
    def test_adapter_processes_reasoning_start(self, adapter):
        """Test adapter processes reasoning_start events."""
        event = {
            "type": "reasoning_start",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "step": "tool_intent",
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result is not None
        assert result["type"] == "reasoning_start"
        assert result["metadata"]["step"] == "tool_intent"
        assert result["metadata"]["conversation_id"] == "conv-1"
        assert result["metadata"]["id"] == "turn-1"
        assert result["metadata"]["streaming"] is True
        assert result["metadata"]["source"] == "langgraph_stream"
        assert result["metadata"]["step_type"] == stream_consts.STEP_REASONING_START
        assert result["metadata"]["ind"] == stream_consts.REASONING_PHASE_INDEX
        assert result["metadata"]["phase_sequence"] == 0
        section_id = result["metadata"]["reasoning_section_id"]
        assert isinstance(section_id, str)
        assert section_id.startswith("turn-1:reasoning:")
        assert section_id != "turn-1:reasoning:0"
    
    def test_adapter_processes_reasoning_delta(self, adapter):
        """Test adapter processes reasoning_delta events."""
        start = adapter.process_streaming_event({
            "type": "reasoning_start",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "step": "tool_intent",
        })
        section_id = start["metadata"]["reasoning_section_id"]
        event = {
            "type": "reasoning_delta",
            "content": "I will execute nmap...",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result is not None
        assert result["type"] == "reasoning_delta"
        assert result["metadata"]["subtype"] == "reasoning_delta"
        assert result["content"] == "I will execute nmap..."
        assert result["metadata"]["conversation_id"] == "conv-1"
        assert result["metadata"]["id"] == "turn-1"
        assert result["metadata"]["streaming"] is True
        assert result["metadata"]["step_type"] == stream_consts.STEP_REASONING_DELTA
        assert result["metadata"]["ind"] == stream_consts.REASONING_PHASE_INDEX
        assert result["metadata"]["phase_sequence"] == 0
        assert result["metadata"]["reasoning_section_id"] == section_id
    
    def test_adapter_rejects_reasoning_delta_without_content(self, adapter):
        """Test adapter rejects reasoning_delta without content."""
        event = {
            "type": "reasoning_delta",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            # Missing content field
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result is None
    
    def test_adapter_processes_reasoning_section_end(self, adapter):
        """Test adapter processes reasoning_section_end events."""
        start = adapter.process_streaming_event({
            "type": "reasoning_start",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "step": "tool_intent",
        })
        section_id = start["metadata"]["reasoning_section_id"]
        event = {
            "type": "reasoning_section_end",
            "section_name": "tool_intent",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result is not None
        assert result["type"] == "reasoning_section_end"
        assert result["metadata"]["section_name"] == "tool_intent"
        assert result["metadata"]["streaming"] is False
        assert "Reasoning complete" in result["content"]
        assert result["metadata"]["step_type"] == stream_consts.STEP_REASONING_SECTION_END
        assert result["metadata"]["ind"] == stream_consts.REASONING_PHASE_INDEX
        assert result["metadata"]["phase_sequence"] == 0
        assert result["metadata"]["reasoning_section_id"] == section_id

    def test_reasoning_section_identity_stays_unique_across_container_boundaries(self, adapter):
        """Reasoning identity must not reuse phase-derived ids across runtime paths."""
        container = ChatStateContainer()
        first_start = adapter.process_streaming_event(
            {
                "type": "reasoning_start",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "step": "intent",
            },
            state_container=container,
        )
        adapter.process_streaming_event(
            {
                "type": "reasoning_delta",
                "content": "first",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
            },
            state_container=container,
        )
        adapter.process_streaming_event(
            {
                "type": "reasoning_section_end",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "section_name": "intent",
            },
            state_container=container,
        )

        second_start = adapter.process_streaming_event(
            {
                "type": "reasoning_start",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "step": "tool_category_selection",
            }
        )

        assert first_start["metadata"]["phase_sequence"] == 0
        assert second_start["metadata"]["phase_sequence"] == 1
        assert (
            first_start["metadata"]["reasoning_section_id"]
            != second_start["metadata"]["reasoning_section_id"]
        )

    def test_reasoning_snapshot_after_section_end_reuses_closed_identity(self, adapter):
        """Final reasoning snapshots belong to the section that just closed."""
        start = adapter.process_streaming_event(
            {
                "type": "reasoning_start",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "step": "tool_intent",
            }
        )
        section_id = start["metadata"]["reasoning_section_id"]
        adapter.process_streaming_event(
            {
                "type": "reasoning_delta",
                "content": "streamed",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
            }
        )
        adapter.process_streaming_event(
            {
                "type": "reasoning_section_end",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "section_name": "tool_intent",
            }
        )

        snapshot = adapter.process_streaming_event(
            {
                "type": "reasoning_delta",
                "content": "streamed",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "step": "tool_intent",
                "snapshot": True,
            }
        )

        assert snapshot["metadata"]["reasoning_section_id"] == section_id
        assert snapshot["metadata"]["phase_sequence"] == start["metadata"]["phase_sequence"]
        assert snapshot["metadata"]["snapshot"] is True

    def test_reasoning_snapshot_after_section_end_does_not_duplicate_container_text(self, adapter):
        """Snapshot replay should not append a second copy to persisted reasoning."""
        container = ChatStateContainer()
        adapter.process_streaming_event(
            {
                "type": "reasoning_start",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "step": "tool_intent",
            },
            state_container=container,
        )
        adapter.process_streaming_event(
            {
                "type": "reasoning_delta",
                "content": "streamed",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
            },
            state_container=container,
        )
        adapter.process_streaming_event(
            {
                "type": "reasoning_section_end",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "section_name": "tool_intent",
            },
            state_container=container,
        )

        adapter.process_streaming_event(
            {
                "type": "reasoning_delta",
                "content": "streamed",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "step": "tool_intent",
                "snapshot": True,
            },
            state_container=container,
        )

        assert [section["content"] for section in container.get_reasoning_sections()] == ["streamed"]

    def test_non_snapshot_reasoning_delta_after_section_end_still_fails(self, adapter):
        """Only explicit final snapshots may use the last closed section identity."""
        adapter.process_streaming_event(
            {
                "type": "reasoning_start",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "step": "tool_intent",
            }
        )
        adapter.process_streaming_event(
            {
                "type": "reasoning_section_end",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "section_name": "tool_intent",
            }
        )

        with pytest.raises(ValueError, match="missing active reasoning_section_id"):
            adapter.process_streaming_event(
                {
                    "type": "reasoning_delta",
                    "content": "late chunk",
                    "conversation_id": "conv-1",
                    "turn_id": "turn-1",
                }
            )
    
    def test_reasoning_delta_increments_metric(self, adapter):
        """Test reasoning_delta increments processing metric."""
        adapter.process_streaming_event({
            "type": "reasoning_start",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "step": "thinking",
        })
        event = {
            "type": "reasoning_delta",
            "content": "Test content",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
        }
        
        with patch("backend.services.langgraph_chat.streaming.adapter.safe_inc") as mock_inc:
            result = adapter.process_streaming_event(event)
            
            # Verify metric was incremented
            mock_inc.assert_called_with("langgraph_reasoning_deltas_processed")
    
    def test_reasoning_start_increments_metric(self, adapter):
        """Test reasoning_start increments processing metric."""
        event = {
            "type": "reasoning_start",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "step": "thinking",
        }
        
        with patch("backend.services.langgraph_chat.streaming.adapter.safe_inc") as mock_inc:
            result = adapter.process_streaming_event(event)
            
            # Verify metric was incremented
            mock_inc.assert_called_with("langgraph_reasoning_starts_processed")


class TestToolEventProcessing:
    """Test tool event processing (Phase 1.3 executor node events)."""
    
    def test_adapter_processes_tool_start(self, adapter):
        """Test adapter processes tool_start events."""
        event = {
            "type": "tool_start",
            "tool": "nmap",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "parameters": {"ports": "80,443"},
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result is not None
        assert result["type"] == "tool_start"
        assert result["metadata"]["tool"] == "nmap"
        assert result["metadata"]["parameters"] == {"ports": "80,443"}
        assert result["metadata"]["conversation_id"] == "conv-1"
        assert result["metadata"]["id"] == "turn-1"
        assert result["metadata"]["streaming"] is True
        assert result["metadata"]["step_type"] == stream_consts.STEP_TOOL_START
        assert result["metadata"]["ind"] == stream_consts.TOOL_PHASE_INDEX
        assert "Executing nmap" in result["content"]

    def test_adapter_processes_tool_batch_start(self, adapter):
        event = {
            "type": "tool_batch_start",
            "tool_batch_id": "tb_1",
            "execution_strategy": "sequential",
            "requested_execution_strategy": "parallel",
            "tool_batch_total": 2,
            "calls": [
                {"tool_call_id": "tc_1", "tool": "tool.a"},
                {"tool_call_id": "tc_2", "tool": "tool.b"},
            ],
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
        }

        result = adapter.process_streaming_event(event)

        assert result is not None
        assert result["type"] == "tool_batch_start"
        assert result["metadata"]["tool_batch_id"] == "tb_1"
        assert result["metadata"]["tool_calls"][0]["tool_call_id"] == "tc_1"
        assert result["metadata"]["step_type"] == "tool_batch_start"
        assert result["metadata"]["ind"] == stream_consts.TOOL_PHASE_INDEX
    
    def test_adapter_processes_tool_delta(self, adapter):
        """Compact-only mode suppresses tool_delta events."""
        event = {
            "type": "tool_delta",
            "tool": "nmap",
            "content": "Starting Nmap 7.95...",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result is None

    def test_adapter_filters_tool_delta_when_compact_mode_enabled(self, adapter):
        """Compact-only mode should suppress raw tool_delta streaming payloads."""
        event = {
            "type": "tool_delta",
            "tool": "nmap",
            "content": "Starting Nmap 7.95...",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
        }

        result = adapter.process_streaming_event(event)

        assert result is None

    def test_adapter_forwards_tool_batch_id_on_tool_events(self, adapter):
        start = adapter.process_streaming_event(
            {
                "type": "tool_start",
                "tool": "tool.a",
                "tool_call_id": "tc_1",
                "tool_batch_id": "tb_1",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "parameters": {},
            }
        )
        state_container = Mock()
        state_container.get_tool_call_parameters.return_value = None
        state_container.add_tool_call.side_effect = lambda payload: payload
        state_container.reserved_message_id = None
        end = adapter.process_streaming_event(
            {
                "type": "tool_end",
                "tool": "tool.a",
                "tool_call_id": "tc_1",
                "tool_batch_id": "tb_1",
                "conversation_id": "conv-1",
                "turn_id": "turn-1",
                "status": "success",
            },
            state_container=state_container,
        )

        assert start["metadata"]["tool_batch_id"] == "tb_1"
        assert end["metadata"]["tool_batch_id"] == "tb_1"
        persisted = state_container.add_tool_call.call_args[0][0]
        assert persisted["tool_batch_id"] == "tb_1"

    def test_adapter_processes_tool_batch_end(self, adapter):
        event = {
            "type": "tool_batch_end",
            "tool_batch_id": "tb_1",
            "execution_strategy": "sequential",
            "status": "completed_with_errors",
            "completed": 1,
            "failed": 1,
            "results": [
                {"tool_call_id": "tc_1", "tool": "tool.a", "status": "success"},
                {"tool_call_id": "tc_2", "tool": "tool.b", "status": "failed"},
            ],
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
        }

        result = adapter.process_streaming_event(event)

        assert result is not None
        assert result["type"] == "tool_batch_end"
        assert result["metadata"]["tool_batch_id"] == "tb_1"
        assert result["metadata"]["status"] == "completed_with_errors"
        assert result["metadata"]["results"][1]["tool_call_id"] == "tc_2"
    
    def test_adapter_processes_tool_end(self, adapter):
        """Test adapter processes tool_end events."""
        event = {
            "type": "tool_end",
            "tool": "nmap",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "status": "success",
            "duration": 5.2,
            "exit_code": 0,
            "summary": {
                "summary": "Open ports discovered.",
                "key_findings": ["5432/tcp open postgresql"],
            },
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result is not None
        assert result["type"] == "tool_end"
        assert result["metadata"]["tool"] == "nmap"
        assert result["metadata"]["status"] == "success"
        assert result["metadata"]["duration"] == 5.2
        assert result["metadata"]["exit_code"] == 0
        assert result["metadata"]["summary"]["summary"] == "Open ports discovered."
        assert result["metadata"]["step_type"] == stream_consts.STEP_TOOL_END
        assert result["metadata"]["ind"] == stream_consts.TOOL_PHASE_INDEX
        assert result["metadata"]["streaming"] is False
    
    def test_adapter_processes_tool_end_with_error(self, adapter):
        """Test adapter processes tool_end with error."""
        event = {
            "type": "tool_end",
            "tool": "nmap",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "status": "error",
            "duration": 0.5,
            "error": "Network unreachable",
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result is not None
        assert result["type"] == "tool_end"
        assert result["metadata"]["status"] == "error"
        assert result["metadata"]["error"] == "Network unreachable"

    def test_tool_lifecycle_contract_for_raw_output_lookup(self, adapter):
        """tool_call_id/status/task_id metadata must be available for raw lookup flow."""
        tool_call_id = "call-contract-1"

        start_event = {
            "type": "tool_start",
            "tool": "nmap",
            "tool_call_id": tool_call_id,
            "task_id": 42,
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "parameters": {"ports": "80"},
        }
        start_result = adapter.process_streaming_event(start_event)
        assert start_result is not None
        assert start_result["metadata"]["tool_call_id"] == tool_call_id
        assert start_result["metadata"]["task_id"] == 42

        end_event = {
            "type": "tool_end",
            "tool": "nmap",
            "tool_call_id": tool_call_id,
            "task_id": 42,
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "status": "failed",
            "duration": 0.3,
        }
        end_result = adapter.process_streaming_event(end_event)
        assert end_result is not None
        assert end_result["metadata"]["tool_call_id"] == tool_call_id
        assert end_result["metadata"]["task_id"] == 42
        # status should be passthrough/stable for deterministic client mapping
        assert end_result["metadata"]["status"] == "failed"

    def test_tool_end_persists_compact_tool_result_in_compact_mode(self, adapter):
        """Persist compact envelope to state container tool_result when enabled."""
        compact_payload = {
            "schema_version": "2.0",
            "tool": "nmap",
            "status": "success",
            "success": True,
            "exit_code": 0,
            "summary": "Open ports found",
            "key_findings": ["80/tcp open"],
            "errors": [],
            "report_recommendations": ["Investigate open SSH"],
            "structured_signals": [{"type": "service", "port": 80, "service": "http"}],
            "decision_evidence": [],
            "lossiness_risk": "low",
            "artifact_refs": [],
            "compression": {"source": "llm"},
        }
        event = {
            "type": "tool_end",
            "tool": "nmap",
            "tool_call_id": "call-1",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "status": "success",
            "duration": 1.2,
            "exit_code": 0,
            "summary": {"summary": "Open ports found"},
            "compact_tool_result": compact_payload,
        }
        state_container = Mock()
        state_container.get_tool_call_parameters.return_value = None
        state_container.add_tool_call.side_effect = lambda payload: payload
        state_container.reserved_message_id = None

        result = adapter.process_streaming_event(event, state_container=state_container)

        assert result is not None
        assert result["metadata"]["compact_tool_result"]["schema_version"] == "2.0"
        persisted = state_container.add_tool_call.call_args[0][0]
        assert persisted["tool_result"]["schema_version"] == "2.0"
        assert persisted["tool_result"]["summary"] == "Open ports found"

    def test_tool_end_persists_stream_sub_turn_index_as_turn_index(self, adapter):
        """Tool persistence must use stream sub_turn_index for stable replay ordering."""
        event = {
            "type": "tool_end",
            "tool": "nmap",
            "tool_call_id": "call-turn-idx",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "status": "success",
            "duration": 0.2,
            "sub_turn_index": 2,
        }
        state_container = Mock()
        state_container.get_tool_call_parameters.return_value = None
        state_container.add_tool_call.side_effect = lambda payload: payload
        state_container.reserved_message_id = None

        result = adapter.process_streaming_event(event, state_container=state_container)

        assert result is not None
        persisted = state_container.add_tool_call.call_args[0][0]
        assert persisted["turn_index"] == 2

    def test_tool_end_persists_snapshot_immediately_when_reserved_message_id_and_tool_call_id_exist(
        self,
        adapter,
    ):
        """Snapshot persistence should run immediately after tool state is stored."""
        event = {
            "type": "tool_end",
            "tool": "nmap",
            "tool_call_id": "call-snapshot-1",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "status": "success",
            "duration": 0.2,
            "parameters": {"target": "127.0.0.1"},
        }
        state_container = Mock()
        state_container.get_tool_call_parameters.return_value = None
        state_container.reserved_message_id = 101

        def _store(payload):
            stored = dict(payload)
            stored["persisted_marker"] = True
            return stored

        state_container.add_tool_call.side_effect = _store

        with patch.object(adapter._tool_call_snapshot_service, "persist_snapshot") as mock_persist:
            result = adapter.process_streaming_event(event, state_container=state_container)

        assert result is not None
        mock_persist.assert_called_once()
        assert mock_persist.call_args.kwargs["reserved_message_id"] == 101
        assert mock_persist.call_args.kwargs["tool_call_info"]["persisted_marker"] is True

    @pytest.mark.parametrize(
        ("reserved_message_id", "tool_call_id"),
        [
            (None, "call-snapshot-1"),
            ("101", "call-snapshot-1"),
            (101, None),
        ],
    )
    def test_tool_end_skips_snapshot_persistence_without_required_identifiers(
        self,
        adapter,
        reserved_message_id,
        tool_call_id,
    ):
        """Snapshot persistence must stay gated on integer message id plus tool_call_id."""
        event = {
            "type": "tool_end",
            "tool": "nmap",
            "tool_call_id": tool_call_id,
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "status": "success",
            "duration": 0.2,
        }
        state_container = Mock()
        state_container.get_tool_call_parameters.return_value = None
        state_container.add_tool_call.side_effect = lambda payload: payload
        state_container.reserved_message_id = reserved_message_id

        with patch.object(adapter._tool_call_snapshot_service, "persist_snapshot") as mock_persist:
            result = adapter.process_streaming_event(event, state_container=state_container)

        assert result is not None
        mock_persist.assert_not_called()
    
    def test_tool_start_increments_metric(self, adapter):
        """Test tool_start increments processing metric."""
        event = {
            "type": "tool_start",
            "tool": "nmap",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "parameters": {},
        }
        
        with patch("backend.services.langgraph_chat.streaming.adapter.safe_inc") as mock_inc:
            result = adapter.process_streaming_event(event)
            
            # Verify metric was incremented
            mock_inc.assert_called_with("langgraph_tool_starts_processed")


    def test_tool_end_increments_metric(self, adapter):
        """Test tool_end increments processing metric."""
        event = {
            "type": "tool_end",
            "tool": "nmap",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "status": "success",
            "duration": 2.0,
        }
        
        with patch("backend.services.langgraph_chat.streaming.adapter.safe_inc") as mock_inc:
            result = adapter.process_streaming_event(event)
            
            # Verify metric was incremented
            mock_inc.assert_called_with("langgraph_tool_ends_processed")

    def test_build_tool_event_sequence_has_contract_metadata(self):
        """Synthetic tool events should carry step_type/ind like live events."""
        summary = {"status": "success", "summary": "done", "key_findings": ["host reachable"]}
        events = build_tool_event_sequence(
            "nmap",
            summary,
            conversation_id="conv-1",
            turn_id="turn-1",
        )
        assert len(events) == 3
        start_event, delta_event, end_event = events
        assert start_event["metadata"]["step_type"] == stream_consts.STEP_TOOL_START
        assert start_event["metadata"]["ind"] == stream_consts.TOOL_PHASE_INDEX
        assert delta_event["metadata"]["step_type"] == stream_consts.STEP_TOOL_DELTA
        assert delta_event["metadata"]["ind"] == stream_consts.TOOL_PHASE_INDEX
        assert end_event["metadata"]["step_type"] == stream_consts.STEP_TOOL_END
        assert end_event["metadata"]["ind"] == stream_consts.TOOL_PHASE_INDEX


class TestEventSchemaCompatibility:
    """Test events are compatible with SSE schema."""
    
    def test_all_reasoning_events_have_required_fields(self, adapter):
        """Test reasoning events include required SSE fields."""
        events = [
            {"type": "reasoning_start", "conversation_id": "c", "turn_id": "t", "step": "s"},
            {"type": "reasoning_delta", "content": "text", "conversation_id": "c", "turn_id": "t"},
            {"type": "reasoning_section_end", "section_name": "s", "conversation_id": "c", "turn_id": "t"},
        ]
        
        for event in events:
            result = adapter.process_streaming_event(event)
            
            assert result is not None
            assert "type" in result
            assert "metadata" in result
            assert "source" in result["metadata"]
            assert "timestamp" in result["metadata"]
            assert "phase_sequence" in result["metadata"]
            assert "reasoning_section_id" in result["metadata"]
    
    def test_all_tool_events_have_required_fields(self, adapter):
        """Test tool events include required SSE fields."""
        events = [
            {"type": "tool_start", "tool": "t", "conversation_id": "c", "turn_id": "i", "parameters": {}},
            {"type": "tool_end", "tool": "t", "conversation_id": "c", "turn_id": "i", "status": "success", "duration": 1.0},
        ]
        
        for event in events:
            result = adapter.process_streaming_event(event)
            
            assert result is not None
            assert "type" in result
            assert "metadata" in result
            assert "source" in result["metadata"]
            assert "timestamp" in result["metadata"]
    
    def test_events_include_conversation_context(self, adapter):
        """Test events preserve conversation context."""
        event = {
            "type": "tool_start",
            "tool": "nmap",
            "conversation_id": "conv-123",
            "turn_id": "turn-456",
            "parameters": {},
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result["metadata"]["conversation_id"] == "conv-123"
        assert result["metadata"]["conversationId"] == "conv-123"  # Both formats
        assert result["metadata"]["id"] == "turn-456"

    def test_adapter_processes_retry_attempt_as_reasoning_phase(self, adapter):
        """retry_attempt is retry progress and must not be tool phase."""
        event = {
            "type": "retry_attempt",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "attempt": 2,
            "alternative_tool": "nmap",
            "reasoning": "Switching strategy after timeout",
        }

        result = adapter.process_streaming_event(event)

        assert result is not None
        assert result["type"] == "retry_attempt"
        assert result["metadata"]["step_type"] == stream_consts.STEP_RETRY_ATTEMPT
        assert result["metadata"]["ind"] == stream_consts.REASONING_PHASE_INDEX


class TestEventValidation:
    """Test event validation and error handling."""
    
    def test_adapter_handles_missing_event_type(self, adapter):
        """Test adapter handles events without type field."""
        event = {
            "content": "Some content",
            "conversation_id": "conv-1",
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result is None
    
    def test_adapter_handles_unknown_event_type(self, adapter):
        """Test adapter handles unknown event types gracefully."""
        event = {
            "type": "unknown_event_type",
            "content": "Some content",
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result is None
    
    def test_adapter_validates_required_fields(self, adapter):
        """Test adapter validates required fields per event type."""
        # reasoning_delta requires content
        event = {
            "type": "reasoning_delta",
            "conversation_id": "conv-1",
            # Missing content
        }
        
        result = adapter.process_streaming_event(event)
        assert result is None
    
    def test_adapter_provides_defaults_for_optional_fields(self, adapter):
        """Test adapter provides sensible defaults for optional fields."""
        event = {
            "type": "tool_start",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            # Missing tool name and parameters
        }
        
        result = adapter.process_streaming_event(event)
        
        assert result is not None
        assert result["metadata"]["tool"] == "unknown"
        assert result["metadata"]["parameters"] == {}


class TestEndToEndEventFlow:
    """Test complete event flow through adapter."""
    
    def test_complete_reasoning_flow(self, adapter):
        """Test complete reasoning event sequence."""
        events = [
            {"type": "reasoning_start", "conversation_id": "c", "turn_id": "t", "step": "tool_intent"},
            {"type": "reasoning_delta", "content": "To scan ", "conversation_id": "c", "turn_id": "t"},
            {"type": "reasoning_delta", "content": "the network, ", "conversation_id": "c", "turn_id": "t"},
            {"type": "reasoning_delta", "content": "I will execute nmap.", "conversation_id": "c", "turn_id": "t"},
            {"type": "reasoning_section_end", "section_name": "tool_intent", "conversation_id": "c", "turn_id": "t"},
        ]
        
        results = []
        for event in events:
            result = adapter.process_streaming_event(event)
            if result:
                results.append(result)
        
        assert len(results) == 5
        assert results[0]["type"] == "reasoning_start"
        assert results[1]["type"] == "reasoning_delta"
        assert results[1]["metadata"]["subtype"] == "reasoning_delta"
        assert results[-1]["type"] == "reasoning_section_end"
    
    def test_complete_tool_execution_flow(self, adapter):
        """Test complete tool execution event sequence."""
        events = [
            {"type": "tool_start", "tool": "nmap", "conversation_id": "c", "turn_id": "t", "parameters": {"ports": "80"}},
            {"type": "tool_delta", "tool": "nmap", "content": "Starting Nmap...\n", "conversation_id": "c", "turn_id": "t"},
            {"type": "tool_delta", "tool": "nmap", "content": "PORT 80/tcp open\n", "conversation_id": "c", "turn_id": "t"},
            {"type": "tool_end", "tool": "nmap", "conversation_id": "c", "turn_id": "t", "status": "success", "duration": 2.5},
        ]
        
        results = []
        for event in events:
            result = adapter.process_streaming_event(event)
            if result:
                results.append(result)
        
        assert len(results) == 2
        assert results[0]["type"] == "tool_start"
        assert results[-1]["type"] == "tool_end"
        assert results[-1]["metadata"]["duration"] == 2.5


class TestCommonMetadataEnrichment:
    """Test centralized common metadata forwarding."""

    @pytest.mark.parametrize(
        ("event", "expected_type"),
        [
            (
                {
                    "type": "message_start",
                    "conversation_id": "conv-1",
                    "turn_id": "turn-1",
                    "sub_turn_index": 7,
                },
                "message_start",
            ),
            (
                {
                    "type": "section_end",
                    "conversation_id": "conv-1",
                    "turn_id": "turn-1",
                    "section_name": "final_answer",
                    "sub_turn_index": 7,
                },
                "section_end",
            ),
            (
                {
                    "type": "reasoning_start",
                    "conversation_id": "conv-1",
                    "turn_id": "turn-1",
                    "step": "thinking",
                    "sub_turn_index": 7,
                },
                "reasoning_start",
            ),
            (
                {
                    "type": "tool_start",
                    "tool": "nmap",
                    "conversation_id": "conv-1",
                    "turn_id": "turn-1",
                    "parameters": {},
                    "sub_turn_index": 7,
                },
                "tool_start",
            ),
            (
                {
                    "type": "observation_start",
                    "conversation_id": "conv-1",
                    "turn_id": "turn-1",
                    "step": "observing",
                    "sub_turn_index": 7,
                },
                "observation_start",
            ),
            (
                {
                    "type": "retry_start",
                    "conversation_id": "conv-1",
                    "turn_id": "turn-1",
                    "attempt": 1,
                    "max_attempts": 3,
                    "sub_turn_index": 7,
                },
                "retry_start",
            ),
            (
                {
                    "type": "plan_created",
                    "conversation_id": "conv-1",
                    "turn_id": "turn-1",
                    "sub_turn_index": 7,
                },
                "plan_created",
            ),
            (
                {
                    "type": "todo_progress",
                    "conversation_id": "conv-1",
                    "turn_id": "turn-1",
                    "sub_turn_index": 7,
                },
                "todo_progress",
            ),
        ],
    )
    def test_forwards_sub_turn_index_for_all_event_families(self, adapter, event, expected_type):
        """sub_turn_index should be forwarded centrally, regardless of event type."""
        result = adapter.process_streaming_event(event)

        assert result is not None
        assert result["type"] == expected_type
        assert result["metadata"].get("sub_turn_index") == 7

    def test_forwards_sub_turn_index_from_raw_metadata_when_top_level_missing(self, adapter):
        """Fallback to raw event metadata when top-level sub_turn_index is absent."""
        adapter.process_streaming_event({
            "type": "reasoning_start",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "metadata": {"sub_turn_index": 3},
        })
        event = {
            "type": "reasoning_delta",
            "content": "analysis chunk",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "metadata": {"sub_turn_index": 3},
        }

        result = adapter.process_streaming_event(event)

        assert result is not None
        assert result["metadata"].get("sub_turn_index") == 3

    def test_forwards_turn_sequence_sub_turn_index_and_task_id_unchanged(self, adapter):
        """Forwarded routing metadata must preserve the original values exactly."""
        event = {
            "type": "message_delta",
            "content": "hello",
            "conversation_id": "conv-1",
            "turn_id": "turn-1",
            "turn_sequence": 11,
            "sub_turn_index": 4,
            "task_id": 42,
        }

        result = adapter.process_streaming_event(event)

        assert result is not None
        metadata = result["metadata"]
        assert metadata["turn_sequence"] == 11
        assert metadata["sub_turn_index"] == 4
        assert metadata["task_id"] == 42


class TestTurnOutcomeCompatibility:
    """Test compatibility helpers that remain public after extraction."""

    def test_build_final_event_preserves_assistant_contract_metadata(self, adapter):
        """Final assistant event must keep the established sentinel metadata."""
        state = InteractiveState(
            facts=FactsState(
                task_id=1,
                message="hello",
                conversation_id="conv-1",
                metadata={},
            ),
            trace=TraceState(final_text="done"),
        )

        event = adapter.build_final_event(state, turn_id="turn-final")

        assert event["type"] == "assistant_final"
        assert event["content"] == "done"
        assert event["metadata"]["role"] == "assistant"
        assert event["metadata"]["subtype"] == "assistant_final"
        assert event["metadata"]["internal_only"] is True
        assert event["metadata"]["step_type"] == stream_consts.STEP_ASSISTANT_MESSAGE
        assert event["metadata"]["ind"] == stream_consts.ANSWER_PHASE_INDEX

    def test_build_simple_chat_events_remains_single_final_event(self, adapter):
        """Compatibility helper should stay a thin wrapper over build_final_event."""
        state = InteractiveState(
            facts=FactsState(
                task_id=2,
                message="hello",
                conversation_id="conv-2",
                metadata={},
            ),
            trace=TraceState(final_text="done"),
        )

        events = adapter.build_simple_chat_events(state, turn_id="turn-simple")

        assert events == [adapter.build_final_event(state, turn_id="turn-simple")]


__all__ = [
    "TestReasoningEventProcessing",
    "TestToolEventProcessing",
    "TestEventSchemaCompatibility",
    "TestEventValidation",
    "TestEndToEndEventFlow",
    "TestCommonMetadataEnrichment",
    "TestTurnOutcomeCompatibility",
]
