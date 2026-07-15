"""Tool-command cache / inflight / dispatch-event DTOs. Data only.

Holds the tooling_plane dataclasses tracking cached tool-command outcomes, inflight
dispatch state, and the dispatch completion/failure events surfaced back to the
main loop. No logic, no I/O, no protocol behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from drowai_runner.artifact_manifest import ScannedArtifactFile
from runtime_shared.runner_protocol import (
    RunnerArtifactManifestPayload,
    RunnerArtifactUploadCompleteItem,
    RunnerToolResultPayload,
)


@dataclass(frozen=True, slots=True)
class _ToolCommandCacheEntry:
    """Cached outcome for a logical tooling_plane tool command to prevent duplicate execution."""

    task_runtime_job_id: str
    command_id: str
    tool_command_runtime_job_id: str
    task_id: int | None
    result_payload: RunnerToolResultPayload
    workspace_id: str
    tool_call_id: str | None
    tool_batch_id: str | None
    manifest_payload: RunnerArtifactManifestPayload | None
    files_by_client_id: Mapping[str, ScannedArtifactFile]
    upload_completions_by_object_key: dict[str, RunnerArtifactUploadCompleteItem]


@dataclass(slots=True)
class _ToolCommandInflightEntry:
    """Tracks accepted tool commands whose dispatch is still in progress."""

    tool_command_runtime_job_id: str
    task_id: int | None
    replay_requests: list[tuple[str | None, int | None]]


@dataclass(frozen=True, slots=True)
class _ToolCommandDispatchCompleted:
    """Background dispatch completion payload emitted back to the main loop."""

    command_key: tuple[str, str]
    cache_entry: _ToolCommandCacheEntry
    correlation_id: str | None


@dataclass(frozen=True, slots=True)
class _ToolCommandDispatchFailed:
    """Background dispatch failure payload surfaced back to the main loop."""

    error: BaseException
