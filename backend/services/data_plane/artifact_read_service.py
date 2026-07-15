"""Object-backed artifact text reads for tenant/task-scoped data-plane access.

Scope:
- Resolve one `ExecutionArtifact` row by tenant/task/artifact identity.
- Read bounded text content from object storage for ready text artifacts.

Boundaries:
- Read-only service; does not mutate artifact upload/provenance state.
- Returns stable availability reasons and never includes object keys or host
  filesystem paths in response payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import uuid

from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.models.provenance import ExecutionArtifact
from .object_store import ObjectStore
from .registry import get_object_store

ArtifactObjectReadStatus = Literal["ready", "not_found", "not_available"]
ArtifactObjectReadReason = Literal[
    "ready",
    "not_found",
    "not_text_artifact",
    "upload_pending",
    "upload_failed",
    "object_unavailable",
    "object_read_failed",
    "decode_failed",
]

_MAX_OBJECT_READ_BYTES = 200000
_NON_READY_UPLOAD_STATUSES = frozenset({"upload_pending", "upload_failed", "failed"})
_TEXTUAL_MIME_PREFIXES = ("text/",)
_TEXTUAL_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/javascript",
        "application/x-sh",
    }
)


@dataclass(frozen=True, slots=True)
class ArtifactObjectReadResult:
    """Bounded text read result for one task-scoped artifact row."""

    status: ArtifactObjectReadStatus
    content: str | None
    truncated: bool
    reason: ArtifactObjectReadReason


class ArtifactReadService:
    """Task-scoped object-backed text reader for execution artifacts."""

    def __init__(
        self,
        db: Session,
        *,
        object_store: ObjectStore | None = None,
    ) -> None:
        self._db = db
        self._object_store = object_store or get_object_store()

    def read_artifact_text(
        self,
        *,
        task_id: int,
        artifact_id: str,
        tenant_id: int | None = None,
        max_bytes: int = _MAX_OBJECT_READ_BYTES,
    ) -> ArtifactObjectReadResult:
        """Read bounded text for one artifact row using object storage when available."""
        scoped_task_id = _require_positive_int(task_id, field_name="task_id")
        scoped_tenant_id = self._resolve_tenant_scope(task_id=scoped_task_id, tenant_id=tenant_id)
        if scoped_tenant_id is None:
            return ArtifactObjectReadResult(
                status="not_found",
                content=None,
                truncated=False,
                reason="not_found",
            )
        parsed_artifact_id = _parse_uuid(artifact_id)
        if parsed_artifact_id is None:
            return ArtifactObjectReadResult(
                status="not_found",
                content=None,
                truncated=False,
                reason="not_found",
            )

        query = self._db.query(ExecutionArtifact).filter(
            ExecutionArtifact.task_id == scoped_task_id,
            ExecutionArtifact.id == parsed_artifact_id,
            ExecutionArtifact.tenant_id == scoped_tenant_id,
        )

        artifact = query.one_or_none()
        if artifact is None:
            return ArtifactObjectReadResult(
                status="not_found",
                content=None,
                truncated=False,
                reason="not_found",
            )

        if not _artifact_is_text_readable(artifact):
            return ArtifactObjectReadResult(
                status="not_available",
                content=None,
                truncated=False,
                reason="not_text_artifact",
            )

        normalized_upload_status = str(artifact.upload_status or "").strip().lower()
        if normalized_upload_status in _NON_READY_UPLOAD_STATUSES:
            reason: ArtifactObjectReadReason = "upload_pending"
            if normalized_upload_status in {"upload_failed", "failed"}:
                reason = "upload_failed"
            return ArtifactObjectReadResult(
                status="not_available",
                content=None,
                truncated=False,
                reason=reason,
            )

        object_key = str(artifact.object_key or "").strip()
        if not object_key:
            return ArtifactObjectReadResult(
                status="not_available",
                content=None,
                truncated=False,
                reason="object_unavailable",
            )

        byte_budget = max(1, min(int(max_bytes), _MAX_OBJECT_READ_BYTES))
        try:
            raw = self._object_store.read_bytes(object_key, max_bytes=byte_budget + 1)
        except Exception:
            return ArtifactObjectReadResult(
                status="not_available",
                content=None,
                truncated=False,
                reason="object_read_failed",
            )

        truncated = len(raw) > byte_budget
        bounded = raw[:byte_budget]
        try:
            content = bounded.decode("utf-8")
        except UnicodeDecodeError:
            return ArtifactObjectReadResult(
                status="not_available",
                content=None,
                truncated=False,
                reason="decode_failed",
            )

        return ArtifactObjectReadResult(
            status="ready",
            content=content,
            truncated=truncated,
            reason="ready",
        )

    def _resolve_tenant_scope(self, *, task_id: int, tenant_id: int | None) -> int | None:
        explicit_tenant_id = _require_optional_positive_int(tenant_id, field_name="tenant_id")
        if explicit_tenant_id is not None:
            return explicit_tenant_id
        resolved_tenant_id = (
            self._db.query(Task.tenant_id)
            .filter(Task.id == int(task_id))
            .scalar()
        )
        if resolved_tenant_id is None:
            return None
        try:
            return _require_positive_int(int(resolved_tenant_id), field_name="tenant_id")
        except ValueError:
            return None


def _artifact_is_text_readable(artifact: ExecutionArtifact) -> bool:
    if artifact.is_text is False:
        return False
    normalized_mime = str(artifact.mime_type or "").strip().lower()
    if not normalized_mime:
        return True
    if normalized_mime.startswith(_TEXTUAL_MIME_PREFIXES):
        return True
    return normalized_mime in _TEXTUAL_MIME_TYPES


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _require_positive_int(value: int, *, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Valid {field_name} is required for artifact object reads") from None
    if parsed <= 0:
        raise ValueError(f"Valid {field_name} is required for artifact object reads")
    return parsed


def _require_optional_positive_int(value: int | None, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _require_positive_int(value, field_name=field_name)


__all__ = [
    "ArtifactObjectReadResult",
    "ArtifactObjectReadStatus",
    "ArtifactReadService",
]
