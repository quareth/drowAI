"""Tests for the PTR internal commit schema and completed-call parsing."""

from __future__ import annotations

import json

import pytest

from agent.providers.llm.core.base import ToolCall

from agent.graph.nodes.post_tool_reasoning.route_tools import (
    PTR_COMMIT_TOOL_NAME,
    build_post_tool_commit_tool,
    parse_post_tool_commit_call,
)


def _common_arguments() -> dict[str, object]:
    return {
        "next_action": "finalize",
        "action_reasoning": "The delegated scan resolved the requested check.",
        "tool_intent": None,
        "user_goal_achieved": True,
        "todo_progress": [],
        "effective_next_goal": None,
        "failure_detected": False,
        "failure_category": None,
        "retry_suggested": False,
        "candidate_observations": None,
        "agent_handoff": None,
        "par_irrelevant_active_agent_run_ids": [],
    }


def test_commit_tool_uses_one_expanded_registry_scoped_schema() -> None:
    """One function should carry every route and the registered-agent enum."""
    tool = build_post_tool_commit_tool(("pathfinder", "cartographer"))
    schema = tool.parameters_schema

    assert tool.name == PTR_COMMIT_TOOL_NAME
    assert schema["properties"]["next_action"]["enum"] == [
        "call_tool",
        "think_more",
        "reflect",
        "finalize",
        "delegate_subagent",
        "wait_for_subagents",
    ]
    assert "tool_intent" in schema["properties"]
    assert schema["properties"]["agent_handoff"]["properties"]["subagent"]["enum"] == [
        "pathfinder",
        "cartographer",
    ]
    assert schema["required"] == list(schema["properties"])


def test_completed_commit_call_maps_to_existing_decision_contract() -> None:
    """A completed commit function should become the existing router payload."""
    decision = parse_post_tool_commit_call(
        [
            ToolCall(
                id="commit-1",
                name=PTR_COMMIT_TOOL_NAME,
                arguments=json.dumps(_common_arguments()),
            )
        ]
    )

    assert decision.next_action == "finalize"
    assert decision.user_goal_achieved is True
    assert decision.tool_intent is None


@pytest.mark.parametrize("tool_calls", [None, [], [
    ToolCall(id="1", name=PTR_COMMIT_TOOL_NAME, arguments="{}"),
    ToolCall(id="2", name=PTR_COMMIT_TOOL_NAME, arguments="{}"),
]])
def test_commit_parser_rejects_missing_or_multiple_calls(tool_calls: object) -> None:
    """PTR must commit exactly one route after the text stream completes."""
    with pytest.raises(ValueError, match="exactly one"):
        parse_post_tool_commit_call(tool_calls)  # type: ignore[arg-type]


def test_commit_parser_rejects_unknown_internal_function() -> None:
    """Only the canonical commit function may control PTR routing."""
    with pytest.raises(ValueError, match="Unknown PTR commit tool"):
        parse_post_tool_commit_call(
            [
                ToolCall(
                    id="route-1",
                    name="ptr_finalize",
                    arguments=json.dumps(_common_arguments()),
                )
            ]
        )


@pytest.mark.parametrize(
    ("next_action", "tool_intent", "agent_handoff", "message"),
    [
        ("call_tool", None, None, "tool_intent is required"),
        (
            "finalize",
            {"description": "unexpected", "target": None, "focus": None},
            None,
            "tool_intent is only allowed",
        ),
        ("delegate_subagent", None, None, "agent_handoff is required"),
    ],
)
def test_commit_parser_rejects_action_payload_mismatches(
    next_action: str,
    tool_intent: object,
    agent_handoff: object,
    message: str,
) -> None:
    """Application validation should enforce unified-schema route pairings."""
    arguments = _common_arguments()
    arguments.update(
        {
            "next_action": next_action,
            "tool_intent": tool_intent,
            "agent_handoff": agent_handoff,
        }
    )

    with pytest.raises(ValueError, match=message):
        parse_post_tool_commit_call(
            [
                ToolCall(
                    id="commit-1",
                    name=PTR_COMMIT_TOOL_NAME,
                    arguments=json.dumps(arguments),
                )
            ]
        )
