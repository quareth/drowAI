"""Provider-neutral function tool contracts for LLM provider adapters.

This module defines the internal tool schema passed across the LLM boundary.
It does not own provider-native request wrappers, registry lookups, or planner
orchestration; adapters translate these contracts to their provider payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Literal, Mapping, cast

ToolChoiceMode = Literal["auto", "none", "required", "specific"]
TOOL_CHOICE_MODES: frozenset[str] = frozenset(("auto", "none", "required", "specific"))


def normalize_tool_choice_mode(mode: str) -> ToolChoiceMode:
    """Normalize and validate a provider-neutral tool-choice mode."""
    if not isinstance(mode, str):
        raise TypeError("mode must be a string")
    normalized = mode.strip().lower()
    if normalized not in TOOL_CHOICE_MODES:
        allowed = ", ".join(sorted(TOOL_CHOICE_MODES))
        raise ValueError(f"mode must be one of: {allowed}")
    return cast(ToolChoiceMode, normalized)


def freeze_tool_choice_modes(modes: Iterable[str]) -> frozenset[str]:
    """Normalize an iterable of tool-choice modes into an immutable set."""
    return frozenset(normalize_tool_choice_mode(mode) for mode in modes)


def _require_non_empty_string(value: str, *, label: str) -> str:
    """Return a stripped string after validating that it contains text."""
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label} cannot be empty")
    return stripped


@dataclass(frozen=True, slots=True)
class FunctionToolSpec:
    """Provider-neutral function tool contract built from registered tools."""

    tool_id: str
    name: str
    description: str
    parameters_schema: Dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tool_id",
            _require_non_empty_string(self.tool_id, label="tool_id"),
        )
        object.__setattr__(
            self,
            "name",
            _require_non_empty_string(self.name, label="name"),
        )
        object.__setattr__(self, "description", str(self.description or ""))
        if not isinstance(self.parameters_schema, dict):
            raise TypeError("parameters_schema must be a dictionary")


@dataclass(frozen=True, slots=True)
class ToolChoice:
    """Provider-neutral tool selection strategy."""

    mode: ToolChoiceMode
    function_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", normalize_tool_choice_mode(self.mode))
        if self.mode == "specific":
            object.__setattr__(
                self,
                "function_name",
                _require_non_empty_string(
                    self.function_name or "",
                    label="function_name",
                ),
            )
        elif self.function_name is not None and not str(self.function_name).strip():
            raise ValueError("function_name cannot be empty")


def function_tool_spec_from_openai_dict(
    tool: Mapping[str, Any],
    *,
    tool_id: str | None = None,
) -> FunctionToolSpec:
    """Convert a legacy OpenAI-style function tool dictionary to a neutral spec.

    This exists only for compatibility at migration boundaries. New builders
    should create ``FunctionToolSpec`` directly.
    """
    if tool.get("type") != "function":
        raise ValueError("legacy tool dictionary must have type='function'")

    function = tool.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
        description = function.get("description", "")
        parameters = function.get("parameters") or {}
    else:
        name = tool.get("name")
        description = tool.get("description", "")
        parameters = tool.get("parameters") or {}

    if not isinstance(parameters, dict):
        raise TypeError("legacy function parameters must be a dictionary")

    normalized_name = _require_non_empty_string(str(name or ""), label="name")
    return FunctionToolSpec(
        tool_id=tool_id or normalized_name,
        name=normalized_name,
        description=str(description or ""),
        parameters_schema=parameters,
    )


__all__ = [
    "FunctionToolSpec",
    "TOOL_CHOICE_MODES",
    "ToolChoice",
    "ToolChoiceMode",
    "freeze_tool_choice_modes",
    "function_tool_spec_from_openai_dict",
    "normalize_tool_choice_mode",
]
