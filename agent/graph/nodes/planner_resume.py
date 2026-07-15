"""Planner resume detection and result preparation.

This module owns existing-plan, approved-plan, and pending-approval resume
detection for the planner node. It prepares the plan/todo/current-goal values
used when resuming, but it does not apply graph-state writes, plan versioning,
budgets, cache context, todo activation, or todo ID seeding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, List

from ..state import InteractiveState
from ..utils.todo_sync import sync_todos_with_plan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannerResumeState:
    """Planner-owned resume facts consumed by planner_node orchestration."""

    has_existing_plan: Any
    plan_approved: Any
    plan_pending_approval: Any
    is_resuming: Any
    plan: List[str]
    todo_list: List[Any]
    first_goal: str


def detect_planner_resume_state(interactive: InteractiveState) -> PlannerResumeState:
    """Detect whether planner execution is resuming with an existing plan."""
    facts = interactive.facts
    existing_plan = facts.plan
    existing_goal = facts.current_goal
    existing_todo = facts.todo_list
    plan_approved = facts.metadata.get("plan_approved", False)
    plan_pending_approval = facts.metadata.get("plan_pending_approval", False)

    # The simplest and most reliable check: if we have a plan + goal, we're resuming.
    # We also check plan_approved/plan_pending_approval as additional signals.
    has_existing_plan = existing_plan and len(existing_plan) > 0 and existing_goal

    # Debug logging to understand state on entry
    logger.info(
        f"[PLANNER] State check: existing_plan={len(existing_plan) if existing_plan else 0} steps, "
        f"existing_goal={bool(existing_goal)}, has_existing_plan={has_existing_plan}, "
        f"plan_approved={plan_approved}, plan_pending_approval={plan_pending_approval}"
    )

    # Skip LLM if we have an existing plan (most reliable indicator of resume)
    # OR if plan was already approved (defensive check)
    # OR if plan is pending approval (flag set before interrupt)
    is_resuming = has_existing_plan or plan_approved or plan_pending_approval

    return PlannerResumeState(
        has_existing_plan=has_existing_plan,
        plan_approved=plan_approved,
        plan_pending_approval=plan_pending_approval,
        is_resuming=is_resuming,
        plan=existing_plan or [],
        todo_list=existing_todo or [],
        first_goal=existing_goal or "",
    )


def build_resume_planning_result(resume_state: PlannerResumeState) -> PlannerResumeState:
    """Prepare plan, todo list, and first goal for a planner resume path."""
    logger.info(
        f"[PLANNER] ✅ SKIPPING LLM - Reusing existing plan ({len(resume_state.plan) if resume_state.plan else 0} steps) - "
        f"resuming from interrupt/approval (has_existing_plan={resume_state.has_existing_plan}, "
        f"plan_approved={resume_state.plan_approved}, plan_pending_approval={resume_state.plan_pending_approval})"
    )
    plan = resume_state.plan or []
    todo_list = sync_todos_with_plan(plan, resume_state.todo_list or [])
    first_goal = resume_state.first_goal or ""
    return replace(resume_state, plan=plan, todo_list=todo_list, first_goal=first_goal)


__all__ = [
    "PlannerResumeState",
    "build_resume_planning_result",
    "detect_planner_resume_state",
]
