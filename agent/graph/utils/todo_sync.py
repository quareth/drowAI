"""Helpers for keeping todo lists aligned with plan steps."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Union

from ..state import TodoItem

_STEP_PREFIX = re.compile(r"^\s*step\s*\d+\s*[:.\-]\s*", re.IGNORECASE)


def normalize_step_text(text: str) -> str:
    """Normalize a plan/todo string for matching across renumbering edits."""
    return _STEP_PREFIX.sub("", text or "").strip()


def sync_todos_with_plan(
    plan_steps: Sequence[str],
    existing_todos: Optional[Iterable[Union[str, TodoItem]]] = None,
) -> List[TodoItem]:
    """Return todo items aligned to plan steps, preserving status when possible."""
    existing_map: dict[str, Union[str, TodoItem]] = {}
    if existing_todos:
        for todo in existing_todos:
            description = todo.description if isinstance(todo, TodoItem) else str(todo)
            key = normalize_step_text(description)
            if key and key not in existing_map:
                existing_map[key] = todo

    synced: List[TodoItem] = []
    for step in plan_steps or []:
        description = str(step)
        key = normalize_step_text(description)
        existing = existing_map.get(key)
        if isinstance(existing, TodoItem):
            updated = existing.model_copy(deep=True)
            updated.description = description
            synced.append(updated)
        else:
            synced.append(TodoItem.from_string(description))

    return synced


def build_todos_from_plan(plan_steps: Sequence[str]) -> List[TodoItem]:
    """Create a fresh todo list from plan steps."""
    return [TodoItem.from_string(str(step)) for step in (plan_steps or [])]
