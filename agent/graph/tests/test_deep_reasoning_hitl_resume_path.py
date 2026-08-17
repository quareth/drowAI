"""Deep reasoning HITL resume and interactive-continuation path tests."""

from __future__ import annotations

from agent.graph.builders.deep_reasoning_builder import (
    _route_after_prepare_tool_plan,
    build_deep_reasoning_graph,
)
from agent.graph.state import FactsState, InteractiveState


def test_dr_approved_resume_reaches_dispatch_directly() -> None:
    """DR retains explicit approval and dispatch before optional continuation."""
    graph = build_deep_reasoning_graph()
    edges = getattr(graph, "edges", set())

    # The route label and target remain the original approval contract.
    state = InteractiveState(
        facts=FactsState(task_id=1, message="test", metadata={})
    )
    assert _route_after_prepare_tool_plan(state) == "approval_gate"
    assert ("approval_gate", "dispatch_tool") in edges
    assert ("tool_execution_session", "terminal_session_compressor") in edges
    assert ("terminal_session_compressor", "tool_synthesizer") in edges
    assert ("tool_synthesizer", "post_tool_reasoning") in edges
    assert "approval_gate" in graph.nodes
    assert "dispatch_tool" in graph.nodes


def test_dr_tool_path_has_no_decision_router_before_dispatch() -> None:
    """Tool execution reaches the session without decision-router replay."""
    graph = build_deep_reasoning_graph()
    edges = getattr(graph, "edges", set())

    # From normal prepare_tool_plan we go to approval_gate, not decision_router.
    state = InteractiveState(
        facts=FactsState(task_id=1, message="test", metadata={})
    )
    assert _route_after_prepare_tool_plan(state) == "approval_gate"
    assert ("approval_gate", "dispatch_tool") in edges

    assert ("tool_execution_session", "terminal_session_compressor") in edges
    assert ("terminal_session_compressor", "tool_synthesizer") in edges
    assert ("tool_synthesizer", "post_tool_reasoning") in edges
    assert ("decision_router", "tool_execution_session") not in edges
