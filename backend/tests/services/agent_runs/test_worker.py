"""Tests for the generic process-local subagent worker extraction."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from agent.subagents.definition import SubagentDefinition, load_subagent_definitions
from agent.subagents.registry import SubagentRegistry
from agent.subagents.runtime.model import SUBAGENT_RESULT_METADATA_KEY
from agent.subagents.runtime.state import build_subagent_initial_state
from backend.services.agent_runs.contracts import (
    AgentAssignment,
    AgentResult,
    AgentRuntimeIdentity,
)
from backend.services.agent_runs.launcher import SubagentRunFailed
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.worker import (
    ProcessLocalAgentRunWorker,
    extract_subagent_result_from_state,
    prepare_subagent_child_config,
    resolve_definition_for_assignment,
)
from backend.services.langgraph_chat.execution.graph_executor import GraphExecutionResult


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
        relevant_context={
            "ticket": "ENG-123",
            "turn_sequence": 4,
            "agent_mode": "full_access",
        },
        runtime_identity=_runtime_identity(),
    )


def _result() -> AgentResult:
    return AgentResult(
        agent_run_id="run-1",
        agent_id="pathfinder",
        agent_kind="recon",
        outcome="completed",
        summary="Pathfinder found one live host.",
        key_findings=["10.0.0.10 responded to probes."],
        tools_used=["fping"],
    )


def _final_state() -> dict[str, Any]:
    return {
        "facts": {
            "metadata": {
                SUBAGENT_RESULT_METADATA_KEY: _result().model_dump(mode="json")
            }
        },
        "trace": {
            "usage_records": [
                {
                    "source": "subagent_runtime_model",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                }
            ]
        },
    }


class _FakeCheckpointerService:
    def __init__(self) -> None:
        self.task_ids: list[int] = []

    @asynccontextmanager
    async def get_checkpointer(self, task_id: int) -> Any:
        self.task_ids.append(task_id)
        yield "checkpoint"


class _FakeExecutor:
    def __init__(self, result: GraphExecutionResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def stream_graph(
        self,
        compiled_graph: Any,
        graph_input: Any,
        config: dict[str, Any],
        task_id: int,
        state_container: Any = None,
        should_cancel: Any = None,
    ) -> GraphExecutionResult:
        self.calls.append(
            {
                "compiled_graph": compiled_graph,
                "graph_input": graph_input,
                "config": config,
                "task_id": task_id,
                "state_container": state_container,
                "should_cancel": should_cancel,
                "cancelled": should_cancel(),
            }
        )
        return self.result


@pytest.mark.asyncio
async def test_generic_worker_builds_definition_configured_graph_input_config_and_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.services.agent_runs.worker as worker_module

    definition = _pathfinder_definition()
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    checkpointers = _FakeCheckpointerService()
    executor = _FakeExecutor(GraphExecutionResult(final_state=_final_state()))
    build_calls: list[tuple[str, Any]] = []

    def _fake_build_subagent_graph(
        actual_definition: SubagentDefinition,
        *,
        checkpointer: Any = None,
    ) -> str:
        build_calls.append((actual_definition.id, checkpointer))
        return "compiled-subagent"

    monkeypatch.setattr(
        worker_module,
        "build_subagent_graph",
        _fake_build_subagent_graph,
    )

    worker = ProcessLocalAgentRunWorker(
        registry=registry,
        definition_registry=SubagentRegistry([definition]),
        checkpointer_service=checkpointers,
        executor=executor,
    )

    completion = await worker(
        assignment=assignment,
        runtime_config={
            "configurable": {
                "runtime_projection": {
                    "tenant_id": 7,
                    "credential_ref": {"credential_id": "must-not-cross"},
                }
            }
        },
        graph_thread_id="child-thread-1",
        is_cancel_requested=_not_cancelled,
    )

    assert completion.result == _result()
    assert completion.graph_thread_id == "child-thread-1"
    assert completion.usage_records[0]["agent_run_id"] == "run-1"
    assert completion.usage_records[0]["tenant_id"] == 7
    assert completion.usage_records[0]["task_id"] == 42
    assert completion.usage_records[0]["user_id"] == 3
    assert completion.usage_records[0]["conversation_id"] == "conversation-1"
    assert completion.usage_records[0]["turn_sequence"] == 4
    assert checkpointers.task_ids == [42]
    assert build_calls == [("pathfinder", "checkpoint")]
    [call] = executor.calls
    assert call["compiled_graph"] == "compiled-subagent"
    assert call["graph_input"] == build_subagent_initial_state(
        definition=definition,
        assignment=assignment,
        graph_thread_id="child-thread-1",
    )
    assert call["config"] == prepare_subagent_child_config(
        {
            "configurable": {
                "runtime_projection": {
                    "tenant_id": 7,
                    "credential_ref": {"credential_id": "must-not-cross"},
                }
            }
        },
        assignment=assignment,
        graph_thread_id="child-thread-1",
    )
    assert "credential_ref" not in call["config"]["configurable"][
        "graph_runtime_context"
    ]
    assert call["task_id"] == 42
    assert call["state_container"] is None
    assert call["cancelled"] is False


def test_generic_worker_resolves_definition_by_assignment_kind() -> None:
    definition = _pathfinder_definition()

    assert (
        resolve_definition_for_assignment(
            SubagentRegistry([definition]),
            assignment=_assignment(),
        )
        == definition
    )


def test_generic_worker_rejects_missing_assignment_agent_id() -> None:
    definition = _pathfinder_definition()
    other_definition = replace(
        definition,
        id="otheragent",
        display_name="Other Agent",
        icon="otheragent",
    )

    with pytest.raises(RuntimeError, match="No subagent definition matches"):
        resolve_definition_for_assignment(
            SubagentRegistry([other_definition]),
            assignment=_assignment(),
        )


def test_generic_result_extraction_reads_definition_owned_result() -> None:
    assert extract_subagent_result_from_state(
        _final_state(),
        assignment=_assignment(),
    ) == _result()


@pytest.mark.asyncio
async def test_generic_worker_failure_preserves_graph_usage_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.services.agent_runs.worker as worker_module

    definition = _pathfinder_definition()
    registry = ProcessLocalAgentRunRegistry()
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id="child-thread-1")
    checkpointers = _FakeCheckpointerService()
    final_state = {
        "trace": {
            "usage_records": [
                {
                    "source": "subagent_runtime_model",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                }
            ]
        }
    }
    executor = _FakeExecutor(GraphExecutionResult(final_state=final_state))

    monkeypatch.setattr(
        worker_module,
        "build_subagent_graph",
        lambda _definition, *, checkpointer=None: "compiled-subagent",
    )
    worker = ProcessLocalAgentRunWorker(
        registry=registry,
        definition_registry=SubagentRegistry([definition]),
        checkpointer_service=checkpointers,
        executor=executor,
    )

    with pytest.raises(SubagentRunFailed) as exc_info:
        await worker(
            assignment=assignment,
            runtime_config={"configurable": {}},
            graph_thread_id="child-thread-1",
            is_cancel_requested=_not_cancelled,
        )

    assert exc_info.value.execution_result.final_state == final_state


async def _not_cancelled() -> bool:
    return False
