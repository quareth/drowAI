"""Graph construction utilities for LangGraph integration."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Mapping, Optional

from langgraph.graph import END, StateGraph

from .builders.common_edges import (
    wrap_with_context,
    wrap_with_context_async,
)
from .nodes import classify_turn as classify_node
from .nodes import finalize_turn
from .nodes.memory_retrieval import memory_retrieval_node
from .nodes import post_process_simple_chat
from .nodes import run_simple_chat
from .nodes.working_memory import update_working_memory_node
from .persistence import get_default_checkpointer
from .state import InteractiveInput, InteractiveState

NodeState = Mapping[str, Any]


def _ensure_state(raw_state: NodeState | InteractiveState) -> InteractiveState:
    """Adapt a raw graph state mapping to a typed ``InteractiveState``.

    The parameter is named ``raw_state`` (not the conventional ``state``)
    so the literal-token static inventory guard can enforce that no
    builder calls the boundary-conversion form bound to ``state``: the
    canonical builder-side conversion lives in
    ``common_edges.with_interactive_state``, and this helper is the
    legacy bootstrap path used by the minimal / simple-chat graphs.
    """
    return InteractiveState.from_mapping(raw_state)


def build_minimal_interactive_graph(
    *,
    checkpointer: Optional[Any] = None,
) -> Any:
    """Compile the bootstrap LangGraph used in Phase 1."""

    graph = StateGraph(dict)

    def classify_turn(state: NodeState) -> dict:
        interactive = _ensure_state(state)
        if not interactive.facts.capability:
            interactive.facts.capability = "respond_only"
        interactive.trace.reasoning.append(
            "LangGraph bootstrap classification: respond_only"
        )
        return interactive.as_graph_update()

    def finalize(state: NodeState) -> dict:
        interactive = _ensure_state(state)
        if interactive.trace.final_text is None:
            interactive.trace.final_text = interactive.facts.message
        interactive.trace.history.append(
            {
                "type": "final_text",
                "content": interactive.trace.final_text,
            }
        )
        return {"trace": interactive.trace.model_dump()}

    graph.add_node("classify_turn", classify_turn)
    graph.add_node("finalize", finalize)
    graph.set_entry_point("classify_turn")
    graph.add_edge("classify_turn", "finalize")
    graph.add_edge("finalize", END)

    compiled = graph.compile(
        checkpointer=checkpointer or get_default_checkpointer(),
    )
    return compiled


def build_simple_chat_graph(
    *,
    checkpointer: Optional[Any] = None,
) -> Any:
    """Compile the LangGraph branch responsible for simple chat responses.

    Wrappers are sourced from ``common_edges`` so context extraction and
    optional ``config`` / ``writer`` forwarding stay consistent across
    builders. The memory-retrieval node uses a small local adapter to
    preserve its empty-update fallback without leaking that policy into
    the shared wrapper factory.
    """

    graph = StateGraph(dict)

    async def _memory_retrieval_with_fallback(
        state: NodeState,
        context=None,
        config=None,
    ) -> dict:
        """Preserve the empty-update fallback for memory retrieval.

        ``memory_retrieval_node`` returns an empty update when retrieval
        is a no-op. This builder historically routed that empty update
        through ``_ensure_state`` so downstream nodes always see a fully
        formed state mapping. Keep that behaviour as a small local
        adapter rather than leaking the policy into the shared factory.
        """
        update = await memory_retrieval_node(state, context=context, config=config)
        if update:
            return update
        return _ensure_state(state).as_graph_update()

    graph.add_node("classification", wrap_with_context(classify_node))
    graph.add_node("update_working_memory", wrap_with_context(update_working_memory_node))
    graph.add_node(
        "memory_retrieval",
        wrap_with_context_async(_memory_retrieval_with_fallback),
    )
    graph.add_node("simple_chat", wrap_with_context_async(run_simple_chat))
    graph.add_node("post_process", wrap_with_context_async(post_process_simple_chat))
    graph.add_node("finalize", wrap_with_context(finalize_turn))
    graph.set_entry_point("classification")
    graph.add_edge("classification", "update_working_memory")
    graph.add_edge("update_working_memory", "memory_retrieval")
    graph.add_edge("memory_retrieval", "simple_chat")
    graph.add_edge("simple_chat", "post_process")
    graph.add_edge("post_process", "finalize")
    graph.add_edge("finalize", END)

    compiled = graph.compile(
        checkpointer=checkpointer or get_default_checkpointer(),
    )
    return compiled


def build_initial_state(payload: InteractiveInput) -> dict:
    """Helper to emit the initial state mapping for LangGraph."""
    return payload.to_state().as_graph_state()


@lru_cache(maxsize=1)
def get_compiled_minimal_graph() -> Any:
    """Return a cached compiled graph for repeated runs."""
    return build_minimal_interactive_graph()


@lru_cache(maxsize=1)
def get_compiled_simple_chat_graph() -> Any:
    """Return a cached compiled simple chat graph."""
    return build_simple_chat_graph()
