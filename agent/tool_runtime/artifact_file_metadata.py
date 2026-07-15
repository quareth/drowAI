"""Artifact file metadata for filesystem-tool parameter planning.

This module collects compact artifact references already present in runtime
metadata and resolves them to bounded file metadata for the native tool
parameter builder. It never reads or returns file content.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


FILESYSTEM_ARTIFACT_TOOL_IDS = frozenset(
    {
        "filesystem.read_file",
        "filesystem.search_text",
    }
)

_MAX_ARTIFACT_METADATA_ENTRIES = 8


def filesystem_artifact_tools_selected(selected_tools: Iterable[str]) -> bool:
    """Return whether selected tools need artifact file metadata."""

    selected = {str(tool_id).strip() for tool_id in selected_tools if str(tool_id).strip()}
    return not FILESYSTEM_ARTIFACT_TOOL_IDS.isdisjoint(selected)


def collect_artifact_file_ref_candidates(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Collect compact artifact refs from current planner metadata."""

    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_ref(raw: Any) -> None:
        path: str | None = None
        label: str | None = None
        if isinstance(raw, str):
            path = raw.strip()
        elif isinstance(raw, Mapping):
            raw_path = raw.get("path") or raw.get("relative_path") or raw.get("artifact_path")
            if raw_path is not None:
                path = str(raw_path).strip()
            raw_label = raw.get("label") or raw.get("artifact_kind") or raw.get("tool_name")
            if raw_label is not None:
                label = str(raw_label).strip() or None
        if not path or path in seen:
            return
        seen.add(path)
        ref: dict[str, Any] = {"path": path}
        if label:
            ref["label"] = label
        refs.append(ref)

    add_ref(metadata.get("last_artifact_path"))

    compact_result = metadata.get("last_tool_result_compact")
    if isinstance(compact_result, Mapping):
        _add_refs_from_sequence(compact_result.get("artifact_refs"), add_ref)

    last_tool_result = metadata.get("last_tool_result")
    if isinstance(last_tool_result, Mapping):
        _add_refs_from_sequence(last_tool_result.get("artifacts"), add_ref)
        result_metadata = last_tool_result.get("metadata")
        if isinstance(result_metadata, Mapping):
            _add_refs_from_sequence(result_metadata.get("artifact_refs"), add_ref)

    for record in _tail_mappings(metadata.get("tool_execution_records"), limit=5):
        _add_refs_from_sequence(record.get("artifact_refs"), add_ref)

    working_memory = metadata.get("working_memory")
    if isinstance(working_memory, Mapping):
        for record in _tail_mappings(working_memory.get("current_turn_phases"), limit=5):
            _add_refs_from_sequence(record.get("artifact_refs"), add_ref)

    return refs[:_MAX_ARTIFACT_METADATA_ENTRIES]


def build_artifact_file_metadata_for_prompt(
    *,
    selected_tools: Iterable[str],
    workspace_path: str | None,
    artifact_refs: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return bounded file metadata only when filesystem artifact tools are selected."""

    if not filesystem_artifact_tools_selected(selected_tools):
        return []
    if not artifact_refs:
        return []

    workspace = _resolve_workspace_root(workspace_path)
    entries: list[dict[str, Any]] = []
    for raw_ref in artifact_refs[:_MAX_ARTIFACT_METADATA_ENTRIES]:
        raw_path = str(raw_ref.get("path") or "").strip()
        if not raw_path:
            continue
        label = str(raw_ref.get("label") or "").strip()
        entry = _metadata_for_path(raw_path, workspace)
        if label:
            entry["label"] = label
        entries.append(entry)
    return entries


def _add_refs_from_sequence(value: Any, add_ref: Any) -> None:
    if isinstance(value, (str, bytes, bytearray)) or isinstance(value, Mapping):
        add_ref(value)
        return
    if not isinstance(value, Sequence):
        return
    for item in value:
        add_ref(item)


def _tail_mappings(value: Any, *, limit: int) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    mappings = [item for item in value if isinstance(item, Mapping)]
    return mappings[-limit:]


def _resolve_workspace_root(workspace_path: str | None) -> Path | None:
    if not workspace_path:
        return None
    try:
        return Path(workspace_path).resolve(strict=False)
    except Exception:
        return None


def _metadata_for_path(raw_path: str, workspace: Path | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": raw_path}
    resolved, reason = _resolve_artifact_path(raw_path, workspace)
    if resolved is None:
        entry.update({"status": "unavailable", "reason": reason})
        return entry

    try:
        stat_result = resolved.stat()
    except OSError:
        entry.update({"status": "unavailable", "reason": "file does not exist"})
        return entry

    if not resolved.is_file():
        entry.update({"status": "unavailable", "reason": "path is not a regular file"})
        return entry

    entry.update(
        {
            "status": "ready",
            "size_bytes": int(stat_result.st_size),
            "line_count": _count_lines(resolved),
        }
    )
    return entry


def _resolve_artifact_path(raw_path: str, workspace: Path | None) -> tuple[Path | None, str]:
    candidate = raw_path.strip()
    if not candidate:
        return None, "path is empty"
    if candidate.startswith("artifact://"):
        return None, "artifact id is not a workspace file path"
    if workspace is None:
        return None, "workspace path is unavailable"

    try:
        if candidate == "/workspace" or candidate.startswith("/workspace/"):
            relative = PurePosixPath(candidate).relative_to(PurePosixPath("/workspace"))
            resolved = (workspace / Path(str(relative))).resolve(strict=False)
        else:
            raw = Path(candidate)
            if raw.is_absolute():
                resolved = raw.resolve(strict=False)
            else:
                resolved = (workspace / raw).resolve(strict=False)
    except Exception as exc:
        return None, f"path resolution failed: {exc}"

    try:
        resolved.relative_to(workspace)
    except ValueError:
        return None, "path resolves outside workspace"
    return resolved, ""


def _count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        for count, _line in enumerate(handle, start=1):
            pass
    return count


__all__ = [
    "FILESYSTEM_ARTIFACT_TOOL_IDS",
    "build_artifact_file_metadata_for_prompt",
    "collect_artifact_file_ref_candidates",
    "filesystem_artifact_tools_selected",
]
