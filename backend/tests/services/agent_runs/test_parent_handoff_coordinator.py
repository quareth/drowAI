"""Tests for serialized parent processing of completed subagent handoffs."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import patch

import pytest

from agent.subagents.registry import get_subagent_registry
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
    AgentRuntimeIdentity,
)
from backend.services.agent_runs.parent_handoff_coordinator import (
    ParentFollowupDelegation,
    ParentHandoffCoordinator as _ParentHandoffCoordinator,
    ParentHandoffGuardPool,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.result_projection import (
    ACTIVE_AGENT_RUNS_KEY,
    COMPLETED_AGENT_RESULTS_KEY,
)
from backend.services.langgraph_chat.contracts import LangGraphChatResult


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
    agent_run_id: str = "run-1",
    conversation_id: str = "conversation-1",
    parent_turn_id: str = "turn-1",
    objective: str = "Map open services on the approved target.",
) -> AgentAssignment:
    return AgentAssignment(
        assignment_id=f"assign-{agent_run_id}",
        agent_run_id=agent_run_id,
        agent_id="pathfinder",
        agent_kind="recon",
        task_id=42,
        tenant_id=7,
        conversation_id=conversation_id,
        parent_turn_id=parent_turn_id,
        parent_graph_thread_id="parent-thread-1",
        objective=objective,
        targets=["10.0.0.10"],
        suggested_capabilities=["host_discovery", "port_scan"],
        scope_summary="Approved internal test host only.",
        relevant_context={"ticket": "ENG-123"},
        runtime_identity=_runtime_identity(),
    )


def _result(
    agent_run_id: str = "run-1",
    *,
    outcome: str = "completed",
) -> AgentResult:
    return AgentResult(
        agent_run_id=agent_run_id,
        agent_id="pathfinder",
        agent_kind="recon",
        outcome=outcome,  # type: ignore[arg-type]
        summary=f"{agent_run_id} found exposed HTTP.",
        key_findings=["HTTP exposed on 80"],
        evidence_refs=[{"kind": "artifact", "path": "/workspace/artifacts/nmap.xml"}],
        tools_used=["nmap"],
        limitations=[],
        recommended_next_steps=["Review HTTP headers"],
        final_checkpoint_id=f"checkpoint-{agent_run_id}",
    )


async def _register_completed(
    registry: ProcessLocalAgentRunRegistry,
    agent_run_id: str,
    *,
    conversation_id: str = "conversation-1",
    parent_turn_id: str = "turn-1",
) -> None:
    await registry.register(
        _assignment(
            agent_run_id=agent_run_id,
            conversation_id=conversation_id,
            parent_turn_id=parent_turn_id,
        ),
        graph_thread_id=f"child-{agent_run_id}",
    )
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id=agent_run_id,
        result=_result(agent_run_id),
    )


def _completed_result(status: str = "completed") -> LangGraphChatResult:
    return LangGraphChatResult(
        final_text="parent done",
        conversation_id="conversation-1",
        metadata={"status": status},
    )


def _coordinator(
    *,
    registry: ProcessLocalAgentRunRegistry,
    guard_pool: ParentHandoffGuardPool | None = None,
    **kwargs: Any,
) -> _ParentHandoffCoordinator:
    """Build an isolated coordinator unless a shared guard pool is explicit."""
    return _ParentHandoffCoordinator(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        guard_pool=guard_pool or ParentHandoffGuardPool(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_guard_pool_serializes_shared_key_across_coordinators() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    guard_pool = ParentHandoffGuardPool()
    first_coordinator = _coordinator(registry=registry, guard_pool=guard_pool)
    second_coordinator = _coordinator(registry=registry, guard_pool=guard_pool)
    entered = asyncio.Event()
    release = asyncio.Event()
    parent_calls = 0

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        nonlocal parent_calls
        parent_calls += 1
        entered.set()
        await release.wait()
        return _completed_result()

    first = asyncio.create_task(
        first_coordinator.process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata={},
            run_parent_continuation=_run_parent,
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        second_coordinator.process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata={},
            run_parent_continuation=_run_parent,
        )
    )
    await asyncio.sleep(0)

    assert parent_calls == 1
    release.set()
    first_outcome, second_outcome = await asyncio.gather(first, second)

    assert first_outcome is not None
    assert second_outcome is None
    assert parent_calls == 1
    assert guard_pool._entries == {}


@pytest.mark.asyncio
async def test_guard_pool_cleans_cancelled_waiter_and_erroring_holder() -> None:
    guard_pool = ParentHandoffGuardPool()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _holder() -> None:
        async with guard_pool.acquire(tenant_id=7, task_id=42):
            entered.set()
            await release.wait()
            raise RuntimeError("expected")

    async def _waiter() -> None:
        async with guard_pool.acquire(tenant_id=7, task_id=42):
            return None

    holder = asyncio.create_task(_holder())
    await entered.wait()
    waiter = asyncio.create_task(_waiter())
    await asyncio.sleep(0)
    waiter.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiter
    release.set()
    with pytest.raises(RuntimeError, match="expected"):
        await holder

    assert guard_pool._entries == {}


@pytest.mark.asyncio
async def test_coordinator_claims_ready_results_together_and_acknowledges() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    await _register_completed(registry, "run-2")
    await registry.register(
        _assignment(agent_run_id="run-active", objective="Keep checking headers."),
        graph_thread_id="child-active",
    )
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-active")
    metadata: dict[str, Any] = {}
    observed: dict[str, Any] = {}

    async def _run_parent(
        handoff: Any,
        active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        observed["handoff_ids"] = handoff.agent_run_ids
        observed["active_ids"] = tuple(run["agent_run_id"] for run in active_runs)
        return _completed_result()

    outcome = await _coordinator(registry=registry).process_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        metadata=metadata,
        run_parent_continuation=_run_parent,
    )

    assert outcome is not None
    assert observed["handoff_ids"] == ("run-1", "run-2")
    assert observed["active_ids"] == ("run-active",)
    assert [item["agent_run_id"] for item in metadata[COMPLETED_AGENT_RESULTS_KEY]] == [
        "run-1",
        "run-2",
    ]
    assert metadata[ACTIVE_AGENT_RUNS_KEY][0]["agent_run_id"] == "run-active"
    entries = await registry.list_task_runs(tenant_id=7, task_id=42)
    consumed = {
        entry.agent_run_id: entry.result_consumed
        for entry in entries
        if entry.agent_run_id in {"run-1", "run-2"}
    }
    assert consumed == {"run-1": True, "run-2": True}


@pytest.mark.asyncio
async def test_coordinator_records_bounded_handoff_observability() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    await registry.register(
        _assignment(agent_run_id="run-active", objective="Keep checking headers."),
        graph_thread_id="child-active",
    )
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-active")

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        return _completed_result()

    with patch(
        "backend.services.agent_runs.parent_handoff_coordinator.safe_inc"
    ) as mock_inc, patch(
        "backend.services.agent_runs.parent_handoff_coordinator.safe_gauge"
    ) as mock_gauge:
        outcome = await _coordinator(
            registry=registry
        ).process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata={},
            run_parent_continuation=_run_parent,
        )

    assert outcome is not None
    recorded_counters = [call.args[0] for call in mock_inc.call_args_list]
    recorded_gauges = {
        call.args[0]: call.args[1] for call in mock_gauge.call_args_list
    }
    assert "post_action_reasoning_handoff_claim_observed" in recorded_counters
    assert "post_action_reasoning_parent_finalization_count" in recorded_counters
    assert recorded_gauges["post_action_reasoning_handoff_batch_size"] == 1
    assert recorded_gauges["post_action_reasoning_active_run_count"] == 1


@pytest.mark.asyncio
async def test_coordinator_emits_one_parent_progress_block_per_handoff_batch() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    await _register_completed(registry, "run-2")
    await registry.register(
        _assignment(agent_run_id="run-active", objective="Keep checking headers."),
        graph_thread_id="child-active",
    )
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-active")
    published: list[dict[str, Any]] = []

    async def _publish_progress(
        task_id: int,
        events: tuple[dict[str, Any], ...],
    ) -> None:
        assert task_id == 42
        published.extend(events)

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        return _completed_result()

    outcome = await _coordinator(
        registry=registry,
        parent_progress_publisher=_publish_progress,
    ).process_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        metadata={"turn_sequence": 9},
        run_parent_continuation=_run_parent,
    )

    assert outcome is not None
    assert [event["type"] for event in published] == [
        "reasoning_start",
        "reasoning_delta",
        "reasoning_section_end",
    ]
    deltas = [event for event in published if event["type"] == "reasoning_delta"]
    assert len(deltas) == 1
    delta = deltas[0]
    metadata = delta["metadata"]
    assert metadata["producer_type"] == "main_agent"
    assert "agent_run_id" not in metadata
    assert metadata["progress_kind"] == "parent_handoff"
    assert metadata["reasoning_section_id"].startswith("turn-1:parent-handoff:")
    assert metadata["turn_sequence"] == 9
    assert metadata["parent_progress"]["completed_assignment_count"] == 2
    assert metadata["parent_progress"]["completed_agent_run_ids"] == ["run-1", "run-2"]
    assert metadata["parent_progress"]["active_assignment_count"] == 1
    assert metadata["parent_progress"]["active_agent_run_ids"] == ["run-active"]
    assert "2 assignments returned a handoff" in delta["content"]
    assert "1 relevant assignment still active" in delta["content"]


@pytest.mark.asyncio
async def test_parent_progress_section_id_is_stable_across_claim_retry() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    published: list[dict[str, Any]] = []

    async def _publish_progress(
        _task_id: int,
        events: tuple[dict[str, Any], ...],
    ) -> None:
        published.extend(events)

    async def _fail_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        raise RuntimeError("retryable parent failure")

    coordinator = _coordinator(
        registry=registry,
        parent_progress_publisher=_publish_progress,
    )
    with pytest.raises(RuntimeError, match="retryable parent failure"):
        await coordinator.process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata={},
            run_parent_continuation=_fail_parent,
        )

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        return _completed_result()

    outcome = await coordinator.process_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        metadata={},
        run_parent_continuation=_run_parent,
    )

    assert outcome is not None
    section_ids = {
        event["metadata"]["reasoning_section_id"]
        for event in published
        if event["type"] == "reasoning_delta"
    }
    assert len(section_ids) == 1


@pytest.mark.asyncio
async def test_coordinator_serializes_parent_cycles_for_task_turn() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    coordinator = _coordinator(registry=registry)
    entered = asyncio.Event()
    release = asyncio.Event()
    parent_calls = 0

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        nonlocal parent_calls
        parent_calls += 1
        entered.set()
        await release.wait()
        return _completed_result()

    first = asyncio.create_task(
        coordinator.process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata={},
            run_parent_continuation=_run_parent,
        )
    )
    await entered.wait()
    second = asyncio.create_task(
        coordinator.process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata={},
            run_parent_continuation=_run_parent,
        )
    )
    await asyncio.sleep(0)

    assert parent_calls == 1
    release.set()
    first_outcome, second_outcome = await asyncio.gather(first, second)
    assert first_outcome is not None
    assert second_outcome is None
    assert parent_calls == 1


@pytest.mark.asyncio
async def test_coordinator_serializes_parent_cycles_for_task_across_turns() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(
        registry,
        "run-1",
        conversation_id="conversation-a",
        parent_turn_id="turn-a",
    )
    await _register_completed(
        registry,
        "run-2",
        conversation_id="conversation-b",
        parent_turn_id="turn-b",
    )
    coordinator = _coordinator(registry=registry)
    entered_first = asyncio.Event()
    release_first = asyncio.Event()
    entered: list[str] = []

    async def _run_first(
        handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        entered.append(handoff.agent_run_ids[0])
        entered_first.set()
        await release_first.wait()
        return _completed_result()

    async def _run_second(
        handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        entered.append(handoff.agent_run_ids[0])
        return _completed_result()

    first = asyncio.create_task(
        coordinator.process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-a",
            parent_turn_id="turn-a",
            metadata={},
            run_parent_continuation=_run_first,
        )
    )
    await entered_first.wait()
    second = asyncio.create_task(
        coordinator.process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-b",
            parent_turn_id="turn-b",
            metadata={},
            run_parent_continuation=_run_second,
        )
    )
    await asyncio.sleep(0)

    assert entered == ["run-1"]
    release_first.set()
    first_outcome, second_outcome = await asyncio.gather(first, second)

    assert first_outcome is not None
    assert second_outcome is not None
    assert first_outcome.agent_run_ids == ("run-1",)
    assert second_outcome.agent_run_ids == ("run-2",)
    assert entered == ["run-1", "run-2"]


@pytest.mark.asyncio
async def test_handoffs_arriving_during_parent_cycle_remain_for_next_batch() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    coordinator = _coordinator(registry=registry)
    entered = asyncio.Event()
    release = asyncio.Event()
    observed_batches: list[tuple[str, ...]] = []

    async def _run_parent(
        handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        observed_batches.append(handoff.agent_run_ids)
        entered.set()
        await release.wait()
        return _completed_result()

    first = asyncio.create_task(
        coordinator.process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata={},
            run_parent_continuation=_run_parent,
        )
    )
    await entered.wait()
    await _register_completed(registry, "run-2")
    release.set()
    await first

    entries = {
        entry.agent_run_id: entry
        for entry in await registry.list_task_runs(tenant_id=7, task_id=42)
    }
    assert entries["run-1"].result_consumed is True
    assert entries["run-2"].result_consumed is False

    async def _run_second(
        handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        observed_batches.append(handoff.agent_run_ids)
        return _completed_result()

    second = await coordinator.process_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        metadata={},
        run_parent_continuation=_run_second,
    )

    assert second is not None
    assert observed_batches == [("run-1",), ("run-2",)]


@pytest.mark.asyncio
async def test_parent_failure_releases_claim_without_consuming_result() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        raise RuntimeError("parent continuation failed")

    with patch(
        "backend.services.agent_runs.parent_handoff_coordinator.safe_inc"
    ) as mock_inc:
        with pytest.raises(RuntimeError, match="parent continuation failed"):
            await _coordinator(registry=registry).process_ready_handoffs(
                tenant_id=7,
                task_id=42,
                conversation_id="conversation-1",
                parent_turn_id="turn-1",
                metadata={},
                run_parent_continuation=_run_parent,
            )

    retried = await registry.claim_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
    )
    assert retried is not None
    assert retried.agent_run_ids == ("run-1",)
    recorded_counters = [call.args[0] for call in mock_inc.call_args_list]
    assert "post_action_reasoning_claim_release_after_error" in recorded_counters


@pytest.mark.asyncio
async def test_cancelled_parent_result_releases_claim_for_retry() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        return _completed_result(status="cancelled")

    outcome = await _coordinator(registry=registry).process_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        metadata={},
        run_parent_continuation=_run_parent,
    )

    assert outcome is not None
    retried = await registry.claim_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
    )
    assert retried is not None
    assert retried.agent_run_ids == ("run-1",)


@pytest.mark.asyncio
async def test_request_cancellation_releases_claim_for_retry() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    entered = asyncio.Event()

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        entered.set()
        await asyncio.sleep(60)
        return _completed_result()

    task = asyncio.create_task(
        _coordinator(registry=registry).process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata={},
            run_parent_continuation=_run_parent,
        )
    )
    await entered.wait()

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    retried = await registry.claim_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
    )
    assert retried is not None
    assert retried.agent_run_ids == ("run-1",)


@pytest.mark.asyncio
async def test_delegate_subagent_control_launches_followup_and_continues_loop() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    observed: dict[str, Any] = {"parent_batches": []}

    async def _dispatch_followup(
        agent_handoff: Any,
        decision_id: str,
    ) -> ParentFollowupDelegation:
        observed["agent_handoff"] = dict(agent_handoff)
        observed["decision_id"] = decision_id
        assignment = _assignment(
            agent_run_id="run-followup",
            objective=agent_handoff["objective"],
        )
        await registry.register(assignment, graph_thread_id="child-followup")
        await registry.mark_running(
            tenant_id=7,
            task_id=42,
            agent_run_id="run-followup",
        )
        await registry.mark_completed(
            tenant_id=7,
            task_id=42,
            agent_run_id="run-followup",
            result=_result("run-followup"),
        )
        return ParentFollowupDelegation(
            agent_run_ids=("run-followup",),
            launched_agent_run_ids=("run-followup",),
        )

    async def _run_parent(
        handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        observed["parent_batches"].append(handoff.agent_run_ids)
        if handoff.agent_run_ids == ("run-1",):
            return LangGraphChatResult(
                final_text="delegating",
                conversation_id="conversation-1",
                metadata={
                    "router_outcome": {
                        "action": "delegate_subagent",
                        "candidate_id": "par-candidate-1",
                        "agent_handoff": {
                            "agent_handoff": "required",
                            "subagent": "pathfinder",
                            "objective": (
                                "Check the unresolved HTTPS service evidence."
                            ),
                        },
                    }
                },
            )
        return _completed_result()

    outcome = await _coordinator(registry=registry).process_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        metadata={},
        run_parent_continuation=_run_parent,
        dispatch_followup_delegation=_dispatch_followup,
    )

    assert outcome is not None
    assert observed["parent_batches"] == [("run-1",), ("run-followup",)]
    assert observed["agent_handoff"]["objective"] == (
        "Check the unresolved HTTPS service evidence."
    )
    assert observed["decision_id"] == "par-candidate-1"
    entries = {
        entry.agent_run_id: entry
        for entry in await registry.list_task_runs(tenant_id=7, task_id=42)
    }
    assert entries["run-1"].result_consumed is True
    assert entries["run-followup"].result_consumed is True


@pytest.mark.asyncio
async def test_parent_continuation_calls_direct_tool_after_child_then_finalizes() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    observed: dict[str, Any] = {}

    async def _run_parent(
        handoff: Any,
        active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        observed["handoff_ids"] = handoff.agent_run_ids
        observed["active_runs"] = active_runs
        return LangGraphChatResult(
            final_text="Parent finalized after direct tool continuation.",
            conversation_id="conversation-1",
            metadata={
                "status": "completed",
                "parent_graph_routes": [
                    "post_action_reasoning",
                    "decision_router",
                    "call_tool",
                    "post_action_reasoning",
                    "decision_router",
                    "finalize",
                ],
            },
        )

    outcome = await _coordinator(registry=registry).process_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        metadata={},
        run_parent_continuation=_run_parent,
    )

    assert outcome is not None
    assert outcome.result.final_text == "Parent finalized after direct tool continuation."
    assert observed["handoff_ids"] == ("run-1",)
    assert observed["active_runs"] == ()
    assert outcome.result.metadata["parent_graph_routes"] == [
        "post_action_reasoning",
        "decision_router",
        "call_tool",
        "post_action_reasoning",
        "decision_router",
        "finalize",
    ]
    entries = {
        entry.agent_run_id: entry
        for entry in await registry.list_task_runs(tenant_id=7, task_id=42)
    }
    assert entries["run-1"].result_consumed is True


@pytest.mark.asyncio
async def test_parent_continuation_reflects_after_blocked_child_result() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(agent_run_id="run-blocked"), graph_thread_id="child")
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-blocked",
        result=_result("run-blocked", outcome="blocked"),
    )
    observed: dict[str, Any] = {}

    async def _run_parent(
        handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        observed["outcomes"] = tuple(result["outcome"] for result in handoff.results)
        return LangGraphChatResult(
            final_text="Parent finalized after reflection recovery.",
            conversation_id="conversation-1",
            metadata={
                "status": "completed",
                "parent_graph_routes": [
                    "post_action_reasoning",
                    "decision_router",
                    "reflect",
                    "decision_router",
                    "finalize",
                ],
            },
        )

    outcome = await _coordinator(registry=registry).process_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        metadata={},
        run_parent_continuation=_run_parent,
    )

    assert outcome is not None
    assert observed["outcomes"] == ("blocked",)
    assert outcome.result.metadata["parent_graph_routes"] == [
        "post_action_reasoning",
        "decision_router",
        "reflect",
        "decision_router",
        "finalize",
    ]
    entries = {
        entry.agent_run_id: entry
        for entry in await registry.list_task_runs(tenant_id=7, task_id=42)
    }
    assert entries["run-blocked"].result_consumed is True


@pytest.mark.asyncio
async def test_wait_for_subagents_blocks_until_scoped_handoff_is_ready() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    await registry.register(
        _assignment(agent_run_id="run-active", objective="Check service headers."),
        graph_thread_id="child-active",
    )
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-active")
    observed: dict[str, Any] = {"parent_batches": []}
    metadata: dict[str, Any] = {}

    async def _run_parent(
        handoff: Any,
        active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        observed["parent_batches"].append(handoff.agent_run_ids)
        if handoff.agent_run_ids == ("run-1",):
            assert [run["agent_run_id"] for run in active_runs] == ["run-active"]
            return LangGraphChatResult(
                final_text="waiting",
                conversation_id="conversation-1",
                metadata={
                    "router_outcome": {
                        "action": "wait_for_subagents",
                        "candidate_id": "par-wait-1",
                    }
                },
            )
        return _completed_result()

    processing = asyncio.create_task(
        _coordinator(registry=registry).process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata=metadata,
            run_parent_continuation=_run_parent,
        )
    )
    await asyncio.sleep(0)

    await registry.register(
        _assignment(agent_run_id="run-other", objective="Other task.").model_copy(
            update={
                "assignment_id": "assign-run-other",
                "task_id": 43,
                "runtime_identity": _runtime_identity(task_id=43),
            }
        ),
        graph_thread_id="child-other",
    )
    await registry.mark_completed(
        tenant_id=7,
        task_id=43,
        agent_run_id="run-other",
        result=_result("run-other"),
    )
    await asyncio.sleep(0)

    assert observed["parent_batches"] == [("run-1",)]
    assert processing.done() is False

    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-active",
        result=_result("run-active"),
    )

    outcome = await asyncio.wait_for(processing, timeout=1)

    assert outcome is not None
    assert observed["parent_batches"] == [("run-1",), ("run-active",)]
    assert metadata["last_parent_control_outcome"] == {
        "action": "wait_for_subagents",
        "decision_id": "par-wait-1",
        "completed_agent_run_ids": ["run-1"],
        "active_agent_run_ids": ["run-active"],
    }
    entries = {
        entry.agent_run_id: entry
        for entry in await registry.list_task_runs(tenant_id=7, task_id=42)
    }
    assert entries["run-1"].result_consumed is True
    assert entries["run-active"].result_consumed is True


@pytest.mark.asyncio
async def test_cancellation_during_wait_emits_no_final_parent_result() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    await registry.register(
        _assignment(agent_run_id="run-active", objective="Keep running."),
        graph_thread_id="child-active",
    )
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-active")
    parent_entered = asyncio.Event()
    final_outcomes: list[ParentHandoffOutcome | None] = []

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        parent_entered.set()
        return LangGraphChatResult(
            final_text="waiting is not final",
            conversation_id="conversation-1",
            metadata={
                "router_outcome": {
                    "action": "wait_for_subagents",
                    "candidate_id": "par-wait-cancel",
                }
            },
        )

    async def _process() -> None:
        final_outcomes.append(
            await _coordinator(registry=registry).process_ready_handoffs(
                tenant_id=7,
                task_id=42,
                conversation_id="conversation-1",
                parent_turn_id="turn-1",
                metadata={},
                run_parent_continuation=_run_parent,
            )
        )

    processing = asyncio.create_task(_process())
    await parent_entered.wait()
    await asyncio.sleep(0)

    assert processing.done() is False
    processing.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await processing

    assert final_outcomes == []
    entries = {
        entry.agent_run_id: entry
        for entry in await registry.list_task_runs(tenant_id=7, task_id=42)
    }
    assert entries["run-1"].result_consumed is True
    assert entries["run-active"].result_consumed is False


@pytest.mark.asyncio
async def test_wait_for_subagents_requires_active_runs_in_claim_snapshot() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        return LangGraphChatResult(
            final_text="waiting",
            conversation_id="conversation-1",
            metadata={"router_outcome": {"action": "wait_for_subagents"}},
        )

    with pytest.raises(RuntimeError, match="no active subagent runs"):
        await _coordinator(registry=registry).process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata={},
            run_parent_continuation=_run_parent,
        )

    retried = await registry.claim_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
    )
    assert retried is not None
    assert retried.agent_run_ids == ("run-1",)


@pytest.mark.asyncio
async def test_wait_for_subagents_timeout_releases_no_processed_claim() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")
    await registry.register(
        _assignment(agent_run_id="run-active", objective="Keep running."),
        graph_thread_id="child-active",
    )
    await registry.mark_running(tenant_id=7, task_id=42, agent_run_id="run-active")

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        return LangGraphChatResult(
            final_text="waiting",
            conversation_id="conversation-1",
            metadata={"router_outcome": {"action": "wait_for_subagents"}},
        )

    with pytest.raises(asyncio.TimeoutError):
        await _coordinator(registry=registry).process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata={},
            run_parent_continuation=_run_parent,
            wait_timeout_seconds=0.001,
        )

    entries = {
        entry.agent_run_id: entry
        for entry in await registry.list_task_runs(tenant_id=7, task_id=42)
    }
    assert entries["run-1"].result_consumed is True
    assert entries["run-active"].result_consumed is False


@pytest.mark.asyncio
async def test_invalid_delegate_subagent_control_releases_claim() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await _register_completed(registry, "run-1")

    async def _run_parent(
        _handoff: Any,
        _active_runs: tuple[dict[str, Any], ...],
    ) -> LangGraphChatResult:
        return LangGraphChatResult(
            final_text="invalid",
            conversation_id="conversation-1",
            metadata={"router_outcome": {"action": "delegate_subagent"}},
        )

    with pytest.raises(RuntimeError, match="missing agent_handoff"):
        await _coordinator(registry=registry).process_ready_handoffs(
            tenant_id=7,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
            metadata={},
            run_parent_continuation=_run_parent,
            dispatch_followup_delegation=lambda _handoff, _decision_id: (
                _completed_result()  # type: ignore[return-value]
            ),
        )

    retried = await registry.claim_ready_handoffs(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
    )
    assert retried is not None
    assert retried.agent_run_ids == ("run-1",)
