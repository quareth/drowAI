"""Phase 6 Task 6.1 unit tests for ``BatchAggregator``.

Locks the aggregate status table, the per-row compact metadata shape, the
intent-per-row contract, and the cancellation-after-terminal rule.
"""

from __future__ import annotations

import pytest

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.aggregator import BatchAggregator
from agent.tool_runtime.batch.types import (
    BatchStatus,
    ToolBatch,
    ToolCall,
    ToolCallResult,
    ToolCallStatus,
)


def _batch(*intents):
    return ToolBatch(
        tool_batch_id="tb-1",
        tool_calls=tuple(
            ToolCall(
                tool_call_id=f"tc-{i}",
                tool_id=f"tool.{i}",
                parameters={},
                intent=intent,
            )
            for i, intent in enumerate(intents, start=1)
        ),
        requested_execution_strategy=ExecutionStrategy.PARALLEL,
    )


def _result(call_id, *, status=ToolCallStatus.SUCCESS, failure_category=None):
    return ToolCallResult(
        tool_call_id=call_id,
        tool_id=call_id.replace("tc-", "tool."),
        status=status,
        failure_category=failure_category,
    )


def test_aggregator_marks_mixed_batch_completed_with_errors():
    batch = _batch("a", "b")
    rows = [_result("tc-1"), _result("tc-2", status=ToolCallStatus.FAILED, failure_category="timeout")]
    aggregator = BatchAggregator()
    result = aggregator.aggregate(rows, batch=batch, effective_strategy=ExecutionStrategy.PARALLEL)
    assert result.status is BatchStatus.COMPLETED_WITH_ERRORS


def test_aggregator_marks_all_failed_batch_failed():
    batch = _batch("a", "b")
    rows = [
        _result("tc-1", status=ToolCallStatus.FAILED, failure_category="timeout"),
        _result("tc-2", status=ToolCallStatus.FAILED, failure_category="invalid_params"),
    ]
    result = BatchAggregator().aggregate(rows, batch=batch, effective_strategy=ExecutionStrategy.SEQUENTIAL)
    assert result.status is BatchStatus.FAILED


def test_aggregator_marks_all_denied_batch_denied():
    batch = _batch("a", "b")
    rows = [
        _result("tc-1", status=ToolCallStatus.DENIED),
        _result("tc-2", status=ToolCallStatus.DENIED),
    ]
    result = BatchAggregator().aggregate(rows, batch=batch, effective_strategy=ExecutionStrategy.SEQUENTIAL)
    assert result.status is BatchStatus.DENIED


def test_aggregator_keeps_natural_status_when_cancellation_arrives_after_terminal():
    """A success-only batch must not be marked CANCELLED if a cancel arrived late."""
    batch = _batch("a", "b")
    rows = [_result("tc-1"), _result("tc-2")]
    result = BatchAggregator().aggregate(rows, batch=batch, effective_strategy=ExecutionStrategy.PARALLEL)
    assert result.status is BatchStatus.COMPLETED


def test_aggregator_marks_pure_cancellation_batch_cancelled():
    batch = _batch("a", "b")
    rows = [
        _result("tc-1", status=ToolCallStatus.CANCELLED),
        _result("tc-2", status=ToolCallStatus.CANCELLED),
    ]
    result = BatchAggregator().aggregate(rows, batch=batch, effective_strategy=ExecutionStrategy.SEQUENTIAL)
    assert result.status is BatchStatus.CANCELLED


def test_compact_metadata_shape_matches_design():
    batch = _batch("scan ports", "fingerprint server")
    rows = [
        _result("tc-1"),
        _result("tc-2", status=ToolCallStatus.FAILED, failure_category="timeout"),
    ]
    aggregator = BatchAggregator()
    result = aggregator.aggregate(rows, batch=batch, effective_strategy=ExecutionStrategy.PARALLEL)
    metadata = aggregator.to_compact_metadata(
        result,
        batch=batch,
        compact_by_call_id={"tc-1": {"summary": "ok"}, "tc-2": {"summary": "timed out"}},
    )

    assert metadata["tool_batch_id"] == "tb-1"
    assert metadata["execution_strategy"] == "parallel"
    assert metadata["status"] == "completed_with_errors"
    assert metadata["success"] is False
    assert metadata["deferred_followups"] == []
    assert len(metadata["results"]) == 2
    assert metadata["results"][0]["intent"] == "scan ports"
    assert metadata["results"][0]["compact_tool_result"] == {"summary": "ok"}
    assert metadata["results"][1]["status"] == "failed"
    assert metadata["results"][1]["failure_category"] == "timeout"
    assert metadata["results"][1]["intent"] == "fingerprint server"


def test_compact_metadata_includes_optional_deterministic_lane():
    batch = _batch("scan ports")
    rows = [_result("tc-1")]
    aggregator = BatchAggregator()
    result = aggregator.aggregate(
        rows,
        batch=batch,
        effective_strategy=ExecutionStrategy.SEQUENTIAL,
    )
    metadata = aggregator.to_compact_metadata(
        result,
        batch=batch,
        compact_by_call_id={"tc-1": {"summary": "llm summary"}},
        deterministic_compact_by_call_id={
            "tc-1": {"summary": "deterministic summary"}
        },
    )

    row = metadata["results"][0]
    assert row["compact_tool_result"] == {"summary": "llm summary"}
    assert row["deterministic_compact_tool_result"] == {
        "summary": "deterministic summary"
    }


def test_failed_middle_call_not_hidden_by_later_success():
    batch = _batch("a", "b", "c")
    rows = [
        _result("tc-1"),
        _result("tc-2", status=ToolCallStatus.FAILED, failure_category="timeout"),
        _result("tc-3"),
    ]
    aggregator = BatchAggregator()
    result = aggregator.aggregate(rows, batch=batch, effective_strategy=ExecutionStrategy.SEQUENTIAL)
    metadata = aggregator.to_compact_metadata(result, batch=batch)
    statuses = [row["status"] for row in metadata["results"]]
    assert statuses == ["success", "failed", "success"]
    assert result.status is BatchStatus.COMPLETED_WITH_ERRORS


def test_aggregator_carries_intent_per_row():
    batch = _batch("first intent", "second intent")
    rows = [_result("tc-1"), _result("tc-2")]
    aggregator = BatchAggregator()
    result = aggregator.aggregate(rows, batch=batch, effective_strategy=ExecutionStrategy.PARALLEL)
    metadata = aggregator.to_compact_metadata(result, batch=batch)
    assert metadata["results"][0]["intent"] == "first intent"
    assert metadata["results"][1]["intent"] == "second intent"


def test_failure_category_is_populated_for_every_non_success_row():
    batch = _batch("a", "b", "c")
    rows = [
        _result("tc-1"),
        _result("tc-2", status=ToolCallStatus.FAILED),  # no explicit category
        _result("tc-3", status=ToolCallStatus.DENIED),
    ]
    aggregator = BatchAggregator()
    result = aggregator.aggregate(rows, batch=batch, effective_strategy=ExecutionStrategy.PARALLEL)
    metadata = aggregator.to_compact_metadata(result, batch=batch)
    failure_rows = [row for row in metadata["results"] if not row["success"]]
    for row in failure_rows:
        assert "failure_category" in row and row["failure_category"]
