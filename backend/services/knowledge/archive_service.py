"""Archive delete-critical evidence with minimal, lineage-preserving records.

Scope:
- Convert runtime artifact evidence into engagement-owned archive rows.

Responsibilities:
- Inspect source artifacts through existing provenance and memory services.
- Choose explicit storage mode per evidence row.
- Persist minimal lineage snapshots without duplicating artifact catalogs.

Boundary:
- This service decides archive policy and writes archive rows.
- File placement and path authority are delegated to storage/path services.
"""

from __future__ import annotations

from pathlib import Path
import logging
from backend.core.time_utils import format_iso, utc_now
from typing import Any
import uuid as uuid_lib

from sqlalchemy.orm import Session
from sqlalchemy import select

from ...config.workspace_config import WorkspaceConfig
from ...models import KnowledgeEvidenceArchive, Task
from ...models.provenance import ExecutionArtifact
from backend.services.artifact.memory_service import ArtifactMemoryService, ArtifactReadRequest
from backend.services.runtime_provider.runtime_artifact_access import (
    decode_runtime_artifact_binary_delegate,
    execute_runtime_artifact_read_sync,
)
from backend.services.artifact.provenance_query_service import ArtifactProvenanceQueryService
from backend.services.runtime_provider.contracts import RuntimeActorType, is_runner_placement_mode
from .evidence_storage_service import (
    EvidenceStorageService,
    is_runner_backed_artifact,
    runner_archive_mode_allowed,
)

logger = logging.getLogger(__name__)


class KnowledgeArchiveService:
    """Decide archive storage mode and persist minimal evidence archive rows."""

    STORAGE_MODE_INLINE_EXCERPT = "inline_excerpt"
    STORAGE_MODE_OBJECT_REF = "object_ref"
    STORAGE_MODE_ARCHIVED_FILE = "archived_file"
    STORAGE_MODE_METADATA_ONLY = "metadata_only"
    TRUE_STORAGE_MODES = frozenset(
        {
            STORAGE_MODE_INLINE_EXCERPT,
            STORAGE_MODE_OBJECT_REF,
            STORAGE_MODE_ARCHIVED_FILE,
            STORAGE_MODE_METADATA_ONLY,
        }
    )
    INLINE_TEXT_MAX_BYTES = 16 * 1024
    EXCERPT_MAX_CHARS = 4000
    HIGH_VALUE_TEXT_KINDS = {
        "command",
        "stdout",
        "stderr",
        "tool_result",
        "http_response",
    }

    def __init__(
        self,
        db: Session,
        *,
        query_service: ArtifactProvenanceQueryService | None = None,
        artifact_memory_service: ArtifactMemoryService | None = None,
        evidence_storage_service: EvidenceStorageService | None = None,
    ) -> None:
        self.db = db
        self.query_service = query_service or ArtifactProvenanceQueryService(db)
        self.artifact_memory_service = artifact_memory_service or ArtifactMemoryService(db)
        self.evidence_storage_service = evidence_storage_service or EvidenceStorageService()

    def archive_execution_artifacts(
        self,
        *,
        engagement_id: int,
        task_id: int,
        execution_id: str,
        delete_survival_required: bool,
    ) -> list[KnowledgeEvidenceArchive]:
        """Archive task-scoped execution artifacts into the durable knowledge plane."""
        execution_payload = self.query_service.get_execution_by_id(
            execution_id=execution_id,
            task_id=task_id,
            include_artifacts=True,
        )
        if execution_payload is None:
            raise ValueError(f"Execution not found for archival: {execution_id}")

        execution = execution_payload.get("execution") or {}
        artifacts = execution_payload.get("artifacts") or []

        created_rows: list[KnowledgeEvidenceArchive] = []
        for artifact in artifacts:
            row = self._archive_one_artifact(
                engagement_id=engagement_id,
                task_id=task_id,
                execution=execution,
                artifact=artifact,
                delete_survival_required=delete_survival_required,
            )
            if row is not None:
                created_rows.append(row)
        self.db.flush()
        return created_rows

    def _archive_one_artifact(
        self,
        *,
        engagement_id: int,
        task_id: int,
        execution: dict[str, Any],
        artifact: dict[str, Any],
        delete_survival_required: bool,
    ) -> KnowledgeEvidenceArchive | None:
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        if not artifact_id:
            return None

        detail = self.query_service.get_artifact_by_id(
            artifact_id=artifact_id,
            task_id=task_id,
            include_content=False,
            include_internal_paths=True,
        )
        if detail is None:
            return None
        artifact_row = self._load_execution_artifact_row(task_id=task_id, artifact_id=artifact_id)
        if artifact_row is None:
            return None
        runner_placement = self._task_uses_runner_placement(task_id=task_id)

        execution_id = str(execution.get("execution_id"))
        user_id, tenant_id = self._resolve_ownership_from_engagement(engagement_id)
        existing = self.db.execute(
            select(KnowledgeEvidenceArchive).where(
                KnowledgeEvidenceArchive.engagement_id == int(engagement_id),
                KnowledgeEvidenceArchive.source_execution_id == execution_id,
                KnowledgeEvidenceArchive.source_artifact_id == str(artifact_id),
            )
        ).scalar_one_or_none()
        if existing is not None:
            if delete_survival_required:
                return self._ensure_existing_row_meets_delete_survival(
                    row=existing,
                    engagement_id=engagement_id,
                    task_id=task_id,
                    execution_id=execution_id,
                    artifact=detail,
                    artifact_row=artifact_row,
                    tenant_id=tenant_id,
                    runner_placement=runner_placement,
                )
            return existing

        evidence_id = uuid_lib.uuid4()
        (
            storage_mode,
            inline_excerpt,
            object_key,
            archived_file_ref,
            resolved_content_sha256,
            resolved_byte_size,
            resolved_mime_type,
        ) = self._select_storage_mode(
            tenant_id=tenant_id,
            engagement_id=engagement_id,
            task_id=task_id,
            evidence_id=str(evidence_id),
            execution_id=execution_id,
            artifact=detail,
            artifact_row=artifact_row,
            delete_survival_required=delete_survival_required,
            runner_placement=runner_placement,
        )
        if is_runner_backed_artifact(
            upload_status=artifact_row.upload_status,
            object_key=artifact_row.object_key,
        ) and not runner_archive_mode_allowed(storage_mode):
            raise ValueError(
                "Runner-backed evidence archive storage mode must be "
                "`object_ref`, `inline_excerpt`, or `metadata_only`"
            )
        row = KnowledgeEvidenceArchive(
            id=evidence_id,
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            task_id=task_id,
            source_execution_id=execution.get("execution_id"),
            source_artifact_id=artifact_id,
            storage_mode=self.normalize_storage_mode(storage_mode),
            inline_excerpt=inline_excerpt,
            object_key=object_key,
            archived_file_ref=archived_file_ref,
            content_sha256=resolved_content_sha256,
            byte_size=resolved_byte_size,
            mime_type=resolved_mime_type,
            lineage_snapshot={
                "task_id": task_id,
                "execution_id": execution.get("execution_id"),
                "tool_name": execution.get("tool_name"),
                "tool_call_id": execution.get("tool_call_id"),
                "artifact_id": detail.get("artifact_id"),
                "artifact_kind": detail.get("artifact_kind"),
                "relative_path": detail.get("relative_path"),
                "content_availability": detail.get("content_availability"),
                "created_at": detail.get("created_at"),
            },
            archive_metadata={
                "policy_family": "default_archive_policy",
                "delete_survival_required": bool(delete_survival_required),
                "storage_authority": "object_store" if storage_mode == "object_ref" else "knowledge_archive_service",
                "path_authority": "workspace_config.engagement_evidence_path"
                if storage_mode == "archived_file"
                else None,
            },
        )
        self.db.add(row)
        return row

    def _resolve_ownership_from_engagement(self, engagement_id: int) -> tuple[int, int]:
        from ...models import Engagement
        from sqlalchemy import select as sa_select
        ownership = self.db.execute(
            sa_select(Engagement.user_id, Engagement.tenant_id).where(
                Engagement.id == int(engagement_id)
            )
        ).one_or_none()
        if ownership is None:
            raise ValueError(f"Engagement not found: {engagement_id}")
        return int(ownership[0]), int(ownership[1])

    def _ensure_existing_row_meets_delete_survival(
        self,
        *,
        row: KnowledgeEvidenceArchive,
        engagement_id: int,
        task_id: int,
        execution_id: str,
        artifact: dict[str, Any],
        artifact_row: ExecutionArtifact,
        tenant_id: int,
        runner_placement: bool,
    ) -> KnowledgeEvidenceArchive:
        runner_backed = is_runner_backed_artifact(
            upload_status=artifact_row.upload_status,
            object_key=artifact_row.object_key,
        ) or bool(runner_placement)
        if runner_backed:
            if self.normalize_storage_mode(str(row.storage_mode or "")) == self.STORAGE_MODE_OBJECT_REF and str(row.object_key or "").strip():
                return row
            preferred_content = row.inline_excerpt
            if preferred_content is None and bool(artifact.get("is_text")):
                preferred_content = self._read_excerpt(
                    task_id=task_id,
                    artifact_id=str(artifact["artifact_id"]),
                )
            object_result = self._materialize_object_reference(
                tenant_id=tenant_id,
                engagement_id=engagement_id,
                evidence_id=str(row.id),
                artifact=artifact,
                artifact_row=artifact_row,
            )
            if object_result is not None:
                row.storage_mode = self.STORAGE_MODE_OBJECT_REF
                row.object_key = object_result.object_key
                row.content_sha256 = object_result.content_sha256
                row.byte_size = object_result.byte_size
                row.mime_type = object_result.mime_type
                row.inline_excerpt = preferred_content
                row.archived_file_ref = None
            elif preferred_content is not None:
                row.storage_mode = self.STORAGE_MODE_INLINE_EXCERPT
                row.inline_excerpt = preferred_content
                row.object_key = None
                row.archived_file_ref = None
            else:
                row.storage_mode = self.STORAGE_MODE_METADATA_ONLY
                row.inline_excerpt = None
                row.object_key = None
                row.archived_file_ref = None

            metadata = dict(row.archive_metadata or {})
            metadata["policy_family"] = metadata.get("policy_family") or "default_archive_policy"
            metadata["delete_survival_required"] = True
            metadata["storage_authority"] = "object_store" if row.storage_mode == self.STORAGE_MODE_OBJECT_REF else "knowledge_archive_service"
            metadata["path_authority"] = None
            row.archive_metadata = metadata
            if not runner_archive_mode_allowed(str(row.storage_mode or "")):
                raise ValueError("Existing runner-backed evidence row resolved to an invalid storage mode")
            return row

        ref = str(row.archived_file_ref or "").strip()
        has_materialized_archive = (
            row.storage_mode == "archived_file"
            and bool(ref)
            and not ref.startswith("pending://")
        )
        if not has_materialized_archive:
            preferred_content = row.inline_excerpt
            if preferred_content is None and bool(artifact.get("is_text")):
                preferred_content = self._read_excerpt(
                    task_id=task_id,
                    artifact_id=str(artifact["artifact_id"]),
                )
            row.storage_mode = "archived_file"
            row.inline_excerpt = preferred_content
            row.archived_file_ref = self._materialize_archived_file(
                engagement_id=engagement_id,
                task_id=task_id,
                execution_id=execution_id,
                artifact=artifact,
                preferred_content=preferred_content,
            )

        metadata = dict(row.archive_metadata or {})
        metadata["policy_family"] = metadata.get("policy_family") or "default_archive_policy"
        metadata["delete_survival_required"] = True
        metadata["path_authority"] = "workspace_config.engagement_evidence_path"
        row.archive_metadata = metadata
        return row

    def compact_archive_to_metadata_only(
        self,
        *,
        evidence_row: KnowledgeEvidenceArchive,
        reason: str,
        replay_policy_status: str,
    ) -> tuple[bool, int]:
        """Downgrade one archive row from archived file to metadata-only mode."""
        current_mode = self.normalize_storage_mode(evidence_row.storage_mode)
        if current_mode != self.STORAGE_MODE_ARCHIVED_FILE:
            return False, 0

        deleted_bytes = 0
        archived_ref = str(evidence_row.archived_file_ref or "").strip()
        deleted_file = False
        if archived_ref and not archived_ref.startswith("pending://"):
            archived_path = self._resolve_safe_archived_ref_path(
                engagement_id=int(evidence_row.engagement_id),
                archived_file_ref=archived_ref,
            )
            if archived_path is not None and archived_path.exists() and archived_path.is_file():
                try:
                    deleted_bytes = int(archived_path.stat().st_size)
                except OSError:
                    deleted_bytes = 0
                try:
                    archived_path.unlink()
                    deleted_file = True
                except OSError:
                    deleted_file = False

        metadata = dict(evidence_row.archive_metadata or {})
        metadata["compaction"] = {
            "compacted_at": format_iso(utc_now()),
            "previous_storage_mode": self.STORAGE_MODE_ARCHIVED_FILE,
            "reason": str(reason or "retention_compaction_policy"),
            "replay_policy_status": str(replay_policy_status or "not_required"),
            "archived_file_deleted": bool(deleted_file),
        }
        metadata["last_storage_mode"] = self.STORAGE_MODE_ARCHIVED_FILE
        metadata["path_authority"] = None
        evidence_row.archive_metadata = metadata
        evidence_row.storage_mode = self.STORAGE_MODE_METADATA_ONLY
        evidence_row.inline_excerpt = None
        evidence_row.archived_file_ref = None
        self.db.flush()
        return True, deleted_bytes

    def _select_storage_mode(
        self,
        *,
        tenant_id: int,
        engagement_id: int,
        task_id: int,
        evidence_id: str,
        execution_id: str,
        artifact: dict[str, Any],
        artifact_row: ExecutionArtifact,
        delete_survival_required: bool,
        runner_placement: bool,
    ) -> tuple[str, str | None, str | None, str | None, str | None, int | None, str | None]:
        artifact_kind = str(artifact.get("artifact_kind") or "")
        byte_size = int(artifact.get("byte_size") or 0)
        is_text = bool(artifact.get("is_text"))
        default_hash = artifact.get("content_sha256")
        default_size = artifact.get("byte_size")
        default_mime = artifact.get("mime_type")

        small_high_value_text = (
            is_text
            and byte_size <= self.INLINE_TEXT_MAX_BYTES
            and artifact_kind in self.HIGH_VALUE_TEXT_KINDS
        )
        if small_high_value_text:
            return (
                self.STORAGE_MODE_INLINE_EXCERPT,
                self._read_excerpt(task_id=task_id, artifact_id=str(artifact["artifact_id"])),
                None,
                None,
                default_hash,
                int(default_size) if default_size is not None else None,
                default_mime,
            )

        runner_backed = is_runner_backed_artifact(
            upload_status=artifact_row.upload_status,
            object_key=artifact_row.object_key,
        ) or bool(runner_placement)
        if runner_backed and delete_survival_required:
            preferred_content = self._read_excerpt(task_id=task_id, artifact_id=str(artifact["artifact_id"])) if is_text else None
            object_result = self._materialize_object_reference(
                tenant_id=tenant_id,
                engagement_id=engagement_id,
                evidence_id=evidence_id,
                artifact=artifact,
                artifact_row=artifact_row,
            )
            if object_result is not None:
                return (
                    self.STORAGE_MODE_OBJECT_REF,
                    preferred_content,
                    object_result.object_key,
                    None,
                    object_result.content_sha256,
                    object_result.byte_size,
                    object_result.mime_type,
                )
            if preferred_content is not None:
                return (
                    self.STORAGE_MODE_INLINE_EXCERPT,
                    preferred_content,
                    None,
                    None,
                    default_hash,
                    int(default_size) if default_size is not None else None,
                    default_mime,
                )
            return (
                self.STORAGE_MODE_METADATA_ONLY,
                None,
                None,
                None,
                default_hash,
                int(default_size) if default_size is not None else None,
                default_mime,
            )

        if is_text:
            excerpt = self._read_excerpt(task_id=task_id, artifact_id=str(artifact["artifact_id"]))
            if delete_survival_required:
                archived_ref = self._materialize_archived_file(
                    engagement_id=engagement_id,
                    task_id=task_id,
                    execution_id=execution_id,
                    artifact=artifact,
                    preferred_content=excerpt,
                )
                return (
                    self.STORAGE_MODE_ARCHIVED_FILE,
                    excerpt,
                    None,
                    archived_ref,
                    default_hash,
                    int(default_size) if default_size is not None else None,
                    default_mime,
                )
            return (
                self.STORAGE_MODE_INLINE_EXCERPT,
                excerpt,
                None,
                None,
                default_hash,
                int(default_size) if default_size is not None else None,
                default_mime,
            )

        if delete_survival_required:
            archived_ref = self._materialize_archived_file(
                engagement_id=engagement_id,
                task_id=task_id,
                execution_id=execution_id,
                artifact=artifact,
                preferred_content=None,
            )
            return (
                self.STORAGE_MODE_ARCHIVED_FILE,
                None,
                None,
                archived_ref,
                default_hash,
                int(default_size) if default_size is not None else None,
                default_mime,
            )
        return (
            self.STORAGE_MODE_METADATA_ONLY,
            None,
            None,
            None,
            default_hash,
            int(default_size) if default_size is not None else None,
            default_mime,
        )

    def _read_excerpt(self, *, task_id: int, artifact_id: str) -> str | None:
        read_result = self.artifact_memory_service.read_task_artifact(
            task_id=task_id,
            artifact_id=artifact_id,
            request=ArtifactReadRequest(mode="head", max_chars=self.EXCERPT_MAX_CHARS),
        )
        if read_result.status in {"ready", "omitted_by_policy"} and read_result.content:
            return str(read_result.content)
        return None

    def _materialize_object_reference(
        self,
        *,
        tenant_id: int,
        engagement_id: int,
        evidence_id: str,
        artifact: dict[str, Any],
        artifact_row: ExecutionArtifact,
    ):
        return self.evidence_storage_service.materialize_object_reference(
            tenant_id=int(tenant_id),
            engagement_id=int(engagement_id),
            evidence_id=str(evidence_id),
            artifact_id=str(artifact.get("artifact_id") or ""),
            source_object_key=str(artifact_row.object_key or ""),
            source_relative_path=str(artifact.get("relative_path") or ""),
            mime_type=str(artifact.get("mime_type") or "") or None,
        )

    def _materialize_archived_file(
        self,
        *,
        engagement_id: int,
        task_id: int,
        execution_id: str,
        artifact: dict[str, Any],
        preferred_content: str | None,
    ) -> str:
        evidence_dir = WorkspaceConfig.ensure_engagement_durable_structure(engagement_id)["evidence"]
        artifact_id = str(artifact["artifact_id"])
        artifact_bytes = self._read_runtime_artifact_bytes(task_id=task_id, artifact=artifact)
        if artifact_bytes is not None:
            suffix = Path(str(artifact.get("relative_path") or artifact_id)).suffix or ".bin"
            output_path = evidence_dir / f"execution-{execution_id}_artifact-{artifact_id}{suffix}"
            output_path.write_bytes(artifact_bytes)
            return str(output_path.resolve())

        text_to_store = preferred_content
        if text_to_store is None and bool(artifact.get("is_text")):
            full_read = self.artifact_memory_service.read_task_artifact(
                task_id=task_id,
                artifact_id=artifact_id,
                request=ArtifactReadRequest(mode="full", max_chars=self.EXCERPT_MAX_CHARS * 5),
            )
            if full_read.status in {"ready", "omitted_by_policy"} and full_read.content:
                text_to_store = str(full_read.content)

        if text_to_store is not None:
            output_path = evidence_dir / f"execution-{execution_id}_artifact-{artifact_id}.txt"
            output_path.write_text(text_to_store, encoding="utf-8")
            return str(output_path.resolve())

        return self._pending_archive_reference(
            engagement_id=engagement_id,
            execution_id=execution_id,
            artifact_id=artifact_id,
        )

    @staticmethod
    def _pending_archive_reference(
        *,
        engagement_id: int,
        execution_id: str,
        artifact_id: str,
    ) -> str:
        return (
            f"pending://durable-knowledge/engagement-{engagement_id}"
            f"/execution-{execution_id}/artifact-{artifact_id}"
        )

    @classmethod
    def normalize_storage_mode(cls, value: str | None) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in cls.TRUE_STORAGE_MODES:
            return normalized
        return cls.STORAGE_MODE_METADATA_ONLY

    @staticmethod
    def _resolve_safe_archived_ref_path(*, engagement_id: int, archived_file_ref: str) -> Path | None:
        normalized_ref = str(archived_file_ref or "").strip()
        if not normalized_ref:
            return None
        if normalized_ref.startswith("pending://"):
            return None

        root = WorkspaceConfig.get_engagement_durable_root_path(int(engagement_id)).resolve()
        ref_path = Path(normalized_ref)
        try:
            candidate = ref_path.resolve() if ref_path.is_absolute() else (root / ref_path).resolve()
        except Exception:
            return None
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    def _read_runtime_artifact_bytes(self, *, task_id: int, artifact: dict[str, Any]) -> bytes | None:
        for candidate in (artifact.get("source_path"), artifact.get("fallback_path"), artifact.get("relative_path")):
            if not candidate:
                continue
            result = execute_runtime_artifact_read_sync(
                self.db,
                task_id=int(task_id),
                path=str(candidate),
                actor_type=RuntimeActorType.SYSTEM,
                actor_id="knowledge_archive",
                binary=True,
                log_context="knowledge archive runtime read",
            )
            data, _resolved_path = decode_runtime_artifact_binary_delegate(
                result,
                fallback_path=str(candidate),
            )
            if data is not None:
                return data
        return None

    def _task_uses_runner_placement(self, *, task_id: int) -> bool:
        row = self.db.query(Task.runtime_placement_mode).filter(Task.id == int(task_id)).one_or_none()
        mode = row[0] if row is not None else None
        return is_runner_placement_mode(mode)

    def _load_execution_artifact_row(self, *, task_id: int, artifact_id: str) -> ExecutionArtifact | None:
        try:
            parsed_artifact_id = uuid_lib.UUID(str(artifact_id))
        except (TypeError, ValueError, AttributeError):
            return None
        return self.db.execute(
            select(ExecutionArtifact).where(
                ExecutionArtifact.task_id == int(task_id),
                ExecutionArtifact.id == parsed_artifact_id,
            )
        ).scalar_one_or_none()

    @staticmethod
    def _is_within_workspace(*, candidate: Path, workspace: Path) -> bool:
        try:
            candidate.relative_to(workspace)
            return True
        except ValueError:
            return False
