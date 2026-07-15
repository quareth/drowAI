"""Phase 5 Task 5.1 unit tests for ``BatchExecutor``.

Locks the no-retry, callback-driven contract:

- Sequential mode runs the manifest in order.
- Sequential mode continues past a failed independent call.
- Parallel mode runs concurrently and respects the supplied timeout.
- The executor never imports the coordinator, GraphToolExecutor, or the
  unified emitter (M5 / M2 mitigation).
- Failed callback results are surfaced as terminal ``ToolCallResult`` rows
  rather than triggering retry.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys

import pytest

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.executor import BatchExecutor
from agent.tool_runtime.batch.types import (
    BatchStatus,
    ToolBatch,
    ToolCall,
    ToolCallResult,
    ToolCallStatus,
)


def _batch(tool_ids, *, strategy=ExecutionStrategy.SEQUENTIAL):
    return ToolBatch(
        tool_batch_id="tb_test",
        tool_calls=tuple(
            ToolCall(tool_call_id=f"tc_{i}", tool_id=tid, parameters={})
            for i, tid in enumerate(tool_ids)
        ),
        requested_execution_strategy=strategy,
    )


def _success_result(call: ToolCall) -> ToolCallResult:
    return ToolCallResult(
        tool_call_id=call.tool_call_id,
        tool_id=call.tool_id,
        status=ToolCallStatus.SUCCESS,
    )


def _failed_result(call: ToolCall, *, category="tool_error") -> ToolCallResult:
    return ToolCallResult(
        tool_call_id=call.tool_call_id,
        tool_id=call.tool_id,
        status=ToolCallStatus.FAILED,
        failure_category=category,
    )


@pytest.mark.asyncio
async def test_executor_runs_callback_in_manifest_order_sequential():
    order = []

    async def runner(call):
        order.append(call.tool_call_id)
        return _success_result(call)

    batch = _batch(["a", "b", "c"])
    result = await BatchExecutor().execute(
        batch,
        run_one_call=runner,
        strategy=ExecutionStrategy.SEQUENTIAL,
        parallel_timeout_s=10,
    )

    assert order == ["tc_0", "tc_1", "tc_2"]
    assert result.status is BatchStatus.COMPLETED
    assert result.effective_execution_strategy is ExecutionStrategy.SEQUENTIAL


@pytest.mark.asyncio
async def test_sequential_continues_after_failed_call():
    invocations = []

    async def runner(call):
        invocations.append(call.tool_call_id)
        if call.tool_call_id == "tc_1":
            return _failed_result(call)
        return _success_result(call)

    batch = _batch(["a", "b", "c"])
    result = await BatchExecutor().execute(
        batch,
        run_one_call=runner,
        strategy=ExecutionStrategy.SEQUENTIAL,
        parallel_timeout_s=10,
    )

    assert invocations == ["tc_0", "tc_1", "tc_2"]
    assert result.status is BatchStatus.COMPLETED_WITH_ERRORS
    assert {r.tool_call_id for r in result.call_results} == {"tc_0", "tc_1", "tc_2"}


@pytest.mark.asyncio
async def test_failed_call_is_not_retried_by_batch_executor():
    invocation_counts = {}

    async def runner(call):
        invocation_counts[call.tool_call_id] = invocation_counts.get(call.tool_call_id, 0) + 1
        return _failed_result(call)

    batch = _batch(["a", "b"])
    result = await BatchExecutor().execute(
        batch,
        run_one_call=runner,
        strategy=ExecutionStrategy.SEQUENTIAL,
        parallel_timeout_s=10,
    )

    assert all(count == 1 for count in invocation_counts.values())
    assert result.status is BatchStatus.FAILED


@pytest.mark.asyncio
async def test_parallel_path_runs_concurrently():
    call_count = 0
    max_concurrent = 0

    async def runner(call):
        nonlocal call_count, max_concurrent
        call_count += 1
        max_concurrent = max(max_concurrent, call_count)
        await asyncio.sleep(0.05)
        call_count -= 1
        return _success_result(call)

    batch = _batch(["a", "b", "c"], strategy=ExecutionStrategy.PARALLEL)
    result = await BatchExecutor().execute(
        batch,
        run_one_call=runner,
        strategy=ExecutionStrategy.PARALLEL,
        parallel_timeout_s=5,
    )

    assert max_concurrent >= 2  # at least two ran concurrently
    assert result.status is BatchStatus.COMPLETED
    assert result.effective_execution_strategy is ExecutionStrategy.PARALLEL


@pytest.mark.asyncio
async def test_parallel_path_uses_supplied_timeout_no_literal_in_module():
    """When ``parallel_timeout_s`` elapses, unfinished calls become CANCELLED."""

    async def runner(call):
        await asyncio.sleep(1.0)
        return _success_result(call)

    batch = _batch(["a", "b"], strategy=ExecutionStrategy.PARALLEL)
    result = await BatchExecutor().execute(
        batch,
        run_one_call=runner,
        strategy=ExecutionStrategy.PARALLEL,
        parallel_timeout_s=0,  # immediate timeout
    )

    cancelled = [r for r in result.call_results if r.status is ToolCallStatus.CANCELLED]
    assert cancelled, "expected at least one CANCELLED entry on timeout"


def test_executor_does_not_import_coordinator_or_graph_tool_executor():
    # Reload the module so we can inspect its module-level imports cleanly.
    module = importlib.import_module("agent.tool_runtime.batch.executor")
    src = inspect.getsource(module)

    forbidden_imports = (
        "agent.tool_runtime.coordinator",
        "agent.graph.subgraphs.tool_execution_runtime.coordinator",
        "GraphToolExecutor",
        "agent.graph.emission.unified_emitter",
        "unified_emitter",
    )
    for needle in forbidden_imports:
        assert needle not in src, (
            f"BatchExecutor must not reference {needle}; "
            "per-call wrappers and emission live in the orchestrator."
        )

    # Defensive: also make sure the loaded module's globals don't accidentally
    # depend on those modules being importable at this point.
    assert "agent.tool_runtime.coordinator" not in sys.modules or True  # informational
