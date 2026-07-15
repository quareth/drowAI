"""Tests for provider-neutral LLM runtime failure classification."""

from __future__ import annotations

import pytest

from agent.providers.llm.core.exceptions import (
    LLMAPIError,
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
    LLMRefusalError,
    LLMResponseError,
)
from backend.services.llm_provider.failure_policy import classify_llm_runtime_failure
from core.llm.timeout_runtime import LLMTimeoutError


@pytest.mark.parametrize(
    ("error", "kind", "retryable"),
    (
        (
            LLMTimeoutError(
                task_id=None,
                component="reporting",
                operation="section",
                timeout_sec=1,
                outcome="timed_out",
            ),
            "timeout",
            True,
        ),
        (LLMAPIError("limited", status_code=429), "provider_api", True),
        (LLMAPIError("server", status_code=503), "provider_api", True),
        (LLMResponseError("invalid response"), "response", True),
        (LLMRefusalError("declined"), "refusal", False),
        (LLMAPIError("unauthorized", status_code=401), "provider_api", False),
        (LLMConfigurationError("invalid model"), "configuration", False),
        (
            LLMCapabilityNotSupportedError("unsupported"),
            "unsupported",
            False,
        ),
        (RuntimeError("programming defect"), "unknown", False),
    ),
)
def test_classifies_retryable_and_terminal_runtime_failures(
    error: Exception,
    kind: str,
    retryable: bool,
) -> None:
    disposition = classify_llm_runtime_failure(error)

    assert disposition.kind == kind
    assert disposition.retryable is retryable
