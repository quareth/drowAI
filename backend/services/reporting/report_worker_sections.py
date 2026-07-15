"""Execute report sections without owning worker claims or attempt finalization.

This module is limited to per-section prompting, generation, validation,
diagnostics, and durable checkpoint updates through initialized collaborators.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.models.reporting import EngagementReport
from backend.services.reporting.contracts import (
    REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
    REPORT_GENERATION_PHASE_SECTIONS,
    REPORT_SECTION_SCHEMA_VERSION,
)
from backend.services.reporting.report_context_builder import ReportContext
from backend.services.reporting.report_finalization_checkpoint import (
    existing_section_metadata,
    safe_report_metadata,
)
from backend.services.reporting.report_section_generator import (
    ReportSectionGenerationError,
)
from backend.services.reporting.report_section_plan import (
    ReportSectionPlan,
    ReportSectionPlanItem,
)
from backend.services.reporting.report_section_validation import (
    ReportSectionValidationError,
)
from backend.services.reporting.report_worker_failure import _ReportWorkerFailure
from backend.services.reporting.report_worker_types import _ClaimedJobScope

_SECTION_SCHEMA_NAME = "engagement_report_section"


class _ReportWorkerSectionExecution:
    """Run and checkpoint report sections through worker-owned collaborators."""

    async def _generate_sections(
        self,
        *,
        scope: _ClaimedJobScope,
        report: EngagementReport,
        context: ReportContext,
        section_plan: ReportSectionPlan,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        sections: list[dict[str, Any]] = [
            dict(section)
            for section in (
                report.sections if isinstance(report.sections, list) else []
            )
            if isinstance(section, Mapping)
        ]
        section_metadata: list[dict[str, Any]] = existing_section_metadata(report)
        completed_section_ids: list[str] = list(scope.completed_section_ids)
        completed_section_id_set = set(completed_section_ids)
        total_sections = len(section_plan.sections)

        for section in section_plan.sections:
            if section.section_id in completed_section_id_set:
                continue

            started_job = self._jobs.mark_progress(
                job_id=scope.job_id,
                current_section_id=section.section_id,
                completed_sections=completed_section_ids,
                total_sections=total_sections,
                generation_phase=REPORT_GENERATION_PHASE_SECTIONS,
            )
            if started_job is None:
                raise _ReportWorkerFailure(
                    reason=REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
                    safe_message="Report generation could not persist section progress.",
                    phase=REPORT_GENERATION_PHASE_SECTIONS,
                )
            self._db.commit()
            self._diagnostics.section_started(
                job_id=scope.job_id,
                report_id=report.id,
                engagement_id=scope.engagement_id,
                report_type=scope.report_type,
                section_id=section.section_id,
                section_order=section.order,
            )

            rendered_prompt = self._prompt_renderer.render(
                context=context,
                section_plan_item=section,
                report_type=scope.report_type,
                candidate_policy=context.candidate_policy,
                section_schema_name=_SECTION_SCHEMA_NAME,
                section_schema_version=REPORT_SECTION_SCHEMA_VERSION,
            )
            try:
                generated = await self._section_generator.generate(
                    user_id=scope.requested_by_user_id,
                    task_id=None,
                    rendered_prompt=rendered_prompt,
                    runtime_selection=dict(scope.llm_runtime_selection),
                )
                validated = self._section_validator.validate(
                    payload=generated.payload,
                    context=context,
                    section_plan_item=section,
                )
            except ReportSectionGenerationError as exc:
                self._diagnostics.section_generation_failed(
                    job_id=scope.job_id,
                    report_id=report.id,
                    engagement_id=scope.engagement_id,
                    report_type=scope.report_type,
                    section_id=section.section_id,
                    section_order=section.order,
                    reason=str(exc.reason),
                )
                raise _ReportWorkerFailure(
                    reason=exc.reason,
                    safe_message=exc.safe_message,
                    metadata={
                        **_section_identity_metadata(section),
                        **safe_report_metadata(exc.metadata),
                    },
                    retryable=exc.retryable,
                    phase=REPORT_GENERATION_PHASE_SECTIONS,
                ) from exc
            except ReportSectionValidationError as exc:
                self._diagnostics.section_validation_failed(
                    job_id=scope.job_id,
                    report_id=report.id,
                    engagement_id=scope.engagement_id,
                    report_type=scope.report_type,
                    section_id=section.section_id,
                    section_order=section.order,
                    issues=exc.issues,
                )
                raise _ReportWorkerFailure(
                    reason=exc.reason,
                    safe_message=exc.safe_message,
                    metadata=_section_failure_metadata(
                        section=section,
                        validation_error=exc,
                    ),
                    retryable=True,
                    phase=REPORT_GENERATION_PHASE_SECTIONS,
                ) from exc
            sections.append(dict(validated.payload))
            section_metadata.append(
                {
                    "section_id": section.section_id,
                    "generation": safe_report_metadata(generated.metadata),
                    "validation": safe_report_metadata(validated.metadata),
                }
            )
            completed_section_ids.append(section.section_id)
            checkpointed_report = self._report_repository.update_report_sections(
                tenant_id=scope.tenant_id,
                user_id=scope.user_id,
                engagement_id=scope.engagement_id,
                report_id=report.id,
                sections=sections,
                generation_metadata={"sections": section_metadata},
            )
            checkpointed_job = self._jobs.mark_progress(
                job_id=scope.job_id,
                current_section_id=section.section_id,
                completed_sections=completed_section_ids,
                total_sections=total_sections,
                generation_phase=REPORT_GENERATION_PHASE_SECTIONS,
                clear_error=True,
            )
            if checkpointed_report is None or checkpointed_job is None:
                raise _ReportWorkerFailure(
                    reason=REPORT_GENERATION_ERROR_PERSISTENCE_FAILED,
                    safe_message="Report generation could not persist the section checkpoint.",
                    phase=REPORT_GENERATION_PHASE_SECTIONS,
                )
            self._db.commit()
            self._diagnostics.section_succeeded(
                job_id=scope.job_id,
                report_id=report.id,
                engagement_id=scope.engagement_id,
                report_type=scope.report_type,
                section_id=section.section_id,
                section_order=section.order,
                completed_sections=len(completed_section_ids),
                total_sections=total_sections,
            )

        return sections, section_metadata


def _section_identity_metadata(section: ReportSectionPlanItem) -> dict[str, Any]:
    return {
        "failed_section_id": str(section.section_id),
        "failed_section_order": int(section.order),
        "failed_section_type": str(section.section_type),
    }


def _section_failure_metadata(
    *,
    section: ReportSectionPlanItem,
    validation_error: ReportSectionValidationError,
) -> dict[str, Any]:
    return {
        **_section_identity_metadata(section),
        "validation_issues": [
            {"code": str(issue.code), "path": str(issue.path)}
            for issue in validation_error.issues
        ],
    }
