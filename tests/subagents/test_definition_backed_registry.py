"""Tests for the definition-backed generic subagent registry."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.contracts import (
    AgentAssignment,
    AgentResult,
    AgentRuntimeIdentity,
)
from agent.subagents.registry import (
    SubagentRegistry,
    get_subagent_registry,
    load_subagent_registry,
)
from agent.subagents.runtime.profile import resolve_subagent_tool_profile


def _pathfinder_definition() -> SubagentDefinition:
    [definition] = load_subagent_definitions()
    return definition


def test_default_registry_loads_enabled_pathfinder_from_toml() -> None:
    registry = get_subagent_registry()

    assert registry.ids() == ("pathfinder",)
    assert registry.get("PATHFINDER") == _pathfinder_definition()
    assert registry.get("unknown") is None
    assert registry.require("pathfinder").display_name == "Pathfinder"


def test_classifier_catalog_is_definition_backed_and_matches_legacy_metadata() -> None:
    registry = load_subagent_registry()
    definition = registry.require("pathfinder")
    [catalog_projection] = [dict(entry) for entry in registry.classifier_catalog()]

    assert catalog_projection["name"] == "pathfinder"
    assert catalog_projection["agent_id"] == definition.id
    assert catalog_projection["display_name"] == definition.display_name
    assert catalog_projection["purpose"] == definition.description
    assert catalog_projection["ownership_boundary"] == definition.ownership_boundary
    assert (
        catalog_projection["supported_task_categories"]
        == definition.supported_task_categories
    )
    assert (
        catalog_projection["excluded_task_categories"]
        == definition.excluded_task_categories
    )
    assert (
        catalog_projection["max_active_runs_per_task"]
        == definition.max_active_runs_per_task
    )
    assert (
        catalog_projection["requires_resolved_target"]
        == definition.requires_resolved_target
    )


def test_display_tool_and_limit_metadata_are_definition_projections() -> None:
    registry = load_subagent_registry()
    definition = _pathfinder_definition()

    display = registry.display_metadata("pathfinder")
    tools = registry.tool_metadata("pathfinder")
    limits = registry.limits("pathfinder")

    assert display.agent_id == definition.id
    assert display.display_name == definition.display_name
    assert display.description == definition.description
    assert display.icon == definition.icon
    assert tools.agent_id == definition.id
    assert tools.tool_ids == definition.tool_ids
    assert tools.supported_task_categories == definition.supported_task_categories
    assert tools.excluded_task_categories == definition.excluded_task_categories
    assert tools.ownership_boundary == definition.ownership_boundary
    assert limits.agent_id == definition.id
    assert limits.max_active_runs_per_task == definition.max_active_runs_per_task
    assert limits.max_iterations == definition.max_iterations
    assert limits.max_tool_calls_per_iteration == (
        definition.max_tool_calls_per_iteration
    )
    assert limits.requires_resolved_target == definition.requires_resolved_target

    with pytest.raises(FrozenInstanceError):
        display.display_name = "Changed"  # type: ignore[misc]


def test_availability_uses_enabled_state_and_task_local_limits() -> None:
    definition = _pathfinder_definition()
    disabled = replace(definition, id="disabled_pathfinder", enabled=False)
    registry = SubagentRegistry((disabled, definition))

    assert registry.ids() == ("pathfinder",)
    assert registry.is_available("pathfinder", active_runs_for_task=0) is True
    assert registry.is_available("pathfinder", active_runs_for_task=1) is False
    assert registry.is_available("disabled_pathfinder", active_runs_for_task=0) is False
    assert registry.is_available("unknown", active_runs_for_task=0) is False


def test_registry_rejects_duplicate_definition_ids() -> None:
    definition = _pathfinder_definition()

    with pytest.raises(ValueError, match="duplicate subagent definition id"):
        SubagentRegistry((definition, definition))


def test_non_recon_definition_flows_through_registry_profile_and_contracts(
    tmp_path: Path,
) -> None:
    definition_path = tmp_path / "web_mapper.toml"
    definition_path.write_text(
        """
schema_version = 1
id = "web_mapper"
display_name = "Web Mapper"
kind = "web_assessment"
description = "Map approved HTTP surfaces."
ownership_boundary = "Own HTTP probing only."
supported_task_categories = ["web_mapping"]
excluded_task_categories = ["exploitation"]
tool_ids = ["information_gathering.web_enumeration.http_request"]
enabled = true
max_active_runs_per_task = 1
max_iterations = 2
max_tool_calls_per_iteration = 2
requires_resolved_target = true
icon = "web_mapper"
instructions = "Probe only the assigned approved HTTP targets."

[capability_aliases]
http_probe = "web_mapping"
""".strip(),
        encoding="utf-8",
    )

    registry = load_subagent_registry(tmp_path)
    definition = registry.require("web_mapper")
    profile = resolve_subagent_tool_profile(definition, definition.tool_ids)
    runtime_identity = AgentRuntimeIdentity(
        tenant_id=7,
        task_id=42,
        workspace_id="task-42",
        runtime_placement_mode="runner",
        actor_type="user",
        actor_id="3",
        feature_flags={},
    )
    assignment = AgentAssignment(
        assignment_id="assign-web-1",
        agent_run_id="run-web-1",
        agent_id=definition.id,
        agent_kind=definition.kind,
        task_id=42,
        tenant_id=7,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        parent_graph_thread_id="parent-thread-1",
        objective="Map the approved HTTP endpoint.",
        targets=("https://example.test",),
        suggested_capabilities=profile.capabilities_for_tool(
            "information_gathering.web_enumeration.http_request"
        ),
        runtime_identity=runtime_identity,
    )
    result = AgentResult(
        agent_run_id=assignment.agent_run_id,
        agent_id=assignment.agent_id,
        agent_kind=assignment.agent_kind,
        outcome="completed",
        summary="The approved HTTP endpoint responded.",
    )

    assert definition.kind == "web_assessment"
    assert profile.tool_ids == (
        "information_gathering.web_enumeration.http_request",
        "shell.exec",
        "shell.write_stdin",
    )
    assert assignment.suggested_capabilities == ("web_mapping",)
    assert AgentAssignment.model_validate(assignment.model_dump()) == assignment
    assert AgentResult.model_validate(result.model_dump()) == result
