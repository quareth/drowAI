"""Direct tests for subagent dispatch settlement.

These tests lock the extracted settlement collaborator's result translation,
registry-backed recovery, cleanup ordering, and cache lookup independently
from the dispatch facade.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from backend.services.agent_runs import dispatch_settlement as dispatch_settlement_module
from backend.services.agent_runs.completion import AgentRunCompletion
from backend.services.agent_runs.dispatch_settlement import DispatchSettlement
from backend.services.agent_runs.launcher import (
    SubagentRunCancelled,
    SubagentRunFailed,
    SubagentRunPaused,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.result_projection import CompletedAgentResultHandoff
from backend.services.langgraph_chat.execution.graph_executor import (
    GraphExecutionResult,
)
from backend.tests.services.agent_runs.test_dispatch_service import (
    TASK_ID,
    TENANT_ID,
    _assignment,
    _completion,
    _final_state,
    _plan,
    _result,
    _runtime_config,
)


class _TerminalAwaitable:
    def __init__(self, result: Any, *, raise_result: bool = False) -> None:
        self._result = result
        self._raise_result = raise_result
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def done(self) -> bool:
        return False

    def __await__(self) -> Iterator[Any]:
        if False:
            yield None
        if self._raise_result:
            raise self._result
        return self._result


class _ShellCleanupService:
    def __init__(self) -> None:
        self.close_calls: list[dict[str, Any]] = []

    async def close_owner_sessions(self, **kwargs: Any) -> None:
        self.close_calls.append(dict(kwargs))


class _FailingShellCleanupService:
    async def close_owner_sessions(self, **_kwargs: Any) -> None:
        raise RuntimeError("cleanup failed")


@pytest.mark.asyncio
async def test_require_child_task_conversion_and_invalid_values() -> None:
    registry = ProcessLocalAgentRunRegistry()
    settlement = DispatchSettlement(registry=registry)
    item = _plan("pathfinder")[0]
    completion = _completion(item.assignment, graph_thread_id=item.graph_thread_id)
    agent_result = _result(item.assignment)

    assert (
        await settlement.require_child_task(
            _TerminalAwaitable(completion),
            assignment=item.assignment,
            graph_thread_id=item.graph_thread_id,
        )
    ) is completion

    converted = await settlement.require_child_task(
        _TerminalAwaitable(agent_result),
        assignment=item.assignment,
        graph_thread_id=item.graph_thread_id,
    )

    assert converted.result is agent_result
    assert converted.graph_thread_id == item.graph_thread_id
    assert converted.usage_records == ()

    with pytest.raises(RuntimeError, match="did not return an awaitable"):
        await settlement.require_child_task(
            object(),
            assignment=item.assignment,
            graph_thread_id=item.graph_thread_id,
        )

    with pytest.raises(RuntimeError, match="invalid terminal result"):
        await settlement.require_child_task(
            _TerminalAwaitable(object()),
            assignment=item.assignment,
            graph_thread_id=item.graph_thread_id,
        )


@pytest.mark.asyncio
async def test_require_child_task_result_preserves_exception_identity() -> None:
    settlement = DispatchSettlement(registry=ProcessLocalAgentRunRegistry())
    item = _plan("pathfinder")[0]
    exc = RuntimeError("child task exploded")

    result = await settlement.require_child_task_result(
        _TerminalAwaitable(exc, raise_result=True),
        assignment=item.assignment,
        graph_thread_id=item.graph_thread_id,
    )

    assert result is exc


@pytest.mark.asyncio
async def test_terminal_exception_recovers_failed_and_cancelled_usage() -> None:
    registry = ProcessLocalAgentRunRegistry()
    settlement = DispatchSettlement(registry=registry)
    failed_item, cancelled_item = _plan("pathfinder", "cartographer")
    await registry.register(
        failed_item.assignment,
        graph_thread_id=failed_item.graph_thread_id,
    )
    await registry.mark_failed(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        agent_run_id=failed_item.assignment.agent_run_id,
        safe_error="Subagent worker failed",
    )
    await registry.register(
        cancelled_item.assignment,
        graph_thread_id=cancelled_item.graph_thread_id,
    )
    await registry.mark_cancelled(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        agent_run_id=cancelled_item.assignment.agent_run_id,
    )

    failed = await settlement.completion_for_terminal_exception(
        SubagentRunFailed(
            "Subagent graph completed without a valid terminal result",
            GraphExecutionResult(
                final_state=_final_state(failed_item.assignment.agent_run_id)
            ),
        ),
        item=failed_item,
    )
    cancelled = await settlement.completion_for_terminal_exception(
        SubagentRunCancelled(
            execution_result=GraphExecutionResult(
                final_state=_final_state(cancelled_item.assignment.agent_run_id)
            )
        ),
        item=cancelled_item,
    )

    assert failed is not None
    assert failed.result.outcome == "failed"
    assert failed.usage_records[0]["agent_run_id"] == "run-1"
    assert cancelled is not None
    assert cancelled.result.outcome == "cancelled"
    assert cancelled.usage_records[0]["agent_run_id"] == "run-2"
    assert (
        await settlement.completion_for_terminal_exception(
            SubagentRunPaused(
                execution_result=GraphExecutionResult(final_state=_final_state("run-1"))
            ),
            item=failed_item,
        )
    ) is None
    assert (
        await settlement.completion_for_terminal_exception(
            RuntimeError("not terminal"),
            item=failed_item,
        )
    ) is None


@pytest.mark.asyncio
async def test_settle_child_result_returns_typed_batch_facts() -> None:
    registry = ProcessLocalAgentRunRegistry()
    settlement = DispatchSettlement(registry=registry)
    completed_item, paused_item, failed_item, unexpected_item = _plan(
        "pathfinder",
        "cartographer",
        "scribe",
        "auditor",
    )
    completion = _completion(
        completed_item.assignment,
        graph_thread_id=completed_item.graph_thread_id,
    )
    await registry.register(
        failed_item.assignment,
        graph_thread_id=failed_item.graph_thread_id,
    )
    await registry.mark_failed(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        agent_run_id=failed_item.assignment.agent_run_id,
        safe_error="Subagent worker failed",
    )

    completed = await settlement.settle_child_result(
        completion,
        item=completed_item,
        task_id=TASK_ID,
        turn_index=5,
    )
    paused = await settlement.settle_child_result(
        SubagentRunPaused(
            execution_result=GraphExecutionResult(
                final_state=_final_state(paused_item.assignment.agent_run_id)
            )
        ),
        item=paused_item,
        task_id=TASK_ID,
        turn_index=5,
    )
    failed = await settlement.settle_child_result(
        SubagentRunFailed(
            "Subagent graph completed without a valid terminal result",
            GraphExecutionResult(
                final_state=_final_state(failed_item.assignment.agent_run_id)
            ),
        ),
        item=failed_item,
        task_id=TASK_ID,
        turn_index=5,
    )
    unexpected = await settlement.settle_child_result(
        RuntimeError("child task exploded"),
        item=unexpected_item,
        task_id=TASK_ID,
        turn_index=5,
    )

    assert completed.completion is completion
    assert paused.paused is True
    assert failed.completion is not None
    assert failed.completion.result.outcome == "failed"
    assert failed.completion.usage_records[0]["agent_run_id"] == "run-3"
    assert unexpected.stop is not None
    assert unexpected.stop.status == "failed"
    assert unexpected.stop.invocation is unexpected_item


@pytest.mark.asyncio
async def test_settle_child_result_closes_only_terminal_subagent_owner_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_service = _ShellCleanupService()
    monkeypatch.setattr(
        dispatch_settlement_module,
        "get_shell_session_service",
        lambda: shell_service,
    )
    registry = ProcessLocalAgentRunRegistry()
    settlement = DispatchSettlement(registry=registry)
    completed_item, paused_item, failed_item, cancelled_item = _plan(
        "pathfinder",
        "cartographer",
        "scribe",
        "auditor",
    )
    completion = _completion(
        completed_item.assignment,
        graph_thread_id=completed_item.graph_thread_id,
    )
    await registry.register(
        failed_item.assignment,
        graph_thread_id=failed_item.graph_thread_id,
    )
    await registry.mark_failed(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        agent_run_id=failed_item.assignment.agent_run_id,
        safe_error="Subagent worker failed",
    )

    completed = await settlement.settle_child_result(
        completion,
        item=completed_item,
        task_id=TASK_ID,
        turn_index=5,
    )
    paused = await settlement.settle_child_result(
        SubagentRunPaused(
            execution_result=GraphExecutionResult(
                final_state=_final_state(paused_item.assignment.agent_run_id)
            )
        ),
        item=paused_item,
        task_id=TASK_ID,
        turn_index=5,
    )
    failed = await settlement.settle_child_result(
        SubagentRunFailed(
            "Subagent graph completed without a valid terminal result",
            GraphExecutionResult(
                final_state=_final_state(failed_item.assignment.agent_run_id)
            ),
        ),
        item=failed_item,
        task_id=TASK_ID,
        turn_index=5,
    )
    cancelled = await settlement.settle_child_result(
        asyncio.CancelledError(),
        item=cancelled_item,
        task_id=TASK_ID,
        turn_index=5,
    )

    assert completed.completion is completion
    assert paused.paused is True
    assert failed.completion is not None
    assert cancelled.stop is not None
    assert cancelled.stop.status == "cancelled"
    assert shell_service.close_calls == [
        {
            "tenant_id": TENANT_ID,
            "task_id": TASK_ID,
            "execution_owner_id": "subagent:run-1",
        },
        {
            "tenant_id": TENANT_ID,
            "task_id": TASK_ID,
            "execution_owner_id": "subagent:run-3",
        },
        {
            "tenant_id": TENANT_ID,
            "task_id": TASK_ID,
            "execution_owner_id": "subagent:run-4",
        },
    ]


@pytest.mark.asyncio
async def test_settle_child_result_logs_and_preserves_result_on_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        dispatch_settlement_module,
        "get_shell_session_service",
        lambda: _FailingShellCleanupService(),
    )
    settlement = DispatchSettlement(registry=ProcessLocalAgentRunRegistry())
    item = _plan("pathfinder")[0]
    completion = _completion(item.assignment, graph_thread_id=item.graph_thread_id)

    settled = await settlement.settle_child_result(
        completion,
        item=item,
        task_id=TASK_ID,
        turn_index=5,
    )

    assert settled.completion is completion
    assert "shell_session.subagent_owner_cleanup_failed" in caplog.text


def test_stop_for_child_exception_preserves_status_and_usage_mapping() -> None:
    settlement = DispatchSettlement(registry=ProcessLocalAgentRunRegistry())
    item = _plan("pathfinder")[0]
    final_state = _final_state(item.assignment.agent_run_id)

    paused = settlement.stop_for_child_exception(
        SubagentRunPaused(
            execution_result=GraphExecutionResult(final_state=final_state)
        ),
        item=item,
        task_id=TASK_ID,
        turn_index=5,
    )
    cancelled = settlement.stop_for_child_exception(
        SubagentRunCancelled(
            execution_result=GraphExecutionResult(final_state=final_state)
        ),
        item=item,
        task_id=TASK_ID,
        turn_index=5,
    )
    failed = settlement.stop_for_child_exception(
        SubagentRunFailed(
            "Subagent graph completed without a valid terminal result",
            GraphExecutionResult(final_state=final_state),
        ),
        item=item,
        task_id=TASK_ID,
        turn_index=5,
    )
    bare_cancel = settlement.stop_for_child_exception(
        asyncio.CancelledError(),
        item=item,
        task_id=TASK_ID,
        turn_index=5,
    )
    unexpected = settlement.stop_for_child_exception(
        RuntimeError("child task exploded"),
        item=item,
        task_id=TASK_ID,
        turn_index=5,
    )

    assert paused.status == "waiting_for_approval"
    assert cancelled.status == "cancelled"
    assert failed.status == "failed"
    assert bare_cancel.status == "cancelled"
    assert unexpected.status == "failed"
    assert paused.usage[0].metadata.agent_run_id == item.assignment.agent_run_id
    assert cancelled.usage[0].metadata.agent_run_id == item.assignment.agent_run_id
    assert failed.usage[0].metadata.agent_run_id == item.assignment.agent_run_id
    assert bare_cancel.usage == ()
    assert unexpected.usage == ()


@pytest.mark.asyncio
async def test_settle_launched_batch_on_failure_recovers_original_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_service = _ShellCleanupService()
    monkeypatch.setattr(
        dispatch_settlement_module,
        "get_shell_session_service",
        lambda: shell_service,
    )
    registry = ProcessLocalAgentRunRegistry()
    settlement = DispatchSettlement(registry=registry)
    first, second, third = _plan("pathfinder", "cartographer", "pathfinder")
    first_completion = _completion(
        first.assignment,
        graph_thread_id=first.graph_thread_id,
    )
    second_terminal = _TerminalAwaitable(
        SubagentRunFailed(
            "Subagent graph completed without a valid terminal result",
            GraphExecutionResult(
                final_state=_final_state(second.assignment.agent_run_id)
            ),
        ),
        raise_result=True,
    )
    third_terminal = _TerminalAwaitable(
        RuntimeError("terminal task crashed"),
        raise_result=True,
    )
    await registry.register(second.assignment, graph_thread_id=second.graph_thread_id)
    await registry.mark_failed(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        agent_run_id=second.assignment.agent_run_id,
        safe_error="Subagent worker failed",
    )
    await registry.register(third.assignment, graph_thread_id=third.graph_thread_id)
    await registry.mark_cancelled(
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        agent_run_id=third.assignment.agent_run_id,
    )

    completions = await settlement.settle_launched_batch_on_failure(
        [
            (first, _TerminalAwaitable(first_completion)),
            (second, second_terminal),
            (third, third_terminal),
        ]
    )

    assert [completion.result.agent_run_id for completion in completions] == [
        "run-1",
        "run-2",
        "run-3",
    ]
    assert completions[0] is first_completion
    assert completions[1].result.outcome == "failed"
    assert completions[1].usage_records[0]["agent_run_id"] == "run-2"
    assert completions[2].result.outcome == "cancelled"
    assert completions[2].usage_records == ()
    assert second_terminal.cancelled is True
    assert third_terminal.cancelled is True
    assert shell_service.close_calls == [
        {
            "tenant_id": TENANT_ID,
            "task_id": TASK_ID,
            "execution_owner_id": "subagent:run-1",
        },
        {
            "tenant_id": TENANT_ID,
            "task_id": TASK_ID,
            "execution_owner_id": "subagent:run-2",
        },
        {
            "tenant_id": TENANT_ID,
            "task_id": TASK_ID,
            "execution_owner_id": "subagent:run-3",
        },
    ]


def test_record_batch_completions_and_completed_entries_preserve_plan_order() -> None:
    settlement = DispatchSettlement(registry=ProcessLocalAgentRunRegistry())
    first, second, third = _plan("pathfinder", "cartographer", "pathfinder")
    completions: list[AgentRunCompletion | None] = [None, None, None]
    first_completion = _completion(
        first.assignment,
        graph_thread_id=first.graph_thread_id,
    )
    third_completion = _completion(
        third.assignment,
        graph_thread_id=third.graph_thread_id,
    )

    settlement.record_batch_completions(
        completions,
        batch=[third, first],
        batch_completions=(third_completion, first_completion),
    )

    assert settlement.completed_entries(completions) == (
        first_completion,
        third_completion,
    )
    assert completions[second.index] is None


@pytest.mark.asyncio
async def test_handoff_completions_use_cache_first_then_registry_fallback() -> None:
    registry = ProcessLocalAgentRunRegistry()
    settlement = DispatchSettlement(registry=registry)
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
    completions = await settlement.completions_for_handoff(
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
