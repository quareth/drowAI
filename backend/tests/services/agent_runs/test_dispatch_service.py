"""Direct characterization tests for the subagent dispatch facade.

The tests lock observable scheduling, settlement, follow-up replay, and
lifecycle ordering for ``SubagentDispatchService`` before decomposition. They
exercise the public facade with real registry contracts and small fakes around
launcher and parent-handoff collaborators.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import replace
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

import pytest

from agent.subagents.definition import SCHEMA_VERSION, SubagentDefinition
from agent.subagents.registry import SubagentRegistry
from backend.services.agent_runs.completion import (
    AgentRunCompletion,
    build_agent_run_completion,
)
from backend.services.agent_runs.contracts import AgentAssignment, AgentResult
from backend.services.agent_runs.dispatch_plan import (
    PlannedAgentInvocation,
    stable_par_assignment_identity,
)
from backend.services.agent_runs.dispatch_service import (
    AgentRunDispatchResult,
    SubagentDispatchService,
)
from backend.services.agent_runs.launcher import (
    SubagentRunCancelled,
    SubagentRunFailed,
    SubagentRunPaused,
)
from backend.services.agent_runs.parent_handoff_coordinator import ParentHandoffOutcome
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.result_projection import CompletedAgentResultHandoff
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    LangGraphChatResult,
    LangGraphRuntimeConfig,
)
from backend.services.langgraph_chat.execution.graph_executor import GraphExecutionResult
from backend.tests.agent_run_test_support import (
    build_agent_assignment,
    build_agent_result,
    build_runtime_identity,
)


TENANT_ID = 7
TASK_ID = 42
USER_ID = 3
CONVERSATION_ID = "conversation-1"
PARENT_TURN_ID = "turn-1"
PARENT_RUN_ID = "parent-run-1"
PARENT_GRAPH_THREAD_ID = "parent-thread-1"


def _definition(
    agent_id: str,
    *,
    max_active_runs_per_task: int = 1,
) -> SubagentDefinition:
    return SubagentDefinition(
        schema_version=SCHEMA_VERSION,
        id=agent_id,
        display_name=agent_id.title(),
        kind="recon",
        description=f"{agent_id.title()} test subagent.",
        ownership_boundary=f"{agent_id.title()} owns test reconnaissance.",
        supported_task_categories=("host_discovery", "port_scan"),
        excluded_task_categories=(),
        tool_ids=("information_gathering.network_discovery.nmap",),
        enabled=True,
        max_active_runs_per_task=max_active_runs_per_task,
        max_iterations=3,
        max_tool_calls_per_iteration=4,
        requires_resolved_target=True,
        icon="radar",
        instructions="Stay inside the bounded test assignment.",
        runtime_role_prompt=None,
        runtime_boundary_rules=(),
    )


def _subagent_registry(
    *,
    pathfinder_capacity: int = 1,
    include_cartographer: bool = True,
) -> SubagentRegistry:
    definitions = [_definition("pathfinder", max_active_runs_per_task=pathfinder_capacity)]
    if include_cartographer:
        definitions.append(_definition("cartographer", max_active_runs_per_task=1))
    return SubagentRegistry(definitions)


def _runtime_config(*, runtime_services: Any = None) -> LangGraphRuntimeConfig:
    return LangGraphRuntimeConfig(
        chat_inputs=ChatInputs(
            task_id=TASK_ID,
            user_id=USER_ID,
            message="Run bounded subagent work.",
            conversation_id=CONVERSATION_ID,
            history=[],
            provider="openai",
            model="gpt-5.2-mini",
            reasoning_effort="medium",
        ),
        metadata={
            "tenant_id": TENANT_ID,
            "workspace_id": f"task-{TASK_ID}",
            "workspace_path": "/workspace",
            "runtime_placement_mode": "runner",
            "actor_type": "user",
            "actor_id": str(USER_ID),
            "runner_id": "runner-1",
            "execution_site_id": "site-1",
            "graph_thread_id": PARENT_GRAPH_THREAD_ID,
            "parent_run_id": PARENT_RUN_ID,
            "turn_sequence": 5,
            "intent_classifier_label": "direct_executor",
            "intent_hints": {
                "targets": ["10.0.0.10"],
                "suggested_capabilities": ["host_discovery"],
            },
        },
        runtime_services=runtime_services,
    )


def _assignment(agent_id: str, agent_run_id: str) -> AgentAssignment:
    runtime_identity = build_runtime_identity(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        user_id=USER_ID,
        workspace_id=f"task-{TASK_ID}",
        workspace_path="/workspace",
        runtime_placement_mode="runner",
        actor_type="user",
        actor_id=str(USER_ID),
        runner_id="runner-1",
        execution_site_id="site-1",
        provider="openai",
        model="gpt-5.2-mini",
        reasoning_effort="medium",
    )
    return build_agent_assignment(
        runtime_identity=runtime_identity,
        assignment_id=f"assign-{agent_run_id}",
        agent_run_id=agent_run_id,
        agent_id=agent_id,
        agent_kind="recon",
        conversation_id=CONVERSATION_ID,
        parent_turn_id=PARENT_TURN_ID,
        parent_graph_thread_id=PARENT_GRAPH_THREAD_ID,
        relevant_context={
            "parent_run_id": PARENT_RUN_ID,
            "turn_sequence": 5,
            "agent_mode": "full_access",
        },
    )


def _plan(*agent_ids: str) -> tuple[PlannedAgentInvocation, ...]:
    return tuple(
        PlannedAgentInvocation(
            index=index,
            assignment=_assignment(agent_id, f"run-{index + 1}"),
            display_name=agent_id.title(),
            graph_thread_id=f"{index + 1:032x}",
        )
        for index, agent_id in enumerate(agent_ids)
    )


def _result(
    assignment: AgentAssignment,
    *,
    outcome: str = "completed",
    summary: str | None = None,
) -> AgentResult:
    return build_agent_result(
        assignment,
        outcome=outcome,  # type: ignore[arg-type]
        summary=summary or f"{assignment.agent_run_id} {outcome}.",
        key_findings=(f"{assignment.agent_run_id} finding",),
        tools_used=("information_gathering.network_discovery.nmap",),
    )


def _final_state(agent_run_id: str) -> dict[str, Any]:
    return {
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
                    "agent_run_id": agent_run_id,
                }
            ]
        }
    }


def _completion(
    assignment: AgentAssignment,
    *,
    graph_thread_id: str,
    outcome: str = "completed",
) -> AgentRunCompletion:
    return build_agent_run_completion(
        result=_result(assignment, outcome=outcome),
        assignment=assignment,
        graph_thread_id=graph_thread_id,
        final_state=_final_state(assignment.agent_run_id),
    )


def _parent_outcome(
    *,
    agent_run_ids: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
    child_completions: tuple[AgentRunCompletion, ...] = (),
) -> ParentHandoffOutcome:
    return ParentHandoffOutcome(
        result=LangGraphChatResult(
            final_text="Parent finalized.",
            conversation_id=CONVERSATION_ID,
            metadata=metadata or {},
        ),
        claim_id="claim-1",
        agent_run_ids=agent_run_ids,
        child_completions=child_completions,
    )


def _handoff(subagent: str, objective: str) -> dict[str, str]:
    return {
        "agent_handoff": "required",
        "subagent": subagent,
        "objective": objective,
    }


async def _no_parent_handoff(
    _child_completions: tuple[AgentRunCompletion, ...],
    _wait_for_initial_handoff: bool,
) -> ParentHandoffOutcome | None:
    return None


class _RecordingRegistry(ProcessLocalAgentRunRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.consumed: list[tuple[int, int, str]] = []

    async def consume_result(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> AgentResult | None:
        self.consumed.append((tenant_id, task_id, agent_run_id))
        return await super().consume_result(
            tenant_id=tenant_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )


class _ListFailureRegistry(ProcessLocalAgentRunRegistry):
    async def list_task_runs(self, *, tenant_id: int, task_id: int) -> list[Any]:
        raise RuntimeError("registry read failed")


class _ScriptedLauncher:
    def __init__(
        self,
        registry: ProcessLocalAgentRunRegistry,
        *,
        scripts: dict[str, str] | None = None,
        trace: list[tuple[str, str]] | None = None,
    ) -> None:
        self.registry = registry
        self.scripts = scripts or {}
        self.trace = trace if trace is not None else []
        self.calls: list[dict[str, Any]] = []
        self.releases: dict[str, asyncio.Event] = {}

    async def launch(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        assignment: AgentAssignment = kwargs["assignment"]
        graph_thread_id: str = kwargs["graph_thread_id"]
        self.trace.append(("launch", assignment.agent_run_id))
        mode = self.scripts.get(assignment.agent_run_id, "completion")

        if mode == "launch_error":
            raise RuntimeError("launcher refused the run")
        if mode == "non_awaitable":
            return {"not": "awaitable"}

        async def _finish() -> Any:
            if mode == "blocked":
                release = asyncio.Event()
                self.releases[assignment.agent_run_id] = release
                await release.wait()
            if mode == "cancel_on_cancel":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.trace.append(("task_cancelled", assignment.agent_run_id))
                    await self.registry.mark_cancelled(
                        tenant_id=assignment.tenant_id,
                        task_id=assignment.task_id,
                        agent_run_id=assignment.agent_run_id,
                    )
                    raise
            if mode == "unexpected_exception":
                raise RuntimeError("child task exploded")
            if mode == "invalid_task_result":
                return object()
            if mode == "cancelled_exception":
                await self.registry.mark_cancelled(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    agent_run_id=assignment.agent_run_id,
                )
                raise SubagentRunCancelled(
                    execution_result=GraphExecutionResult(
                        final_state=_final_state(assignment.agent_run_id)
                    )
                )
            if mode == "failed_exception":
                await self.registry.mark_failed(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    agent_run_id=assignment.agent_run_id,
                    safe_error="Subagent worker failed",
                )
                raise SubagentRunFailed(
                    "Subagent graph completed without a valid terminal result",
                    GraphExecutionResult(final_state=_final_state(assignment.agent_run_id)),
                )
            if mode == "paused_exception":
                await self.registry.mark_waiting_for_approval(
                    tenant_id=assignment.tenant_id,
                    task_id=assignment.task_id,
                    agent_run_id=assignment.agent_run_id,
                    accounted_usage_record_count=1,
                )
                raise SubagentRunPaused(
                    execution_result=GraphExecutionResult(
                        final_state=_final_state(assignment.agent_run_id)
                    )
                )

            result = _result(assignment)
            await self.registry.mark_completed(
                tenant_id=assignment.tenant_id,
                task_id=assignment.task_id,
                agent_run_id=assignment.agent_run_id,
                result=result,
            )
            self.trace.append(("terminal", assignment.agent_run_id))
            if mode == "agent_result":
                return result
            return build_agent_run_completion(
                result=result,
                assignment=assignment,
                graph_thread_id=graph_thread_id,
                final_state=_final_state(assignment.agent_run_id),
            )

        task = asyncio.create_task(_finish())
        if mode == "cancel_on_cancel":
            await asyncio.sleep(0)
        return task


def _service(
    *,
    registry: ProcessLocalAgentRunRegistry | None = None,
    launcher: _ScriptedLauncher | None = None,
    subagent_registry: SubagentRegistry | None = None,
    lifecycle_events: list[tuple[int, str, str]] | None = None,
) -> tuple[SubagentDispatchService, ProcessLocalAgentRunRegistry, _ScriptedLauncher]:
    resolved_registry = registry or ProcessLocalAgentRunRegistry()
    resolved_launcher = launcher or _ScriptedLauncher(resolved_registry)
    resolved_subagents = subagent_registry or _subagent_registry()
    events = lifecycle_events if lifecycle_events is not None else []

    async def _publish(task_id: int, event: dict[str, Any]) -> None:
        agent_run = event["agent_run"]
        events.append((task_id, agent_run["agent_run_id"], agent_run["status"]))
        assert "runtime_services" not in event
        assert "runtime_services" not in agent_run

    return (
        SubagentDispatchService(
            registry=resolved_registry,
            launcher=resolved_launcher,
            subagent_registry=resolved_subagents,
            lifecycle_publisher=_publish,
        ),
        resolved_registry,
        resolved_launcher,
    )


async def _wait_for_call_count(launcher: _ScriptedLauncher, expected: int) -> None:
    for _ in range(100):
        if len(launcher.calls) >= expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} launch calls, got {len(launcher.calls)}")


@pytest.mark.asyncio
async def test_empty_plan_returns_default_result_and_capacity_block_fails_closed() -> None:
    service, registry, launcher = _service()

    empty = await service.dispatch(
        (),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )

    assert empty == AgentRunDispatchResult()
    assert launcher.calls == []

    active = _assignment("pathfinder", "active-run")
    await registry.register(active, graph_thread_id="a" * 32)
    await registry.mark_running(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        agent_run_id=active.agent_run_id,
    )

    blocked_plan = _plan("pathfinder")
    blocked = await service.dispatch(
        blocked_plan,
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )

    assert blocked.stop is not None
    assert blocked.stop.status == "failed"
    assert blocked.stop.invocation == blocked_plan[0]
    assert blocked.child_completions == ()
    assert len(launcher.calls) == 0


@pytest.mark.asyncio
async def test_ordered_capacity_defers_in_original_order_and_serializes_same_agent() -> None:
    service, _registry, launcher = _service()

    result = await service.dispatch(
        _plan("pathfinder", "pathfinder", "pathfinder"),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )

    assert [call["assignment"].agent_run_id for call in launcher.calls] == [
        "run-1",
        "run-2",
        "run-3",
    ]
    assert [completion.result.agent_run_id for completion in result.child_completions] == [
        "run-1",
        "run-2",
        "run-3",
    ]


@pytest.mark.asyncio
async def test_distinct_agents_launch_together_while_repeated_agent_waits() -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ScriptedLauncher(
        registry,
        scripts={"run-1": "blocked", "run-2": "blocked", "run-3": "completion"},
    )
    service, _registry, launcher = _service(registry=registry, launcher=launcher)

    dispatch_task = asyncio.create_task(
        service.dispatch(
            _plan("pathfinder", "cartographer", "pathfinder"),
            _runtime_config(),
            parent_turn_sequence=5,
            process_ready_handoffs=_no_parent_handoff,
        )
    )
    await _wait_for_call_count(launcher, 2)

    assert [call["assignment"].agent_run_id for call in launcher.calls] == [
        "run-1",
        "run-2",
    ]
    assert sorted(call["assignment"].agent_id for call in launcher.calls) == [
        "cartographer",
        "pathfinder",
    ]

    launcher.releases["run-1"].set()
    launcher.releases["run-2"].set()
    await _wait_for_call_count(launcher, 3)

    result = await asyncio.wait_for(dispatch_task, timeout=1)
    assert [call["assignment"].agent_run_id for call in launcher.calls] == [
        "run-1",
        "run-2",
        "run-3",
    ]
    assert [completion.result.agent_run_id for completion in result.child_completions] == [
        "run-1",
        "run-2",
        "run-3",
    ]


@pytest.mark.asyncio
async def test_lifecycle_order_and_runtime_services_attachment_are_launch_scoped() -> None:
    lifecycle_events: list[tuple[int, str, str]] = []
    service, _registry, launcher = _service(lifecycle_events=lifecycle_events)
    runtime_services = object()

    result = await service.dispatch(
        _plan("pathfinder"),
        _runtime_config(runtime_services=runtime_services),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )

    assert [event[1:] for event in lifecycle_events] == [
        ("run-1", "queued"),
        ("run-1", "running"),
    ]
    assert result.child_completions[0].result.agent_run_id == "run-1"
    launched_config = launcher.calls[0]["runtime_config"]
    assert launched_config["configurable"]["runtime_services"] is runtime_services


@pytest.mark.asyncio
async def test_agent_result_conversion_and_invalid_launcher_results_fail_closed() -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ScriptedLauncher(
        registry,
        scripts={
            "run-1": "agent_result",
            "run-2": "invalid_task_result",
            "run-3": "non_awaitable",
        },
    )
    service, _registry, _launcher = _service(registry=registry, launcher=launcher)

    converted = await service.dispatch(
        (_plan("pathfinder")[0],),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )
    assert converted.child_completions[0].result.agent_run_id == "run-1"
    assert converted.child_completions[0].usage_records == ()

    invalid_task = replace(_plan("pathfinder")[0], index=0, assignment=_assignment("pathfinder", "run-2"))
    invalid = await service.dispatch(
        (invalid_task,),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )
    assert invalid.stop is not None
    assert invalid.stop.status == "failed"
    assert invalid.stop.usage == ()

    non_awaitable_task = replace(
        _plan("pathfinder")[0],
        index=0,
        assignment=_assignment("pathfinder", "run-3"),
    )
    non_awaitable = await service.dispatch(
        (non_awaitable_task,),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )
    assert non_awaitable.stop is not None
    assert non_awaitable.stop.status == "failed"
    assert non_awaitable.stop.usage == ()


@pytest.mark.asyncio
async def test_pause_cancels_ready_parent_wait_and_requires_resumed_outcome() -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ScriptedLauncher(
        registry,
        scripts={"run-1": "paused_exception", "run-2": "completion"},
    )
    service, _registry, _launcher = _service(registry=registry, launcher=launcher)
    early_cancelled = asyncio.Event()
    calls: list[tuple[tuple[str, ...], bool]] = []

    async def _process(
        completions: tuple[AgentRunCompletion, ...],
        wait_for_initial_handoff: bool,
    ) -> ParentHandoffOutcome | None:
        calls.append(
            (
                tuple(completion.result.agent_run_id for completion in completions),
                wait_for_initial_handoff,
            )
        )
        if wait_for_initial_handoff and not completions:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                early_cancelled.set()
                raise
        if wait_for_initial_handoff:
            return _parent_outcome(
                agent_run_ids=tuple(
                    completion.result.agent_run_id for completion in completions
                ),
                child_completions=completions,
            )
        return None

    result = await service.dispatch(
        _plan("pathfinder", "cartographer"),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_process,
    )

    assert early_cancelled.is_set()
    assert calls[0] == ((), True)
    assert calls[-1] == (("run-2",), True)
    assert result.parent_handoff_outcome is not None
    assert result.parent_handoff_outcome.agent_run_ids == ("run-2",)

    pause_registry = ProcessLocalAgentRunRegistry()
    service, _registry, _launcher = _service(
        registry=pause_registry,
        launcher=_ScriptedLauncher(
            pause_registry,
            scripts={"run-1": "paused_exception"},
        ),
    )

    async def _missing_resumed(
        _completions: tuple[AgentRunCompletion, ...],
        _wait_for_initial_handoff: bool,
    ) -> ParentHandoffOutcome | None:
        return None

    with pytest.raises(RuntimeError, match="approval resume completed"):
        await service.dispatch(
            _plan("pathfinder"),
            _runtime_config(),
            parent_turn_sequence=5,
            process_ready_handoffs=_missing_resumed,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_outcome"),
    [
        ("cancelled_exception", "cancelled"),
        ("failed_exception", "failed"),
    ],
)
async def test_terminal_launcher_exceptions_recover_registry_completion_and_usage(
    mode: str,
    expected_outcome: str,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ScriptedLauncher(registry, scripts={"run-1": mode})
    service, _registry, _launcher = _service(registry=registry, launcher=launcher)

    result = await service.dispatch(
        _plan("pathfinder"),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )

    assert result.stop is None
    assert result.child_completions[0].result.outcome == expected_outcome
    assert result.child_completions[0].usage_records[0]["agent_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_unexpected_child_exception_returns_failed_stop_without_usage() -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ScriptedLauncher(registry, scripts={"run-1": "unexpected_exception"})
    service, _registry, _launcher = _service(registry=registry, launcher=launcher)

    result = await service.dispatch(
        _plan("pathfinder"),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )

    assert result.stop is not None
    assert result.stop.status == "failed"
    assert result.stop.usage == ()
    assert result.child_completions == ()


@pytest.mark.asyncio
async def test_later_launch_failure_settles_earlier_siblings_before_failed_publish() -> None:
    trace: list[tuple[str, str]] = []
    lifecycle_events: list[tuple[int, str, str]] = []
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ScriptedLauncher(
        registry,
        scripts={"run-1": "cancel_on_cancel", "run-2": "launch_error"},
        trace=trace,
    )
    service, _registry, _launcher = _service(
        registry=registry,
        launcher=launcher,
        lifecycle_events=lifecycle_events,
    )

    result = await service.dispatch(
        _plan("pathfinder", "cartographer"),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )

    assert result.stop is None
    assert [completion.result.agent_run_id for completion in result.child_completions] == [
        "run-1",
        "run-2",
    ]
    assert result.child_completions[0].result.outcome == "cancelled"
    assert result.child_completions[1].result.outcome == "failed"
    assert trace.index(("task_cancelled", "run-1")) < lifecycle_events.index(
        (TASK_ID, "run-2", "failed")
    )
    assert [event[1:] for event in lifecycle_events] == [
        ("run-1", "queued"),
        ("run-1", "running"),
        ("run-2", "queued"),
        ("run-2", "running"),
        ("run-2", "failed"),
    ]


@pytest.mark.asyncio
async def test_completion_order_uses_immutable_plan_index_not_batch_order() -> None:
    service, _registry, _launcher = _service()

    result = await service.dispatch(
        _plan("pathfinder", "cartographer", "pathfinder"),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )

    assert [completion.result.agent_run_id for completion in result.child_completions] == [
        "run-1",
        "run-2",
        "run-3",
    ]


@pytest.mark.asyncio
async def test_early_parent_outcome_precedence_consumes_declared_irrelevant_runs() -> None:
    registry = _RecordingRegistry()
    service, _registry, _launcher = _service(registry=registry)

    async def _process(
        completions: tuple[AgentRunCompletion, ...],
        wait_for_initial_handoff: bool,
    ) -> ParentHandoffOutcome | None:
        if wait_for_initial_handoff:
            return _parent_outcome(
                agent_run_ids=("run-1",),
                metadata={
                    "router_outcome": {
                        "par_irrelevant_active_agent_run_ids": [
                            "run-2",
                            "run-1",
                            "",
                            "run-2",
                        ]
                    }
                },
                child_completions=completions,
            )
        return None

    result = await service.dispatch(
        _plan("pathfinder", "cartographer"),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_process,
    )

    assert result.parent_handoff_outcome is not None
    assert result.parent_handoff_outcome.agent_run_ids == ("run-1",)
    assert registry.consumed == [(TENANT_ID, TASK_ID, "run-2")]
    run_1 = await registry.get(tenant_id=TENANT_ID, task_id=TASK_ID, agent_run_id="run-1")
    run_2 = await registry.get(tenant_id=TENANT_ID, task_id=TASK_ID, agent_run_id="run-2")
    assert run_1 is not None and run_1.result_consumed is False
    assert run_2 is not None and run_2.result_consumed is True


@pytest.mark.asyncio
async def test_handoff_completion_cache_hit_registry_fallback_and_cache_population() -> None:
    service, registry, _launcher = _service()
    assignment_a = _assignment("pathfinder", "run-a")
    assignment_b = _assignment("pathfinder", "run-b")
    assignment_c = _assignment("pathfinder", "run-c")
    completion_a = _completion(assignment_a, graph_thread_id="a" * 32)
    result_b = _result(assignment_b)
    await registry.register(assignment_b, graph_thread_id="b" * 32)
    await registry.mark_completed(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        agent_run_id="run-b",
        result=result_b,
    )
    await registry.register(assignment_c, graph_thread_id="c" * 32)

    cache = {"run-a": completion_a}
    completions = await service.completions_for_handoff(
        CompletedAgentResultHandoff(
            results=(),
            agent_run_ids=("run-a", "run-b", "run-c", "missing-run"),
        ),
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        completion_by_run_id=cache,
    )

    assert [completion.result.agent_run_id for completion in completions] == [
        "run-a",
        "run-b",
    ]
    assert cache["run-a"] is completion_a
    assert cache["run-b"].result == result_b
    assert "run-c" not in cache
    assert "missing-run" not in cache


@pytest.mark.asyncio
async def test_active_count_registry_read_failure_allows_dispatch_attempt() -> None:
    registry = _ListFailureRegistry()
    service, _registry, launcher = _service(registry=registry)

    result = await service.dispatch(
        _plan("pathfinder"),
        _runtime_config(),
        parent_turn_sequence=5,
        process_ready_handoffs=_no_parent_handoff,
    )

    assert result.child_completions[0].result.agent_run_id == "run-1"
    assert [call["assignment"].agent_run_id for call in launcher.calls] == ["run-1"]


@pytest.mark.asyncio
async def test_followup_invalid_plan_existing_replay_and_capacity_rejection() -> None:
    service, registry, launcher = _service()

    with pytest.raises(RuntimeError, match="invalid_handoff_plan"):
        await service.dispatch_followup(
            _runtime_config(),
            parent_turn_id=PARENT_TURN_ID,
            agent_handoff={"subagent": "", "objective": "Do work"},
            decision_id="decision-1",
        )

    objective = "Recheck HTTP headers."
    _, stable_run_id = stable_par_assignment_identity(
        delegation_decision_id="decision-2",
        agent_id="pathfinder",
        objective=objective,
    )
    existing = _assignment("pathfinder", stable_run_id)
    await registry.register(existing, graph_thread_id="d" * 32)

    replay = await service.dispatch_followup(
        _runtime_config(),
        parent_turn_id=PARENT_TURN_ID,
        agent_handoff=_handoff("pathfinder", objective),
        decision_id="decision-2",
    )

    assert replay.agent_run_ids == (stable_run_id,)
    assert replay.launched_agent_run_ids == ()
    assert launcher.calls == []

    active = _assignment("pathfinder", "active-run")
    await registry.register(active, graph_thread_id="e" * 32)
    await registry.mark_running(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        agent_run_id="active-run",
    )

    with pytest.raises(RuntimeError, match="subagent_unavailable"):
        await service.dispatch_followup(
            _runtime_config(),
            parent_turn_id=PARENT_TURN_ID,
            agent_handoff=_handoff("pathfinder", "New bounded work"),
            decision_id="decision-3",
        )


@pytest.mark.asyncio
async def test_followup_stable_success_order_and_batch_launch_failure() -> None:
    registry = ProcessLocalAgentRunRegistry()
    launcher = _ScriptedLauncher(registry)
    service, _registry, launcher = _service(registry=registry, launcher=launcher)

    objective = "Confirm the bounded exposed HTTP result."
    _, stable_run_id = stable_par_assignment_identity(
        delegation_decision_id="decision-success",
        agent_id="pathfinder",
        objective=objective,
    )
    success = await service.dispatch_followup(
        _runtime_config(),
        parent_turn_id=PARENT_TURN_ID,
        agent_handoff=_handoff("pathfinder", objective),
        decision_id="decision-success",
    )

    assert success.agent_run_ids == (stable_run_id,)
    assert success.launched_agent_run_ids == (stable_run_id,)
    assert [call["assignment"].agent_run_id for call in launcher.calls] == [
        stable_run_id
    ]
    assert launcher.calls[0]["assignment"].relevant_context["delegation_source"] == "par"

    failure_registry = ProcessLocalAgentRunRegistry()
    failure_launcher = _ScriptedLauncher(failure_registry)
    failure_service, _registry, _launcher = _service(
        registry=failure_registry,
        launcher=failure_launcher,
    )
    _, failing_run_id = stable_par_assignment_identity(
        delegation_decision_id="decision-failure",
        agent_id="pathfinder",
        objective="Bounded launch failure.",
    )
    failure_launcher.scripts[failing_run_id] = "launch_error"

    failed = await failure_service.dispatch_followup(
        _runtime_config(),
        parent_turn_id=PARENT_TURN_ID,
        agent_handoff=_handoff("pathfinder", "Bounded launch failure."),
        decision_id="decision-failure",
    )

    assert failed.agent_run_ids == (failing_run_id,)
    assert failed.launched_agent_run_ids == ()
