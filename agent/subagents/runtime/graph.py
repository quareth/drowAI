"""Definition-configured subagent LangGraph builder.

Purpose
-------
Wire the generic child graph from a declarative subagent definition while
reusing the existing shared working-memory, approval, tool execution,
post-tool reasoning, routing, finalizer, and checkpoint infrastructure.

Responsibility boundary
-----------------------
This module owns graph topology and definition-bound node adaptation only. It
does not import backend services, launch runs, authorize tasks, execute tools
outside the shared graph nodes, or choose between generic and legacy paths.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from langgraph.graph import END, StateGraph

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
from agent.graph.nodes.decision_router import decision_router
from agent.graph.nodes.finalize import finalize_results
from agent.graph.nodes.memory_retrieval import memory_retrieval_node
from agent.graph.nodes.post_tool_reasoning.node import post_tool_reasoning
from agent.graph.nodes.reflect import reflect_node
from agent.graph.nodes.think_more import think_more_node
from agent.graph.nodes.tool_synthesizer import synthesize_tool_output
from agent.graph.nodes.working_memory import update_working_memory_node
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
    choose_subagent_action,
)
from agent.subagents.runtime.profile import resolve_subagent_tool_profile
from agent.subagents.runtime.state import (
    SubagentRuntimeState,
    SubagentToolProfileState,
    apply_subagent_state_to_interactive,
    subagent_state_from_graph_state,
)

logger = logging.getLogger(__name__)

GRAPH_NAME_SUBAGENT = "subagent"


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
            "type": "scout_initialize",
            "agent_run_id": refreshed.agent_run_id,
            "agent_id": refreshed.agent_id,
            "agent_kind": refreshed.agent_kind,
            "tool_ids": list(refreshed.tool_profile.tool_ids),
        }
    )
    return updated.model_dump(mode="json")


def _route_after_choose_action(interactive: InteractiveState) -> str:
    """Route a selected native call to execution or completion."""

    action = interactive.facts.safe_metadata.get(SUBAGENT_ACTION_METADATA_KEY)
    route = action.get("route") if isinstance(action, dict) else None
    if route == "tool":
        return "approval_gate"
    if route == "complete":
        return "complete"
    raise ValueError("Subagent choose_action did not write a valid route")


def _route_after_router(interactive: InteractiveState) -> str:
    """Route existing router outcomes into generic subagent graph destinations."""

    outcome = interactive.facts.safe_metadata.get("router_outcome")
    action = ""
    if isinstance(outcome, dict):
        action = str(outcome.get("action") or "").strip().lower()

    if action == "call_tool":
        return "choose_action"
    if action == "think_more":
        return "think_more"
    if action == "reflect":
        return "reflect"
    if action == "finalize":
        return "complete"

    logger.warning(
        "[SUBAGENT_GRAPH] Unknown router_outcome.action '%s'; completing subagent run",
        action,
    )
    return "complete"


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
        wrap_with_context(_bind_initialize(definition)),
    )
    graph.add_node(
        "update_working_memory",
        wrap_with_context(update_working_memory_node),
    )
    graph.add_node(
        "memory_retrieval",
        wrap_with_context_async(memory_retrieval_node),
    )
    graph.add_node(
        "choose_action",
        wrap_with_context_async(_bind_choose_action(definition)),
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
        "post_tool_reasoning",
        wrap_with_context_async(post_tool_reasoning),
    )
    graph.add_node(
        "decision_router",
        wrap_with_context_async(decision_router),
    )
    graph.add_node(
        "think_more",
        wrap_with_context_async(think_more_node),
    )
    graph.add_node(
        "reflect",
        wrap_with_context_async(reflect_node),
    )
    graph.add_node(
        "finalize",
        wrap_with_context_async(finalize_results),
    )
    graph.add_node(
        "complete",
        wrap_with_context(_bind_complete(definition)),
    )

    graph.set_entry_point("initialize")
    graph.add_edge("initialize", "update_working_memory")
    graph.add_edge("update_working_memory", "memory_retrieval")
    graph.add_edge("memory_retrieval", "choose_action")
    graph.add_conditional_edges(
        "choose_action",
        with_interactive_state(_route_after_choose_action),
        {
            "approval_gate": "approval_gate",
            "complete": "finalize",
        },
    )
    graph.add_edge("approval_gate", "dispatch_tool")
    graph.add_edge("dispatch_tool", "tool_synthesizer")
    graph.add_edge("tool_synthesizer", "post_tool_reasoning")
    graph.add_edge("post_tool_reasoning", "decision_router")
    graph.add_conditional_edges(
        "decision_router",
        with_interactive_state(_route_after_router),
        {
            "choose_action": "choose_action",
            "think_more": "think_more",
            "reflect": "reflect",
            "complete": "finalize",
        },
    )
    graph.add_edge("think_more", "post_tool_reasoning")
    graph.add_edge("reflect", "decision_router")
    graph.add_edge("finalize", "complete")
    graph.add_edge("complete", END)

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
    """Return a stable registry key for a definition-configured child graph."""

    return f"{GRAPH_NAME_SUBAGENT}:{definition.id}"


def _bind_initialize(definition: SubagentDefinition) -> Any:
    def _initialize(
        state: Mapping[str, Any] | InteractiveState,
        context: GraphRuntimeContext | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return initialize_subagent_state(
            definition,
            state,
            context=context,
            config=config,
        )

    return _initialize


def _bind_choose_action(definition: SubagentDefinition) -> Any:
    async def _choose_action(
        state: Mapping[str, Any] | InteractiveState,
        context: GraphRuntimeContext | None = None,
        config: Mapping[str, Any] | None = None,
        writer: Any = None,
    ) -> dict[str, Any]:
        return await choose_subagent_action(
            definition,
            state,
            context=context,
            config=config,
            writer=writer,
        )

    return _choose_action


def _bind_complete(definition: SubagentDefinition) -> Any:
    def _complete(
        state: Mapping[str, Any] | InteractiveState,
        context: GraphRuntimeContext | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return complete_subagent_result(
            definition,
            state,
            context=context,
            config=config,
        )

    return _complete


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
