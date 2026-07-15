"""Resolve replay source payloads for ingestion replay.

Scope:
- Select one authoritative replay source for a source execution id.

Responsibilities:
- Prefer runtime provenance payload when task/runtime rows still exist.
- Fall back to durable archive + ingestion lineage when runtime rows are gone.
- Return a normalized payload contract for replay ingestion orchestration.

Boundary:
- This service resolves replay source data only.
- It does not write ingestion runs, archives, or observations."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import KnowledgeEvidenceArchive, KnowledgeIngestionRun, Task
from backend.services.data_plane.object_store import ObjectStore
from backend.services.data_plane.registry import get_object_store
from backend.services.artifact.provenance_query_service import ArtifactProvenanceQueryService
from .contracts import (
    build_replay_execution_metadata_from_snapshot,
    build_semantic_input_snapshot,
)

_MAX_OBJECT_TEXT_BYTES = 200_000
_TEXTUAL_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-ndjson",
        "application/x-yaml",
        "application/yaml",
    }
)


class KnowledgeReplaySourceResolver:
    """Resolve runtime-first, durable-fallback source payloads for replay."""

    def __init__(
        self,
        db: Session,
        *,
        query_service: ArtifactProvenanceQueryService | None = None,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.db = db
        self.query_service = query_service or ArtifactProvenanceQueryService(db)
        self._object_store = object_store or get_object_store()

    def resolve_source(
        self,
        *,
        source_execution_id: str,
        task_id: int | None,
    ) -> dict[str, Any]:
        runtime_source = self._resolve_runtime_source(
            source_execution_id=source_execution_id,
            task_id=task_id,
        )
        if runtime_source is not None:
            return runtime_source
        return self._resolve_durable_source(
            source_execution_id=source_execution_id,
            task_id=task_id,
        )

    def _resolve_runtime_source(
        self,
        *,
        source_execution_id: str,
        task_id: int | None,
    ) -> dict[str, Any] | None:
        if task_id is None:
            return None
        task_row = self.db.execute(
            select(Task.id, Task.engagement_id).where(Task.id == int(task_id))
        ).first()
        if task_row is None or task_row[1] is None:
            return None
        execution_payload = self.query_service.get_execution_by_id(
            execution_id=source_execution_id,
            task_id=int(task_id),
            include_artifacts=True,
        )
        if execution_payload is None:
            return None
        semantic_input_snapshot = build_semantic_input_snapshot(
            execution=dict(execution_payload.get("execution") or {}),
            artifacts=list(execution_payload.get("artifacts") or []),
        )
        return {
            "source_kind": "runtime",
            "engagement_id": int(task_row[1]),
            "task_id": int(task_id),
            "execution_payload": execution_payload,
            "compact_output_hint": None,
            "semantic_input_snapshot": semantic_input_snapshot,
        }

    def _resolve_durable_source(
        self,
        *,
        source_execution_id: str,
        task_id: int | None,
    ) -> dict[str, Any]:
        run_query = select(KnowledgeIngestionRun).where(
            KnowledgeIngestionRun.source_execution_id == str(source_execution_id)
        )
        if task_id is not None:
            run_query = run_query.where(KnowledgeIngestionRun.task_id == int(task_id))
        run = self.db.execute(
            run_query.order_by(
                KnowledgeIngestionRun.updated_at.desc(),
                KnowledgeIngestionRun.created_at.desc(),
            ).limit(1)
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(
                "Replay source not found: no runtime execution payload and no prior ingestion run"
            )

        archives = self.db.execute(
            select(KnowledgeEvidenceArchive).where(
                KnowledgeEvidenceArchive.engagement_id == int(run.engagement_id),
                KnowledgeEvidenceArchive.source_execution_id == str(source_execution_id),
            )
        ).scalars().all()
        if not archives:
            raise ValueError(
                "Replay source not found: no runtime execution payload and no durable archive rows"
            )

        run_metadata = dict(run.run_metadata or {})
        snapshot_raw = run_metadata.get("semantic_input_snapshot")
        semantic_input_snapshot = dict(snapshot_raw) if isinstance(snapshot_raw, dict) else None
        source_tool_name = run_metadata.get("source_tool_name")
        if (
            isinstance(semantic_input_snapshot, dict)
            and isinstance(semantic_input_snapshot.get("source_tool_name"), str)
            and semantic_input_snapshot.get("source_tool_name")
        ):
            source_tool_name = semantic_input_snapshot.get("source_tool_name")

        execution_metadata: dict[str, Any] = {}
        if isinstance(semantic_input_snapshot, dict):
            execution_metadata = build_replay_execution_metadata_from_snapshot(
                semantic_input_snapshot
            )

        execution_payload = {
            "execution": {
                "execution_id": str(source_execution_id),
                "task_id": run.task_id,
                "tool_name": source_tool_name,
                "tool_call_id": None,
                "status": "durable_replay_source",
                "created_at": str(run.created_at) if run.created_at is not None else None,
            },
            "artifacts": [self._serialize_archive_as_artifact(row) for row in archives],
        }
        if execution_metadata:
            execution_payload["execution"]["execution_metadata"] = execution_metadata
        return {
            "source_kind": "durable_archive",
            "engagement_id": int(run.engagement_id),
            "task_id": run.task_id,
            "execution_payload": execution_payload,
            "compact_output_hint": None,
            "semantic_input_snapshot": semantic_input_snapshot,
        }

    def _serialize_archive_as_artifact(self, row: KnowledgeEvidenceArchive) -> dict[str, Any]:
        lineage = dict(row.lineage_snapshot or {})
        source_artifact_id = str(row.source_artifact_id) if row.source_artifact_id is not None else None
        mime_type = str(row.mime_type or "").strip().lower()
        is_text = bool(row.inline_excerpt is not None) or self._mime_type_is_textual(mime_type)
        content_text = self._read_archive_text_content(row=row, is_text=is_text)
        return {
            "artifact_id": source_artifact_id or f"archive-{row.id}",
            "execution_id": str(row.source_execution_id),
            "task_id": row.task_id,
            "artifact_kind": lineage.get("artifact_kind") or "archived_evidence",
            "relative_path": lineage.get("relative_path"),
            "content_text": content_text,
            "content_sha256": row.content_sha256,
            "byte_size": row.byte_size,
            "mime_type": row.mime_type,
            "is_text": is_text,
            "content_availability": "archived",
            "artifact_metadata": {
                "storage_mode": row.storage_mode,
                "archived_file_ref": row.archived_file_ref,
                "lineage_snapshot": lineage,
            },
        }

    @staticmethod
    def _mime_type_is_textual(mime_type: str) -> bool:
        if not mime_type:
            return False
        if mime_type.startswith("text/"):
            return True
        return mime_type in _TEXTUAL_MIME_TYPES

    def _read_archive_text_content(
        self,
        *,
        row: KnowledgeEvidenceArchive,
        is_text: bool,
    ) -> str | None:
        if isinstance(row.inline_excerpt, str) and row.inline_excerpt.strip():
            return row.inline_excerpt
        if not is_text:
            return None
        object_key = str(row.object_key or "").strip()
        if not object_key:
            return None
        try:
            raw = self._object_store.read_bytes(object_key, max_bytes=_MAX_OBJECT_TEXT_BYTES)
        except Exception:
            return None
        if not raw:
            return None
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return decoded if decoded.strip() else None
