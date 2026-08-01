"""Fresh contract builders shared by backend agent-run tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent.subagents.contracts import (
    AgentAssignment,
    AgentCredentialReference,
    AgentResult,
    AgentRunOutcome,
    AgentRuntimeIdentity,
)


def build_runtime_identity(
    *,
    tenant_id: int = 7,
    task_id: int = 42,
    user_id: int | None = None,
    workspace_id: str | None = None,
    workspace_path: str | None = "/workspace",
    runtime_placement_mode: str = "runner",
    actor_type: str = "user",
    actor_id: str = "3",
    runner_id: str | None = "runner-1",
    execution_site_id: str | None = "site-1",
    provider: str | None = "openai",
    model: str | None = "gpt-5.2-mini",
    reasoning_effort: str | None = "medium",
    feature_flags: Mapping[str, bool] | None = None,
    credential_ref: AgentCredentialReference | Mapping[str, str] | None = None,
) -> AgentRuntimeIdentity:
    """Build one runtime identity without sharing nested values."""

    return AgentRuntimeIdentity(
        tenant_id=tenant_id,
        task_id=task_id,
        user_id=user_id,
        workspace_id=f"task-{task_id}" if workspace_id is None else workspace_id,
        workspace_path=workspace_path,
        runtime_placement_mode=runtime_placement_mode,
        actor_type=actor_type,
        actor_id=actor_id,
        runner_id=runner_id,
        execution_site_id=execution_site_id,
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
        feature_flags=dict(feature_flags or {}),
        credential_ref=(
            None
            if credential_ref is None
            else AgentCredentialReference.model_validate(
                credential_ref.model_dump(mode="python")
                if isinstance(credential_ref, AgentCredentialReference)
                else dict(credential_ref)
            )
        ),
    )


def build_agent_assignment(
    *,
    runtime_identity: AgentRuntimeIdentity | None = None,
    assignment_id: str = "assign-1",
    agent_run_id: str = "run-1",
    agent_id: str = "pathfinder",
    agent_kind: str = "recon",
    conversation_id: str = "conversation-1",
    parent_turn_id: str = "turn-1",
    parent_graph_thread_id: str = "parent-thread-1",
    objective: str = "Map open services on the approved target.",
    targets: Sequence[str] = ("10.0.0.10",),
    suggested_capabilities: Sequence[str] = ("host_discovery", "port_scan"),
    scope_summary: str | None = "Approved internal test host only.",
    relevant_context: Mapping[str, Any] | None = None,
) -> AgentAssignment:
    """Build an assignment whose tenant and task derive from its identity."""

    identity = runtime_identity or build_runtime_identity()
    identity_copy = AgentRuntimeIdentity.model_validate(
        identity.model_dump(mode="python")
    )
    context = {"ticket": "ENG-123"} if relevant_context is None else dict(relevant_context)
    return AgentAssignment(
        assignment_id=assignment_id,
        agent_run_id=agent_run_id,
        agent_id=agent_id,
        agent_kind=agent_kind,
        task_id=identity_copy.task_id,
        tenant_id=identity_copy.tenant_id,
        conversation_id=conversation_id,
        parent_turn_id=parent_turn_id,
        parent_graph_thread_id=parent_graph_thread_id,
        objective=objective,
        targets=list(targets),
        suggested_capabilities=list(suggested_capabilities),
        scope_summary=scope_summary,
        relevant_context=context,
        runtime_identity=identity_copy,
    )


def build_agent_result(
    assignment: AgentAssignment,
    *,
    outcome: AgentRunOutcome = "completed",
    summary: str = "Pathfinder found an exposed service.",
    key_findings: Sequence[str] = ("HTTP exposed on 80",),
    evidence_refs: Sequence[Mapping[str, str]] | None = None,
    tools_used: Sequence[str] = ("nmap",),
    limitations: Sequence[str] = (),
    recommended_next_steps: Sequence[str] = ("Review HTTP service headers",),
    final_checkpoint_id: str | None = "checkpoint-1",
) -> AgentResult:
    """Build a result whose immutable agent identity derives from an assignment."""

    evidence = (
        [{"kind": "artifact", "path": "/workspace/artifacts/nmap.xml"}]
        if evidence_refs is None
        else [dict(reference) for reference in evidence_refs]
    )
    return AgentResult(
        agent_run_id=assignment.agent_run_id,
        agent_id=assignment.agent_id,
        agent_kind=assignment.agent_kind,
        outcome=outcome,
        summary=summary,
        key_findings=list(key_findings),
        evidence_refs=evidence,
        tools_used=list(tools_used),
        limitations=list(limitations),
        recommended_next_steps=list(recommended_next_steps),
        final_checkpoint_id=final_checkpoint_id,
    )


__all__ = [
    "build_agent_assignment",
    "build_agent_result",
    "build_runtime_identity",
]
