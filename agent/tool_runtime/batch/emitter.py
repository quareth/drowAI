"""Stream-event payload builders for tool-batch events (Phase 5 Task 5.2).

Builds the ``tool_batch_start`` and ``tool_batch_end`` payloads that the
unified emitter ships to the frontend (and to telemetry). Keeping these
builders here — rather than in ``unified_emitter.py`` — makes it cheap to
test the payload shape in isolation and prevents the emitter from growing
batch-specific control flow.

Payload shapes match the design doc's manifest snippets in
``docs/architecture/tool-batch-execution.md``.
"""

from __future__ import annotations

from typing import Any, Dict

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.types import (
    BatchResult,
    BatchStatus,
    ToolBatch,
    ToolCallStatus,
)


def _strategy_name(strategy: ExecutionStrategy) -> str:
    return "parallel" if strategy is ExecutionStrategy.PARALLEL else "sequential"


def _params_summary(parameters: Any) -> str:
    """Render a one-line preview of a call's parameters for the manifest."""
    if not isinstance(parameters, dict) or not parameters:
        return ""
    # Prefer common identifying keys; otherwise dump the first key/value.
    for key in ("target", "url", "host", "command", "query"):
        if key in parameters:
            value = parameters[key]
            return f"{key}={str(value)[:120]}"
    first_key = next(iter(parameters))
    return f"{first_key}={str(parameters[first_key])[:120]}"


def build_tool_batch_start_payload(
    batch: ToolBatch,
    *,
    effective_execution_strategy: ExecutionStrategy,
) -> Dict[str, Any]:
    """Return the metadata payload for a ``tool_batch_start`` event."""
    calls = [
        {
            "tool_call_id": call.tool_call_id,
            "tool": call.tool_id,
            "intent": call.intent,
            "params_summary": _params_summary(call.parameters),
        }
        for call in batch.tool_calls
    ]
    return {
        "tool_batch_id": batch.tool_batch_id,
        "execution_strategy": _strategy_name(effective_execution_strategy),
        "requested_execution_strategy": _strategy_name(batch.requested_execution_strategy),
        "tool_batch_total": len(batch.tool_calls),
        "calls": calls,
    }


def build_tool_batch_end_payload(result: BatchResult) -> Dict[str, Any]:
    """Return the metadata payload for a ``tool_batch_end`` event."""
    completed = sum(1 for r in result.call_results if r.status is ToolCallStatus.SUCCESS)
    failed = sum(
        1
        for r in result.call_results
        if r.status in (ToolCallStatus.FAILED, ToolCallStatus.CANCELLED, ToolCallStatus.DENIED)
    )
    rows = [
        {
            "tool_call_id": row.tool_call_id,
            "tool": row.tool_id,
            "status": row.status.value,
            "failure_category": row.failure_category,
        }
        for row in result.call_results
    ]
    return {
        "tool_batch_id": result.tool_batch_id,
        "execution_strategy": _strategy_name(result.effective_execution_strategy),
        "requested_execution_strategy": _strategy_name(result.requested_execution_strategy),
        "status": result.status.value,
        "success": result.status is BatchStatus.COMPLETED,
        "completed": completed,
        "failed": failed,
        "results": rows,
    }


__all__ = [
    "build_tool_batch_start_payload",
    "build_tool_batch_end_payload",
]
