"""
Artifact provenance service for tool execution lifecycle persistence.

This service orchestrates repository writes/reads for tool executions and
execution artifacts, including content thresholding, hash integrity, and
graceful degradation when persistence fails.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import uuid

from sqlalchemy.orm import Session
from sqlalchemy import select

from backend.config.feature_flags import is_artifact_provenance_enabled
from backend.core.time_utils import to_utc, utc_now
from backend.models.core import Task
from backend.models.provenance import ToolExecution
from backend.repositories.execution_artifact_repository import ExecutionArtifactRepository
from backend.repositories.tool_execution_repository import ToolExecutionRepository
from backend.services.runtime_provider.contracts import RuntimeActorType
from backend.services.runtime_provider.runtime_artifact_access import (
    decode_runtime_artifact_binary_delegate,
    execute_runtime_artifact_read_sync,
    normalize_runtime_artifact_relative_path,
)
from runtime_shared.durable_secret_masking import mask_durable_secrets

logger = logging.getLogger(__name__)

MAX_CONTENT_SIZE = 1024 * 1024  # 1 MB
_TEXTUAL_MIME_PREFIXES = ("text/",)
_TEXTUAL_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-sh",
    "application/yaml",
}


class ArtifactProvenanceService:
    """Business logic layer for execution/artifact provenance."""

    def __init__(
        self,
        db: Session,
        *,
        execution_repo: Optional[ToolExecutionRepository] = None,
        artifact_repo: Optional[ExecutionArtifactRepository] = None,
    ) -> None:
        self.db = db
        self.execution_repo = execution_repo or ToolExecutionRepository(db)
        self.artifact_repo = artifact_repo or ExecutionArtifactRepository(db)

    def record_tool_execution(
        self,
        *,
        task_id: int,
        tool_name: str,
        tool_arguments: Optional[Dict[str, Any]] = None,
        agent_path: str = "langgraph",
        status: str = "started",
        started_at: Optional[datetime] = None,
        chat_message_id: Optional[int] = None,
        tool_call_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        turn_sequence: Optional[int] = None,
        purpose: Optional[str] = None,
        execution_transport: Optional[str] = None,
        workspace_path: Optional[str] = None,
        container_path: Optional[str] = None,
        execution_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[ToolExecution]:
        """Persist start-of-execution metadata and commit transaction."""
        if not is_artifact_provenance_enabled():
            return None

        started = started_at or utc_now()
        try:
            logger.info(
                "record_tool_execution writing start row (task_id=%s tool_name=%s tool_call_id=%s turn_id=%s).",
                task_id,
                tool_name,
                tool_call_id,
                turn_id,
            )
            tenant_id = self._resolve_task_tenant_id(task_id)
            execution = self.execution_repo.create(
                task_id=task_id,
                tenant_id=tenant_id,
                chat_message_id=chat_message_id,
                tool_call_id=tool_call_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                turn_sequence=turn_sequence,
                tool_name=tool_name,
                tool_arguments=_mask_dict(tool_arguments or {}, source="provenance_tool_arguments"),
                purpose=purpose,
                agent_path=agent_path,
                execution_transport=execution_transport,
                workspace_path=workspace_path,
                container_path=container_path,
                status=status,
                started_at=started,
                execution_metadata=_mask_dict(
                    execution_metadata or {},
                    source="provenance_execution_metadata",
                ),
            )
            self.db.commit()
            logger.info(
                "record_tool_execution committed (execution_id=%s task_id=%s tool_name=%s).",
                execution.id,
                task_id,
                tool_name,
            )
            return execution
        except Exception:
            self.db.rollback()
            logger.exception(
                "Artifact provenance write failed in record_tool_execution; continuing without persistence."
            )
            return None

    def complete_tool_execution(
        self,
        *,
        execution_id: str | uuid.UUID,
        status: str,
        exit_code: Optional[int] = None,
        command_text: Optional[str] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        artifact_paths: Optional[List[str]] = None,
        workspace_path: Optional[str] = None,
        execution_metadata_patch: Optional[Dict[str, Any]] = None,
    ) -> Optional[ToolExecution]:
        """Persist completion metadata and produced artifacts atomically."""
        if not is_artifact_provenance_enabled():
            return None

        try:
            logger.info(
                "complete_tool_execution start (execution_id=%s status=%s artifact_paths=%s workspace_path=%s).",
                execution_id,
                status,
                len(artifact_paths or []),
                workspace_path,
            )
            execution = self.execution_repo.get_by_id(execution_id)
            if execution is None:
                raise ValueError(f"Execution {execution_id} not found")

            finished_at = utc_now()
            started_at = execution.started_at
            started_at = to_utc(started_at)
            duration_ms = int((finished_at - started_at).total_seconds() * 1000)

            updated = self.execution_repo.update_status(
                execution_id=execution.id,
                status=status,
                exit_code=exit_code,
                finished_at=finished_at,
                duration_ms=duration_ms,
                execution_metadata_patch=_mask_dict(
                    execution_metadata_patch or {},
                    source="provenance_execution_metadata_patch",
                ),
            )
            if updated is None:
                raise ValueError(f"Execution {execution_id} not found for status update")

            artifacts_data: List[Dict[str, Any]] = []
            if command_text:
                artifacts_data.append(
                    self._prepare_artifact_data_from_content(
                        execution_id=updated.id,
                        tenant_id=updated.tenant_id,
                        task_id=updated.task_id,
                        artifact_kind="command",
                        content=command_text,
                    )
                )
            if stdout:
                artifacts_data.append(
                    self._prepare_artifact_data_from_content(
                        execution_id=updated.id,
                        tenant_id=updated.tenant_id,
                        task_id=updated.task_id,
                        artifact_kind="stdout",
                        content=stdout,
                    )
                )
            if stderr:
                artifacts_data.append(
                    self._prepare_artifact_data_from_content(
                        execution_id=updated.id,
                        tenant_id=updated.tenant_id,
                        task_id=updated.task_id,
                        artifact_kind="stderr",
                        content=stderr,
                    )
                )

            if artifact_paths:
                for path_item in artifact_paths:
                    prepared = self._prepare_artifact_data_from_runtime_file(
                        execution_id=updated.id,
                        tenant_id=updated.tenant_id,
                        task_id=updated.task_id,
                        artifact_kind="tool_file",
                        relative_path=path_item,
                    )
                    if prepared is not None:
                        artifacts_data.append(prepared)

            if artifacts_data:
                self.artifact_repo.create_batch(artifacts_data)
                logger.info(
                    "complete_tool_execution wrote %s artifact rows (execution_id=%s).",
                    len(artifacts_data),
                    execution_id,
                )
            else:
                logger.info(
                    "complete_tool_execution wrote no artifact rows (execution_id=%s).",
                    execution_id,
                )

            self.db.commit()
            logger.info(
                "complete_tool_execution committed (execution_id=%s status=%s).",
                execution_id,
                status,
            )
            return updated
        except Exception:
            self.db.rollback()
            logger.exception(
                "Artifact provenance write failed in complete_tool_execution; continuing without persistence."
            )
            return None

    def get_execution_with_artifacts(
        self,
        execution_id: str | uuid.UUID,
        *,
        task_id: int,
    ) -> Optional[Dict[str, Any]]:
        """Return one task-scoped execution together with all linked artifacts."""
        scoped_task_id = self._require_task_scope(task_id)
        execution = self.execution_repo.get_by_id(execution_id)
        if execution is None:
            return None
        if int(execution.task_id) != scoped_task_id:
            return None
        artifacts = self.artifact_repo.get_by_execution(execution.id)
        return {
            "execution": execution,
            "artifacts": artifacts,
        }

    def get_task_executions(
        self,
        *,
        task_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ToolExecution]:
        """Return executions for one task (newest-first)."""
        return self.execution_repo.get_by_task(task_id=task_id, limit=limit, offset=offset)

    def get_conversation_executions(
        self,
        *,
        task_id: int,
        conversation_id: str,
        turn_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ToolExecution]:
        """Return executions for a task conversation (optionally one turn)."""
        return self.execution_repo.get_by_conversation_turn(
            task_id=task_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            limit=limit,
            offset=offset,
        )

    def _prepare_artifact_data_from_content(
        self,
        *,
        execution_id: uuid.UUID,
        tenant_id: int,
        task_id: int,
        artifact_kind: str,
        content: str,
    ) -> Dict[str, Any]:
        """Prepare artifact payload from in-memory text content."""
        content = str(mask_durable_secrets(content, source=f"provenance_artifact_{artifact_kind}"))
        content_bytes = content.encode("utf-8")
        byte_size = len(content_bytes)
        content_text = content if byte_size <= MAX_CONTENT_SIZE else None
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        return {
            "execution_id": execution_id,
            "tenant_id": int(tenant_id),
            "task_id": task_id,
            "artifact_kind": artifact_kind,
            "relative_path": None,
            "source_path": None,
            "fallback_path": None,
            "content_text": content_text,
            "content_sha256": content_hash,
            "byte_size": byte_size,
            "is_text": True,
            "mime_type": "text/plain",
            "artifact_metadata": {},
        }

    @staticmethod
    def _require_task_scope(task_id: int) -> int:
        try:
            scoped = int(task_id)
        except (TypeError, ValueError):
            raise ValueError("Valid task_id is required for provenance access") from None
        if scoped <= 0:
            raise ValueError("Valid task_id is required for provenance access")
        return scoped

    def _prepare_artifact_data_from_file(
        self,
        *,
        execution_id: uuid.UUID,
        task_id: int,
        artifact_kind: str,
        relative_path: str,
        workspace_path: str,
    ) -> Optional[Dict[str, Any]]:
        """Prepare artifact payload from a file path with workspace safety checks."""
        path_result = validate_artifact_path(workspace_path=workspace_path, candidate_path=relative_path)
        if path_result is None:
            logger.warning("Skipping artifact path '%s': invalid or inaccessible.", relative_path)
            return None

    def _prepare_artifact_data_from_runtime_file(
        self,
        *,
        execution_id: uuid.UUID,
        tenant_id: int,
        task_id: int,
        artifact_kind: str,
        relative_path: str,
    ) -> Optional[Dict[str, Any]]:
        """Prepare artifact payload from a provider-read runtime file."""
        data, source_path = self._read_runtime_artifact_file_bytes(
            task_id=task_id,
            path=relative_path,
        )
        if data is None:
            logger.warning("Skipping artifact path '%s': provider read failed.", relative_path)
            return None

        normalized_relative = normalize_runtime_artifact_relative_path(relative_path)
        mime_type, is_text = detect_content_type(normalized_relative or str(relative_path))
        content_hash = hashlib.sha256(data).hexdigest()
        byte_size = len(data)
        content_text = None
        if is_text and byte_size <= MAX_CONTENT_SIZE:
            content_text = str(
                mask_durable_secrets(
                    data.decode("utf-8", errors="ignore"),
                    source=f"provenance_runtime_artifact_{artifact_kind}",
                )
            )

        return {
            "execution_id": execution_id,
            "tenant_id": int(tenant_id),
            "task_id": task_id,
            "artifact_kind": artifact_kind,
            "relative_path": normalized_relative,
            "source_path": source_path or normalized_relative,
            "fallback_path": None,
            "content_text": content_text,
            "content_sha256": content_hash,
            "byte_size": byte_size,
            "mime_type": mime_type,
            "is_text": is_text,
            "artifact_metadata": {},
        }

    def _resolve_task_tenant_id(self, task_id: int) -> int:
        """Resolve tenant ownership for provenance writes from task context."""
        tenant_id = self.db.execute(
            select(Task.tenant_id).where(Task.id == int(task_id))
        ).scalar_one_or_none()
        if tenant_id is None:
            raise ValueError(f"Task {task_id} not found for tenant resolution")
        return int(tenant_id)

    def _read_runtime_artifact_file_bytes(
        self,
        *,
        task_id: int,
        path: str,
    ) -> tuple[bytes | None, str | None]:
        """Read a runtime-produced artifact through the provider boundary."""
        result = execute_runtime_artifact_read_sync(
            self.db,
            task_id=int(task_id),
            path=str(path),
            actor_type=RuntimeActorType.SYSTEM,
            actor_id="artifact_provenance",
            binary=True,
            log_context="artifact provenance runtime read",
        )
        return decode_runtime_artifact_binary_delegate(result, fallback_path=str(path))


def _mask_dict(value: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    masked = mask_durable_secrets(value, source=source)
    return masked if isinstance(masked, dict) else {}


def compute_file_hash(file_path: str) -> Tuple[str, int]:
    """Compute SHA256 hash from raw file bytes and return (hash, byte_size)."""
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with open(file_path, "rb") as file_handle:
            while True:
                chunk = file_handle.read(8192)
                if not chunk:
                    break
                digest.update(chunk)
                byte_size += len(chunk)
    except Exception:
        return "", 0
    return digest.hexdigest(), byte_size


def resolve_workspace_root(*, task_id: int, workspace_path: Optional[str]) -> Optional[str]:
    """Resolve provider-projected task workspace path without reconstructing it."""
    _ = task_id
    if not workspace_path:
        return None
    try:
        return str(Path(workspace_path).resolve())
    except Exception:
        logger.warning("Ignoring invalid provider workspace path for task %s: %s", task_id, workspace_path)
        return None


def validate_artifact_path(
    *,
    workspace_path: str,
    candidate_path: str,
) -> Optional[Tuple[Optional[str], Optional[str], Path, str]]:
    """
    Validate and resolve an artifact path.

    Returns:
        (source_path, fallback_path, resolved_path, normalized_relative_path)
    """
    workspace_abs = Path(workspace_path).resolve()
    normalized_candidate = candidate_path.replace("\\", "/")
    if normalized_candidate == "/workspace":
        return None
    if normalized_candidate.startswith("/workspace/"):
        normalized_candidate = normalized_candidate[len("/workspace/") :]
    raw_candidate = Path(normalized_candidate)

    if raw_candidate.is_absolute():
        resolved = raw_candidate.resolve()
    else:
        resolved = (workspace_abs / raw_candidate).resolve()

    if not resolved.exists() or not resolved.is_file():
        return None

    try:
        normalized_relative = resolved.relative_to(workspace_abs).as_posix()
        return str(resolved), None, resolved, normalized_relative
    except ValueError:
        logger.warning(
            "Rejecting artifact path outside workspace. candidate=%s resolved=%s workspace=%s",
            candidate_path,
            resolved,
            workspace_abs,
        )
        return None


def detect_content_type(file_path: str) -> Tuple[Optional[str], bool]:
    """Return MIME type guess and whether content should be treated as text."""
    mime_type, _encoding = mimetypes.guess_type(file_path)
    if mime_type is None:
        return None, False
    is_text = mime_type.startswith(_TEXTUAL_MIME_PREFIXES) or mime_type in _TEXTUAL_MIME_TYPES
    return mime_type, is_text


def read_artifact_content(*, file_path: str, is_text: bool, byte_size: int) -> Optional[str]:
    """Read text content when textual and within threshold; otherwise return None."""
    if not is_text or byte_size > MAX_CONTENT_SIZE:
        return None
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as file_handle:
            return file_handle.read()
    except Exception:
        return None
