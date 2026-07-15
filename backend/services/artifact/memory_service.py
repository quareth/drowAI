"""
Task-scoped artifact memory service contracts and orchestration boundary.

This module defines the shared application-level contract for artifact search
and bounded artifact reads. It delegates database retrieval to
`ArtifactProvenanceQueryService` and keeps task-scope checks centralized.
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from typing import Optional, Literal

from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.services.data_plane.artifact_read_service import ArtifactReadService
from backend.services.data_plane.artifact_read_service import ArtifactObjectReadReason
from backend.services.runtime_provider.contracts import RuntimeActorType, is_runner_placement_mode
from backend.services.runtime_provider.runtime_artifact_access import (
    decode_runtime_artifact_text_delegate,
    execute_runtime_artifact_read_sync,
)

from .catalog_labels import build_artifact_catalog_label
from .provenance_query_service import ArtifactProvenanceQueryService

ArtifactReadMode = Literal["auto", "head", "tail", "match", "full"]
ArtifactReadStatus = Literal["ready", "not_found", "not_available", "omitted_by_policy"]
ArtifactReadSource = Literal["inline_db", "object_store", "workspace_file", "none"]

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 1000
_DEFAULT_MAX_CHARS = 4000
_MAX_READ_CHARS = 20000
_MAX_FILE_SCAN_CHARS = 200000
_NON_READABLE_UPLOAD_STATUSES = frozenset({"upload_pending", "upload_failed", "failed"})
_OBJECT_READ_UNAVAILABLE_REASONS: frozenset[ArtifactObjectReadReason] = frozenset(
    {"object_unavailable", "object_read_failed", "decode_failed"}
)


class ArtifactMemoryScopeError(ValueError):
    """Raised when artifact-memory requests do not include a valid task scope."""


@dataclass(frozen=True, slots=True)
class ArtifactSearchFilters:
    """Agent-facing search filters for task-scoped artifact catalog lookup."""

    query: Optional[str] = None
    tool_name: Optional[str] = None
    artifact_kind: Optional[str] = None
    execution_id: Optional[str] = None
    turn_id: Optional[str] = None
    conversation_id: Optional[str] = None
    limit: int = _DEFAULT_LIMIT
    offset: int = 0

    def normalized(self) -> "ArtifactSearchFilters":
        """Return a pagination-safe copy while preserving additive filter fields."""
        safe_limit = max(1, min(int(self.limit), _MAX_LIMIT))
        safe_offset = max(0, int(self.offset))
        return ArtifactSearchFilters(
            query=self.query,
            tool_name=self.tool_name,
            artifact_kind=self.artifact_kind,
            execution_id=self.execution_id,
            turn_id=self.turn_id,
            conversation_id=self.conversation_id,
            limit=safe_limit,
            offset=safe_offset,
        )


@dataclass(frozen=True, slots=True)
class ArtifactCatalogEntry:
    """Typed catalog row contract for task-scoped artifact discovery."""

    artifact_id: str
    execution_id: str
    tool_call_id: Optional[str]
    tool_name: str
    task_id: int
    artifact_kind: str
    relative_path: Optional[str]
    turn_id: Optional[str]
    turn_sequence: Optional[int]
    byte_size: Optional[int]
    mime_type: Optional[str]
    content_availability: str
    label: str
    created_at: Optional[str]


@dataclass(frozen=True, slots=True)
class ArtifactCatalogPage:
    """Typed paginated artifact catalog response."""

    artifacts: tuple[ArtifactCatalogEntry, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ArtifactReadRequest:
    """Bounded artifact read contract used by routers/tools."""

    mode: ArtifactReadMode = "auto"
    query: Optional[str] = None
    max_chars: int = _DEFAULT_MAX_CHARS

    def normalized_max_chars(self) -> int:
        """Return a safe service-level character budget."""
        return max(1, min(int(self.max_chars), _MAX_READ_CHARS))


@dataclass(frozen=True, slots=True)
class ArtifactReadResult:
    """Typed artifact read response for application-level callers."""

    status: ArtifactReadStatus
    artifact_id: str
    content: Optional[str]
    content_availability: str
    mode_used: ArtifactReadMode
    truncated: bool
    source: ArtifactReadSource
    artifact: Optional[ArtifactCatalogEntry]


class ArtifactMemoryService:
    """Task-scoped artifact memory boundary for search/read orchestration."""

    def __init__(
        self,
        db: Session,
        *,
        query_service: Optional[ArtifactProvenanceQueryService] = None,
        object_read_service: Optional[ArtifactReadService] = None,
    ) -> None:
        self.db = db
        self.query_service = query_service or ArtifactProvenanceQueryService(db)
        self.object_read_service = object_read_service or ArtifactReadService(db)

    def search_task_artifacts(
        self,
        *,
        task_id: int,
        tenant_id: int | None = None,
        filters: ArtifactSearchFilters,
    ) -> ArtifactCatalogPage:
        """
        Return task-scoped artifact catalog rows.

        Query semantics:
        - strict task scoping
        - deterministic newest-first ordering, tie-broken by artifact UUID
        - optional case-insensitive substring query on `label` and `relative_path`
        """
        self._require_task_context(task_id)
        normalized = filters.normalized()
        page = self.query_service.get_artifact_catalog_page(
            task_id=task_id,
            tenant_id=tenant_id,
            tool_name=normalized.tool_name,
            artifact_kind=normalized.artifact_kind,
            execution_id=normalized.execution_id,
            turn_id=normalized.turn_id,
            conversation_id=normalized.conversation_id,
            query_text=normalized.query,
            limit=normalized.limit,
            offset=normalized.offset,
        )

        shaped_rows = tuple(
            self._catalog_entry_from_catalog_row(row) for row in page.get("rows", [])
        )
        return ArtifactCatalogPage(
            artifacts=shaped_rows,
            total=int(page.get("total", 0)),
            limit=int(page.get("limit", normalized.limit)),
            offset=int(page.get("offset", normalized.offset)),
        )

    def task_has_persisted_artifacts(
        self,
        *,
        task_id: int,
        tenant_id: int | None = None,
    ) -> bool:
        """Return whether at least one artifact exists for the task."""
        self._require_task_context(task_id)
        return self.query_service.task_has_any_artifacts_by_scope(
            task_id=task_id,
            tenant_id=tenant_id,
        )

    def read_task_artifact(
        self,
        *,
        task_id: int,
        tenant_id: int | None = None,
        artifact_id: str,
        request: ArtifactReadRequest,
        user_id: int | None = None,
    ) -> ArtifactReadResult:
        """Return a task-scoped bounded artifact read result."""
        self._require_task_context(task_id)
        scoped_tenant_id = int(tenant_id) if tenant_id is not None else self._resolve_task_tenant_id(task_id)
        payload = self.query_service.get_artifact_by_id(
            artifact_id,
            task_id=task_id,
            tenant_id=scoped_tenant_id,
            include_content=True,
            include_internal_paths=True,
        )
        if payload is None:
            return ArtifactReadResult(
                status="not_found",
                artifact_id=str(artifact_id),
                content=None,
                content_availability="not_found",
                mode_used=request.mode,
                truncated=False,
                source="none",
                artifact=None,
            )

        execution_id = str(payload["execution_id"])
        execution_record = self.query_service.get_execution_by_id(
            execution_id,
            task_id=task_id,
            tenant_id=scoped_tenant_id,
            include_artifacts=False,
        )
        execution_payload = (execution_record or {}).get("execution")
        catalog_entry = self._catalog_entry_from_artifact_detail_payload(
            artifact_payload=payload,
            execution_payload=execution_payload,
        )
        if self._artifact_upload_status_disallows_read(payload):
            return ArtifactReadResult(
                status="not_available",
                artifact_id=catalog_entry.artifact_id,
                content=None,
                content_availability=catalog_entry.content_availability,
                mode_used="auto" if request.mode == "auto" else request.mode,
                truncated=False,
                source="none",
                artifact=catalog_entry,
            )
        content_text = payload.get("content_text")
        source: ArtifactReadSource = "inline_db"
        omitted_by_policy = False
        content_availability = catalog_entry.content_availability
        object_read_unavailable = False

        if content_text is None:
            object_read = self.object_read_service.read_artifact_text(
                task_id=task_id,
                artifact_id=catalog_entry.artifact_id,
                tenant_id=scoped_tenant_id,
                max_bytes=_MAX_FILE_SCAN_CHARS,
            )
            if object_read.status == "ready":
                content_text = object_read.content
                source = "object_store"
                omitted_by_policy = object_read.truncated
            elif object_read.reason in _OBJECT_READ_UNAVAILABLE_REASONS:
                object_read_unavailable = True

        if content_text is None:
            content_text, source, omitted_by_policy = self._resolve_file_fallback_content(
                task_id=task_id,
                artifact_payload=payload,
                execution_payload=execution_payload,
                runner_placement=self._task_uses_runner_placement(task_id),
                user_id=user_id,
            )

        if content_text is None:
            if object_read_unavailable:
                content_availability = "not_available"
            response_catalog_entry = (
                catalog_entry
                if content_availability == catalog_entry.content_availability
                else replace(catalog_entry, content_availability=content_availability)
            )
            return ArtifactReadResult(
                status="not_available",
                artifact_id=response_catalog_entry.artifact_id,
                content=None,
                content_availability=response_catalog_entry.content_availability,
                mode_used="auto" if request.mode == "auto" else request.mode,
                truncated=False,
                source="none",
                artifact=response_catalog_entry,
            )

        bounded_content, truncated, mode_used = self._apply_mode(
            text=str(content_text),
            request=request,
        )
        was_truncated = truncated or omitted_by_policy
        status: ArtifactReadStatus = "ready"
        if request.mode == "full" and omitted_by_policy:
            status = "omitted_by_policy"

        response_catalog_entry = (
            catalog_entry
            if content_availability == catalog_entry.content_availability
            else replace(catalog_entry, content_availability=content_availability)
        )
        return ArtifactReadResult(
            status=status,
            artifact_id=response_catalog_entry.artifact_id,
            content=bounded_content,
            content_availability=response_catalog_entry.content_availability,
            mode_used=mode_used,
            truncated=was_truncated,
            source=source,
            artifact=response_catalog_entry,
        )

    @staticmethod
    def _require_task_context(task_id: int) -> None:
        """Fail closed when task context is absent or invalid."""
        try:
            parsed = int(task_id)
        except (TypeError, ValueError):
            raise ArtifactMemoryScopeError("Valid task_id is required for artifact memory access") from None
        if parsed <= 0:
            raise ArtifactMemoryScopeError("Valid task_id is required for artifact memory access")

    def _resolve_task_tenant_id(self, task_id: int) -> int | None:
        row = self.db.query(Task.tenant_id).filter(Task.id == int(task_id)).one_or_none()
        if row is None:
            return None
        tenant_id = row[0]
        if tenant_id is None:
            return None
        return int(tenant_id)

    @staticmethod
    def _catalog_entry_from_catalog_row(payload: dict) -> ArtifactCatalogEntry:
        label = build_artifact_catalog_label(
            artifact_kind=str(payload["artifact_kind"]),
            tool_name=str(payload["tool_name"]),
            turn_sequence=payload.get("turn_sequence"),
            execution_id=str(payload["execution_id"]),
        )
        return ArtifactCatalogEntry(
            artifact_id=str(payload["artifact_id"]),
            execution_id=str(payload["execution_id"]),
            tool_call_id=payload.get("tool_call_id"),
            tool_name=str(payload["tool_name"]),
            task_id=int(payload["task_id"]),
            artifact_kind=str(payload["artifact_kind"]),
            relative_path=payload.get("relative_path"),
            turn_id=payload.get("turn_id"),
            turn_sequence=payload.get("turn_sequence"),
            byte_size=payload.get("byte_size"),
            mime_type=payload.get("mime_type"),
            content_availability=str(payload.get("content_availability") or "unknown"),
            label=label,
            created_at=payload.get("created_at"),
        )

    @staticmethod
    def _catalog_entry_from_artifact_detail_payload(
        *,
        artifact_payload: dict,
        execution_payload: Optional[dict],
    ) -> ArtifactCatalogEntry:
        tool_name = (
            str(execution_payload.get("tool_name"))
            if execution_payload and execution_payload.get("tool_name")
            else "unknown_tool"
        )
        turn_sequence = execution_payload.get("turn_sequence") if execution_payload else None
        return ArtifactCatalogEntry(
            artifact_id=str(artifact_payload["artifact_id"]),
            execution_id=str(artifact_payload["execution_id"]),
            tool_call_id=execution_payload.get("tool_call_id") if execution_payload else None,
            tool_name=tool_name,
            task_id=int(artifact_payload["task_id"]),
            artifact_kind=str(artifact_payload["artifact_kind"]),
            relative_path=artifact_payload.get("relative_path"),
            turn_id=execution_payload.get("turn_id") if execution_payload else None,
            turn_sequence=turn_sequence,
            byte_size=artifact_payload.get("byte_size"),
            mime_type=artifact_payload.get("mime_type"),
            content_availability=str(artifact_payload.get("content_availability") or "unknown"),
            label=build_artifact_catalog_label(
                artifact_kind=str(artifact_payload["artifact_kind"]),
                tool_name=tool_name,
                turn_sequence=turn_sequence,
                execution_id=str(artifact_payload["execution_id"]),
            ),
            created_at=artifact_payload.get("created_at"),
        )

    @staticmethod
    def catalog_page_to_dict(page: ArtifactCatalogPage) -> dict:
        """Serialize typed catalog page into JSON-safe dict payload."""
        return {
            "artifacts": [asdict(item) for item in page.artifacts],
            "total": page.total,
            "limit": page.limit,
            "offset": page.offset,
        }

    @staticmethod
    def _apply_mode(
        *,
        text: str,
        request: ArtifactReadRequest,
    ) -> tuple[str, bool, ArtifactReadMode]:
        budget = request.normalized_max_chars()
        if not text:
            return "", False, "auto" if request.mode == "auto" else request.mode

        mode = request.mode
        if mode == "auto":
            if (request.query or "").strip():
                mode = "match"
            else:
                mode = "head"

        if mode == "head":
            sliced = text[:budget]
            return sliced, len(sliced) < len(text), "head"

        if mode == "tail":
            sliced = text[-budget:]
            return sliced, len(sliced) < len(text), "tail"

        if mode == "match":
            query = (request.query or "").strip()
            if not query:
                sliced = text[:budget]
                return sliced, len(sliced) < len(text), "head"
            lower_text = text.lower()
            lower_query = query.lower()
            hit = lower_text.find(lower_query)
            if hit < 0:
                sliced = text[:budget]
                return sliced, len(sliced) < len(text), "head"
            half = max(1, budget // 2)
            start = max(0, hit - half)
            end = min(len(text), start + budget)
            return text[start:end], (start > 0 or end < len(text)), "match"

        # `full` remains explicit but still bounded by a service-level cap.
        sliced = text[:budget]
        return sliced, len(sliced) < len(text), "full"

    def _resolve_file_fallback_content(
        self,
        *,
        task_id: int,
        artifact_payload: dict,
        execution_payload: dict | None,
        runner_placement: bool,
        user_id: int | None,
    ) -> tuple[Optional[str], ArtifactReadSource, bool]:
        if artifact_payload.get("is_text") is False:
            return None, "none", False
        if runner_placement or self._is_runner_cloud_execution(execution_payload):
            return None, "none", False

        candidate_paths = self._candidate_artifact_paths(artifact_payload)

        for candidate in candidate_paths:
            result = self._read_runtime_artifact_text(
                task_id=task_id,
                path=candidate,
                user_id=user_id,
            )
            if result is None:
                continue
            text, omitted_by_policy = result
            return text, "workspace_file", omitted_by_policy
        return None, "none", False

    def _read_runtime_artifact_text(
        self,
        *,
        task_id: int,
        path: str,
        user_id: int | None,
    ) -> tuple[str, bool] | None:
        result = execute_runtime_artifact_read_sync(
            self.db,
            task_id=int(task_id),
            path=str(path),
            actor_type=(
                RuntimeActorType.USER if user_id is not None else RuntimeActorType.SYSTEM
            ),
            actor_id=user_id if user_id is not None else "artifact_memory",
            user_id=user_id,
            binary=False,
            max_chars=_MAX_FILE_SCAN_CHARS + 1,
            log_context="artifact memory runtime read",
        )
        text, omitted_by_policy = decode_runtime_artifact_text_delegate(result)
        if text is None:
            return None
        return text, omitted_by_policy

    @staticmethod
    def _is_runner_cloud_execution(execution_payload: dict | None) -> bool:
        if not isinstance(execution_payload, dict):
            return False
        normalized_transport = str(execution_payload.get("execution_transport") or "").strip().lower()
        return normalized_transport == "runner_control_channel"

    def _task_uses_runner_placement(self, task_id: int) -> bool:
        row = self.db.query(Task.runtime_placement_mode).filter(Task.id == int(task_id)).one_or_none()
        mode = row[0] if row is not None else None
        return is_runner_placement_mode(mode)

    @staticmethod
    def _candidate_artifact_paths(artifact_payload: dict) -> tuple[str, ...]:
        candidates: list[str] = []
        for key in ("relative_path", "source_path", "fallback_path"):
            raw = artifact_payload.get(key)
            if raw is None:
                continue
            normalized = str(raw).strip()
            if not normalized or normalized in candidates:
                continue
            candidates.append(normalized)
        return tuple(candidates)

    @staticmethod
    def _artifact_upload_status_disallows_read(artifact_payload: dict) -> bool:
        raw = artifact_payload.get("upload_status")
        normalized = str(raw or "").strip().lower()
        return normalized in _NON_READABLE_UPLOAD_STATUSES



__all__ = [
    "ArtifactCatalogEntry",
    "ArtifactCatalogPage",
    "ArtifactMemoryScopeError",
    "ArtifactMemoryService",
    "ArtifactReadRequest",
    "ArtifactReadResult",
    "ArtifactSearchFilters",
    "ArtifactReadMode",
]
