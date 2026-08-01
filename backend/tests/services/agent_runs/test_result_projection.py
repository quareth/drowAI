"""Tests for best-effort subagent result projection into main context."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent.subagents.registry import SubagentRegistry, get_subagent_registry
from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from agent.graph.context.projections import project_for_planner
from agent.graph.context.serialization import (
    SECTION_ACTIVE_AGENT_RUNS,
    serialize_projection_to_section_map,
)
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
    AgentRuntimeIdentity,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.result_projection import (
    ACTIVE_AGENT_RUNS_KEY,
    COMPLETED_AGENT_RESULTS_KEY,
    AgentRunResultProjector,
    attach_active_agent_runs_to_context,
    attach_completed_agent_results_to_context,
)


def _runtime_identity(*, tenant_id: int = 7, task_id: int = 42) -> AgentRuntimeIdentity:
    return AgentRuntimeIdentity(
        tenant_id=tenant_id,
        task_id=task_id,
        workspace_id=f"task-{task_id}",
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


def _assignment(
    *,
    tenant_id: int = 7,
    task_id: int = 42,
    agent_run_id: str = "run-1",
    conversation_id: str = "conversation-1",
    objective: str = "Map open services on the approved target.",
) -> AgentAssignment:
    return AgentAssignment(
        assignment_id=f"assign-{agent_run_id}",
        agent_run_id=agent_run_id,
        agent_id="pathfinder",
        agent_kind="recon",
        task_id=task_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        parent_turn_id="turn-1",
        parent_graph_thread_id="parent-thread-1",
        objective=objective,
        targets=["10.0.0.10"],
        suggested_capabilities=["host_discovery", "port_scan"],
        scope_summary="Approved internal test host only.",
        relevant_context={"ticket": "ENG-123"},
        runtime_identity=_runtime_identity(tenant_id=tenant_id, task_id=task_id),
    )


def _result(
    agent_run_id: str = "run-1",
    *,
    summary: str = "Pathfinder found HTTP.",
) -> AgentResult:
    return AgentResult(
        agent_run_id=agent_run_id,
        agent_id="pathfinder",
        agent_kind="recon",
        outcome="completed",
        summary=summary,
        key_findings=[
            "HTTP on 80",
            "SSH on 22 token=super-secret-token-value",
            "extra finding",
        ],
        evidence_refs=[
            {
                "kind": "artifact",
                "evidence_id": "nmap-xml",
                "summary": "Nmap XML artifact",
            }
        ],
        tools_used=["nmap", "curl"],
        limitations=["No UDP scan"],
        recommended_next_steps=["Review HTTP headers"],
        final_checkpoint_id="checkpoint-1",
    )


@pytest.mark.asyncio
async def test_collect_projects_completed_unconsumed_results_for_conversation() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(agent_run_id="run-1"), graph_thread_id="child-1")
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result(
            "run-1",
            summary="Pathfinder found HTTP with api_key=secret-value-that-must-not-leak",
        ),
    )
    await registry.register(
        _assignment(agent_run_id="run-2", conversation_id="other-conversation"),
        graph_thread_id="child-2",
    )
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-2",
        result=_result("run-2", summary="Other conversation result"),
    )

    projector = AgentRunResultProjector(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        max_results=1,
        max_list_items=2,
        max_text_chars=48,
    )

    handoff = await projector.collect_for_context(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
    )

    assert handoff.agent_run_ids == ("run-1",)
    assert len(handoff.results) == 1
    projected = handoff.results[0]
    assert projected["agent_id"] == "pathfinder"
    assert projected["agent_display_name"] == "Pathfinder"
    assert projected["summary"] == "Pathfinder found HTTP with <REDACTED>"
    assert projected["key_findings"] == [
        "HTTP on 80",
        "SSH on 22 <REDACTED>",
    ]
    assert "raw_output" not in projected
    assert "reasoning" not in projected


@pytest.mark.asyncio
async def test_collect_projects_task_local_active_runs_with_bounded_fields() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(
        _assignment(
            agent_run_id="run-active",
            objective=(
                "Investigate api_key=secret-value-that-must-not-leak and "
                "continue with a very long bounded assignment brief."
            ),
        ),
        graph_thread_id="child-active",
    )
    await registry.mark_running(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-active",
    )
    await registry.register(
        _assignment(agent_run_id="run-other-task", task_id=43),
        graph_thread_id="child-other-task",
    )
    await registry.register(
        _assignment(agent_run_id="run-other-conversation", conversation_id="other"),
        graph_thread_id="child-other-conversation",
    )
    await registry.register(
        _assignment(agent_run_id="run-completed"),
        graph_thread_id="child-completed",
    )
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-completed",
        result=_result("run-completed"),
    )

    projector = AgentRunResultProjector(
        registry=registry,
        subagent_registry=get_subagent_registry(),
        max_active_runs=3,
        max_text_chars=64,
    )

    active_runs = await projector.collect_active_for_context(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
    )

    assert len(active_runs) == 1
    active = active_runs[0]
    assert active["agent_run_id"] == "run-active"
    assert active["assignment_id"] == "assign-run-active"
    assert active["agent_id"] == "pathfinder"
    assert active["agent_display_name"] == "Pathfinder"
    assert active["status"] == "running"
    assert active["lifecycle_version"] == 2
    assert active["created_at"]
    assert active["started_at"]
    assert "<REDACTED>" in active["objective"]
    assert len(active["objective"]) <= 64
    assert "task_handle" not in active
    assert "assignment" not in active
    assert "relevant_context" not in active
    assert "result" not in active


@pytest.mark.asyncio
async def test_mark_consumed_is_idempotent_after_acceptance() -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(), graph_thread_id="child-1")
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=_result("run-1"),
    )
    projector = AgentRunResultProjector(
        registry=registry,
        subagent_registry=get_subagent_registry(),
    )
    handoff = await projector.collect_for_context(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
    )

    await projector.mark_consumed(tenant_id=7, task_id=42, handoff=handoff)
    await projector.mark_consumed(tenant_id=7, task_id=42, handoff=handoff)

    assert await registry.consume_result(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
    ) is None
    second_handoff = await projector.collect_for_context(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
    )
    assert second_handoff.results == ()
    assert second_handoff.agent_run_ids == ()


@pytest.mark.asyncio
async def test_projection_uses_the_injected_definition_registry() -> None:
    default_definition = get_subagent_registry().require("pathfinder")
    definitions = SubagentRegistry(
        (
            replace(
                default_definition,
                id="web_mapper",
                display_name="Web Mapper",
                icon="web-mapper",
            ),
        )
    )
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment().model_copy(update={"agent_id": "web_mapper"})
    result = _result().model_copy(update={"agent_id": "web_mapper"})
    await registry.register(assignment, graph_thread_id="child-custom")
    await registry.mark_completed(
        tenant_id=7,
        task_id=42,
        agent_run_id="run-1",
        result=result,
    )
    projector = AgentRunResultProjector(
        registry=registry,
        subagent_registry=definitions,
    )

    handoff = await projector.collect_for_context(
        tenant_id=7,
        task_id=42,
        conversation_id="conversation-1",
    )

    assert handoff.results[0]["agent_id"] == "web_mapper"
    assert handoff.results[0]["agent_display_name"] == "Web Mapper"


def test_attach_completed_agent_results_updates_metadata_and_context_bundle() -> None:
    bundle = build_conversation_context_bundle(
        conversation_id="conversation-1",
        turn_id="turn-2",
        turn_sequence=2,
        messages=[],
        current_message="summarize pathfinder",
    )
    metadata = {METADATA_CONTEXT_BUNDLE_KEY: bundle}
    handoff = type(
        "_Handoff",
        (),
        {
            "results": (
                {
                    "agent_run_id": "run-1",
                    "agent_id": "pathfinder",
                    "agent_kind": "recon",
                    "agent_display_name": "Pathfinder",
                    "outcome": "completed",
                    "summary": "HTTP exposed",
                },
            )
        },
    )()

    attach_completed_agent_results_to_context(metadata, handoff)

    assert metadata[COMPLETED_AGENT_RESULTS_KEY][0]["summary"] == "HTTP exposed"
    assert (
        metadata[METADATA_CONTEXT_BUNDLE_KEY][COMPLETED_AGENT_RESULTS_KEY][0]["summary"]
        == "HTTP exposed"
    )


def test_attach_active_agent_runs_updates_metadata_and_context_bundle() -> None:
    bundle = build_conversation_context_bundle(
        conversation_id="conversation-1",
        turn_id="turn-2",
        turn_sequence=2,
        messages=[],
        current_message="check active runs",
    )
    metadata = {METADATA_CONTEXT_BUNDLE_KEY: bundle}
    active_runs = (
        {
            "agent_run_id": "run-1",
            "assignment_id": "assign-run-1",
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "agent_display_name": "Pathfinder",
            "objective": "Map open services.",
            "status": "running",
            "lifecycle_version": 2,
            "created_at": "2026-07-29T10:00:00+00:00",
            "started_at": "2026-07-29T10:00:01+00:00",
        },
    )

    attach_active_agent_runs_to_context(metadata, active_runs)

    assert metadata[ACTIVE_AGENT_RUNS_KEY][0]["objective"] == "Map open services."
    assert (
        metadata[METADATA_CONTEXT_BUNDLE_KEY][ACTIVE_AGENT_RUNS_KEY][0]["objective"]
        == "Map open services."
    )


def test_attach_active_agent_runs_clears_stale_metadata_and_context_bundle() -> None:
    bundle = build_conversation_context_bundle(
        conversation_id="conversation-1",
        turn_id="turn-2",
        turn_sequence=2,
        messages=[],
        current_message="check active runs",
    )
    bundle[ACTIVE_AGENT_RUNS_KEY] = [{"agent_run_id": "stale-run"}]
    metadata = {
        ACTIVE_AGENT_RUNS_KEY: [{"agent_run_id": "stale-run"}],
        METADATA_CONTEXT_BUNDLE_KEY: bundle,
    }

    attach_active_agent_runs_to_context(metadata, ())

    assert metadata[ACTIVE_AGENT_RUNS_KEY] == []
    assert metadata[METADATA_CONTEXT_BUNDLE_KEY][ACTIVE_AGENT_RUNS_KEY] == []


def test_active_agent_runs_project_and_render_without_private_fields() -> None:
    bundle = build_conversation_context_bundle(
        conversation_id="conversation-1",
        turn_id="turn-2",
        turn_sequence=2,
        messages=[],
        current_message="check active runs",
    )
    bundle[ACTIVE_AGENT_RUNS_KEY] = [
        {
            "agent_run_id": "run-1",
            "assignment_id": "assign-run-1",
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "agent_display_name": "Pathfinder",
            "objective": "Map open services.",
            "status": "running",
            "lifecycle_version": 2,
            "created_at": "2026-07-29T10:00:00+00:00",
            "started_at": "2026-07-29T10:00:01+00:00",
            "task_handle": "PRIVATE_TASK_HANDLE",
            "child_messages": ["PRIVATE_CHILD_TRANSCRIPT"],
            "tool_transcript": "PRIVATE_TOOL_OUTPUT",
        }
    ]

    projection = project_for_planner(bundle)
    section_map = serialize_projection_to_section_map(projection)

    assert projection[ACTIVE_AGENT_RUNS_KEY] == [
        {
            "agent_run_id": "run-1",
            "assignment_id": "assign-run-1",
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "agent_display_name": "Pathfinder",
            "objective": "Map open services.",
            "status": "running",
            "lifecycle_version": 2,
            "created_at": "2026-07-29T10:00:00+00:00",
            "started_at": "2026-07-29T10:00:01+00:00",
        }
    ]
    rendered = section_map[SECTION_ACTIVE_AGENT_RUNS]
    assert "Active Agent Runs:" in rendered
    assert "objective: Map open services." in rendered
    assert "PRIVATE_TASK_HANDLE" not in rendered
    assert "PRIVATE_CHILD_TRANSCRIPT" not in rendered
    assert "PRIVATE_TOOL_OUTPUT" not in rendered
