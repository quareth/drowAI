"""Scope progress tracking for goal completion monitoring."""

from __future__ import annotations

import logging
from typing import Optional

from ..state import InteractiveState

logger = logging.getLogger(__name__)


def calculate_scope_progress(state: InteractiveState) -> float:
    """
    Calculate progress percentage (goals achieved / total goals).

    Returns a value between 0.0 (no progress) and 1.0 (all goals achieved).

    Args:
        state: Current reasoning state

    Returns:
        Progress value (0.0-1.0)
    """
    facts = state.facts
    metadata = facts.safe_metadata

    # Get scope goals
    scope_goals = facts.scope_goals or []
    if not scope_goals:
        # Try to get from metadata (for backward compatibility)
        user_scope = metadata.get("user_scope")
        if user_scope:
            if isinstance(user_scope, dict):
                from .scope_parser import UserScope

                user_scope = UserScope.from_dict(user_scope)
            scope_goals = user_scope.goals if hasattr(user_scope, "goals") else []

    if not scope_goals:
        return 0.0  # No goals defined, no progress to track

    # Get achieved goals
    achieved_goals = facts.achieved_goals or set()
    if isinstance(achieved_goals, list):
        achieved_goals = set(achieved_goals)

    # Calculate progress
    achieved_count = len(achieved_goals)
    total_count = len(scope_goals)

    if total_count == 0:
        return 0.0

    progress = achieved_count / total_count

    return progress


def get_progress_milestone(progress: float) -> Optional[str]:
    """
    Get milestone string for progress value.

    Args:
        progress: Progress value (0.0-1.0)

    Returns:
        Milestone string (e.g., "25%", "50%", "75%", "100%") or None
    """
    if progress >= 1.0:
        return "100%"
    elif progress >= 0.75:
        return "75%"
    elif progress >= 0.5:
        return "50%"
    elif progress >= 0.25:
        return "25%"
    else:
        return None


def log_progress_milestone(state: InteractiveState) -> None:
    """
    Log progress milestone if reached.

    Tracks the last logged milestone to avoid duplicate logging.

    Args:
        state: Current reasoning state
    """
    facts = state.facts
    metadata = facts.ensure_metadata()

    # Calculate current progress
    progress = calculate_scope_progress(state)

    # Get last logged milestone
    last_milestone = metadata.get("last_progress_milestone")

    # Get current milestone
    current_milestone = get_progress_milestone(progress)

    # Log if milestone changed
    if current_milestone and current_milestone != last_milestone:
        scope_goals = facts.scope_goals or []
        achieved_goals = facts.achieved_goals or set()
        if isinstance(achieved_goals, list):
            achieved_goals = set(achieved_goals)

        logger.info(
            f"[SCOPE] Progress milestone: {current_milestone} "
            f"({len(achieved_goals)}/{len(scope_goals)} goals achieved)"
        )

        # Update last milestone
        metadata["last_progress_milestone"] = current_milestone
        facts.metadata = metadata


__all__ = [
    "calculate_scope_progress",
    "get_progress_milestone",
    "log_progress_milestone",
]

