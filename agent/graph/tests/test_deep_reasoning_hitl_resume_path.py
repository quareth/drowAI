""": Deep reasoning HITL resume path tests.

Verifies that DR approved resume reaches dispatch directly without
replaying decision_router or category selection."""

from __future__ import annotations

from agent.graph.builders.deep_reasoning_builder import (
    _route_after_prepare_tool_plan,
    build_deep_reasoning_graph,
)
from agent.graph.state import FactsState, InteractiveState


def test_dr_approved_resume_reaches_dispatch_directly() -> None:
    """DR graph: approval_gate -> dispatch_tool (no cycle back to decision_router)."""
    graph = build_deep_reasoning_graph()
    edges = getattr(graph, "edges", set())

    # Approved path: prepare_tool_plan routes to approval_gate -> dispatch_tool.
    state = InteractiveState(
        facts=FactsState(task_id=1, message="test", metadata={})
    )
    assert _route_after_prepare_tool_plan(state) == "approval_gate"
    assert ("approval_gate", "dispatch_tool") in edges
    assert ("dispatch_tool", "tool_synthesizer") in edges

    # No edge from approval_gate back to decision_router or select_categories
    approval_targets = {dst for (src, dst) in edges if src == "approval_gate"}
    assert approval_targets == {"dispatch_tool"}
    assert "decision_router" not in approval_targets
    assert "select_categories" not in approval_targets


def test_dr_tool_path_has_no_decision_router_before_dispatch() -> None:
    """Tool execution path: select_categories -> prepare -> approval -> dispatch (no decision_router)."""
    graph = build_deep_reasoning_graph()
    edges = getattr(graph, "edges", set())

    # From normal prepare_tool_plan we go to approval_gate, not decision_router.
    state = InteractiveState(
        facts=FactsState(task_id=1, message="test", metadata={})
    )
    assert _route_after_prepare_tool_plan(state) == "approval_gate"

    # From approval_gate we go to dispatch_tool only
    approval_targets = {dst for (src, dst) in edges if src == "approval_gate"}
    assert approval_targets == {"dispatch_tool"}
