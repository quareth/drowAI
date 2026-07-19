"""Synthesis node for graceful finalization when agent encounters reasoning loops."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping, Optional

from ..emission.reasoning_section import reasoning_section
from ..infrastructure.state_models import GraphRuntimeContext
from ..memory.findings import build_relevant_findings_for_prompt
from ..state import InteractiveState
from ..utils import iteration_memory as _iteration_memory
from ..utils.environment_loader import get_environment_full
from ..utils.event_identity import resolve_turn_sequence
from .node_utils import append_usage_to_state
from ..utils.llm_resolver import (
    ROLE_REASONING_MAIN,
    get_llm_reasoning_effort,
    has_llm_runtime_services,
    resolve_llm_client,
)
from ..config.token_limits import LIMITS
from agent.providers.llm.core.exceptions import (
    LLMConfigurationError,
    LLMProviderError,
    LLMRefusalError,
)
from agent.tools.capability_surface import render_capability_surface
from core.llm import LLM_TIMEOUT_REASONING_MAIN_SEC, wait_for_with_timeout
from core.prompts.builders.synthesis import build_synthesis_prompt
from core.prompts.constants import SYNTHESIS_SYSTEM_PROMPT

if TYPE_CHECKING:
    from langgraph.types import StreamWriter

logger = logging.getLogger(__name__)


async def synthesis_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
    config: Optional[Mapping[str, Any]] = None,
    writer: Optional["StreamWriter"] = None,
) -> dict:
    """Synthesize findings and provide graceful completion when stuck in reasoning loop.
    
    This node handles cases where the agent detects it's repeating the same
    analysis without making progress. Instead of failing abruptly, it:
    
    1. Acknowledges the situation to the user (transparency)
    2. Summarizes what was attempted and discovered
    3. Synthesizes partial findings despite incomplete execution
    4. Suggests alternative approaches the user might try
    5. Provides value even though the task wasn't fully completed
    
    Args:
        state: Current graph state
        context: Runtime context
    
    Returns:
        State update dict with final_text set
    """
    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    trace = interactive.trace

    # Build synthesis prompt
    prompt = _build_synthesis_prompt(interactive, context)
    system_prompt = _build_system_prompt()
    
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
        # Fallback without LLM
        logger.warning("No LLMClient for synthesis, using fallback")
        final_text = _build_fallback_message(interactive)
        llm_client = None
    
    if llm_client is not None:
        try:
            # Use chat_with_usage to capture tokens (Phase 7)
            async with reasoning_section(
                writer,
                state=interactive,
                step="synthesis",
                label="Synthesizing findings.",
                config=config,
                context=context,
            ):
                llm_response = await wait_for_with_timeout(
                    llm_client.chat_with_usage(
                        system_prompt,
                        prompt,
                        temperature=0.7,  # Higher temp for natural, conversational language
                        max_tokens=LIMITS.synthesis,
                        reasoning_effort=reasoning_effort,
                    ),
                    timeout_sec=LLM_TIMEOUT_REASONING_MAIN_SEC,
                    component="REASONING_MAIN",
                    operation="synthesis_llm_call",
                    logger=logger,
                    task_id=facts.task_id,
                    outcome="synthesis_timeout",
                )
            final_text = llm_response.content
            append_usage_to_state(
                interactive,
                llm_response.usage,
                "synthesis",
                request_mode="non_streaming",
            )
            
        except LLMRefusalError:
            raise
        except LLMProviderError:
            raise
        except Exception as exc:
            logger.error(f"Synthesis LLM call failed: {exc}")
            final_text = _build_fallback_message(interactive)
    
    # Update trace with final text
    trace.final_text = final_text
    trace.reasoning.append("Reasoning loop detected - synthesizing findings")
    
    logger.info(f"Synthesis completed for task {facts.task_id}")
    
    return interactive.as_graph_update()


def _build_system_prompt() -> str:
    """Build system prompt for synthesis."""
    return SYNTHESIS_SYSTEM_PROMPT


def _build_synthesis_prompt(
    interactive: InteractiveState,
    context: Optional[GraphRuntimeContext],
) -> str:
    """Build prompt for synthesis from canonical state projections.

    Phase 1 of the synthesize-shared-context plan moved
    ``build_synthesis_prompt`` to ``core.prompts.builders.synthesis`` and
    rebuilt it from the same canonical projections ``think_more``
    consumes plus a synthesis-only ``## Loop Details`` block. This node-
    side helper mirrors ``think_more_node`` (``agent/graph/nodes/
    think_more.py:62-79``): it computes the keyword-only context kwargs
    (turn / phase identifiers, relevant findings, environment context,
    loop-diagnosis counters) and forwards them to the builder.
    """

    facts = interactive.facts
    metadata = facts.safe_metadata

    # Compute canonical prompt-context kwargs (mirroring think_more_node).
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

    # Synthesis-specific loop-diagnosis counters; both gate the
    # ``## Loop Details`` section in the canonical builder.
    decision_history = facts.safe_decision_history
    reflection_count = sum(1 for d in decision_history if d.startswith("reflect:"))
    iterations = facts.iterations

    return build_synthesis_prompt(
        interactive.as_graph_state(),
        turn_sequence=turn_sequence,
        current_phase_sequence=current_phase_sequence,
        latest_recorded_phase_sequence=latest_recorded_phase_sequence_value,
        relevant_findings=relevant_findings,
        capability_surface=capability_surface,
        environment_context=environment_context,
        reflection_count=reflection_count,
        iterations=iterations,
    )


def _build_fallback_message(interactive: InteractiveState) -> str:
    """Build fallback message when LLM unavailable."""
    facts = interactive.facts
    trace = interactive.trace
    
    # Count loop details
    decision_history = facts.safe_decision_history
    reflection_count = sum(1 for d in decision_history if d.startswith("reflect:"))
    observations = trace.observations or []
    executed_tools = trace.executed_tools or []
    
    # Build message
    message_parts = [
        "I apologize, but I encountered a reasoning loop while processing your request.",
        "",
        f"**What I was trying to do**: {facts.message}",
        "",
        f"**What happened**: After {facts.iterations} iterations and {reflection_count} reflection cycles, "
        "I found myself repeating the same analysis without making progress.",
    ]
    
    # Add partial findings if any
    if observations:
        message_parts.extend([
            "",
            "**What I discovered**:",
        ])
        for obs in observations[-3:]:  # Last 3 observations
            message_parts.append(f"- {obs}")
    
    # Add tool attempts if any
    if executed_tools:
        # Handle both dict and ToolExecutionRecord objects
        tool_ids = []
        for t in executed_tools:
            if isinstance(t, dict):
                tool_ids.append(t.get("tool_id", "unknown"))
            else:
                # ToolExecutionRecord object
                tool_ids.append(getattr(t, "tool_id", "unknown"))
        
        unique_tools = list(dict.fromkeys(tool_ids))  # Preserve order, remove duplicates
        message_parts.extend([
            "",
            f"**Tools I attempted**: {', '.join(unique_tools)}",
        ])
    
    # Add possible reasons
    message_parts.extend([
        "",
        "**Possible reasons**:",
        "- The task requires capabilities I don't currently have",
        "- There was an infrastructure issue preventing tool execution",
        "- The problem scope needs to be refined or broken into smaller tasks",
    ])
    
    # Add suggestions
    message_parts.extend([
        "",
        "**Suggestions**:",
        "- Try breaking your request into smaller, more specific tasks",
        "- Verify the execution environment is properly configured",
        "- Consider using different tools or approaches",
    ])
    
    return "\n".join(message_parts)


__all__ = ["synthesis_node"]
