"""Tests for provider-neutral LLM response recovery policy."""

from __future__ import annotations

from agent.providers.llm.contracts.recovery import ResponseParseRetryState
from agent.providers.llm.core.exceptions import (
    LLMResponseError,
    LLMStructuredOutputParseError,
)


def test_response_parse_retry_state_retries_then_fails_closed() -> None:
    state = ResponseParseRetryState()

    exc = LLMStructuredOutputParseError(
        "cut response",
        provider="test",
        schema_name="tool_selector",
        parse_reason="json_decode_error",
        raw_content='{"selected_tools":["dnsenum"],"execution_strategy',
    )

    assert state.should_retry(exc, attempt=1, max_attempts=10) is True
    assert state.should_retry(exc, attempt=2, max_attempts=10) is True
    assert state.should_retry(exc, attempt=3, max_attempts=10) is False


def test_response_parse_retry_state_rejects_non_retryable_schema_error() -> None:
    state = ResponseParseRetryState()
    exc = LLMStructuredOutputParseError(
        "schema mismatch",
        provider="test",
        schema_name="tool_selector",
        parse_reason="schema_validation_error",
        raw_content='{"selected_tools":[],"execution_strategy":"sequential"}',
    )

    assert state.should_retry(exc, attempt=1, max_attempts=10) is False
    assert state.consecutive_failures == 0


def test_response_parse_retry_state_retries_empty_content() -> None:
    state = ResponseParseRetryState()

    assert (
        state.should_retry(
            LLMResponseError("provider returned empty content"),
            attempt=1,
            max_attempts=2,
        )
        is True
    )
