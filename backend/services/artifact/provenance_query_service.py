"""
Query service for artifact provenance retrieval and API payload shaping.

This module provides read-oriented queries over `tool_executions` and
`execution_artifacts`, including pagination, filtering, and timeline views used
by the artifact provenance API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from backend.models.provenance import ExecutionArtifact, ToolExecution
from .catalog_labels import build_artifact_catalog_label_expression


class ArtifactProvenanceScopeError(ValueError):
    """Raised when provenance read APIs are called without valid task scope."""


class ArtifactProvenanceQueryService:
    """Read-focused service for execution and artifact provenance data."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_execution_by_id(
        self,
        execution_id: str | uuid.UUID,
        *,
        task_id: int,
        tenant_id: int | None = None,
        include_artifacts: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Return one execution by UUID constrained to one task."""
        scoped_task_id = self._require_task_scope(task_id)
        scoped_tenant_id = self._normalize_optional_tenant_scope(tenant_id)
        parsed_id = self._parse_uuid(execution_id)
        if parsed_id is None:
            return None

        query = self.db.query(ToolExecution)
        if scoped_tenant_id is not None:
            query = query.filter(ToolExecution.tenant_id == scoped_tenant_id)
        query = query.filter(
            ToolExecution.task_id == scoped_task_id,
            ToolExecution.id == parsed_id,
        )
        if include_artifacts:
            query = query.options(selectinload(ToolExecution.artifacts))

        execution = query.first()
        if execution is None:
            return None

        result: Dict[str, Any] = {
            "execution": self._serialize_execution(execution),
        }
        result["execution"]["raw_output"] = self._build_raw_output_state(execution.artifacts)
        if include_artifacts:
            result["artifacts"] = [
                self._serialize_artifact(artifact, include_content=False)
                for artifact in execution.artifacts
            ]
        return result

    def get_execution_by_tool_call_id(
        self,
        *,
        task_id: int,
        tenant_id: int | None = None,
        tool_call_id: str,
        include_artifacts: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Return one execution by task-scoped tool_call_id.

        Task scoping prevents collisions where identical tool_call_id values
        exist across different tasks.
        """
        scoped_tenant_id = self._normalize_optional_tenant_scope(tenant_id)
        query = self.db.query(ToolExecution)
        if scoped_tenant_id is not None:
            query = query.filter(ToolExecution.tenant_id == scoped_tenant_id)
        query = query.filter(
            ToolExecution.task_id == task_id,
            ToolExecution.tool_call_id == tool_call_id,
        )
        if include_artifacts:
            query = query.options(selectinload(ToolExecution.artifacts))

        execution = query.one_or_none()
        if execution is None:
            return None

        result: Dict[str, Any] = {
            "execution": self._serialize_execution(execution),
        }
        result["execution"]["raw_output"] = self._build_raw_output_state(execution.artifacts)
        if include_artifacts:
            result["artifacts"] = [
                self._serialize_artifact(artifact, include_content=False)
                for artifact in execution.artifacts
            ]
        return result

    def get_task_executions(
        self,
        *,
        task_id: int,
        tenant_id: int | None = None,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
        include_artifacts: bool = False,
    ) -> Dict[str, Any]:
        """Return paginated executions for one task with optional filters."""
        safe_limit, safe_offset = self._normalize_pagination(limit=limit, offset=offset)

        scoped_tenant_id = self._normalize_optional_tenant_scope(tenant_id)
        query = self.db.query(ToolExecution)
        if scoped_tenant_id is not None:
            query = query.filter(ToolExecution.tenant_id == scoped_tenant_id)
        query = query.filter(ToolExecution.task_id == task_id)

        if tool_name:
            query = query.filter(ToolExecution.tool_name == tool_name)
        if status:
            query = query.filter(ToolExecution.status == status)
        if start_time:
            query = query.filter(ToolExecution.started_at >= start_time)
        if end_time:
            query = query.filter(ToolExecution.started_at <= end_time)
        if include_artifacts:
            # Prevent N+1 queries when serializing artifacts.
            query = query.options(selectinload(ToolExecution.artifacts))

        total = query.count()
        executions = (
            query.order_by(ToolExecution.created_at.desc())
            .offset(safe_offset)
            .limit(safe_limit)
            .all()
        )

        return {
            "executions": [
                self._serialize_execution(execution, include_artifacts=include_artifacts)
                for execution in executions
            ],
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
        }

    def get_conversation_executions(
        self,
        *,
        task_id: int,
        tenant_id: int | None = None,
        conversation_id: str,
        turn_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        include_artifacts: bool = True,
    ) -> Dict[str, Any]:
        """Return paginated executions for one task conversation/turn."""
        safe_limit, safe_offset = self._normalize_pagination(limit=limit, offset=offset)

        scoped_tenant_id = self._normalize_optional_tenant_scope(tenant_id)
        query = self.db.query(ToolExecution)
        if scoped_tenant_id is not None:
            query = query.filter(ToolExecution.tenant_id == scoped_tenant_id)
        query = query.filter(
            ToolExecution.task_id == task_id,
            ToolExecution.conversation_id == conversation_id,
        )
        if turn_id is not None:
            query = query.filter(ToolExecution.turn_id == turn_id)
        if include_artifacts:
            query = query.options(selectinload(ToolExecution.artifacts))

        total = query.count()
        executions = (
            query.order_by(ToolExecution.created_at.desc())
            .offset(safe_offset)
            .limit(safe_limit)
            .all()
        )

        return {
            "executions": [
                self._serialize_execution(execution, include_artifacts=include_artifacts)
                for execution in executions
            ],
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
        }

    def get_tool_execution_timeline(
        self,
        task_id: int,
        tenant_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return chronological execution timeline for a task with artifact counts."""
        safe_limit, safe_offset = self._normalize_pagination(limit=limit, offset=offset)

        scoped_tenant_id = self._normalize_optional_tenant_scope(tenant_id)
        artifact_count_subquery = (
            self.db.query(
                ExecutionArtifact.execution_id.label("execution_id"),
                func.count(ExecutionArtifact.id).label("artifact_count"),
            )
            .filter(ExecutionArtifact.task_id == task_id)
            .group_by(ExecutionArtifact.execution_id)
            .subquery()
        )
        if scoped_tenant_id is not None:
            artifact_count_subquery = (
                self.db.query(
                    ExecutionArtifact.execution_id.label("execution_id"),
                    func.count(ExecutionArtifact.id).label("artifact_count"),
                )
                .filter(
                    ExecutionArtifact.tenant_id == scoped_tenant_id,
                    ExecutionArtifact.task_id == task_id,
                )
                .group_by(ExecutionArtifact.execution_id)
                .subquery()
            )

        query = (
            self.db.query(
                ToolExecution,
                func.coalesce(artifact_count_subquery.c.artifact_count, 0).label("artifact_count"),
            )
            .outerjoin(
                artifact_count_subquery,
                ToolExecution.id == artifact_count_subquery.c.execution_id,
            )
            .filter(ToolExecution.task_id == task_id)
        )
        if scoped_tenant_id is not None:
            query = query.filter(ToolExecution.tenant_id == scoped_tenant_id)

        total = query.count()
        rows = (
            query.order_by(ToolExecution.started_at.asc(), ToolExecution.created_at.asc())
            .offset(safe_offset)
            .limit(safe_limit)
            .all()
        )

        timeline = []
        for execution, artifact_count in rows:
            timeline.append(
                {
                    "execution_id": str(execution.id),
                    "task_id": execution.task_id,
                    "tool_call_id": execution.tool_call_id,
                    "conversation_id": execution.conversation_id,
                    "turn_id": execution.turn_id,
                    "turn_sequence": execution.turn_sequence,
                    "tool_name": execution.tool_name,
                    "status": execution.status,
                    "exit_code": execution.exit_code,
                    "started_at": self._serialize_datetime(execution.started_at),
                    "finished_at": self._serialize_datetime(execution.finished_at),
                    "duration_ms": execution.duration_ms,
                    "artifact_count": int(artifact_count or 0),
                }
            )

        return {
            "timeline": timeline,
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
        }

    def get_artifact_by_id(
        self,
        artifact_id: str | uuid.UUID,
        *,
        task_id: int,
        tenant_id: int | None = None,
        include_content: bool = False,
        include_internal_paths: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Return one artifact by UUID constrained to one task."""
        scoped_task_id = self._require_task_scope(task_id)
        scoped_tenant_id = self._normalize_optional_tenant_scope(tenant_id)
        parsed_id = self._parse_uuid(artifact_id)
        if parsed_id is None:
            return None

        query = self.db.query(ExecutionArtifact)
        if scoped_tenant_id is not None:
            query = query.filter(ExecutionArtifact.tenant_id == scoped_tenant_id)
        query = query.filter(
            ExecutionArtifact.task_id == scoped_task_id,
            ExecutionArtifact.id == parsed_id,
        )
        artifact = query.first()
        if artifact is None:
            return None
        return self._serialize_artifact(
            artifact,
            include_content=include_content,
            include_internal_paths=include_internal_paths,
        )

    def get_execution_workspace_path(
        self,
        *,
        execution_id: str | uuid.UUID,
        task_id: int,
        tenant_id: int | None = None,
    ) -> Optional[str]:
        """Return persisted workspace_path for one task-scoped execution."""
        parsed_execution_id = self._parse_uuid(execution_id)
        if parsed_execution_id is None:
            return None

        scoped_tenant_id = self._normalize_optional_tenant_scope(tenant_id)
        query = self.db.query(ToolExecution.workspace_path)
        if scoped_tenant_id is not None:
            query = query.filter(ToolExecution.tenant_id == scoped_tenant_id)
        workspace_path = query.filter(
            ToolExecution.task_id == task_id,
            ToolExecution.id == parsed_execution_id,
        ).scalar()
        if workspace_path is None:
            return None
        normalized = str(workspace_path).strip()
        return normalized or None

    def search_artifacts(
        self,
        *,
        task_id: int,
        tenant_id: int | None = None,
        artifact_kind: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return paginated artifacts for a task with optional kind filtering."""
        safe_limit, safe_offset = self._normalize_pagination(limit=limit, offset=offset)
        scoped_tenant_id = self._normalize_optional_tenant_scope(tenant_id)
        query = self.db.query(ExecutionArtifact)
        if scoped_tenant_id is not None:
            query = query.filter(ExecutionArtifact.tenant_id == scoped_tenant_id)
        query = query.filter(ExecutionArtifact.task_id == task_id)
        if artifact_kind:
            query = query.filter(ExecutionArtifact.artifact_kind == artifact_kind)

        total = query.count()
        artifacts = (
            query.order_by(ExecutionArtifact.created_at.desc(), ExecutionArtifact.id.desc())
            .offset(safe_offset)
            .limit(safe_limit)
            .all()
        )

        return {
            "artifacts": [
                self._serialize_artifact(artifact, include_content=False) for artifact in artifacts
            ],
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
        }

    def get_artifact_catalog_rows(
        self,
        *,
        task_id: int,
        tenant_id: int | None = None,
        tool_name: Optional[str] = None,
        artifact_kind: Optional[str] = None,
        execution_id: Optional[str | uuid.UUID] = None,
        turn_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        query_text: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return joined artifact/execution rows for task-scoped catalog shaping.

        This unpaginated path exists for compatibility in tests/internal call
        sites. Prefer `get_artifact_catalog_page` for request-path usage.
        """
        query = self._build_artifact_catalog_query(
            tenant_id=tenant_id,
            task_id=task_id,
            tool_name=tool_name,
            artifact_kind=artifact_kind,
            execution_id=execution_id,
            turn_id=turn_id,
            conversation_id=conversation_id,
            query_text=query_text,
        )
        if query is None:
            return []

        rows = query.order_by(
            ExecutionArtifact.created_at.desc(),
            ExecutionArtifact.id.desc(),
        ).all()
        return [self._serialize_artifact_catalog_row(artifact, execution) for artifact, execution in rows]

    def get_artifact_catalog_page(
        self,
        *,
        task_id: int,
        tenant_id: int | None = None,
        tool_name: Optional[str] = None,
        artifact_kind: Optional[str] = None,
        execution_id: Optional[str | uuid.UUID] = None,
        turn_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        query_text: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Return task-scoped joined artifact/execution rows with DB-level pagination."""
        safe_limit, safe_offset = self._normalize_pagination(limit=limit, offset=offset)
        query = self._build_artifact_catalog_query(
            tenant_id=tenant_id,
            task_id=task_id,
            tool_name=tool_name,
            artifact_kind=artifact_kind,
            execution_id=execution_id,
            turn_id=turn_id,
            conversation_id=conversation_id,
            query_text=query_text,
        )
        if query is None:
            return {"rows": [], "total": 0, "limit": safe_limit, "offset": safe_offset}

        total = query.count()
        rows = (
            query.order_by(
                ExecutionArtifact.created_at.desc(),
                ExecutionArtifact.id.desc(),
            )
            .offset(safe_offset)
            .limit(safe_limit)
            .all()
        )
        serialized_rows = [self._serialize_artifact_catalog_row(artifact, execution) for artifact, execution in rows]
        return {
            "rows": serialized_rows,
            "total": total,
            "limit": safe_limit,
            "offset": safe_offset,
        }

    def task_has_any_artifacts(self, *, task_id: int) -> bool:
        """Return whether at least one artifact exists for the task."""
        return self.task_has_any_artifacts_by_scope(task_id=task_id, tenant_id=None)

    def task_has_any_artifacts_by_scope(self, *, task_id: int, tenant_id: int | None = None) -> bool:
        """Return whether at least one artifact exists for task and optional tenant scope."""
        scoped_task_id = self._require_task_scope(task_id)
        scoped_tenant_id = self._normalize_optional_tenant_scope(tenant_id)
        query = self.db.query(ExecutionArtifact.id)
        if scoped_tenant_id is not None:
            query = query.filter(ExecutionArtifact.tenant_id == scoped_tenant_id)
        row = query.filter(ExecutionArtifact.task_id == scoped_task_id).limit(1).first()
        return row is not None

    def get_raw_output_batch(
        self,
        *,
        task_id: int,
        tenant_id: int | None = None,
        tool_call_ids: List[str],
    ) -> Dict[str, Any]:
        """Resolve terminal raw-output state for many tool_call_ids in one query path."""
        normalized_ids: List[str] = []
        seen = set()
        for raw_id in tool_call_ids:
            normalized = str(raw_id).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            normalized_ids.append(normalized)

        if not normalized_ids:
            return {"results": {}, "missing": []}

        scoped_tenant_id = self._normalize_optional_tenant_scope(tenant_id)
        query = self.db.query(ToolExecution).options(selectinload(ToolExecution.artifacts))
        if scoped_tenant_id is not None:
            query = query.filter(ToolExecution.tenant_id == scoped_tenant_id)
        rows = query.filter(
            ToolExecution.task_id == task_id,
            ToolExecution.tool_call_id.in_(normalized_ids),
        ).all()
        by_tool_call = {
            str(row.tool_call_id): row for row in rows if isinstance(row.tool_call_id, str) and row.tool_call_id.strip()
        }

        results: Dict[str, Dict[str, Any]] = {}
        missing: List[str] = []
        for tool_call_id in normalized_ids:
            execution = by_tool_call.get(tool_call_id)
            if execution is None:
                missing.append(tool_call_id)
                results[tool_call_id] = {
                    "status": "not_available",
                    "reason": "execution_not_found",
                }
                continue
            results[tool_call_id] = self._build_raw_output_payload(execution.artifacts)

        return {"results": results, "missing": missing}

    def _serialize_execution(
        self,
        execution: ToolExecution,
        *,
        include_artifacts: bool = False,
    ) -> Dict[str, Any]:
        """Serialize ToolExecution ORM entity to JSON-safe dict."""
        data: Dict[str, Any] = {
            "execution_id": str(execution.id),
            "task_id": execution.task_id,
            "chat_message_id": execution.chat_message_id,
            "tool_call_id": execution.tool_call_id,
            "conversation_id": execution.conversation_id,
            "turn_id": execution.turn_id,
            "turn_sequence": execution.turn_sequence,
            "tool_name": execution.tool_name,
            "tool_arguments": execution.tool_arguments,
            "purpose": execution.purpose,
            "agent_path": execution.agent_path,
            "execution_transport": execution.execution_transport,
            "status": execution.status,
            "exit_code": execution.exit_code,
            "started_at": self._serialize_datetime(execution.started_at),
            "finished_at": self._serialize_datetime(execution.finished_at),
            "duration_ms": execution.duration_ms,
            "execution_metadata": execution.execution_metadata,
            "created_at": self._serialize_datetime(execution.created_at),
        }
        if include_artifacts:
            data["artifacts"] = [
                self._serialize_artifact(artifact, include_content=False)
                for artifact in execution.artifacts
            ]
        return data

    def _serialize_artifact(
        self,
        artifact: ExecutionArtifact,
        *,
        include_content: bool = False,
        include_internal_paths: bool = False,
    ) -> Dict[str, Any]:
        """Serialize ExecutionArtifact ORM entity to JSON-safe dict."""
        payload = {
            "artifact_id": str(artifact.id),
            "execution_id": str(artifact.execution_id),
            "task_id": artifact.task_id,
            "artifact_kind": artifact.artifact_kind,
            "relative_path": artifact.relative_path,
            "content_text": artifact.content_text if include_content else None,
            "upload_status": artifact.upload_status,
            "content_sha256": artifact.content_sha256,
            "byte_size": artifact.byte_size,
            "mime_type": artifact.mime_type,
            "is_text": artifact.is_text,
            "content_availability": self._resolve_content_availability(artifact),
            "artifact_metadata": artifact.artifact_metadata,
            "created_at": self._serialize_datetime(artifact.created_at),
        }
        if include_internal_paths:
            payload["source_path"] = artifact.source_path
            payload["fallback_path"] = artifact.fallback_path
        return payload

    @staticmethod
    def _build_raw_output_state(artifacts: list[ExecutionArtifact]) -> Dict[str, Any]:
        """Summarize command/stdout/stderr availability for frontend tool cards."""
        command_artifact = next((artifact for artifact in artifacts if artifact.artifact_kind == "command"), None)
        stdout_artifact = next((artifact for artifact in artifacts if artifact.artifact_kind == "stdout"), None)
        stderr_artifact = next((artifact for artifact in artifacts if artifact.artifact_kind == "stderr"), None)
        has_output_refs = command_artifact is not None or stdout_artifact is not None or stderr_artifact is not None

        if has_output_refs:
            return {
                "availability": "available",
                "reason": "artifacts_present",
                "command_artifact_id": str(command_artifact.id) if command_artifact is not None else None,
                "stdout_artifact_id": str(stdout_artifact.id) if stdout_artifact is not None else None,
                "stderr_artifact_id": str(stderr_artifact.id) if stderr_artifact is not None else None,
            }

        return {
            "availability": "not_available",
            "reason": "missing_command_stdout_stderr_artifacts",
            "command_artifact_id": None,
            "stdout_artifact_id": None,
            "stderr_artifact_id": None,
        }

    @staticmethod
    def _build_raw_output_payload(artifacts: list[ExecutionArtifact]) -> Dict[str, Any]:
        """Build frontend-ready raw output state from execution artifacts."""
        command_artifact = next((artifact for artifact in artifacts if artifact.artifact_kind == "command"), None)
        stdout_artifact = next((artifact for artifact in artifacts if artifact.artifact_kind == "stdout"), None)
        stderr_artifact = next((artifact for artifact in artifacts if artifact.artifact_kind == "stderr"), None)

        command_artifact_id = str(command_artifact.id) if command_artifact is not None else None
        stdout_artifact_id = str(stdout_artifact.id) if stdout_artifact is not None else None
        stderr_artifact_id = str(stderr_artifact.id) if stderr_artifact is not None else None

        if command_artifact is None and stdout_artifact is None and stderr_artifact is None:
            return {
                "status": "not_available",
                "reason": "missing_output_artifacts",
                "command_artifact_id": command_artifact_id,
                "stdout_artifact_id": stdout_artifact_id,
                "stderr_artifact_id": stderr_artifact_id,
            }

        command_text = command_artifact.content_text if command_artifact is not None else None
        stdout_text = stdout_artifact.content_text if stdout_artifact is not None else None
        stderr_text = stderr_artifact.content_text if stderr_artifact is not None else None

        has_any_inline_content = any(
            content is not None for content in (command_text, stdout_text, stderr_text)
        )
        if not has_any_inline_content:
            return {
                "status": "not_available",
                "reason": "artifact_content_unavailable",
                "command_artifact_id": command_artifact_id,
                "stdout_artifact_id": stdout_artifact_id,
                "stderr_artifact_id": stderr_artifact_id,
            }

        output_text = ArtifactProvenanceQueryService._compose_terminal_output(
            command_text=command_text,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
        )
        return {
            "status": "ready",
            "output_text": output_text,
            "command_artifact_id": command_artifact_id,
            "stdout_artifact_id": stdout_artifact_id,
            "stderr_artifact_id": stderr_artifact_id,
        }

    @staticmethod
    def _compose_terminal_output(
        *,
        command_text: Optional[str],
        stdout_text: Optional[str],
        stderr_text: Optional[str],
    ) -> str:
        """Compose shell-like terminal output text from command/stdout/stderr artifacts."""
        normalized_command = str(command_text or "").strip()
        normalized_stdout = ArtifactProvenanceQueryService._normalize_terminal_text(stdout_text)
        normalized_stderr = ArtifactProvenanceQueryService._normalize_terminal_text(stderr_text)

        blocks: List[str] = []
        if normalized_command:
            blocks.append(f"$ {normalized_command}")
        if normalized_stdout:
            blocks.append(ArtifactProvenanceQueryService._strip_trailing_newlines(normalized_stdout))
        if normalized_stderr:
            blocks.append(ArtifactProvenanceQueryService._strip_trailing_newlines(normalized_stderr))

        output = "\n".join(blocks)
        if output and (normalized_stdout.endswith("\n") or normalized_stderr.endswith("\n")):
            output += "\n"
        return output

    @staticmethod
    def _normalize_terminal_text(value: Optional[str]) -> str:
        if not value:
            return ""
        return str(value).replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _strip_trailing_newlines(value: str) -> str:
        return value.rstrip("\n")

    @staticmethod
    def _resolve_content_availability(artifact: ExecutionArtifact) -> str:
        """Classify persisted content availability for explicit frontend rendering states."""
        normalized_upload_status = str(artifact.upload_status or "").strip().lower()
        if normalized_upload_status == "upload_pending":
            return "upload_pending"
        if normalized_upload_status in {"upload_failed", "failed"}:
            return "upload_failed"
        if artifact.content_text is not None:
            return "available_inline"
        if artifact.object_key:
            return "available_object"
        if artifact.is_text is False:
            return "unavailable_non_text"
        if artifact.source_path or artifact.fallback_path:
            return "local_compatibility_only"
        return "local_compatibility_only"

    @staticmethod
    def _parse_uuid(value: str | uuid.UUID) -> Optional[uuid.UUID]:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            return None

    @staticmethod
    def _serialize_datetime(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _normalize_pagination(*, limit: int, offset: int) -> tuple[int, int]:
        safe_limit = max(1, min(int(limit), 1000))
        safe_offset = max(0, int(offset))
        return safe_limit, safe_offset

    def _build_artifact_catalog_query(
        self,
        *,
        tenant_id: int | None,
        task_id: int,
        tool_name: Optional[str],
        artifact_kind: Optional[str],
        execution_id: Optional[str | uuid.UUID],
        turn_id: Optional[str],
        conversation_id: Optional[str],
        query_text: Optional[str],
    ):
        scoped_tenant_id = self._normalize_optional_tenant_scope(tenant_id)
        query = (
            self.db.query(ExecutionArtifact, ToolExecution)
            .join(ToolExecution, ToolExecution.id == ExecutionArtifact.execution_id)
        )
        if scoped_tenant_id is not None:
            query = query.filter(
                ExecutionArtifact.tenant_id == scoped_tenant_id,
                ToolExecution.tenant_id == scoped_tenant_id,
            )
        query = query.filter(
            ExecutionArtifact.task_id == task_id,
            ToolExecution.task_id == task_id,
        )
        if tool_name:
            query = query.filter(ToolExecution.tool_name == tool_name)
        if artifact_kind:
            query = query.filter(ExecutionArtifact.artifact_kind == artifact_kind)
        if turn_id:
            query = query.filter(ToolExecution.turn_id == turn_id)
        if conversation_id:
            query = query.filter(ToolExecution.conversation_id == conversation_id)
        if execution_id is not None:
            parsed_execution_id = self._parse_uuid(execution_id)
            if parsed_execution_id is None:
                return None
            query = query.filter(ExecutionArtifact.execution_id == parsed_execution_id)

        normalized_query = str(query_text or "").strip().lower()
        if normalized_query:
            wildcard = f"%{normalized_query}%"
            label_expression = func.lower(
                build_artifact_catalog_label_expression(
                    artifact_kind=ExecutionArtifact.artifact_kind,
                    tool_name=ToolExecution.tool_name,
                    turn_sequence=ToolExecution.turn_sequence,
                    execution_id=ExecutionArtifact.execution_id,
                )
            )
            query = query.filter(
                or_(
                    func.lower(func.coalesce(ExecutionArtifact.relative_path, "")).like(wildcard),
                    label_expression.like(wildcard),
                )
            )

        return query

    def _serialize_artifact_catalog_row(
        self,
        artifact: ExecutionArtifact,
        execution: ToolExecution,
    ) -> Dict[str, Any]:
        return {
            "artifact_id": str(artifact.id),
            "execution_id": str(artifact.execution_id),
            "tool_call_id": execution.tool_call_id,
            "tool_name": execution.tool_name,
            "artifact_kind": artifact.artifact_kind,
            "relative_path": artifact.relative_path,
            "turn_id": execution.turn_id,
            "turn_sequence": execution.turn_sequence,
            "byte_size": artifact.byte_size,
            "mime_type": artifact.mime_type,
            "content_availability": self._resolve_content_availability(artifact),
            "task_id": artifact.task_id,
            "created_at": self._serialize_datetime(artifact.created_at),
        }

    @staticmethod
    def _require_task_scope(task_id: int) -> int:
        try:
            scoped = int(task_id)
        except (TypeError, ValueError):
            raise ArtifactProvenanceScopeError("Valid task_id is required for provenance queries") from None
        if scoped <= 0:
            raise ArtifactProvenanceScopeError("Valid task_id is required for provenance queries")
        return scoped

    @staticmethod
    def _normalize_optional_tenant_scope(tenant_id: int | None) -> int | None:
        """Return normalized tenant scope when provided, otherwise None."""
        if tenant_id is None:
            return None
        try:
            scoped = int(tenant_id)
        except (TypeError, ValueError):
            raise ArtifactProvenanceScopeError("Valid tenant_id is required for scoped provenance queries") from None
        if scoped <= 0:
            raise ArtifactProvenanceScopeError("Valid tenant_id is required for scoped provenance queries")
        return scoped
