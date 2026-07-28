"""Scout graph completion node.

This module preserves the legacy Scout completion API while delegating terminal
result projection to the definition-parameterized subagent runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import InteractiveState
from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.runtime.complete import (
    SUBAGENT_COMPLETION_METADATA_KEY,
    SUBAGENT_RESULT_PROJECTION_METADATA_KEY,
    SubagentCompletionError,
    complete_subagent_result,
)


SCOUT_COMPLETION_METADATA_KEY = SUBAGENT_COMPLETION_METADATA_KEY
SCOUT_RESULT_PROJECTION_METADATA_KEY = SUBAGENT_RESULT_PROJECTION_METADATA_KEY
ScoutCompletionError = SubagentCompletionError


def complete_scout_result(
    state: Mapping[str, Any] | InteractiveState,
    context: GraphRuntimeContext | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate or derive Scout's terminal ``AgentResult`` and finalize state."""

    return complete_subagent_result(
        _pathfinder_definition(),
        state,
        context=context,
        config=config,
    )


def _pathfinder_definition() -> SubagentDefinition:
    for definition in load_subagent_definitions():
        if definition.id == "pathfinder":
            return definition
    raise RuntimeError("pathfinder subagent definition is not available")


__all__ = [
    "SCOUT_COMPLETION_METADATA_KEY",
    "SCOUT_RESULT_PROJECTION_METADATA_KEY",
    "ScoutCompletionError",
    "complete_scout_result",
]
