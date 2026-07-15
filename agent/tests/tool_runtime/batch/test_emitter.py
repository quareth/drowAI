"""Phase 5 Task 5.2 unit tests for batch event payload builders.

Locks the structure that ``unified_emitter.emit_tool_batch_start/end``
ships to the frontend (matches the design doc's manifest snippets).
"""

from __future__ import annotations

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.emitter import (
    build_tool_batch_end_payload,
    build_tool_batch_start_payload,
)
from agent.tool_runtime.batch.types import (
    BatchResult,
    BatchStatus,
    ToolBatch,
    ToolCall,
    ToolCallResult,
    ToolCallStatus,
)


def _batch():
    return ToolBatch(
        tool_batch_id="tb-1",
        tool_calls=(
            ToolCall(
                tool_call_id="tc-1",
                tool_id="web.ffuf",
                parameters={"url": "http://x/FUZZ"},
                intent="find paths",
            ),
            ToolCall(
                tool_call_id="tc-2",
                tool_id="web.whatweb",
                parameters={"target": "http://x"},
                intent="fingerprint",
            ),
        ),
        requested_execution_strategy=ExecutionStrategy.PARALLEL,
    )


def test_tool_batch_start_payload_lists_calls_in_manifest_order():
    payload = build_tool_batch_start_payload(
        _batch(),
        effective_execution_strategy=ExecutionStrategy.SEQUENTIAL,
    )
    assert payload["tool_batch_id"] == "tb-1"
    assert payload["execution_strategy"] == "sequential"
    assert payload["requested_execution_strategy"] == "parallel"
    assert payload["tool_batch_total"] == 2
    assert [c["tool_call_id"] for c in payload["calls"]] == ["tc-1", "tc-2"]
    assert payload["calls"][0]["tool"] == "web.ffuf"
    assert payload["calls"][0]["intent"] == "find paths"
    assert "FUZZ" in payload["calls"][0]["params_summary"]


def test_tool_batch_end_payload_aggregates_per_call_status():
    batch = _batch()
    result = BatchResult(
        tool_batch_id=batch.tool_batch_id,
        status=BatchStatus.COMPLETED_WITH_ERRORS,
        call_results=(
            ToolCallResult(
                tool_call_id="tc-1",
                tool_id="web.ffuf",
                status=ToolCallStatus.SUCCESS,
            ),
            ToolCallResult(
                tool_call_id="tc-2",
                tool_id="web.whatweb",
                status=ToolCallStatus.FAILED,
                failure_category="timeout",
            ),
        ),
        effective_execution_strategy=ExecutionStrategy.PARALLEL,
        requested_execution_strategy=ExecutionStrategy.PARALLEL,
    )
    payload = build_tool_batch_end_payload(result)
    assert payload["tool_batch_id"] == "tb-1"
    assert payload["status"] == "completed_with_errors"
    assert payload["success"] is False
    assert payload["completed"] == 1
    assert payload["failed"] == 1
    assert payload["execution_strategy"] == "parallel"
    assert [r["tool_call_id"] for r in payload["results"]] == ["tc-1", "tc-2"]
    assert payload["results"][1]["failure_category"] == "timeout"
