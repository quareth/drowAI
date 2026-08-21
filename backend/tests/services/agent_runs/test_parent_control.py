"""Tests for decoding parent graph output into coordination actions."""

from __future__ import annotations

import pytest

from backend.services.agent_runs.parent_control import parse_parent_control_outcome


def test_missing_or_unrecognized_control_finishes_parent() -> None:
    missing = parse_parent_control_outcome(
        {},
        parent_turn_id="turn-1",
        claimed_agent_run_ids=("run-1",),
    )
    unrecognized = parse_parent_control_outcome(
        {"router_outcome": {"action": "use_tool"}},
        parent_turn_id="turn-1",
        claimed_agent_run_ids=("run-1",),
    )

    assert missing.action == "finish"
    assert unrecognized.action == "finish"


def test_wait_control_preserves_explicit_candidate_identity() -> None:
    outcome = parse_parent_control_outcome(
        {
            "router_outcome": {
                "action": "wait_for_subagents",
                "candidate_id": "candidate-1",
            }
        },
        parent_turn_id="turn-1",
        claimed_agent_run_ids=("run-1",),
    )

    assert outcome.action == "wait_for_subagents"
    assert outcome.decision_id == "candidate-1"
    assert outcome.agent_handoff is None


def test_delegate_control_normalizes_handoff_and_preserves_identity() -> None:
    outcome = parse_parent_control_outcome(
        {
            "candidate_decision": {
                "next_action": "delegate subagent",
                "decision_id": "decision-1",
                "agent_handoff": {
                    "agent_handoff": "required",
                    "subagent": "pathfinder",
                    "objective": "Inspect the unresolved HTTPS evidence.",
                    "skill_ids": ["network-reconnaissance"],
                },
            }
        },
        parent_turn_id="turn-1",
        claimed_agent_run_ids=("run-1",),
    )

    assert outcome.action == "delegate_subagent"
    assert outcome.decision_id == "decision-1"
    assert outcome.agent_handoff is not None
    assert outcome.agent_handoff["subagent"] == "pathfinder"
    assert outcome.agent_handoff["objective"] == (
        "Inspect the unresolved HTTPS evidence."
    )


def test_derived_decision_identity_is_stable_and_claim_scoped() -> None:
    metadata = {
        "parent_control_outcome": {
            "action": "wait_for_subagents",
        }
    }
    first = parse_parent_control_outcome(
        metadata,
        parent_turn_id="turn-1",
        claimed_agent_run_ids=("run-1",),
    )
    replay = parse_parent_control_outcome(
        metadata,
        parent_turn_id="turn-1",
        claimed_agent_run_ids=("run-1",),
    )
    other_claim = parse_parent_control_outcome(
        metadata,
        parent_turn_id="turn-1",
        claimed_agent_run_ids=("run-2",),
    )

    assert first.decision_id == replay.decision_id
    assert first.decision_id.startswith("par-decision-")
    assert first.decision_id != other_claim.decision_id


def test_delegate_control_requires_one_valid_handoff() -> None:
    with pytest.raises(
        RuntimeError,
        match="PAR delegate_subagent outcome missing agent_handoff",
    ):
        parse_parent_control_outcome(
            {"router_outcome": {"action": "delegate_subagent"}},
            parent_turn_id="turn-1",
            claimed_agent_run_ids=("run-1",),
        )
