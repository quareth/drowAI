"""Builder for the deep reasoning LangGraph with full DR loop."""

from __future__ import annotations

import logging
from typing import Optional

from langgraph.graph import END, StateGraph

from backend.services.metrics.utils import safe_inc
from agent.reasoning.tool_selection_sentinel import metadata_has_unavailable_capability

from ..graph_names import GRAPH_NAME_DEEP_REASONING
from ..infrastructure.graph_registry import (
    GraphRegistry,
    get_default_graph_registry,
    get_or_register_compiled_graph,
)
from ..state import InteractiveState
from ..persistence import get_default_checkpointer
from ..nodes.working_memory import (
    update_working_memory_node,
)
from ..nodes.memory_retrieval import memory_retrieval_node
from .common_edges import (
    build_router_action_map,
    wire_capability_gate,
    with_interactive_state,
    wrap_with_context,
    wrap_with_context_async,
)
from .diagnostics import (
    log_builder_graph_build,
    make_wrapper_log_callback,
)

GRAPH_NAME = GRAPH_NAME_DEEP_REASONING

logger = logging.getLogger(__name__)


# Build a single wrapper-callback instance so every DR-wrapped node forwards
# diagnostic signal through the shared Tier 5 helper. Mirrors simple-tool
# diagnostic parity for the listed tool-execution / deep-reasoning nodes.
_log_node_wrapper_context = make_wrapper_log_callback()

_DR_ROUTER_ACTION_MAP = build_router_action_map(
    call_tool_target="select_categories",
    finalize_target="finalize",
)


def _route_after_clarify_gate(interactive: InteractiveState) -> str:
    """Route to planner after clarify state is processed."""
    metadata = interactive.facts.safe_metadata
    if metadata.get("planner_mode") == "plan_failed" or metadata.get("plan_rejected"):
        return "finalize"
    return "planner"


def _route_from_planner(interactive: InteractiveState) -> str:
    """Route from plan review based on approval and tool availability."""
    metadata = interactive.facts.safe_metadata

    if metadata.get("plan_rejected"):
        logger.info("[ROUTE_PLANNER] Plan rejected by user, routing to finalize")
        return "finalize"

    decision_history = interactive.facts.safe_decision_history
    if decision_history:
        last_decision = decision_history[-1]
        if "handle_unavailable_tools" in last_decision.lower():
            return "handle_unavailable_tools"

    return "decision_router"


def _route_after_planner(interactive: InteractiveState) -> str:
    """Route planner output to plan review or clarify gate loop."""
    metadata = interactive.facts.safe_metadata
    if metadata.get("planner_mode") == "clarify_required":
        return "clarify_gate"
    return "plan_review"


def _route_decision(interactive: InteractiveState) -> str:
    """Dispatch from ``metadata.router_outcome.action`` with safe fallback."""
    metadata = interactive.facts.safe_metadata
    outcome = metadata.get("router_outcome") if isinstance(metadata, dict) else None
    action = ""
    if isinstance(outcome, dict):
        action = str(outcome.get("action") or "").strip().lower()

    target = _DR_ROUTER_ACTION_MAP.get(action)
    if target is None:
        safe_inc("deep_reasoning_router_action_missing")
        return "finalize"

    safe_inc(f"deep_reasoning_router_action_{action}")
    return target


def _route_after_prepare_tool_plan(interactive: InteractiveState) -> str:
    """Bypass tool approval/dispatch when planning produced a PTR blocker."""
    if metadata_has_unavailable_capability(interactive.facts.safe_metadata):
        safe_inc("deep_reasoning_prepare_tool_plan_unavailable_capability")
        return "post_tool_reasoning"
    return "approval_gate"


def build_deep_reasoning_graph(*, checkpointer=None) -> StateGraph:
    """Construct the deep reasoning graph with full DR loop.
    
    Graph Structure:
    - classification: Verify we should enter DR
    - clarify_gate: Non-LLM control gate for pending clarify blockers
    - planner: Initial task decomposition
    - plan_review: Plan approval/interrupt (if required)
    - decision_router: Route to next action (think/tool/reflect/synthesis/finalize)
    - think_more: Pure reasoning node
    - select_categories: LLM selects relevant tool categories (before tool execution)
    - call_tool: Execute tool with category-filtered catalog
    - tool_synthesizer: Process tool output with LLM (extract structured findings)
    - post_tool_reasoning: Unified observation + decision (replaces observation_articulation)
    - observation_adapter: Convert findings to compact observations for agent
    - reflect: Failure analysis and replanning
    - synthesis: Graceful loop termination when stuck (triggered by consecutive reflects)
    - finalize: Synthesize final answer
    
    Tool Execution Flow (NEW - unified reasoning):
    select_categories → call_tool → tool_synthesizer → post_tool_reasoning → 
    observation_adapter → decision_router → [router_outcome.action dispatch]
    
    This ensures:
    1. Relevant tool categories are selected first (focused tool selection)
    2. Tool outputs are processed into structured findings
    3. post_tool_reasoning produces observation + decision in ONE LLM call
    4. What the observation says = what actually happens next
    5. Observations flow to adapter for dedup/progress tracking
    6. Routing happens via deterministic router authority, not builder-local parsing
    
    The decision_router handles both non-tool and post-tool route authority paths.
    """
    # Import nodes here to avoid circular imports
    from ..nodes.classification import classify_turn
    from ..nodes.clarify_gate import clarify_gate_node
    from ..nodes.decision_router import decision_router
    from ..nodes.finalize import finalize_results
    from ..nodes.finalizer import finalize_turn
    from ..nodes.handle_unavailable_tools import handle_unavailable_tools_node
    from ..nodes.observation_adapter import adapt_to_observations
    from ..nodes.plan_review import plan_review_node
    from ..nodes.planner import planner_node
    from ..nodes.post_tool_reasoning import post_tool_reasoning
    from ..nodes.reflect import reflect_node
    from ..nodes.select_tool_categories import select_tool_categories_node
    from ..nodes.synthesis import synthesis_node
    from ..nodes.think_more import think_more_node
    from ..nodes.tool_synthesizer import synthesize_tool_output
    from ..subgraphs.tool_execution import (
        approval_gate_node,
        dispatch_tool_execution_node,
        prepare_tool_execution_plan,
    )
    
    graph = StateGraph(dict)

    # Add all nodes. DR mirrors simple-tool's wrapper diagnostic parity:
    # tool-execution / deep-reasoning nodes pass ``node_name`` plus the
    # shared ``_log_node_wrapper_context`` callback so observability
    # decouples from each wrapped node's signature. Non-instrumented
    # nodes (classification/update_working_memory/fallback_finalize) are
    # outside the listed Tier 5 parity scope and stay silent.
    graph.add_node("classification", wrap_with_context(classify_turn))
    graph.add_node("update_working_memory", wrap_with_context(update_working_memory_node))
    graph.add_node(
        "memory_retrieval",
        wrap_with_context_async(
            memory_retrieval_node,
            node_name="memory_retrieval",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "clarify_gate",
        wrap_with_context_async(
            clarify_gate_node,
            node_name="clarify_gate",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "planner",
        wrap_with_context_async(
            planner_node,
            node_name="planner",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "plan_review",
        wrap_with_context_async(
            plan_review_node,
            node_name="plan_review",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "handle_unavailable_tools",
        wrap_with_context_async(
            handle_unavailable_tools_node,
            node_name="handle_unavailable_tools",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "decision_router",
        wrap_with_context_async(
            decision_router,
            node_name="decision_router",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "think_more",
        wrap_with_context_async(
            think_more_node,
            node_name="think_more",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "select_categories",
        wrap_with_context_async(
            select_tool_categories_node,
            node_name="select_categories",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "prepare_tool_plan",
        wrap_with_context_async(
            prepare_tool_execution_plan,
            node_name="prepare_tool_plan",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "approval_gate",
        wrap_with_context_async(
            approval_gate_node,
            node_name="approval_gate",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "dispatch_tool",
        wrap_with_context_async(
            dispatch_tool_execution_node,
            node_name="dispatch_tool",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "tool_synthesizer",
        wrap_with_context_async(
            synthesize_tool_output,
            node_name="tool_synthesizer",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    # NEW: Unified post-tool reasoning replaces observation_articulation
    graph.add_node(
        "post_tool_reasoning",
        wrap_with_context_async(
            post_tool_reasoning,
            node_name="post_tool_reasoning",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "observation_adapter",
        wrap_with_context_async(
            adapt_to_observations,
            node_name="observation_adapter",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "reflect",
        wrap_with_context_async(
            reflect_node,
            node_name="reflect",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "synthesis",
        wrap_with_context_async(
            synthesis_node,
            node_name="synthesis",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    # Unified finalizer (capability-aware; runs the DR LLM pass via
    # ``agent.graph.nodes.finalize.finalize_results``) followed by the
    # cheap suffixer ``finalize_turn`` to mirror the simple-tool topology
    # (gap notes + memory extraction). ``fallback_finalize`` is reused as
    # the suffixer step so the False-classification short-circuit and the
    # main DR exit both terminate through the same node.
    graph.add_node(
        "finalize",
        wrap_with_context_async(
            finalize_results,
            node_name="finalize",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node("fallback_finalize", wrap_with_context(finalize_turn))

    # Set entry point
    graph.set_entry_point("classification")

    # Classification routes to update_working_memory if the deep_reasoning
    # capability is present, otherwise to the fallback finalize suffixer.
    conditional = wire_capability_gate(
        graph,
        capability="deep_reasoning",
        false_target="fallback_finalize",
    )

    graph.add_edge("update_working_memory", "memory_retrieval")
    graph.add_edge("memory_retrieval", "clarify_gate")
    conditional(
        "clarify_gate",
        with_interactive_state(_route_after_clarify_gate),
        {
            "finalize": "finalize",
            "planner": "planner",
        },
    )

    conditional(
        "planner",
        with_interactive_state(_route_after_planner),
        {
            "clarify_gate": "clarify_gate",
            "plan_review": "plan_review",
        },
    )

    conditional(
        "plan_review",
        with_interactive_state(_route_from_planner),
        {
            "handle_unavailable_tools": "handle_unavailable_tools",
            "decision_router": "decision_router",
            "finalize": "finalize",
        },
    )
    
    # handle_unavailable_tools routes based on decision (finalize or planner)
    def _route_degradation(interactive: InteractiveState) -> str:
        """Route from handle_unavailable_tools based on decision."""
        decision_history = interactive.facts.safe_decision_history

        if not decision_history:
            return "finalize"

        last_decision = decision_history[-1]
        if "planner" in last_decision.lower():
            return "planner"  # Replan with fallback capability
        else:
            return "finalize"  # Finalize with limitations

    conditional(
        "handle_unavailable_tools",
        with_interactive_state(_route_degradation),
        {"planner": "planner", "finalize": "finalize"},
    )

    # Route based on decision-router outcome.
    conditional(
        "decision_router",
        with_interactive_state(_route_decision),
        {
            "select_categories": "select_categories",
            "think_more": "think_more",
            "reflect": "reflect",
            "synthesis": "synthesis",
            "finalize": "finalize",
        },
    )
    
    # think_more enriches state and returns to PTR for the next candidate.
    graph.add_edge("think_more", "post_tool_reasoning")
    
    # Tool execution flow (NEW - with unified post_tool_reasoning):
    # select_categories → call_tool → tool_synthesizer → post_tool_reasoning → 
    # observation_adapter → [conditional routing based on decision]
    #
    # Key change: post_tool_reasoning produces BOTH observation AND decision in one LLM call.
    # This guarantees: what observation says = what actually happens.
    graph.add_edge("select_categories", "prepare_tool_plan")
    conditional(
        "prepare_tool_plan",
        with_interactive_state(_route_after_prepare_tool_plan),
        {
            "approval_gate": "approval_gate",
            "post_tool_reasoning": "post_tool_reasoning",
        },
    )
    graph.add_edge("approval_gate", "dispatch_tool")
    graph.add_edge("dispatch_tool", "tool_synthesizer")
    graph.add_edge("tool_synthesizer", "post_tool_reasoning")
    graph.add_edge("post_tool_reasoning", "observation_adapter")
    
    # Post-tool path now re-enters router authority before dispatch.
    graph.add_edge("observation_adapter", "decision_router")

    # reflect loops back to decision router (for non-tool paths)
    graph.add_edge("reflect", "decision_router")
    
    # Synthesis produces final summary when stuck in loop (detected by consecutive reflect limit)
    graph.add_edge("synthesis", "finalize")
    
    # Topology: unified ``finalize`` (LLM pass) → ``fallback_finalize``
    # (cheap suffixer / memory extraction) → END. This matches the
    # simple-tool graph and gives the deep-reasoning path symmetric
    # gap-note + memory-consolidation behaviour.
    graph.add_edge("finalize", "fallback_finalize")
    graph.add_edge("fallback_finalize", END)
    
    return graph


def _log_dr_graph_build(graph: StateGraph, checkpointer: object) -> None:
    """Emit DR graph-build diagnostics with the actual checkpointer type.

    Used as the ``on_compiled`` callback for
    :func:`get_or_register_compiled_graph` so DR records ``GRAPH_NAME``,
    the resolved checkpointer type, and node count through the shared
    diagnostic helper without leaking diagnostic imports into
    ``graph_registry.py``.
    """
    log_builder_graph_build(
        GRAPH_NAME,
        type(checkpointer).__name__,
        len(graph.nodes) if hasattr(graph, "nodes") else 0,
    )


def compile_deep_reasoning_graph(*, checkpointer=None) -> object:
    """Build and compile the deep-reasoning graph with diagnostics.

    Active backend execution paths compile DR graphs with a per-task
    checkpointer instead of the registry-backed getter. Keeping that
    compile step here preserves graph-build diagnostic parity without
    scattering builder diagnostics across backend services.
    """
    graph = build_deep_reasoning_graph()
    actual_checkpointer = checkpointer or get_default_checkpointer()
    _log_dr_graph_build(graph, actual_checkpointer)
    return graph.compile(checkpointer=actual_checkpointer)


def get_compiled_deep_reasoning_graph(
    *,
    registry: Optional[GraphRegistry] = None,
) -> object:
    """Return the compiled deep reasoning graph from the shared registry.

    ``build_deep_reasoning_graph()`` already returns an uncompiled
    ``StateGraph``; the registry helper owns the single ``compile`` call.
    """

    return get_or_register_compiled_graph(
        registry=registry or get_default_graph_registry(),
        name=GRAPH_NAME,
        build_uncompiled=build_deep_reasoning_graph,
        checkpointer_factory=get_default_checkpointer,
        on_compiled=_log_dr_graph_build,
    )


__all__ = [
    "build_deep_reasoning_graph",
    "compile_deep_reasoning_graph",
    "get_compiled_deep_reasoning_graph",
]
