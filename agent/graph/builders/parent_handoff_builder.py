"""Parent continuation graph for completed subagent handoff batches.

This builder routes bounded child handoffs through the existing parent
post-action reasoning and decision-router path before any final response is
formatted. Backend-owned parent-control outcomes stop at the graph boundary so
the coordinator can launch follow-up subagents or wait for active runs.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from langgraph.graph import END, StateGraph

from backend.services.metrics.utils import safe_inc

from ..nodes.decision_router import decision_router
from ..nodes.finalize import finalize_results
from ..nodes.finalizer import finalize_turn
from ..nodes.post_tool_reasoning.models import (
    DIRECT_TOOL_OUTCOME_SOURCE,
    POST_ACTION_OUTCOME_SOURCE_METADATA_KEY,
    SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE,
)
from ..nodes.post_tool_reasoning.node import post_tool_reasoning
from ..nodes.post_tool_reasoning.core.retry_logic import RETRY_METADATA_KEY
from ..nodes.reflect import reflect_node
from ..nodes.select_tool_categories import select_tool_categories_node
from ..nodes.synthesis import synthesis_node
from ..nodes.think_more import think_more_node
from ..nodes.tool_articulation import articulate_tool_intent
from ..nodes.tool_synthesizer import synthesize_tool_output
from ..state import InteractiveState
from ..subgraphs.tool_execution import (
    approval_gate_node,
    dispatch_tool_execution_node,
    prepare_tool_execution_plan,
)
from agent.reasoning.tool_selection_sentinel import (
    metadata_has_unavailable_capability,
)
from .common_edges import (
    build_router_action_map,
    require_conditional_edges,
    wrap_with_context,
)
from .post_action_wiring import (
    add_direct_tool_action_nodes,
    add_post_action_continuation_nodes,
    wire_direct_tool_action_path,
    wire_post_action_continuation,
)

logger = logging.getLogger(__name__)

_PARENT_ROUTER_ACTION_MAP = {
    **build_router_action_map(
        call_tool_target="select_tool_categories",
        finalize_target="format_results",
    ),
    "delegate_subagent": "delegate_subagent",
    "wait_for_subagents": "wait_for_subagents",
}


def _prepare_handoff_reasoning_context(
    state: Mapping[str, Any],
    context: Any = None,
) -> dict[str, Any]:
    """Mark the initial parent continuation outcome as a subagent handoff batch."""
    _ = context
    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.ensure_metadata()
    metadata[POST_ACTION_OUTCOME_SOURCE_METADATA_KEY] = (
        SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE
    )
    metadata.pop("synthesized_output", None)
    return interactive.as_graph_update()


def _prepare_direct_tool_reasoning_context(
    state: Mapping[str, Any],
    context: Any = None,
) -> dict[str, Any]:
    """Mark post-tool continuation outcomes as direct-tool PAR inputs."""
    _ = context
    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.ensure_metadata()
    metadata[POST_ACTION_OUTCOME_SOURCE_METADATA_KEY] = DIRECT_TOOL_OUTCOME_SOURCE
    return interactive.as_graph_update()


def _route_after_parent_router(interactive: InteractiveState) -> str:
    """Dispatch parent PAR router outcomes without executing backend controls."""
    metadata = interactive.facts.safe_metadata
    outcome = metadata.get("router_outcome") if isinstance(metadata, dict) else None
    action = ""
    if isinstance(outcome, dict):
        action = str(outcome.get("action") or "").strip().lower()

    target = _PARENT_ROUTER_ACTION_MAP.get(action)
    if target is None:
        logger.warning(
            "[PARENT_HANDOFF_ROUTE] Missing/unknown router_outcome.action '%s'; "
            "defaulting to format_results",
            action,
        )
        safe_inc("parent_handoff_router_action_unknown")
        return "format_results"

    safe_inc(f"parent_handoff_router_action_{action}")
    return target


def _route_after_prepare_tool_plan(interactive: InteractiveState) -> str:
    """Route prepared unavailable-capability state to PAR without execution."""
    if metadata_has_unavailable_capability(interactive.facts.safe_metadata):
        safe_inc("parent_handoff_prepare_tool_plan_unavailable_capability")
        return "post_tool_reasoning"

    metadata = interactive.facts.safe_metadata
    retry_data = metadata.get(RETRY_METADATA_KEY, {}) or {}
    retry_count = retry_data.get("count", 0)
    return "articulation" if retry_count == 0 else "approval_gate"


def build_parent_handoff_graph(
    *,
    checkpointer: Any = None,
    build_only: bool = False,
) -> Any:
    """Compile the parent continuation graph for one claimed child handoff batch."""
    graph = StateGraph(dict)
    conditional = require_conditional_edges(graph)

    graph.add_node(
        "prepare_handoff_context",
        wrap_with_context(_prepare_handoff_reasoning_context),
    )
    graph.add_node(
        "prepare_direct_tool_context",
        wrap_with_context(_prepare_direct_tool_reasoning_context),
    )
    add_direct_tool_action_nodes(
        graph,
        select_tool_categories_node_fn=select_tool_categories_node,
        articulate_tool_intent_fn=articulate_tool_intent,
        prepare_tool_execution_plan_fn=prepare_tool_execution_plan,
        approval_gate_node_fn=approval_gate_node,
        dispatch_tool_execution_node_fn=dispatch_tool_execution_node,
        synthesize_tool_output_fn=synthesize_tool_output,
    )
    add_post_action_continuation_nodes(
        graph,
        reasoning_node_name="post_action_reasoning",
        reasoning_node=post_tool_reasoning,
        decision_router_node=decision_router,
        think_more_node_fn=think_more_node,
        reflect_node_fn=reflect_node,
        synthesis_node_fn=synthesis_node,
        finalize_results_fn=finalize_results,
        finalize_turn_fn=finalize_turn,
    )

    graph.set_entry_point("prepare_handoff_context")
    graph.add_edge("prepare_handoff_context", "post_action_reasoning")
    wire_direct_tool_action_path(
        graph,
        route_after_prepare_tool_plan=_route_after_prepare_tool_plan,
        terminal_target="prepare_direct_tool_context",
        conditional=conditional,
    )
    graph.add_edge("prepare_direct_tool_context", "post_action_reasoning")
    wire_post_action_continuation(
        graph,
        route_after_router=_route_after_parent_router,
        router_targets={
            "select_tool_categories": "select_tool_categories",
            "think_more": "think_more",
            "reflect": "reflect",
            "synthesis": "synthesis",
            "format_results": "format_results",
            "delegate_subagent": END,
            "wait_for_subagents": END,
        },
        reasoning_node_name="post_action_reasoning",
        conditional=conditional,
    )
    if build_only:
        return graph
    return graph.compile(checkpointer=checkpointer)


__all__ = [
    "_PARENT_ROUTER_ACTION_MAP",
    "_route_after_parent_router",
    "build_parent_handoff_graph",
]
