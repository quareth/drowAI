"""Minimal parent graph for finalizing a completed subagent handoff.

This builder only rewires existing finalization nodes. The child result is
already present in the parent context bundle; this graph lets the canonical
main finalizer consume it, stream the user-facing answer, and run the existing
turn suffixer without executing another tool or adding an articulation layer.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from agent.graph.nodes.finalize import finalize_results
from agent.graph.nodes.finalizer import finalize_turn

from .common_edges import wrap_with_context, wrap_with_context_async


def build_parent_handoff_graph() -> Any:
    """Compile the existing main finalizer path for one child handoff."""
    graph = StateGraph(dict)
    graph.add_node("format_results", wrap_with_context_async(finalize_results))
    graph.add_node("finalize", wrap_with_context(finalize_turn))
    graph.set_entry_point("format_results")
    graph.add_edge("format_results", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


__all__ = ["build_parent_handoff_graph"]
