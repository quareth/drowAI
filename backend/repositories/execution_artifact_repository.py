"""
Data access for execution artifact provenance records.

This module provides CRUD-style operations for `execution_artifacts`,
including batched creation and SHA256 hashing helpers.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy.orm import Session
from sqlalchemy import select

from backend.models.core import Task
from backend.models.provenance import ToolExecution
from backend.models.provenance import ExecutionArtifact


class ExecutionArtifactRepository:
    """Repository for `ExecutionArtifact` persistence and filtering queries."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create_batch(self, artifacts_data: List[Dict[str, Any]]) -> List[ExecutionArtifact]:
        """Insert a batch of artifacts and return the created ORM objects."""
        if not artifacts_data:
            return []

        rows: List[ExecutionArtifact] = []
        for item in artifacts_data:
            payload = dict(item)
            payload.setdefault("id", uuid.uuid4())
            payload.setdefault("artifact_metadata", {})
            payload["tenant_id"] = self._resolve_tenant_id(payload)
            row = ExecutionArtifact(**payload)
            rows.append(row)

        self.db.add_all(rows)
        self.db.flush()
        for row in rows:
            self.db.refresh(row)
        return rows

    def get_by_execution(self, execution_id: str | uuid.UUID) -> List[ExecutionArtifact]:
        """Return artifacts linked to one execution."""
        parsed_id = self._parse_uuid(execution_id)
        if parsed_id is None:
            return []
        return (
            self.db.query(ExecutionArtifact)
            .filter(ExecutionArtifact.execution_id == parsed_id)
            .order_by(ExecutionArtifact.created_at.asc())
            .all()
        )

    def get_by_manifest(self, manifest_id: str | uuid.UUID) -> List[ExecutionArtifact]:
        """Return artifacts linked to one artifact manifest."""
        parsed_manifest_id = self._parse_uuid(manifest_id)
        if parsed_manifest_id is None:
            return []
        return (
            self.db.query(ExecutionArtifact)
            .filter(ExecutionArtifact.manifest_id == parsed_manifest_id)
            .order_by(ExecutionArtifact.created_at.asc())
            .all()
        )

    def get_by_task(
        self,
        *,
        task_id: int,
        artifact_kind: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ExecutionArtifact]:
        """Return artifacts for a task, optionally filtered by kind."""
        query = self.db.query(ExecutionArtifact).filter(ExecutionArtifact.task_id == task_id)
        if artifact_kind is not None:
            query = query.filter(ExecutionArtifact.artifact_kind == artifact_kind)
        return query.order_by(ExecutionArtifact.created_at.desc()).offset(offset).limit(limit).all()

    def get_by_id(self, artifact_id: str | uuid.UUID) -> Optional[ExecutionArtifact]:
        """Return one artifact by UUID primary key."""
        parsed_id = self._parse_uuid(artifact_id)
        if parsed_id is None:
            return None
        return self.db.get(ExecutionArtifact, parsed_id)

    def get_by_tenant_task_artifact_id(
        self,
        *,
        tenant_id: int,
        task_id: int,
        artifact_id: str | uuid.UUID,
    ) -> Optional[ExecutionArtifact]:
        """Return one artifact constrained by tenant/task/artifact identity."""
        parsed_id = self._parse_uuid(artifact_id)
        if parsed_id is None:
            return None
        return (
            self.db.query(ExecutionArtifact)
            .filter(
                ExecutionArtifact.tenant_id == int(tenant_id),
                ExecutionArtifact.task_id == int(task_id),
                ExecutionArtifact.id == parsed_id,
            )
            .one_or_none()
        )

    def list_by_tenant_task(
        self,
        *,
        tenant_id: int,
        task_id: int,
        artifact_kind: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ExecutionArtifact]:
        """Return artifacts constrained by tenant/task with optional kind filter."""
        query = self.db.query(ExecutionArtifact).filter(
            ExecutionArtifact.tenant_id == int(tenant_id),
            ExecutionArtifact.task_id == int(task_id),
        )
        if artifact_kind is not None:
            query = query.filter(ExecutionArtifact.artifact_kind == artifact_kind)
        return query.order_by(ExecutionArtifact.created_at.desc()).offset(offset).limit(limit).all()

    @staticmethod
    def compute_content_hash(content: str | bytes) -> str:
        """Compute a SHA256 hex digest for string or bytes payload."""
        if isinstance(content, str):
            payload = content.encode("utf-8")
        else:
            payload = content
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _parse_uuid(value: str | uuid.UUID) -> Optional[uuid.UUID]:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None

    def _resolve_tenant_id(self, payload: Dict[str, Any]) -> int:
        """Resolve artifact tenant ownership from explicit payload or parent rows."""
        explicit = payload.get("tenant_id")
        if explicit is not None:
            return int(explicit)

        execution_id = payload.get("execution_id")
        if execution_id is not None:
            parsed_execution_id = self._parse_uuid(execution_id)
            if parsed_execution_id is not None:
                execution_tenant = self.db.execute(
                    select(ToolExecution.tenant_id).where(ToolExecution.id == parsed_execution_id)
                ).scalar_one_or_none()
                if execution_tenant is not None:
                    return int(execution_tenant)

        task_id = payload.get("task_id")
        if task_id is not None:
            task_tenant = self.db.execute(
                select(Task.tenant_id).where(Task.id == int(task_id))
            ).scalar_one_or_none()
            if task_tenant is not None:
                return int(task_tenant)

        raise ValueError("Cannot resolve tenant_id for execution artifact write")
