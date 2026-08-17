"""Tests for provider-neutral LLM API retry decisions and delay handling."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.providers.llm.core.exceptions import LLMAPIError
from core.llm.api_retry import (
    DEFAULT_API_RETRY_COUNT,
    INITIAL_API_RETRY_DELAY_SECONDS,
    calculate_retry_delay,
    is_retryable_api_error,
    retry_after_seconds_from_headers,
)


def test_default_retry_policy_uses_three_exponential_waits() -> None:
    assert DEFAULT_API_RETRY_COUNT == 3
    assert INITIAL_API_RETRY_DELAY_SECONDS == 1.0
    assert [
        calculate_retry_delay(attempt, random_value=0.0)
        for attempt in range(1, DEFAULT_API_RETRY_COUNT + 1)
    ] == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("status_code", (None, 408, 429, 500, 502, 503, 504, 529))
def test_transient_api_failures_are_retryable(status_code: int | None) -> None:
    assert is_retryable_api_error(LLMAPIError("failed", status_code=status_code))


@pytest.mark.parametrize("status_code", (400, 401, 403, 404, 409, 422, 501))
def test_permanent_api_failures_are_not_retryable(status_code: int) -> None:
    assert not is_retryable_api_error(
        LLMAPIError("failed", status_code=status_code)
    )


def test_retry_after_numeric_header_is_bounded() -> None:
    assert retry_after_seconds_from_headers({"Retry-After": "3.5"}) == 3.5
    assert retry_after_seconds_from_headers({"retry-after": "600"}) == 60.0
    assert retry_after_seconds_from_headers({"retry-after": "nan"}) is None


def test_retry_after_http_date_is_supported() -> None:
    now = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)

    assert retry_after_seconds_from_headers(
        {"Retry-After": "Thu, 13 Aug 2026 10:00:12 GMT"},
        now=now,
    ) == 12.0


def test_provider_retry_after_takes_precedence_over_backoff() -> None:
    error = LLMAPIError("limited", status_code=429, retry_after_seconds=7.0)

    assert calculate_retry_delay(3, error=error, random_value=1.0) == 7.0
    assert calculate_retry_delay(3, random_value=0.0) == 4.0
