"""Usage-tracking-owned retention executor for LLM usage accounting rows.

This module evaluates tenant-scoped usage records whose request metadata has
aged past policy and scrubs that metadata while preserving token/count fields
needed for accounting totals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from sqlalchemy import JSON
from sqlalchemy.orm import Session

from backend.core.time_utils import utc_now
from backend.models.llm import LLMUsageRecord
from backend.services.retention.contracts import (
    RETENTION_CLASS_USAGE_ACCOUNTING,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
    RetentionBatchCounts,
    RetentionDecision,
    RetentionExecutorResult,
    RetentionRunMode,
    TenantId,
    validate_run_mode,
)


USAGE_RECORD_METADATA_RETENTION_EXPIRED = "usage_record_metadata_retention_expired"


class SupportsUsageRetentionPolicy(Protocol):
    """Policy fields consumed by the usage-accounting retention executor."""

    usage_record_retention_days: int
    retention_batch_size_per_tenant: int


@dataclass(frozen=True, slots=True)
class UsageRetentionExecutor:
    """Run bounded usage-record retention through the shared contract."""

    db: Session
    name: str = "usage.retention"
    retention_class: str = RETENTION_CLASS_USAGE_ACCOUNTING

    def run(
        self,
        *,
        policy: SupportsUsageRetentionPolicy,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate and optionally scrub tenant-scoped usage metadata."""

        run_mode = validate_run_mode(mode)
        scoped_tenant_id = _normalize_positive_int(tenant_id, field_name="tenant_id")
        effective_limit = _effective_limit(policy=policy, limit=limit)
        cutoff = utc_now() - timedelta(
            days=_normalize_positive_int(
                policy.usage_record_retention_days,
                field_name="policy.usage_record_retention_days",
            )
        )

        candidates = _load_usage_metadata_candidates(
            self.db,
            tenant_id=scoped_tenant_id,
            older_than=cutoff,
            limit=effective_limit,
        )
        candidate_outcome = (
            RETENTION_DECISION_CANDIDATE
            if run_mode == RETENTION_RUN_MODE_DRY_RUN
            else RETENTION_DECISION_APPLIED
        )
        decisions = [
            _usage_decision(
                usage_record_id=str(record.id),
                outcome=candidate_outcome,
                reason_code=USAGE_RECORD_METADATA_RETENTION_EXPIRED,
            )
            for record in candidates
        ]

        applied_count = 0
        if run_mode == RETENTION_RUN_MODE_APPLY:
            applied_count = _scrub_usage_request_metadata(
                self.db,
                tenant_id=scoped_tenant_id,
                records=candidates,
            )

        candidate_count = len(candidates)
        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=RETENTION_CLASS_USAGE_ACCOUNTING,
            mode=run_mode,
            tenant_id=scoped_tenant_id,
            counts=RetentionBatchCounts(
                scanned_count=candidate_count,
                candidate_count=candidate_count,
                applied_count=applied_count,
                batch_count=candidate_count,
                batch_limit=effective_limit,
            ),
            reason_counts=_reason_counts(decisions),
            decisions=tuple(decisions),
        )


def _load_usage_metadata_candidates(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
    limit: int,
) -> list[LLMUsageRecord]:
    return (
        db.query(LLMUsageRecord)
        .filter(
            LLMUsageRecord.tenant_id == tenant_id,
            LLMUsageRecord.created_at < older_than,
            LLMUsageRecord.request_metadata.is_not(None),
            LLMUsageRecord.request_metadata != JSON.NULL,
        )
        .order_by(LLMUsageRecord.created_at.asc(), LLMUsageRecord.id.asc())
        .limit(limit)
        .all()
    )


def _scrub_usage_request_metadata(
    db: Session,
    *,
    tenant_id: int,
    records: list[LLMUsageRecord],
) -> int:
    applied_count = 0
    for record in records:
        if int(record.tenant_id) != tenant_id or record.request_metadata is None:
            continue
        record.request_metadata = None
        applied_count += 1
    if applied_count:
        db.flush()
    return applied_count


def _usage_decision(
    *,
    usage_record_id: str,
    outcome: str,
    reason_code: str,
) -> RetentionDecision:
    return RetentionDecision(
        retention_class=RETENTION_CLASS_USAGE_ACCOUNTING,
        outcome=outcome,
        reason_code=reason_code,
        resource_id=f"llm_usage_record:{usage_record_id}",
    )


def _reason_counts(decisions: list[RetentionDecision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.reason_code] = counts.get(decision.reason_code, 0) + int(
            decision.count
        )
    return counts


def _effective_limit(
    *,
    policy: SupportsUsageRetentionPolicy,
    limit: int,
) -> int:
    policy_limit = _normalize_positive_int(
        policy.retention_batch_size_per_tenant,
        field_name="policy.retention_batch_size_per_tenant",
    )
    request_limit = _normalize_positive_int(limit, field_name="limit")
    return min(policy_limit, request_limit)


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
    "USAGE_RECORD_METADATA_RETENTION_EXPIRED",
    "SupportsUsageRetentionPolicy",
    "UsageRetentionExecutor",
]
