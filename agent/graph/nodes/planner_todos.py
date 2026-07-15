"""Shared planner todo normalization helpers.

This module owns small deterministic conversions for planner and plan-review
todo payloads. It does not mutate graph state or emit events.
"""

from __future__ import annotations

from typing import Any, List

from ..state import TodoItem


def normalize_todo_texts(todo_list: List[Any] | None) -> List[str]:
    """Normalize todo inputs to text strings for planner payloads."""
    if not todo_list:
        return []

    normalized: List[str] = []
    for item in todo_list:
        if isinstance(item, TodoItem):
            normalized.append(item.description)
        elif isinstance(item, dict) and "text" in item:
            normalized.append(str(item["text"]))
        else:
            normalized.append(str(item))
    return normalized


__all__ = ["normalize_todo_texts"]
