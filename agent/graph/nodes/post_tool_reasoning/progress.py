"""Todo progress tracking for post-tool reasoning.

This module handles applying LLM-assessed progress updates to todo items
and building progress summaries for context.

The LLM uses TodoProgress to report status changes, and this module
applies those changes to the actual TodoItem objects in state.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from typing import Any, List, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PostToolReasoningOutput
    from ...state import InteractiveState

from ...context.runtime_state import refresh_bundle_active_todo
from ...utils.plan_progress_authority import apply_llm_updates, build_todo_stream_updates

logger = logging.getLogger(__name__)


# =============================================================================
# Progress Application
# =============================================================================


def apply_progress_updates(
    interactive: "InteractiveState",
    output: "PostToolReasoningOutput",
) -> list[dict[str, Any]]:
    """Apply todo progress updates from LLM output to state.
    
    Updates todo items based on LLM's assessment of what was completed.
    This is the SINGLE SOURCE OF TRUTH for todo completion - no other
    system should mark todos as complete.
    
    Args:
        interactive: The InteractiveState to update (mutated in place).
        output: The PostToolReasoningOutput containing progress updates.

    Returns:
        Changed-only todo stream updates derived from before/after state.
    """
    from ...state import TodoItem
    
    facts = interactive.facts
    todo_list = facts.safe_todo_list
    
    if not todo_list or not output.todo_progress:
        return []
    
    # Convert string todos to TodoItems if needed (backward compatibility)
    if todo_list and isinstance(todo_list[0], str):
        todo_list = [TodoItem.from_string(t) for t in todo_list]
        facts.todo_list = todo_list

    # Snapshot before applying updates so stream emission can be change-based.
    before_snapshot = [todo.model_copy(deep=True) for todo in todo_list]
    changed_indices = apply_llm_updates(todo_list, output.todo_progress)

    metadata = facts.metadata if isinstance(facts.metadata, Mapping) else {}
    todo_id_map = metadata.get("todo_id_map") if isinstance(metadata, Mapping) else None
    stream_updates = build_todo_stream_updates(
        before_snapshot,
        todo_list,
        todo_id_map=todo_id_map if isinstance(todo_id_map, list) else None,
    )
    if changed_indices and not stream_updates:
        logger.warning(
            "[PROGRESS] Changed indices %s yielded no stream deltas",
            sorted(changed_indices),
        )
    
    # Update achieved_goals set for completed todos
    for todo in todo_list:
        if hasattr(todo, 'is_complete') and todo.is_complete():
            facts.achieved_goals.add(todo.description)

    # Keep the context bundle's active_todo slot in sync with the new
    # in-progress item so downstream selection layers see the current
    # plan step, not a stale one.
    if isinstance(facts.metadata, MutableMapping):
        refresh_bundle_active_todo(facts.metadata, todo_list)

    return stream_updates


# =============================================================================
# Progress Summary
# =============================================================================


def build_progress_summary(output: "PostToolReasoningOutput") -> str:
    """Build progress summary for conversation history.
    
    Creates a brief summary of progress updates from this iteration
    to include in conversation history for LLM context.
    
    Args:
        output: The PostToolReasoningOutput with progress updates.
        
    Returns:
        Formatted progress summary string, or empty string if no updates.
    """
    # Return early only if there's nothing to report
    if (not output.todo_progress 
        and not output.user_goal_achieved 
        and not output.effective_next_goal):
        return ""
    
    parts: List[str] = []
    
    if output.todo_progress:
        completed_indices = [
            p.index for p in output.todo_progress 
            if p.status in ("completed", "skipped")
        ]
        in_progress_indices = [
            p.index for p in output.todo_progress 
            if p.status == "in_progress"
        ]
        
        if completed_indices:
            parts.append(f"Completed: todos {completed_indices}")
        if in_progress_indices:
            parts.append(f"In progress: todos {in_progress_indices}")
    
    if output.user_goal_achieved:
        parts.append("✅ User goal achieved")
    
    if output.effective_next_goal:
        parts.append(f"Next goal: {output.effective_next_goal}")
    
    return " | ".join(parts) if parts else ""


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "apply_progress_updates",
    "build_progress_summary",
]









