"""Tests for subagent HITL continuation registry verification."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from agent.graph.graph_names import GRAPH_NAME_PARENT_HANDOFF, GRAPH_NAME_SUBAGENT
from agent.subagents.definition import SubagentDefinition
from agent.subagents.registry import SubagentRegistry, get_subagent_registry
from agent.subagents.runtime.model import SUBAGENT_RESULT_METADATA_KEY
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
from backend.services.agent_runs.parent_handoff_continuation import (
    ParentHandoffContinuationBroker,
)
from backend.services.agent_runs.worker import mark_subagent_completed_from_state
from backend.services.langgraph_chat.checkpoint.continuation_service import (
    CheckpointContinuationService,
)
from backend.services.langgraph_chat.contracts import LangGraphChatResult
from backend.services.langgraph_chat.execution.graph_executor import (
    GraphExecutionCancelled,
    GraphExecutionResult,
)
from backend.services.langgraph_chat.runtime.state_container import ChatStateContainer
from backend.services.langgraph_chat.checkpoint.interrupt_state_service import (
    InterruptStateService,
)
from backend.tests.agent_run_test_support import (
    build_agent_assignment,
    build_runtime_identity,
)


def _runtime_identity() -> AgentRuntimeIdentity:
    return build_runtime_identity(
        user_id=3,
        workspace_path="/workspace/task-42",
        actor_type="agent",
        actor_id="langgraph",
        provider=None,
        model=None,
        reasoning_effort=None,
    )


def _assignment() -> AgentAssignment:
    return build_agent_assignment(
        assignment_id="assignment-1",
        agent_run_id="pathfinder-run-1",
        conversation_id="conv-42",
        parent_turn_id="turn-42",
        parent_graph_thread_id="b" * 32,
        objective="Scan 10.0.0.10",
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
        assignment=waiting.assignment.model_copy(
            update={
                "agent_id": "cartographer",
                "agent_kind": "asset_mapper",
            }
        ),
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
async def test_checkpoint_continuation_compiles_parent_handoff_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = object()
    checkpointer = object()
    received: dict[str, Any] = {}

    def _build_parent_handoff_graph(*, checkpointer: Any) -> object:
        received["checkpointer"] = checkpointer
        return compiled

    monkeypatch.setattr(
        "agent.graph.builders.parent_handoff_builder.build_parent_handoff_graph",
        _build_parent_handoff_graph,
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
        graph_name=GRAPH_NAME_PARENT_HANDOFF,
        checkpointer=checkpointer,
    )

    assert result is compiled
    assert received == {"checkpointer": checkpointer}


@pytest.mark.asyncio
async def test_parent_handoff_resume_returns_execution_to_original_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = ParentHandoffContinuationBroker()
    state_container = ChatStateContainer()
    session = broker.open(
        task_id=42,
        thread_id="graph-" + ("a" * 32),
        state_container=state_container,
    )
    execution_result = GraphExecutionResult(
        final_state=_interactive_state(final_text="Parent PTR resumed."),
        metadata={"checkpoint_resume": True},
    )
    persist_calls: list[dict[str, Any]] = []

    class _Executor:
        async def stream_graph(
            self,
            _compiled: Any,
            _graph_input: Any,
            _config: dict[str, Any],
            task_id: int,
            **kwargs: Any,
        ) -> GraphExecutionResult:
            assert task_id == 42
            assert kwargs["state_container"] is state_container
            return execution_result

    service = CheckpointContinuationService(
        checkpointer_service=_FakeCheckpointerService(),
        executor=_Executor(),
        streaming_adapter=object(),
        build_checkpoint_execution_config=lambda **kwargs: {
            "configurable": {
                "thread_id": f"graph-{kwargs['graph_thread_id']}",
                "graph_name": kwargs["graph_name"],
            }
        },
        hydrate_container_from_checkpoint_state=lambda *_args, **_kwargs: None,
        extract_resume_conversation_id=lambda _state: "",
        resolve_resume_turn_number=lambda **_kwargs: 0,
        persist_chat_message_from_container=lambda **kwargs: persist_calls.append(
            kwargs
        ),
        build_result=lambda **_kwargs: None,
        parent_handoff_continuation_broker=broker,
    )
    monkeypatch.setattr(service, "_compile_graph_for_name", _compile_stub)

    result = await service.resume_from_interrupt(
        task_id=42,
        graph_thread_id="a" * 32,
        graph_name=GRAPH_NAME_PARENT_HANDOFF,
        response={"approved": True},
    )
    delivered = await broker.wait(session, should_cancel=lambda: False)

    assert delivered is execution_result
    assert result.final_text is None
    assert result.persistence_handled is True
    assert result.metadata["subagent_parent_continuation_pending"] is True
    assert result.metadata["graph_name"] == GRAPH_NAME_PARENT_HANDOFF
    assert result.metadata["checkpoint_resume"] is True
    assert persist_calls == []

    broker.close(session)


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
    persist_calls: list[dict[str, Any]] = []
    lifecycle_events: list[dict[str, Any]] = []
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
        persist_calls=persist_calls,
        lifecycle_events=lifecycle_events,
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

    assert result.final_text is None
    assert result.metadata["subagent_parent_continuation_pending"] is True
    assert persist_calls[-1]["final_message"] is None
    assert persist_calls[-1]["event_attribution"]["producer_type"] == "subagent"
    assert (
        persist_calls[-1]["event_attribution"]["agent_run_id"]
        == "pathfinder-run-1"
    )
    assert [event["agent_run"]["status"] for event in lifecycle_events] == [
        "running",
        "completed",
    ]
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
async def test_resume_from_interrupt_settles_child_stop_as_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle_events: list[dict[str, Any]] = []
    partial_state = _interactive_state(
        usage_records=[
            {
                "source": "subagent_runtime_model",
                "prompt_tokens": 8,
                "completion_tokens": 2,
                "total_tokens": 10,
            }
        ]
    )
    registry, service = await _resumable_service(
        monkeypatch,
        final_state=partial_state,
        lifecycle_events=lifecycle_events,
    )

    class _CancellingExecutor:
        async def stream_graph(self, *_args: Any, should_cancel: Any, **_kwargs: Any) -> Any:
            await registry.request_cancellation(
                tenant_id=7,
                task_id=42,
                agent_run_id="pathfinder-run-1",
            )
            assert should_cancel() is False
            await asyncio.sleep(0)
            assert should_cancel() is True
            raise GraphExecutionCancelled(
                GraphExecutionResult(final_state=partial_state)
            )

    service._executor = _CancellingExecutor()

    result = await service.resume_from_interrupt(
        task_id=42,
        tenant_id=7,
        graph_name=GRAPH_NAME_SUBAGENT,
        interrupt_id="interrupt-1",
        response={"approved": True},
        should_cancel=lambda: False,
    )

    entry = await registry.get(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
    )
    assert entry is not None
    assert entry.status == "cancelled"
    assert result.metadata["subagent_parent_continuation_pending"] is True
    assert result.metadata["status"] == "cancelled"
    assert result.usage is not None
    assert result.usage[0].usage.total_tokens == 10
    assert [event["agent_run"]["status"] for event in lifecycle_events] == [
        "running",
        "cancelled",
    ]


@pytest.mark.parametrize(
    ("child_cancelled", "expected_child_status"),
    ((False, "running"), (True, "cancelled")),
    ids=("parent-only", "parent-and-child"),
)
@pytest.mark.asyncio
async def test_resume_from_interrupt_preserves_parent_cancellation_precedence(
    monkeypatch: pytest.MonkeyPatch,
    child_cancelled: bool,
    expected_child_status: str,
) -> None:
    partial_result = GraphExecutionResult(final_state=_interactive_state())
    registry, service = await _resumable_service(
        monkeypatch,
        final_state=partial_result.final_state or {},
    )

    class _CancelledExecutor:
        async def stream_graph(self, *_args: Any, **_kwargs: Any) -> Any:
            if child_cancelled:
                await registry.request_cancellation(
                    tenant_id=7,
                    task_id=42,
                    agent_run_id="pathfinder-run-1",
                )
            raise GraphExecutionCancelled(partial_result)

    service._executor = _CancelledExecutor()

    with pytest.raises(GraphExecutionCancelled) as raised:
        await service.resume_from_interrupt(
            task_id=42,
            tenant_id=7,
            graph_name=GRAPH_NAME_SUBAGENT,
            interrupt_id="interrupt-1",
            response={"approved": True},
            should_cancel=lambda: True,
        )

    assert raised.value.execution_result is partial_result
    entry = await registry.get(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
    )
    assert entry is not None
    assert entry.status == expected_child_status


@pytest.mark.asyncio
async def test_resume_from_interrupt_honors_child_stop_after_last_executor_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_state = _interactive_state(
        metadata={
            SUBAGENT_RESULT_METADATA_KEY: {
                "agent_run_id": "pathfinder-run-1",
                "agent_id": "pathfinder",
                "agent_kind": "recon",
                "outcome": "completed",
                "summary": "This completion must lose the cancellation race.",
            }
        },
        final_text="done",
    )
    registry, service = await _resumable_service(
        monkeypatch,
        final_state=final_state,
    )

    class _LastPollExecutor:
        async def stream_graph(self, *_args: Any, **_kwargs: Any) -> Any:
            await registry.request_cancellation(
                tenant_id=7,
                task_id=42,
                agent_run_id="pathfinder-run-1",
            )
            return GraphExecutionResult(final_state=final_state)

    service._executor = _LastPollExecutor()

    result = await service.resume_from_interrupt(
        task_id=42,
        tenant_id=7,
        graph_name=GRAPH_NAME_SUBAGENT,
        interrupt_id="interrupt-1",
        response={"approved": True},
        should_cancel=lambda: False,
    )

    entry = await registry.get(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
    )
    assert entry is not None
    assert entry.status == "cancelled"
    assert result.metadata["status"] == "cancelled"


@pytest.mark.asyncio
async def test_resume_completion_ignores_lifecycle_publication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    final_state = _interactive_state(
        metadata={
            SUBAGENT_RESULT_METADATA_KEY: {
                "agent_run_id": "pathfinder-run-1",
                "agent_id": "pathfinder",
                "agent_kind": "recon",
                "outcome": "completed",
                "summary": "done",
            }
        },
        final_text="done",
    )
    registry, service = await _resumable_service(
        monkeypatch,
        final_state=final_state,
    )

    async def _failing_publisher(_task_id: int, event: dict[str, Any]) -> None:
        if event["agent_run"]["status"] == "completed":
            raise RuntimeError("stream persistence unavailable")

    service._agent_run_lifecycle_publisher = _failing_publisher

    result = await service.resume_from_interrupt(
        task_id=42,
        tenant_id=7,
        graph_name=GRAPH_NAME_SUBAGENT,
        interrupt_id="interrupt-1",
        response={"approved": True},
    )

    assert result.metadata["subagent_parent_continuation_pending"] is True
    entry = await registry.get(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
    )
    assert entry is not None
    assert entry.status == "completed"


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

    async def _publish_lifecycle(
        _task_id: int,
        _event: dict[str, Any],
    ) -> None:
        return None

    completion = await mark_subagent_completed_from_state(
        registry=registry,
        definition_registry=get_subagent_registry(),
        entry=entry,
        final_state=final_state,
        lifecycle_publisher=_publish_lifecycle,
    )

    assert completion.result.agent_run_id == "pathfinder-run-1"
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
async def test_interrupt_snapshot_hydrates_matching_subagent_graph_with_two_definitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(), graph_thread_id="b" * 32)
    matching_assignment = _assignment().model_copy(
        update={
            "assignment_id": "assignment-2",
            "agent_run_id": "cartographer-run-1",
            "agent_id": "cartographer",
        }
    )
    await registry.register(matching_assignment, graph_thread_id="a" * 32)
    await registry.mark_waiting_for_approval(
        tenant_id=7,
        task_id=42,
        agent_run_id="cartographer-run-1",
    )
    definition_registry = SubagentRegistry(
        (
            _definition("pathfinder", "Pathfinder"),
            _definition("cartographer", "Cartographer"),
        )
    )
    compiled = _FakeCompiledGraph()

    compiled_agent_ids: list[str] = []
    monkeypatch.setattr(
        "agent.subagents.runtime.graph.build_subagent_graph",
        lambda definition, *, checkpointer: (
            compiled_agent_ids.append(definition.id) or compiled
        ),
    )
    service = InterruptStateService(
        checkpointer_service=_FakeCheckpointerService(),
        agent_run_registry=registry,
        subagent_registry=definition_registry,
    )

    result = await service.get_pending_interrupt(
        task_id=42,
        tenant_id=7,
        graph_name=GRAPH_NAME_SUBAGENT,
        thread_id="graph-" + ("a" * 32),
    )

    assert result is not None
    assert result["graph_name"] == GRAPH_NAME_SUBAGENT
    assert result["thread_id"] == "graph-" + ("a" * 32)
    assert result["interrupt_id"] == "interrupt-1"
    assert result["checkpoint_id"] == "cp-1"
    assert compiled_agent_ids == ["cartographer"]


def _definition(agent_id: str, display_name: str) -> SubagentDefinition:
    return SubagentDefinition(
        schema_version=1,
        id=agent_id,
        display_name=display_name,
        kind="recon",
        description=f"{display_name} test definition.",
        ownership_boundary="Own only the assigned test objective.",
        supported_task_categories=("host_discovery",),
        excluded_task_categories=(),
        tool_ids=("information_gathering.network_discovery.nmap",),
        enabled=True,
        max_active_runs_per_task=1,
        max_iterations=1,
        max_tool_calls_per_iteration=1,
        requires_resolved_target=True,
        icon=agent_id,
        instructions=f"You are {display_name}.",
        runtime_role_prompt=None,
        runtime_boundary_rules=(),
    )


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


async def _waiting_registry() -> ProcessLocalAgentRunRegistry:
    registry = ProcessLocalAgentRunRegistry()
    await registry.register(_assignment(), graph_thread_id="a" * 32)
    await registry.mark_waiting_for_approval(
        tenant_id=7,
        task_id=42,
        agent_run_id="pathfinder-run-1",
    )
    return registry


def _stub_subagent_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        continuation,
        "_load_subagent_resume_ticket",
        lambda **_kwargs: SubagentInterruptTicketSnapshot(
            graph_name=GRAPH_NAME_SUBAGENT,
            thread_id="graph-" + ("a" * 32),
            checkpoint_id="cp-ticket",
        ),
    )


async def _resumable_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    final_state: dict[str, Any],
    lifecycle_events: list[dict[str, Any]] | None = None,
) -> tuple[ProcessLocalAgentRunRegistry, CheckpointContinuationService]:
    registry = await _waiting_registry()
    _stub_subagent_ticket(monkeypatch)
    service = _build_subagent_resume_service(
        registry=registry,
        final_state=final_state,
        interrupted=False,
        lifecycle_events=lifecycle_events,
    )
    monkeypatch.setattr(service, "_compile_graph_for_name", _compile_stub)
    return registry, service


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
    persist_calls: list[dict[str, Any]] | None = None,
    lifecycle_events: list[dict[str, Any]] | None = None,
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
            assert config["configurable"]["producer_type"] == "subagent"
            assert config["configurable"]["agent_run_id"] == "pathfinder-run-1"
            assert config["configurable"]["agent_id"] == "pathfinder"
            assert config["configurable"]["parent_turn_id"] == "turn-42"
            return SimpleNamespace(
                final_state=final_state,
                interrupted=interrupted,
                metadata={},
            )

    async def _publish_lifecycle(_task_id: int, event: dict[str, Any]) -> None:
        if lifecycle_events is not None:
            lifecycle_events.append(event)

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
        persist_chat_message_from_container=lambda **kwargs: (
            persist_calls.append(kwargs) if persist_calls is not None else None
        ),
        build_result=lambda **kwargs: LangGraphChatResult(
            final_text=kwargs["final_text"],
            conversation_id=kwargs["conversation_id"],
            interactive_state=kwargs["interactive_state"],
            metadata=kwargs["metadata"],
            usage=kwargs["usage"],
        ),
        agent_run_registry=registry,
        subagent_registry=get_subagent_registry(),
        agent_run_lifecycle_publisher=_publish_lifecycle,
    )
