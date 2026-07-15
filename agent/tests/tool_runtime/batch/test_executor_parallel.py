"""Phase 8 Task 8.1 unit test for the parallel execution path.

Locks the runtime contract that the parallel branch is honored when the
validator admits a parallel batch and the config gate is enabled. The
executor itself does not consult the gate (the orchestrator does that
upstream); this test exercises the executor with strategy=PARALLEL and
asserts concurrent dispatch.
"""

from __future__ import annotations

import asyncio

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
        tool_batch_id="tb_par",
        tool_calls=tuple(
            ToolCall(tool_call_id=f"tc_{i}", tool_id=tid, parameters={})
            for i, tid in enumerate(tool_ids)
        ),
        requested_execution_strategy=ExecutionStrategy.PARALLEL,
    )


@pytest.mark.asyncio
async def test_parallel_runs_concurrently_when_validator_admits():
    in_flight = 0
    max_in_flight = 0

    async def runner(call):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return ToolCallResult(
            tool_call_id=call.tool_call_id,
            tool_id=call.tool_id,
            status=ToolCallStatus.SUCCESS,
        )

    batch = _batch(["a", "b", "c"])
    result = await BatchExecutor().execute(
        batch,
        run_one_call=runner,
        strategy=ExecutionStrategy.PARALLEL,
        parallel_timeout_s=5,
    )

    assert max_in_flight >= 2
    assert result.status is BatchStatus.COMPLETED
    assert result.effective_execution_strategy is ExecutionStrategy.PARALLEL
