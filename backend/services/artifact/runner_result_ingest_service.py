"""Runner tool-result ingest service for Data Plane provenance promotion.

Scope:
- Consumes validated `tool.result` payloads with runtime-job binding context.
- Resolves or creates the canonical `ToolExecution` row, updates terminal
  execution fields, preserves semantic metadata, and links manifest artifacts.
- Persists bounded command/stdout/stderr artifacts using inline text for small
  payloads and object-backed storage for large text payloads.

Boundaries:
- Assumes runner envelope authentication/binding has already happened.
- Does not manage runtime-job state transitions or channel acknowledgements.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import hashlib
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.config.data_plane import DataPlaneConfig, get_data_plane_config
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.runner_control import RuntimeJob
from backend.repositories.execution_artifact_repository import ExecutionArtifactRepository
from backend.repositories.tool_execution_repository import ToolExecutionRepository
from backend.services.data_plane.object_key_builder import build_artifact_object_key
from backend.services.data_plane.object_store import ObjectStore
from backend.services.data_plane.registry import get_object_store
from runtime_shared.durable_secret_masking import mask_durable_secrets
from runtime_shared.runner_protocol import RunnerToolResultPayload

_INLINE_TEXT_MAX_BYTES = 1024 * 1024


class RunnerResultIngestService:
    """Persist validated runner tool-result payloads into provenance rows."""

    def __init__(
        self,
        db: Session,
        *,
        object_store: ObjectStore | None = None,
        data_plane_config: DataPlaneConfig | None = None,
        execution_repository: ToolExecutionRepository | None = None,
        artifact_repository: ExecutionArtifactRepository | None = None,
        inline_text_max_bytes: int = _INLINE_TEXT_MAX_BYTES,
    ) -> None:
        self._db = db
        self._object_store = object_store or get_object_store()
        self._data_plane_config = data_plane_config or get_data_plane_config()
        self._execution_repository = execution_repository or ToolExecutionRepository(db)
        self._artifact_repository = artifact_repository or ExecutionArtifactRepository(db)
        self._inline_text_max_bytes = max(1, int(inline_text_max_bytes))

    def ingest_tool_result(
        self,
        *,
        tenant_id: int,
        runtime_job: RuntimeJob,
        payload: RunnerToolResultPayload,
        runtime_job_status: str | None = None,
    ) -> ToolExecution:
        """Upsert one tool execution and reconcile manifest/output artifact rows."""

        if runtime_job.task_id is None:
            raise ValueError("runtime_job.task_id is required for tool-result ingest")

        task_id = int(runtime_job.task_id)
        now = _utcnow()
        command_payload = runtime_job.payload_json if isinstance(runtime_job.payload_json, Mapping) else {}
        workspace_id = _normalize_optional_text(payload.metadata.get("workspace_id")) or _normalize_optional_text(
            command_payload.get("workspace_id")
        )
        tool_call_id = _resolve_tool_call_id(payload=payload, runtime_job=runtime_job)

        execution = self._resolve_execution(
            tenant_id=tenant_id,
            task_id=task_id,
            runtime_job=runtime_job,
            command_id=payload.command_id,
            tool_call_id=tool_call_id,
        )
        if execution is None:
            tool_name = _normalize_optional_text(payload.tool) or _normalize_optional_text(command_payload.get("tool"))
            agent_path = _normalize_optional_text(command_payload.get("agent_path")) or "runner.tool_result"
            tool_arguments = command_payload.get("args") if isinstance(command_payload.get("args"), Mapping) else {}
            started_at = runtime_job.created_at or now
            execution = self._execution_repository.create(
                tenant_id=tenant_id,
                task_id=task_id,
                runtime_job_id=runtime_job.id,
                runner_id=runtime_job.runner_id,
                execution_site_id=runtime_job.execution_site_id,
                command_id=payload.command_id,
                workspace_id=workspace_id,
                tool_name=tool_name or "runner.tool_result",
                tool_arguments=dict(tool_arguments),
                agent_path=agent_path,
                status="pending",
                started_at=started_at,
                tool_call_id=tool_call_id,
                execution_transport="runner_control_channel",
                execution_metadata={},
            )

        semantic_metadata = _mask_mapping(
            _extract_tool_result_semantic_fields(payload=payload, runtime_job=runtime_job),
            source="runner_result_semantic_metadata",
        )
        metadata_patch: dict[str, Any] = {
            "runner_tool_result": {
                "operation_id": payload.operation_id,
                "command_id": payload.command_id,
                "tool": payload.tool,
                "status": payload.status,
                "success": payload.success,
                "exit_code": payload.exit_code,
                "error_code": payload.error_code,
                "error_message": mask_durable_secrets(
                    payload.error_message,
                    source="runner_result_error_message",
                ),
                "artifact_paths": list(payload.artifacts),
                "runtime_job_id": str(runtime_job.id),
                "updated_at": now.isoformat(),
            }
        }
        if semantic_metadata:
            metadata_patch.update(semantic_metadata)
            metadata_patch["semantic_snapshot"] = semantic_metadata

        execution.runtime_job_id = execution.runtime_job_id or runtime_job.id
        execution.runner_id = execution.runner_id or runtime_job.runner_id
        execution.execution_site_id = execution.execution_site_id or runtime_job.execution_site_id
        execution.command_id = execution.command_id or payload.command_id
        execution.tool_call_id = execution.tool_call_id or tool_call_id
        if workspace_id and not str(execution.workspace_id or "").strip():
            execution.workspace_id = workspace_id
        execution.status = _normalize_terminal_status(runtime_job_status=runtime_job_status, payload_status=payload.status)
        execution.exit_code = int(payload.exit_code)
        execution.finished_at = now
        execution.duration_ms = _compute_duration_ms(started_at=execution.started_at, finished_at=now)
        execution.execution_transport = execution.execution_transport or "runner_control_channel"
        execution.execution_metadata = _merge_json_dicts(
            execution.execution_metadata,
            _mask_mapping(metadata_patch, source="runner_result_execution_metadata"),
        )
        self._db.flush()

        command_text = _extract_display_command(runtime_job=runtime_job, tool_result=payload)
        self._upsert_output_artifact(
            execution=execution,
            runtime_job=runtime_job,
            artifact_kind="command",
            content=command_text,
        )
        self._upsert_output_artifact(
            execution=execution,
            runtime_job=runtime_job,
            artifact_kind="stdout",
            content=payload.stdout,
        )
        self._upsert_output_artifact(
            execution=execution,
            runtime_job=runtime_job,
            artifact_kind="stderr",
            content=payload.stderr,
        )

        self._link_manifest_artifacts(
            execution=execution,
            tenant_id=tenant_id,
            task_id=task_id,
            runtime_job_id=runtime_job.id,
            command_id=payload.command_id,
        )
        self._db.flush()
        self._db.refresh(execution)
        return execution

    def _resolve_execution(
        self,
        *,
        tenant_id: int,
        task_id: int,
        runtime_job: RuntimeJob,
        command_id: str,
        tool_call_id: str | None,
    ) -> ToolExecution | None:
        execution = self._execution_repository.get_by_runtime_binding(
            tenant_id=tenant_id,
            task_id=task_id,
            runtime_job_id=runtime_job.id,
            command_id=command_id,
        )
        if execution is not None:
            return execution

        normalized_command_id = _normalize_optional_text(command_id)
        if normalized_command_id:
            execution = (
                self._db.execute(
                    select(ToolExecution)
                    .where(
                        ToolExecution.tenant_id == int(tenant_id),
                        ToolExecution.task_id == int(task_id),
                        ToolExecution.command_id == normalized_command_id,
                    )
                    .order_by(ToolExecution.created_at.asc())
                )
                .scalars()
                .first()
            )
            if execution is not None:
                return execution

        if tool_call_id:
            return self._execution_repository.get_by_task_tool_call_id(
                tenant_id=tenant_id,
                task_id=task_id,
                tool_call_id=tool_call_id,
            )
        return None

    def _upsert_output_artifact(
        self,
        *,
        execution: ToolExecution,
        runtime_job: RuntimeJob,
        artifact_kind: str,
        content: str,
    ) -> None:
        if not content:
            return

        existing = (
            self._db.execute(
                select(ExecutionArtifact)
                .where(
                    ExecutionArtifact.execution_id == execution.id,
                    ExecutionArtifact.artifact_kind == artifact_kind,
                )
                .order_by(ExecutionArtifact.created_at.asc())
            )
            .scalars()
            .first()
        )

        if existing is None:
            artifact_id = uuid4()
            storage_fields = self._build_text_storage_fields(
                content=content,
                artifact_id=artifact_id,
                execution=execution,
                artifact_kind=artifact_kind,
            )
            row = {
                "id": artifact_id,
                "execution_id": execution.id,
                "tenant_id": int(execution.tenant_id),
                "task_id": int(execution.task_id),
                "runtime_job_id": runtime_job.id,
                "runner_id": runtime_job.runner_id,
                "command_id": execution.command_id,
                "artifact_kind": artifact_kind,
                "is_text": True,
                "mime_type": "text/plain; charset=utf-8",
                "artifact_metadata": {
                    "source": "runner.tool_result",
                    "artifact_kind": artifact_kind,
                    "truncated": storage_fields.get("truncated", False),
                    "object_backed": bool(storage_fields.get("object_key")),
                },
            }
            row.update(
                {
                    "content_text": storage_fields["content_text"],
                    "object_key": storage_fields["object_key"],
                    "storage_backend": storage_fields["storage_backend"],
                    "upload_status": storage_fields["upload_status"],
                    "byte_size": storage_fields["byte_size"],
                    "content_sha256": storage_fields["content_sha256"],
                }
            )
            self._artifact_repository.create_batch([row])
            return

        existing.execution_id = execution.id
        existing.tenant_id = int(execution.tenant_id)
        existing.task_id = int(execution.task_id)
        existing.runtime_job_id = runtime_job.id
        existing.runner_id = runtime_job.runner_id
        existing.command_id = execution.command_id
        existing.is_text = True
        existing.mime_type = existing.mime_type or "text/plain; charset=utf-8"
        storage_fields = self._build_text_storage_fields(
            content=content,
            artifact_id=existing.id,
            execution=execution,
            artifact_kind=artifact_kind,
        )

        existing.content_text = storage_fields["content_text"]
        existing.byte_size = storage_fields["byte_size"]
        existing.content_sha256 = storage_fields["content_sha256"]

        if existing.relative_path is None:
            existing.object_key = storage_fields["object_key"]
            existing.storage_backend = storage_fields["storage_backend"]
            existing.upload_status = storage_fields["upload_status"]
        existing.artifact_metadata = _merge_json_dicts(
            existing.artifact_metadata,
            {
                "source": "runner.tool_result",
                "artifact_kind": artifact_kind,
                "updated_at": _utcnow().isoformat(),
                "truncated": storage_fields.get("truncated", False),
                "object_backed": bool(storage_fields.get("object_key")),
            },
        )
        self._db.flush()

    def _build_text_storage_fields(
        self,
        *,
        content: str,
        artifact_id: UUID,
        execution: ToolExecution,
        artifact_kind: str,
    ) -> dict[str, Any]:
        content = str(mask_durable_secrets(content, source=f"runner_result_artifact_{artifact_kind}"))
        data = content.encode("utf-8")
        byte_size = len(data)
        content_sha256 = hashlib.sha256(data).hexdigest()

        if byte_size <= self._inline_text_max_bytes:
            return {
                "content_text": content,
                "object_key": None,
                "storage_backend": None,
                "upload_status": "inline",
                "byte_size": byte_size,
                "content_sha256": content_sha256,
                "truncated": False,
            }

        object_key = build_artifact_object_key(
            tenant_id=execution.tenant_id,
            task_id=execution.task_id,
            execution_id=execution.id,
            artifact_id=artifact_id,
            filename=f"{artifact_kind}.txt",
        )
        try:
            self._object_store.put_bytes(
                object_key,
                data,
                content_type="text/plain; charset=utf-8",
                metadata={
                    "tenant_id": str(execution.tenant_id),
                    "task_id": str(execution.task_id),
                    "execution_id": str(execution.id),
                    "artifact_kind": artifact_kind,
                },
            )
            return {
                "content_text": None,
                "object_key": object_key,
                "storage_backend": self._data_plane_config.object_store_backend,
                "upload_status": "ready",
                "byte_size": byte_size,
                "content_sha256": content_sha256,
                "truncated": False,
            }
        except Exception:
            preview = data[: self._inline_text_max_bytes].decode("utf-8", errors="replace")
            return {
                "content_text": preview,
                "object_key": None,
                "storage_backend": None,
                "upload_status": "inline",
                "byte_size": byte_size,
                "content_sha256": content_sha256,
                "truncated": True,
            }

    def _link_manifest_artifacts(
        self,
        *,
        execution: ToolExecution,
        tenant_id: int,
        task_id: int,
        runtime_job_id: UUID,
        command_id: str,
    ) -> None:
        rows = (
            self._db.execute(
                select(ExecutionArtifact)
                .where(
                    ExecutionArtifact.tenant_id == int(tenant_id),
                    ExecutionArtifact.task_id == int(task_id),
                    ExecutionArtifact.runtime_job_id == runtime_job_id,
                    ExecutionArtifact.command_id == command_id,
                )
                .order_by(ExecutionArtifact.created_at.asc())
            )
            .scalars()
            .all()
        )
        for artifact in rows:
            if artifact.execution_id != execution.id:
                artifact.execution_id = execution.id
                artifact.tenant_id = int(tenant_id)
                artifact.task_id = int(task_id)


def _extract_display_command(
    *,
    runtime_job: RuntimeJob,
    tool_result: RunnerToolResultPayload | None = None,
) -> str:
    """Resolve the shell command shown in tool-card terminal raw output.

    Matches bound ``tool.command`` contracts end-to-end:
    - Tooling plane stores the prepared command on ``runtime_job.payload_json.command``.
    - Runner ``tool.result`` may echo the same value in ``metadata.command_text``.
    - Legacy jobs may only carry ``args.command``, ``params.command``, or structured tool args.
    """
    payload = runtime_job.payload_json if isinstance(runtime_job.payload_json, Mapping) else {}

    prepared_command = _normalize_optional_text(payload.get("command"))
    if prepared_command:
        return prepared_command

    if tool_result is not None and isinstance(tool_result.metadata, Mapping):
        metadata_command = _normalize_optional_text(tool_result.metadata.get("command_text"))
        if metadata_command:
            return metadata_command

    args = payload.get("args") if isinstance(payload.get("args"), Mapping) else {}
    args_command = _normalize_optional_text(args.get("command"))
    if args_command:
        return args_command

    params = payload.get("params") if isinstance(payload.get("params"), Mapping) else {}
    params_command = _normalize_optional_text(params.get("command"))
    if params_command:
        return params_command

    tool = _normalize_optional_text(payload.get("tool"))
    if tool:
        try:
            from agent.tools.utils import resolve_command_text_for_execution

            command_text = resolve_command_text_for_execution(tool, dict(args), None)
            if command_text:
                return command_text
        except Exception:
            pass
    return ""


def _resolve_tool_call_id(*, payload: RunnerToolResultPayload, runtime_job: RuntimeJob) -> str | None:
    metadata_tool_call_id = _normalize_optional_text(
        payload.metadata.get("tool_call_id") if isinstance(payload.metadata, Mapping) else None
    )
    if metadata_tool_call_id:
        return metadata_tool_call_id
    runtime_payload = runtime_job.payload_json if isinstance(runtime_job.payload_json, Mapping) else {}
    return _normalize_optional_text(runtime_payload.get("tool_call_id"))


def _extract_tool_result_semantic_fields(
    *,
    payload: RunnerToolResultPayload,
    runtime_job: RuntimeJob,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    metadata_mapping = payload.metadata if isinstance(payload.metadata, Mapping) else {}
    result_mapping = payload.result if isinstance(payload.result, Mapping) else {}
    command_payload = runtime_job.payload_json if isinstance(runtime_job.payload_json, Mapping) else {}
    command_metadata = command_payload.get("metadata")
    command_metadata_mapping = command_metadata if isinstance(command_metadata, Mapping) else {}

    for key in (
        "semantic_observations",
        "semantic_evidence",
        "semantic_schema_version",
        "capability_family",
        "tool_metadata",
    ):
        for source in (metadata_mapping, result_mapping, command_metadata_mapping):
            if key not in source:
                continue
            snapshot[key] = source[key]
            break
    return snapshot


def _normalize_terminal_status(*, runtime_job_status: str | None, payload_status: str | None) -> str:
    status = str(runtime_job_status or payload_status or "").strip().lower()
    return status or "failed"


def _normalize_optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _compute_duration_ms(*, started_at: datetime | None, finished_at: datetime) -> int | None:
    if started_at is None:
        return None
    normalized_started = started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=UTC)
    elapsed_ms = int((finished_at - normalized_started).total_seconds() * 1000)
    return max(0, elapsed_ms)


def _merge_json_dicts(base: object, patch: object) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if isinstance(base, Mapping):
        for key, value in base.items():
            merged[str(key)] = value
    if not isinstance(patch, Mapping):
        return merged
    for key, value in patch.items():
        normalized_key = str(key)
        existing = merged.get(normalized_key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[normalized_key] = _merge_json_dicts(existing, value)
        else:
            merged[normalized_key] = value
    return merged


def _mask_mapping(value: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    masked = mask_durable_secrets(dict(value), source=source)
    return masked if isinstance(masked, dict) else {}


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)
