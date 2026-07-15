"""Read-only helpers for serialized planner ToolBatch manifests.

The active runtime stores the canonical batch manifest under
``planner_plan.tool_batch``. These helpers expose that serialized shape to
prompt/context readers without falling back to old single-tool planner fields.
They do not validate or mutate execution data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True, slots=True)
class SerializedToolCallView:
    """A read-only view of one serialized tool call in planner metadata."""

    tool_call_id: str
    tool_id: str
    parameters: Mapping[str, Any]
    intent: str = ""


def serialized_tool_calls_from_plan(plan: Mapping[str, Any]) -> tuple[SerializedToolCallView, ...]:
    """Return serialized ToolBatch calls from a planner_plan mapping."""
    raw_batch = plan.get("tool_batch") if isinstance(plan, Mapping) else None
    if not isinstance(raw_batch, Mapping):
        return ()
    raw_calls = raw_batch.get("tool_calls")
    if not isinstance(raw_calls, list):
        return ()

    calls: list[SerializedToolCallView] = []
    for entry in raw_calls:
        if not isinstance(entry, Mapping):
            continue
        tool_id = str(entry.get("tool_id") or "").strip()
        if not tool_id:
            continue
        tool_call_id = str(entry.get("tool_call_id") or "").strip()
        raw_parameters = entry.get("parameters")
        calls.append(
            SerializedToolCallView(
                tool_call_id=tool_call_id,
                tool_id=tool_id,
                parameters=dict(raw_parameters) if isinstance(raw_parameters, Mapping) else {},
                intent=str(entry.get("intent") or ""),
            )
        )
    return tuple(calls)


def serialized_tool_calls_from_metadata(
    metadata: Mapping[str, Any],
) -> tuple[SerializedToolCallView, ...]:
    """Return serialized ToolBatch calls from runtime metadata."""
    plan = metadata.get("planner_plan") if isinstance(metadata, Mapping) else None
    if not isinstance(plan, Mapping):
        return ()
    return serialized_tool_calls_from_plan(plan)


def primary_tool_call_from_metadata(metadata: Mapping[str, Any]) -> Optional[SerializedToolCallView]:
    """Return the first serialized ToolBatch call from runtime metadata."""
    calls = serialized_tool_calls_from_metadata(metadata)
    return calls[0] if calls else None


__all__ = [
    "SerializedToolCallView",
    "primary_tool_call_from_metadata",
    "serialized_tool_calls_from_metadata",
    "serialized_tool_calls_from_plan",
]
