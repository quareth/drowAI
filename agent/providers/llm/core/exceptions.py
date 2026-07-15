"""Exception classes for LLM provider errors.

This module defines explicit exception types for all LLM provider failure modes.
Never catch these exceptions silently - they indicate real problems that must
be surfaced to callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMRefusalOutcome:
    """Provider-neutral structured refusal returned by a successful API call."""

    provider: str
    model: str
    category: str | None = None
    explanation: str | None = None
    response_id: str | None = None
    usage: Any = None
    partial_content: str | None = None


class LLMProviderError(Exception):
    """Base exception for all LLM provider errors.
    
    All provider-specific exceptions inherit from this class, allowing callers
    to catch any provider error with a single except clause when appropriate.
    
    Attributes:
        message: Human-readable error description
        provider: Optional name of the provider that raised the error
    """
    
    def __init__(self, message: str, *, provider: str | None = None) -> None:
        self.message = message
        self.provider = provider
        super().__init__(message)
    
    def __str__(self) -> str:
        if self.provider:
            return f"[{self.provider}] {self.message}"
        return self.message


class LLMConfigurationError(LLMProviderError):
    """Raised when provider configuration is invalid.
    
    Examples:
        - Missing or empty API key
        - Invalid model identifier
        - Unsupported configuration options
    """
    pass


class LLMAPIError(LLMProviderError):
    """Raised when the underlying LLM API call fails.
    
    This wraps provider-specific API errors (e.g., OpenAI's APIError) into
    a consistent exception type. The original exception is preserved in
    the __cause__ chain.
    
    Examples:
        - Network errors
        - Authentication failures  
        - Rate limiting
        - Server errors (5xx)
        - Invalid request errors (4xx)
    
    Attributes:
        status_code: Optional HTTP status code from the API response
    """
    
    def __init__(
        self, 
        message: str, 
        *, 
        provider: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.status_code = status_code


class LLMResponseError(LLMProviderError):
    """Raised when the API response cannot be parsed or is invalid.
    
    Examples:
        - Empty response content
        - Malformed JSON in response
        - Missing expected fields
        - Unexpected response structure
    """


class LLMRefusalError(LLMResponseError):
    """Raised when a provider returns a successful refusal outcome."""

    def __init__(
        self,
        message: str,
        *,
        outcome: LLMRefusalOutcome | None = None,
        provider: str | None = None,
        model: str | None = None,
        category: str | None = None,
        explanation: str | None = None,
        response_id: str | None = None,
        usage: Any = None,
        partial_content: str | None = None,
        stop_details: dict[str, object] | None = None,
    ) -> None:
        resolved_outcome = outcome or LLMRefusalOutcome(
            provider=provider or "",
            model=model or "",
            category=category,
            explanation=explanation,
            response_id=response_id,
            usage=usage,
            partial_content=partial_content,
        )
        super().__init__(message, provider=resolved_outcome.provider or provider)
        self.outcome = resolved_outcome
        self.model = resolved_outcome.model
        self.category = resolved_outcome.category
        self.explanation = resolved_outcome.explanation
        self.response_id = resolved_outcome.response_id
        self.usage = resolved_outcome.usage
        self.partial_content = resolved_outcome.partial_content
        self.stop_details = stop_details or {}


class LLMProfileNotFoundError(LLMProviderError):
    """Raised when provider or model profile metadata is missing."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.model = model

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.model:
            parts.append(f"Requested model: {self.model}")
        return " | ".join(parts)


class LLMCapabilityNotSupportedError(LLMProviderError):
    """Raised when a provider or model does not support a requested capability."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        capability: str | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.capability = capability

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.capability:
            parts.append(f"Capability: {self.capability}")
        return " | ".join(parts)


class LLMStructuredOutputParseError(LLMResponseError):
    """Raised when provider-level structured output parsing fails.

    This captures the schema contract, parse reason, raw text, and any
    best-effort provider diagnostics so callers can decide whether to retry,
    fall back to plain-text parsing, or surface a richer user-facing error.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        schema_name: str,
        parse_reason: str,
        raw_content: str,
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.schema_name = schema_name
        self.parse_reason = parse_reason
        self.raw_content = raw_content
        self.diagnostics = diagnostics or {}


class LLMProviderNotFoundError(LLMProviderError):
    """Raised when no provider matches the requested model.
    
    This indicates that the model identifier is not recognized by any
    registered provider. Check that:
    1. The model name is spelled correctly
    2. The appropriate provider is registered with the factory
    
    Attributes:
        model: The model identifier that was requested
        available_prefixes: List of registered provider prefixes
    """
    
    def __init__(
        self, 
        message: str, 
        *, 
        model: str | None = None,
        available_prefixes: list[str] | None = None,
    ) -> None:
        super().__init__(message, provider=None)
        self.model = model
        self.available_prefixes = available_prefixes or []
    
    def __str__(self) -> str:
        parts = [self.message]
        if self.model:
            parts.append(f"Requested model: {self.model}")
        if self.available_prefixes:
            parts.append(f"Available prefixes: {', '.join(sorted(self.available_prefixes))}")
        return " | ".join(parts)


__all__ = [
    "LLMProviderError",
    "LLMConfigurationError",
    "LLMAPIError",
    "LLMResponseError",
    "LLMRefusalOutcome",
    "LLMRefusalError",
    "LLMStructuredOutputParseError",
    "LLMProviderNotFoundError",
    "LLMProfileNotFoundError",
    "LLMCapabilityNotSupportedError",
]
