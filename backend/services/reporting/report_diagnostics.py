"""Emit safe operational diagnostics for engagement report generation."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from backend.services.metrics.utils import safe_inc
from backend.services.reporting.report_section_validation import (
    ReportSectionValidationIssue,
)

logger = logging.getLogger(__name__)

_METRIC_PREFIX = "reporting.report_generation"


class ReportDiagnostics:
    """Record bounded report generation logs and metrics without raw report text."""

    def job_claimed(
        self,
        *,
        job_id: Any,
        engagement_id: int,
        report_type: str,
        attempt_count: int,
        max_attempts: int,
    ) -> None:
        safe_inc(f"{_METRIC_PREFIX}.job_claimed_count")
        logger.info(
            "Report generation job claimed job_id=%s engagement_id=%s "
            "report_type=%s attempt=%s max_attempts=%s",
            job_id,
            engagement_id,
            report_type,
            attempt_count,
            max_attempts,
        )

    def context_built(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
        context: Any,
    ) -> None:
        logger.info(
            "Report generation context built job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s selected_memos=%s "
            "selected_tasks=%s knowledge_refs=%s evidence_refs=%s "
            "include_candidate_findings=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
            _safe_len(getattr(context, "selected_memos", ())),
            _safe_len(getattr(context, "selected_tasks", ())),
            _safe_len(getattr(context, "compatible_knowledge_refs", ())),
            _safe_len(getattr(context, "compatible_evidence_refs", ())),
            bool(
                getattr(
                    getattr(context, "candidate_policy", None),
                    "include_candidate_findings",
                    False,
                )
            ),
        )

    def section_started(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
        section_id: str,
        section_order: int,
    ) -> None:
        logger.info(
            "Report section generation started job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s section_id=%s section_order=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
            section_id,
            section_order,
        )

    def section_generation_failed(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
        section_id: str,
        section_order: int,
        reason: str,
    ) -> None:
        safe_inc(f"{_METRIC_PREFIX}.section_generation_failed_count")
        logger.warning(
            "Report section generation failed job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s section_id=%s section_order=%s "
            "reason=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
            section_id,
            section_order,
            reason,
        )

    def section_succeeded(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
        section_id: str,
        section_order: int,
        completed_sections: int,
        total_sections: int,
    ) -> None:
        safe_inc(f"{_METRIC_PREFIX}.section_succeeded_count")
        logger.info(
            "Report section generation succeeded job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s section_id=%s section_order=%s "
            "completed_sections=%s total_sections=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
            section_id,
            section_order,
            completed_sections,
            total_sections,
        )

    def section_validation_failed(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
        section_id: str,
        section_order: int,
        issues: Sequence[ReportSectionValidationIssue],
    ) -> None:
        safe_inc(f"{_METRIC_PREFIX}.section_validation_failed_count")
        issue_summaries = safe_validation_issue_summaries(issues)
        logger.warning(
            "Report section validation failed job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s section_id=%s section_order=%s "
            "issue_count=%s issues=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
            section_id,
            section_order,
            len(issue_summaries),
            issue_summaries,
        )

    def job_requeued(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
        reason: str,
        attempt_count: int,
        max_attempts: int,
    ) -> None:
        safe_inc(f"{_METRIC_PREFIX}.job_requeued_count")
        logger.warning(
            "Report generation job requeued job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s reason=%s attempt=%s "
            "max_attempts=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
            reason,
            attempt_count,
            max_attempts,
        )

    def job_failed(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
        reason: str,
        attempt_count: int | None,
        max_attempts: int | None,
    ) -> None:
        safe_inc(f"{_METRIC_PREFIX}.job_failed_count")
        logger.error(
            "Report generation job failed job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s reason=%s attempt=%s "
            "max_attempts=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
            reason,
            attempt_count,
            max_attempts,
        )

    def finalization_started(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
    ) -> None:
        logger.info(
            "Report finalization started job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
        )

    def finalization_failed(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
        reason: str,
    ) -> None:
        safe_inc(f"{_METRIC_PREFIX}.finalization_failed_count")
        logger.error(
            "Report finalization failed job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s reason=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
            reason,
        )

    def job_ready(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
        completed_sections: int,
        total_sections: int,
    ) -> None:
        safe_inc(f"{_METRIC_PREFIX}.job_ready_count")
        logger.info(
            "Report generation job ready job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s completed_sections=%s "
            "total_sections=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
            completed_sections,
            total_sections,
        )

    def stale_job_requeued(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
        reason: str,
        attempt_count: int,
        max_attempts: int,
    ) -> None:
        safe_inc(f"{_METRIC_PREFIX}.stale_job_requeued_count")
        logger.warning(
            "Stale report generation job requeued job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s reason=%s attempt=%s "
            "max_attempts=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
            reason,
            attempt_count,
            max_attempts,
        )

    def stale_job_failed(
        self,
        *,
        job_id: Any,
        report_id: Any,
        engagement_id: int,
        report_type: str,
        reason: str,
        attempt_count: int,
        max_attempts: int,
    ) -> None:
        safe_inc(f"{_METRIC_PREFIX}.stale_job_failed_count")
        logger.error(
            "Stale report generation job failed job_id=%s report_id=%s "
            "engagement_id=%s report_type=%s reason=%s attempt=%s "
            "max_attempts=%s",
            job_id,
            report_id,
            engagement_id,
            report_type,
            reason,
            attempt_count,
            max_attempts,
        )


def safe_validation_issue_summaries(
    issues: Sequence[ReportSectionValidationIssue],
) -> list[dict[str, str]]:
    """Return validation issue summaries safe for logs and persisted metadata."""

    return [
        {
            "code": str(issue.code),
            "path": str(issue.path),
        }
        for issue in issues
    ]


def _safe_len(value: Any) -> int:
    try:
        return len(value)
    except Exception:
        return 0


__all__ = [
    "ReportDiagnostics",
    "safe_validation_issue_summaries",
]
