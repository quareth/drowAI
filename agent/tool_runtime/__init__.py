"""Runtime helpers for LangGraph tool execution integration."""

from __future__ import annotations

from typing import Any

__all__ = [
    "ToolCatalogEntry",
    "ToolExecutionCoordinator",
    "ToolExecutionRequest",
    "ToolExecutionOutcome",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from .coordinator import (
            ToolCatalogEntry,
            ToolExecutionCoordinator,
            ToolExecutionRequest,
            ToolExecutionOutcome,
        )

        exported = {
            "ToolCatalogEntry": ToolCatalogEntry,
            "ToolExecutionCoordinator": ToolExecutionCoordinator,
            "ToolExecutionRequest": ToolExecutionRequest,
            "ToolExecutionOutcome": ToolExecutionOutcome,
        }
        return exported[name]
    raise AttributeError(name)
