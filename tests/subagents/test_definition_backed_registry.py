"""Tests for the definition-backed generic subagent registry."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.registry import (
    SubagentRegistry,
    get_subagent_registry,
    load_subagent_registry,
)
from backend.services.agent_runs.subagent_registry import (
    get_subagent_registry as get_control_plane_subagent_registry,
)


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
    control_plane_projection = dict(
        get_control_plane_subagent_registry()
        .require("pathfinder")
        .classifier_projection()
    )
    [catalog_projection] = [dict(entry) for entry in registry.classifier_catalog()]

    assert control_plane_projection["name"] == "pathfinder"
    assert catalog_projection["name"] == "pathfinder"
    assert catalog_projection["agent_id"] == "pathfinder"
    for key in (
        "display_name",
        "purpose",
        "ownership_boundary",
        "supported_task_categories",
        "excluded_task_categories",
        "max_active_runs_per_task",
        "requires_resolved_target",
    ):
        assert catalog_projection[key] == control_plane_projection[key]


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
