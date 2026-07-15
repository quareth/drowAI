"""Skeleton builder for the simple tool execution graph."""

from __future__ import annotations

import logging
from typing import Any, Optional

from langgraph.graph import END, StateGraph

from backend.services.metrics.utils import safe_inc
from agent.reasoning.tool_selection_sentinel import metadata_has_unavailable_capability

from ..graph_names import GRAPH_NAME_SIMPLE_TOOL
from ..infrastructure.graph_registry import (
    GraphRegistry,
    get_default_graph_registry,
    get_or_register_compiled_graph,
)
from ..nodes.classification import classify_turn
from ..nodes.decision_router import decision_router
from ..nodes.finalize import finalize_results
from ..nodes.finalizer import finalize_turn
from ..nodes.memory_retrieval import memory_retrieval_node
from ..nodes.post_tool_reasoning.core.retry_logic import RETRY_METADATA_KEY
from ..nodes.post_tool_reasoning.node import post_tool_reasoning
from ..nodes.reflect import reflect_node
from ..nodes.select_tool_categories import select_tool_categories_node
from ..nodes.synthesis import synthesis_node
from ..nodes.think_more import think_more_node
from ..nodes.tool_articulation import articulate_tool_intent
from ..nodes.tool_synthesizer import synthesize_tool_output
from ..nodes.working_memory import (
    update_working_memory_node,
)
from ..persistence import get_default_checkpointer
from ..subgraphs.tool_execution import (
    approval_gate_node,
    dispatch_tool_execution_node,
    prepare_tool_execution_plan,
)
from ..state import InteractiveState
from .common_edges import (
    build_router_action_map,
    wire_capability_gate,
    with_interactive_state,
    wrap_with_context,
    wrap_with_context_async,
)
from .diagnostics import (
    get_builder_diagnostic_logger,
    log_builder_graph_build,
    make_wrapper_log_callback,
)


# Resolve the optional backend diagnostic logger once at import time and
# build a single wrapper-callback instance so every wrapped node forwards
# diagnostic signal through the same Tier 5 shared helper.
diag = get_builder_diagnostic_logger()
_log_node_wrapper_context = make_wrapper_log_callback()

GRAPH_NAME = GRAPH_NAME_SIMPLE_TOOL
logger = logging.getLogger(__name__)


_ROUTER_ACTION_MAP = build_router_action_map(
    call_tool_target="select_tool_categories",
    finalize_target="format_results",
)


def _route_after_router(interactive: InteractiveState) -> str:
    """Dispatch from ``metadata.router_outcome.action`` with safe fallback."""
    metadata = interactive.facts.safe_metadata
    outcome = metadata.get("router_outcome") if isinstance(metadata, dict) else None
    action = ""
    if isinstance(outcome, dict):
        action = str(outcome.get("action") or "").strip().lower()

    target = _ROUTER_ACTION_MAP.get(action)
    if target is None:
        logger.warning(
            "[ROUTE_ROUTER] Missing/unknown router_outcome.action '%s'; defaulting to format_results",
            action,
        )
        safe_inc("simple_tool_router_action_unknown")
        return "format_results"

    safe_inc(f"simple_tool_router_action_{action}")
    return target


def _route_after_prepare_tool_plan(interactive: InteractiveState) -> str:
    """Route prepared unavailable-capability state to PTR without execution."""
    if metadata_has_unavailable_capability(interactive.facts.safe_metadata):
        safe_inc("simple_tool_prepare_tool_plan_unavailable_capability")
        return "post_tool_reasoning"

    metadata = interactive.facts.safe_metadata
    retry_data = metadata.get(RETRY_METADATA_KEY, {}) or {}
    retry_count = retry_data.get("count", 0)
    should_articulate = retry_count == 0
    logger.info(
        "[ARTICULATION_GATE] retry_count=%s, should_articulate=%s",
        retry_count,
        should_articulate,
    )
    return "articulation" if should_articulate else "approval_gate"


def build_simple_tool_graph(*, checkpointer=None, build_only: bool = False) -> Any:
    """Construct the simple tool execution graph with LLM-powered synthesis and recovery.

    Note: Artifact indexing is now handled as a fire-and-forget side effect within
    tool_execution node, controlled by LANGGRAPH_ENABLE_ARTIFACT_INDEXING env var.

    Node order:
       1. classification          -> determine capability/tool access
       2. update_working_memory   -> deterministic working-memory bundle update
       3. memory_retrieval        -> fetch prior findings for prompt context
       4. select_tool_categories  -> LLM selects relevant tool categories
       5. prepare_tool_plan       -> precompute tool + params before HITL interrupt
       6. articulation            -> explain intent before tool execution (first attempt only)
       7. approval_gate           -> interrupt only, no planning (shared contract with DR)
       8. dispatch_tool           -> execution only, no interrupt (shared contract with DR)
       9. tool_synthesizer        -> extract structured findings from tool output (LLM-powered)
      10. post_tool_reasoning     -> analyze results and emit candidate decision
      11. decision_router         -> deterministic route authority, writes router_outcome
      12. [CONDITIONAL dispatch on router_outcome.action]
            call_tool   -> select_tool_categories (loop)
            think_more  -> think_more -> post_tool_reasoning
            reflect     -> reflect -> decision_router (one-hop hint recovery)
            synthesis   -> synthesis    (terminal-bound)
            finalize    -> format_results
      13. format_results          -> stream structured data for frontend rendering
      14. finalize                -> produce final assistant response

    This architecture separates concerns:
    - select_tool_categories: Category-based tool filtering (focused selection)
    - articulation: Natural language explanation of action (streaming, first attempt only)
    - approval_gate + dispatch_tool: Shared HITL contract (interrupt then execute)
    - tool_synthesizer: Pure LLM processing (reusable by deep reasoning)
    - post_tool_reasoning: Unified failure detection and candidate decision emission
    - decision_router: deterministic route authority for normal-loop actions
    - think_more / reflect / synthesis: Reasoning destinations shared with DR; each
      returns through deterministic router authority for final dispatch
    - format_results: Streams structured data (frontend formats display)

    Recovery Flow:
    - post_tool_reasoning detects failures and decides next_action
    - call_tool: routes back to select_tool_categories (recovery retry, corrective retry,
      or bounded direct-executor continuation — policy is owned by PTR)
    - think_more / reflect / synthesis: enter reasoning node, then re-enter router
      authority for deterministic dispatch
    - finalize: proceeds to format_results and finalize
    """
    
    # Log graph build start
    if diag:
        diag.info(
            f"BUILDER | Starting {GRAPH_NAME} build | "
            f"checkpointer={'provided' if checkpointer else 'none'}"
        )

    graph = StateGraph(dict)

    # Wrappers come from ``common_edges`` so context extraction and
    # optional ``config`` / ``writer`` forwarding stay consistent across
    # builders. Diagnostic labels are preserved verbatim by passing
    # ``node_name`` plus the local ``_log_node_wrapper_context`` callback,
    # so observability decouples from the wrapped node's signature.
    graph.add_node("classification", wrap_with_context(classify_turn))
    graph.add_node(
        "update_working_memory",
        wrap_with_context(update_working_memory_node),
    )
    graph.add_node(
        "memory_retrieval",
        wrap_with_context_async(memory_retrieval_node),
    )
    graph.add_node(
        "select_tool_categories",
        wrap_with_context_async(select_tool_categories_node),
    )
    graph.add_node(
        "articulation",
        wrap_with_context_async(
            articulate_tool_intent,
            node_name="articulation",
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
            node_name="synthesizer",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node(
        "post_tool_reasoning",
        wrap_with_context_async(
            post_tool_reasoning,
            node_name="post_tool_reasoning",
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
    graph.add_node(
        "format_results",
        wrap_with_context_async(
            finalize_results,
            node_name="format_results",
            on_wrap_log=_log_node_wrapper_context,
        ),
    )
    graph.add_node("finalize", wrap_with_context(finalize_turn))

    graph.set_entry_point("classification")

    # Classification routes to update_working_memory if the
    # simple_tool_execution capability is present, otherwise straight to
    # finalize. The capability predicate lives in ``common_edges`` to keep
    # both builder gates aligned.
    conditional = wire_capability_gate(
        graph,
        capability="simple_tool_execution",
        false_target="finalize",
    )

    # Main path: update_working_memory -> memory_retrieval -> select_tool_categories -> prepare_tool_plan -> ...
    # Working memory is context-only and must not block execution.
    graph.add_edge("update_working_memory", "memory_retrieval")
    graph.add_edge("memory_retrieval", "select_tool_categories")
    #             [articulation or approval_gate] -> dispatch_tool -> synthesizer -> post_tool_reasoning
    #               -> decision_router -> [call_tool / think_more / reflect / synthesis / finalize]
    # - select_tool_categories: LLM selects relevant tool categories
    # - articulation: Natural language explanation (ONLY on first attempt, skip on retry)
    # - approval_gate + dispatch_tool: Shared HITL contract (interrupt then execute)
    # - tool_synthesizer: Extracts structured findings (reusable LLM logic)
    # - post_tool_reasoning: Analyzes results and emits candidate_decision
    # - decision_router: deterministic route authority (writes router_outcome)
    # - think_more / reflect / synthesis: Reasoning destinations (same node modules as DR)
    # - format_results: Streams structured data for frontend rendering
    # - finalize: Produces final assistant response
    # Note: Artifact indexing happens as a fire-and-forget side effect within tool_execution
    
    # Tool/params are selected in prepare_tool_plan; articulation runs after that decision.
    # Conditional: Skip articulation on retry to avoid confusing UI updates.
    graph.add_edge("select_tool_categories", "prepare_tool_plan")
    conditional(
        "prepare_tool_plan",
        with_interactive_state(_route_after_prepare_tool_plan),
        {
            "articulation": "articulation",
            "approval_gate": "approval_gate",
            "post_tool_reasoning": "post_tool_reasoning",
        },
    )
    graph.add_edge("articulation", "approval_gate")
    graph.add_edge("approval_gate", "dispatch_tool")
    graph.add_edge("dispatch_tool", "tool_synthesizer")
    graph.add_edge("tool_synthesizer", "post_tool_reasoning")
    
    # Post-tool decisions always return to deterministic router authority.
    graph.add_edge("post_tool_reasoning", "decision_router")

    # Router-authoritative action dispatch for simple-tool boundaries.
    conditional(
        "decision_router",
        with_interactive_state(_route_after_router),
        {
            "select_tool_categories": "select_tool_categories",
            "think_more": "think_more",
            "reflect": "reflect",
            "synthesis": "synthesis",
            "format_results": "format_results",
        },
    )

    # think_more enriches context and returns to PTR for the next loop candidate.
    graph.add_edge("think_more", "post_tool_reasoning")

    # reflect emits a one-hop hint and re-enters router authority for consume+clear.
    graph.add_edge("reflect", "decision_router")

    # synthesis produces a final answer, then proceeds to terminal formatting (Task 1.7).
    # synthesis.py is unchanged.
    graph.add_edge("synthesis", "format_results")

    graph.add_edge("format_results", "finalize")
    graph.add_edge("finalize", END)

    if build_only:
        return graph

    # ✅ Compile inside builder (like simple_chat) to enable writer injection
    actual_checkpointer = checkpointer or get_default_checkpointer()
    
    # Log graph compilation through the shared diagnostic helper.
    log_builder_graph_build(
        GRAPH_NAME,
        type(actual_checkpointer).__name__,
        len(graph.nodes) if hasattr(graph, 'nodes') else 8,  # Added post_tool_reasoning for recovery
    )
    
    compiled = graph.compile(checkpointer=actual_checkpointer)
    
    if diag:
        diag.info(
            f"BUILDER | Graph compiled successfully | "
            f"type={type(compiled).__name__}, "
            f"has_checkpointer={hasattr(compiled, 'checkpointer')}, "
            f"has_astream={hasattr(compiled, 'astream')}"
        )
    
    return compiled


def get_compiled_simple_tool_graph(
    *,
    registry: Optional[GraphRegistry] = None,
) -> object:
    """Return the compiled simple tool graph from the shared registry.

    Uses ``build_simple_tool_graph(build_only=True)`` so the registry helper
    owns the single ``compile`` call. Calling the default
    ``build_simple_tool_graph()`` here would double-compile because it
    already returns a compiled graph by default.
    """

    return get_or_register_compiled_graph(
        registry=registry or get_default_graph_registry(),
        name=GRAPH_NAME,
        build_uncompiled=lambda: build_simple_tool_graph(build_only=True),
        checkpointer_factory=get_default_checkpointer,
    )


__all__ = ["build_simple_tool_graph", "get_compiled_simple_tool_graph", "GRAPH_NAME"]
