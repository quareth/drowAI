"""Tests for the process-local Scout asyncio launcher."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import pytest

from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
    AgentRuntimeIdentity,
)
from backend.services.agent_runs.launcher import AgentRunLauncher, ScoutRunPaused
from backend.services.agent_runs.registry import (
    LocalAgentRun,
    ProcessLocalAgentRunRegistry,
)


def _runtime_identity(*, tenant_id: int = 7, task_id: int = 42) -> AgentRuntimeIdentity:
    return AgentRuntimeIdentity(
        tenant_id=tenant_id,
        task_id=task_id,
        workspace_id=f"task-{task_id}",
        workspace_path="/workspace",
        runtime_placement_mode="runner",
        actor_type="user",
        actor_id="3",
        runner_id="runner-1",
        execution_site_id="site-1",
        provider="openai",
        model="gpt-5.2-mini",
        reasoning_effort="medium",
        feature_flags={},
    )


def _assignment(
    *,
    tenant_id: int = 7,
    task_id: int = 42,
    agent_run_id: str = "run-1",
) -> AgentAssignment:
    return AgentAssignment(
        assignment_id=f"assign-{agent_run_id}",
        agent_run_id=agent_run_id,
        agent_id="pathfinder",
        agent_kind="recon",
        task_id=task_id,
        tenant_id=tenant_id,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        parent_graph_thread_id="parent-thread-1",
        objective="Map open services on the approved target.",
        targets=["10.0.0.10"],
        suggested_capabilities=["host_discovery", "port_scan"],
        scope_summary="Approved internal test host only.",
        relevant_context={"ticket": "ENG-123"},
        runtime_identity=_runtime_identity(tenant_id=tenant_id, task_id=task_id),
    )


def _result(agent_run_id: str = "run-1") -> AgentResult:
    return AgentResult(
        agent_run_id=agent_run_id,
        agent_id="pathfinder",
        agent_kind="recon",
        outcome="completed",
        summary="Scout found exposed HTTP.",
        key_findings=["HTTP exposed on 80"],
        evidence_refs=[{"kind": "artifact", "path": "/workspace/artifacts/nmap.xml"}],
        tools_used=["nmap"],
        limitations=[],
        recommended_next_steps=["Review HTTP headers"],
        final_checkpoint_id="checkpoint-1",
    )


@pytest.mark.asyncio
async def test_launch_attaches_task_and_completion_callback_stores_result() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    release_worker = asyncio.Event()

    async def _worker(**kwargs: Any) -> AgentResult:
        assert kwargs["assignment"] == assignment
        assert kwargs["graph_thread_id"] == "child-thread-1"
        assert await kwargs["is_cancel_requested"]() is False
        await release_worker.wait()
        return _result("run-1")

    launcher = AgentRunLauncher(registry=registry, worker=_worker)

    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
    )
    attached = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")

    assert attached is not None
    assert attached.task_handle is task
    assert task.done() is False

    release_worker.set()
    assert await task == _result("run-1")
    completed = await _wait_for_status(
        registry, tenant_id=7, task_id=42, agent_run_id="run-1", status="completed"
    )

    assert completed.result == _result("run-1")
    assert completed.task_handle is None


@pytest.mark.asyncio
async def test_terminal_completion_publishes_attributed_lifecycle_event() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    events: list[tuple[int, dict[str, Any]]] = []

    async def _publish(task_id: int, event: dict[str, Any]) -> None:
        events.append((task_id, event))

    async def _worker(**_kwargs: Any) -> AgentResult:
        return _result("run-1")

    launcher = AgentRunLauncher(
        registry=registry,
        worker=_worker,
        lifecycle_publisher=_publish,
    )

    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
        parent_run_id="parent-run-1",
    )

    assert await task == _result("run-1")
    await _wait_for_status(
        registry, tenant_id=7, task_id=42, agent_run_id="run-1", status="completed"
    )

    assert len(events) == 1
    task_id, event = events[0]
    metadata = event["metadata"]
    assert task_id == 42
    assert event["agent_run"]["status"] == "completed"
    assert metadata["producer_type"] == "subagent"
    assert metadata["agent_run_id"] == "run-1"
    assert metadata["agent_kind"] == "recon"
    assert metadata["agent_display_name"] == "Pathfinder"
    assert metadata["parent_turn_id"] == "turn-1"
    assert metadata["parent_run_id"] == "parent-run-1"
    assert metadata["lifecycle_version"] == 2
    assert "assignment" not in event["agent_run"] or event["agent_run"]["assignment"] is None


@pytest.mark.asyncio
async def test_worker_failure_is_sanitized_and_contained() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")

    async def _worker(**_kwargs: Any) -> AgentResult:
        raise RuntimeError("secret token=abc123")

    launcher = AgentRunLauncher(registry=registry, worker=_worker)

    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
    )

    with pytest.raises(RuntimeError):
        await task
    failed = await _wait_for_status(
        registry, tenant_id=7, task_id=42, agent_run_id="run-1", status="failed"
    )

    assert failed.safe_error == "Scout worker failed"
    assert "abc123" not in failed.safe_error
    assert failed.task_handle is None


@pytest.mark.asyncio
async def test_cancellation_signal_is_scoped_to_exact_local_run() -> None:
    registry = ProcessLocalAgentRunRegistry()
    first = _assignment(tenant_id=7, task_id=42, agent_run_id="run-1")
    second = _assignment(tenant_id=8, task_id=42, agent_run_id="run-2")
    await registry.register(first, graph_thread_id="child-thread-1")
    await registry.register(second, graph_thread_id="child-thread-2")

    async def _worker(**kwargs: Any) -> AgentResult:
        while not await kwargs["is_cancel_requested"]():
            await asyncio.sleep(0.01)
        await asyncio.sleep(60)
        return _result(kwargs["assignment"].agent_run_id)

    launcher = AgentRunLauncher(registry=registry, worker=_worker)
    first_task = await launcher.launch(
        assignment=first,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
    )
    second_task = await launcher.launch(
        assignment=second,
        runtime_config=object(),
        graph_thread_id="child-thread-2",
    )

    cancelled_request = await launcher.request_cancellation(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    )

    assert cancelled_request.cancel_requested is True
    assert first_task.cancelling() > 0
    assert second_task.done() is False

    with contextlib.suppress(asyncio.CancelledError):
        await first_task
    cancelled = await _wait_for_status(
        registry, tenant_id=7, task_id=42, agent_run_id="run-1", status="cancelled"
    )
    other = await registry.get(tenant_id=8, task_id=42, agent_run_id="run-2")

    assert cancelled.task_handle is None
    assert other is not None
    assert other.cancel_requested is False
    assert other.task_handle is second_task

    await launcher.request_cancellation(tenant_id=8, task_id=42, agent_run_id="run-2")
    with contextlib.suppress(asyncio.CancelledError):
        await second_task
    await _wait_for_status(
        registry, tenant_id=8, task_id=42, agent_run_id="run-2", status="cancelled"
    )


@pytest.mark.asyncio
async def test_paused_approval_cancellation_becomes_terminal_and_publishes() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    events: list[tuple[int, dict[str, Any]]] = []

    async def _publish(task_id: int, event: dict[str, Any]) -> None:
        events.append((task_id, event))

    async def _worker(**_kwargs: Any) -> AgentResult:
        raise ScoutRunPaused(execution_result={"interrupt_id": "interrupt-1"})

    launcher = AgentRunLauncher(
        registry=registry,
        worker=_worker,
        lifecycle_publisher=_publish,
    )

    task = await launcher.launch(
        assignment=assignment,
        runtime_config=object(),
        graph_thread_id="child-thread-1",
    )
    waiting = await _wait_for_status(
        registry,
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        status="waiting_for_approval",
    )

    assert task.done() is True
    assert waiting.task_handle is task

    cancelled = await launcher.request_cancellation(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    )

    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested is True
    assert cancelled.task_handle is None
    assert [event["agent_run"]["status"] for _task_id, event in events] == [
        "waiting_for_approval",
        "cancelled",
    ]
    assert events[-1][0] == 42
    assert events[-1][1]["metadata"]["agent_run_id"] == "run-1"
    assert events[-1][1]["metadata"]["lifecycle_version"] == cancelled.lifecycle_version


@pytest.mark.asyncio
async def test_create_task_failure_does_not_attach_local_handle() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")

    async def _worker(**_kwargs: Any) -> AgentResult:
        return _result("run-1")

    def _failing_task_factory(_coro: Any) -> asyncio.Task[AgentResult]:
        raise RuntimeError("create task failed")

    launcher = AgentRunLauncher(
        registry=registry,
        worker=_worker,
        task_factory=_failing_task_factory,
    )

    with pytest.raises(RuntimeError):
        await launcher.launch(
            assignment=assignment,
            runtime_config=object(),
            graph_thread_id="child-thread-1",
        )
    entry = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")

    assert entry is not None
    assert entry.status == "queued"
    assert entry.task_handle is None


def test_launcher_module_has_no_durable_or_route_boundary_dependencies() -> None:
    source = Path("backend/services/agent_runs/launcher.py").read_text()

    assert "backend.database" not in source
    assert "sqlalchemy" not in source
    assert "Session" not in source
    assert "scheduler" not in source.lower()
    assert "lease" not in source.lower()
    assert "poll" not in source.lower()


async def _wait_for_status(
    registry: ProcessLocalAgentRunRegistry,
    *,
    tenant_id: int,
    task_id: int,
    agent_run_id: str,
    status: str,
) -> LocalAgentRun:
    for _ in range(20):
        entry = await registry.get(
            tenant_id=tenant_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        if entry is not None and entry.status == status:
            return entry
        await asyncio.sleep(0)
    raise AssertionError(f"Timed out waiting for {status}")
