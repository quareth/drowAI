"""Equivalence tests between the Pathfinder TOML definition and legacy Scout facts."""

from __future__ import annotations

from agent.config import AgentConfig
from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.scout.profile import (
    SCOUT_OWNED_CAPABILITIES,
    SCOUT_RECON_TOOL_ID_CEILING,
    resolve_scout_tool_profile,
)
from backend.services.agent_runs.subagent_registry import SCOUT_SUBAGENT_SPEC
from core.prompts.builders.scout_tool_builder import ScoutToolBuilderPromptBuilder
from core.prompts.tests._golden import assert_golden


_SUPPORTED_CATEGORY_TO_LEGACY_CAPABILITY = {
    "host_discovery": "host_discovery",
    "port_scanning": "port_scan",
    "service_enumeration": "service_enum",
}


def _pathfinder_definition() -> SubagentDefinition:
    definitions = load_subagent_definitions()
    [pathfinder] = [
        definition for definition in definitions if definition.id == "pathfinder"
    ]
    return pathfinder


def test_pathfinder_definition_matches_legacy_scout_registry_metadata() -> None:
    definition = _pathfinder_definition()
    legacy = SCOUT_SUBAGENT_SPEC

    assert definition.id == "pathfinder"
    assert legacy.name == "scout"
    assert definition.display_name == legacy.display_name
    assert definition.kind == legacy.agent_kind
    assert definition.description == legacy.purpose
    assert definition.ownership_boundary == legacy.ownership_boundary
    assert definition.supported_task_categories == legacy.supported_task_categories
    assert definition.excluded_task_categories == legacy.excluded_task_categories
    assert definition.enabled == legacy.enabled
    assert definition.max_active_runs_per_task == legacy.max_active_runs_per_task
    assert definition.requires_resolved_target == legacy.requires_resolved_target
    assert definition.icon == "pathfinder"


def test_pathfinder_definition_matches_current_scout_tool_profile() -> None:
    definition = _pathfinder_definition()
    profile = resolve_scout_tool_profile(definition.tool_ids)

    assert set(definition.tool_ids) == SCOUT_RECON_TOOL_ID_CEILING
    assert profile.tool_ids == definition.tool_ids
    assert profile.capabilities_for_tool(
        "information_gathering.network_discovery.fping"
    ) == ("host_discovery",)
    assert profile.capabilities_for_tool(
        "information_gathering.network_discovery.nmap"
    ) == ("port_scan", "service_enum")
    assert {
        _SUPPORTED_CATEGORY_TO_LEGACY_CAPABILITY[category]
        for category in definition.supported_task_categories
    } == set(SCOUT_OWNED_CAPABILITIES)


def test_pathfinder_definition_matches_current_scout_runtime_limits() -> None:
    definition = _pathfinder_definition()

    assert (
        definition.max_active_runs_per_task
        == SCOUT_SUBAGENT_SPEC.max_active_runs_per_task
    )
    assert (
        definition.max_tool_calls_per_iteration
        == AgentConfig().max_committed_tools_per_batch
    )
    assert definition.max_iterations == 3


def test_pathfinder_definition_matches_current_scout_prompt_sections() -> None:
    definition = _pathfinder_definition()

    prompt = ScoutToolBuilderPromptBuilder().build_system_prompt(
        max_committed_tools_per_batch=definition.max_tool_calls_per_iteration,
    )

    assert_golden("scout_tool_builder__system.txt", prompt)
    assert prompt.startswith(
        f"You are {definition.display_name}, a bounded recon subagent.\n"
        "Emit native tool calls only."
    )
    assert (
        f"Call between 1 and {definition.max_tool_calls_per_iteration} "
        "candidate tool function(s) for this iteration."
    ) in prompt
    assert (
        f"{definition.display_name} batch strategy metadata (`_execution_strategy`):"
        in prompt
    )
    assert (
        f"{definition.display_name} boundaries:\n"
        "- Use only the targets, objective, scope, and constraints in the "
        "assignment context.\n"
        "- Do not exploit, authenticate, mutate files, run shells, manage "
        "agents, or request credentials."
    ) in prompt
