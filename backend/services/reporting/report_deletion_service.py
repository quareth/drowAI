"""Manual report deletion and undo orchestration.

This service owns user-triggered report lifecycle changes and current-report
promotion behavior. It does not perform automatic age-based retention scans.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from backend.core.time_utils import to_utc, utc_now
from backend.models.reporting import EngagementReport
from backend.repositories.reporting.engagement_report_repository import (
    EngagementReportRepository,
)
from backend.schemas.reporting import (
    EngagementReportDeleteResponse,
    EngagementReportUndoDeleteResponse,
)

DEFAULT_REPORT_DELETE_UNDO_SECONDS = 30
REPORT_DELETION_REASON_MANUAL = "manual"


class ReportDeletionError(ValueError):
    """Raised when a report deletion action cannot be applied."""


@dataclass(frozen=True, slots=True)
class _CurrentPointerResult:
    """Current pointer state after a deletion lifecycle mutation."""

    current_report_id: UUID | None


class ReportDeletionService:
    """Schedule and undo scoped report deletion requests."""

    def __init__(
        self,
        db: Session,
        *,
        repository: EngagementReportRepository | None = None,
        undo_seconds: int = DEFAULT_REPORT_DELETE_UNDO_SECONDS,
    ) -> None:
        self._db = db
        self._repository = repository or EngagementReportRepository(db)
        self._undo_seconds = max(1, int(undo_seconds))

    def schedule_delete(
        self,
        *,
        tenant_id: int,
        user_id: int,
        requested_by_user_id: int,
        report_id: str | UUID,
    ) -> EngagementReportDeleteResponse | None:
        """Schedule one report for deletion and hide it from reads immediately."""

        report = self._repository.get_report_by_id_for_lifecycle(
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=report_id,
        )
        if report is None:
            return None
        if report.delete_scheduled_at is not None:
            if report.delete_undo_until is None:
                raise ReportDeletionError("Report deletion is already scheduled.")
            return EngagementReportDeleteResponse(
                report_id=report.id,
                engagement_id=int(report.engagement_id),
                report_type=report.report_type,
                deleted_current=bool(report.deletion_original_is_current),
                current_report_id=self._current_report_id(report),
                undo_until=report.delete_undo_until,
            )

        scheduled_at = utc_now()
        undo_until = scheduled_at + timedelta(seconds=self._undo_seconds)
        was_current = bool(report.is_current)
        self._repository.schedule_report_deletion(
            report=report,
            deleted_by_user_id=int(requested_by_user_id),
            reason=REPORT_DELETION_REASON_MANUAL,
            scheduled_at=scheduled_at,
            undo_until=undo_until,
            metadata={
                "requested_by_user_id": int(requested_by_user_id),
                "original_is_current": was_current,
            },
        )
        pointer = self._promote_current_if_missing(report)
        self._db.commit()
        return EngagementReportDeleteResponse(
            report_id=report.id,
            engagement_id=int(report.engagement_id),
            report_type=report.report_type,
            deleted_current=was_current,
            current_report_id=pointer.current_report_id,
            undo_until=undo_until,
        )

    def undo_delete(
        self,
        *,
        tenant_id: int,
        user_id: int,
        report_id: str | UUID,
    ) -> EngagementReportUndoDeleteResponse | None:
        """Cancel pending report deletion while the undo window is open."""

        report = self._repository.get_report_by_id_for_lifecycle(
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=report_id,
        )
        if report is None:
            return None
        if report.delete_scheduled_at is None:
            raise ReportDeletionError("Report deletion is not pending.")
        if report.delete_undo_until is None or to_utc(report.delete_undo_until) <= utc_now():
            raise ReportDeletionError("Report deletion can no longer be undone.")

        should_restore_current = bool(report.deletion_original_is_current) and (
            self._repository.get_current_ready_report(
                tenant_id=int(report.tenant_id),
                user_id=int(report.user_id),
                engagement_id=int(report.engagement_id),
                report_type=str(report.report_type),
            )
            is None
        )
        self._repository.cancel_report_deletion(report=report)
        if should_restore_current:
            report.is_current = True
            self._db.flush()
            self._db.refresh(report)

        current_report_id = self._current_report_id(report)
        self._db.commit()
        return EngagementReportUndoDeleteResponse(
            report_id=report.id,
            engagement_id=int(report.engagement_id),
            report_type=report.report_type,
            restored_current=bool(should_restore_current),
            current_report_id=current_report_id,
        )

    def _promote_current_if_missing(self, report: EngagementReport) -> _CurrentPointerResult:
        current = self._repository.get_current_ready_report(
            tenant_id=int(report.tenant_id),
            user_id=int(report.user_id),
            engagement_id=int(report.engagement_id),
            report_type=str(report.report_type),
        )
        if current is not None:
            return _CurrentPointerResult(current_report_id=current.id)

        candidates = self._repository.list_ready_reports_for_type(
            tenant_id=int(report.tenant_id),
            user_id=int(report.user_id),
            engagement_id=int(report.engagement_id),
            report_type=str(report.report_type),
        )
        next_current = candidates[0] if candidates else None
        if next_current is None:
            return _CurrentPointerResult(current_report_id=None)
        next_current.is_current = True
        self._db.flush()
        self._db.refresh(next_current)
        return _CurrentPointerResult(current_report_id=next_current.id)

    def _current_report_id(self, report: EngagementReport) -> UUID | None:
        current = self._repository.get_current_ready_report(
            tenant_id=int(report.tenant_id),
            user_id=int(report.user_id),
            engagement_id=int(report.engagement_id),
            report_type=str(report.report_type),
        )
        return current.id if current is not None else None


__all__ = [
    "DEFAULT_REPORT_DELETE_UNDO_SECONDS",
    "REPORT_DELETION_REASON_MANUAL",
    "ReportDeletionError",
    "ReportDeletionService",
]
