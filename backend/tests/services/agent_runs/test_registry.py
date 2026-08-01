"""Tests for the process-local subagent-run registry."""

from __future__ import annotations

import ast
import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
    AgentRuntimeIdentity,
)
from backend.services.agent_runs.registry import (
    ActiveAgentRunExistsError,
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
        summary="Pathfinder found exposed HTTP.",
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
    assert entry.result_claim_id is None
    assert entry.accounted_usage_record_count == 0


@pytest.mark.asyncio
async def test_registry_accepts_distinct_active_subagent_run_ids_for_task() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(
        _assignment(agent_run_id="run-1"), graph_thread_id="child-1"
    )

    second = await registry.register(
        _assignment(agent_run_id="run-2"), graph_thread_id="child-2"
    )

    assert second.agent_run_id == "run-2"
    assert second.status == "queued"


@pytest.mark.asyncio
async def test_register_capacity_claim_is_atomic_per_task_and_agent() -> None:
    registry = ProcessLocalAgentRunRegistry()

    first_attempt = registry.register(
        _assignment(agent_run_id="run-1"),
        graph_thread_id="child-1",
        max_active_runs_per_task=1,
    )
    second_attempt = registry.register(
        _assignment(agent_run_id="run-2"),
        graph_thread_id="child-2",
        max_active_runs_per_task=1,
    )

    results = await asyncio.gather(first_attempt, second_attempt, return_exceptions=True)

    successful = [result for result in results if not isinstance(result, BaseException)]
    failed = [result for result in results if isinstance(result, BaseException)]
    assert len(successful) == 1
    assert len(failed) == 1
    assert isinstance(failed[0], ActiveAgentRunExistsError)
    assert failed[0].active_agent_run_id == successful[0].agent_run_id

    other_agent = _assignment(agent_run_id="run-3").model_copy(
        update={
            "assignment_id": "assign-run-3",
            "agent_id": "cartographer",
        }
    )
    other = await registry.register(
        other_agent,
        graph_thread_id="child-3",
        max_active_runs_per_task=1,
    )

    assert other.agent_id == "cartographer"
    assert other.status == "queued"


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

    with patch("backend.services.agent_runs.registry.safe_inc") as mock_inc:
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
    recorded_counters = [call.args[0] for call in mock_inc.call_args_list]
    assert recorded_counters.count("agent_run_terminal_duplicate_suppressed") == 2
    assert "agent_run_terminal_duplicate_suppressed_failed" in recorded_counters
    assert "agent_run_terminal_duplicate_suppressed_cancelled" in recorded_counters


@pytest.mark.asyncio
async def test_failed_and_cancelled_terminal_results_are_claimable() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(
        _assignment(agent_run_id="run-1"), graph_thread_id="child-thread-1"
    )
    await registry.register(
        _assignment(agent_run_id="run-2"), graph_thread_id="child-thread-2"
    )
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-1")
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-2")

    failed = await registry.mark_failed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        safe_error="Subagent worker failed",
    )
    cancelled = await registry.mark_cancelled(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-2",
    )

    claim = await registry.claim_ready_handoffs(tenant_id=7, task_id=42)

    assert failed.result is not None
    assert failed.result.outcome == "failed"
    assert failed.result.summary == "Subagent run failed: Subagent worker failed"
    assert cancelled.result is not None
    assert cancelled.result.outcome == "cancelled"
    assert claim is not None
    assert claim.agent_run_ids == ("run-1", "run-2")
    assert [result.outcome for result in claim.results] == ["failed", "cancelled"]


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


@pytest.mark.asyncio
async def test_concurrent_claims_do_not_receive_the_same_result() -> None:
    registry = ProcessLocalAgentRunRegistry()
    for agent_run_id in ("run-1", "run-2"):
        await registry.register(
            _assignment(agent_run_id=agent_run_id),
            graph_thread_id=f"child-{agent_run_id}",
        )
        await registry.mark_completed(
            tenant_id=7,
            task_id=42,
            agent_run_id=agent_run_id,
            result=_result(agent_run_id),
        )

    first_attempt = registry.claim_ready_handoffs(tenant_id=7, task_id=42)
    second_attempt = registry.claim_ready_handoffs(tenant_id=7, task_id=42)

    with patch("backend.services.agent_runs.registry.safe_inc") as mock_inc, patch(
        "backend.services.agent_runs.registry.safe_gauge"
    ) as mock_gauge:
        claims = [
            claim
            for claim in await asyncio.gather(first_attempt, second_attempt)
            if claim is not None
        ]

    claimed_run_ids = [
        agent_run_id for claim in claims for agent_run_id in claim.agent_run_ids
    ]
    assert sorted(claimed_run_ids) == ["run-1", "run-2"]
    assert len(claimed_run_ids) == len(set(claimed_run_ids))
    recorded_counters = [call.args[0] for call in mock_inc.call_args_list]
    assert "agent_run_handoff_claim_created" in recorded_counters
    assert "agent_run_handoff_duplicate_claim_suppressed" in recorded_counters
    recorded_gauges = {
        call.args[0]: call.args[1] for call in mock_gauge.call_args_list
    }
    assert recorded_gauges["agent_run_handoff_claim_batch_size"] == 2
    assert recorded_gauges["agent_run_handoff_duplicate_claim_suppressed_count"] == 2


@pytest.mark.asyncio
async def test_acknowledged_handoffs_cannot_be_claimed_again() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(), graph_thread_id="child-thread-1")
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result("run-1"),
    )

    claim = await registry.claim_ready_handoffs(tenant_id=7, task_id=42)
    assert claim is not None
    await registry.acknowledge_handoffs(claim.claim_id)

    assert await registry.claim_ready_handoffs(tenant_id=7, task_id=42) is None
    entry = await registry.get(tenant_id=7, task_id=42, agent_run_id="run-1")
    assert entry is not None
    assert entry.result_consumed is True
    assert entry.result_claim_id is None


@pytest.mark.asyncio
async def test_released_handoffs_become_claimable_again() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(), graph_thread_id="child-thread-1")
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result("run-1"),
    )

    claim = await registry.claim_ready_handoffs(tenant_id=7, task_id=42)
    assert claim is not None
    assert await registry.claim_ready_handoffs(tenant_id=7, task_id=42) is None

    await registry.release_handoffs(claim.claim_id)

    retried = await registry.claim_ready_handoffs(tenant_id=7, task_id=42)
    assert retried is not None
    assert retried.agent_run_ids == ("run-1",)


@pytest.mark.asyncio
async def test_claim_result_and_active_snapshot_are_task_scoped() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(
        _assignment(agent_run_id="run-1"), graph_thread_id="child-1"
    )
    await registry.register(
        _assignment(agent_run_id="run-2", task_id=43),
        graph_thread_id="child-2",
    )
    await registry.register(
        _assignment(agent_run_id="run-3"), graph_thread_id="child-3"
    )
    await registry.register(
        _assignment(agent_run_id="run-4", task_id=43),
        graph_thread_id="child-4",
    )
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result("run-1"),
    )
    await registry.mark_completed(
        tenant_id=7,
        task_id=43,
        agent_run_id="run-2",
        result=_result("run-2"),
    )
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-3")
    await registry.mark_running(tenant_id=7, task_id=43, agent_run_id="run-4")

    claim = await registry.claim_ready_handoffs(tenant_id=7, task_id=42)

    assert claim is not None
    assert claim.agent_run_ids == ("run-1",)
    assert [entry.agent_run_id for entry in claim.active_runs] == ["run-3"]


@pytest.mark.asyncio
async def test_cancelled_processing_can_release_claimed_handoffs() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(), graph_thread_id="child-thread-1")
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result("run-1"),
    )
    claim_started = asyncio.Event()

    async def _process_claim() -> None:
        claim = await registry.claim_ready_handoffs(tenant_id=7, task_id=42)
        assert claim is not None
        claim_started.set()
        try:
            await asyncio.sleep(60)
        finally:
            await registry.release_handoffs(claim.claim_id)

    task = asyncio.create_task(_process_claim())
    await claim_started.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    retried = await registry.claim_ready_handoffs(tenant_id=7, task_id=42)
    assert retried is not None
    assert retried.agent_run_ids == ("run-1",)


@pytest.mark.asyncio
async def test_state_change_notification_has_no_lost_wakeup() -> None:
    registry = ProcessLocalAgentRunRegistry()
    before_register = await registry.state_version()
    await registry.register(_assignment(), graph_thread_id="child-thread-1")

    observed_after_register = await asyncio.wait_for(
        registry.wait_for_state_change(after_version=before_register),
        timeout=1,
    )
    assert observed_after_register > before_register

    before_completion = await registry.state_version()
    waiter = asyncio.create_task(
        registry.wait_for_state_change(after_version=before_completion)
    )
    await asyncio.sleep(0)
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result("run-1"),
    )

    observed_after_completion = await asyncio.wait_for(waiter, timeout=1)
    assert observed_after_completion > before_completion


@pytest.mark.asyncio
async def test_scoped_handoff_wait_ignores_unrelated_task_notifications() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(
        _assignment(agent_run_id="run-active"),
        graph_thread_id="child-active",
    )
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-active")

    before_wait = await registry.state_version()
    waiter = asyncio.create_task(
        registry.wait_for_ready_handoffs_or_inactive(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            after_version=before_wait,
        )
    )
    await asyncio.sleep(0)

    await registry.register(
        _assignment(agent_run_id="run-other", task_id=43),
        graph_thread_id="child-other",
    )
    await registry.mark_completed(
        tenant_id=7,
        task_id=43,
        agent_run_id="run-other",
        result=_result("run-other"),
    )
    await asyncio.sleep(0)

    assert waiter.done() is False

    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-active",
        result=_result("run-active"),
    )

    assert await asyncio.wait_for(waiter, timeout=1) == "ready"


@pytest.mark.asyncio
async def test_scoped_handoff_wait_exits_when_no_active_runs_remain() -> None:
    registry = ProcessLocalAgentRunRegistry()

    assert (
        await registry.wait_for_ready_handoffs_or_inactive(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            after_version=await registry.state_version(),
        )
        == "inactive"
    )


def test_registry_module_has_no_database_or_orm_dependency() -> None:
    source_path = Path(__file__).resolve().parents[3] / "services/agent_runs/registry.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    assert all(
        module != "backend.database"
        and not module.startswith("backend.database.")
        and module != "sqlalchemy"
        and not module.startswith("sqlalchemy.")
        for module in imported_modules
    )
