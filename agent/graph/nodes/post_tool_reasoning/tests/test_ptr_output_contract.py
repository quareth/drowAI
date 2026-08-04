"""Tests for the PTR structured output schema contract.

This module validates that post-tool reasoning outputs expose only
decision fields. Current-turn phase records are runtime-derived, so the
LLM-facing schema must not request a phase-memory payload.
"""

from __future__ import annotations

from importlib import import_module

import pytest
from pydantic import ValidationError

from agent.graph.nodes.post_tool_reasoning.models import (
    PostToolReasoningDecisionOutput,
    PostToolReasoningOutput,
)


DIRECT_TOOL_ACTIONS = ("call_tool", "think_more", "reflect", "finalize")
PAR_COORDINATION_ACTIONS = ("delegate_subagent", "wait_for_subagents")


def test_ptr_output_models_do_not_expose_phase_memory_field() -> None:
    """PTR output schemas must not ask the LLM to produce phase memory."""
    assert "phase_memory" not in PostToolReasoningOutput.model_fields
    assert "phase_memory" not in PostToolReasoningDecisionOutput.model_fields

    full_schema = PostToolReasoningOutput.model_json_schema()
    decision_schema = PostToolReasoningDecisionOutput.model_json_schema()
    assert "phase_memory" not in full_schema.get("properties", {})
    assert "phase_memory" not in decision_schema.get("properties", {})


def test_post_tool_reasoning_package_does_not_export_iteration_memory_payload() -> None:
    """Removed PTR phase-memory model is absent from package exports."""
    post_tool_reasoning = import_module("agent.graph.nodes.post_tool_reasoning")

    assert not hasattr(post_tool_reasoning, "IterationMemoryPayload")
    assert "IterationMemoryPayload" not in post_tool_reasoning.__all__


@pytest.mark.parametrize("action", DIRECT_TOOL_ACTIONS)
def test_ptr_decision_output_accepts_existing_direct_tool_actions(action: str) -> None:
    payload = {
        "next_action": action,
        "action_reasoning": f"{action} remains a supported direct-tool route.",
    }
    if action == "call_tool":
        payload["tool_intent"] = {
            "description": "Run a bounded follow-up check.",
            "target": "10.0.0.10",
            "focus": "open services",
        }

    output = PostToolReasoningDecisionOutput.model_validate(payload)

    assert output.next_action == action
    assert output.action_reasoning == payload["action_reasoning"]


def test_ptr_decision_output_rejects_unknown_action_values() -> None:
    with pytest.raises(ValidationError):
        PostToolReasoningDecisionOutput.model_validate(
            {
                "next_action": "restart_everything",
                "action_reasoning": "Not part of the PAR contract.",
            }
        )


def test_ptr_decision_output_requires_handoff_for_delegate_subagent() -> None:
    with pytest.raises(ValidationError):
        PostToolReasoningDecisionOutput.model_validate(
            {
                "next_action": "delegate_subagent",
                "action_reasoning": "Need a bounded child assignment.",
            }
        )


def test_ptr_decision_output_accepts_delegate_subagent_with_existing_entry_shape() -> None:
    output = PostToolReasoningDecisionOutput.model_validate(
        {
            "next_action": "delegate_subagent",
            "action_reasoning": "Need a bounded child assignment.",
            "agent_handoff": {
                "agent_handoff": "required",
                "subagent": "pathfinder",
                "objective": "Enumerate services on the approved target.",
            },
        }
    )

    assert output.next_action == "delegate_subagent"
    assert output.agent_handoff is not None
    assert output.agent_handoff.agent_handoff == "required"
    assert output.agent_handoff.subagent == "pathfinder"
    assert output.agent_handoff.objective == "Enumerate services on the approved target."


@pytest.mark.parametrize("action", (*DIRECT_TOOL_ACTIONS, "wait_for_subagents"))
def test_ptr_decision_output_rejects_handoff_for_non_delegation_actions(
    action: str,
) -> None:
    payload = {
        "next_action": action,
        "action_reasoning": f"{action} must not carry a delegation entry.",
        "agent_handoff": {
            "agent_handoff": "required",
            "subagent": "pathfinder",
            "objective": "Enumerate services on the approved target.",
        },
    }
    if action == "call_tool":
        payload["tool_intent"] = {
            "description": "Run a bounded follow-up check.",
            "target": "10.0.0.10",
            "focus": "open services",
        }

    with pytest.raises(ValidationError):
        PostToolReasoningDecisionOutput.model_validate(payload)


def test_ptr_decision_output_accepts_wait_for_subagents_without_handoff() -> None:
    output = PostToolReasoningDecisionOutput.model_validate(
        {
            "next_action": "wait_for_subagents",
            "action_reasoning": "Relevant child assignments are still running.",
        }
    )

    assert output.next_action == "wait_for_subagents"
    assert output.agent_handoff is None


def test_ptr_decision_output_validates_irrelevant_active_run_ids() -> None:
    output = PostToolReasoningDecisionOutput.model_validate(
        {
            "next_action": "finalize",
            "action_reasoning": "Only unrelated active assignments remain.",
            "par_irrelevant_active_agent_run_ids": [
                " run-irrelevant ",
                "run-irrelevant",
            ],
        }
    )

    assert output.par_irrelevant_active_agent_run_ids == ["run-irrelevant"]

    with pytest.raises(ValidationError):
        PostToolReasoningDecisionOutput.model_validate(
            {
                "next_action": "finalize",
                "action_reasoning": "Malformed ids must fail closed.",
                "par_irrelevant_active_agent_run_ids": [123],
            }
        )


def test_ptr_output_schema_exposes_existing_required_direct_tool_fields() -> None:
    full_schema = PostToolReasoningOutput.model_json_schema()
    decision_schema = PostToolReasoningDecisionOutput.model_json_schema()

    assert full_schema["required"] == [
        "observation",
        "next_action",
        "action_reasoning",
    ]
    assert decision_schema["required"] == ["next_action", "action_reasoning"]
    assert tuple(full_schema["properties"]["next_action"]["enum"]) == (
        *DIRECT_TOOL_ACTIONS,
        *PAR_COORDINATION_ACTIONS,
    )
    assert tuple(decision_schema["properties"]["next_action"]["enum"]) == (
        *DIRECT_TOOL_ACTIONS,
        *PAR_COORDINATION_ACTIONS,
    )
