"""Unified post-tool reasoning node for deep reasoning flows.

This module replaces the fragmented observation_articulation → decision_router
path with a single coherent LLM call that:
1. Reasons about tool output
2. Produces observation text (streamed to frontend)
3. Determines next action based on that observation

This ensures what the agent "says it will do" in observations
actually drives what happens next.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ...infrastructure.state_models import GraphRuntimeContext
from ...context.runtime_state import sync_target_hint_from_plan_todo
from core.prompts.builders.post_tool import PostToolReasoningPromptBuilder
from ...state import InteractiveState
from ...utils.llm_resolver import (
    ROLE_POST_TOOL_OBSERVATION,
    resolve_llm_call_settings,
    resolve_llm_client,
    get_llm_reasoning_effort,
)
from ...emission.factory import EventEmitterFactory
from ...utils.environment_loader import get_environment_full
from ...utils.todo_stall_guard import (
    TODO_STALL_METADATA_KEY,
    apply_active_todo_stall_guard,
)
from ..working_memory import (
    apply_post_tool_active_decision,
    apply_post_tool_candidate_findings,
)
from ...memory.findings import build_relevant_findings_for_prompt
from ...utils.event_identity import (
    reserve_post_action_sub_turn_index,
    resolve_turn_sequence,
)
from ...utils import iteration_memory as _iteration_memory
from agent.providers.llm.core.exceptions import (
    LLMConfigurationError,
    LLMProviderError,
    LLMRefusalError,
)
from agent.providers.llm.contracts.tool_contracts import ToolChoice
from agent.subagents.registry import get_subagent_registry
from agent.tools.capability_surface import render_capability_surface
from agent.graph.config.token_limits import LIMITS
from backend.services.metrics.utils import safe_gauge, safe_inc
from backend.services.usage_tracking.models import ProviderUsageComponents, UsageData
from backend.services.usage_tracking.pricing import (
    aggregate_pricing_statuses,
    calculate_cost,
    pricing_status_for_usage,
)

# Import models from the models submodule
from .models import (
    DIRECT_TOOL_OUTCOME_SOURCE,
    POST_ACTION_OUTCOME_SOURCE_METADATA_KEY,
    PostToolReasoningError,
    PostToolReasoningDecisionOutput,
    PostToolReasoningOutput,
    RetryablePostToolReasoningError,
    SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE,
    VALID_POST_ACTION_OUTCOME_SOURCES,
    map_decision_output_to_post_tool_reasoning_output,
)

# Import progress functions from the progress submodule
from .progress import (
    apply_progress_updates as _apply_progress_updates,
    build_progress_summary as _build_progress_summary,
)

# Import recorders from the recorders submodule
from .recorders import (
    format_tool_intent_for_hint as _format_tool_intent_for_hint,
    record_decision as _record_decision,
    record_observation as _record_observation,
)

# Import core logic (capability-agnostic)
from .core import (
    build_failure_context_from_state,
    detect_failure,
    get_retry_count,
    can_retry,
    increment_retry_count,
    analyze_tool_result,
)
from .route_tools import build_post_tool_commit_tool, parse_post_tool_commit_call

# Import streaming adapters
from .streaming import StreamingAdapterFactory
from .streaming.base import STREAMING_STEP_NAME

# Modular policy package — sole owners of the post-tool decision overrides.
# The intent-contract evaluator stays as informational metadata for the
# next-turn LLM prompt; the override policy that previously coerced LLM
# ``finalize`` decisions to ``call_tool`` has been removed (LLM is the sole
# authority for intent classification).
from .policies.capability_guardrails import _enforce_simple_tool_single_step_policy
from .policies.direct_executor import apply_direct_executor_policy
from .policies.intent_contract import _evaluate_simple_tool_intent_contract
from .policies.request_contract import _apply_request_contract_policy

try:
    from langgraph.types import StreamWriter
except ImportError:  # pragma: no cover - typing fallback
    StreamWriter = Any  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

# Compatibility seam for focused policy tests that replace the historical
# decision function. The wired runtime never enters this branch.
_DEFAULT_ANALYZE_TOOL_RESULT = analyze_tool_result

_COMPLETED_AGENT_RESULTS_METADATA_KEY = "completed_agent_results"
_ACTIVE_AGENT_RUNS_METADATA_KEY = "active_agent_runs"
_CONTEXT_BUNDLE_METADATA_KEY = "context_bundle"


def _metric_segment(value: str) -> str:
    """Return a bounded metric-name segment for closed PAR enum values."""
    return value.strip().lower().replace(" ", "_") or "unknown"


def _resolve_outcome_source(metadata: Mapping[str, Any]) -> str:
    """Return deterministic PAR outcome source from serializable metadata."""
    raw_source = metadata.get(POST_ACTION_OUTCOME_SOURCE_METADATA_KEY)
    if raw_source in (None, ""):
        return DIRECT_TOOL_OUTCOME_SOURCE
    if not isinstance(raw_source, str):
        raise PostToolReasoningError(
            "post_action_outcome_source must be a string",
            error_code="invalid_post_action_outcome_source",
            diagnostics={
                "metadata_key": POST_ACTION_OUTCOME_SOURCE_METADATA_KEY,
                "actual_type": type(raw_source).__name__,
            },
        )
    source = raw_source.strip()
    if source not in VALID_POST_ACTION_OUTCOME_SOURCES:
        raise PostToolReasoningError(
            f"Unknown post_action_outcome_source: {source}",
            error_code="invalid_post_action_outcome_source",
            diagnostics={
                "metadata_key": POST_ACTION_OUTCOME_SOURCE_METADATA_KEY,
                "value": source,
                "valid_values": sorted(VALID_POST_ACTION_OUTCOME_SOURCES),
            },
        )
    return source


def _has_completed_agent_results(metadata: Mapping[str, Any]) -> bool:
    """Return whether metadata carries bounded completed subagent results."""
    results = metadata.get(_COMPLETED_AGENT_RESULTS_METADATA_KEY)
    if isinstance(results, list) and results:
        return True

    bundle = metadata.get(_CONTEXT_BUNDLE_METADATA_KEY)
    if isinstance(bundle, Mapping):
        bundle_results = bundle.get(_COMPLETED_AGENT_RESULTS_METADATA_KEY)
        return isinstance(bundle_results, list) and bool(bundle_results)
    return False


def _has_active_agent_runs(metadata: Mapping[str, Any]) -> bool:
    """Return whether deterministic context carries active subagent runs."""
    runs = metadata.get(_ACTIVE_AGENT_RUNS_METADATA_KEY)
    if isinstance(runs, list) and runs:
        return True

    bundle = metadata.get(_CONTEXT_BUNDLE_METADATA_KEY)
    if isinstance(bundle, Mapping):
        bundle_runs = bundle.get(_ACTIVE_AGENT_RUNS_METADATA_KEY)
        return isinstance(bundle_runs, list) and bool(bundle_runs)
    return False


def _context_sequence_count(metadata: Mapping[str, Any], key: str) -> int:
    """Return bounded context item count from metadata or its context bundle."""
    value = metadata.get(key)
    if isinstance(value, list):
        return len(value)
    if isinstance(value, tuple):
        return len(value)

    bundle = metadata.get(_CONTEXT_BUNDLE_METADATA_KEY)
    if isinstance(bundle, Mapping):
        bundle_value = bundle.get(key)
        if isinstance(bundle_value, list):
            return len(bundle_value)
        if isinstance(bundle_value, tuple):
            return len(bundle_value)
    return 0


def _record_par_cycle_observed_context(
    *,
    task_id: int | None,
    outcome_source: str,
    metadata: Mapping[str, Any],
) -> None:
    """Record bounded PAR source and context-size telemetry."""
    source_segment = _metric_segment(outcome_source)
    completed_count = _context_sequence_count(
        metadata,
        _COMPLETED_AGENT_RESULTS_METADATA_KEY,
    )
    active_count = _context_sequence_count(metadata, _ACTIVE_AGENT_RUNS_METADATA_KEY)
    safe_inc(f"post_action_reasoning_cycle_source_{source_segment}")
    safe_gauge("post_action_reasoning_handoff_batch_size", completed_count)
    safe_gauge("post_action_reasoning_active_run_count", active_count)
    logger.info(
        "[POST_ACTION_REASONING] Cycle started task_id=%s source=%s "
        "handoff_batch_size=%s active_run_count=%s",
        task_id,
        outcome_source,
        completed_count,
        active_count,
    )


def _record_par_decision_route(
    *,
    task_id: int | None,
    outcome_source: str,
    action: str,
    user_goal_achieved: bool,
) -> None:
    """Record bounded PAR route telemetry without prompt or result content."""
    action_segment = _metric_segment(action)
    safe_inc(f"post_action_reasoning_decision_route_{action_segment}")
    if action == "finalize" or user_goal_achieved:
        safe_inc("post_action_reasoning_finalization_decisions")
    logger.info(
        "[POST_ACTION_REASONING] Decision route task_id=%s source=%s "
        "action=%s user_goal_achieved=%s",
        task_id,
        outcome_source,
        action,
        user_goal_achieved,
    )


def _validate_outcome_source_metadata(
    metadata: Mapping[str, Any],
    *,
    outcome_source: str,
    synthesized: Any,
) -> None:
    """Reject contradictory deterministic source metadata before LLM work."""
    if outcome_source != SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE:
        return
    if synthesized:
        raise PostToolReasoningError(
            "subagent_handoff_batch source cannot include synthesized direct-tool output",
            error_code="contradictory_post_action_outcome_source",
            diagnostics={
                "metadata_key": POST_ACTION_OUTCOME_SOURCE_METADATA_KEY,
                "source": outcome_source,
                "has_synthesized_output": True,
            },
        )
    if not _has_completed_agent_results(metadata):
        raise PostToolReasoningError(
            "subagent_handoff_batch source requires completed_agent_results",
            error_code="contradictory_post_action_outcome_source",
            diagnostics={
                "metadata_key": POST_ACTION_OUTCOME_SOURCE_METADATA_KEY,
                "source": outcome_source,
                "has_completed_agent_results": False,
            },
        )


def _prefix_reasoning(prefix: str, existing: str) -> str:
    """Return policy rationale without losing the LLM-authored reason."""
    current = str(existing or "").strip()
    if not current:
        return prefix
    return f"{prefix} Original PAR rationale: {current}"


def _apply_source_aware_decision_policy(
    interactive: InteractiveState,
    output: PostToolReasoningOutput,
    *,
    outcome_source: str,
) -> None:
    """Apply deterministic source-specific route guardrails before recording."""
    if (
        outcome_source == SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE
        and output.next_action == "wait_for_subagents"
        and not _has_active_agent_runs(interactive.facts.safe_metadata)
    ):
        output.next_action = "think_more"
        output.action_reasoning = _prefix_reasoning(
            (
                "PAR selected wait_for_subagents, but deterministic context "
                "contains no active subagent runs; continuing parent reasoning "
                "instead."
            ),
            output.action_reasoning,
        )
        safe_inc("post_action_reasoning_wait_without_active_run_coerced")


def _resolve_iteration_memory_prompt_context(
    metadata: Mapping[str, Any],
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Return turn-scoped iteration-memory prompt context for PTR prompts.

    This compatibility helper computes values that older PTR prompt-path tests
    assert directly. It is read-only and degrades to ``(None, None, None)``
    when runtime ``turn_sequence`` is not available.
    """
    turn_sequence = metadata.get("turn_sequence")
    if not isinstance(turn_sequence, int):
        return None, None, None

    # ``peek`` is explicitly non-mutating and reports the next phase that
    # would be reserved for this active turn.
    current_ptr_phase_sequence = _iteration_memory.peek_next_phase_sequence(
        dict(metadata),
        turn_sequence=turn_sequence,
    )
    latest_recorded_phase_sequence = _iteration_memory.latest_recorded_phase_sequence(
        dict(metadata),
        turn_sequence=turn_sequence,
    )
    return (
        turn_sequence,
        current_ptr_phase_sequence,
        latest_recorded_phase_sequence,
    )

def derive_dr_stream_identifiers(
    interactive: InteractiveState,
    config: Optional[Mapping[str, Any]],
    *,
    advance_iteration: bool = False,
) -> Tuple[str, str, int]:
    """Compatibility wrapper returning DR iteration-specific stream identifiers."""
    metadata = interactive.facts.metadata_copy()
    interactive.facts.metadata = metadata
    dr_meta = metadata.setdefault("dr_iteration_meta", {})

    if advance_iteration:
        iteration = int(dr_meta.get("counter") or 0) + 1
        dr_meta["counter"] = iteration
        dr_meta["active_iteration"] = iteration
    elif isinstance(dr_meta.get("active_iteration"), int):
        iteration = int(dr_meta["active_iteration"])
    else:
        iteration = int(dr_meta.get("counter") or 0) + 1
        dr_meta["counter"] = iteration
        dr_meta["active_iteration"] = iteration

    metadata["dr_iteration_meta"] = dr_meta
    conversation_id = interactive.facts.conversation_id or ""
    runtime_context = metadata.get("graph_runtime_context")
    runtime_turn_id = runtime_context.get("turn_id") if isinstance(runtime_context, Mapping) else None
    if runtime_turn_id is None and runtime_context is not None:
        runtime_turn_id = getattr(runtime_context, "turn_id", None)
    configurable = (config or {}).get("configurable") if isinstance(config, Mapping) else None
    config_turn_id = configurable.get("thread_id") if isinstance(configurable, Mapping) else None
    base_turn_id = metadata.get("turn_id") or runtime_turn_id or config_turn_id or f"lg-{interactive.facts.task_id}"
    return conversation_id, f"{base_turn_id}-dr-iter-{iteration}", iteration


# -----------------------------------------------------------------------------
# Retry/failure detection constants (imported from core)
# -----------------------------------------------------------------------------

# These are now imported from core.retry_logic, but keep local references for backward compatibility
from .core.retry_logic import MAX_RETRIES  # noqa: E402


def _make_fallback_observation(
    interactive: InteractiveState,
    decision_output: PostToolReasoningDecisionOutput,
) -> str:
    """Build a non-empty fallback observation when a separate articulation call
    is not yet executed.
    """
    metadata = interactive.facts.safe_metadata
    synthesized = metadata.get("synthesized_output")
    if isinstance(synthesized, Mapping):
        source_observation = synthesized.get("observation_text")
        if isinstance(source_observation, str) and source_observation.strip():
            return _ensure_min_length_observation(
                source_observation.strip(),
                decision_output,
            )

        summary = synthesized.get("summary")
        if isinstance(summary, str) and summary.strip():
            return _ensure_min_length_observation(
                summary.strip(),
                decision_output,
            )

    if decision_output.tool_intent is not None:
        details = [decision_output.tool_intent.description]
        if decision_output.tool_intent.target:
            details.append(f"target={decision_output.tool_intent.target}")
        if decision_output.tool_intent.focus:
            details.append(f"focus={decision_output.tool_intent.focus}")
        tool_focus = ", ".join(details)
        return (
            f"Decision: {decision_output.next_action}. "
            f"Reasoning: {decision_output.action_reasoning}. "
            f"Tool intent: {tool_focus}"
        )

    return (
        f"Decision: {decision_output.next_action}. "
        f"Reasoning: {decision_output.action_reasoning}"
    )


def _ensure_min_length_observation(
    observation: str,
    decision_output: PostToolReasoningDecisionOutput,
) -> str:
    """Ensure fallback observations satisfy model minimum length requirements."""
    base = str(observation or "").strip()
    if len(base) >= 10:
        return base

    parts = [
        base or "Tool result observed.",
        f"Action: {decision_output.next_action}.",
        f"Reasoning: {decision_output.action_reasoning}",
    ]

    if decision_output.tool_intent:
        intent_bits = [decision_output.tool_intent.description]
        if decision_output.tool_intent.target:
            intent_bits.append(f"target={decision_output.tool_intent.target}")
        if decision_output.tool_intent.focus:
            intent_bits.append(f"focus={decision_output.tool_intent.focus}")
        parts.append("Intent: " + ", ".join(intent_bits))

    return " ".join(parts).strip()


async def _generate_observation_text(
    llm_client: Any,
    system_prompt: str,
    user_prompt: str,
    *,
    interactive: InteractiveState,
    reasoning_effort: Optional[str] = None,
) -> str:
    """Generate plain-text observation via the dedicated articulation prompt."""
    if hasattr(llm_client, "chat_with_usage"):
        response_obj = await llm_client.chat_with_usage(
            system_prompt,
            user_prompt,
            temperature=0.3,
            max_tokens=MAX_OBSERVATION_TOKENS,
            reasoning_effort=reasoning_effort,
        )
        content = response_obj.content

        if interactive is not None and getattr(response_obj, "usage", None):
            from ..node_utils import append_usage_to_state

            append_usage_to_state(
                interactive,
                response_obj.usage,
                "post_tool_observation",
                request_mode="non_streaming",
            )
    else:
        content = await llm_client.chat(
            system_prompt,
            user_prompt,
            temperature=0.3,
            max_tokens=MAX_OBSERVATION_TOKENS,
            reasoning_effort=reasoning_effort,
        )

    observation = str(content or "").strip()
    if not observation:
        raise PostToolReasoningError("Articulation LLM returned empty observation")
    return observation


# -----------------------------------------------------------------------------
# Wrapper functions for core logic (for backward compatibility with prompt builder)
# -----------------------------------------------------------------------------

def _detect_tool_failure(state: InteractiveState) -> Tuple[bool, Optional[str]]:
    """Detect if tool execution failed and classify failure type.
    
    Wrapper around core.detect_failure for backward compatibility.
    
    Returns:
        Tuple of (failure_detected, failure_category)
    """
    failure_ctx = build_failure_context_from_state(state)
    failure_detected, category = detect_failure(failure_ctx)
    
    if failure_detected:
        safe_inc("post_tool_reasoning_failures_detected")
    
    return failure_detected, category


def _get_retry_count(state: InteractiveState) -> int:
    """Get current retry attempt count from state.
    
    Wrapper around core.get_retry_count for backward compatibility.
    """
    metadata = state.facts.safe_metadata
    return get_retry_count(metadata)


def _increment_retry_count_in_state(state: InteractiveState) -> None:
    """Increment retry count in state (in-place modification).
    
    Note: This modifies state in-place, unlike the core.increment_retry_count
    which returns a new dict. This wrapper maintains backward compatibility.
    """
    metadata = state.facts.metadata
    if metadata is None:
        metadata = {}
        state.facts.metadata = metadata

    updated = increment_retry_count(metadata)
    if updated is not metadata:
        metadata.clear()
        metadata.update(updated)
    new_metadata = metadata
    state.facts.metadata = new_metadata
    
    retry_count = get_retry_count(new_metadata)
    logger.info(f"[POST_TOOL_REASONING] Incremented retry count to {retry_count}")
    safe_inc("post_tool_reasoning_retry_attempts")


def _can_retry(state: InteractiveState) -> bool:
    """Check if retry budget is available.
    
    Wrapper around core.can_retry for backward compatibility.
    """
    retry_count = _get_retry_count(state)
    can_retry_flag = can_retry(retry_count, MAX_RETRIES)
    
    if not can_retry_flag:
        safe_inc("post_tool_reasoning_retry_budget_exhausted")
    
    return can_retry_flag


def _extract_active_decision(metadata: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Return current active decision payload from working memory, if present."""
    working_memory = metadata.get("working_memory")
    if not isinstance(working_memory, Mapping):
        return None
    active_decision = working_memory.get("active_decision")
    if not isinstance(active_decision, Mapping):
        return None
    return dict(active_decision)


def _build_relevant_findings_for_prompt(interactive: InteractiveState) -> List[Dict[str, Any]]:
    """Select relevant prior findings for PTR prompts from canonical working memory."""
    return build_relevant_findings_for_prompt(interactive)


def _build_todo_delta(output: PostToolReasoningOutput) -> List[Dict[str, Any]]:
    """Build compact todo delta payload from structured PTR output."""
    delta: List[Dict[str, Any]] = []
    for item in output.todo_progress or []:
        entry: Dict[str, Any] = {
            "index": int(item.index),
            "status": str(item.status),
        }
        if item.completion_type is not None:
            entry["completion_type"] = str(item.completion_type)
        if item.completion_reason:
            entry["completion_reason"] = str(item.completion_reason)
        delta.append(entry)
    return delta


def _has_terminal_todo_delta(output: PostToolReasoningOutput) -> bool:
    """Return True when current iteration conclusively resolved todo items."""
    return any(item.status in ("completed", "skipped") for item in output.todo_progress or [])


def _build_active_decision(
    *,
    output: PostToolReasoningOutput,
    iteration: int,
    status: str,
    status_reason: str,
) -> Dict[str, Any]:
    """Map structured PTR output to advisory active-decision contract."""
    return {
        "source": "post_tool_reasoning",
        "authority": "llm_proposal",
        "status": status,
        "status_reason": status_reason,
        "iteration": int(iteration),
        "next_action": output.next_action,
        "tool_intent": output.tool_intent.model_dump() if output.tool_intent else None,
        "effective_next_goal": output.effective_next_goal,
        "action_reasoning": output.action_reasoning,
        "todo_delta": _build_todo_delta(output),
    }


def _update_active_decision_memory(
    interactive: InteractiveState,
    output: PostToolReasoningOutput,
) -> None:
    """Apply active-decision lifecycle updates to canonical working memory."""
    metadata = interactive.facts.safe_metadata
    current = _extract_active_decision(metadata)
    payload: Optional[Dict[str, Any]] = None

    if output.next_action == "call_tool" and output.tool_intent:
        payload = _build_active_decision(
            output=output,
            iteration=interactive.facts.iterations,
            status="active",
            status_reason="call_tool_decision",
        )
    elif output.next_action == "finalize":
        if current is None:
            return
        payload = None
    elif current is not None:
        updated = dict(current)
        if _has_terminal_todo_delta(output):
            updated["status"] = "resolved"
            updated["status_reason"] = "todo_terminal_update"
            updated["todo_delta"] = _build_todo_delta(output)
        elif output.effective_next_goal:
            previous_goal = str(current.get("effective_next_goal") or "").strip()
            next_goal = str(output.effective_next_goal).strip()
            if previous_goal and previous_goal != next_goal:
                updated["status"] = "superseded"
                updated["status_reason"] = "goal_phase_changed"
            else:
                updated["status"] = "superseded"
                updated["status_reason"] = "next_action_changed"
        else:
            updated["status"] = "superseded"
            updated["status_reason"] = "next_action_changed"
        payload = updated
    else:
        return

    apply_post_tool_active_decision(interactive, payload)


def _build_post_tool_candidate_payload(
    decision_output: PostToolReasoningDecisionOutput,
) -> Optional[Dict[str, Any]]:
    """Convert optional post-tool candidate rows into ingestion payload shape."""
    rows = decision_output.candidate_observations
    if rows is None:
        return None
    candidate_rows = [row.to_payload_dict() for row in rows]
    return {
        "candidate_observations": candidate_rows,
        "analyst_notes": [],
        "no_signal": len(candidate_rows) == 0,
    }


def _safe_usage_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _usage_data_from_trace_record(record: Mapping[str, Any]) -> UsageData:
    prompt_tokens = _safe_usage_int(record.get("prompt_tokens"))
    completion_tokens = _safe_usage_int(record.get("completion_tokens"))
    total_tokens = _safe_usage_int(record.get("total_tokens")) or (
        prompt_tokens + completion_tokens
    )
    return UsageData(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        model=str(record.get("model") or "unknown"),
        provider=str(record.get("provider") or "openai").strip().lower() or "openai",
        cached_tokens=_safe_usage_int(record.get("cached_tokens")),
        reasoning_tokens=_safe_usage_int(record.get("reasoning_tokens")),
        api_surface=str(record.get("api_surface") or "unknown").strip().lower()
        or "unknown",
        cache_reporting=str(record.get("cache_reporting") or "unknown").strip().lower()
        or "unknown",
        provider_usage_components=ProviderUsageComponents.from_mapping(
            record.get("provider_usage_components")
        ),
    )


def _merge_provider_usage_components(
    current: Dict[str, Any] | None,
    usage: UsageData,
) -> Dict[str, Any] | None:
    components = usage.provider_usage_components
    if components is None:
        return current
    serialized = components.to_dict()
    if current is None:
        return serialized
    if (
        current.get("provider") != serialized.get("provider")
        or current.get("api_surface") != serialized.get("api_surface")
    ):
        return None
    merged = dict(current)
    merged_components = dict(merged.get("components") or {})
    for key, value in dict(serialized.get("components") or {}).items():
        merged_components[str(key)] = _safe_usage_int(
            merged_components.get(str(key))
        ) + _safe_usage_int(value)
    merged["components"] = merged_components
    return merged


def _extract_post_tool_decision_usage_summary(
    interactive: InteractiveState,
    *,
    start_index: int,
) -> Optional[Dict[str, Any]]:
    """Summarize usage records created by post-tool decision analysis call."""
    records = list(interactive.trace.usage_records or [])
    if start_index >= len(records):
        return None

    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    cached_tokens = 0
    reasoning_tokens = 0
    matched_usages: List[UsageData] = []
    provider_usage_components: Dict[str, Any] | None = None
    component_merge_valid = True
    for record in records[start_index:]:
        if not isinstance(record, Mapping):
            continue
        source = str(record.get("source") or "").strip()
        if source != "post_tool_analysis":
            continue
        usage = _usage_data_from_trace_record(record)
        matched_usages.append(usage)
        prompt_tokens += usage.prompt_tokens
        completion_tokens += usage.completion_tokens
        total_tokens += usage.total_tokens
        cached_tokens += usage.cached_tokens
        reasoning_tokens += usage.reasoning_tokens
        if component_merge_valid:
            merged_components = _merge_provider_usage_components(
                provider_usage_components,
                usage,
            )
            if (
                merged_components is None
                and usage.provider_usage_components is not None
            ):
                component_merge_valid = False
                provider_usage_components = None
            else:
                provider_usage_components = merged_components

    if not matched_usages:
        return None
    provider_model_surface = {
        (usage.provider, usage.model, usage.api_surface) for usage in matched_usages
    }
    if len(provider_model_surface) != 1:
        return None
    provider, model, api_surface = next(iter(provider_model_surface))
    pricing_status = aggregate_pricing_statuses(
        [pricing_status_for_usage(usage) for usage in matched_usages]
    )

    summary = {
        "input_tokens": max(0, prompt_tokens),
        "output_tokens": max(0, completion_tokens),
        "total_tokens": max(0, total_tokens),
        "estimated_cost_usd": sum(calculate_cost(usage) for usage in matched_usages),
        "pricing_status": pricing_status,
        "provider": provider,
        "model": model,
        "api_surface": api_surface,
        "cached_tokens": max(0, cached_tokens),
        "reasoning_tokens": max(0, reasoning_tokens),
    }
    if component_merge_valid and provider_usage_components is not None:
        summary["provider_usage_components"] = provider_usage_components
    return summary


# Backward compatibility alias
_increment_retry_count = _increment_retry_count_in_state


# -----------------------------------------------------------------------------
# Constants (streaming/history/parser constants imported from submodules)
# -----------------------------------------------------------------------------

MAX_OBSERVATION_TOKENS = 400

# Progress tracking constants
MAX_TODOS_IN_PROMPT = 10  # Maximum todos to include in prompt (prevent context bloat)


# -----------------------------------------------------------------------------
# Main Node Function
# -----------------------------------------------------------------------------


async def post_tool_reasoning(
    state: Mapping[str, Any] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Mapping[str, Any]] = None,
    writer: Optional[StreamWriter] = None,
) -> Dict[str, Any]:
    """Unified post-tool reasoning: observe results and decide next action.
    
    This node replaces the fragmented observation_articulation → decision_router
    path with a single coherent LLM call. It:
    
    1. Builds context from conversation history and tool output
    2. Makes ONE LLM call that produces observation + decision
    3. Streams observation to frontend (if writer provided)
    4. Records observation to trace.observations
    5. Records decision to decision_history (drives graph routing)
    6. Updates history for next iteration context
    
    The key guarantee: what the LLM says it will do in the observation
    is the same as what actually happens next (because both come from
    the same response).
    
    Args:
        state: Current graph state (Mapping or InteractiveState).
        context: Runtime context with API key, model, workspace path.
        config: Optional LangGraph config.
        writer: StreamWriter for streaming observation to frontend.
        
    Returns:
        State update dict with observation and decision recorded.
        
    Raises:
        PostToolReasoningError: If LLM call fails or response is invalid.
        LLMConfigurationError: If no API key is available.
    """
    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    metadata = facts.metadata_copy()
    facts.metadata = metadata
    sync_target_hint_from_plan_todo(
        metadata,
        todo_list=list(facts.safe_todo_list),
        plan=list(facts.plan or []),
        current_goal=facts.current_goal,
    )
    
    # Get capability (defaults to simple_tool_execution if not set)
    capability = (facts.capability or "simple_tool_execution").lower()
    
    # Create streaming adapter for this capability
    try:
        adapter = StreamingAdapterFactory.create(capability)
    except ValueError as e:
        logger.warning(
            f"[POST_TOOL_REASONING] Unsupported capability '{capability}': {e}. "
            "Skipping post-tool reasoning."
        )
        return interactive.as_graph_update()
    
    outcome_source = _resolve_outcome_source(metadata)
    metadata[POST_ACTION_OUTCOME_SOURCE_METADATA_KEY] = outcome_source
    _record_par_cycle_observed_context(
        task_id=facts.task_id,
        outcome_source=outcome_source,
        metadata=metadata,
    )

    # Get synthesized tool output
    synthesized = metadata.get("synthesized_output") or {}
    _validate_outcome_source_metadata(
        metadata,
        outcome_source=outcome_source,
        synthesized=synthesized,
    )
    if outcome_source == DIRECT_TOOL_OUTCOME_SOURCE and not synthesized:
        logger.warning(
            "[POST_TOOL_REASONING] No synthesized_output in metadata. "
            "This may indicate tool execution failed or was skipped."
        )
        # Record a minimal observation about missing output
        interactive.trace.reasoning.append(
            "[POST_TOOL_REASONING] Missing synthesized_output; "
            "cannot produce observation."
        )
        return interactive.as_graph_update()

    if (
        outcome_source == DIRECT_TOOL_OUTCOME_SOURCE
        and capability == "simple_tool_execution"
    ):
        intent_contract = _evaluate_simple_tool_intent_contract(interactive)
        metadata["intent_contract_evaluation"] = intent_contract
    else:
        metadata.pop("intent_contract_evaluation", None)
    
    # Resolve one provider-neutral client for the combined visible observation
    # and internal route commitment.
    try:
        decision_llm_client = resolve_llm_client(
            metadata,
            context,
            config=config,
            role=ROLE_POST_TOOL_OBSERVATION,
        )
        decision_call_settings = resolve_llm_call_settings(
            metadata,
            context,
            role=ROLE_POST_TOOL_OBSERVATION,
        )
        decision_reasoning_effort = get_llm_reasoning_effort(
            decision_llm_client,
            decision_call_settings,
        )
    except LLMConfigurationError as e:
        logger.error(f"[POST_TOOL_REASONING] No API key available: {e}")
        raise  # Don't fall back - let caller handle

    # Build prompts
    prompt_builder = PostToolReasoningPromptBuilder()
    system_prompt = prompt_builder.build_route_system_prompt()
    
    if outcome_source == DIRECT_TOOL_OUTCOME_SOURCE:
        failure_detected, failure_category = _detect_tool_failure(interactive)
        retry_count = _get_retry_count(interactive)
        can_retry_flag = _can_retry(interactive)
    else:
        failure_detected = False
        failure_category = None
        retry_count = 0
        can_retry_flag = False
    failure_context = {
        "failure_detected": failure_detected,
        "failure_category": failure_category,
        "retry_count": retry_count,
        "can_retry": can_retry_flag,
        "max_retries": MAX_RETRIES,
    }
    environment_context = get_environment_full(metadata.get("environment_info"))

    # Eagerly emit observation_start for UI latency improvement.
    # Keep a single stream identity owner and suppress duplicate start in adapter.
    observation_emitter = None
    stream_turn_sequence = None
    stream_conversation_id = None
    stream_turn_id = None
    stream_sub_turn_index = None

    if writer is not None:
        stream_turn_sequence = resolve_turn_sequence(context, metadata)
        stream_ids = adapter.get_stream_identifiers(interactive, config)
        stream_conversation_id = stream_ids[0]
        stream_turn_id = stream_ids[1]
        stream_sub_turn_index = reserve_post_action_sub_turn_index(metadata)

        observation_emitter = EventEmitterFactory.create_from_identity(
            writer,
            stream_conversation_id,
            stream_turn_id,
            turn_sequence=stream_turn_sequence,
            sub_turn_index=stream_sub_turn_index,
        )
        observation_emitter.emit_observation_start(STREAMING_STEP_NAME)

    # One model turn: ordinary assistant text becomes the visible observation;
    # exactly one completed internal route tool becomes the graph decision.
    logger.info(
        f"[POST_TOOL_REASONING] Making LLM call for task {facts.task_id} "
        f"(iteration {facts.iterations}, streaming={writer is not None})"
    )
    decision_usage_start = len(interactive.trace.usage_records or [])
    decision_candidate_payload: Optional[Dict[str, Any]] = None
    decision_candidate_usage: Optional[Dict[str, Any]] = None
    relevant_findings = _build_relevant_findings_for_prompt(interactive)
    capability_surface = render_capability_surface()
    (
        turn_sequence,
        current_ptr_phase_sequence,
        latest_recorded_phase_sequence,
    ) = _resolve_iteration_memory_prompt_context(metadata)
    try:
        decision_user_prompt = prompt_builder.build_user_prompt(
            interactive=interactive,
            synthesized=synthesized,
            relevant_findings=relevant_findings,
            failure_context=failure_context,
            environment_context=environment_context,
            capability_surface=capability_surface,
            outcome_source=outcome_source,
            turn_sequence=turn_sequence,
            current_ptr_phase_sequence=current_ptr_phase_sequence,
            latest_recorded_phase_sequence=latest_recorded_phase_sequence,
        )
        decision_tools = [
            build_post_tool_commit_tool(get_subagent_registry().ids())
        ]
        if analyze_tool_result is not _DEFAULT_ANALYZE_TOOL_RESULT:
            legacy_output = await analyze_tool_result(
                llm_client=decision_llm_client,
                system_prompt=system_prompt,
                user_prompt=decision_user_prompt,
                interactive=interactive,
                reasoning_effort=decision_reasoning_effort,
            )
            if isinstance(legacy_output, PostToolReasoningOutput):
                observation = legacy_output.observation
                decision_output = PostToolReasoningDecisionOutput.model_validate(
                    legacy_output.model_dump(exclude={"observation"})
                )
            else:
                decision_output = PostToolReasoningDecisionOutput.model_validate(
                    legacy_output
                )
                observation = ""

            if writer is not None:
                observation, streamed, streaming_usage = (
                    await adapter.stream_observation_text(
                        writer=writer,
                        llm_client=decision_llm_client,
                        call_settings=decision_call_settings,
                        system_prompt=system_prompt,
                        user_prompt=decision_user_prompt,
                        conversation_id=stream_conversation_id,
                        turn_id=stream_turn_id,
                        sequence=stream_turn_sequence,
                        sub_turn_index=stream_sub_turn_index,
                        reasoning_effort=decision_reasoning_effort,
                        task_id=interactive.facts.task_id,
                        suppress_observation_start=True,
                    )
                )
                metadata["observation_streamed"] = streamed
                if streaming_usage is not None:
                    if interactive.trace.usage_records is None:
                        interactive.trace.usage_records = []
                    interactive.trace.usage_records.append(streaming_usage)
            elif not observation:
                observation = await _generate_observation_text(
                    decision_llm_client,
                    system_prompt,
                    decision_user_prompt,
                    interactive=interactive,
                    reasoning_effort=decision_reasoning_effort,
                )
            if writer is None:
                metadata["observation_streamed"] = False
        elif writer is not None:
            (
                observation,
                decision_output,
                streamed,
                streaming_usage,
            ) = await adapter.stream_observation_with_route(
                writer=writer,
                llm_client=decision_llm_client,
                call_settings=decision_call_settings,
                system_prompt=system_prompt,
                user_prompt=decision_user_prompt,
                route_tools=decision_tools,
                conversation_id=stream_conversation_id,
                turn_id=stream_turn_id,
                sequence=stream_turn_sequence,
                sub_turn_index=stream_sub_turn_index,
                reasoning_effort=decision_reasoning_effort,
                task_id=interactive.facts.task_id,
                suppress_observation_start=True,
            )
            metadata["observation_streamed"] = streamed
            if streaming_usage is not None:
                if interactive.trace.usage_records is None:
                    interactive.trace.usage_records = []
                interactive.trace.usage_records.append(streaming_usage)
        else:
            route_result = await decision_llm_client.chat_with_tools_with_usage(
                system_prompt,
                decision_user_prompt,
                decision_tools,
                tool_choice=ToolChoice(mode="required"),
                temperature=0.3,
                max_tokens=LIMITS.post_tool_reasoning,
                reasoning_effort=decision_reasoning_effort,
                parallel_tool_calls=False,
            )
            observation = str(route_result.content or "").strip()
            if not observation:
                raise PostToolReasoningError(
                    "LLM route turn returned no visible observation text"
                )
            decision_output = parse_post_tool_commit_call(route_result.tool_calls)
            if route_result.usage is not None:
                from ..node_utils import append_usage_to_state

                append_usage_to_state(
                    interactive,
                    route_result.usage,
                    "post_tool_analysis",
                    request_mode="non_streaming",
                )
            metadata["observation_streamed"] = False
        decision_candidate_payload = _build_post_tool_candidate_payload(decision_output)
        decision_candidate_usage = _extract_post_tool_decision_usage_summary(
            interactive,
            start_index=decision_usage_start,
        )
    except RetryablePostToolReasoningError as exc:
        configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
        graph_name = configurable.get("graph_name")
        if isinstance(graph_name, str) and graph_name.strip():
            exc.graph_name = graph_name.strip()
        if observation_emitter is not None:
            observation_emitter.emit_stream_error(
                error=f"Decision analysis failed: {exc}",
                recoverable=True,
                details={
                    "error_code": exc.error_code,
                    "retry_mode": exc.retry_mode,
                    "internal_only": True,
                },
            )
            observation_emitter.emit_observation_section_end(STREAMING_STEP_NAME)
        raise
    except (LLMConfigurationError, LLMProviderError, LLMRefusalError):
        if observation_emitter is not None:
            observation_emitter.emit_stream_error(
                error="Decision analysis failed at the configured LLM boundary",
                recoverable=False,
            )
            observation_emitter.emit_observation_section_end(STREAMING_STEP_NAME)
        raise
    except Exception as exc:
        if observation_emitter is not None:
            observation_emitter.emit_stream_error(
                error=f"Decision analysis failed: {exc}",
                recoverable=False,
            )
            observation_emitter.emit_observation_section_end(STREAMING_STEP_NAME)
        if isinstance(exc, PostToolReasoningError):
            raise
        failure_detail = (
            f"Route JSON parse failed: {exc}"
            if isinstance(exc, json.JSONDecodeError)
            else f"LLM call failed during post-tool route turn: {exc}"
        )
        raise PostToolReasoningError(failure_detail) from exc

    output = map_decision_output_to_post_tool_reasoning_output(
        decision_output,
        observation=observation,
    )

    # Handle retry suggestions (budgeted for both streaming and non-streaming)
    retry_suggested = (
        outcome_source == DIRECT_TOOL_OUTCOME_SOURCE
        and output.failure_detected
        and output.retry_suggested
    )
    retry_attempt_number: Optional[int] = None
    if retry_suggested:
        if not _can_retry(interactive):
            logger.info("[POST_TOOL_REASONING] Retry suggestion rejected - budget exhausted")
            output.retry_suggested = False
            retry_suggested = False
        else:
            retry_attempt_number = _get_retry_count(interactive) + 1
            _increment_retry_count_in_state(interactive)
            safe_inc("post_tool_reasoning_retry_suggested")

            if writer is not None:
                emitter = EventEmitterFactory.create(writer, interactive, config, context)

                emitter.emit_retry_start(
                    attempt=retry_attempt_number,
                    max_attempts=MAX_RETRIES + 1,
                    failure_category=output.failure_category,
                )

                emitter.emit_retry_attempt(
                    attempt=retry_attempt_number,
                    alternative_tool=(
                        output.tool_intent.description if output.tool_intent else None
                    ),
                    reasoning=output.action_reasoning[:200],
                )

                logger.info(
                    "[POST_TOOL_REASONING] Retry suggested: "
                    f"attempt={retry_attempt_number}, category={output.failure_category}"
                )

    # Direct-executor bounded continuation (goal_achieved / budget /
    # todos_terminal / repeated_no_progress). Runs after retry analysis so
    # post-retry state is visible, and before capability guardrails so the
    # single-step policy sees the coerced ``next_action``.
    if outcome_source == DIRECT_TOOL_OUTCOME_SOURCE:
        apply_direct_executor_policy(interactive, output)

    _apply_source_aware_decision_policy(
        interactive,
        output,
        outcome_source=outcome_source,
    )

    _enforce_simple_tool_single_step_policy(output, capability)
    _record_par_decision_route(
        task_id=facts.task_id,
        outcome_source=outcome_source,
        action=output.next_action,
        user_goal_achieved=output.user_goal_achieved,
    )
    retry_suggested = (
        outcome_source == DIRECT_TOOL_OUTCOME_SOURCE
        and output.failure_detected
        and output.retry_suggested
    )

    # Apply progress updates from LLM output and capture changed-only deltas.
    todo_updates = _apply_progress_updates(interactive, output)
    _apply_request_contract_policy(interactive, output)
    if apply_active_todo_stall_guard(interactive, output, todo_updates=todo_updates):
        stall_tracking = metadata.get(TODO_STALL_METADATA_KEY)
        forced_action = (
            stall_tracking.get("forced_action")
            if isinstance(stall_tracking, Mapping)
            else None
        )
        safe_inc(
            f"post_tool_reasoning_active_todo_stall_{forced_action or 'override'}"
        )

    # Record observation (updates trace.observations, metadata, history)
    _record_observation(
        interactive,
        output,
        write_synthesized_output=outcome_source == DIRECT_TOOL_OUTCOME_SOURCE,
    )

    # Record decision (updates decision_history, decision_log, reasoning)
    _record_decision(interactive, output)

    if writer is not None and todo_updates:
        todo_emitter = EventEmitterFactory.create(writer, interactive, config, context)
        todo_emitter.emit_todo_progress(
            todo_updates,
            run_id=stream_turn_sequence,
            plan_version=metadata.get("plan_version"),
        )
        logger.debug(
            "[POST_TOOL_REASONING] Emitted %d todo updates",
            len(todo_updates),
        )
    
    # Update effective goal if specified (advances the task phase)
    if output.effective_next_goal:
        facts.current_goal = output.effective_next_goal
        logger.info(f"[PROGRESS] Updated current_goal to: {output.effective_next_goal}")
    
    # Store goal achievement status for routing
    if output.user_goal_achieved:
        metadata["user_goal_achieved"] = True
        logger.info("[PROGRESS] User goal marked as achieved - will finalize")

    # Update canonical advisory decision memory for next PTR iteration context.
    _update_active_decision_memory(interactive, output)
    
    # Add progress summary to history for context
    progress_summary = _build_progress_summary(output)
    if progress_summary:
        metadata["last_progress_summary"] = progress_summary
        logger.debug(f"[PROGRESS] Summary: {progress_summary}")
    
    # Store structured tool intent for downstream planner
    # This uses the LLM's structured output instead of regex extraction
    if output.next_action == "call_tool" and output.tool_intent:
        # Format the structured intent as a hint string
        tool_hint = _format_tool_intent_for_hint(output.tool_intent)
        if tool_hint:
            facts.next_tool_hint = tool_hint
            metadata["next_tool_hint"] = tool_hint
            # Also store structured intent for advanced use cases
            metadata["tool_intent"] = output.tool_intent.model_dump()
            logger.info(
                f"[POST_TOOL_REASONING] Stored structured tool_intent: "
                f"desc='{output.tool_intent.description}', "
                f"target='{output.tool_intent.target}', "
                f"focus='{output.tool_intent.focus}'"
            )
    else:
        # Clear any previous hint to avoid stale data affecting next iteration
        facts.next_tool_hint = None
        metadata.pop("next_tool_hint", None)
        metadata.pop("tool_intent", None)
    
    # Store failure detection results for routing decisions
    if outcome_source == DIRECT_TOOL_OUTCOME_SOURCE and output.failure_detected:
        metadata["failure_detected"] = True
        metadata["failure_category"] = output.failure_category
        metadata["retry_suggested"] = output.retry_suggested
        logger.warning(
            "[POST_TOOL_REASONING] Tool failure detected: "
            f"category={output.failure_category}, retry_suggested={output.retry_suggested}"
        )
    else:
        metadata.pop("failure_detected", None)
        metadata.pop("failure_category", None)
        metadata.pop("retry_suggested", None)

    # Mark that this node processed the state
    metadata["post_tool_reasoning_completed"] = True
    metadata["last_post_tool_action"] = output.next_action
    if output.candidate_observations is not None:
        apply_post_tool_candidate_findings(
            interactive,
            [row.to_payload_dict() for row in output.candidate_observations],
        )

    execution_id = str(metadata.get("last_execution_id") or "").strip()
    compact_output = metadata.get("last_tool_result_compact")
    if (
        facts.task_id
        and execution_id
        and isinstance(compact_output, Mapping)
        and isinstance(decision_candidate_payload, Mapping)
    ):
        try:
            from ...subgraphs.tool_execution import _enqueue_execution_ingestion

            _enqueue_execution_ingestion(
                task_id=int(facts.task_id),
                execution_id=execution_id,
                tool_name=str(facts.selected_tool or "unknown_tool"),
                compact_output=dict(compact_output),
                post_tool_candidate_payload=dict(decision_candidate_payload),
                post_tool_candidate_usage=(
                    dict(decision_candidate_usage)
                    if isinstance(decision_candidate_usage, Mapping)
                    else None
                ),
            )
        except Exception as exc:
            logger.warning(
                "[KNOWLEDGE_INGESTION] Failed to enqueue post-tool candidate enrichment "
                "(task_id=%s execution_id=%s): %s",
                facts.task_id,
                execution_id,
                exc,
            )
            safe_inc("knowledge_ingestion_enqueue_failures")
    
    logger.info(
        f"[POST_TOOL_REASONING] Completed for task {facts.task_id}: "
        f"action={output.next_action}, "
        f"has_tool_intent={output.tool_intent is not None}"
    )
    
    return interactive.as_graph_update()
