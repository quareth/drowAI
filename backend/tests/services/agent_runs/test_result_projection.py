"""Tests for best-effort Scout result projection into main context."""

from __future__ import annotations

import pytest

from agent.graph.context.builder import (
    METADATA_CONTEXT_BUNDLE_KEY,
    build_conversation_context_bundle,
)
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
    AgentRuntimeIdentity,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.result_projection import (
    COMPLETED_AGENT_RESULTS_KEY,
    AgentRunResultProjector,
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
        objective="Map open services on the approved target.",
        targets=["10.0.0.10"],
        suggested_capabilities=["host_discovery", "port_scan"],
        scope_summary="Approved internal test host only.",
        relevant_context={"ticket": "ENG-123"},
        runtime_identity=_runtime_identity(tenant_id=tenant_id, task_id=task_id),
    )


def _result(agent_run_id: str = "run-1", *, summary: str = "Scout found HTTP.") -> AgentResult:
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
            summary="Scout found HTTP with api_key=secret-value-that-must-not-leak",
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
    assert projected["agent_display_name"] == "Pathfinder"
    assert projected["summary"] == "Scout found HTTP with <REDACTED>"
    assert projected["key_findings"] == [
        "HTTP on 80",
        "SSH on 22 <REDACTED>",
    ]
    assert "raw_output" not in projected
    assert "reasoning" not in projected


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
    projector = AgentRunResultProjector(registry=registry)
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


def test_attach_completed_agent_results_updates_metadata_and_context_bundle() -> None:
    bundle = build_conversation_context_bundle(
        conversation_id="conversation-1",
        turn_id="turn-2",
        turn_sequence=2,
        messages=[],
        current_message="summarize scout",
    )
    metadata = {METADATA_CONTEXT_BUNDLE_KEY: bundle}
    handoff = type(
        "_Handoff",
        (),
        {
            "results": (
                {
                    "agent_run_id": "run-1",
                    "agent_kind": "recon",
                    "agent_display_name": "Scout",
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
