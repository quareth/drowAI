"""Run durable engagement report generation jobs from claimed database rows."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.reporting import EngagementReport, EngagementReportJob
from backend.repositories.reporting.engagement_report_job_repository import (
    EngagementReportJobRepository,
)
from backend.repositories.reporting.engagement_report_repository import (
    EngagementReportRepository,
)
from backend.repositories.reporting.report_job_worker_repository import (
    ReportJobWorkerRepository,
)
from backend.repositories.reporting.task_closure_memo_repository import (
    TaskClosureMemoRepository,
)
from backend.services.reporting.contracts import (
    REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
    REPORT_GENERATION_PHASE_SECTIONS,
)
from backend.services.reporting.report_context_builder import ReportContextBuilder
from backend.services.reporting.report_diagnostics import ReportDiagnostics
from backend.services.reporting.report_job_service import (
    ReportJobClaimLimits,
    ReportJobService,
)
from backend.services.reporting.report_renderer import (
    EngagementReportMarkdownRenderer,
)
from backend.services.reporting.report_section_generator import ReportSectionGenerator
from backend.services.reporting.report_section_prompt import (
    ReportSectionPromptRenderer,
)
from backend.services.reporting.report_section_validation import ReportSectionValidator
from backend.services.reporting.report_worker_attempt import (
    _ReportWorkerAttemptExecution,
)
from backend.services.reporting.report_worker_types import (
    ReportWorkerRunResult,
    _ClaimedJobScope,
)
from backend.services.reporting.report_worker_failure import (
    _ReportWorkerFailure,
    _ReportWorkerFailurePersistence,
    _is_expected_failure,
    _safe_failure,
)
from backend.services.reporting.source_watermark_service import SourceWatermarkService

_DEFAULT_STALE_AFTER = timedelta(minutes=15)
logger = logging.getLogger(__name__)


class ReportWorker(_ReportWorkerAttemptExecution, _ReportWorkerFailurePersistence):
    """Claim and execute one persisted engagement report generation job."""

    def __init__(
        self,
        db: Session,
        *,
        memo_repository: TaskClosureMemoRepository | None = None,
        report_repository: EngagementReportRepository | None = None,
        request_job_repository: EngagementReportJobRepository | None = None,
        worker_job_repository: ReportJobWorkerRepository | None = None,
        job_service: ReportJobService | None = None,
        context_builder: ReportContextBuilder | None = None,
        prompt_renderer: ReportSectionPromptRenderer | None = None,
        section_generator: ReportSectionGenerator | None = None,
        section_validator: ReportSectionValidator | None = None,
        markdown_renderer: EngagementReportMarkdownRenderer | None = None,
        source_watermarks: SourceWatermarkService | None = None,
        claim_limits: ReportJobClaimLimits | None = None,
        stale_after: timedelta = _DEFAULT_STALE_AFTER,
        diagnostics: ReportDiagnostics | None = None,
    ) -> None:
        self._db = db
        self._memo_repository = (
            memo_repository
            if memo_repository is not None
            else TaskClosureMemoRepository(db)
        )
        self._report_repository = (
            report_repository
            if report_repository is not None
            else EngagementReportRepository(db)
        )
        self._scoped_job_repository = (
            request_job_repository
            if request_job_repository is not None
            else EngagementReportJobRepository(db)
        )
        self._worker_repository = (
            worker_job_repository
            if worker_job_repository is not None
            else ReportJobWorkerRepository(db)
        )
        self._diagnostics = diagnostics or ReportDiagnostics()
        self._jobs = job_service or ReportJobService(
            db,
            report_repository=self._report_repository,
            worker_job_repository=self._worker_repository,
            diagnostics=self._diagnostics,
        )
        self._context_builder = context_builder or ReportContextBuilder(db)
        self._prompt_renderer = prompt_renderer or ReportSectionPromptRenderer()
        self._section_generator = section_generator or ReportSectionGenerator(db)
        self._section_validator = section_validator or ReportSectionValidator()
        self._markdown_renderer = (
            markdown_renderer or EngagementReportMarkdownRenderer()
        )
        self._source_watermarks = source_watermarks
        self._claim_limits = claim_limits or ReportJobClaimLimits()
        self._stale_after = stale_after

    async def run_once(self, *, worker_id: str) -> ReportWorkerRunResult:
        """Claim at most one queued job and run one generation attempt."""

        claimed_job = self._jobs.claim_next_job(
            worker_id=worker_id,
            stale_after=self._stale_after,
            limits=self._claim_limits,
        )
        self._db.commit()
        if claimed_job is None:
            return ReportWorkerRunResult(
                claimed=False,
                job_id=None,
                report_id=None,
                status="idle",
            )

        scope = _claimed_job_scope(claimed_job)
        report: EngagementReport | None = None
        try:
            report = self._create_generating_report(scope)
            linked_job = self._worker_repository.link_report_job_attempt_by_id(
                job_id=scope.job_id,
                report_id=report.id,
            )
            if linked_job is None:
                raise _ReportWorkerFailure(
                    reason=REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
                    safe_message="Report generation could not persist the report attempt.",
                )
            self._db.commit()
            return await self._generate_report(scope=scope, report=report)
        except Exception as exc:
            self._db.rollback()
            phase = self._current_generation_phase(
                job_id=scope.job_id,
                fallback=scope.generation_phase,
            )
            if not _is_expected_failure(exc):
                logger.exception(
                    "Unexpected report worker failure job_id=%s report_id=%s phase=%s section_id=%s",
                    scope.job_id,
                    report.id if report is not None else None,
                    phase,
                    self._current_section_id(job_id=scope.job_id),
                )
            failure = _safe_failure(exc, phase=phase)
            return self._persist_failure(
                scope=scope,
                report_id=report.id if report is not None else None,
                failure=failure,
            )

    def _current_generation_phase(self, *, job_id: UUID, fallback: str) -> str:
        try:
            job = self._worker_repository.get_report_job_by_id(job_id=job_id)
        except Exception:
            return fallback
        return str(getattr(job, "generation_phase", fallback) or fallback)

    def _current_section_id(self, *, job_id: UUID) -> str | None:
        try:
            job = self._worker_repository.get_report_job_by_id(job_id=job_id)
        except Exception:
            return None
        value = getattr(job, "current_section_id", None)
        return str(value) if value is not None else None


async def run_report_worker_once(
    db: Session,
    *,
    worker_id: str,
    claim_limits: ReportJobClaimLimits | None = None,
    stale_after: timedelta = _DEFAULT_STALE_AFTER,
) -> ReportWorkerRunResult:
    """Run one report worker claim using default production collaborators."""

    return await ReportWorker(
        db,
        claim_limits=claim_limits,
        stale_after=stale_after,
    ).run_once(worker_id=worker_id)


def _claimed_job_scope(job: EngagementReportJob) -> _ClaimedJobScope:
    return _ClaimedJobScope(
        job_id=job.id,
        tenant_id=int(job.tenant_id),
        user_id=int(job.user_id),
        requested_by_user_id=int(job.requested_by_user_id),
        engagement_id=int(job.engagement_id),
        report_type=str(job.report_type),
        selected_task_memo_ids=tuple(str(item) for item in job.selected_task_memo_ids),
        include_candidate_findings=bool(job.include_candidate_findings),
        report_id=job.report_id,
        completed_section_ids=tuple(
            str(section_id) for section_id in (job.completed_sections or [])
        ),
        llm_runtime_selection=(
            dict(job.llm_runtime_selection)
            if isinstance(job.llm_runtime_selection, Mapping)
            else {}
        ),
        generation_phase=str(
            getattr(job, "generation_phase", REPORT_GENERATION_PHASE_SECTIONS)
            or REPORT_GENERATION_PHASE_SECTIONS
        ),
    )
__all__ = [
    "ReportWorker",
    "run_report_worker_once",
]
