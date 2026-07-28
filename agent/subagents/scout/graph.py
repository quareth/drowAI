"""LangGraph builder for the Scout recon child graph.

This module wires Scout-specific initialization and action selection around
the existing working-memory, memory-retrieval, approval, dispatch, compact
synthesis, post-tool reasoning, router, think-more, and reflection nodes.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from agent.graph.builders.common_edges import (
    with_interactive_state,
    wrap_with_context,
    wrap_with_context_async,
)
from agent.graph.nodes.decision_router import decision_router
from agent.graph.nodes.finalize import finalize_results
from agent.graph.nodes.memory_retrieval import memory_retrieval_node
from agent.graph.nodes.post_tool_reasoning.node import post_tool_reasoning
from agent.graph.nodes.reflect import reflect_node
from agent.graph.nodes.think_more import think_more_node
from agent.graph.nodes.tool_synthesizer import synthesize_tool_output
from agent.graph.nodes.working_memory import update_working_memory_node
from agent.graph.infrastructure.graph_registry import (
    GraphRegistry,
    get_default_graph_registry,
    get_or_register_compiled_graph,
)
from agent.graph.persistence import get_default_checkpointer
from agent.graph.state import InteractiveState
from agent.graph.subgraphs.tool_execution import (
    approval_gate_node,
    dispatch_tool_execution_node,
)
from agent.subagents.scout.nodes.choose_action import (
    SCOUT_ACTION_METADATA_KEY,
    choose_scout_action,
)
from agent.subagents.scout.nodes.complete import complete_scout_result
from agent.subagents.scout.nodes.initialize import initialize_scout_state

logger = logging.getLogger(__name__)

GRAPH_NAME_SCOUT_RECON = "scout_recon"


def _route_after_choose_action(interactive: InteractiveState) -> str:
    """Route Scout's selected native call to execution or completion."""

    action = interactive.facts.safe_metadata.get(SCOUT_ACTION_METADATA_KEY)
    route = action.get("route") if isinstance(action, dict) else None
    if route == "tool":
        return "approval_gate"
    if route == "complete":
        return "complete"
    raise ValueError("Scout choose_action did not write a valid route")


def _route_after_router(interactive: InteractiveState) -> str:
    """Route existing router outcomes into Scout graph destinations."""

    outcome = interactive.facts.safe_metadata.get("router_outcome")
    action = ""
    if isinstance(outcome, dict):
        action = str(outcome.get("action") or "").strip().lower()

    if action == "call_tool":
        return "choose_action"
    if action == "think_more":
        return "think_more"
    if action == "reflect":
        return "reflect"
    if action == "finalize":
        return "complete"

    logger.warning(
        "[SCOUT_GRAPH] Unknown router_outcome.action '%s'; completing Scout run",
        action,
    )
    return "complete"


def build_scout_recon_graph(
    *,
    checkpointer: Any | None = None,
    build_only: bool = False,
) -> Any:
    """Build the Scout recon graph, optionally returning it uncompiled."""

    graph = StateGraph(dict)

    graph.add_node(
        "initialize",
        wrap_with_context(initialize_scout_state),
    )
    graph.add_node(
        "update_working_memory",
        wrap_with_context(update_working_memory_node),
    )
    graph.add_node(
        "memory_retrieval",
        wrap_with_context_async(memory_retrieval_node),
    )
    graph.add_node(
        "choose_action",
        wrap_with_context_async(choose_scout_action),
    )
    graph.add_node(
        "approval_gate",
        wrap_with_context_async(approval_gate_node),
    )
    graph.add_node(
        "dispatch_tool",
        wrap_with_context_async(dispatch_tool_execution_node),
    )
    graph.add_node(
        "tool_synthesizer",
        wrap_with_context_async(synthesize_tool_output),
    )
    graph.add_node(
        "post_tool_reasoning",
        wrap_with_context_async(post_tool_reasoning),
    )
    graph.add_node(
        "decision_router",
        wrap_with_context_async(decision_router),
    )
    graph.add_node(
        "think_more",
        wrap_with_context_async(think_more_node),
    )
    graph.add_node(
        "reflect",
        wrap_with_context_async(reflect_node),
    )
    graph.add_node(
        "finalize",
        wrap_with_context_async(finalize_results),
    )
    graph.add_node(
        "complete",
        wrap_with_context(complete_scout_result),
    )

    graph.set_entry_point("initialize")
    graph.add_edge("initialize", "update_working_memory")
    graph.add_edge("update_working_memory", "memory_retrieval")
    graph.add_edge("memory_retrieval", "choose_action")
    graph.add_conditional_edges(
        "choose_action",
        with_interactive_state(_route_after_choose_action),
        {
            "approval_gate": "approval_gate",
            "complete": "finalize",
        },
    )
    graph.add_edge("approval_gate", "dispatch_tool")
    graph.add_edge("dispatch_tool", "tool_synthesizer")
    graph.add_edge("tool_synthesizer", "post_tool_reasoning")
    graph.add_edge("post_tool_reasoning", "decision_router")
    graph.add_conditional_edges(
        "decision_router",
        with_interactive_state(_route_after_router),
        {
            "choose_action": "choose_action",
            "think_more": "think_more",
            "reflect": "reflect",
            "complete": "finalize",
        },
    )
    graph.add_edge("think_more", "post_tool_reasoning")
    graph.add_edge("reflect", "decision_router")
    graph.add_edge("finalize", "complete")
    graph.add_edge("complete", END)

    if build_only:
        return graph
    return graph.compile(checkpointer=checkpointer or get_default_checkpointer())


def get_compiled_scout_recon_graph(
    *,
    registry: GraphRegistry | None = None,
) -> object:
    """Return the compiled Scout recon graph from the shared registry."""
    return get_or_register_compiled_graph(
        registry=registry or get_default_graph_registry(),
        name=GRAPH_NAME_SCOUT_RECON,
        build_uncompiled=lambda: build_scout_recon_graph(build_only=True),
        checkpointer_factory=get_default_checkpointer,
    )


__all__ = [
    "GRAPH_NAME_SCOUT_RECON",
    "build_scout_recon_graph",
    "get_compiled_scout_recon_graph",
]
