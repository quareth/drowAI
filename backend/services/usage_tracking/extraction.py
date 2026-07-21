"""Provider-aware usage extraction boundary.

This module dispatches SDK response usage parsing by explicit provider,
model, and API surface. ``UsageData`` remains the normalized value object;
provider-specific response shapes belong in extractors registered here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .models import UsageAttributionContext, UsageData


@dataclass(frozen=True, slots=True)
class UsageExtractionTarget:
    """Explicit provider/model/API-surface identity for one usage extraction."""

    provider: str
    model: str
    api_surface: str
    parser_provider: str | None = None
    attribution: UsageAttributionContext | None = None

    @property
    def key(self) -> tuple[str, str]:
        """Return the normalized extractor registry key."""
        parser_provider = self.parser_provider or self.provider
        return (
            str(parser_provider or "").strip().lower(),
            str(self.api_surface or "").strip().lower(),
        )


class UsageExtractor(Protocol):
    """Parser for one provider API surface's usage response shape."""

    def extract(self, response: Any, target: UsageExtractionTarget) -> UsageData:
        """Extract normalized usage from one provider response."""


class UnsupportedUsageExtractorError(ValueError):
    """Raised when no extractor is registered for a provider API surface."""


def _registry() -> dict[tuple[str, str], UsageExtractor]:
    """Build the current extractor registry lazily to avoid import cycles."""
    from .extractors.anthropic import AnthropicMessagesUsageExtractor
    from .extractors.openai import (
        OpenAIChatCompletionsUsageExtractor,
        OpenAIResponsesUsageExtractor,
    )

    return {
        ("openai", "chat_completions"): OpenAIChatCompletionsUsageExtractor(),
        ("openai", "responses"): OpenAIResponsesUsageExtractor(),
        ("anthropic", "messages"): AnthropicMessagesUsageExtractor(),
    }


def extract_usage(response: Any, target: UsageExtractionTarget) -> UsageData:
    """Extract normalized usage through the provider-aware registry."""
    extractor = _registry().get(target.key)
    if extractor is None:
        provider, api_surface = target.key
        raise UnsupportedUsageExtractorError(
            f"No usage extractor registered for provider={provider!r} api_surface={api_surface!r}"
        )
    return extractor.extract(response, target)


def response_usage_attribution(
    response: Any,
    target: UsageExtractionTarget,
    *,
    usage_completeness: str,
) -> UsageAttributionContext:
    """Merge target attribution with provider response identifiers."""

    attribution = target.attribution or UsageAttributionContext(
        requested_model_id=target.model,
        api_surface=target.api_surface,
    )
    return attribution.with_updates(
        requested_model_id=attribution.requested_model_id or target.model,
        api_surface=attribution.api_surface or target.api_surface,
        provider_request_id=_response_text(response, "id"),
        reported_model_id=_response_text(response, "model"),
        usage_completeness=usage_completeness,
    )


def _response_text(response: Any, attr: str) -> str | None:
    if isinstance(response, dict):
        value = response.get(attr)
    else:
        value = getattr(response, attr, None)
    return value if isinstance(value, str) and value.strip() else None


__all__ = [
    "UnsupportedUsageExtractorError",
    "UsageExtractionTarget",
    "UsageExtractor",
    "extract_usage",
    "response_usage_attribution",
]
