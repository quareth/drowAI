"""Tests for the LangGraph-to-executor adapter contract."""

import asyncio
import os
import sys
from typing import Any, Dict

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from agent.executor import EnhancedCommandExecutor
from agent.graph.adapters.executor_adapter import GraphToolExecutor
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.models import ExecutionResult


class _StubExecutor:
    def __init__(self) -> None:
        self._last_action = None
        self.approval_calls = 0
        self.allow_pty_values: list[bool] = []
        self.session_names: list[str | None] = []
        self.cleanup_values: list[bool] = []
        self.artifact_stamps: list[int | None] = []

    def set_scope_validator(self, validator) -> None:  # pragma: no cover - compatibility
        self.validator = validator

    async def _maybe_request_approval(self, tool: str, params: Dict[str, Any], reasoning: str) -> bool:
        self.approval_calls += 1
        return True

    async def _execute_single_tool(
        self,
        tool: str,
        params: Dict[str, Any],
        *,
        interrupt_id: str | None = None,
        tool_call_id: str | None = None,
        tool_batch_id: str | None = None,
        session_name: str | None = None,
        cleanup_session: bool = False,
        artifact_stamp: int | None = None,
        allow_pty: bool = True,
    ) -> ExecutionResult:
        _ = interrupt_id, tool_call_id, tool_batch_id
        self.allow_pty_values.append(allow_pty)
        self.session_names.append(session_name)
        self.cleanup_values.append(cleanup_session)
        self.artifact_stamps.append(artifact_stamp)
        result = ExecutionResult(True, "execution-complete", "", 0)
        setattr(result, "artifacts", ["artifacts/tool-output.txt"])
        setattr(
            result,
            "metadata",
            {
                "tool_metadata": {"parser": "ok"},
                "semantic_observations": [{"observation_type": "test.semantic"}],
            },
        )
        return result


def _build_state() -> InteractiveState:
    facts = FactsState(
        task_id=42,
        message="Scan 127.0.0.1 with nmap",
        capability="scan_ports",
        tool_candidates=["information_gathering.network_discovery.nmap"],
        intent_hints={"targets": ["127.0.0.1"]},
    )
    trace = TraceState(reasoning=["Plan selected tool execution."])
    return InteractiveState(facts=facts, trace=trace)


def test_create_tool_request_populates_target() -> None:
    state = _build_state()
    executor = GraphToolExecutor(executor=_StubExecutor())
    request = executor.create_tool_request(state)

    assert request["tool"] == "information_gathering.network_discovery.nmap"
    assert request["parameters"]["target"] == "127.0.0.1"
    assert request["reasoning"]


def test_execute_tool_invokes_underlying_executor(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.environ["WORKSPACE"] = str(workspace)

    stub = _StubExecutor()
    executor = GraphToolExecutor(executor=stub)

    state = _build_state()
    state.facts.metadata["graph_runtime_context"] = {"workspace_path": str(workspace)}
    request = executor.create_tool_request(state)
    request["runtime_placement_mode"] = "local"

    result = asyncio.run(executor.execute_tool(request))

    assert result["success"] is True
    assert result["stdout"] == "execution-complete"
    assert result["artifacts"] == ["artifacts/tool-output.txt"]
    assert result["metadata"]["tool_metadata"]["parser"] == "ok"
    assert result["metadata"]["semantic_observations"] == [{"observation_type": "test.semantic"}]
    assert stub.approval_calls == 1


def test_execute_tool_disables_pty_for_parallel_strategy_without_call_identity(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.environ["WORKSPACE"] = str(workspace)

    stub = _StubExecutor()
    executor = GraphToolExecutor(executor=stub)

    request = executor.create_tool_request(_build_state())
    request["workspace_path"] = str(workspace)
    request["runtime_placement_mode"] = "local"
    request["execution_strategy"] = "parallel"

    asyncio.run(executor.execute_tool(request))

    assert stub.allow_pty_values == [False]
    assert stub.session_names == [None]


def test_execute_tool_uses_named_pty_identity_for_parallel_call(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.environ["WORKSPACE"] = str(workspace)

    stub = _StubExecutor()
    executor = GraphToolExecutor(executor=stub)

    request = executor.create_tool_request(_build_state())
    request["workspace_path"] = str(workspace)
    request["runtime_placement_mode"] = "local"
    request["execution_strategy"] = "parallel"
    request["tool_batch_id"] = "tb_test"
    request["tool_call_id"] = "tc_1"

    asyncio.run(executor.execute_tool(request))

    assert stub.allow_pty_values == [True]
    assert stub.session_names[0] is not None
    assert "tb_test" in stub.session_names[0]
    assert "tc_1" in stub.session_names[0]
    assert stub.cleanup_values == [True]
    assert isinstance(stub.artifact_stamps[0], int)


def test_execute_tool_constructs_executor_without_openai_key(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.environ["WORKSPACE"] = str(workspace)

    async def _allow_approval(self, tool: str, params: Dict[str, Any], reasoning: str) -> bool:
        return True

    async def _execute_single_tool(
        self,
        tool: str,
        params: Dict[str, Any],
        **kwargs: Any,
    ) -> ExecutionResult:
        assert getattr(self.config, "openai_api_key", None) is None
        result = ExecutionResult(True, "runtime-ok", "", 0)
        setattr(result, "metadata", {})
        return result

    monkeypatch.setattr(EnhancedCommandExecutor, "_maybe_request_approval", _allow_approval)
    monkeypatch.setattr(EnhancedCommandExecutor, "_execute_single_tool", _execute_single_tool)

    executor = GraphToolExecutor()
    request = executor.create_tool_request(_build_state())
    request["workspace_path"] = str(workspace)
    request["runtime_placement_mode"] = "local"

    result = asyncio.run(executor.execute_tool(request))

    assert result["success"] is True
    assert result["stdout"] == "runtime-ok"


def test_execute_tool_missing_placement_fails_before_local_executor(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    stub = _StubExecutor()
    executor = GraphToolExecutor(executor=stub)
    request = executor.create_tool_request(_build_state())
    request["workspace_path"] = str(workspace)

    result = asyncio.run(executor.execute_tool(request))

    assert result["success"] is False
    assert result["status"] == "missing_runtime_placement"
    assert result["metadata"]["error_code"] == "missing_runtime_placement"
    assert stub.approval_calls == 0
    assert stub.allow_pty_values == []
