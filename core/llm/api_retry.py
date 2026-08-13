"""Provider-neutral retry decisions and bounded delay handling for LLM APIs."""

from __future__ import annotations

import asyncio
import logging
import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504, 529})
_MAX_RETRY_AFTER_SECONDS = 60.0
DEFAULT_API_RETRY_COUNT = 3
INITIAL_API_RETRY_DELAY_SECONDS = 1.0


def is_retryable_api_error(error: Exception) -> bool:
    """Return whether a normalized API failure is transient enough to retry."""

    status_code = getattr(error, "status_code", None)
    return status_code is None or status_code in _RETRYABLE_HTTP_STATUS_CODES


def retry_after_seconds_from_headers(
    headers: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse a standard Retry-After header and clamp untrusted delays."""

    if not headers:
        return None
    value = next(
        (
            header_value
            for name, header_value in headers.items()
            if str(name).lower() == "retry-after"
        ),
        None,
    )
    if value is None:
        return None
    raw_value = str(value).strip()
    if not raw_value:
        return None
    try:
        delay = float(raw_value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(raw_value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            reference = now or datetime.now(timezone.utc)
            delay = (retry_at - reference).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(delay):
        return None
    if delay < 0:
        return 0.0
    return min(delay, _MAX_RETRY_AFTER_SECONDS)


def retry_after_seconds_from_error(error: Exception) -> float | None:
    """Read a normalized delay or a provider SDK response header."""

    normalized = getattr(error, "retry_after_seconds", None)
    if isinstance(normalized, (int, float)) and not isinstance(normalized, bool):
        normalized_delay = float(normalized)
        if math.isfinite(normalized_delay):
            return min(max(normalized_delay, 0.0), _MAX_RETRY_AFTER_SECONDS)
    response = getattr(error, "response", None)
    return retry_after_seconds_from_headers(getattr(response, "headers", None))


def calculate_retry_delay(
    attempt: int,
    *,
    error: Exception | None = None,
    initial_retry_delay: float = INITIAL_API_RETRY_DELAY_SECONDS,
    random_value: float | None = None,
) -> float:
    """Return Retry-After when supplied, otherwise exponential jittered delay."""

    retry_after = (
        retry_after_seconds_from_error(error) if error is not None else None
    )
    if retry_after is not None:
        return retry_after
    delay = initial_retry_delay * (2 ** max(attempt - 1, 0))
    jitter_seed = random.random() if random_value is None else random_value
    return delay + (delay * min(max(jitter_seed, 0.0), 1.0) * 0.25)


async def sleep_before_retry(
    attempt: int,
    *,
    error: Exception | None = None,
    initial_retry_delay: float = INITIAL_API_RETRY_DELAY_SECONDS,
    logger: logging.Logger | None = None,
) -> None:
    """Sleep for the common bounded provider retry delay."""

    delay = calculate_retry_delay(
        attempt,
        error=error,
        initial_retry_delay=initial_retry_delay,
    )
    if logger is not None:
        logger.debug("Backing off for %.2fs before LLM API retry", delay)
    await asyncio.sleep(delay)


__all__ = [
    "DEFAULT_API_RETRY_COUNT",
    "INITIAL_API_RETRY_DELAY_SECONDS",
    "calculate_retry_delay",
    "is_retryable_api_error",
    "retry_after_seconds_from_error",
    "retry_after_seconds_from_headers",
    "sleep_before_retry",
]
