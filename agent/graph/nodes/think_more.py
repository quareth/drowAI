"""Think more node for pure reasoning without tool execution."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from ..builders.common_edges import decrement_iteration_budget
from ..context.runtime_state import sync_target_hint_from_plan_todo
from ..infrastructure.state_models import GraphRuntimeContext
from core.prompts.builders.deep_reasoning import DeepReasoningPromptBuilder
from core.llm import LLM_TIMEOUT_REASONING_MAIN_SEC, wait_for_with_timeout
from core.llm.structured_schemas import THINK_MORE_STRUCTURED_OUTPUT
from ..memory.findings import build_relevant_findings_for_prompt
from ..state import InteractiveState
from backend.services.metrics.utils import safe_inc as _safe_inc
from ..utils.environment_loader import get_environment_full
from ..utils.llm_resolver import (
    ROLE_REASONING_MAIN,
    get_llm_reasoning_effort,
    resolve_llm_client,
)
from ..utils.plan_validation import merge_plans, should_reject_plan_update, validate_plan_quality
from ..emission.factory import EventEmitterFactory
from ..emission.reasoning_section import reasoning_section
from ..utils.event_identity import derive_dr_stream_identifiers, resolve_turn_sequence
from ..utils import iteration_memory as _iteration_memory
from ..utils.dr_iteration_state import (
    record_dr_reasoning_snippet,
)
from ..utils.todo_sync import build_todos_from_plan
from .node_utils import append_usage_to_state
from agent.graph.config.token_limits import LIMITS
from agent.providers.llm.core.exceptions import (
    LLMConfigurationError,
    LLMRefusalError,
)
from agent.tools.capability_surface import render_capability_surface

if TYPE_CHECKING:
    from langgraph.types import StreamWriter

logger = logging.getLogger(__name__)


async def think_more_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Dict[str, Any]] = None,
    writer: Optional["StreamWriter"] = None,
) -> dict:
    """Pure reasoning node that refines plan and reasoning trace.
    
    This node:
    1. Analyzes current observations and tool results
    2. Updates reasoning trace
    3. Refines plan if needed based on new information
    4. Sets next goal
    5. Decrements iteration budget
    
    Args:
        state: Current graph state
        context: Runtime context
    
    Returns:
        State update dict with updated plan and goal
    """
    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    metadata = facts.ensure_metadata()
    sync_target_hint_from_plan_todo(
        metadata,
        todo_list=list(facts.safe_todo_list),
        plan=list(facts.plan or []),
        current_goal=facts.current_goal,
    )

    # Compute canonical prompt-context kwargs (mirroring PTR idioms).
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

    # Build thinking prompt
    prompt_builder = DeepReasoningPromptBuilder()
    think_prompt = prompt_builder.build_think_more_prompt(
        state,
        turn_sequence=turn_sequence,
        current_phase_sequence=current_phase_sequence,
        latest_recorded_phase_sequence=latest_recorded_phase_sequence_value,
        relevant_findings=relevant_findings,
        capability_surface=capability_surface,
        environment_context=environment_context,
    )
    system_prompt = prompt_builder.build_system_prompt(state)
    
    # Get LLMClient
    try:
        llm_client = resolve_llm_client(
            facts.metadata,
            context,
            config=config,
            role=ROLE_REASONING_MAIN,
        )
        reasoning_effort = get_llm_reasoning_effort(llm_client)
    except LLMConfigurationError:
        llm_client = None
    
    if llm_client is None:
        # Fallback: add simple reasoning without LLM
        logger.warning("No LLMClient for think_more, using simple fallback")
        _apply_fallback_thinking(interactive)
        _record_think_more_phase_memory(
            interactive,
            turn_sequence=turn_sequence if isinstance(turn_sequence, int) else None,
            parsed_payload=None,
            used_fallback=True,
        )
    else:
        try:
            structured_payload: Optional[Mapping[str, Any]] = None
            if writer:
                emitter = EventEmitterFactory.create(writer, interactive, config, context)
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": think_prompt},
                ]
                try:
                    dr_iteration = None
                    if (facts.capability or "").lower() == "deep_reasoning":
                        # Track iteration internally (does NOT change event identity)
                        _, _, dr_iteration = derive_dr_stream_identifiers(
                            interactive,
                            config,
                            advance_iteration=True,
                        )

                    response = await emitter.stream_reasoning(
                        llm_client,
                        messages,
                        step="reasoning_loop",
                        section_name="reasoning_loop",
                        temperature=0.4,
                        max_tokens=LIMITS.think_more,
                        reasoning_effort=reasoning_effort,
                        timeout_sec=LLM_TIMEOUT_REASONING_MAIN_SEC,
                        task_id=facts.task_id,
                    )
                    if dr_iteration is not None:
                        record_dr_reasoning_snippet(interactive, dr_iteration, response)
                except LLMRefusalError:
                    raise
                except Exception as stream_exc:
                    logger.warning(
                        "[THINK_MORE] Streaming failed, falling back to non-streaming reasoning: %s",
                        stream_exc,
                    )
                    # Fallback: use chat_with_usage (Phase 7)
                    async with reasoning_section(
                        writer,
                        state=interactive,
                        step="reasoning_loop",
                        label="Refining reasoning and plan.",
                        config=config,
                        context=context,
                    ):
                        llm_response = await wait_for_with_timeout(
                            llm_client.chat_with_usage(
                                system_prompt,
                                think_prompt,
                                temperature=0.4,
                                max_tokens=LIMITS.think_more,
                                reasoning_effort=reasoning_effort,
                                structured_output=THINK_MORE_STRUCTURED_OUTPUT,
                            ),
                            timeout_sec=LLM_TIMEOUT_REASONING_MAIN_SEC,
                            component="REASONING_MAIN",
                            operation="think_more_llm_call_fallback",
                            logger=logger,
                            task_id=facts.task_id,
                            outcome="think_more_timeout",
                        )
                    response = llm_response.content
                    append_usage_to_state(
                        interactive,
                        llm_response.usage,
                        "think_more_fallback",
                        request_mode="non_streaming",
                    )
                    maybe_structured = getattr(llm_response, "structured_output", None)
                    if isinstance(maybe_structured, Mapping):
                        structured_payload = maybe_structured
            else:
                dr_iteration = None
                if (facts.capability or "").lower() == "deep_reasoning":
                    _, _, dr_iteration = derive_dr_stream_identifiers(
                        interactive,
                        config,
                        advance_iteration=True,
                    )
                # Non-streaming: use chat_with_usage (Phase 7)
                async with reasoning_section(
                    writer,
                    state=interactive,
                    step="reasoning_loop",
                    label="Refining reasoning and plan.",
                    config=config,
                    context=context,
                ):
                    llm_response = await wait_for_with_timeout(
                        llm_client.chat_with_usage(
                            system_prompt,
                            think_prompt,
                            temperature=0.4,  # Moderate temperature for reasoning
                            max_tokens=LIMITS.think_more,
                            reasoning_effort=reasoning_effort,
                            structured_output=THINK_MORE_STRUCTURED_OUTPUT,
                        ),
                        timeout_sec=LLM_TIMEOUT_REASONING_MAIN_SEC,
                        component="REASONING_MAIN",
                        operation="think_more_llm_call",
                        logger=logger,
                        task_id=facts.task_id,
                        outcome="think_more_timeout",
                    )
                response = llm_response.content
                append_usage_to_state(
                    interactive,
                    llm_response.usage,
                    "think_more",
                    request_mode="non_streaming",
                )
                maybe_structured = getattr(llm_response, "structured_output", None)
                if isinstance(maybe_structured, Mapping):
                    structured_payload = maybe_structured
                if dr_iteration is not None:
                    record_dr_reasoning_snippet(interactive, dr_iteration, response)
            
            # Parse reasoning response
            parsed_payload = _extract_thinking_payload(
                response=response,
                structured_payload=structured_payload,
            )
            success = _apply_thinking_payload(
                parsed_payload=parsed_payload,
                response=response,
                interactive=interactive,
            )
            
            if not success:
                logger.warning("Failed to parse thinking response, using fallback")
                _apply_fallback_thinking(interactive)
            _record_think_more_phase_memory(
                interactive,
                turn_sequence=turn_sequence if isinstance(turn_sequence, int) else None,
                parsed_payload=parsed_payload,
                used_fallback=not success,
            )
                
        except LLMRefusalError:
            raise
        except Exception as exc:
            logger.error(f"Think more LLM call failed: {exc}")
            _apply_fallback_thinking(interactive)
            _record_think_more_phase_memory(
                interactive,
                turn_sequence=turn_sequence if isinstance(turn_sequence, int) else None,
                parsed_payload=None,
                used_fallback=True,
            )
    
    # Decrement iteration budget
    budget_update = decrement_iteration_budget(state)
    facts_update = budget_update.get("facts", {})
    
    # Merge budget updates
    facts.iterations = facts_update.get("iterations", facts.iterations)
    if "runtime_budgets" in facts_update:
        facts.metadata["runtime_budgets"] = facts_update["runtime_budgets"]
    
    logger.info(f"Think more node completed for task {facts.task_id}, iteration {facts.iterations}")
    
    return interactive.as_graph_update()


def _parse_thinking_response(
    response: str,
    interactive: InteractiveState,
    structured_payload: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Parse LLM thinking response and update state."""
    parsed_payload = _extract_thinking_payload(
        response=response,
        structured_payload=structured_payload,
    )
    return _apply_thinking_payload(
        parsed_payload=parsed_payload,
        response=response,
        interactive=interactive,
    )


def _extract_thinking_payload(
    *,
    response: str,
    structured_payload: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Extract think-more structured payload from explicit or textual response."""
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


def _apply_thinking_payload(
    *,
    parsed_payload: Optional[Dict[str, Any]],
    response: str,
    interactive: InteractiveState,
) -> bool:
    """Apply parsed think-more payload to state."""
    try:
        if isinstance(parsed_payload, dict):
            # Extract fields
            reasoning = parsed_payload.get("reasoning", "")
            updated_plan = parsed_payload.get("updated_plan", [])
            next_goal = parsed_payload.get("next_goal", "")
            key_observations = parsed_payload.get("key_observations", [])
            
            # Update plan if provided and non-empty (with validation and merge)
            if updated_plan and len(updated_plan) > 0:
                old_plan = interactive.facts.plan or []
                
                # Validate new plan quality
                new_quality = validate_plan_quality(updated_plan)
                
                # Check if update should be rejected
                if should_reject_plan_update(old_plan, updated_plan):
                    logger.warning(
                        "[CACHE] Rejected degraded plan update from think_more: "
                        f"new plan specificity {new_quality['specificity_score']:.2f} is too low"
                    )
                    _safe_inc("cache_invalidation_degradation")
                    # Keep existing plan, don't update
                    plan_entry = f"Plan update rejected (quality too low), keeping existing {len(old_plan)} steps"
                else:
                    # Merge plans if old plan exists, otherwise use new plan
                    if old_plan:
                        merged_plan = merge_plans(old_plan, updated_plan)
                        interactive.facts.plan = merged_plan
                        metadata = interactive.facts.ensure_metadata()
                        plan_version = metadata.get("plan_version") or 1
                        metadata["plan_version"] = plan_version + 1
                        interactive.facts.metadata = metadata
                        interactive.facts.todo_list = build_todos_from_plan(merged_plan)
                        plan_entry = f"Plan merged: {len(old_plan)} old + {len(updated_plan)} new -> {len(merged_plan)} steps"
                        logger.info("[CACHE] Merged plan update from think_more")
                    else:
                        interactive.facts.plan = updated_plan
                        metadata = interactive.facts.ensure_metadata()
                        plan_version = metadata.get("plan_version") or 1
                        metadata["plan_version"] = plan_version + 1
                        interactive.facts.metadata = metadata
                        interactive.facts.todo_list = build_todos_from_plan(updated_plan)
                        plan_entry = f"Plan updated: {len(updated_plan)} steps"
                    
                    interactive.trace.reasoning.append(plan_entry)
            
            # Update goal if provided
            if next_goal:
                interactive.facts.current_goal = next_goal
                goal_entry = f"Next goal: {next_goal}"
                interactive.trace.reasoning.append(goal_entry)
            
            # Add key observations to trace
            if key_observations:
                for obs in key_observations:
                    if obs and obs not in interactive.trace.observations:
                        interactive.trace.observations.append(obs)
            
            # Add reasoning summary to trace
            reasoning_summary = reasoning[:200] + "..." if len(reasoning) > 200 else reasoning
            thinking_entry = f"Thinking: {reasoning_summary}" if reasoning_summary else "Thinking: (no details)"
            interactive.trace.reasoning.append(thinking_entry)
            
            return True
            
    except (json.JSONDecodeError, KeyError, AttributeError) as exc:
        logger.warning(f"Failed to parse thinking response: {exc}")
        # Fallback to a bounded reasoning trace summary.
        if response and len(response) > 10:
            interactive.trace.reasoning.append(f"Thinking (text): {response[:150]}...")
            return True
    
    return False


def _record_think_more_phase_memory(
    interactive: InteractiveState,
    *,
    turn_sequence: Optional[int],
    parsed_payload: Optional[Mapping[str, Any]],
    used_fallback: bool,
) -> None:
    """Write think-more section snapshots for PTR continuity."""
    if not isinstance(turn_sequence, int):
        return

    facts = interactive.facts
    reasoning = ""
    next_goal = ""
    key_observations: list[str] = []
    updated_plan = []

    if isinstance(parsed_payload, Mapping):
        reasoning = str(parsed_payload.get("reasoning") or "").strip()
        next_goal = str(parsed_payload.get("next_goal") or "").strip()
        raw_observations = parsed_payload.get("key_observations")
        if isinstance(raw_observations, list):
            key_observations = [str(item).strip() for item in raw_observations if str(item).strip()]
        raw_plan = parsed_payload.get("updated_plan")
        if isinstance(raw_plan, list):
            updated_plan = [str(item).strip() for item in raw_plan if str(item).strip()]

    summary = reasoning or (interactive.trace.reasoning[-1] if interactive.trace.reasoning else "")
    if len(summary) > 280:
        summary = summary[:280] + "..."

    overview_lines = [f"status: {'fallback' if used_fallback else 'completed'}"]
    if updated_plan:
        overview_lines.append(f"updated_plan_steps: {len(updated_plan)}")

    sections: list[Dict[str, str]] = [
        {"heading": "Think More", "body": "\n".join(overview_lines)},
        {
            "heading": "Reasoning",
            "body": summary or "think_more reasoning step completed",
        },
    ]
    if key_observations:
        sections.append(
            {
                "heading": "Key Observations",
                "body": "\n".join(f"- {item}" for item in key_observations[:5]),
            }
        )
    if next_goal:
        sections.append({"heading": "Next Goal", "body": next_goal})
    if updated_plan:
        sections.append(
            {
                "heading": "Updated Plan",
                "body": "\n".join(
                    f"{index}. {step}" for index, step in enumerate(updated_plan[:6], start=1)
                ),
            }
        )

    payload: Dict[str, Any] = {"sections": sections}

    _iteration_memory.append(
        facts.ensure_metadata(),
        turn_sequence=turn_sequence,
        source="think_more",
        payload=payload,
    )


def _apply_fallback_thinking(interactive: InteractiveState) -> None:
    """Apply simple fallback reasoning when LLM unavailable."""
    facts = interactive.facts
    trace = interactive.trace
    
    # Create simple reasoning based on state
    executed_tools = trace.executed_tools or []
    reasoning = ""
    
    if executed_tools:
        last_tool = executed_tools[-1]
        if isinstance(last_tool, dict):
            tool_id = last_tool.get("tool_id", "unknown")
            obs = last_tool.get("observation", "")
        else:
            tool_id = getattr(last_tool, "tool_id", "unknown")
            obs = getattr(last_tool, "observation", "")

        reasoning = f"Executed {tool_id}. Observed: {str(obs)[:200]}"

        # Check if we should continue or finalize
        if len(executed_tools) >= 2:
            reasoning += "\n\nMultiple tools executed. Should analyze results and decide if more information needed."
        else:
            reasoning += "\n\nNeed to analyze results and determine next steps."
    else:
        reasoning = "No tools executed yet. Should identify first action from plan."
        
        if facts.todo_list:
            next_todo = facts.todo_list[0]
            description = next_todo.description if hasattr(next_todo, "description") else str(next_todo)
            reasoning += f"\n\nNext todo: {description}"
    
    fallback_entry = "Thinking (fallback): " + reasoning[:150]
    trace.reasoning.append(fallback_entry)
    
    # Set simple goal if none exists
    if not facts.current_goal and facts.plan:
        facts.current_goal = facts.plan[0] if facts.plan else "Continue with task"


__all__ = ["think_more_node"]
