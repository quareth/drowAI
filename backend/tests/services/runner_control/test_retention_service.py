"""Tests for tenant-scoped runner-control retention executor behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.core.time_utils import utc_now
from backend.database import Base
from backend.models.runner_control import (
    ExecutionSite,
    Runner,
    RunnerConnection,
    RunnerControlMessage,
    RunnerCredential,
    RunnerInstallToken,
    RuntimeJob,
)
from backend.models.tenant import Tenant
from backend.services.retention.contracts import (
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_PROTECTED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
)
from backend.services.runner_control.retention_service import (
    ACTIVE_CONTROL_MESSAGE_RETENTION_PROTECTED,
    ACTIVE_RUNNER_CREDENTIAL_RETENTION_PROTECTED,
    ACTIVE_RUNNER_IDENTITY_RETENTION_PROTECTED,
    ACTIVE_RUNTIME_JOB_RETENTION_PROTECTED,
    CONTROL_MESSAGE_RETENTION_EXPIRED,
    RUNNER_CONNECTION_RETENTION_EXPIRED,
    RUNTIME_JOB_RETENTION_EXPIRED,
    UNEXPIRED_INSTALL_TOKEN_RETENTION_PROTECTED,
    RunnerControlRetentionExecutor,
)


@dataclass(frozen=True, slots=True)
class _Policy:
    runner_control_retention_days: int = 30
    retention_batch_size_per_tenant: int = 100


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def test_runner_control_retention_dry_run_is_tenant_scoped_and_does_not_mutate() -> None:
    db = _build_session()
    try:
        tenant, runner = _seed_runner_scope(db, label="dry-run")
        other_tenant, other_runner = _seed_runner_scope(db, label="other")
        old_job = _seed_runtime_job(db, tenant=tenant, runner=runner, status="succeeded", age_days=45)
        active_job = _seed_runtime_job(db, tenant=tenant, runner=runner, status="running", age_days=45)
        recent_job = _seed_runtime_job(db, tenant=tenant, runner=runner, status="failed", age_days=5)
        foreign_job = _seed_runtime_job(
            db,
            tenant=other_tenant,
            runner=other_runner,
            status="succeeded",
            age_days=45,
        )
        old_message = _seed_control_message(
            db,
            tenant=tenant,
            runner=runner,
            status="acked",
            age_days=45,
        )
        protected_message = _seed_control_message(
            db,
            tenant=tenant,
            runner=runner,
            status="delivered",
            age_days=45,
        )
        old_connection = _seed_connection(
            db,
            tenant=tenant,
            runner=runner,
            status="disconnected",
            age_days=45,
        )

        result = RunnerControlRetentionExecutor(db).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_DRY_RUN,
            limit=100,
        )

        assert result.retention_class == RETENTION_CLASS_OPERATIONAL_EPHEMERAL
        assert result.counts.candidate_count == 3
        assert result.counts.applied_count == 0
        assert result.reason_counts == {
            RUNTIME_JOB_RETENTION_EXPIRED: 1,
            ACTIVE_RUNTIME_JOB_RETENTION_PROTECTED: 1,
            CONTROL_MESSAGE_RETENTION_EXPIRED: 1,
            ACTIVE_CONTROL_MESSAGE_RETENTION_PROTECTED: 1,
            RUNNER_CONNECTION_RETENTION_EXPIRED: 1,
            ACTIVE_RUNNER_IDENTITY_RETENTION_PROTECTED: 1,
        }
        assert {
            (decision.outcome, decision.resource_id, decision.reason_code)
            for decision in result.decisions
        } == {
            (
                RETENTION_DECISION_CANDIDATE,
                f"runtime_job:{old_job.id}",
                RUNTIME_JOB_RETENTION_EXPIRED,
            ),
            (
                RETENTION_DECISION_PROTECTED,
                f"runtime_job:{active_job.id}",
                ACTIVE_RUNTIME_JOB_RETENTION_PROTECTED,
            ),
            (
                RETENTION_DECISION_CANDIDATE,
                f"control_message:{old_message.id}",
                CONTROL_MESSAGE_RETENTION_EXPIRED,
            ),
            (
                RETENTION_DECISION_PROTECTED,
                f"control_message:{protected_message.id}",
                ACTIVE_CONTROL_MESSAGE_RETENTION_PROTECTED,
            ),
            (
                RETENTION_DECISION_CANDIDATE,
                f"runner_connection:{old_connection.id}",
                RUNNER_CONNECTION_RETENTION_EXPIRED,
            ),
            (
                RETENTION_DECISION_PROTECTED,
                None,
                ACTIVE_RUNNER_IDENTITY_RETENTION_PROTECTED,
            ),
        }
        assert db.get(RuntimeJob, old_job.id) is not None
        assert db.get(RuntimeJob, active_job.id) is not None
        assert db.get(RuntimeJob, recent_job.id) is not None
        assert db.get(RuntimeJob, foreign_job.id) is not None
        assert db.get(RunnerControlMessage, old_message.id) is not None
        assert db.get(RunnerControlMessage, protected_message.id) is not None
        assert db.get(RunnerConnection, old_connection.id) is not None
    finally:
        db.close()


def test_runner_control_retention_apply_deletes_only_terminal_operational_rows() -> None:
    db = _build_session()
    try:
        tenant, runner = _seed_runner_scope(db, label="apply")
        other_tenant, other_runner = _seed_runner_scope(db, label="apply-other")
        old_job = _seed_runtime_job(db, tenant=tenant, runner=runner, status="failed", age_days=45)
        active_job = _seed_runtime_job(db, tenant=tenant, runner=runner, status="dispatching", age_days=45)
        foreign_job = _seed_runtime_job(
            db,
            tenant=other_tenant,
            runner=other_runner,
            status="failed",
            age_days=45,
        )
        terminal_message = _seed_control_message(
            db,
            tenant=tenant,
            runner=runner,
            status="failed",
            age_days=45,
        )
        delivered_message = _seed_control_message(
            db,
            tenant=tenant,
            runner=runner,
            status="delivered",
            age_days=45,
        )
        stale_connection = _seed_connection(
            db,
            tenant=tenant,
            runner=runner,
            status="disconnected",
            age_days=45,
        )
        active_connection = _seed_connection(
            db,
            tenant=tenant,
            runner=runner,
            status="active",
            age_days=45,
        )

        result = RunnerControlRetentionExecutor(db).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert result.counts.candidate_count == 3
        assert result.counts.applied_count == 3
        assert result.reason_counts == {
            RUNTIME_JOB_RETENTION_EXPIRED: 1,
            ACTIVE_RUNTIME_JOB_RETENTION_PROTECTED: 1,
            CONTROL_MESSAGE_RETENTION_EXPIRED: 1,
            ACTIVE_CONTROL_MESSAGE_RETENTION_PROTECTED: 1,
            RUNNER_CONNECTION_RETENTION_EXPIRED: 1,
            ACTIVE_RUNNER_IDENTITY_RETENTION_PROTECTED: 1,
        }
        assert {
            (decision.outcome, decision.resource_id)
            for decision in result.decisions
            if decision.outcome == RETENTION_DECISION_APPLIED
        } == {
            (RETENTION_DECISION_APPLIED, f"runtime_job:{old_job.id}"),
            (RETENTION_DECISION_APPLIED, f"control_message:{terminal_message.id}"),
            (RETENTION_DECISION_APPLIED, f"runner_connection:{stale_connection.id}"),
        }
        assert _row_exists(db, RuntimeJob, old_job.id) is False
        assert _row_exists(db, RunnerControlMessage, terminal_message.id) is False
        assert _row_exists(db, RunnerConnection, stale_connection.id) is False
        assert _row_exists(db, RuntimeJob, active_job.id) is True
        assert _row_exists(db, RuntimeJob, foreign_job.id) is True
        assert _row_exists(db, RunnerControlMessage, delivered_message.id) is True
        assert _row_exists(db, RunnerConnection, active_connection.id) is True
    finally:
        db.close()


def test_runner_control_retention_protects_active_credentials_tokens_and_runner_identity() -> None:
    db = _build_session()
    try:
        tenant, runner = _seed_runner_scope(db, label="protected")
        active_credential = _seed_credential(
            db,
            tenant=tenant,
            runner=runner,
            status="active",
            age_days=45,
            expires_in_days=45,
        )
        revoked_credential = _seed_credential(
            db,
            tenant=tenant,
            runner=runner,
            status="revoked",
            age_days=45,
            expires_in_days=-1,
        )
        unexpired_token = _seed_install_token(
            db,
            tenant=tenant,
            runner=runner,
            status="issued",
            age_days=45,
            expires_in_days=45,
        )
        expired_token = _seed_install_token(
            db,
            tenant=tenant,
            runner=runner,
            status="issued",
            age_days=45,
            expires_in_days=-1,
        )

        result = RunnerControlRetentionExecutor(db).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert result.counts.candidate_count == 0
        assert result.counts.applied_count == 0
        assert result.reason_counts == {
            ACTIVE_RUNNER_IDENTITY_RETENTION_PROTECTED: 1,
            ACTIVE_RUNNER_CREDENTIAL_RETENTION_PROTECTED: 1,
            UNEXPIRED_INSTALL_TOKEN_RETENTION_PROTECTED: 1,
        }
        assert db.get(Runner, runner.id) is not None
        assert db.get(RunnerCredential, active_credential.id) is not None
        assert db.get(RunnerCredential, revoked_credential.id) is not None
        assert db.get(RunnerInstallToken, unexpired_token.id) is not None
        assert db.get(RunnerInstallToken, expired_token.id) is not None
        safe_result = str(result.to_safe_dict())
        assert "secret" not in safe_result
        assert "token_hash" not in safe_result
    finally:
        db.close()


def test_runner_control_retention_honors_per_tenant_batch_limit() -> None:
    db = _build_session()
    try:
        tenant, runner = _seed_runner_scope(db, label="batch")
        old_jobs = [
            _seed_runtime_job(db, tenant=tenant, runner=runner, status="succeeded", age_days=45)
            for _ in range(3)
        ]

        result = RunnerControlRetentionExecutor(db).run(
            policy=_Policy(retention_batch_size_per_tenant=2),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert result.counts.candidate_count == 2
        assert result.counts.batch_count == 2
        assert result.counts.batch_limit == 2
        assert result.counts.applied_count == 2
        assert (
            db.query(RuntimeJob)
            .filter(
                RuntimeJob.tenant_id == tenant.id,
                RuntimeJob.id.in_([job.id for job in old_jobs]),
            )
            .count()
            == 1
        )
    finally:
        db.close()


def _seed_runner_scope(db: Session, *, label: str) -> tuple[Tenant, Runner]:
    tenant = Tenant(slug=f"tenant-{label}", name=f"Tenant {label}")
    db.add(tenant)
    db.flush()
    site = ExecutionSite(
        tenant_id=tenant.id,
        name=f"Site {label}",
        slug=f"site-{label}",
        status="active",
    )
    db.add(site)
    db.flush()
    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name=f"runner-{label}",
        status="active",
        last_seen_at=utc_now() - timedelta(days=45),
        created_at=utc_now() - timedelta(days=45),
        updated_at=utc_now() - timedelta(days=45),
    )
    db.add(runner)
    db.flush()
    return tenant, runner


def _row_exists(
    db: Session,
    model: type[RuntimeJob] | type[RunnerControlMessage] | type[RunnerConnection],
    row_id: object,
) -> bool:
    return bool(db.query(model.id).filter(model.id == row_id).first())


def _seed_runtime_job(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    status: str,
    age_days: int,
) -> RuntimeJob:
    timestamp = utc_now() - timedelta(days=age_days)
    job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=runner.execution_site_id,
        job_type="task.start",
        status=status,
        idempotency_key=f"job-{status}-{age_days}-{uuid.uuid4()}",
        created_at=timestamp,
        updated_at=timestamp,
    )
    db.add(job)
    db.flush()
    return job


def _seed_control_message(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    status: str,
    age_days: int,
) -> RunnerControlMessage:
    timestamp = utc_now() - timedelta(days=age_days)
    message = RunnerControlMessage(
        tenant_id=tenant.id,
        runner_id=runner.id,
        message_id=f"message-{status}-{age_days}-{uuid.uuid4()}",
        direction="outbound",
        type="task.start",
        status=status,
        created_at=timestamp,
        updated_at=timestamp,
    )
    db.add(message)
    db.flush()
    return message


def _seed_connection(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    status: str,
    age_days: int,
) -> RunnerConnection:
    timestamp = utc_now() - timedelta(days=age_days)
    connection = RunnerConnection(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id=f"pod-{status}-{age_days}",
        connection_id=f"connection-{status}-{age_days}",
        status=status,
        lease_expires_at=timestamp,
        last_seen_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
    )
    db.add(connection)
    db.flush()
    return connection


def _seed_credential(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    status: str,
    age_days: int,
    expires_in_days: int,
) -> RunnerCredential:
    timestamp = utc_now() - timedelta(days=age_days)
    credential = RunnerCredential(
        tenant_id=tenant.id,
        runner_id=runner.id,
        credential_fingerprint=f"fingerprint-{status}-{age_days}",
        secret_hash=f"sha256${status}-{age_days}",
        status=status,
        expires_at=utc_now() + timedelta(days=expires_in_days),
        revoked_at=timestamp if status == "revoked" else None,
        created_at=timestamp,
    )
    db.add(credential)
    db.flush()
    return credential


def _seed_install_token(
    db: Session,
    *,
    tenant: Tenant,
    runner: Runner,
    status: str,
    age_days: int,
    expires_in_days: int,
) -> RunnerInstallToken:
    timestamp = utc_now() - timedelta(days=age_days)
    token = RunnerInstallToken(
        tenant_id=tenant.id,
        execution_site_id=runner.execution_site_id,
        token_hash=f"sha256$token-{status}-{age_days}-{expires_in_days}",
        status=status,
        expires_at=utc_now() + timedelta(days=expires_in_days),
        used_at=timestamp if status == "used" else None,
        created_at=timestamp,
    )
    db.add(token)
    db.flush()
    return token
