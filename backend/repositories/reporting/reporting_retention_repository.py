"""Retention persistence primitives for reporting maintenance.

This module owns tenant-filtered candidate/protection queries, optionally global
pending-deletion queries, and caller-selected row deletes; retention policy and
report tombstone content finalization are excluded.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func

from backend.models.reporting import (
    EngagementReport,
    EngagementReportJob,
    TaskClosureMemo,
)
from backend.repositories.reporting.base import ReportingRepositoryBase
from backend.services.reporting.contracts import (
    MEMO_STATUS_READY,
    REPORT_JOB_STATUS_FAILED,
    REPORT_JOB_STATUS_READY,
    REPORT_STATUS_READY,
)


class ReportingRetentionRepository(ReportingRepositoryBase):
    """Query scoped candidates or optionally global pending deletions.

    Delete primitives act on caller-selected rows.
    """

    def list_reports_pending_deletion(
        self,
        *,
        now: datetime,
        tenant_id: int | None = None,
        limit: int = 100,
    ) -> list[EngagementReport]:
        """Return reports whose undo window has expired."""

        query = self.db.query(EngagementReport).filter(
            EngagementReport.delete_scheduled_at.is_not(None),
            EngagementReport.delete_undo_until.is_not(None),
            EngagementReport.delete_undo_until <= now,
            EngagementReport.deletion_finalized_at.is_(None),
        )
        if tenant_id is not None:
            query = query.filter(EngagementReport.tenant_id == int(tenant_id))
        return (
            query.order_by(EngagementReport.delete_undo_until.asc())
            .limit(max(1, int(limit)))
            .all()
        )

    def list_retention_candidate_reports(
        self,
        *,
        tenant_id: int,
        generated_before: datetime,
        limit: int = 100,
    ) -> list[EngagementReport]:
        """Return historical ready reports eligible for automatic retention."""

        return (
            self.db.query(EngagementReport)
            .filter(
                EngagementReport.tenant_id == int(tenant_id),
                EngagementReport.status == REPORT_STATUS_READY,
                EngagementReport.is_current.is_(False),
                EngagementReport.delete_scheduled_at.is_(None),
                EngagementReport.deletion_finalized_at.is_(None),
                EngagementReport.generated_at.is_not(None),
                EngagementReport.generated_at < generated_before,
            )
            .order_by(EngagementReport.generated_at.asc())
            .limit(max(1, int(limit)))
            .all()
        )

    def count_retention_protected_current_reports(
        self,
        *,
        tenant_id: int,
        generated_before: datetime,
    ) -> int:
        """Count current ready reports matching the historical retention window."""

        return int(
            self.db.query(func.count(EngagementReport.id))
            .filter(
                EngagementReport.tenant_id == int(tenant_id),
                EngagementReport.status == REPORT_STATUS_READY,
                EngagementReport.is_current.is_(True),
                EngagementReport.delete_scheduled_at.is_(None),
                EngagementReport.deletion_finalized_at.is_(None),
                EngagementReport.generated_at.is_not(None),
                EngagementReport.generated_at < generated_before,
            )
            .scalar()
            or 0
        )

    def list_retention_candidate_report_jobs(
        self,
        *,
        tenant_id: int,
        finished_before: datetime,
        limit: int = 100,
    ) -> list[EngagementReportJob]:
        """Return terminal report jobs eligible for automatic retention."""

        job_age = func.coalesce(
            EngagementReportJob.finished_at,
            EngagementReportJob.updated_at,
            EngagementReportJob.created_at,
        )
        return (
            self.db.query(EngagementReportJob)
            .filter(
                EngagementReportJob.tenant_id == int(tenant_id),
                EngagementReportJob.status.in_(
                    (REPORT_JOB_STATUS_READY, REPORT_JOB_STATUS_FAILED)
                ),
                job_age < finished_before,
            )
            .order_by(job_age.asc(), EngagementReportJob.created_at.asc())
            .limit(max(1, int(limit)))
            .all()
        )

    def delete_report_jobs(self, jobs: Sequence[EngagementReportJob]) -> int:
        """Delete report job rows and return the number scheduled for deletion."""

        deleted_count = 0
        for job in jobs:
            self.db.delete(job)
            deleted_count += 1
        if deleted_count:
            self.db.flush()
        return deleted_count

    def list_retention_candidate_task_memos(
        self,
        *,
        tenant_id: int,
        memo_before: datetime,
        limit: int = 100,
    ) -> list[TaskClosureMemo]:
        """Return non-current task memo history eligible for retention cleanup."""

        memo_age = func.coalesce(
            TaskClosureMemo.generated_at,
            TaskClosureMemo.updated_at,
            TaskClosureMemo.created_at,
        )
        return (
            self.db.query(TaskClosureMemo)
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.is_current.is_(False),
                memo_age < memo_before,
            )
            .order_by(memo_age.asc(), TaskClosureMemo.created_at.asc())
            .limit(max(1, int(limit)))
            .all()
        )

    def count_retention_protected_current_task_memos(
        self,
        *,
        tenant_id: int,
        memo_before: datetime,
    ) -> int:
        """Count current ready memos matching the memo history retention window."""

        memo_age = func.coalesce(
            TaskClosureMemo.generated_at,
            TaskClosureMemo.updated_at,
            TaskClosureMemo.created_at,
        )
        return int(
            self.db.query(func.count(TaskClosureMemo.id))
            .filter(
                TaskClosureMemo.tenant_id == int(tenant_id),
                TaskClosureMemo.status == MEMO_STATUS_READY,
                TaskClosureMemo.is_current.is_(True),
                memo_age < memo_before,
            )
            .scalar()
            or 0
        )

    def delete_task_memos(self, memos: Sequence[TaskClosureMemo]) -> int:
        """Delete task memo rows and return the number scheduled for deletion."""

        deleted_count = 0
        for memo in memos:
            self.db.delete(memo)
            deleted_count += 1
        if deleted_count:
            self.db.flush()
        return deleted_count
