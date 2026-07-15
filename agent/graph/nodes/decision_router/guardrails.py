"""Router-specific guardrail orchestration for decision router.

This module handles router-specific guardrail checks and decision logic.
It IMPORTS from agent.graph.utils.termination_guardrails for core logic.

NOTE: Core guardrail functions come from termination_guardrails.py.
This module ONLY contains router-specific orchestration:
- Budget exhaustion checks
- Goal completion checks
- Loop detection response
- Reflection loop handling
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from backend.services.metrics.utils import safe_inc

from .helpers import extract_action_label

# IMPORT existing guardrails - DO NOT DUPLICATE
from ...utils.termination_guardrails import (
    are_scope_goals_achieved,
    calculate_termination_bias,
    check_iteration_budget_warnings,
    has_sufficient_findings,
    is_action_loop_detected,
    is_stuck_without_progress,
)
from ...utils.scope_progress import (
    calculate_scope_progress,
    log_progress_milestone,
)
from ...utils.post_tool_metadata import (
    request_contract_terminal,
    user_goal_achieved,
)
from ...builders.common_edges import increment_stuck_counter

if TYPE_CHECKING:
    from ...state import InteractiveState, FactsState

logger = logging.getLogger(__name__)


# =============================================================================
# Budget Checks
# =============================================================================


def _coerce_non_negative_int(value: Any) -> Optional[int]:
    """Return non-negative integer budget values, otherwise None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(0, value)
    return None


def _resolve_remaining_budget(
    *,
    runtime_remaining: Any,
    structured_max: Any,
    used: int,
) -> tuple[Optional[int], bool]:
    """Return most restrictive remaining budget and conflict flag."""
    runtime_value = _coerce_non_negative_int(runtime_remaining)
    structured_max_value = _coerce_non_negative_int(structured_max)
    structured_remaining = None
    if structured_max_value is not None:
        structured_remaining = max(0, structured_max_value - max(0, used))

    known_values = [value for value in (runtime_value, structured_remaining) if value is not None]
    if not known_values:
        return None, False

    conflict = (
        runtime_value is not None
        and structured_remaining is not None
        and runtime_value != structured_remaining
    )
    return min(known_values), conflict


def check_budget_exhaustion(
    facts: "FactsState",
    *,
    intended_action: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Check if iteration or tool call budget is exhausted.
    
    Args:
        facts: FactsState containing budget information
        
    Returns:
        Tuple of (is_exhausted: bool, reason: Optional[str])
    """
    metadata = facts.safe_metadata
    runtime_budgets = metadata.get("runtime_budgets", {})
    if not isinstance(runtime_budgets, dict):
        runtime_budgets = {}

    remaining_iterations, iteration_conflict = _resolve_remaining_budget(
        runtime_remaining=runtime_budgets.get("remaining_iterations"),
        structured_max=(
            facts.budgets.max_iterations
            if facts.budgets.max_iterations is not None
            else 15
        ),
        used=facts.iterations,
    )
    if remaining_iterations is not None and remaining_iterations <= 0:
        reason = "budget_exhausted"
        if iteration_conflict:
            reason = "budget_exhausted_conflict_iterations"
        return True, reason

    remaining_tool_calls, tool_conflict = _resolve_remaining_budget(
        runtime_remaining=runtime_budgets.get("remaining_tool_calls"),
        structured_max=facts.budgets.max_tool_calls,
        used=facts.tool_calls_used,
    )
    if remaining_tool_calls is not None and remaining_tool_calls <= 0:
        reason = "budget_exhausted"
        if tool_conflict:
            reason = "budget_exhausted_conflict_tool_calls"
        return True, reason

    if (
        (intended_action or "").strip().lower() == "call_tool"
        and remaining_tool_calls is None
    ):
        return True, "budget_unknown_call_tool"

    return False, None


def check_goal_completion(
    facts: "FactsState",
    metadata: dict,
) -> tuple[bool, Optional[str]]:
    """Check if goals are achieved based on state flags.

    Both terminal metadata flags must be honored consistently with the
    DR post-tool route in :mod:`agent.graph.builders.deep_reasoning_builder`:
    ``user_goal_achieved`` is the LLM's goal-completion verdict, while
    ``request_contract_terminal`` is set by the request-contract policy
    when a binary/short request resolves to a terminal answer. The reason
    string distinguishes the two so observability remains meaningful.

    Args:
        facts: FactsState containing goal information
        metadata: Metadata dict with completion flags

    Returns:
        Tuple of (is_complete: bool, reason: Optional[str])
    """
    # Check user_goal_achieved flag from post_tool_reasoning
    if user_goal_achieved(metadata):
        return True, "User goal achieved (LLM assessment)"

    # Request contract may mark binary/short asks as terminal once
    # determined; honor it consistently with DR post-tool routing.
    if request_contract_terminal(metadata):
        return True, "Request contract terminal"

    return False, None


# =============================================================================
# Loop Detection
# =============================================================================


def count_consecutive_reflections(decision_history: list) -> int:
    """Count consecutive reflection decisions from the end of history.
    
    This helps detect reflection loops where the agent keeps reflecting
    without making progress.
    
    Args:
        decision_history: List of decision entries (format: "action: reasoning")
    
    Returns:
        Number of consecutive "reflect" decisions at the end of history
    
    Example:
        ["call_tool: ...", "reflect: ...", "reflect: ..."] → returns 2
        ["reflect: ...", "call_tool: ..."] → returns 0 (call_tool broke the chain)
    """
    if not decision_history:
        return 0
    
    count = 0
    # Iterate backwards through history
    for entry in reversed(decision_history):
        # Extract action name (before the colon)
        action = extract_action_label(entry)

        if action == "reflect":
            count += 1
        else:
            # Chain broken by non-reflect action
            break
    
    return count


def check_no_progress(facts: "FactsState") -> tuple[bool, Optional[str]]:
    """Check no_progress_count for consecutive stalls.
    
    Args:
        facts: FactsState containing progress tracking
        
    Returns:
        Tuple of (is_stuck: bool, reason: Optional[str])
    """
    metadata = facts.safe_metadata
    no_progress_count = metadata.get("no_progress_count", 0)
    
    if no_progress_count >= 2:
        return True, f"No progress detected for {no_progress_count} consecutive observations"
    
    return False, None


def check_redundant_execution(facts: "FactsState") -> tuple[bool, Optional[str], bool]:
    """Check for redundant execution warning.
    
    Args:
        facts: FactsState containing warnings
        
    Returns:
        Tuple of (has_warning: bool, warning_text: Optional[str], has_findings: bool)
    """
    metadata = facts.safe_metadata
    redundant_warning = metadata.get("redundant_execution_warning")
    
    if redundant_warning:
        # Need to check findings - defer to caller
        return True, redundant_warning, False
    
    return False, None, False


# =============================================================================
# Termination Bias
# =============================================================================


def get_termination_bias_context(interactive: "InteractiveState") -> tuple[float, str]:
    """Get termination bias and context string for LLM prompt.
    
    Args:
        interactive: Current InteractiveState
        
    Returns:
        Tuple of (bias_value: float, bias_context: str)
    """
    termination_bias = calculate_termination_bias(interactive)
    
    if termination_bias > 0.5:
        bias_context = (
            f"\n\nIMPORTANT: Strong bias toward finalization (bias: {termination_bias:.2f}). "
            "Consider finalizing if goals are achieved or sufficient information gathered."
        )
    elif termination_bias > 0.2:
        bias_context = (
            f"\n\nNote: Moderate bias toward finalization (bias: {termination_bias:.2f}). "
            "Consider finalizing if progress is sufficient."
        )
    else:
        bias_context = ""
    
    return termination_bias, bias_context


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Metrics
    "safe_inc",
    # Budget checks
    "check_budget_exhaustion",
    "check_goal_completion",
    # Loop detection
    "count_consecutive_reflections",
    "check_no_progress",
    "check_redundant_execution",
    # Termination bias
    "get_termination_bias_context",
    # Re-exports from termination_guardrails (for convenience)
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
]









