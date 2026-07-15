"""Runner signed-upload executor for data_plane artifact promotion.

This module uploads runner-local artifact bytes to cloud-provided signed upload
targets, applies bounded retries, and returns typed upload-completion payload
items without importing backend services or leaking signed URL material.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import time
from typing import Callable, Mapping, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request

from drowai_runner.artifact_manifest import ScannedArtifactFile
from runtime_shared.runner_protocol import (
    RunnerArtifactUploadCompleteItem,
    RunnerArtifactUploadRequestItem,
)
from runtime_shared.workspace_filesystem import WorkspaceFilesystem

DEFAULT_UPLOAD_RETRY_ATTEMPTS = 3
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 30.0
_RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class ArtifactUploadFailure:
    """Structured upload failure for one cloud-accepted upload instruction."""

    artifact_id: str
    artifact_client_id: str
    object_key: str
    error_code: str
    message: str

    def to_json(self) -> dict[str, str]:
        """Return JSON-safe failure details for tool-result metadata projection."""
        return {
            "artifact_id": self.artifact_id,
            "artifact_client_id": self.artifact_client_id,
            "object_key": self.object_key,
            "error_code": self.error_code,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ArtifactUploadBatchResult:
    """Upload outcome summary for one `artifact.upload.request` payload."""

    completed: tuple[RunnerArtifactUploadCompleteItem, ...]
    failures: tuple[ArtifactUploadFailure, ...]

    def failures_json(self) -> list[dict[str, str]]:
        """Serialize upload failures for metadata reporting."""
        return [failure.to_json() for failure in self.failures]


class RunnerArtifactUploader:
    """Upload cloud-accepted artifact objects with bounded retries and dedupe."""

    def __init__(
        self,
        *,
        max_attempts: int = DEFAULT_UPLOAD_RETRY_ATTEMPTS,
        upload_timeout_seconds: float = DEFAULT_UPLOAD_TIMEOUT_SECONDS,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max_attempts = max(1, int(max_attempts))
        self._upload_timeout_seconds = max(1.0, float(upload_timeout_seconds))
        self._sleep = sleep_fn

    def upload(
        self,
        *,
        uploads: Sequence[RunnerArtifactUploadRequestItem],
        files_by_client_id: Mapping[str, ScannedArtifactFile],
        uploaded_by_object_key: Mapping[str, RunnerArtifactUploadCompleteItem] | None = None,
    ) -> ArtifactUploadBatchResult:
        """Upload cloud-accepted artifacts and return completion/failure summaries."""
        completed_by_object_key: dict[str, RunnerArtifactUploadCompleteItem] = dict(uploaded_by_object_key or {})
        completed: list[RunnerArtifactUploadCompleteItem] = []
        failures: list[ArtifactUploadFailure] = []

        for item in uploads:
            cached = completed_by_object_key.get(item.object_key)
            if cached is not None:
                completed.append(cached)
                continue

            local_file = files_by_client_id.get(item.artifact_client_id)
            if local_file is None:
                failures.append(
                    ArtifactUploadFailure(
                        artifact_id=item.artifact_id,
                        artifact_client_id=item.artifact_client_id,
                        object_key=item.object_key,
                        error_code="ARTIFACT_LOCAL_FILE_MISSING",
                        message="No local artifact matched cloud upload instruction.",
                    )
                )
                continue
            if local_file.size_bytes != item.size_bytes:
                failures.append(
                    ArtifactUploadFailure(
                        artifact_id=item.artifact_id,
                        artifact_client_id=item.artifact_client_id,
                        object_key=item.object_key,
                        error_code="ARTIFACT_SIZE_MISMATCH",
                        message="Local artifact size did not match cloud upload instruction.",
                    )
                )
                continue
            if local_file.content_sha256 != item.content_sha256:
                failures.append(
                    ArtifactUploadFailure(
                        artifact_id=item.artifact_id,
                        artifact_client_id=item.artifact_client_id,
                        object_key=item.object_key,
                        error_code="ARTIFACT_HASH_MISMATCH",
                        message="Local artifact hash did not match cloud upload instruction.",
                    )
                )
                continue

            upload_error: ArtifactUploadFailure | None = None
            for attempt in range(1, self._max_attempts + 1):
                try:
                    self._upload_single(item=item, local_file=local_file)
                    completion = RunnerArtifactUploadCompleteItem(
                        artifact_id=item.artifact_id,
                        artifact_client_id=item.artifact_client_id,
                        object_key=item.object_key,
                        size_bytes=local_file.size_bytes,
                        content_sha256=local_file.content_sha256,
                        uploaded_at=datetime.now(tz=UTC).isoformat(),
                    )
                    completed_by_object_key[item.object_key] = completion
                    completed.append(completion)
                    upload_error = None
                    break
                except Exception as exc:  # pragma: no cover - retry control path
                    retryable = _is_retryable_upload_error(exc)
                    upload_error = ArtifactUploadFailure(
                        artifact_id=item.artifact_id,
                        artifact_client_id=item.artifact_client_id,
                        object_key=item.object_key,
                        error_code="ARTIFACT_UPLOAD_FAILED",
                        message=f"Upload attempt {attempt} failed ({type(exc).__name__}).",
                    )
                    if not retryable or attempt >= self._max_attempts:
                        break
                    self._sleep(min(1.0, 0.1 * attempt))
            if upload_error is not None:
                failures.append(upload_error)

        return ArtifactUploadBatchResult(
            completed=tuple(completed),
            failures=tuple(failures),
        )

    def _upload_single(
        self,
        *,
        item: RunnerArtifactUploadRequestItem,
        local_file: ScannedArtifactFile,
    ) -> None:
        method = str(item.upload_method or "PUT").strip().upper() or "PUT"
        headers = dict(item.upload_headers or {})
        if "content-type" not in {key.lower() for key in headers.keys()}:
            headers["Content-Type"] = item.content_type or local_file.content_type
        if local_file.workspace_root is None:
            raise ValueError("Artifact workspace root is unavailable.")
        filesystem = WorkspaceFilesystem(local_file.workspace_root)
        payload = filesystem.read_bytes(local_file.relative_path)
        if len(payload) != local_file.size_bytes:
            raise ValueError("Artifact changed after manifest creation.")
        if hashlib.sha256(payload).hexdigest() != local_file.content_sha256:
            raise ValueError("Artifact changed after manifest creation.")
        request = urllib_request.Request(
            item.upload_url,
            data=payload,
            method=method,
            headers=headers,
        )
        with urllib_request.urlopen(request, timeout=self._upload_timeout_seconds) as response:
            status_code = int(getattr(response, "status", 200) or 200)
            if status_code < 200 or status_code >= 300:
                raise urllib_error.HTTPError(
                    item.upload_url,
                    status_code,
                    "Signed artifact upload failed.",
                    hdrs=getattr(response, "headers", None),
                    fp=None,
                )


def _is_retryable_upload_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib_error.URLError):
        return True
    if isinstance(exc, urllib_error.HTTPError):
        return exc.code in _RETRYABLE_HTTP_STATUS_CODES
    return False
