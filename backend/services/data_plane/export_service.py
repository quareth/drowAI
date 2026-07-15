"""Tenant-scoped data plane export service.

Scope:
- Build task export bundles from tenant/task-scoped provenance, knowledge, and
  stream-event rows.
- Optionally include bounded inline object payloads for artifact/evidence rows.
- Sanitize signed URL fields from exported JSON payloads.

Boundary:
- This service only assembles export payload data from the existing system of
  record; it does not create archives, generate signed URLs, or mutate rows.
"""

from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from backend.core.time_utils import format_iso, utc_now
from backend.models.core import Task
from backend.models.knowledge import (
    EngagementAssetLink,
    EngagementFindingLink,
    EngagementServiceLink,
    EngagementWebPathLink,
    KnowledgeAsset,
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeIngestionRun,
    KnowledgeObservation,
    KnowledgeRelationship,
    KnowledgeService,
    KnowledgeWebPath,
)
from backend.models.provenance import ArtifactManifest, ExecutionArtifact, ToolExecution
from backend.models.streaming import StreamEvent
from backend.services.data_plane.object_store import ObjectStore
from backend.services.data_plane.registry import get_object_store

_SIGNED_URL_KEYS = {
    "signed_url",
    "signed_upload_url",
    "signed_download_url",
    "upload_signed_url",
    "download_signed_url",
}


@dataclass(frozen=True, slots=True)
class DataPlaneTaskExportBundle:
    """Serialized task export payload plus bounded object-content summary."""

    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


class DataPlaneExportService:
    """Export tenant/task-scoped data plane records."""

    DEFAULT_MAX_OBJECT_BYTES = 256 * 1024
    DEFAULT_MAX_TOTAL_OBJECT_BYTES = 4 * 1024 * 1024

    def __init__(self, db: Session, *, object_store: ObjectStore | None = None) -> None:
        self._db = db
        self._object_store = object_store or get_object_store()

    def export_task_bundle(
        self,
        *,
        tenant_id: int,
        task_id: int,
        include_object_payloads: bool = False,
        max_object_bytes: int | None = None,
        max_total_object_bytes: int | None = None,
    ) -> DataPlaneTaskExportBundle:
        """Build one tenant-scoped task export bundle."""
        task = self._db.execute(
            select(Task).where(
                Task.id == int(task_id),
                Task.tenant_id == int(tenant_id),
            )
        ).scalar_one_or_none()
        if task is None:
            raise ValueError("task_not_found")

        per_object_limit = self._normalize_limit(
            max_object_bytes,
            default_value=self.DEFAULT_MAX_OBJECT_BYTES,
        )
        total_object_limit = self._normalize_limit(
            max_total_object_bytes,
            default_value=self.DEFAULT_MAX_TOTAL_OBJECT_BYTES,
        )

        execution_rows = self._query_rows(
            select(ToolExecution).where(
                ToolExecution.tenant_id == int(tenant_id),
                ToolExecution.task_id == int(task_id),
            )
        )
        execution_ids = {row.id for row in execution_rows}

        manifest_rows = self._query_rows(
            select(ArtifactManifest).where(
                ArtifactManifest.tenant_id == int(tenant_id),
                ArtifactManifest.task_id == int(task_id),
            )
        )
        artifact_rows = self._query_rows(
            select(ExecutionArtifact).where(
                ExecutionArtifact.tenant_id == int(tenant_id),
                ExecutionArtifact.task_id == int(task_id),
            )
        )

        ingestion_rows = self._query_rows(
            select(KnowledgeIngestionRun).where(
                KnowledgeIngestionRun.tenant_id == int(tenant_id),
                (
                    (KnowledgeIngestionRun.task_id == int(task_id))
                    | (KnowledgeIngestionRun.source_execution_id.in_(execution_ids))
                ),
            )
        )
        observation_rows = self._query_rows(
            select(KnowledgeObservation).where(
                KnowledgeObservation.tenant_id == int(tenant_id),
                (
                    (KnowledgeObservation.task_id == int(task_id))
                    | (KnowledgeObservation.source_execution_id.in_(execution_ids))
                ),
            )
        )
        evidence_rows = self._query_rows(
            select(KnowledgeEvidenceArchive).where(
                KnowledgeEvidenceArchive.tenant_id == int(tenant_id),
                (
                    (KnowledgeEvidenceArchive.task_id == int(task_id))
                    | (KnowledgeEvidenceArchive.source_execution_id.in_(execution_ids))
                ),
            )
        )

        engagement_ids: set[int] = {
            int(task.engagement_id) for _ in [0] if task.engagement_id is not None
        }
        engagement_ids.update(
            int(row.engagement_id)
            for row in ingestion_rows
            if row.engagement_id is not None
        )
        engagement_ids.update(
            int(row.engagement_id)
            for row in observation_rows
            if row.engagement_id is not None
        )
        engagement_ids.update(
            int(row.engagement_id)
            for row in evidence_rows
            if row.engagement_id is not None
        )

        read_models = self._load_read_models(
            tenant_id=int(tenant_id),
            engagement_ids=engagement_ids,
            execution_ids=execution_ids,
            task_id=int(task_id),
        )

        stream_rows = self._query_rows(
            select(StreamEvent).where(
                StreamEvent.task_id == int(task_id),
                StreamEvent.tenant_id == int(tenant_id),
            )
        )

        object_payload_summary, object_payloads = self._build_object_payloads(
            include_object_payloads=bool(include_object_payloads),
            artifacts=artifact_rows,
            evidence_archives=evidence_rows,
            max_object_bytes=per_object_limit,
            max_total_object_bytes=total_object_limit,
        )

        payload = {
            "tenant_id": int(tenant_id),
            "task_id": int(task_id),
            "exported_at": format_iso(utc_now()),
            "object_payload_mode": "bounded_inline" if include_object_payloads else "references_only",
            "object_payload_summary": object_payload_summary,
            "tool_executions": [self._serialize_row(row) for row in execution_rows],
            "artifact_manifests": [self._serialize_row(row) for row in manifest_rows],
            "execution_artifacts": [self._serialize_row(row) for row in artifact_rows],
            "knowledge_evidence_archives": [self._serialize_row(row) for row in evidence_rows],
            "knowledge_ingestion_runs": [self._serialize_row(row) for row in ingestion_rows],
            "knowledge_observations": [self._serialize_row(row) for row in observation_rows],
            "knowledge_read_models": read_models,
            "stream_events": [self._serialize_row(row) for row in stream_rows],
            "object_payloads": object_payloads,
        }
        return DataPlaneTaskExportBundle(payload=self._sanitize_for_export(payload))

    def _load_read_models(
        self,
        *,
        tenant_id: int,
        engagement_ids: set[int],
        execution_ids: set[UUID],
        task_id: int,
    ) -> dict[str, list[dict[str, Any]]]:
        def engagement_predicate(column: Any) -> Any:
            if engagement_ids:
                return column.in_(engagement_ids)
            return column.is_(None)

        assets = self._query_rows(
            select(KnowledgeAsset).where(
                KnowledgeAsset.tenant_id == tenant_id,
                engagement_predicate(KnowledgeAsset.engagement_id),
            )
        )
        services = self._query_rows(
            select(KnowledgeService).where(
                KnowledgeService.tenant_id == tenant_id,
                engagement_predicate(KnowledgeService.engagement_id),
            )
        )
        findings = self._query_rows(
            select(KnowledgeFinding).where(
                KnowledgeFinding.tenant_id == tenant_id,
                engagement_predicate(KnowledgeFinding.engagement_id),
            )
        )
        relationships = self._query_rows(
            select(KnowledgeRelationship).where(
                KnowledgeRelationship.tenant_id == tenant_id,
                engagement_predicate(KnowledgeRelationship.engagement_id),
            )
        )
        engagement_asset_links = self._query_rows(
            select(EngagementAssetLink).where(
                EngagementAssetLink.tenant_id == tenant_id,
                engagement_predicate(EngagementAssetLink.engagement_id),
            )
        )
        engagement_service_links = self._query_rows(
            select(EngagementServiceLink).where(
                EngagementServiceLink.tenant_id == tenant_id,
                engagement_predicate(EngagementServiceLink.engagement_id),
            )
        )
        engagement_finding_links = self._query_rows(
            select(EngagementFindingLink).where(
                EngagementFindingLink.tenant_id == tenant_id,
                engagement_predicate(EngagementFindingLink.engagement_id),
            )
        )
        engagement_web_path_links = self._query_rows(
            select(EngagementWebPathLink).where(
                EngagementWebPathLink.tenant_id == tenant_id,
                engagement_predicate(EngagementWebPathLink.engagement_id),
            )
        )
        entity_provenance = self._query_rows(
            select(KnowledgeEntityProvenance).where(
                KnowledgeEntityProvenance.tenant_id == tenant_id,
                (
                    engagement_predicate(KnowledgeEntityProvenance.engagement_id)
                    | (KnowledgeEntityProvenance.execution_id.in_(execution_ids))
                    | (KnowledgeEntityProvenance.task_id == task_id)
                ),
            )
        )
        scoped_web_path_ids = {
            row.web_path_id
            for row in engagement_web_path_links
            if row.web_path_id is not None
        }
        scoped_web_path_ids.update(
            row.entity_id
            for row in entity_provenance
            if row.entity_type == "web_path" and row.entity_id is not None
        )
        if scoped_web_path_ids:
            web_paths = self._query_rows(
                select(KnowledgeWebPath).where(
                    KnowledgeWebPath.tenant_id == tenant_id,
                    KnowledgeWebPath.id.in_(scoped_web_path_ids),
                )
            )
        else:
            web_paths = []

        return {
            "knowledge_assets": [self._serialize_row(row) for row in assets],
            "knowledge_services": [self._serialize_row(row) for row in services],
            "knowledge_findings": [self._serialize_row(row) for row in findings],
            "knowledge_relationships": [self._serialize_row(row) for row in relationships],
            "knowledge_web_paths": [self._serialize_row(row) for row in web_paths],
            "engagement_asset_links": [self._serialize_row(row) for row in engagement_asset_links],
            "engagement_service_links": [self._serialize_row(row) for row in engagement_service_links],
            "engagement_finding_links": [self._serialize_row(row) for row in engagement_finding_links],
            "engagement_web_path_links": [self._serialize_row(row) for row in engagement_web_path_links],
            "knowledge_entity_provenance": [self._serialize_row(row) for row in entity_provenance],
        }

    def _build_object_payloads(
        self,
        *,
        include_object_payloads: bool,
        artifacts: list[ExecutionArtifact],
        evidence_archives: list[KnowledgeEvidenceArchive],
        max_object_bytes: int,
        max_total_object_bytes: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not include_object_payloads:
            return (
                {
                    "included_count": 0,
                    "failed_count": 0,
                    "truncated_count": 0,
                    "total_returned_bytes": 0,
                    "max_object_bytes": int(max_object_bytes),
                    "max_total_object_bytes": int(max_total_object_bytes),
                    "remaining_object_bytes": int(max_total_object_bytes),
                },
                [],
            )

        remaining = int(max_total_object_bytes)
        truncated_count = 0
        failed_count = 0
        payloads: list[dict[str, Any]] = []

        artifact_targets = [
            ("execution_artifact", str(row.id), str(row.object_key or "").strip(), row.byte_size)
            for row in artifacts
            if str(row.object_key or "").strip()
        ]
        evidence_targets = [
            ("knowledge_evidence_archive", str(row.id), str(row.object_key or "").strip(), row.byte_size)
            for row in evidence_archives
            if str(row.object_key or "").strip()
        ]
        targets = artifact_targets + evidence_targets

        for object_type, row_id, object_key, expected_size in targets:
            if remaining <= 0:
                payloads.append(
                    {
                        "object_type": object_type,
                        "row_id": row_id,
                        "object_key": object_key,
                        "status": "not_included",
                        "reason": "bundle_byte_budget_exhausted",
                    }
                )
                continue

            read_limit = min(int(max_object_bytes), remaining)
            item_payload = self._read_object_payload(
                object_type=object_type,
                row_id=row_id,
                object_key=object_key,
                expected_size=expected_size,
                max_bytes=read_limit,
            )
            payloads.append(item_payload)
            if item_payload.get("status") != "ready":
                failed_count += 1
                continue

            returned = int(item_payload.get("returned_bytes") or 0)
            remaining = max(0, remaining - returned)
            if bool(item_payload.get("truncated")):
                truncated_count += 1

        included_count = sum(1 for item in payloads if item.get("status") == "ready")
        total_returned = sum(int(item.get("returned_bytes") or 0) for item in payloads if item.get("status") == "ready")
        summary = {
            "included_count": int(included_count),
            "failed_count": int(failed_count),
            "truncated_count": int(truncated_count),
            "total_returned_bytes": int(total_returned),
            "max_object_bytes": int(max_object_bytes),
            "max_total_object_bytes": int(max_total_object_bytes),
            "remaining_object_bytes": int(remaining),
        }
        return summary, payloads

    def _read_object_payload(
        self,
        *,
        object_type: str,
        row_id: str,
        object_key: str,
        expected_size: int | None,
        max_bytes: int,
    ) -> dict[str, Any]:
        try:
            content = self._object_store.read_bytes(object_key, max_bytes=max_bytes)
            head = self._object_store.head_object(object_key)
        except Exception:
            return {
                "object_type": object_type,
                "row_id": row_id,
                "object_key": object_key,
                "status": "not_available",
                "reason": "object_read_failed",
            }

        estimated_size = int(expected_size or 0)
        if estimated_size <= 0 and head is not None:
            estimated_size = max(0, int(head.byte_size))
        returned_bytes = len(content)
        truncated = estimated_size > returned_bytes if estimated_size > 0 else False

        return {
            "object_type": object_type,
            "row_id": row_id,
            "object_key": object_key,
            "status": "ready",
            "expected_bytes": estimated_size if estimated_size > 0 else None,
            "returned_bytes": int(returned_bytes),
            "truncated": bool(truncated),
            "content_base64": b64encode(content).decode("ascii"),
        }

    def _query_rows(self, stmt: Select[Any]) -> list[Any]:
        return list(self._db.execute(stmt).scalars())

    @staticmethod
    def _normalize_limit(value: int | None, *, default_value: int) -> int:
        if value is None:
            return int(default_value)
        return max(1, int(value))

    def _serialize_row(self, row: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for attr in row.__mapper__.column_attrs:  # type: ignore[attr-defined]
            column = attr.columns[0]
            payload[column.name] = self._serialize_scalar(getattr(row, attr.key))
        return payload

    def _sanitize_for_export(self, value: Any) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                normalized_key = str(key).strip().lower()
                if normalized_key in _SIGNED_URL_KEYS:
                    continue
                if "signed" in normalized_key and "url" in normalized_key:
                    continue
                sanitized[key] = self._sanitize_for_export(item)
            return sanitized
        if isinstance(value, list):
            return [self._sanitize_for_export(item) for item in value]
        if isinstance(value, tuple):
            return [self._sanitize_for_export(item) for item in value]
        return self._serialize_scalar(value)

    def _serialize_scalar(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return format_iso(value)
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        return value
