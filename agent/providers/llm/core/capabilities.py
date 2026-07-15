"""Provider-neutral LLM capability names and membership helpers.

This module defines feature names used by provider and model profiles. It does
not store provider metadata, construct clients, or translate provider-native
request payloads.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class LLMCapability(str, Enum):
    """Provider-neutral capabilities exposed by providers or concrete models."""

    CHAT = "chat"
    STREAMING = "streaming"
    TOOLS = "tools"
    PARALLEL_TOOLS = "parallel_tools"
    STRUCTURED_OUTPUT_NATIVE = "structured_output_native"
    STRUCTURED_OUTPUT_TOOL_FALLBACK = "structured_output_tool_fallback"
    USAGE_REPORTING = "usage_reporting"
    STREAMING_USAGE_REPORTING = "streaming_usage_reporting"
    REASONING_EFFORT = "reasoning_effort"
    REMOTE_CONVERSATION_LIFECYCLE = "remote_conversation_lifecycle"
    CONTEXT_WINDOW = "context_window"
    MAX_OUTPUT_TOKENS = "max_output_tokens"


CapabilityInput = LLMCapability | str


def normalize_capability(capability: CapabilityInput) -> LLMCapability:
    """Normalize a capability value to ``LLMCapability``."""
    if isinstance(capability, LLMCapability):
        return capability
    try:
        return LLMCapability(str(capability).strip())
    except ValueError as exc:
        allowed = ", ".join(cap.value for cap in LLMCapability)
        raise ValueError(f"Unknown LLM capability '{capability}'. Allowed: {allowed}") from exc


def freeze_capabilities(capabilities: Iterable[CapabilityInput]) -> frozenset[LLMCapability]:
    """Normalize an iterable of capability values into an immutable set."""
    return frozenset(normalize_capability(capability) for capability in capabilities)


def has_capability(
    capabilities: Iterable[CapabilityInput],
    capability: CapabilityInput,
) -> bool:
    """Return True when a capability set contains the requested capability."""
    normalized = freeze_capabilities(capabilities)
    return normalize_capability(capability) in normalized


__all__ = [
    "CapabilityInput",
    "LLMCapability",
    "freeze_capabilities",
    "has_capability",
    "normalize_capability",
]
