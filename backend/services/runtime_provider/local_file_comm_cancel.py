"""Local file-comm cancellation writer for provider-owned runtime stops.

This module appends backend-authored cancellation requests to the shared
file-comm workspace so the in-container executor can interrupt active commands.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from backend.core.time_utils import utc_now
from runtime_shared.file_comm_contracts import (
    CANCELLATIONS_FILE_NAME,
    CANCELLATIONS_LOCK_FILE_NAME,
    CancellationMessage,
    LOCKS_DIRECTORY_NAME,
)
from runtime_shared.workspace_filesystem import WorkspaceFilesystem


@dataclass(frozen=True, slots=True)
class LocalFileCommCancelResult:
    """Summary of cancellation rows appended to a local workspace."""

    command_ids: tuple[str, ...]
    cancellation_ids: tuple[str, ...]


def append_file_comm_cancellations(
    *,
    workspace_path: str | Path,
    command_ids: Iterable[str],
    reason: str,
    source: str = "chat_stop",
) -> LocalFileCommCancelResult:
    """Append cancellation messages for command ids in a local task workspace."""
    resolved_command_ids = tuple(
        dict.fromkeys(str(command_id or "").strip() for command_id in command_ids if str(command_id or "").strip())
    )
    if not resolved_command_ids:
        return LocalFileCommCancelResult(command_ids=(), cancellation_ids=())

    workspace = Path(workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)
    filesystem = WorkspaceFilesystem(workspace)
    filesystem.mkdirs(LOCKS_DIRECTORY_NAME, mode=0o755)

    cancellation_ids: list[str] = []
    timestamp = utc_now().isoformat()
    rows: list[str] = []
    for command_id in resolved_command_ids:
        cancellation_id = str(uuid4())
        message = CancellationMessage(
            id=cancellation_id,
            timestamp=timestamp,
            command_id=command_id,
            reason=(reason or "user_stop").strip() or "user_stop",
            source=(source or "chat_stop").strip() or "chat_stop",
        )
        rows.append(json.dumps(message.model_dump(), separators=(",", ":")) + "\n")
        cancellation_ids.append(cancellation_id)
    filesystem.append_bytes_locked(
        CANCELLATIONS_FILE_NAME,
        f"{LOCKS_DIRECTORY_NAME}/{CANCELLATIONS_LOCK_FILE_NAME}",
        "".join(rows).encode("utf-8"),
        mode=0o644,
    )

    return LocalFileCommCancelResult(
        command_ids=resolved_command_ids,
        cancellation_ids=tuple(cancellation_ids),
    )
