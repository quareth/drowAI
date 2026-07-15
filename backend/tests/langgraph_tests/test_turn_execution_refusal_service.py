"""Tests for provider-refusal sanitization and terminal chat side effects."""

from __future__ import annotations

from typing import Any

import pytest

from agent.providers.llm.core.exceptions import LLMRefusalError, LLMRefusalOutcome
from backend.services.langgraph_chat.execution.refusal_service import (
    TurnExecutionRefusalService,
    sanitize_refusal_outcome,
)


class _ContextErrorService:
    """Provide deterministic terminal context for refusal-service tests."""

    @staticmethod
    def resolve_failure_context(**_kwargs: Any) -> dict[str, Any]:
        return {
            "conversation_id": "conv-refusal",
            "turn_id": "turn-refusal",
            "turn_sequence": 4,
            "reserved_message_id": 44,
            "graph_name": "normal_chat",
            "checkpoint_id": "ckpt-refusal",
        }


def test_sanitize_refusal_outcome_bounds_fields_and_builds_safe_summary() -> None:
    refusal = sanitize_refusal_outcome(
        LLMRefusalOutcome(
            provider="anthropic\x00\n",
            model="claude-fable-5\u200b",
            category="cyber<script>",
            explanation="  Provider\n explanation\x00  " + ("x" * 3000),
            response_id="msg\x00_123",
            partial_content="partial",
        )
    )

    assert refusal["provider"] == "anthropic"
    assert refusal["model"] == "claude-fable-5"
    assert refusal["category"] == "cyber<script>"
    assert refusal["summary"] == (
        "The provider declined this request under its cyberscript safety policy."
    )
    assert refusal["explanation"].startswith("Provider explanation")
    assert len(refusal["explanation"]) == 2000
    assert refusal["response_id"] == "msg_123"
    assert refusal["partial"] is True


def test_extract_refusal_outcome_finds_nested_cause() -> None:
    outcome = LLMRefusalOutcome(provider="openai", model="gpt-5.6")
    refusal = LLMRefusalError("declined", outcome=outcome)
    outer = RuntimeError("graph failed")
    outer.__cause__ = refusal

    assert TurnExecutionRefusalService().extract_refusal_outcome(outer) is outcome


@pytest.mark.asyncio
async def test_terminal_refusal_persists_declined_without_error_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TurnExecutionRefusalService(
        error_service=_ContextErrorService(),  # type: ignore[arg-type]
    )
    workflow_calls: list[dict[str, Any]] = []
    persist_calls: list[dict[str, Any]] = []
    publish_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        TurnExecutionRefusalService,
        "_persist_assistant_refusal",
        staticmethod(lambda **kwargs: persist_calls.append(kwargs)),
    )

    async def publish(**kwargs: Any) -> None:
        publish_calls.append(kwargs)

    await service.handle_terminal_turn_refusal(
        outcome=LLMRefusalOutcome(
            provider="anthropic",
            model="claude-fable-5",
            category="cyber",
            explanation="Blocked by provider policy.",
            response_id="msg_123",
            partial_content="Partial answer",
        ),
        task_id=4,
        hub=object(),
        workflow_id=8,
        reserved_message_id=44,
        publish_boundary_completion_events=publish,
        mark_turn_workflow_failed=lambda **kwargs: workflow_calls.append(kwargs),
    )

    assert workflow_calls[0]["replace_metadata"] is True
    metadata = workflow_calls[0]["metadata"]
    assert metadata["outcome_type"] == "provider_refusal"
    assert metadata["retryable"] is False
    assert "error" not in metadata
    assert "error_code" not in metadata
    assert persist_calls == [
        {"reserved_message_id": 44, "content": "Partial answer"}
    ]
    boundary = publish_calls[0]["base_metadata"]
    assert boundary["status"] == "declined"
    assert boundary["stop_reason"] == "refusal"
    assert boundary["retryable"] is False
    assert "error" not in boundary

