"""Shared recovery helpers for LLM structured output parse failures.

Provides diagnostics extraction and logging for the common pattern:
try structured output -> catch parse failure -> retry with plain-text fallback.
Used by any LLM call site that requests structured output and needs graceful
degradation when the provider returns malformed or truncated JSON.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..core.exceptions import (
    LLMRefusalError,
    LLMResponseError,
    LLMStructuredOutputParseError,
)

_RETRYABLE_STRUCTURED_PARSE_REASONS = frozenset(
    {
        "empty_content",
        "json_decode_error",
    }
)

_RETRYABLE_RESPONSE_ERROR_MARKERS = (
    "empty content",
    "empty response",
    "not valid json",
    "unterminated string",
)

MAX_CONSECUTIVE_RESPONSE_PARSE_FAILURES = 3
"""Maximum exact-call retries for repeated provider response parse failures."""


def is_retryable_response_parse_error(exc: Exception) -> bool:
    """Return True when repeating the exact provider request may recover.

    This is intentionally narrow: retry cut/truncated/malformed provider
    response text and empty content, but do not retry schema/semantic contract
    violations where the provider returned a complete parseable answer.
    """

    if isinstance(exc, LLMRefusalError):
        return False

    if isinstance(exc, LLMStructuredOutputParseError):
        return exc.parse_reason in _RETRYABLE_STRUCTURED_PARSE_REASONS

    if isinstance(exc, LLMResponseError):
        message = str(exc).strip().lower()
        return any(marker in message for marker in _RETRYABLE_RESPONSE_ERROR_MARKERS)

    return False


@dataclass
class ResponseParseRetryState:
    """Provider-neutral retry guard for malformed full-response payloads."""

    consecutive_failures: int = 0
    max_consecutive_failures: int = MAX_CONSECUTIVE_RESPONSE_PARSE_FAILURES

    def should_retry(self, exc: Exception, *, attempt: int, max_attempts: int) -> bool:
        """Return True when the exact same provider request should be replayed."""

        if not is_retryable_response_parse_error(exc):
            return False

        self.consecutive_failures += 1
        return (
            self.consecutive_failures < self.max_consecutive_failures
            and attempt < max_attempts
        )


def build_provider_recovery_diagnostics(exc: Exception) -> dict[str, Any]:
    """Build normalized diagnostics dict from a provider-side failure.

    Works with LLMStructuredOutputParseError, LLMResponseError,
    and generic exceptions.
    """
    if isinstance(exc, LLMStructuredOutputParseError):
        diagnostics: dict[str, Any] = dict(exc.diagnostics)
        diagnostics.setdefault("schema_name", exc.schema_name)
        diagnostics.setdefault("parse_reason", exc.parse_reason)
        diagnostics.setdefault("provider_error", str(exc))
        return diagnostics

    if isinstance(exc, LLMResponseError):
        return {
            "provider_error": str(exc),
            "reason": "empty_or_invalid_provider_content",
        }

    return {"provider_error": str(exc)}


def log_provider_recovery_attempt(
    exc: Exception,
    diagnostics: dict[str, Any],
    *,
    target_logger: logging.Logger,
    log_prefix: str = "LLM",
) -> None:
    """Log a structured output recovery attempt with contextual diagnostics."""
    if isinstance(exc, LLMStructuredOutputParseError):
        target_logger.warning(
            "[%s] Structured output parse failed; retrying with plain-text fallback "
            "(schema=%s reason=%s response_id=%s status=%s)",
            log_prefix,
            exc.schema_name,
            exc.parse_reason,
            diagnostics.get("response_id"),
            diagnostics.get("status"),
        )
        return

    target_logger.warning(
        "[%s] Provider returned empty/invalid content; "
        "retrying with plain-text fallback (%s)",
        log_prefix,
        exc,
    )
