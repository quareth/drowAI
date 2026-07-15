"""Tests for Deep Reasoning builder route helpers and topology contracts."""

from agent.graph.builders.deep_reasoning_builder import (
    _route_after_clarify_gate,
    _route_decision,
    _route_after_prepare_tool_plan,
    _route_after_planner,
    _route_from_planner,
    build_deep_reasoning_graph,
)
from agent.reasoning.tool_selection_sentinel import UNAVAILABLE_CAPABILITY_METADATA_KEY
from agent.graph.state import FactsState, InteractiveState, TraceState


def _branch_ends(graph, source: str, branch_name: str) -> dict:
    """Return LangGraph conditional branch targets for topology assertions."""
    return graph.branches[source][branch_name].ends


def _state_with(*, metadata=None, decision_history=None) -> InteractiveState:
    """Build an ``InteractiveState`` for the migrated route handlers.

    Phase 2 of the LangGraph DRY migration converted deep-reasoning route
    functions to take a typed ``InteractiveState`` directly. The graph
    wiring now wraps them with ``with_interactive_state(...)``; tests
    invoke the underlying handlers with the typed argument.
    """
    return InteractiveState(
        facts=FactsState(
            task_id=1,
            message="test",
            metadata=metadata or {},
            decision_history=decision_history or [],
        ),
        trace=TraceState(),
    )


def test_route_from_planner_rejected() -> None:
    state = _state_with(metadata={"plan_rejected": True})
    assert _route_from_planner(state) == "finalize"


def test_route_from_planner_handle_unavailable_tools() -> None:
    state = _state_with(decision_history=["handle_unavailable_tools"])
    assert _route_from_planner(state) == "handle_unavailable_tools"


def test_route_from_planner_default() -> None:
    state = _state_with()
    assert _route_from_planner(state) == "decision_router"


def test_route_after_planner_clarify_required() -> None:
    state = _state_with(metadata={"planner_mode": "clarify_required"})
    assert _route_after_planner(state) == "clarify_gate"


def test_route_after_planner_plan_ready_default() -> None:
    state = _state_with(metadata={"planner_mode": "plan_ready"})
    assert _route_after_planner(state) == "plan_review"


def test_route_after_clarify_gate_routes_to_planner_by_default() -> None:
    state = _state_with(metadata={})
    assert _route_after_clarify_gate(state) == "planner"


def test_route_after_clarify_gate_plan_failure_finalizes() -> None:
    state = _state_with(metadata={"planner_mode": "plan_failed"})
    assert _route_after_clarify_gate(state) == "finalize"


def test_route_after_prepare_tool_plan_bypasses_dispatch_for_unavailable_capability() -> None:
    state = _state_with(
        metadata={
            UNAVAILABLE_CAPABILITY_METADATA_KEY: {
                "active": True,
                "status": "unavailable_capability",
            }
        }
    )
    assert _route_after_prepare_tool_plan(state) == "post_tool_reasoning"


def test_route_after_prepare_tool_plan_uses_approval_gate_by_default() -> None:
    state = _state_with()
    assert _route_after_prepare_tool_plan(state) == "approval_gate"


def test_deep_reasoning_graph_routes_clarify_gate_to_planner_or_finalize() -> None:
    graph = build_deep_reasoning_graph()
    nodes = getattr(graph, "nodes", None) or {}

    assert "clarify_gate" in nodes
    assert "todo_bootstrap" not in nodes
    assert _branch_ends(graph, "clarify_gate", "_route_after_clarify_gate") == {
        "finalize": "finalize",
        "planner": "planner",
    }


def test_deep_reasoning_graph_keeps_plan_review_behind_planner_only() -> None:
    graph = build_deep_reasoning_graph()

    assert _branch_ends(graph, "planner", "_route_after_planner") == {
        "clarify_gate": "clarify_gate",
        "plan_review": "plan_review",
    }


def test_deep_reasoning_post_tool_reenters_router_authority() -> None:
    """Post-tool DR boundary should return to decision_router before dispatch."""
    graph = build_deep_reasoning_graph()
    edges = getattr(graph, "edges", set())

    assert ("think_more", "post_tool_reasoning") in edges
    assert ("post_tool_reasoning", "observation_adapter") in edges
    assert ("observation_adapter", "decision_router") in edges


def test_deep_reasoning_reflect_reenters_router_authority() -> None:
    """Task 4.3 contract: reflect recovery re-enters decision_router."""
    graph = build_deep_reasoning_graph()
    edges = getattr(graph, "edges", set())

    assert ("reflect", "decision_router") in edges


def test_deep_reasoning_tool_dispatch_remains_approval_gated() -> None:
    """Task 6.2 contract: DR tool execution stays behind approval flow."""
    graph = build_deep_reasoning_graph()
    edges = getattr(graph, "edges", set())

    assert ("select_categories", "prepare_tool_plan") in edges
    assert ("approval_gate", "dispatch_tool") in edges
    # Router authority chooses a branch, but never bypasses approval ownership.
    assert ("decision_router", "dispatch_tool") not in edges


def test_deep_reasoning_terminal_chain_remains_finalize_then_suffix() -> None:
    """Task 6.2 contract: DR terminal formatting chain remains stable."""
    graph = build_deep_reasoning_graph()
    edges = getattr(graph, "edges", set())

    assert ("finalize", "fallback_finalize") in edges


def test_route_decision_call_tool_dispatches_to_select_categories() -> None:
    state = _state_with(metadata={"router_outcome": {"action": "call_tool"}})
    assert _route_decision(state) == "select_categories"


def test_route_decision_reflect_dispatches_to_reflect() -> None:
    state = _state_with(metadata={"router_outcome": {"action": "reflect"}})
    assert _route_decision(state) == "reflect"


def test_route_decision_finalize_dispatches_to_finalize() -> None:
    state = _state_with(metadata={"router_outcome": {"action": "finalize"}})
    assert _route_decision(state) == "finalize"


def test_route_decision_unknown_action_falls_back_to_finalize() -> None:
    state = _state_with(metadata={"router_outcome": {"action": "unknown"}})
    assert _route_decision(state) == "finalize"


def test_route_decision_missing_outcome_falls_back_to_finalize() -> None:
    state = _state_with(metadata={})
    assert _route_decision(state) == "finalize"
