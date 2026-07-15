""": Simple tool HITL resume path tests.

Verifies that simple tool approved resume reaches dispatch directly
without replaying classification or category selection."""

from __future__ import annotations

import pytest

from agent.graph.builders.simple_tool_builder import build_simple_tool_graph


def test_simple_tool_approved_resume_reaches_dispatch_directly() -> None:
    """Simple tool graph: approval_gate -> dispatch_tool (no cycle back)."""
    graph = build_simple_tool_graph(build_only=True)
    edges = getattr(graph, "edges", set())

    # Approved path includes post-selection articulation step before approval.
    assert ("select_tool_categories", "prepare_tool_plan") in edges
    assert ("articulation", "approval_gate") in edges
    assert ("approval_gate", "dispatch_tool") in edges
    assert ("dispatch_tool", "tool_synthesizer") in edges

    # No edge from approval_gate back to select_tool_categories or classification
    approval_targets = {dst for (src, dst) in edges if src == "approval_gate"}
    assert approval_targets == {"dispatch_tool"}
    assert "select_tool_categories" not in approval_targets
    assert "classification" not in approval_targets


def test_simple_tool_flow_prepare_to_dispatch() -> None:
    """Tool path includes articulation before approval and dispatch."""
    graph = build_simple_tool_graph(build_only=True)
    edges = getattr(graph, "edges", set())

    assert ("select_tool_categories", "prepare_tool_plan") in edges
    assert ("articulation", "approval_gate") in edges

    approval_targets = {dst for (src, dst) in edges if src == "approval_gate"}
    assert approval_targets == {"dispatch_tool"}
