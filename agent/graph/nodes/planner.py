"""Initial planner node for deep reasoning task decomposition.

This node is a brief consumer: it sources current-turn meaning
exclusively from ``working_memory.intent_brief``. It does NOT read
``ConversationContextBundle`` transcript text. Full-history access is
reserved for the intent classifier and the deep-reasoning finalizer
(see ``docs/plans/intent_interpretation_wiring.md``).

Planner-owned authority for initial plan / todo_list / current_goal creation
and planner-side clarify lifecycle remains here. Plan approval routing is
delegated to ``plan_review.py``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from ..infrastructure.state_models import GraphRuntimeContext
from ..state import InteractiveState
from ..utils.event_identity import (
    resolve_stream_identifiers,
    resolve_turn_sequence,
)
from ..emission.reasoning_section import reasoning_section
from . import (
    planner_clarify,
    planner_generation,
    planner_resume,
    planner_setup,
    planner_state,
)

if TYPE_CHECKING:
    from langgraph.types import StreamWriter

logger = logging.getLogger(__name__)


async def planner_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Dict[str, Any]] = None,
    writer: Optional["StreamWriter"] = None,
) -> dict:
    """Decompose user request into multi-step plan and initialize budgets.
    
    This node:
    1. Analyzes the user's request
    2. Creates a concrete multi-step plan
    3. Generates initial todo list
    4. Initializes execution budgets
    5. Sets the first goal
    
    Args:
        state: Current graph state
        context: Runtime context with API keys, workspace path, etc.
    
    Returns:
        State update dict with plan, todo list, and budgets
    """
    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    setup = planner_setup.build_planner_setup(interactive)
    metadata = setup.metadata
    targets = setup.targets
    user_message = setup.user_message

    clarify_outcome = planner_clarify.handle_existing_clarify_state(
        interactive,
        stage="failed",
    )
    if clarify_outcome.handled:
        return clarify_outcome.update or interactive.as_graph_update()

    if (facts.capability or "").lower() == "deep_reasoning":
        dr_meta = metadata.setdefault("dr_iteration_meta", {})
        dr_meta.pop("active_iteration", None)
        metadata["dr_iteration_meta"] = dr_meta

    clarify_outcome = planner_clarify.handle_existing_clarify_state(interactive, stage="pending")
    if clarify_outcome.handled:
        return clarify_outcome.update or interactive.as_graph_update()
    
    setup = planner_setup.evaluate_tool_availability(interactive, setup)
    if setup.tool_unavailable_update is not None:
        return setup.tool_unavailable_update
    targets = setup.targets
    user_message = setup.user_message
    
    logger.info(f"[PLANNER] Starting planning for user request: {user_message[:100]}...")
    
    # Resolve identity for non-emitter uses (plan payload, etc.)
    conversation_id, turn_id = resolve_stream_identifiers(interactive, config)
    sequence = resolve_turn_sequence(context, facts.metadata)
    reserved_message_id = metadata.get("reserved_message_id")

    # CRITICAL: Skip LLM if we already have a plan (resuming from interrupt)
    # When LangGraph resumes, it restores state from checkpoint which includes the plan.
    resume_state = planner_resume.detect_planner_resume_state(interactive)

    if resume_state.is_resuming:
        resume_state = planner_resume.build_resume_planning_result(resume_state)
        plan = resume_state.plan
        todo_list = resume_state.todo_list
        first_goal = resume_state.first_goal
        
        # Still emit reasoning events for thinking card, but indicate we're resuming.
        planning_message = (
            f"Resuming with approved plan: {first_goal[:100]}{'...' if len(first_goal) > 100 else ''}"
        )
        async with reasoning_section(
            writer,
            state=interactive,
            step="planning",
            label=planning_message,
            config=config,
            context=context,
        ):
            pass
    else:
        generation_result = await planner_generation.run_planning_generation(
            interactive,
            setup,
            context=context,
            config=config,
            writer=writer,
        )
        if generation_result.returned_update is not None:
            return generation_result.returned_update
        plan = generation_result.plan
        todo_list = generation_result.todo_list
        first_goal = generation_result.first_goal

    planner_state.apply_planning_result(
        interactive,
        plan=plan,
        todo_list=todo_list,
        first_goal=first_goal,
        targets=targets,
        is_resuming=bool(resume_state.is_resuming),
        context=context,
        sequence=sequence,
        turn_id=turn_id,
        reserved_message_id=reserved_message_id if isinstance(reserved_message_id, int) else None,
    )

    return interactive.as_graph_update()


__all__ = ["planner_node"]
