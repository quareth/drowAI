"""Bounded retention scheduling helpers and executor ordering.

This module owns deterministic executor ordering and per-tenant run limits for
the central retention orchestrator. It only builds finite execution plans; the
module-owned executors still own candidate selection and mutations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from backend.services.retention.contracts import (
    RETENTION_CLASSES,
    RETENTION_CLASS_ARTIFACT_PAYLOAD,
    RETENTION_CLASS_EXECUTION_PROVENANCE,
    RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_CLASS_REPORTING,
    RETENTION_CLASS_RUNTIME_RESUME_STATE,
    RETENTION_CLASS_SEMANTIC_MEMORY,
    RETENTION_CLASS_TASK_RECORD,
    RETENTION_CLASS_TASK_TRANSCRIPT,
    RETENTION_CLASS_USAGE_ACCOUNTING,
    RETENTION_SCOPE_ALL_TENANTS,
    RETENTION_SCOPE_TENANT,
    RetentionClass,
    RetentionRunRequest,
    TenantId,
    validate_retention_class,
    validate_run_mode,
    validate_safe_identifier,
    validate_scope,
)


DEFAULT_MAX_TENANTS_PER_RUN = 1000


class SupportsRetentionBatchLimit(Protocol):
    """Policy shape required by scheduling helpers."""

    retention_batch_size_per_tenant: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RetentionExecutorOrderEntry:
    """Documented position of one retention executor in the run order."""

    order: int
    executor_name: str
    retention_class: RetentionClass
    dependency_note: str

    def __post_init__(self) -> None:
        if self.order < 1:
            raise ValueError("executor order must be positive")
        validate_safe_identifier(self.executor_name)
        validate_retention_class(self.retention_class)
        if not self.dependency_note.strip():
            raise ValueError("executor dependency_note is required")


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduledRetentionExecutor:
    """One bounded executor invocation for one tenant."""

    tenant_id: TenantId
    executor_name: str
    retention_class: RetentionClass
    mode: str
    limit: int
    order: int

    def __post_init__(self) -> None:
        if self.tenant_id < 1:
            raise ValueError("tenant_id must be positive")
        validate_safe_identifier(self.executor_name)
        validate_retention_class(self.retention_class)
        validate_run_mode(self.mode)
        if self.limit < 1:
            raise ValueError("scheduled executor limit must be positive")
        if self.order < 1:
            raise ValueError("scheduled executor order must be positive")


# Dependency-sensitive default order:
# 1. Runtime/control state is evaluated before task rows so resumable work wins.
# 2. Chat transcripts are evaluated before task rows because they depend on task
#    lifecycle but should not outlive eligible terminal task cleanup.
# 3. Task rows run after dependent task-local state checks.
# 4. Artifact payload and provenance executors stay adjacent so object cleanup
#    and metadata cleanup remain module-owned but dependency-aware.
# 5. Knowledge, reporting, memory, and usage executors follow with their
#    module-owned protection rules and no cross-module destructive SQL.
DEFAULT_EXECUTOR_ORDER: tuple[RetentionExecutorOrderEntry, ...] = (
    RetentionExecutorOrderEntry(
        order=10,
        executor_name="runner_control.retention",
        retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        dependency_note="Runtime control records are task-local operational state.",
    ),
    RetentionExecutorOrderEntry(
        order=20,
        executor_name="checkpoint.retention",
        retention_class=RETENTION_CLASS_RUNTIME_RESUME_STATE,
        dependency_note="Resume state must be protected before task row cleanup.",
    ),
    RetentionExecutorOrderEntry(
        order=30,
        executor_name="chat.retention",
        retention_class=RETENTION_CLASS_TASK_TRANSCRIPT,
        dependency_note="Task transcripts are evaluated before task row cleanup.",
    ),
    RetentionExecutorOrderEntry(
        order=40,
        executor_name="task.retention",
        retention_class=RETENTION_CLASS_TASK_RECORD,
        dependency_note="Task rows run after dependent task-local state checks.",
    ),
    RetentionExecutorOrderEntry(
        order=50,
        executor_name="artifact.retention",
        retention_class=RETENTION_CLASS_ARTIFACT_PAYLOAD,
        dependency_note="Artifact executors own object and artifact-row protection.",
    ),
    RetentionExecutorOrderEntry(
        order=60,
        executor_name="artifact_provenance.retention",
        retention_class=RETENTION_CLASS_EXECUTION_PROVENANCE,
        dependency_note="Artifact provenance is evaluated beside artifact payloads.",
    ),
    RetentionExecutorOrderEntry(
        order=70,
        executor_name="knowledge.retention",
        retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        dependency_note="Migrated operational log cleanup remains knowledge-owned.",
    ),
    RetentionExecutorOrderEntry(
        order=75,
        executor_name="knowledge.evidence_retention",
        retention_class=RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
        dependency_note="Durable evidence compaction remains knowledge-owned.",
    ),
    RetentionExecutorOrderEntry(
        order=80,
        executor_name="reporting.retention",
        retention_class=RETENTION_CLASS_REPORTING,
        dependency_note="Reporting owns current-report and report-job protection.",
    ),
    RetentionExecutorOrderEntry(
        order=90,
        executor_name="memory.retention",
        retention_class=RETENTION_CLASS_SEMANTIC_MEMORY,
        dependency_note="Semantic memory pruning is independent and module-owned.",
    ),
    RetentionExecutorOrderEntry(
        order=100,
        executor_name="usage.retention",
        retention_class=RETENTION_CLASS_USAGE_ACCOUNTING,
        dependency_note="Usage cleanup runs after operational retention decisions.",
    ),
)


def resolve_per_tenant_limit(
    *,
    policy: SupportsRetentionBatchLimit,
    request_limit: int | None = None,
) -> int:
    """Return the effective bounded per-tenant executor limit."""

    policy_limit = _normalize_positive_int(
        policy.retention_batch_size_per_tenant,
        field_name="policy.retention_batch_size_per_tenant",
    )
    if request_limit is None:
        return policy_limit
    return min(
        policy_limit,
        _normalize_positive_int(request_limit, field_name="request.limit_per_tenant"),
    )


def ordered_executor_entries(
    *,
    retention_classes: Sequence[RetentionClass] = RETENTION_CLASSES,
    executors: Sequence[RetentionExecutorOrderEntry] = DEFAULT_EXECUTOR_ORDER,
) -> tuple[RetentionExecutorOrderEntry, ...]:
    """Return deterministic executor order filtered to requested classes."""

    requested_classes = frozenset(
        validate_retention_class(retention_class)
        for retention_class in retention_classes
    )
    _validate_executor_order(executors)
    return tuple(
        entry
        for entry in sorted(executors, key=lambda item: item.order)
        if entry.retention_class in requested_classes
    )


def build_tenant_execution_plan(
    *,
    request: RetentionRunRequest,
    tenant_ids: Sequence[TenantId],
    policies: Mapping[TenantId, SupportsRetentionBatchLimit],
    executors: Sequence[RetentionExecutorOrderEntry] = DEFAULT_EXECUTOR_ORDER,
    max_tenants_per_run: int = DEFAULT_MAX_TENANTS_PER_RUN,
) -> tuple[ScheduledRetentionExecutor, ...]:
    """Build a finite tenant/executor plan with per-tenant limits applied."""

    validate_scope(request.scope)
    max_tenants = _normalize_positive_int(
        max_tenants_per_run,
        field_name="max_tenants_per_run",
    )
    selected_tenants = _select_bounded_tenant_ids(
        request=request,
        tenant_ids=tenant_ids,
        max_tenants_per_run=max_tenants,
    )
    ordered_entries = ordered_executor_entries(
        retention_classes=request.retention_classes,
        executors=executors,
    )

    scheduled: list[ScheduledRetentionExecutor] = []
    for tenant_id in selected_tenants:
        policy = policies.get(tenant_id)
        if policy is None:
            raise ValueError(f"missing retention policy for tenant_id: {tenant_id}")
        limit = resolve_per_tenant_limit(
            policy=policy,
            request_limit=request.limit_per_tenant,
        )
        for entry in ordered_entries:
            scheduled.append(
                ScheduledRetentionExecutor(
                    tenant_id=tenant_id,
                    executor_name=entry.executor_name,
                    retention_class=entry.retention_class,
                    mode=request.mode,
                    limit=limit,
                    order=entry.order,
                )
            )
    return tuple(scheduled)


def _select_bounded_tenant_ids(
    *,
    request: RetentionRunRequest,
    tenant_ids: Sequence[TenantId],
    max_tenants_per_run: int,
) -> tuple[TenantId, ...]:
    if len(tenant_ids) > max_tenants_per_run:
        raise ValueError("tenant_ids exceeds max_tenants_per_run")
    normalized_tenant_ids = tuple(
        _normalize_positive_int(tenant_id, field_name="tenant_id")
        for tenant_id in tenant_ids
    )
    if request.scope == RETENTION_SCOPE_TENANT:
        tenant_id = request.tenant_id
        if tenant_id is None:
            raise ValueError("tenant_id is required for tenant-scoped retention")
        normalized_requested_id = _normalize_positive_int(
            tenant_id,
            field_name="request.tenant_id",
        )
        if normalized_tenant_ids and normalized_tenant_ids != (normalized_requested_id,):
            raise ValueError("tenant_ids must match tenant-scoped request")
        return (normalized_requested_id,)
    if request.scope == RETENTION_SCOPE_ALL_TENANTS:
        return normalized_tenant_ids
    raise ValueError(f"unknown retention scope: {request.scope}")


def _validate_executor_order(
    executors: Sequence[RetentionExecutorOrderEntry],
) -> None:
    seen_orders: set[int] = set()
    seen_names: set[str] = set()
    for entry in executors:
        if entry.order in seen_orders:
            raise ValueError(f"duplicate executor order: {entry.order}")
        if entry.executor_name in seen_names:
            raise ValueError(f"duplicate executor name: {entry.executor_name}")
        seen_orders.add(entry.order)
        seen_names.add(entry.executor_name)


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
    "DEFAULT_EXECUTOR_ORDER",
    "DEFAULT_MAX_TENANTS_PER_RUN",
    "RetentionExecutorOrderEntry",
    "ScheduledRetentionExecutor",
    "SupportsRetentionBatchLimit",
    "build_tenant_execution_plan",
    "ordered_executor_entries",
    "resolve_per_tenant_limit",
]
