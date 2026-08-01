"""Tests for generic subagent-run event projection helpers."""

from __future__ import annotations

import pytest

from agent.subagents.registry import get_subagent_registry
from backend.services.agent_runs.contracts import AgentAssignment, AgentRuntimeIdentity
from backend.services.agent_runs.event_projection import (
    apply_agent_run_metadata,
    build_agent_run_lifecycle_event,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.tests.agent_run_test_support import (
    build_agent_assignment,
    build_runtime_identity,
)


def _runtime_identity() -> AgentRuntimeIdentity:
    return build_runtime_identity(
        runner_id=None,
        execution_site_id=None,
        provider=None,
        model=None,
        reasoning_effort=None,
    )


def _assignment() -> AgentAssignment:
    return build_agent_assignment(
        assignment_id="assignment-1",
        agent_run_id="pathfinder-run-1",
        objective="Map open services.",
        suggested_capabilities=["port_scan"],
        runtime_identity=_runtime_identity(),
    )


@pytest.mark.asyncio
async def test_lifecycle_event_carries_agent_identity_in_metadata() -> None:
    registry = ProcessLocalAgentRunRegistry()
    entry = await registry.register(_assignment(), graph_thread_id="child-thread-1")

    event = build_agent_run_lifecycle_event(
        entry,
        display_metadata=get_subagent_registry().display_metadata(entry.agent_id),
        parent_run_id="parent-run-1",
    )

    metadata = event["metadata"]
    assert event["type"] == "status"
    assert event["content"] == "agent_run_lifecycle"
    assert metadata["producer_type"] == "subagent"
    assert metadata["agent_run_id"] == "pathfinder-run-1"
    assert metadata["agent_id"] == "pathfinder"
    assert metadata["agent_kind"] == "recon"
    assert metadata["agent_display_name"] == "Pathfinder"
    assert metadata["agent_icon_key"] == "pathfinder"
    assert metadata["parent_turn_id"] == "turn-1"
    assert metadata["parent_run_id"] == "parent-run-1"
    assert metadata["internal_only"] is False
    assert metadata["lifecycle_version"] == 1
    assert event["agent_run"]["assignment"]["agent_run_id"] == "pathfinder-run-1"
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


def test_apply_agent_run_metadata_preserves_boundary_display_metadata() -> None:
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
                "agent_run_id": "pathfinder-run-1",
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
    assert metadata["agent_display_name"] == "Spoofed"
    assert metadata["agent_icon_key"] == "spoofed"
