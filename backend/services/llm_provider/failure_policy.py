"""Classify provider-neutral LLM runtime failures for application retry policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agent.providers.llm.core.exceptions import (
    LLMAPIError,
    LLMCapabilityNotSupportedError,
    LLMConfigurationError,
    LLMProfileNotFoundError,
    LLMProviderNotFoundError,
    LLMRefusalError,
    LLMResponseError,
)
from core.llm.timeout_runtime import LLMTimeoutError

from .types import (
    CredentialAuthorizationError,
    LLMProviderServiceError,
    ProviderConfigurationError,
)

LLMRuntimeFailureKind = Literal[
    "timeout",
    "provider_api",
    "refusal",
    "response",
    "configuration",
    "authorization",
    "unsupported",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class LLMRuntimeFailureDisposition:
    """Safe failure category and retry decision for one LLM runtime error."""

    kind: LLMRuntimeFailureKind
    retryable: bool


def classify_llm_runtime_failure(exc: Exception) -> LLMRuntimeFailureDisposition:
    """Return a bounded retry decision without exposing provider error text."""

    if isinstance(exc, LLMTimeoutError):
        return LLMRuntimeFailureDisposition(kind="timeout", retryable=True)
    if isinstance(exc, LLMAPIError):
        status_code = exc.status_code
        retryable = (
            status_code is None
            or status_code in {408, 409, 425, 429}
            or (isinstance(status_code, int) and status_code >= 500)
        )
        return LLMRuntimeFailureDisposition(kind="provider_api", retryable=retryable)
    if isinstance(exc, LLMRefusalError):
        return LLMRuntimeFailureDisposition(kind="refusal", retryable=False)
    if isinstance(exc, LLMResponseError):
        return LLMRuntimeFailureDisposition(kind="response", retryable=True)
    if isinstance(exc, CredentialAuthorizationError):
        return LLMRuntimeFailureDisposition(kind="authorization", retryable=False)
    if isinstance(exc, LLMCapabilityNotSupportedError):
        return LLMRuntimeFailureDisposition(kind="unsupported", retryable=False)
    if isinstance(
        exc,
        (
            ProviderConfigurationError,
            LLMConfigurationError,
            LLMProfileNotFoundError,
            LLMProviderNotFoundError,
        ),
    ):
        return LLMRuntimeFailureDisposition(kind="configuration", retryable=False)
    if isinstance(exc, LLMProviderServiceError):
        return LLMRuntimeFailureDisposition(kind="unknown", retryable=False)
    return LLMRuntimeFailureDisposition(kind="unknown", retryable=False)


__all__ = [
    "LLMRuntimeFailureDisposition",
    "LLMRuntimeFailureKind",
    "classify_llm_runtime_failure",
]
