"""Focused tests for working-memory graph wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.graph.builders.deep_reasoning_builder import build_deep_reasoning_graph
from agent.graph.builders.simple_tool_builder import build_simple_tool_graph
from agent.graph.graph_builder import build_simple_chat_graph
from agent.graph.state import InteractiveInput, InteractiveState


def test_deep_reasoning_graph_wires_working_memory_after_classification() -> None:
    graph = build_deep_reasoning_graph()
    nodes = getattr(graph, "nodes", {}) or {}
    edges = getattr(graph, "edges", set())

    assert "update_working_memory" in nodes
    assert "memory_retrieval" in nodes
    assert ("update_working_memory", "memory_retrieval") in edges
    assert ("memory_retrieval", "clarify_gate") in edges
    assert "working_memory_validation_gate" not in nodes
    assert "working_memory_clarification" not in nodes


def test_simple_tool_graph_wires_working_memory_after_classification() -> None:
    graph = build_simple_tool_graph(build_only=True)
    nodes = getattr(graph, "nodes", {}) or {}
    edges = getattr(graph, "edges", set())

    assert "update_working_memory" in nodes
    assert "memory_retrieval" in nodes
    assert "working_memory_validation_gate" not in nodes
    assert "working_memory_clarification" not in nodes
    assert ("update_working_memory", "memory_retrieval") in edges
    assert ("memory_retrieval", "select_tool_categories") in edges


@pytest.mark.asyncio
async def test_simple_chat_graph_passes_working_memory_to_simple_chat_node() -> None:
    def _classify(state, context=None):  # noqa: ANN001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.capability = "respond_only"
        return interactive.as_graph_update()

    async def _simple_chat(state, context=None, config=None):  # noqa: ANN001
        interactive = InteractiveState.from_mapping(state)
        assert "working_memory" in interactive.facts.metadata
        interactive.trace.final_text = "ok"
        return interactive.as_graph_update()

    def _post_process(state, context=None):  # noqa: ANN001
        return InteractiveState.from_mapping(state).as_graph_update()

    def _finalize(state, context=None):  # noqa: ANN001
        return InteractiveState.from_mapping(state).as_graph_update()

    async def _memory_retrieval(state, context=None):  # noqa: ANN001
        return InteractiveState.from_mapping(state).as_graph_update()

    payload = InteractiveInput(task_id=1, message="hello", conversation_id="c1", metadata={})
    graph = build_simple_chat_graph()
    topology = graph.get_graph()
    nodes = set(getattr(topology, "nodes", {}).keys())
    edges = {
        (edge.source, edge.target) for edge in getattr(topology, "edges", []) if hasattr(edge, "source")
    }
    assert "memory_retrieval" in nodes
    assert ("update_working_memory", "memory_retrieval") in edges
    assert ("memory_retrieval", "simple_chat") in edges
    with patch("agent.graph.graph_builder.classify_node", side_effect=_classify), patch(
        "agent.graph.graph_builder.memory_retrieval_node", new=AsyncMock(side_effect=_memory_retrieval)
    ), patch(
        "agent.graph.graph_builder.run_simple_chat", new=AsyncMock(side_effect=_simple_chat)
    ), patch("agent.graph.graph_builder.post_process_simple_chat", side_effect=_post_process), patch(
        "agent.graph.graph_builder.finalize_turn", side_effect=_finalize
    ):
        final_state = None
        async for event in graph.astream(
            payload.to_state().as_graph_state(),
            {"configurable": {"thread_id": "wm-wiring-simple-chat"}},
            stream_mode="values",
        ):
            final_state = event

    assert final_state is not None
    interactive = InteractiveState.from_mapping(final_state)
    assert "working_memory" in interactive.facts.metadata
