"""Validate engagement report generation requests before job creation.

This module owns request-level report generation checks that must happen before
selected memo lookup or durable job creation. It does not build report context,
call LLM providers, execute workers, or mutate report rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.reporting import (
    EngagementReport,
    EngagementReportJob,
    TaskClosureMemo,
)
from backend.repositories.reporting.engagement_report_job_repository import (
    EngagementReportJobRepository,
)
from backend.repositories.reporting.engagement_report_repository import (
    EngagementReportRepository,
)
from backend.repositories.reporting.task_closure_memo_repository import (
    TaskClosureMemoRepository,
)
from backend.services.llm_provider import ProviderConfigurationError
from backend.services.llm_provider.reporting_selection_service import (
    ReportingLLMSelectionMissingError,
    ReportingLLMSelectionService,
)
from backend.services.reporting.contracts import (
    REPORT_GENERATION_ERROR_DUPLICATE_SELECTED_TASK_MEMO_IDS,
    REPORT_GENERATION_ERROR_INVALID_REQUEST,
    REPORT_GENERATION_ERROR_LLM_RUNTIME_UNAVAILABLE,
    REPORT_GENERATION_ERROR_STALE_MEMO,
    REPORT_GENERATION_ERROR_UNSUPPORTED_MEMO_MIX,
    MEMO_MODE_SUPPORTED,
    REPORT_STATUS_READY,
    ReportGenerationServiceErrorReason,
    ReportType,
    validate_report_type,
)
from backend.services.reporting.report_section_plan import get_report_section_plan
from backend.services.reporting.reporting_state_service import watermarks_match
from backend.services.reporting.source_watermark_service import (
    ReportSourceMemoWatermarkInput,
    SourceWatermarkService,
)


_IDEMPOTENCY_KEY_PREFIX = "engagement-report-generation"


class ReportGenerationRequestError(Exception):
    """Typed report generation request failure safe for router error mapping."""

    def __init__(
        self,
        *,
        reason: ReportGenerationServiceErrorReason,
        safe_message: str,
    ) -> None:
        super().__init__(safe_message)
        self.reason = reason
        self.safe_message = safe_message


@dataclass(frozen=True, slots=True)
class ReportGenerationResult:
    """Outcome of accepting or replaying a report generation request."""

    job_id: UUID | None
    report_id: UUID | None
    status: str


class ReportGenerationService:
    """Validate report generation inputs before orchestration or persistence."""

    def __init__(
        self,
        db: Session,
        *,
        memo_repository: TaskClosureMemoRepository | None = None,
        report_repository: EngagementReportRepository | None = None,
        request_job_repository: EngagementReportJobRepository | None = None,
        source_watermarks: SourceWatermarkService | None = None,
        reporting_selection_service: ReportingLLMSelectionService | None = None,
    ) -> None:
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
        self._job_repository = (
            request_job_repository
            if request_job_repository is not None
            else EngagementReportJobRepository(db)
        )
        self._source_watermarks = source_watermarks or SourceWatermarkService(db)
        self._reporting_selection_service = (
            reporting_selection_service or ReportingLLMSelectionService(db)
        )

    def request_generation(
        self,
        *,
        tenant_id: int,
        user_id: int,
        requested_by_user_id: int,
        engagement_id: int,
        report_type: str,
        selected_task_memo_ids: Sequence[str | UUID],
        engagement_is_owned: bool,
        include_candidate_findings: bool = False,
        force_regenerate: bool = False,
        task_ids: Sequence[int] | None = None,
    ) -> ReportGenerationResult:
        """Create or replay a durable report generation job without LLM work."""

        if task_ids is not None:
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_INVALID_REQUEST,
                safe_message="Report generation requires selected task memo IDs.",
            )
        validated_report_type = self._validate_report_type(report_type)
        if not engagement_is_owned:
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_INVALID_REQUEST,
                safe_message="Engagement is not available for report generation.",
            )
        if not selected_task_memo_ids:
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_INVALID_REQUEST,
                safe_message="At least one selected task memo ID is required.",
            )

        selected_memos = self.validate_selected_current_ready_memos(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            selected_task_memo_ids=selected_task_memo_ids,
        )
        source_watermark = self._compute_report_source_watermark(
            report_type=validated_report_type,
            selected_memos=selected_memos,
            include_candidate_findings=include_candidate_findings,
        )
        source_watermark_hash = str(source_watermark.get("hash") or "")
        if not source_watermark_hash:
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_INVALID_REQUEST,
                safe_message="Report generation source watermark is unavailable.",
            )
        runtime_selection_payload = self._resolve_reporting_runtime_selection(
            user_id=requested_by_user_id
        )
        idempotency_key = self._idempotency_key(
            tenant_id=tenant_id,
            user_id=user_id,
            requested_by_user_id=requested_by_user_id,
            engagement_id=engagement_id,
            report_type=validated_report_type,
            source_watermark_hash=source_watermark_hash,
            runtime_selection_payload=runtime_selection_payload,
        )

        active_job = self._job_repository.get_active_job_by_idempotency_key(
            tenant_id=tenant_id,
            user_id=user_id,
            requested_by_user_id=requested_by_user_id,
            engagement_id=engagement_id,
            report_type=validated_report_type,
            idempotency_key=idempotency_key,
        )
        if active_job is not None:
            return self._job_result(active_job)

        if not force_regenerate:
            current_report = self._report_repository.find_ready_current_report_by_source(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                report_type=validated_report_type,
                selected_task_memo_ids=[memo.id for memo in selected_memos],
                source_watermark_hash=source_watermark_hash,
                llm_runtime_selection=runtime_selection_payload,
            )
            if current_report is not None:
                return self._ready_report_result(current_report)

        job = self._job_repository.create_report_job(
            tenant_id=tenant_id,
            user_id=user_id,
            requested_by_user_id=requested_by_user_id,
            engagement_id=engagement_id,
            report_type=validated_report_type,
            idempotency_key=idempotency_key,
            selected_task_memo_ids=[memo.id for memo in selected_memos],
            include_candidate_findings=include_candidate_findings,
            source_watermark=self._job_source_watermark(
                source_watermark=source_watermark,
                idempotency_key=idempotency_key,
                requested_by_user_id=requested_by_user_id,
                runtime_selection_payload=runtime_selection_payload,
            ),
            llm_runtime_selection=runtime_selection_payload,
            total_sections=len(
                get_report_section_plan(validated_report_type).sections
            ),
        )
        return self._job_result(job)

    def validate_selected_current_ready_memos(
        self,
        *,
        tenant_id: int,
        user_id: int,
        engagement_id: int,
        selected_task_memo_ids: Sequence[str | UUID],
    ) -> list[TaskClosureMemo]:
        """Reject duplicate selected memo IDs before scoped memo lookup."""

        normalized_memo_ids, duplicate_memo_ids = (
            self._memo_repository.normalize_selected_memo_ids(selected_task_memo_ids)
        )
        if duplicate_memo_ids:
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_DUPLICATE_SELECTED_TASK_MEMO_IDS,
                safe_message="Selected task memo IDs must be unique.",
            )
        if len(normalized_memo_ids) != len(selected_task_memo_ids):
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_INVALID_REQUEST,
                safe_message="Selected task memo IDs must reference eligible memos.",
            )

        selected_memos = self._memo_repository.list_selected_current_ready_memos(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            selected_task_memo_ids=normalized_memo_ids,
        )
        if len(selected_memos) != len(normalized_memo_ids):
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_INVALID_REQUEST,
                safe_message="Selected task memo IDs must reference eligible memos.",
            )

        selected_tasks = self._memo_repository.get_selected_memo_tasks(
            tenant_id=tenant_id,
            user_id=user_id,
            engagement_id=engagement_id,
            selected_task_memo_ids=normalized_memo_ids,
        )
        selected_task_memo_ids = {memo_id for memo_id, _task in selected_tasks}
        if set(normalized_memo_ids) != selected_task_memo_ids:
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_INVALID_REQUEST,
                safe_message="Selected task memo IDs must reference eligible memos.",
            )

        if not any(
            str(memo.memo_mode) == MEMO_MODE_SUPPORTED for memo in selected_memos
        ):
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_UNSUPPORTED_MEMO_MIX,
                safe_message="At least one selected task memo must support report generation.",
            )

        for memo in selected_memos:
            current_source_watermark = self._source_watermarks.compute_for_task(
                tenant_id=tenant_id,
                user_id=user_id,
                engagement_id=engagement_id,
                task_id=int(memo.task_id),
            )
            if not watermarks_match(memo.source_watermark, current_source_watermark):
                raise ReportGenerationRequestError(
                    reason=REPORT_GENERATION_ERROR_STALE_MEMO,
                    safe_message="Selected task memo is stale.",
                )

        return selected_memos

    @staticmethod
    def _validate_report_type(report_type: str) -> ReportType:
        try:
            return validate_report_type(report_type)
        except ValueError as exc:
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_INVALID_REQUEST,
                safe_message="Report type is not supported.",
            ) from exc

    def _compute_report_source_watermark(
        self,
        *,
        report_type: ReportType,
        selected_memos: Sequence[TaskClosureMemo],
        include_candidate_findings: bool,
    ) -> dict[str, object]:
        return self._source_watermarks.compute_for_report(
            report_type=report_type,
            selected_memos=[
                ReportSourceMemoWatermarkInput(
                    memo_id=str(memo.id),
                    version=int(memo.version),
                    source_watermark=(
                        memo.source_watermark
                        if isinstance(memo.source_watermark, Mapping)
                        else {}
                    ),
                )
                for memo in selected_memos
            ],
            include_candidate_findings=include_candidate_findings,
        )

    @staticmethod
    def _idempotency_key(
        *,
        tenant_id: int,
        user_id: int,
        requested_by_user_id: int,
        engagement_id: int,
        report_type: ReportType,
        source_watermark_hash: str,
        runtime_selection_payload: Mapping[str, object],
    ) -> str:
        return (
            f"{_IDEMPOTENCY_KEY_PREFIX}:"
            f"{int(tenant_id)}:{int(user_id)}:{int(requested_by_user_id)}:"
            f"{int(engagement_id)}:"
            f"{report_type}:{source_watermark_hash}:"
            f"{_runtime_selection_key(runtime_selection_payload)}"
        )

    @staticmethod
    def _job_source_watermark(
        *,
        source_watermark: dict[str, object],
        idempotency_key: str,
        requested_by_user_id: int,
        runtime_selection_payload: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            **dict(source_watermark),
            "llm_runtime_selection": dict(runtime_selection_payload),
            "idempotency": {
                "key": idempotency_key,
                "requested_by_user_id": int(requested_by_user_id),
            },
        }

    def _resolve_reporting_runtime_selection(self, *, user_id: int) -> dict[str, object]:
        try:
            return self._reporting_selection_service.build_runtime_selection(
                user_id=user_id
            ).to_dict()
        except ReportingLLMSelectionMissingError as exc:
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_LLM_RUNTIME_UNAVAILABLE,
                safe_message="Reporting model is not configured.",
            ) from exc
        except ProviderConfigurationError as exc:
            raise ReportGenerationRequestError(
                reason=REPORT_GENERATION_ERROR_LLM_RUNTIME_UNAVAILABLE,
                safe_message=str(exc) or "Reporting model is not runnable.",
            ) from exc

    @staticmethod
    def _job_result(job: EngagementReportJob) -> ReportGenerationResult:
        return ReportGenerationResult(
            job_id=job.id,
            report_id=None,
            status=str(job.status),
        )

    @staticmethod
    def _ready_report_result(report: EngagementReport) -> ReportGenerationResult:
        return ReportGenerationResult(
            job_id=None,
            report_id=report.id,
            status=REPORT_STATUS_READY,
        )


def _runtime_selection_key(value: Mapping[str, object]) -> str:
    provider = str(value.get("provider") or value.get("legacy_provider") or "unknown")
    model = str(value.get("model") or value.get("legacy_model") or "unknown")
    deployment_ref = value.get("deployment_ref")
    deployment_id = ""
    deployment_revision = ""
    if isinstance(deployment_ref, Mapping):
        deployment_id = str(deployment_ref.get("deployment_id") or "")
        deployment_revision = str(deployment_ref.get("expected_revision") or "")
    reasoning_effort = str(value.get("reasoning_effort") or "")
    return f"{provider}:{model}:{deployment_id}:{deployment_revision}:{reasoning_effort}"


__all__ = [
    "ReportGenerationRequestError",
    "ReportGenerationResult",
    "ReportGenerationService",
]
