"""Direct tests for replay-stable PAR follow-up dispatch.

These tests lock the extracted follow-up coordinator independently from the
dispatch facade. They do not test initial dispatch admission, parent-handoff
coordination, child settlement, or presentation formatting.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

import pytest

from agent.subagents.registry import SubagentRegistry
from backend.services.agent_runs.completion import AgentRunCompletion
from backend.services.agent_runs.contracts import AgentAssignment
from backend.services.agent_runs.dispatch_batch import DispatchBatchExecutor
from backend.services.agent_runs.dispatch_contracts import (
    AgentRunDispatchStop,
    DispatchBatchLaunchFailure,
)
from backend.services.agent_runs.dispatch_followup import FollowupDispatcher
from backend.services.agent_runs.dispatch_plan import (
    PlannedAgentInvocation,
    stable_par_assignment_identity,
)
from backend.services.agent_runs.dispatch_settlement import DispatchSettlement
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.registry_contracts import LocalAgentRun
from backend.services.langgraph_chat.contracts import LangGraphRuntimeConfig
from backend.tests.services.agent_runs.test_dispatch_service import (
    CONVERSATION_ID,
    PARENT_GRAPH_THREAD_ID,
    PARENT_RUN_ID,
    PARENT_TURN_ID,
    TASK_ID,
    TENANT_ID,
    _ScriptedLauncher,
    _assignment,
    _completion,
    _handoff,
    _runtime_config,
    _subagent_registry,
)


Trace = list[tuple[str, str]]


class _TracingRegistry(ProcessLocalAgentRunRegistry):
    def __init__(self, trace: Trace) -> None:
        super().__init__()
        self._trace = trace

    async def get(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun | None:
        self._trace.append(("registry.get", agent_run_id))
        return await super().get(
            tenant_id=tenant_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )

    async def register(
        self,
        assignment: AgentAssignment,
        *,
        graph_thread_id: str,
        max_active_runs_per_task: int | None = None,
    ) -> LocalAgentRun:
        self._trace.append(("registry.register", assignment.agent_run_id))
        return await super().register(
            assignment,
            graph_thread_id=graph_thread_id,
            max_active_runs_per_task=max_active_runs_per_task,
        )


class _ActiveCountReader:
    def __init__(
        self,
        trace: Trace,
        *,
        counts: Mapping[str, int] | None = None,
        on_call: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._trace = trace
        self._counts = dict(counts or {})
        self._on_call = on_call
        self.calls = 0

    async def __call__(
        self,
        _runtime_config: LangGraphRuntimeConfig,
    ) -> Mapping[str, int]:
        self.calls += 1
        self._trace.append(("active_counts", "read"))
        if self._on_call is not None:
            await self._on_call()
        return dict(self._counts)


class _RecordingBatchExecutor:
    def __init__(
        self,
        result_factory: Callable[
            [tuple[PlannedAgentInvocation, ...], LangGraphRuntimeConfig],
            Any,
        ],
    ) -> None:
        self._result_factory = result_factory
        self.calls: list[
            tuple[tuple[PlannedAgentInvocation, ...], LangGraphRuntimeConfig]
        ] = []

    async def launch_batch(
        self,
        batch: Sequence[PlannedAgentInvocation],
        runtime_config: LangGraphRuntimeConfig,
    ) -> Any:
        captured = tuple(batch)
        self.calls.append((captured, runtime_config))
        return self._result_factory(captured, runtime_config)


async def _publish_noop(_task_id: int, _event: dict[str, Any]) -> None:
    return None


def _real_dispatcher(
    *,
    registry: ProcessLocalAgentRunRegistry | None = None,
    subagent_registry: SubagentRegistry | None = None,
    launcher: _ScriptedLauncher | None = None,
    active_reader: _ActiveCountReader | None = None,
    trace: Trace | None = None,
) -> tuple[
    FollowupDispatcher,
    ProcessLocalAgentRunRegistry,
    _ScriptedLauncher,
    _ActiveCountReader,
    Trace,
]:
    resolved_trace = trace if trace is not None else []
    resolved_registry = registry or _TracingRegistry(resolved_trace)
    resolved_subagents = subagent_registry or _subagent_registry()
    resolved_launcher = launcher or _ScriptedLauncher(
        resolved_registry,
        trace=resolved_trace,
    )
    resolved_reader = active_reader or _ActiveCountReader(resolved_trace)
    batch_executor = DispatchBatchExecutor(
        registry=resolved_registry,
        launcher=resolved_launcher,
        subagent_registry=resolved_subagents,
        lifecycle_publisher=_publish_noop,
        settlement=DispatchSettlement(registry=resolved_registry),
    )
    return (
        FollowupDispatcher(
            registry=resolved_registry,
            subagent_registry=resolved_subagents,
            batch_executor=batch_executor,
            active_count_reader=resolved_reader,
        ),
        resolved_registry,
        resolved_launcher,
        resolved_reader,
        resolved_trace,
    )


def _fake_dispatcher(
    batch_executor: _RecordingBatchExecutor,
    active_reader: _ActiveCountReader,
    *,
    registry: ProcessLocalAgentRunRegistry | None = None,
    subagent_registry: SubagentRegistry | None = None,
) -> FollowupDispatcher:
    return FollowupDispatcher(
        registry=registry or ProcessLocalAgentRunRegistry(),
        subagent_registry=subagent_registry or _subagent_registry(),
        batch_executor=batch_executor,  # type: ignore[arg-type]
        active_count_reader=active_reader,
    )


@pytest.mark.asyncio
async def test_malformed_and_empty_handoffs_reject_without_capacity_or_launch() -> None:
    trace: Trace = []
    reader = _ActiveCountReader(trace)
    batch_executor = _RecordingBatchExecutor(
        lambda _batch, _runtime_config: pytest.fail("launch should not be called")
    )
    dispatcher = _fake_dispatcher(batch_executor, reader)

    for invalid in (
        {},
        {"agent_handoff": "required", "subagent": "", "objective": "Do work"},
        {
            "agent_handoff": "required",
            "subagent": "pathfinder",
            "objective": "Do work",
            "extra": "not allowed",
        },
    ):
        with pytest.raises(RuntimeError, match="invalid_handoff_plan"):
            await dispatcher.dispatch_followup(
                _runtime_config(),
                parent_turn_id=PARENT_TURN_ID,
                agent_handoff=invalid,
                decision_id="decision-invalid",
            )

    assert reader.calls == 0
    assert batch_executor.calls == []


@pytest.mark.asyncio
async def test_existing_replay_returns_stable_id_before_capacity_read() -> None:
    dispatcher, registry, launcher, reader, trace = _real_dispatcher()
    objective = "Recheck HTTP headers."
    _, stable_run_id = stable_par_assignment_identity(
        delegation_decision_id="decision-replay",
        agent_id="pathfinder",
        objective=objective,
    )
    await registry.register(
        _assignment("pathfinder", stable_run_id),
        graph_thread_id="d" * 32,
    )
    trace.clear()

    result = await dispatcher.dispatch_followup(
        _runtime_config(),
        parent_turn_id=PARENT_TURN_ID,
        agent_handoff=_handoff("pathfinder", objective),
        decision_id="decision-replay",
    )

    assert result.agent_run_ids == (stable_run_id,)
    assert result.launched_agent_run_ids == ()
    assert trace == [("registry.get", stable_run_id)]
    assert reader.calls == 0
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_capacity_rejection_reads_live_counts_after_initial_replay_lookup() -> None:
    trace: Trace = []
    reader = _ActiveCountReader(trace, counts={"pathfinder": 1})
    dispatcher, _registry, launcher, _reader, trace = _real_dispatcher(
        active_reader=reader,
        trace=trace,
    )
    objective = "Inspect the bounded service."
    _, stable_run_id = stable_par_assignment_identity(
        delegation_decision_id="decision-capacity",
        agent_id="pathfinder",
        objective=objective,
    )

    with pytest.raises(RuntimeError, match="subagent_unavailable"):
        await dispatcher.dispatch_followup(
            _runtime_config(),
            parent_turn_id=PARENT_TURN_ID,
            agent_handoff=_handoff("pathfinder", objective),
            decision_id="decision-capacity",
        )

    assert trace[:2] == [
        ("registry.get", stable_run_id),
        ("active_counts", "read"),
    ]
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_successful_launch_preserves_stable_assignment_metadata_and_ordered_ids() -> None:
    dispatcher, _registry, launcher, reader, _trace = _real_dispatcher()
    objective = "Confirm the bounded exposed HTTP result."
    stable_assignment_id, stable_run_id = stable_par_assignment_identity(
        delegation_decision_id="decision-success",
        agent_id="pathfinder",
        objective=objective,
    )

    result = await dispatcher.dispatch_followup(
        _runtime_config(),
        parent_turn_id=PARENT_TURN_ID,
        agent_handoff=_handoff("pathfinder", objective),
        decision_id="decision-success",
    )

    assert result.agent_run_ids == (stable_run_id,)
    assert result.launched_agent_run_ids == (stable_run_id,)
    assert reader.calls == 1
    assert [call["assignment"].agent_run_id for call in launcher.calls] == [
        stable_run_id
    ]
    assignment = launcher.calls[0]["assignment"]
    assert assignment.assignment_id == stable_assignment_id
    assert assignment.agent_run_id == stable_run_id
    assert assignment.conversation_id == CONVERSATION_ID
    assert assignment.parent_turn_id == PARENT_TURN_ID
    assert assignment.parent_graph_thread_id == PARENT_GRAPH_THREAD_ID
    assert assignment.objective == objective
    assert assignment.targets == ("10.0.0.10",)
    assert assignment.suggested_capabilities == ("host_discovery",)
    assert assignment.relevant_context["parent_run_id"] == PARENT_RUN_ID
    assert assignment.relevant_context["delegation_source"] == "par"
    assert assignment.relevant_context["delegation_decision_id"] == "decision-success"
    assert launcher.calls[0]["graph_thread_id"]


@pytest.mark.asyncio
async def test_pre_register_replay_guard_skips_launch_without_duplicate_work() -> None:
    trace: Trace = []
    objective = "Replay the bounded service check."
    _, stable_run_id = stable_par_assignment_identity(
        delegation_decision_id="decision-race",
        agent_id="pathfinder",
        objective=objective,
    )
    registry = _TracingRegistry(trace)

    async def _register_replay_before_launch() -> None:
        await registry.register(
            _assignment("pathfinder", stable_run_id),
            graph_thread_id="e" * 32,
        )

    reader = _ActiveCountReader(trace, on_call=_register_replay_before_launch)
    dispatcher, _registry, launcher, _reader, trace = _real_dispatcher(
        registry=registry,
        active_reader=reader,
        trace=trace,
    )

    result = await dispatcher.dispatch_followup(
        _runtime_config(),
        parent_turn_id=PARENT_TURN_ID,
        agent_handoff=_handoff("pathfinder", objective),
        decision_id="decision-race",
    )

    assert result.agent_run_ids == (stable_run_id,)
    assert result.launched_agent_run_ids == ()
    assert trace.count(("registry.get", stable_run_id)) == 2
    assert ("registry.register", stable_run_id) in trace
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_unregistered_launch_failure_preserves_runtime_error_message() -> None:
    trace: Trace = []
    reader = _ActiveCountReader(trace)

    def _stop_failure(
        batch: tuple[PlannedAgentInvocation, ...],
        _runtime_config: LangGraphRuntimeConfig,
    ) -> DispatchBatchLaunchFailure:
        return DispatchBatchLaunchFailure(
            stop=AgentRunDispatchStop(invocation=batch[0], status="failed")
        )

    batch_executor = _RecordingBatchExecutor(_stop_failure)
    dispatcher = _fake_dispatcher(batch_executor, reader)

    with pytest.raises(
        RuntimeError,
        match="PAR follow-up delegation launch failed: failed",
    ):
        await dispatcher.dispatch_followup(
            _runtime_config(),
            parent_turn_id=PARENT_TURN_ID,
            agent_handoff=_handoff("pathfinder", "Bounded launch failure."),
            decision_id="decision-stop-failure",
        )

    assert reader.calls == 1
    assert len(batch_executor.calls) == 1


@pytest.mark.asyncio
async def test_registered_launch_failure_returns_ordered_ids_with_empty_launched() -> None:
    trace: Trace = []
    reader = _ActiveCountReader(trace)
    objective = "Bounded registered launch failure."
    _, stable_run_id = stable_par_assignment_identity(
        delegation_decision_id="decision-registered-failure",
        agent_id="pathfinder",
        objective=objective,
    )

    def _registered_failure(
        batch: tuple[PlannedAgentInvocation, ...],
        _runtime_config: LangGraphRuntimeConfig,
    ) -> DispatchBatchLaunchFailure:
        completion: AgentRunCompletion = _completion(
            batch[0].assignment,
            graph_thread_id=batch[0].graph_thread_id,
            outcome="failed",
        )
        return DispatchBatchLaunchFailure(child_completions=(completion,))

    batch_executor = _RecordingBatchExecutor(_registered_failure)
    dispatcher = _fake_dispatcher(batch_executor, reader)

    result = await dispatcher.dispatch_followup(
        _runtime_config(),
        parent_turn_id=PARENT_TURN_ID,
        agent_handoff=_handoff("pathfinder", objective),
        decision_id="decision-registered-failure",
    )

    assert result.agent_run_ids == (stable_run_id,)
    assert result.launched_agent_run_ids == ()
    assert reader.calls == 1
    assert len(batch_executor.calls) == 1
