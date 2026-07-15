"""Tests for stale runner and runtime-job reconciliation behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.runner_control import ExecutionSite, Runner, RunnerConnection, RuntimeJob
from backend.models.tenant import Tenant
from backend.services.runner_control.db_coordination import DBRunnerCoordinationStore
from backend.services.runner_control.registry_service import RunnerRegistryService


def _build_session_factory(database_url: str = "sqlite+pysqlite:///:memory:") -> sessionmaker[Session]:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Engagement.__table__,
            Task.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerConnection.__table__,
            RuntimeJob.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _build_session() -> Session:
    return _build_session_factory()()


def _build_shared_sessions(tmp_path: Path) -> tuple[Session, Session]:
    database_path = tmp_path / "presence-reconciliation.db"
    factory = _build_session_factory(f"sqlite+pysqlite:///{database_path}")
    return factory(), factory()


def _seed_runner(db: Session) -> tuple[Tenant, ExecutionSite, Runner]:
    tenant = Tenant(slug="tenant-one", name="Tenant One")
    user = User(username="owner", password="hashed")
    db.add_all([tenant, user])
    db.flush()

    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name="Primary Engagement",
        status="active",
    )
    db.add(engagement)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name="Seed Task",
    )
    db.add(task)
    db.flush()

    site = ExecutionSite(
        tenant_id=tenant.id,
        name="Primary Site",
        slug="primary-site",
        status="active",
    )
    db.add(site)
    db.flush()

    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name="runner-alpha",
        status="active",
    )
    db.add(runner)
    db.commit()
    return tenant, site, runner


def test_reconcile_stale_presence_is_concurrency_safe_and_tracks_offline_metadata(tmp_path: Path) -> None:
    db_a, db_b = _build_shared_sessions(tmp_path)
    tenant, site, runner = _seed_runner(db_a)
    store_a = DBRunnerCoordinationStore(db_a, pod_id="pod-a")

    now = datetime.now(tz=UTC)
    store_a.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="stale-conn",
        lease_expires_at=now - timedelta(seconds=30),
        last_seen_at=now - timedelta(seconds=45),
    )
    db_a.add(
        RuntimeJob(
            tenant_id=tenant.id,
            runner_id=runner.id,
            execution_site_id=site.id,
            task_id=None,
            job_type="task.start",
            status="dispatching",
            idempotency_key="runtime-job-lost",
            lease_expires_at=now - timedelta(seconds=15),
        )
    )
    db_a.commit()

    service_a = RunnerRegistryService(db_a)
    service_b = RunnerRegistryService(db_b)

    first = service_a.reconcile_stale_presence(now=now)
    db_a.commit()
    second = service_b.reconcile_stale_presence(now=now)
    db_b.commit()

    assert first.lease_expiry.expired_connection_count == 1
    assert first.lease_expiry.offline_runner_count == 1
    assert len(first.lease_expiry.offline_transitions) == 1
    transition = first.lease_expiry.offline_transitions[0]
    assert transition.tenant_id == tenant.id
    assert transition.runner_id == runner.id
    assert transition.last_seen_at == now
    assert transition.reason == "stale_connection_lease_expired"
    assert first.lost_runtime_job_count == 1

    assert second.lease_expiry.expired_connection_count == 0
    assert second.lease_expiry.offline_runner_count == 0
    assert second.lost_runtime_job_count == 0


def test_reconcile_stale_presence_preserves_runtime_job_with_valid_runner_lease() -> None:
    db = _build_session()
    tenant, site, runner = _seed_runner(db)
    store = DBRunnerCoordinationStore(db, pod_id="pod-a")

    now = datetime.now(tz=UTC)
    store.claim_connection_lease(
        tenant_id=tenant.id,
        runner_id=runner.id,
        pod_id="pod-a",
        connection_id="active-conn",
        lease_expires_at=now + timedelta(seconds=180),
        last_seen_at=now,
    )
    runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=site.id,
        task_id=None,
        job_type="task.start",
        status="dispatching",
        idempotency_key=f"runtime-job-{uuid.uuid4()}",
        lease_expires_at=now - timedelta(seconds=10),
    )
    db.add(runtime_job)
    db.commit()

    service = RunnerRegistryService(db)
    result = service.reconcile_stale_presence(now=now)
    db.commit()

    assert result.lost_runtime_job_count == 0
    refreshed_job = db.get(RuntimeJob, runtime_job.id)
    assert refreshed_job is not None
    assert refreshed_job.status == "dispatching"


@pytest.mark.parametrize("status", ["queued", "assigned", "dispatching", "dispatched"])
def test_reconcile_stale_presence_marks_unassigned_runtime_job_expired(status: str) -> None:
    db = _build_session()
    tenant, site, _runner = _seed_runner(db)

    now = datetime.now(tz=UTC)
    runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=None,
        execution_site_id=site.id,
        task_id=None,
        job_type="task.start",
        status=status,
        idempotency_key=f"runtime-job-{uuid.uuid4()}",
        lease_expires_at=now - timedelta(seconds=10),
    )
    db.add(runtime_job)
    db.commit()

    service = RunnerRegistryService(db)
    result = service.reconcile_stale_presence(now=now)
    db.commit()

    assert result.expired_runtime_job_count == 1
    refreshed_job = db.get(RuntimeJob, runtime_job.id)
    assert refreshed_job is not None
    assert refreshed_job.status == "expired"
    assert refreshed_job.error_code == "RUNTIME_JOB_LEASE_EXPIRED"


@pytest.mark.parametrize("status", ["assigned", "dispatching", "dispatched"])
def test_reconcile_stale_presence_marks_assigned_runtime_job_lost_when_runner_lease_missing(status: str) -> None:
    db = _build_session()
    tenant, site, runner = _seed_runner(db)

    now = datetime.now(tz=UTC)
    runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=runner.id,
        execution_site_id=site.id,
        task_id=None,
        job_type="task.start",
        status=status,
        idempotency_key=f"runtime-job-{uuid.uuid4()}",
        lease_expires_at=now - timedelta(seconds=10),
    )
    db.add(runtime_job)
    db.commit()

    service = RunnerRegistryService(db)
    result = service.reconcile_stale_presence(now=now)
    db.commit()

    assert result.lost_runtime_job_count == 1
    refreshed_job = db.get(RuntimeJob, runtime_job.id)
    assert refreshed_job is not None
    assert refreshed_job.status == "lost"
    assert refreshed_job.error_code == "RUNNER_LEASE_EXPIRED"


def test_reconcile_stale_presence_keeps_terminal_runtime_job_status_unchanged() -> None:
    db = _build_session()
    tenant, site, _runner = _seed_runner(db)

    now = datetime.now(tz=UTC)
    runtime_job = RuntimeJob(
        tenant_id=tenant.id,
        runner_id=None,
        execution_site_id=site.id,
        task_id=None,
        job_type="task.start",
        status="acknowledged",
        idempotency_key=f"runtime-job-{uuid.uuid4()}",
        lease_expires_at=now - timedelta(seconds=10),
    )
    db.add(runtime_job)
    db.commit()

    service = RunnerRegistryService(db)
    result = service.reconcile_stale_presence(now=now)
    db.commit()

    assert result.expired_runtime_job_count == 0
    assert result.lost_runtime_job_count == 0
    refreshed_job = db.get(RuntimeJob, runtime_job.id)
    assert refreshed_job is not None
    assert refreshed_job.status == "acknowledged"
