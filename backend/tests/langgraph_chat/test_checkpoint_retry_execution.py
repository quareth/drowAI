"""Tests for checkpoint threading through retry execution.

These tests pin the contract that the retry worker entrypoint, the
orchestrator, and the LangGraph facade all forward the workflow's stored
``checkpoint_id`` plus a sanitized retry context end-to-end. The goal is
to prove that retry continuation is pinned to the checkpoint stored on
the workflow row at claim time (no fallback to "latest"), and that the
graph/prompt runtime can read ``retry_attempt`` / ``retry_max_attempts``
plus the sanitized ``previous_failure`` projection off the run config.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Mapping
from unittest.mock import AsyncMock

import pytest

from agent.graph.utils.retry_context import RetryContext, read_retry_context
from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome
from backend.services.langgraph_chat.compression.context_models import (
    CompressionRequiredError,
)
from backend.services.langgraph_chat.checkpoint.continuation_service import (
    CheckpointContinuationService,
)
from backend.services.langgraph_chat.checkpoint.execution_config import (
    build_checkpoint_execution_config,
)
from backend.services.langgraph_chat.contracts import LangGraphChatResult
from backend.services.langgraph_chat.facade import LangGraphChatFacade
from backend.services.langgraph_chat.execution.turn_service import (
    TurnExecutionService,
    run_checkpoint_retry_generation,
)

GRAPH_THREAD_ID = "a" * 32


def test_build_checkpoint_execution_config_emits_retry_context_fields() -> None:
    """Retry context surfaces canonical retry identity onto configurable."""
    retry_context: Dict[str, Any] = {
        "retry_attempt": 1,
        "retry_max_attempts": 2,
        "previous_failure": {
            "error_code": "tool_argument_invalid",
            "failure_stage": "graph_continuation",
            "graph_name": "simple_tool",
            "tool_name": "http_get",
            "tool_call_id": "call-77",
            "summary": "tool rejected malformed url argument",
        },
    }
    config = build_checkpoint_execution_config(
        task_id=7,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="simple_tool",
        checkpoint_id="ckpt-stable-abc123",
        retry_context=retry_context,
    )
    configurable = config["configurable"]
    assert configurable["checkpoint_id"] == "ckpt-stable-abc123"
    assert configurable["retry_attempt"] == 1
    assert configurable["retry_max_attempts"] == 2
    previous = configurable["previous_failure"]
    assert previous["error_code"] == "tool_argument_invalid"
    assert previous["tool_name"] == "http_get"
    bundled = configurable["retry_context"]
    assert bundled["retry_attempt"] == 1
    assert bundled["retry_max_attempts"] == 2
    assert bundled["previous_failure"]["tool_call_id"] == "call-77"


def test_build_checkpoint_execution_config_omits_retry_context_when_absent() -> None:
    """Resume/HITL flows without a retry_context must keep current shape."""
    config = build_checkpoint_execution_config(
        task_id=7,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="simple_tool",
        checkpoint_id="ckpt-stable-abc123",
    )
    configurable = config["configurable"]
    assert "retry_attempt" not in configurable
    assert "retry_max_attempts" not in configurable
    assert "previous_failure" not in configurable
    assert "retry_context" not in configurable


class _RetryTestHub:
    def set_streaming_state(self, task_id: int, state: bool) -> None:
        return None

    async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
        return None


class _RetryTestLifecycle:
    def __init__(self, *, cancel_requested: bool = False) -> None:
        self.cancel_requested = cancel_requested
        self.end_calls: list[Dict[str, Any]] = []

    def start_run(self, **_kwargs: Any) -> None:
        return None

    def end_run(self, **kwargs: Any) -> None:
        self.end_calls.append(dict(kwargs))

    def is_cancel_requested(self, **_kwargs: Any) -> bool:
        return self.cancel_requested


def _provider_refusal() -> LLMRefusalError:
    """Build a deterministic provider refusal for retry orchestration tests."""
    return LLMRefusalError(
        "declined",
        outcome=LLMRefusalOutcome(
            provider="openai",
            model="gpt-4o-mini",
            category="content_filter",
            explanation="Blocked by policy.",
        ),
    )


def _compression_refusal() -> CompressionRequiredError:
    """Wrap a provider refusal exactly as turn compression does."""
    refusal = _provider_refusal()
    compression = CompressionRequiredError("compression_required")
    compression.__cause__ = refusal
    return compression


def _patch_retry_lifecycle_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    """Capture paired retry lifecycle publications for orchestration tests."""
    retry_events: list[Dict[str, Any]] = []
    rewind_events: list[Dict[str, Any]] = []

    async def _capture_retry_state(**kwargs: Any) -> bool:
        retry_events.append(kwargs)
        return True

    async def _capture_rewind_state(**kwargs: Any) -> bool:
        rewind_events.append(kwargs)
        return True

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_retry_state_event",
        _capture_retry_state,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_checkpoint_rewind_state_event",
        _capture_rewind_state,
    )
    return retry_events, rewind_events


def _patch_provider_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retry worker tests DB-free while exercising provider runtime wiring."""

    class _Session:
        def close(self) -> None:
            return None

    class _Resolver:
        def resolve_secret(self, *_args: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(value="test-key")

    class _RuntimeServices:
        client_resolver = _Resolver()

    class _RuntimeConfigService:
        def __init__(self, _db: Any) -> None:
            return None

        def build_continuation_selection(self, *, user_id: int) -> Any:
            raise AssertionError(
                "orchestrator must not prebuild current runtime selection"
            )

        def build_runtime_services(self) -> _RuntimeServices:
            return _RuntimeServices()

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.SessionLocal",
        lambda: _Session(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.LLMRuntimeConfigService",
        _RuntimeConfigService,
    )


def _patch_provider_runtime_with_session(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Any]:
    """Return runtime service/session stubs for continuation cleanup assertions."""

    class _Session:
        closed = False

        def close(self) -> None:
            self.closed = True

    class _RuntimeServices:
        pass

    class _RuntimeConfigService:
        def __init__(self, db: Any) -> None:
            self._db = db

        def build_continuation_selection(self, *, user_id: int) -> Any:
            raise AssertionError(
                "orchestrator must not prebuild current runtime selection"
            )

        def build_runtime_services(self) -> _RuntimeServices:
            return runtime_services

    session = _Session()
    runtime_services = _RuntimeServices()
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.SessionLocal",
        lambda: session,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.LLMRuntimeConfigService",
        _RuntimeConfigService,
    )
    return session, runtime_services


@pytest.mark.asyncio
async def test_facade_retry_from_checkpoint_forwards_checkpoint_id_and_retry_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_from_checkpoint must hand checkpoint_id + retry_context to continuation."""
    facade = LangGraphChatFacade()
    captured_kwargs: Dict[str, Any] = {}

    async def _capture_continue(
        self: CheckpointContinuationService,
        **kwargs: Any,
    ) -> LangGraphChatResult:
        captured_kwargs.update(kwargs)
        return LangGraphChatResult(
            final_text="ok", conversation_id="conv-1", metadata={}
        )

    monkeypatch.setattr(
        CheckpointContinuationService,
        "continue_from_checkpoint",
        _capture_continue,
    )

    retry_context = {
        "retry_attempt": 1,
        "retry_max_attempts": 2,
        "previous_failure": {
            "error_code": "tool_argument_invalid",
            "tool_name": "http_get",
        },
    }
    await facade.retry_from_checkpoint(
        task_id=42,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="simple_tool",
        checkpoint_id="ckpt-stable-abc123",
        retry_context=retry_context,
        reserved_message_id=99,
    )
    assert captured_kwargs["checkpoint_id"] == "ckpt-stable-abc123"
    assert captured_kwargs["retry_context"] == retry_context
    assert captured_kwargs["graph_name"] == "simple_tool"
    assert captured_kwargs["reserved_message_id"] == 99
    assert captured_kwargs["graph_input"] is None
    assert captured_kwargs["interrupt_persist_reason"] == "checkpoint_retry_interrupt"
    assert captured_kwargs["success_persist_reason"] == "checkpoint_retry"


@pytest.mark.asyncio
async def test_resume_generation_closes_runtime_db_and_forwards_runtime_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume success closes the runtime DB and passes continuation runtime unchanged."""
    session, runtime_services = _patch_provider_runtime_with_session(monkeypatch)
    captured_kwargs: Dict[str, Any] = {}
    completed_workflows: list[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _RetryTestHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.get_run_lifecycle_service",
        lambda: _RetryTestLifecycle(cancel_requested=False),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_turn_id_from_workflow_best_effort",
        lambda _workflow_id: "task-401-turn-1",
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_checkpoint_retry_identity_best_effort",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.resolve_interrupt_tool_call_id_best_effort",
        lambda **_kwargs: None,
    )

    async def _resume_from_interrupt(**kwargs: Any) -> LangGraphChatResult:
        captured_kwargs.update(kwargs)
        return LangGraphChatResult(
            final_text="Resume complete",
            conversation_id="conv-resume-runtime",
            metadata={
                "id": "task-401-turn-1",
                "turn_sequence": 1,
                "role": "assistant",
                "streaming": False,
            },
        )

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.resume_from_interrupt",
        AsyncMock(side_effect=_resume_from_interrupt),
    )

    service = TurnExecutionService()
    await service.resume_turn_generation(
        task_id=401,
        user_id=15,
        graph_thread_id=GRAPH_THREAD_ID,
        response={"action": "approve"},
        graph_name="simple_tool",
        checkpoint_id="ckpt-resume-runtime",
        workflow_id=9401,
        interrupt_id="intr-401",
        mark_turn_workflow_completed=lambda **kwargs: completed_workflows.append(
            kwargs
        ),
        mark_interrupt_ticket_resumed=lambda **_kwargs: None,
        mark_interrupt_ticket_completed=lambda **_kwargs: None,
    )

    assert session.closed is True
    assert captured_kwargs["llm_runtime_selection"] is None
    assert captured_kwargs["runtime_services"] is runtime_services
    assert (
        completed_workflows[0]["metadata"]["completion_source"]
        == "resume_generation"
    )


@pytest.mark.asyncio
async def test_run_checkpoint_retry_generation_threads_carrier_to_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker entrypoint must forward the canonical retry carrier into the facade."""
    retry_events: list[Dict[str, Any]] = []
    rewind_events: list[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.mark_turn_workflow_completed_best_effort",
        lambda **kwargs: None,
    )
    async def _capture_retry_state(**kwargs: Any) -> bool:
        retry_events.append(kwargs)
        return True

    async def _capture_rewind_state(**kwargs: Any) -> bool:
        rewind_events.append(kwargs)
        return True

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_retry_state_event",
        _capture_retry_state,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_checkpoint_rewind_state_event",
        _capture_rewind_state,
    )

    retry_result = LangGraphChatResult(
        final_text="Retry complete",
        conversation_id="conv-checkpoint-retry",
        metadata={
            "id": "task-301-turn-9",
            "turn_sequence": 9,
            "role": "assistant",
            "streaming": False,
            "graph_name": "simple_tool",
        },
    )
    retry_mock = AsyncMock(return_value=retry_result)
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.retry_from_checkpoint",
        retry_mock,
    )
    _patch_provider_runtime(monkeypatch)

    sanitized_previous_failure: Mapping[str, Any] = {
        "error_code": "tool_argument_invalid",
        "failure_stage": "graph_continuation",
        "graph_name": "simple_tool",
        "tool_name": "http_get",
        "tool_call_id": "call-77",
        "summary": "tool rejected malformed url argument",
    }

    await run_checkpoint_retry_generation(
        task_id=301,
        user_id=12,
        workflow_id=991,
        graph_thread_id=GRAPH_THREAD_ID,
        turn_id="task-301-turn-9",
        turn_sequence=9,
        graph_name="simple_tool",
        reserved_message_id=909,
        checkpoint_id="ckpt-stable-abc123",
        retry_attempt=1,
        retry_max_attempts=2,
        previous_failure=sanitized_previous_failure,
    )

    retry_mock.assert_awaited_once()
    kwargs = retry_mock.await_args.kwargs
    assert kwargs["task_id"] == 301
    assert kwargs["graph_name"] == "simple_tool"
    assert kwargs["reserved_message_id"] == 909
    assert kwargs["user_id"] == 12
    assert kwargs["llm_runtime_selection"] is None
    assert kwargs["runtime_services"].client_resolver is not None
    # Stored checkpoint pins continuation; the worker must not prebuild from
    # the current user default before the facade can read checkpoint identity.
    assert kwargs["checkpoint_id"] == "ckpt-stable-abc123"
    retry_context = kwargs["retry_context"]
    assert retry_context["retry_attempt"] == 1
    assert retry_context["retry_max_attempts"] == 2
    forwarded_failure = retry_context["previous_failure"]
    assert forwarded_failure["error_code"] == "tool_argument_invalid"
    assert forwarded_failure["tool_name"] == "http_get"
    assert forwarded_failure["tool_call_id"] == "call-77"
    # Sanitization invariant: secret-bearing keys must never appear.
    serialized = repr(retry_context)
    assert "Authorization" not in serialized
    assert "raw_request" not in serialized
    assert "raw_response" not in serialized

    assert [event["state"] for event in rewind_events] == [
        "retrying",
        "started",
        "completed",
    ]
    assert [event["state"] for event in retry_events] == [
        "retrying",
        "started",
        "completed",
    ]
    assert rewind_events[0]["operation_kind"] == "retry"
    assert rewind_events[0]["checkpoint_id"] == "ckpt-stable-abc123"
    assert rewind_events[0]["transcript_resync_required"] is False
    assert retry_events[0]["checkpoint_id"] == "ckpt-stable-abc123"
    assert retry_events[0]["transcript_resync_required"] is False
    assert rewind_events[1]["transcript_resync_required"] is True
    assert retry_events[1]["transcript_resync_required"] is True


@pytest.mark.asyncio
async def test_run_checkpoint_retry_generation_publishes_resync_before_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The destructive resync packet must be published before fast retry finals."""
    published: list[Dict[str, Any]] = []

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            published.append({"task_id": task_id, "event": event})

    hub = _StubHub()
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: hub,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.streaming.status_events.get_in_memory_stream_hub",
        lambda: hub,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.mark_turn_workflow_completed_best_effort",
        lambda **kwargs: None,
    )

    async def _retry_from_checkpoint(**_kwargs: Any) -> LangGraphChatResult:
        lifecycle_contents = [
            item["event"].get("content")
            for item in published
            if item["event"].get("type") == "status"
        ]
        assert lifecycle_contents == [
            "checkpoint_rewind_state",
            "retry_state",
            "checkpoint_rewind_state",
            "retry_state",
        ]
        return LangGraphChatResult(
            final_text="Retry complete",
            conversation_id="conv-checkpoint-retry",
            metadata={
                "id": "task-301-turn-9",
                "turn_sequence": 9,
                "role": "assistant",
                "streaming": False,
                "graph_name": "simple_tool",
            },
        )

    retry_mock = AsyncMock(side_effect=_retry_from_checkpoint)
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.retry_from_checkpoint",
        retry_mock,
    )
    _patch_provider_runtime(monkeypatch)

    await run_checkpoint_retry_generation(
        task_id=301,
        user_id=12,
        workflow_id=991,
        graph_thread_id=GRAPH_THREAD_ID,
        turn_id="task-301-turn-9",
        turn_sequence=9,
        graph_name="simple_tool",
        reserved_message_id=909,
        checkpoint_id="ckpt-stable-abc123",
        retry_attempt=1,
        retry_max_attempts=2,
    )

    event_order = [
        (
            f"status:{event.get('content')}"
            if event.get("type") == "status"
            else str(event.get("type"))
        )
        for event in (item["event"] for item in published)
    ]
    assert event_order == [
        "status:checkpoint_rewind_state",
        "status:retry_state",
        "status:checkpoint_rewind_state",
        "status:retry_state",
        "message_delta",
        "assistant_final",
        "status:checkpoint_rewind_state",
        "status:retry_state",
    ]


@pytest.mark.asyncio
async def test_checkpoint_retry_failure_lifecycle_waits_for_workflow_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure lifecycle resync is published only after workflow failure metadata."""
    order: list[str] = []
    retry_events: list[Dict[str, Any]] = []
    rewind_events: list[Dict[str, Any]] = []
    failed_workflows: list[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _RetryTestHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.get_run_lifecycle_service",
        lambda: _RetryTestLifecycle(cancel_requested=False),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_checkpoint_retry_identity_best_effort",
        lambda **_kwargs: None,
    )

    async def _capture_retry_state(**kwargs: Any) -> bool:
        retry_events.append(kwargs)
        if kwargs["state"] == "failed":
            order.append("retry_failed_event")
        return True

    async def _capture_rewind_state(**kwargs: Any) -> bool:
        rewind_events.append(kwargs)
        if kwargs["state"] == "failed":
            order.append("rewind_failed_event")
        return True

    async def _dispatch_retry_exception(_self: Any, **kwargs: Any) -> None:
        mark_failed = kwargs["mark_turn_workflow_failed"]
        mark_failed(
            workflow_id=kwargs["workflow_id"],
            metadata={
                "failure_source": "checkpoint_retry",
                "error": "checkpoint_retry_failed",
                "active_retry": None,
                "retry_state": "failed",
            },
        )
        order.append("workflow_failed_metadata")

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_retry_state_event",
        _capture_retry_state,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_checkpoint_rewind_state_event",
        _capture_rewind_state,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.failure_dispatcher.TurnExecutionFailureDispatcher.dispatch_retry_exception",
        _dispatch_retry_exception,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.retry_from_checkpoint",
        AsyncMock(side_effect=RuntimeError("retry boom")),
    )
    _patch_provider_runtime(monkeypatch)

    service = TurnExecutionService()
    await service.retry_turn_from_checkpoint(
        task_id=302,
        user_id=12,
        graph_thread_id=GRAPH_THREAD_ID,
        workflow_id=992,
        turn_id="task-302-turn-9",
        turn_sequence=9,
        graph_name="simple_tool",
        reserved_message_id=910,
        checkpoint_id="ckpt-failure",
        retry_attempt=1,
        retry_max_attempts=2,
        mark_turn_workflow_failed=lambda **kwargs: failed_workflows.append(kwargs),
    )

    assert failed_workflows[0]["metadata"]["retry_state"] == "failed"
    assert order == [
        "workflow_failed_metadata",
        "rewind_failed_event",
        "retry_failed_event",
    ]
    failed_retry_event = next(event for event in retry_events if event["state"] == "failed")
    failed_rewind_event = next(
        event for event in rewind_events if event["state"] == "failed"
    )
    assert failed_retry_event["transcript_resync_required"] is True
    assert failed_rewind_event["transcript_resync_required"] is True


@pytest.mark.asyncio
async def test_checkpoint_retry_refusal_resyncs_without_generic_failure_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumed retry refusal settles retry UI without an error code."""
    retry_events, rewind_events = _patch_retry_lifecycle_capture(monkeypatch)
    declined_outcomes: list[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _RetryTestHub(),
    )
    lifecycle = _RetryTestLifecycle(cancel_requested=False)
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.get_run_lifecycle_service",
        lambda: lifecycle,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_checkpoint_retry_identity_best_effort",
        lambda **_kwargs: None,
    )

    async def _dispatch_retry_refusal(_self: Any, **_kwargs: Any) -> bool:
        declined_outcomes.append({"status": "declined"})
        return True

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.failure_dispatcher.TurnExecutionFailureDispatcher.dispatch_retry_exception",
        _dispatch_retry_refusal,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.retry_from_checkpoint",
        AsyncMock(side_effect=_provider_refusal()),
    )
    _patch_provider_runtime(monkeypatch)

    service = TurnExecutionService()
    await service.retry_turn_from_checkpoint(
        task_id=502,
        user_id=17,
        graph_thread_id=GRAPH_THREAD_ID,
        workflow_id=9502,
        turn_id="task-502-turn-1",
        turn_sequence=1,
        graph_name="simple_tool",
        reserved_message_id=95020,
        checkpoint_id="ckpt-refusal",
        retry_attempt=1,
        retry_max_attempts=2,
        mark_turn_workflow_failed=lambda **_kwargs: None,
    )

    assert declined_outcomes == [{"status": "declined"}]
    assert [event["state"] for event in retry_events] == [
        "retrying",
        "started",
        "declined",
    ]
    assert [event["state"] for event in rewind_events] == [
        "retrying",
        "started",
        "declined",
    ]
    for terminal in (retry_events[-1], rewind_events[-1]):
        assert terminal["transcript_resync_required"] is True
        assert terminal.get("error_code") is None
        assert terminal.get("failure_stage") is None
    assert lifecycle.end_calls[-1]["status"] == "declined"


@pytest.mark.asyncio
async def test_checkpoint_retry_resume_refusal_resyncs_without_generic_failure_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumed resume refusal emits one declined outcome and clean resync."""
    retry_events, rewind_events = _patch_retry_lifecycle_capture(monkeypatch)
    declined_outcomes: list[Dict[str, Any]] = []
    retry_identity = {
        "task_id": 503,
        "turn_id": "task-503-turn-2",
        "workflow_id": 9503,
        "graph_name": "deep_reasoning",
        "checkpoint_id": "ckpt-resume-refusal",
        "retry_mode": "checkpoint",
        "retry_attempt": 1,
        "retry_max_attempts": 2,
        "state": "waiting_for_human",
    }

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _RetryTestHub(),
    )
    lifecycle = _RetryTestLifecycle(cancel_requested=False)
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.get_run_lifecycle_service",
        lambda: lifecycle,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_turn_id_from_workflow_best_effort",
        lambda _workflow_id: "task-503-turn-2",
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_checkpoint_retry_identity_best_effort",
        lambda **_kwargs: retry_identity,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.resolve_interrupt_tool_call_id_best_effort",
        lambda **_kwargs: None,
    )

    async def _dispatch_resume_refusal(_self: Any, **_kwargs: Any) -> bool:
        declined_outcomes.append({"status": "declined"})
        return True

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.failure_dispatcher.TurnExecutionFailureDispatcher.dispatch_resume_exception",
        _dispatch_resume_refusal,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.resume_from_interrupt",
        AsyncMock(side_effect=_provider_refusal()),
    )
    _patch_provider_runtime(monkeypatch)

    service = TurnExecutionService()
    await service.resume_turn_generation(
        task_id=503,
        user_id=17,
        graph_thread_id=GRAPH_THREAD_ID,
        response={"action": "approve"},
        graph_name="deep_reasoning",
        checkpoint_id="ckpt-resume-refusal",
        reserved_message_id=95030,
        workflow_id=9503,
        interrupt_id="intr-503",
        mark_turn_workflow_failed=lambda **_kwargs: None,
        mark_interrupt_ticket_resumed=lambda **_kwargs: None,
        mark_interrupt_ticket_failed=lambda **_kwargs: None,
    )

    assert declined_outcomes == [{"status": "declined"}]
    assert [event["state"] for event in retry_events] == ["declined"]
    assert [event["state"] for event in rewind_events] == ["declined"]
    for terminal in (retry_events[0], rewind_events[0]):
        assert terminal["transcript_resync_required"] is True
        assert terminal.get("error_code") is None
        assert terminal.get("failure_stage") is None
    assert lifecycle.end_calls[-1]["status"] == "declined"


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ("resume", "retry"))
async def test_compression_refusal_uses_declined_wired_continuation_boundary(
    monkeypatch: pytest.MonkeyPatch,
    flow: str,
) -> None:
    """Resume and retry compression refusals bypass generic failure packets."""
    retry_events, rewind_events = _patch_retry_lifecycle_capture(monkeypatch)
    lifecycle = _RetryTestLifecycle(cancel_requested=False)
    failed_workflows: list[Dict[str, Any]] = []
    failed_interrupts: list[Dict[str, Any]] = []
    boundary_calls: list[Dict[str, Any]] = []
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _RetryTestHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.get_run_lifecycle_service",
        lambda: lifecycle,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_turn_id_from_workflow_best_effort",
        lambda _workflow_id: "task-504-turn-1",
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_checkpoint_retry_identity_best_effort",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.resolve_interrupt_tool_call_id_best_effort",
        lambda **_kwargs: None,
    )
    _patch_provider_runtime(monkeypatch)

    service = TurnExecutionService()

    async def _capture_boundary(**kwargs: Any) -> None:
        boundary_calls.append(kwargs)

    service._publish_boundary_completion_events = _capture_boundary  # type: ignore[method-assign]
    if flow == "resume":
        monkeypatch.setattr(
            "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.resume_from_interrupt",
            AsyncMock(side_effect=_compression_refusal()),
        )
        await service.resume_turn_generation(
            task_id=504,
            user_id=17,
            graph_thread_id=GRAPH_THREAD_ID,
            response={"action": "approve"},
            graph_name="simple_tool",
            checkpoint_id="ckpt-compression-refusal",
            reserved_message_id=None,
            workflow_id=9504,
            interrupt_id="intr-504",
            mark_turn_workflow_failed=lambda **kwargs: failed_workflows.append(kwargs),
            mark_interrupt_ticket_resumed=lambda **_kwargs: None,
            mark_interrupt_ticket_failed=lambda **kwargs: failed_interrupts.append(kwargs),
        )
    else:
        monkeypatch.setattr(
            "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.retry_from_checkpoint",
            AsyncMock(side_effect=_compression_refusal()),
        )
        await service.retry_turn_from_checkpoint(
            task_id=504,
            user_id=17,
            graph_thread_id=GRAPH_THREAD_ID,
            workflow_id=9504,
            turn_id="task-504-turn-1",
            turn_sequence=1,
            graph_name="simple_tool",
            reserved_message_id=None,
            checkpoint_id="ckpt-compression-refusal",
            retry_attempt=1,
            retry_max_attempts=2,
            mark_turn_workflow_failed=lambda **kwargs: failed_workflows.append(kwargs),
        )

    assert len(failed_workflows) == 1
    metadata = failed_workflows[0]["metadata"]
    assert metadata["outcome_type"] == "provider_refusal"
    assert metadata["retryable"] is False
    assert len(boundary_calls) == 1
    boundary_metadata = boundary_calls[0]["base_metadata"]
    assert boundary_metadata["status"] == "declined"
    assert "error_code" not in boundary_metadata
    assert lifecycle.end_calls[-1]["status"] == "declined"
    if flow == "resume":
        assert retry_events == []
        assert rewind_events == []
        assert failed_interrupts == [
            {"task_id": 504, "interrupt_id": "intr-504"}
        ]
    else:
        assert [event["state"] for event in retry_events][-1] == "declined"
        assert [event["state"] for event in rewind_events][-1] == "declined"
        assert retry_events[-1].get("error_code") is None


@pytest.mark.asyncio
async def test_checkpoint_retry_exception_closes_runtime_db_and_preserves_failure_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry exception closes the runtime DB after preserving failure event order."""
    session, _runtime_services = _patch_provider_runtime_with_session(monkeypatch)
    order: list[str] = []
    retry_events: list[Dict[str, Any]] = []
    rewind_events: list[Dict[str, Any]] = []
    failed_workflows: list[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _RetryTestHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.get_run_lifecycle_service",
        lambda: _RetryTestLifecycle(cancel_requested=False),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.resolve_checkpoint_retry_identity_best_effort",
        lambda **_kwargs: None,
    )

    async def _capture_retry_state(**kwargs: Any) -> bool:
        retry_events.append(kwargs)
        if kwargs["state"] == "failed":
            order.append("retry_failed_event")
        return True

    async def _capture_rewind_state(**kwargs: Any) -> bool:
        rewind_events.append(kwargs)
        if kwargs["state"] == "failed":
            order.append("rewind_failed_event")
        return True

    async def _dispatch_retry_exception(_self: Any, **kwargs: Any) -> None:
        mark_failed = kwargs["mark_turn_workflow_failed"]
        mark_failed(
            workflow_id=kwargs["workflow_id"],
            metadata={
                "failure_source": "checkpoint_retry",
                "error": "checkpoint_retry_failed",
                "active_retry": None,
                "retry_state": "failed",
            },
        )
        order.append("workflow_failed_metadata")

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_retry_state_event",
        _capture_retry_state,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_checkpoint_rewind_state_event",
        _capture_rewind_state,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.failure_dispatcher.TurnExecutionFailureDispatcher.dispatch_retry_exception",
        _dispatch_retry_exception,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.retry_from_checkpoint",
        AsyncMock(side_effect=RuntimeError("retry boom")),
    )

    service = TurnExecutionService()
    await service.retry_turn_from_checkpoint(
        task_id=402,
        user_id=16,
        graph_thread_id=GRAPH_THREAD_ID,
        workflow_id=9402,
        turn_id="task-402-turn-1",
        turn_sequence=1,
        graph_name="simple_tool",
        reserved_message_id=94020,
        checkpoint_id="ckpt-runtime-close",
        retry_attempt=1,
        retry_max_attempts=2,
        mark_turn_workflow_failed=lambda **kwargs: failed_workflows.append(kwargs),
    )

    assert session.closed is True
    assert failed_workflows[0]["metadata"]["retry_state"] == "failed"
    assert order == [
        "workflow_failed_metadata",
        "rewind_failed_event",
        "retry_failed_event",
    ]
    assert next(event for event in retry_events if event["state"] == "failed")[
        "transcript_resync_required"
    ] is True
    assert next(event for event in rewind_events if event["state"] == "failed")[
        "transcript_resync_required"
    ] is True


@pytest.mark.asyncio
async def test_checkpoint_retry_cancelled_lifecycle_resyncs_and_clears_active_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation terminals carry retry cleanup metadata and force resync."""
    retry_events: list[Dict[str, Any]] = []
    rewind_events: list[Dict[str, Any]] = []
    failed_workflows: list[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _RetryTestHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.get_run_lifecycle_service",
        lambda: _RetryTestLifecycle(cancel_requested=True),
    )

    async def _capture_retry_state(**kwargs: Any) -> bool:
        retry_events.append(kwargs)
        return True

    async def _capture_rewind_state(**kwargs: Any) -> bool:
        rewind_events.append(kwargs)
        return True

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_retry_state_event",
        _capture_retry_state,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.orchestration.orchestrator.publish_checkpoint_rewind_state_event",
        _capture_rewind_state,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.retry_from_checkpoint",
        AsyncMock(side_effect=RuntimeError("cancel after start")),
    )
    _patch_provider_runtime(monkeypatch)

    service = TurnExecutionService()
    await service.retry_turn_from_checkpoint(
        task_id=303,
        user_id=12,
        graph_thread_id=GRAPH_THREAD_ID,
        workflow_id=993,
        turn_id="task-303-turn-9",
        turn_sequence=9,
        graph_name="simple_tool",
        reserved_message_id=911,
        checkpoint_id="ckpt-cancel",
        retry_attempt=1,
        retry_max_attempts=2,
        mark_turn_workflow_failed=lambda **kwargs: failed_workflows.append(kwargs),
    )

    cancelled_metadata = failed_workflows[0]["metadata"]
    assert cancelled_metadata["failure_source"] == "checkpoint_retry"
    assert cancelled_metadata["error"] == "run_cancelled"
    assert cancelled_metadata["active_retry"] is None
    assert cancelled_metadata["retry_state"] == "cancelled"
    assert cancelled_metadata["terminal_status"] == "cancelled"
    assert cancelled_metadata["cancel_requested"] is True

    cancelled_retry_event = next(
        event for event in retry_events if event["state"] == "cancelled"
    )
    cancelled_rewind_event = next(
        event for event in rewind_events if event["state"] == "cancelled"
    )
    assert cancelled_retry_event["transcript_resync_required"] is True
    assert cancelled_rewind_event["transcript_resync_required"] is True


@pytest.mark.asyncio
async def test_run_checkpoint_retry_generation_legacy_path_omits_retry_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a retry carrier, facade.retry_from_checkpoint sees retry_context=None."""

    class _StubHub:
        def set_streaming_state(self, task_id: int, state: bool) -> None:
            return None

        async def publish(self, task_id: int, event: Dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _StubHub(),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.mark_turn_workflow_completed_best_effort",
        lambda **kwargs: None,
    )

    retry_result = LangGraphChatResult(
        final_text="Retry complete",
        conversation_id="conv-checkpoint-retry",
        metadata={
            "id": "task-301-turn-9",
            "turn_sequence": 9,
            "role": "assistant",
            "streaming": False,
        },
    )
    retry_mock = AsyncMock(return_value=retry_result)
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.turn_service.LangGraphChatFacade.retry_from_checkpoint",
        retry_mock,
    )
    _patch_provider_runtime(monkeypatch)

    await run_checkpoint_retry_generation(
        task_id=301,
        user_id=12,
        workflow_id=991,
        graph_thread_id=GRAPH_THREAD_ID,
        turn_id="task-301-turn-9",
        turn_sequence=9,
        graph_name="simple_tool",
        reserved_message_id=909,
    )

    retry_mock.assert_awaited_once()
    kwargs = retry_mock.await_args.kwargs
    assert kwargs.get("checkpoint_id") is None
    assert kwargs.get("retry_context") is None


def test_graph_runtime_can_read_retry_context_from_facade_config() -> None:
    """Graph runtime reads retry identity from configurable."""
    config = build_checkpoint_execution_config(
        task_id=42,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="simple_tool",
        checkpoint_id="ckpt-stable-abc123",
        retry_context={
            "retry_attempt": 1,
            "retry_max_attempts": 2,
            "previous_failure": {
                "error_code": "tool_argument_invalid",
                "failure_stage": "graph_continuation",
                "graph_name": "simple_tool",
                "tool_name": "http_get",
                "tool_call_id": "call-77",
                "summary": "tool rejected malformed url argument",
            },
        },
    )
    ctx = read_retry_context(config)
    assert isinstance(ctx, RetryContext)
    assert ctx.is_retry is True
    assert ctx.retry_attempt == 1
    assert ctx.retry_max_attempts == 2
    assert ctx.previous_failure is not None
    assert ctx.previous_failure["error_code"] == "tool_argument_invalid"
    assert ctx.previous_failure["tool_name"] == "http_get"
    assert ctx.previous_failure["tool_call_id"] == "call-77"


def test_graph_runtime_retry_context_empty_without_carrier() -> None:
    """Resume/HITL or fresh-turn configs surface as a non-retry context."""
    config = build_checkpoint_execution_config(
        task_id=42,
        graph_thread_id=GRAPH_THREAD_ID,
        graph_name="simple_tool",
        checkpoint_id="ckpt-stable-abc123",
    )
    ctx = read_retry_context(config)
    assert ctx.is_retry is False
    assert ctx.retry_attempt is None
    assert ctx.retry_max_attempts is None
    assert ctx.previous_failure is None


def test_graph_runtime_retry_context_strips_unwhitelisted_keys() -> None:
    """read_retry_context never surfaces non-whitelisted previous-failure keys."""
    fake_configurable = {
        "configurable": {
            "retry_attempt": 1,
            "retry_max_attempts": 2,
            "previous_failure": {
                "error_code": "tool_argument_invalid",
                "tool_name": "http_get",
                # Forbidden keys must never appear on the read surface.
                "raw_request": {"headers": {"Authorization": "Bearer DO-NOT-LEAK"}},
                "auth_token": "Bearer DO-NOT-LEAK",
                "api_key": "sk-LEAK-ME",
            },
        }
    }
    ctx = read_retry_context(fake_configurable)
    assert ctx.previous_failure is not None
    assert set(ctx.previous_failure.keys()).issubset(
        {
            "error_code",
            "failure_stage",
            "graph_name",
            "tool_name",
            "tool_call_id",
            "summary",
        }
    )
    assert "raw_request" not in ctx.previous_failure
    assert "auth_token" not in ctx.previous_failure
    assert "api_key" not in ctx.previous_failure
