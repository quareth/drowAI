"""Classification node used to determine the next branch."""

from __future__ import annotations

import logging
from typing import Mapping, Optional

from ..infrastructure.state_models import GraphRuntimeContext
from ..routers.intent_router import choose_capability
from ..state import InteractiveState

logger = logging.getLogger(__name__)

DEFAULT_CAPABILITY = "respond_only"


def _format_decision_log(capability: str, decisions: dict, context: Optional[GraphRuntimeContext]) -> str:
    considered = ", ".join(decisions.get("considered", [])) or capability
    parts = [f"Intent router selected {capability} (considered: {considered})."]
    if context is not None:
        mode = context.feature_flags.get("mode", "unset") if context.feature_flags else "unset"
        parts.append(f"Mode hint={mode}.")
    return " ".join(parts)


def classify_turn(
    state: Mapping[str, object] | InteractiveState,
    context: Optional[GraphRuntimeContext] = None,
) -> dict:
    """Determine the capability for the current turn based on intent signals."""

    interactive = InteractiveState.from_mapping(state)
    capability, decisions = choose_capability(interactive)

    # Keep routing labels as-is (deep_reasoning, simple_tool_execution, respond_only)
    # CapabilityType enum is for task-specific capabilities, not routing
    capability_str = capability or DEFAULT_CAPABILITY

    interactive.facts.capability = capability_str
    interactive.facts.metadata.setdefault("intent_router", {})
    interactive.facts.metadata["intent_router"].update(
        {
            "chosen_capability": interactive.facts.capability,
            "decisions": decisions,
        }
    )

    interactive.trace.reasoning.append(_format_decision_log(interactive.facts.capability, decisions, context))
    return interactive.as_graph_update()


__all__ = ["classify_turn"]
