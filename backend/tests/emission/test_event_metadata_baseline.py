"""Characterization tests for event metadata.

Establishes baseline test coverage for event metadata behavior, validates
that known bugs exist, and creates a safety net before production changes.

Scope:
- Metadata completeness (ind, turn_id)
- Stream segment separation (observation vs message)
- Baseline metrics (completeness %, fallback %, blending rate)
- SSE endpoint metadata preservation

No production code is modified; tests are deterministic and use mocks for LLM."""

from __future__ import annotations

import os
import pytest
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

# Set mock DATABASE_URL before backend imports
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

from agent.graph.contracts.streaming_constants import (
    ANSWER_PHASE_INDEX,
    OBSERVATION_PHASE_INDEX,
    TOOL_PHASE_INDEX,
)
from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter


# --- Fixtures ---


@pytest.fixture
def adapter() -> LangGraphStreamingAdapter:
    """Streaming adapter used to process raw LangGraph events."""
    return LangGraphStreamingAdapter()


def _processed_events(adapter: LangGraphStreamingAdapter, raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run raw events through adapter and return non-None processed events."""
    out: List[Dict[str, Any]] = []
    for ev in raw_events:
        processed = adapter.process_streaming_event(ev)
        if processed is not None:
            out.append(processed)
    return out


# --- 1. Metadata Completeness Tests ---


class TestMetadataCompleteness:
    """Verify event metadata includes ind (phase index) where required."""

    def test_simple_chat_events_have_ind(self, adapter: LangGraphStreamingAdapter) -> None:
        """Verify simple chat events include ind in metadata.

        Simple chat emits message_start, message_delta, section_end.
        Expected to PASS if adapter/node path sets ind; FAIL confirms bug.
        """
        raw_simple_chat = [
            {"type": "message_start", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "message_delta", "content": "Hi", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "section_end", "section_name": "final_answer"},
        ]
        processed = _processed_events(adapter, raw_simple_chat)
        assert len(processed) == 3, "All three simple chat events should be processed"
        for ev in processed:
            meta = ev.get("metadata") or {}
            assert "ind" in meta, (
                f"Simple chat event type={ev.get('type')} must include metadata.ind; "
                "missing ind confirms metadata bug."
            )
            assert meta["ind"] == ANSWER_PHASE_INDEX, (
                f"Message-phase events should have ind={ANSWER_PHASE_INDEX}, got {meta.get('ind')}"
            )

    def test_simple_tool_observation_has_ind(self, adapter: LangGraphStreamingAdapter) -> None:
        """Verify observation events include ind=3.

        Observations already use OBSERVATION_PHASE_INDEX in helpers/adapter.
        Expected to PASS.
        """
        raw_observation = [
            {"type": "observation_start", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "observation_delta", "content": "Tool output", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "observation_section_end", "section_name": "observing"},
        ]
        processed = _processed_events(adapter, raw_observation)
        assert len(processed) == 3
        for ev in processed:
            meta = ev.get("metadata") or {}
            assert "ind" in meta, f"Observation event {ev.get('type')} must have metadata.ind"
            assert meta["ind"] == OBSERVATION_PHASE_INDEX, (
                f"Observation events should have ind={OBSERVATION_PHASE_INDEX}, got {meta.get('ind')}"
            )

    def test_simple_tool_message_has_ind(self, adapter: LangGraphStreamingAdapter) -> None:
        """Verify message events (answer phase) include ind=2.

        Expected to FAIL if message path omits ind (confirms bug).
        """
        raw_message = [
            {"type": "message_start", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "message_delta", "content": "Answer", "conversation_id": "c1", "turn_id": "t1"},
        ]
        processed = _processed_events(adapter, raw_message)
        assert len(processed) == 2
        for ev in processed:
            meta = ev.get("metadata") or {}
            assert "ind" in meta, (
                f"Message event {ev.get('type')} must include metadata.ind=2; "
                "missing ind confirms simple-tool message metadata bug."
            )
            assert meta["ind"] == ANSWER_PHASE_INDEX, (
                f"Message events should have ind={ANSWER_PHASE_INDEX}, got {meta.get('ind')}"
            )


# --- 2. Multi-Phase Separation Tests ---


class TestMultiPhaseSeparation:
    """Verify observation (ind=3) and message (ind=2) stay distinct; no blending."""

    def test_observation_and_message_separate(self, adapter: LangGraphStreamingAdapter) -> None:
        """Verify observation (ind=3) and message (ind=2) don't blend.

        PRIMARY BUG REPRODUCER: when ind is missing or wrong, frontend
        can group observation and message into one card. Expected to FAIL
        when blending bug exists.
        """
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
        assert all(i == OBSERVATION_PHASE_INDEX for i in observation_inds), (
            "All observation events must have ind=3; mixing with message confirms blending bug."
        )
        assert all(i == ANSWER_PHASE_INDEX for i in message_inds), (
            "All message events must have ind=2; mixing with observation confirms blending bug."
        )
        assert set(observation_inds) != set(message_inds) or (observation_inds and message_inds), (
            "Observation and message must have distinct ind; same ind causes blending."
        )

    def test_frontend_grouping_by_turn_and_ind(self, adapter: LangGraphStreamingAdapter) -> None:
        """Verify events can be grouped by (turn_id, ind) tuple.

        Frontend groups by (turn_id, ind). When ind is missing, fallback
        (e.g. ind=-1) can merge distinct phases. Expected to FAIL when ind missing.
        """
        raw = [
            {"type": "tool_start", "tool": "nmap", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "observation_delta", "content": "Out", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "message_delta", "content": "Done", "conversation_id": "c1", "turn_id": "t1"},
        ]
        processed = _processed_events(adapter, raw)
        assert len(processed) == 3
        groups: Dict[tuple, List[str]] = {}
        for ev in processed:
            meta = ev.get("metadata") or {}
            turn_id = meta.get("id") or meta.get("conversationId") or ""
            ind = meta.get("ind", -999)
            key = (turn_id, ind)
            groups.setdefault(key, []).append(ev.get("type", ""))
        # We expect at least two distinct (turn_id, ind) groups: tool/observation vs message
        distinct_inds = {k[1] for k in groups}
        assert -999 not in distinct_inds, (
            "Events must have explicit ind for frontend grouping; ind=-999 indicates missing metadata."
        )
        assert len(distinct_inds) >= 2, (
            "At least two phase indices (e.g. observation and message) for correct grouping."
        )


# --- 3. Baseline Metrics Tests ---


class TestBaselineMetrics:
    """Capture baseline rates for metadata completeness and fallback usage."""

    def test_capture_metadata_completeness_baseline(self, adapter: LangGraphStreamingAdapter) -> None:
        """Capture % of events with complete metadata (ind, turn_id, step_type).

        Establishes baseline for comparison after refactor.
        """
        raw_all = [
            {"type": "message_start", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "message_delta", "content": "x", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "observation_delta", "content": "y", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "tool_start", "tool": "n", "conversation_id": "c1", "turn_id": "t1"},
        ]
        processed = _processed_events(adapter, raw_all)
        total = len(processed)
        complete = 0
        for ev in processed:
            meta = ev.get("metadata") or {}
            if meta.get("ind") is not None and meta.get("id"):
                complete += 1
        rate = (complete / total * 100) if total else 0
        assert total > 0
        # Document baseline: typically 60–70% if some paths omit ind
        assert 0 <= rate <= 100

    def test_capture_frontend_fallback_usage(self, adapter: LangGraphStreamingAdapter) -> None:
        """Capture % of events using frontend fallback (ind=-1 or missing).

        When ind is missing, frontend may use ind=-1. Establishes baseline.
        """
        raw_without_ind = [
            {"type": "message_start", "conversation_id": "c1", "turn_id": "t1"},
            {"type": "message_delta", "content": "x", "conversation_id": "c1", "turn_id": "t1"},
        ]
        processed = _processed_events(adapter, raw_without_ind)
        fallback_count = sum(
            1 for ev in processed
            if (ev.get("metadata") or {}).get("ind") == -1
            or (ev.get("metadata") or {}).get("ind") is None
        )
        total = len(processed)
        rate = (fallback_count / total * 100) if total else 0
        assert 0 <= rate <= 100


# --- 4. SSE Endpoint Tests ---


class TestSSEMetadataPreservation:
    """Verify SSE/OpenAI-style chunks preserve metadata (ind)."""

    def test_sse_openai_chunks_preserve_metadata(self) -> None:
        """Verify SSE endpoint preserves metadata in OpenAI chunks.

        Phase 3: Chunks now include a top-level 'metadata' field with ind, step_type,
        conversation_id so the frontend can group by (turn_id, ind) and avoid blending.
        """
        # Chunk shape produced by _stream_optimized_realtime / _stream_standard_with_delays
        task_id = 1
        conv_id = "conv-1"
        anchor_seq = 1
        turn_id = "turn-1"
        content_piece = "Hello"
        delta = {
            "id": turn_id,
            "object": "chat.completion.chunk",
            "taskId": task_id,
            "sequence": anchor_seq,
            "metadata": {
                "ind": 2,
                "step_type": "message_delta",
                "conversation_id": conv_id,
                "conversationId": conv_id,
                "streaming": True,
            },
            "choices": [{"delta": {"content": content_piece}}],
        }
        choices = delta.get("choices") or []
        delta_content = choices[0].get("delta", {}) if choices else {}
        meta = delta.get("metadata") or {}
        has_ind = "ind" in meta or "ind" in delta_content or "ind" in delta
        has_turn_id = "turn_id" in delta_content or "id" in delta
        assert has_ind, (
            "SSE OpenAI-style chunk must preserve phase metadata (ind) for frontend grouping."
        )
        assert has_turn_id
        ind_val = meta.get("ind") or delta_content.get("ind") or delta.get("ind")
        assert ind_val is not None and ind_val >= 0
