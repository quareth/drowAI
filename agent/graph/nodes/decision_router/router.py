"""Main decision router node for deterministic loop route authority."""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from agent.config import AgentConfig

from ...infrastructure.state_models import GraphRuntimeContext
from ...state import InteractiveState
from ...utils.event_identity import resolve_turn_sequence

# Import from submodules
from .guardrails import (
    safe_inc,
    are_scope_goals_achieved,
    check_budget_exhaustion,
    check_goal_completion,
    check_iteration_budget_warnings,
    check_no_progress,
    count_consecutive_reflections,
    check_redundant_execution,
    has_sufficient_findings,
    increment_stuck_counter,
    is_action_loop_detected,
    is_stuck_without_progress,
    log_progress_milestone,
)
from .helpers import (
    VALID_ACTIONS,
    check_all_todos_complete,
    consume_valid_candidate_decision,
    consume_post_reflect_hint,
    extract_action_label,
    get_current_todo,
    resolve_router_phase_sequence,
    update_router_observability,
    write_router_outcome,
)
from .pause import (
    build_pause_request,
    emit_and_wait_for_pause_response,
    should_pause_for_confirmation,
)

logger = logging.getLogger(__name__)


def _build_resolution(
    *,
    action: str,
    reason: str,
    candidate_action: Optional[str],
    candidate_source: str,
    resolution_source: str,
    append_history: bool,
) -> dict[str, Any]:
    """Return a normalized in-memory resolution payload."""
    return {
        "action": action,
        "reason": reason,
        "candidate_action": candidate_action,
        "candidate_source": candidate_source,
        "resolution_source": resolution_source,
        "append_history": append_history,
    }


# =============================================================================
# Decision Recording
# =============================================================================


def record_decision(
    interactive: InteractiveState,
    action: str,
    reasoning: str,
    *,
    append_history: bool = True,
) -> None:
    """Record decision to history and trace.
    
    Updates multiple state locations for consistency:
    - facts.decision_history: Appends "action: reasoning" entry for routing
    - trace.decision_log: Appends structured record for debugging
    - trace.reasoning: Appends visibility entry for logging
    - facts.stuck_counter: Updated via increment_stuck_counter
    
    Args:
        interactive: The InteractiveState to update (mutated in place).
        action: The decided action (e.g., "call_tool", "finalize").
        reasoning: Explanation for the decision.
    """
    # IMPORTANT: Update stuck counter BEFORE appending to history
    state_dict = interactive.as_graph_state()
    stuck_update = increment_stuck_counter(state_dict, action)
    
    # Apply stuck counter update
    if "facts" in stuck_update:
        interactive.facts.stuck_counter = stuck_update["facts"].get("stuck_counter", 0)
    
    # Record the decision in history only when router owns the fallback/override.
    if append_history:
        decision_entry = f"{action}: {reasoning}"
        interactive.facts.ensure_decision_history().append(decision_entry)
    
    # Add to decision log with structured data
    decision_record = {
        "iteration": interactive.facts.iterations,
        "action": action,
        "reasoning": reasoning,
        "stuck_counter": interactive.facts.stuck_counter,
    }
    interactive.trace.decision_log.append(decision_record)
    
    # Also add to reasoning trace for visibility
    interactive.trace.reasoning.append(f"Decision: {action} - {reasoning}")


def _route_with_outcome(
    interactive: InteractiveState,
    *,
    action: str,
    reason: str,
    candidate_action: Optional[str],
    candidate_source: str,
    resolution_source: str,
    append_history: bool,
) -> dict:
    """Write router contracts, record decision, and return graph update."""
    facts = interactive.facts
    outcome = write_router_outcome(
        facts,
        action=action,
        candidate_action=candidate_action,
        candidate_source=candidate_source,
        resolution_source=resolution_source,
        reason=reason,
        profile=str(facts.capability or ""),
    )
    record_decision(interactive, action, reason, append_history=append_history)
    update_router_observability(facts, outcome)
    return interactive.as_graph_update()


# =============================================================================
# Main Decision Router Function
# =============================================================================


async def decision_router(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> dict:
    """Route to the next action using deterministic candidate + guardrails."""
    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    reflect_recovery_resolution = _resolve_reflect_recovery_action(interactive, facts)
    
    # =========================================================================
    # PRIORITY CHECKS (handled before guardrails)
    # =========================================================================
    
    # PRIORITY 1: User goal achieved (set by post_tool_reasoning)
    is_complete, reason = check_goal_completion(facts, metadata)
    if is_complete:
        logger.info(f"[ROUTER] Finalizing: {reason}")
        safe_inc("router_finalize_goal_achieved")
        metadata.pop("user_goal_achieved", None)
        return _route_with_outcome(
            interactive,
            action="finalize",
            reason=reason,
            candidate_action=None,
            candidate_source="terminal_state",
            resolution_source="terminal_state",
            append_history=True,
        )
    
    # PRIORITY 2: All todos complete
    if check_all_todos_complete(facts):
        logger.info("[ROUTER] Finalizing: all todos marked complete")
        safe_inc("router_finalize_todos_complete")
        return _route_with_outcome(
            interactive,
            action="finalize",
            reason="All todos complete",
            candidate_action=None,
            candidate_source="terminal_state",
            resolution_source="terminal_state",
            append_history=True,
        )

    planner_entrypoint_result = _route_planner_entrypoint(interactive, facts)
    if planner_entrypoint_result is not None:
        return planner_entrypoint_result
    
    # Resolve pre-guardrail action in strict precedence order:
    # reflect-recovery hint -> PTR candidate.
    resolved = reflect_recovery_resolution or _resolve_candidate_action(
        interactive,
        facts,
        context,
    )

    # Apply guardrails on the resolved pre-guardrail action.
    guardrail_result = _check_guardrails(
        interactive,
        facts,
        metadata,
        resolved_action=str(resolved["action"]),
        resolved_candidate_source=str(resolved["candidate_source"]),
    )
    if guardrail_result is not None:
        return guardrail_result

    resolved = _normalize_profile_resolution(facts, resolved)

    # Optional HITL pause remains a late-stage override.
    pause_result = await _handle_pause_logic(interactive, facts, context)
    if pause_result is not None:
        return pause_result

    return _route_with_outcome(
        interactive,
        action=str(resolved["action"]),
        reason=str(resolved["reason"]),
        candidate_action=resolved.get("candidate_action"),
        candidate_source=str(resolved["candidate_source"]),
        resolution_source=str(resolved["resolution_source"]),
        append_history=bool(resolved["append_history"]),
    )


def _check_guardrails(
    interactive: InteractiveState,
    facts,
    metadata: dict,
    *,
    resolved_action: str,
    resolved_candidate_source: str,
) -> Optional[dict]:
    """Check all guardrail conditions and return early if violated."""
    
    # Guardrail 1: Budget exhausted
    budget_exhausted, budget_reason = check_budget_exhaustion(
        facts,
        intended_action=resolved_action,
    )
    if budget_exhausted:
        logger.info(f"[ROUTER] Forcing finalize: {budget_reason}")
        safe_inc("router_finalize_budget_exhausted")
        return _route_with_outcome(
            interactive,
            action="finalize",
            reason=budget_reason or "budget_exhausted",
            candidate_action=resolved_action,
            candidate_source=resolved_candidate_source,
            resolution_source="guardrail",
            append_history=True,
        )
    
    # Guardrail 2: Legacy scope goals (fallback when no todos)
    current_todo = get_current_todo(facts)
    if not current_todo and are_scope_goals_achieved(interactive):
        logger.info("[ROUTER] Forcing finalize: all scope goals achieved (legacy)")
        safe_inc("router_finalize_goals_achieved")
        return _route_with_outcome(
            interactive,
            action="finalize",
            reason="legacy_scope_goals_achieved",
            candidate_action=resolved_action,
            candidate_source=resolved_candidate_source,
            resolution_source="guardrail",
            append_history=True,
        )

    # Guardrail 3: Deep-reasoning reflection loop recovery
    capability = str(facts.capability or "").strip().lower()
    consecutive_reflects = count_consecutive_reflections(facts.safe_decision_history)
    if resolved_action == "reflect":
        consecutive_reflects += 1
    if capability in {"deep_reasoning", "deep_reasoning_execution"} and consecutive_reflects >= 3:
        logger.info(
            "[ROUTER] Forcing synthesis: deep reasoning reflection loop (%s)",
            consecutive_reflects,
        )
        safe_inc("router_reflection_loop_synthesis")
        return _route_with_outcome(
            interactive,
            action="synthesis",
            reason="reflection_loop_synthesis",
            candidate_action=resolved_action,
            candidate_source=resolved_candidate_source,
            resolution_source="guardrail",
            append_history=True,
        )
    
    # Guardrail 4: Loop detected
    if is_action_loop_detected(interactive):
        logger.warning("[ROUTER] Loop detected: identical actions in history")
        safe_inc("router_loop_detected")
        
        if has_sufficient_findings(interactive):
            logger.info("[ROUTER] Forcing finalize: loop + sufficient findings")
            action = "finalize"
            reason = "action_loop_with_sufficient_findings"
        else:
            logger.info("[ROUTER] Forcing reflect: loop + insufficient findings")
            action = "reflect"
            reason = "action_loop_requires_reflect"
        return _route_with_outcome(
            interactive,
            action=action,
            reason=reason,
            candidate_action=resolved_action,
            candidate_source=resolved_candidate_source,
            resolution_source="guardrail",
            append_history=True,
        )
    
    # Guardrail 5: No progress (observation-based)
    if is_stuck_without_progress(interactive):
        logger.info("[ROUTER] Forcing finalize: no new findings for 2+ iterations")
        safe_inc("router_finalize_no_progress")
        return _route_with_outcome(
            interactive,
            action="finalize",
            reason="no_progress_guardrail",
            candidate_action=resolved_action,
            candidate_source=resolved_candidate_source,
            resolution_source="guardrail",
            append_history=True,
        )
    
    # Guardrail 6: No progress count
    no_progress, no_progress_reason = check_no_progress(facts)
    if no_progress:
        logger.info(f"[ROUTER] Forcing finalize: {no_progress_reason}")
        safe_inc("router_finalize_no_progress")
        return _route_with_outcome(
            interactive,
            action="finalize",
            reason="no_progress_count_guardrail",
            candidate_action=resolved_action,
            candidate_source=resolved_candidate_source,
            resolution_source="guardrail",
            append_history=True,
        )
    
    # Guardrail 7: Redundant execution warning
    has_warning, redundant_warning, _ = check_redundant_execution(facts)
    if has_warning:
        logger.warning(f"[ROUTER] Redundant execution detected: {redundant_warning}")
        if has_sufficient_findings(interactive):
            action = "finalize"
            reason = "redundant_execution_sufficient_findings"
        else:
            action = "reflect"
            reason = "redundant_execution_requires_reflect"
        facts.metadata.pop("redundant_execution_warning", None)
        return _route_with_outcome(
            interactive,
            action=action,
            reason=reason,
            candidate_action=resolved_action,
            candidate_source=resolved_candidate_source,
            resolution_source="guardrail",
            append_history=True,
        )
    
    # Budget warnings (log but don't force action)
    check_iteration_budget_warnings(interactive)
    log_progress_milestone(interactive)
    
    return None


def _resolve_reflect_recovery_action(
    interactive: InteractiveState,
    facts,
) -> Optional[dict[str, Any]]:
    """Resolve reflect-recovery hint when present, otherwise return None."""
    metadata = facts.safe_metadata
    has_reflect_recovery_hint = isinstance(metadata.get("next_after_reflect"), Mapping) or bool(
        facts.post_reflect_action
    )
    if not has_reflect_recovery_hint:
        return None

    # Reflect recovery is one-hop and must not reuse stale PTR candidates.
    facts.consume_candidate_decision()

    hinted_action = consume_post_reflect_hint(interactive)

    if hinted_action:
        safe_inc("langgraph_post_reflect_hint_consumed")
        return _build_resolution(
            action=hinted_action,
            reason="post_reflect_hint_consumed",
            candidate_action=hinted_action,
            candidate_source="reflect_hint",
            resolution_source="candidate",
            append_history=True,
        )

    # Reflect recovery is one-hop only; missing/invalid hints fail closed and
    # must not continue into PTR candidate/history inspection.
    return _build_resolution(
        action="finalize",
        reason="reflect_recovery_invalid_hint",
        candidate_action=None,
        candidate_source="reflect_hint",
        resolution_source="fallback",
        append_history=True,
    )


def _resolve_candidate_action(
    interactive: InteractiveState,
    facts,
    context: Optional[GraphRuntimeContext],
) -> dict[str, Any]:
    """Resolve action from candidate contract with fail-closed fallback."""
    metadata = facts.ensure_metadata()
    invocation_turn = resolve_turn_sequence(context, metadata)
    invocation_phase = resolve_router_phase_sequence(metadata)

    candidate, invalid_reason = consume_valid_candidate_decision(
        facts,
        turn_sequence=invocation_turn,
        phase_sequence=invocation_phase,
    )

    if candidate is not None:
        candidate_action = str(candidate.get("next_action") or "")
        legacy_action = ""
        if facts.safe_decision_history:
            legacy_action = extract_action_label(facts.safe_decision_history[-1]).strip().lower()
        reason = "candidate_decision_accepted"
        if legacy_action and legacy_action != candidate_action:
            reason = "candidate_history_conflict"
        return _build_resolution(
            action=candidate_action,
            reason=reason,
            candidate_action=candidate_action,
            candidate_source=str(candidate.get("decision_source") or "ptr"),
            resolution_source="candidate",
            append_history=False,
        )

    if facts.safe_decision_history:
        legacy_action = extract_action_label(facts.safe_decision_history[-1]).strip().lower()
        if legacy_action in VALID_ACTIONS:
            fallback_reason = "decision_history_fallback"
            if invalid_reason:
                fallback_reason = f"{invalid_reason}_history_fallback"
            return _build_resolution(
                action=legacy_action,
                reason=fallback_reason,
                candidate_action=legacy_action,
                candidate_source="legacy_compatibility",
                resolution_source="fallback",
                append_history=False,
            )

    fail_closed_reason = invalid_reason or "candidate_missing"
    return _build_resolution(
        action="finalize",
        reason=fail_closed_reason,
        candidate_action=None,
        candidate_source="candidate_contract",
        resolution_source="fallback",
        append_history=True,
    )


def _normalize_profile_resolution(
    facts,
    resolution: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize profile-incompatible actions before final write."""
    capability = str(facts.capability or "").strip().lower()
    action = str(resolution.get("action") or "").strip().lower()

    if capability in {"simple_tool", "simple_tool_execution"} and action == "synthesis":
        return _build_resolution(
            action="finalize",
            reason="profile_normalized_simple_tool_synthesis_to_finalize",
            candidate_action=action,
            candidate_source=str(resolution.get("candidate_source") or "profile"),
            resolution_source="profile_normalization",
            append_history=True,
        )

    return dict(resolution)


async def _handle_pause_logic(
    interactive: InteractiveState,
    facts,
    context: Optional[GraphRuntimeContext],
) -> Optional[dict]:
    """Handle agent-initiated pause if enabled."""
    config = AgentConfig()
    enable_pause = facts.metadata.get("enable_agent_pause", config.enable_agent_pause)
    
    if not enable_pause:
        return None
    
    should_pause, pause_reason = should_pause_for_confirmation(interactive, config)
    if not should_pause:
        return None
    
    logger.info(f"[ROUTER] Pause condition detected: {pause_reason}")
    
    try:
        pause_request = build_pause_request(interactive, pause_reason)
        user_approved = await emit_and_wait_for_pause_response(
            pause_request, context, interactive, config,
        )
        
        if not user_approved:
            logger.info("[ROUTER] User declined continuation, finalizing")
            safe_inc("router_finalize_user_declined")
            return _route_with_outcome(
                interactive,
                action="finalize",
                reason="pause_declined",
                candidate_action=None,
                candidate_source="terminal_state",
                resolution_source="terminal_state",
                append_history=True,
            )
        
        # User approved - continue normally
        logger.info("[ROUTER] User approved continuation, proceeding")
        safe_inc("router_pause_approved")
        interactive.trace.reasoning.append(
            f"[PAUSE] User approved continuation after {pause_reason}"
        )
        
    except Exception as exc:
        logger.error(f"[ROUTER] Pause logic failed: {exc}, continuing without pause")
        safe_inc("router_pause_failed")
        interactive.trace.reasoning.append(f"[PAUSE] Pause logic error: {exc}, continuing")
    
    return None


def _route_planner_entrypoint(interactive: InteractiveState, facts) -> Optional[dict]:
    """Route the planner handoff into loop start once per approved plan."""
    metadata = facts.ensure_metadata()
    planner_mode = str(metadata.get("planner_mode") or "").strip().lower()
    if planner_mode != "plan_ready":
        return None

    if metadata.get("planner_entrypoint_consumed") is True:
        return None
    metadata["planner_entrypoint_consumed"] = True

    if facts.safe_todo_list or facts.plan:
        return _route_with_outcome(
            interactive,
            action="call_tool",
            reason="planner_entrypoint_start_execution",
            candidate_action="call_tool",
            candidate_source="planner_entrypoint",
            resolution_source="fallback",
            append_history=True,
        )

    return _route_with_outcome(
        interactive,
        action="finalize",
        reason="planner_entrypoint_no_executable_work",
        candidate_action=None,
        candidate_source="planner_entrypoint",
        resolution_source="fallback",
        append_history=True,
    )


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "decision_router",
    "record_decision",
]
