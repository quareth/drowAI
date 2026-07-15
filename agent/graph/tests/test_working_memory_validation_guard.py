"""Focused tests for non-blocking working-memory routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.graph.builders.simple_tool_builder import build_simple_tool_graph
from agent.graph.state import InteractiveInput, InteractiveState


@pytest.mark.asyncio
async def test_simple_tool_graph_does_not_block_tool_path_when_validation_not_ready() -> None:
    def _classify(state, context=None):  # noqa: ANN001
        interactive = InteractiveState.from_mapping(state)
        interactive.facts.capability = "simple_tool_execution"
        return interactive.as_graph_update()

    def _update_working_memory(state, context=None):  # noqa: ANN001
        interactive = InteractiveState.from_mapping(state)
        metadata = interactive.facts.metadata or {}
        metadata["working_memory"] = {
            "validation": {
                "is_ready": False,
                "missing": [
                    {"code": "missing_target_handle", "message": "Please specify which target to use."}
                ],
                "errors": [],
            },
            "open_questions": [],
        }
        interactive.facts.metadata = metadata
        return interactive.as_graph_update()

    select_tool_categories_mock = AsyncMock(side_effect=RuntimeError("select_categories_reached"))
    payload = InteractiveInput(task_id=1, message="scan the host", conversation_id="c1", metadata={})
    graph = build_simple_tool_graph()

    with patch("agent.graph.builders.simple_tool_builder.classify_turn", side_effect=_classify), patch(
        "agent.graph.builders.simple_tool_builder.update_working_memory_node",
        side_effect=_update_working_memory,
    ), patch(
        "agent.graph.builders.simple_tool_builder.select_tool_categories_node",
        new=select_tool_categories_mock,
    ):
        with pytest.raises(RuntimeError, match="select_categories_reached"):
            async for _event in graph.astream(
                payload.to_state().as_graph_state(),
                {"configurable": {"thread_id": "wm-validation-guard"}},
                stream_mode="values",
            ):
                pass

    assert select_tool_categories_mock.await_count == 1
