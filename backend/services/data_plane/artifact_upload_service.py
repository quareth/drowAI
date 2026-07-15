"""Runner artifact upload-completion service for data plane readiness.

Scope:
- Owns backend-side ingest for inbound `artifact.upload.complete` channel
  messages after protocol-level runner identity checks.
- Verifies accepted artifact identity, confirms object existence plus size/hash
  integrity where object-store metadata is available, transitions artifact
  upload state, and updates manifest readiness summaries.
- Emits browser-facing upload status stream events with safe metadata only.

Boundaries:
- Never emits or persists signed upload/download URLs or secret headers.
- Does not generate upload instructions; manifest ingest remains responsible for
  creating placeholders and signed upload targets.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.provenance import ArtifactManifest, ExecutionArtifact, ToolExecution
from backend.services.data_plane.object_store import ObjectStore
from backend.services.data_plane.registry import get_object_store
from runtime_shared.runner_protocol import (
    RunnerArtifactUploadCompletePayload,
    RunnerEnvelope,
    RunnerMessageType,
)

logger = logging.getLogger(__name__)


class ArtifactUploadServiceError(ValueError):
    """Raised when upload-complete channel processing fails closed."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class ArtifactUploadHandleResult:
    """Service result for one inbound upload-complete message."""

    response_envelopes: tuple[RunnerEnvelope, ...] = ()


@dataclass(frozen=True, slots=True)
class _ArtifactVerificationFailure:
    """Deterministic verification failure attached to one artifact row."""

    error_code: str
    message: str


class ArtifactUploadService:
    """Validate and apply runner upload-complete transitions for manifest artifacts."""

    def __init__(
        self,
        db: Session,
        *,
        object_store: ObjectStore | None = None,
    ) -> None:
        self._db = db
        self._object_store = object_store or get_object_store()

    def handle_inbound_message(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        runtime_job_id: UUID,
        envelope: RunnerEnvelope,
    ) -> ArtifactUploadHandleResult:
        """Handle one validated inbound `artifact.upload.complete` message."""
        if envelope.message_type is not RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE:
            raise ArtifactUploadServiceError(
                error_code="RUNNER_ARTIFACT_MESSAGE_TYPE_INVALID",
                message=f"Unsupported artifact upload message type `{envelope.type}`.",
            )
        payload = envelope.payload
        if not isinstance(payload, RunnerArtifactUploadCompletePayload):
            raise ArtifactUploadServiceError(
                error_code="RUNNER_PROTOCOL_INVALID",
                message="artifact.upload.complete requires a typed payload.",
            )
        if not payload.uploads:
            raise ArtifactUploadServiceError(
                error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                message="artifact.upload.complete uploads must not be empty.",
            )

        artifacts_by_id, manifest = self._load_and_validate_bindings(
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=task_id,
            runtime_job_id=runtime_job_id,
            payload=payload,
        )

        ready_count = 0
        failed_count = 0
        artifact_statuses: list[dict[str, Any]] = []

        for upload_item in payload.uploads:
            artifact = artifacts_by_id[upload_item.artifact_id]
            verification_failure = self._verify_uploaded_object(artifact=artifact, upload_item=upload_item)

            if verification_failure is None:
                artifact.upload_status = "ready"
                artifact.uploaded_at = _parse_optional_uploaded_at(upload_item.uploaded_at) or _utcnow()
                artifact.artifact_metadata = _merge_json_dicts(
                    artifact.artifact_metadata,
                    {
                        "upload_completed": {
                            "completed_at": artifact.uploaded_at.isoformat(),
                            "source": "artifact.upload.complete",
                        },
                    },
                )
                ready_count += 1
                artifact_statuses.append(
                    {
                        "artifact_id": str(artifact.id),
                        "upload_status": "ready",
                        "error_code": None,
                    }
                )
                continue

            artifact.upload_status = "upload_failed"
            artifact.uploaded_at = None
            artifact.artifact_metadata = _merge_json_dicts(
                artifact.artifact_metadata,
                {
                    "upload_error": {
                        "error_code": verification_failure.error_code,
                        "message": verification_failure.message,
                        "recorded_at": _utcnow().isoformat(),
                    }
                },
            )
            failed_count += 1
            artifact_statuses.append(
                {
                    "artifact_id": str(artifact.id),
                    "upload_status": "upload_failed",
                    "error_code": verification_failure.error_code,
                }
            )

        self._db.flush()
        manifest_status = self._resolve_manifest_status(manifest_id=manifest.id)
        manifest.status = manifest_status
        manifest.manifest_metadata = _merge_json_dicts(
            manifest.manifest_metadata,
            {
                "upload_completion": {
                    "ready_item_count": ready_count,
                    "failed_item_count": failed_count,
                    "reported_item_count": len(payload.uploads),
                    "manifest_status": manifest_status,
                    "updated_at": _utcnow().isoformat(),
                },
            },
        )

        self._publish_browser_status_event(
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=task_id,
            runtime_job_id=runtime_job_id,
            command_id=payload.command_id,
            workspace_id=payload.workspace_id,
            manifest_id=manifest.id,
            manifest_status=manifest_status,
            artifact_statuses=artifact_statuses,
            message_id=envelope.message_id,
            correlation_id=envelope.correlation_id,
        )
        self._reconcile_knowledge_after_upload_complete(
            task_id=task_id,
            artifact_statuses=artifact_statuses,
            artifacts_by_id=artifacts_by_id,
        )

        self._db.flush()
        return ArtifactUploadHandleResult()

    def _reconcile_knowledge_after_upload_complete(
        self,
        *,
        task_id: int,
        artifact_statuses: Sequence[Mapping[str, Any]],
        artifacts_by_id: Mapping[str, ExecutionArtifact],
    ) -> None:
        ready_execution_ids: set[str] = set()
        for status in artifact_statuses:
            artifact_id = str(status.get("artifact_id") or "").strip()
            upload_status = str(status.get("upload_status") or "").strip().lower()
            if not artifact_id or upload_status != "ready":
                continue
            artifact = artifacts_by_id.get(artifact_id)
            if artifact is None or artifact.execution_id is None:
                continue
            execution = self._db.execute(
                select(ToolExecution).where(ToolExecution.id == artifact.execution_id)
            ).scalar_one_or_none()
            if execution is None or execution.finished_at is None:
                continue
            ready_execution_ids.add(str(execution.id))

        if not ready_execution_ids:
            return

        from backend.services.knowledge.archive_service import KnowledgeArchiveService
        from backend.services.knowledge.evidence_storage_service import EvidenceStorageService
        from backend.services.knowledge.ingestion_service import KnowledgeIngestionService

        ingestion_service = KnowledgeIngestionService(
            self._db,
            archive_service=KnowledgeArchiveService(
                self._db,
                evidence_storage_service=EvidenceStorageService(object_store=self._object_store),
            ),
        )
        for execution_id in sorted(ready_execution_ids):
            try:
                result = ingestion_service.ingest_execution(
                    task_id=int(task_id),
                    source_execution_id=execution_id,
                    delete_survival_required=True,
                    raise_on_error=False,
                )
                if not bool(result.get("ok")):
                    logger.warning(
                        "[KNOWLEDGE_INGESTION] Upload-complete reconciliation did not succeed "
                        "(task_id=%s execution_id=%s status=%s error=%s).",
                        task_id,
                        execution_id,
                        result.get("status"),
                        result.get("error"),
                    )
            except Exception:
                logger.exception(
                    "[KNOWLEDGE_INGESTION] Upload-complete reconciliation failed "
                    "(task_id=%s execution_id=%s).",
                    task_id,
                    execution_id,
                )

    def _load_and_validate_bindings(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        runtime_job_id: UUID,
        payload: RunnerArtifactUploadCompletePayload,
    ) -> tuple[dict[str, ExecutionArtifact], ArtifactManifest]:
        artifact_ids = _parse_artifact_ids(payload.uploads)
        rows = self._db.execute(
            select(ExecutionArtifact, ArtifactManifest)
            .join(ArtifactManifest, ArtifactManifest.id == ExecutionArtifact.manifest_id)
            .where(
                ExecutionArtifact.id.in_(artifact_ids),
                ExecutionArtifact.tenant_id == tenant_id,
                ExecutionArtifact.task_id == task_id,
                ExecutionArtifact.runtime_job_id == runtime_job_id,
                ExecutionArtifact.runner_id == runner_id,
                ExecutionArtifact.command_id == payload.command_id,
                ExecutionArtifact.object_key.isnot(None),
                ArtifactManifest.tenant_id == tenant_id,
                ArtifactManifest.task_id == task_id,
                ArtifactManifest.runtime_job_id == runtime_job_id,
                ArtifactManifest.runner_id == runner_id,
                ArtifactManifest.command_id == payload.command_id,
                ArtifactManifest.workspace_id == payload.workspace_id,
            )
        ).all()
        if not rows:
            raise ArtifactUploadServiceError(
                error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                message="artifact.upload.complete references an unaccepted artifact identity.",
            )

        artifacts_by_id: dict[str, ExecutionArtifact] = {}
        manifest: ArtifactManifest | None = None
        for artifact, artifact_manifest in rows:
            artifacts_by_id[str(artifact.id)] = artifact
            if manifest is None:
                manifest = artifact_manifest

        if manifest is None:
            raise ArtifactUploadServiceError(
                error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                message="artifact.upload.complete manifest binding is missing.",
            )

        for upload_item in payload.uploads:
            artifact = artifacts_by_id.get(upload_item.artifact_id)
            if artifact is None:
                raise ArtifactUploadServiceError(
                    error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                    message="artifact.upload.complete references an unaccepted artifact identity.",
                )
            if str(artifact.object_key or "").strip() != upload_item.object_key:
                raise ArtifactUploadServiceError(
                    error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                    message="artifact.upload.complete object key does not match accepted artifact identity.",
                )
            expected_hash = str(artifact.content_sha256 or "").strip().lower()
            if expected_hash and expected_hash != upload_item.content_sha256.lower():
                raise ArtifactUploadServiceError(
                    error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                    message="artifact.upload.complete content hash does not match accepted artifact identity.",
                )
            expected_size = int(artifact.byte_size or 0)
            if expected_size and expected_size != int(upload_item.size_bytes):
                raise ArtifactUploadServiceError(
                    error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                    message="artifact.upload.complete size does not match accepted artifact identity.",
                )
            metadata = artifact.artifact_metadata if isinstance(artifact.artifact_metadata, Mapping) else {}
            expected_client_id = str(metadata.get("artifact_client_id") or "").strip()
            if expected_client_id and expected_client_id != upload_item.artifact_client_id:
                raise ArtifactUploadServiceError(
                    error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                    message="artifact.upload.complete client artifact id does not match accepted artifact identity.",
                )

        return artifacts_by_id, manifest

    def _verify_uploaded_object(
        self,
        *,
        artifact: ExecutionArtifact,
        upload_item: Any,
    ) -> _ArtifactVerificationFailure | None:
        object_key = str(upload_item.object_key or "").strip()
        try:
            object_head = self._object_store.head_object(object_key)
        except Exception as exc:
            return _ArtifactVerificationFailure(
                error_code="RUNNER_ARTIFACT_OBJECT_HEAD_FAILED",
                message=f"Unable to verify uploaded object metadata: {exc}",
            )

        if object_head is None:
            return _ArtifactVerificationFailure(
                error_code="RUNNER_ARTIFACT_OBJECT_MISSING",
                message="Uploaded object key was not found in object storage.",
            )

        expected_size = int(artifact.byte_size or 0)
        if expected_size and int(object_head.byte_size) != expected_size:
            return _ArtifactVerificationFailure(
                error_code="RUNNER_ARTIFACT_UPLOAD_SIZE_MISMATCH",
                message="Uploaded object size does not match manifest size.",
            )

        head_hash = str(object_head.content_sha256 or "").strip().lower()
        expected_hash = str(artifact.content_sha256 or "").strip().lower()
        if head_hash and expected_hash and head_hash != expected_hash:
            return _ArtifactVerificationFailure(
                error_code="RUNNER_ARTIFACT_UPLOAD_HASH_MISMATCH",
                message="Uploaded object hash does not match manifest hash.",
            )

        return None

    def _resolve_manifest_status(self, *, manifest_id: UUID) -> str:
        statuses = [
            str(status or "").strip().lower()
            for status in self._db.execute(
                select(ExecutionArtifact.upload_status).where(ExecutionArtifact.manifest_id == manifest_id)
            ).scalars()
        ]
        if not statuses:
            return "failed"

        ready_count = sum(1 for status in statuses if status == "ready")
        failed_count = sum(1 for status in statuses if status == "upload_failed")
        total = len(statuses)

        if ready_count == total:
            return "ready"
        if failed_count == total:
            return "failed"
        return "partially_ready"

    def _publish_browser_status_event(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        runtime_job_id: UUID,
        command_id: str,
        workspace_id: str,
        manifest_id: UUID,
        manifest_status: str,
        artifact_statuses: Sequence[Mapping[str, Any]],
        message_id: str,
        correlation_id: str | None,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        try:
            from backend.services.streaming.in_memory_hub import get_in_memory_stream_hub
        except Exception:
            return

        event_metadata = {
            "tenant_id": tenant_id,
            "runner_id": str(runner_id),
            "runtime_job_id": str(runtime_job_id),
            "task_id": task_id,
            "command_id": command_id,
            "workspace_id": workspace_id,
            "manifest_id": str(manifest_id),
            "manifest_status": manifest_status,
            "message_id": message_id,
            "correlation_id": correlation_id,
            "artifact_statuses": [dict(item) for item in artifact_statuses],
        }
        loop.create_task(
            get_in_memory_stream_hub().publish(
                task_id,
                {
                    "type": "status",
                    "content": "artifact_upload_status",
                    "metadata": event_metadata,
                },
            )
        )


def _parse_artifact_ids(uploads: Sequence[Any]) -> tuple[UUID, ...]:
    parsed_ids: list[UUID] = []
    for upload_item in uploads:
        artifact_id = str(getattr(upload_item, "artifact_id", "") or "").strip()
        try:
            parsed_ids.append(UUID(artifact_id))
        except ValueError as exc:
            raise ArtifactUploadServiceError(
                error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                message="artifact.upload.complete artifact_id must be a UUID assigned by cloud.",
            ) from exc
    return tuple(parsed_ids)


def _parse_optional_uploaded_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _merge_json_dicts(base: object, patch: object) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base) if isinstance(base, Mapping) else {}
    if not isinstance(patch, Mapping):
        return merged
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[str(key)] = _merge_json_dicts(existing, value)
            continue
        if isinstance(value, Mapping):
            merged[str(key)] = dict(value)
            continue
        merged[str(key)] = value
    return merged


def _utcnow() -> datetime:
    return datetime.now(UTC)
