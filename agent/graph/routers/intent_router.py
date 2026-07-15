"""Intent routing helpers that map state signals to LangGraph branches."""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from ..guards import capability_in, has_candidate_tools, has_eligible_routes, route_eligible
from ..infrastructure.state_models import CapabilityType
from ..state import InteractiveState

logger = logging.getLogger(__name__)

DEFAULT_CAPABILITY = "respond_only"

# Routing labels - these control graph flow, NOT tool selection
ROUTING_LABELS = {
    "respond_only",
    "respond",
    "simple_chat",
    "direct_executor",
    "plan_executor",
    "tool_call",
    "simple_tool_execution",
    "deep_reasoning",
}


def _normalize_capability(signal: str | None) -> str:
    """Normalize capability signal to routing capability or CapabilityType.
    
    This function handles both routing capabilities (like "simple_tool_execution", "deep_reasoning")
    and maps classifier labels to canonical CapabilityType values when appropriate.
    """
    if not signal:
        return DEFAULT_CAPABILITY
    
    signal_lower = signal.lower()
    
    # Map routing capabilities (keep as-is for routing decisions)
    routing_mapping = {
        "simple_chat": DEFAULT_CAPABILITY,
        "respond_only": DEFAULT_CAPABILITY,
        "direct_executor": "simple_tool_execution",
        "tool_call": "simple_tool_execution",
        "simple_tool": "simple_tool_execution",
        "plan_executor": "deep_reasoning",
        "multi_step_execution": "deep_reasoning",
        "deep_reasoning": "deep_reasoning",
    }
    
    if signal_lower in routing_mapping:
        return routing_mapping[signal_lower]
    
    # Try to normalize to CapabilityType for deep reasoning capabilities
    try:
        normalized = CapabilityType.from_intent(signal)
        logger.debug(
            f"[CAPABILITY] Normalized '{signal}' to CapabilityType '{normalized.value}'"
        )
        # For deep reasoning flows, return the enum value
        # For routing, we may still need "simple_tool_execution" or "deep_reasoning"
        return normalized.value
    except Exception:
        # If normalization fails, return as-is
        return signal


def _eligible(capability: str, state: InteractiveState) -> bool:
    """Check if a capability is eligible based on state.
    
    For deep_reasoning: If classifier explicitly requested it, trust it.
    Don't require it to be in eligible_routes if classifier confidence is high.
    """
    capability_lower = capability.lower()
    
    if capability_lower == DEFAULT_CAPABILITY:
        return True
    if capability_lower == "simple_tool_execution":
        normalized_eligible_routes = {
            _normalize_capability(str(route)).lower()
            for route in (state.facts.eligible_routes or [])
        }
        return (
            has_candidate_tools(state.facts)
            or capability_in(state.facts, ["simple_tool_execution"])
            or route_eligible(state.facts, "simple_tool_execution")
            or "simple_tool_execution" in normalized_eligible_routes
        )
    if capability_lower == "deep_reasoning":
        # Trust classifier if it explicitly chose deep_reasoning
        hints = state.facts.intent_hints or {}
        classifier_label = _normalize_capability(str(hints.get("classifier_label", ""))).lower()
        classifier_confidence = hints.get("classifier_confidence", 0.0)
        normalized_eligible_routes = {
            _normalize_capability(str(route)).lower()
            for route in (state.facts.eligible_routes or [])
        }
        
        # If classifier said deep_reasoning with high confidence, allow it
        if classifier_label == "deep_reasoning" and classifier_confidence >= 0.7:
            return True
        
        # Otherwise check eligible routes as before
        return has_eligible_routes(state.facts) and "deep_reasoning" in normalized_eligible_routes
    return route_eligible(state.facts, capability)


def choose_capability(state: InteractiveState) -> Tuple[str, Dict[str, List[str]]]:
    """Return the chosen capability and decision metadata.

    Returns a capability string that should be normalized to CapabilityType enum
    in the classification node.
    """

    facts = state.facts
    hints = facts.intent_hints or {}
    classifier_label = hints.get("classifier_label")
    heuristic_hints = hints.get("tool_hints") or []
    eligible_routes = facts.eligible_routes or []
    normalized_eligible_routes = {
        _normalize_capability(str(route)).lower() for route in eligible_routes
    }
    metadata = facts.safe_metadata
    hint_capability = metadata.get("intent_capability")
    hint_candidates = metadata.get("intent_capability_candidates") or []

    decisions: Dict[str, List[str]] = {"considered": []}

    # Phase 4 Task 4.2: honor the deep-reasoning graph-entry override
    # before any classifier/heuristic candidate is considered. The
    # override is derived once by the facade (see
    # ``backend/services/langgraph_chat/facade_helpers.build_metadata``)
    # from the durable user-surface ``execution_route_policy``. Without
    # this short-circuit the deep-reasoning graph can self-route to
    # ``respond_only`` / ``fallback_finalize`` at graph entry even when
    # the facade selected ``DeepReasoningHandler`` for a Plan turn.
    #
    # Bypass the eligibility check on purpose: the override exists
    # exactly because eligibility derived from heuristic / classifier
    # signals can disagree with the user-surface tier. Eligibility
    # gating here would silently downgrade Plan to ``respond_only``,
    # re-introducing the bug Phase 4 is meant to close.
    graph_entry_override = metadata.get("intent_router_graph_entry_override")
    if isinstance(graph_entry_override, str) and graph_entry_override.strip():
        normalized_override = _normalize_capability(graph_entry_override.strip())
        decisions["considered"].append(normalized_override)
        decisions["graph_entry_override"] = [normalized_override]
        if normalized_override in ROUTING_LABELS:
            facts.capability = normalized_override
        else:
            try:
                normalized_enum = CapabilityType.from_intent(normalized_override)
                facts.capability = normalized_enum.value
                normalized_override = normalized_enum.value
            except Exception:
                facts.capability = normalized_override
        logger.info(
            "[ROUTING] Applied graph-entry override capability '%s'",
            normalized_override,
        )
        return normalized_override, decisions

    candidates: List[str] = []

    # Prefer canonical capability from intent classifier if available
    if hint_capability:
        hint_cap_str = _normalize_capability(str(hint_capability))
        # Skip normalization for routing labels
        if hint_cap_str in ROUTING_LABELS:
            candidates.append(hint_cap_str)
            logger.debug(f"[ROUTING] Using hint routing label: {hint_cap_str}")
        else:
            # Try to normalize to CapabilityType
            try:
                normalized = CapabilityType.from_intent(hint_cap_str)
                candidates.append(normalized.value)
                logger.debug(
                    f"[CAPABILITY] Using normalized hint capability: {normalized.value}"
                )
            except Exception:
                candidates.append(hint_cap_str)
    
    for candidate in hint_candidates:
        if candidate not in candidates:
            candidate_str = _normalize_capability(str(candidate))
            # Skip normalization for routing labels
            if candidate_str in ROUTING_LABELS:
                candidates.append(candidate_str)
            else:
                # Try to normalize candidate
                try:
                    normalized = CapabilityType.from_intent(candidate_str)
                    if normalized.value not in candidates:
                        candidates.append(normalized.value)
                except Exception:
                    candidates.append(candidate_str)

    if classifier_label:
        normalized = _normalize_capability(str(classifier_label))
        candidates.append(normalized)
    if heuristic_hints:
        candidates.append("simple_tool_execution")
    if "deep_reasoning" in normalized_eligible_routes or hints.get("deep_reasoning_requested"):
        candidates.append("deep_reasoning")
    candidates.append(DEFAULT_CAPABILITY)

    for capability in candidates:
        decisions["considered"].append(capability)
        if _eligible(capability, state):
            # Check if this is a routing label (not a pentesting capability)
            if capability in ROUTING_LABELS:
                # Keep routing labels as-is - they control graph flow
                facts.capability = capability
                logger.debug(f"[ROUTING] Chose routing label '{capability}'")
                return capability, decisions
            
            # Otherwise, normalize to CapabilityType enum
            try:
                normalized = CapabilityType.from_intent(capability)
                facts.capability = normalized.value
                logger.info(
                    f"[CAPABILITY] Chose capability '{normalized.value}' (from '{capability}')"
                )
                return normalized.value, decisions
            except Exception:
                # Normalization failed - use as-is
                facts.capability = capability
                logger.debug(f"[CAPABILITY] Could not normalize '{capability}', using as-is")
                return capability, decisions

    # Fallback: use default capability (a routing label)
    if DEFAULT_CAPABILITY in ROUTING_LABELS:
        facts.capability = DEFAULT_CAPABILITY
        decisions.setdefault("fallback", []).append(DEFAULT_CAPABILITY)
        logger.debug(f"[ROUTING] Fallback to routing label '{DEFAULT_CAPABILITY}'")
        return DEFAULT_CAPABILITY, decisions
    
    # If default is not a routing label, try to normalize
    try:
        normalized = CapabilityType.from_intent(DEFAULT_CAPABILITY)
        facts.capability = normalized.value
        decisions.setdefault("fallback", []).append(normalized.value)
        return normalized.value, decisions
    except Exception:
        facts.capability = DEFAULT_CAPABILITY
        decisions.setdefault("fallback", []).append(DEFAULT_CAPABILITY)
        return DEFAULT_CAPABILITY, decisions


__all__ = ["choose_capability"]
