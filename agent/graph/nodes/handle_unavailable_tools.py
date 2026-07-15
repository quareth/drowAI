"""Graceful degradation node for handling unavailable tools.

This node handles cases where the desired capability lacks available tools,
providing fallback options or finalizing with current findings instead of looping.
"""

from __future__ import annotations

import logging
from typing import Mapping, Optional

from backend.services.metrics.utils import safe_inc

from ..infrastructure.state_models import CapabilityType, GraphRuntimeContext
from ..state import InteractiveState
from ..utils.termination_guardrails import are_scope_goals_achieved
from ..utils.tool_availability import (
    are_tools_available,
    get_fallback_capability,
)

logger = logging.getLogger(__name__)


async def handle_unavailable_tools_node(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
) -> dict:
    """Handle cases where desired capability lacks available tools.
    
    Decision flow:
    1. Check if current findings satisfy user scope
    2. If yes → finalize with findings
    3. If no → check for fallback tools
    4. If fallback available → update plan with fallback
    5. If no fallback → finalize with limitations note
    
    Args:
        state: Current graph state
        context: Runtime context
    
    Returns:
        State update dict with routing decision in decision_history
    """
    interactive = InteractiveState.from_mapping(state)
    facts = interactive.facts
    trace = interactive.trace
    metadata = facts.metadata if isinstance(facts.metadata, dict) else {}
    facts.metadata = metadata
    decision_history = facts.ensure_decision_history()
    
    # Get current capability
    capability_str = facts.capability or ""
    
    # Normalize to CapabilityType
    if not CapabilityType:
        logger.warning("[DEGRADATION] CapabilityType not available, cannot handle degradation")
        # Record decision to finalize
        decision_history.append("finalize: CapabilityType unavailable")
        return interactive.as_graph_update()
    
    try:
        desired_capability = CapabilityType.from_intent(capability_str)
    except Exception as exc:
        logger.warning(
            f"[DEGRADATION] Failed to normalize capability '{capability_str}': {exc}"
        )
        # Record decision to finalize
        decision_history.append(
            f"finalize: Failed to normalize capability '{capability_str}'"
        )
        return interactive.as_graph_update()
    
    logger.info(
        f"[DEGRADATION] Handling unavailable tools for {desired_capability.value}"
    )
    
    # Check if current findings satisfy scope
    if are_scope_goals_achieved(interactive):
        logger.info(
            "[DEGRADATION] Finalizing: scope satisfied with current findings"
        )
        safe_inc("degradation_finalize_scope_satisfied")
        
        # Add note about tool unavailability
        tool_gaps = metadata.get("tool_gaps", [])
        tool_gaps.append(
            f"{desired_capability.value} was requested but no tools available"
        )
        metadata["tool_gaps"] = tool_gaps
        
        # Record decision to finalize
        decision_history.append(
            "finalize: Scope satisfied despite missing tools"
        )
        trace.reasoning.append(
            f"[DEGRADATION] Finalizing: scope goals achieved even though "
            f"no tools available for {desired_capability.value}"
        )
        return interactive.as_graph_update()
    
    # Check for fallback capability
    fallback = get_fallback_capability(desired_capability)
    if fallback and are_tools_available(fallback):
        logger.info(
            f"[DEGRADATION] Falling back: {desired_capability.value} → {fallback.value}"
        )
        safe_inc("degradation_fallback_success")
        
        # Update state with fallback capability
        facts.capability = fallback.value
        
        # Track capability fallbacks
        capability_fallbacks = metadata.get("capability_fallbacks", [])
        capability_fallbacks.append(f"{desired_capability.value} → {fallback.value}")
        metadata["capability_fallbacks"] = capability_fallbacks
        
        # Invalidate cached plan to force replanning with fallback
        from ..utils.cache_invalidation import invalidate_plan
        invalidate_plan(interactive, reason=f"Capability fallback: {desired_capability.value} → {fallback.value}")
        
        # Record decision to replan
        decision_history.append(
            f"planner: Fallback to {fallback.value} capability"
        )
        trace.reasoning.append(
            f"[DEGRADATION] Falling back from {desired_capability.value} to {fallback.value} "
            "and replanning"
        )
        return interactive.as_graph_update()
    
    # No fallback available, finalize with what we have
    logger.warning(
        f"[DEGRADATION] Finalizing with limitations: "
        f"no tools or fallbacks for {desired_capability.value}"
    )
    safe_inc("degradation_finalize_no_fallback")
    
    # Track tool gaps and limitations
    tool_gaps = metadata.get("tool_gaps", [])
    tool_gaps.append(
        f"{desired_capability.value} was requested but no tools or fallbacks available"
    )
    metadata["tool_gaps"] = tool_gaps
    
    limitations = metadata.get("limitations", [])
    limitations.append(
        f"Unable to perform {desired_capability.value} due to missing tools"
    )
    metadata["limitations"] = limitations
    
    # Record decision to finalize
    decision_history.append(
        f"finalize: No tools or fallbacks for {desired_capability.value}"
    )
    trace.reasoning.append(
        f"[DEGRADATION] Finalizing with limitations: requested {desired_capability.value} "
        "but no tools or fallbacks available"
    )
    
    return interactive.as_graph_update()


__all__ = ["handle_unavailable_tools_node"]
