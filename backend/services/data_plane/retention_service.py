"""Data-plane retention helpers for task-scoped artifact object cleanup.

Scope:
- Evaluate retention decisions for `ExecutionArtifact` rows with object keys.
- Delete runtime-ephemeral artifact objects only when durable evidence policy allows.
- Return dry-run-safe summaries without exposing object keys in outputs.

Boundary:
- This service only handles artifact object-key retention mechanics.
- Durable evidence policy decisions are owned by knowledge retention services.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.time_utils import format_iso, utc_now
from backend.models.provenance import ExecutionArtifact
from backend.services.data_plane.object_store import ObjectStore
from backend.services.data_plane.registry import get_object_store

ArtifactRetentionAction = Literal["preserve", "eligible_for_delete"]
RETENTION_CLASS_ARTIFACT_PAYLOAD = "artifact_payload"


@dataclass(frozen=True, slots=True)
class ArtifactObjectRetentionDecision:
    """One retention policy decision for an object-backed execution artifact row."""

    artifact_id: str
    execution_id: str
    task_id: int
    retention_class: str
    action: ArtifactRetentionAction
    reason: str
    estimated_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "retention_class": self.retention_class,
            "action": self.action,
            "reason": self.reason,
            "estimated_bytes": int(self.estimated_bytes),
        }


@dataclass(frozen=True, slots=True)
class ArtifactObjectRetentionResult:
    """Dry-run-safe summary for one artifact-object retention run."""

    dry_run: bool
    executed_at: str
    retention_class: str
    decisions: tuple[ArtifactObjectRetentionDecision, ...]
    deleted_count: int
    already_deleted_count: int
    failed_count: int
    deleted_bytes: int

    def to_dict(self) -> dict[str, Any]:
        candidate = [item.to_dict() for item in self.decisions if item.action == "eligible_for_delete"]
        preserved = [item.to_dict() for item in self.decisions if item.action != "eligible_for_delete"]
        estimated_delete_bytes = sum(item.estimated_bytes for item in self.decisions if item.action == "eligible_for_delete")
        return {
            "dry_run": self.dry_run,
            "executed_at": self.executed_at,
            "retention_class": self.retention_class,
            "decision_count": len(self.decisions),
            "candidate_count": len(candidate),
            "preserved_count": len(preserved),
            "deleted_count": int(self.deleted_count),
            "already_deleted_count": int(self.already_deleted_count),
            "failed_count": int(self.failed_count),
            "estimated_delete_bytes": int(estimated_delete_bytes),
            "deleted_bytes": int(self.deleted_bytes),
            "eligible": candidate,
            "preserved": preserved,
        }


class DataPlaneRetentionService:
    """Evaluate and apply task-scoped artifact object retention decisions."""

    RUNTIME_EPHEMERAL_CLASS = RETENTION_CLASS_ARTIFACT_PAYLOAD

    def __init__(
        self,
        db: Session,
        *,
        object_store: ObjectStore | None = None,
    ) -> None:
        self._db = db
        self._object_store = object_store or get_object_store()

    def run_artifact_object_retention(
        self,
        *,
        tenant_task_scopes: set[tuple[int, int]],
        archived_artifact_ids: set[str],
        protected_artifact_ids: set[str],
        created_before: datetime | None = None,
        limit_per_tenant: int | None = None,
        dry_run: bool = True,
    ) -> ArtifactObjectRetentionResult:
        """Apply retention policy to artifact object keys with dry-run support."""
        scoped_pairs = self._normalize_scope_pairs(tenant_task_scopes)
        rows = self._load_object_backed_rows(
            scope_pairs=scoped_pairs,
            created_before=created_before,
            limit_per_tenant=self._normalize_optional_positive_int(
                limit_per_tenant,
                field_name="limit_per_tenant",
            ),
        )
        decisions = tuple(
            self._build_decision(
                row=row,
                archived_artifact_ids=archived_artifact_ids,
                protected_artifact_ids=protected_artifact_ids,
            )
            for row in rows
        )

        deleted_count = 0
        already_deleted_count = 0
        failed_count = 0
        deleted_bytes = 0
        if not dry_run:
            deleted_count, already_deleted_count, failed_count, deleted_bytes = self._apply_deletions(
                rows=rows,
                decisions=decisions,
            )

        return ArtifactObjectRetentionResult(
            dry_run=bool(dry_run),
            executed_at=format_iso(utc_now()),
            retention_class=self.RUNTIME_EPHEMERAL_CLASS,
            decisions=decisions,
            deleted_count=deleted_count,
            already_deleted_count=already_deleted_count,
            failed_count=failed_count,
            deleted_bytes=deleted_bytes,
        )

    def _load_object_backed_rows(
        self,
        *,
        scope_pairs: tuple[tuple[int, int], ...],
        created_before: datetime | None,
        limit_per_tenant: int | None,
    ) -> list[ExecutionArtifact]:
        if not scope_pairs:
            return []
        task_ids_by_tenant: dict[int, set[int]] = {}
        for tenant_id, task_id in scope_pairs:
            task_ids_by_tenant.setdefault(int(tenant_id), set()).add(int(task_id))

        rows: list[ExecutionArtifact] = []
        for tenant_id in sorted(task_ids_by_tenant):
            tenant_task_ids = tuple(sorted(task_ids_by_tenant[tenant_id]))
            if not tenant_task_ids:
                continue
            query = (
                select(ExecutionArtifact)
                .where(
                    ExecutionArtifact.tenant_id == tenant_id,
                    ExecutionArtifact.task_id.in_(tenant_task_ids),
                    ExecutionArtifact.object_key.is_not(None),
                )
                .order_by(ExecutionArtifact.created_at.asc(), ExecutionArtifact.id.asc())
            )
            if created_before is not None:
                query = query.where(ExecutionArtifact.created_at < created_before)
            if limit_per_tenant is not None:
                query = query.limit(int(limit_per_tenant))
            rows.extend(self._db.execute(query).scalars())
        return rows

    def _build_decision(
        self,
        *,
        row: ExecutionArtifact,
        archived_artifact_ids: set[str],
        protected_artifact_ids: set[str],
    ) -> ArtifactObjectRetentionDecision:
        artifact_id = str(row.id)
        normalized_key = str(row.object_key or "").strip()
        estimated_bytes = self._estimate_object_bytes(row=row, object_key=normalized_key)
        if not normalized_key:
            return ArtifactObjectRetentionDecision(
                artifact_id=artifact_id,
                execution_id=str(row.execution_id),
                task_id=int(row.task_id),
                retention_class=self.RUNTIME_EPHEMERAL_CLASS,
                action="preserve",
                reason="object_key_missing",
                estimated_bytes=estimated_bytes,
            )
        if artifact_id in protected_artifact_ids:
            return ArtifactObjectRetentionDecision(
                artifact_id=artifact_id,
                execution_id=str(row.execution_id),
                task_id=int(row.task_id),
                retention_class=self.RUNTIME_EPHEMERAL_CLASS,
                action="preserve",
                reason="durable_evidence_policy_protected",
                estimated_bytes=estimated_bytes,
            )
        if artifact_id not in archived_artifact_ids:
            return ArtifactObjectRetentionDecision(
                artifact_id=artifact_id,
                execution_id=str(row.execution_id),
                task_id=int(row.task_id),
                retention_class=self.RUNTIME_EPHEMERAL_CLASS,
                action="preserve",
                reason="durable_evidence_missing",
                estimated_bytes=estimated_bytes,
            )
        return ArtifactObjectRetentionDecision(
            artifact_id=artifact_id,
            execution_id=str(row.execution_id),
            task_id=int(row.task_id),
            retention_class=self.RUNTIME_EPHEMERAL_CLASS,
            action="eligible_for_delete",
            reason="durable_evidence_retained",
            estimated_bytes=estimated_bytes,
        )

    def _apply_deletions(
        self,
        *,
        rows: list[ExecutionArtifact],
        decisions: tuple[ArtifactObjectRetentionDecision, ...],
    ) -> tuple[int, int, int, int]:
        by_id = {str(item.artifact_id): item for item in decisions}
        deleted_count = 0
        already_deleted_count = 0
        failed_count = 0
        deleted_bytes = 0
        touched_rows = 0

        for row in rows:
            artifact_id = str(row.id)
            decision = by_id.get(artifact_id)
            if decision is None or decision.action != "eligible_for_delete":
                continue
            object_key = str(row.object_key or "").strip()
            if not object_key:
                continue

            delete_exception: Exception | None = None
            try:
                deleted = bool(self._object_store.delete_object(object_key))
            except Exception as exc:
                deleted = False
                delete_exception = exc

            metadata = dict(row.artifact_metadata or {})
            retention_meta = dict(metadata.get("retention") or {})
            failure_message = ""
            if delete_exception is not None:
                failure_message = (
                    f"{type(delete_exception).__name__}: {delete_exception}"
                    if str(delete_exception)
                    else type(delete_exception).__name__
                )
            retention_meta.update(
                {
                    "object_deleted": bool(deleted),
                    "delete_status": "delete_failed" if delete_exception else ("deleted" if deleted else "already_absent"),
                    "delete_error": failure_message,
                    "deleted_at": self._now_iso(),
                    "reason": decision.reason,
                }
            )
            metadata["retention"] = retention_meta
            row.artifact_metadata = metadata
            if delete_exception is None:
                row.object_key = None
            self._db.add(row)
            touched_rows += 1

            if deleted:
                deleted_count += 1
                deleted_bytes += int(decision.estimated_bytes)
            elif delete_exception is not None:
                failed_count += 1
            elif delete_exception is None:
                already_deleted_count += 1

        if touched_rows > 0:
            self._db.flush()
        return deleted_count, already_deleted_count, failed_count, deleted_bytes

    def _estimate_object_bytes(self, *, row: ExecutionArtifact, object_key: str) -> int:
        if row.byte_size is not None:
            return max(0, int(row.byte_size))
        if not object_key:
            return 0
        try:
            head = self._object_store.head_object(object_key)
        except Exception:
            return 0
        if head is None:
            return 0
        return max(0, int(head.byte_size))

    @staticmethod
    def _now_iso() -> str:
        return format_iso(utc_now())

    @staticmethod
    def _normalize_scope_pairs(scope_pairs: set[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
        normalized: set[tuple[int, int]] = set()
        for tenant_id, task_id in scope_pairs:
            try:
                scoped_tenant_id = int(tenant_id)
                scoped_task_id = int(task_id)
            except (TypeError, ValueError):
                continue
            if scoped_tenant_id <= 0 or scoped_task_id <= 0:
                continue
            normalized.add((scoped_tenant_id, scoped_task_id))
        return tuple(sorted(normalized))

    @staticmethod
    def _normalize_optional_positive_int(value: int | None, *, field_name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be a positive integer")
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a positive integer") from exc
        if normalized < 1:
            raise ValueError(f"{field_name} must be positive")
        return normalized
