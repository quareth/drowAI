"""Runner-control-owned retention executor for operational control-plane rows.

This module evaluates tenant-scoped runner-control retention candidates and
deletes only bounded terminal runtime jobs, terminal control messages, and
stale inactive runner connection rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.core.time_utils import utc_now
from backend.models.runner_control import (
    Runner,
    RunnerConnection,
    RunnerControlMessage,
    RunnerCredential,
    RunnerInstallToken,
    RuntimeJob,
)
from backend.services.retention.contracts import (
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
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


RUNTIME_JOB_RETENTION_EXPIRED = "runner_runtime_job_retention_expired"
CONTROL_MESSAGE_RETENTION_EXPIRED = "runner_control_message_retention_expired"
RUNNER_CONNECTION_RETENTION_EXPIRED = "runner_connection_retention_expired"
ACTIVE_RUNTIME_JOB_RETENTION_PROTECTED = "active_runtime_job_retention_protected"
ACTIVE_CONTROL_MESSAGE_RETENTION_PROTECTED = (
    "active_control_message_retention_protected"
)
ACTIVE_RUNNER_IDENTITY_RETENTION_PROTECTED = (
    "active_runner_identity_retention_protected"
)
ACTIVE_RUNNER_CREDENTIAL_RETENTION_PROTECTED = (
    "active_runner_credential_retention_protected"
)
UNEXPIRED_INSTALL_TOKEN_RETENTION_PROTECTED = (
    "unexpired_install_token_retention_protected"
)

_RUNTIME_JOB_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "lost", "expired"}
)
_RUNTIME_JOB_ACTIVE_STATUSES = frozenset(
    {
        "queued",
        "assigned",
        "dispatching",
        "dispatched",
        "acknowledged",
        "accepted",
        "running",
    }
)
_CONTROL_MESSAGE_TERMINAL_STATUSES = frozenset(
    {"acked", "failed", "accepted", "rejected"}
)
_CONTROL_MESSAGE_PROTECTED_STATUSES = frozenset(
    {"queued", "pending", "retry", "dispatching", "delivered"}
)
_RUNNER_ACTIVE_STATUSES = frozenset({"active", "registered"})
_INSTALL_TOKEN_INACTIVE_STATUSES = frozenset({"revoked", "used", "disabled"})


class SupportsRunnerControlRetentionPolicy(Protocol):
    """Policy fields consumed by the runner-control retention executor."""

    runner_control_retention_days: int
    retention_batch_size_per_tenant: int


@dataclass(frozen=True, slots=True)
class RunnerControlRetentionExecutor:
    """Run bounded runner-control retention through the shared contract."""

    db: Session
    name: str = "runner_control.retention"
    retention_class: str = RETENTION_CLASS_OPERATIONAL_EPHEMERAL

    def run(
        self,
        *,
        policy: SupportsRunnerControlRetentionPolicy,
        tenant_id: TenantId,
        mode: RetentionRunMode,
        limit: int,
    ) -> RetentionExecutorResult:
        """Evaluate and optionally delete tenant-scoped runner-control rows."""

        run_mode = validate_run_mode(mode)
        scoped_tenant_id = _normalize_positive_int(tenant_id, field_name="tenant_id")
        effective_limit = _effective_limit(policy=policy, limit=limit)
        now = utc_now()
        cutoff = now - timedelta(
            days=_normalize_positive_int(
                policy.runner_control_retention_days,
                field_name="policy.runner_control_retention_days",
            )
        )

        runtime_jobs = _load_runtime_job_candidates(
            self.db,
            tenant_id=scoped_tenant_id,
            older_than=cutoff,
            limit=effective_limit,
        )
        remaining_limit = max(0, effective_limit - len(runtime_jobs))
        control_messages = (
            _load_control_message_candidates(
                self.db,
                tenant_id=scoped_tenant_id,
                older_than=cutoff,
                limit=remaining_limit,
            )
            if remaining_limit
            else []
        )
        remaining_limit = max(0, remaining_limit - len(control_messages))
        connections = (
            _load_connection_candidates(
                self.db,
                tenant_id=scoped_tenant_id,
                older_than=cutoff,
                limit=remaining_limit,
            )
            if remaining_limit
            else []
        )

        protected_decisions = _load_protected_decisions(
            self.db,
            tenant_id=scoped_tenant_id,
            older_than=cutoff,
            now=now,
            limit=effective_limit,
        )
        decisions: list[RetentionDecision] = list(protected_decisions)
        candidate_decisions = [
            _decision(
                resource_kind="runtime_job",
                resource_id=str(job.id),
                outcome=(
                    RETENTION_DECISION_CANDIDATE
                    if run_mode == RETENTION_RUN_MODE_DRY_RUN
                    else RETENTION_DECISION_APPLIED
                ),
                reason_code=RUNTIME_JOB_RETENTION_EXPIRED,
            )
            for job in runtime_jobs
        ]
        candidate_decisions.extend(
            _decision(
                resource_kind="control_message",
                resource_id=str(message.id),
                outcome=(
                    RETENTION_DECISION_CANDIDATE
                    if run_mode == RETENTION_RUN_MODE_DRY_RUN
                    else RETENTION_DECISION_APPLIED
                ),
                reason_code=CONTROL_MESSAGE_RETENTION_EXPIRED,
            )
            for message in control_messages
        )
        candidate_decisions.extend(
            _decision(
                resource_kind="runner_connection",
                resource_id=str(connection.id),
                outcome=(
                    RETENTION_DECISION_CANDIDATE
                    if run_mode == RETENTION_RUN_MODE_DRY_RUN
                    else RETENTION_DECISION_APPLIED
                ),
                reason_code=RUNNER_CONNECTION_RETENTION_EXPIRED,
            )
            for connection in connections
        )
        decisions.extend(candidate_decisions)

        applied_count = 0
        if run_mode == RETENTION_RUN_MODE_APPLY:
            applied_count += _delete_by_ids(
                self.db,
                RunnerControlMessage,
                tenant_id=scoped_tenant_id,
                ids=[message.id for message in control_messages],
            )
            applied_count += _delete_by_ids(
                self.db,
                RuntimeJob,
                tenant_id=scoped_tenant_id,
                ids=[job.id for job in runtime_jobs],
            )
            applied_count += _delete_by_ids(
                self.db,
                RunnerConnection,
                tenant_id=scoped_tenant_id,
                ids=[connection.id for connection in connections],
            )

        candidate_count = len(runtime_jobs) + len(control_messages) + len(connections)
        protected_count = sum(int(decision.count) for decision in protected_decisions)
        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
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


def _load_runtime_job_candidates(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
    limit: int,
) -> list[RuntimeJob]:
    touched_at = func.coalesce(RuntimeJob.updated_at, RuntimeJob.created_at)
    return (
        db.query(RuntimeJob)
        .filter(
            RuntimeJob.tenant_id == tenant_id,
            RuntimeJob.status.in_(tuple(sorted(_RUNTIME_JOB_TERMINAL_STATUSES))),
            touched_at < older_than,
        )
        .order_by(touched_at.asc(), RuntimeJob.id.asc())
        .limit(limit)
        .all()
    )


def _load_control_message_candidates(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
    limit: int,
) -> list[RunnerControlMessage]:
    touched_at = func.coalesce(
        RunnerControlMessage.updated_at,
        RunnerControlMessage.created_at,
    )
    return (
        db.query(RunnerControlMessage)
        .filter(
            RunnerControlMessage.tenant_id == tenant_id,
            RunnerControlMessage.status.in_(
                tuple(sorted(_CONTROL_MESSAGE_TERMINAL_STATUSES))
            ),
            touched_at < older_than,
        )
        .order_by(touched_at.asc(), RunnerControlMessage.id.asc())
        .limit(limit)
        .all()
    )


def _load_connection_candidates(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
    limit: int,
) -> list[RunnerConnection]:
    stale_at = func.coalesce(
        RunnerConnection.last_seen_at,
        RunnerConnection.updated_at,
        RunnerConnection.created_at,
    )
    return (
        db.query(RunnerConnection)
        .filter(
            RunnerConnection.tenant_id == tenant_id,
            RunnerConnection.status != "active",
            stale_at < older_than,
        )
        .order_by(stale_at.asc(), RunnerConnection.id.asc())
        .limit(limit)
        .all()
    )


def _load_protected_decisions(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
    now: object,
    limit: int,
) -> tuple[RetentionDecision, ...]:
    decisions: list[RetentionDecision] = []
    for runtime_job in _load_protected_runtime_jobs(
        db,
        tenant_id=tenant_id,
        older_than=older_than,
        limit=limit,
    ):
        decisions.append(
            _decision(
                resource_kind="runtime_job",
                resource_id=str(runtime_job.id),
                outcome=RETENTION_DECISION_PROTECTED,
                reason_code=ACTIVE_RUNTIME_JOB_RETENTION_PROTECTED,
            )
        )
    for message in _load_protected_control_messages(
        db,
        tenant_id=tenant_id,
        older_than=older_than,
        limit=limit,
    ):
        decisions.append(
            _decision(
                resource_kind="control_message",
                resource_id=str(message.id),
                outcome=RETENTION_DECISION_PROTECTED,
                reason_code=ACTIVE_CONTROL_MESSAGE_RETENTION_PROTECTED,
            )
        )
    aggregate_protected_counts = (
        (
            ACTIVE_RUNNER_IDENTITY_RETENTION_PROTECTED,
            _count_protected_active_runners(
                db,
                tenant_id=tenant_id,
                older_than=older_than,
            ),
        ),
        (
            ACTIVE_RUNNER_CREDENTIAL_RETENTION_PROTECTED,
            _count_protected_active_credentials(
                db,
                tenant_id=tenant_id,
                older_than=older_than,
                now=now,
            ),
        ),
        (
            UNEXPIRED_INSTALL_TOKEN_RETENTION_PROTECTED,
            _count_protected_unexpired_install_tokens(
                db,
                tenant_id=tenant_id,
                older_than=older_than,
                now=now,
            ),
        ),
    )
    for reason_code, count in aggregate_protected_counts:
        if count <= 0:
            continue
        decisions.append(
            RetentionDecision(
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                outcome=RETENTION_DECISION_PROTECTED,
                reason_code=reason_code,
                count=count,
            )
        )
    return tuple(decisions)


def _load_protected_runtime_jobs(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
    limit: int,
) -> list[RuntimeJob]:
    touched_at = func.coalesce(RuntimeJob.updated_at, RuntimeJob.created_at)
    return (
        db.query(RuntimeJob)
        .filter(
            RuntimeJob.tenant_id == tenant_id,
            RuntimeJob.status.in_(tuple(sorted(_RUNTIME_JOB_ACTIVE_STATUSES))),
            touched_at < older_than,
        )
        .order_by(touched_at.asc(), RuntimeJob.id.asc())
        .limit(limit)
        .all()
    )


def _load_protected_control_messages(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
    limit: int,
) -> list[RunnerControlMessage]:
    touched_at = func.coalesce(
        RunnerControlMessage.updated_at,
        RunnerControlMessage.created_at,
    )
    return (
        db.query(RunnerControlMessage)
        .filter(
            RunnerControlMessage.tenant_id == tenant_id,
            RunnerControlMessage.status.in_(
                tuple(sorted(_CONTROL_MESSAGE_PROTECTED_STATUSES))
            ),
            touched_at < older_than,
        )
        .order_by(touched_at.asc(), RunnerControlMessage.id.asc())
        .limit(limit)
        .all()
    )


def _count_protected_active_runners(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
) -> int:
    touched_at = func.coalesce(Runner.last_seen_at, Runner.updated_at, Runner.created_at)
    return int(
        db.query(Runner.id)
        .filter(
            Runner.tenant_id == tenant_id,
            Runner.status.in_(tuple(sorted(_RUNNER_ACTIVE_STATUSES))),
            touched_at < older_than,
        )
        .count()
    )


def _count_protected_active_credentials(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
    now: object,
) -> int:
    return int(
        db.query(RunnerCredential.id)
        .filter(
            RunnerCredential.tenant_id == tenant_id,
            RunnerCredential.status == "active",
            RunnerCredential.revoked_at.is_(None),
            RunnerCredential.created_at < older_than,
            (
                (RunnerCredential.expires_at.is_(None))
                | (RunnerCredential.expires_at > now)
            ),
        )
        .count()
    )


def _count_protected_unexpired_install_tokens(
    db: Session,
    *,
    tenant_id: int,
    older_than: object,
    now: object,
) -> int:
    return int(
        db.query(RunnerInstallToken.id)
        .filter(
            RunnerInstallToken.tenant_id == tenant_id,
            ~RunnerInstallToken.status.in_(tuple(sorted(_INSTALL_TOKEN_INACTIVE_STATUSES))),
            RunnerInstallToken.used_at.is_(None),
            RunnerInstallToken.created_at < older_than,
            RunnerInstallToken.expires_at > now,
        )
        .count()
    )


def _delete_by_ids(
    db: Session,
    model: type[RuntimeJob] | type[RunnerControlMessage] | type[RunnerConnection],
    *,
    tenant_id: int,
    ids: list[object],
) -> int:
    if not ids:
        return 0
    return int(
        db.query(model)
        .filter(
            model.tenant_id == tenant_id,
            model.id.in_(ids),
        )
        .delete(synchronize_session=False)
    )


def _decision(
    *,
    resource_kind: str,
    resource_id: str,
    outcome: str,
    reason_code: str,
) -> RetentionDecision:
    return RetentionDecision(
        retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        outcome=outcome,
        reason_code=reason_code,
        resource_id=f"{resource_kind}:{resource_id}",
    )


def _reason_counts(decisions: list[RetentionDecision]) -> dict[str, int]:
    reason_counts: dict[str, int] = {}
    for decision in decisions:
        reason_counts[decision.reason_code] = (
            reason_counts.get(decision.reason_code, 0) + int(decision.count)
        )
    return reason_counts


def _effective_limit(
    *,
    policy: SupportsRunnerControlRetentionPolicy,
    limit: int,
) -> int:
    return min(
        _normalize_positive_int(limit, field_name="limit"),
        _normalize_positive_int(
            policy.retention_batch_size_per_tenant,
            field_name="policy.retention_batch_size_per_tenant",
        ),
    )


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
    "ACTIVE_CONTROL_MESSAGE_RETENTION_PROTECTED",
    "ACTIVE_RUNNER_CREDENTIAL_RETENTION_PROTECTED",
    "ACTIVE_RUNNER_IDENTITY_RETENTION_PROTECTED",
    "ACTIVE_RUNTIME_JOB_RETENTION_PROTECTED",
    "CONTROL_MESSAGE_RETENTION_EXPIRED",
    "RUNNER_CONNECTION_RETENTION_EXPIRED",
    "RUNTIME_JOB_RETENTION_EXPIRED",
    "RunnerControlRetentionExecutor",
    "SupportsRunnerControlRetentionPolicy",
    "UNEXPIRED_INSTALL_TOKEN_RETENTION_PROTECTED",
]
