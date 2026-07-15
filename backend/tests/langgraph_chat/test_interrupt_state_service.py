"""Tests for InterruptStateService (HITL interrupt state from checkpointer)."""

import os

# Set mock DATABASE_URL before any imports
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.langgraph_chat.checkpoint.interrupt_state_service import (
    InterruptStateService,
    get_interrupt_state_service,
)

GRAPH_THREAD_ID = "a" * 32


class TestInterruptStateServicePendingInterrupt:
    """Test get_pending_interrupt method."""

    @pytest.mark.asyncio
    async def test_get_pending_interrupt_returns_payload_when_interrupted(self):
        """Test that service returns interrupt payload when graph is interrupted."""
        service = InterruptStateService()

        # Mock checkpointer
        mock_checkpointer = AsyncMock()

        @asynccontextmanager
        async def mock_get_checkpointer(task_id):
            yield mock_checkpointer

        service._checkpointer.get_checkpointer = mock_get_checkpointer

        # Mock compiled graph with aget_state returning interrupt state
        mock_interrupt = MagicMock()
        mock_interrupt.value = {
            "type": "tool_approval",
            "interrupt_id": "intr-123",
            "tool_id": "network.nmap",
            "tool_name": "Nmap",
            "parameters": {"target": "192.168.1.1"},
            "description": "Scan network",
            "reserved_message_id": 55,
        }
        mock_interrupt.resumable = True

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_compiled = MagicMock()
        mock_compiled.aget_state = AsyncMock(return_value=mock_state_snapshot)

        with patch(
            "agent.graph.builders.simple_tool_builder.build_simple_tool_graph"
        ) as mock_build:
            mock_build.return_value = mock_compiled

            result = await service.get_pending_interrupt(
                task_id=1,
                graph_thread_id=GRAPH_THREAD_ID,
            )

            assert result is not None
            assert result["task_id"] == 1
            assert result["thread_id"] == f"graph-{GRAPH_THREAD_ID}"
            assert result["graph_name"] == "simple_tool"
            assert result["interrupt_type"] == "tool_approval"
            assert result["interrupt_id"] == "intr-123"
            assert result["payload"]["tool_id"] == "network.nmap"
            assert result["payload"]["interrupt_id"] == "intr-123"
            assert result["resumable"] is True
            assert result["reserved_message_id"] == 55

    @pytest.mark.asyncio
    async def test_get_pending_interrupt_synthesizes_interrupt_id_from_checkpoint(self):
        """Legacy payloads without interrupt_id synthesize one from checkpoint id."""
        service = InterruptStateService()

        mock_checkpointer = AsyncMock()

        @asynccontextmanager
        async def mock_get_checkpointer(task_id):
            yield mock_checkpointer

        service._checkpointer.get_checkpointer = mock_get_checkpointer

        mock_interrupt = MagicMock()
        mock_interrupt.value = {
            "type": "tool_approval",
            "tool_id": "network.nmap",
            "tool_name": "Nmap",
            "parameters": {"target": "192.168.1.1"},
            "description": "Scan network",
            "reserved_message_id": 55,
        }
        mock_interrupt.resumable = True

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]
        mock_state_snapshot.config = {"configurable": {"checkpoint_id": "cp-1"}}

        mock_compiled = MagicMock()
        mock_compiled.aget_state = AsyncMock(return_value=mock_state_snapshot)

        with patch(
            "agent.graph.builders.simple_tool_builder.build_simple_tool_graph"
        ) as mock_build:
            mock_build.return_value = mock_compiled

            result = await service.get_pending_interrupt(
                task_id=1,
                graph_thread_id=GRAPH_THREAD_ID,
            )

            assert result is not None
            assert result["checkpoint_id"] == "cp-1"
            assert result["interrupt_id"] == "simple_tool:checkpoint:cp-1"
            assert result["payload"]["interrupt_id"] == "simple_tool:checkpoint:cp-1"

    @pytest.mark.asyncio
    async def test_get_pending_interrupt_synthesizes_interrupt_id_from_turn_id(self):
        """Turn id is used when checkpoint_id and interrupt_id are missing."""
        service = InterruptStateService()

        mock_checkpointer = AsyncMock()

        @asynccontextmanager
        async def mock_get_checkpointer(task_id):
            yield mock_checkpointer

        service._checkpointer.get_checkpointer = mock_get_checkpointer

        mock_interrupt = MagicMock()
        mock_interrupt.value = {
            "type": "tool_approval",
            "turn_id": "turn-42",
            "tool_id": "network.nmap",
            "tool_name": "Nmap",
            "parameters": {"target": "192.168.1.1"},
            "description": "Scan network",
        }
        mock_interrupt.resumable = True

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_compiled = MagicMock()
        mock_compiled.aget_state = AsyncMock(return_value=mock_state_snapshot)

        with patch(
            "agent.graph.builders.simple_tool_builder.build_simple_tool_graph"
        ) as mock_build:
            mock_build.return_value = mock_compiled

            result = await service.get_pending_interrupt(
                task_id=1,
                graph_thread_id=GRAPH_THREAD_ID,
            )

            assert result is not None
            assert result["interrupt_id"] == "simple_tool:turn:turn-42"
            assert result["payload"]["interrupt_id"] == "simple_tool:turn:turn-42"

    @pytest.mark.asyncio
    async def test_get_pending_interrupt_returns_none_when_no_interrupt(self):
        """Test that service returns None when no interrupt pending."""
        service = InterruptStateService()

        # Mock checkpointer
        mock_checkpointer = AsyncMock()

        @asynccontextmanager
        async def mock_get_checkpointer(task_id):
            yield mock_checkpointer

        service._checkpointer.get_checkpointer = mock_get_checkpointer

        # Mock compiled graph with no tasks (no interrupt)
        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = []

        mock_compiled = MagicMock()
        mock_compiled.aget_state = AsyncMock(return_value=mock_state_snapshot)

        with patch(
            "agent.graph.builders.simple_tool_builder.build_simple_tool_graph"
        ) as mock_build:
            mock_build.return_value = mock_compiled

            result = await service.get_pending_interrupt(
                task_id=1,
                graph_thread_id=GRAPH_THREAD_ID,
            )

            assert result is None

    @pytest.mark.asyncio
    async def test_get_pending_interrupt_returns_none_when_no_state(self):
        """Test that service returns None when no state exists."""
        service = InterruptStateService()

        # Mock checkpointer
        mock_checkpointer = AsyncMock()

        @asynccontextmanager
        async def mock_get_checkpointer(task_id):
            yield mock_checkpointer

        service._checkpointer.get_checkpointer = mock_get_checkpointer

        # Mock compiled graph returning None state
        mock_compiled = MagicMock()
        mock_compiled.aget_state = AsyncMock(return_value=None)

        with patch(
            "agent.graph.builders.simple_tool_builder.build_simple_tool_graph"
        ) as mock_build:
            mock_build.return_value = mock_compiled

            result = await service.get_pending_interrupt(
                task_id=1,
                graph_thread_id=GRAPH_THREAD_ID,
            )

            assert result is None

    @pytest.mark.asyncio
    async def test_get_pending_interrupt_handles_error_gracefully(self):
        """Test that service returns None on checkpointer errors."""
        service = InterruptStateService()

        # Mock checkpointer to raise exception
        @asynccontextmanager
        async def mock_get_checkpointer_error(task_id):
            raise RuntimeError("Database connection failed")
            yield  # Never reached, but needed for generator

        service._checkpointer.get_checkpointer = mock_get_checkpointer_error

        result = await service.get_pending_interrupt(
            task_id=1,
            graph_thread_id=GRAPH_THREAD_ID,
        )

        # Should return None rather than raising
        assert result is None


class TestInterruptStateServiceHasPending:
    """Test has_pending_interrupt method."""

    @pytest.mark.asyncio
    async def test_has_pending_interrupt_true_when_exists(self):
        """Test has_pending_interrupt returns True when interrupt exists."""
        service = InterruptStateService()

        # Mock get_pending_interrupt to return a payload
        async def mock_get_pending(task_id, graph_name="simple_tool", **_kwargs):
            return {
                "task_id": task_id,
                "thread_id": f"graph-{GRAPH_THREAD_ID}",
                "graph_name": graph_name,
                "interrupt_type": "tool_approval",
                "payload": {"tool_id": "test"},
                "resumable": True,
            }

        service.get_pending_interrupt = mock_get_pending

        result = await service.has_pending_interrupt(
            task_id=1,
            graph_thread_id=GRAPH_THREAD_ID,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_has_pending_interrupt_false_when_none(self):
        """Test has_pending_interrupt returns False when no interrupt."""
        service = InterruptStateService()

        # Mock get_pending_interrupt to return None
        async def mock_get_pending(task_id, graph_name="simple_tool", **_kwargs):
            return None

        service.get_pending_interrupt = mock_get_pending

        result = await service.has_pending_interrupt(
            task_id=1,
            graph_thread_id=GRAPH_THREAD_ID,
        )

        assert result is False


class TestInterruptStateServiceGraphSelection:
    """Test graph selection based on graph_name parameter."""

    @pytest.mark.asyncio
    async def test_uses_simple_tool_graph_by_default(self):
        """When graph_name is None, both graphs are checked (simple_tool first, then deep_reasoning)."""
        service = InterruptStateService()

        # Mock checkpointer
        mock_checkpointer = AsyncMock()

        @asynccontextmanager
        async def mock_get_checkpointer(task_id):
            yield mock_checkpointer

        service._checkpointer.get_checkpointer = mock_get_checkpointer

        # Mock state with no tasks (no interrupt found per graph)
        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = []

        mock_compiled = MagicMock()
        mock_compiled.aget_state = AsyncMock(return_value=mock_state_snapshot)

        with patch(
            "agent.graph.builders.simple_tool_builder.build_simple_tool_graph"
        ) as mock_simple:
            with patch(
                "agent.graph.builders.deep_reasoning_builder.compile_deep_reasoning_graph"
            ) as mock_deep:
                mock_simple.return_value = mock_compiled
                mock_deep.return_value = mock_compiled

                await service.get_pending_interrupt(
                    task_id=1,
                    graph_thread_id=GRAPH_THREAD_ID,
                )

                # When graph_name is None, service checks both graphs in order
                mock_simple.assert_called_once()
                mock_deep.assert_called_once()

    @pytest.mark.asyncio
    async def test_uses_deep_reasoning_graph_when_specified(self):
        """Test that deep_reasoning graph is used when specified."""
        service = InterruptStateService()

        # Mock checkpointer
        mock_checkpointer = AsyncMock()

        @asynccontextmanager
        async def mock_get_checkpointer(task_id):
            yield mock_checkpointer

        service._checkpointer.get_checkpointer = mock_get_checkpointer

        # Mock state with no tasks
        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = []

        mock_compiled = MagicMock()
        mock_compiled.aget_state = AsyncMock(return_value=mock_state_snapshot)

        with patch(
            "agent.graph.builders.simple_tool_builder.build_simple_tool_graph"
        ) as mock_simple:
            with patch(
                "agent.graph.builders.deep_reasoning_builder.compile_deep_reasoning_graph"
            ) as mock_deep:
                mock_simple.return_value = mock_compiled
                mock_deep.return_value = mock_compiled

                await service.get_pending_interrupt(
                    task_id=1,
                    graph_thread_id=GRAPH_THREAD_ID,
                    graph_name="deep_reasoning",
                )

                # Should use deep_reasoning graph
                mock_deep.assert_called_once()
                mock_simple.assert_not_called()


class TestInterruptStateServiceSingleton:
    """Test singleton instance."""

    def test_get_interrupt_state_service_returns_singleton(self):
        """Test that get_interrupt_state_service returns same instance."""
        # Reset singleton for test
        import backend.services.langgraph_chat.checkpoint.interrupt_state_service as module
        module._interrupt_service = None

        service1 = get_interrupt_state_service()
        service2 = get_interrupt_state_service()

        assert service1 is service2

    def test_singleton_is_interrupt_state_service_instance(self):
        """Test that singleton is InterruptStateService instance."""
        import backend.services.langgraph_chat.checkpoint.interrupt_state_service as module
        module._interrupt_service = None

        service = get_interrupt_state_service()

        assert isinstance(service, InterruptStateService)


class TestInterruptStateServiceIntegration:
    """Integration tests."""

    @pytest.mark.asyncio
    async def test_service_can_be_injected_with_custom_checkpointer(self):
        """Test that service accepts custom checkpointer service."""
        from backend.services.langgraph_chat.checkpoint.checkpointer_service import CheckpointerService

        custom_checkpointer = CheckpointerService()
        service = InterruptStateService(checkpointer_service=custom_checkpointer)

        assert service._checkpointer is custom_checkpointer

    @pytest.mark.asyncio
    async def test_service_creates_default_checkpointer_when_none_provided(self):
        """Test that service creates default CheckpointerService."""
        from backend.services.langgraph_chat.checkpoint.checkpointer_service import CheckpointerService

        service = InterruptStateService()

        assert isinstance(service._checkpointer, CheckpointerService)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
