"""Least-privilege tool profile for the Scout recon subagent.

This module preserves the legacy Scout profile API while delegating resolution
to the definition-parameterized subagent runtime. It does not execute tools,
mutate runtime state, or authorize targets.
"""

from __future__ import annotations

from typing import Any

from agent.subagents.contracts import ReconCapability
from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.runtime.profile import (
    SubagentToolProfile,
    SubagentToolSpec,
    is_subagent_tool_allowed,
    resolve_subagent_tool_ids,
    resolve_subagent_tool_profile,
    subagent_capabilities_from_metadata,
)
from agent.tools.enhanced_metadata import EnhancedToolMetadata


SCOUT_RECON_TOOL_ID_CEILING: frozenset[str] = frozenset(
    {
        "information_gathering.network_discovery.fping",
        "information_gathering.network_discovery.nmap",
    }
)
SCOUT_OWNED_CAPABILITIES: frozenset[ReconCapability] = frozenset(
    {"host_discovery", "port_scan", "service_enum"}
)

ScoutToolSpec = SubagentToolSpec
ScoutToolProfile = SubagentToolProfile


def resolve_scout_tool_profile(
    visible_tool_ids: Any = None,
) -> ScoutToolProfile:
    """Resolve the bounded Scout profile from visible registered metadata."""

    return resolve_subagent_tool_profile(_pathfinder_definition(), visible_tool_ids)


def resolve_scout_tool_ids(
    visible_tool_ids: Any = None,
) -> tuple[str, ...]:
    """Return Scout-visible tool ids for graph binding code."""

    return resolve_subagent_tool_ids(_pathfinder_definition(), visible_tool_ids)


def is_scout_tool_allowed(tool_id: Any) -> bool:
    """Return whether a tool id is currently allowed by the Scout profile."""

    return is_subagent_tool_allowed(_pathfinder_definition(), tool_id)


def scout_capabilities_from_metadata(
    tool_id: Any,
    metadata: EnhancedToolMetadata,
) -> tuple[ReconCapability, ...]:
    """Return normalized Scout capabilities only when metadata proves safety."""

    return subagent_capabilities_from_metadata(
        _pathfinder_definition(),
        tool_id,
        metadata,
    )


def _pathfinder_definition() -> SubagentDefinition:
    for definition in load_subagent_definitions():
        if definition.id == "pathfinder":
            return definition
    raise RuntimeError("pathfinder subagent definition is not available")


__all__ = [
    "SCOUT_OWNED_CAPABILITIES",
    "SCOUT_RECON_TOOL_ID_CEILING",
    "ScoutToolProfile",
    "ScoutToolSpec",
    "is_scout_tool_allowed",
    "resolve_scout_tool_ids",
    "resolve_scout_tool_profile",
    "scout_capabilities_from_metadata",
]
