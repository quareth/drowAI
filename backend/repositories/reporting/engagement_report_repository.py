"""Report artifact persistence for scoped reporting data.

This module owns report artifact, library, lifecycle, and deletion storage;
memo, job-queue, worker, and retention persistence are excluded.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_

from backend.models.reporting import EngagementReport
from backend.repositories.reporting.base import ReportingRepositoryBase
from backend.services.reporting.contracts import (
    ENGAGEMENT_REPORT_SCHEMA_VERSION,
    GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY,
    REPORT_STATUS_FAILED,
    REPORT_STATUS_GENERATING,
    REPORT_STATUS_READY,
)


class EngagementReportRepository(ReportingRepositoryBase):
    """Persist scoped report artifacts and their deletion lifecycle."""

    def get_current_ready_report(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_type: str,
    ) -> EngagementReport | None:
        """Return the current ready report for a tenant/user-owned engagement."""

        return (
            self.db.query(EngagementReport)
            .filter(
                EngagementReport.tenant_id == int(tenant_id),
                EngagementReport.user_id == int(user_id),
                EngagementReport.engagement_id == int(engagement_id),
                EngagementReport.report_type == str(report_type),
                EngagementReport.status == REPORT_STATUS_READY,
                EngagementReport.is_current.is_(True),
                EngagementReport.delete_scheduled_at.is_(None),
                EngagementReport.deletion_finalized_at.is_(None),
            )
            .order_by(
                EngagementReport.version.desc(), EngagementReport.created_at.desc()
            )
            .one_or_none()
        )

    def get_report_by_id(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_id: str | uuid.UUID,
    ) -> EngagementReport | None:
        """Return one report constrained by tenant/user/engagement identity."""

        parsed_report_id = self._parse_uuid(report_id)
        if parsed_report_id is None:
            return None

        return (
            self.db.query(EngagementReport)
            .filter(
                EngagementReport.tenant_id == int(tenant_id),
                EngagementReport.user_id == int(user_id),
                EngagementReport.engagement_id == int(engagement_id),
                EngagementReport.id == parsed_report_id,
                EngagementReport.delete_scheduled_at.is_(None),
                EngagementReport.deletion_finalized_at.is_(None),
            )
            .one_or_none()
        )

    def get_report_by_id_for_owned_engagement(
        self,
        *,
        tenant_id: int,
        user_id: int,
        report_id: str | uuid.UUID,
    ) -> EngagementReport | None:
        """Return one report by tenant/user ownership without requiring live engagement."""

        parsed_report_id = self._parse_uuid(report_id)
        if parsed_report_id is None:
            return None

        return (
            self.db.query(EngagementReport)
            .filter(
                EngagementReport.tenant_id == int(tenant_id),
                EngagementReport.user_id == int(user_id),
                EngagementReport.id == parsed_report_id,
                EngagementReport.delete_scheduled_at.is_(None),
                EngagementReport.deletion_finalized_at.is_(None),
            )
            .one_or_none()
        )

    def next_report_version(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_type: str,
    ) -> int:
        """Return the next report version for one scoped report type."""

        latest_version = (
            self.db.query(func.max(EngagementReport.version))
            .filter(
                EngagementReport.tenant_id == int(tenant_id),
                EngagementReport.user_id == int(user_id),
                EngagementReport.engagement_id == int(engagement_id),
                EngagementReport.report_type == str(report_type),
            )
            .scalar()
        )
        return int(latest_version or 0) + 1

    def create_report_attempt(
        self,
        *,
        tenant_id: int,
        user_id: int,
        created_by_user_id: int,
        engagement_id: int,
        report_type: str,
        version: int,
        title: str,
        source_task_memo_ids: Sequence[str | uuid.UUID],
        engagement_name_snapshot: str | None = None,
        engagement_status_snapshot: str | None = None,
        sections: Sequence[dict[str, Any]] | None = None,
        source_knowledge_refs: Sequence[dict[str, Any]] | None = None,
        source_evidence_refs: Sequence[dict[str, Any]] | None = None,
        generation_metadata: dict[str, Any] | None = None,
        markdown_snapshot: str | None = None,
        status: str = REPORT_STATUS_GENERATING,
        error_message: str | None = None,
    ) -> EngagementReport:
        """Insert a scoped report attempt and return the created row."""

        row = EngagementReport(
            schema_version=ENGAGEMENT_REPORT_SCHEMA_VERSION,
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            created_by_user_id=int(created_by_user_id),
            engagement_id=int(engagement_id),
            engagement_name_snapshot=engagement_name_snapshot,
            engagement_status_snapshot=engagement_status_snapshot,
            report_type=str(report_type),
            version=int(version),
            status=str(status),
            is_current=False,
            title=str(title),
            sections=[dict(section) for section in sections or []],
            markdown_snapshot=markdown_snapshot,
            source_task_memo_ids=self._canonical_memo_id_strings(source_task_memo_ids),
            source_knowledge_refs=[dict(ref) for ref in source_knowledge_refs or []],
            source_evidence_refs=[dict(ref) for ref in source_evidence_refs or []],
            generation_metadata=dict(generation_metadata or {}),
            error_message=error_message,
        )
        self.db.add(row)
        self.db.flush()
        self.db.refresh(row)
        return row

    def update_report_sections(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_id: str | uuid.UUID,
        sections: Sequence[dict[str, Any]],
        generation_metadata: dict[str, Any] | None = None,
    ) -> EngagementReport | None:
        """Replace generated sections for one scoped report attempt."""

        row = self.get_report_by_id(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            report_id=report_id,
        )
        if row is None:
            return None

        row.sections = [dict(section) for section in sections]
        if generation_metadata is not None:
            row.generation_metadata = dict(generation_metadata)
        self.db.flush()
        self.db.refresh(row)
        return row

    def merge_report_generation_metadata(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_id: str | uuid.UUID,
        generation_metadata: dict[str, Any],
    ) -> EngagementReport | None:
        """Merge safe diagnostics into one scoped report attempt."""

        row = self.get_report_by_id(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            report_id=report_id,
        )
        if row is None:
            return None

        row.generation_metadata = {
            **dict(row.generation_metadata or {}),
            **dict(generation_metadata),
        }
        self.db.flush()
        self.db.refresh(row)
        return row

    def mark_report_ready(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_id: str | uuid.UUID,
        markdown_snapshot: str,
        source_task_memo_ids: Sequence[str | uuid.UUID],
        source_knowledge_refs: Sequence[dict[str, Any]],
        source_evidence_refs: Sequence[dict[str, Any]],
        generation_metadata: dict[str, Any],
        generated_at: datetime | None = None,
    ) -> EngagementReport | None:
        """Promote one scoped report attempt as the current ready report."""

        row = self.get_report_by_id(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            report_id=report_id,
        )
        if row is None:
            return None

        self.clear_current_ready_reports_for_type(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            report_type=row.report_type,
        )
        row.status = REPORT_STATUS_READY
        row.is_current = True
        row.markdown_snapshot = str(markdown_snapshot)
        row.source_task_memo_ids = self._canonical_memo_id_strings(source_task_memo_ids)
        row.source_knowledge_refs = [dict(ref) for ref in source_knowledge_refs]
        row.source_evidence_refs = [dict(ref) for ref in source_evidence_refs]
        row.generation_metadata = dict(generation_metadata)
        row.error_message = None
        row.generated_at = generated_at or datetime.now(UTC)
        self.db.flush()
        self.db.refresh(row)
        return row

    def mark_report_failed(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_id: str | uuid.UUID,
        error_message: str,
        generation_metadata: dict[str, Any] | None = None,
    ) -> EngagementReport | None:
        """Mark one scoped report attempt failed without changing current reports."""

        row = self.get_report_by_id(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            report_id=report_id,
        )
        if row is None:
            return None

        row.status = REPORT_STATUS_FAILED
        row.is_current = False
        row.generation_metadata = dict(generation_metadata or {})
        row.error_message = str(error_message)
        self.db.flush()
        self.db.refresh(row)
        return row

    def clear_current_ready_reports_for_type(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_type: str,
    ) -> int:
        """Clear current pointers only on ready reports for one scoped type."""

        updated = (
            self.db.query(EngagementReport)
            .filter(
                EngagementReport.tenant_id == int(tenant_id),
                EngagementReport.user_id == int(user_id),
                EngagementReport.engagement_id == int(engagement_id),
                EngagementReport.report_type == str(report_type),
                EngagementReport.status == REPORT_STATUS_READY,
                EngagementReport.is_current.is_(True),
                EngagementReport.delete_scheduled_at.is_(None),
                EngagementReport.deletion_finalized_at.is_(None),
            )
            .update({EngagementReport.is_current: False}, synchronize_session="fetch")
        )
        self.db.flush()
        return int(updated)

    def find_ready_current_report_by_source(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_type: str,
        selected_task_memo_ids: Sequence[str | uuid.UUID],
        source_watermark_hash: str,
        llm_runtime_selection: dict[str, Any] | None = None,
    ) -> EngagementReport | None:
        """Return a current ready report only when its source snapshot matches."""

        expected_memo_ids = self._canonical_memo_id_strings(selected_task_memo_ids)
        if not expected_memo_ids:
            return None

        rows = (
            self.db.query(EngagementReport)
            .filter(
                EngagementReport.tenant_id == int(tenant_id),
                EngagementReport.user_id == int(user_id),
                EngagementReport.engagement_id == int(engagement_id),
                EngagementReport.report_type == str(report_type),
                EngagementReport.status == REPORT_STATUS_READY,
                EngagementReport.is_current.is_(True),
            )
            .order_by(
                EngagementReport.version.desc(), EngagementReport.created_at.desc()
            )
            .all()
        )
        for row in rows:
            generation_metadata = row.generation_metadata or {}
            if not isinstance(generation_metadata, dict):
                continue
            if _reporting_model_identity(
                generation_metadata.get("llm_runtime_selection")
            ) != _reporting_model_identity(llm_runtime_selection):
                continue
            if generation_metadata.get(
                    GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY
            ) != str(source_watermark_hash):
                continue
            if (
                self._canonical_memo_id_strings(row.source_task_memo_ids or [])
                == expected_memo_ids
            ):
                return row
        return None

    def list_report_history(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EngagementReport]:
        """Return report versions for a tenant/user-owned engagement."""

        query = self.db.query(EngagementReport).filter(
            EngagementReport.tenant_id == int(tenant_id),
            EngagementReport.user_id == int(user_id),
            EngagementReport.engagement_id == int(engagement_id),
            EngagementReport.status == REPORT_STATUS_READY,
            EngagementReport.delete_scheduled_at.is_(None),
            EngagementReport.deletion_finalized_at.is_(None),
        )
        if report_type is not None:
            query = query.filter(EngagementReport.report_type == str(report_type))

        return (
            query.order_by(
                EngagementReport.created_at.desc(), EngagementReport.version.desc()
            )
            .offset(max(0, int(offset)))
            .limit(max(1, int(limit)))
            .all()
        )

    def list_report_library(
        self,
        *,
        tenant_id: int,
        user_id: int,
        report_type: str | None = None,
        engagement_id: int | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EngagementReport]:
        """Return ready report artifacts by tenant/user ownership."""

        query_stmt = self._report_library_query(
            tenant_id=tenant_id,
            user_id=user_id,
            report_type=report_type,
            engagement_id=engagement_id,
            query=query,
        )
        return (
            query_stmt.order_by(
                EngagementReport.generated_at.desc().nullslast(),
                EngagementReport.created_at.desc(),
                EngagementReport.version.desc(),
            )
            .offset(max(0, int(offset)))
            .limit(max(1, int(limit)))
            .all()
        )

    def count_report_library(
        self,
        *,
        tenant_id: int,
        user_id: int,
        report_type: str | None = None,
        engagement_id: int | None = None,
        query: str | None = None,
    ) -> int:
        """Count ready report artifacts by tenant/user ownership."""

        return int(
            self._report_library_query(
                tenant_id=tenant_id,
                user_id=user_id,
                report_type=report_type,
                engagement_id=engagement_id,
                query=query,
            )
            .order_by(None)
            .count()
            or 0
        )

    def _report_library_query(
        self,
        *,
        tenant_id: int,
        user_id: int,
        report_type: str | None,
        engagement_id: int | None,
        query: str | None,
    ):
        query_stmt = self.db.query(EngagementReport).filter(
            EngagementReport.tenant_id == int(tenant_id),
            EngagementReport.user_id == int(user_id),
            EngagementReport.status == REPORT_STATUS_READY,
            EngagementReport.delete_scheduled_at.is_(None),
            EngagementReport.deletion_finalized_at.is_(None),
        )
        if report_type is not None:
            query_stmt = query_stmt.filter(
                EngagementReport.report_type == str(report_type)
            )
        if engagement_id is not None:
            query_stmt = query_stmt.filter(
                EngagementReport.engagement_id == int(engagement_id)
            )
        normalized_query = str(query or "").strip().lower()
        if normalized_query:
            like_query = f"%{normalized_query}%"
            query_stmt = query_stmt.filter(
                or_(
                    func.lower(func.coalesce(EngagementReport.title, "")).like(
                        like_query
                    ),
                    func.lower(
                        func.coalesce(EngagementReport.engagement_name_snapshot, "")
                    ).like(like_query),
                )
            )
        return query_stmt

    def get_report_by_id_for_lifecycle(
        self,
        *,
        tenant_id: int,
        user_id: int,
        report_id: str | uuid.UUID,
    ) -> EngagementReport | None:
        """Return one owned report that has not been finalized as deleted."""

        parsed_report_id = self._parse_uuid(report_id)
        if parsed_report_id is None:
            return None

        return (
            self.db.query(EngagementReport)
            .filter(
                EngagementReport.tenant_id == int(tenant_id),
                EngagementReport.user_id == int(user_id),
                EngagementReport.id == parsed_report_id,
                EngagementReport.deletion_finalized_at.is_(None),
            )
            .one_or_none()
        )

    def list_ready_reports_for_type(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_type: str,
        include_pending_deletion: bool = False,
    ) -> list[EngagementReport]:
        """Return ready reports for one scoped type, newest first."""

        query = self.db.query(EngagementReport).filter(
            EngagementReport.tenant_id == int(tenant_id),
            EngagementReport.user_id == int(user_id),
            EngagementReport.engagement_id == int(engagement_id),
            EngagementReport.report_type == str(report_type),
            EngagementReport.status == REPORT_STATUS_READY,
            EngagementReport.deletion_finalized_at.is_(None),
        )
        if not include_pending_deletion:
            query = query.filter(EngagementReport.delete_scheduled_at.is_(None))
        return query.order_by(
            EngagementReport.version.desc(), EngagementReport.created_at.desc()
        ).all()

    def schedule_report_deletion(
        self,
        *,
        report: EngagementReport,
        deleted_by_user_id: int | None,
        reason: str,
        scheduled_at: datetime,
        undo_until: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> EngagementReport:
        """Mark a report pending deletion without erasing content."""

        report.delete_scheduled_at = scheduled_at
        report.delete_undo_until = undo_until
        report.deleted_by_user_id = (
            int(deleted_by_user_id) if deleted_by_user_id is not None else None
        )
        report.deletion_reason = str(reason)
        report.deletion_original_is_current = bool(report.is_current)
        report.deletion_metadata = dict(metadata or {})
        report.is_current = False
        self.db.flush()
        self.db.refresh(report)
        return report

    def cancel_report_deletion(self, *, report: EngagementReport) -> EngagementReport:
        """Clear pending deletion metadata before finalization."""

        report.delete_scheduled_at = None
        report.delete_undo_until = None
        report.deleted_by_user_id = None
        report.deletion_reason = None
        report.deletion_metadata = None
        report.deletion_original_is_current = False
        self.db.flush()
        self.db.refresh(report)
        return report

    def finalize_report_deletion(
        self,
        *,
        report: EngagementReport,
        finalized_at: datetime,
    ) -> EngagementReport:
        """Erase generated report content and leave a minimal tombstone."""

        metadata = dict(report.deletion_metadata or {})
        metadata.update(
            {
                "content_erased": True,
                "content_erased_at": finalized_at.isoformat(),
                "source_task_memo_count": len(report.source_task_memo_ids or []),
                "source_knowledge_ref_count": len(report.source_knowledge_refs or []),
                "source_evidence_ref_count": len(report.source_evidence_refs or []),
            }
        )
        report.sections = []
        report.markdown_snapshot = None
        report.source_task_memo_ids = []
        report.source_knowledge_refs = []
        report.source_evidence_refs = []
        report.generation_metadata = {}
        report.error_message = None
        report.is_current = False
        report.deletion_metadata = metadata
        report.deletion_finalized_at = finalized_at
        self.db.flush()
        self.db.refresh(report)
        return report


def _canonical_json_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _reporting_model_identity(value: Any) -> dict[str, Any]:
    mapping = _canonical_json_mapping(value)
    return {
        "provider": mapping.get("provider"),
        "model": mapping.get("model"),
        "reasoning_effort": mapping.get("reasoning_effort"),
    }
