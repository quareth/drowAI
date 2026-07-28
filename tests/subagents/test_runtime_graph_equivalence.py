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
from agent.subagents.runtime.profile import SubagentToolProfile, SubagentToolSpec
from agent.subagents.runtime.state import build_subagent_initial_state


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


def _profile() -> SubagentToolProfile:
    return SubagentToolProfile(
        tools=(
            SubagentToolSpec(
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


def test_generic_subagent_graph_topology_stays_locked() -> None:
    definition = _pathfinder_definition()
    generic_graph = build_subagent_graph(definition, build_only=True)

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
    assert set(generic_graph.edges) == {
        ("__start__", "initialize"),
        ("initialize", "update_working_memory"),
        ("update_working_memory", "memory_retrieval"),
        ("memory_retrieval", "choose_action"),
        ("approval_gate", "dispatch_tool"),
        ("dispatch_tool", "tool_synthesizer"),
        ("tool_synthesizer", "post_tool_reasoning"),
        ("post_tool_reasoning", "decision_router"),
        ("think_more", "post_tool_reasoning"),
        ("reflect", "decision_router"),
        ("finalize", "complete"),
        ("complete", "__end__"),
    }
    assert set(generic_graph.branches) == {"choose_action", "decision_router"}
    choose_branch = generic_graph.branches["choose_action"][
        "_route_after_choose_action"
    ]
    router_branch = generic_graph.branches["decision_router"]["_route_after_router"]
    assert choose_branch.ends == {
        "approval_gate": "approval_gate",
        "complete": "finalize",
    }
    assert router_branch.ends == {
        "choose_action": "choose_action",
        "think_more": "think_more",
        "reflect": "reflect",
        "complete": "finalize",
    }


def test_generic_initialize_preserves_assignment_and_tool_identity() -> None:
    definition = _pathfinder_definition()
    config = {"configurable": {"thread_id": "graph-child-thread-1"}}

    update = initialize_subagent_state(
        definition,
        _generic_state(),
        config=config,
    )

    metadata = update["facts"]["metadata"]
    subagent_metadata = metadata["scout"]
    assert metadata["agent_id"] == "pathfinder"
    assert metadata["agent_kind"] == "recon"
    assert metadata["graph_thread_id"] == "child-thread-1"
    assert subagent_metadata["agent_id"] == "pathfinder"
    assert subagent_metadata["graph_thread_id"] == "child-thread-1"
    assert update["facts"]["tool_ids"] == [FPING_TOOL_ID]
    assert update["facts"]["tool_candidates"] == [FPING_TOOL_ID]
    assert update["trace"]["history"][-1] == {
        "type": "scout_initialize",
        "agent_run_id": "run-1",
        "agent_id": "pathfinder",
        "agent_kind": "recon",
        "tool_ids": [FPING_TOOL_ID],
    }


def test_generic_graph_terminal_result_projects_pathfinder_result() -> None:
    generic_interactive = InteractiveState.from_mapping(_generic_state())
    generic_interactive.facts.last_tool_result_compact = {
        "tool": FPING_TOOL_ID,
        "status": "success",
        "success": True,
        "summary": "fping found one live host.",
        "key_findings": ["10.0.0.10 is alive."],
        "report_recommendations": ["Run service enumeration next."],
        "artifact_refs": [{"path": "/workspace/fping.json", "label": "fping"}],
    }
    generic_interactive.facts.metadata["last_tool_result_compact"] = (
        generic_interactive.facts.last_tool_result_compact
    )
    generic_interactive.facts.metadata["router_outcome"] = {"action": "finalize"}
    generic_interactive.trace.executed_tools.append(
        ToolExecutionRecord(tool_id=FPING_TOOL_ID, status="success")
    )

    update = complete_subagent_result(
        _pathfinder_definition(),
        generic_interactive.as_graph_state(),
    )

    result = update["facts"]["metadata"][SUBAGENT_RESULT_METADATA_KEY]
    assert result["agent_run_id"] == "run-1"
    assert result["agent_id"] == "pathfinder"
    assert result["agent_kind"] == "recon"
    assert result["outcome"] == "completed"
    assert result["summary"] == "fping found one live host."
    assert result["key_findings"] == ["10.0.0.10 is alive."]
    assert result["evidence_refs"] == [
        {"path": "/workspace/fping.json", "label": "fping"}
    ]
    assert result["tools_used"] == [FPING_TOOL_ID]


def test_generic_graph_registry_uses_single_child_graph_type_name(
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

    assert graph_name_for_definition(definition) == "subagent"
    assert compiled == ("compiled-subagent", "checkpoint")
    assert registry.get("subagent") == compiled
    assert calls == [("pathfinder", True)]


def test_generic_graph_exposes_current_result_metadata_key() -> None:
    assert SUBAGENT_RESULT_METADATA_KEY == "scout_result"
