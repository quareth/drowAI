"""Memory-owned retention executor for tenant semantic memory rows.

This module evaluates stale task-engagement semantic memories and deletes only
bounded rows whose engagement lifecycle no longer requires active protection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from sqlalchemy import exists, func
from sqlalchemy.orm import Session

from backend.core.time_utils import utc_now
from backend.models.core import Engagement, Task
from backend.models.semantic_memory import SemanticMemory
from backend.services.memory.memory_models import MemoryTier
from backend.services.retention.contracts import (
    RETENTION_CLASS_SEMANTIC_MEMORY,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_PROTECTED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
    RetentionBatchCounts,
    RetentionDecision,
    RetentionExecutorResult,
    RetentionRunMode,
    TenantId,
    validate_run_mode,
)


STALE_SEMANTIC_MEMORY_UNUSED = "stale_semantic_memory_unused"
ACTIVE_ENGAGEMENT_SEMANTIC_MEMORY_PROTECTED = (
    "active_engagement_semantic_memory_protected"
)

_ACTIVE_ENGAGEMENT_STATUSES = frozenset({"active"})


class SupportsMemoryRetentionPolicy(Protocol):
    """Policy fields consumed by the semantic-memory retention executor."""

    semantic_memory_stale_retention_days: int
    retention_batch_size_per_tenant: int


@dataclass(frozen=True, slots=True)
class MemoryRetentionExecutor:
    """Run bounded stale semantic-memory retention through the shared contract."""

    db: Session
    name: str = "memory.retention"
    retention_class: str = RETENTION_CLASS_SEMANTIC_MEMORY

    def run(
        self,
        *,
        policy: SupportsMemoryRetentionPolicy,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate and optionally delete tenant-scoped stale memory rows."""

        run_mode = validate_run_mode(mode)
        scoped_tenant_id = _normalize_positive_int(tenant_id, field_name="tenant_id")
        effective_limit = _effective_limit(policy=policy, limit=limit)
        cutoff = utc_now() - timedelta(
            days=_normalize_positive_int(
                policy.semantic_memory_stale_retention_days,
                field_name="policy.semantic_memory_stale_retention_days",
            )
        )

        candidates = _load_stale_task_engagement_candidates(
            self.db,
            tenant_id=scoped_tenant_id,
            older_than=cutoff,
            limit=effective_limit,
        )
        protected_memories = _load_active_engagement_protected_memories(
            self.db,
            tenant_id=scoped_tenant_id,
            older_than=cutoff,
            limit=effective_limit,
        )

        decisions: list[RetentionDecision] = [
            _memory_decision(
                memory_id=str(memory.id),
                outcome=RETENTION_DECISION_PROTECTED,
                reason_code=ACTIVE_ENGAGEMENT_SEMANTIC_MEMORY_PROTECTED,
            )
            for memory in protected_memories
        ]
        candidate_outcome = (
            RETENTION_DECISION_CANDIDATE
            if run_mode == RETENTION_RUN_MODE_DRY_RUN
            else RETENTION_DECISION_APPLIED
        )
        decisions.extend(
            _memory_decision(
                memory_id=str(memory.id),
                outcome=candidate_outcome,
                reason_code=STALE_SEMANTIC_MEMORY_UNUSED,
            )
            for memory in candidates
        )

        applied_count = 0
        if run_mode == RETENTION_RUN_MODE_APPLY:
            applied_count = _delete_memory_rows(
                self.db,
                tenant_id=scoped_tenant_id,
                ids=[memory.id for memory in candidates],
            )

        candidate_count = len(candidates)
        protected_count = len(protected_memories)
        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=RETENTION_CLASS_SEMANTIC_MEMORY,
            mode=run_mode,
            tenant_id=scoped_tenant_id,
            counts=RetentionBatchCounts(
                scanned_count=candidate_count + protected_count,
                candidate_count=candidate_count,
                protected_count=protected_count,
                applied_count=applied_count,
                batch_count=candidate_count,
                batch_limit=effective_limit,
            ),
            reason_counts=_reason_counts(decisions),
            decisions=tuple(decisions),
        )


def _load_stale_task_engagement_candidates(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
    limit: int,
) -> list[SemanticMemory]:
    touched_at = _memory_touched_at()
    return (
        db.query(SemanticMemory)
        .filter(
            SemanticMemory.tenant_id == tenant_id,
            SemanticMemory.memory_tier == MemoryTier.TASK_ENGAGEMENT.value,
            touched_at < older_than,
            ~_has_active_direct_engagement(tenant_id=tenant_id),
            ~_has_active_task_engagement(tenant_id=tenant_id),
        )
        .order_by(touched_at.asc(), SemanticMemory.id.asc())
        .limit(limit)
        .all()
    )


def _load_active_engagement_protected_memories(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
    limit: int,
) -> list[SemanticMemory]:
    touched_at = _memory_touched_at()
    return (
        db.query(SemanticMemory)
        .filter(
            SemanticMemory.tenant_id == tenant_id,
            SemanticMemory.memory_tier == MemoryTier.TASK_ENGAGEMENT.value,
            touched_at < older_than,
            (
                _has_active_direct_engagement(tenant_id=tenant_id)
                | _has_active_task_engagement(tenant_id=tenant_id)
            ),
        )
        .order_by(touched_at.asc(), SemanticMemory.id.asc())
        .limit(limit)
        .all()
    )


def _memory_touched_at() -> object:
    return func.coalesce(
        SemanticMemory.last_accessed_at,
        SemanticMemory.updated_at,
        SemanticMemory.created_at,
    )


def _has_active_direct_engagement(*, tenant_id: int) -> object:
    return exists().where(
        Engagement.id == SemanticMemory.engagement_id,
        Engagement.tenant_id == tenant_id,
        Engagement.status.in_(tuple(sorted(_ACTIVE_ENGAGEMENT_STATUSES))),
    )


def _has_active_task_engagement(*, tenant_id: int) -> object:
    return exists().where(
        Task.id == SemanticMemory.task_id,
        Task.tenant_id == tenant_id,
        Engagement.id == Task.engagement_id,
        Engagement.tenant_id == tenant_id,
        Engagement.status.in_(tuple(sorted(_ACTIVE_ENGAGEMENT_STATUSES))),
    )


def _delete_memory_rows(
    db: Session,
    *,
    tenant_id: int,
    ids: list[object],
) -> int:
    if not ids:
        return 0
    return int(
        db.query(SemanticMemory)
        .filter(
            SemanticMemory.tenant_id == tenant_id,
            SemanticMemory.memory_tier == MemoryTier.TASK_ENGAGEMENT.value,
            SemanticMemory.id.in_(ids),
        )
        .delete(synchronize_session=False)
    )


def _memory_decision(
    *,
    memory_id: str,
    outcome: str,
    reason_code: str,
) -> RetentionDecision:
    return RetentionDecision(
        retention_class=RETENTION_CLASS_SEMANTIC_MEMORY,
        outcome=outcome,
        reason_code=reason_code,
        resource_id=f"semantic_memory:{memory_id}",
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
    policy: SupportsMemoryRetentionPolicy,
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
    "ACTIVE_ENGAGEMENT_SEMANTIC_MEMORY_PROTECTED",
    "MemoryRetentionExecutor",
    "STALE_SEMANTIC_MEMORY_UNUSED",
    "SupportsMemoryRetentionPolicy",
]
