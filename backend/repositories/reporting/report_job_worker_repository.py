"""Worker-only report-job persistence for internal queue control.

This module owns durable-ID lookup, linking, claim limits, atomic claims,
progress, failure, retry, and stale recovery; requester-scoped access is excluded.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import aliased

from backend.models.reporting import EngagementReportJob
from backend.repositories.reporting.base import ReportingRepositoryBase
from backend.services.reporting.contracts import (
    REPORT_JOB_STATUS_FAILED,
    REPORT_JOB_STATUS_GENERATING,
    REPORT_JOB_STATUS_QUEUED,
)


class ReportJobWorkerRepository(ReportingRepositoryBase):
    """Persist internal worker queue state through durable job identities."""

    def link_report_job_attempt_by_id(
        self,
        *,
        job_id: str | uuid.UUID,
        report_id: str | uuid.UUID,
    ) -> EngagementReportJob | None:
        """Bind a generating job to its current report attempt for recovery."""

        row = self.get_report_job_by_id(job_id=job_id)
        parsed_report_id = self._parse_uuid(report_id)
        if (
            row is None
            or parsed_report_id is None
            or str(row.status) != REPORT_JOB_STATUS_GENERATING
        ):
            return None

        row.report_id = parsed_report_id
        self.db.flush()
        self.db.refresh(row)
        return row

    def list_claimable_report_jobs(
        self,
        *,
        now: datetime,
        limit: int = 25,
    ) -> list[EngagementReportJob]:
        """Return queued report jobs that still have attempts remaining."""

        return (
            self.db.query(EngagementReportJob)
            .filter(
                EngagementReportJob.status == REPORT_JOB_STATUS_QUEUED,
                EngagementReportJob.attempt_count < EngagementReportJob.max_attempts,
                or_(
                    EngagementReportJob.next_attempt_at.is_(None),
                    EngagementReportJob.next_attempt_at <= now,
                ),
            )
            .order_by(
                EngagementReportJob.next_attempt_at.asc().nullsfirst(),
                EngagementReportJob.created_at.asc(),
            )
            .limit(max(1, int(limit)))
            .all()
        )

    def count_active_report_jobs(
        self,
        *,
        tenant_id: int | None = None,
        user_id: int | None = None,
    ) -> int:
        """Return active generating job count, optionally scoped to tenant/user."""

        query = self.db.query(func.count(EngagementReportJob.id)).filter(
            EngagementReportJob.status == REPORT_JOB_STATUS_GENERATING
        )
        if tenant_id is not None:
            query = query.filter(EngagementReportJob.tenant_id == int(tenant_id))
        if user_id is not None:
            query = query.filter(EngagementReportJob.user_id == int(user_id))
        return int(query.scalar() or 0)

    def acquire_report_job_claim_limit_lock(
        self,
        *,
        namespace_key: int,
        claim_key: int,
    ) -> None:
        """Acquire the Postgres transaction lock for report job claim limits."""

        bind = self.db.get_bind()
        if bind is None or bind.dialect.name != "postgresql":
            return

        self.db.execute(
            text("SELECT pg_advisory_xact_lock(:namespace_key, :claim_key)"),
            {
                "namespace_key": int(namespace_key),
                "claim_key": int(claim_key),
            },
        )

    def claim_report_job(
        self,
        *,
        job_id: str | uuid.UUID,
        worker_id: str,
        claimed_at: datetime,
        global_limit: int | None = None,
        per_tenant_limit: int | None = None,
        per_user_limit: int | None = None,
    ) -> EngagementReportJob | None:
        """Atomically transition one queued report job to generating."""

        parsed_job_id = self._parse_uuid(job_id)
        if parsed_job_id is None:
            return None

        row = self.get_report_job_by_id(job_id=parsed_job_id)
        if row is None:
            return None

        filters = [
            EngagementReportJob.id == parsed_job_id,
            EngagementReportJob.tenant_id == row.tenant_id,
            EngagementReportJob.user_id == row.user_id,
            EngagementReportJob.status == REPORT_JOB_STATUS_QUEUED,
            EngagementReportJob.attempt_count < EngagementReportJob.max_attempts,
            or_(
                EngagementReportJob.next_attempt_at.is_(None),
                EngagementReportJob.next_attempt_at <= claimed_at,
            ),
        ]
        filters.extend(
            self._active_limit_filters(
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                global_limit=global_limit,
                per_tenant_limit=per_tenant_limit,
                per_user_limit=per_user_limit,
            )
        )

        updated = (
            self.db.query(EngagementReportJob)
            .filter(*filters)
            .update(
                {
                    EngagementReportJob.status: REPORT_JOB_STATUS_GENERATING,
                    EngagementReportJob.locked_by: str(worker_id),
                    EngagementReportJob.locked_at: claimed_at,
                    EngagementReportJob.next_attempt_at: None,
                    EngagementReportJob.started_at: claimed_at,
                    EngagementReportJob.finished_at: None,
                    EngagementReportJob.attempt_count: EngagementReportJob.attempt_count
                    + 1,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            return None

        self.db.flush()
        row = self.get_report_job_by_id(job_id=parsed_job_id)
        if row is not None:
            self.db.refresh(row)
        return row

    def _active_limit_filters(
        self,
        *,
        tenant_id: int,
        user_id: int,
        global_limit: int | None,
        per_tenant_limit: int | None,
        per_user_limit: int | None,
    ) -> list[Any]:
        """Build count guards for active job limits inside a claim update."""

        filters: list[Any] = []
        if global_limit is not None:
            filters.append(
                self._active_job_count_subquery() < max(0, int(global_limit))
            )
        if per_tenant_limit is not None:
            filters.append(
                self._active_job_count_subquery(tenant_id=tenant_id)
                < max(0, int(per_tenant_limit))
            )
        if per_user_limit is not None:
            filters.append(
                self._active_job_count_subquery(tenant_id=tenant_id, user_id=user_id)
                < max(0, int(per_user_limit))
            )
        return filters

    def _active_job_count_subquery(
        self,
        *,
        tenant_id: int | None = None,
        user_id: int | None = None,
    ) -> Any:
        active_job = aliased(EngagementReportJob)
        query = select(func.count(active_job.id)).where(
            active_job.status == REPORT_JOB_STATUS_GENERATING
        )
        if tenant_id is not None:
            query = query.where(active_job.tenant_id == int(tenant_id))
        if user_id is not None:
            query = query.where(active_job.user_id == int(user_id))
        return query.scalar_subquery()

    def list_stale_generating_report_jobs(
        self,
        *,
        stale_before: datetime,
        limit: int = 100,
    ) -> list[EngagementReportJob]:
        """Return generating jobs whose locks are older than the stale threshold."""

        return (
            self.db.query(EngagementReportJob)
            .filter(
                EngagementReportJob.status == REPORT_JOB_STATUS_GENERATING,
                EngagementReportJob.locked_at.is_not(None),
                EngagementReportJob.locked_at < stale_before,
            )
            .order_by(EngagementReportJob.locked_at.asc())
            .limit(max(1, int(limit)))
            .all()
        )

    def requeue_stale_report_job(
        self,
        *,
        job_id: str | uuid.UUID,
        stale_before: datetime,
    ) -> EngagementReportJob | None:
        """Atomically release one stale generating job back to the queued state."""

        parsed_job_id = self._parse_uuid(job_id)
        if parsed_job_id is None:
            return None

        updated = (
            self.db.query(EngagementReportJob)
            .filter(
                EngagementReportJob.id == parsed_job_id,
                EngagementReportJob.status == REPORT_JOB_STATUS_GENERATING,
                EngagementReportJob.locked_at.is_not(None),
                EngagementReportJob.locked_at < stale_before,
                EngagementReportJob.attempt_count < EngagementReportJob.max_attempts,
            )
            .update(
                {
                    EngagementReportJob.status: REPORT_JOB_STATUS_QUEUED,
                    EngagementReportJob.locked_by: None,
                    EngagementReportJob.locked_at: None,
                    EngagementReportJob.next_attempt_at: None,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            return None

        self.db.flush()
        row = self.get_report_job_by_id(job_id=parsed_job_id)
        if row is not None:
            self.db.refresh(row)
        return row

    def requeue_report_job_after_failure_by_id(
        self,
        *,
        job_id: str | uuid.UUID,
        last_error_code: str,
        error_message: str,
        next_attempt_at: datetime,
        last_error_at: datetime,
    ) -> EngagementReportJob | None:
        """Release one failed worker attempt back to queued while attempts remain."""

        row = self.get_report_job_by_id(job_id=job_id)
        if row is None:
            return None
        if str(row.status) != REPORT_JOB_STATUS_GENERATING or int(
            row.attempt_count
        ) >= int(row.max_attempts):
            return None

        row.status = REPORT_JOB_STATUS_QUEUED
        row.next_attempt_at = next_attempt_at
        row.locked_by = None
        row.locked_at = None
        row.last_error_code = str(last_error_code)
        row.error_message = str(error_message)
        row.last_error_at = last_error_at
        row.finished_at = None
        self.db.flush()
        self.db.refresh(row)
        return row

    def mark_report_job_failed_by_id(
        self,
        *,
        job_id: str | uuid.UUID,
        error_message: str,
        last_error_code: str | None = None,
        finished_at: datetime | None = None,
    ) -> EngagementReportJob | None:
        """Mark one report job failed by ID for internal worker recovery paths."""

        row = self.get_report_job_by_id(job_id=job_id)
        if row is None:
            return None

        row.status = REPORT_JOB_STATUS_FAILED
        row.next_attempt_at = None
        row.locked_by = None
        row.locked_at = None
        row.last_error_code = last_error_code
        row.error_message = str(error_message)
        row.last_error_at = finished_at or datetime.now(UTC)
        row.finished_at = finished_at
        self.db.flush()
        self.db.refresh(row)
        return row

    def update_report_job_progress_by_id(
        self,
        *,
        job_id: str | uuid.UUID,
        current_section_id: str | None,
        completed_sections: Sequence[str],
        total_sections: int,
        generation_phase: str | None = None,
        clear_error: bool = False,
    ) -> EngagementReportJob | None:
        """Persist section-level progress for one internal worker-owned job."""

        row = self.get_report_job_by_id(job_id=job_id)
        if row is None:
            return None

        row.current_section_id = current_section_id
        row.completed_sections = [str(section_id) for section_id in completed_sections]
        row.total_sections = max(0, int(total_sections))
        if generation_phase is not None:
            row.generation_phase = str(generation_phase)
        if clear_error:
            row.last_error_code = None
            row.error_message = None
            row.last_error_at = None
            row.next_attempt_at = None
        row.locked_at = datetime.now(UTC)
        self.db.flush()
        self.db.refresh(row)
        return row

    def get_report_job_by_id(
        self,
        *,
        job_id: str | uuid.UUID,
    ) -> EngagementReportJob | None:
        """Return one report job by durable ID for internal worker paths."""

        parsed_job_id = self._parse_uuid(job_id)
        if parsed_job_id is None:
            return None

        return (
            self.db.query(EngagementReportJob)
            .filter(EngagementReportJob.id == parsed_job_id)
            .one_or_none()
        )


