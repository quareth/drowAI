"""Scout native tool-batch builder for the recon subagent pilot.

The legacy Scout node API remains the production-wired entry point while the
definition-parameterized runtime owns the native model-builder behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import InteractiveState
from agent.graph.utils.llm_resolver import resolve_llm_client
from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.runtime import model as runtime_model
from agent.subagents.runtime.model import (
    SUBAGENT_ACTION_METADATA_KEY,
    SUBAGENT_EXECUTION_STRATEGY_KEY,
    SUBAGENT_RESULT_METADATA_KEY,
    SubagentActionSelectionError,
)


SCOUT_ACTION_METADATA_KEY = SUBAGENT_ACTION_METADATA_KEY
SCOUT_RESULT_METADATA_KEY = SUBAGENT_RESULT_METADATA_KEY
SCOUT_EXECUTION_STRATEGY_KEY = SUBAGENT_EXECUTION_STRATEGY_KEY
ScoutActionSelectionError = SubagentActionSelectionError


async def choose_scout_action(
    state: Mapping[str, Any] | InteractiveState,
    context: GraphRuntimeContext | None = None,
    config: Mapping[str, Any] | None = None,
    writer: Any = None,
) -> dict[str, Any]:
    """Build one bounded Scout tool batch from all visible Scout tools."""

    return await runtime_model.choose_subagent_action(
        _pathfinder_definition(),
        state,
        context=context,
        config=config,
        writer=writer,
        llm_resolver=resolve_llm_client,
    )


def _pathfinder_definition() -> SubagentDefinition:
    for definition in load_subagent_definitions():
        if definition.id == "pathfinder":
            return definition
    raise RuntimeError("pathfinder subagent definition is not available")


__all__ = [
    "SCOUT_ACTION_METADATA_KEY",
    "SCOUT_EXECUTION_STRATEGY_KEY",
    "SCOUT_RESULT_METADATA_KEY",
    "ScoutActionSelectionError",
    "choose_scout_action",
]
