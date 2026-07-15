"""Execute report attempts without owning worker claims or dependency setup.

This module is limited to attempt preparation, context construction, durable
finalization, rendering, and ready promotion through initialized collaborators.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from backend.models.core import Engagement
from backend.models.reporting import EngagementReport
from backend.services.reporting.contracts import (
    REPORT_GENERATION_ERROR_CONTEXT_UNAVAILABLE,
    REPORT_GENERATION_ERROR_FINALIZATION_FAILED,
    REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
    REPORT_GENERATION_PHASE_FINALIZING,
)
from backend.services.reporting.report_context_builder import ReportContext
from backend.services.reporting.report_finalization_checkpoint import (
    ReportFinalizationCheckpointError,
    build_finalization_checkpoint,
    checkpoint_generation_metadata,
    final_generation_metadata,
    load_finalization_checkpoint,
)
from backend.services.reporting.report_generation_service import (
    ReportGenerationRequestError,
    ReportGenerationService,
)
from backend.services.reporting.report_section_plan import get_report_section_plan
from backend.services.reporting.report_worker_failure import _ReportWorkerFailure
from backend.services.reporting.report_worker_sections import (
    _ReportWorkerSectionExecution,
)
from backend.services.reporting.report_worker_types import (
    ReportWorkerRunResult,
    _ClaimedJobScope,
)

logger = logging.getLogger("backend.services.reporting.report_worker")


class _ReportWorkerAttemptExecution(_ReportWorkerSectionExecution):
    """Prepare, generate, finalize, and promote one initialized report attempt."""

    def _create_generating_report(
        self,
        scope: _ClaimedJobScope,
    ) -> EngagementReport:
        if scope.report_id is not None:
            existing = self._report_repository.get_report_by_id(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                engagement_id=scope.engagement_id,
                report_id=scope.report_id,
            )
            if existing is not None and str(existing.status) == "generating":
                return existing

        version = self._report_repository.next_report_version(
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            engagement_id=scope.engagement_id,
            report_type=scope.report_type,
        )
        engagement = self._load_source_engagement(scope)
        return self._report_repository.create_report_attempt(
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            created_by_user_id=scope.requested_by_user_id,
            engagement_id=scope.engagement_id,
            report_type=scope.report_type,
            version=version,
            title=_report_title(scope.report_type),
            source_task_memo_ids=scope.selected_task_memo_ids,
            engagement_name_snapshot=(
                str(engagement.name) if engagement is not None else None
            ),
            engagement_status_snapshot=(
                str(engagement.status) if engagement is not None else None
            ),
            generation_metadata={},
        )

    def _load_source_engagement(self, scope: _ClaimedJobScope) -> Engagement | None:
        """Return live engagement metadata for immutable report snapshots."""

        return (
            self._db.query(Engagement)
            .filter(
                Engagement.id == int(scope.engagement_id),
                Engagement.tenant_id == int(scope.tenant_id),
                Engagement.user_id == int(scope.user_id),
            )
            .one_or_none()
        )

    async def _generate_report(
        self,
        *,
        scope: _ClaimedJobScope,
        report: EngagementReport,
    ) -> ReportWorkerRunResult:
        section_plan = get_report_section_plan(scope.report_type)
        if scope.generation_phase == REPORT_GENERATION_PHASE_FINALIZING:
            try:
                checkpoint = load_finalization_checkpoint(report)
            except ReportFinalizationCheckpointError as exc:
                raise _ReportWorkerFailure(
                    reason=REPORT_GENERATION_ERROR_FINALIZATION_FAILED,
                    safe_message=exc.safe_message,
                    metadata=(
                        {"failure_class": exc.failure_class}
                        if exc.failure_class is not None
                        else None
                    ),
                    phase=REPORT_GENERATION_PHASE_FINALIZING,
                ) from exc
        else:
            selected_memos = self._validate_selected_memos(scope)
            context = self._build_context(scope=scope, selected_memos=selected_memos)
            self._diagnostics.context_built(
                job_id=scope.job_id,
                report_id=report.id,
                engagement_id=scope.engagement_id,
                report_type=scope.report_type,
                context=context,
            )
            sections, section_metadata = await self._generate_sections(
                scope=scope,
                report=report,
                context=context,
                section_plan=section_plan,
            )
            checkpoint = build_finalization_checkpoint(
                sections=sections,
                section_metadata=section_metadata,
                context=context,
                section_plan=section_plan,
            )
            checkpointed_report = self._report_repository.update_report_sections(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                engagement_id=scope.engagement_id,
                report_id=report.id,
                sections=sections,
                generation_metadata=checkpoint_generation_metadata(checkpoint),
            )
            checkpointed_job = self._jobs.mark_progress(
                job_id=scope.job_id,
                current_section_id=None,
                completed_sections=[item.section_id for item in section_plan.sections],
                total_sections=len(section_plan.sections),
                generation_phase=REPORT_GENERATION_PHASE_FINALIZING,
            )
            if checkpointed_report is None or checkpointed_job is None:
                raise _ReportWorkerFailure(
                    reason=REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
                    safe_message="Report generation could not persist finalization state.",
                )
            self._db.commit()
        self._diagnostics.finalization_started(
            job_id=scope.job_id,
            report_id=report.id,
            engagement_id=scope.engagement_id,
            report_type=scope.report_type,
        )
        try:
            rendered = self._markdown_renderer.render(
                title=report.title,
                report_type=scope.report_type,
                sections=checkpoint.sections,
                evidence_timeline=checkpoint.evidence_timeline,
            )
        except Exception as exc:
            logger.exception(
                "Report finalization rendering failed job_id=%s report_id=%s phase=%s",
                scope.job_id,
                report.id,
                REPORT_GENERATION_PHASE_FINALIZING,
            )
            raise _ReportWorkerFailure(
                reason=REPORT_GENERATION_ERROR_FINALIZATION_FAILED,
                safe_message="Report finalization failed.",
                metadata={"failure_class": exc.__class__.__name__},
                retryable=False,
                phase=REPORT_GENERATION_PHASE_FINALIZING,
            ) from exc
        ready_report = self._report_repository.mark_report_ready(
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            engagement_id=scope.engagement_id,
            report_id=report.id,
            markdown_snapshot=rendered.markdown_snapshot,
            source_task_memo_ids=checkpoint.source_task_memo_ids,
            source_knowledge_refs=checkpoint.source_knowledge_refs,
            source_evidence_refs=checkpoint.source_evidence_refs,
            generation_metadata=final_generation_metadata(
                checkpoint=checkpoint,
                renderer_metadata=rendered.generation_metadata,
                llm_runtime_selection=scope.llm_runtime_selection,
            ),
            generated_at=datetime.now(UTC),
        )
        if ready_report is None:
            raise _ReportWorkerFailure(
                reason=REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
                safe_message="Report generation could not persist the ready report.",
                phase=REPORT_GENERATION_PHASE_FINALIZING,
            )

        ready_job = self._scoped_job_repository.mark_report_job_ready(
            tenant_id=scope.tenant_id,
            user_id=scope.user_id,
            engagement_id=scope.engagement_id,
            job_id=scope.job_id,
            report_id=ready_report.id,
            finished_at=datetime.now(UTC),
        )
        if ready_job is None:
            raise _ReportWorkerFailure(
                reason=REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
                safe_message="Report generation could not mark the job ready.",
                phase=REPORT_GENERATION_PHASE_FINALIZING,
            )
        self._db.commit()
        self._diagnostics.job_ready(
            job_id=ready_job.id,
            report_id=ready_report.id,
            engagement_id=scope.engagement_id,
            report_type=scope.report_type,
            completed_sections=len(section_plan.sections),
            total_sections=len(section_plan.sections),
        )
        return ReportWorkerRunResult(
            claimed=True,
            job_id=ready_job.id,
            report_id=ready_report.id,
            status=str(ready_job.status),
        )

    def _validate_selected_memos(
        self,
        scope: _ClaimedJobScope,
    ) -> Sequence[object]:
        validator = ReportGenerationService(
            self._db,
            memo_repository=self._memo_repository,
            report_repository=self._report_repository,
            request_job_repository=self._scoped_job_repository,
            source_watermarks=self._source_watermarks,
        )
        try:
            return validator.validate_selected_current_ready_memos(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                engagement_id=scope.engagement_id,
                selected_task_memo_ids=scope.selected_task_memo_ids,
            )
        except ReportGenerationRequestError as exc:
            raise _ReportWorkerFailure(
                reason=exc.reason,
                safe_message=exc.safe_message,
            ) from exc

    def _build_context(
        self,
        *,
        scope: _ClaimedJobScope,
        selected_memos: Sequence[object],
    ) -> ReportContext:
        try:
            return self._context_builder.build(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                engagement_id=scope.engagement_id,
                report_type=scope.report_type,
                selected_memos=selected_memos,  # type: ignore[arg-type]
                include_candidate_findings=scope.include_candidate_findings,
            )
        except _ReportWorkerFailure:
            raise
        except Exception as exc:
            raise _ReportWorkerFailure(
                reason=REPORT_GENERATION_ERROR_CONTEXT_UNAVAILABLE,
                safe_message="Report context is unavailable.",
            ) from exc


def _report_title(report_type: str) -> str:
    return f"{report_type.replace('_', ' ').title()} Report"
