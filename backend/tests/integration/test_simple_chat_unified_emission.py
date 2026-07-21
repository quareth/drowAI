""": Integration tests for simple chat migration to UnifiedEventEmitter.

Covers:
- Simple chat with flag ON produces complete metadata (ind=2 on all message events)
- Simple chat with flag OFF uses old helpers (unchanged behavior)
- Metadata includes ind=2 for all message events when flag ON
- Events group correctly by (turn_id, ind)
- No performance regression (< 5ms overhead per event)"""

from __future__ import annotations

import asyncio
import os
import logging
import time
from typing import Any, Dict, List

import pytest

# Set mock DATABASE_URL before backend/agent imports that may touch DB
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.contracts.streaming_constants import ANSWER_PHASE_INDEX
from agent.graph.nodes.simple_chat import run_simple_chat
from agent.graph.state import InteractiveInput, InteractiveState
from backend.services.usage_tracking.models import UsageData


def _empty_bundle(conversation_id: str = "test-conv") -> Dict[str, Any]:
    """Seed an empty ``ConversationContextBundle`` for direct-call tests.

    The simple-chat node's LLM path requires the bundle as the
    transcript-window authority; production wires it via
    ``LangGraphContextBuilder.build_runtime_config``.
    """
    return build_conversation_context_bundle(
        conversation_id=conversation_id,
        turn_id=f"{conversation_id}-turn-0",
        turn_sequence=0,
        messages=[],
    )


# --- Helpers ---


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


def _make_stalled_streaming_llm_client(chunks: List[str], *, stall_after_first_sec: float):
    """Return a mock LLMClient whose stream stalls after the first chunk."""

    async def _stalled_stream():
        for index, chunk in enumerate(chunks):
            if index == 1:
                await asyncio.sleep(stall_after_first_sec)
            yield chunk

    async def _healthy_stream():
        for chunk in chunks:
            yield chunk

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
            return _healthy_stream()

        async def stream_chat_messages_with_usage(self, *args: Any, **kwargs: Any):
            s = _StreamWithUsage()
            s.content_iterator = _stalled_stream()
            return s

    return _Client()


def _run_simple_chat_with_captured_events(
    monkeypatch: pytest.MonkeyPatch,
    *,
    flag_enabled: bool,
    chunks: List[str] | None = None,
    config: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Run run_simple_chat with a capturing writer; return list of emitted events."""
    events: List[Dict[str, Any]] = []

    def writer(event: Dict[str, Any]) -> None:
        events.append(dict(event))

    monkeypatch.setattr(
        "agent.graph.nodes.simple_chat.get_stream_writer",
        lambda: writer,
    )
    monkeypatch.setenv(
        "ENABLE_UNIFIED_EMITTER_SIMPLE_CHAT",
        "true" if flag_enabled else "false",
    )
    if chunks is None:
        chunks = ["Hello", " ", "world"]
    monkeypatch.setattr(
        "agent.graph.nodes.simple_chat.resolve_llm_client",
        lambda *args, **kwargs: _make_streaming_llm_client(chunks),
    )

    payload = InteractiveInput(
        task_id=10,
        message="Hi",
        metadata={
            "simple_chat_runtime": {"model": "stub"},
            METADATA_CONTEXT_BUNDLE_KEY: _empty_bundle(),
        },
    )
    state = payload.to_state().as_graph_state()
    if config is None:
        config = {"configurable": {"thread_id": "test-thread-10"}}

    import asyncio
    result = asyncio.run(run_simple_chat(state, context=None, config=config))
    updated = InteractiveState.from_mapping(result)
    assert updated.trace.final_text == "".join(chunks)
    return events


# --- 1. Flag ON: complete metadata ---


class TestSimpleChatFlagOnProducesCompleteMetadata:
    """When ENABLE_UNIFIED_EMITTER_SIMPLE_CHAT=true, events have complete metadata."""

    def test_simple_chat_flag_on_produces_complete_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_simple_chat_with_captured_events(
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
            assert "step_type" in ev
            assert "conversation_id" in ev
            assert "turn_id" in ev

    def test_simple_chat_flag_on_metadata_includes_ind_two(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_simple_chat_with_captured_events(
            monkeypatch, flag_enabled=True
        )
        message_events = [
            e for e in events
            if e.get("type") in ("message_start", "message_delta", "section_end")
        ]
        assert message_events, "No message-phase events captured"
        for ev in message_events:
            assert ev.get("ind") == ANSWER_PHASE_INDEX, (
                f"Event type={ev.get('type')} must have ind=2, got {ev.get('ind')}"
            )

    def test_simple_chat_flag_on_events_have_conversation_id_and_turn_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_simple_chat_with_captured_events(
            monkeypatch, flag_enabled=True
        )
        message_events = [
            e for e in events
            if e.get("type") in ("message_start", "message_delta", "section_end")
        ]
        for ev in message_events:
            assert ev.get("conversation_id") is not None
            assert ev.get("turn_id") is not None
            assert ev.get("streaming") is True


# --- 2. Flag OFF: old behavior unchanged ---


class TestSimpleChatFlagOffUnchangedBehavior:
    """When ENABLE_UNIFIED_EMITTER_SIMPLE_CHAT=false, old helpers are used."""

    def test_simple_chat_flag_off_uses_old_helpers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_simple_chat_with_captured_events(
            monkeypatch, flag_enabled=False
        )
        message_events = [
            e for e in events
            if e.get("type") in ("message_start", "message_delta", "section_end")
        ]
        assert len(message_events) >= 3, (
            "Flag OFF must still emit message_start, deltas, section_end"
        )
        # Old helpers also set ind=ANSWER_PHASE_INDEX; at least type and content shape unchanged
        for ev in message_events:
            assert "type" in ev
            if ev.get("type") == "message_delta":
                assert "content" in ev

    def test_simple_chat_flag_off_final_text_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunks = ["Same", " ", "output"]
        events = _run_simple_chat_with_captured_events(
            monkeypatch, flag_enabled=False, chunks=chunks
        )
        # Final text is asserted inside _run_simple_chat_with_captured_events
        assert any(e.get("type") == "message_delta" for e in events)


# --- 3. Events group by (turn_id, ind) ---


class TestEventsGroupByTurnIdAndInd:
    """Events can be grouped by (turn_id, ind) for frontend."""

    def test_events_group_correctly_by_turn_id_and_ind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_simple_chat_with_captured_events(
            monkeypatch, flag_enabled=True
        )
        message_events = [
            e for e in events
            if e.get("type") in ("message_start", "message_delta", "section_end")
        ]
        turn_ids = {e.get("turn_id") for e in message_events}
        inds = {e.get("ind") for e in message_events}
        assert len(turn_ids) == 1, "All message events should share one turn_id"
        assert inds == {ANSWER_PHASE_INDEX}, (
            f"All message events should have ind={ANSWER_PHASE_INDEX}"
        )


# --- 4. Performance ---


class TestPerformanceRegression:
    """No performance regression when using unified emitter."""

    def test_performance_regression(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: List[Dict[str, Any]] = []
        writer_calls = [0]

        def writer(event: Dict[str, Any]) -> None:
            events.append(dict(event))
            writer_calls[0] += 1

        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: writer,
        )
        monkeypatch.setenv("ENABLE_UNIFIED_EMITTER_SIMPLE_CHAT", "true")
        chunks = ["x"] * 50
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *args, **kwargs: _make_streaming_llm_client(chunks),
        )
        payload = InteractiveInput(
            task_id=11,
            message="Perf",
            metadata={
                "simple_chat_runtime": {"model": "stub"},
                METADATA_CONTEXT_BUNDLE_KEY: _empty_bundle("perf-conv"),
            },
        )
        state = payload.to_state().as_graph_state()
        config = {"configurable": {"thread_id": "perf-thread"}}

        import asyncio
        t0 = time.perf_counter()
        asyncio.run(run_simple_chat(state, context=None, config=config))
        elapsed_ms = (time.perf_counter() - t0) * 1000
        n_events = len(events)
        if n_events == 0:
            pytest.skip("No events captured (writer not used)")
        overhead_per_event_ms = elapsed_ms / n_events
        assert overhead_per_event_ms < 5.0, (
            f"Per-event overhead {overhead_per_event_ms:.2f}ms must be < 5ms "
            f"(total {elapsed_ms:.2f}ms for {n_events} events)"
        )


class TestNonStreamingFallback:
    """Visible chat falls back when streamed usage is unavailable."""

    @pytest.mark.asyncio
    async def test_writer_falls_back_to_usage_tracked_non_streaming_call(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A visible chat stream must not reject clients lacking streamed usage."""

        events: List[Dict[str, Any]] = []
        calls: List[Dict[str, Any]] = []

        def writer(event: Dict[str, Any]) -> None:
            events.append(dict(event))

        class _Response:
            content = "Hello from a compatible endpoint"
            usage = UsageData(
                prompt_tokens=8,
                completion_tokens=5,
                total_tokens=13,
                model="gpt-oss-20b",
                provider="openai",
                api_surface="chat_completions",
            )

        class _NonStreamingClient:
            async def chat_messages_with_usage(
                self,
                _messages: List[Dict[str, Any]],
                **kwargs: Any,
            ) -> _Response:
                calls.append(dict(kwargs))
                return _Response()

        class _CallSettings:
            provider = "openai"
            model = "gpt-oss-20b"
            reasoning_effort = None

        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: writer,
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *args, **kwargs: _NonStreamingClient(),
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_call_settings",
            lambda *args, **kwargs: _CallSettings(),
        )
        payload = InteractiveInput(
            task_id=13,
            message="Hi",
            metadata={
                "simple_chat_runtime": {"model": "stub"},
                METADATA_CONTEXT_BUNDLE_KEY: _empty_bundle("fallback-conv"),
            },
        )

        result = await run_simple_chat(
            payload.to_state().as_graph_state(),
            context=None,
            config={"configurable": {"thread_id": "fallback-thread"}},
        )

        updated = InteractiveState.from_mapping(result)
        assert updated.trace.final_error is None
        assert updated.trace.final_text == _Response.content
        assert len(calls) == 1
        assert calls[0]["temperature"] == 0.2
        assert calls[0]["max_tokens"] > 0
        assert "reasoning_effort" not in calls[0]
        assert any(
            event.get("type") == "message_delta"
            and event.get("content") == _Response.content
            for event in events
        )
        assert any(event.get("type") == "section_end" for event in events)
        assert updated.trace.usage_records[-1]["request_mode"] == "non_streaming"


class TestStreamingTimeouts:
    """Streaming idle timeouts are logged when chunk delivery stalls."""

    def test_simple_chat_stream_idle_timeout_logs_canonical_event(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        events: List[Dict[str, Any]] = []

        def writer(event: Dict[str, Any]) -> None:
            events.append(dict(event))

        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.get_stream_writer",
            lambda: writer,
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.LLM_STREAM_IDLE_TIMEOUT_CONVERSATION_MAIN_SEC",
            0.01,
        )
        monkeypatch.setattr(
            "agent.graph.nodes.simple_chat.resolve_llm_client",
            lambda *args, **kwargs: _make_stalled_streaming_llm_client(
                ["hello", " world"],
                stall_after_first_sec=0.05,
            ),
        )
        payload = InteractiveInput(
            task_id=12,
            message="Hi",
            metadata={
                "simple_chat_runtime": {"model": "stub"},
                METADATA_CONTEXT_BUNDLE_KEY: _empty_bundle("timeout-conv"),
            },
        )
        state = payload.to_state().as_graph_state()

        with caplog.at_level(logging.WARNING):
            result = asyncio.run(
                run_simple_chat(
                    state,
                    context=None,
                    config={"configurable": {"thread_id": "timeout-thread"}},
                )
            )

        updated = InteractiveState.from_mapping(result)
        assert updated.trace.final_text == ""
        assert "TIMEOUT | Task 12 | CONVERSATION_MAIN | simple_chat_stream" in caplog.text
        assert any(event.get("type") == "message_delta" and event.get("content") == "hello" for event in events)
        assert any(event.get("type") == "stream_error" for event in events)


# --- 5. Both flag states pass ---


class TestBothFlagStatesPass:
    """Integration tests pass for both flag ON and OFF."""

    def test_flag_on_run_completes_successfully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_simple_chat_with_captured_events(
            monkeypatch, flag_enabled=True
        )
        assert len(events) >= 3

    def test_flag_off_run_completes_successfully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = _run_simple_chat_with_captured_events(
            monkeypatch, flag_enabled=False
        )
        assert len(events) >= 3
