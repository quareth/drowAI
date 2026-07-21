"""Reflect node for failure analysis and replanning."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from ..builders.common_edges import decrement_iteration_budget
from ..emission.reasoning_section import reasoning_section
from ..infrastructure.state_models import GraphRuntimeContext
from ..memory.findings import build_relevant_findings_for_prompt
from ..state import InteractiveState
from ..utils import iteration_memory as _iteration_memory
from ..utils.environment_loader import get_environment_full
from ..utils.event_identity import resolve_turn_sequence
from core.llm import LLM_TIMEOUT_REFLECT_SEC, wait_for_with_timeout
from core.llm.structured_schemas import REFLECT_STRUCTURED_OUTPUT
from .decision_router.helpers import extract_action_label
from .node_utils import (
    append_usage_to_state,
    determine_post_reflect_action,
)
from ..utils.llm_resolver import (
    ROLE_REASONING_MAIN,
    get_llm_reasoning_effort,
    has_llm_runtime_services,
    resolve_llm_client,
)
from agent.providers.llm.core.exceptions import (
    LLMConfigurationError,
    LLMProviderError,
    LLMRefusalError,
)
from agent.tools.capability_surface import render_capability_surface
from backend.services.metrics.utils import safe_inc
from core.prompts.builders.reflect import (
    build_reflection_fallback_guidance,
    build_reflection_prompt,
)

if TYPE_CHECKING:
    from langgraph.types import StreamWriter

logger = logging.getLogger(__name__)


async def reflect_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Mapping[str, Any]] = None,
    writer: Optional["StreamWriter"] = None,
) -> dict:
    """Analyze stuck loops and generate alternative approaches.
    
    NOTE: Tool failures are now handled by post_tool_reasoning, which is the
    primary recovery mechanism for execution issues. This node focuses on:
    - Stuck loops (repeating same action without progress)
    - Strategic failures (approach not working after multiple attempts)
    - Decision paralysis (unable to choose next action)
    
    This node:
    1. Identifies stuck loop patterns (repetition, no progress)
    2. Analyzes why the strategy isn't working
    3. Generates alternative approaches
    4. Updates plan with new strategies
    5. Resets stuck counter
    6. Decrements iteration budget
    
    Args:
        state: Current graph state
        context: Runtime context
    
    Returns:
        State update dict with updated plan and reset stuck counter
    """
    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    turn_sequence = resolve_turn_sequence(context, metadata)
    
    # Identify what went wrong
    problem_description = _identify_problem(interactive)
    
    # Build reflection prompt
    reflection_prompt = _build_reflection_prompt(interactive, problem_description, context)
    system_prompt = "You are an expert problem analyzer helping troubleshoot pentesting issues."
    fallback_guidance: Optional[str] = None
    
    # Get LLMClient (sync - just creates client object)
    try:
        llm_client = resolve_llm_client(
            facts.metadata,
            context,
            config=config,
            role=ROLE_REASONING_MAIN,
        )
        reasoning_effort = get_llm_reasoning_effort(llm_client)
    except LLMConfigurationError:
        if has_llm_runtime_services(config):
            raise
        # Fallback: simple reflection without LLM
        logger.warning("No LLMClient for reflection, using simple fallback")
        fallback_guidance = _apply_fallback_reflection(interactive, problem_description)
        llm_client = None
    
    parsed_payload_for_memory: Optional[Dict[str, Any]] = None
    used_fallback_reflection = False
    if llm_client is not None:
        # Use LLM for reflection with usage tracking (Phase 7)
        try:
            async with reasoning_section(
                writer,
                state=interactive,
                step="reflection",
                label="Re-evaluating the strategy.",
                config=config,
                context=context,
            ):
                llm_response = await wait_for_with_timeout(
                    llm_client.chat_with_usage(
                        system_prompt,
                        reflection_prompt,
                        temperature=0.5,  # Higher temperature for creative alternatives
                        reasoning_effort=reasoning_effort,
                        structured_output=REFLECT_STRUCTURED_OUTPUT,
                    ),
                    timeout_sec=LLM_TIMEOUT_REFLECT_SEC,
                    component="REASONING_MAIN",
                    operation="reflect_llm_call",
                    logger=logger,
                    task_id=facts.task_id,
                    outcome="reflect_timeout",
                )
            response = llm_response.content
            append_usage_to_state(
                interactive,
                llm_response.usage,
                "reflect",
                request_mode="non_streaming",
            )
            structured_payload = getattr(llm_response, "structured_output", None)
            parsed_payload_for_memory = _extract_reflection_payload(
                response=response,
                structured_payload=structured_payload if isinstance(structured_payload, Mapping) else None,
            )
            
            # Parse reflection response
            success = _apply_reflection_payload(
                parsed_payload_for_memory,
                interactive,
            )
            
            if not success:
                logger.warning("Failed to parse reflection response, using fallback")
                fallback_guidance = _apply_fallback_reflection(interactive, problem_description)
                used_fallback_reflection = True
                
        except LLMRefusalError:
            raise
        except LLMProviderError:
            raise
        except Exception as exc:
            logger.error(f"Reflect node LLM call failed: {exc}")
            fallback_guidance = _apply_fallback_reflection(interactive, problem_description)
            used_fallback_reflection = True
    else:
        used_fallback_reflection = True
    
    # Determine deterministic one-hop follow-up action for router reflect recovery.
    next_action = determine_post_reflect_action(facts.todo_list)

    # Decrement iteration budget
    budget_update = decrement_iteration_budget(state)
    facts_update = budget_update.get("facts", {})
    
    # Merge budget updates
    facts.iterations = facts_update.get("iterations", facts.iterations)
    if "runtime_budgets" in facts_update:
        facts.metadata["runtime_budgets"] = facts_update["runtime_budgets"]

    # Stamp canonical reflect hint with the post-budget iteration that router consumes.
    hint_id = f"reflect-{facts.iterations}-{next_action}"
    metadata["next_after_reflect"] = {
        "action": next_action,
        "hint_id": hint_id,
        "issued_at_iteration": facts.iterations,
    }
    _record_reflect_phase_memory(
        interactive,
        turn_sequence=turn_sequence if isinstance(turn_sequence, int) else None,
        parsed_payload=parsed_payload_for_memory,
        next_action=next_action,
        used_fallback=used_fallback_reflection,
        fallback_guidance=fallback_guidance,
    )
    facts.post_reflect_action = next_action
    _remove_trailing_reflections(interactive)
    facts.stuck_counter = 0
    interactive.trace.reasoning.append(
        "Reflection resolved: queued deterministic one-hop post-reflection "
        f"action `{next_action}` (hint_id={hint_id})"
    )
    
    logger.info(f"Reflect node completed for task {facts.task_id}, stuck counter reset")
    
    return interactive.as_graph_update()


def _remove_trailing_reflections(interactive: InteractiveState) -> None:
    """Remove consecutive reflection entries from decision history."""

    history = interactive.facts.decision_history
    if not history:
        return

    while history and extract_action_label(history[-1]) == "reflect":
        history.pop()


def _identify_problem(interactive: InteractiveState) -> str:
    """Identify stuck loop patterns that triggered reflection.
    
    NOTE: Tool failures are handled by post_tool_reasoning. This focuses on
    higher-level strategic issues like repetition and lack of progress.
    """
    facts = interactive.facts
    trace = interactive.trace
    
    problems = []
    
    # PRIORITY: Check stuck counter (action repetition)
    if facts.stuck_counter >= 3:
        problems.append(f"Stuck in loop: repeated the same action {facts.stuck_counter} times without progress")
        # Increment metric for stuck loop detection
        safe_inc("reflect_triggered_stuck_loop")

    # Check decision history for repetition patterns
    decision_history = facts.safe_decision_history
    if len(decision_history) >= 3:
        recent_decisions = decision_history[-3:]
        # Check for exact repetition
        if len(set(recent_decisions)) == 1:
            problems.append(f"Decision paralysis: same decision repeated 3+ times ({recent_decisions[0]})")
            safe_inc("reflect_triggered_decision_paralysis")
        # Check for oscillation (A→B→A→B pattern)
        elif len(decision_history) >= 4 and len(set(decision_history[-4:])) == 2:
            problems.append(f"Oscillating between two decisions: {decision_history[-4:]}")
            safe_inc("reflect_triggered_oscillation")

    # Check if no progress in many iterations (strategy not working)
    executed_tools = trace.executed_tools or []
    if facts.iterations > 5 and not executed_tools:
        problems.append("Multiple iterations with no tool execution - strategy may need revision")
        safe_inc("reflect_triggered_no_progress")

    # Check for todo list stagnation (items not completing)
    todo_list = facts.safe_todo_list
    if len(todo_list) > 0 and facts.iterations > 3:
        completed = sum(1 for t in todo_list if hasattr(t, 'is_complete') and t.is_complete())
        if completed == 0:
            problems.append(f"No todos completed after {facts.iterations} iterations - approach may need revision")
            safe_inc("reflect_triggered_todo_stagnation")
    
    if problems:
        return "; ".join(problems)
    else:
        return "Strategic reflection triggered (evaluating overall approach)"


def _build_reflection_prompt(
    interactive: InteractiveState,
    problem: str,
    context: Optional[GraphRuntimeContext],
) -> str:
    """Build prompt for failure reflection from canonical state projections.

    Mirrors ``synthesis_node._build_synthesis_prompt`` and
    ``think_more_node`` in computing the keyword-only context kwargs
    (turn / phase identifiers, relevant findings, environment context,
    recent decisions) and forwarding them to the canonical
    ``build_reflection_prompt`` builder.
    """
    facts = interactive.facts
    metadata = facts.safe_metadata

    # Compute canonical prompt-context kwargs (mirroring synthesis_node).
    turn_sequence = resolve_turn_sequence(context, metadata)
    if isinstance(turn_sequence, int):
        current_phase_sequence = _iteration_memory.peek_next_phase_sequence(
            dict(metadata),
            turn_sequence=turn_sequence,
        )
        latest_recorded_phase_sequence_value = _iteration_memory.latest_recorded_phase_sequence(
            dict(metadata),
            turn_sequence=turn_sequence,
        )
    else:
        current_phase_sequence = None
        latest_recorded_phase_sequence_value = None
    relevant_findings = build_relevant_findings_for_prompt(interactive)
    capability_surface = render_capability_surface()
    environment_context = get_environment_full(metadata.get("environment_info"))
    recent_decisions = list(facts.safe_decision_history[-5:])

    return build_reflection_prompt(
        interactive.as_graph_state(),
        problem=problem,
        recent_decisions=recent_decisions,
        turn_sequence=turn_sequence,
        current_phase_sequence=current_phase_sequence,
        latest_recorded_phase_sequence=latest_recorded_phase_sequence_value,
        relevant_findings=relevant_findings,
        capability_surface=capability_surface,
        environment_context=environment_context,
    )


def _parse_reflection_response(
    response: str,
    interactive: InteractiveState,
    structured_payload: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Parse LLM reflection response and update state."""
    parsed_payload = _extract_reflection_payload(
        response=response,
        structured_payload=structured_payload,
    )
    return _apply_reflection_payload(parsed_payload, interactive)


def _extract_reflection_payload(
    *,
    response: str,
    structured_payload: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Extract reflection structured payload from explicit or textual response."""
    if isinstance(structured_payload, Mapping):
        return dict(structured_payload)

    json_start = response.find("{")
    json_end = response.rfind("}") + 1
    if json_start < 0 or json_end <= json_start:
        return None

    try:
        maybe_parsed = json.loads(response[json_start:json_end])
    except json.JSONDecodeError:
        return None
    return dict(maybe_parsed) if isinstance(maybe_parsed, dict) else None


def _apply_reflection_payload(
    parsed_payload: Optional[Dict[str, Any]],
    interactive: InteractiveState,
) -> bool:
    """Apply parsed reflection payload to state."""
    try:
        if isinstance(parsed_payload, dict):
            # Extract fields
            root_cause = parsed_payload.get("root_cause", "")
            alternatives = parsed_payload.get("alternative_approaches", [])
            
            # Log root cause analysis
            if root_cause:
                root_entry = f"Reflection - Root cause: {root_cause}"
                interactive.trace.reasoning.append(root_entry)
            
            # Log alternatives
            if alternatives:
                alternatives_str = "; ".join(alternatives[:3])  # Top 3
                alt_entry = f"Alternative approaches: {alternatives_str}"
                interactive.trace.reasoning.append(alt_entry)
            
            return True
            
    except (json.JSONDecodeError, KeyError, AttributeError) as exc:
        logger.warning(f"Failed to parse reflection response: {exc}")
    
    return False


def _record_reflect_phase_memory(
    interactive: InteractiveState,
    *,
    turn_sequence: Optional[int],
    parsed_payload: Optional[Mapping[str, Any]],
    next_action: str,
    used_fallback: bool,
    fallback_guidance: Optional[str] = None,
) -> None:
    """Write section-snapshot reflection phase memory for PTR continuity."""
    metadata = interactive.facts.ensure_metadata()
    if not isinstance(turn_sequence, int):
        return

    root_cause = ""
    alternatives: list[str] = []
    if isinstance(parsed_payload, Mapping):
        root_cause = str(parsed_payload.get("root_cause") or "").strip()
        raw_alternatives = parsed_payload.get("alternative_approaches")
        if isinstance(raw_alternatives, list):
            alternatives = [str(item).strip() for item in raw_alternatives if str(item).strip()]

    summary = str(fallback_guidance or "").strip() if used_fallback else root_cause
    if not summary and interactive.trace.reasoning:
        summary = str(interactive.trace.reasoning[-1]).strip()
    max_summary_chars = 1200 if used_fallback else 280
    if len(summary) > max_summary_chars:
        summary = summary[:max_summary_chars] + "..."

    overview_lines = [f"status: {'fallback' if used_fallback else 'completed'}"]

    sections: list[Dict[str, str]] = [
        {"heading": "Reflection", "body": "\n".join(overview_lines)},
        {
            "heading": "Root Cause",
            "body": summary or "reflect step completed",
        },
    ]
    if alternatives:
        sections.append(
            {
                "heading": "Alternative Approaches",
                "body": "\n".join(f"- {item}" for item in alternatives[:5]),
            }
        )
    sections.append({"heading": "Next Action", "body": next_action})

    payload: Dict[str, Any] = {"sections": sections}

    _iteration_memory.append(
        metadata,
        turn_sequence=turn_sequence,
        source="reflect",
        payload=payload,
    )


def _apply_fallback_reflection(interactive: InteractiveState, problem: str) -> str:
    """Apply prompt-owned fallback reflection guidance when LLM unavailable."""
    trace = interactive.trace

    reflection = build_reflection_fallback_guidance(problem)
    
    # Keep reflection output in reasoning/history; scratchpad remains memory-rendered.
    fallback_entry = f"Reflection (fallback): {reflection}"
    trace.reasoning.append(fallback_entry)
    return reflection


__all__ = ["reflect_node"]
