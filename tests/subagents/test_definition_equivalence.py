"""Contract tests for the built-in Pathfinder declarative subagent definition."""

from __future__ import annotations

from dataclasses import replace

from agent.config import AgentConfig
from agent.subagents.definition import (
    SubagentDefinition,
    load_subagent_definitions,
    resolve_definition_capability,
)
from agent.subagents.runtime.profile import (
    resolve_subagent_tool_profile,
    subagent_capabilities_from_metadata,
)
from agent.subagents.registry import get_subagent_registry
from agent.tools.categories import ToolCategory
from agent.tools.enhanced_metadata import EnhancedToolMetadata, ToolCapability


_SUPPORTED_CATEGORY_TO_PROFILE_CAPABILITY = {
    "host_discovery": "host_discovery",
    "port_scanning": "port_scanning",
    "service_enumeration": "service_enumeration",
}
_PATHFINDER_CAPABILITY_ALIASES = {
    "discover_hosts": "host_discovery",
    "discovery": "host_discovery",
    "gather_info": "host_discovery",
    "host_discover": "host_discovery",
    "host_enumeration": "host_discovery",
    "host_recon": "host_discovery",
    "information_gathering": "host_discovery",
    "network_discovery": "host_discovery",
    "recon": "host_discovery",
    "reconnaissance": "host_discovery",
    "network_scan": "port_scanning",
    "network_scanning": "port_scanning",
    "port_discovery": "port_scanning",
    "port_enumeration": "port_scanning",
    "port_scan": "port_scanning",
    "scan_ports": "port_scanning",
    "enumerate_services": "service_enumeration",
    "service_detection": "service_enumeration",
    "service_discovery": "service_enumeration",
    "service_enum": "service_enumeration",
}


def _pathfinder_definition() -> SubagentDefinition:
    definitions = load_subagent_definitions()
    [pathfinder] = [
        definition for definition in definitions if definition.id == "pathfinder"
    ]
    return pathfinder


def test_pathfinder_definition_matches_control_plane_registry_metadata() -> None:
    definition = _pathfinder_definition()
    registered = get_subagent_registry().require("pathfinder")

    assert definition.id == "pathfinder"
    assert registered == definition
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


def test_pathfinder_definition_preserves_the_existing_capability_vocabulary() -> None:
    definition = _pathfinder_definition()

    assert dict(definition.capability_aliases) == _PATHFINDER_CAPABILITY_ALIASES
    for category in definition.supported_task_categories:
        assert resolve_definition_capability(definition, category) == category
    for alias, category in _PATHFINDER_CAPABILITY_ALIASES.items():
        assert resolve_definition_capability(definition, alias) == category


def test_platform_shell_restriction_remains_definition_independent() -> None:
    definition = replace(
        _pathfinder_definition(),
        id="command_runner",
        kind="automation",
        supported_task_categories=("command_execution",),
        tool_ids=("shell.execute",),
        capability_aliases=(),
    )
    metadata = EnhancedToolMetadata(
        tool_id="shell.execute",
        display_name="Execute shell command",
        category=ToolCategory.SHELL,
        capabilities=[
            ToolCapability(
                name="command_execution",
                description="Execute an arbitrary shell command.",
            )
        ],
    )

    assert subagent_capabilities_from_metadata(
        definition,
        "shell.execute",
        metadata,
    ) == ()


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


def test_pathfinder_definition_owns_runtime_prompt_sections() -> None:
    definition = _pathfinder_definition()

    assert definition.runtime_role_prompt == (
        "You are Pathfinder, a bounded recon subagent.\n"
        "Use native tool calls when more evidence is needed; otherwise return a "
        "concise parent handoff."
    )
    assert definition.runtime_boundary_rules == (
        "Use only the targets, objective, scope, and constraints in the "
        "assignment context.",
        "Do not exploit, authenticate, mutate files, run shells, manage "
        "agents, or request credentials.",
    )
