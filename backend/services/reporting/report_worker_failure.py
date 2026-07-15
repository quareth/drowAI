"""Persist safe report-worker failures without owning claim or generation flow.

This module is limited to failure classification and retry/terminal persistence;
the public worker remains responsible for orchestration and dependency setup.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import DBAPIError, OperationalError

from backend.services.reporting.contracts import (
    REPORT_GENERATION_ERROR_FINALIZATION_FAILED,
    REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
    REPORT_GENERATION_PHASE_FINALIZING,
    REPORT_GENERATION_PHASE_SECTIONS,
    ReportGenerationServiceErrorReason,
)
from backend.services.reporting.report_finalization_checkpoint import (
    safe_report_metadata,
)
from backend.services.reporting.report_section_generator import (
    ReportSectionGenerationError,
)
from backend.services.reporting.report_section_validation import (
    ReportSectionValidationError,
)
from backend.services.reporting.report_worker_types import (
    ReportWorkerRunResult,
    _ClaimedJobScope,
)


class _ReportWorkerFailure(Exception):
    """Internal safe failure envelope for persisted job/report errors."""

    def __init__(
        self,
        *,
        reason: ReportGenerationServiceErrorReason,
        safe_message: str,
        metadata: Mapping[str, Any] | None = None,
        retryable: bool = False,
        phase: str = REPORT_GENERATION_PHASE_SECTIONS,
    ) -> None:
        super().__init__(safe_message)
        self.reason = reason
        self.safe_message = safe_message
        self.metadata = dict(metadata or {})
        self.retryable = bool(retryable)
        self.phase = str(phase)


class _ReportWorkerFailurePersistence:
    """Provide durable failure transitions to an initialized report worker."""

    def _persist_failure(
        self,
        *,
        scope: _ClaimedJobScope,
        report_id: UUID | None,
        failure: _ReportWorkerFailure,
    ) -> ReportWorkerRunResult:
        current_job = self._worker_repository.get_report_job_by_id(job_id=scope.job_id)
        attempts_remain = current_job is not None and int(
            current_job.attempt_count
        ) < int(current_job.max_attempts)
        if failure.phase == REPORT_GENERATION_PHASE_FINALIZING:
            self._diagnostics.finalization_failed(
                job_id=scope.job_id,
                report_id=report_id,
                engagement_id=scope.engagement_id,
                report_type=scope.report_type,
                reason=str(failure.reason),
            )
        if failure.retryable and attempts_remain:
            retry_metadata_ready = report_id is None
            if report_id is not None:
                retry_report = self._report_repository.merge_report_generation_metadata(
                    tenant_id=scope.tenant_id,
                    user_id=scope.user_id,
                    engagement_id=scope.engagement_id,
                    report_id=report_id,
                    generation_metadata={
                        "last_failure_code": failure.reason,
                        "last_failure_phase": failure.phase,
                        "last_failure_retryable": True,
                        **safe_report_metadata(failure.metadata),
                    },
                )
                retry_metadata_ready = retry_report is not None
            requeued = (
                self._jobs.requeue_after_failure(
                    job_id=scope.job_id,
                    reason=failure.reason,
                    safe_message=failure.safe_message,
                )
                if retry_metadata_ready
                else None
            )
            if requeued is not None:
                self._db.commit()
                return ReportWorkerRunResult(
                    claimed=True,
                    job_id=requeued.id,
                    report_id=report_id,
                    status=str(requeued.status),
                )

        if report_id is not None:
            failed_report = self._report_repository.mark_report_failed(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                engagement_id=scope.engagement_id,
                report_id=report_id,
                error_message=failure.safe_message,
                generation_metadata={
                    "error_code": failure.reason,
                    "failure_phase": failure.phase,
                    **safe_report_metadata(failure.metadata),
                },
            )
            if failed_report is None:
                self._db.rollback()
                raise RuntimeError(
                    "Report attempt failure state could not be persisted."
                )

        failed_job = self._worker_repository.mark_report_job_failed_by_id(
            job_id=scope.job_id,
            last_error_code=failure.reason,
            error_message=failure.safe_message,
            finished_at=datetime.now(UTC),
        )
        if failed_job is None:
            self._db.rollback()
            raise RuntimeError("Report failure state could not be persisted.")
        self._db.commit()
        self._diagnostics.job_failed(
            job_id=scope.job_id,
            report_id=report_id,
            engagement_id=scope.engagement_id,
            report_type=scope.report_type,
            reason=str(failure.reason),
            attempt_count=(
                int(failed_job.attempt_count) if failed_job is not None else None
            ),
            max_attempts=(
                int(failed_job.max_attempts) if failed_job is not None else None
            ),
        )
        return ReportWorkerRunResult(
            claimed=True,
            job_id=scope.job_id,
            report_id=report_id,
            status=str(failed_job.status) if failed_job is not None else "failed",
        )


def _safe_failure(
    exc: Exception,
    *,
    phase: str = REPORT_GENERATION_PHASE_SECTIONS,
) -> _ReportWorkerFailure:
    if isinstance(exc, _ReportWorkerFailure):
        return exc
    if isinstance(exc, ReportSectionGenerationError):
        return _ReportWorkerFailure(
            reason=exc.reason,
            safe_message=exc.safe_message,
            metadata=exc.metadata,
            retryable=exc.retryable,
            phase=phase,
        )
    if isinstance(exc, ReportSectionValidationError):
        return _ReportWorkerFailure(
            reason=exc.reason,
            safe_message=exc.safe_message,
            retryable=True,
            phase=phase,
        )
    if isinstance(exc, OperationalError):
        return _ReportWorkerFailure(
            reason=REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
            safe_message="Report generation was interrupted by a temporary database failure.",
            metadata={"failure_class": exc.__class__.__name__},
            retryable=True,
            phase=phase,
        )
    if isinstance(exc, DBAPIError) and bool(exc.connection_invalidated):
        return _ReportWorkerFailure(
            reason=REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
            safe_message="Report generation was interrupted by a temporary database failure.",
            metadata={"failure_class": exc.__class__.__name__},
            retryable=True,
            phase=phase,
        )
    return _ReportWorkerFailure(
        reason=(
            REPORT_GENERATION_ERROR_FINALIZATION_FAILED
            if phase == REPORT_GENERATION_PHASE_FINALIZING
            else REPORT_GENERATION_ERROR_PERSISTENCE_FAILED
        ),
        safe_message="Report generation failed.",
        metadata={"failure_class": exc.__class__.__name__},
        retryable=False,
        phase=phase,
    )


def _is_expected_failure(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            _ReportWorkerFailure,
            ReportSectionGenerationError,
            ReportSectionValidationError,
            OperationalError,
        ),
    )
