"""Tests for completion callback pattern.

Covers:
- Normal completion (LLM finishes)
- Cancellation (client disconnects)
- Error (LLM raises exception)
- Streaming order/latency
- ChatMessage update from ChatStateContainer
- StreamEmitter queue behavior
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import backend.services.langgraph_chat.execution.completion_callback as completion_callback_module
from backend.services.langgraph_chat.execution.completion_callback import (
    StreamEmitter,
    persist_chat_message_from_container,
    run_turn_with_completion_callback,
)
from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer
from backend.database import Base
from backend.models.chat import ChatMessage, ChatTurnEvent
from backend.models.core import Task, User
from backend.models.hitl import TurnWorkflow


class TestCompletionCallbackNormalCompletion:
    """Tests for normal completion scenario."""

    @pytest.mark.asyncio
    async def test_callback_streams_events(self):
        """Events stream when LLM completes normally."""

        async def mock_llm(emitter: StreamEmitter):
            await emitter.emit({"type": "reasoning_start", "content": ""})
            await emitter.emit({"type": "reasoning_delta", "content": "Thinking..."})
            await emitter.emit({"type": "reasoning_section_end", "content": ""})
            return "Final response"

        events = []
        async for event in run_turn_with_completion_callback(
            turn_id="turn-1",
            turn_number=1,
            task_id=123,
            conversation_id="conv-1",
            llm_func=mock_llm,
            is_connected=lambda: True,
        ):
            events.append(event)

        assert len(events) == 3
        assert events[0]["type"] == "reasoning_start"
        assert events[1]["type"] == "reasoning_delta"
        assert events[2]["type"] == "reasoning_section_end"

    @pytest.mark.asyncio
    async def test_streaming_latency_unchanged(self):
        """Events should stream immediately, not wait for completion."""
        import time

        stream_times = []

        async def mock_llm(emitter: StreamEmitter):
            for i in range(5):
                await emitter.emit({"type": "delta", "content": f"Event {i}"})
                await asyncio.sleep(0.1)  # Simulate processing
            return "Done"

        start_time = time.time()
        async for _event in run_turn_with_completion_callback(
            turn_id="turn-1",
            turn_number=1,
            task_id=1,
            conversation_id="conv-1",
            llm_func=mock_llm,
            is_connected=lambda: True,
        ):
            stream_times.append(time.time() - start_time)

        # First event should arrive quickly (< 200ms)
        assert stream_times[0] < 0.2

        # Events should be spaced out (not all at once at the end)
        time_deltas = [stream_times[i + 1] - stream_times[i] for i in range(len(stream_times) - 1)]
        avg_delta = sum(time_deltas) / len(time_deltas)
        assert 0.08 < avg_delta < 0.15


class TestCompletionCallbackCancellation:
    """Tests for cancellation scenario."""

    @pytest.mark.asyncio
    async def test_callback_ignores_disconnect_without_explicit_cancel(self):
        """Disconnect checker is transport-only and does not cancel backend run."""
        connected = True

        async def mock_llm(emitter: StreamEmitter):
            await emitter.emit({"type": "reasoning_start", "content": ""})
            await emitter.emit({"type": "reasoning_delta", "content": "Thinking..."})
            await emitter.emit({"type": "reasoning_section_end", "content": ""})
            return "Should still complete"

        events = []

        async for event in run_turn_with_completion_callback(
            turn_id="turn-1",
            turn_number=1,
            task_id=1,
            conversation_id="conv-1",
            llm_func=mock_llm,
            is_connected=lambda: connected,
        ):
            events.append(event)
            if len(events) == 2:
                connected = False

        assert len(events) == 3

    @pytest.mark.asyncio
    async def test_callback_stops_on_explicit_cancel(self):
        """Explicit lifecycle cancel stops streaming and cancels the run."""
        cancel_requested = False

        async def mock_llm(emitter: StreamEmitter):
            await emitter.emit({"type": "reasoning_start", "content": ""})
            await emitter.emit({"type": "reasoning_delta", "content": "Thinking..."})
            await asyncio.sleep(10)
            await emitter.emit({"type": "reasoning_section_end", "content": ""})
            return "Should not reach here"

        result_holder = {}
        events = []

        async for event in run_turn_with_completion_callback(
            turn_id="turn-1",
            turn_number=1,
            task_id=1,
            conversation_id="conv-1",
            llm_func=mock_llm,
            should_cancel=lambda: cancel_requested,
            result_holder=result_holder,
        ):
            events.append(event)
            if len(events) == 2:
                cancel_requested = True

        assert len(events) == 2
        assert result_holder.get("cancelled") is True

    @pytest.mark.asyncio
    async def test_explicit_cancel_persists_stopped_assistant_message(self):
        """Explicit cancel finalizes an empty assistant row as stopped."""
        cancel_requested = False
        state_container = ChatStateContainer()

        async def mock_llm(emitter: StreamEmitter):
            await emitter.emit({"type": "reasoning_start", "content": ""})
            await asyncio.sleep(10)
            return "Should not reach here"

        db = MagicMock()
        with patch(
            "backend.services.langgraph_chat.execution.completion_callback.SessionLocal",
            return_value=db,
        ), patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatMessageService"
        ) as mock_chat_service, patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatTurnEventService"
        ) as mock_turn_event_service:
            message_service_instance = mock_chat_service.return_value
            event_service_instance = mock_turn_event_service.return_value

            async for _event in run_turn_with_completion_callback(
                turn_id="turn-cancel-empty",
                turn_number=5,
                task_id=5,
                conversation_id="conv-5",
                llm_func=mock_llm,
                should_cancel=lambda: cancel_requested,
                state_container=state_container,
                reserved_message_id=555,
            ):
                cancel_requested = True

            message_service_instance.update_message.assert_called_once()
            call_args = message_service_instance.update_message.call_args
            assert call_args.args[0] == 555
            assert call_args.args[1] == "[Stopped]"
            assert call_args.kwargs.get("error") == "run_cancelled"
            event_service_instance.merge_events_for_message.assert_called_once_with(
                task_id=5,
                conversation_id="conv-5",
                chat_message_id=555,
                turn_number=5,
                reasoning_sections=None,
                tool_calls=None,
                observation_sections=[],
            )
            db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_explicit_cancel_preserves_partial_state(self):
        """Explicit cancel persists partial answer/reasoning/tool state."""
        cancel_requested = False
        state_container = ChatStateContainer()

        async def mock_llm(emitter: StreamEmitter):
            state_container.append_answer("Partial answer")
            state_container.append_reasoning("Partial reasoning")
            state_container.add_tool_call({
                "tool_call_id": "tc-cancel",
                "tool_name": "dummy_tool",
                "tool_arguments": {"target": "example"},
                "tool_result": "partial result",
                "turn_index": 0,
            })
            await emitter.emit({"type": "answer_delta", "content": "Partial answer"})
            await asyncio.sleep(10)
            return "Should not reach here"

        db = MagicMock()
        with patch(
            "backend.services.langgraph_chat.execution.completion_callback.SessionLocal",
            return_value=db,
        ), patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatMessageService"
        ) as mock_chat_service, patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatTurnEventService"
        ) as mock_turn_event_service:
            message_service_instance = mock_chat_service.return_value
            event_service_instance = mock_turn_event_service.return_value

            async for _event in run_turn_with_completion_callback(
                turn_id="turn-cancel-partial",
                turn_number=6,
                task_id=6,
                conversation_id="conv-6",
                llm_func=mock_llm,
                should_cancel=lambda: cancel_requested,
                state_container=state_container,
                reserved_message_id=666,
            ):
                cancel_requested = True

            message_service_instance.update_message.assert_called_once()
            call_args = message_service_instance.update_message.call_args
            assert call_args.args[0] == 666
            assert call_args.args[1] == "Partial answer"
            assert call_args.kwargs.get("reasoning_tokens") == "Partial reasoning"
            assert call_args.kwargs.get("error") == "run_cancelled"
            assert isinstance(call_args.kwargs.get("tool_calls"), list)
            event_service_instance.merge_events_for_message.assert_called_once_with(
                task_id=6,
                conversation_id="conv-6",
                chat_message_id=666,
                turn_number=6,
                reasoning_sections=None,
                tool_calls=call_args.kwargs.get("tool_calls"),
                observation_sections=[],
            )
            db.commit.assert_called_once()


class TestCompletionCallbackError:
    """Tests for error scenario."""

    @pytest.mark.asyncio
    async def test_callback_propagates_error(self):
        """Exception from LLM is re-raised after streaming prior events."""

        async def mock_llm(emitter: StreamEmitter):
            await emitter.emit({"type": "reasoning_start", "content": ""})
            await emitter.emit({"type": "reasoning_delta", "content": "Thinking..."})
            raise ValueError("LLM processing error")

        events = []
        with pytest.raises(ValueError, match="LLM processing error"):
            async for event in run_turn_with_completion_callback(
                turn_id="turn-1",
                turn_number=1,
                task_id=1,
                conversation_id="conv-1",
                llm_func=mock_llm,
                is_connected=lambda: True,
            ):
                events.append(event)

        assert len(events) == 2

    @pytest.mark.asyncio
    async def test_callback_error_does_not_persist_cancelled_message(self):
        """Generic LLM errors do not use explicit-cancel persistence."""
        state_container = ChatStateContainer()
        state_container.append_answer("Partial answer")

        async def mock_llm(_emitter: StreamEmitter):
            raise ValueError("LLM processing error")

        db = MagicMock()
        with patch(
            "backend.services.langgraph_chat.execution.completion_callback.SessionLocal",
            return_value=db,
        ) as mock_session, patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatMessageService"
        ) as mock_chat_service:
            with pytest.raises(ValueError, match="LLM processing error"):
                async for _event in run_turn_with_completion_callback(
                    turn_id="turn-error",
                    turn_number=7,
                    task_id=7,
                    conversation_id="conv-7",
                    llm_func=mock_llm,
                    state_container=state_container,
                    reserved_message_id=777,
                ):
                    pass

            mock_session.assert_not_called()
            mock_chat_service.assert_not_called()


class TestCompletionCallbackEventOrder:
    """Tests for event order preservation."""

    @pytest.mark.asyncio
    async def test_callback_preserves_event_order(self):
        """Events stream in the same order they are emitted."""

        async def mock_llm(emitter: StreamEmitter):
            for i in range(10):
                await emitter.emit({"type": "event", "content": f"Event {i}", "index": i})
            return "Done"

        streamed_events = []
        async for event in run_turn_with_completion_callback(
            turn_id="turn-1",
            turn_number=1,
            task_id=1,
            conversation_id="conv-1",
            llm_func=mock_llm,
            is_connected=lambda: True,
        ):
            streamed_events.append(event)

        for i, event in enumerate(streamed_events):
            assert event["index"] == i


class TestCompletionCallbackEdgeCases:
    """Tests for edge cases."""

    @pytest.mark.asyncio
    async def test_empty_llm_function(self):
        """LLM function that emits no events."""

        async def empty_llm(_emitter: StreamEmitter):
            return "Just a message, no events"

        events = []
        async for event in run_turn_with_completion_callback(
            turn_id="turn-1",
            turn_number=1,
            task_id=1,
            conversation_id="conv-1",
            llm_func=empty_llm,
            is_connected=lambda: True,
        ):
            events.append(event)

        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_llm_returns_none(self):
        """LLM function that returns None instead of message."""

        async def no_return_llm(emitter: StreamEmitter):
            await emitter.emit({"type": "event", "content": "Event"})

        events = []
        async for event in run_turn_with_completion_callback(
            turn_id="turn-1",
            turn_number=1,
            task_id=1,
            conversation_id="conv-1",
            llm_func=no_return_llm,
            is_connected=lambda: True,
        ):
            events.append(event)

        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_cleanup_timeout_logs_langgraph_timeout_event(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
        """Cleanup wait timeout is logged through the shared LangGraph timeout hook."""

        original_wait_for = asyncio.wait_for
        timeout_events: list[tuple] = []
        cancel_requested = False

        async def hanging_llm(emitter: StreamEmitter):
            await emitter.emit({"type": "event", "content": "Event"})
            await asyncio.sleep(10)
            return "Done"

        async def patched_wait_for(awaitable, timeout):
            if timeout == 5.0:
                raise asyncio.TimeoutError()
            return await original_wait_for(awaitable, timeout)

        def record_timeout(*args, **kwargs):
            timeout_events.append((args, kwargs))

        monkeypatch.setattr(completion_callback_module.asyncio, "wait_for", patched_wait_for)
        monkeypatch.setattr(completion_callback_module, "log_timeout_event", record_timeout)

        events = []
        with caplog.at_level("WARNING"):
            async for event in run_turn_with_completion_callback(
                turn_id="turn-1",
                turn_number=1,
                task_id=1,
                conversation_id="conv-1",
                llm_func=hanging_llm,
                should_cancel=lambda: cancel_requested,
            ):
                events.append(event)
                cancel_requested = True

        assert len(events) == 1
        assert "LLM task timeout during cleanup" in caplog.text
        assert timeout_events == [
            (
                (1, "COMPLETION_CALLBACK", "llm_cleanup_wait", 5.0, "task_cancelled", "turn_id=turn-1"),
                {},
            )
        ]


class TestChatMessageUpdate:
    """Tests for ChatMessage persistence from state container."""

    @pytest.mark.asyncio
    async def test_updates_chat_message_from_state_container(self):
        """ChatMessage update is invoked from state container on completion."""
        state_container = ChatStateContainer()
        state_container.append_answer("Final answer")
        state_container.append_reasoning("Reasoning")
        state_container.start_observation()
        state_container.append_observation("Observed host up")
        state_container.end_observation()
        state_container.add_tool_call({
            "tool_call_id": "tc-1",
            "tool_name": "dummy_tool",
            "tool_arguments": {"arg": "value"},
            "tool_result": "ok",
            "turn_index": 0,
        })

        async def mock_llm(_emitter: StreamEmitter):
            return "Ignored"

        db = MagicMock()
        with patch(
            "backend.services.langgraph_chat.execution.completion_callback.SessionLocal",
            return_value=db,
        ) as mock_session, patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatMessageService"
        ) as mock_chat_service, patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatTurnEventService"
        ) as mock_turn_event_service:
            message_service_instance = mock_chat_service.return_value
            event_service_instance = mock_turn_event_service.return_value

            async for _event in run_turn_with_completion_callback(
                turn_id="turn-1",
                turn_number=1,
                task_id=1,
                conversation_id="conv-1",
                llm_func=mock_llm,
                is_connected=lambda: True,
                state_container=state_container,
                reserved_message_id=321,
            ):
                pass

            mock_session.assert_called_once()
            mock_chat_service.assert_called_once_with(db)
            mock_turn_event_service.assert_called_once_with(db)
            message_service_instance.update_message.assert_called_once()
            call_args = message_service_instance.update_message.call_args
            assert call_args.args[0] == 321
            assert call_args.args[1] == "Ignored"
            assert call_args.kwargs.get("reasoning_tokens") == "Reasoning"
            assert call_args.kwargs.get("observation_tokens") == '[{"content": "Observed host up", "phase_sequence": 0}]'
            assert isinstance(call_args.kwargs.get("tool_calls"), list)
            event_service_instance.merge_events_for_message.assert_called_once_with(
                task_id=1,
                conversation_id="conv-1",
                chat_message_id=321,
                turn_number=1,
                reasoning_sections=None,
                tool_calls=call_args.kwargs.get("tool_calls"),
                observation_sections=[{"content": "Observed host up", "phase_sequence": 0}],
            )
            db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_persists_partial_on_hitl_interrupt(self):
        """Partial state is persisted when HITL interrupt is signaled."""
        state_container = ChatStateContainer()
        state_container.append_answer("Partial answer")
        state_container.append_reasoning("Partial reasoning")
        state_container.start_observation()
        state_container.append_observation("Partial observation")
        state_container.end_observation()

        async def mock_llm(_emitter: StreamEmitter, result_holder: dict):
            result_holder["interrupted"] = True
            return None

        db = MagicMock()
        with patch(
            "backend.services.langgraph_chat.execution.completion_callback.SessionLocal",
            return_value=db,
        ) as mock_session, patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatMessageService"
        ) as mock_chat_service, patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatTurnEventService"
        ) as mock_turn_event_service:
            message_service_instance = mock_chat_service.return_value
            event_service_instance = mock_turn_event_service.return_value

            async for _event in run_turn_with_completion_callback(
                turn_id="turn-2",
                turn_number=2,
                task_id=2,
                conversation_id="conv-2",
                llm_func=mock_llm,
                is_connected=lambda: True,
                state_container=state_container,
                reserved_message_id=456,
            ):
                pass

            mock_session.assert_called_once()
            mock_chat_service.assert_called_once_with(db)
            mock_turn_event_service.assert_called_once_with(db)
            message_service_instance.update_message.assert_called_once()
            call_args = message_service_instance.update_message.call_args
            assert call_args.args[0] == 456
            assert call_args.args[1] == "Partial answer"
            assert call_args.kwargs.get("reasoning_tokens") == "Partial reasoning"
            assert call_args.kwargs.get("observation_tokens") == '[{"content": "Partial observation", "phase_sequence": 0}]'
            assert call_args.kwargs.get("error") == "interrupted"
            event_service_instance.merge_events_for_message.assert_called_once_with(
                task_id=2,
                conversation_id="conv-2",
                chat_message_id=456,
                turn_number=2,
                reasoning_sections=None,
                tool_calls=call_args.kwargs.get("tool_calls"),
                observation_sections=[{"content": "Partial observation", "phase_sequence": 0}],
            )
            db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_persists_prefill_reasoning_when_turn_has_no_graph_reasoning(self):
        """Live-only intent-phase reasoning should survive refresh via ChatMessage persistence."""
        state_container = ChatStateContainer()
        state_container.append_answer("Final answer")

        async def mock_llm(_emitter: StreamEmitter):
            return "Ignored"

        db = MagicMock()
        with patch(
            "backend.services.langgraph_chat.execution.completion_callback.SessionLocal",
            return_value=db,
        ), patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatMessageService"
        ) as mock_chat_service, patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatTurnEventService"
        ):
            message_service_instance = mock_chat_service.return_value

            async for _event in run_turn_with_completion_callback(
                turn_id="turn-3",
                turn_number=3,
                task_id=3,
                conversation_id="conv-3",
                llm_func=mock_llm,
                is_connected=lambda: True,
                state_container=state_container,
                reserved_message_id=789,
                prefill_reasoning_tokens="Analyzing request and deciding execution path.",
            ):
                pass

            call_args = message_service_instance.update_message.call_args
            assert call_args is not None
            assert (
                call_args.kwargs.get("reasoning_tokens")
                == "Analyzing request and deciding execution path."
            )

    @pytest.mark.asyncio
    async def test_merges_prefill_reasoning_with_graph_reasoning(self):
        """Persisted reasoning should keep facade-owned intent text ahead of graph reasoning."""
        state_container = ChatStateContainer()
        state_container.append_answer("Final answer")
        state_container.append_reasoning("Planning the next action.")

        async def mock_llm(_emitter: StreamEmitter):
            return "Ignored"

        db = MagicMock()
        with patch(
            "backend.services.langgraph_chat.execution.completion_callback.SessionLocal",
            return_value=db,
        ), patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatMessageService"
        ) as mock_chat_service, patch(
            "backend.services.langgraph_chat.execution.completion_callback.ChatTurnEventService"
        ):
            message_service_instance = mock_chat_service.return_value

            async for _event in run_turn_with_completion_callback(
                turn_id="turn-4",
                turn_number=4,
                task_id=4,
                conversation_id="conv-4",
                llm_func=mock_llm,
                is_connected=lambda: True,
                state_container=state_container,
                reserved_message_id=790,
                prefill_reasoning_tokens="Analyzing request and deciding execution path.",
            ):
                pass

            call_args = message_service_instance.update_message.call_args
            assert call_args is not None
            assert (
                call_args.kwargs.get("reasoning_tokens")
                == "Analyzing request and deciding execution path.\n\nPlanning the next action."
            )

    def test_persist_container_merges_resume_segments_without_erasing_prior_events(self):
        """Persisting multiple segments for one message should append canonical turn events."""

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        seed_db = session_factory()
        try:
            user = User(username="completion-callback-user", password="secret")
            seed_db.add(user)
            seed_db.flush()

            task = Task(user_id=user.id, name="completion-callback-task")
            seed_db.add(task)
            seed_db.flush()

            message = ChatMessage(
                task_id=task.id,
                conversation_id="conv-1",
                parent_message_id=None,
                latest_child_message_id=None,
                message_type="assistant",
                message="",
                token_count=0,
                turn_number=1,
            )
            seed_db.add(message)
            seed_db.commit()
            message_id = int(message.id)
            task_id = int(task.id)
        finally:
            seed_db.close()

        first_segment = ChatStateContainer()
        first_segment.add_tool_call(
            {
                "tool_call_id": "tc-1",
                "tool_name": "nmap",
                "tool_arguments": {"target": "10.10.10.0/24"},
                "tool_result": "tool-one",
                "turn_index": 0,
            }
        )
        first_segment.start_observation(sub_turn_index=0)
        first_segment.append_observation("obs-one")
        first_segment.end_observation(sub_turn_index=0)

        second_segment = ChatStateContainer()
        second_segment.add_tool_call(
            {
                "tool_call_id": "tc-2",
                "tool_name": "nmap",
                "tool_arguments": {"target": "10.10.10.3", "ports": "5432"},
                "tool_result": "tool-two",
                "turn_index": 1,
            }
        )
        second_segment.start_observation(sub_turn_index=1)
        second_segment.append_observation("obs-two")
        second_segment.end_observation(sub_turn_index=1)

        with patch(
            "backend.services.langgraph_chat.execution.completion_callback.SessionLocal",
            side_effect=session_factory,
        ):
            persist_chat_message_from_container(
                task_id=task_id,
                turn_id="task-1-turn-1",
                reserved_message_id=message_id,
                state_container=first_segment,
                final_message="partial",
                error="interrupted",
                reason="hitl_interrupt",
                conversation_id="conv-1",
                turn_number=1,
            )
            persist_chat_message_from_container(
                task_id=task_id,
                turn_id="task-1-turn-1",
                reserved_message_id=message_id,
                state_container=second_segment,
                final_message="done",
                error=None,
                reason="resume_normal",
                conversation_id="conv-1",
                turn_number=1,
            )

        verify_db = session_factory()
        try:
            rows = verify_db.execute(
                select(ChatTurnEvent)
                .where(ChatTurnEvent.chat_message_id == message_id)
                .order_by(ChatTurnEvent.phase_sequence.asc())
            ).scalars().all()

            assert [row.kind for row in rows] == ["tool", "observation", "tool", "observation"]
            assert [row.content for row in rows] == ["tool-one", "obs-one", "tool-two", "obs-two"]
            assert [row.phase_sequence for row in rows] == [0, 1, 2, 3]
        finally:
            verify_db.close()
            engine.dispose()

    def test_persist_replaces_canonical_events_on_successful_retry_completion(self):
        """Regression: a successful retry must drop stale failed-attempt detail rows.

        Without this, the COMPLETED projection still surfaces the prior
        attempt's tool/observation rows ("old failed-attempt details
        render as active transcript after retry resync"). Retry callers pass
        an explicit replace flag so canonical detail rows for the message are
        replaced wholesale rather than merged.
        """
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        seed_db = session_factory()
        try:
            user = User(username="completion-retry-user", password="secret")
            seed_db.add(user)
            seed_db.flush()

            task = Task(user_id=user.id, name="completion-retry-task")
            seed_db.add(task)
            seed_db.flush()

            message = ChatMessage(
                task_id=task.id,
                conversation_id="conv-retry",
                parent_message_id=None,
                latest_child_message_id=None,
                message_type="assistant",
                message="",
                token_count=0,
                turn_number=1,
            )
            seed_db.add(message)
            seed_db.flush()
            message_id = int(message.id)
            task_id = int(task.id)

            # Pre-existing canonical rows from the FAILED attempt.
            seed_db.add(
                ChatTurnEvent(
                    task_id=task_id,
                    conversation_id="conv-retry",
                    chat_message_id=message_id,
                    turn_number=1,
                    phase_sequence=0,
                    kind="tool",
                    content="STALE-FAILED-ATTEMPT-tool-output",
                    sub_turn_index=0,
                    tool_call_id="tc-stale",
                    event_metadata={"tool_name": "shell"},
                )
            )
            seed_db.add(
                ChatTurnEvent(
                    task_id=task_id,
                    conversation_id="conv-retry",
                    chat_message_id=message_id,
                    turn_number=1,
                    phase_sequence=1,
                    kind="observation",
                    content="STALE-FAILED-ATTEMPT-observation",
                    sub_turn_index=0,
                    event_metadata={},
                )
            )
            # The retry just succeeded — workflow row reflects retry_attempt_count >= 1.
            workflow = TurnWorkflow(
                task_id=task_id,
                conversation_id="conv-retry",
                turn_id=f"task-{task_id}-turn-1",
                turn_sequence=1,
                state="COMPLETED",
                graph_name="simple_tool",
                reserved_message_id=message_id,
                checkpoint_id="ckpt-retry-success",
                workflow_metadata={
                    "retryable": False,
                    "retry_mode": "checkpoint",
                    "retry_attempt_count": 1,
                    "retry_max_attempts": 2,
                },
            )
            seed_db.add(workflow)
            seed_db.commit()
        finally:
            seed_db.close()

        # The successful retry attempt's container — only the new attempt's events.
        retry_segment = ChatStateContainer()
        retry_segment.add_tool_call(
            {
                "tool_call_id": "tc-success",
                "tool_name": "shell",
                "tool_arguments": {"target": "10.10.10.0/24"},
                "tool_result": "FRESH-RETRY-SUCCESS-tool-output",
                "turn_index": 0,
            }
        )
        retry_segment.start_observation(sub_turn_index=0)
        retry_segment.append_observation("FRESH-RETRY-SUCCESS-observation")
        retry_segment.end_observation(sub_turn_index=0)

        with patch(
            "backend.services.langgraph_chat.execution.completion_callback.SessionLocal",
            side_effect=session_factory,
        ):
            persist_chat_message_from_container(
                task_id=task_id,
                turn_id=f"task-{task_id}-turn-1",
                reserved_message_id=message_id,
                state_container=retry_segment,
                final_message="retry completed",
                error=None,
                reason="retry_complete",
                conversation_id="conv-retry",
                turn_number=1,
                replace_turn_events=True,
            )

        verify_db = session_factory()
        try:
            rows = verify_db.execute(
                select(ChatTurnEvent)
                .where(ChatTurnEvent.chat_message_id == message_id)
                .order_by(ChatTurnEvent.phase_sequence.asc())
            ).scalars().all()

            # The persisted events must be ONLY the successful attempt's rows.
            contents = [row.content for row in rows]
            kinds = [row.kind for row in rows]
            for content in contents:
                assert "STALE-FAILED-ATTEMPT" not in (content or ""), (
                    "stale failed-attempt detail rows must be dropped after a "
                    f"successful retry; got persisted contents={contents!r}"
                )
            # The successful attempt's rows must be present.
            assert "tool" in kinds and "observation" in kinds
            assert any("FRESH-RETRY-SUCCESS-tool-output" in (c or "") for c in contents)
            assert any("FRESH-RETRY-SUCCESS-observation" in (c or "") for c in contents)
        finally:
            verify_db.close()
            engine.dispose()

    def test_persist_uses_merge_when_not_a_retry_attempt(self):
        """Non-retry turns keep the merge semantics so resume-segments still chain."""
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

        seed_db = session_factory()
        try:
            user = User(username="completion-non-retry-user", password="secret")
            seed_db.add(user)
            seed_db.flush()

            task = Task(user_id=user.id, name="completion-non-retry-task")
            seed_db.add(task)
            seed_db.flush()

            message = ChatMessage(
                task_id=task.id,
                conversation_id="conv-non-retry",
                parent_message_id=None,
                latest_child_message_id=None,
                message_type="assistant",
                message="",
                token_count=0,
                turn_number=1,
            )
            seed_db.add(message)
            seed_db.flush()
            message_id = int(message.id)
            task_id = int(task.id)

            # Pre-existing canonical row from a previous resume segment.
            seed_db.add(
                ChatTurnEvent(
                    task_id=task_id,
                    conversation_id="conv-non-retry",
                    chat_message_id=message_id,
                    turn_number=1,
                    phase_sequence=0,
                    kind="tool",
                    content="prior-segment-tool",
                    sub_turn_index=0,
                    tool_call_id="tc-prior",
                    event_metadata={"tool_name": "shell"},
                )
            )
            # Workflow has no retry attempt yet (count == 0).
            workflow = TurnWorkflow(
                task_id=task_id,
                conversation_id="conv-non-retry",
                turn_id=f"task-{task_id}-turn-1",
                turn_sequence=1,
                state="COMPLETED",
                graph_name="simple_tool",
                reserved_message_id=message_id,
                workflow_metadata={
                    "retry_mode": "checkpoint",
                    "retry_attempt_count": 0,
                    "retry_max_attempts": 2,
                },
            )
            seed_db.add(workflow)
            seed_db.commit()
        finally:
            seed_db.close()

        new_segment = ChatStateContainer()
        new_segment.add_tool_call(
            {
                "tool_call_id": "tc-new",
                "tool_name": "shell",
                "tool_arguments": {"target": "10.10.10.0/24"},
                "tool_result": "new-segment-tool",
                "turn_index": 1,
            }
        )

        with patch(
            "backend.services.langgraph_chat.execution.completion_callback.SessionLocal",
            side_effect=session_factory,
        ):
            persist_chat_message_from_container(
                task_id=task_id,
                turn_id=f"task-{task_id}-turn-1",
                reserved_message_id=message_id,
                state_container=new_segment,
                final_message="done",
                error=None,
                reason="resume_normal",
                conversation_id="conv-non-retry",
                turn_number=1,
            )

        verify_db = session_factory()
        try:
            rows = verify_db.execute(
                select(ChatTurnEvent)
                .where(ChatTurnEvent.chat_message_id == message_id)
                .order_by(ChatTurnEvent.phase_sequence.asc())
            ).scalars().all()

            contents = [row.content for row in rows]
            # Both the prior segment and the new segment must be present —
            # this is the existing resume-merge contract; the retry-detection
            # logic must not break it for non-retry runs.
            assert "prior-segment-tool" in contents
            assert "new-segment-tool" in contents
        finally:
            verify_db.close()
            engine.dispose()


class TestStreamEmitter:
    """Tests for StreamEmitter helper class."""

    @pytest.mark.asyncio
    async def test_stream_emitter_adds_to_queue(self):
        """StreamEmitter should add events to queue."""
        queue = asyncio.Queue()
        emitter = StreamEmitter(queue=queue)

        event = {"type": "test", "content": "test"}
        await emitter.emit(event)

        assert queue.qsize() == 1
        queued_event = await queue.get()
        assert queued_event["type"] == "test"

    @pytest.mark.asyncio
    async def test_stream_emitter_on_emit_callback(self):
        """StreamEmitter should invoke on_emit callback."""
        queue = asyncio.Queue()
        seen = []

        def on_emit(event):
            seen.append(event["type"])

        emitter = StreamEmitter(queue=queue, on_emit=on_emit)
        await emitter.emit({"type": "event1", "content": "A"})
        await emitter.emit({"type": "event2", "content": "B"})

        assert seen == ["event1", "event2"]
