"""Tests for RuntimeWarmupService step orchestration and idempotency."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.langgraph_chat.runtime.warmup_service import RuntimeWarmupService


@pytest.mark.asyncio
async def test_warm_task_runtime_is_idempotent_for_successful_steps() -> None:
    """Successful warmup steps should only execute once per task."""
    mock_checkpointer = AsyncMock()
    mock_checkpointer.setup = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield mock_checkpointer

    checkpointer_service = MagicMock()
    checkpointer_service.get_checkpointer.return_value = _ctx()

    service = RuntimeWarmupService(checkpointer_service=checkpointer_service)
    service._warm_pty_session = AsyncMock()  # type: ignore[method-assign]

    with patch(
        "backend.services.langgraph_chat.runtime.warmup_service.warm_catalog_metadata_snapshot"
    ) as mock_warm_snapshot, patch(
        "backend.services.langgraph_chat.runtime.warmup_service.build_tool_catalog"
    ) as mock_catalog, patch.dict("os.environ", {"ENABLE_PTY_EXECUTION": "true"}):
        first = await service.warm_task_runtime(task_id=101, graph_name="simple_tool")
        second = await service.warm_task_runtime(task_id=101, graph_name="simple_tool")

    assert first["checkpointer"]["ready"] is True
    assert first["tool_catalog"]["ready"] is True
    assert first["pty_session"]["ready"] is True
    assert second["checkpointer"]["ready"] is True
    assert second["tool_catalog"]["ready"] is True
    assert second["pty_session"]["ready"] is True

    mock_checkpointer.setup.assert_not_awaited()
    assert checkpointer_service.get_checkpointer.call_count == 1
    assert mock_warm_snapshot.call_count == 1
    assert mock_catalog.call_count == 1
    assert service._warm_pty_session.await_count == 1


@pytest.mark.asyncio
async def test_warm_task_runtime_reports_step_failures_without_crashing() -> None:
    """Warmup should isolate per-step failures and keep executing later steps."""
    checkpointer_service = MagicMock()
    checkpointer_service.get_checkpointer.side_effect = RuntimeError("cp unavailable")

    service = RuntimeWarmupService(checkpointer_service=checkpointer_service)
    service._warm_pty_session = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with patch(
        "backend.services.langgraph_chat.runtime.warmup_service.warm_catalog_metadata_snapshot"
    ) as mock_warm_snapshot, patch(
        "backend.services.langgraph_chat.runtime.warmup_service.build_tool_catalog"
    ) as mock_catalog, patch.dict("os.environ", {"ENABLE_PTY_EXECUTION": "true"}):
        status = await service.warm_task_runtime(task_id=102, graph_name="deep_reasoning")

    assert status["checkpointer"]["ready"] is False
    assert "cp unavailable" in (status["checkpointer"]["error"] or "")

    assert status["tool_catalog"]["ready"] is True
    assert status["tool_catalog"]["error"] is None
    assert mock_warm_snapshot.call_count == 1
    assert mock_catalog.call_count == 1

    assert status["pty_session"]["ready"] is True
    assert status["pty_session"]["error"] is None
    assert service._warm_pty_session.await_count == 1


@pytest.mark.asyncio
async def test_warm_task_runtime_skips_pty_when_not_required() -> None:
    """PTY warmup should be skipped when feature flag is disabled."""
    mock_checkpointer = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield mock_checkpointer

    checkpointer_service = MagicMock()
    checkpointer_service.get_checkpointer.return_value = _ctx()

    service = RuntimeWarmupService(checkpointer_service=checkpointer_service)
    service._warm_pty_session = AsyncMock()  # type: ignore[method-assign]

    with patch(
        "backend.services.langgraph_chat.runtime.warmup_service.warm_catalog_metadata_snapshot"
    ) as mock_warm_snapshot, patch(
        "backend.services.langgraph_chat.runtime.warmup_service.build_tool_catalog"
    ), patch.dict("os.environ", {"ENABLE_PTY_EXECUTION": "false"}):
        status = await service.warm_task_runtime(task_id=103, graph_name="simple_tool")

    assert status["checkpointer"]["ready"] is True
    assert status["tool_catalog"]["ready"] is True
    assert status["pty_session"]["ready"] is False
    assert status["pty_session"]["skipped"] is True
    assert mock_warm_snapshot.call_count == 1
    assert service._warm_pty_session.await_count == 0


@pytest.mark.asyncio
async def test_warm_task_runtime_without_graph_name_preserves_existing_pty_ready() -> None:
    """Generic warmup must not downgrade an already-warmed PTY session."""
    mock_checkpointer = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield mock_checkpointer

    checkpointer_service = MagicMock()
    checkpointer_service.get_checkpointer.return_value = _ctx()

    service = RuntimeWarmupService(checkpointer_service=checkpointer_service)
    service._warm_pty_session = AsyncMock()  # type: ignore[method-assign]

    with patch(
        "backend.services.langgraph_chat.runtime.warmup_service.warm_catalog_metadata_snapshot"
    ), patch(
        "backend.services.langgraph_chat.runtime.warmup_service.build_tool_catalog"
    ), patch.dict("os.environ", {"ENABLE_PTY_EXECUTION": "true"}):
        warmed = await service.warm_task_runtime(task_id=104, graph_name="simple_tool")
        generic = await service.warm_task_runtime(task_id=104, graph_name=None)

    assert warmed["pty_session"]["ready"] is True
    assert generic["pty_session"]["ready"] is True
    assert generic["pty_session"]["skipped"] is False
    assert service._warm_pty_session.await_count == 1
