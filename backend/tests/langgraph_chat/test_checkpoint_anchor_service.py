"""Tests for generic LangGraph checkpoint anchor resolution."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.langgraph_chat.checkpoint.anchor_service import (
    CheckpointAnchorService,
)

GRAPH_THREAD_ID = "a" * 32


class _DummyCheckpointerService:
    """Minimal async checkpointer provider for resolver tests."""

    def __init__(self) -> None:
        self.checkpointer = AsyncMock()

    @asynccontextmanager
    async def get_checkpointer(self, _task_id: int):
        yield self.checkpointer


@pytest.mark.asyncio
async def test_resolve_latest_anchor_extracts_checkpoint_from_state_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpointer_service = _DummyCheckpointerService()
    service = CheckpointAnchorService(checkpointer_service=checkpointer_service)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.anchor_service._load_graph_thread_id",
        lambda *, task_id: GRAPH_THREAD_ID,
    )

    state_snapshot = MagicMock()
    state_snapshot.config = {"configurable": {"checkpoint_id": "ckpt-stable-1"}}

    compiled = MagicMock()
    compiled.aget_state = AsyncMock(return_value=state_snapshot)

    with patch(
        "agent.graph.builders.simple_tool_builder.build_simple_tool_graph",
        return_value=compiled,
    ) as build_simple_tool_graph:
        anchor = await service.resolve_latest_anchor(
            task_id=501,
            graph_name="simple_tool",
        )

    assert anchor is not None
    assert anchor.task_id == 501
    assert anchor.graph_name == "simple_tool"
    assert anchor.thread_id == f"graph-{GRAPH_THREAD_ID}"
    assert anchor.checkpoint_id == "ckpt-stable-1"
    build_simple_tool_graph.assert_called_once_with(
        checkpointer=checkpointer_service.checkpointer
    )
    compiled.aget_state.assert_awaited_once_with(
        {
            "configurable": {
                "thread_id": f"graph-{GRAPH_THREAD_ID}",
                "graph_name": "simple_tool",
            }
        }
    )


@pytest.mark.asyncio
async def test_resolve_latest_anchor_returns_none_when_checkpoint_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpointer_service = _DummyCheckpointerService()
    service = CheckpointAnchorService(checkpointer_service=checkpointer_service)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.anchor_service._load_graph_thread_id",
        lambda *, task_id: GRAPH_THREAD_ID,
    )

    state_snapshot = MagicMock()
    state_snapshot.config = {"configurable": {}}

    compiled = MagicMock()
    compiled.aget_state = AsyncMock(return_value=state_snapshot)

    with patch(
        "agent.graph.builders.simple_tool_builder.build_simple_tool_graph",
        return_value=compiled,
    ):
        anchor = await service.resolve_latest_anchor(
            task_id=502,
            graph_name="simple_tool",
        )

    assert anchor is None


@pytest.mark.asyncio
async def test_resolve_latest_anchor_without_graph_name_preserves_simple_tool_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpointer_service = _DummyCheckpointerService()
    service = CheckpointAnchorService(checkpointer_service=checkpointer_service)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.anchor_service._load_graph_thread_id",
        lambda *, task_id: GRAPH_THREAD_ID,
    )

    state_snapshot = MagicMock()
    state_snapshot.config = {"configurable": {"checkpoint_id": "simple-ckpt-1"}}

    compiled = MagicMock()
    compiled.aget_state = AsyncMock(return_value=state_snapshot)

    with patch(
        "agent.graph.builders.simple_tool_builder.build_simple_tool_graph",
        return_value=compiled,
    ) as build_simple_tool_graph, patch(
        "agent.graph.builders.deep_reasoning_builder.compile_deep_reasoning_graph",
        return_value=MagicMock(),
    ) as compile_deep_reasoning_graph:
        anchor = await service.resolve_latest_anchor(task_id=503)

    assert anchor is not None
    assert anchor.graph_name == "simple_tool"
    assert anchor.checkpoint_id == "simple-ckpt-1"
    build_simple_tool_graph.assert_called_once_with(
        checkpointer=checkpointer_service.checkpointer
    )
    compile_deep_reasoning_graph.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_latest_anchor_uses_deep_reasoning_when_specified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpointer_service = _DummyCheckpointerService()
    service = CheckpointAnchorService(checkpointer_service=checkpointer_service)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.anchor_service._load_graph_thread_id",
        lambda *, task_id: GRAPH_THREAD_ID,
    )

    state_snapshot = MagicMock()
    state_snapshot.config = {"configurable": {"checkpoint_id": "dr-ckpt-1"}}

    compiled = MagicMock()
    compiled.aget_state = AsyncMock(return_value=state_snapshot)

    with patch(
        "agent.graph.builders.deep_reasoning_builder.compile_deep_reasoning_graph",
        return_value=compiled,
    ) as compile_deep_reasoning_graph, patch(
        "agent.graph.builders.simple_tool_builder.build_simple_tool_graph",
        return_value=MagicMock(),
    ) as build_simple_tool_graph:
        anchor = await service.resolve_latest_anchor(
            task_id=504,
            graph_name="deep_reasoning",
        )

    assert anchor is not None
    assert anchor.graph_name == "deep_reasoning"
    assert anchor.checkpoint_id == "dr-ckpt-1"
    compile_deep_reasoning_graph.assert_called_once_with(
        checkpointer=checkpointer_service.checkpointer
    )
    build_simple_tool_graph.assert_not_called()
