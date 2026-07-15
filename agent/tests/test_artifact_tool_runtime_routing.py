"""Runtime routing tests for task-scoped artifact tool execution."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.config import AgentConfig
from agent.executor import EnhancedCommandExecutor
from agent.models import ExecutionResult
from agent.tools.schemas import ToolResult


@pytest.fixture(autouse=True)
def _mock_openai_client():
    with patch("agent.reasoning.enhanced_planner.LLMClientFactory.get_client") as mock_client:
        mock_client.return_value = MagicMock()
        yield


@pytest.mark.asyncio
async def test_artifact_tool_requires_runtime_task_context(tmp_path) -> None:
    """artifact.* tools must fail closed when runtime task context is missing."""
    config = AgentConfig(
        workspace_path=str(tmp_path),
        task_id=None,
        runtime_placement_mode="local",
        openai_api_key="test-key",
        model_name="gpt-4o-mini",
    )
    executor = EnhancedCommandExecutor(config, MagicMock())

    with patch("agent.executor.run_tool_by_name") as mock_direct:
        result = await executor._execute_single_tool_internal("artifact.search", {"limit": 1})

    assert result.success is False
    assert result.exit_code == 2
    assert "require active runtime task context" in result.stderr
    mock_direct.assert_not_called()


@pytest.mark.asyncio
async def test_artifact_tool_skips_file_comm_and_receives_runtime_context(tmp_path) -> None:
    """artifact.* direct execution should bind runtime task context from executor config."""
    config = AgentConfig(
        workspace_path=str(tmp_path),
        task_id="42",
        runtime_placement_mode="local",
        openai_api_key="test-key",
        model_name="gpt-4o-mini",
    )
    executor = EnhancedCommandExecutor(config, MagicMock())
    executor.set_file_comm(object())

    captured = {"task_id": None}

    def _run_tool(_tool_name: str, _params: dict) -> ToolResult:
        from agent.tool_runtime.runtime_context import get_tool_runtime_context

        context = get_tool_runtime_context()
        captured["task_id"] = context.task_id if context is not None else None
        return ToolResult(
            success=True,
            exit_code=0,
            stdout="artifact search ok",
            stderr="",
            artifacts=[],
            metadata={},
            execution_time=0.01,
        )

    with patch.object(executor, "_execute_tool_via_comm", new=AsyncMock(return_value=ExecutionResult(True, "comm", "", 0))) as mock_comm:
        with patch("agent.executor.run_tool_by_name", side_effect=_run_tool) as mock_direct:
            result = await executor._execute_single_tool_internal("artifact.search", {"limit": 1})

    assert result.success is True
    assert result.exit_code == 0
    assert captured["task_id"] == 42
    mock_direct.assert_called_once()
    mock_comm.assert_not_called()


@pytest.mark.asyncio
async def test_backend_scoped_cve_tool_skips_file_comm_and_runs_direct(tmp_path) -> None:
    """Backend-scoped tools should bypass file-comm and execute in host runtime scope."""
    config = AgentConfig(
        workspace_path=str(tmp_path),
        task_id="42",
        runtime_placement_mode="local",
        openai_api_key="test-key",
        model_name="gpt-4o-mini",
    )
    executor = EnhancedCommandExecutor(config, MagicMock())
    executor.set_file_comm(object())

    def _run_tool(_tool_name: str, _params: dict) -> ToolResult:
        assert _tool_name == "knowledge.cve_lookup"
        return ToolResult(
            success=True,
            exit_code=0,
            stdout='{"tool":"knowledge.cve_lookup","matches":[]}',
            stderr="",
            artifacts=[],
            metadata={"cve_lookup": {"status": "ok"}},
            execution_time=0.01,
        )

    with patch.object(
        executor,
        "_execute_tool_via_comm",
        new=AsyncMock(return_value=ExecutionResult(True, "comm", "", 0)),
    ) as mock_comm:
        with patch("agent.executor.run_tool_by_name", side_effect=_run_tool) as mock_direct:
            result = await executor._execute_single_tool_internal(
                "knowledge.cve_lookup",
                {"product": "PostgreSQL", "version": "9.6.0"},
            )

    assert result.success is True
    assert result.exit_code == 0
    mock_direct.assert_called_once()
    mock_comm.assert_not_called()


@pytest.mark.asyncio
async def test_backend_scoped_cve_tool_executes_via_to_thread(tmp_path) -> None:
    """Backend-scoped direct execution should be offloaded via asyncio.to_thread."""
    config = AgentConfig(
        workspace_path=str(tmp_path),
        task_id="42",
        runtime_placement_mode="local",
        openai_api_key="test-key",
        model_name="gpt-4o-mini",
    )
    executor = EnhancedCommandExecutor(config, MagicMock())
    executor.set_file_comm(object())

    def _run_tool(_tool_name: str, _params: dict) -> ToolResult:
        return ToolResult(
            success=True,
            exit_code=0,
            stdout='{"tool":"knowledge.cve_lookup","matches":[]}',
            stderr="",
            artifacts=[],
            metadata={"cve_lookup": {"status": "ok"}},
            execution_time=0.01,
        )

    async def _fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch.object(
        executor,
        "_execute_tool_via_comm",
        new=AsyncMock(return_value=ExecutionResult(True, "comm", "", 0)),
    ) as mock_comm:
        with patch("agent.executor.run_tool_by_name", side_effect=_run_tool):
            with patch("agent.tool_runtime.transport_router.asyncio.to_thread", side_effect=_fake_to_thread) as mock_to_thread:
                result = await executor._execute_single_tool_internal(
                    "knowledge.cve_lookup",
                    {"product": "PostgreSQL", "version": "9.6.0"},
                )

    assert result.success is True
    mock_to_thread.assert_called_once()
    mock_comm.assert_not_called()


@pytest.mark.asyncio
async def test_backend_scoped_cve_tool_does_not_block_event_loop(tmp_path) -> None:
    """Slow backend-scoped direct tool execution should not starve other coroutines."""
    config = AgentConfig(
        workspace_path=str(tmp_path),
        task_id="42",
        runtime_placement_mode="local",
        openai_api_key="test-key",
        model_name="gpt-4o-mini",
    )
    executor = EnhancedCommandExecutor(config, MagicMock())
    executor.set_file_comm(object())

    def _run_tool(_tool_name: str, _params: dict) -> ToolResult:
        time.sleep(0.25)
        return ToolResult(
            success=True,
            exit_code=0,
            stdout='{"tool":"knowledge.cve_lookup","matches":[]}',
            stderr="",
            artifacts=[],
            metadata={"cve_lookup": {"status": "ok"}},
            execution_time=0.25,
        )

    ticks = {"count": 0, "running": True}

    async def _heartbeat() -> None:
        while ticks["running"]:
            ticks["count"] += 1
            await asyncio.sleep(0.01)

    with patch("agent.executor.run_tool_by_name", side_effect=_run_tool):
        heartbeat_task = asyncio.create_task(_heartbeat())
        await asyncio.sleep(0)
        result = await executor._execute_single_tool_internal(
            "knowledge.cve_lookup",
            {"product": "PostgreSQL", "version": "9.6.0"},
        )
        ticks["running"] = False
        await heartbeat_task

    assert result.success is True
    assert ticks["count"] >= 5


@pytest.mark.asyncio
async def test_runner_mode_blocks_management_artifact_tool_before_direct_lane(tmp_path) -> None:
    """Runner mode should reject artifact-scoped tools before local direct fallback."""
    config = AgentConfig(
        workspace_path=str(tmp_path),
        task_id="42",
        runtime_placement_mode="runner",
        openai_api_key="test-key",
        model_name="gpt-4o-mini",
    )
    executor = EnhancedCommandExecutor(config, MagicMock())
    executor.set_file_comm(object())

    with patch.object(
        executor,
        "_execute_tool_via_comm",
        new=AsyncMock(return_value=ExecutionResult(True, "comm", "", 0)),
    ) as mock_comm:
        with patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal("artifact.search", {"limit": 1})

    assert result.success is False
    assert result.exit_code == 2
    assert result.metadata["error_code"] == "unsupported_management_artifact_tool_runner_v1"
    mock_comm.assert_not_called()
    mock_direct.assert_not_called()


@pytest.mark.asyncio
async def test_runner_mode_blocks_management_knowledge_tool_before_direct_lane(tmp_path) -> None:
    """Runner mode should reject backend-scoped tools before local direct fallback."""
    config = AgentConfig(
        workspace_path=str(tmp_path),
        task_id="42",
        runtime_placement_mode="runner",
        openai_api_key="test-key",
        model_name="gpt-4o-mini",
    )
    executor = EnhancedCommandExecutor(config, MagicMock())
    executor.set_file_comm(object())

    with patch.object(
        executor,
        "_execute_tool_via_comm",
        new=AsyncMock(return_value=ExecutionResult(True, "comm", "", 0)),
    ) as mock_comm:
        with patch("agent.executor.run_tool_by_name") as mock_direct:
            result = await executor._execute_single_tool_internal(
                "knowledge.cve_lookup",
                {"product": "PostgreSQL", "version": "9.6.0"},
            )

    assert result.success is False
    assert result.exit_code == 2
    assert result.metadata["error_code"] == "unsupported_management_knowledge_tool_runner_v1"
    mock_comm.assert_not_called()
    mock_direct.assert_not_called()
