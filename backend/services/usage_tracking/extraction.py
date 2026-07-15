"""Provider-aware usage extraction boundary.

This module dispatches SDK response usage parsing by explicit provider,
model, and API surface. ``UsageData`` remains the normalized value object;
provider-specific response shapes belong in extractors registered here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .models import UsageData


@dataclass(frozen=True, slots=True)
class UsageExtractionTarget:
    """Explicit provider/model/API-surface identity for one usage extraction."""

    provider: str
    model: str
    api_surface: str

    @property
    def key(self) -> tuple[str, str]:
        """Return the normalized extractor registry key."""
        return (
            str(self.provider or "").strip().lower(),
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


__all__ = [
    "UnsupportedUsageExtractorError",
    "UsageExtractionTarget",
    "UsageExtractor",
    "extract_usage",
]
