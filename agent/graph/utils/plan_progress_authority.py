"""Central authority for todo progression transitions and status mappings.

This module owns deterministic todo status transitions used by planning,
post-tool progression, and event/prompt translation layers. It intentionally
reuses existing `TodoItem` and `TodoStatus` models from `agent.graph.state`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, List, Mapping, MutableSequence, Sequence, Set, cast

from agent.graph.state import CompletionType, TodoItem, TodoStatus

logger = logging.getLogger(__name__)


_TERMINAL_STATUSES = {
    TodoStatus.COMPLETE_POSITIVE,
    TodoStatus.COMPLETE_NEGATIVE,
    TodoStatus.EXHAUSTED,
}


def ensure_initial_in_progress(todo_list: MutableSequence[TodoItem]) -> bool:
    """Ensure one actionable todo is active.

    Marks the first pending todo as `in_progress` when no todo is currently in
    progress. Returns True only when the list is mutated.
    """
    if not todo_list:
        return False

    has_active = any(todo.status == TodoStatus.IN_PROGRESS for todo in todo_list)
    if has_active:
        return False

    for todo in todo_list:
        if todo.status == TodoStatus.PENDING:
            todo.status = TodoStatus.IN_PROGRESS
            if todo.started_at is None:
                todo.started_at = datetime.now(timezone.utc)
            return True

    return False


def resolve_active_todo(
    todo_list: Sequence[Any] | None,
) -> dict[str, Any] | None:
    """Return a compact descriptor of the single in-progress todo, or None.

    Authority: this is the sole "current in-progress todo" accessor used by
    the context bundle to surface the active plan step to tool-selection
    layers (category selector, planner/tool-plan preparation, articulation).
    It is deliberately shallow — only ``index`` and ``description`` are
    returned, never full ``TodoItem`` data — to keep prompt-bound payloads
    minimal and matched to the "current step only" authority scope.

    Tolerates both real shapes ``facts.todo_list`` can carry:
    - ``list[TodoItem]``: returns the first item whose status is
      ``IN_PROGRESS`` (matching the invariant maintained by
      :func:`ensure_initial_in_progress`).
    - ``list[str]`` (legacy): returns ``None`` — the string form carries
      no status signal, so there is no authoritative "active" todo.

    Returns ``None`` for empty lists and when no todo is IN_PROGRESS.
    """
    if not todo_list:
        return None

    first = todo_list[0]
    if not isinstance(first, TodoItem):
        # Legacy ``list[str]`` carries no progression — nothing to surface.
        return None

    for index, todo in enumerate(todo_list):
        if not isinstance(todo, TodoItem):
            continue
        if todo.status != TodoStatus.IN_PROGRESS:
            continue
        description = (todo.description or "").strip()
        if not description:
            return None
        return {"index": index, "description": description}

    return None


def apply_llm_updates(
    todo_list: MutableSequence[TodoItem],
    todo_progress: Iterable[object],
) -> Set[int]:
    """Apply one or many LLM progression updates to todos.

    Supports model objects and mapping-style payloads. Invalid indices/statuses
    are ignored with warning logs. Returns the set of changed todo indices.
    """
    changed_indices: Set[int] = set()
    if not todo_list:
        return changed_indices

    for raw_update in todo_progress:
        normalized = _normalize_progress_update(raw_update)
        if normalized is None:
            continue

        index = normalized["index"]
        if index < 0 or index >= len(todo_list):
            logger.warning(
                "Ignoring invalid todo index %s (len=%s)", index, len(todo_list)
            )
            continue

        todo = todo_list[index]
        if todo.status in _TERMINAL_STATUSES:
            logger.debug("Ignoring update for terminal todo index %s", index)
            continue

        before = _snapshot_todo(todo)
        _apply_single_update(todo, normalized)
        after = _snapshot_todo(todo)
        if before != after:
            changed_indices.add(index)

    return changed_indices


def build_todo_stream_updates(
    before: Sequence[TodoItem],
    after: Sequence[TodoItem],
    todo_id_map: Sequence[str] | None = None,
) -> List[dict[str, Any]]:
    """Build changed-only stream updates by comparing before/after snapshots.

    Only indices in ``range(min(len(before), len(after)))`` are considered.
    """
    updates: List[dict[str, Any]] = []
    max_len = min(len(before), len(after))
    ids = list(todo_id_map or [])

    for idx in range(max_len):
        previous = before[idx]
        current = after[idx]
        if _snapshot_todo(previous) == _snapshot_todo(current):
            continue

        updates.append(
            {
                "id": ids[idx] if idx < len(ids) else str(idx),
                "text": current.description,
                "status": to_stream_status(current),
                "index": idx,
            }
        )

    return updates


def to_prompt_status(todo: TodoItem) -> str:
    """Map canonical todo status to prompt-friendly marker."""
    if todo.status == TodoStatus.PENDING:
        return "pending"
    if todo.status == TodoStatus.IN_PROGRESS:
        return "in_progress"
    if todo.status == TodoStatus.COMPLETE_POSITIVE:
        return "completed"
    if todo.status == TodoStatus.COMPLETE_NEGATIVE:
        if _is_skipped_completion(todo):
            return "skipped"
        return "completed"
    if todo.status == TodoStatus.EXHAUSTED:
        return "skipped"
    return "pending"


def to_stream_status(todo: TodoItem) -> str:
    """Map canonical todo status to stream contract status."""
    return to_prompt_status(todo)


def _normalize_progress_update(raw_update: object) -> dict[str, Any] | None:
    """Normalize LLM todo update object or mapping into a dict."""
    if isinstance(raw_update, Mapping):
        payload = raw_update
    else:
        payload = cast(Mapping[str, Any], _as_mapping_from_object(raw_update))

    try:
        index = int(payload.get("index"))
    except (TypeError, ValueError):
        logger.warning("Ignoring todo update without valid index: %s", raw_update)
        return None

    status = str(payload.get("status") or "").strip().lower()
    if status not in {"pending", "in_progress", "completed", "skipped"}:
        logger.warning("Ignoring todo update with invalid status: %s", status)
        return None

    completion_type = payload.get("completion_type")
    completion_reason = payload.get("completion_reason")
    return {
        "index": index,
        "status": status,
        "completion_type": completion_type,
        "completion_reason": completion_reason,
    }


def _as_mapping_from_object(raw_update: object) -> dict[str, Any]:
    """Extract update-like fields from model/object payloads."""
    return {
        "index": getattr(raw_update, "index", None),
        "status": getattr(raw_update, "status", None),
        "completion_type": getattr(raw_update, "completion_type", None),
        "completion_reason": getattr(raw_update, "completion_reason", None),
    }


def _apply_single_update(todo: TodoItem, update: Mapping[str, Any]) -> None:
    """Apply one normalized todo progression update in-place."""
    status = cast(str, update["status"])
    now = datetime.now(timezone.utc)

    if status == "completed":
        completion_type = str(update.get("completion_type") or "positive").lower()
        completion_reason = str(
            update.get("completion_reason") or "Objective resolved per LLM assessment"
        )
        if completion_type == "negative":
            todo.status = TodoStatus.COMPLETE_NEGATIVE
            todo.completion_type = CompletionType.NEGATIVE
        else:
            todo.status = TodoStatus.COMPLETE_POSITIVE
            todo.completion_type = CompletionType.POSITIVE
        todo.completion_reasoning = completion_reason
        todo.completed_at = now
        return

    if status == "skipped":
        completion_reason = str(update.get("completion_reason") or "Skipped per LLM assessment")
        todo.status = TodoStatus.COMPLETE_NEGATIVE
        todo.completion_type = CompletionType.NEGATIVE
        todo.completion_reasoning = _normalize_skipped_reason(completion_reason)
        todo.completed_at = now
        return

    if status == "in_progress":
        todo.status = TodoStatus.IN_PROGRESS
        if todo.started_at is None:
            todo.started_at = now
        return

    if status == "pending":
        todo.status = TodoStatus.PENDING
        return


def _snapshot_todo(todo: TodoItem) -> tuple[Any, ...]:
    """Return a compact tuple used for change detection."""
    return (
        todo.status,
        todo.started_at,
        todo.completed_at,
        todo.completion_type,
        todo.completion_reasoning,
    )


def _is_skipped_completion(todo: TodoItem) -> bool:
    """Infer whether a negative completion should be surfaced as skipped."""
    if todo.status != TodoStatus.COMPLETE_NEGATIVE:
        return False
    reason = (todo.completion_reasoning or "").strip().lower()
    return reason.startswith("skipped")


def _normalize_skipped_reason(reason: str) -> str:
    """Persist skipped reasons with a stable prefix for deterministic mapping."""
    normalized = reason.strip()
    if not normalized:
        return "Skipped: per LLM assessment"
    if normalized.lower().startswith("skipped"):
        return normalized
    return f"Skipped: {normalized}"


__all__ = [
    "ensure_initial_in_progress",
    "resolve_active_todo",
    "apply_llm_updates",
    "build_todo_stream_updates",
    "to_prompt_status",
    "to_stream_status",
]
