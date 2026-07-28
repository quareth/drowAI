"""Tests for the process-local Scout agent-run registry."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
    AgentRuntimeIdentity,
)
from backend.services.agent_runs.registry import (
    ActiveScoutRunExistsError,
    AgentRunNotFoundError,
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
async def test_register_creates_queued_process_local_entry() -> None:
    now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    registry = ProcessLocalAgentRunRegistry(clock=lambda: now)

    entry = await registry.register(_assignment(), graph_thread_id="child-thread-1")

    assert entry.agent_run_id == "run-1"
    assert entry.tenant_id == 7
    assert entry.task_id == 42
    assert entry.graph_thread_id == "child-thread-1"
    assert entry.status == "queued"
    assert entry.lifecycle_version == 1
    assert entry.created_at == now
    assert entry.started_at is None
    assert entry.completed_at is None
    assert entry.task_handle is None
    assert entry.cancel_requested is False
    assert entry.result_consumed is False


@pytest.mark.asyncio
async def test_second_active_scout_for_task_is_rejected_until_terminal() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(agent_run_id="run-1"), graph_thread_id="child-1")

    with pytest.raises(ActiveScoutRunExistsError) as error:
        await registry.register(
            _assignment(agent_run_id="run-2"), graph_thread_id="child-2"
        )

    assert error.value.active_agent_run_id == "run-1"

    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result("run-1"),
    )
    accepted = await registry.register(
        _assignment(agent_run_id="run-2"), graph_thread_id="child-2"
    )

    assert accepted.agent_run_id == "run-2"
    assert accepted.status == "queued"


@pytest.mark.asyncio
async def test_lookup_is_scoped_by_tenant_task_and_agent_run_id() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(), graph_thread_id="child-thread-1")

    assert await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1") is not None
    assert await registry.get(tenant_id=8, task_id=42, agent_run_id="run-1") is None
    assert await registry.get(tenant_id=7, task_id=43, agent_run_id="run-1") is None
    assert await registry.get(tenant_id=7, task_id=42, agent_run_id="run-2") is None

    with pytest.raises(AgentRunNotFoundError):
        await registry.mark_running(tenant_id=8, task_id=42, agent_run_id="run-1")


@pytest.mark.asyncio
async def test_duplicate_terminal_callbacks_do_not_regress_state() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(), graph_thread_id="child-thread-1")
    running = await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-1")
    completed = await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result("run-1"),
    )

    duplicate_failed = await registry.mark_failed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        safe_error="later worker error",
    )
    duplicate_cancelled = await registry.mark_cancelled(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    )

    assert running.lifecycle_version == 2
    assert completed.status == "completed"
    assert completed.lifecycle_version == 3
    assert duplicate_failed == completed
    assert duplicate_cancelled == completed
    assert duplicate_failed.safe_error is None
    assert duplicate_failed.result == _result("run-1")


@pytest.mark.asyncio
async def test_lifecycle_transitions_and_cancellation_flag_cancel_task_handle() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(), graph_thread_id="child-thread-1")

    async def _sleep_until_cancelled() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(_sleep_until_cancelled())
    attached = await registry.attach_task_handle(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        task_handle=task,
    )
    waiting = await registry.mark_waiting_for_approval(
        tenant_id=7, task_id=42, agent_run_id="run-1"
    )
    cancelled_requested = await registry.request_cancellation(
        tenant_id=7, task_id=42, agent_run_id="run-1"
    )
    cancelled = await registry.mark_cancelled(
        tenant_id=7, task_id=42, agent_run_id="run-1"
    )

    assert attached.task_handle is task
    assert waiting.status == "waiting_for_approval"
    assert cancelled_requested.cancel_requested is True
    assert task.cancelled() or task.cancelling() > 0
    assert cancelled.status == "cancelled"
    assert cancelled.task_handle is None
    assert cancelled.cancel_requested is True

    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_result_consumption_is_one_shot_and_finished_cleanup_is_bounded() -> None:
    clock = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    def _clock() -> datetime:
        return clock

    registry = ProcessLocalAgentRunRegistry(
        clock=_clock, finished_retention=timedelta(seconds=30)
    )
    await registry.register(_assignment(), graph_thread_id="child-thread-1")
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result("run-1"),
    )

    assert (
        await registry.consume_result(tenant_id=7, task_id=42, agent_run_id="run-1")
    ) == _result("run-1")
    assert (
        await registry.consume_result(tenant_id=7, task_id=42, agent_run_id="run-1")
    ) is None

    clock = datetime(2026, 7, 26, 12, 0, 31, tzinfo=UTC)
    assert await registry.cleanup_finished() == 1
    assert await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1") is None


def test_registry_module_has_no_database_or_orm_dependency() -> None:
    source = Path("backend/services/agent_runs/registry.py").read_text()

    assert "backend.database" not in source
    assert "sqlalchemy" not in source
    assert "Session" not in source
    assert "ORM" not in source
