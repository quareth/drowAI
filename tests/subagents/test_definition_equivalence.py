"""Contract tests for the built-in Pathfinder declarative subagent definition."""

from __future__ import annotations

from agent.config import AgentConfig
from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.runtime.model import SubagentToolBuilderPromptBuilder
from agent.subagents.runtime.profile import resolve_subagent_tool_profile
from backend.services.agent_runs.subagent_registry import get_subagent_registry
from core.prompts.tests._golden import assert_golden


_SUPPORTED_CATEGORY_TO_PROFILE_CAPABILITY = {
    "host_discovery": "host_discovery",
    "port_scanning": "port_scanning",
    "service_enumeration": "service_enumeration",
}


def _pathfinder_definition() -> SubagentDefinition:
    definitions = load_subagent_definitions()
    [pathfinder] = [
        definition for definition in definitions if definition.id == "pathfinder"
    ]
    return pathfinder


def test_pathfinder_definition_matches_control_plane_registry_metadata() -> None:
    definition = _pathfinder_definition()
    spec = get_subagent_registry().require("pathfinder")

    assert definition.id == "pathfinder"
    assert spec.name == "pathfinder"
    assert definition.display_name == spec.display_name
    assert definition.kind == spec.agent_kind
    assert definition.description == spec.purpose
    assert definition.ownership_boundary == spec.ownership_boundary
    assert definition.supported_task_categories == spec.supported_task_categories
    assert definition.excluded_task_categories == spec.excluded_task_categories
    assert definition.enabled == spec.enabled
    assert definition.max_active_runs_per_task == spec.max_active_runs_per_task
    assert definition.requires_resolved_target == spec.requires_resolved_target
    assert definition.icon == "pathfinder"


def test_pathfinder_definition_owns_current_tool_profile() -> None:
    definition = _pathfinder_definition()
    profile = resolve_subagent_tool_profile(definition, definition.tool_ids)

    assert set(definition.tool_ids) == {
        "information_gathering.network_discovery.fping",
        "information_gathering.network_discovery.nmap",
    }
    assert profile.tool_ids == definition.tool_ids
    assert profile.capabilities_for_tool(
        "information_gathering.network_discovery.fping"
    ) == ("host_discovery",)
    assert profile.capabilities_for_tool(
        "information_gathering.network_discovery.nmap"
    ) == ("port_scanning", "service_enumeration")
    assert {
        _SUPPORTED_CATEGORY_TO_PROFILE_CAPABILITY[category]
        for category in definition.supported_task_categories
    } == {"host_discovery", "port_scanning", "service_enumeration"}


def test_pathfinder_definition_matches_current_runtime_limits() -> None:
    definition = _pathfinder_definition()

    assert (
        definition.max_active_runs_per_task
        == get_subagent_registry().require("pathfinder").max_active_runs_per_task
    )
    assert (
        definition.max_tool_calls_per_iteration
        == AgentConfig().max_committed_tools_per_batch
    )
    assert definition.max_iterations == 3


def test_pathfinder_definition_matches_current_prompt_sections() -> None:
    definition = _pathfinder_definition()

    prompt = SubagentToolBuilderPromptBuilder(definition).build_system_prompt(
        max_committed_tools_per_batch=definition.max_tool_calls_per_iteration,
    )

    assert_golden("subagent_tool_builder__system.txt", prompt)
    assert prompt.startswith(
        f"You are {definition.display_name}, a bounded recon subagent.\n"
        "Use native tool calls when more evidence is needed; otherwise return a "
        "concise parent handoff."
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
