"""Data models for token usage tracking.

This module defines immutable data containers for token usage. Provider
response parsing is delegated to usage extractors, while this file owns the
normalized in-process usage contracts shared by graph, persistence, pricing,
and insights code.

It is also the single source of truth for the per-call *cache-reporting*
signal: whether the API surface that produced a given usage row is known
to expose prompt-cache information, known NOT to expose it, or is simply
unclassified. This signal is extracted at the same place where tokens are
normalized (the ``UsageData.from_*`` factories) so the read side never has
to re-parse ``source`` strings to infer cache capability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Cache-reporting labels and classifier.
#
# These three literals are the only values ever written into
# ``UsageData.cache_reporting`` or ``UsageRecordMetadata.cache_reporting``.
# They are kept as module-level constants so downstream query code can rely
# on a single literal when bucketing — and so a future rename only edits
# this file.
# ---------------------------------------------------------------------------

#: The provider surface is known to report cache info AND this row carried
#: a value (even if ``0``). Insights can treat ``cached_tokens`` as truth.
CACHE_REPORTING_REPORTED: str = "reported"

#: The provider surface is known NOT to report cache info on this row
#: (for a surface that omits cache-token details). Insights must NOT render
#: ``cached_tokens == 0`` as a definitive "no cache" for such rows.
CACHE_REPORTING_NOT_REPORTED: str = "not_reported"

#: The provider / api_surface combination isn't yet classified, or the
#: upstream payload was incomplete.
CACHE_REPORTING_UNKNOWN: str = "unknown"


#: Known provider / api_surface combinations whose cache-reporting
#: capability is well understood. Anything not listed stays ``"unknown"``.
#:
#: Note: this is capability, not observation. It answers "does this API
#: surface expose cache info for this provider?" — whether a particular
#: row actually had cached tokens is orthogonal (and captured by
#: ``UsageData.cached_tokens``).
_CACHE_REPORTING_CAPABILITY: Dict[tuple[str, str], str] = {
    # OpenAI Chat Completions exposes ``prompt_tokens_details.cached_tokens``
    # on every response. Even a row with ``cached_tokens == 0`` is an
    # honest "reported zero".
    ("openai", "chat_completions"): CACHE_REPORTING_REPORTED,
    # OpenAI Responses API surfaces ``input_tokens_details.cached_tokens``.
    # A zero value from this surface is therefore an honest reported zero.
    ("openai", "responses"): CACHE_REPORTING_REPORTED,
}


def classify_cache_reporting(provider: str, api_surface: str) -> str:
    """Return whether this provider / api_surface reports cache info.

    This is the *only* place provider + api_surface are consulted to
    derive a cache-reporting label. Downstream code (the insights query
    service, the Usage page, anything reading ``request_metadata``)
    reads the label verbatim instead of re-deriving it.

    Args:
        provider: Provider name (e.g. ``"openai"``). Compared
            case-insensitively after stripping; an empty / missing
            provider always maps to ``"unknown"``.
        api_surface: Provider API surface that produced the call (e.g.
            ``"chat_completions"``, ``"responses"``). Same handling as
            ``provider``.

    Returns:
        One of ``"reported"``, ``"not_reported"``, ``"unknown"``.

    Example:
        classify_cache_reporting("openai", "chat_completions")
        # -> "reported"
        classify_cache_reporting("openai", "responses")
        # -> "reported"
        classify_cache_reporting("anthropic", "messages")
        # -> "unknown"
    """
    if not isinstance(provider, str) or not isinstance(api_surface, str):
        return CACHE_REPORTING_UNKNOWN
    key = (provider.strip().lower(), api_surface.strip().lower())
    if not key[0] or not key[1]:
        return CACHE_REPORTING_UNKNOWN
    return _CACHE_REPORTING_CAPABILITY.get(key, CACHE_REPORTING_UNKNOWN)


def _coerce_token_count(value: Any) -> int:
    """Return a non-negative token count from provider usage fields."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value.strip()))
        except ValueError:
            return 0
    return 0


def _usage_token_attr(obj: Any, attr: str) -> int:
    """Read one integer token field from an SDK object or mock."""
    if isinstance(obj, dict):
        return _coerce_token_count(obj.get(attr, 0))
    return _coerce_token_count(getattr(obj, attr, 0))


@dataclass(frozen=True, slots=True)
class ProviderUsageComponents:
    """Provider-specific token components required for billing semantics."""

    provider: str
    api_surface: str
    components: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize provider billing components into the canonical JSON shape."""
        return {
            "provider": str(self.provider or "unknown"),
            "api_surface": str(self.api_surface or "unknown"),
            "components": {
                str(key): _coerce_token_count(value)
                for key, value in dict(self.components or {}).items()
            },
        }

    @classmethod
    def from_mapping(cls, value: Any) -> Optional["ProviderUsageComponents"]:
        """Build components from the canonical JSON shape when present."""
        if isinstance(value, ProviderUsageComponents):
            return value
        if not isinstance(value, dict):
            return None
        provider = value.get("provider")
        api_surface = value.get("api_surface")
        components = value.get("components")
        if not isinstance(provider, str) or not provider.strip():
            return None
        if not isinstance(api_surface, str) or not api_surface.strip():
            return None
        if not isinstance(components, dict):
            return None
        return cls(
            provider=provider.strip().lower(),
            api_surface=api_surface.strip().lower(),
            components={
                str(key): _coerce_token_count(component_value)
                for key, component_value in components.items()
            },
        )


@dataclass(slots=True, frozen=True)
class UsageData:
    """Immutable container for token usage from a single LLM call.

    Normalizes token counts from different API formats (Chat Completions
    vs Responses API) into a consistent structure.

    Attributes:
        prompt_tokens: Number of input tokens (called input_tokens in Responses API)
        completion_tokens: Number of output tokens (called output_tokens in Responses API)
        total_tokens: Sum of prompt + completion tokens
        model: Model identifier (e.g., "gpt-4o-mini", "gpt-5")
        provider: Provider name (default: "openai")
        cached_tokens: Tokens served from cache (prompt_tokens_details.cached_tokens)
        reasoning_tokens: Provider-reported reasoning or thinking tokens
        api_surface: Provider API surface that produced the call
            (e.g. ``"chat_completions"``, ``"responses"``). Defaults to
            ``"unknown"`` so legacy constructions stay valid; the
            ``from_openai_*`` factories set this explicitly so the
            cache-reporting classifier can be applied without re-parsing
            the legacy ``source`` string.
        cache_reporting: Honest label for whether this call's API surface
            reports cache info. One of ``"reported"`` / ``"not_reported"``
            / ``"unknown"``. Derived at extraction time via
            ``classify_cache_reporting(provider, api_surface)`` so the
            read side (insights) never re-derives it from surface strings.
        provider_usage_components: Optional provider-specific token component
            breakdown for billing semantics that cannot be represented by
            normalized prompt/completion/cached fields alone.
        pricing_date: Optional effective date used for scheduled model pricing.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str
    provider: str = "openai"
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    api_surface: str = CACHE_REPORTING_UNKNOWN
    cache_reporting: str = CACHE_REPORTING_UNKNOWN
    provider_usage_components: Optional[ProviderUsageComponents] = None
    pricing_date: Optional[date] = None
    
    @classmethod
    def from_openai_chat_response(cls, response: Any, model: str) -> UsageData:
        """Extract usage from Chat Completions API response.
        
        Works with both full responses and streaming final chunks.
        
        Args:
            response: OpenAI API response object (or streaming chunk with usage)
            model: Model identifier used for the request
            
        Returns:
            UsageData with extracted token counts, or empty if no usage present
            
        Example:
            response = await client.chat.completions.create(...)
            usage = UsageData.from_openai_chat_response(response, "gpt-4o")
        """
        from backend.services.usage_tracking.extraction import (
            UsageExtractionTarget,
            extract_usage,
        )

        return extract_usage(
            response,
            UsageExtractionTarget(
                provider="openai",
                model=model,
                api_surface="chat_completions",
            ),
        )

    @classmethod
    def from_openai_responses_api(cls, response: Any, model: str) -> UsageData:
        """Extract usage from Responses API (GPT-5) response.
        
        The Responses API uses different field names:
        - input_tokens instead of prompt_tokens
        - output_tokens instead of completion_tokens
        - output_tokens_details.reasoning_tokens for extended thinking
        - input_tokens_details.cached_tokens for prompt cache hits
        
        Args:
            response: OpenAI Responses API response object
            model: Model identifier used for the request
            
        Returns:
            UsageData with extracted token counts, or empty if no usage present
            
        Example:
            response = await client.responses.create(...)
            usage = UsageData.from_openai_responses_api(response, "gpt-5")
        """
        from backend.services.usage_tracking.extraction import (
            UsageExtractionTarget,
            extract_usage,
        )

        return extract_usage(
            response,
            UsageExtractionTarget(
                provider="openai",
                model=model,
                api_surface="responses",
            ),
        )

    @classmethod
    def from_anthropic_messages_response(cls, response: Any, model: str) -> UsageData:
        """Extract usage from an Anthropic Messages API response.

        Anthropic reports ordinary input tokens plus optional prompt-cache
        creation/read token components and adaptive-thinking output details.
        Provider-aware components are preserved for pricing while all input-side
        components contribute to normalized prompt tokens.
        """
        from backend.services.usage_tracking.extraction import (
            UsageExtractionTarget,
            extract_usage,
        )

        return extract_usage(
            response,
            UsageExtractionTarget(
                provider="anthropic",
                model=model,
                api_surface="messages",
            ),
        )
    
    @classmethod
    def empty(
        cls,
        model: str = "unknown",
        *,
        provider: str = "openai",
        api_surface: str = CACHE_REPORTING_UNKNOWN,
    ) -> UsageData:
        """Return zero-usage instance for fallback scenarios.
        
        Args:
            model: Model identifier to include in the empty record
            
        Returns:
            UsageData with all zero token counts
        """
        return cls(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            model=model,
            provider=provider,
            cached_tokens=0,
            reasoning_tokens=0,
            api_surface=api_surface,
            cache_reporting=classify_cache_reporting(provider, api_surface),
        )
    
    def is_empty(self) -> bool:
        """Check if this usage record has zero tokens."""
        return self.total_tokens == 0
    
    def to_dict(self, source: str = "unknown") -> Dict[str, Any]:
        """Convert to dict format for storage in state or DB.
        
        This is the canonical method for converting UsageData to dict format.
        All code that needs to serialize usage data should use this method
        to ensure consistency across the codebase.
        
        Args:
            source: Identifier for the call site (e.g., "planner", "simple_chat")
            
        Returns:
            Dict representation with source tag
            
        Example:
            usage = UsageData.from_openai_chat_response(response, "gpt-4o")
            usage_dict = usage.to_dict("planner")
            # {"prompt_tokens": 100, "completion_tokens": 50, ..., "source": "planner"}
        """
        result = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model,
            "provider": self.provider,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            # Propagate the cache-reporting signal through the graph's
            # ``trace.usage_records`` dict so the handler-boundary
            # ``build_usage_metadata_from_trace_record`` can lift it into
            # ``UsageRecordMetadata.cache_reporting`` without re-deriving
            # the label from provider-specific shapes.
            "api_surface": self.api_surface,
            "cache_reporting": self.cache_reporting,
            "source": source,
        }
        if self.provider_usage_components is not None:
            result["provider_usage_components"] = (
                self.provider_usage_components.to_dict()
            )
        return result


@dataclass
class TaskUsageSummary:
    """Aggregated usage statistics for a task.
    
    Represents the sum of all LLM calls made for a specific task,
    with cost calculation included.
    """
    
    task_id: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_cached_tokens: int
    total_reasoning_tokens: int
    total_cost_usd: float
    call_count: int
    models_used: List[str] = field(default_factory=list)
    pricing_status: str = "available"
    unpriced_providers: List[str] = field(default_factory=list)
    unpriced_models: List[str] = field(default_factory=list)
    first_call: Optional[datetime] = None
    last_call: Optional[datetime] = None
    
    @classmethod
    def empty(cls, task_id: int) -> TaskUsageSummary:
        """Return empty summary for tasks with no usage records."""
        return cls(
            task_id=task_id,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            total_tokens=0,
            total_cached_tokens=0,
            total_reasoning_tokens=0,
            total_cost_usd=0.0,
            call_count=0,
            models_used=[],
            first_call=None,
            last_call=None,
        )
