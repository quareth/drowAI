"""Tests for subagent HITL continuation registry verification."""

from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from agent.graph.graph_names import GRAPH_NAME_SUBAGENT
from backend.services.agent_runs import continuation
from backend.services.agent_runs.continuation import (
    SUBAGENT_RECOVERY_ERROR,
    SubagentContinuationError,
    SubagentInterruptTicketSnapshot,
    mark_subagent_running,
    mark_subagent_waiting_for_approval,
    prepare_subagent_resume,
)
from backend.services.agent_runs.contracts import AgentAssignment, AgentRuntimeIdentity
from backend.services.agent_runs.registry import ProcessLocalAgentRunRegistry
from backend.services.agent_runs.worker import mark_subagent_completed_from_state
from agent.subagents.runtime.model import SUBAGENT_RESULT_METADATA_KEY
from backend.services.langgraph_chat.checkpoint.continuation_service import (
    CheckpointContinuationService,
)
from backend.services.langgraph_chat.contracts import LangGraphChatResult
from backend.services.langgraph_chat.checkpoint.interrupt_state_service import (
    InterruptStateService,
)


def _runtime_identity() -> AgentRuntimeIdentity:
    return AgentRuntimeIdentity(
        tenant_id=7,
        task_id=42,
        user_id=3,
        workspace_id="task-42",
        workspace_path="/workspace/task-42",
        runtime_placement_mode="runner",
        actor_type="agent",
        actor_id="langgraph",
        runner_id="runner-1",
        execution_site_id="site-1",
    )


def _assignment() -> AgentAssignment:
    return AgentAssignment(
        assignment_id="assignment-1",
        agent_run_id="pathfinder-run-1",
        agent_id="pathfinder",
        agent_kind="recon",
        task_id=42,
        tenant_id=7,
        conversation_id="conv-42",
        parent_turn_id="turn-42",
        parent_graph_thread_id="b" * 32,
        objective="Scan 10.0.0.10",
        targets=["10.0.0.10"],
        suggested_capabilities=["port_scan"],
        relevant_context={},
        runtime_identity=_runtime_identity(),
    )


@pytest.mark.asyncio
async def test_prepare_subagent_resume_uses_ticket_thread_and_updates_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    child_thread = "a" * 32
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id=child_thread)
    await registry.mark_waiting_for_approval(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
        accounted_usage_record_count=1,
    )

    monkeypatch.setattr(
        continuation,
        "_load_subagent_resume_ticket",
        lambda **_kwargs: SubagentInterruptTicketSnapshot(
            graph_name=GRAPH_NAME_SUBAGENT,
            thread_id=f"graph-{child_thread}",
            checkpoint_id="cp-ticket",
        ),
    )

    context = await prepare_subagent_resume(
        registry=registry,
        tenant_id=7,
        task_id=42,
        graph_name=GRAPH_NAME_SUBAGENT,
        interrupt_id="interrupt-1",
        checkpoint_id="stale-client-checkpoint",
    )

    assert context is not None
    assert context.graph_thread_id == child_thread
    assert context.checkpoint_id == "cp-ticket"
    await mark_subagent_running(registry=registry, context=context)
    running = await registry.get(tenant_id=7, task_id=42, agent_run_id="pathfinder-run-1")
    assert running is not None
    assert running.status == "running"

    await mark_subagent_waiting_for_approval(registry=registry, context=context)
    waiting = await registry.get(tenant_id=7, task_id=42, agent_run_id="pathfinder-run-1")
    assert waiting is not None
    assert waiting.status == "waiting_for_approval"


@pytest.mark.asyncio
async def test_prepare_subagent_resume_rejects_legacy_scout_ticket_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    child_thread = "a" * 32
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id=child_thread)
    await registry.mark_waiting_for_approval(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
        accounted_usage_record_count=1,
    )

    monkeypatch.setattr(
        continuation,
        "_load_subagent_resume_ticket",
        lambda **_kwargs: SubagentInterruptTicketSnapshot(
            graph_name="scout_recon",
            thread_id=f"graph-{child_thread}",
            checkpoint_id="cp-ticket",
        ),
    )

    assert not continuation.is_subagent_graph_name("scout_recon")
    with pytest.raises(SubagentContinuationError, match=SUBAGENT_RECOVERY_ERROR):
        await prepare_subagent_resume(
            registry=registry,
            tenant_id=7,
            task_id=42,
            graph_name=GRAPH_NAME_SUBAGENT,
            interrupt_id="interrupt-1",
            checkpoint_id=None,
        )


@pytest.mark.asyncio
async def test_prepare_subagent_resume_fails_explicitly_without_live_registry_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    child_thread = "a" * 32
    monkeypatch.setattr(
        continuation,
        "_load_subagent_resume_ticket",
        lambda **_kwargs: SubagentInterruptTicketSnapshot(
            graph_name=GRAPH_NAME_SUBAGENT,
            thread_id=f"graph-{child_thread}",
            checkpoint_id="cp-ticket",
        ),
    )

    with pytest.raises(SubagentContinuationError, match=SUBAGENT_RECOVERY_ERROR):
        await prepare_subagent_resume(
            registry=registry,
            tenant_id=7,
            task_id=42,
            graph_name=GRAPH_NAME_SUBAGENT,
            interrupt_id="interrupt-1",
            checkpoint_id=None,
        )


@pytest.mark.asyncio
async def test_prepare_subagent_resume_matches_registered_thread_without_recon_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    child_thread = "a" * 32
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id=child_thread)
    waiting = await registry.mark_waiting_for_approval(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
    )
    registry._runs[(7, 42, "pathfinder-run-1")] = replace(
        waiting,
        agent_id="cartographer",
        agent_kind="asset_mapper",  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        continuation,
        "_load_subagent_resume_ticket",
        lambda **_kwargs: SubagentInterruptTicketSnapshot(
            graph_name=GRAPH_NAME_SUBAGENT,
            thread_id=f"graph-{child_thread}",
            checkpoint_id="cp-ticket",
        ),
    )

    context = await prepare_subagent_resume(
        registry=registry,
        tenant_id=7,
        task_id=42,
        graph_name=GRAPH_NAME_SUBAGENT,
        interrupt_id="interrupt-1",
        checkpoint_id=None,
    )

    assert context is not None
    assert context.entry.agent_id == "cartographer"
    assert context.entry.agent_kind == "asset_mapper"
    assert context.graph_thread_id == child_thread


@pytest.mark.asyncio
async def test_prepare_subagent_resume_ignores_non_subagent_graph() -> None:
    context = await prepare_subagent_resume(
        registry=ProcessLocalAgentRunRegistry(),
        tenant_id=None,
        task_id=42,
        graph_name="simple_tool",
        interrupt_id=None,
        checkpoint_id=None,
    )

    assert context is None


@pytest.mark.asyncio
async def test_checkpoint_continuation_compiles_subagent_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = object()
    monkeypatch.setattr(
        "agent.subagents.runtime.graph.build_subagent_graph",
        lambda _definition, *, checkpointer: compiled,
    )
    service = CheckpointContinuationService(
        checkpointer_service=object(),
        executor=object(),
        streaming_adapter=object(),
        build_checkpoint_execution_config=lambda **_kwargs: {},
        hydrate_container_from_checkpoint_state=lambda *_args, **_kwargs: None,
        extract_resume_conversation_id=lambda _state: "",
        resolve_resume_turn_number=lambda **_kwargs: 0,
        persist_chat_message_from_container=lambda **_kwargs: None,
        build_result=lambda **_kwargs: None,
    )

    result = await service._compile_graph_for_name(
        task_id=42,
        graph_name=GRAPH_NAME_SUBAGENT,
        checkpointer=object(),
    )

    assert result is compiled


@pytest.mark.asyncio
async def test_resume_from_interrupt_keeps_subagent_waiting_after_next_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    child_thread = "a" * 32
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id=child_thread)
    await registry.mark_waiting_for_approval(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
    )
    monkeypatch.setattr(
        continuation,
        "_load_subagent_resume_ticket",
        lambda **_kwargs: SubagentInterruptTicketSnapshot(
            graph_name=GRAPH_NAME_SUBAGENT,
            thread_id=f"graph-{child_thread}",
            checkpoint_id="cp-ticket",
        ),
    )
    service = _build_subagent_resume_service(
        registry=registry,
        final_state=_interactive_state(),
        interrupted=True,
    )
    monkeypatch.setattr(
        service,
        "_compile_graph_for_name",
        _compile_stub,
    )

    result = await service.resume_from_interrupt(
        task_id=42,
        tenant_id=7,
        graph_name=GRAPH_NAME_SUBAGENT,
        interrupt_id="interrupt-1",
        checkpoint_id="stale-client-checkpoint",
        response={"approved": True},
    )

    assert result.metadata["interrupt"] is True
    entry = await registry.get(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
    )
    assert entry is not None
    assert entry.status == "waiting_for_approval"


@pytest.mark.asyncio
async def test_resume_from_interrupt_returns_new_child_usage_on_next_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    child_thread = "a" * 32
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id=child_thread)
    await registry.mark_waiting_for_approval(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
        accounted_usage_record_count=1,
    )
    monkeypatch.setattr(
        continuation,
        "_load_subagent_resume_ticket",
        lambda **_kwargs: SubagentInterruptTicketSnapshot(
            graph_name=GRAPH_NAME_SUBAGENT,
            thread_id=f"graph-{child_thread}",
            checkpoint_id="cp-ticket",
        ),
    )
    service = _build_subagent_resume_service(
        registry=registry,
        final_state=_interactive_state(
            usage_records=[
                {
                    "source": "subagent_runtime_model",
                    "prompt_tokens": 50,
                    "completion_tokens": 50,
                    "total_tokens": 100,
                    "provider": "openai",
                    "model": "gpt-5.2-mini",
                    "api_surface": "responses",
                    "request_mode": "non_streaming",
                    "cache_reporting": "reported",
                },
                {
                    "source": "subagent_runtime_model",
                    "prompt_tokens": 11,
                    "completion_tokens": 4,
                    "total_tokens": 15,
                    "provider": "openai",
                    "model": "gpt-5.2-mini",
                    "api_surface": "responses",
                    "request_mode": "non_streaming",
                    "cache_reporting": "reported",
                },
            ],
        ),
        interrupted=True,
    )
    monkeypatch.setattr(
        service,
        "_compile_graph_for_name",
        _compile_stub,
    )

    result = await service.resume_from_interrupt(
        task_id=42,
        tenant_id=7,
        graph_name=GRAPH_NAME_SUBAGENT,
        interrupt_id="interrupt-1",
        checkpoint_id="stale-client-checkpoint",
        response={"approved": True},
    )

    assert result.metadata["interrupt"] is True
    assert result.usage is not None
    assert len(result.usage) == 1
    [usage] = result.usage
    assert usage.usage.total_tokens == 15
    assert usage.metadata.execution_branch == "subagent_child"
    assert usage.metadata.role == "subagent"
    assert usage.metadata.node_name == "subagent_runtime_model"
    entry = await registry.get(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
    )
    assert entry is not None
    assert entry.status == "waiting_for_approval"
    assert entry.accounted_usage_record_count == 2


@pytest.mark.asyncio
async def test_resume_from_interrupt_marks_subagent_completed_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    child_thread = "a" * 32
    assignment = _assignment()
    await registry.register(assignment, graph_thread_id=child_thread)
    await registry.mark_waiting_for_approval(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
        accounted_usage_record_count=1,
    )
    monkeypatch.setattr(
        continuation,
        "_load_subagent_resume_ticket",
        lambda **_kwargs: SubagentInterruptTicketSnapshot(
            graph_name=GRAPH_NAME_SUBAGENT,
            thread_id=f"graph-{child_thread}",
            checkpoint_id="cp-ticket",
        ),
    )
    service = _build_subagent_resume_service(
        registry=registry,
        final_state=_interactive_state(
            metadata={
                SUBAGENT_RESULT_METADATA_KEY: {
                    "agent_run_id": "pathfinder-run-1",
                    "agent_id": "pathfinder",
                    "agent_kind": "recon",
                    "outcome": "completed",
                    "summary": "Pathfinder found HTTP on port 80.",
                    "key_findings": ["HTTP exposed on 80"],
                    "evidence_refs": [
                        {"kind": "artifact", "path": "/workspace/nmap.xml"}
                    ],
                    "tools_used": ["nmap"],
                    "limitations": [],
                    "recommended_next_steps": ["Review HTTP service details"],
                    "final_checkpoint_id": "cp-final",
                }
            },
            final_text="Pathfinder found HTTP on port 80.",
            usage_records=[
                {
                    "source": "subagent_runtime_model",
                    "prompt_tokens": 50,
                    "completion_tokens": 50,
                    "total_tokens": 100,
                    "provider": "openai",
                    "model": "gpt-5.2-mini",
                    "api_surface": "responses",
                    "request_mode": "non_streaming",
                    "cache_reporting": "reported",
                },
                {
                    "source": "subagent_runtime_model",
                    "prompt_tokens": 11,
                    "completion_tokens": 4,
                    "total_tokens": 15,
                    "provider": "openai",
                    "model": "gpt-5.2-mini",
                    "api_surface": "responses",
                    "request_mode": "non_streaming",
                    "cache_reporting": "reported",
                }
            ],
        ),
        interrupted=False,
    )
    monkeypatch.setattr(
        service,
        "_compile_graph_for_name",
        _compile_stub,
    )

    result = await service.resume_from_interrupt(
        task_id=42,
        tenant_id=7,
        graph_name=GRAPH_NAME_SUBAGENT,
        interrupt_id="interrupt-1",
        checkpoint_id="stale-client-checkpoint",
        response={"approved": True},
    )

    assert result.final_text == "Pathfinder found HTTP on port 80."
    entry = await registry.get(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
    )
    assert entry is not None
    assert entry.status == "completed"
    assert entry.result is not None
    assert entry.result.agent_run_id == "pathfinder-run-1"
    assert entry.result.final_checkpoint_id == "cp-final"
    assert result.usage is not None
    assert len(result.usage) == 1
    [usage] = result.usage
    assert usage.usage.total_tokens == 15
    assert usage.usage.model == "gpt-5.2-mini"
    assert usage.metadata.execution_branch == "subagent_child"
    assert usage.metadata.role == "subagent"
    assert usage.metadata.node_name == "subagent_runtime_model"


@pytest.mark.asyncio
async def test_mark_subagent_completed_from_state_returns_usage_identity_envelope() -> None:
    registry = ProcessLocalAgentRunRegistry()
    child_thread = "a" * 32
    assignment = _assignment()
    entry = await registry.register(assignment, graph_thread_id=child_thread)
    final_state = _interactive_state(
        metadata={
            SUBAGENT_RESULT_METADATA_KEY: {
                "agent_run_id": "pathfinder-run-1",
                "agent_id": "pathfinder",
                "agent_kind": "recon",
                "outcome": "completed",
                "summary": "Pathfinder found HTTP on port 80.",
            }
        },
        final_text="Pathfinder found HTTP on port 80.",
        usage_records=[
            {
                "source": "subagent_runtime_model",
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
            }
        ],
    )

    completion = await mark_subagent_completed_from_state(
        registry=registry,
        entry=entry,
        final_state=final_state,
    )

    assert completion.result.agent_run_id == "pathfinder-run-1"
    assert completion.tenant_id == 7
    assert completion.task_id == 42
    assert completion.user_id == 3
    assert completion.conversation_id == "conv-42"
    assert completion.agent_id == "pathfinder"
    assert completion.agent_run_id == "pathfinder-run-1"
    assert completion.graph_thread_id == child_thread
    assert completion.usage_records == (
        {
            "source": "subagent_runtime_model",
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
            "tenant_id": 7,
            "task_id": 42,
            "user_id": 3,
            "conversation_id": "conv-42",
            "provider": "unknown",
            "model": "unknown",
            "agent_id": "pathfinder",
            "agent_kind": "recon",
            "agent_run_id": "pathfinder-run-1",
            "graph_thread_id": child_thread,
            "parent_turn_id": "turn-42",
        },
    )


@pytest.mark.asyncio
async def test_interrupt_snapshot_hydrates_subagent_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _FakeCompiledGraph()
    monkeypatch.setattr(
        "agent.subagents.runtime.graph.build_subagent_graph",
        lambda _definition, *, checkpointer: compiled,
    )
    service = InterruptStateService(checkpointer_service=_FakeCheckpointerService())

    result = await service.get_pending_interrupt(
        task_id=42,
        graph_name=GRAPH_NAME_SUBAGENT,
        thread_id="graph-" + ("a" * 32),
    )

    assert result is not None
    assert result["graph_name"] == GRAPH_NAME_SUBAGENT
    assert result["thread_id"] == "graph-" + ("a" * 32)
    assert result["interrupt_id"] == "interrupt-1"
    assert result["checkpoint_id"] == "cp-1"


class _FakeCheckpointerService:
    def get_checkpointer(self, task_id: int) -> "_FakeCheckpointerContext":
        assert task_id == 42
        return _FakeCheckpointerContext()


class _FakeCheckpointerContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _FakeCompiledGraph:
    async def aget_state(self, config: dict[str, Any]) -> "_FakeSnapshot":
        assert config["configurable"]["thread_id"] == "graph-" + ("a" * 32)
        return _FakeSnapshot()


class _FakeInterrupt:
    value = {"type": "tool_approval", "interrupt_id": "interrupt-1"}
    resumable = True


class _FakeTask:
    interrupts = [_FakeInterrupt()]


class _FakeSnapshot:
    config = {"configurable": {"checkpoint_id": "cp-1"}}
    tasks = [_FakeTask()]


async def _compile_stub(**_kwargs: Any) -> object:
    return object()


def _interactive_state(
    *,
    metadata: dict[str, Any] | None = None,
    final_text: str | None = None,
    usage_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "facts": {
            "task_id": 42,
            "message": "Scan 10.0.0.10",
            "conversation_id": "conv-42",
            "metadata": metadata or {},
        },
        "trace": {
            "final_text": final_text,
            "usage_records": usage_records or [],
        },
    }


def _build_subagent_resume_service(
    *,
    registry: ProcessLocalAgentRunRegistry,
    final_state: dict[str, Any],
    interrupted: bool,
) -> CheckpointContinuationService:
    class _CheckpointerService:
        def get_checkpointer(self, task_id: int) -> "_FakeCheckpointerContext":
            assert task_id == 42
            return _FakeCheckpointerContext()

    class _Executor:
        async def stream_graph(
            self,
            _compiled: Any,
            _graph_input: Any,
            config: dict[str, Any],
            task_id: int,
            **_kwargs: Any,
        ) -> Any:
            assert task_id == 42
            assert config["configurable"]["thread_id"] == "graph-" + ("a" * 32)
            assert config["configurable"]["checkpoint_id"] == "cp-ticket"
            return SimpleNamespace(
                final_state=final_state,
                interrupted=interrupted,
                metadata={},
            )

    return CheckpointContinuationService(
        checkpointer_service=_CheckpointerService(),
        executor=_Executor(),
        streaming_adapter=object(),
        build_checkpoint_execution_config=lambda **kwargs: {
            "configurable": {
                "thread_id": f"graph-{kwargs['graph_thread_id']}",
                **(
                    {"checkpoint_id": str(kwargs["checkpoint_id"])}
                    if kwargs.get("checkpoint_id") is not None
                    else {}
                ),
            }
        },
        hydrate_container_from_checkpoint_state=lambda *_args, **_kwargs: None,
        extract_resume_conversation_id=lambda _state: "",
        resolve_resume_turn_number=lambda **_kwargs: 0,
        persist_chat_message_from_container=lambda **_kwargs: None,
        build_result=lambda **kwargs: LangGraphChatResult(
            final_text=kwargs["final_text"],
            conversation_id=kwargs["conversation_id"],
            interactive_state=kwargs["interactive_state"],
            metadata=kwargs["metadata"],
            usage=kwargs["usage"],
        ),
        agent_run_registry=registry,
    )
