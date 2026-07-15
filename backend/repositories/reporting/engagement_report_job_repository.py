"""Requester-scoped report-job persistence for reporting data.

This module owns tenant/user/requester-scoped job reads and state changes;
worker-only durable-ID claim, progress, failure, and recovery are excluded.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from backend.models.core import Engagement
from backend.models.reporting import EngagementReportJob
from backend.repositories.reporting.base import ReportingRepositoryBase
from backend.services.reporting.contracts import (
    REPORT_JOB_STATUS_FAILED,
    REPORT_JOB_STATUS_GENERATING,
    REPORT_JOB_STATUS_QUEUED,
    REPORT_JOB_STATUS_READY,
)


class EngagementReportJobRepository(ReportingRepositoryBase):
    """Persist requester-scoped report-job reads and state transitions."""

    def get_report_job(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        job_id: str | uuid.UUID,
    ) -> EngagementReportJob | None:
        """Return one report job constrained by tenant/user/engagement identity."""

        parsed_job_id = self._parse_uuid(job_id)
        if parsed_job_id is None:
            return None

        return (
            self.db.query(EngagementReportJob)
            .filter(
                EngagementReportJob.tenant_id == int(tenant_id),
                EngagementReportJob.user_id == int(user_id),
                EngagementReportJob.engagement_id == int(engagement_id),
                EngagementReportJob.id == parsed_job_id,
            )
            .one_or_none()
        )

    def get_report_job_by_id_for_requester(
        self,
        *,
        tenant_id: int,
        user_id: int,
        requested_by_user_id: int,
        job_id: str | uuid.UUID,
    ) -> EngagementReportJob | None:
        """Return one report job only when requester and engagement scope match."""

        parsed_job_id = self._parse_uuid(job_id)
        if parsed_job_id is None:
            return None

        return (
            self.db.query(EngagementReportJob)
            .join(Engagement, EngagementReportJob.engagement_id == Engagement.id)
            .filter(
                EngagementReportJob.tenant_id == int(tenant_id),
                EngagementReportJob.user_id == int(user_id),
                EngagementReportJob.requested_by_user_id == int(requested_by_user_id),
                EngagementReportJob.id == parsed_job_id,
                Engagement.tenant_id == int(tenant_id),
                Engagement.user_id == int(user_id),
                Engagement.id == EngagementReportJob.engagement_id,
            )
            .one_or_none()
        )

    def get_active_job_by_idempotency_key(
        self,
        *,
        tenant_id: int,
        user_id: int,
        requested_by_user_id: int,
        engagement_id: int,
        report_type: str,
        idempotency_key: str,
    ) -> EngagementReportJob | None:
        """Return an active scoped report job for one idempotency key."""

        return (
            self.db.query(EngagementReportJob)
            .filter(
                EngagementReportJob.tenant_id == int(tenant_id),
                EngagementReportJob.user_id == int(user_id),
                EngagementReportJob.requested_by_user_id == int(requested_by_user_id),
                EngagementReportJob.engagement_id == int(engagement_id),
                EngagementReportJob.report_type == str(report_type),
                EngagementReportJob.idempotency_key == str(idempotency_key),
                EngagementReportJob.status.in_(
                    (REPORT_JOB_STATUS_QUEUED, REPORT_JOB_STATUS_GENERATING)
                ),
            )
            .order_by(EngagementReportJob.created_at.desc())
            .one_or_none()
        )

    def get_active_report_job_for_requester(
        self,
        *,
        tenant_id: int,
        user_id: int,
        requested_by_user_id: int,
        engagement_id: int,
        report_type: str,
    ) -> EngagementReportJob | None:
        """Return the latest active report job for a scoped requester."""

        return (
            self.db.query(EngagementReportJob)
            .join(Engagement, EngagementReportJob.engagement_id == Engagement.id)
            .filter(
                EngagementReportJob.tenant_id == int(tenant_id),
                EngagementReportJob.user_id == int(user_id),
                EngagementReportJob.requested_by_user_id == int(requested_by_user_id),
                EngagementReportJob.engagement_id == int(engagement_id),
                EngagementReportJob.report_type == str(report_type),
                EngagementReportJob.status.in_(
                    (REPORT_JOB_STATUS_QUEUED, REPORT_JOB_STATUS_GENERATING)
                ),
                Engagement.tenant_id == int(tenant_id),
                Engagement.user_id == int(user_id),
                Engagement.id == EngagementReportJob.engagement_id,
            )
            .order_by(
                EngagementReportJob.updated_at.desc(),
                EngagementReportJob.created_at.desc(),
            )
            .first()
        )

    def create_report_job(
        self,
        *,
        tenant_id: int,
        user_id: int,
        requested_by_user_id: int,
        engagement_id: int,
        report_type: str,
        idempotency_key: str,
        selected_task_memo_ids: Sequence[str | uuid.UUID],
        include_candidate_findings: bool,
        source_watermark: dict[str, Any],
        llm_runtime_selection: dict[str, Any] | None = None,
        total_sections: int = 0,
        max_attempts: int = 3,
    ) -> EngagementReportJob:
        """Insert a queued report job or return the existing idempotent row."""

        persisted_idempotency_key = str(idempotency_key)
        row = EngagementReportJob(
            tenant_id=int(tenant_id),
            user_id=int(user_id),
            requested_by_user_id=int(requested_by_user_id),
            engagement_id=int(engagement_id),
            report_type=str(report_type),
            status=REPORT_JOB_STATUS_QUEUED,
            idempotency_key=persisted_idempotency_key,
            selected_task_memo_ids=self._canonical_memo_id_strings(
                selected_task_memo_ids
            ),
            include_candidate_findings=bool(include_candidate_findings),
            llm_runtime_selection=dict(llm_runtime_selection or {}),
            source_watermark=dict(source_watermark),
            generation_phase="sections",
            current_section_id=None,
            completed_sections=[],
            total_sections=max(0, int(total_sections)),
            next_attempt_at=None,
            attempt_count=0,
            max_attempts=max(1, int(max_attempts)),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError:
            existing = self._get_report_job_by_idempotency_key(
                tenant_id=tenant_id,
                user_id=user_id,
                requested_by_user_id=requested_by_user_id,
                engagement_id=engagement_id,
                report_type=report_type,
                idempotency_key=idempotency_key,
            )
            if existing is None:
                raise
            if str(existing.status) in (
                REPORT_JOB_STATUS_QUEUED,
                REPORT_JOB_STATUS_GENERATING,
            ):
                return existing
            persisted_idempotency_key = self._retry_idempotency_key(idempotency_key)
            row = EngagementReportJob(
                tenant_id=int(tenant_id),
                user_id=int(user_id),
                requested_by_user_id=int(requested_by_user_id),
                engagement_id=int(engagement_id),
                report_type=str(report_type),
                status=REPORT_JOB_STATUS_QUEUED,
                idempotency_key=persisted_idempotency_key,
                selected_task_memo_ids=self._canonical_memo_id_strings(
                    selected_task_memo_ids
                ),
                include_candidate_findings=bool(include_candidate_findings),
                llm_runtime_selection=dict(llm_runtime_selection or {}),
                source_watermark=self._source_watermark_with_idempotency_key(
                    source_watermark=source_watermark,
                    idempotency_key=persisted_idempotency_key,
                    original_idempotency_key=str(idempotency_key),
                ),
                generation_phase="sections",
                current_section_id=None,
                completed_sections=[],
                total_sections=max(0, int(total_sections)),
                next_attempt_at=None,
                attempt_count=0,
                max_attempts=max(1, int(max_attempts)),
            )
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()

        self.db.refresh(row)
        return row

    def mark_report_job_ready(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        job_id: str | uuid.UUID,
        report_id: str | uuid.UUID,
        finished_at: datetime | None = None,
    ) -> EngagementReportJob | None:
        """Mark one scoped report job ready and bind the generated report."""

        row = self.get_report_job(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            job_id=job_id,
        )
        parsed_report_id = self._parse_uuid(report_id)
        if row is None or parsed_report_id is None:
            return None

        row.status = REPORT_JOB_STATUS_READY
        row.report_id = parsed_report_id
        row.current_section_id = None
        row.next_attempt_at = None
        row.locked_by = None
        row.locked_at = None
        row.last_error_code = None
        row.error_message = None
        row.last_error_at = None
        row.finished_at = finished_at
        self.db.flush()
        self.db.refresh(row)
        return row

    def mark_report_job_failed(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        job_id: str | uuid.UUID,
        error_message: str,
        last_error_code: str | None = None,
        finished_at: datetime | None = None,
    ) -> EngagementReportJob | None:
        """Mark one scoped report job failed without changing reports."""

        row = self.get_report_job(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            job_id=job_id,
        )
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

    def update_report_job_progress(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        job_id: str | uuid.UUID,
        current_section_id: str | None,
        completed_sections: Sequence[str],
        total_sections: int,
    ) -> EngagementReportJob | None:
        """Persist section-level progress for one scoped report job."""

        row = self.get_report_job(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            job_id=job_id,
        )
        if row is None:
            return None

        row.current_section_id = current_section_id
        row.completed_sections = [str(section_id) for section_id in completed_sections]
        row.total_sections = max(0, int(total_sections))
        self.db.flush()
        self.db.refresh(row)
        return row

    def _get_report_job_by_idempotency_key(
        self,
        *,
        tenant_id: int,
        user_id: int,
        requested_by_user_id: int,
        engagement_id: int,
        report_type: str,
        idempotency_key: str,
    ) -> EngagementReportJob | None:
        """Return any scoped job matching an idempotency key."""

        return (
            self.db.query(EngagementReportJob)
            .filter(
                EngagementReportJob.tenant_id == int(tenant_id),
                EngagementReportJob.user_id == int(user_id),
                EngagementReportJob.requested_by_user_id == int(requested_by_user_id),
                EngagementReportJob.engagement_id == int(engagement_id),
                EngagementReportJob.report_type == str(report_type),
                EngagementReportJob.idempotency_key == str(idempotency_key),
            )
            .one_or_none()
        )

    @staticmethod
    def _retry_idempotency_key(idempotency_key: str) -> str:
        """Return a unique retry key that fits the report job key column."""

        suffix = f":retry:{uuid.uuid4().hex}"
        return f"{str(idempotency_key)[: 255 - len(suffix)]}{suffix}"

    @staticmethod
    def _source_watermark_with_idempotency_key(
        *,
        source_watermark: dict[str, Any],
        idempotency_key: str,
        original_idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Return source metadata with the persisted idempotency key recorded."""

        watermark = dict(source_watermark)
        raw_idempotency = watermark.get("idempotency")
        idempotency = dict(raw_idempotency) if isinstance(raw_idempotency, dict) else {}
        if original_idempotency_key is not None:
            idempotency["original_key"] = original_idempotency_key
        idempotency["key"] = str(idempotency_key)
        watermark["idempotency"] = idempotency
        return watermark
