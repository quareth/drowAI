"""Tests for Scout recon graph wiring and completion."""

from __future__ import annotations

from typing import Any

import pytest

from agent.graph.state import InteractiveState, ToolExecutionRecord
from agent.subagents.contracts import AgentAssignment, AgentRuntimeIdentity
from agent.subagents.scout.graph import (
    GRAPH_NAME_SCOUT_RECON,
    build_scout_recon_graph,
    get_compiled_scout_recon_graph,
)
from agent.subagents.scout.nodes.choose_action import (
    SCOUT_ACTION_METADATA_KEY,
    SCOUT_RESULT_METADATA_KEY,
)
from agent.subagents.scout.nodes.complete import (
    SCOUT_COMPLETION_METADATA_KEY,
    SCOUT_RESULT_PROJECTION_METADATA_KEY,
    complete_scout_result,
)
from agent.subagents.scout.profile import ScoutToolProfile, ScoutToolSpec
from agent.subagents.scout.state import build_scout_initial_state


FPING_TOOL_ID = "information_gathering.network_discovery.fping"


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


def _state() -> dict[str, Any]:
    return build_scout_initial_state(
        assignment=_assignment(),
        graph_thread_id="child-thread-1",
        tool_profile=ScoutToolProfile(
            tools=(
                ScoutToolSpec(
                    tool_id=FPING_TOOL_ID,
                    display_name="fping",
                    scout_capabilities=("host_discovery",),
                ),
            )
        ),
    )


def test_scout_graph_topology_reuses_existing_execution_loop() -> None:
    graph = build_scout_recon_graph(build_only=True)

    assert GRAPH_NAME_SCOUT_RECON == "scout_recon"
    assert set(graph.nodes) == {
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

    edges = set(graph.edges)
    assert ("initialize", "update_working_memory") in edges
    assert ("update_working_memory", "memory_retrieval") in edges
    assert ("memory_retrieval", "choose_action") in edges
    assert ("approval_gate", "dispatch_tool") in edges
    assert ("dispatch_tool", "tool_synthesizer") in edges
    assert ("tool_synthesizer", "post_tool_reasoning") in edges
    assert ("post_tool_reasoning", "decision_router") in edges
    assert ("think_more", "post_tool_reasoning") in edges
    assert ("reflect", "decision_router") in edges
    assert ("finalize", "complete") in edges


def test_scout_graph_registry_uses_canonical_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent.graph.infrastructure.graph_registry import GraphRegistry
    import agent.subagents.scout.graph as scout_graph

    class _FakeGraph:
        def compile(self, *, checkpointer: Any) -> tuple[str, Any]:
            return ("compiled-scout", checkpointer)

    calls: list[bool] = []

    def _fake_build_scout_recon_graph(*, build_only: bool = False) -> _FakeGraph:
        calls.append(build_only)
        return _FakeGraph()

    monkeypatch.setattr(
        scout_graph,
        "build_scout_recon_graph",
        _fake_build_scout_recon_graph,
    )
    monkeypatch.setattr(scout_graph, "get_default_checkpointer", lambda: "checkpoint")

    registry = GraphRegistry()
    compiled = get_compiled_scout_recon_graph(registry=registry)

    assert compiled == ("compiled-scout", "checkpoint")
    assert registry.get(GRAPH_NAME_SCOUT_RECON) == compiled
    assert calls == [True]


def test_complete_node_validates_explicit_submit_result() -> None:
    state = _state()
    metadata = state["facts"]["metadata"]
    metadata[SCOUT_ACTION_METADATA_KEY] = {
        "route": "complete",
        "agent_run_id": "run-1",
        "outcome": "completed",
    }
    metadata[SCOUT_RESULT_METADATA_KEY] = {
        "agent_run_id": "run-1",
        "agent_id": "pathfinder",
        "agent_kind": "recon",
        "outcome": "completed",
        "summary": "Host liveness was checked.",
        "key_findings": ["10.0.0.10 responded to probes."],
        "tools_used": [FPING_TOOL_ID],
    }

    update = complete_scout_result(state)

    metadata = update["facts"]["metadata"]
    assert metadata[SCOUT_COMPLETION_METADATA_KEY] == {
        "agent_run_id": "run-1",
        "agent_id": "pathfinder",
        "agent_kind": "recon",
        "outcome": "completed",
    }
    assert (
        metadata[SCOUT_RESULT_PROJECTION_METADATA_KEY]["agent_display_name"]
        == "Pathfinder"
    )
    assert update["trace"]["final_text"] == "Host liveness was checked."
    assert update["trace"]["history"][-1]["type"] == "scout_result"


def test_complete_node_uses_streamed_finalizer_message_as_handoff_summary() -> None:
    interactive = InteractiveState.from_mapping(_state())
    interactive.facts.metadata[SCOUT_RESULT_METADATA_KEY] = {
        "agent_run_id": "run-1",
        "agent_id": "pathfinder",
        "agent_kind": "recon",
        "outcome": "completed",
        "summary": "Structured draft summary.",
    }
    interactive.trace.final_text = "Scout's streamed final response."

    update = complete_scout_result(interactive.as_graph_state())

    result = update["facts"]["metadata"][SCOUT_RESULT_METADATA_KEY]
    assert result["summary"] == "Scout's streamed final response."


def test_complete_node_derives_bounded_result_from_compact_state() -> None:
    interactive = InteractiveState.from_mapping(_state())
    interactive.facts.last_tool_result_compact = {
        "tool": FPING_TOOL_ID,
        "status": "success",
        "success": True,
        "summary": "fping found one live host.",
        "key_findings": ["10.0.0.10 is alive."],
        "report_recommendations": ["Run service enumeration next."],
        "artifact_refs": [{"path": "/workspace/fping.json", "label": "fping output"}],
    }
    interactive.facts.metadata["last_tool_result_compact"] = (
        interactive.facts.last_tool_result_compact
    )
    interactive.facts.metadata["router_outcome"] = {"action": "finalize"}
    interactive.trace.executed_tools.append(
        ToolExecutionRecord(tool_id=FPING_TOOL_ID, status="success")
    )

    update = complete_scout_result(interactive.as_graph_state())

    result = update["facts"]["metadata"][SCOUT_RESULT_METADATA_KEY]
    assert result["agent_run_id"] == "run-1"
    assert result["agent_id"] == "pathfinder"
    assert result["agent_kind"] == "recon"
    assert result["outcome"] == "completed"
    assert result["summary"] == "fping found one live host."
    assert result["key_findings"] == ["10.0.0.10 is alive."]
    assert result["tools_used"] == [FPING_TOOL_ID]
    assert result["recommended_next_steps"] == ["Run service enumeration next."]
    assert result["evidence_refs"] == [
        {"path": "/workspace/fping.json", "label": "fping output"}
    ]


def test_complete_node_rejects_result_identity_drift() -> None:
    state = _state()
    state["facts"]["metadata"][SCOUT_RESULT_METADATA_KEY] = {
        "agent_run_id": "other-run",
        "agent_kind": "recon",
        "outcome": "completed",
        "summary": "Wrong run.",
    }

    with pytest.raises(ValueError, match="agent_run_id"):
        complete_scout_result(state)
