"""Plan-review HITL graph node for deep reasoning plans.

This module owns user approval, rejection, and editing of planner-produced
plans. Initial plan generation remains in ``planner.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

from agent.graph.context.runtime_state import refresh_bundle_active_todo

from ..emission.factory import EventEmitterFactory
from ..infrastructure.state_models import GraphRuntimeContext
from ..state import InteractiveState, TodoItem
from ..utils.event_identity import resolve_stream_identifiers, resolve_turn_sequence
from ..utils.plan_progress_authority import (
    build_todo_stream_updates,
    ensure_initial_in_progress,
    to_stream_status,
)
from ..utils.todo_sync import build_todos_from_plan, sync_todos_with_plan
from .planner_todos import normalize_todo_texts
from .hitl_helpers import (
    build_interrupt_id,
    build_plan_review_payload,
    request_plan_approval,
    should_require_plan_approval,
)

if TYPE_CHECKING:
    from langgraph.types import StreamWriter

logger = logging.getLogger(__name__)


def _build_plan_payload_with_ids(
    *,
    goal: str,
    plan_steps: List[str],
    todo_items: List[Any],
    todo_texts: List[str],
    metadata: Dict[str, Any],
    reasoning: Optional[str],
    targets: List[str],
    run_id: Optional[int],
    plan_version: Optional[int],
) -> Dict[str, Any]:
    """Build plan review payload using persisted todo IDs when available."""
    todo_id_map = metadata.get("todo_id_map")
    turn_sequence = metadata.get("turn_sequence")
    if not isinstance(turn_sequence, int):
        turn_sequence = None
    turn_id = metadata.get("turn_id")
    if not isinstance(turn_id, str):
        turn_id = None
    reserved_message_id = metadata.get("reserved_message_id")
    if not isinstance(reserved_message_id, int):
        reserved_message_id = None
    interrupt_id = metadata.get("interrupt_id")
    if not isinstance(interrupt_id, str) or not interrupt_id.strip():
        interrupt_id = build_interrupt_id()
    metadata["interrupt_id"] = interrupt_id

    if isinstance(todo_id_map, list) and len(todo_id_map) == len(todo_texts):
        todo_items = [
            {
                "id": todo_id_map[index],
                "text": text,
                "status": _resolve_stream_status(todo_items[index]),
            }
            for index, text in enumerate(todo_texts)
        ]
        payload = {
            "type": "plan_review",
            "interrupt_id": interrupt_id,
            "goal": goal,
            "plan_steps": plan_steps,
            "todo_list": todo_items,
            "reasoning": reasoning,
            "targets": targets or [],
            "run_id": run_id,
            "plan_version": plan_version,
        }
        if turn_sequence is not None:
            payload["turn_sequence"] = turn_sequence
        if turn_id:
            payload["turn_id"] = turn_id
        if reserved_message_id is not None:
            payload["reserved_message_id"] = reserved_message_id
        return payload

    plan_payload = build_plan_review_payload(
        goal=goal,
        plan_steps=plan_steps,
        todo_list=todo_texts,
        reasoning=reasoning,
        targets=targets,
        run_id=run_id,
        plan_version=plan_version,
        turn_sequence=turn_sequence,
        turn_id=turn_id,
        reserved_message_id=reserved_message_id,
        interrupt_id=interrupt_id,
    )
    metadata["todo_id_map"] = [item["id"] for item in plan_payload["todo_list"]]
    return plan_payload


def _resolve_stream_status(todo_item: Any) -> str:
    """Resolve stream status from canonical todo item when possible."""
    if isinstance(todo_item, TodoItem):
        return to_stream_status(todo_item)
    if isinstance(todo_item, dict):
        status = todo_item.get("status")
        if isinstance(status, str) and status.strip():
            return status.strip().lower()
    # Legacy fallback for text-only todo entries.
    return "pending"


async def plan_review_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Dict[str, Any]] = None,
    writer: Optional["StreamWriter"] = None,
) -> dict:
    """Request plan approval (if required) after plan generation.

    This node is separated from planner_node so plan state is checkpointed
    before any interrupt, preventing duplicate LLM calls on resume.
    """
    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    trace = interactive.trace
    metadata = facts.ensure_metadata()
    facts.metadata = metadata
    _, turn_id = resolve_stream_identifiers(interactive, config)

    plan = facts.plan or []
    facts.todo_list = sync_todos_with_plan(plan, facts.todo_list)
    requires_plan_approval = should_require_plan_approval(metadata)
    if not requires_plan_approval or metadata.get("plan_approved"):
        ensure_initial_in_progress(facts.todo_list)
    refresh_bundle_active_todo(metadata, facts.todo_list)
    todo_texts = normalize_todo_texts(facts.todo_list)
    first_goal = facts.current_goal or ""
    targets = list((facts.intent_hints or {}).get("targets", []))
    plan_version = metadata.get("plan_version") or 1

    if not plan:
        logger.warning("[PLAN_REVIEW] No plan available; skipping plan review")
        return interactive.as_graph_update()

    resolved_run_id = resolve_turn_sequence(context, metadata)
    if isinstance(resolved_run_id, int):
        run_id = resolved_run_id
    else:
        run_id = metadata.get("turn_sequence") if isinstance(metadata.get("turn_sequence"), int) else 0
    planning_summary = f"Created plan with {len(plan)} steps. First goal: {first_goal}"
    plan_payload = _build_plan_payload_with_ids(
        goal=first_goal,
        plan_steps=plan,
        todo_items=facts.todo_list,
        todo_texts=todo_texts,
        metadata=metadata,
        reasoning=planning_summary,
        targets=targets,
        run_id=run_id,
        plan_version=plan_version,
    )

    if requires_plan_approval:
        user_response = request_plan_approval(
            goal=first_goal,
            plan_steps=plan,
            todo_list=todo_texts,
            reasoning=planning_summary,
            targets=targets,
            run_id=run_id,
            payload=plan_payload,
            metadata=metadata,
            turn_sequence=resolve_turn_sequence(context, metadata),
            turn_id=turn_id,
            reserved_message_id=metadata.get("reserved_message_id"),
        )

        action = user_response.get("action", "approve")
        if action == "reject":
            logger.info("[PLANNER] Plan rejected by user")
            metadata["plan_rejected"] = True
            metadata.pop("plan_pending_approval", None)
            facts.ensure_decision_history().append("finalize: user rejected plan")
            return interactive.as_graph_update()

        before_authoritative_sync = [todo.model_copy(deep=True) for todo in facts.todo_list]

        if action == "edit":
            if user_response.get("edited_goal"):
                first_goal = user_response["edited_goal"]
                facts.current_goal = first_goal

            if user_response.get("edited_plan_steps"):
                plan = user_response["edited_plan_steps"]
                facts.plan = plan
                plan_version = plan_version + 1
                metadata["plan_version"] = plan_version
                facts.todo_list = build_todos_from_plan(plan)
                # When the user edits plan steps without also editing the
                # goal, resync `current_goal` from the new first step so
                # downstream prompt consumers (tool-planning, PTR) do not
                # render a stale goal that would steer the LLM back to the
                # original first step.
                if not user_response.get("edited_goal") and plan:
                    first_goal = str(plan[0])
                    facts.current_goal = first_goal

            facts.todo_list = sync_todos_with_plan(plan, facts.todo_list)
            todo_texts = normalize_todo_texts(facts.todo_list)

            planning_summary = f"Created plan with {len(plan)} steps. First goal: {first_goal}"
            plan_payload = build_plan_review_payload(
                goal=first_goal,
                plan_steps=plan,
                todo_list=todo_texts,
                reasoning=planning_summary,
                targets=targets,
                run_id=run_id,
                plan_version=plan_version,
                turn_sequence=resolve_turn_sequence(context, metadata),
                turn_id=turn_id,
                reserved_message_id=metadata.get("reserved_message_id"),
            )
            metadata["todo_id_map"] = [item["id"] for item in plan_payload["todo_list"]]
            logger.info("[PLANNER] Plan edited by user: %d steps", len(plan))

        facts.todo_list = sync_todos_with_plan(plan, facts.todo_list)
        ensure_initial_in_progress(facts.todo_list)
        # Keep the context bundle's `active_todo` slot in sync with the
        # post-edit / post-approve todo list. The pre-interrupt refresh
        # at the top of this node captured the ORIGINAL first step;
        # without this call, downstream prompt consumers (planner,
        # category_selector, articulation) would read a stale
        # descriptor and drive the LLM against the original plan.
        refresh_bundle_active_todo(metadata, facts.todo_list)
        todo_texts = normalize_todo_texts(facts.todo_list)
        planning_summary = f"Created plan with {len(plan)} steps. First goal: {first_goal}"
        plan_payload = _build_plan_payload_with_ids(
            goal=first_goal,
            plan_steps=plan,
            todo_items=facts.todo_list,
            todo_texts=todo_texts,
            metadata=metadata,
            reasoning=planning_summary,
            targets=targets,
            run_id=run_id,
            plan_version=plan_version,
        )

        logger.info("[PLANNER] Plan approved by user (action=%s)", action)
        metadata["plan_approved"] = True
        metadata["plan_approval_action"] = action
        metadata.pop("plan_pending_approval", None)

        if writer is not None:
            emitter = EventEmitterFactory.create(writer, interactive, config, context)
            todo_updates = build_todo_stream_updates(
                before_authoritative_sync,
                facts.todo_list,
                todo_id_map=metadata.get("todo_id_map")
                if isinstance(metadata.get("todo_id_map"), list)
                else None,
            )
            if todo_updates:
                emitter.emit_todo_progress(
                    todo_updates,
                    run_id=run_id,
                    plan_version=plan_version,
                )
            else:
                emitter.emit_plan_created(
                    goal=first_goal,
                    plan_steps=plan,
                    todo_list=plan_payload["todo_list"],
                    run_id=run_id,
                    plan_version=plan_version,
                )
    else:
        if writer is not None:
            emitter = EventEmitterFactory.create(writer, interactive, config, context)
            emitter.emit_plan_created(
                goal=first_goal,
                plan_steps=plan,
                todo_list=plan_payload["todo_list"],
                run_id=run_id,
                plan_version=plan_version,
            )

    trace.reasoning.append(planning_summary)
    trace.reasoning.append(f"Plan: {', '.join(plan)}")

    return interactive.as_graph_update()


__all__ = ["plan_review_node"]
