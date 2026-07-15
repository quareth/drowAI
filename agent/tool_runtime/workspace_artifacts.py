"""Workspace-local tool output artifact persistence for runtime transports.

This module owns the runtime contract for saving tool command output into the
task workspace and indexing the saved file for later filesystem/context reads.
It is intentionally below LangGraph so local graph execution, Kali file-comm,
and runner PTY transports all use the same behavior.
"""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass(frozen=True, slots=True)
class WorkspaceIndexWrite:
    """Chunk-index bytes produced by one artifact indexing operation."""

    path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class WorkspaceArtifactSaveResult:
    """Saved raw output artifact plus index bytes appended for that save."""

    artifact_path: Optional[str]
    index_writes: tuple[WorkspaceIndexWrite, ...] = ()


_INDEX_CAPTURE_LOCK = threading.Lock()


SKIP_WORKSPACE_ARTIFACT_TOOLS = frozenset(
    {
        "filesystem.read_file",
        "filesystem.stat_path",
        "filesystem.list_dir",
        "filesystem.find_paths",
        "filesystem.search_text",
        "information_gathering.web_enumeration.http_download",
        "artifact.search",
        "artifact.read",
    }
)


def should_persist_workspace_artifact(tool_id: Optional[str]) -> bool:
    """Return whether a tool execution should create a workspace artifact."""
    normalized_tool_id = str(tool_id or "").strip()
    return (
        bool(normalized_tool_id)
        and normalized_tool_id not in SKIP_WORKSPACE_ARTIFACT_TOOLS
    )


def save_and_index_tool_output_artifact(
    *,
    workspace_path: str,
    stdout: str,
    stderr: str = "",
    selected_tool: Optional[str] = None,
) -> Optional[str]:
    """Persist command output and synchronously index it in the task workspace."""
    return save_and_index_tool_output_artifact_with_index_writes(
        workspace_path=workspace_path,
        stdout=stdout,
        stderr=stderr,
        selected_tool=selected_tool,
    ).artifact_path


def save_and_index_tool_output_artifact_with_index_writes(
    *,
    workspace_path: str,
    stdout: str,
    stderr: str = "",
    selected_tool: Optional[str] = None,
) -> WorkspaceArtifactSaveResult:
    """Persist command output and return the index bytes appended by this call."""
    if not should_persist_workspace_artifact(selected_tool):
        return WorkspaceArtifactSaveResult(artifact_path=None)

    from agent.utils.artifact_manager import save_tool_output_artifact

    artifact_path = save_tool_output_artifact(
        workspace_path=workspace_path,
        stdout=stdout,
        stderr=stderr,
        logger=None,
    )
    if not artifact_path:
        return WorkspaceArtifactSaveResult(artifact_path=None)

    index_writes = index_workspace_artifact(
        artifact_path=artifact_path,
        workspace_path=workspace_path,
        selected_tool=selected_tool,
    )
    return WorkspaceArtifactSaveResult(
        artifact_path=artifact_path,
        index_writes=index_writes,
    )


def index_workspace_artifact(
    *,
    artifact_path: str,
    workspace_path: str,
    selected_tool: Optional[str],
) -> tuple[WorkspaceIndexWrite, ...]:
    """Index one workspace artifact and configured sibling artifacts."""
    from agent.config.chunking_config import (
        CHUNKING_PROFILES_DIR,
        DEFAULT_MAX_CHUNK_TOKENS,
        INGEST_SIBLING_ARTIFACTS,
        MAX_SIBLINGS_PER_ARTIFACT,
        SIBLING_EXTENSIONS,
    )
    from agent.context.chunking.artifact_ingestor import SimpleArtifactIngestor
    from agent.utils.workspace_helpers import (
        get_index_directory,
        get_run_id_from_workspace,
    )

    workspace = Path(workspace_path)
    artifact_file = Path(artifact_path)
    if not artifact_file.is_absolute():
        artifact_file = workspace / artifact_file

    index_dir = get_index_directory(str(workspace), respect_env_override=False)
    Path(index_dir).mkdir(parents=True, exist_ok=True)

    profiles_dir = (
        str(CHUNKING_PROFILES_DIR)
        if CHUNKING_PROFILES_DIR and CHUNKING_PROFILES_DIR.exists()
        else None
    )
    ingestor = SimpleArtifactIngestor(
        index_dir=index_dir,
        max_chunk_tokens=DEFAULT_MAX_CHUNK_TOKENS,
        profiles_dir=profiles_dir,
    )

    run_id = get_run_id_from_workspace(str(workspace))
    tool_name = selected_tool or "unknown"
    manifest_path = Path(index_dir) / f"chunks_{run_id}.jsonl"
    with _INDEX_CAPTURE_LOCK:
        before_size = manifest_path.stat().st_size if manifest_path.exists() else 0
        ingestor.ingest(
            run_id=run_id,
            artifact_path=str(artifact_file),
            tool_name=tool_name,
            meta={},
        )

        if INGEST_SIBLING_ARTIFACTS:
            sibling_paths = [
                sibling
                for sibling in artifact_file.parent.glob("*.*")
                if sibling.name != artifact_file.name
                and sibling.suffix.lower() in SIBLING_EXTENSIONS
                and sibling.stat().st_size > 0
            ]

            for sibling_path in sibling_paths[:MAX_SIBLINGS_PER_ARTIFACT]:
                try:
                    ingestor.ingest(
                        run_id=run_id,
                        artifact_path=str(sibling_path),
                        tool_name=tool_name,
                        meta={},
                    )
                except Exception:
                    continue

        return _capture_manifest_append(
            manifest_path=manifest_path,
            workspace=workspace,
            before_size=before_size,
        )


def _capture_manifest_append(
    *,
    manifest_path: Path,
    workspace: Path,
    before_size: int,
) -> tuple[WorkspaceIndexWrite, ...]:
    """Return bytes appended to one chunk manifest since ``before_size``."""
    if not manifest_path.exists() or not manifest_path.is_file():
        return ()
    try:
        file_size = manifest_path.stat().st_size
        start = before_size if file_size >= before_size else 0
        with manifest_path.open("rb") as handle:
            handle.seek(start)
            content = handle.read()
        if not content:
            return ()
        relative_path = (
            manifest_path.resolve()
            .relative_to(workspace.resolve())
            .as_posix()
        )
    except Exception:
        return ()
    return (WorkspaceIndexWrite(path=relative_path, content=content),)


def schedule_workspace_artifact_indexing(
    *,
    artifact_path: Optional[str],
    workspace_path: Optional[str],
    selected_tool: Optional[str],
    create_task_fn: Callable[[Any], Any] = asyncio.create_task,
) -> None:
    """Schedule best-effort workspace artifact indexing for async graph callers."""
    if not artifact_path or not workspace_path:
        return

    if os.getenv("LANGGRAPH_ENABLE_ARTIFACT_INDEXING", "true").lower() != "true":
        return

    async def _index_artifact() -> None:
        try:
            index_workspace_artifact(
                artifact_path=artifact_path,
                workspace_path=workspace_path,
                selected_tool=selected_tool,
            )
        except Exception:
            pass

    create_task_fn(_index_artifact())


__all__ = [
    "SKIP_WORKSPACE_ARTIFACT_TOOLS",
    "WorkspaceArtifactSaveResult",
    "WorkspaceIndexWrite",
    "index_workspace_artifact",
    "save_and_index_tool_output_artifact",
    "save_and_index_tool_output_artifact_with_index_writes",
    "schedule_workspace_artifact_indexing",
    "should_persist_workspace_artifact",
]
