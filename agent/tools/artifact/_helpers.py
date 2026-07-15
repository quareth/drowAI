"""Shared helpers for task-scoped artifact memory tool adapters.

Responsibilities:
- Resolve active runtime task scope from execution context.
- Open/close backend artifact memory service sessions lazily.
- Build concise textual summaries from structured artifact-memory payloads.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from agent.tool_runtime.runtime_context import get_tool_runtime_context


def resolve_active_task_id() -> Optional[int]:
    """Return active runtime task id or ``None`` when execution scope is missing."""
    runtime_context = get_tool_runtime_context()
    if runtime_context is None:
        return None
    return runtime_context.task_id


@contextmanager
def artifact_memory_session() -> Iterator[Any]:
    """Yield a backend ``ArtifactMemoryService`` instance and close DB session safely."""
    from backend.database import SessionLocal
    from backend.services.artifact.memory_service import ArtifactMemoryService

    db = SessionLocal()
    try:
        yield ArtifactMemoryService(db)
    finally:
        db.close()


def build_search_stdout(payload: Dict[str, Any]) -> str:
    """Render concise, LLM-friendly artifact catalog lines from search payload."""
    artifacts = payload.get("artifacts") or []
    if not artifacts:
        return "No artifacts found for the current task and filters."

    lines: List[str] = []
    for index, entry in enumerate(artifacts, start=1):
        if not isinstance(entry, dict):
            continue
        artifact_id = str(entry.get("artifact_id") or "").strip()
        label = str(entry.get("label") or "").strip()
        tool_name = str(entry.get("tool_name") or "").strip()
        artifact_kind = str(entry.get("artifact_kind") or "").strip()
        relative_path = str(entry.get("relative_path") or "").strip()
        path_suffix = f" path={relative_path}" if relative_path else ""
        lines.append(
            f"[{index}] id={artifact_id} label={label} tool={tool_name} kind={artifact_kind}{path_suffix}"
        )

    if not lines:
        return "No artifacts found for the current task and filters."
    return "\n".join(lines)
