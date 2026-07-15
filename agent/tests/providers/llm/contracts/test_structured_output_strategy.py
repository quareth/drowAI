"""Tests for provider-neutral structured-output strategy metadata helpers."""

from __future__ import annotations

import pytest

from agent.providers.llm.core.base import StructuredOutputSpec
from agent.providers.llm.core.exceptions import LLMCapabilityNotSupportedError
from agent.providers.llm.contracts.structured_output_strategy import (
    STRUCTURED_OUTPUT_STRATEGIES,
    StructuredOutputFallbackPolicy,
    freeze_structured_output_strategies,
    normalize_structured_output_strategy,
    select_structured_output_strategy,
)


def _spec(*, strict: bool = True) -> StructuredOutputSpec:
    """Return a minimal structured-output schema for strategy tests."""
    return StructuredOutputSpec(
        name="answer",
        schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        strict=strict,
    )


def test_known_structured_output_strategies_are_supported() -> None:
    assert STRUCTURED_OUTPUT_STRATEGIES == frozenset(
        ("native_schema", "strict_tool", "non_strict_tool", "prompt_parse")
    )
    assert normalize_structured_output_strategy(" NATIVE_SCHEMA ") == "native_schema"
    assert freeze_structured_output_strategies(("strict_tool", "prompt_parse")) == frozenset(
        ("strict_tool", "prompt_parse")
    )


def test_unknown_structured_output_strategy_fails_loudly() -> None:
    with pytest.raises(ValueError, match="Unknown structured-output strategy"):
        normalize_structured_output_strategy("json_mode")


def test_strategy_selection_prefers_native_schema() -> None:
    selection = select_structured_output_strategy(
        _spec(),
        allowed_strategies=("strict_tool", "native_schema"),
        supports_native_schema=True,
        supports_tool_fallback=True,
        provider="test",
        model="model-a",
    )

    assert selection is not None
    assert selection.strategy == "native_schema"


def test_strict_strategy_can_use_strict_tool_when_native_is_unavailable() -> None:
    selection = select_structured_output_strategy(
        _spec(),
        allowed_strategies=("strict_tool",),
        supports_native_schema=False,
        supports_tool_fallback=True,
        provider="test",
        model="model-a",
    )

    assert selection is not None
    assert selection.strategy == "strict_tool"


def test_strict_strategy_never_falls_back_to_prompt_parse() -> None:
    with pytest.raises(LLMCapabilityNotSupportedError, match="strict structured output"):
        select_structured_output_strategy(
            _spec(strict=True),
            allowed_strategies=("prompt_parse",),
            supports_native_schema=False,
            supports_tool_fallback=False,
            fallback_policy=StructuredOutputFallbackPolicy(allow_prompt_parse=True),
            provider="test",
            model="model-a",
        )


def test_strict_prompt_parse_requires_explicit_strict_policy() -> None:
    selection = select_structured_output_strategy(
        _spec(strict=True),
        allowed_strategies=("prompt_parse",),
        supports_native_schema=False,
        supports_tool_fallback=False,
        fallback_policy=StructuredOutputFallbackPolicy(
            allow_prompt_parse=True,
            allow_strict_prompt_parse=True,
        ),
        provider="test",
        model="model-a",
    )

    assert selection is not None
    assert selection.strategy == "prompt_parse"


def test_non_strict_prompt_parse_requires_explicit_policy() -> None:
    with pytest.raises(LLMCapabilityNotSupportedError, match="non-strict structured output"):
        select_structured_output_strategy(
            _spec(strict=False),
            allowed_strategies=("prompt_parse",),
            supports_native_schema=False,
            supports_tool_fallback=False,
            provider="test",
            model="model-a",
        )

    selection = select_structured_output_strategy(
        _spec(strict=False),
        allowed_strategies=("prompt_parse",),
        supports_native_schema=False,
        supports_tool_fallback=False,
        fallback_policy=StructuredOutputFallbackPolicy(allow_prompt_parse=True),
        provider="test",
        model="model-a",
    )

    assert selection is not None
    assert selection.strategy == "prompt_parse"
