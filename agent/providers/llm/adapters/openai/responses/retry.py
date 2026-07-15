"""Provide retry utilities for the OpenAI Responses provider.

This module owns provider-local API error wrapping, retry logging, and backoff
timing helpers without changing the calling methods' retry control flow.
"""

from __future__ import annotations

import asyncio
import logging
import random

from ....core.exceptions import LLMAPIError

DEFAULT_RETRY_COUNT = 2
INITIAL_RETRY_DELAY = 0.5


def wrap_api_error(error: Exception) -> LLMAPIError:
    """Wrap OpenAI SDK exceptions into LLMAPIError."""
    status_code = None
    if hasattr(error, "status_code"):
        status_code = error.status_code

    return LLMAPIError(
        f"OpenAI Responses API error: {error}",
        provider="OpenAI",
        status_code=status_code,
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
    initial_retry_delay: float = INITIAL_RETRY_DELAY,
) -> None:
    """Sleep with exponential backoff and jitter."""
    delay = initial_retry_delay * (2 ** (attempt - 1))
    jitter = delay * random.random() * 0.25
    sleep_duration = delay + jitter

    logger.debug(f"Backing off for {sleep_duration:.2f}s before retry")
    await asyncio.sleep(sleep_duration)
