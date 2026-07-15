"""Unit tests for EventEmitterFactory.

Covers: factory routing by capability, correct emitter type returned,
identifier resolution (conversation_id, turn_id), iteration handling for DR emitter.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

from agent.graph.emission.factory import EventEmitterFactory
from agent.graph.emission.unified_emitter import (
    DeepReasoningEmitter,
    SimpleEmitter,
    UnifiedEventEmitter,
)
from agent.graph.infrastructure.state_models import FactsState, GraphRuntimeContext
from agent.graph.state import InteractiveState, TraceState


def make_writer(events: List[Dict[str, Any]]) -> Any:
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


class TestFactoryRoutingByCapability:
    """Factory returns correct emitter type based on capability."""

    def test_deep_reasoning_returns_deep_reasoning_emitter(self) -> None:
        state = make_state(capability="deep_reasoning")
        writer = make_writer([])
        emitter = EventEmitterFactory.create(writer, state, None, None)
        assert isinstance(emitter, DeepReasoningEmitter)
        assert isinstance(emitter, UnifiedEventEmitter)

    def test_simple_tool_returns_simple_emitter(self) -> None:
        state = make_state(capability="simple_tool_execution")
        writer = make_writer([])
        emitter = EventEmitterFactory.create(writer, state, None, None)
        assert isinstance(emitter, SimpleEmitter)
        assert not isinstance(emitter, DeepReasoningEmitter)

    def test_respond_only_returns_simple_emitter(self) -> None:
        state = make_state(capability="respond_only")
        writer = make_writer([])
        emitter = EventEmitterFactory.create(writer, state, None, None)
        assert isinstance(emitter, SimpleEmitter)

    def test_capability_from_metadata_returns_dr_emitter(self) -> None:
        state = make_state(metadata={"capability": "deep_reasoning"})
        state.facts.capability = None
        writer = make_writer([])
        emitter = EventEmitterFactory.create(writer, state, None, None)
        assert isinstance(emitter, DeepReasoningEmitter)

    def test_none_capability_returns_simple_emitter(self) -> None:
        state = make_state(capability=None)
        writer = make_writer([])
        emitter = EventEmitterFactory.create(writer, state, None, None)
        assert isinstance(emitter, SimpleEmitter)

    def test_unknown_capability_returns_simple_emitter(self) -> None:
        state = make_state(capability="unknown_cap")
        writer = make_writer([])
        emitter = EventEmitterFactory.create(writer, state, None, None)
        assert isinstance(emitter, SimpleEmitter)


class TestFactoryIdentifierResolution:
    """Factory-created emitters resolve conversation_id and turn_id correctly."""

    def test_simple_emitter_uses_config_thread_id(self) -> None:
        events: List[Dict[str, Any]] = []
        state = make_state(conversation_id="c1", task_id=1)
        config = {"configurable": {"thread_id": "thread-xyz"}}
        emitter = EventEmitterFactory.create(make_writer(events), state, config, None)
        emitter.emit_message_delta("hi")
        assert len(events) == 1
        assert events[0]["conversation_id"] == "c1"
        assert events[0]["turn_id"] == "thread-xyz"

    def test_simple_emitter_fallback_turn_id_from_task_id(self) -> None:
        events = []
        state = make_state(conversation_id="c1", task_id=99)
        emitter = EventEmitterFactory.create(make_writer(events), state, None, None)
        emitter.emit_message_delta("x")
        assert events[0]["turn_id"] == "lg-99"

    def test_dr_emitter_turn_id_is_canonical(self) -> None:
        """DR emitter uses canonical turn_id from config, not iteration-encoded."""
        events = []
        state = make_state(capability="deep_reasoning", conversation_id="c1", task_id=5)
        state.facts.metadata = {}
        config = {"configurable": {"thread_id": "th-1"}}
        emitter = EventEmitterFactory.create(make_writer(events), state, config, None)
        assert isinstance(emitter, DeepReasoningEmitter)
        emitter.emit_message_delta("dr")
        assert len(events) == 1
        assert events[0]["turn_id"] == "th-1"


class TestFactoryIterationHandling:
    """DeepReasoningEmitter advance_iteration behavior when created via factory."""

    def test_dr_emitter_advance_iteration_returns_increment(self) -> None:
        state = make_state(capability="deep_reasoning")
        state.facts.metadata = {}
        config = {"configurable": {"thread_id": "t1"}}
        emitter = EventEmitterFactory.create(make_writer([]), state, config, None)
        assert isinstance(emitter, DeepReasoningEmitter)
        it1 = emitter.advance_iteration()
        it2 = emitter.advance_iteration()
        assert it1 >= 1
        assert it2 >= 1
        assert it2 >= it1

    def test_dr_emitter_turn_id_stable_after_advance(self) -> None:
        """advance_iteration does NOT change turn_id — canonical identity is stable."""
        state = make_state(capability="deep_reasoning")
        state.facts.metadata = {}
        config = {"configurable": {"thread_id": "t1"}}
        emitter = EventEmitterFactory.create(make_writer([]), state, config, None)
        assert isinstance(emitter, DeepReasoningEmitter)
        turn_id_before = emitter._turn_id
        emitter.advance_iteration()
        turn_id_after = emitter._turn_id
        assert turn_id_before == turn_id_after


class TestFactoryCreateSimpleAndCreateDeepReasoning:
    """Explicit create_simple and create_deep_reasoning bypass routing."""

    def test_create_simple_always_returns_simple_emitter(self) -> None:
        state = make_state(capability="deep_reasoning")
        emitter = EventEmitterFactory.create_simple(make_writer([]), state, None, None)
        assert isinstance(emitter, SimpleEmitter)
        assert not isinstance(emitter, DeepReasoningEmitter)

    def test_create_deep_reasoning_always_returns_dr_emitter(self) -> None:
        state = make_state(capability="respond_only")
        emitter = EventEmitterFactory.create_deep_reasoning(
            make_writer([]), state, None, None
        )
        assert isinstance(emitter, DeepReasoningEmitter)


class TestFactoryCreateFromIdentitySubTurnIndex:
    """create_from_identity passes sub_turn_index to emitter."""

    def test_create_from_identity_with_sub_turn_index(self) -> None:
        events: List[Dict[str, Any]] = []
        emitter = EventEmitterFactory.create_from_identity(
            make_writer(events), "conv-1", "turn-1",
            turn_sequence=5,
            sub_turn_index=2,
        )
        emitter.emit_observation_start("observing")
        assert len(events) == 1
        assert events[0].get("sub_turn_index") == 2

    def test_create_from_identity_without_sub_turn_index(self) -> None:
        events: List[Dict[str, Any]] = []
        emitter = EventEmitterFactory.create_from_identity(
            make_writer(events), "conv-1", "turn-1",
            turn_sequence=5,
        )
        emitter.emit_observation_start("observing")
        assert len(events) == 1
        assert "sub_turn_index" not in events[0]


class TestFactoryCreateTurnLevel:
    """create_turn_level emits canonical identity without sub-turn tracking."""

    def test_create_turn_level_ignores_dr_sub_turn_index(self) -> None:
        events: List[Dict[str, Any]] = []
        state = make_state(
            capability="deep_reasoning",
            conversation_id="conv-1",
            metadata={"dr_iteration_meta": {"counter": 4, "active_iteration": 4}},
        )
        config = {"configurable": {"canonical_conversation_id": "conv-can", "canonical_turn_id": "turn-can", "canonical_turn_sequence": 9}}

        emitter = EventEmitterFactory.create_turn_level(make_writer(events), state, config, None)
        emitter.emit_message_start()

        assert len(events) == 1
        assert events[0].get("conversation_id") == "conv-can"
        assert events[0].get("turn_id") == "turn-can"
        assert events[0].get("turn_sequence") == 9
        assert "sub_turn_index" not in events[0]

    def test_create_deep_reasoning_finalizer_alias_matches_turn_level(self) -> None:
        events_turn_level: List[Dict[str, Any]] = []
        events_alias: List[Dict[str, Any]] = []
        state = make_state(
            capability="deep_reasoning",
            conversation_id="conv-1",
            metadata={"dr_iteration_meta": {"counter": 2, "active_iteration": 2}},
        )
        config = {"configurable": {"thread_id": "turn-1"}}

        emitter_turn_level = EventEmitterFactory.create_turn_level(
            make_writer(events_turn_level), state, config, None
        )
        emitter_alias = EventEmitterFactory.create_deep_reasoning_finalizer(
            make_writer(events_alias), state, config, None
        )
        emitter_turn_level.emit_message_delta("x")
        emitter_alias.emit_message_delta("x")

        assert len(events_turn_level) == 1
        assert len(events_alias) == 1
        assert events_turn_level[0] == events_alias[0]
