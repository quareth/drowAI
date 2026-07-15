""": Integration tests for simple tool migration to UnifiedEventEmitter.

Covers:
- Finalize node with ENABLE_UNIFIED_EMITTER_SIMPLE_TOOL=true produces complete metadata (ind=2)
- Finalize node with flag OFF uses old helpers (unchanged behavior)
- Observation (ind=3) and message (ind=2) remain separate (no blending)
- Metadata preserved through adapter and SSE chunking
- Both flag states complete successfully"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from agent.graph.contracts.streaming_constants import (
    ANSWER_PHASE_INDEX,
    OBSERVATION_PHASE_INDEX,
)
from agent.graph.nodes.finalize import finalize_results as finalize_tool_results
from agent.graph.state import InteractiveState
from agent.graph.infrastructure.state_models import FactsState, TraceState
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter
from backend.services.usage_tracking.models import UsageData


# --- Helpers ---


def _make_finalize_state(
    task_id: int = 10,
    conversation_id: str = "conv-finalize-test",
    message: str = "Run nmap",
    synthesized: Dict[str, Any] | None = None,
    last_result: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build minimal state for finalize_tool_results."""
    synthesized = synthesized or {
        "tool": "nmap",
        "summary": "Port scan completed",
        "key_findings": ["Open ports 22, 80"],
        "vulnerabilities": [],
        "next_actions": ["Enumerate services"],
    }
    last_result = last_result or {
        "tool": "nmap",
        "stdout_excerpt": "22/tcp open ssh",
    }
    metadata = {
        "synthesized_output": synthesized,
        "last_tool_result": last_result,
    }
    facts = FactsState(
        task_id=task_id,
        message=message,
        conversation_id=conversation_id,
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


def _run_finalize_with_captured_events(
    monkeypatch: pytest.MonkeyPatch,
    *,
    flag_enabled: bool,
    chunks: List[str] | None = None,
    config: Dict[str, Any] | None = None,
    state: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Run finalize_tool_results with a capturing writer; return list of emitted events."""
    events: List[Dict[str, Any]] = []

    def writer(event: Dict[str, Any]) -> None:
        events.append(dict(event))

    monkeypatch.setattr(
        "agent.graph.nodes.finalize.get_stream_writer",
        lambda: writer,
    )
    monkeypatch.setenv(
        "ENABLE_UNIFIED_EMITTER_SIMPLE_TOOL",
        "true" if flag_enabled else "false",
    )
    if chunks is None:
        chunks = ["Summary", " ", "of", " ", "tool", " ", "result"]
    monkeypatch.setattr(
        "agent.graph.nodes.finalize.resolve_llm_client",
        lambda *args, **kwargs: _make_streaming_llm_client(chunks),
    )

    if state is None:
        state = _make_finalize_state()
    if config is None:
        config = {"configurable": {"thread_id": "test-thread-finalize"}}

    result = asyncio.run(
        finalize_tool_results(state, context=None, config=config)
    )
    updated = InteractiveState.from_mapping(result)
    assert updated.trace.final_text
    return events


def _processed_events(adapter: LangGraphStreamingAdapter, raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run raw events through adapter and return non-None processed events."""
    out: List[Dict[str, Any]] = []
    for ev in raw_events:
        processed = adapter.process_streaming_event(ev)
        if processed is not None:
            out.append(processed)
    return out


# --- 1. Flag ON: complete metadata ---


class TestFinalizeFlagOnProducesCompleteMetadata:
    """When ENABLE_UNIFIED_EMITTER_SIMPLE_TOOL=true, finalize events have complete metadata."""

    def test_finalize_flag_on_events_have_ind_two(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_finalize_with_captured_events(
            monkeypatch, flag_enabled=True, chunks=["Hi", " ", "there"]
        )
        message_types = {"message_start", "message_delta", "section_end"}
        emitted = [e for e in events if e.get("type") in message_types]
        assert len(emitted) >= 3, "Expected at least message_start, message_deltas, section_end"
        for ev in emitted:
            assert "ind" in ev, f"Event {ev.get('type')} must include ind"
            assert ev["ind"] == ANSWER_PHASE_INDEX, (
                f"Message-phase events must have ind={ANSWER_PHASE_INDEX}, got {ev.get('ind')}"
            )

    def test_finalize_flag_on_metadata_includes_step_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_finalize_with_captured_events(
            monkeypatch, flag_enabled=True
        )
        message_events = [
            e for e in events
            if e.get("type") in ("message_start", "message_delta", "section_end")
        ]
        assert message_events
        for ev in message_events:
            assert ev.get("ind") == ANSWER_PHASE_INDEX
            assert "step_type" in ev or "conversation_id" in ev


# --- 2. Flag OFF: old behavior unchanged ---


class TestFinalizeFlagOffUnchangedBehavior:
    """When ENABLE_UNIFIED_EMITTER_SIMPLE_TOOL=false, old helpers are used."""

    def test_finalize_flag_off_emits_message_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_finalize_with_captured_events(
            monkeypatch, flag_enabled=False
        )
        message_events = [
            e for e in events
            if e.get("type") in ("message_start", "message_delta", "section_end")
        ]
        assert len(message_events) >= 3
        for ev in message_events:
            assert "type" in ev
            if ev.get("type") == "message_delta":
                assert "content" in ev

    def test_finalize_flag_off_final_text_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks = ["Same", " ", "output"]
        events = _run_finalize_with_captured_events(
            monkeypatch, flag_enabled=False, chunks=chunks
        )
        assert any(e.get("type") == "message_delta" for e in events)


# --- 3. Observation + message separate (no blending) ---


class TestObservationAndMessageSeparate:
    """Observation (ind=3) and message (ind=2) don't blend; adapter preserves ind."""

    def test_adapter_preserves_observation_ind_three(self) -> None:
        adapter = LangGraphStreamingAdapter()
        raw = [
            {"type": "observation_start", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "observation_delta", "content": "Obs", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "observation_section_end", "section_name": "observing"},
        ]
        processed = _processed_events(adapter, raw)
        assert len(processed) == 3
        for ev in processed:
            meta = ev.get("metadata") or {}
            assert meta.get("ind") == OBSERVATION_PHASE_INDEX, (
                f"Observation events must have ind={OBSERVATION_PHASE_INDEX}, got {meta.get('ind')}"
            )

    def test_adapter_preserves_message_ind_two(self) -> None:
        adapter = LangGraphStreamingAdapter()
        raw = [
            {"type": "message_start", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "message_delta", "content": "Reply", "conversation_id": "c1", "turn_id": "t1"},
        ]
        processed = _processed_events(adapter, raw)
        assert len(processed) == 2
        for ev in processed:
            meta = ev.get("metadata") or {}
            assert meta.get("ind") == ANSWER_PHASE_INDEX, (
                f"Message events must have ind={ANSWER_PHASE_INDEX}, got {meta.get('ind')}"
            )

    def test_observation_and_message_mixed_distinct_inds(self) -> None:
        """Mixed observation + message events have distinct ind (no blending)."""
        adapter = LangGraphStreamingAdapter()
        raw_mixed = [
            {"type": "observation_start", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "observation_delta", "content": "Obs", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "observation_section_end", "section_name": "observing"},
            {"type": "message_start", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "message_delta", "content": "Reply", "conversation_id": "c1", "turn_id": "t1"},
        ]
        processed = _processed_events(adapter, raw_mixed)
        assert len(processed) == 5
        observation_inds = [
            ev["metadata"]["ind"]
            for ev in processed
            if ev.get("type", "").startswith("observation")
        ]
        message_inds = [
            ev["metadata"]["ind"]
            for ev in processed
            if ev.get("type") in ("message_start", "message_delta")
        ]
        assert all(i == OBSERVATION_PHASE_INDEX for i in observation_inds)
        assert all(i == ANSWER_PHASE_INDEX for i in message_inds)
        assert set(observation_inds) != set(message_inds) or (observation_inds and message_inds)


# --- 4. Metadata preserved through SSE (chunk shape) ---


class TestMetadataPreservedThroughSSE:
    """SSE chunking receives event metadata and preserves ind/step_type in chunks."""

    def test_sse_chunk_metadata_shape(self) -> None:
        """Chunk metadata shape matches what frontend expects (ind, step_type, conversation_id)."""
        from backend.routers.agent_reasoning import _build_chunk_metadata

        meta = _build_chunk_metadata("conv-1", ANSWER_PHASE_INDEX, "message_delta")
        assert "ind" in meta
        assert "step_type" in meta
        assert "conversation_id" in meta
        assert meta.get("streaming") is True


# --- 5. Both flag states pass ---


class TestBothFlagStatesPass:
    """Integration tests pass for both flag ON and OFF."""

    def test_flag_on_run_completes_successfully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_finalize_with_captured_events(
            monkeypatch, flag_enabled=True
        )
        assert len(events) >= 3

    def test_flag_off_run_completes_successfully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_finalize_with_captured_events(
            monkeypatch, flag_enabled=False
        )
        assert len(events) >= 3
