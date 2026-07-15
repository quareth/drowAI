"""Anthropic structured-output prompt and parse helpers.

This module keeps Anthropic structured output on prompt-and-parse rails.
Anthropic native ``output_config`` rejects several JSON Schema features used by
the app's canonical contracts, so the adapter asks for JSON in the prompt and
delegates validation to the shared provider-neutral parser.
"""

from __future__ import annotations

import json
from typing import Any

from ...contracts.structured_output_strategy import (
    StructuredOutputFallbackPolicy,
    select_structured_output_strategy,
)
from ...contracts.structured_output import (
    StructuredOutputParseError,
    parse_structured_content,
)
from ...core.capabilities import LLMCapability
from ...core.base import ChatMessage, StructuredOutputSpec
from ...core.exceptions import LLMStructuredOutputParseError
from ...core.identity import ANTHROPIC_PROVIDER_ID, ProviderModelRef
from ...profiles import require_model_profile


def require_anthropic_prompt_parse_structured_output_strategy(
    spec: StructuredOutputSpec | None,
    *,
    model: str,
) -> None:
    """Require Anthropic's profiled prompt-parse strategy for one request."""
    if spec is None:
        return
    profile = require_model_profile(ProviderModelRef(ANTHROPIC_PROVIDER_ID, model))
    select_structured_output_strategy(
        spec,
        allowed_strategies=profile.structured_output_strategies,
        supports_native_schema=profile.supports(LLMCapability.STRUCTURED_OUTPUT_NATIVE),
        supports_tool_fallback=profile.supports(LLMCapability.STRUCTURED_OUTPUT_TOOL_FALLBACK),
        fallback_policy=StructuredOutputFallbackPolicy(
            allow_prompt_parse=True,
            allow_strict_prompt_parse=True,
        ),
        provider=ANTHROPIC_PROVIDER_ID,
        model=model,
    )


def apply_anthropic_structured_output_prompt(
    messages: list[ChatMessage],
    spec: StructuredOutputSpec | None,
) -> list[ChatMessage]:
    """Append prompt-only JSON output instructions for Anthropic requests."""
    if spec is None:
        return messages

    updated_messages = [dict(message) for message in messages]
    instruction = _build_structured_output_instruction(spec)
    for index in range(len(updated_messages) - 1, -1, -1):
        if str(updated_messages[index].get("role", "")).strip().lower() != "user":
            continue
        updated_messages[index]["content"] = _append_text_to_content(
            updated_messages[index].get("content"),
            instruction,
        )
        return updated_messages

    return updated_messages


def _build_structured_output_instruction(spec: StructuredOutputSpec) -> str:
    """Build a compact provider prompt for local JSON parsing."""
    schema = json.dumps(spec.schema, separators=(",", ":"), ensure_ascii=False)
    return (
        "\n\nStructured output requirement:\n"
        "Return only valid JSON. Do not wrap the JSON in Markdown or add prose. "
        "The JSON must satisfy this schema after local validation.\n"
        f"Schema name: {spec.name}\n"
        f"Schema: {schema}"
    )


def _append_text_to_content(content: Any, text: str) -> Any:
    """Append prompt text to supported neutral message content shapes."""
    if isinstance(content, str):
        return f"{content}{text}"
    if isinstance(content, list):
        return [*content, {"type": "text", "text": text}]
    if content is None:
        return text
    return f"{content}{text}"


def parse_anthropic_structured_output(
    content: str,
    spec: StructuredOutputSpec | None,
    response: Any,
) -> dict[str, Any] | None:
    """Parse structured output through the shared JSON/schema validator."""
    if spec is None:
        return None
    try:
        return parse_structured_content(content, spec)
    except StructuredOutputParseError as exc:
        raise LLMStructuredOutputParseError(
            str(exc),
            provider=ANTHROPIC_PROVIDER_ID,
            schema_name=spec.name,
            parse_reason=exc.reason,
            raw_content=content,
            diagnostics={"response_id": getattr(response, "id", None)},
        ) from exc


__all__ = [
    "apply_anthropic_structured_output_prompt",
    "parse_anthropic_structured_output",
    "require_anthropic_prompt_parse_structured_output_strategy",
]
