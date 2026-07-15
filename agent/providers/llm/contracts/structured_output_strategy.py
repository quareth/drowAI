"""Provider-neutral structured-output strategy selection helpers.

This module names, validates, and selects the strategies model profiles may
advertise. It does not build provider-native payloads or parse provider
responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, cast

from ..core.base import StructuredOutputSpec
from ..core.exceptions import LLMCapabilityNotSupportedError

StructuredOutputStrategy = Literal[
    "native_schema",
    "strict_tool",
    "non_strict_tool",
    "prompt_parse",
]

STRUCTURED_OUTPUT_STRATEGIES: frozenset[str] = frozenset(
    (
        "native_schema",
        "strict_tool",
        "non_strict_tool",
        "prompt_parse",
    )
)


@dataclass(frozen=True, slots=True)
class StructuredOutputFallbackPolicy:
    """Explicit fallback policy for non-native structured-output requests."""

    allow_prompt_parse: bool = False
    allow_strict_prompt_parse: bool = False


@dataclass(frozen=True, slots=True)
class StructuredOutputStrategySelection:
    """Selected provider-neutral strategy for one structured-output request."""

    spec: StructuredOutputSpec
    strategy: StructuredOutputStrategy


def normalize_structured_output_strategy(strategy: str) -> StructuredOutputStrategy:
    """Normalize and validate a provider-neutral structured-output strategy."""
    normalized = str(strategy).strip().lower()
    if normalized not in STRUCTURED_OUTPUT_STRATEGIES:
        allowed = ", ".join(sorted(STRUCTURED_OUTPUT_STRATEGIES))
        raise ValueError(
            f"Unknown structured-output strategy '{strategy}'. Allowed: {allowed}"
        )
    return cast(StructuredOutputStrategy, normalized)


def freeze_structured_output_strategies(
    strategies: Iterable[str],
) -> frozenset[str]:
    """Normalize an iterable of structured-output strategies into an immutable set."""
    return frozenset(
        normalize_structured_output_strategy(strategy) for strategy in strategies
    )


def select_structured_output_strategy(
    spec: StructuredOutputSpec | None,
    *,
    allowed_strategies: Iterable[str],
    supports_native_schema: bool,
    supports_tool_fallback: bool = False,
    fallback_policy: StructuredOutputFallbackPolicy | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> StructuredOutputStrategySelection | None:
    """Select the first safe strategy for a structured-output request.

    Selection is intentionally deterministic: native schema, strict tool,
    non-strict tool for non-strict requests, then prompt-and-parse only when
    explicitly allowed. Strict requests never downgrade to prompt-only parsing
    unless the caller opts into provider-local validation explicitly.
    """
    if spec is None:
        return None

    allowed = freeze_structured_output_strategies(allowed_strategies)
    policy = fallback_policy or StructuredOutputFallbackPolicy()

    if supports_native_schema and "native_schema" in allowed:
        return StructuredOutputStrategySelection(spec=spec, strategy="native_schema")

    if supports_tool_fallback and "strict_tool" in allowed:
        return StructuredOutputStrategySelection(spec=spec, strategy="strict_tool")

    if not spec.strict:
        if supports_tool_fallback and "non_strict_tool" in allowed:
            return StructuredOutputStrategySelection(
                spec=spec,
                strategy="non_strict_tool",
            )
    if policy.allow_prompt_parse and "prompt_parse" in allowed:
        if not spec.strict or policy.allow_strict_prompt_parse:
            return StructuredOutputStrategySelection(spec=spec, strategy="prompt_parse")

    subject = f"Provider '{provider}'" if provider else "Provider"
    if model:
        subject = f"{subject} model '{model}'"
    strict_label = "strict" if spec.strict else "non-strict"
    raise LLMCapabilityNotSupportedError(
        (
            f"{subject} cannot satisfy {strict_label} structured "
            f"output for schema '{spec.name}' with advertised strategies "
            f"{sorted(allowed)}"
        ),
        provider=provider,
        capability=f"structured_output:{strict_label}",
    )


__all__ = [
    "STRUCTURED_OUTPUT_STRATEGIES",
    "StructuredOutputFallbackPolicy",
    "StructuredOutputStrategySelection",
    "StructuredOutputStrategy",
    "freeze_structured_output_strategies",
    "normalize_structured_output_strategy",
    "select_structured_output_strategy",
]
