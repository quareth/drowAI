"""Regression tests for provider-neutral tool execution runtime context."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import agent.graph.builders  # noqa: F401  # side-effect: resolve graph import cycle
import pytest

from agent.config import AgentConfig
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import FactsState, InteractiveState
from agent.models import ActionType, ExecutionResult, ExecutionStrategy
from agent.graph.adapters.executor_adapter import GraphToolExecutor
from agent.tool_runtime import ToolExecutionCoordinator, ToolExecutionRequest
from agent.tool_runtime.batch.types import ToolBatch, ToolCall
from agent.graph.subgraphs.tool_execution_runtime.request_context import (
    build_request_and_coordinator_config,
)
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.facade_helpers import build_metadata
from backend.services.runtime_provider.contracts import RuntimeOperationStatus, build_runtime_result


SECRET = "sk-test-execution-plane-secret-do-not-persist"


class _StubPlanner:
    async def build_action_plan(self, action: Any, context: Dict[str, Any]) -> Any:
        class _Plan:
            tool_batch = ToolBatch(
                tool_batch_id="tb_stub",
                tool_calls=(
                    ToolCall(
                        tool_call_id="tc_stub",
                        tool_id="information_gathering.network_discovery.nmap",
                        parameters={
                            "target": context["targets"][0],
                            "ports": "80",
                        },
                    ),
                ),
                requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
                selection_rationale="Selected nmap.",
            )
            selected_tools = ["information_gathering.network_discovery.nmap"]
            tool_parameters = {
                "information_gathering.network_discovery.nmap": {
                    "target": context["targets"][0],
                    "ports": "80",
                }
            }
            reasoning = "Selected nmap."
            expected_outcome = "Open ports discovered."
            execution_strategy = ExecutionStrategy.SEQUENTIAL

        return _Plan()


class _StubExecutor:
    def __init__(self) -> None:
        self.last_request: Dict[str, Any] | None = None

    async def execute_tool(self, request: Dict[str, Any]) -> Dict[str, Any]:
        self.last_request = dict(request)
        return {
            "tool": request["tool"],
            "success": True,
            "stdout": "ok",
            "stderr": "",
            "stdout_excerpt": "ok",
            "stderr_excerpt": "",
            "observation": "ok",
            "status": "success",
            "duration": 0.1,
        }


def _empty_context_bundle() -> dict[str, Any]:
    return dict(
        build_conversation_context_bundle(
            conversation_id="conv-provider-tool-test",
            turn_id="turn-provider-tool-test",
            turn_sequence=1,
            messages=[],
        )
    )


def test_request_context_strips_raw_secret_and_carries_provider_metadata() -> None:
    metadata: Dict[str, Any] = {
        "api_key": SECRET,
        "runtime_api_key": SECRET,
        "provider": "openai",
        "model": "gpt-5.2",
        "credential_ref": {"user_id": 7, "provider": "openai"},
        "llm_runtime_selection": {
            "provider": "openai",
            "model": "gpt-5.2",
            "credential_ref": {"user_id": 7, "provider": "openai"},
            "reasoning_effort": None,
        },
        "simple_chat_runtime": {"api_key": SECRET, "model": "gpt-5.2"},
        METADATA_CONTEXT_BUNDLE_KEY: _empty_context_bundle(),
    }
    facts = FactsState(
        task_id=5,
        message="Scan host",
        capability="scan_ports",
        intent_hints={"targets": ["127.0.0.1"]},
        metadata=metadata,
    )

    request, coordinator_config, _runtime_context, _workspace_path = (
        build_request_and_coordinator_config(
            interactive=InteractiveState(facts=facts),
            context=GraphRuntimeContext(
                task_id=5,
                user_id=7,
                tenant_id=3,
                runtime_placement_mode="local",
                workspace_id="task-5",
                actor_type="agent",
                actor_id="langgraph",
                workspace_path="/workspace",
                provider="openai",
                model="gpt-5.2",
                credential_ref={"user_id": 7, "provider": "openai"},
            ),
            metadata=dict(metadata),
        )
    )

    serialized_metadata = json.dumps(request.metadata, sort_keys=True)
    assert SECRET not in serialized_metadata
    assert request.provider == "openai"
    assert request.model == "gpt-5.2"
    assert request.credential_ref == {"user_id": 7, "provider": "openai"}
    assert request.llm_runtime_selection is not None
    assert coordinator_config.tenant_id == 3
    assert coordinator_config.openai_api_key is None


def test_request_context_runner_mode_does_not_require_workspace_path() -> None:
    metadata: Dict[str, Any] = {
        METADATA_CONTEXT_BUNDLE_KEY: _empty_context_bundle(),
    }
    facts = FactsState(
        task_id=5,
        message="Scan host",
        capability="scan_ports",
        intent_hints={"targets": ["127.0.0.1"]},
        metadata=metadata,
    )

    request, coordinator_config, runtime_context, workspace_path = (
        build_request_and_coordinator_config(
            interactive=InteractiveState(facts=facts),
            context=GraphRuntimeContext(
                task_id=5,
                user_id=7,
                tenant_id=3,
                runtime_placement_mode="runner",
                workspace_id="task-5",
                actor_type="agent",
                actor_id="langgraph",
            ),
            metadata=dict(metadata),
        )
    )

    assert runtime_context is not None
    assert request.workspace_path is None
    assert workspace_path is None
    assert coordinator_config.runtime_placement_mode == "runner"


def test_request_context_missing_runtime_identity_fails_closed() -> None:
    metadata: Dict[str, Any] = {
        METADATA_CONTEXT_BUNDLE_KEY: _empty_context_bundle(),
    }
    facts = FactsState(
        task_id=5,
        message="Scan host",
        capability="scan_ports",
        intent_hints={"targets": ["127.0.0.1"]},
        metadata=metadata,
    )

    with pytest.raises(RuntimeError, match="workspace_id"):
        build_request_and_coordinator_config(
            interactive=InteractiveState(facts=facts),
            context=GraphRuntimeContext(
                task_id=5,
                user_id=7,
                tenant_id=3,
                runtime_placement_mode="runner",
                actor_type="agent",
                actor_id="langgraph",
            ),
            metadata=dict(metadata),
        )


def test_request_context_product_context_missing_placement_fails_closed() -> None:
    metadata: Dict[str, Any] = {
        METADATA_CONTEXT_BUNDLE_KEY: _empty_context_bundle(),
    }
    facts = FactsState(
        task_id=5,
        message="Scan host",
        capability="scan_ports",
        intent_hints={"targets": ["127.0.0.1"]},
        metadata=metadata,
    )

    with pytest.raises(RuntimeError, match="runtime_placement_mode"):
        build_request_and_coordinator_config(
            interactive=InteractiveState(facts=facts),
            context=GraphRuntimeContext(
                task_id=5,
                user_id=7,
                tenant_id=3,
                workspace_id="task-5",
                actor_type="agent",
                actor_id="langgraph",
                workspace_path="/workspace",
            ),
            metadata=dict(metadata),
        )


def test_wired_metadata_path_projects_runtime_identity_for_executor_request() -> None:
    chat_inputs = ChatInputs(
        task_id=5,
        user_id=7,
        message="Scan host",
        conversation_id="conv-provider-tool-test",
        history=[],
        provider="openai",
        model="gpt-5.2",
    )
    runtime_config = LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        execution_mode=ExecutionMode.SIMPLE_TOOL,
        metadata={
            "tenant_id": 3,
            "runtime_placement_mode": "runner",
            "workspace_id": "task-5",
            "actor_type": "agent",
            "actor_id": "langgraph",
            "runner_id": "runner-1",
            "execution_site_id": "site-1",
            METADATA_CONTEXT_BUNDLE_KEY: _empty_context_bundle(),
        },
    )
    metadata = build_metadata(chat_inputs, runtime_config)
    assert "tenant_id" not in metadata
    assert "runtime_placement_mode" not in metadata

    facts = FactsState(
        task_id=5,
        message="Scan host",
        capability="scan_ports",
        intent_hints={"targets": ["127.0.0.1"]},
        metadata=metadata,
    )
    request, _coordinator_config, _runtime_context, _workspace_path = (
        build_request_and_coordinator_config(
            interactive=InteractiveState(facts=facts),
            context=None,
            metadata=dict(metadata),
        )
    )

    executor = _StubExecutor()
    coordinator = ToolExecutionCoordinator(
        config=AgentConfig(task_id="5", workspace_path="/tmp", model_name="gpt-5.2"),
        planner=_StubPlanner(),
        executor=executor,
    )
    asyncio.run(coordinator.run(request))

    assert executor.last_request is not None
    assert executor.last_request["tenant_id"] == 3
    assert executor.last_request["runtime_placement_mode"] == "runner"
    assert executor.last_request["workspace_id"] == "task-5"
    assert executor.last_request["actor_type"] == "agent"
    assert executor.last_request["actor_id"] == "langgraph"
    assert executor.last_request["runner_id"] == "runner-1"
    assert executor.last_request["execution_site_id"] == "site-1"


def test_coordinator_graph_request_omits_raw_secret_and_preserves_runtime_ref() -> None:
    executor = _StubExecutor()
    coordinator = ToolExecutionCoordinator(
        config=AgentConfig(task_id="5", workspace_path="/tmp", model_name="gpt-5.2"),
        planner=_StubPlanner(),
        executor=executor,
    )

    request = ToolExecutionRequest(
        capability=ActionType.SCAN_PORTS.value,
        targets=["127.0.0.1"],
        message="Run nmap",
        task_id=5,
        metadata={},
        api_key=SECRET,
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
    assert getattr(request, "api_key", None) is None
    assert SECRET not in repr(request)

    asyncio.run(coordinator.run(request))

    assert executor.last_request is not None
    assert "api_key" not in executor.last_request
    assert SECRET not in json.dumps(executor.last_request, sort_keys=True)
    assert executor.last_request["provider"] == "openai"
    assert executor.last_request["credential_ref"] == {"user_id": 7, "provider": "openai"}


@pytest.mark.asyncio
async def test_runner_container_lane_dispatches_per_call_via_send_tool_command(monkeypatch) -> None:
    calls: list[Any] = []

    class _Provider:
        async def send_tool_command(self, request):
            calls.append(request)
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={
                    "delegate_result": {
                        "success": True,
                        "stdout": "runner-ok",
                        "stderr": "",
                        "exit_code": 0,
                        "status": "success",
                    }
                },
            )

    class _Registry:
        def get_provider(self, *, runtime_placement_mode):
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    provided_executor = MagicMock()
    provided_executor._maybe_request_approval = AsyncMock(return_value=True)
    executor = GraphToolExecutor(executor=provided_executor)
    context = GraphRuntimeContext(
        task_id=5,
        user_id=7,
        tenant_id=3,
        runtime_placement_mode="runner",
        workspace_id="task-5",
        actor_type="agent",
        actor_id="langgraph",
        workspace_path="/tmp/task-5",
    )

    result = await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "echo ok"},
            "task_id": 5,
            "user_id": 7,
            "tenant_id": 3,
            "runtime_placement_mode": "runner",
            "workspace_id": "task-5",
            "actor_type": "agent",
            "actor_id": "langgraph",
            "tool_call_id": "call-1",
            "timeout_plan": {
                "tool_id": "shell.exec",
                "deadline_seconds": 5.0,
                "native_timeout_seconds": 5,
                "source": "test",
                "grace_seconds": 1.0,
                "max_timeout_seconds": 600.0,
                "default_timeout_seconds": 600.0,
            },
        },
        context=context,
    )

    assert result["success"] is True
    assert result["status"] == "success"
    assert len(calls) == 1
    assert calls[0].operation == "send_tool_command"
    assert calls[0].payload["tool"] == "shell.exec"
    assert calls[0].payload["command_id"] == "call-1"
    assert calls[0].payload["wait_for_result"] is True
    assert calls[0].metadata["wait_for_result"] is True
    assert calls[0].payload["timeout_policy"] == {
        "deadline_seconds": 5.0,
        "grace_seconds": 1.0,
    }
    assert provided_executor._execute_single_tool.call_count == 0


@pytest.mark.asyncio
async def test_runner_container_lane_forwards_env_enabled_pty_transport(monkeypatch) -> None:
    calls: list[Any] = []
    monkeypatch.setenv("ENABLE_PTY_EXECUTION", "true")

    class _Provider:
        async def send_tool_command(self, request):
            calls.append(request)
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={
                    "delegate_result": {
                        "success": True,
                        "stdout": "runner-ok",
                        "stderr": "",
                        "exit_code": 0,
                        "status": "success",
                    }
                },
            )

    class _Registry:
        def get_provider(self, *, runtime_placement_mode):
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    provided_executor = MagicMock()
    provided_executor._maybe_request_approval = AsyncMock(return_value=True)
    executor = GraphToolExecutor(executor=provided_executor)
    context = GraphRuntimeContext(
        task_id=5,
        user_id=7,
        tenant_id=3,
        runtime_placement_mode="runner",
        workspace_id="task-5",
        actor_type="agent",
        actor_id="langgraph",
        workspace_path="/tmp/task-5",
    )

    await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "echo ok"},
            "task_id": 5,
            "user_id": 7,
            "tenant_id": 3,
            "runtime_placement_mode": "runner",
            "workspace_id": "task-5",
            "actor_type": "agent",
            "actor_id": "langgraph",
            "tool_call_id": "call-env-pty",
        },
        context=context,
    )

    assert calls[0].payload["transport"] == "pty"
    assert calls[0].payload["command_id"] == "call-env-pty"


@pytest.mark.asyncio
async def test_runner_container_lane_uses_control_plane_workspace_for_command_prep(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[Any] = []

    class _Provider:
        async def send_tool_command(self, request):
            calls.append(request)
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={
                    "delegate_result": {
                        "success": True,
                        "stdout": "runner-ok",
                        "stderr": "",
                        "exit_code": 0,
                        "status": "success",
                    }
                },
            )

    class _Registry:
        def get_provider(self, *, runtime_placement_mode):
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    class _PrepExecutor:
        def __init__(self) -> None:
            self.config = AgentConfig(task_id="5", workspace_path=str(tmp_path))
            self.logger = None

        def _tool_to_shell_command(self, _tool_id: str, parameters: Dict[str, Any]) -> str:
            return str(parameters["command"])

    def _get_executor(
        self,
        workspace_path: str | None,
        task_id: int | None,
        *_args: Any,
        **_kwargs: Any,
    ) -> Any:
        assert workspace_path == str(tmp_path)
        assert task_id == 5
        return _PrepExecutor()

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.runner_command_orchestration._resolve_control_plane_workspace_path",
        lambda task_id, *, workspace_path=None: str(tmp_path),
    )
    monkeypatch.setattr(GraphToolExecutor, "_get_executor", _get_executor)
    monkeypatch.delenv("WORKSPACE", raising=False)

    executor = GraphToolExecutor()
    result = await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "echo ok"},
            "task_id": 5,
            "user_id": 7,
            "tenant_id": 3,
            "runtime_placement_mode": "runner",
            "workspace_id": "task-5",
            "actor_type": "agent",
            "actor_id": "langgraph",
            "tool_call_id": "call-1",
            "timeout_plan": {
                "tool_id": "shell.exec",
                "deadline_seconds": 5.0,
                "grace_seconds": 1.0,
            },
        },
        context=GraphRuntimeContext(
            task_id=5,
            user_id=7,
            tenant_id=3,
            runtime_placement_mode="runner",
            workspace_id="task-5",
            actor_type="agent",
            actor_id="langgraph",
        ),
    )

    assert result["success"] is True
    assert result["status"] == "success"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_runner_container_lane_materializes_enriched_artifacts_in_runtime(
    monkeypatch,
    tmp_path,
) -> None:
    writes: list[Any] = []

    class _Provider:
        async def send_tool_command(self, request):
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={
                    "delegate_result": {
                        "success": True,
                        "stdout": "runner raw output",
                        "stderr": "",
                        "exit_code": 0,
                        "status": "success",
                        "metadata": {"command_text": request.payload["command"]},
                    }
                },
            )

        async def write_runtime_artifact_file(self, request):
            writes.append(request)
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={"path": request.payload["path"]},
            )

    class _Registry:
        def get_provider(self, *, runtime_placement_mode):
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    class _PrepExecutor:
        def __init__(self) -> None:
            self.config = AgentConfig(task_id="5", workspace_path=str(tmp_path))
            self.logger = None

        def _tool_to_shell_command(self, _tool_id: str, parameters: Dict[str, Any]) -> str:
            return str(parameters["command"])

    def _get_executor(
        self,
        workspace_path: str | None,
        task_id: int | None,
        *_args: Any,
        **_kwargs: Any,
    ) -> Any:
        assert workspace_path == str(tmp_path)
        assert task_id == 5
        return _PrepExecutor()

    def _fake_enrich(**kwargs: Any) -> ExecutionResult:
        assert kwargs["host_workspace_path"] == str(tmp_path)
        artifact_path = tmp_path / "artifacts" / "nmap.xml"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("runner raw output", encoding="utf-8")
        enriched = ExecutionResult(True, "runner raw output", "", 0)
        enriched.artifacts = ["artifacts/nmap.xml"]
        enriched.metadata = {"parsed": True}
        return enriched

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.runner_command_orchestration._resolve_control_plane_workspace_path",
        lambda task_id, *, workspace_path=None: str(tmp_path),
    )
    monkeypatch.setattr(GraphToolExecutor, "_get_executor", _get_executor)
    monkeypatch.setattr(
        "agent.graph.subgraphs.tool_execution_runtime.runner_command_result_finalizer.build_command_transport_tool_result",
        _fake_enrich,
    )

    executor = GraphToolExecutor()
    result = await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "echo ok", "transport": "file-comm"},
            "task_id": 5,
            "user_id": 7,
            "tenant_id": 3,
            "runtime_placement_mode": "runner",
            "workspace_id": "task-5",
            "actor_type": "agent",
            "actor_id": "langgraph",
            "tool_call_id": "call-artifact",
            "timeout_plan": {
                "tool_id": "shell.exec",
                "deadline_seconds": 5.0,
                "grace_seconds": 1.0,
            },
        },
        context=GraphRuntimeContext(
            task_id=5,
            user_id=7,
            tenant_id=3,
            runtime_placement_mode="runner",
            workspace_id="task-5",
            actor_type="agent",
            actor_id="langgraph",
        ),
    )

    assert result["success"] is True
    assert "artifacts/nmap.xml" in result["artifacts"]
    assert any(path.startswith("artifacts/") and path.endswith("_tool.txt") for path in result["artifacts"])
    assert result["metadata"]["parsed"] is True
    assert result["metadata"]["artifact_materialization"]["status"] == "succeeded"
    assert result["command_text"] == "bash -c 'echo ok'"
    written_paths = [write.payload["path"] for write in writes]
    assert "artifacts/nmap.xml" in written_paths
    assert any(path.startswith("artifacts/") and path.endswith("_tool.txt") for path in written_paths)
    assert any(path.startswith("index/chunks_") and path.endswith(".jsonl") for path in written_paths)
    index_writes = [write for write in writes if str(write.payload["path"]).startswith("index/")]
    assert index_writes
    assert all(write.payload["mode"] == "append" for write in index_writes)
    assert all(write.operation == "write_runtime_artifact_file" for write in writes)
    assert all(write.payload["content_base64"] for write in writes)
    assert not (tmp_path / "artifacts" / "nmap.xml").exists()


@pytest.mark.asyncio
async def test_runner_container_lane_ack_only_result_is_not_treated_as_success(monkeypatch) -> None:
    class _Provider:
        async def send_tool_command(self, request):
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.ACCEPTED,
                metadata={},
            )

    class _Registry:
        def get_provider(self, *, runtime_placement_mode):
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    provided_executor = MagicMock()
    provided_executor._maybe_request_approval = AsyncMock(return_value=True)
    executor = GraphToolExecutor(executor=provided_executor)
    context = GraphRuntimeContext(
        task_id=5,
        user_id=7,
        tenant_id=3,
        runtime_placement_mode="runner",
        workspace_id="task-5",
        actor_type="agent",
        actor_id="langgraph",
    )

    result = await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "echo ok"},
            "task_id": 5,
            "user_id": 7,
            "tenant_id": 3,
            "runtime_placement_mode": "runner",
            "workspace_id": "task-5",
            "actor_type": "agent",
            "actor_id": "langgraph",
            "tool_call_id": "call-1",
            "timeout_plan": {
                "tool_id": "shell.exec",
                "deadline_seconds": 5.0,
                "native_timeout_seconds": 5,
                "source": "test",
                "grace_seconds": 1.0,
                "max_timeout_seconds": 600.0,
                "default_timeout_seconds": 600.0,
            },
        },
        context=context,
    )

    assert result["success"] is False
    assert result["status"] == "tool_result_missing"
    assert result["metadata"]["error_code"] == "tool_result_missing"
    assert "did not return a terminal result" in result["stderr"]


@pytest.mark.asyncio
async def test_runner_container_lane_preserves_cancelled_delegate_status(monkeypatch) -> None:
    class _Provider:
        async def send_tool_command(self, request):
            return build_runtime_result(
                request,
                accepted=False,
                provider="fake",
                status=RuntimeOperationStatus.FAILED,
                error_code="TOOL_RESULT_CANCELLED",
                error_message="Tool command result waiter was cancelled.",
                metadata={
                    "delegate_result": {
                        "success": False,
                        "stdout": "",
                        "stderr": "Tool command result waiter was cancelled.",
                        "exit_code": 130,
                        "status": "cancelled",
                        "error_code": "TOOL_RESULT_CANCELLED",
                        "error_message": "Tool command result waiter was cancelled.",
                    }
                },
            )

    class _Registry:
        def get_provider(self, *, runtime_placement_mode):
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    provided_executor = MagicMock()
    provided_executor._maybe_request_approval = AsyncMock(return_value=True)
    executor = GraphToolExecutor(executor=provided_executor)

    result = await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "echo ok"},
            "task_id": 5,
            "user_id": 7,
            "tenant_id": 3,
            "runtime_placement_mode": "runner",
            "workspace_id": "task-5",
            "actor_type": "agent",
            "actor_id": "langgraph",
            "tool_call_id": "call-1",
            "timeout_plan": {"tool_id": "shell.exec", "deadline_seconds": 5.0, "grace_seconds": 1.0},
        },
        context=GraphRuntimeContext(
            task_id=5,
            user_id=7,
            tenant_id=3,
            runtime_placement_mode="runner",
            workspace_id="task-5",
            actor_type="agent",
            actor_id="langgraph",
        ),
    )

    assert result["success"] is False
    assert result["status"] == "cancelled"
    assert result["metadata"]["error_code"] == "TOOL_RESULT_CANCELLED"


@pytest.mark.asyncio
async def test_runner_container_lane_routes_via_provider_and_management_tools_fail_closed(
    monkeypatch,
) -> None:
    runner_calls: list[Any] = []

    class _Provider:
        async def send_tool_command(self, request):
            runner_calls.append(request)
            return build_runtime_result(
                request,
                accepted=True,
                provider="fake",
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={
                    "delegate_result": {
                        "success": True,
                        "stdout": "runner-ok",
                        "stderr": "",
                        "exit_code": 0,
                        "status": "success",
                    }
                },
            )

    class _Registry:
        def get_provider(self, *, runtime_placement_mode):
            assert runtime_placement_mode.value == "runner"
            return _Provider()

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    provided_executor = MagicMock()
    provided_executor._maybe_request_approval = AsyncMock(return_value=True)
    provided_executor._execute_single_tool = AsyncMock(
        return_value=ExecutionResult(success=True, stdout="local-unexpected", stderr="", exit_code=0)
    )
    executor = GraphToolExecutor(executor=provided_executor)
    context = GraphRuntimeContext(
        task_id=5,
        user_id=7,
        tenant_id=3,
        runtime_placement_mode="runner",
        workspace_id="task-5",
        actor_type="agent",
        actor_id="langgraph",
        workspace_path="/tmp/task-5",
    )

    shell_response = await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "echo runner"},
            "tool_call_id": "call-1",
            "task_id": 5,
            "user_id": 7,
            "tenant_id": 3,
            "runtime_placement_mode": "runner",
            "workspace_id": "task-5",
            "actor_type": "agent",
            "actor_id": "langgraph",
            "tool_batch_id": "batch-1",
            "timeout_plan": {
                "tool_id": "shell.exec",
                "deadline_seconds": 5.0,
                "grace_seconds": 1.0,
            },
        },
        context=context,
    )
    assert shell_response["success"] is True

    unsupported_responses = []
    for request in [
        {
            "tool": "knowledge.cve_lookup",
            "parameters": {"cve_id": "CVE-2024-0001"},
            "tool_call_id": "call-2",
            "expected_status": "unsupported_management_knowledge_tool_runner_v1",
        },
        {
            "tool": "artifact.search",
            "parameters": {"query": "ioc"},
            "tool_call_id": "call-3",
            "expected_status": "unsupported_management_artifact_tool_runner_v1",
        },
    ]:
        response = await executor.execute_tool(
            {
                "tool": request["tool"],
                "parameters": request["parameters"],
                "tool_call_id": request["tool_call_id"],
                "task_id": 5,
                "user_id": 7,
                "tenant_id": 3,
                "runtime_placement_mode": "runner",
                "workspace_id": "task-5",
                "actor_type": "agent",
                "actor_id": "langgraph",
                "tool_batch_id": "batch-1",
                "timeout_plan": {
                    "tool_id": request["tool"],
                    "deadline_seconds": 5.0,
                    "grace_seconds": 1.0,
                },
            },
            context=context,
        )
        unsupported_responses.append(response)
        assert response["success"] is False
        assert response["status"] == request["expected_status"]

    assert len(runner_calls) == 1
    assert runner_calls[0].payload["tool"] == "shell.exec"
    assert provided_executor._execute_single_tool.call_count == 0
    assert {response["metadata"]["route_policy"]["selected_transport"] for response in unsupported_responses} == {
        "blocked-pre-dispatch"
    }


@pytest.mark.asyncio
async def test_runner_management_lanes_fail_without_local_fallback(monkeypatch) -> None:
    monkeypatch.delenv("WORKSPACE", raising=False)

    provided_executor = MagicMock()
    provided_executor._maybe_request_approval = AsyncMock(return_value=True)
    provided_executor._execute_single_tool = AsyncMock(
        return_value=ExecutionResult(True, "local-unexpected", "", 0)
    )
    executor = GraphToolExecutor(executor=provided_executor)
    context = GraphRuntimeContext(
        task_id=5,
        user_id=7,
        tenant_id=3,
        runtime_placement_mode="runner",
        workspace_id="task-5",
        actor_type="agent",
        actor_id="langgraph",
    )

    requests = [
        (
            "knowledge.cve_lookup",
            {"product": "PostgreSQL", "version": "9.6.0"},
            "unsupported_management_knowledge_tool_runner_v1",
        ),
        ("artifact.search", {"limit": 1}, "unsupported_management_artifact_tool_runner_v1"),
        (
            "artifact.read",
            {"artifact_id": "artifact-1"},
            "unsupported_management_artifact_tool_runner_v1",
        ),
    ]

    for tool_name, params, expected_status in requests:
        response = await executor.execute_tool(
            {
                "tool": tool_name,
                "parameters": params,
                "task_id": 5,
                "user_id": 7,
                "tenant_id": 3,
                "runtime_placement_mode": "runner",
                "workspace_id": "task-5",
                "actor_type": "agent",
                "actor_id": "langgraph",
                "tool_call_id": f"call-{tool_name}",
                "timeout_plan": {
                    "tool_id": tool_name,
                    "deadline_seconds": 5.0,
                    "grace_seconds": 1.0,
                },
            },
            context=context,
        )
        assert response["success"] is False
        assert response["status"] == expected_status
        assert response["metadata"]["route_policy"]["selected_transport"] == "blocked-pre-dispatch"

    assert provided_executor._maybe_request_approval.call_count == 0
    assert provided_executor._execute_single_tool.call_count == 0


@pytest.mark.asyncio
async def test_runner_container_lane_missing_identity_fails_before_provider(monkeypatch) -> None:
    class _Registry:
        def get_provider(self, **_kwargs):
            raise AssertionError("provider registry must not be reached without runtime identity")

    monkeypatch.setattr(
        "agent.graph.adapters.executor_adapter.RuntimeProviderRegistry",
        lambda: _Registry(),
    )
    provided_executor = MagicMock()
    provided_executor._maybe_request_approval = AsyncMock(return_value=True)
    executor = GraphToolExecutor(executor=provided_executor)

    result = await executor.execute_tool(
        {
            "tool": "shell.exec",
            "parameters": {"command": "echo ok"},
            "runtime_placement_mode": "runner",
            "tool_call_id": "call-1",
            "timeout_plan": {"tool_id": "shell.exec", "deadline_seconds": 5.0, "grace_seconds": 1.0},
        },
        context=GraphRuntimeContext(task_id=5, user_id=7, runtime_placement_mode="runner"),
    )

    assert result["success"] is False
    assert result["status"] == "missing_runtime_identity"
