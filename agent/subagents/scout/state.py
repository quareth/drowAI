"""Checkpoint-safe Scout graph state helpers.

This module preserves the legacy Scout state API while delegating graph-state
projection to the definition-parameterized subagent runtime. Shared graph nodes
continue to receive ordinary ``InteractiveState`` payloads.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.graph.state import InteractiveState
from agent.subagents.contracts import AgentAssignment
from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.runtime.profile import SubagentToolProfile
from agent.subagents.runtime.state import (
    SUBAGENT_GRAPH_CAPABILITY,
    SUBAGENT_METADATA_KEY,
    SubagentRuntimeState,
    SubagentToolProfileState,
    SubagentToolState,
    apply_subagent_state_to_interactive,
    build_subagent_initial_state,
    subagent_state_from_graph_state,
)


SCOUT_METADATA_KEY = SUBAGENT_METADATA_KEY
SCOUT_GRAPH_CAPABILITY = SUBAGENT_GRAPH_CAPABILITY
ScoutToolState = SubagentToolState
ScoutToolProfileState = SubagentToolProfileState
ScoutRuntimeState = SubagentRuntimeState


def build_scout_initial_state(
    *,
    assignment: AgentAssignment,
    graph_thread_id: str,
    tool_profile: SubagentToolProfile | SubagentToolProfileState | Any | None = None,
) -> dict[str, Any]:
    """Return an initial ``InteractiveState`` mapping for a Scout child run."""

    return build_subagent_initial_state(
        definition=_pathfinder_definition(),
        assignment=assignment,
        graph_thread_id=graph_thread_id,
        tool_profile=tool_profile,
    )


def scout_state_from_graph_state(
    state: Mapping[str, Any] | InteractiveState,
) -> ScoutRuntimeState:
    """Parse Scout metadata from an interactive graph state."""

    return subagent_state_from_graph_state(
        state,
        definition=_pathfinder_definition(),
    )


def apply_scout_state_to_interactive(
    interactive: InteractiveState,
    scout: ScoutRuntimeState,
) -> InteractiveState:
    """Attach Scout metadata and tool profile fields to an interactive state."""

    return apply_subagent_state_to_interactive(
        interactive,
        scout,
        definition=_pathfinder_definition(),
    )


def _pathfinder_definition() -> SubagentDefinition:
    for definition in load_subagent_definitions():
        if definition.id == "pathfinder":
            return definition
    raise RuntimeError("pathfinder subagent definition is not available")


__all__ = [
    "SCOUT_GRAPH_CAPABILITY",
    "SCOUT_METADATA_KEY",
    "ScoutRuntimeState",
    "ScoutToolProfileState",
    "ScoutToolState",
    "apply_scout_state_to_interactive",
    "build_scout_initial_state",
    "scout_state_from_graph_state",
]
