"""Tests for LangGraphExecutor."""

import os

# Set mock DATABASE_URL before any imports
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.services.langgraph_chat.execution.graph_executor import LangGraphExecutor
from backend.services.langgraph_chat.checkpoint.thread_identity import format_graph_thread_id
from backend.services.langgraph_chat.compression.window_models import (
    ContextWindowDecision,
    ContextWindowSnapshot,
)
from backend.database import Base
from backend.models import Task, Tenant, User
from backend.models.hitl import InterruptTicket, InterruptTicketState


def _seed_task(session, *, task_id: int) -> Task:
    tenant = Tenant(slug=f"graph-executor-{task_id}", name=f"Graph Executor {task_id}")
    user = User(
        username=f"graph-executor-{task_id}",
        password="x",
        email=f"graph-executor-{task_id}@example.test",
    )
    session.add_all([tenant, user])
    session.commit()
    task = Task(id=task_id, tenant_id=tenant.id, user_id=user.id, name=f"task-{task_id}")
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


class TestGraphExecutorStreaming:
    """Test streaming execution."""
    
    @pytest.mark.asyncio
    async def test_graph_executor_streaming_success(self):
        """Test successful streaming execution."""
        executor = LangGraphExecutor()
        
        # Mock compiled graph
        mock_graph = AsyncMock()
        
        # Mock astream to yield events
        async def mock_astream(input_state, config, stream_mode):
            # Yield custom event
            yield ("custom", {"type": "message_delta", "content": "test"})
            # Yield final state
            yield ("values", {"facts": {}, "trace": {}})
        
        mock_graph.astream = mock_astream
        
        result = await executor.stream_graph(
            compiled_graph=mock_graph,
            graph_input={"facts": {}, "trace": {}},
            config={"configurable": {"thread_id": "test"}},
            task_id=1,
        )
        
        # Should return final state
        assert result is not None
        assert isinstance(result.final_state, dict)

    @pytest.mark.asyncio
    async def test_graph_executor_honors_cancel_checker(self):
        """Cancellation checker aborts streaming before processing the next graph chunk."""
        executor = LangGraphExecutor()
        mock_graph = AsyncMock()

        async def mock_astream(input_state, config, stream_mode):
            yield ("values", {"facts": {}, "trace": {}})

        mock_graph.astream = mock_astream

        with pytest.raises(RuntimeError, match="run_cancelled"):
            await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={"configurable": {"thread_id": "test"}},
                task_id=1,
                should_cancel=lambda: True,
            )
    
    @pytest.mark.asyncio
    async def test_graph_executor_streaming_captures_final_state(self):
        """Test that streaming captures final state from values mode."""
        executor = LangGraphExecutor()
        
        mock_graph = AsyncMock()
        
        final_state_dict = {"facts": {"message": "test"}, "trace": {"final_text": "result"}}
        
        async def mock_astream(input_state, config, stream_mode):
            # Yield values mode with final state
            yield ("values", final_state_dict)
        
        mock_graph.astream = mock_astream
        
        result = await executor.stream_graph(
            compiled_graph=mock_graph,
            graph_input={},
            config={"configurable": {"thread_id": "test"}},
            task_id=1,
        )
        
        # Should return the final state from values mode
        assert result.final_state == final_state_dict
    
    @pytest.mark.asyncio
    async def test_graph_executor_streaming_recovers_snapshot_when_values_missing(self):
        """Recover final state from graph snapshot when values mode emits no state."""
        executor = LangGraphExecutor()
        
        mock_graph = AsyncMock()
        
        # Mock astream to emit only custom events
        async def mock_astream(input_state, config, stream_mode):
            yield ("custom", {"type": "message_delta", "content": "test"})
        
        mock_graph.astream = mock_astream
        mock_graph.aget_state = AsyncMock(
            return_value=type("Snapshot", (), {"values": {"facts": {}, "trace": {"final_text": "done"}}})()
        )
        
        result = await executor.stream_graph(
            compiled_graph=mock_graph,
            graph_input={},
            config={"configurable": {"thread_id": "test"}},
            task_id=1,
        )
        
        assert result.final_state == {"facts": {}, "trace": {"final_text": "done"}}

    @pytest.mark.asyncio
    async def test_graph_executor_snapshot_recovery_prefers_latest_thread_over_checkpoint_anchor(self):
        """Recovery must query latest thread state first, not pinned checkpoint anchor."""
        executor = LangGraphExecutor()
        mock_graph = AsyncMock()

        async def mock_astream(input_state, config, stream_mode):
            yield ("custom", {"type": "message_delta", "content": "resume-done"})

        seen_configs = []

        async def mock_aget_state(cfg):
            seen_configs.append(cfg)
            configurable = (cfg or {}).get("configurable") or {}
            if "checkpoint_id" in configurable:
                return None
            return type(
                "Snapshot",
                (),
                {"values": {"facts": {"message": "ok"}, "trace": {"final_text": "resumed"}}},
            )()

        mock_graph.astream = mock_astream
        mock_graph.aget_state = AsyncMock(side_effect=mock_aget_state)

        result = await executor.stream_graph(
            compiled_graph=mock_graph,
            graph_input={},
            config={
                "configurable": {
                    "thread_id": "task-9",
                    "checkpoint_id": "cp-123",
                    "runtime_services": object(),
                }
            },
            task_id=9,
        )

        assert result.final_state == {"facts": {"message": "ok"}, "trace": {"final_text": "resumed"}}
        assert len(seen_configs) == 1
        seen_configurable = seen_configs[0].get("configurable") or {}
        assert "checkpoint_id" not in seen_configurable
        assert "runtime_services" not in seen_configurable

    @pytest.mark.asyncio
    async def test_graph_executor_streaming_returns_none_without_values_or_snapshot(self):
        """Return None when neither values mode nor snapshot recovery captures state."""
        executor = LangGraphExecutor()

        mock_graph = AsyncMock()

        async def mock_astream(input_state, config, stream_mode):
            yield ("custom", {"type": "message_delta", "content": "test"})

        mock_graph.astream = mock_astream
        mock_graph.aget_state = AsyncMock(return_value=None)

        result = await executor.stream_graph(
            compiled_graph=mock_graph,
            graph_input={},
            config={"configurable": {"thread_id": "test"}},
            task_id=1,
        )

        assert result.final_state is None
    
    @pytest.mark.asyncio
    async def test_graph_executor_forwards_events_to_hub(self):
        """Test that executor forwards events to stream hub."""
        # Mock streaming adapter
        mock_adapter = MagicMock()
        mock_adapter.process_streaming_event.return_value = {"type": "processed", "content": "test"}
        
        executor = LangGraphExecutor(streaming_adapter=mock_adapter)
        
        mock_graph = AsyncMock()
        
        async def mock_astream(input_state, config, stream_mode):
            yield ("custom", {"type": "message_delta", "content": "test"})
            yield ("values", {})
        
        mock_graph.astream = mock_astream
        
        # Mock stream hub
        with patch.object(executor, '_stream_hub') as mock_hub:
            mock_hub.publish = AsyncMock()
            
            result = await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={"configurable": {"thread_id": "test"}},
                task_id=1,
            )
            
            # Verify event was forwarded to hub
            mock_hub.publish.assert_called_once()
            call_args = mock_hub.publish.call_args
            assert call_args[1]["task_id"] == 1
            assert call_args[1]["event"]["type"] == "processed"

    @pytest.mark.asyncio
    async def test_graph_executor_emits_interrupt_event(self):
        """Test that executor emits interrupt events to stream hub."""
        executor = LangGraphExecutor()

        mock_graph = AsyncMock()
        interrupt_payload = {"type": "tool_approval", "tool_id": "nmap"}

        class DummyInterrupt:
            def __init__(self, value):
                self.value = value

        async def mock_astream(input_state, config, stream_mode):
            yield ("values", {"__interrupt__": [DummyInterrupt(interrupt_payload)]})

        mock_graph.astream = mock_astream

        with patch.object(executor, "_stream_hub") as mock_hub:
            mock_hub.publish = AsyncMock()

            result = await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={"configurable": {"thread_id": "test-thread", "graph_name": "simple_tool"}},
                task_id=42,
            )

            assert result is not None
            assert result.interrupted
            mock_hub.publish.assert_called_once()
            event = mock_hub.publish.call_args.kwargs["event"]
            assert event["type"] == "graph_interrupt"
            assert event["payload"]["tool_id"] == "nmap"
            assert isinstance(event.get("interrupt_id"), str)
            assert event["interrupt_id"] == event["payload"]["interrupt_id"]
            assert event.get("checkpoint_id") is None

    @pytest.mark.asyncio
    async def test_graph_executor_schedules_runtime_warmup_on_interrupt_emission(self):
        """Interrupt emission should trigger best-effort warmup during HITL wait."""
        executor = LangGraphExecutor()

        mock_graph = AsyncMock()
        interrupt_payload = {"type": "tool_approval", "tool_id": "nmap"}

        class DummyInterrupt:
            def __init__(self, value):
                self.value = value

        async def mock_astream(input_state, config, stream_mode):
            yield ("values", {"__interrupt__": [DummyInterrupt(interrupt_payload)]})

        mock_graph.astream = mock_astream

        with (
            patch.object(executor, "_stream_hub") as mock_hub,
            patch.object(executor, "_schedule_runtime_warmup") as mock_schedule_warmup,
        ):
            mock_hub.publish = AsyncMock()

            await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={
                    "configurable": {
                        "thread_id": "test-thread",
                        "graph_name": "simple_tool",
                        "graph_runtime_context": {"workspace_path": "/tmp/task-42"},
                    }
                },
                task_id=42,
            )

            mock_schedule_warmup.assert_called_once_with(
                task_id=42,
                graph_name="simple_tool",
                workspace_path="/tmp/task-42",
            )

    @pytest.mark.asyncio
    async def test_graph_executor_creates_interrupt_ticket_on_interrupt_emission(self):
        """Interrupt emission upserts pending ticket for resume claim path."""
        executor = LangGraphExecutor()

        mock_graph = AsyncMock()
        interrupt_payload = {
            "type": "tool_approval",
            "interrupt_id": "simple_tool:checkpoint:cp-123",
            "turn_id": "turn-7",
            "turn_sequence": 7,
            "tool_call_id": "tool-call-1",
            "tool_id": "nmap",
            "checkpoint_id": "cp-123",
        }

        class DummyInterrupt:
            def __init__(self, value):
                self.value = value

        async def mock_astream(input_state, config, stream_mode):
            yield ("values", {"__interrupt__": [DummyInterrupt(interrupt_payload)]})

        mock_graph.astream = mock_astream
        mock_db = MagicMock()

        with (
            patch.object(executor, "_stream_hub") as mock_hub,
            patch("backend.services.langgraph_chat.execution.graph_executor.SessionLocal", return_value=mock_db),
            patch(
                "backend.services.langgraph_chat.checkpoint.interrupt_ticket_service.InterruptTicketService"
            ) as mock_ticket_service_cls,
        ):
            mock_hub.publish = AsyncMock()
            mock_ticket_service = mock_ticket_service_cls.return_value

            await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={
                    "configurable": {
                        "thread_id": "task-42",
                        "graph_name": "simple_tool",
                        "checkpoint_id": "cp-123",
                    }
                },
                task_id=42,
            )

            mock_ticket_service.create_or_update_pending.assert_called_once_with(
                interrupt_id="simple_tool:checkpoint:cp-123",
                task_id=42,
                graph_name="simple_tool",
                interrupt_type="tool_approval",
                checkpoint_id="cp-123",
                thread_id="task-42",
                turn_id="turn-7",
                turn_sequence=7,
                tool_call_id="tool-call-1",
                payload_snapshot={
                    "type": "tool_approval",
                    "interrupt_id": "simple_tool:checkpoint:cp-123",
                    "turn_id": "turn-7",
                    "turn_sequence": 7,
                    "tool_call_id": "tool-call-1",
                    "tool_id": "nmap",
                    "checkpoint_id": "cp-123",
                },
            )
            mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_graph_executor_interrupt_ticket_ignores_resume_anchor_checkpoint_in_config(self):
        """Resume anchor checkpoint_id in config must not be persisted as new interrupt checkpoint."""
        executor = LangGraphExecutor()

        mock_graph = AsyncMock()
        interrupt_payload = {
            "type": "tool_approval",
            "interrupt_id": "intr-reinterrupt-1",
            "turn_id": "turn-9",
            "turn_sequence": 9,
            "tool_call_id": "tool-call-9",
            "tool_id": "shell.exec",
        }

        class DummyInterrupt:
            def __init__(self, value):
                self.value = value

        async def mock_astream(input_state, config, stream_mode):
            yield ("values", {"__interrupt__": [DummyInterrupt(interrupt_payload)]})

        mock_graph.astream = mock_astream
        mock_db = MagicMock()

        with (
            patch.object(executor, "_stream_hub") as mock_hub,
            patch("backend.services.langgraph_chat.execution.graph_executor.SessionLocal", return_value=mock_db),
            patch(
                "backend.services.langgraph_chat.checkpoint.interrupt_ticket_service.InterruptTicketService"
            ) as mock_ticket_service_cls,
        ):
            mock_hub.publish = AsyncMock()
            mock_ticket_service = mock_ticket_service_cls.return_value

            await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={
                    "configurable": {
                        "thread_id": "task-99",
                        "graph_name": "simple_tool",
                        # Old resume anchor: must not be treated as new interrupt checkpoint
                        "checkpoint_id": "cp-old-anchor",
                    }
                },
                task_id=99,
            )

            kwargs = mock_ticket_service.create_or_update_pending.call_args.kwargs
            assert kwargs["interrupt_id"] == "intr-reinterrupt-1"
            assert kwargs["checkpoint_id"] is None
            assert "checkpoint_id" not in kwargs["payload_snapshot"]

    @pytest.mark.asyncio
    async def test_graph_executor_persists_interrupt_ticket_claimable(self):
        """Wired interrupt emission persists a pending ticket claimable by interrupt_id."""
        executor = LangGraphExecutor()
        engine = create_engine("sqlite:///:memory:")
        Session = sessionmaker(bind=engine)
        Base.metadata.create_all(bind=engine)
        seed_db = Session()
        try:
            task = _seed_task(seed_db, task_id=100)
            thread_id = format_graph_thread_id(task.graph_thread_id, task_id=100)
        finally:
            seed_db.close()

        mock_graph = AsyncMock()
        interrupt_payload = {
            "type": "tool_approval",
            "interrupt_id": "simple_tool:checkpoint:cp-claim",
            "turn_id": "turn-claim",
            "turn_sequence": 3,
        }

        class DummyInterrupt:
            def __init__(self, value):
                self.value = value

        async def mock_astream(input_state, config, stream_mode):
            yield ("values", {"__interrupt__": [DummyInterrupt(interrupt_payload)]})

        mock_graph.astream = mock_astream

        with (
            patch.object(executor, "_stream_hub") as mock_hub,
            patch("backend.services.langgraph_chat.execution.graph_executor.SessionLocal", side_effect=lambda: Session()),
        ):
            mock_hub.publish = AsyncMock()
            await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "graph_name": "simple_tool",
                        "checkpoint_id": "cp-claim",
                    }
                },
                task_id=100,
            )

        verify_db = Session()
        try:
            ticket = (
                verify_db.query(InterruptTicket)
                .filter(InterruptTicket.interrupt_id == "simple_tool:checkpoint:cp-claim")
                .one()
            )
            assert ticket.task_id == 100
            assert ticket.graph_name == "simple_tool"
            assert ticket.state == InterruptTicketState.PENDING

            from backend.services.langgraph_chat.checkpoint.interrupt_ticket_service import InterruptTicketService

            claimed = InterruptTicketService(verify_db).claim_for_resume(
                interrupt_id="simple_tool:checkpoint:cp-claim",
                task_id=100,
            )
            assert claimed.state == InterruptTicketState.RESUMING
        finally:
            verify_db.close()
            engine.dispose()

    @pytest.mark.asyncio
    async def test_graph_executor_reobserved_interrupt_does_not_repend_resumed_ticket(self):
        """REGRESSION: Re-observing same interrupt_id must not downgrade RESUMED to PENDING.

        Original bug: graph executor _register_observed_interrupt_ticket could re-pend
        an already-resumed interrupt when stream re-emitted __interrupt__ for same id.
        CI must fail if this path returns; stale card replay depends on this guard.
        """
        executor = LangGraphExecutor()
        engine = create_engine("sqlite:///:memory:")
        Session = sessionmaker(bind=engine)
        Base.metadata.create_all(bind=engine)

        seed_db = Session()
        try:
            task = _seed_task(seed_db, task_id=101)
            thread_id = format_graph_thread_id(task.graph_thread_id, task_id=101)
            seed_db.add(
                InterruptTicket(
                    interrupt_id="simple_tool:checkpoint:cp-stale-1",
                    task_id=101,
                    tenant_id=task.tenant_id,
                    graph_name="simple_tool",
                    interrupt_type="tool_approval",
                    checkpoint_id="cp-stale-1",
                    thread_id=thread_id,
                    state=InterruptTicketState.RESUMED,
                    payload_snapshot={"type": "tool_approval", "tool_id": "old-tool"},
                )
            )
            seed_db.commit()
        finally:
            seed_db.close()

        mock_graph = AsyncMock()
        interrupt_payload = {
            "type": "tool_approval",
            "interrupt_id": "simple_tool:checkpoint:cp-stale-1",
            "tool_id": "new-tool",
            "checkpoint_id": "cp-stale-1",
        }

        class DummyInterrupt:
            def __init__(self, value):
                self.value = value

        async def mock_astream(input_state, config, stream_mode):
            yield ("values", {"__interrupt__": [DummyInterrupt(interrupt_payload)]})

        mock_graph.astream = mock_astream

        with (
            patch.object(executor, "_stream_hub") as mock_hub,
            patch("backend.services.langgraph_chat.execution.graph_executor.SessionLocal", side_effect=lambda: Session()),
        ):
            mock_hub.publish = AsyncMock()
            await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "graph_name": "simple_tool",
                    }
                },
                task_id=101,
            )

        verify_db = Session()
        try:
            ticket = (
                verify_db.query(InterruptTicket)
                .filter(InterruptTicket.interrupt_id == "simple_tool:checkpoint:cp-stale-1")
                .one()
            )
            assert ticket.state == InterruptTicketState.RESUMED
            assert ticket.payload_snapshot["tool_id"] == "old-tool"
        finally:
            verify_db.close()
            engine.dispose()
    
    @pytest.mark.asyncio
    async def test_graph_executor_metrics_emitted(self):
        """Test that execution metrics are emitted."""
        executor = LangGraphExecutor()
        
        mock_graph = AsyncMock()
        
        async def mock_astream(input_state, config, stream_mode):
            yield ("values", {})
        
        mock_graph.astream = mock_astream
        
        with patch('backend.services.langgraph_chat.execution.graph_executor.safe_inc') as mock_inc:
            result = await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={"configurable": {"thread_id": "test"}},
                task_id=1,
            )
            
            # Verify metrics were incremented
            metric_names = [call[0][0] for call in mock_inc.call_args_list]
            assert "langgraph_streaming_sessions_started" in metric_names
            assert "langgraph_streaming_sessions_completed" in metric_names

    @pytest.mark.asyncio
    async def test_graph_executor_streaming_error_raises(self):
        """Test that streaming errors are raised."""
        executor = LangGraphExecutor()
        
        mock_graph = AsyncMock()
        
        async def mock_astream(input_state, config, stream_mode):
            raise RuntimeError("Streaming error")
            yield  # pragma: no cover
        
        mock_graph.astream = mock_astream
        
        with pytest.raises(RuntimeError, match="Streaming error"):
            await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={"configurable": {"thread_id": "test"}},
                task_id=1,
            )

    @pytest.mark.asyncio
    async def test_graph_executor_simple_tool_event_sequence_contract(self):
        """Simple-tool custom events are forwarded in exact emission order."""
        mock_adapter = MagicMock()
        mock_adapter.process_streaming_event.side_effect = lambda e, state_container=None: e
        executor = LangGraphExecutor(streaming_adapter=mock_adapter)

        expected_types = [
            "tool_start",
            "tool_end",
            "message_start",
            "message_delta",
            "section_end",
        ]

        async def mock_astream(input_state, config, stream_mode):
            for event_type in expected_types:
                yield (
                    "custom",
                    {
                        "type": event_type,
                        "content": event_type,
                        "conversation_id": "conv-1",
                        "turn_id": "turn-1",
                    },
                )
            yield ("values", {"facts": {}, "trace": {"final_text": "done"}})

        mock_graph = AsyncMock()
        mock_graph.astream = mock_astream

        with patch.object(executor, "_stream_hub") as mock_hub:
            mock_hub.publish = AsyncMock()
            await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={"configurable": {"thread_id": "test-thread", "graph_name": "simple_tool"}},
                task_id=1,
            )

        forwarded_types = [call.kwargs["event"]["type"] for call in mock_hub.publish.call_args_list]
        assert forwarded_types == expected_types

    @pytest.mark.asyncio
    async def test_graph_executor_deep_reasoning_event_sequence_contract(self):
        """Deep-reasoning event families are forwarded in stable order."""
        mock_adapter = MagicMock()
        mock_adapter.process_streaming_event.side_effect = lambda e, state_container=None: e
        executor = LangGraphExecutor(streaming_adapter=mock_adapter)

        expected_types = [
            "reasoning_start",
            "reasoning_delta",
            "reasoning_section_end",
            "tool_start",
            "tool_end",
            "observation_start",
            "observation_delta",
            "observation_section_end",
            "message_start",
            "message_delta",
            "section_end",
        ]

        async def mock_astream(input_state, config, stream_mode):
            for event_type in expected_types:
                yield (
                    "custom",
                    {
                        "type": event_type,
                        "content": event_type,
                        "conversation_id": "conv-dr",
                        "turn_id": "turn-dr-1",
                    },
                )
            yield ("values", {"facts": {}, "trace": {"final_text": "done"}})

        mock_graph = AsyncMock()
        mock_graph.astream = mock_astream

        with patch.object(executor, "_stream_hub") as mock_hub:
            mock_hub.publish = AsyncMock()
            await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={"configurable": {"thread_id": "test-thread", "graph_name": "deep_reasoning"}},
                task_id=7,
            )

        forwarded_types = [call.kwargs["event"]["type"] for call in mock_hub.publish.call_args_list]
        assert forwarded_types == expected_types

    @pytest.mark.asyncio
    async def test_graph_executor_interrupt_then_resume_sequence_contract(self):
        """Interrupt marker is emitted in-order and resume continues deterministically."""
        mock_adapter = MagicMock()
        mock_adapter.process_streaming_event.side_effect = lambda e, state_container=None: e
        executor = LangGraphExecutor(streaming_adapter=mock_adapter)

        class DummyInterrupt:
            def __init__(self, value):
                self.value = value

        pre_interrupt_types = ["reasoning_start", "reasoning_delta"]
        resume_types = ["tool_start", "tool_end", "message_start", "message_delta", "section_end"]

        async def first_astream(input_state, config, stream_mode):
            for event_type in pre_interrupt_types:
                yield (
                    "custom",
                    {
                        "type": event_type,
                        "content": event_type,
                        "conversation_id": "conv-hitl",
                        "turn_id": "turn-hitl-1",
                    },
                )
            yield (
                "values",
                {
                    "__interrupt__": [DummyInterrupt({"type": "tool_approval", "tool_id": "shell.exec"})],
                    "facts": {},
                    "trace": {},
                },
            )

        async def second_astream(input_state, config, stream_mode):
            for event_type in resume_types:
                yield (
                    "custom",
                    {
                        "type": event_type,
                        "content": event_type,
                        "conversation_id": "conv-hitl",
                        "turn_id": "turn-hitl-1",
                    },
                )
            yield ("values", {"facts": {}, "trace": {"final_text": "resumed"}})

        first_graph = AsyncMock()
        first_graph.astream = first_astream
        second_graph = AsyncMock()
        second_graph.astream = second_astream

        with patch.object(executor, "_stream_hub") as mock_hub:
            mock_hub.publish = AsyncMock()

            first_result = await executor.stream_graph(
                compiled_graph=first_graph,
                graph_input={},
                config={"configurable": {"thread_id": "task-1", "graph_name": "deep_reasoning"}},
                task_id=1,
            )
            assert first_result.interrupted

            second_result = await executor.stream_graph(
                compiled_graph=second_graph,
                graph_input={},
                config={"configurable": {"thread_id": "task-1", "graph_name": "deep_reasoning"}},
                task_id=1,
            )
            assert not second_result.interrupted

        forwarded_types = [call.kwargs["event"]["type"] for call in mock_hub.publish.call_args_list]
        expected = [*pre_interrupt_types, "graph_interrupt", *resume_types]
        assert forwarded_types == expected

    @pytest.mark.asyncio
    async def test_graph_executor_checkpoint_observer_emits_context_window_status_when_ceiling_reached(self):
        """Values checkpoint emits non-blocking context-window status when over ceiling."""
        executor = LangGraphExecutor()
        mock_graph = AsyncMock()
        values_chunk = {
            "facts": {
                "conversation_id": "conv-ctx-1",
                "message": "projected user prompt",
                "metadata": {
                    "model": "gpt-4o-mini",
                    "conversation_history": [{"role": "user", "content": "hello"}],
                },
            },
            "trace": {},
        }

        async def mock_astream(input_state, config, stream_mode):
            yield ("values", values_chunk)

        mock_graph.astream = mock_astream
        decision = ContextWindowDecision(
            snapshot=ContextWindowSnapshot(
                task_id=44,
                conversation_id="conv-ctx-1",
                max_tokens=128_000,
                used_tokens=128_500,
                remaining_tokens=0,
                ratio=1.0,
                ceiling_reached=True,
            ),
            ceiling_reached=True,
            recommended_next_action="compress",
            compression_candidate=True,
        )

        with (
            patch(
                "backend.services.langgraph_chat.execution.graph_executor.ContextWindowManager.evaluate_history",
                return_value=decision,
            ) as mock_evaluate,
            patch(
                "backend.services.langgraph_chat.execution.graph_executor.emit_context_window_event"
            ) as mock_emit,
        ):
            result = await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={"configurable": {"thread_id": "task-44"}},
                task_id=44,
            )

        assert result.final_state == values_chunk
        assert result.interrupted is False
        assert isinstance(result.metadata, dict)
        assert "context_window" in result.metadata
        assert result.metadata["context_window"]["ceiling_reached"] is True
        assert result.metadata["context_window"]["recommended_next_action"] == "compress"
        mock_evaluate.assert_called_once()
        assert mock_evaluate.call_args.kwargs["projected_user_message"] == "projected user prompt"
        mock_emit.assert_called_once()
        assert mock_emit.call_args.kwargs["task_id"] == 44
        assert mock_emit.call_args.kwargs["conversation_id"] == "conv-ctx-1"
        assert mock_emit.call_args.kwargs["ceiling_reached"] is True
        assert mock_emit.call_args.kwargs["recommended_next_action"] == "compress"
        assert mock_emit.call_args.kwargs["compression_candidate"] is True

    @pytest.mark.asyncio
    async def test_graph_executor_checkpoint_observer_skips_context_event_when_not_reached(self):
        """Values checkpoint continues normally without context status below ceiling."""
        executor = LangGraphExecutor()
        mock_graph = AsyncMock()
        values_chunk = {
            "facts": {
                "conversation_id": "conv-ctx-2",
                "message": "still below ceiling",
                "metadata": {
                    "model": "gpt-4o-mini",
                    "conversation_history": [{"role": "assistant", "content": "ok"}],
                },
            },
            "trace": {"final_text": "done"},
        }

        async def mock_astream(input_state, config, stream_mode):
            yield ("values", values_chunk)

        mock_graph.astream = mock_astream
        decision = ContextWindowDecision(
            snapshot=ContextWindowSnapshot(
                task_id=45,
                conversation_id="conv-ctx-2",
                max_tokens=128_000,
                used_tokens=10,
                remaining_tokens=127_990,
                ratio=10 / 128_000,
                ceiling_reached=False,
            ),
            ceiling_reached=False,
            recommended_next_action="none",
            compression_candidate=False,
        )

        with (
            patch(
                "backend.services.langgraph_chat.execution.graph_executor.ContextWindowManager.evaluate_history",
                return_value=decision,
            ) as mock_evaluate,
            patch(
                "backend.services.langgraph_chat.execution.graph_executor.emit_context_window_event"
            ) as mock_emit,
        ):
            result = await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={"configurable": {"thread_id": "task-45"}},
                task_id=45,
            )

        assert result.final_state == values_chunk
        assert result.interrupted is False
        mock_evaluate.assert_called_once()
        assert mock_evaluate.call_args.kwargs["projected_user_message"] == "still below ceiling"
        mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_graph_executor_checkpoint_over_ceiling_observes_without_rewriting_state(self):
        """Over-ceiling observation must not compress or rewrite checkpoint state."""
        mock_adapter = MagicMock()
        mock_adapter.process_streaming_event.side_effect = lambda e, state_container=None: e
        executor = LangGraphExecutor(streaming_adapter=mock_adapter)
        mock_graph = AsyncMock()
        context_events = []

        first_values_chunk = {
            "facts": {
                "conversation_id": "conv-ctx-6",
                "message": "projected user prompt",
                "metadata": {
                    "model": "gpt-4o-mini",
                    "api_key": "test-key",
                    "conversation_history": [{"role": "user", "content": "hello"}],
                },
            },
            "trace": {},
        }
        final_values_chunk = {
            "facts": {
                "conversation_id": "conv-ctx-6",
                "metadata": {"conversation_history": [{"role": "assistant", "content": "after"}]},
            },
            "trace": {"final_text": "done"},
        }

        async def mock_astream(input_state, config, stream_mode):
            yield ("values", first_values_chunk)
            yield ("custom", {"type": "message_delta", "content": "still streaming"})
            yield ("values", final_values_chunk)

        mock_graph.astream = mock_astream
        mock_graph.aupdate_state = AsyncMock()
        decision = ContextWindowDecision(
            snapshot=ContextWindowSnapshot(
                task_id=46,
                conversation_id="conv-ctx-6",
                max_tokens=128_000,
                used_tokens=128_777,
                remaining_tokens=0,
                ratio=1.0,
                ceiling_reached=True,
            ),
            ceiling_reached=True,
            recommended_next_action="compress",
            compression_candidate=True,
        )
        with (
            patch(
                "backend.services.langgraph_chat.execution.graph_executor.ContextWindowManager.evaluate_history",
                return_value=decision,
            ),
            patch(
                "backend.services.langgraph_chat.execution.graph_executor.emit_context_window_event",
                lambda **kwargs: context_events.append(kwargs),
            ),
            patch.object(executor, "_stream_hub") as mock_hub,
        ):
            mock_hub.publish = AsyncMock()
            result = await executor.stream_graph(
                compiled_graph=mock_graph,
                graph_input={},
                config={"configurable": {"thread_id": "task-46", "runtime_services": object()}},
                task_id=46,
            )

        mock_graph.aupdate_state.assert_not_called()
        assert len(context_events) == 1
        assert context_events[0]["compression_pass_count"] is None
        assert context_events[0]["compression_tokens_before"] is None
        assert context_events[0]["compression_tokens_after"] is None
        assert context_events[0]["compression_degraded"] is None
        assert "compression" not in result.metadata["context_window"]
        assert result.final_state == final_values_chunk
        # Ordinary custom streaming continues after ceiling observation.
        assert mock_hub.publish.call_count == 1

    def test_extract_context_window_inputs_preserves_provider_identity(self):
        """Context-window extraction carries provider identity with OpenAI fallback."""
        chunk = {
            "facts": {
                "conversation_id": "conv-provider",
                "message": "next",
                "metadata": {
                    "provider": "openai",
                    "model": "gpt-5.2",
                    "conversation_history": [{"role": "user", "content": "hello"}],
                },
            }
        }

        extracted = LangGraphExecutor._extract_context_window_inputs(chunk)

        assert extracted is not None
        conversation_id, provider, model, history, projected_user_message = extracted
        assert conversation_id == "conv-provider"
        assert provider == "openai"
        assert model == "gpt-5.2"
        assert history == [{"role": "user", "content": "hello"}]
        assert projected_user_message == "next"

    def test_extract_context_window_inputs_defaults_provider_to_openai(self):
        """Legacy context-window metadata remains OpenAI-compatible."""
        chunk = {
            "facts": {
                "conversation_id": "conv-provider",
                "metadata": {
                    "runtime_model": "gpt-5-mini",
                    "conversation_history": [],
                },
            }
        }

        extracted = LangGraphExecutor._extract_context_window_inputs(chunk)

        assert extracted is not None
        assert extracted[1] == "openai"
        assert extracted[2] == "gpt-5-mini"

    def test_extract_context_window_inputs_does_not_mix_metadata_provider_with_runtime_model(self):
        """Runtime model without runtime provider defaults to OpenAI."""
        chunk = {
            "facts": {
                "conversation_id": "conv-provider",
                "metadata": {
                    "provider": "anthropic",
                    "runtime_model": "gpt-5-mini",
                    "conversation_history": [],
                },
            }
        }

        extracted = LangGraphExecutor._extract_context_window_inputs(chunk)

        assert extracted is not None
        assert extracted[1] == "openai"
        assert extracted[2] == "gpt-5-mini"

    def test_extract_context_window_inputs_preserves_runtime_provider_model_pair(self):
        """Runtime provider/model metadata is paired from the same source."""
        chunk = {
            "facts": {
                "conversation_id": "conv-provider",
                "metadata": {
                    "runtime_provider": "anthropic",
                    "runtime_model": "claude-3",
                    "conversation_history": [],
                },
            }
        }

        extracted = LangGraphExecutor._extract_context_window_inputs(chunk)

        assert extracted is not None
        assert extracted[1] == "anthropic"
        assert extracted[2] == "claude-3"


class TestGraphExecutorBatch:
    """Test batch invocation."""
    
    @pytest.mark.asyncio
    async def test_graph_executor_invoke_batch_mode(self):
        """Test batch invocation with ainvoke."""
        executor = LangGraphExecutor()
        
        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={"facts": {}, "trace": {}})
        
        result = await executor.invoke_graph(
            compiled_graph=mock_graph,
            graph_input={"facts": {}, "trace": {}},
            config={"configurable": {"thread_id": "test"}},
        )
        
        # Should return result from ainvoke
        assert result == {"facts": {}, "trace": {}}
        mock_graph.ainvoke.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_graph_executor_invoke_fallback_to_sync(self):
        """Test fallback to sync invoke when ainvoke not available."""
        executor = LangGraphExecutor()
        
        mock_graph = MagicMock()
        # No ainvoke method
        del mock_graph.ainvoke
        mock_graph.invoke = MagicMock(return_value={"facts": {}, "trace": {}})
        
        result = await executor.invoke_graph(
            compiled_graph=mock_graph,
            graph_input={"facts": {}, "trace": {}},
            config={"configurable": {"thread_id": "test"}},
        )
        
        # Should return result from sync invoke
        assert result == {"facts": {}, "trace": {}}
        mock_graph.invoke.assert_called_once()


class TestGraphExecutorEventForwarding:
    """Test event forwarding logic."""
    
    @pytest.mark.asyncio
    async def test_forward_streaming_event_success(self):
        """Test successful event forwarding."""
        executor = LangGraphExecutor()
        
        with patch.object(executor, '_stream_hub') as mock_hub:
            mock_hub.publish = AsyncMock()
            
            await executor._forward_streaming_event(
                task_id=1,
                event={"type": "test", "content": "data"},
            )
            
            # Verify event was published
            mock_hub.publish.assert_called_once_with(
                task_id=1,
                event={"type": "test", "content": "data"},
            )
    
    @pytest.mark.asyncio
    async def test_forward_streaming_event_failure_logs_but_continues(self):
        """Test that forwarding failures are logged but don't raise."""
        executor = LangGraphExecutor()
        
        with patch.object(executor, '_stream_hub') as mock_hub:
            mock_hub.publish = AsyncMock(side_effect=Exception("Hub error"))
            
            # Should not raise
            await executor._forward_streaming_event(
                task_id=1,
                event={"type": "test", "content": "data"},
            )
            
            # Verify publish was attempted
            mock_hub.publish.assert_called_once()


class TestGraphExecutorIntegration:
    """Integration tests with facade."""
    
    @pytest.mark.asyncio
    async def test_executor_can_be_injected_into_facade(self):
        """Test that GraphExecutor can be injected into facade."""
        from backend.services.langgraph_chat.facade import LangGraphChatFacade
        
        executor = LangGraphExecutor()
        facade = LangGraphChatFacade(executor=executor)
        
        assert facade._executor is executor
    
    @pytest.mark.asyncio
    async def test_facade_uses_default_executor_when_none_provided(self):
        """Test that facade creates default GraphExecutor when none provided."""
        from backend.services.langgraph_chat.facade import LangGraphChatFacade
        
        facade = LangGraphChatFacade()
        
        assert isinstance(facade._executor, LangGraphExecutor)
    
    @pytest.mark.asyncio
    async def test_facade_exposes_injected_executor_for_handlers(self):
        """Facade keeps the injected executor on its handlers."""
        from backend.services.langgraph_chat.facade import LangGraphChatFacade
        
        mock_executor = MagicMock()
        
        facade = LangGraphChatFacade(executor=mock_executor)

        assert facade._handlers
        for handler in facade._handlers.values():
            assert handler._executor is mock_executor


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
