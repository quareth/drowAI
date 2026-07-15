"""Unit tests for UnifiedEventEmitter, EventMetadata, SimpleEmitter, and DeepReasoningEmitter.

Covers metadata completeness for all event types, index correctness,
thread safety (concurrent emission), EventMetadata validation,
and SimpleEmitter vs DeepReasoningEmitter behavior."""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

from agent.graph.contracts.streaming_constants import (
    ANSWER_PHASE_INDEX,
    OBSERVATION_PHASE_INDEX,
    REASONING_PHASE_INDEX,
    TOOL_PHASE_INDEX,
)
from agent.graph.emission.unified_emitter import (
    DeepReasoningEmitter,
    EventMetadata,
    SimpleEmitter,
    UnifiedEventEmitter,
)
from agent.graph.infrastructure.state_models import FactsState, GraphRuntimeContext
from agent.graph.state import InteractiveState, TraceState


# --- Helpers ---


def make_writer(events: List[Dict[str, Any]]) -> Any:
    """Append each emitted event to events list."""
    def writer(event: Dict[str, Any]) -> None:
        events.append(dict(event))
    return writer


def make_state(
    conversation_id: str = "conv-1",
    task_id: int = 42,
    capability: str | None = None,
    metadata: Dict[str, Any] | None = None,
) -> InteractiveState:
    facts = FactsState(
        task_id=task_id,
        message="test",
        conversation_id=conversation_id,
        capability=capability,
        metadata=metadata or {},
    )
    return InteractiveState(facts=facts, trace=TraceState())


def assert_metadata_complete(ev: Dict[str, Any], ind: int, step_type: str) -> None:
    assert "ind" in ev, f"Missing ind in {ev}"
    assert ev["ind"] == ind, f"Expected ind={ind}, got {ev.get('ind')}"
    assert "step_type" in ev, f"Missing step_type in {ev}"
    assert ev["step_type"] == step_type
    assert "conversation_id" in ev
    assert "turn_id" in ev
    assert "conversationId" in ev
    assert ev["conversationId"] == ev["conversation_id"]
    assert ev.get("id") == ev.get("turn_id")


# --- EventMetadata ---


class TestEventMetadata:
    """EventMetadata dataclass and validation."""

    def test_to_dict_includes_required_fields(self) -> None:
        meta = EventMetadata(
            ind=ANSWER_PHASE_INDEX,
            step_type="message_delta",
            conversation_id="c1",
            turn_id="t1",
            sequence=99,
            turn_sequence=1,
            streaming=True,
        )
        d = meta.to_dict()
        assert d["ind"] == 2
        assert d["step_type"] == "message_delta"
        assert d["conversation_id"] == "c1"
        assert d["conversationId"] == "c1"
        assert d["turn_id"] == "t1"
        assert d["id"] == "t1"
        assert d["sequence"] == 99
        assert d["turn_sequence"] == 1
        assert d["streaming"] is True

    def test_to_dict_optional_sequence_omitted(self) -> None:
        meta = EventMetadata(
            ind=REASONING_PHASE_INDEX,
            step_type="reasoning_start",
            conversation_id="c1",
            turn_id="t1",
            sequence=None,
            turn_sequence=None,
            streaming=True,
        )
        d = meta.to_dict()
        assert "sequence" not in d
        assert "turn_sequence" not in d
        assert "sub_turn_index" not in d

    def test_to_dict_includes_sub_turn_index_when_set(self) -> None:
        meta = EventMetadata(
            ind=OBSERVATION_PHASE_INDEX,
            step_type="observation_start",
            conversation_id="c1",
            turn_id="t1",
            sequence=None,
            turn_sequence=1,
            streaming=True,
            sub_turn_index=2,
        )
        d = meta.to_dict()
        assert d["sub_turn_index"] == 2

    def test_to_dict_omits_sub_turn_index_when_none(self) -> None:
        meta = EventMetadata(
            ind=OBSERVATION_PHASE_INDEX,
            step_type="observation_start",
            conversation_id="c1",
            turn_id="t1",
            sequence=None,
            turn_sequence=1,
            streaming=True,
            sub_turn_index=None,
        )
        d = meta.to_dict()
        assert "sub_turn_index" not in d

    def test_validate_accepts_valid_metadata(self) -> None:
        meta = EventMetadata(
            ind=0, step_type="reasoning_start",
            conversation_id="c", turn_id="t",
            sequence=None, turn_sequence=None, streaming=True,
        )
        assert meta.validate() is True

    def test_validate_rejects_invalid_ind(self) -> None:
        meta = EventMetadata(
            ind=-1, step_type="x",
            conversation_id="c", turn_id="t",
            sequence=None, turn_sequence=None, streaming=True,
        )
        assert meta.validate() is False
        meta2 = EventMetadata(
            ind=4, step_type="x",
            conversation_id="c", turn_id="t",
            sequence=None, turn_sequence=None, streaming=True,
        )
        assert meta2.validate() is False

    def test_validate_rejects_empty_step_type(self) -> None:
        meta = EventMetadata(
            ind=0, step_type="",
            conversation_id="c", turn_id="t",
            sequence=None, turn_sequence=None, streaming=True,
        )
        assert meta.validate() is False


# --- UnifiedEventEmitter (via SimpleEmitter with fixed ids) ---


class TestUnifiedEmitterMetadataCompleteness:
    """All event types include complete metadata and correct phase index."""

    def test_reasoning_events_ind_0(self) -> None:
        events: List[Dict[str, Any]] = []
        writer = make_writer(events)
        emitter = SimpleEmitter(writer, make_state(), None, None)
        emitter.emit_reasoning_start(step="thinking")
        emitter.emit_reasoning_delta("chunk")
        emitter.emit_reasoning_section_end(section_name="thinking")
        assert len(events) == 3
        for ev in events:
            assert_metadata_complete(ev, REASONING_PHASE_INDEX, ev["step_type"])
        assert events[0]["type"] == "reasoning_start"
        assert events[1]["type"] == "reasoning_delta"
        assert events[2]["type"] == "reasoning_section_end"

    def test_tool_events_ind_1(self) -> None:
        events = []
        emitter = SimpleEmitter(make_writer(events), make_state(), None, None)
        emitter.emit_tool_start("nmap", {"target": "127.0.0.1"})
        emitter.emit_tool_delta("nmap", "output")
        emitter.emit_tool_end("nmap", "success", 1.0, {"lines": 10})
        assert len(events) == 3
        for ev in events:
            assert_metadata_complete(ev, TOOL_PHASE_INDEX, ev["step_type"])
        assert events[0]["tool"] == "nmap"
        assert events[1]["content"] == "output"
        assert events[2]["status"] == "success"

    def test_message_events_ind_2(self) -> None:
        events = []
        emitter = SimpleEmitter(make_writer(events), make_state(), None, None)
        emitter.emit_message_start()
        emitter.emit_message_delta("Hello", extra_fields={"run_id": 1})
        emitter.emit_section_end("final_answer")
        assert len(events) == 3
        for ev in events:
            assert ev["ind"] == ANSWER_PHASE_INDEX
        assert events[1].get("run_id") == 1

    def test_observation_events_ind_3(self) -> None:
        events = []
        emitter = SimpleEmitter(make_writer(events), make_state(), None, None)
        emitter.emit_observation_start(step="observing")
        emitter.emit_observation_delta("summary text")
        emitter.emit_observation_section_end(section_name="observing")
        assert len(events) == 3
        for ev in events:
            assert_metadata_complete(ev, OBSERVATION_PHASE_INDEX, ev["step_type"])

    def test_retry_events_phase_indices(self) -> None:
        events = []
        emitter = SimpleEmitter(make_writer(events), make_state(), None, None)
        emitter.emit_retry_start(1, 3, failure_category="timeout")
        emitter.emit_retry_attempt(1, alternative_tool="nmap", reasoning="retry")
        assert len(events) == 2
        assert events[0]["ind"] == REASONING_PHASE_INDEX
        assert events[1]["ind"] == REASONING_PHASE_INDEX
        assert events[0]["failure_category"] == "timeout"
        assert events[1]["alternative_tool"] == "nmap"


# --- Phase index constants ---


class TestPhaseIndexCorrectness:
    """Phase indices: reasoning=0, tool=1, answer=2, observation=3."""

    def test_reasoning_phase_index_0(self) -> None:
        events = []
        emitter = SimpleEmitter(make_writer(events), make_state(), None, None)
        emitter.emit_reasoning_delta("x")
        assert events[0]["ind"] == 0

    def test_tool_phase_index_1(self) -> None:
        events = []
        emitter = SimpleEmitter(make_writer(events), make_state(), None, None)
        emitter.emit_tool_start("t", {})
        assert events[0]["ind"] == 1

    def test_answer_phase_index_2(self) -> None:
        events = []
        emitter = SimpleEmitter(make_writer(events), make_state(), None, None)
        emitter.emit_message_delta("x")
        assert events[0]["ind"] == 2

    def test_observation_phase_index_3(self) -> None:
        events = []
        emitter = SimpleEmitter(make_writer(events), make_state(), None, None)
        emitter.emit_observation_delta("x")
        assert events[0]["ind"] == 3


# --- Thread safety ---


class TestThreadSafety:
    """Concurrent emission does not corrupt events."""

    def test_concurrent_emission_serialized(self) -> None:
        events: List[Dict[str, Any]] = []
        lock = threading.Lock()

        def writer(ev: Dict[str, Any]) -> None:
            with lock:
                events.append(dict(ev))

        emitter = SimpleEmitter(writer, make_state(), None, None)

        def emit_many() -> None:
            for i in range(50):
                emitter.emit_message_delta(f"chunk-{i}")

        threads = [threading.Thread(target=emit_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(events) == 200
        for ev in events:
            assert "ind" in ev and ev["ind"] == ANSWER_PHASE_INDEX
            assert "content" in ev
            assert "conversation_id" in ev and "turn_id" in ev


# --- SimpleEmitter vs DeepReasoningEmitter ---


class TestSimpleEmitter:
    """SimpleEmitter uses derive_stream_identifiers for conversation_id and turn_id."""

    def test_simple_emitter_resolves_identifiers(self) -> None:
        events = []
        state = make_state(conversation_id="my-conv", task_id=99)
        config = {"configurable": {"thread_id": "thread-abc"}}
        emitter = SimpleEmitter(make_writer(events), state, config, None)
        emitter.emit_message_delta("hi")
        assert len(events) == 1
        assert events[0]["conversation_id"] == "my-conv"
        assert events[0]["turn_id"] == "thread-abc"

    def test_simple_emitter_fallback_turn_id(self) -> None:
        events = []
        state = make_state(conversation_id="c1", task_id=7)
        emitter = SimpleEmitter(make_writer(events), state, None, None)
        emitter.emit_message_delta("x")
        assert events[0]["turn_id"] == "lg-7"

    def test_simple_emitter_includes_turn_sequence_only(self) -> None:
        events = []
        state = make_state(conversation_id="c1", task_id=7)
        config = {"configurable": {"thread_id": "thread-abc"}}
        context = GraphRuntimeContext(task_id=7, turn_id="thread-abc", turn_sequence=7)
        emitter = SimpleEmitter(make_writer(events), state, config, context)
        emitter.emit_message_delta("x")
        assert events[0].get("turn_sequence") == 7
        assert "sequence" not in events[0]


class TestDeepReasoningEmitter:
    """DeepReasoningEmitter uses canonical turn_id; iteration is internal only."""

    def test_dr_emitter_uses_canonical_turn_id(self) -> None:
        """DR emitter uses the same canonical turn_id as SimpleEmitter (no iteration encoding)."""
        events = []
        state = make_state(conversation_id="c1", task_id=10, capability="deep_reasoning")
        state.facts.metadata = {"dr_iteration_meta": {"counter": 0, "active_iteration": 1}}
        config = {"configurable": {"thread_id": "th-1"}}
        emitter = DeepReasoningEmitter(make_writer(events), state, config, None)
        emitter.emit_message_delta("dr chunk")
        assert len(events) == 1
        # Canonical identity: turn_id comes from config thread_id, NOT iteration-encoded
        assert events[0]["turn_id"] == "th-1"

    def test_dr_advance_iteration_does_not_change_turn_id(self) -> None:
        """advance_iteration increments internal counter but keeps turn_id stable."""
        events = []
        state = make_state(conversation_id="c1", task_id=10)
        state.facts.metadata = {}
        config = {"configurable": {"thread_id": "th-1"}}
        emitter = DeepReasoningEmitter(make_writer(events), state, config, None)
        turn_id_before = emitter._turn_id
        it = emitter.advance_iteration()
        assert isinstance(it, int)
        assert it >= 1
        # turn_id must NOT change — canonical identity is stable across iterations
        assert emitter._turn_id == turn_id_before

    def test_dr_emitter_sets_sub_turn_index_from_state(self) -> None:
        """DR emitter extracts sub_turn_index from state's dr_iteration_meta.counter."""
        events: List[Dict[str, Any]] = []
        state = make_state(conversation_id="c1", task_id=10, capability="deep_reasoning")
        state.facts.metadata = {"dr_iteration_meta": {"counter": 3, "active_iteration": 3}}
        config = {"configurable": {"thread_id": "th-1"}}
        emitter = DeepReasoningEmitter(make_writer(events), state, config, None)
        emitter.emit_observation_start("observing")
        assert len(events) == 1
        assert events[0].get("sub_turn_index") == 3

    def test_dr_emitter_no_sub_turn_index_when_no_dr_meta(self) -> None:
        """DR emitter does not set sub_turn_index when dr_iteration_meta is absent."""
        events: List[Dict[str, Any]] = []
        state = make_state(conversation_id="c1", task_id=10, capability="deep_reasoning")
        state.facts.metadata = {}
        config = {"configurable": {"thread_id": "th-1"}}
        emitter = DeepReasoningEmitter(make_writer(events), state, config, None)
        emitter.emit_observation_start("observing")
        assert len(events) == 1
        assert "sub_turn_index" not in events[0]

    def test_dr_emitter_falls_back_to_retry_tracking_count(self) -> None:
        """When DR iteration metadata is absent, retry count can drive sub-turn identity."""
        events: List[Dict[str, Any]] = []
        state = make_state(conversation_id="c1", task_id=10, capability="deep_reasoning")
        state.facts.metadata = {"retry_tracking": {"count": 1}}
        config = {"configurable": {"thread_id": "th-1"}}
        emitter = DeepReasoningEmitter(make_writer(events), state, config, None)
        emitter.emit_observation_start("observing")
        assert len(events) == 1
        assert events[0].get("sub_turn_index") == 1

    def test_dr_emitter_prefers_dr_counter_over_retry_tracking(self) -> None:
        """DR iteration counter has precedence over retry count for sub-turn identity."""
        events: List[Dict[str, Any]] = []
        state = make_state(conversation_id="c1", task_id=10, capability="deep_reasoning")
        state.facts.metadata = {
            "dr_iteration_meta": {"counter": 4, "active_iteration": 4},
            "retry_tracking": {"count": 2},
        }
        config = {"configurable": {"thread_id": "th-1"}}
        emitter = DeepReasoningEmitter(make_writer(events), state, config, None)
        emitter.emit_observation_start("observing")
        assert len(events) == 1
        assert events[0].get("sub_turn_index") == 4


# --- Edge cases ---


class TestEmitterEdgeCases:
    """Optional args and extra_fields."""

    def test_emit_tool_end_optional_args(self) -> None:
        events = []
        emitter = SimpleEmitter(make_writer(events), make_state(), None, None)
        emitter.emit_tool_end("tool1", "success", 0.0, None)
        assert events[0]["summary"] == {}
        assert events[0].get("exit_code") is None
        assert events[0].get("error") is None

    def test_emit_message_start_extra_fields_filter_none(self) -> None:
        events = []
        emitter = SimpleEmitter(make_writer(events), make_state(), None, None)
        emitter.emit_message_start(extra_fields={"a": 1, "b": None, "c": "x"})
        assert events[0]["a"] == 1
        assert events[0]["c"] == "x"
        assert "b" not in events[0]
