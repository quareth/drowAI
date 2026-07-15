"""Canonical LangGraph checkpoint thread identity helpers.

This module owns the formatted thread ids used for LangGraph checkpoint
storage. It intentionally contains small, side-effect-free helpers so services
do not duplicate graph or legacy task thread string construction.
"""

from __future__ import annotations

import re
import uuid
from typing import Iterable

_GRAPH_THREAD_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def generate_graph_thread_id() -> str:
    """Return a new non-reusable task graph identity."""
    return uuid.uuid4().hex


def normalize_graph_thread_id(value: object) -> str | None:
    """Return a validated graph identity, or ``None`` when invalid."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if not _GRAPH_THREAD_ID_PATTERN.fullmatch(cleaned):
        return None
    return cleaned


def require_graph_thread_id(value: object, *, task_id: int) -> str:
    """Return a valid graph identity or fail closed for SaaS runtime paths."""
    normalized = normalize_graph_thread_id(value)
    if normalized is None:
        raise RuntimeError(f"Task {int(task_id)} is missing a valid graph_thread_id")
    return normalized


def format_graph_thread_id(graph_thread_id: object, *, task_id: int) -> str:
    """Return the LangGraph checkpoint thread id for a task graph identity."""
    return f"graph-{require_graph_thread_id(graph_thread_id, task_id=task_id)}"


def legacy_task_thread_id(task_id: int) -> str:
    """Return the legacy checkpoint thread id used before graph_thread_id."""
    return f"task-{int(task_id)}"


def owned_checkpoint_thread_ids(
    *,
    task_id: int,
    graph_thread_id: object,
) -> tuple[str, ...]:
    """Return current and legacy checkpoint thread ids owned by one task."""
    normalized_graph_thread_id = normalize_graph_thread_id(graph_thread_id)
    candidates: Iterable[str | None] = (
        (
            format_graph_thread_id(normalized_graph_thread_id, task_id=task_id)
            if normalized_graph_thread_id is not None
            else None
        ),
        legacy_task_thread_id(task_id),
    )
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


__all__ = [
    "format_graph_thread_id",
    "generate_graph_thread_id",
    "legacy_task_thread_id",
    "normalize_graph_thread_id",
    "owned_checkpoint_thread_ids",
    "require_graph_thread_id",
]
