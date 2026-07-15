"""Read-only report and report-job projections for engagement reporting.

This service validates reporting inputs, delegates scoped reads to the
reporting repository, and returns stable API schema shapes without creating
generation jobs or mutating report rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy.orm import Session

from backend.repositories.reporting.engagement_report_job_repository import (
    EngagementReportJobRepository,
)
from backend.repositories.reporting.engagement_report_repository import (
    EngagementReportRepository,
)
from backend.schemas.reporting import (
    CurrentEngagementReportResponse,
    EngagementReportActiveJobResponse,
    EngagementReportHistoryResponse,
    EngagementReportHistoryItem,
    EngagementReportJobStatusResponse,
    EngagementReportReadResponse,
    ReportLibraryItem,
    ReportLibraryResponse,
)
from backend.services.reporting.contracts import ReportType, validate_report_type
from backend.services.reporting.report_failure_details import report_job_failure_details


class ReportReadService:
    """Project persisted report rows into read-only response schemas."""

    def __init__(self, db: Session) -> None:
        self._report_repository = EngagementReportRepository(db)
        self._job_repository = EngagementReportJobRepository(db)

    def get_current_report(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_type: str,
    ) -> CurrentEngagementReportResponse:
        """Return the current ready report, or a stable empty current shape."""

        validated_report_type = _validated_report_type(report_type)
        report = self._report_repository.get_current_ready_report(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            report_type=validated_report_type,
        )
        return CurrentEngagementReportResponse(
            engagement_id=int(engagement_id),
            report_type=validated_report_type,
            report=_full_report(report) if report is not None else None,
        )

    def list_report_history(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        report_type: str,
        limit: int = 50,
        offset: int = 0,
    ) -> EngagementReportHistoryResponse:
        """Return report history, or a stable empty list when no rows exist."""

        validated_report_type = _validated_report_type(report_type)
        reports = self._report_repository.list_report_history(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            report_type=validated_report_type,
            limit=limit,
            offset=offset,
        )
        return EngagementReportHistoryResponse(
            engagement_id=int(engagement_id),
            report_type=validated_report_type,
            reports=[_compact_report_summary(report) for report in reports],
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
    ) -> ReportLibraryResponse:
        """Return generated report artifacts owned by one tenant/user."""

        validated_report_type = (
            _validated_report_type(report_type) if report_type is not None else None
        )
        safe_limit = max(1, min(int(limit), 100))
        safe_offset = max(0, int(offset))
        reports = self._report_repository.list_report_library(
            tenant_id=tenant_id,
            user_id=user_id,
            report_type=validated_report_type,
            engagement_id=engagement_id,
            query=query,
            limit=safe_limit,
            offset=safe_offset,
        )
        total = self._report_repository.count_report_library(
            tenant_id=tenant_id,
            user_id=user_id,
            report_type=validated_report_type,
            engagement_id=engagement_id,
            query=query,
        )
        return ReportLibraryResponse(
            reports=[_library_report_summary(report) for report in reports],
            total=total,
            limit=safe_limit,
            offset=safe_offset,
        )

    def get_report(
        self,
        *,
        tenant_id: int,
        user_id: int,
        report_id: str | UUID,
    ) -> EngagementReportReadResponse | None:
        """Return one scoped report with full section content, or None."""

        report = self._report_repository.get_report_by_id_for_owned_engagement(
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=report_id,
        )
        if report is None:
            return None
        return EngagementReportReadResponse.model_validate(report)

    def get_job_status_by_id(
        self,
        *,
        tenant_id: int,
        user_id: int,
        requested_by_user_id: int,
        job_id: str | UUID,
    ) -> EngagementReportJobStatusResponse | None:
        """Return one scoped requester-owned report job, or None."""

        job = self._job_repository.get_report_job_by_id_for_requester(
            tenant_id=tenant_id,
            user_id=user_id,
            requested_by_user_id=requested_by_user_id,
            job_id=job_id,
        )
        if job is None:
            return None
        return _job_status(job, repository=self._report_repository)

    def get_job_status(
        self,
        *,
        tenant_id: int,
        user_id: int,
        requested_by_user_id: int,
        engagement_id: int,
        job_id: str | UUID,
    ) -> EngagementReportJobStatusResponse | None:
        """Return one scoped requester-owned engagement report job, or None."""

        job = self._job_repository.get_report_job_by_id_for_requester(
            tenant_id=tenant_id,
            user_id=user_id,
            requested_by_user_id=requested_by_user_id,
            job_id=job_id,
        )
        if job is None or int(job.engagement_id) != int(engagement_id):
            return None
        return _job_status(job, repository=self._report_repository)

    def get_active_job(
        self,
        *,
        tenant_id: int,
        user_id: int,
        requested_by_user_id: int,
        engagement_id: int,
        report_type: str,
    ) -> EngagementReportActiveJobResponse:
        """Return the latest active scoped report job, if one exists."""

        validated_report_type = _validated_report_type(report_type)
        job = self._job_repository.get_active_report_job_for_requester(
            tenant_id=tenant_id,
            user_id=user_id,
            requested_by_user_id=requested_by_user_id,
            engagement_id=engagement_id,
            report_type=validated_report_type,
        )
        return EngagementReportActiveJobResponse(
            job=(
                _job_status(job, repository=self._report_repository)
                if job is not None
                else None
            )
        )


def _validated_report_type(report_type: str) -> ReportType:
    return validate_report_type(report_type)


def _full_report(report: object) -> EngagementReportReadResponse:
    return EngagementReportReadResponse.model_validate(report)


def _compact_report_summary(report: object) -> EngagementReportHistoryItem:
    return EngagementReportHistoryItem.model_validate(report)


def _library_report_summary(report: object) -> ReportLibraryItem:
    return ReportLibraryItem.model_validate(
        {
            "report_id": getattr(report, "id"),
            "engagement_id": int(getattr(report, "engagement_id")),
            "engagement_name_snapshot": getattr(report, "engagement_name_snapshot", None),
            "engagement_status_snapshot": getattr(report, "engagement_status_snapshot", None),
            "report_type": getattr(report, "report_type"),
            "version": int(getattr(report, "version")),
            "status": getattr(report, "status"),
            "is_current": bool(getattr(report, "is_current")),
            "title": getattr(report, "title"),
            "source_task_count": len(getattr(report, "source_task_memo_ids", None) or []),
            "source_knowledge_count": len(getattr(report, "source_knowledge_refs", None) or []),
            "source_evidence_count": len(getattr(report, "source_evidence_refs", None) or []),
            "created_at": getattr(report, "created_at"),
            "updated_at": getattr(report, "updated_at"),
            "generated_at": getattr(report, "generated_at"),
        }
    )


def _job_status(
    job: object,
    *,
    repository: EngagementReportRepository,
) -> EngagementReportJobStatusResponse:
    response = EngagementReportJobStatusResponse.model_validate(job)
    report_id = getattr(job, "report_id", None)
    if report_id is None:
        return response

    report = repository.get_report_by_id(
        tenant_id=int(getattr(job, "tenant_id")),
        user_id=int(getattr(job, "user_id")),
        engagement_id=int(getattr(job, "engagement_id")),
        report_id=report_id,
    )
    if report is None:
        return response

    metadata = report.generation_metadata
    details = report_job_failure_details(
        metadata if isinstance(metadata, Mapping) else None
    )
    if details is None:
        return response
    return EngagementReportJobStatusResponse.model_validate(
        {
            **response.model_dump(),
            "failure_details": details,
        }
    )


__all__ = ["ReportReadService"]
