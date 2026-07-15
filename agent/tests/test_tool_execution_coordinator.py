"""Tests for the planner/executor coordinator used by tool runtime."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from agent.config import AgentConfig
from agent.models import ActionType, ExecutionResult, ExecutionStrategy
from agent.reasoning.enhanced_planner import PlannerToolParameterValidationError
from agent.tool_runtime import (
    ToolExecutionCoordinator,
    ToolExecutionOutcome,
    ToolExecutionRequest,
)
from agent.tool_runtime.artifact_tool_policy import (
    ARTIFACT_READ_TOOL_ID,
    ARTIFACT_SEARCH_TOOL_ID,
)


class _StubPlanner:
    def __init__(self) -> None:
        self.calls = 0

    async def build_action_plan(self, action, context: Dict[str, Any]):  # noqa: ANN001
        self.calls += 1
        target = context["targets"][0]
        tool_id = "information_gathering.network_discovery.nmap"
        parameters = {"target": target, "ports": "1-1024"}

        class _ToolCall:
            pass

        tool_call = _ToolCall()
        tool_call.tool_id = tool_id
        tool_call.parameters = parameters
        tool_call.tool_call_id = "stub-call"
        tool_call.intent = "port scan"

        class _ToolBatch:
            pass

        tool_batch = _ToolBatch()
        tool_batch.tool_batch_id = "stub-batch"
        tool_batch.requested_execution_strategy = ExecutionStrategy.SEQUENTIAL
        tool_batch.deferred_followups = ()
        tool_batch.selection_rationale = "Selected nmap for port scanning"
        tool_batch.tool_calls = (tool_call,)

        class _Plan:
            pass

        plan = _Plan()
        plan.selected_tools = [tool_id]
        plan.tool_parameters = {tool_id: parameters}
        plan.reasoning = "Selected nmap for port scanning"
        plan.expected_outcome = "Open ports discovered"
        plan.execution_strategy = ExecutionStrategy.SEQUENTIAL
        plan.tool_batch = tool_batch
        return plan


class _StubExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.last_request: Dict[str, Any] | None = None

    async def execute_tool(self, request: Dict[str, Any]):  # noqa: ANN001
        self.calls += 1
        self.last_request = dict(request)
        return {
            "tool": request["tool"],
            "success": True,
            "stdout": "Scan complete",
            "stderr": "",
            "stdout_excerpt": "Scan complete",
            "stderr_excerpt": "",
            "observation": "Found ports 22,80",
            "status": "success",
            "duration": 0.42,
        }


class _RawObservationExecutor(_StubExecutor):
    async def execute_tool(self, request: Dict[str, Any]):  # noqa: ANN001
        base = await super().execute_tool(request)
        base["observation"] = (
            "Traceback (most recent call last):\n"
            "  File \"/app/alembic.py\", line 10, in <module>\n"
            "sqlalchemy.exc.NotSupportedError: extension vector is not available"
        )
        base["stderr"] = base["observation"]
        base["stderr_excerpt"] = base["observation"]
        base["success"] = False
        base["status"] = "error"
        return base


class _ValidationErrorPlanner:
    async def build_action_plan(self, action, context: Dict[str, Any]):  # noqa: ANN001
        raise PlannerToolParameterValidationError(
            tool_id="shell.exec",
            reason="schema_validation_error",
            validation_errors=[
                {
                    "field": "command",
                    "error": "Field required",
                    "message": "Field required",
                    "suggested_fix": "Provide a value for 'command'",
                }
            ],
            raw_arguments="{}",
            provided_parameters={"target": "127.0.0.1"},
        )


@pytest.mark.asyncio
async def test_coordinator_executes_planned_tool():
    config = AgentConfig(task_id="123", workspace_path="/tmp", openai_api_key="key", model_name="model")
    planner = _StubPlanner()
    executor = _StubExecutor()

    coordinator = ToolExecutionCoordinator(config=config, planner=planner, executor=executor)

    request = ToolExecutionRequest(
        capability=ActionType.SCAN_PORTS.value,
        targets=["10.0.0.1"],
        message="Run nmap",
        task_id=123,
        metadata={},
    )

    outcome = await coordinator.run(request)

    assert isinstance(outcome, ToolExecutionOutcome)
    assert outcome.tool_id == "information_gathering.network_discovery.nmap"
    assert outcome.parameters["target"] == "10.0.0.1"
    assert outcome.result["success"] is True
    assert outcome.summary
    assert planner.calls == 1
    assert executor.calls == 1


@pytest.mark.asyncio
async def test_coordinator_forwards_hitl_correlation_ids() -> None:
    """Coordinator forwards interrupt/tool call IDs into executor request."""
    config = AgentConfig(task_id="123", workspace_path="/tmp", openai_api_key="key", model_name="model")
    planner = _StubPlanner()
    executor = _StubExecutor()
    coordinator = ToolExecutionCoordinator(config=config, planner=planner, executor=executor)

    request = ToolExecutionRequest(
        capability=ActionType.SCAN_PORTS.value,
        targets=["10.0.0.2"],
        message="Run nmap",
        task_id=123,
        metadata={"interrupt_id": "it-123", "tool_call_id": "tc-456"},
    )

    await coordinator.run(request)

    assert executor.last_request is not None
    assert executor.last_request.get("interrupt_id") == "it-123"
    assert executor.last_request.get("tool_call_id") == "tc-456"


def test_run_tool_turn_sync():
    config = AgentConfig(task_id="123", workspace_path="/tmp", openai_api_key="key", model_name="model")
    planner = _StubPlanner()
    executor = _StubExecutor()

    coordinator = ToolExecutionCoordinator(config=config, planner=planner, executor=executor)

    request = ToolExecutionRequest(
        capability=ActionType.SCAN_PORTS.value,
        targets=["10.0.0.1"],
        message="Run nmap",
        task_id=123,
        metadata={},
    )

    outcome = asyncio.run(coordinator.run(request))
    assert outcome.tool_id == "information_gathering.network_discovery.nmap"


@pytest.mark.asyncio
async def test_coordinator_uses_runtime_resolver_for_lazy_planner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_clients: list[Any] = []
    constructed_clients: list[Any] = []

    class _PlannerFromResolver(_StubPlanner):
        def __init__(self, config: AgentConfig, llm_client: Any = None) -> None:
            super().__init__()
            constructed_clients.append(llm_client)

    client = object()

    def _resolve_client() -> object:
        resolved_clients.append(client)
        return client

    monkeypatch.setattr(
        "agent.tool_runtime.coordinator.EnhancedActionPlanner",
        _PlannerFromResolver,
    )

    config = AgentConfig(task_id="123", workspace_path="/tmp", model_name="model")
    setattr(config, "llm_client_resolver", _resolve_client)
    executor = _StubExecutor()
    coordinator = ToolExecutionCoordinator(config=config, executor=executor)

    request = ToolExecutionRequest(
        capability=ActionType.SCAN_PORTS.value,
        targets=["10.0.0.3"],
        message="Run nmap",
        task_id=123,
        metadata={"api_key": "sk-should-not-forward"},
        provider="openai",
        model="gpt-5.2",
        credential_ref={"user_id": 7, "provider": "openai"},
        llm_runtime_selection={
            "provider": "openai",
            "model": "gpt-5.2",
            "credential_ref": {"user_id": 7, "provider": "openai"},
            "reasoning_effort": None,
        },
    )

    await coordinator.run(request)

    assert resolved_clients == [client]
    assert constructed_clients == [client]
    assert executor.last_request is not None
    assert "api_key" not in executor.last_request


@pytest.mark.asyncio
async def test_coordinator_converts_planner_validation_error_without_executor_call() -> None:
    config = AgentConfig(task_id="123", workspace_path="/tmp", openai_api_key="key", model_name="model")
    planner = _ValidationErrorPlanner()
    executor = _StubExecutor()
    coordinator = ToolExecutionCoordinator(config=config, planner=planner, executor=executor)

    request = ToolExecutionRequest(
        capability=ActionType.GATHER_INFO.value,
        targets=["127.0.0.1"],
        message="run shell command",
        task_id=123,
        metadata={},
    )

    outcome = await coordinator.run(request)

    assert outcome.tool_id == "shell.exec"
    assert outcome.result.get("status") == "validation_error"
    assert outcome.result.get("success") is False
    assert outcome.result.get("validation_errors")
    assert executor.calls == 0


@pytest.mark.asyncio
async def test_coordinator_reasoning_never_includes_raw_tool_observation() -> None:
    config = AgentConfig(task_id="123", workspace_path="/tmp", openai_api_key="key", model_name="model")
    planner = _StubPlanner()
    executor = _RawObservationExecutor()
    coordinator = ToolExecutionCoordinator(config=config, planner=planner, executor=executor)

    request = ToolExecutionRequest(
        capability=ActionType.GATHER_INFO.value,
        targets=["127.0.0.1"],
        message="run shell command",
        task_id=123,
        metadata={},
    )

    outcome = await coordinator.run(request)
    reasoning_text = "\n".join(outcome.reasoning)

    assert "Traceback" not in reasoning_text
    assert "NotSupportedError" not in reasoning_text
    assert "Observation:" not in reasoning_text


def test_coordinator_build_catalog_hides_artifact_tools_when_not_relevant(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AgentConfig(task_id="123", workspace_path="/tmp", openai_api_key="key", model_name="model")
    coordinator = ToolExecutionCoordinator(config=config, planner=_StubPlanner(), executor=_StubExecutor())

    monkeypatch.setattr(
        "agent.tool_runtime.coordinator.get_catalog_metadata_snapshot",
        lambda: {
            "shell.exec": {"name": "shell.exec", "description": "", "category": "shell"},
            ARTIFACT_SEARCH_TOOL_ID: {"name": ARTIFACT_SEARCH_TOOL_ID, "description": "", "category": "artifact"},
            ARTIFACT_READ_TOOL_ID: {"name": ARTIFACT_READ_TOOL_ID, "description": "", "category": "artifact"},
        },
    )
    entries = coordinator._build_catalog(
        capability="gather_info",
        metadata={},
        task_id=123,
        history=[],
        user_message="run shell command",
    )
    tool_ids = [entry.tool_id for entry in entries]
    assert ARTIFACT_SEARCH_TOOL_ID not in tool_ids
    assert ARTIFACT_READ_TOOL_ID not in tool_ids


def test_coordinator_build_catalog_hides_search_even_when_artifact_signal_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AgentConfig(task_id="123", workspace_path="/tmp", openai_api_key="key", model_name="model")
    coordinator = ToolExecutionCoordinator(config=config, planner=_StubPlanner(), executor=_StubExecutor())

    monkeypatch.setattr(
        "agent.tool_runtime.coordinator.get_catalog_metadata_snapshot",
        lambda: {
            "shell.exec": {"name": "shell.exec", "description": "", "category": "shell"},
            ARTIFACT_SEARCH_TOOL_ID: {"name": ARTIFACT_SEARCH_TOOL_ID, "description": "", "category": "artifact"},
            ARTIFACT_READ_TOOL_ID: {"name": ARTIFACT_READ_TOOL_ID, "description": "", "category": "artifact"},
        },
    )
    entries = coordinator._build_catalog(
        capability="gather_info",
        metadata={},
        task_id=123,
        history=[],
        user_message="check prior outputs",
    )
    tool_ids = [entry.tool_id for entry in entries]
    assert ARTIFACT_SEARCH_TOOL_ID not in tool_ids
    assert ARTIFACT_READ_TOOL_ID not in tool_ids


def test_coordinator_build_catalog_hides_nikto_and_openvas(monkeypatch: pytest.MonkeyPatch) -> None:
    config = AgentConfig(task_id="123", workspace_path="/tmp", openai_api_key="key", model_name="model")
    coordinator = ToolExecutionCoordinator(config=config, planner=_StubPlanner(), executor=_StubExecutor())

    monkeypatch.setattr(
        "agent.tool_runtime.coordinator.get_catalog_metadata_snapshot",
        lambda: {
            "information_gathering.network_discovery.nmap": {
                "name": "Nmap",
                "description": "",
                "category": "information_gathering",
            },
            "web_applications.web_vulnerability_scanners.nikto": {
                "name": "Nikto",
                "description": "",
                "category": "web_applications",
            },
            "vulnerability_analysis.openvas.openvas": {
                "name": "OpenVAS",
                "description": "",
                "category": "vulnerability_analysis",
            },
        },
    )
    entries = coordinator._build_catalog(
        capability="scan_ports",
        metadata={},
        task_id=123,
        history=[],
        user_message="scan host",
    )
    tool_ids = [entry.tool_id for entry in entries]
    assert "information_gathering.network_discovery.nmap" in tool_ids
    assert "web_applications.web_vulnerability_scanners.nikto" not in tool_ids
    assert "vulnerability_analysis.openvas.openvas" not in tool_ids
