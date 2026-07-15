"""Final planner state application.

This module owns planner-produced graph-state mutations after a plan has
been generated or resumed: todo activation, plan versioning, runtime budget
defaults, facts writes, cache context, and todo ID seeding. It does not
generate plans, perform resume detection, or coordinate graph-node control
flow.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping, MutableMapping, Optional

from agent.graph.context.runtime_state import refresh_bundle_active_todo

from ..infrastructure.state_models import GraphRuntimeContext, build_budget_envelope
from ..state import InteractiveState, TodoItem
from ..utils.cache_invalidation import create_plan_context
from ..utils.event_identity import resolve_turn_sequence
from ..utils.plan_progress_authority import ensure_initial_in_progress
from . import hitl_helpers
from .planner_todos import normalize_todo_texts

logger = logging.getLogger(__name__)


def should_activate_todos_for_execution(metadata: Mapping[str, Any]) -> bool:
    """Return True when todo progression may start without leaking pre-approval state."""
    if metadata.get("plan_approved"):
        return True
    return not hitl_helpers.should_require_plan_approval(dict(metadata))


def maybe_activate_initial_todo(todo_list: List[TodoItem], metadata: Mapping[str, Any]) -> None:
    """Activate the first pending todo only when execution is allowed to start."""
    if should_activate_todos_for_execution(metadata):
        ensure_initial_in_progress(todo_list)
        if isinstance(metadata, MutableMapping):
            refresh_bundle_active_todo(metadata, todo_list)


def apply_planning_result(
    interactive: InteractiveState,
    *,
    plan: List[str],
    todo_list: List[Any],
    first_goal: str,
    targets: List[str],
    is_resuming: bool,
    context: Optional[GraphRuntimeContext],
    sequence: Optional[int],
    turn_id: Optional[str],
    reserved_message_id: Optional[int],
) -> None:
    """Apply final planner result state with the existing mutation semantics."""
    facts = interactive.facts
    metadata = facts.metadata

    maybe_activate_initial_todo(todo_list, metadata)

    plan_version = metadata.get("plan_version") or 0
    if is_resuming:
        plan_version = plan_version or 1
    else:
        plan_version = plan_version + 1
    metadata["plan_version"] = plan_version

    # Initialize budgets (shared defaults via build_budget_envelope)
    facts.metadata["runtime_budgets"] = build_budget_envelope().model_dump()

    # Update state
    facts.plan = plan
    facts.todo_list = todo_list
    facts.current_goal = first_goal

    # Store plan context for cache invalidation tracking
    facts.metadata["plan_context"] = create_plan_context(interactive)

    # Capture run id for multi-run tracking
    run_id = resolve_turn_sequence(context, facts.metadata)

    # Ensure stable todo IDs for plan review/streaming (persisted before any interrupt)
    todo_texts = normalize_todo_texts(todo_list)
    todo_id_map = facts.metadata.get("todo_id_map")
    if not isinstance(todo_id_map, list) or len(todo_id_map) != len(todo_texts):
        planning_summary = f"Created plan with {len(plan)} steps. First goal: {first_goal}"
        plan_payload = hitl_helpers.build_plan_review_payload(
            goal=first_goal,
            plan_steps=plan,
            todo_list=todo_texts,
            reasoning=planning_summary,
            targets=targets,
            run_id=run_id,
            plan_version=plan_version,
            turn_sequence=sequence,
            turn_id=turn_id,
            reserved_message_id=reserved_message_id if isinstance(reserved_message_id, int) else None,
        )
        facts.metadata["todo_id_map"] = [item["id"] for item in plan_payload["todo_list"]]

    logger.info(
        f"[CACHE] Plan created at iteration {facts.iterations} "
        f"with capability {facts.capability} for task {facts.task_id}"
    )


__all__ = [
    "apply_planning_result",
    "maybe_activate_initial_todo",
    "should_activate_todos_for_execution",
]
