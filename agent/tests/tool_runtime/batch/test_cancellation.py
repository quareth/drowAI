"""Phase 7 Task 7.3 unit tests for batch cancellation contract.

Locks:

- Mid-batch cancellation produces a single ``BatchResult`` with
  ``BatchStatus.CANCELLED`` (or ``COMPLETED_WITH_ERRORS`` when partial
  successes preceded the cancel).
- Surviving terminal results retain their per-call status.
- Cancelled rows carry ``failure_category="batch_cancelled"``.
- ``BatchExecutor.execute`` accepts an optional ``cancel_check`` hook
  without breaking existing callers.
"""

from __future__ import annotations

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


def _batch(tool_ids):
    return ToolBatch(
        tool_batch_id="tb_cancel",
        tool_calls=tuple(
            ToolCall(tool_call_id=f"tc_{i}", tool_id=tid, parameters={})
            for i, tid in enumerate(tool_ids)
        ),
        requested_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )


def _success_result(call: ToolCall) -> ToolCallResult:
    return ToolCallResult(
        tool_call_id=call.tool_call_id,
        tool_id=call.tool_id,
        status=ToolCallStatus.SUCCESS,
    )


@pytest.mark.asyncio
async def test_cancellation_marks_pending_calls_cancelled_pure_cancel():
    invocations = []

    cancel_state = {"flag": False}

    async def runner(call):
        invocations.append(call.tool_call_id)
        cancel_state["flag"] = True  # cancel after the first call
        return _success_result(call)

    batch = _batch(["a", "b", "c"])

    # cancel_check fires before the first call too — set it up so first
    # call runs, then cancel kicks in.
    def cancel_check():
        return cancel_state["flag"]

    result = await BatchExecutor().execute(
        batch,
        run_one_call=runner,
        strategy=ExecutionStrategy.SEQUENTIAL,
        parallel_timeout_s=10,
        cancel_check=cancel_check,
    )

    # Only first call runs; remaining are cancelled.
    assert invocations == ["tc_0"]
    assert result.status is BatchStatus.COMPLETED_WITH_ERRORS  # 1 success + 2 cancel
    statuses = [r.status for r in result.call_results]
    assert statuses == [
        ToolCallStatus.SUCCESS,
        ToolCallStatus.CANCELLED,
        ToolCallStatus.CANCELLED,
    ]
    cancelled = [r for r in result.call_results if r.status is ToolCallStatus.CANCELLED]
    assert all(r.failure_category == "batch_cancelled" for r in cancelled)


@pytest.mark.asyncio
async def test_cancellation_before_any_call_yields_cancelled_aggregate():
    async def runner(call):
        return _success_result(call)

    batch = _batch(["a", "b"])
    result = await BatchExecutor().execute(
        batch,
        run_one_call=runner,
        strategy=ExecutionStrategy.SEQUENTIAL,
        parallel_timeout_s=10,
        cancel_check=lambda: True,
    )

    assert result.status is BatchStatus.CANCELLED
    assert all(r.status is ToolCallStatus.CANCELLED for r in result.call_results)


@pytest.mark.asyncio
async def test_cancellation_hook_default_none_preserves_legacy_behavior():
    async def runner(call):
        return _success_result(call)

    batch = _batch(["a", "b"])
    result = await BatchExecutor().execute(
        batch,
        run_one_call=runner,
        strategy=ExecutionStrategy.SEQUENTIAL,
        parallel_timeout_s=10,
        # cancel_check not supplied
    )

    assert result.status is BatchStatus.COMPLETED
    assert len(result.call_results) == 2
