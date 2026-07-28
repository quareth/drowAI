"""Scout graph initialization node.

The node validates child-run assignment metadata, binds the current
least-privilege Scout profile, and re-emits ordinary ``InteractiveState`` so
the shared graph nodes can run without backend service objects or runtime
handles in checkpoint state.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.state import InteractiveState
from agent.subagents.scout.profile import resolve_scout_tool_profile
from agent.subagents.scout.state import (
    ScoutRuntimeState,
    ScoutToolProfileState,
    apply_scout_state_to_interactive,
    scout_state_from_graph_state,
)


def initialize_scout_state(
    state: Mapping[str, Any] | InteractiveState,
    config: Mapping[str, Any] | None = None,
    *,
    context: GraphRuntimeContext | None = None,
) -> dict[str, Any]:
    """Validate and normalize Scout child state for downstream graph nodes."""

    _ = context
    interactive = InteractiveState.from_mapping(state)
    scout = scout_state_from_graph_state(interactive)
    _validate_config_thread(config, scout.graph_thread_id)

    profile = (
        scout.tool_profile
        if scout.tool_profile.tools
        else ScoutToolProfileState.from_profile(resolve_scout_tool_profile())
    )
    refreshed = ScoutRuntimeState.from_assignment(
        assignment=scout.assignment,
        graph_thread_id=scout.graph_thread_id,
        tool_profile=profile,
    )
    updated = apply_scout_state_to_interactive(interactive, refreshed)
    updated.trace.history.append(
        {
            "type": "scout_initialize",
            "agent_run_id": refreshed.agent_run_id,
            "agent_kind": refreshed.agent_kind,
            "tool_ids": list(refreshed.tool_profile.tool_ids),
        }
    )
    return updated.model_dump(mode="json")


def _validate_config_thread(
    config: Mapping[str, Any] | None,
    expected_graph_thread_id: str,
) -> None:
    if not isinstance(config, Mapping):
        return
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return
    thread_id = configurable.get("thread_id")
    if thread_id is None:
        return
    if expected_graph_thread_id not in _equivalent_thread_ids(thread_id):
        raise ValueError("Scout graph thread does not match assignment metadata")


def _equivalent_thread_ids(thread_id: Any) -> set[str]:
    normalized = str(thread_id).strip()
    if not normalized:
        return set()
    candidates = {normalized}
    if normalized.startswith("graph-"):
        candidates.add(normalized.removeprefix("graph-"))
    return candidates


__all__ = ["initialize_scout_state"]
