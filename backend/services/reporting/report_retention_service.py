"""Automatic report retention and deletion finalization service.

This service applies tenant data-management policy to generated report rows and
finalizes expired pending deletions by erasing generated content.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy.orm import Session

from backend.config.retention import (
    DEFAULT_REPORT_RETENTION_ENABLED,
    DEFAULT_REPORT_HISTORY_RETENTION_DAYS,
    DEFAULT_REPORT_JOB_RETENTION_DAYS,
    DEFAULT_TASK_MEMO_HISTORY_RETENTION_DAYS,
)
from backend.core.time_utils import utc_now
from backend.models.data_management import TenantDataManagementSettings
from backend.models.tenant import Tenant
from backend.repositories.reporting.engagement_report_repository import (
    EngagementReportRepository,
)
from backend.repositories.reporting.reporting_retention_repository import (
    ReportingRetentionRepository,
)

if TYPE_CHECKING:
    from backend.services.retention.contracts import (
        RetentionDecision,
        RetentionExecutorResult,
    )

REPORT_DELETION_REASON_RETENTION = "retention"
RETENTION_CLASS_REPORTING = "reporting"
HISTORICAL_REPORT_RETENTION_EXPIRED = "historical_report_retention_expired"
CURRENT_READY_REPORT_PROTECTED = "current_ready_report_protected"
CURRENT_READY_TASK_MEMO_PROTECTED = "current_ready_task_memo_protected"
REPORT_JOB_RETENTION_EXPIRED = "report_job_retention_expired"
TASK_MEMO_HISTORY_RETENTION_EXPIRED = "task_memo_history_retention_expired"
PENDING_REPORT_DELETION_FINALIZATION_EXPIRED = (
    "pending_report_deletion_finalization_expired"
)


class SupportsReportRetentionPolicy(Protocol):
    """Policy fields consumed by the reporting retention executor."""

    report_history_retention_days: int
    report_retention_enabled: bool
    report_job_retention_days: int
    task_memo_history_retention_days: int
    retention_batch_size_per_tenant: int


@dataclass(frozen=True, slots=True)
class ReportRetentionRunResult:
    """Summary counters for one report retention run."""

    dry_run: bool
    tenant_count: int
    retention_candidate_count: int
    retention_finalized_count: int
    pending_finalized_count: int
    protected_current_count: int = 0
    report_job_candidate_count: int = 0
    report_job_deleted_count: int = 0
    task_memo_history_candidate_count: int = 0
    task_memo_history_deleted_count: int = 0
    protected_current_memo_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "tenant_count": int(self.tenant_count),
            "retention_candidate_count": int(self.retention_candidate_count),
            "retention_finalized_count": int(self.retention_finalized_count),
            "pending_finalized_count": int(self.pending_finalized_count),
            "protected_current_count": int(self.protected_current_count),
            "report_job_candidate_count": int(self.report_job_candidate_count),
            "report_job_deleted_count": int(self.report_job_deleted_count),
            "task_memo_history_candidate_count": int(
                self.task_memo_history_candidate_count
            ),
            "task_memo_history_deleted_count": int(
                self.task_memo_history_deleted_count
            ),
            "protected_current_memo_count": int(self.protected_current_memo_count),
        }


class ReportRetentionService:
    """Apply configured report retention and finalize expired deletions."""

    def __init__(
        self,
        db: Session,
        *,
        repository: (
            EngagementReportRepository | ReportingRetentionRepository | None
        ) = None,
    ) -> None:
        self._db = db
        if repository:
            self._report_repository = repository
            self._retention_repository = repository
        else:
            self._report_repository = EngagementReportRepository(db)
            self._retention_repository = ReportingRetentionRepository(db)

    def run(
        self,
        *,
        dry_run: bool = True,
        tenant_id: int | None = None,
        limit_per_tenant: int = 100,
        report_history_retention_days: int | None = None,
        report_retention_enabled: bool | None = None,
        report_job_retention_days: int | None = None,
        task_memo_history_retention_days: int | None = None,
        manage_transaction: bool = True,
    ) -> ReportRetentionRunResult:
        """Run automatic report retention and deletion finalization."""

        now = utc_now()
        tenant_settings = self._tenant_settings(tenant_id=tenant_id)
        retention_candidate_count = 0
        retention_finalized_count = 0
        pending_finalized_count = 0
        protected_current_count = 0
        report_job_candidate_count = 0
        report_job_deleted_count = 0
        task_memo_history_candidate_count = 0
        task_memo_history_deleted_count = 0
        protected_current_memo_count = 0

        for settings in tenant_settings:
            retention_days = _normalize_positive_int(
                (
                    report_history_retention_days
                    if report_history_retention_days is not None
                    else (
                        settings.report_history_retention_days
                        or DEFAULT_REPORT_HISTORY_RETENTION_DAYS
                    )
                ),
                field_name="report_history_retention_days",
            )
            job_retention_days = _normalize_positive_int(
                (
                    report_job_retention_days
                    if report_job_retention_days is not None
                    else (
                        settings.report_job_retention_days
                        or DEFAULT_REPORT_JOB_RETENTION_DAYS
                    )
                ),
                field_name="report_job_retention_days",
            )
            memo_retention_days = _normalize_positive_int(
                (
                    task_memo_history_retention_days
                    if task_memo_history_retention_days is not None
                    else (
                        settings.task_memo_history_retention_days
                        or DEFAULT_TASK_MEMO_HISTORY_RETENTION_DAYS
                    )
                ),
                field_name="task_memo_history_retention_days",
            )
            cutoff = now - timedelta(days=retention_days)
            job_cutoff = now - timedelta(days=job_retention_days)
            memo_cutoff = now - timedelta(days=memo_retention_days)
            effective_report_retention_enabled = (
                bool(report_retention_enabled)
                if report_retention_enabled is not None
                else bool(settings.report_retention_enabled)
            )
            candidates = (
                self._retention_repository.list_retention_candidate_reports(
                    tenant_id=int(settings.tenant_id),
                    generated_before=cutoff,
                    limit=limit_per_tenant,
                )
                if effective_report_retention_enabled
                else []
            )
            retention_candidate_count += len(candidates)
            if effective_report_retention_enabled:
                protected_current_count += (
                    self._retention_repository.count_retention_protected_current_reports(
                        tenant_id=int(settings.tenant_id),
                        generated_before=cutoff,
                    )
                )
            remaining_limit = max(0, int(limit_per_tenant) - len(candidates))
            if dry_run:
                pending = (
                    self._retention_repository.list_reports_pending_deletion(
                        now=now,
                        tenant_id=int(settings.tenant_id),
                        limit=remaining_limit,
                    )
                    if remaining_limit
                    else []
                )
                pending_finalized_count += len(pending)
                remaining_limit = max(0, remaining_limit - len(pending))
                report_jobs = (
                    self._retention_repository.list_retention_candidate_report_jobs(
                        tenant_id=int(settings.tenant_id),
                        finished_before=job_cutoff,
                        limit=remaining_limit,
                    )
                    if remaining_limit
                    else []
                )
                report_job_candidate_count += len(report_jobs)
                remaining_limit = max(0, remaining_limit - len(report_jobs))
                task_memos = (
                    self._retention_repository.list_retention_candidate_task_memos(
                        tenant_id=int(settings.tenant_id),
                        memo_before=memo_cutoff,
                        limit=remaining_limit,
                    )
                    if remaining_limit
                    else []
                )
                task_memo_history_candidate_count += len(task_memos)
                protected_current_memo_count += (
                    self._retention_repository.count_retention_protected_current_task_memos(
                        tenant_id=int(settings.tenant_id),
                        memo_before=memo_cutoff,
                    )
                )
                continue
            for report in candidates:
                report.delete_scheduled_at = now
                report.delete_undo_until = now
                report.deleted_by_user_id = None
                report.deletion_reason = REPORT_DELETION_REASON_RETENTION
                report.deletion_original_is_current = False
                report.deletion_metadata = {
                    "retention_days": retention_days,
                    "retention_cutoff": cutoff.isoformat(),
                }
                self._report_repository.finalize_report_deletion(
                    report=report,
                    finalized_at=now,
                )
                retention_finalized_count += 1

            pending = (
                self._retention_repository.list_reports_pending_deletion(
                    now=now,
                    tenant_id=int(settings.tenant_id),
                    limit=remaining_limit,
                )
                if remaining_limit
                else []
            )
            pending_finalized_count += len(pending)
            for report in pending:
                self._report_repository.finalize_report_deletion(
                    report=report,
                    finalized_at=now,
                )
            remaining_limit = max(0, remaining_limit - len(pending))
            report_jobs = (
                self._retention_repository.list_retention_candidate_report_jobs(
                    tenant_id=int(settings.tenant_id),
                    finished_before=job_cutoff,
                    limit=remaining_limit,
                )
                if remaining_limit
                else []
            )
            report_job_candidate_count += len(report_jobs)
            report_job_deleted_count += self._retention_repository.delete_report_jobs(
                report_jobs
            )
            remaining_limit = max(0, remaining_limit - len(report_jobs))
            task_memos = (
                self._retention_repository.list_retention_candidate_task_memos(
                    tenant_id=int(settings.tenant_id),
                    memo_before=memo_cutoff,
                    limit=remaining_limit,
                )
                if remaining_limit
                else []
            )
            task_memo_history_candidate_count += len(task_memos)
            task_memo_history_deleted_count += (
                self._retention_repository.delete_task_memos(task_memos)
            )
            protected_current_memo_count += (
                self._retention_repository.count_retention_protected_current_task_memos(
                    tenant_id=int(settings.tenant_id),
                    memo_before=memo_cutoff,
                )
            )
        if not dry_run:
            if manage_transaction:
                self._db.commit()
        elif manage_transaction:
            self._db.rollback()

        return ReportRetentionRunResult(
            dry_run=bool(dry_run),
            tenant_count=len(tenant_settings),
            retention_candidate_count=retention_candidate_count,
            retention_finalized_count=retention_finalized_count,
            pending_finalized_count=pending_finalized_count,
            protected_current_count=protected_current_count,
            report_job_candidate_count=report_job_candidate_count,
            report_job_deleted_count=report_job_deleted_count,
            task_memo_history_candidate_count=task_memo_history_candidate_count,
            task_memo_history_deleted_count=task_memo_history_deleted_count,
            protected_current_memo_count=protected_current_memo_count,
        )

    def _tenant_settings(
        self,
        *,
        tenant_id: int | None,
    ) -> list[TenantDataManagementSettings]:
        query = self._db.query(TenantDataManagementSettings)
        if tenant_id is not None:
            query = query.filter(TenantDataManagementSettings.tenant_id == int(tenant_id))
        rows = list(query.order_by(TenantDataManagementSettings.tenant_id.asc()).all())
        if tenant_id is not None:
            return rows or [
                TenantDataManagementSettings(
                    tenant_id=int(tenant_id),
                    report_retention_enabled=DEFAULT_REPORT_RETENTION_ENABLED,
                    report_history_retention_days=DEFAULT_REPORT_HISTORY_RETENTION_DAYS,
                )
            ]

        configured_tenant_ids = {int(row.tenant_id) for row in rows}
        tenants = self._db.query(Tenant).order_by(Tenant.id.asc()).all()
        for tenant in tenants:
            if int(tenant.id) in configured_tenant_ids:
                continue
            rows.append(
                TenantDataManagementSettings(
                    tenant_id=int(tenant.id),
                    report_retention_enabled=DEFAULT_REPORT_RETENTION_ENABLED,
                    report_history_retention_days=DEFAULT_REPORT_HISTORY_RETENTION_DAYS,
                    report_job_retention_days=DEFAULT_REPORT_JOB_RETENTION_DAYS,
                    task_memo_history_retention_days=(
                        DEFAULT_TASK_MEMO_HISTORY_RETENTION_DAYS
                    ),
                )
            )
        return rows


@dataclass(frozen=True, slots=True)
class ReportRetentionExecutor:
    """Run bounded report retention through the shared executor contract."""

    db: Session
    name: str = "reporting.retention"
    retention_class: str = RETENTION_CLASS_REPORTING

    def run(
        self,
        *,
        policy: SupportsReportRetentionPolicy,
        tenant_id: int,
        mode: str,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate and optionally erase tenant-scoped historical report content."""

        from backend.services.retention.contracts import (
            RETENTION_RUN_MODE_APPLY,
            RETENTION_RUN_MODE_DRY_RUN,
            RetentionBatchCounts,
            RetentionExecutorResult,
            validate_run_mode,
        )

        run_mode = validate_run_mode(mode)
        scoped_tenant_id = _normalize_positive_int(tenant_id, field_name="tenant_id")
        effective_limit = min(
            _normalize_positive_int(limit, field_name="limit"),
            _normalize_positive_int(
                policy.retention_batch_size_per_tenant,
                field_name="policy.retention_batch_size_per_tenant",
            ),
        )
        retention_days = _normalize_positive_int(
            policy.report_history_retention_days,
            field_name="policy.report_history_retention_days",
        )
        job_retention_days = _normalize_positive_int(
            getattr(
                policy,
                "report_job_retention_days",
                DEFAULT_REPORT_JOB_RETENTION_DAYS,
            ),
            field_name="policy.report_job_retention_days",
        )
        memo_retention_days = _normalize_positive_int(
            getattr(
                policy,
                "task_memo_history_retention_days",
                DEFAULT_TASK_MEMO_HISTORY_RETENTION_DAYS,
            ),
            field_name="policy.task_memo_history_retention_days",
        )
        result = ReportRetentionService(self.db).run(
            dry_run=run_mode == RETENTION_RUN_MODE_DRY_RUN,
            tenant_id=scoped_tenant_id,
            limit_per_tenant=effective_limit,
                report_history_retention_days=retention_days,
                report_retention_enabled=bool(policy.report_retention_enabled),
                report_job_retention_days=job_retention_days,
            task_memo_history_retention_days=memo_retention_days,
            manage_transaction=False,
        )
        total_candidates = (
            result.retention_candidate_count + result.pending_finalized_count
            + result.report_job_candidate_count
            + result.task_memo_history_candidate_count
        )
        protected_count = (
            result.protected_current_count + result.protected_current_memo_count
        )
        scanned_count = total_candidates + protected_count

        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=RETENTION_CLASS_REPORTING,
            mode=run_mode,
            tenant_id=scoped_tenant_id,
            counts=RetentionBatchCounts(
                scanned_count=scanned_count,
                candidate_count=total_candidates,
                protected_count=protected_count,
                applied_count=(
                    result.retention_finalized_count
                    + result.pending_finalized_count
                    + result.report_job_deleted_count
                    + result.task_memo_history_deleted_count
                    if run_mode == RETENTION_RUN_MODE_APPLY
                    else 0
                ),
                preserved_count=protected_count,
                batch_count=scanned_count,
                batch_limit=effective_limit,
            ),
            reason_counts=_build_reason_counts(result),
            decisions=_build_decisions(result=result, mode=run_mode),
        )


def _build_reason_counts(result: ReportRetentionRunResult) -> dict[str, int]:
    reason_counts: dict[str, int] = {}
    if result.retention_candidate_count:
        reason_counts[HISTORICAL_REPORT_RETENTION_EXPIRED] = int(
            result.retention_candidate_count
        )
    if result.pending_finalized_count:
        reason_counts[PENDING_REPORT_DELETION_FINALIZATION_EXPIRED] = int(
            result.pending_finalized_count
        )
    if result.protected_current_count:
        reason_counts[CURRENT_READY_REPORT_PROTECTED] = int(
            result.protected_current_count
        )
    if result.report_job_candidate_count:
        reason_counts[REPORT_JOB_RETENTION_EXPIRED] = int(
            result.report_job_candidate_count
        )
    if result.task_memo_history_candidate_count:
        reason_counts[TASK_MEMO_HISTORY_RETENTION_EXPIRED] = int(
            result.task_memo_history_candidate_count
        )
    if result.protected_current_memo_count:
        reason_counts[CURRENT_READY_TASK_MEMO_PROTECTED] = int(
            result.protected_current_memo_count
        )
    return reason_counts


def _build_decisions(
    *,
    result: ReportRetentionRunResult,
    mode: str,
) -> tuple[RetentionDecision, ...]:
    from backend.services.retention.contracts import (
        RETENTION_DECISION_APPLIED,
        RETENTION_DECISION_CANDIDATE,
        RETENTION_DECISION_PROTECTED,
        RETENTION_RUN_MODE_APPLY,
        RetentionDecision,
    )

    outcome = (
        RETENTION_DECISION_APPLIED
        if mode == RETENTION_RUN_MODE_APPLY
        else RETENTION_DECISION_CANDIDATE
    )
    decisions: list[RetentionDecision] = []
    if result.retention_candidate_count:
        decisions.append(
            RetentionDecision(
                retention_class=RETENTION_CLASS_REPORTING,
                outcome=outcome,
                reason_code=HISTORICAL_REPORT_RETENTION_EXPIRED,
                count=int(result.retention_candidate_count),
            )
        )
    if result.pending_finalized_count:
        decisions.append(
            RetentionDecision(
                retention_class=RETENTION_CLASS_REPORTING,
                outcome=outcome,
                reason_code=PENDING_REPORT_DELETION_FINALIZATION_EXPIRED,
                count=int(result.pending_finalized_count),
            )
        )
    if result.protected_current_count:
        decisions.append(
            RetentionDecision(
                retention_class=RETENTION_CLASS_REPORTING,
                outcome=RETENTION_DECISION_PROTECTED,
                reason_code=CURRENT_READY_REPORT_PROTECTED,
                count=int(result.protected_current_count),
            )
        )
    if result.report_job_candidate_count:
        decisions.append(
            RetentionDecision(
                retention_class=RETENTION_CLASS_REPORTING,
                outcome=outcome,
                reason_code=REPORT_JOB_RETENTION_EXPIRED,
                count=int(result.report_job_candidate_count),
            )
        )
    if result.task_memo_history_candidate_count:
        decisions.append(
            RetentionDecision(
                retention_class=RETENTION_CLASS_REPORTING,
                outcome=outcome,
                reason_code=TASK_MEMO_HISTORY_RETENTION_EXPIRED,
                count=int(result.task_memo_history_candidate_count),
            )
        )
    if result.protected_current_memo_count:
        decisions.append(
            RetentionDecision(
                retention_class=RETENTION_CLASS_REPORTING,
                outcome=RETENTION_DECISION_PROTECTED,
                reason_code=CURRENT_READY_TASK_MEMO_PROTECTED,
                count=int(result.protected_current_memo_count),
            )
        )
    return tuple(decisions)


def _normalize_positive_int(value: object, *, field_name: str) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if normalized < 1:
        raise ValueError(f"{field_name} must be positive")
    return normalized


__all__ = [
    "CURRENT_READY_REPORT_PROTECTED",
    "CURRENT_READY_TASK_MEMO_PROTECTED",
    "HISTORICAL_REPORT_RETENTION_EXPIRED",
    "PENDING_REPORT_DELETION_FINALIZATION_EXPIRED",
    "REPORT_DELETION_REASON_RETENTION",
    "REPORT_JOB_RETENTION_EXPIRED",
    "ReportRetentionExecutor",
    "ReportRetentionRunResult",
    "ReportRetentionService",
    "TASK_MEMO_HISTORY_RETENTION_EXPIRED",
]
