"""Phase 3.3 — pin the checkpoint-retry failure diagnostics contract.

These tests cover ``TurnExecutionFailureDispatcher`` for the checkpoint
retry flow. They assert that:

  * ``dispatch_retry_compression_failure``, ``dispatch_retry_exception``,
    and ``dispatch_retry_hub_unavailable`` accept the canonical retry
    carrier (``retry_attempt`` / ``retry_max_attempts`` / ``checkpoint_id``
    / ``retry_mode`` / ``previous_failure``) and forward sanitized retry
    diagnostics into ``handle_terminal_turn_error.extra_workflow_metadata``,
  * the ``previous_failure`` projection is re-run through
    ``sanitize_previous_failure`` as defense in depth — non-whitelisted
    fields like raw provider payloads, JWTs, or API keys never reach the
    workflow row,
  * retry budget exhaustion is reflected in the persisted diagnostics
    (``retry_exhausted`` / ``another_retry_allowed``),
  * the canonical ``failure_source="checkpoint_retry"`` and
    ``retry_state="failed"`` markers are set on every retry-flow failure.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from backend.services.langgraph_chat.compression.context_models import (
    CompressionRequiredError,
)
from backend.services.langgraph_chat.execution.orchestration.failure_dispatcher import (
    TurnExecutionFailureDispatcher,
)
from backend.services.langgraph_chat.execution.refusal_service import (
    TurnExecutionRefusalService,
)
from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome


class _RecordingErrorService:
    """Minimal stand-in for ``TurnExecutionErrorService`` capturing dispatch kwargs."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.retryable_failure: Dict[str, Any] | None = None

    @staticmethod
    def resolve_compression_error_code(_exc, *, default: str) -> str:
        return default

    def extract_retryable_post_tool_failure(self, _exc):
        return self.retryable_failure

    async def handle_terminal_turn_error(self, **kwargs) -> None:
        self.calls.append(kwargs)

    @staticmethod
    def resolve_failure_context(**kwargs: Any) -> Dict[str, Any]:
        return {
            "conversation_id": kwargs.get("conversation_id") or "",
            "turn_id": kwargs.get("turn_id"),
            "turn_sequence": kwargs.get("turn_sequence"),
            "reserved_message_id": kwargs.get("reserved_message_id"),
            "graph_name": None,
            "checkpoint_id": None,
        }


class _RecordingRefusalService:
    """Capture refusal dispatches without terminal persistence dependencies."""

    def __init__(self) -> None:
        self.outcome = LLMRefusalOutcome(provider="openai", model="gpt-5.6")
        self.calls: List[Dict[str, Any]] = []

    def extract_refusal_outcome(self, _exc: BaseException) -> LLMRefusalOutcome:
        return self.outcome

    async def handle_terminal_turn_refusal(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


async def _noop_publish(**_kwargs):  # pragma: no cover - test-only stub
    return None


def _make_dispatcher() -> tuple[TurnExecutionFailureDispatcher, _RecordingErrorService]:
    error_service = _RecordingErrorService()
    dispatcher = TurnExecutionFailureDispatcher(error_service=error_service)  # type: ignore[arg-type]
    return dispatcher, error_service


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ("start", "resume", "retry"))
async def test_provider_refusal_precedes_generic_failure_dispatch(flow: str) -> None:
    error_service = _RecordingErrorService()
    refusal_service = _RecordingRefusalService()
    dispatcher = TurnExecutionFailureDispatcher(
        error_service=error_service,  # type: ignore[arg-type]
        refusal_service=refusal_service,  # type: ignore[arg-type]
    )
    common = {
        "exc": RuntimeError("wrapped refusal"),
        "task_id": 42,
        "hub": object(),
        "workflow_id": 99,
        "reserved_message_id": 77,
        "mark_turn_workflow_failed": lambda **_kwargs: None,
        "publish_boundary_completion_events": _noop_publish,
    }
    if flow == "start":
        refusal_consumed = await dispatcher.dispatch_start_exception(
            **common,
            retryable_post_tool_error_message="retryable",
            generation_failed_error_message="failed",
            conversation_id="conv-z",
            turn_id="turn-z",
            turn_sequence=3,
        )
    elif flow == "resume":
        refusal_consumed = await dispatcher.dispatch_resume_exception(
            **common,
            graph_name="simple_tool",
            retryable_post_tool_error_message="retryable",
            resume_failed_error_message="failed",
            result=SimpleNamespace(
                conversation_id="conv-z",
                metadata={"id": "turn-z", "turn_sequence": 3},
            ),
            interrupt_id="interrupt-z",
            mark_interrupt_ticket_failed=lambda **_kwargs: None,
        )
    else:
        refusal_consumed = await dispatcher.dispatch_retry_exception(
            **common,
            graph_name="simple_tool",
            retryable_post_tool_error_message="retryable",
            checkpoint_retry_failed_error_message="failed",
            turn_id="turn-z",
            turn_sequence=3,
            checkpoint_id="ckpt-z",
        )

    assert len(refusal_service.calls) == 1
    assert refusal_service.calls[0]["outcome"] is refusal_service.outcome
    assert error_service.calls == []
    if flow in {"resume", "retry"}:
        assert refusal_consumed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ("start", "resume", "retry"))
async def test_nested_compression_refusal_precedes_generic_failure_dispatch(
    flow: str,
) -> None:
    """Compression wrappers must still route their refusal cause as declined."""
    error_service = _RecordingErrorService()
    refusal_service = TurnExecutionRefusalService(
        error_service=error_service,  # type: ignore[arg-type]
    )
    dispatcher = TurnExecutionFailureDispatcher(
        error_service=error_service,  # type: ignore[arg-type]
        refusal_service=refusal_service,  # type: ignore[arg-type]
    )
    outcome = LLMRefusalOutcome(provider="openai", model="gpt-5.6")
    refusal = LLMRefusalError(
        "declined",
        outcome=outcome,
    )
    try:
        raise refusal
    except LLMRefusalError as cause:
        compression_exc = CompressionRequiredError("compression_required")
        compression_exc.__cause__ = cause

    failed_workflows: List[Dict[str, Any]] = []
    boundary_calls: List[Dict[str, Any]] = []

    async def _capture_boundary(**kwargs: Any) -> None:
        boundary_calls.append(kwargs)

    common = {
        "compression_exc": compression_exc,
        "default_error_code": "compression_persist_failed",
        "task_id": 42,
        "hub": object(),
        "workflow_id": 99,
        "reserved_message_id": None,
        "mark_turn_workflow_failed": lambda **kwargs: failed_workflows.append(kwargs),
        "publish_boundary_completion_events": _capture_boundary,
    }
    if flow == "start":
        refusal_consumed = await dispatcher.dispatch_start_compression_failure(
            **common,
            generation_failed_error_message="failed",
            conversation_id="conv-z",
            turn_id="turn-z",
            turn_sequence=3,
        )
    elif flow == "resume":
        refusal_consumed = await dispatcher.dispatch_resume_compression_failure(
            **common,
            resume_failed_error_message="failed",
            graph_name="simple_tool",
            result=SimpleNamespace(
                conversation_id="conv-z",
                metadata={"id": "turn-z", "turn_sequence": 3},
            ),
            interrupt_id="interrupt-z",
            mark_interrupt_ticket_failed=lambda **_kwargs: None,
        )
    else:
        refusal_consumed = await dispatcher.dispatch_retry_compression_failure(
            **common,
            checkpoint_retry_failed_error_message="failed",
            graph_name="simple_tool",
            turn_id="turn-z",
            turn_sequence=3,
            checkpoint_id="ckpt-z",
        )

    assert refusal_consumed is True
    assert error_service.calls == []
    assert len(failed_workflows) == 1
    assert failed_workflows[0]["metadata"]["outcome_type"] == "provider_refusal"
    assert failed_workflows[0]["metadata"]["retryable"] is False
    assert len(boundary_calls) == 1
    boundary_metadata = boundary_calls[0]["base_metadata"]
    assert boundary_metadata["status"] == "declined"
    assert boundary_metadata["stop_reason"] == "refusal"
    assert "error_code" not in boundary_metadata


@pytest.mark.asyncio
async def test_start_exception_timeout_uses_timeout_specific_retryable_content() -> None:
    dispatcher, error_service = _make_dispatcher()
    error_service.retryable_failure = {
        "error_code": "llm_timeout",
        "internal_error_message": "LLM request timed out",
        "retry_mode": "checkpoint",
        "graph_name": None,
        "diagnostics": {
            "component": "CONVERSATION",
            "operation": "conversation_main_llm_call",
            "timeout_sec": 120,
            "outcome": "request_timeout",
        },
    }

    await dispatcher.dispatch_start_exception(
        exc=RuntimeError("boom"),
        task_id=42,
        hub=object(),
        workflow_id=99,
        reserved_message_id=None,
        retryable_post_tool_error_message="retryable",
        generation_failed_error_message="[Error] Failed to generate response.",
        conversation_id="conv-z",
        turn_id="turn-z",
        turn_sequence=3,
        mark_turn_workflow_failed=lambda **_kwargs: None,
        publish_boundary_completion_events=_noop_publish,
    )

    assert len(error_service.calls) == 1
    call = error_service.calls[0]
    assert call["failure_source"] == "initial_generation"
    assert call["error_code"] == "llm_timeout"
    assert (
        call["content"]
        == "The request is taking too much time to generate a response."
    )
    assert call["retryable"] is True


@pytest.mark.asyncio
async def test_retry_compression_failure_persists_sanitized_diagnostics() -> None:
    dispatcher, error_service = _make_dispatcher()

    await dispatcher.dispatch_retry_compression_failure(
        compression_exc=CompressionRequiredError("compression_required"),
        default_error_code="compression_persist_failed",
        task_id=42,
        hub=object(),  # opaque hub stand-in; error service is recorded
        workflow_id=99,
        reserved_message_id=None,
        checkpoint_retry_failed_error_message="retry compression failed",
        graph_name="simple_tool",
        turn_id="turn-z",
        turn_sequence=3,
        mark_turn_workflow_failed=lambda **_kwargs: None,
        publish_boundary_completion_events=_noop_publish,
        retry_attempt=1,
        retry_max_attempts=2,
        checkpoint_id="ckpt-z",
        retry_mode="checkpoint",
        previous_failure={
            "error_code": "tool_arg_invalid",
            "failure_stage": "tool",
            "graph_name": "simple_tool",
            # Non-whitelisted keys must be dropped:
            "raw_provider_payload": "secret",
            "api_key": "sk-should-never-stream",
        },
    )

    assert len(error_service.calls) == 1
    call = error_service.calls[0]
    assert call["failure_source"] == "checkpoint_retry"
    assert call["retry_mode"] == "checkpoint"
    extra = call["extra_workflow_metadata"]
    assert extra["retry_state"] == "failed"
    assert extra["failure_stage"] == "compression"
    assert extra["retry_attempt"] == 1
    assert extra["retry_max_attempts"] == 2
    assert extra["another_retry_allowed"] is True
    assert extra["retry_exhausted"] is False
    assert extra["checkpoint_id"] == "ckpt-z"
    assert extra["graph_name"] == "simple_tool"
    assert extra["workflow_id"] == 99
    # Phase 4.3: terminal failure clears the in-flight ``active_retry`` block
    # on the workflow row so transcript bootstrap renders the canonical
    # post-failure overlay from one workflow read.
    assert "active_retry" in extra and extra["active_retry"] is None
    # Defense-in-depth sanitization: non-whitelisted keys are dropped.
    sanitized = extra["previous_failure"]
    assert sanitized == {
        "error_code": "tool_arg_invalid",
        "failure_stage": "tool",
        "graph_name": "simple_tool",
    }, "previous_failure must be re-projected through the canonical whitelist"


@pytest.mark.asyncio
async def test_retry_exception_dispatch_persists_diagnostics_and_marks_exhausted() -> None:
    dispatcher, error_service = _make_dispatcher()

    await dispatcher.dispatch_retry_exception(
        exc=RuntimeError("boom"),
        task_id=42,
        hub=object(),
        workflow_id=99,
        reserved_message_id=None,
        graph_name="simple_tool",
        retryable_post_tool_error_message="retryable",
        checkpoint_retry_failed_error_message="retry failed",
        turn_id="turn-z",
        turn_sequence=3,
        mark_turn_workflow_failed=lambda **_kwargs: None,
        publish_boundary_completion_events=_noop_publish,
        retry_attempt=2,  # already at the budget cap
        retry_max_attempts=2,
        checkpoint_id="ckpt-z",
        retry_mode="checkpoint",
        previous_failure={"error_code": "checkpoint_retry_failed"},
    )

    assert len(error_service.calls) == 1
    call = error_service.calls[0]
    assert call["failure_source"] == "checkpoint_retry"
    assert call["error_code"] == "checkpoint_retry_failed"
    extra = call["extra_workflow_metadata"]
    assert extra["retry_state"] == "failed"
    assert extra["failure_stage"] == "exception"
    assert extra["retry_attempt"] == 2
    assert extra["retry_max_attempts"] == 2
    assert extra["retry_exhausted"] is True
    assert extra["another_retry_allowed"] is False


@pytest.mark.asyncio
async def test_retry_exception_exhausted_retryable_failure_suppresses_retry_boundary() -> None:
    dispatcher, error_service = _make_dispatcher()
    error_service.retryable_failure = {
        "error_code": "tool_argument_invalid",
        "internal_error_message": "tool rejected malformed url argument",
        "retry_mode": "checkpoint",
        "graph_name": "simple_tool",
        "diagnostics": {"tool_name": "shell.exec"},
    }

    await dispatcher.dispatch_retry_exception(
        exc=RuntimeError("boom"),
        task_id=42,
        hub=object(),
        workflow_id=99,
        reserved_message_id=None,
        graph_name="simple_tool",
        retryable_post_tool_error_message="retryable",
        checkpoint_retry_failed_error_message="retry failed",
        turn_id="turn-z",
        turn_sequence=3,
        mark_turn_workflow_failed=lambda **_kwargs: None,
        publish_boundary_completion_events=_noop_publish,
        retry_attempt=2,
        retry_max_attempts=2,
        checkpoint_id="ckpt-z",
        retry_mode="checkpoint",
        previous_failure={"error_code": "tool_argument_invalid"},
    )

    assert len(error_service.calls) == 1
    call = error_service.calls[0]
    assert call["retryable"] is False
    extra = call["extra_workflow_metadata"]
    assert extra["retry_exhausted"] is True
    assert extra["another_retry_allowed"] is False
    boundary = call["extra_boundary_metadata"]
    assert boundary["retryable"] is False
    assert boundary["retry_state"] == "failed"
    assert boundary["retry_exhausted"] is True
    assert boundary["another_retry_allowed"] is False
    assert boundary["retry_attempt"] == 2
    assert boundary["retry_max_attempts"] == 2


@pytest.mark.asyncio
async def test_retry_exception_timeout_uses_timeout_specific_content_when_exhausted() -> None:
    dispatcher, error_service = _make_dispatcher()
    error_service.retryable_failure = {
        "error_code": "llm_timeout",
        "internal_error_message": "LLM request timed out",
        "retry_mode": "checkpoint",
        "graph_name": None,
        "diagnostics": {
            "component": "PLANNER",
            "operation": "tool_selection_llm_call",
            "timeout_sec": 120,
            "outcome": "selection_timeout",
        },
    }

    await dispatcher.dispatch_retry_exception(
        exc=RuntimeError("boom"),
        task_id=42,
        hub=object(),
        workflow_id=99,
        reserved_message_id=None,
        graph_name="simple_tool",
        retryable_post_tool_error_message="retryable",
        checkpoint_retry_failed_error_message="retry failed",
        turn_id="turn-z",
        turn_sequence=3,
        mark_turn_workflow_failed=lambda **_kwargs: None,
        publish_boundary_completion_events=_noop_publish,
        retry_attempt=2,
        retry_max_attempts=2,
        checkpoint_id="ckpt-z",
        retry_mode="checkpoint",
        previous_failure={"error_code": "llm_timeout"},
    )

    assert len(error_service.calls) == 1
    call = error_service.calls[0]
    assert call["error_code"] == "llm_timeout"
    assert (
        call["content"]
        == "The request is taking too much time to generate a response."
    )
    assert call["retryable"] is False


@pytest.mark.asyncio
async def test_retry_hub_unavailable_persists_failure_stage_and_diagnostics() -> None:
    dispatcher, error_service = _make_dispatcher()

    await dispatcher.dispatch_retry_hub_unavailable(
        task_id=42,
        workflow_id=99,
        reserved_message_id=None,
        checkpoint_retry_failed_error_message="hub unavailable",
        graph_name="simple_tool",
        turn_id="turn-z",
        turn_sequence=3,
        mark_turn_workflow_failed=lambda **_kwargs: None,
        publish_boundary_completion_events=_noop_publish,
        retry_attempt=1,
        retry_max_attempts=2,
        checkpoint_id="ckpt-z",
        retry_mode="checkpoint",
        previous_failure=None,
    )

    assert len(error_service.calls) == 1
    extra = error_service.calls[0]["extra_workflow_metadata"]
    assert extra["retry_state"] == "failed"
    assert extra["failure_stage"] == "hub_unavailable"
    assert extra["retry_attempt"] == 1
    assert extra["retry_max_attempts"] == 2
    assert extra.get("previous_failure") is None
