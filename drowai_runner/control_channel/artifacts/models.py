"""Pending artifact-upload context DTO. Data only.

Holds the data_plane manifest context retained until cloud sends signed upload
instructions. No logic, no I/O, no protocol behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from drowai_runner.artifact_manifest import ScannedArtifactFile
from runtime_shared.runner_protocol import (
    RunnerArtifactManifestPayload,
    RunnerArtifactUploadCompleteItem,
)


@dataclass(slots=True)
class _PendingArtifactUploadContext:
    """Manifest context retained until cloud sends signed upload instructions."""

    tool_command_runtime_job_id: str
    task_id: int | None
    manifest_payload: RunnerArtifactManifestPayload
    files_by_client_id: Mapping[str, ScannedArtifactFile]
    upload_completions_by_object_key: dict[str, RunnerArtifactUploadCompleteItem]
