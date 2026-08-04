"""Direct tests for one-batch subagent dispatch launch.

These tests lock the extracted batch executor's register-to-launch side effects
independently from the dispatch facade. They do not test admission, parent
handoff coordination, or presentation outcomes.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")

import pytest

from agent.subagents.definition import SubagentDefinition
from agent.subagents.registry import SubagentDisplayMetadata, SubagentRegistry
from backend.services.agent_runs.contracts import AgentAssignment
from backend.services.agent_runs.dispatch_batch import DispatchBatchExecutor
from backend.services.agent_runs.dispatch_contracts import (
    DispatchBatchLaunch,
    DispatchBatchLaunchFailure,
)
from backend.services.agent_runs.dispatch_plan import PlannedAgentInvocation
from backend.services.agent_runs.dispatch_settlement import DispatchSettlement
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.registry_contracts import LocalAgentRun
from backend.tests.services.agent_runs.test_dispatch_service import (
    PARENT_RUN_ID,
    TASK_ID,
    TENANT_ID,
    _ScriptedLauncher,
    _assignment,
    _plan,
    _runtime_config,
    _subagent_registry,
)


Trace = list[tuple[str, str]]


class _TracingRegistry(ProcessLocalAgentRunRegistry):
    def __init__(
        self,
        trace: Trace,
        *,
        fail_get_ids: Sequence[str] = (),
        fail_mark_running_ids: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self._trace = trace
        self._fail_get_ids = frozenset(fail_get_ids)
        self._fail_mark_running_ids = frozenset(fail_mark_running_ids)

    async def get(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun | None:
        self._trace.append(("registry.get", agent_run_id))
        if agent_run_id in self._fail_get_ids:
            raise RuntimeError("registry get failed")
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

    async def mark_running(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
    ) -> LocalAgentRun:
        self._trace.append(("registry.mark_running", agent_run_id))
        if agent_run_id in self._fail_mark_running_ids:
            raise RuntimeError("mark running failed")
        return await super().mark_running(
            tenant_id=tenant_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )

    async def mark_failed(
        self,
        *,
        tenant_id: int,
        task_id: int,
        agent_run_id: str,
        safe_error: str,
    ) -> LocalAgentRun:
        self._trace.append(("registry.mark_failed", agent_run_id))
        return await super().mark_failed(
            tenant_id=tenant_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
            safe_error=safe_error,
        )


class _TracingSubagentRegistry(SubagentRegistry):
    def __init__(self, trace: Trace) -> None:
        super().__init__(_subagent_registry().definitions())
        self._trace = trace

    def require(self, agent_id: str) -> SubagentDefinition:
        self._trace.append(("definition.require", agent_id))
        return super().require(agent_id)

    def display_metadata(self, agent_id: str) -> SubagentDisplayMetadata:
        self._trace.append(("definition.display", agent_id))
        return super().display_metadata(agent_id)


class _RecordingLifecyclePublisher:
    def __init__(
        self,
        trace: Trace,
        *,
        fail_once_on: tuple[str, str] | None = None,
    ) -> None:
        self._trace = trace
        self._fail_once_on = fail_once_on
        self._failed = False

    async def __call__(self, task_id: int, event: dict[str, Any]) -> None:
        agent_run = event["agent_run"]
        agent_run_id = agent_run["agent_run_id"]
        status = agent_run["status"]
        assert task_id == TASK_ID
        assert event["metadata"]["parent_run_id"] == PARENT_RUN_ID
        assert agent_run["parent_run_id"] == PARENT_RUN_ID
        assert "runtime_services" not in event
        assert "runtime_services" not in agent_run
        self._trace.append(("publish", f"{agent_run_id}:{status}"))
        if (
            not self._failed
            and self._fail_once_on == (agent_run_id, status)
        ):
            self._failed = True
            raise RuntimeError(f"publish {status} failed")


def _executor(
    *,
    registry: _TracingRegistry | None = None,
    launcher: _ScriptedLauncher | None = None,
    trace: Trace | None = None,
    lifecycle_publisher: _RecordingLifecyclePublisher | None = None,
) -> tuple[DispatchBatchExecutor, _TracingRegistry, _ScriptedLauncher, Trace]:
    resolved_trace = trace if trace is not None else []
    resolved_registry = registry or _TracingRegistry(resolved_trace)
    resolved_launcher = launcher or _ScriptedLauncher(
        resolved_registry,
        trace=resolved_trace,
    )
    publisher = lifecycle_publisher or _RecordingLifecyclePublisher(resolved_trace)
    return (
        DispatchBatchExecutor(
            registry=resolved_registry,
            launcher=resolved_launcher,
            subagent_registry=_TracingSubagentRegistry(resolved_trace),
            lifecycle_publisher=publisher,
            settlement=DispatchSettlement(registry=resolved_registry),
        ),
        resolved_registry,
        resolved_launcher,
        resolved_trace,
    )


def _event_index(trace: Trace, event: tuple[str, str]) -> int:
    return trace.index(event)


@pytest.mark.asyncio
async def test_successful_multi_launch_preserves_side_effect_order_and_runtime_attachment() -> None:
    executor, _registry, launcher, trace = _executor()
    runtime_services = object()

    result = await executor.launch_batch(
        _plan("pathfinder", "cartographer"),
        _runtime_config(runtime_services=runtime_services),
    )

    assert isinstance(result, DispatchBatchLaunch)
    assert [child.invocation.assignment.agent_run_id for child in result.children] == [
        "run-1",
        "run-2",
    ]
    await asyncio.gather(*(child.terminal for child in result.children))

    assert [call["assignment"].agent_run_id for call in launcher.calls] == [
        "run-1",
        "run-2",
    ]
    assert launcher.calls[0]["parent_run_id"] == PARENT_RUN_ID
    assert launcher.calls[0]["graph_thread_id"] == "1".zfill(32)
    assert (
        launcher.calls[0]["runtime_config"]["configurable"]["runtime_services"]
        is runtime_services
    )
    expected_agents = {
        "run-1": "pathfinder",
        "run-2": "cartographer",
    }
    for agent_run_id, agent_id in expected_agents.items():
        assert _event_index(trace, ("definition.require", agent_id)) < _event_index(
            trace, ("registry.register", agent_run_id)
        )
        assert _event_index(trace, ("registry.register", agent_run_id)) < _event_index(
            trace, ("publish", f"{agent_run_id}:queued")
        )
        assert _event_index(trace, ("publish", f"{agent_run_id}:queued")) < _event_index(
            trace, ("registry.mark_running", agent_run_id)
        )
        assert _event_index(
            trace, ("registry.mark_running", agent_run_id)
        ) < _event_index(trace, ("publish", f"{agent_run_id}:running"))
        assert _event_index(trace, ("publish", f"{agent_run_id}:running")) < _event_index(
            trace, ("launch", agent_run_id)
        )


@pytest.mark.asyncio
async def test_definition_lookup_failure_fails_before_registration() -> None:
    executor, _registry, launcher, trace = _executor()
    missing = PlannedAgentInvocation(
        index=0,
        assignment=_assignment("missing", "run-missing"),
        display_name="Missing",
        graph_thread_id="f" * 32,
    )

    result = await executor.launch_batch((missing,), _runtime_config())

    assert isinstance(result, DispatchBatchLaunchFailure)
    assert result.stop is not None
    assert result.stop.status == "failed"
    assert result.stop.invocation == missing
    assert ("definition.require", "missing") in trace
    assert not any(event[0] == "registry.register" for event in trace)
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_register_failure_fails_before_lifecycle_publication() -> None:
    trace: Trace = []
    registry = _TracingRegistry(trace)
    active = _assignment("pathfinder", "active-run")
    await registry.register(active, graph_thread_id="a" * 32)
    await registry.mark_running(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        agent_run_id=active.agent_run_id,
    )
    trace.clear()
    executor, _registry, launcher, trace = _executor(registry=registry, trace=trace)

    result = await executor.launch_batch((_plan("pathfinder")[0],), _runtime_config())

    assert isinstance(result, DispatchBatchLaunchFailure)
    assert result.stop is not None
    assert result.stop.status == "failed"
    assert ("registry.register", "run-1") in trace
    assert not any(event[0] == "publish" for event in trace)
    assert launcher.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "scenario",
        "publisher_failure",
        "invalid_graph_thread",
        "fail_mark_running",
        "launcher_script",
    ),
    [
        ("queued_publish", ("run-1", "queued"), False, False, None),
        ("child_config", None, True, False, None),
        ("mark_running", None, False, True, None),
        ("running_publish", ("run-1", "running"), False, False, None),
        ("launcher", None, False, False, "launch_error"),
    ],
)
async def test_registered_failure_points_mark_and_publish_failed_run(
    scenario: str,
    publisher_failure: tuple[str, str] | None,
    invalid_graph_thread: bool,
    fail_mark_running: bool,
    launcher_script: str | None,
) -> None:
    trace: Trace = []
    registry = _TracingRegistry(
        trace,
        fail_mark_running_ids=("run-1",) if fail_mark_running else (),
    )
    launcher = _ScriptedLauncher(
        registry,
        scripts={"run-1": launcher_script} if launcher_script is not None else {},
        trace=trace,
    )
    executor, _registry, launcher, trace = _executor(
        registry=registry,
        launcher=launcher,
        trace=trace,
        lifecycle_publisher=_RecordingLifecyclePublisher(
            trace,
            fail_once_on=publisher_failure,
        ),
    )
    item = _plan("pathfinder")[0]
    if invalid_graph_thread:
        item = replace(item, graph_thread_id="not-a-valid-thread")

    result = await executor.launch_batch((item,), _runtime_config())

    assert isinstance(result, DispatchBatchLaunchFailure)
    assert result.stop is None
    assert [completion.result.agent_run_id for completion in result.child_completions] == [
        "run-1"
    ]
    assert result.child_completions[0].result.outcome == "failed"
    assert result.child_completions[0].graph_thread_id == item.graph_thread_id
    assert ("registry.mark_failed", "run-1") in trace
    assert ("publish", "run-1:failed") in trace
    if scenario == "launcher":
        assert _event_index(trace, ("publish", "run-1:running")) < _event_index(
            trace, ("launch", "run-1")
        )
        assert _event_index(trace, ("launch", "run-1")) < _event_index(
            trace, ("registry.mark_failed", "run-1")
        )
    else:
        assert ("launch", "run-1") not in trace


@pytest.mark.asyncio
async def test_later_launch_failure_settles_earlier_sibling_before_failed_publish() -> None:
    trace: Trace = []
    registry = _TracingRegistry(trace)
    launcher = _ScriptedLauncher(
        registry,
        scripts={"run-1": "cancel_on_cancel", "run-2": "launch_error"},
        trace=trace,
    )
    executor, _registry, _launcher, trace = _executor(
        registry=registry,
        launcher=launcher,
        trace=trace,
    )

    result = await executor.launch_batch(
        _plan("pathfinder", "cartographer"),
        _runtime_config(),
    )

    assert isinstance(result, DispatchBatchLaunchFailure)
    assert [completion.result.agent_run_id for completion in result.child_completions] == [
        "run-1",
        "run-2",
    ]
    assert result.child_completions[0].result.outcome == "cancelled"
    assert result.child_completions[1].result.outcome == "failed"
    assert _event_index(trace, ("task_cancelled", "run-1")) < _event_index(
        trace, ("registry.mark_failed", "run-2")
    )
    assert _event_index(trace, ("registry.mark_failed", "run-2")) < _event_index(
        trace, ("publish", "run-2:failed")
    )


@pytest.mark.asyncio
async def test_replay_guard_lookup_failure_settles_siblings_before_stop() -> None:
    trace: Trace = []
    followup_run_id = "stable-followup-run"
    registry = _TracingRegistry(trace, fail_get_ids=(followup_run_id,))
    launcher = _ScriptedLauncher(
        registry,
        scripts={"run-1": "cancel_on_cancel"},
        trace=trace,
    )
    first, second = _plan("pathfinder", "cartographer")
    followup_assignment = second.assignment.model_copy(
        update={
            "agent_run_id": followup_run_id,
            "relevant_context": {
                **dict(second.assignment.relevant_context),
                "delegation_source": "par",
            },
        }
    )
    failing_followup = PlannedAgentInvocation(
        index=second.index,
        assignment=followup_assignment,
        display_name=second.display_name,
        graph_thread_id=second.graph_thread_id,
    )
    executor, _registry, launcher, trace = _executor(
        registry=registry,
        launcher=launcher,
        trace=trace,
    )

    result = await executor.launch_batch(
        (first, failing_followup),
        _runtime_config(),
    )

    assert isinstance(result, DispatchBatchLaunchFailure)
    assert result.child_completions == ()
    assert result.stop is not None
    assert result.stop.status == "failed"
    assert result.stop.invocation == failing_followup
    assert _event_index(trace, ("definition.require", "pathfinder")) < _event_index(
        trace, ("registry.register", "run-1")
    )
    assert _event_index(trace, ("registry.register", "run-1")) < _event_index(
        trace, ("publish", "run-1:queued")
    )
    assert _event_index(trace, ("publish", "run-1:queued")) < _event_index(
        trace, ("registry.mark_running", "run-1")
    )
    assert _event_index(trace, ("registry.mark_running", "run-1")) < _event_index(
        trace, ("publish", "run-1:running")
    )
    assert _event_index(trace, ("publish", "run-1:running")) < _event_index(
        trace, ("launch", "run-1")
    )
    assert _event_index(trace, ("definition.require", "cartographer")) < _event_index(
        trace, ("registry.get", followup_run_id)
    )
    assert _event_index(trace, ("registry.get", followup_run_id)) < _event_index(
        trace, ("task_cancelled", "run-1")
    )
    assert ("registry.register", followup_run_id) not in trace
    assert ("registry.mark_failed", followup_run_id) not in trace
    assert not any(
        event[0] == "publish" and event[1].startswith(f"{followup_run_id}:")
        for event in trace
    )
    assert [call["assignment"].agent_run_id for call in launcher.calls] == ["run-1"]


@pytest.mark.asyncio
async def test_replay_guard_skips_launch_immediately_before_registration() -> None:
    trace: Trace = []
    registry = _TracingRegistry(trace)
    assignment = _assignment("pathfinder", "stable-followup-run")
    replay_assignment = assignment.model_copy(
        update={
            "relevant_context": {
                **dict(assignment.relevant_context),
                "delegation_source": "par",
            }
        }
    )
    item = PlannedAgentInvocation(
        index=0,
        assignment=replay_assignment,
        display_name="Pathfinder",
        graph_thread_id="d" * 32,
    )
    await registry.register(replay_assignment, graph_thread_id=item.graph_thread_id)
    trace.clear()
    executor, _registry, launcher, trace = _executor(registry=registry, trace=trace)

    result = await executor.launch_batch((item,), _runtime_config())

    assert isinstance(result, DispatchBatchLaunch)
    assert result.children == ()
    assert trace[:2] == [
        ("definition.require", "pathfinder"),
        ("registry.get", "stable-followup-run"),
    ]
    assert not any(event[0] == "registry.register" for event in trace)
    assert not any(event[0] == "publish" for event in trace)
    assert launcher.calls == []
