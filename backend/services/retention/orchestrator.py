"""Tenant-scoped retention orchestration and executor registry.

This module coordinates effective policy resolution, bounded scheduling, and
transaction handling for module-owned retention executors. Domain-specific
candidate selection and mutation logic stays inside registered executors.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from time import monotonic
from typing import Any, Callable, Mapping, Protocol, Sequence

from sqlalchemy.orm import Session

from backend.models.tenant import Tenant
from backend.services.metrics.retention import (
    emit_retention_executor_metrics,
    emit_retention_run_metrics,
)
from backend.services.retention.audit import (
    RetentionAuditEmitter,
    RetentionAuditService,
)
from backend.services.retention.contracts import (
    RETENTION_CLASS_ARTIFACT_PAYLOAD,
    RETENTION_CLASS_EXECUTION_PROVENANCE,
    RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
    RETENTION_CLASS_REPORTING,
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_CLASS_RUNTIME_RESUME_STATE,
    RETENTION_CLASS_SEMANTIC_MEMORY,
    RETENTION_CLASS_TASK_RECORD,
    RETENTION_CLASS_TASK_TRANSCRIPT,
    RETENTION_CLASS_USAGE_ACCOUNTING,
    RETENTION_DECISION_FAILED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
    RETENTION_SCOPE_ALL_TENANTS,
    RETENTION_SCOPE_TENANT,
    RetentionBatchCounts,
    RetentionClass,
    RetentionDecision,
    RetentionExecutorResult,
    RetentionRunMode,
    RetentionRunRequest,
    RetentionRunResult,
    TenantId,
    validate_retention_class,
    validate_run_mode,
    validate_safe_identifier,
)
from backend.services.retention.policies import (
    EffectiveRetentionPolicy,
    resolve_effective_retention_policy_for_tenant,
)
from backend.services.retention.scheduling import (
    DEFAULT_EXECUTOR_ORDER,
    DEFAULT_MAX_TENANTS_PER_RUN,
    RetentionExecutorOrderEntry,
    ScheduledRetentionExecutor,
    build_tenant_execution_plan,
)


RETENTION_TRANSACTION_BOUNDARY_EXECUTOR = "executor"
RETENTION_TRANSACTION_BOUNDARY_TENANT = "tenant"
RETENTION_TRANSACTION_BOUNDARIES = (
    RETENTION_TRANSACTION_BOUNDARY_EXECUTOR,
    RETENTION_TRANSACTION_BOUNDARY_TENANT,
)
RETENTION_EXECUTOR_FAILURE_CODE = "retention_executor_failed"
EXISTING_RETENTION_EXECUTOR_NAMES = frozenset(
    {
        "artifact.retention",
        "artifact_provenance.retention",
        "knowledge.evidence_retention",
        "knowledge.retention",
        "reporting.retention",
    }
)
EXISTING_RETENTION_CLASSES = (
    RETENTION_CLASS_ARTIFACT_PAYLOAD,
    RETENTION_CLASS_EXECUTION_PROVENANCE,
    RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_CLASS_REPORTING,
)
TASK_LOCAL_RETENTION_EXECUTOR_NAMES = frozenset(
    {
        "checkpoint.retention",
        "chat.retention",
        "task.retention",
    }
)
TASK_LOCAL_RETENTION_CLASSES = (
    RETENTION_CLASS_RUNTIME_RESUME_STATE,
    RETENTION_CLASS_TASK_TRANSCRIPT,
    RETENTION_CLASS_TASK_RECORD,
)
TASK_LOCAL_RETENTION_EXECUTOR_ORDER = tuple(
    entry
    for entry in DEFAULT_EXECUTOR_ORDER
    if entry.executor_name in TASK_LOCAL_RETENTION_EXECUTOR_NAMES
)
REMAINING_RETENTION_EXECUTOR_NAMES = frozenset(
    {
        "runner_control.retention",
        "memory.retention",
        "usage.retention",
    }
)
REMAINING_RETENTION_CLASSES = (
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_CLASS_SEMANTIC_MEMORY,
    RETENTION_CLASS_USAGE_ACCOUNTING,
)
REMAINING_RETENTION_EXECUTOR_ORDER = tuple(
    entry
    for entry in DEFAULT_EXECUTOR_ORDER
    if entry.executor_name in REMAINING_RETENTION_EXECUTOR_NAMES
)
EXISTING_RETENTION_EXECUTOR_ORDER = tuple(
    entry
    for entry in DEFAULT_EXECUTOR_ORDER
    if entry.executor_name in EXISTING_RETENTION_EXECUTOR_NAMES
)

PolicyResolver = Callable[[Session, int], EffectiveRetentionPolicy]
TenantLoader = Callable[[Session], Sequence[TenantId]]


class RetentionOrchestratorExecutor(Protocol):
    """Runtime executor shape accepted by the orchestrator registry."""

    name: str
    retention_class: str

    def run(
        self,
        *,
        policy: object,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> object:
        """Run one bounded tenant-scoped executor pass."""


@dataclass(frozen=True, slots=True)
class NoOpRetentionExecutor:
    """Safe placeholder executor for retention modules not implemented yet."""

    name: str
    retention_class: RetentionClass

    def run(
        self,
        *,
        policy: object,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=self.retention_class,
            mode=mode,
            tenant_id=tenant_id,
            counts=RetentionBatchCounts(batch_limit=limit),
            reason_counts={},
            decisions=(),
        )


class RetentionOrchestrator:
    """Resolve tenant policy and run retention executors in scheduled order."""

    def __init__(
        self,
        db: Session,
        *,
        executors: Sequence[RetentionOrchestratorExecutor] | None = None,
        executor_order: Sequence[RetentionExecutorOrderEntry] = DEFAULT_EXECUTOR_ORDER,
        transaction_boundary: str = RETENTION_TRANSACTION_BOUNDARY_EXECUTOR,
        policy_resolver: PolicyResolver | None = None,
        tenant_loader: TenantLoader | None = None,
        max_tenants_per_run: int = DEFAULT_MAX_TENANTS_PER_RUN,
        audit_emitter: RetentionAuditEmitter | None = None,
    ) -> None:
        self._db = db
        self._executor_order = tuple(executor_order)
        registered_executors = (
            _build_default_retention_executors(
                db,
                executor_order=self._executor_order,
            )
            if executors is None
            else tuple(executors)
        )
        self._registry = _build_executor_registry(
            executor_order=self._executor_order,
            executors=registered_executors,
        )
        self._transaction_boundary = _validate_transaction_boundary(
            transaction_boundary
        )
        self._policy_resolver = policy_resolver or _resolve_default_policy
        self._tenant_loader = tenant_loader or _load_active_tenant_ids
        self._max_tenants_per_run = int(max_tenants_per_run)
        self._audit = RetentionAuditService(emitter=audit_emitter)

    def run(self, request: RetentionRunRequest) -> RetentionRunResult:
        """Run one bounded retention request and return safe executor results."""

        run_started_at = monotonic()
        tenant_ids = _resolve_tenant_ids(
            db=self._db,
            request=request,
            tenant_loader=self._tenant_loader,
        )
        _validate_tenant_batch_size(
            tenant_ids=tenant_ids,
            max_tenants_per_run=self._max_tenants_per_run,
        )
        policies = {
            tenant_id: self._policy_resolver(self._db, tenant_id)
            for tenant_id in tenant_ids
        }
        plan = build_tenant_execution_plan(
            request=request,
            tenant_ids=tenant_ids,
            policies=policies,
            executors=self._executor_order,
            max_tenants_per_run=self._max_tenants_per_run,
        )

        results: list[RetentionExecutorResult] = []
        if self._transaction_boundary == RETENTION_TRANSACTION_BOUNDARY_TENANT:
            results.extend(self._run_plan_per_tenant(plan=plan, policies=policies))
        else:
            results.extend(self._run_plan_per_executor(plan=plan, policies=policies))

        run_result = RetentionRunResult(
            mode=request.mode,
            scope=request.scope,
            tenant_id=request.tenant_id,
            results=tuple(results),
            succeeded=all(result.succeeded for result in results),
        )
        run_duration = monotonic() - run_started_at
        emit_retention_run_metrics(run_result, duration_seconds=run_duration)
        self._audit.emit_run_result(run_result, duration_seconds=run_duration)
        return run_result

    def _run_plan_per_executor(
        self,
        *,
        plan: Sequence[ScheduledRetentionExecutor],
        policies: Mapping[TenantId, EffectiveRetentionPolicy],
    ) -> tuple[RetentionExecutorResult, ...]:
        results: list[RetentionExecutorResult] = []
        for scheduled in plan:
            result = self._run_scheduled_executor(
                scheduled=scheduled,
                policy=policies[scheduled.tenant_id],
            )
            results.append(result)
            if scheduled.mode == RETENTION_RUN_MODE_DRY_RUN or not result.succeeded:
                self._db.rollback()
            else:
                self._db.commit()
        return tuple(results)

    def _run_plan_per_tenant(
        self,
        *,
        plan: Sequence[ScheduledRetentionExecutor],
        policies: Mapping[TenantId, EffectiveRetentionPolicy],
    ) -> tuple[RetentionExecutorResult, ...]:
        results: list[RetentionExecutorResult] = []
        for tenant_id in _ordered_tenant_ids(plan):
            tenant_failed = False
            tenant_plan = tuple(item for item in plan if item.tenant_id == tenant_id)
            for scheduled in tenant_plan:
                result = self._run_scheduled_executor(
                    scheduled=scheduled,
                    policy=policies[scheduled.tenant_id],
                )
                results.append(result)
                tenant_failed = tenant_failed or not result.succeeded
                if scheduled.mode == RETENTION_RUN_MODE_DRY_RUN:
                    self._db.rollback()
                elif not result.succeeded:
                    self._db.rollback()
            if tenant_plan and tenant_plan[0].mode == RETENTION_RUN_MODE_APPLY:
                if tenant_failed:
                    self._db.rollback()
                else:
                    self._db.commit()
        return tuple(results)

    def _run_scheduled_executor(
        self,
        *,
        scheduled: ScheduledRetentionExecutor,
        policy: EffectiveRetentionPolicy,
    ) -> RetentionExecutorResult:
        executor = self._registry[scheduled.executor_name]
        executor_started_at = monotonic()
        try:
            result = executor.run(
                policy=policy,
                tenant_id=scheduled.tenant_id,
                mode=scheduled.mode,
                limit=scheduled.limit,
            )
            executor_result = _coerce_executor_result(result, scheduled=scheduled)
        except Exception:
            executor_result = _build_failure_result(scheduled)
        executor_duration = monotonic() - executor_started_at
        emit_retention_executor_metrics(
            executor_result,
            duration_seconds=executor_duration,
        )
        self._audit.emit_executor_result(
            executor_result,
            duration_seconds=executor_duration,
        )
        return executor_result


def build_existing_retention_executors(
    db: Session,
) -> tuple[RetentionOrchestratorExecutor, ...]:
    """Return existing module-owned executors registered before new retention paths."""

    from backend.services.artifact.retention_service import (
        ArtifactProvenanceRetentionExecutor,
        ArtifactRetentionExecutor,
    )
    from backend.services.knowledge.retention_executor import (
        KnowledgeEvidenceRetentionExecutor,
        KnowledgeRetentionExecutor,
    )
    from backend.services.reporting.report_retention_service import (
        ReportRetentionExecutor,
    )

    return (
        ArtifactRetentionExecutor(db),
        ArtifactProvenanceRetentionExecutor(db),
        KnowledgeRetentionExecutor(db),
        KnowledgeEvidenceRetentionExecutor(db),
        ReportRetentionExecutor(db),
    )


def build_task_local_retention_executors(
    db: Session,
) -> tuple[RetentionOrchestratorExecutor, ...]:
    """Return task-local executors in dependency-safe cleanup order."""

    from backend.services.chat.retention_service import ChatTranscriptRetentionExecutor
    from backend.services.langgraph_chat.checkpoint.retention_service import (
        CheckpointRetentionExecutor,
    )
    from backend.services.task.retention_service import TaskRetentionExecutor

    return (
        CheckpointRetentionExecutor(db),
        ChatTranscriptRetentionExecutor(db),
        TaskRetentionExecutor(db),
    )


def build_remaining_retention_executors(
    db: Session,
) -> tuple[RetentionOrchestratorExecutor, ...]:
    """Return remaining implemented module-owned executors for MVP coverage."""

    from backend.services.memory.retention_service import MemoryRetentionExecutor
    from backend.services.runner_control.retention_service import (
        RunnerControlRetentionExecutor,
    )
    from backend.services.usage_tracking.retention_service import UsageRetentionExecutor

    return (
        RunnerControlRetentionExecutor(db),
        MemoryRetentionExecutor(db),
        UsageRetentionExecutor(db),
    )


def build_retention_executors(
    db: Session,
) -> tuple[RetentionOrchestratorExecutor, ...]:
    """Return all implemented module-owned executors for the default orchestrator."""

    executors = (
        *build_task_local_retention_executors(db),
        *build_existing_retention_executors(db),
        *build_remaining_retention_executors(db),
    )
    by_name = {executor.name: executor for executor in executors}
    return tuple(
        by_name[entry.executor_name]
        for entry in DEFAULT_EXECUTOR_ORDER
        if entry.executor_name in by_name
    )


def _build_default_retention_executors(
    db: Session,
    *,
    executor_order: Sequence[RetentionExecutorOrderEntry],
) -> tuple[RetentionOrchestratorExecutor, ...]:
    ordered_names = frozenset(entry.executor_name for entry in executor_order)
    if ordered_names <= EXISTING_RETENTION_EXECUTOR_NAMES:
        executors = build_existing_retention_executors(db)
    else:
        executors = build_retention_executors(db)
    return tuple(executor for executor in executors if executor.name in ordered_names)


def _build_executor_registry(
    *,
    executor_order: Sequence[RetentionExecutorOrderEntry],
    executors: Sequence[RetentionOrchestratorExecutor],
) -> dict[str, RetentionOrchestratorExecutor]:
    registry: dict[str, RetentionOrchestratorExecutor] = {
        entry.executor_name: NoOpRetentionExecutor(
            name=entry.executor_name,
            retention_class=entry.retention_class,
        )
        for entry in executor_order
    }
    for executor in executors:
        executor_name = validate_safe_identifier(str(executor.name))
        if executor_name not in registry:
            raise ValueError(f"executor is not in retention order: {executor_name}")
        registry[executor_name] = executor
    return registry


def _resolve_tenant_ids(
    *,
    db: Session,
    request: RetentionRunRequest,
    tenant_loader: TenantLoader,
) -> tuple[TenantId, ...]:
    if request.scope == RETENTION_SCOPE_TENANT:
        if request.tenant_id is None:
            raise ValueError("tenant_id is required for tenant-scoped retention")
        return (int(request.tenant_id),)
    if request.scope == RETENTION_SCOPE_ALL_TENANTS:
        return tuple(int(tenant_id) for tenant_id in tenant_loader(db))
    raise ValueError(f"unknown retention scope: {request.scope}")


def _load_active_tenant_ids(db: Session) -> tuple[TenantId, ...]:
    rows = (
        db.query(Tenant.id)
        .filter(Tenant.status == "active")
        .order_by(Tenant.id.asc())
        .all()
    )
    return tuple(_extract_tenant_id(row) for row in rows)


def _resolve_default_policy(db: Session, tenant_id: int) -> EffectiveRetentionPolicy:
    return resolve_effective_retention_policy_for_tenant(db, tenant_id=tenant_id)


def _validate_tenant_batch_size(
    *,
    tenant_ids: Sequence[TenantId],
    max_tenants_per_run: int,
) -> None:
    if max_tenants_per_run < 1:
        raise ValueError("max_tenants_per_run must be positive")
    if len(tenant_ids) > max_tenants_per_run:
        raise ValueError("tenant_ids exceeds max_tenants_per_run")


def _extract_tenant_id(row: object) -> TenantId:
    if isinstance(row, int):
        return row
    if isinstance(row, tuple):
        return int(row[0])
    row_id = getattr(row, "id", None)
    if row_id is not None:
        return int(row_id)
    return int(row[0])  # type: ignore[index]


def _ordered_tenant_ids(
    plan: Sequence[ScheduledRetentionExecutor],
) -> tuple[TenantId, ...]:
    seen: set[TenantId] = set()
    tenant_ids: list[TenantId] = []
    for item in plan:
        if item.tenant_id not in seen:
            seen.add(item.tenant_id)
            tenant_ids.append(item.tenant_id)
    return tuple(tenant_ids)


def _build_failure_result(
    scheduled: ScheduledRetentionExecutor,
) -> RetentionExecutorResult:
    return RetentionExecutorResult(
        executor_name=scheduled.executor_name,
        retention_class=scheduled.retention_class,
        mode=scheduled.mode,
        tenant_id=scheduled.tenant_id,
        counts=RetentionBatchCounts(
            failed_count=1,
            batch_limit=scheduled.limit,
        ),
        reason_counts={RETENTION_EXECUTOR_FAILURE_CODE: 1},
        decisions=(
            RetentionDecision(
                retention_class=scheduled.retention_class,
                outcome=RETENTION_DECISION_FAILED,
                reason_code=RETENTION_EXECUTOR_FAILURE_CODE,
                count=1,
            ),
        ),
        succeeded=False,
        error_code=RETENTION_EXECUTOR_FAILURE_CODE,
    )


def _coerce_executor_result(
    result: object,
    *,
    scheduled: ScheduledRetentionExecutor,
) -> RetentionExecutorResult:
    if isinstance(result, RetentionExecutorResult):
        return _normalize_executor_result(result, scheduled=scheduled)

    data = _object_to_mapping(result)
    decisions = tuple(
        _coerce_decision(item, scheduled=scheduled)
        for item in data.get("decisions", ())
    )
    return RetentionExecutorResult(
        executor_name=str(data.get("executor_name", scheduled.executor_name)),
        retention_class=_normalize_retention_class(
            str(data.get("retention_class", scheduled.retention_class)),
            scheduled=scheduled,
        ),
        mode=validate_run_mode(str(data.get("mode", scheduled.mode))),
        tenant_id=int(data.get("tenant_id", scheduled.tenant_id)),
        counts=_coerce_counts(data.get("counts"), scheduled=scheduled),
        reason_counts=_coerce_reason_counts(data.get("reason_counts", {})),
        decisions=decisions,
        succeeded=bool(data.get("succeeded", True)),
        error_code=data.get("error_code"),
    )


def _normalize_executor_result(
    result: RetentionExecutorResult,
    *,
    scheduled: ScheduledRetentionExecutor,
) -> RetentionExecutorResult:
    normalized_class = _normalize_retention_class(
        result.retention_class,
        scheduled=scheduled,
    )
    normalized_decisions = tuple(
        _coerce_decision(decision, scheduled=scheduled)
        for decision in result.decisions
    )
    if (
        normalized_class == result.retention_class
        and normalized_decisions == result.decisions
    ):
        return result
    return RetentionExecutorResult(
        executor_name=result.executor_name,
        retention_class=normalized_class,
        mode=result.mode,
        tenant_id=result.tenant_id,
        counts=result.counts,
        reason_counts=result.reason_counts,
        decisions=normalized_decisions,
        succeeded=result.succeeded,
        error_code=result.error_code,
    )


def _coerce_decision(
    value: object,
    *,
    scheduled: ScheduledRetentionExecutor,
) -> RetentionDecision:
    if isinstance(value, RetentionDecision):
        return RetentionDecision(
            retention_class=_normalize_retention_class(
                value.retention_class,
                scheduled=scheduled,
            ),
            outcome=value.outcome,
            reason_code=value.reason_code,
            resource_id=value.resource_id,
            count=value.count,
        )
    data = _object_to_mapping(value)
    return RetentionDecision(
        retention_class=_normalize_retention_class(
            str(data.get("retention_class", scheduled.retention_class)),
            scheduled=scheduled,
        ),
        outcome=str(data.get("outcome", RETENTION_DECISION_FAILED)),
        reason_code=str(data.get("reason_code", RETENTION_EXECUTOR_FAILURE_CODE)),
        resource_id=data.get("resource_id"),
        count=int(data.get("count", 1)),
    )


def _coerce_counts(
    value: object,
    *,
    scheduled: ScheduledRetentionExecutor,
) -> RetentionBatchCounts:
    if isinstance(value, RetentionBatchCounts):
        return value
    if value is None:
        return RetentionBatchCounts(batch_limit=scheduled.limit)
    data = _object_to_mapping(value)
    field_names = {field_info.name for field_info in fields(RetentionBatchCounts)}
    return RetentionBatchCounts(
        **{
            field_name: data[field_name]
            for field_name in field_names
            if field_name in data
        }
    )


def _coerce_reason_counts(value: object) -> Mapping[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("reason_counts must be a mapping")
    return {str(reason): int(count) for reason, count in value.items()}


def _object_to_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value):
        return {
            field_info.name: getattr(value, field_info.name)
            for field_info in fields(value)
        }
    if hasattr(value, "__dict__"):
        return vars(value)
    raise TypeError("executor result must be a RetentionExecutorResult or mapping")


def _normalize_retention_class(
    retention_class: str,
    *,
    scheduled: ScheduledRetentionExecutor,
) -> RetentionClass:
    if retention_class == "runtime_ephemeral":
        if scheduled.retention_class in (
            RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
            RETENTION_CLASS_ARTIFACT_PAYLOAD,
        ):
            return scheduled.retention_class
        return RETENTION_CLASS_OPERATIONAL_EPHEMERAL
    legacy_map = {
        "operational_logs": RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        "engagement_evidence": RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
        "engagement_truth": RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
    }
    return validate_retention_class(legacy_map.get(retention_class, retention_class))


def _validate_transaction_boundary(value: str) -> str:
    if value not in RETENTION_TRANSACTION_BOUNDARIES:
        raise ValueError(f"unknown retention transaction boundary: {value}")
    return value


__all__ = [
    "EXISTING_RETENTION_CLASSES",
    "EXISTING_RETENTION_EXECUTOR_NAMES",
    "EXISTING_RETENTION_EXECUTOR_ORDER",
    "TASK_LOCAL_RETENTION_CLASSES",
    "TASK_LOCAL_RETENTION_EXECUTOR_NAMES",
    "TASK_LOCAL_RETENTION_EXECUTOR_ORDER",
    "build_existing_retention_executors",
    "build_retention_executors",
    "build_task_local_retention_executors",
    "NoOpRetentionExecutor",
    "RETENTION_EXECUTOR_FAILURE_CODE",
    "RETENTION_TRANSACTION_BOUNDARY_EXECUTOR",
    "RETENTION_TRANSACTION_BOUNDARY_TENANT",
    "RetentionOrchestrator",
    "RetentionOrchestratorExecutor",
]
