"""Base prompt builder abstractions.

This module defines the stable interface (`ChatPromptBuilder`) used by LangGraph
nodes to construct prompts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from .schemas import PromptContext, ToolResultContext


class ToolResult(Protocol):  # pragma: no cover - structural placeholder
    """Protocol describing the subset of tool result data needed for prompts."""

    def __getitem__(self, item: str) -> object:
        ...


class ChatPromptBuilder(ABC):
    """Defines the interface used by LangGraph nodes to construct prompts."""

    @abstractmethod
    def build_system_prompt(self, state: PromptContext) -> str:
        """Return the system prompt for the current turn."""

    @abstractmethod
    def build_decision_prompt(self, state: PromptContext) -> str:
        """Return the decision prompt used by orchestrator nodes."""

    @abstractmethod
    def build_tool_summary_prompt(self, tool_result: ToolResultContext) -> str:
        """Summarise tool output for follow-up reasoning prompts."""


__all__ = ["ChatPromptBuilder", "ToolResult"]

