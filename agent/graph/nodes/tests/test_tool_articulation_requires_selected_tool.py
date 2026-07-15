"""Contract test for simple-tool articulation ordering and ToolBatch context."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.graph.builders.simple_tool_builder import build_simple_tool_graph
from agent.graph.state import InteractiveInput, InteractiveState


def _classify_simple_tool(state, context=None):  # noqa: ANN001
    interactive = InteractiveState.from_mapping(state)
    interactive.facts.capability = "simple_tool_execution"
    return interactive.as_graph_update()


def _inject_ready_working_memory(state, context=None):  # noqa: ANN001
    interactive = InteractiveState.from_mapping(state)
    metadata = interactive.facts.metadata or {}
    metadata["working_memory"] = {
        "validation": {"is_ready": True, "missing": [], "errors": []},
        "open_questions": [],
    }
    interactive.facts.metadata = metadata
    return interactive.as_graph_update()


@pytest.mark.asyncio
async def test_articulation_runs_after_prepare_with_selected_tool_and_params() -> None:
    payload = InteractiveInput(task_id=1, message="scan host", conversation_id="c1", metadata={})
    articulation_seen = {"called": False}

    async def _prepare_tool_plan(state, context=None, config=None):  # noqa: ANN001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.metadata["planner_plan"] = {
            "tool_batch": {
                "tool_batch_id": "tb_test",
                "requested_execution_strategy": "sequential",
                "tool_calls": [
                    {
                        "tool_call_id": "tc_test",
                        "tool_id": "nmap",
                        "parameters": {"target": "10.0.0.1"},
                    }
                ],
            }
        }
        return interactive.as_graph_update()

    async def _articulate(state, context=None, config=None):  # noqa: ANN001
        interactive = InteractiveState.from_mapping(state)
        call = interactive.facts.metadata["planner_plan"]["tool_batch"]["tool_calls"][0]
        assert call["tool_id"] == "nmap"
        assert call["parameters"]["target"] == "10.0.0.1"
        interactive.facts.metadata["articulation_checked"] = True
        articulation_seen["called"] = True
        return interactive.as_graph_update()

    async def _post_tool_to_format_results(state, context=None, config=None, writer=None):  # noqa: ANN001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.decision_history = interactive.facts.decision_history or []
        interactive.facts.decision_history.append("format_results: done")
        return interactive.as_graph_update()

    async def _noop_async(state, **_kwargs):  # noqa: ANN001
        return InteractiveState.from_mapping(state).as_graph_update()

    def _noop_sync(state, **_kwargs):  # noqa: ANN001
        return InteractiveState.from_mapping(state).as_graph_update()

    with patch("agent.graph.builders.simple_tool_builder.classify_turn", side_effect=_classify_simple_tool), patch(
        "agent.graph.builders.simple_tool_builder.update_working_memory_node",
        side_effect=_inject_ready_working_memory,
    ), patch(
        "agent.graph.builders.simple_tool_builder.select_tool_categories_node",
        new=AsyncMock(side_effect=_noop_async),
    ), patch(
        "agent.graph.builders.simple_tool_builder.prepare_tool_execution_plan",
        new=AsyncMock(side_effect=_prepare_tool_plan),
    ), patch(
        "agent.graph.builders.simple_tool_builder.articulate_tool_intent",
        new=AsyncMock(side_effect=_articulate),
    ), patch(
        "agent.graph.builders.simple_tool_builder.approval_gate_node",
        new=AsyncMock(side_effect=_noop_async),
    ), patch(
        "agent.graph.builders.simple_tool_builder.dispatch_tool_execution_node",
        new=AsyncMock(side_effect=_noop_async),
    ), patch(
        "agent.graph.builders.simple_tool_builder.synthesize_tool_output",
        new=AsyncMock(side_effect=_noop_async),
    ), patch(
        "agent.graph.builders.simple_tool_builder.post_tool_reasoning",
        new=AsyncMock(side_effect=_post_tool_to_format_results),
    ), patch(
        "agent.graph.builders.simple_tool_builder.finalize_results",
        new=AsyncMock(side_effect=_noop_async),
    ), patch(
        "agent.graph.builders.simple_tool_builder.finalize_turn",
        side_effect=_noop_sync,
    ):
        graph = build_simple_tool_graph()
        final_state = None
        async for event in graph.astream(
            payload.to_state().as_graph_state(),
            {"configurable": {"thread_id": "articulation-order"}},
            stream_mode="values",
        ):
            final_state = event

    assert final_state is not None
    interactive = InteractiveState.from_mapping(final_state)
    assert articulation_seen["called"] is True
    assert interactive.facts.metadata.get("articulation_checked") is True
