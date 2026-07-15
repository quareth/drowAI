"""Prompt builder implementations migrated to `core.prompts`.

This package exposes the migrated builder classes, but uses lazy imports to
avoid importing the full LangGraph/agent graph at module import time.

Prefer importing concrete modules (e.g. `core.prompts.builders.simple_tool`) in
performance- or cycle-sensitive paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .deep_reasoning import DeepReasoningPromptBuilder as DeepReasoningPromptBuilder
    from .post_tool import PostToolReasoningPromptBuilder as PostToolReasoningPromptBuilder
    from .simple_tool import SimpleToolPromptBuilder as SimpleToolPromptBuilder
    from .tool_planning import ToolPlanningPromptBuilder as ToolPlanningPromptBuilder


__all__ = [
    "DeepReasoningPromptBuilder",
    "PostToolReasoningPromptBuilder",
    "SimpleToolPromptBuilder",
    "ToolPlanningPromptBuilder",
]


def __getattr__(name: str) -> Any:  # pragma: no cover
    if name == "DeepReasoningPromptBuilder":
        from .deep_reasoning import DeepReasoningPromptBuilder

        return DeepReasoningPromptBuilder
    if name == "PostToolReasoningPromptBuilder":
        from .post_tool import PostToolReasoningPromptBuilder

        return PostToolReasoningPromptBuilder
    if name == "SimpleToolPromptBuilder":
        from .simple_tool import SimpleToolPromptBuilder

        return SimpleToolPromptBuilder
    if name == "ToolPlanningPromptBuilder":
        from .tool_planning import ToolPlanningPromptBuilder

        return ToolPlanningPromptBuilder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
