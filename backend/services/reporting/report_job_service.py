"""Coordinate durable engagement report generation job lifecycle transitions.

This module owns job claim policy, stale-lock recovery, active generation
limits, and worker-visible progress/failure updates. It does not execute report
generation, call LLM providers, or keep restart-critical state in process memory.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.reporting import EngagementReportJob
from backend.repositories.reporting.engagement_report_repository import (
    EngagementReportRepository,
)
from backend.repositories.reporting.report_job_worker_repository import (
    ReportJobWorkerRepository,
)
from backend.services.reporting.contracts import (
    REPORT_STATUS_GENERATING,
)
from backend.services.reporting.report_diagnostics import ReportDiagnostics


_ADVISORY_LOCK_NAMESPACE_REPORT_JOB_CLAIM = 2861
_ADVISORY_LOCK_KEY_REPORT_JOB_CLAIM_LIMITS = 1
_STALE_ATTEMPT_RECOVERED_CODE = "stale_attempt_recovered"
_RETRY_BASE_DELAY_SECONDS = 5
_RETRY_MAX_DELAY_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ReportJobClaimLimits:
    """Configurable active generation ceilings for report job claiming."""

    global_limit: int = 2
    per_tenant_limit: int = 1
    per_user_limit: int = 1
    candidate_batch_size: int = 25


@dataclass(frozen=True, slots=True)
class StaleJobRecoveryResult:
    """Summary of stale report jobs recovered or failed."""

    requeued: int
    failed: int


class ReportJobService:
    """Apply report job lifecycle policy around persistence-backed state."""

    def __init__(
        self,
        db: Session,
        *,
        report_repository: EngagementReportRepository | None = None,
        worker_job_repository: ReportJobWorkerRepository | None = None,
        diagnostics: ReportDiagnostics | None = None,
    ) -> None:
        self._report_repository = (
            report_repository
            if report_repository is not None
            else EngagementReportRepository(db)
        )
        self._worker_repository = (
            worker_job_repository
            if worker_job_repository is not None
            else ReportJobWorkerRepository(db)
        )
        self._diagnostics = diagnostics or ReportDiagnostics()

    def claim_next_job(
        self,
        *,
        worker_id: str,
        stale_after: timedelta,
        limits: ReportJobClaimLimits,
    ) -> EngagementReportJob | None:
        """Claim the next queued job that fits active generation limits."""

        now = datetime.now(UTC)
        self._acquire_claim_limit_lock()
        self._recover_stale_jobs(now=now, stale_after=stale_after, max_attempts=None)

        if self._active_count() >= max(0, int(limits.global_limit)):
            return None

        candidates = self._worker_repository.list_claimable_report_jobs(
            now=now, limit=limits.candidate_batch_size
        )
        for candidate in candidates:
            if self._tenant_at_limit(candidate, limits):
                continue
            if self._user_at_limit(candidate, limits):
                continue

            claimed = self._worker_repository.claim_report_job(
                job_id=candidate.id,
                worker_id=worker_id,
                claimed_at=now,
                global_limit=limits.global_limit,
                per_tenant_limit=limits.per_tenant_limit,
                per_user_limit=limits.per_user_limit,
            )
            if claimed is not None:
                self._diagnostics.job_claimed(
                    job_id=claimed.id,
                    engagement_id=int(claimed.engagement_id),
                    report_type=str(claimed.report_type),
                    attempt_count=int(claimed.attempt_count),
                    max_attempts=int(claimed.max_attempts),
                )
                return claimed

        return None

    def recover_stale_jobs(
        self,
        *,
        now: datetime,
        stale_after: timedelta,
        max_attempts: int,
    ) -> StaleJobRecoveryResult:
        """Release stale generating jobs or fail jobs with no attempts remaining."""

        return self._recover_stale_jobs(
            now=now,
            stale_after=stale_after,
            max_attempts=max(1, int(max_attempts)),
        )

    def mark_progress(
        self,
        *,
        job_id: str | UUID,
        current_section_id: str | None,
        completed_sections: Sequence[str],
        total_sections: int,
        generation_phase: str | None = None,
        clear_error: bool = False,
    ) -> EngagementReportJob | None:
        """Persist progress for a worker-owned job."""

        return self._worker_repository.update_report_job_progress_by_id(
            job_id=job_id,
            current_section_id=current_section_id,
            completed_sections=completed_sections,
            total_sections=total_sections,
            generation_phase=generation_phase,
            clear_error=clear_error,
        )

    def requeue_after_failure(
        self,
        *,
        job_id: str | UUID,
        reason: str,
        safe_message: str,
        now: datetime | None = None,
    ) -> EngagementReportJob | None:
        """Schedule a resumable retry without clearing durable progress."""

        failed_at = now or datetime.now(UTC)
        job = self._worker_repository.get_report_job_by_id(job_id=job_id)
        if job is None:
            return None
        delay_seconds = min(
            _RETRY_MAX_DELAY_SECONDS,
            _RETRY_BASE_DELAY_SECONDS * (2 ** max(0, int(job.attempt_count) - 1)),
        )
        requeued = self._worker_repository.requeue_report_job_after_failure_by_id(
            job_id=job_id,
            last_error_code=str(reason),
            error_message=str(safe_message),
            next_attempt_at=failed_at + timedelta(seconds=delay_seconds),
            last_error_at=failed_at,
        )
        if requeued is not None:
            self._diagnostics.job_requeued(
                job_id=requeued.id,
                report_id=requeued.report_id,
                engagement_id=int(requeued.engagement_id),
                report_type=str(requeued.report_type),
                reason=str(reason),
                attempt_count=int(requeued.attempt_count),
                max_attempts=int(requeued.max_attempts),
        )
        return requeued

    def fail_job(
        self,
        *,
        job_id: str | UUID,
        reason: str,
        safe_message: str,
    ) -> EngagementReportJob | None:
        """Mark a job failed with safe metadata suitable for persistence."""

        return self._worker_repository.mark_report_job_failed_by_id(
            job_id=job_id,
            last_error_code=str(reason),
            error_message=str(safe_message),
            finished_at=datetime.now(UTC),
        )

    def _recover_stale_jobs(
        self,
        *,
        now: datetime,
        stale_after: timedelta,
        max_attempts: int | None,
    ) -> StaleJobRecoveryResult:
        stale_before = now - stale_after
        requeued = 0
        failed = 0

        for job in self._worker_repository.list_stale_generating_report_jobs(
            stale_before=stale_before
        ):
            effective_max_attempts = _effective_max_attempts(job, max_attempts)
            if int(job.attempt_count) >= effective_max_attempts:
                self._mark_linked_generating_attempt_failed(
                    job=job,
                    error_code="max_attempts_exceeded",
                    error_message="Report generation exceeded the retry limit.",
                )
                failed_job = self._worker_repository.mark_report_job_failed_by_id(
                    job_id=job.id,
                    last_error_code="max_attempts_exceeded",
                    error_message="Report generation exceeded the retry limit.",
                    finished_at=now,
                )
                if failed_job is not None:
                    failed += 1
                    self._diagnostics.stale_job_failed(
                        job_id=failed_job.id,
                        report_id=failed_job.report_id,
                        engagement_id=int(failed_job.engagement_id),
                        report_type=str(failed_job.report_type),
                        reason="max_attempts_exceeded",
                        attempt_count=int(failed_job.attempt_count),
                        max_attempts=effective_max_attempts,
                    )
                continue

            recovered = self._worker_repository.requeue_stale_report_job(
                job_id=job.id,
                stale_before=stale_before,
            )
            if recovered is not None:
                requeued += 1
                self._diagnostics.stale_job_requeued(
                    job_id=recovered.id,
                    report_id=recovered.report_id,
                    engagement_id=int(recovered.engagement_id),
                    report_type=str(recovered.report_type),
                    reason=_STALE_ATTEMPT_RECOVERED_CODE,
                    attempt_count=int(recovered.attempt_count),
                    max_attempts=effective_max_attempts,
                )

        return StaleJobRecoveryResult(requeued=requeued, failed=failed)

    def _mark_linked_generating_attempt_failed(
        self,
        *,
        job: EngagementReportJob,
        error_code: str,
        error_message: str,
    ) -> None:
        if job.report_id is None:
            return

        report = self._report_repository.get_report_by_id(
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            engagement_id=job.engagement_id,
            report_id=job.report_id,
        )
        if report is None or str(report.status) != REPORT_STATUS_GENERATING:
            return

        self._report_repository.mark_report_failed(
            tenant_id=job.tenant_id,
            user_id=job.user_id,
            engagement_id=job.engagement_id,
            report_id=job.report_id,
            error_message=error_message,
            generation_metadata={"error_code": error_code},
        )

    def _active_count(
        self,
        *,
        tenant_id: int | None = None,
        user_id: int | None = None,
    ) -> int:
        return self._worker_repository.count_active_report_jobs(
            tenant_id=tenant_id,
            user_id=user_id,
        )

    def _tenant_at_limit(
        self,
        job: EngagementReportJob,
        limits: ReportJobClaimLimits,
    ) -> bool:
        return self._active_count(tenant_id=job.tenant_id) >= max(
            0, int(limits.per_tenant_limit)
        )

    def _user_at_limit(
        self,
        job: EngagementReportJob,
        limits: ReportJobClaimLimits,
    ) -> bool:
        return self._active_count(tenant_id=job.tenant_id, user_id=job.user_id) >= max(
            0, int(limits.per_user_limit)
        )

    def _acquire_claim_limit_lock(self) -> None:
        self._worker_repository.acquire_report_job_claim_limit_lock(
            namespace_key=_ADVISORY_LOCK_NAMESPACE_REPORT_JOB_CLAIM,
            claim_key=_ADVISORY_LOCK_KEY_REPORT_JOB_CLAIM_LIMITS,
        )


def _effective_max_attempts(
    job: EngagementReportJob,
    service_max_attempts: int | None,
) -> int:
    job_max_attempts = max(1, int(job.max_attempts))
    if service_max_attempts is None:
        return job_max_attempts
    return min(job_max_attempts, max(1, int(service_max_attempts)))


__all__ = [
    "ReportJobClaimLimits",
    "ReportJobService",
    "StaleJobRecoveryResult",
]
