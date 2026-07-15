"""Formatting and coercion helpers for post-tool prompt assembly.

This module keeps PTR-local rendering logic separate from builder
orchestration while preserving current prompt behavior exactly.
"""

from __future__ import annotations

import json
from typing import Any, List, Mapping, Sequence

from core.prompts.builders._todo_formatting import (
    extract_todo_description,
    extract_todo_status,
    to_progress_marker,
)
from core.prompts.constants import (
    POST_TOOL_MAX_PARAM_CHARS,
    POST_TOOL_MAX_SUMMARY_CHARS,
    POST_TOOL_MAX_TODO_CHARS,
    POST_TOOL_MAX_TODOS_IN_PROMPT,
)


MAX_PARAM_CHARS = POST_TOOL_MAX_PARAM_CHARS
MAX_SUMMARY_CHARS = POST_TOOL_MAX_SUMMARY_CHARS
MAX_TODO_CHARS = POST_TOOL_MAX_TODO_CHARS
MAX_TODOS_IN_PROMPT = POST_TOOL_MAX_TODOS_IN_PROMPT


def get_field(source: Any, key: str, default: Any = None) -> Any:
    """Read a key from mappings or an attribute from objects."""
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def as_mapping(value: Any) -> Mapping[str, Any]:
    """Coerce supported values into a mapping view."""
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, Mapping):
        return value_dict
    return {}


def as_sequence(value: Any) -> Sequence[Any]:
    """Coerce supported values into a non-string sequence view."""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return []


def truncate(value: str, limit: int) -> str:
    """Truncate a string to a limit with ellipsis."""
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def format_parameters(params: Mapping[str, Any]) -> str:
    """Format tool parameters for display."""
    if not params:
        return ""

    rendered: List[str] = []
    for key, value in params.items():
        if value in (None, "", [], {}):
            continue
        rendered.append(f"{key}={value}")

    return truncate(", ".join(rendered), MAX_PARAM_CHARS)


def format_sequence(values: Sequence[Any]) -> str:
    """Format a sequence of items as a compact bulleted list."""
    filtered = [str(item) for item in values if item]
    if not filtered:
        return ""
    bullets = ["• " + item for item in filtered[:50]]
    return "\n".join(bullets)


def format_structured_signals(values: Sequence[Any]) -> str:
    """Format compact structured signals without dumping large JSON blobs."""
    lines: List[str] = []
    for item in values[:10]:
        if not isinstance(item, Mapping):
            continue
        rendered = json.dumps(
            {str(key): value for key, value in item.items() if value is not None},
            ensure_ascii=True,
            separators=(",", ": "),
            sort_keys=True,
        )
        lines.append("• " + truncate(rendered, 220))
    return "\n".join(lines)


def format_artifact_refs(refs: Sequence[Any]) -> str:
    """Format compact artifact references as metadata-first hints."""
    if not refs:
        return ""
    lines: List[str] = []
    for item in refs[:5]:
        if not isinstance(item, Mapping):
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        label = str(item.get("label") or "").strip()
        tool_name = str(item.get("tool_name") or "").strip()
        artifact_kind = str(item.get("artifact_kind") or "").strip()
        path = str(item.get("path") or "").strip()
        descriptor = label or f"{artifact_kind or 'artifact'} from {tool_name or 'unknown_tool'}"
        if artifact_id:
            lines.append(f"- {descriptor} (artifact_id={artifact_id})")
        elif path:
            lines.append(f"- {descriptor} (path={path})")
        else:
            lines.append(f"- {descriptor}")
    if not lines:
        return ""
    return "\n".join(lines)


def format_plan(plan: List[str]) -> str:
    """Format plan steps for display."""
    if not plan:
        return ""
    numbered = [f"{i}. {step}" for i, step in enumerate(plan, 1)]
    return "\n".join(numbered)


def format_todos(todo_list: List[Any]) -> str:
    """Format todo list with indices for progress tracking."""
    if not todo_list:
        return ""

    todos: List[str] = []
    for i, item in enumerate(todo_list[:MAX_TODOS_IN_PROMPT]):
        description = extract_todo_description(item)
        if not description:
            continue

        status_str = extract_todo_status(item)
        status_icon = {
            "pending": "☐",
            "in_progress": "▶",
            "completed": "✅",
            "skipped": "⊘",
        }.get(status_str, "☐")

        status_suffix = ""
        if status_str == "in_progress":
            status_suffix = " (in progress)"
        elif status_str == "completed":
            status_suffix = " (done)"
        elif status_str == "skipped":
            status_suffix = " (skipped)"

        marker = to_progress_marker(status_str)
        todos.append(f"[{i}] {status_icon} {description}{status_suffix} {marker}")

    return truncate("\n".join(todos), MAX_TODO_CHARS)


__all__ = [
    "MAX_PARAM_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_TODO_CHARS",
    "MAX_TODOS_IN_PROMPT",
    "as_mapping",
    "as_sequence",
    "format_artifact_refs",
    "format_parameters",
    "format_plan",
    "format_sequence",
    "format_structured_signals",
    "format_todos",
    "get_field",
    "truncate",
]
