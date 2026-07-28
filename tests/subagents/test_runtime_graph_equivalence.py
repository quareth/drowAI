"""Equivalence tests for the generic subagent graph extraction."""

from __future__ import annotations

from typing import Any

import pytest

from agent.graph.infrastructure.graph_registry import GraphRegistry
from agent.graph.state import InteractiveState, ToolExecutionRecord
from agent.subagents.contracts import AgentAssignment, AgentRuntimeIdentity
from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.runtime.complete import complete_subagent_result
from agent.subagents.runtime.graph import (
    build_subagent_graph,
    get_compiled_subagent_graph,
    graph_name_for_definition,
    initialize_subagent_state,
)
from agent.subagents.runtime.model import SUBAGENT_RESULT_METADATA_KEY
from agent.subagents.runtime.state import build_subagent_initial_state
from agent.subagents.scout.graph import build_scout_recon_graph
from agent.subagents.scout.nodes.complete import complete_scout_result
from agent.subagents.scout.nodes.initialize import initialize_scout_state
from agent.subagents.scout.profile import ScoutToolProfile, ScoutToolSpec
from agent.subagents.scout.state import build_scout_initial_state


FPING_TOOL_ID = "information_gathering.network_discovery.fping"


def _pathfinder_definition() -> SubagentDefinition:
    [definition] = [
        definition
        for definition in load_subagent_definitions()
        if definition.id == "pathfinder"
    ]
    return definition


def _runtime_identity() -> AgentRuntimeIdentity:
    return AgentRuntimeIdentity(
        tenant_id=7,
        task_id=42,
        user_id=3,
        workspace_id="task-42",
        workspace_path="/workspace",
        runtime_placement_mode="runner",
        actor_type="user",
        actor_id="3",
        runner_id="runner-1",
        execution_site_id="site-1",
        provider="openai",
        model="gpt-5.2-mini",
        reasoning_effort="medium",
        feature_flags={},
    )


def _assignment() -> AgentAssignment:
    return AgentAssignment(
        assignment_id="assign-1",
        agent_run_id="run-1",
        agent_id="pathfinder",
        agent_kind="recon",
        task_id=42,
        tenant_id=7,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        parent_graph_thread_id="parent-thread-1",
        objective="Map live hosts on the approved target.",
        targets=["10.0.0.10"],
        suggested_capabilities=["host_discovery"],
        scope_summary="Approved internal test host only.",
        relevant_context={"ticket": "ENG-123"},
        runtime_identity=_runtime_identity(),
    )


def _profile() -> ScoutToolProfile:
    return ScoutToolProfile(
        tools=(
            ScoutToolSpec(
                tool_id=FPING_TOOL_ID,
                display_name="fping",
                scout_capabilities=("host_discovery",),
            ),
        ),
    )


def _generic_state() -> dict[str, Any]:
    return build_subagent_initial_state(
        definition=_pathfinder_definition(),
        assignment=_assignment(),
        graph_thread_id="child-thread-1",
        tool_profile=_profile(),
    )


def _legacy_state() -> dict[str, Any]:
    return build_scout_initial_state(
        assignment=_assignment(),
        graph_thread_id="child-thread-1",
        tool_profile=_profile(),
    )


def test_generic_subagent_graph_topology_matches_current_scout_graph() -> None:
    definition = _pathfinder_definition()
    generic_graph = build_subagent_graph(definition, build_only=True)
    scout_graph = build_scout_recon_graph(build_only=True)

    assert set(generic_graph.nodes) == set(scout_graph.nodes)
    assert set(generic_graph.nodes) == {
        "initialize",
        "update_working_memory",
        "memory_retrieval",
        "choose_action",
        "approval_gate",
        "dispatch_tool",
        "tool_synthesizer",
        "post_tool_reasoning",
        "decision_router",
        "think_more",
        "reflect",
        "finalize",
        "complete",
    }
    assert set(generic_graph.edges) == set(scout_graph.edges)


def test_generic_initialize_matches_current_scout_initialize() -> None:
    definition = _pathfinder_definition()
    config = {"configurable": {"thread_id": "graph-child-thread-1"}}

    assert initialize_subagent_state(
        definition,
        _generic_state(),
        config=config,
    ) == initialize_scout_state(_legacy_state(), config=config)


def test_generic_graph_terminal_result_matches_current_scout_result() -> None:
    generic_interactive = InteractiveState.from_mapping(_generic_state())
    scout_interactive = InteractiveState.from_mapping(_legacy_state())
    for interactive in (generic_interactive, scout_interactive):
        interactive.facts.last_tool_result_compact = {
            "tool": FPING_TOOL_ID,
            "status": "success",
            "success": True,
            "summary": "fping found one live host.",
            "key_findings": ["10.0.0.10 is alive."],
            "report_recommendations": ["Run service enumeration next."],
            "artifact_refs": [{"path": "/workspace/fping.json", "label": "fping"}],
        }
        interactive.facts.metadata["last_tool_result_compact"] = (
            interactive.facts.last_tool_result_compact
        )
        interactive.facts.metadata["router_outcome"] = {"action": "finalize"}
        interactive.trace.executed_tools.append(
            ToolExecutionRecord(tool_id=FPING_TOOL_ID, status="success")
        )

    assert complete_subagent_result(
        _pathfinder_definition(),
        generic_interactive.as_graph_state(),
    ) == complete_scout_result(scout_interactive.as_graph_state())


def test_generic_graph_registry_uses_definition_scoped_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.subagents.runtime.graph as runtime_graph

    definition = _pathfinder_definition()

    class _FakeGraph:
        def compile(self, *, checkpointer: Any) -> tuple[str, Any]:
            return ("compiled-subagent", checkpointer)

    calls: list[tuple[str, bool]] = []

    def _fake_build_subagent_graph(
        actual_definition: SubagentDefinition,
        *,
        build_only: bool = False,
    ) -> _FakeGraph:
        calls.append((actual_definition.id, build_only))
        return _FakeGraph()

    monkeypatch.setattr(
        runtime_graph,
        "build_subagent_graph",
        _fake_build_subagent_graph,
    )
    monkeypatch.setattr(runtime_graph, "get_default_checkpointer", lambda: "checkpoint")

    registry = GraphRegistry()
    compiled = get_compiled_subagent_graph(definition, registry=registry)

    assert graph_name_for_definition(definition) == "subagent:pathfinder"
    assert compiled == ("compiled-subagent", "checkpoint")
    assert registry.get("subagent:pathfinder") == compiled
    assert calls == [("pathfinder", True)]


def test_generic_graph_exposes_current_result_metadata_key() -> None:
    assert SUBAGENT_RESULT_METADATA_KEY == "scout_result"
