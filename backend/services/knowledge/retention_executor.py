"""Knowledge-owned adapter for central retention executor orchestration.

This module converts effective tenant retention policy inputs into bounded
`KnowledgeRetentionService` runs and returns the shared safe executor result
contract without owning central scheduling or transaction orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from backend.services.knowledge.retention_service import KnowledgeRetentionService
from backend.services.retention.contracts import (
    RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_PROTECTED,
    RETENTION_DECISION_SKIPPED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
    RetentionBatchCounts,
    RetentionDecision,
    RetentionExecutorResult,
    RetentionRunMode,
    TenantId,
    validate_run_mode,
)


OPERATIONAL_LOG_RETENTION_EXPIRED = "operational_log_retention_expired"
EVIDENCE_COMPACTION_ELIGIBLE_REASON = "cold_archived_file_without_active_or_replay_dependency"


class SupportsKnowledgeRetentionPolicy(Protocol):
    """Policy fields consumed by the knowledge retention executor."""

    operational_log_retention_days: int
    retention_batch_size_per_tenant: int


@dataclass(frozen=True, slots=True)
class KnowledgeRetentionExecutor:
    """Run bounded knowledge retention through the central executor contract."""

    db: Session
    name: str = "knowledge.retention"
    retention_class: str = RETENTION_CLASS_OPERATIONAL_EPHEMERAL

    def run(
        self,
        *,
        policy: SupportsKnowledgeRetentionPolicy,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate and optionally apply tenant-scoped operational cleanup."""

        run_mode = validate_run_mode(mode)
        effective_limit = min(
            _normalize_positive_int(limit, field_name="limit"),
            _normalize_positive_int(
                policy.retention_batch_size_per_tenant,
                field_name="policy.retention_batch_size_per_tenant",
            ),
        )
        result = KnowledgeRetentionService(
            self.db,
            tenant_id=tenant_id,
            operational_retention_days=_normalize_positive_int(
                policy.operational_log_retention_days,
                field_name="policy.operational_log_retention_days",
            ),
            operational_batch_limit=effective_limit,
            manage_transaction=False,
            include_durable_evidence_retention=False,
        ).run(dry_run=run_mode == RETENTION_RUN_MODE_DRY_RUN)

        operational_candidate_total = sum(
            item.candidate_count for item in result.operational_log_results
        )
        operational_deleted_total = sum(
            item.deleted_count for item in result.operational_log_results
        )
        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
            mode=run_mode,
            tenant_id=int(tenant_id),
            counts=RetentionBatchCounts(
                scanned_count=operational_candidate_total,
                candidate_count=operational_candidate_total,
                applied_count=(
                    operational_deleted_total
                    if run_mode == RETENTION_RUN_MODE_APPLY
                    else 0
                ),
                batch_count=operational_candidate_total,
                batch_limit=effective_limit,
            ),
            reason_counts=(
                {OPERATIONAL_LOG_RETENTION_EXPIRED: operational_candidate_total}
                if operational_candidate_total
                else {}
            ),
            decisions=_operational_decisions(
                result=result,
                mode=run_mode,
            ),
        )


@dataclass(frozen=True, slots=True)
class KnowledgeEvidenceRetentionExecutor:
    """Run durable evidence compaction through the shared executor contract."""

    db: Session
    name: str = "knowledge.evidence_retention"
    retention_class: str = RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE

    def run(
        self,
        *,
        policy: SupportsKnowledgeRetentionPolicy,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate and optionally compact tenant-scoped evidence archives."""

        run_mode = validate_run_mode(mode)
        effective_limit = min(
            _normalize_positive_int(limit, field_name="limit"),
            _normalize_positive_int(
                policy.retention_batch_size_per_tenant,
                field_name="policy.retention_batch_size_per_tenant",
            ),
        )
        result = KnowledgeRetentionService(
            self.db,
            tenant_id=tenant_id,
            operational_retention_days=_normalize_positive_int(
                policy.operational_log_retention_days,
                field_name="policy.operational_log_retention_days",
            ),
            operational_batch_limit=effective_limit,
            manage_transaction=False,
            include_operational_log_retention=False,
            include_durable_evidence_retention=True,
            include_artifact_object_retention=False,
        ).run(dry_run=run_mode == RETENTION_RUN_MODE_DRY_RUN)

        eligible_count = sum(
            1
            for item in result.evidence_decisions
            if item.action == "eligible_for_compaction"
        )
        preserved_count = len(result.evidence_decisions) - eligible_count
        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
            mode=run_mode,
            tenant_id=int(tenant_id),
            counts=RetentionBatchCounts(
                scanned_count=len(result.evidence_decisions),
                candidate_count=eligible_count,
                protected_count=sum(
                    1
                    for item in result.evidence_decisions
                    if item.action in {"preserve_active_finding", "preserve_replay_policy"}
                ),
                applied_count=(
                    result.evidence_compacted_count
                    if run_mode == RETENTION_RUN_MODE_APPLY
                    else 0
                ),
                preserved_count=preserved_count,
                batch_count=len(result.evidence_decisions),
                batch_limit=effective_limit,
            ),
            reason_counts=_evidence_reason_counts(result.evidence_decisions),
            decisions=_evidence_decisions(result=result, mode=run_mode),
        )


def _operational_decisions(
    *,
    result: object,
    mode: RetentionRunMode,
) -> tuple[RetentionDecision, ...]:
    outcome = (
        RETENTION_DECISION_APPLIED
        if mode == RETENTION_RUN_MODE_APPLY
        else RETENTION_DECISION_CANDIDATE
    )
    decisions: list[RetentionDecision] = []
    for item in result.operational_log_results:
        count = item.deleted_count if mode == RETENTION_RUN_MODE_APPLY else item.candidate_count
        if count <= 0:
            continue
        decisions.append(
            RetentionDecision(
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                outcome=outcome,
                reason_code=OPERATIONAL_LOG_RETENTION_EXPIRED,
                resource_id=item.name,
                count=count,
            )
        )
    return tuple(decisions)


def _evidence_reason_counts(evidence_decisions: tuple[object, ...]) -> dict[str, int]:
    reason_counts: dict[str, int] = {}
    for item in evidence_decisions:
        reason = str(item.reason)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return reason_counts


def _evidence_decisions(
    *,
    result: object,
    mode: RetentionRunMode,
) -> tuple[RetentionDecision, ...]:
    decisions: list[RetentionDecision] = []
    for item in result.evidence_decisions:
        if item.action == "eligible_for_compaction":
            outcome = (
                RETENTION_DECISION_APPLIED
                if mode == RETENTION_RUN_MODE_APPLY
                else RETENTION_DECISION_CANDIDATE
            )
        elif item.action in {"preserve_active_finding", "preserve_replay_policy"}:
            outcome = RETENTION_DECISION_PROTECTED
        else:
            outcome = RETENTION_DECISION_SKIPPED
        decisions.append(
            RetentionDecision(
                retention_class=RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
                outcome=outcome,
                reason_code=str(item.reason),
                resource_id=str(item.evidence_id),
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
    "KnowledgeEvidenceRetentionExecutor",
    "KnowledgeRetentionExecutor",
    "EVIDENCE_COMPACTION_ELIGIBLE_REASON",
    "OPERATIONAL_LOG_RETENTION_EXPIRED",
]
