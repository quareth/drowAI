"""Focused wiring tests for the parent handoff continuation graph."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END

import agent.graph.builders.parent_handoff_builder as parent_handoff_builder
from agent.graph.builders.parent_handoff_builder import (
    _PARENT_ROUTER_ACTION_MAP,
    _prepare_direct_tool_reasoning_context,
    _prepare_handoff_reasoning_context,
    _route_after_parent_router,
    build_parent_handoff_graph,
)
from agent.graph.nodes.post_tool_reasoning.models import (
    DIRECT_TOOL_OUTCOME_SOURCE,
    POST_ACTION_OUTCOME_SOURCE_METADATA_KEY,
    SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE,
)
from agent.graph.state import FactsState, InteractiveState, TraceState


def _make_interactive(*, router_action: str | None = None) -> InteractiveState:
    """Build a minimal parent state for route-level assertions."""
    metadata: dict[str, Any] = {}
    if router_action is not None:
        metadata["router_outcome"] = {"action": router_action}
    return InteractiveState(
        facts=FactsState(task_id=1, message="handoff", metadata=metadata),
        trace=TraceState(),
    )


def test_parent_handoff_graph_enters_par_before_finalization() -> None:
    """A claimed child handoff reaches PAR before the finalizer path."""
    graph = build_parent_handoff_graph(build_only=True)
    nodes = getattr(graph, "nodes", {}) or {}
    edges = getattr(graph, "edges", set())

    assert "prepare_handoff_context" in nodes
    assert "post_action_reasoning" in nodes
    assert "decision_router" in nodes
    assert "format_results" in nodes
    assert "finalize" in nodes

    assert ("prepare_handoff_context", "post_action_reasoning") in edges
    assert ("post_action_reasoning", "decision_router") in edges
    assert ("format_results", "finalize") in edges
    assert ("finalize", END) in edges
    assert ("prepare_handoff_context", "format_results") not in edges


def test_parent_handoff_router_controls_end_at_graph_boundary() -> None:
    """Delegate and wait are typed backend-control boundaries, not graph work."""
    graph = build_parent_handoff_graph(build_only=True)
    branches = getattr(graph, "branches", {}) or {}
    router_branch = branches["decision_router"]["_route_after_parent_router"]

    assert router_branch.ends["delegate_subagent"] == END
    assert router_branch.ends["wait_for_subagents"] == END
    assert router_branch.ends["format_results"] == "format_results"
    assert router_branch.ends["select_tool_categories"] == "select_tool_categories"


def test_parent_handoff_router_action_map_includes_all_parent_routes() -> None:
    """The parent graph owns route labels for all PAR outcomes."""
    assert _PARENT_ROUTER_ACTION_MAP == {
        "call_tool": "select_tool_categories",
        "think_more": "think_more",
        "reflect": "reflect",
        "synthesis": "synthesis",
        "finalize": "format_results",
        "delegate_subagent": "delegate_subagent",
        "wait_for_subagents": "wait_for_subagents",
    }


def test_parent_handoff_route_function_preserves_control_actions() -> None:
    """Router metadata for delegate/wait terminates with the same typed label."""
    assert (
        _route_after_parent_router(_make_interactive(router_action="delegate_subagent"))
        == "delegate_subagent"
    )
    assert (
        _route_after_parent_router(
            _make_interactive(router_action="wait_for_subagents")
        )
        == "wait_for_subagents"
    )


def test_handoff_context_preparation_marks_handoff_source_and_preserves_plan() -> None:
    """The entry adapter does not rebuild parent plan/todo state between cycles."""
    state = InteractiveState(
        facts=FactsState(
            task_id=1,
            message="handoff",
            plan=["Keep global plan"],
            todo_list=["Keep global todo"],
            metadata={
                "completed_agent_results": [{"agent_run_id": "run-1"}],
                "synthesized_output": {"summary": "stale direct-tool output"},
            },
        ),
        trace=TraceState(reasoning=["prior parent reasoning"]),
    )

    result = _prepare_handoff_reasoning_context(state.as_graph_state())
    updated = InteractiveState.from_mapping(result)

    assert updated.facts.metadata[POST_ACTION_OUTCOME_SOURCE_METADATA_KEY] == (
        SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE
    )
    assert "synthesized_output" not in updated.facts.metadata
    assert updated.facts.plan == ["Keep global plan"]
    assert updated.facts.todo_list == ["Keep global todo"]
    assert updated.trace.reasoning == ["prior parent reasoning"]


def test_direct_tool_context_preparation_marks_direct_source() -> None:
    """Direct-tool continuations re-enter PAR with direct-tool source metadata."""
    state = InteractiveState(
        facts=FactsState(
            task_id=1,
            message="handoff",
            metadata={
                POST_ACTION_OUTCOME_SOURCE_METADATA_KEY: (
                    SUBAGENT_HANDOFF_BATCH_OUTCOME_SOURCE
                ),
                "synthesized_output": {"summary": "fresh tool result"},
            },
        ),
        trace=TraceState(),
    )

    result = _prepare_direct_tool_reasoning_context(state.as_graph_state())
    updated = InteractiveState.from_mapping(result)

    assert (
        updated.facts.metadata[POST_ACTION_OUTCOME_SOURCE_METADATA_KEY]
        == DIRECT_TOOL_OUTCOME_SOURCE
    )
    assert updated.facts.metadata["synthesized_output"] == {
        "summary": "fresh tool result"
    }


async def test_parent_handoff_node_uses_patched_callable_from_graph_construction(
    monkeypatch: Any,
) -> None:
    """Builder-local patches are captured when the graph is constructed."""
    received: dict[str, Any] = {}

    async def _patched_post_tool_reasoning(
        state: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        received.update(state=state, context=context)
        return {"facts": {"message": "patched"}}

    monkeypatch.setattr(
        parent_handoff_builder,
        "post_tool_reasoning",
        _patched_post_tool_reasoning,
    )
    graph = build_parent_handoff_graph(build_only=True)
    state = {"facts": {"metadata": {}}}

    result = await graph.nodes["post_action_reasoning"].runnable.ainvoke(state)

    assert result == {"facts": {"message": "patched"}}
    assert received == {"state": state, "context": None}
