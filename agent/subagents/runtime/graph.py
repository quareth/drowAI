"""Definition-configured subagent LangGraph builder.

Purpose
-------
Wire the generic child graph from a declarative subagent definition while
reusing the existing shared working-memory, approval, tool execution,
compact synthesis, and checkpoint infrastructure.

Responsibility boundary
-----------------------
This module owns graph topology and definition-bound node adaptation only. It
does not import backend services, launch runs, authorize tasks, execute tools
outside the shared graph nodes, or choose between generic and legacy paths.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import Any

from langgraph.graph import END, StateGraph

from agent.graph.graph_names import GRAPH_NAME_SUBAGENT
from agent.graph.builders.common_edges import (
    with_interactive_state,
    wrap_with_context,
    wrap_with_context_async,
)
from agent.graph.infrastructure.graph_registry import (
    GraphRegistry,
    get_default_graph_registry,
    get_or_register_compiled_graph,
)
from agent.graph.infrastructure.state_models import GraphRuntimeContext
from agent.graph.nodes.tool_synthesizer import synthesize_tool_output
from agent.graph.persistence import get_default_checkpointer
from agent.graph.state import InteractiveState
from agent.graph.subgraphs.tool_execution import (
    approval_gate_node,
    dispatch_tool_execution_node,
)
from agent.subagents.definition import SubagentDefinition
from agent.subagents.runtime.complete import complete_subagent_result
from agent.subagents.runtime.model import (
    SUBAGENT_ACTION_METADATA_KEY,
    record_subagent_observation_and_budget,
    run_subagent_model_turn,
)
from agent.subagents.runtime.profile import resolve_subagent_tool_profile
from agent.subagents.runtime.state import (
    SubagentRuntimeState,
    SubagentToolProfileState,
    apply_subagent_state_to_interactive,
    subagent_state_from_graph_state,
)


def initialize_subagent_state(
    definition: SubagentDefinition,
    state: Mapping[str, Any] | InteractiveState,
    config: Mapping[str, Any] | None = None,
    *,
    context: GraphRuntimeContext | None = None,
) -> dict[str, Any]:
    """Validate and normalize a definition-configured child graph state."""

    _ = context
    interactive = InteractiveState.from_mapping(state)
    subagent = subagent_state_from_graph_state(interactive, definition=definition)
    _validate_config_thread(config, subagent.graph_thread_id)

    profile = (
        subagent.tool_profile
        if subagent.tool_profile.tools
        else SubagentToolProfileState.from_profile(
            resolve_subagent_tool_profile(definition)
        )
    )
    refreshed = SubagentRuntimeState.from_assignment(
        definition=definition,
        assignment=subagent.assignment,
        graph_thread_id=subagent.graph_thread_id,
        tool_profile=profile,
    )
    updated = apply_subagent_state_to_interactive(
        interactive,
        refreshed,
        definition=definition,
    )
    updated.trace.history.append(
        {
            "type": "subagent_initialize",
            "agent_run_id": refreshed.agent_run_id,
            "agent_id": refreshed.agent_id,
            "agent_kind": refreshed.agent_kind,
            "tool_ids": list(refreshed.tool_profile.tool_ids),
        }
    )
    return updated.model_dump(mode="json")


def _route_after_model(interactive: InteractiveState) -> str:
    """Route a model turn to native tool execution or handoff completion."""

    action = interactive.facts.safe_metadata.get(SUBAGENT_ACTION_METADATA_KEY)
    route = action.get("route") if isinstance(action, dict) else None
    if route == "tool":
        return "approval_gate"
    if route == "handoff":
        return "handoff"
    raise ValueError("Subagent model turn did not write a valid route")


def build_subagent_graph(
    definition: SubagentDefinition,
    *,
    checkpointer: Any | None = None,
    build_only: bool = False,
) -> Any:
    """Build a definition-configured subagent graph."""

    graph = StateGraph(dict)

    graph.add_node(
        "initialize",
        wrap_with_context(partial(initialize_subagent_state, definition)),
    )
    graph.add_node(
        "model",
        wrap_with_context_async(partial(run_subagent_model_turn, definition)),
    )
    graph.add_node(
        "approval_gate",
        wrap_with_context_async(approval_gate_node),
    )
    graph.add_node(
        "dispatch_tool",
        wrap_with_context_async(dispatch_tool_execution_node),
    )
    graph.add_node(
        "tool_synthesizer",
        wrap_with_context_async(synthesize_tool_output),
    )
    graph.add_node(
        "observation",
        wrap_with_context(
            partial(record_subagent_observation_and_budget, definition)
        ),
    )
    graph.add_node(
        "handoff",
        wrap_with_context(partial(complete_subagent_result, definition)),
    )

    graph.set_entry_point("initialize")
    graph.add_edge("initialize", "model")
    graph.add_conditional_edges(
        "model",
        with_interactive_state(_route_after_model),
        {
            "approval_gate": "approval_gate",
            "handoff": "handoff",
        },
    )
    graph.add_edge("approval_gate", "dispatch_tool")
    graph.add_edge("dispatch_tool", "tool_synthesizer")
    graph.add_edge("tool_synthesizer", "observation")
    graph.add_edge("observation", "model")
    graph.add_edge("handoff", END)

    if build_only:
        return graph
    return graph.compile(checkpointer=checkpointer or get_default_checkpointer())


def get_compiled_subagent_graph(
    definition: SubagentDefinition,
    *,
    registry: GraphRegistry | None = None,
) -> object:
    """Return the compiled generic subagent graph from the shared registry."""

    return get_or_register_compiled_graph(
        registry=registry or get_default_graph_registry(),
        name=graph_name_for_definition(definition),
        build_uncompiled=lambda: build_subagent_graph(definition, build_only=True),
        checkpointer_factory=get_default_checkpointer,
    )


def graph_name_for_definition(definition: SubagentDefinition) -> str:
    """Return the stable graph type name for definition-configured child graphs."""

    _ = definition
    return GRAPH_NAME_SUBAGENT


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
        raise ValueError("Subagent graph thread does not match assignment metadata")


def _equivalent_thread_ids(thread_id: Any) -> set[str]:
    normalized = str(thread_id).strip()
    if not normalized:
        return set()
    candidates = {normalized}
    if normalized.startswith("graph-"):
        candidates.add(normalized.removeprefix("graph-"))
    return candidates


__all__ = [
    "GRAPH_NAME_SUBAGENT",
    "build_subagent_graph",
    "get_compiled_subagent_graph",
    "graph_name_for_definition",
    "initialize_subagent_state",
]
