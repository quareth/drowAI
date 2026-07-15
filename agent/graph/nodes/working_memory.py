"""LangGraph nodes for deterministic working-memory update and validation gating.

After the Phase 4 narrowing, working memory maintained here is
runtime-state only (active target, current goal/objective, latest
decision, selected tool, typed handles). Cross-turn transcript
continuity is authoritative in the shared
``ConversationContextBundle`` (see
``agent/graph/context/contracts.py``); the reducer still tracks
bounded recent-turn excerpts for structural uses (e.g., target
resolution fallback), but those are never surfaced into prompts.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from ..builders.common_edges import ensure_metadata_runtime_budgets
from ..context.runtime_state import (
    refresh_bundle_active_todo,
    refresh_bundle_from_working_memory,
)
from ..infrastructure.state_models import GraphRuntimeContext
from ..memory.memory_manager import MemoryManager
from ..memory.scratchpad import refresh_trace_scratchpad
from ..memory.target_resolution import resolve_target_from_working_memory
from ..state import InteractiveState, TodoItem
from ..utils.plan_progress_authority import ensure_initial_in_progress

_INTENT_BRIEF_SEED_KEY = "intent_brief_seed"
_INTENT_TURN_INTERPRETATION_KEY = "intent_turn_interpretation"
_PLANNER_CURRENT_GOAL_SOURCE = "planner_current_goal"
_SIMPLE_TOOL_CAPABILITY = "simple_tool_execution"
_TASK_SEED_OBJECTIVE_SOURCE = "intent_task_seed"


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value[-4:]:
        if isinstance(item, Mapping):
            raw_content = item.get("content", item.get("message", ""))
            if raw_content is None:
                continue
            content_text = raw_content if isinstance(raw_content, str) else str(raw_content)
            if not content_text.strip():
                continue
            normalized.append(
                {
                    "role": item.get("role", item.get("type", "user")),
                    "content": content_text,
                    "turn_sequence": item.get("turn_sequence", 0),
                }
            )
    return normalized


def _runtime_ids(interactive: InteractiveState, context: Optional[GraphRuntimeContext]) -> dict[str, Any]:
    metadata = interactive.facts.safe_metadata
    turn_sequence = 0
    if context and context.turn_sequence is not None:
        turn_sequence = int(context.turn_sequence)
    elif isinstance(metadata.get("turn_sequence"), int):
        turn_sequence = int(metadata.get("turn_sequence"))

    turn_id = ""
    if context and context.turn_id:
        turn_id = str(context.turn_id)
    elif metadata.get("turn_id"):
        turn_id = str(metadata.get("turn_id"))

    return {
        "task_id": int(interactive.facts.task_id),
        "conversation_id": str(interactive.facts.conversation_id or ""),
        "turn_id": turn_id,
        "turn_sequence": turn_sequence,
    }


def _planner_state_is_authoritative(interactive: InteractiveState, route: str) -> bool:
    """Return True when planner-owned goal state should not be overwritten.

    Used by ``update_working_memory_node`` to gate whether the classifier's
    proposed goal projection is allowed to overwrite the planner-authored
    ``facts.current_goal`` and the working-memory ``objective``. Planner
    authority applies in ``deep_reasoning`` route once a plan has been
    proposed and either is awaiting approval, has been approved, or the
    planner has explicitly entered ``plan_ready`` mode.
    """
    if str(route or "").strip().lower() != "deep_reasoning":
        return False

    facts = interactive.facts
    metadata = facts.safe_metadata
    if str(metadata.get("planner_mode") or "").strip().lower() == "plan_ready":
        return True
    if metadata.get("plan_pending_approval") or metadata.get("plan_approved"):
        return True
    if facts.plan and str(facts.current_goal or "").strip():
        return True
    return False


def _planner_goal_objective(current_goal: str) -> dict[str, Any]:
    """Build a planner-owned objective projection for working memory sync."""
    return {
        "text": current_goal,
        "status": "active",
        "source": _PLANNER_CURRENT_GOAL_SOURCE,
        "provenance": {
            "authority": "llm_proposal",
            "source": _PLANNER_CURRENT_GOAL_SOURCE,
        },
    }


def _task_seed_objective(task: str) -> dict[str, Any]:
    """Build an internal task-seed objective projection for working memory."""
    return {
        "text": task,
        "status": "active",
        "source": _TASK_SEED_OBJECTIVE_SOURCE,
        "provenance": {
            "authority": "derived",
            "source": _TASK_SEED_OBJECTIVE_SOURCE,
        },
    }


def _normalized_task_seed(intent_brief: Mapping[str, Any] | None) -> list[str]:
    """Return a capped list of non-empty classifier task seed strings."""
    if not isinstance(intent_brief, Mapping):
        return []
    raw_seed = intent_brief.get("task_seed")
    if not isinstance(raw_seed, list):
        return []

    normalized: list[str] = []
    for item in raw_seed:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        normalized.append(text)
        if len(normalized) >= 3:
            break
    return normalized


def _maybe_seed_simple_tool_todos(
    *,
    interactive: InteractiveState,
    route: str,
    updated_working_memory: dict[str, Any],
    intent_turn_interpretation: Mapping[str, Any],
) -> None:
    """Seed simple-tool todos from the classifier's internal task seeds.

    Existing planner, resume, checkpoint, or prior graph todos are preserved.
    PTR remains the sole authority that advances seeded todo status.
    """
    if str(route or "").strip().lower() != _SIMPLE_TOOL_CAPABILITY:
        return
    classifier_label = str(
        interactive.facts.safe_metadata.get("intent_classifier_label") or ""
    ).strip().lower()
    if classifier_label and classifier_label != "direct_executor":
        return
    if interactive.facts.safe_todo_list:
        return

    readiness = str(
        intent_turn_interpretation.get("execution_readiness") or ""
    ).strip().lower()
    if readiness != "ready":
        return

    task_seed = _normalized_task_seed(
        _as_mapping(updated_working_memory.get("intent_brief"))
    )
    if not task_seed:
        return

    todo_list = TodoItem.from_string_list(task_seed)
    ensure_initial_in_progress(todo_list)
    interactive.facts.todo_list = todo_list
    first_task = task_seed[0]
    interactive.facts.current_goal = first_task
    updated_working_memory["objective"] = _task_seed_objective(first_task)


def update_working_memory_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
) -> dict:
    """Compute and store deterministic working memory at turn start.

    When the planner is authoritative for the current route and turn (see
    ``_planner_state_is_authoritative``), the planner's existing
    ``facts.current_goal`` is preserved and projected into the working-memory
    objective. Otherwise, the classifier's ``intent_turn_interpretation``
    proposes the next operational goal, which is projected both into the
    working-memory objective and into ``facts.current_goal``.
    """
    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    ensure_metadata_runtime_budgets(metadata)

    previous = metadata.get("working_memory")
    conversation_history_raw = metadata.get("conversation_history")
    conversation_history_tail = _as_history(conversation_history_raw)
    route = str(facts.capability or metadata.get("intent_router", {}).get("chosen_capability") or "chat")
    constraints = _as_mapping(metadata.get("constraints"))
    intent_hints = _as_mapping(getattr(facts, "intent_hints", {}))
    intent_target_continuity = _as_mapping(metadata.get("intent_target_continuity")) or None
    intent_turn_interpretation = _as_mapping(metadata.get(_INTENT_TURN_INTERPRETATION_KEY))
    preserve_planner_authority = _planner_state_is_authoritative(interactive, route)

    updated_working_memory = MemoryManager.reduce_turn_start(
        previous=previous if isinstance(previous, Mapping) else None,
        user_message=facts.message or "",
        conversation_history_tail=conversation_history_tail,
        runtime_ids=_runtime_ids(interactive, context),
        route=route,
        constraints=constraints,
        intent_hints=intent_hints,
        intent_target_continuity=intent_target_continuity,
        intent_turn_interpretation=intent_turn_interpretation or None,
        project_classifier_goal=not preserve_planner_authority,
    )

    intent_brief_seed = metadata.get(_INTENT_BRIEF_SEED_KEY)
    if isinstance(intent_brief_seed, Mapping):
        updated_working_memory = MemoryManager.reduce_intent_brief_fold(
            updated_working_memory,
            intent_brief_seed,
        )
    metadata.pop(_INTENT_BRIEF_SEED_KEY, None)

    if preserve_planner_authority:
        planner_goal = str(facts.current_goal or "").strip()
        if planner_goal:
            updated_working_memory["objective"] = _planner_goal_objective(planner_goal)
    else:
        projected_goal = str(intent_turn_interpretation.get("next_operational_goal") or "").strip()
        facts.current_goal = projected_goal or ""

    _maybe_seed_simple_tool_todos(
        interactive=interactive,
        route=route,
        updated_working_memory=updated_working_memory,
        intent_turn_interpretation=intent_turn_interpretation,
    )

    metadata["working_memory"] = updated_working_memory
    refresh_bundle_from_working_memory(metadata)
    refresh_bundle_active_todo(metadata, facts.todo_list)
    facts.metadata = metadata
    refresh_trace_scratchpad(interactive)
    return interactive.as_graph_update()


def apply_post_tool_active_decision(
    interactive: InteractiveState,
    active_decision: Mapping[str, Any] | None,
) -> None:
    """Apply post-tool active decision payload to canonical working memory."""
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    previous = metadata.get("working_memory")
    metadata["working_memory"] = MemoryManager.reduce_post_tool_decision(
        previous=previous if isinstance(previous, Mapping) else None,
        active_decision=active_decision,
    )
    refresh_bundle_from_working_memory(metadata)
    refresh_bundle_active_todo(metadata, facts.todo_list)
    facts.metadata = metadata
    refresh_trace_scratchpad(interactive)


def apply_post_tool_candidate_findings(
    interactive: InteractiveState,
    candidate_observations: list[Mapping[str, Any]] | None,
) -> None:
    """Apply PTR candidate observations to the working-memory findings store."""
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    previous = metadata.get("working_memory")
    active_target = ""
    if isinstance(previous, Mapping):
        resolved_target = resolve_target_from_working_memory(
            dict(previous),
            intent_referent_key="intent:target",
            recent_turn_limit=4,
        )
        if isinstance(resolved_target, str):
            active_target = resolved_target
    metadata["working_memory"] = MemoryManager.reduce_post_tool_findings(
        previous=previous if isinstance(previous, Mapping) else None,
        candidate_observations=candidate_observations,
        active_target=active_target,
    )
    facts.metadata = metadata
    refresh_trace_scratchpad(interactive)


__all__ = [
    "apply_post_tool_candidate_findings",
    "apply_post_tool_active_decision",
    "update_working_memory_node",
]
