"""Decision router package for deep reasoning loop control.

This package provides deterministic decision routing logic for the deep
reasoning graph, determining which action to take next from contracts,
state, and guardrails.

The package is modularized into:
- router.py: Main decision_router function and decision recording
- guardrails.py: Budget/goal checks and termination bias
- pause.py: Agent-initiated pause handling for user confirmation
- helpers.py: Todo management, findings extraction, decision parsing

All public symbols are re-exported here for backward compatibility.
External code should import from this package:

    from agent.graph.nodes.decision_router import decision_router

For testing internal functions:

    from agent.graph.nodes.decision_router import (
        _check_all_todos_complete,
        _record_decision,
        _get_current_todo,
    )
"""

# Main node function
from .router import (
    decision_router,
    record_decision,
)

# Guardrails
from .guardrails import (
    safe_inc,
    check_budget_exhaustion,
    check_goal_completion,
    check_no_progress,
    check_redundant_execution,
    count_consecutive_reflections,
    get_termination_bias_context,
    # Re-exported from termination_guardrails
    are_scope_goals_achieved,
    calculate_termination_bias,
    check_iteration_budget_warnings,
    has_sufficient_findings,
    is_action_loop_detected,
    is_stuck_without_progress,
    # Re-exported from scope_progress
    calculate_scope_progress,
    log_progress_milestone,
    # Re-exported from common_edges
    increment_stuck_counter,
)

# Helpers
from .helpers import (
    VALID_ACTIONS,
    check_all_todos_complete,
    consume_post_reflect_hint,
    determine_post_reflect_action,
    extract_findings,
    get_current_todo,
    get_next_todo,
    heuristic_decision,
    parse_decision_response,
)

# Pause logic
from .pause import (
    build_pause_request,
    emit_and_wait_for_pause_response,
    should_pause_for_confirmation,
)

# =============================================================================
# Backward Compatibility Aliases
# =============================================================================
# Tests and other code may import these with underscore prefix

_record_decision = record_decision
_check_all_todos_complete = check_all_todos_complete
_get_current_todo = get_current_todo
_get_next_todo = get_next_todo
_extract_findings = extract_findings
_count_consecutive_reflections = count_consecutive_reflections
_consume_post_reflect_hint = consume_post_reflect_hint
_parse_decision_response = parse_decision_response
_heuristic_decision = heuristic_decision
_should_pause_for_confirmation = should_pause_for_confirmation
_build_pause_request = build_pause_request
_emit_and_wait_for_pause_response = emit_and_wait_for_pause_response


__all__ = [
    # Main exports
    "decision_router",
    "record_decision",
    "VALID_ACTIONS",
    # Guardrails
    "check_budget_exhaustion",
    "check_goal_completion",
    "check_no_progress",
    "check_redundant_execution",
    "count_consecutive_reflections",
    "get_termination_bias_context",
    # Re-exports from termination_guardrails
    "are_scope_goals_achieved",
    "calculate_termination_bias",
    "check_iteration_budget_warnings",
    "has_sufficient_findings",
    "is_action_loop_detected",
    "is_stuck_without_progress",
    # Re-exports from scope_progress
    "calculate_scope_progress",
    "log_progress_milestone",
    # Re-exports from common_edges
    "increment_stuck_counter",
    # Helpers
    "check_all_todos_complete",
    "consume_post_reflect_hint",
    "determine_post_reflect_action",
    "extract_findings",
    "get_current_todo",
    "get_next_todo",
    "heuristic_decision",
    "parse_decision_response",
    # Pause
    "build_pause_request",
    "emit_and_wait_for_pause_response",
    "should_pause_for_confirmation",
    # Backward compatibility aliases (underscore prefix)
    "_record_decision",
    "_check_all_todos_complete",
    "_get_current_todo",
    "_get_next_todo",
    "_extract_findings",
    "_count_consecutive_reflections",
    "_consume_post_reflect_hint",
    "_parse_decision_response",
    "_heuristic_decision",
    "_should_pause_for_confirmation",
    "_build_pause_request",
    "_emit_and_wait_for_pause_response",
    "safe_inc",
]









