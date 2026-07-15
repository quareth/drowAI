"""Router-specific helper functions for decision router.

This module contains helper functions specific to the decision router,
including todo management, findings extraction, and decision parsing.

For shared helpers, see agent.graph.nodes.node_utils.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Mapping, MutableMapping, Optional

# Import shared utilities from node_utils
from ..node_utils import determine_post_reflect_action
from ...utils.plan_progress_authority import ensure_initial_in_progress

if TYPE_CHECKING:
    from ...state import FactsState, TraceState, InteractiveState, TodoItem

# Import TodoItem and TodoStatus from state
from ...state import TodoItem, TodoStatus

logger = logging.getLogger(__name__)

# Valid node names for routing
VALID_ACTIONS = frozenset({"think_more", "call_tool", "reflect", "finalize", "synthesis"})


# =============================================================================
# Decision-history Action Label
# =============================================================================


def extract_action_label(decision_entry: str) -> str:
    """Return the action label from a decision-history entry.

    Decision-history entries follow the shape ``"action: reasoning"``. This
    helper centralizes the parsing rule (split on the first colon and strip
    whitespace) so router and builder routing helpers do not duplicate it.

    Reasoning text may contain additional colons (e.g. port ranges in tool
    arguments), so ``split(":", 1)`` is used to keep them in the reasoning
    portion. Empty or whitespace-only entries return ``""``.
    """

    entry = (decision_entry or "").strip()
    if not entry:
        return ""
    if ":" in entry:
        return entry.split(":", 1)[0].strip()
    return entry


# =============================================================================
# Todo Completion Checks
# =============================================================================


def check_all_todos_complete(facts: "FactsState") -> bool:
    """Check if all todos are complete based on their status.
    
    This is a simple state check, NOT an LLM assessment.
    The LLM in post_tool_reasoning is responsible for marking todos complete.
    
    Args:
        facts: FactsState containing todo_list
        
    Returns:
        True if all todos are in a completed state
    """
    todo_list = facts.safe_todo_list
    
    if not todo_list:
        return False  # No todos to complete
    
    for todo in todo_list:
        if isinstance(todo, str):
            return False  # String todos haven't been processed
        if hasattr(todo, 'is_complete') and not todo.is_complete():
            return False
    
    return True


def get_current_todo(facts: "FactsState") -> Optional["TodoItem"]:
    """Get current in-progress todo item.
    
    Args:
        facts: FactsState containing todo_list
        
    Returns:
        Current TodoItem or None if no todos or all complete
    """
    todo_list = facts.safe_todo_list
    
    # Handle both string-based (legacy) and TodoItem-based lists
    if not todo_list:
        return None
    
    # If first item is a string, convert all to TodoItems
    if isinstance(todo_list[0], str):
        todo_list = TodoItem.from_string_list(todo_list)
        facts.todo_list = todo_list
    
    # Find in-progress todo
    for todo in todo_list:
        if todo.status == TodoStatus.IN_PROGRESS:
            return todo
    
    # No in-progress, delegate activation to authoritative transition helper
    activated = ensure_initial_in_progress(todo_list)
    if activated:
        for todo in todo_list:
            if todo.status == TodoStatus.IN_PROGRESS:
                logger.info("[TODO] Starting todo: %s", todo.description)
                return todo
    
    # All todos complete
    return None


def get_next_todo(facts: "FactsState") -> Optional["TodoItem"]:
    """Get next pending todo item.
    
    Args:
        facts: FactsState containing todo_list
        
    Returns:
        Next pending TodoItem or None if no pending todos
    """
    todo_list = facts.safe_todo_list
    
    if not todo_list:
        return None
    
    # If first item is a string, convert all
    if isinstance(todo_list[0], str):
        todo_list = TodoItem.from_string_list(todo_list)
        facts.todo_list = todo_list
    
    # Find first pending todo
    for todo in todo_list:
        if todo.status == TodoStatus.PENDING:
            return todo
    
    return None


# =============================================================================
# Findings Extraction
# =============================================================================


def extract_findings(trace: "TraceState") -> list:
    """Extract findings from trace for completion context.
    
    Args:
        trace: TraceState containing executed tools and observations
        
    Returns:
        List of finding strings
    """
    findings = []
    
    # Extract from tool executions
    for tool_record in trace.executed_tools or []:
        if hasattr(tool_record, "observation") and tool_record.observation:
            findings.append(tool_record.observation)
    
    return findings


# =============================================================================
# Decision Parsing
# =============================================================================


def parse_decision_response(response: str) -> tuple[str, str]:
    """Parse LLM decision response to extract action and reasoning.
    
    Attempts to extract JSON first, then falls back to text matching.
    
    Args:
        response: Raw LLM response text
        
    Returns:
        Tuple of (action: str, reasoning: str)
    """
    try:
        # Try to extract JSON
        json_start = response.find("{")
        json_end = response.rfind("}") + 1
        
        if json_start >= 0 and json_end > json_start:
            json_str = response[json_start:json_end]
            parsed = json.loads(json_str)
            
            action = parsed.get("action", "").lower().replace(" ", "_")
            reasoning = parsed.get("reasoning", "No reasoning provided")
            
            if action in VALID_ACTIONS:
                return action, reasoning
    
    except (json.JSONDecodeError, KeyError, AttributeError) as exc:
        logger.warning(f"Failed to parse decision response: {exc}")
    
    # If parsing fails, try to extract action from text
    response_lower = response.lower()
    for action in VALID_ACTIONS:
        if action.replace("_", " ") in response_lower or action in response_lower:
            return action, response[:200]  # Use first part as reasoning
    
    # Default fallback
    return "think_more", "Failed to parse decision, defaulting to think_more"


# =============================================================================
# Heuristic Decision
# =============================================================================


def heuristic_decision(interactive: "InteractiveState") -> str:
    """Simple heuristic decision when LLM unavailable.
    
    Args:
        interactive: Current InteractiveState
        
    Returns:
        Action string (one of VALID_ACTIONS)
    """
    facts = interactive.facts
    trace = interactive.trace
    
    # If no tools executed yet and have todo list, try to call tool
    executed_tools = trace.executed_tools or []
    todo_list = facts.safe_todo_list
    
    if not executed_tools and todo_list:
        return "call_tool"
    
    # If just executed tool, think about results
    if executed_tools:
        last_tool = executed_tools[-1]
        # Check if we've already reasoned about this tool
        if isinstance(last_tool, dict):
            tool_id = last_tool.get("tool_id")
            # Simple check: if scratchpad doesn't mention this tool, think about it
            scratchpad = trace.scratchpad or ""
            if tool_id and tool_id not in scratchpad:
                return "think_more"
    
    # If we've thought a lot but haven't executed tools recently, try tool
    if facts.iterations > 3 and len(executed_tools) == 0:
        return "call_tool"
    
    # If we've done several iterations, finalize
    if facts.iterations >= 5:
        return "finalize"
    
    # Default: think more
    return "think_more"


# =============================================================================
# Post-Reflection Hint
# =============================================================================


def consume_post_reflect_hint(interactive: "InteractiveState") -> Optional[str]:
    """Read and clear deterministic action hint set by reflection.
    
    Args:
        interactive: Current InteractiveState
        
    Returns:
        Normalized action hint or None if no valid hint
    """
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    observability = facts.ensure_router_observability()
    current_iteration = facts.iterations

    canonical_raw = metadata.pop("next_after_reflect", None)
    compat_raw = facts.post_reflect_action
    # Consume-and-clear compatibility field before any early-return route path.
    facts.post_reflect_action = None

    hint_id: Optional[str] = None
    issued_at_iteration: Optional[int] = None
    action_raw: Optional[str] = None
    hint_source = "none"

    if isinstance(canonical_raw, Mapping):
        action_raw = str(canonical_raw.get("action") or "")
        raw_hint_id = canonical_raw.get("hint_id")
        if isinstance(raw_hint_id, str) and raw_hint_id.strip():
            hint_id = raw_hint_id.strip()
        issued_at_iteration = _coerce_int(canonical_raw.get("issued_at_iteration"))
        hint_source = "canonical"
    elif compat_raw:
        action_raw = str(compat_raw)
        normalized_compat = action_raw.strip().lower().replace(" ", "_")
        hint_id = f"compat-reflect-{current_iteration}-{normalized_compat}"
        issued_at_iteration = current_iteration
        hint_source = "compatibility"
    else:
        return None

    previous_consumed = str(observability.get("last_consumed_reflect_hint_id") or "")

    if not hint_id:
        interactive.trace.reasoning.append(
            "Post-reflection hint rejected: missing required hint_id"
        )
        return None

    if issued_at_iteration != current_iteration:
        observability["last_consumed_reflect_hint_id"] = hint_id
        interactive.trace.reasoning.append(
            "Post-reflection hint rejected due to iteration mismatch "
            f"(hint={issued_at_iteration}, current={current_iteration})"
        )
        return None

    if hint_source == "canonical":
        if previous_consumed == hint_id:
            observability["last_consumed_reflect_hint_id"] = hint_id
            interactive.trace.reasoning.append(
                f"Post-reflection hint `{hint_id}` rejected as replayed"
            )
            return None

    normalized = (action_raw or "").strip().lower().replace(" ", "_")
    if normalized not in {"call_tool", "think_more"}:
        observability["last_consumed_reflect_hint_id"] = hint_id
        interactive.trace.reasoning.append(
            f"Post-reflection hint `{action_raw}` ignored because it is not a one-hop action"
        )
        return None

    observability["last_consumed_reflect_hint_id"] = hint_id
    interactive.trace.reasoning.append(
        f"Post-reflection hint detected from {hint_source}; routing next action to `{normalized}`"
    )
    return normalized


def _coerce_int(value: Any) -> Optional[int]:
    """Return integer value when coercion is deterministic."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def resolve_router_phase_sequence(metadata: Mapping[str, Any]) -> Optional[int]:
    """Resolve current router phase sequence from metadata/working memory."""
    direct = _coerce_int(metadata.get("phase_sequence"))
    if direct is not None:
        return direct

    ptr_phase = _coerce_int(metadata.get("current_ptr_phase_sequence"))
    if ptr_phase is not None:
        return ptr_phase

    working_memory = metadata.get("working_memory")
    if not isinstance(working_memory, Mapping):
        return None

    current_turn_phases = working_memory.get("current_turn_phases")
    if not isinstance(current_turn_phases, list) or not current_turn_phases:
        return None

    latest = current_turn_phases[-1]
    if isinstance(latest, Mapping):
        return _coerce_int(latest.get("phase_sequence"))
    return None


def consume_valid_candidate_decision(
    facts: "FactsState",
    *,
    turn_sequence: Optional[int],
    phase_sequence: Optional[int],
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Consume candidate_decision and validate invocation binding.

    Returns a tuple of ``(candidate, invalid_reason)`` where candidate is present
    only when all required contract fields are valid and bound to the current
    router invocation.
    """
    raw = facts.consume_candidate_decision()
    if raw is None:
        return None, None

    required_str = ("candidate_id", "producer_node")
    for key in required_str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            return None, f"candidate_invalid_{key}"

    if turn_sequence is None or phase_sequence is None:
        return None, "candidate_invocation_binding_missing"

    candidate_turn = _coerce_int(raw.get("turn_sequence"))
    candidate_phase = _coerce_int(raw.get("phase_sequence"))
    if candidate_turn != turn_sequence or candidate_phase != phase_sequence:
        return None, "candidate_invocation_binding_mismatch"

    next_action = str(raw.get("next_action") or "").strip().lower().replace(" ", "_")
    if next_action not in VALID_ACTIONS:
        return None, "candidate_invalid_next_action"

    normalized = dict(raw)
    normalized["next_action"] = next_action
    normalized["turn_sequence"] = candidate_turn
    normalized["phase_sequence"] = candidate_phase
    return normalized, None


def write_router_outcome(
    facts: "FactsState",
    *,
    action: str,
    candidate_action: Optional[str],
    candidate_source: str,
    resolution_source: str,
    reason: str,
    profile: Optional[str] = None,
) -> dict[str, Any]:
    """Write normalized router_outcome metadata contract."""
    outcome = {
        "action": action,
        "candidate_action": candidate_action,
        "candidate_source": candidate_source,
        "resolution_source": resolution_source,
        "profile": profile or "",
        "reason": reason,
    }
    facts.set_router_outcome(outcome)
    return outcome


def update_router_observability(
    facts: "FactsState",
    outcome: Mapping[str, Any],
) -> MutableMapping[str, Any]:
    """Update router observability fields from latest router_outcome."""
    observability = facts.ensure_router_observability()
    action = str(outcome.get("action") or "")
    reason = str(outcome.get("reason") or "")

    previous = str(observability.get("last_final_action") or "")
    counts = observability.get("consecutive_action_counts")
    if not isinstance(counts, Mapping):
        counts = {}
    counts_mutable = dict(counts)
    if action:
        counts_mutable[action] = counts_mutable.get(action, 0) + 1 if action == previous else 1

    action_streak = 0
    if action:
        raw_streak = counts_mutable.get(action, 0)
        action_streak = int(raw_streak) if isinstance(raw_streak, int) else 0

    observability["last_final_action"] = action
    observability["last_router_reason"] = reason
    observability["consecutive_action_counts"] = counts_mutable
    observability["stuck_progression"] = {
        "action": action,
        "same_action_streak": action_streak,
        "stuck_counter": int(facts.stuck_counter),
    }
    return observability


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Constants
    "VALID_ACTIONS",
    # Decision-history parsing
    "extract_action_label",
    # Todo helpers
    "check_all_todos_complete",
    "get_current_todo",
    "get_next_todo",
    # Findings
    "extract_findings",
    # Parsing
    "parse_decision_response",
    # Heuristics
    "heuristic_decision",
    # Post-reflection
    "consume_post_reflect_hint",
    # Router contract helpers
    "consume_valid_candidate_decision",
    "resolve_router_phase_sequence",
    "update_router_observability",
    "write_router_outcome",
    # Re-export from node_utils
    "determine_post_reflect_action",
]


