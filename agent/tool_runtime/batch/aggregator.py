"""Per-call → batch result aggregation + compact metadata (Phase 6 Task 6.1).

Reduces a sequence of :class:`ToolCallResult` into a single
:class:`BatchResult` and projects a compact, model-friendly metadata
representation consumed by the post-tool-reflection prompt builder.

The aggregator never makes recovery decisions. Failed/denied/cancelled
calls remain visible in ``results[]`` with their failure category so PTR
sees the complete evidence; goal-relevance is PTR's judgment, derived
from ``intent + status + compact_tool_result`` against the goal it
already holds in context. The aggregator does not compute a
``goal_relevant`` boolean — surfacing ``intent`` per row is what enables
that derivation downstream.

Cancellation policy: a cancellation that arrives *after* every call has
already reached a non-cancel terminal state must not overwrite the
natural per-call outcome. The status table below reflects that — when no
``CANCELLED`` rows are present the aggregate is derived from the
success/failure mix even if the batch was nominally cancelled at a
higher layer.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.types import (
    BatchResult,
    BatchStatus,
    ToolBatch,
    ToolCallResult,
    ToolCallStatus,
)


def _status_from_calls(rows: Sequence[ToolCallResult]) -> BatchStatus:
    """Map per-call statuses to the batch-level rollup status."""
    if not rows:
        return BatchStatus.FAILED
    statuses = [r.status for r in rows]
    if all(s is ToolCallStatus.DENIED for s in statuses):
        return BatchStatus.DENIED
    if any(s is ToolCallStatus.CANCELLED for s in statuses) and not any(
        s is ToolCallStatus.SUCCESS for s in statuses
    ):
        # Pure cancellation (or cancellation + denied/failed without any
        # success) → CANCELLED. If at least one success exists we keep the
        # natural mixed outcome below (cancellation that arrived after
        # success/failure rows were already terminal must not overwrite).
        all_terminal_non_success = all(
            s in (ToolCallStatus.CANCELLED, ToolCallStatus.FAILED, ToolCallStatus.DENIED)
            for s in statuses
        )
        if all_terminal_non_success and not any(s is ToolCallStatus.SUCCESS for s in statuses):
            return BatchStatus.CANCELLED
    successes = [s for s in statuses if s is ToolCallStatus.SUCCESS]
    failures = [
        s
        for s in statuses
        if s in (ToolCallStatus.FAILED, ToolCallStatus.CANCELLED, ToolCallStatus.DENIED)
    ]
    if successes and not failures:
        return BatchStatus.COMPLETED
    if successes and failures:
        return BatchStatus.COMPLETED_WITH_ERRORS
    return BatchStatus.FAILED


def _strategy_name(strategy: ExecutionStrategy) -> str:
    return "parallel" if strategy is ExecutionStrategy.PARALLEL else "sequential"


def _row_compact(
    call_result: ToolCallResult,
    *,
    intent_by_call_id: Mapping[str, str],
    compact_by_call_id: Mapping[str, Mapping[str, Any]],
    deterministic_compact_by_call_id: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Project a single ToolCallResult row into the compact metadata shape."""
    is_success = call_result.status is ToolCallStatus.SUCCESS
    row: Dict[str, Any] = {
        "tool_call_id": call_result.tool_call_id,
        "tool_id": call_result.tool_id,
        "intent": intent_by_call_id.get(call_result.tool_call_id, ""),
        "status": call_result.status.value,
        "success": is_success,
        "compact_tool_result": dict(
            compact_by_call_id.get(call_result.tool_call_id, {})
        ),
    }
    deterministic_compact = deterministic_compact_by_call_id.get(
        call_result.tool_call_id
    )
    if isinstance(deterministic_compact, Mapping) and deterministic_compact:
        row["deterministic_compact_tool_result"] = dict(deterministic_compact)
    if not is_success:
        row["failure_category"] = (
            call_result.failure_category or _default_failure_category(call_result.status)
        )
        if call_result.error_message:
            row["error_message"] = call_result.error_message
    return row


def _default_failure_category(status: ToolCallStatus) -> str:
    if status is ToolCallStatus.DENIED:
        return "denied"
    if status is ToolCallStatus.CANCELLED:
        return "cancelled"
    return "tool_error"


class BatchAggregator:
    """Aggregates per-call results into :class:`BatchResult` + compact metadata."""

    def aggregate(
        self,
        results: Sequence[ToolCallResult],
        *,
        batch: ToolBatch,
        effective_strategy: ExecutionStrategy,
    ) -> BatchResult:
        """Build a :class:`BatchResult` from per-call rows."""
        return BatchResult(
            tool_batch_id=batch.tool_batch_id,
            status=_status_from_calls(results),
            call_results=tuple(results),
            effective_execution_strategy=effective_strategy,
            requested_execution_strategy=batch.requested_execution_strategy,
        )

    def to_compact_metadata(
        self,
        result: BatchResult,
        *,
        batch: ToolBatch,
        compact_by_call_id: Optional[Mapping[str, Mapping[str, Any]]] = None,
        deterministic_compact_by_call_id: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Project a :class:`BatchResult` into the model-facing metadata.

        ``compact_by_call_id`` carries the per-call compact projection
        (already produced by ``result_state_projection.compact_observation_text``)
        keyed by ``tool_call_id``. The aggregator does not re-run
        compression — it only assembles the wrapper.
        """
        compact_by_call_id = compact_by_call_id or {}
        deterministic_compact_by_call_id = deterministic_compact_by_call_id or {}
        intent_by_call_id = {call.tool_call_id: call.intent for call in batch.tool_calls}

        rows: List[Dict[str, Any]] = []
        for row in result.call_results:
            rows.append(
                _row_compact(
                    row,
                    intent_by_call_id=intent_by_call_id,
                    compact_by_call_id=compact_by_call_id,
                    deterministic_compact_by_call_id=deterministic_compact_by_call_id,
                )
            )

        return {
            "tool_batch_id": result.tool_batch_id,
            "execution_strategy": _strategy_name(result.effective_execution_strategy),
            "requested_execution_strategy": _strategy_name(result.requested_execution_strategy),
            "status": result.status.value,
            "success": result.status is BatchStatus.COMPLETED,
            "results": rows,
            "deferred_followups": list(batch.deferred_followups),
        }


__all__ = ["BatchAggregator"]
