"""Anthropic tool-schema translation for provider-neutral tool contracts.

This module converts ``FunctionToolSpec`` and ``ToolChoice`` values into the
Messages API payload fields. It does not call the Anthropic SDK or orchestrate
planner behavior.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...contracts.tool_contracts import (
    FunctionToolSpec,
    ToolChoice,
    function_tool_spec_from_openai_dict,
    normalize_tool_choice_mode,
)
from ...core.base import ToolSpecInput
from ...core.exceptions import LLMConfigurationError
from ...core.identity import ANTHROPIC_PROVIDER_ID


def normalize_anthropic_tool_spec(tool: ToolSpecInput) -> dict[str, Any]:
    """Convert a neutral or legacy tool spec to Anthropic's schema."""
    if isinstance(tool, FunctionToolSpec):
        spec = tool
    elif isinstance(tool, Mapping):
        spec = function_tool_spec_from_openai_dict(tool)
    else:
        raise LLMConfigurationError(
            "tools must be FunctionToolSpec values or legacy function dictionaries",
            provider=ANTHROPIC_PROVIDER_ID,
        )
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.parameters_schema,
    }


def coerce_tool_choice(tool_choice: Any) -> ToolChoice:
    """Return a provider-neutral ``ToolChoice`` from compatibility inputs."""
    if isinstance(tool_choice, ToolChoice):
        return tool_choice
    elif isinstance(tool_choice, Mapping):
        mode = str(tool_choice.get("mode") or tool_choice.get("type") or "auto")
        function_name = tool_choice.get("function_name") or tool_choice.get("name")
    elif isinstance(tool_choice, str):
        mode = tool_choice
        function_name = None
    elif tool_choice is None:
        mode = "auto"
        function_name = None
    else:
        raise LLMConfigurationError(
            "Unsupported Anthropic tool_choice value",
            provider=ANTHROPIC_PROVIDER_ID,
        )

    try:
        normalized = normalize_tool_choice_mode(str(mode))
        return ToolChoice(
            normalized,
            function_name=str(function_name) if function_name else None,
        )
    except (TypeError, ValueError) as exc:
        raise LLMConfigurationError(
            f"Unsupported Anthropic tool_choice mode '{mode}'",
            provider=ANTHROPIC_PROVIDER_ID,
        ) from exc


def normalize_anthropic_tool_choice(tool_choice: Any) -> dict[str, Any]:
    """Convert provider-neutral tool choice to Anthropic's tool_choice."""
    choice = coerce_tool_choice(tool_choice)
    if choice.mode == "required":
        return {"type": "any"}
    if choice.mode == "specific":
        return {"type": "tool", "name": str(choice.function_name)}
    if choice.mode in {"auto", "none"}:
        return {"type": choice.mode}
    raise LLMConfigurationError(
        f"Unsupported Anthropic tool_choice mode '{choice.mode}'",
        provider=ANTHROPIC_PROVIDER_ID,
    )


__all__ = [
    "coerce_tool_choice",
    "normalize_anthropic_tool_choice",
    "normalize_anthropic_tool_spec",
]
