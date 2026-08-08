"""Current-turn operational controls for graph execution continuity.

This module owns bounded runtime state that must survive transient evidence
projection without becoming durable tool output. It does not store command
arguments, stdout, stderr, observations, or long-term memory.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any


CURRENT_TURN_RUNTIME_CONTROLS_KEY = "current_turn_runtime_controls"
ACTIVE_EXECUTION_CONTROL_KEY = "active_execution"


def read_current_turn_runtime_controls(
    metadata: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return controls only when they belong to the active turn."""

    controls = metadata.get(CURRENT_TURN_RUNTIME_CONTROLS_KEY)
    if not isinstance(controls, Mapping):
        return None
    requested_turn = metadata.get("turn_sequence")
    control_turn = controls.get("turn_sequence")
    if (
        isinstance(requested_turn, int)
        and isinstance(control_turn, int)
        and requested_turn != control_turn
    ):
        return None
    return controls


def ensure_current_turn_runtime_controls(
    metadata: MutableMapping[str, Any],
    *,
    turn_sequence: int,
) -> dict[str, Any]:
    """Return a bounded mutable control envelope for ``turn_sequence``."""

    existing = metadata.get(CURRENT_TURN_RUNTIME_CONTROLS_KEY)
    controls: dict[str, Any] = {
        "turn_sequence": turn_sequence,
        "unavailable_tools": [],
    }
    if isinstance(existing, Mapping) and existing.get("turn_sequence") == turn_sequence:
        raw_tools = existing.get("unavailable_tools")
        if isinstance(raw_tools, list):
            controls["unavailable_tools"] = [
                normalized
                for item in raw_tools
                if (normalized := str(item or "").strip())
            ]
        active_execution = existing.get(ACTIVE_EXECUTION_CONTROL_KEY)
        if isinstance(active_execution, Mapping):
            controls[ACTIVE_EXECUTION_CONTROL_KEY] = dict(active_execution)
    metadata[CURRENT_TURN_RUNTIME_CONTROLS_KEY] = controls
    return controls


def read_active_execution_control(
    metadata: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return a validated running-execution control for the active turn."""

    controls = read_current_turn_runtime_controls(metadata)
    if controls is None:
        return None
    active = controls.get(ACTIVE_EXECUTION_CONTROL_KEY)
    if not isinstance(active, Mapping):
        return None
    if str(active.get("process_status") or "").strip().lower() != "running":
        return None
    session_id = str(active.get("session_id") or "").strip()
    continuation_tool_id = str(active.get("continuation_tool_id") or "").strip()
    if not session_id or len(session_id) > 128 or not continuation_tool_id:
        return None
    return active


def set_active_execution_control(
    metadata: MutableMapping[str, Any],
    *,
    turn_sequence: int,
    active_execution: Mapping[str, Any] | None,
) -> None:
    """Set or clear the active execution without retaining output evidence."""

    controls = ensure_current_turn_runtime_controls(
        metadata,
        turn_sequence=turn_sequence,
    )
    if active_execution is None:
        controls.pop(ACTIVE_EXECUTION_CONTROL_KEY, None)
        return
    controls[ACTIVE_EXECUTION_CONTROL_KEY] = {
        "originating_tool_id": str(
            active_execution.get("originating_tool_id") or ""
        ).strip(),
        "continuation_tool_id": str(
            active_execution.get("continuation_tool_id") or ""
        ).strip(),
        "process_status": "running",
        "session_id": str(active_execution.get("session_id") or "").strip(),
        "stdin_available": bool(active_execution.get("stdin_available")),
    }


__all__ = [
    "ACTIVE_EXECUTION_CONTROL_KEY",
    "CURRENT_TURN_RUNTIME_CONTROLS_KEY",
    "ensure_current_turn_runtime_controls",
    "read_active_execution_control",
    "read_current_turn_runtime_controls",
    "set_active_execution_control",
]
