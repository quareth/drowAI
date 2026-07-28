"""Tests for safe subagent child LangGraph execution config construction."""

from __future__ import annotations

import json
import os
from typing import Any

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from agent.graph.graph_names import GRAPH_NAME_SUBAGENT
from agent.subagents.definition import load_subagent_definitions
from agent.subagents.runtime.graph import initialize_subagent_state
from agent.subagents.runtime.state import build_subagent_initial_state
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentCredentialReference,
    AgentRuntimeIdentity,
)
from backend.services.agent_runs.execution_config import (
    ChildExecutionConfigError,
    build_child_execution_config,
)
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.langgraph_chat.checkpoint.thread_identity import (
    format_graph_thread_id,
    generate_graph_thread_id,
)
from backend.services.langgraph_chat.contracts import (
    ChatInputs,
    ExecutionMode,
    LangGraphRuntimeConfig,
)


def _runtime_identity(
    *,
    runtime_placement_mode: str = "runner",
    runner_id: str | None = "runner-1",
    execution_site_id: str | None = "site-1",
) -> AgentRuntimeIdentity:
    return AgentRuntimeIdentity(
        tenant_id=7,
        task_id=42,
        user_id=3,
        workspace_id="task-42",
        workspace_path="/workspace/task-42",
        runtime_placement_mode=runtime_placement_mode,
        actor_type="agent",
        actor_id="langgraph",
        runner_id=runner_id,
        execution_site_id=execution_site_id,
        provider="openai",
        model="gpt-5.2-mini",
        reasoning_effort="medium",
        feature_flags={},
        credential_ref=AgentCredentialReference(
            provider="openai",
            credential_id="cred-1",
        ),
    )


def _assignment(
    *,
    runtime_identity: AgentRuntimeIdentity | None = None,
) -> AgentAssignment:
    return AgentAssignment(
        assignment_id="assignment-1",
        agent_run_id="scout-run-1",
        agent_id="pathfinder",
        agent_kind="recon",
        task_id=42,
        tenant_id=7,
        conversation_id="conv-42",
        parent_turn_id="turn-42",
        parent_graph_thread_id="a" * 32,
        objective="Scan 10.0.0.10",
        targets=["10.0.0.10"],
        suggested_capabilities=["port_scan"],
        scope_summary="Targets: 10.0.0.10",
        relevant_context={"classifier_label": "direct_executor"},
        runtime_identity=runtime_identity or _runtime_identity(),
    )


def _runtime_config(
    *,
    metadata_overrides: dict[str, Any] | None = None,
    runtime_placement_mode: str = "runner",
    runner_id: str | None = "runner-1",
    execution_site_id: str | None = "site-1",
) -> LangGraphRuntimeConfig:
    metadata: dict[str, Any] = {
        "tenant_id": 7,
        "graph_thread_id": "a" * 32,
        "runtime_placement_mode": runtime_placement_mode,
        "workspace_id": "task-42",
        "workspace_path": "/workspace/task-42",
        "actor_type": "agent",
        "actor_id": "langgraph",
        "runner_id": runner_id,
        "execution_site_id": execution_site_id,
        "api_key": "must-not-cross-boundary",
    }
    if metadata_overrides:
        metadata.update(metadata_overrides)
    return LangGraphRuntimeConfig(
        chat_inputs=ChatInputs(
            task_id=42,
            user_id=3,
            message="Scan 10.0.0.10",
            conversation_id="conv-42",
            history=[],
            requested_mode=ExecutionMode.SIMPLE_TOOL,
            llm_runtime_selection={
                "schema_version": 2,
                "deployment_ref": {
                    "deployment_id": "11111111-1111-4111-8111-111111111111",
                    "expected_revision": 3,
                },
                "preferred_route_id": "22222222-2222-4222-8222-222222222222",
                "reasoning_effort": "medium",
                "legacy_provider": "openai",
                "legacy_model": "gpt-5.2-mini",
                "resolved_client": object(),
            },
        ),
        execution_mode=ExecutionMode.SIMPLE_TOOL,
        metadata=metadata,
        runtime_services=object(),
    )


@pytest.mark.asyncio
async def test_child_execution_config_inherits_runner_identity_without_live_objects() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    child_graph_thread_id = generate_graph_thread_id()
    await registry.register(assignment, graph_thread_id=child_graph_thread_id)

    config = await build_child_execution_config(
        assignment=assignment,
        runtime_config=_runtime_config(metadata_overrides={"run_id": "run-42"}),
        registry=registry,
        graph_thread_id=child_graph_thread_id,
    )

    json.dumps(config)
    configurable = config["configurable"]
    projection = configurable["runtime_projection"]
    assert configurable["thread_id"] == format_graph_thread_id(
        child_graph_thread_id,
        task_id=42,
    )
    assert configurable["graph_name"] == GRAPH_NAME_SUBAGENT
    assert configurable["producer_type"] == "subagent"
    assert configurable["agent_run_id"] == "scout-run-1"
    assert configurable["agent_kind"] == "recon"
    assert configurable["agent_display_name"] == "Pathfinder"
    assert configurable["parent_turn_id"] == "turn-42"
    assert configurable["parent_run_id"] == "run-42"
    assert configurable["internal_only"] is False
    assert configurable["lifecycle_version"] == 1
    assert projection["tenant_id"] == 7
    assert projection["task_id"] == 42
    assert projection["user_id"] == 3
    assert projection["graph_thread_id"] == child_graph_thread_id
    assert projection["runtime_placement_mode"] == "runner"
    assert projection["runner_id"] == "runner-1"
    assert projection["execution_site_id"] == "site-1"
    assert projection["workspace_id"] == "task-42"
    assert projection["agent_run_id"] == "scout-run-1"
    assert projection["parent_run_id"] == "run-42"
    assert projection["credential_ref"] == {
        "provider": "openai",
        "credential_id": "cred-1",
    }
    assert configurable["llm_runtime_selection"] == {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": "11111111-1111-4111-8111-111111111111",
            "expected_revision": 3,
        },
        "preferred_route_id": "22222222-2222-4222-8222-222222222222",
        "reasoning_effort": "medium",
        "legacy_provider": "openai",
        "legacy_model": "gpt-5.2-mini",
    }
    assert "runtime_services" not in configurable
    assert "api_key" not in json.dumps(config)
    assert "resolved_client" not in json.dumps(config)


@pytest.mark.asyncio
async def test_child_execution_config_preserves_explicit_local_lane() -> None:
    registry = ProcessLocalAgentRunRegistry()
    identity = _runtime_identity(
        runtime_placement_mode="local",
        runner_id=None,
        execution_site_id=None,
    )
    assignment = _assignment(runtime_identity=identity)
    child_graph_thread_id = generate_graph_thread_id()
    await registry.register(assignment, graph_thread_id=child_graph_thread_id)

    config = await build_child_execution_config(
        assignment=assignment,
        runtime_config=_runtime_config(
            runtime_placement_mode="local",
            runner_id=None,
            execution_site_id=None,
        ),
        registry=registry,
        graph_thread_id=child_graph_thread_id,
    )

    projection = config["configurable"]["runtime_projection"]
    assert projection["runtime_placement_mode"] == "local"
    assert "runner_id" not in projection
    assert "execution_site_id" not in projection


@pytest.mark.asyncio
async def test_child_execution_config_fails_when_parent_identity_differs() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    child_graph_thread_id = generate_graph_thread_id()
    await registry.register(assignment, graph_thread_id=child_graph_thread_id)

    with pytest.raises(ChildExecutionConfigError, match="tenant_id mismatch"):
        await build_child_execution_config(
            assignment=assignment,
            runtime_config=_runtime_config(metadata_overrides={"tenant_id": 8}),
            registry=registry,
            graph_thread_id=child_graph_thread_id,
        )


@pytest.mark.asyncio
async def test_child_execution_config_fails_when_child_thread_not_registered() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id=generate_graph_thread_id())

    with pytest.raises(ChildExecutionConfigError, match="child thread"):
        await build_child_execution_config(
            assignment=assignment,
            runtime_config=_runtime_config(),
            registry=registry,
            graph_thread_id=generate_graph_thread_id(),
        )


@pytest.mark.asyncio
async def test_child_execution_config_thread_id_initializes_subagent_state() -> None:
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    child_graph_thread_id = generate_graph_thread_id()
    await registry.register(assignment, graph_thread_id=child_graph_thread_id)

    config = await build_child_execution_config(
        assignment=assignment,
        runtime_config=_runtime_config(),
        registry=registry,
        graph_thread_id=child_graph_thread_id,
    )
    definition = next(
        definition
        for definition in load_subagent_definitions()
        if definition.id == "pathfinder"
    )
    state = build_subagent_initial_state(
        definition=definition,
        assignment=assignment,
        graph_thread_id=child_graph_thread_id,
    )

    update = initialize_subagent_state(definition, state, config=config)

    assert update["facts"]["metadata"]["graph_thread_id"] == child_graph_thread_id
