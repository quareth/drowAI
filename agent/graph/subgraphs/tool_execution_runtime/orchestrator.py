"""High-level orchestrator entrypoints for tool-execution subgraph runtime.

This module hosts orchestration flow for tool execution, plan preparation,
dispatch, and approval gate nodes while `tool_execution.py` remains the public
compatibility facade with stable import paths and signatures.

Batch-backed plans are admitted by ``BatchValidator`` and scheduled through
``BatchExecutor``. Active execution requires a canonical ``ToolBatch`` manifest;
old single-tool-shaped planner fields are not accepted as execution input.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

if TYPE_CHECKING:
    from langgraph.types import StreamWriter

from .batch_runner import (
    _effective_batch_strategy,
    _require_canonical_tool_batch,
    build_batch_cancel_check,
    build_terminal_batch_result,
    compact_failure_result,
    emit_tool_batch_start,
    merge_batch_call_results,
    should_emit_batch_lifecycle,
    validate_batch,
    validate_batch_after_approval,
)
from .approval_and_idempotency import (
    _apply_approval_edits_to_batch,
    _call_by_id,
    extract_approved_call_ids,
)
from .batch_result_application import finish_with_batch_result
from .per_call_execution import build_run_one_call_callback
from .retry_replanning import _apply_checkpoint_retry_tool_replanning_context
from agent.reasoning.tool_selection_sentinel import metadata_has_unavailable_capability
from agent.tool_runtime.batch.executor import BatchExecutor
from agent.tool_runtime.batch.types import ToolBatch, ToolCallResult, ToolCallStatus
from agent.tool_runtime.backend_tool_policy import require_runtime_placement_mode
from agent.tool_runtime.timeout_policy import ToolTimeoutPolicy
from ...emission.reasoning_section import reasoning_section


def _attach_planner_llm_client_resolver(
    *,
    coordinator_config: Any,
    metadata: Mapping[str, Any],
    context: Optional[Any],
    runtime_context: Optional[Any],
    config: Optional[Dict[str, Any]],
    deps: Mapping[str, Any],
) -> None:
    """Attach a lazy provider-neutral planner client resolver to local config."""

    resolve_llm_client_fn = deps.get("resolve_llm_client")
    if not callable(resolve_llm_client_fn):
        return

    def _resolve() -> Any:
        return resolve_llm_client_fn(
            dict(metadata),
            runtime_context or context,
            config=config,
        )

    setattr(coordinator_config, "llm_client_resolver", _resolve)


def _resolve_required_runtime_placement_mode(
    *,
    runtime_context: Optional[Any],
    metadata: Mapping[str, Any],
    coordinator_config: Any,
) -> str:
    """Return explicit placement for dispatch or raise a fail-closed error."""
    return require_runtime_placement_mode(
        (
            getattr(runtime_context, "runtime_placement_mode", None)
            if runtime_context is not None
            else None
        )
        or metadata.get("runtime_placement_mode")
        or getattr(coordinator_config, "runtime_placement_mode", None)
    )


async def run_tool_execution_orchestrator(
    state: Mapping[str, object] | Any,
    *,
    context: Optional[Any],
    config: Optional[Dict[str, Any]],
    writer: Optional["StreamWriter"] = None,
    deps: Mapping[str, Any],
) -> dict:
    """Execute full tool-runtime orchestration using injected dependencies."""
    interactive = deps["InteractiveState"].from_mapping(state)
    facts = interactive.facts
    metadata = facts.metadata_copy()

    configurable = (config or {}).get("configurable") or {}
    approval_received_at = configurable.get("approval_received_at")
    resume_worker_start_at = configurable.get("resume_worker_start_at")
    interrupt_id_raw = configurable.get("interrupt_id")
    interrupt_id = (
        interrupt_id_raw.strip()
        if isinstance(interrupt_id_raw, str) and interrupt_id_raw.strip()
        else None
    )
    dispatch_tool_start_at = time.perf_counter()
    approval_received_ts = deps["coerce_timestamp"](approval_received_at)
    resume_worker_start_ts = deps["coerce_timestamp"](resume_worker_start_at)
    if approval_received_ts is not None:
        try:
            ms = (dispatch_tool_start_at - approval_received_ts) * 1000
            task_id = facts.task_id
            graph_name = configurable.get("graph_name", "unknown")
            runtime_path = deps["resolve_runtime_path_label"](configurable, metadata)
            deps["emit_labeled_latency_metric"](
                "approval_to_tool_start_ms",
                ms,
                graph_name=graph_name,
                runtime_path=runtime_path,
                gauge_fn=deps["safe_gauge"],
            )
            deps["logger"].info(
                "[HITL] approval_to_tool_start_ms=%.1f task_id=%s graph_name=%s",
                ms,
                task_id,
                graph_name,
            )
        except Exception:
            pass

    conversation_id, turn_id = deps["resolve_stream_identifiers"](interactive, config)
    dr_iteration = (
        deps["_resolve_dr_iteration"](metadata)
        if (facts.capability or "").lower() == "deep_reasoning"
        else None
    )

    has_writer = writer is not None

    deps["logger"].info(f"[TOOL_EXECUTION] has_writer={has_writer}, task_id={facts.task_id}")
    metadata = _apply_checkpoint_retry_tool_replanning_context(
        interactive,
        config=config,
        metadata=metadata,
        deps=deps,
    )
    request, coordinator_config, runtime_context, workspace_path = deps[
        "build_request_and_coordinator_config"
    ](
        interactive=interactive,
        context=context,
        metadata=metadata,
    )
    _attach_planner_llm_client_resolver(
        coordinator_config=coordinator_config,
        metadata=metadata,
        context=context,
        runtime_context=runtime_context,
        config=config,
        deps=deps,
    )

    plan_prepared = bool(metadata.get("tool_plan_prepared", False))
    if plan_prepared and metadata.get("planner_plan"):
        request.metadata = metadata
        deps["logger"].info("[TOOL_EXECUTION] Reusing preplanned action plan (HITL-safe path)")
    else:
        async with reasoning_section(
            writer,
            state=interactive,
            step="tool_planning",
            label="Preparing tool execution.",
            config=config,
            context=context,
        ):
            await deps["_ensure_action_plan"](interactive, request, coordinator_config)

    if metadata_has_unavailable_capability(facts.metadata):
        deps["logger"].info(
            "[TOOL_EXECUTION] Planner reported unavailable capability; "
            "returning to PTR without batch dispatch."
        )
        deps["_clear_tool_plan_prepared_flag"](interactive)
        deps["_clear_approval_gate_metadata"](interactive)
        return interactive.as_graph_update()

    # Reconstruct the canonical ToolBatch from planner_plan. Missing batch
    # manifests are treated as planner/runtime contract violations so old
    # single-tool-shaped fields cannot become execution authority.
    tool_batch = _require_canonical_tool_batch(facts.metadata, stage="tool_execution")
    batch_validation = validate_batch(tool_batch, config=coordinator_config, facts=facts)
    tool_batch = batch_validation.batch
    batch_emit_lifecycle = should_emit_batch_lifecycle(tool_batch, config=coordinator_config)
    metadata["tool_batch_id"] = tool_batch.tool_batch_id
    metadata["tool_batch_validation"] = {
        "admitted": batch_validation.admitted,
        "requested_execution_strategy": batch_validation.requested_execution_strategy.value,
        "effective_execution_strategy": batch_validation.effective_execution_strategy.value,
        "strategy_downgraded": batch_validation.strategy_downgraded,
        "downgrade_reason": batch_validation.downgrade_reason,
        "rejected_reason": batch_validation.rejected_reason,
    }
    facts.metadata = metadata
    return await _run_batch_tool_execution(
        interactive=interactive,
        facts=facts,
        metadata=metadata,
        tool_batch=tool_batch,
        batch_validation=batch_validation,
        batch_emit_lifecycle=batch_emit_lifecycle,
        request=request,
        coordinator_config=coordinator_config,
        runtime_context=runtime_context,
        workspace_path=workspace_path,
        context=context,
        config=config,
        deps=deps,
        conversation_id=conversation_id,
        turn_id=turn_id,
        dr_iteration=dr_iteration,
        approval_received_ts=approval_received_ts,
        resume_worker_start_ts=resume_worker_start_ts,
        dispatch_tool_start_at=dispatch_tool_start_at,
        interrupt_id=interrupt_id,
        writer=writer,
        has_writer=has_writer,
    )


async def _run_batch_tool_execution(
    *,
    interactive: Any,
    facts: Any,
    metadata: Dict[str, Any],
    tool_batch: ToolBatch,
    batch_validation: Any,
    batch_emit_lifecycle: bool,
    request: Any,
    coordinator_config: Any,
    runtime_context: Optional[Any],
    workspace_path: Optional[str],
    context: Optional[Any],
    config: Optional[Dict[str, Any]],
    deps: Mapping[str, Any],
    conversation_id: Optional[str],
    turn_id: Optional[str],
    dr_iteration: Optional[int],
    approval_received_ts: Optional[float],
    resume_worker_start_ts: Optional[float],
    dispatch_tool_start_at: float,
    interrupt_id: Optional[str],
    writer: Optional[Any],
    has_writer: bool,
) -> dict:
    """Run a validated ToolBatch through the batch executor."""
    original_plan = dict(metadata.get("planner_plan") or {})
    gate_completed = bool(metadata.get(deps["_APPROVAL_GATE_COMPLETED_KEY"]))
    if interrupt_id:
        metadata["interrupt_id"] = interrupt_id

    step_sub_turn_index = None
    if (facts.capability or "").lower() == "simple_tool_execution":
        step_sub_turn_index = deps["resolve_direct_executor_step_index"](interactive)
        metadata["sub_turn_index"] = step_sub_turn_index

    turn_sequence = deps["resolve_turn_sequence"](context, metadata)
    if turn_sequence is None and runtime_context and runtime_context.turn_sequence is not None:
        turn_sequence = runtime_context.turn_sequence

    emitter = None
    if has_writer:
        emitter = deps["EventEmitterFactory"].create_from_identity(
            writer,
            conversation_id,
            turn_id,
            turn_sequence=turn_sequence,
            sub_turn_index=step_sub_turn_index,
        )
        if batch_emit_lifecycle:
            emit_tool_batch_start(emitter, tool_batch, batch_validation)

    compact_by_call_id: Dict[str, Mapping[str, Any]] = {}
    deterministic_compact_by_call_id: Dict[str, Mapping[str, Any]] = {}
    projection_by_call_id: Dict[str, Mapping[str, Any]] = {}
    outcome_by_call_id: Dict[str, Any] = {}
    execution_id_by_call_id: Dict[str, Optional[Any]] = {}
    tool_catalog_by_call_id: Dict[str, Mapping[str, Any]] = {}
    cached_dispatch_by_call_id: Dict[str, Mapping[str, Any]] = {}
    dispatch_cache_entry_by_call_id: Dict[str, Mapping[str, Any]] = {}
    trace_delta_by_call_id: Dict[str, Mapping[str, Any]] = {}
    metadata_patch_by_call_id: Dict[str, Mapping[str, Any]] = {}
    observation_by_call_id: Dict[str, str] = {}
    dr_execution_by_call_id: Dict[str, Mapping[str, Any]] = {}
    budget_consumed_call_ids: set[str] = set()
    approval_response: Optional[Dict[str, Any]] = None
    finish_result_args = {
        "interactive": interactive,
        "facts": facts,
        "batch": tool_batch,
        "original_plan": original_plan,
        "compact_by_call_id": compact_by_call_id,
        "deterministic_compact_by_call_id": deterministic_compact_by_call_id,
        "projection_by_call_id": projection_by_call_id,
        "tool_catalog_by_call_id": tool_catalog_by_call_id,
        "outcome_by_call_id": outcome_by_call_id,
        "execution_id_by_call_id": execution_id_by_call_id,
        "cached_dispatch_by_call_id": cached_dispatch_by_call_id,
        "dispatch_cache_entry_by_call_id": dispatch_cache_entry_by_call_id,
        "trace_delta_by_call_id": trace_delta_by_call_id,
        "metadata_patch_by_call_id": metadata_patch_by_call_id,
        "observation_by_call_id": observation_by_call_id,
        "dr_execution_by_call_id": dr_execution_by_call_id,
        "budget_consumed_call_ids": budget_consumed_call_ids,
        "deps": deps,
        "turn_sequence": turn_sequence,
        "batch_emit_lifecycle": batch_emit_lifecycle,
        "has_writer": has_writer,
        "emitter": emitter,
    }

    if not batch_validation.admitted:
        reason = str(batch_validation.rejected_reason or "batch_validation_rejected")
        result = build_terminal_batch_result(
            tool_batch,
            status=ToolCallStatus.FAILED,
            failure_category=reason,
            effective_strategy=batch_validation.effective_execution_strategy,
        )
        compact_by_call_id.update(
            {
                call.tool_call_id: compact_failure_result(call, category=reason)
                for call in tool_batch.tool_calls
            }
        )
        return finish_with_batch_result(
            result=result,
            approval_response=approval_response,
            **finish_result_args,
        )

    execution_batch = batch_validation.batch
    effective_strategy = _effective_batch_strategy(batch_validation, coordinator_config)
    pre_terminal_rows: list[ToolCallResult] = []

    try:
        runtime_placement_mode = _resolve_required_runtime_placement_mode(
            runtime_context=runtime_context,
            metadata=metadata,
            coordinator_config=coordinator_config,
        )
    except ValueError:
        result = build_terminal_batch_result(
            tool_batch,
            status=ToolCallStatus.FAILED,
            failure_category="missing_runtime_placement",
            effective_strategy=effective_strategy,
        )
        compact_by_call_id.update(
            {
                call.tool_call_id: compact_failure_result(
                    call,
                    category="missing_runtime_placement",
                )
                for call in tool_batch.tool_calls
            }
        )
        return finish_with_batch_result(
            result=result,
            approval_response=approval_response,
            **finish_result_args,
        )

    if deps["should_require_approval"](metadata):
        if gate_completed:
            approval_response = deps["normalize_tool_approval_response"](
                metadata.get(deps["_APPROVAL_GATE_RESPONSE_KEY"])
                if isinstance(metadata.get(deps["_APPROVAL_GATE_RESPONSE_KEY"]), dict)
                else None
            )
        else:
            approval_response = deps["normalize_tool_approval_response"](
                deps["request_tool_approval"](
                    tool_id=execution_batch.tool_calls[0].tool_id,
                    tool_name=execution_batch.tool_calls[0].tool_id,
                    parameters=dict(execution_batch.tool_calls[0].parameters),
                    description=f"Execute {len(execution_batch.tool_calls)} tool calls",
                    risk_level=deps["_get_tool_risk_level"](execution_batch.tool_calls[0].tool_id),
                    metadata=metadata,
                    turn_sequence=turn_sequence,
                    turn_id=turn_id,
                    reserved_message_id=metadata.get("reserved_message_id"),
                    tool_call_id=execution_batch.tool_calls[0].tool_call_id,
                    tool_batch_id=execution_batch.tool_batch_id,
                    items=[
                        {
                            "tool_call_id": call.tool_call_id,
                            "tool_id": call.tool_id,
                            "tool_name": call.tool_id,
                            "parameters": dict(call.parameters),
                            "description": f"Execute {call.tool_id} on target",
                            "risk_level": deps["_get_tool_risk_level"](call.tool_id),
                        }
                        for call in execution_batch.tool_calls
                    ],
                )
            )
            metadata[deps["_APPROVAL_GATE_COMPLETED_KEY"]] = True
            metadata[deps["_APPROVAL_GATE_RESPONSE_KEY"]] = dict(approval_response)
            facts.metadata = metadata

        execution_batch, edit_failures = _apply_approval_edits_to_batch(
            execution_batch,
            approval_response,
            logger=deps["logger"],
        )
        pre_terminal_rows.extend(edit_failures)
        for row in edit_failures:
            failed_call = _call_by_id(tool_batch, row.tool_call_id)
            if failed_call is not None:
                compact_by_call_id[row.tool_call_id] = compact_failure_result(
                    failed_call,
                    category=row.failure_category or "invalid_edited_parameters",
                )

        invalid_edit_ids = {row.tool_call_id for row in edit_failures}
        approved_ids = [
            call_id
            for call_id in extract_approved_call_ids(
                approval_response,
                all_call_ids=[call.tool_call_id for call in execution_batch.tool_calls],
            )
            if call_id not in invalid_edit_ids
        ]
        approved_set = set(approved_ids)
        for call in execution_batch.tool_calls:
            if call.tool_call_id in approved_set or call.tool_call_id in invalid_edit_ids:
                continue
            pre_terminal_rows.append(
                ToolCallResult(
                    tool_call_id=call.tool_call_id,
                    tool_id=call.tool_id,
                    status=ToolCallStatus.DENIED,
                    failure_category="denied",
                    error_message="denied",
                )
            )
            compact_by_call_id[call.tool_call_id] = compact_failure_result(
                call,
                category="denied",
            )

        approval_validation = validate_batch_after_approval(
            execution_batch,
            approved_call_ids=approved_ids,
        )
        if not approval_validation.admitted:
            result = merge_batch_call_results(
                tool_batch,
                executed=(),
                pre_terminal=pre_terminal_rows,
                effective_strategy=approval_validation.effective_execution_strategy,
            )
            return finish_with_batch_result(
                result=result,
                approval_response=approval_response,
                **finish_result_args,
            )

        partial_approval = len(approved_ids) < len(execution_batch.tool_calls)
        execution_batch = approval_validation.batch
        if partial_approval:
            effective_strategy = approval_validation.effective_execution_strategy

    coordinator_holder: dict[str, Any] = {}

    def _get_local_coordinator() -> Any:
        coordinator = coordinator_holder.get("coordinator")
        if coordinator is None:
            coordinator = deps["ToolExecutionCoordinator"](config=coordinator_config)
            coordinator_holder["coordinator"] = coordinator
        return coordinator

    timeout_policy = ToolTimeoutPolicy.from_runtime_config(coordinator_config)
    timeout_plan_by_call_id = {
        call.tool_call_id: timeout_policy.resolve(
            tool_id=call.tool_id,
            parameters=call.parameters,
        )
        for call in execution_batch.tool_calls
    }

    run_one_call = build_run_one_call_callback(
        interactive=interactive,
        metadata=metadata,
        original_plan=original_plan,
        tool_batch=tool_batch,
        deps=deps,
        interrupt_id=interrupt_id,
        step_sub_turn_index=step_sub_turn_index,
        effective_strategy=effective_strategy,
        timeout_plan_by_call_id=timeout_plan_by_call_id,
        timeout_policy=timeout_policy,
        runtime_placement_mode=runtime_placement_mode,
        approval_received_ts=approval_received_ts,
        dispatch_tool_start_at=dispatch_tool_start_at,
        resume_worker_start_ts=resume_worker_start_ts,
        cached_dispatch_by_call_id=cached_dispatch_by_call_id,
        compact_by_call_id=compact_by_call_id,
        deterministic_compact_by_call_id=deterministic_compact_by_call_id,
        projection_by_call_id=projection_by_call_id,
        outcome_by_call_id=outcome_by_call_id,
        execution_id_by_call_id=execution_id_by_call_id,
        tool_catalog_by_call_id=tool_catalog_by_call_id,
        dispatch_cache_entry_by_call_id=dispatch_cache_entry_by_call_id,
        trace_delta_by_call_id=trace_delta_by_call_id,
        metadata_patch_by_call_id=metadata_patch_by_call_id,
        observation_by_call_id=observation_by_call_id,
        dr_execution_by_call_id=dr_execution_by_call_id,
        budget_consumed_call_ids=budget_consumed_call_ids,
        has_writer=has_writer,
        emitter=emitter,
        turn_sequence=turn_sequence,
        turn_id=turn_id,
        conversation_id=conversation_id,
        request=request,
        workspace_path=workspace_path,
        runtime_context=runtime_context,
        dr_iteration=dr_iteration,
        config=config,
        writer=writer,
        approval_response=approval_response,
        get_local_coordinator=_get_local_coordinator,
    )

    cancel_check = build_batch_cancel_check(
        task_id=getattr(facts, "task_id", None),
        turn_id=turn_id,
    )
    result = await BatchExecutor().execute(
        execution_batch,
        run_one_call=run_one_call,
        strategy=effective_strategy,
        parallel_timeout_s=max(
            (
                plan.deadline_seconds + plan.grace_seconds
                for plan in timeout_plan_by_call_id.values()
            ),
            default=coordinator_config.tool_timeout_default_seconds
            + coordinator_config.tool_timeout_grace_seconds,
        ),
        cancel_check=cancel_check,
    )
    if pre_terminal_rows:
        result = merge_batch_call_results(
            tool_batch,
            executed=result.call_results,
            pre_terminal=pre_terminal_rows,
            effective_strategy=effective_strategy,
        )

    return finish_with_batch_result(
        result=result,
        approval_response=approval_response,
        **finish_result_args,
    )

async def approval_gate_node_orchestrator(
    state: Mapping[str, object] | Any,
    *,
    context: Optional[Any],
    config: Optional[Dict[str, Any]],
    deps: Mapping[str, Any],
) -> dict:
    """Interrupt-only approval gate orchestration via injected dependencies."""
    interactive = deps["InteractiveState"].from_mapping(state)
    facts = interactive.facts
    metadata = facts.metadata_copy()
    tool_batch = _require_canonical_tool_batch(metadata, stage="approval_gate")
    primary_call = tool_batch.tool_calls[0]
    tool_name = primary_call.tool_id
    tool_params = dict(primary_call.parameters)
    metadata["tool_batch_id"] = tool_batch.tool_batch_id
    approval_response: Dict[str, Any] = {"action": "approve"}

    if deps["should_require_approval"](metadata):
        _, turn_id = deps["resolve_stream_identifiers"](interactive, config)
        approval_response = deps["normalize_tool_approval_response"](
            deps["request_tool_approval"](
                tool_id=tool_name,
                tool_name=tool_name,
                parameters=tool_params,
                description=f"Execute {tool_name} on target",
                risk_level=deps["_get_tool_risk_level"](tool_name),
                metadata=metadata,
                turn_sequence=deps["resolve_turn_sequence"](context, metadata),
                turn_id=turn_id,
                reserved_message_id=metadata.get("reserved_message_id"),
                tool_call_id=primary_call.tool_call_id,
                tool_batch_id=tool_batch.tool_batch_id,
                items=[
                    {
                        "tool_call_id": call.tool_call_id,
                        "tool_id": call.tool_id,
                        "tool_name": call.tool_id,
                        "parameters": dict(call.parameters),
                        "description": f"Execute {call.tool_id} on target",
                        "risk_level": deps["_get_tool_risk_level"](call.tool_id),
                    }
                    for call in tool_batch.tool_calls
                ],
            )
        )

    metadata[deps["_APPROVAL_GATE_COMPLETED_KEY"]] = True
    metadata[deps["_APPROVAL_GATE_RESPONSE_KEY"]] = dict(approval_response)
    facts.metadata = metadata
    return interactive.as_graph_update()


async def dispatch_tool_execution_node_orchestrator(
    state: Mapping[str, object] | Any,
    *,
    context: Optional[Any],
    config: Optional[Dict[str, Any]],
    writer: Optional["StreamWriter"] = None,
    deps: Mapping[str, Any],
) -> dict:
    """Dispatch-only node that routes into run orchestration."""
    interactive = deps["InteractiveState"].from_mapping(state)
    metadata = interactive.facts.metadata_copy()
    if not metadata.get(deps["_APPROVAL_GATE_COMPLETED_KEY"]):
        metadata[deps["_APPROVAL_GATE_COMPLETED_KEY"]] = True
        metadata[deps["_APPROVAL_GATE_RESPONSE_KEY"]] = {"action": "approve"}
        interactive.facts.metadata = metadata
    return await deps["run_tool_execution_fn"](
        interactive.as_graph_state(),
        context=context,
        config=config,
        writer=writer,
    )


async def prepare_tool_execution_plan_orchestrator(
    state: Mapping[str, object] | Any,
    *,
    context: Optional[Any],
    config: Optional[Dict[str, Any]],
    writer: Optional["StreamWriter"] = None,
    deps: Mapping[str, Any],
) -> dict:
    """Prepare planner output and pre-dispatch metadata markers."""
    interactive = deps["InteractiveState"].from_mapping(state)
    facts = interactive.facts
    metadata = facts.metadata_copy()
    metadata = _apply_checkpoint_retry_tool_replanning_context(
        interactive,
        config=config,
        metadata=metadata,
        deps=deps,
    )
    request, coordinator_config, _runtime_context, _workspace_path = deps[
        "build_request_and_coordinator_config"
    ](
        interactive=interactive,
        context=context,
        metadata=metadata,
    )
    _attach_planner_llm_client_resolver(
        coordinator_config=coordinator_config,
        metadata=metadata,
        context=context,
        runtime_context=_runtime_context,
        config=config,
        deps=deps,
    )

    async with reasoning_section(
        writer,
        state=interactive,
        step="tool_planning",
        label="Preparing tool execution.",
        config=config,
        context=context,
    ):
        await deps["_ensure_action_plan"](interactive, request, coordinator_config)
    metadata = interactive.facts.ensure_metadata()
    if metadata_has_unavailable_capability(metadata):
        deps["logger"].info(
            "[TOOL_EXECUTION] Prepared unavailable-capability planner result; "
            "routing directly to PTR."
        )
        metadata.pop("tool_plan_prepared", None)
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()
    metadata["tool_plan_prepared"] = True
    # Per-call ``tool_call_id`` is minted by the canonical batch commit path
    # and written to metadata inside ``_run_batch_tool_execution.run_one_call``.
    interactive.facts.metadata = metadata
    return interactive.as_graph_update()
