"""Active-todo stall guard for post-tool reasoning.

This module tracks consecutive no-progress tool phases for the current
in-progress todo without taking over todo completion authority from PTR.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any, TYPE_CHECKING

from agent.graph.state import TodoItem
from agent.graph.utils.plan_progress_authority import resolve_active_todo

if TYPE_CHECKING:
    from agent.graph.nodes.post_tool_reasoning.models import PostToolReasoningOutput
    from agent.graph.state import InteractiveState

logger = logging.getLogger(__name__)

TODO_STALL_METADATA_KEY = "active_todo_stall_guard"
TODO_STALL_THRESHOLD = 3
_OVERRIDE_REASON = "Override: active todo stalled without progress"
_POST_REFLECT_OVERRIDE_REASON = "Override: active todo still stalled after reflection"


def render_todo_stall_prompt_section(metadata: Mapping[str, Any]) -> str:
    """Render compact prompt guidance for an active todo stall, if present."""

    tracking = _tracking(metadata)
    if not tracking:
        return ""

    count = _int_value(tracking.get("count"))
    if count <= 0:
        return ""

    threshold = _int_value(tracking.get("threshold")) or TODO_STALL_THRESHOLD
    index = tracking.get("index")
    description = str(tracking.get("description") or "active todo").strip()
    prefix = (
        f"Active todo [{index}] `{description}` has {count} consecutive "
        f"no-progress tool phase"
    )
    if count != 1:
        prefix += "s"
    prefix += "."

    if tracking.get("post_reflect_awaiting_progress"):
        guidance = (
            "A reflection was already attempted for this active todo. If this "
            "phase still has no progress, do not choose another tool; synthesize "
            "or finalize with the current blocker."
        )
    elif count >= max(1, threshold - 1):
        guidance = (
            "Prefer reflect or finalize unless current evidence proves a materially "
            "different approach is justified."
        )
    else:
        guidance = (
            "Continue only if the next tool has a materially different path to "
            "resolving this todo."
        )

    return f"{prefix}\n{guidance}"


def apply_active_todo_stall_guard(
    interactive: "InteractiveState",
    output: "PostToolReasoningOutput",
    *,
    todo_updates: Sequence[Mapping[str, Any]] | None = None,
    threshold: int = TODO_STALL_THRESHOLD,
) -> bool:
    """Update active-todo stall tracking and coerce repeated stalls.

    Returns True only when a ``call_tool`` decision is overridden.
    """

    metadata = interactive.facts.ensure_metadata()
    active = resolve_active_todo(interactive.facts.safe_todo_list)

    if not active:
        _clear_tracking(metadata)
        return False

    active_index = int(active["index"])

    if _has_meaningful_todo_progress(todo_updates, active_index):
        _clear_tracking(metadata)
        return False

    if output.next_action != "call_tool":
        if output.next_action == "reflect" and _is_tracked_active_todo(metadata, active):
            _mark_awaiting_post_reflect_progress(metadata)
            return False
        _clear_tracking(metadata)
        return False

    previous = _tracking(metadata)
    if previous and _same_active_todo(previous, active) and previous.get(
        "post_reflect_awaiting_progress"
    ):
        _force_synthesis(output)
        metadata[TODO_STALL_METADATA_KEY] = {
            **dict(previous),
            "index": int(active["index"]),
            "description": str(active["description"]),
            "last_reason": "call_tool_without_progress_after_reflect",
            "forced_action": "synthesis",
            "post_reflect_awaiting_progress": False,
        }
        return True

    if previous and _same_active_todo(previous, active):
        count = _int_value(previous.get("count")) + 1
    else:
        count = 1

    metadata[TODO_STALL_METADATA_KEY] = {
        "index": active_index,
        "description": str(active["description"]),
        "count": count,
        "threshold": int(threshold),
        "attempts": _active_todo_attempts(
            interactive.facts.safe_todo_list,
            active_index,
        ),
        "last_reason": "call_tool_without_todo_progress",
    }

    if count < threshold:
        return False

    logger.info(
        "[TODO_STALL] Forcing reflect for active todo %s after %s no-progress phases",
        active_index,
        count,
    )
    output.next_action = "reflect"
    output.retry_suggested = False
    output.tool_intent = None
    output.action_reasoning = f"({_OVERRIDE_REASON}) {output.action_reasoning}"
    metadata[TODO_STALL_METADATA_KEY]["forced_action"] = "reflect"
    metadata[TODO_STALL_METADATA_KEY]["post_reflect_awaiting_progress"] = True
    return True


def _has_meaningful_todo_progress(
    todo_updates: Sequence[Mapping[str, Any]] | None,
    active_index: int,
) -> bool:
    """Return True when PTR produced real deltas for the active todo."""

    if not todo_updates:
        return False
    return any(
        _todo_update_matches_index(update, active_index) for update in todo_updates
    )


def _todo_update_matches_index(update: Mapping[str, Any], active_index: int) -> bool:
    try:
        return int(update.get("index")) == active_index
    except (TypeError, ValueError):
        return False


def _tracking(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = metadata.get(TODO_STALL_METADATA_KEY)
    if isinstance(raw, Mapping):
        return raw
    return None


def _clear_tracking(metadata: MutableMapping[str, Any]) -> None:
    metadata.pop(TODO_STALL_METADATA_KEY, None)


def _is_tracked_active_todo(
    metadata: Mapping[str, Any],
    active: Mapping[str, Any],
) -> bool:
    previous = _tracking(metadata)
    return bool(previous and _same_active_todo(previous, active))


def _mark_awaiting_post_reflect_progress(metadata: MutableMapping[str, Any]) -> None:
    previous = _tracking(metadata)
    if not previous:
        return
    metadata[TODO_STALL_METADATA_KEY] = {
        **dict(previous),
        "forced_action": "reflect",
        "post_reflect_awaiting_progress": True,
    }


def _force_synthesis(output: "PostToolReasoningOutput") -> None:
    logger.info("[TODO_STALL] Forcing synthesis after post-reflect no-progress phase")
    output.next_action = "synthesis"  # type: ignore[assignment]
    output.retry_suggested = False
    output.tool_intent = None
    output.action_reasoning = (
        f"({_POST_REFLECT_OVERRIDE_REASON}) {output.action_reasoning}"
    )


def _same_active_todo(previous: Mapping[str, Any], active: Mapping[str, Any]) -> bool:
    return (
        _int_value(previous.get("index")) == _int_value(active.get("index"))
        and str(previous.get("description") or "").strip()
        == str(active.get("description") or "").strip()
    )


def _active_todo_attempts(todo_list: Sequence[Any], index: int) -> int:
    if index < 0 or index >= len(todo_list):
        return 0
    todo = todo_list[index]
    if isinstance(todo, TodoItem):
        return int(todo.attempts or 0)
    return 0


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "TODO_STALL_METADATA_KEY",
    "TODO_STALL_THRESHOLD",
    "apply_active_todo_stall_guard",
    "render_todo_stall_prompt_section",
]
