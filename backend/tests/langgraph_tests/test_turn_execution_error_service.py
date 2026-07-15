"""Tests for TurnExecutionErrorService error classification and terminal side effects."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from agent.graph.nodes.post_tool_reasoning.models import RetryablePostToolReasoningError
from agent.reasoning.structured_contract_recovery import (
    StructuredContractViolationError,
)
from core.llm import LLMTimeoutError
from backend.services.langgraph_chat.compression.context_models import (
    CompressionRequiredError,
)
from backend.services.langgraph_chat.execution.error_service import (
    TurnExecutionErrorService,
)


def test_resolve_compression_error_code_prefers_known_reason() -> None:
    service = TurnExecutionErrorService()

    known = CompressionRequiredError(
        reason="compression_required_failed", detail="blocked"
    )
    assert (
        service.resolve_compression_error_code(known, default="fallback")
        == "compression_required_failed"
    )

    uncompactable = CompressionRequiredError(
        reason="context_uncompactable",
        detail="minimum candidate exceeds context",
    )
    assert (
        service.resolve_compression_error_code(uncompactable, default="fallback")
        == "context_uncompactable"
    )

    unknown = CompressionRequiredError(reason="unexpected_reason", detail="blocked")
    assert (
        service.resolve_compression_error_code(unknown, default="fallback")
        == "fallback"
    )


def test_extract_retryable_post_tool_failure_from_nested_exception_chain() -> None:
    service = TurnExecutionErrorService()
    retryable_exc = RetryablePostToolReasoningError(
        "Provider returned invalid structured output",
        error_code="provider_structured_output_parse",
        diagnostics={"response_id": "resp_301"},
        graph_name="simple_tool",
    )

    try:
        try:
            raise retryable_exc
        except RetryablePostToolReasoningError as inner:
            raise RuntimeError("outer failure") from inner
    except RuntimeError as outer:
        details = service.extract_retryable_post_tool_failure(outer)

    assert details is not None
    assert details["error_code"] == "provider_structured_output_parse"
    assert details["retry_mode"] == "checkpoint"
    assert details["graph_name"] == "simple_tool"
    assert details["diagnostics"] == {"response_id": "resp_301"}
    assert (
        details["internal_error_message"]
        == "Provider returned invalid structured output"
    )


def test_extract_retryable_structured_contract_failure_from_nested_exception_chain() -> (
    None
):
    service = TurnExecutionErrorService()
    retryable_exc = StructuredContractViolationError(
        error_code="structured_contract_schema_validation",
        stage="tool_selector",
        contract="tool_selector",
        kind="schema_validation_error",
        details="Selected tools payload violated contract",
        retryable=True,
        diagnostics={"selected_tools": []},
    )

    try:
        try:
            raise retryable_exc
        except StructuredContractViolationError as inner:
            raise RuntimeError("outer failure") from inner
    except RuntimeError as outer:
        details = service.extract_retryable_post_tool_failure(outer)

    assert details is not None
    assert details["error_code"] == "structured_contract_schema_validation"
    assert details["retry_mode"] == "checkpoint"
    assert details["diagnostics"] == {
        "selected_tools": [],
        "stage": "tool_selector",
        "contract": "tool_selector",
        "kind": "schema_validation_error",
    }
    assert (
        details["internal_error_message"] == "Selected tools payload violated contract"
    )


def test_extract_retryable_llm_timeout_failure_from_nested_exception_chain() -> None:
    service = TurnExecutionErrorService()
    timeout_exc = LLMTimeoutError(
        task_id=201,
        component="PLANNER",
        operation="tool_selection_llm_call",
        timeout_sec=120,
        outcome="selection_timeout",
        details="mode=resume",
    )

    try:
        try:
            raise timeout_exc
        except LLMTimeoutError as inner:
            raise RuntimeError("outer failure") from inner
    except RuntimeError as outer:
        details = service.extract_retryable_post_tool_failure(outer)

    assert details is not None
    assert details["error_code"] == "llm_timeout"
    assert details["retry_mode"] == "checkpoint"
    assert details["graph_name"] is None
    assert details["diagnostics"] == {
        "component": "PLANNER",
        "operation": "tool_selection_llm_call",
        "timeout_sec": 120,
        "outcome": "selection_timeout",
        "task_id": "201",
        "details": "mode=resume",
    }
    assert "LLM request timed out" in details["internal_error_message"]


@pytest.mark.asyncio
async def test_handle_terminal_turn_error_skips_publish_when_hub_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TurnExecutionErrorService()
    workflow_calls: List[Dict[str, Any]] = []
    persist_calls: List[Dict[str, Any]] = []
    publish_calls: List[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._resolve_failure_context",
        staticmethod(
            lambda **_kwargs: {
                "conversation_id": "conv-100",
                "turn_id": "task-100-turn-4",
                "turn_sequence": 4,
                "reserved_message_id": 700,
            }
        ),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._persist_assistant_error_message",
        staticmethod(lambda **kwargs: persist_calls.append(kwargs)),
    )

    async def _publish(**kwargs: Any) -> None:
        publish_calls.append(kwargs)

    await service.handle_terminal_turn_error(
        task_id=100,
        hub=None,
        workflow_id=901,
        reserved_message_id=700,
        failure_source="initial_generation",
        error_code="generation_failed",
        content="[Error] Failed to generate response.",
        retryable=False,
        retry_mode=None,
        graph_name=None,
        publish_boundary_completion_events=_publish,
        mark_turn_workflow_failed=lambda **kwargs: workflow_calls.append(kwargs),
    )

    assert workflow_calls == [
        {
            "workflow_id": 901,
            "metadata": {
                "failure_source": "initial_generation",
                "error": "generation_failed",
                "error_message": "[Error] Failed to generate response.",
            },
        }
    ]
    assert persist_calls == [
        {
            "reserved_message_id": 700,
            "content": "[Error] Failed to generate response.",
            "error_code": "generation_failed",
        }
    ]
    assert publish_calls == []


@pytest.mark.asyncio
async def test_handle_terminal_turn_error_preserves_side_effect_order_and_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TurnExecutionErrorService()
    steps: List[str] = []
    publish_calls: List[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._resolve_failure_context",
        staticmethod(
            lambda **_kwargs: {
                "conversation_id": "conv-201",
                "turn_id": "task-201-turn-7",
                "turn_sequence": 7,
                "reserved_message_id": 701,
            }
        ),
    )

    def _persist(**_kwargs: Any) -> None:
        steps.append("persist")

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._persist_assistant_error_message",
        staticmethod(_persist),
    )

    async def _publish(**kwargs: Any) -> None:
        steps.append("publish")
        publish_calls.append(kwargs)

    await service.handle_terminal_turn_error(
        task_id=201,
        hub=object(),
        workflow_id=8801,
        reserved_message_id=701,
        failure_source="resume_generation",
        error_code="provider_structured_output_parse",
        content="[Error] A structured response failed validation. Retry to continue from the latest checkpoint.",
        retryable=True,
        retry_mode="checkpoint",
        graph_name="simple_tool",
        checkpoint_id="ckpt-201",
        publish_boundary_completion_events=_publish,
        mark_turn_workflow_failed=lambda **_kwargs: steps.append("workflow_failed"),
        interrupt_id="intr-201",
        mark_interrupt_ticket_failed=lambda **_kwargs: steps.append("interrupt_failed"),
    )

    assert steps == ["workflow_failed", "interrupt_failed", "persist", "publish"]
    assert len(publish_calls) == 1
    payload = publish_calls[0]
    assert payload["task_id"] == 201
    assert payload["conversation_id"] == "conv-201"
    assert payload["turn_id"] == "task-201-turn-7"
    assert payload["turn_sequence"] == 7
    assert payload["base_metadata"]["status"] == "error"
    assert payload["base_metadata"]["error_code"] == "provider_structured_output_parse"
    assert payload["base_metadata"]["retryable"] is True
    assert payload["base_metadata"]["retry_mode"] == "checkpoint"
    assert payload["base_metadata"]["graph_name"] == "simple_tool"
    assert payload["base_metadata"]["checkpoint_id"] == "ckpt-201"


@pytest.mark.asyncio
async def test_handle_terminal_turn_error_merges_extra_boundary_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TurnExecutionErrorService()
    publish_calls: List[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._resolve_failure_context",
        staticmethod(
            lambda **_kwargs: {
                "conversation_id": "conv-202",
                "turn_id": "task-202-turn-8",
                "turn_sequence": 8,
                "reserved_message_id": 702,
            }
        ),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._persist_assistant_error_message",
        staticmethod(lambda **_kwargs: None),
    )

    async def _publish(**kwargs: Any) -> None:
        publish_calls.append(kwargs)

    await service.handle_terminal_turn_error(
        task_id=202,
        hub=object(),
        workflow_id=8802,
        reserved_message_id=702,
        failure_source="checkpoint_retry",
        error_code="tool_argument_invalid",
        content="retry failed",
        retryable=False,
        retry_mode="checkpoint",
        graph_name="simple_tool",
        publish_boundary_completion_events=_publish,
        mark_turn_workflow_failed=lambda **_kwargs: None,
        extra_boundary_metadata={
            "retryable": False,
            "retry_state": "failed",
            "retry_exhausted": True,
            "another_retry_allowed": False,
            "retry_attempt": 2,
            "retry_max_attempts": 2,
        },
    )

    assert len(publish_calls) == 1
    metadata = publish_calls[0]["base_metadata"]
    assert metadata["retryable"] is False
    assert metadata["retry_state"] == "failed"
    assert metadata["retry_exhausted"] is True
    assert metadata["another_retry_allowed"] is False
    assert metadata["retry_attempt"] == 2
    assert metadata["retry_max_attempts"] == 2


@pytest.mark.asyncio
async def test_handle_terminal_turn_error_persists_sanitized_last_failure_for_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retryable terminal failures must persist a sanitized ``last_failure`` block.

    Without this projection on ``workflow_metadata['last_failure']`` the
    retry route's ``previous_failure`` carrier stays empty and the graph
    runtime cannot distinguish a retry from a fresh continuation.
    """
    service = TurnExecutionErrorService()
    workflow_calls: list[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._resolve_failure_context",
        staticmethod(
            lambda **_kwargs: {
                "conversation_id": "conv-300",
                "turn_id": "task-300-turn-9",
                "turn_sequence": 9,
                "reserved_message_id": 900,
            }
        ),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._persist_assistant_error_message",
        staticmethod(lambda **_kwargs: None),
    )

    async def _publish(**_kwargs: Any) -> None:
        return None

    async def _resolve_checkpoint_anchor(**kwargs: Any) -> Dict[str, Any]:
        assert kwargs == {"task_id": 300, "graph_name": "simple_tool"}
        return {
            "task_id": 300,
            "graph_name": "simple_tool",
            "checkpoint_id": "ckpt-non-hitl-300",
            "thread_id": "task-300",
        }

    await service.handle_terminal_turn_error(
        task_id=300,
        hub=None,
        workflow_id=9001,
        reserved_message_id=900,
        failure_source="initial_generation",
        error_code="tool_argument_invalid",
        content="tool rejected malformed url argument",
        retryable=True,
        retry_mode="checkpoint",
        graph_name="simple_tool",
        publish_boundary_completion_events=_publish,
        mark_turn_workflow_failed=lambda **kwargs: workflow_calls.append(kwargs),
        resolve_checkpoint_anchor=_resolve_checkpoint_anchor,
        diagnostics={
            "tool_name": "http_get",
            "tool_call_id": "call-77",
            # Diagnostics intentionally include a secret-looking field that
            # must NEVER appear on the persisted last_failure block.
            "raw_request_headers": {"Authorization": "Bearer sk-LEAK-ME"},
        },
    )

    assert len(workflow_calls) == 1
    assert workflow_calls[0]["checkpoint_id"] == "ckpt-non-hitl-300"
    assert workflow_calls[0]["graph_name"] == "simple_tool"
    persisted_metadata = workflow_calls[0].get("metadata") or {}
    assert persisted_metadata["checkpoint_id"] == "ckpt-non-hitl-300"
    assert persisted_metadata["retryable"] is True
    last_failure = persisted_metadata.get("last_failure")
    assert isinstance(last_failure, dict) and last_failure, (
        "retryable terminal failures must persist a sanitized last_failure block"
    )

    # Whitelisted fields must be present.
    assert last_failure["error_code"] == "tool_argument_invalid"
    assert last_failure["graph_name"] == "simple_tool"
    assert last_failure["tool_name"] == "http_get"
    assert last_failure["tool_call_id"] == "call-77"
    assert last_failure["summary"] == "tool rejected malformed url argument"
    # The writer is contractually required to label the failure stage.
    assert last_failure["failure_stage"]

    # Only the contract-approved keys are stored — no extras leak through.
    assert set(last_failure.keys()).issubset(
        {
            "error_code",
            "failure_stage",
            "graph_name",
            "tool_name",
            "tool_call_id",
            "summary",
        }
    )

    # Defensive: no secret literals from diagnostics may end up in the
    # sanitized last_failure block (the legacy ``diagnostics`` field on the
    # surrounding workflow_metadata is out of scope for this projection).
    serialized_block = repr(last_failure)
    assert "Bearer sk-LEAK-ME" not in serialized_block


@pytest.mark.asyncio
async def test_handle_terminal_turn_error_fails_closed_when_retry_checkpoint_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retryable checkpoint failures without an anchor must not advertise Retry."""
    service = TurnExecutionErrorService()
    workflow_calls: list[Dict[str, Any]] = []
    publish_calls: list[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._resolve_failure_context",
        staticmethod(
            lambda **_kwargs: {
                "conversation_id": "conv-350",
                "turn_id": "task-350-turn-9",
                "turn_sequence": 9,
                "reserved_message_id": 950,
            }
        ),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._persist_assistant_error_message",
        staticmethod(lambda **_kwargs: None),
    )

    async def _publish(**kwargs: Any) -> None:
        publish_calls.append(kwargs)

    async def _missing_anchor(**_kwargs: Any) -> None:
        return None

    await service.handle_terminal_turn_error(
        task_id=350,
        hub=object(),
        workflow_id=9501,
        reserved_message_id=950,
        failure_source="initial_generation",
        error_code="tool_argument_invalid",
        content="tool rejected malformed url argument",
        retryable=True,
        retry_mode="checkpoint",
        graph_name="simple_tool",
        publish_boundary_completion_events=_publish,
        mark_turn_workflow_failed=lambda **kwargs: workflow_calls.append(kwargs),
        resolve_checkpoint_anchor=_missing_anchor,
    )

    persisted_metadata = workflow_calls[0].get("metadata") or {}
    assert persisted_metadata["retryable"] is False
    assert persisted_metadata["retry_unavailable_reason"] == "missing_checkpoint"
    assert "checkpoint_id" not in workflow_calls[0]
    assert "last_failure" not in persisted_metadata

    boundary_metadata = publish_calls[0]["base_metadata"]
    assert boundary_metadata.get("retryable") is not True
    assert boundary_metadata["retry_mode"] == "checkpoint"
    assert boundary_metadata["retry_unavailable_reason"] == "missing_checkpoint"


@pytest.mark.asyncio
async def test_handle_terminal_turn_error_does_not_persist_last_failure_for_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-retryable terminal failures must not persist last_failure."""
    service = TurnExecutionErrorService()
    workflow_calls: list[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._resolve_failure_context",
        staticmethod(
            lambda **_kwargs: {
                "conversation_id": "conv-400",
                "turn_id": "task-400-turn-2",
                "turn_sequence": 2,
                "reserved_message_id": 1000,
            }
        ),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._persist_assistant_error_message",
        staticmethod(lambda **_kwargs: None),
    )

    async def _publish(**_kwargs: Any) -> None:
        return None

    await service.handle_terminal_turn_error(
        task_id=400,
        hub=None,
        workflow_id=10001,
        reserved_message_id=1000,
        failure_source="initial_generation",
        error_code="generation_failed",
        content="terminal non-retryable failure",
        retryable=False,
        retry_mode=None,
        graph_name=None,
        publish_boundary_completion_events=_publish,
        mark_turn_workflow_failed=lambda **kwargs: workflow_calls.append(kwargs),
    )

    assert len(workflow_calls) == 1
    persisted_metadata = workflow_calls[0].get("metadata") or {}
    assert "last_failure" not in persisted_metadata


@pytest.mark.asyncio
async def test_handle_terminal_turn_error_compression_failure_persists_last_failure_without_tool_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compression failures persist last_failure with stage=compression and no tool fields."""
    service = TurnExecutionErrorService()
    workflow_calls: list[Dict[str, Any]] = []

    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._resolve_failure_context",
        staticmethod(
            lambda **_kwargs: {
                "conversation_id": "conv-500",
                "turn_id": "task-500-turn-3",
                "turn_sequence": 3,
                "reserved_message_id": 1100,
            }
        ),
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.execution.error_service.TurnExecutionErrorService._persist_assistant_error_message",
        staticmethod(lambda **_kwargs: None),
    )

    async def _publish(**_kwargs: Any) -> None:
        return None

    await service.handle_terminal_turn_error(
        task_id=500,
        hub=None,
        workflow_id=11001,
        reserved_message_id=1100,
        failure_source="initial_generation",
        error_code="compression_required_failed",
        content="context compression failed",
        retryable=True,
        retry_mode="checkpoint",
        graph_name="simple_tool",
        checkpoint_id="ckpt-500",
        publish_boundary_completion_events=_publish,
        mark_turn_workflow_failed=lambda **kwargs: workflow_calls.append(kwargs),
    )

    persisted_metadata = workflow_calls[0].get("metadata") or {}
    last_failure = persisted_metadata.get("last_failure")
    assert isinstance(last_failure, dict)
    assert last_failure["error_code"] == "compression_required_failed"
    assert last_failure["failure_stage"] == "compression"
    assert "tool_name" not in last_failure
    assert "tool_call_id" not in last_failure
