"""Tests for Scout child graph state initialization."""

from __future__ import annotations

import json

import pytest

from agent.graph.builders.common_edges import wrap_with_context
from agent.graph.context.builder import METADATA_CONTEXT_BUNDLE_KEY
from agent.graph.state import InteractiveState
from agent.subagents.contracts import AgentAssignment, AgentRuntimeIdentity
from agent.subagents.scout.nodes.initialize import initialize_scout_state
from agent.subagents.scout.profile import ScoutToolProfile, ScoutToolSpec
from agent.subagents.scout.state import (
    SCOUT_GRAPH_CAPABILITY,
    SCOUT_METADATA_KEY,
    ScoutRuntimeState,
    ScoutToolProfileState,
    build_scout_initial_state,
    scout_state_from_graph_state,
)


def _runtime_identity() -> AgentRuntimeIdentity:
    return AgentRuntimeIdentity(
        tenant_id=7,
        task_id=42,
        user_id=3,
        workspace_id="task-42",
        workspace_path="/workspace",
        runtime_placement_mode="runner",
        actor_type="user",
        actor_id="3",
        runner_id="runner-1",
        execution_site_id="site-1",
        provider="openai",
        model="gpt-5.2-mini",
        reasoning_effort="medium",
        feature_flags={},
        credential_ref={"provider": "openai", "credential_id": "cred-1"},
    )


def _assignment() -> AgentAssignment:
    return AgentAssignment(
        assignment_id="assign-1",
        agent_run_id="run-1",
        agent_id="pathfinder",
        agent_kind="recon",
        task_id=42,
        tenant_id=7,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        parent_graph_thread_id="parent-thread-1",
        objective="Map open services on the approved target.",
        targets=["10.0.0.10"],
        suggested_capabilities=["host_discovery", "port_scan"],
        scope_summary="Approved internal test host only.",
        relevant_context={"ticket": "ENG-123"},
        runtime_identity=_runtime_identity(),
    )


def _profile() -> ScoutToolProfile:
    return ScoutToolProfile(
        tools=(
            ScoutToolSpec(
                tool_id="information_gathering.network_discovery.nmap",
                display_name="Nmap",
                scout_capabilities=("port_scanning", "service_enumeration"),
            ),
        )
    )


def test_build_initial_state_is_json_serializable_and_preserves_identity() -> None:
    state = build_scout_initial_state(
        assignment=_assignment(),
        graph_thread_id="child-thread-1",
        tool_profile=_profile(),
    )

    dumped = json.dumps(state)
    assert "raw_tool_output" not in dumped
    assert "chain_of_thought" not in dumped

    interactive = InteractiveState.from_mapping(state)
    assert interactive.facts.task_id == 42
    assert interactive.facts.conversation_id == "conversation-1"
    assert interactive.facts.message == "Map open services on the approved target."
    assert interactive.facts.capability == SCOUT_GRAPH_CAPABILITY
    assert interactive.facts.metadata["agent_run_id"] == "run-1"
    assert interactive.facts.metadata["agent_kind"] == "recon"
    assert interactive.facts.metadata["agent_display_name"] == "Pathfinder"
    assert interactive.facts.metadata["parent_turn_id"] == "turn-1"
    assert interactive.facts.metadata["parent_graph_thread_id"] == "parent-thread-1"
    assert interactive.facts.metadata["graph_thread_id"] == "child-thread-1"
    assert interactive.facts.metadata["producer_type"] == "subagent"
    assert interactive.facts.metadata["lifecycle_version"] == 1
    runtime_context = interactive.facts.metadata["graph_runtime_context"]
    assert runtime_context["tenant_id"] == 7
    assert runtime_context["graph_thread_id"] == "child-thread-1"
    assert runtime_context["turn_id"] == "turn-1"
    assert "credential_ref" not in runtime_context
    context_bundle = interactive.facts.metadata[METADATA_CONTEXT_BUNDLE_KEY]
    assert context_bundle["conversation_id"] == "conversation-1"
    assert context_bundle["turn_id"] == "turn-1"
    assert context_bundle["current_user_turn"] == {
        "role": "user",
        "content": "Map open services on the approved target.",
    }

    scout = scout_state_from_graph_state(interactive)
    assert scout.assignment == _assignment()
    assert scout.runtime_identity == _runtime_identity()
    assert scout.tool_profile.tool_ids == (
        "information_gathering.network_discovery.nmap",
    )


def test_scout_metadata_survives_shared_interactive_state_round_trip() -> None:
    state = build_scout_initial_state(
        assignment=_assignment(),
        graph_thread_id="child-thread-1",
        tool_profile=_profile(),
    )

    round_tripped = InteractiveState.from_mapping(state).as_graph_update()

    assert SCOUT_METADATA_KEY in round_tripped["facts"]["metadata"]
    assert (
        round_tripped["facts"]["metadata"][SCOUT_METADATA_KEY]["runtime_identity"][
            "workspace_id"
        ]
        == "task-42"
    )


def test_initialize_node_binds_profile_and_keeps_plain_graph_update() -> None:
    state = build_scout_initial_state(
        assignment=_assignment(),
        graph_thread_id="child-thread-1",
        tool_profile=_profile(),
    )

    update = initialize_scout_state(
        state,
        config={"configurable": {"thread_id": "child-thread-1"}},
    )

    json.dumps(update)
    assert update["facts"]["tool_ids"] == [
        "information_gathering.network_discovery.nmap"
    ]
    assert update["facts"]["tool_candidates"] == [
        "information_gathering.network_discovery.nmap"
    ]
    assert update["facts"]["metadata"]["agent_run_id"] == "run-1"
    assert update["trace"]["history"][-1] == {
        "type": "scout_initialize",
        "agent_run_id": "run-1",
        "agent_id": "pathfinder",
        "agent_kind": "recon",
        "tool_ids": ["information_gathering.network_discovery.nmap"],
    }


def test_initialize_node_accepts_shared_graph_wrapper_context() -> None:
    state = build_scout_initial_state(
        assignment=_assignment(),
        graph_thread_id="child-thread-1",
        tool_profile=_profile(),
    )

    update = wrap_with_context(initialize_scout_state)(
        state,
        config={"configurable": {"thread_id": "child-thread-1"}},
    )

    assert update["facts"]["metadata"]["agent_run_id"] == "run-1"
    assert update["trace"]["history"][-1]["type"] == "scout_initialize"


def test_initialize_node_rejects_mismatched_child_thread() -> None:
    state = build_scout_initial_state(
        assignment=_assignment(),
        graph_thread_id="child-thread-1",
        tool_profile=_profile(),
    )

    with pytest.raises(ValueError, match="thread"):
        initialize_scout_state(
            state,
            config={"configurable": {"thread_id": "other-thread"}},
        )


def test_scout_runtime_state_rejects_runtime_identity_drift() -> None:
    assignment = _assignment()
    payload = ScoutRuntimeState.from_assignment(
        assignment=assignment,
        graph_thread_id="child-thread-1",
        tool_profile=ScoutToolProfileState.from_profile(_profile()),
    ).model_dump(mode="json")
    payload["runtime_identity"]["task_id"] = 999

    with pytest.raises(ValueError, match="runtime_identity"):
        ScoutRuntimeState.model_validate(payload)
