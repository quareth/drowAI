"""Tests for the generic subagent facade handler handoff."""

from __future__ import annotations

import asyncio
import copy
import os
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

import pytest

from agent.graph.graph_names import GRAPH_NAME_SUBAGENT
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.subagents.definition import SubagentDefinition
from agent.subagents.registry import SubagentRegistry as DefinitionSubagentRegistry
from agent.subagents.runtime.model import SUBAGENT_RESULT_METADATA_KEY
from backend.services.agent_runs.contracts import AgentResult
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.result_projection import COMPLETED_AGENT_RESULTS_KEY
from backend.services.agent_runs.subagent_registry import (
    SubagentRegistry,
    SubagentSpec,
)
from backend.services.langgraph_chat.execution.graph_executor import GraphExecutionResult
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.facade import LangGraphChatFacade
from backend.services.langgraph_chat.handlers.subagent_handler import (
    SubagentHandler,
)
from backend.services.langgraph_chat.routing.selectors import ChatBranch, resolve_branch


class _RecordingLauncher:
    def __init__(self, registry: ProcessLocalAgentRunRegistry) -> None:
        self.registry = registry
        self.calls: list[dict[str, Any]] = []

    async def launch(self, **kwargs: Any) -> asyncio.Task[AgentResult]:
        self.calls.append(kwargs)
        assignment = kwargs["assignment"]

        async def _finish() -> AgentResult:
            result = AgentResult(
                agent_run_id=assignment.agent_run_id,
                agent_id=assignment.agent_id,
                agent_kind="recon",
                outcome="completed",
                summary="Scout found HTTP.",
                key_findings=("80/tcp open",),
                tools_used=("information_gathering.network_discovery.nmap",),
            )
            await self.registry.mark_completed(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                result=result,
            )
            return result

        return asyncio.create_task(_finish())


class _FailingLauncher:
    async def launch(self, **kwargs: Any) -> None:
        raise RuntimeError("boom secret-free")


class _CompletingExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def stream_graph(
        self,
        compiled: Any,
        graph_input: Any,
        config: dict[str, Any],
        task_id: int,
        **kwargs: Any,
    ) -> GraphExecutionResult:
        self.calls.append(
            {
                "compiled": compiled,
                "graph_input": graph_input,
                "config": config,
                "task_id": task_id,
                "kwargs": kwargs,
            }
        )
        graph_name = config["configurable"]["graph_name"]
        if graph_name != GRAPH_NAME_SUBAGENT:
            final_state = copy.deepcopy(graph_input)
            final_state["trace"]["final_text"] = "Main agent finalized Scout result."
            return GraphExecutionResult(final_state=final_state)

        agent_run_id = graph_input["facts"]["metadata"]["agent_run_id"]
        return GraphExecutionResult(
            final_state={
                "facts": {
                    "metadata": {
                        SUBAGENT_RESULT_METADATA_KEY: {
                            "agent_run_id": agent_run_id,
                            "agent_id": "pathfinder",
                            "agent_kind": "recon",
                            "outcome": "completed",
                            "summary": "Scout found HTTP.",
                            "tools_used": ["information_gathering.network_discovery.nmap"],
                        }
                    }
                }
            }
        )


class _FakeCheckpointerService:
    def get_checkpointer(self, task_id: int) -> "_FakeCheckpointerContext":
        assert task_id == 42
        return _FakeCheckpointerContext()


class _FakeCheckpointerContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: Any) -> None:
        return None


def _runtime_config() -> LangGraphRuntimeConfig:
    chat_inputs = ChatInputs(
        task_id=42,
        user_id=3,
        message="Scan ports on 10.0.0.10",
        conversation_id="conv-42",
        history=[],
        requested_mode=ExecutionMode.SIMPLE_TOOL,
        provider="openai",
        model="gpt-5.2-mini",
        reasoning_effort="medium",
    )
    return LangGraphRuntimeConfig(
        chat_inputs=chat_inputs,
        execution_mode=ExecutionMode.SIMPLE_TOOL,
        metadata={
            "tenant_id": 7,
            "graph_thread_id": "00000000000040008000000000000042",
            "runtime_placement_mode": "runner",
            "workspace_id": "task-42",
            "actor_type": "agent",
            "actor_id": "langgraph",
            "runner_id": "runner-1",
            "execution_site_id": "site-1",
            "turn_id": "task-42-turn-5",
            "turn_number": 5,
            "turn_sequence": 5,
            "intent_classifier_label": "direct_executor",
            "intent_hints": {
                "classifier_label": "direct_executor",
                "targets": ["10.0.0.10"],
            },
            "subagent_routing": {
                "should_delegate": True,
                "reason": "scout_owned",
                "agent_id": "pathfinder",
                "agent_kind": "recon",
                "dispatch_branch": "subagent",
                "capabilities": ["port_scanning"],
                "targets": ["10.0.0.10"],
                "objective": "Scan ports on 10.0.0.10.",
            },
            "feature_flags": {"simple_tool_enabled": True},
            METADATA_CONTEXT_BUNDLE_KEY: build_conversation_context_bundle(
                conversation_id="conv-42",
                turn_id="task-42-turn-5",
                turn_sequence=5,
                messages=[],
                current_message="Scan ports on 10.0.0.10",
            ),
        },
    )


def _second_agent_definition() -> SubagentDefinition:
    return SubagentDefinition(
        schema_version=1,
        id="cartographer",
        display_name="Cartographer",
        kind="recon",
        description="Map approved assets and summarize reachable surfaces.",
        ownership_boundary="Own approved asset inventory only.",
        supported_task_categories=("asset_inventory",),
        excluded_task_categories=(),
        tool_ids=("information_gathering.network_discovery.fping",),
        enabled=True,
        max_active_runs_per_task=1,
        max_iterations=1,
        max_tool_calls_per_iteration=1,
        requires_resolved_target=True,
        icon="cartographer",
        instructions="Map only the assigned approved assets.",
        tool_builder_role_prompt=None,
        tool_builder_boundary_rules=(),
    )


def _second_agent_runtime_config() -> LangGraphRuntimeConfig:
    config = _runtime_config()
    routing = dict(config.metadata["subagent_routing"])
    routing.update(
        {
            "reason": "cartographer_owned",
            "agent_id": "cartographer",
            "capabilities": ["asset_inventory", "port_scanning"],
            "objective": "Inventory approved assets.",
        }
    )
    config.metadata["subagent_routing"] = routing
    return config


@pytest.mark.asyncio
async def test_subagent_handler_uses_registered_agent_identity_for_launch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = _second_agent_definition()
    backend_registry = SubagentRegistry([SubagentSpec.from_definition(definition)])
    monkeypatch.setattr(
        "agent.subagents.registry.get_subagent_registry",
        lambda: DefinitionSubagentRegistry([definition]),
    )
    registry = ProcessLocalAgentRunRegistry()
    events: list[dict[str, Any]] = []

    async def _publish(_task_id: int, event: dict[str, Any]) -> None:
        events.append(event)

    handler = SubagentHandler(
        object(),
        object(),
        object(),
        registry=registry,
        launcher=_FailingLauncher(),
        lifecycle_publisher=_publish,
        subagent_registry=backend_registry,
    )

    result = await handler.handle(_second_agent_runtime_config())

    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].agent_id == "cartographer"
    assert entries[0].agent_run_id.startswith("agent-run-")
    assert entries[0].safe_error == "Cartographer launch failed"
    assert result.final_text == "Cartographer could not complete the subagent run."
    assert result.metadata["agent_id"] == "cartographer"
    assert result.metadata["agent_display_name"] == "Cartographer"
    assert [event["agent_run"]["agent_id"] for event in events] == [
        "cartographer",
        "cartographer",
        "cartographer",
    ]
    assert events[-1]["agent_run"]["safe_error"] == "Cartographer launch failed"


@pytest.mark.asyncio
async def test_subagent_handler_waits_for_pathfinder_and_runs_parent_finalizer() -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _RecordingLauncher(registry)
    executor = _CompletingExecutor()
    events: list[tuple[int, dict[str, Any]]] = []

    async def _publish(task_id: int, event: dict[str, Any]) -> None:
        events.append((task_id, event))

    handler = SubagentHandler(
        object(),
        executor,
        object(),
        registry=registry,
        launcher=launcher,
        lifecycle_publisher=_publish,
    )

    runtime_config = _runtime_config()
    result = await handler.handle(runtime_config)

    assert result.final_text == "Main agent finalized Scout result."
    assert result.metadata["branch"] == "subagent"
    assert result.metadata["handoff_agent_kind"] == "recon"
    assert result.metadata["status"] == "completed"
    assert len(launcher.calls) == 1
    assignment = launcher.calls[0]["assignment"]
    assert assignment.agent_kind == "recon"
    assert assignment.agent_id == "pathfinder"
    assert assignment.objective == "Scan ports on 10.0.0.10."
    assert assignment.targets == ("10.0.0.10",)
    assert assignment.suggested_capabilities == ("port_scanning",)
    assert assignment.runtime_identity.tenant_id == 7
    child_config = launcher.calls[0]["runtime_config"]
    assert child_config["configurable"]["graph_name"] == GRAPH_NAME_SUBAGENT
    assert child_config["configurable"]["agent_run_id"] == assignment.agent_run_id
    assert child_config["configurable"]["agent_icon_key"] == "pathfinder"
    assert child_config["configurable"]["thread_id"].startswith("graph-")
    assert (
        child_config["configurable"]["runtime_projection"]["runtime_placement_mode"]
        == "runner"
    )
    assert "runtime_services" not in child_config["configurable"]
    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].status == "completed"
    assert entries[0].result_consumed is True
    assert [event[1]["agent_run"]["status"] for event in events] == [
        "queued",
        "running",
    ]
    assert events[0][1]["agent_run"]["assignment"]["agent_run_id"] == (
        assignment.agent_run_id
    )
    assert "assignment" not in events[1][1]["agent_run"] or (
        events[1][1]["agent_run"]["assignment"] is None
    )
    completed_results = runtime_config.metadata[COMPLETED_AGENT_RESULTS_KEY]
    assert completed_results[0]["agent_id"] == "pathfinder"
    assert completed_results[0]["agent_run_id"] == assignment.agent_run_id


@pytest.mark.asyncio
async def test_subagent_handler_attaches_live_runtime_services_only_at_launch() -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _RecordingLauncher(registry)
    runtime_config = _runtime_config()
    runtime_services = object()
    runtime_config.runtime_services = runtime_services

    async def _publish(_task_id: int, _event: dict[str, Any]) -> None:
        return None

    handler = SubagentHandler(
        object(),
        _CompletingExecutor(),
        object(),
        registry=registry,
        launcher=launcher,
        lifecycle_publisher=_publish,
    )

    await handler.handle(runtime_config)

    child_config = launcher.calls[0]["runtime_config"]
    assert child_config["configurable"]["runtime_services"] is runtime_services


@pytest.mark.asyncio
async def test_subagent_handler_emits_failed_lifecycle_when_launch_fails() -> None:
    registry = ProcessLocalAgentRunRegistry()
    events: list[dict[str, Any]] = []

    async def _publish(task_id: int, event: dict[str, Any]) -> None:
        events.append(event)

    handler = SubagentHandler(
        object(),
        object(),
        object(),
        registry=registry,
        launcher=_FailingLauncher(),
        lifecycle_publisher=_publish,
    )

    result = await handler.handle(_runtime_config())

    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].status == "failed"
    assert entries[0].safe_error == "Pathfinder launch failed"
    assert result.metadata["status"] == "failed"
    assert [event["agent_run"]["status"] for event in events] == [
        "queued",
        "running",
        "failed",
    ]
    assert events[-1]["agent_run"]["safe_error"] == "Pathfinder launch failed"


def test_facade_registers_subagent_handler() -> None:
    registry = ProcessLocalAgentRunRegistry()
    facade = LangGraphChatFacade(
        agent_run_registry=registry,
        agent_run_launcher=_RecordingLauncher(registry),
        agent_run_lifecycle_publisher=lambda _task_id, _event: None,
    )

    assert isinstance(facade._handlers[ChatBranch.SUBAGENT], SubagentHandler)


@pytest.mark.asyncio
async def test_subagent_handler_default_launcher_runs_real_worker_to_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    executor = _CompletingExecutor()
    events: list[dict[str, Any]] = []

    async def _publish(task_id: int, event: dict[str, Any]) -> None:
        events.append(event)

    monkeypatch.setattr(
        "backend.services.agent_runs.worker.build_subagent_graph",
        lambda _definition, *, checkpointer: {"compiled_with": checkpointer},
    )
    handler = SubagentHandler(
        _FakeCheckpointerService(),
        executor,
        object(),
        registry=registry,
        lifecycle_publisher=_publish,
    )

    result = await handler.handle(_runtime_config())

    assert result.metadata["status"] == "completed"

    assert len(executor.calls) == 2
    call = executor.calls[0]
    assert call["config"]["configurable"]["graph_name"] == GRAPH_NAME_SUBAGENT
    assert call["config"]["configurable"]["graph_runtime_context"]["tenant_id"] == 7
    assert "credential_ref" not in call["config"]["configurable"]["graph_runtime_context"]
    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].status == "completed"
    assert entries[0].result is not None
    assert entries[0].result.summary == "Scout found HTTP."
    assert entries[0].result_consumed is True
    assert events[-1]["agent_run"]["status"] == "completed"


@pytest.mark.asyncio
async def test_facade_active_registry_check_prevents_recon_branch() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _build_assignment_for_active_run(_runtime_config())
    await registry.register(assignment, graph_thread_id="graph-child-active")
    await registry.mark_running(
        tenant_id=assignment.tenant_id,
        task_id=assignment.task_id,
        agent_run_id=assignment.agent_run_id,
    )

    config = _runtime_config()
    facade = LangGraphChatFacade(
        agent_run_registry=registry,
        agent_run_launcher=_RecordingLauncher(registry),
    )
    assert (await facade._active_subagent_run_counts(config)) == {"pathfinder": 1}
    assert (
        resolve_branch(
            config,
            deep_reasoning_enabled=True,
            simple_tool_enabled=True,
            active_subagent_run_counts={"pathfinder": 1},
        )
        is ChatBranch.SIMPLE_TOOL
    )


def _build_assignment_for_active_run(runtime_config: LangGraphRuntimeConfig) -> Any:
    from backend.services.langgraph_chat.handlers.subagent_handler import (
        _build_assignment,
    )

    return _build_assignment(runtime_config, parent_turn_id="task-42-turn-5")
