"""Execute one approved ToolCall for the tool-execution runtime.

This module owns call-local graph state isolation and the per-call path from
cache lookup or coordinator dispatch through result projection capture. It does
not apply terminal batch results to the shared graph state.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.types import ToolBatch, ToolCall, ToolCallResult, ToolCallStatus

from .approval_and_idempotency import _read_dispatch_cache_entry
from .lane_dispatch import resolve_tool_lane_dispatch


def _project_tool_call_status(*, success: bool, status: Any) -> ToolCallStatus:
    """Map runtime outcome status into ToolCallStatus without losing cancellation."""
    if success:
        return ToolCallStatus.SUCCESS
    status_text = str(status or "").strip().lower()
    if status_text in {"cancelled", "canceled"}:
        return ToolCallStatus.CANCELLED
    return ToolCallStatus.FAILED


def _single_call_planner_plan(
    original_plan: Mapping[str, Any],
    call: ToolCall,
    *,
    effective_strategy: ExecutionStrategy,
) -> Dict[str, Any]:
    plan = dict(original_plan or {})
    plan["tool_batch"] = {
        "tool_batch_id": str(plan.get("tool_batch_id") or ""),
        "requested_execution_strategy": effective_strategy.value,
        "deferred_followups": [],
        "selection_rationale": str(plan.get("selection_rationale") or ""),
        "tool_calls": [
            {
                "tool_call_id": call.tool_call_id,
                "tool_id": call.tool_id,
                "parameters": dict(call.parameters),
                "intent": call.intent,
            }
        ],
    }
    plan["execution_strategy"] = effective_strategy.value
    return plan


def _build_call_local_context(
    *,
    interactive: Any,
    metadata: Mapping[str, Any],
    original_plan: Mapping[str, Any],
    call: ToolCall,
    tool_batch: ToolBatch,
    deps: Mapping[str, Any],
    interrupt_id: Optional[str],
    step_sub_turn_index: Optional[int],
    effective_strategy: ExecutionStrategy,
) -> tuple[Any, Any, Dict[str, Any]]:
    """Return detached per-call state so parallel callbacks cannot share facts."""
    if hasattr(interactive, "model_copy"):
        call_interactive = interactive.model_copy(deep=True)
    else:  # pragma: no cover - compatibility for non-pydantic test doubles
        call_interactive = deps["InteractiveState"].from_mapping(
            interactive.as_graph_state()
        )
    call_facts = call_interactive.facts
    call_metadata = dict(metadata or call_facts.metadata_copy())
    call_metadata.update(
        {
            deps["_TOOL_CALL_ID_KEY"]: call.tool_call_id,
            "tool_call_id": call.tool_call_id,
            "tool_batch_id": tool_batch.tool_batch_id,
            "planner_plan": _single_call_planner_plan(
                original_plan,
                call,
                effective_strategy=effective_strategy,
            ),
            "batch_effective_execution_strategy": effective_strategy.value,
        }
    )
    if interrupt_id:
        call_metadata["interrupt_id"] = interrupt_id
    if step_sub_turn_index is not None:
        call_metadata["sub_turn_index"] = step_sub_turn_index

    call_facts.metadata = call_metadata
    return call_interactive, call_facts, call_metadata


def _clone_request_for_call(request: Any, metadata: Dict[str, Any]) -> Any:
    return type(request)(
        capability=request.capability,
        targets=list(request.targets or []),
        message=request.message,
        task_id=request.task_id,
        conversation_id=request.conversation_id,
        history=list(request.history or []),
        metadata=metadata,
        workspace_path=request.workspace_path,
        user_id=request.user_id,
        provider=getattr(request, "provider", None),
        model=request.model,
        credential_ref=(
            dict(request.credential_ref)
            if isinstance(getattr(request, "credential_ref", None), Mapping)
            else None
        ),
        llm_runtime_selection=(
            dict(request.llm_runtime_selection)
            if isinstance(getattr(request, "llm_runtime_selection", None), Mapping)
            else None
        ),
        reasoning_effort=getattr(request, "reasoning_effort", None),
    )


def _trace_lengths(interactive: Any) -> Dict[str, int]:
    trace = interactive.trace
    return {
        "reasoning": len(trace.reasoning or []),
        "observations": len(trace.observations or []),
        "executed_tools": len(trace.executed_tools or []),
    }


def _collect_trace_delta(
    interactive: Any,
    *,
    base_lengths: Mapping[str, int],
) -> Dict[str, list[Any]]:
    trace = interactive.trace
    reasoning_start = int(base_lengths.get("reasoning") or 0)
    observation_start = int(base_lengths.get("observations") or 0)
    executed_start = int(base_lengths.get("executed_tools") or 0)
    return {
        "reasoning": list((trace.reasoning or [])[reasoning_start:]),
        "observations": list((trace.observations or [])[observation_start:]),
        "executed_tools": [
            item.model_copy(deep=True) if hasattr(item, "model_copy") else item
            for item in (trace.executed_tools or [])[executed_start:]
        ],
    }


def _collect_call_metadata_patch(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    patch: Dict[str, Any] = {}
    for key in ("last_artifact_path", "workspace_path", "last_execution_id"):
        if key in metadata:
            patch[key] = metadata[key]
    return patch


def build_run_one_call_callback(
    *,
    interactive: Any,
    metadata: Mapping[str, Any],
    original_plan: Mapping[str, Any],
    tool_batch: ToolBatch,
    deps: Mapping[str, Any],
    interrupt_id: Optional[str],
    step_sub_turn_index: Optional[int],
    effective_strategy: ExecutionStrategy,
    timeout_plan_by_call_id: Mapping[str, Any],
    timeout_policy: Any,
    runtime_placement_mode: str,
    approval_received_ts: Optional[float],
    dispatch_tool_start_at: float,
    resume_worker_start_ts: Optional[float],
    cached_dispatch_by_call_id: Dict[str, Mapping[str, Any]],
    compact_by_call_id: Dict[str, Mapping[str, Any]],
    deterministic_compact_by_call_id: Dict[str, Mapping[str, Any]],
    projection_by_call_id: Dict[str, Mapping[str, Any]],
    outcome_by_call_id: Dict[str, Any],
    execution_id_by_call_id: Dict[str, Optional[Any]],
    tool_catalog_by_call_id: Dict[str, Mapping[str, Any]],
    dispatch_cache_entry_by_call_id: Dict[str, Mapping[str, Any]],
    trace_delta_by_call_id: Dict[str, Mapping[str, Any]],
    metadata_patch_by_call_id: Dict[str, Mapping[str, Any]],
    observation_by_call_id: Dict[str, str],
    dr_execution_by_call_id: Dict[str, Mapping[str, Any]],
    budget_consumed_call_ids: set[str],
    has_writer: bool,
    emitter: Optional[Any],
    turn_sequence: Optional[int],
    turn_id: Optional[str],
    conversation_id: Optional[str],
    request: Any,
    workspace_path: Optional[str],
    runtime_context: Optional[Any],
    dr_iteration: Optional[int],
    config: Optional[Dict[str, Any]],
    writer: Optional[Any],
    approval_response: Optional[Mapping[str, Any]],
    get_local_coordinator: Callable[[], Any],
) -> Callable[[ToolCall], Awaitable[ToolCallResult]]:
    """Build the per-call executor callback used by BatchExecutor."""

    async def run_one_call(call: ToolCall) -> ToolCallResult:
        tool_name = call.tool_id
        tool_call_id = call.tool_call_id
        timeout_plan = timeout_plan_by_call_id.get(tool_call_id)
        if timeout_plan is None:
            timeout_plan = timeout_policy.resolve(
                tool_id=call.tool_id,
                parameters=call.parameters,
            )
        tool_params = dict(timeout_plan.normalized_parameters)
        call_interactive, call_facts, call_metadata = _build_call_local_context(
            interactive=interactive,
            metadata=metadata,
            original_plan=original_plan,
            call=call,
            tool_batch=tool_batch,
            deps=deps,
            interrupt_id=interrupt_id,
            step_sub_turn_index=step_sub_turn_index,
            effective_strategy=effective_strategy,
        )
        lane_dispatch = resolve_tool_lane_dispatch(
            tool_id=call.tool_id,
            runtime_placement_mode=runtime_placement_mode,
        )
        call_metadata["lane_dispatch"] = lane_dispatch.as_metadata()
        call_metadata["timeout_plan"] = timeout_plan.to_metadata()
        call_facts.metadata = call_metadata
        trace_base_lengths = _trace_lengths(call_interactive)

        if approval_received_ts is not None:
            deps["_emit_hitl_stage"](
                stage="approval_received_at",
                timestamp=approval_received_ts,
                task_id=call_facts.task_id,
                interrupt_id=interrupt_id,
                tool_call_id=tool_call_id,
            )
            deps["_emit_hitl_stage"](
                stage="dispatch_tool_start_at",
                timestamp=dispatch_tool_start_at,
                task_id=call_facts.task_id,
                interrupt_id=interrupt_id,
                tool_call_id=tool_call_id,
            )
        if resume_worker_start_ts is not None:
            deps["_emit_hitl_stage"](
                stage="resume_worker_start_at",
                timestamp=resume_worker_start_ts,
                task_id=call_facts.task_id,
                interrupt_id=interrupt_id,
                tool_call_id=tool_call_id,
            )

        cached = _read_dispatch_cache_entry(
            call_metadata,
            deps["_TOOL_DISPATCH_CACHE_KEY"],
            tool_call_id,
        )
        if cached is not None:
            cached_dispatch_by_call_id[tool_call_id] = dict(cached)
            compact = cached.get("last_tool_result_compact")
            if isinstance(compact, Mapping):
                compact_by_call_id[tool_call_id] = dict(compact)
            deterministic_compact = cached.get("last_tool_result_deterministic_compact")
            if isinstance(deterministic_compact, Mapping):
                deterministic_compact_by_call_id[tool_call_id] = dict(
                    deterministic_compact
                )
            exec_record = (
                cached.get("exec_record")
                if isinstance(cached.get("exec_record"), Mapping)
                else {}
            )
            cached_status = str(exec_record.get("status") or "").lower()
            success = cached_status == "success"
            projected_status = _project_tool_call_status(
                success=success,
                status=cached_status,
            )
            return ToolCallResult(
                tool_call_id=tool_call_id,
                tool_id=tool_name,
                status=projected_status,
                raw_result=dict(cached.get("last_tool_result") or {}),
                failure_category=(
                    None
                    if success
                    else "cancelled"
                    if projected_status is ToolCallStatus.CANCELLED
                    else "cached_tool_error"
                ),
            )

        if has_writer and emitter is not None:
            emitter.emit_tool_start(
                tool_name,
                parameters=tool_params,
                tool_call_id=tool_call_id,
                tool_batch_id=tool_batch.tool_batch_id,
            )
            deps["logger"].info(
                "[TOOL_EXECUTION] Emitted tool_start for %s (task_id=%s interrupt_id=%s tool_call_id=%s turn_sequence=%s turn_id=%s conv=%s sub_turn_index=%s)",
                tool_name,
                call_facts.task_id,
                interrupt_id or "unknown",
                tool_call_id,
                turn_sequence,
                turn_id,
                conversation_id,
                step_sub_turn_index,
            )
            deps["_diag_info"](
                "TOOL_EXECUTION | tool_start | task_id=%s interrupt_id=%s tool=%s tool_call_id=%s turn_sequence=%s turn_id=%s conv=%s sub_turn_index=%s",
                call_facts.task_id,
                interrupt_id or "unknown",
                tool_name,
                tool_call_id,
                turn_sequence,
                turn_id,
                conversation_id,
                step_sub_turn_index,
            )

        call_request = _clone_request_for_call(request, call_metadata)
        execution_id = deps["record_provenance_execution_start_service"](
            get_provenance_service_fn=deps["_get_provenance_service"],
            request=call_request,
            metadata=call_metadata,
            facts=call_facts,
            tool_name=tool_name,
            tool_params=tool_params,
            tool_call_id=tool_call_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            workspace_path=workspace_path,
            logger=deps["logger"],
            safe_inc_fn=deps["safe_inc"],
        )
        persisted_artifact_refs = []

        try:
            deps["safe_inc"]("langgraph_tool_plans")
            outcome = await get_local_coordinator().run(call_request)
        except Exception as exc:
            deps["safe_inc"]("langgraph_tool_plan_failures")
            if execution_id is not None:
                deps["finalize_provenance_after_execution_error_service"](
                    get_provenance_service_fn=deps["_get_provenance_service"],
                    execution_id=execution_id,
                    exc=exc,
                    workspace_path=workspace_path,
                    tool_name=tool_name,
                    should_persist_artifact_outputs_fn=deps["_should_persist_artifact_outputs"],
                    logger=deps["logger"],
                    safe_inc_fn=deps["safe_inc"],
                )
            raise

        dr_tool_for_display: Optional[str] = None
        dr_command_display: Optional[str] = None
        call_dr_iteration: Optional[int] = None
        if (call_facts.capability or "").lower() == "deep_reasoning":
            call_dr_iteration = dr_iteration or deps["_resolve_dr_iteration"](call_metadata)
            dr_tool_for_display = str(outcome.tool_id or tool_name or "unknown_tool")
            parameters_for_display = dict(tool_params)
            dr_command_display = deps["_build_command_for_display"](
                dr_tool_for_display,
                parameters_for_display,
            )

        artifact_path = deps["save_execution_artifact_service"](
            outcome=outcome,
            tool_name=tool_name,
            workspace_path=workspace_path,
            facts=call_facts,
            interactive=call_interactive,
            save_tool_output_artifact_fn=deps["save_tool_output_artifact"],
            safe_inc_fn=deps["safe_inc"],
            logger=deps["logger"],
        )
        artifact_workspace_path = deps["resolve_execution_artifact_workspace_service"](
            workspace_path=workspace_path,
            facts=call_facts,
        )
        deps["schedule_artifact_indexing_service"](
            artifact_path=artifact_path,
            workspace_path=artifact_workspace_path,
            selected_tool=tool_name,
        )

        if execution_id is not None:
            persisted_artifact_refs = deps["finalize_provenance_execution_service"](
                get_provenance_service_fn=deps["_get_provenance_service"],
                execution_id=execution_id,
                outcome=outcome,
                facts=call_facts,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                turn_sequence=turn_sequence,
                workspace_path=artifact_workspace_path,
                artifact_path=artifact_path,
                should_persist_artifact_outputs_fn=deps["_should_persist_artifact_outputs"],
                build_command_for_display_fn=deps["_build_command_for_display"],
                collect_persistable_tool_artifact_paths_fn=deps[
                    "_collect_persistable_tool_artifact_paths"
                ],
                collect_provenance_artifact_refs_fn=deps["_collect_provenance_artifact_refs"],
                logger=deps["logger"],
                safe_inc_fn=deps["safe_inc"],
            )

        projection = await deps["project_result_state_service"](
            interactive=call_interactive,
            facts=call_facts,
            outcome=outcome,
            tool_name=tool_name,
            metadata=call_metadata,
            runtime_context=runtime_context,
            artifact_path=artifact_path,
            execution_id=execution_id,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch.tool_batch_id,
            tool_intent=call.intent,
            turn_sequence=turn_sequence,
            persisted_artifact_refs=persisted_artifact_refs,
            compact_sanitized_result_keys=tuple(deps["_COMPACT_SANITIZED_RESULT_KEYS"]),
            compact_observation_text_fn=deps["_compact_observation_text"],
            enrich_artifact_refs_with_provenance_fn=deps["_enrich_artifact_refs_with_provenance"],
            refresh_trace_scratchpad_fn=deps["refresh_trace_scratchpad"],
            resolve_llm_client_fn=deps["resolve_llm_client"],
            compress_tool_output_fn=deps["compress_tool_output"],
            compact_output_size_bytes_fn=deps["compact_output_size_bytes"],
            record_compression_observability_metrics_fn=deps[
                "record_compression_observability_metrics"
            ],
            memory_reduce_tool_result_fn=deps["MemoryManager"].reduce_tool_result,
            logger=deps["logger"],
            safe_inc_fn=deps["safe_inc"],
            safe_gauge_fn=deps["safe_gauge"],
            config=config,
            apply_to_state=False,
        )
        compact_result_dict = dict(projection["compact_result_dict"])
        deterministic_compact_result = projection.get("deterministic_compact_result_dict")
        result_for_metadata = dict(projection["result_for_metadata"])
        graph_metadata = dict(projection["graph_metadata"])
        action_record = dict(projection["action_record"])
        compact_by_call_id[tool_call_id] = dict(compact_result_dict)
        if isinstance(deterministic_compact_result, Mapping):
            deterministic_compact_by_call_id[tool_call_id] = dict(
                deterministic_compact_result
            )
        projection_by_call_id[tool_call_id] = dict(projection)
        outcome_by_call_id[tool_call_id] = outcome
        execution_id_by_call_id[tool_call_id] = execution_id

        if (call_facts.capability or "").lower() == "deep_reasoning":
            dr_execution_by_call_id[tool_call_id] = {
                "iteration": call_dr_iteration or 1,
                "tool": str(
                    dr_tool_for_display
                    or outcome.tool_id
                    or tool_name
                    or "unknown_tool"
                ),
                "status": outcome.result.get("status")
                or ("success" if outcome.result.get("success") else "error"),
                "command": dr_command_display,
                "summary": str(compact_result_dict.get("summary") or "Tool executed.").strip(),
            }
        budget_consumed_call_ids.add(tool_call_id)

        from dataclasses import asdict

        tool_catalog_payload = {
            "entries": [asdict(entry) for entry in outcome.catalog],
            "capability": call_request.capability,
        }
        call_facts.metadata["tool_catalog"] = tool_catalog_payload
        tool_catalog_by_call_id[tool_call_id] = dict(tool_catalog_payload)

        observation_text = deps["project_trace_history_and_outbound_events_service"](
            interactive=call_interactive,
            facts=call_facts,
            outcome=outcome,
            compact_result_dict=compact_result_dict,
            result_for_metadata=result_for_metadata,
            graph_metadata=graph_metadata,
            action_record=action_record,
            approval_response=approval_response,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_batch_id=tool_batch.tool_batch_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            sub_turn_index=step_sub_turn_index,
            interrupt_id=interrupt_id,
            has_writer=has_writer,
            writer=writer,
            compact_observation_text_fn=deps["_compact_observation_text"],
            tool_execution_record_cls=deps["ToolExecutionRecord"],
            store_dispatch_cache_result_fn=deps["store_dispatch_cache_result_service"],
            tool_dispatch_cache_key=deps["_TOOL_DISPATCH_CACHE_KEY"],
            diag_info_fn=deps["_diag_info"],
            logger=deps["logger"],
            deterministic_compact_result_dict=(
                deterministic_compact_result
                if isinstance(deterministic_compact_result, Mapping)
                else None
            ),
        )

        observation_by_call_id[tool_call_id] = observation_text
        trace_delta_by_call_id[tool_call_id] = _collect_trace_delta(
            call_interactive,
            base_lengths=trace_base_lengths,
        )
        metadata_patch_by_call_id[tool_call_id] = _collect_call_metadata_patch(
            call_facts.metadata,
        )
        cache_entry = _read_dispatch_cache_entry(
            call_facts.metadata,
            deps["_TOOL_DISPATCH_CACHE_KEY"],
            tool_call_id,
        )
        if cache_entry is not None:
            dispatch_cache_entry_by_call_id[tool_call_id] = cache_entry

        success_value = bool(outcome.result.get("success", False))
        projected_status = _project_tool_call_status(
            success=success_value,
            status=outcome.result.get("status"),
        )
        failure_category = None
        if not success_value:
            failure_category = (
                str(outcome.result.get("status") or "tool_error")
                if isinstance(outcome.result, dict)
                else "tool_error"
            )
        return ToolCallResult(
            tool_call_id=tool_call_id,
            tool_id=str(outcome.tool_id or tool_name),
            status=projected_status,
            duration_ms=int(float(outcome.duration or 0) * 1000),
            raw_result=dict(outcome.result or {}),
            failure_category=failure_category,
            error_message=str(outcome.result.get("stderr") or outcome.result.get("error") or "")
            if not success_value
            else None,
        )

    return run_one_call
