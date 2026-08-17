"""Shared graph wiring for post-action continuation paths.

This module owns the reusable LangGraph node and edge registration shared by
the simple-tool graph and the parent-handoff continuation graph. Ordinary
tools retain the approval, dispatch, and synthesis path; only a running
execution enters the continuation-session boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from langgraph.graph import END

from ..nodes.decision_router import decision_router
from ..nodes.finalize import finalize_results
from ..nodes.finalizer import finalize_turn
from ..nodes.post_tool_reasoning.node import post_tool_reasoning
from ..nodes.reflect import reflect_node
from ..nodes.select_tool_categories import select_tool_categories_node
from ..nodes.synthesis import synthesis_node
from ..nodes.think_more import think_more_node
from ..nodes.tool_articulation import articulate_tool_intent
from ..nodes.terminal_session_compressor import (
    compress_terminal_execution_session_output,
)
from ..nodes.tool_synthesizer import synthesize_tool_output
from ..state import InteractiveState
from ..subgraphs.tool_execution import (
    approval_gate_node,
    dispatch_tool_execution_node,
    prepare_tool_execution_plan,
)
from ..subgraphs.tool_execution_session import (
    build_tool_execution_session_subgraph,
    route_after_tool_dispatch,
)
from .common_edges import (
    require_conditional_edges,
    with_interactive_state,
    wrap_with_context,
    wrap_with_context_async,
    WrapperLogCallback,
)


def add_direct_tool_action_nodes(
    graph: Any,
    *,
    on_wrap_log: WrapperLogCallback | None = None,
    select_tool_categories_node_fn: Callable[..., Any] = select_tool_categories_node,
    articulate_tool_intent_fn: Callable[..., Any] = articulate_tool_intent,
    prepare_tool_execution_plan_fn: Callable[..., Any] = prepare_tool_execution_plan,
    approval_gate_node_fn: Callable[..., Any] = approval_gate_node,
    dispatch_tool_execution_node_fn: Callable[..., Any] = (
        dispatch_tool_execution_node
    ),
    terminal_session_compressor_fn: Callable[..., Any] = (
        compress_terminal_execution_session_output
    ),
    synthesize_tool_output_fn: Callable[..., Any] = synthesize_tool_output,
) -> None:
    """Register existing direct-tool selection, HITL, execution, and synthesis nodes."""
    graph.add_node(
        "select_tool_categories",
        wrap_with_context_async(select_tool_categories_node_fn),
    )
    graph.add_node(
        "articulation",
        wrap_with_context_async(
            articulate_tool_intent_fn,
            node_name="articulation",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "prepare_tool_plan",
        wrap_with_context_async(
            prepare_tool_execution_plan_fn,
            node_name="prepare_tool_plan",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "approval_gate",
        wrap_with_context_async(
            approval_gate_node_fn,
            node_name="approval_gate",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "dispatch_tool",
        wrap_with_context_async(
            dispatch_tool_execution_node_fn,
            node_name="dispatch_tool",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "tool_execution_session",
        build_tool_execution_session_subgraph(on_wrap_log=on_wrap_log),
    )
    graph.add_node(
        "terminal_session_compressor",
        wrap_with_context_async(
            terminal_session_compressor_fn,
            node_name="terminal_session_compressor",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "tool_synthesizer",
        wrap_with_context_async(
            synthesize_tool_output_fn,
            node_name="tool_synthesizer",
            on_wrap_log=on_wrap_log,
        ),
    )


def wire_direct_tool_action_path(
    graph: Any,
    *,
    route_after_prepare_tool_plan: Callable[[InteractiveState], str],
    terminal_target: str,
    conditional: Callable[..., Any] | None = None,
) -> Callable[..., Any]:
    """Wire ordinary execution directly and branch running work to continuation."""
    add_conditional_edges = conditional or require_conditional_edges(graph)
    graph.add_edge("select_tool_categories", "prepare_tool_plan")
    add_conditional_edges(
        "prepare_tool_plan",
        with_interactive_state(route_after_prepare_tool_plan),
        {
            "articulation": "articulation",
            "approval_gate": "approval_gate",
            "post_tool_reasoning": terminal_target,
        },
    )
    graph.add_edge("articulation", "approval_gate")
    graph.add_edge("approval_gate", "dispatch_tool")
    add_conditional_edges(
        "dispatch_tool",
        with_interactive_state(route_after_tool_dispatch),
        {
            "execution_session": "tool_execution_session",
            "terminal": "tool_synthesizer",
        },
    )
    graph.add_edge("tool_execution_session", "terminal_session_compressor")
    graph.add_edge("terminal_session_compressor", "tool_synthesizer")
    graph.add_edge("tool_synthesizer", terminal_target)
    return add_conditional_edges


def add_post_action_continuation_nodes(
    graph: Any,
    *,
    reasoning_node_name: str = "post_tool_reasoning",
    reasoning_node: Callable[..., Any] = post_tool_reasoning,
    on_wrap_log: WrapperLogCallback | None = None,
    decision_router_node: Callable[..., Any] = decision_router,
    think_more_node_fn: Callable[..., Any] = think_more_node,
    reflect_node_fn: Callable[..., Any] = reflect_node,
    synthesis_node_fn: Callable[..., Any] = synthesis_node,
    finalize_results_fn: Callable[..., Any] = finalize_results,
    finalize_turn_fn: Callable[..., Any] = finalize_turn,
) -> None:
    """Register existing PAR, router, reasoning, and finalization nodes."""
    graph.add_node(
        reasoning_node_name,
        wrap_with_context_async(
            reasoning_node,
            node_name=reasoning_node_name,
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "decision_router",
        wrap_with_context_async(
            decision_router_node,
            node_name="decision_router",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "think_more",
        wrap_with_context_async(
            think_more_node_fn,
            node_name="think_more",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "reflect",
        wrap_with_context_async(
            reflect_node_fn,
            node_name="reflect",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "synthesis",
        wrap_with_context_async(
            synthesis_node_fn,
            node_name="synthesis",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node(
        "format_results",
        wrap_with_context_async(
            finalize_results_fn,
            node_name="format_results",
            on_wrap_log=on_wrap_log,
        ),
    )
    graph.add_node("finalize", wrap_with_context(finalize_turn_fn))


def wire_post_action_continuation(
    graph: Any,
    *,
    route_after_router: Callable[[InteractiveState], str],
    router_targets: Mapping[str, str],
    reasoning_node_name: str = "post_tool_reasoning",
    conditional: Callable[..., Any] | None = None,
) -> Callable[..., Any]:
    """Wire PAR through the deterministic router and existing continuation nodes."""
    add_conditional_edges = conditional or require_conditional_edges(graph)
    graph.add_edge(reasoning_node_name, "decision_router")
    add_conditional_edges(
        "decision_router",
        with_interactive_state(route_after_router),
        dict(router_targets),
    )
    graph.add_edge("think_more", reasoning_node_name)
    graph.add_edge("reflect", "decision_router")
    graph.add_edge("synthesis", "format_results")
    graph.add_edge("format_results", "finalize")
    graph.add_edge("finalize", END)
    return add_conditional_edges


__all__ = [
    "add_direct_tool_action_nodes",
    "add_post_action_continuation_nodes",
    "wire_direct_tool_action_path",
    "wire_post_action_continuation",
]
