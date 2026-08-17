"""Provide retry utilities for the OpenAI Responses provider.

This module owns provider-local API error wrapping, retry logging, and backoff
timing helpers without changing the calling methods' retry control flow.
"""

from __future__ import annotations

import logging

from core.llm.api_retry import (
    DEFAULT_API_RETRY_COUNT,
    INITIAL_API_RETRY_DELAY_SECONDS,
    retry_after_seconds_from_error,
    sleep_before_retry,
)

from ....core.exceptions import LLMAPIError

DEFAULT_RETRY_COUNT = DEFAULT_API_RETRY_COUNT
INITIAL_RETRY_DELAY = INITIAL_API_RETRY_DELAY_SECONDS


def wrap_api_error(error: Exception) -> LLMAPIError:
    """Wrap OpenAI SDK exceptions into LLMAPIError."""
    status_code = None
    if hasattr(error, "status_code"):
        status_code = error.status_code

    return LLMAPIError(
        f"OpenAI Responses API error: {error}",
        provider="OpenAI",
        status_code=status_code,
        retry_after_seconds=retry_after_seconds_from_error(error),
    )


def log_retry(
    logger: logging.Logger,
    attempt: int,
    error: Exception,
    max_attempts: int,
) -> None:
    """Log retry attempt."""
    logger.debug(
        f"Responses API request attempt {attempt}/{max_attempts} failed: {error}; "
        f"retrying..."
    )


async def backoff_sleep(
    logger: logging.Logger,
    attempt: int,
    *,
    error: LLMAPIError | None = None,
    initial_retry_delay: float = INITIAL_RETRY_DELAY,
) -> None:
    """Sleep with exponential backoff and jitter."""
    await sleep_before_retry(
        attempt,
        error=error,
        initial_retry_delay=initial_retry_delay,
        logger=logger,
    )
