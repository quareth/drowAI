"""Apply terminal ToolBatch results to graph state.

This module owns manifest-order state application after batch execution. It
does not execute tools, validate approvals, or project raw tool outcomes.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from agent.tool_runtime.batch.types import ToolBatch, ToolCall, ToolCallResult, ToolCallStatus

from .approval_and_idempotency import _store_dispatch_cache_entry
from .batch_runner import emit_tool_batch_end, write_compact_batch_metadata
from .observability import record_per_call_metrics_for_batch
from .result_state_projection import append_tool_phase_snapshot_from_metadata


def finish_with_batch_result(
    *,
    interactive: Any,
    facts: Any,
    batch: ToolBatch,
    result: Any,
    original_plan: Mapping[str, Any],
    compact_by_call_id: Mapping[str, Mapping[str, Any]],
    deterministic_compact_by_call_id: Mapping[str, Mapping[str, Any]],
    projection_by_call_id: Mapping[str, Mapping[str, Any]],
    outcome_by_call_id: Mapping[str, Any],
    execution_id_by_call_id: Mapping[str, Optional[Any]],
    tool_catalog_by_call_id: Mapping[str, Mapping[str, Any]],
    cached_dispatch_by_call_id: Mapping[str, Mapping[str, Any]],
    dispatch_cache_entry_by_call_id: Mapping[str, Mapping[str, Any]],
    trace_delta_by_call_id: Mapping[str, Mapping[str, Any]],
    metadata_patch_by_call_id: Mapping[str, Mapping[str, Any]],
    observation_by_call_id: Mapping[str, str],
    dr_execution_by_call_id: Mapping[str, Mapping[str, Any]],
    budget_consumed_call_ids: set[str],
    deps: Mapping[str, Any],
    turn_sequence: Optional[int],
    approval_response: Optional[Mapping[str, Any]],
    batch_emit_lifecycle: bool,
    has_writer: bool,
    emitter: Optional[Any],
) -> dict:
    """Apply terminal batch results and return the graph update."""
    if original_plan:
        facts.metadata["planner_plan"] = original_plan
    _apply_call_results_in_manifest_order(
        interactive=interactive,
        facts=facts,
        batch=batch,
        result=result,
        projection_by_call_id=projection_by_call_id,
        outcome_by_call_id=outcome_by_call_id,
        execution_id_by_call_id=execution_id_by_call_id,
        cached_dispatch_by_call_id=cached_dispatch_by_call_id,
        dispatch_cache_entry_by_call_id=dispatch_cache_entry_by_call_id,
        trace_delta_by_call_id=trace_delta_by_call_id,
        observation_by_call_id=observation_by_call_id,
        dr_execution_by_call_id=dr_execution_by_call_id,
        budget_consumed_call_ids=budget_consumed_call_ids,
        deps=deps,
        turn_sequence=turn_sequence,
        approval_response=approval_response,
    )
    _restore_primary_call_metadata_fields(
        facts=facts,
        batch=batch,
        projection_by_call_id=projection_by_call_id,
        tool_catalog_by_call_id=tool_catalog_by_call_id,
        cached_dispatch_by_call_id=cached_dispatch_by_call_id,
        metadata_patch_by_call_id=metadata_patch_by_call_id,
    )
    write_compact_batch_metadata(
        facts,
        batch=batch,
        result=result,
        compact_by_call_id=compact_by_call_id,
        deterministic_compact_by_call_id=deterministic_compact_by_call_id,
    )
    if projection_by_call_id or cached_dispatch_by_call_id:
        append_tool_phase_snapshot_from_metadata(
            facts=facts,
            turn_sequence=turn_sequence,
            logger=deps["logger"],
        )
    # Phase 1.2: emit per-call telemetry once per terminal row, covering
    # SUCCESS / FAILED / DENIED / CANCELLED uniformly. Single site so
    # validator-rejected, full-denial, and normal-execution paths all
    # produce the same telemetry shape.
    record_per_call_metrics_for_batch(result)
    if batch_emit_lifecycle and has_writer and emitter is not None:
        emit_tool_batch_end(emitter, result)
    deps["safe_inc"]("langgraph_tool_execution_subgraph_runs")
    deps["_clear_tool_plan_prepared_flag"](interactive)
    deps["_clear_approval_gate_metadata"](interactive)
    return interactive.as_graph_update()


def _apply_call_results_in_manifest_order(
    *,
    interactive: Any,
    facts: Any,
    batch: ToolBatch,
    result: Any,
    projection_by_call_id: Mapping[str, Mapping[str, Any]],
    outcome_by_call_id: Mapping[str, Any],
    execution_id_by_call_id: Mapping[str, Optional[Any]],
    cached_dispatch_by_call_id: Mapping[str, Mapping[str, Any]],
    dispatch_cache_entry_by_call_id: Mapping[str, Mapping[str, Any]],
    trace_delta_by_call_id: Mapping[str, Mapping[str, Any]],
    observation_by_call_id: Mapping[str, str],
    dr_execution_by_call_id: Mapping[str, Mapping[str, Any]],
    budget_consumed_call_ids: set[str],
    deps: Mapping[str, Any],
    turn_sequence: Optional[int],
    approval_response: Optional[Mapping[str, Any]],
) -> None:
    """Apply call-local result projections and side effects in manifest order."""
    terminal_rows_by_call_id = {
        row.tool_call_id: row
        for row in (getattr(result, "call_results", None) or [])
        if isinstance(getattr(row, "tool_call_id", None), str)
    }
    for call in batch.tool_calls:
        cached = cached_dispatch_by_call_id.get(call.tool_call_id)
        if isinstance(cached, Mapping):
            deps["_apply_cached_dispatch_result"](interactive, dict(cached), call.tool_id)
            continue

        projection = projection_by_call_id.get(call.tool_call_id)
        outcome = outcome_by_call_id.get(call.tool_call_id)
        if not isinstance(projection, Mapping) or outcome is None:
            terminal_row = terminal_rows_by_call_id.get(call.tool_call_id)
            _apply_denied_terminal_call_result(
                interactive=interactive,
                facts=facts,
                call=call,
                row=terminal_row,
                deps=deps,
                approval_response=approval_response,
            )
            continue
        deps["apply_result_state_projection_service"](
            interactive=interactive,
            facts=facts,
            outcome=outcome,
            projection=projection,
            execution_id=execution_id_by_call_id.get(call.tool_call_id),
            tool_call_id=call.tool_call_id,
            turn_sequence=turn_sequence,
            compact_observation_text_fn=deps["_compact_observation_text"],
            refresh_trace_scratchpad_fn=deps["refresh_trace_scratchpad"],
            memory_reduce_tool_result_fn=deps["MemoryManager"].reduce_tool_result,
            logger=deps["logger"],
            safe_inc_fn=deps["safe_inc"],
        )
        _apply_trace_delta(
            interactive,
            trace_delta_by_call_id.get(call.tool_call_id),
        )
        cache_entry = dispatch_cache_entry_by_call_id.get(call.tool_call_id)
        if isinstance(cache_entry, Mapping):
            _store_dispatch_cache_entry(
                facts,
                cache_key=deps["_TOOL_DISPATCH_CACHE_KEY"],
                tool_call_id=call.tool_call_id,
                entry=cache_entry,
            )

        dr_record = dr_execution_by_call_id.get(call.tool_call_id)
        if isinstance(dr_record, Mapping):
            deps["record_dr_tool_execution"](
                interactive,
                int(dr_record.get("iteration") or 1),
                tool=dr_record.get("tool"),
                status=dr_record.get("status"),
                command=dr_record.get("command"),
                summary=dr_record.get("summary"),
            )

        compact_result = projection.get("compact_result_dict")
        observation_text = observation_by_call_id.get(call.tool_call_id, "")
        if isinstance(compact_result, Mapping):
            _record_active_todo_attempt(
                interactive,
                outcome=outcome,
                observation_text=observation_text,
                compact_result_dict=compact_result,
                deps=deps,
            )

        if call.tool_call_id in budget_consumed_call_ids:
            _apply_tool_budget_decrement(
                interactive=interactive,
                deps=deps,
            )


def _apply_denied_terminal_call_result(
    *,
    interactive: Any,
    facts: Any,
    call: ToolCall,
    row: Optional[ToolCallResult],
    deps: Mapping[str, Any],
    approval_response: Optional[Mapping[str, Any]],
) -> None:
    """Project approval-denied rows that never reached coordinator dispatch."""
    if row is None or row.status is not ToolCallStatus.DENIED:
        return

    metadata = facts.metadata_copy()
    rejection_message = "User declined to execute this tool. The user chose to skip this tool execution."
    metadata["last_tool_result"] = {
        "status": "rejected",
        "success": False,
        "tool_name": call.tool_id,
        "stdout": rejection_message,
        "stderr": "",
        "exit_code": -1,
        "observation": rejection_message,
        "message": rejection_message,
    }
    metadata["tool_skipped"] = True
    metadata["skipped_tool"] = call.tool_id
    facts.metadata = metadata

    interactive.trace.reasoning.append(f"Tool {call.tool_id} skipped by user")
    interactive.trace.executed_tools.append(
        deps["ToolExecutionRecord"](
            tool_id=call.tool_id,
            args=dict(call.parameters or {}),
            status="skipped",
            approval_granted=False,
            approval_reason="user_skipped",
            approval_metadata=dict(approval_response or {}),
        )
    )


def _restore_primary_call_metadata_fields(
    *,
    facts: Any,
    batch: ToolBatch,
    projection_by_call_id: Mapping[str, Mapping[str, Any]],
    tool_catalog_by_call_id: Mapping[str, Mapping[str, Any]],
    cached_dispatch_by_call_id: Mapping[str, Mapping[str, Any]],
    metadata_patch_by_call_id: Mapping[str, Mapping[str, Any]],
) -> None:
    """Restore primary-call metadata projections after batch execution."""
    if not batch.tool_calls:
        return
    primary = batch.tool_calls[0]
    primary_parameters = dict(primary.parameters or {})
    if hasattr(facts, "selected_tool"):
        facts.selected_tool = primary.tool_id
    if hasattr(facts, "tool_parameters"):
        facts.tool_parameters = primary_parameters
    facts.metadata["selected_tool"] = primary.tool_id
    facts.metadata["tool_parameters"] = primary_parameters

    primary_projection = projection_by_call_id.get(primary.tool_call_id)
    if isinstance(primary_projection, Mapping):
        result_for_metadata = primary_projection.get("result_for_metadata")
        if isinstance(result_for_metadata, Mapping):
            facts.metadata["last_tool_result"] = dict(result_for_metadata)
    else:
        cached = cached_dispatch_by_call_id.get(primary.tool_call_id)
        if isinstance(cached, Mapping) and isinstance(
            cached.get("last_tool_result"), Mapping
        ):
            facts.metadata["last_tool_result"] = dict(cached["last_tool_result"])

    primary_catalog = tool_catalog_by_call_id.get(primary.tool_call_id)
    if isinstance(primary_catalog, Mapping):
        facts.metadata["tool_catalog"] = dict(primary_catalog)
    else:
        cached = cached_dispatch_by_call_id.get(primary.tool_call_id)
        if isinstance(cached, Mapping) and isinstance(
            cached.get("tool_catalog"), Mapping
        ):
            facts.metadata["tool_catalog"] = dict(cached["tool_catalog"])

    primary_patch = metadata_patch_by_call_id.get(primary.tool_call_id)
    if isinstance(primary_patch, Mapping):
        for key in ("last_artifact_path", "workspace_path", "last_execution_id"):
            if key in primary_patch:
                facts.metadata[key] = primary_patch[key]


def _apply_trace_delta(interactive: Any, delta: Optional[Mapping[str, Any]]) -> None:
    if not isinstance(delta, Mapping):
        return
    interactive.trace.reasoning.extend(list(delta.get("reasoning") or []))
    interactive.trace.observations.extend(list(delta.get("observations") or []))
    interactive.trace.executed_tools.extend(list(delta.get("executed_tools") or []))


def _apply_tool_budget_decrement(
    *,
    interactive: Any,
    deps: Mapping[str, Any],
) -> None:
    tool_budget_update = deps["decrement_tool_call_budget"](interactive.as_graph_state())
    if "facts" in tool_budget_update:
        facts_update = tool_budget_update["facts"]
        interactive.facts.tool_calls_used = facts_update.get(
            "tool_calls_used",
            interactive.facts.tool_calls_used,
        )
        if "runtime_budgets" in facts_update:
            interactive.facts.metadata["runtime_budgets"] = facts_update["runtime_budgets"]
    deps["logger"].debug(
        "[TOOL_EXECUTION] Decremented tool call budget (tool_calls_used: %s)",
        interactive.facts.tool_calls_used,
    )


def _record_active_todo_attempt(
    interactive: Any,
    *,
    outcome: Any,
    observation_text: str,
    compact_result_dict: Mapping[str, Any],
    deps: Mapping[str, Any],
) -> None:
    facts = interactive.facts
    if not facts.todo_list:
        return
    from ...state import TodoItem, TodoStatus

    active_todo = None
    for item in facts.todo_list:
        if isinstance(item, TodoItem) and item.status == TodoStatus.IN_PROGRESS:
            active_todo = item
            break

    if not active_todo:
        return

    action = {
        "tool_id": str(outcome.tool_id),
        "parameters": dict(outcome.parameters),
    }
    result = {
        "success": outcome.result.get("success", False),
        "observation": observation_text[:500],
        "summary": str(compact_result_dict.get("summary") or "")[:500],
        "errors": [str(item) for item in (compact_result_dict.get("errors") or [])][:5],
    }
    active_todo.add_attempt(action, result)
    todo_label = (
        getattr(active_todo, "description", None)
        or getattr(active_todo, "goal", "<unknown>")
    )
    deps["logger"].info(
        "[TOOL_EXECUTION] Recorded attempt for active todo '%s' (attempts=%s)",
        todo_label,
        active_todo.attempts,
    )
