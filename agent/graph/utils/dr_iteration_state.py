"""Deep-reasoning iteration metadata helpers.

This module owns the mutable metadata structures used to track deep-reasoning
iteration state and per-iteration records. It preserves the legacy mutation
semantics currently relied on by streaming compatibility helpers and DR nodes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from agent.graph.state import InteractiveState


def _dr_iteration_metadata(interactive: "InteractiveState") -> Dict[str, Any]:
    metadata = interactive.facts.metadata_copy()
    interactive.facts.metadata = metadata
    return metadata.setdefault("dr_iteration_meta", {})


def _advance_dr_iteration(dr_meta: Dict[str, Any]) -> int:
    current = int(dr_meta.get("counter") or 0) + 1
    dr_meta["counter"] = current
    dr_meta["active_iteration"] = current
    return current


def _ensure_dr_iteration(dr_meta: Dict[str, Any]) -> int:
    if isinstance(dr_meta.get("active_iteration"), int):
        return int(dr_meta["active_iteration"])
    return _advance_dr_iteration(dr_meta)


def clear_dr_active_iteration(interactive: "InteractiveState") -> None:
    metadata = interactive.facts.metadata_copy()
    interactive.facts.metadata = metadata
    dr_meta = metadata.setdefault("dr_iteration_meta", {})
    dr_meta.pop("active_iteration", None)
    metadata["dr_iteration_meta"] = dr_meta


def _dr_iteration_records(metadata: Dict[str, Any]) -> Dict[str, Any]:
    return metadata.setdefault("dr_iteration_records", {})


def record_dr_reasoning_snippet(interactive: "InteractiveState", iteration: int, content: str) -> None:
    metadata = interactive.facts.metadata_copy()
    interactive.facts.metadata = metadata
    records = _dr_iteration_records(metadata)
    entry = records.setdefault(str(iteration), {})
    reasoning_entries = entry.setdefault("reasoning", [])
    snippet = content.strip()
    if snippet:
        reasoning_entries.append(snippet)
    metadata["dr_iteration_records"] = records


def record_dr_tool_execution(
    interactive: "InteractiveState",
    iteration: int,
    *,
    tool: Optional[str],
    status: Optional[str],
    command: Optional[str] = None,
    summary: Optional[str] = None,
) -> None:
    metadata = interactive.facts.metadata_copy()
    interactive.facts.metadata = metadata
    records = _dr_iteration_records(metadata)
    entry = records.setdefault(str(iteration), {})
    entry["tool"] = {
        "tool": tool,
        "status": status,
        "command": command,
        "summary": summary,
    }
    metadata["dr_iteration_records"] = records


def record_dr_observation(interactive: "InteractiveState", iteration: int, observation: str) -> None:
    metadata = interactive.facts.metadata_copy()
    interactive.facts.metadata = metadata
    records = _dr_iteration_records(metadata)
    entry = records.setdefault(str(iteration), {})
    entry["observation"] = observation
    metadata["dr_iteration_records"] = records


__all__ = [
    "_advance_dr_iteration",
    "_dr_iteration_metadata",
    "_dr_iteration_records",
    "_ensure_dr_iteration",
    "clear_dr_active_iteration",
    "record_dr_observation",
    "record_dr_reasoning_snippet",
    "record_dr_tool_execution",
]
