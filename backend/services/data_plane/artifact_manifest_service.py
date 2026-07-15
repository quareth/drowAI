"""Runner artifact-manifest ingest service for data plane promotion.

Scope:
- Owns backend-side ingest for inbound `artifact.manifest` and identity checks
  for `artifact.upload.complete` runner messages.
- Validates runtime binding, creates/updates manifest rows, ensures
  tenant-bound skeletal `tool_executions`, creates artifact placeholders, and
  returns signed upload instructions to runners.

Boundaries:
- This module creates signed upload instructions but never persists signed URLs
  or secret headers in database rows/metadata.
- Object upload verification and final readiness transitions are handled by the
  upload-completion service in later wave tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config.data_plane import DataPlaneConfig, get_data_plane_config
from backend.models.provenance import ArtifactManifest, ExecutionArtifact, ToolExecution
from backend.models.runner_control import RuntimeJob
from backend.repositories.execution_artifact_repository import ExecutionArtifactRepository
from backend.repositories.tool_execution_repository import ToolExecutionRepository
from backend.services.data_plane.object_key_builder import build_artifact_object_key
from backend.services.data_plane.object_store import ObjectStore
from backend.services.data_plane.registry import get_object_store
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_DATA_PLANE_VERSION,
    RunnerArtifactManifestItem,
    RunnerArtifactManifestPayload,
    RunnerArtifactUploadCompletePayload,
    RunnerArtifactUploadRequestItem,
    RunnerArtifactUploadRequestPayload,
    RunnerEnvelope,
    RunnerMessageType,
)


class ArtifactManifestServiceError(ValueError):
    """Raised when artifact-manifest channel processing fails."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class ArtifactManifestHandleResult:
    """Service result for one inbound artifact message."""

    response_envelopes: tuple[RunnerEnvelope, ...] = ()


@dataclass(frozen=True, slots=True)
class _ManifestRuntimeBinding:
    """Validated command/task runtime-job identity for one artifact manifest."""

    command_runtime_job: RuntimeJob
    task_runtime_job: RuntimeJob
    command_payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _ManifestRejection:
    """Per-item rejection result retained in manifest metadata."""

    index: int
    artifact_client_id: str
    relative_path: str
    error_code: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "artifact_client_id": self.artifact_client_id,
            "relative_path": self.relative_path,
            "error_code": self.error_code,
            "reason": self.reason,
        }


class ArtifactManifestService:
    """Validate and handle inbound runner artifact manifest channel messages."""

    def __init__(
        self,
        db: Session,
        *,
        object_store: ObjectStore | None = None,
        data_plane_config: DataPlaneConfig | None = None,
        execution_repository: ToolExecutionRepository | None = None,
        artifact_repository: ExecutionArtifactRepository | None = None,
    ) -> None:
        self._db = db
        self._object_store = object_store or get_object_store()
        self._data_plane_config = data_plane_config or get_data_plane_config()
        self._execution_repository = execution_repository or ToolExecutionRepository(db)
        self._artifact_repository = artifact_repository or ExecutionArtifactRepository(db)

    def handle_inbound_message(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        runtime_job_id: UUID,
        envelope: RunnerEnvelope,
    ) -> ArtifactManifestHandleResult:
        """Handle one validated inbound artifact message."""
        if envelope.message_type is RunnerMessageType.ARTIFACT_MANIFEST:
            return self._handle_manifest_message(
                tenant_id=tenant_id,
                runner_id=runner_id,
                task_id=task_id,
                runtime_job_id=runtime_job_id,
                envelope=envelope,
            )
        if envelope.message_type is RunnerMessageType.ARTIFACT_UPLOAD_COMPLETE:
            self._validate_upload_complete_identity(
                tenant_id=tenant_id,
                runner_id=runner_id,
                task_id=task_id,
                runtime_job_id=runtime_job_id,
                payload=envelope.payload,
            )
            return ArtifactManifestHandleResult()
        raise ArtifactManifestServiceError(
            error_code="RUNNER_ARTIFACT_MESSAGE_TYPE_INVALID",
            message=f"Unsupported artifact message type `{envelope.type}`.",
        )

    def _handle_manifest_message(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        runtime_job_id: UUID,
        envelope: RunnerEnvelope,
    ) -> ArtifactManifestHandleResult:
        payload = envelope.payload
        if not isinstance(payload, RunnerArtifactManifestPayload):
            raise ArtifactManifestServiceError(
                error_code="RUNNER_PROTOCOL_INVALID",
                message="artifact.manifest requires a typed payload.",
            )

        runtime_binding = self._validate_manifest_runtime_binding(
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=task_id,
            runtime_job_id=runtime_job_id,
            payload=payload,
        )
        execution = self._find_or_create_skeletal_execution(
            tenant_id=tenant_id,
            task_id=task_id,
            runner_id=runner_id,
            runtime_job_id=runtime_job_id,
            payload=payload,
            runtime_binding=runtime_binding,
            message_id=envelope.message_id,
        )
        manifest = self._find_or_create_manifest_row(
            tenant_id=tenant_id,
            task_id=task_id,
            runtime_job_id=runtime_job_id,
            runner_id=runner_id,
            payload=payload,
            message_id=envelope.message_id,
        )

        uploads, rejections = self._create_or_update_artifact_placeholders(
            tenant_id=tenant_id,
            task_id=task_id,
            runner_id=runner_id,
            runtime_job_id=runtime_job_id,
            payload=payload,
            manifest=manifest,
            execution=execution,
        )

        self._update_manifest_summary(
            manifest=manifest,
            payload=payload,
            execution=execution,
            uploads=uploads,
            rejections=rejections,
        )

        upload_request_envelope = self._build_upload_request_envelope(
            tenant_id=tenant_id,
            runner_id=runner_id,
            task_id=task_id,
            runtime_job_id=runtime_job_id,
            payload=payload,
            correlation_id=envelope.correlation_id,
            manifest_id=manifest.id,
            uploads=uploads,
        )
        self._db.flush()

        if upload_request_envelope is None:
            return ArtifactManifestHandleResult()
        return ArtifactManifestHandleResult(response_envelopes=(upload_request_envelope,))

    def _validate_manifest_runtime_binding(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        runtime_job_id: UUID,
        payload: RunnerArtifactManifestPayload,
    ) -> _ManifestRuntimeBinding:
        command_job = self._db.execute(
            select(RuntimeJob).where(
                RuntimeJob.id == runtime_job_id,
                RuntimeJob.tenant_id == tenant_id,
                RuntimeJob.runner_id == runner_id,
                RuntimeJob.task_id == task_id,
            )
        ).scalar_one_or_none()
        if command_job is None:
            raise ArtifactManifestServiceError(
                error_code="RUNNER_ARTIFACT_BINDING_INVALID",
                message="artifact.manifest runtime job binding not found for tenant/runner/task.",
            )
        if str(command_job.job_type or "").strip().lower() != "tool.command":
            raise ArtifactManifestServiceError(
                error_code="RUNNER_ARTIFACT_BINDING_INVALID",
                message="artifact.manifest runtime_job_id must reference a tool.command runtime job.",
            )

        command_payload = command_job.payload_json if isinstance(command_job.payload_json, Mapping) else {}
        payload_command_id = _normalize_optional_text(command_payload.get("command_id"))
        payload_workspace_id = _normalize_optional_text(command_payload.get("workspace_id"))
        payload_task_runtime_job_id = _normalize_optional_text(command_payload.get("task_runtime_job_id"))

        if payload_command_id != payload.command_id:
            raise ArtifactManifestServiceError(
                error_code="RUNNER_ARTIFACT_COMMAND_ID_MISMATCH",
                message="artifact.manifest command_id does not match bound tool.command runtime job.",
            )
        if payload_workspace_id != payload.workspace_id:
            raise ArtifactManifestServiceError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message="artifact.manifest workspace_id does not match bound tool.command runtime job.",
            )
        if payload_task_runtime_job_id != payload.task_runtime_job_id:
            raise ArtifactManifestServiceError(
                error_code="RUNNER_ARTIFACT_TASK_RUNTIME_MISMATCH",
                message="artifact.manifest task_runtime_job_id does not match bound tool.command runtime job.",
            )

        try:
            task_runtime_job_uuid = UUID(payload.task_runtime_job_id)
        except ValueError as exc:
            raise ArtifactManifestServiceError(
                error_code="RUNNER_ARTIFACT_BINDING_INVALID",
                message="artifact.manifest task_runtime_job_id must be a UUID.",
            ) from exc

        task_runtime_job = self._db.execute(
            select(RuntimeJob).where(
                RuntimeJob.id == task_runtime_job_uuid,
                RuntimeJob.tenant_id == tenant_id,
                RuntimeJob.runner_id == runner_id,
                RuntimeJob.task_id == task_id,
            )
        ).scalar_one_or_none()
        if task_runtime_job is None:
            raise ArtifactManifestServiceError(
                error_code="RUNNER_ARTIFACT_BINDING_INVALID",
                message="artifact.manifest task_runtime_job_id binding not found for tenant/runner/task.",
            )
        if str(task_runtime_job.job_type or "").strip().lower() != "task.start":
            raise ArtifactManifestServiceError(
                error_code="RUNNER_ARTIFACT_BINDING_INVALID",
                message="artifact.manifest task_runtime_job_id must reference a task.start runtime job.",
            )

        task_payload = task_runtime_job.payload_json if isinstance(task_runtime_job.payload_json, Mapping) else {}
        task_workspace_id = _normalize_optional_text(task_payload.get("workspace_id"))
        if task_workspace_id != payload.workspace_id:
            raise ArtifactManifestServiceError(
                error_code="RUNNER_WORKSPACE_MISMATCH",
                message="artifact.manifest workspace_id does not match bound task runtime job.",
            )

        return _ManifestRuntimeBinding(
            command_runtime_job=command_job,
            task_runtime_job=task_runtime_job,
            command_payload=dict(command_payload),
        )

    def _find_or_create_skeletal_execution(
        self,
        *,
        tenant_id: int,
        task_id: int,
        runner_id: UUID,
        runtime_job_id: UUID,
        payload: RunnerArtifactManifestPayload,
        runtime_binding: _ManifestRuntimeBinding,
        message_id: str,
    ) -> ToolExecution:
        execution = self._execution_repository.get_by_runtime_binding(
            tenant_id=tenant_id,
            task_id=task_id,
            runtime_job_id=runtime_job_id,
            command_id=payload.command_id,
        )
        if execution is None:
            execution = self._execution_repository.get_by_task_tool_call_id(
                tenant_id=tenant_id,
                task_id=task_id,
                tool_call_id=payload.tool_call_id,
            )

        command_payload = runtime_binding.command_payload
        tool_name = _normalize_optional_text(command_payload.get("tool")) or "runner.tool_command"
        tool_arguments = command_payload.get("args") if isinstance(command_payload.get("args"), Mapping) else {}
        agent_path = _normalize_optional_text(command_payload.get("agent_path")) or "runner.tool_command"
        started_at = runtime_binding.command_runtime_job.created_at or _utcnow()

        metadata_patch = {
            "runner_manifest": {
                "skeletal": True,
                "message_id": message_id,
                "workspace_id": payload.workspace_id,
                "command_id": payload.command_id,
                "task_runtime_job_id": payload.task_runtime_job_id,
                "ingested_at": _utcnow().isoformat(),
            }
        }

        if execution is None:
            execution = self._execution_repository.create(
                tenant_id=tenant_id,
                task_id=task_id,
                runtime_job_id=runtime_job_id,
                runner_id=runner_id,
                execution_site_id=runtime_binding.command_runtime_job.execution_site_id,
                command_id=payload.command_id,
                workspace_id=payload.workspace_id,
                tool_name=tool_name,
                tool_arguments=dict(tool_arguments),
                agent_path=agent_path,
                status="pending",
                started_at=started_at,
                tool_call_id=payload.tool_call_id,
                execution_transport="runner_control_channel",
                execution_metadata=metadata_patch,
            )
            return execution

        execution.runtime_job_id = execution.runtime_job_id or runtime_job_id
        execution.runner_id = execution.runner_id or runner_id
        execution.execution_site_id = execution.execution_site_id or runtime_binding.command_runtime_job.execution_site_id
        execution.command_id = execution.command_id or payload.command_id
        execution.workspace_id = execution.workspace_id or payload.workspace_id
        execution.execution_transport = execution.execution_transport or "runner_control_channel"
        execution.tool_call_id = execution.tool_call_id or payload.tool_call_id
        execution.execution_metadata = _merge_json_dicts(execution.execution_metadata, metadata_patch)
        self._db.flush()
        self._db.refresh(execution)
        return execution

    def _find_or_create_manifest_row(
        self,
        *,
        tenant_id: int,
        task_id: int,
        runtime_job_id: UUID,
        runner_id: UUID,
        payload: RunnerArtifactManifestPayload,
        message_id: str,
    ) -> ArtifactManifest:
        manifest = self._db.execute(
            select(ArtifactManifest).where(
                ArtifactManifest.tenant_id == tenant_id,
                ArtifactManifest.task_id == task_id,
                ArtifactManifest.runtime_job_id == runtime_job_id,
                ArtifactManifest.runner_id == runner_id,
                ArtifactManifest.command_id == payload.command_id,
                ArtifactManifest.workspace_id == payload.workspace_id,
                ArtifactManifest.message_id == message_id,
            )
        ).scalar_one_or_none()

        payload_json = _manifest_payload_to_json(payload)
        idempotency_key = f"{tenant_id}:{runner_id}:{message_id}"
        if manifest is None:
            manifest = ArtifactManifest(
                tenant_id=tenant_id,
                task_id=task_id,
                runtime_job_id=runtime_job_id,
                runner_id=runner_id,
                command_id=payload.command_id,
                workspace_id=payload.workspace_id,
                message_id=message_id,
                idempotency_key=idempotency_key,
                status="accepted",
                manifest_json=payload_json,
                manifest_metadata={
                    "source": "artifact.manifest",
                    "ingested_at": _utcnow().isoformat(),
                },
            )
            self._db.add(manifest)
            self._db.flush()
            self._db.refresh(manifest)
            return manifest

        manifest.idempotency_key = manifest.idempotency_key or idempotency_key
        manifest.manifest_json = payload_json
        manifest.status = "accepted"
        manifest.manifest_metadata = _merge_json_dicts(
            manifest.manifest_metadata,
            {"source": "artifact.manifest", "updated_at": _utcnow().isoformat()},
        )
        self._db.flush()
        self._db.refresh(manifest)
        return manifest

    def _create_or_update_artifact_placeholders(
        self,
        *,
        tenant_id: int,
        task_id: int,
        runner_id: UUID,
        runtime_job_id: UUID,
        payload: RunnerArtifactManifestPayload,
        manifest: ArtifactManifest,
        execution: ToolExecution,
    ) -> tuple[tuple[RunnerArtifactUploadRequestItem, ...], tuple[_ManifestRejection, ...]]:
        rejections: list[_ManifestRejection] = []
        uploads: list[RunnerArtifactUploadRequestItem] = []
        seen_client_ids: set[str] = set()

        existing_artifacts = self._artifact_repository.get_by_manifest(manifest.id)
        artifacts_by_client_id = {
            _normalize_optional_text(_artifact_client_id_from_metadata(item.artifact_metadata) or ""): item
            for item in existing_artifacts
            if _normalize_optional_text(_artifact_client_id_from_metadata(item.artifact_metadata) or "")
        }

        for index, artifact_item in enumerate(payload.artifacts):
            rejection = self._reject_manifest_item_if_needed(
                index=index,
                item=artifact_item,
                seen_client_ids=seen_client_ids,
            )
            if rejection is not None:
                rejections.append(rejection)
                continue

            server_artifact = artifacts_by_client_id.get(artifact_item.artifact_client_id)
            if server_artifact is None:
                artifact_id = uuid4()
                object_key = self._build_server_object_key(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    execution_id=execution.id,
                    artifact_id=artifact_id,
                    relative_path=artifact_item.relative_path,
                )
                metadata = _merge_json_dicts(
                    artifact_item.metadata,
                    {
                        "artifact_client_id": artifact_item.artifact_client_id,
                        "manifest_message_id": manifest.message_id,
                    },
                )
                server_artifact = self._artifact_repository.create_batch(
                    [
                        {
                            "id": artifact_id,
                            "execution_id": execution.id,
                            "manifest_id": manifest.id,
                            "tenant_id": tenant_id,
                            "task_id": task_id,
                            "runtime_job_id": runtime_job_id,
                            "runner_id": runner_id,
                            "command_id": payload.command_id,
                            "artifact_kind": artifact_item.artifact_kind,
                            "relative_path": artifact_item.relative_path,
                            "source_path": f"/workspace/{artifact_item.relative_path.lstrip('/')}",
                            "object_key": object_key,
                            "storage_backend": self._data_plane_config.object_store_backend,
                            "upload_status": "upload_pending",
                            "content_sha256": artifact_item.content_sha256.lower(),
                            "byte_size": int(artifact_item.size_bytes),
                            "mime_type": artifact_item.content_type,
                            "is_text": bool(artifact_item.is_text),
                            "artifact_metadata": metadata,
                        }
                    ]
                )[0]
                artifacts_by_client_id[artifact_item.artifact_client_id] = server_artifact
            else:
                server_artifact.execution_id = execution.id
                server_artifact.manifest_id = manifest.id
                server_artifact.tenant_id = tenant_id
                server_artifact.task_id = task_id
                server_artifact.runtime_job_id = runtime_job_id
                server_artifact.runner_id = runner_id
                server_artifact.command_id = payload.command_id
                server_artifact.artifact_kind = artifact_item.artifact_kind
                server_artifact.relative_path = artifact_item.relative_path
                server_artifact.content_sha256 = artifact_item.content_sha256.lower()
                server_artifact.byte_size = int(artifact_item.size_bytes)
                server_artifact.mime_type = artifact_item.content_type
                server_artifact.is_text = bool(artifact_item.is_text)
                server_artifact.storage_backend = self._data_plane_config.object_store_backend
                if not str(server_artifact.object_key or "").strip():
                    server_artifact.object_key = self._build_server_object_key(
                        tenant_id=tenant_id,
                        task_id=task_id,
                        execution_id=execution.id,
                        artifact_id=server_artifact.id,
                        relative_path=artifact_item.relative_path,
                    )
                merged_metadata = _merge_json_dicts(
                    server_artifact.artifact_metadata,
                    {
                        "artifact_client_id": artifact_item.artifact_client_id,
                        "manifest_message_id": manifest.message_id,
                    },
                )
                server_artifact.artifact_metadata = _merge_json_dicts(merged_metadata, artifact_item.metadata)

            try:
                signed_target = self._object_store.create_signed_upload(
                    str(server_artifact.object_key or ""),
                    content_type=artifact_item.content_type,
                    metadata={
                        "tenant_id": str(tenant_id),
                        "task_id": str(task_id),
                        "artifact_id": str(server_artifact.id),
                    },
                )
            except Exception as exc:
                server_artifact.upload_status = "upload_failed"
                server_artifact.artifact_metadata = _merge_json_dicts(
                    server_artifact.artifact_metadata,
                    {
                        "upload_error": {
                            "error_code": "UPLOAD_INSTRUCTION_FAILED",
                            "message": str(exc),
                        }
                    },
                )
                rejections.append(
                    _ManifestRejection(
                        index=index,
                        artifact_client_id=artifact_item.artifact_client_id,
                        relative_path=artifact_item.relative_path,
                        error_code="UPLOAD_INSTRUCTION_FAILED",
                        reason="Unable to create signed upload target for artifact.",
                    )
                )
                continue

            server_artifact.object_key = signed_target.object_key
            server_artifact.upload_status = "upload_pending"
            uploads.append(
                RunnerArtifactUploadRequestItem(
                    artifact_id=str(server_artifact.id),
                    artifact_client_id=artifact_item.artifact_client_id,
                    object_key=signed_target.object_key,
                    upload_url=signed_target.url,
                    upload_method=signed_target.method,
                    upload_headers={str(key): str(value) for key, value in signed_target.headers.items()},
                    size_bytes=int(server_artifact.byte_size or artifact_item.size_bytes),
                    content_sha256=str(server_artifact.content_sha256 or artifact_item.content_sha256).lower(),
                    content_type=str(server_artifact.mime_type or artifact_item.content_type),
                    is_text=bool(server_artifact.is_text),
                )
            )

        self._db.flush()
        return tuple(uploads), tuple(rejections)

    def _reject_manifest_item_if_needed(
        self,
        *,
        index: int,
        item: RunnerArtifactManifestItem,
        seen_client_ids: set[str],
    ) -> _ManifestRejection | None:
        if item.artifact_client_id in seen_client_ids:
            return _ManifestRejection(
                index=index,
                artifact_client_id=item.artifact_client_id,
                relative_path=item.relative_path,
                error_code="RUNNER_ARTIFACT_DUPLICATE_CLIENT_ID",
                reason="artifact_client_id is duplicated inside the same manifest payload.",
            )
        seen_client_ids.add(item.artifact_client_id)

        if int(item.size_bytes) > int(self._data_plane_config.max_artifact_size_bytes):
            return _ManifestRejection(
                index=index,
                artifact_client_id=item.artifact_client_id,
                relative_path=item.relative_path,
                error_code="RUNNER_ARTIFACT_ITEM_TOO_LARGE",
                reason=(
                    "artifact size exceeds configured tenant data-plane limit "
                    f"({self._data_plane_config.max_artifact_size_bytes} bytes)."
                ),
            )
        return None

    def _build_server_object_key(
        self,
        *,
        tenant_id: int,
        task_id: int,
        execution_id: UUID,
        artifact_id: UUID,
        relative_path: str,
    ) -> str:
        key = build_artifact_object_key(
            tenant_id=tenant_id,
            task_id=task_id,
            execution_id=execution_id,
            artifact_id=artifact_id,
            filename=relative_path,
        )
        prefix = str(self._data_plane_config.object_store_prefix or "").strip("/")
        if not prefix:
            return key
        return f"{prefix}/{key}"

    def _update_manifest_summary(
        self,
        *,
        manifest: ArtifactManifest,
        payload: RunnerArtifactManifestPayload,
        execution: ToolExecution,
        uploads: tuple[RunnerArtifactUploadRequestItem, ...],
        rejections: tuple[_ManifestRejection, ...],
    ) -> None:
        upload_summaries = [
            {
                "artifact_id": item.artifact_id,
                "artifact_client_id": item.artifact_client_id,
                "object_key": item.object_key,
            }
            for item in uploads
        ]
        manifest.manifest_json = _manifest_payload_to_json(payload)
        manifest.manifest_metadata = _merge_json_dicts(
            manifest.manifest_metadata,
            {
                "execution_id": str(execution.id),
                "accepted_item_count": len(uploads),
                "rejected_item_count": len(rejections),
                "accepted_items": upload_summaries,
                "rejected_items": [item.to_json() for item in rejections],
                "updated_at": _utcnow().isoformat(),
            },
        )
        if uploads:
            manifest.status = "accepted"
        elif rejections:
            manifest.status = "failed"

    def _build_upload_request_envelope(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        runtime_job_id: UUID,
        payload: RunnerArtifactManifestPayload,
        correlation_id: str | None,
        manifest_id: UUID,
        uploads: tuple[RunnerArtifactUploadRequestItem, ...],
    ) -> RunnerEnvelope | None:
        if not uploads:
            return None

        request_payload = RunnerArtifactUploadRequestPayload(
            task_runtime_job_id=payload.task_runtime_job_id,
            command_id=payload.command_id,
            workspace_id=payload.workspace_id,
            tool_call_id=payload.tool_call_id,
            tool_batch_id=payload.tool_batch_id,
            uploads=uploads,
        )
        return RunnerEnvelope(
            message_id=f"data-plane-artifact-upload-request-{manifest_id}-{uuid4().hex[:10]}",
            message_type=RunnerMessageType.ARTIFACT_UPLOAD_REQUEST,
            schema_version=RUNNER_PROTOCOL_DATA_PLANE_VERSION,
            tenant_id=str(tenant_id),
            runner_id=str(runner_id),
            correlation_id=correlation_id,
            runtime_job_id=str(runtime_job_id),
            task_id=task_id,
            created_at=_utcnow().isoformat(),
            payload=request_payload,
            raw_message_type=RunnerMessageType.ARTIFACT_UPLOAD_REQUEST.value,
        )

    def _validate_upload_complete_identity(
        self,
        *,
        tenant_id: int,
        runner_id: UUID,
        task_id: int,
        runtime_job_id: UUID,
        payload: object,
    ) -> None:
        if not isinstance(payload, RunnerArtifactUploadCompletePayload):
            raise ArtifactManifestServiceError(
                error_code="RUNNER_PROTOCOL_INVALID",
                message="artifact.upload.complete requires a typed payload.",
            )
        if not payload.uploads:
            raise ArtifactManifestServiceError(
                error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                message="artifact.upload.complete uploads must not be empty.",
            )

        artifact_ids = _parse_artifact_ids(payload.uploads)
        artifacts = self._db.execute(
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
                ArtifactManifest.status == "accepted",
                ArtifactManifest.tenant_id == tenant_id,
                ArtifactManifest.task_id == task_id,
                ArtifactManifest.runtime_job_id == runtime_job_id,
                ArtifactManifest.runner_id == runner_id,
                ArtifactManifest.command_id == payload.command_id,
                ArtifactManifest.workspace_id == payload.workspace_id,
            )
        ).all()
        bindings_by_artifact_id = {str(artifact.id): (artifact, manifest) for artifact, manifest in artifacts}

        for upload in payload.uploads:
            binding = bindings_by_artifact_id.get(upload.artifact_id)
            if binding is None:
                raise ArtifactManifestServiceError(
                    error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                    message="artifact.upload.complete references an unaccepted artifact identity.",
                )
            artifact, manifest = binding
            if artifact.object_key != upload.object_key:
                raise ArtifactManifestServiceError(
                    error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                    message="artifact.upload.complete object key does not match accepted artifact identity.",
                )
            if str(artifact.content_sha256 or "").strip().lower() != upload.content_sha256.lower():
                raise ArtifactManifestServiceError(
                    error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                    message="artifact.upload.complete content hash does not match accepted artifact identity.",
                )
            if int(artifact.byte_size or 0) != int(upload.size_bytes):
                raise ArtifactManifestServiceError(
                    error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                    message="artifact.upload.complete size does not match accepted artifact identity.",
                )
            metadata = artifact.artifact_metadata if isinstance(artifact.artifact_metadata, dict) else {}
            expected_client_id = str(metadata.get("artifact_client_id") or "").strip()
            if expected_client_id and expected_client_id != upload.artifact_client_id:
                raise ArtifactManifestServiceError(
                    error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                    message="artifact.upload.complete client artifact id does not match accepted artifact identity.",
                )
            if str(manifest.workspace_id or "").strip() != payload.workspace_id:
                raise ArtifactManifestServiceError(
                    error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                    message="artifact.upload.complete workspace binding mismatch.",
                )


def _parse_artifact_ids(uploads: Iterable[object]) -> tuple[UUID, ...]:
    parsed_ids: list[UUID] = []
    for upload in uploads:
        artifact_id = getattr(upload, "artifact_id", None)
        normalized = str(artifact_id or "").strip()
        try:
            parsed_ids.append(UUID(normalized))
        except ValueError as exc:
            raise ArtifactManifestServiceError(
                error_code="RUNNER_ARTIFACT_UPLOAD_NOT_ACCEPTED",
                message="artifact.upload.complete artifact_id must be a UUID assigned by cloud.",
            ) from exc
    return tuple(parsed_ids)


def _artifact_client_id_from_metadata(metadata: object) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    candidate = metadata.get("artifact_client_id")
    normalized = _normalize_optional_text(candidate)
    return normalized


def _manifest_payload_to_json(payload: RunnerArtifactManifestPayload) -> dict[str, Any]:
    return {
        "task_runtime_job_id": payload.task_runtime_job_id,
        "command_id": payload.command_id,
        "workspace_id": payload.workspace_id,
        "tool_call_id": payload.tool_call_id,
        "tool_batch_id": payload.tool_batch_id,
        "artifacts": [
            {
                "artifact_client_id": item.artifact_client_id,
                "relative_path": item.relative_path,
                "artifact_kind": item.artifact_kind,
                "size_bytes": int(item.size_bytes),
                "content_sha256": item.content_sha256,
                "content_type": item.content_type,
                "is_text": bool(item.is_text),
                "created_at": item.created_at,
                "metadata": dict(item.metadata),
            }
            for item in payload.artifacts
        ],
    }


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


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _utcnow() -> datetime:
    return datetime.now(UTC)
