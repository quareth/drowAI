"""Sentinel contract for unavailable planner tool capabilities.

This module owns the small internal marker used when the tool selector
determines that no exposed tool, and no reasonable substitute, can satisfy the
current tool intent. It is not an executable tool id.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


UNAVAILABLE_CAPABILITY_TOOL = "unavailable_capability"
UNAVAILABLE_CAPABILITY_METADATA_KEY = "tool_selection_unavailable_capability"


def is_unavailable_capability_tool(tool_id: Any) -> bool:
    """Return True when ``tool_id`` is the unavailable-capability sentinel."""
    return str(tool_id or "").strip().lower() == UNAVAILABLE_CAPABILITY_TOOL


def selection_is_unavailable_capability(selected_tools: Sequence[Any] | None) -> bool:
    """Return True when the selected-tool list is exactly the sentinel."""
    if not isinstance(selected_tools, Sequence) or isinstance(selected_tools, (str, bytes)):
        return False
    return len(selected_tools) == 1 and is_unavailable_capability_tool(selected_tools[0])


def plan_is_unavailable_capability(plan: Any) -> bool:
    """Return True when an ActionPlan-like object carries the sentinel result."""
    return selection_is_unavailable_capability(getattr(plan, "selected_tools", None))


def metadata_has_unavailable_capability(metadata: Mapping[str, Any] | None) -> bool:
    """Return True when runtime metadata marks selector capability unavailability."""
    if not isinstance(metadata, Mapping):
        return False
    marker = metadata.get(UNAVAILABLE_CAPABILITY_METADATA_KEY)
    return isinstance(marker, Mapping) and bool(marker.get("active", False))


__all__ = [
    "UNAVAILABLE_CAPABILITY_METADATA_KEY",
    "UNAVAILABLE_CAPABILITY_TOOL",
    "is_unavailable_capability_tool",
    "metadata_has_unavailable_capability",
    "plan_is_unavailable_capability",
    "selection_is_unavailable_capability",
]
