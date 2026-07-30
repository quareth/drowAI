"""Tests for the generic subagent facade handler handoff."""

from __future__ import annotations

import asyncio
import copy
import os
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

import pytest

from agent.graph.graph_names import GRAPH_NAME_SUBAGENT
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.subagents.definition import SubagentDefinition
from agent.subagents.registry import (
    SubagentRegistry as DefinitionSubagentRegistry,
    get_subagent_registry as get_definition_subagent_registry,
)
from agent.subagents.runtime.model import SUBAGENT_RESULT_METADATA_KEY
from backend.services.agent_runs.contracts import AgentResult
from backend.services.agent_runs.completion import (
    AgentRunCompletion,
    build_agent_run_completion,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.result_projection import (
    ACTIVE_AGENT_RUNS_KEY,
    COMPLETED_AGENT_RESULTS_KEY,
    AgentRunResultProjector,
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
from backend.services.agent_runs.launcher import (
    SubagentRunCancelled,
    SubagentRunFailed,
    SubagentRunPaused,
)
from backend.services.langgraph_chat.routing.selectors import ChatBranch, resolve_branch


class _RecordingLauncher:
    def __init__(self, registry: ProcessLocalAgentRunRegistry) -> None:
        self.registry = registry
        self.calls: list[dict[str, Any]] = []

    async def launch(self, **kwargs: Any) -> asyncio.Task[AgentRunCompletion]:
        self.calls.append(kwargs)
        assignment = kwargs["assignment"]
        graph_thread_id = kwargs["graph_thread_id"]

        async def _finish() -> AgentRunCompletion:
            result = AgentResult(
                agent_run_id=assignment.agent_run_id,
                agent_id=assignment.agent_id,
                agent_kind="recon",
                outcome="completed",
                summary="Pathfinder found HTTP.",
                key_findings=("80/tcp open",),
                tools_used=("information_gathering.network_discovery.nmap",),
            )
            await self.registry.mark_completed(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                result=result,
            )
            return build_agent_run_completion(
                result=result,
                assignment=assignment,
                graph_thread_id=graph_thread_id,
                final_state=_subagent_final_state(
                    agent_run_id=assignment.agent_run_id,
                ),
            )

        return asyncio.create_task(_finish())


class _ControlledLauncher:
    def __init__(self, registry: ProcessLocalAgentRunRegistry) -> None:
        self.registry = registry
        self.calls: list[dict[str, Any]] = []
        self.releases: list[asyncio.Event] = []

    async def launch(self, **kwargs: Any) -> asyncio.Task[AgentRunCompletion]:
        self.calls.append(kwargs)
        assignment = kwargs["assignment"]
        graph_thread_id = kwargs["graph_thread_id"]
        release = asyncio.Event()
        self.releases.append(release)

        async def _finish() -> AgentRunCompletion:
            await release.wait()
            result = AgentResult(
                agent_run_id=assignment.agent_run_id,
                agent_id=assignment.agent_id,
                agent_kind=assignment.agent_kind,
                outcome="completed",
                summary=f"{assignment.agent_id} completed.",
                key_findings=(f"{assignment.agent_id} finding",),
                tools_used=("information_gathering.network_discovery.nmap",),
            )
            await self.registry.mark_completed(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                result=result,
            )
            return build_agent_run_completion(
                result=result,
                assignment=assignment,
                graph_thread_id=graph_thread_id,
                final_state=_subagent_final_state(
                    agent_run_id=assignment.agent_run_id,
                    agent_id=assignment.agent_id,
                ),
            )

        return asyncio.create_task(_finish())


class _FailingLauncher:
    async def launch(self, **kwargs: Any) -> None:
        raise RuntimeError("boom secret-free")


class _FailingSecondLaunchAfterCompletionLauncher:
    def __init__(self, registry: ProcessLocalAgentRunRegistry) -> None:
        self.registry = registry
        self.calls: list[dict[str, Any]] = []

    async def launch(self, **kwargs: Any) -> asyncio.Task[AgentRunCompletion]:
        self.calls.append(kwargs)
        assignment = kwargs["assignment"]
        graph_thread_id = kwargs["graph_thread_id"]
        if len(self.calls) == 2:
            raise RuntimeError("second launch failed")

        async def _finish() -> AgentRunCompletion:
            result = AgentResult(
                agent_run_id=assignment.agent_run_id,
                agent_id=assignment.agent_id,
                agent_kind=assignment.agent_kind,
                outcome="completed",
                summary=f"{assignment.agent_id} completed before batch failure.",
                key_findings=(f"{assignment.agent_id} finding",),
                tools_used=("information_gathering.network_discovery.nmap",),
            )
            await self.registry.mark_completed(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                result=result,
            )
            return build_agent_run_completion(
                result=result,
                assignment=assignment,
                graph_thread_id=graph_thread_id,
                final_state=_subagent_final_state(
                    agent_run_id=assignment.agent_run_id,
                    agent_id=assignment.agent_id,
                ),
            )

        task = asyncio.create_task(_finish())
        await task
        return task


class _FailingAfterUsageLauncher:
    def __init__(self, registry: ProcessLocalAgentRunRegistry) -> None:
        self.registry = registry

    async def launch(self, **kwargs: Any) -> asyncio.Task[AgentRunCompletion]:
        assignment = kwargs["assignment"]

        async def _fail() -> AgentRunCompletion:
            await self.registry.mark_failed(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                safe_error="Subagent worker failed",
            )
            raise SubagentRunFailed(
                "Subagent graph completed without a valid terminal result",
                GraphExecutionResult(
                    final_state={
                        "trace": {
                            "usage_records": [
                                {
                                    "source": "subagent_runtime_model",
                                    "prompt_tokens": 10,
                                    "completion_tokens": 5,
                                    "total_tokens": 15,
                                    "provider": "openai",
                                    "model": "gpt-5.2-mini",
                                    "api_surface": "responses",
                                    "request_mode": "non_streaming",
                                    "cache_reporting": "reported",
                                }
                            ]
                        }
                    }
                ),
            )

        return asyncio.create_task(_fail())


class _CancellingLauncher:
    def __init__(self, registry: ProcessLocalAgentRunRegistry) -> None:
        self.registry = registry

    async def launch(self, **kwargs: Any) -> asyncio.Task[AgentRunCompletion]:
        assignment = kwargs["assignment"]

        async def _cancel() -> AgentRunCompletion:
            await self.registry.mark_cancelled(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
            )
            raise SubagentRunCancelled(
                execution_result=GraphExecutionResult(
                    final_state=_subagent_final_state(agent_run_id="agent-run-cancelled")
                )
            )

        return asyncio.create_task(_cancel())


class _PausingLauncher:
    def __init__(self, registry: ProcessLocalAgentRunRegistry) -> None:
        self.registry = registry
        self.paused = asyncio.Event()
        self._assignment: Any = None
        self._graph_thread_id: str | None = None

    async def launch(self, **kwargs: Any) -> asyncio.Task[AgentRunCompletion]:
        assignment = kwargs["assignment"]
        self._assignment = assignment
        self._graph_thread_id = kwargs["graph_thread_id"]

        async def _pause() -> AgentRunCompletion:
            await self.registry.mark_waiting_for_approval(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                accounted_usage_record_count=1,
            )
            self.paused.set()
            raise SubagentRunPaused(
                execution_result=GraphExecutionResult(
                    final_state=_subagent_final_state(agent_run_id="agent-run-paused")
                )
            )

        return asyncio.create_task(_pause())

    async def complete_after_approval(self) -> None:
        assignment = self._assignment
        assert assignment is not None
        assert self._graph_thread_id is not None
        result = AgentResult(
            agent_run_id=assignment.agent_run_id,
            agent_id=assignment.agent_id,
            agent_kind=assignment.agent_kind,
            outcome="completed",
            summary="Pathfinder completed after approval.",
            key_findings=("80/tcp closed",),
            tools_used=("information_gathering.network_discovery.nmap",),
        )
        await self.registry.mark_completed(
            tenant_id=assignment.tenant_id,
            task_id=assignment.task_id,
            agent_run_id=assignment.agent_run_id,
            result=result,
        )


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
            final_state["trace"]["final_text"] = "Main agent finalized Pathfinder result."
            final_state["trace"]["usage_records"] = [
                {
                    "source": "finalize_tool_results",
                    "prompt_tokens": 21,
                    "completion_tokens": 8,
                    "total_tokens": 29,
                    "provider": "openai",
                    "model": "gpt-5.2-mini",
                    "api_surface": "responses",
                    "request_mode": "non_streaming",
                    "cache_reporting": "reported",
                }
            ]
            return GraphExecutionResult(final_state=final_state)

        agent_run_id = graph_input["facts"]["metadata"]["agent_run_id"]
        return GraphExecutionResult(
            final_state=_subagent_final_state(agent_run_id=agent_run_id)
        )


class _BlockingFirstParentExecutor(_CompletingExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.first_parent_started = asyncio.Event()
        self.release_first_parent = asyncio.Event()
        self.parent_calls: list[dict[str, Any]] = []

    async def stream_graph(
        self,
        compiled: Any,
        graph_input: Any,
        config: dict[str, Any],
        task_id: int,
        **kwargs: Any,
    ) -> GraphExecutionResult:
        graph_name = config["configurable"]["graph_name"]
        if graph_name != GRAPH_NAME_SUBAGENT:
            call = {
                "compiled": compiled,
                "graph_input": graph_input,
                "config": config,
                "task_id": task_id,
                "kwargs": kwargs,
            }
            self.calls.append(call)
            self.parent_calls.append(call)
            if len(self.parent_calls) == 1:
                self.first_parent_started.set()
                await self.release_first_parent.wait()
            final_state = copy.deepcopy(graph_input)
            if len(self.parent_calls) == 1:
                final_state["facts"]["metadata"]["runtime_budgets"][
                    "remaining_tool_calls"
                ] = 9
            final_state["trace"]["final_text"] = "Main agent finalized Pathfinder result."
            return GraphExecutionResult(final_state=final_state)
        return await super().stream_graph(
            compiled,
            graph_input,
            config,
            task_id,
            **kwargs,
        )


class _IrrelevantFirstParentExecutor(_CompletingExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.parent_calls: list[dict[str, Any]] = []

    async def stream_graph(
        self,
        compiled: Any,
        graph_input: Any,
        config: dict[str, Any],
        task_id: int,
        **kwargs: Any,
    ) -> GraphExecutionResult:
        graph_name = config["configurable"]["graph_name"]
        if graph_name == GRAPH_NAME_SUBAGENT:
            return await super().stream_graph(
                compiled,
                graph_input,
                config,
                task_id,
                **kwargs,
            )

        call = {
            "compiled": compiled,
            "graph_input": graph_input,
            "config": config,
            "task_id": task_id,
            "kwargs": kwargs,
        }
        self.calls.append(call)
        self.parent_calls.append(call)
        metadata = graph_input["facts"]["metadata"]
        active_runs = metadata.get(ACTIVE_AGENT_RUNS_KEY)
        if not isinstance(active_runs, list):
            bundle = metadata.get(METADATA_CONTEXT_BUNDLE_KEY)
            active_runs = (
                bundle.get(ACTIVE_AGENT_RUNS_KEY, [])
                if isinstance(bundle, dict)
                else []
            )
        irrelevant_run_ids = [
            run["agent_run_id"]
            for run in active_runs
            if isinstance(run, dict) and isinstance(run.get("agent_run_id"), str)
        ]
        final_state = copy.deepcopy(graph_input)
        final_state["facts"]["metadata"]["router_outcome"] = {
            "action": "finalize",
            "candidate_action": "finalize",
            "candidate_source": "ptr",
            "resolution_source": "candidate",
            "reason": "candidate_decision_accepted",
            "par_irrelevant_active_agent_run_ids": irrelevant_run_ids,
        }
        final_state["trace"]["final_text"] = "Main agent finalized without Cartographer."
        return GraphExecutionResult(final_state=final_state)


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
            "reason": "pathfinder_owned",
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


def _subagent_final_state(
    *,
    agent_run_id: str,
    agent_id: str = "pathfinder",
) -> dict[str, Any]:
    return {
        "facts": {
            "metadata": {
                SUBAGENT_RESULT_METADATA_KEY: {
                    "agent_run_id": agent_run_id,
                    "agent_id": agent_id,
                    "agent_kind": "recon",
                    "outcome": "completed",
                    "summary": "Pathfinder found HTTP.",
                    "tools_used": ["information_gathering.network_discovery.nmap"],
                }
            }
        },
        "trace": {
            "usage_records": [
                {
                    "source": "subagent_runtime_model",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "provider": "openai",
                    "model": "gpt-5.2-mini",
                    "api_surface": "responses",
                    "request_mode": "non_streaming",
                    "cache_reporting": "reported",
                }
            ]
        },
    }


async def _wait_for_call_count(
    launcher: Any,
    expected: int,
    *,
    timeout: float = 0.5,
) -> None:
    async def _poll() -> None:
        while len(launcher.calls) < expected:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def _wait_for_parent_call_count(
    executor: Any,
    expected: int,
    *,
    timeout: float = 0.5,
) -> None:
    async def _poll() -> None:
        while len(executor.parent_calls) < expected:
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


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
        runtime_role_prompt=None,
        runtime_boundary_rules=(),
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


def _ordered_handoff_runtime_config(
    *handoffs: dict[str, Any],
) -> LangGraphRuntimeConfig:
    config = _runtime_config()
    config.metadata["subagent_routing"] = {
        "should_delegate": True,
        "reason": "ordered_handoff_plan",
        "agent_id": handoffs[0]["agent_id"],
        "agent_kind": "recon",
        "dispatch_branch": "subagent",
        "capabilities": ["port_scanning"],
        "targets": ["10.0.0.10"],
        "objective": handoffs[0]["objective"],
        "handoffs": list(handoffs),
    }
    return config


@pytest.mark.asyncio
async def test_subagent_handler_uses_registered_agent_identity_for_launch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = _second_agent_definition()
    definition_registry = DefinitionSubagentRegistry([definition])
    monkeypatch.setattr(
        "agent.subagents.registry.get_subagent_registry",
        lambda: definition_registry,
    )
    registry = ProcessLocalAgentRunRegistry()
    events: list[dict[str, Any]] = []

    async def _publish(_task_id: int, event: dict[str, Any]) -> None:
        events.append(event)

    handler = SubagentHandler(
        object(),
        _CompletingExecutor(),
        object(),
        registry=registry,
        launcher=_FailingLauncher(),
        lifecycle_publisher=_publish,
        subagent_registry=definition_registry,
    )

    result = await handler.handle(_second_agent_runtime_config())

    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].agent_id == "cartographer"
    assert entries[0].agent_run_id.startswith("agent-run-")
    assert entries[0].safe_error == "Cartographer launch failed"
    assert result.final_text == "Main agent finalized Pathfinder result."
    assert result.metadata["handoff_agent_id"] == "cartographer"
    assert entries[0].result_consumed is True
    lifecycle_events = [event for event in events if "agent_run" in event]
    assert [event["agent_run"]["agent_id"] for event in lifecycle_events] == [
        "cartographer",
        "cartographer",
        "cartographer",
    ]
    assert lifecycle_events[-1]["agent_run"]["safe_error"] == (
        "Cartographer launch failed"
    )


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

    assert result.final_text == "Main agent finalized Pathfinder result."
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
    lifecycle_events = [event for _task_id, event in events if "agent_run" in event]
    parent_progress_events = [
        event
        for _task_id, event in events
        if event.get("metadata", {}).get("progress_kind") == "parent_handoff"
    ]
    assert [event["agent_run"]["status"] for event in lifecycle_events] == [
        "queued",
        "running",
    ]
    assert lifecycle_events[0]["agent_run"]["assignment"]["agent_run_id"] == (
        assignment.agent_run_id
    )
    assert "assignment" not in lifecycle_events[1]["agent_run"] or (
        lifecycle_events[1]["agent_run"]["assignment"] is None
    )
    assert [event["type"] for event in parent_progress_events] == [
        "reasoning_start",
        "reasoning_delta",
        "reasoning_section_end",
    ]
    assert parent_progress_events[1]["metadata"]["producer_type"] == "main_agent"
    assert "agent_run_id" not in parent_progress_events[1]["metadata"]
    completed_results = runtime_config.metadata[COMPLETED_AGENT_RESULTS_KEY]
    assert completed_results[0]["agent_id"] == "pathfinder"
    assert completed_results[0]["agent_run_id"] == assignment.agent_run_id
    parent_graph_input = executor.calls[-1]["graph_input"]
    assert parent_graph_input["facts"]["metadata"]["runtime_budgets"] == {
        "time_budget_ms": 300_000,
        "remaining_iterations": 15,
        "remaining_tool_calls": 10,
    }
    assert runtime_config.metadata["runtime_budgets"] == {
        "time_budget_ms": 300_000,
        "remaining_iterations": 15,
        "remaining_tool_calls": 10,
    }
    assert result.total_tokens == 44
    assert result.usage is not None
    assert len(result.usage) == 2
    child_usage, parent_usage = result.usage
    assert child_usage.usage.total_tokens == 15
    assert child_usage.metadata.execution_branch == "subagent_child"
    assert child_usage.metadata.role == "subagent"
    assert child_usage.metadata.node_name == "subagent_runtime_model"
    assert child_usage.metadata.agent_id == "pathfinder"
    assert child_usage.metadata.agent_kind == "recon"
    assert child_usage.metadata.agent_run_id == assignment.agent_run_id
    assert child_usage.metadata.graph_thread_id == (
        launcher.calls[0]["graph_thread_id"]
    )
    assert child_usage.metadata.parent_turn_id == assignment.parent_turn_id
    assert child_usage.metadata.parent_run_id == (
        assignment.relevant_context["parent_run_id"]
    )
    assert parent_usage.usage.total_tokens == 29
    assert parent_usage.metadata.execution_branch == "subagent_parent_finalizer"
    assert parent_usage.metadata.role == "finalizer"
    assert parent_usage.metadata.node_name == "finalize_tool_results"


@pytest.mark.asyncio
async def test_subagent_handler_fails_closed_before_launching_invalid_plan() -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _RecordingLauncher(registry)

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
    runtime_config = _ordered_handoff_runtime_config(
        {
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "capabilities": ["port_scanning"],
            "targets": ["10.0.0.10"],
            "objective": "Scan ports on 10.0.0.10.",
        },
        {
            "agent_id": "exploit",
            "agent_kind": "recon",
            "capabilities": ["port_scanning"],
            "targets": ["10.0.0.10"],
            "objective": "Exploit 10.0.0.10.",
        },
    )

    with pytest.raises(RuntimeError, match="subagent is not registered"):
        await handler.handle(runtime_config)

    assert launcher.calls == []
    assert await registry.list_task_runs(tenant_id=7, task_id=42) == []


@pytest.mark.asyncio
async def test_subagent_handler_launches_independent_handoffs_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ControlledLauncher(registry)
    executor = _CompletingExecutor()
    second_definition = _second_agent_definition()
    definition_registry = DefinitionSubagentRegistry(
        [
            get_definition_subagent_registry().require("pathfinder"),
            second_definition,
        ]
    )
    monkeypatch.setattr(
        "agent.subagents.registry.get_subagent_registry",
        lambda: definition_registry,
    )

    async def _publish(_task_id: int, _event: dict[str, Any]) -> None:
        return None

    handler = SubagentHandler(
        object(),
        executor,
        object(),
        registry=registry,
        launcher=launcher,
        lifecycle_publisher=_publish,
        subagent_registry=definition_registry,
    )
    runtime_config = _ordered_handoff_runtime_config(
        {
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "capabilities": ["port_scanning"],
            "targets": ["10.0.0.10"],
            "objective": "Scan ports on 10.0.0.10.",
        },
        {
            "agent_id": "cartographer",
            "agent_kind": "recon",
            "capabilities": ["asset_inventory"],
            "targets": ["10.0.0.10"],
            "objective": "Inventory approved assets.",
        },
    )

    result_task = asyncio.create_task(handler.handle(runtime_config))
    await _wait_for_call_count(launcher, 2)
    assert executor.calls == []

    for release in launcher.releases:
        release.set()
    result = await result_task

    assert result.metadata["status"] == "completed"
    assert result.metadata["handoff_agent_ids"] == ["pathfinder", "cartographer"]
    assert len(executor.calls) == 1
    assignments = [call["assignment"] for call in launcher.calls]
    assert [assignment.agent_id for assignment in assignments] == [
        "pathfinder",
        "cartographer",
    ]
    assert len({assignment.agent_run_id for assignment in assignments}) == 2
    assert len({call["graph_thread_id"] for call in launcher.calls}) == 2
    completed_results = runtime_config.metadata[COMPLETED_AGENT_RESULTS_KEY]
    assert [result["agent_id"] for result in completed_results] == [
        "pathfinder",
        "cartographer",
    ]


@pytest.mark.asyncio
async def test_subagent_handler_processes_first_ready_handoff_with_active_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ControlledLauncher(registry)
    executor = _BlockingFirstParentExecutor()
    second_definition = _second_agent_definition()
    definition_registry = DefinitionSubagentRegistry(
        [
            get_definition_subagent_registry().require("pathfinder"),
            second_definition,
        ]
    )
    monkeypatch.setattr(
        "agent.subagents.registry.get_subagent_registry",
        lambda: definition_registry,
    )

    async def _publish(_task_id: int, _event: dict[str, Any]) -> None:
        return None

    handler = SubagentHandler(
        object(),
        executor,
        object(),
        registry=registry,
        launcher=launcher,
        lifecycle_publisher=_publish,
        subagent_registry=definition_registry,
    )
    runtime_config = _ordered_handoff_runtime_config(
        {
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "capabilities": ["port_scanning"],
            "targets": ["10.0.0.10"],
            "objective": "Scan ports on 10.0.0.10.",
        },
        {
            "agent_id": "cartographer",
            "agent_kind": "recon",
            "capabilities": ["asset_inventory"],
            "targets": ["10.0.0.10"],
            "objective": "Inventory approved assets.",
        },
    )

    result_task = asyncio.create_task(handler.handle(runtime_config))
    await _wait_for_call_count(launcher, 2)

    launcher.releases[0].set()
    await asyncio.wait_for(executor.first_parent_started.wait(), timeout=0.5)

    assert len(executor.parent_calls) == 1
    completed_results = runtime_config.metadata[COMPLETED_AGENT_RESULTS_KEY]
    active_runs = runtime_config.metadata[ACTIVE_AGENT_RUNS_KEY]
    assert [result["agent_id"] for result in completed_results] == ["pathfinder"]
    assert [run["agent_id"] for run in active_runs] == ["cartographer"]
    assert active_runs[0]["status"] == "running"
    assert not launcher.releases[1].is_set()

    launcher.releases[1].set()
    executor.release_first_parent.set()
    result = await result_task

    assert len(executor.parent_calls) == 2
    first_graph_input = executor.parent_calls[0]["graph_input"]
    assert first_graph_input["facts"]["metadata"]["runtime_budgets"] == {
        "time_budget_ms": 300_000,
        "remaining_iterations": 15,
        "remaining_tool_calls": 10,
    }
    second_graph_input = executor.parent_calls[1]["graph_input"]
    second_metadata = second_graph_input["facts"]["metadata"]
    assert second_metadata["runtime_budgets"] == {
        "time_budget_ms": 300_000,
        "remaining_iterations": 15,
        "remaining_tool_calls": 9,
    }
    assert runtime_config.metadata["runtime_budgets"] == {
        "time_budget_ms": 300_000,
        "remaining_iterations": 15,
        "remaining_tool_calls": 9,
    }
    assert (
        second_metadata[METADATA_CONTEXT_BUNDLE_KEY][ACTIVE_AGENT_RUNS_KEY]
        == []
    )
    assert runtime_config.metadata[ACTIVE_AGENT_RUNS_KEY] == []
    assert (
        runtime_config.metadata[METADATA_CONTEXT_BUNDLE_KEY][ACTIVE_AGENT_RUNS_KEY]
        == []
    )
    assert result.metadata["status"] == "completed"


@pytest.mark.asyncio
async def test_subagent_handler_does_not_reprocess_irrelevant_active_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ControlledLauncher(registry)
    executor = _IrrelevantFirstParentExecutor()
    second_definition = _second_agent_definition()
    definition_registry = DefinitionSubagentRegistry(
        [
            get_definition_subagent_registry().require("pathfinder"),
            second_definition,
        ]
    )
    monkeypatch.setattr(
        "agent.subagents.registry.get_subagent_registry",
        lambda: definition_registry,
    )

    async def _publish(_task_id: int, _event: dict[str, Any]) -> None:
        return None

    handler = SubagentHandler(
        object(),
        executor,
        object(),
        registry=registry,
        launcher=launcher,
        lifecycle_publisher=_publish,
        subagent_registry=definition_registry,
    )
    runtime_config = _ordered_handoff_runtime_config(
        {
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "capabilities": ["port_scanning"],
            "targets": ["10.0.0.10"],
            "objective": "Scan ports on 10.0.0.10.",
        },
        {
            "agent_id": "cartographer",
            "agent_kind": "recon",
            "capabilities": ["asset_inventory"],
            "targets": ["10.0.0.10"],
            "objective": "Inventory approved assets.",
        },
    )

    result_task = asyncio.create_task(handler.handle(runtime_config))
    await _wait_for_call_count(launcher, 2)
    launcher.releases[0].set()
    await _wait_for_parent_call_count(executor, 1)
    launcher.releases[1].set()
    result = await result_task

    assert result.final_text == "Main agent finalized without Cartographer."
    assert len(executor.parent_calls) == 1
    first_graph_input = executor.parent_calls[0]["graph_input"]
    first_metadata = first_graph_input["facts"]["metadata"]
    first_bundle = first_metadata[METADATA_CONTEXT_BUNDLE_KEY]
    active_runs = first_bundle[ACTIVE_AGENT_RUNS_KEY]
    assert [run["agent_id"] for run in active_runs] == ["cartographer"]
    entries = sorted(
        await registry.list_task_runs(tenant_id=7, task_id=42),
        key=lambda entry: entry.agent_id,
    )
    assert [entry.agent_id for entry in entries] == ["cartographer", "pathfinder"]
    assert [entry.status for entry in entries] == ["completed", "completed"]
    assert [entry.result_consumed for entry in entries] == [True, True]
    later_handoff = await AgentRunResultProjector(registry=registry).collect_for_context(
        tenant_id=7,
        task_id=42,
        conversation_id="conv-42",
    )
    assert later_handoff.results == ()
    assert later_handoff.agent_run_ids == ()


@pytest.mark.asyncio
async def test_subagent_handler_serializes_repeated_agent_handoffs_by_limit() -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ControlledLauncher(registry)

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
    runtime_config = _ordered_handoff_runtime_config(
        {
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "capabilities": ["port_scanning"],
            "targets": ["10.0.0.10"],
            "objective": "Scan the first target.",
        },
        {
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "capabilities": ["port_scanning"],
            "targets": ["10.0.0.11"],
            "objective": "Scan the second target.",
        },
    )

    result_task = asyncio.create_task(handler.handle(runtime_config))
    await _wait_for_call_count(launcher, 1)
    await asyncio.sleep(0)
    assert len(launcher.calls) == 1

    launcher.releases[0].set()
    await _wait_for_call_count(launcher, 2)
    launcher.releases[1].set()
    result = await result_task

    assert result.metadata["status"] == "completed"
    assignments = [call["assignment"] for call in launcher.calls]
    assert [assignment.objective for assignment in assignments] == [
        "Scan the first target.",
        "Scan the second target.",
    ]
    assert len({assignment.agent_run_id for assignment in assignments}) == 2
    assert len({call["graph_thread_id"] for call in launcher.calls}) == 2


@pytest.mark.asyncio
async def test_subagent_handler_fails_closed_for_concurrent_same_agent_parent_turns() -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ControlledLauncher(registry)

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

    first = asyncio.create_task(handler.handle(_runtime_config()))
    second = asyncio.create_task(handler.handle(_runtime_config()))
    await _wait_for_call_count(launcher, 1)
    await asyncio.sleep(0)

    assert len(launcher.calls) == 1

    launcher.releases[0].set()
    results = await asyncio.gather(first, second)

    statuses = sorted(result.metadata["status"] for result in results)
    assert statuses == ["completed", "failed"]
    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].status == "completed"
    assert entries[0].result_consumed is True


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
    executor = _CompletingExecutor()
    events: list[dict[str, Any]] = []

    async def _publish(task_id: int, event: dict[str, Any]) -> None:
        events.append(event)

    handler = SubagentHandler(
        object(),
        executor,
        object(),
        registry=registry,
        launcher=_FailingLauncher(),
        lifecycle_publisher=_publish,
    )

    runtime_config = _runtime_config()
    result = await handler.handle(runtime_config)

    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].status == "failed"
    assert entries[0].safe_error == "Pathfinder launch failed"
    assert entries[0].result_consumed is True
    assert result.metadata["status"] == "completed"
    assert result.metadata["handoff_agent_id"] == "pathfinder"
    assert result.final_text == "Main agent finalized Pathfinder result."
    assert result.usage is not None
    assert len(result.usage) == 1
    assert len(executor.calls) == 1
    completed_results = runtime_config.metadata[COMPLETED_AGENT_RESULTS_KEY]
    assert [handoff["outcome"] for handoff in completed_results] == ["failed"]
    lifecycle_events = [event for event in events if "agent_run" in event]
    assert [event["agent_run"]["status"] for event in lifecycle_events] == [
        "queued",
        "running",
        "failed",
    ]
    assert lifecycle_events[-1]["agent_run"]["safe_error"] == (
        "Pathfinder launch failed"
    )


@pytest.mark.asyncio
async def test_subagent_handler_settles_prior_batch_child_when_later_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _FailingSecondLaunchAfterCompletionLauncher(registry)
    executor = _CompletingExecutor()
    second_definition = _second_agent_definition()
    definition_registry = DefinitionSubagentRegistry(
        [
            get_definition_subagent_registry().require("pathfinder"),
            second_definition,
        ]
    )
    monkeypatch.setattr(
        "agent.subagents.registry.get_subagent_registry",
        lambda: definition_registry,
    )

    async def _publish(_task_id: int, _event: dict[str, Any]) -> None:
        return None

    handler = SubagentHandler(
        object(),
        executor,
        object(),
        registry=registry,
        launcher=launcher,
        lifecycle_publisher=_publish,
        subagent_registry=definition_registry,
    )
    runtime_config = _ordered_handoff_runtime_config(
        {
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "capabilities": ["port_scanning"],
            "targets": ["10.0.0.10"],
            "objective": "Scan ports on 10.0.0.10.",
        },
        {
            "agent_id": "cartographer",
            "agent_kind": "recon",
            "capabilities": ["asset_inventory"],
            "targets": ["10.0.0.10"],
            "objective": "Inventory approved assets.",
        },
    )

    result = await handler.handle(runtime_config)

    assert result.metadata["status"] == "completed"
    assert result.metadata["handoff_agent_ids"] == ["pathfinder", "cartographer"]
    assert result.final_text == "Main agent finalized Pathfinder result."
    assert result.usage is not None
    assert len(result.usage) == 2
    usage = result.usage[0]
    assert usage.usage.total_tokens == 15
    assert usage.metadata.execution_branch == "subagent_child"
    assert usage.metadata.node_name == "subagent_runtime_model"
    assert len(executor.calls) == 1
    completed_results = runtime_config.metadata[COMPLETED_AGENT_RESULTS_KEY]
    assert [handoff["agent_id"] for handoff in completed_results] == [
        "pathfinder",
        "cartographer",
    ]
    assert [handoff["outcome"] for handoff in completed_results] == [
        "completed",
        "failed",
    ]
    entries = sorted(
        await registry.list_task_runs(tenant_id=7, task_id=42),
        key=lambda entry: entry.agent_id,
    )
    assert [entry.agent_id for entry in entries] == ["cartographer", "pathfinder"]
    assert [entry.status for entry in entries] == ["failed", "completed"]
    assert entries[0].result_consumed is True
    assert entries[1].result_consumed is True
    later_handoff = await AgentRunResultProjector(registry=registry).collect_for_context(
        tenant_id=7,
        task_id=42,
        conversation_id="conv-42",
    )
    assert later_handoff.results == ()
    assert later_handoff.agent_run_ids == ()


@pytest.mark.asyncio
async def test_subagent_handler_child_cancellation_returns_partial_child_usage() -> None:
    registry = ProcessLocalAgentRunRegistry()
    executor = _CompletingExecutor()

    async def _publish(_task_id: int, _event: dict[str, Any]) -> None:
        return None

    handler = SubagentHandler(
        object(),
        executor,
        object(),
        registry=registry,
        launcher=_CancellingLauncher(registry),
        lifecycle_publisher=_publish,
    )

    result = await handler.handle(_runtime_config())

    assert result.metadata["status"] == "completed"
    assert result.final_text == "Main agent finalized Pathfinder result."
    assert result.usage is not None
    assert len(result.usage) == 2
    usage = result.usage[0]
    assert usage.usage.total_tokens == 15
    assert usage.metadata.execution_branch == "subagent_child"
    assert usage.metadata.node_name == "subagent_runtime_model"
    assert len(executor.calls) == 1
    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].status == "cancelled"
    assert entries[0].result_consumed is True


@pytest.mark.asyncio
async def test_subagent_handler_hitl_pause_keeps_parent_open_until_child_resumes() -> None:
    registry = ProcessLocalAgentRunRegistry()
    executor = _CompletingExecutor()
    launcher = _PausingLauncher(registry)

    async def _publish(_task_id: int, _event: dict[str, Any]) -> None:
        return None

    handler = SubagentHandler(
        object(),
        executor,
        object(),
        registry=registry,
        launcher=launcher,
        lifecycle_publisher=_publish,
    )

    parent_task = asyncio.create_task(handler.handle(_runtime_config()))
    await asyncio.wait_for(launcher.paused.wait(), timeout=1)
    await asyncio.sleep(0)
    assert parent_task.done() is False

    await launcher.complete_after_approval()
    result = await asyncio.wait_for(parent_task, timeout=1)

    assert result.metadata["status"] == "completed"
    assert result.final_text == "Main agent finalized Pathfinder result."
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_subagent_handler_child_failure_returns_partial_child_usage() -> None:
    registry = ProcessLocalAgentRunRegistry()
    executor = _CompletingExecutor()

    async def _publish(_task_id: int, _event: dict[str, Any]) -> None:
        return None

    handler = SubagentHandler(
        object(),
        executor,
        object(),
        registry=registry,
        launcher=_FailingAfterUsageLauncher(registry),
        lifecycle_publisher=_publish,
    )

    result = await handler.handle(_runtime_config())

    assert result.metadata["status"] == "completed"
    assert result.final_text == "Main agent finalized Pathfinder result."
    assert result.usage is not None
    assert len(result.usage) == 2
    usage = result.usage[0]
    assert usage.usage.total_tokens == 15
    assert usage.metadata.execution_branch == "subagent_child"
    assert usage.metadata.node_name == "subagent_runtime_model"
    assert len(executor.calls) == 1
    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    assert len(entries) == 1
    assert entries[0].status == "failed"
    assert entries[0].result_consumed is True


@pytest.mark.asyncio
async def test_par_followup_delegation_replay_does_not_launch_duplicate() -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ControlledLauncher(registry)

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
    runtime_config = _runtime_config()
    turn = SimpleNamespace(turn_id="task-42-turn-5", turn_number=5)
    agent_handoff = {
        "agent_handoff": "required",
        "subagent": "pathfinder",
        "objective": "Check the unresolved HTTPS service evidence.",
    }

    first = await handler._dispatch_par_followup_delegation(
        runtime_config,
        turn=turn,
        agent_handoff=agent_handoff,
        decision_id="par-candidate-1",
    )
    second = await handler._dispatch_par_followup_delegation(
        runtime_config,
        turn=turn,
        agent_handoff=agent_handoff,
        decision_id="par-candidate-1",
    )

    assert len(launcher.calls) == 1
    assert first.agent_run_ids == second.agent_run_ids
    assert len(first.launched_agent_run_ids) == 1
    assert second.launched_agent_run_ids == ()
    assignment = launcher.calls[0]["assignment"]
    assert assignment.agent_run_id == first.agent_run_ids[0]
    assert assignment.objective == "Check the unresolved HTTPS service evidence."
    assert assignment.relevant_context["delegation_source"] == "par"
    assert assignment.relevant_context["delegation_decision_id"] == "par-candidate-1"
    assert assignment.suggested_capabilities == ()

    launcher.releases[0].set()


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
    assert entries[0].result.summary == "Pathfinder found HTTP."
    assert entries[0].result_consumed is True
    lifecycle_events = [event for event in events if "agent_run" in event]
    parent_progress_events = [
        event
        for event in events
        if event.get("metadata", {}).get("progress_kind") == "parent_handoff"
    ]
    assert [event["agent_run"]["status"] for event in lifecycle_events] == [
        "queued",
        "running",
        "completed",
    ]
    assert lifecycle_events[-1]["agent_run"]["status"] == "completed"
    assert any(event["type"] == "reasoning_delta" for event in parent_progress_events)
    assert result.usage is not None
    assert [record.metadata.execution_branch for record in result.usage] == [
        "subagent_child",
        "subagent_parent_finalizer"
    ]
    assert [record.usage.total_tokens for record in result.usage] == [15, 29]


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
