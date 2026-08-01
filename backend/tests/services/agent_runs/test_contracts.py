"""Contract tests for generic subagent run schemas."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent.subagents import contracts as agent_contracts
from agent.subagents.registry import get_subagent_registry
from backend.services.agent_runs import contracts as backend_contracts
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
    AgentResultProjection,
    AgentRunLifecycleProjection,
    AgentRuntimeIdentity,
)


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
        credential_ref={"provider": "openai", "credential_id": "cred-1"},
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
        objective="Map open services on the approved target.",
        targets=["10.0.0.10"],
        suggested_capabilities=["host_discovery", "port_scan", "service_enum"],
        scope_summary="Approved internal test host only.",
        relevant_context={"ticket": "ENG-123", "ports": [80, 443]},
        runtime_identity=_runtime_identity(),
    )


def test_backend_contracts_reexport_agent_contracts() -> None:
    assert backend_contracts.AgentAssignment is agent_contracts.AgentAssignment
    assert (
        backend_contracts.AgentCredentialReference
        is agent_contracts.AgentCredentialReference
    )
    assert backend_contracts.AgentRuntimeIdentity is agent_contracts.AgentRuntimeIdentity
    assert backend_contracts.AgentResult is agent_contracts.AgentResult
    display_map_name = "AGENT_" + "DISPLAY_NAMES"
    assert not hasattr(agent_contracts, display_map_name)
    assert not hasattr(backend_contracts, display_map_name)
    legacy_capability_name = "Recon" + "Capability"
    assert not hasattr(agent_contracts, legacy_capability_name)
    assert not hasattr(backend_contracts, legacy_capability_name)


def test_assignment_validates_deterministically_and_keeps_display_name_separate() -> None:
    assignment = _assignment()

    assert assignment.agent_kind == "recon"
    assert "agent_display_name" not in assignment.model_dump()
    assert (
        get_subagent_registry().display_metadata(assignment.agent_id).display_name
        == "Pathfinder"
    )

    dumped = assignment.model_dump(mode="json")
    round_tripped = AgentAssignment.model_validate(dumped)
    assert round_tripped == assignment


def test_runtime_identity_rejects_non_serializable_values() -> None:
    with pytest.raises(ValidationError, match="credential_id"):
        AgentRuntimeIdentity(
            tenant_id=7,
            task_id=42,
            workspace_id="task-42",
            runtime_placement_mode="runner",
            actor_type="user",
            actor_id="3",
            credential_ref={"credential_id": object()},
        )


def test_runtime_identity_allows_only_identifier_credential_refs() -> None:
    identity = _runtime_identity()

    assert identity.credential_ref is not None
    assert identity.credential_ref.provider == "openai"
    assert identity.credential_ref.credential_id == "cred-1"
    assert identity.model_dump(mode="json")["credential_ref"] == {
        "provider": "openai",
        "credential_id": "cred-1",
    }

    secret_payloads = (
        {"provider": "openai", "credential_id": "cred-1", "api_key": "key-id"},
        {"provider": "openai", "credential_id": "cred-1", "token": "token-id"},
        {"provider": "openai", "credential_id": "cred-1", "password": "password-id"},
        {"provider": "openai", "credential_id": "cred-1", "secret": "secret-id"},
        {"provider": "openai", "credential_id": "sk-live-secret"},
    )
    for credential_ref in secret_payloads:
        with pytest.raises(ValidationError, match="secret|not safe to project"):
            AgentRuntimeIdentity(
                tenant_id=7,
                task_id=42,
                workspace_id="task-42",
                runtime_placement_mode="runner",
                actor_type="user",
                actor_id="3",
                credential_ref=credential_ref,
            )


def test_assignment_rejects_raw_tool_output_and_chain_of_thought_context() -> None:
    base = _assignment().model_dump()

    for forbidden_key in ("raw_tool_output", "chain_of_thought"):
        with pytest.raises(ValidationError, match="not safe to project"):
            AgentAssignment.model_validate(
                {
                    **base,
                    "relevant_context": {forbidden_key: "sensitive internals"},
                }
            )


def test_contracts_reject_post_validation_mutation_of_safe_payloads() -> None:
    assignment = _assignment()
    result = AgentResult(
        agent_run_id="run-1",
        agent_id="pathfinder",
        agent_kind="recon",
        outcome="completed",
        summary="Pathfinder found two exposed services.",
        key_findings=["HTTP exposed on 80"],
        evidence_refs=[{"kind": "artifact", "path": "/workspace/artifacts/nmap.xml"}],
        tools_used=["nmap"],
        limitations=["UDP was not scanned"],
        recommended_next_steps=["Review HTTP service headers"],
        final_checkpoint_id="checkpoint-1",
    )
    result_projection = AgentResultProjection.from_result(
        result,
        agent_display_name="Pathfinder",
    )
    lifecycle_projection = AgentRunLifecycleProjection(
        agent_run_id="run-1",
        agent_id="pathfinder",
        agent_kind="recon",
        agent_display_name="Pathfinder",
        agent_icon_key="pathfinder",
        status="completed",
        lifecycle_version=3,
        task_id=42,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        assignment=assignment,
        result=result_projection,
    )

    with pytest.raises(TypeError):
        assignment.relevant_context["chain_of_thought"] = "secret reasoning"
    with pytest.raises(TypeError):
        assignment.runtime_identity.feature_flags["raw_tool_output"] = True
    with pytest.raises(ValidationError, match="frozen"):
        assignment.runtime_identity.credential_ref.credential_id = "cred-2"
    with pytest.raises(AttributeError):
        assignment.targets.append("10.0.0.11")  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        result.key_findings.append("chain_of_thought: secret")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        result.evidence_refs[0]["raw_tool_output"] = "secret"
    with pytest.raises(AttributeError):
        result_projection.tools_used.append("raw_tool_output")  # type: ignore[attr-defined]

    dumped_lifecycle = json.dumps(lifecycle_projection.model_dump(mode="json"))
    dumped_result = json.dumps(result_projection.model_dump(mode="json"))
    assert "chain_of_thought" not in dumped_lifecycle
    assert "raw_tool_output" not in dumped_lifecycle
    assert "chain_of_thought" not in dumped_result
    assert "raw_tool_output" not in dumped_result


def test_result_and_lifecycle_projections_exclude_raw_activity() -> None:
    result = AgentResult(
        agent_run_id="run-1",
        agent_id="pathfinder",
        agent_kind="recon",
        outcome="completed",
        summary="Pathfinder found two exposed services.",
        key_findings=["HTTP exposed on 80", "HTTPS exposed on 443"],
        evidence_refs=[{"kind": "artifact", "path": "/workspace/artifacts/nmap.xml"}],
        tools_used=["nmap"],
        limitations=["UDP was not scanned"],
        recommended_next_steps=["Review HTTP service headers"],
        final_checkpoint_id="checkpoint-1",
    )

    result_projection = AgentResultProjection.from_result(
        result,
        agent_display_name="Pathfinder",
    )
    lifecycle_projection = AgentRunLifecycleProjection(
        agent_run_id="run-1",
        agent_id="pathfinder",
        agent_kind="recon",
        agent_display_name="Pathfinder",
        agent_icon_key="pathfinder",
        status="completed",
        lifecycle_version=3,
        task_id=42,
        conversation_id="conversation-1",
        parent_turn_id="turn-1",
        parent_run_id="parent-run-1",
        assignment=_assignment(),
        result=result_projection,
    )

    dumped_result = result_projection.model_dump()
    dumped_lifecycle = lifecycle_projection.model_dump()

    assert "raw_tool_output" not in dumped_result
    assert "chain_of_thought" not in dumped_result
    assert "raw_tool_output" not in dumped_lifecycle
    assert dumped_result["agent_display_name"] == "Pathfinder"


def test_lifecycle_projection_requires_non_empty_display_metadata() -> None:
    with pytest.raises(ValidationError, match="agent_display_name"):
        AgentRunLifecycleProjection(
            agent_run_id="run-1",
            agent_id="pathfinder",
            agent_kind="recon",
            agent_display_name=" ",
            agent_icon_key="pathfinder",
            status="running",
            lifecycle_version=1,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
        )


def test_lifecycle_projection_requires_non_empty_icon_metadata() -> None:
    with pytest.raises(ValidationError, match="icon_key"):
        AgentRunLifecycleProjection(
            agent_run_id="run-1",
            agent_id="pathfinder",
            agent_kind="recon",
            agent_display_name="Pathfinder",
            agent_icon_key=" ",
            status="running",
            lifecycle_version=1,
            task_id=42,
            conversation_id="conversation-1",
            parent_turn_id="turn-1",
        )
