"""Simple tool HITL resume and interactive-continuation path tests."""

from __future__ import annotations

import pytest

from agent.graph.builders.simple_tool_builder import build_simple_tool_graph


def test_simple_tool_approved_resume_reaches_dispatch_directly() -> None:
    """The stable ordinary path retains its explicit approval and dispatch nodes."""
    graph = build_simple_tool_graph(build_only=True)
    edges = getattr(graph, "edges", set())

    # Approved path includes post-selection articulation before dispatch.
    assert ("select_tool_categories", "prepare_tool_plan") in edges
    assert ("articulation", "approval_gate") in edges
    assert ("approval_gate", "dispatch_tool") in edges
    assert ("tool_execution_session", "terminal_session_compressor") in edges
    assert ("terminal_session_compressor", "tool_synthesizer") in edges
    assert ("tool_synthesizer", "post_tool_reasoning") in edges
    assert "approval_gate" in graph.nodes
    assert "dispatch_tool" in graph.nodes


def test_simple_tool_flow_prepare_to_dispatch() -> None:
    """Tool path includes articulation before the unchanged approval boundary."""
    graph = build_simple_tool_graph(build_only=True)
    edges = getattr(graph, "edges", set())

    assert ("select_tool_categories", "prepare_tool_plan") in edges
    assert ("articulation", "approval_gate") in edges
    assert ("approval_gate", "dispatch_tool") in edges
