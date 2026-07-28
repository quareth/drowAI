"""Tests for Scout agent-run event projection helpers."""

from __future__ import annotations

import pytest

from backend.services.agent_runs.contracts import AgentAssignment, AgentRuntimeIdentity
from backend.services.agent_runs.event_projection import (
    apply_agent_run_metadata,
    build_agent_run_lifecycle_event,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry


def _runtime_identity() -> AgentRuntimeIdentity:
    return AgentRuntimeIdentity(
        tenant_id=7,
        task_id=42,
        workspace_id="task-42",
        workspace_path="/workspace",
        runtime_placement_mode="runner",
        actor_type="user",
        actor_id="3",
    )


def _assignment() -> AgentAssignment:
    return AgentAssignment(
        assignment_id="assignment-1",
        agent_run_id="scout-run-1",
        agent_id="pathfinder",
        agent_kind="recon",
        task_id=42,
        tenant_id=7,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        parent_graph_thread_id="parent-thread-1",
        objective="Map open services.",
        targets=["10.0.0.10"],
        suggested_capabilities=["port_scan"],
        relevant_context={"ticket": "ENG-123"},
        runtime_identity=_runtime_identity(),
    )


@pytest.mark.asyncio
async def test_lifecycle_event_carries_agent_identity_in_metadata() -> None:
    registry = ProcessLocalAgentRunRegistry()
    entry = await registry.register(_assignment(), graph_thread_id="child-thread-1")

    event = build_agent_run_lifecycle_event(entry, parent_run_id="parent-run-1")

    metadata = event["metadata"]
    assert event["type"] == "status"
    assert event["content"] == "agent_run_lifecycle"
    assert metadata["producer_type"] == "subagent"
    assert metadata["agent_run_id"] == "scout-run-1"
    assert metadata["agent_id"] == "pathfinder"
    assert metadata["agent_kind"] == "recon"
    assert metadata["agent_display_name"] == "Pathfinder"
    assert metadata["agent_icon_key"] == "pathfinder"
    assert metadata["parent_turn_id"] == "turn-1"
    assert metadata["parent_run_id"] == "parent-run-1"
    assert metadata["internal_only"] is False
    assert metadata["lifecycle_version"] == 1
    assert event["agent_run"]["assignment"]["agent_run_id"] == "scout-run-1"
    assert event["agent_run"]["agent_icon_key"] == "pathfinder"


def test_apply_agent_run_metadata_preserves_generic_subagent_identity() -> None:
    processed: dict[str, object] = {"type": "reasoning_delta", "metadata": {}}

    apply_agent_run_metadata(
        processed,
        {
            "type": "reasoning_delta",
            "producer_type": "subagent",
            "agent_run_id": "review-run-1",
            "agent_kind": "review",
            "agent_icon_key": "reviewer",
            "parent_turn_id": "turn-parent",
        },
    )

    metadata = processed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["producer_type"] == "subagent"
    assert metadata["agent_run_id"] == "review-run-1"
    assert metadata["agent_kind"] == "review"
    assert metadata["agent_display_name"] == "Review"
    assert metadata["agent_icon_key"] == "reviewer"


def test_apply_agent_run_metadata_canonicalizes_registered_display_name() -> None:
    processed: dict[str, object] = {
        "type": "reasoning_delta",
        "metadata": {"agent_display_name": "Spoofed"},
    }

    apply_agent_run_metadata(
        processed,
        {
            "type": "reasoning_delta",
            "metadata": {
                "producer_type": "subagent",
                "agent_run_id": "scout-run-1",
                "agent_id": "pathfinder",
                "agent_kind": "recon",
                "agent_display_name": "Spoofed",
                "agent_icon_key": "spoofed",
                "parent_turn_id": "turn-parent",
            },
        },
    )

    metadata = processed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["agent_display_name"] == "Pathfinder"
    assert metadata["agent_icon_key"] == "pathfinder"
