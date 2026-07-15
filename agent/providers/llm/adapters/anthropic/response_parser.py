"""Anthropic Messages response normalization helpers.

This module extracts provider-neutral text and tool-call results from
Anthropic response content blocks. It intentionally contains no SDK calls.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from ...core.base import ToolCall
from ...core.exceptions import (
    LLMRefusalError,
    LLMRefusalOutcome,
    LLMResponseError,
)
from ...core.identity import ANTHROPIC_PROVIDER_ID


def extract_anthropic_text(
    response: Any,
    *,
    allow_empty: bool = False,
    allowed_block_types: set[str] | None = None,
) -> str:
    """Extract concatenated text blocks from an Anthropic response."""
    allowed = (allowed_block_types or {"text"}) | {
        "thinking",
        "redacted_thinking",
    }
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(str(getattr(block, "text", "") or ""))
        elif block_type not in allowed:
            raise LLMResponseError(
                f"Anthropic response contained unsupported content block type '{block_type}'",
                provider=ANTHROPIC_PROVIDER_ID,
            )
    content = "".join(parts)
    if not allow_empty and not content.strip():
        raise LLMResponseError(
            "Anthropic response contained no text blocks",
            provider=ANTHROPIC_PROVIDER_ID,
        )
    return content


def raise_for_anthropic_refusal(
    response: Any,
    *,
    model: str,
    usage: Any = None,
    partial_content: str | None = None,
) -> None:
    """Raise a provider-neutral refusal outcome for HTTP-200 refusals."""
    if str(getattr(response, "stop_reason", "") or "") != "refusal":
        return

    details = _stop_details_mapping(getattr(response, "stop_details", None))
    category = str(details.get("category") or "").strip() or None
    explanation = str(details.get("explanation") or "").strip() or None
    message = "Anthropic declined the request"
    if category:
        message = f"{message} due to the '{category}' safety classifier"
    raise LLMRefusalError(
        message,
        outcome=LLMRefusalOutcome(
            provider=ANTHROPIC_PROVIDER_ID,
            model=model,
            category=category,
            explanation=explanation,
            response_id=str(getattr(response, "id", "") or "").strip() or None,
            usage=usage,
            partial_content=partial_content or None,
        ),
        stop_details=details,
    )


def _stop_details_mapping(value: Any) -> dict[str, object]:
    """Normalize SDK or mapping refusal details without provider coupling."""
    if isinstance(value, Mapping):
        return {str(key): detail for key, detail in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return {str(key): detail for key, detail in dumped.items()}
    if value is None:
        return {}
    return {
        key: detail
        for key in ("type", "category", "explanation")
        if (detail := getattr(value, key, None)) is not None
    }


def extract_anthropic_tool_calls(response: Any) -> list[ToolCall]:
    """Extract normalized tool calls from Anthropic tool_use blocks."""
    tool_calls: list[ToolCall] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) != "tool_use":
            continue
        tool_input = getattr(block, "input", {}) or {}
        tool_calls.append(
            ToolCall(
                id=str(getattr(block, "id", "")),
                name=str(getattr(block, "name", "")),
                arguments=json.dumps(tool_input, separators=(",", ":")),
            )
        )
    return tool_calls


__all__ = [
    "extract_anthropic_text",
    "extract_anthropic_tool_calls",
    "raise_for_anthropic_refusal",
]
