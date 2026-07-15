"""Regression tests for deep-reasoning HITL plan preparation semantics.

These tests ensure the deep-reasoning graph routes through pre-planning before
tool execution and that `run_tool_execution` reuses prepared plan state.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.graph.builders.deep_reasoning_builder import (
    _route_after_prepare_tool_plan,
    build_deep_reasoning_graph,
)
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.state import FactsState, InteractiveState, TraceState
from agent.graph.subgraphs.tool_execution import run_tool_execution


def _empty_bundle() -> dict:
    """Bundle placeholder required by the Phase 5 hot-path authority cutover."""
    return build_conversation_context_bundle(
        conversation_id="conv-dr-hitl",
        turn_id="turn-dr-hitl",
        turn_sequence=0,
        messages=[],
    )


def _dr_state_with_prepared_plan() -> dict:
    planner_plan = {
        "selected_tools": ["shell.exec"],
        "tool_parameters": {"shell.exec": {"command": "echo ok"}},
        "execution_strategy": "sequential",
        "tool_batch": {
            "tool_batch_id": "batch-dr-hitl",
            "requested_execution_strategy": "sequential",
            "deferred_followups": [],
            "selection_rationale": "test prepared plan",
            "tool_calls": [
                {
                    "tool_call_id": "call-dr-hitl-1",
                    "tool_id": "shell.exec",
                    "parameters": {"command": "echo ok"},
                    "intent": "run prepared command",
                }
            ],
        },
    }
    facts = FactsState(
        task_id=1,
        message="run echo",
        capability="deep_reasoning",
        selected_tool="shell.exec",
        tool_parameters={"shell.exec": {"command": "echo ok"}},
        metadata={
            "agent_mode": "agent",
            "planner_plan": planner_plan,
            "tool_plan_prepared": True,
            "graph_runtime_context": {
                "task_id": 1,
                "tenant_id": 1,
                "runtime_placement_mode": "local",
                "workspace_id": "task-1",
                "actor_type": "system",
                "actor_id": "langgraph",
                "workspace_path": "/tmp",
            },
            METADATA_CONTEXT_BUNDLE_KEY: _empty_bundle(),
        },
    )
    return InteractiveState(facts=facts, trace=TraceState()).as_graph_state()


def _fake_outcome() -> SimpleNamespace:
    def _to_graph_metadata() -> dict:
        return {"tool_id": "shell.exec", "result": {"success": True}}

    return SimpleNamespace(
        tool_id="shell.exec",
        parameters={"command": "echo ok"},
        duration=1.0,
        result={
            "success": True,
            "status": "success",
            "stdout": "ok",
            "stdout_excerpt": "ok",
            "stderr": "",
            "stderr_excerpt": "",
            "observation": "ok",
            "duration": 1,
            "exit_code": 0,
        },
        catalog=[],
        reasoning=["Executed shell command"],
        summary="ok",
        to_graph_metadata=_to_graph_metadata,
    )


def test_deep_reasoning_graph_routes_call_tool_through_prepare_node() -> None:
    graph = build_deep_reasoning_graph()
    nodes = getattr(graph, "nodes", None) or {}
    edges = getattr(graph, "edges", set())

    assert "prepare_tool_plan" in nodes
    assert "approval_gate" in nodes
    assert "dispatch_tool" in nodes
    assert ("select_categories", "prepare_tool_plan") in edges
    assert (
        _route_after_prepare_tool_plan(
            InteractiveState(
                facts=FactsState(task_id=1, message="test", metadata={})
            )
        )
        == "approval_gate"
    )
    assert ("approval_gate", "dispatch_tool") in edges
    assert ("dispatch_tool", "tool_synthesizer") in edges
    assert ("select_categories", "call_tool") not in edges


@pytest.mark.asyncio
async def test_run_tool_execution_skips_replanning_for_prepared_dr_state() -> None:
    state = _dr_state_with_prepared_plan()
    with patch(
        "agent.graph.subgraphs.tool_execution._ensure_action_plan",
        new_callable=AsyncMock,
    ) as ensure_mock, patch(
        "agent.graph.subgraphs.tool_execution.get_stream_writer",
        return_value=None,
    ), patch(
        "agent.graph.subgraphs.tool_execution.should_require_approval",
        return_value=False,
    ), patch(
        "agent.graph.subgraphs.tool_execution.save_tool_output_artifact",
        return_value="",
    ), patch(
        "agent.graph.subgraphs.tool_execution.ToolExecutionCoordinator.run",
        new_callable=AsyncMock,
        return_value=_fake_outcome(),
    ):
        updated = await run_tool_execution(state)

    ensure_mock.assert_not_awaited()
    interactive = InteractiveState.from_mapping(updated)
    assert "tool_plan_prepared" not in (interactive.facts.metadata or {})
