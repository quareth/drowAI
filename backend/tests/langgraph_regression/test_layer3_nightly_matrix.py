"""Nightly-only expanded scenario matrix for LangGraph regression coverage."""

from __future__ import annotations

import pytest

pytestmark = [
    pytest.mark.regression_layer3,
    pytest.mark.regression_nightly,
]


@pytest.mark.parametrize("history_size", [0, 1, 5, 12])
@pytest.mark.parametrize("payload_size", [48, 512])
def test_nightly_history_prompt_matrix(
    history_size: int,
    payload_size: int,
    regression_harness,
) -> None:
    history = []
    for index in range(history_size):
        role = "user" if index % 2 == 0 else "assistant"
        history.append({"role": role, "content": f"turn-{index}-" + ("x" * payload_size)})

    prompt = regression_harness.build_planner_prompt(
        user_message="Continue based on previous context.",
        history=history,
    )
    metadata = regression_harness.build_history_metadata(
        message="Continue based on previous context.",
        history=history,
    )

    assert metadata["history_turns"] == history_size
    assert len(prompt) < 12000
    assert "DR Planner Input Brief" in prompt
    assert "Previous Conversation" not in prompt


@pytest.mark.parametrize(
    ("decision_history", "metadata", "expected_simple_route", "expected_deep_route"),
    [
        (
            ["call_tool: retry now"],
            {"failure_detected": True, "retry_suggested": True},
            "select_tool_categories",
            "select_categories",
        ),
        (
            ["call_tool: continue without retry flags"],
            {"failure_detected": False, "retry_suggested": False},
            "select_tool_categories",
            "select_categories",
        ),
        (
            ["finalize: enough information"],
            {},
            "format_results",
            "finalize",
        ),
        (
            ["think_more: need another reasoning pass"],
            {},
            "think_more",
            "think_more",
        ),
    ],
)
def test_nightly_tool_route_matrix(
    decision_history,
    metadata,
    expected_simple_route: str,
    expected_deep_route: str,
    regression_harness,
) -> None:
    simple_route = regression_harness.route_simple_tool_decision(
        decision_history=decision_history,
        metadata=metadata,
    )
    deep_route = regression_harness.route_deep_reasoning_decision(
        decision_history=decision_history,
        metadata=metadata,
    )

    assert simple_route == expected_simple_route
    assert deep_route == expected_deep_route
