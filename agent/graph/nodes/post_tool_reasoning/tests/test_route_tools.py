"""Tests for the PTR internal commit schema and completed-call parsing."""

from __future__ import annotations

import json

import pytest

from agent.providers.llm.core.base import ToolCall
from agent.graph.nodes.post_tool_reasoning.route_tools import (
    PTR_COMMIT_TOOL_NAME,
    PTRCommitError,
    build_post_tool_commit_tool,
    parse_post_tool_commit_call,
)
from agent.graph.nodes.post_tool_reasoning.models import (
    CandidateObservation,
    PostToolReasoningDecisionOutput,
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


def _call(arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        id="commit-1",
        name=PTR_COMMIT_TOOL_NAME,
        arguments=json.dumps(arguments),
    )


def test_commit_tool_uses_one_expanded_registry_scoped_schema() -> None:
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
    assert schema["properties"]["agent_handoff"]["properties"]["subagent"][
        "enum"
    ] == ["pathfinder", "cartographer"]
    assert schema["required"] == list(schema["properties"])


def test_provider_and_runtime_decision_fields_stay_aligned() -> None:
    """The provider function and Pydantic parser must expose the same fields."""
    schema = build_post_tool_commit_tool(("pathfinder",)).parameters_schema

    assert set(schema["properties"]) == set(
        PostToolReasoningDecisionOutput.model_fields
    )
    candidate_schema = schema["properties"]["candidate_observations"]["items"]
    assert set(candidate_schema["properties"]) == set(
        CandidateObservation.model_fields
    )


def test_completed_commit_call_maps_to_decision_contract() -> None:
    decision = parse_post_tool_commit_call([_call(_common_arguments())])

    assert decision.next_action == "finalize"
    assert decision.user_goal_achieved is True


@pytest.mark.parametrize(
    ("tool_calls", "code"),
    [
        (None, "missing_commit"),
        ([], "missing_commit"),
        (
            [
                ToolCall(id="1", name=PTR_COMMIT_TOOL_NAME, arguments="{}"),
                ToolCall(id="2", name=PTR_COMMIT_TOOL_NAME, arguments="{}"),
            ],
            "multiple_commits",
        ),
    ],
)
def test_commit_parser_classifies_missing_or_multiple_calls(
    tool_calls: object,
    code: str,
) -> None:
    with pytest.raises(PTRCommitError) as exc_info:
        parse_post_tool_commit_call(tool_calls)  # type: ignore[arg-type]
    assert exc_info.value.code == code


def test_commit_parser_identifies_truncated_json() -> None:
    with pytest.raises(PTRCommitError) as exc_info:
        parse_post_tool_commit_call(
            [
                ToolCall(
                    id="commit-1",
                    name=PTR_COMMIT_TOOL_NAME,
                    arguments='{"next_action":"finalize"',
                )
            ]
        )
    assert exc_info.value.code == "invalid_commit_json"
    assert "0 calls" not in str(exc_info.value)


def test_invalid_optional_candidate_is_dropped_without_losing_route() -> None:
    arguments = _common_arguments()
    arguments["candidate_observations"] = [
        {
            "observation_type": "asset.host",
            "subject_type": "host",
            "subject_key_hint": "127.0.0.1",
            "assertion_level": "candidate",
            "confidence": 4.0,
            "attributes": [],
            "rationale": "out-of-range confidence should not kill routing",
            "evidence_refs": [],
            "vulnerability": None,
            "vulnerability_confidence": None,
        }
    ]

    decision = parse_post_tool_commit_call([_call(arguments)])

    assert decision.next_action == "finalize"
    assert decision.candidate_observations == []


def test_invalid_optional_candidate_container_is_dropped() -> None:
    arguments = _common_arguments()
    arguments["candidate_observations"] = "not-an-array"

    decision = parse_post_tool_commit_call([_call(arguments)])

    assert decision.candidate_observations == []


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
def test_commit_parser_keeps_required_route_fields_strict(
    next_action: str,
    tool_intent: object,
    agent_handoff: object,
    message: str,
) -> None:
    arguments = _common_arguments()
    arguments.update(
        {
            "next_action": next_action,
            "tool_intent": tool_intent,
            "agent_handoff": agent_handoff,
        }
    )

    with pytest.raises(PTRCommitError, match=message):
        parse_post_tool_commit_call([_call(arguments)])
