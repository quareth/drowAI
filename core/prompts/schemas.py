"""Shared type contracts for prompt infrastructure.

This module contains minimal, reusable type definitions used across
`core.prompts` to keep contracts explicit and centralized.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, TypeAlias


PromptContext: TypeAlias = Mapping[str, object]
ToolResultContext: TypeAlias = Mapping[str, object]

TemplateId: TypeAlias = str

BuilderFactory: TypeAlias = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class TemplateRef:
    """Mapping target for a stable template ID."""

    family: str
    filename: str


__all__ = [
    "PromptContext",
    "ToolResultContext",
    "TemplateId",
    "TemplateRef",
    "BuilderFactory",
]
