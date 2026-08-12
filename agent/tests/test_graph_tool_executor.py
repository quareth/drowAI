"""Tests for the LangGraph-to-executor adapter contract."""

import asyncio
import inspect
import os
import sys
from typing import Any, Dict

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from agent.executor import EnhancedCommandExecutor
from agent.graph.adapters.executor_adapter import GraphToolExecutor
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.models import ExecutionResult
from runtime_shared import shell_session_port
from runtime_shared.shell_session_contracts import (
    ShellExecRequest,
    ShellProcessStatus,
    ShellSessionErrorCode,
    ShellSessionIdentity,
    ShellSessionOrigin,
    ShellSessionUpdate,
    ShellWriteRequest,
)
from runtime_shared.shell_capabilities import ShellCapability


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


class _FakeShellSessionService:
    def __init__(self, update: ShellSessionUpdate) -> None:
        self.update = update
        self.exec_calls: list[tuple[ShellSessionIdentity, ShellExecRequest]] = []
        self.write_calls: list[tuple[ShellSessionIdentity, ShellWriteRequest]] = []
        self.origins: list[ShellSessionOrigin | None] = []
        self.capability = ShellCapability.ASSESSMENT

    async def execute(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellExecRequest,
        capability: ShellCapability = ShellCapability.ASSESSMENT,
        origin: ShellSessionOrigin | None = None,
    ) -> ShellSessionUpdate:
        self.capability = capability
        self.origins.append(origin)
        self.exec_calls.append((identity, request))
        return self.update

    async def get_session_capability(
        self,
        *,
        identity: ShellSessionIdentity,
        public_session_id: str,
    ) -> ShellCapability | None:
        return self.capability

    async def write_stdin(
        self,
        *,
        identity: ShellSessionIdentity,
        request: ShellWriteRequest,
    ) -> ShellSessionUpdate:
        self.write_calls.append((identity, request))
        return self.update

    async def close_owner_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
        execution_owner_id: str,
    ) -> None:
        return None

    async def close_task_sessions(
        self,
        *,
        tenant_id: int,
        task_id: int,
    ) -> None:
        return None


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


def _shell_context(tmp_path) -> GraphRuntimeContext:
    return GraphRuntimeContext(
        task_id=5,
        user_id=7,
        tenant_id=3,
        runtime_placement_mode="runner",
        workspace_id="task-5",
        actor_type="agent",
        actor_id="langgraph",
        workspace_path=str(tmp_path),
        runner_id="runner-1",
        execution_site_id="site-1",
        execution_owner_id="main:turn-1",
    )


@pytest.mark.asyncio
async def test_shell_session_callback_body_does_not_import_backend_services() -> None:
    source = inspect.getsource(GraphToolExecutor._execute_runtime_session_tool_call)

    assert "backend.services" not in source
    assert "RuntimeProviderRegistry" not in source


@pytest.mark.asyncio
async def test_shell_session_unbound_service_returns_structured_unavailable(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shell_session_port, "_shell_session_service_resolver", None)
    stub = _StubExecutor()
    executor = GraphToolExecutor(executor=stub)

    result = await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "sleep 10", "yield_time_ms": 0},
            "tool_call_id": "call-shell",
            "tool_batch_id": "batch-shell",
            "timeout_plan": {
                "tool_id": "shell.exec",
                "deadline_seconds": 5.0,
                "native_timeout_seconds": 5,
                "normalized_parameters": {"command": "sleep 10", "yield_time_ms": 0},
                "source": "test",
            },
        },
        context=_shell_context(tmp_path),
    )

    assert result["success"] is False
    assert result["status"] == "shell_runtime_unavailable"
    assert result["exit_code"] is None
    assert result["metadata"]["error_code"] == "shell_runtime_unavailable"
    assert stub.allow_pty_values == []


@pytest.mark.asyncio
async def test_shell_exec_running_update_maps_session_continuation_fields(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeShellSessionService(
        ShellSessionUpdate(
            success=True,
            status="success",
            process_status=ShellProcessStatus.RUNNING,
            session_id="shs_public123",
            stdout="progress\n",
            stderr="",
            exit_code=None,
            stdin_available=True,
            truncated=False,
            duration_ms=1200,
        )
    )
    monkeypatch.setattr(
        shell_session_port,
        "_shell_session_service_resolver",
        lambda: service,
    )

    executor = GraphToolExecutor(executor=_StubExecutor())
    result = await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "sleep 10", "yield_time_ms": 1000},
            "tool_call_id": "call-shell",
            "tool_batch_id": "batch-shell",
            "timeout_plan": {
                "tool_id": "shell.exec",
                "deadline_seconds": 5.0,
                "native_timeout_seconds": 5,
                "normalized_parameters": {"command": "sleep 10", "yield_time_ms": 1000},
                "source": "test",
            },
        },
        context=_shell_context(tmp_path),
    )

    assert result["success"] is True
    assert result["status"] == "success"
    assert result["process_status"] == "running"
    assert result["session_id"] == "shs_public123"
    assert result["exit_code"] is None
    assert result["stdin_available"] is True
    assert result["metadata"]["runtime_session"]["session_id"] == "shs_public123"
    identity, request = service.exec_calls[0]
    assert identity == ShellSessionIdentity(
        tenant_id=3,
        task_id=5,
        execution_owner_id="main:turn-1",
        runtime_placement_mode="runner",
        workspace_id="task-5",
        workspace_path=str(tmp_path),
        runner_id="runner-1",
        execution_site_id="site-1",
    )
    assert request == ShellExecRequest(command="sleep 10", yield_time_ms=1000)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_id", "expected_capability"),
    [
        ("shell.utility", ShellCapability.UTILITY),
        ("shell.assessment", ShellCapability.ASSESSMENT),
    ],
)
async def test_shell_start_alias_retains_originating_capability(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    tool_id: str,
    expected_capability: ShellCapability,
) -> None:
    service = _FakeShellSessionService(
        ShellSessionUpdate(
            success=True,
            status="success",
            process_status=ShellProcessStatus.RUNNING,
            session_id="shs_capability123",
            stdout="ready\n",
            stdin_available=True,
            duration_ms=10,
        )
    )
    monkeypatch.setattr(
        shell_session_port,
        "_shell_session_service_resolver",
        lambda: service,
    )

    result = await GraphToolExecutor(executor=_StubExecutor()).execute_tool(
        {
            "tool": tool_id,
            "parameters": {"command": "printf ready"},
            "tool_call_id": "call-capability",
            "timeout_plan": {
                "tool_id": tool_id,
                "deadline_seconds": 5.0,
                "native_timeout_seconds": 5,
                "normalized_parameters": {"command": "printf ready"},
                "source": "test",
            },
        },
        context=_shell_context(tmp_path),
    )

    assert service.capability is expected_capability
    assert service.origins == [
        ShellSessionOrigin(
            tool_call_id="call-capability",
            tool_batch_id=None,
            tool_name=tool_id,
        )
    ]
    assert result["metadata"]["runtime_session"]["originating_capability"] == (
        expected_capability.value
    )


@pytest.mark.asyncio
async def test_shell_exec_nonzero_completion_maps_failed_without_fake_exit_code(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeShellSessionService(
        ShellSessionUpdate(
            success=False,
            status="error",
            process_status=ShellProcessStatus.COMPLETED,
            session_id=None,
            stdout="",
            stderr="failed\n",
            exit_code=7,
            stdin_available=False,
            truncated=False,
            duration_ms=50,
        )
    )
    monkeypatch.setattr(
        shell_session_port,
        "_shell_session_service_resolver",
        lambda: service,
    )

    executor = GraphToolExecutor(executor=_StubExecutor())
    result = await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "exit 7"},
            "tool_call_id": "call-shell",
            "timeout_plan": {
                "tool_id": "shell.exec",
                "deadline_seconds": 5.0,
                "native_timeout_seconds": 5,
                "normalized_parameters": {"command": "exit 7"},
                "source": "test",
            },
        },
        context=_shell_context(tmp_path),
    )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["process_status"] == "completed"
    assert result["exit_code"] == 7


@pytest.mark.asyncio
async def test_shell_exec_provider_failure_maps_error_without_local_fallback(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeShellSessionService(
        ShellSessionUpdate(
            success=False,
            status="error",
            process_status=None,
            session_id=None,
            stdout="",
            stderr="provider read failed",
            exit_code=None,
            stdin_available=False,
            truncated=False,
            duration_ms=10,
            error_code=ShellSessionErrorCode.RUNTIME_TRANSPORT_FAILED,
        )
    )
    monkeypatch.setattr(
        shell_session_port,
        "_shell_session_service_resolver",
        lambda: service,
    )
    stub = _StubExecutor()

    executor = GraphToolExecutor(executor=stub)
    result = await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "whoami"},
            "tool_call_id": "call-shell",
            "timeout_plan": {
                "tool_id": "shell.exec",
                "deadline_seconds": 5.0,
                "native_timeout_seconds": 5,
                "normalized_parameters": {"command": "whoami"},
                "source": "test",
            },
        },
        context=_shell_context(tmp_path),
    )

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["exit_code"] is None
    assert result["metadata"]["error_code"] == "runtime_transport_failed"
    assert stub.allow_pty_values == []


@pytest.mark.asyncio
async def test_shell_write_stdin_builds_write_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeShellSessionService(
        ShellSessionUpdate(
            success=True,
            status="success",
            process_status=ShellProcessStatus.RUNNING,
            session_id="shs_public123",
            stdout="accepted\n",
            stderr="",
            exit_code=None,
            stdin_available=True,
            truncated=False,
            duration_ms=20,
        )
    )
    service.capability = ShellCapability.UTILITY
    monkeypatch.setattr(
        shell_session_port,
        "_shell_session_service_resolver",
        lambda: service,
    )

    executor = GraphToolExecutor(executor=_StubExecutor())
    result = await executor.execute_tool(
        {
            "tool": "shell.write_stdin",
            "parameters": {"session_id": "shs_public123", "chars": "yes\n"},
            "tool_call_id": "call-stdin",
            "timeout_plan": {
                "tool_id": "shell.write_stdin",
                "deadline_seconds": 5.0,
                "native_timeout_seconds": 5,
                "normalized_parameters": {
                    "session_id": "shs_public123",
                    "chars": "yes\n",
                },
                "source": "test",
            },
        },
        context=_shell_context(tmp_path),
    )

    assert result["success"] is True
    assert result["process_status"] == "running"
    assert result["metadata"]["runtime_session"]["originating_capability"] == "utility"
    assert service.write_calls
    _, write_request = service.write_calls[0]
    assert write_request == ShellWriteRequest(session_id="shs_public123", chars="yes\n")
