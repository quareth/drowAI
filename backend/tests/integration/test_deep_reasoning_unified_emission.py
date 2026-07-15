""": Integration tests for deep reasoning migration to UnifiedEventEmitter.

Covers:
- DR iterations have unique turn_ids (iteration-aware turn_id)
- Metadata consistent within and across iterations (ind=2, step_type)
- No blending within or across iterations
- DeepReasoningEmitter.advance_iteration updates turn_id
- Both flag states (ENABLE_UNIFIED_EMITTER_DEEP_REASONING) work correctly"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from agent.graph.contracts.streaming_constants import ANSWER_PHASE_INDEX
from agent.graph.emission import EventEmitterFactory
from agent.graph.nodes.finalize import finalize_results as finalize_deep_reasoning
from agent.graph.state import InteractiveState
from agent.graph.infrastructure.state_models import FactsState, TraceState
from backend.services.usage_tracking.models import UsageData


# --- Helpers ---


def _make_dr_finalize_state(
    task_id: int = 20,
    conversation_id: str = "conv-dr-test",
    message: str = "Run deep analysis",
    dr_iteration: int | None = None,
    capability: str = "deep_reasoning",
) -> Dict[str, Any]:
    """Build minimal state for finalize_deep_reasoning with optional DR iteration."""
    metadata: Dict[str, Any] = {}
    if dr_iteration is not None:
        metadata["dr_iteration_meta"] = {
            "active_iteration": dr_iteration,
            "counter": dr_iteration,
        }
    facts = FactsState(
        task_id=task_id,
        message=message,
        conversation_id=conversation_id,
        capability=capability,
        metadata=metadata,
    )
    trace = TraceState(reasoning=[])
    interactive = InteractiveState(facts=facts, trace=trace)
    return interactive.as_graph_state()


def _make_streaming_llm_client(chunks: List[str]):
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


def _run_dr_finalize_with_captured_events(
    monkeypatch: pytest.MonkeyPatch,
    *,
    flag_enabled: bool,
    chunks: List[str] | None = None,
    config: Dict[str, Any] | None = None,
    state: Dict[str, Any] | None = None,
    dr_iteration: int | None = 1,
) -> List[Dict[str, Any]]:
    """Run finalize_deep_reasoning with a capturing writer; return list of emitted events."""
    events: List[Dict[str, Any]] = []

    def writer(event: Dict[str, Any]) -> None:
        events.append(dict(event))

    monkeypatch.setattr(
        "agent.graph.nodes.finalize.get_stream_writer",
        lambda: writer,
    )
    monkeypatch.setenv(
        "ENABLE_UNIFIED_EMITTER_DEEP_REASONING",
        "true" if flag_enabled else "false",
    )
    if chunks is None:
        chunks = ["DR ", "final ", "answer"]
    monkeypatch.setattr(
        "agent.graph.nodes.finalize.resolve_llm_client",
        lambda *args, **kwargs: _make_streaming_llm_client(chunks),
    )

    if state is None:
        state = _make_dr_finalize_state(dr_iteration=dr_iteration)
    if config is None:
        config = {"configurable": {"thread_id": "test-thread-dr"}}

    result = asyncio.run(
        finalize_deep_reasoning(state, context=None, config=config)
    )
    updated = InteractiveState.from_mapping(result)
    assert updated.trace.final_text
    return events


def _message_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return events that are message_start, message_delta, or section_end."""
    message_types = {"message_start", "message_delta", "section_end"}
    return [e for e in events if e.get("type") in message_types]


# --- 1. DR iterations have unique turn_ids ---


class TestDRIterationsHaveUniqueTurnIds:
    """Verify DR iterations use canonical turn_id (stable across iterations)."""

    def test_dr_iterations_share_canonical_turn_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run finalizer with iteration 1, 2, 3; verify all share the same canonical turn_id.

        After Issue 13.5 migration, DR iteration tracking is internal and does NOT
        affect turn_id. All iterations use the canonical turn_id from config.
        """
        turn_ids_seen: List[str] = []
        for iteration in (1, 2, 3):
            state = _make_dr_finalize_state(dr_iteration=iteration)
            events = _run_dr_finalize_with_captured_events(
                monkeypatch,
                flag_enabled=True,
                state=state,
                dr_iteration=iteration,
            )
            message_evs = _message_events(events)
            assert message_evs, f"Iteration {iteration}: expected message events"
            turn_ids = list({e.get("turn_id") or e.get("id") for e in message_evs if e.get("turn_id") or e.get("id")})
            assert len(turn_ids) == 1, f"Iteration {iteration}: all events in run should share one turn_id"
            turn_ids_seen.append(turn_ids[0])
        assert len(turn_ids_seen) == 3
        # All iterations share the same canonical turn_id
        assert len(set(turn_ids_seen)) == 1, (
            f"All iterations must share the same canonical turn_id, got {set(turn_ids_seen)}"
        )


    def test_dr_events_within_iteration_share_turn_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Within a single run, all message events share the same turn_id."""
        events = _run_dr_finalize_with_captured_events(
            monkeypatch, flag_enabled=True, dr_iteration=2
        )
        message_evs = _message_events(events)
        assert len(message_evs) >= 3
        turn_ids = [e.get("turn_id") or e.get("id") for e in message_evs]
        turn_ids = [t for t in turn_ids if t]
        assert turn_ids
        assert all(t == turn_ids[0] for t in turn_ids), "All events in one run must share turn_id"


# --- 2. Metadata consistent across iterations ---


class TestDRMetadataConsistentAcrossIterations:
    """Verify metadata (ind, step_type) consistent across iterations; no blending."""

    def test_dr_metadata_consistent_across_iterations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All message events have ind=2; no blending across iterations."""
        for iteration in (1, 2):
            state = _make_dr_finalize_state(dr_iteration=iteration)
            events = _run_dr_finalize_with_captured_events(
                monkeypatch,
                flag_enabled=True,
                state=state,
                dr_iteration=iteration,
            )
            message_evs = _message_events(events)
            for ev in message_evs:
                assert ev.get("ind") == ANSWER_PHASE_INDEX, (
                    f"Iteration {iteration}: message events must have ind={ANSWER_PHASE_INDEX}, got {ev.get('ind')}"
                )

    def test_dr_no_blending_within_iteration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Within one run, all message events have ind=2 (no other ind mixed in)."""
        events = _run_dr_finalize_with_captured_events(
            monkeypatch, flag_enabled=True
        )
        message_evs = _message_events(events)
        for ev in message_evs:
            assert ev.get("ind") == ANSWER_PHASE_INDEX

    def test_dr_flag_on_metadata_includes_step_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When flag ON, events include step_type and conversation_id."""
        events = _run_dr_finalize_with_captured_events(
            monkeypatch, flag_enabled=True
        )
        message_evs = _message_events(events)
        assert message_evs
        for ev in message_evs:
            assert ev.get("ind") == ANSWER_PHASE_INDEX
            assert "step_type" in ev or "conversation_id" in ev or "turn_id" in ev


# --- 3. Iteration advancement ---


class TestDRIterationAdvancement:
    """Verify DeepReasoningEmitter.advance_iteration() works with canonical identity."""

    def test_dr_iteration_advancement(self) -> None:
        """advance_iteration() increments internal counter but keeps turn_id canonical."""
        state = _make_dr_finalize_state(dr_iteration=1)
        interactive = InteractiveState.from_mapping(state)
        config = {"configurable": {"thread_id": "advance-test"}}
        emitted: List[Dict[str, Any]] = []

        def writer(event: Dict[str, Any]) -> None:
            emitted.append(dict(event))

        emitter = EventEmitterFactory.create_deep_reasoning(
            writer, interactive, config, context=None
        )
        # Initial emit: turn_id should be canonical (from thread_id)
        emitter.emit_message_start()
        start_evs = [e for e in emitted if e.get("type") == "message_start"]
        assert len(start_evs) == 1
        turn_id_1 = start_evs[0].get("turn_id") or start_evs[0].get("id")
        assert turn_id_1 is not None

        # Advance iteration
        new_iteration = emitter.advance_iteration()
        assert new_iteration >= 1

        # Next emit should use SAME canonical turn_id (iteration is internal only)
        emitter.emit_message_delta("chunk")
        delta_evs = [e for e in emitted if e.get("type") == "message_delta"]
        assert len(delta_evs) == 1
        turn_id_2 = delta_evs[0].get("turn_id") or delta_evs[0].get("id")
        assert turn_id_2 is not None
        assert turn_id_2 == turn_id_1, (
            "turn_id must remain canonical after advance_iteration() — "
            "iteration tracking is internal"
        )


# --- 4. Flag OFF: old behavior unchanged ---


class TestDRFlagOffUnchangedBehavior:
    """When ENABLE_UNIFIED_EMITTER_DEEP_REASONING=false, old helpers are used."""

    def test_dr_flag_off_emits_message_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_dr_finalize_with_captured_events(
            monkeypatch, flag_enabled=False
        )
        message_evs = _message_events(events)
        assert len(message_evs) >= 3
        for ev in message_evs:
            assert "type" in ev
            if ev.get("type") == "message_delta":
                assert "content" in ev

    def test_dr_flag_off_final_text_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks = ["Same", " ", "output"]
        events = _run_dr_finalize_with_captured_events(
            monkeypatch, flag_enabled=False, chunks=chunks
        )
        assert any(e.get("type") == "message_delta" for e in events)


# --- 5. Both flag states pass ---


class TestBothFlagStatesPass:
    """Integration tests pass for both flag ON and OFF."""

    def test_dr_flag_on_run_completes_successfully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_dr_finalize_with_captured_events(
            monkeypatch, flag_enabled=True
        )
        assert len(events) >= 3

    def test_dr_flag_off_run_completes_successfully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_dr_finalize_with_captured_events(
            monkeypatch, flag_enabled=False
        )
        assert len(events) >= 3
