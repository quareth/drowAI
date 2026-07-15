"""Layer 2 transition tests for routing and graph topology invariants."""

from __future__ import annotations

import pytest

from agent.graph.builders.deep_reasoning_builder import build_deep_reasoning_graph
from backend.services.langgraph_chat.contracts import ExecutionMode

pytestmark = [
    pytest.mark.regression_layer2,
    pytest.mark.regression_main,
    pytest.mark.regression_nightly,
]


@pytest.mark.regression_quick
@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        (ExecutionMode.NORMAL_CHAT, "normal_chat"),
        (ExecutionMode.SIMPLE_TOOL, "simple_tool_execution"),
        (ExecutionMode.DEEP_REASONING, "deep_reasoning"),
    ),
)
def test_select_branch_mode_mapping_contract(regression_harness, mode: ExecutionMode, expected: str) -> None:
    assert regression_harness.resolve_branch(mode) == expected


@pytest.mark.regression_quick
def test_simple_tool_post_tool_route_honors_bounded_continuation(regression_harness) -> None:
    retry_route = regression_harness.route_simple_tool_decision(
        decision_history=["call_tool: retry with alternate flags"],
        metadata={"failure_detected": True, "retry_suggested": True},
    )
    continuation_route = regression_harness.route_simple_tool_decision(
        decision_history=["call_tool: run another tool"],
        metadata={"failure_detected": False, "retry_suggested": False},
    )
    finalize_route = regression_harness.route_simple_tool_decision(
        decision_history=["finalize: enough evidence collected"],
        metadata={},
    )

    assert retry_route == "select_tool_categories"
    assert continuation_route == "select_tool_categories"
    assert finalize_route == "format_results"


@pytest.mark.regression_quick
def test_deep_reasoning_post_tool_route_contract(regression_harness) -> None:
    assert (
        regression_harness.route_deep_reasoning_decision(
            decision_history=["call_tool: continue probing"],
            metadata={},
        )
        == "select_categories"
    )
    assert (
        regression_harness.route_deep_reasoning_decision(
            decision_history=["think_more: need synthesis first"],
            metadata={},
        )
        == "think_more"
    )
    assert (
        regression_harness.route_deep_reasoning_decision(
            decision_history=["unknown_action"],
            metadata={},
        )
        == "finalize"
    )


def test_deep_reasoning_post_tool_conditional_map_accepts_route_output() -> None:
    graph = build_deep_reasoning_graph()
    branches = getattr(graph, "branches", {}) or {}

    post_tool_branch = branches["decision_router"]["_route_decision"]
    assert post_tool_branch.ends["select_categories"] == "select_categories"
    assert "call_tool" not in post_tool_branch.ends


def test_deep_reasoning_graph_prepare_tool_transition_exists() -> None:
    graph = build_deep_reasoning_graph()
    nodes = getattr(graph, "nodes", {}) or {}
    edges = getattr(graph, "edges", set())

    assert "prepare_tool_plan" in nodes
    assert ("select_categories", "prepare_tool_plan") in edges
    # DR flow: prepare_tool_plan -> approval_gate -> dispatch_tool (shared HITL contract)
    assert ("prepare_tool_plan", "approval_gate") in edges
    assert ("approval_gate", "dispatch_tool") in edges
    assert ("select_categories", "dispatch_tool") not in edges


def test_thread_config_interrupt_resume_contract(regression_harness) -> None:
    task_thread = regression_harness.make_thread_config(conversation_id=None)
    conv_thread = regression_harness.make_thread_config(conversation_id="conv-4242")
    anchored = regression_harness.make_thread_config(conversation_id=None, anchor_sequence=19)

    assert task_thread["configurable"]["thread_id"] == "graph-" + ("a" * 32)
    assert conv_thread["configurable"]["thread_id"] == "graph-" + ("a" * 32)
    assert anchored["configurable"]["checkpoint_id"] == "19"


@pytest.mark.regression_quick
def test_thread_config_rejects_mismatched_checkpoint_thread(regression_harness) -> None:
    with pytest.raises(RuntimeError, match="checkpoint thread_id does not match"):
        regression_harness.make_thread_config(
            conversation_id=None,
            metadata={"thread_config": {"configurable": {"thread_id": "task-77"}}}
        )
