"""Validation tests for legacy helper removal and unified emitter migration.

Ensures:
- 100% metadata completeness across all graphs
- 0% frontend fallback usage (no ind=-1)
- 0% observation blending (ind=3 never mixes with ind=2)
- All nodes use UnifiedEventEmitter or EventEmitterFactory
- Integration tests validate event metadata in actual graph flows
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

# Set mock DATABASE_URL before backend/agent imports that may touch DB
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from agent.graph.contracts.streaming_constants import (
    ANSWER_PHASE_INDEX,
    OBSERVATION_PHASE_INDEX,
    REASONING_PHASE_INDEX,
    TOOL_PHASE_INDEX,
)
from agent.graph.emission import EventEmitterFactory
from agent.graph.emission.unified_emitter import SimpleEmitter, UnifiedEventEmitter
from agent.graph.state import FactsState, InteractiveState, InteractiveInput, TraceState
from backend.services.usage_tracking.models import UsageData


# --- Helper functions ---


def _make_mock_llm_client(chunks: List[str]):
    """Return a mock LLMClient that yields usage-aware stream chunks."""
    async def _stream():
        for c in chunks:
            yield c

    class _StreamWithUsage:
        content_iterator = None

        def get_final_usage(self):
            return UsageData(
                prompt_tokens=10,
                completion_tokens=len(chunks),
                total_tokens=10 + len(chunks),
                model="gpt-5.2",
                provider="openai",
                api_surface="responses",
            )

    class _Client:
        def stream_chat_messages(self, *args: Any, **kwargs: Any):
            return _stream()

        async def stream_chat_messages_with_usage(self, *args: Any, **kwargs: Any):
            s = _StreamWithUsage()
            s.content_iterator = _stream()
            return s

    return _Client()


def _make_capturing_writer(events: List[Dict[str, Any]]):
    """Return a writer function that appends events to the list."""
    def writer(event: Dict[str, Any]) -> None:
        events.append(dict(event))

    return writer


def _make_simple_chat_state(
    task_id: int,
    conversation_id: str,
    message: str,
    *,
    history: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Factory for creating InteractiveState as graph state for simple_chat tests.

    Seeds ``metadata['context_bundle']`` so the simple-chat node can
    resolve its prior-turn transcript from the shared bundle authority
    (the production path wires this via
    ``LangGraphContextBuilder.build_runtime_config``).
    """
    from agent.graph.context.builder import (
        METADATA_CONTEXT_BUNDLE_KEY,
        build_conversation_context_bundle,
    )

    bundle = build_conversation_context_bundle(
        conversation_id=conversation_id,
        turn_id=f"{conversation_id}-turn-0",
        turn_sequence=0,
        messages=list(history or []),
    )
    payload = InteractiveInput(
        task_id=task_id,
        message=message,
        conversation_id=conversation_id,
        metadata={
            "simple_chat_runtime": {"model": "stub"},
            METADATA_CONTEXT_BUNDLE_KEY: bundle,
        },
    )
    return payload.to_state().as_graph_state()


def _make_simple_tool_state(
    task_id: int,
    conversation_id: str,
    synthesized_output: Dict[str, Any],
    *,
    last_tool_result: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Factory for finalize_tool_results tests."""
    facts = FactsState(
        task_id=task_id,
        message="Run nmap",
        conversation_id=conversation_id,
        metadata={
            "synthesized_output": synthesized_output,
            "last_tool_result": last_tool_result or {},
        },
    )
    return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()


def _make_dr_state(
    task_id: int,
    conversation_id: str,
    dr_iteration: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Factory for deep_reasoning finalizer tests."""
    metadata = dr_iteration or {}
    facts = FactsState(
        task_id=task_id,
        message="Deep reasoning task",
        conversation_id=conversation_id,
        capability="deep_reasoning",
        metadata=metadata,
    )
    return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()


REQUIRED_METADATA_KEYS = {"ind", "step_type", "conversation_id", "turn_id", "streaming"}


def _events_missing_required_keys(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return events that are missing any of the required metadata keys."""
    return [e for e in events if not REQUIRED_METADATA_KEYS.issubset(e.keys())]


def _calculate_completeness_rate(events: List[Dict[str, Any]]) -> float:
    """Calculate percentage of events with complete metadata (ind, step_type, conversation_id, turn_id, streaming)."""
    if not events:
        return 100.0
    complete = sum(1 for e in events if REQUIRED_METADATA_KEYS.issubset(e.keys()))
    return (complete / len(events)) * 100.0


def _calculate_fallback_rate(events: List[Dict[str, Any]]) -> float:
    """Calculate percentage of events with ind=-1 or missing ind."""
    if not events:
        return 0.0
    fallback = sum(1 for e in events if e.get("ind") == -1 or "ind" not in e)
    return (fallback / len(events)) * 100.0


def _calculate_blending_rate(events: List[Dict[str, Any]]) -> float:
    """Calculate percentage of observation events with wrong ind or message events with wrong ind."""
    if not events:
        return 0.0
    observation_types = {"observation_start", "observation_delta", "observation_section_end"}
    message_types = {"message_start", "message_delta", "section_end"}
    wrong = 0
    for e in events:
        step_type = e.get("step_type") or e.get("type", "")
        ind = e.get("ind")
        if step_type in observation_types and ind != OBSERVATION_PHASE_INDEX:
            wrong += 1
        elif step_type in message_types and ind != ANSWER_PHASE_INDEX:
            wrong += 1
    return (wrong / len(events)) * 100.0 if events else 0.0


def _group_events_by_turn_and_ind(
    events: List[Dict[str, Any]],
) -> Dict[Tuple[str, int], List[Dict[str, Any]]]:
    """Group events by (turn_id, ind) for frontend simulation."""
    groups: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for e in events:
        turn_id = e.get("turn_id", "")
        ind = e.get("ind", 0)
        key = (turn_id, ind)
        groups.setdefault(key, []).append(e)
    return groups


# --- Fixtures ---


@pytest.fixture
def mock_llm_chunks():
    """Sample LLM response chunks for tests."""
    return ["Hello", " ", "world", "!"]


@pytest.fixture
def simple_chat_config():
    """Config for simple_chat tests."""
    return {"configurable": {"thread_id": "test-thread-123"}}


@pytest.fixture
def synthesized_tool_output():
    """Sample synthesized tool output for simple_tool tests."""
    return {
        "tool": "nmap",
        "summary": "Port scan completed.",
        "key_findings": ["Open ports: 22, 80"],
        "vulnerabilities": [],
        "next_actions": ["Review services"],
    }


@pytest.fixture
def dr_iteration_metadata():
    """Sample DR iteration metadata."""
    return {
        "dr_iteration_meta": {"counter": 0, "active_iteration": 1},
        "dr_iteration_records": {},
    }


# --- 1. Static code analysis tests ---


@pytest.mark.emission
class TestEmitterFactoryWiring:
    """Validate primary graph nodes use EventEmitterFactory."""

    def test_nodes_use_emitter_factory(self) -> None:
        """Node files must use EventEmitterFactory.create or create_simple/create_deep_reasoning."""
        workspace = Path(__file__).resolve().parents[3]
        # Unified-finalizer migration: ``finalize.py`` is the single
        # streaming surface for both simple-tool and deep-reasoning paths.
        # The legacy ``finalize_results.py`` / ``deep_reasoning_finalizer.py``
        # modules have been removed.
        node_files = [
            workspace / "agent" / "graph" / "nodes" / "simple_chat.py",
            workspace / "agent" / "graph" / "nodes" / "finalize.py",
        ]
        factory_pattern = "EventEmitterFactory.create"
        for path in node_files:
            assert path.exists(), f"Node file missing: {path}"
            text = path.read_text(encoding="utf-8", errors="replace")
            assert factory_pattern in text, (
                f"{path.name} must use EventEmitterFactory.create (or create_simple/create_deep_reasoning)."
            )


# --- 2. Metadata completeness tests ---


@pytest.mark.emission
@pytest.mark.integration
@pytest.mark.slow
class TestMetadataCompleteness:
    """100% metadata completeness across all graph flows."""

    def test_simple_chat_metadata_completeness(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run run_simple_chat; every event has ind, step_type, conversation_id, turn_id, streaming."""
        events: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: _make_capturing_writer(events),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *args, **kwargs: _make_mock_llm_client(["Hi", " ", "there"]),
        )
        state = _make_simple_chat_state(10, "conv-1", "Hi")
        config = {"configurable": {"thread_id": "t-1"}}
        asyncio.run(
            __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"]).run_simple_chat(
                state, context=None, config=config
            )
        )
        all_emitted = events
        assert len(all_emitted) >= 3, "Expected at least message_start, message_delta(s), section_end"
        rate = _calculate_completeness_rate(all_emitted)
        incomplete = _events_missing_required_keys(all_emitted)
        assert rate == 100.0, (
            f"Metadata completeness must be 100%, got {rate:.2f}%. "
            f"Events missing required keys (ind, step_type, conversation_id, turn_id, streaming): {incomplete}"
        )

    def test_simple_tool_metadata_completeness(
        self, monkeypatch: pytest.MonkeyPatch, synthesized_tool_output: Dict[str, Any]
    ) -> None:
        """Run finalize_tool_results; validate 100% completeness for all emitted events (message and observation)."""
        events: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *args, **kwargs: _make_mock_llm_client(["Summary", " ", "text"]),
        )
        state = _make_simple_tool_state(11, "conv-2", synthesized_tool_output)
        config = {"configurable": {"thread_id": "t-2"}}
        asyncio.run(
            __import__("agent.graph.nodes.finalize", fromlist=["finalize_results"]).finalize_results(
                state, context=None, config=config
            )
        )
        all_emitted = events
        assert all_emitted, "Expected at least one event from finalize_tool_results"
        rate = _calculate_completeness_rate(all_emitted)
        incomplete = _events_missing_required_keys(all_emitted)
        assert rate == 100.0, (
            f"Simple tool completeness must be 100%, got {rate:.2f}%. "
            f"Events missing required keys (ind, step_type, conversation_id, turn_id, streaming): {incomplete}"
        )

    def test_deep_reasoning_metadata_completeness(
        self, monkeypatch: pytest.MonkeyPatch, dr_iteration_metadata: Dict[str, Any]
    ) -> None:
        """Run finalize_deep_reasoning; 100% completeness for all emitted events (message and observation)."""
        events: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *args, **kwargs: _make_mock_llm_client(["DR", " ", "response"]),
        )
        state = _make_dr_state(12, "conv-3", dr_iteration_metadata)
        config = {"configurable": {"thread_id": "t-3"}}
        asyncio.run(
            __import__(
                "agent.graph.nodes.finalize",
                fromlist=["finalize_results"],
            ).finalize_results(state, context=None, config=config)
        )
        all_emitted = events
        assert all_emitted, "Expected events from finalize_deep_reasoning"
        rate = _calculate_completeness_rate(all_emitted)
        incomplete = _events_missing_required_keys(all_emitted)
        assert rate == 100.0, (
            f"Deep reasoning completeness must be 100%, got {rate:.2f}%. "
            f"Events missing required keys (ind, step_type, conversation_id, turn_id, streaming): {incomplete}"
        )

    def test_all_graphs_metadata_completeness_aggregate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Aggregate completeness across all three graph types >= 100%."""
        all_events: List[Dict[str, Any]] = []
        # Simple chat
        events_sc: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: _make_capturing_writer(events_sc),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *args, **kwargs: _make_mock_llm_client(["x"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"]).run_simple_chat(
                _make_simple_chat_state(1, "c1", "m1"), context=None, config={"configurable": {"thread_id": "t1"}}
            )
        )
        all_events.extend(events_sc)
        # Simple tool
        events_st: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events_st),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *args, **kwargs: _make_mock_llm_client(["y"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.finalize", fromlist=["finalize_results"]).finalize_results(
                _make_simple_tool_state(2, "c2", {"tool": "nmap", "summary": "ok"}), context=None, config={"configurable": {"thread_id": "t2"}}
            )
        )
        all_events.extend(events_st)
        # Deep reasoning
        events_dr: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events_dr),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *args, **kwargs: _make_mock_llm_client(["z"]),
        )
        asyncio.run(
            __import__(
                "agent.graph.nodes.finalize",
                fromlist=["finalize_results"],
            ).finalize_results(
                _make_dr_state(3, "c3", {}), context=None, config={"configurable": {"thread_id": "t3"}}
            )
        )
        all_events.extend(events_dr)
        rate = _calculate_completeness_rate(all_events)
        incomplete = _events_missing_required_keys(all_events)
        assert rate >= 100.0 or (len(all_events) > 0 and rate == 100.0), (
            f"Aggregate metadata completeness must be 100%, got {rate:.2f}%. "
            f"Events missing required keys (ind, step_type, conversation_id, turn_id, streaming): {incomplete}"
        )


# --- 3. Frontend fallback usage tests ---


@pytest.mark.emission
@pytest.mark.integration
class TestZeroFrontendFallback:
    """No events use frontend fallback (ind=-1 or missing ind)."""

    def test_no_events_with_ind_negative_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No event has ind == -1 across simple_chat, finalize_tool_results, finalize_deep_reasoning."""
        events1: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: _make_capturing_writer(events1),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["a"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"]).run_simple_chat(
                _make_simple_chat_state(1, "c1", "m"), context=None, config={"configurable": {"thread_id": "t1"}}
            )
        )
        events2: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events2),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["b"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.finalize", fromlist=["finalize_results"]).finalize_results(
                _make_simple_tool_state(2, "c2", {"tool": "nmap", "summary": "s"}), context=None, config={"configurable": {"thread_id": "t2"}}
            )
        )
        events3: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events3),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["c"]),
        )
        asyncio.run(
            __import__(
                "agent.graph.nodes.finalize",
                fromlist=["finalize_results"],
            ).finalize_results(
                _make_dr_state(3, "c3", {}), context=None, config={"configurable": {"thread_id": "t3"}}
            )
        )
        all_events = events1 + events2 + events3
        with_ind_neg = [e for e in all_events if e.get("ind") == -1]
        rate = (len(with_ind_neg) / len(all_events)) * 100.0 if all_events else 0.0
        assert rate == 0.0, (
            f"Frontend fallback (ind=-1) rate must be 0%, got {rate:.2f}%. "
            f"Events with ind=-1: {with_ind_neg}"
        )

    def test_no_events_with_missing_ind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No event is missing the ind key across simple_chat, finalize_tool_results, finalize_deep_reasoning."""
        events1: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: _make_capturing_writer(events1),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["x"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"]).run_simple_chat(
                _make_simple_chat_state(1, "c1", "m"), context=None, config={"configurable": {"thread_id": "t1"}}
            )
        )
        events2: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events2),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["y"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.finalize", fromlist=["finalize_results"]).finalize_results(
                _make_simple_tool_state(2, "c2", {"tool": "nmap", "summary": "s"}), context=None, config={"configurable": {"thread_id": "t2"}}
            )
        )
        events3: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events3),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["z"]),
        )
        asyncio.run(
            __import__(
                "agent.graph.nodes.finalize",
                fromlist=["finalize_results"],
            ).finalize_results(
                _make_dr_state(3, "c3", {}), context=None, config={"configurable": {"thread_id": "t3"}}
            )
        )
        all_events = events1 + events2 + events3
        missing = [e for e in all_events if "ind" not in e]
        rate = (len(missing) / len(all_events)) * 100.0 if all_events else 0.0
        assert rate == 0.0, (
            f"Missing ind rate must be 0%, got {rate:.2f}%. Events missing ind: {missing}"
        )

    def test_all_events_have_valid_phase_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All events have ind in [0, 3] across simple_chat, finalize_tool_results, finalize_deep_reasoning."""
        events1: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: _make_capturing_writer(events1),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["x"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"]).run_simple_chat(
                _make_simple_chat_state(1, "c1", "m"), context=None, config={"configurable": {"thread_id": "t1"}}
            )
        )
        events2: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events2),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["y"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.finalize", fromlist=["finalize_results"]).finalize_results(
                _make_simple_tool_state(2, "c2", {"tool": "nmap", "summary": "s"}), context=None, config={"configurable": {"thread_id": "t2"}}
            )
        )
        events3: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events3),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["z"]),
        )
        asyncio.run(
            __import__(
                "agent.graph.nodes.finalize",
                fromlist=["finalize_results"],
            ).finalize_results(
                _make_dr_state(3, "c3", {}), context=None, config={"configurable": {"thread_id": "t3"}}
            )
        )
        all_events = events1 + events2 + events3
        valid_range = {REASONING_PHASE_INDEX, TOOL_PHASE_INDEX, ANSWER_PHASE_INDEX, OBSERVATION_PHASE_INDEX}
        invalid = [e for e in all_events if e.get("ind") not in valid_range]
        assert not invalid, (
            f"All events must have ind in [0,3]. Invalid: {invalid}"
        )


# --- 4. Observation blending tests ---


@pytest.mark.emission
class TestZeroObservationBlending:
    """Observation events (ind=3) never mix with message events (ind=2)."""

    def test_observation_and_message_distinct_phases(self) -> None:
        """Emit observation then message via SimpleEmitter; groups by (turn_id, ind) are distinct."""
        events: List[Dict[str, Any]] = []
        state = InteractiveState(facts=FactsState(task_id=1, message="m", conversation_id="c1"), trace=TraceState())
        emitter = SimpleEmitter(_make_capturing_writer(events), state, None, None)
        emitter.emit_observation_start(step="obs")
        emitter.emit_observation_delta("text")
        emitter.emit_observation_section_end(section_name="obs")
        emitter.emit_message_start()
        emitter.emit_message_delta("msg")
        emitter.emit_section_end("final_answer")
        groups = _group_events_by_turn_and_ind(events)
        obs_keys = [k for k in groups if k[1] == OBSERVATION_PHASE_INDEX]
        msg_keys = [k for k in groups if k[1] == ANSWER_PHASE_INDEX]
        assert len(obs_keys) >= 1 and len(msg_keys) >= 1, "Expected both observation and message groups"
        mixed = [g for g in groups.values() if len({e.get("ind") for e in g}) > 1]
        blend_rate = (sum(len(g) for g in mixed) / len(events)) * 100.0 if events else 0.0
        assert blend_rate == 0.0, f"Observation/message blending rate must be 0%, got {blend_rate:.2f}%."

    def test_observation_events_only_ind_three(self) -> None:
        """All observation-type events have ind == 3."""
        events: List[Dict[str, Any]] = []
        state = InteractiveState(facts=FactsState(task_id=1, message="m", conversation_id="c1"), trace=TraceState())
        emitter = SimpleEmitter(_make_capturing_writer(events), state, None, None)
        emitter.emit_observation_start(step="x")
        emitter.emit_observation_delta("y")
        emitter.emit_observation_section_end(section_name="x")
        observation_types = ("observation_start", "observation_delta", "observation_section_end")
        obs_events = [e for e in events if (e.get("step_type") or e.get("type")) in observation_types]
        wrong = [e for e in obs_events if e.get("ind") != OBSERVATION_PHASE_INDEX]
        assert not wrong, f"Observation events must have ind=3. Wrong: {wrong}"

    def test_message_events_only_ind_two(self) -> None:
        """All message-type events have ind == 2."""
        events: List[Dict[str, Any]] = []
        state = InteractiveState(facts=FactsState(task_id=1, message="m", conversation_id="c1"), trace=TraceState())
        emitter = SimpleEmitter(_make_capturing_writer(events), state, None, None)
        emitter.emit_message_start()
        emitter.emit_message_delta("x")
        emitter.emit_section_end("final_answer")
        message_types = ("message_start", "message_delta", "section_end")
        msg_events = [e for e in events if (e.get("step_type") or e.get("type")) in message_types]
        wrong = [e for e in msg_events if e.get("ind") != ANSWER_PHASE_INDEX]
        assert not wrong, f"Message events must have ind=2. Wrong: {wrong}"

    def test_frontend_grouping_by_turn_and_ind(self) -> None:
        """Group events by (turn_id, ind); each group contains only one event type family."""
        events: List[Dict[str, Any]] = []
        state = InteractiveState(facts=FactsState(task_id=1, message="m", conversation_id="c1"), trace=TraceState())
        emitter = SimpleEmitter(_make_capturing_writer(events), state, None, None)
        emitter.emit_observation_start(step="o")
        emitter.emit_observation_delta("o1")
        emitter.emit_message_start()
        emitter.emit_message_delta("m1")
        groups = _group_events_by_turn_and_ind(events)
        for key, group in groups.items():
            inds = {e.get("ind") for e in group}
            assert len(inds) <= 1, f"Group {key} must not mix phases: {inds}"


# --- 5. Node usage validation tests ---


@pytest.mark.emission
class TestNodesUseUnifiedEmitter:
    """All nodes use UnifiedEventEmitter or EventEmitterFactory."""

    def test_simple_chat_uses_emitter_factory(self) -> None:
        """simple_chat.py uses EventEmitterFactory.create or UnifiedEventEmitter."""
        workspace = Path(__file__).resolve().parents[3]
        path = workspace / "agent" / "graph" / "nodes" / "simple_chat.py"
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "EventEmitterFactory.create" in text or "UnifiedEventEmitter" in text
        assert "emit_message_start(writer," not in text and "emit_message_delta(writer," not in text

    def test_finalize_results_uses_emitter_factory(self) -> None:
        """Unified finalizer node uses EventEmitterFactory (simple-tool path)."""
        workspace = Path(__file__).resolve().parents[3]
        path = workspace / "agent" / "graph" / "nodes" / "finalize.py"
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "EventEmitterFactory" in text and ("create_simple" in text or "create" in text)
        assert "emit_message_start(writer," not in text and "emit_message_delta(writer," not in text

    def test_deep_reasoning_finalizer_uses_emitter_factory(self) -> None:
        """Unified finalizer node uses EventEmitterFactory (deep-reasoning path)."""
        workspace = Path(__file__).resolve().parents[3]
        path = workspace / "agent" / "graph" / "nodes" / "finalize.py"
        text = path.read_text(encoding="utf-8", errors="replace")
        assert (
            "EventEmitterFactory" in text
            and ("create_turn_level" in text or "create_deep_reasoning" in text or "create" in text)
        )
        assert "emit_message_start(writer," not in text and "emit_message_delta(writer," not in text

    def test_no_direct_helper_calls_in_nodes(self) -> None:
        """Node files must not call emit_message_delta(writer, or emit_observation_start(writer,."""
        workspace = Path(__file__).resolve().parents[3]
        nodes_dir = workspace / "agent" / "graph" / "nodes"
        patterns = ["emit_message_delta(writer,", "emit_observation_start(writer,"]
        for path in (nodes_dir / "simple_chat.py", nodes_dir / "finalize.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for pat in patterns:
                assert pat not in text, f"{path.name} must not use direct helper call: {pat}"


# --- 6. Integration flow tests ---


@pytest.mark.emission
@pytest.mark.integration
@pytest.mark.slow
class TestGraphFlowsWithMetadataValidation:
    """Run full graph flows and validate event metadata end-to-end."""

    def test_simple_chat_flow_complete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_simple_chat: >=3 events, complete metadata, all ind=2, groupable by (turn_id, ind)."""
        events: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: _make_capturing_writer(events),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["A", " ", "B"]),
        )
        state = _make_simple_chat_state(10, "conv-1", "Hi")
        config = {"configurable": {"thread_id": "turn-1"}}
        asyncio.run(
            __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"]).run_simple_chat(
                state, context=None, config=config
            )
        )
        message_events = [e for e in events if e.get("type") in ("message_start", "message_delta", "section_end")]
        assert len(message_events) >= 3, "At least message_start, message_delta(s), section_end"
        assert _calculate_completeness_rate(message_events) == 100.0
        assert all(e.get("ind") == ANSWER_PHASE_INDEX for e in message_events)
        groups = _group_events_by_turn_and_ind(message_events)
        assert len(groups) >= 1

    def test_simple_tool_flow_complete(
        self, monkeypatch: pytest.MonkeyPatch, synthesized_tool_output: Dict[str, Any]
    ) -> None:
        """finalize_tool_results: message events present, no blending, complete metadata."""
        events: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["Final", " ", "answer"]),
        )
        state = _make_simple_tool_state(11, "conv-2", synthesized_tool_output)
        config = {"configurable": {"thread_id": "turn-2"}}
        asyncio.run(
            __import__("agent.graph.nodes.finalize", fromlist=["finalize_results"]).finalize_results(
                state, context=None, config=config
            )
        )
        message_events = [e for e in events if e.get("type") in ("message_start", "message_delta", "section_end")]
        assert message_events, "Message events from finalize_tool_results"
        assert _calculate_completeness_rate(message_events) == 100.0
        assert all(e.get("ind") == ANSWER_PHASE_INDEX for e in message_events)

    def test_deep_reasoning_flow_complete(
        self, monkeypatch: pytest.MonkeyPatch, dr_iteration_metadata: Dict[str, Any]
    ) -> None:
        """finalize_deep_reasoning: iteration-aware turn_id, all ind=2, consistent metadata."""
        events: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["DR", " ", "summary"]),
        )
        state = _make_dr_state(12, "conv-3", dr_iteration_metadata)
        config = {"configurable": {"thread_id": "thread-dr"}}
        asyncio.run(
            __import__(
                "agent.graph.nodes.finalize",
                fromlist=["finalize_results"],
            ).finalize_results(state, context=None, config=config)
        )
        message_events = [e for e in events if e.get("type") in ("message_start", "message_delta", "section_end")]
        assert message_events, "Message events from finalize_deep_reasoning"
        assert all(e.get("ind") == ANSWER_PHASE_INDEX for e in message_events)
        turn_ids = {e.get("turn_id") for e in message_events}
        assert len(turn_ids) >= 1

    def test_all_graphs_event_ordering_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Events emitted in logical order (e.g. message_start before message_delta)."""
        events: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: _make_capturing_writer(events),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["x"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"]).run_simple_chat(
                _make_simple_chat_state(1, "c1", "m"), context=None, config={"configurable": {"thread_id": "t1"}}
            )
        )
        types_seen = [e.get("type") or e.get("step_type") for e in events]
        if "message_start" in types_seen and "message_delta" in types_seen:
            assert types_seen.index("message_start") < types_seen.index("message_delta"), "message_start must precede message_delta"
        if "message_delta" in types_seen and "section_end" in types_seen:
            assert types_seen.index("message_delta") < types_seen.index("section_end"), "message_delta must precede section_end"

    def test_observation_and_message_flow_no_blending(self) -> None:
        """Run a flow producing both observation and message events; assert no blending (ind=3 vs ind=2)."""
        events: List[Dict[str, Any]] = []
        state = InteractiveState(facts=FactsState(task_id=1, message="m", conversation_id="c1"), trace=TraceState())
        emitter = SimpleEmitter(_make_capturing_writer(events), state, None, None)
        emitter.emit_observation_start(step="obs")
        emitter.emit_observation_delta("observation text")
        emitter.emit_observation_section_end(section_name="obs")
        emitter.emit_message_start()
        emitter.emit_message_delta("message text")
        emitter.emit_section_end("final_answer")
        groups = _group_events_by_turn_and_ind(events)
        for key, group in groups.items():
            inds = {e.get("ind") for e in group}
            assert len(inds) <= 1, (
                f"Group (turn_id={key[0]!r}, ind={key[1]}) must not contain mixed phases; inds={inds}"
            )
        observation_types = {"observation_start", "observation_delta", "observation_section_end"}
        message_types = {"message_start", "message_delta", "section_end"}
        for e in events:
            step_type = e.get("step_type") or e.get("type", "")
            ind = e.get("ind")
            if step_type in observation_types:
                assert ind == OBSERVATION_PHASE_INDEX, (
                    f"Observation event must have ind=3, got ind={ind}: {e}"
                )
            if step_type in message_types:
                assert ind == ANSWER_PHASE_INDEX, (
                    f"Message event must have ind=2, got ind={ind}: {e}"
                )
        blend_rate = _calculate_blending_rate(events)
        assert blend_rate == 0.0, (
            f"Observation/message blending rate must be 0%, got {blend_rate:.2f}%. Fail on any blending above 0%."
        )


# --- 7. Success metrics validation ---


@pytest.mark.emission
@pytest.mark.integration
class TestSuccessMetrics:
    """Validate refactoring ticket success metrics."""

    def test_metadata_completeness_100_percent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Aggregate across simple_chat, finalize_tool_results, finalize_deep_reasoning: completeness == 100%."""
        events1: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: _make_capturing_writer(events1),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["a"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"]).run_simple_chat(
                _make_simple_chat_state(1, "c1", "m"), context=None, config={"configurable": {"thread_id": "t1"}}
            )
        )
        events2: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events2),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["b"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.finalize", fromlist=["finalize_results"]).finalize_results(
                _make_simple_tool_state(2, "c2", {"tool": "nmap", "summary": "s"}), context=None, config={"configurable": {"thread_id": "t2"}}
            )
        )
        events3: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events3),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["c"]),
        )
        asyncio.run(
            __import__(
                "agent.graph.nodes.finalize",
                fromlist=["finalize_results"],
            ).finalize_results(
                _make_dr_state(3, "c3", {}), context=None, config={"configurable": {"thread_id": "t3"}}
            )
        )
        all_events = events1 + events2 + events3
        rate = _calculate_completeness_rate(all_events)
        incomplete = _events_missing_required_keys(all_events)
        assert rate == 100.0, (
            f"Aggregate metadata completeness must be 100%, got {rate:.2f}%. "
            f"Events missing required keys: {incomplete}"
        )

    def test_frontend_fallback_usage_0_percent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Aggregate across all three flows: (events_with_ind_negative_one / total_events) * 100 == 0%."""
        events1: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: _make_capturing_writer(events1),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["a"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"]).run_simple_chat(
                _make_simple_chat_state(1, "c1", "m"), context=None, config={"configurable": {"thread_id": "t1"}}
            )
        )
        events2: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events2),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["b"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.finalize", fromlist=["finalize_results"]).finalize_results(
                _make_simple_tool_state(2, "c2", {"tool": "nmap", "summary": "s"}), context=None, config={"configurable": {"thread_id": "t2"}}
            )
        )
        events3: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events3),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["c"]),
        )
        asyncio.run(
            __import__(
                "agent.graph.nodes.finalize",
                fromlist=["finalize_results"],
            ).finalize_results(
                _make_dr_state(3, "c3", {}), context=None, config={"configurable": {"thread_id": "t3"}}
            )
        )
        all_events = events1 + events2 + events3
        rate = _calculate_fallback_rate(all_events)
        with_ind_neg = [e for e in all_events if e.get("ind") == -1 or "ind" not in e]
        assert rate == 0.0, (
            f"Aggregate frontend fallback usage must be 0%, got {rate:.2f}%. "
            f"Events with ind=-1 or missing ind: {with_ind_neg}"
        )

    def test_observation_blending_rate_0_percent(self) -> None:
        """(observation_events_with_wrong_ind / total_observation_events) * 100 == 0%."""
        events: List[Dict[str, Any]] = []
        state = InteractiveState(facts=FactsState(task_id=1, message="m", conversation_id="c1"), trace=TraceState())
        emitter = SimpleEmitter(_make_capturing_writer(events), state, None, None)
        emitter.emit_observation_start(step="x")
        emitter.emit_observation_delta("y")
        rate = _calculate_blending_rate(events)
        assert rate == 0.0, f"Observation blending rate must be 0%, got {rate:.2f}%."

    def test_event_ordering_violations_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No message_delta before message_start, etc."""
        events: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: _make_capturing_writer(events),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["x"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"]).run_simple_chat(
                _make_simple_chat_state(1, "c1", "m"), context=None, config={"configurable": {"thread_id": "t1"}}
            )
        )
        types_list = [e.get("type") or e.get("step_type") for e in events]
        violations = 0
        if "message_delta" in types_list and "message_start" in types_list:
            if types_list.index("message_delta") < types_list.index("message_start"):
                violations += 1
        if "section_end" in types_list and "message_start" in types_list:
            if types_list.index("section_end") < types_list.index("message_start"):
                violations += 1
        assert violations == 0, f"Event ordering violations: {violations}"

    def test_all_success_metrics_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Aggregate across all three flows: completeness 100%, fallback 0%, blending 0%, ordering violations 0."""
        events1: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: _make_capturing_writer(events1),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["ok"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.simple_chat", fromlist=["run_simple_chat"]).run_simple_chat(
                _make_simple_chat_state(1, "c1", "m"), context=None, config={"configurable": {"thread_id": "t1"}}
            )
        )
        events2: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events2),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["ok"]),
        )
        asyncio.run(
            __import__("agent.graph.nodes.finalize", fromlist=["finalize_results"]).finalize_results(
                _make_simple_tool_state(2, "c2", {"tool": "nmap", "summary": "s"}), context=None, config={"configurable": {"thread_id": "t2"}}
            )
        )
        events3: List[Dict[str, Any]] = []
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.get_stream_writer",
            lambda: _make_capturing_writer(events3),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.finalize.resolve_llm_client",
            lambda *a, **k: _make_mock_llm_client(["ok"]),
        )
        asyncio.run(
            __import__(
                "agent.graph.nodes.finalize",
                fromlist=["finalize_results"],
            ).finalize_results(
                _make_dr_state(3, "c3", {}), context=None, config={"configurable": {"thread_id": "t3"}}
            )
        )
        all_events = events1 + events2 + events3
        completeness = _calculate_completeness_rate(all_events)
        fallback = _calculate_fallback_rate(all_events)
        blending = _calculate_blending_rate(all_events)
        types_list = [e.get("type") or e.get("step_type") for e in all_events]
        order_ok = True
        if "message_delta" in types_list and "message_start" in types_list:
            order_ok = order_ok and types_list.index("message_start") <= types_list.index("message_delta")
        assert completeness == 100.0, f"Aggregate completeness: {completeness}%"
        assert fallback == 0.0, f"Aggregate fallback: {fallback}%"
        assert blending == 0.0, f"Aggregate blending: {blending}%"
        assert order_ok, "Ordering violation"
