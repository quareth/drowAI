"""LangGraph subgraph that runs planner/executor coordinator flows."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from langgraph.types import StreamWriter

from langgraph.config import get_stream_writer

logger = logging.getLogger(__name__)
try:
    from backend.services.langgraph_chat.diagnostic_logger import get_diagnostic_logger
    _diag_logger = get_diagnostic_logger()
except Exception:  # pragma: no cover - diagnostics unavailable
    _diag_logger = None

def _diag_info(message: str, *args: object) -> None:
    if _diag_logger is not None:
        _diag_logger.info(message, *args)

from agent.config import AgentConfig  # noqa: E402
from agent.models import Action  # noqa: E402
from agent.tool_runtime import ToolExecutionCoordinator, ToolExecutionRequest  # noqa: E402
from agent.tool_runtime.workspace_artifacts import (  # noqa: E402
    schedule_workspace_artifact_indexing as schedule_artifact_indexing_service,
    should_persist_workspace_artifact,
)
from agent.utils.artifact_manager import save_tool_output_artifact  # noqa: E402
from backend.services.metrics.utils import safe_gauge, safe_inc  # noqa: E402

from ..builders.common_edges import decrement_tool_call_budget  # noqa: E402
from ..compression import compact_output_size_bytes, compress_tool_output  # noqa: E402
from ..emission.factory import EventEmitterFactory  # noqa: E402
from ..infrastructure.state_models import GraphRuntimeContext  # noqa: E402
from ..memory.memory_manager import MemoryManager  # noqa: E402
from ..memory.scratchpad import refresh_trace_scratchpad  # noqa: E402
from ..state import InteractiveState, ToolExecutionRecord  # noqa: E402
from ..nodes.hitl_helpers import (  # noqa: E402
    normalize_tool_approval_response,
    request_tool_approval,
    should_require_approval,
)
from ..utils.llm_resolver import resolve_llm_client  # noqa: E402
from ..utils.event_identity import (  # noqa: E402
    resolve_direct_executor_step_index,
    resolve_stream_identifiers,
    resolve_turn_sequence,
)
from ..utils.dr_iteration_state import record_dr_tool_execution  # noqa: E402
from ..streaming import _build_command_for_display, _get_tool_parameters_for_display  # noqa: E402
from .tool_execution_runtime.observability import (  # noqa: E402
    coerce_timestamp,
    emit_labeled_latency_metric,
    emit_hitl_stage as emit_hitl_stage_service,
    record_compression_observability_metrics,
    resolve_dr_iteration as resolve_dr_iteration_service,
    resolve_runtime_path_label,
)
from .tool_execution_runtime.request_context import build_request_and_coordinator_config  # noqa: E402
from .tool_execution_runtime.approval_and_idempotency import (  # noqa: E402
    apply_cached_dispatch_result as apply_cached_dispatch_result_service,
    build_skipped_tool_result as build_skipped_tool_result_service,
    clear_approval_gate_metadata as clear_approval_gate_metadata_service,
    clear_tool_plan_prepared_flag as clear_tool_plan_prepared_flag_service,
    get_tool_risk_level as get_tool_risk_level_service,
    handle_run_tool_execution_approval as handle_run_tool_execution_approval_service,
    maybe_return_cached_dispatch_update as maybe_return_cached_dispatch_update_service,
    store_dispatch_cache_result as store_dispatch_cache_result_service,
)
from .tool_execution_runtime.planner_service import (  # noqa: E402
    apply_plan_to_state as apply_plan_to_state_service,
    build_action_for_planner as build_action_for_planner_service,
    build_planner_context as build_planner_context_service,
    build_working_memory_context_for_planner as build_working_memory_context_for_planner_service,
    get_category_filtered_catalog as get_category_filtered_catalog_service,
    get_full_tool_catalog_for_planner as get_full_tool_catalog_for_planner_service,
    ensure_action_plan as ensure_action_plan_service,
    resolve_planner_target as resolve_planner_target_service,
)
from .tool_execution_runtime.artifact_and_provenance import (  # noqa: E402
    build_artifact_ref_label as build_artifact_ref_label_service,
    collect_persistable_tool_artifact_paths as collect_persistable_tool_artifact_paths_service,
    collect_provenance_artifact_refs as collect_provenance_artifact_refs_service,
    enrich_artifact_refs_with_provenance as enrich_artifact_refs_with_provenance_service,
    finalize_provenance_after_execution_error as finalize_provenance_after_execution_error_service,
    finalize_provenance_execution as finalize_provenance_execution_service,
    get_provenance_service as get_provenance_service_impl,
    normalize_artifact_ref_path as normalize_artifact_ref_path_service,
    path_lookup_keys as path_lookup_keys_service,
    record_provenance_execution_start as record_provenance_execution_start_service,
    resolve_execution_artifact_workspace as resolve_execution_artifact_workspace_service,
    save_execution_artifact as save_execution_artifact_service,
)
from .tool_execution_runtime.result_state_projection import (  # noqa: E402
    apply_result_state_projection as apply_result_state_projection_service,
    compact_observation_text as compact_observation_text_service,
    project_result_state as project_result_state_service,
    project_trace_history_and_outbound_events as project_trace_history_and_outbound_events_service,
    sanitize_tool_result_for_metadata as sanitize_tool_result_for_metadata_service,
)
from .tool_execution_runtime.orchestrator import (  # noqa: E402
    approval_gate_node_orchestrator,
    dispatch_tool_execution_node_orchestrator,
    prepare_tool_execution_plan_orchestrator,
    run_tool_execution_orchestrator,
)

def _get_provenance_service() -> Tuple[Optional[Any], Optional[Any]]:
    """Lazy import and create artifact provenance service.

    Returns (service, db_session) or (None, None) when backend/DB is unavailable.
    """
    return get_provenance_service_impl(logger=logger)


def _enqueue_execution_ingestion(
    *,
    task_id: int,
    execution_id: str,
    tool_name: str,
    compact_output: Mapping[str, Any] | None,
    post_tool_candidate_payload: Mapping[str, Any] | None = None,
    post_tool_candidate_usage: Mapping[str, Any] | None = None,
) -> None:
    """Queue non-blocking ingestion from the live LangGraph execution seam."""
    from backend.services.knowledge.ingestion_trigger_service import (
        enqueue_execution_ingestion,
    )

    enqueue_execution_ingestion(
        task_id=task_id,
        execution_id=execution_id,
        tool_name=tool_name,
        compact_output=compact_output,
        post_tool_candidate_payload=post_tool_candidate_payload,
        post_tool_candidate_usage=post_tool_candidate_usage,
    )


HIGH_RISK_TOOL_PREFIXES = ("shell.exec", "exploitation_tools.")
MEDIUM_RISK_TOOL_PREFIXES = (
    "information_gathering.network_discovery.nmap",
    "vulnerability_analysis.",
)
_COMPACT_SANITIZED_RESULT_KEYS = frozenset(
    {"tool", "status", "success", "exit_code", "duration", "parameters"}
)
_APPROVAL_GATE_COMPLETED_KEY = "tool_approval_gate_completed"
_APPROVAL_GATE_RESPONSE_KEY = "tool_approval_response"
_TOOL_CALL_ID_KEY = "tool_call_id"
_TOOL_DISPATCH_CACHE_KEY = "tool_dispatch_cache"
_PLANNER_WORKING_MEMORY_SUMMARY_MAX_CHARS = 3600


def _should_persist_artifact_outputs(tool_id: Optional[str]) -> bool:
    """Return whether execution outputs for this tool should be persisted as artifacts."""
    return should_persist_workspace_artifact(tool_id)


def _emit_hitl_stage(
    *,
    stage: str,
    timestamp: Optional[float],
    task_id: Optional[int],
    interrupt_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> None:
    """Emit standardized stage timestamps with HITL correlation identifiers."""
    emit_hitl_stage_service(
        stage=stage,
        timestamp=timestamp,
        task_id=task_id,
        logger=logger,
        interrupt_id=interrupt_id,
        tool_call_id=tool_call_id,
    )


def _resolve_dr_iteration(metadata: Mapping[str, Any]) -> int:
    """Return current DR iteration index from metadata."""
    return resolve_dr_iteration_service(metadata)


def _sanitize_tool_result_for_metadata(
    raw_result: Mapping[str, Any],
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Keep only compact-safe lifecycle fields for state metadata."""
    return sanitize_tool_result_for_metadata_service(
        raw_result,
        compact_sanitized_result_keys=tuple(_COMPACT_SANITIZED_RESULT_KEYS),
        tool_name=tool_name,
    )


def _resolve_planner_target(
    *,
    user_message: str,
    request_targets: Sequence[str],
    metadata: Mapping[str, Any],
    history: Sequence[Any],
    tool_intent: Mapping[str, Any],
) -> str:
    """Resolve planner target via extracted planner service."""
    return resolve_planner_target_service(
        user_message=user_message,
        request_targets=request_targets,
        metadata=metadata,
        history=history,
        tool_intent=tool_intent,
    )


def _compact_observation_text(
    compact_result: Mapping[str, Any],
    fallback: Optional[str] = None,
) -> str:
    """Return a compact, prompt-safe observation string."""
    return compact_observation_text_service(compact_result, fallback=fallback)


def _build_artifact_ref_label(
    *,
    artifact_kind: str,
    tool_name: str,
    turn_sequence: Optional[int],
    execution_id: Optional[str],
) -> str:
    """Build deterministic artifact label consistent with catalog semantics."""
    return build_artifact_ref_label_service(
        artifact_kind=artifact_kind,
        tool_name=tool_name,
        turn_sequence=turn_sequence,
        execution_id=execution_id,
    )


def _normalize_artifact_ref_path(raw_path: Any) -> Optional[str]:
    """Normalize artifact path strings for lightweight lookup matching."""
    return normalize_artifact_ref_path_service(raw_path)


def _path_lookup_keys(raw_path: Any) -> List[str]:
    """Return normalized path keys including workspace-relative aliases."""
    return path_lookup_keys_service(raw_path)


def _collect_persistable_tool_artifact_paths(
    *,
    raw_artifacts: Any,
    synthetic_output_path: Optional[str],
) -> List[str]:
    """
    Return unique tool artifact paths suitable for provenance persistence.

    LangGraph may create a synthetic `<timestamp>_tool.txt` mirror artifact from
    stdout/stderr for indexing. Provenance already persists canonical command/stdout/stderr
    records, so this helper excludes that synthetic mirror from `tool_file` rows.
    """
    return collect_persistable_tool_artifact_paths_service(
        raw_artifacts=raw_artifacts,
        synthetic_output_path=synthetic_output_path,
        path_lookup_keys_fn=_path_lookup_keys,
        normalize_artifact_ref_path_fn=_normalize_artifact_ref_path,
    )


def _collect_provenance_artifact_refs(
    *,
    persisted_artifacts: Sequence[Any],
    tool_name: str,
    tool_call_id: Optional[str],
    execution_id: Optional[str],
    turn_sequence: Optional[int],
) -> List[Dict[str, Any]]:
    """Build compact metadata-first refs from persisted artifact provenance rows."""
    return collect_provenance_artifact_refs_service(
        persisted_artifacts=persisted_artifacts,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        execution_id=execution_id,
        turn_sequence=turn_sequence,
        build_artifact_ref_label_fn=_build_artifact_ref_label,
    )


def _enrich_artifact_refs_with_provenance(
    *,
    refs: Sequence[Mapping[str, Any]],
    provenance_refs: Sequence[Mapping[str, Any]],
    tool_name: str,
    tool_call_id: Optional[str],
    execution_id: Optional[str],
    turn_sequence: Optional[int],
) -> List[Dict[str, Any]]:
    """Enrich compact artifact refs with stable provenance metadata fields."""
    return enrich_artifact_refs_with_provenance_service(
        refs=refs,
        provenance_refs=provenance_refs,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        execution_id=execution_id,
        turn_sequence=turn_sequence,
        path_lookup_keys_fn=_path_lookup_keys,
        build_artifact_ref_label_fn=_build_artifact_ref_label,
    )


def _build_orchestrator_deps() -> Dict[str, Any]:
    """Assemble runtime dependencies for extracted orchestration entrypoints."""
    return {
        "InteractiveState": InteractiveState,
        "ToolExecutionCoordinator": ToolExecutionCoordinator,
        "ToolExecutionRecord": ToolExecutionRecord,
        "logger": logger,
        "safe_inc": safe_inc,
        "safe_gauge": safe_gauge,
        "get_stream_writer": get_stream_writer,
        "resolve_stream_identifiers": resolve_stream_identifiers,
        "resolve_turn_sequence": resolve_turn_sequence,
        "build_request_and_coordinator_config": build_request_and_coordinator_config,
        "_ensure_action_plan": _ensure_action_plan,
        "_resolve_dr_iteration": _resolve_dr_iteration,
        "_emit_hitl_stage": _emit_hitl_stage,
        "_apply_cached_dispatch_result": _apply_cached_dispatch_result,
        "_clear_tool_plan_prepared_flag": _clear_tool_plan_prepared_flag,
        "_clear_approval_gate_metadata": _clear_approval_gate_metadata,
        "_get_tool_risk_level": _get_tool_risk_level,
        "_get_provenance_service": _get_provenance_service,
        "_should_persist_artifact_outputs": _should_persist_artifact_outputs,
        "_build_command_for_display": _build_command_for_display,
        "_get_tool_parameters_for_display": _get_tool_parameters_for_display,
        "_collect_persistable_tool_artifact_paths": _collect_persistable_tool_artifact_paths,
        "_collect_provenance_artifact_refs": _collect_provenance_artifact_refs,
        "_enrich_artifact_refs_with_provenance": _enrich_artifact_refs_with_provenance,
        "_compact_observation_text": _compact_observation_text,
        "_diag_info": _diag_info,
        "_APPROVAL_GATE_COMPLETED_KEY": _APPROVAL_GATE_COMPLETED_KEY,
        "_APPROVAL_GATE_RESPONSE_KEY": _APPROVAL_GATE_RESPONSE_KEY,
        "_TOOL_CALL_ID_KEY": _TOOL_CALL_ID_KEY,
        "_TOOL_DISPATCH_CACHE_KEY": _TOOL_DISPATCH_CACHE_KEY,
        "_COMPACT_SANITIZED_RESULT_KEYS": _COMPACT_SANITIZED_RESULT_KEYS,
        "coerce_timestamp": coerce_timestamp,
        "resolve_runtime_path_label": resolve_runtime_path_label,
        "emit_labeled_latency_metric": emit_labeled_latency_metric,
        "record_compression_observability_metrics": record_compression_observability_metrics,
        "normalize_tool_approval_response": normalize_tool_approval_response,
        "request_tool_approval": request_tool_approval,
        "should_require_approval": should_require_approval,
        "maybe_return_cached_dispatch_update_service": maybe_return_cached_dispatch_update_service,
        "handle_run_tool_execution_approval_service": handle_run_tool_execution_approval_service,
        "EventEmitterFactory": EventEmitterFactory,
        "record_dr_tool_execution": record_dr_tool_execution,
        "record_provenance_execution_start_service": record_provenance_execution_start_service,
        "finalize_provenance_after_execution_error_service": finalize_provenance_after_execution_error_service,
        "resolve_execution_artifact_workspace_service": resolve_execution_artifact_workspace_service,
        "save_execution_artifact_service": save_execution_artifact_service,
        "schedule_artifact_indexing_service": schedule_artifact_indexing_service,
        "finalize_provenance_execution_service": finalize_provenance_execution_service,
        "project_result_state_service": project_result_state_service,
        "apply_result_state_projection_service": apply_result_state_projection_service,
        "project_trace_history_and_outbound_events_service": project_trace_history_and_outbound_events_service,
        "refresh_trace_scratchpad": refresh_trace_scratchpad,
        "resolve_llm_client": resolve_llm_client,
        "compress_tool_output": compress_tool_output,
        "compact_output_size_bytes": compact_output_size_bytes,
        "MemoryManager": MemoryManager,
        "decrement_tool_call_budget": decrement_tool_call_budget,
        "save_tool_output_artifact": save_tool_output_artifact,
        "store_dispatch_cache_result_service": store_dispatch_cache_result_service,
        "resolve_direct_executor_step_index": resolve_direct_executor_step_index,
    }


async def run_tool_execution(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Dict[str, Any]] = None,
    writer: Optional["StreamWriter"] = None,
) -> dict:
    return await run_tool_execution_orchestrator(
        state,
        context=context,
        config=config,
        writer=writer,
        deps=_build_orchestrator_deps(),
    )


async def approval_gate_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Dict[str, Any]] = None,
) -> dict:
    return await approval_gate_node_orchestrator(
        state,
        context=context,
        config=config,
        deps=_build_orchestrator_deps(),
    )


async def dispatch_tool_execution_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Dict[str, Any]] = None,
    writer: Optional["StreamWriter"] = None,
) -> dict:
    deps = _build_orchestrator_deps()
    deps["run_tool_execution_fn"] = run_tool_execution
    return await dispatch_tool_execution_node_orchestrator(
        state,
        context=context,
        config=config,
        writer=writer,
        deps=deps,
    )


async def prepare_tool_execution_plan(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Dict[str, Any]] = None,
    writer: Optional["StreamWriter"] = None,
) -> dict:
    return await prepare_tool_execution_plan_orchestrator(
        state,
        context=context,
        config=config,
        writer=writer,
        deps=_build_orchestrator_deps(),
    )


def _build_skipped_tool_result(
    interactive: InteractiveState,
    tool_name: str,
    user_response: Dict[str, Any],
) -> dict:
    return build_skipped_tool_result_service(interactive, tool_name, user_response)


def _get_tool_risk_level(tool_id: str) -> str:
    """Get risk level for a tool."""
    return get_tool_risk_level_service(
        tool_id,
        high_risk_prefixes=HIGH_RISK_TOOL_PREFIXES,
        medium_risk_prefixes=MEDIUM_RISK_TOOL_PREFIXES,
    )


def _apply_cached_dispatch_result(
    interactive: InteractiveState,
    cached: Dict[str, Any],
    tool_name: str,
) -> None:
    """Apply cached tool dispatch result for idempotent return (no re-execution)."""
    apply_cached_dispatch_result_service(interactive, cached, tool_name)


def _clear_tool_plan_prepared_flag(interactive: InteractiveState) -> None:
    """Clear one-shot preplan marker after tool node successfully returns.

    The marker must survive until after HITL approval interrupt/resume so the
    resumed node can keep reusing the precomputed planner output.
    """
    clear_tool_plan_prepared_flag_service(interactive)


def _clear_approval_gate_metadata(interactive: InteractiveState) -> None:
    """Clear one-shot approval gate markers after dispatch finishes."""
    clear_approval_gate_metadata_service(
        interactive,
        approval_gate_completed_key=_APPROVAL_GATE_COMPLETED_KEY,
        approval_gate_response_key=_APPROVAL_GATE_RESPONSE_KEY,
    )


async def _ensure_action_plan(
    interactive: InteractiveState,
    request: ToolExecutionRequest,
    config: AgentConfig,
) -> None:
    await ensure_action_plan_service(
        interactive,
        request,
        config,
        build_action_for_planner=_build_action_for_planner,
        build_planner_context=_build_planner_context,
    )


def _get_category_filtered_catalog(categories: List[str], config: Optional[AgentConfig]) -> List[str]:
    """Get tool catalog filtered to specific categories.
    
    This provides tools only from the specified categories, enabling
    focused tool selection after category_selector node.
    
    IMPORTANT: Shell and filesystem tools are ALWAYS included as utilities,
    regardless of selected categories. The LLM needs these to discover
    environment info (ip a, ifconfig, etc.) and manage files.
    
    Args:
        categories: List of category names to include
        config: Agent configuration with max_tools_exposed setting
    
    Returns:
        List of tool IDs from specified categories (limited by config)
    """
    return get_category_filtered_catalog_service(
        categories,
        config,
        logger=logger,
        get_full_tool_catalog_for_planner_fn=_get_full_tool_catalog_for_planner,
    )


def _get_full_tool_catalog_for_planner(config: Optional[AgentConfig]) -> List[str]:
    """Get complete tool catalog for LLM-based selection.
    
    This provides all available tools to the planner without capability filtering,
    enabling true LLM-centric tool selection.
    
    Args:
        config: Agent configuration with max_tools_exposed setting
    
    Returns:
        List of tool IDs (limited by config for token budget)
    """
    return get_full_tool_catalog_for_planner_service(config, logger=logger)


def _build_action_for_planner(interactive: InteractiveState, request: ToolExecutionRequest) -> Action:
    """Build Action for planner with LLM-centric tool selection.
    
    IMPLEMENTATION STATUS: ✅ FULLY LLM-CENTRIC
    - Pattern matching REMOVED from enhanced_planner.py
    - Full tool catalog provided in context (no capability filtering)
    - LLM receives complete catalog and selects tools based on:
    1. User request / current goal
    2. Rich tool schema descriptions (Pydantic Field descriptions)
      3. Tool capabilities and use cases (enhanced metadata)
    
    No hardcoded pattern matching or capability gating.
    Tool selection is purely LLM reasoning.
    
    CRITICAL: Target priority is resolved in canonical target-resolution helper:
    1. classifier-resolved current-turn target
    2. continuity-authorized active target binding from canonical working memory
    3. current-turn request targets (intent_hints)
    4. current-iteration tool_intent.target fallback
    """
    action = build_action_for_planner_service(interactive, request)
    if action.target:
        logger.debug("[PLANNER] Resolved planner target: %s", action.target)
    else:
        logger.debug("[PLANNER] No concrete planner target resolved; continuing without explicit target")
    return action


def _build_planner_context(
    interactive: InteractiveState,
    request: ToolExecutionRequest,
) -> Dict[str, Any]:
    """Build context for planner with full tool catalog and conversation history.
    
    This function provides all necessary context for LLM-based tool selection,
    including the complete tool catalog (no capability filtering).
    
    Args:
        interactive: Current interactive state with facts and trace
        request: Tool execution request with message and metadata
    
    Returns:
        Context dict with message, history, tools, and constraints
    """
    return build_planner_context_service(
        interactive,
        request,
        get_category_filtered_catalog=_get_category_filtered_catalog,
        get_full_tool_catalog_for_planner=_get_full_tool_catalog_for_planner,
        working_memory_summary_max_chars=_PLANNER_WORKING_MEMORY_SUMMARY_MAX_CHARS,
    )


def _build_working_memory_context_for_planner(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    """Build bounded working-memory payload for planner prompts."""
    return build_working_memory_context_for_planner_service(
        metadata,
        max_summary_chars=_PLANNER_WORKING_MEMORY_SUMMARY_MAX_CHARS,
    )


def _apply_plan_to_state(interactive: InteractiveState, plan_data: Dict[str, Any]) -> None:
    apply_plan_to_state_service(interactive, plan_data)


__all__ = [
    "_TOOL_CALL_ID_KEY",
    "_TOOL_DISPATCH_CACHE_KEY",
    "prepare_tool_execution_plan",
    "approval_gate_node",
    "dispatch_tool_execution_node",
    "run_tool_execution",
]
