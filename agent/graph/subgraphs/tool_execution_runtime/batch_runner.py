"""Tool-batch lifecycle helpers for the orchestrator (Phase 5 Task 5.4).

Lives next to ``orchestrator.py`` so batch admission, lifecycle emission, and
compact batch metadata stay centralized while the orchestrator owns the
per-call execution callback.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.aggregator import BatchAggregator
from agent.tool_runtime.batch.compatibility import BatchCompatibilityChecker
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
from agent.tool_runtime.batch.validator import BatchValidationResult, BatchValidator
from core.prompts.builders.post_tool.evidence import register_runtime_compact_evidence
from runtime_shared.durable_secret_masking import mask_durable_secrets


def deserialize_tool_batch_from_plan_data(plan_data: Mapping[str, Any]) -> Optional[ToolBatch]:
    """Reconstruct a ToolBatch from the serialized planner_plan dict.

    Returns ``None`` if the plan_data does not include a ``tool_batch``
    block. Tolerant: missing optional fields fall back to safe defaults so
    a partial canonical dict still produces a runnable ToolBatch.
    """
    if not isinstance(plan_data, Mapping):
        return None
    raw = plan_data.get("tool_batch")
    if not isinstance(raw, Mapping):
        return None
    raw_calls = raw.get("tool_calls")
    if not isinstance(raw_calls, list) or not raw_calls:
        return None
    calls = []
    for entry in raw_calls:
        if not isinstance(entry, Mapping):
            continue
        tool_call_id = entry.get("tool_call_id")
        tool_id = entry.get("tool_id")
        if not isinstance(tool_call_id, str) or not isinstance(tool_id, str):
            continue
        params = entry.get("parameters") or {}
        intent = entry.get("intent") or ""
        calls.append(
            ToolCall(
                tool_call_id=tool_call_id,
                tool_id=tool_id,
                parameters=dict(params) if isinstance(params, Mapping) else {},
                intent=str(intent),
            )
        )
    if not calls:
        return None
    strategy_text = str(raw.get("requested_execution_strategy", "sequential")).lower()
    strategy = (
        ExecutionStrategy.PARALLEL
        if strategy_text == "parallel"
        else ExecutionStrategy.SEQUENTIAL
    )
    deferred = raw.get("deferred_followups") or []
    rationale = raw.get("selection_rationale") or ""
    return ToolBatch(
        tool_batch_id=str(raw.get("tool_batch_id") or ""),
        tool_calls=tuple(calls),
        requested_execution_strategy=strategy,
        deferred_followups=tuple(deferred) if isinstance(deferred, list) else (),
        selection_rationale=str(rationale),
    )


def _effective_batch_strategy(batch_validation: Any, config: Any) -> ExecutionStrategy:
    strategy = batch_validation.effective_execution_strategy
    if (
        strategy is ExecutionStrategy.PARALLEL
        and not bool(getattr(config, "parallel_execution_enabled", True))
    ):
        return ExecutionStrategy.SEQUENTIAL
    return strategy


def _require_canonical_tool_batch(metadata: Mapping[str, Any], *, stage: str) -> ToolBatch:
    """Return the canonical ToolBatch manifest or fail closed."""
    planner_plan = metadata.get("planner_plan") if isinstance(metadata, Mapping) else {}
    tool_batch = deserialize_tool_batch_from_plan_data(planner_plan or {})
    if tool_batch is not None:
        return tool_batch
    raise RuntimeError(
        f"{stage} requires planner_plan.tool_batch; refusing old single-tool planner state"
    )


def validate_batch(
    batch: ToolBatch,
    *,
    config: Any,
    facts: Any,
    candidate_count: Optional[int] = None,
) -> BatchValidationResult:
    """Run BatchValidator with config + facts derived ctx (Phase 4 + Task 5.4).

    Phase 8 Task 8.2: emit validator telemetry (candidate_count,
    committed_count, requested vs effective strategy, downgrade_reason
    histogram, validation_rejected_reason histogram).

    Phase 1.3 (re-audit fix): when ``candidate_count`` is omitted, fall back
    to the selector candidate count derived from ``facts`` (via the same
    ``_candidate_tool_ids`` helper the validator uses), not the committed
    batch size — so the metric reports the audit-gap signal the operator
    needs (committed << candidates) instead of a tautology
    (candidate_count == committed_count).
    """
    candidate_tool_ids = _candidate_tool_ids(facts)
    ctx = {
        "max_committed_tools_per_batch": int(
            getattr(config, "max_committed_tools_per_batch", 1) or 1
        ),
        "candidate_tool_ids": candidate_tool_ids,
        "action_target": _action_target(facts),
        "max_shell_command_chars": int(
            getattr(config, "shell_exec_max_command_chars", 320) or 320
        ),
        "high_risk_tool_prefixes": ("exploitation_tools.",),
    }
    available_tool_ids = _available_tool_ids()
    if available_tool_ids:
        ctx["available_tool_ids"] = available_tool_ids
    budgets = getattr(facts, "budgets", None)
    if budgets is not None:
        max_calls = getattr(budgets, "max_tool_calls", None)
        if isinstance(max_calls, int):
            ctx["max_tool_calls"] = max_calls
    used = getattr(facts, "tool_calls_used", None)
    if isinstance(used, int):
        ctx["tool_calls_used"] = used
    result = BatchValidator(BatchCompatibilityChecker()).validate(batch, ctx)

    # Telemetry — fire-and-forget (safe_inc/safe_gauge swallow errors).
    try:
        from .observability import record_batch_validation_metrics

        if candidate_count is not None:
            effective_candidate_count = int(candidate_count)
        elif candidate_tool_ids:
            effective_candidate_count = len(candidate_tool_ids)
        else:
            # No selector candidates available on the canonical batch manifest.
            # Fall back to committed_count so the gauge stays populated;
            # downgrade-reason / audit-gap signals come from ``downgrade_reason``
            # regardless.
            effective_candidate_count = len(batch.tool_calls)
        record_batch_validation_metrics(
            candidate_count=effective_candidate_count,
            committed_count=len(batch.tool_calls),
            requested_strategy=result.requested_execution_strategy.value,
            effective_strategy=result.effective_execution_strategy.value,
            strategy_downgraded=result.strategy_downgraded,
            downgrade_reason=result.downgrade_reason,
            rejected_reason=result.rejected_reason,
        )
    except Exception:  # pragma: no cover - defensive
        pass

    return result


def validate_batch_after_approval(
    batch: ToolBatch,
    *,
    approved_call_ids: Sequence[str],
) -> BatchValidationResult:
    """Run the validator's approval-subset admission check."""
    return BatchValidator(BatchCompatibilityChecker()).validate_after_approval(
        batch,
        approved_call_ids=approved_call_ids,
    )


def build_terminal_batch_result(
    batch: ToolBatch,
    *,
    status: ToolCallStatus,
    failure_category: str,
    effective_strategy: ExecutionStrategy,
) -> BatchResult:
    """Build a batch result where every call ended before coordinator dispatch."""
    rows = tuple(
        ToolCallResult(
            tool_call_id=call.tool_call_id,
            tool_id=call.tool_id,
            status=status,
            failure_category=failure_category,
            error_message=failure_category,
        )
        for call in batch.tool_calls
    )
    aggregate = BatchAggregator().aggregate(
        rows,
        batch=batch,
        effective_strategy=effective_strategy,
    )
    return BatchResult(
        tool_batch_id=aggregate.tool_batch_id,
        status=aggregate.status,
        call_results=aggregate.call_results,
        effective_execution_strategy=aggregate.effective_execution_strategy,
        requested_execution_strategy=aggregate.requested_execution_strategy,
        downgrade_reason=None,
        duration_ms=aggregate.duration_ms,
    )


def merge_batch_call_results(
    batch: ToolBatch,
    *,
    executed: Sequence[ToolCallResult],
    pre_terminal: Sequence[ToolCallResult],
    effective_strategy: ExecutionStrategy,
) -> BatchResult:
    """Merge executed and pre-terminal rows in original manifest order."""
    by_call_id = {
        row.tool_call_id: row
        for row in list(pre_terminal or []) + list(executed or [])
    }
    ordered = tuple(
        by_call_id[call.tool_call_id]
        for call in batch.tool_calls
        if call.tool_call_id in by_call_id
    )
    return BatchAggregator().aggregate(
        ordered,
        batch=batch,
        effective_strategy=effective_strategy,
    )


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?)[^'\"\s,;]+"
    ),
)


def _redact_failure_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: (
                f"{match.group(1)}<redacted>"
                if match.groups()
                else "<redacted>"
            ),
            text,
        )
    return text


def compact_failure_result(
    call: ToolCall,
    *,
    category: str,
    message: str | None = None,
) -> Dict[str, Any]:
    """Build a compact failure envelope for rows without a coordinator result."""
    safe_category = _redact_failure_text(category) or "tool_error"
    safe_message = _redact_failure_text(message) or safe_category
    return {
        "schema_version": "2.0",
        "tool": call.tool_id,
        "status": safe_category,
        "success": False,
        "summary": f"{call.tool_id} did not execute: {safe_message}",
        "errors": [safe_message],
    }


def _candidate_tool_ids(facts: Any) -> list[str]:
    candidates = getattr(facts, "tool_candidates", None) or getattr(facts, "tool_ids", None)
    if isinstance(candidates, Sequence) and not isinstance(candidates, (str, bytes)):
        return [str(item) for item in candidates if str(item or "").strip()]
    metadata = getattr(facts, "metadata", {}) or {}
    if isinstance(metadata, Mapping):
        plan = metadata.get("planner_plan")
        if isinstance(plan, Mapping):
            selected = plan.get("selected_tools")
            if isinstance(selected, Sequence) and not isinstance(selected, (str, bytes)):
                return [str(item) for item in selected if str(item or "").strip()]
    return []


def _action_target(facts: Any) -> Optional[str]:
    intent_hints = getattr(facts, "intent_hints", None)
    if isinstance(intent_hints, Mapping):
        targets = intent_hints.get("targets")
        if isinstance(targets, Sequence) and not isinstance(targets, (str, bytes)) and targets:
            return str(targets[0])
    return None


def _available_tool_ids() -> list[str]:
    try:
        from agent.tools.tool_registry import available_tools
    except Exception:  # pragma: no cover - defensive
        return []
    try:
        return list(available_tools())
    except Exception:  # pragma: no cover - defensive
        return []


def should_emit_batch_lifecycle(batch: ToolBatch, *, config: Any) -> bool:
    """Decide whether to emit batch start/end events for this batch.

    Multi-call batches always emit. Single-call batches are gated by
    ``AgentConfig.emit_batch_events_for_single_call`` (defaults False in
    Phase 5; flipped to True in Phase 7 once ToolBatchCard.tsx lands).
    """
    if len(batch.tool_calls) > 1:
        return True
    return bool(getattr(config, "emit_batch_events_for_single_call", False))


def emit_tool_batch_start(emitter: Any, batch: ToolBatch, validation: BatchValidationResult) -> None:
    """Emit ``tool_batch_start`` via the unified emitter."""
    payload = build_tool_batch_start_payload(
        batch,
        effective_execution_strategy=validation.effective_execution_strategy,
    )
    emitter.emit_tool_batch_start(payload)


def emit_tool_batch_end(emitter: Any, result: BatchResult) -> None:
    """Emit ``tool_batch_end`` via the unified emitter.

    Phase 8 Task 8.2: also records the batch aggregate-status counter and
    duration gauge so the operator can chart denied/cancelled/completed
    rollups across runs.
    """
    payload = build_tool_batch_end_payload(result)
    emitter.emit_tool_batch_end(payload)
    try:
        from .observability import record_batch_aggregate_metrics

        record_batch_aggregate_metrics(
            aggregate_status=result.status.value,
            duration_ms=float(getattr(result, "duration_ms", 0) or 0),
        )
    except Exception:  # pragma: no cover - defensive
        pass


def synthesize_single_call_batch_result(
    batch: ToolBatch,
    *,
    success: bool,
    failure_category: Optional[str],
    effective_strategy: ExecutionStrategy,
) -> BatchResult:
    """Build a one-row BatchResult for the legacy single-call path.

    Phase 5 ships with ``max_committed_tools_per_batch=1``; the
    orchestrator's existing single-call body produces the per-call
    outcome. This helper turns that outcome into a one-row BatchResult so
    the lifecycle emitter sees a uniform aggregate shape.
    """
    if not batch.tool_calls:
        return BatchResult(
            tool_batch_id=batch.tool_batch_id,
            status=BatchStatus.FAILED,
            call_results=tuple(),
            effective_execution_strategy=effective_strategy,
            requested_execution_strategy=batch.requested_execution_strategy,
        )
    call = batch.tool_calls[0]
    status = ToolCallStatus.SUCCESS if success else ToolCallStatus.FAILED
    row = ToolCallResult(
        tool_call_id=call.tool_call_id,
        tool_id=call.tool_id,
        status=status,
        failure_category=failure_category if not success else None,
    )
    batch_status = BatchStatus.COMPLETED if success else BatchStatus.FAILED
    return BatchResult(
        tool_batch_id=batch.tool_batch_id,
        status=batch_status,
        call_results=(row,),
        effective_execution_strategy=effective_strategy,
        requested_execution_strategy=batch.requested_execution_strategy,
    )


def write_compact_batch_metadata(
    facts: Any,
    *,
    batch: ToolBatch,
    result: BatchResult,
    compact_by_call_id: Optional[Mapping[str, Mapping[str, Any]]] = None,
    deterministic_compact_by_call_id: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> None:
    """Author the batch-shaped compact metadata + the derived legacy field.

    Phase 9 Task 9.2: this is the *single author site* for both
    ``last_tool_result_compact_batch`` (the batch-shaped twin consumed by
    PTR's batch-aware reader) and ``last_tool_result_compact`` (the legacy
    single-call field consumed by older readers — synthesizer, failure
    detection, planner_service). The legacy field is derived as a
    projection of the primary call's compact entry from
    ``compact_by_call_id`` so there is exactly one author site for the
    compact evidence; readers see the same shape they always did.
    """
    compact_map = {
        str(call_id): dict(compact)
        for call_id, compact in (compact_by_call_id or {}).items()
        if isinstance(compact, Mapping)
    }
    deterministic_compact_map = {
        str(call_id): dict(compact)
        for call_id, compact in (deterministic_compact_by_call_id or {}).items()
        if isinstance(compact, Mapping)
    }
    call_by_id = {call.tool_call_id: call for call in batch.tool_calls}
    for row in result.call_results:
        if row.tool_call_id in compact_map:
            continue
        if row.status is ToolCallStatus.SUCCESS:
            continue
        call = call_by_id.get(row.tool_call_id)
        if call is None:
            continue
        category = row.failure_category or row.status.value
        compact_map[row.tool_call_id] = compact_failure_result(
            call,
            category=category,
            message=row.error_message or category,
        )

    aggregator = BatchAggregator()
    metadata_dict = aggregator.to_compact_metadata(
        result,
        batch=batch,
        compact_by_call_id=compact_map,
        deterministic_compact_by_call_id=deterministic_compact_map,
    )

    # Derive the legacy single-call field from the primary call (the first
    # tool_call in the batch manifest order). When a per-call compact entry
    # is supplied for that call we forward it verbatim; otherwise the field
    # is omitted (legacy readers tolerate the missing key).
    legacy_compact = _derive_primary_call_compact(
        batch=batch, compact_by_call_id=compact_map
    )
    register_runtime_compact_evidence(
        metadata_dict,
        single_compact=legacy_compact if legacy_compact is not None else {},
    )
    facts.metadata["last_tool_result_compact_batch"] = mask_durable_secrets(
        metadata_dict,
        source="last_tool_result_compact_batch",
    )
    if legacy_compact is not None:
        facts.metadata["last_tool_result_compact"] = mask_durable_secrets(
            legacy_compact,
            source="last_tool_result_compact",
        )


def build_batch_cancel_check(
    *,
    task_id: Optional[Any],
    turn_id: Optional[str],
) -> Optional[Callable[[], bool]]:
    """Construct a cancel-check closure for ``BatchExecutor.execute``.

    Reuses the same lifecycle source the single-tool path queries via
    ``base_handler._build_cancellation_checker`` (per-task / per-turn cancel
    flag in ``run_lifecycle``). Throttled to one DB poll per 250ms to match
    that helper's cadence so two concurrent checkers do not double-poll.

    Returns ``None`` when ``task_id`` or ``turn_id`` is missing (e.g. the
    hand-rolled-state test paths the orchestrator still serves), in which
    case the executor is invoked without a cancel hook — equivalent to
    today's behavior.
    """
    if task_id in (None, "") or not turn_id:
        return None
    try:
        from backend.services.langgraph_chat.runtime.run_lifecycle import (
            get_run_lifecycle_service,
        )
    except Exception:
        return None
    lifecycle = get_run_lifecycle_service()

    state: Dict[str, float] = {"last_checked_at": 0.0}
    cached: Dict[str, bool] = {"value": False}
    poll_interval_seconds = 0.25

    def cancel_check() -> bool:
        if cached["value"]:
            return True
        now = time.monotonic()
        if now - state["last_checked_at"] < poll_interval_seconds:
            return False
        state["last_checked_at"] = now
        try:
            cached["value"] = bool(
                lifecycle.is_cancel_requested(task_id=int(task_id), turn_id=str(turn_id))
            )
        except Exception:
            cached["value"] = False
        return cached["value"]

    return cancel_check


def _derive_primary_call_compact(
    *,
    batch: ToolBatch,
    compact_by_call_id: Mapping[str, Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Project the primary (first) call's compact dict for legacy consumers."""
    if not batch.tool_calls:
        return None
    primary_call_id = batch.tool_calls[0].tool_call_id
    primary = compact_by_call_id.get(primary_call_id)
    if not isinstance(primary, Mapping):
        return None
    return dict(primary)


__all__ = [
    "deserialize_tool_batch_from_plan_data",
    "validate_batch",
    "should_emit_batch_lifecycle",
    "emit_tool_batch_start",
    "emit_tool_batch_end",
    "synthesize_single_call_batch_result",
    "validate_batch_after_approval",
    "build_terminal_batch_result",
    "merge_batch_call_results",
    "compact_failure_result",
    "write_compact_batch_metadata",
    "build_batch_cancel_check",
]
